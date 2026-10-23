import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import os
import usps_browser as ub

# Isolate the probe's profile copy from the live bot's: the bot may be
# mid-lookup on the shared copy path, so this debug run works on its own dir
# (removed again by cleanup_profile_copy() after each probe below).
ub._PROFILE_COPY = os.path.expanduser(
    "~/Library/Caches/usps_tracker_profile_dbg")

# Pass one or more tracking numbers on the command line to debug a specific
# report; falls back to these two known-good sample numbers when run bare.
TIDS = sys.argv[1:] or ["9235990424545700014997", "9400150206217911639042"]


def probe(tid):
    copy = ub._ensure_profile_copy()
    print(f"\n=== {tid} ===")
    from playwright.sync_api import sync_playwright
    body = ""
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=copy, headless=True, executable_path=ub.CHROME_PATH,
            user_agent=ub.UA, locale="en-US", viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--disable-extensions"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.add_init_script(ub._STEALTH)
            page.goto(f"https://tools.usps.com/tracking/{tid}",
                      wait_until="domcontentloaded", timeout=40000)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                time.sleep(1.2)
                try:
                    body = page.inner_text("body")
                except Exception:
                    continue
                if "Latest Update" in body or "Tracking Number:" in body:
                    break
        finally:
            ctx.close()
    low = body.lower()
    print("url:", page.url)
    print("len:", len(body), "| LatestUpdate:", "Latest Update" in body,
          "| TrackingNumber:", "Tracking Number:" in body)
    for anchor in ("out for delivery", "in transit", "delivered",
                   "shipping label created", "awaiting item", "on the way",
                   "expected delivery", "arriving", "tracking number",
                   "notice left", "pre-shipment", "pre shipment",
                   "not yet scanned", "available for pickup"):
        if anchor in low:
            print(f"  anchor '{anchor}' at", low.find(anchor))
    if "Latest Update" in body or "Tracking Number:" in body:
        zone = ub._result_zone(body)
        print("STATUS:", ub._classify(zone, no_latest_update="Latest Update" not in body))
        print("DETAIL:", " ".join(zone.split())[:180])
    else:
        print("no markers. head:", " ".join(body.split())[:220])


for t in TIDS:
    probe(t)
    ub.cleanup_profile_copy()