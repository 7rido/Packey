"""The "dashboard moved" announcement is rate-limited per chat.

The tunnel churn of 2026-09-26 delivered it three times in twelve minutes
(18:01, 18:10, 18:13) — once a day per chat keeps the self-heal without the
spam. The announce state is redirected to tmp so the live mini_app_url.json is
never written by a test.
"""
import asyncio
import time

import pytest

import miniapp
import storage

URL = "https://x.test"


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    saved = dict(storage.track_sessions)
    storage.track_sessions.clear()
    storage.track_sessions[1] = {"X": {"status": "In Transit"}}
    storage.track_sessions[2] = {"Y": {"status": "In Transit"}}
    monkeypatch.setattr(miniapp, "_announced_at", {})
    monkeypatch.setattr(miniapp, "_miniapp_url_state_path",
                        lambda: tmp_path / "mini_app_url.json")
    yield
    storage.track_sessions.clear()
    storage.track_sessions.update(saved)


def test_announce_reaches_every_chat():
    bot = _Bot()
    sent = asyncio.run(miniapp.announce_miniapp_url(bot, URL))
    assert sent == 2 and len(bot.sent) == 2


def test_a_second_announce_within_24h_is_skipped():
    bot = _Bot()
    asyncio.run(miniapp.announce_miniapp_url(bot, URL))
    again = asyncio.run(miniapp.announce_miniapp_url(bot, URL))
    assert again == 0, "the 24h rate limit must hold"
    assert len(bot.sent) == 2


def test_a_chat_announced_long_ago_gets_the_next_one(monkeypatch):
    bot = _Bot()
    asyncio.run(miniapp.announce_miniapp_url(bot, URL))
    monkeypatch.setattr(miniapp, "_announced_at",
                        {1: time.time() - 25 * 3600, 2: time.time() - 25 * 3600})
    sent = asyncio.run(miniapp.announce_miniapp_url(bot, URL))
    assert sent == 2, "outside the window, the announce fires again"