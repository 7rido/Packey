"""Privacy guarantees of the chat surface.

Two promises the bot makes in /privacy, pinned down here:
  1. messages that carry package data delete themselves (config.EPHEMERAL_TTL),
  2. tracking numbers never reach the log file in the clear.
The notification side (a ping that names nothing) is covered in test_tracker.py.
"""
import asyncio
import inspect
from datetime import datetime, timedelta, timezone

import botstate
import carriers
import main
import refresh
import storage
import ui
import usps_browser

TID = "9400111206200074888350"


class _Req:
    """Minimal aiohttp-request stand-in (headers only)."""

    def __init__(self, headers):
        self.headers = headers


class _Sent:
    def __init__(self, chat_id, message_id):
        self.chat = type("Chat", (), {"id": chat_id})()
        self.message_id = message_id


class _Msg:
    """Minimal stand-in for an aiogram Message: just enough for answer()."""

    def __init__(self, chat_id=7):
        self.answers = []
        self._chat_id = chat_id
        self.chat = type("Chat", (), {"id": chat_id, "type": "private"})()
        self.text = ""

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _Sent(self._chat_id, 42)


class _Bot:
    def __init__(self):
        self.deleted = []
        self.sent = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return _Sent(chat_id, 99)


def _with_bot(monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(botstate, "bot", bot)
    return bot


def test_ephemeral_card_is_deleted(monkeypatch):
    bot = _with_bot(monkeypatch)

    async def run():
        sent = await ui.send_ephemeral(_Msg(7), "package card", ttl=0.05)
        await asyncio.sleep(0.2)   # past the ttl → deletion task has fired
        return sent

    sent = asyncio.run(run())
    assert sent.message_id == 42
    assert bot.deleted == [(7, 42)]


def test_default_ttl_comes_from_config(monkeypatch):
    bot = _with_bot(monkeypatch)
    monkeypatch.setattr(ui.config, "EPHEMERAL_TTL", 0.05)

    async def run():
        await ui.send_ephemeral(_Msg(7), "package card")   # no explicit ttl
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert bot.deleted == [(7, 42)]


def test_ttl_zero_keeps_the_message(monkeypatch):
    """The owner's escape hatch: EPHEMERAL_TTL=0 restores plain messages."""
    bot = _with_bot(monkeypatch)
    monkeypatch.setattr(ui.config, "EPHEMERAL_TTL", 0)

    async def run():
        await ui.send_ephemeral(_Msg(7), "package card")
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert bot.deleted == []


def test_default_ttl_is_switched_on():
    assert ui.config.EPHEMERAL_TTL > 0


def test_every_data_bearing_send_is_ephemeral():
    """track cards, /list and /info are the messages that carry tracking
    numbers — they must all go through send_ephemeral.

    The /track cards now live in the shared add pipeline (_add_packages), which
    the screenshot path uses too, so both entry points are covered by one rule.
    """
    for handler in (main._add_packages, main.list_handler, main.info_handler):
        src = inspect.getsource(handler)
        assert "send_ephemeral" in src, f"{handler.__name__} sends a plain message"
    # ...and every add entry point must route into that pipeline.
    for entry in (main.track_handler, main.photo_handler):
        assert "_add_packages" in inspect.getsource(entry), entry.__name__


def test_deletion_failure_is_swallowed(monkeypatch):
    """A group where the bot can't delete (not admin) must not break anything."""
    class _RefusingBot(_Bot):
        async def delete_message(self, chat_id, message_id):
            raise RuntimeError("not enough rights")

    monkeypatch.setattr(botstate, "bot", _RefusingBot())

    async def run():
        await ui.send_ephemeral(_Msg(7), "package card", ttl=0.05)
        await asyncio.sleep(0.2)

    asyncio.run(run())   # no exception escapes


def _iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class TestRetention:
    """Lifecycle deletion: a delivered package is terminal, so the number stops
    being useful the moment the parcel arrives (see config.RETENTION_DAYS)."""

    def _setup(self, monkeypatch, packages, days=14):
        monkeypatch.setattr(refresh.config, "RETENTION_DAYS", days)
        monkeypatch.setattr(refresh, "_save_data_fn", lambda: None)
        # Purging also tells 17TRACK to stop tracking (refresh.py:271). Without
        # this the tests would hit the live API — and leak its socket, which
        # `-W error` then blames on whatever test happens to run next.
        self.deregistered = []
        monkeypatch.setattr(carriers, "stop_17track",
                            self._record_stop)
        storage.track_sessions.clear()
        storage.track_sessions[7] = packages

    async def _record_stop(self, numbers):
        self.deregistered.extend(numbers)
        return len(numbers)

    def test_old_delivered_packages_are_forgotten(self, monkeypatch):
        self._setup(monkeypatch, {
            "OLD": {"name": "Box", "status": "Delivered", "last_updated": _iso_days_ago(15)},
            "NEW": {"name": "Box", "status": "Delivered", "last_updated": _iso_days_ago(3)},
        })
        try:
            purged = asyncio.run(refresh._auto_purge_delivered())
            assert purged == {7: 1}
            assert list(storage.track_sessions[7]) == ["NEW"]
            # The forgotten number must also be deregistered at 17TRACK: local
            # deletion that leaves the partner tracking is not deletion.
            assert self.deregistered == ["OLD"]
        finally:
            storage.track_sessions.clear()

    def test_non_delivered_packages_are_never_auto_purged(self, monkeypatch):
        """In Transit is not terminal, and Exception can resume — those stay the
        user's call, not the timer's."""
        self._setup(monkeypatch, {
            "MOVING": {"name": "Box", "status": "In Transit", "last_updated": _iso_days_ago(99)},
            "STUCK": {"name": "Box", "status": "Exception", "last_updated": _iso_days_ago(99)},
            "WAITING": {"name": "Box", "status": "Info Received", "last_updated": _iso_days_ago(99)},
        })
        try:
            assert asyncio.run(refresh._auto_purge_delivered()) == {}
            assert len(storage.track_sessions[7]) == 3
        finally:
            storage.track_sessions.clear()

    def test_zero_days_disables_retention(self, monkeypatch):
        self._setup(monkeypatch, {
            "OLD": {"name": "Box", "status": "Delivered", "last_updated": _iso_days_ago(999)},
        }, days=0)
        try:
            assert asyncio.run(refresh._auto_purge_delivered()) == {}
            assert "OLD" in storage.track_sessions[7]
        finally:
            storage.track_sessions.clear()

    def test_unparsable_timestamp_is_never_guessed(self, monkeypatch):
        """Retention must not delete on a made-up date."""
        self._setup(monkeypatch, {
            "BROKEN": {"name": "Box", "status": "Delivered", "last_updated": "not-a-date"},
            "MISSING": {"name": "Box", "status": "Delivered"},
        })
        try:
            assert asyncio.run(refresh._auto_purge_delivered()) == {}
            assert len(storage.track_sessions[7]) == 2
        finally:
            storage.track_sessions.clear()

    def test_legacy_stamp_without_offset_still_works(self, monkeypatch):
        self._setup(monkeypatch, {
            "LEGACY": {"name": "Box", "status": "Delivered",
                       "last_updated": "2020-01-01T00:00:00"},   # no tzinfo
        })
        try:
            assert asyncio.run(refresh._auto_purge_delivered()) == {7: 1}
        finally:
            storage.track_sessions.clear()

    def test_retention_notice_names_nothing(self, monkeypatch):
        monkeypatch.setattr(refresh.config, "MINI_APP_URL", "https://app.example")
        monkeypatch.setattr(refresh.config, "RETENTION_DAYS", 14)
        info = {"name": "AliExpress order", "status": "Delivered",
                "carrier_name": "China Post"}
        text, kb = refresh.build_retention_notice(2)
        for secret in ("RR123456789CN", info["name"], info["status"], info["carrier_name"]):
            assert secret not in text
        assert "2 delivered packages" in text and "14 days" in text
        assert "RR" not in kb.inline_keyboard[0][0].url
        singular = refresh.build_retention_notice(1)[0]
        assert "A delivered package was" in singular and "about it" in singular


def test_list_never_shows_tracking_numbers(monkeypatch):
    """/list lands in the same permanent chat history the notifications avoid —
    it lists names, statuses and carriers, and points at the app."""
    monkeypatch.setattr(botstate, "bot", _Bot())
    monkeypatch.setattr(main.config, "EPHEMERAL_TTL", 0)      # no delete task
    monkeypatch.setattr(main.config, "MINI_APP_URL", "https://app.example")
    storage.track_sessions.clear()
    storage.track_sessions[7] = {
        "RR123456789CN": {"name": "AliExpress order", "status": "In Transit",
                          "carrier_name": "China Post"},
        "9400111206200074888350": {"name": "USPS box", "status": "Delivered",
                                   "carrier_name": "USPS"},
    }
    try:
        msg = _Msg(7)
        asyncio.run(main.list_handler(msg))
        text = msg.answers[0][0]
        for tid in ("RR123456789CN", "9400111206200074888350"):
            assert tid not in text
        assert "AliExpress order" in text and "In Transit" in text
        assert "China Post" in text
        assert "1." in text and "2." in text
        assert "app" in text.lower()          # the pointer to the real data
    finally:
        storage.track_sessions.clear()


def test_chat_ids_are_masked_in_logs():
    """Chat IDs are personal identifiers: logs get a stable, irreversible tag
    instead (same chat → same tag, so a story stays followable)."""
    tag = storage.mask_chat(123456789)
    assert tag.startswith("chat:") and len(tag) == 13
    assert "123456789" not in tag
    assert storage.mask_chat(123456789) == tag       # stable per chat
    assert storage.mask_chat(987654321) != tag       # distinct per chat
    # int and str forms of one chat must agree: ids arrive as ints from aiogram
    # and as strings out of JSON, and the tag has to be stable across both.
    assert storage.mask_chat("123456789") == tag


def test_log_sites_mask_every_chat_id():
    """Source gate: any log call whose arguments carry a chat id must route it
    through mask_chat.

    AST-based on purpose, like the tracking-number gate further down. The first
    version scanned source lines for "chat=%s"/"chat %s", so a call that
    formatted the id as "(%s/%s)" passed while still writing the raw id to disk —
    ui.py's ephemeral-delete line did exactly that.

    Boundary, same as the other gate: an attribute (message.from_user.id) or a
    bare container holding the id is not caught — only a name that IS the id.
    """
    import ast
    import miniapp
    import pro
    import usps_api

    chatted = {"chat_id", "chatid", "cid", "user_id", "owner_id"}
    levels = {"debug", "info", "warning", "error", "exception", "critical"}
    offenders = []
    for module in (main, carriers, miniapp, pro, refresh, storage, ui,
                   usps_api, usps_browser):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in levels):
                continue
            owner = func.value
            name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
            if name not in ("logger", "LOGGER"):
                continue
            for arg in node.args:
                for value in (arg.elts if isinstance(arg, ast.Tuple) else [arg]):
                    if isinstance(value, ast.Name) and value.id in chatted:
                        offenders.append(f"{module.__name__}.py:{node.lineno} logs "
                                         f"{value.id} unmasked")
                    elif isinstance(value, ast.JoinedStr):
                        for part in value.values:
                            if (isinstance(part, ast.FormattedValue)
                                    and isinstance(part.value, ast.Name)
                                    and part.value.id in chatted):
                                offenders.append(
                                    f"{module.__name__}.py:{node.lineno} logs "
                                    f"{part.value.id} in an f-string")
    assert not offenders, "unmasked chat ids in logs: " + "; ".join(offenders)


