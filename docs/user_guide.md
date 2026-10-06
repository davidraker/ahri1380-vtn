# AHRI 1380 VTN test tool: installation and user guide

This tool is an OpenADR 2.0b Virtual Top Node (VTN) used to exercise a vendor's VEN (the
HVAC system or its cloud) against the draft AHRI Standard 1380-202x. It records every
message, requests and stores the AHRI reports, sends the Table 8 events, captures customer
overrides, and runs the Appendix C conformance sequence with pass / fail / manual results.

Contents

1. Requirements
2. Installation
3. Configuration
4. Starting the VTN
5. Connecting the equipment under test
6. Using the web interface
7. Running conformance tests
8. Using the simulated VEN
9. JSON API reference
10. Data storage and export
11. Troubleshooting

---

## 1. Requirements

- Linux, macOS or Windows (WSL recommended) with Python 3.10 to 3.13. All dependencies install
  from prebuilt wheels; no compiler or development headers are needed.
- Network connectivity between the VTN host and the VEN. The VEN polls the VTN over HTTP
  (pull mode), so the VTN host must be reachable from the equipment or from the vendor cloud.
  No inbound connection to the VEN is needed.
- The `ahri1380/` folder of the repository with its `openleadr-python` submodule (the OpenLEADR fork,
  branch `ahri-1380`). All commands below are run from `ahri1380/`.
- A browser for the web interface. No internet access is required at run time; all assets are
  served locally.

## 2. Installation

```bash
git clone <repository-url> open_adr_work
cd open_adr_work
git submodule update --init ahri1380/openleadr-python
cd ahri1380
git -C openleadr-python checkout ahri-1380

python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ./openleadr-python      # the OpenLEADR fork with the VTN extensions
pip install -e ".[dev]"                # the tool itself (+ pytest for self-tests)
```

Verify the installation:

```bash
python -m ahri_vtn --help
python -m sim_ven.ahri_sim_ven --help
pytest tests                            # optional self-test, about 90 seconds
```

The `pytest tests` run starts a VTN and the simulated VEN on random ports and exercises
registration, reports, events, overrides and every conformance scenario.

## 3. Configuration

Copy the example and edit it:

```bash
cp config.example.toml config.toml
```

If `config.toml` exists in the working directory it is loaded automatically; otherwise pass
`--config <file>`. Every top-level value can be overridden by an environment variable named
`AHRI_VTN_<FIELD>` in upper case, for example `AHRI_VTN_PORT=9000`.

