"""Disk persistence + data-at-rest encryption for the tracker bot.

Extracted from main.py (Session 2 of EXTRACTION_PLAN.md): everything that
reads/writes the local JSON files — the Fernet encryption helpers, the
track_sessions / cooldown / confirmation stores, and the time-formatting
helpers used by the cards.

main.py re-imports every name here, so call sites (and tests) keep working
unchanged. Storage imports nothing from main — config.py only, plus stdlib.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import config  # .env is loaded here; _bot_token is set at bot startup

logger = logging.getLogger("tracker")

# ---- Data-at-rest encryption (Fernet: AES-128-CBC + HMAC-SHA256) ----
# The master key is DATA_ENCRYPTION_KEY when set (recommended: the store no
# longer depends on the bot token, so rotating the token can't brick it and a
# leaked token can't decrypt the disk), otherwise the bot token as before.
# Files encrypted under the *other* key stay readable — they're re-encrypted
# under the active one on first read — which makes setting the env var a
# transparent migration. With no key material at all we write plaintext and say
# so loudly: a hardcoded constant was worse than nothing, because it made the
# data look encrypted. If the cryptography package is missing, the bot falls
# back to plaintext writes and never destroys files it cannot decrypt.
try:
    from cryptography.fernet import Fernet as _Fernet
    _ENCRYPTION_AVAILABLE = True
except ImportError:
    _ENCRYPTION_AVAILABLE = False

_KEY_PURPOSE = "glm-enc:"      # domain separation for the KDF
_warned_no_key = False


def _fernet_for(secret):
    if not _ENCRYPTION_AVAILABLE or not secret:
        return None
    key = base64.urlsafe_b64encode(
        hashlib.sha256((_KEY_PURPOSE + secret).encode()).digest())
    return _Fernet(key)


def _master_secret():
    """What NEW writes are encrypted with: DATA_ENCRYPTION_KEY, else the bot
    token (pre-separation behaviour), else nothing (plaintext + warning)."""
    global _warned_no_key
    if config.DATA_ENCRYPTION_KEY:
        return config.DATA_ENCRYPTION_KEY
    token = config._bot_token or os.getenv("TELEGRAM_BOT_API_TOKEN")
    if token:
        return token
    if not _warned_no_key:
        _warned_no_key = True
        logger.warning("Neither DATA_ENCRYPTION_KEY nor a bot token is available: "
                       "data files are PLAINTEXT at rest. Set DATA_ENCRYPTION_KEY in .env.")
    return None


def _legacy_secret():
    """The bot token — the only key files written before key separation can be
    under (config._bot_token is kept in sync by main() at startup)."""
    return config._bot_token or os.getenv("TELEGRAM_BOT_API_TOKEN")


def _get_fernet():
    return _fernet_for(_master_secret())


def _decrypt_with(secret, raw):
    f = _fernet_for(secret)
    if f is None:
        return None
    try:
        return f.decrypt(raw).decode()
    except Exception:
        return None


def _needs_rekey(raw) -> bool:
    """True when these bytes open under the legacy bot-token key but not under
    the active master key — i.e. the file predates DATA_ENCRYPTION_KEY."""
    master, legacy = _master_secret(), _legacy_secret()
    if not legacy or legacy == master:
        return False
    if _decrypt_with(master, raw) is not None:
        return False
    return _decrypt_with(legacy, raw) is not None


def _rewrap_envelopes(text, extra_keys: tuple = ()):
    """Text → text with every per-chat DEK re-wrapped under the ACTIVE master key.

    A v2 container is double-wrapped: the master seals the file, and a freshly
    generated key (DEK) seals each chat's blob. Re-keying only the outer layer —
    what this did before — left the DEKs sealed by the key we were migrating
    AWAY from, so the next load unwrapped them with the new master, failed, and
    the store failed closed. That is an outage, not a migration (it happened
    once, and had to be repaired by hand).

    Returns the text with nothing to re-wrap for v1 / plaintext payloads, or
    None when the file could NOT be fully re-wrapped — in which case the caller
    must not write, so the old key keeps opening it.

    extra_keys: additional masters that may have sealed the DEKs, tried after
    the active key. A DATA_ENCRYPTION_KEY → NEW DATA_ENCRYPTION_KEY rotation is
    exactly the case the (master, legacy-token) pair cannot cover — the
    DEK-to-DEK rewrap needs the key being rotated AWAY from, passed explicitly.
    """
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if not isinstance(obj, dict) or obj.get("v") != _ENVELOPE_VERSION:
        return text
    chats = obj.get(_ENVELOPE_KEY)
    if not isinstance(chats, dict):
        return text
    master = _get_fernet()
    if master is None:
        return None                     # refuse: no key → a write would be plaintext
    unwrap_keys = [master]
    unwrap_keys += [_fernet_for(k) for k in extra_keys if k]
    legacy = _fernet_for(_legacy_secret())
    for envelope in chats.values():
        if not isinstance(envelope, dict):
            return None
        wrapped = envelope.get("dek") or ""
        dek = None
        for f in unwrap_keys:
            if f is None:
                continue
            dek = _unwrap_dek(f, wrapped)
            if dek is not None:
                break
        if dek is None and legacy is not None:
            dek = _unwrap_dek(legacy, wrapped)      # sealed by the key we leave
        if dek is None:
            logger.warning("Re-key: a chat data key opens under neither the old nor "
                           "the new master — leaving this file untouched")
            return None
        envelope["dek"] = master.encrypt(dek).decode()
    return json.dumps(obj, indent=4)


def _rekey_file(path, text):
    """Transparent one-time migration to the active key. Never fatal: the data
    is already decrypted in memory, so a failure only means we retry next read.

    Returns the text that is now on disk — the caller must parse the RE-WRAPPED
    payload, not the one it handed in: the incoming text still carries DEKs
    sealed by the old key, so parsing it under the new master is exactly the
    unwrap failure this migration exists to prevent (found by the rekey test
    suite; without this the migration read itself failed closed).

    Writes nothing unless the whole file could be re-wrapped: re-encrypting the
    outer layer while a DEK stays broken would make the file unreadable to the
    OLD key too, turning a recoverable file into a dead one.
    """
    try:
        rewrapped = _rewrap_envelopes(text)
        if rewrapped is None:
            return text
        _write_protected(path, rewrapped)
        logger.info("RE-KEYED %s: encrypted under DATA_ENCRYPTION_KEY now — the bot "
                    "token no longer opens it", path.name)
        return rewrapped
    except Exception as e:      # never fatal: the old key still opens the file
        logger.warning("Could not re-key %s: %s", path.name, e)
        return text


# ---- Per-chat envelopes (v2 container for track_data.json) ----
# Each chat's packages get their own random data key, wrapped by the master and
# stored beside the blob *inside* the master-encrypted file. The master still
# opens everything (the server must decrypt to poll in the background), so this
# is blast-radius control, not a wall: a bug that serializes one chat's payload
# somewhere, or a partial copy of the file, leaks ciphertext under a key that
# lives only in that file. Deleting a chat drops its key with its blob.
# Files written by earlier versions (a flat {chat_id: pkgs} mapping) load
# unchanged and are upgraded on the next save; a rollback to older code needs
# the .v1.bak this upgrade writes first.
_ENVELOPE_VERSION = 2
_ENVELOPE_KEY = "chats"


class _EnvelopeError(Exception):
    """A chat envelope that exists but cannot be opened."""


def _build_container() -> dict:
    """The JSON payload for track_data.json: v2 envelopes when we have both
    crypto and a key, otherwise the v1 flat mapping (plaintext path)."""
    master = _get_fernet()
    if master is None:
        return dict(track_sessions)
    chats = {}
    for chat_id, pkgs in track_sessions.items():
        dek = _Fernet.generate_key()                       # fresh per save, per chat
        blob = _Fernet(dek).encrypt(json.dumps(pkgs).encode())
        chats[str(chat_id)] = {
            "dek": master.encrypt(dek).decode(),           # wrapped by the master
            "blob": blob.decode(),
        }
    return {"v": _ENVELOPE_VERSION, _ENVELOPE_KEY: chats}


def _parse_container(obj):
    """Container JSON → {chat_id: pkgs}, accepting v2 envelopes and the v1 flat
    mapping. Raises _EnvelopeError when an envelope can't be opened (the caller
    must preserve the file rather than start fresh)."""
    if not isinstance(obj, dict):
        raise ValueError("not a JSON object")
    if obj.get("v") == _ENVELOPE_VERSION and isinstance(obj.get(_ENVELOPE_KEY), dict):
        master = _get_fernet()
        if master is None:
            raise _EnvelopeError("envelopes present but no key to unwrap them")
        loaded = {}
        for chat_id, envelope in obj[_ENVELOPE_KEY].items():
            dek = _unwrap_dek(master, (envelope or {}).get("dek", ""))
            if dek is None:
                raise _EnvelopeError(f"chat {chat_id}: data key does not unwrap")
            try:
                pkgs = json.loads(_Fernet(dek).decrypt((envelope.get("blob") or "").encode()).decode())
            except Exception:
                raise _EnvelopeError(f"chat {chat_id}: blob does not decrypt")
            loaded[int(chat_id)] = pkgs
        return loaded
    # v1: a flat {chat_id: pkgs} mapping (JSON keys are strings).
    return {int(chat_id): pkgs for chat_id, pkgs in obj.items()}


def _unwrap_dek(master, wrapped):
    try:
        return master.decrypt(wrapped.encode())
    except Exception:
        return None


def mask_chat(chat_id) -> str:
    """A stable, non-reversible tag for logs.

    Chat IDs are personal identifiers; a plaintext log shouldn't accumulate
    them. The same chat always maps to the same tag, so one chat's story stays
    followable across lines — without the raw ID being recoverable. Keyed by
    the master secret, so tags change if the key changes (old lines can't be
    correlated with new ones, which is the trade for not storing a reverse map).
    """
    secret = _master_secret() or "no-key"
    digest = hmac.new(secret.encode(), f"chat:{chat_id}".encode(), hashlib.sha256).hexdigest()
    return "chat:" + digest[:8]


def _backup_v1_once(data_file) -> None:
    """Before the first v2 write, keep the v1 ciphertext as .v1.bak (still
    encrypted — no plaintext copy) so a code rollback can still read the data."""
    bak = data_file.with_suffix(".v1.bak")
    if bak.exists() or not data_file.exists():
        return
    raw = data_file.read_bytes()
    if not raw.startswith(b"gAAAA"):
        return                                   # plaintext path: nothing to keep
    text = _decrypt_payload(raw)
    if text is None:
        return
    try:
        if json.loads(text).get("v") == _ENVELOPE_VERSION:
            return                               # already v2 (crash before write?)
    except (json.JSONDecodeError, ValueError, AttributeError):
        return
    try:
        bak.write_bytes(raw)
        os.chmod(bak, 0o600)
        logger.info("Kept the pre-envelope copy as %s (delete once you're happy)", bak.name)
    except OSError as e:
        logger.warning("Could not keep %s: %s", bak.name, e)


def _is_ciphertext(path) -> bool:
    """True when the file on disk really starts with a Fernet token prefix.

    Migration logs must only claim "encrypted at rest" after inspecting the
    bytes that actually landed: the old code logged it unconditionally, which
    hid a broken key derivation that was silently writing plaintext.
    """
    try:
        return path.read_bytes()[:5] == b"gAAAA"
    except OSError:
        return False


def _encrypt_payload(text):
    f = _get_fernet()
    data = text.encode()
    return f.encrypt(data) if f is not None else data

def _decrypt_payload(raw):
    # Fernet-decrypt raw bytes with the active key, falling back to the legacy
    # bot-token key so files from before key separation stay readable. Returns
    # None when undecryptable.
    master = _master_secret()
    text = _decrypt_with(master, raw)
    if text is not None:
        return text
    legacy = _legacy_secret()
    if legacy and legacy != master:
        return _decrypt_with(legacy, raw)
    return None

def _write_protected(path, text):
    # Atomic write: encrypted when available, always chmod 0600.
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        fh.write(_encrypt_payload(text))
    os.chmod(tmp, 0o600)
    tmp.replace(path)

# Names of state files found encrypted but undecryptable (preserved as .locked).
# The loaders deliberately carry on with an empty store rather than refusing to
# start, so without this record that loss would be silent — main() reports it to
# the operator once, at startup.
locked_preserved: List[str] = []

# Set by read-only invocations (run.sh --status probes) so _read_protected
# reports instead of repairing: no .locked rename, no rekey write. A status
# command that "fixes" a state file as a side effect breaks its own promise.
READ_ONLY = False


def _preserve_path(path, tag: str):
    """A preserve name that can never overwrite an earlier preserve. The old
    fixed suffixes (.locked / .corrupt) meant a SECOND incident silently
    replaced the first one's evidence — the operator kept one file believing
    it was the only one. Timestamped, with a collision counter for two
    incidents inside the same second."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    cand = path.with_name(path.name + f".{tag}-{ts}")
    n = 2
    while cand.exists():
        cand = path.with_name(path.name + f".{tag}-{ts}-{n}")
        n += 1
    return cand


