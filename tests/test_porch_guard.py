"""Porch Guard: a loud per-package ping the moment a delivery lands.

The feature's promise is narrow and this file pins it: ONE ping on the transition,
ONE nudge if nobody taps ✅, then silence — and the loud ping takes the place of the
quiet aggregated count for that package (never both). It is armed by default for new
and legacy packages, it survives mute (a muted parcel is still on your porch), and
its copy never carries a tracking number.
"""
import asyncio
import hashlib
import hmac
import inspect
import json
import time
from urllib.parse import urlencode

import pytest
from aiogram.enums import ChatType

import botstate
import config
import main
import miniapp
import pro
import refresh
import storage
import ui

TID = "9400111206200074888350"
NAME = "Box"
CHAT = 987654321
CARRIER = {"code": "usps", "name": "USPS", "url": ""}


def _pkg(**extra):
    """A stored package; no porch_guard key unless the test says so (default on)."""
    info = {"name": NAME, "status": "In Transit", "carrier": "usps",
            "carrier_name": "USPS", "muted": False}
    info.update(extra)
    storage.track_sessions.setdefault(CHAT, {})[TID] = info
    return info


_DERIVED = object()


def _deliver(info, notify=_DERIVED):
    """The real transition path: _apply_lookup is where detection lives.

    notify defaults to the shape _check_one builds — (chat, id, the package's OWN
    name) — because a fixed name would hide a copy bug in the ping. Pass notify=None
    for the no-chat-to-tell case.
    """
    if notify is _DERIVED:
        notify = (CHAT, TID, info.get("name", "Package"))
    refresh._apply_lookup(info, "Delivered", CARRIER, "17TRACK", None,
                          changed_only=True, notify=notify)


class _Chat:
    def __init__(self, chat_id=CHAT, chat_type=ChatType.PRIVATE):
        self.id = chat_id
        self.type = chat_type


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    def __init__(self, chat, uid):
        self.chat = chat
        self.from_user = _User(uid)
        self.texts = []

    async def answer(self, text, **kwargs):
        self.texts.append(text)

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)


class _Callback:
    def __init__(self, data, chat=None, uid=CHAT):
        self.data = data
        self.message = _Msg(chat or _Chat(), uid)
        self.from_user = _User(uid)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class _Bot:
    async def delete_message(self, chat_id, message_id):
        pass


class _Req:
    def __init__(self, headers, body):
        self.headers = headers
        self._body = body

    async def json(self):
        return self._body


