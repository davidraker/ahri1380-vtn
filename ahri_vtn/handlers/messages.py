"""
Raw message capture: every incoming and outgoing OpenADR message is stored (XML + parsed
payload), paired request/response, with schema violations recorded in lenient mode.
Also keeps the per-VEN last-poll bookkeeping used for the online indicator.
"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Optional

from openleadr import hooks
from sqlalchemy import select

from ..db import models
from ..util import jsonable, utcnow
from .reports import current_message_pk

if TYPE_CHECKING:
    from ..server import AhriVtn

logger = logging.getLogger("ahri_vtn.messages")

_current_pair: ContextVar[Optional[str]] = ContextVar("current_pair", default=None)


class MessageLogger:
    def __init__(self, vtn: "AhriVtn"):
        self.vtn = vtn
        self._installed = False

    def install(self):
        if not self._installed:
            hooks.register("message_received", self.on_message_received)
            hooks.register("message_sent", self.on_message_sent)
            hooks.register("schema_violation", self.on_schema_violation)
            self._installed = True

    def uninstall(self):
        if self._installed:
            hooks.unregister("message_received", self.on_message_received)
            hooks.unregister("message_sent", self.on_message_sent)
            hooks.unregister("schema_violation", self.on_schema_violation)
            self._installed = False

    @staticmethod
    def _text(raw) -> Optional[str]:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw)

    async def on_schema_violation(self, raw, error, remote):
        self.vtn.bus.publish("message.schema_violation", error=str(error), remote=remote)

    async def on_message_received(self, message_type, ven_id, raw, payload, remote, schema_error):
        pair_id = uuid.uuid4().hex
        _current_pair.set(pair_id)
        now = utcnow()
        with self.vtn.db.session() as s:
            msg = models.Message(ts=now, direction="in", message_type=message_type, ven_id=ven_id, remote=remote,
                                 schema_error=schema_error, raw_xml=self._text(raw), payload=jsonable(payload),
                                 pair_id=pair_id)
            s.add(msg)
            s.flush()
            message_pk = msg.id
            if ven_id:
                ven = s.scalar(select(models.Ven).where(models.Ven.ven_id == ven_id))
                if ven is not None:
                    ven.last_seen_at = now
                    if message_type == "oadrPoll":
                        ven.last_poll_at = now
                        ven.poll_count = (ven.poll_count or 0) + 1
                    if schema_error:
                        s.add(models.Finding(ven_pk=ven.id, severity="warning", code="schema-violation",
                                             message=f"{message_type} failed XML schema validation",
                                             evidence={"error": schema_error, "message_pk": message_pk}, ts=now))
        current_message_pk.set(message_pk)
        self.vtn.bus.publish("message", direction="in", message_type=message_type, ven_id=ven_id,
                             message_pk=message_pk, schema_error=bool(schema_error))

    async def on_message_sent(self, message_type, ven_id, raw, payload, in_reply_to, http_status):
        pair_id = _current_pair.get()
        now = utcnow()
        with self.vtn.db.session() as s:
            msg = models.Message(ts=now, direction="out", message_type=message_type, ven_id=ven_id,
                                 http_status=http_status, raw_xml=self._text(raw), payload=jsonable(payload),
                                 in_reply_to=in_reply_to, pair_id=pair_id)
            s.add(msg)
            s.flush()
            message_pk = msg.id
        if message_type == "oadrRegisteredReport" and ven_id and payload.get("report_requests"):
            try:
                self.vtn.reports.record_registered_report_requests(ven_id, payload["report_requests"])
            except Exception as err:   # noqa: BLE001
                logger.error(f"Could not record report requests: {err}", exc_info=True)
        self.vtn.bus.publish("message", direction="out", message_type=message_type, ven_id=ven_id,
                             message_pk=message_pk, in_reply_to=in_reply_to, http_status=http_status)
