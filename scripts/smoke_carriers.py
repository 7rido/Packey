"""smoke_carriers.py — live smoke test of the browser tier for every carrier.

Runs Packey's REAL lookup path (usps_browser.get_browser_statuses: same Chrome,
UA, stealth, per-carrier session policy, parsers) once per carrier against the
live sites, in its own profile copy so it never touches the running bot's.

Usage:
  .venv/bin/python scripts/smoke_carriers.py                # defaults below
  .venv/bin/python scripts/smoke_carriers.py --from-store   # + numbers Packey tracks
  .venv/bin/python scripts/smoke_carriers.py --number ups=1Z... --carrier ups

Verdicts:
  PASS  a status was read, or (FedEx) the site's API answered "not found"
  WARN  page loaded and nothing went wrong, but a placeholder number can only
        prove that much — re-run with a real number for a full check
  FAIL  blocked, timed out, missed the API window, or a REAL number got nothing
Exit code 0 when nothing FAILed. Tracking numbers are masked in all output.
"""
import argparse
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import usps_browser as ub  # noqa: E402

ub._PROFILE_COPY = os.path.expanduser("~/Library/Caches/carrier_smoke_profile")

# Placeholders. FedEx's public test number returns real shipment data, so it is
# a full check; the others are format-valid fakes (page-load check only).
DEFAULTS = {
    "fedex": ("123456789012", True),
    "ups": ("1Z999AA10123456784", False),
    "usps": ("9400100000000000000000", False),
    "dhl": ("1234567890", False),
}


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


def from_store():
    """One live (non-terminal) number per browser carrier from Packey's store,
    read with storage.READ_ONLY so nothing is ever written back."""
    import carriers
    import config
    import storage
    storage.READ_ONLY = True
    storage.load_data(config.DATA_FILE)
    picked = {}
    for _chat, pkgs in storage.track_sessions.items():
        for tid, info in pkgs.items():
            slug = carriers.detect_carrier(tid)["code"]
            if slug not in DEFAULTS:
                continue
            live = info.get("status") not in ("Delivered",)
            if slug not in picked or (live and not picked[slug][2]):
                picked[slug] = (tid, True, live)
    return {s: (t, real) for s, (t, real, _live) in picked.items()}


def verdict(slug, tid, real, res, outcome, lines, secs):
    text = "\n".join(lines)
    if res:
        return "PASS", f"{res[0]} — {res[1][:70]}"
    if outcome in ("blocked", "throttled") or "BOT WALL" in text:
        return "FAIL", f"refused by the site ({outcome})"
    if outcome in ("error", "timeout", "no_profile", "no_playwright", "busy",
                   "aborted"):
        return "FAIL", f"lookup did not complete ({outcome})"
    if slug == "fedex":
        if "not found at FedEx" in text:
            return ("FAIL" if real and tid != DEFAULTS["fedex"][0] else "PASS",
                    "API answered: not found at FedEx")
        if "payload shape" in text:
            return "FAIL", "API answered but the parser read nothing (see log)"
        return "FAIL", "tracking API never fired (window missed)"
    if outcome == "parse_failed":
        return "FAIL", "page rendered a result the parser could not read"
    if real:
        return "FAIL", f"real number, no status ({outcome})"
    return "WARN", f"page loaded, no status — expected for a placeholder ({outcome})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--number", action="append", default=[], metavar="SLUG=TID")
    ap.add_argument("--carrier", action="append", default=[])
    ap.add_argument("--from-store", action="store_true")
    ap.add_argument("--budget", type=int, default=90)
    args = ap.parse_args()

    plan = dict(DEFAULTS)
    if args.from_store:
        plan.update(from_store())
    for item in args.number:
        slug, _, tid = item.partition("=")
        plan[slug.strip().lower()] = (tid.strip(), True)
    slugs = [s for s in ("usps", "ups", "fedex", "dhl")
             if s in ub.browser_slugs() and (not args.carrier or s in args.carrier)]

    cap = _Capture()
    log = logging.getLogger("tracker")
    log.addHandler(cap)
    log.setLevel(logging.INFO)
    print(f"carrier smoke — UA {ub.current_ua().split(') ')[-1]}, "
          f"profile {ub._PROFILE_COPY}\n")
    rows = []
    try:
        for slug in slugs:
            tid, real = plan[slug]
            kind = "real" if real else "placeholder"
            print(f"  {ub.page_label(slug):6} {ub._mask(tid)} ({kind}) …", flush=True)
            cap.lines.clear()
            t0 = time.monotonic()
            try:
                res = ub.get_browser_statuses(slug, [tid], args.budget).get(tid)
            except Exception as e:
                res = None
                cap.lines.append(f"exception {type(e).__name__}")
            secs = time.monotonic() - t0
            outcome = ub.get_last_outcome(tid)
            v, why = verdict(slug, tid, real, res, outcome, cap.lines, secs)
            rows.append((v, ub.page_label(slug), ub._mask(tid), kind, secs, why))
            print(f"         {v}  {secs:4.0f}s  {why}", flush=True)
    finally:
        log.removeHandler(cap)
        ub.cleanup_profile_copy()

    print("\nSUMMARY")
    for v, label, tid, kind, secs, why in rows:
        print(f"  {v:4}  {label:6} {tid:14} {kind:11} {secs:4.0f}s  {why}")
    fails = sum(1 for r in rows if r[0] == "FAIL")
    print(f"\n{len(rows)} carrier(s): {len(rows) - fails} ok/warn, {fails} fail")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
