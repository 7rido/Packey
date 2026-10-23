"""Single owner of the live Bot handle for the tracker bot.

Before this module, pro.py and ui.py each kept their own `bot` global plus
a set_bot(); refresh.py would have added a third. All three receive the same
object from main(), so one handle + one setter replaces the duplicates.
Read sites use `botstate.bot`; main() calls `botstate.set_bot(bot)` once.

It also carries the operator alert helper and the startup update notice, for the
same reason: the startup checks and the background loops all need to say something,
and a second copy of that logic per module is a second thing to get wrong.
"""
import asyncio
import hashlib
import json
import logging
import time
from typing import Dict, Optional

import httpx
from aiogram import Bot

import config
import storage

logger = logging.getLogger(__name__)

# Set once by main() at startup; None until then (and in tests).
bot: Optional[Bot] = None


def set_bot(b: Optional[Bot]) -> None:
    """main() hands over the live Bot after creating it."""
    global bot
    bot = b


# ── startup update notice ───────────────────────────────────────────────────
# The operator asked for the bot to say when it comes up with something new
# (2026-09-26): a restart, new code, a new Mini App URL. The state file is what
# makes "new" mean anything across restarts — an in-memory record cannot survive
# the crash loop the debounce exists to damp — and it is encrypted at rest like
# every other state file (it holds a hash and a timestamp, no user data).
_STATE_FILE = "admin_notify.json"
_NOTICE_MIN_GAP = 900.0        # 15 min: a crash loop pings ~4/hour, not 60


def _state_path():
    from pathlib import Path
    return Path(_STATE_FILE)


def load_state() -> Dict:
    """Last startup notice + the code fingerprint it was sent with."""
    path = _state_path()
    if not path.exists():
        return {}
    try:
        text, _was_enc = storage._read_protected(path)
        return json.loads(text) if text else {}
    except (SystemExit, ValueError, TypeError, OSError) as e:
        logger.warning("Could not read the admin-notify state: %s", type(e).__name__)
        return {}


def save_state(state: Dict) -> None:
    try:
        storage._write_protected(_state_path(), json.dumps(state))
    except OSError as e:
        logger.warning("Could not save the admin-notify state: %s", e)


def code_fingerprint(root=None) -> str:
    """Short hash of the app's own modules — "new code" without a version constant.

    Root-level *.py only: that is where the bot's modules live, and hashing them is
    a few milliseconds. Content, not mtime, so a touch is not a release.
    """
    from pathlib import Path
    base = Path(root) if root is not None else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(base.glob("*.py")):
        digest.update(path.name.encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:12]


def startup_note(pid: int, counts: Dict[str, int], url: Optional[str] = None) -> str:
    """The message for this start, or "" when it is a repeat within the gap.

    Updates the state either way, so a crash loop reports once and a code change
    is never announced twice. Counts only: nothing here identifies a user.
    """
    state = load_state()
    now = time.time()
    previous = state.get("code_fingerprint")
    current = code_fingerprint()
    last = float(state.get("last_notice_ts") or 0.0)

    state["code_fingerprint"] = current
    state["last_notice_ts"] = now
    state["restart_count"] = int(state.get("restart_count") or 0) + 1
    save_state(state)

    if last and now - last < _NOTICE_MIN_GAP:
        return ""                       # crash loop: one report, not sixty

    # Plain text on purpose: notify_admins sends without a parse mode, so markup
    # would reach the chat literally (measured: <b>…</b> shown as text).
    lines = [f"\U0001f504 Packey is up \u2014 PID {pid}"]
    if previous and previous != current:
        lines.append(f"\U0001f195 New code: {previous} \u2192 {current}")
    if url and state.get("mini_app_url") != url:
        lines.append(f"\U0001f310 Mini App URL: {url}")
    if url:
        state["mini_app_url"] = url
        save_state(state)
    lines.append(f"\U0001f4e6 {counts.get('packages', 0)} package(s) in "
                 f"{counts.get('chats', 0)} chat(s) \u00b7 "
                 f"{counts.get('pro', 0)} active subscriber(s)")
    return "\n".join(lines)


# ── operator alerts ─────────────────────────────────────────────────────────
# Recipients come from config.OWNER_CHAT_IDS and nothing else, so an alert can
# never reach a user. The text is passed through untouched: callers keep it free
# of tracking numbers and chat IDs (counts and reasons only) — this helper has no
# way to redact what it is handed, so it must never be handed an identifier.
_ALERT_MIN_GAP = 1800.0        # per key: a loop failing every cycle can't storm
_alert_sent_at: Dict[str, float] = {}


def reset_alert_throttle() -> None:
    """Clear the throttle windows. Tests call this; production does not."""
    _alert_sent_at.clear()


async def notify_admins(text: str, key: str = "general") -> bool:
    """Best-effort alert to the owner chat(s). Never raises.

    `key` names the throttle bucket — at most one alert per 30 minutes per key —
    so a poller that fails every minute sends one message, not sixty. Returns
    True when the message was handed to Telegram.
    """
    if not getattr(config, "ADMIN_ALERTS", True):
        return False
    targets = sorted(config.OWNER_CHAT_IDS)
    if not targets:
        return False
    now = time.monotonic()
    if now - _alert_sent_at.get(key, -_ALERT_MIN_GAP) < _ALERT_MIN_GAP:
        return False
    # Stamp BEFORE the send: a hanging network call must not re-alert next cycle.
    _alert_sent_at[key] = now

    live = bot
    if live is not None:
        for chat_id in targets:
            try:
                await asyncio.wait_for(live.send_message(chat_id, text), timeout=10)
            except Exception as e:
                logger.warning("Operator alert could not be delivered: %s", e)
        return True

    # The single-instance guard runs before main() creates the Bot, so there is
    # a token-only path. The token is never logged.
    token = getattr(config, "_bot_token", None)
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for chat_id in targets:
                await client.post(
                    "https://api.telegram.org/bot" + token + "/sendMessage",
                    data={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.warning("Operator alert could not be delivered: %s", e)
    return True
