"""A stale Confirm button must be a dead button.

The confirm callbacks used to proceed whenever get_confirmation(key) was None —
so /cancel left a working Confirm button behind (tap it and the action ran anyway),
and a bot restart (the confirmation store is in memory) armed every old button.
Fixed 2026-09-26: a missing confirmation refuses, for every destructive kind.
"""
import asyncio
import time

import pytest

import main
import storage

CHAT = 7
UID = 5
NUMBER = "9400111206200074888350"
OTHER = "1Z1E852G0321884183"


class _User:
    def __init__(self, uid=UID):
        self.id = uid
        self.first_name = "F"


class _Chat:
    def __init__(self, cid=CHAT):
        self.id = cid
        self.type = "private"


class _Prompt:
    """The message the buttons sit on."""

    def __init__(self):
        self.chat = _Chat()
        self.edited = None

    async def edit_text(self, text, **kw):
        self.edited = text
        return self


class _Callback:
    def __init__(self, data, uid=UID):
        self.data = data
        self.message = _Prompt()
        self.from_user = _User(uid=uid)
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


class _Cmd:
    """A /cancel command message."""

    def __init__(self):
        self.chat = _Chat()
        self.from_user = _User()
        self.text = "/cancel"
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)
        return self


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    saved = dict(storage.track_sessions)
    storage.track_sessions.clear()
    storage.track_sessions[CHAT] = {
        NUMBER: {"name": "One", "status": "Delivered"},
        OTHER: {"name": "Two", "status": "In Transit"},
    }
    monkeypatch.setattr(main.config, "EPHEMERAL_TTL", 0)
    async def no_stop(tids, chat_id):
        return None
    monkeypatch.setattr(main.carriers, "stop_17track_if_unshared", no_stop)
    yield
    storage.track_sessions.clear()
    storage.track_sessions.update(saved)


def _cancel():
    asyncio.run(main.cancel_handler(_Cmd()))


def _store(key):
    storage.store_confirmation(key, UID)


def test_cancel_then_tap_clear_changes_nothing():
    key = (CHAT, "clear", "")
    _store(key)
    _cancel()
    cb = _Callback("confirm_clear")
    asyncio.run(main.callback_confirm_clear(cb))
    assert "no longer active" in (cb.message.edited or ""), cb.message.edited
    assert len(storage.track_sessions[CHAT]) == 2, "clear must not have run"


def test_cancel_then_tap_purge_changes_nothing():
    key = (CHAT, "purge", "")
    _store(key)
    _cancel()
    cb = _Callback("confirm_purge")
    asyncio.run(main.callback_confirm_purge(cb))
    assert "no longer active" in (cb.message.edited or "")
    assert NUMBER in storage.track_sessions[CHAT], "delivered package must survive"


def test_cancel_then_tap_remove_changes_nothing():
    key = (CHAT, "remove", OTHER)
    _store(key)
    _cancel()
    cb = _Callback(f"confirm_remove:{OTHER}")
    asyncio.run(main.callback_confirm_remove(cb))
    assert "no longer active" in (cb.message.edited or "")
    assert OTHER in storage.track_sessions[CHAT]


def test_cancel_then_tap_add_adds_nothing(monkeypatch):
    key = (CHAT, "add", OTHER)
    _store(key)
    _cancel()
    added = []
    async def fake_add(*a, **k):
        added.append(a)
    monkeypatch.setattr(main, "_add_packages", fake_add)
    cb = _Callback(f"confirm_add:{OTHER}")
    asyncio.run(main.callback_confirm_add(cb))
    assert "no longer active" in (cb.message.edited or "")
    assert added == [], "a cancelled prompt must not register anything"


def test_an_expired_confirmation_is_a_dead_button_too():
    """TTL expiry and /cancel are the same refusal: the button outlives both."""
    key = (CHAT, "purge", "")
    storage.pending_confirmations[key] = (
        UID, time.monotonic() - 10_000)          # far past CONFIRM_TTL
    cb = _Callback("confirm_purge")
    asyncio.run(main.callback_confirm_purge(cb))
    assert "no longer active" in (cb.message.edited or "")
    assert NUMBER in storage.track_sessions[CHAT]


def test_a_live_confirmation_still_works():
    """The positive control: this must not turn every button dead."""
    key = (CHAT, "clear", "")
    _store(key)
    cb = _Callback("confirm_clear")
    asyncio.run(main.callback_confirm_clear(cb))
    assert storage.track_sessions.get(CHAT, {}) == {}, "clear ran on a live confirm"
    assert "no longer active" not in (cb.message.edited or "")
