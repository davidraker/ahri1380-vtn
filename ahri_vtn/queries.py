"""
Read-side helpers shared by the web UI / API and the conformance scenarios.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import desc, func, select

from . import spec
from .db import models
from .util import utcnow


class Queries:
    def __init__(self, vtn):
        self.vtn = vtn

    # ------------------------------------------------------------------ VENs

    def online_threshold(self) -> timedelta:
        return timedelta(seconds=self.vtn.config.requested_poll_freq_seconds * self.vtn.config.offline_after_polls + 2)

    def ven_summary(self, ven: models.Ven, now: Optional[datetime] = None) -> dict:
        now = now or utcnow()
        online = bool(ven.last_poll_at and now - ven.last_poll_at <= self.online_threshold())
        latest = ven.latest_status or {}
        status = {k: latest.get(k, {}).get("value") for k in ("Inst Power", "Operational State", "Override State")}
        status_ts = max((v.get("received_at") or "" for v in latest.values()), default=None)
        op_state = status.get("Operational State")
        return {**ven.as_dict(), "online": online, "status": status, "status_ts": status_ts,
                "operational_state_name": spec.operational_state_name(op_state) if op_state is not None else None,
                "rated_power": ((ven.static_info or {}).get("Size") or {}).get("value")}

    def list_vens(self) -> list[dict]:
        with self.vtn.db.session() as s:
            vens = s.scalars(select(models.Ven).order_by(models.Ven.ven_name)).all()
            now = utcnow()
            result = []
            for ven in vens:
                summary = self.ven_summary(ven, now)
                summary["active_events"] = s.scalar(select(func.count(models.Event.id)).where(
                    models.Event.ven_pk == ven.id, models.Event.status.in_(("far", "near", "active"))))
                result.append(summary)
            return result

    def get_ven(self, ven_pk: int) -> Optional[dict]:
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, ven_pk)
            if ven is None:
                return None
            summary = self.ven_summary(ven)
            summary["registered_reports"] = []
            for rr in sorted(ven.registered_reports, key=lambda r: r.registered_at, reverse=True):
                item = rr.as_dict()
                item["descriptions"] = [d.as_dict() for d in rr.descriptions]
                rspec = spec.REPORTS.get(rr.spec_name)
                item["checklist"] = self.report_checklist(rspec, rr) if rspec else None
                summary["registered_reports"].append(item)
            summary["report_requests"] = [r.as_dict() for r in s.scalars(
                select(models.ReportRequest).where(models.ReportRequest.ven_pk == ven.id)
                .order_by(desc(models.ReportRequest.requested_at)).limit(50))]
            summary["registration_events"] = [r.as_dict() for r in s.scalars(
                select(models.RegistrationEvent).where(models.RegistrationEvent.ven_pk == ven.id)
                .order_by(desc(models.RegistrationEvent.ts)).limit(20))]
            summary["findings"] = [f.as_dict() for f in s.scalars(
                select(models.Finding).where(models.Finding.ven_pk == ven.id, models.Finding.run_pk.is_(None))
                .order_by(desc(models.Finding.ts)).limit(100))]
            summary["events"] = self._events_for(s, ven.id, limit=30)
            summary["opts"] = [o.as_dict() for o in s.scalars(
                select(models.Opt).where(models.Opt.ven_pk == ven.id).order_by(desc(models.Opt.received_at)).limit(30))]
            summary["static_rows"] = self.static_rows(ven)
            summary["status_rows"] = self.status_rows(ven)
            summary["sample_count"] = s.scalar(select(func.count(models.ReportSample.id))
                                               .where(models.ReportSample.ven_pk == ven.id))
            return summary

    def ven_by_ven_id(self, ven_id: str) -> Optional[dict]:
        with self.vtn.db.session() as s:
            ven = s.scalar(select(models.Ven).where(models.Ven.ven_id == ven_id))
            return self.ven_summary(ven) if ven else None

    @staticmethod
    def report_checklist(rspec: spec.ReportSpec, rr: models.RegisteredReport) -> list[dict]:
        rows = []
        by_key = {}
        for d in rr.descriptions:
            for rid in rspec.rids:
                if d.r_key in rid.keys:
                    by_key[rid.key] = d
        for rid in rspec.rids:
            d = by_key.get(rid.key)
            rows.append({"rid": rid.name, "description": rid.description, "present": d is not None,
                         "r_id": d.r_id if d else None, "report_type": d.report_type if d else None,
                         "reading_type": d.reading_type if d else None,
                         "unit": d.measurement_unit if d else None,
                         "measurement_description": d.measurement_description if d else None,
                         "min_period": d.min_period_seconds if d else None,
                         "max_period": d.max_period_seconds if d else None,
                         "expected_units": ", ".join(rid.units), "expected_type": rid.report_type,
                         "expected_reading": rid.reading_type})
        for d in [d for d in rr.descriptions if d.spec_rid is None]:
            rows.append({"rid": None, "description": "(not in AHRI 1380)", "present": True, "r_id": d.r_id,
                         "report_type": d.report_type, "reading_type": d.reading_type, "unit": d.measurement_unit,
                         "measurement_description": d.measurement_description, "min_period": d.min_period_seconds,
                         "max_period": d.max_period_seconds, "expected_units": "", "expected_type": "",
                         "expected_reading": ""})
        return rows

    @staticmethod
    def static_rows(ven: models.Ven) -> list[dict]:
        info = ven.static_info or {}
        rows = []
        for rid in spec.STATIC_REPORT.rids:
            entry = info.get(rid.name)
            rows.append({"rid": rid.name, "description": rid.description,
                         "value": entry.get("value") if entry else None,
                         "meaning": rid.describe_value(entry.get("value")) if entry and rid.enum else None,
                         "ts": entry.get("received_at") if entry else None})
        return rows

    @staticmethod
    def status_rows(ven: models.Ven) -> list[dict]:
        info = ven.latest_status or {}
        rows = []
        for rid in spec.STATUS_REPORT.rids:
            entry = info.get(rid.name)
            rows.append({"rid": rid.name, "description": rid.description,
                         "value": entry.get("value") if entry else None,
                         "meaning": rid.describe_value(entry.get("value")) if entry and rid.enum else None,
                         "ts": entry.get("ts") if entry else None,
                         "received_at": entry.get("received_at") if entry else None})
        return rows

    # ------------------------------------------------------------------ samples

    def samples(self, ven_pk: int, spec_rid: Optional[str] = None, r_key: Optional[str] = None,
                since: Optional[datetime] = None, until: Optional[datetime] = None, limit: int = 2000,
                order: str = "asc") -> list[models.ReportSample]:
        with self.vtn.db.session() as s:
            q = select(models.ReportSample).where(models.ReportSample.ven_pk == ven_pk)
            if spec_rid:
                q = q.where(models.ReportSample.spec_rid == spec_rid)
            if r_key:
                q = q.where(models.ReportSample.r_key == r_key)
            if since:
                q = q.where(models.ReportSample.ts >= since)
            if until:
                q = q.where(models.ReportSample.ts <= until)
            q = q.order_by(models.ReportSample.ts.asc() if order == "asc" else models.ReportSample.ts.desc()).limit(limit)
            return s.scalars(q).all()

    def status_series(self, ven_pk: int, since: datetime, until: Optional[datetime] = None) -> dict:
        """Time series of the three status rIDs as {rid: [(ts, value), ...]}."""
        series = {rid.name: [] for rid in spec.STATUS_REPORT.rids}
        for sample in self.samples(ven_pk, since=since, until=until, limit=20000):
            if sample.spec_rid in series:
                series[sample.spec_rid].append((sample.ts, sample.value))
        return series

    def latest_sample(self, ven_pk: int, spec_rid: str) -> Optional[models.ReportSample]:
        rows = self.samples(ven_pk, spec_rid=spec_rid, limit=1, order="desc")
        return rows[0] if rows else None

    # ------------------------------------------------------------------ events / opts

    def _events_for(self, s, ven_pk: Optional[int], limit: int = 100, status: Optional[str] = None,
                    event_id: Optional[str] = None) -> list[dict]:
        q = select(models.Event)
        if ven_pk is not None:
            q = q.where(models.Event.ven_pk == ven_pk)
        if status:
            q = q.where(models.Event.status == status)
        if event_id:
            q = q.where(models.Event.event_id == event_id)
        q = q.order_by(desc(models.Event.created_at)).limit(limit)
        result = []
        for ev in s.scalars(q):
            item = ev.as_dict()
            item["dtend"] = ev.dtend.isoformat()
            item["responses"] = [r.as_dict() for r in sorted(ev.responses, key=lambda r: r.received_at)]
            ven = s.get(models.Ven, ev.ven_pk)
            item["ven_name"] = ven.ven_name if ven else None
            item["preset_label"] = spec.EVENT_PRESETS[ev.preset].label if ev.preset in spec.EVENT_PRESETS else ev.preset
            result.append(item)
        return result

    def list_events(self, ven_pk: Optional[int] = None, limit: int = 100, status: Optional[str] = None) -> list[dict]:
        with self.vtn.db.session() as s:
            return self._events_for(s, ven_pk, limit, status)

    def get_event(self, event_id: str) -> Optional[dict]:
        with self.vtn.db.session() as s:
            rows = self._events_for(s, None, limit=1, event_id=event_id)
            return rows[0] if rows else None

    def event_responses(self, event_id: str) -> list[models.EventResponse]:
        with self.vtn.db.session() as s:
            return s.scalars(select(models.EventResponse).where(models.EventResponse.event_id == event_id)
                             .order_by(models.EventResponse.received_at)).all()

    def opts(self, ven_pk: Optional[int] = None, since: Optional[datetime] = None, limit: int = 100) -> list[models.Opt]:
        with self.vtn.db.session() as s:
            q = select(models.Opt)
            if ven_pk is not None:
                q = q.where(models.Opt.ven_pk == ven_pk)
            if since is not None:
                q = q.where(models.Opt.received_at >= since)
            return s.scalars(q.order_by(desc(models.Opt.received_at)).limit(limit)).all()

    # ------------------------------------------------------------------ messages

    def list_messages(self, ven_id: Optional[str] = None, message_type: Optional[str] = None,
                      direction: Optional[str] = None, violations_only: bool = False, page: int = 1,
                      page_size: int = 50) -> dict:
        with self.vtn.db.session() as s:
            q = select(models.Message)
            if ven_id:
                q = q.where(models.Message.ven_id == ven_id)
            if message_type:
                q = q.where(models.Message.message_type == message_type)
            if direction:
                q = q.where(models.Message.direction == direction)
            if violations_only:
                q = q.where(models.Message.schema_error.is_not(None))
            total = s.scalar(select(func.count()).select_from(q.subquery()))
            rows = s.scalars(q.order_by(desc(models.Message.id)).offset((page - 1) * page_size).limit(page_size)).all()
            items = []
            for m in rows:
                item = m.as_dict()
                item["raw_xml"] = None
                item["payload"] = None
                item["size"] = len(m.raw_xml or "")
                items.append(item)
            types = [t for (t,) in s.execute(select(models.Message.message_type).distinct()
                                              .order_by(models.Message.message_type)) if t]
            return {"items": items, "total": total, "page": page, "page_size": page_size, "types": types,
                    "pages": max(1, (total + page_size - 1) // page_size)}

    def get_message(self, message_pk: int) -> Optional[dict]:
        with self.vtn.db.session() as s:
            m = s.get(models.Message, message_pk)
            if m is None:
                return None
            item = m.as_dict()
            pair = []
            if m.pair_id:
                pair = [p.as_dict() | {"raw_xml": None, "payload": None} for p in s.scalars(
                    select(models.Message).where(models.Message.pair_id == m.pair_id).order_by(models.Message.id))]
            item["pair"] = pair
            return item

    # ------------------------------------------------------------------ findings & runs

    def findings(self, ven_pk: Optional[int] = None, run_pk: Optional[int] = None, limit: int = 500) -> list[dict]:
        with self.vtn.db.session() as s:
            q = select(models.Finding)
            if ven_pk is not None:
                q = q.where(models.Finding.ven_pk == ven_pk)
            if run_pk is not None:
                q = q.where(models.Finding.run_pk == run_pk)
            return [f.as_dict() for f in s.scalars(q.order_by(desc(models.Finding.ts)).limit(limit))]

    def list_runs(self, limit: int = 100) -> list[dict]:
        with self.vtn.db.session() as s:
            result = []
            for run in s.scalars(select(models.TestRun).order_by(desc(models.TestRun.started_at)).limit(limit)):
                item = run.as_dict()
                ven = s.get(models.Ven, run.ven_pk) if run.ven_pk else None
                item["ven_name"] = ven.ven_name if ven else None
                item["step_counts"] = {}
                for step in run.steps:
                    item["step_counts"][step.status] = item["step_counts"].get(step.status, 0) + 1
                result.append(item)
            return result

    def get_run(self, run_pk: int) -> Optional[dict]:
        with self.vtn.db.session() as s:
            run = s.get(models.TestRun, run_pk)
            if run is None:
                return None
            item = run.as_dict()
            ven = s.get(models.Ven, run.ven_pk) if run.ven_pk else None
            item["ven_name"] = ven.ven_name if ven else None
            item["steps"] = [st.as_dict() for st in run.steps]
            item["findings"] = [f.as_dict() for f in s.scalars(
                select(models.Finding).where(models.Finding.run_pk == run.id).order_by(models.Finding.ts))]
            return item

    def dashboard_counts(self) -> dict:
        with self.vtn.db.session() as s:
            return {
                "vens": s.scalar(select(func.count(models.Ven.id))),
                "events_active": s.scalar(select(func.count(models.Event.id))
                                          .where(models.Event.status.in_(("far", "near", "active")))),
                "samples": s.scalar(select(func.count(models.ReportSample.id))),
                "messages": s.scalar(select(func.count(models.Message.id))),
                "runs_running": s.scalar(select(func.count(models.TestRun.id)).where(models.TestRun.status == "running")),
                "violations": s.scalar(select(func.count(models.Message.id)).where(models.Message.schema_error.is_not(None))),
            }
