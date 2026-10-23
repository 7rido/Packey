"""usps_browser_explore.py — headless Chrome form-submit explorer (salvaged).

Fills the USPS tracking input in headless Chrome and drives the natural
form-submit flow (the route that first surfaced tools.usps.com/tracking/<id>).
"""
import time
from playwright.sync_api import sync_playwright

TID = "9400150206217911639042"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

results = []


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

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
                results.append((resp.url, txt))

        page.on("response", on_response)
        print("goto track.htm …")
        page.goto("https://tools.usps.com/track.htm", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        inputs = page.locator("input").all()
        print("\ninputs found:")
        for i in inputs:
            ph = i.get_attribute("placeholder")
            if ph:
                print("  placeholder=%r name=%r" % (ph, i.get_attribute("name")))

        try:
            inp = page.locator("input[placeholder*='Tracking Number'], input[placeholder*='tracking']").first
            print("\nfilling:", inp.get_attribute("placeholder"))
            inp.fill(TID)
            inp.press("Enter")
        except Exception as e:
            print("\nfill/enter failed:", e)
            browser.close()
            return

        deadline = time.time() + 30
        found = False
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            low = page.content().lower()
            if any(w in low for w in ("delivered", "out for delivery", "label created",
                                      "pre-shipment", "in transit", "accepted at",
                                      "expected", "arrival")):
                found = True
                break
        print("\nresult text detected:", found, "| captured responses:", len(results))
        page.screenshot(path="/tmp/usps_result.png", full_page=False)

        for url, txt in results:
            print("\nCAPTURED:", url[:170])
            print(txt[:350].replace("\n", " ")[:350])
            print("-----")
        try:
            sel = page.locator("section.track--results, .track--results, [class*='results']").first
            print("\nRESULTS TEXT SNIPPET:", sel.inner_text(timeout=5000)[:400].replace("\n", " | "))
        except Exception:
            pass
        browser.close()


main()