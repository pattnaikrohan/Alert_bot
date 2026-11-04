# Alert Bot

**One bot, many projects.** Alert Bot watches the web scraping that a trigger table kicks
off through Azure Data Factory. It compares every run with baselines learned from history,
groups failing checks into incidents, explains each one, and alerts the project's team
through Power Automate. A React portal lets people sign in and see only their own projects.

> **This is the prototype.** It runs entirely on fake data: two simulated projects
> (Slipstream and CSDW) with eight weeks of history, a simulated ADF, and fake people.
> The check engine, incident engine, investigator, alert payloads, API and portal are
> the real code. Production swaps the fake adapters for Azure SQL and the ADF REST API
> through config (see [From prototype to production](#from-prototype-to-production)).

```
 trigger table ─┐                    ┌─ checks + learned baselines ─┐
 ADF runs ──────┼── read-only ──►    │  incident grouping           │ ──► Power Automate ──► Teams / Outlook
 scraped output ┘   connectors       └─ investigator (rules → AI) ──┘
                                              │
                                        metadata DB ◄── FastAPI (checks access on every call) ◄── React portal
```

## Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/) and Node 20+.

```powershell
uv sync                                   # Python 3.12 env + dependencies
uv run alertbot demo                      # fake estates, learn baselines, play the story
uv run alertbot serve                     # API on http://localhost:8000

cd portal; npm install; npm run dev       # portal on http://localhost:5173
```

Sign in as **Rohan Pattnaik** (owner of both projects). You can also pick **Priya**
(Slipstream only) or **Alex** (no projects) to see access control at work.

The home screen is **the bot itself**: a hand-built vector robot with glossy shading
and parallax depth. It uses no WebGL and no filters; one animation loop drives it, so it holds
60 fps on any machine. Its mood comes from live data:

| Mood | Robot |
|---|---|
| Healthy | Happy ∩ ∩ eyes, blue lights, waves now and then |
| Warning | Focused eyes, amber antenna, megaphone half up |
| Critical | Wide eyes, red antenna, megaphone up and shouting |

Beyond the moods:
- It follows your pointer, blinks, and hops when you poke it.
- A speech bubble types out what it has to say about your projects.
- Floating cards link to each incident.
- An optional chime plays when a new incident opens.
- **Dashboard** in the top bar switches to the full view.

Single-process option: `cd portal; npm run build`, then `uv run alertbot serve` also
serves the portal on http://localhost:8000.

## The demo story

`alertbot demo` replays the architecture doc's example:

1. It builds 8 weeks of normal history for 9 sites, then learns per-site, per-weekday baselines.
2. The first cycle is all green.
3. **Slipstream Site A** comes back with 88% of its rows missing and `price` empty in 63% of rows.
4. The bot fails two checks (row count, null spike). Because they share a site and a run, it opens **one** critical incident: "Site A scrape incomplete".
5. The investigator calls its read-only tools. It finds HTTP 200, no activity errors, an unchanged schema and seven normal previous runs. Its conclusion: *the site most likely changed its page layout*.
6. It builds the Power Automate alert and stores it as a **dry-run**, because no flow URL is set yet.

Then try more:

```powershell
uv run alertbot scenario --list
uv run alertbot scenario csdw blocked              # HTTP 403 / captcha → zero rows
uv run alertbot scenario csdw failed --site "Retailer North"
uv run alertbot scenario slipstream schema-drop    # accept the change in Tables
uv run alertbot scenario slipstream recover        # incident resolves, resolution alert
uv run alertbot status                             # health, incidents, delivery mode
```

| Scenario | What the bot reports |
|---|---|
| `incomplete` | Scrape incomplete: row count + null spike → likely site layout change |
| `blocked` | Returned no data: empty site + mismatch → site blocked the scraper (403) |
| `failed` | Scrape failed: run failed → scraper timed out (504) |
| `stuck` | Trigger stuck: pending with no pipeline run |
| `slow` | Scrape running slow: in progress far longer than its learned duration |
| `duplicates`, `spike` | Duplicate keys; row volume unusually high |
| `schema-add`, `schema-drop` | Table schema changed (added = warning, removed/retyped = critical) |
| `recover` | A healthy re-run that resolves the site's incident |

## The Power Automate slot

Each project has an alert slot that is deliberately left empty:

```yaml
# config/projects/slipstream.yaml
alerts:
  notifier: power_automate
  flow_url: env://PA_SLIPSTREAM_URL      # production: kv://pa-slipstream-alerts
```

```ini
# .env  (copy from .env.example; git-ignored)
PA_SLIPSTREAM_URL=          # ← paste the flow's HTTP URL here
PA_CSDW_URL=
```

While it's empty, every alert is fully built and visible in the portal, including a
preview of the Teams card, but nothing is sent. Once it's set, run
`uv run alertbot notify-test slipstream`. Building the flow is a step-by-step guide in
[docs/power-automate.md](docs/power-automate.md), and the payload contract is
[contracts/alert-payload.schema.json](contracts/alert-payload.schema.json).

## How it maps to the architecture

| Architecture piece | Where it lives |
|---|---|
| Connectors (swappable adapters) | `src/alertbot/connectors/`: `sql_source.py` (Azure SQL / SQLite), `adf.py` (ADF REST + fake), `notifiers.py` (Power Automate, Teams workflow) |
| Checks + baselines | `engine/checks.py` (all 10 checks from the doc), `engine/baselines.py` |
| Incident grouping | `engine/incidents.py`: open, worsen, improve, resolve; one alert per change |
| AI investigator | `ai/tools.py` (7 read-only, audited tools), `ai/rules.py` (runs today), `ai/investigator.py` (LangGraph agent, used once a model is set) |
| Scheduler | `alertbot worker` (honours each project's cron); in Azure, a Container Apps job running `alertbot cycle` |
| Metadata DB | `db/models.py`: users, projects, memberships, runs, metrics, baselines, schema snapshots, check results, incidents, events, tool-call audit, alert outbox, mutes |
| FastAPI | `api/`: Entra token validation plus a membership check on every project route (non-members get 404) |
| Portal | `portal/`: React + Vite + TypeScript, Tailwind v4 + Radix + lucide, MSAL for Entra sign-in. The design system (tokens, dark/light themes and shades, fonts, shell, cards, tables) is ported from Team Tesseract's frontend; see `portal/src/styles/index.css` |

### Design decisions worth knowing

- **Thresholds are learned, not configured.** The normal range for each site's metric uses the median and median absolute deviation of its last 8 same-weekday runs. It falls back to all days, then to "still learning". One bad run can't widen the band. Projects can still pin hard rules (for example `row_count: { min: 500 }`).
- **Detection never depends on AI.** Checks and grouping are plain Python. Every incident gets a rule-based analysis (metric, expected range, actual value, likely cause) gathered through the same audited tools the AI agent will use.
- **The AI slot is ready.** `config/ai.yaml` has empty `provider` / `model` / `endpoint` slots and a Key Vault reference for the key. Install the extra (`uv sync --extra ai`), fill in the slots and set `enabled: true`. Any AI failure falls back to the rule-based analysis.
- **Guardrails:**
  - Tools are bound to one project and run only fixed, parameterised queries.
  - There is no free-form SQL anywhere.
  - Every tool call is logged against its incident and shown in the portal.
- **Alerts go through an outbox:**
  - An alert is written in the same transaction as the incident change.
  - Each alert has an idempotency key and is retried until delivered.
  - Owners can mute alerts for planned work; incidents keep being recorded.
- **Monitoring the bot itself:**
  - Every cycle is a heartbeat, and the portal shows "Bot watching · checked 2 min ago".
  - After 30 quiet minutes the indicator turns amber.
  - `/api/health` reports the same, so Azure Monitor can alert on it.
- **Secrets never touch config:**
  - Config files hold only references (`kv://`, `env://`).
  - The API shows the reference, never the value. There's a test for that.

## Commands

| Command | What it does |
|---|---|
| `alertbot demo` | Reset to a fresh fake estate, learn baselines, play the story |
| `alertbot scenario <project> <name>` | Inject trouble (or `recover`) and run a cycle |
| `alertbot cycle [project]` | Run one cycle now |
| `alertbot worker` | Run continuously on each project's schedule |
| `alertbot serve` | API (plus the built portal) on :8000 |
| `alertbot status` / `incidents` | Health, open incidents, delivery mode |
| `alertbot notify-test <project>` | Send a test card through the project's flow |
| `alertbot config-check` | Validate config; show what each secret reference resolves to (never the value) |
| `alertbot members list/add/remove` | Manage who can see which project |
| `alertbot backfill [project]` | Measure history so baselines exist from day one |

## Adding a project

1. Copy `config/projects/slipstream.yaml` to `config/projects/<key>.yaml`. Point it at the project's sources, pipeline, trigger table and outputs, and map the trigger table's own status values under `statuses`.
2. `uv run alertbot config-check` validates it (unknown checks, missing sources and typos fail loudly).
3. `uv run alertbot backfill <key>`, then `uv run alertbot members add someone@company.com <key> --role owner`.

There are no code changes. A project on Synapse, Databricks or Airflow needs only a new adapter
that satisfies the Protocols in `connectors/base.py`.

## From prototype to production

| Prototype (now) | Production (config + deployment, no engine changes) |
|---|---|
| `type: sqlite` estates | `type: azure_sql`, `server` + `database`, managed identity with `db_datareader` + `VIEW DEFINITION` |
| `pipeline.type: fake_adf` | `type: adf` + `subscription_id` / `resource_group` / `factory`; read access to pipeline runs (check whether `Reader` covers `queryPipelineRuns`, or use a custom read-only role) |
| SQLite metadata DB | `ALERTBOT_DB_URL` pointing at Azure SQL |
| Dev sign-in picker | `ALERTBOT_AUTH_MODE=entra` + two app registrations (SPA + API scope); portal `VITE_ENTRA_*` |
| Dry-run alerts | Flow URL in Key Vault (`kv://…`), `ALERTBOT_KEYVAULT_URL` set |
| `alertbot worker` locally | Container Apps job running `alertbot cycle` every 10 min; API on App Service / Container Apps; portal on Static Web Apps |

`uv sync --extra azure` installs the Azure SDKs the real adapters use.

## Development

```powershell
uv run pytest            # 21 tests: the story end to end, every scenario, access control, alert delivery, config
uv run ruff check src tests
cd portal; npm run typecheck; npm run build
```

Project layout:

```
config/            projects/*.yaml, ai.yaml, members.yaml (fake people)
contracts/         alert payload JSON schema (the Power Automate contract)
docs/              power-automate.md
src/alertbot/      connectors/ engine/ ai/ db/ api/ sim/ (the fake estate) cli.py
portal/            React + Vite + TypeScript
tests/
```
