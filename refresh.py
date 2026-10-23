"""Refresh plumbing for the tracker bot: live refreshes, the background
poller, and 17TRACK quota watching.

Extracted from main.py (Session 7a of EXTRACTION_PLAN.md). main.py
re-imports every name here, so handlers, the Mini App views and tests keep
working. refresh.py reads config / storage / carriers / ui / pro, plus the
live Bot via botstate (main() calls botstate.set_bot() once) and main's
save_data wrapper via configure() — one injection point for the whole app's
single save path. (miniapp.py's views call refresh.save_now() instead of
holding their own hook.)

NOTE: main.py's background_updater rebound the module global
`global LAST_CYCLE_AT`; here those times live as module state in refresh.py
and readers (e.g. /status) must access them as refresh.* attributes.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from html import escape
from typing import Dict, List, Optional, Tuple

from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import storage
import carriers
import delivery_history
import pro
import ui
import usps_api
import botstate

logger = logging.getLogger("tracker")

# Operational state owned HERE (the background loop is the only writer;
# /status is the only other reader — via main's re-export of the names).
START_TS = time.monotonic()
LAST_CYCLE_AT: Optional[str] = None
# What the most recent browser sweep actually did (wall-clock "at" so readers
# don't need to know this module runs on monotonic time).
LAST_SWEEP: Dict[str, float] = {"at": 0.0, "read": 0, "changed": 0}

# Which muted-package changes still notify (deliveries + problems only).
ALERT_STATUSES = {"Delivered", "Exception", "Delivery Attempted"}

# Resurrection notices queued by the lookup path — it is sync and owns no Bot,
# so the loop drains this each tick: (chat_id, tracking_id, name, kind).
_pending_lost_notices: List[Tuple[int, str, str, str]] = []

# Porch Guard pings queued by the same sync path: the moment a package flips to
# Delivered, one loud per-package ping takes the place of the quiet aggregated one
# (the loop subtracts these from the global count — never both for one package).
# Same tuple shape as the lost path: (chat_id, tracking_id, name).
_pending_porch_pings: List[Tuple[int, str, str]] = []
# Screen 3: the 'Out for delivery' alert, one per package, queued at the first
# OFD scan and drained by the loop (which owns the Bot), like the porch pings.
_pending_ofd_alerts: List[Tuple[int, str]] = []

# ── State-adaptive cadence ───────────────────────────────────────────
# How long until a package deserves another lookup, keyed on the status we
# last saw. Terminal states are absent on purpose: a delivered parcel never
# moves again, so re-checking it forever only burned lookups (17TRACK's quota
# counts accepted numbers). Cadence no longer varies by chat alone — the old
# per-chat gate polled every package, delivered ones included, at the tier
# interval for eternity.
CADENCE: Dict[str, Tuple[int, int]] = {  # status → (pro seconds, free seconds)
    # In-transit IS the tier baseline: config owns it because the tier pitch and
    # the Pro-activation message quote these two numbers, and a second copy here
    # would drift from them.
    "In Transit": (config.PRO_REFRESH_INTERVAL, config.FREE_REFRESH_INTERVAL),
    # Delivery day: the OFD fast lane, owned by config for the same reason the
    # baseline above is (the tier pitch quotes this table). This lane is shared
    # with the API tier on purpose: whichever tier took the reading, a parcel on
    # the truck deserves the next look sooner.
    "Out for Delivery": (config.OFD_FAST_PRO, config.OFD_FAST_FREE),
    "Delivery Attempted": (1800, 7200),  # redelivery window: 30 min / 2 h
    "Exception": (180, 180),             # user call 2026-09-17: fast lane, both tiers
    "Info Received": (21600, 43200),     # pre-scan quiet period: quota headroom over faster first-scan detection (user call 2026-09-20; the first acceptance scan still alerts on the next poll; browser numbers still ride the >=5-min sweep gap)
    "Pending": (21600, 43200),           # 6 h / 12 h
    "Unavailable": (60, 600),          # failed lookup — retry, don't freeze
}
# Available for Pickup has no dedicated lane (user call 2026-09-17): it
# falls to the unknown-status default (7200, 14400) but stays in the critical
# set, so quota pressure never stretches a pickup moment.
CADENCE_TERMINAL = {"Delivered"}
# Under quota pressure the non-critical waits stretch; the moments above stay
# sharp, so a delivery day is still noticed on time.
_CADENCE_CRITICAL = {"Out for Delivery", "Delivery Attempted", "Available for Pickup"}
CADENCE_QUOTA_STRETCH = 2.0

# Per-PACKAGE due times (monotonic). float("inf") = terminal, never again.
_due_at: Dict[Tuple[int, str], float] = {}

# Webhook-forced reschedules (monotonic), keyed like _due_at. The webhook URL
# carries no secret, so anyone who knows a tracked number can POST it: without a
# floor, a loop of hits keeps every lookup permanently due and spends the paid
# tiers for nothing. 0 disables the floor.
WEBHOOK_MIN_RESCHEDULE_SECONDS = float(os.getenv("WEBHOOK_MIN_RESCHEDULE_SECONDS", "300"))
_webhook_kicked: Dict[Tuple[int, str], float] = {}

# ── Delivery-claim authority (2026-09-29 false-Delivered incident) ─────────
# A source claimed Delivered for a package the USPS site shows as in transit.
# The downgrade guard below makes Delivered sticky (yesterday's fix: lagging
# sources must never un-deliver), and terminal cadence means nothing ever asks
# again — together, one false terminal claim would be permanent. So witnesses
# are ranked: only an equal-or-better source may retract a Delivered claim,
# and weak-source claims get a bounded verification series instead of
# resting at terminal forever.
_SOURCE_AUTHORITY = {
    "USPS API": 5,          # official API — freshest USPS truth
    "USPS Web Tools": 4,    # official, older interface
    "OnTrac": 3,            # carrier-direct page scrape
    "Ship24": 2,            # paid aggregator
    "17TRACK": 1,           # aggregate — documented laggard
}
_BROWSER_AUTHORITY = 3       # "<carrier> Browser": carrier-direct page read

DELIVERY_VERIFY_SECS = 3600      # spacing between verification attempts
DELIVERY_VERIFY_ATTEMPTS = 3     # then the claim is accepted (stop burning lookups)


def _source_authority(source: Optional[str]) -> int:
    if not source:
        return 0
    if source.endswith("Browser"):
        return _BROWSER_AUTHORITY
    return _SOURCE_AUTHORITY.get(source, 0)


# The banner-classifier fix ("delivered by the expected delivery date" →
# In Transit) went live 2026-09-29 ~13:45 EDT. Delivered claims WRITTEN before
# that moment by weak sources were classified by the buggy classifier and may
# be false — their verification state (verified flag, burned attempts) was
# earned from broken reads and carries no weight. Claims written after the fix
# are trustworthy and stay terminal.
_BANNER_FIX_EPOCH = datetime(2026, 9, 29, 17, 45, tzinfo=timezone.utc).timestamp()


def _reset_poison_era_verification(sessions: Optional[dict] = None) -> int:
    """One-time repair, run at startup: reset the verification state of every
    Delivered package claimed before the banner-classifier fix by a
    non-official source. Idempotent — repaired records carry a post-fix
    last_updated and are skipped on later passes. Returns the count repaired."""
    sessions = storage.track_sessions if sessions is None else sessions
    repaired = 0
    for packages in sessions.values():
        for info in packages.values():
            if info.get("status") != "Delivered":
                continue
            if _source_authority(info.get("delivered_by") or "") >= _SOURCE_AUTHORITY["USPS API"]:
                continue
            claimed_at = _parse_iso_epoch(info.get("last_updated"))
            if claimed_at is None or claimed_at >= _BANNER_FIX_EPOCH:
                continue
            info.pop("delivery_verified", None)
            info.pop("delivery_verify_attempts", None)
            info.pop("delivered_by", None)
            repaired += 1
    if repaired:
        save_now()
        logger.warning("Poison-era repair: verification state reset for %d "
                       "delivered package(s) claimed before the banner-"
                       "classifier fix — they will be re-verified", repaired)
    return repaired


def _delivered_verify_secs(info: dict) -> Optional[int]:
    """Terminal-status override: an UNVERIFIED Delivered claimed by a source
    weaker than the official USPS tiers still gets looked up — a bounded
    verification series so a false terminal claim can be retracted. Verified
    claims and official-source claims stay truly terminal (None).

    Pre-stamping-era records (no recorded claimant — delivered_by unknown)
    get their burned attempts FORGIVEN: those reads came from the buggy
    banner classifier, so the cap was protecting unverified evidence. The
    fixed classifier gets one honest series; once any read stamps a claimant,
    the cap applies normally and the state terminates."""
    if info.get("status") != "Delivered":
        return None
    claimant = _source_authority(info.get("delivered_by") or "")
    if claimant >= _SOURCE_AUTHORITY["USPS API"]:
        return None
    attempts = info.get("delivery_verify_attempts") or 0
    # Pre-stamping-era records (claimant unknown): a "verified" flag or burned
    # attempts from the buggy banner classifier carry no weight — those reads
    # confirmed Delivered from the prediction banner itself. The fixed
    # classifier gets one honest series; once any read stamps a claimant (it
    # happens on the first confirming read), both caps apply normally and the
    # state terminates.
    if claimant > 0 and (attempts >= DELIVERY_VERIFY_ATTEMPTS
                         or info.get("delivery_verified")):
        return None
    return DELIVERY_VERIFY_SECS

# main's save_data wrapper (passes main.DATA_FILE, so tests keep patching
# main.DATA_FILE). Wired once by main.py via configure().
_save_data_fn = None


def configure(save_data=None) -> None:
    """main.py passes its save_data wrapper once at import time."""
    global _save_data_fn
    if save_data is not None:
        _save_data_fn = save_data


def save_now() -> None:
    # The app's single save path for refresh-driven writes. Both this
    # module and miniapp.py's views call it instead of duplicating hooks.
    return _save_data_fn()


def _store_scan_extras(tracking_id: str, info: dict) -> None:
    """Keep the scan list + delivery window the last 17TRACK read parked (see
    carriers.take_scan_extras). Additive: a package without them behaves exactly
    as before, and nothing here touches status or notifications."""
    extras = carriers.take_scan_extras(tracking_id)
    if not extras:
        return
    if extras.get("events"):
        info["events"] = extras["events"]
    if extras.get("eta") is not None:
        info["eta"] = extras["eta"]


def _apply_lookup(info: dict, status: str, carrier: dict, source: str,
                  detail: Optional[str], *, force: bool = False,
                  force_source: Optional[str] = None,
                  changed_only: bool = False,
                  notify: Optional[Tuple[int, str, str]] = None) -> None:
    """Stitch a fresh lookup into a stored package record. A transient
    'Unavailable' never clobbers the last known status unless force=True
    (used when the browser tier returned real data). changed_only=True keeps
    status/last_updated untouched when the status didn't change (background
    cycle semantics).

    notify=(chat_id, tracking_id, name) lets a package that comes back from the
    dead queue its one-time resurrection notice: this function is sync and owns
    no Bot, so the background loop does the sending."""
    old_status = info.get("status")
    old_detail = info.get("detail")
    # Captured before the transition overwrites it: for a record with no
    # added_at this is the last event we know of before the delivery, which is
    # the closest thing to a start time it has (see delivery_history.py).
    old_last_updated = info.get("last_updated")
    info["carrier"] = carrier["code"]
    info["carrier_name"] = carrier["name"]
    info["carrier_url"] = carrier["url"]
    # 17TRACK's verified carrier identity (key/country) is worth keeping: it
    # tells later cycles what this number really is, without re-detecting.
    for field in ("key", "country"):
        if carrier.get(field) is not None:
            info[f"carrier_{field}"] = carrier[field]
    if force_source is not None:
        info["source"] = force_source
    elif source != "None":
        info["source"] = source
    if detail:
        info["detail"] = storage._trim_sentence(detail, 180)
    # A lookup completed for a Delivered package: count the bounded
    # verification attempt unless the claim is already verified. A read that
    # returned nothing usable (Unavailable — page shell, render race) proves
    # nothing either way, so it must not burn the bounded series.
    if (info.get("status") == "Delivered"
            and not info.get("delivery_verified")
            and not status.startswith("Unavailable")):
        info["delivery_verify_attempts"] = min(
            (info.get("delivery_verify_attempts") or 0) + 1,
            DELIVERY_VERIFY_ATTEMPTS)
        if info["delivery_verify_attempts"] >= DELIVERY_VERIFY_ATTEMPTS:
            info["delivery_verified"] = True
    if force or not status.startswith("Unavailable"):
        # A delivery is terminal: a lagging source (17TRACK trails USPS, the
        # USPS page trails its own API) must never un-deliver a package. But
        # the guard ranks witnesses: an equal-or-better source contradicting
        # the claim is a RETRACTION — fresher truth from the same or a
        # stronger tier — not the lagging echo this guard exists for.
        downgrade = (not force and info.get("status") == "Delivered"
                     and status != "Delivered")
        blocked = (downgrade
                   and _source_authority(source) < _source_authority(info.get("delivered_by") or ""))
        if blocked:
            logger.info("Ignoring downgrade Delivered -> %s (source=%s, claimant=%s)",
                        status, source, info.get("delivered_by") or "unknown")
        else:
            if downgrade:
                logger.warning(
                    "Delivery claim retracted: Delivered -> %s (source=%s, claimant=%s)",
                    status, source, info.get("delivered_by") or "unknown")
                info["delivered_by"] = None
                info["delivery_verified"] = False
                info["delivery_verify_attempts"] = 0
                if notify is not None:
                    _pending_lost_notices.append(
                        (notify[0], notify[1], notify[2] or "", "retraction"))
            info["register_blocked"] = False  # real data arrived, so not blocked
            if not changed_only or info.get("status") != status:
                info["status"] = status
                info["last_updated"] = storage.now_iso()
            # A confirming Delivered from a stronger witness upgrades the
            # recorded claimant; the official USPS tier confirming ends
            # verification outright.
            if (info.get("status") == "Delivered"
                    and _source_authority(source) > _source_authority(info.get("delivered_by") or "")):
                info["delivered_by"] = source
                if _source_authority(source) >= _SOURCE_AUTHORITY["USPS API"]:
                    info["delivery_verified"] = True
    # First Out for Delivery scan only: OFD → delivered is the duration the
    # future delivery-window estimates train on, so a repeat scan must not
    # overwrite the original moment.
    if (old_status != "Out for Delivery"
            and info.get("status") == "Out for Delivery"
            and not info.get("out_for_delivery_at")):
        info["out_for_delivery_at"] = storage.now_iso()
        # Screen 3. Same gates the aggregated ping would have passed: a muted
        # package only alerts if OFD is an ALERT status, and the chat's own
        # notification settings (Settings screen) apply.
        if (notify is not None
                and (not info.get("muted") or "Out for Delivery" in ALERT_STATUSES)
                and _chat_allows(notify[0], "Out for Delivery")):
            _pending_ofd_alerts.append((notify[0], notify[1]))
    # A delivery is the one transition worth keeping after the package itself is
    # gone (retention deletes it later), so it is logged for the future
    # delivery-window estimates. Logging only — nothing user-visible reads it,
    # and the Delivered-downgrade guard above is untouched.
    if old_status != "Delivered" and info.get("status") == "Delivered":
        delivery_history.record_delivery(info, start_iso=old_last_updated)
        # Porch Guard: this is the ONLY delivery-detection point (the 17TRACK
        # webhook never trusts a payload status — it only marks the number due).
        # The queue is drained by the loop, which owns the Bot. notify is None on
        # paths with no chat to tell, and then there is nothing to ping.
        #
        # The mute gate lives in _check_one AFTER this call, and ALERT_STATUSES
        # already contains "Delivered", so a muted package still pings: a muted
        # parcel is still a parcel on your porch.
        if notify is not None and info.get("porch_guard", True):
            _pending_porch_pings.append(notify)
    info["last_checked"] = storage.now_iso()  # the poll happened, regardless of outcome
    # Did that poll actually read the carrier? Unavailable means it did not, so
    # the stored status is the last known one rather than a fresh reading.
    info["last_lookup_failed"] = status.startswith("Unavailable")
    # A source of "Pro" means this chat's tier cannot look the number up
    # (17TRACK-only carrier on a free plan): the stored status stays as the
    # last known one and the flag tells the UI why updates stopped. Any real
    # source arriving clears the flag.
    info["needs_pro"] = (source == "Pro")
    # A "carrier event" is a new status OR new detail text. last_updated only
    # moves on status transitions, so an In Transit package with weeks of real
    # movement would otherwise look silent — this stamp is what the lost-package
    # detector measures. Compared against the values captured on entry, so it
    # reflects exactly what this lookup changed.
    real_event = not status.startswith("Unavailable")
    if real_event and (info.get("status") != old_status
                       or info.get("detail") != old_detail):
        info["last_event_at"] = storage.now_iso()
        if info.get("lost") is True:
            # It was never lost: clear the memorial, re-arm the funeral (each
            # genuine death gets its own) and queue the one-time notice. Saved
            # here because a detail-only change leaves the caller's
            # changed_only bookkeeping untouched — without this the flag would
            # live in memory only until some later save.
            info["lost"] = False
            info.pop("lost_at", None)
            info["funeral_shown"] = False
            save_now()
            if notify is not None:
                _queue_lost_notice(*notify, "resurrect")


async def _refresh_browser_batch(chat_id: int, tracking_ids: List[str]) -> Dict[str, dict]:
    """Check browser-capable numbers (USPS, UPS, ...) in ONE session per carrier
    (single profile copy and single context per carrier), then stitch the
    results into the stored records. The name is historical (USPS was first)."""
    # The browser locks (and the hang watchdog around them) are owned by
    # carriers.py — always go through carriers._browser_slot(slug), never
    # touch a semaphore directly: a watchdog rebuild REPLACES that carrier's.
    updates: Dict[str, dict] = {}
    ids = [t for t in tracking_ids if t in storage.track_sessions.get(chat_id, {})]
    if not ids:
        return updates
    try:
        import usps_browser
    except ImportError:
        return updates
    # One session per carrier; the carriers run in PARALLEL — each holds its
    # own slot, so a slow or wedged batch in one carrier no longer delays the
    # others, and no single carrier's request rate changes (its slot depth
    # stays 1 unless USPS_BROWSER_SLOTS_PER_CARRIER says otherwise).
    by_slug: Dict[str, List[str]] = {}
    for t in ids:
        by_slug.setdefault(carriers.detect_carrier(t)["code"], []).append(t)

    async def _one_carrier(slug: str, group: List[str]) -> Dict[str, dict]:
        label = usps_browser.page_label(slug)
        updates_slug: Dict[str, dict] = {}
        try:
            async with carriers._browser_slot(slug):
                # The watchdog scales with session chunks (≤ USPS_BATCH_MAX
                # numbers each; internal budgets are ~120s per chunk), so a
                # large but healthy batch never trips it.
                chunks = max(1, -(-len(group) // usps_browser.USPS_BATCH_MAX))
                raw = await asyncio.wait_for(
                    asyncio.to_thread(usps_browser.get_browser_statuses,
                                      slug, group, 120),
                    timeout=carriers._BROWSER_BODY_TIMEOUT_S * chunks)
        except asyncio.TimeoutError:
            logger.warning("%s browser: sweep batch stalled past its watchdog "
                           "— this carrier is skipped until the next cycle",
                           label)
            return {}
        except Exception as e:
            logger.warning("%s browser batch failed: %s", label, e)
            return {}
        # _apply_lookup is sync (owns no Bot and no await), so concurrent
        # carrier tasks cannot interleave inside it.
        for tid, (status, detail) in raw.items():
            info = storage.track_sessions[chat_id][tid]
            _apply_lookup(info, status, carriers.detect_carrier(tid),
                          f"{label} Browser", detail,
                          force=True, force_source=f"{label} Browser",
                          notify=(chat_id, tid, info.get("name", "Package")))
            carriers.LOOKUP_OK[f"{label} Browser"] += 1
            updates_slug[tid] = info
        carriers.LOOKUP_FAIL[f"{label} Browser"] += max(0, len(group) - len(raw))
        return updates_slug

    for updates_slug in await asyncio.gather(
            *[_one_carrier(slug, group) for slug, group in by_slug.items()]):
        updates.update(updates_slug)
    now = storage.now_iso()
    for tid_ in ids:
        info_ = storage.track_sessions.get(chat_id, {}).get(tid_)
        if info_ is not None:
            info_["last_checked"] = now  # the batch polled every number
            # The batch attempted every number it polled, so the ones it could
            # not read are flagged exactly like a failed single lookup.
            info_["last_lookup_failed"] = tid_ not in updates
    return updates
def cadence_seconds(status: str, is_pro_tier: bool) -> Optional[int]:
    """Seconds until this package should be looked up again, or None when it
    never should (terminal status)."""
    if status in CADENCE_TERMINAL:
        return None
    pro_s, free_s = CADENCE.get(status, (7200, 14400))  # unknown → slow lane
    secs = pro_s if is_pro_tier else free_s
    pct = carriers.quota_pct()
    if pct is not None and pct >= carriers.TRACK17_HARD_PCT and status not in _CADENCE_CRITICAL:
        secs = int(secs * CADENCE_QUOTA_STRETCH)
    return secs


def _app_button_keyboard() -> Optional[InlineKeyboardMarkup]:
    """The only button the bot ever attaches: a bare link to the Mini App. No
    tracking id — inline-keyboard URLs are visible to the whole chat."""
    url = config.MINI_APP_URL
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\U0001f4e6 Open the app", url=url),
    ]])


def build_global_ping(count: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """A notification that carries NO package data — no tracking number, name,
    status or carrier. Chats, groups especially, keep everything forever, so
    the details stay in the Mini App behind signed initData."""
    text = (
        "\U0001f514 <b>Packages updated</b>\n\n"
        + (f"{count} of your packages changed." if count > 1 else "One of your packages changed.")
        + "\n<i>The details stay in the app — nothing about them is shown here.</i>"
    )
    return text, _app_button_keyboard()


def build_retention_notice(count: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Delivered packages were forgotten — like every notification, this one
    names nothing and carries no identifiers."""
    plural = count > 1
    what = (f"{count} delivered packages were" if plural else "A delivered package was")
    left = "them" if plural else "it"
    text = (
        f"\U0001f9f9 <b>Cleaned up</b>\n\n{what} forgotten {config.RETENTION_DAYS} days "
        f"after delivery — that's the promise in /privacy. Nothing about {left} is "
        "kept here anymore."
    )
    return text, _app_button_keyboard()


