"""Pro tier for the tracker bot: limits, refresh quotas, Solana payments.

Extracted from main.py (Session 4 of EXTRACTION_PLAN.md). main.py re-imports
every name used elsewhere, so handlers and the Mini App views keep working.
pro.py reads config / storage / carriers only — plus the live Bot via
botstate (main() calls botstate.set_bot() once; without it the notifier
paths stay silent, exactly as the old `if bot is not None` guards behaved
pre-startup).

NOTE on load_pro_state: main.py's version rebound the module globals
(`global _pro_users, ...; _pro_users = {...}`); here it mutates the dicts
in place (clear+update) so main's re-imported references stay the SAME
objects — same fix as storage.load_data in Session 2.
"""
import asyncio
import hashlib
import json
import logging
import random
import secrets
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple

from aiogram.enums import ParseMode

import config
import storage
import carriers
import botstate
import solana_tx

logger = logging.getLogger("tracker")

_pro_users: Dict[int, float] = {}          # chat_id → expiry unix timestamp
# 17TRACK registrations consumed in the member's CURRENT Pro billing period:
# chat_id → {"period_end": <expiry when counted>, "count": n}. The slot is spent
# at register time, so this — not the stored cards — is what the international
# allowance counts; a changed period_end means the period rolled over.
_intl_registrations: Dict[int, Dict] = {}
_pending_payments: Dict[str, Dict] = {}    # payment_id → {chat_id, amount, amount_micros, created}
# Expired invoices stay matchable for a grace window: someone who pays late
# still gets what they paid for. Keyed like the pending pool.
_expired_payments: Dict[str, Dict] = {}
# amount_micros → when it was consumed. A used amount must not be re-issued
# while a duplicate or late transfer of it could still arrive.
_used_amounts: Dict[int, float] = {}
# chat_id → the expiry timestamp the last-day warning was already sent for.
# Keyed by the expiry (a renewal arms a fresh warning) and persisted in
# pro_expiry_warned.json, so no restart can re-send it — the operator ruled:
# remind once, never again after a restart. Dead entries prune on save/load.
_expiry_warned: Dict[int, float] = {}
# Set by every mutation of the pools above, so the monitor writes the file once
# per cycle instead of on every change (and never writes when nothing moved).
_payments_dirty = False


def _mark_payments_dirty() -> None:
    global _payments_dirty
    _payments_dirty = True


class _OrderedSigs:
    """Insertion-ordered set of signatures, with a bounded window.

    The monitor's dedup table has to mean "the N most recent signatures", and a
    plain set cannot express that: iteration is hash order, so trimming it with
    list(...)[-300:] kept an arbitrary subset — sig000 alongside sig599 — rather
    than the newest. Only .add(), `in` and len() are ever performed on this
    object, so a plain set still works everywhere else, including the tests,
    which pass set() straight into the functions that share it.

    `trim` drops from the OLDEST end, so the survivors are genuinely the most
    recent. The constructor takes an ALREADY-ORDERED iterable (oldest first);
    materializing it once matters, because a generator would be drained by
    set() and leave the deque empty — trim would then silently do nothing.
    """

    def __init__(self, ordered: Iterable[str] = ()):
        items = list(ordered)
        self._s: Set[str] = set(items)
        self._q: Deque[str] = deque(items)

    def add(self, sig: str) -> None:
        if sig not in self._s:
            self._s.add(sig)
            self._q.append(sig)

    def __contains__(self, sig: str) -> bool:
        return sig in self._s

    def __len__(self) -> int:
        return len(self._s)

    def trim(self, keep: int) -> None:
        while len(self._q) > keep:
            self._s.discard(self._q.popleft())


# The monitor's dedup window: keep at most this many signatures, dropping the
# oldest. 500 is the trigger, 300 the survivor count, as before.
_SIG_WINDOW_TRIM_AT = 500
_SIG_WINDOW_KEEP = 300


def _seed_known_sigs() -> "_OrderedSigs":
    """The starting dedup table, oldest receipt first.

    _credited_signatures() stays the seam that produces the SET (it is the
    durable exactly-once guard, and tests substitute it), but a set carries no
    order — so the receipts are consulted only to SORT what that call returned.
    Reading _receipts directly instead would duplicate the guard's logic and
    silently ignore the substitution, which is how a seeded table would stop
    honouring a receipt trail it never asked for.
    """
    stamps: Dict[str, float] = {}
    for entry in _receipts:
        sig = str(entry.get("sig") or "")
        if sig:
            stamps.setdefault(sig, float(entry.get("ts") or 0.0))
    ordered = sorted(_credited_signatures(), key=lambda s: stamps.get(s, 0.0))
    return _OrderedSigs(ordered)


def load_pro_state() -> None:
    try:
        if config.PRO_USERS_FILE.exists():
            text, was_enc = storage._read_protected(config.PRO_USERS_FILE)
            if text is not None:
                data = json.loads(text)
                loaded = {int(k): float(v) for k, v in data.items()}
                _pro_users.clear()
                _pro_users.update(loaded)
                if storage._ENCRYPTION_AVAILABLE and not was_enc and not storage.READ_ONLY:
                    save_pro_users()
                    if storage._is_ciphertext(config.PRO_USERS_FILE):
                        logger.info("MIGRATED: pro_users.json is now encrypted at rest")
                    else:
                        logger.warning("pro_users.json still PLAINTEXT at rest - "
                                       "check storage._get_fernet()")
    except (Exception, SystemExit) as e:
        # SystemExit = undecryptable ciphertext (wrong token); _read_protected
        # already preserved the file as .locked. Pro timers rebuild on
        # their own — never refuse startup over these files.
        logger.warning("Could not load pro_users.json: %s", e)
    load_pending_payments()
    load_receipts()
    load_intl_registrations()
    load_deposit_addrs()
    load_expiry_warnings()


def load_expiry_warnings() -> None:
    """Restore the once-per-subscription warning ledger.

    A missing file is normal (nothing warned yet); entries for subscriptions
    that already ended prune themselves here, so the file stays bounded
    without a separate sweeper.
    """
    try:
        if not config.PRO_EXPIRY_WARNED_FILE.exists():
            return
        text, _was_enc = storage._read_protected(config.PRO_EXPIRY_WARNED_FILE)
        if text is None:
            return
        data = json.loads(text)
        now = time.time()
        _expiry_warned.clear()
        _expiry_warned.update({int(k): float(v) for k, v in data.items()
                               if float(v) > now})
    except (Exception, SystemExit) as e:
        # SystemExit = undecryptable ciphertext; like the other pro state
        # files, a lost ledger may cost one repeat warning, never a startup.
        logger.warning("Could not load pro_expiry_warned.json: %s", e)


def save_expiry_warnings() -> None:
    """Persist the ledger; entries for finished subscriptions drop off here."""
    try:
        now = time.time()
        live = {str(k): v for k, v in _expiry_warned.items() if v > now}
        if len(live) != len(_expiry_warned):
            _expiry_warned.clear()
            _expiry_warned.update({int(k): v for k, v in live.items()})
        storage._write_protected(config.PRO_EXPIRY_WARNED_FILE,
                                 json.dumps(live, indent=1))
    except Exception as e:
        logger.warning("Could not save pro_expiry_warned.json: %s", e)


def save_pending_payments() -> None:
    """Persist the invoice pools so a restart cannot orphan a live payment.

    These pools were memory-only, which meant a restart silently discarded every
    open invoice: the buyer paid the exact amount they were shown, the monitor
    found the transfer, matched it against an empty pool and credited nobody —
    no Pro, no receipt. The amount is the payer's identifier, so losing the pool
    loses the payment. Chat IDs live in here, so the file is encrypted at rest
    like every other state file.
    """
    global _payments_dirty
    try:
        storage._write_protected(config.PENDING_PAYMENTS_FILE, json.dumps({
            "pending": dict(_pending_payments),
            "expired": dict(_expired_payments),
            "used": {str(k): v for k, v in _used_amounts.items()},
        }, indent=1))
        _payments_dirty = False
    except Exception as e:
        logger.warning("Could not save pending_payments.json: %s", e)


def load_pending_payments() -> None:
    """Restore invoices on startup; a missing file is normal (nothing was open)."""
    try:
        if not config.PENDING_PAYMENTS_FILE.exists():
            return
        text, was_enc = storage._read_protected(config.PENDING_PAYMENTS_FILE)
        if text is None:
            return
        data = json.loads(text)
        _pending_payments.clear()
        _pending_payments.update(data.get("pending") or {})
        _expired_payments.clear()
        _expired_payments.update(data.get("expired") or {})
        _used_amounts.clear()
        _used_amounts.update({int(k): float(v)
                              for k, v in (data.get("used") or {}).items()})
        if storage._ENCRYPTION_AVAILABLE and not was_enc and not storage.READ_ONLY:
            save_pending_payments()
            if storage._is_ciphertext(config.PENDING_PAYMENTS_FILE):
                logger.info("MIGRATED: pending_payments.json is now encrypted at rest")
        if _pending_payments or _expired_payments:
            logger.info("Solana invoices restored: %d open, %d in the grace pool",
                        len(_pending_payments), len(_expired_payments))
    except (Exception, SystemExit) as e:
        logger.warning("Could not load pending_payments.json: %s", e)


# ─── Deposit-address registry (chat → address) ───────────────────────────────
#
# Encrypted at rest like the invoice pools: it maps a chat ID to the address that
# identified it, i.e. it de-pseudonymises the chain for exactly the chats that paid.
# The address itself is pseudonymous on-chain; only this file links the two. Kept as
# a file rather than just re-derived because it is the auditable list of addresses
# this bot ever handed out.

_deposit_addrs: Dict[int, dict] = {}


def save_deposit_addrs() -> None:
    try:
        storage._write_protected(config.DEPOSIT_ADDRS_FILE, json.dumps(
            {str(k): v for k, v in _deposit_addrs.items()}, indent=1))
    except Exception as e:
        logger.warning("Could not save deposit_addrs.json: %s", e)


