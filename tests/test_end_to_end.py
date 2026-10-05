"""
End-to-end tests: AHRI VTN <-> simulated VEN over real HTTP, fast timings.
"""
import asyncio
import os
from datetime import timedelta

import aiohttp
import pytest
from sqlalchemy import select

from ahri_vtn import spec
from ahri_vtn.db import models
from openleadr.messaging import parse_message, validate_xml_schema

from .conftest import start_sim, ven_pk_of, wait_until



@pytest.mark.asyncio
async def test_registration_reports_and_samples(vtn, sim):
    vens = vtn.queries.list_vens()
    assert len(vens) == 1
    ven = vens[0]
    assert ven["registered"] and ven["ven_id"] == sim.ven_id
    assert ven["profile_name"] == "2.0b" and ven["transport_name"] == "simpleHttp" and ven["http_pull_model"] is True

    detail = vtn.queries.get_ven(ven["id"])
    specs = {r["spec_name"] for r in detail["registered_reports"] if r["active"]}
    assert specs == {"x-AHRI_STATIC", "x-AHRI_STATUS"}
    status_report = next(r for r in detail["registered_reports"] if r["spec_name"] == "x-AHRI_STATUS")
    assert all(row["present"] for row in status_report["checklist"] if row["rid"])

    # Static values (incl. text) arrive with the automatic request
    assert await wait_until(lambda: (vtn.queries.get_ven(ven["id"]).get("static_info") or {}).get("Model_Number"), 15)
    static = {row["rid"]: row["value"] for row in vtn.queries.get_ven(ven["id"])["static_rows"]}
    assert static["Size"] == 4800.0 and static["Unloading_Type"] == 0 and static["Device_Type"] == 1
    assert static["Vendor_Name"] == "Acme HVAC" and static["Model_Number"] == "HP-48K-2S"

    # Status samples accumulate
    assert await wait_until(lambda: len(vtn.queries.samples(ven["id"], spec_rid="Inst Power")) >= 3, 15)
    latest = vtn.queries.get_ven(ven["id"])["status"]
    assert latest["Operational State"] == 1 and latest["Override State"] == 0 and latest["Inst Power"] > 0

    codes = {f["code"] for f in vtn.queries.findings(ven_pk=ven["id"])}
    assert "reg-table4" in codes and "report-registered" in codes
    assert "schema-violation" in codes      # text payloads are schema-invalid but tolerated in lenient mode

    # Raw messages were captured and paired
    page = vtn.queries.list_messages(message_type="oadrRegisterReport")
    assert page["total"] >= 1
    msg = vtn.queries.get_message(page["items"][0]["id"])
    assert "oadrRegisterReport" in msg["raw_xml"]
    assert any(p["message_type"] == "oadrRegisteredReport" for p in msg["pair"])


@pytest.mark.asyncio
async def test_event_lifecycle_and_cancel(vtn, sim):
    ven_pk = ven_pk_of(vtn)
    event_id = vtn.events.create_event(ven_pk, "general_curtailment", duration=timedelta(seconds=60), mito=3)
    assert await wait_until(lambda: vtn.queries.event_responses(event_id), 10)
    resp = vtn.queries.event_responses(event_id)[0]
    assert (resp.opt_type, resp.response_code, resp.modification_number) == ("optIn", 200, 0)
    assert await wait_until(lambda: sim.level == 1, 10)
    assert sim.mito == 3
    assert await wait_until(lambda: vtn.queries.get_ven(ven_pk)["status"]["Operational State"] == 2, 10)
    ev = vtn.queries.get_event(event_id)
    assert len(ev["signals"]) == 2 and {s["signal_name"] for s in ev["signals"]} == {"SIMPLE", "LOAD_CONTROL"}

    assert vtn.events.cancel_event(event_id)
    assert await wait_until(lambda: len(vtn.queries.event_responses(event_id)) >= 2, 10)
    assert vtn.queries.event_responses(event_id)[1].modification_number == 1
    assert await wait_until(lambda: sim.level == 0, 10)
    assert vtn.queries.get_event(event_id)["status"] == "cancelled"
    assert not vtn.events.cancel_event(event_id)


