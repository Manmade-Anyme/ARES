import os
import re

files_to_fix = [
    "tests/unit/test_alerts.py",
    "tests/unit/test_ml_collector.py",
    "tests/unit/test_ml_feature_fidelity.py",
    "tests/unit/test_position_manager.py",
    "tests/unit/test_task194_ml_labels_and_oi_distribution.py",
    "tests/unit/test_task195_orphan_and_sentinel_repair.py",
    "tests/unit/test_task199_audit_fixes.py"
]

for path in files_to_fix:
    if not os.path.exists(path):
        continue
    with open(path, 'r') as f:
        code = f.read()

    # Step 1: Make test functions async
    # Find all test methods calling pm.add_trade, collector.snapshot, or self.analytics.log_entry
    # We will use a regex to replace def test_ with async def test_ IF the body contains these calls.
    # Actually, let's just replace ALL `def test_` in these files with `async def test_`
    code = re.sub(r'\bdef (test_\w+)\(', r'async def \1(', code)

    # Step 2: add await to pm.add_trade, collector.snapshot, log_entry
    code = re.sub(r'(?<!await )(pm\.add_trade\()', r'await \1', code)
    code = re.sub(r'(?<!await )(collector\.snapshot\()', r'await \1', code)
    code = re.sub(r'(?<!await )(self\.analytics\.log_entry(_atomic)?\()', r'await self.analytics.log_entry_atomic(', code)
    code = re.sub(r'(?<!await )(self\.storage\.log_signal\()', r'await \1', code)

    # Clean up double async just in case
    code = code.replace("async async def", "async def")

    with open(path, 'w') as f:
        f.write(code)
