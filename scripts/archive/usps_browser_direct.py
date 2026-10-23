"""usps_browser_direct.py — direct-URL widget renderer with stealth (salvaged).

Warms the session on track.htm, then opens tools.usps.com/tracking/<id> directly
with automation-fingerprint hiding. This flow produced the first live status
("Shipping Label Created") during the USPS investigation.
"""
import time
from playwright.sync_api import sync_playwright

TID = "9400150206217911639042"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = { runtime: {} };
"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        captured = []
        try:
            ctx = browser.new_context(
                user_agent=UA, locale="en-US",
                viewport={"width": 1280, "height": 900},
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = ctx.new_page()
            page.add_init_script(STEALTH)

            def on_response(resp):
                url = resp.url.lower()
                if any(x in url for x in (".css", ".js", ".png", ".jpg", ".jpeg",
                                          ".woff", "fonts", ".svg", ".ico")):
                    return
                try:
                    body = resp.body()
                except Exception:
                    return
                txt = body.decode("utf-8", "ignore")
                if TID in txt:
                    captured.append((resp.url, txt))

            page.on("response", on_response)
            print("warming session…")
            page.goto("https://tools.usps.com/track.htm", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            print("goto direct tracking URL…")
            page.goto(f"https://tools.usps.com/tracking/{TID}",
                      wait_until="domcontentloaded", timeout=30000)

            body_text = ""
            found = False
            watch = ("delivered", "out for delivery", "label created", "pre-shipment",
                     "in transit", "accepted at", "expected delivery", "arrival",
                     "your item", "status")
            while time.time() - time.monotonic() * 0 < 40:
                page.wait_for_timeout(1500)
                try:
                    body_text = page.inner_text("body")
                except Exception:
                    continue
                if any(w in body_text.lower() for w in watch):
                    found = True
                    break
            print("result detected:", found, "| final URL:", page.url, "| captured:", len(captured))
            for url, txt in captured:
                print("CAP URL:", url[:150])
                print(txt[:300].replace("\n", " "))
                print("-----")
        finally:
            browser.close()


main()