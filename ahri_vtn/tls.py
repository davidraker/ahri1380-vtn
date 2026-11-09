"""
TLS for the two listeners: SSL contexts built from the configuration, the OpenADR 2.0b cipher
suite guarantee (rule 67), certificate inspection and fingerprints (section 10.5.1), and the
loopback self-check behind `ahri-vtn tls-selfcheck`.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, ed448, rsa

from .config import Config, TlsConfig, UiConfig

logger = logging.getLogger("ahri_vtn.tls")

# OpenADR 2.0b section 10.4 / rule 67: the two suites a VTN MUST support, in OpenSSL spelling.
OPENADR_SUITES = {
    "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA256": "ECDHE-ECDSA-AES128-SHA256",
    "TLS_RSA_WITH_AES_128_CBC_SHA256": "AES128-SHA256",
}
# 2.0b suites first (so a VEN offering only those is served), then modern AEAD suites.
OPENADR_DEFAULT_CIPHERS = "ECDHE-ECDSA-AES128-SHA256:AES128-SHA256:ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM"

MIN_RSA_BITS = 2048     # rule 68
MIN_ECC_BITS = 224      # rule 68


class TlsSetupError(RuntimeError):
    """The TLS configuration cannot provide what OpenADR 2.0b requires."""


# ---------------------------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------------------------

from .fingerprint import normalize_fingerprint, openadr_fingerprint, sha256_fingerprint  # noqa: E402,F401


@dataclass
class CertInfo:
    subject: str
    issuer: str
    not_before: datetime
    not_after: datetime
    key_type: str           # RSA | EC | Ed25519 | Ed448 | other
    key_bits: Optional[int]
    fingerprint: str        # OpenADR form
    sha256: str
    sans: list
    version: int            # 3 for X.509v3
    serial: str

    def as_dict(self) -> dict:
        d = asdict(self)
        d["not_before"] = self.not_before.isoformat()
        d["not_after"] = self.not_after.isoformat()
        return d

    @property
    def expired(self) -> bool:
        return self.not_after < datetime.now(timezone.utc)

    def expires_within(self, days: int) -> bool:
        return self.not_after < datetime.now(timezone.utc) + timedelta(days=days)

    @property
    def key_too_short(self) -> bool:
        if self.key_type == "RSA":
            return (self.key_bits or 0) < MIN_RSA_BITS
        if self.key_type == "EC":
            return (self.key_bits or 0) < MIN_ECC_BITS
        return False

    def problems(self, expiry_warning_days: int = 30) -> list[tuple[str, str, str]]:
        """(code, severity, message) for the conformance findings on this certificate."""
        out = []
        if self.expired:
            out.append(("cert-expired", "warning", f"Certificate '{self.subject}' expired {self.not_after:%Y-%m-%d}"))
        elif self.expires_within(expiry_warning_days):
            out.append(("cert-expiring-soon", "warning",
                        f"Certificate '{self.subject}' expires {self.not_after:%Y-%m-%d} (2.0b 10.4: warn before expiry)"))
        if self.key_too_short:
            out.append(("cert-key-too-short", "warning",
                        f"{self.key_type} key of {self.key_bits} bits is below the 2.0b rule 68 minimum"))
        if self.version != 3:
            out.append(("cert-not-x509v3", "warning", f"Certificate is X.509 v{self.version}; 2.0b requires v3"))
        return out


def inspect_der(der: bytes) -> CertInfo:
    cert = x509.load_der_x509_certificate(der)
    return _inspect(cert, der)


def inspect_pem_file(path: str | Path) -> CertInfo:
    """Inspect the first certificate in a PEM file."""
    data = Path(path).read_bytes()
    cert = x509.load_pem_x509_certificate(data)
    return _inspect(cert, cert.public_bytes(encoding=__import__("cryptography.hazmat.primitives.serialization",
                                                                fromlist=["Encoding"]).Encoding.DER))


def _inspect(cert: x509.Certificate, der: bytes) -> CertInfo:
    key = cert.public_key()
    if isinstance(key, rsa.RSAPublicKey):
        key_type, bits = "RSA", key.key_size
    elif isinstance(key, ec.EllipticCurvePublicKey):
        key_type, bits = "EC", key.curve.key_size
    elif isinstance(key, ed25519.Ed25519PublicKey):
        key_type, bits = "Ed25519", 256
    elif isinstance(key, ed448.Ed448PublicKey):
        key_type, bits = "Ed448", 456
    else:
        key_type, bits = type(key).__name__, None
    sans: list = []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        sans = [str(v) for v in san.get_values_for_type(x509.DNSName)] + \
               [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass
    return CertInfo(subject=cert.subject.rfc4514_string(), issuer=cert.issuer.rfc4514_string(),
                    not_before=cert.not_valid_before_utc, not_after=cert.not_valid_after_utc,
                    key_type=key_type, key_bits=bits, fingerprint=openadr_fingerprint(der),
                    sha256=sha256_fingerprint(der), sans=sans, version=cert.version.value + 1,
                    serial=format(cert.serial_number, "X"))


def pem_certificates(path: str | Path) -> list[CertInfo]:
    """Every certificate in a PEM bundle."""
    data = Path(path).read_bytes()
    return [_inspect(c, c.public_bytes(_DER)) for c in x509.load_pem_x509_certificates(data)]


from cryptography.hazmat.primitives.serialization import Encoding as _Encoding   # noqa: E402
_DER = _Encoding.DER


# ---------------------------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------------------------

def _min_version(value: str) -> ssl.TLSVersion:
    return ssl.TLSVersion.TLSv1_3 if value == "1.3" else ssl.TLSVersion.TLSv1_2


def _cipher_string(value: str) -> str:
    return OPENADR_DEFAULT_CIPHERS if value in ("openadr-default", "", None) else value


def _load_chain(ctx: ssl.SSLContext, cert: str, key: str, passphrase: Optional[str], label: str):
    for path in (cert, key):
        if not Path(path).exists():
            raise TlsSetupError(f"{label}: file {path} does not exist")
    try:
        ctx.load_cert_chain(cert, key, passphrase)
    except ssl.SSLError as err:
        raise TlsSetupError(f"{label}: could not load {cert} / {key}: {err}") from err
    info = inspect_pem_file(cert)
    if info.expired:
        raise TlsSetupError(f"{label}: certificate {cert} expired on {info.not_after:%Y-%m-%d}")
    return info


def enabled_suites(ctx: ssl.SSLContext) -> set[str]:
    return {c["name"] for c in ctx.get_ciphers()}


def missing_openadr_suites(ctx: ssl.SSLContext, has_rsa: bool, has_ecdsa: bool) -> list[str]:
    """
    The 2.0b suites this context cannot negotiate: either absent from the cipher list or lacking
    the certificate type they need (static RSA needs an RSA certificate, ECDHE-ECDSA an ECDSA one).
    """
    names = enabled_suites(ctx)
    missing = []
    if "ECDHE-ECDSA-AES128-SHA256" not in names or not has_ecdsa:
        missing.append("TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA256"
                       + ("" if has_ecdsa else " (no ECDSA server certificate loaded: set [tls] ecc_cert / ecc_key)"))
    if "AES128-SHA256" not in names or not has_rsa:
        missing.append("TLS_RSA_WITH_AES_128_CBC_SHA256"
                       + ("" if has_rsa else " (no RSA server certificate loaded)")
                       + ("" if "AES128-SHA256" in names else
                          " (static RSA key exchange is disabled by the OpenSSL build or the system crypto policy)"))
    return missing


@dataclass
class ContextReport:
    context: ssl.SSLContext
    certificates: list      # CertInfo of the loaded server chains
    suites: list            # enabled cipher names
    missing_openadr_suites: list
    warnings: list


def build_openadr_context(tls: TlsConfig) -> Optional[ContextReport]:
    """
    The OpenADR listener's context. None in plain mode. Raises TlsSetupError when the
    configuration cannot satisfy 2.0b (unless require_default_suites is False, in which case the
    shortfall is reported as a warning).
    """
    if tls.mode == "plain":
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = _min_version(tls.min_version)
    try:
        ctx.set_ciphers(_cipher_string(tls.ciphers))
    except ssl.SSLError as err:
        raise TlsSetupError(f"[tls] ciphers {tls.ciphers!r} rejected by OpenSSL: {err}") from err
    certs = [_load_chain(ctx, tls.cert, tls.key, tls.key_passphrase(), "[tls] cert/key")]
    if tls.ecc_cert:
        certs.append(_load_chain(ctx, tls.ecc_cert, tls.ecc_key, tls.ecc_key_passphrase(), "[tls] ecc_cert/ecc_key"))
    if tls.mode == "mutual":
        if not Path(tls.client_ca_file).exists():
            raise TlsSetupError(f"[tls] client_ca_file {tls.client_ca_file} does not exist")
        try:
            ctx.load_verify_locations(tls.client_ca_file)
        except ssl.SSLError as err:
            raise TlsSetupError(f"[tls] client_ca_file {tls.client_ca_file} could not be loaded: {err}") from err
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        ctx.verify_mode = ssl.CERT_NONE

    has_rsa = any(c.key_type == "RSA" for c in certs)
    has_ecdsa = any(c.key_type == "EC" for c in certs)
    missing = missing_openadr_suites(ctx, has_rsa, has_ecdsa) if tls.min_version == "1.2" else \
        ["both 2.0b default suites (TLS 1.2 suites) because min_version is 1.3"]
    warnings = []
    for c in certs:
        for code, _sev, message in c.problems(tls.expiry_warning_days):
            if code != "cert-expired":
                warnings.append(message)
    if missing:
        text = ("The OpenADR listener cannot offer the OpenADR 2.0b default cipher suite(s) required by "
                "rule 67:\n  - " + "\n  - ".join(missing)
                + "\nSee the user guide section 'Guaranteeing the mandated cipher suites'.")
        if tls.require_default_suites:
            raise TlsSetupError(text + "\nSet [tls] require_default_suites = false to run anyway (for modern-only VENs).")
        warnings.append(text)
    return ContextReport(ctx, certs, sorted(enabled_suites(ctx)), missing, warnings)


def build_ui_context(ui: UiConfig, tls: TlsConfig) -> ContextReport:
    """
    The UI/API listener's context: always TLS, operator certificates always requested against
    the operator CA, required or optional per configuration.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    certs = []
    if ui.cert and ui.key:
        certs.append(_load_chain(ctx, ui.cert, ui.key, ui.key_passphrase(), "[ui] cert/key"))
    else:
        certs.append(_load_chain(ctx, tls.cert, tls.key, tls.key_passphrase(), "[tls] cert/key (reused for the UI)"))
        if tls.ecc_cert:
            certs.append(_load_chain(ctx, tls.ecc_cert, tls.ecc_key, tls.ecc_key_passphrase(),
                                     "[tls] ecc_cert/ecc_key (reused for the UI)"))
    if not Path(ui.operator_ca_file).exists():
        raise TlsSetupError(f"[ui] operator_ca_file {ui.operator_ca_file} does not exist")
    try:
        ctx.load_verify_locations(ui.operator_ca_file)
    except ssl.SSLError as err:
        raise TlsSetupError(f"[ui] operator_ca_file {ui.operator_ca_file} could not be loaded: {err}") from err
    ctx.verify_mode = ssl.CERT_REQUIRED if ui.client_cert == "required" else ssl.CERT_OPTIONAL
    return ContextReport(ctx, certs, sorted(enabled_suites(ctx)), [], [])


