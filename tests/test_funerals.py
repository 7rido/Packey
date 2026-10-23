"""Funerals for lost packages: detection, the memorial line, and the notices.

Structural on purpose — the copy is meant to be edited freely, so these tests
pin behaviour (who is declared lost, what the card says, that each notice is
sent once and deleted) rather than sentences.
"""
import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import pytest
from aiogram.enums import ParseMode

import botstate
import config
import main
import refresh
import storage
import ui

ROOT = Path(__file__).resolve().parent.parent
TID = "9400111206200074888350"
CARRIER = {"code": "usps", "name": "USPS", "url": "https://tools.usps.com"}


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class _Sent:
    def __init__(self, chat_id, message_id):
        self.chat_id = chat_id
        self.message_id = message_id


class _Bot:
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return _Sent(chat_id, 99)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """The session store and the notice queue are module state — and the live
    bot must never see a test's packages."""
    saved = {cid: dict(pkgs) for cid, pkgs in storage.track_sessions.items()}
    storage.track_sessions.clear()
    refresh._pending_lost_notices.clear()
    # Pin the threshold: an operator's .env must not change what these assert.
    monkeypatch.setattr(config, "LOST_AFTER_DAYS", 30)
    yield
    storage.track_sessions.clear()
    storage.track_sessions.update(saved)
    refresh._pending_lost_notices.clear()


