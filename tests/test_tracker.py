import asyncio
import json
import re
import time

import carriers
import config
import main
import pro
import refresh
import storage


class TestCarrierDetection:
    def test_ups(self):
        assert main.detect_carrier("1Z1E852G0310312712")["code"] == "ups"

    def test_usps(self):
        assert main.detect_carrier("9400111206200074888350")["code"] == "usps"
        assert main.detect_carrier("9300111043900023959782")["code"] == "usps"

    def test_fedex(self):
        assert main.detect_carrier("123456789012")["code"] == "fedex"

    def test_dhl(self):
        assert main.detect_carrier("1234567890")["code"] == "dhl"

    def test_ontrac(self):
        assert main.detect_carrier("C01234567890123")["code"] == "ontrac"

    def test_unknown(self):
        assert main.detect_carrier("Mom gift")["code"] == "unknown"


class TestStatusMapping:
    def test_map_17track(self):
        assert main._map_17track_status("Delivered") == "Delivered"
        assert main._map_17track_status("InTransit") == "In Transit"
        assert main._map_17track_status("DeliveryFailure") == "Delivery Attempted"
        # Unknown/renamed codes must NOT be laundered into a positive status:
        # "Info Received" claims the carrier already has the package, while
        # Pending is the neutral pre-scan state the blocked path uses.
        assert main._map_17track_status("WeirdThing") == "Pending"
        assert main._map_17track_status("NotFound") == "Pending"
        # Real 17TRACK codes that used to fall through to that default:
        assert main._map_17track_status("Pickup") == "Available for Pickup"
        assert main._map_17track_status("Undelivered") == "Delivery Attempted"

    def test_extract_status_nested(self):
        item = {"track_info": {"latest_status": {"status": "InTransit"}}}
        assert main._extract_status(item) == "InTransit"


class TestParseTrackArgs:
    def test_single_with_name(self):
        ids, name = main.parse_track_args("/track 9300111043900023959782 Mom gift")
        assert ids == ["9300111043900023959782"]
        assert name == "Mom gift"

    def test_multi(self):
        ids, name = main.parse_track_args(
            "/track 9300111043900023959782 1Z1E852G0310312712")
        assert ids == ["9300111043900023959782", "1Z1E852G0310312712"]
        assert name == ""

    def test_name_tokens_not_ids(self):
        _, name = main.parse_track_args("/track 9300111043900023959782 Mike's order!!")
        assert name == "Mike's order!!"

    def test_no_ids(self):
        ids, _ = main.parse_track_args("/track hello there")
        assert ids == []

    def test_quoted_name_with_spaces(self):
        ids, name = main.parse_track_args(
            '/track 9300111043900023959782 "Mom gift"')
        assert ids == ["9300111043900023959782"]
        assert name == "Mom gift"

    def test_quoted_name_with_digits_stays_name(self):
        # A digit-only word would be a tracking number unquoted; quoted it
        # is a name. (9300111043900023959782 = USPS, 123456789012 = FedEx.)
        ids, name = main.parse_track_args(
            '/track 9300111043900023959782 "123456789012"')
        assert ids == ["9300111043900023959782"]
        assert name == "123456789012"

    def test_quoted_number_with_spaces(self):
        ids, name = main.parse_track_args(
            '/track "9300 1110 4390 0023 9597 82" "Mom gift"')
        assert ids == ["9300111043900023959782"]
        assert name == "Mom gift"

    def test_curly_quotes_from_ios(self):
        ids, name = main.parse_track_args(
            "/track \u201c9300111043900023959782\u201d \u201cMom gift\u201d")
        assert ids == ["9300111043900023959782"]
        assert name == "Mom gift"

    def test_name_before_number(self):
        ids, name = main.parse_track_args(
            '/track "Mom gift" 9300111043900023959782')
        assert ids == ["9300111043900023959782"]
        assert name == "Mom gift"

    def test_spaced_number_from_ocr_joins(self):
        """A label prints "1Z 1E8 52G 03 2501 5793": OCR hands us six fragments,
        none of them tracking-like alone, and the join pass must recognize the
        number they spell."""
        ids, _name = main.parse_track_args(
            "TRACKING #: 1Z 1E8 52G 03 2501 5793", has_command=False)
        assert ids == ["1Z1E852G0325015793"]

    def test_spaced_number_with_the_command(self):
        ids, name = main.parse_track_args("/track 1Z 1E8 52G 03 2501 5793")
        assert ids == ["1Z1E852G0325015793"]
        assert name == ""

    def test_joined_digits_are_not_part_of_the_name(self):
        ids, name = main.parse_track_args(
            "/track 9400 1112 0620 0074 8883 50 Mom gift")
        assert ids == ["9400111206200074888350"]
        assert name == "Mom gift"

    def test_short_fragments_never_join_into_a_number(self):
        """MA / 019 / 6-66 spell nothing that clears the gate."""
        ids, name = main.parse_track_args("MA 019 6-66", has_command=False)
        assert ids == []
        assert name == "MA 019 6-66"

    def test_quoted_tokens_are_never_joined(self):
        """"1Z" "1E8" quoted is literal text, not a number."""
        ids, name = main.parse_track_args(
            '/track "1Z" "1E8" "52G" "03" "2501" "5793"')
        assert ids == []
        assert name == "1Z 1E8 52G 03 2501 5793"

    def test_ups_prefix_read_as_iz_still_parses(self):
        """The live failure: OCR reads the printed 1 as I, so the joined
        candidate is "IZ1E8…" and the 1Z gate rejects it. The join then falls
        through to a shorter window and stores 0325015793 — a silently wrong
        package — instead of the number on the label. One confusable swap
        rescues the whole candidate."""
        ids, name = main.parse_track_args(
            "TRACKING #: IZ 1E8 52G 03 2501 5793", has_command=False)
        assert ids == ["1Z1E852G0325015793"]
        assert name == "TRACKING #:"          # fragments are not a name

    def test_ups_prefix_read_as_iz_in_one_token(self):
        """The same confusion with no spaces at all: the token failed the gate
        and was dropped entirely — the "couldn't find a number" report."""
        ids, name = main.parse_track_args("/track IZ1E852G0325015793 Mom gift")
        assert ids == ["1Z1E852G0325015793"]
        assert name == "Mom gift"

    def test_all_digits_number_with_one_ocr_letter_is_corrected(self):
        """A USPS number whose printed 0 came back as O is one swap away from
        the all-digits form the gate already accepts."""
        ids, _name = main.parse_track_args(
            "94O0111206200074888350", has_command=False)
        assert ids == ["9400111206200074888350"]

    def test_more_than_eight_fragments_still_join(self):
        """A label printed in 2-char groups (nine pieces) exceeded the old
        window cap, so only a short tail window could form."""
        ids, _name = main.parse_track_args(
            "1Z 1E 85 2G 03 25 01 57 93", has_command=False)
        assert ids == ["1Z1E852G0325015793"]

    def test_a_candidate_that_already_validates_is_never_rewritten(self):
        """Substitution runs only on candidates the gate rejected, so an id that
        already parses keeps its exact characters: "1ZIE8…" is a valid 1Z form
        (the pattern allows letters) and must not be second-guessed."""
        ids, _name = main.parse_track_args(
            "1Z IE8 52G 03 2501 5793", has_command=False)
        assert ids == ["1ZIE852G0325015793"]

    def test_a_letter_that_led_to_a_valid_reading_is_left_alone(self):
        """The mirror case: "526" + "03" reads as a valid 1Z number on its own,
        so nothing rewrites it toward "52G". Guessing which of two valid readings
        was printed is how a tracker ends up watching a number nobody shipped."""
        ids, _name = main.parse_track_args(
            "1Z 1E8 526 03 2501 5793", has_command=False)
        assert ids == ["1Z1E85260325015793"]

    def test_correction_never_invents_a_number_from_junk(self):
        """Guard for the whole feature: a single swap, and only when the result
        clears the same gate. Junk that no one swap can turn into a number stays
        a name."""
        for text in ("SPRINGFIELD APT BASEMENT",   # words: many letters left
                     "Order I2345",               # too short after the swap
                     "(555) 123-4567",            # punctuation survives a swap
                     "I",                         # a lone confusable letter
                     "REF O"):
            ids, name = main.parse_track_args(text, has_command=False)
            assert ids == [], f"{text!r} invented {ids}"
            assert name == text

    def test_two_adjacent_numbers_do_not_merge(self):
        """A wider join window must not fuse two numbers on one page: their
        shapes differ, so the joined candidate fails and each parses alone."""
        ids, _name = main.parse_track_args(
            "9400111206200074888350 1Z1E852G0325015793", has_command=False)
        assert ids == ["9400111206200074888350", "1Z1E852G0325015793"]

    def test_separators_inside_a_number_are_not_part_of_it(self):
        """A label prints the number in groups; OCR hands back whatever it thinks
        separates them — a hyphen, a dot, a middle dot, a barcode bar. The 1Z
        pattern needs exactly 18 alphanumerics, so every separator used to make
        the whole number invisible and the photo path answered "couldn't find a
        tracking number"."""
        for text in ("1Z-1E8-52G-03-2501-5793",
                     "1Z.1E8.52G.03.2501.5793",
                     "1Z–1E8·52G|03 2501 5793"):
            ids, _name = main.parse_track_args(text, has_command=False)
            assert ids == ["1Z1E852G0325015793"], text

    def test_separators_do_not_turn_junk_into_a_number(self):
        """Normalising separators is not a loosening: the normalised candidate
        still has to clear the same gate, so lettered junk stays junk."""
        for text in ("SO-4481922", "Order-12345678", "REF|X"):
            ids, name = main.parse_track_args(text, has_command=False)
            assert ids == [], f"{text!r} invented {ids}"
            assert name == text

    def test_a_printed_z_read_as_2_is_still_a_1z_number(self):
        """OCR reads the printed Z as 2. The prefix is a constant of the format,
        so restoring it is not a guess about a character — and without it the
        join fell back to a 13-digit slice of the real number, which the
        all-digits rule accepted as the package ("it didn't scan the whole
        tracking")."""
        for text in ("TRACKING #: 12 1E8 52G 03 2501 5793",
                     "121E852G0325015793",
                     "I2 1E8 52G 03 2501 5793"):
            ids, _name = main.parse_track_args(text, has_command=False)
            assert ids == ["1Z1E852G0325015793"], text

    def test_a_half_read_number_is_not_stored_as_a_package(self):
        """A digit dropped by OCR leaves a 17-char candidate: it has a number's
        shape and fails the gate, so its fragments must be refused instead of
        stored. Refusing is the honest answer — the user gets asked for a tighter
        photo rather than a package that never updates."""
        ids, name = main.parse_track_args(
            "TRACKING #: 1Z 1E8 52G 03 2501 579", has_command=False)
        assert ids == [], "a slice of the number must not become the package"
        assert "1Z" in name, "the unread text stays as the name, as before"

    def test_a_stray_quote_never_hides_a_number_on_a_later_line(self):
        """OCR noise beside a label's logo came back as a quote mark. An
        unclosed span used to swallow every following newline, so the whole
        "TRACKING #:" line collapsed into ONE token and the number never
        reached the gate — the photo path then stored the destination ZIP
        instead. A quote that never closes is a mark, not a quote."""
        for text in ('SHIP TO "J DOE\nTRACKING #: 1Z 1E8 52G 03 2501 5793',
                     'STEP 6 \u201c J DOE\nTRACKING #: 1Z 1E8 52G 03 2501 5793',
                     "' J DOE\nTRACKING #: 1Z 1E8 52G 03 2501 5793"):
            ids, _name = main.parse_track_args(text, has_command=False)
            assert ids == ["1Z1E852G0325015793"], text

    def test_an_unterminated_quote_is_literal_text(self):
        """The typed case: a forgotten closing quote must not become a span that
        eats the rest of the line, and the words still read as a name."""
        ids, name = main.parse_track_args(
            '/track 9300111043900023959782 "Mom gift')
        assert ids == ["9300111043900023959782"]
        assert name == "Mom gift"

    def test_a_complete_number_survives_a_longer_junk_window(self):
        """The join also builds the number plus the next word's first letter
        ("1Z…5793J"): it has a number's shape and fails the gate, and that
        longer candidate used to DELETE the number that was read in full. The
        label photo then stored the destination ZIP instead."""
        ids, _name = main.parse_track_args(
            "| TRACKING #: 1Z 1E8 52G 03 2501 5793 |\nJ BILLING: 3RD PARTY |",
            has_command=False)
        assert ids == ["1Z1E852G0325015793"]

    def test_a_labels_postal_data_is_not_a_package(self):
        """What a label prints around the number: the destination ZIP+4 and a
        contact phone. Both have a number's length once the separators are
        stripped (the ZIP+4 becomes nine bare digits), so the shape has to be
        judged on the RAW token. The parenthesised phone was already refused
        (the parentheses survive cleaning, so it never looks like a number);
        what this rule adds is the dashed and spaced forms, which used to be
        stored — 555-867-5309 as a 10-digit "DHL" package."""
        for text in ("SHIP TO HEMPSTEAD NY 12345-6789",
                     "SHIP TO HEMPSTEAD NY 12345 6789",
                     "call (555) 867-5309",
                     "call 555-867-5309"):
            ids, _name = main.parse_track_args(text, has_command=False)
            assert ids == [], f"{text!r} invented {ids}"
        # The boundary this rule must not cross: with no separator at all the
        # run is left to the gate exactly as before.
        assert main.parse_track_args("123456789", has_command=False)[0] == \
            ["123456789"]

    def test_postal_data_cannot_be_fused_into_a_number(self):
        """A join window that spans a ZIP+4 can only manufacture junk: "NY" +
        "12345-6789" + "UP" cleans to two letters, nine digits and two letters
        — a valid-looking international post number printed nowhere."""
        ids, _name = main.parse_track_args("NY 12345-6789 UP", has_command=False)
        assert ids == [], "a ZIP fused with its neighbours became a package"

    def test_a_legitimate_all_digit_number_is_untouched_by_both_rules(self):
        """Guards: an 18-digit number from another courier must not be rewritten
        into a UPS one, and normal short numbers must still be accepted."""
        assert main.parse_track_args("/track 123456789012345678")[0] == \
            ["123456789012345678"], "all-digits 18: not a 1Z prefix to repair"
        assert main.parse_track_args("/track 1234567890")[0] == ["1234567890"]
        ids, _n = main.parse_track_args(
            "9400111206200074888350", has_command=False)
        assert ids == ["9400111206200074888350"]



