"""Pro covers a fixed number of international (17TRACK-only) registrations per
period (config.PRO_INTL_LIMIT).

International posts have no source but 17TRACK, so every one of them spends a
slot of the bot's own shared free quota (200 numbers). That cap is what stops a
single subscriber from eating it — and it must count exactly what the 🌍 tab
shows, so the classification lives in one place (carriers.is_international).
"""
import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import carriers
import config
import miniapp
import pro
import storage

CN = "RR123456789CN"
CN2 = "RB987654321CN"
HK = "EE123456789HK"
USPS = "9400111206200074888350"
# Enough distinct international numbers to exhaust the allowance whatever
# config.PRO_INTL_LIMIT is set to.
INTS = (CN, CN2, HK, "RR333333333CN", "RR444444444CN", "RR555555555CN")


def _intl(tid=CN):
    return {"name": "Box", "status": "In Transit", "carrier": "chinapost",
            "tracking_id": tid}


def _pro(chat_id, days=7):
    pro._pro_users[chat_id] = time.time() + days * 86400


def _cleanup(*chat_ids):
    for chat_id in chat_ids:
        storage.track_sessions.pop(chat_id, None)
        pro._pro_users.pop(chat_id, None)
        pro._intl_registrations.pop(chat_id, None)


class TestIntlLimit:
    def test_three_international_packages_are_allowed(self):
        chat = 4101
        try:
            _pro(chat)
            storage.track_sessions[chat] = {CN: _intl(), CN2: _intl(CN2)}
            ok, err = pro.check_intl_limit(chat, [HK])
            assert ok and err == ""
        finally:
            _cleanup(chat)

    def test_one_past_the_allowance_is_blocked_with_the_allowance_message(self):
        chat = 4102
        try:
            _pro(chat)
            for tid in INTS[:config.PRO_INTL_LIMIT]:
                pro.record_intl_registration(chat, tid)   # the whole allowance spent
            ok, err = pro.check_intl_limit(chat, ["RR222222222CN"])
            assert not ok
            allowance = f"{config.PRO_INTL_LIMIT}/{config.PRO_INTL_LIMIT}"
            assert allowance in err, err
            assert "17TRACK quota" in err            # explains *why*
            assert "registrations" in err and "renews" in err   # the period rule
            assert "Remove one" not in err, "removal must not promise a free slot"
            # No upsell: this user is already paying.
            assert "Upgrade to Pro" not in err
        finally:
            _cleanup(chat)

    def test_multi_add_cannot_sneak_past_the_cap(self):
        """A batch of CN numbers in one /track must not slip through one at a time."""
        chat = 4103
        try:
            _pro(chat)
            storage.track_sessions[chat] = {CN: _intl()}
            for tid in INTS[:config.PRO_INTL_LIMIT - 1]:
                pro.record_intl_registration(chat, tid)   # allowance nearly spent
            # Numbers already counted this period charge nothing on re-add, so the
            # batch must be two FRESH ones — each would fit alone, together they
            # must not.
            ok, _err = pro.check_intl_limit(
                chat, list(INTS[config.PRO_INTL_LIMIT - 1:config.PRO_INTL_LIMIT + 1]))
            assert not ok, "the whole batch must be judged before any lookup"
        finally:
            _cleanup(chat)

    def test_non_international_adds_are_unaffected(self):
        chat = 4104
        try:
            _pro(chat)
            storage.track_sessions[chat] = {CN: _intl(), CN2: _intl(CN2), HK: _intl(HK)}
            ok, err = pro.check_intl_limit(chat, [USPS, "1Z999AA10123456784"])
            assert ok and err == "", "the cap is about international posts only"
        finally:
            _cleanup(chat)

    def test_the_counter_charges_exactly_what_the_gate_can_recognise(self):
        """The counter and the gate use the same format classification, so the
        two can never disagree: a number the gate treats as international is
        charged, one it cannot classify is neither charged nor blocked."""
        chat = 4105
        try:
            _pro(chat)
            assert carriers.is_international_number("RR444444444CN") is True
            pro.record_intl_registration(chat, "RR444444444CN")
            assert pro.count_international(chat) == 1
            # A domestic number spends no 17TRACK slot, so it is not counted.
            pro.record_intl_registration(chat, "1Z999AA10123456784")
            assert pro.count_international(chat) == 1
            # With the allowance fully used, the SAME number must still be
            # re-registerable — removing and re-adding must not be a dead end
            # even when nothing else fits. (At allowance 1 this is the
            # load-bearing case; at 5 it was simply one of several.)
            ok, err = pro.check_intl_limit(chat, ["RR444444444CN"])
            assert ok and err == "", \
                "re-adding a number that was already counted stays allowed"
        finally:
            _cleanup(chat)

    def test_free_users_get_no_international_allowance(self):
        """International is Pro-only again: a free chat's FIRST international
        number is refused outright with the upsell. There is no monthly slot
        to spend (FREE_INTL_LIMIT = 0) and nothing is recorded, so there is
        no re-add exemption to honor either."""
        chat = 4106
        try:
            storage.track_sessions[chat] = {}
            assert not pro.is_pro(chat)
            ok, err = pro.check_intl_limit(chat, [CN])
            assert not ok
            assert "Pro feature" in err and "Upgrade to Pro" in err, err
            assert pro.count_international(chat) == 0, \
                "a refused add must not consume anything"
        finally:
            _cleanup(chat)

    def test_re_adding_a_counted_number_does_not_eat_the_count(self):
        """The tally's `numbers` list is the "already paid" record: the gate
        exempts a re-add of a counted number, so the re-registration's record
        call must not increment the count a second time — otherwise every
        remove → re-add cycle would spend a fresh 17TRACK slot."""
        chat = 4109
        try:
            _pro(chat)
            pro.record_intl_registration(chat, CN)
            assert pro.count_international(chat) == 1
            pro.record_intl_registration(chat, CN)      # the remove → re-add
            assert pro.count_international(chat) == 1, \
                "a number this period already counted charges only once"
            pro.record_intl_registration(chat, CN2)     # a distinct number still counts
            assert pro.count_international(chat) == 2
        finally:
            _cleanup(chat)

    def test_owner_chats_are_exempt(self, monkeypatch):
        chat = 4107
        try:
            monkeypatch.setattr(config, "OWNER_CHAT_IDS", {chat})
            storage.track_sessions[chat] = {CN: _intl(), CN2: _intl(CN2), HK: _intl(HK)}
            ok, err = pro.check_intl_limit(chat, ["RR666666666CN"])
            assert ok and err == ""
        finally:
            _cleanup(chat)

    def test_expired_subscription_drops_to_the_free_allowance(self):
        """The allowance is tied to the tier: a lapsed subscriber no longer gets
        the Pro allowance — and with the free tier retired to zero, their next
        international add is refused outright, like any free chat's."""
        chat = 4108
        try:
            storage.track_sessions[chat] = {CN: _intl(), CN2: _intl(CN2), HK: _intl(HK)}
            pro._pro_users[chat] = time.time() - 60        # lapsed
            assert not pro.is_pro(chat)
            ok, err = pro.check_intl_limit(chat, ["RR777777777CN"])
            assert not ok, "a lapsed Pro has no intl allowance left"
            assert "Pro feature" in err and "Upgrade to Pro" in err, err
        finally:
            _cleanup(chat)


