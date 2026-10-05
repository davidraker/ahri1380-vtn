"""
Report handling: registration of the AHRI metadata reports, automatic report requests,
storage of every delivered sample and bookkeeping of VTN-initiated report requests.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

from openleadr import utils as oadr_utils
from sqlalchemy import select

from .. import spec
from ..db import models
from ..util import jsonable, seconds, to_float, utcnow

if TYPE_CHECKING:
    from ..server import AhriVtn

logger = logging.getLogger("ahri_vtn.reports")

# Set by MessageLogger for the duration of an incoming request so samples can link to the raw message
current_message_pk: ContextVar[Optional[int]] = ContextVar("current_message_pk", default=None)


class ReportHandler:
    def __init__(self, vtn: "AhriVtn"):
        self.vtn = vtn

    # ------------------------------------------------------------------ helpers

    def _ven_by_ven_id(self, s, ven_id: str) -> Optional[models.Ven]:
        return s.scalar(select(models.Ven).where(models.Ven.ven_id == ven_id))

    @staticmethod
    def _choose_interval(rspec: spec.ReportSpec, descriptions: list[dict], target: timedelta) -> timedelta:
        """Clamp the desired sampling interval into what the VEN offered (one interval per report)."""
        chosen = target
        for rd in descriptions:
            sampling = rd.get("sampling_rate") or {}
            min_period, max_period = sampling.get("min_period"), sampling.get("max_period")
            if isinstance(min_period, timedelta) and chosen < min_period:
                chosen = min_period
            if isinstance(max_period, timedelta) and max_period > timedelta(0) and chosen > max_period:
                chosen = max_period
        return chosen

    async def _noop(self, values):
        """Per-rID callback required by OpenLEADR; all storage happens in on_update_report."""
        return None

    # ------------------------------------------------------------------ OpenLEADR handlers

    async def on_register_report(self, report: dict):
        ven_id = report.get("ven_id")
        raw_name = report.get("report_name")
        base_name = oadr_utils.strip_metadata_prefix(raw_name)
        specifier_id = report.get("report_specifier_id")
        rspec = spec.identify_report(raw_name, specifier_id)
        descriptions = report.get("report_descriptions") or []
        now = utcnow()

        with self.vtn.db.session() as s:
            ven = self._ven_by_ven_id(s, ven_id)
            if ven is None:
                # Registration must have happened, but tolerate it (e.g. after a database reset)
                ven = models.Ven(ven_name=f"unknown-{ven_id}", ven_id=ven_id, registered=True)
                s.add(ven)
                s.flush()
            for old in s.scalars(select(models.RegisteredReport)
                                 .where(models.RegisteredReport.ven_pk == ven.id,
                                        models.RegisteredReport.report_specifier_id == specifier_id,
                                        models.RegisteredReport.active.is_(True))):
                old.active = False
            row = models.RegisteredReport(ven_pk=ven.id, report_name_raw=raw_name, report_name=base_name,
                                          spec_name=rspec.name if rspec else None,
                                          report_specifier_id=specifier_id,
                                          duration_seconds=seconds(report.get("duration")),
                                          created_date_time=report.get("created_date_time"),
                                          registered_at=now, raw=jsonable(report))
            s.add(row)
            s.flush()
            for rd in descriptions:
                measurement = rd.get("measurement") or {}
                sampling = rd.get("sampling_rate") or {}
                rid = rspec.rid(rd.get("r_id")) if rspec else None
                s.add(models.ReportDescription(
                    report_pk=row.id, r_id=str(rd.get("r_id")), r_key=spec.normalize_key(rd.get("r_id")),
                    spec_rid=rid.name if rid else None,
                    report_type=rd.get("report_type"), reading_type=rd.get("reading_type"),
                    measurement_name=measurement.get("name"), measurement_description=measurement.get("description"),
                    measurement_unit=measurement.get("unit"), measurement_scale=measurement.get("scale"),
                    min_period_seconds=seconds(sampling.get("min_period")),
                    max_period_seconds=seconds(sampling.get("max_period")),
                    on_change=sampling.get("on_change"),
                    resource_id=(rd.get("report_data_source") or {}).get("resource_id"),
                    market_context=rd.get("market_context")))
            if rspec is not None:
                deviations = spec.validate_registered_report(rspec, report)
                for d in deviations:
                    s.add(models.Finding(ven_pk=ven.id, severity=d.severity, code=d.code, message=d.message,
                                         evidence=jsonable({**d.evidence, "report": rspec.name}), ts=now))
                if not any(d.severity == "fail" for d in deviations):
                    s.add(models.Finding(ven_pk=ven.id, severity="pass", code="report-registered",
                                         message=f"{rspec.name} registered with all required rIDs",
                                         evidence={"report_specifier_id": specifier_id}, ts=now))
            else:
                s.add(models.Finding(ven_pk=ven.id, severity="info", code="report-unknown",
                                     message=f"VEN registered a report '{raw_name}' ({specifier_id}) that is not part of AHRI 1380",
                                     evidence={"report_name": raw_name, "report_specifier_id": specifier_id}, ts=now))
            ven.last_seen_at = now
            ven_pk = ven.id

        self.vtn.bus.publish("report.registered", ven_pk=ven_pk, ven_id=ven_id, report_name=base_name,
                             spec_name=rspec.name if rspec else None, report_specifier_id=specifier_id)
        logger.info(f"VEN {ven_id} registered report '{raw_name}' ({specifier_id}) -> {rspec.name if rspec else 'unknown'}")

        if rspec is None or not self.vtn.config.auto_request_reports or not descriptions:
            return None

        # Request the report immediately (inside the oadrRegisteredReport response)
        if rspec is spec.STATUS_REPORT:
            target = timedelta(seconds=self.vtn.config.status_report_interval_seconds)
        else:
            max_periods = [((rd.get("sampling_rate") or {}).get("max_period")) for rd in descriptions]
            max_periods = [mp for mp in max_periods if isinstance(mp, timedelta) and mp > timedelta(0)]
            target = min(max_periods) if max_periods else timedelta(hours=1)
        interval = self._choose_interval(rspec, descriptions, target)
        return [(rd["r_id"], self._noop, interval) for rd in descriptions if rd.get("r_id") is not None]

    def record_registered_report_requests(self, ven_id: str, report_requests: list):
        """Called (via the message_sent hook) when oadrRegisteredReport carries report requests."""
        if not report_requests:
            return
        with self.vtn.db.session() as s:
            ven = self._ven_by_ven_id(s, ven_id)
            if ven is None:
                return
            for rr in report_requests:
                rrid = oadr_utils.getmember(rr, "report_request_id")
                if s.scalar(select(models.ReportRequest).where(models.ReportRequest.report_request_id == rrid)):
                    continue
                specifier = oadr_utils.getmember(rr, "report_specifier")
                specifier_id = oadr_utils.getmember(specifier, "report_specifier_id")
                rspec = spec.identify_report(None, specifier_id)
                registered = s.scalar(select(models.RegisteredReport)
                                      .where(models.RegisteredReport.ven_pk == ven.id,
                                             models.RegisteredReport.report_specifier_id == specifier_id,
                                             models.RegisteredReport.active.is_(True)))
                spec_name = registered.spec_name if registered and registered.spec_name else (rspec.name if rspec else None)
                payloads = oadr_utils.getmember(specifier, "specifier_payloads", missing=[]) or []
                s.add(models.ReportRequest(
                    report_request_id=rrid, ven_pk=ven.id, report_specifier_id=specifier_id, spec_name=spec_name,
                    r_ids=[oadr_utils.getmember(p, "r_id") for p in payloads],
                    granularity_seconds=seconds(oadr_utils.getmember(specifier, "granularity", missing=None)),
                    report_back_duration_seconds=seconds(oadr_utils.getmember(specifier, "report_back_duration", missing=None)),
                    one_shot=False, status="queued", source="auto"))
            ven_pk = ven.id
        self.vtn.bus.publish("report.requested", ven_pk=ven_pk, ven_id=ven_id, source="auto")

    async def on_created_report(self, payload):
        ven_id = payload.get("ven_id")
        pending = [p.get("report_request_id") for p in (payload.get("pending_reports") or [])]
        now = utcnow()
        with self.vtn.db.session() as s:
            for rrid in pending:
                rr = s.scalar(select(models.ReportRequest).where(models.ReportRequest.report_request_id == str(rrid)))
                if rr is not None and rr.status in ("queued",):
                    rr.status = "created"
                    rr.confirmed_at = now
        self.vtn.bus.publish("report.created", ven_id=ven_id, pending=pending)

    async def on_canceled_report(self, payload):
        ven_id = payload.get("ven_id")
        still_pending = {str(p.get("report_request_id")) for p in (payload.get("pending_reports") or [])}
        now = utcnow()
        with self.vtn.db.session() as s:
            ven = self._ven_by_ven_id(s, ven_id)
            if ven is not None:
                for rr in s.scalars(select(models.ReportRequest)
                                    .where(models.ReportRequest.ven_pk == ven.id,
                                           models.ReportRequest.status.in_(("cancel_requested", "queued", "created", "active")))):
                    if rr.report_request_id not in still_pending and rr.status == "cancel_requested":
                        rr.status = "cancelled"
                        rr.cancelled_at = now
        self.vtn.bus.publish("report.cancelled", ven_id=ven_id, pending=sorted(still_pending))

    async def on_update_report(self, report: dict):
        ven_id = report.get("ven_id")
        rrid = report.get("report_request_id")
        rrid = str(rrid) if rrid is not None else None
        specifier_id = report.get("report_specifier_id")
        now = utcnow()
        intervals = report.get("intervals") or []
        message_pk = current_message_pk.get()
        values_summary: dict = {}
        one_shot_done = False

        with self.vtn.db.session() as s:
            ven = self._ven_by_ven_id(s, ven_id)
            if ven is None:
                logger.warning(f"Received a report from unknown venID {ven_id}; storing under a placeholder VEN")
                ven = models.Ven(ven_name=f"unknown-{ven_id}", ven_id=ven_id, registered=True)
                s.add(ven)
                s.flush()
            rr = s.scalar(select(models.ReportRequest).where(models.ReportRequest.report_request_id == rrid)) if rrid else None
            registered = s.scalar(select(models.RegisteredReport)
                                  .where(models.RegisteredReport.ven_pk == ven.id,
                                         models.RegisteredReport.report_specifier_id == specifier_id,
                                         models.RegisteredReport.active.is_(True)))
            spec_name = (rr.spec_name if rr and rr.spec_name else None) \
                or (registered.spec_name if registered else None) \
                or (spec.identify_report(report.get("report_name"), specifier_id).name
                    if spec.identify_report(report.get("report_name"), specifier_id) else None)
            rspec = spec.REPORTS.get(spec_name) if spec_name else None
            resource_by_key = {}
            if registered is not None:
                for rd in registered.descriptions:
                    resource_by_key[rd.r_key] = rd.resource_id

            latest = dict(ven.latest_status or {}) if rspec is spec.STATUS_REPORT else None
            static = dict(ven.static_info or {}) if rspec is spec.STATIC_REPORT else None

            # An interval normally carries one oadrReportPayload, but the schema allows several
            payload_rows = []
            for ri in intervals:
                payloads = ri.get("report_payload")
                if isinstance(payloads, dict):
                    payloads = [payloads]
                elif not isinstance(payloads, list):
                    continue
                payload_rows.extend((ri, p) for p in payloads if isinstance(p, dict))
            for ri, payload in payload_rows:
                r_id = payload.get("r_id")
                if r_id is None:
                    continue
                r_key = spec.normalize_key(r_id)
                rid = rspec.rid(r_id) if rspec else None
                raw_value = payload.get("value")
                value_float = to_float(raw_value)
                value_text = None if value_float is not None else (None if raw_value is None else str(raw_value))
                extra = {k: v for k, v in payload.items()
                         if k not in ("r_id", "value", "confidence", "accuracy", "data_quality")}
                ts = ri.get("dtstart") or report.get("dtstart") or now
                sample = models.ReportSample(
                    ven_pk=ven.id, report_request_id=rrid, report_specifier_id=specifier_id, spec_name=spec_name,
                    resource_id=resource_by_key.get(r_key), r_id=str(r_id), r_key=r_key,
                    spec_rid=rid.name if rid else None, ts=ts, received_at=now,
                    value_float=value_float, value_text=value_text,
                    confidence=to_float(payload.get("confidence")), accuracy=to_float(payload.get("accuracy")),
                    data_quality=payload.get("data_quality"), extra=jsonable(extra) if extra else None,
                    message_pk=message_pk)
                s.add(sample)
                key = rid.name if rid else str(r_id)
                entry = {"value": value_float if value_float is not None else value_text,
                         "ts": jsonable(ts), "received_at": jsonable(now), "r_id": str(r_id)}
                if rid is not None and rid.enum is not None:
                    entry["meaning"] = rid.describe_value(entry["value"])
                values_summary[key] = entry["value"]
                if latest is not None:
                    previous = latest.get(key)
                    if previous is None or (previous.get("ts") or "") <= entry["ts"]:
                        latest[key] = entry
                if static is not None:
                    static[key] = entry

            if latest is not None:
                ven.latest_status = latest
            if static is not None:
                ven.static_info = static
            ven.last_seen_at = now
            if rr is not None:
                rr.update_count = (rr.update_count or 0) + 1
                rr.last_update_at = now
                if rr.status in ("queued", "created"):
                    rr.status = "active"
                if rr.one_shot and rr.status != "cancel_requested":
                    rr.status = "cancel_requested"
                    one_shot_done = True
            ven_pk = ven.id

        if one_shot_done and rrid:
            self.vtn.server.cancel_report(ven_id, rrid, report_to_follow=False)
        self.vtn.bus.publish("report.update", ven_pk=ven_pk, ven_id=ven_id, spec_name=spec_name,
                             report_specifier_id=specifier_id, report_request_id=rrid, values=jsonable(values_summary))

    # ------------------------------------------------------------------ VTN initiated requests (UI / scenarios)

    def request_report(self, ven_pk: int, spec_name: Optional[str] = None, report_specifier_id: Optional[str] = None,
                       one_shot: bool = True, granularity_seconds: Optional[float] = None,
                       source: str = "operator", test_step_pk: Optional[int] = None) -> str:
        """
        Queue an oadrCreateReport for the VEN's next poll. Either `spec_name` (x-AHRI_STATUS /
        x-AHRI_STATIC) or an explicit `report_specifier_id` must be given.
        """
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, ven_pk)
            if ven is None or not ven.ven_id:
                raise ValueError("Unknown or unregistered VEN")
            query = select(models.RegisteredReport).where(models.RegisteredReport.ven_pk == ven.id,
                                                          models.RegisteredReport.active.is_(True))
            if report_specifier_id:
                query = query.where(models.RegisteredReport.report_specifier_id == report_specifier_id)
            elif spec_name:
                query = query.where(models.RegisteredReport.spec_name == spec_name)
            else:
                raise ValueError("spec_name or report_specifier_id is required")
            registered = s.scalar(query.order_by(models.RegisteredReport.registered_at.desc()))
            if registered is None:
                raise ValueError(f"VEN '{ven.ven_name}' has not registered {spec_name or report_specifier_id}")
            ven_id = ven.ven_id
            specifier_id = registered.report_specifier_id
            spec_name = registered.spec_name
            min_periods = [rd.min_period_seconds for rd in registered.descriptions if rd.min_period_seconds]
            max_periods = [rd.max_period_seconds for rd in registered.descriptions if rd.max_period_seconds]

        if granularity_seconds is None:
            if spec_name == spec.STATUS_REPORT.name and not one_shot:
                granularity_seconds = self.vtn.config.status_report_interval_seconds
            else:
                granularity_seconds = min(min_periods) if min_periods else 60
        if min_periods and granularity_seconds < min(min_periods):
            granularity_seconds = min(min_periods)
        if max_periods and granularity_seconds > max(max_periods):
            granularity_seconds = max(max_periods)
        granularity = timedelta(seconds=granularity_seconds)
        report_interval = {"dtstart": utcnow(), "duration": granularity} if one_shot else None

        rrid = self.vtn.server.request_report(ven_id, specifier_id, granularity=granularity,
                                              report_back_duration=granularity, report_interval=report_interval)
        with self.vtn.db.session() as s:
            registered = s.scalar(select(models.RegisteredReport)
                                  .where(models.RegisteredReport.ven_pk == ven_pk,
                                         models.RegisteredReport.report_specifier_id == specifier_id,
                                         models.RegisteredReport.active.is_(True)))
            s.add(models.ReportRequest(report_request_id=rrid, ven_pk=ven_pk, report_specifier_id=specifier_id,
                                       spec_name=spec_name,
                                       r_ids=[rd.r_id for rd in registered.descriptions] if registered else [],
                                       granularity_seconds=granularity_seconds,
                                       report_back_duration_seconds=granularity_seconds, one_shot=one_shot,
                                       status="queued", source=source, test_step_pk=test_step_pk))
        self.vtn.bus.publish("report.requested", ven_pk=ven_pk, ven_id=ven_id, report_request_id=rrid,
                             spec_name=spec_name, one_shot=one_shot, source=source)
        return rrid

    def cancel_report(self, report_request_id: str, report_to_follow: bool = False) -> bool:
        with self.vtn.db.session() as s:
            rr = s.scalar(select(models.ReportRequest).where(models.ReportRequest.report_request_id == report_request_id))
            if rr is None:
                return False
            ven = s.get(models.Ven, rr.ven_pk)
            rr.status = "cancel_requested"
            ven_id, ven_pk = ven.ven_id, ven.id
        self.vtn.server.cancel_report(ven_id, report_request_id, report_to_follow=report_to_follow)
        self.vtn.bus.publish("report.cancel_requested", ven_pk=ven_pk, ven_id=ven_id, report_request_id=report_request_id)
        return True