class TestPersistenceMigrations:
    def test_legacy_migration(self, tmp_path):
        data = tmp_path / "data.json"
        data.write_text(json.dumps({
            "123": {
                "TID1": {"name": "A", "status": "Unavailable",
                         "status_source": "none", "lookup_ok": False,
                         "diagnostic": "old msg"},
                "TID2": {"name": "B", "status": "In Transit",
                         "status_source": "17TRACK", "lookup_ok": True},
                "TID3": {"name": "C", "status": "Awaiting First Scan"},
            }
        }))
        main.DATA_FILE = data
        main.load_data()
        pkgs = main.track_sessions[123]
        assert "status_source" not in pkgs["TID1"]
        assert "lookup_ok" not in pkgs["TID1"]
        assert "diagnostic" not in pkgs["TID1"]
        assert "source" not in pkgs["TID1"]
        assert pkgs["TID2"]["source"] == "17TRACK"
        assert pkgs["TID3"]["status"] == "Pending"
        assert pkgs["TID3"]["register_blocked"] is True

    def test_cooldown_roundtrip(self, tmp_path):
        main.REG_COOLDOWN_FILE = tmp_path / "cooldown.json"
        main._register_blocked_until.clear()
        main._register_blocked_until["TID1"] = time.time() + 600
        main._register_blocked_until["TID2"] = time.time() - 10  # expired
        main.save_cooldown()
        main._register_blocked_until.clear()
        main.load_cooldown()
        assert "TID1" in main._register_blocked_until
        assert "TID2" not in main._register_blocked_until
        main._register_blocked_until.clear()

    def test_cooldown_undecryptable_does_not_abort_startup(self, tmp_path):
        """A cooldown file encrypted under another key must not abort
        startup: the wrapper preserves it as .locked and starts with empty
        timers instead of raising SystemExit."""
        import base64
        import storage
        from cryptography.fernet import Fernet
        other = Fernet(base64.urlsafe_b64encode(b"\x00" * 32))
        path = tmp_path / "cooldown.json"
        path.write_bytes(other.encrypt(b"{}"))
        main.REG_COOLDOWN_FILE = path
        storage._register_blocked_until.clear()
        main.load_cooldown()  # must not raise
        assert not path.exists()
        assert list(tmp_path.glob("cooldown.json.locked-*")), \
            "the preserve must be timestamped"
        assert storage._register_blocked_until == {}
        storage._register_blocked_until.clear()


class TestFormatting:
    def test_package_card_escapes(self):
        info = {"name": "<script>", "carrier_name": "USPS", "status": "Delivered",
                "source": "17TRACK", "last_updated": None}
        card = main.package_card("TID", info)
        assert "<script>" not in card
        assert "&lt;script&gt;" in card

    def test_package_card_blocked_note(self):
        info = {"name": "X", "carrier_name": "USPS", "status": "Pending",
                "register_blocked": True}
        assert "official" in main.package_card("TID", info).lower()

    def test_package_card_detail_line(self):
        info = {"name": "X", "carrier_name": "USPS", "status": "Info Received",
                "source": "USPS Browser",
                "detail": "USPS Awaiting Item Shipping Label Created TUCSON, AZ 85706 Sep 9, 2026 12:31 PM"}
        card = main.package_card("TID", info)
        assert "Shipping Label Created" in card
        assert "Latest:" in card
        assert "<script>" not in card

    def test_package_card_color_badge(self):
        info = {"name": "X", "carrier_name": "USPS", "status": "Delivered",
                "last_updated": None}
        assert "\U0001f7e2" in main.package_card("TID", info)  # 🟢 green
        info["status"] = "Exception"
        assert "\U0001f534" in main.package_card("TID", info)  # 🔴 red
        info["status"] = "In Transit"
        assert "\U0001f535" in main.package_card("TID", info)  # 🔵 blue

    def test_status_badge_unknown(self):
        assert main.status_badge("Something Weird") == "\u26aa"  # ⚪ gray

    def test_package_card_checked_line(self):
        info = {"name": "X", "carrier_name": "USPS", "status": "Info Received",
                "last_updated": "2026-09-13T11:00:00+00:00",
                "last_checked": "2026-09-13T12:30:00+00:00"}
        card = main.package_card("TID", info)
        assert "Checked:" in card

    def test_trim_sentence_capitalizes_and_dots(self):
        assert main._trim_sentence("  hello world ") == "Hello world."

    def test_trim_sentence_cuts_on_sentence(self):
        out = main._trim_sentence("One two three. " * 40, 60)
        assert out.endswith("One two three.")
        assert len(out) <= 70

    def test_trim_sentence_ellipsis(self):
        assert main._trim_sentence("word " * 40, 50).endswith("\u2026")

    def test_trim_sentence_none(self):
        assert main._trim_sentence(None) == ""

    def test_trim_sentence_strips_promo(self):
        out = main._trim_sentence(
            "in transit to the destination. Get More Out of USPS Tracking: "
            "USPS Tracking Plus® Delivered Out for Delivery")
        assert out == "In transit to the destination."

    def test_sort_delivered_last(self):
        pkgs = {
            "A": {"status": "Delivered", "last_updated": "2026-09-13T00:00:00+00:00"},
            "B": {"status": "In Transit", "last_updated": "2026-09-12T00:00:00+00:00"},
            "C": {"status": "In Transit", "last_updated": "2026-09-13T00:00:00+00:00"},
        }
        order = [tid for tid, _ in main.sort_packages(pkgs)]
        assert order == ["C", "B", "A"]

    def test_format_duration(self):
        assert main._format_duration(90) == "1m"
        assert main._format_duration(3720) == "1h 2m"
        assert main._format_duration(90061) == "1d 1h 1m"

    def test_time_since(self):
        assert main.time_since(None) == "never"
        assert main.time_since("not-a-date") == "unknown"


class TestRateLimiter:
    def test_rate_limited(self):
        async def _run():
            rl = main.RateLimiter(rate=2, per=0.2)
            start = time.monotonic()
            for _ in range(3):
                await rl.acquire()
            return time.monotonic() - start
        assert asyncio.run(_run()) >= 0.15


class TestRegisterClassifier:
    def test_already_registered_by_code(self):
        rejected = [{"error": {"code": -18019901,
                               "message": "The tracking number 'X' has been registered, don't need to repeat registration."}}]
        assert main._already_registered(rejected) is True

    def test_already_by_exist_message(self):
        assert main._already_registered([{"error": {"code": 0, "message": "number already exists"}}]) is True

    def test_policy_blocked_is_not_already(self):
        rejected = [{"error": {"code": -18019823, "message": "policy restrictions"}}]
        assert main._already_registered(rejected) is False

    def test_empty(self):
        assert main._already_registered([]) is False


class TestUsps:
    def test_map_delivered(self):
        assert main._map_usps_status("<b>Your item was delivered</b> at the front door") == "Delivered"

    def test_map_out_for_delivery(self):
        assert main._map_usps_status("Your item is out for delivery") == "Out for Delivery"

    def test_map_label_created(self):
        assert main._map_usps_status("Label Created, USPS Awaiting Item") == "Info Received"

    def test_map_accepted(self):
        assert main._map_usps_status("Accepted at USPS Origin Facility") == "In Transit"

    def test_map_notice_left(self):
        assert main._map_usps_status("Notice Left, No Authorized Recipient Available") == "Delivery Attempted"

    def test_no_userid_returns_none_no_network(self, monkeypatch):
        monkeypatch.setattr(carriers.config, "USPS_API_USERID", None)
        assert asyncio.run(main._check_usps_webtools("9400111206200074888350")) is None

    def test_auth_backoff_skips_network(self, monkeypatch):
        monkeypatch.setattr(carriers.config, "USPS_API_USERID", "test-user")
        monkeypatch.setattr(carriers, "_usps_auth_failed_until", time.time() + 3600)
        assert asyncio.run(main._check_usps_webtools("9400111206200074888350")) is None
        carriers._usps_auth_failed_until = 0.0


class TestBrowserOnDemand:
    def test_allowed_when_off(self, monkeypatch):
        monkeypatch.setattr(carriers.config, "USPS_BROWSER_ONDEMAND", False)
        assert main._browser_allowed(None) is True
        assert main._browser_allowed(True) is True

    def test_on_demand_restricts_background(self, monkeypatch):
        monkeypatch.setattr(carriers.config, "USPS_BROWSER_ONDEMAND", True)
        assert main._browser_allowed(None) is False
        assert main._browser_allowed(False) is False
        assert main._browser_allowed(True) is True


class TestFetchOffline:
    def test_unavailable_without_keys(self, monkeypatch):
        # Never hit the network in tests — force "no providers".
        monkeypatch.setattr(carriers.config, "track17_api_key", "default_api_key")
        monkeypatch.setattr(carriers.config, "USPS_API_USERID", None)
        status, _carrier, source, detail = asyncio.run(main.fetch_track_status("123456789012"))
        assert status == "Unavailable"
        assert source == "None"
        assert detail is None


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.responses.pop(0)

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.responses.pop(0)