def _auth(monkeypatch, user_id):
    monkeypatch.setattr(config, "_bot_token", "123456:TESTTOKEN")
    params = {"user": json.dumps({"id": user_id}, separators=(",", ":")),
              "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest()
    return {"Authorization": "tma " + urlencode(
        {**params, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """One chat, one package, no queue left over from another test."""
    saved = {cid: dict(pkgs) for cid, pkgs in storage.track_sessions.items()}
    storage.track_sessions.clear()
    refresh._pending_porch_pings.clear()
    refresh._last_sweep_at = None
    monkeypatch.setattr(botstate, "bot", _Bot())
    monkeypatch.setattr(config, "EPHEMERAL_TTL", 0)
    monkeypatch.setattr(pro, "_pro_users", {})
    yield
    storage.track_sessions.clear()
    storage.track_sessions.update(saved)
    refresh._pending_porch_pings.clear()


def _capture_porch(monkeypatch):
    """Record what the flush actually sends (chat, text, keyboard)."""
    sent = []

    async def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs.get("reply_markup")))

    monkeypatch.setattr(refresh.ui, "send_ephemeral_chat", fake_send)
    return sent


# ── Detection: one ping per transition, armed by default ─────────────────
def test_a_delivery_queues_exactly_one_ping():
    info = _pkg()                      # no key: legacy/default packages are armed
    _deliver(info)
    assert refresh._pending_porch_pings == [(CHAT, TID, NAME)]

    _deliver(info)                     # still Delivered: not a transition
    assert len(refresh._pending_porch_pings) == 1, "a re-poll must not re-queue"


def test_an_explicitly_armed_package_queues_a_ping():
    info = _pkg(porch_guard=True)
    _deliver(info)
    assert refresh._pending_porch_pings == [(CHAT, TID, NAME)]


def test_a_guard_off_package_queues_nothing():
    info = _pkg(porch_guard=False)
    _deliver(info)
    assert refresh._pending_porch_pings == []


def test_a_lookup_with_no_chat_to_tell_queues_nothing():
    """notify is None on paths that own no chat — nothing to ping, no entry."""
    info = _pkg()
    _deliver(info, notify=None)
    assert refresh._pending_porch_pings == []


def test_a_muted_package_still_gets_its_porch_ping():
    """The mute gate sits in _check_one AFTER _apply_lookup, and Delivered is in
    ALERT_STATUSES: muting quiets the chat, not the doorbell."""
    info = _pkg(muted=True)
    _deliver(info)
    assert refresh._pending_porch_pings == [(CHAT, TID, NAME)]


# ── The ping itself ─────────────────────────────────────────────────────
def test_the_flush_sends_the_named_ping_and_stamps_the_window(monkeypatch):
    sent = _capture_porch(monkeypatch)
    info = _pkg()
    _deliver(info)

    asyncio.run(refresh._flush_porch_pings())

    assert len(sent) == 1
    chat_id, text, markup = sent[0]
    assert chat_id == CHAT
    assert NAME in text and "at your door" in text, "the ping names the parcel"
    assert TID not in text, "the copy must never carry the tracking number"
    assert markup.inline_keyboard[0][0].callback_data == f"pg_ack:{TID}"
    assert info["porch_acked"] is False and info["porch_repinged"] is False
    assert info["porch_reping_at"] == pytest.approx(
        time.time() + config.PORCH_REPING_SECONDS, abs=5)


def test_a_queued_package_removed_before_the_flush_is_skipped(monkeypatch):
    sent = _capture_porch(monkeypatch)
    _pkg()
    _deliver(storage.track_sessions[CHAT][TID])
    del storage.track_sessions[CHAT][TID]

    asyncio.run(refresh._flush_porch_pings())

    assert sent == [], "a package that is gone cannot be pinged"


def test_a_queued_package_switched_off_before_the_flush_is_skipped(monkeypatch):
    sent = _capture_porch(monkeypatch)
    info = _pkg()
    _deliver(info)
    info["porch_guard"] = False

    asyncio.run(refresh._flush_porch_pings())

    assert sent == [] and "porch_reping_at" not in info


def test_the_ping_escapes_the_name(monkeypatch):
    sent = _capture_porch(monkeypatch)
    info = _pkg(name="<script>&")
    _deliver(info)

    asyncio.run(refresh._flush_porch_pings())

    assert "&lt;script&gt;&amp;" in sent[0][1]


# ── One nudge, then silence ─────────────────────────────────────────────
def test_the_reping_fires_once_and_then_never_again(monkeypatch):
    sent = _capture_porch(monkeypatch)
    info = _pkg(status="Delivered", porch_reping_at=time.time() - 1,
                porch_acked=False, porch_repinged=False)

    asyncio.run(refresh._flush_porch_repings())

    assert len(sent) == 1 and "still sitting out there" in sent[0][1]
    assert info["porch_repinged"] is True

    asyncio.run(refresh._flush_porch_repings())        # the latch holds
    assert len(sent) == 1, "a nudge must never become a nag loop"


def test_a_ping_inside_its_window_is_not_nudged_yet(monkeypatch):
    sent = _capture_porch(monkeypatch)
    _pkg(status="Delivered", porch_reping_at=time.time() + 600, porch_acked=False)
    asyncio.run(refresh._flush_porch_repings())
    assert sent == []


def test_an_acked_package_is_never_nudged(monkeypatch):
    sent = _capture_porch(monkeypatch)
    _pkg(status="Delivered", porch_reping_at=time.time() - 1, porch_acked=True)
    asyncio.run(refresh._flush_porch_repings())
    assert sent == []


def test_a_stale_stamp_on_an_in_transit_package_is_ignored(monkeypatch):
    """A stamp should only exist after a delivery, but a stray one must not produce
    "still sitting out there" for a parcel that is still moving."""
    sent = _capture_porch(monkeypatch)
    _pkg(status="In Transit", porch_reping_at=time.time() - 1)
    asyncio.run(refresh._flush_porch_repings())
    assert sent == []


def test_guard_off_stops_the_nudge_too(monkeypatch):
    sent = _capture_porch(monkeypatch)
    _pkg(status="Delivered", porch_reping_at=time.time() - 1, porch_guard=False)
    asyncio.run(refresh._flush_porch_repings())
    assert sent == []


def test_zero_disables_the_nudge_entirely(monkeypatch):
    """Same convention as EPHEMERAL_TTL: 0 means "don't", not "straight away"."""
    sent = _capture_porch(monkeypatch)
    monkeypatch.setattr(config, "PORCH_REPING_SECONDS", 0)
    info = _pkg()
    _deliver(info)

    asyncio.run(refresh._flush_porch_pings())
    assert info["porch_reping_at"] is None

    asyncio.run(refresh._flush_porch_repings())
    assert len(sent) == 1, "the ping only, never the nudge"


# ── Loop wiring: the loud ping replaces the quiet count ─────────────────
class _EndTick(BaseException):
    """Ends one background_updater iteration without its except-block catching it."""


def _run_one_tick(monkeypatch, changed_count=0, sweep=None, porch=(),
                  porch_in_sweep=()):
    """One real loop iteration: no network, no browser, no real messages.

    porch is queued before the tick (the loop path); porch_in_sweep is queued by the
    fake sweep, because that is the only way to reach the sweep's substitution — the
    loop path flushes the queue first, exactly as documented.
    """
    pings, porch_sent = [], []
    seen = {"n": 0}

    async def fake_check_one(chat_id, tracking_id, info, is_pro_tier, semaphore):
        if seen["n"] < changed_count:
            seen["n"] += 1
            return chat_id
        return None

    async def fake_sweep(now_mono):
        refresh._pending_porch_pings.extend(porch_in_sweep)
        return dict(sweep or {})

    async def fake_ping(chat_id, count):
        pings.append((chat_id, count))

    async def fake_ephemeral(chat_id, text, **kwargs):
        porch_sent.append((chat_id, text))

    async def end_tick(seconds):
        raise _EndTick()

    monkeypatch.setattr(refresh, "_check_one", fake_check_one)
    monkeypatch.setattr(refresh, "_browser_only", lambda *a, **k: False)
    monkeypatch.setattr(refresh, "_sweep_browser", fake_sweep)
    monkeypatch.setattr(refresh, "_send_global_ping", fake_ping)
    monkeypatch.setattr(refresh.ui, "send_ephemeral_chat", fake_ephemeral)
    monkeypatch.setattr(refresh.asyncio, "sleep", end_tick)

    refresh._pending_porch_pings.extend(porch)
    refresh._last_sweep_at = None       # force the sweep into this same tick
    with pytest.raises(_EndTick):
        asyncio.run(refresh.background_updater())
    return pings, porch_sent


def test_a_delivered_package_replaces_the_aggregated_ping(monkeypatch):
    _pkg()
    pings, porch_sent = _run_one_tick(monkeypatch, changed_count=1,
                                      porch=[(CHAT, TID, NAME)])
    assert pings == [], "the porch ping takes the place of the count"
    assert len(porch_sent) == 1 and "at your door" in porch_sent[0][1]


def test_other_changed_packages_are_still_counted(monkeypatch):
    _pkg()
    storage.track_sessions[CHAT]["1Z1E852G0321884183"] = {
        "name": "Other", "status": "In Transit", "carrier": "ups"}
    pings, porch_sent = _run_one_tick(monkeypatch, changed_count=2,
                                      porch=[(CHAT, TID, NAME)])
    assert pings == [(CHAT, 1)], "the other change is still announced"
    assert len(porch_sent) == 1


def test_the_sweep_path_substitutes_the_ping_too(monkeypatch):
    _pkg()
    pings, porch_sent = _run_one_tick(monkeypatch, changed_count=0,
                                      sweep={CHAT: 1},
                                      porch_in_sweep=[(CHAT, TID, NAME)])
    assert pings == [], "a sweep-picked delivery is a porch ping, not a count"
    assert len(porch_sent) == 1 and "at your door" in porch_sent[0][1]


# ── The card toggle, the ack, and the app ───────────────────────────────
def test_the_card_toggle_flips_the_flag_both_ways():
    info = _pkg()                       # armed by default
    cb = _Callback(f"pg:{TID}")

    asyncio.run(main.callback_porch_guard(cb))
    assert info["porch_guard"] is False
    assert "Porch guard off" in cb.answers[-1][0]

    asyncio.run(main.callback_porch_guard(cb))
    assert info["porch_guard"] is True
    assert "Porch guard on" in cb.answers[-1][0]


def test_the_toggle_refuses_an_unknown_package():
    cb = _Callback(f"pg:{TID}")         # nothing stored for this chat
    asyncio.run(main.callback_porch_guard(cb))
    assert cb.answers[-1] == ("Package not found", {"show_alert": True})


def test_the_ack_marks_it_safe_and_edits_the_ping():
    info = _pkg(status="Delivered", porch_reping_at=time.time() - 1)
    cb = _Callback(f"pg_ack:{TID}")

    asyncio.run(main.callback_porch_ack(cb))

    assert info["porch_acked"] is True
    assert "is safe inside" in cb.message.texts[-1]
    assert cb.answers[-1][0] == "Nice — marked safe."


def test_the_ack_needs_no_permission_but_needs_the_package():
    """Decision 5: whoever taps ✅ is at the door, so no can_manage_package — but an
    unknown package must answer rather than raise."""
    assert "can_manage_package" not in inspect.getsource(main.callback_porch_ack)

    cb = _Callback(f"pg_ack:{TID}", uid=4242)
    asyncio.run(main.callback_porch_ack(cb))
    assert cb.answers[-1] == ("Package not found", {"show_alert": True})


def test_callback_data_stays_under_telegrams_ceiling():
    """64 bytes is a hard Telegram limit and both prefixes carry the id."""
    longest = "1Z" + "9" * 38           # 40 chars — longer than any real number
    for data in (f"pg:{longest}", f"pg_ack:{longest}"):
        assert len(data.encode()) <= 64, data

    datas = [b.callback_data
             for row in ui.build_package_keyboard(longest, {}).inline_keyboard
             for b in row]
    assert f"pg:{longest}" in datas
    ack = refresh._porch_ack_keyboard(longest)
    assert ack.inline_keyboard[0][0].callback_data == f"pg_ack:{longest}"


def test_the_porch_row_is_not_pro_gated():
    """Decision 2: free chats get the toggle too — the cadence difference is the
    upsell, not a locked switch."""
    free_row = [b.callback_data
                for row in ui.build_package_keyboard(TID, {}, False).inline_keyboard
                for b in row]
    assert f"pg:{TID}" in free_row, "a free chat must see the toggle"
    assert "pg:" in inspect.getsource(ui.build_package_keyboard)


def _porch(monkeypatch, body, user_id=CHAT):
    return asyncio.run(miniapp._api_porch(_Req(_auth(monkeypatch, user_id), body)))


def test_the_app_toggle_requires_initdata():
    resp = asyncio.run(miniapp._api_porch(
        _Req({}, {"tracking_id": TID, "enabled": False})))
    assert resp.status == 401


def test_the_app_toggle_sets_the_state_it_is_given(monkeypatch):
    _pkg()
    resp = _porch(monkeypatch, {"tracking_id": TID, "enabled": False})

    assert resp.status == 200
    assert json.loads(resp.body)["package"]["porch_guard"] is False
    assert storage.track_sessions[CHAT][TID]["porch_guard"] is False

    missing = _porch(monkeypatch, {"tracking_id": "1Z1111111111111111", "enabled": True})
    assert missing.status == 404


def test_the_app_payload_carries_the_flag():
    assert miniapp._package_payload(TID, {})["porch_guard"] is True, "default on"
    assert miniapp._package_payload(TID, {"porch_guard": False})["porch_guard"] is False
