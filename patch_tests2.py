import os
import re

for root, _, files in os.walk('tests/unit'):
    for file in files:
        if file.endswith('.py'):
            path = os.path.join(root, file)
            with open(path, 'r') as f:
                code = f.read()

            if "await collector.snapshot" in code or "await pm.add_trade" in code or "await self.analytics.log_entry" in code:
                # Replace await with asyncio.run(
                code = re.sub(r'await collector\.snapshot\((.*?)\)', r'__import__("asyncio").run(collector.snapshot(\1))', code, flags=re.DOTALL)
                code = re.sub(r'await pm\.add_trade\((.*?)\)', r'__import__("asyncio").run(pm.add_trade(\1))', code, flags=re.DOTALL)
                code = re.sub(r'await self\.analytics\.log_entry_atomic\((.*?)\)', r'__import__("asyncio").run(self.analytics.log_entry_atomic(\1))', code, flags=re.DOTALL)
                
                # We need to make sure we don't have async def where it shouldn't be
                # It's fine if the test is async def and we use asyncio.run inside? Actually no, asyncio.run inside async def throws an error.
                # Let's remove async def from all tests!
                code = code.replace("async def test_", "def test_")
                
                with open(path, 'w') as f:
                    f.write(code)
