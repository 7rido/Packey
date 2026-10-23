"""Carrier lookup backends for the tracker bot.

Extracted from main.py (Session 3 of EXTRACTION_PLAN.md): carrier detection,
the shared async HTTP client, rate limiters, 17TRACK helpers + registration,
the USPS tiers (API v3, Web Tools, experimental browser), the OnTrac direct
scrape, and the fetch_track_status dispatcher.

main.py re-imports every name used elsewhere, so handlers and tests keep
working. carriers.py imports nothing from main — cooldown persistence goes
through an injected callback (main wires it once via configure()), which
keeps the import graph acyclic so main.py also works when run as a script
(`__main__`; a module-level `from main import ...` here would re-execute
main.py under a second name and crash with a circular ImportError).
Mutable cross-module shares (`track_sessions`, `_register_blocked_until`)
come from storage as the same objects.
"""
import asyncio
import collections
import contextlib
import logging
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from html import unescape
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import quoteattr

import httpx

import usps_api  # USPS Tracking API v3 (OAuth2) — optional; enabled via .env
import ship24  # Ship24 per-call fallback — optional AND paid; enabled via .env
import config
import storage
from storage import track_sessions, _register_blocked_until

# Browser-capable carriers: the slug list and labels come from usps_browser's
# own table (one source of truth). usps_browser imports playwright lazily, so
# this eager import is cheap; the browser tier itself still launches nothing
# unless USPS_BROWSER_TRACK is on and a lookup reaches it.
try:
    import usps_browser as _ub
    _browser_slugs = _ub.browser_slugs()
    _browser_labels = {s: _ub.page_label(s) for s in _browser_slugs}
except ImportError:      # usps_browser.py missing — no browser tier at all
    _browser_slugs = ()
    _browser_labels = {}

# Cooldown persistence: main owns REG_COOLDOWN_FILE (and its test patching),
# so main injects its save_cooldown() wrapper via configure(). Injection —
# not `from main import ...` — keeps imports acyclic for script execution.
_save_cooldown_fn = None


def configure(save_cooldown=None) -> None:
    """main.py wires its save_cooldown() wrapper once at import time."""
    global _save_cooldown_fn
    if save_cooldown is not None:
        _save_cooldown_fn = save_cooldown


def _persist_cooldown() -> None:
    # Unconfigured (e.g. a bare `import carriers` in a probe) → nothing to
    # persist to; register_17track/load paths interpret this as memory-only.
    if _save_cooldown_fn is not None:
        _save_cooldown_fn()

logger = logging.getLogger("tracker")

TRACK17_BASE_URL = "https://api.17track.net/track/v2.4"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


# ──────────────────────────────────────────────
# Carrier detection
# ──────────────────────────────────────────────
CARRIER_NAMES = {
    "usps": "USPS",
    "ups": "UPS",
    "fedex": "FedEx",
    "dhl": "DHL",
    "ontrac": "OnTrac",
    "royalmail": "Royal Mail",
    "canadapost": "Canada Post",
    "chinapost": "China Post / ePacket",
    "hkpost": "Hongkong Post",
    "intl": "International post",
    "unknown": "Unknown Carrier",
}

# Universal fallback: 17TRACK's own tracking page accepts any number from any
# of its 3,500+ carriers, so every card gets a working link even when we have
# no carrier-specific deep link (CN/HK/EU posts mostly aren't deep-linkable).
UNIVERSAL_TRACK_URL = "https://www.17track.net/en/tracknums?nums={tid}"

CARRIER_URL_TEMPLATES = {
    "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={tid}",
    "ups": "https://www.ups.com/track?tracknum={tid}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={tid}",
    "dhl": "https://www.dhl.com/us-en/home/tracking.html?tracking-id={tid}",
    "ontrac": "https://www.ontrac.com/trackingres.asp?tracking_number={tid}",
    "royalmail": "https://www.royalmail.com/track-your-item#/tracking-results/{tid}",
    "canadapost": "https://www.canadapost-postescanada.ca/track-reperage/en#/search?searchFor={tid}",
    "chinapost": UNIVERSAL_TRACK_URL,
    "hkpost": UNIVERSAL_TRACK_URL,
    "intl": UNIVERSAL_TRACK_URL,
}

# UPU-style numbers end with the ORIGIN country code (RR123456789CN, EE…HK),
# which is how international posts are identified without a carrier API.
_COUNTRY_SUFFIX_SLUGS = {
    "US": "usps",
    "GB": "royalmail",
    "CN": "chinapost",
    "HK": "hkpost",
}


def detect_carrier(tracking_id: str) -> dict:
    """Best-effort carrier detection from number format. Not exhaustive —
    17TRACK auto-detection is more reliable; treat this as a
    fallback first guess. International UPU numbers end with their ORIGIN
    country code (RR123456789CN, EE…HK), so CN/HK/EU posts are recognised
    without any carrier API."""
    tid = tracking_id.strip().upper().replace(" ", "")

    # UPU/S10 format: 2 letters + 9 digits + origin country code. Checked
    # first: it also covers the US and GB postal patterns below.
    upu = re.fullmatch(r"([A-Z]{2})(\d{9})([A-Z]{2})", tid)
    if upu:
        slug = _COUNTRY_SUFFIX_SLUGS.get(upu.group(3), "intl")
    elif re.fullmatch(r"1Z[0-9A-Z]{16}", tid):
        slug = "ups"
    elif re.fullmatch(r"(94|93|92|95|82|420\d{5}9\d{2})\d{16,20}", tid):
        slug = "usps"
    elif re.fullmatch(r"\d{16}", tid):
        slug = "canadapost"
    elif re.fullmatch(r"[CD]\d{14}", tid):
        slug = "ontrac"
    elif re.fullmatch(r"\d{10}|\d{11}", tid):
        slug = "dhl"
    elif re.fullmatch(r"\d{12}|\d{15}|\d{20}", tid):
        # FedEx Ground/Express are 12, SmartPost-era 15, and the newer
        # GSN range runs 20 digits. 16-digit numbers above stay Canada Post.
        slug = "fedex"
    else:
        slug = "unknown"

    # Every slug gets a link: carrier-specific when known, else 17TRACK's
    # universal tracker (which accepts all 3,500+ carriers it knows).
    template = CARRIER_URL_TEMPLATES.get(slug) or UNIVERSAL_TRACK_URL
    name = CARRIER_NAMES.get(slug, "Unknown Carrier")
    if slug == "intl" and upu:
        name = f"International post ({upu.group(3)})"
    return {
        "code": slug,
        "name": name,
        "url": template.format(tid=tid),
    }


# The 🌍 International lane: national posts that ONLY 17TRACK can check
# (CN/HK and other countries' post offices). Global express carriers
# (UPS/FedEx/DHL) are not "international" in this sense. Defined here — next to
# the slugs themselves — because both the Mini App view and the Pro
# international allowance (pro.check_intl_limit) must classify identically.
INTERNATIONAL_SLUGS = {"chinapost", "hkpost", "intl"}
INTERNATIONAL_COUNTRIES = {"CN", "HK"}


def is_international(info: dict) -> bool:
    """Whether a package belongs in the 🌍 International view / counts against
    the Pro international allowance. Accepts a stored package dict (key
    "carrier") or a detect_carrier() result (key "code")."""
    slug = info.get("carrier") or info.get("code") or ""
    if slug in INTERNATIONAL_SLUGS:
        return True
    # 17TRACK's verified origin country catches numbers our format guess
    # labelled 'unknown' (and is the only signal for the others).
    country = (info.get("carrier_country") or "").upper()
    return country in INTERNATIONAL_COUNTRIES


def is_international_number(tracking_id: str) -> bool:
    """Format-only classification for a number that has no stored record yet.

    Used at add time: a UPU/S10 number ending in CN/HK (or any non-US/GB
    country code) is already known to be an international post before the first
    lookup happens, so the allowance can't be exceeded by adding several at once.
    """
    return is_international(detect_carrier(tracking_id))


# ──────────────────────────────────────────────
# Shared HTTP client
# ──────────────────────────────────────────────
_http: Optional[httpx.AsyncClient] = None


def http_client() -> httpx.AsyncClient:
    """One shared async client, created lazily inside the running loop.

    The pool is bounded: the carrier fan-out (REFRESH_CONCURRENCY) plus the
    Solana poller share this client, and an unbounded default let a burst open
    as many sockets as there were packages.
    """
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=config.HTTP_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20,
                                max_keepalive_connections=config.REFRESH_CONCURRENCY),
        )
    return _http


