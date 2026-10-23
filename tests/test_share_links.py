"""Shared tracking links: tokens, the public page, and the guards.

Structural where the point is behaviour (never distinguish a dead link, never
leak the number, always ephemeral) and byte-exact where the point is that two
answers are literally the SAME answer.
"""
import asyncio
import inspect
import re
import time
from html import escape

import pytest
from aiogram.enums import ChatMemberStatus, ChatType

import botstate
import config
import main
import miniapp
import share_links
import storage
import ui

# Distinctive on purpose: a leak must be caught by an exact-string assertion.
TID = "9400111206200074888350"
CHAT = 987654321
OTHER_USER = 4242


# ── fakes ──────────────────────────────────────────────────────────────────

class _Chat:
    def __init__(self, chat_id=CHAT, chat_type=ChatType.PRIVATE):
        self.id = chat_id
        self.type = chat_type


class _User:
    def __init__(self, uid):
        self.id = uid


class _Sent:
    def __init__(self, chat_id, message_id=42):
        # send_ephemeral reads sent.chat.id (aiogram's Message shape) and
        # send_ephemeral_chat reads sent.message_id.
        self.chat = _Chat(chat_id)
        self.chat_id = chat_id
        self.message_id = message_id


class _Msg:
    """The message a callback came from (a real Message in production, which is
    why send_ephemeral() works here — FakeMessage has no .answer())."""

    def __init__(self, chat, uid):
        self.chat = chat
        self.from_user = _User(uid)
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _Sent(self.chat.id)


class _Callback:
    def __init__(self, data, chat=None, uid=OTHER_USER):
        self.data = data
        self.message = _Msg(chat or _Chat(), uid)
        self.from_user = _User(uid)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class _Member:
    def __init__(self, status):
        self.status = status


class _Bot:
    def __init__(self, status=ChatMemberStatus.MEMBER):
        self.status = status
        self.sent = []
        self.deleted = []

    async def get_chat_member(self, chat_id, user_id):
        return _Member(self.status)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _Request:
    """Duck-typed aiohttp request: only what the share route and the
    no-store middleware actually touch."""

    def __init__(self, token, path=None):
        self.match_info = {"token": token}
        self.path = path if path is not None else f"/share/{token}"
        self.headers = {}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """The token store and the session store are module state, and the live
    bot's data files must never see a test."""
    saved = {cid: dict(pkgs) for cid, pkgs in storage.track_sessions.items()}
    storage.track_sessions.clear()
    share_links._tokens.clear()
    monkeypatch.setattr(config, "EPHEMERAL_TTL", 0)      # no delete tasks by default
    # The handler reads main's re-exported binding, not config's, so both are
    # pinned: a test must never depend on the operator's real tunnel URL.
    monkeypatch.setattr(config, "MINI_APP_URL", "https://app.example")
    monkeypatch.setattr(main, "MINI_APP_URL", "https://app.example")
    yield
    storage.track_sessions.clear()
    storage.track_sessions.update(saved)
    share_links._tokens.clear()


def _package(tracking_id=TID, **extra):
    info = {"name": "Blue Headphones", "status": "In Transit", "carrier": "usps",
            "carrier_name": "USPS", "carrier_url": "https://tools.usps.com",
            "detail": "Departed the hub.", "muted": False,
            "last_updated": "2026-08-01T10:00:00+00:00",
            "last_event_at": "2026-08-01T10:00:00+00:00"}
    info.update(extra)
    storage.track_sessions.setdefault(CHAT, {})[tracking_id] = info
    return info


def _expire(token):
    """Age a token out without waiting 30 days."""
    share_links._tokens[token]["expires_at"] = time.time() - 1
    return token


# ── the token store ───────────────────────────────────────────────────────

def test_mint_returns_a_128_bit_token():
    """128 bits, not 256: the token has to ride in callback_data, and Telegram
    rejects any button over 64 bytes outright."""
    token = share_links.mint(CHAT, TID)
    assert re.fullmatch(r"[0-9a-f]{32}", token), token
    assert share_links.lookup(token)["tracking_id"] == TID
    assert share_links.lookup(token)["chat_id"] == CHAT


