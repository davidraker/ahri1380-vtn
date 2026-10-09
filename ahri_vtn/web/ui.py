"""
Server-rendered HTML UI (Jinja2) under /ui. Forms post back to /ui/... endpoints which call the
same handlers as the JSON API, then redirect.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import spec
from ..targets import Selection
from ..util import parse_iso, utcnow
from .api import read_body, selection_from, _bool, _float, _int

logger = logging.getLogger("ahri_vtn.ui")

HERE = Path(__file__).parent


# ---------------------------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------------------------

def fmt_ts(value, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if value in (None, ""):
        return "-"
    if isinstance(value, str):
        try:
            value = parse_iso(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime(fmt) + "Z"
    return str(value)


def fmt_time(value) -> str:
    return fmt_ts(value, "%H:%M:%S")


def fmt_ago(value) -> str:
    if value in (None, ""):
        return "never"
    if isinstance(value, str):
        value = parse_iso(value)
    delta = utcnow() - value
    s = int(delta.total_seconds())
    if s < 0:
        return f"in {-s}s"
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def fmt_duration(value) -> str:
    if value in (None, ""):
        return "-"
    s = int(float(value))
    if s < 60:
        return f"{s}s"
    if s % 3600 == 0:
        return f"{s // 3600}h"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s // 60}m{s % 60:02d}s"


def fmt_num(value, digits: int = 1) -> str:
    if value is None or value == "":
        return "-"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f.is_integer():
        return str(int(f))
    return f"{f:.{digits}f}"


def op_state_name(value) -> str:
    if value is None:
        return "-"
    return spec.operational_state_name(value)


def status_class(status: str) -> str:
    return {"passed": "ok", "pass": "ok", "failed": "bad", "fail": "bad", "running": "run", "active": "run",
            "manual": "warn", "warning": "warn", "needs_review": "warn", "waiting": "warn", "info": "muted",
            "aborted": "muted", "error": "bad", "cancelled": "muted", "completed": "muted", "far": "info",
            "near": "info", "optIn": "ok", "optOut": "bad", "created": "info", "queued": "muted"}.get(status or "", "")


def svg_series(points: list, width: int = 640, height: int = 120, y_min: Optional[float] = None,
               y_max: Optional[float] = None, t_min: Optional[datetime] = None, t_max: Optional[datetime] = None,
               step: bool = False) -> dict:
    """Return polyline points for an SVG chart from [(datetime, value)] (numeric values only)."""
    pts = [(ts, float(v)) for ts, v in points if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not pts:
        return {"points": "", "y_min": 0, "y_max": 1, "labels": []}
    t_min = t_min or pts[0][0]
    t_max = t_max or pts[-1][0]
    if t_max <= t_min:
        t_max = t_min + timedelta(seconds=1)
    values = [v for _, v in pts]
    lo = min(values) if y_min is None else y_min
    hi = max(values) if y_max is None else y_max
    if hi <= lo:
        hi = lo + 1
    coords = []
    span = (t_max - t_min).total_seconds()
    last_y = None
    for ts, v in pts:
        x = (ts - t_min).total_seconds() / span * (width - 10) + 5
        y = height - 5 - (v - lo) / (hi - lo) * (height - 10)
        if step and last_y is not None:
            coords.append(f"{x:.1f},{last_y:.1f}")
        coords.append(f"{x:.1f},{y:.1f}")
        last_y = y
    labels = [(5, fmt_time(t_min)), (width - 60, fmt_time(t_max))]
    return {"points": " ".join(coords), "y_min": lo, "y_max": hi, "labels": labels, "width": width, "height": height}


# ---------------------------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------------------------

class Ui:
    def __init__(self, vtn):
        self.vtn = vtn
        self.env = Environment(loader=FileSystemLoader(str(HERE / "templates")),
                               autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
        self.env.filters.update({"ts": fmt_ts, "time": fmt_time, "ago": fmt_ago, "duration": fmt_duration,
                                 "num": fmt_num, "op_state": op_state_name, "status_class": status_class})
        self.env.globals.update({"spec": spec, "presets": spec.EVENT_PRESETS, "now": utcnow})

    def selector(self, selected: Optional[list] = None) -> dict:
        """Context for the target selector widget."""
        return {"tree": self.q.selector_tree(), "selected": selected or []}

    @property
    def q(self):
        return self.vtn.queries

    def render(self, template: str, request: web.Request, **context) -> web.Response:
        flash = request.query.get("flash")
        flash_kind = request.query.get("kind", "info")
        html = self.env.get_template(template).render(request=request, config=self.vtn.config, flash=flash,
                                                      flash_kind=flash_kind, path=request.path, **context)
        return web.Response(text=html, content_type="text/html")

    @staticmethod
    def redirect(path: str, flash: Optional[str] = None, kind: str = "info") -> web.HTTPFound:
        if flash:
            path = f"{path}{'&' if '?' in path else '?'}{urlencode({'flash': flash, 'kind': kind})}"
        return web.HTTPFound(path)

    # ------------------------------------------------------------------ pages

    async def index(self, request):
        raise web.HTTPFound("/ui/")

    async def dashboard(self, request):
        return self.render("dashboard.html", request, vens=self.q.list_vens(), counts=self.q.dashboard_counts(),
                           events=self.q.list_events(limit=10), runs=self.q.list_runs(limit=5),
                           recent=list(self.vtn.bus.history)[-15:][::-1])

    async def ven_page(self, request):
        ven_pk = int(request.match_info["pk"])
        ven = self.q.get_ven(ven_pk)
        if ven is None:
            raise web.HTTPNotFound(text="Unknown VEN")
        minutes = float(request.query.get("minutes", 120))
        since = utcnow() - timedelta(minutes=minutes)
        series = self.q.status_series(ven_pk, since)
        rated = ven.get("rated_power")
        now = utcnow()
        charts = {
            "power": svg_series(series["Inst Power"], y_min=0, y_max=max(float(rated or 0) * 1.1, 1.0) if rated else None,
                                t_min=since, t_max=now),
            "state": svg_series(series["Operational State"], y_min=0, y_max=14, t_min=since, t_max=now, step=True),
            "override": svg_series(series["Override State"], y_min=0, y_max=1, t_min=since, t_max=now, step=True),
        }
        recent_samples = self.q.samples(ven_pk, limit=60, order="desc")
        return self.render("ven.html", request, ven=ven, charts=charts, minutes=minutes, recent_samples=recent_samples,
                           rated=rated, scenarios=self.vtn.conformance.list_scenarios(),
                           **self.selector([f"ven:{ven_pk}"]))

    async def resource_page(self, request):
        ven_pk = int(request.match_info["pk"])
        resource_id = request.match_info["rid"]
        res = self.q.get_resource(ven_pk, resource_id)
        if res is None:
            raise web.HTTPNotFound(text="Unknown resource")
        minutes = float(request.query.get("minutes", 120))
        since = utcnow() - timedelta(minutes=minutes)
        series = self.q.status_series(ven_pk, since, resource_id=resource_id)
        rated = res.get("rated_power")
        now = utcnow()
        charts = {
            "power": svg_series(series["Inst Power"], y_min=0, y_max=max(float(rated or 0) * 1.1, 1.0) if rated else None,
                                t_min=since, t_max=now),
            "state": svg_series(series["Operational State"], y_min=0, y_max=14, t_min=since, t_max=now, step=True),
            "override": svg_series(series["Override State"], y_min=0, y_max=1, t_min=since, t_max=now, step=True),
        }
        recent_samples = self.q.samples(ven_pk, limit=60, order="desc", resource_id=resource_id)
        return self.render("resource.html", request, res=res, charts=charts, minutes=minutes, rated=rated,
                           recent_samples=recent_samples, scenarios=self.vtn.conformance.list_scenarios(),
                           **self.selector([f"res:{ven_pk}:{resource_id}"]))

    async def groups_page(self, request):
        edit = self.vtn.groups.get_group(int(request.query["edit"])) if request.query.get("edit") else None
        return self.render("groups.html", request, groups=self.vtn.groups.list_groups(), edit=edit,
                           **self.selector(edit["form_values"] if edit else []))

    async def save_group(self, request):
        data = await read_body(request)
        group_pk = _int(data.get("group_pk"))
        try:
            sel = Selection.from_form(data.get("targets") or [])
            group = self.vtn.groups.save_group(data.get("name"), sel, description=data.get("description"),
                                               signal_group_id=data.get("signal_group_id"), group_pk=group_pk)
        except ValueError as err:
            raise self.redirect("/ui/groups", str(err), "bad")
        raise self.redirect("/ui/groups", f"Group '{group['name']}' saved", "ok")

    async def delete_group(self, request):
        self.vtn.groups.delete_group(int(request.match_info["pk"]))
        raise self.redirect("/ui/groups", "Group deleted", "ok")

    async def resource_notes(self, request):
        ven_pk = int(request.match_info["pk"])
        rid = request.match_info["rid"]
        data = await read_body(request)
        self.q.update_resource(ven_pk, rid, label=data.get("label") or None, notes=data.get("notes") or None)
        raise self.redirect(f"/ui/vens/{ven_pk}/resources/{rid}", "Saved", "ok")

    async def request_reports(self, request):
        data = await read_body(request)
        back = data.get("back") or "/ui/events"
        try:
            result = self.vtn.reports.request_reports(selection_from(data), data.get("spec_name") or "x-AHRI_STATUS",
                                                      one_shot=_bool(data.get("one_shot"), True),
                                                      granularity_seconds=_float(data.get("granularity_seconds")))
        except ValueError as err:
            raise self.redirect(back, str(err), "bad")
        msg = f"{len(result['report_request_ids'])} report request(s) queued"
        if result["errors"]:
            msg += "; " + "; ".join(result["errors"])
        raise self.redirect(back, msg, "ok" if not result["errors"] else "bad")

    async def cancel_dispatch(self, request):
        data = await read_body(request)
        count = self.vtn.events.cancel_dispatch(int(request.match_info["pk"]))
        raise self.redirect(data.get("back") or "/ui/events", f"{count} event(s) cancelled", "ok")

    async def events_page(self, request):
        ven_pk = _int(request.query.get("ven"))
        selected = [f"ven:{ven_pk}"] if ven_pk else []
        if request.query.get("group"):
            selected.append(f"group:{request.query['group']}")
        return self.render("events.html", request, dispatches=self.q.list_dispatches(limit=100),
                           loose_events=[e for e in self.q.list_events(ven_pk=ven_pk, limit=200) if not e["dispatch_pk"]],
                           **self.selector(selected))

    async def messages_page(self, request):
        qs = request.query
        page = self.q.list_messages(ven_id=qs.get("ven_id") or None, message_type=qs.get("type") or None,
                                    direction=qs.get("direction") or None, violations_only=_bool(qs.get("violations")),
                                    page=int(qs.get("page", 1)), page_size=int(qs.get("page_size", 50)))
        return self.render("messages.html", request, page=page, filters=dict(qs), vens=self.q.list_vens())

    async def message_page(self, request):
        m = self.q.get_message(int(request.match_info["pk"]))
        if m is None:
            raise web.HTTPNotFound(text="Unknown message")
        return self.render("message.html", request, m=m, pretty=_pretty_xml(m.get("raw_xml")))

    async def runs_page(self, request):
        selected = []
        if request.query.get("ven"):
            selected.append(f"ven:{request.query['ven']}")
        if request.query.get("resource") and request.query.get("ven"):
            selected = [f"res:{request.query['ven']}:{request.query['resource']}"]
        if request.query.get("group"):
            selected.append(f"group:{request.query['group']}")
        return self.render("runs.html", request, runs=self.q.list_runs(),
                           scenarios=self.vtn.conformance.list_scenarios(),
                           selected_scenario=request.query.get("scenario"), **self.selector(selected))

    async def run_page(self, request):
        run = self.q.get_run(int(request.match_info["pk"]))
        if run is None:
            raise web.HTTPNotFound(text="Unknown run")
        prompt = self.vtn.conformance.pending_prompt(run["id"])
        return self.render("run.html", request, run=run, prompt=prompt)

    async def findings_page(self, request):
        ven_pk = _int(request.query.get("ven"))
        return self.render("findings.html", request, findings=self.q.findings(ven_pk=ven_pk), vens=self.q.list_vens(),
                           selected_ven=ven_pk)

    # ------------------------------------------------------------------ form actions

    async def add_ven(self, request):
        data = await read_body(request)
        try:
            self.vtn.registry.add_ven(data.get("ven_name", "").strip(), ven_id=(data.get("ven_id") or "").strip() or None,
                                      notes=data.get("notes") or None)
        except ValueError as err:
            raise self.redirect("/ui/", str(err), "bad")
        raise self.redirect("/ui/", f"VEN '{data.get('ven_name')}' added", "ok")

    async def toggle_ven(self, request):
        ven_pk = int(request.match_info["pk"])
        ven = self.q.get_ven(ven_pk)
        if ven is None:
            raise web.HTTPNotFound()
        self.vtn.registry.update_ven(ven_pk, enabled=not ven["enabled"])
        raise self.redirect(f"/ui/vens/{ven_pk}", "VEN " + ("enabled" if not ven["enabled"] else "disabled"), "ok")

    async def delete_ven(self, request):
        ven_pk = int(request.match_info["pk"])
        self.vtn.registry.delete_ven(ven_pk)
        raise self.redirect("/ui/", "VEN deleted", "ok")

    async def ven_notes(self, request):
        ven_pk = int(request.match_info["pk"])
        data = await read_body(request)
        self.vtn.registry.update_ven(ven_pk, notes=data.get("notes", ""))
        raise self.redirect(f"/ui/vens/{ven_pk}", "Notes saved", "ok")

    async def request_report(self, request):
        ven_pk = int(request.match_info["pk"])
        data = await read_body(request)
        back = data.get("back") or f"/ui/vens/{ven_pk}"
        try:
            result = self.vtn.reports.request_reports(selection_from(data, default_ven_pk=ven_pk),
                                                      data.get("spec_name") or "x-AHRI_STATUS",
                                                      one_shot=_bool(data.get("one_shot"), True),
                                                      granularity_seconds=_float(data.get("granularity_seconds")))
        except ValueError as err:
            raise self.redirect(back, str(err), "bad")
        msg = f"{len(result['report_request_ids'])} report request(s) queued; delivered on the VEN's next poll"
        if result["errors"]:
            msg += "; " + "; ".join(result["errors"])
        raise self.redirect(back, msg, "ok" if not result["errors"] else "bad")

    async def cancel_report(self, request):
        data = await read_body(request)
        ven_pk = int(request.match_info["pk"])
        ok = self.vtn.reports.cancel_report(data.get("report_request_id"), _bool(data.get("report_to_follow")))
        raise self.redirect(f"/ui/vens/{ven_pk}", "Cancellation queued" if ok else "Unknown report request",
                            "ok" if ok else "bad")

    async def request_reregistration(self, request):
        ven_pk = int(request.match_info["pk"])
        ok = self.vtn.registry.request_reregistration(ven_pk)
        raise self.redirect(f"/ui/vens/{ven_pk}", "Re-registration requested" if ok else "VEN is not registered",
                            "ok" if ok else "bad")

    async def create_event(self, request):
        data = await read_body(request)
        back = data.get("back") or "/ui/events"
        try:
            result = self.vtn.api.create_event_from_form(data)
        except (ValueError, KeyError) as err:
            raise self.redirect(back, f"Could not create event: {err}", "bad")
        n = len(result["events"])
        raise self.redirect(back, f"Event queued to {n} VEN{'s' if n != 1 else ''} (dispatch {result['dispatch_pk']})", "ok")

    async def cancel_event(self, request):
        event_id = request.match_info["event_id"]
        data = await read_body(request)
        ok = self.vtn.events.cancel_event(event_id)
        raise self.redirect(data.get("back") or "/ui/events", "Event cancelled" if ok else "Event not active",
                            "ok" if ok else "bad")

    async def start_run(self, request):
        data = await read_body(request)
        params = {}
        for key, value in data.items():
            if key.startswith("param_") and value not in (None, ""):
                try:
                    params[key[6:]] = float(value)
                except ValueError:
                    params[key[6:]] = value
        try:
            selection = selection_from(data)
            run_pk = self.vtn.conformance.start_run(data.get("scenario_id"), selection=selection,
                                                    mode=data.get("mode") or "cooling", params=params,
                                                    name=data.get("name") or None)
        except (ValueError, KeyError) as err:
            raise self.redirect("/ui/runs", f"Could not start run: {err}", "bad")
        raise self.redirect(f"/ui/runs/{run_pk}")

    async def abort_run(self, request):
        run_pk = int(request.match_info["pk"])
        await self.vtn.conformance.abort_run(run_pk)
        raise self.redirect(f"/ui/runs/{run_pk}", "Abort requested", "ok")

    async def respond_run(self, request):
        run_pk = int(request.match_info["pk"])
        data = await read_body(request)
        self.vtn.conformance.respond(run_pk, data.get("answer") or "Continue", note=data.get("note") or None)
        raise self.redirect(f"/ui/runs/{run_pk}")

    async def run_notes(self, request):
        run_pk = int(request.match_info["pk"])
        data = await read_body(request)
        self.vtn.conformance.set_notes(run_pk, data.get("operator_notes", ""))
        raise self.redirect(f"/ui/runs/{run_pk}", "Notes saved", "ok")


def _pretty_xml(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        from lxml import etree
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.fromstring(raw.encode("utf-8"), parser)
        return etree.tostring(tree, pretty_print=True, encoding="unicode")
    except Exception:   # noqa: BLE001
        return raw


@web.middleware
async def _basic_auth_factory(request, handler):
    return await handler(request)


def make_basic_auth_middleware(user: str, password: str):
    expected = base64.b64encode(f"{user}:{password}".encode()).decode()

    @web.middleware
    async def middleware(request, handler):
        if request.path.startswith(("/ui", "/api")):
            header = request.headers.get("Authorization", "")
            if header != f"Basic {expected}":
                return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="AHRI VTN"'})
        return await handler(request)
    return middleware


def setup_ui(vtn):
    ui = Ui(vtn)
    vtn.ui = ui
    app = vtn.server.app
    if vtn.config.ui.basic_auth_user and vtn.config.ui.basic_auth_password:
        app.middlewares.append(make_basic_auth_middleware(vtn.config.ui.basic_auth_user,
                                                          vtn.config.ui.basic_auth_password))
    app.add_routes([
        web.get("/", ui.index),
        web.get("/ui", ui.index),
        web.get("/ui/", ui.dashboard),
        web.get("/ui/vens/{pk}", ui.ven_page),
        web.get("/ui/vens/{pk}/resources/{rid}", ui.resource_page),
        web.post("/ui/vens/{pk}/resources/{rid}/notes", ui.resource_notes),
        web.get("/ui/groups", ui.groups_page),
        web.post("/ui/groups", ui.save_group),
        web.post("/ui/groups/{pk}/delete", ui.delete_group),
        web.post("/ui/report-requests", ui.request_reports),
        web.post("/ui/dispatches/{pk}/cancel", ui.cancel_dispatch),
        web.get("/ui/events", ui.events_page),
        web.get("/ui/messages", ui.messages_page),
        web.get("/ui/messages/{pk}", ui.message_page),
        web.get("/ui/runs", ui.runs_page),
        web.get("/ui/runs/{pk}", ui.run_page),
        web.get("/ui/findings", ui.findings_page),
        web.post("/ui/vens", ui.add_ven),
        web.post("/ui/vens/{pk}/toggle", ui.toggle_ven),
        web.post("/ui/vens/{pk}/delete", ui.delete_ven),
        web.post("/ui/vens/{pk}/notes", ui.ven_notes),
        web.post("/ui/vens/{pk}/request-report", ui.request_report),
        web.post("/ui/vens/{pk}/cancel-report", ui.cancel_report),
        web.post("/ui/vens/{pk}/request-reregistration", ui.request_reregistration),
        web.post("/ui/events", ui.create_event),
        web.post("/ui/events/{event_id}/cancel", ui.cancel_event),
        web.post("/ui/runs", ui.start_run),
        web.post("/ui/runs/{pk}/abort", ui.abort_run),
        web.post("/ui/runs/{pk}/respond", ui.respond_run),
        web.post("/ui/runs/{pk}/notes", ui.run_notes),
        web.static("/ui/static", str(HERE / "static"), show_index=False),
    ])
    return ui