def _parse_iso_epoch(ts: Optional[str]) -> Optional[float]:
    """Stored ISO timestamp → epoch seconds, or None when unusable. A missing
    or malformed stamp must never be guessed at: retention would delete on a
    made-up date."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:  # legacy stamps without an offset are UTC
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


async def _auto_purge_delivered() -> Dict[int, int]:
    """Lifecycle deletion: forget delivered packages after RETENTION_DAYS.

    A delivered parcel never moves again (the cadence table stops checking it),
    so `last_updated` is the delivery moment and the number has no further use.
    Only exactly 'Delivered' qualifies — Exception/Expired can resume, and those
    are the user's call via /forget."""
    days = config.RETENTION_DAYS
    if days <= 0:
        return {}
    cutoff = time.time() - days * 86400
    purged: Dict[int, int] = {}
    to_stop: List[str] = []
    for chat_id, packages in list(storage.track_sessions.items()):
        for tracking_id, info in list(packages.items()):
            if info.get("status") != "Delivered":
                continue
            delivered_at = _parse_iso_epoch(info.get("last_updated"))
            if delivered_at is None or delivered_at > cutoff:
                continue
            del packages[tracking_id]
            to_stop.append(tracking_id)
            purged[chat_id] = purged.get(chat_id, 0) + 1
    if purged:
        save_now()
        # Spans chats: the guard checks every chat for each number, and the log
        # says how many still hold it (there is no single "removed from" chat).
        await carriers.stop_17track_if_unshared(to_stop)
        logger.info("\U0001f9f9 Retention: forgot %d delivered package(s) older than %d days "
                    "across %d chat(s)", sum(purged.values()), days, len(purged))
    return purged