def test_the_revoke_button_data_fits_telegrams_limit():
    """The live bug, pinned: "share_revoke:" + a 256-bit token was 77 bytes, so
    Telegram refused the entire message with BUTTON_DATA_INVALID — the link got
    minted, but the reply carrying it never sent and Share looked like a no-op.
    Mint → the callback the keyboard would carry → the cap and the slack."""
    token = share_links.mint(CHAT, TID)
    data = f"share_revoke:{token}"
    assert len(data.encode()) <= 64, f"{len(data.encode())} bytes: {data}"
    assert len("share_revoke:") + 2 * share_links.TOKEN_BYTES == len(data)


def test_the_share_button_data_fits_telegrams_limit():
    """The same cap on the way in, where the tracking id is the variable part.
    Nothing caps its length (validation only enforces a minimum), so the real
    headroom is pinned explicitly rather than assumed."""
    long_id = "9" * 40                     # longer than any live format
    data = f"share:{long_id}"
    assert len(data.encode()) <= 64, f"{len(data.encode())} bytes: {data}"
    assert len(data.encode()) == len("share:") + len(long_id)


def test_minting_again_revokes_the_previous_link():
    first = share_links.mint(CHAT, TID)
    second = share_links.mint(CHAT, TID)
    assert first != second
    assert share_links.lookup(first) is None, "the old link must die with the new one"
    assert share_links.lookup(second) is not None


def test_one_chats_link_is_not_anothers():
    """The same number tracked in two chats: each chat owns its own link."""
    mine = share_links.mint(CHAT, TID)
    theirs = share_links.mint(OTHER_USER, TID)
    assert share_links.lookup(mine) and share_links.lookup(theirs)


def test_unknown_token_is_not_a_record():
    assert share_links.lookup("f" * 64) is None
    assert share_links.lookup("") is None
    assert share_links.lookup(None) is None


def test_an_expired_token_is_dropped_on_read():
    token = _expire(share_links.mint(CHAT, TID))
    assert share_links.lookup(token) is None
    assert token not in share_links._tokens, "lazy prune must not keep it around"


def test_a_zero_ttl_means_links_never_expire(monkeypatch):
    monkeypatch.setattr(config, "SHARE_LINK_TTL_DAYS", 0)
    token = share_links.mint(CHAT, TID)
    assert share_links.lookup(token) is not None
    assert share_links._tokens[token]["expires_at"] == float("inf")


def test_expired_tokens_are_not_persisted():
    live = share_links.mint(CHAT, TID)
    dead = _expire(share_links.mint(OTHER_USER, TID))
    share_links.save()
    share_links._tokens.clear()
    share_links.load()
    assert share_links.lookup(live) is not None
    assert share_links.lookup(dead) is None


def test_the_sidecar_round_trips_through_the_encrypted_writer():
    """Same store contract as the other sidecars: _write_protected out,
    _read_protected in — so share_links.json is encrypted at rest too."""
    assert "_write_protected" in inspect.getsource(share_links.save)
    assert "_read_protected" in inspect.getsource(share_links.load)
    token = share_links.mint(CHAT, TID)
    assert config.SHARE_LINKS_FILE.exists(), "minting must persist the token"
    share_links._tokens.clear()
    share_links.load()
    assert share_links.lookup(token) is not None, "a restart must not kill links"


def test_revoke_is_final():
    token = share_links.mint(CHAT, TID)
    assert share_links.revoke(token) is True
    assert share_links.lookup(token) is None
    assert share_links.revoke(token) is False       # already gone


# ── the public page ───────────────────────────────────────────────────────

def _page(token):
    return asyncio.run(miniapp._webapp_share(_Request(token)))


def test_a_live_link_shows_the_package_without_the_number():
    _package()
    resp = _page(share_links.mint(CHAT, TID))
    assert resp.status == 200
    body = resp.text
    assert "Blue Headphones" in body
    assert "In Transit" in body
    assert "Departed the hub." in body
    assert TID not in body, "the tracking number must never reach a share page"
    assert str(CHAT) not in body, "the chat id must never reach a share page"


def test_the_page_carries_the_character_line():
    _package()
    body = _page(share_links.mint(CHAT, TID)).text
    assert escape(ui.character_line(TID, "Blue Headphones", "In Transit")) in body