class TestClassificationIsShared:
    """One rule, two consumers: the 🌍 tab and the allowance."""

    def test_table(self):
        assert carriers.is_international({"carrier": "chinapost"}) is True
        assert carriers.is_international({"carrier": "hkpost"}) is True
        assert carriers.is_international({"carrier": "intl"}) is True
        assert carriers.is_international(
            {"carrier": "unknown", "carrier_country": "CN"}) is True
        assert carriers.is_international({"carrier_country": "hk"}) is True
        assert carriers.is_international(
            {"carrier": "usps", "carrier_country": "US"}) is False
        assert carriers.is_international({"carrier": "ups"}) is False
        assert carriers.is_international({}) is False

    def test_the_mini_app_view_and_the_allowance_agree(self):
        samples = [{"carrier": "chinapost"}, {"carrier": "ups"},
                   {"carrier": "unknown", "carrier_country": "CN"},
                   {"carrier": "intl"}, {}]
        for info in samples:
            assert miniapp._is_international(info) == carriers.is_international(info)

    def test_format_classification_before_the_first_lookup(self):
        assert carriers.is_international_number(CN) is True
        assert carriers.is_international_number(HK) is True
        assert carriers.is_international_number("RR123456789FR") is True  # other post
        assert carriers.is_international_number(USPS) is False
        assert carriers.is_international_number("1Z999AA10123456784") is False