# ──────────────────────────────────────────────
# 17TRACK rate limiting
# ──────────────────────────────────────────────
# 3 req/sec tripped HTTP 429s on the live bot (three POSTs to /gettrackinfo inside
# the same second, 08:59:56 and again at 12:25:19), so the cap sits a notch under
# the rate that was observed to fail. 17TRACK's published per-plan limit could not
# be read from this machine (their docs render client-side, and the help-centre
# article answers 403), which is the second reason to stay conservative rather than
# tune to the edge: a 429 wastes a whole refresh round, while one extra request per
# second costs nothing measurable. Retries go through acquire() too, so a back-off
# that lands inside the budget is not allowed to jump the queue.
class RateLimiter:
    def __init__(self, rate: int, per: float = 1.0):
        self.rate = rate
        self.per = per
        self.calls = collections.deque()
        # Lazily created inside the running loop — an asyncio.Lock() made at
        # module import binds to the wrong loop on some Python versions.
        self._lock: Optional[asyncio.Lock] = None

    async def acquire(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            while True:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > self.per:
                    self.calls.popleft()
                if len(self.calls) < self.rate:
                    self.calls.append(now)
                    return
                await asyncio.sleep(self.per - (now - self.calls[0]))


track17_rate_limiter = RateLimiter(rate=2, per=1.0)
usps_rate_limiter = RateLimiter(rate=2, per=1.0)  # USPS Web Tools: be polite

# Operational lookup stats — re-exported by main for /status.
LOOKUP_OK = collections.Counter()    # source -> successful reads
LOOKUP_FAIL = collections.Counter()  # source -> failed/empty reads
# The same outcomes split by chat tier — this is what lets /status prove the
# free tier never spends 17TRACK (the question that keeps coming up).
LOOKUP_TIER: Dict[str, Dict[str, int]] = {
    "pro": {"ok": 0, "fail": 0},
    "free": {"ok": 0, "fail": 0},
    "unknown": {"ok": 0, "fail": 0},
}

# One browser PER CARRIER at a time (also touched by main's refresh plumbing
# for the batch path — always via carriers._browser_slot(slug)). Slots are
# keyed by carrier: carriers run in parallel with each other while each
# carrier's request rate stays at ONE session — parallelism never multiplies
# what any single carrier's WAF sees from us. A hang rebuilds only THAT
# carrier's slot; the others keep serving.
_usps_browser_sem: Optional[asyncio.Semaphore] = None  # legacy name, unused
_browser_sems: Dict[str, asyncio.Semaphore] = {}

# Browser-hang watchdog: every Playwright step has its own timeout, but the
# driver handshake and a wedged renderer sit OUTSIDE those — a hung worker
# thread would otherwise hold its slot forever and silently wedge the tier
# until restart. Constants live here (not in .env) so no knob can disable
# the recovery.
_BROWSER_BODY_TIMEOUT_S = 240.0  # per ≤USPS_BATCH_MAX session chunk; internal
#                                 budgets are ~55-120s, so this fires on a
#                                 genuine hang, not a slow page
_BROWSER_QUEUE_STEP_S = 30.0     # poll slice while waiting for a free slot
_BROWSER_QUEUE_MAX_S = 900.0     # absolute cap on queueing (a healthy
#                                 multi-carrier sweep holds a slot ≲ 10 min)


def _slot_depth() -> int:
    """Concurrent sessions allowed for ONE carrier (config.USPS_BROWSER_
    SLOTS_PER_CARRIER). Carriers always run in parallel with each other; this
    knob only multiplies a single carrier's request rate — the thing that
    historically woke the bot flag — so it defaults to 1."""
    try:
        return max(1, int(config.USPS_BROWSER_SLOTS_PER_CARRIER))
    except (TypeError, ValueError):
        return 1


async def _acquire_browser_sem(slug: str) -> asyncio.Semaphore:
    """Acquire the carrier's browser lock, re-reading the slot between short
    waits so a watchdog rebuild (which REPLACES that carrier's semaphore)
    can never strand a waiter on the dead lock. The None-check, creation and
    dict store happen without an await between them, so two waiters can never
    build two live semaphores for the same carrier."""
    deadline = time.monotonic() + _BROWSER_QUEUE_MAX_S
    while True:
        sem = _browser_sems.get(slug)
        if sem is None:
            sem = asyncio.Semaphore(_slot_depth())
            _browser_sems[slug] = sem
        try:
            await asyncio.wait_for(sem.acquire(), timeout=_BROWSER_QUEUE_STEP_S)
            return sem
        except asyncio.TimeoutError:
            if time.monotonic() >= deadline:
                raise


@contextlib.asynccontextmanager
async def _browser_slot(slug: str = "usps"):
    """Owns ONE carrier's browser lock for a lookup/batch (both
    carriers._check_browser and refresh's per-carrier sweep batches). If the
    body escapes with asyncio.TimeoutError — the watchdog's signature — THAT
    carrier's lock is held by a hung worker that will never return: rebuild
    it instead of releasing, so later lookups queue on a fresh lock. Other
    carriers' slots are untouched. The abandoned worker thread cannot be
    killed from Python; it is left to expire alone."""
    sem = await _acquire_browser_sem(slug)
    hung = False
    try:
        yield sem
    except asyncio.TimeoutError:
        hung = True
        raise
    finally:
        if hung:
            _browser_sems[slug] = asyncio.Semaphore(_slot_depth())
            logger.warning("%s browser: a lookup stalled past its watchdog — "
                           "rebuilt this carrier's browser lock so later "
                           "lookups recover; a stuck Chrome process may "
                           "linger until it is quit", slug)
        else:
            sem.release()

# USPS Web Tools auth backoff — owned HERE (was config.py's). Same float,
# main re-imports it for the couple of readers that stayed.
_usps_auth_failed_until: float = 0.0
# ──────────────────────────────────────────────
# Status helpers
# ──────────────────────────────────────────────
def _map_17track_status(status_code: Optional[str]) -> str:
    mapping = {
        "InfoReceived": "Info Received",
        "InTransit": "In Transit",
        "Expired": "Exception",
        "AvailableForPickup": "Available for Pickup",
        "Pickup": "Available for Pickup",
        "OutForDelivery": "Out for Delivery",
        "DeliveryFailure": "Delivery Attempted",
        "Undelivered": "Delivery Attempted",
        "Delivered": "Delivered",
        "Exception": "Exception",
        "NotFound": "Pending",
    }
    return mapping.get(status_code, "Pending")


def _extract_status(item: dict) -> Optional[str]:
    """Tolerant status extraction — 17TRACK's nesting has varied across
    versions. Callers log the raw item when this returns None so the real
    shape can be added."""
    track_info = item.get("track_info") or item.get("track") or {}
    latest = track_info.get("latest_status") or item.get("latest_status") or {}
    return latest.get("status") or item.get("package_status") or item.get("status")


def _extract_carrier(item: dict) -> dict:
    """17TRACK's VERIFIED carrier identity for a number.

    Live-verified shape: track_info.tracking.providers[0].provider holds
    {alias, country, homepage, key, name, tel}. Tolerant like _extract_status
    — older payloads carry no provider block, and a missing identity must
    never break a lookup. This is what fixes "Unknown Carrier" for CN/HK/EU
    posts our format regexes don't know.
    """
    track_info = item.get("track_info") or item.get("track") or {}
    providers = (track_info.get("tracking") or {}).get("providers") or []
    provider: dict = {}
    if providers and isinstance(providers[0], dict):
        provider = providers[0].get("provider") or {}
    name = (provider.get("name") or "").strip()
    if not name:  # alternate/legacy nesting
        alt = track_info.get("carrier_info") or item.get("carrier_info") or {}
        if isinstance(alt, dict):
            name = (alt.get("carrier_name") or alt.get("name") or "").strip()
            provider = provider or alt
    found = {
        "name": name or None,
        "key": provider.get("key"),
        "country": (provider.get("country") or "").strip() or None,
    }
    return {k: v for k, v in found.items() if v}


def _extract_detail(item: dict) -> Optional[str]:
    """Latest event text, so 17TRACK-sourced cards carry the same
    "what happened" line the browser tier provides."""
    track_info = item.get("track_info") or item.get("track") or {}
    event = track_info.get("latest_event") or item.get("latest_event") or {}
    desc = (event.get("description") or "").strip()
    loc = (event.get("location") or "").strip()
    if desc and loc:
        return f"{desc} — {loc}"
    return desc or None


# ── Scan list + delivery window (additive side channel) ────────────────────
# fetch_track_status returns a fixed 4-tuple that many call sites unpack, so the
# richer 17TRACK fields travel beside it instead of through it: the 17TRACK read
# parks them here, and the one place that stores a lookup (refresh) takes them.
# Bounded and in-memory on purpose: it is a hand-off, not a store.
_SCAN_EXTRAS: Dict[str, dict] = {}
_SCAN_EXTRAS_MAX = 200
SCAN_HISTORY_MAX = 15


def _extract_scans(item: dict, limit: int = SCAN_HISTORY_MAX) -> List[dict]:
    """Newest-first scan events: {t: ISO time, d: description, l: place,
    lat/lon only when 17TRACK returned coordinates for that scan}."""
    track_info = item.get("track_info") or item.get("track") or {}
    out: List[dict] = []
    for prov in (track_info.get("tracking") or {}).get("providers") or []:
        for ev in (prov or {}).get("events") or []:
            if not isinstance(ev, dict):
                continue
            desc = (ev.get("description") or "").strip()
            if not desc:
                continue
            scan = {"t": ev.get("time_iso") or ev.get("time_utc") or "",
                    "d": desc[:200], "l": (ev.get("location") or "").strip()[:80]}
            coords = ((ev.get("address") or {}).get("coordinates") or {})
            try:
                lat, lon = float(coords["latitude"]), float(coords["longitude"])
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    scan["lat"], scan["lon"] = lat, lon
            except (KeyError, TypeError, ValueError):
                pass
            out.append(scan)
    out.sort(key=lambda e: e["t"], reverse=True)
    return out[:limit]


def _extract_eta(item: dict) -> Optional[dict]:
    """17TRACK's estimated delivery window, {from, to, source}, when it has one."""
    track_info = item.get("track_info") or item.get("track") or {}
    est = ((track_info.get("time_metrics") or {}).get("estimated_delivery_date") or {})
    if not isinstance(est, dict) or not (est.get("from") or est.get("to")):
        return None
    return {"from": est.get("from"), "to": est.get("to"),
            "source": (est.get("source") or "")[:30]}


def _stash_scan_extras(tracking_id: str, item: dict) -> None:
    extras = {"events": _extract_scans(item), "eta": _extract_eta(item)}
    if len(_SCAN_EXTRAS) >= _SCAN_EXTRAS_MAX:
        _SCAN_EXTRAS.pop(next(iter(_SCAN_EXTRAS)))
    _SCAN_EXTRAS[tracking_id] = extras


def take_scan_extras(tracking_id: str) -> Optional[dict]:
    """Pop what the last 17TRACK read of this number parked (or None)."""
    return _SCAN_EXTRAS.pop(tracking_id, None)


def _17track_headers() -> dict:
    return {"17token": config.track17_api_key, "Content-Type": "application/json"}


class Track17RateLimited(Exception):
    """17TRACK throttled this call (HTTP 429) — not the same as "no data".

    Callers must keep the distinction: being asked to slow down is not a dead end,
    and _fetch_track_status_impl uses it to refuse to hand the number to the paid
    tier. Everything else (transport errors, 5xx, empty answers) stays a generic
    failure.
    """


_RETRY_AFTER_MAX = 60   # a longer Retry-After means "come back later", not "wait"


def _retry_after_seconds(resp, attempt: int) -> float:
    """The server's Retry-After when it offers a usable one, else 2**attempt.

    Capped, and any HTTP-date form falls back to the exponential: a header must
    never be able to park a refresh cycle for hours.
    """
    raw = None
    headers = getattr(resp, "headers", None)
    if headers:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw:
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            seconds = None
        if seconds is not None:
            return max(1.0, min(seconds, _RETRY_AFTER_MAX))
    return float(2 ** attempt)


async def _post_17track(path: str, payload: list, retries: int = 3) -> Optional[dict]:
    """POST to 17TRACK with rate-limiting and small back-off retries.

    Retries transient failures (5xx, timeouts, DNS) and a 429 — for a 429, waiting
    for the server's own Retry-After rather than retrying instantly, which would
    add pressure to the very limit that just fired. Returns parsed JSON, raises the
    last exception so callers can log it, and raises Track17RateLimited when the
    attempts ran out on a 429 so callers can tell throttling from a dead end.

    Only a 2xx body is parsed: a 429 arrives as HTML/plain text, and calling
    .json() on it raised "Extra data: line 1 column 5" — a parse error that hid
    the actual cause for months of logs.
    """
    saw_429 = False
    for attempt in range(retries):
        try:
            await track17_rate_limiter.acquire()
            resp = await http_client().post(
                f"{TRACK17_BASE_URL}{path}",
                headers=_17track_headers(),
                json=payload,
            )
        except Exception as e:
            # Transport-level: no response to reason about.
            if attempt < retries - 1:
                logger.warning("17TRACK %s request failed (attempt %d): %s",
                               path, attempt + 1, e)
                await asyncio.sleep(1 + attempt)
                continue
            raise

        if resp.status_code == 429:
            saw_429 = True
            delay = _retry_after_seconds(resp, attempt)
            logger.warning("17TRACK %s returned HTTP 429 (attempt %d); backing off %ss",
                           path, attempt + 1, delay)
            await asyncio.sleep(delay)
            continue
        if resp.status_code >= 500:
            logger.warning("17TRACK %s returned HTTP %s (attempt %d)",
                           path, resp.status_code, attempt + 1)
            await asyncio.sleep(1 + attempt)
            continue
        if resp.status_code >= 400:
            # Named, not parsed: this is a request/auth problem that will not heal
            # by waiting, and the body is not JSON.
            logger.warning("17TRACK %s returned HTTP %s (non-JSON error body, not retrying)",
                           path, resp.status_code)
            raise RuntimeError(f"17TRACK {path} returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except Exception as e:
            if attempt < retries - 1:
                logger.warning("17TRACK %s returned HTTP %s with an unparsable body "
                               "(attempt %d): %s", path, resp.status_code, attempt + 1, e)
                await asyncio.sleep(1 + attempt)
                continue
            raise
    if saw_429:
        raise Track17RateLimited(f"17TRACK {path}: rate limited after {retries} attempts")
    return None


# ──────────────────────────────────────────────
# Lookup backends
# ──────────────────────────────────────────────
def _already_registered(rejected: list) -> bool:
    """17TRACK signals 'registered already' via error code -18019901 /
    messages like 'has been registered, don't need to repeat registration' /
    'already exists'. Counts as success, not failure."""
    for r in rejected:
        err = r.get("error") or {}
        if not isinstance(err, dict):
            continue
        if err.get("code") == -18019901:
            return True
        msg = str(err.get("message", "")).lower()
        if any(word in msg for word in ("already", "don't need to repeat", "exist")):
            return True
    return False
def _redacted_rejected(rejected: list) -> list:
    """Redact tracking numbers from 17TRACK rejected entries for logs —
    keep error codes/messages (needed for debugging)."""
    out = []
    for r in rejected or []:
        err = r.get("error", {}) or {}
        out.append({
            "number": _sanitize_tracking_id(r.get("number", "")),
            "error_code": err.get("code"),
            "error_message": err.get("message"),
        })
    return out


async def register_17track(tracking_id: str) -> str:
    """Register a number with 17TRACK. Returns one of:
    'registered', 'already', 'blocked', 'failed'.

    'blocked' = -18019823: carrier policy forbids registering numbers with no
    acceptance scan yet. We put the number on a cooldown so the poller
    doesn't hammer the API with doomed register+lookup pairs every cycle.
    A top-level code != 0 (quota exhausted, auth problem) is always logged."""
    if config.track17_api_key == "default_api_key":
        return "failed"
    if storage._registration_on_cooldown(tracking_id):
        return "blocked"

    try:
        data = await _post_17track("/register", [{"number": tracking_id, "auto_detection": True}])
    except Exception as e:
        logger.error("❌ 17TRACK register request failed for %s: %s", _sanitize_tracking_id(tracking_id), e)
        return "failed"
    if data is None:
        return "failed"

    if data.get("code") != 0:
        logger.error("❌ 17TRACK register rejected outright for %s: code=%s rejected=%s", _sanitize_tracking_id(tracking_id), data.get("code"), _redacted_rejected((data.get("data") or {}).get("rejected") or []))
        return "failed"

    payload = data.get("data") or {}
    rejected = payload.get("rejected") or []

    if _already_registered(rejected):
        _register_blocked_until.pop(tracking_id, None)
        _persist_cooldown()
        return "already"  # registered previously — fine

    policy_blocked = any(
        r.get("error", {}).get("code") == -18019823
        or "policy restrictions" in str(r.get("error", {})).lower()
        for r in rejected
    )
    if policy_blocked:
        _register_blocked_until[tracking_id] = time.time() + config.REGISTER_RETRY_INTERVAL
        _persist_cooldown()
        logger.info("⏳ 17TRACK: %s not yet scannable (carrier first-scan policy) — "
                    "will retry registration every %d min", _sanitize_tracking_id(tracking_id), config.REGISTER_RETRY_INTERVAL // 60)
        return "blocked"

    if rejected:
        logger.error("❌ 17TRACK register rejected %s: %s", _sanitize_tracking_id(tracking_id), _redacted_rejected(rejected))
        return "failed"

    logger.info("✅ 17TRACK registered %s", _sanitize_tracking_id(tracking_id))
    _register_blocked_until.pop(tracking_id, None)
    _persist_cooldown()
    return "registered"


async def stop_17track_if_unshared(numbers: List[str],
                                   chat_id: Optional[int] = None) -> int:
    """Stop upstream registration only for numbers no other chat still tracks.

    Deleting a package is a per-chat action; the 17TRACK registration is not.
    Two chats can hold the same number (the store is keyed by chat), and stopping
    it for one of them stopped the other's tracking too: the card stayed but
    never updated again. So the stop is now conditional — the caller's own entry
    is already gone by the time this runs, and any remaining holder keeps the
    registration alive.

    chat_id is the chat that dropped the number, used only for the log (None for
    a purge that spans chats). Returns how many numbers were actually stopped,
    with stop_17track's own contract.
    """
    last_holder = []
    for number in [t for t in numbers if t]:
        holders = [cid for cid, packages in track_sessions.items()
                   if cid != chat_id and number in packages]
        if holders:
            if chat_id is None:
                logger.info(
                    "17TRACK stop skipped for %s: still tracked by %d other chat(s)",
                    _sanitize_tracking_id(number), len(holders))
            else:
                logger.info(
                    "17TRACK stop skipped for %s: still tracked by %d other chat(s) "
                    "(removed from chat=%s)", _sanitize_tracking_id(number),
                    len(holders), storage.mask_chat(chat_id))
            continue
        last_holder.append(number)
    if not last_holder:
        return 0
    return await stop_17track(last_holder)


async def re_register_all_17track(eligible=None):
    """On startup, ensure every stored number is registered. Pre-first-scan
    numbers get 'blocked' here, which pre-populates the cooldown cache so the
    poller stays quiet about them.

    eligible: optional (chat_id, tracking_id) -> bool gate. None keeps the
    original behaviour (register everything). main() passes the production
    routing gate, because a free chat's numbers can never be looked up through
    17TRACK (it is a Pro perk) — registering them spent quota slots on numbers
    that would never use one.
    """
    if config.track17_api_key == "default_api_key" or not track_sessions:
        return
    pairs = {(cid, tid) for cid, pkgs in track_sessions.items() for tid in pkgs}
    if eligible is not None:
        allowed = {(cid, tid) for cid, tid in pairs if eligible(cid, tid)}
        skipped = len(pairs) - len(allowed)
        if skipped:
            logger.info("📡 Startup registration: %d number(s) skipped (free "
                        "chat, or routed to another source)", skipped)
        pairs = allowed
    numbers = sorted({tid for _cid, tid in pairs})
    logger.info("📡 Re-registering %d number(s) with 17TRACK…", len(numbers))
    counts = {"registered": 0, "already": 0, "blocked": 0, "failed": 0}
    for n in numbers:
        counts[await register_17track(n)] += 1
    logger.info("📡 Re-registration done: %d new, %d existing, %d awaiting "
                "first scan, %d failed", counts["registered"], counts["already"],
                counts["blocked"], counts["failed"])


# ── Source routing: 17TRACK is the reserved lane ──────────────────────────
# Policy: 17TRACK serves the numbers NO other source can check — its 3,500+
# carrier breadth, which in practice means CN/HK and other international
# posts. Numbers whose carrier has a configured OFFICIAL API are checked
# there instead, so 17TRACK quota is spent only where it is the only option.
# SOURCE_ROUTING=legacy restores the old "17TRACK first for everything".
SOURCE_ROUTING = (os.getenv("SOURCE_ROUTING") or "matrix").strip().lower()

# Slugs whose 17TRACK access a FREE chat must not get (a free chat gets the
# Pro-required state instead of a paid Ship24 call substituting for the perk).
# UPS/DHL/FedEx are excluded — the browser tier serves them, so they are not a
# perk. NOTE: this set is about the free-chat gate only. Whether quota pressure
# may suspend 17TRACK is decided by _sheddable(), because "has another source"
# depends on the running config (an API key, USPS_BROWSER_TRACK), not on the slug.
_17TRACK_ONLY_SLUGS = {
    "chinapost", "hkpost", "intl", "unknown",
    "ups", "dhl", "royalmail", "canadapost",
}

# Slugs a FREE chat cannot look up at all: 17TRACK is their only real source
# (UPS/DHL/FedEx are excluded — the browser tier serves them: UPS via its
# results page, DHL via the form flow, FedEx via the wtrk SPA's own API call),
# so a lookup for one of these from a free chat returns the Pro-required state
# instead of a paid Ship24 call substituting for the perk.
_PRO_GATED_SLUGS = _17TRACK_ONLY_SLUGS - {"ups", "dhl"}

# Pro chats with PRO_BROWSER_FIRST=1 (the default): the big four are read on the
# carrier's own page in the headless browser BEFORE 17TRACK spends a lookup.
# Measured 2026-09-23: 17TRACK's aggregated feed lagged a delivered UPS parcel by
# ~5 h, and a carrier-direct read cannot lag the carrier. USPS is deliberately
# absent — its official API is the carrier's own feed (faster than scraping,
# unblockable, works pre-first-scan), so the browser slots in AFTER it.
_BROWSER_FIRST_SLUGS = {"ups", "fedex", "dhl"}

# Latest /getquota reading, refreshed hourly by the bot's quota watch. Routing
# reacts to pressure instead of discovering exhaustion mid-cycle.
_quota_state: dict = {}
_QUOTA_STALE_SECONDS = 7200
TRACK17_SOFT_PCT = float(os.getenv("TRACK17_SOFT_PCT", "70"))
TRACK17_HARD_PCT = float(os.getenv("TRACK17_HARD_PCT", "85"))


async def stop_17track(numbers: List[str]) -> int:
    """Stop tracking `numbers` on 17TRACK — the delete-flow deactivation.

    Deleting a package here is only half a deletion: our /register call leaves
    the number registered on 17TRACK's side. /stoptrack is the matching call —
    undocumented on any public page (their doc portal is login-walled); it was
    found by calibrated probing and MEASURED against the live API
    (2026-09-16): it answers the same accepted/rejected envelope as /register,
    and rejects a number that is not registered with error code -18019902
    ("does not register, please register first") — which for a DELETE flow IS
    success: already gone is the goal. Branch on the code, never on prose.

    MEASURED post-conditions (know what this does NOT do): after a successful
    stop, re-registering the same number still answers "has been registered"
    (-18019901) and the quota still counts the slot — /stoptrack stops ACTIVE
    tracking, it does not delete the registration record, and no record-delete
    endpoint exists (every delete-style candidate path answers 404 "invalid
    url"; /retrack exists and re-registers). So a stopped number is dormant at
    the partner, not erased there.

    Never raises and never blocks a local delete: a failed stop only leaves
    the number as it was at the partner. Returns how many of `numbers` were
    confirmed stopped or already gone.
    """
    numbers = [t for t in numbers if t]
    if not config.track17_api_key or not numbers:
        return 0
    try:
        data = await _post_17track("/stoptrack",
                                   [{"number": t} for t in numbers], retries=1)
        payload = (data or {}).get("data") or {}
        confirmed: set = set()
        for entry in payload.get("accepted") or []:
            number = entry.get("number") if isinstance(entry, dict) else entry
            if number:
                confirmed.add(number)
        for entry in payload.get("rejected") or []:
            if not isinstance(entry, dict):
                continue
            error = entry.get("error") or {}
            if error.get("code") == -18019902 and entry.get("number"):
                confirmed.add(entry["number"])   # not registered → already gone
        if len(confirmed) < len(numbers):
            missing = [t for t in numbers if t not in confirmed]
            logger.warning("17TRACK /stoptrack: %d of %d number(s) not confirmed "
                           "stopped (%s)", len(missing), len(numbers),
                           ", ".join(_sanitize_tracking_id(t) for t in missing))
        return len(confirmed)
    except Exception as e:
        logger.warning("17TRACK /stoptrack failed for %d number(s): %s",
                       len(numbers), e)
        return 0


def record_quota(used, total) -> None:
    """Called by the hourly quota watch so routing can see the pressure."""
    global _quota_state
    if used is None or not total:
        return
    _quota_state = {"used": used, "total": total, "at": time.time()}


def quota_pct() -> Optional[float]:
    """Percent of the 17TRACK quota used, or None when unknown/stale."""
    at = _quota_state.get("at")
    if not at or time.time() - at > _QUOTA_STALE_SECONDS:
        return None
    total = _quota_state.get("total") or 0
    if total <= 0:
        return None
    return 100.0 * (_quota_state.get("used") or 0) / total


def quota_dict() -> dict:
    """The current quota reading as an API/UI shape (empty when unknown)."""
    pct = quota_pct()
    return {
        "used": _quota_state.get("used"),
        "total": _quota_state.get("total"),
        "percent": round(pct, 1) if pct is not None else None,
        "checked_at": _quota_state.get("at"),
        "known": pct is not None,
    }


async def fetch_quota() -> Optional[dict]:
    """Ask 17TRACK for this key's used/total quota, and record it for routing.

    Single call site for quota knowledge: the hourly watch and the Mini App's
    international view both come through here.
    """
    if config.track17_api_key == "default_api_key":
        return None
    try:
        resp = await http_client().post(
            f"{TRACK17_BASE_URL}/getquota", headers=_17track_headers(), json=[])
        data = resp.json()
    except Exception as e:
        logger.warning("📊 Quota check failed: %s", e)
        return None
    if data.get("code") != 0:
        logger.warning("📊 Quota check error (code %s)", data.get("code"))
        return None
    q = data.get("data") or {}
    used, total = q.get("quota_used"), q.get("quota_total")
    record_quota(used, total)
    return {"used": used, "total": total}


async def quota_snapshot(max_age: float = 900) -> dict:
    """Quota for the Mini App: reuse a reading younger than max_age, otherwise
    refresh once. It's the bot's own API key, so keep this cheap."""
    age = time.time() - (_quota_state.get("at") or 0)
    if not _quota_state or age > max_age:
        await fetch_quota()
    return quota_dict()


def _sheddable(slug: str) -> bool:
    """Whether quota pressure may suspend 17TRACK for this slug.

    Only a slug that has another tier RIGHT NOW is sheddable: the rule exists to
    spend 17TRACK where it is the last thing between the user and "Unavailable",
    and for USPS that is a property of the CONFIG, not of the slug — an install
    with no official USPS API and the browser tier off (USPS_BROWSER_TRACK is
    opt-in) has nothing else to check a USPS number with.

    UPS/DHL/FedEx stay protected even though the browser can read them: a browser
    read is the tier that gets blocked, so dropping the free retry behind it
    would turn a blocked page into a blank card. That trade is deliberate.
    """
    if slug == "usps":
        return bool(usps_api.enabled() or config.USPS_API_USERID
                    or (config.USPS_BROWSER_TRACK and "usps" in _browser_slugs))
    return slug not in _17TRACK_ONLY_SLUGS


def _use_17track(slug: str, is_pro: Optional[bool] = None,
                 official_empty: bool = False) -> bool:
    """Whether this lookup may spend 17TRACK quota.

    is_pro: True = paying/owner chat (17TRACK allowed per the routing matrix);
    False = free chat — 17TRACK is a Pro perk, so it is never spent, whatever
    the slug; None = tier unknown (legacy callers and tests keep the pre-gate
    behavior).

    official_empty: the official carrier tier for this slug was already asked
    THIS round and answered nothing. The "official API is configured → don't
    spend 17TRACK" rule exists because that API is serving the number; once it
    answers nothing, that reason is gone and 17TRACK is the free fallback that
    keeps the lookup off the paid Ship24 tier (Pro USPS: API v3 → Web Tools →
    browser → 17TRACK).
    """
    if is_pro is False:
        return False
    if SOURCE_ROUTING != "matrix":
        return True  # legacy: 17TRACK first for everything
    if slug in ("chinapost", "hkpost", "intl"):
        # CN/HK/international posts: 17TRACK is our only source — never
        # starve them, even under quota pressure.
        return True
    if slug == "usps" and not official_empty and (usps_api.enabled() or config.USPS_API_USERID):
        # Official USPS source is configured → don't spend 17TRACK (this also
        # keeps US domestic numbers away from a third party entirely).
        return False
    pct = quota_pct()
    if pct is not None and pct >= TRACK17_HARD_PCT and _sheddable(slug):
        # Quota nearly gone: shed the slugs that have another tier to fall back
        # on (official API / direct scrape / browser) and keep the ones where
        # 17TRACK is the only thing standing between the user and "Unavailable".
        logger.warning("17TRACK quota at %.0f%% — suspending %s lookups until it resets", pct, slug)
        return False
    return True


# Slug -> keywords that must appear in 17TRACK's verified carrier name for our
# format-derived deep link to be trusted. A purely numeric format is ambiguous
# (12 digits matched "fedex" while 17TRACK verified the number as UPS), so when
# the verified name disagrees we fall back to the universal 17TRACK link
# rather than sending the user to the wrong carrier's site.
_SLUG_NAME_KEYWORDS = {
    "usps": ("usps", "united states postal"),
    "ups": ("ups", "united parcel"),
    "fedex": ("fedex", "federal express"),
    "dhl": ("dhl",),
    "ontrac": ("ontrac",),
    "royalmail": ("royal mail",),
    "canadapost": ("canada post",),
}


def verify_link(slug: str, verified_name: Optional[str], fallback_url: str, tid: str) -> str:
    """Carrier-specific link only when 17TRACK's verified carrier agrees with
    our format guess; otherwise the universal tracker URL (always correct)."""
    if not verified_name:
        return fallback_url
    keywords = _SLUG_NAME_KEYWORDS.get(slug)
    if keywords is None:
        return fallback_url  # intl/unknown already point at the universal URL
    name = verified_name.lower()
    if any(k in name for k in keywords):
        return fallback_url
    return UNIVERSAL_TRACK_URL.format(tid=tid)


async def _check_17track(tracking_id: str) -> Tuple[Optional[str], Optional[dict]]:
    """Fetch live status from 17TRACK, plus its verified carrier identity and
    latest event text. Returns (status, hint) where hint may be None.

    If the lookup says the number isn't registered, re-register once and retry
    (covers the register-then-query propagation race). If registration is on
    policy cooldown, skip quietly so fallback sources (direct / USPS) get
    their turn."""
    if storage._registration_on_cooldown(tracking_id):
        return None, None

    for attempt in (1, 2):  # 2nd pass = self-heal after a successful register
        try:
            data = await _post_17track("/gettrackinfo", [{"number": tracking_id}], retries=2)
        except Track17RateLimited:
            # Must not be swallowed into "no data": the caller decides whether a
            # throttled free tier may fall through to a BILLED one (it may not).
            raise
        except Exception as e:
            logger.error("17TRACK /gettrackinfo failed for %s: %s", _sanitize_tracking_id(tracking_id), e)
            return None, None
        if data is None:
            return None, None

        if data.get("code") != 0:
            logger.error("❌ 17TRACK error for %s (top-level code %s)", _sanitize_tracking_id(tracking_id),
                         data.get("code"))
            return None, None

        payload = data.get("data") or {}
        accepted = payload.get("accepted") or []
        rejected = payload.get("rejected") or []

        if not accepted:
            if rejected:
                not_registered = any(
                    "register" in str(r.get("error", "")).lower() for r in rejected
                )
                if not_registered and attempt == 1:
                    # Could be propagation lag — try (re)registering once.
                    result = await register_17track(tracking_id)
                    if result in ("registered", "already"):
                        continue
                    # 'blocked' or 'failed' — reason already logged by
                    # register_17track; stay quiet and let fallbacks run.
                    return None, None
                logger.error("17TRACK rejected %s (errors: %s)", _sanitize_tracking_id(tracking_id),
                             [str(r.get("error", ""))[:60] for r in rejected])
            else:
                logger.info("17TRACK: no data yet for %s (carrier may not have scanned yet).", _sanitize_tracking_id(tracking_id))
            return None, None

        item = accepted[0]
        status_code = _extract_status(item)
        if not status_code:
            # Never dump the raw item: it carries the tracking number,
            # addresses and phone fragments. Key names are enough to debug.
            logger.warning("17TRACK: couldn't locate a status field for %s (item keys: %s)",
                           _sanitize_tracking_id(tracking_id), sorted(item.keys()))
            return None, None
        hint = {
            "carrier": _extract_carrier(item),
            "detail": _extract_detail(item),
        }
        try:
            _stash_scan_extras(tracking_id, item)
        except Exception:      # extras are a bonus — never break a lookup
            logger.debug("17TRACK scan extras skipped", exc_info=True)
        return _map_17track_status(status_code), hint

    return None, None


async def _check_ontrac_direct(tracking_id: str) -> Optional[str]:
    """Best-effort scrape of OnTrac's public tracking page (no public API).
    Brittle; may conflict with their ToS — stopgap only."""
    resp = await http_client().get(
        f"https://www.ontrac.com/trackingres.asp?tracking_number={tracking_id}",
        headers={"User-Agent": random_user_agent()},
    )
    text = resp.text.lower()
    if "delivered" in text:
        return "Delivered"
    if "out for delivery" in text:
        return "Out for Delivery"
    if "exception" in text or "delay" in text:
        return "Exception"
    if "in transit" in text or "arrived at" in text:
        return "In Transit"
    if "shipment created" in text or "label created" in text:
        return "Info Received"
    return None


def _map_usps_status(track_summary: str) -> Optional[str]:
    """Map a USPS TrackSummary (HTML) onto our status vocabulary."""
    text = unescape(re.sub(r"<[^>]+>", "", track_summary or "")).lower()
    if not text:
        return None
    if "delivered" in text:
        return "Delivered"
    if "out for delivery" in text:
        return "Out for Delivery"
    if "notice left" in text or "redelivery" in text or "attempted" in text:
        return "Delivery Attempted"
    if "available for pickup" in text:
        return "Available for Pickup"
    if "returned" in text or "exception" in text or "security alert" in text:
        return "Exception"
    if ("label created" in text or "pre-shipment" in text or "awaiting item" in text
            or "label has been prepared" in text or "prepared for your item" in text
            or "status not yet available" in text):
        return "Info Received"
    if "accepted" in text or "departed" in text or "arrived" in text or "arriving" in text or "in transit" in text or "processed through" in text:
        return "In Transit"
    return None


# ── Ship24 (paid per-call fallback) ───────────────────────────────────────
# Ship24 normalizes every courier's wording into a `statusMilestone`, and those
# map one-to-one onto our vocabulary — unlike the USPS page scrape above, which
# has to guess a status out of prose. `statusCode` (optional on their side)
# covers an event whose milestone we don't recognise; `statusCategory` is
# deliberately unused: it names the PHASE an event belongs to, not what
# happened ("delivery" covers delivered, attempted and exception alike).
_SHIP24_MILESTONES = {
    "info_received": "Info Received",
    "in_transit": "In Transit",
    "out_for_delivery": "Out for Delivery",
    "failed_attempt": "Delivery Attempted",
    "available_for_pickup": "Available for Pickup",
    "delivered": "Delivered",
    "exception": "Exception",
    "pending": "Pending",
}
# Refinement vocabulary, consulted only when the milestone is missing/unknown.
_SHIP24_CODES = {
    "data_order_created": "Info Received",
    "transit_handover": "In Transit",
    "delivery_delivered": "Delivered",
    "delivery_out_for_delivery": "Out for Delivery",
    "delivery_attempted": "Delivery Attempted",
    "delivery_available_for_pickup": "Available for Pickup",
    "delivery_exception": "Exception",
    "exception_return": "Exception",
    "exception_lost": "Exception",
    "exception_discarded": "Exception",
}


def _map_ship24_status(milestone: Optional[str],
                       code: Optional[str] = None) -> Optional[str]:
    """Ship24's vocabulary → ours. None when we recognise neither field, so the
    caller keeps 'Unavailable' rather than inventing a status."""
    mapped = _SHIP24_MILESTONES.get((milestone or "").strip().lower())
    if mapped:
        return mapped
    return _SHIP24_CODES.get((code or "").strip().lower())


async def _check_usps_webtools(tracking_id: str) -> Optional[str]:
    """Official USPS Web Tools TrackV2 API. Legacy accounts authenticate with
    USERID + password; newer accounts use a generated Web Tools API Key as the
    USERID with NO password. We try password first, then retry without it on
    an 80040B1A authorization failure so both styles work."""
    if not config.USPS_API_USERID:
        return None
    global _usps_auth_failed_until
    if time.time() < _usps_auth_failed_until:
        return None  # recent 80040B1A — don't hammer USPS with bad credentials

    def build_xml(include_pw: bool) -> str:
        # quoteattr on every interpolated value: USERID/Password are config
        # values and the tracking id is user input — none may break out of (or
        # forge) an attribute in a request that carries credentials
        # (2026-10-04 audit).
        attrs = "USERID=" + quoteattr(config.USPS_API_USERID)
        if include_pw and config.USPS_API_PASSWORD:
            attrs += " Password=" + quoteattr(config.USPS_API_PASSWORD)
        return (f"<TrackRequest {attrs}>"
                f"<TrackID ID={quoteattr(tracking_id)}></TrackID>"
                f"</TrackRequest>")

    async def post_once(xml: str):
        await usps_rate_limiter.acquire()
        # The XML — credentials included — travels in the request BODY, never
        # in the URL: a URL is recorded by the remote server, anything on the
        # path, httpx's INFO request log, and any exception message. Confirmed
        # against the live endpoint: a form-urlencoded POST body is parsed
        # (dummy credentials reached the auth check), a raw text/xml body is
        # not (80040B19 XML Syntax Error).
        resp = await http_client().post(
            config.USPS_WEBTOOLS_URL,
            data={"API": "TrackV2", "XML": xml},
            headers={"User-Agent": random_user_agent()},
        )
        resp.raise_for_status()
        return ET.fromstring(resp.text)

    root = await post_once(build_xml(True))

    def error_info(el) -> Tuple[Optional[str], Optional[str]]:
        # USPS XML uses child elements (<Number>/<Description>), legacy
        # TrackInfo errors use attributes — support both.
        num = el.get("Number") or el.findtext("Number")
        desc = el.get("Description") or el.findtext("Description")
        return num, desc

    num, _desc = error_info(root)
    # A password was rejected → retry API-key style (no password) once.
    if root.tag == "Error" and num == "80040B1A" and config.USPS_API_PASSWORD:
        logger.warning("USPS Web Tools auth rejected (with password) for %s — retrying without it",
                       _sanitize_tracking_id(tracking_id))
        root = await post_once(build_xml(False))
        num, _desc = error_info(root)

    if root.tag == "Error" and num == "80040B1A":
        _usps_auth_failed_until = time.time() + config.USPS_AUTH_BACKOFF
        logger.warning("USPS Web Tools authorization failed for %s — pausing USPS checks for %d min "
                       "(verify USPS_API_USERID in .env)", _sanitize_tracking_id(tracking_id),
                       config.USPS_AUTH_BACKOFF // 60)
        return None

    if root.tag == "Error":
        # The raw error XML is NOT logged: it embeds <TrackID> and recipient
        # detail, so it defeated the masking everywhere else. The USPS error
        # code + description are what actually identify the failure.
        logger.warning("USPS Web Tools error for %s: %s - %s",
                       _sanitize_tracking_id(tracking_id), num, _desc)
        return None

    track_info = root.find("TrackInfo")
    if track_info is None:
        logger.warning("USPS Web Tools: no TrackInfo for %s (no result block)",
                       _sanitize_tracking_id(tracking_id))
        return None
    err = track_info.find("Error")
    if err is not None:
        num, desc = error_info(err)
        logger.warning("USPS Web Tools error for %s: %s - %s",
                       _sanitize_tracking_id(tracking_id), num, desc)
        return None
    return _map_usps_status(track_info.findtext("TrackSummary") or "")
def _browser_allowed(allow_browser: Optional[bool]) -> bool:
    """Whether the USPS browser tier may run for this request. On-demand mode
    restricts it to user-invoked lookups (allow_browser=True)."""
    if config.USPS_BROWSER_ONDEMAND:
        return allow_browser is True
    return True


async def _check_browser(tracking_id: str, slug: str) -> Optional[Tuple[str, str]]:
    """Experimental real-browser lookup for a browser-capable carrier
    (opt-in, USPS_BROWSER_TRACK=1). Runs the blocking Playwright flow in a
    worker thread; only one browser instance at a time, under the
    hang-watchdogged _browser_slot (see carriers._browser_slot)."""
    if not config.USPS_BROWSER_TRACK:
        return None
    try:
        import usps_browser  # noqa: F401  (lazy — playwright optional)
    except ImportError:
        logger.warning("usps_browser.py unavailable — install playwright for USPS_BROWSER_TRACK")
        return None
    async with _browser_slot(slug):
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(usps_browser.get_browser_status,
                                  slug, tracking_id),
                timeout=_BROWSER_BODY_TIMEOUT_S)
        except asyncio.TimeoutError:
            # The slot treats this as a hang and rebuilds the lock; surface
            # the watchdog, not wait_for's bare empty-message TimeoutError.
            raise asyncio.TimeoutError(
                f"{usps_browser.page_label(slug)} browser lookup stalled past "
                f"the {int(_BROWSER_BODY_TIMEOUT_S)}s watchdog") from None
        except Exception as e:
            logger.warning("%s browser lookup failed for %s: %s",
                           usps_browser.page_label(slug),
                           _sanitize_tracking_id(tracking_id), e)
            return None
    if not result:
        return None
    status, detail = result
    logger.info("%s browser: %s → %s (%s)", usps_browser.page_label(slug),
                _sanitize_tracking_id(tracking_id), status, detail[:160])
    return status, detail


async def _try_browser_tier(tracking_id: str, slug: str,
                            allow_browser: Optional[bool],
                            carrier: dict,
                            ) -> Optional[Tuple[str, dict, str, Optional[str]]]:
    """One browser read for a browser-capable slug, tallied like every other tier.
    Returns the lookup tuple on success, None when the carrier answered nothing.

    Shared by the two positions the browser now occupies in the order: the Pro
    headless-first tier for the big four (called with allow_browser=True — there
    the browser IS the number's primary tier) and the standard position below the
    official USPS APIs (allow_browser as the caller passed it). The on-demand
    skip is the caller's decision, so it is logged there and not here.
    """
    label = _browser_labels[slug]
    try:
        result = await _check_browser(tracking_id, slug)
    except Exception as e:
        logger.warning("%s browser lookup failed for %s: %s", label,
                       _sanitize_tracking_id(tracking_id), e)
        result = None
    if not result:
        LOOKUP_FAIL[f"{label} Browser"] += 1
        return None
    status, detail = result
    LOOKUP_OK[f"{label} Browser"] += 1
    return status, carrier, f"{label} Browser", detail


async def _try_17track_tier(tracking_id: str, carrier: dict, slug: str,
                            ) -> Tuple[Optional[Tuple[str, dict, str, Optional[str]]], bool]:
    """The 17TRACK tier as one unit: a /gettrackinfo round plus the identity merge
    that lets its verified carrier override our format guess.

    Returns (lookup, throttled): lookup is the dispatcher's return tuple, or None
    when 17TRACK answered nothing; throttled is True when it answered 429 — the
    caller keeps that for the paid-tier gate, because being throttled is not a
    dead end and must never buy a Ship24 call.

    Extracted from the dispatcher because the tier now has two positions in the
    order (Pro big-4 reads the carrier's own page first, Pro USPS reads 17TRACK
    only after the browser) while both must behave identically.
    """
    if config.track17_api_key == "default_api_key":
        return None, False
    try:
        status, hint = await _check_17track(tracking_id)
    except Track17RateLimited:
        # Throttling is not a dead end: spending a billed call because the free
        # tier asked us to slow down is exactly the leak this guards against.
        logger.warning("17TRACK rate-limited for %s — skipping paid Ship24 "
                       "fallback this round", _sanitize_tracking_id(tracking_id))
        return None, True
    except Exception as e:
        logger.warning("17TRACK lookup failed for %s: %s",
                       _sanitize_tracking_id(tracking_id), e)
        return None, False
    if not status:
        LOOKUP_FAIL["17TRACK"] += 1
        return None, False
    LOOKUP_OK["17TRACK"] += 1
    ident = (hint or {}).get("carrier") or {}
    # 17TRACK's verified identity beats our format guess — it names
    # CN/HK/EU posts we have no pattern for ("Unknown Carrier" fix).
    if ident.get("name"):
        carrier = dict(carrier, name=ident["name"])
        # The verified name can disagree with our format guess (a
        # 12-digit number regexed as FedEx is really UPS) — in that
        # case the deep link must not send users to the wrong carrier.
        carrier["url"] = verify_link(slug, ident["name"], carrier["url"], tracking_id)
    if ident.get("key") is not None:
        carrier["key"] = ident["key"]
    if ident.get("country"):
        carrier["country"] = ident["country"]
    return (status, carrier, "17TRACK", (hint or {}).get("detail")), False


async def fetch_track_status(tracking_id: str,
                             allow_browser: Optional[bool] = None,
                             is_pro: Optional[bool] = None,
                            ) -> Tuple[str, dict, str, Optional[str]]:
    """Public entry: the routing dispatcher plus per-tier tallying.

    Kept as the patchable name (tests and refresh.py target it); the routing
    body lives in _fetch_track_status_impl. A lookup "failed" when the status
    is Unavailable or the source is None (nothing answered) / "Pro" (gated) —
    the same convention LOOKUP_FAIL already uses.
    """
    tier = "pro" if is_pro else ("free" if is_pro is False else "unknown")
    result = await _fetch_track_status_impl(tracking_id, allow_browser, is_pro)
    ok = not (result[0].startswith("Unavailable") or result[2] in ("None", "Pro"))
    LOOKUP_TIER[tier]["ok" if ok else "fail"] += 1
    return result


async def _fetch_track_status_impl(tracking_id: str,
                                   allow_browser: Optional[bool] = None,
                                   is_pro: Optional[bool] = None,
                                  ) -> Tuple[str, dict, str, Optional[str]]:
    """Checks a package's live status. Returns (status, carrier, source, detail)
    — detail is raw provider text (e.g. the USPS browser's "Shipping Label
    Created" row) when the source provides it, else None. allow_browser=True
    forces the USPS browser tier even in on-demand mode. is_pro: True = paying
    or owner chat (17TRACK allowed), False = free chat (17TRACK is a Pro perk —
    and 17TRACK-only slugs return the Pro-required state instead of a paid
    Ship24 call), None = tier unknown (legacy behavior).
    Source = '17TRACK', 'USPS Web Tools', 'USPS', 'Ship24', or the carrier
    name — displayed on the card. Order of preference (SOURCE_ROUTING=matrix):
    1. Browser, for a Pro big-four number (UPS/FedEx/DHL) with
       PRO_BROWSER_FIRST=1: the carrier's own page cannot lag the carrier the way
       an aggregate feed can (measured 2026-09-23: 17TRACK trailed a delivered
       UPS parcel by ~5 h). 17TRACK is the fallback when the page is blocked or
       finds nothing.
    2. USPS, both tiers: API v3 → Web Tools → browser → 17TRACK (Pro only, and
       only once the official tiers came up empty) → Ship24. The official APIs
       are the carrier's own feed — faster than scraping, unblockable, works
       pre-first-scan — so the browser never precedes them.
    3. 17TRACK — the reserved lane for every other slug: CN/HK/international
       posts and any carrier with no other source. Skipped for slugs whose
       official API is configured (see _use_17track), so its quota goes where
       it's needed.
    4. Direct scrape — currently only OnTrac (doesn't aggressively block bots)
    5. Ship24 — the only PAID tier, and strictly a last resort: one billed
       per-call lookup, used only when every tier above came up empty (none
       configured, no data, or a browser scrape that found nothing) and
       SHIP24_API_KEY is set. It turns a dead end into a real status, which is
       exactly the case the user sees as "Unavailable". config.SHIP24_DAILY_MAX
       caps the daily cost; ship24.py owns the pause/budget logic. Two rules bound
       it (2026-09-23): a free chat never reaches it (is_pro is False returns
       Unavailable and lets the free lane recheck), and neither does a round where
       17TRACK answered 429 — being throttled is not a dead end.
    6. "Unavailable" — check the Official tracking link manually.
    """
    carrier = detect_carrier(tracking_id)
    slug = carrier["code"]

    # Set when 17TRACK answers 429: the paid tier below must not be asked to cover
    # a rate limit (see the Ship24 gate).
    rate_limited_17track = False

    # True once the browser tier was asked for THIS number and answered nothing
    # (or could not run at all). Two jobs: it keeps the browser from being asked
    # twice in one round, and it is what lets 17TRACK back a Pro USPS number whose
    # official tiers came up empty — see the override below the browser block.
    browser_attempted = False

    # 2026-09-23: for the big four the carrier's own page outranks the aggregator
    # — 17TRACK's feed lagged a delivered UPS parcel by ~5 h, and a carrier-direct
    # read cannot lag the carrier. Pro only (a free chat never had a 17TRACK tier
    # to reorder) and behind the PRO_BROWSER_FIRST kill switch, which restores the
    # pre-change order exactly.
    reorder = is_pro is True and config.PRO_BROWSER_FIRST
    browser_first = (reorder and slug in _BROWSER_FIRST_SLUGS
                     and slug in _browser_slugs)
    # Pro USPS keeps the official APIs first on purpose (the carrier's own feed:
    # faster than scraping, unblockable, works pre-first-scan). The browser slots
    # in after them and before 17TRACK, so that 17TRACK call is deferred below.
    usps_browser_first = reorder and slug == "usps"

    if browser_first:
        # allow_browser=True on purpose: the browser is this number's PRIMARY tier
        # here, so USPS_BROWSER_ONDEMAND (a knob for keeping an occasional tier
        # quiet) must not skip it — a sweep would otherwise never read a Pro
        # big-4 number at all.
        browser_attempted = True
        result = await _try_browser_tier(tracking_id, slug, True, carrier)
        if result:
            return result

    if not usps_browser_first and _use_17track(slug, is_pro):
        result, throttled = await _try_17track_tier(tracking_id, carrier, slug)
        rate_limited_17track = rate_limited_17track or throttled
        if result:
            return result
    elif config.track17_api_key != "default_api_key" and not usps_browser_first:
        logger.debug("17TRACK skipped for %s (%s): source routing", _sanitize_tracking_id(tracking_id), slug)

    if slug == "usps" and usps_api.enabled():
        # New USPS Tracking API v3 (OAuth2) — first choice: official, fast,
        # works pre-first-scan, no Akamai fights.
        try:
            raw = await usps_api.check_usps(tracking_id, http_client())
        except Exception as e:
            logger.warning("USPS API lookup failed for %s: %s",
                           _sanitize_tracking_id(tracking_id), e)
            raw = None
        if raw:
            text, _cat = raw
            status = _map_usps_status(text)
            if status:
                LOOKUP_OK["USPS API"] += 1
                return status, carrier, "USPS API", text
        LOOKUP_FAIL["USPS API"] += 1

    if slug == "usps" and config.USPS_API_USERID:
        try:
            status = await _check_usps_webtools(tracking_id)
        except Exception as e:
            # type(e).__name__ only: httpx exception text embeds the full
            # request URL, and the same rule already guards usps_browser's
            # tid-bearing URLs (2026-10-04 audit).
            logger.warning("USPS Web Tools lookup failed for %s: %s",
                           _sanitize_tracking_id(tracking_id), type(e).__name__)
            status = None
        if status:
            LOOKUP_OK["USPS Web Tools"] += 1
            return status, carrier, "USPS Web Tools", None
        LOOKUP_FAIL["USPS Web Tools"] += 1

    if slug in _browser_slugs and not browser_attempted:
        if not _browser_allowed(allow_browser):
            logger.info("%s browser skipped for %s (on-demand mode; the next add or command runs it live)",
                        _browser_labels[slug], _sanitize_tracking_id(tracking_id))
        else:
            browser_attempted = True
            result = await _try_browser_tier(tracking_id, slug, allow_browser, carrier)
            if result:
                return result

    if usps_browser_first and browser_attempted and _use_17track(slug, is_pro, official_empty=True):
        # Every free tier this Pro USPS number has has now been asked and the
        # official APIs and the browser all came up empty. The rule that keeps
        # 17TRACK away from US domestic numbers exists because an official API
        # serves them — for this round it doesn't, so 17TRACK is the free
        # fallback that keeps the lookup off the paid tier.
        result, throttled = await _try_17track_tier(tracking_id, carrier, slug)
        rate_limited_17track = rate_limited_17track or throttled
        if result:
            return result

    if slug == "ontrac":
        try:
            status = await _check_ontrac_direct(tracking_id)
        except Exception as e:
            logger.warning("Direct OnTrac lookup failed for %s: %s", _sanitize_tracking_id(tracking_id), e)
            status = None
        if status:
            LOOKUP_OK[carrier["name"]] += 1
            return status, carrier, carrier["name"], None
        LOOKUP_FAIL[carrier["name"]] += 1

    if is_pro is False and slug in _PRO_GATED_SLUGS:
        # 17TRACK-only carrier + free chat: international tracking is the Pro
        # perk, so a paid Ship24 call must not silently substitute for it. The
        # caller keeps the last known status and flags the package needs_pro.
        logger.info("Pro-gated lookup skipped for %s (%s)",
                    _sanitize_tracking_id(tracking_id), slug)
        LOOKUP_FAIL["Pro"] += 1
        return "Unavailable", carrier, "Pro", None

    if is_pro is False and ship24.enabled() and not rate_limited_17track:
        # Free chats must never spend a billed call (user decision 2026-09-23):
        # every free tier above came up empty, so the honest answer is Unavailable
        # plus a recheck on the free fast-retry lane — not ~$0.05 of the owner's
        # money per cycle. Mirrors the _PRO_GATED_SLUGS gate just above, which also
        # tests is_pro is False so that None (legacy/unknown tier) keeps working.
        logger.info("Ship24 skipped for %s: paid tier is Pro-only",
                    _sanitize_tracking_id(tracking_id))

    if ship24.enabled() and is_pro is not False and not rate_limited_17track:
        # Last resort — and the only tier that costs money per lookup: every
        # free source above has already come up empty for this number, so one
        # billed call buys a real status instead of the "Unavailable" the user
        # would otherwise see. ship24.py enforces the daily cap and pauses
        # itself after an account-level failure.
        try:
            raw = await ship24.check_ship24(tracking_id, http_client())
        except Exception as e:
            logger.warning("Ship24 lookup failed for %s: %s", _sanitize_tracking_id(tracking_id), e)
            raw = None
        if raw:
            milestone, code, detail = raw
            status = _map_ship24_status(milestone, code)
            if status:
                LOOKUP_OK["Ship24"] += 1
                logger.info("Ship24 fallback: %s → %s (%s)",
                            _sanitize_tracking_id(tracking_id), status, milestone or code)
                return status, carrier, "Ship24", detail or None
        LOOKUP_FAIL["Ship24"] += 1

    LOOKUP_FAIL["unknown"] += 1
    return "Unavailable", carrier, "None", None


def _sanitize_tracking_id(tracking_id: str) -> str:
    """Mask tracking numbers for logs (privacy): 9400...3527 instead of full.

    The window scales with the length, because a fixed 4+4 window gives most of
    a SHORT number away: a 10-digit DHL id came out as 1234...7890. Every format
    detect_carrier knows is at least 10 characters, so anything shorter is
    garbage rather than a real number and is left as it came in.
    """
    if not tracking_id or len(tracking_id) <= 8:
        return tracking_id
    if len(tracking_id) <= 11:
        return f"{tracking_id[:2]}...{tracking_id[-2:]}"
    return f"{tracking_id[:4]}...{tracking_id[-4:]}"