def test_logs_carry_no_handles_memos_or_partial_ids():
    """Source gate: no human handle, no payer memo, no truncated identifier.

    mask_chat covers the chat id, but three other things were still landing in the
    log: the Telegram username on /start, the first six characters of every stored
    tracking number on a Mini App 404, and the payer's memo on an unmatched Solana
    transfer. A handle names a person, a six-character prefix is a partial tracking
    number, and a memo is payer-written free text — none of them belongs in the one
    file that is never meant to hold user data.
    """
    import ast
    import miniapp
    import pro
    import usps_api

    handles = {"username", "first_name", "last_name", "full_name"}
    free_text = {"memo", "note", "comment"}
    # Truncation is how a log stays readable, so it is allowed for values that are
    # public anyway — a chain signature, a carrier's event text — and for nothing
    # else: a shortened identifier is still a partial identifier.
    public_ok = {"sig", "detail"}
    levels = {"debug", "info", "warning", "error", "exception", "critical"}
    offenders = []
    for module in (main, carriers, miniapp, pro, refresh, storage, ui,
                   usps_api, usps_browser):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in levels):
                continue
            owner = func.value
            name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
            if name not in ("logger", "LOGGER"):
                continue
            values = []
            for arg in node.args:
                # ast.walk, not the top level only: the first version of this gate
                # looked at direct arguments and therefore missed the very shape it
                # was written for — [k[:6] for k in pkgs] hides the subscript inside
                # a comprehension.
                walked = list(ast.walk(arg))
                # len(x) is a size, not the text: logging that a memo is 41 bytes is
                # the point of that line, and it is not the memo.
                sized = {n.id for call in walked
                         if isinstance(call, ast.Call)
                         and isinstance(call.func, ast.Name)
                         and call.func.id == "len"
                         for n in ast.walk(call) if isinstance(n, ast.Name)}
                values.extend(w for w in walked
                              if not (isinstance(w, ast.Name) and w.id in sized))
            for value in values:
                if isinstance(value, ast.Attribute) and value.attr in handles:
                    offenders.append(f"{module.__name__}.py:{node.lineno} logs "
                                     f".{value.attr}")
                elif isinstance(value, ast.Name) and value.id in free_text:
                    offenders.append(f"{module.__name__}.py:{node.lineno} logs "
                                     f"{value.id}")
                elif (isinstance(value, ast.Subscript)
                        and isinstance(value.value, ast.Name)
                        and value.value.id not in public_ok):
                    upper = getattr(value.slice, "upper", None)
                    if isinstance(upper, ast.Constant) and isinstance(upper.value, int):
                        offenders.append(
                            f"{module.__name__}.py:{node.lineno} logs a "
                            f"{upper.value}-character prefix of {value.value.id}")
    assert not offenders, "user data in logs: " + "; ".join(offenders)


