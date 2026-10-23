"""ship24_probe.py — one-shot evaluation probe for the Ship24 Tracking API.

Same job scripts/pb_probe.py had for Pitney Bowes SendPro 360: prove (or
disprove) that a candidate source really answers for our tracking numbers
BEFORE any app module learns about it. Nothing here is imported by the bot —
the probe talks to Ship24 directly, prints only masked numbers, and never
echoes the key.

Usage (run from the project root, which is where .env lives):
  python3 scripts/ship24_probe.py                  # auth + couriers + 8 sample lookups
  python3 scripts/ship24_probe.py --key-only       # auth/plan check, creates no tracker
  python3 scripts/ship24_probe.py --number 9400... # probe real numbers too (masked)
  python3 scripts/ship24_probe.py --no-samples     # only the numbers given on the CLI
  python3 scripts/ship24_probe.py --per-call       # also try POST /tracking/search
  python3 scripts/ship24_probe.py --cleanup        # unsubscribe the sample trackers

The sample numbers Ship24 publishes (SHIP24_SAMPLE_<MILESTONE>_000) exercise
every milestone without touching a single customer number, so this probe can
run before we decide anything. A run creates one tracker per sample on the
account; --cleanup PATCHes isSubscribed=false on THOSE afterwards (results stay
readable on an unsubscribed tracker). Trackers made for real numbers are left
subscribed on purpose — those are the ones an integration would keep.

Exit code 0 = the key works and at least one lookup answered; 1 = it did not.
"""
import argparse
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

# config is imported for its side effect: it loads .env (python-dotenv) before
# anything reads os.getenv below — the same trick the other scripts/ probes use.
# Its resolved location is also what the header prints as the project root: the
# working directory can be a symlink to the real checkout, and a probe that
# reports a MISSING key must be able to show which .env it actually read.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

PROJECT_ROOT = Path(config.__file__).resolve().parent

BASE_URL = "https://api.ship24.com/public/v1"
KEY_ENV = "SHIP24_API_KEY"

# The documented first call of /trackers/track queries the courier
# synchronously and may take up to 60 s — far beyond the bot's own
# config.HTTP_TIMEOUT (18 s), which is exactly what decides whether a per-call
# integration fits the refresh loop at all. Warm calls are fast.
COLD_TIMEOUT = 90.0
WARM_TIMEOUT = 20.0
# /trackers/track allows 10 req/s; the probe stays well under it on purpose
# (we are guests on the account we are evaluating).
CALL_GAP = 1.2

# Ship24's published test numbers, one per statusMilestone.
SAMPLE_MILESTONES = [
    "PENDING",
    "INFO_RECEIVED",
    "IN_TRANSIT",
    "OUT_FOR_DELIVERY",
    "FAILED_ATTEMPT",
    "AVAILABLE_FOR_PICKUP",
    "DELIVERED",
    "EXCEPTION",
]

# The mapping the bot WOULD use (carriers._map_ship24_status, if we integrate).
# Kept here so the verdict can state coverage instead of implying it.
MILESTONE_TO_STATUS = {
    "info_received": "Info Received",
    "in_transit": "In Transit",
    "out_for_delivery": "Out for Delivery",
    "failed_attempt": "Delivery Attempted",
    "available_for_pickup": "Available for Pickup",
    "delivered": "Delivered",
    "exception": "Exception",
    "pending": "Pending",
}

# Courier codes the slug -> courierCode map would need, one per carrier the bot
# auto-detects (carriers._SLUG_NAME_KEYWORDS).
WANTED_COURIERS = ["us-post", "ups", "fedex", "dhl", "ontrac", "royal-mail", "canada-post"]


def _mask(value: str) -> str:
    """Same shape as carriers._sanitize_tracking_id — never a full number."""
    value = str(value or "")
    if len(value) <= 8:
        return value or "?"
    return f"{value[:4]}…{value[-4:]}({len(value)})"


def _key() -> str:
    return (os.getenv(KEY_ENV) or "").strip()


def _fingerprint(key: str) -> str:
    """Describe the key without disclosing any of it. 'apik_' is public."""
    return (f"present (len {len(key)}, prefix {'apik_ (ok)' if key.startswith('apik_') else 'NOT apik_…'})")