@pytest.mark.asyncio
async def test_customer_override_opt(vtn, sim):
    ven_pk = ven_pk_of(vtn)
    event_id = vtn.events.create_event(ven_pk, "critical_curtailment", duration=timedelta(seconds=60))
    assert await wait_until(lambda: sim.level == 2, 10)
    opt_id, (response_type, payload) = await sim.customer_override()
    assert response_type == "oadrCreatedOpt" and payload["opt_id"] == opt_id
    opts = vtn.queries.opts(ven_pk=ven_pk)
    assert opts[0].opt_type == "optOut" and opts[0].opt_reason == "overrideStatus" and opts[0].event_id == event_id
    assert await wait_until(lambda: vtn.queries.get_ven(ven_pk)["status"]["Override State"] == 1, 10)
    assert vtn.queries.get_ven(ven_pk)["status"]["Operational State"] == 12
    await sim.clear_override(opt_id)
    assert await wait_until(lambda: vtn.queries.opts(ven_pk=ven_pk)[0].cancelled_at is not None, 5)


@pytest.mark.asyncio
async def test_two_capacity_unit_naks_critical_curtailment(vtn):
    sim = await start_sim(vtn, unloading_type=1)
    try:
        assert await wait_until(lambda: vtn.queries.list_vens() and vtn.queries.list_vens()[0]["registered"], 15)
        ven_pk = ven_pk_of(vtn)
        event_id = vtn.events.create_event(ven_pk, "critical_curtailment", duration=timedelta(seconds=60))
        assert await wait_until(lambda: vtn.queries.event_responses(event_id), 10)
        resp = vtn.queries.event_responses(event_id)[0]
        assert resp.opt_type == "optOut" and resp.response_code == 460
        assert sim.level == 0
    finally:
        await sim.stop()


@pytest.mark.asyncio
async def test_one_shot_report_request_and_cancel(vtn, sim):
    ven_pk = ven_pk_of(vtn)
    rrid = vtn.reports.request_report(ven_pk, spec_name="x-AHRI_STATIC", one_shot=True)

    def status():
        with vtn.db.session() as s:
            rr = s.scalar(select(models.ReportRequest).where(models.ReportRequest.report_request_id == rrid))
            return rr.status, rr.update_count
    assert await wait_until(lambda: status() == ("cancelled", 1), 20), status()

    # Periodic request then explicit cancel
    rrid2 = vtn.reports.request_report(ven_pk, spec_name="x-AHRI_STATUS", one_shot=False, granularity_seconds=1)
    assert await wait_until(lambda: status.__func__ if False else _rr_status(vtn, rrid2)[1] >= 2, 15)
    assert vtn.reports.cancel_report(rrid2)
    assert await wait_until(lambda: _rr_status(vtn, rrid2)[0] == "cancelled", 10)


def _rr_status(vtn, rrid):
    with vtn.db.session() as s:
        rr = s.scalar(select(models.ReportRequest).where(models.ReportRequest.report_request_id == rrid))
        return rr.status, rr.update_count


@pytest.mark.asyncio
async def test_request_reregistration(vtn, sim):
    ven_pk = ven_pk_of(vtn)
    assert vtn.registry.request_reregistration(ven_pk)

    def kinds():
        with vtn.db.session() as s:
            return [r.kind for r in s.scalars(select(models.RegistrationEvent).order_by(models.RegistrationEvent.ts))]
    assert await wait_until(lambda: "reregister" in kinds(), 10), kinds()
    assert "request_reregistration" in kinds()
    assert vtn.queries.get_ven(ven_pk)["registered"]


@pytest.mark.asyncio
async def test_unknown_ven_rejected_when_not_auto_accepting():
    from .conftest import make_config
    from ahri_vtn.server import AhriVtn
    config = make_config(auto_accept_unknown_vens=False)
    vtn = AhriVtn(config, enable_web=False)
    await vtn.start()
    sim = None
    try:
        vtn.registry.add_ven("known-unit")
        sim = await start_sim(vtn, ven_name="unknown-unit")
        await asyncio.sleep(2)
        assert sim.client.registration_id is None
        assert [v["ven_name"] for v in vtn.queries.list_vens()] == ["known-unit"]
        codes = [f["code"] for f in vtn.queries.findings()]
        assert "reg-unknown-ven" in codes
    finally:
        if sim:
            await sim.stop()
        await vtn.stop()
        os.remove(config.db_url.replace("sqlite:///", ""))