class TestTierGate:
    """17TRACK is a Pro perk: free chats fall through to the browser tier for
    self-served carriers (UPS/FedEx/DHL/USPS) and never reach a paid source.
    17TRACK-only carriers (international posts) return the Pro-required state
    instead of a paid call substituting for the perk — and since 2026-09-23 the
    paid Ship24 tier is Pro-only as well: a free chat whose free tiers come up
    empty gets Unavailable and a recheck, never a billed lookup. is_pro=None
    (legacy callers) keeps the pre-gate behavior.
    """

    INTL = "RR123456789CN"          # chinapost — a 17TRACK-only slug
    UPS = "1Z1234567890123456"

    def _install(self, monkeypatch):
        monkeypatch.setattr(carriers.config, "track17_api_key", "k-test")
        monkeypatch.setattr(carriers, "SOURCE_ROUTING", "matrix")
        monkeypatch.setattr(carriers, "quota_pct", lambda: None)
        # Pinned, not inherited from .env: this class asserts the shipped default
        # order (headless-first for Pro big-4), so an operator's PRO_BROWSER_FIRST=0
        # must not decide what these tests mean.
        monkeypatch.setattr(carriers.config, "PRO_BROWSER_FIRST", True)
        self.track17 = []

        async def fake_17track(tid):
            self.track17.append(tid)
            return ("In Transit", {"carrier": {"name": "UPS", "key": "k",
                                               "country": "US"},
                                   "detail": "moving"})
        monkeypatch.setattr(carriers, "_check_17track", fake_17track)

        self.browser = []

        async def fake_browser(tid, slug):
            self.browser.append((tid, slug))
            return ("Out for Delivery", "out for delivery now")
        monkeypatch.setattr(carriers, "_check_browser", fake_browser)

        self.ship24 = []
        monkeypatch.setattr(carriers.ship24, "enabled", lambda: True)

        async def fake_ship24(tid, client):
            self.ship24.append(tid)
            return ("delivered", "delivery_delivered",
                    "Delivered, Left at front door")
        monkeypatch.setattr(carriers.ship24, "check_ship24", fake_ship24)

    def test_pro_chat_reads_the_carrier_page_before_17track(self, monkeypatch):
        """Expectation flipped 2026-09-23 (PRO_BROWSER_FIRST): a Pro big-4 number
        is read on the carrier's own page first — 17TRACK's aggregate feed showed
        a delivered UPS parcel still in transit ~5 h after UPS itself said
        delivered, and a carrier-direct read cannot lag the carrier. 17TRACK is
        the fallback when the page finds nothing (see TestBrowserFirst)."""
        self._install(monkeypatch)
        status, _c, source, _d = asyncio.run(
            carriers.fetch_track_status(self.UPS, is_pro=True))
        assert self.browser == [(self.UPS, "ups")] and source == "UPS Browser"
        assert status == "Out for Delivery"
        assert self.track17 == [], "17TRACK must not be spent when the carrier answered"
        assert self.ship24 == []

    def test_free_chat_never_touches_17track(self, monkeypatch):
        """17TRACK is a Pro perk — and so is the paid tier: a background lookup
        (the browser belongs to the sweep) ends at Unavailable instead of buying a
        Ship24 call. Expectation updated 2026-09-23 with the user's decision."""
        self._install(monkeypatch)
        status, _c, source, _d = asyncio.run(
            carriers.fetch_track_status(self.UPS, is_pro=False))
        assert self.track17 == [], "17TRACK is a Pro perk"
        assert (status, source) == ("Unavailable", "None")
        assert self.ship24 == [], "a free chat must not buy a paid lookup"

    def test_free_chat_user_invoked_lookup_prefers_the_browser(self, monkeypatch):
        self._install(monkeypatch)
        status, _c, source, _d = asyncio.run(
            carriers.fetch_track_status(self.UPS, is_pro=False, allow_browser=True))
        assert source == "UPS Browser" and status == "Out for Delivery"
        assert self.ship24 == [], "the browser answered — Ship24 stays asleep"
        assert self.ship24 == [], "the browser answered — Ship24 stays asleep"

    def test_free_ups_still_never_falls_to_the_paid_tier(self, monkeypatch):
        """Was "…falls_to_ship24_only_when_the_browser_fails": a failed free read
        used to hand a free chat's number to a billed lookup. The 2026-09-23
        decision stops that, so the assertion is now the opposite one."""
        self._install(monkeypatch)

        async def dead(tid, slug):
            return None
        monkeypatch.setattr(carriers, "_check_browser", dead)
        status, _c, source, _d = asyncio.run(
            carriers.fetch_track_status(self.UPS, is_pro=False))
        assert (status, source) == ("Unavailable", "None")
        assert self.ship24 == [], "a failed browser read must not become a paid call"

    def test_free_intl_returns_the_pro_state_without_ship24(self, monkeypatch):
        self._install(monkeypatch)
        status, _c, source, _d = asyncio.run(
            carriers.fetch_track_status(self.INTL, is_pro=False))
        assert status == "Unavailable" and source == "Pro"
        assert self.track17 == [] and self.ship24 == [], \
            "a paid call must not substitute for the Pro perk"

    def test_pro_intl_uses_17track(self, monkeypatch):
        self._install(monkeypatch)
        status, _c, source, _d = asyncio.run(
            carriers.fetch_track_status(self.INTL, is_pro=True))
        assert self.track17 == [self.INTL] and source == "17TRACK"
        assert status == "In Transit"

    def test_unknown_tier_keeps_legacy_behavior(self, monkeypatch):
        """is_pro=None (legacy callers/tests): 17TRACK stays allowed."""
        self._install(monkeypatch)
        asyncio.run(carriers.fetch_track_status(self.UPS))
        assert self.track17 == [self.UPS] and self.browser == []

    def test_tier_counters_split_the_outcomes(self, monkeypatch):
        """The /status 'by tier' line is the auditable proof of the gate: the
        same lookup tallies ok for Pro and fail (Pro-gated) for free."""
        self._install(monkeypatch)
        before = {k: dict(v) for k, v in carriers.LOOKUP_TIER.items()}
        asyncio.run(carriers.fetch_track_status(self.UPS, is_pro=True))
        asyncio.run(carriers.fetch_track_status(self.INTL, is_pro=False))
        assert carriers.LOOKUP_TIER["pro"]["ok"] == before["pro"]["ok"] + 1
        assert carriers.LOOKUP_TIER["free"]["fail"] == before["free"]["fail"] + 1
        assert carriers.LOOKUP_TIER["free"]["ok"] == before["free"]["ok"]

    def test_apply_lookup_owns_the_needs_pro_lifecycle(self):
        info = {"name": "Box", "status": "In Transit"}
        refresh._apply_lookup(info, "Unavailable",
                              {"code": "chinapost", "name": "CN Post", "url": "u"},
                              "Pro", None)
        assert info["needs_pro"] is True
        assert info["status"] == "In Transit", "last known status is preserved"
        refresh._apply_lookup(info, "In Transit",
                              {"code": "usps", "name": "USPS", "url": "u"},
                              "USPS Browser", "arrived at facility")
        assert info["needs_pro"] is False, "a real source clears the flag"

    def setup_method(self):
        import usps_api
        usps_api._reset_token()
        self.usps_api = usps_api

    def test_disabled_without_creds(self, monkeypatch):
        monkeypatch.setenv("USPS_CONSUMER_KEY", "")
        monkeypatch.setenv("USPS_CONSUMER_SECRET", "")
        assert self.usps_api.enabled() is False
        assert asyncio.run(
            self.usps_api.check_usps("9400111206200074888350", _FakeClient([]))) is None

    def test_token_and_lookup(self, monkeypatch):
        monkeypatch.setenv("USPS_CONSUMER_KEY", "k")
        monkeypatch.setenv("USPS_CONSUMER_SECRET", "s")
        client = _FakeClient([
            _Resp(200, {"access_token": "tok123", "expires_in": 3600}),
            _Resp(200, {"trackingSummary": {
                "statusSummary": "In Transit, Arriving On Time",
                "statusCategory": "INTRANSIT"}}),
        ])
        result = asyncio.run(self.usps_api.check_usps("9400111206200074888350", client))
        assert result == ("In Transit, Arriving On Time", "INTRANSIT")
        assert client.calls[0][1].endswith("/oauth2/v3/token")
        assert client.calls[0][2]["json"]["grant_type"] == "client_credentials"
        assert "/tracking/v3/tracking/9400111206200074888350" in client.calls[1][1]
        assert client.calls[1][2]["headers"]["Authorization"] == "Bearer tok123"
        assert client.calls[1][2]["params"] == {"expand": "SUMMARY"}

    def test_token_cached_across_lookups(self, monkeypatch):
        monkeypatch.setenv("USPS_CONSUMER_KEY", "k")
        monkeypatch.setenv("USPS_CONSUMER_SECRET", "s")
        client = _FakeClient([
            _Resp(200, {"access_token": "tok123", "expires_in": 3600}),
            _Resp(200, {"trackingSummary": {"statusSummary": "Delivered",
                                            "statusCategory": "DELIVERED"}}),
            _Resp(200, {"trackingSummary": {"statusSummary": "Delivered",
                                            "statusCategory": "DELIVERED"}}),
        ])
        asyncio.run(self.usps_api.check_usps("9400111206200074888350", client))
        asyncio.run(self.usps_api.check_usps("9300111043900023959782", client))
        assert len([c for c in client.calls if c[0] == "POST"]) == 1

    def test_401_reauths_once_and_retries(self, monkeypatch):
        monkeypatch.setenv("USPS_CONSUMER_KEY", "k")
        monkeypatch.setenv("USPS_CONSUMER_SECRET", "s")
        client = _FakeClient([
            _Resp(200, {"access_token": "stale", "expires_in": 3600}),
            _Resp(401, {}),
            _Resp(200, {"access_token": "fresh", "expires_in": 3600}),
            _Resp(200, {"trackingSummary": {"statusSummary": "Delivered",
                                            "statusCategory": "DELIVERED"}}),
        ])
        result = asyncio.run(self.usps_api.check_usps("9400111206200074888350", client))
        assert result == ("Delivered", "DELIVERED")
        gets = [c for c in client.calls if c[0] == "GET"]
        assert len(gets) == 2
        assert gets[1][2]["headers"]["Authorization"] == "Bearer fresh"

    def test_404_pre_scan_returns_none(self, monkeypatch):
        monkeypatch.setenv("USPS_CONSUMER_KEY", "k")
        monkeypatch.setenv("USPS_CONSUMER_SECRET", "s")
        client = _FakeClient([
            _Resp(200, {"access_token": "tok", "expires_in": 3600}),
            _Resp(404, {}),
        ])
        assert asyncio.run(
            self.usps_api.check_usps("9400111206200074888350", client)) is None

    def test_status_summary_maps_through_main(self, monkeypatch):
        """The bot's vocabulary mapper must accept API statusSummary text."""
        monkeypatch.setenv("USPS_CONSUMER_KEY", "k")
        monkeypatch.setenv("USPS_CONSUMER_SECRET", "s")
        client = _FakeClient([
            _Resp(200, {"access_token": "tok", "expires_in": 3600}),
            _Resp(200, {"trackingSummary": {
                "statusSummary": "A shipping label has been prepared for your item",
                "statusCategory": "PRESHIPMENT"}}),
        ])
        text, _cat = asyncio.run(
            self.usps_api.check_usps("9400111206200074888350", client))
        assert main._map_usps_status(text) == "Info Received"

    def test_fetch_track_status_prefers_usps_api(self, monkeypatch):
        """With creds set, fetch_track_status returns via USPS API (no browser)."""
        monkeypatch.setenv("USPS_CONSUMER_KEY", "k")
        monkeypatch.setenv("USPS_CONSUMER_SECRET", "s")
        self.usps_api._reset_token()
        orig_http_client = carriers.http_client

        class _C:
            async def post(self, url, **kw):
                return _Resp(200, {"access_token": "tok", "expires_in": 3600})

            async def get(self, url, **kw):
                return _Resp(200, {"trackingSummary": {
                    "statusSummary": "Out for Delivery",
                    "statusCategory": "OFDELIVERY"}})

        monkeypatch.setattr(carriers, "http_client", lambda: _C())
        monkeypatch.setattr(carriers.config, "USPS_API_USERID", None)
        monkeypatch.setattr(carriers.config, "USPS_BROWSER_TRACK", False)
        try:
            status, _c, source, detail = asyncio.run(
                main.fetch_track_status("9400111206200074888350"))
            assert status == "Out for Delivery"
            assert source == "USPS API"
            assert detail == "Out for Delivery"
        finally:
            carriers.http_client = orig_http_client