def _headers(key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def _rate_line(resp: httpx.Response) -> str:
    """Ship24 sends both RateLimit-* (IETF draft) and X-RateLimit-*; show what
    is actually there so we know which family to trust in the integration."""
    parts = []
    for name in ("RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset",
                 "Retry-After", "X-RateLimit-Remaining"):
        val = resp.headers.get(name)
        if val is not None:
            parts.append(f"{name}={val}")
    return "  ".join(parts)


def _error_codes(resp: httpx.Response) -> str:
    """Branch on Ship24's error `code`, never on the message text."""
    if resp.status_code < 300:
        # The error-path probes also print through here; a 2xx means the input
        # was accepted, and dumping the payload would be noise, not a code.
        return "(no error: 2xx)"
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "")[:160].replace("\n", " ")
    if isinstance(body, dict):
        errors = body.get("errors") or []
        codes = [str(e.get("code")) for e in errors if isinstance(e, dict)]
        if codes:
            return ",".join(codes)
        if body.get("status"):        # /trackers/bulk and friends
            return str(body["status"])
    return str(body)[:160]


async def _call(client: httpx.AsyncClient, method: str, url: str, key: str,
                timeout: float, **kw) -> Tuple[Optional[httpx.Response], float]:
    """One timed request; prints the status line so the log reads top-down."""
    t0 = time.perf_counter()
    try:
        resp = await client.request(method, url, headers=_headers(key),
                                    timeout=timeout, **kw)
    except Exception as e:
        print(f"    {method} {url.split('/public/v1')[-1]} → "
              f"{type(e).__name__}: {e}")
        return None, 0.0
    ms = (time.perf_counter() - t0) * 1000
    rate = _rate_line(resp)
    print(f"    {method} {url.split('/public/v1')[-1]} → HTTP {resp.status_code} "
          f"in {ms:.0f} ms" + (f"   {rate}" if rate else ""))
    return resp, ms


def _courier_codes(tracker: dict) -> str:
    codes = tracker.get("courierCode")
    if isinstance(codes, list):
        return ", ".join(str(c) for c in codes) or "?"
    return str(codes or "?")


def _first_tracking(payload: dict) -> Optional[dict]:
    trackings = ((payload.get("data") or {}).get("trackings")) or []
    return trackings[0] if trackings else None


def _print_tracking(tracking: dict) -> str:
    """Print what matters for the mapping decision; returns the milestone."""
    tracker = tracking.get("tracker") or {}
    shipment = tracking.get("shipment") or {}
    events = tracking.get("events") or []
    stamps = ((tracking.get("statistics") or {}).get("timestamps")) or {}

    milestone = str(shipment.get("statusMilestone") or "")
    code = shipment.get("statusCode")
    category = shipment.get("statusCategory")
    print(f"      tracker   isTracked={tracker.get('isTracked')} "
          f"id={_mask(tracker.get('trackerId'))}")
    print(f"      shipment  milestone={milestone or 'null'}  "
          f"code={code or 'null'}  category={category or 'null'}")
    print(f"      courier   {_courier_codes(tracker)}")
    if events:
        # events arrive most-recent-first; order survives events without a time
        newest = events[0]
        text = " ".join(str(newest.get("status") or "").split())[:70]
        print(f"      events    n={len(events)}  newest: \"{text}\" "
              f"@ {newest.get('occurrenceDatetime')} (order {newest.get('order')})")
    else:
        print("      events    n=0  ← nothing recorded yet (pre-scan)")
    if stamps:
        present = " ".join(f"{k[:-8]}={'yes' if v else 'no'}"
                           for k, v in stamps.items() if k.endswith("Datetime"))
        print(f"      stamps    {present}")
    wanted = MILESTONE_TO_STATUS.get(milestone)
    print(f"      → maps to bot status: {wanted!r}" if wanted
          else "      → maps to bot status: NONE (unknown milestone)")
    return milestone