async def _send_retention_notices(purged: Dict[int, int]) -> None:
    """Tell each affected chat, data-free, that its delivered packages are gone."""
    for chat_id, count in purged.items():
        text, keyboard = build_retention_notice(count)
        try:
            await botstate.bot.send_message(
                chat_id, text, parse_mode=ParseMode.HTML,
                reply_markup=keyboard, disable_web_page_preview=True,
            )
        except Exception as e:
            # A blocked/kicked chat is normal — the purge itself already happened.
            logger.info("Retention notice not delivered: %s", e)


# ── Lost packages: funerals, memorials, resurrections ──────────────────────
# A package with no carrier event for LOST_AFTER_DAYS while still in a
# non-terminal state is declared lost: its card carries the memorial line and
# it gets a one-time funeral. That notice NAMES the package, so it is the only
# loop notice carrying package data — hence ephemeral, like the cards.
def _lost_eligible(info: dict) -> bool:
    """Is this silence a disappearance? Only a package that should be moving
    counts. A delivered one arrived; one at a pickup point is waiting on the
    human; a Pro-paused or pre-first-scan one is quiet by design; and a failing
    lookup is our problem, not the package's."""
    if info.get("status") in CADENCE_TERMINAL:          # Delivered — it arrived
        return False
    if info.get("status") == "Available for Pickup":    # waiting on the human
        return False
    if info.get("register_blocked"):                    # pre-first-scan labels sit quiet for weeks
        return False
    if info.get("needs_pro") is True:                   # frozen by tier, not lost
        return False
    if info.get("last_lookup_failed") is True:          # our lookup failing != lost
        return False
    return True


