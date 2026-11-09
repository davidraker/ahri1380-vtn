"""
Assembly of the AHRI 1380 VTN: OpenLEADR server (OpenADR listener, mutual TLS by default),
handlers, persistence, and the separate TLS-protected operator UI/API listener.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from aiohttp import web
from openleadr import OpenADRServer

from . import tls as tlsmod
from .aio import service_namespace
from .bus import LiveBus
from .config import Config
from .db import Database
from .groups import GroupManager
from .handlers import EventManager, MessageLogger, RegistrationHandler, ReportHandler
from .handlers.messages import current_tls
from .queries import Queries
from .util import fire_and_forget
from .web.auth import make_rate_limit_middleware

logger = logging.getLogger("ahri_vtn")

INSECURE_BANNER = """
********************************************************************************
*  INSECURE: the OpenADR listener runs plain HTTP (tls.mode = "plain").       *
*  VENs are not authenticated. Development against the simulator only; never  *
*  expose this listener to equipment or to a network you do not control.      *
********************************************************************************"""


class AhriVtn:
    def __init__(self, config: Config, db: Optional[Database] = None, enable_web: bool = True):
        self.config = config
        self.db = db or Database(config.db_url, password=config.db_password(), pool_size=config.db_pool_size)
        self.db.create_all()
        logger.info(f"Database: {self.db.describe()['backend']} ({self.db.url})")
        self.bus = LiveBus()

        self.registry = RegistrationHandler(self)
        self.reports = ReportHandler(self)
        self.events = EventManager(self)
        self.messages = MessageLogger(self)
        self.queries = Queries(self)
        self.groups = GroupManager(self)

        # --- transport security of the OpenADR listener
        self.tls_report = tlsmod.build_openadr_context(config.tls)      # None in plain mode
        for warning in (self.tls_report.warnings if self.tls_report else []):
            logger.warning(warning)
        if self.tls_report is None:
            logger.warning(INSECURE_BANNER)

        sig = config.signatures
        signing = dict(cert=sig.cert, key=sig.key, passphrase=sig.key_passphrase()) if sig.mode != "none" and sig.cert else {}
        self.server = OpenADRServer(
            vtn_id=config.vtn_id, http_host=config.host, http_port=config.port,
            http_path_prefix=config.http_path_prefix,
            requested_poll_freq=timedelta(seconds=config.requested_poll_freq_seconds),
            ven_lookup=self.registry.ven_lookup, validate_xml=config.schema_mode,
            ssl_context=self.tls_report.context if self.tls_report else None,
            verify_message_signatures=sig.mode,
            verify_fingerprints=(config.tls.mode == "mutual" and config.tls.fingerprint_policy != "ca-only"),
            show_fingerprint=False, **signing)
        self.server.app._client_max_size = config.limits.max_message_body_bytes
        self.server.app.middlewares.append(make_rate_limit_middleware(config.limits.requests_per_minute_per_ip))
        self.server.app.middlewares.append(_tls_evidence_middleware)

        self.server.add_handler("on_create_party_registration", self.registry.on_create_party_registration)
        self.server.add_handler("on_query_registration", self.registry.on_query_registration)
        self.server.add_handler("on_cancel_party_registration", self.registry.on_cancel_party_registration)
        self.server.add_handler("on_register_report", self.reports.on_register_report)
        self.server.add_handler("on_update_report", self.reports.on_update_report)
        self.server.add_handler("on_created_report", self.reports.on_created_report)
        self.server.add_handler("on_canceled_report", self.reports.on_canceled_report)
        self.server.add_handler("on_create_opt", self.events.on_create_opt)
        self.server.add_handler("on_cancel_opt", self.events.on_cancel_opt)

        self.conformance = None
        self._housekeeping_task: Optional[asyncio.Task] = None
        self._started = False

        from .conformance.engine import ConformanceEngine   # noqa: E402 (avoid import cycle)
        self.conformance = ConformanceEngine(self)
        # Awaitable (non-blocking) views of the services for the web layer, scenarios and tests
        self.aio = service_namespace(self)

        # --- operator UI / API: its own application and TLS listener
        self.web_app: Optional[web.Application] = None
        self.ui_tls_report = None
        self._ui_runner: Optional[web.AppRunner] = None
        if enable_web:
            from .web import setup_web   # noqa: E402
            self.ui_tls_report = tlsmod.build_ui_context(config.ui, config.tls)
            self.web_app = setup_web(self)

    @property
    def app(self):
        """The OpenADR application (OpenLEADR's)."""
        return self.server.app

    @property
    def openadr_url(self) -> str:
        return self.config.openadr_url

    @property
    def ui_url(self) -> str:
        return self.config.ui_url

    async def start(self):
        if self._started:
            return
        self.messages.install()
        await self.server.run()
        if self.web_app is not None:
            self._ui_runner = web.AppRunner(self.web_app, access_log=logging.getLogger("ahri_vtn.ui.access"))
            await self._ui_runner.setup()
            site = web.TCPSite(self._ui_runner, host=self.config.ui.host, port=self.config.ui.port,
                               ssl_context=self.ui_tls_report.context)
            await site.start()
        self._housekeeping_task = fire_and_forget(self._housekeeping(), "housekeeping")
        self._started = True
        logger.info(f"AHRI 1380 VTN '{self.config.vtn_id}' listening on {self.openadr_url} "
                    f"(tls {self.config.tls.mode}, fingerprint policy {self.config.tls.fingerprint_policy}, "
                    f"signatures {self.config.signatures.mode})")
        if self.tls_report:
            for c in self.tls_report.certificates:
                logger.info(f"VTN certificate {c.key_type} {c.key_bits}: {c.subject}, SAN {', '.join(c.sans) or '-'}, "
                            f"fingerprint {c.fingerprint}, expires {c.not_after:%Y-%m-%d}")
        if self.web_app is not None:
            logger.info(f"Operator UI on {self.ui_url} (client certificates {self.config.ui.client_cert}, "
                        f"password login {'on' if self.config.ui.password_auth else 'off'})")

    async def stop(self):
        if self._housekeeping_task:
            self._housekeeping_task.cancel()
            try:
                await self._housekeeping_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.conformance is not None:
            await self.conformance.shutdown()
        if self._started:
            await self.server.stop()
            if self._ui_runner is not None:
                await self._ui_runner.cleanup()
                self._ui_runner = None
        self.messages.uninstall()
        await self.db.aclose()
        self._started = False

    async def _housekeeping(self):
        while True:
            try:
                await self.db.run(self.events.refresh_event_statuses)
                self.bus.publish("tick")
            except Exception as err:   # noqa: BLE001
                logger.error(f"Housekeeping failed: {err}", exc_info=True)
            await asyncio.sleep(5)

    def run(self):
        """Blocking run (used by the CLI)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.start())
            loop.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            loop.run_until_complete(self.stop())
            loop.close()


@web.middleware
async def _tls_evidence_middleware(request: web.Request, handler):
    """Record the TLS parameters and client certificate of this connection for the message log."""
    current_tls.set(tlsmod.evidence_from_transport(request.transport, request.remote))
    return await handler(request)