class TestAtRestEncryption:
    """Data-at-rest encryption contract (the earlier pipeline hole: tests
    asserted in-memory behavior only, so a broken key derivation that wrote
    plaintext for a whole session passed every check)."""

    def test_get_fernet_returns_an_instance(self, monkeypatch):
        """Pins the regression directly: with cryptography installed and a
        token set, the key derivation must return a Fernet."""
        import storage
        if not storage._ENCRYPTION_AVAILABLE:
            import pytest
            pytest.skip("cryptography not installed")
        monkeypatch.setattr(storage.config, "_bot_token", "123456:TEST-TOKEN")
        assert storage._get_fernet() is not None

    def test_saved_data_is_ciphertext_on_disk(self, tmp_path, monkeypatch):
        """What lands on disk must not be readable JSON."""
        import storage
        if not storage._ENCRYPTION_AVAILABLE:
            import pytest
            pytest.skip("cryptography not installed")
        monkeypatch.setattr(storage.config, "_bot_token", "123456:TEST-TOKEN")
        data = tmp_path / "track_data.json"
        main.track_sessions.clear()
        main.track_sessions[42] = {
            "9400111206200074888350": {"name": "Mom gift", "status": "In Transit"}
        }
        old_file = main.DATA_FILE
        main.DATA_FILE = data
        try:
            main.save_data()
            raw = data.read_bytes()
            assert not raw.startswith(b"{"), "track_data.json was written as plaintext"
            assert raw.startswith(b"gAAAA"), "expected a Fernet token on disk"
            assert "Mom gift" not in raw.decode(errors="ignore")
        finally:
            main.DATA_FILE = old_file
            main.track_sessions.clear()

    def test_encryption_roundtrip(self, tmp_path, monkeypatch):
        import storage
        if not storage._ENCRYPTION_AVAILABLE:
            import pytest
            pytest.skip("cryptography not installed")
        monkeypatch.setattr(storage.config, "_bot_token", "123456:TEST-TOKEN")
        data = tmp_path / "track_data.json"
        old_file = main.DATA_FILE
        main.DATA_FILE = data
        try:
            main.track_sessions.clear()
            main.track_sessions[7] = {
                "1Z1E852G0310312712": {"name": "Shoes", "status": "Delivered"}
            }
            main.save_data()
            main.track_sessions.clear()
            main.load_data()
            assert main.track_sessions[7]["1Z1E852G0310312712"]["name"] == "Shoes"
        finally:
            main.DATA_FILE = old_file
            main.track_sessions.clear()

    def test_plaintext_file_is_migrated_to_ciphertext(self, tmp_path, monkeypatch):
        """A legacy plaintext file must be read, re-saved encrypted, and the
        plaintext copy deleted once the migration verifies by round-trip (M6):
        it must not outlive its purpose."""
        import storage
        if not storage._ENCRYPTION_AVAILABLE:
            import pytest
            pytest.skip("cryptography not installed")
        monkeypatch.setattr(storage.config, "_bot_token", "123456:TEST-TOKEN")
        data = tmp_path / "track_data.json"
        data.write_text(json.dumps({"9": {"TID1": {"name": "Legacy"}}}))
        old_file = main.DATA_FILE
        main.DATA_FILE = data
        try:
            main.load_data()
            assert main.track_sessions[9]["TID1"]["name"] == "Legacy"
            assert data.read_bytes().startswith(b"gAAAA"), "not migrated to ciphertext"
            assert not (tmp_path / "track_data.plaintext.bak").exists(), \
                "a verified migration must delete the plaintext copy"
        finally:
            main.DATA_FILE = old_file
            main.track_sessions.clear()

    def test_wrong_key_ciphertext_still_fails_loud(self, tmp_path, monkeypatch):
        """Fail-fast is intentional: ciphertext that cannot be decrypted must
        abort startup with the original bytes preserved — never silently
        start fresh and overwrite the owner's data."""
        import base64
        import storage
        from cryptography.fernet import Fernet
        monkeypatch.setattr(storage.config, "_bot_token", "123456:TEST-TOKEN")
        data = tmp_path / "track_data.json"
        other = Fernet(base64.urlsafe_b64encode(b"\x11" * 32))
        data.write_bytes(other.encrypt(b'{"5": {"TID9": {"name": "Secret"}}}'))
        old_file = main.DATA_FILE
        main.DATA_FILE = data
        try:
            import pytest
            with pytest.raises(SystemExit):
                main.load_data()
            assert not data.exists()
            assert list(tmp_path.glob("track_data.json.locked-*")), \
                "the preserve must be timestamped"
        finally:
            main.DATA_FILE = old_file
            main.track_sessions.clear()


class TestKeySeparation:
    """DATA_ENCRYPTION_KEY decouples the encrypted store from the bot token:
    rotating the token must no longer brick the data, and a leaked token must
    no longer open the disk."""

    def test_env_key_is_preferred_over_the_token(self, monkeypatch):
        import storage
        if not storage._ENCRYPTION_AVAILABLE:
            import pytest
            pytest.skip("cryptography not installed")
        monkeypatch.setattr(storage.config, "DATA_ENCRYPTION_KEY", "env-key")
        monkeypatch.setattr(storage.config, "_bot_token", "123456:TEST-TOKEN")
        blob = storage._encrypt_payload("payload")
        assert storage._decrypt_with("env-key", blob) == "payload"
        assert storage._decrypt_with("123456:TEST-TOKEN", blob) is None

    def test_token_rotation_no_longer_bricks_the_store(self, monkeypatch):
        """Pre-separation this exact sequence made the data undecryptable."""
        import storage
        if not storage._ENCRYPTION_AVAILABLE:
            import pytest
            pytest.skip("cryptography not installed")
        monkeypatch.setattr(storage.config, "DATA_ENCRYPTION_KEY", "env-key")
        monkeypatch.setattr(storage.config, "_bot_token", "111:TOKEN-A")
        blob = storage._encrypt_payload("payload")
        monkeypatch.setattr(storage.config, "_bot_token", "222:TOKEN-B")
        assert storage._decrypt_payload(blob) == "payload"

    def test_legacy_file_is_rekeyed_on_first_read(self, tmp_path, monkeypatch):
        """A file written under the bot token must stay readable AND be upgraded
        to the env key, so the token is free to rotate afterwards."""
        import storage
        if not storage._ENCRYPTION_AVAILABLE:
            import pytest
            pytest.skip("cryptography not installed")
        monkeypatch.setattr(storage.config, "DATA_ENCRYPTION_KEY", None)
        monkeypatch.setattr(storage.config, "_bot_token", "123456:OLD-TOKEN")
        data = tmp_path / "track_data.json"
        storage._write_protected(data, json.dumps({"7": {"TID": {"name": "Legacy"}}}))
        legacy_bytes = data.read_bytes()

        monkeypatch.setattr(storage.config, "DATA_ENCRYPTION_KEY", "env-key")
        text, was_enc = storage._read_protected(data)
        assert json.loads(text)["7"]["TID"]["name"] == "Legacy"
        assert was_enc
        rekeyed = data.read_bytes()
        assert rekeyed != legacy_bytes and rekeyed.startswith(b"gAAAA")
        assert storage._decrypt_with("env-key", rekeyed) is not None
        assert storage._decrypt_with("123456:OLD-TOKEN", rekeyed) is None

    def test_no_key_material_means_plaintext_not_fake_encryption(self, monkeypatch):
        """The old hardcoded fallback made data *look* encrypted. Without any
        key material we now say so and write plaintext."""
        import storage
        monkeypatch.setattr(storage.config, "DATA_ENCRYPTION_KEY", None)
        monkeypatch.setattr(storage.config, "_bot_token", None)
        monkeypatch.delenv("TELEGRAM_BOT_API_TOKEN", raising=False)
        assert storage._master_secret() is None
        assert storage._get_fernet() is None
        assert storage._encrypt_payload("x") == b"x"

    def test_env_key_is_documented_in_the_example_env(self):
        from pathlib import Path
        example = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
        assert "DATA_ENCRYPTION_KEY=" in example
        assert "BACK IT UP" in example


class TestPerChatEnvelopes:
    """track_data.json v2: one envelope per chat, sealed under its own random
    data key. Blast-radius control — the master still opens everything."""

    def _prepare(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage.config, "DATA_ENCRYPTION_KEY", None)
        monkeypatch.setattr(storage.config, "_bot_token", "123456:TEST-TOKEN")
        return tmp_path / "track_data.json"

    def _container(self, data):
        return json.loads(storage._decrypt_payload(data.read_bytes()))

    def test_container_has_one_envelope_per_chat(self, tmp_path, monkeypatch):
        import storage
        from cryptography.fernet import InvalidToken
        import pytest
        data = self._prepare(tmp_path, monkeypatch)
        old_file = main.DATA_FILE
        main.DATA_FILE = data
        try:
            main.track_sessions.clear()
            main.track_sessions[7] = {"TID1": {"name": "A", "status": "In Transit"}}
            main.track_sessions[9] = {"TID2": {"name": "B", "status": "Delivered"}}
            main.save_data()
            plain = storage._decrypt_payload(data.read_bytes())
            container = json.loads(plain)
            assert container["v"] == 2
            assert set(container["chats"]) == {"7", "9"}
            master = storage._get_fernet()
            for envelope in container["chats"].values():
                assert envelope["blob"].startswith("gAAAA")   # encrypted payload
                assert envelope["dek"].startswith("gAAAA")    # wrapped data key
                # The blob is sealed under the chat's DEK, NOT under the master:
                # knowing the master alone does not open a chat's data.
                with pytest.raises(InvalidToken):
                    master.decrypt(envelope["blob"].encode())
                # ...and it isn't readable JSON either.
                with pytest.raises(Exception):
                    json.loads(envelope["blob"])
        finally:
            main.DATA_FILE = old_file
            main.track_sessions.clear()

    def test_each_chat_blob_needs_its_own_key(self, tmp_path, monkeypatch):
        """The point of envelopes: chat 7's payload must not open with chat 9's
        key, even though the master unwraps both."""
        import storage
        from cryptography.fernet import Fernet, InvalidToken
        import pytest
        data = self._prepare(tmp_path, monkeypatch)
        old_file = main.DATA_FILE
        main.DATA_FILE = data
        try:
            main.track_sessions.clear()
            main.track_sessions[7] = {"TID1": {"name": "A"}}
            main.track_sessions[9] = {"TID2": {"name": "B"}}
            main.save_data()
            container = self._container(data)
            master = storage._get_fernet()
            dek_a = storage._unwrap_dek(master, container["chats"]["7"]["dek"])
            dek_b = storage._unwrap_dek(master, container["chats"]["9"]["dek"])
            assert dek_a != dek_b
            blob_a = container["chats"]["7"]["blob"].encode()
            assert json.loads(Fernet(dek_a).decrypt(blob_a))["TID1"]["name"] == "A"
            with pytest.raises(InvalidToken):
                Fernet(dek_b).decrypt(blob_a)
        finally:
            main.DATA_FILE = old_file
            main.track_sessions.clear()

    def test_deleted_chat_leaves_no_envelope(self, tmp_path, monkeypatch):
        """Shredding: with its key gone, nothing of that chat is recoverable."""
        data = self._prepare(tmp_path, monkeypatch)
        old_file = main.DATA_FILE
        main.DATA_FILE = data
        try:
            main.track_sessions.clear()
            main.track_sessions[7] = {"TID1": {"name": "A"}}
            main.track_sessions[9] = {"TID2": {"name": "B"}}
            main.save_data()
            del main.track_sessions[9]
            main.save_data()
            container = self._container(data)
            assert set(container["chats"]) == {"7"}
        finally:
            main.DATA_FILE = old_file
            main.track_sessions.clear()

    def test_v1_file_is_upgraded_and_kept_as_ciphertext_backup(self, tmp_path, monkeypatch):
        import storage
        data = self._prepare(tmp_path, monkeypatch)
        storage._write_protected(data, json.dumps({"7": {"TID1": {"name": "Legacy"}}}))
        old_file = main.DATA_FILE
        main.DATA_FILE = data
        try:
            main.load_data()
            assert main.track_sessions[7]["TID1"]["name"] == "Legacy"
            main.save_data()
            assert self._container(data)["v"] == 2
            bak = data.with_suffix(".v1.bak")
            assert bak.exists() and bak.read_bytes().startswith(b"gAAAA")
            # and the backup is STILL readable after the upgrade (rollback path)
            text, _ = storage._read_protected(bak)
            assert json.loads(text)["7"]["TID1"]["name"] == "Legacy"
        finally:
            main.DATA_FILE = old_file
            main.track_sessions.clear()

    def test_unopenable_envelope_preserves_the_file(self, tmp_path, monkeypatch):
        """A key problem must never look like corrupt data: preserve, then stop."""
        import base64
        import storage
        from cryptography.fernet import Fernet
        import pytest
        data = self._prepare(tmp_path, monkeypatch)
        # Valid shape, but the wrapped key belongs to a DIFFERENT master.
        stranger = Fernet(base64.urlsafe_b64encode(b"\x22" * 32))
        container = {"v": 2, "chats": {"7": {"dek": stranger.encrypt(Fernet.generate_key()).decode(),
                                             "blob": stranger.encrypt(b"{}").decode()}}}
        storage._write_protected(data, json.dumps(container))
        old_file = main.DATA_FILE
        main.DATA_FILE = data
        try:
            with pytest.raises(SystemExit):
                main.load_data()
            assert not data.exists()
            assert list(tmp_path.glob("track_data.json.corrupt-*")), "the preserve must be timestamped"
        finally:
            main.DATA_FILE = old_file
            main.track_sessions.clear()