def test_browser_logs_carry_no_raw_exceptions():
    """Source gate, scoped to usps_browser.py on purpose.

    Logging the raw exception is normal diagnostic practice everywhere else (a
    local OSError names a file, not a person). In the browser tier it is a leak:
    Playwright's TimeoutError text embeds the navigated URL, and that URL carries
    the raw tracking id — _mask(tid) covers the argument, never the exception text.
    type(e).__name__ is the safe form and is allowed explicitly.
    """
    import ast
    import usps_browser

    raw_exc = {"e", "err", "error", "exc", "exception", "ex"}
    levels = {"debug", "info", "warning", "error", "exception", "critical"}
    offenders = []
    tree = ast.parse(inspect.getsource(usps_browser))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in levels):
            continue
        owner = func.value
        name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
        if name not in ("LOGGER", "logger"):
            continue
        # type(e).__name__ is the deliberate exception: the type NAME is what may
        # be logged, so any name appearing only inside a type() call is safe.
        typed = {n.id for call in ast.walk(node)
                 if isinstance(call, ast.Call)
                 and isinstance(call.func, ast.Name)
                 and call.func.id == "type"
                 for n in ast.walk(call) if isinstance(n, ast.Name)}
        for arg in node.args:
            for value in ast.walk(arg):
                if isinstance(value, ast.Name) and value.id in raw_exc \
                        and value.id not in typed:
                    offenders.append(f"usps_browser.py:{node.lineno} logs the raw "
                                     f"exception {value.id}")
    assert not offenders, "raw exceptions in browser logs: " + "; ".join(offenders)