def _queue_lost_notice(chat_id: int, tracking_id: str, name: str, kind: str) -> None:
    """Queue a one-time notice from the sync lookup path (see _apply_lookup)."""
    _pending_lost_notices.append((chat_id, tracking_id, name, kind))


async def _send_lost_notice(chat_id: int, tracking_id: str, name: str, kind: str) -> None:
    """One funeral or resurrection notice — ephemeral by construction, because
    it names the package and must not linger in chat history (a group's most of
    all)."""
    text = escape(ui.lost_notice(tracking_id, name, kind))
    try:
        await ui.send_ephemeral_chat(chat_id, text, parse_mode=ParseMode.HTML,
                                     disable_web_page_preview=True)
    except Exception as e:
        # A blocked/kicked chat is normal — the flagging itself already happened.
        logger.info("Lost notice (%s) not delivered: %s", kind, e)


async def _send_funeral_notices(flagged: Dict[int, List[Tuple[str, str]]]) -> None:
    """Deliver the funeral for each newly-lost package, then mark it told.
    funeral_shown is written AFTER the send is queued: a crash in between
    re-sends a funeral (mild) rather than never sending one (silent)."""
    for chat_id, packages in flagged.items():
        for tracking_id, name in packages:
            await _send_lost_notice(chat_id, tracking_id, name, "funeral")
            info = storage.track_sessions.get(chat_id, {}).get(tracking_id)
            if info is not None:
                info["funeral_shown"] = True
    if flagged:
        save_now()


async def _flush_lost_notices() -> None:
    """Drain resurrection notices queued by the lookup path (same tick)."""
    while _pending_lost_notices:
        chat_id, tracking_id, name, kind = _pending_lost_notices.pop(0)
        await _send_lost_notice(chat_id, tracking_id, name, kind)


# ── Porch Guard ──────────────────────────────────────────────────────────
# The one notification that is about a single package instead of a chat-wide
# count: a delivery is a "go outside now" moment, and "1 of your packages
# changed" is no use when the parcel is on the step in the rain. Same ephemeral
# contract as the funeral notice — it names the package, so it must not become
# permanent history (a group's most of all). At most two messages ever: the ping,
# then one nudge if nobody taps the ✅. Acking or the per-package toggle ends it.
PORCH_PING = ("📦 <b>{name}</b> was just delivered — it's at your door right "
              "now. Go grab it!")
PORCH_REPING = ("📦 <b>{name}</b> is still sitting out there — just making sure "
                "it made it inside.")
PORCH_ACKED = "✅ <b>{name}</b> is safe inside. Nice."


def _porch_chat_counts() -> Dict[int, int]:
    """Queued porch pings per chat, so the aggregated count can drop them."""
    counts: Dict[int, int] = {}
    for chat_id, _tracking_id, _name in _pending_porch_pings:
        counts[chat_id] = counts.get(chat_id, 0) + 1
    for chat_id, _tracking_id in _pending_ofd_alerts:
        counts[chat_id] = counts.get(chat_id, 0) + 1
    return counts


def _porch_ack_keyboard(tracking_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Got it — it's inside",
                             callback_data=f"pg_ack:{tracking_id}")]])


async def _send_porch_ping(chat_id: int, tracking_id: str, text: str) -> None:
    """One ephemeral porch ping, carrying its own ack button."""
    try:
        await ui.send_ephemeral_chat(
            chat_id, text, parse_mode=ParseMode.HTML,
            reply_markup=_porch_ack_keyboard(tracking_id),
            disable_web_page_preview=True)
    except Exception as e:
        # A blocked/kicked chat is normal — the stamps are written either way.
        logger.info("Porch ping not delivered: %s", e)


async def _flush_porch_pings() -> None:
    """Drain deliveries queued this tick: stamp the window, then send.

    The stamps are written BEFORE the send, so a chat that can't receive the ping
    is not re-pinged forever: they are what makes this "one ping plus one nudge,
    never again". A package that was switched off (or removed) while it sat in the
    queue is dropped instead.
    """
    while _pending_porch_pings:
        chat_id, tracking_id, name = _pending_porch_pings.pop(0)
        info = storage.track_sessions.get(chat_id, {}).get(tracking_id)
        if info is None or not info.get("porch_guard", True):
            continue
        # 0 disables the nudge entirely (same convention as EPHEMERAL_TTL).
        info["porch_reping_at"] = (time.time() + config.PORCH_REPING_SECONDS
                                   if config.PORCH_REPING_SECONDS > 0 else None)
        info["porch_acked"] = False
        info["porch_repinged"] = False
        save_now()
        await _send_porch_ping(chat_id, tracking_id,
                               PORCH_PING.format(name=escape(name)))


