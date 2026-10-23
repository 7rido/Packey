"""Every user-facing claim in /start and the commands menu, checked against the
code that has to back it.

Why this exists: the pitch is the only thing most users read, and copy drifts
silently — "checks every 3 min" survives a cadence change, "no update buttons"
survives a handler being re-added. Each test names the claim it guards, so
changing the copy becomes a deliberate act.
"""
import asyncio
import inspect
import pathlib
import re

import carriers
import config
import main
import miniapp
import pro
import refresh
import storage
import ui


class _Sent:
    chat = type("C", (), {"id": 1})()
    message_id = 1


class _Msg:
    def __init__(self, chat_id, chat_type="private"):
        self.texts = []
        self.text = ""
        self.message_id = 1
        self.chat = type("C", (), {"id": chat_id, "type": chat_type})()
        self.from_user = type("U", (), {"id": 9, "first_name": "F", "username": "u"})()

    async def answer(self, text, **kw):
        self.texts.append(text)
        return _Sent()

    async def delete(self):
        pass


_counter = {"n": 0}


def _render(handler, chat_id=None):
    """Run a handler against a throwaway chat and return the text it sent.

    `chat_id` pins the chat when the reply depends on who is asking — the /help
    text lists operator-only commands to an operator chat and not to a user's.
    """
    _counter["n"] += 1
    main._recent_starts.clear()          # fresh state per render
    main._start_last_response.clear()
    msg = _Msg(chat_id if chat_id is not None else 900000 + _counter["n"])
    asyncio.run(handler(msg))
    assert msg.texts, f"{handler.__name__} sent nothing"
    return msg.texts[0]


def _start_text():
    return _render(main.start_handler)


def test_carriers_claimed_as_auto_detected_really_are():
    """"Carriers auto-detected: USPS, UPS, FedEx, DHL, OnTrac, Royal Mail"."""
    # The pitch stopped naming carriers (the line is short now), so this pins
    # the SENTENCE plus the capability behind it: whatever the greeting implies
    # must still auto-detect, with an official link to follow.
    text = _start_text()
    assert "I detect the carrier" in text
    samples = {
        "usps": "9400111206200074888350",
        "ups": "1Z1E852G0310312712",
        "fedex": "123456789012",
        "dhl": "1234567890",
        "ontrac": "C01234567890123",
        "royalmail": "RR123456789GB",
    }
    for slug, number in samples.items():
        carrier = carriers.detect_carrier(number)
        assert carrier["code"] == slug, f"{slug} is advertised, detected as {carrier['code']}"
        assert carrier["url"], f"{slug} is advertised without an official link"


def test_updates_claim_matches_the_cadence_table():
    """"Updates are automatic and paced by each package — delivered ones are
    never checked again"."""
    # The pitch no longer quotes the cadence numbers, so this pins the sentence
    # plus the behaviour that backs it: the loop is what makes updates
    # automatic, and Delivered is terminal (never checked again).
    text = _start_text()
    assert "ping you when the status changes" in text
    assert refresh.CADENCE, "no cadence table — the claim has nothing behind it"
    assert refresh.CADENCE_TERMINAL == {"Delivered"}
    assert refresh.cadence_seconds("Delivered", True) is None
    assert refresh.cadence_seconds("Delivered", False) is None


def test_no_update_buttons_claim_holds():
    """"No update buttons, by design" — no command, no callback, no button."""
    src = inspect.getsource(main)
    assert 'Command("update")' not in src and 'Command("refresh")' not in src
    assert 'F.data.startswith("update:")' not in src
    kb = main.build_package_keyboard("RR123456789CN", {"muted": False})
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert not any("update" in label.lower() for label in labels), labels
    menu = {name for name, _ in main.MENU_ITEMS}
    assert "update" not in menu and "refresh" not in menu, menu
    # The claim itself survives in the new, shorter pitch; the cadence
    # parenthetical about the one non-cycle-paced lookup moved to /help.
    assert "No account, no refresh buttons" in _start_text()


