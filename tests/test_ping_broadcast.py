"""`/ping` — the operator broadcast, and the guards around it.

Built to the operator's spec (2026-09-26): the text goes out the moment it is sent,
with no confirmation step. These tests pin what that must not cost — users never
see the command, the caller is not pinged, one unreachable recipient cannot abort
the run, and a double-tap cannot send the same thing twice.
"""
import asyncio
from pathlib import Path

import pytest

import botstate
import main
import storage

ROOT = Path(__file__).resolve().parent.parent
ADMIN = 900
OTHER = 4242
PRO = 5150


class _User:
    def __init__(self, uid=ADMIN):
        self.id = uid
        self.first_name = "F"


class _Chat:
    def __init__(self, cid=ADMIN, ctype="private"):
        self.id = cid
        self.type = ctype


class _Sent:
    def __init__(self, text, markup=None, message_id=1, chat_id=ADMIN):
        self.chat = _Chat(chat_id)
        self.message_id = message_id
        self.text = text
        self.reply_markup = markup

    async def answer(self, text, **kw):
        return _Sent(text, kw.get("reply_markup"), self.message_id + 1, self.chat.id)

    async def edit_text(self, text, **kw):
        return self


class _Message:
    def __init__(self, text, chat_id=ADMIN):
        self.text = text
        self.chat = _Chat(chat_id)
        self.from_user = _User(chat_id)
        self.message_id = 5
        self.sent = []
        self.delete_calls = 0

    async def answer(self, text, **kw):
        sent = _Sent(text, kw.get("reply_markup"), len(self.sent) + 1, self.chat.id)
        self.sent.append(sent)
        return sent

    async def delete(self):
        self.delete_calls += 1


class _Bot:
    """Records broadcasts; ids in `blocks` answer 403 like a user who blocked us."""

    def __init__(self, blocks=()):
        self.sent = []
        self.blocks = set(blocks)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.blocks:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))

    async def delete_message(self, chat_id, message_id):
        return True


@pytest.fixture(autouse=True)
def _operator(monkeypatch):
    saved = dict(storage.track_sessions)
    storage.track_sessions.clear()
    storage.track_sessions[OTHER] = {"X": {"status": "In Transit"}}
    storage.track_sessions[PRO] = {"Y": {"status": "In Transit"}}
    monkeypatch.setattr(main, "OWNER_CHAT_IDS", {ADMIN})
    monkeypatch.setattr(main, "_ping_last_sent", None)
    monkeypatch.setattr(main, "_pro_users", {PRO: 0.0, ADMIN: 0.0})
    botstate.set_bot(_Bot())
    yield
    botstate.set_bot(None)
    storage.track_sessions.clear()
    storage.track_sessions.update(saved)


def _ping(text, chat_id=ADMIN):
    message = _Message(text, chat_id=chat_id)
    asyncio.run(main.ping_handler(message))
    return message


def test_a_paste_guard_refuses_a_digit_run(monkeypatch):
    """/ping delivers to EVERY chat: a chat ID or tracking number pasted into the
    text must not be broadcast to all of them. The input is kept (not deleted) so
    the operator can edit it."""
    botstate.set_bot(_Bot())
    msg = _Message("/ping call 1234567890 about your delivery")
    monkeypatch.setattr(main, "_ping_last_sent", None)
    asyncio.run(main.ping_handler(msg))
    assert botstate.bot.sent == [], "an identifier-shaped text must not broadcast"
    assert msg.delete_calls == 0, "the operator's text stays for editing"
    assert any("digit run" in s.text for s in msg.sent), msg.sent


def test_reworded_digits_pass_the_paste_guard(monkeypatch):
    """A comma or a word between the digits is ordinary prose, not an id."""
    botstate.set_bot(_Bot())
    monkeypatch.setattr(main, "_ping_last_sent", None)
    msg = _Message("/ping call 1,234,567 about your delivery")
    asyncio.run(main.ping_handler(msg))
    assert len(botstate.bot.sent) == 2, "reworded digits are ordinary text"


def test_a_non_operator_gets_nothing_at_all():
    """Not even an error: users must not learn the command exists."""
    message = _ping("/ping hello", chat_id=OTHER)
    assert message.sent == []
    assert botstate.bot.sent == []


def test_a_bare_ping_counts_the_reach_without_sending():
    message = _ping("/ping")
    assert botstate.bot.sent == [], "a bare /ping must never message anyone"
    assert "<b>2</b>" in message.sent[-1].text, message.sent[-1].text


def test_the_broadcast_reaches_every_known_chat_but_not_the_caller():
    message = _ping("/ping Maintenance at 03:00")
    assert sorted(chat for chat, _ in botstate.bot.sent) == [OTHER, PRO]
    assert all(text == "Maintenance at 03:00" for _, text in botstate.bot.sent)
    report = message.sent[-1].text
    assert "<b>2</b>" in report and "blocked <b>0</b>" in report, report


def test_one_unreachable_chat_is_counted_and_the_rest_still_go_out():
    botstate.set_bot(_Bot(blocks={OTHER}))
    message = _ping("/ping hello")
    assert [chat for chat, _ in botstate.bot.sent] == [PRO]
    assert "blocked <b>1</b>" in message.sent[-1].text, message.sent[-1].text


def test_a_second_ping_inside_the_window_is_refused():
    _ping("/ping first")
    sent_after_first = len(botstate.bot.sent)
    second = _ping("/ping second")
    assert len(botstate.bot.sent) == sent_after_first, "the cooldown must hold"
    assert "wait" in second.sent[-1].text, second.sent[-1].text


def test_an_overlong_message_is_refused_before_anything_is_sent():
    message = _ping("/ping " + "x" * 5000)
    assert botstate.bot.sent == []
    assert "4096" in message.sent[-1].text, message.sent[-1].text


def test_help_shows_the_command_to_the_operator_only():
    admin = _Message("/help")
    asyncio.run(main.help_handler(admin))
    assert "/ping" in admin.sent[-1].text

    user = _Message("/help", chat_id=OTHER)
    asyncio.run(main.help_handler(user))
    assert "/ping" not in user.sent[-1].text


def test_the_menu_registration_is_scoped_to_the_operator():
    """/ping must be operator-only AND layered on top of the full menu: a
    chat-scoped menu REPLACES the default one (Telegram never merges scopes), so
    an operator registration carrying only /ping would strip the other thirteen
    commands from the operator's own menu — the first version's bug."""
    source = (ROOT / "main.py").read_text()
    assert "BotCommandScopeChat" in source
    assert "ping" not in [name for name, _ in main.MENU_ITEMS], \
        "/ping must not be in the global menu"
    assert 'BotCommand(command="ping"' in source, "the operator scope adds it"
    assert "+ menu_commands" in source, \
        "the operator's menu must carry every command, not just /ping"