def test_capability_urls_are_redacted_in_logs():
    """Share tokens and invoice ids are bearer capabilities: whoever holds the
    path holds the page. They must travel in the user's chat, never in a log —
    and aiohttp's access log writes every request path to tracker.log."""
    red = main._redact_text
    line = red("GET /share/abc123def456 HTTP/1.1")
    assert "/share/<redacted>" in line and "abc123def456" not in line
    line = red("GET /pay/inv-abc123def456/tx HTTP/1.1")
    assert "/pay/<redacted>/tx" in line and "inv-abc123def456" not in line
    assert red("GET /api/packages HTTP/1.1") == "GET /api/packages HTTP/1.1", \
        "ordinary paths must survive untouched"


def test_the_bot_token_path_is_redacted_from_logs():
    """The pre-Bot alert path posts to api.telegram.org/bot<TOKEN>/sendMessage and
    httpx logs the full request line at INFO. The token in that path is the bot's
    master credential: its path shape must redact like the capability URLs do
    (2026-10-04 audit)."""
    red = main._redact_text
    line = red("HTTP Request: POST https://api.telegram.org/"
               "bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage HTTP/1.1")
    assert "/bot<redacted>/sendMessage" in line, "the token segment must be redacted"
    assert "123456789:" not in line and "AAHdqTcv" not in line
    # An ordinary path that merely contains "/bot" and no token shape survives.
    assert red("GET /bot/about HTTP/1.1") == "GET /bot/about HTTP/1.1"
    assert red("GET /bottling-plant HTTP/1.1") == "GET /bottling-plant HTTP/1.1"