class TestMiniAppAddPath:
    class _Req:
        def __init__(self, headers, body):
            self.headers = headers
            self._body = body

        async def json(self):
            return self._body

    def _auth(self, monkeypatch, user_id):
        monkeypatch.setattr(config, "_bot_token", "123456:TESTTOKEN")
        params = {"user": json.dumps({"id": user_id}, separators=(",", ":")),
                  "auth_date": str(int(time.time()))}
        check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest()
        return {"Authorization": "tma " + urlencode(
            {**params, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})}

    def test_one_past_the_allowance_tells_the_user_why(self, monkeypatch):
        chat = 4201
        try:
            _pro(chat)
            storage.track_sessions[chat] = {CN: _intl(), CN2: _intl(CN2), HK: _intl(HK)}
            for tid in INTS[:config.PRO_INTL_LIMIT]:
                pro.record_intl_registration(chat, tid)   # the whole allowance spent
            resp = asyncio.run(miniapp._api_add(
                self._Req(self._auth(monkeypatch, chat),
                          {"tracking_id": "RR888888888CN", "name": "Box"})))
            body = json.loads(resp.body)
            assert resp.status == 403
            assert body.get("intl_limit") is True
            # Must NOT look like the upgrade nudge (this user is already Pro).
            assert not body.get("limit_reached")
            assert body.get("intl_total") == config.PRO_INTL_LIMIT
            assert body.get("intl_used") == config.PRO_INTL_LIMIT
            assert "17TRACK quota" in body["error"]
        finally:
            _cleanup(chat)

    def test_a_free_caller_gets_their_own_limit_in_the_403(self, monkeypatch):
        """The 403 carries the CALLER's total — for a free chat that is 0.
        The pre-existing tally here is a transitional state (a chat that
        registered while the free allowance existed); it must read as
        "used 1 out of 0" with the Pro upsell, not Pro's numbers."""
        chat = 4203
        try:
            storage.track_sessions[chat] = {}
            assert not pro.is_pro(chat)
            pro.record_intl_registration(chat, CN)    # the free month is spent
            resp = asyncio.run(miniapp._api_add(
                self._Req(self._auth(monkeypatch, chat),
                          {"tracking_id": CN2, "name": "Box"})))
            body = json.loads(resp.body)
            assert resp.status == 403
            assert body.get("intl_limit") is True
            assert body.get("pro") is False
            assert body.get("intl_total") == config.FREE_INTL_LIMIT
            assert body.get("intl_used") == 1
            assert "Upgrade to Pro" in body["error"]
        finally:
            _cleanup(chat)

    def test_a_usps_number_still_adds_past_the_international_cap(self, monkeypatch):
        """The cap must not touch non-international numbers: the add gets through
        both limit checks (the lookup itself is stubbed — no network in tests)."""
        import refresh
        chat = 4202
        saved = []
        async def fake_register(tracking_id, is_pro=None, chat_id=None):
            return ({"name": "Box", "status": "Pending", "carrier": "usps"}, "ok")
        monkeypatch.setattr(refresh, "_register_and_fetch", fake_register)
        monkeypatch.setattr(refresh, "save_now", lambda: saved.append(True))
        try:
            _pro(chat)
            storage.track_sessions[chat] = {CN: _intl(), CN2: _intl(CN2), HK: _intl(HK)}
            resp = asyncio.run(miniapp._api_add(
                self._Req(self._auth(monkeypatch, chat),
                          {"tracking_id": USPS, "name": "Box"})))
            body = json.loads(resp.body)
            assert resp.status == 200, body
            assert body["package"]["tracking_id"] == USPS
            assert USPS in storage.track_sessions[chat]
        finally:
            _cleanup(chat)


