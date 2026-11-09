"""
Multi-resource VENs, saved groups and dispatches: resource discovery, resource / group / whole-VEN
event targeting, selection-based report requests and scenario runs on selections.
"""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from ahri_vtn.db import models
from ahri_vtn.targets import Selection

from .conftest import start_sim, wait_until


def _ven(vtn, name):
    return next(v for v in vtn.queries.list_vens() if v["ven_name"] == name)


@pytest.mark.asyncio
async def test_multi_resource_discovery_and_targeting(vtn):
    fleet = await start_sim(vtn, ven_name="fleet", resource_ids=["u1", "u2", "u3"], group_ids=["lab-a"])
    single = await start_sim(vtn, ven_name="single")
    try:
        assert await wait_until(lambda: len(vtn.queries.list_vens()) == 2 and
                                all(v["registered"] for v in vtn.queries.list_vens()), 15)
        fleet_ven, single_ven = _ven(vtn, "fleet"), _ven(vtn, "single")
        assert await wait_until(lambda: _ven(vtn, "fleet")["resource_ids"] == ["u1", "u2", "u3"], 10)
        assert _ven(vtn, "single")["resource_ids"] == ["hvac-1"]

        # Per-resource status caches fill independently
        assert await wait_until(lambda: all(r["status"]["Operational State"] is not None
                                            for r in vtn.queries.list_resources(fleet_ven["id"])), 15)
        res_u2 = vtn.queries.get_resource(fleet_ven["id"], "u2")
        assert res_u2["status"]["Operational State"] == 1 and res_u2["sample_count"] > 0
        assert await wait_until(lambda: vtn.queries.get_resource(fleet_ven["id"], "u2")["rated_power"] == 4800.0, 15)

        # Event targeted at one resource only affects that unit
        result = await vtn.aio.events.dispatch_event(Selection.resource(fleet_ven["id"], "u2"), "general_curtailment",
                                           duration=timedelta(seconds=60))
        assert list(result["events"]) == [fleet_ven["id"]]
        event_id = result["events"][fleet_ven["id"]]
        assert await wait_until(lambda: vtn.queries.event_responses(event_id), 10)
        assert vtn.queries.event_responses(event_id)[0].opt_type == "optIn"
        assert await wait_until(lambda: fleet.units["u2"].level == 1, 10)
        await asyncio.sleep(1.5)
        assert fleet.units["u1"].level == 0 and fleet.units["u3"].level == 0 and single.level == 0
        ev = vtn.queries.get_event(event_id)
        assert ev["target_resource_ids"] == ["u2"] and ev["targets"] == [{"resource_id": "u2"}]
        assert await wait_until(lambda: vtn.queries.get_resource(fleet_ven["id"], "u2")["status"]["Operational State"] == 2, 10)
        assert vtn.queries.get_resource(fleet_ven["id"], "u1")["status"]["Operational State"] == 1
        # Resource-level event listing: u2 sees it, u1 does not
        assert any(e["event_id"] == event_id for e in vtn.queries.list_events(ven_pk=fleet_ven["id"], resource_id="u2"))
        assert not any(e["event_id"] == event_id for e in vtn.queries.list_events(ven_pk=fleet_ven["id"], resource_id="u1"))
        assert await vtn.aio.events.cancel_dispatch(result["dispatch_pk"]) == 1
        assert await wait_until(lambda: fleet.units["u2"].level == 0, 10)

        # A group spanning a resource of one VEN and the whole other VEN, signalling a groupID
        group = await vtn.aio.groups.save_group("Bay A", {"vens": [{"ven_pk": fleet_ven["id"], "resource_ids": ["u1"]},
                                                        {"ven_pk": single_ven["id"]}]},
                                      signal_group_id="lab-a")
        assert group["summary"] == "fleet (u1); single (whole VEN)"
        targets = Selection(groups=[group["id"]]).resolve(vtn.db)
        assert [(t.ven_name, t.resource_ids, t.group_ids) for t in targets] == \
            [("fleet", ["u1"], []), ("single", None, ["lab-a"])]
        result = await vtn.aio.events.dispatch_event(Selection(groups=[group["id"]]), "critical_curtailment",
                                           duration=timedelta(seconds=60))
        assert set(result["events"]) == {fleet_ven["id"], single_ven["id"]}
        assert await wait_until(lambda: all(vtn.queries.event_responses(e) for e in result["events"].values()), 10)
        # The whole-VEN member receives venID + groupID; the resource member receives only its resourceID,
        # so the fleet VEN curtails u1 alone even though it belongs to group lab-a.
        assert await wait_until(lambda: single.level == 2 and fleet.units["u1"].level == 2, 10)
        await asyncio.sleep(1)
        assert fleet.units["u2"].level == 0 and fleet.units["u3"].level == 0
        ev_fleet = vtn.queries.get_event(result["events"][fleet_ven["id"]])
        assert ev_fleet["targets"] == [{"resource_id": "u1"}]
        ev_single = vtn.queries.get_event(result["events"][single_ven["id"]])
        assert {"group_id": "lab-a"} in ev_single["targets"] and {"ven_id": single_ven["ven_id"]} in ev_single["targets"]
        dispatch = vtn.queries.get_dispatch(result["dispatch_pk"])
        assert dispatch["live"] and len(dispatch["events"]) == 2
        assert await vtn.aio.events.cancel_dispatch(result["dispatch_pk"]) == 2
        assert await wait_until(lambda: single.level == 0 and all(u.level == 0 for u in fleet.units.values()), 10)

        # Whole-VEN event reaches every unit of the fleet
        result = await vtn.aio.events.dispatch_event(Selection.whole_ven(fleet_ven["id"]), "general_curtailment",
                                           duration=timedelta(seconds=60))
        assert await wait_until(lambda: all(u.level == 1 for u in fleet.units.values()), 10)
        assert single.level == 0
        await vtn.aio.events.cancel_dispatch(result["dispatch_pk"])
        assert await wait_until(lambda: all(u.level == 0 for u in fleet.units.values()), 10)

        # Event to a resource the VEN does not have is refused with 462
        result = await vtn.aio.events.dispatch_event(Selection.resource(fleet_ven["id"], "nope"), "general_curtailment",
                                           duration=timedelta(seconds=60))
        event_id = result["events"][fleet_ven["id"]]
        assert await wait_until(lambda: vtn.queries.event_responses(event_id), 10)
        resp = vtn.queries.event_responses(event_id)[0]
        assert (resp.opt_type, resp.response_code) == ("optOut", 462)

        # Resource-targeted override is attributed to the resource
        result = await vtn.aio.events.dispatch_event(Selection.resource(fleet_ven["id"], "u3"), "general_curtailment",
                                           duration=timedelta(seconds=60))
        assert await wait_until(lambda: fleet.units["u3"].level == 1, 10)
        await fleet.customer_override(resource_id="u3")
        opts = vtn.queries.opts(ven_pk=fleet_ven["id"])
        assert opts[0].resource_id == "u3" and opts[0].opt_type == "optOut"
        assert await wait_until(lambda: vtn.queries.get_resource(fleet_ven["id"], "u3")["status"]["Override State"] == 1, 10)
        assert vtn.queries.get_resource(fleet_ven["id"], "u1")["status"]["Override State"] == 0
        await vtn.aio.events.cancel_dispatch(result["dispatch_pk"])
    finally:
        await fleet.stop()
        await single.stop()


