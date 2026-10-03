# ARES Deployment Guide (Fly.io)

This guide covers the end-to-end deployment of the ARES (Adaptive Reversal & Entry Signal) system to [Fly.io](https://fly.io), a platform optimized for running Docker containers globally.

Because ARES is a background algorithmic engine (it polls an API and sends webhooks), it does **not** expose an HTTP web server. Therefore, the deployment avoids HTTP health checks to prevent "instance refused connection" errors.

---

## 1. Prerequisites

1.  **Docker Installed:** Though Fly handles builds remotely by default, having Docker locally is good for testing.
2.  **Flyctl Installed:** The official CLI for Fly.io.
    *   *Mac/Linux:* `curl -L https://fly.io/install.sh | sh`
    *   *Windows:* `pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"`
3.  **Fly Account:** Run `fly auth login` to authenticate or sign up.
4.  **Supabase Credentials:** URL and Anon Key for Postgres persistence. Also stores the Dhan credentials (`client_id`, `access_token`) inside the `api_keys` table.
6.  **Discord Webhook URL:** For signal routing.

---

## 2. Configuration Files

The project contains two critical deployment files at the root:

### `Dockerfile`
A lightweight, optimized Python 3.10-slim image. It installs the dependencies from `requirements.txt` and executes `python main.py` directly. Inside `main.py`, the core trading engine, REST poll loop, and WebSocket tick feed run concurrently within a single Python process.

### `fly.toml`
The Fly configuration file. 
*   It explicitly avoids `[http_service]` blocks.
*   It configures the VM size (`shared-cpu-1x`, 1GB memory) necessary for Pandas/NumPy operations inside the fetchers.
*   The primary region is set to `sin` (Singapore) to ensure minimal latency to the NSE servers.

---

## 3. Deployment Steps

### Step 1: Launch the Application (Initialize)

Run the launch command in the root of the ARES directory.
```bash
fly launch
```
*   When prompted: *"An existing fly.toml file was found... Would you like to use this fly.toml configuration?"* -> **Select Yes**
*   When prompted: *"Do you want to tweak these settings before proceeding?"* -> **Select No**

### Step 2: Inject Secrets

ARES strictly uses environment variables (Pydantic Settings) for all configurations. Do **not** put these in your `fly.toml` or commit them.

Push your secrets directly to Fly's encrypted vault:

```bash
fly secrets set \
  SUPABASE_URL="https://your-project.supabase.co" \
  SUPABASE_KEY="your_supabase_anon_key" \
  DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  DISCORD_HEALTH_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  NIFTY_SECURITY_ID="13" \
  NIFTY_EXCHANGE="IDX_I"
```

The Jev worker runs inside the same Fly `app` Machine as `main.py` and
additionally requires `SUPABASE_SERVICE_ROLE_KEY` plus its TypeSafe
credentials. Configure the service-role key in the Fly app's secure secret
settings; use the backend Supabase service-role key, not an anon key.
`SUPABASE_KEY` is a separate binding and is not used by Jev. Never commit or
share its value in chat. Repository Actions secrets do not automatically
populate Fly runtime secrets.

### Step 3: Deploy (bootstrap only)

With the app created and secrets loaded, build and deploy the container once by
hand to confirm the image builds and the app boots:

```bash
fly deploy
```

> **This is the last manual deploy you need.** After this, every push to `main`
> runs the test suite and deploys automatically via
> [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) — see README
> section 7.
>
> Note there is **no market-hours guard**: merging to `main` between 09:15 and
> 15:30 IST restarts the machine mid-session, which resets VWAP and the candle
> buffers for the rest of the day. Merge after the close.
>
> The pipeline only ever runs `flyctl deploy`. It never creates the app and
> never touches the secrets set in Step 2 — those live on the Fly app.

### Step 3b: GitHub repository secrets (required for CI)

The Fly secrets from Step 2 are read by the **running app**. The CI pipeline
reads its own, separate set from **GitHub Actions secrets** — setting one does
not set the other, and they are stored in different places entirely:

| Secret | Store | Read by |
| :--- | :--- | :--- |
| `DISCORD_HEALTH_WEBHOOK_URL` | Fly app secret (Step 2) | `alerts.py` at runtime — heartbeats, error alerts |
| `DISCORD_HEALTH_WEBHOOK_URL` | **GitHub repo secret** | the `notify` job, on a failed `main` deploy |
| `FLY_API_TOKEN` | **GitHub repo secret** | the `deploy` job, to authenticate `flyctl` |
| `SUPABASE_URL` | **GitHub repo secret** | the `train` job in `ml_training.yml`, for offline model training |
| `SUPABASE_KEY` | **GitHub repo secret** | the `train` job in `ml_training.yml`, for offline model training |
| `DISCORD_WEBHOOK_URL` | **GitHub repo secret** | optional weekly ML report, including the Sharpe diagnostic and SHAP chart; failed delivery does not block training, artifact publication, or deployment |

Set both repo secrets:

```bash
# deploy-scoped token, so a leak here cannot reach your other Fly apps
fly tokens create deploy -a ares-xzy-gq -x 8760h \
  | gh secret set FLY_API_TOKEN -R <owner>/ARES

# same webhook value as the Fly secret, piped so it never lands in shell history
grep '^DISCORD_HEALTH_WEBHOOK_URL=' .env | cut -d= -f2- \
  | gh secret set DISCORD_HEALTH_WEBHOOK_URL -R <owner>/ARES

# weekly ML report and attached SHAP chart
grep '^DISCORD_WEBHOOK_URL=' .env | cut -d= -f2- \
  | gh secret set DISCORD_WEBHOOK_URL -R <owner>/ARES
```

Verify with `gh secret list -R <owner>/ARES` — all three must appear.

> **If `DISCORD_HEALTH_WEBHOOK_URL` is missing from the repo secrets, failed
> deploys alert nobody.** The `notify` job skips with only a `::warning::`
> annotation in the run log rather than failing, so the absence is easy to miss
> — the alert you were relying on simply never arrives.

### Step 4: Monitor Logs

To verify the system has started correctly, connected to the broker, and sent the initial Discord heartbeat:

```bash
fly logs
```

You should see something like:
```text
[+] Initializing ARES Engine...
[+] Dynamic PDH/PDL initialized: 24500.0 / 24100.0
[+] Warmup state: Collecting initial candles...
```

### Troubleshooting: deploy skipped after a stalled test job

The `deploy` job requires the `test` job to succeed. A cancelled or timed-out
test job means Fly deployment did not run; inspect the test log before changing
Fly credentials or region settings. For example, [run 37121217919](https://github.com/Manmade-Anyme/ARES/actions/runs/37121217919)
cancelled the Test step after 14m51s and skipped deployment after the Singapore
configuration change in PR #124.

MANM-219 locally reproduced a stall in `test_run_catches_general_exception` in
`tests/unit/test_task210_system_one.py`: the test mocked sleep but used the real
market-session clock. Outside trading hours, the worker kept waiting and never
reached the mocked polling failure or test exit. The regression test now pins
the session predicate, mocks the delivery thread, and bounds sleep with a
`KeyboardInterrupt`, verifying polling resumes after the exception. The
cancelled job's annotations establish the blocked test gate; its raw test logs
were unavailable during diagnosis, so the reproduction supplies the test-level
evidence.

After the repair merged, [main run 37131943959](https://github.com/Manmade-Anyme/ARES/actions/runs/37131943959)
passed tests and deployed successfully. Run `37132294024` confirmed all three
Machines in Singapore. These checks establish rollout, while worker startup
requires its own runtime checks below.

### Troubleshooting: Jev exits after a successful deploy

The read-only `check-region.yml` workflow collects Machine states, exit events,
secret names/status, and recognized startup error signatures without printing
raw application logs or secret values. After merging the diagnostic workflow,
dispatch it with:

```bash
gh workflow run check-region.yml -R Manmade-Anyme/ARES --ref main
```

Before merge, use the current feature branch ref. A successful collection job
confirms diagnostics completed; it does not mean Jev is healthy.

Run `37132792338` found the primary Jev Machine repeatedly exiting with code 1,
without OOM or a requested stop. Startup logs identified
`SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set`; Fly secret metadata
confirmed `SUPABASE_SERVICE_ROLE_KEY` was missing. Add the backend service-role
key through Fly's secure secret settings, then repeat diagnostics and verify
the polling loop initializes without repeated exits. Database migration and
access readiness remain unverified because startup failed before database
initialization. Current deployments run Jev inside the scheduled `app` Machine,
so start the app Machine once after configuration is fixed; no separate Jev
Machine or Jev cron is required.

---

## 4. Scaling, Lifecycle & Scheduled Execution

The Fly `app` process is configured to run during NSE Market Hours (09:15 to 15:30 IST). To minimize Machine costs and ensure reliable starts/stops, we use an external precision scheduling service (specifically [cron-job.com](https://cron-job.com)) targeting that Machine's Fly.io Machines API.

The `app` Machine runs `scripts/run_app_with_jev.sh`, which starts both
`python main.py` and `python -m system_one.consumer` inside one 1 GB Machine.
After startup loads its persisted rollout state, Jev performs no database
polling, Jev requests, or Discord delivery outside 09:15-15:30 IST. At the next
session it resumes normal polling and database recovery with the same cutoff
and freshness guards. An after-hours start waits for the next session instead
of exiting.

`fly.toml` scopes `policy = 'never'` to `app`. The existing weekday start cron
therefore starts both ARES and Jev, and the normal market-close exit or 15:35
backup stop shuts both down. Jev no longer has an always-running Machine and no
separate Jev cron is required.

### 4.1 Automated Lifecycle Flow
1. **Auto Start (09:10 IST):** Triggered via a `POST` request from `cron-job.com` to the Fly Machines `/start` API.
2. **Graceful Exit (15:30 IST):** The ARES engine (`main.py`) monitors the time and breaks the execution loop at 15:30 IST. The runner stops Jev after `main.py` exits, then the process exits normally, which powers down the Fly Machine.
3. **Auto Stop Safety Backup (15:35 IST):** A secondary `POST` request from `cron-job.com` to the Fly Machines `/stop` API acts as a backup shutdown trigger (does not delete the machine).

Both scheduled requests must target the single `app` Machine ID. After the
reviewed deployment, use `fly machine list -a ares-xzy-gq` and
`fly machine status <MACHINE_ID> -d -a ares-xzy-gq` to verify the Machine's
process group, 1 GB memory, and `never` restart policy. Historical stopped Jev
Machines may still appear until manually destroyed; do not add them to cron.
These checks are rollout instructions, not a deployment performed by this PR.

### 4.2 Step-by-Step Setup Guide

#### Step 1: Retrieve App & Machine Details
Find your `app` Machine ID and verify your App Name by running:
```bash
fly machine list
```
*Note: Your app name is `ares-xzy-gq`.*
Use this `app` Machine ID for `<YOUR_MACHINE_ID>` in both cron jobs below.

#### Step 2: Generate Fly API Deploy Token
Generate a scoped token for authorization:
```bash
fly tokens create deploy -a ares-xzy-gq
```
*Keep this token safe; you will need it for the cron configuration.*

#### Step 3: Configure the "Start" Cron Job (cron-job.com)
1. Log in to `cron-job.com` and click **Create Cronjob**.
2. Set the following options:
   - **Title:** `ARES Start`
   - **Address (URL):** `https://api.machines.dev/v1/apps/ares-xzy-gq/machines/<YOUR_MACHINE_ID>/start`
   - **Request Method:** `POST`
   - **Schedule:** Select **Custom/Cron expression** -> `10 9 * * 1-5` (Runs at 09:10 AM, Monday to Friday).
   - **Timezone:** `Asia/Kolkata`.
3. Add these two **Request Headers**:
   - `Authorization` : `Bearer <YOUR_FLY_DEPLOY_TOKEN>`
   - `Content-Type` : `application/json`
4. Click **Create**.

#### Step 4: Configure the "Stop" Cron Job (Safety Backup)
*Note: Calling the `/stop` endpoint changes the state to `stopped`. It will NOT delete or destroy the machine.*
1. Create a second Cron Job.
2. Set the following options:
   - **Title:** `ARES Stop`
   - **Address (URL):** `https://api.machines.dev/v1/apps/ares-xzy-gq/machines/<YOUR_MACHINE_ID>/stop`
   - **Request Method:** `POST`
   - **Schedule:** Select **Custom/Cron expression** -> `35 15 * * 1-5` (Runs at 03:35 PM, Monday to Friday).
   - **Timezone:** `Asia/Kolkata`.
3. Add the same **Request Headers**:
   - `Authorization` : `Bearer <YOUR_FLY_DEPLOY_TOKEN>`
   - `Content-Type` : `application/json`
4. Click **Create**.