def _read_protected(path):
    # Returns (text, was_encrypted). text is None when unreadable.
    raw = path.read_bytes()
    text = _decrypt_payload(raw)
    if text is not None:
        if _needs_rekey(raw) and not READ_ONLY:
            # Parse what was just written, not the pre-migration text.
            text = _rekey_file(path, text)
        return text, True
    if raw.startswith(b"gAAAA"):
        if READ_ONLY:
            # Report-only: the caller decides how to degrade. The rename is
            # deferred to a real (non read-only) process that owns the file.
            return None, True
        locked = _preserve_path(path, "locked")
        path.replace(locked)
        locked_preserved.append(path.name)
        raise SystemExit(
            "ERROR: " + path.name + " is encrypted but cannot be decrypted; "
            "preserved as " + locked.name + ". "
            "Install cryptography or restore the original bot token.")
    try:
        return raw.decode(), False
    except (UnicodeDecodeError, ValueError):
        return None, False
# ---------------------------------------------
# 17TRACK registration cooldown persistence
# ---------------------------------------------
# Tracking numbers 17TRACK refused with the pre-first-scan policy error.
# Value = epoch seconds when registration may be attempted again (wall-clock,
# so the cooldown survives restarts; monotonic clocks aren't comparable
# across processes).
_register_blocked_until: Dict[str, float] = {}