@pytest.mark.asyncio
async def test_selection_report_requests(vtn):
    fleet = await start_sim(vtn, ven_name="fleet", resource_ids=["u1", "u2"])
    try:
        assert await wait_until(lambda: vtn.queries.list_vens() and _ven(vtn, "fleet")["resource_ids"] == ["u1", "u2"], 15)
        fleet_ven = _ven(vtn, "fleet")
        result = await vtn.aio.reports.request_reports(Selection.resource(fleet_ven["id"], "u2"), "x-AHRI_STATIC", one_shot=True)
        assert len(result["report_request_ids"]) == 1 and not result["errors"]
        rrid = result["report_request_ids"][0]
        with vtn.db.session() as s:
            rr = s.scalar(select(models.ReportRequest).where(models.ReportRequest.report_request_id == rrid))
            assert rr.resource_id == "u2" and rr.report_specifier_id == "u2-x-AHRI_STATIC" and rr.dispatch_pk is not None

        result = await vtn.aio.reports.request_reports(Selection.whole_ven(fleet_ven["id"]), "x-AHRI_STATUS", one_shot=True)
        assert len(result["report_request_ids"]) == 2        # one status report per unit
        result = await vtn.aio.reports.request_reports(Selection.whole_ven(fleet_ven["id"]), "x-NOPE", one_shot=True)
        assert result["report_request_ids"] == [] and result["errors"]
        assert vtn.queries.list_dispatches(kind="report_request")
    finally:
        await fleet.stop()


