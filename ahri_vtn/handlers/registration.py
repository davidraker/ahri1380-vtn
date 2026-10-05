"""
VEN registration: oadrQueryRegistration, oadrCreatePartyRegistration, oadrCancelPartyRegistration
and the ven_lookup used by OpenLEADR to decide whether a VEN must re-register.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from openleadr import utils as oadr_utils
from sqlalchemy import select

from .. import spec
from ..db import models
from ..util import jsonable, utcnow

if TYPE_CHECKING:
    from ..server import AhriVtn

logger = logging.getLogger("ahri_vtn.registration")


class RegistrationHandler:
    def __init__(self, vtn: "AhriVtn"):
        self.vtn = vtn

    # ------------------------------------------------------------------ VEN management (UI)

    def add_ven(self, ven_name: str, ven_id: Optional[str] = None, notes: Optional[str] = None,
                enabled: bool = True) -> dict:
        with self.vtn.db.session() as s:
            existing = s.scalar(select(models.Ven).where(models.Ven.ven_name == ven_name))
            if existing:
                raise ValueError(f"A VEN named '{ven_name}' already exists")
            ven = models.Ven(ven_name=ven_name, ven_id=ven_id or None, notes=notes, enabled=enabled)
            s.add(ven)
            s.flush()
            result = ven.as_dict()
        self.vtn.bus.publish("ven.added", ven_name=ven_name)
        return result

    def update_ven(self, ven_pk: int, **fields) -> Optional[dict]:
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, ven_pk)
            if not ven:
                return None
            for key in ("enabled", "notes", "ven_name", "ven_id"):
                if key in fields and fields[key] is not None:
                    setattr(ven, key, fields[key])
            result = ven.as_dict()
        self.vtn.bus.publish("ven.updated", ven_pk=ven_pk)
        return result

    def delete_ven(self, ven_pk: int) -> bool:
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, ven_pk)
            if not ven:
                return False
            s.delete(ven)
        self.vtn.bus.publish("ven.deleted", ven_pk=ven_pk)
        return True

    # ------------------------------------------------------------------ OpenLEADR handlers

    def ven_lookup(self, ven_id: str) -> Optional[dict]:
        """Used by OpenLEADR: unknown / unregistered VENs are asked to re-register."""
        with self.vtn.db.session() as s:
            ven = s.scalar(select(models.Ven).where(models.Ven.ven_id == ven_id))
            if ven is None or not ven.registered or not ven.enabled:
                return None
            return {"ven_id": ven.ven_id, "ven_name": ven.ven_name,
                    "fingerprint": ven.fingerprint, "registration_id": ven.registration_id}

    async def on_query_registration(self, payload):
        self.vtn.bus.publish("registration.query", request_id=payload.get("request_id"))
        reg_service = self.vtn.server.services["registration_service"]
        return "oadrCreatedPartyRegistration", {
            "request_id": payload.get("request_id"),
            "profiles": [{"profile_name": "2.0b", "transports": [{"transport_name": "simpleHttp"}]}],
            "requested_oadr_poll_freq": reg_service.poll_freq,
        }

    async def on_create_party_registration(self, registration_info):
        ven_name = registration_info.get("ven_name")
        received_ven_id = registration_info.get("ven_id")
        received_registration_id = registration_info.get("registration_id")
        deviations = spec.validate_registration(registration_info)
        details = jsonable(registration_info)
        now = utcnow()

        with self.vtn.db.session() as s:
            ven = None
            if ven_name:
                ven = s.scalar(select(models.Ven).where(models.Ven.ven_name == ven_name))
            if ven is None and received_ven_id:
                ven = s.scalar(select(models.Ven).where(models.Ven.ven_id == received_ven_id))
            if ven is None:
                if not self.vtn.config.auto_accept_unknown_vens:
                    logger.warning(f"Rejecting registration from unknown VEN '{ven_name}' (venID {received_ven_id})")
                    s.add(models.Finding(severity="warning", code="reg-unknown-ven",
                                         message=f"Rejected registration from unknown VEN name '{ven_name}'",
                                         evidence=details))
                    self.vtn.bus.publish("registration.rejected", ven_name=ven_name, reason="unknown")
                    return False
                ven = models.Ven(ven_name=ven_name or f"ven-{oadr_utils.generate_id()[:8]}")
                s.add(ven)
                s.flush()
                logger.info(f"Auto-accepting new VEN '{ven.ven_name}'")
            if not ven.enabled:
                s.add(models.RegistrationEvent(ven_pk=ven.id, kind="create", accepted=False, details=details, ts=now))
                self.vtn.bus.publish("registration.rejected", ven_name=ven.ven_name, reason="disabled")
                return False

            kind = "reregister" if ven.registered else "create"
            if received_ven_id and ven.ven_id and received_ven_id != ven.ven_id:
                deviations.append(spec.Deviation("warning", "reg-ven-id-mismatch",
                                                 f"VEN supplied venID '{received_ven_id}' but was assigned '{ven.ven_id}'",
                                                 {"received": received_ven_id, "assigned": ven.ven_id}))
            if received_ven_id and not ven.registered and kind == "create":
                deviations.append(spec.Deviation("info", "reg-ven-id-on-first-registration",
                                                 "VEN supplied a venID on its first registration (Table 4: only on re-registration)",
                                                 {"received": received_ven_id}))
            if received_registration_id and ven.registration_id and received_registration_id != ven.registration_id:
                deviations.append(spec.Deviation("warning", "reg-registration-id-mismatch",
                                                 f"VEN supplied registrationID '{received_registration_id}' but the VTN "
                                                 f"issued '{ven.registration_id}'",
                                                 {"received": received_registration_id, "issued": ven.registration_id}))
            if not ven.ven_id:
                ven.ven_id = received_ven_id or oadr_utils.generate_id()
            if not ven.registration_id:
                ven.registration_id = oadr_utils.generate_id()
            ven.profile_name = registration_info.get("profile_name")
            ven.transport_name = registration_info.get("transport_name")
            ven.transport_address = registration_info.get("transport_address")
            ven.http_pull_model = registration_info.get("http_pull_model")
            ven.xml_signature = registration_info.get("xml_signature")
            ven.report_only = registration_info.get("report_only")
            ven.fingerprint = registration_info.get("fingerprint") or ven.fingerprint
            ven.registered = True
            ven.first_registered_at = ven.first_registered_at or now
            ven.last_registered_at = now
            ven.last_seen_at = now
            s.add(models.RegistrationEvent(ven_pk=ven.id, kind=kind, accepted=True, details=details, ts=now))
            for d in deviations:
                s.add(models.Finding(ven_pk=ven.id, severity=d.severity, code=d.code, message=d.message,
                                     evidence=jsonable(d.evidence), ts=now))
            if not deviations:
                s.add(models.Finding(ven_pk=ven.id, severity="pass", code="reg-table4",
                                     message="Registration conforms to Table 4 (2.0b, simpleHttp, pull, no signature)",
                                     ts=now))
            ven_id, registration_id, ven_pk = ven.ven_id, ven.registration_id, ven.id

        self.vtn.bus.publish("ven.registered", ven_pk=ven_pk, ven_id=ven_id, ven_name=ven_name, kind=kind)
        logger.info(f"VEN '{ven_name}' registered ({kind}) as venID {ven_id}")
        return ven_id, registration_id

    async def on_cancel_party_registration(self, payload):
        ven_id = payload.get("ven_id")
        registration_id = payload.get("registration_id")
        with self.vtn.db.session() as s:
            ven = s.scalar(select(models.Ven).where(models.Ven.ven_id == ven_id))
            if ven is not None:
                ven.registered = False
                s.add(models.RegistrationEvent(ven_pk=ven.id, kind="cancel", accepted=True, details=jsonable(payload)))
                ven_pk = ven.id
            else:
                ven_pk = None
        self.vtn.bus.publish("ven.cancelled", ven_pk=ven_pk, ven_id=ven_id)
        return "oadrCanceledPartyRegistration", {"registration_id": registration_id, "ven_id": ven_id}

    def request_reregistration(self, ven_pk: int) -> bool:
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, ven_pk)
            if ven is None or not ven.ven_id:
                return False
            s.add(models.RegistrationEvent(ven_pk=ven.id, kind="request_reregistration", accepted=None))
            ven_id = ven.ven_id
        self.vtn.server.request_reregistration(ven_id)
        self.vtn.bus.publish("registration.requested", ven_pk=ven_pk, ven_id=ven_id)
        return True
