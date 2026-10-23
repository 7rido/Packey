"""probe_scrape.py — inspect USPS / AfterShip pages for embedded status text."""
import re
import httpx

TID = "9400150206217911639042"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
KEYWORDS = ["delivered", "label created", "in transit", "departed", "arrived",
            "out for delivery", "accepted at", "pending", "status", "pre-shipment",
            "awaiting", "not yet"]


def show_context(text, needle, ctx=120, max_hits=3):
    low = text.lower()
    count = 0
    start = 0
    while count < max_hits:
        i = low.find(needle.lower(), start)
        if i < 0:
            break
        snippet = re.sub(r"\s+", " ", text[max(0, i - 60):i + len(needle) + 100])
        print(f"   …{snippet}…")
        count += 1
        start = i + len(needle)


with httpx.Client(timeout=15.0, follow_redirects=True,
                  headers={"User-Agent": UA}) as c:
    print("=== USPS track.htm ===")
    r = c.get(f"https://tools.usps.com/track.htm?tLabels={TID}")
    for kw in ["Label Created", "Delivered", "Pre-Shipment", "In Transit",
               "Acceptance", "Arrived", "Expected"]:
        if kw.lower() in r.text.lower():
            show_context(r.text, kw)
            break
    print("\n=== AfterShip public page ===")
    r2 = c.get(f"https://www.aftership.com/track/{TID}")
    for kw in KEYWORDS:
        show_context(r2.text, kw, max_hits=1)
        break
    for needle in ['"tag"', '"statusDescription"', '"slug"', '"lastEvent"',
                   '"checkpoints"', '"lastCheckpoint"', 'Label Created',
                   'Delivery Pending', 'pending']:
        if needle in r2.text:
            print(f"\n[found payload key: {needle}]")
            show_context(r2.text, needle, ctx=150, max_hits=2)