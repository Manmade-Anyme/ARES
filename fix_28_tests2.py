import re
import os

path = "tests/unit/test_task194_ml_labels_and_oi_distribution.py"
with open(path, 'r') as f:
    code = f.read()

code = code.replace("def _snapshot_record", "async def _snapshot_record")
code = re.sub(r'(?<!await )(_snapshot_record\()', r'await \1', code)

with open(path, 'w') as f:
    f.write(code)

path = "tests/unit/test_task195_orphan_and_sentinel_repair.py"
with open(path, 'r') as f:
    code = f.read()

code = code.replace("def _snapshot_record", "async def _snapshot_record")
code = re.sub(r'(?<!await )(_snapshot_record\()', r'await \1', code)

with open(path, 'w') as f:
    f.write(code)

path = "tests/unit/test_ml_feature_fidelity.py"
with open(path, 'r') as f:
    code = f.read()

code = code.replace("def _snapshot_record(self", "async def _snapshot_record(self")
code = re.sub(r'(?<!await )(self\._snapshot_record\()', r'await \1', code)

with open(path, 'w') as f:
    f.write(code)
