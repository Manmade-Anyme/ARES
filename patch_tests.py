import os
import re

for root, _, files in os.walk('tests/unit'):
    for file in files:
        if file.endswith('.py'):
            path = os.path.join(root, file)
            with open(path, 'r') as f:
                code = f.read()

            # We need to make tests async if they call pm.add_trade or collector.snapshot
            if "pm.add_trade" in code or "collector.snapshot" in code or "self.analytics.log_entry" in code:
                # Find all def test_ that contain the calls inside their block
                # A simple regex to replace def test_ with async def test_
                code = re.sub(r'def (test_\w+)\(', r'async def \1(', code)
                
                # Replace the calls
                code = re.sub(r'(?<!await )pm\.add_trade\(', r'await pm.add_trade(', code)
                code = re.sub(r'(?<!await )collector\.snapshot\(', r'await collector.snapshot(', code)
                code = re.sub(r'(?<!await )self\.analytics\.log_entry\(', r'await self.analytics.log_entry_atomic(', code)
                
                with open(path, 'w') as f:
                    f.write(code)