| Key | Default | Meaning |
|---|---|---|
| `vtn_id` | `ahri1380-vtn` | The vtnID returned to VENs. |
| `host`, `port` | `0.0.0.0`, `8080` | Bind address and TCP port for both OpenADR and the UI. |
| `http_path_prefix` | `/OpenADR2/Simple/2.0b` | OpenADR endpoint prefix (the VEN's VTN URL). |
| `db_url` | `sqlite:///ahri_vtn.sqlite` | SQLAlchemy URL. SQLite file by default; PostgreSQL works too. |
| `requested_poll_freq_seconds` | `10` | `oadrRequestedOadrPollFreq` sent to the VEN at registration. |
| `auto_accept_unknown_vens` | `true` | Accept registrations from VEN names not added in the UI. Set `false` to require pre-registration. |
| `schema_mode` | `lenient` | `lenient` records XML schema violations as findings and processes the message; `strict` rejects them with HTTP 400. |
| `auto_request_reports` | `true` | Request `x-AHRI_STATUS` periodically and `x-AHRI_STATIC` once as soon as the VEN registers them. |
| `status_report_interval_seconds` | `60` | Requested sampling interval for `x-AHRI_STATUS`. |
| `offline_after_polls` | `3` | A VEN is shown offline after this many missed poll intervals. |
| `market_context` | `oadr://ahri1380` | marketContext placed in events. |
| `default_event_duration_minutes` | `30` | Default duration in the event form. |
| `log_level` | `INFO` | Logging level. `DEBUG` also enables OpenLEADR message logging. |

The `[thresholds]` table holds the Appendix C parameters used as defaults by the conformance
scenarios: `general_curtailment_fraction` (0.70), `critical_curtailment_fraction` (0.40),
`off_mode_max_watts` (200, allowance for controls and crankcase heater in Full Curtailment),
`stabilization_seconds` (120), `sample_count` (20), `sample_interval_seconds` (60),
`event_response_timeout_seconds` (60), `override_timeout_seconds` (600),
`status_interval_tolerance` (0.5) and `max_mito_degrees_f` (4).

The optional `[tls]` table (`http_cert`, `http_key`, `http_ca_file`, `http_key_passphrase`,
`signing_cert`, `signing_key`, `signing_passphrase`) enables HTTPS with client certificates and
XML message signing. AHRI 1380 Table 4 specifies plain HTTP without XML signatures, so these are
normally left unset. The optional `[ui]` table (`basic_auth_user`, `basic_auth_password`)
protects `/ui` and `/api` with HTTP basic authentication.

## 4. Starting the VTN

```bash
python -m ahri_vtn --config config.toml
```

Useful flags: `--host`, `--port`, `--db`, `--vtn-id`, `--schema-mode strict|lenient`,
`--log-level DEBUG`, `--no-web` (OpenADR endpoints only). The console prints the OpenADR URL
and the UI URL:

```
AHRI 1380 VTN 'ahri1380-vtn' listening on http://localhost:8080/OpenADR2/Simple/2.0b (UI: http://0.0.0.0:8080/ui/)
```

Open `http://<vtn-host>:8080/ui/` in a browser. Stop the VTN with Ctrl-C; all data is already
on disk in the SQLite file, and VENs that stay registered continue to work after a restart
because registrations are persisted.

Running as a service: any process supervisor works (systemd, supervisord, a container). The
process needs write access to the directory holding the SQLite file.

## 5. Connecting the equipment under test

Configure the VEN (per the manufacturer's instructions, Appendix C.3.1) with:

- **VTN URL**: `http://<vtn-host>:8080/OpenADR2/Simple/2.0b`
- **VEN name**: any ASCII string. If `auto_accept_unknown_vens` is `false`, add the same
  name on the dashboard first ("Add VEN"). With auto-accept enabled the VEN is created when
  it registers; you can still pre-register it to attach notes or a fixed venID.
- **Security**: none for plain HTTP (Table 4: XML signature N). For HTTPS lab setups, provide
  the VTN certificate chain to the vendor and configure the `[tls]` section.

Expected sequence once the VEN starts: `oadrQueryRegistration`, `oadrCreatePartyRegistration`
(the VTN assigns venID and registrationID), `oadrRegisterReport` with the `x-AHRI_STATIC` and
`x-AHRI_STATUS` metadata reports, then `oadrPoll` at the requested interval. The VTN requests
the status report (1 minute) and the static report (once) immediately inside
`oadrRegisteredReport`; the VEN confirms with `oadrCreatedReport` and starts sending
`oadrUpdateReport`. Everything appears on the VEN page within one poll interval.

If the VEN does not appear, check the **Messages** page: a request with an HTTP 400 means a
schema problem in `strict` mode; `oadrRequestReregistration` responses mean the VEN is using a
venID the VTN does not know (for example after the database was deleted) and should re-register.

## 6. Using the web interface

All pages update live; the green dot in the header shows the event stream is connected.

**Dashboard** (`/ui/`): counts, the VEN list (online state, last poll, latest operational
state, power and override state, rated power, live events) and the Add VEN form. Recent events,
recent test runs and an activity feed.

**VEN page** (`/ui/vens/<id>`):

- *Registration*: venID, registrationID, profile and transport, pull model, signature flag,
  timestamps, poll count. Buttons: request re-registration, enable / disable, delete; a notes
  field for vendor, model and bay information.
- *Static configuration*: the eight Table 5 values with their meaning (device type, unloading
  type) and the time received.
- *Latest status*: Inst Power, Operational State (with the Table 7 name) and Override State,
  plus a form to request `x-AHRI_STATUS` or `x-AHRI_STATIC` on demand, one shot or periodic.
- *Telemetry charts*: power with the 70 % and 40 % lines, operational state and override state
  for the chosen window (30 min to 24 h).
- *Events*: the event form (see below) and the event history with the VEN's responses.
- *Customer overrides*: every `oadrCreateOpt` received.
- *Registered reports*: a checklist of each Table 5 / 6 rID against what the VEN offered
  (name used, reportType, readingType, units, min / max sampling period).
- *Report requests*, *Findings*, a quick "run a conformance test" form and the recent samples.

**Sending an event** (VEN page or Events page): choose a preset, start offset, duration and
optional payload / MITO / vtnComment, then "Send event". Presets map to Table 8:

| Preset | Signal | Payload | Section |
|---|---|---|---|
| General Curtailment | SIMPLE : level | 1 | 6.1.3 |
| Critical Curtailment | SIMPLE : level | 2 | 6.1.4 |
| Full Curtailment / Grid Emergency | SIMPLE : level | 3 | 6.1.5 |
| SIMPLE level 0 (normal) | SIMPLE : level | 0 | Table 8 |
| Maximum Indoor Temperature Offset | LOAD_CONTROL : x-loadControlLevelOffset | degrees F | 6.1.2 |
| Precondition (Load Up) | LOAD_CONTROL : x-loadControlLevelOffset | 1 to 4 F | Appendix D |
| Price Stream | ELECTRICITY_PRICE : priceMultiplier | list of multipliers | 6.1.8 |

Entering a MITO value with a SIMPLE preset produces the two-signal event described in Table 8.
The price stream takes a comma-separated list of multipliers and an interval length; 25 or more
values satisfy Section 6.1.8. The event is delivered on the VEN's next poll; the response
(`optIn` / `optOut`, response code, modification number) appears in the Responses column.
"Cancel" re-sends the event with eventStatus CANCELLED (Section 6.1.7).

**Events** (`/ui/events`): the same form for any VEN plus the full event history.

**Messages** (`/ui/messages`): every message in both directions with time, type, venID,
remote address, HTTP status and a schema-violation marker. Filter by VEN, type, direction or
violations only. Clicking a message shows the pretty-printed XML, the parsed payload and the
paired request / response.

**Findings** (`/ui/findings`): registration checks (Table 4), report checklist deviations
(Tables 5 and 6), schema violations and all scenario assertions, filterable by VEN.

**Test runs** (`/ui/runs`): start and follow conformance runs (next section).

## 7. Running conformance tests

Open **Test runs**, choose the VEN, a scenario and the mode (cooling or heating per the lab
conditions), optionally change the timing parameters, and press Start. The run page lists each
step with its assertions as they complete.

Scenarios:

| ID | What it does |
|---|---|
| `appendix_c` | The full Appendix C sequence: all checks below in order, with operator prompts. |
| `tc_reg` | Utility Setup: Table 4 registration values and poll cadence. |
| `tc_meta` | `x-AHRI_STATIC` and `x-AHRI_STATUS` registered with all rIDs, types and a 1 minute capability. |
| `tc_static` | Requests the static report once and checks the eight values. |
| `tc_status` | Collects status samples; checks cadence and valid Table 7 / override codes. |
| `tc_gc` | General Curtailment: opt-in, 2 min stabilization, 20 one-minute samples at or below 70 % of rated power, return to normal. |
| `tc_cc` | Critical Curtailment at or below 40 % (or NAK 460 for two-capacity units), with the customer override step, return to normal. |
| `tc_fc` | Full Curtailment: power within the off-mode allowance, state Idle Curtailed. |
| `tc_override` | General Curtailment with a customer override. |
| `tc_cancel_future` | Schedule a future event and cancel it. |
| `tc_mito` | Offset event alone and combined with SIMPLE. |
| `tc_price` | 25-interval price stream. |
| `tc_rereg` | `oadrRequestReregistration` followed by a new registration. |

Assertion results: **pass**, **fail**, **warning** (deviation from the table that does not
affect operation), **info**, and **manual** (something the lab must verify by hand, such as the
wattmeter average, resistance heat state in heating mode, or the 5 second ramp). A run ends as
*passed*, *failed* (any failed step) or *needs review* (manual items only).

Operator prompts appear in a yellow box on the run page and must be answered before the run
continues:

- "Initiate a customer override at the equipment user interface now" during the Critical
  Curtailment step (Appendix C.3.5 b). Press Continue after operating the thermostat; the run
  then waits for `oadrCreateOpt`, Override State 1 and the return to normal power.
- "Enter the wattmeter average input power" after each sampling window. Type the measured
  value and press Submit to have it compared with the 70 % / 40 % / off-mode limit, or Skip.

Timing parameters (all in seconds unless noted) can be changed per run: `stabilization_seconds`,
`sample_count`, `sample_interval_seconds`, `response_timeout_seconds`,
`override_timeout_seconds`, `recovery_timeout_seconds`, `report_timeout_seconds`, `mito`,
`price_intervals`, `price_interval_minutes`, `operator_prompts` (0 disables prompts; they are
auto-answered), `wattmeter_prompt`, `time_scale` (multiplies every wait; use 0.05 for a quick
dry run against the simulator). A full Appendix C run with default timings takes roughly two
hours of equipment time.

Results are kept in the database and can be downloaded as JSON from the run page
(`/api/runs/<id>/report.json`). Operator notes can be added to any run.

## 8. Using the simulated VEN

The simulator is useful to learn the tool, to rehearse a test sequence, and to check the VTN
installation before equipment arrives.

```bash
python -m sim_ven.ahri_sim_ven --vtn-url http://localhost:8080/OpenADR2/Simple/2.0b --name sim-hvac
```

Options: `--rated-power 4800`, `--device-type 0|1`, `--unloading-type 0|1` (1 simulates a
two-capacity unit that NAKs Critical Curtailment with 460), `--mode cooling|heating`,
`--status-interval 60`, `--text-mode payload|description` (how the ASCII items are sent),
`--report-name-style x-|telemetry` (register the status report as `x-AHRI_STATUS` or as
`TELEMETRY_STATUS` with an `x-AHRI_STATUS` specifier, an alternative a VEN might choose), `--idle` (no
thermostat demand), `--control-port 9090`.

With a control port enabled:

```bash
curl -X POST localhost:9090/override         # customer override (oadrCreateOpt optOut)
curl -X POST localhost:9090/clear-override
curl -X POST -d running=false localhost:9090/set
curl localhost:9090/state
```

## 9. JSON API reference

Everything the UI does is available under `/api`. Responses are JSON; POST bodies may be JSON
or form-encoded.

| Method and path | Purpose |
|---|---|
| `GET /api/status` | VTN identity, configuration summary and counts. |
| `GET /api/spec`, `GET /api/presets` | The AHRI tables and event presets as data. |
| `GET /api/vens`, `POST /api/vens` | List VENs; add one (`ven_name`, optional `ven_id`, `notes`). |
| `GET /api/vens/{id}` | Full VEN detail (registration, reports, checklist, latest values, events, opts, findings). |
| `PATCH /api/vens/{id}`, `DELETE /api/vens/{id}` | Update `enabled` / `notes`; delete. |
| `POST /api/vens/{id}/request-report` | `spec_name` (`x-AHRI_STATUS` or `x-AHRI_STATIC`) or `report_specifier_id`, `one_shot`, `granularity_seconds`. |
| `POST /api/report-requests/{rrid}/cancel` | Cancel a report request. |
| `POST /api/vens/{id}/request-reregistration` | Queue `oadrRequestReregistration`. |
| `GET /api/vens/{id}/samples` and `/samples.csv` | Samples; filters `rid`, `since`, `until`, `minutes`, `limit`. |
| `GET /api/vens/{id}/series?minutes=120` | Status time series for charts. |
| `GET /api/events`, `POST /api/events` | List events; create one (`ven_pk`, `preset`, `start` or `start_offset_seconds`, `duration_seconds` or `duration_minutes`, `payload`, `mito`, `price_multipliers`, `price_interval_minutes`, `test_event`, `vtn_comment`). |
| `GET /api/events/{event_id}`, `POST /api/events/{event_id}/cancel` | Event detail with responses; cancel. |
| `GET /api/messages`, `GET /api/messages/{id}` | Message log (filters `ven_id`, `type`, `direction`, `violations`, `page`) and raw XML. |
| `GET /api/findings?ven=&run=` | Findings. |
| `GET /api/scenarios` | Available scenarios with default parameters. |
| `GET /api/runs`, `POST /api/runs` | List runs; start one (`scenario_id`, `ven_pk`, `mode`, `params` object). |
| `GET /api/runs/{id}`, `GET /api/runs/{id}/report.json` | Run detail and downloadable report. |
| `POST /api/runs/{id}/abort`, `POST /api/runs/{id}/respond` | Abort; answer a prompt (`answer`, optional `note`). |
| `GET /api/stream` | Server-sent events for live updates. |

Example, starting a General Curtailment on VEN 1 for 45 minutes with a 3 F offset:

```bash
curl -X POST localhost:8080/api/events -H 'content-type: application/json' \
  -d '{"ven_pk": 1, "preset": "general_curtailment", "duration_minutes": 45, "mito": 3}'
```

## 10. Data storage and export

All data lives in the configured database (default `ahri_vtn.sqlite` in the working
directory). Main tables: `vens`, `registration_events`, `registered_reports`,
`report_descriptions`, `report_requests`, `report_samples` (one row per rID per interval, with
numeric or text value, confidence, accuracy, data quality and a link to the raw message),
`events`, `event_responses`, `opts`, `messages` (raw XML in and out, paired), `test_runs`,
`test_steps` and `findings`.

Exports: CSV of samples from the VEN page or `/api/vens/{id}/samples.csv`, JSON run reports,
and the SQLite file itself (copy it while the VTN is stopped, or use `sqlite3 .backup`). To
start a clean test campaign, stop the VTN and move the SQLite file aside; VENs will be asked to
re-register automatically because their venIDs are no longer known.

## 11. Troubleshooting

- **VEN never appears.** Confirm the VEN can reach `http://<vtn-host>:8080/OpenADR2/Simple/2.0b`
  (for example `curl -X POST` to it returns HTTP 400 "Content-Type header must be
  application/xml", which proves connectivity). Check firewall rules and that `host` is
  `0.0.0.0`, not `127.0.0.1`.
- **Registration rejected.** With `auto_accept_unknown_vens = false` the VEN name must be added
  first and the VEN must be enabled. The Findings page shows `reg-unknown-ven` entries.
- **HTTP 400 on messages.** Only in `strict` mode. Switch to `lenient` to see the message and
  the violation recorded as a finding.
- **No report data.** Check the Report requests table on the VEN page: `queued` means the VEN
  has not polled yet, `created` means it acknowledged but has not delivered. Vendor VENs must
  accept `oadrReportRequest` inside `oadrRegisteredReport` (Table 4).
- **Static values show numbers for vendor name / model.** The vendor sent a placeholder because
  the payload type is float-only; the text, if any, is in the report checklist's description
  column.
- **Event response never arrives.** The VEN must poll; check the last poll time. If the event
  started in the past the VEN may treat it as already active; use a start offset.
- **Scenario waits forever at a prompt.** Prompts need an operator answer on the run page; set
  `operator_prompts` to 0 for unattended dry runs.
- **`pip install` tries to compile lxml and fails** ("libxml2 and libxslt development packages
  are not installed" or "Python.h: No such file or directory"). This happens when pip resolves an
  lxml version that has no prebuilt wheel for your interpreter, typically an old `signxml` pin
  forcing `lxml<5` on Python 3.13. Make sure the `openleadr-python` submodule is on branch
  `ahri-1380` (its `setup.py` allows `signxml<6`, which uses current lxml wheels) and run
  `pip install --upgrade pip` first. If you really need to compile, install the headers:
  `sudo dnf install python3-devel libxml2-devel libxslt-devel gcc` (Fedora) or
  `sudo apt install python3-dev libxml2-dev libxslt1-dev build-essential` (Debian/Ubuntu).
- **Verbose OpenLEADR logging.** The default level hides OpenLEADR's per-message INFO lines;
  pass `--log-level DEBUG` to see every message in the console.
