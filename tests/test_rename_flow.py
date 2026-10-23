"""The Rename button: one typed answer, not a template to copy and edit.

Reported 2026-09-26: tapping ✏️ Rename replied with
"✏️ Send the new name: /name <tid> New Name Here" — a block to copy, edit and send.
It read as if the rename had happened, while tapping that block only COPIES it; a
user who sent it verbatim named their package the literal words "New Name Here", and
one who just tapped it saw nothing change at all.

What these tests pin down:
  * the prompt asks for a name and says WHICH package (by name, never by number —
    this prompt stays in history, and numbers are kept out of it),
  * the next ordinary message is applied to that package and the hand-off is spent,
  * a later message cannot rename with a leftover target,
  * another user in the group cannot spend someone else's pending rename,
  * an expired prompt is ignored and dropped,
  * an unrelated command does not consume the pending rename, and
  * the /name command path is untouched.
"""
import asyncio
import time

import pytest

import main
import storage

CHAT = 4242
USER = 777
TID = "RR123456789CN"


class _Chat:
    def __init__(self, chat_id=CHAT, ctype="private"):
        self.id = chat_id
        self.type = ctype


class _User:
    def __init__(self, uid=USER):
        self.id = uid


class _Sent:
    def __init__(self, chat, message_id=1):
        self.chat = chat
        self.message_id = message_id


class _Message:
    def __init__(self, text, chat_id=CHAT, uid=USER, ctype="private"):
        self.text = text
        self.chat = _Chat(chat_id, ctype)
        self.from_user = _User(uid)
        self.message_id = 5
        self.sent = []          # (text, kwargs) for everything this fake answered

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return _Sent(self.chat)


class _Callback:
    """A tap on the Rename button of a card."""

    def __init__(self, data, chat_id=CHAT, uid=USER, ctype="private"):
        self.data = data
        self.from_user = _User(uid)
        self.message = _Message("", chat_id, uid, ctype)
        self.alerts = []

    async def answer(self, text="", **kwargs):
        self.alerts.append(text)


@pytest.fixture(autouse=True)
def clean_state():
    saved = dict(storage.track_sessions)
    storage.track_sessions.clear()
    main._pending_renames.clear()
    yield
    storage.track_sessions.clear()
    storage.track_sessions.update(saved)
    main._pending_renames.clear()


def _package(name="Box from Mum"):
    storage.track_sessions[CHAT] = {TID: {"name": name, "status": "In Transit"}}
    return TID


def _tap(**kw):
    callback = _Callback(f"rename:{TID}", **kw)
    asyncio.run(main.callback_rename(callback))
    return callback


def test_the_prompt_asks_for_a_name_and_names_the_package():
    _package()
    callback = _tap()
    prompt, kwargs = callback.message.sent[-1]
    assert "Reply with the new name" in prompt
    assert "Box from Mum" in prompt, "it must say which package is being renamed"
    assert TID not in prompt, (
        "no tracking number: this prompt stays in history, and the bot keeps numbers "
        "out of it (cards are ephemeral, /list omits them)")
    assert "New Name Here" not in prompt, "the copy-me placeholder is gone"
    assert kwargs["reply_markup"].force_reply is True, (
        "ForceReply is the point: the client opens a reply box, so there is nothing "
        "to copy, edit and paste")


def test_the_next_message_becomes_the_name_and_spends_the_handoff():
    _package()
    _tap()
    message = _Message("Mum's birthday box")
    asyncio.run(main.unknown_handler(message))
    assert storage.track_sessions[CHAT][TID]["name"] == "Mum's birthday box"
    assert any("Renamed to" in text for text, _kw in message.sent)
    assert CHAT not in main._pending_renames, "one message, one attempt"

    second = _Message("and another thing")
    asyncio.run(main.unknown_handler(second))
    assert storage.track_sessions[CHAT][TID]["name"] == "Mum's birthday box", (
        "a leftover target must not rename with the message after the answer")
    assert any("don't understand" in text for text, _kw in second.sent)


def test_another_user_cannot_spend_the_pending_rename():
    _package()
    _tap()
    intruder = _Message("hijacked", uid=USER + 1)
    asyncio.run(main.unknown_handler(intruder))
    assert storage.track_sessions[CHAT][TID]["name"] == "Box from Mum"
    assert CHAT in main._pending_renames, (
        "not theirs to spend — the tapper may still answer")
    assert any("don't understand" in text for text, _kw in intruder.sent)


def test_an_expired_prompt_is_ignored_and_dropped():
    _package()
    _tap()
    tracking_id, requester, _deadline = main._pending_renames[CHAT]
    main._pending_renames[CHAT] = (tracking_id, requester, time.monotonic() - 1)
    message = _Message("far too late")
    asyncio.run(main.unknown_handler(message))
    assert storage.track_sessions[CHAT][TID]["name"] == "Box from Mum"
    assert CHAT not in main._pending_renames, "the stale entry does not linger"


def test_a_command_does_not_consume_the_pending_rename():
    _package()
    _tap()
    asyncio.run(main.unknown_handler(_Message("/list")))
    assert CHAT in main._pending_renames, (
        "an unrelated command must not eat the answer the prompt waits for")


def test_a_refused_tap_records_nothing():
    _package()
    storage.track_sessions[CHAT][TID]["added_by"] = 999      # someone else's package
    callback = _tap(ctype="group")
    assert main._pending_renames == {}, "a refused tap must not arm a rename"
    assert callback.alerts, "and the tap is told why"


def test_the_name_command_still_works():
    """/name <id> New Name is the power-user path and stays exactly as it was."""
    _package()
    message = _Message(f"/name {TID} A better name")
    asyncio.run(main.name_handler(message))
    assert storage.track_sessions[CHAT][TID]["name"] == "A better name"
    assert any("Renamed to" in text for text, _kw in message.sent)