def _registration_on_cooldown(tracking_id: str) -> bool:
    return time.time() < _register_blocked_until.get(tracking_id, 0.0)


def _prune_cooldown():
    now = time.time()
    for tid in [t for t, until in _register_blocked_until.items() if until <= now]:
        _register_blocked_until.pop(tid, None)


def save_cooldown(cooldown_file):
    # Persist registration cooldowns so restarts do not re-hammer 17TRACK
    # with doomed register+lookup pairs for pre-first-scan numbers.
    _prune_cooldown()
    try:
        _write_protected(cooldown_file, json.dumps(_register_blocked_until, indent=2))
    except OSError as e:
        logger.warning("Could not save registration cooldown: %s", e)

def load_cooldown(cooldown_file):
    # Restore persisted cooldown expiry timestamps that have not hit yet.
    if not cooldown_file.exists():
        return
    try:
        text, was_enc = _read_protected(cooldown_file)
    except SystemExit:
        # Ciphertext under a different key (old/wrong token): _read_protected
        # already preserved the file as .locked. Cooldowns are disposable
        # retry timers — warn and start fresh instead of refusing to start.
        # (track_data.json keeps the strict behavior: precious data fails fast.)
        logger.warning("register_cooldown.json unreadable (wrong key?) - preserved as .locked, starting with empty cooldowns")
        return
    if text is None:
        logger.warning("Could not read registration cooldown file - ignoring")
        return
    try:
        raw = json.loads(text)
        now = time.time()
        for tid, until in raw.items():
            until = float(until)
            if until > now:
                _register_blocked_until[tid] = until
        if _ENCRYPTION_AVAILABLE and not was_enc:
            save_cooldown(cooldown_file)
            if _is_ciphertext(cooldown_file):
                logger.info("MIGRATED: register_cooldown.json is now encrypted at rest")
            else:
                logger.warning("register_cooldown.json still PLAINTEXT at rest - "
                               "check _get_fernet()")
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Could not load registration cooldown file: %s", e)

