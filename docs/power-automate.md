# Connecting Power Automate

The bot is already producing every alert. Until a flow URL is configured it runs in
**dry-run**: alerts are built, stored and shown in the portal (Alerts tab, and on each
incident with a preview of the Teams card), but nothing is sent. Connecting a flow
takes about ten minutes per project, or once for a shared flow.

## 1. Create the flow

1. In Power Automate: **Create → Instant cloud flow → When an HTTP request is received**
   (a premium trigger).
2. **Who can trigger the flow:** *Anyone*. The bot authenticates with the signed URL,
   which is why that URL is treated as a secret (see step 3).
3. **Request Body JSON Schema:** paste the `properties` object from
   [`contracts/alert-payload.schema.json`](../contracts/alert-payload.schema.json)
   (wrapped as `{"type": "object", "properties": { … }}`). This gives you dynamic
   content such as `title`, `severity` and `portal_url`.

## 2. Post to Teams

Add **Microsoft Teams → Post card in a chat or channel**:

| Field | Value |
|---|---|
| Post as | Flow bot |
| Post in | Channel |
| Team / Channel | the project's channel, e.g. *Slipstream Data Alerts* |
| Adaptive Card | expression `triggerBody()?['card']` |

The bot ships a ready-made Adaptive Card in `card`, so the flow doesn't need to build
one. A shared flow for several projects can add a **Switch** on `project` and post to a
different channel per case.

## 3. Email owners for critical incidents

Add a **Condition**: `triggerBody()?['email_owners']` *is equal to* `true`. In *If yes*,
add **Office 365 Outlook → Send an email (V2)**:

| Field | Value |
|---|---|
| To | expression `join(triggerBody()?['owner_emails'], ';')` |
| Subject | `[@{triggerBody()?['severity']}] @{triggerBody()?['project_name']}: @{triggerBody()?['title']}` |
| Body | `summary`, `likely_cause`, `action` and a link to `portal_url` |

`email_owners` is true for critical incidents that open or get worse (configurable per
project with `alerts.email_owners_on`). Resolutions go to Teams only.

## 4. Give the bot the URL

Save the flow and copy the trigger's **HTTP URL**.

**Locally:** put it in `.env` (never in the YAML; `.env` is git-ignored):

```
PA_SLIPSTREAM_URL=https://...
```

**In production:** store it in Key Vault and point the project at it:

```yaml
alerts:
  flow_url: kv://pa-slipstream-alerts
```

Then check the connection:

```
uv run alertbot notify-test slipstream
```

A clearly labelled test card should appear in the channel. `alertbot status` now shows
the flow URL as *set*, and the portal's badge changes from **Alerts in dry-run** to
**Alerts live**.

## No premium licence?

Use a Teams **Workflows** webhook instead: in the channel, choose **Workflows → Post to a
channel when a webhook request is received**, copy its URL, and set:

```yaml
alerts:
  notifier: teams_workflow
  flow_url: env://PA_SLIPSTREAM_URL
```

The bot then posts only the Adaptive Card, in the shape Teams webhooks expect. Email
needs the premium flow.

## How delivery behaves

- **When the bot sends:** only when an incident opens, gets worse, or resolves.
- **Retries:** each send is retried with backoff. A failed send stays queued and is retried on later cycles, up to 6 attempts.
- **Idempotency key:** each alert carries `x-alertbot-idempotency-key`, which a flow can use to drop duplicates.
- **Mutes:** project owners can mute alerts from the portal for planned work. Incidents are still recorded.
