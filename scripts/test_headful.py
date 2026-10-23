"""test_headful.py — the breakthrough probe: VISIBLE Chrome window + trusted
profile copy renders the USPS widget (Akamai serves headless empty shells)."""
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import usps_browser as ub

TID = "9235990424545700014997"

# Fresh copy first (freshest cookies)
import os
import shutil
shutil.rmtree(ub._PROFILE_COPY, ignore_errors=True)
copy = ub._ensure_profile_copy()
print("profile copy:", copy, "| cookie age min:", round((ub._usps_cookie_age() or 0) / 60, 1))

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    # HEADFUL: a real, visible Chrome window from the trusted profile.
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=copy,
        headless=False,                 # <-- real window on screen
        executable_path=ub.CHROME_PATH,
        viewport={"width": 1366, "height": 900},
        args=["--disable-blink-features=AutomationControlled",
              "--disable-extensions", "--window-position=40,40"],
    )
    body = ""
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://tools.usps.com/track.htm",
                  wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        page.goto(f"https://tools.usps.com/tracking/{TID}",
                  wait_until="domcontentloaded", timeout=45000)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            time.sleep(1.5)
            try:
                body = page.inner_text("body")
            except Exception:
                continue
            if "Latest Update" in body or "Tracking Number:" in body:
                break
        page.screenshot(path="/tmp/headful_result.png")
        print("url:", page.url)
        print("len:", len(body), "| markers:",
              "Latest Update" in body, "Tracking Number:" in body)
        if "Latest Update" in body or "Tracking Number:" in body:
            zone = ub._result_zone(body)
            print("STATUS:", ub._classify(zone))
            print("DETAIL:", " ".join(zone.split())[:200])
        else:
            print("no markers; head:", " ".join(body.split())[:220])
    finally:
        ctx.close()
print("done")