# ---------------------------------------------
# Persistence
# ---------------------------------------------
# Single shared dict: main.py imports this exact object, so readers see
# every update. load_data() mutates it in place — never rebind it.
track_sessions: Dict[int, Dict[str, dict]] = {}
CONFIRM_TTL = 900  # pending confirmations expire after 15 minutes
pending_confirmations: Dict[tuple, Tuple[int, float]] = {}  # key -> (user_id, ts)

def load_data(data_file):
    # Loads Fernet-encrypted storage and transparently migrates legacy
    # plaintext track_data.json on first run. JSON turns dict keys into
    # strings, so chat_id is converted back to int. Also applies schema
    # migrations from older bot versions.
    if not data_file.exists():
        return
    text, was_enc = _read_protected(data_file)
    if text is None:
        if READ_ONLY:
            # A status probe reports counts, never repairs: degrade to "?"
            # in the caller and leave the file exactly as it is.
            raise RuntimeError("read-only load: " + data_file.name + " is unreadable")
        backup = _preserve_path(data_file, "corrupt")
        data_file.replace(backup)
        logger.warning("track_data.json unreadable - backed up to %s, starting fresh", backup)
        return
    try:
        raw = json.loads(text)
        loaded = _parse_container(raw)
    except _EnvelopeError as e:
        if READ_ONLY:
            raise RuntimeError(
                "read-only load: " + data_file.name + " holds an envelope this build "
                "cannot open (" + str(e) + ")")
        # An envelope we can't open means a key problem, not corrupt data:
        # preserve the file and stop, never start fresh over the owner's data.
        backup = _preserve_path(data_file, "corrupt")
        data_file.replace(backup)
        raise SystemExit(
            "ERROR: track_data.json holds an envelope this build cannot open (" + str(e) +
            "); preserved as " + backup.name + ". " +
            "If DATA_ENCRYPTION_KEY changed, restore the old one to read it.")
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        if READ_ONLY:
            raise RuntimeError(
                "read-only load: " + data_file.name + " is unreadable (" + str(e) + ")")
        backup = _preserve_path(data_file, "corrupt")
        data_file.replace(backup)
        logger.warning("track_data.json unreadable (%s) - backed up to %s, starting fresh", e, backup)
        return
    # In-place mutation keeps main.track_sessions the SAME object.
    track_sessions.clear()
    track_sessions.update(loaded)
    # Migrations:
    # 1) Old "Awaiting First Scan" status becomes Pending + blocked flag.
    # 2) Legacy schema fields (status_source / lookup_ok / diagnostic) fold into source.
    for pkgs in track_sessions.values():
        for info in pkgs.values():
            if info.get("status") == "Awaiting First Scan":
                info["status"] = "Pending"
                info["register_blocked"] = True
                info["source"] = info.get("carrier_name", "Unknown")
            legacy_src = info.get("status_source")
            if legacy_src and legacy_src != "none" and not info.get("source"):
                info["source"] = legacy_src
            for old_key in ("status_source", "lookup_ok", "diagnostic"):
                info.pop(old_key, None)
    if _ENCRYPTION_AVAILABLE and not was_enc and not READ_ONLY:
        bak = None
        try:
            bak = data_file.with_suffix(".plaintext.bak")
            bak.write_text(text)
            os.chmod(bak, 0o600)
        except OSError:
            pass
        save_data(data_file)
        if not _is_ciphertext(data_file):
            logger.warning("track_data.json still PLAINTEXT at rest - encryption "
                           "unavailable or the key derivation failed; check _get_fernet()")
            return
        # The plaintext copy existed for the window in which the migration
        # could still have gone wrong. Once the store reads back cleanly from
        # disk, keeping it is pure downside: a plaintext copy of every chat
        # and package, one careless `cp` away from leaving the machine. So
        # verify by round-trip, then delete it instead of nagging the operator
        # to (M6).
        try:
            round_trip = _decrypt_payload(data_file.read_bytes())
            if round_trip is not None and json.loads(round_trip) is not None:
                if bak is not None and bak.exists():
                    try:
                        os.remove(bak)
                        logger.info("MIGRATED: track_data.json is now encrypted at "
                                    "rest; the plaintext copy was verified and deleted")
                    except OSError:
                        logger.warning("MIGRATED: encrypted at rest, but the plaintext "
                                       "copy could not be deleted - remove %s manually", bak)
            else:
                logger.warning("MIGRATED: encrypted at rest, but the round-trip read "
                               "failed - keeping the plaintext copy %s", bak)
        except (OSError, ValueError):
            logger.warning("MIGRATED: encrypted at rest, but verification failed - "
                           "keeping the plaintext copy %s", bak)

