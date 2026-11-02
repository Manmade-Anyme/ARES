# QA Report — TASK-20261102 Future Tick Watermark

**Date:** 2026-11-02  
**Result:** PASS

## Verification

Focused command:

```bash
python -m pytest tests/unit/test_tick_feed.py tests/unit/test_position_manager.py tests/unit/test_reference_lifecycle_accounting_new.py -q
```

Result: **158 passed in 4.79s**.

Full repository command:

```bash
python -m pytest tests/ -q
```

Result: **2,129 passed, 12 warnings, 45 subtests**.

Configured coverage gate:

```bash
python -m pytest tests/unit/test_task210*.py tests/unit/test_task211*.py -q --cov=system_one --cov-branch --cov-report=term-missing --cov-fail-under=100
```

Result: **398 passed, 35 subtests; 100% `system_one` line and branch coverage**.

Coverage includes rejecting an LTT more than five seconds ahead of receipt, retaining the tick with receipt-time provenance, recovering a future ATR watermark from its trusted T1 anchor, routing receipt-only ticks in mixed batches, and applying receipt-only ticks during candle-fetch protection. Existing tick/ATR lifecycle cases use deterministic 2099 test time.

## Limits

No live Dhan, Fly, or Supabase runtime was exercised. Test and coverage results do not verify the deployed worker or market-data behavior.
