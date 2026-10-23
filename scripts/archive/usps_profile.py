"""usps_profile.py — real-Chrome-profile breakthrough probe (salvaged).

Copies the user's real Chrome profile (Akamai-trusted cookies) and drives the
tracking widget with it. HEADLESS in this version — the shipping version uses
a visible window (headful) which USPS requires.
"""
import os
import shutil
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import usps_browser as ub

TID = "9400150206217911639042"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SRC = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
DST = "/tmp/chrome_profile_copy"

IGNORE = {"Cache", "Code Cache", "GPUCache", "Cached Data", "Cached Data_1",
          "Web Data", "History", "Download Metadata", "Session Storage", "Preferences"}


def _ignore(dir_, names):
    return {n for n in names if n in IGNORE or n.startswith(".")}


print("copying real Chrome profile (caches excluded) …")
if os.path.exists(DST):
    shutil.rmtree(DST)
shutil.copytree(SRC, DST, ignore=_ignore)
print("profile copied to", DST)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=DST,
        headless=True,
        executable_path=CHROME,
        user_agent=ub.UA,
        locale="en-US",
        viewport={"width": 1366, "height": 900},
        args=["--disable-blink-features=AutomationControlled", "--disable-extensions"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.add_init_script(ub._STEALTH)

    print("1) goto the direct tracking URL …")
    page.goto(f"https://tools.usps.com/tracking/{TID}",
              wait_until="domcontentloaded", timeout=45000)
    print("   url:", page.url)
    print("   cookies set:", len(ctx.cookies()))

    body = ""
    watch = ("Latest Update", "Tracking Number:")
    for _ in range(25):
        time.sleep(1.6)
        try:
            body = page.inner_text("body")
        except Exception as e:
            print("   poll err:", e)
            continue
        print(f"   poll: url={page.url[:66]} len={len(body)}")
        if any(w in body for w in watch):
            break

    if any(w in body for w in watch):
        zone = ub._result_zone(body)
        print("\n★★★ STATUS:", ub._classify(zone))
        print("DETAIL:", " ".join(zone.split())[:260])
    else:
        print("\nNO RESULT. body head:", " ".join(body.split())[:220])
    ctx.close()
print("done")