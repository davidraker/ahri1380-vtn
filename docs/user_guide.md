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
7. Resources, groups and targeting
8. Running conformance tests
9. Using the simulated VEN
10. JSON API reference
11. Data storage and export
12. Troubleshooting
13. Securing the test tool: TLS, client certificates and operator access

---

## 1. Requirements

- Linux, macOS or Windows (WSL recommended) with Python 3.10 to 3.13. All dependencies install
  from prebuilt wheels; no compiler or development headers are needed.
- Network connectivity between the VTN host and the VEN. The VEN polls the VTN over HTTPS
  (pull mode, OpenADR 2.0b mutual TLS), so the VTN host must be reachable from the equipment or
  from the vendor cloud on the OpenADR port (443 by default). No inbound connection to the VEN
  is needed. The operator UI listens on a second port (9443) that only the operator needs.
- An OpenSSL build that can negotiate the two OpenADR 2.0b default cipher suites; see section
  13.6, and run `ahri-vtn tls-selfcheck` after installation.
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

Create the lab certificates and the operator password, then verify the installation:

```bash
ahri-vtn certs quickstart --san <dns-name-or-ip-the-units-will-use> --san localhost
ahri-vtn hash-password                  # paste the output into [ui] basic_auth_password_hash
cp config.example.toml config.toml      # then fill in the [tls] / [ui] paths printed by quickstart
ahri-vtn tls-selfcheck                  # both OpenADR 2.0b default cipher suites must say OK
python -m sim_ven.ahri_sim_ven --help
pytest tests                            # optional self-test, a few minutes
```

