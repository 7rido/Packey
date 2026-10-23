"""The user's side of the chat is cleared too, once the bot has run on the message.

The bot's own data-bearing messages were already ephemeral; the message the USER
sent was not, so a /track line, a pasted number or a label photo sat in history
forever. The rule (main._is_bot_input) is deliberately narrow: what the bot acted
on, never the surrounding conversation, and never a run that failed.
"""
import asyncio

import pytest

import botstate
import main
import storage

CHAT = 7
NAME = "Mom's Gift"
NUMBER = "9400111206200074888350"


class _Bot:
    async def delete_message(self, chat_id, message_id):
        return None


class _Chat:
    def __init__(self, chat_id=CHAT, chat_type="private"):
        self.id = chat_id
        self.type = chat_type


class _User:
    id = 5
    first_name = "F"


class _Sent:
    def __init__(self, chat, message_id=42):
        self.chat = chat
        self.message_id = message_id


class _Msg:
    """A message as the middleware sees it: text or photo, and a delete() dial."""

    def __init__(self, text=None, photo=False, chat_type="private", delete_fails=False):
        self.chat = _Chat(chat_type=chat_type)
        self.from_user = _User()
        self.text = text
        self.message_id = 42          # real Messages always carry one
        self.photo = [object()] if photo else []
        self.answers = []
        self.delete_fails = delete_fails
        self.delete_calls = 0

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _Sent(self.chat)

    async def delete(self):
        self.delete_calls += 1
        if self.delete_fails:
            raise RuntimeError("not enough rights to delete this message")
        return True


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(botstate, "bot", _Bot())
    monkeypatch.setattr(main.config, "EPHEMERAL_TTL", 0)
    storage.track_sessions.clear()
    storage.track_sessions[CHAT] = {NUMBER: {"name": NAME, "status": "In Transit"}}
    yield
    storage.track_sessions.clear()


def _through(msg, next_handler=None):
    """Run the middleware exactly as aiogram does: around the handler."""
    async def _next(event, data):
        if next_handler is None:
            return "handled"
        return await next_handler(event)      # bot handlers take the message

    return asyncio.run(main.tidy_user_message(_next, msg, {}))


def test_the_gate_covers_what_the_bot_acts_on():
    """Commands, photos and pasted numbers — and nothing else."""
    assert main._is_bot_input(_Msg(text="/list"))
    assert main._is_bot_input(_Msg(text="/info"))
    assert main._is_bot_input(_Msg(photo=True))
    assert main._is_bot_input(_Msg(photo=True, text="a caption"))
    assert main._is_bot_input(_Msg(text=NUMBER)), "a pasted tracking number"
    assert main._is_bot_input(_Msg(text=f"  {NUMBER}  "))
    for ordinary in (None, "", "   ", "hello mum", "how are you?", "thanks!"):
        assert not main._is_bot_input(_Msg(text=ordinary)), ordinary


def test_a_command_is_deleted_after_it_runs():
    """Run first, delete second: the card for /info still arrives."""
    msg = _Msg(text=f"/info {NAME}")
    _through(msg, main.info_handler)

    assert msg.answers and NUMBER in msg.answers[0][0], "the command must have run"
    assert msg.delete_calls == 1, "the command message must be cleared"


def test_a_photo_is_deleted_after_it_runs():
    msg = _Msg(photo=True)
    seen = []

    async def handler(event):
        seen.append("ran")
        return "ok"

    assert _through(msg, handler) == "ok", "the handler's result must pass through"
    assert seen == ["ran"] and msg.delete_calls == 1


def test_a_pasted_tracking_number_is_deleted():
    msg = _Msg(text=NUMBER)
    _through(msg, main.unknown_handler)
    assert msg.delete_calls == 1, "a pasted number must not stay in history"


def test_ordinary_chat_is_left_alone():
    """In a group the bot must not police the conversation."""
    msg = _Msg(text="hello mum", chat_type="supergroup")
    _through(msg, main.unknown_handler)
    assert msg.delete_calls == 0


def test_a_failed_run_leaves_the_message():
    """The user should be able to read what they sent instead of wondering where
    it went."""
    msg = _Msg(text=f"/info {NAME}")

    async def boom(event):
        raise RuntimeError("handler exploded")

    with pytest.raises(RuntimeError):
        _through(msg, boom)
    assert msg.delete_calls == 0, "a crash must not eat the user's message"


def test_a_refused_delete_does_not_break_the_command():
    """Groups without the 'delete messages' right (and messages too old to touch)
    must not turn a working command into an error."""
    msg = _Msg(text=f"/info {NAME}", delete_fails=True)
    assert _through(msg, main.info_handler) is None, "the handler's own result"
    assert msg.answers and NUMBER in msg.answers[0][0]
    assert msg.delete_calls == 1