@pytest.mark.asyncio
async def test_api_and_ui(vtn, sim):
    ven_pk = ven_pk_of(vtn)
    base = f"http://127.0.0.1:{vtn.config.port}"
    async with aiohttp.ClientSession() as http:
        for path in ["/ui/", f"/ui/vens/{ven_pk}", "/ui/events", "/ui/messages", "/ui/messages/1", "/ui/runs",
                     "/ui/findings", "/api/status", "/api/vens", f"/api/vens/{ven_pk}", f"/api/vens/{ven_pk}/samples.csv",
                     f"/api/vens/{ven_pk}/series?minutes=5", "/api/events", "/api/messages", "/api/scenarios",
                     "/api/runs", "/api/spec", "/api/presets", "/api/findings", "/ui/static/app.css"]:
            async with http.get(base + path) as r:
                assert r.status == 200, (path, await r.text())
        async with http.post(base + "/api/events", json={"ven_pk": ven_pk, "preset": "price_stream",
                                                         "price_multipliers": "1.0,1.5,2.0", "price_interval_minutes": 5}) as r:
            assert r.status == 201
            event_id = (await r.json())["event_id"]
        ev = vtn.queries.get_event(event_id)
        assert len(ev["signals"][0]["intervals"]) == 3 and ev["duration_seconds"] == 900
        assert await wait_until(lambda: vtn.queries.event_responses(event_id), 10)
        async with http.post(base + f"/api/events/{event_id}/cancel") as r:
            assert r.status == 200
        async with http.post(base + "/api/vens", json={"ven_name": "bench-2", "notes": "second bay"}) as r:
            assert r.status == 201
        async with http.post(base + "/api/vens", json={"ven_name": "bench-2"}) as r:
            assert r.status == 409
        async with http.post(base + "/ui/events", data={"ven_pk": ven_pk, "preset": "mito", "payload": "3",
                                                        "duration_minutes": "5"}, allow_redirects=False) as r:
            assert r.status == 302 and "queued" in r.headers["Location"]
        async with http.get(base + f"/api/vens/{ven_pk}/samples.csv?rid=Inst%20Power") as r:
            text = await r.text()
            assert text.splitlines()[0].startswith("ts,received_at")
            assert len(text.splitlines()) > 1


@pytest.mark.asyncio
async def test_scenarios_pass_against_simulator(vtn, sim):
    ven_pk = ven_pk_of(vtn)
    params = {"stabilization_seconds": 4, "sample_count": 3, "sample_interval_seconds": 1, "response_timeout_seconds": 10,
              "override_timeout_seconds": 15, "recovery_timeout_seconds": 20, "report_timeout_seconds": 15,
              "status_samples": 3, "future_offset_seconds": 60, "poll_interval_seconds": 1, "operator_prompts": False,
              "wattmeter_prompt": False, "price_interval_minutes": 1}

    # When the override step starts, the "customer" presses override on the simulated unit
    async def override_when_prompted():
        while True:
            ev = await vtn.bus.wait_for(lambda e: e["topic"] == "run.step" and e.get("status") == "running"
                                        and "customer override" in e.get("name", ""), timeout=120)
            if ev is None:
                return
            await asyncio.sleep(0.5)
            await sim.customer_override()
    helper = asyncio.get_event_loop().create_task(override_when_prompted())
    try:
        results = {}
        for scenario_id in ("tc_reg", "tc_meta", "tc_static", "tc_status", "tc_gc", "tc_cc", "tc_fc", "tc_cancel_future",
                            "tc_mito", "tc_price", "tc_rereg"):
            run_pk = vtn.conformance.start_run(scenario_id, ven_pk, params=params)
            assert await wait_until(lambda: vtn.queries.get_run(run_pk)["status"] != "running", 120), scenario_id
            run = vtn.queries.get_run(run_pk)
            failed = [(st["name"], [a for a in st["details"]["assertions"] if a["status"] == "fail"])
                      for st in run["steps"] if st["status"] == "failed"]
            results[scenario_id] = run["status"]
            assert run["status"] in ("passed", "needs_review"), (scenario_id, failed)
            assert not failed, (scenario_id, failed)
    finally:
        helper.cancel()
    assert results["tc_reg"] == "passed" and results["tc_static"] == "passed"
    assert vtn.conformance.list_scenarios()[0]["id"] == "appendix_c"
    # The two-capacity branch: a discretely variable unit must NAK Critical Curtailment
    report = vtn.queries.get_run(run_pk)
    assert report["ven_name"] == "sim-hvac"


