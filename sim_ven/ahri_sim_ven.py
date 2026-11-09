"""
Simulated AHRI 1380 VEN built on the OpenLEADR client.

One VEN fronts one or more HVAC units (resources). Each unit registers its own Table 5
(x-AHRI_STATIC) and Table 6 (x-AHRI_STATUS) reports, responds to the Table 8 events with a very
simple power model, honours eiTarget venID / resourceID / groupID addressing, and can simulate a
customer override (oadrCreateOpt). Intended for exercising and testing the AHRI VTN test tool.

    python -m sim_ven.ahri_sim_ven --vtn-url https://vtn.lab/OpenADR2/Simple/2.0b --name sim-hvac \\
        --cert certs/ven-sim-hvac.crt --key certs/ven-sim-hvac.key --ca-file certs/lab-ca.crt --resources hvac-1,hvac-2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from openleadr import OpenADRClient, enums, utils as oadr_utils

logger = logging.getLogger("ahri_sim_ven")

# Table 7 operational states used by the simulator
IDLE_NORMAL, RUNNING_NORMAL, RUNNING_CURTAILED, RUNNING_HEIGHTENED, IDLE_CURTAILED = 0, 1, 2, 3, 4
ERROR, IDLE_HEIGHTENED, IDLE_OPTED_OUT, RUNNING_OPTED_OUT = 5, 6, 11, 12
RUNNING_PRICE, IDLE_PRICE = 13, 14


def utcnow():
    return datetime.now(timezone.utc)


class SimUnit:
    """One HVAC unit: static data plus the dynamic DR state."""

    def __init__(self, resource_id: str, rated_power_w: float, benchmark_cooling_w: Optional[float],
                 benchmark_heating_w: Optional[float], device_type: int, unloading_type: int, vendor_name: str,
                 device_revision: str, model_number: str, running: bool, ramp_seconds: float):
        self.resource_id = resource_id
        self.rated_power = float(rated_power_w)
        self.benchmark_cooling = float(benchmark_cooling_w or rated_power_w)
        self.benchmark_heating = float(benchmark_heating_w or rated_power_w * 0.9)
        self.device_type = device_type
        self.unloading_type = unloading_type
        self.vendor_name, self.device_revision, self.model_number = vendor_name, device_revision, model_number
        self.ramp_seconds = ramp_seconds
        self.running = running                  # thermostat demand present
        self.level: int = 0                     # active SIMPLE level (0 = none)
        self.heightened = False                 # preconditioning
        self.override = False
        self.mito: Optional[int] = None
        self.prices: list = []
        self.active_event: Optional[dict] = None
        self.events: dict[str, dict] = {}
        self.error = False
        self.power = 0.0
        self.target_power = 0.0
        self.op_state = IDLE_NORMAL
        self.history: list = []                 # (ts, level, power, op_state)
        self.mode = "cooling"

    @property
    def normal_power(self) -> float:
        return self.benchmark_cooling * 0.95 if self.mode == "cooling" else self.benchmark_heating * 0.95

    def recompute(self):
        """Derive target power and operational state from the current DR state."""
        if self.error:
            self.target_power, self.op_state = 0.0, ERROR
            return
        if not self.running:
            self.target_power = 60.0   # controls
            if self.override:
                self.op_state = IDLE_OPTED_OUT
            elif self.level:
                self.op_state = IDLE_CURTAILED
            elif self.heightened:
                self.op_state = IDLE_HEIGHTENED
            else:
                self.op_state = IDLE_NORMAL
            return
        if self.override:
            self.target_power, self.op_state = self.normal_power, RUNNING_OPTED_OUT
        elif self.level == 1:
            self.target_power, self.op_state = self.rated_power * 0.68, RUNNING_CURTAILED
        elif self.level == 2:
            self.target_power, self.op_state = self.rated_power * 0.38, RUNNING_CURTAILED
        elif self.level == 3:
            self.target_power, self.op_state = 150.0, IDLE_CURTAILED
        elif self.heightened:
            self.target_power, self.op_state = self.rated_power * 1.0, RUNNING_HEIGHTENED
        else:
            self.target_power, self.op_state = self.normal_power, RUNNING_NORMAL

    def has_active_event(self, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        return any(not e.get("cancelled") and e["start"] <= now < e["end"] for e in self.events.values())

    def tick(self, now: datetime):
        """Apply the event schedule and ramp the power towards the target (5 s ramp, Section 6.1.13.1)."""
        level, heightened, active = 0, False, None
        for event in list(self.events.values()):
            if event.get("cancelled"):
                continue
            if event["start"] <= now < event["end"]:
                active = event
                if event.get("level"):
                    level = max(level, event["level"])
                if event.get("precondition"):
                    heightened = True
        if active is not None and (self.active_event is None or self.active_event["event_id"] != active["event_id"]):
            logger.info(f"[{self.resource_id}] Event {active['event_id']} is now active (level {active.get('level')})")
        if active is None and self.active_event is not None:
            logger.info(f"[{self.resource_id}] Event {self.active_event['event_id']} ended, resuming normal operation")
            self.override = False
        self.active_event = active
        self.level, self.heightened = level, heightened
        self.recompute()
        step = max(abs(self.target_power - self.power) / max(self.ramp_seconds, 0.1), 50.0)
        if self.power < self.target_power:
            self.power = min(self.target_power, self.power + step)
        elif self.power > self.target_power:
            self.power = max(self.target_power, self.power - step)
        self.history.append((now, self.level, round(self.power, 1), self.op_state))
        if len(self.history) > 10000:
            del self.history[:5000]

    def state(self) -> dict:
        return {"resource_id": self.resource_id, "running": self.running, "level": self.level,
                "override": self.override, "mito": self.mito, "power": round(self.power, 1),
                "op_state": self.op_state, "active_event": self.active_event["event_id"] if self.active_event else None,
                "events": list(self.events), "prices": len(self.prices), "unloading_type": self.unloading_type}


class AhriSimVen:
    def __init__(self, ven_name: str, vtn_url: str, *, resource_id: str = "hvac-1",
                 resource_ids: Optional[list] = None, rated_power_w: float = 4800.0,
                 benchmark_cooling_w: Optional[float] = None, benchmark_heating_w: Optional[float] = None,
                 device_type: int = 1, unloading_type: int = 0, vendor_name: str = "Acme HVAC",
                 device_revision: str = "R3", model_number: str = "HP-48K-2S", mode: str = "cooling",
                 status_interval: timedelta = timedelta(minutes=1), static_interval: timedelta = timedelta(hours=1),
                 text_mode: str = "payload", report_name_style: str = "x-", ven_id: Optional[str] = None,
                 running: bool = True, ramp_seconds: float = 5.0, group_ids: Optional[list] = None,
                 unit_overrides: Optional[dict] = None, cert: Optional[str] = None, key: Optional[str] = None,
                 ca_file: Optional[str] = None, vtn_fingerprint: Optional[str] = None, check_hostname: bool = True,
                 sign_messages: bool = False, ssl_context=None):
        """
        :param resource_ids: the units behind this VEN (default: [resource_id]). Each gets its own
                             x-AHRI_STATIC / x-AHRI_STATUS report.
        :param unloading_type: 0 = continuously variable (responds to all levels),
                               1 = discretely variable / two-capacity (NAKs Critical Curtailment with 460)
        :param text_mode: how the ASCII items of Table 5 are sent: 'payload' puts the string in the
                          payloadFloat value (schema-invalid, tolerated by the VTN in lenient mode),
                          'description' puts it in the metadata itemDescription and sends 0.
        :param report_name_style: 'x-' registers the reports as x-AHRI_STATIC / x-AHRI_STATUS;
                          'telemetry' registers the status report as TELEMETRY_STATUS with a
                          reportSpecifierID of '<resource>-x-AHRI_STATUS'.
        :param group_ids: eiTarget groupID values this VEN has been configured to belong to.
        :param unit_overrides: {resource_id: {attribute: value}} to vary units (e.g. unloading_type).
        :param cert, key: PEM client certificate and key for mutual TLS (OpenADR 2.0b 9.2.1.1).
        :param ca_file: PEM bundle used to verify the VTN server certificate.
        :param vtn_fingerprint: expected fingerprint of the VTN's signing certificate (signed VTNs only).
        :param check_hostname: verify that the VTN certificate matches the URL host name.
        :param sign_messages: sign outgoing messages with cert/key (OpenADR 2.0b 'High' security).
        :param ssl_context: a prebuilt client SSLContext (tests: restrict TLS version or cipher suite).
        """
        self.ven_name = ven_name
        self.mode = mode
        self.text_mode = text_mode
        self.group_ids = list(group_ids or [])
        self.units: dict[str, SimUnit] = {}
        for rid in (resource_ids or [resource_id]):
            unit = SimUnit(rid, rated_power_w, benchmark_cooling_w, benchmark_heating_w, device_type, unloading_type,
                           vendor_name, device_revision, model_number, running, ramp_seconds)
            unit.mode = mode
            for attr, value in (unit_overrides or {}).get(rid, {}).items():
                setattr(unit, attr, value)
            unit.recompute()
            self.units[rid] = unit
        self._tick_task = None
        self.last_opt_response = None

        self.client = OpenADRClient(ven_name=ven_name, vtn_url=vtn_url, ven_id=ven_id, cert=cert, key=key,
                                    ca_file=ca_file, vtn_fingerprint=vtn_fingerprint, check_hostname=check_hostname,
                                    disable_signature=not sign_messages, show_fingerprint=False,
                                    ssl_context=ssl_context)
        self.signs_messages = sign_messages
        self.client.add_handler("on_event", self.on_event)
        self.client.add_handler("on_update_event", self.on_update_event)
        for unit in self.units.values():
            self._add_reports(unit, status_interval, static_interval, report_name_style)

    # ------------------------------------------------------------------ compatibility accessors (first unit)

    @property
    def unit(self) -> SimUnit:
        return next(iter(self.units.values()))

    @property
    def resource_id(self):
        return self.unit.resource_id

    def __getattr__(self, name):
        # level, override, power, op_state, mito, active_event, events, running, prices, rated_power, unloading_type ...
        if name in ("level", "override", "power", "op_state", "mito", "active_event", "events", "running",
                    "prices", "rated_power", "unloading_type", "error", "history"):
            return getattr(self.unit, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in ("running", "error", "override") and "units" in self.__dict__:
            for unit in self.units.values():
                setattr(unit, name, value)
                unit.recompute()
            return
        if name == "mode" and "units" in self.__dict__:
            for unit in self.units.values():
                unit.mode = value
                unit.recompute()
        object.__setattr__(self, name, value)

    # ------------------------------------------------------------------ reports

    def _add_reports(self, unit: SimUnit, status_interval, static_interval, style):
        static_name = "x-AHRI_STATIC"
        status_name = "x-AHRI_STATUS" if style == "x-" else "TELEMETRY_STATUS"
        static_spec = f"{unit.resource_id}-x-AHRI_STATIC"
        status_spec = f"{unit.resource_id}-x-AHRI_STATUS"
        static_rate = {"sampling_rate": static_interval, "report_duration": timedelta(hours=24)}
        status_rate = {"sampling_rate": status_interval, "report_duration": timedelta(hours=24)}

        def text_cb(getter):
            async def cb():
                return getter() if self.text_mode == "payload" else 0.0
            return cb

        numeric = [
            ("Size", "x_ahri_Rated_Load", "W", lambda: unit.rated_power),
            ("Device_Type", "x-ahri_Type", "x-ahri_Type", lambda: unit.device_type),
            ("Unloading_Type", "x-ahri_UnloadingType", "x-ahri_UnloadingType", lambda: unit.unloading_type),
            ("Benchmark_Power_Cooling", "Benchmark power for HVAC in cooling mode", "Watts", lambda: unit.benchmark_cooling),
            ("Benchmark_Power_Heating", "Benchmark power for HVAC in heating mode", "Watts", lambda: unit.benchmark_heating),
        ]
        for r_id, description, unit_str, getter in numeric:
            async def cb(getter=getter):
                return float(getter())
            self.client.add_report(callback=cb, resource_id=unit.resource_id, report_specifier_id=static_spec,
                                   report_name=static_name, r_id=r_id, measurement=description, unit=unit_str,
                                   **static_rate)
        texts = [("Vendor_Name", "Name of manufacturer", lambda: unit.vendor_name),
                 ("Device_Revision", "Revision number", lambda: unit.device_revision),
                 ("Model_Number", "Model number of HVAC system (ASCII)", lambda: unit.model_number)]
        for r_id, description, getter in texts:
            desc = getter() if self.text_mode == "description" else description
            self.client.add_report(callback=text_cb(getter), resource_id=unit.resource_id,
                                   report_specifier_id=static_spec, report_name=static_name, r_id=r_id,
                                   measurement=desc, unit="customUnit", **static_rate)

        async def inst_power():
            return round(unit.power, 1)

        async def op_state():
            return unit.op_state

        async def override_state():
            return 1 if unit.override else 0

        self.client.add_report(callback=inst_power, resource_id=unit.resource_id, report_specifier_id=status_spec,
                               report_name=status_name, r_id="Inst Power", measurement="x-ahri_InstPower", unit="W",
                               **status_rate)
        self.client.add_report(callback=op_state, resource_id=unit.resource_id, report_specifier_id=status_spec,
                               report_name=status_name, r_id="Operational State", measurement="x-ahri_OperationalState",
                               unit="Integer", **status_rate)
        self.client.add_report(callback=override_state, resource_id=unit.resource_id, report_specifier_id=status_spec,
                               report_name=status_name, r_id="Override State", measurement="x-ahri_OverrideState",
                               unit="Discrete", **status_rate)

    # ------------------------------------------------------------------ events

    def _parse_event(self, event: dict) -> dict:
        descriptor = event["event_descriptor"]
        active_period = event["active_period"]
        start = active_period["dtstart"]
        duration = active_period["duration"]
        parsed = {"event_id": descriptor["event_id"], "modification_number": descriptor.get("modification_number", 0),
                  "status": descriptor.get("event_status"), "start": start, "end": start + duration,
                  "level": None, "precondition": False, "mito": None, "prices": None,
                  "cancelled": descriptor.get("event_status") == "cancelled", "raw": event}
        for signal in event.get("event_signals", []):
            name, stype = signal.get("signal_name"), signal.get("signal_type")
            intervals = signal.get("intervals") or []
            first = intervals[0]["signal_payload"] if intervals else None
            if name in ("SIMPLE", "simple"):
                parsed["level"] = int(first) if first is not None else 0
                if parsed["level"] == 0:
                    parsed["level"] = None
            elif name == "LOAD_CONTROL" and stype == enums.SIGNAL_TYPE.X_LOAD_CONTROL_LEVEL_OFFSET:
                parsed["mito"] = int(first) if first is not None else None
                if not any(s.get("signal_name") in ("SIMPLE", "simple") for s in event.get("event_signals", [])):
                    parsed["precondition"] = True
            elif name == "ELECTRICITY_PRICE":
                parsed["prices"] = [(i["dtstart"], i["duration"], i["signal_payload"]) for i in intervals]
        return parsed

    def _addressed_units(self, event: dict):
        """Units an event applies to, from its eiTarget: resourceIDs select units, venID / our groupIDs
        select all, anything else nothing. Returns (units, matched_any_target)."""
        targets = event.get("targets") or []
        if not targets:
            return list(self.units.values()), True
        selected, matched = [], False
        for target in targets:
            if target.get("ven_id") and target["ven_id"] == self.client.ven_id:
                return list(self.units.values()), True
            if target.get("group_id") and target["group_id"] in self.group_ids:
                return list(self.units.values()), True
            if target.get("resource_id"):
                matched = True
                unit = self.units.get(str(target["resource_id"]))
                if unit is not None:
                    selected.append(unit)
            elif target.get("group_id") or target.get("ven_id"):
                matched = True
        return selected, matched

    async def on_event(self, event):
        parsed = self._parse_event(event)
        units, _ = self._addressed_units(event)
        logger.info(f"Received event {parsed['event_id']} level={parsed['level']} mito={parsed['mito']} "
                    f"precondition={parsed['precondition']} status={parsed['status']} "
                    f"for units {[u.resource_id for u in units]}")
        if not units:
            logger.info("Event targets none of our units: opting out with 462 Target mismatch")
            return "optOut", 462, "Target mismatch"
        if parsed["level"] == 2 and all(u.unloading_type == 1 for u in units):
            # Two-capacity systems cannot unload 60 %: NAK with 460 and take no action (Table 8)
            logger.info("Discretely variable unit(s): NAK Critical Curtailment with 460")
            return "optOut", 460, "Signal not supported"
        for unit in units:
            if parsed["mito"] is not None:
                unit.mito = parsed["mito"]
            if parsed["prices"] is not None:
                unit.prices = parsed["prices"]
            if parsed["cancelled"]:
                unit.events.pop(parsed["event_id"], None)
            elif not (parsed["level"] == 2 and unit.unloading_type == 1):
                unit.events[parsed["event_id"]] = parsed
        return "optIn"

    async def on_update_event(self, event):
        parsed = self._parse_event(event)
        logger.info(f"Event {parsed['event_id']} updated (mod {parsed['modification_number']}, status {parsed['status']})")
        if parsed["cancelled"]:
            for unit in self.units.values():
                if parsed["event_id"] in unit.events:
                    unit.events.pop(parsed["event_id"])
                    # A customer override ends with the event it overrode, unless another event is still active
                    if not unit.has_active_event():
                        unit.override = False
            return "optIn"
        return await self.on_event(event)

    # ------------------------------------------------------------------ customer override

    async def customer_override(self, event_id: Optional[str] = None, resource_id: Optional[str] = None,
                                opt_reason: str = enums.OPT_REASON.OVERRIDE_STATUS):
        """Simulate the customer overriding the event at the thermostat of one unit: oadrCreateOpt optOut."""
        unit = self.units[resource_id] if resource_id else next(
            (u for u in self.units.values() if u.active_event), self.unit)
        unit.override = True
        unit.recompute()
        if event_id is None and unit.active_event is not None:
            event_id = unit.active_event["event_id"]
        payload = {"opt_id": oadr_utils.generate_id(), "opt_type": enums.OPT.OPT_OUT, "opt_reason": opt_reason,
                   "ven_id": self.client.ven_id, "created_date_time": utcnow(), "request_id": oadr_utils.generate_id(),
                   "targets": [{"resource_id": unit.resource_id}]}
        if event_id is not None:
            event = unit.events.get(event_id)
            payload["event_id"] = event_id
            payload["modification_number"] = event["modification_number"] if event else 0
        message = self.client._create_message("oadrCreateOpt", **payload)
        self.last_opt_response = await self.client._perform_request("EiOpt", message)
        logger.info(f"[{unit.resource_id}] Sent customer override opt for event {event_id}: "
                    f"{self.last_opt_response[0] if self.last_opt_response else None}")
        return payload["opt_id"], self.last_opt_response

    async def clear_override(self, opt_id: Optional[str] = None, resource_id: Optional[str] = None):
        for unit in ([self.units[resource_id]] if resource_id else self.units.values()):
            unit.override = False
            unit.recompute()
        if opt_id:
            message = self.client._create_message("oadrCancelOpt", opt_id=opt_id, ven_id=self.client.ven_id,
                                                  request_id=oadr_utils.generate_id())
            return await self.client._perform_request("EiOpt", message)

    # ------------------------------------------------------------------ lifecycle

    async def _tick(self):
        try:
            while True:
                now = utcnow()
                for unit in self.units.values():
                    unit.tick(now)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return

    async def start(self):
        self._tick_task = asyncio.get_event_loop().create_task(self._tick())
        await self.client.run()

    async def stop(self):
        if self._tick_task:
            self._tick_task.cancel()
        await self.client.stop()

    @property
    def ven_id(self):
        return self.client.ven_id

    def state(self) -> dict:
        first = self.unit.state()
        return {"ven_id": self.client.ven_id, "registration_id": self.client.registration_id, "mode": self.mode,
                "group_ids": self.group_ids, **{k: v for k, v in first.items() if k != "resource_id"},
                "units": {rid: u.state() for rid, u in self.units.items()}}


# ---------------------------------------------------------------------------------------------
# Small HTTP control interface for manual lab use
# ---------------------------------------------------------------------------------------------

def _control_app(sim: AhriSimVen):
    from aiohttp import web

    def dumps(o):
        return json.dumps(o, default=str)

    async def state(request):
        return web.json_response(sim.state(), dumps=dumps)

    async def override(request):
        data = await request.post()
        opt_id, _ = await sim.customer_override(resource_id=data.get("resource") or request.query.get("resource"))
        return web.json_response({"ok": True, "opt_id": opt_id})

    async def clear(request):
        data = await request.post()
        await sim.clear_override(resource_id=data.get("resource") or request.query.get("resource"))
        return web.json_response({"ok": True})

    async def set_state(request):
        data = await request.post()
        units = [sim.units[data["resource"]]] if data.get("resource") else list(sim.units.values())
        for unit in units:
            if "running" in data:
                unit.running = data["running"].lower() in ("1", "true", "yes", "on")
            if "error" in data:
                unit.error = data["error"].lower() in ("1", "true", "yes", "on")
            if "mode" in data:
                unit.mode = data["mode"]
            unit.recompute()
        return web.json_response(sim.state(), dumps=dumps)

    app = web.Application()
    app.add_routes([web.get("/state", state), web.post("/override", override), web.post("/clear-override", clear),
                    web.post("/set", set_state)])
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Simulated AHRI 1380 VEN")
    parser.add_argument("--vtn-url", default="https://localhost/OpenADR2/Simple/2.0b")
    parser.add_argument("--name", default="sim-hvac")
    parser.add_argument("--resource-id", default="hvac-1")
    parser.add_argument("--resources", help="comma separated resource ids for a multi-unit VEN (overrides --resource-id)")
    parser.add_argument("--rated-power", type=float, default=4800.0)
    parser.add_argument("--device-type", type=int, default=1, help="0=cooling only, 1=heat pump")
    parser.add_argument("--unloading-type", type=int, default=0, help="0=continuously variable, 1=discretely variable")
    parser.add_argument("--mode", choices=["cooling", "heating"], default="cooling")
    parser.add_argument("--status-interval", type=int, default=60, help="status sampling interval in seconds")
    parser.add_argument("--text-mode", choices=["payload", "description"], default="payload")
    parser.add_argument("--report-name-style", choices=["x-", "telemetry"], default="x-")
    parser.add_argument("--group-ids", help="comma separated eiTarget groupIDs this VEN belongs to")
    parser.add_argument("--control-port", type=int, default=0, help="HTTP control port (0 = disabled)")
    parser.add_argument("--control-host", default="127.0.0.1", help="bind address of the control port")
    parser.add_argument("--idle", action="store_true", help="start with no thermostat demand")
    tls = parser.add_argument_group("TLS (OpenADR 2.0b security)")
    tls.add_argument("--cert", help="PEM client certificate (e.g. certs/ven-sim-hvac.crt)")
    tls.add_argument("--key", help="PEM private key for --cert")
    tls.add_argument("--ca-file", help="PEM CA bundle to verify the VTN (e.g. certs/lab-ca.crt)")
    tls.add_argument("--vtn-fingerprint", help="expected fingerprint of the VTN signing certificate")
    tls.add_argument("--no-check-hostname", action="store_true", help="do not match the VTN certificate to the host name")
    tls.add_argument("--sign-messages", action="store_true", help="XML-sign outgoing messages with --cert/--key")
    tls.add_argument("--tls-max-version", choices=["1.2", "1.3"], help="cap the TLS version offered (conformance checks)")
    tls.add_argument("--cipher", help="offer only this OpenSSL cipher suite, e.g. AES128-SHA256 (conformance checks)")
    tls.add_argument("--insecure", action="store_true", help="plain-HTTP VTN (development): allow http:// URLs")
    args = parser.parse_args(argv)
    if args.vtn_url.startswith("http://") and not args.insecure:
        parser.error("the VTN URL is plain http://; pass --insecure to talk to a development VTN without TLS")
    ssl_context = None
    if args.tls_max_version or args.cipher:
        import ssl
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if args.ca_file:
            ssl_context.load_verify_locations(args.ca_file)
        else:
            ssl_context.load_default_certs()
        if args.cert:
            ssl_context.load_cert_chain(args.cert, args.key)
        ssl_context.check_hostname = not args.no_check_hostname
        if args.tls_max_version == "1.2":
            ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2
        if args.cipher:
            ssl_context.set_ciphers(args.cipher)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    sim = AhriSimVen(args.name, args.vtn_url, resource_id=args.resource_id,
                     resource_ids=[r.strip() for r in args.resources.split(",") if r.strip()] if args.resources else None,
                     rated_power_w=args.rated_power, device_type=args.device_type, unloading_type=args.unloading_type,
                     mode=args.mode, status_interval=timedelta(seconds=args.status_interval), text_mode=args.text_mode,
                     report_name_style=args.report_name_style, running=not args.idle,
                     group_ids=[g.strip() for g in args.group_ids.split(",") if g.strip()] if args.group_ids else None,
                     cert=args.cert, key=args.key, ca_file=args.ca_file, vtn_fingerprint=args.vtn_fingerprint,
                     check_hostname=not args.no_check_hostname, sign_messages=args.sign_messages,
                     ssl_context=ssl_context)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    runner = None
    try:
        loop.run_until_complete(sim.start())
        if args.control_port:
            from aiohttp import web
            runner = web.AppRunner(_control_app(sim))
            loop.run_until_complete(runner.setup())
            loop.run_until_complete(web.TCPSite(runner, args.control_host, args.control_port).start())
            logger.info(f"Control interface on http://{args.control_host}:{args.control_port}/state (unauthenticated; "
                        "keep it on localhost)")
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if runner:
            loop.run_until_complete(runner.cleanup())
        loop.run_until_complete(sim.stop())
        loop.close()
    return 0


if __name__ == "__main__":
    main()