def test_a_lost_package_shares_its_memorial_line():
    _package(lost=True, lost_at="2026-08-31T10:00:00+00:00")
    body = _page(share_links.mint(CHAT, TID)).text
    assert "lost since" in body
    assert escape(ui.character_line(TID, "Blue Headphones", "In Transit")) not in body


def test_the_page_escapes_what_it_renders():
    _package(name="<script>alert(1)</script>")
    body = _page(share_links.mint(CHAT, TID)).text
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_every_dead_link_answers_the_same_page():
    """Unknown, expired, revoked and package-deleted must be indistinguishable —
    otherwise the URL becomes an oracle for which packages exist."""
    _package()
    expired = _expire(share_links.mint(CHAT, TID))
    revoked = share_links.mint(CHAT, TID)
    share_links.revoke(revoked)
    live_but_deleted = share_links.mint(CHAT, TID)  # a live token...
    del storage.track_sessions[CHAT][TID]           # ...whose package is gone
    answers = [_page(token) for token in
               ("f" * 64, expired, revoked, live_but_deleted)]
    assert {r.status for r in answers} == {404}
    assert len({r.text for r in answers}) == 1, "the 404s differ — that is an oracle"


def test_the_share_page_is_never_cached():
    """_api_no_store wraps every route and filters nothing, so an iOS webview
    cannot keep a share page (or a dead-link answer) in its cache."""
    _package()
    token = share_links.mint(CHAT, TID)

    async def through_middleware():
        async def handler(request):
            return await miniapp._webapp_share(request)
        return await miniapp._api_no_store(_Request(token), handler)

    resp = asyncio.run(through_middleware())
    assert resp.headers["Cache-Control"] == "no-store, must-revalidate"
    assert "startswith" not in inspect.getsource(miniapp._api_no_store)
    assert miniapp._api_no_store in miniapp._build_miniapp_app().middlewares


def test_the_share_route_is_registered():
    paths = {r.resource.canonical for r in miniapp._build_miniapp_app().router.routes()}
    assert "/share/{token}" in paths


def test_the_rate_limit_gate_covers_share_paths():
    src = inspect.getsource(miniapp._api_rate_limit)
    assert '"/api/"' in src and '"/share/"' in src


# ── the Share button and its guards ───────────────────────────────────────

def test_the_card_offers_a_share_button():
    kb = main.build_package_keyboard(TID, {"muted": False})
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"share:{TID}" in callbacks


def test_every_package_button_stays_inside_telegrams_callback_data_cap():
    """The Share bug was one instance of a class: any button whose data passes
    64 bytes takes the whole message down with it, silently. Sweep both real
    keyboard builders with a deliberately long id and report the longest, so
    the tightest prefix is the one to watch — "confirm_remove:" (15 bytes)
    breaks at a 50-char id, "share:" (6) at a 59-char one, and "share_revoke:"
    can never break: 13 + a fixed 32-char token is 45 by construction."""
    long_id = "9" * 40                     # longer than any live format
    card = main.build_package_keyboard(long_id, {"muted": False}, True)
    confirm = ui.build_confirm_keyboard("remove", long_id)
    callbacks = [b.callback_data
                 for kb in (card, confirm)
                 for row in kb.inline_keyboard for b in row if b.callback_data]
    assert len(callbacks) >= 6, "both keyboards must actually build buttons"
    longest = max(callbacks, key=len)
    assert len(longest.encode()) <= 64, f"{len(longest.encode())} bytes: {longest}"
    assert longest == f"confirm_remove:{long_id}", "the tightest prefix moved"


