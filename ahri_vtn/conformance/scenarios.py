"""
AHRI 1380 conformance scenarios (Appendix C.3.4 / C.3.5 for OpenADR 2.0b, Tables 4-8).

Each scenario is a coroutine taking a RunContext. A run targets a *selection*: whole VENs,
individual resources, saved groups, or any mix. Events go to every VEN in the selection (one
OpenADR event per VEN, grouped in a dispatch); response checks are per VEN; state, power,
override and static checks are per *unit* (a VEN/resource pair, or the VEN itself when it reports
no resource ids). A step fails if any unit fails.

Timings come from the run parameters (defaulting to the configured thresholds) multiplied by
`time_scale`, so the same scenarios run in seconds against the simulator and in real time
against lab equipment.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from openleadr import utils as oadr_utils
from sqlalchemy import select

from .. import spec
from ..db import models
from ..targets import Unit
from ..util import utcnow
from .engine import RunContext, Scenario, StepContext, mean


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------

def _int(value) -> Optional[int]:
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _wait_received_all(rc: RunContext, rid: str, count: int, since: datetime, timeout: float) -> dict:
    """Wait until every unit has `count` samples of rid received after `since`; {unit.key: rows}."""
    deadline = asyncio.get_event_loop().time() + rc.scaled(timeout)
    results = {}
    for unit in rc.units:
        remaining = max(deadline - asyncio.get_event_loop().time(), 0)
        results[unit.key] = await rc.wait_unit_samples(unit, rid, count, since, remaining / max(rc.scaled(1), 1e-9))
    return results


async def _ensure_status_streams(rc: RunContext, sc: StepContext) -> float:
    """Make sure a periodic x-AHRI_STATUS report is being delivered for every unit; returns the interval."""
    interval = rc.vtn.config.status_report_interval_seconds
    missing = [u for u in rc.units if await rc.unit_active_status_request(u) is None]
    if missing:
        for target in rc.targets:
            units_missing = [u for u in missing if u.ven_pk == target.ven_pk]
            if not units_missing:
                continue
            try:
                if target.whole_ven:
                    await rc.run(rc.vtn.reports.request_reports, target.as_dict() and {"vens": [{"ven_pk": target.ven_pk}]},
                                                   spec.STATUS_REPORT.name, one_shot=False, source="scenario")
                else:
                    await rc.run(rc.vtn.reports.request_reports, {"vens": [{"ven_pk": target.ven_pk,
                                                              "resource_ids": [u.resource_id for u in units_missing
                                                                               if u.resource_id]}]},
                                                   spec.STATUS_REPORT.name, one_shot=False, source="scenario")
                sc.info(f"No periodic x-AHRI_STATUS request active for {', '.join(u.label for u in units_missing)}; requested one")
            except ValueError as err:
                sc.require(False, f"x-AHRI_STATUS cannot be requested for {target.ven_name}: {err}")
    for unit in rc.units:
        rr = await rc.unit_active_status_request(unit)
        if rr is not None and rr.granularity_seconds:
            interval = rr.granularity_seconds
            break
    since = utcnow() - timedelta(seconds=rc.scaled(interval) * 1.5)
    rows = await _wait_received_all(rc, "Operational State", 1, since, max(interval * 2.5, 30))
    for unit in rc.units:
        sc.require(len(rows[unit.key]) >= 1, f"{rc.tag(unit)}x-AHRI_STATUS samples are arriving",
                   {"interval_seconds": interval})
    return interval


async def _fetch_static(rc: RunContext, sc: StepContext, units: list[Unit]):
    """Request x-AHRI_STATIC once so Size / Unloading_Type are known for the given units."""
    t0 = utcnow()
    try:
        result = await rc.request_reports(spec.STATIC_REPORT.name, one_shot=True, test_step_pk=sc.step_pk)
    except ValueError as err:
        sc.info(f"x-AHRI_STATIC not available ({err}); rated power unknown")
        return
    for error in result["errors"]:
        sc.info(error)
    sc.info(f"Rated power unknown for {', '.join(u.label for u in units)}; requested x-AHRI_STATIC first")
    timeout = float(rc.p("report_timeout_seconds", 180))
    deadline = asyncio.get_event_loop().time() + rc.scaled(timeout)
    for unit in units:
        remaining = max(deadline - asyncio.get_event_loop().time(), 0)
        await rc.wait_unit_samples(unit, "Size", 1, t0, remaining / max(rc.scaled(1), 1e-9))
    await rc.refresh_units()


async def _clear_other_events(rc: RunContext, sc: StepContext):
    """Other live events touching the selected units would confound the measurement; cancel (or report) them."""
    live = []
    for target in rc.targets:
        for e in await rc.run(rc.vtn.queries.list_events, ven_pk=target.ven_pk, limit=50):
            if e["status"] not in ("far", "near", "active") or e["event_id"] in rc.created_event_ids:
                continue
            if target.whole_ven or e["target_resource_ids"] is None \
                    or set(e["target_resource_ids"]) & set(target.resource_ids):
                live.append(e)
    if not live:
        return
    ids = [e["event_id"][:8] for e in live]
    if rc.p("cancel_other_events", True):
        for e in live:
            await rc.cancel_event(e["event_id"])
        sc.warn(f"Cancelled {len(live)} other live event(s) touching the selected units before the test: {ids}")
        await sc.sleep(float(rc.p("poll_interval_seconds", 10)) * 2, "waiting for the cancellations to be delivered")
    else:
        sc.warn(f"Other live event(s) may confound the result: {ids}")


def _heating_manual_checks(rc: RunContext, sc: StepContext, level_name: str):
    if rc.mode != "heating":
        return
    sc.manual(f"Heating mode ({level_name}): verify that electric resistance heat stays off unless the indoor "
              "temperature is 10 F below setpoint (Table 8); with dual fuel the unit may switch to the secondary heat source.")


def _check_responses(rc: RunContext, sc: StepContext, responses: dict, timeout: float, expect_opt_in: bool = True,
                     label: str = "oadrCreatedEvent") -> dict:
    """Assert one response per VEN; returns {ven_pk: response}."""
    for target in rc.targets:
        resp = responses.get(target.ven_pk)
        prefix = f"[{target.ven_name}] " if len(rc.targets) > 1 else ""
        if not sc.check(resp is not None, f"{prefix}{label} received within {timeout:.0f}s"):
            continue
        if expect_opt_in:
            sc.check(resp.opt_type == "optIn", f"{prefix}VEN opted in: {resp.opt_type} (6.1.3-6.1.5 'indicate participation')")
            sc.check(resp.response_code == 200, f"{prefix}Response code {resp.response_code}")
    return responses


# ---------------------------------------------------------------------------------------------
# TC-REG / TC-META / TC-STATIC / TC-STATUS
# ---------------------------------------------------------------------------------------------

async def tc_reg(rc: RunContext):
    async with rc.step("TC-REG Utility Setup: oadrCreatePartyRegistration (Table 4)") as sc:
        for target in rc.targets:
            ven = await rc.ven(target.ven_pk)
            p = f"[{ven['ven_name']}] " if len(rc.targets) > 1 else ""
            sc.require(ven["registered"], f"{p}VEN '{ven['ven_name']}' is registered", {"ven_id": ven["ven_id"]})
            sc.check(ven["profile_name"] == "2.0b", f"{p}oadrProfileName = {ven['profile_name']!r} (expected '2.0b')")
            sc.check(ven["transport_name"] == "simpleHttp",
                     f"{p}oadrTransportName = {ven['transport_name']!r} (expected 'simpleHttp')")
            sc.check(ven["http_pull_model"] is True, f"{p}oadrHttpPullModel = {ven['http_pull_model']} (expected true)")
            sc.check(not ven["xml_signature"], f"{p}oadrXmlSignature = {ven['xml_signature']} (expected false)",
                     severity="warning")
            sc.check(not ven["report_only"], f"{p}oadrReportOnly = {ven['report_only']} (expected false)")
            sc.check(bool(ven["ven_id"]) and bool(ven["registration_id"]), f"{p}VTN issued venID and registrationID",
                     {"ven_id": ven["ven_id"], "registration_id": ven["registration_id"]})
            sc.check(ven["online"], f"{p}VEN is polling (last poll {ven['last_poll_at']})", severity="warning")
            polls = await rc.run(_recent_poll_times, rc, ven["ven_id"])
            if len(polls) >= 3:
                diffs = [(polls[i] - polls[i + 1]).total_seconds() for i in range(len(polls) - 1)]
                avg = mean(diffs)
                requested = rc.vtn.config.requested_poll_freq_seconds
                sc.check(avg <= requested * 1.5 + 2, f"{p}Average poll interval {avg:.1f}s (requested {requested}s)",
                         {"intervals": [round(d, 1) for d in diffs]}, severity="warning")
            else:
                sc.info(f"{p}Only {len(polls)} oadrPoll messages recorded; poll cadence not assessed")
            if ven["resource_count"]:
                sc.info(f"{p}VEN exposes {ven['resource_count']} resource(s): {', '.join(ven['resource_ids'])}")


def _recent_poll_times(rc: RunContext, ven_id: str) -> list[datetime]:
    with rc.vtn.db.session() as s:
        return s.scalars(select(models.Message.ts).where(models.Message.ven_id == ven_id,
                                                          models.Message.message_type == "oadrPoll",
                                                          models.Message.direction == "in")
                         .order_by(models.Message.ts.desc()).limit(12)).all()


async def _reports_for_unit(rc: RunContext, unit: Unit) -> list[dict]:
    ven = await rc.ven(unit.ven_pk)
    active = [r for r in ven["registered_reports"] if r["active"]]
    if unit.resource_id is None:
        return active
    return [r for r in active if any(d["resource_id"] == unit.resource_id for d in r["descriptions"])]


async def tc_meta(rc: RunContext):
    async with rc.step("TC-META Report registration: oadrRegisterReport (Tables 5 and 6)") as sc:
        for unit in rc.units:
            p = rc.tag(unit)
            active = await _reports_for_unit(rc, unit)
            sc.info(f"{p}{len(active)} registered report(s): " + ", ".join(
                f"{r['report_name_raw']} [{r['report_specifier_id']}]" for r in active))
            for rspec in (spec.STATIC_REPORT, spec.STATUS_REPORT):
                matches = [r for r in active if r["spec_name"] == rspec.name]
                if not sc.check(bool(matches), f"{p}{rspec.name} is registered"):
                    continue
                rr = matches[0]
                base = oadr_utils.strip_metadata_prefix(rr["report_name_raw"])
                sc.check(base == rspec.name, f"{p}{rspec.name} registered under reportName '{rr['report_name_raw']}'",
                         severity="warning")
                for row in rr["checklist"] or []:
                    if not row["rid"]:
                        sc.info(f"{p}{rspec.name}: extra rID '{row['r_id']}' not in the table")
                        continue
                    offered = f" (offered as '{row['r_id']}')" if row["present"] and row["r_id"] != row["rid"] else ""
                    if not sc.check(row["present"], f"{p}{rspec.name} rID '{row['rid']}' offered{offered}"):
                        continue
                    sc.check((row["report_type"] or "").lower() == row["expected_type"].lower(),
                             f"{p}'{row['rid']}' reportType '{row['report_type']}' (table: {row['expected_type']})",
                             severity="warning")
                    sc.check((row["reading_type"] or "").lower() == row["expected_reading"].lower(),
                             f"{p}'{row['rid']}' readingType '{row['reading_type']}' (table: {row['expected_reading']})",
                             severity="warning")
                    if row["expected_units"]:
                        sc.info(f"{p}'{row['rid']}' units/description: {row['unit']!r} / {row['measurement_description']!r} "
                                f"(table: {row['expected_units']})")
                    if rspec.sampling_interval is not None:
                        limit = rspec.sampling_interval.total_seconds()
                        sc.check(row["min_period"] is not None and row["min_period"] <= limit,
                                 f"{p}'{row['rid']}' minimum sampling period {row['min_period']}s allows the 1 minute interval")


async def tc_static(rc: RunContext):
    async with rc.step("TC-STATIC Request and verify x-AHRI_STATIC (Table 5)") as sc:
        t0 = utcnow()
        try:
            result = await rc.request_reports(spec.STATIC_REPORT.name, one_shot=True, test_step_pk=sc.step_pk)
        except ValueError as err:
            sc.require(False, f"Could not request x-AHRI_STATIC: {err}")
        for error in result["errors"]:
            sc.warn(error)
        sc.info(f"Requested one-shot x-AHRI_STATIC ({len(result['report_request_ids'])} request(s)); "
                "delivered with oadrCreateReport on the next poll")
        timeout = rc.p("report_timeout_seconds", 180)
        rows = await _wait_received_all(rc, "Size", 1, t0, timeout)
        await rc.refresh_units()
        for unit in rc.units:
            p = rc.tag(unit)
            if not sc.check(len(rows.get(unit.key, [])) >= 1, f"{p}x-AHRI_STATIC delivered within {timeout}s"):
                continue
            for rid in spec.STATIC_REPORT.rids:
                value = await rc.unit_static(unit, rid.name)
                meaning = f" = {rid.describe_value(value)}" if rid.enum and value is not None else ""
                sc.check(value is not None, f"{p}'{rid.name}' = {value!r}{meaning}")
                if rid.value_kind == "text" and value is not None and not isinstance(value, str):
                    sc.info(f"{p}'{rid.name}' was sent as a number ({value}); ASCII values cannot be carried in payloadFloat")
                if rid.value_kind == "text" and isinstance(value, str):
                    sc.info(f"{p}'{rid.name}' sent as text inside payloadFloat (schema-invalid but recorded)")
            size = await rc.unit_rated_power(unit)
            sc.check(size is not None and size > 0, f"{p}Size (rated load power) = {size} W is positive")
            ut = await rc.unit_unloading_type(unit)
            sc.check(ut in (0, 1), f"{p}Unloading_Type = {ut} ({spec.UNLOADING_TYPES.get(ut, 'invalid')})")
            dt = _int(await rc.unit_static(unit, "Device_Type"))
            sc.check(dt in (0, 1), f"{p}Device_Type = {dt} ({spec.DEVICE_TYPES.get(dt, 'invalid')})")
            for rid in ("Benchmark_Power_Cooling", "Benchmark_Power_Heating"):
                v = _float(await rc.unit_static(unit, rid))
                sc.check(v is not None and v > 0, f"{p}{rid} = {v} W", severity="warning")


async def tc_status(rc: RunContext):
    async with rc.step("TC-STATUS Periodic x-AHRI_STATUS telemetry (Table 6 / 6.1.9-6.1.11)") as sc:
        interval = await _ensure_status_streams(rc, sc)
        n = int(rc.p("status_samples", 3))
        timeout = interval * (n + 1) * 1.5
        t0 = utcnow()
        rows_all = await _wait_received_all(rc, "Operational State", n, t0, timeout)
        expected = rc.scaled(interval)
        tol = float(rc.p("status_interval_tolerance", 0.5))
        for unit in rc.units:
            p = rc.tag(unit)
            rows = rows_all[unit.key]
            if not sc.check(len(rows) >= n, f"{p}Received {len(rows)}/{n} status samples within {timeout:.0f}s"):
                continue
            ts = [r.ts for r in rows]
            diffs = [(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)]
            if diffs:
                sc.check(all(abs(d - expected) <= expected * tol + 1 for d in diffs),
                         f"{p}Sample spacing {[round(d) for d in diffs]}s (expected {expected:.0f}s +-{tol * 100:.0f}%)",
                         severity="warning")
            rated = await rc.unit_rated_power(unit)
            power = [r.value for r in await rc.unit_samples_received_since(unit, "Inst Power", t0)]
            states = [r.value for r in rows]
            overrides = [r.value for r in await rc.unit_samples_received_since(unit, "Override State", t0)]
            sc.check(all(isinstance(v, (int, float)) and v >= 0 for v in power) and bool(power),
                     f"{p}Inst Power values numeric and >= 0: {[round(v, 1) if isinstance(v, (int, float)) else v for v in power]}")
            if rated:
                sc.check(all(isinstance(v, (int, float)) and v <= rated * 1.5 for v in power),
                         f"{p}Inst Power <= 150% of rated {rated:.0f} W", severity="warning")
            sc.check(all(_int(v) in spec.OPERATIONAL_STATES for v in states),
                     f"{p}Operational State values valid Table 7 codes: {[_int(v) for v in states]} "
                     f"({', '.join(sorted({spec.operational_state_name(v) for v in states}))})")
            sc.check(all(_int(v) in (0, 1) for v in overrides) and bool(overrides),
                     f"{p}Override State values in {{0, 1}}: {[_int(v) for v in overrides]}")


# ---------------------------------------------------------------------------------------------
# Curtailment events (Table 8, Appendix C.3.5)
# ---------------------------------------------------------------------------------------------

async def _override_phase(rc: RunContext, sc_title: str, dispatch: dict, fraction: Optional[float], units: list[Unit]):
    p = rc.params
    async with rc.step(f"{sc_title}: customer override during the event (6.1.10, C.3.5 b)") as sc:
        t0 = utcnow()
        names = ", ".join(u.label for u in units)
        answer = await sc.prompt(f"Initiate a customer override at the equipment user interface of {names} now, "
                                 "then press Continue.", options=("Continue", "Skip", "Abort"))
        if answer == "Skip":
            sc.manual("Customer override was not performed by the operator")
            return
        timeout = float(p.get("override_timeout_seconds", 600))
        deadline = asyncio.get_event_loop().time() + rc.scaled(timeout)
        for unit in units:
            tag = rc.tag(unit)
            remaining = max(deadline - asyncio.get_event_loop().time(), 0) / max(rc.scaled(1), 1e-9)
            opt = await rc.wait_unit_opt(unit, t0, remaining)
            sc.check(opt is not None, f"{tag}oadrCreateOpt received within {timeout:.0f}s of the override",
                     {"opt_id": opt.opt_id if opt else None})
            if opt is not None:
                sc.check(opt.opt_type == "optOut", f"{tag}optType = {opt.opt_type} (expected optOut)")
                sc.check(opt.opt_reason == "overrideStatus", f"{tag}optReason = {opt.opt_reason} (expected overrideStatus)",
                         severity="warning")
                event_id = dispatch["events"].get(unit.ven_pk)
                sc.check(opt.event_id == event_id, f"{tag}Opt references event {str(opt.event_id)[:8]} "
                         f"(active event {(event_id or '')[:8]})", severity="warning")
                if unit.resource_id and opt.resource_id:
                    sc.check(opt.resource_id == unit.resource_id,
                             f"{tag}Opt target resourceID {opt.resource_id} matches the unit", severity="warning")
            remaining = max(deadline - asyncio.get_event_loop().time(), 0) / max(rc.scaled(1), 1e-9)
            status = await rc.wait_unit_status(unit, lambda st: _int(st.get("Override State")) == 1, remaining)
            sc.check(status is not None, f"{tag}Override State reported as 1")
            if status is not None:
                op = _int(status.get("Operational State"))
                sc.check(op in spec.OPTED_OUT_STATES,
                         f"{tag}Operational State {op} ({spec.operational_state_name(op)}) is opted out (11/12)",
                         severity="warning")
            rated = await rc.unit_rated_power(unit)
            if rated and fraction:
                recovered = await rc.wait_unit_status(unit, lambda st: (_float(st.get("Inst Power")) or 0) > fraction * rated,
                                                      float(p.get("recovery_timeout_seconds", 300)))
                sc.check(recovered is not None, f"{tag}Reported power rose above the {fraction * 100:.0f}% limit after the override",
                         severity="warning")
        sc.manual("Verify with the wattmeter that the equipment returned to normal operation after the override")


async def _curtailment(rc: RunContext, preset_id: str, title: str, fraction: Optional[float], expected_states: tuple,
                       off_mode: bool = False, with_override: bool = False):
    p = rc.params
    stab = float(p.get("stabilization_seconds", 120))
    n = int(p.get("sample_count", 20))
    interval = float(p.get("sample_interval_seconds", 60))
    response_timeout = float(p.get("response_timeout_seconds", 60))
    duration = stab + (n + 2) * interval + (float(p.get("override_timeout_seconds", 600)) + 120 if with_override else 60)
    dispatch = None

    async with rc.step(f"{title}: send oadrDistributeEvent and check oadrCreatedEvent") as sc:
        await _ensure_status_streams(rc, sc)
        await _clear_other_events(rc, sc)
        unknown = [u for u in rc.units if await rc.unit_rated_power(u) is None]
        if unknown:
            await _fetch_static(rc, sc, unknown)
        for unit in rc.units:
            if await rc.unit_rated_power(unit) is None:
                sc.warn(f"{rc.tag(unit)}Rated load power (Size) unknown; power fractions can only be judged by the lab wattmeter")
            sc.info(f"{rc.tag(unit)}Baseline status before the event: {await rc.unit_status(unit)}")
        # A VEN NAKs Critical Curtailment when all its selected units are two-capacity systems
        nak_vens = set()
        if preset_id == "critical_curtailment":
            for target in rc.targets:
                types = {await rc.unit_unloading_type(u) for u in rc.units if u.ven_pk == target.ven_pk}
                if types and types == {1}:
                    nak_vens.add(target.ven_pk)
        rc.shared["nak_vens"] = nak_vens
        dispatch = await rc.dispatch(preset_id, duration)
        rc.shared["dispatch"] = dispatch
        preset = spec.EVENT_PRESETS[preset_id]
        sc.info(f"Sent {preset.signal_name}:{preset.signal_type} = {preset.payload} ({preset.label}), "
                f"duration {rc.scaled(duration):.0f}s, to {'; '.join(t.describe() for t in rc.targets)} "
                f"(events {[e[:8] for e in dispatch['events'].values()]})")
        responses = await rc.wait_dispatch_responses(dispatch, response_timeout, modification_number=0)
        for target in rc.targets:
            resp = responses.get(target.ven_pk)
            prefix = f"[{target.ven_name}] " if len(rc.targets) > 1 else ""
            if not sc.check(resp is not None, f"{prefix}oadrCreatedEvent received within {response_timeout:.0f}s"):
                continue
            if target.ven_pk in nak_vens:
                sc.check(resp.opt_type == "optOut" or resp.response_code == 460,
                         f"{prefix}Two-capacity unit declines Critical Curtailment: {resp.opt_type} / {resp.response_code}")
                sc.check(resp.response_code == 460, f"{prefix}Response code {resp.response_code} "
                         f"({spec.RESPONSE_CODES.get(resp.response_code, '?')}); Table 8 specifies 460 Signal not supported",
                         severity="warning")
            else:
                sc.check(resp.opt_type == "optIn", f"{prefix}VEN opted in: {resp.opt_type} (6.1.3-6.1.5 'indicate participation')")
                sc.check(resp.response_code == 200, f"{prefix}Response code {resp.response_code}")

    nak_vens = rc.shared.get("nak_vens", set())
    active_units = [u for u in rc.units if u.ven_pk not in nak_vens]
    if nak_vens:
        async with rc.step(f"{title}: verify two-capacity units take no action") as sc:
            await sc.sleep(min(stab, 120), "waiting after NAK")
            for unit in [u for u in rc.units if u.ven_pk in nak_vens]:
                op = _int((await rc.unit_status(unit)).get("Operational State"))
                sc.check(op not in spec.CURTAILED_STATES,
                         f"{rc.tag(unit)}Operational State {op} ({spec.operational_state_name(op)}) not curtailed")
        if not active_units:
            await rc.cancel_dispatch(dispatch)
            return dispatch

    async with rc.step(f"{title}: {stab:.0f}s stabilization then {n} samples at {interval:.0f}s (Appendix C.2)") as sc:
        await sc.sleep(stab, "stabilization")
        t_start = utcnow()
        timeout = (n + 1) * interval * 1.5
        deadline = asyncio.get_event_loop().time() + rc.scaled(timeout)
        measured_prompt_units = []
        for unit in active_units:
            tag = rc.tag(unit)
            remaining = max(deadline - asyncio.get_event_loop().time(), 0) / max(rc.scaled(1), 1e-9)
            power_rows = await rc.wait_unit_samples(unit, "Inst Power", n, t_start, remaining)
            state_rows = await rc.unit_samples_received_since(unit, "Operational State", t_start)
            sc.check(len(power_rows) >= n, f"{tag}Collected {len(power_rows)}/{n} power samples in {timeout:.0f}s")
            values = [r.value for r in power_rows if isinstance(r.value, (int, float))]
            avg = mean(values)
            sc.info(f"{tag}Reported power samples (W): {[round(v) for v in values]}")
            rated = await rc.unit_rated_power(unit)
            if avg is not None:
                if off_mode:
                    limit = float(p.get("off_mode_max_watts", 200))
                    sc.check(avg <= limit, f"{tag}Mean reported power {avg:.0f} W <= {limit:.0f} W (controls + crankcase heater allowance)")
                elif rated and fraction:
                    limit = fraction * rated
                    sc.check(avg <= limit, f"{tag}Mean reported power {avg:.0f} W <= {fraction * 100:.0f}% of rated {rated:.0f} W ({limit:.0f} W)")
                else:
                    sc.info(f"{tag}Mean reported power {avg:.0f} W (rated power unknown)")
            op_values = sorted({_int(r.value) for r in state_rows if r.value is not None})
            sc.check(bool(op_values) and set(op_values) <= set(expected_states),
                     f"{tag}Operational State(s) during the event {op_values} "
                     f"({', '.join(spec.operational_state_name(v) for v in op_values)}) within expected {list(expected_states)}")
            measured_prompt_units.append((unit, rated))
        _heating_manual_checks(rc, sc, title)
        if p.get("wattmeter_prompt", True):
            for unit, rated in measured_prompt_units:
                tag = rc.tag(unit)
                answer = await sc.prompt(f"Enter the wattmeter average input power (W) for {unit.label} over the {n} "
                                         "one-minute samples, or Skip if the lab records it separately.",
                                         options=("Submit", "Skip", "Abort"), input_hint="measured watts")
                measured = _float(sc.note) if answer == "Submit" else None
                if measured is not None:
                    if off_mode:
                        sc.check(measured <= float(p.get("off_mode_max_watts", 200)),
                                 f"{tag}Measured {measured:.0f} W within off-mode allowance")
                    elif rated and fraction:
                        sc.check(measured <= fraction * rated,
                                 f"{tag}Measured {measured:.0f} W <= {fraction * 100:.0f}% of rated ({fraction * rated:.0f} W)")
                    else:
                        sc.manual(f"{tag}Measured {measured:.0f} W recorded; compare against the rated load power from the test report")
                else:
                    sc.manual(f"{tag}Wattmeter result to be recorded by the lab (Appendix C.2: twenty one-minute measurements averaged)")
        else:
            sc.manual("Wattmeter result to be recorded by the lab (Appendix C.2)")
        sc.manual("Continuously variable equipment: verify the 5 second linear power ramp (Section 6.1.13.1, C.4)")

    if with_override:
        await _override_phase(rc, title, dispatch, fraction, active_units)

    async with rc.step(f"{title}: end the event (CANCELLED) and verify return to normal (6.1.7)") as sc:
        await rc.cancel_dispatch(dispatch)
        responses = await rc.wait_dispatch_responses(dispatch, response_timeout, modification_number=1)
        for target in rc.targets:
            if target.ven_pk in nak_vens:
                continue
            resp = responses.get(target.ven_pk)
            prefix = f"[{target.ven_name}] " if len(rc.targets) > 1 else ""
            sc.check(resp is not None and resp.opt_type == "optIn",
                     f"{prefix}VEN acknowledged the CANCELLED event: {resp.opt_type if resp else 'no response'}")
        timeout = float(p.get("recovery_timeout_seconds", 300))
        deadline = asyncio.get_event_loop().time() + rc.scaled(timeout)
        for unit in active_units:
            remaining = max(deadline - asyncio.get_event_loop().time(), 0) / max(rc.scaled(1), 1e-9)
            status = await rc.wait_unit_status(unit, lambda st: _int(st.get("Operational State")) in spec.NORMAL_STATES, remaining)
            op = _int((status or await rc.unit_status(unit)).get("Operational State"))
            sc.check(status is not None, f"{rc.tag(unit)}Operational State back to normal (0/1) within {timeout:.0f}s: now {op} "
                     f"({spec.operational_state_name(op)})")
    return dispatch


async def tc_gc(rc: RunContext):
    await _curtailment(rc, "general_curtailment", "TC-GC General Curtailment (6.1.3)", float(rc.p("general_fraction", 0.7)),
                       spec.CURTAILED_STATES)


async def tc_cc(rc: RunContext):
    await _curtailment(rc, "critical_curtailment", "TC-CC Critical Curtailment (6.1.4)", float(rc.p("critical_fraction", 0.4)),
                       spec.CURTAILED_STATES, with_override=bool(rc.p("override_in_critical", True)))


async def tc_fc(rc: RunContext):
    await _curtailment(rc, "full_curtailment", "TC-FC Full Curtailment / Grid Emergency (6.1.5)", None, (4,), off_mode=True)


async def tc_override(rc: RunContext):
    await _curtailment(rc, "general_curtailment", "TC-OVERRIDE General Curtailment with customer override",
                       float(rc.p("general_fraction", 0.7)), spec.CURTAILED_STATES, with_override=True)


# ---------------------------------------------------------------------------------------------
# Cancel, MITO, price, re-registration
# ---------------------------------------------------------------------------------------------

async def tc_cancel_future(rc: RunContext):
    p = rc.params
    timeout = float(p.get("response_timeout_seconds", 60))
    async with rc.step("TC-CANCEL-FUTURE Schedule and cancel a future event (6.1.7)") as sc:
        offset = float(p.get("future_offset_seconds", 600))
        dispatch = await rc.dispatch("general_curtailment", 600, start_offset_seconds=offset)
        responses = await rc.wait_dispatch_responses(dispatch, timeout, modification_number=0)
        _check_responses(rc, sc, responses, timeout, label=f"Future event (starts in {rc.scaled(offset):.0f}s) acknowledged:")
        before = {u.key: _int((await rc.unit_status(u)).get("Operational State")) for u in rc.units}
        await rc.cancel_dispatch(dispatch)
        responses = await rc.wait_dispatch_responses(dispatch, timeout, modification_number=1)
        for target in rc.targets:
            resp = responses.get(target.ven_pk)
            prefix = f"[{target.ven_name}] " if len(rc.targets) > 1 else ""
            sc.check(resp is not None, f"{prefix}CANCELLED modification acknowledged: {resp.opt_type if resp else 'no response'}")
        for unit in rc.units:
            after = _int((await rc.unit_status(unit)).get("Operational State"))
            sc.check(after not in spec.CURTAILED_STATES,
                     f"{rc.tag(unit)}No curtailment occurred (Operational State {before[unit.key]} -> {after})")


async def tc_mito(rc: RunContext):
    p = rc.params
    timeout = float(p.get("response_timeout_seconds", 60))
    mito = int(float(p.get("mito", 3)))
    async with rc.step("TC-MITO Maximum Indoor Temperature Offset event (6.1.2)") as sc:
        sc.check(1 <= mito <= int(float(p.get("max_mito", 4))), f"MITO {mito} F within the valid range (<= 4 F)")
        dispatch = await rc.dispatch("mito", 300, payload=mito)
        responses = await rc.wait_dispatch_responses(dispatch, timeout, modification_number=0)
        _check_responses(rc, sc, responses, timeout, label=f"LOAD_CONTROL:x-loadControlLevelOffset = {mito} acknowledged:")
        await rc.cancel_dispatch(dispatch)
        await rc.wait_dispatch_responses(dispatch, timeout, modification_number=1)
    async with rc.step("TC-MITO Combined SIMPLE + LOAD_CONTROL event (Table 8 'two signals')") as sc:
        dispatch = await rc.dispatch("general_curtailment", 300, mito=mito)
        responses = await rc.wait_dispatch_responses(dispatch, timeout, modification_number=0)
        _check_responses(rc, sc, responses, timeout, label="Two-signal event acknowledged:")
        sc.manual(f"Verify the HVAC system applies the {mito} F offset while curtailing (indoor temperature within MITO)")
        await rc.cancel_dispatch(dispatch)
        responses = await rc.wait_dispatch_responses(dispatch, timeout, modification_number=1)
        for target in rc.targets:
            prefix = f"[{target.ven_name}] " if len(rc.targets) > 1 else ""
            sc.check(responses.get(target.ven_pk) is not None, f"{prefix}Cancellation acknowledged")


async def tc_price(rc: RunContext):
    p = rc.params
    timeout = float(p.get("response_timeout_seconds", 60))
    async with rc.step("TC-PRICE Price stream ELECTRICITY_PRICE:priceMultiplier (6.1.8)") as sc:
        n = int(float(p.get("price_intervals", 25)))
        minutes = float(p.get("price_interval_minutes", 60))
        multipliers = [round(1.0 + 0.1 * ((i * 7) % 5), 2) for i in range(n)]
        dispatch = await rc.dispatch("price_stream", 0, price_multipliers=multipliers, price_interval=timedelta(minutes=minutes))
        sc.info(f"Sent {n} price intervals of {minutes:.0f} min (multipliers {multipliers[:5]}...)")
        responses = await rc.wait_dispatch_responses(dispatch, timeout, modification_number=0)
        for target in rc.targets:
            resp = responses.get(target.ven_pk)
            prefix = f"[{target.ven_name}] " if len(rc.targets) > 1 else ""
            sc.check(resp is not None, f"{prefix}Price stream acknowledged: {resp.opt_type if resp else 'no response'} "
                     f"(6.1.8: capable of receiving >= 25 intervals)")
        sc.manual("Optional: verify on the equipment that the price stream was received / displayed")
        await rc.cancel_dispatch(dispatch)
        await rc.wait_dispatch_responses(dispatch, timeout, modification_number=1)


async def tc_rereg(rc: RunContext):
    p = rc.params
    async with rc.step("TC-REREG oadrRequestReregistration (Table 4)") as sc:
        timeout = float(p.get("poll_interval_seconds", 10)) * 4 + float(p.get("response_timeout_seconds", 60))
        for target in rc.targets:
            prefix = f"[{target.ven_name}] " if len(rc.targets) > 1 else ""
            ok = await rc.run(rc.vtn.registry.request_reregistration, target.ven_pk)
            if not sc.check(ok, f"{prefix}oadrRequestReregistration queued for the VEN's next poll"):
                continue
            ev = await rc.wait_topic(lambda e, pk=target.ven_pk: e["topic"] == "ven.registered" and e.get("ven_pk") == pk, timeout)
            sc.check(ev is not None, f"{prefix}VEN re-registered (oadrCreatePartyRegistration) within {timeout:.0f}s")
            ven = await rc.ven(target.ven_pk)
            sc.check(ven["registered"] and bool(ven["ven_id"]), f"{prefix}VEN registered again as venID {ven['ven_id']}")


# ---------------------------------------------------------------------------------------------
# Composite Appendix C sequences
# ---------------------------------------------------------------------------------------------

async def appendix_c(rc: RunContext):
    await tc_reg(rc)
    await tc_meta(rc)
    await tc_static(rc)
    await tc_status(rc)
    await _curtailment(rc, "general_curtailment", "C.3.5 (a) General Curtailment 30% reduction",
                       float(rc.p("general_fraction", 0.7)), spec.CURTAILED_STATES)
    await _curtailment(rc, "critical_curtailment", "C.3.5 (b) Critical Curtailment 60% reduction",
                       float(rc.p("critical_fraction", 0.4)), spec.CURTAILED_STATES, with_override=True)
    await _curtailment(rc, "full_curtailment", "C.3.5 (c) Off mode", None, (4,), off_mode=True)
    await tc_cancel_future(rc)
    await tc_mito(rc)
    await tc_price(rc)
    if rc.p("include_reregistration", True):
        await tc_rereg(rc)


ALL = [
    Scenario("appendix_c", "Appendix C full sequence (C.3.4 + C.3.5)", "Appendix C",
             "Registration and report checks, then General / Critical (with customer override) / Full curtailment with "
             "stabilization and averaged samples, cancel, MITO, price stream and re-registration. Choose cooling or heating "
             "mode for the lab conditions.", appendix_c, composite=True),
    Scenario("tc_reg", "TC-REG Utility Setup", "Table 4 / 6.1.1",
             "oadrCreatePartyRegistration values (2.0b, simpleHttp, pull, no signature) and polling cadence.", tc_reg),
    Scenario("tc_meta", "TC-META Report registration", "Tables 5 & 6",
             "x-AHRI_STATIC and x-AHRI_STATUS registered with all rIDs, types and a 1 minute sampling capability.", tc_meta),
    Scenario("tc_static", "TC-STATIC Static configuration report", "Table 5",
             "Request x-AHRI_STATIC once and verify all eight values (size, type, unloading, benchmarks, vendor...).", tc_static),
    Scenario("tc_status", "TC-STATUS Periodic status telemetry", "Table 6 / 6.1.9-6.1.11",
             "Collect status samples, check 1 minute cadence and valid operational / override state codes.", tc_status),
    Scenario("tc_gc", "TC-GC General Curtailment", "6.1.3 / C.3.5 a",
             "SIMPLE level 1; opt-in, stabilization, 20 one-minute samples <= 70% of rated power, return to normal.", tc_gc),
    Scenario("tc_cc", "TC-CC Critical Curtailment with override", "6.1.4 / 6.1.10 / C.3.5 b",
             "SIMPLE level 2; <= 40% of rated power (or NAK 460 for two-capacity units), customer override via "
             "oadrCreateOpt, return to normal.", tc_cc),
    Scenario("tc_fc", "TC-FC Full Curtailment / Off mode", "6.1.5 / C.3.5 c",
             "SIMPLE level 3; compressor off (power within controls allowance), operational state Idle Curtailed.", tc_fc),
    Scenario("tc_override", "TC-OVERRIDE Customer override during General Curtailment", "6.1.10",
             "General Curtailment with a customer override initiated at the unit.", tc_override),
    Scenario("tc_cancel_future", "TC-CANCEL-FUTURE Cancel a scheduled event", "6.1.7",
             "Schedule a future event, cancel it, verify acknowledgement and that no curtailment happened.", tc_cancel_future),
    Scenario("tc_mito", "TC-MITO Temperature offset events", "6.1.2 / Table 8",
             "LOAD_CONTROL:x-loadControlLevelOffset alone and combined with SIMPLE.", tc_mito),
    Scenario("tc_price", "TC-PRICE Price stream", "6.1.8 / Table 8",
             "ELECTRICITY_PRICE:priceMultiplier event with 25 intervals.", tc_price),
    Scenario("tc_rereg", "TC-REREG Re-registration request", "Table 4",
             "oadrRequestReregistration followed by a new oadrCreatePartyRegistration.", tc_rereg),
]
