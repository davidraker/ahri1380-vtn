# AHRI 1380 OpenADR 2.0b VTN test tool

A Virtual Top Node (OpenADR 2.0b server) for exercising vendor VENs against the draft
**AHRI Standard 1380-202x** *Demand Response through Variable Capacity HVAC Systems in Residential
and Small Commercial Applications*. It is built on a fork of [OpenLEADR](https://openleadr.org)
(`openleadr-python/`, branch `ahri-1380`) and provides:

- VEN management: pre-register VENs or auto-accept them, assign venID/registrationID, request
  re-registration, enable/disable.
- Report handling for the AHRI metadata reports `x-AHRI_STATIC` (Table 5) and `x-AHRI_STATUS`
  (Table 6): registration checked against the tables, periodic status requested automatically,
  one-shot requests on demand, every sample stored.
- Events from Table 8 (General / Critical / Full curtailment, MITO offset, precondition, price
  stream, cancel) with recording of the VEN's `oadrCreatedEvent` responses (opt type and response
  code, e.g. 460 for two-capacity units). Events, report requests and test runs target any
  selection of whole VENs, individual resources (units behind a fleet VEN) or saved groups.
- Customer override capture (`oadrCreateOpt` / `oadrCancelOpt`).
- Raw XML log of every message (paired request/response), with schema violations recorded as
  findings instead of rejected (lenient mode).
- A web UI + JSON API served from the same process, live-updating via server-sent events.
- Scripted conformance scenarios (Appendix C.3.4 / C.3.5) with pass / fail / manual assertions,
  operator prompts (e.g. "trigger the customer override now") and exportable run reports.
- A simulated AHRI VEN (`sim_ven/`) for self-tests and demonstrations.

Full installation and usage instructions: [docs/user_guide.md](docs/user_guide.md).

## Layout

All components live in this folder (`ahri1380/` in the workspace repository):

```
openleadr-python/        git submodule: OpenLEADR fork, branch ahri-1380 (VTN extensions, see docs/roadmap.rst)
ahri_vtn/                the VTN test tool package
  config.py              TOML + environment configuration
  spec.py                AHRI 1380 Tables 4-8 as data, lenient name matching, validation helpers
  db/models.py           SQLAlchemy models (VENs, reports, samples, events, opts, messages, runs, findings)
  handlers/              registration, reports, events, raw message logging
  conformance/           scenario engine + TC-* scenarios
  web/                   JSON API (/api) and HTML UI (/ui)
sim_ven/ahri_sim_ven.py  simulated VEN (OpenLEADR client) with a simple HVAC power model
tests/                   pytest end-to-end tests (VTN <-> simulated VEN)
docs/                    user guide
```

## Setup

```bash
cd ahri1380
python -m venv .venv && source .venv/bin/activate
pip install -e ./openleadr-python        # the fork (branch ahri-1380)
pip install -e .[dev]
cp config.example.toml config.toml       # edit as needed
```

Python 3.10 or newer. SQLite is used by default (`ahri_vtn.sqlite` in the working directory);
set `db_url` to a PostgreSQL URL if preferred.

## Running

```bash
python -m ahri_vtn --config config.toml           # VTN + UI on http://localhost:8080/ui/
python -m sim_ven.ahri_sim_ven --name sim-hvac    # optional: simulated VEN against it
```

Point the VEN under test at `http://<host>:8080/OpenADR2/Simple/2.0b` with the venName you added
on the dashboard (or leave `auto_accept_unknown_vens = true`). The standard specifies HTTP without
XML signatures (Table 4); TLS can be enabled through the `[tls]` section if required.

Useful simulator options: `--unloading-type 1` (two-capacity unit that NAKs Critical Curtailment
with 460), `--report-name-style telemetry` (registers the status report as `TELEMETRY_STATUS` with a
`...-x-AHRI_STATUS` specifier, an alternative a VEN might choose), `--text-mode description`, `--control-port 9090`
(then `curl -X POST localhost:9090/override` simulates the customer override).

## Lab workflow

1. Add the VEN (dashboard) and connect the equipment. Registration findings (Table 4) and the
   report checklist (Tables 5/6) appear on the VEN page.
2. `x-AHRI_STATUS` is requested at 1 minute automatically; `x-AHRI_STATIC` once. Values, charts
   and a CSV export are on the VEN page.
3. Send events manually (VEN page or Events page) or start a scenario (Test runs). The
   *Appendix C full sequence* performs C.3.5 (a)-(c) with the 2 minute stabilization and twenty
   1-minute samples, prompts for the customer override and for the wattmeter reading, then cancel,
   MITO, price-stream and re-registration checks. Choose cooling or heating mode.
4. Review the run report (pass / fail / manual items), export JSON, and inspect the raw message
   log for any schema violations.

All timing parameters (stabilization, sample count, timeouts) can be overridden per run; the
`time_scale` parameter shortens every wait for bench testing against the simulator.

## Tests

```bash
cd openleadr-python && pytest                   # OpenLEADR suite incl. test_ahri_extensions.py
cd .. && pytest tests                            # end-to-end VTN <-> simulated VEN
```

## Standard ambiguities the tool tolerates

- Report names: the schema only allows enumerated names or `x-...` tokens, so metadata reports arrive
  as `x-METADATA_x-AHRI_STATUS`, `x-AHRI_STATUS`, or `METADATA_TELEMETRY_STATUS` with a specifier id
  containing `x-AHRI_STATUS`. All are recognised; deviations from the table are recorded as findings.
- rID spelling differs within the draft (`Unloading Type` / `Unloading_Type`, `Model Number`,
  `Inst Power`). Matching is case/space/underscore insensitive and the exact strings are recorded.
- The ASCII items of Table 5 (Vendor_Name, Device_Revision, Model_Number) cannot be carried in a
  schema-valid `payloadFloat`. In lenient mode the VTN accepts text values and records a
  `schema-violation` finding; vendors that send a placeholder number and the text in the metadata
  `itemDescription` are handled too (the description is shown in the report checklist).
- `LOAD_CONTROL` signal type is sent as the OpenADR 2.0b value `x-loadControlLevelOffset`
  (the draft's hyphenation is inconsistent).

## OpenLEADR fork changes

See `openleadr-python/docs/roadmap.rst` (0.5.35 ahri-1380 entry) and `test/test_ahri_extensions.py`:
extension report names, richer payload parsing, EiOpt service, VTN-initiated `oadrCreateReport` /
`oadrCancelReport` / `oadrRequestReregistration` via poll, response codes in event callbacks,
lenient schema validation and message hooks.