def load_deposit_addrs() -> None:
    """Restore the registry; a missing file is normal (nothing handed out yet)."""
    try:
        if not config.DEPOSIT_ADDRS_FILE.exists():
            return
        text, was_enc = storage._read_protected(config.DEPOSIT_ADDRS_FILE)
        if text is None:
            return
        data = json.loads(text)
        _deposit_addrs.clear()
        _deposit_addrs.update({int(k): v for k, v in data.items()})
        if storage._ENCRYPTION_AVAILABLE and not was_enc and not storage.READ_ONLY:
            save_deposit_addrs()
            if storage._is_ciphertext(config.DEPOSIT_ADDRS_FILE):
                logger.info("MIGRATED: deposit_addrs.json is now encrypted at rest")
        if _deposit_addrs:
            logger.info("Deposit addresses restored: %d chat(s)", len(_deposit_addrs))
    except (Exception, SystemExit) as e:
        # Same as the other stores: preserved as .locked, never refuse startup.
        logger.warning("Could not load deposit_addrs.json: %s", e)


def register_deposit_address(chat_id: int, address: str) -> None:
    """Record the address handed to a chat — once per chat, written immediately.

    Bookkeeping rather than state that can be lost (the derivation is
    deterministic), but it is the only list of addresses this bot ever gave out, so
    it is written when the address is first handed over.
    """
    record = _deposit_addrs.get(chat_id)
    if record and record.get("address") == address:
        return
    _deposit_addrs[chat_id] = {"address": address, "created": time.time()}
    save_deposit_addrs()


def register_invoice(payment_id: str, invoice: dict) -> None:
    """Record a new invoice and write it out before the buyer can pay it."""
    _pending_payments[payment_id] = invoice
    save_pending_payments()


def mask_invoice_id(payment_id: str) -> str:
    """A log-safe handle for an invoice id.

    New ids are random, so showing their own prefix is harmless. A legacy
    "chat-timestamp" id is the opposite: its prefix IS the payer's chat id, so
    those are reduced to a stable, non-reversible tag instead. Support still gets
    one handle per invoice and no log line ever names a chat.
    """
    pid = str(payment_id or "")
    if pid.startswith("inv-"):
        return pid[:12] + "…"
    return "legacy-" + hashlib.sha256(pid.encode()).hexdigest()[:8]


# ── Receipts ─────────────────────────────────────────────────────────────
# One entry per Pro activation: what was paid, which chain transaction, when it
# expires. This is the accounting trail (and what --notify re-sends), and every
# entry names the paying chat — so it lives in an encrypted file rather than the
# plaintext pro_receipts.log it started as. Legacy logs are migrated on first
# read and then deleted: leaving plaintext chat IDs on disk would defeat the
# whole point.
_receipts: List[Dict] = []


def _parse_legacy_receipt(line: str) -> Optional[Dict]:
    """One line of the old CSV log → an entry, or None if it is unreadable.

    Legacy format: epoch,chat_id,amount,token,sig,payment_id,expiry
    """
    parts = line.split(",")
    if len(parts) < 7:
        return None
    try:
        return {"ts": float(parts[0]), "chat_id": int(parts[1]),
                "amount": float(parts[2]), "token": parts[3], "sig": parts[4],
                "payment_id": parts[5], "expiry": float(parts[6])}
    except ValueError:
        return None


def _fold_legacy_receipts() -> int:
    """Absorb a legacy plaintext log into the trail, then delete it.

    Two reasons this is not a one-shot migration: the old code appended to
    pro_receipts.log, so a bot that has not been restarted yet can recreate it
    after the migration — and a plaintext file naming paying chats must never be
    left on disk. Returns how many entries were new.
    """
    legacy = Path("pro_receipts.log")
    if not legacy.exists():
        return 0
    known = {(e.get("ts"), e.get("sig")) for e in _receipts}
    added = 0
    for line in legacy.read_text().splitlines():
        entry = _parse_legacy_receipt(line)
        if entry and (entry["ts"], entry["sig"]) not in known:
            _receipts.append(entry)
            added += 1
    if added:
        save_receipts()
    legacy.unlink(missing_ok=True)
    logger.info("MIGRATED: %d receipt(s) folded in from the legacy plaintext log; "
                "the file was removed", added)
    return added


def load_receipts() -> None:
    try:
        if config.PRO_RECEIPTS_FILE.exists():
            text, was_enc = storage._read_protected(config.PRO_RECEIPTS_FILE)
            if text is not None:
                _receipts.clear()
                _receipts.extend(json.loads(text))
                if storage._ENCRYPTION_AVAILABLE and not was_enc and not storage.READ_ONLY:
                    save_receipts()
                    if storage._is_ciphertext(config.PRO_RECEIPTS_FILE):
                        logger.info("MIGRATED: pro_receipts.json is now encrypted at rest")
            _fold_legacy_receipts()
            return
        if not Path("pro_receipts.log").exists():
            return
        save_receipts()
        if storage._is_ciphertext(config.PRO_RECEIPTS_FILE):
            _fold_legacy_receipts()
    except (Exception, SystemExit) as e:
        logger.warning("Could not load pro_receipts.json: %s", e)


def save_receipts() -> None:
    try:
        storage._write_protected(config.PRO_RECEIPTS_FILE,
                                 json.dumps(_receipts, indent=1))
    except Exception as e:
        logger.warning("Could not save pro_receipts.json: %s", e)


def append_receipt(entry: Dict) -> None:
    """Record one activation.

    Read-modify-write on purpose: an append-only log cannot be encrypted as a
    whole document, and activations are rare enough that rewriting a few KB
    costs nothing.
    """
    _receipts.append(entry)
    save_receipts()


def last_receipt_for(chat_id: int) -> Optional[Dict]:
    """The most recent receipt for a chat — what a re-sent receipt must quote."""
    for entry in reversed(_receipts):
        if entry.get("chat_id") == chat_id:
            return entry
    return None


def payment_was_credited(payment_id: str) -> bool:
    """Whether this invoice was already credited — for what the page should SAY.

    Display only; the invoice pools stay the authority on matching. Why it exists: a
    credited invoice leaves both pools, so a payer who paid and then reopened the link
    was told "this payment link has expired" — reading a completed payment as failure
    (2026-09-25). The receipt trail already records the payment_id at credit time (the
    monitor, the deposit scan and payment_reconcile all go through _activate_pro), so
    this answers the question without new state and survives a restart.
    """
    if not payment_id:
        return False
    return any(entry.get("payment_id") == payment_id for entry in _receipts)


def _credited_signatures() -> Set[str]:
    """Every signature the receipt trail says has already been credited.

    Why this is load-bearing: the monitor's `known` set is per-process, so a restart
    empties it. The main flow is still safe — it parks the transfer amount in
    `_used_amounts`, which is persisted — but the deposit path deliberately does NOT
    (rounded amounts repeat across chats), so the receipts are the only durable
    exactly-once record for it. Without this, a transfer still inside an address's
    newest page was credited again after any restart: a free renewal for whoever
    opened the sheet next. Measured before the fix: 1 credit, restart, 1 more.
    """
    return {str(entry.get("sig")) for entry in _receipts if entry.get("sig")}

def save_pro_users() -> None:
    try:
        storage._write_protected(config.PRO_USERS_FILE, json.dumps(
            {str(k): v for k, v in _pro_users.items()}, indent=1))
    except Exception as e:
        logger.warning("Could not save pro_users.json: %s", e)


def reconcile_receipts() -> List[Tuple[int, float]]:
    """Payment proven, subscription missing: make the store match the receipts.

    _activate_pro writes the receipt trail FIRST and pro_users SECOND — the
    receipt is the durable proof, the store is a cache of it. A crash or a
    swallowed save between the two left a paying customer silently on the free
    tier after a restart (2026-10-04 audit), and nothing re-derived Pro from
    the receipts. One direction only, deliberately conservative: a receipt can
    only ever EXTEND what the store claims (manual grants, comp time and an
    operator rollback must survive), and the expiry comes from the receipt's
    own record, not recomputed. Only expiries still in the future are restored:
    a lapsed receipt is history, not state — resurrecting it would put the
    former member back into the store on every startup, and the expiry task
    would re-send the "expired" notice each time (2026-10-04 19:01 restart
    re-notified all three lapsed chats exactly that way).

    Called from the bot's startup path — NOT from load_pro_state, which
    run.sh --status also imports and which must stay read-only. Returns the
    (chat_id, expiry) corrections it applied; empty means nothing needed
    fixing."""
    fixes: List[Tuple[int, float]] = []
    for entry in _receipts:
        chat_id = entry.get("chat_id")
        expiry = entry.get("expiry")
        if not chat_id or not expiry:
            continue
        chat_id, expiry = int(chat_id), float(expiry)
        if expiry <= time.time():
            # A receipt whose term already ended is history, not state: re-adding
            # it would resurrect a lapsed member on every startup, and the expiry
            # task would then re-send "Your Pro subscription expired" each time.
            continue
        if expiry > _pro_users.get(chat_id, 0.0):
            _pro_users[chat_id] = expiry
            fixes.append((chat_id, expiry))
    if fixes:
        save_pro_users()
        for chat_id, expiry in fixes:
            logger.warning(
                "PRO reconciled from the receipt trail: chat=%s restored until %s",
                storage.mask_chat(chat_id),
                datetime.fromtimestamp(expiry, tz=timezone.utc).strftime("%b %d, %Y"))
    return fixes

def save_intl_registrations() -> None:
    try:
        storage._write_protected(config.INTL_REGISTRATIONS_FILE, json.dumps(
            {str(k): v for k, v in _intl_registrations.items()}, indent=1))
    except Exception as e:
        logger.warning("Could not save intl_registrations.json: %s", e)


def load_intl_registrations() -> None:
    """Restore the per-member registration counters (encrypted at rest).

    Counters from before this file existed simply are not there: everyone starts
    at 0 for the period they are in. Inventing history from the stored cards
    would be guessing, and the counter's job is to stop FUTURE churn.
    """
    try:
        if config.INTL_REGISTRATIONS_FILE.exists():
            text, _was_enc = storage._read_protected(config.INTL_REGISTRATIONS_FILE)
            if text is not None:
                loaded = {int(k): v for k, v in json.loads(text).items()}
                _intl_registrations.clear()
                _intl_registrations.update(loaded)
    except (Exception, SystemExit) as e:
        # Undecryptable ciphertext is preserved as .locked by _read_protected;
        # a counter that cannot be read must never refuse startup.
        logger.warning("Could not load intl_registrations.json: %s", e)


def _intl_period_end(chat_id: int) -> float:
    """When this member's current allowance period ends.

    Pro: the subscription expiry — a renewal starts a new, empty period.
    Free: the end of the current calendar month (the free international
    allowance resets on the 1st), so a non-subscriber's tally has a period
    key too and `count_international` can work for it.
    """
    expiry = float(_pro_users.get(chat_id, 0.0))
    if expiry > 0.0:
        return expiry
    now = datetime.now(timezone.utc)
    first_next = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                  + timedelta(days=32)).replace(day=1)
    return first_next.timestamp()