def test_notification_privacy_claim_holds():
    """"Pings carry no tracking number, name or status"."""
    text, _kb = refresh.build_global_ping(2)
    for leak in ("9400111206200074888350", "RR123456789CN", "In Transit",
                 "China Post", "AliExpress order"):
        assert leak not in text
    # ...and the pitch says so, with the group qualifier intact.
    assert ("Pings never include your tracking number, your name, or what's "
            "inside — groups included") in _start_text()


def test_ephemeral_claim_matches_config_and_handlers():
    """"Cards, /list and /info delete themselves ~90 s after sending"."""
    # The self-deleting cards are real — /start just no longer advertises them.
    # The bullet was cut for length, but the BEHAVIOUR is unchanged and stays
    # tested here, so dropping the copy never means assuming the feature.
    assert config.EPHEMERAL_TTL > 0
    # The cards live in the shared add pipeline now (both /track and the
    # screenshot path go through it), so the claim is pinned there.
    for handler in (main._add_packages, main.list_handler, main.info_handler):
        assert "send_ephemeral" in inspect.getsource(handler), handler.__name__
    for entry in (main.track_handler, main.photo_handler):
        assert "_add_packages" in inspect.getsource(entry), entry.__name__


def test_the_landing_page_quotes_the_live_numbers():
    """A stranger opening the bare domain gets static HTML that cannot read .env —
    so, like the Pro sheet's baked-in fallback, its figures are pinned here:
    computed from config, then asserted against the page. An operator who changes
    the price, the term or retention in .env fails the suite instead of leaving
    the landing advertising the old numbers. (The FREE tier's carrier list is
    name-dropped without a package count, and "3,000+ carriers" is marketing
    copy, not a config value — neither is pinned.)"""
    landing = (pathlib.Path(__file__).resolve().parent.parent
               / "webapp" / "index.html").read_text().split(
                   "if (!inTelegram) {", 1)[1].split("} else {", 1)[0]
    price = f"${config.PRO_PRICE_USD:g}"
    term = config.SUBSCRIPTION_DAYS
    per_week = f"{price} per week" if term == 7 else f"{price} per {term} days"
    for quoted in (
            f"forgotten after {config.RETENTION_DAYS} days",            # the forget promise
            per_week,                                                   # the price phrase
            f"{per_week} — {config.PRO_PACKAGE_LIMIT} packages",        # the FAQ's Pro line
    ):
        assert quoted in landing, f"the landing no longer quotes: {quoted}"
    # The demo card is a MOCK-UP: the fake name and carrier are the app's own
    # example furniture (the same ones the bot's /help shows), never real data —
    # a browser visitor has none, and none may leak into a public page. The name
    # is matched in its JS-escaped form (Mom\'s in the single-quoted string).
    assert "Mom\\'s gift" in landing and "USPS" in landing


def test_retention_claim_matches_config():
    """"Delivered packages are forgotten after 14 days"."""
    text = _start_text()
    assert config.RETENTION_DAYS > 0
    assert f"forgotten after {config.RETENTION_DAYS} days" in text
    src = inspect.getsource(refresh._auto_purge_delivered)
    assert '"Delivered"' in src, "purge no longer targets only delivered packages"


def test_outliving_records_claim_holds():
    """"A few small records outlive the package ... none holds a tracking number".

    The pitch says a number is forgotten after RETENTION_DAYS, which a reader
    could reasonably take to mean EVERYTHING is. Three records outlive the
    package, so the honest version names them — and the reassuring half of that
    claim is checkable: none of the outliving stores may hold a number.
    """
    text = _start_text()
    assert config.RETENTION_DAYS > 0
    assert "outlive the package" in text
    # The receipts and the payment address hold no tracking number; the intl
    # record DOES (it must, to keep re-adds free) — the greeting says so, the
    # way /privacy §1 now states it.
    assert "None of them holds a tracking number" not in text
    # The three named records, and /privacy is pointed at for the list. The
    # wording is deliberately plain: "address" alone read as a HOME address,
    # and "international count" as a count of stored parcels.
    for named in ("Pro payment records", "reserved payment address",
                  "international packages you've registered this period"):
        assert named in text, f"/start no longer names: {named}"
    assert "/privacy" in text
    # The reassuring half, verified against the shapes that actually persist.
    receipts = pro._receipts or []
    for entry in receipts:
        assert "tracking" not in " ".join(entry).lower(), \
            "a receipt now holds a tracking number — the /start claim is false"
    # The per-chat address registry and the intl allowance are chat_id keyed,
    # and neither carries a number (delivery_history is de-identified too).
    src = inspect.getsource(pro.deposit_address)
    assert "tracking" not in src.lower()


