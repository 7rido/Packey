"""`/info` by name, not just by tracking number.

Retyping a number to see a card is a copy-paste round trip to the Mini App, and
/forget's own reasoning says why it is worse than that: typing a number into chat
re-leaks it at the moment the user wants it gone. So a name resolves too — the exact
name the cards show, matched inside that chat only, and never guessed at when two
packages share one.
"""
import asyncio

import pytest

import botstate
import main
import storage

CHAT = 7
OTHER = 8
NUMBER = "9400111206200074888350"
UPS = "1Z1E852G0321884183"
NAME = "Mom's Gift"


class _Bot:
    async def delete_message(self, chat_id, message_id):
        return None


class _Chat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.type = "private"


class _User:
    def __init__(self, uid=5):
        self.id = uid
        self.first_name = "F"


class _Sent:
    def __init__(self, chat, message_id=42):
        self.chat = chat
        self.message_id = message_id


class _Msg:
    """A `/info ...` command message; records everything the handler answers."""

    def __init__(self, text, chat_id=CHAT):
        self.chat = _Chat(chat_id)
        self.from_user = _User()
        self.text = text
        self.message_id = 42          # real Messages always carry one
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _Sent(self.chat)


@pytest.fixture(autouse=True)
def _packages(monkeypatch):
    """This chat: one named package and one with no name at all. Another chat holds
    a package with the SAME name — it must never be reachable from here."""
    monkeypatch.setattr(botstate, "bot", _Bot())
    monkeypatch.setattr(main.config, "EPHEMERAL_TTL", 0)      # no delete task
    storage.track_sessions.clear()
    storage.track_sessions[CHAT] = {
        NUMBER: {"name": NAME, "status": "In Transit", "carrier_name": "USPS"},
        UPS: {"name": "", "status": "Pending", "carrier_name": "UPS"},
    }
    storage.track_sessions[OTHER] = {
        "RR123456789CN": {"name": "Other chat parcel", "status": "Delivered"},
    }
    yield
    storage.track_sessions.clear()


def _info(query, chat_id=CHAT):
    msg = _Msg(f"/info {query}", chat_id=chat_id)
    asyncio.run(main.info_handler(msg))
    return msg


def test_a_tracking_number_still_resolves():
    """The regression that matters most: everything that worked keeps working."""
    msg = _info(NUMBER)
    assert NUMBER in msg.answers[0][0], "the card must be sent for the number"
    assert storage.track_sessions[CHAT][NUMBER]["card_msg_id"] == 42


def test_a_name_resolves_case_insensitively():
    msg = _info("mom's gift")
    reply = msg.answers[0][0]
    assert NUMBER in reply, "the name must find the package"
    assert "Gift" in reply, "the card names the package it found"


def test_a_quoted_name_resolves():
    """Quotes are how /track handles a name with a space in it — same habit here."""
    assert NUMBER in _info(f'"{NAME}"').answers[0][0]


def test_a_blank_name_answers_to_what_the_bot_printed():
    """A package with no name is shown as "Package" on its card and "Unnamed" in
    /list: the user can only type what they were shown, so both work."""
    for spelling in ("package", "Unnamed"):
        assert UPS in _info(spelling).answers[0][0], spelling


def test_an_unknown_query_mentions_both_routes():
    msg = _info("nothing like this")
    assert "No package with that tracking number or name." in msg.answers[0][0]
    assert len(msg.answers) == 1 and UPS not in msg.answers[0][0]


def test_no_argument_shows_the_usage_line():
    msg = _Msg("/info")
    asyncio.run(main.info_handler(msg))
    assert "TRACKING_NUMBER_OR_NAME" in msg.answers[0][0]


def test_a_shared_name_asks_instead_of_guessing():
    """Two parcels named the same thing: list them and ask, never pick one — a card
    for the wrong parcel is worse than a question."""
    storage.track_sessions[CHAT][UPS]["name"] = NAME
    msg = _info(NAME)

    reply = msg.answers[0][0]
    assert "Several packages share that name" in reply
    assert NUMBER in reply and UPS in reply, "both candidates must be listed"
    assert "Re-run /info with the tracking number" in reply
    assert "📦" not in reply, "no card may be sent for an ambiguous name"
    assert "card_msg_id" not in storage.track_sessions[CHAT][NUMBER]


def test_the_info_route_is_wired_to_the_handler():
    """`/info` must route to the handler, and the resolver must stay a helper.

    This is not hypothetical: while adding the name lookup, `_name_forms` was
    inserted under the @router decorator, so aiogram called the HELPER for every
    /info — a Message has no .get() — and the command was dead in production while
    these tests (which call `info_handler` directly) all passed.
    """
    callbacks = [h.callback for h in main.router.message.handlers]
    assert main.info_handler in callbacks, "/info is not wired to its handler"
    for helper in (main._name_forms, main._resolve_package):
        assert helper not in callbacks, "a helper swallowed the /info route"


def test_a_name_never_resolves_across_chats():
    """Names are per chat: another chat's package must stay unreachable — by name
    and by number."""
    assert main._resolve_package(OTHER, "Other chat parcel").status == "found"
    for query in ("Other chat parcel", "RR123456789CN"):
        msg = _info(query)
        assert "No package with that tracking number or name." in msg.answers[0][0], query
        assert "📦" not in msg.answers[0][0], query
