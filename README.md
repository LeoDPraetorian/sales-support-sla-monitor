# Sales-Support SLA Monitor

Posts a header-only card to Slack when email lands on `sales-support@praetorian.com`,
and escalates any thread that goes **24 calendar hours** without a Praetorian reply.
Runs on a GitHub Actions cron (every 15 min). No database — state lives in Gmail labels.

## SLA logic
- An **external** (non-`@praetorian.com`) message (re)starts a 24h clock.
- The clock is **satisfied** when a Praetorian address replies.
- **Breach** = the latest message in a thread is external and older than 24h → one
  escalation card per arming (re-arms automatically after the next Praetorian reply).

## What it posts
- **New mail:** `:envelope_with_arrow:` Subject · From · Received · thread link.
- **Breach:** `:warning:` Subject · last external sender · waiting-since · age · thread link.

---

## Setup

### 1. Slack channel + incoming webhook
1. Create the channel (e.g. `#sales-support-sla`).
2. Mint an incoming webhook for it. With PMO tools configured:
   ```bash
   cd "$PMO_PLUGIN_ROOT/tools"
   npx tsx lib/slack-oauth-setup.ts          # one-time, pulls GuardBot creds from 1Password
   npx tsx lib/slack-incoming-webhook.ts sales-support-sla   # pick the channel in the consent screen
   ```
   Copy the `https://hooks.slack.com/services/...` URL.

### 2. Gmail credential for the runner
`sales-support@` is a Google Group, so read a **member inbox** filtered by the alias.
Pick ONE:

- **OAuth refresh token (fastest):** an OAuth client + refresh token for an account that
  receives the alias. Needs scope `https://www.googleapis.com/auth/gmail.modify`.
  Set secrets `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`
  and (optional) var `GMAIL_USER=me`.
- **Service account + domain-wide delegation (cleanest, needs Workspace admin):**
  authorize the SA for `gmail.modify`, then set secret `GOOGLE_SERVICE_ACCOUNT_JSON`
  (the key file contents) and var `GMAIL_USER=<group-member@praetorian.com>`.

### 3. GitHub repo config
**Secrets** (Settings → Secrets and variables → Actions → Secrets):
- `SLACK_WEBHOOK_URL`
- Gmail set from step 2.

**Variables** (optional — defaults in `sla_monitor.py`):
- `GMAIL_USER` (default `me`), `SALES_SUPPORT_ADDRESS`, `INTERNAL_DOMAINS`, `SLA_HOURS`.

### 4. Test before going live
Actions → **Sales-Support SLA Monitor** → **Run workflow** → set **dry_run = true**.
The logs show exactly what *would* be posted, with no Slack messages or label writes.
Flip to a normal run once the output looks right.

## Local dry-run
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=...
export DRY_RUN=1            # print instead of post; never writes labels
python sla_monitor.py
```

## Tuning notes
- **Notification scope:** by default every message to the alias is announced (includes
  internal replies/forwards). To cut noise, restrict notifications to external inbound
  only — see `notify_new()`. (Open decision.)
- GitHub cron is best-effort and can lag a few minutes; irrelevant for a 24h SLA.
- "Internal" = sender domain in `INTERNAL_DOMAINS`. Add subsidiary domains there if needed.