async def _flush_ofd_alerts() -> None:
    """Send the queued 'Out for delivery' alerts (screen 3). Falls back silently
    to nothing on a render error: the quiet count ping was already dropped for
    these, so a failure is logged, never raised into the poll loop."""
    while _pending_ofd_alerts:
        chat_id, tracking_id = _pending_ofd_alerts.pop(0)
        info = storage.track_sessions.get(chat_id, {}).get(tracking_id)
        if info is None:
            continue
        try:
            import screens
            await ui.send_ephemeral_chat(
                chat_id, screens.delivery_alert_text(tracking_id, info),
                parse_mode=ParseMode.HTML,
                reply_markup=screens.delivery_alert_keyboard(tracking_id, info),
                disable_web_page_preview=True)
        except Exception as e:
            # A blocked/kicked chat is normal.
            logger.info("Out-for-delivery alert not delivered: %s", e)


async def _flush_porch_repings() -> None:
    """One nudge for a delivered package whose ping was never acked.

    Scans the whole store once per tick — it is a dozen-odd packages, and the
    alternative (a second due-time table to keep consistent with removals) would
    be more state than it saves. Nothing here loops: porch_repinged latches on the
    send, so a porch that stays unacked is silent after this.
    """
    now = time.time()
    nudged = False
    for chat_id, packages in list(storage.track_sessions.items()):
        for tracking_id, info in list(packages.items()):
            if not info.get("porch_guard", True):
                continue
            if info.get("status") != "Delivered":
                continue
            due_at = info.get("porch_reping_at")
            if not due_at or now < due_at:
                continue
            if info.get("porch_acked") or info.get("porch_repinged"):
                continue
            info["porch_repinged"] = True
            nudged = True
            await _send_porch_ping(
                chat_id, tracking_id,
                PORCH_REPING.format(name=escape(info.get("name", "Package"))))
    if nudged:
        save_now()


async def _flag_lost_packages() -> Dict[int, List[Tuple[str, str]]]:
    """Declare packages lost after LOST_AFTER_DAYS of carrier silence, and
    return the ones that still owe a funeral: {chat_id: [(tracking_id, name)]}.
    Mirrors _auto_purge_delivered: hourly, mutate the records, save once.

    The first cycle after deploy flags everything already silent that long in
    one burst — expected, not a bug."""
    days = config.LOST_AFTER_DAYS
    if days <= 0:
        return {}
    cutoff = time.time() - days * 86400
    flagged: Dict[int, List[Tuple[str, str]]] = {}
    chats: set = set()
    newly = 0
    for chat_id, packages in list(storage.track_sessions.items()):
        for tracking_id, info in list(packages.items()):
            if info.get("lost") is True:                # already declared
                continue
            if not _lost_eligible(info):
                continue
            # Records predating last_event_at fall back to last_updated, the
            # closest thing they have to a last-seen stamp. An unreadable stamp
            # is never guessed at (same rule as retention): the package stays
            # unflagged rather than funeralized on a made-up date.
            last_event = _parse_iso_epoch(info.get("last_event_at")
                                          or info.get("last_updated"))
            if last_event is None or last_event > cutoff:
                continue
            info["lost"] = True
            info["lost_at"] = storage.now_iso()
            newly += 1
            chats.add(chat_id)
            if info.get("funeral_shown") is not True:
                flagged.setdefault(chat_id, []).append(
                    (tracking_id, info.get("name", "Package")))
    if newly:
        save_now()
        logger.info("\U0001faa6 Lost: declared %d package(s) lost after %d days "
                    "with no carrier event (%d chat(s) affected)",
                    newly, days, len(chats))
    return flagged


async def _send_global_ping(chat_id: int, count: int) -> None:
    """One data-free ping per chat per cycle — the loop's only notification."""
    text, keyboard = build_global_ping(count)
    await botstate.bot.send_message(
        chat_id, text, parse_mode=ParseMode.HTML,
        reply_markup=keyboard, disable_web_page_preview=True,
    )


async def _log_17track_quota() -> None:
    """Hourly quota watch: log 17TRACK usage and warn before we hit the wall."""
    if config.track17_api_key == "default_api_key":
        return
    # One call site for quota knowledge (it also records the reading that
    # source routing and the Mini App's international view read).
    q_raw = await carriers.fetch_quota()
    if not q_raw:
        return
    used, total = q_raw.get("used"), q_raw.get("total")
    if used is None or total is None:
        logger.info("📊 17TRACK quota: unknown (key has no quota info)")
        return
    used, total = int(used), int(total)
    pct = (used / total * 100.0) if total else 0.0
    n_pkg = sum(len(v) for v in storage.track_sessions.values())
    logger.info("📊 17TRACK quota: %d/%d used (%.0f%%) — %d packages tracked",
                used, total, pct, n_pkg)
    if pct >= 90:
        logger.warning("⚠️ 17TRACK quota CRITICAL (%.0f%%) — raise refresh intervals "
                       "or add direct carrier APIs", pct)
    elif pct >= 70:
        logger.warning("⚠️ 17TRACK quota high (%.0f%%) — monitor closely", pct)
def _chat_allows(chat_id: int, status: Optional[str]) -> bool:
    """The chat's own notification settings (screens.py, Settings). Defaults are
    today's behaviour, so a chat that never opens Settings is unaffected; any
    failure here fails OPEN — a settings bug must never silence a delivery."""
    try:
        import screens
        return screens.notify_allowed(chat_id, status)
    except Exception:
        logger.debug("notification settings unreadable", exc_info=True)
        return True


async def _check_one(chat_id: int, tracking_id: str, info: dict,
                     is_pro_tier: bool, semaphore: asyncio.Semaphore) -> Optional[int]:
    """Look one package up, reschedule it from its fresh state, then return its
    chat_id when the change deserves a ping (None = no ping)."""
    old_status = info.get("status", "Pending")
    async with semaphore:
        new_status, carrier, source, detail = await carriers.fetch_track_status(
            tracking_id, is_pro=is_pro_tier)

    changed = (not new_status.startswith("Unavailable")) and new_status != old_status
    _apply_lookup(info, new_status, carrier, source, detail, changed_only=True,
                  notify=(chat_id, tracking_id, info.get("name", "Package")))
    _store_scan_extras(tracking_id, info)
    if new_status.startswith("Unavailable"):
        logger.info("ℹ %s (%s): Unavailable — 17TRACK registration cooldown=%s, source=%s",
                    carriers._sanitize_tracking_id(tracking_id), carrier["name"], storage._registration_on_cooldown(tracking_id), source)
    # Schedule this package's next look from its fresh state (terminal
    # statuses land on inf — never checked again). A failed lookup is the one
    # exception: the stored status is the last KNOWN one (a reading already
    # acted on), not this poll's outcome, so scheduling from it would bury the
    # retry hours out and the (60, 600) retry lane would never fire.
    schedule_from = ("Unavailable" if new_status.startswith("Unavailable")
                     else info.get("status", "Pending"))
    secs = cadence_seconds(schedule_from, is_pro_tier)
    if secs is None:
        # Terminal — unless the delivery claim is unverified and weakly
        # sourced: then a bounded verification series runs instead of inf.
        secs = _delivered_verify_secs(info)
    _due_at[(chat_id, tracking_id)] = float("inf") if secs is None else time.monotonic() + secs
    if changed:
        save_now()
        if info.get("muted") and new_status not in ALERT_STATUSES:
            logger.info("ℹ %s muted — status changed to %s, skipped notification", carriers._sanitize_tracking_id(tracking_id), new_status)
            return None
        if not _chat_allows(chat_id, new_status):
            logger.info("ℹ %s — %s ping skipped by the chat's notification settings", carriers._sanitize_tracking_id(tracking_id), new_status)
            return None
        return chat_id
    return None