class TestStartGreeting:
    """/start must greet a fresh chat even when the monotonic clock is small.

    Regression: the throttle defaulted the "last greeting" to 0.0, so on a
    machine whose monotonic clock starts near zero every first /start looked
    like a duplicate sent moments ago — and the greeting was deleted as spam
    (the live bot was observed greeting nobody)."""

    class _Msg:
        def __init__(self, chat_id, message_id):
            self.texts = []
            self.text = "/start"
            self.message_id = message_id
            self.chat = type("C", (), {"id": chat_id, "type": "private"})()
            self.from_user = type("U", (), {"id": 5, "first_name": "F", "username": "u"})()
            self.deleted = False

        async def answer(self, text, **kw):
            self.texts.append(text)
            return type("S", (), {"chat": self.chat, "message_id": 1})()

        async def delete(self):
            self.deleted = True

    def test_fresh_chat_is_greeted_with_a_small_clock(self, monkeypatch):
        main._recent_starts.clear()
        main._start_last_response.clear()
        monkeypatch.setattr(main.time, "monotonic", lambda: 2.0)
        monkeypatch.setattr(main.config, "MINI_APP_URL", None)
        msg = self._Msg(chat_id=555001, message_id=1)
        asyncio.run(main.start_handler(msg))
        assert msg.texts and "Packey" in msg.texts[0]
        assert not msg.deleted

    def test_repeat_within_the_window_is_still_throttled(self, monkeypatch):
        main._recent_starts.clear()
        main._start_last_response.clear()
        monkeypatch.setattr(main.config, "MINI_APP_URL", None)
        clock = {"now": 2.0}
        monkeypatch.setattr(main.time, "monotonic", lambda: clock["now"])
        first = self._Msg(chat_id=555002, message_id=1)
        asyncio.run(main.start_handler(first))
        assert first.texts
        clock["now"] = 5.0                       # 3s later — a real duplicate
        second = self._Msg(chat_id=555002, message_id=2)
        asyncio.run(main.start_handler(second))
        assert second.texts == [] and second.deleted

    def test_a_stale_start_entry_is_pruned_and_still_greets(self, monkeypatch):
        """_start_last_response is keyed by chat and nothing ever removed a key,
        so it grew for the life of the process — while its sibling
        _recent_starts pruned itself. An entry older than the throttle window is
        indistinguishable from "never greeted", so dropping it must not change
        what the user sees: the greeting still goes out, and the table shrinks.
        """
        main._recent_starts.clear()
        main._start_last_response.clear()
        monkeypatch.setattr(main.config, "MINI_APP_URL", None)
        clock = {"now": 100.0}
        monkeypatch.setattr(main.time, "monotonic", lambda: clock["now"])
        # Two chats greeted long ago, well past START_THROTTLE_SECONDS.
        for stale_chat in (777001, 777002, 777003):
            main._start_last_response[stale_chat] = 1.0
        clock["now"] = 100.0 + main.START_THROTTLE_SECONDS + 1

        msg = self._Msg(chat_id=777001, message_id=1)
        asyncio.run(main.start_handler(msg))

        assert msg.texts and "Packey" in msg.texts[0], (
            "an expired entry must read as 'never greeted', not as a duplicate")
        assert not msg.deleted
        assert main._start_last_response.get(777002) is None, (
            "stale entries must be dropped, or the table grows forever")
        assert main._start_last_response.get(777003) is None

    def test_a_fresh_start_entry_still_throttles_after_the_prune(self, monkeypatch):
        """The prune must not eat an entry that is still load-bearing: a repeat
        /start inside the window is a duplicate and is still deleted."""
        main._recent_starts.clear()
        main._start_last_response.clear()
        monkeypatch.setattr(main.config, "MINI_APP_URL", None)
        clock = {"now": 500.0}
        monkeypatch.setattr(main.time, "monotonic", lambda: clock["now"])
        first = self._Msg(chat_id=778001, message_id=1)
        asyncio.run(main.start_handler(first))
        assert first.texts
        # A different chat's stale entries must not disturb this one's window.
        main._start_last_response[999999] = 1.0
        clock["now"] = 503.0
        second = self._Msg(chat_id=778001, message_id=2)
        asyncio.run(main.start_handler(second))
        assert second.texts == [] and second.deleted, (
            "an in-window repeat is still a duplicate")


class TestInternationalCarriers:
    """UPU/S10 numbers carry their ORIGIN country code, which is how CN/HK/EU
    posts are recognised without a carrier API (17TRACK covers the lookup)."""

    def test_china_post(self):
        c = main.detect_carrier("RR123456789CN")
        assert c["code"] == "chinapost"
        assert c["name"] == "China Post / ePacket"
        assert "17track.net" in c["url"]

    def test_cainiao_style_cn(self):
        assert main.detect_carrier("LP123456789CN")["code"] == "chinapost"

    def test_hongkong_post(self):
        c = main.detect_carrier("EE123456789HK")
        assert c["code"] == "hkpost"
        assert c["name"] == "Hongkong Post"

    def test_other_country_falls_back_to_intl(self):
        c = main.detect_carrier("RR123456789FR")
        assert c["code"] == "intl"
        assert c["name"] == "International post (FR)"

    def test_us_and_gb_still_map_to_their_carriers(self):
        assert main.detect_carrier("EE123456789US")["code"] == "usps"
        assert main.detect_carrier("RR123456789GB")["code"] == "royalmail"

    def test_every_carrier_gets_a_link(self):
        """No card may ship without a working tracking link."""
        for tid in ("RR123456789CN", "EE123456789HK", "RR123456789FR",
                    "Mom gift", "123456789012", "1Z1E852G0310312712"):
            assert main.detect_carrier(tid)["url"].startswith("https://")


class Test17TrackCarrierIdentity:
    """17TRACK names the carrier even when our regexes say 'unknown' — that is
    what fixes 'Unknown Carrier' for international numbers."""

    ITEM = {
        "number": "RR123456789CN",
        "carrier": 21051,
        "track_info": {
            "latest_status": {"status": "InTransit", "sub_status": "InTransit"},
            "latest_event": {"description": "Departed from origin",
                             "location": "Shenzhen, CN"},
            "tracking": {"providers": [{
                "provider": {"alias": "china-post", "country": "CN",
                             "homepage": "https://www.chinapost.com.cn",
                             "key": 21051, "name": "China Post", "tel": None},
            }]},
        },
    }

    def test_extracts_verified_identity(self):
        ident = carriers._extract_carrier(self.ITEM)
        assert ident["name"] == "China Post"
        assert ident["key"] == 21051
        assert ident["country"] == "CN"

    def test_extracts_latest_event_detail(self):
        assert carriers._extract_detail(self.ITEM) == "Departed from origin — Shenzhen, CN"

    def test_missing_carrier_block_is_tolerated(self):
        """Older payloads have no provider block — must not raise or lie."""
        assert carriers._extract_carrier({"track_info": {}}) == {}
        assert carriers._extract_detail({"track_info": {}}) is None

    def test_legacy_carrier_info_nesting(self):
        legacy = {"track_info": {"carrier_info": {"carrier_name": "Cainiao"}}}
        assert carriers._extract_carrier(legacy)["name"] == "Cainiao"


class TestVerifiedLink:
    """A wrong deep link is worse than a generic one: only keep the
    carrier-specific URL when 17TRACK's verified name agrees with the guess."""

    FEDEX_URL = "https://www.fedex.com/fedextrack/?trknbr=123456789012"

    def test_agreement_keeps_the_specific_link(self):
        assert carriers.verify_link("fedex", "FedEx", self.FEDEX_URL, "123456789012") == self.FEDEX_URL

    def test_disagreement_switches_to_universal_link(self):
        """The live case: 12 digits regexed as FedEx, verified as UPS."""
        url = carriers.verify_link("fedex", "UPS", self.FEDEX_URL, "123456789012")
        assert url.startswith("https://www.17track.net/")
        assert "123456789012" in url

    def test_no_verified_name_keeps_the_guess(self):
        assert carriers.verify_link("usps", None, "https://tools.usps.com/x", "94") == "https://tools.usps.com/x"

    def test_united_parcel_alias_matches_ups(self):
        assert carriers.verify_link("ups", "United Parcel Service", "https://www.ups.com/t", "1Z") == "https://www.ups.com/t"




class TestSourceRouting:
    """17TRACK is the reserved lane: CN/HK/international always keep it; a
    carrier with a configured official API, or a quota crunch, does not."""

    def _set_mode(self, monkeypatch, mode):
        monkeypatch.setattr(carriers, "SOURCE_ROUTING", mode)

    def test_matrix_keeps_17track_for_cn_hk(self, monkeypatch):
        self._set_mode(monkeypatch, "matrix")
        assert carriers._use_17track("chinapost") is True
        assert carriers._use_17track("hkpost") is True
        assert carriers._use_17track("intl") is True

    def test_matrix_keeps_17track_when_no_alternative_exists(self, monkeypatch):
        """ups/fedex/dhl/royalmail/canadapost have no other source yet."""
        self._set_mode(monkeypatch, "matrix")
        monkeypatch.setattr(carriers.config, "USPS_API_USERID", None)
        for slug in ("ups", "fedex", "dhl", "royalmail", "canadapost", "unknown"):
            assert carriers._use_17track(slug) is True, slug

    def test_matrix_skips_17track_when_official_usps_api_is_configured(self, monkeypatch):
        self._set_mode(monkeypatch, "matrix")
        monkeypatch.setattr(carriers.config, "USPS_API_USERID", "TESTUSERID")
        assert carriers._use_17track("usps") is False

    def test_matrix_keeps_usps_when_no_official_api(self, monkeypatch):
        self._set_mode(monkeypatch, "matrix")
        monkeypatch.setattr(carriers.config, "USPS_API_USERID", None)
        monkeypatch.setattr(carriers.usps_api, "enabled", lambda: False)
        assert carriers._use_17track("usps") is True

    def test_legacy_mode_never_skips(self, monkeypatch):
        self._set_mode(monkeypatch, "legacy")
        monkeypatch.setattr(carriers.config, "USPS_API_USERID", "TESTUSERID")
        assert carriers._use_17track("usps") is True

    def test_quota_pressure_sheds_only_slugs_with_a_fallback(self, monkeypatch):
        self._set_mode(monkeypatch, "matrix")
        monkeypatch.setattr(carriers.config, "USPS_API_USERID", None)
        monkeypatch.setattr(carriers.usps_api, "enabled", lambda: False)
        carriers.record_quota(950, 1000)  # 95% — past the hard threshold
        try:
            assert carriers.quota_pct() == 95.0
            assert carriers._use_17track("ups") is True        # nothing else can check it
            assert carriers._use_17track("ontrac") is False    # direct scrape exists
            assert carriers._use_17track("chinapost") is True  # never starved
        finally:
            carriers._quota_state.clear()

    def test_stale_quota_reading_is_ignored(self, monkeypatch):
        self._set_mode(monkeypatch, "matrix")
        monkeypatch.setattr(carriers.config, "USPS_API_USERID", None)
        carriers.record_quota(999, 1000)
        carriers._quota_state["at"] = time.time() - 10_000  # older than the window
        try:
            assert carriers.quota_pct() is None
            assert carriers._use_17track("ups") is True
        finally:
            carriers._quota_state.clear()


