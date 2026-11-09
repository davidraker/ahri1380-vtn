"""
Operator authentication and request hardening for the UI/API listener.

Every request must be authenticated: an operator client certificate (always accepted, required
when `ui.client_cert = "required"`), HTTP Basic credentials checked against a PBKDF2 hash, or a
Bearer token from the environment. State-changing requests additionally need a CSRF token (UI
forms) or a Bearer token / same-origin JSON request (API), and a same-origin Origin header.
Security headers and a per-address rate limit are applied to every response.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from aiohttp import web
from markupsafe import Markup

from ..config import Config
from ..tls import evidence_from_transport

logger = logging.getLogger("ahri_vtn.auth")

PBKDF2_ITERATIONS = 600_000
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------------------------
# Passwords and tokens
# ---------------------------------------------------------------------------------------------

def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, hashed: str) -> bool:
    try:
        scheme, iterations, salt, digest = hashed.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations),
                                     dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _constant_time_in(value: str, candidates: list[str]) -> bool:
    found = False
    for candidate in candidates:
        if hmac.compare_digest(value.encode(), candidate.encode()):
            found = True
    return found


# ---------------------------------------------------------------------------------------------
# Principals
# ---------------------------------------------------------------------------------------------

@dataclass
class Principal:
    kind: str           # certificate | password | token
    name: str

    def __str__(self):
        return f"{self.kind}:{self.name}"


class Throttle:
    """Authentication failures per client address within a sliding minute (bounded memory)."""

    def __init__(self, limit: int):
        self.limit = limit
        self.failures: dict[str, deque] = {}

    def blocked(self, remote: str) -> bool:
        q = self.failures.get(remote)
        if not q:
            return False
        now = time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if not q:
            del self.failures[remote]
            return False
        return len(q) >= self.limit

    def record(self, remote: str):
        self.failures.setdefault(remote, deque()).append(time.monotonic())
        if len(self.failures) > 10000:
            stale = [k for k, q in self.failures.items() if not q or time.monotonic() - q[-1] > 60]
            for k in stale:
                del self.failures[k]


class RateLimiter:
    """Token bucket per client address (requests per minute)."""

    def __init__(self, per_minute: int):
        self.rate = per_minute / 60.0
        self.capacity = float(per_minute)
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, remote: str) -> bool:
        now = time.monotonic()
        tokens, last = self.buckets.get(remote, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < 1:
            self.buckets[remote] = (tokens, now)
            return False
        self.buckets[remote] = (tokens - 1, now)
        if len(self.buckets) > 10000:      # forgetful under churn
            self.buckets = dict(list(self.buckets.items())[-5000:])
        return True


class Authenticator:
    def __init__(self, config: Config):
        self.config = config
        self.ui = config.ui
        self.secret = secrets.token_bytes(32)      # per process: CSRF tokens and flash signatures
        self.throttle = Throttle(config.limits.auth_failures_per_minute)
        self.allowed_operator_fingerprints = list(self.ui.operator_fingerprints)   # normalised by UiConfig
        # PBKDF2 costs ~0.1 s; verified credentials are remembered (keyed by a keyed hash of the
        # credentials, never the credentials) so a browser session does not pay it on every request.
        self._verified: dict[bytes, float] = {}
        self._verified_ttl = 600.0

    # ------------------------------------------------------------------ identification

    def certificate_principal(self, request: web.Request) -> Optional[Principal]:
        """The operator certificate presented on this connection, if it is acceptable."""
        evidence = evidence_from_transport(request.transport)
        if evidence.peer is None:
            return None
        # The handshake already validated the chain against the operator CA (CERT_OPTIONAL /
        # CERT_REQUIRED verify a presented certificate); the allow-list is the second gate.
        if self.allowed_operator_fingerprints and evidence.peer.fingerprint not in self.allowed_operator_fingerprints:
            logger.warning(f"Operator certificate {evidence.peer.subject} ({evidence.peer.fingerprint}) is not in "
                           "ui.operator_fingerprints")
            return None
        cn = next((part[3:] for part in evidence.peer.subject.split(",") if part.startswith("CN=")),
                  evidence.peer.subject)
        return Principal("certificate", cn)

    async def password_principal(self, request: web.Request) -> Optional[Principal]:
        if not self.ui.password_auth:
            return None
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        user_ok = hmac.compare_digest(user.encode(), (self.ui.basic_auth_user or "").encode())
        if not user_ok:
            return None
        key = hmac.new(self.secret, f"{user}\0{password}".encode(), hashlib.sha256).digest()
        now = time.monotonic()
        expires = self._verified.get(key)
        if expires is not None and expires > now:
            return Principal("password", user)
        # Cache miss: the slow hash runs in a worker thread so the event loop keeps serving
        pw_ok = await asyncio.to_thread(verify_password, password, self.ui.basic_auth_password_hash.get_secret_value())
        if not pw_ok:
            return None
        if len(self._verified) > 100:
            self._verified = {k: v for k, v in self._verified.items() if v > now}
        self._verified[key] = now + self._verified_ttl
        return Principal("password", user)

    def token_principal(self, request: web.Request) -> Optional[Principal]:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        tokens = self.ui.api_tokens()
        if tokens and _constant_time_in(header[7:].strip(), tokens):
            return Principal("token", "api")
        return None

    async def authenticate(self, request: web.Request) -> Optional[Principal]:
        """
        client_cert = "optional": an acceptable operator certificate logs in by itself; otherwise
        a Bearer token or Basic credentials are needed.
        client_cert = "required": the certificate is mandatory (the handshake already refused
        connections without one); with password_auth = true the Basic credentials are a second
        factor and are required as well.
        """
        cert = self.certificate_principal(request)
        if self.ui.client_cert == "required":
            if cert is None:
                return None
            if self.ui.password_auth and await self.password_principal(request) is None:
                return None
            return cert
        if cert is not None:
            return cert
        return self.token_principal(request) or await self.password_principal(request)

    # ------------------------------------------------------------------ CSRF and flash signing

    def csrf_token(self, principal: Principal) -> str:
        return hmac.new(self.secret, f"csrf:{principal}".encode(), hashlib.sha256).hexdigest()

    def csrf_field(self, principal: Principal) -> Markup:
        return Markup(f'<input type="hidden" name="csrf_token" value="{self.csrf_token(principal)}">')

    def csrf_ok(self, principal: Principal, token) -> bool:
        if not isinstance(token, str) or not token:
            return False
        return hmac.compare_digest(token.encode("utf-8", "replace"), self.csrf_token(principal).encode())

    def sign(self, text: str) -> str:
        return hmac.new(self.secret, f"flash:{text}".encode(), hashlib.sha256).hexdigest()[:24]

    def signature_ok(self, text: str, signature: Optional[str]) -> bool:
        return bool(signature) and hmac.compare_digest(signature, self.sign(text))


# ---------------------------------------------------------------------------------------------
# Middlewares
# ---------------------------------------------------------------------------------------------

def _same_origin(request: web.Request) -> bool:
    """Reject cross-site requests using Sec-Fetch-Site / Origin (whichever the client sends)."""
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        host = urlsplit(origin).netloc
        if host != request.host:
            return False
    elif origin == "null":
        return False
    referer = request.headers.get("Referer")
    if site is None and not origin and referer:
        if urlsplit(referer).netloc != request.host:
            return False
    return True


def make_auth_middleware(auth: Authenticator):
    @web.middleware
    async def middleware(request: web.Request, handler):
        remote = request.remote or "?"
        if auth.throttle.blocked(remote):
            logger.warning(f"Too many authentication failures from {remote}")
            return web.Response(status=429, text="Too many authentication failures; wait a minute",
                                headers={"Retry-After": "60"})
        principal = await auth.authenticate(request)
        if principal is None:
            auth.throttle.record(remote)
            headers = {}
            if auth.ui.password_auth and not request.headers.get("Authorization", "").startswith("Bearer "):
                headers["WWW-Authenticate"] = 'Basic realm="AHRI 1380 VTN"'
            logger.warning(f"Unauthenticated request to {request.path} from {remote}")
            return web.Response(status=401, text="Authentication required", headers=headers)
        request["principal"] = principal

        if request.method in STATE_CHANGING:
            logger.info(f"audit: {principal} {request.method} {request.path} from {remote}")
            if not _same_origin(request):
                logger.warning(f"Cross-origin {request.method} {request.path} from {remote} refused")
                return web.Response(status=403, text="Cross-origin request refused")
            if request.path.startswith("/api"):
                # A browser form can only submit these content types; a script sends JSON or nothing.
                if principal.kind != "token" and request.content_type in ("application/x-www-form-urlencoded",
                                                                           "multipart/form-data", "text/plain"):
                    return web.Response(status=403, text="API state changes from a browser session must be JSON "
                                                         "(or use a Bearer token)")
            else:   # UI form posts
                data = await request.post()
                if not auth.csrf_ok(principal, data.get("csrf_token")):
                    logger.warning(f"CSRF token missing or wrong on {request.path} from {remote}")
                    return web.Response(status=403, text="Form token missing or stale; reload the page and retry")
        return await handler(request)
    return middleware


def make_headers_middleware():
    csp = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
           "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    @web.middleware
    async def middleware(request: web.Request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        response.headers.setdefault("Cache-Control", "no-store")
        if isinstance(response, web.HTTPException):
            raise response
        return response
    return middleware


def make_rate_limit_middleware(per_minute: int):
    limiter = RateLimiter(per_minute)

    @web.middleware
    async def middleware(request: web.Request, handler):
        if not limiter.allow(request.remote or "?"):
            return web.Response(status=429, text="Rate limit exceeded", headers={"Retry-After": "1"})
        return await handler(request)
    return middleware
