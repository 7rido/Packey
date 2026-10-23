"""probe_17track.py — live 17TRACK register + lookup probe (salvaged)."""
import asyncio
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import main


async def probe():
    tids = ["9400150206217911639042", "9300111043900023959782", "9300111043900026403077"]
    for t in tids:
        main._register_blocked_until.pop(t, None)
        r = await main.register_17track(t)
        print(t, "-> register:", r)
        if r in ("registered", "already"):
            print("   status:", await main._check_17track(t))


asyncio.run(probe())