class TestAdaptiveCadence:
    """State-adaptive cadence replaced the per-chat tier gate: without it the
    loop polled delivered and pre-scan numbers forever (see refresh.CADENCE)."""

    def test_every_status_the_mappers_emit_has_a_rule(self):
        """A status with no rule would silently fall into the slow lane — the
        table must cover the whole vocabulary the app can store."""
        emitted = {main._map_17track_status(code) for code in (
            "InfoReceived", "InTransit", "Expired", "Pickup", "Undelivered",
            "DeliveryFailure", "AvailableForPickup", "OutForDelivery",
            "Delivered", "Exception", "NotFound")}
        emitted |= {"Pending", "Unavailable", "Info Received", "In Transit",
                    "Out for Delivery", "Available for Pickup",
                    "Delivery Attempted"}
        for status in emitted:
            if status in refresh.CADENCE or status in refresh.CADENCE_TERMINAL:
                continue
            # Available for Pickup has no dedicated lane by design (2026-09-17):
            # it rides the unknown-status default. Anything else is a bug.
            assert status == "Available for Pickup", status
            assert refresh.cadence_seconds(status, True) == 7200

    def test_in_transit_baseline_comes_from_config(self):
        """One source of truth for the tier baseline: the schedule, /start's
        pitch and the Pro-activation message must all quote the same numbers."""
        assert refresh.CADENCE["In Transit"] == (config.PRO_REFRESH_INTERVAL,
                                                config.FREE_REFRESH_INTERVAL)

    def test_delivered_never_checked_again(self):
        assert refresh.cadence_seconds("Delivered", True) is None
        assert refresh.cadence_seconds("Delivered", False) is None

    def test_in_transit_keeps_the_advertised_tier_intervals(self):
        assert refresh.cadence_seconds("In Transit", True) == 7200
        assert refresh.cadence_seconds("In Transit", False) == 14400

    def test_delivery_day_is_checked_harder_than_transit(self):
        assert (refresh.cadence_seconds("Out for Delivery", True)
                < refresh.cadence_seconds("In Transit", True))

    def test_pre_scan_is_a_slow_lane_by_design(self):
        # User call 2026-09-20: quota headroom over faster first-scan
        # detection -- pre-scan waits LONGER than transit (the 2026-09-17
        # fast-lane inversion is intentionally reverted).
        assert (refresh.cadence_seconds("Info Received", True)
                >= refresh.cadence_seconds("In Transit", True))

    def test_pre_scan_pro_interval_is_advertised(self):
        """Exact pin so a cadence change can't silently diverge from the pitch
        (same style as test_in_transit_keeps_the_advertised_tier_intervals)."""
        assert refresh.cadence_seconds("Info Received", True) == 21600
        assert refresh.cadence_seconds("Info Received", False) == 43200

    def test_failed_lookup_retries_instead_of_freezing(self):
        secs = refresh.cadence_seconds("Unavailable", True)
        assert secs is not None and secs > 0

    def test_quota_pressure_stretches_non_critical_only(self, monkeypatch):
        monkeypatch.setattr(carriers, "quota_pct",
                            lambda: carriers.TRACK17_HARD_PCT + 1.0)
        assert refresh.cadence_seconds("In Transit", True) == 14400  # stretched
        assert refresh.cadence_seconds("Info Received", True) == 43200
        assert refresh.cadence_seconds("Out for Delivery", True) == config.OFD_FAST_PRO  # sharp

    def _failed_lookup(self, monkeypatch, status="Unavailable", detail=None):
        monkeypatch.setattr(carriers, "quota_pct", lambda: None)  # no stretch
        monkeypatch.setattr(carriers, "fetch_track_status",
                            self._lookup_returning(status, detail))

    @staticmethod
    def _lookup_returning(status, detail):
        async def _lookup(tracking_id, is_pro=None, **kwargs):
            return status, {"code": "usps", "name": "USPS", "url": ""}, "17TRACK", detail
        return _lookup

    def test_unavailable_uses_fast_lane(self, monkeypatch):
        """A failed lookup retries on the (60, 600) lane, not on the lane of the
        last KNOWN status: _apply_lookup preserves that status on purpose, so
        scheduling from it buried the retry hours out and the fast lane never
        fired at all."""
        self._failed_lookup(monkeypatch)
        tid = "9400111206200074888350"

        async def check(info, is_pro):
            # The semaphore is built inside the loop: on 3.9 it needs one.
            return await refresh._check_one(7, tid, info, is_pro, asyncio.Semaphore(1))

        for is_pro, expected in ((True, 60), (False, 600)):
            info = {"name": "Box", "status": "In Transit"}
            refresh._due_at.clear()
            try:
                asyncio.run(check(info, is_pro))
                due_in = refresh._due_at[(7, tid)] - time.monotonic()
                # Measured just after scheduling, so allow the microseconds.
                assert expected - 2 <= due_in <= expected + 2, (is_pro, due_in)
                # ...and nothing about the stored record changed.
                assert info["status"] == "In Transit"
                assert info["last_lookup_failed"] is True
            finally:
                refresh._due_at.clear()

    def test_success_still_uses_fresh_status(self, monkeypatch):
        """The success path is untouched: it schedules from the status this very
        lookup wrote, and a terminal status still lands on inf."""
        tid = "9400111206200074888350"

        async def check(info):
            return await refresh._check_one(7, tid, info, True, asyncio.Semaphore(1))

        self._failed_lookup(monkeypatch, status="Out for Delivery",
                            detail="On the truck")
        info = {"name": "Box", "status": "In Transit"}
        refresh._due_at.clear()
        try:
            asyncio.run(check(info))
            assert info["status"] == "Out for Delivery"
            due_in = refresh._due_at[(7, tid)] - time.monotonic()
            # The OFD fast lane, read from config so this pin cannot drift from
            # the table (was a literal 1800 before the 2026-09-23 fast lane).
            assert config.OFD_FAST_PRO - 2 <= due_in <= config.OFD_FAST_PRO + 2, due_in
        finally:
            refresh._due_at.clear()

        self._failed_lookup(monkeypatch, status="Delivered", detail="Delivered")
        info = {"name": "Box", "status": "In Transit"}
        refresh._due_at.clear()
        try:
            asyncio.run(check(info))
            assert info["status"] == "Delivered"
            # A fresh weak-source claim is not final: the verification series
            # is scheduled instead of inf (2026-09-29 false-Delivered fix).
            assert refresh._due_at[(7, tid)] != float("inf")
            # A verified (or official-claimed) delivery stays terminal.
            info["delivery_verified"] = True
            refresh._due_at.clear()
            asyncio.run(check(info))
            assert refresh._due_at[(7, tid)] == float("inf")
        finally:
            refresh._due_at.clear()


class TestGlobalPing:
    """Notifications must carry no package data: chats (groups especially) keep
    everything forever, so details stay in the Mini App."""

    def test_ping_text_leaks_nothing(self, monkeypatch):
        monkeypatch.setattr(refresh.config, "MINI_APP_URL", "https://app.example")
        info = {"name": "AliExpress order", "status": "In Transit",
                "carrier_name": "China Post / ePacket", "detail": "Departed CN"}
        text, kb = refresh.build_global_ping(2)
        for secret in ("RR123456789CN", info["name"], info["status"],
                       info["carrier_name"], info["detail"]):
            assert secret not in text

    def test_button_opens_the_app_without_a_tracking_id(self, monkeypatch):
        monkeypatch.setattr(refresh.config, "MINI_APP_URL", "https://app.example")
        _, kb = refresh.build_global_ping(1)
        button = kb.inline_keyboard[0][0]
        assert button.url == "https://app.example"
        assert "RR" not in button.url

    def test_no_mini_app_url_means_no_button(self, monkeypatch):
        monkeypatch.setattr(refresh.config, "MINI_APP_URL", None)
        text, kb = refresh.build_global_ping(1)
        assert kb is None and text

    def test_count_is_the_only_number(self):
        assert "3 of your packages changed" in refresh.build_global_ping(3)[0]
        assert "One of your packages changed" in refresh.build_global_ping(1)[0]


class TestPingWorthiness:
    """Which changes deserve a ping — same mute rules as before, but the ping is
    now aggregated per chat instead of a per-package card."""

    TID = "RR123456789CN"

    def _check(self, monkeypatch, info, new_status, is_pro_tier=True):
        async def fake_fetch(tracking_id, *a, **k):
            return new_status, carriers.detect_carrier(tracking_id), "17TRACK", "detail"

        monkeypatch.setattr(carriers, "fetch_track_status", fake_fetch)
        monkeypatch.setattr(refresh, "_save_data_fn", lambda: None)
        storage.track_sessions.clear()
        storage.track_sessions[7] = {self.TID: info}
        refresh._due_at.clear()
        try:
            async def run_check():
                # Build the semaphore inside the loop: Python 3.9's Semaphore
                # grabs the current event loop at construction time.
                return await refresh._check_one(7, self.TID, info, is_pro_tier,
                                                asyncio.Semaphore(1))
            result = asyncio.run(run_check())
            due = refresh._due_at[(7, self.TID)]
        finally:
            storage.track_sessions.clear()
            refresh._due_at.clear()
        return result, info, due

    def test_changed_status_asks_for_a_ping(self, monkeypatch):
        info = {"name": "Box", "status": "Info Received", "carrier_name": "China Post"}
        result, info, _ = self._check(monkeypatch, info, "In Transit")
        assert result == 7
        assert info["status"] == "In Transit"

    def test_unchanged_status_is_silent(self, monkeypatch):
        info = {"name": "Box", "status": "In Transit", "carrier_name": "China Post"}
        result, _, _ = self._check(monkeypatch, info, "In Transit")
        assert result is None

    def test_delivered_schedules_a_verification_check(self, monkeypatch):
        info = {"name": "Box", "status": "Out for Delivery", "carrier_name": "USPS"}
        result, info, due = self._check(monkeypatch, info, "Delivered")
        assert result == 7
        # The claimant is recorded and, being a weak source, the terminal
        # state is not final: a bounded verification series runs.
        assert info["delivered_by"] == "17TRACK"
        assert due != float("inf") and due > 0

    def test_verified_delivery_schedules_never_again(self, monkeypatch):
        info = {"name": "Box", "status": "Out for Delivery", "carrier_name": "USPS",
                "delivery_verified": True}
        result, _, due = self._check(monkeypatch, info, "Delivered")
        assert result == 7 and due == float("inf")

    def test_muted_routine_change_is_silent(self, monkeypatch):
        info = {"name": "Box", "status": "In Transit", "carrier_name": "China Post",
                "muted": True}
        result, _, _ = self._check(monkeypatch, info, "Out for Delivery")
        assert result is None

    def test_muted_delivery_still_pings(self, monkeypatch):
        info = {"name": "Box", "status": "Out for Delivery", "carrier_name": "USPS",
                "muted": True}
        result, _, _ = self._check(monkeypatch, info, "Delivered")
        assert result == 7