async def probe_couriers(client: httpx.AsyncClient, key: str) -> Optional[Dict[str, dict]]:
    """Step 1: the cheapest call that proves the key + plan are alive."""
    print("\n[1] GET /couriers — proves the key/plan; also answers which courier "
          "codes our slugs would map to (1 req/s endpoint).")
    resp, _ = await _call(client, "GET", f"{BASE_URL}/couriers", key, WARM_TIMEOUT)
    if resp is None:
        return None
    if resp.status_code != 200:
        print(f"    error code: {_error_codes(resp)}   ← 401/403 here = no usable key")
        return None
    couriers = ((resp.json().get("data") or {}).get("couriers")) or []
    by_code = {c.get("courierCode"): c for c in couriers}
    print(f"    couriers known to Ship24: {len(couriers)}")
    for code in WANTED_COURIERS:
        entry = by_code.get(code)
        if not entry:
            print(f"      {code:<14} MISSING (a different code would be needed)")
            continue
        flags = []
        required = entry.get("requiredFields") or []
        if required:
            flags.append("requires " + ",".join(required))
        if entry.get("isDeprecated"):
            flags.append("DEPRECATED")
        print(f"      {code:<14} {entry.get('courierName')} "
              f"({entry.get('countryCode')})"
              + (f" — {'; '.join(flags)}" if flags else ""))
    return by_code


async def probe_track(client: httpx.AsyncClient, key: str, number: str, label: str,
                      timeout: float, client_tracker_id: Optional[str] = None) -> Dict[str, object]:
    """Create-or-fetch a tracker for one number and print the result.

    /trackers/track is idempotent for an identical payload, so repeating a call
    with the same clientTrackerId measures the WARM latency (the tracker already
    exists) instead of creating a second one. Returns
    {"ok", "milestone", "tracker_id", "ms", ...} for the verdict and the cleanup.
    """
    body: Dict[str, object] = {"trackingNumber": number}
    if client_tracker_id:
        body["clientTrackerId"] = client_tracker_id
    out: Dict[str, object] = {"ok": False, "milestone": "", "tracker_id": "",
                              "ms": 0.0, "number": number, "label": label}
    print(f"\n  {label}: {_mask(number)}")
    resp, ms = await _call(client, "POST", f"{BASE_URL}/trackers/track", key,
                           timeout, json=body)
    out["ms"] = ms
    if resp is None:
        return out
    if resp.status_code not in (200, 201):
        out["error"] = _error_codes(resp)
        print(f"      error code: {out['error']}")
        return out
    tracking = _first_tracking(resp.json())
    if tracking is None:
        print("      HTTP OK but data.trackings is empty — nothing to map")
        return out
    out["milestone"] = _print_tracking(tracking)
    out["tracker_id"] = (tracking.get("tracker") or {}).get("trackerId") or ""
    out["ok"] = bool(out["milestone"])
    print(f"      latency   {ms:.0f} ms  (bot HTTP_TIMEOUT = 18000 ms)")
    return out


async def probe_per_call(client: httpx.AsyncClient, key: str, number: str) -> Optional[bool]:
    """Step 4: does the account carry the per-call subscription?

    /tracking/search answers 201 (the docs' own example) — accepting only 200
    here is what made an earlier run of this probe call a working per-call
    account "not active".
    """
    print("\n[4] POST /tracking/search — the per-call product (a separate subscription).")
    resp, _ = await _call(client, "POST", f"{BASE_URL}/tracking/search", key,
                          COLD_TIMEOUT, json={"trackingNumber": number})
    if resp is None:
        return None
    if resp.status_code in (200, 201):
        tracking = _first_tracking(resp.json())
        if tracking:
            _print_tracking(tracking)
        else:
            print("      2xx but the body carried no trackings")
        print("      → the per-call product IS active on this account")
        return True
    code = _error_codes(resp)
    print(f"      error code: {code}")
    if "no_active_subscription" in code:
        print("      ← no per-call add-on on this account")
    return False


async def probe_error_paths(client: httpx.AsyncClient, key: str) -> None:
    """Step 5: the exact codes an integration must branch on. No real numbers —
    a dummy value and a deliberately bogus bearer."""
    print("\n[5] error paths — the codes we would branch on.")
    checks = [
        ("bogus bearer", "GET", f"{BASE_URL}/trackers", "probe-invalid-key", None),
        ("unknown trackerId", "GET", f"{BASE_URL}/trackers/{uuid.uuid4()}/results", key, None),
        ("number too short", "POST", f"{BASE_URL}/trackers/track", key, {"trackingNumber": "0000"}),
    ]
    for note, method, url, use_key, payload in checks:
        resp, _ = await _call(client, method, url, use_key, WARM_TIMEOUT,
                              **({"json": payload} if payload else {}))
        if resp is not None:
            code = _error_codes(resp)
            # Without a Trackers subscription the gate answers first, so the code
            # we came for stays hidden — say so instead of reporting it as ours.
            masked = ("   (masked: the plan gate answers first)"
                      if "no_active_subscription" in code else "")
            print(f"    {note:<18} → code={code}{masked}")


