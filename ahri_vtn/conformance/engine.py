"""
Conformance scenario engine.

A Scenario is a coroutine that drives the VTN (sends events, requests reports), observes the
VEN's behaviour (responses, samples, opts) and records assertions. Runs, steps and assertions are
persisted (TestRun / TestStep / Finding) and progress is published on the live bus so the UI can
follow along and answer operator prompts (e.g. "trigger the customer override now").
"""
from __future__ import annotations

import asyncio
import logging
import statistics
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from sqlalchemy import or_, select

from .. import spec
from ..db import models
from ..targets import Selection, Unit, VenTarget
from ..util import jsonable, utcnow

logger = logging.getLogger("ahri_vtn.conformance")


class RunAborted(Exception):
    """Raised inside a scenario when the operator aborts (or a prompt is answered with Abort)."""


class StepFailed(Exception):
    """Raised by StepContext.require() to stop the current step early (the step is marked failed)."""


@dataclass
class Scenario:
    id: str
    title: str
    section: str
    description: str
    run: Callable[["RunContext"], Any]
    default_params: dict = field(default_factory=dict)
    composite: bool = False

    def describe(self) -> dict:
        return {"id": self.id, "title": self.title, "section": self.section, "description": self.description,
                "default_params": self.default_params, "composite": self.composite}


# ---------------------------------------------------------------------------------------------
# Step context
# ---------------------------------------------------------------------------------------------

class StepContext:
    def __init__(self, rc: "RunContext", step_pk: int, name: str):
        self.rc = rc
        self.step_pk = step_pk
        self.name = name
        self.assertions: list[dict] = []
        self.answer: Optional[str] = None
        self.note: Optional[str] = None

    def _record(self, status: str, message: str, evidence: Optional[dict] = None):
        entry = {"status": status, "message": message, "ts": utcnow().isoformat()}
        if evidence:
            entry["evidence"] = jsonable(evidence)
        self.assertions.append(entry)
        severity = {"pass": "pass", "fail": "fail", "warning": "warning", "manual": "warning", "info": "info"}[status]
        self.rc.engine._add_finding(self.rc, self.step_pk, severity, f"{self.rc.scenario.id}:{self.name}", message, evidence)
        self.rc.engine._update_step(self.step_pk, details={"assertions": self.assertions})
        logger.info(f"[run {self.rc.run_pk}] {self.name}: {status.upper()} {message}")
        return status == "pass"

    def check(self, condition: bool, message: str, evidence: Optional[dict] = None, severity: str = "fail") -> bool:
        return self._record("pass" if condition else severity, message, evidence)

    def require(self, condition: bool, message: str, evidence: Optional[dict] = None) -> None:
        if not self.check(condition, message, evidence):
            raise StepFailed(message)

    def info(self, message: str, evidence: Optional[dict] = None):
        self._record("info", message, evidence)

    def warn(self, message: str, evidence: Optional[dict] = None):
        self._record("warning", message, evidence)

    def manual(self, message: str, evidence: Optional[dict] = None):
        """Something the lab must verify by hand (wattmeter, resistance heat, ramp)."""
        self._record("manual", message, evidence)

    async def prompt(self, message: str, options=("Continue", "Skip", "Abort"), input_hint: Optional[str] = None,
                     timeout: Optional[float] = None) -> str:
        """
        Ask the operator something and wait for the answer (via the UI / API). When operator
        prompts are disabled in the run parameters the first option is returned immediately.
        """
        if not self.rc.params.get("operator_prompts", True):
            self.answer = options[0]
            self._record("info", f"Prompt auto-answered '{options[0]}': {message}")
            return options[0]
        answer, note = await self.rc.engine._wait_for_prompt(self.rc, self.step_pk, message, list(options),
                                                             input_hint, timeout)
        self.answer, self.note = answer, note
        self._record("info", f"Operator answered '{answer}'" + (f" ({note})" if note else "") + f": {message}")
        if answer == "Abort":
            raise RunAborted(message)
        return answer

    async def sleep(self, seconds: float, reason: str = ""):
        seconds = self.rc.scaled(seconds)
        if reason:
            self.rc.engine._update_step(self.step_pk, details={"assertions": self.assertions,
                                                               "waiting": f"{reason} ({seconds:.0f}s)"})
        await asyncio.sleep(seconds)

    @property
    def final_status(self) -> str:
        statuses = {a["status"] for a in self.assertions}
        if "fail" in statuses:
            return "failed"
        if "manual" in statuses:
            return "manual"
        if "pass" in statuses:
            return "passed"
        if "warning" in statuses:
            return "manual"
        return "info"