class TestBotTrackPath:
    """The bot's /track must refuse the 4th international number too."""

    class _Msg:
        def __init__(self, chat_id, text):
            self.texts = []
            self.text = text
            self.message_id = 1
            self.chat = type("C", (), {"id": chat_id, "type": "private"})()
            self.from_user = type("U", (), {"id": 5, "first_name": "F", "username": "u"})()

        async def answer(self, text, **kw):
            self.texts.append(text)
            return type("S", (), {"chat": self.chat, "message_id": 1})()

        async def delete(self):
            pass

    def test_one_past_the_allowance_is_refused_before_any_lookup(self):
        import main
        chat = 4501
        try:
            _pro(chat)
            storage.track_sessions[chat] = {CN: _intl(), CN2: _intl(CN2), HK: _intl(HK)}
            for tid in INTS[:config.PRO_INTL_LIMIT]:
                pro.record_intl_registration(chat, tid)   # the whole allowance spent
            msg = self._Msg(chat, "/track RR123456700CN")
            asyncio.run(main.track_handler(msg))
            assert msg.texts, "the handler must answer"
            reply = msg.texts[0]
            assert "International limit reached" in reply
            assert f"{config.PRO_INTL_LIMIT}/{config.PRO_INTL_LIMIT}" in reply
            # Nothing was added and no lookup was started.
            assert list(storage.track_sessions[chat]) == [CN, CN2, HK]
            assert not any("Looking up" in t for t in msg.texts)
        finally:
            _cleanup(chat)

    def test_the_third_one_still_goes_through_the_check(self, monkeypatch, tmp_path):
        """With room left, the handler proceeds past the allowance check (the
        registration itself is stubbed — no network in tests).

        NOTE: track_handler ends with save_data(), so this test writes a store.
        tests/conftest.py points main.DATA_FILE at tmp_path for every test —
        without that, this test would overwrite the LIVE track_data.json.
        """
        import main
        import refresh
        chat = 4502
        async def fake_register(tracking_id, is_pro=None, chat_id=None):
            return ({"name": "Box", "status": "Pending", "carrier": "chinapost"}, "ok")
        monkeypatch.setattr(refresh, "_register_and_fetch", fake_register)
        try:
            _pro(chat)
            storage.track_sessions[chat] = {CN: _intl(), CN2: _intl(CN2)}
            msg = self._Msg(chat, "/track RR123456700CN")
            asyncio.run(main.track_handler(msg))
            assert any("Looking up" in t for t in msg.texts), msg.texts
            assert "RR123456700CN" in storage.track_sessions[chat]
            assert main.DATA_FILE.parent == tmp_path, "the live store must stay untouched"
        finally:
            _cleanup(chat)