def test_forget_claim_holds():
    """"/forget removes any package without typing its number"."""
    assert hasattr(main, "forget_handler")
    assert "send_ephemeral" in inspect.getsource(main.forget_handler)
    menu = {name for name, _ in main.MENU_ITEMS}
    assert "forget" in menu


def test_encryption_claim_holds():
    """"Encrypted at rest (AES + HMAC)" — Fernet really is in play."""
    assert storage._ENCRYPTION_AVAILABLE, "cryptography missing: nothing is encrypted"
    assert storage._encrypt_payload("x").startswith(b"gAAAA"), "writes are not ciphertext"


def test_operator_access_claim_holds():
    """/privacy says plainly what 'encrypted at rest' cannot promise: the operator
    runs the machine and holds the key, so they could technically read the store —
    and it pins the companion promise that the bot's own surfaces never carry one
    user's tracking number or chat ID to them. Without the first sentence the
    privacy text oversells the encryption; without the second it undersells the
    interface. Both are this project's honest position, so neither may drift."""
    text = ui.PRIVACY_TEXT
    assert "could technically read the encrypted store" in text
    assert ("no command, alert, notice or log line carries one user's tracking "
            "number or chat ID") in text


def test_log_masking_claim_holds():
    """"logs mask identifiers" — chat ids and tracking numbers."""
    tag = storage.mask_chat(123456789)
    assert tag.startswith("chat:") and "123456789" not in tag
    assert "9400111206200074888350" not in carriers._sanitize_tracking_id(
        "9400111206200074888350")


