import asyncio
import httpx
async def test():
    try:
        async with httpx.AsyncClient() as c:
            await c.get('https://discord.nonexistent.com')
    except Exception as e:
        print(f'Exception: >{e}<')
asyncio.run(test())