def test_sharing_mints_a_link_and_sends_it_ephemerally(monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(botstate, "bot", bot)
    monkeypatch.setattr(config, "EPHEMERAL_TTL", 0.05)
    _package()
    callback = _Callback(f"share:{TID}")

    async def drive():
        await main.callback_share(callback)
        await asyncio.sleep(0.2)          # past the ttl → the delete has fired

    asyncio.run(drive())
    assert len(callback.message.answers) == 1
    text, kwargs = callback.message.answers[0]
    assert "https://app.example/share/" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data.startswith("share_revoke:")
    # The bug this test grew from: an over-long callback_data makes Telegram
    # reject the whole send with BUTTON_DATA_INVALID, which looks exactly like
    # Share doing nothing. Assert the button that really goes out, not a copy.
    assert len(button.callback_data.encode()) <= 64, button.callback_data
    assert bot.deleted == [(CHAT, 42)], "the bearer URL must not linger in chat"
    assert callback.answers[-1][0] == "Link created"


def test_the_share_message_goes_through_the_ephemeral_path():
    """Structural: send_ephemeral is the one place that schedules the delete."""
    assert "send_ephemeral" in inspect.getsource(main.callback_share)
    assert "_delete_later" in inspect.getsource(ui.send_ephemeral)


def test_a_non_admin_cannot_share_someone_elses_package(monkeypatch):
    monkeypatch.setattr(botstate, "bot", _Bot())        # a plain group member
    _package(added_by=CHAT)                             # someone else added it
    callback = _Callback(f"share:{TID}",
                         chat=_Chat(CHAT, ChatType.SUPERGROUP), uid=OTHER_USER)
    asyncio.run(main.callback_share(callback))
    assert callback.answers[-1][0] == "No permission"
    assert share_links._tokens == {}, "no token may be minted"
    assert callback.message.answers == []


def test_sharing_without_a_public_url_answers_gracefully(monkeypatch):
    monkeypatch.setattr(botstate, "bot", _Bot())
    # main.py re-exports the name, so the handler reads main's binding.
    monkeypatch.setattr(main, "MINI_APP_URL", None)
    _package()
    callback = _Callback(f"share:{TID}")
    asyncio.run(main.callback_share(callback))          # must not raise
    text, kwargs = callback.answers[-1]
    assert "available right now" in text.lower()
    assert kwargs.get("show_alert") is True
    assert share_links._tokens == {}
    assert callback.message.answers == []


def test_sharing_a_missing_package_is_refused(monkeypatch):
    monkeypatch.setattr(botstate, "bot", _Bot())
    callback = _Callback(f"share:{TID}")                # nothing in the store
    asyncio.run(main.callback_share(callback))
    assert callback.answers[-1][0] == "Package not found"
    assert share_links._tokens == {}


def test_revoke_kills_the_link_and_confirms(monkeypatch):
    monkeypatch.setattr(botstate, "bot", _Bot())
    _package()
    token = share_links.mint(CHAT, TID)
    callback = _Callback(f"share_revoke:{token}")
    asyncio.run(main.callback_share_revoke(callback))
    assert share_links.lookup(token) is None
    assert "revoked" in callback.message.answers[0][0].lower()
    assert callback.answers[-1][0] == "Link revoked"


def test_revoking_someone_elses_link_is_refused(monkeypatch):
    monkeypatch.setattr(botstate, "bot", _Bot())
    _package(added_by=CHAT)
    token = share_links.mint(CHAT, TID)
    callback = _Callback(f"share_revoke:{token}",
                         chat=_Chat(CHAT, ChatType.SUPERGROUP), uid=OTHER_USER)
    asyncio.run(main.callback_share_revoke(callback))
    assert callback.answers[-1][0] == "No permission"
    assert share_links.lookup(token) is not None, "the link must survive"


def test_revoking_an_already_dead_link_says_so(monkeypatch):
    monkeypatch.setattr(botstate, "bot", _Bot())
    callback = _Callback("share_revoke:" + "f" * 64)
    asyncio.run(main.callback_share_revoke(callback))
    assert "already gone" in callback.answers[-1][0]


def test_sharing_is_free_for_everyone():
    src = inspect.getsource(main.callback_share)
    assert "pro." not in src and "is_pro" not in src


def test_the_privacy_text_states_the_share_promise():
    text = ui.PRIVACY_TEXT
    assert "Share links" in text
    assert "never the tracking number" in text
    assert f"{config.SHARE_LINK_TTL_DAYS} days" in text, "the promise quotes the code"


def test_the_token_file_is_declared_for_backup():
    """A new state file must not be able to miss its backup (the script's own
    rule), and tests must not be able to write the live one."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert "SHARE_LINKS_FILE" in (root / "scripts" / "backup_state.sh").read_text()
    assert "SHARE_LINKS_FILE" in (root / "tests" / "test_backup.py").read_text()
    assert "SHARE_LINKS_FILE" in (root / "tests" / "conftest.py").read_text()
    assert config.SHARE_LINKS_FILE.name == "share_links.json"
