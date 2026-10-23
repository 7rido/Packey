"""usps_human.py — human-paced USPS attempt (slow typing, mouse movement)."""
import random
import time
from playwright.sync_api import sync_playwright

import usps_browser as ub

TID = "9400150206217911639042"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

rng = random.Random()


def human_pause(lo=2.5, hi=6.5):
    time.sleep(rng.uniform(lo, hi))


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(user_agent=UA, locale="en-US",
                              viewport={"width": 1366, "height": 900})
    page = ctx.new_page()
    page.add_init_script(ub._STEALTH)

    print("1) load track.htm …")
    page.goto("https://tools.usps.com/track.htm", wait_until="domcontentloaded", timeout=40000)
    human_pause(4, 8)
    print("   url:", page.url)
    if "error_404" in page.url:
        print("THROTTLED at warm-up (404)")

    try:
        page.mouse.move(400, 300)
        human_pause(0.5, 1.2)
        page.mouse.wheel(0, 260)
        human_pause(0.5, 1.5)
    except Exception:
        pass

    inp = page.locator("input[placeholder*='Tracking']").first
    inp.click()
    human_pause(0.4, 1.2)
    for ch in TID:
        inp.type(ch, delay=rng.uniform(40, 120) / 1000)
    human_pause(0.8, 2.0)
    page.keyboard.press("Enter")
    print("2) submitted …")

    body_text = ""
    watch = ("Latest Update", "Tracking Number:")
    for _ in range(24):
        human_pause(2.2, 3.4)
        try:
            body_text = page.inner_text("body")
        except Exception:
            continue
        print(f"   poll: url={page.url[:70]} len={len(body_text)}")
        if any(w in body_text for w in watch):
            break

    print("final url:", page.url)
    if any(w in body_text for w in watch):
        zone = ub._result_zone(body_text)
        print("STATUS:", ub._classify(zone))
        print("DETAIL:", " ".join(zone.split())[:260])
    else:
        print("NO RESULT. body head:", " ".join(body_text.split())[:200])
    browser.close()
print("done")