# ADR — TASK-212 Exhaustion Stop Configuration

Date: 2026-10-14
Decision: implement the user-approved minimal configuration change.

Add optional `min_rr_ratio` to the existing immutable `SetupLevels` value. `None` retains the existing global minimum. Configure exhaustion as `SetupLevels(30.0, 18.0, 40.0, min_rr_ratio=0.60)`. The existing engine gate uses that setup's override when present, otherwise its global threshold; suppression logs report the threshold actually used.

This keeps the stop, targets and required threshold together in the existing configuration table. Both startup profiles inherit the same table. Detection, target resolution, position management and scoring retain their current behavior. No standalone trial runner or additional configuration framework is introduced.

Files: config_profiles.py, engine.py, the three existing config/level/gate test files, and accompanying documentation. Acceptance tests exercise the real engine tick for bullish/bearish 30-point stops, the exact 18/30 boundary and a rejected 18/31 setup; other setups and unconfigured entries continue through the existing global gate. Existing open positions keep their stored stop values; new emitted signals use the new values after normal deployment.