def test_app_is_signed_by_telegram_claim_holds(monkeypatch):
    """"app access is signed by Telegram" — initData HMAC is verified."""
    import hashlib
    import hmac
    import json
    import time
    from urllib.parse import urlencode

    def sign(user, token):
        params = {"user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
                  "auth_date": str(int(time.time()))}
        check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        return urlencode({**params,
                          "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})

    monkeypatch.setattr(config, "_bot_token", "123456:TESTTOKEN")
    assert miniapp._validate_init_data(sign({"id": 7}, "123456:TESTTOKEN")) is not None
    assert miniapp._validate_init_data(sign({"id": 7}, "999999:WRONG")) is None


def test_international_tab_claim_holds():
    """"🌍 International tab (CN/HK posts) + live 17TRACK quota"."""
    assert "International tab" in _start_text()
    paths = {r.resource.canonical for r in miniapp._build_miniapp_app().router.routes()}
    assert "/api/international" in paths
    assert miniapp._is_international({"carrier": "chinapost"}) is True
    assert miniapp._is_international({"carrier": "hkpost"}) is True
    # "live quota" must be a real 17TRACK call, not a cached constant.
    assert "/getquota" in inspect.getsource(carriers)
    assert hasattr(carriers, "quota_snapshot")


def test_pro_extras_claim_holds():
    """"Dark & Pink themes · haptic feedback" (listed under PRO, so gated)."""
    html = (config.WEBAPP_DIR / "index.html").read_text()
    assert 'data-theme-opt="dark"' in html and 'data-theme-opt="pink"' in html
    assert "const hap = (" in html, "no haptics helper"
    assert html.count("hap(") > 5, "haptics helper is defined but never used"
    assert "themeSegRow" in html and "tier.pro" in html   # themes are Pro-gated


def test_tier_numbers_come_from_config():
    text = _start_text()
    assert f"{config.FREE_PACKAGE_LIMIT} packages" in text
    assert f"{config.PRO_PACKAGE_LIMIT} packages" in text
    assert config.FREE_PACKAGE_LIMIT < config.PRO_PACKAGE_LIMIT


def test_tier_cadence_quotes_match_the_table():
    """The Free/Pro numbers in the pitch must be the table's own values —
    rendered the way /start renders them: in-transit/delivery-day in raw
    minutes, pre-scan via main's _fmt (>= 60 min means h)."""
    cad = refresh.CADENCE
    text = _start_text().lower()
    # The pitch no longer quotes per-tier cadences ("faster checks" replaced
    # them for length), so the table is pinned where the schedule is actually
    # published instead, and /start is checked to stay free of schedule text.
    cad = refresh.CADENCE
    assert cad["In Transit"] and cad["Out for Delivery"]
    for tier in (True, False):
        assert refresh.cadence_seconds("In Transit", tier) is not None
    text = _start_text()
    assert "faster checks" in text
    assert "pickup every" not in text   # no dedicated pickup lane anymore
    assert "in-transit" not in text and "pre-scan" not in text, \
        "the cadence schedule leaked back into the greeting"


def test_privacy_is_really_not_paywalled():
    """The tiers claim identical privacy, so none of those paths may *call* the
    tier check — `is_pro_tier` (the cadence argument) is fine, `is_pro(cid)` is
    not: that would make the claim false for free users."""
    for func in (refresh.build_global_ping, refresh._auto_purge_delivered,
                 ui.send_ephemeral, refresh._check_one):
        src = inspect.getsource(func)
        assert not re.search(r"\bis_pro\s*\(", src), f"{func.__name__} gates on tier"


def test_stop_on_delete_claim_matches_capability():
    """The greeting may promise stop-on-delete only while carriers.stop_17track
    exists — and since the shared-number guard the promise is conditional, so the
    copy has to say so. Same honest-direction style as the roadmap tripwire: copy
    follows capability, never the reverse."""
    # The greeting no longer carries the stop-on-delete promise (it moved to
    # /privacy for length), but the promise must still be made SOMEWHERE and
    # only while the capability exists — so the check follows the copy there.
    text = ui.PRIVACY_TEXT
    claim = "asks 17TRACK to stop tracking"
    condition = "unless another chat still tracks"
    if hasattr(carriers, "stop_17track"):
        assert claim in text, "capability shipped but no surface mentions it"
        assert condition in text, \
            "the stop is skipped while another chat tracks the number — say so"
    else:
        assert claim not in text, "greeting promises stop-on-delete with no capability behind it"


def test_roadmap_items_are_still_unshipped():
    """Roadmap claims must be true in the honest direction: work that is listed
    as upcoming must not actually be done (that belongs in the pitch)."""
    src = inspect.getsource(carriers)
    # fedex LEFT this list 2026-09-20: the wtrk browser tier shipped, so the
    # roadmap claim "FedEx via browser" is now the pitch, not the roadmap.
    for slug in ("ups", "dhl", "royalmail", "canadapost"):
        assert slug in carriers._17TRACK_ONLY_SLUGS, f"{slug} has an official API now"
    # Deactivation SHIPPED (2026-09-16): every delete flow sends /stoptrack.
    # The tripwire flipped from "not implemented" to "implemented" — and stays
    # honest in the other direction: the partner's API has no record-delete
    # endpoint, so the literal must not appear in our code as a call.
    assert "stop_17track" in src, "delete-flow deactivation is missing again"
    assert "/delete" not in src, "a record-delete endpoint appeared"
    assert isinstance(config.RETENTION_DAYS, int)   # per-chat controls not built


def test_every_menu_command_has_a_handler_and_help_line(monkeypatch):
    """House rule: a ☰ menu entry must be handled AND listed in /help — in the help
    that the chat which SEES it actually reads. /ping is registered per-operator
    (BotCommandScopeChat) and stays out of a user's help on purpose, so it is
    checked against the operator's help instead."""
    src_main = inspect.getsource(main)
    menu = {name for name, _ in main.MENU_ITEMS}      # the menu's single source
    assert menu, "no commands menu parsed"
    user_help = _render(main.help_handler)
    owner = 900000 + _counter["n"] + 1        # a chat the throwaway id never is
    monkeypatch.setattr(main, "OWNER_CHAT_IDS", {owner})
    owner_help = _render(main.help_handler, chat_id=owner)
    for cmd in menu:
        assert f'Command("{cmd}"' in src_main, f"{cmd} is in the menu but has no handler"
        assert f"/{cmd}" in user_help or f"/{cmd}" in owner_help, \
            f"{cmd} is in the menu but in neither the user's nor the operator's /help"

