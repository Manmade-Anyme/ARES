# Debug Report — TASK-210 timestamp compatibility

Root cause category: TYPE / runtime compatibility. Root cause reproduced and
surgical fix verified in an isolated native Python 3.10.22 process.

The latest signal #6123 and preceding #0264 reached the invocation gate, then
failed parsing PostgreSQL timestamps with five/four fractional digits. Their
jobs recorded UNKNOWN / alert NONE; inference and prediction persistence never
ran. Examples: `2026-10-05T08:39:22.26328+00:00` and
`2026-10-05T08:27:46.4949+00:00`.

Replace two `datetime.fromisoformat` calls in `system_one/consumer.py` with
existing `dateutil.parser.isoparse`. Preserve database timestamps, RPC budget
accounting, monotonic dispatch deadline and all ownership/freshness gates.

Verification: public-path unittest regression with fake DB and SDK transports,
native Python 3.10.22 baseline 19 failed subcases, fixed zero failures/errors.
Tests cover 0–6 fractional digits, offsets, exact failed timestamps, microsecond
budgets, invalid timestamps and expired windows. The running worker and database
were unchanged; no real inference or webhook call was made. No debug logs remain.
Production repair awaits human merge and CI deployment. Old jobs are not replayed.
