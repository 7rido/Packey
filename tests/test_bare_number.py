"""A pasted tracking number must work — /start promises exactly that.

"Send a tracking number — that's it." was NOT implemented (found 2026-09-26): a bare
number fell through to "🧠 I don't understand that — I'm just a package tracker", and
the tidy middleware then deleted the user's own message along with it, so the headline
flow of the whole product did nothing at all.

It now takes the same confirm-then-register prompt as a barcode read — the step that
makes a misread or a typo free, because registering spends a 17TRACK slot (sometimes a
paid lookup). Limits and the per-chat lock stay where they were: inside _add_packages,
so this path cannot bypass them.
"""
import asyncio
import time

import pytest

import main
import storage

CHAT = 4242
USER = 777
NUMBER = "9400111899223197428490"
OTHER = "RR123456789CN"


class _User:
    """The person in the chat — `first_name` is what an unnamed package is called."""

    def __init__(self, uid=USER):
        self.id = uid
        self.first_name = "F"


class _Chat:
    id = CHAT
    type = "private"


class _Sent:
    """A message the bot sent."""

    def __init__(self, text, markup=None, message_id=1):
        self.chat = _Chat()
        self.message_id = message_id
        self.text = text
        self.reply_markup = markup
        self.edited = None

    async def answer(self, text, **kw):
        return _Sent(text, kw.get("reply_markup"), self.message_id + 1)

    async def edit_text(self, text, **kw):
        self.edited = text
        return self


class _Message:
    def __init__(self, text):
        self.text = text
        self.chat = _Chat()
        self.from_user = _User()
        self.message_id = 5
        self.sent = []

    async def answer(self, text, **kw):
        sent = _Sent(text, kw.get("reply_markup"), len(self.sent) + 1)
        self.sent.append(sent)
        return sent


class _Callback:
    """A tap on the confirm prompt."""

    def __init__(self, message, data):
        self.message = message
        self.data = data
        self.from_user = _User()
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    saved = dict(storage.track_sessions)
    storage.track_sessions.clear()
    storage.pending_confirmations.clear()
    main._pending_renames.clear()

    async def fake_register(tracking_id, is_pro=None, chat_id=None):
        """No network, no quota spent: the registration seam is the stub."""
        return ({"name": "Box", "status": "Pending", "carrier": "usps"},
                "registered")

    monkeypatch.setattr(main, "_register_and_fetch", fake_register)
    yield
    storage.track_sessions.clear()
    storage.track_sessions.update(saved)
    storage.pending_confirmations.clear()
    main._pending_renames.clear()


def _prompts(message):
    return [sent for sent in message.sent if sent.reply_markup is not None]


def test_a_pasted_number_asks_before_spending_anything():
    message = _Message(NUMBER)
    asyncio.run(main.unknown_handler(message))
    prompt = _prompts(message)[-1]
    assert "From your message" in prompt.text, (
        "it was typed, not read off a barcode — the heading must not lie")
    assert NUMBER in prompt.text, "quote it back: that is what makes a typo free"
    assert storage.track_sessions.get(CHAT, {}) == {}, "nothing registered yet"
    assert storage.get_confirmation((CHAT, "add", NUMBER)) == USER


def test_confirming_registers_the_package():
    message = _Message(NUMBER)
    asyncio.run(main.unknown_handler(message))
    prompt = _prompts(message)[-1]
    data = prompt.reply_markup.inline_keyboard[0][0].callback_data
    asyncio.run(main.callback_confirm_add(_Callback(prompt, data)))
    assert NUMBER in storage.track_sessions[CHAT], "the tap registers it"


def test_ordinary_text_still_gets_the_fallback():
    message = _Message("hello there")
    asyncio.run(main.unknown_handler(message))
    assert any("don't understand" in sent.text for sent in message.sent)
    assert not _prompts(message), "no add prompt for something that is not a number"


def test_two_numbers_in_one_message_each_get_their_own_prompt():
    message = _Message(f"{NUMBER} {OTHER}")
    asyncio.run(main.unknown_handler(message))
    assert len(_prompts(message)) == 2
    assert storage.get_confirmation((CHAT, "add", NUMBER)) == USER
    assert storage.get_confirmation((CHAT, "add", OTHER)) == USER


def test_a_rename_answer_wins_over_the_number_path():
    """The rename prompt asked for a name, so a number-looking reply IS that name.

    Pinned deliberately: adding a package from a rename answer would be a surprise,
    and a wrongly-named package is visible and one tap to fix.
    """
    storage.track_sessions[CHAT] = {NUMBER: {"name": "Box"}}
    main._pending_renames[CHAT] = (NUMBER, USER, time.monotonic() + 60)
    message = _Message("1Z999AA10123456784")
    asyncio.run(main.unknown_handler(message))
    assert storage.track_sessions[CHAT][NUMBER]["name"] == "1Z999AA10123456784"
    assert not _prompts(message), "no add prompt was raised for it"
