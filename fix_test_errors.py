import os
import re

for root, _, files in os.walk('tests/unit'):
    for file in files:
        if file.endswith('.py'):
            path = os.path.join(root, file)
            with open(path, 'r') as f:
                code = f.read()

            code = code.replace('async async def', 'async def')
            
            # Find any def that contains await inside it but isn't async
            # A simple heuristic: if a class contains 'await', its methods might be missing async
            # Better: just use a regex to replace 'def ' with 'async def ' for methods containing await
            
            # Actually, I'll just change any remaining 'def test_' containing 'await '
            def replacer(match):
                body = match.group(0)
                if 'await ' in body and not body.startswith('async '):
                    return 'async ' + body
                return body
                
            code = re.sub(r'def test_\w+\(.*?\):(?:\n\s+.*)*', replacer, code)
            
            with open(path, 'w') as f:
                f.write(code)