# One browser session per sweep at most: a session per package is what trips
# the Akamai flag, and each session is a real Chrome plus a USPS page load.
# None, not 0.0: time.monotonic() is process-relative on this platform (the
# same reason START_TS works for uptime), so 0.0 would read as just-swept and
# delay the first sweep by the whole gap after every restart.
_last_sweep_at: Optional[float] = None


def mark_due_now(tracking_id: str) -> Dict[str, int]:
    """Webhook hook: pull the next lookup for this number forward to the next
    cycle tick (≤60 s) in every chat that tracks it.

    Both consumers read the same _due_at map, so routing is automatic: normal
    numbers are refreshed by the loop, browser-only numbers (excluded from the
    loop) are picked up by the sweep later in the same cycle. After the lookup
    the cadence is re-derived, so a Delivered number goes back to terminal.

    Returns {"matched", "skipped_throttled", "skipped_terminal"} counts. The
    webhook is unauthenticated, so a hit must not be able to make work: a
    delivered package has nothing left to learn, and a number looked up seconds
    ago is not worth paying a source for again.
    """
    now = time.monotonic()
    counts = {"matched": 0, "skipped_throttled": 0, "skipped_terminal": 0}
    for chat_id, packages in storage.track_sessions.items():
        info = packages.get(tracking_id)
        if info is None:
            continue
        if info.get("status") in CADENCE_TERMINAL:
            counts["skipped_terminal"] += 1
            continue
        key = (chat_id, tracking_id)
        last = _webhook_kicked.get(key)
        # `last is None`, not 0.0: monotonic() is time-since-boot on macOS, so a
        # freshly booted host would look "just kicked" for the first interval.
        if last is not None and now - last < WEBHOOK_MIN_RESCHEDULE_SECONDS:
            counts["skipped_throttled"] += 1
            continue
        _due_at[key] = 0.0
        _webhook_kicked[key] = now
        counts["matched"] += 1
    return counts


def _browser_only(tracking_id: str, info: dict, chat_id: int = None) -> bool:
    """True for numbers the headless sweep owns (the generic loop must skip).

    Two cases:
    * USPS with no official API configured: 17TRACK refuses pre-first-scan
      labels, so the loop's attempt can only return Unavailable AND it
      reschedules the package hours out, hiding it from the sweep that runs
      later in the same cycle.
    * UPS in a FREE chat: 17TRACK is a Pro perk, so the sweep's free browser
      read is the number's primary tier. Pro chats keep their numbers in the
      loop (which reads them on the carrier's own page first since 2026-09-23 —
      PRO_BROWSER_FIRST — and only then 17TRACK)."""
    if not config.USPS_BROWSER_TRACK:
        return False
    slug = carriers.detect_carrier(tracking_id)["code"]
    if slug == "usps":
        return not (bool(config.USPS_API_USERID) or usps_api.enabled())
    if slug == "ups" or slug == "fedex":
        # Free chat: the browser is the number's primary tier (UPS via its
        # results page, FedEx via the wtrk SPA's own API call). Pro chat: the
        # sweep only retries numbers the loop's lookup failed.
        return chat_id is not None and not pro.is_pro(chat_id)
    return False

async def _sweep_browser(now_mono: float) -> Dict[int, int]:
    """Read the browser-only numbers (USPS pre-scan labels) plus UPS/FedEx
    numbers whose last lookup failed, one session per chat per carrier.

    With no official USPS API configured, the browser is the only tier that can
    see a pre-first-scan label - but it is on-demand by default, so the loop
    attempt can only return Unavailable while _apply_lookup keeps the last known
    status, which left those numbers frozen. The sweep reuses the same due
    schedule as the loop, so nothing is polled more often than before: the
    attempt is merely spent on a tier that can answer. UPS and FedEx numbers
    join when 17TRACK failed their last lookup (or in free chats, where the
    browser IS the primary tier): the sweep gives them a free browser read on
    the gap schedule instead of leaving them to paid calls. Returns pings.
    """
    if not config.USPS_BROWSER_TRACK:
        logger.info("sweep: USPS_BROWSER_TRACK is off — sweep disabled")
        return {}
    try:
        import usps_browser
    except ImportError:
        return {}
    official = bool(config.USPS_API_USERID) or usps_api.enabled()
    slugs = usps_browser.browser_slugs()
    pings = {}
    swept = 0
    changed_total = 0
    for chat_id, packages in list(storage.track_sessions.items()):
        due = []
        for tid, info in packages.items():
            slug = carriers.detect_carrier(tid)["code"]
            # Temporary diagnostic (2026-09-29 false-Delivered follow-up): the
            # verification pass was admitted by the pure logic but the live
            # sweep skipped it silently — this line records which gate.
            logger.info(
                "sweep gate %s: status=%r slug=%s throttled=%s verify_secs=%r due_at=%r",
                carriers._sanitize_tracking_id(tid), info.get("status"), slug,
                usps_browser.is_throttled(slug),
                _delivered_verify_secs(info), _due_at.get((chat_id, tid)))
            if slug not in slugs:
                continue
            if usps_browser.is_throttled(slug):
                continue
            if info.get("status") in CADENCE_TERMINAL:
                if _delivered_verify_secs(info) is None:
                    continue
                # Temporary diagnostic (2026-09-29 false-Delivered follow-up):
                # the verification pass was admitted by the pure logic but the
                # live sweep skipped it — this line records which gate, why.
                logger.info("sweep verification admit: %s (due_at=%r, attempts=%r)",
                            carriers._sanitize_tracking_id(tid),
                            _due_at.get((chat_id, tid)),
                            info.get("delivery_verify_attempts"))
            if now_mono < _due_at.get((chat_id, tid), 0.0):
                continue
            if slug == "usps":
                # Browser-only: the loop cannot serve these at all.
                if official and not info.get("register_blocked"):
                    continue
                due.append(tid)
            elif slug in ("ups", "fedex"):
                # Free chat: the browser is the number's primary tier. Pro
                # chat: the sweep only retries numbers the loop's lookup failed.
                if not pro.is_pro(chat_id) or info.get("last_lookup_failed"):
                    due.append(tid)
        if not due:
            continue
        # Delivery-day priority: OFD numbers go first so the cap below can never
        # cut them, and a throttled carrier still spends its one session on the
        # parcels closest to arriving. list.sort is stable, so the non-OFD
        # numbers keep the order they were collected in.
        due.sort(key=lambda tid: packages[tid].get("status") != "Out for Delivery")
        due = due[:config.USPS_SWEEP_MAX]
        before = {tid: packages[tid].get("status") for tid in due}
        updates = await _refresh_browser_batch(chat_id, due)
        for tid in due:
            secs = cadence_seconds(packages[tid].get("status", "Pending"),
                                   pro.is_pro(chat_id))
            if secs is None:
                secs = _delivered_verify_secs(packages[tid])
            _due_at[(chat_id, tid)] = (float("inf") if secs is None
                                       else now_mono + secs)
        changed = sum(1 for tid in due if tid in updates
                      and updates[tid].get("status") != before[tid])
        notifiable = sum(1 for tid in due if tid in updates
                         and updates[tid].get("status") != before[tid]
                         and _chat_allows(chat_id, updates[tid].get("status")))
        swept += len(due)
        changed_total += changed
        if changed:
            save_now()
            if notifiable:
                pings[chat_id] = notifiable
    if swept:
        logger.info("browser sweep: read %d number(s), %d changed", swept, changed_total)
    LAST_SWEEP.update(at=time.time(), read=swept, changed=changed_total)
    # Persist for run.sh --status/--check: the dashboard samples this file,
    # because a fresh `import refresh` in a subprocess cannot see this
    # process's in-memory LAST_SWEEP (it always reported "never" after any
    # restart, even while sweeps were running). Counters only — no user data.
    try:
        # Atomic + 0600 like every other state file: counters only, but the
        # dashboard subprocess reads it, and 0644 invites an accidental edit.
        tmp = config.SWEEP_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"at": LAST_SWEEP["at"], "read": swept, "changed": changed_total}))
        os.chmod(tmp, 0o600)
        tmp.replace(config.SWEEP_STATE_FILE)
    except OSError as e:
        logger.warning("Could not write sweep state: %s", e)
    return pings


