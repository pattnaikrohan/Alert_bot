# Going live: CSDW Risk Monitor

The live setup lives beside the fake-data prototype and doesn't replace it:

| | Prototype | Live |
|---|---|---|
| Config | `config/` | `config-live/` |
| Env file | `.env` | `.env.live` (layered over `.env` via `ALERTBOT_ENV_FILE`) |
| Metadata DB | `.data/alertbot.db` | `.data/alertbot-live.db` |
| Sign-in | fake-user picker | Microsoft (Entra ID), Authenticator required |

## 1. Snowflake (read-only)

1. Copy `.env.live.example` to `.env.live`.
2. Set `SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_USER`.
3. Run the check:
   ```powershell
   $env:ALERTBOT_ENV_FILE = ".env.live"
   uv run alertbot feeds-check
   ```
   - The pilot uses `authenticator: externalbrowser`, so a browser window opens for Microsoft sign-in with Authenticator.
   - The token is then cached. Snowflake's `ALLOW_ID_TOKEN` account setting must be on, or the prompt returns on every connection.
   - The table shows each feed's latest row and its volume over the last 24 h and 7 days. Compare it with the team's unified monitoring query.
4. Production: switch to a key-pair service user:
   - `authenticator: snowflake_jwt`;
   - `private_key: kv://snowflake-alertbot-key`;
   - a role with `SELECT` on the four tables and `MONITOR` on the SWIC task.

## 2. Microsoft sign-in with Authenticator

1. Have an Entra admin run `scripts/setup-entra.ps1 -Users <chinmoy's email> -CreateConditionalAccess`. It creates:
   - the API and portal app registrations;
   - an "assigned users only" restriction;
   - a report-only Conditional Access policy that requires MFA.
2. Paste its output into `.env.live` and `portal/.env.local`.
3. After checking the sign-in log, switch the Conditional Access policy to **On**.
4. The API enforces MFA independently (`ALERTBOT_REQUIRE_MFA=true`): it rejects any token whose `amr` claim shows no second factor. A sign-in without Authenticator never reaches project data.

## 3. Access: Chinmoy on CSDW only

```powershell
$env:ALERTBOT_ENV_FILE = ".env.live"
uv run alertbot init-db
uv run alertbot members add <chinmoy's email> csdw --role owner --name "Chinmoy Jagjit"
```

- His first Microsoft sign-in links to this row by email.
- CSDW is the only project in `config-live`, and every API route re-checks membership. Any other project returns 404 for him.

## 4. Run

```powershell
$env:ALERTBOT_ENV_FILE = ".env.live"
uv run alertbot backfill csdw --days 30   # learn normal volumes first; never alerts
uv run alertbot serve                     # API + portal
uv run alertbot worker                    # checks every 15 min
```

While `PA_CSDW_URL` is empty, alerts run in dry-run mode: they're built and shown in the portal but not sent. See `power-automate.md`.

## Assumptions to confirm (marked `TODO` in `config-live/projects/csdw.yaml`)

- **Timezone:** the timezone of the run times in the matrix. UTC is assumed.
- **Function App telemetry:** Application Insights app ids for the five Function Apps. They let the bot show a function's own errors.
- **Ingestion timestamps for SWIC and cyclones:** `EFFECTIVETIME` and `DATE_TIME_IN_UTC` are event times, not load times.
  - Both feeds are set as event-driven: empty runs are normal, and only no rows for 7 days alerts.
  - An insert timestamp column would allow per-run checks.
- **SG Terminal task:** the name of the task that calls `EXECUTE_NOTEBOOK_IF_STREAM_HAS_DATA()`, for `TASK_HISTORY`.
- **NEWS API "food" exclusion:** it also drops any general article with "food" in its URL. This matches the team's monitoring query today.
- **Carrier advisories:** the weekly run day.