# ---------------------------------------------------------------------------------------------
# Run context (helpers used by scenarios)
# ---------------------------------------------------------------------------------------------

class RunContext:
    """
    Everything a scenario needs: the selection of targets (VENs / resources / groups), the
    units (VEN-resource pairs) whose behaviour must be checked, and helpers to send events,
    request reports and wait for VEN behaviour. Single-VEN helpers (latest_status, rated_power,
    ...) operate on the first unit for scenarios that do not care about multiple units.
    """

    def __init__(self, engine: "ConformanceEngine", run_pk: int, scenario: Scenario, selection: Selection,
                 mode: str, params: dict):
        self.engine = engine
        self.vtn = engine.vtn
        self.run_pk = run_pk
        self.scenario = scenario
        self.selection = selection
        self.targets: list[VenTarget] = [t for t in selection.resolve(engine.vtn.db) if t.ven_id]
        self.units: list[Unit] = [u for t in self.targets for u in t.units()]
        self.ven_pk = self.targets[0].ven_pk if self.targets else None
        self.mode = mode
        self.params = params
        self.step_index = 0
        self.created_event_ids: list[str] = []
        self.dispatch_pks: list[int] = []
        self.shared: dict = {}      # scratch space shared between steps / sub-scenarios

    @property
    def multi(self) -> bool:
        return len(self.units) > 1

    def tag(self, unit: Unit) -> str:
        """Prefix for per-unit assertion messages when more than one unit is under test."""
        return f"[{unit.label}] " if self.multi else ""

    def refresh_units(self):
        """Re-resolve the selection (e.g. after a static report revealed the VEN's resources)."""
        self.targets = [t for t in self.selection.resolve(self.vtn.db) if t.ven_id]
        self.units = [u for t in self.targets for u in t.units()]

    # ---- parameters / timing

    def scaled(self, seconds: float) -> float:
        return float(seconds) * float(self.params.get("time_scale", 1.0))

    def p(self, key: str, default=None):
        value = self.params.get(key, default)
        return value

    @property
    def thresholds(self):
        return self.vtn.config.thresholds

    # ---- VEN state

    def ven(self, ven_pk: Optional[int] = None) -> dict:
        ven = self.vtn.queries.get_ven(ven_pk or self.ven_pk)
        if ven is None:
            raise RunAborted("VEN was deleted")
        return ven

    def ven_row(self, ven_pk: Optional[int] = None) -> models.Ven:
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, ven_pk or self.ven_pk)
            s.expunge(ven)
            return ven

    @property
    def ven_id(self) -> Optional[str]:
        return self.ven_row().ven_id

    @property
    def unit(self) -> Unit:
        return self.units[0]

    # ---- per-unit state

    def _unit_caches(self, unit: Unit):
        """(latest_status, static_info) dicts for a unit: the resource's, or the VEN's when it has none."""
        with self.vtn.db.session() as s:
            if unit.resource_id:
                res = s.scalar(select(models.Resource).where(models.Resource.ven_pk == unit.ven_pk,
                                                             models.Resource.resource_id == unit.resource_id))
                if res is not None:
                    return dict(res.latest_status or {}), dict(res.static_info or {})
            ven = s.get(models.Ven, unit.ven_pk)
            return dict(ven.latest_status or {}), dict(ven.static_info or {})

    def unit_status(self, unit: Unit) -> dict:
        """{rid name: value} from the unit's latest status cache."""
        latest, _ = self._unit_caches(unit)
        return {k: v.get("value") for k, v in latest.items()}

    def unit_static(self, unit: Unit, rid: str):
        _, static = self._unit_caches(unit)
        return (static.get(rid) or {}).get("value")

    def unit_rated_power(self, unit: Unit) -> Optional[float]:
        value = self.unit_static(unit, "Size")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def unit_unloading_type(self, unit: Unit) -> Optional[int]:
        value = self.unit_static(unit, "Unloading_Type")
        try:
            return int(float(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    def unit_samples(self, unit: Unit, rid: str, since: datetime, until: Optional[datetime] = None):
        return self.vtn.queries.samples(unit.ven_pk, spec_rid=rid, since=since, until=until, limit=10000,
                                        resource_id=unit.resource_id)

    def unit_samples_received_since(self, unit: Unit, rid: str, since: datetime) -> list[models.ReportSample]:
        with self.vtn.db.session() as s:
            q = select(models.ReportSample).where(models.ReportSample.ven_pk == unit.ven_pk,
                                                  models.ReportSample.spec_rid == rid,
                                                  models.ReportSample.received_at >= since)
            if unit.resource_id:
                q = q.where(models.ReportSample.resource_id == unit.resource_id)
            return s.scalars(q.order_by(models.ReportSample.received_at)).all()

    def unit_active_status_request(self, unit: Unit) -> Optional[models.ReportRequest]:
        with self.vtn.db.session() as s:
            q = select(models.ReportRequest).where(models.ReportRequest.ven_pk == unit.ven_pk,
                                                   models.ReportRequest.spec_name == spec.STATUS_REPORT.name,
                                                   models.ReportRequest.one_shot.is_(False),
                                                   models.ReportRequest.status.in_(("queued", "created", "active")))
            if unit.resource_id:
                q = q.where(or_(models.ReportRequest.resource_id == unit.resource_id,
                                models.ReportRequest.resource_id.is_(None)))
            return s.scalar(q.order_by(models.ReportRequest.requested_at.desc()))

    def unit_opts_since(self, unit: Unit, since: datetime) -> list[models.Opt]:
        return [o for o in self.vtn.queries.opts(ven_pk=unit.ven_pk, since=since)
                if not (unit.resource_id and o.resource_id and o.resource_id != unit.resource_id)]

    # ---- single-unit conveniences (first unit)

    def latest_status(self) -> dict:
        return self.unit_status(self.unit)

    def static_value(self, rid: str):
        return self.unit_static(self.unit, rid)

    def rated_power(self) -> Optional[float]:
        return self.unit_rated_power(self.unit)

    def unloading_type(self) -> Optional[int]:
        return self.unit_unloading_type(self.unit)

    def samples(self, rid: str, since: datetime, until: Optional[datetime] = None) -> list[models.ReportSample]:
        return self.unit_samples(self.unit, rid, since, until)

    def samples_received_since(self, rid: str, since: datetime) -> list[models.ReportSample]:
        return self.unit_samples_received_since(self.unit, rid, since)

    def active_status_request(self) -> Optional[models.ReportRequest]:
        return self.unit_active_status_request(self.unit)

    def event_responses(self, event_id: str) -> list[models.EventResponse]:
        return self.vtn.queries.event_responses(event_id)

    def opts_since(self, since: datetime) -> list[models.Opt]:
        return self.unit_opts_since(self.unit, since)

    # ---- actions

    def dispatch(self, preset: str, duration_seconds: float, start_offset_seconds: float = 0, **kwargs) -> dict:
        """
        Send an event to the whole selection (one OpenADR event per VEN). Returns the dispatch
        dict: {'dispatch_pk', 'events': {ven_pk: event_id}, 'targets': [...]}.
        """
        start = utcnow() + timedelta(seconds=self.scaled(start_offset_seconds)) if start_offset_seconds else utcnow()
        result = self.vtn.events.dispatch_event(self.selection, preset, start=start,
                                                duration=timedelta(seconds=self.scaled(duration_seconds)),
                                                source="scenario", test_event=bool(self.p("test_event", False)),
                                                vtn_comment=f"AHRI 1380 {self.scenario.id} run {self.run_pk}", **kwargs)
        self.created_event_ids.extend(result["events"].values())
        self.dispatch_pks.append(result["dispatch_pk"])
        return result

    def create_event(self, preset: str, duration_seconds: float, start_offset_seconds: float = 0, **kwargs) -> str:
        """Single-VEN convenience: dispatch to the selection and return the first VEN's event id."""
        result = self.dispatch(preset, duration_seconds, start_offset_seconds, **kwargs)
        return next(iter(result["events"].values()))

    def cancel_event(self, event_id: str) -> bool:
        return self.vtn.events.cancel_event(event_id)

    def cancel_dispatch(self, dispatch: dict) -> int:
        return self.vtn.events.cancel_dispatch(dispatch["dispatch_pk"])

    def request_reports(self, spec_name: str, one_shot: bool = True, test_step_pk: Optional[int] = None) -> dict:
        return self.vtn.reports.request_reports(self.selection, spec_name, one_shot=one_shot, source="scenario",
                                                test_step_pk=test_step_pk)

    def cleanup_events(self):
        for event_id in self.created_event_ids:
            try:
                self.vtn.events.cancel_event(event_id)
            except Exception:   # noqa: BLE001
                pass

    # ---- waiting

    async def wait_event_response(self, event_id: str, timeout: float, modification_number: Optional[int] = None,
                                  after: Optional[datetime] = None) -> Optional[models.EventResponse]:
        def existing():
            for r in self.event_responses(event_id):
                if modification_number is not None and r.modification_number != modification_number:
                    continue
                if after is not None and r.received_at < after:
                    continue
                return r
            return None

        if existing():
            return existing()
        await self.vtn.bus.wait_for(lambda e: e["topic"] == "event.response" and e.get("event_id") == event_id
                                    and (modification_number is None or e.get("modification_number") == modification_number),
                                    timeout=self.scaled(timeout))
        return existing()

    async def wait_dispatch_responses(self, dispatch: dict, timeout: float,
                                      modification_number: Optional[int] = None) -> dict:
        """Wait for the oadrCreatedEvent of every VEN in a dispatch: {ven_pk: EventResponse or None}."""
        deadline = asyncio.get_event_loop().time() + self.scaled(timeout)
        results = {}
        for ven_pk, event_id in dispatch["events"].items():
            remaining = max(deadline - asyncio.get_event_loop().time(), 0)
            results[ven_pk] = await self.wait_event_response(event_id, remaining / max(self.scaled(1), 1e-9),
                                                             modification_number=modification_number)
        return results

    async def wait_unit_samples(self, unit: Unit, rid: str, count: int, since: datetime, timeout: float,
                                received: bool = True) -> list[models.ReportSample]:
        """Wait until `count` samples of rid for the unit have been received (or sampled) after `since`."""
        deadline = asyncio.get_event_loop().time() + self.scaled(timeout)
        while True:
            rows = self.unit_samples_received_since(unit, rid, since) if received else self.unit_samples(unit, rid, since)
            if len(rows) >= count:
                return rows
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return rows
            await self.vtn.bus.wait_for(lambda e: e["topic"] == "report.update" and e.get("ven_pk") == unit.ven_pk,
                                        timeout=remaining)

    async def wait_samples(self, rid: str, count: int, since: datetime, timeout: float) -> list[models.ReportSample]:
        return await self.wait_unit_samples(self.unit, rid, count, since, timeout, received=False)

    async def wait_unit_status(self, unit: Unit, predicate: Callable[[dict], bool], timeout: float) -> Optional[dict]:
        """Wait for the unit's latest status (dict rid -> value) to satisfy predicate."""
        status = self.unit_status(unit)
        if status and predicate(status):
            return status
        deadline = asyncio.get_event_loop().time() + self.scaled(timeout)
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            await self.vtn.bus.wait_for(lambda e: e["topic"] == "report.update" and e.get("ven_pk") == unit.ven_pk,
                                        timeout=remaining)
            status = self.unit_status(unit)
            if status and predicate(status):
                return status

    async def wait_status(self, predicate: Callable[[dict], bool], timeout: float) -> Optional[dict]:
        return await self.wait_unit_status(self.unit, predicate, timeout)

    async def wait_unit_opt(self, unit: Unit, since: datetime, timeout: float,
                            opt_type: Optional[str] = "optOut") -> Optional[models.Opt]:
        def existing():
            for o in self.unit_opts_since(unit, since):
                if opt_type is None or o.opt_type == opt_type:
                    return o
            return None
        if existing():
            return existing()
        await self.vtn.bus.wait_for(lambda e: e["topic"] == "opt.created" and e.get("ven_pk") == unit.ven_pk
                                    and (opt_type is None or e.get("opt_type") == opt_type), timeout=self.scaled(timeout))
        return existing()

    async def wait_opt(self, since: datetime, timeout: float, opt_type: Optional[str] = "optOut") -> Optional[models.Opt]:
        return await self.wait_unit_opt(self.unit, since, timeout, opt_type)

    async def wait_topic(self, predicate: Callable[[dict], bool], timeout: float) -> Optional[dict]:
        return await self.vtn.bus.wait_for(predicate, timeout=self.scaled(timeout))

    # ---- steps

    @asynccontextmanager
    async def step(self, name: str):
        self.step_index += 1
        step_pk = self.engine._create_step(self.run_pk, self.step_index, name)
        sc = StepContext(self, step_pk, name)
        self.engine.vtn.bus.publish("run.step", run_pk=self.run_pk, step_pk=step_pk, name=name, status="running")
        try:
            yield sc
        except StepFailed:
            pass
        except RunAborted:
            self.engine._update_step(step_pk, status="skipped", finished=True,
                                     details={"assertions": sc.assertions, "answer": sc.answer, "note": sc.note})
            raise
        except asyncio.CancelledError:
            self.engine._update_step(step_pk, status="skipped", finished=True, details={"assertions": sc.assertions})
            raise
        except Exception as err:   # noqa: BLE001
            logger.exception(f"Step '{name}' crashed")
            sc._record("fail", f"Step crashed: {err.__class__.__name__}: {err}")
        status = sc.final_status
        self.engine._update_step(step_pk, status=status, finished=True,
                                 details={"assertions": sc.assertions, "answer": sc.answer, "note": sc.note})
        self.engine.vtn.bus.publish("run.step", run_pk=self.run_pk, step_pk=step_pk, name=name, status=status)


# ---------------------------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------------------------

class ConformanceEngine:
    def __init__(self, vtn):
        self.vtn = vtn
        from . import scenarios as _scenarios
        self.scenarios: dict[str, Scenario] = {s.id: s for s in _scenarios.ALL}
        self._tasks: dict[int, asyncio.Task] = {}
        self._prompts: dict[int, dict] = {}      # run_pk -> {'future', 'step_pk', 'message', 'options', 'input'}

    # ---- public API

    def list_scenarios(self) -> list[dict]:
        return [s.describe() for s in self.scenarios.values()]

    def default_params(self) -> dict:
        t = self.vtn.config.thresholds
        return {"time_scale": 1.0, "stabilization_seconds": t.stabilization_seconds, "sample_count": t.sample_count,
                "sample_interval_seconds": t.sample_interval_seconds,
                "response_timeout_seconds": t.event_response_timeout_seconds,
                "override_timeout_seconds": t.override_timeout_seconds, "status_interval_tolerance": t.status_interval_tolerance,
                "general_fraction": t.general_curtailment_fraction, "critical_fraction": t.critical_curtailment_fraction,
                "off_mode_max_watts": t.off_mode_max_watts, "max_mito": t.max_mito_degrees_f,
                "status_samples": 3, "report_timeout_seconds": 180, "recovery_timeout_seconds": 300,
                "future_offset_seconds": 600, "mito": 3, "price_intervals": 25, "price_interval_minutes": 60,
                "poll_interval_seconds": self.vtn.config.requested_poll_freq_seconds,
                "operator_prompts": True, "wattmeter_prompt": True, "test_event": False}

    def start_run(self, scenario_id: str, ven_pk: Optional[int] = None, mode: str = "cooling",
                  params: Optional[dict] = None, name: Optional[str] = None, selection=None) -> int:
        """
        Start a scenario against a selection of targets (whole VENs, resources, groups). `ven_pk`
        is a convenience for "this whole VEN"; `selection` takes a Selection, the wire dict or a
        list of form values.
        """
        if scenario_id not in self.scenarios:
            raise ValueError(f"Unknown scenario '{scenario_id}'")
        sel = Selection.from_any(selection) if selection is not None else None
        if (sel is None or sel.is_empty()) and ven_pk is not None:
            sel = Selection.whole_ven(ven_pk)
        if sel is None or sel.is_empty():
            raise ValueError("Select at least one VEN, resource or group")
        targets = sel.resolve(self.vtn.db)
        unregistered = [t.ven_name for t in targets if not t.ven_id]
        if not targets:
            raise ValueError("The selection contains no VEN")
        if unregistered:
            raise ValueError(f"Not registered: {', '.join(unregistered)}")
        ven_pk = targets[0].ven_pk
        scenario = self.scenarios[scenario_id]
        merged = {**self.default_params(), **scenario.default_params, **(params or {})}
        for key in ("operator_prompts", "wattmeter_prompt", "test_event"):
            if isinstance(merged.get(key), str):
                merged[key] = merged[key].strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(merged.get(key), (int, float)):
                merged[key] = bool(merged[key])
        target_label = "; ".join(t.describe() for t in targets)
        with self.vtn.db.session() as s:
            run = models.TestRun(scenario_id=scenario_id, name=name or f"{scenario.title} - {target_label}",
                                 ven_pk=ven_pk, selection=sel.as_dict(), resolved=[t.as_dict() for t in targets],
                                 mode=mode, params=jsonable(merged), status="running")
            s.add(run)
            s.flush()
            run_pk = run.id
        rc = RunContext(self, run_pk, scenario, sel, mode, merged)
        self._tasks[run_pk] = asyncio.get_event_loop().create_task(self._execute(rc))
        self.vtn.bus.publish("run.started", run_pk=run_pk, scenario_id=scenario_id, ven_pk=ven_pk)
        logger.info(f"Started run {run_pk}: {scenario.title} for VEN {ven_pk} ({mode})")
        return run_pk

    async def abort_run(self, run_pk: int) -> bool:
        task = self._tasks.get(run_pk)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return True

    def respond(self, run_pk: int, answer: str, note: Optional[str] = None) -> bool:
        prompt = self._prompts.get(run_pk)
        if prompt is None or prompt["future"].done():
            return False
        prompt["future"].set_result((answer, note))
        return True

    def pending_prompt(self, run_pk: int) -> Optional[dict]:
        prompt = self._prompts.get(run_pk)
        if prompt is None or prompt["future"].done():
            return None
        return {"step_pk": prompt["step_pk"], "message": prompt["message"], "options": prompt["options"],
                "input": prompt["input"]}

    def set_notes(self, run_pk: int, notes: str):
        with self.vtn.db.session() as s:
            run = s.get(models.TestRun, run_pk)
            if run is not None:
                run.operator_notes = notes

    async def shutdown(self):
        for run_pk, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # ---- execution

    async def _execute(self, rc: RunContext):
        status, error = "passed", None
        try:
            await rc.scenario.run(rc)
        except RunAborted as err:
            status, error = "aborted", str(err)
        except asyncio.CancelledError:
            status, error = "aborted", "cancelled by operator"
        except Exception as err:   # noqa: BLE001
            logger.exception(f"Run {rc.run_pk} crashed")
            status, error = "error", f"{err.__class__.__name__}: {err}"
        finally:
            rc.cleanup_events()
            self._prompts.pop(rc.run_pk, None)
        with self.vtn.db.session() as s:
            run = s.get(models.TestRun, rc.run_pk)
            counts: dict = {}
            for step in run.steps:
                counts[step.status] = counts.get(step.status, 0) + 1
            if status == "passed":
                if counts.get("failed"):
                    status = "failed"
                elif counts.get("manual") or counts.get("waiting"):
                    status = "needs_review"
            run.status = status
            run.finished_at = utcnow()
            run.summary = {"steps": counts, "error": error, "events": rc.created_event_ids,
                           "dispatches": rc.dispatch_pks, "mode": rc.mode,
                           "units": [{"unit": u.label, "rated_power": rc.unit_rated_power(u),
                                      "unloading_type": rc.unit_unloading_type(u)} for u in rc.units]}
        self.vtn.bus.publish("run.finished", run_pk=rc.run_pk, status=status)
        logger.info(f"Run {rc.run_pk} finished: {status}")

    async def _wait_for_prompt(self, rc: RunContext, step_pk: int, message: str, options: list, input_hint, timeout):
        future = asyncio.get_event_loop().create_future()
        self._prompts[rc.run_pk] = {"future": future, "step_pk": step_pk, "message": message, "options": options,
                                    "input": input_hint}
        self._update_step(step_pk, status="waiting", details=None)
        self.vtn.bus.publish("run.prompt", run_pk=rc.run_pk, step_pk=step_pk, message=message, options=options)
        try:
            if timeout:
                return await asyncio.wait_for(future, rc.scaled(timeout))
            return await future
        except asyncio.TimeoutError:
            return options[-1] if "Skip" not in options else "Skip", "timed out"
        finally:
            self._prompts.pop(rc.run_pk, None)
            self._update_step(step_pk, status="running", details=None)

    # ---- persistence helpers

    def _create_step(self, run_pk: int, index: int, name: str) -> int:
        with self.vtn.db.session() as s:
            step = models.TestStep(run_pk=run_pk, index=index, name=name, status="running", started_at=utcnow(),
                                   details={"assertions": []})
            s.add(step)
            s.flush()
            return step.id

    def _update_step(self, step_pk: int, status: Optional[str] = None, finished: bool = False, details=None):
        with self.vtn.db.session() as s:
            step = s.get(models.TestStep, step_pk)
            if step is None:
                return
            if status:
                step.status = status
            if finished:
                step.finished_at = utcnow()
            if details is not None:
                step.details = jsonable({**(step.details or {}), **details})

    def _add_finding(self, rc: RunContext, step_pk: int, severity: str, code: str, message: str, evidence):
        with self.vtn.db.session() as s:
            s.add(models.Finding(ven_pk=rc.ven_pk, run_pk=rc.run_pk, step_pk=step_pk, severity=severity, code=code,
                                 message=message, evidence=jsonable(evidence) if evidence else None))


def mean(values: list) -> Optional[float]:
    values = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return statistics.fmean(values) if values else None
