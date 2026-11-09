"""
Operator web UI and JSON API: a separate aiohttp application served on its own TLS listener,
with authentication, CSRF protection, security headers and rate limiting on every request.
"""
from __future__ import annotations

from aiohttp import web

from .auth import Authenticator, make_auth_middleware, make_headers_middleware, make_rate_limit_middleware


def setup_web(vtn) -> web.Application:
    from .api import setup_api
    from .ui import setup_ui
    auth = Authenticator(vtn.config)
    vtn.auth = auth
    app = web.Application(client_max_size=vtn.config.limits.max_message_body_bytes, middlewares=[
        make_rate_limit_middleware(vtn.config.limits.requests_per_minute_per_ip),
        make_headers_middleware(),
        make_auth_middleware(auth),
    ])
    app["vtn"] = vtn
    setup_api(vtn, app)
    setup_ui(vtn, app)
    return app