class TestAllowanceCopy:
    """What the user is told must match what the bot enforces."""

    def _html(self):
        import pathlib
        return (pathlib.Path(__file__).resolve().parent.parent
                / "webapp" / "index.html").read_text()

    def test_pro_sheet_counts_from_the_same_number(self):
        html = self._html()
        # The feature stays discoverable (pinned by test_miniapp_filters too)…
        assert "International posts + 17TRACK quota view" in html
        # …and the allowance is stated twice, both mirroring config.
        # The baked-in fallback must mirror config, and the server resends the live
        # value in tier_facts.intl_included on every load, so a change to the
        # allowance reaches the sheet by itself.
        assert f"let INTL_TOTAL = {config.PRO_INTL_LIMIT};" in html, \
            "the app's allowance fallback drifted from config.PRO_INTL_LIMIT"
        assert (f'<em><span id="factIntl">{config.PRO_INTL_LIMIT}</span> included</em>'
                in html), "the sheet states Pro's intl allowance, with no free tier"
        # The 🌍 view states the allowance per chat — with the plural that
        # follows the number ("1 international package", "5 … packages") — and
        # quotes the registration tally ("N registered this period"), the same
        # number the add gate spends, not the stored-package count.
        assert "${INTL_TOTAL} international " in html, \
            "the 🌍 view must state the allowance per chat"
        assert "package${INTL_TOTAL === 1 ? '' : 's'}" in html
        assert "registered this period." in html, \
            "the 🌍 view must quote the registration tally, not the stored count"
        assert "intlState.intl_used" in html, \
            "the 🌍 view must read intl_used from the server payload"
        # The tab is Pro/owner-only and self-sufficient: the chip is hidden
        # entirely for non-pro (the server 403 is the real gate), and the 🌍
        # view carries its own add button (same /api/add, intl-hinted sheet).
        assert "chip.hidden = !tier.pro" in html, \
            "the International chip must be hidden, not locked, for non-pro"
        assert "Add international package" in html and "openAddSheet(true)" in html, \
            "the 🌍 view must have its own add button"

    def test_start_pitch_counts_from_config_not_a_hardcoded_number(self):
        import inspect
        import main
        body = inspect.getsource(main.start_handler)
        assert "PRO_INTL_LIMIT" in body, "the pitch must not hardcode the allowance"
        assert "International tab" in body

    def test_activation_message_states_the_allowance(self, monkeypatch, tmp_path):
        import botstate
        import config as cfg
        sent = []

        class _Bot:
            async def send_message(self, chat_id, text, **kw):
                sent.append(text)
                return type("M", (), {"message_id": 1})()

        monkeypatch.setattr(botstate, "bot", _Bot())
        monkeypatch.chdir(tmp_path)                    # receipts log stays in tmp
        chat = 4301
        try:
            _pro(chat)
            asyncio.run(pro._activate_pro(
                chat, 0.011234, cfg.USDC_MINT,
                "5sIgNatUrEsIgNatUrEsIgNatUrEsIgNatUrEsIgNatUrE", "pid"))
            assert sent, "no activation message"
            plural = "" if config.PRO_INTL_LIMIT == 1 else "s"
            assert (f"{config.PRO_INTL_LIMIT} international package{plural} included"
                    in sent[0])
        finally:
            _cleanup(chat)

    def test_the_paid_user_is_never_upsold_in_the_intl_message(self):
        chat = 4302
        try:
            _pro(chat, days=1)
            storage.track_sessions[chat] = {CN: _intl(), CN2: _intl(CN2), HK: _intl(HK)}
            for tid in INTS[:config.PRO_INTL_LIMIT]:
                pro.record_intl_registration(chat, tid)   # the whole allowance spent
            _ok, err = pro.check_intl_limit(chat, ["RR999999999CN"])
            assert "Upgrade" not in err and "Pro is" not in err
            assert "🌍" in err        # still recognisably the international rule
        finally:
            _cleanup(chat)


class TestPrivacySheetFollowsTheBot:
    """The Mini App must show the bot's own /privacy text, not a paraphrase."""

    def _html(self):
        import pathlib
        return (pathlib.Path(__file__).resolve().parent.parent
                / "webapp" / "index.html").read_text()

    def test_sheet_fetches_the_shared_policy_and_keeps_a_fallback(self):
        html = self._html()
        assert "/api/privacy" in html, "the sheet must fetch the bot's policy"
        assert "loadPrivacy()" in html
        assert "keep the local copy" in html, "a failed fetch must not blank the sheet"
        # The offline fallback still states the chat-side promises.
        assert "delete themselves" in html
        assert "Solana blockchain" in html

    def test_authenticated_sheet_text_is_byte_identical_to_the_command(
            self, monkeypatch):
        import ui
        chat = 4401
        try:
            storage.track_sessions[chat] = {}
            auth = TestMiniAppAddPath()._auth(monkeypatch, chat)
            resp = asyncio.run(miniapp._api_privacy(
                TestMiniAppAddPath._Req(auth, {})))
            assert resp.status == 200
            assert json.loads(resp.body)["html"] == ui.PRIVACY_TEXT
        finally:
            _cleanup(chat)


