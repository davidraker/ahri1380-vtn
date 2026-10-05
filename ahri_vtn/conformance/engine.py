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

from sqlalchemy import select

from .. import spec
from ..db import models
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
    def __init__(self, engine: "ConformanceEngine", run_pk: int, scenario: Scenario, ven_pk: int, mode: str,
                 params: dict):
        self.engine = engine
        self.vtn = engine.vtn
        self.run_pk = run_pk
        self.scenario = scenario
        self.ven_pk = ven_pk
        self.mode = mode
        self.params = params
        self.step_index = 0
        self.created_event_ids: list[str] = []
        self.shared: dict = {}      # scratch space shared between steps / sub-scenarios

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

    def ven(self) -> dict:
        ven = self.vtn.queries.get_ven(self.ven_pk)
        if ven is None:
            raise RunAborted("VEN was deleted")
        return ven

    def ven_row(self) -> models.Ven:
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, self.ven_pk)
            s.expunge(ven)
            return ven

    @property
    def ven_id(self) -> Optional[str]:
        return self.ven_row().ven_id

    def latest_status(self) -> dict:
        """{rid name: value} from the VEN's latest status cache."""
        latest = self.ven_row().latest_status or {}
        return {k: v.get("value") for k, v in latest.items()}

    def static_value(self, rid: str):
        info = self.ven_row().static_info or {}
        return (info.get(rid) or {}).get("value")

    def rated_power(self) -> Optional[float]:
        value = self.static_value("Size")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def unloading_type(self) -> Optional[int]:
        value = self.static_value("Unloading_Type")
        try:
            return int(float(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    def samples(self, rid: str, since: datetime, until: Optional[datetime] = None) -> list[models.ReportSample]:
        return self.vtn.queries.samples(self.ven_pk, spec_rid=rid, since=since, until=until, limit=10000)

    def samples_received_since(self, rid: str, since: datetime) -> list[models.ReportSample]:
        with self.vtn.db.session() as s:
            return s.scalars(select(models.ReportSample)
                             .where(models.ReportSample.ven_pk == self.ven_pk, models.ReportSample.spec_rid == rid,
                                    models.ReportSample.received_at >= since)
                             .order_by(models.ReportSample.received_at)).all()

    def active_status_request(self) -> Optional[models.ReportRequest]:
        with self.vtn.db.session() as s:
            return s.scalar(select(models.ReportRequest)
                            .where(models.ReportRequest.ven_pk == self.ven_pk,
                                   models.ReportRequest.spec_name == spec.STATUS_REPORT.name,
                                   models.ReportRequest.one_shot.is_(False),
                                   models.ReportRequest.status.in_(("queued", "created", "active")))
                            .order_by(models.ReportRequest.requested_at.desc()))

    def event_responses(self, event_id: str) -> list[models.EventResponse]:
        return self.vtn.queries.event_responses(event_id)

    def opts_since(self, since: datetime) -> list[models.Opt]:
        return self.vtn.queries.opts(ven_pk=self.ven_pk, since=since)

    # ---- actions

    def create_event(self, preset: str, duration_seconds: float, start_offset_seconds: float = 0, **kwargs) -> str:
        start = utcnow() + timedelta(seconds=self.scaled(start_offset_seconds)) if start_offset_seconds else utcnow()
        event_id = self.vtn.events.create_event(self.ven_pk, preset, start=start,
                                                duration=timedelta(seconds=self.scaled(duration_seconds)),
                                                source="scenario", test_event=bool(self.p("test_event", False)),
                                                vtn_comment=f"AHRI 1380 {self.scenario.id} run {self.run_pk}", **kwargs)
        self.created_event_ids.append(event_id)
        return event_id

    def cancel_event(self, event_id: str) -> bool:
        return self.vtn.events.cancel_event(event_id)

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

    async def wait_samples(self, rid: str, count: int, since: datetime, timeout: float) -> list[models.ReportSample]:
        """Wait until `count` samples of rid with ts >= since have been received (or timeout)."""
        deadline = asyncio.get_event_loop().time() + self.scaled(timeout)
        while True:
            rows = self.samples(rid, since)
            if len(rows) >= count:
                return rows
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return rows
            await self.vtn.bus.wait_for(lambda e: e["topic"] == "report.update" and e.get("ven_pk") == self.ven_pk,
                                        timeout=remaining)

    async def wait_status(self, predicate: Callable[[dict], bool], timeout: float) -> Optional[dict]:
        """Wait for the latest status (dict rid -> value) to satisfy predicate."""
        status = self.latest_status()
        if status and predicate(status):
            return status
        deadline = asyncio.get_event_loop().time() + self.scaled(timeout)
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            await self.vtn.bus.wait_for(lambda e: e["topic"] == "report.update" and e.get("ven_pk") == self.ven_pk,
                                        timeout=remaining)
            status = self.latest_status()
            if status and predicate(status):
                return status

    async def wait_opt(self, since: datetime, timeout: float, opt_type: Optional[str] = "optOut") -> Optional[models.Opt]:
        def existing():
            for o in self.opts_since(since):
                if opt_type is None or o.opt_type == opt_type:
                    return o
            return None
        if existing():
            return existing()
        await self.vtn.bus.wait_for(lambda e: e["topic"] == "opt.created" and e.get("ven_pk") == self.ven_pk
                                    and (opt_type is None or e.get("opt_type") == opt_type), timeout=self.scaled(timeout))
        return existing()

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

    def start_run(self, scenario_id: str, ven_pk: Optional[int], mode: str = "cooling", params: Optional[dict] = None,
                  name: Optional[str] = None) -> int:
        if scenario_id not in self.scenarios:
            raise ValueError(f"Unknown scenario '{scenario_id}'")
        if ven_pk is None:
            raise ValueError("A VEN must be selected")
        scenario = self.scenarios[scenario_id]
        merged = {**self.default_params(), **scenario.default_params, **(params or {})}
        for key in ("operator_prompts", "wattmeter_prompt", "test_event"):
            if isinstance(merged.get(key), str):
                merged[key] = merged[key].strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(merged.get(key), (int, float)):
                merged[key] = bool(merged[key])
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, ven_pk)
            if ven is None:
                raise ValueError("Unknown VEN")
            if not ven.registered or not ven.ven_id:
                raise ValueError(f"VEN '{ven.ven_name}' is not registered")
            run = models.TestRun(scenario_id=scenario_id, name=name or f"{scenario.title} - {ven.ven_name}", ven_pk=ven_pk,
                                 mode=mode, params=jsonable(merged), status="running")
            s.add(run)
            s.flush()
            run_pk = run.id
        rc = RunContext(self, run_pk, scenario, ven_pk, mode, merged)
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
                           "rated_power": rc.rated_power(), "unloading_type": rc.unloading_type(), "mode": rc.mode}
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