def save_data(data_file):
    # Atomic write - a crash mid-write cannot corrupt the real file.
    # Encrypted at rest when crypto is available; temp file is always
    # 0600 because it holds tracking numbers and package names.
    _backup_v1_once(data_file)      # keep a readable-by-old-code copy, once
    _write_protected(data_file, json.dumps(_build_container(), indent=4))
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_ts(iso_ts: Optional[str]) -> str:
    """Human-readable local time for a stored ISO timestamp."""
    if not iso_ts:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:  # timestamps written by older versions were naive UTC
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone()
    except (ValueError, TypeError):
        return "-"
    now = datetime.now().astimezone()
    fmt = "%d %b, %H:%M" if dt.year == now.year else "%d %b %Y, %H:%M"
    return dt.strftime(fmt)


def _trim_sentence(text: Optional[str], max_len: int = 170) -> str:
    """Tidy a status detail for display: collapse whitespace, strip USPS
    widget promo/UI markers, capitalize, end with a period, and cut on a
    sentence boundary."""
    if not text:
        return ""
    text = " ".join(text.split()).strip(" ,;:")
    for marker in ("Get More Out of USPS Tracking", "USPS Tracking Plus",
                   "See Less", "Track Another Package"):
        i = text.find(marker)
        if i != -1:
            text = text[:i]
    text = text.strip(" ,;:")
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if not text.endswith((".", "!", "?")):
        text += "."
    if len(text) > max_len:
        cut = text.rfind(".", 0, max_len)
        if cut != -1:
            text = text[: cut + 1]
        else:
            cut = text.rfind(" ", 0, max_len)
            text = (text[:cut] + "\u2026") if cut != -1 else text[:max_len] + "\u2026"
    return text