class TestNoManualRefreshControls:
    """Manual refresh is gone: the global loop owns updating."""

    def test_package_keyboard_has_no_update_button(self):
        kb = main.build_package_keyboard("RR123456789CN", {"muted": False})
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert not any(c and c.startswith("update:") for c in callbacks)
        assert "rename:RR123456789CN" in callbacks  # the rest stays
        assert "remove:RR123456789CN" in callbacks

    def test_refresh_handlers_are_gone(self):
        for name in ("update_handler", "refresh_handler", "callback_update",
                     "_notify_status_change"):
            assert not hasattr(main, name), name



class TestStatusWriteGuard:
    """A lagging source must never un-deliver a package (17TRACK trails USPS).
    But the guard ranks witnesses: an equal-or-better source contradicting the
    claim is a RETRACTION (fresher truth from the same or a stronger tier),
    not the lagging echo this guard exists for — 2026-09-29: a false Delivered
    (parser furniture) froze forever because the guard had no ranks."""

    CARRIER = {"code": "usps", "name": "USPS", "url": ""}

    def test_delivered_is_never_downgraded(self):
        info = {"status": "Delivered", "delivered_by": "USPS API"}
        refresh._apply_lookup(info, "In Transit", self.CARRIER, "17TRACK", None)
        assert info["status"] == "Delivered"

    def test_weaker_source_still_cannot_downgrade(self):
        info = {"status": "Delivered", "delivered_by": "USPS Browser"}
        refresh._apply_lookup(info, "In Transit", self.CARRIER, "17TRACK", None)
        assert info["status"] == "Delivered"

    def test_equal_or_better_source_retracts_the_claim(self):
        refresh._pending_lost_notices.clear()
        info = {"status": "Delivered", "delivered_by": "USPS Browser", "name": "Box"}
        refresh._apply_lookup(info, "In Transit", self.CARRIER, "USPS API", None,
                              notify=(7, "9400111899560000000001", "Box"))
        assert info["status"] == "In Transit"
        assert info["delivered_by"] is None
        assert any(kind == "retraction"
                   for _, _, _, kind in refresh._pending_lost_notices), \
            "a retraction must tell the chat"
        refresh._pending_lost_notices.clear()

    def test_verification_attempts_are_bounded(self):
        info = {"status": "Delivered", "delivered_by": "17TRACK"}
        for _ in range(refresh.DELIVERY_VERIFY_ATTEMPTS):
            refresh._apply_lookup(info, "Delivered", self.CARRIER, "17TRACK", None)
        assert info["delivery_verified"] is True
        assert refresh._delivered_verify_secs(info) is None

    def test_official_confirmation_ends_verification(self):
        info = {"status": "Delivered", "delivered_by": "17TRACK"}
        refresh._apply_lookup(info, "Delivered", self.CARRIER, "USPS API", None)
        assert info["delivered_by"] == "USPS API"
        assert info["delivery_verified"] is True

    def test_unavailable_read_does_not_burn_verification(self):
        # A dead render proves nothing in either direction — it must not
        # consume the bounded series (or three slow pages would cement a
        # false Delivered).
        info = {"status": "Delivered", "delivered_by": "17TRACK"}
        refresh._apply_lookup(info, "Unavailable", self.CARRIER, "17TRACK", None)
        assert info["status"] == "Delivered"
        assert info.get("delivery_verify_attempts", 0) == 0
        assert refresh._delivered_verify_secs(info) == refresh.DELIVERY_VERIFY_SECS

    def test_unclaimed_legacy_delivery_is_verification_eligible(self):
        # Pre-fix records carry no delivered_by: unknown authority (0) means
        # the claim gets verified via the tier order, not trusted or frozen.
        assert refresh._delivered_verify_secs({"status": "Delivered"}) is not None

    def test_poisoned_verified_flag_does_not_cement_unknown_claimant(self):
        """The banner-era records confirmed Delivered by reading the prediction
        banner three times with the buggy classifier — their verified flag and
        burned attempts carry no weight without a recorded claimant. The fixed
        classifier gets one honest series."""
        poisoned = {"status": "Delivered", "delivery_verified": True,
                    "delivery_verify_attempts": refresh.DELIVERY_VERIFY_ATTEMPTS}
        assert refresh._delivered_verify_secs(poisoned) == refresh.DELIVERY_VERIFY_SECS
        # But once a claimant IS recorded, the caps hold (state terminates).
        claimed = dict(poisoned, delivered_by="17TRACK")
        assert refresh._delivered_verify_secs(claimed) is None

    def test_official_verified_stays_terminal(self):
        assert refresh._delivered_verify_secs(
            {"status": "Delivered", "delivered_by": "USPS API",
             "delivery_verified": True}) is None

    def test_force_still_overrides(self):
        info = {"status": "Delivered", "delivered_by": "USPS API"}
        refresh._apply_lookup(info, "In Transit", self.CARRIER, "USPS Browser", None, force=True)
        assert info["status"] == "In Transit"

    def test_normal_transition_still_applies(self):
        info = {"status": "In Transit"}
        refresh._apply_lookup(info, "Delivered", self.CARRIER, "17TRACK", None)
        assert info["status"] == "Delivered"
        assert info["delivered_by"] == "17TRACK"


class TestStaleFlag:
    """A failed lookup must not be presented as fresh news."""

    CARRIER = {"code": "usps", "name": "USPS", "url": ""}

    def test_failed_lookup_is_flagged(self):
        info = {"status": "In Transit"}
        refresh._apply_lookup(info, "Unavailable", self.CARRIER, "None", None)
        assert info["last_lookup_failed"] is True
        assert info["status"] == "In Transit"   # Unavailable never clobbers

    def test_success_clears_the_flag(self):
        info = {"status": "In Transit", "last_lookup_failed": True}
        refresh._apply_lookup(info, "Out for Delivery", self.CARRIER, "17TRACK", None)
        assert info["last_lookup_failed"] is False

    def test_unchanged_status_still_refreshes_the_flag(self):
        # changed_only=True skips the status write, but the flag describes the
        # ATTEMPT, so it has to be updated on every lookup.
        info = {"status": "In Transit", "last_lookup_failed": True}
        refresh._apply_lookup(info, "In Transit", self.CARRIER, "17TRACK", None,
                              changed_only=True)
        assert info["last_lookup_failed"] is False

    def test_payload_exposes_stale_and_defaults_to_false(self):
        tid = "9400111899560000000001"
        fresh = main._package_payload(tid, {"status": "In Transit"})
        stale = main._package_payload(tid, {"status": "In Transit", "last_lookup_failed": True})
        assert fresh["stale"] is False   # legacy records read as not stale
        assert stale["stale"] is True


def test_ofd_keeps_its_fast_lane_under_quota_pressure(monkeypatch):
    """A lane is only as fast as the harshest condition it survives: OFD stays in
    _CADENCE_CRITICAL, so the quota stretch cannot slow a delivery day back down.
    Change-3 guard — if a later edit drops OFD from that set, this fails."""
    assert "Out for Delivery" in refresh._CADENCE_CRITICAL
    assert refresh.CADENCE["Out for Delivery"] == (config.OFD_FAST_PRO,
                                                   config.OFD_FAST_FREE)
    monkeypatch.setattr(carriers, "quota_pct", lambda: carriers.TRACK17_HARD_PCT + 1)
    assert refresh.cadence_seconds("Out for Delivery", True) == config.OFD_FAST_PRO
    assert refresh.cadence_seconds("Out for Delivery", False) == config.OFD_FAST_FREE
    # ...and the pressure is real: a non-critical status does stretch.
    assert refresh.cadence_seconds("Pending", True) == int(
        refresh.CADENCE["Pending"][0] * refresh.CADENCE_QUOTA_STRETCH)


