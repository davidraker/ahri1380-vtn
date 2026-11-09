"""
JSON API (aiohttp) under /api. Everything the UI does is available here for scripting.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import timedelta

from aiohttp import web

from .. import spec
from ..aio import service_proxies
from ..targets import Selection
from ..util import jsonable, parse_iso, utcnow

logger = logging.getLogger("ahri_vtn.api")


def _dumps(obj) -> str:
    return json.dumps(jsonable(obj), default=str)


def json_response(data, status: int = 200) -> web.Response:
    return web.Response(text=_dumps(data), status=status, content_type="application/json")


def error(message: str, status: int = 400) -> web.Response:
    return json_response({"error": message}, status=status)


async def read_body(request: web.Request) -> dict:
    """Body as a dict. Repeated form fields named 'target' are collected into a 'targets' list."""
    if request.content_type == "application/json":
        try:
            return await request.json()
        except json.JSONDecodeError as err:
            raise web.HTTPBadRequest(text=f"Invalid JSON: {err}")
    data = await request.post()
    body = {k: v for k, v in data.items()}
    targets = data.getall("target", [])
    if targets:
        body["targets"] = list(targets)
    return body


def selection_from(data: dict, default_ven_pk=None) -> Selection:
    """
    Build a Selection from a request body: 'selection' (wire dict), 'targets' (form values such as
    'ven:3' / 'res:3:hvac-1' / 'group:2'), or 'ven_pk' (+ optional 'resource_ids') for one VEN.
    """
    if data.get("selection"):
        sel = data["selection"]
        if isinstance(sel, str):
            sel = json.loads(sel)
        return Selection.from_any(sel)
    if data.get("targets"):
        return Selection.from_form(data["targets"])
    if data.get("group_pk"):
        return Selection(groups=[int(data["group_pk"])])
    ven_pk = _int(data.get("ven_pk")) if data.get("ven_pk") else default_ven_pk
    if ven_pk is None:
        raise ValueError("Select at least one VEN, resource or group")
    resource_ids = data.get("resource_ids")
    if isinstance(resource_ids, str):
        resource_ids = [r.strip() for r in resource_ids.split(",") if r.strip()]
    if data.get("resource_id"):
        resource_ids = [data["resource_id"]]
    return Selection(vens=[{"ven_pk": ven_pk, "resource_ids": resource_ids or None}])


def _bool(value, default=False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _float(value, default=None):
    if value is None or value == "":
        return default
    return float(value)


def _int(value, default=None):
    if value is None or value == "":
        return default
    return int(float(value))


def _qint(qs, name: str, default: int, lo: int, hi: int) -> int:
    """Integer query parameter clamped into [lo, hi]; malformed values are a 400."""
    raw = qs.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"{name} must be a number")
    return max(lo, min(hi, value))


def _qfloat(qs, name: str, default: float, lo: float, hi: float) -> float:
    raw = qs.get(name)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"{name} must be a number")
    return max(lo, min(hi, value))


class Api:
    def __init__(self, vtn):
        self.vtn = vtn
        for name, proxy in service_proxies(vtn).items():
            setattr(self, name, proxy)

    # ------------------------------------------------------------------ status

    async def status(self, request):
        cfg = self.vtn.config
        return json_response({"vtn_id": cfg.vtn_id, "openadr_url": cfg.openadr_url, "schema_mode": cfg.schema_mode,
                              "requested_poll_freq_seconds": cfg.requested_poll_freq_seconds,
                              "auto_accept_unknown_vens": cfg.auto_accept_unknown_vens,
                              "security": cfg.summary(), "principal": str(request.get("principal")),
                              "counts": await self.q.dashboard_counts(), "now": utcnow()})

    async def tls_info(self, request):
        """The VTN's certificates (to hand to vendors) and the cipher suites the listener offers."""
        report = self.vtn.tls_report
        return json_response({"mode": self.vtn.config.tls.mode,
                              "certificates": [c.as_dict() for c in report.certificates] if report else [],
                              "cipher_suites": report.suites if report else [],
                              "missing_openadr_suites": report.missing_openadr_suites if report else [],
                              "fingerprint_policy": self.vtn.config.tls.fingerprint_policy})

    async def presets(self, request):
        return json_response({pid: p.__dict__ for pid, p in spec.EVENT_PRESETS.items()})

    async def spec_tables(self, request):
        return json_response({"reports": {name: {"title": r.title, "sampling_interval": r.sampling_interval,
                                                 "rids": [rid.__dict__ for rid in r.rids]}
                                          for name, r in spec.REPORTS.items()},
                              "operational_states": spec.OPERATIONAL_STATES,
                              "response_codes": spec.RESPONSE_CODES})

    # ------------------------------------------------------------------ VENs

    async def list_vens(self, request):
        return json_response(await self.q.list_vens())

    async def create_ven(self, request):
        data = await read_body(request)
        if not data.get("ven_name"):
            return error("ven_name is required")
        try:
            ven = await self.registry.add_ven(data["ven_name"], ven_id=data.get("ven_id") or None,
                                            notes=data.get("notes") or None, enabled=_bool(data.get("enabled"), True),
                                            fingerprint=data.get("fingerprint") or None)
        except ValueError as err:
            return error(str(err), 409)
        return json_response(ven, 201)

    async def pin_fingerprint(self, request):
        data = await read_body(request)
        if not data.get("fingerprint"):
            return error("fingerprint is required")
        try:
            ven = await self.registry.pin_fingerprint(int(request.match_info["pk"]), data["fingerprint"])
        except ValueError as err:
            return error(str(err), 409)
        return json_response(ven) if ven else error("Not found", 404)

    async def clear_fingerprint(self, request):
        ven = await self.registry.clear_fingerprint(int(request.match_info["pk"]))
        return json_response(ven) if ven else error("Not found", 404)

    async def rejected_registrations(self, request):
        return json_response(await self.registry.list_rejected(limit=_qint(request.query, "limit", 50, 1, 500),
                                                             include_resolved=_bool(request.query.get("all"))))

    async def dismiss_rejected(self, request):
        ok = await self.registry.dismiss_rejected(int(request.match_info["pk"]))
        return json_response({"ok": ok}) if ok else error("Not found", 404)

    async def get_ven(self, request):
        ven = await self.q.get_ven(int(request.match_info["pk"]))
        return json_response(ven) if ven else error("Not found", 404)

    async def update_ven(self, request):
        data = await read_body(request)
        fields = {}
        if "enabled" in data:
            fields["enabled"] = _bool(data["enabled"])
        for key in ("notes", "ven_name", "ven_id"):
            if key in data:
                fields[key] = data[key]
        ven = await self.registry.update_ven(int(request.match_info["pk"]), **fields)
        return json_response(ven) if ven else error("Not found", 404)

    async def delete_ven(self, request):
        ok = await self.registry.delete_ven(int(request.match_info["pk"]))
        return json_response({"ok": ok}) if ok else error("Not found", 404)

    async def request_reregistration(self, request):
        ok = await self.registry.request_reregistration(int(request.match_info["pk"]))
        return json_response({"ok": ok}) if ok else error("VEN is not registered", 409)

    async def request_report(self, request):
        data = await read_body(request)
        try:
            rrid = await self.reports.request_report(int(request.match_info["pk"]), spec_name=data.get("spec_name") or None,
                                                   report_specifier_id=data.get("report_specifier_id") or None,
                                                   one_shot=_bool(data.get("one_shot"), True),
                                                   granularity_seconds=_float(data.get("granularity_seconds")),
                                                   source="operator", resource_id=data.get("resource_id") or None)
        except ValueError as err:
            return error(str(err), 409)
        return json_response({"report_request_id": rrid}, 201)

    async def cancel_report(self, request):
        ok = await self.reports.cancel_report(request.match_info["rrid"])
        return json_response({"ok": ok}) if ok else error("Unknown report request", 404)

    async def samples(self, request):
        ven_pk = int(request.match_info["pk"])
        rows = await self._samples(request, ven_pk)
        return json_response([{"ts": r.ts, "received_at": r.received_at, "r_id": r.r_id, "spec_rid": r.spec_rid,
                               "value": r.value, "report_request_id": r.report_request_id,
                               "resource_id": r.resource_id} for r in rows])

    async def _samples(self, request, ven_pk):
        qs = request.query
        since = parse_iso(qs.get("since")) if qs.get("since") else None
        if since is None and qs.get("minutes"):
            since = utcnow() - timedelta(minutes=float(qs["minutes"]))
        until = parse_iso(qs.get("until")) if qs.get("until") else None
        return await self.q.samples(ven_pk, spec_rid=qs.get("rid") or None, r_key=qs.get("r_key") or None,
                              since=since, until=until, limit=_qint(qs, "limit", 5000, 1, 100000),
                              order=qs.get("order", "asc"), resource_id=qs.get("resource") or None)

    async def samples_csv(self, request):
        ven_pk = int(request.match_info["pk"])
        rows = await self._samples(request, ven_pk)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["ts", "received_at", "report", "r_id", "canonical_rid", "resource_id", "value",
                         "report_request_id", "confidence", "accuracy", "data_quality"])
        for r in rows:
            writer.writerow([r.ts.isoformat(), r.received_at.isoformat(), r.spec_name, r.r_id, r.spec_rid,
                             r.resource_id, r.value, r.report_request_id, r.confidence, r.accuracy, r.data_quality])
        return web.Response(text=out.getvalue(), content_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="ven{ven_pk}_samples.csv"'})

    async def series(self, request):
        ven_pk = int(request.match_info["pk"])
        minutes = _qfloat(request.query, "minutes", 120, 1, 60 * 24 * 31)
        since = utcnow() - timedelta(minutes=minutes)
        series = await self.q.status_series(ven_pk, since, resource_id=request.query.get("resource") or None)
        return json_response({rid: [{"ts": ts, "value": v} for ts, v in pts] for rid, pts in series.items()})

    async def findings(self, request):
        ven_pk = _int(request.query.get("ven"))
        run_pk = _int(request.query.get("run"))
        return json_response(await self.q.findings(ven_pk=ven_pk, run_pk=run_pk))

    # ------------------------------------------------------------------ events

    async def list_events(self, request):
        return json_response(await self.q.list_events(ven_pk=_int(request.query.get("ven")),
                                                limit=_qint(request.query, "limit", 100, 1, 5000),
                                                status=request.query.get("status") or None,
                                                resource_id=request.query.get("resource") or None))

    async def get_event(self, request):
        ev = await self.q.get_event(request.match_info["event_id"])
        return json_response(ev) if ev else error("Not found", 404)

    async def create_event(self, request):
        data = await read_body(request)
        try:
            result = await self.create_event_from_form(data)
        except (ValueError, KeyError) as err:
            return error(str(err))
        return json_response({"event_id": next(iter(result["events"].values())), **result}, 201)

    async def create_event_from_form(self, data: dict, default_ven_pk=None) -> dict:
        """Dispatch an event to the selection described by the form / JSON body."""
        selection = selection_from(data, default_ven_pk)
        preset = data.get("preset") or "general_curtailment"
        if preset not in spec.EVENT_PRESETS:
            raise ValueError(f"Unknown preset '{preset}'")
        start = parse_iso(data["start"]) if data.get("start") else None
        if start is None:
            start = utcnow() + timedelta(seconds=_float(data.get("start_offset_seconds"), 0) or 0)
        duration_seconds = _float(data.get("duration_seconds"))
        if duration_seconds is None and data.get("duration_minutes"):
            duration_seconds = float(data["duration_minutes"]) * 60
        duration = timedelta(seconds=duration_seconds) if duration_seconds else None
        multipliers = data.get("price_multipliers")
        if isinstance(multipliers, str):
            multipliers = [float(x) for x in multipliers.replace(";", ",").split(",") if x.strip()]
        price_interval = timedelta(minutes=_float(data.get("price_interval_minutes"), 60))
        ramp_up = timedelta(seconds=_float(data["ramp_up_seconds"])) if data.get("ramp_up_seconds") else None
        return await self.events.dispatch_event(
            selection, preset, start=start, duration=duration, payload=_float(data.get("payload")),
            mito=_int(data.get("mito")), price_multipliers=multipliers, price_interval=price_interval,
            test_event=_bool(data.get("test_event")), vtn_comment=data.get("vtn_comment") or None,
            ramp_up=ramp_up, source=data.get("source") or "operator", label=data.get("label") or None)

    async def list_dispatches(self, request):
        return json_response(await self.q.list_dispatches(limit=_qint(request.query, "limit", 100, 1, 5000),
                                                    kind=request.query.get("kind", "event")))

    async def get_dispatch(self, request):
        d = await self.q.get_dispatch(int(request.match_info["pk"]))
        return json_response(d) if d else error("Not found", 404)

    async def cancel_dispatch(self, request):
        count = await self.events.cancel_dispatch(int(request.match_info["pk"]))
        return json_response({"cancelled": count})

    # ------------------------------------------------------------------ resources, groups, targets

    async def list_resources(self, request):
        return json_response(await self.q.list_resources(int(request.match_info["pk"])))

    async def get_resource(self, request):
        res = await self.q.get_resource(int(request.match_info["pk"]), request.match_info["rid"])
        return json_response(res) if res else error("Not found", 404)

    async def update_resource(self, request):
        data = await read_body(request)
        ok = await self.q.update_resource(int(request.match_info["pk"]), request.match_info["rid"],
                                    **{k: data[k] for k in ("label", "notes") if k in data})
        return json_response({"ok": ok}) if ok else error("Not found", 404)

    async def targets(self, request):
        return json_response(await self.q.selector_tree())

    async def list_groups(self, request):
        return json_response(await self.groups.list_groups())

    async def get_group(self, request):
        g = await self.groups.get_group(int(request.match_info["pk"]))
        return json_response(g) if g else error("Not found", 404)

    async def save_group(self, request):
        data = await read_body(request)
        group_pk = int(request.match_info["pk"]) if "pk" in request.match_info else None
        try:
            selection = selection_from(data) if (data.get("selection") or data.get("targets")) else Selection()
            group = await self.groups.save_group(data.get("name"), selection, description=data.get("description"),
                                               signal_group_id=data.get("signal_group_id"), group_pk=group_pk)
        except ValueError as err:
            return error(str(err), 409 if "already exists" in str(err) else 400)
        return json_response(group, 201 if group_pk is None else 200)

    async def delete_group(self, request):
        ok = await self.groups.delete_group(int(request.match_info["pk"]))
        return json_response({"ok": ok}) if ok else error("Not found", 404)

    async def request_reports(self, request):
        data = await read_body(request)
        try:
            result = await self.reports.request_reports(selection_from(data), data.get("spec_name") or "x-AHRI_STATUS",
                                                      one_shot=_bool(data.get("one_shot"), True),
                                                      granularity_seconds=_float(data.get("granularity_seconds")))
        except ValueError as err:
            return error(str(err), 409)
        return json_response(result, 201)

    async def cancel_event(self, request):
        ok = await self.events.cancel_event(request.match_info["event_id"])
        return json_response({"ok": ok}) if ok else error("Event not found or already finished", 409)

    # ------------------------------------------------------------------ messages

    async def list_messages(self, request):
        qs = request.query
        return json_response(await self.q.list_messages(ven_id=qs.get("ven_id") or None, message_type=qs.get("type") or None,
                                                  direction=qs.get("direction") or None,
                                                  violations_only=_bool(qs.get("violations")),
                                                  page=_qint(qs, "page", 1, 1, 10**6), page_size=_qint(qs, "page_size", 50, 1, 500)))

    async def get_message(self, request):
        m = await self.q.get_message(int(request.match_info["pk"]))
        return json_response(m) if m else error("Not found", 404)

    # ------------------------------------------------------------------ conformance

    async def list_scenarios(self, request):
        return json_response(await self.conformance.list_scenarios())

    async def list_runs(self, request):
        return json_response(await self.q.list_runs(limit=_qint(request.query, "limit", 100, 1, 5000)))

    async def get_run(self, request):
        run = await self.q.get_run(int(request.match_info["pk"]))
        return json_response(run) if run else error("Not found", 404)

    async def start_run(self, request):
        data = await read_body(request)
        params = data.get("params") or {}
        if isinstance(params, str):
            params = json.loads(params) if params.strip() else {}
        # Flat form fields prefixed with param_ are also accepted
        for key, value in data.items():
            if key.startswith("param_") and value not in (None, ""):
                try:
                    params[key[6:]] = float(value) if "." in str(value) or str(value).isdigit() else value
                except ValueError:
                    params[key[6:]] = value
        try:
            selection = selection_from(data)
            run_pk = await self.conformance.start_run(data.get("scenario_id"), selection=selection,
                                                    mode=data.get("mode") or "cooling", params=params,
                                                    name=data.get("name") or None)
        except (ValueError, KeyError) as err:
            return error(str(err))
        return json_response({"run_pk": run_pk}, 201)

    async def abort_run(self, request):
        ok = await self.vtn.conformance.abort_run(int(request.match_info["pk"]))
        return json_response({"ok": ok}) if ok else error("Run is not running", 409)

    async def respond_run(self, request):
        data = await read_body(request)
        ok = await self.conformance.respond(int(request.match_info["pk"]), data.get("answer") or "Continue",
                                          note=data.get("note") or None)
        return json_response({"ok": ok}) if ok else error("No prompt is waiting for this run", 409)

    async def run_report(self, request):
        run = await self.q.get_run(int(request.match_info["pk"]))
        if run is None:
            return error("Not found", 404)
        return web.Response(text=_dumps(run), content_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="run{run["id"]}.json"'})

    # ------------------------------------------------------------------ live stream

    async def stream(self, request):
        if len(self.vtn.bus._subscribers) >= self.vtn.config.ui.max_sse_clients:
            return error("Too many live-update clients", 503)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                               "X-Accel-Buffering": "no"})
        await response.prepare(request)
        queue = self.vtn.bus.subscribe()
        try:
            await response.write(b": connected\n\n")
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 15)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                data = _dumps(event)
                await response.write(f"event: {event['topic']}\ndata: {data}\n\n".encode())
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self.vtn.bus.unsubscribe(queue)
        return response


