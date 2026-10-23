"""probe_trackers.py — probe credential-free USPS status sources (salvaged)."""
import asyncio
import httpx

TID = "9400150206217911639042"

ENDPOINTS = {
    "usps-links-tools": f"https://tools.usps.com/go/TrackConfirmAction?tLabels={TID}",
    "usps-track-htm": f"https://tools.usps.com/track.htm?tLabels={TID}",
    "usps-spa": f"https://www.usps.com/tracking?tracking-number={TID}",
    "aftership-public": f"https://www.aftership.com/track/{TID}",
    "track-trace-usps": f"https://www.track-trace.com/usps/index.php?numbers={TID}",
    "parcelsapp": f"https://parcelsapp.com/en/track/{TID}",
    "bing-search": f"https://www.bing.com/search?q={TID}",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


async def probe():
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                 headers={"User-Agent": UA}) as c:
        for name, url in ENDPOINTS.items():
            try:
                r = await c.get(url)
                body = r.text.lower()
                hints = [w for w in ("delivered", "label created", "in transit",
                                     "departed", "arrived", "out for delivery",
                                     "accepted at", "cloudflare", "captcha",
                                     "access denied", "just a moment", "track") if w in body]
                print(f"{name:18s} HTTP {r.status_code:3d} len={len(r.text):6d} "
                      f"type={r.headers.get('content-type','')[:30]}")
                print(f"    hints/status-words: {hints[:8]}")
            except Exception as e:
                print(f"{name:18s} ERROR: {type(e).__name__}: {str(e)[:90]}")
            await asyncio.sleep(0.6)


asyncio.run(probe())