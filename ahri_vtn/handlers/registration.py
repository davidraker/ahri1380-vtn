"""
VEN registration: oadrQueryRegistration, oadrCreatePartyRegistration, oadrCancelPartyRegistration,
the ven_lookup used by OpenLEADR, and the client-certificate identity model (OpenADR 2.0b 9.2.1.1
and 10.5.1): fingerprints are pinned per VEN and never silently replaced.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from openleadr import errors as oadr_errors
from openleadr import utils as oadr_utils
from sqlalchemy import desc, func, select

from .. import spec
from ..db import models
from ..tls import TlsEvidence, normalize_fingerprint
from ..util import jsonable, utcnow
from .messages import current_tls

if TYPE_CHECKING:
    from ..server import AhriVtn

logger = logging.getLogger("ahri_vtn.registration")


class RegistrationRefused(oadr_errors.NotRegisteredOrAuthorizedError):
    """463 Not registered or authorized, raised from the registration handler."""


class RegistrationHandler:
    def __init__(self, vtn: "AhriVtn"):
        self.vtn = vtn

    # ------------------------------------------------------------------ VEN management (UI)

    def add_ven(self, ven_name: str, ven_id: Optional[str] = None, notes: Optional[str] = None,
                enabled: bool = True, fingerprint: Optional[str] = None) -> dict:
        if not ven_name:
            raise ValueError("A VEN name is required")
        fp = normalize_fingerprint(fingerprint) if fingerprint else None
        with self.vtn.db.session() as s:
            existing = s.scalar(select(models.Ven).where(models.Ven.ven_name == ven_name))
            if existing:
                raise ValueError(f"A VEN named '{ven_name}' already exists")
            if fp and s.scalar(select(models.Ven).where(models.Ven.fingerprint == fp)):
                raise ValueError(f"Fingerprint {fp} is already pinned to another VEN")
            ven = models.Ven(ven_name=ven_name, ven_id=ven_id or None, notes=notes, enabled=enabled,
                             fingerprint=fp, fingerprint_source="operator" if fp else None,
                             fingerprint_set_at=utcnow() if fp else None)
            s.add(ven)
            s.flush()
            if fp:
                s.add(models.RegistrationEvent(ven_pk=ven.id, kind="pin", accepted=True,
                                               details={"fingerprint": fp, "by": "operator"}))
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

    def pin_fingerprint(self, ven_pk: int, fingerprint: str, source: str = "operator",
                        details: Optional[dict] = None) -> Optional[dict]:
        """Pin (or re-pin) the certificate fingerprint of a VEN. An operator action."""
        fp = normalize_fingerprint(fingerprint)
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, ven_pk)
            if ven is None:
                return None
            other = s.scalar(select(models.Ven).where(models.Ven.fingerprint == fp, models.Ven.id != ven_pk))
            if other is not None:
                raise ValueError(f"Fingerprint {fp} is already pinned to VEN '{other.ven_name}'")
            previous = ven.fingerprint
            ven.fingerprint = fp
            ven.fingerprint_source = source
            ven.fingerprint_set_at = utcnow()
            if details:
                self._apply_cert_details(ven, details)
            else:
                for col in ("fingerprint_sha256", "cert_subject", "cert_issuer", "cert_not_before", "cert_not_after",
                            "cert_key_type", "cert_key_bits"):
                    setattr(ven, col, None)
            s.add(models.RegistrationEvent(ven_pk=ven.id, kind="pin", accepted=True,
                                           details={"fingerprint": fp, "previous": previous, "by": source}))
            for rej in s.scalars(select(models.RejectedRegistration)
                                 .where(models.RejectedRegistration.fingerprint == fp,
                                        models.RejectedRegistration.resolved.is_(False))):
                rej.resolved = True
            result = ven.as_dict()
        self.vtn.bus.publish("ven.updated", ven_pk=ven_pk)
        return result

    def clear_fingerprint(self, ven_pk: int) -> Optional[dict]:
        """Forget the pinned certificate so a re-keyed device can register again (operator action)."""
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, ven_pk)
            if ven is None:
                return None
            previous = ven.fingerprint
            for col in ("fingerprint", "fingerprint_sha256", "fingerprint_source", "fingerprint_set_at",
                        "cert_subject", "cert_issuer", "cert_not_before", "cert_not_after", "cert_key_type",
                        "cert_key_bits"):
                setattr(ven, col, None)
            s.add(models.RegistrationEvent(ven_pk=ven.id, kind="clear", accepted=True,
                                           details={"previous": previous, "by": "operator"}))
            result = ven.as_dict()
        self.vtn.bus.publish("ven.updated", ven_pk=ven_pk)
        return result

    def list_rejected(self, limit: int = 20, include_resolved: bool = False) -> list[dict]:
        with self.vtn.db.session() as s:
            q = select(models.RejectedRegistration).order_by(desc(models.RejectedRegistration.ts)).limit(limit)
            if not include_resolved:
                q = q.where(models.RejectedRegistration.resolved.is_(False))
            return [r.as_dict() for r in s.scalars(q)]

    def dismiss_rejected(self, rejected_pk: int) -> bool:
        with self.vtn.db.session() as s:
            rej = s.get(models.RejectedRegistration, rejected_pk)
            if rej is None:
                return False
            rej.resolved = True
        return True

    # ------------------------------------------------------------------ OpenLEADR handlers

    async def ven_lookup(self, ven_id: str) -> Optional[dict]:
        """Used by OpenLEADR: unknown / unregistered VENs are asked to re-register."""
        return await self.vtn.db.run(self._ven_lookup, ven_id)

    def _ven_lookup(self, ven_id: str) -> Optional[dict]:
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

    # ------------------------------------------------------------------ certificate identity

    @staticmethod
    def _cert_details(tls: Optional[TlsEvidence]) -> Optional[dict]:
        if tls is None or tls.peer is None:
            return None
        p = tls.peer
        return {"fingerprint": p.fingerprint, "fingerprint_sha256": p.sha256, "cert_subject": p.subject,
                "cert_issuer": p.issuer, "cert_not_before": p.not_before, "cert_not_after": p.not_after,
                "cert_key_type": p.key_type, "cert_key_bits": p.key_bits}

    @staticmethod
    def _apply_cert_details(ven: models.Ven, details: dict):
        for col in ("fingerprint_sha256", "cert_subject", "cert_issuer", "cert_not_before", "cert_not_after",
                    "cert_key_type", "cert_key_bits"):
            if col in details:
                setattr(ven, col, details[col])

    def _refuse(self, s, *, reason: str, ven: Optional[models.Ven], ven_name, received_ven_id, tls: Optional[TlsEvidence],
                remote, message: str, details: dict):
        """
        Record the refusal and raise a 463. The records go through their own session: raising
        rolls the registration transaction back (nothing of the refused VEN must persist), but the
        evidence must survive.
        """
        fp = tls.fingerprint if tls else None
        ven_pk = ven.id if ven is not None else None     # only persisted records reach a refusal
        code = "reg-unknown-ven" if reason == "unknown-ven" else f"cert-{reason}"
        with self.vtn.db.session() as s2:
            s2.add(models.RejectedRegistration(ven_pk=ven_pk, ven_name=ven_name, ven_id=received_ven_id,
                                               reason=reason, fingerprint=fp,
                                               cert_subject=tls.peer.subject if tls and tls.peer else None,
                                               cert_issuer=tls.peer.issuer if tls and tls.peer else None, remote=remote))
            s2.add(models.Finding(ven_pk=ven_pk, severity="warning", code=code, message=message,
                                  evidence={**details, "fingerprint": fp, "remote": remote}, ts=utcnow()))
            if ven_pk is not None:
                s2.add(models.RegistrationEvent(ven_pk=ven_pk, kind="create", accepted=False,
                                                details={**details, "reason": reason, "fingerprint": fp}))
        logger.warning(f"Registration refused ({reason}): VEN '{ven_name}' venID {received_ven_id} fingerprint {fp} "
                       f"from {remote}")
        self.vtn.bus.publish("registration.rejected", ven_name=ven_name, reason=reason, fingerprint=fp)
        raise RegistrationRefused(message)

    def _apply_fingerprint_policy(self, s, ven: Optional[models.Ven], tls: Optional[TlsEvidence], ven_name,
                                  received_ven_id, remote, details: dict, deviations: list) -> Optional[models.Ven]:
        """
        Enforce tls.fingerprint_policy for a registration arriving over mutual TLS. Returns the
        VEN record to use (possibly newly created), or raises RegistrationRefused (463).
        `deviations` receives the certificate findings (they are recorded but, unlike Table 4
        deviations, do not affect the reg-table4 pass finding).
        """
        policy = self.vtn.config.tls.fingerprint_policy
        fp = tls.fingerprint
        cert = self._cert_details(tls)
        for code, severity, message in tls.peer.problems(self.vtn.config.tls.expiry_warning_days):
            deviations.append(spec.Deviation(severity, code, message, {"fingerprint": fp}))

        # A fingerprint pinned to another VEN record identifies that VEN, whatever name is claimed
        owner = s.scalar(select(models.Ven).where(models.Ven.fingerprint == fp))
        if owner is not None and ven is not None and owner.id != ven.id:
            if policy == "ca-only":
                deviations.append(spec.Deviation("warning", "cert-fingerprint-reused",
                                                 f"Certificate {fp} is pinned to VEN '{owner.ven_name}' but registered "
                                                 f"as '{ven_name}'", {"fingerprint": fp, "owner": owner.ven_name}))
            else:
                self._refuse(s, reason="fingerprint-owned-by-other-ven", ven=ven, ven_name=ven_name,
                             received_ven_id=received_ven_id, tls=tls, remote=remote,
                             message=f"Certificate fingerprint {fp} belongs to VEN '{owner.ven_name}'", details=details)
        if owner is not None and ven is None:
            ven = owner      # known certificate, new or changed venName: same device

        if policy == "pinned":
            if ven is None:
                self._refuse(s, reason="unknown-fingerprint", ven=None, ven_name=ven_name, received_ven_id=received_ven_id,
                             tls=tls, remote=remote, details=details,
                             message=f"Certificate fingerprint {fp} is not pinned to any VEN (fingerprint_policy = pinned)")
            if not ven.fingerprint:
                self._refuse(s, reason="unknown-fingerprint", ven=ven, ven_name=ven_name, received_ven_id=received_ven_id,
                             tls=tls, remote=remote, details=details,
                             message=f"VEN '{ven.ven_name}' has no pinned fingerprint; pin {fp} first "
                                     "(fingerprint_policy = pinned)")
            if ven.fingerprint != fp:
                self._refuse(s, reason="fingerprint-mismatch", ven=ven, ven_name=ven_name, received_ven_id=received_ven_id,
                             tls=tls, remote=remote, details=details,
                             message=f"Certificate fingerprint {fp} does not match the fingerprint pinned to VEN "
                                     f"'{ven.ven_name}' ({ven.fingerprint})")
            self._apply_cert_details(ven, cert)
            return ven

        if policy == "first-use":
            if ven is None:
                ven = self._auto_create(s, ven_name, ven_name_known=False)
                if ven is None:
                    self._refuse(s, reason="unknown-ven", ven=None, ven_name=ven_name, received_ven_id=received_ven_id,
                                 tls=tls, remote=remote, details=details,
                                 message=f"Unknown VEN '{ven_name}' and auto-accept is off or at its limit")
            if not ven.fingerprint:
                ven.fingerprint = fp
                ven.fingerprint_source = "first-use"
                ven.fingerprint_set_at = utcnow()
                self._apply_cert_details(ven, cert)
                s.add(models.RegistrationEvent(ven_pk=ven.id, kind="pin", accepted=True,
                                               details={"fingerprint": fp, "by": "first-use"}))
                deviations.append(spec.Deviation("info", "cert-fingerprint-pinned-on-first-use",
                                                 f"Certificate fingerprint {fp} pinned to VEN '{ven.ven_name}' on first contact",
                                                 {"fingerprint": fp}))
            elif ven.fingerprint != fp:
                self._refuse(s, reason="fingerprint-mismatch", ven=ven, ven_name=ven_name, received_ven_id=received_ven_id,
                             tls=tls, remote=remote, details=details,
                             message=f"Certificate fingerprint {fp} does not match the fingerprint pinned to VEN "
                                     f"'{ven.ven_name}' ({ven.fingerprint})")
            return ven

        # ca-only: any trusted certificate; record what was seen
        if ven is None:
            ven = self._auto_create(s, ven_name, ven_name_known=False)
            if ven is None:
                self._refuse(s, reason="unknown-ven", ven=None, ven_name=ven_name, received_ven_id=received_ven_id,
                             tls=tls, remote=remote, details=details,
                             message=f"Unknown VEN '{ven_name}' and auto-accept is off or at its limit")
        if ven.fingerprint and ven.fingerprint != fp:
            deviations.append(spec.Deviation("info", "cert-fingerprint-changed",
                                             f"VEN '{ven.ven_name}' now uses certificate {fp} (previously {ven.fingerprint})",
                                             {"previous": ven.fingerprint, "current": fp}))
        if ven.fingerprint != fp:
            ven.fingerprint = fp
            ven.fingerprint_source = "ca-only"
            ven.fingerprint_set_at = utcnow()
        self._apply_cert_details(ven, cert)
        return ven

    def _auto_create(self, s, ven_name, ven_name_known: bool) -> Optional[models.Ven]:
        """Create a VEN record for an unknown name if auto-accept allows it."""
        cfg = self.vtn.config
        if not cfg.auto_accept_unknown_vens:
            return None
        count = s.scalar(select(func.count(models.Ven.id)))
        if count >= cfg.limits.max_vens_auto_accept:
            logger.warning(f"Auto-accept limit of {cfg.limits.max_vens_auto_accept} VENs reached; refusing '{ven_name}'")
            return None
        ven = models.Ven(ven_name=ven_name or f"ven-{oadr_utils.generate_id()[:8]}")
        s.add(ven)
        s.flush()
        logger.info(f"Auto-accepting new VEN '{ven.ven_name}'")
        return ven

    # ------------------------------------------------------------------ registration

    async def on_create_party_registration(self, registration_info):
        return await self.vtn.db.run(self._create_party_registration, registration_info)

    def _create_party_registration(self, registration_info):
        ven_name = registration_info.get("ven_name")
        received_ven_id = registration_info.get("ven_id")
        received_registration_id = registration_info.get("registration_id")
        deviations = spec.validate_registration(registration_info)
        cert_notes: list = []
        details = jsonable({k: v for k, v in registration_info.items() if k != "fingerprint"})
        now = utcnow()
        tls = current_tls.get()
        remote = None
        mutual = tls is not None and tls.secure and tls.peer is not None

        with self.vtn.db.session() as s:
            ven = None
            if received_ven_id:
                ven = s.scalar(select(models.Ven).where(models.Ven.ven_id == received_ven_id))
            if ven is None and ven_name:
                ven = s.scalar(select(models.Ven).where(models.Ven.ven_name == ven_name))

            if mutual:
                ven = self._apply_fingerprint_policy(s, ven, tls, ven_name, received_ven_id, remote, details, cert_notes)
            elif ven is None:
                ven = self._auto_create(s, ven_name, ven_name_known=False)
                if ven is None:
                    logger.warning(f"Rejecting registration from unknown VEN '{ven_name}' (venID {received_ven_id})")
                    s.add(models.Finding(severity="warning", code="reg-unknown-ven",
                                         message=f"Rejected registration from unknown VEN name '{ven_name}'",
                                         evidence=details))
                    self.vtn.bus.publish("registration.rejected", ven_name=ven_name, reason="unknown")
                    return False
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
            if ven_name and ven.ven_name != ven_name and mutual:
                cert_notes.append(spec.Deviation("info", "reg-ven-name-changed",
                                                 f"Certificate {tls.fingerprint} registered as '{ven_name}', record is "
                                                 f"'{ven.ven_name}'", {"received": ven_name, "record": ven.ven_name}))
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
            if tls is not None and tls.secure:
                ven.last_tls_version, ven.last_tls_cipher = tls.version, tls.cipher
            ven.registered = True
            ven.first_registered_at = ven.first_registered_at or now
            ven.last_registered_at = now
            ven.last_seen_at = now
            s.add(models.RegistrationEvent(ven_pk=ven.id, kind=kind, accepted=True, details=details, ts=now))
            for d in deviations + cert_notes:
                s.add(models.Finding(ven_pk=ven.id, severity=d.severity, code=d.code, message=d.message,
                                     evidence=jsonable(d.evidence), ts=now))
            if not deviations:
                s.add(models.Finding(ven_pk=ven.id, severity="pass", code="reg-table4",
                                     message="Registration conforms to Table 4 (2.0b, simpleHttp, pull, no signature)",
                                     ts=now))
            ven_id, registration_id, ven_pk, record_name = ven.ven_id, ven.registration_id, ven.id, ven.ven_name

        self.vtn.bus.publish("ven.registered", ven_pk=ven_pk, ven_id=ven_id, ven_name=record_name, kind=kind)
        logger.info(f"VEN '{record_name}' registered ({kind}) as venID {ven_id}")
        return ven_id, registration_id

    async def on_cancel_party_registration(self, payload):
        return await self.vtn.db.run(self._cancel_party_registration, payload)

    def _cancel_party_registration(self, payload):
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
