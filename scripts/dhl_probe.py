"""One-shot DHL edge probe — WHY does the dhl browser slot keep accruing strikes?

Isolated from the live bot (same pattern as dbg_tid.py):
  * profile copy redirected to the dbg dir (own Chrome singleton + cookie DB),
  * the throttle store redirected to /tmp (arming here never touches the live
    browser_throttle.json), in-memory throttle state cleared so the tier path
    actually runs,
  * the tier's own WARNING lines are captured in-process — the arm reason
    ("edge refused the page load (403)" vs "the edge answered a block page")
    is the diagnostic answer.

Stage A reproduces the exact _reach()/_poll_page() flow by hand and reports
the warmup HTTP status, body size, _looks_blocked verdict and which marker
fired. Stage B runs the real get_browser_status("dhl", tid) and reports the
outcome taxonomy + strike delta.
"""
import logging
import os
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import usps_browser as ub

ub._PROFILE_COPY = os.path.expanduser(
    "~/Library/Caches/usps_tracker_profile_dbg")
ub._THROTTLE_STORE = "/tmp/dhl_probe_throttle.json"
ub._throttled_until.clear()
ub._throttle_strikes.clear()

_args = [a for a in sys.argv[1:] if a != "--headful"]
TID = _args[0] if _args else "1234567890"
HEADFUL = "--headful" in sys.argv   # opens a visible Chrome window

records = []


class _Cap(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())


cap = _Cap(level=logging.WARNING)
ub.LOGGER.addHandler(cap)

from playwright.sync_api import sync_playwright


def summarize(body):
    low = body.lower()
    hits = [m for m in ub._BLOCK_MARKERS if m in low]
    return len(body), hits


def frame_scan(pg):
    """inner_text('body') never crosses iframe boundaries — if DHL renders
    results in an iframe, the tier's parser is blind to it by construction."""
    for fr in pg.frames:
        try:
            txt = fr.inner_text("body")
        except Exception as e:
            print(f"    frame [{fr.url[:90]}] unreadable ({type(e).__name__})")
            continue
        mark = " TrackingResults=True" if "Tracking Results" in txt else ""
        print(f"    frame [{fr.url[:90]}] {len(txt)} chars{mark}")


api_hits = []


def _watch(pg):
    def _on_resp(r):
        u = r.url
        if "dhl.com" in u and any(
                k in u for k in ("track", "shipment", "api", "gsta")):
            api_hits.append((r.status, u[:110]))
    pg.on("response", _on_resp)


import atexit
atexit.register(ub.cleanup_profile_copy)   # even on a crash, drop the copy

cfg = ub._CARRIER_PAGES["dhl"]
copy = ub._ensure_profile_copy()
print(f"=== stage A: raw warmup + submit (tid {TID}, headful={HEADFUL}) ===")
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=copy, headless=not HEADFUL,
        executable_path=ub.CHROME_PATH,
        user_agent=ub.UA, locale="en-US",
        viewport={"width": 1366, "height": 900},
        args=["--disable-blink-features=AutomationControlled",
              "--disable-extensions"])
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.add_init_script(ub._STEALTH)
        _watch(page)
        resp = page.goto(cfg["warmup"], wait_until="domcontentloaded",
                         timeout=40000)
        print("warmup status:", resp.status if resp else None,
              "| final url:", page.url)
        time.sleep(2.0)  # same hydration pause _reach() uses
        for sel in ("#onetrust-accept-btn-handler",
                    "button:has-text('Accept all')",
                    "button:has-text('Accept All')"):
            loc = page.locator(sel)
            if loc.count():
                try:
                    loc.first.click()
                    time.sleep(0.6)
                except Exception:
                    pass
        body = page.inner_text("body")
        n, hits = summarize(body)
        print(f"warmup body: {n} chars | blocked={bool(hits)} markers={hits}")
        if hits and n < 900:
            i = body.lower().find(hits[0])
            print("  context:", " ".join(body[max(0, i - 70):i + 70].split()))
        filled = None
        for sel in cfg["inputs"]:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click()
                loc.first.fill(TID)
                filled = sel
                break
        print("input matched:", filled)
        if filled:
            for sub in cfg["submits"]:
                loc = page.locator(sub)
                if loc.count():
                    loc.first.click()
                    break
            else:
                page.keyboard.press("Enter")
            res_body = ""
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                time.sleep(1.5)
                try:
                    res_body = page.inner_text("body")
                except Exception:
                    continue
                if "Tracking Results" in res_body:
                    break
            n2, hits2 = summarize(res_body)
            print(f"result body: {n2} chars | "
                  f"TrackingResults={'Tracking Results' in res_body} | "
                  f"blocked={bool(hits2)} markers={hits2}")
            parsed = ub._parse_dhl_body(res_body, TID)
            print("parse:", parsed)
            print("result url after submit:", page.url)
            # What did the SPA actually render? Public page text only (the
            # probe never runs against real shipment data in stage A unless a
            # real tid was passed — and then only masked facts are printed).
            print("  body head:", " ".join(res_body.split())[:300])
            for phrase in ("no result", "not available", "couldn't find",
                           "could not find", "check the number"):
                if phrase in res_body.lower():
                    print("  no-results wording:", phrase)
            print("  api responses so far:", api_hits[:12])
            print("  frame scan:")
            frame_scan(page)

        # --- stage A2: does the tracking-id query param render headless? ---
        # carriers.py:110 already uses this URL for DHL's HTTP path; the
        # browser cfg claims the SPA "ignores URL parameters" — verify.
        print("\n=== stage A2: param-URL variant ===")
        page2 = ctx.new_page() if not ctx.pages else ctx.pages[0]
        resp2 = page2.goto(
            "https://www.dhl.com/us-en/home/tracking.html?tracking-id=" + TID,
            wait_until="domcontentloaded", timeout=40000)
        print("param-URL status:", resp2.status if resp2 else None)
        time.sleep(6.0)
        api_hits.clear()
        page2.add_init_script(ub._STEALTH)
        _watch(page2)
        pbody = page2.inner_text("body")
        pn, phits = summarize(pbody)
        print(f"param body: {pn} chars | "
              f"TrackingResults={'Tracking Results' in pbody} | "
              f"blocked={bool(phits)} markers={phits}")
        print("parse:", ub._parse_dhl_body(pbody, TID))
        print("final url:", page2.url)
        print("  body head:", " ".join(pbody.split())[:300])
        print("  api responses so far:", api_hits[:12])
        print("  frame scan:")
        frame_scan(page2)
    finally:
        ctx.close()

print("\n=== stage B: the tier's own path ===")
res = ub.get_browser_status("dhl", TID)
time.sleep(0.2)
print("tier result:", res)
print("tier outcome:", ub.get_last_outcome(TID))
print("strikes after:", ub._throttle_strikes.get("dhl", 0))
for msg in records:
    print("  tier log:", msg[:180])