# ---------------------------------------------------------------------------------------------
# Connection evidence
# ---------------------------------------------------------------------------------------------

@dataclass
class TlsEvidence:
    version: Optional[str] = None
    cipher: Optional[str] = None
    peer_der: Optional[bytes] = None
    peer: Optional[CertInfo] = None
    remote: Optional[str] = None        # peer address of the connection

    @property
    def secure(self) -> bool:
        return self.version is not None

    @property
    def fingerprint(self) -> Optional[str]:
        return self.peer.fingerprint if self.peer else None

    @property
    def cipher_is_openadr_default(self) -> Optional[bool]:
        if self.cipher is None:
            return None
        return self.cipher in OPENADR_SUITES.values()


def evidence_from_transport(transport, remote: Optional[str] = None) -> TlsEvidence:
    ssl_object = transport.get_extra_info("ssl_object") if transport is not None else None
    if ssl_object is None:
        return TlsEvidence(remote=remote)
    cipher = ssl_object.cipher()
    der = ssl_object.getpeercert(binary_form=True)
    peer = inspect_der(der) if der else None
    return TlsEvidence(version=ssl_object.version(), cipher=cipher[0] if cipher else None, peer_der=der, peer=peer,
                       remote=remote)


# ---------------------------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------------------------

@dataclass
class SelfCheckResult:
    suite: str              # IANA name
    openssl_name: str
    ok: bool
    negotiated: Optional[str]
    version: Optional[str]
    error: Optional[str]