def count_international(chat_id: int) -> int:
    """International registrations this member has consumed this period.

    This used to count currently stored international packages, which made the
    allowance refillable: add, remove, re-add spent a fresh 17TRACK slot every
    time while the card count never grew. The period is the subscription window
    ending at the member's expiry, so a renewal starts a new, empty period.
    """
    period_end = _intl_period_end(chat_id)
    entry = _intl_registrations.get(chat_id) or {}
    if not period_end or float(entry.get("period_end") or 0.0) != period_end:
        return 0
    return int(entry.get("count") or 0)


def record_intl_registration(chat_id: int, tracking_id: str) -> None:
    """Count one international registration against this member's allowance.

    Pro: called when 17TRACK answered "registered" — the moment a shared quota
    slot is actually spent. Free: called at add time (there is no 17TRACK
    registration for a free chat; the tally counts the add itself). Removing the
    package later must not hand the slot back, so nothing decrements this.
    Idempotent per period: a number already in the tally charges nothing again —
    the gate exempts that remove → re-add precisely because its slot was spent,
    so the re-registration must not spend a second one.
    """
    period_end = _intl_period_end(chat_id)
    if not period_end or not carriers.is_international_number(tracking_id):
        return
    entry = _intl_registrations.get(chat_id) or {}
    if float(entry.get("period_end") or 0.0) != period_end:
        entry = {"period_end": period_end, "count": 0, "numbers": []}
    # WHICH numbers this period consumed, not just how many: the gate exempts
    # these on re-add. Without it, allowance 1 plus remove/re-add would strand a
    # member's own number until renewal — the tally's job is stopping churn from
    # REFILLING the allowance, not locking anyone out of what they registered.
    numbers = entry.setdefault("numbers", [])
    if tracking_id in numbers:
        return
    entry["count"] = int(entry.get("count") or 0) + 1
    numbers.append(tracking_id)
    _intl_registrations[chat_id] = entry
    save_intl_registrations()
    limit = config.PRO_INTL_LIMIT if is_pro(chat_id) else config.FREE_INTL_LIMIT
    logger.info("17TRACK: %s registered — international allowance %d/%d this "
                "period (chat=%s)", carriers._sanitize_tracking_id(tracking_id),
                entry["count"], limit, storage.mask_chat(chat_id))


def check_intl_limit(chat_id: int, new_numbers=None) -> Tuple[bool, str]:
    """Whether this chat may add international packages.

    International posts are 17TRACK-only, so every one of them spends a slot of
    the bot's own shared free quota (200 numbers) — the cap keeps a single
    account from eating it. The cap counts REGISTRATIONS this period
    (count_international), because that is when the quota is spent: a member who
    adds and removes the same package would otherwise consume slots forever.
    Only Pro chats were capped originally; FREE_INTL_LIMIT then gave free
    chats their own monthly allowance for a while — retired: international is
    Pro-only again, so a free chat's international add is refused right here
    (limit 0) and never reaches the tally. Owners are exempt. `new_numbers`
    are the tracking IDs being added right now, classified from their format
    (the first lookup hasn't happened yet); numbers already stored are
    classified from their verified carrier/country.
    """
    if chat_id in config.OWNER_CHAT_IDS:
        return True, ""
    pro_chat = is_pro(chat_id)
    # A number this period already counted charges nothing on re-add — its slot
    # was spent at its first registration. Checked against the REGISTRATION
    # TALLY (not the stored packages), because both add flows run this gate
    # BEFORE the package is stored: the remove→re-add scenario has nothing in
    # the store to exempt, and the tally is exactly the "already paid" record.
    counted = set((_intl_registrations.get(chat_id) or {}).get("numbers") or [])
    adding = sum(1 for tid in (new_numbers or [])
                 if carriers.is_international_number(tid) and tid not in counted)
    current = count_international(chat_id)
    limit = config.PRO_INTL_LIMIT if pro_chat else config.FREE_INTL_LIMIT
    if current + adding > limit:
        if pro_chat:
            return False, (f"🌍 International limit reached ({current}/{limit} on Pro). "
                           "These posts run on our shared 17TRACK quota, and the "
                           f"allowance counts registrations: {limit} per Pro period. "
                           "Removing a package does not free a slot — it resets when "
                           "your Pro period renews.")
        return False, ("🌍 International tracking is a Pro feature. Upgrade "
                       "to Pro for "
                       f"{config.PRO_INTL_LIMIT} international packages per "
                       "subscription period.")
    return True, ""


def is_pro(chat_id: int) -> bool:
    return chat_id in config.OWNER_CHAT_IDS or _pro_users.get(chat_id, 0.0) > time.time()


def pkg_limit(chat_id: int) -> int:
    if chat_id in config.OWNER_CHAT_IDS:
        return config.UNLIMITED
    return config.PRO_PACKAGE_LIMIT if is_pro(chat_id) else config.FREE_PACKAGE_LIMIT


def check_package_limit(chat_id: int, adding: int = 1) -> Tuple[bool, str]:
    current = len(storage.track_sessions.get(chat_id, {}))
    limit = pkg_limit(chat_id)
    if current + adding > limit:
        if is_pro(chat_id):
            return False, (f"Package limit reached ({current}/{limit} Pro tier). "
                           f"Remove old packages with /remove or /purge.")
        return False, (f"Package limit reached ({current}/{limit} free tier). "
                       f"Upgrade to Pro for {config.PRO_PACKAGE_LIMIT} packages — "
                       f"tap Upgrade to Pro in the Mini App.")
    return True, ""


# ── Unique payment amounts ───────────────────────────────────────────────
# A flat price can't tell two simultaneous buyers apart: with one coin amount
# and a ±1 cent tolerance, "who paid?" is unanswerable (and memos only work
# when the sender's wallet bothers to attach one). So every invoice gets its own
# amount — base price plus a random micro-tail — and that amount IS the payer's
# identifier. Matching is then exact, which also survives payments made late.
_TAIL_MAX = 9999        # identifying values: +$0.000001 … +$0.009999 (4 digits)


def allocate_amount(taken: Set[int]) -> Tuple[int, float]:
    """(amount_micros, amount) = base price + a free random tail.

    `taken` is every amount that must stay unambiguous (pending, grace-pooled
    and recently consumed invoices). The scan starts at a random offset and
    wraps, so it cannot spin: 9999 candidates minus a handful taken."""
    base = int(round(config.PRO_PRICE_USD * 1_000_000))
    start = random.randint(1, _TAIL_MAX)
    for step in range(_TAIL_MAX):
        tail = ((start - 1 + step) % _TAIL_MAX) + 1
        micros = base + tail
        if micros not in taken:
            return micros, micros / 1_000_000
    raise RuntimeError("no free payment amount — too many open invoices")


def retire_amount(invoice: dict, transfer_micros: Optional[int] = None) -> None:
    """Park an invoice's amount: no new invoice may reuse it while a wallet
    could still hold it in a QR code, deep link or pending transfer.

    transfer_micros is the amount the wallet actually sent, which can differ
    from the invoice after wallet rounding — parked alongside so a replay of
    the rounded amount is caught by the duplicate check after a restart."""
    micros = invoice.get("amount_micros")
    if micros:
        _used_amounts[micros] = time.time()
    if transfer_micros:
        _used_amounts[transfer_micros] = time.time()
    if micros or transfer_micros:
        _mark_payments_dirty()


def retire_credited_amount(amount_micros: int) -> int:
    """Consume a credited amount and drop every invoice that carries it.

    The monitor does this inline: it pops the invoice it matched (by id) and
    calls retire_amount. A manual credit has no matched invoice, so it has to do
    the same work by amount — otherwise the invoice stays matchable and the next
    monitor pass credits the same transfer a second time (known_sigs is empty
    after a restart, so the newest signatures are rescanned). Returns how many
    invoices were retired.
    """
    retired = 0
    for pool in (_pending_payments, _expired_payments):
        for pid in [p for p, inv in pool.items()
                    if inv.get("amount_micros") == amount_micros]:
            retire_amount(pool.pop(pid), amount_micros)
            retired += 1
    if not retired:
        # Nothing was open for it, but the amount is spent all the same: parking
        # it keeps a replay from matching some other invoice later.
        retire_amount({"amount_micros": amount_micros}, amount_micros)
    _mark_payments_dirty()
    save_pending_payments()
    return retired


def park_invoice(pid: str, invoice: dict) -> None:
    """Keep a superseded/expired invoice matchable for the grace window.

    Two reasons: the amount must not be re-issued meanwhile, and a customer
    whose wallet still holds the amount they were shown (old QR, deep link,
    opened invoice) must still be credited instead of losing the transfer.
    """
    invoice["expired_at"] = time.time()
    _expired_payments[pid] = invoice
    _mark_payments_dirty()


def to_micros(amount: float) -> int:
    """Integer micro-units of a stablecoin amount (USDC/USDT use 6 decimals).

    _tx_payment_info rounds the on-chain delta to 6 decimals, so recovering the
    integer here is exact — a single micro of drift would leave a paying
    customer unmatched.
    """
    return int(round(amount * 1_000_000))


def open_amounts() -> Set[int]:
    """Amounts a new invoice must avoid: matchable invoices plus any amount
    consumed recently (a duplicate/late transfer of a used amount must not be
    able to hit a fresh invoice for someone else)."""
    now = time.time()
    amounts = {inv.get("amount_micros") for inv in _pending_payments.values()}
    amounts |= {inv.get("amount_micros") for inv in _expired_payments.values()}
    amounts |= {m for m, at in _used_amounts.items()
                if now - at <= config.PAYMENT_GRACE_SECONDS}
    return {a for a in amounts if a}


def new_memo_token() -> str:
    """Public, non-identifying memo for a Solana Pay URL.

    The old memo was f"{chat_id}-{timestamp}", i.e. every payment published the
    customer's Telegram chat ID on a permanent public ledger. The unique amount
    already identifies the payer, so the memo only has to be unique, not
    meaningful — and the Mini App's privacy sheet stays true.
    """
    return "pk" + secrets.token_hex(6)


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(data: bytes) -> str:
    """Base58 (Bitcoin alphabet) — the encoding Solana addresses use.

    Hand-rolled on purpose: the project has no base58 package and adding one for
    a dozen lines would be a dependency for nothing. Leading zero bytes map to
    leading '1's, which is what makes 32 zero bytes the system program's address.
    """
    zeros = len(data) - len(data.lstrip(b"\x00"))
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = _B58_ALPHABET[rem] + out
    return "1" * zeros + out