`certs quickstart` writes a lab CA, RSA and ECC VTN certificates, a certificate for the
simulator and an operator certificate into `certs/` (section 13.3). The `pytest tests` run
starts a VTN with its own throw-away certificates and the simulated VEN on random ports and
exercises registration over mutual TLS, reports, events, overrides, the UI security rules and
every conformance scenario.

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
| `host`, `port` | `0.0.0.0`, `443` | Bind address and TCP port of the OpenADR listener (the UI has its own, `[ui] host` / `port`, default `127.0.0.1:9443`). |
| `http_path_prefix` | `/OpenADR2/Simple/2.0b` | OpenADR endpoint prefix (the VEN's VTN URL). |
| `db_url` | `sqlite:///ahri_vtn.sqlite` | SQLAlchemy URL: `sqlite:///file` or `postgresql://user@host:5432/dbname` (section 11). |
| `db_password_env` | unset | Name of the environment variable holding the database password (PostgreSQL). Never put the password in the URL. |
| `db_pool_size` | `5` | Connection pool size of the asynchronous engine (PostgreSQL). |
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

The `[tls]`, `[signatures]`, `[ui]` and `[limits]` tables configure transport security, XML
signatures, operator access and abuse limits. They are mandatory in the sense that the tool
refuses to start without a complete, secure set of values; `config.example.toml` documents every
key and section 13 explains the choices. The defaults are mutual TLS on port 443 with pinned
VEN fingerprints, no XML signatures (Table 4), and an operator UI on `127.0.0.1:9443` that
accepts an operator certificate or the password. Plain HTTP exists only as the explicit
development setting `[tls] mode = "plain"`.

## 4. Starting the VTN

```bash
ahri-vtn --config config.toml            # same as: ahri-vtn run --config config.toml
```

Useful flags: `--host`, `--port`, `--db`, `--vtn-id`, `--schema-mode strict|lenient`,
`--log-level DEBUG`, `--no-web` (OpenADR listener only). Port 443 needs the
`CAP_NET_BIND_SERVICE` capability or a container port mapping (section 13.7); use `port = 8443`
where that is not possible. The console prints both URLs, the VTN certificates and their
fingerprints:

```
AHRI 1380 VTN 'ahri1380-vtn' listening on https://localhost/OpenADR2/Simple/2.0b (tls mutual, fingerprint policy pinned, signatures none)
VTN certificate RSA 2048: O=AHRI 1380 test tool,CN=vtn.lab, SAN vtn.lab, 192.168.10.5, fingerprint 3F:..., expires 2029-02-10
VTN certificate EC 256: ...
Operator UI on https://127.0.0.1:9443/ui/ (client certificates optional, password login on)
```

Open `https://127.0.0.1:9443/ui/` in a browser on the VTN host (or through an SSH tunnel or
VPN), accept or import the lab CA, and log in with the operator certificate or the password.
Stop the VTN with Ctrl-C; all data is already on disk in the SQLite file, and VENs that stay
registered continue to work after a restart because registrations and pinned certificates are
persisted.

Running as a service: `deploy/ahri-vtn.service` is a hardened systemd unit (unprivileged user,
`CAP_NET_BIND_SERVICE` for port 443, self-check before start); `Dockerfile` builds a pinned
container image whose build fails if the cipher-suite self-check does not pass. The process
needs write access to the directory holding the SQLite file and read access to the keys.

## 5. Connecting the equipment under test

Configure the VEN (per the manufacturer's instructions, Appendix C.3.1) with:

- **VTN URL**: `https://<vtn-host>/OpenADR2/Simple/2.0b` (add `:8443` if the VTN does not
  run on 443). The host name or IP address must be one of the Subject Alternative Names of the
  VTN certificate (`ahri-vtn certs vtn --san ...`).
- **Trust**: give the vendor `certs/lab-ca.crt` (or the VTN certificates themselves if the unit
  pins a certificate) so the unit accepts the VTN. Never hand out a `.key` file.
- **The unit's certificate**: OpenADR 2.0b requires the unit to present a client certificate.
  Add the issuing CA (or the device certificate itself, for self-signed prototypes) to the trust
  bundle with `ahri-vtn certs bundle add <pem>`, or issue a lab certificate with
  `ahri-vtn certs ven --name <unit>` and load it into the unit.
- **VEN name and fingerprint**: with the default `fingerprint_policy = "pinned"`, add the VEN on
  the dashboard with its certificate fingerprint (printed by the vendor, by
  `ahri-vtn certs fingerprint <pem>`, or shown in the dashboard's "Refused registrations" list
  after the unit's first attempt, where one click pins it). With `first-use` the first
  certificate a new name presents is pinned automatically. A pinned fingerprint is never
  replaced silently; re-keyed devices are re-pinned by the operator on the VEN page.
- **XML signatures**: Table 4 says N; leave `[signatures] mode = "none"` unless the committee
  decides otherwise. Section 13 has the details of every setting.

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

**Events** (`/ui/events`): the event form with the full target selector (see section 7) and the
dispatch history: one row per operator action, listing the OpenADR event sent to each VEN with
its target, status and responses, and a "cancel all" button.

**Groups** (`/ui/groups`): create, edit and delete saved target groups (section 7).

**Messages** (`/ui/messages`): every message in both directions with time, type, venID,
remote address, HTTP status and a schema-violation marker. Filter by VEN, type, direction or
violations only. Clicking a message shows the pretty-printed XML, the parsed payload and the
paired request / response.

**Findings** (`/ui/findings`): registration checks (Table 4), report checklist deviations
(Tables 5 and 6), schema violations and all scenario assertions, filterable by VEN.

**Test runs** (`/ui/runs`): start and follow conformance runs (next section).

## 7. Resources, groups and targeting

A VEN may represent one HVAC unit or a whole fleet (for example a manufacturer cloud). The tool
discovers the *resources* behind a VEN from the `reportDataSource/resourceID` of the reports it
registers, keeps per-resource status and static values, and lists them on the VEN page. Each
resource has its own page (`/ui/vens/<id>/resources/<resourceID>`) with charts, samples, the
events that affected it (targeted at it or at the whole VEN), its overrides and its reports, plus
a label and notes field.

**Targets.** Events, report requests and test runs all take a *selection*: any mix of whole VENs,
individual resources and saved groups, across several VENs. The default is the whole VEN you
launched from. In the forms the selector lists every VEN with its resources nested beneath it;
ticking a VEN addresses all of its units, ticking resources addresses just those.

- For events, one OpenADR event is created per targeted VEN. Its `eiTarget` carries the `venID`
  for a whole VEN or the chosen `resourceID`s for specific units. The events of one action are
  grouped as a *dispatch* on the Events page and can be cancelled together.
- For report requests, a whole VEN gets every registered report of the chosen type (one per unit
  on a fleet VEN); selected resources get only the reports covering them.
- For test runs, response checks are made per VEN and state, power, override and static checks
  per unit, each tagged with the unit name; a step fails if any unit fails.

**Groups.** A group is a saved selection with a name (for example "Bay 3 units"). Optionally give
it an OpenADR `groupID`: events to whole-VEN members then also carry `eiTarget/groupID`, which a
VEN configured with that id may honour (2.0b does not require it). Members that are individual
resources are addressed by `resourceID` only, because a groupID applies to everything a VEN
controls.

Note that OpenADR 2.0b does not require VENs to filter on `resourceID`, and the AHRI draft does
not mention resources at all. If a vendor's fleet VEN ignores resource targets, the practical
alternative is a dedicated test VEN containing only the unit under test.

## 8. Running conformance tests

Open **Test runs**, choose the targets (a whole VEN, specific resources or a group), a scenario
and the mode (cooling or heating per the lab conditions), optionally change the timing
parameters, and press Start. The run page lists each step with its assertions as they complete;
with several units under test the assertions are prefixed with the unit name.

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

## 9. Using the simulated VEN

The simulator is useful to learn the tool, to rehearse a test sequence, and to check the VTN
installation before equipment arrives.

```bash
python -m sim_ven.ahri_sim_ven --vtn-url https://localhost/OpenADR2/Simple/2.0b --name sim-hvac \
    --cert certs/ven-sim-hvac.crt --key certs/ven-sim-hvac.key --ca-file certs/lab-ca.crt
```

`certs quickstart` already created `ven-sim-hvac.crt`; `ahri-vtn certs ven --name other` makes
more. TLS options: `--cert/--key/--ca-file`, `--no-check-hostname`, `--sign-messages` (XML
signatures with the same certificate), `--tls-max-version 1.2` and `--cipher AES128-SHA256`
(behave like a VEN that offers only one OpenADR 2.0b default suite), `--vtn-fingerprint` (verify
a signing VTN). Against a development VTN in `plain` mode pass `--insecure` with an `http://` URL.

Options: `--resources hvac-1,hvac-2,hvac-3` (a fleet VEN with several units, each registering
its own reports and honouring `resourceID` and `groupID` targets), `--group-ids lab-a` (groupIDs
the VEN belongs to), `--rated-power 4800`, `--device-type 0|1`, `--unloading-type 0|1` (1 simulates a
two-capacity unit that NAKs Critical Curtailment with 460), `--mode cooling|heating`,
`--status-interval 60`, `--text-mode payload|description` (how the ASCII items are sent),
`--report-name-style x-|telemetry` (register the status report as `x-AHRI_STATUS` or as
`TELEMETRY_STATUS` with an `x-AHRI_STATUS` specifier, an alternative a VEN might choose), `--idle` (no
thermostat demand), `--control-port 9090` (bound to `127.0.0.1`; `--control-host` to change).

With a control port enabled:

```bash
curl -X POST localhost:9090/override                  # customer override (oadrCreateOpt optOut)
curl -X POST -d resource=hvac-2 localhost:9090/override   # on one unit of a fleet VEN
curl -X POST localhost:9090/clear-override
curl -X POST -d running=false localhost:9090/set
curl localhost:9090/state
```

## 10. JSON API reference

Everything the UI does is available under `/api` on the UI listener (`https://<ui-host>:9443`).
Every request is authenticated: an operator certificate, HTTP Basic credentials, or a Bearer
token from the variable named by `[ui] api_tokens_env` (create tokens with `ahri-vtn gen-token`).
State-changing requests must send JSON (form-encoded bodies are refused for browser sessions, a
CSRF defence) and must not carry a cross-site `Origin`. Responses are JSON.

```bash
curl -s --cacert certs/lab-ca.crt -H "Authorization: Bearer $TOKEN" https://127.0.0.1:9443/api/status
curl -s --cacert certs/lab-ca.crt -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"ven_name": "unit-3", "fingerprint": "5E:71:FA:E4:61:75:9C:EF:A4:D6"}' https://127.0.0.1:9443/api/vens
```

| Method and path | Purpose |
|---|---|
| `GET /api/status` | VTN identity, security summary, who you are logged in as, counts. |
| `GET /api/tls` | The VTN certificates (subject, SANs, expiry, fingerprints) to hand to vendors, and the cipher suites offered. |
| `POST /api/vens/{id}/fingerprint`, `DELETE /api/vens/{id}/fingerprint` | Pin (`fingerprint`: OpenADR or SHA-256 form) or clear the VEN's certificate. |
| `GET /api/rejected-registrations`, `POST /api/rejected-registrations/{id}/dismiss` | Registrations refused because of their certificate. |
| `GET /api/spec`, `GET /api/presets` | The AHRI tables and event presets as data. |
| `GET /api/vens`, `POST /api/vens` | List VENs; add one (`ven_name`, optional `ven_id`, `notes`, `fingerprint`). |
| `GET /api/vens/{id}` | Full VEN detail (registration, reports, checklist, latest values, events, opts, findings). |
| `PATCH /api/vens/{id}`, `DELETE /api/vens/{id}` | Update `enabled` / `notes`; delete. |
| `POST /api/vens/{id}/request-report` | `spec_name` (`x-AHRI_STATUS` or `x-AHRI_STATIC`) or `report_specifier_id`, `one_shot`, `granularity_seconds`. |
| `POST /api/report-requests/{rrid}/cancel` | Cancel a report request. |
| `POST /api/vens/{id}/request-reregistration` | Queue `oadrRequestReregistration`. |
| `GET /api/vens/{id}/samples` and `/samples.csv` | Samples; filters `rid`, `since`, `until`, `minutes`, `limit`. |
| `GET /api/vens/{id}/series?minutes=120` | Status time series for charts. |
| `GET /api/events`, `POST /api/events` | List events (filters `ven`, `resource`); dispatch one to a selection (`selection` object, or `targets` list such as `["ven:3", "res:4:hvac-1", "group:2"]`, or `ven_pk` with optional `resource_ids`; plus `preset`, `start` or `start_offset_seconds`, `duration_seconds` or `duration_minutes`, `payload`, `mito`, `price_multipliers`, `price_interval_minutes`, `test_event`, `vtn_comment`). Returns the dispatch id and the event id per VEN. |
| `GET /api/dispatches`, `GET /api/dispatches/{id}`, `POST /api/dispatches/{id}/cancel` | Dispatch history with per-VEN events and responses; cancel every live event of a dispatch. |
| `GET /api/vens/{id}/resources`, `GET`/`PATCH /api/vens/{id}/resources/{resourceID}` | Resources of a VEN with latest status; detail; set `label` / `notes`. |
| `GET /api/targets` | VENs with their resources and the saved groups, for building selections. |
| `GET`/`POST /api/groups`, `GET`/`PUT`/`DELETE /api/groups/{id}` | Saved groups (`name`, `description`, `signal_group_id`, `selection`). |
| `POST /api/report-requests` | Request a report type (`spec_name`) for a selection; `one_shot`, `granularity_seconds`. |
| `GET /api/events/{event_id}`, `POST /api/events/{event_id}/cancel` | Event detail with responses; cancel. |
| `GET /api/messages`, `GET /api/messages/{id}` | Message log (filters `ven_id`, `type`, `direction`, `violations`, `page`) and raw XML. |
| `GET /api/findings?ven=&run=` | Findings. |
| `GET /api/scenarios` | Available scenarios with default parameters. |
| `GET /api/runs`, `POST /api/runs` | List runs; start one (`scenario_id`, a selection as for events, `mode`, `params` object). |
| `GET /api/runs/{id}`, `GET /api/runs/{id}/report.json` | Run detail and downloadable report. |
| `POST /api/runs/{id}/abort`, `POST /api/runs/{id}/respond` | Abort; answer a prompt (`answer`, optional `note`). |
| `GET /api/stream` | Server-sent events for live updates. |

Example, starting a General Curtailment on VEN 1 for 45 minutes with a 3 F offset:

```bash
curl --cacert certs/lab-ca.crt -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -X POST https://127.0.0.1:9443/api/events \
  -d '{"ven_pk": 1, "preset": "general_curtailment", "duration_minutes": 45, "mito": 3}'
```

## 11. Data storage and export

All data lives in the configured database. Two backends are supported through one `db_url`:

- **SQLite** (default): `sqlite:///ahri_vtn.sqlite`, a file in the working directory. Right for a
  single bench PC; copy the file for a backup.
- **PostgreSQL**, on the same host or across the network: `pip install -e ".[postgres]"`, then
  `db_url = "postgresql://ahri_vtn@dbhost:5432/ahri_vtn"` and the password in the environment
  variable named by `db_password_env` (the URL may carry a user name but never a password; the
  start-up log and `/api/status` show the URL with the password masked). For TLS to the database
  server append `?sslmode=verify-full&sslrootcert=/path/to/db-ca.pem` to the URL. Create the
  database and a role with rights on it beforehand; the tool creates its own tables on first start.

Database access never blocks the VTN's event loop on either backend: handlers, the UI and the
conformance engine run their queries through an asynchronous driver (aiosqlite, psycopg), so a
slow network round trip delays only the request that caused it, not VEN polls, live updates or
other operators. The same test suite runs against both backends
(`AHRI_VTN_TEST_DB_URL=postgresql://... pytest tests`).

Main tables: `vens`, `registration_events`, `registered_reports`,
`report_descriptions`, `report_requests`, `report_samples` (one row per rID per interval, with
numeric or text value, confidence, accuracy, data quality and a link to the raw message),
`resources`, `groups`, `group_members`, `dispatches`, `events`, `event_responses`, `opts`,
`messages` (raw XML in and out, paired, with the TLS version, cipher suite and client
certificate of each inbound message), `rejected_registrations`, `test_runs`, `test_steps`,
`findings` and `meta` (schema version). The tool does not migrate databases: when the schema
version changes it refuses to open the old database and asks you to delete the file (SQLite) or
drop and recreate the schema (PostgreSQL).

Exports: CSV of samples from the VEN page or `/api/vens/{id}/samples.csv`, JSON run reports,
and the SQLite file itself (copy it while the VTN is stopped, or use `sqlite3 .backup`). To
start a clean test campaign, stop the VTN and move the SQLite file aside; VENs will be asked to
re-register automatically because their venIDs are no longer known.

## 12. Troubleshooting

- **VEN never appears.** Confirm the VEN can reach `https://<vtn-host>/OpenADR2/Simple/2.0b`:
  `curl -v --cacert certs/lab-ca.crt --cert certs/ven-sim-hvac.crt --key certs/ven-sim-hvac.key
  -X POST https://<vtn-host>/OpenADR2/Simple/2.0b/OadrPoll` must return HTTP 400 "Content-Type
  header must be application/xml", which proves connectivity and trust. Without `--cert` the
  handshake must fail (that is the client-certificate requirement working). Check firewall
  rules and that `host` is `0.0.0.0`, not `127.0.0.1`.
- **Handshake failures on the unit.** Either the unit does not trust the VTN certificate (give
  it `lab-ca.crt`, and make sure the URL host is in the certificate's SANs), or the VTN does not
  trust the unit's certificate (add its CA or the certificate to the trust bundle with
  `ahri-vtn certs bundle add`). The VTN log shows the OpenSSL reason.
- **Registration answered with 463.** The unit's certificate is trusted but its fingerprint is
  not pinned to the claimed VEN, or does not match the pinned one. The dashboard's "Refused
  registrations" list shows the fingerprint and offers to pin it or to add the VEN with it.
  The Findings page shows `cert-unknown-fingerprint` / `cert-fingerprint-mismatch`. With
  `auto_accept_unknown_vens = false` the VEN name must be added first (`reg-unknown-ven`).
- **"cannot offer the OpenADR 2.0b default cipher suite(s)" at start-up.** Run
  `ahri-vtn tls-selfcheck`; see section 13.6. Usually the ECC certificate is missing
  (`ecc_cert` / `ecc_key`) or the host's OpenSSL policy removed static RSA key exchange.
- **UI answers 401 or 403.** 401: no or wrong credentials; after five failures in a minute the
  address is throttled (429). 403 on a form: the page was stale, reload it (CSRF token). 403 on
  the API: send JSON, not a form body, and no cross-site `Origin`.
- **Permission denied binding port 443.** Run under the systemd unit (`CAP_NET_BIND_SERVICE`),
  in the container, or set `port = 8443`.
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

## 13. Securing the test tool: TLS, client certificates and operator access

### 13.1 What OpenADR 2.0b requires

OpenADR 2.0b (Profile Specification v1.1) is not optional about transport security. Section 9.2
requires TLS for all traffic; section 9.2.1.1 requires a client certificate on every connection,
validated during the handshake, and an application-layer 463 when the certificate does not
match the venID; conformance rule 67 requires TLS 1.2 with the default cipher suites
`TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA256` and `TLS_RSA_WITH_AES_128_CBC_SHA256`; rule 68
requires the VTN to hold both an ECC and an RSA certificate; section 10.5.1 defines the
certificate fingerprint (last 10 bytes of the SHA-256 of the DER certificate) that identifies a
VEN. AHRI 1380 Table 4 says "transport = HTTP", which most plausibly means the simpleHttp
transport rather than clear text; the tool implements the 2.0b rules and reports plain HTTP as a
development mode only.

### 13.2 The three transport modes

| `[tls] mode` | Behaviour | Use |
|---|---|---|
| `mutual` (default) | TLS 1.2+, both VTN certificates loaded, client certificate required at the handshake, fingerprint checked on every message. | Any test with real equipment. |
| `server` | TLS without a client certificate. Every message from such a VEN carries a `tls-no-client-cert` finding. | Prototypes that cannot yet present a certificate. |
| `plain` | Clear-text HTTP. The log prints an INSECURE banner, the UI shows a red badge, and every VEN gets a `transport-plain-http` finding. The UI itself stays on TLS. | Development against the simulator only. |

### 13.3 The lab PKI (`ahri-vtn certs`)

| Command | Produces |
|---|---|
| `certs quickstart --san <name-or-ip> ...` | Everything below in one go, plus a configuration snippet. |
| `certs init-ca` | `lab-ca.crt/.key` (signs VTN and VEN certificates), `operator-ca.crt/.key` (signs operator certificates), `ven-trust.pem` (the VEN trust bundle, initially the lab CA). |
| `certs vtn --san vtn.lab --san 192.168.10.5` | `vtn-rsa.crt/.key` (RSA 2048) and `vtn-ecc.crt/.key` (P-256) with those Subject Alternative Names. |
| `certs ven --name unit-3 [--ecc] [--p12-password ...]` | A client certificate for a unit or the simulator; prints its OpenADR fingerprint. |
| `certs operator --name dmr` | An operator certificate and a `.p12` for the browser; prints the fingerprint for `operator_fingerprints`. |
| `certs bundle add vendor-ca.pem` | Adds a vendor CA, an OpenADR Alliance CA, or a single self-signed device certificate to `ven-trust.pem`. |
| `certs fingerprint cert.pem` | OpenADR and SHA-256 fingerprints of any certificate. |
| `certs show` | Every certificate in `certs/` and in the configuration, with expiry warnings. |

Private keys are written with mode 0600 and must stay on the VTN host. `certs/`, `*.key`,
`*.crt`, `*.pem` and `*.p12` are ignored by git.

### 13.4 VEN identity: fingerprint policies

The trust bundle only says a certificate is acceptable; the fingerprint says which VEN it is.
`[tls] fingerprint_policy` selects how the two are bound:

- `pinned` (default): a VEN registers only if a record with its fingerprint exists. Pin through
  the Add VEN form, the API, or the one-click action on the dashboard's "Refused registrations"
  list after the unit's first attempt. Unknown or mismatching certificates get a 463.
- `first-use`: a new VEN's first certificate is pinned automatically (finding
  `cert-fingerprint-pinned-on-first-use`); later changes get a 463.
- `ca-only`: any trusted certificate is accepted for any VEN; fingerprints are recorded and
  changes are reported (`cert-fingerprint-changed`) but never refused.

In every policy a pinned fingerprint is never replaced by the VTN on its own. Re-keying a device
is an operator action on the VEN page (re-pin, or clear and let `first-use` pin again), and a
certificate pinned to one VEN identifies that VEN whatever venName it sends. Certificate
problems (expired, expiring within `expiry_warning_days`, key below the rule 68 minimum, not
X.509 v3) are findings on the VEN.

### 13.5 Operator access

The UI/API listener (`[ui]`) is always separate, always TLS, and never anonymous:

- `client_cert = "optional"` (default): a certificate from `operator_ca_file` (and, if
  `operator_fingerprints` is set, listed there) logs in by itself; a browser without one gets
  the password prompt (`basic_auth_user`, `basic_auth_password_hash` from `ahri-vtn hash-password`).
- `client_cert = "required"`: the handshake refuses connections without an operator
  certificate; with `password_auth = true` the password is a second factor.
- Scripts use `Authorization: Bearer <token>`, tokens from the environment variable named by
  `api_tokens_env`.
- Five authentication failures per minute from one address are throttled; all forms carry a
  CSRF token; cross-site requests are refused; responses carry a strict Content-Security-Policy,
  `X-Frame-Options`, `nosniff`, `Referrer-Policy` and HSTS headers; flash messages are signed.

Bind the listener to `127.0.0.1` (default) and reach it through SSH or a VPN, or set `host` to
the operator network deliberately. Import `operator-<name>.p12` into the browser for
password-less login; import `lab-ca.crt` to avoid certificate warnings for the UI.

### 13.6 Guaranteeing the mandated cipher suites

Whether the VTN can negotiate the two 2.0b default suites depends on the OpenSSL build, the
distribution's crypto policy, and the certificates loaded:

1. Static RSA key exchange (`TLS_RSA_WITH_AES_128_CBC_SHA256`) is deprecated and **disabled by
   the default crypto policy on Fedora and RHEL**; Debian and Ubuntu allow it. The tool sets its
   cipher list explicitly (`ciphers = "openadr-default"`), which overrides the policy on all of
   these, but a stricter security level or a future OpenSSL could still remove it.
2. The ECDSA suite needs the ECC VTN certificate (`ecc_cert` / `ecc_key`), the RSA suite the RSA
   one. Both are loaded by default.
3. Both suites are TLS 1.2 suites; `min_version = "1.3"` removes them. A VEN that negotiates
   TLS 1.3 is fine and is recorded with an informational `tls-cipher-not-default` finding.

The tool therefore checks at start-up that both suites are available and both chains are loaded
and refuses to run otherwise (`require_default_suites = false` downgrades this to a warning for
modern-only VENs), and `ahri-vtn tls-selfcheck` performs a real handshake per suite:

```
$ ahri-vtn tls-selfcheck
OpenSSL: OpenSSL 3.2.4 11 Feb 2025
OK   TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA256       negotiated ECDHE-ECDSA-AES128-SHA256 (TLSv1.2)
OK   TLS_RSA_WITH_AES_128_CBC_SHA256               negotiated AES128-SHA256 (TLSv1.2)
```

Make the self-check part of every deployment (the systemd unit runs it before start, the
container image runs it at build time). The recommended runtime is the pinned container image
(`Dockerfile`, Debian 12, OpenSSL 3.0 LTS); on a bare host prefer Debian 12 or Ubuntu 24.04 LTS,
or Fedora/RHEL with the explicit cipher list as shipped.

### 13.6a Known limitations

- **No revocation checking.** Neither trust store consults CRLs or OCSP. To exclude a device,
  clear or re-pin its fingerprint (pinned / first-use policies) or remove its CA from the trust
  bundle; in `ca-only` mode removing the CA is the only lever.
- **`server` mode does not verify venIDs.** Without client certificates a message's venID is taken
  as sent, as on plain HTTP; the mode exists for prototypes and is flagged with
  `tls-no-client-cert` findings. In `mutual` mode a message whose certificate is not the one pinned
  to the claimed venID is answered 463 and leaves no trace on that VEN's record (the raw message is
  kept with the real certificate's fingerprint).
- **Form tokens live as long as the process.** CSRF tokens are derived from a per-process secret and
  the logged-in principal; restart the VTN to invalidate them.
- **`ahri-vtn hash-password --password`** puts the password in the shell history and process list;
  use the interactive prompt except in scripts.

### 13.7 Deployment checklist

- Build or install on the target host and run `ahri-vtn tls-selfcheck`.
- Create the PKI (`certs quickstart --san ...` with every name and address the units will use),
  the operator password hash, and `config.toml`; keep `config.toml` and `certs/` readable by the
  service user only.
- Firewall: only the OpenADR port (443, or 8443) reaches the equipment network; the UI port
  (9443) is local, SSH or VPN only.
- Port 443: `deploy/ahri-vtn.service` grants `CAP_NET_BIND_SERVICE`; containers map the port.
- Give the vendor `lab-ca.crt` and the VTN URL; obtain the unit's certificate or CA and its
  fingerprint; add them (`certs bundle add`, Add VEN with fingerprint).
- Never run `mode = "plain"` on a host that equipment can reach.
- Back up the SQLite file (raw messages and evidence) with the VTN stopped.
- Keep `lxml`, `signxml`, `cryptography` and `aiohttp` current (`pip install -U`; `pip-audit`).