def test_tracebacks_are_redacted_too():
    """A filter sees record.getMessage() only; exc_info is rendered later by the
    handler — so a secret inside an exception's own text sailed past it."""
    import logging
    fmt = main._RedactingFormatter("%(levelname)s %(message)s")
    try:
        raise RuntimeError("connect failed for https://x.test/?api-key=SECRET123")
    except RuntimeError as exc:
        text = fmt.formatException((type(exc), exc, exc.__traceback__))
    assert "SECRET123" not in text, "the traceback carried the secret"
    assert "<redacted>" in text and "RuntimeError" in text, \
        "the useful part must survive"


def test_url_embedded_secrets_are_redacted_whatever_the_parameter_name():
    """The log filter was written for the one leak that was measured (Helius's
    api-key=), but httpx logs the whole request URL at INFO: the next provider to
    put a credential in a query parameter must not write it to tracker.log.
    """
    import logging
    redact = main._SecretRedactor()
    for param in ("api-key", "api_key", "apikey", "key", "token",
                  "access_token", "secret", "password"):
        url = f"https://provider.example.com/v1?{param}=SECRET123&page=2"
        record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                                   "HTTP Request: GET %s", (url,), None)
        assert redact.filter(record) is True
        message = record.getMessage()
        assert "SECRET123" not in message, param
        assert "<redacted>" in message, param
        assert "page=2" in message, f"{param}: the rest of the URL must survive"
    # A URL with nothing secret in it stays readable: the log must remain useful.
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1,
                               "HTTP Request: GET %s",
                               ("https://x.example.com/v1?page=2&monkey=banana",), None)
    redact.filter(record)
    assert record.getMessage() == \
        "HTTP Request: GET https://x.example.com/v1?page=2&monkey=banana"