def new_reference() -> str:
    """Unique Solana Pay reference for one invoice: 32 random bytes, base58.

    It is NOT a real keypair — nobody signs with it. It only has to look like a
    pubkey to the wallet (which adds it to the transaction as a readonly account)
    and be unique per invoice, so the monitor can identify the payer without the
    amount matching at all. Random, so it carries no chat ID: memos are public
    forever and references are too, and they get the same treatment.
    """
    return _b58encode(secrets.token_bytes(32))


# ─── Per-chat deposit addresses (manual / exchange payments) ─────────────────
#
# Why: a reference only helps if the wallet speaks Solana Pay. A buyer who copies
# the address into an exchange sends a bare transfer — no reference, no memo, and
# whatever amount the exchange's form rounded to — so the QR path cannot see it
# (that is the 2026-09-23 case this whole section exists for). A per-chat address
# fixes it at the root: the address IS the identifier, whatever arrives on it.
# On-chain the address is pseudonymous; the chat ↔ address map lives only in the
# encrypted registry further down this file.

_ED_P = 2 ** 255 - 19
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P
_ED_BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_ED_BY = (4 * pow(5, _ED_P - 2, _ED_P)) % _ED_P
_ED_B = (_ED_BX, _ED_BY, 1, _ED_BX * _ED_BY % _ED_P)


def _ed_add(p, q):
    """Point addition in extended coordinates (a = -1 twisted Edwards)."""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _ED_P
    b = (y1 + x1) * (y2 + x2) % _ED_P
    c = 2 * t1 * t2 * _ED_D % _ED_P
    d = 2 * z1 * z2 % _ED_P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed25519_public_key_pure(seed: bytes) -> bytes:
    """RFC 8032 public key for a 32-byte seed — base-point scalar multiply.

    Hand-rolled because the project has no nacl/solders and must not grow one for
    this. It is the fallback behind _ed25519_public_key (cryptography is already a
    hard dependency, so in practice this runs in tests only) — and those tests pin
    it against that library AND the RFC's own vectors, byte for byte. A wrong
    derivation here produces an address nobody holds the key to, i.e. funds that are
    gone, which is why it is verified rather than trusted.
    """
    digest = hashlib.sha512(seed).digest()
    # Clamp (RFC 8032 §5.1.5): clear bits 0-2 and bit 255, SET bit 254. Getting
    # this wrong still produces a valid-looking address — just not this key's.
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 255) - 8                   # bits 0,1,2 and 255 cleared
    scalar |= 1 << 254
    point = (0, 1, 1, 0)                       # neutral element
    for bit in reversed(range(256)):
        point = _ed_add(point, point)          # double
        if scalar >> bit & 1:
            point = _ed_add(point, _ED_B)      # add the base point
    x, y, z, _t = point
    zinv = pow(z, _ED_P - 2, _ED_P)
    affine_x, affine_y = x * zinv % _ED_P, y * zinv % _ED_P
    out = bytearray(affine_y.to_bytes(32, "little"))
    out[31] |= (affine_x & 1) << 7             # the sign of x rides in the top bit
    return bytes(out)


try:      # the audited implementation, whenever it is importable
    from cryptography.hazmat.primitives import serialization as _ed_ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey as _Ed25519PrivateKey)
    _HAVE_ED25519_LIB = True
except Exception:                              # pragma: no cover
    _HAVE_ED25519_LIB = False


def _ed25519_public_key(seed: bytes) -> bytes:
    """Public key for a 32-byte Ed25519 seed."""
    if _HAVE_ED25519_LIB:
        return _Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
            _ed_ser.Encoding.Raw, _ed_ser.PublicFormat.Raw)
    return _ed25519_public_key_pure(seed)


_DEPOSIT_DOMAIN = b"packey-deposit-v1"


def deposit_seed_bytes() -> Optional[bytes]:
    """The configured master seed, or None when the feature is off/misconfigured."""
    raw = (config.DEPOSIT_SEED or "").strip()
    if not raw:
        return None
    try:
        seed = bytes.fromhex(raw)
    except ValueError:
        logger.error("DEPOSIT_SEED is not hexadecimal — per-chat deposit addresses "
                     "are OFF (the QR / payment-link path is unaffected)")
        return None
    if len(seed) != 32:
        # A short seed is a guessable seed: every chat's key derives from this.
        logger.error("DEPOSIT_SEED must be 32 bytes (64 hex chars), %d given — "
                     "per-chat deposit addresses are OFF", len(seed))
        return None
    return seed


def deposit_secret(chat_id: int) -> Optional[bytes]:
    """One chat's deposit key: a real, spendable Ed25519 seed. Never stored.

    Deterministic (seed + chat id) so a lost registry can be rebuilt, and derived
    rather than stored so the key exists only here and in the operator's export.
    Whoever holds it can move that chat's funds — it must never be logged.
    """
    seed = deposit_seed_bytes()
    if seed is None:
        return None
    return hashlib.sha256(_DEPOSIT_DOMAIN + seed
                          + int(chat_id).to_bytes(8, "big")).digest()


def deposit_address(chat_id: int) -> Optional[str]:
    """Where this chat's manual payments go, or None when the feature is off."""
    secret = deposit_secret(chat_id)
    if secret is None:
        return None
    return _b58encode(_ed25519_public_key(secret))


def deposit_keypair_b58(chat_id: int) -> Optional[str]:
    """The importable secret for one deposit address: base58(seed ‖ pubkey).

    That 64-byte form is what Phantom & co. accept. Only the explicit
    --deposit-secret export in scripts/payment_reconcile.py may call this, and it
    prints to the terminal — never to a log line, never to a file.
    """
    secret = deposit_secret(chat_id)
    if secret is None:
        return None
    return _b58encode(secret + _ed25519_public_key(secret))


