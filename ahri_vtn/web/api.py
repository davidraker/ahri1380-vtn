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
from ..util import jsonable, parse_iso, utcnow

logger = logging.getLogger("ahri_vtn.api")


def _dumps(obj) -> str:
    return json.dumps(jsonable(obj), default=str)


def json_response(data, status: int = 200) -> web.Response:
    return web.Response(text=_dumps(data), status=status, content_type="application/json")


def error(message: str, status: int = 400) -> web.Response:
    return json_response({"error": message}, status=status)


async def read_body(request: web.Request) -> dict:
    if request.content_type == "application/json":
        try:
            return await request.json()
        except json.JSONDecodeError as err:
            raise web.HTTPBadRequest(text=f"Invalid JSON: {err}")
    data = await request.post()
    return {k: v for k, v in data.items()}


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


class Api:
    def __init__(self, vtn):
        self.vtn = vtn

    @property
    def q(self):
        return self.vtn.queries

    # ------------------------------------------------------------------ status

    async def status(self, request):
        cfg = self.vtn.config
        return json_response({"vtn_id": cfg.vtn_id, "openadr_url": cfg.openadr_url, "schema_mode": cfg.schema_mode,
                              "requested_poll_freq_seconds": cfg.requested_poll_freq_seconds,
                              "auto_accept_unknown_vens": cfg.auto_accept_unknown_vens,
                              "counts": self.q.dashboard_counts(), "now": utcnow()})

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
        return json_response(self.q.list_vens())

    async def create_ven(self, request):
        data = await read_body(request)
        if not data.get("ven_name"):
            return error("ven_name is required")
        try:
            ven = self.vtn.registry.add_ven(data["ven_name"], ven_id=data.get("ven_id") or None,
                                            notes=data.get("notes") or None, enabled=_bool(data.get("enabled"), True))
        except ValueError as err:
            return error(str(err), 409)
        return json_response(ven, 201)

    async def get_ven(self, request):
        ven = self.q.get_ven(int(request.match_info["pk"]))
        return json_response(ven) if ven else error("Not found", 404)

    async def update_ven(self, request):
        data = await read_body(request)
        fields = {}
        if "enabled" in data:
            fields["enabled"] = _bool(data["enabled"])
        for key in ("notes", "ven_name", "ven_id"):
            if key in data:
                fields[key] = data[key]
        ven = self.vtn.registry.update_ven(int(request.match_info["pk"]), **fields)
        return json_response(ven) if ven else error("Not found", 404)

    async def delete_ven(self, request):
        ok = self.vtn.registry.delete_ven(int(request.match_info["pk"]))
        return json_response({"ok": ok}) if ok else error("Not found", 404)

    async def request_reregistration(self, request):
        ok = self.vtn.registry.request_reregistration(int(request.match_info["pk"]))
        return json_response({"ok": ok}) if ok else error("VEN is not registered", 409)

    async def request_report(self, request):
        data = await read_body(request)
        try:
            rrid = self.vtn.reports.request_report(int(request.match_info["pk"]), spec_name=data.get("spec_name") or None,
                                                   report_specifier_id=data.get("report_specifier_id") or None,
                                                   one_shot=_bool(data.get("one_shot"), True),
                                                   granularity_seconds=_float(data.get("granularity_seconds")),
                                                   source="operator")
        except ValueError as err:
            return error(str(err), 409)
        return json_response({"report_request_id": rrid}, 201)

    async def cancel_report(self, request):
        ok = self.vtn.reports.cancel_report(request.match_info["rrid"])
        return json_response({"ok": ok}) if ok else error("Unknown report request", 404)

    async def samples(self, request):
        ven_pk = int(request.match_info["pk"])
        rows = self._samples(request, ven_pk)
        return json_response([{"ts": r.ts, "received_at": r.received_at, "r_id": r.r_id, "spec_rid": r.spec_rid,
                               "value": r.value, "report_request_id": r.report_request_id,
                               "resource_id": r.resource_id} for r in rows])

    def _samples(self, request, ven_pk):
        qs = request.query
        since = parse_iso(qs.get("since")) if qs.get("since") else None
        if since is None and qs.get("minutes"):
            since = utcnow() - timedelta(minutes=float(qs["minutes"]))
        until = parse_iso(qs.get("until")) if qs.get("until") else None
        return self.q.samples(ven_pk, spec_rid=qs.get("rid") or None, r_key=qs.get("r_key") or None,
                              since=since, until=until, limit=int(qs.get("limit", 5000)),
                              order=qs.get("order", "asc"))

    async def samples_csv(self, request):
        ven_pk = int(request.match_info["pk"])
        rows = self._samples(request, ven_pk)
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
        minutes = float(request.query.get("minutes", 120))
        since = utcnow() - timedelta(minutes=minutes)
        series = self.q.status_series(ven_pk, since)
        return json_response({rid: [{"ts": ts, "value": v} for ts, v in pts] for rid, pts in series.items()})

    async def findings(self, request):
        ven_pk = _int(request.query.get("ven"))
        run_pk = _int(request.query.get("run"))
        return json_response(self.q.findings(ven_pk=ven_pk, run_pk=run_pk))

    # ------------------------------------------------------------------ events

    async def list_events(self, request):
        return json_response(self.q.list_events(ven_pk=_int(request.query.get("ven")),
                                                limit=int(request.query.get("limit", 100)),
                                                status=request.query.get("status") or None))

    async def get_event(self, request):
        ev = self.q.get_event(request.match_info["event_id"])
        return json_response(ev) if ev else error("Not found", 404)

    async def create_event(self, request):
        data = await read_body(request)
        try:
            event_id = self.create_event_from_form(data)
        except (ValueError, KeyError) as err:
            return error(str(err))
        return json_response({"event_id": event_id}, 201)

    def create_event_from_form(self, data: dict) -> str:
        ven_pk = _int(data.get("ven_pk"))
        if ven_pk is None:
            raise ValueError("ven_pk is required")
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
        return self.vtn.events.create_event(
            ven_pk, preset, start=start, duration=duration, payload=_float(data.get("payload")),
            mito=_int(data.get("mito")), price_multipliers=multipliers, price_interval=price_interval,
            test_event=_bool(data.get("test_event")), vtn_comment=data.get("vtn_comment") or None,
            ramp_up=ramp_up, source=data.get("source") or "operator", label=data.get("label") or None)

    async def cancel_event(self, request):
        ok = self.vtn.events.cancel_event(request.match_info["event_id"])
        return json_response({"ok": ok}) if ok else error("Event not found or already finished", 409)

    # ------------------------------------------------------------------ messages

    async def list_messages(self, request):
        qs = request.query
        return json_response(self.q.list_messages(ven_id=qs.get("ven_id") or None, message_type=qs.get("type") or None,
                                                  direction=qs.get("direction") or None,
                                                  violations_only=_bool(qs.get("violations")),
                                                  page=int(qs.get("page", 1)), page_size=int(qs.get("page_size", 50))))

    async def get_message(self, request):
        m = self.q.get_message(int(request.match_info["pk"]))
        return json_response(m) if m else error("Not found", 404)

    # ------------------------------------------------------------------ conformance

    async def list_scenarios(self, request):
        return json_response(self.vtn.conformance.list_scenarios())

    async def list_runs(self, request):
        return json_response(self.q.list_runs(limit=int(request.query.get("limit", 100))))

    async def get_run(self, request):
        run = self.q.get_run(int(request.match_info["pk"]))
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
            run_pk = self.vtn.conformance.start_run(data.get("scenario_id"), _int(data.get("ven_pk")),
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
        ok = self.vtn.conformance.respond(int(request.match_info["pk"]), data.get("answer") or "Continue",
                                          note=data.get("note") or None)
        return json_response({"ok": ok}) if ok else error("No prompt is waiting for this run", 409)

    async def run_report(self, request):
        run = self.q.get_run(int(request.match_info["pk"]))
        if run is None:
            return error("Not found", 404)
        return web.Response(text=_dumps(run), content_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="run{run["id"]}.json"'})

    # ------------------------------------------------------------------ live stream

    async def stream(self, request):
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


def setup_api(vtn):
    api = Api(vtn)
    vtn.api = api
    app = vtn.server.app
    app.add_routes([
        web.get("/api/status", api.status),
        web.get("/api/presets", api.presets),
        web.get("/api/spec", api.spec_tables),
        web.get("/api/stream", api.stream),
        web.get("/api/vens", api.list_vens),
        web.post("/api/vens", api.create_ven),
        web.get("/api/vens/{pk}", api.get_ven),
        web.patch("/api/vens/{pk}", api.update_ven),
        web.delete("/api/vens/{pk}", api.delete_ven),
        web.post("/api/vens/{pk}/request-reregistration", api.request_reregistration),
        web.post("/api/vens/{pk}/request-report", api.request_report),
        web.get("/api/vens/{pk}/samples", api.samples),
        web.get("/api/vens/{pk}/samples.csv", api.samples_csv),
        web.get("/api/vens/{pk}/series", api.series),
        web.post("/api/report-requests/{rrid}/cancel", api.cancel_report),
        web.get("/api/findings", api.findings),
        web.get("/api/events", api.list_events),
        web.post("/api/events", api.create_event),
        web.get("/api/events/{event_id}", api.get_event),
        web.post("/api/events/{event_id}/cancel", api.cancel_event),
        web.get("/api/messages", api.list_messages),
        web.get("/api/messages/{pk}", api.get_message),
        web.get("/api/scenarios", api.list_scenarios),
        web.get("/api/runs", api.list_runs),
        web.post("/api/runs", api.start_run),
        web.get("/api/runs/{pk}", api.get_run),
        web.get("/api/runs/{pk}/report.json", api.run_report),
        web.post("/api/runs/{pk}/abort", api.abort_run),
        web.post("/api/runs/{pk}/respond", api.respond_run),
    ])
    return api