def test_forget_list_never_shows_tracking_numbers(monkeypatch):
    """The whole point of /forget: delete without ever typing the number, and
    without the list re-printing it into the chat."""
    monkeypatch.setattr(botstate, "bot", _Bot())
    storage.track_sessions.clear()
    storage.track_sessions[7] = {
        "RR123456789CN": {"name": "AliExpress order", "status": "In Transit"},
        "9400111206200074888350": {"name": "USPS box", "status": "Delivered"},
    }
    try:
        msg = _Msg(7)
        asyncio.run(main.forget_handler(msg))
        text, kwargs = msg.answers[0]
        for tid in ("RR123456789CN", "9400111206200074888350"):
            assert tid not in text
        assert "AliExpress order" in text and "USPS box" in text

        buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
        assert [b.text for b in buttons][:2] == ["1", "2"]   # indices, not numbers
        assert any(b.callback_data.startswith("forget:") for b in buttons)
        assert any(b.callback_data == "forget_all_delivered" for b in buttons)
    finally:
        storage.track_sessions.clear()


def test_tracking_ids_are_masked_in_logs():
    masked = carriers._sanitize_tracking_id(TID)
    assert masked != TID
    assert TID not in masked
    assert masked.startswith("9400") and masked.endswith("8350")
    assert usps_browser._mask(TID) == masked   # the browser tier uses the same mask


def test_privacy_command_states_the_load_bearing_claims():
    """/privacy is the consent text users actually read — a copy edit must not
    silently drop the promises it makes.

    The text now lives in ui.PRIVACY_TEXT (shared with the Mini App sheet so the
    two surfaces can't drift), so the claims are checked there.
    """
    import ui
    text = ui.PRIVACY_TEXT
    for claim in ("What's stored",
                  "No tracking number, name, status or carrier",   # data-free pings
                  "delete themselves",                             # ephemeral cards
                  "the carriers' own sites",                       # source routing
                  "Ship24 as a paid last-resort",                  # paid fallback disclosed
                  "never checked again",                           # adaptive cadence
                  "encrypted at rest (AES + HMAC)",
                  "the image is deleted immediately",              # screenshot OCR
                  "forgotten automatically after",                  # retention
                  "/forget",                                        # number-free deletion
                  "and your Pro payment records",                   # payment stores
                  "plus the payment address reserved for you",       # the registry
                  "Three things outlive the package",                # what deletion does NOT cover
                  "hold no tracking number",                         # receipts + address, still true
                  "the numbers themselves",                          # the intl record holds them — honest
                  "re-adding one you already registered is free",    # why the tally keeps them
                  "stable, or a payment already sent",              # why the address persists
                  "That payment address is stable",                 # §4, not "personal address"
                  "linked to one another on-chain",                 # address linkage
                  "Telegram (the platform) sees your chats"):      # honest limits
        assert claim in text, f"/privacy no longer states: {claim}"


def test_privacy_has_one_source_for_bot_and_mini_app():
    """The bot command and the Mini App sheet must render the same policy: the
    command sends the constant, the app's endpoint serves it (main.py re-exports
    it for the whole re-export surface)."""
    import ui
    import miniapp
    import main
    assert main.PRIVACY_TEXT is ui.PRIVACY_TEXT
    body = inspect.getsource(main.privacy_handler)
    assert "PRIVACY_TEXT" in body, "the command must not inline its own copy"
    assert "text = (" not in body, "a second inline copy would drift again"
    routes = {r.resource.canonical for r in miniapp._build_miniapp_app().router.routes()}
    assert "/api/privacy" in routes
    payload = asyncio.run(miniapp._api_privacy(_Req({"Authorization": "tma bad"})))
    assert payload.status == 401, "privacy endpoint must still require initData"


def test_every_privacy_part_fits_telegram():
    """Telegram rejects a message over 4096 chars, and the rejection is total:
    /privacy was 5,131 chars, the send raised, and the user saw NOTHING at all
    (it failed 3x in the live log on 2026-09-28). Pinned against the real cap
    rather than the 4,000 a test can read at a glance, so a copy edit cannot
    silently re-break the command — the failure has no partial output to notice.
    """
    import ui
    for part in ui.privacy_parts():
        assert len(part) <= 4096, f"a /privacy message is {len(part)} chars"


def test_privacy_parts_preserve_the_whole_text():
    """Splitting is a transport detail, not an edit: joined back, the parts must
    be the policy verbatim, in order, with nothing dropped at the seam. A lost
    section here would delete a promise from the consent text."""
    import ui
    assert "".join(ui.privacy_parts()) == ui.PRIVACY_TEXT
    assert ui.privacy_parts() == ui.PRIVACY_PARTS   # the section split suffices today