async def _handshake(server_ctx: ssl.SSLContext, openssl_name: str) -> SelfCheckResult:
    iana = next(k for k, v in OPENADR_SUITES.items() if v == openssl_name)
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    client.minimum_version = ssl.TLSVersion.TLSv1_2
    client.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        client.set_ciphers(openssl_name)
    except ssl.SSLError as err:
        return SelfCheckResult(iana, openssl_name, False, None, None, f"client side cannot enable the suite: {err}")

    async def handle(reader, writer):
        try:
            await reader.read(1)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port, ssl=client), 5)
        ssl_object = writer.get_extra_info("ssl_object")
        negotiated = ssl_object.cipher()[0]
        version = ssl_object.version()
        writer.close()
        return SelfCheckResult(iana, openssl_name, negotiated == openssl_name, negotiated, version,
                               None if negotiated == openssl_name else "a different suite was negotiated")
    except Exception as err:   # noqa: BLE001
        return SelfCheckResult(iana, openssl_name, False, None, None, f"{err.__class__.__name__}: {err}")
    finally:
        server.close()
        await server.wait_closed()


async def self_check(config: Config) -> list[SelfCheckResult]:
    """
    Build the configured OpenADR context (client-certificate requirement switched off for the
    check) and perform one real handshake per OpenADR 2.0b default suite.
    """
    report = build_openadr_context(config.tls.model_copy(update={"require_default_suites": False}))
    if report is None:
        raise TlsSetupError("tls.mode is 'plain': there is no TLS to check")
    ctx = report.context
    ctx.verify_mode = ssl.CERT_NONE
    results = []
    for openssl_name in OPENADR_SUITES.values():
        results.append(await _handshake(ctx, openssl_name))
    return results


def describe_runtime() -> dict:
    return {"openssl": ssl.OPENSSL_VERSION, "python_ssl_default_suites": sorted(
        c["name"] for c in ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).get_ciphers())}
