"""Smoke test for the Mini App backend — runs the aiohttp app in isolation
(no Telegram connection), signs initData the way Telegram does, and exercises
every endpoint. Run:  python3 scripts/smoke_miniapp.py
"""
import asyncio
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from aiohttp import web  # noqa: E402

import main  # noqa: E402

# Isolation: point persistence at a throwaway directory so the smoke test can
# NEVER overwrite the bot's real track_data.json / register_cooldown.json —
# the mute/remove/refresh endpoints it exercises call save_data().
import tempfile
_tmp = tempfile.mkdtemp(prefix="glm_smoke_")
main.DATA_FILE = Path(_tmp) / "smoke_track_data.json"
main.REG_COOLDOWN_FILE = Path(_tmp) / "smoke_register_cooldown.json"

TOKEN = "123456:SMOKE-TEST"
PORT = 8899

main._bot_token = TOKEN
main.track_sessions.clear()
main.track_sessions[42] = {
    "9400111899560000000000": {
        "name": "Smoke Test Box", "status": "In Transit", "carrier": "usps",
        "carrier_name": "USPS", "carrier_url": "https://tools.usps.com/x",
        "source": "17TRACK", "detail": "Arrived at facility.",
        "last_checked": main.now_iso(), "muted": False,
    }
}

user = {"id": 42, "first_name": "Fred"}
params = {"user": json.dumps(user, separators=(",", ":")),
          "auth_date": str(int(time.time()))}
dcs = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
digest = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
init_data = "&".join(f"{k}={quote(v)}" for k, v in params.items()) + f"&hash={digest}"
AUTH = {"Authorization": "tma " + init_data}


async def smoke() -> None:
    runner = web.AppRunner(main._build_miniapp_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}") as c:
        r = await c.get("/")
        print(f"GET  /                        -> {r.status_code} (index.html, {len(r.text)} bytes)")
        assert r.status_code == 200 and "Package Tracker" in r.text

        r = await c.get("/api/packages")
        print(f"GET  /api/packages (no auth)  -> {r.status_code} {r.json()}")
        assert r.status_code == 401

        r = await c.get("/api/packages", headers=AUTH)
        pkgs = r.json()["packages"]
        print(f"GET  /api/packages (auth)     -> {r.status_code} {pkgs[0]['name']} / {pkgs[0]['status']}")
        assert r.status_code == 200 and pkgs[0]["name"] == "Smoke Test Box"

        r = await c.post("/api/rename", headers=AUTH,
                         json={"tracking_id": "9400111899560000000000", "name": "Renamed Box"})
        print(f"POST /api/rename              -> {r.status_code} {r.json()['package']['name']}")
        assert r.status_code == 200 and r.json()["package"]["name"] == "Renamed Box"

        r = await c.post("/api/rename", headers=AUTH,
                         json={"tracking_id": "9400111899560000000000", "name": "  "})
        print(f"POST /api/rename (blank)      -> {r.status_code}")
        assert r.status_code == 400

        r = await c.post("/api/add", headers=AUTH, json={"tracking_id": "hello world"})
        print(f"POST /api/add (garbage id)    -> {r.status_code} {r.json()}")
        assert r.status_code == 400

        r = await c.post("/api/add", headers=AUTH, json={"tracking_id": "9400111899560000000000"})
        print(f"POST /api/add (duplicate)     -> {r.status_code} {r.json()}")
        assert r.status_code == 409

        r = await c.post("/api/mute", headers=AUTH,
                         json={"tracking_id": "9400111899560000000000"})
        print(f"POST /api/mute                -> {r.status_code} muted={r.json()['package']['muted']}")
        assert r.json()["package"]["muted"] is True

        r = await c.post("/api/refresh", headers=AUTH, json={"all": True})
        print(f"POST /api/refresh (removed)   -> {r.status_code}")
        assert r.status_code == 404

        r = await c.post("/api/remove", headers=AUTH,
                         json={"tracking_id": "9400111899560000000000"})
        print(f"POST /api/remove              -> {r.status_code} {r.json()}")
        assert r.status_code == 200

        r = await c.get("/api/packages", headers=AUTH)
        print(f"GET  after remove             -> {r.status_code} packages={r.json()['packages']}")
        assert r.json()["packages"] == []
    await runner.cleanup()
    main.track_sessions.clear()
    print("SMOKE TEST OK")


if __name__ == "__main__":
    asyncio.run(smoke())