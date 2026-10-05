# QA Report — TASK-210 timestamp compatibility

Verdict: PASS. No production deployment has been performed.

| Check | Result |
| --- | --- |
| Native Python 3.10.22 regression before fix | 19 failed subcases |
| Native Python 3.10.22 regression with fix | 3 methods, 35 subcases, no failures/errors |
| Local Jev suite, Python 3.14.6 | 232 tests and 35 subcases passed |
| Jev statement and branch coverage | 100% / 100% |
| Full local suite, Python 3.14.6 | 1088 tests and 45 subcases passed; 12 dependency warnings |
| Independent QA/code review | No blocking findings |

The native-runtime check loaded the candidate consumer into a separate diagnostic
Python process in memory, using fake Supabase RPC and TypeSafe SDK transports.
It neither modified the running worker nor called a real model or webhook.

The public processing regression covers exact observed four/five-digit fractions,
all PostgreSQL fractional precisions 0–6, timezone offsets, a 10-microsecond budget
difference, malformed timestamps and zero/negative dispatch windows. Existing
slow-RPC and SDK-startup gates also pass in the Jev suite.

The full-suite runner selected this repository's namespace tests package in
`sys.modules` to resolve a local installed-package collision with `tests.unit`.
No repository change was needed for that test environment issue. The full native
Python 3.10 suite remains the GitHub CI check; local full-suite results use 3.14.

Database freshness, invocation ownership, persisted timestamps and monotonic
budgets remain unchanged. Historical expired UNKNOWN jobs are not replayed.
Production rollout requires human review/merge and successful CI deployment.