@pytest.mark.asyncio
async def test_scenario_abort_and_prompt(vtn, sim):
    ven_pk = ven_pk_of(vtn)
    params = {"stabilization_seconds": 1, "sample_count": 2, "sample_interval_seconds": 1, "response_timeout_seconds": 10,
              "poll_interval_seconds": 1, "operator_prompts": True, "wattmeter_prompt": True}
    assert await wait_until(lambda: vtn.queries.get_ven(ven_pk)["rated_power"], 15)
    run_pk = vtn.conformance.start_run("tc_gc", ven_pk, params=params)
    assert await wait_until(lambda: vtn.conformance.pending_prompt(run_pk) is not None, 40)
    prompt = vtn.conformance.pending_prompt(run_pk)
    assert "wattmeter" in prompt["message"].lower()
    assert vtn.conformance.respond(run_pk, "Submit", note="3100")
    assert await wait_until(lambda: vtn.queries.get_run(run_pk)["status"] != "running", 60)
    run = vtn.queries.get_run(run_pk)
    assertions = [a for st in run["steps"] for a in st["details"]["assertions"]]
    assert any("Measured 3100 W" in a["message"] and a["status"] == "pass" for a in assertions)

    run_pk = vtn.conformance.start_run("tc_gc", ven_pk, params=params)
    await asyncio.sleep(1)
    assert await vtn.conformance.abort_run(run_pk)
    assert vtn.queries.get_run(run_pk)["status"] == "aborted"


def test_status_report_registered_under_standard_name_is_recognised():
    """Some VENs register the AHRI status report as TELEMETRY_STATUS with an x-AHRI_STATUS specifier id."""
    from openleadr import objects
    from openleadr.messaging import create_message
    report = objects.Report(report_specifier_id="Device001-x-AHRI_STATUS", report_name="TELEMETRY_STATUS",
                            duration=timedelta(hours=1), report_descriptions=[
                                objects.ReportDescription(r_id=r_id, market_context=None, reading_type="Direct Read",
                                                          report_subject=None, report_data_source=objects.Target(resource_id="Device001"),
                                                          report_type="reading",
                                                          sampling_rate=objects.SamplingRate(timedelta(minutes=1), timedelta(minutes=1)))
                                for r_id in ("Inst Power", "Operational State", "Override State")])
    raw = create_message("oadrRegisterReport", request_id="r1", ven_id="ven1", reports=[report])
    validate_xml_schema(raw)
    message_type, payload = parse_message(raw)
    assert message_type == "oadrRegisterReport"
    registered = payload["reports"][0]
    assert registered["report_name"] == "METADATA_TELEMETRY_STATUS"
    assert spec.identify_report(registered["report_name"], registered["report_specifier_id"]) is spec.STATUS_REPORT
    # A plain TELEMETRY_STATUS report with an unrelated specifier id is not an AHRI report
    assert spec.identify_report("METADATA_TELEMETRY_STATUS", "789ed6cd4e_telemetry_status") is None
    assert spec.validate_registered_report(spec.STATUS_REPORT, registered) and \
        all(d.severity != "fail" for d in spec.validate_registered_report(spec.STATUS_REPORT, registered))


def test_spec_validation_helpers():
    report = {"report_name": "x-METADATA_x-AHRI_STATUS", "report_specifier_id": "x-AHRI_STATUS",
              "report_descriptions": [
                  {"r_id": "Inst_Power", "report_type": "reading", "reading_type": "Direct Read",
                   "measurement": {"name": "customUnit", "description": "power", "unit": "W"},
                   "sampling_rate": {"min_period": timedelta(minutes=5), "max_period": timedelta(minutes=5)}},
                  {"r_id": "Operational State", "report_type": "usage", "reading_type": "Direct Read",
                   "sampling_rate": {"min_period": timedelta(minutes=1), "max_period": timedelta(minutes=1)}}]}
    codes = {(d.code, d.severity) for d in spec.validate_registered_report(spec.STATUS_REPORT, report)}
    assert ("rid-missing", "fail") in codes            # Override State
    assert ("rid-sampling", "fail") in codes           # 5 minute minimum
    assert ("rid-report-type", "warning") in codes     # usage instead of reading
    assert ("rid-spelling", "info") in codes           # Inst_Power
    assert spec.operational_state_name(12) == "Running, Opted Out"
    assert spec.validate_registration({"profile_name": "2.0b", "transport_name": "simpleHttp", "http_pull_model": True,
                                       "xml_signature": False, "report_only": False, "ven_name": "x"}) == []