# ── Browser canary ────────────────────────────────────────────────
# Once a day, read one live package per browser carrier through the tier and
# alert the operator on a health CHANGE. The sweep's failure modes are silent
# by design (no_data/aborted just skip the tick), so a carrier whose widget
# breaks (DHL 2026-10-04: their SPA config 404s, results never render) or
# whose edge starts refusing would go unnoticed until a user asks where their
# update went. The canary is the tripwire. It probes each carrier's most
# recently checked live (non-terminal) package — a number that already HAS
# data, so a healthy tier answers ok — and masks it in every alert. Disabled
# slugs are probed too (one read a day): that is how a recovery gets noticed
# and re-enabling becomes worthwhile. No new env knob — the interval is a
# constant, and an extra probe after a restart is harmless.
_CANARY_INTERVAL_S = 24 * 3600
_CANARY_BAD = frozenset({"no_data", "parse_failed", "throttled", "blocked",
                         "timeout", "aborted", "error"})
_last_canary_at: Optional[float] = None
_canary_healthy: Dict[str, bool] = {}
_canary_tasks = set()


def _canary_due(now_mono: float) -> bool:
    return (_last_canary_at is None
            or now_mono - _last_canary_at >= _CANARY_INTERVAL_S)


def _canary_tid_for(slug: str) -> Optional[str]:
    """The most recently checked live (non-terminal) package for this slug —
    a number that already carries data, so a healthy tier must answer ok."""
    best, best_at = None, ""
    for pkgs in storage.track_sessions.values():
        for tid, info in pkgs.items():
            if carriers.detect_carrier(tid)["code"] != slug:
                continue
            if info.get("status") in CADENCE_TERMINAL:
                continue
            stamp = str(info.get("last_checked") or "")
            if stamp >= best_at:        # ISO timestamps compare chronologically
                best, best_at = tid, stamp
    return best


async def _run_browser_canary() -> None:
    """Probe one live package per browser carrier; alert on state change."""
    try:
        import usps_browser
    except ImportError:
        return
    results: Dict[str, Tuple[str, str]] = {}   # slug -> (tid, outcome)
    for slug in usps_browser._CARRIER_PAGES:
        tid = _canary_tid_for(slug)
        if tid is None:
            continue                    # nothing live to probe it with
        try:
            await asyncio.to_thread(
                usps_browser.get_browser_statuses, slug, [tid], 90)
            outcome = usps_browser.get_last_outcome(tid) or "error"
        except Exception as e:
            logger.warning("canary: %s probe failed: %s", slug, e)
            outcome = "error"
        results[slug] = (tid, outcome)
        await asyncio.sleep(0)          # yield between carriers
    for slug, (tid, outcome) in sorted(results.items()):
        healthy = outcome not in _CANARY_BAD
        prev = _canary_healthy.get(slug)
        _canary_healthy[slug] = healthy
        # First observation alerts only when unhealthy; afterwards, on change.
        if prev is healthy or (prev is None and healthy):
            continue
        label = usps_browser.page_label(slug)
        mask = carriers._sanitize_tracking_id(tid)
        if healthy:
            text = (f"🌐 Browser canary: {label} is healthy again — {mask} "
                    f"read back ok.")
        else:
            text = (f"🌐 Browser canary: {label} lookups look BROKEN "
                    f"(outcome: {outcome} on {mask}) — sweep updates for it "
                    f"may be silently missing. Try that carrier in a normal "
                    f"browser to see whether it's the site or the bot.")
        await botstate.notify_admins(text, key=f"canary-{slug}")


def _schedule_browser_canary(now_mono: float) -> None:
    """Due check + task spawn, kept out of the tick so the canary never
    delays the minute loop."""
    global _last_canary_at
    if not _canary_due(now_mono):
        return
    _last_canary_at = now_mono
    task = asyncio.create_task(_run_browser_canary())
    _canary_tasks.add(task)
    task.add_done_callback(_canary_tasks.discard)


def _readable(tid: str, info: dict, chat_id: Optional[int]) -> bool:
    """Whether the loop/ETA should consider this package: not terminal (the
    cadence stops there) and not browser-owned (the sweep reads those)."""
    if info.get("status") in CADENCE_TERMINAL:
        return False
    return not _browser_only(tid, info, chat_id)


def pending_due() -> int:
    """/status: how many packages the next cycle will look at."""
    now_mono = time.monotonic()
    return sum(
        1
        for chat_id, packages in storage.track_sessions.items()
        for tid, info in packages.items()
        if _readable(tid, info, chat_id)
        and now_mono >= _due_at.get((chat_id, tid), 0.0)
    )


def next_due_in(chat_id: Optional[int] = None,
                tracking_id: Optional[str] = None) -> Optional[float]:
    """/status + Mini App ETA: seconds until the next lookup, or None when
    nothing is scheduled. With (chat_id, tracking_id), the ETA for one package."""
    now_mono = time.monotonic()
    if chat_id is not None and tracking_id is not None:
        due = _due_at.get((chat_id, tracking_id))
        if due is None or due == float("inf"):
            return None
        return round(max(0.0, due - now_mono), 1)
    best: Optional[float] = None
    for cid, packages in storage.track_sessions.items():
        for tid, info in packages.items():
            if not _readable(tid, info, cid):
                continue
            due = _due_at.get((cid, tid))
            if due is None or due == float("inf"):
                continue   # never scheduled = picked up next cycle anyway
            wait = max(0.0, due - now_mono)
            if best is None or wait < best:
                best = round(wait, 1)
    return best

