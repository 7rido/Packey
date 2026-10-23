"""probe_unorthodox.py — cookie-session, unlisted endpoints, headless dump-dom."""
import asyncio
import os
import subprocess
import tempfile
import httpx

TID = "9400150206217911639042"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def sniff(text: str, label: str):
    low = text.lower()
    hits = [w for w in ("delivered", "label created", "pre-shipment", "in transit",
                        "departed", "arrived at", "out for delivery", "accepted at",
                        "awaiting item", "access denied", "captcha") if w in low]
    print(f"{label}: len={len(text)} status-words={hits[:10]}")


async def httpx_probe():
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                 headers={"User-Agent": UA}) as c:
        print("=== A) cookie+referer session against USPS ===")
        await c.get("https://tools.usps.com/track.htm")
        r = await c.get(f"https://tools.usps.com/go/TrackConfirmAction?tLabels={TID}",
                        headers={"Referer": "https://tools.usps.com/track.htm"})
        sniff(r.text, f"TrackConfirmAction HTTP {r.status_code}")

        print("\n=== B) unlisted/internal endpoints ===")
        cands = [
            f"https://tracks.usps.com/track?numbers={TID}",
            f"https://tracks.usps.com/rest2/current/{TID}",
            f"https://tracks.usps.com/api/track?trackNumbers={TID}",
            f"https://track.aftership.com/{TID}",
            f"https://track.aftership.com/track/{TID}",
            f"https://t.17track.net/en/search?nums={TID}",
        ]
        for url in cands:
            try:
                rr = await c.get(url)
                sniff(rr.text[:4000], f"{url.split('//')[1][:40]} HTTP {rr.status_code}")
            except Exception as e:
                print(f"{url.split('//')[1][:40]} ERROR {type(e).__name__}")
            await asyncio.sleep(0.4)


def chrome_probe():
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not os.path.exists(chrome):
        print("\n=== C) headless Chrome: NOT FOUND ===")
        return
    prof = tempfile.mkdtemp(prefix="chrome-prof-")
    print("\n=== C) headless Chrome --dump-dom ===")
    urls = {
        "usps-track.htm": f"https://tools.usps.com/track.htm?tLabels={TID}",
        "aftership-public": f"https://www.aftership.com/track/{TID}",
    }
    for label, url in urls.items():
        try:
            out = subprocess.run(
                [chrome, "--headless", "--disable-gpu", "--no-first-run",
                 "--user-data-dir", prof, "--dump-dom", url],
                capture_output=True, text=True, timeout=45, cwd=tempfile.gettempdir(),
            )
            sniff(out.stdout, f"{label} (dom len={len(out.stdout)})")
        except subprocess.TimeoutExpired:
            print(f"{label}: TIMEOUT")
        except Exception as e:
            print(f"{label}: ERROR {e}")


async def main():
    await httpx_probe()
    chrome_probe()


asyncio.run(main())