# ARES Project Todo & Roadmap

## Active Tasks

### 🔒 Dhan Token Automation (Programmatic Refresh) [COMPLETED]
**Description:** Migrated to centralized `dhanrenew` microservice running on Fly.io which auto-renews tokens into Supabase. ARES fetches client ID and access token from Supabase dynamically on startup and auto-recovers mid-session.

**Steps Completed:**
- [x] Integrate Supabase dynamic credentials fetcher on startup.
- [x] Implement dynamic mid-session credentials reloading on auth failure.
- [x] Remove Dhan client ID and access token from local `.env` and deployment secrets.

### 📐 Sizing Safety Gate Styling Enhancement [TODO]
**Description:** Enhance `alerts.py` to clearly label trades as "Paper Sizing Only" when capital limits calculate suggested lots to 0, ensuring alerts remain visible and active.
- [ ] Add a "Paper Sizing Only" visual indicator to Discord embed alerts.
- [ ] Highlight paper-sizing signals differently in the local console UI.

---

## Completed Tasks
- [x] Dhan Token Automation (Programmatic Refresh)
- [x] Dynamic PDH/PDL fetching from Dhan API.
- [x] Target sorting logic based on proximity and momentum.
- [x] Supabase RLS policy fix for trade logging.