class TestUspsBrowserSweep:
    """Headless sweep: the only tier that can move a USPS number when no official"""
    """USPS API is configured (refresh._sweep_browser)."""

    TID = "9400111899560000000001"
    UPS = "1Z1E852G0310312712"
    CHAT = 7

    def _install(self, monkeypatch, **extra):
        saved = {k: dict(v) for k, v in storage.track_sessions.items()}
        storage.track_sessions.clear()
        info = {"name": "Box", "status": "Pending"}
        info.update(extra)
        storage.track_sessions[self.CHAT] = {self.TID: info}
        monkeypatch.setattr(refresh, "_due_at", {})
        monkeypatch.setattr(config, "USPS_BROWSER_TRACK", True)
        monkeypatch.setattr(config, "USPS_API_USERID", None)
        monkeypatch.setattr(refresh.usps_api, "enabled", lambda: False)
        import usps_browser
        monkeypatch.setattr(usps_browser, "is_throttled",
                            lambda slug="usps": False)
        return saved

    def _browser(self, monkeypatch, mapping):
        import usps_browser
        calls = []
        def fake(slug, ids, timeout_budget=120):
            calls.append((slug, list(ids)))
            return {t: mapping[t] for t in ids if t in mapping}
        monkeypatch.setattr(usps_browser, "get_browser_statuses", fake)
        return calls

    @staticmethod
    def _restore(saved):
        storage.track_sessions.clear()
        storage.track_sessions.update(saved)

    def test_sweep_persists_counters_for_the_status_block(self, monkeypatch, tmp_path):
        """run.sh --status samples sweep_state.json (a fresh `import refresh`
        cannot see the running bot's memory), so every sweep must leave the
        counters on disk."""
        state_file = tmp_path / "sweep_state.json"
        monkeypatch.setattr(config, "SWEEP_STATE_FILE", state_file)
        saved = self._install(monkeypatch)
        calls = self._browser(monkeypatch, {self.TID: ("Out for Delivery", "out")})
        try:
            asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
        finally:
            self._restore(saved)
        import json as _json
        data = _json.loads(state_file.read_text())
        assert data["read"] == 1
        assert data["changed"] == 1
        assert data["at"] > 0
        assert oct(state_file.stat().st_mode)[-3:] == "600", \
            "counters only, but this is still state: 0600 at rest"
        assert not list(tmp_path.glob("*.tmp")), "the atomic write must not leak tmp"

    def test_due_number_is_read_in_one_session(self, monkeypatch):
        saved = self._install(monkeypatch)
        calls = self._browser(monkeypatch, {self.TID: ("Out for Delivery", "out")})
        try:
            pings = asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
            info = dict(storage.track_sessions[self.CHAT][self.TID])
        finally:
            self._restore(saved)
        assert calls == [("usps", [self.TID])]           # exactly one browser session
        assert info["status"] == "Out for Delivery"
        assert info["source"] == "USPS Browser"
        assert info["last_lookup_failed"] is False
        assert pings == {self.CHAT: 1}         # a real change pings once

    def test_ofd_numbers_sweep_first_and_survive_the_cap(self, monkeypatch):
        """Delivery-day priority: with more numbers due than the sweep cap allows,
        the OFD ones must take the batch — the cap cannot be allowed to cut the
        parcels closest to arriving."""
        saved = self._install(monkeypatch)
        monkeypatch.setattr(config, "USPS_SWEEP_MAX", 2)
        waiting = [f"94001118995600000000{i:02d}" for i in range(3)]
        ofd = ["9400111899560000000099", "9400111899560000000098"]
        storage.track_sessions[self.CHAT] = {
            t: {"name": "Box", "status": "In Transit"} for t in waiting}
        for t in ofd:
            storage.track_sessions[self.CHAT][t] = {"name": "OFD box",
                                                    "status": "Out for Delivery"}
        calls = self._browser(monkeypatch, {})
        try:
            asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
        finally:
            self._restore(saved)

        assert len(calls) == 1, "one session, as before"
        assert calls[0][1] == ofd, "the OFD numbers must take the capped batch"

    def test_an_ofd_number_reschedules_on_the_fast_lane(self, monkeypatch):
        """The lane's other half: after a sweep a Pro OFD package is due again in
        10 minutes and a free one in 30 — not the old 30/60."""
        for is_pro_tier, expected in ((False, config.OFD_FAST_FREE),
                                      (True, config.OFD_FAST_PRO)):
            saved = self._install(monkeypatch)
            monkeypatch.setattr(refresh.pro, "is_pro", lambda chat_id: is_pro_tier)
            self._browser(monkeypatch, {self.TID: ("Out for Delivery", "on it")})
            now = time.monotonic() + 3600
            try:
                asyncio.run(refresh._sweep_browser(now))
                wait = refresh._due_at[(self.CHAT, self.TID)] - now
            finally:
                self._restore(saved)
            assert abs(wait - expected) < 0.001, \
                f"pro={is_pro_tier} waited {wait}s"

    def test_unread_number_is_flagged_and_silent(self, monkeypatch):
        saved = self._install(monkeypatch)
        self._browser(monkeypatch, {})
        try:
            pings = asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
            info = dict(storage.track_sessions[self.CHAT][self.TID])
        finally:
            self._restore(saved)
        assert pings == {}
        assert info["status"] == "Pending"     # no invented status
        assert info["last_lookup_failed"] is True

    def test_delivered_is_never_swept(self, monkeypatch):
        """A VERIFIED delivery (official claimant) is terminal — the sweep must
        leave it alone. (Poisoned-era claims without a recorded claimant are
        re-verified instead — see the companion test.)"""
        saved = self._install(monkeypatch, status="Delivered")
        storage.track_sessions[self.CHAT][self.TID]["delivered_by"] = "USPS API"
        storage.track_sessions[self.CHAT][self.TID]["delivery_verified"] = True
        calls = self._browser(monkeypatch, {})
        try:
            asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
        finally:
            self._restore(saved)
        assert calls == []

    def test_unverified_delivery_is_swept_for_verification(self, monkeypatch):
        """The sweep now admits terminal packages whose delivery claim is
        unverified and weakly sourced — the verification pass that keeps a
        false Delivered correctable."""
        saved = self._install(monkeypatch, status="Delivered")
        calls = self._browser(monkeypatch, {self.TID: ("In Transit", "moving")})
        try:
            asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
            info = dict(storage.track_sessions[self.CHAT][self.TID])
        finally:
            self._restore(saved)
        assert calls == [("usps", [self.TID])]
        assert info["status"] == "In Transit"   # the claim was retracted
        assert info.get("delivered_by") is None

    def test_not_due_is_left_to_the_cadence(self, monkeypatch):
        saved = self._install(monkeypatch)
        refresh._due_at[(self.CHAT, self.TID)] = time.monotonic() + 600
        calls = self._browser(monkeypatch, {})
        try:
            asyncio.run(refresh._sweep_browser(time.monotonic()))
        finally:
            self._restore(saved)
        assert calls == []

    def test_non_usps_numbers_are_ignored(self, monkeypatch):
        """In a free chat the browser owns UPS too: a healthy UPS number is
        swept exactly like the USPS ones, in the same sweep pass."""
        saved = self._install(monkeypatch)
        storage.track_sessions[self.CHAT][self.UPS] = {"name": "UPS", "status": "In Transit"}
        calls = self._browser(monkeypatch, {})
        try:
            asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
            ups_status = storage.track_sessions[self.CHAT][self.UPS]["status"]
        finally:
            self._restore(saved)
        assert ("usps", [self.TID]) in calls
        assert ("ups", [self.UPS]) in calls
        assert ups_status == "In Transit"

    def test_official_tier_wins_unless_17track_refused_the_label(self, monkeypatch):
        saved = self._install(monkeypatch)
        monkeypatch.setattr(refresh.usps_api, "enabled", lambda: True)
        calls = self._browser(monkeypatch, {})
        try:
            asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
            assert calls == []                 # an official API is the better tool
            storage.track_sessions[self.CHAT][self.TID]["register_blocked"] = True
            asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
        finally:
            self._restore(saved)
        assert calls == [("usps", [self.TID])]           # but a refused label still needs it

    def test_browser_disabled_is_a_noop(self, monkeypatch):
        saved = self._install(monkeypatch)
        monkeypatch.setattr(config, "USPS_BROWSER_TRACK", False)
        calls = self._browser(monkeypatch, {})
        try:
            assert asyncio.run(refresh._sweep_browser(time.monotonic() + 3600)) == {}
        finally:
            self._restore(saved)
        assert calls == []

    def test_throttled_browser_skips_cleanly(self, monkeypatch):
        saved = self._install(monkeypatch)
        import usps_browser
        monkeypatch.setattr(usps_browser, "is_throttled",
                            lambda slug="usps": True)
        calls = self._browser(monkeypatch, {})
        try:
            assert asyncio.run(refresh._sweep_browser(time.monotonic() + 3600)) == {}
        finally:
            self._restore(saved)
        assert calls == []

    def test_sweep_reschedules_the_number(self, monkeypatch):
        saved = self._install(monkeypatch)
        self._browser(monkeypatch, {self.TID: ("In Transit", "moving")})
        now = time.monotonic()
        try:
            asyncio.run(refresh._sweep_browser(now))
            due = refresh._due_at[(self.CHAT, self.TID)]
        finally:
            self._restore(saved)
        assert due > now                       # the generic loop will not repeat it

    def test_failed_ups_joins_the_sweep(self, monkeypatch):
        """17TRACK failed this UPS number (last_lookup_failed): the sweep gives
        the free browser tier a shot instead of leaving it to paid calls."""
        saved = self._install(monkeypatch)
        # Isolate: the sweep is called with a +3600s now_mono, so the USPS
        # number must be due beyond THAT to stay out of the run.
        refresh._due_at[(self.CHAT, self.TID)] = time.monotonic() + 7200
        storage.track_sessions[self.CHAT][self.UPS] = {
            "name": "UPS", "status": "In Transit", "last_lookup_failed": True}
        calls = self._browser(monkeypatch, {self.UPS: ("Delivered", "done")})
        try:
            pings = asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
            info = dict(storage.track_sessions[self.CHAT][self.UPS])
        finally:
            self._restore(saved)
        assert ("ups", [self.UPS]) in calls
        assert info["status"] == "Delivered"
        assert info["source"] == "UPS Browser"
        assert info["last_lookup_failed"] is False   # a real read clears the flag
        assert pings == {self.CHAT: 1}

    def test_free_ups_is_swept_as_its_primary_tier(self, monkeypatch):
        """In a free chat the browser IS the UPS tier (17TRACK is Pro-only), so
        healthy UPS numbers are swept on the due schedule like USPS ones."""
        saved = self._install(monkeypatch)
        refresh._due_at[(self.CHAT, self.TID)] = time.monotonic() + 7200  # isolate
        storage.track_sessions[self.CHAT][self.UPS] = {
            "name": "UPS", "status": "In Transit"}
        calls = self._browser(monkeypatch, {self.UPS: ("In Transit", "moving")})
        try:
            pings = asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
            info = dict(storage.track_sessions[self.CHAT][self.UPS])
        finally:
            self._restore(saved)
        assert ("ups", [self.UPS]) in calls
        assert info["status"] == "In Transit"
        assert pings == {}                     # unchanged status → silent

    def test_pro_ups_stays_in_the_loop(self, monkeypatch):
        """Pro chats read UPS in the loop — on the carrier's own page first since
        2026-09-23 — and the sweep leaves their healthy numbers alone."""
        saved = self._install(monkeypatch)
        monkeypatch.setattr(pro, "is_pro", lambda c: True)
        refresh._due_at[(self.CHAT, self.TID)] = time.monotonic() + 7200  # isolate
        storage.track_sessions[self.CHAT][self.UPS] = {
            "name": "UPS", "status": "In Transit"}
        calls = self._browser(monkeypatch, {})
        try:
            asyncio.run(refresh._sweep_browser(time.monotonic() + 3600))
        finally:
            self._restore(saved)
        assert calls == [], "a Pro chat's UPS number is 17TRACK's to answer"


class TestBrowserOnlyDecision:
    """Numbers the loop must leave to the sweep."""

    """Ordering is the point: the loop reschedules whatever it polls, so polling a"""
    """browser-only number here hides it from the sweep later in the same cycle."""

    TID = "9400111899560000000001"

    def test_usps_with_no_official_tier_is_browser_only(self, monkeypatch):
        monkeypatch.setattr(config, "USPS_BROWSER_TRACK", True)
        monkeypatch.setattr(config, "USPS_API_USERID", None)
        monkeypatch.setattr(refresh.usps_api, "enabled", lambda: False)
        assert refresh._browser_only(self.TID, {}) is True

    def test_official_tier_means_the_loop_should_poll_it(self, monkeypatch):
        monkeypatch.setattr(config, "USPS_BROWSER_TRACK", True)
        monkeypatch.setattr(config, "USPS_API_USERID", "userid")
        assert refresh._browser_only(self.TID, {}) is False

    def test_official_v3_tier_means_the_loop_should_poll_it(self, monkeypatch):
        monkeypatch.setattr(config, "USPS_BROWSER_TRACK", True)
        monkeypatch.setattr(config, "USPS_API_USERID", None)
        monkeypatch.setattr(refresh.usps_api, "enabled", lambda: True)
        assert refresh._browser_only(self.TID, {}) is False

    def test_browser_disabled_means_the_loop_keeps_polling(self, monkeypatch):
        monkeypatch.setattr(config, "USPS_BROWSER_TRACK", False)
        assert refresh._browser_only(self.TID, {}) is False

    def test_non_usps_is_never_browser_only(self, monkeypatch):
        monkeypatch.setattr(config, "USPS_BROWSER_TRACK", True)
        monkeypatch.setattr(config, "USPS_API_USERID", None)
        monkeypatch.setattr(refresh.usps_api, "enabled", lambda: False)
        assert refresh._browser_only("1Z1E852G0310312712", {}) is False


class TestStatusReport:
    """build_status_text is the whole /status body — pinned here so the report
    can't quietly lose the tier split, the quota line or the paid-call count."""

    def test_reports_tiers_quota_spend_and_mix(self, monkeypatch):
        fake_tier = {"pro": {"ok": 2, "fail": 0}, "free": {"ok": 0, "fail": 1},
                     "unknown": {"ok": 0, "fail": 0}}
        monkeypatch.setattr(carriers, "LOOKUP_TIER", fake_tier)
        monkeypatch.setattr(carriers, "_quota_state",
                            {"used": 11, "total": 200, "at": time.time()})
        fake_sessions = {
            1: {"RR123456789CN": {"name": "p", "status": "In Transit",
                                  "needs_pro": False}},
            2: {"9400111206200074888350": {"name": "q", "status": "Pending",
                                           "needs_pro": True}},
        }
        monkeypatch.setattr(main, "track_sessions", fake_sessions)
        text = main.build_status_text()
        assert "Lookups (ok / fail)" in text
        assert "by tier — pro: 2 ok / 0 fail · free: 0 ok / 1 fail" in text
        assert "17TRACK quota: 11/200" in text and "healthy" in text
        assert "Ship24 today:" in text and "paid calls" in text
        # Pending counts as in transit on the mix line (the JS group() does
        # the same), so two Pendings + one gated package read exactly so.
        assert "2 in transit" in text and "1 paused (Pro)" in text

    def test_quota_line_reflects_pressure_thresholds(self, monkeypatch):
        monkeypatch.setattr(carriers, "_quota_state",
                            {"used": 180, "total": 200, "at": time.time()})
        text = main.build_status_text()
        assert "17TRACK quota: 180/200 (90.0%)" in text
        assert "critical" in text

    def test_report_never_contains_tracking_numbers(self, monkeypatch):
        """Numbers in a status report would defeat the bot's masking promise."""
        monkeypatch.setattr(main, "track_sessions", {
            1: {"9400111206200074888350": {"name": "q", "status": "Pending"}},
        })
        assert "9400111206200074888350" not in main.build_status_text()


def test_ping_stays_data_free_status_vocabulary():
    """The /privacy promise says pings carry 'No tracking number, name, status
    or carrier'. Richer pings are welcome ONLY after that promise is rewritten
    — this test keeps the two from drifting apart silently."""
    for count in (1, 2, 3):
        text, _kb = refresh.build_global_ping(count)
        for word in ("Delivered", "In Transit", "Exception", "Out for Delivery",
                     "Info Received", "Available for Pickup", "Delivery Attempted",
                     "USPS", "UPS", "FedEx", "DHL", "17TRACK"):
            assert word not in text
        assert not re.search(r"\d{8,}", text), "no tracking-number-like runs"
