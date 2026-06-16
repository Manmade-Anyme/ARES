# Fly.io Deployment & Automation

ARES is fully Dockerized and optimized for Fly.io deployments. It uses GitHub Actions to automate daily start/stop cycles, minimizing server costs.

## 1. Initial Deployment to Fly.io

1. Install `flyctl` and authenticate.
2. Ensure you have the `fly.toml` file in the root.
3. Import your `.env` secrets into Fly:
   ```bash
   cat .env | flyctl secrets import
   ```
4. Deploy the app (use `--depot=false` if the builder times out):
   ```bash
   flyctl deploy
   ```

## 2. GitHub Actions Automation

To save costs, a GitHub workflow (`fly-schedule.yml`) scales the app to 1 instance at **09:15 AM IST** and scales it down to 0 at **15:30 PM IST** on weekdays.

**Setup Steps:**
1. Generate a Deploy Token from Fly:
   ```bash
   fly tokens create deploy
   ```
2. Navigate to your GitHub Repository -> **Settings** -> **Secrets and variables** -> **Actions**.
3. Create a **New repository secret**:
   - **Name:** `FLY_API_TOKEN`
   - **Secret:** *(Paste the token generated above)*
4. The GitHub Action will now automatically run the schedule.

## 3. Manual Override

You can manually trigger a start or stop if you need to run the system outside of normal hours:
1. Go to the **Actions** tab in GitHub.
2. Select **Fly App Schedule**.
3. Click **Run workflow** and select your desired action (`start` or `stop`).