def _band_ceiling(base_micros: int) -> int:
    """The rounded-up top of one price band.

    A wallet or an exchange form showing two decimals sends exactly the rounded-up
    value of what it was asked for (0.019999 -> 0.020000), so the envelope has to
    reach that far or a correctly-paid invoice would look like an overpayment.
    """
    return ((base_micros + _TAIL_MAX) // 10_000 + 1) * 10_000


def _active_price_bases() -> Set[int]:
    """Every price whose band can still legitimately be paid.

    The configured price now, plus whatever each still-matchable invoice recorded when
    it was created. A price change must not strand real money: a transfer for an
    invoice at the old price — or a late one, from the grace pool — is still a payment,
    and the band is the only thing standing between a fat-fingered transfer and a free
    subscription. Old bands retire by themselves, because their invoices leave the pools.
    """
    bases = {int(round(config.PRO_PRICE_USD * 1_000_000))}
    for pool in (_pending_payments, _expired_payments):
        for invoice in pool.values():
            base = invoice.get("base_micros")
            if base:
                bases.add(int(base))
    return bases


def _in_price_band(amount_micros: int) -> bool:
    """Is this amount plausibly a payment for this product, exact or rounded?

    For the main wallet the amount is an identifier, so this is only a sanity guard;
    for a deposit address it is the guard that keeps a 500 USDT mistake from buying
    Pro. Either way it must accept the price any open invoice was created at — see
    _active_price_bases.
    """
    return any(base <= amount_micros <= _band_ceiling(base)
               for base in _active_price_bases())


def _match_payment(amount_micros: int, memo: str = "",
                   tx_time: Optional[float] = None) -> Optional[Tuple[str, dict]]:
    """Which invoice this transfer pays — exact micro-USD match.

    The memo (Solana Pay attaches one, when the wallet sends it) is a fast path,
    but it must still agree on the amount. Returns (payment_id, invoice) or None.

    tx_time is the transfer's own block time, when the caller knows it: it bounds
    the search to invoices that could have been open when the money moved. The
    grace pool keeps an expired invoice matchable for a whole day, so without
    this an old transfer could pay a much newer invoice that happens to carry the
    same amount. None (report-only callers, e.g. the reconcile script) keeps the
    unbounded scan it always had.

    An amount already in _used_amounts is refused outright: it has been paid
    once. The monitor consumes an amount only AFTER it credits it, so this can
    never block a first payment.
    """
    if amount_micros in _used_amounts:
        return None
    window = config.PAYMENT_EXPIRY_SECONDS + config.PAYMENT_GRACE_SECONDS

    def _in_window(invoice: dict) -> bool:
        if tx_time is None:
            return True
        created = invoice.get("created", 0.0)
        # 300 s of slack on both sides: clock skew, and an invoice created
        # moments after the transfer was signed.
        return created - 300 <= tx_time <= created + window + 300

    if memo:
        for pid, invoice in _pending_payments.items():
            if (invoice.get("memo") == memo
                    and invoice.get("amount_micros") == amount_micros
                    and _in_window(invoice)):
                return pid, invoice
    for pool in (_pending_payments, _expired_payments):
        for pid, invoice in pool.items():
            if invoice.get("amount_micros") == amount_micros and _in_window(invoice):
                return pid, invoice
    return None


def _match_by_timing(amount_micros: int, tx_time: float) -> Optional[Tuple[str, dict]]:
    """Fallback for a real payment that missed the identifier: plausible amount,
    exactly one invoice open at the moment the transfer landed.

    Why this exists: a buyer sent 0.020000 USDT while an invoice for 0.012590 was
    open — a two-decimal wallet collapses an invoice amount to its whole cents and
    drops the tail digits, and a human may just send a round figure. The amount IS
    the identifier, so exact matching credited nobody: money taken, no Pro. A price
    band alone is unsafe with several buyers at once, so the guard is uniqueness in
    time — the transfer must fall inside exactly ONE invoice's open window. The payer
    cannot choose the chat (they can only pay an invoice that already exists), so the
    worst case is gifting Pro to the chat that was already waiting for it.
    """
    if amount_micros in _used_amounts:
        return None          # this exact amount was paid before: a repeat is a duplicate
    # One band rule for every path (_in_price_band), which also means an invoice
    # created at an older price keeps its band while it stays matchable.
    if not _in_price_band(amount_micros):
        return None
    window = config.PAYMENT_EXPIRY_SECONDS + config.PAYMENT_GRACE_SECONDS
    candidates = [(pid, inv)
                  for pool in (_pending_payments, _expired_payments)
                  for pid, inv in pool.items()
                  if inv.get("created", 0.0) <= tx_time
                  <= inv.get("created", 0.0) + window]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        logger.warning("Solana payment of %.6f could not be attributed: %d invoices "
                       "were open at that moment", amount_micros / 1_000_000,
                       len(candidates))
    return None


def _tx_references(tx: dict) -> Set[str]:
    """Every account pubkey the transaction mentions.

    A Solana Pay reference rides along as a read-only, non-signing account, so it
    appears here and nowhere else in the parsed transaction — which is what lets a
    payment be identified by the URL the buyer used rather than by its amount.
    """
    msg = (tx.get("transaction") or {}).get("message") or {}
    out: Set[str] = set()
    for key in (msg.get("accountKeys") or []):
        pubkey = key.get("pubkey") if isinstance(key, dict) else key
        if pubkey:
            out.add(str(pubkey))
    return out


def _match_by_reference(tx: dict) -> Optional[Tuple[str, dict]]:
    """Which invoice this transfer pays, by its Solana Pay reference.

    Why this exists: a wallet that shows two decimals turns a $0.013434 invoice
    into a $0.01 payment, and the exact-micro-amount identifier then matches
    nothing — money landed, no Pro, buyer retried (2026-09-23). The reference is
    embedded in the payment URL, so any Solana Pay-compatible wallet includes it in
    the transaction regardless of what the amount became.

    Both pools are searched: an invoice past its 30-minute window stays matchable
    for a day, so a late payment through an old QR still counts. Single-use holds
    because the caller pops the invoice from both pools on a match.
    """
    present = _tx_references(tx)
    if not present:
        return None
    for pool in (_pending_payments, _expired_payments):
        for pid, invoice in pool.items():
            reference = invoice.get("reference")
            if reference and reference in present:
                return pid, invoice
    return None


async def _sol_rpc(method: str, params: list) -> Optional[dict]:
    """Minimal Solana JSON-RPC call over the shared httpx client."""
    try:
        resp = await carriers.http_client().post(
            config.SOLANA_RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=config.HTTP_TIMEOUT,
        )
        data = resp.json()
        if data.get("error"):
            logger.warning("Solana RPC %s error: %s", method, data["error"])
            return None
        return data.get("result")
    except Exception as e:
        logger.warning("Solana RPC %s failed: %s", method, e)
        return None


# ─── Building the payer's transaction (the wallet-scoped payment) ────────────
#
# Why: the pay page used to hand the wallet a `solana:` URI. Every Solana wallet
# registers that scheme, so the OPERATING SYSTEM picks the handler — and on a device
# whose default is another wallet, that app opens its own empty send screen and the
# amount is lost (reported 2026-09-24, from a payer whose default was Coinbase
# Wallet). No wallet-scoped payment link exists to use instead: Phantom's own "Request
# payment" recipe is the same URL. So the page asks the backend for a TRANSACTION
# instead and hands it to the wallet provider already injected in that in-app
# browser: nothing is resolved by the OS, and the recipient, amount, mint and memo are
# whatever this function put in the bytes. Zero keys are involved — the transaction we
# return has an empty signature slot for the payer's own wallet to fill.
#
# The two account addresses a transfer needs are LOOKED UP rather than derived: the
# RPC answers "which token account holds this mint for this owner", which is the
# question a PDA derivation would answer with an ed25519 on-curve check the project
# has no business hand-rolling. The same call reports the mint's decimals, so
# TransferChecked is told the truth.
_our_token_accounts: Dict[str, Dict[str, Tuple[str, int]]] = {}


async def _token_account_of(owner: str, mint: str) -> Optional[Tuple[str, int]]:
    """(address, decimals) of the token account `owner` holds `mint` in, fullest first.

    An owner can hold one mint in several accounts (non-canonical ones included), and
    the empty one would fail the send, so the highest balance wins.
    """
    result = await _sol_rpc("getTokenAccountsByOwner",
                            [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    best: Optional[Tuple[str, int]] = None
    best_amount = -1.0
    for entry in ((result or {}).get("value") or []):
        data = (entry.get("account") or {}).get("data") or {}
        info = (data.get("parsed") or {}).get("info") or {}
        token_amount = info.get("tokenAmount") or {}
        address = entry.get("pubkey")
        decimals = token_amount.get("decimals")
        if not address or not isinstance(decimals, int):
            continue
        amount = float(token_amount.get("uiAmount") or 0)
        if amount > best_amount:
            best, best_amount = (address, decimals), amount
    return best


async def _our_token_account(mint: str) -> Optional[Tuple[str, int]]:
    """Our own receiving account for one mint, cached for this process.

    Our address is fixed, so the lookup is a startup-cost-shaped one: the cache only
    needs dropping if the account did not exist yet and later appears.
    """
    cached = _our_token_accounts.get(config.BOT_USDC_ADDRESS, {}).get(mint)
    if cached:
        return cached
    found = await _token_account_of(config.BOT_USDC_ADDRESS, mint)
    if found:
        _our_token_accounts.setdefault(config.BOT_USDC_ADDRESS, {})[mint] = found
    return found


async def payer_can_pay(payer: str, mint: str) -> bool:
    """Whether this payer holds an account for one of our accepted mints.

    Asked before signing, so a payer whose wallet holds the other stablecoin is not
    stopped by a choice made on another screen — and so a payment that cannot work says
    why instead of showing a code.
    """
    return await _token_account_of(payer, mint) is not None


async def payment_transaction_parts(invoice: dict, payer: str,
                                    mint: str) -> Tuple[Optional[bytes], str]:
    """As payment_transaction_bytes, plus WHICH piece was missing.

    The reason exists for the payer's own screen: "no_payer_account" is worth more than a
    status code, and it is only ever asked for once the build has already failed.
    """
    amount_micros = invoice.get("amount_micros")
    if not isinstance(amount_micros, int) or amount_micros <= 0:
        return None, "bad_amount"
    source = await _token_account_of(payer, mint)
    if source is None:
        return None, "no_payer_account"
    destination = await _our_token_account(mint)
    if destination is None:
        return None, "no_receiving_account"
    # The invoice's amount is in micros because the price is quoted in USD units of
    # 1e-6; a mint with other decimals would make those micros mean something else.
    if source[1] != 6 or destination[1] != 6:
        logger.warning("Refusing to build a payment: mint %s has %d/%d decimals, "
                       "invoices are in 1e-6 units", mint, source[1], destination[1])
        return None, "unsupported_decimals"
    result = await _sol_rpc("getLatestBlockhash", [{"commitment": "processed"}])
    blockhash = ((result or {}).get("value") or {}).get("blockhash")
    if not blockhash:
        return None, "no_blockhash"
    # Both the memo and the reference are MATCHING AIDS, not the payment: the spec has
    # the reference optional, and a transfer needs no memo. An invoice from before a
    # field existed (or a hand-edited file) must still be payable through the wallet's
    # own provider, so a memo that cannot be expressed is dropped rather than fatal...
    memo = invoice.get("memo") or ""
    if len(memo.encode("utf-8")) > 566:              # the memo program's limit
        logger.warning("Payment transaction: memo of %d bytes is not expressible — "
                       "building without it", len(memo.encode("utf-8")))
        memo = ""

    def _build(reference: str) -> bytes:
        return solana_tx.payment_transaction(
            payer=payer, source=source[0], destination=destination[0], mint=mint,
            amount_micros=amount_micros, decimals=source[1],
            memo=memo, reference=reference, blockhash=blockhash)

    try:
        transaction = _build(invoice.get("reference") or "")
    except ValueError as e:
        # ...and the reference likewise only ever STRENGTHENS matching. Something that
        # is not a public key must not turn a payable invoice into a manual one: the
        # amount and memo still identify the payer, so retry without it.
        logger.warning("Payment transaction: unusable reference (%s) — retrying "
                       "without it", e)
        try:
            transaction = _build("")
        except ValueError as e2:
            # Nothing left to drop: a malformed input here would be a transaction
            # nobody should sign, and the manual route is the honest answer.
            logger.warning("Cannot build a payment transaction: %s", e2)
            return None, "unbuildable"
    logger.info("Payment transaction built in %s (%.6f) for %s — the payer's own "
                "wallet signs it", mint, amount_micros / 1_000_000,
                storage.mask_chat(int(invoice.get("chat_id") or 0)))
    return transaction, ""


async def payment_transaction_bytes(invoice: dict, payer: str,
                                    mint: str) -> Optional[bytes]:
    """The unsigned transaction paying `invoice` from `payer` in `mint`, or None.

    The two consumers of the bytes differ: the in-browser route hands the wallet base64,
    Phantom's deep link takes base58 (phantom_link wraps it). Callers that want to tell
    the payer WHICH piece was missing use payment_transaction_parts instead.
    """
    transaction, _reason = await payment_transaction_parts(invoice, payer, mint)
    return transaction


async def build_payment_transaction(invoice: dict, payer: str,
                                    mint: str) -> Optional[str]:
    """The same transaction as base64 — what the in-browser wallet provider takes."""
    transaction = await payment_transaction_bytes(invoice, payer, mint)
    return solana_tx.to_base64(transaction) if transaction else None


async def submit_signed_transaction(signed_b58: str) -> Optional[str]:
    """Submit a transaction the payer's wallet signed, and return its signature.

    Why we submit rather than letting the wallet do it: Phantom's deep-link API marks
    signAndSendTransaction deprecated, and taking the signed bytes back puts the
    submission (and any retry) in code that can log it, while the wallet only ever sees
    what it signs.
    """
    signature = await _sol_rpc("sendTransaction",
                               [signed_b58, {"encoding": "base58"}])
    if not isinstance(signature, str) or not signature:
        return None
    logger.info("Signed payment transaction submitted (signature %.8s…)", signature)
    return signature


def _signed_by_us(tx: dict) -> bool:
    """Did one of OUR keys sign this transaction? Then it cannot be a payment to us.

    Why this exists: the shared wallet's incoming balance is the monitor's evidence
    that somebody paid — and our own money moving raises that balance too. The
    operator sweeping a deposit address into the shared wallet is exactly that
    (the sweep workflow is deliberate — see --deposit-secret), and without this
    check the arrival is treated as income and matched to whatever invoice is open:
    a sweep of 0.017777 matched another chat's 0.017777 invoice and credited them
    for their own money, and a rounded sweep matched by timing. A customer's
    transfer is signed by the customer; anything we signed is our own movement, so
    it must not be matched to anybody's invoice. `config.OPERATOR_WALLETS` extends
    "ours" to the operator's own wallets (declared as addresses in .env, never as
    keys) — the refund-float and test-transfer case, where the money arrives from a
    wallet the bot would otherwise read as a customer. Costs one dict lookup per
    account key, no extra RPC — jsonParsed lists the signers inline.
    """
    ours = {config.BOT_USDC_ADDRESS} if config.BOT_USDC_ADDRESS else set()
    ours |= {str(rec.get("address")) for rec in _deposit_addrs.values()
             if rec.get("address")}
    ours |= set(config.OPERATOR_WALLETS)
    msg = (tx.get("transaction") or {}).get("message") or {}
    for key in (msg.get("accountKeys") or []):
        if isinstance(key, dict) and key.get("signer") and key.get("pubkey") in ours:
            return True
    return False


def _tx_payment_info(tx: dict,
                     address: Optional[str] = None) -> Optional[Tuple[float, str, str]]:
    """Extract (amount, mint, memo) of a stablecoin transfer into `address` from a
    jsonParsed transaction. None if not a matching transfer.

    `address` is the account the caller polled: the shared wallet for the
    QR/reference flow (the default), or one chat's own deposit address for the
    manual flow. A transaction our own keys signed is refused outright — see
    _signed_by_us.
    """
    address = address or config.BOT_USDC_ADDRESS
    try:
        if _signed_by_us(tx):
            sig = ((tx.get("transaction") or {}).get("signatures") or [""])[0]
            logger.info("Solana tx signed by one of our own wallets (sweep or "
                        "consolidation) — not a payment, ignored (sig=%s)",
                        str(sig)[:12])
            return None
        meta = tx.get("meta") or {}
        post_balances = meta.get("postTokenBalances") or []
        pre_by_idx = {b.get("accountIndex"): b for b in (meta.get("preTokenBalances") or [])}
        account_keys = [k.get("pubkey") for k in
                        (tx.get("transaction", {}).get("message", {}).get("accountKeys") or [])]
        for post in post_balances:
            mint = post.get("mint", "")
            if mint not in config.ACCEPTED_MINTS:
                continue
            idx = post.get("accountIndex")
            acct = account_keys[idx] if isinstance(idx, int) and idx < len(account_keys) else None
            owner = post.get("owner", "")
            if address not in (acct, owner):
                continue
            pre_entry = pre_by_idx.get(idx)
            pre_amt = 0.0
            if pre_entry and pre_entry.get("uiTokenAmount", {}).get("uiAmount") is not None:
                pre_amt = float(pre_entry["uiTokenAmount"]["uiAmount"])
            post_amt = float(post.get("uiTokenAmount", {}).get("uiAmount") or 0)
            diff = round(post_amt - pre_amt, 6)
            if diff <= 0:
                continue
            memo = ""
            msg = tx.get("transaction", {}).get("message", {})
            all_ix = list(msg.get("instructions") or [])
            for inner in (meta.get("innerInstructions") or []):
                all_ix.extend(inner.get("instructions") or [])
            for ix in all_ix:
                parsed = ix.get("parsed") if isinstance(ix.get("parsed"), dict) else None
                if parsed and parsed.get("type") == "memo":
                    info = parsed.get("info")
                    memo = info.get("memo", "") if isinstance(info, dict) else str(info or "")
                    break
            return diff, mint, memo
    except Exception as e:
        logger.warning("Solana tx parse error: %s", e)
    return None
def pro_welcome_text(amount: float, mint: str, sig: str, payment_id: str = "",
                     matched_by: str = "amount", renewed: bool = False,
                     activated_ts: Optional[float] = None,
                     expiry_ts: Optional[float] = None,
                     resent: bool = False) -> str:
    """The buyer's confirmation message — one copy, used by every path.

    `matched_by` must state how the payment was actually recognised. Automatic
    credits usually match on the unique amount (the identifier), but a rounded
    amount is credited by the open invoice at the time of the transfer, and an
    operator credit is manual — saying "the amount identified you" in those cases
    would be a lie in the receipt a customer keeps.
    """
    now = time.time()
    activated_ts = activated_ts if activated_ts is not None else now
    token_name = config.ACCEPTED_MINTS.get(mint, "USDC")
    action = "extended" if renewed else "activated"
    if resent:
        action = "receipt re-sent"
    header = ("📋 <b>Your Pro receipt</b> (copy)" if resent
              else "🎉 <b>Welcome back to Pro!</b>" if renewed
              else "🎉 <b>Welcome to Pro!</b>")
    why = {
        "deposit": "your personal deposit address — it identified you",
        "reference": "the payment link you used — it identified you",
        "amount": "the unique amount above — it identified you",
        "timing": "your open invoice at the time of payment",
        "manual": "confirmed manually against your payment",
    }.get(matched_by, "confirmed against your payment")
    tx_link = f"https://solscan.io/tx/{sig}"
    short_sig = sig[:6] + "…" + sig[-6:] if len(sig) > 16 else sig
    wallet_short = (config.BOT_USDC_ADDRESS[:8] + "…" + config.BOT_USDC_ADDRESS[-4:]
                    if len(config.BOT_USDC_ADDRESS or "") > 16
                    else config.BOT_USDC_ADDRESS or "")
    activated_str = datetime.fromtimestamp(activated_ts, tz=timezone.utc)\
        .strftime("%b %d, %Y %H:%M UTC")
    expires_full = (datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
                    .strftime("%b %d, %Y %H:%M UTC") if expiry_ts else "—")
    return (
        f"{header}\n"
        f"Your payment is confirmed — Pro is active on this chat.\n\n"
        f"✅ <b>Payment confirmed</b>\n"
        f"<b>Amount:</b> {amount:.6f} {token_name} (Solana)\n"
        f"<b>Matched by:</b> {why}\n"
        f'<b>Transaction:</b> <a href="{tx_link}">{short_sig}</a>\n\n'
        f"────────────────\n"
        f"🌟 <b>Pack·ages Pro</b>\n"
        f"────────────────\n\n"
        f"<b>{action.capitalize()}:</b> {activated_str}\n"
        f"<b>Expires:</b> {expires_full}\n"
        f"<b>Duration:</b> {config.SUBSCRIPTION_DAYS} days\n\n"
        f"<b>Receipt ID:</b> <code>{payment_id or sig[:16]}</code>\n"
        f"<b>Wallet:</b> <code>{wallet_short}</code>\n\n"
        f"<b>Pro Benefits:</b>\n"
        f"✓ Track up to {config.PRO_PACKAGE_LIMIT} packages\n"
        f"✓ Checks paced per package every {config.PRO_REFRESH_INTERVAL // 60} min "
        f"(in transit)\n"
        f"✓ 🌍 International tab + live 17TRACK quota "
        f"({config.PRO_INTL_LIMIT} international package"
        f"{'s' if config.PRO_INTL_LIMIT != 1 else ''} included)\n"
        f"✓ Dark &amp; Pink themes · haptic feedback\n\n"
        f"Need help? /help or reply to this message")


async def _activate_pro(chat_id: int, amount: float, mint: str, sig: str,
                        payment_id: str = "", matched_by: str = "amount") -> None:
    now = time.time()
    renewed = chat_id in _pro_users and _pro_users[chat_id] > now
    base = max(now, _pro_users.get(chat_id, 0.0))  # renewals stack
    _pro_users[chat_id] = base + config.SUBSCRIPTION_DAYS * 86400
    save_pro_users()
    _expiry_warned.pop(chat_id, None)
    token_name = config.ACCEPTED_MINTS.get(mint, "USDC")
    expiry_ts = _pro_users[chat_id]
    expiry_str = datetime.fromtimestamp(expiry_ts, tz=timezone.utc).strftime("%b %d, %Y")
    logger.info("🌟 PRO ACTIVATED: chat=%s paid %.6f %s (sig=%s, matched by %s) "
                "until %s", storage.mask_chat(chat_id), amount, token_name, sig,
                matched_by, expiry_str)
    # Accounting trail for dispute resolution. Six decimals on purpose: the tail
    # of the amount is what identified the payer, so rounding it to cents would
    # make the receipt useless for reconcilliation.
    append_receipt({"ts": now, "chat_id": chat_id, "amount": amount,
                    "token": token_name, "sig": sig, "payment_id": payment_id,
                    "expiry": expiry_ts})
    if botstate.bot is not None:
        try:
            await botstate.bot.send_message(
                chat_id,
                pro_welcome_text(amount, mint, sig, payment_id, matched_by,
                                 renewed=renewed, activated_ts=now,
                                 expiry_ts=expiry_ts),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            # A failed notification must never block the credit itself.
            logger.warning("Pro activation notification failed: %s", e)
async def _recent_signatures(known: Set[str],
                       address: Optional[str] = None) -> List[dict]:
    """The newest signatures for one account, paged back over what we have handled.

    One page is 15 and the loop used to look at exactly that page: a burst of
    more than 15 transfers between two polls silently lost the excess — the
    oldest of the burst were never fetched, so a real payment could go
    uncredited. Paging walks towards older signatures (the RPC answers
    newest-first, so the cursor is the LAST signature of each page) and stops at
    the first page holding something already handled, or after 3 pages: bounded
    work, and in the steady state page one already contains a known signature,
    so it still costs a single call.
    """
    out: List[dict] = []
    before: Optional[str] = None
    for _page in range(3):
        params: dict = {"limit": 15}
        if before:
            params["before"] = before
        page = await _sol_rpc("getSignaturesForAddress",
                              [address or config.BOT_USDC_ADDRESS, params])
        if not page:
            break
        out.extend(page)
        if any(str(s.get("signature")) in known for s in page):
            break
        before = str(page[-1].get("signature"))
    return out


async def _watched_accounts(address: str) -> List[str]:
    """An address plus the token accounts its stablecoins can actually arrive in.

    Why this exists — the 2026-09-24 16:32 miss: a stablecoin transfer moves tokens
    between ASSOCIATED TOKEN ACCOUNTS, so the wallet we watch is only the *owner* of the
    destination. It is not a party to the transaction and therefore does NOT appear in
    getSignaturesForAddress(wallet) at all. Watching the wallet alone missed every Solana
    Pay payment: the money was sitting in our token account, the monitor was reading the
    wallet's signature list, and no receipt ever came. (Only two credits exist in the
    whole receipt trail, which is what a systematic blind spot looks like.)

    Order matters only for cost: the wallet first, then one account per accepted mint.
    """
    accounts = [address]
    for mint in config.ACCEPTED_MINTS:
        found = await _token_account_of(address, mint)
        if found:
            accounts.append(found[0])
    return accounts


async def _signatures_for_watched(address: str, known: Set[str]) -> List[dict]:
    """Newest signatures across an address AND its token accounts, deduplicated.

    One signature can legitimately appear under two of them (a transaction that touches
    both a wallet and its token account), and the caller credits per signature, so the
    dedup here is what keeps a single transfer from being handled twice.
    """
    out: List[dict] = []
    seen: Set[str] = set()
    for account in await _watched_accounts(address):
        for entry in await _recent_signatures(known, account):
            sig = str(entry.get("signature"))
            if sig and sig not in seen:
                seen.add(sig)
                out.append(entry)
    return out


# Per-cycle ceiling on deposit-address polls: the public RPC rate-limits per IP and
# a cycle must stay short (the shared wallet alone is up to three pages of 15).
_DEPOSIT_POLL_LIMIT = 25
# Rotating start index for the capped watch: oldest-first would never watch the
# newest addresses at all once the registry outgrows the cap (2026-10-04 audit).
_deposit_watch_offset = 0
# How often the slow balance watch runs, and a tolerance for float noise on the
# comparison. Watching only pending-invoice chats left a hole the live bot
# demonstrated: the invoice expired, polling for that address stopped, and a
# transfer arriving afterwards was invisible until the buyer reopened the sheet.
_DEPOSIT_BALANCE_WATCH_SECONDS = 300


async def _token_balances(address: str) -> Dict[str, float]:
    """Accepted-stablecoin balance of one address, by token name.

    Two cheap calls (one per mint) instead of paging signatures: that is what makes
    watching every address on a slow cadence affordable, and a signature page is only
    fetched once a balance has actually moved.
    """
    balances = {name: 0.0 for name in config.ACCEPTED_MINTS.values()}
    for mint, name in config.ACCEPTED_MINTS.items():
        result = await _sol_rpc("getTokenAccountsByOwner", [
            address, {"mint": mint}, {"encoding": "jsonParsed"}])
        for entry in ((result or {}).get("value") or []):
            amount = ((((entry.get("account") or {}).get("data") or {})
                       .get("parsed") or {}).get("info", {})
                      .get("tokenAmount", {}).get("uiAmount"))
            if amount:
                balances[name] += float(amount)
    return balances


async def _deposit_balance_watch(known: Set[str],
                                 seen: Dict[int, float]) -> int:
    """Slow cadence: did money land on any deposit address, invoice open or not?

    `seen` remembers each chat's last observed total (in memory: after a restart the
    first pass re-examines everything once, which is bounded and harmless). Only an
    increase triggers signature work — an address that was credited but not yet swept
    keeps its balance forever, so comparing against the last seen total is what stops
    this from paging signatures every cycle for money already handled.

    Addresses whose chat has a pending invoice are left to the fast path above: that
    one is already polling them, and it must stay the prompt route.

    Returns how many transfers were credited.
    """
    live = {inv.get("chat_id") for inv in _pending_payments.values()}
    targets = [(cid, str(rec.get("address") or ""))
               for cid, rec in sorted(_deposit_addrs.items(),
                                      key=lambda pair: pair[1].get("created", 0.0))
               if rec.get("address") and cid not in live]
    if len(targets) > _DEPOSIT_POLL_LIMIT:
        global _deposit_watch_offset
        # Rotate the window instead of always watching the oldest 25: with the
        # cap fixed and the registry only growing, the newest addresses would
        # never be watched (2026-10-04 audit). `seen` survives across passes,
        # so an address watched less often still detects its next increase.
        start = _deposit_watch_offset % len(targets)
        rotated = targets[start:] + targets[:start]
        targets = rotated[:_DEPOSIT_POLL_LIMIT]
        _deposit_watch_offset = (start + _DEPOSIT_POLL_LIMIT) % len(targets)
        logger.warning("Deposit balance watch capped: %d address(es) without a live "
                       "invoice, watching %d this pass (rotating window)",
                       len(targets), _DEPOSIT_POLL_LIMIT)

    credited = 0
    for chat_id, address in targets:
        balances = await _token_balances(address)
        total = sum(balances.values())
        previous = seen.get(chat_id, 0.0)
        seen[chat_id] = total
        if total <= previous + 1e-9:
            continue                       # unchanged (or swept): two calls, no work
        held = "  ".join(f"{amount:.6f} {name}"
                         for name, amount in balances.items() if amount)
        logger.info("Deposit address of chat=%s moved (%s, was %.6f) with no live "
                    "invoice — scanning for a payment",
                    storage.mask_chat(chat_id), held, previous)
        credited += await _scan_deposit_address(address, chat_id, known)
    return credited


def _deposit_poll_targets(limit: int = _DEPOSIT_POLL_LIMIT) -> List[Tuple[str, int]]:
    """(address, chat_id) pairs to poll this cycle.

    Only chats with a PENDING invoice are polled: an expired invoice's
    QR/reference path already covers a late payment, so polling its address for
    the whole 24 h grace window would double the RPC traffic for nothing. Bounded
    and oldest-first, so a burst of new invoices cannot starve the earlier ones.

    Deliberately NOT gated on DEPOSIT_SEED: addresses already handed out are real
    addresses that buyers may still have in an exchange form, and the seed is only
    needed to DERIVE a new one. If the seed is removed, watching the old addresses
    is what keeps a payment already in flight from being stranded.
    """
    live = {inv.get("chat_id") for inv in _pending_payments.values()}
    targets = [(str(rec.get("address")), cid)
               for cid, rec in _deposit_addrs.items()
               if cid in live and rec.get("address")]
    targets.sort(key=lambda pair: _deposit_addrs.get(pair[1], {}).get("created", 0.0))
    if len(targets) > limit:
        logger.warning("Deposit polling capped: %d live address(es), polling %d this "
                       "cycle (a shared RPC endpoint rate-limits)", len(targets), limit)
    return targets[:limit]


def _invoice_for_chat(chat_id: int) -> Tuple[str, dict]:
    """The invoice open for this chat: pending first, then the grace pool."""
    for pool in (_pending_payments, _expired_payments):
        for pid, invoice in pool.items():
            if invoice.get("chat_id") == chat_id:
                return pid, invoice
    return "", {}


async def _scan_deposit_address(address: str, chat_id: int,
                                known: Set[str]) -> int:
    """Credit everything new that arrived on one chat's deposit address.

    The address IS the identifier here: an exchange transfer carries no reference,
    no memo, and whatever amount the sender's form rounded to, so there is nothing
    else to match on. That is also why the guards are deliberately narrow — the
    accepted mints and the price band, which is what stops a fat-fingered 500 USDT
    from buying Pro. Exactness and the _used_amounts dedup do NOT apply: rounded
    amounts repeat across chats, and known_sigs already makes every signature count
    exactly once, so a second distinct transfer to the same address is a renewal.
    """
    credited = 0
    # Two dedups, and both are needed: `known` is this process's memory (it also
    # catches the same signature twice inside one scan), and the receipts are the
    # durable record of what was already paid for — which is what makes a restart
    # safe (see _credited_signatures).
    credited_before = _credited_signatures()
    # Across the address AND its token accounts: an exchange transfer lands in the
    # address's token account, which never appears in the address's own signature list
    # (see _watched_accounts). Reading only the address is what left deposits uncredited.
    for entry in await _signatures_for_watched(address, known):
        sig = str(entry.get("signature"))
        if sig in known or sig in credited_before:
            continue
        if entry.get("err"):
            known.add(sig)                     # failed on-chain: nothing to credit
            continue
        tx = await _sol_rpc("getTransaction", [
            sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        if not tx:
            # Same tolerance as the main flow: a rate-limited lookup is retried next
            # cycle instead of being dropped.
            logger.info("Deposit tx unreadable, retrying next cycle (sig=%s)",
                        sig[:12])
            continue
        info = _tx_payment_info(tx, address)
        if not info:
            known.add(sig)                     # not an accepted stablecoin transfer
            continue
        amount, mint, _memo = info
        known.add(sig)
        if not _in_price_band(to_micros(amount)):
            logger.warning("Transfer to a deposit address outside the price band — "
                           "not credited (chat=%s, %.6f %s, sig=%s)",
                           storage.mask_chat(chat_id), amount,
                           config.ACCEPTED_MINTS.get(mint, mint), sig[:12])
            continue
        pid, _invoice = _invoice_for_chat(chat_id)
        if pid:
            # Same single-use rule as the main flow: the invoice leaves both pools.
            _pending_payments.pop(pid, None)
            _expired_payments.pop(pid, None)
            _mark_payments_dirty()
        await _activate_pro(chat_id, amount, mint, sig, pid, matched_by="deposit")
        credited += 1
    return credited


async def monitor_solana_payments() -> None:
    """Background task: watch the bot account for incoming USDC/USDT and
    match them to pending payments via memo."""
    if not config.BOT_USDC_ADDRESS:
        logger.warning("BOT_USDC_ADDRESS not set — Solana payments disabled")
        return
    logger.info("⛓️ Solana payment monitor started (watching %s + its token accounts, "
                "poll %ds)", config.BOT_USDC_ADDRESS, config.SOL_POLL_SECONDS)
    watched_now = await _watched_accounts(config.BOT_USDC_ADDRESS)
    logger.info("Solana monitor: watching %d account(s) — the wallet plus one token "
                "account per accepted mint, because a payment lands IN the token account "
                "and never appears in the wallet's own signature list", len(watched_now))
    if deposit_seed_bytes() is None:
        # One clear line, because without the seed the manual path is silently
        # absent: an exchange transfer would land and never be attributed.
        logger.info("Per-chat deposit addresses are OFF (DEPOSIT_SEED not set) — a "
                    "bare transfer with no reference/memo cannot be identified. "
                    'Generate one with: python3 -c "import secrets; '
                    'print(secrets.token_hex(32))"')
        if _deposit_addrs:
            # The seed derives addresses; it is not needed to WATCH them. Saying so
            # matters, because the alternative reading ("this is off now") would
            # leave a buyer who still holds one of those addresses uncredited.
            logger.warning("%d deposit address(es) from an earlier seed are still "
                           "watched, so payments already in flight are credited — "
                           "no NEW address is issued until DEPOSIT_SEED is set again",
                           len(_deposit_addrs))
    else:
        logger.info("Per-chat deposit addresses enabled — %d chat(s) already have one",
                    len(_deposit_addrs))
    known_sigs = _seed_known_sigs()
    # A signature only becomes "known" once it has been handled, and the set starts
    # from the receipt trail so a restart cannot re-credit what was already paid for.
    # Two consequences:
    # a getTransaction the public RPC rate-limited is retried for a few cycles
    # instead of dropped, and there is no blanket "first scan" skip any more —
    # that one marked the last 15 signatures as seen without looking at them, so
    # a payment that landed moments before a restart was orphaned forever.
    retry_counts: Dict[str, int] = {}
    # 20 cycles (~3.5 minutes at the 10 s poll) rather than 5: the first scan after a
    # restart now reads the TOKEN ACCOUNTS' history as well, which is a burst for the
    # public RPC to rate-limit — and a real payment that lands inside that burst must not
    # be dropped just because the endpoint was busy. Giving up is still bounded, and a
    # warning is logged, so nothing is retried forever.
    max_tx_tries = 20
    seen_balances: Dict[int, float] = {}      # chat → last observed deposit total
    next_balance_watch = 0.0                  # run the slow pass on the first cycle
    while True:
        try:
            sigs = await _signatures_for_watched(config.BOT_USDC_ADDRESS, known_sigs)
            if sigs:
                fresh = [s for s in sigs if str(s.get("signature")) not in known_sigs]
                for s in fresh:
                    sig = str(s.get("signature"))
                    # The transfer's own timestamp decides which invoices were
                    # open when the money moved — never "now", which would be
                    # wrong for anything the RPC delivered late.
                    tx_time = float(s.get("blockTime") or time.time())
                    if s.get("err"):
                        known_sigs.add(sig)   # failed on-chain: nothing to credit
                        continue
                    tx = await _sol_rpc("getTransaction", [
                        sig, {"encoding": "jsonParsed",
                              "maxSupportedTransactionVersion": 1}])
                    if not tx:
                        tries = retry_counts.get(sig, 0) + 1
                        if tries >= max_tx_tries:
                            known_sigs.add(sig)
                            retry_counts.pop(sig, None)
                            logger.warning("Solana tx unreadable after %d tries — "
                                           "giving up (sig=%s)", tries, sig[:12])
                        else:
                            retry_counts[sig] = tries
                            logger.info("Solana getTransaction failed (try %d/%d) — "
                                        "retrying next cycle (sig=%s)",
                                        tries, max_tx_tries, sig[:12])
                        continue
                    info = _tx_payment_info(tx)
                    if not info:
                        known_sigs.add(sig)
                        retry_counts.pop(sig, None)
                        # Token movement we could not read as a payment: exactly
                        # what a missed payment looks like from the outside, so
                        # leave a line in the log instead of failing silently.
                        if (tx.get("meta") or {}).get("postTokenBalances"):
                            logger.info("Solana tx moved tokens but held no accepted "
                                        "transfer for our wallet (sig=%s)", sig[:12])
                        continue
                    known_sigs.add(sig)
                    retry_counts.pop(sig, None)
                    amount, mint, memo = info
                    amount_micros = to_micros(amount)
                    # Reference first: it is the payer's real identifier and it
                    # survives a rounded amount, which the exact amount does not.
                    found = _match_by_reference(tx)
                    how = "reference"
                    if found is not None:
                        # The reference is public — it rides in the payment URL —
                        # so any transfer can carry it along as a read-only
                        # account. A two-decimal wallet may land up to one cent
                        # under the invoice (that is what this path is for), but
                        # anything further below is a dust transfer wearing the
                        # reference: refuse it instead of crediting.
                        owed = int(found[1].get("amount_micros") or 0)
                        if owed and amount_micros + 10_000 < owed:
                            logger.warning(
                                "Solana transfer of %.6f carried invoice %s's "
                                "reference but is far below the %.6f owed — "
                                "refused, not credited",
                                amount_micros / 1_000_000, found[0],
                                owed / 1_000_000)
                            continue
                    if found is None:
                        # The amount was the identifier before references existed:
                        # exact micro-USD match, in the pending pool first, then
                        # the grace pool (a late payment still counts). Memo, when
                        # present, is a fast path.
                        found = _match_payment(amount_micros, memo, tx_time)
                        how = "amount"
                    if found is None:
                        # Not the exact identifier — but it may still be this
                        # buyer: a two-decimal wallet drops the tail digits and
                        # people round the amount by hand. Only accepted
                        # when exactly one invoice was open at that moment.
                        found = _match_by_timing(amount_micros, tx_time)
                        if found is not None:
                            how = "timing"
                            logger.warning(
                                "Solana payment matched by TIMING, not by amount: "
                                "sent %.6f, the invoice was %.6f — wallet rounding "
                                "or a rounded amount", amount,
                                found[1].get("amount", 0.0))
                    if found is None:
                        if amount_micros in _used_amounts:
                            logger.warning(
                                "Duplicate/late transfer for an already-paid "
                                "invoice: %.6f %s sig=%s", amount,
                                config.ACCEPTED_MINTS.get(mint, mint), sig)
                        else:
                            # No memo: it is payer-written free text. The signature
                            # already identifies the transfer for reconciliation,
                            # and the chain carries the memo publicly anyway.
                            logger.info("Solana transfer without matching invoice: "
                                        "%.6f %s sig=%s", amount,
                                        config.ACCEPTED_MINTS.get(mint, mint), sig)
                        continue
                    pid, target = found
                    chat_id = target["chat_id"]
                    _pending_payments.pop(pid, None)
                    _expired_payments.pop(pid, None)
                    retire_amount(target, amount_micros)
                    await _activate_pro(chat_id, amount, mint, sig, pid,
                                        matched_by=how)
                if len(known_sigs) > _SIG_WINDOW_TRIM_AT:
                    # Drop the OLDEST, not an arbitrary half: this used to slice a
                    # set, whose iteration order is hash order, so the survivors
                    # were a random subset and the dedup window did not mean
                    # "recent" at all. Membership and dedup are unchanged either
                    # way — the durable guards are the popped invoice and the
                    # receipt trail (_credited_signatures), not this table.
                    known_sigs.trim(_SIG_WINDOW_KEEP)
            # Manual/exchange payments: one poll per chat address with a live
            # invoice. Separate from the wallet above, because there the address
            # itself is the identifier — no reference and no amount match involved.
            for deposit_address, deposit_chat in _deposit_poll_targets():
                await _scan_deposit_address(deposit_address, deposit_chat, known_sigs)
            # Slow cadence, every registered address: did money land while no invoice
            # was open? Two balance calls each, and signatures are paged only when a
            # balance has actually moved — the cheap way to close that blind spot.
            if time.time() >= next_balance_watch:
                next_balance_watch = time.time() + _DEPOSIT_BALANCE_WATCH_SECONDS
                await _deposit_balance_watch(known_sigs, seen_balances)
            # Expire stale invoices — into the grace pool, not oblivion: their
            # unique amount stays matchable so a late payment is still honoured.
            now = time.time()
            for pid in [p for p, v in _pending_payments.items()
                        if now - v["created"] > config.PAYMENT_EXPIRY_SECONDS]:
                park_invoice(pid, _pending_payments.pop(pid))
                logger.info("Solana invoice %s expired unpaid (amount stays matchable "
                            "for %d h)", mask_invoice_id(pid),
                            config.PAYMENT_GRACE_SECONDS // 3600)
            for pid in [p for p, v in _expired_payments.items()
                        if now - v.get("expired_at", v.get("created", now))
                        > config.PAYMENT_GRACE_SECONDS]:
                _expired_payments.pop(pid, None)
                _mark_payments_dirty()
            # Once no transfer of a consumed amount can still arrive, let it be
            # handed to a new invoice again.
            for micros in [m for m, at in _used_amounts.items()
                           if now - at > config.PAYMENT_GRACE_SECONDS]:
                _used_amounts.pop(micros, None)
                _mark_payments_dirty()
            # One write per cycle at most — the pools only change on a payment,
            # an expiry or a prune, and a restart must find them on disk.
            if _payments_dirty:
                save_pending_payments()
            await asyncio.sleep(config.SOL_POLL_SECONDS)
        except Exception as e:
            logger.error("Solana monitor cycle error: %s", e)
            await botstate.notify_admins(
                "⚠️ The Solana payment monitor hit an unexpected error — open "
                "invoices are not being watched right now, so a payment may go "
                "uncredited until it recovers. See tracker.log.", key="solana-monitor")
            await asyncio.sleep(20)


async def _pro_expiry_task() -> None:
    """Periodic check: demote expired pro users, warn ONCE on the last day."""
    while True:
        try:
            now = time.time()
            for chat_id in list(_pro_users.keys()):
                exp = _pro_users[chat_id]
                if exp <= now:
                    _pro_users.pop(chat_id, None)
                    _expiry_warned.pop(chat_id, None)
                    save_pro_users()
                    save_expiry_warnings()
                    logger.info("Pro expired for chat=%s — back to free tier",
                                storage.mask_chat(chat_id))
                    if botstate.bot is not None:
                        try:
                            await botstate.bot.send_message(
                                chat_id,
                                "⚠️ Your Pro subscription expired — back on the free "
                                f"tier ({config.FREE_PACKAGE_LIMIT} packages).\n"
                                "Tap Upgrade in the Mini App to renew! 🌟")
                        except Exception:
                            pass
                elif exp - now < 86400 and _expiry_warned.get(chat_id) != exp:
                    # The last day only, and only once per subscription: the old
                    # 3-day warning nagged on every process run across three
                    # days (operator: "annoying — just once, on the last day").
                    # "Today" rather than a day count — with under 24h left,
                    # "1 day(s)" read like two more full days of buffer.
                    # Keyed by the expiry and persisted, so a restart inside the
                    # last day can no longer re-send it (operator: restarts must
                    # never re-notify). A renewal's new expiry is a fresh key.
                    _expiry_warned[chat_id] = exp
                    save_expiry_warnings()
                    if botstate.bot is not None:
                        try:
                            await botstate.bot.send_message(
                                chat_id,
                                "⏳ Your Pro subscription expires today. "
                                "Renew from the Mini App to keep your limits! 🌟")
                        except Exception:
                            pass
            await asyncio.sleep(900)
        except Exception as e:
            logger.error("Pro expiry task error: %s", e)
            await asyncio.sleep(300)