"""usps_bundled_test.py — one USPS lookup with bundled Chromium (fresh fingerprint)."""
import time
from playwright.sync_api import sync_playwright

import usps_browser as ub

TID = "9400150206217911639042"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)  # bundled Chromium (fresh fingerprint)
    try:
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.add_init_script(ub._STEALTH)
        page.goto("https://tools.usps.com/track.htm", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        print("warm url:", page.url)
        if "error_404" in page.url:
            print("STILL THROTTLED (track.htm -> 404)")
        else:
            try:
                inp = page.locator("input[placeholder*='Tracking']").first
                inp.fill(TID)
                inp.press("Enter")
            except Exception as e:
                print("submit err:", e)
            body_text = ""
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                page.wait_for_timeout(1200)
                try:
                    body_text = page.inner_text("body")
                except Exception:
                    continue
                if "Latest Update" in body_text or "Tracking Number:" in body_text:
                    break
            print("page url:", page.url)
            if "Latest Update" in body_text or "Tracking Number:" in body_text:
                zone = ub._result_zone(body_text)
                print("STATUS:", ub._classify(zone))
                print("DETAIL:", " ".join(zone.split())[:250])
            else:
                print("no result marker; body head:", " ".join(body_text.split())[:160])
    finally:
        browser.close()
print("done")