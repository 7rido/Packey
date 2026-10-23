"""Outgoing direction: display-only metadata, Pro-gated to set.

Incoming is the default for everything, including every package that predates
the field. Only setting is gated — a downgraded member keeps reading whatever
they had.
"""
import asyncio
import hashlib
import hmac
import inspect
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from aiogram.enums import ChatMemberStatus, ChatType

import botstate
import carriers
import config
import main
import miniapp
import pro
import refresh
import storage
import ui

TID = "9400111206200074888350"
CHAT = 987654321
CARRIER = {"code": "usps", "name": "USPS", "url": ""}
HTML = Path(__file__).resolve().parent.parent / "webapp" / "index.html"


def _pkg(**extra):
    info = {"name": "Box", "status": "In Transit", "carrier": "usps",
            "carrier_name": "USPS", "muted": False}
    info.update(extra)
    storage.track_sessions.setdefault(CHAT, {})[TID] = info
    return info


def _pro(chat_id=CHAT, days=7):
    pro._pro_users[chat_id] = time.time() + days * 86400


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
    def __init__(self, status=ChatMemberStatus.MEMBER):
        self.status = status

    async def get_chat_member(self, chat_id, user_id):
        return type("M", (), {"status": self.status})()

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
    saved = {cid: dict(pkgs) for cid, pkgs in storage.track_sessions.items()}
    storage.track_sessions.clear()
    monkeypatch.setattr(botstate, "bot", _Bot())
    monkeypatch.setattr(config, "EPHEMERAL_TTL", 0)
    monkeypatch.setattr(pro, "_pro_users", {})
    yield
    storage.track_sessions.clear()
    storage.track_sessions.update(saved)


@pytest.fixture
def no_network(monkeypatch):
    async def _lookup(tracking_id, allow_browser=None, is_pro=None, **kwargs):
        return "In Transit", CARRIER, "17TRACK", "Departed the hub"
    monkeypatch.setattr(carriers, "fetch_track_status", _lookup)


def _add(monkeypatch, body, user_id=CHAT):
    return asyncio.run(miniapp._api_add(_Req(_auth(monkeypatch, user_id), body)))


def _direction(monkeypatch, body, user_id=CHAT):
    return asyncio.run(miniapp._api_direction(_Req(_auth(monkeypatch, user_id), body)))


def test_new_packages_default_to_incoming(no_network, monkeypatch):
    async def _go():
        return await refresh._register_and_fetch(TID, is_pro=False)
    info, _reg = asyncio.run(_go())
    assert info["direction"] == "incoming"


def test_a_record_without_the_field_reads_as_incoming():
    """Every package that predates the field renders as incoming."""
    info = _pkg()
    assert "direction" not in info
    assert main._package_payload(TID, info)["direction"] == "incoming"
    assert "Outgoing" not in ui.package_card(TID, info)


def test_pro_outgoing_survives_a_reload(no_network, monkeypatch):
    _pro()
    resp = _add(monkeypatch, {"tracking_id": TID, "name": "Box",
                              "direction": "outgoing"})
    assert resp.status == 200
    assert json.loads(resp.body)["package"]["direction"] == "outgoing"
    # ...and it is on disk: reload the store exactly the way a restart does.
    storage.track_sessions.clear()
    storage.load_data(main.DATA_FILE)
    assert storage.track_sessions[CHAT][TID]["direction"] == "outgoing"


def test_a_free_add_forces_incoming(no_network, monkeypatch):
    """Server-enforced, not just hidden in the UI."""
    resp = _add(monkeypatch, {"tracking_id": TID, "name": "Box",
                              "direction": "outgoing"})
    assert resp.status == 200
    assert json.loads(resp.body)["package"]["direction"] == "incoming"
    assert storage.track_sessions[CHAT][TID]["direction"] == "incoming"


def test_a_free_caller_cannot_set_direction(monkeypatch):
    info = _pkg()
    resp = _direction(monkeypatch, {"tracking_id": TID, "direction": "outgoing"})
    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "outgoing_pro_only"
    assert info.get("direction") is None, "the value must not change"


def test_direction_rejects_junk_and_missing_packages(monkeypatch):
    _pro()
    _pkg()
    bad = _direction(monkeypatch, {"tracking_id": TID, "direction": "sideways"})
    assert bad.status == 400
    missing = _direction(monkeypatch, {"tracking_id": "1Z999AA10123456784",
                                       "direction": "outgoing"})
    assert missing.status == 404


