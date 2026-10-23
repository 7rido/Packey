"""Shared tracking links: unguessable public URLs for one package's status.

A link IS the authentication — whoever holds the URL can read that one
package's name, status and latest update with no Telegram account. So the token
is 128 bits from `secrets` (never a guessable id), there is one per
(chat, package) — minting again kills the previous link, so a leaked URL dies
the moment the user shares a new one — and it expires on its own.

128 bits and not more, because the token must also fit Telegram's 64-byte
callback_data cap on the Revoke button: "share_revoke:" eats 13 of those bytes
and hex doubles the rest, so a 256-bit token built a 77-byte callback and the
whole Share reply was rejected with BUTTON_DATA_INVALID (the link existed, but
the message carrying it never sent). 2^128 guesses is still far past the point
where brute-forcing a URL is a thing.

What a link can never show: the tracking number, the chat id, or anything about
another package. The page builds an explicit allowlist of fields; the stored
record is never handed to the renderer as-is.

Storage is a small encrypted sidecar (share_links.json) with the same tolerant
load as register_cooldown.json: a link list is disposable, so an unreadable
file warns and starts empty rather than refusing to start the bot.
"""
import json
import logging
import secrets
import time
from typing import Dict, Optional

import config
import storage

logger = logging.getLogger("tracker")

# 128-bit hex token = 32 chars, so "share_revoke:" + token is 45 bytes: inside
# Telegram's 64-byte callback_data cap, which a 256-bit token blew by 13 bytes.
# Tokens minted before the shrink stay valid until they expire — lookup() is
# length-agnostic, so there is nothing to migrate.
TOKEN_BYTES = 16
# token -> {chat_id, tracking_id, created_at, expires_at}. Epoch SECONDS, not
# monotonic: expiry must survive a restart, and monotonic clocks aren't
# comparable across processes (same reasoning as the registration cooldown).
_tokens: Dict[str, dict] = {}


def _prune_expired() -> int:
    """Drop tokens whose expiry has passed. Expiry is what makes a dead link
    dead even if nobody ever opens it again."""
    now = time.time()
    dead = [t for t, rec in _tokens.items() if rec.get("expires_at", 0) <= now]
    for token in dead:
        _tokens.pop(token, None)
    return len(dead)


def save() -> None:
    _prune_expired()          # an expired link is not worth persisting
    try:
        storage._write_protected(config.SHARE_LINKS_FILE,
                                 json.dumps(_tokens, indent=2))
    except OSError as e:
        logger.warning("Could not save share links: %s", e)


def load() -> None:
    """Restore the links that have not expired (called once at startup)."""
    path = config.SHARE_LINKS_FILE
    if not path.exists():
        return
    try:
        text, was_enc = storage._read_protected(path)
    except SystemExit:
        # Wrong key: _read_protected already preserved the file as .locked.
        # Links expire by themselves, so start with none instead of refusing to
        # start (track_data.json keeps the strict behavior: precious data fails
        # fast, disposable timers do not).
        logger.warning("share_links.json unreadable (wrong key?) - preserved as "
                       ".locked, starting with no share links")
        return
    if text is None:
        logger.warning("Could not read share_links.json - ignoring")
        return
    try:
        raw = json.loads(text)
        now = time.time()
        for token, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            if float(rec.get("expires_at", 0)) <= now:
                continue              # already expired: don't resurrect it
            rec["chat_id"] = int(rec["chat_id"])
            _tokens[token] = rec
        if storage._ENCRYPTION_AVAILABLE and not was_enc:
            save()
            if storage._is_ciphertext(path):
                logger.info("MIGRATED: share_links.json is now encrypted at rest")
            else:
                logger.warning("share_links.json still PLAINTEXT at rest - "
                               "check _get_fernet()")
    except (json.JSONDecodeError, ValueError, TypeError, KeyError) as e:
        logger.warning("Could not load share_links.json: %s", e)


def _expiry_from_now() -> float:
    """When a link minted now stops working. SHARE_LINK_TTL_DAYS=0 means links
    never expire (the same "0 disables the mechanism" convention as
    RETENTION_DAYS / LOST_AFTER_DAYS / EPHEMERAL_TTL)."""
    days = config.SHARE_LINK_TTL_DAYS
    if days <= 0:
        return float("inf")
    return time.time() + days * 86400


def mint(chat_id: int, tracking_id: str) -> str:
    """Create a link for this package, revoking any earlier one for it."""
    revoke_for(chat_id, tracking_id)
    token = secrets.token_hex(TOKEN_BYTES)
    _tokens[token] = {
        "chat_id": int(chat_id),
        "tracking_id": tracking_id,
        "created_at": time.time(),
        "expires_at": _expiry_from_now(),
    }
    save()
    return token


def live_for(chat_id: int, tracking_id: str) -> Optional[str]:
    """The live token for this (chat, package), or None. At most one exists —
    mint() revokes any earlier one for the pair.

    Why the app needs this and the bot does not: the bot's Share REPLACES on
    every tap (a chat message cannot be edited, so replacing is its only revoke),
    but the app SHOWS the link in a sheet — re-opening it must not silently kill
    a link the user may already have sent to someone.
    """
    now = time.time()
    for token, rec in _tokens.items():
        if (int(rec.get("chat_id", 0)) == int(chat_id)
                and rec.get("tracking_id") == tracking_id
                and float(rec.get("expires_at", 0)) > now):
            return token
    return None


def revoke_for(chat_id: int, tracking_id: str) -> int:
    """Drop every link this chat holds for this package (one active link)."""
    dead = [t for t, rec in _tokens.items()
            if int(rec.get("chat_id", 0)) == int(chat_id)
            and rec.get("tracking_id") == tracking_id]
    for token in dead:
        _tokens.pop(token, None)
    return len(dead)


def lookup(token: str) -> Optional[dict]:
    """The live record for a token, or None for unknown/expired/revoked.

    Expiry is checked on every read (lazy prune, like _prune_cooldown): a link
    dies exactly when it says it does, with no background job.
    """
    rec = _tokens.get(token or "")
    if rec is None:
        return None
    if float(rec.get("expires_at", 0)) <= time.time():
        _tokens.pop(token, None)
        return None
    return rec


def revoke(token: str) -> bool:
    """Delete one link. False when it was already unknown or expired."""
    return _tokens.pop(token or "", None) is not None