class TestAllowanceCountsRegistrations:
    """The Pro allowance is N 17TRACK registrations per billing period, not N
    concurrently stored cards.

    The quota slot is spent at REGISTER time, so counting stored cards let a
    member add, remove and re-add the same package forever without ever reaching
    the cap — each round trip spending a fresh slot of the bot's shared 17TRACK
    quota. Removing a package therefore does not give the slot back; the period
    (the subscription window ending at the member's expiry) is what resets it.
    """

    def test_five_registrations_then_a_sixth_is_refused(self):
        chat = 4701
        try:
            _pro(chat)
            for i in range(5):
                pro.record_intl_registration(chat, f"RR12345678{i}CN")
            assert pro.count_international(chat) == 5
            ok, err = pro.check_intl_limit(chat, ["RR999999999CN"])
            assert not ok, "the 6th registration in the period must be refused"
            assert f"5/{config.PRO_INTL_LIMIT}" in err, err
            assert pro.count_international(chat) == 5, "a refusal must not count"
        finally:
            _cleanup(chat)

    def test_removing_a_package_does_not_restore_the_allowance(self):
        """Churn must not refill the cap: the slot was spent when the number was
        registered, and dropping the card does not un-spend it."""
        chat = 4702
        try:
            _pro(chat)
            storage.track_sessions[chat] = {CN: _intl(), CN2: _intl(CN2), HK: _intl(HK)}
            for tid in (CN, CN2, HK):
                pro.record_intl_registration(chat, tid)
            del storage.track_sessions[chat][HK]        # the user removes one
            assert pro.count_international(chat) == 3, "removal must not decrement"
            for tid in ("RR444444444CN", "RR555555555CN"):
                pro.record_intl_registration(chat, tid)
            ok, _err = pro.check_intl_limit(chat, ["RR666666666CN"])
            assert not ok, "a re-add counts as a fresh registration"
        finally:
            _cleanup(chat)

    def test_a_new_period_starts_the_allowance_over(self):
        chat = 4703
        try:
            _pro(chat)
            for i in range(3):
                pro.record_intl_registration(chat, f"RR12345678{i}CN")
            assert pro.count_international(chat) == 3
            pro._pro_users[chat] += config.SUBSCRIPTION_DAYS * 86400   # renewed
            assert pro.count_international(chat) == 0, "a new period starts empty"
            assert pro.check_intl_limit(chat, ["RR999999999CN"])[0] is True
        finally:
            _cleanup(chat)

    def test_the_counter_survives_a_restart_encrypted(self):
        chat = 4704
        try:
            _pro(chat)
            pro.record_intl_registration(chat, "RR123456789CN")
            raw = config.INTL_REGISTRATIONS_FILE.read_bytes()
            assert raw.startswith(b"gAAAA"), "the counter must be encrypted at rest"
            assert b'"count"' not in raw and str(chat).encode() not in raw, \
                "no plaintext member id or count"
            pro._intl_registrations.clear()               # a restart
            pro.load_intl_registrations()
            assert pro.count_international(chat) == 1, "the period count must survive"
        finally:
            _cleanup(chat)

    def test_only_a_successful_registration_is_counted(self, monkeypatch):
        """The increment sits on the registration event, not on the add: an
        'already registered' number spent its slot earlier, and a free chat's
        number is never registered at all."""
        import refresh
        chat = 4705

        async def _lookup(number, allow_browser=False, is_pro=None):
            return ("In Transit", carriers.detect_carrier(number), "17TRACK", "detail")

        async def _registered(number):
            return "registered"

        async def _already(number):
            return "already"

        monkeypatch.setattr(carriers, "fetch_track_status", _lookup)
        try:
            _pro(chat)
            monkeypatch.setattr(carriers, "register_17track", _registered)
            asyncio.run(refresh._register_and_fetch(
                "RR123456789CN", is_pro=True, chat_id=chat))
            assert pro.count_international(chat) == 1

            # 17TRACK already had it: no new slot was spent.
            monkeypatch.setattr(carriers, "register_17track", _already)
            asyncio.run(refresh._register_and_fetch(
                "RR123456789CN", is_pro=True, chat_id=chat))
            assert pro.count_international(chat) == 1

            # A free chat's number is never registered with 17TRACK, but the ADD
            # itself is tallied against the free monthly allowance.
            asyncio.run(refresh._register_and_fetch(
                "RR987654321CN", is_pro=False, chat_id=chat))
            assert pro.count_international(chat) == 2
        finally:
            _cleanup(chat)

