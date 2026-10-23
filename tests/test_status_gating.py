"""`/status` shows each user their own slice; global telemetry is the operator's.

A private-chat user could read totals across every chat, the carrier mix of the
whole user base, the 17TRACK quota and the Ship24 paid spend. None of it identifies
anyone, but it is the operator's information, not theirs — and it describes the bot
as a business, which is not what a user asking "is it working?" needs.
"""
import asyncio

import pytest

import main
import storage

CHAT = 7
OWNER = 900


class _User:
    id = 5
    first_name = "F"


class _Chat:
    def __init__(self, cid=CHAT):
        self.id = cid
        self.type = "private"


class _Msg:
    def __init__(self, text, chat_id=CHAT):
        self.text = text
        self.chat = _Chat(chat_id)
        self.from_user = _User()
        self.message_id = 42
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return self


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(main.config, "EPHEMERAL_TTL", 0)
    monkeypatch.setattr(main, "OWNER_CHAT_IDS", {OWNER})
    saved = dict(storage.track_sessions)
    storage.track_sessions.clear()
    storage.track_sessions[CHAT] = {
        "9400111206200074888350": {"name": "q", "status": "Pending"}}
    storage.track_sessions[2] = {
        "1Z1E852G0310312712": {"name": "r", "status": "In Transit"}}
    yield
    storage.track_sessions.clear()
    storage.track_sessions.update(saved)


def test_a_user_sees_their_own_slice_and_no_global_numbers():
    msg = _Msg("/status")
    asyncio.run(main.status_handler(msg))
    text = msg.answers[-1][0]
    assert "Operational" in text and "1 package(s) tracked for you" in text, text
    for marker in ("Packages tracked:", "17TRACK quota", "Ship24 today:",
                   "Carriers:", "Lookups (ok / fail)", "across 2 chats"):
        assert marker not in text, f"a user must not see: {marker}"


def test_the_owner_still_gets_the_full_report(monkeypatch):
    import time
    import carriers
    monkeypatch.setattr(carriers, "_quota_state",
                        {"used": 11, "total": 200, "at": time.time()})
    msg = _Msg("/status", chat_id=OWNER)
    asyncio.run(main.status_handler(msg))
    text = msg.answers[-1][0]
    for marker in ("Packages tracked:", "17TRACK quota: 11/200", "Ship24 today:",
                   "Package mix:"):
        assert marker in text, f"the operator's report lost: {marker}"


def test_a_user_with_no_packages_gets_the_health_line_too():
    """'Is it working?' must have an answer even with an empty store."""
    msg = _Msg("/status", chat_id=4242)
    asyncio.run(main.status_handler(msg))
    assert "Operational" in msg.answers[-1][0] and "0 package(s)" in msg.answers[-1][0]