def time_since(iso_ts: Optional[str]) -> str:
    """Human-readable age of a stored timestamp, for /list display."""
    if not iso_ts:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    except (ValueError, TypeError):
        return "unknown"
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def store_confirmation(key: tuple, user_id: int):
    prune_confirmations()
    pending_confirmations[key] = (user_id, time.monotonic())


def get_confirmation(key: tuple) -> Optional[int]:
    entry = pending_confirmations.get(key)
    if entry is None:
        return None
    user_id, ts = entry
    if time.monotonic() - ts > CONFIRM_TTL:
        del pending_confirmations[key]
        return None
    return user_id


def drop_confirmation(key: tuple):
    pending_confirmations.pop(key, None)


def prune_confirmations():
    now = time.monotonic()
    for key, (_, ts) in list(pending_confirmations.items()):
        if now - ts > CONFIRM_TTL:
            del pending_confirmations[key]


# ──────────────────────────────────────────────
# Per-chat add locks
# ──────────────────────────────────────────────
# Both add paths (main.track_handler and miniapp._api_add) check the package
# limit, await a network lookup, then insert. Two requests for the same chat
# interleaved across that await both pass the check and both insert, so the cap
# is exceeded and a duplicate 17TRACK registration (or paid fallback) is burned.
# One lock per chat, so unrelated chats never wait on each other, and it lives
# here because storage is the one module both entry points already import.
# The table is capped: without it, every chat that ever added a package left a
# lock behind forever (one small lock each, but nothing ever removed one). The
# cap evicts IDLE locks only — see _ChatLock for why that qualifier is load-bearing.
_CHAT_LOCK_CAP = 512


class _ChatLock:
    """An asyncio.Lock that knows whether anyone is holding or waiting for it.

    Eviction is only safe on a provably idle lock. A plain `lock.locked()` test
    is not enough: asyncio.Lock.release() clears the flag and only THEN wakes the
    first waiter, so there is a window where `locked()` reads False while a
    coroutine is still queued. Evicting inside that window hands the next caller
    a SECOND lock for the same chat — two coroutines inside the "atomic" section
    this table exists to serialize (the over-the-cap plus duplicate-17TRACK-
    registration race the comment above describes). Counting users BEFORE the
    acquire closes that window: a queued waiter keeps the count above zero, so
    the lock it is waiting on can never be taken away from under it.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._users = 0

    async def __aenter__(self):
        self._users += 1
        await self._lock.acquire()
        return self

    async def __aexit__(self, *exc):
        self._lock.release()
        self._users -= 1
        return False

    @property
    def idle(self) -> bool:
        return self._users == 0


chat_locks: Dict[int, _ChatLock] = {}


def get_chat_lock(chat_id: int) -> _ChatLock:
    """The add/limit lock for one chat, created on first use.

    Call it from inside the running loop (both callers are handlers), so the
    lock binds to the loop that will actually use it.
    """
    lock = chat_locks.get(chat_id)
    if lock is None:
        if len(chat_locks) >= _CHAT_LOCK_CAP:
            # Evict one idle entry to make room. If every lock is in use we grow
            # past the cap instead: dropping a HELD lock would trade a slow leak
            # for exactly the race this table exists to prevent, and an extra
            # lock is far cheaper than that.
            for cid, candidate in list(chat_locks.items()):
                if candidate.idle:
                    del chat_locks[cid]
                    break
        lock = chat_locks[chat_id] = _ChatLock()
    return lock