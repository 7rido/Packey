"""Ship24 Tracking API — the paid, per-call last-resort lookup.

Ship24 is an aggregator (~1,660 couriers when this was written) whose per-call
endpoint, POST /tracking/search, fetches results from the courier synchronously:
no tracker to create, nothing to persist, no webhooks. That makes it a drop-in
source next to usps_api.py — but a BILLED one, so carriers.fetch_track_status
only calls it after every free tier has come up empty, and
config.SHIP24_DAILY_MAX bounds what a day of stuck packages can cost.

Setup: https://dashboard.ship24.com → Integrations → API Keys (an apik_... key)
→ SHIP24_API_KEY in .env. The account needs the "per-call" subscription: the
per-shipment product is separate and serves the /trackers/* endpoints, which
this module deliberately does not use (verified 2026-09-16: a per-call key got
422 no_active_subscription on every /trackers/* call while /tracking/search
returned full results — see scripts/ship24_probe.py).

Response facts this module depends on (measured against the live API; pinned by
tests/test_ship24.py):
  * /tracking/search answers 201, not 200, with data.trackings[0].
  * The per-call response carries NO tracker object: trackerId is absent and
    courierCode is null. Status comes from shipment.statusMilestone (present on
    every response), with shipment.statusCode as the refinement.
  * events[] arrive newest-first; occurrenceDatetime is a "logistic date-time"
    (an offset is normal) and `order` may be null — both stay strings here.
  * Errors are {"errors":[{"code":...}],"data":null}: branch on the code.
"""
import json
import logging
import os
import time
from datetime import date
from typing import Any, Dict, Optional, Tuple

import httpx

import config
import storage

LOGGER = logging.getLogger("tracker")

BASE_URL = "https://api.ship24.com/public/v1"
SEARCH_PATH = "/tracking/search"

# A per-call fetch waits on the courier's own system (docs: up to a minute on a
# cold number), which is why this is NOT config.HTTP_TIMEOUT: aborting at 18 s
# would throw away a call we have already been billed for. Measured live latency
# on a known number was ~1.1 s.
REQUEST_TIMEOUT = 45.0

# Account-level failures (bad key, no per-call subscription) cannot fix
# themselves inside one cycle, so stop paying for round-trips that only fail.
_PAUSE_SECONDS = 3600.0
_paused_until = 0.0

# Billed calls, capped per local day. Persisted in config.SHIP24_COUNTER_FILE:
# this is a spending cap, so a restart must not hand the day's budget back. The
# old in-memory count reset to zero on every deploy, which is exactly when a
# stuck package is most likely to be retried against a paid source. The file is
# encrypted at rest like every other state file (storage._write_protected), and a
# stale day is treated as 0.
_calls_made = 0
_calls_day: Optional[str] = None


def _load_counter() -> None:
    """Restore today's paid-call count.

    Never raises, and -- importantly -- never writes: this runs at import time,
    where a write would land on the live path before any test fixture has
    redirected it. A counter left in plaintext by the pre-encryption version is
    rewritten encrypted by the next paid call (_save_counter).
    """
    global _calls_made, _calls_day
    try:
        text, was_encrypted = storage._read_protected(config.SHIP24_COUNTER_FILE)
    except FileNotFoundError:
        return
    except SystemExit:
        # Undecryptable ciphertext: _read_protected already kept it as .locked.
        return
    except Exception as e:
        LOGGER.warning("Could not read the Ship24 call counter: %s", e)
        return
    if text is None:
        return
    try:
        data = json.loads(text)
    except ValueError:
        LOGGER.warning("Could not parse the Ship24 call counter — starting the day at 0")
        return
    _calls_day = str(data.get("day") or "") or None
    _calls_made = max(0, int(data.get("calls") or 0))
    if not was_encrypted:
        LOGGER.warning("ship24_counter.json is PLAINTEXT at rest - the next paid "
                       "call rewrites it encrypted")
    if _calls_day != date.today().isoformat():
        # Yesterday's spend is not today's — _budget_left() would zero it on the
        # first lookup anyway, but calls_today() must not report it as today's.
        _calls_made, _calls_day = 0, None


def _save_counter() -> None:
    """Persist the counter, encrypted at rest; a failed write only costs accuracy
    across a restart (never the fallback itself)."""
    try:
        storage._write_protected(
            config.SHIP24_COUNTER_FILE,
            json.dumps({"day": _calls_day, "calls": _calls_made}, indent=1))
    except Exception as e:
        LOGGER.warning("Could not save the Ship24 call counter: %s", e)


_load_counter()


def _key() -> str:
    """Read at call time so a .env edit applies on restart without import-order
    surprises (same reason usps_api reads its creds late)."""
    return (os.getenv("SHIP24_API_KEY") or "").strip()


def enabled() -> bool:
    return bool(_key())


def paused() -> bool:
    """True while an account-level failure has paid lookups on hold."""
    return time.time() < _paused_until


def calls_today() -> int:
    return _calls_made


def _mask(tracking_id: str) -> str:
    """Mask for logs (9400...3527). The helper lives in carriers.py and is
    imported lazily so this module stays importable on its own."""
    from carriers import _sanitize_tracking_id
    return _sanitize_tracking_id(tracking_id)


