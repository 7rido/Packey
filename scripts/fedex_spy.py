"""fedex_spy.py — replay Packey's FedEx browser session several ways and report
which one makes FedEx's own app call the tracking API.

Each scenario gets a FRESH profile copy and the same launch options, UA and
stealth Packey uses, then records every api.fedex.com call the page makes.

Usage:
  .venv/bin/python scripts/fedex_spy.py                    # fake number
  .venv/bin/python scripts/fedex_spy.py --number 3842...   # a real number
  .venv/bin/python scripts/fedex_spy.py --only AE          # just scenarios A and E
  .venv/bin/python scripts/fedex_spy.py --cookies ~/glm-tracker/.fedex_cookies.txt
      # also runs F/G: cookies from your real Chrome injected into the session.
      # File = the output of document.cookie on fedex.com ("k=v; k2=v2").

Nothing here is imported by the bot. Tracking numbers are masked in output.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usps_browser as ub  # noqa: E402

ub._PROFILE_COPY = os.path.expanduser("~/Library/Caches/fedex_spy_profile")

WTRK_ROOT = "https://www.fedex.com/wtrk/track/"
FTRK_ROOT = "https://www.fedex.com/fedextrack/"


def track_url(tid):
    return f"https://www.fedex.com/fedextrack/?trknbr={tid}"


# (name, warmup url or None, clear cookies before starting?)
SCENARIOS = [
    ("A current Packey: warmup wtrk -> fedextrack", WTRK_ROOT, False),
    ("B no warmup -> fedextrack", None, False),
    ("C warmup fedextrack root -> fedextrack", FTRK_ROOT, False),
    ("D cookies cleared, warmup wtrk -> fedextrack", WTRK_ROOT, True),
    ("E cookies cleared, no warmup -> fedextrack", None, True),
]
RESET_SCENARIOS = [
    ("H warmup wtrk -> RESET storage -> fedextrack", WTRK_ROOT, "reset"),
    ("I fedextrack, then RESET -> fedextrack again (a retry)", FTRK_ROOT + "?trknbr=", "reset"),
]
COOKIE_SCENARIOS = [
    ("F real-Chrome cookies injected, no warmup -> fedextrack", None, "inject"),
    ("G real-Chrome cookies injected, warmup wtrk -> fedextrack", WTRK_ROOT, "inject"),
]


def load_cookies(path):
    """Parse a document.cookie dump into Playwright cookie dicts for .fedex.com.
    Values are never printed — only names."""
    raw = open(os.path.expanduser(path)).read().strip()
    out = []
    for part in raw.split(";"):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        if k:
            out.append({"name": k, "value": v, "domain": ".fedex.com", "path": "/",
                        "secure": True, "sameSite": "Lax"})
    return out


def run(pw, name, warmup, clear, tid, wait_s, cookies=None):
    ub.cleanup_profile_copy()
    profile = ub._ensure_profile_copy()
    if not profile:
        print(f"\n[{name}] no profile copy available — skipped")
        return None
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=profile,
        headless=True,
        executable_path=ub.CHROME_PATH,
        user_agent=ub.current_ua(),
        viewport={"width": 1366, "height": 870},
        device_scale_factor=2,
        args=["--disable-blink-features=AutomationControlled",
              "--disable-extensions", "--window-position=60,60"],
    )
    calls = []
    mark = 0
    try:
        if clear == "reset":
            pass   # applied after the warm-up load, below
        elif clear == "inject":
            ctx.clear_cookies()
            ctx.add_cookies(cookies or [])
        elif clear:
            ctx.clear_cookies()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.add_init_script(ub._STEALTH)

        def on_resp(r):
            if "api.fedex.com" in r.url:
                calls.append((r.request.method, r.status,
                              r.url.split("?")[0].replace("https://api.fedex.com", "")))
        page.on("response", on_resp)

        if warmup:
            try:
                url = warmup + tid if warmup.endswith("trknbr=") else warmup
                page.goto(url, wait_until="domcontentloaded", timeout=40000)
                if warmup.endswith("trknbr="):
                    page.wait_for_timeout(8000)   # let the first boot finish
                else:
                    ub._human_pause(page)
            except Exception as e:
                print(f"  warmup error: {type(e).__name__}")
        if clear == "reset":
            ub._reset_origin_state(page, ("https://www.fedex.com",
                                          "https://api.fedex.com"))
        mark = len(calls)
        try:
            page.goto(track_url(tid), wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            print(f"  track page error: {type(e).__name__}")
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if any("/track/v2/shipments" in c[2] for c in calls[mark:]):
                break
            page.wait_for_timeout(500)
        time.sleep(1)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    # Only the tracking load counts — scenario I's first load fires on its own.
    fired = [c for c in calls[mark:] if "/track/v2/shipments" in c[2]]
    ok = any(c[1] == 200 for c in fired)
    print(f"\n[{name}]")
    print(f"  tracking API: {'FIRED ' + str(fired[-1][1]) if fired else 'NEVER CALLED'}")
    for i, (m, st, u) in enumerate(calls):
        print(f"    {'·' if i < mark else ' '} {m:4} {st}  {u}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--number", default="999999999999")
    ap.add_argument("--wait", type=float, default=25.0,
                    help="seconds to wait for the tracking API (Packey uses 25)")
    ap.add_argument("--only", default="", help="scenario letters, e.g. AB")
    ap.add_argument("--cookies", default="", help="document.cookie dump from real Chrome")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright
    print(f"fedex spy — number {ub._mask(args.number)}, UA {ub.current_ua().split(') ')[-1]}")
    cookies = load_cookies(args.cookies) if args.cookies else None
    if cookies:
        print(f"injecting {len(cookies)} cookie(s): "
              + ", ".join(c["name"] for c in cookies))
    scenarios = SCENARIOS + RESET_SCENARIOS + (COOKIE_SCENARIOS if cookies else [])
    results = {}
    with sync_playwright() as pw:
        for name, warmup, clear in scenarios:
            if args.only and name[0] not in args.only.upper():
                continue
            results[name] = run(pw, name, warmup, clear, args.number, args.wait,
                                cookies)
    ub.cleanup_profile_copy()
    print("\nSUMMARY")
    for name, ok in results.items():
        print(f"  {'WORKS ' if ok else 'fails '} {name}")


if __name__ == "__main__":
    main()