@pytest.mark.asyncio
async def test_scenarios_on_group_and_whole_fleet(vtn):
    fleet = await start_sim(vtn, ven_name="fleet", resource_ids=["u1", "u2"],
                            unit_overrides={"u2": {"unloading_type": 1}})
    single = await start_sim(vtn, ven_name="single")
    try:
        assert await wait_until(lambda: len(vtn.queries.list_vens()) == 2 and
                                all(v["registered"] for v in vtn.queries.list_vens()) and
                                _ven(vtn, "fleet")["resource_ids"] == ["u1", "u2"], 15)
        fleet_ven, single_ven = _ven(vtn, "fleet"), _ven(vtn, "single")
        group = await vtn.aio.groups.save_group("mixed", {"vens": [{"ven_pk": fleet_ven["id"], "resource_ids": ["u1"]},
                                                        {"ven_pk": single_ven["id"]}]})
        params = {"stabilization_seconds": 4, "sample_count": 3, "sample_interval_seconds": 1,
                  "response_timeout_seconds": 10, "override_timeout_seconds": 15, "recovery_timeout_seconds": 20,
                  "report_timeout_seconds": 15, "status_samples": 3, "future_offset_seconds": 60,
                  "poll_interval_seconds": 1, "operator_prompts": False, "wattmeter_prompt": False}

        async def override_when_prompted():
            while True:
                ev = await vtn.bus.wait_for(lambda e: e["topic"] == "run.step" and e.get("status") == "running"
                                            and "customer override" in e.get("name", ""), timeout=120)
                if ev is None:
                    return
                await asyncio.sleep(0.5)
                await fleet.customer_override(resource_id="u1")
                await single.customer_override()
        helper = asyncio.get_event_loop().create_task(override_when_prompted())
        try:
            for scenario_id in ("tc_static", "tc_status", "tc_gc", "tc_cc", "tc_cancel_future"):
                run_pk = await vtn.aio.conformance.start_run(scenario_id, selection={"groups": [group["id"]]}, params=params)
                assert await wait_until(lambda: vtn.queries.get_run(run_pk)["status"] != "running", 150), scenario_id
                run = vtn.queries.get_run(run_pk)
                failed = [(st["name"], [a["message"] for a in st["details"]["assertions"] if a["status"] == "fail"])
                          for st in run["steps"] if st["status"] == "failed"]
                assert not failed, (scenario_id, failed)
                assert run["target_label"] == "fleet: u1; single (whole VEN)"
                assert len(run["summary"]["units"]) == 2
                if scenario_id == "tc_gc":
                    # per-unit assertions are tagged and u2 (not in the group) stayed normal
                    messages = [a["message"] for st in run["steps"] for a in st["details"]["assertions"]]
                    assert any(m.startswith("[fleet/u1]") for m in messages) and any(m.startswith("[single]") for m in messages)
                    assert fleet.units["u2"].level == 0
        finally:
            helper.cancel()

        # Whole fleet VEN: u2 is two-capacity, u1 is not, so the VEN opts in to Critical Curtailment and only u1 curtails
        run_pk = await vtn.aio.conformance.start_run("tc_gc", ven_pk=fleet_ven["id"], params=params)
        assert await wait_until(lambda: vtn.queries.get_run(run_pk)["status"] != "running", 150)
        run = vtn.queries.get_run(run_pk)
        assert run["status"] in ("passed", "needs_review"), [(st["name"], st["status"]) for st in run["steps"]]
        assert run["target_label"] == "fleet (whole VEN)" and len(run["summary"]["units"]) == 2
    finally:
        await fleet.stop()
        await single.stop()


def test_selection_parsing():
    sel = Selection.from_form(["ven:3", "res:4:hvac-1", "res:4:hvac-2", "group:7", "res:3:x"])
    assert sel.as_dict() == {"vens": [{"ven_pk": 3, "resource_ids": None},
                                      {"ven_pk": 4, "resource_ids": ["hvac-1", "hvac-2"]}], "groups": [7]}
    assert sorted(sel.form_values()) == ["group:7", "res:4:hvac-1", "res:4:hvac-2", "ven:3"]
    assert Selection.from_any(5).as_dict()["vens"] == [{"ven_pk": 5, "resource_ids": None}]
    assert Selection.from_any({"vens": [{"ven_pk": 1, "resource_ids": ["a"]}]}).vens == {1: {"a"}}
    assert Selection().is_empty()
    with pytest.raises(ValueError):
        Selection.from_form(["bogus:1"])