def _sender_of(handler, marker):
    """The call that sends the reply containing `marker`, found by walking up from it.

    A handler can legitimately contain both kinds — an error line that expires and a
    prompt that must not — so the assertion has to be about the specific statement.
    """
    import inspect

    src = inspect.getsource(handler).splitlines()
    for index, line in enumerate(src):
        if marker in line:
            for up in range(index, -1, -1):
                if "answer(" in src[up] or "send_ephemeral(" in src[up]:
                    return src[up].strip()
    raise AssertionError(f"{handler.__name__}: no send found for {marker!r}")


def test_chatty_replies_self_delete_and_button_prompts_do_not():
    """The declutter rule, pinned at the source.

    A reply that only tells the user something goes through send_ephemeral, so it
    clears itself like the cards do. A reply that carries BUTTONS must not: deleting
    it removes the buttons before they can be tapped, which is why the removal /
    purge / clear prompts and the barcode confirm prompt stay put.
    """
    import inspect

    for handler, snippet in (
        (main.track_handler, "Usage:"),
        (main.info_handler, "No package with that tracking number"),
        (main.unknown_handler, "I don't understand"),
        (main.list_handler, "No packages yet"),
        (main.photo_handler, "Couldn't find a tracking number"),
        (main.clear_handler, "Only group admins"),
    ):
        assert snippet in inspect.getsource(handler), f"{handler.__name__}: reply moved"
        sender = _sender_of(handler, snippet)
        assert "send_ephemeral" in sender, f"{handler.__name__}: the reply lingers"

    for handler in (main.remove_handler, main.purge_handler, main.clear_handler,
                    main._ask_barcode_add):
        sender = _sender_of(handler, "build_confirm_keyboard")
        assert "message.answer(" in sender and "send_ephemeral" not in sender, (
            f"{handler.__name__}: a button prompt must not expire before it is tapped"
        )

    # The consent text is the one reply a user may want to re-read minutes later;
    # it is deliberately left persistent (re-runnable with /privacy). It is sent
    # as several messages (the policy is longer than Telegram's 4096 cap), so the
    # marker is the answer call itself — PRIVACY_PARTS sits on the `for` line
    # above it, and this helper only walks upward.
    sender = _sender_of(main.privacy_handler, "message.answer(part")
    assert "send_ephemeral" not in sender
    assert "PRIVACY_PARTS" in inspect.getsource(main.privacy_handler), (
        "privacy_handler must send the split parts, not the whole text"
    )


def test_a_usage_reply_keeps_the_message():
    """The user must be able to see what they sent wrong and fix it."""
    msg = _Msg(text="/mute")
    _through(msg, main.mute_handler)
    assert msg.delete_calls == 0, "a usage reply must not eat the user's message"
    assert msg.answers, "and the usage line must still arrive"


def test_an_unknown_command_is_kept_and_answered():
    """Not ours → left in place, answered once, and not deleted out from under
    the user."""
    msg = _Msg(text="/frobnicate")
    _through(msg, main.unknown_handler)
    assert msg.delete_calls == 0
    assert any("don't understand" in t for t, _ in msg.answers)


def test_a_failed_command_still_deletes_when_it_carries_a_number():
    """Privacy wins over retry-ability: a message carrying a tracking number must
    not linger, even when the command fails (nothing to mute)."""
    msg = _Msg(text="/mute 9400111206200074888350")
    _through(msg, main.mute_handler)
    assert msg.delete_calls == 1, "the number must not stay in history"


def test_every_menu_command_is_known_to_the_middleware():
    """The middleware decides 'is this command ours?' from MENU_COMMANDS; the ☰
    menu is written separately. If they drift apart, a registered command stops
    being recognized and its message stops being cleared — pin them together."""
    import inspect

    menu = {name for name, _ in main.MENU_ITEMS}      # the menu's single source
    assert menu, "no commands menu parsed"
    missing = menu - main.MENU_COMMANDS
    assert not missing, f"menu commands the middleware does not recognize: {missing}"
    # …and the middleware's known-command set is exactly menu + aliases + /ping.
    assert main.MENU_COMMANDS - menu == {"start", "add", "packages", "rename",
                                         "unmute", "ping"}


def test_the_middleware_is_actually_registered():
    """A tidy-up nobody calls would be dead code — and this is the one place the
    rule is applied, so it must stay wired to the router."""
    manager = main.router.message.outer_middleware
    registered = getattr(manager, "_middlewares", None)
    if registered is None:                      # pragma: no cover - aiogram shape change
        registered = getattr(manager, "middlewares", None)
    assert registered is not None, "cannot read aiogram's middleware manager"
    assert main.tidy_user_message in list(registered), \
        "the delete-after-run middleware is not registered on the router"
