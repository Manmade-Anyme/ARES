# QA Report — TASK-20261102 Future Tick Watermark

**Date:** 2026-11-03
**Result:** PASS

## Verification

Focused command:

```bash
python -m pytest tests/unit/test_tick_feed.py tests/unit/test_position_manager.py tests/unit/test_reference_lifecycle_accounting_new.py tests/unit/test_option_source_validation.py -q --tb=short
```

Result: **190 passed in 1.73s**.

Mocked session/manual command:

```bash
python -m pytest tests/unit/test_manual_signal_tracking.py tests/unit/test_session_close_drain.py -q --tb=short
```

Result: **50 passed in 1.41s**.

Full repository command:

```bash
python -m pytest tests/ -q
```

Result: **2,133 passed, 12 warnings, 45 subtests in 70.83s**.

Configured coverage gate:

```bash
python -m pytest tests/unit/test_task210*.py tests/unit/test_task211*.py -q --cov=system_one --cov-branch --cov-report=term-missing --cov-fail-under=100
```

Result: **398 passed, 35 subtests in 11.55s; 100% `system_one` line and branch coverage**.

Coverage includes rejecting an LTT more than five seconds ahead of receipt, retaining the tick with receipt-time provenance, recovering a future ATR watermark from its trusted T1 anchor, routing receipt-only ticks in mixed batches, and applying receipt-only ticks during candle-fetch protection. Existing tick/ATR lifecycle cases use deterministic 2099 test time.

## Limits

No live Dhan, Fly, or Supabase runtime was exercised. Test and coverage results do not verify the deployed worker or market-data behavior.
