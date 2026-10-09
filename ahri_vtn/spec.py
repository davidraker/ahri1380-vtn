"""
AHRI Standard 1380-202x (draft) OpenADR 2.0b profile, expressed as data.

Sources (page numbers refer to the AHRI Standard 1380-202x draft, available from AHRI):
- Table 4 "Communication and Discovery with OpenADR 2.0" (p.37-39)
- Table 5 "OpenADR 2.0 Static of Configuration Report" x-AHRI_STATIC (p.39-40)
- Table 6 "Status Data Reported in a TELEMETRY (STATUS) Report" x-AHRI_STATUS (p.41)
- Table 7 "Open ADR 2.0b Operational States" (p.41-42)
- Table 8 "Management with OpenADR 2.0b" (p.43-47)
- Appendix C.3.4 / C.3.5 test procedure (p.27-29)

The draft is inconsistent about the exact spelling of several identifiers
("Unloading Type" vs "Unloading_Type", "Model Number" vs "Model_Number", ...). All matching
of report names and rIDs is therefore done on a normalised key (lower case, alphanumerics
only) and the exact strings used by a VEN are recorded as findings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from openleadr import utils as oadr_utils


def normalize_key(value: Optional[str]) -> str:
    """Lower-case alphanumeric key used for lenient matching of names ('Inst Power' -> 'instpower')."""
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


# ---------------------------------------------------------------------------------------------
# Reports (Tables 5 and 6)
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RidSpec:
    name: str                       # canonical rID as printed in the standard
    description: str
    value_kind: str                 # 'number' | 'text' | 'enum' | 'flag'
    units: tuple = ()               # accepted itemUnits / customUnit descriptions (normalised compare)
    aliases: tuple = ()             # alternative spellings seen in the draft or in the field
    report_type: str = "reading"
    reading_type: str = "Direct Read"
    required: bool = True
    enum: Optional[dict] = None     # value -> meaning, for value_kind == 'enum'

    @property
    def key(self) -> str:
        return normalize_key(self.name)

    @property
    def keys(self) -> tuple:
        return (self.key,) + tuple(normalize_key(a) for a in self.aliases)

    def matches(self, r_id: str) -> bool:
        return normalize_key(r_id) in self.keys

    def describe_value(self, value) -> str:
        if self.enum is not None and value is not None:
            try:
                return self.enum.get(int(float(value)), str(value))
            except (TypeError, ValueError):
                return str(value)
        return "" if value is None else str(value)


@dataclass(frozen=True)
class ReportSpec:
    name: str                       # canonical reportName, e.g. 'x-AHRI_STATUS'
    title: str
    rids: tuple
    aliases: tuple = ()             # other report names / specifier id fragments that identify it
    sampling_interval: Optional[timedelta] = None   # expected sampling (None = on request)
    one_shot: bool = False          # request once (static data) instead of periodically

    @property
    def key(self) -> str:
        return normalize_key(self.name)

    @property
    def keys(self) -> tuple:
        return (self.key,) + tuple(normalize_key(a) for a in self.aliases)

    def rid(self, r_id: str) -> Optional[RidSpec]:
        for rid in self.rids:
            if rid.matches(r_id):
                return rid
        return None

    def rid_by_key(self, key: str) -> Optional[RidSpec]:
        for rid in self.rids:
            if key in rid.keys:
                return rid
        return None


DEVICE_TYPES = {0: "Cooling only", 1: "Heat pump"}
UNLOADING_TYPES = {0: "Continuously variable power", 1: "Discretely variable power"}
OVERRIDE_STATES = {0: "Not overridden", 1: "In override"}

# Table 7: Operational states (Section 6.1.9)
OPERATIONAL_STATES = {
    0: "Idle Normal",
    1: "Running Normal",
    2: "Running Curtailed",
    3: "Running Heightened",
    4: "Idle Curtailed",
    5: "Error Condition",
    6: "Idle Heightened",
    7: "Cycling On",
    8: "Cycling Off",
    9: "Variable Following",
    10: "Variable Not Following",
    11: "Idle, Opted Out",
    12: "Running, Opted Out",
    13: "Running, Price Stream",
    14: "Idle, Price Stream",
}
CURTAILED_STATES = (2, 4)
HEIGHTENED_STATES = (3, 6)
NORMAL_STATES = (0, 1)
OPTED_OUT_STATES = (11, 12)
IDLE_STATES = (0, 4, 6, 8, 11, 14)

STATIC_REPORT = ReportSpec(
    name="x-AHRI_STATIC",
    title="Static configuration report (Table 5)",
    aliases=("x-AHRI-STATIC", "AHRI_STATIC"),
    one_shot=True,
    rids=(
        RidSpec("Size", "Size of the unit (rated load, Watts)", "number",
                units=("x_ahri_Rated_Load", "x-ahri_Rated_Load", "W", "Watts"), aliases=("Rated_Load",)),
        RidSpec("Device_Type", "0=cooling only, 1=heat pump", "enum",
                units=("x-ahri_Type", "x_ahri_Type"), aliases=("Device Type", "DeviceType"), enum=DEVICE_TYPES),
        RidSpec("Unloading_Type", "0=Continuously variable power, 1=Discretely variable power", "enum",
                units=("x-ahri_UnloadingType", "x_ahri_UnloadingType"), aliases=("Unloading Type", "UnloadingType"),
                enum=UNLOADING_TYPES),
        RidSpec("Benchmark_Power_Cooling", "Benchmark power for HVAC in cooling mode (Watts)", "number",
                units=("Watts", "W"), aliases=("Benchmark Power Cooling",)),
        RidSpec("Benchmark_Power_Heating", "Benchmark power for HVAC in heating mode (Watts)", "number",
                units=("Watts", "W"), aliases=("Benchmark Power Heating",)),
        RidSpec("Vendor_Name", "Name of manufacturer (ASCII)", "text", aliases=("Vendor Name", "VendorName")),
        RidSpec("Device_Revision", "Revision number", "text", aliases=("Device Revision", "DeviceRevision")),
        RidSpec("Model_Number", "Model number of HVAC system (ASCII)", "text",
                aliases=("Model Number", "ModelNumber")),
    ),
)

STATUS_REPORT = ReportSpec(
    name="x-AHRI_STATUS",
    title="Telemetry status report (Table 6)",
    aliases=("x-AHRI-STATUS", "AHRI_STATUS"),
    sampling_interval=timedelta(minutes=1),
    rids=(
        RidSpec("Inst Power", "Instantaneous power (W)", "number", units=("W", "Watts"),
                aliases=("Inst_Power", "InstPower", "Instantaneous Power")),
        RidSpec("Operational State", "Operational state code (Table 7)", "enum", units=("Integer",),
                aliases=("Operational_State", "OperationalState", "Op State"), enum=OPERATIONAL_STATES),
        RidSpec("Override State", "1 if in customer override, 0 otherwise", "flag", units=("Discrete",),
                aliases=("Override_State", "OverrideState"), enum=OVERRIDE_STATES),
    ),
)

REPORTS = {STATIC_REPORT.name: STATIC_REPORT, STATUS_REPORT.name: STATUS_REPORT}


def identify_report(report_name: Optional[str], report_specifier_id: Optional[str] = None) -> Optional[ReportSpec]:
    """
    Identify which AHRI report a registered report is, from its (METADATA-stripped) reportName or,
    failing that, from the reportSpecifierID (vendors have been seen registering the status report
    as TELEMETRY_STATUS with a specifier id of 'Device001-x-AHRI_STATUS').
    """
    base = normalize_key(oadr_utils.strip_metadata_prefix(report_name) if report_name else "")
    for spec in REPORTS.values():
        if base in spec.keys:
            return spec
    spec_key = normalize_key(report_specifier_id)
    for spec in REPORTS.values():
        if any(k in spec_key for k in spec.keys if k):
            return spec
    return None


@dataclass
class Deviation:
    severity: str          # 'info' | 'warning' | 'fail'
    code: str
    message: str
    evidence: dict = field(default_factory=dict)


def validate_registered_report(spec: ReportSpec, report: dict) -> list[Deviation]:
    """
    Compare a registered (METADATA) report dict from OpenLEADR against the AHRI table.
    Returns a list of deviations (empty list == fully conforming).
    """
    findings: list[Deviation] = []
    raw_name = report.get("report_name")
    base_name = oadr_utils.strip_metadata_prefix(raw_name) if raw_name else None
    if base_name != spec.name:
        findings.append(Deviation("warning", "report-name",
                                  f"Report registered as '{raw_name}' instead of '{oadr_utils.metadata_report_name(spec.name)}'",
                                  {"report_name": raw_name, "expected": spec.name}))
    descriptions = report.get("report_descriptions") or []
    seen = {}
    for rd in descriptions:
        r_id = rd.get("r_id")
        rid = spec.rid(r_id)
        if rid is None:
            findings.append(Deviation("info", "rid-extra", f"rID '{r_id}' is not part of {spec.name}",
                                      {"r_id": r_id}))
            continue
        seen[rid.key] = rd
        if r_id != rid.name:
            findings.append(Deviation("info", "rid-spelling",
                                      f"rID '{r_id}' matched '{rid.name}' leniently", {"r_id": r_id, "expected": rid.name}))
        report_type = rd.get("report_type")
        if normalize_key(report_type) != normalize_key(rid.report_type):
            findings.append(Deviation("warning", "rid-report-type",
                                      f"rID '{r_id}' has reportType '{report_type}', expected '{rid.report_type}'",
                                      {"r_id": r_id, "report_type": report_type}))
        reading_type = rd.get("reading_type")
        if normalize_key(reading_type) != normalize_key(rid.reading_type):
            findings.append(Deviation("warning", "rid-reading-type",
                                      f"rID '{r_id}' has readingType '{reading_type}', expected '{rid.reading_type}'",
                                      {"r_id": r_id, "reading_type": reading_type}))
        measurement = rd.get("measurement") or {}
        if rid.units:
            unit_candidates = {normalize_key(measurement.get("unit")), normalize_key(measurement.get("description"))}
            if not unit_candidates & {normalize_key(u) for u in rid.units}:
                findings.append(Deviation("info", "rid-units",
                                          f"rID '{r_id}' units/description {measurement.get('unit')!r}/"
                                          f"{measurement.get('description')!r} do not match the table "
                                          f"({', '.join(rid.units)})",
                                          {"r_id": r_id, "measurement": measurement}))
        if spec.sampling_interval is not None:
            sampling = rd.get("sampling_rate") or {}
            min_period = sampling.get("min_period")
            if isinstance(min_period, timedelta) and min_period > spec.sampling_interval:
                findings.append(Deviation("fail", "rid-sampling",
                                          f"rID '{r_id}' offers a minimum sampling period of {min_period}, "
                                          f"the standard requires {spec.sampling_interval}",
                                          {"r_id": r_id, "min_period": min_period.total_seconds()}))
    for rid in spec.rids:
        if rid.required and rid.key not in seen:
            findings.append(Deviation("fail", "rid-missing", f"Required rID '{rid.name}' is missing from {spec.name}",
                                      {"r_id": rid.name}))
    return findings


# ---------------------------------------------------------------------------------------------
# Registration (Table 4)
# ---------------------------------------------------------------------------------------------

def validate_registration(registration_info: dict) -> list[Deviation]:
    """Check an oadrCreatePartyRegistration payload against Table 4 'Utility Setup'."""
    findings: list[Deviation] = []
    if registration_info.get("profile_name") != "2.0b":
        findings.append(Deviation("fail", "reg-profile",
                                  f"oadrProfileName is {registration_info.get('profile_name')!r}, expected '2.0b'"))
    if registration_info.get("transport_name") != "simpleHttp":
        findings.append(Deviation("fail", "reg-transport",
                                  f"oadrTransportName is {registration_info.get('transport_name')!r}, expected 'simpleHttp'"))
    if registration_info.get("xml_signature") not in (False, None):
        findings.append(Deviation("warning", "reg-signature", "oadrXmlSignature is true; the standard specifies N"))
    if registration_info.get("http_pull_model") is not True:
        findings.append(Deviation("fail", "reg-pull", "oadrHttpPullModel is not true; the standard requires pull mode"))
    if registration_info.get("report_only"):
        findings.append(Deviation("fail", "reg-report-only", "oadrReportOnly is true; the VEN must accept events"))
    if not registration_info.get("ven_name"):
        findings.append(Deviation("info", "reg-ven-name", "No oadrVenName supplied (optional)"))
    return findings


# ---------------------------------------------------------------------------------------------
# Events (Table 8)
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class EventPreset:
    id: str
    label: str
    signal_name: str
    signal_type: str
    payload: Optional[float]        # None -> operator supplies the value
    description: str
    section: str
    expected_states: tuple = ()     # operational states expected while active
    power_fraction: Optional[float] = None   # max fraction of rated power while active
    payload_label: str = "Payload"
    payload_min: Optional[float] = None
    payload_max: Optional[float] = None


EVENT_PRESETS = {
    "general_curtailment": EventPreset(
        "general_curtailment", "General Curtailment", "SIMPLE", "level", 1,
        "Limit total system input power to 70% of rated power.", "6.1.3 / Table 8",
        expected_states=CURTAILED_STATES, power_fraction=0.70),
    "critical_curtailment": EventPreset(
        "critical_curtailment", "Critical Curtailment", "SIMPLE", "level", 2,
        "Limit input power to 40% of rated power (two-capacity systems NAK with 460).", "6.1.4 / Table 8",
        expected_states=CURTAILED_STATES, power_fraction=0.40),
    "full_curtailment": EventPreset(
        "full_curtailment", "Full Curtailment / Grid Emergency", "SIMPLE", "level", 3,
        "Compressor off; controls and crankcase heater may run.", "6.1.5 / Table 8",
        expected_states=(4,), power_fraction=None),
    "normal": EventPreset(
        "normal", "SIMPLE level 0 (normal)", "SIMPLE", "level", 0,
        "SIMPLE level 0 - no curtailment.", "Table 8", expected_states=NORMAL_STATES),
    "mito": EventPreset(
        "mito", "Maximum Indoor Temperature Offset", "LOAD_CONTROL", "x-loadControlLevelOffset", None,
        "Integer degrees Fahrenheit the HVAC system may deviate from setpoint (<= 4 F).", "6.1.2 / Table 8",
        payload_label="Offset (deg F)", payload_min=-10, payload_max=10),
    "precondition": EventPreset(
        "precondition", "Precondition (Load Up)", "LOAD_CONTROL", "x-loadControlLevelOffset", None,
        "Precondition the space before a curtailment (1-4 F).", "3.2.11 / Table 8 / Appendix D",
        expected_states=HEIGHTENED_STATES, payload_label="Offset (deg F)", payload_min=1, payload_max=4),
    "price_stream": EventPreset(
        "price_stream", "Price Stream", "ELECTRICITY_PRICE", "priceMultiplier", None,
        "Unit-less price multipliers for >= 25 intervals (5 min to 1 h each).", "6.1.8 / Table 8",
        payload_label="Multiplier"),
}

SIMPLE_LEVELS = {0: "Normal", 1: "General Curtailment", 2: "Critical Curtailment", 3: "Full Curtailment"}

RESPONSE_CODES = {
    200: "OK",
    450: "Out of sequence",
    451: "Not allowed",
    452: "Invalid ID",
    453: "Not recognized",
    454: "Invalid data",
    459: "Compliance error",
    460: "Signal not supported",
    461: "Report not supported",
    462: "Target mismatch",
    463: "Not registered or authorized",
    469: "Deployment error or other error",
}

OPT_REASONS = ("economic", "emergency", "mustRun", "notParticipating", "outageRunStatus",
               "overrideStatus", "participating", "x-schedule")


def operational_state_name(code) -> str:
    try:
        return OPERATIONAL_STATES.get(int(float(code)), f"Unknown ({code})")
    except (TypeError, ValueError):
        return f"Invalid ({code!r})"