def test_privacy_sends_every_part_and_keeps_them_persistent():
    """End to end through the handler: two messages go out, neither is empty,
    and together they carry the whole policy. send_ephemeral is checked in
    test_chat_tidy.py — /privacy is the consent text, meant to be re-read."""
    import main
    msg = _Msg(7)
    asyncio.run(main.privacy_handler(msg))
    assert len(msg.answers) == 2, "/privacy must arrive, not fail"
    texts = [text for text, _ in msg.answers]
    assert all(texts) and "".join(texts) == main.PRIVACY_TEXT
    for _text, kwargs in msg.answers:
        assert kwargs["parse_mode"] == "HTML"
    assert "<b>1. What" in texts[0] and "<b>4. Who sees what" in texts[1]


def test_oversized_part_is_split_rather_than_dropped(monkeypatch):
    """The section split is a courtesy, not a guarantee: a later copy edit can
    push one half past the cap again. privacy_parts() re-splits on paragraph
    breaks so an over-long policy becomes MORE messages, never a blank reply."""
    import ui
    long_half = "\n\n".join(f"<b>P{i}</b> — " + "x" * 900 for i in range(12))
    assert len(long_half) > 4096
    monkeypatch.setattr(ui, "PRIVACY_PARTS", (long_half,))
    parts = ui.privacy_parts()
    assert len(parts) > 1, "an oversized part must not be sent as-is"
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == long_half          # still verbatim, just more messages
    assert not any(p.startswith("\n") for p in parts)   # no leading blank seam


def test_a_part_with_no_paragraph_room_is_still_cut(monkeypatch):
    """The fallback for a single paragraph longer than the cap: there is no
    boundary to break on, so cut at the cap rather than raise — a truncated
    paragraph is recoverable, a rejected send shows nothing."""
    import ui
    one_giant = "y" * 9000
    monkeypatch.setattr(ui, "PRIVACY_PARTS", (one_giant,))
    parts = ui.privacy_parts()
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == one_giant


def test_log_sites_mask_every_tracking_number():
    """Source gate: a log call must never interpolate a tracking number in the
    clear.

    A tracking number is what lets whoever holds the log look the parcel up, so
    it is an identifier exactly like a chat id — and it was masked everywhere
    except two places: usps_api.py logged the raw number (and the raw response
    body), and carriers.py's USPS Web Tools block logged it alongside the raw
    error XML, which embeds <TrackID>. A masked value reaches the log call as a
    CALL argument (_mask(...) / _sanitize_tracking_id(...)), so a bare name or
    an f-string placeholder is what this looks for.
    """
    import ast
    import carriers
    import miniapp
    import pro
    import refresh
    import ship24
    import usps_api
    import usps_browser

    tracked = {"tracking_id", "tid", "tracking_ids"}
    levels = {"debug", "info", "warning", "error", "exception", "critical"}
    offenders = []
    for module in (main, carriers, miniapp, pro, refresh, ship24, storage, ui,
                   usps_api, usps_browser):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in levels):
                continue
            owner = func.value
            name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
            if name not in ("logger", "LOGGER"):
                continue
            for arg in node.args:
                for value in (arg.elts if isinstance(arg, ast.Tuple) else [arg]):
                    if isinstance(value, ast.Name) and value.id in tracked:
                        offenders.append(f"{module.__name__}.py:{node.lineno} logs "
                                         f"{value.id} unmasked")
                    elif isinstance(value, ast.JoinedStr):
                        for part in value.values:
                            if (isinstance(part, ast.FormattedValue)
                                    and isinstance(part.value, ast.Name)
                                    and part.value.id in tracked):
                                offenders.append(
                                    f"{module.__name__}.py:{node.lineno} logs "
                                    f"{part.value.id} in an f-string")
    assert not offenders, "unmasked tracking numbers in logs: " + "; ".join(offenders)