def _bot(monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(botstate, "bot", bot)
    return bot


def _info(days_silent=31, status="In Transit", **extra):
    info = {"name": "Blue Headphones", "status": status,
            "carrier": "usps", "carrier_name": "USPS",
            # Stored details are normalized by the lookup path: a fixture that
            # skipped that would look like a change on every poll.
            "detail": storage._trim_sentence("Departed the hub"),
            "last_event_at": _ago(days_silent),
            "last_updated": _ago(days_silent),
            "last_checked": _ago(1),
            "muted": False}
    info.update(extra)
    return info


def _store(chat_id, info, tracking_id=TID):
    storage.track_sessions[chat_id] = {tracking_id: info}
    return info


# ── last_event_at: what counts as a carrier event ──────────────────────────

def test_a_new_detail_is_an_event_even_when_the_status_holds():
    """last_updated only moves on status transitions, so it cannot measure
    silence: an In Transit package can move for weeks without changing status."""
    ten_days = _ago(10)
    info = {"status": "In Transit", "detail": "Departed the hub",
            "last_updated": ten_days}
    refresh._apply_lookup(info, "In Transit", CARRIER, "17TRACK",
                          "Arrived at the hub", changed_only=True)
    assert info["status"] == "In Transit"
    assert info["last_updated"] == ten_days, "last_updated is not the silence clock"
    assert (refresh._parse_iso_epoch(info["last_event_at"])
            > refresh._parse_iso_epoch(ten_days))


def test_an_unchanged_lookup_is_not_an_event():
    """Records store the normalized detail, so an identical poll must compare
    equal — a trimmed/sentence-cased rewrite is not a carrier event."""
    stored = storage._trim_sentence("Departed the hub")
    info = {"status": "In Transit", "detail": stored, "last_updated": _ago(10)}
    refresh._apply_lookup(info, "In Transit", CARRIER, "17TRACK",
                          "Departed the hub", changed_only=True)
    assert "last_event_at" not in info


def test_a_failed_lookup_is_not_an_event():
    """Unavailable means we could not read the carrier — a package must not
    look alive because our own lookups keep failing."""
    info = {"status": "In Transit", "detail": "Departed the hub",
            "last_updated": _ago(40)}
    refresh._apply_lookup(info, "Unavailable", CARRIER, "None", "Lookup failed")
    assert info["status"] == "In Transit", "a transient failure clobbered the status"
    assert "last_event_at" not in info


# ── detection ─────────────────────────────────────────────────────────────

def test_thirty_one_days_of_silence_is_lost():
    info = _store(7, _info(31))
    flagged = asyncio.run(refresh._flag_lost_packages())
    assert info["lost"] is True
    assert refresh._parse_iso_epoch(info["lost_at"]) is not None
    assert flagged == {7: [(TID, "Blue Headphones")]}


def test_twenty_nine_days_is_not_lost():
    info = _store(7, _info(29))
    assert asyncio.run(refresh._flag_lost_packages()) == {}
    assert info.get("lost") is not True


def test_legacy_records_fall_back_to_last_updated():
    """Records written before last_event_at existed have no better stamp."""
    info = _store(7, _info(31))
    info.pop("last_event_at")
    assert asyncio.run(refresh._flag_lost_packages()) == {7: [(TID, "Blue Headphones")]}


def test_an_unreadable_stamp_is_never_guessed_at():
    """Same rule as retention: a made-up date must not funeralize a package."""
    info = _store(7, _info(31))
    info["last_event_at"] = "not-a-timestamp"
    info["last_updated"] = ""
    assert asyncio.run(refresh._flag_lost_packages()) == {}
    assert info.get("lost") is not True


@pytest.mark.parametrize("status,flag", [
    ("Delivered", None),                           # terminal: it arrived
    ("Available for Pickup", None),                # waiting on the human
    ("In Transit", {"register_blocked": True}),    # pre-first-scan labels sit quiet
    ("In Transit", {"needs_pro": True}),           # frozen by tier, not lost
    ("In Transit", {"last_lookup_failed": True}),  # our lookup failing != lost
])
def test_the_skip_set_is_never_funeralized(status, flag):
    info = _store(7, _info(31, status=status, **(flag or {})))
    assert asyncio.run(refresh._flag_lost_packages()) == {}
    assert info.get("lost") is not True


def test_lost_after_days_zero_disables_the_lifecycle(monkeypatch):
    monkeypatch.setattr(config, "LOST_AFTER_DAYS", 0)
    info = _store(7, _info(400))
    assert asyncio.run(refresh._flag_lost_packages()) == {}
    assert info.get("lost") is not True


# ── the notices ───────────────────────────────────────────────────────────

def test_the_funeral_names_the_package_but_never_the_number(monkeypatch):
    """Naming the package is the joke; the number still stays out of chat."""
    bot = _bot(monkeypatch)
    _store(7, _info(31))

    async def drive():
        await refresh._send_funeral_notices(await refresh._flag_lost_packages())

    asyncio.run(drive())
    assert len(bot.sent) == 1
    chat_id, text, kwargs = bot.sent[0]
    assert chat_id == 7
    assert "Blue Headphones" in text
    assert TID not in text
    assert kwargs.get("parse_mode") == ParseMode.HTML


def test_the_funeral_is_sent_once_and_marked(monkeypatch):
    bot = _bot(monkeypatch)
    info = _store(7, _info(31))

    async def drive():
        await refresh._send_funeral_notices(await refresh._flag_lost_packages())
        # The next hourly pass: already declared, so there is nothing to send.
        second = await refresh._flag_lost_packages()
        await refresh._send_funeral_notices(second)
        return second

    assert asyncio.run(drive()) == {}
    assert info["funeral_shown"] is True
    assert len(bot.sent) == 1, "the funeral went out more than once"


def test_while_lost_the_card_carries_the_memorial_line():
    """The memorial REPLACES the status voice: a lost package must not keep
    talking as if it were still moving."""
    info = _store(7, _info(31))
    asyncio.run(refresh._flag_lost_packages())
    memorial = ui.character_line(TID, "Blue Headphones", "In Transit",
                                 lost=True, lost_since=info["last_event_at"])
    status_voice = ui.character_line(TID, "Blue Headphones", "In Transit")
    card = ui.package_card(TID, info)
    assert escape(memorial) in card
    assert escape(status_voice) not in card
    assert "In Transit" in card, "the facts above the line are untouched"
    payload = main._package_payload(TID, info)
    assert payload["character_line"] == memorial


def test_the_memorial_survives_a_missing_date():
    """Never blank, and never a bare '-': an unreadable stamp is admitted."""
    line = ui.character_line(TID, "Box", "In Transit", lost=True, lost_since=None)
    assert "lost since" in line and "an unknown date" in line


# ── resurrection ──────────────────────────────────────────────────────────

def test_a_real_event_resurrects_a_lost_package(monkeypatch):
    bot = _bot(monkeypatch)
    info = _store(7, _info(31))
    info["lost"] = True
    info["lost_at"] = _ago(1)
    info["funeral_shown"] = True
    refresh._apply_lookup(info, "Out for Delivery", CARRIER, "17TRACK",
                          "On the truck", notify=(7, TID, "Blue Headphones"))
    assert info["lost"] is False
    assert "lost_at" not in info
    assert info["funeral_shown"] is False, "the next death gets its own funeral"
    assert len(refresh._pending_lost_notices) == 1
    asyncio.run(refresh._flush_lost_notices())
    assert len(bot.sent) == 1 and "Blue Headphones" in bot.sent[0][1]
    assert TID not in bot.sent[0][1]
    assert refresh._pending_lost_notices == [], "the queue must drain"


def test_unavailable_noise_never_resurrects(monkeypatch):
    """Our lookup failing says nothing about the package."""
    bot = _bot(monkeypatch)
    info = _store(7, _info(31))
    info["lost"] = True
    refresh._apply_lookup(info, "Unavailable", CARRIER, "None", "Lookup failed",
                          notify=(7, TID, "Blue Headphones"))
    assert info["lost"] is True
    assert refresh._pending_lost_notices == []
    asyncio.run(refresh._flush_lost_notices())
    assert bot.sent == []


def test_the_delivered_downgrade_guard_still_wins():
    """A lagging source must not un-deliver a package — and a delivered package
    is never funeralized in the first place. The claimant here is the official
    USPS tier; 17TRACK (a weaker witness) echoing an older status is exactly
    the lagging echo the authority-ranked guard exists to ignore."""
    info = {"status": "Delivered", "delivered_by": "USPS API",
            "detail": "Delivered, front door",
            "last_event_at": _ago(60), "last_updated": _ago(60)}
    refresh._apply_lookup(info, "In Transit", CARRIER, "17TRACK", "Departed hub")
    assert info["status"] == "Delivered"
    assert refresh._lost_eligible(info) is False


# ── the ephemeral pin ─────────────────────────────────────────────────────

def test_the_notice_is_really_deleted(monkeypatch):
    """Behavioural proof: the message is removed, so a funeral cannot become
    permanent chat history."""
    bot = _bot(monkeypatch)
    monkeypatch.setattr(config, "EPHEMERAL_TTL", 0.05)

    async def drive():
        await refresh._send_lost_notice(7, TID, "Blue Headphones", "funeral")
        await asyncio.sleep(0.2)          # past the ttl → the delete has fired

    asyncio.run(drive())
    assert bot.deleted == [(7, 99)]


def test_the_notice_goes_through_the_ephemeral_helper():
    """Structural pin: the loop holds no Message, so the funeral must use the
    chat-id ephemeral path (bot.send_message + ui._delete_later)."""
    assert "send_ephemeral_chat" in inspect.getsource(refresh._send_lost_notice)
    helper = inspect.getsource(ui.send_ephemeral_chat)
    assert "botstate.bot.send_message" in helper
    assert "_delete_later" in helper


def test_no_new_permanent_data_bearing_loop_message():
    """The funeral names a package, so it must be the only loop notice that
    does — and it must be ephemeral. Every other direct send stays data-free."""
    for text, _kb in (refresh.build_global_ping(2), refresh.build_retention_notice(2)):
        assert "Blue Headphones" not in text
        for variants in ui._LOST_NOTICES.values():
            for variant in variants:
                assert variant.format(name="Blue Headphones") not in text
    data_free = {"_send_retention_notices", "_send_global_ping"}
    for name, fn in vars(refresh).items():
        if not inspect.isfunction(fn) or fn.__module__ != "refresh":
            continue
        if "botstate.bot.send_message" in inspect.getsource(fn):
            assert name in data_free, (
                f"refresh.{name} sends a message directly — a loop message that "
                "carries package data must go through ui.send_ephemeral_chat")


def test_the_readme_admits_the_funeral_exception():
    """The README's "no package data" claim stays true for the ping — and stays
    honest about the one exception that names a package."""
    bullet = (ROOT / "README.md").read_text().split(
        "Notifications carry no package data", 1)[1][:900]
    assert "funeral" in bullet and "ephemeral" in bullet.lower()


def test_the_threshold_is_documented_and_re_exported():
    assert main.LOST_AFTER_DAYS == config.LOST_AFTER_DAYS
    assert "LOST_AFTER_DAYS" in config.__all__
    assert "LOST_AFTER_DAYS=" in (ROOT / ".env.example").read_text()
