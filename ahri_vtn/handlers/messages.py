"""
Raw message capture: every incoming and outgoing OpenADR message is stored (XML + parsed
payload), paired request/response, with schema violations recorded in lenient mode and the
transport evidence of the connection (TLS version, cipher suite, client certificate, whether the
message was XML-signed). Also keeps the per-VEN last-poll bookkeeping used for the online
indicator and raises the transport-related conformance findings.
"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Optional

from openleadr import hooks
from sqlalchemy import select

from ..db import models
from ..tls import TlsEvidence
from ..util import jsonable, utcnow
from .reports import current_message_pk

if TYPE_CHECKING:
    from ..server import AhriVtn

logger = logging.getLogger("ahri_vtn.messages")

_current_pair: ContextVar[Optional[str]] = ContextVar("current_pair", default=None)
# Set by the OpenADR listener middleware for the duration of a request
current_tls: ContextVar[Optional[TlsEvidence]] = ContextVar("current_tls", default=None)

SIGNATURE_MARKER = b"X509Certificate"


def message_is_signed(raw) -> Optional[bool]:
    if raw is None:
        return None
    data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
    return SIGNATURE_MARKER in data and b"SignatureValue" in data


class MessageLogger:
    def __init__(self, vtn: "AhriVtn"):
        self.vtn = vtn
        self._installed = False
        self._noted: set = set()        # (ven_pk, finding code, detail) already recorded this process

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

    # ------------------------------------------------------------------ transport findings

    def _vouched(self, ven: models.Ven, tls: Optional[TlsEvidence]) -> bool:
        """
        Whether the connection identifies this VEN: in mutual mode the client certificate must be
        the one pinned to the record (ca-only: any trusted certificate); without client
        certificates (server / plain mode) the venID cannot be verified and is taken as sent.
        """
        if self.vtn.config.tls.mode != "mutual":
            return True
        if self.vtn.config.tls.fingerprint_policy == "ca-only":
            return True
        return bool(tls and tls.fingerprint and ven.fingerprint == tls.fingerprint)

    def _note_once(self, s, ven: models.Ven, code: str, severity: str, message: str, evidence: dict, detail=None):
        """Record a finding once per VEN (and detail) per process, and never twice in the database."""
        key = (ven.id, code, detail)
        if key in self._noted:
            return
        self._noted.add(key)
        existing = s.scalar(select(models.Finding.id).where(models.Finding.ven_pk == ven.id, models.Finding.code == code)
                            .limit(1))
        if existing is not None and detail is None:
            return
        s.add(models.Finding(ven_pk=ven.id, severity=severity, code=code, message=message, evidence=evidence,
                             ts=utcnow()))

    def _transport_findings(self, s, ven: models.Ven, tls: Optional[TlsEvidence], signed: Optional[bool],
                            message_type: str, message_pk: int):
        mode = self.vtn.config.tls.mode
        if tls is None or not tls.secure:
            if mode == "plain":
                self._note_once(s, ven, "transport-plain-http", "info",
                                "Messages are exchanged over plain HTTP (development mode); the VEN is not "
                                "authenticated and OpenADR 2.0b section 9.2 is not met", {"message_pk": message_pk})
            return
        ven.last_tls_version = tls.version
        ven.last_tls_cipher = tls.cipher
        if tls.peer is None and mode == "server":
            self._note_once(s, ven, "tls-no-client-cert", "warning",
                            "The VEN connected without a client certificate (tls.mode = server); OpenADR 2.0b "
                            "9.2.1.1 requires one", {"message_pk": message_pk})
        if tls.version and tls.version not in ("TLSv1.2", "TLSv1.3"):
            self._note_once(s, ven, "tls-version-below-1.2", "warning",
                            f"The VEN negotiated {tls.version}; OpenADR 2.0b rule 67 requires TLS 1.2",
                            {"version": tls.version, "message_pk": message_pk}, detail=tls.version)
        if tls.cipher and tls.cipher_is_openadr_default is False:
            self._note_once(s, ven, "tls-cipher-not-default", "info",
                            f"Negotiated cipher suite {tls.cipher} ({tls.version}) is not one of the two OpenADR 2.0b "
                            "default suites (rule 67); acceptable, recorded for the report",
                            {"cipher": tls.cipher, "version": tls.version, "message_pk": message_pk}, detail=tls.cipher)
        sig_mode = self.vtn.config.signatures.mode
        if signed and sig_mode == "none":
            self._note_once(s, ven, "sig-present", "info",
                            "The VEN signs its messages (OpenADR 2.0b High security) although AHRI Table 4 says "
                            "XML signature use = N; signatures are not verified in signatures.mode = none",
                            {"message_pk": message_pk})
        if signed is False and sig_mode == "optional":
            self._note_once(s, ven, "sig-absent", "info", "The VEN does not sign its messages (signatures.mode = optional)",
                            {"message_pk": message_pk})

    # ------------------------------------------------------------------ hooks

    async def on_message_received(self, message_type, ven_id, raw, payload, remote, schema_error):
        pair_id = uuid.uuid4().hex
        _current_pair.set(pair_id)
        tls = current_tls.get()
        message_pk = await self.vtn.db.run(self._store_received, message_type, ven_id, raw, payload, remote,
                                           schema_error, pair_id, tls)
        current_message_pk.set(message_pk)
        self.vtn.bus.publish("message", direction="in", message_type=message_type, ven_id=ven_id,
                             message_pk=message_pk, schema_error=bool(schema_error),
                             tls_cipher=tls.cipher if tls else None)

    def _store_received(self, message_type, ven_id, raw, payload, remote, schema_error, pair_id, tls) -> int:
        now = utcnow()
        signed = message_is_signed(raw)
        with self.vtn.db.session() as s:
            msg = models.Message(ts=now, direction="in", message_type=message_type, ven_id=ven_id, remote=remote,
                                 schema_error=schema_error, raw_xml=self._text(raw), payload=jsonable(payload),
                                 pair_id=pair_id, signed=signed,
                                 tls_version=tls.version if tls else None, tls_cipher=tls.cipher if tls else None,
                                 peer_fingerprint=tls.fingerprint if tls else None,
                                 peer_subject=tls.peer.subject if tls and tls.peer else None)
            s.add(msg)
            s.flush()
            message_pk = msg.id
            if ven_id:
                ven = s.scalar(select(models.Ven).where(models.Ven.ven_id == ven_id))
                if ven is not None and not self._vouched(ven, tls):
                    # The venID is only a claim at this point (authentication follows and will answer
                    # 463): keep the raw message, but attribute nothing to the VEN record.
                    ven = None
                if ven is not None:
                    ven.last_seen_at = now
                    if remote and ven.last_remote and remote != ven.last_remote:
                        s.add(models.Finding(ven_pk=ven.id, severity="info", code="reg-remote-changed",
                                             message=f"Messages for venID {ven_id} now arrive from {remote} "
                                                     f"(previously {ven.last_remote})",
                                             evidence={"previous": ven.last_remote, "current": remote,
                                                       "message_pk": message_pk}, ts=now))
                    ven.last_remote = remote
                    if message_type == "oadrPoll":
                        ven.last_poll_at = now
                        ven.poll_count = (ven.poll_count or 0) + 1
                    if schema_error:
                        s.add(models.Finding(ven_pk=ven.id, severity="warning", code="schema-violation",
                                             message=f"{message_type} failed XML schema validation",
                                             evidence={"error": schema_error, "message_pk": message_pk}, ts=now))
                    self._transport_findings(s, ven, tls, signed, message_type, message_pk)
        return message_pk

    async def on_message_sent(self, message_type, ven_id, raw, payload, in_reply_to, http_status):
        pair_id = _current_pair.get()
        message_pk = await self.vtn.db.run(self._store_sent, message_type, ven_id, raw, payload, in_reply_to,
                                           http_status, pair_id)
        self.vtn.bus.publish("message", direction="out", message_type=message_type, ven_id=ven_id,
                             message_pk=message_pk, in_reply_to=in_reply_to, http_status=http_status)

    def _store_sent(self, message_type, ven_id, raw, payload, in_reply_to, http_status, pair_id) -> int:
        now = utcnow()
        with self.vtn.db.session() as s:
            msg = models.Message(ts=now, direction="out", message_type=message_type, ven_id=ven_id,
                                 http_status=http_status, raw_xml=self._text(raw), payload=jsonable(payload),
                                 in_reply_to=in_reply_to, pair_id=pair_id, signed=message_is_signed(raw))
            s.add(msg)
            s.flush()
            message_pk = msg.id
        if message_type == "oadrRegisteredReport" and ven_id and payload.get("report_requests"):
            try:
                self.vtn.reports.record_registered_report_requests(ven_id, payload["report_requests"])
            except Exception as err:   # noqa: BLE001
                logger.error(f"Could not record report requests: {err}", exc_info=True)
        return message_pk