def _json(resp: httpx.Response) -> Dict[str, Any]:
    try:
        payload = resp.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _error_codes(payload: Dict[str, Any]) -> str:
    """Ship24's error `code`s, comma-joined. The message text is never branched
    on: it is prose and may change without notice."""
    errors = (payload or {}).get("errors") or []
    if not isinstance(errors, list):
        return ""
    return ",".join(str(e.get("code")) for e in errors
                    if isinstance(e, dict) and e.get("code"))


def _retry_after(resp: httpx.Response) -> float:
    """Seconds to wait, from Retry-After (Ship24 mirrors it as RateLimit-Reset).

    Capped at one hour: the header is upstream-controlled, so an unbounded
    value would let a single hostile/buggy response park the paid tier for as
    long as it likes (2026-10-04 audit). carriers.py caps its own Retry-After
    for the same reason. Non-numeric values (HTTP-date form) fall through to
    the 60 s default below."""
    headers = getattr(resp, "headers", None) or {}
    for name in ("Retry-After", "RateLimit-Reset"):
        raw = headers.get(name)
        if raw:
            try:
                return min(3600.0, max(1.0, float(raw)))
            except (TypeError, ValueError):
                continue
    return 60.0


def _budget_left() -> bool:
    """Whether today's paid-call budget allows one more lookup."""
    global _calls_made, _calls_day
    today = date.today().isoformat()   # the operator's day, not UTC's
    if _calls_day != today:
        _calls_day, _calls_made = today, 0
    return _calls_made < config.SHIP24_DAILY_MAX


def _newest_event(events: Any) -> str:
    """events[] arrive newest-first, so events[0] is the text of the current
    state — the raw provider line a card shows as `detail`."""
    if not isinstance(events, list) or not events:
        return ""
    first = events[0] if isinstance(events[0], dict) else {}
    return " ".join(str(first.get("status") or "").split())[:200]


def _parse(resp: httpx.Response, tracking_id: str) -> Optional[Tuple[str, str, str]]:
    """(milestone, statusCode, newest event text) in Ship24's own vocabulary;
    carriers._map_ship24_status turns that into a bot status."""
    data = _json(resp)
    trackings = ((data.get("data") or {}).get("trackings")) or []
    if not trackings:
        LOGGER.info("Ship24: no tracking result for %s", _mask(tracking_id))
        return None
    tracking = trackings[0] if isinstance(trackings[0], dict) else {}
    shipment = tracking.get("shipment") or {}
    milestone = str(shipment.get("statusMilestone") or "").strip().lower()
    code = str(shipment.get("statusCode") or "").strip().lower()
    if not milestone and not code:
        LOGGER.info("Ship24: %s returned neither a milestone nor a code",
                    _mask(tracking_id))
        return None
    return milestone, code, _newest_event(tracking.get("events"))


async def check_ship24(tracking_id: str,
                       client: httpx.AsyncClient) -> Optional[Tuple[str, str, str]]:
    """One billed per-call lookup, or None.

    None means "no usable answer" — no key, paused after an account failure,
    daily budget spent, a transport error, or an error response. Every failure
    path is logged (codes only, never the key) and the caller simply keeps its
    own answer, so a paid source can never be why a card breaks.
    """
    global _paused_until, _calls_made
    if not enabled() or paused():
        return None
    if not _budget_left():
        LOGGER.info("Ship24: daily paid-call cap reached (%d) — skipping the fallback",
                    config.SHIP24_DAILY_MAX)
        return None
    # Counted before the request: a failed attempt can still be billed, and
    # undercounting is the expensive direction to be wrong in. Saved in the same
    # step so a crash mid-lookup cannot lose the spend.
    _calls_made += 1
    _save_counter()
    try:
        resp = await client.post(
            BASE_URL + SEARCH_PATH,
            json={"trackingNumber": tracking_id},
            headers={"Authorization": f"Bearer {_key()}",
                     "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        LOGGER.warning("Ship24 lookup failed for %s: %s", _mask(tracking_id), e)
        return None

    if resp.status_code in (200, 201):
        return _parse(resp, tracking_id)

    code = _error_codes(_json(resp)) or str(resp.status_code)
    if resp.status_code in (401, 403) or "auth_invalid_api_key" in code:
        _paused_until = time.time() + _PAUSE_SECONDS
        LOGGER.warning("Ship24: key rejected (%s) — pausing paid lookups for %d min",
                       code, _PAUSE_SECONDS // 60)
    elif "no_active_subscription" in code:
        # No per-call product on the account: every later call fails the same
        # way, so pay for that discovery once an hour instead of once a minute.
        _paused_until = time.time() + _PAUSE_SECONDS
        LOGGER.warning("Ship24: no per-call subscription (%s) — pausing paid "
                       "lookups for %d min", code, _PAUSE_SECONDS // 60)
    elif resp.status_code == 429:
        wait = _retry_after(resp)
        _paused_until = time.time() + wait
        LOGGER.warning("Ship24: rate limited — pausing paid lookups for %.0fs", wait)
    else:
        LOGGER.info("Ship24 lookup for %s failed: HTTP %s (%s)",
                    _mask(tracking_id), resp.status_code, code)
    return None


def reset_state() -> None:
    """Clear the pause and the daily counter (test hook). Also removes the
    persisted counter, so a test never inherits another test's spend."""
    global _paused_until, _calls_made, _calls_day
    _paused_until = 0.0
    _calls_made = 0
    _calls_day = None
    try:
        config.SHIP24_COUNTER_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        LOGGER.warning("Could not remove the Ship24 call counter: %s", e)