def test_the_api_flips_for_pro(monkeypatch):
    _pro()
    info = _pkg()
    resp = _direction(monkeypatch, {"tracking_id": TID, "direction": "outgoing"})
    assert resp.status == 200
    assert json.loads(resp.body)["package"]["direction"] == "outgoing"
    assert info["direction"] == "outgoing"
    back = _direction(monkeypatch, {"tracking_id": TID, "direction": "incoming"})
    assert json.loads(back.body)["package"]["direction"] == "incoming"
    assert info["direction"] == "incoming"


def test_the_bot_toggle_flips_for_pro(monkeypatch):
    _pro()
    info = _pkg()
    callback = _Callback(f"direction:{TID}")
    asyncio.run(main.callback_direction(callback))
    assert info["direction"] == "outgoing"
    assert callback.answers[-1][0].endswith("outgoing")
    asyncio.run(main.callback_direction(_Callback(f"direction:{TID}")))
    assert info["direction"] == "incoming"


def test_the_bot_toggle_refuses_non_pro(monkeypatch):
    info = _pkg()
    callback = _Callback(f"direction:{TID}")
    asyncio.run(main.callback_direction(callback))
    assert "Pro feature" in callback.answers[-1][0]
    assert info.get("direction") is None, "a free chat must not change anything"


def test_the_toggle_button_exists_only_for_pro():
    """A free chat never sees the button (and the handler refuses anyway)."""
    free = [b.callback_data for row in main.build_package_keyboard(TID, {}).inline_keyboard
            for b in row]
    assert f"direction:{TID}" not in free
    pro_kb = main.build_package_keyboard(TID, {"direction": "incoming"},
                                         is_pro_tier=True)
    buttons = {b.callback_data: b.text for row in pro_kb.inline_keyboard for b in row}
    assert buttons[f"direction:{TID}"].endswith("Mark outgoing")
    flippable = main.build_package_keyboard(TID, {"direction": "outgoing"},
                                            is_pro_tier=True)
    labels = [b.text for row in flippable.inline_keyboard for b in row]
    assert any(t.endswith("Mark incoming") for t in labels)


def test_a_downgraded_member_keeps_the_value_but_cannot_set_it(monkeypatch):
    """Grandfathered: reading keeps working, setting stays gated."""
    info = _pkg(direction="outgoing")
    assert pro.is_pro(CHAT) is False
    assert main._package_payload(TID, info)["direction"] == "outgoing"
    assert _direction(monkeypatch,
                      {"tracking_id": TID, "direction": "incoming"}).status == 403
    assert info["direction"] == "outgoing"
    # No toggle for them either — read-only, not flipped by accident.
    callbacks = [b.callback_data for row in main.build_package_keyboard(
        TID, info).inline_keyboard for b in row]
    assert f"direction:{TID}" not in callbacks


def test_an_outgoing_delivery_reads_differently():
    info = _pkg(status="Delivered", direction="outgoing")
    assert "arrived at its destination" in ui.package_card(TID, info)
    incoming = _pkg(status="Delivered", direction="incoming")
    assert "arrived at its destination" not in ui.package_card(TID, incoming)


def test_outgoing_is_display_only():
    """Tracking, allowance, purge, lost-detection and cadence never read it."""
    for fn in (refresh._apply_lookup, refresh._auto_purge_delivered,
               pro.check_intl_limit, pro.count_international,
               refresh._flag_lost_packages, refresh.cadence_seconds):
        assert "direction" not in inspect.getsource(fn), fn.__name__


def test_the_app_gates_outgoing_behind_pro():
    html = HTML.read_text()
    assert 'id="dirRow" hidden' in html, "the control starts hidden"
    assert 'data-f="outgoing"' in html and 'id="dirChip"' in html
    assert "dirChip.hidden = !tier.pro" in html, "hidden for free chats"
    assert "dirRow.hidden = !tier.pro" in html
    assert "direction: selectedDirection()" in html, "the add posts the choice"
    assert "setDirection('incoming')" in html, "every add starts as incoming"
    assert "badge-dir" in html, "the card marks an outgoing package"
    assert "arrived at its destination" in html
    assert "api('/api/direction'" in html, "the card can flip it too"


def test_the_direction_route_is_registered():
    paths = {r.resource.canonical for r in miniapp._build_miniapp_app().router.routes()}
    assert "/api/direction" in paths