async def unsubscribe(client: httpx.AsyncClient, key: str, tracker_id: str) -> bool:
    """PATCH isSubscribed=false: stop tracking without losing the results."""
    resp, _ = await _call(client, "PATCH", f"{BASE_URL}/trackers/{tracker_id}", key,
                          WARM_TIMEOUT, json={"isSubscribed": False})
    return bool(resp is not None and resp.status_code in (200, 202, 204))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the Ship24 Tracking API as a source for Packey.")
    parser.add_argument("--key-only", action="store_true",
                        help="stop after the auth/plan check (creates nothing)")
    parser.add_argument("--no-samples", action="store_true",
                        help="skip Ship24's published SHIP24_SAMPLE_* numbers")
    parser.add_argument("--number", action="append", default=[], metavar="TID",
                        help="also probe this real number (repeatable; masked in output)")
    parser.add_argument("--per-call", action="store_true",
                        help="also exercise POST /tracking/search (per-call plan)")
    parser.add_argument("--cleanup", action="store_true",
                        help="unsubscribe the sample trackers this run created "
                             "(real numbers are kept subscribed)")
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    key = _key()
    print("ship24_probe — evaluating Ship24 against this bot's tracking numbers.")
    print(f"  project root: {PROJECT_ROOT}")
    print(f"  {KEY_ENV}: {_fingerprint(key) if key else 'MISSING'}")
    if not key:
        print(f"\n  Put {KEY_ENV}=apik_... in .env (Dashboard → Integrations → API Keys).")
        print("  A free plan is enough for the sample numbers below.")
        return 1

    async with httpx.AsyncClient(timeout=WARM_TIMEOUT) as client:
        couriers = await probe_couriers(client, key)
        if couriers is None:
            return 1
        if args.key_only:
            print("\nVERDICT\n  key/plan ...... OK (--key-only: nothing else was called)")
            return 0

        created: List[Dict[str, object]] = []
        warm_ms: Optional[float] = None
        per_call: Optional[bool] = None

        if not args.no_samples:
            planned = [(name, f"SHIP24_SAMPLE_{name}_000",
                        f"probe-{uuid.uuid4().hex[:8]}-{name.lower()}")
                       for name in SAMPLE_MILESTONES]
            print(f"\n[2] POST /trackers/track — {len(planned)} published sample numbers "
                  f"(one tracker each; a cold call may take up to 60 s).")
            for name, number, cid in planned:
                res = await probe_track(client, key, number, f"sample {name.lower()}",
                                        COLD_TIMEOUT, cid)
                res["cid"] = cid
                res["sample"] = True      # disposable → the only ones --cleanup archives
                created.append(res)
                if not res["ok"] and "no_active_subscription" in str(res.get("error") or ""):
                    # The plan gate refuses every tracker call alike, so the rest
                    # would be 7 identical failures against the account's quota.
                    print(f"      → per-shipment trackers are not on this account; the "
                          f"other {len(planned) - len(created)} samples would fail the "
                          f"same way. Stopping this step.")
                    break
                await asyncio.sleep(CALL_GAP)

            # Step 3: the same payload again. Idempotent → no second tracker, and
            # the timing is what decides whether polling fits the bot's 18 s cap.
            delivered = next((r for r in created
                              if r["ok"] and str(r["cid"]).endswith("delivered")), None)
            if delivered:
                print("\n[3] warm call — same payload again (idempotent, no new tracker).")
                warm = await probe_track(client, key, str(delivered["number"]),
                                         "sample delivered (warm)", WARM_TIMEOUT,
                                         str(delivered["cid"]))
                warm_ms = float(warm["ms"]) or None

        if args.number:
            print(f"\n[2b] real numbers from the command line ({len(args.number)}).")
            for i, number in enumerate(args.number):
                res = await probe_track(client, key, number, f"real #{i + 1}",
                                        COLD_TIMEOUT, f"probe-{uuid.uuid4().hex[:8]}")
                res["sample"] = False     # a real tracker is worth keeping
                created.append(res)
                await asyncio.sleep(CALL_GAP)

        if not created:
            print("\n  nothing was looked up (--no-samples and no --number)")
            return 0

        if args.per_call:
            per_call = await probe_per_call(client, key, str(created[0]["number"]))

        await probe_error_paths(client, key)

        # ── Verdict: the whole point of the probe ─────────────────────────
        usable = [r for r in created if r["ok"]]
        cold = [float(r["ms"]) for r in created if r["ms"]]
        mapped = sorted({str(r["milestone"]) for r in usable})
        unknown = sorted({str(r["milestone"]) for r in usable} - set(MILESTONE_TO_STATUS))
        ids = [str(r["tracker_id"]) for r in created if r["tracker_id"]]
        # Every tracker call refused with the same plan-gate code = the account
        # simply has no per-shipment subscription (the codes below are moot then).
        errors = [str(r.get("error") or "") for r in created]
        gated = bool(errors) and all("no_active_subscription" in e for e in errors)
        print("\nVERDICT")
        print(f"  key/plan .............. OK ({len(couriers)} couriers visible)")
        print("  bot HTTP_TIMEOUT ...... 18000 ms (config.HTTP_TIMEOUT)")
        if cold:
            print(f"  cold /trackers/track .. {min(cold):.0f}-{max(cold):.0f} ms "
                  f"over {len(cold)} calls")
        if warm_ms:
            print(f"  warm /trackers/track .. {warm_ms:.0f} ms")
        print(f"  milestone coverage .... {len(usable)}/{len(created)} lookups returned a "
              f"milestone: {', '.join(mapped) if mapped else 'NONE'}")
        print(f"  unmapped milestones ... {', '.join(unknown) if unknown else 'none'}")
        print("  per-call add-on ....... "
              + ({True: "ACTIVE", False: "not active"}.get(per_call)
                 or "not tested (--per-call to check)"))
        if gated:
            print("  per-shipment product .. NOT subscribed "
                  "(every /trackers/track → 422 no_active_subscription)")
        # Only the disposable sample trackers are archived; a tracker created
        # for a real number is exactly what a future integration would reuse.
        sample_ids = [str(r["tracker_id"]) for r in created
                      if r["tracker_id"] and r.get("sample")]
        kept = sum(1 for r in created if r["tracker_id"] and not r.get("sample"))
        print(f"  trackers created ...... {len(ids)} "
              f"({len(sample_ids)} sample, {kept} real)")
        if usable and cold and max(cold) > 18000:
            print("  recommendation ........ VIABLE, but a cold call exceeded the bot's "
                  "18 s timeout → create-then-poll (/trackers, then /trackers/{id}/results) "
                  "or a longer timeout on the add path only.")
        elif usable:
            print("  recommendation ........ VIABLE inside the current 18 s timeout — a "
                  "per-shipment tracker tier can slot into carriers.fetch_track_status "
                  "exactly like usps_api.py does.")
        elif gated and per_call:
            print("  recommendation ........ PER-CALL ONLY: the tracker endpoints are "
                  "gated, but POST /tracking/search answers. That endpoint is stateless "
                  "(nothing to persist, no trackerId, no webhooks) — integrate it as a "
                  "one-shot lookup, budget for up to ~60 s on a cold courier fetch.")
        elif gated:
            print("  recommendation ........ NOT USABLE yet: the per-shipment product is "
                  "not subscribed (422 no_active_subscription on every tracker call). "
                  "Subscribe a Trackers plan, or enable per-call and use "
                  "POST /tracking/search.")
        else:
            print("  recommendation ........ NOT USABLE: no lookup returned a status.")

        if args.cleanup and sample_ids:
            print(f"\n[6] PATCH isSubscribed=false on the {len(sample_ids)} sample trackers "
                  f"this run made.")
            for tracker_id in sample_ids:
                ok = await unsubscribe(client, key, tracker_id)
                print(f"    {_mask(tracker_id)} → {'unsubscribed' if ok else 'FAILED'}")
        elif sample_ids:
            print(f"\n[6] {len(sample_ids)} sample trackers are now subscribed on the "
                  f"account — re-run with --cleanup, or archive them in the dashboard.")

        return 0 if (usable or per_call) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
