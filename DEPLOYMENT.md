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
4.  **Dhan API Credentials:** You need a Client ID and JWT Access Token.
5.  **Supabase Credentials:** URL and Anon Key for Postgres persistence.
6.  **Discord Webhook URL:** For signal routing.

---

## 2. Configuration Files

The project contains two critical deployment files at the root:

### `Dockerfile`
A lightweight, optimized Python 3.10-slim image. It installs the dependencies from `requirements.txt` and executes `python main.py` directly without a web server wrapper (like Gunicorn or Uvicorn).

### `fly.toml`
The Fly configuration file. 
*   It explicitly avoids `[http_service]` blocks.
*   It configures the VM size (`shared-cpu-1x`, 1GB memory) necessary for Pandas/NumPy operations inside the fetchers.
*   The primary region is set to `bom` (Mumbai) to ensure minimal latency to the NSE servers.

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
  DHAN_CLIENT_ID="your_dhan_client_id" \
  DHAN_ACCESS_TOKEN="your_dhan_jwt_token" \
  SUPABASE_URL="https://your-project.supabase.co" \
  SUPABASE_KEY="your_supabase_anon_key" \
  DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  DISCORD_HEALTH_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  NIFTY_SECURITY_ID="13" \
  NIFTY_EXCHANGE="IDX_I"
```

### Step 3: Deploy

With the app created and secrets loaded, build and deploy the container:

```bash
fly deploy
```

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

---

## 4. Troubleshooting & Maintenance

### Instance Refused Connection / Healthcheck Failures
If you see healthcheck failures in the Fly logs, ensure that `fly.toml` does **not** contain an `[http_service]` block. ARES does not listen on a port, so network healthchecks will inevitably fail and cause Fly to restart the container constantly.

### Scaling and Cron Jobs
The system is designed to run only during NSE Market Hours (09:15 to 15:30 IST).
To save costs and avoid GitHub Actions scheduling delays, scaling is handled as follows:

1. **Auto Stop (Scale to 0):** Handled entirely by `main.py`. The process checks the time and automatically breaks its loop at 15:30 IST. Since `[http_service]` is removed from `fly.toml`, Fly simply lets the machine power down and scale to zero.
2. **Auto Start (Scale to 1):** Configured via an external precision cron service (e.g., [cron-job.org](https://cron-job.org)). 
   * **URL:** `POST https://api.machines.dev/v1/apps/<APP_NAME>/machines/<MACHINE_ID>/start`
   * **Header:** `Authorization: Bearer <FLY_DEPLOY_TOKEN>`
   * **Schedule:** `08:55 AM IST`, Monday - Friday.