async def background_updater():
    logger.info("\U0001f504 Background updater started")
    semaphore = asyncio.Semaphore(config.REFRESH_CONCURRENCY)
    _cycle = 0
    # One-time repair for the 2026-09-29 false-Delivered incident: poisoned
    # verification state must be cleared before the due gates run, or the
    # poisoned records stay terminal forever (see _reset_poison_era_verification).
    _reset_poison_era_verification()
    while True:
        try:
            now_mono = time.monotonic()
            tasks = []
            for chat_id, packages in list(storage.track_sessions.items()):
                is_pro_tier = pro.is_pro(chat_id)
                for tracking_id, info in list(packages.items()):
                    # Per-package due gate: state-adaptive, so delivered and
                    # pre-scan numbers stop consuming lookups.
                    if now_mono < _due_at.get((chat_id, tracking_id), 0.0):
                        continue
                    # A Delivered package resting at inf with an UNVERIFIED
                    # weak-source claim gets a bounded verification series —
                    # the 2026-09-29 false-Delivered incident froze forever
                    # because terminal cadence meant nothing ever asked again.
                    # Browser-owned numbers are served by the sweep (which
                    # now admits verification-pending terminal packages).
                    if (_due_at.get((chat_id, tracking_id)) == float("inf")
                            and _delivered_verify_secs(info) is not None):
                        _due_at[(chat_id, tracking_id)] = (now_mono
                                                           + DELIVERY_VERIFY_SECS)
                        continue
                    # Browser-owned numbers belong to the sweep later in this
                    # cycle (see _browser_only): polling them here burns an
                    # attempt and hides them from the sweep.
                    if _browser_only(tracking_id, info, chat_id):
                        continue
                    tasks.append(_check_one(chat_id, tracking_id, info, is_pro_tier, semaphore))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                pings: Dict[int, int] = {}
                for r in results:
                    if isinstance(r, Exception):
                        logger.warning("Background lookup error: %r", r)
                    elif r is not None:
                        pings[r] = pings.get(r, 0) + 1
                # One loud porch ping replaces the quiet aggregated one for that
                # package: a delivered parcel deserves "go grab it", not a count.
                for porch_chat_id, n in _porch_chat_counts().items():
                    pings[porch_chat_id] = pings.get(porch_chat_id, 0) - n
                for porch_chat_id, count in list(pings.items()):
                    if count <= 0:
                        del pings[porch_chat_id]
                for ping_chat_id, count in pings.items():
                    try:
                        await _send_global_ping(ping_chat_id, count)
                    except Exception as e:
                        logger.warning("Background notification error: %s", e)

                # ...then the porches: this tick's pings, and the one nudge for
                # anything still unacked from an earlier tick.
                await _flush_porch_pings()
                await _flush_ofd_alerts()
                await _flush_porch_repings()

            # Resurrection notices queued by the lookup path (background or
            # on-demand): a returning package should say so on this tick, not
            # whenever the hourly lifecycle pass happens to run.
            await _flush_lost_notices()

            # Drop schedule entries for packages that are gone (removed/purged).
            live = {(cid, tid) for cid, pkgs in storage.track_sessions.items() for tid in pkgs}
            for key in [k for k in _due_at if k not in live]:
                del _due_at[key]
            # Same for the webhook throttle stamps: a re-added number must not
            # inherit an old stamp (and the map must not grow forever).
            for key in [k for k in _webhook_kicked if k not in live]:
                del _webhook_kicked[key]

            global LAST_CYCLE_AT
            LAST_CYCLE_AT = storage.now_iso()
            _cycle += 1
            if _cycle % 60 == 0:  # hourly — the loop ticks once a minute
                await _log_17track_quota()
                await _send_retention_notices(await _auto_purge_delivered())
                # Lost-package lifecycle: declare the silent ones lost, then
                # deliver the funerals (ephemeral — they name the package).
                await _send_funeral_notices(await _flag_lost_packages())
            # Headless USPS sweep: the only tier that can move a USPS number
            # when no official API is configured. Gap-limited so a flagged
            # browser cannot be hammered on every tick.
            global _last_sweep_at
            if (_last_sweep_at is None or
                    now_mono - _last_sweep_at >= config.USPS_SWEEP_MIN_GAP):
                _last_sweep_at = now_mono
                try:
                    sweep_pings = await _sweep_browser(now_mono)
                    # Same substitution as the loop path: a delivery the sweep
                    # spotted is a porch ping, not part of a count.
                    for porch_chat_id, n in _porch_chat_counts().items():
                        sweep_pings[porch_chat_id] = sweep_pings.get(porch_chat_id, 0) - n
                    for porch_chat_id, count in list(sweep_pings.items()):
                        if count <= 0:
                            del sweep_pings[porch_chat_id]
                    for cid, count in sweep_pings.items():
                        await _send_global_ping(cid, count)
                    await _flush_porch_pings()
                    await _flush_ofd_alerts()
                except Exception as e:
                    logger.warning("USPS browser sweep error: %s", e)
            # Daily browser canary (own task — a slow probe never delays
            # this tick): alerts the operator when a carrier's browser tier
            # degrades or recovers.
            _schedule_browser_canary(now_mono)
            await asyncio.sleep(60)  # tick once a minute; per-package due times gate work
        except Exception as e:
            logger.error("Background cycle error: %s", e)
            # One alert per 30 minutes however often this fires: a poller stuck in
            # a failing loop is exactly when a chat flood is least useful.
            await botstate.notify_admins(
                "⚠️ The tracking poller hit an unexpected error and kept going — "
                "statuses may stop updating until it recovers. See tracker.log.",
                key="poll-cycle")
            await asyncio.sleep(60)


async def _register_and_fetch(tracking_id: str, is_pro: bool = None,
                              chat_id: int = None) -> Tuple[dict, str]:
    """Shared add flow for /track and the Mini App: register with 17TRACK (Pro
    chats only — free chats' numbers never spend a quota slot), then fetch live
    status. Returns (info_fields, reg_result). A 'blocked' result means the
    carrier refuses pre-first-scan labels — show an honest Pending card with
    the note instead of a meaningless 'Unavailable'. For free chats the
    reg_result is 'skipped' and the browser/Ship24 tiers serve the number.

    chat_id is the member this add is for, when the caller knows it: a successful
    international registration is then counted against their period allowance
    (pro.record_intl_registration). The shared quota slot is spent right here, so
    this is the one place that can count it honestly — a later removal must not
    hand it back. FREE chats are counted too, at add time: they never register
    with 17TRACK (that is the Pro perk), but their international allowance
    counts the add itself.
    """
    if is_pro is False:
        reg_result = "skipped"
        detail = None
        if chat_id is not None and carriers.is_international_number(tracking_id):
            pro.record_intl_registration(chat_id, tracking_id)
    else:
        reg_result = await carriers.register_17track(tracking_id)
        detail = None
        if chat_id is not None and reg_result == "registered":
            pro.record_intl_registration(chat_id, tracking_id)
    if reg_result == "blocked":
        carrier = carriers.detect_carrier(tracking_id)
        # 17TRACK will not register a pre-first-scan label, but the USPS page
        # still says "Shipping Label Created / Awaiting Item" - and the
        # browser tier is the only way to read it, so try that before settling
        # for a bare Pending. The waiting-for-first-scan note stays true either
        # way, which is why register_blocked is set in both cases below.
        status, carrier2, source, detail = await carriers.fetch_track_status(
            tracking_id, allow_browser=True, is_pro=is_pro)
        if status.startswith("Unavailable"):
            status, source, detail = "Pending", carrier["name"], None
        else:
            carrier = carrier2 or carrier
        register_blocked = True
    else:
        status, carrier, source, detail = await carriers.fetch_track_status(
            tracking_id, allow_browser=True, is_pro=is_pro)
        register_blocked = False
    info = {
        "status": status,
        "last_lookup_failed": status.startswith("Unavailable"),
        "needs_pro": source == "Pro",
        "source": source,
        "register_blocked": register_blocked,
        "carrier": carrier["code"],
        "carrier_name": carrier["name"],
        "carrier_url": carrier["url"],
        "last_updated": storage.now_iso(),
        "last_checked": storage.now_iso(),
        # When the user added it: the start of the transit measurement that
        # delivery history records (see delivery_history.py).
        "added_at": storage.now_iso(),
        # Incoming by default; only a Pro chat can set outgoing (it is
        # display-only metadata — tracking and quota logic ignore it).
        "direction": "incoming",
        "muted": False,
        # Porch Guard: on by default — the feature's whole point is being armed
        # before the parcel arrives, and a legacy package with no key reads as on
        # through info.get("porch_guard", True). Per package; toggled on the card
        # (bot) or in the app.
        "porch_guard": True,
    }
    if detail:
        info["detail"] = storage._trim_sentence(detail, 180)
    return info, reg_result