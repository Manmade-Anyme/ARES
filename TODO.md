# ARES Project Todo & Roadmap

## Active Tasks

### 🔒 Dhan Token Automation (Programmatic Refresh)
**Description:** Replace the manual 24-hour `DHAN_ACCESS_TOKEN` update process with an automated TOTP-based generation mechanism.

**Implementation Details:**
- **Mechanism:** Use the Dhan programmatic login endpoint (`https://auth.dhan.co/app/generateAccessToken`) which requires `dhanClientId`, `pin`, and a dynamic `totp`.
- **Dependencies:** `pyotp` for generating time-based one-time passwords and `requests` for API calls.
- **Security:** Requires `DHAN_PIN` and `DHAN_TOTP_SECRET` (Seed) to be stored in the `.env` file.
- **System Integration:**
    - The `AresEngine` or `PriceFetcher` should catch `401 Unauthorized` errors.
    - Upon detection, a `TokenManager` class should generate a fresh token.
    - The new token should be used to re-initialize the `DhanContext` and ideally update the `.env` file for persistence across restarts.

**Steps to Implement:**
1. [ ] Enable TOTP on Dhan account and save the Secret Key (Seed).
2. [ ] Add `DHAN_PIN` and `DHAN_TOTP_SECRET` to `.env`.
3. [ ] Create `fetchers/auth_manager.py` with the logic from the POC.
4. [ ] Integrate `AuthManager` into `main.py` error handling loop.
5. [ ] Add a background task to refresh the token every 23 hours to prevent any downtime.

---

## Completed Tasks
- [x] Dynamic PDH/PDL fetching from Dhan API.
- [x] Target sorting logic based on proximity and momentum.
- [x] Supabase RLS policy fix for trade logging.