def setup_api(vtn, app: web.Application):
    api = Api(vtn)
    vtn.api = api
    app.add_routes([
        web.get("/api/status", api.status),
        web.get("/api/tls", api.tls_info),
        web.post(r"/api/vens/{pk:\d+}/fingerprint", api.pin_fingerprint),
        web.delete(r"/api/vens/{pk:\d+}/fingerprint", api.clear_fingerprint),
        web.get("/api/rejected-registrations", api.rejected_registrations),
        web.post(r"/api/rejected-registrations/{pk:\d+}/dismiss", api.dismiss_rejected),
        web.get("/api/presets", api.presets),
        web.get("/api/spec", api.spec_tables),
        web.get("/api/stream", api.stream),
        web.get("/api/vens", api.list_vens),
        web.post("/api/vens", api.create_ven),
        web.get(r"/api/vens/{pk:\d+}", api.get_ven),
        web.patch(r"/api/vens/{pk:\d+}", api.update_ven),
        web.delete(r"/api/vens/{pk:\d+}", api.delete_ven),
        web.post(r"/api/vens/{pk:\d+}/request-reregistration", api.request_reregistration),
        web.post(r"/api/vens/{pk:\d+}/request-report", api.request_report),
        web.get(r"/api/vens/{pk:\d+}/resources", api.list_resources),
        web.get(r"/api/vens/{pk:\d+}/resources/{rid}", api.get_resource),
        web.patch(r"/api/vens/{pk:\d+}/resources/{rid}", api.update_resource),
        web.get("/api/targets", api.targets),
        web.get("/api/groups", api.list_groups),
        web.post("/api/groups", api.save_group),
        web.get(r"/api/groups/{pk:\d+}", api.get_group),
        web.put(r"/api/groups/{pk:\d+}", api.save_group),
        web.delete(r"/api/groups/{pk:\d+}", api.delete_group),
        web.post("/api/report-requests", api.request_reports),
        web.get("/api/dispatches", api.list_dispatches),
        web.get(r"/api/dispatches/{pk:\d+}", api.get_dispatch),
        web.post(r"/api/dispatches/{pk:\d+}/cancel", api.cancel_dispatch),
        web.get(r"/api/vens/{pk:\d+}/samples", api.samples),
        web.get(r"/api/vens/{pk:\d+}/samples.csv", api.samples_csv),
        web.get(r"/api/vens/{pk:\d+}/series", api.series),
        web.post("/api/report-requests/{rrid}/cancel", api.cancel_report),
        web.get("/api/findings", api.findings),
        web.get("/api/events", api.list_events),
        web.post("/api/events", api.create_event),
        web.get("/api/events/{event_id}", api.get_event),
        web.post("/api/events/{event_id}/cancel", api.cancel_event),
        web.get("/api/messages", api.list_messages),
        web.get(r"/api/messages/{pk:\d+}", api.get_message),
        web.get("/api/scenarios", api.list_scenarios),
        web.get("/api/runs", api.list_runs),
        web.post("/api/runs", api.start_run),
        web.get(r"/api/runs/{pk:\d+}", api.get_run),
        web.get(r"/api/runs/{pk:\d+}/report.json", api.run_report),
        web.post(r"/api/runs/{pk:\d+}/abort", api.abort_run),
        web.post(r"/api/runs/{pk:\d+}/respond", api.respond_run),
    ])
    return api
