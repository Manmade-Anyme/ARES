# OI Wall Audit — 2026-07-02

## Scope
Audited the two ARES alert payloads for signals #2599 and #0308 using the repository's OI wall detector implementation in [detectors/oi_wall.py](detectors/oi_wall.py) and the active tuning config in [config_profiles.py](config_profiles.py).

## Important note
Live Dhan fetches could not be used in this session because the configured credentials returned an authentication error. The audit therefore uses the alert payload values and the detector logic in the repo.

## Signal #2599
- Alert time: 09:17 IST
- Signal: OI_WALL_REJECTION / BEARISH / 24100 PE
- Alert values used: spot 24078.60, wall 24100 CE, wall OI 5.81M, change +10.6%, candle rejection condition implied by the alert

### Why the signal fired in code
The detector would trigger if all of the following were true:
1. A CE wall above spot exists with OI above the configured minimum (4.0M in NON_EXPIRY profile) and change above 5.0%.
2. The strike is within 80 points of spot.
3. The candle high reached at least 20 points below the wall strike (so the wall was tested).
4. The candle closed lower than it opened.
5. Option writers were still holding OI (ce_oi >= ce_oi_prev).

Those conditions are satisfied by the alert payload on paper.

### Why the trade likely failed
The detector is overly permissive. It uses a single candle and a shallow test of the wall, not a strong rejection or a confirmed breakdown. A trade can therefore be triggered even when the price only grazed the wall and then immediately moved through the stop.

## Signal #0308
- Alert time: 10:16 IST
- Signal: OI_WALL_REJECTION / BEARISH / 24100 PE
- Alert values used: spot 24085.15, wall 24100 CE, wall OI 9.56M, change +5.3%, candle rejection condition implied by the alert

### Why the signal fired in code
This also satisfies the detector's core conditions. The bigger wall size and higher OI make it look more convincing, but the detector still uses the same simplistic logic as #2599.

### Why the trade likely failed
The confidence logic in the repo would classify this as MEDIUM, not HIGH, because the wall size is only 1.5x the minimum threshold and the change rate does not exceed the 1.5x growth threshold. The alert's HIGH label is therefore not supported by the code's current confidence model.

## Root cause
The repo's OI wall logic is not robust enough for these trades because it:
- uses a single candle to confirm a rejection,
- treats a shallow touch of the wall as sufficient confirmation,
- does not require a strong wick rejection or a close beyond the strike,
- uses a wide stop buffer of 25 points, and
- does not incorporate the surrounding structure or follow-through after the first touch.

## Bottom line
These were not random false alarms in the sense that the detector would trigger on the provided inputs, but they were weak setups that were likely over-trusted. The core problem is the detector's decision rule, not just the specific market outcome on 2026-07-02.
