"""
Assembly of the AHRI 1380 VTN: OpenLEADR server + handlers + persistence + web UI.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from openleadr import OpenADRServer

from .bus import LiveBus
from .config import Config
from .db import Database
from .handlers import EventManager, MessageLogger, RegistrationHandler, ReportHandler
from .queries import Queries
from .util import fire_and_forget

logger = logging.getLogger("ahri_vtn")


class AhriVtn:
    def __init__(self, config: Config, db: Optional[Database] = None, enable_web: bool = True):
        self.config = config
        self.db = db or Database(config.db_url)
        self.db.create_all()
        self.bus = LiveBus()

        self.registry = RegistrationHandler(self)
        self.reports = ReportHandler(self)
        self.events = EventManager(self)
        self.messages = MessageLogger(self)
        self.queries = Queries(self)

        tls = config.tls
        self.server = OpenADRServer(
            vtn_id=config.vtn_id, http_host=config.host, http_port=config.port,
            http_path_prefix=config.http_path_prefix,
            requested_poll_freq=timedelta(seconds=config.requested_poll_freq_seconds),
            ven_lookup=self.registry.ven_lookup, validate_xml=config.schema_mode,
            http_cert=tls.http_cert, http_key=tls.http_key, http_ca_file=tls.http_ca_file,
            http_key_passphrase=tls.http_key_passphrase,
            cert=tls.signing_cert, key=tls.signing_key, passphrase=tls.signing_passphrase,
            show_fingerprint=False)

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

        if enable_web:
            from .web import setup_web   # noqa: E402
            setup_web(self)

    @property
    def app(self):
        return self.server.app

    @property
    def openadr_url(self) -> str:
        return self.config.openadr_url

    async def start(self):
        if self._started:
            return
        self.messages.install()
        await self.server.run()
        self._housekeeping_task = fire_and_forget(self._housekeeping(), "housekeeping")
        self._started = True
        logger.info(f"AHRI 1380 VTN '{self.config.vtn_id}' listening on {self.openadr_url} "
                    f"(UI: http://{self.config.host}:{self.config.port}/ui/)")

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
        self.messages.uninstall()
        self.db.dispose()
        self._started = False

    async def _housekeeping(self):
        while True:
            try:
                self.events.refresh_event_statuses()
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
