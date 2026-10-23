"""smoke_bundled.py — verify Playwright's bundled Chromium launches."""
from playwright.sync_api import sync_playwright

UA_OVERRIDE = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)  # bundled Chromium, no executable_path
    ctx = browser.new_context(user_agent=UA_OVERRIDE, locale="en-US",
                              viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto("https://example.com", wait_until="domcontentloaded", timeout=20000)
    print("bundled chromium OK; title:", page.title())
    print("UA reported by page:", page.evaluate("navigator.userAgent")[:90])
    browser.close()
print("done")