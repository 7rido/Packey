"""Screenshot add path: OCR locally, then the SAME pipeline /track uses.

Two rules make this safe, and both are pinned here: the image is deleted on
every path (success, OCR failure, timeout), and neither the image nor the
recognised text ever reaches a log — a shipping confirmation carries a name and
an address. Number extraction is the existing parser (ui.parse_track_args with
has_command=False), so OCR noise dies exactly where /track's junk dies, and the
one other thing kept from the text is the label's own ship-to name
(ui.extract_recipient_name), read strictly enough that an address, a ZIP or a
carrier line cannot pass for a person.
"""
import asyncio
import inspect
import logging
import shutil
from pathlib import Path

import pytest

import main
import storage
import ui

NUMBER = "9400111206200074888350"
ADDR = "12 Elm Street, Springfield"           # planted: must never reach a log
OCR_TEXT = (f"USPS\nTracking: {NUMBER}\n"
            f"Deliver to: Jane Doe\n{ADDR}\nThank you for your order!")
CHAT = 7001

# A UPS label as OCR actually returns it (the pages that failed live): a stray
# quote glyph in the noise, the address with its ZIP+4, and the number line
# printed in 2-char groups. Both the quote and the ZIP used to win.
LABEL_NUMBER = "1Z1E852G0325015793"
ROUTING = "420115504703"      # the label's second barcode: "420" + ZIP+4
LABEL_OCR = ('SHIP TO "J DOE\n477 MARINE CENTER DR\n'
             "HEMPSTEAD NY 12345-6789\nUPS GROUND\n"
             "TRACKING #: 1Z 1E8 52G 03 2501 5793\nBILLING: 3RD PARTY")


@pytest.fixture(autouse=True)
def _clean_sessions():
    storage.track_sessions.clear()
    yield
    storage.track_sessions.clear()


@pytest.fixture
def ocr_fallback(monkeypatch):
    """Restore the stashed OCR fallback for one test: the one-line reversal.

    Shipping behaviour is barcode-only (main._BARCODE_ONLY), which leaves the
    retained OCR code unreachable — so the tests that still pin it say so out
    loud by asking for this fixture.
    """
    monkeypatch.setattr(main, "_BARCODE_ONLY", False)


@pytest.fixture(autouse=True)
def _no_real_barcode_binary(monkeypatch):
    """No test may shell out to zbar: the seam is stubbed here, and the one test
    that wants the real decoder goes through _REAL_DECODE."""
    async def fake_decode(path):
        return []
    monkeypatch.setattr(main, "_decode_barcodes", fake_decode)


class _Bot:
    """Bot stand-in: 'downloads' by writing the destination file."""

    def __init__(self):
        self.downloaded = []

    async def download(self, file, destination=None, **kw):
        self.downloaded.append(str(destination))
        Path(destination).write_bytes(b"fake-jpeg-bytes")
        return None


class _Sent:
    """A message the bot sent — the confirm prompt, or whatever follows it."""

    def __init__(self, chat, text, markup=None, message_id=1):
        self.chat = chat
        self.message_id = message_id
        self.text = text
        self.reply_markup = markup
        self.edited = None

    async def answer(self, text, **kw):
        """A confirmed add talks through its wrapper, so its replies land here."""
        return _Sent(self.chat, text, kw.get("reply_markup"), self.message_id + 1)

    async def edit_text(self, text, **kw):
        self.edited = text
        return self


class _Callback:
    """A tap on that prompt."""

    def __init__(self, message, data, uid=5):
        self.message = message
        self.data = data
        self.from_user = type("U", (), {"id": uid, "first_name": "F"})()
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


def _prompt_for(msg):
    """The last message the bot sent that carries buttons (the confirm prompt)."""
    return [s for s in msg.sent if s.reply_markup is not None][-1]


def _tap_confirm(msg, uid=5):
    """Tap Confirm on the prompt the user actually got, data and all."""
    prompt = _prompt_for(msg)
    data = prompt.reply_markup.inline_keyboard[0][0].callback_data
    callback = _Callback(prompt, data, uid=uid)
    asyncio.run(main.callback_confirm_add(callback))
    return callback


class _PhotoMsg:
    """Message stand-in for the screenshot handler."""

    def __init__(self, chat_id=CHAT, caption=None, sizes=2):
        self.chat = type("C", (), {"id": chat_id, "type": "private"})()
        self.from_user = type("U", (), {"id": 5, "first_name": "F"})()
        self.message_id = 42          # real Messages always carry one
        self.caption = caption
        self.photo = [object()] * sizes      # photo[-1] is the largest
        self.texts = []
        self.sent = []
        self.bot = _Bot()

    async def answer(self, text, **kw):
        self.texts.append(text)
        sent = _Sent(self.chat, text, kw.get("reply_markup"))
        self.sent.append(sent)
        return sent

    async def delete(self):
        pass


def _stub_pipeline(monkeypatch):
    """Stub the shared pipeline's network + card send; return what they saw."""
    added = []

    async def fake_register(tracking_id, is_pro=None, chat_id=None):
        added.append((tracking_id, chat_id))
        return ({"name": "Box", "status": "Pending", "carrier": "usps"},
                "registered")

    monkeypatch.setattr(main, "_register_and_fetch", fake_register)

    sent = []

    async def fake_ephemeral(message, text, **kw):
        sent.append(text)
        return type("S", (), {"message_id": 1})()

    monkeypatch.setattr(main, "send_ephemeral", fake_ephemeral)
    return added, sent


def _stub_tesseract(monkeypatch, text=OCR_TEXT, seen=None, boom=False, psms=None,
                    by_psm=None, barcodes=None, barcode_calls=None):
    """Pretend the binary exists and OCRs to `text` (or explodes).

    `by_psm` gives each segmentation mode its own text, for the multi-mode tests;
    `psms` records which modes were actually attempted. `barcodes` is the payload
    list the barcode step decodes to ([] = "no barcode on this image"), and
    `barcode_calls` records the image paths that step was handed.
    """
    monkeypatch.setattr(main.shutil, "which", lambda name: "/usr/bin/tesseract")

    async def fake_ocr_file(path, psm=6):
        if seen is not None:
            seen.append(Path(path))
        if psms is not None:
            psms.append(psm)
        if boom:
            raise RuntimeError("ocr exploded")
        if by_psm is not None:
            return by_psm.get(psm, "")
        return text

    monkeypatch.setattr(main, "_ocr_file", fake_ocr_file)

    async def fake_decode(path):
        if barcode_calls is not None:
            barcode_calls.append(Path(path))
        return list(barcodes or [])

    monkeypatch.setattr(main, "_decode_barcodes", fake_decode)



def test_ocr_text_is_parsed_by_the_existing_parser():
    """One parser, two entry points: the command word is what it skips."""
    ids, _name = ui.parse_track_args(OCR_TEXT, has_command=False)
    assert ids == [NUMBER], "the number buried in the confirmation must be found"
    assert ui.parse_track_args(f"/track {NUMBER}")[0] == [NUMBER]
    assert ui.parse_track_args("Thank you for your order!",
                               has_command=False)[0] == [], "junk finds nothing"


def test_a_screenshot_adds_the_package_through_the_shared_path(monkeypatch,
                                                               ocr_fallback):
    added, sent = _stub_pipeline(monkeypatch)
    seen = []
    _stub_tesseract(monkeypatch, seen=seen)

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))

    assert added == [], "a photo must not register anything before the tap"
    _tap_confirm(msg)

    assert added == [(NUMBER, CHAT)], "the shared add pipeline must run"
    assert any("Package added" in t for t in sent), "the same card as /track"
    assert msg.bot.downloaded, "the photo must be downloaded"
    assert NUMBER in storage.track_sessions[CHAT], "and stored for that chat"


def test_a_caption_with_a_number_skips_ocr_entirely(monkeypatch):
    """A forwarded confirmation usually carries the number verbatim — no OCR,
    and no need for the binary at all."""
    added, _sent = _stub_pipeline(monkeypatch)
    called = []

    async def fake_ocr_file(path, psm=6):
        called.append(path)
        return OCR_TEXT

    monkeypatch.setattr(main, "_ocr_file", fake_ocr_file)
    monkeypatch.setattr(main.shutil, "which", lambda name: None)   # no tesseract

    msg = _PhotoMsg(caption=f"my order {NUMBER} arrives friday")
    asyncio.run(main.photo_handler(msg))

    assert called == [], "the caption already had the number"
    assert added == [(NUMBER, CHAT)]
    assert msg.bot.downloaded == [], "nothing to download either"


def test_missing_barcode_reader_answers_with_the_setup_line(monkeypatch):
    """No reader installed: say the one-time setup line and do nothing else."""
    added, _sent = _stub_pipeline(monkeypatch)
    monkeypatch.setattr(main.shutil, "which", lambda name: None)

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))          # must not raise

    assert any("brew install zbar" in t for t in _sent)
    assert not any("tesseract" in t for t in _sent), \
        "the setup line must not name a tool this path never calls"
    assert added == [], "nothing may be added"
    assert msg.bot.downloaded == [], "and no download is attempted"


def test_the_image_is_deleted_and_the_text_never_logged(monkeypatch, caplog,
                                                        ocr_fallback):
    """The screenshot is a user's document: gone on success, gone on failure,
    and its content never reaches a log."""
    added, _sent = _stub_pipeline(monkeypatch)
    seen = []
    _stub_tesseract(monkeypatch, seen=seen)

    with caplog.at_level(logging.DEBUG, logger="tracker"):
        asyncio.run(main.photo_handler(_PhotoMsg()))

    assert seen and not seen[0].exists(), "the image must be deleted after OCR"
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert ADDR not in logs, "an address from the screenshot reached the log"
    assert "Jane Doe" not in logs, "a name from the screenshot reached the log"
    assert NUMBER not in logs, "a raw tracking number reached the log"

    # ...and the same when OCR blows up.
    seen.clear()
    _stub_tesseract(monkeypatch, seen=seen, boom=True)
    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))

    assert seen and not seen[0].exists(), "the image must be deleted on failure too"
    assert any("Couldn't find a tracking number" in t for t in _sent)
    assert added == [], "a failed read must add nothing"


def test_ocr_timeout_is_swallowed_and_the_process_killed(monkeypatch):
    """A pathological image must not pin the handler: the 20 s cap kills it."""
    class _Proc:
        killed = False

        async def communicate(self):
            raise AssertionError("the timeout must fire first")

        def kill(self):
            self.killed = True

        async def wait(self):
            return 0

    proc = _Proc()

    async def fake_exec(*args, **kw):
        assert args[0] == "tesseract" and "stdout" in args, args
        return proc

    async def fake_wait_for(awaitable, timeout=None):
        assert timeout == 20, "the OCR cap must stay at 20 s"
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(main.asyncio, "wait_for", fake_wait_for)

    assert asyncio.run(main._ocr_file("/tmp/whatever.jpg")) == ""
    assert proc.killed, "a stuck tesseract must be killed, not left running"


def test_ocr_start_failure_is_swallowed(monkeypatch):
    """No binary at exec time (it was removed mid-flight) must not raise."""
    async def boom(*args, **kw):
        raise OSError("no such file or directory")

    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", boom)
    assert asyncio.run(main._ocr_file("/tmp/whatever.jpg")) == ""


def test_the_photo_route_is_registered():
    """A handler nothing routes to is dead code."""
    assert main.photo_handler in [h.callback for h in main.router.message.handlers]


def test_a_label_photo_is_read_in_several_segmentation_modes(monkeypatch,
                                                             ocr_fallback):
    """The live failure: --psm 6 is tuned for a uniform text block (a
    screenshot), and a PHOTO of a label has columns and boxes, so 6 came back
    without the number. Sparse mode (11) found it, and the add must go through."""
    added, sent = _stub_pipeline(monkeypatch)
    psms = []
    _stub_tesseract(monkeypatch, psms=psms,
                    by_psm={6: "UPS SHIPPING LABEL\nSHIP TO J DOE\nNO NUMBER HERE",
                            11: OCR_TEXT,
                            3: ""})

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))

    assert psms == list(main._OCR_PSMS), f"all modes must be tried: {psms}"
    assert added == [], "the read waits for the user's tap"
    _tap_confirm(msg)
    assert added == [(NUMBER, CHAT)], "the mode that read it must be used"
    assert any("Package added" in t for t in sent)


def test_two_modes_cannot_fuse_into_a_number(monkeypatch, ocr_fallback):
    """Each mode's text is parsed alone on purpose: half the number in one mode
    and half in another must never be stitched into a number that was never
    printed. (The tail fragment still reads as a number by itself — that is the
    pre-existing all-digits rule — so the assertion is that the FUSED number is
    absent and only the fragment is used.)"""
    added, _sent = _stub_pipeline(monkeypatch)
    _stub_tesseract(monkeypatch, by_psm={6: "TRACKING #: 1Z 1E8",
                                         11: "52G 03 2501 5793",
                                         3: ""})

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))
    _tap_confirm(msg)

    got = [tid for tid, _chat in added]
    # The invariant: what gets added equals what each mode yields ALONE. The
    # fragment is legitimately a number by itself (that is the pre-existing
    # all-digits rule, plus the one-swap correction for the G), so the assertion
    # is about the fused whole number being absent and nothing else appearing.
    alone, _noise = ui.parse_track_args("52G 03 2501 5793", has_command=False)
    assert "1Z1E852G0325015793" not in got, "two readings were fused into a number"
    assert got == alone, got


def test_the_label_photo_that_failed_live_now_adds_the_number(monkeypatch,
                                                              ocr_fallback):
    """End to end on the shape that failed: OCR hands back the number line
    correctly, but the same page also carries a stray quote glyph and the
    destination ZIP+4. The quote swallowed the number's line and the ZIP was
    stored as the package; the label now yields exactly the printed number."""
    added, _sent = _stub_pipeline(monkeypatch)
    _stub_tesseract(monkeypatch, by_psm={6: LABEL_OCR, 11: LABEL_OCR,
                                         3: LABEL_OCR})

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))
    _tap_confirm(msg)

    assert [tid for tid, _chat in added] == [LABEL_NUMBER], added


# A clean ship-to block: the shape a UPS/FedEx label prints, plus the number
# line this file's other tests use. The package should take the recipient's name.
NAME_OCR = ("UPS GROUND\nSHIP TO:\nJANE DOE\n477 MARINE CENTER DR\n"
            "HEMPSTEAD NY 12345-6789\nTRACKING #: 1Z 1E8 52G 03 2501 5793\n"
            "BILLING: 3RD PARTY")
# The same page with no name line at all: nothing may be guessed from it.
NAMELESS_OCR = ("UPS GROUND\n477 MARINE CENTER DR\n"
                "TRACKING #: 1Z 1E8 52G 03 2501 5793\nBILLING: 3RD PARTY")


class TestRecipientName:
    """A label's ship-to block names the package — nothing else may.

    The name is the only thing kept from the recognised text besides the
    numbers, so the rules that keep an address, a ZIP or a carrier line from
    passing for a person are pinned here, one level below the handler.
    """

    def test_a_label_photo_names_the_package_after_its_recipient(self, monkeypatch,
                                                                 ocr_fallback):
        added, _sent = _stub_pipeline(monkeypatch)
        _stub_tesseract(monkeypatch, by_psm={6: NAME_OCR, 11: NAME_OCR, 3: ""})

        msg = _PhotoMsg()
        asyncio.run(main.photo_handler(msg))
        _tap_confirm(msg)

        assert [tid for tid, _chat in added] == [LABEL_NUMBER]
        assert storage.track_sessions[CHAT][LABEL_NUMBER]["name"] == "Jane Doe", \
            "the name read from the label must survive the confirm wait"

    def test_a_caption_name_still_wins(self, monkeypatch):
        """An explicit name is the user's choice, so the label is not read at
        all (the OCR stub would blow up if it were)."""
        _stub_pipeline(monkeypatch)
        _stub_tesseract(monkeypatch, boom=True)

        msg = _PhotoMsg(caption=f"{LABEL_NUMBER} Mom's gift")
        asyncio.run(main.photo_handler(msg))

        assert storage.track_sessions[CHAT][LABEL_NUMBER]["name"] == "Mom's gift"

    def test_a_label_with_no_name_line_keeps_the_default(self, monkeypatch,
                                                         ocr_fallback):
        """No name is better than a guessed one: the old default stands."""
        _stub_pipeline(monkeypatch)
        _stub_tesseract(monkeypatch, by_psm={6: NAMELESS_OCR, 11: "", 3: ""})

        msg = _PhotoMsg()
        asyncio.run(main.photo_handler(msg))
        _tap_confirm(msg)

        assert storage.track_sessions[CHAT][LABEL_NUMBER]["name"] == "F's package"

    def test_both_ship_to_block_shapes_are_read(self):
        """The anchor may stand on its own line or on the name's own line, and
        a label may carry no anchor at all."""
        for text in ("SHIP TO:\nJANE DOE\n477 MARINE CENTER DR\n"
                     "HEMPSTEAD NY 11550-4703",
                     "SHIP TO: JANE DOE\n477 MARINE CENTER DR\n"
                     "HEMPSTEAD NY 11550-4703",
                     "JANE DOE\n477 MARINE CENTER DR\n"
                     "HEMPSTEAD NY 11550-4703",
                     "Deliver to: Jane Doe\n12 Elm Street, Springfield",
                     "SHIP TO\nJOHN SMITH III\n12 ELM ST\nSPRINGFIELD IL 62704"):
            assert ui.extract_recipient_name(text), text
        assert ui.extract_recipient_name(
            "SHIP TO\nJOHN SMITH III\n12 ELM ST\nSPRINGFIELD IL 62704") == \
            "John Smith III", "a suffix must not be title-cased into Iii"

    def test_postal_and_carrier_lines_are_never_a_name(self):
        """Every refusal that keeps an address off the card's name line: a line
        with a digit (street, apartment, ZIP, barcode), a lone street word, a
        city/state pair, and the carrier/billing words a label also prints."""
        for text in ("UPS GROUND\nTRACKING #: 1Z 1E8 52G 03 2501 5793\n"
                     "BILLING: 3RD PARTY |",           # carrier + billing only
                     "477 MARINE CENTER DR\nHEMPSTEAD NY 11550-4703",
                     "HEMPSTEAD NY\n477 MARINE CENTER DR",
                     "SHIP TO:\nAPT 4B\n477 MARINE CENTER DR\n"
                     "HEMPSTEAD NY 11550",
                     "* * *** *\n477 MARINE CENTER DR\nHEMPSTEAD NY 11550",
                     ""):
            assert ui.extract_recipient_name(text) == "", text

    def test_a_suite_line_inside_the_block_is_stepped_over(self):
        """The address block's own apartment line is not the name above it."""
        assert ui.extract_recipient_name(
            "JANE DOE\nSTE 200\n477 MAIN ST\nSPRINGFIELD IL 62704") == "Jane Doe"

    def test_of_two_blocks_the_lower_one_is_the_recipient(self):
        """A label prints the sender's return address above the ship-to block,
        so the lower block is the one the parcel is going to. (The documented
        limit: with no anchor and an unreadable recipient name line, the only
        block left is the sender's.)"""
        both = ("ACME WAREHOUSE\n100 INDUSTRIAL PKWY\nDALLAS TX 75201\n"
                "JANE DOE\n477 MAIN ST\nHEMPSTEAD NY 11550-4703")
        assert ui.extract_recipient_name(both) == "Jane Doe"
        sender_only = ("ACME WAREHOUSE\n100 INDUSTRIAL PKWY\nDALLAS TX 75201\n"
                       "477 MAIN ST\nHEMPSTEAD NY 11550-4703")
        assert ui.extract_recipient_name(sender_only) == "Acme Warehouse"


def test_every_mode_shares_one_download_and_one_delete(monkeypatch, ocr_fallback):
    """Privacy and cost: three reads, one temp file, deleted once."""
    _stub_pipeline(monkeypatch)
    seen = []
    _stub_tesseract(monkeypatch, seen=seen)

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))

    assert len(msg.bot.downloaded) == 1, "the image is fetched once, not per mode"
    assert len(seen) == len(main._OCR_PSMS), "one read per mode"
    assert len(set(seen)) == 1, "all reads use the same temp file"
    assert not seen[0].exists(), "and it is gone afterwards"


def test_the_help_and_privacy_copy_mention_the_screenshot_path():
    help_text = inspect.getsource(main.help_handler)
    assert "photo" in help_text.lower(), "/help must mention the add path"
    assert "barcode" in ui.PRIVACY_TEXT.lower(), \
        "privacy must name how the image is read"
    assert "screenshot" in ui.PRIVACY_TEXT.lower()
    assert "deleted immediately" in ui.PRIVACY_TEXT, \
        "privacy must say the image does not outlive the request"


# ──────────────────────────────────────────────
# Barcode first: the number is decoded, not guessed
# ──────────────────────────────────────────────
# Captured at import — before the autouse stub replaces it — so the test that
# exercises the real decoder is exercising the real decoder.
_REAL_DECODE = main._decode_barcodes


def test_barcode_first_skips_ocr_entirely(monkeypatch):
    """A decodable barcode is the whole answer: the number is added and Tesseract
    never runs, so none of the label's printed text is ever extracted."""
    added, sent = _stub_pipeline(monkeypatch)
    barcode_calls = []
    _stub_tesseract(monkeypatch, barcodes=[LABEL_NUMBER],
                    barcode_calls=barcode_calls)

    async def explode(path, psm=6):
        raise AssertionError("OCR must not run once a barcode named the number")

    monkeypatch.setattr(main, "_ocr_file", explode)

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))

    assert added == [], "the decoded number waits for the tap"
    assert _prompt_for(msg).text.count(LABEL_NUMBER) == 1, \
        "the prompt must quote exactly what was read"
    _tap_confirm(msg)

    assert added == [(LABEL_NUMBER, CHAT)], "the decoded number must be the add"
    assert any("Package added" in t for t in sent), "the same card as /track"
    assert len(barcode_calls) == 1 and not barcode_calls[0].exists(), \
        "one decode, one temp file, and it is gone"


def test_no_barcode_means_no_number_while_barcode_only_is_on(monkeypatch):
    """The shipping default: OCR text is available on the page (the stub returns a
    confirmation carrying a number) but is not consulted, so an image whose number
    is only printed, never encoded, adds nothing."""
    added, _sent = _stub_pipeline(monkeypatch)
    _stub_tesseract(monkeypatch, barcodes=[])

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))

    assert added == [], "OCR must not be consulted while barcode-only is on"
    assert any("Couldn't find a tracking number" in t for t in _sent)


def test_the_reversal_brings_ocr_back(monkeypatch, ocr_fallback):
    """The one-line reversal (main._BARCODE_ONLY = False) restores the fallback:
    same image, same stubs, and the printed number is used again."""
    added, _sent = _stub_pipeline(monkeypatch)
    _stub_tesseract(monkeypatch, barcodes=[])

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))
    _tap_confirm(msg)

    assert added == [(NUMBER, CHAT)], "the retained OCR path must still work"


def test_a_routing_barcode_is_not_a_tracking_number(monkeypatch, caplog):
    """Every parcel barcode set also carries "420" + ZIP routing. Twelve bare
    digits, so the parser would take it as a FedEx number — it must never be
    stored, and the payload must not reach a log."""
    added, _sent = _stub_pipeline(monkeypatch)
    _stub_tesseract(monkeypatch, text="", barcodes=[ROUTING])

    with caplog.at_level(logging.DEBUG, logger="tracker"):
        msg = _PhotoMsg()
        asyncio.run(main.photo_handler(msg))

    assert added == [], "a routing barcode was stored as a package"
    assert any("Couldn't find a tracking number" in t for t in _sent)
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert ROUTING not in logs, "a raw barcode payload reached the log"


def test_the_tracking_barcode_wins_over_the_routing_one(monkeypatch):
    """Both barcodes decode, exactly as on the real label: only the tracking
    number may be added."""
    added, _sent = _stub_pipeline(monkeypatch)
    _stub_tesseract(monkeypatch, barcodes=[ROUTING, LABEL_NUMBER])

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))

    assert ROUTING not in _prompt_for(msg).text, \
        "the routing barcode must never be offered for confirmation"
    _tap_confirm(msg)
    assert added == [(LABEL_NUMBER, CHAT)], added


def test_a_payload_that_is_not_a_number_is_dropped_and_never_logged(monkeypatch,
                                                                   caplog):
    """A barcode can hold anything the shipper printed: only a number the parser
    accepts may survive, and the payload itself must not reach a log."""
    added, _sent = _stub_pipeline(monkeypatch)
    payload = "SHIP-TO-JANE-DOE"
    _stub_tesseract(monkeypatch, text="", barcodes=[payload])

    with caplog.at_level(logging.DEBUG, logger="tracker"):
        asyncio.run(main.photo_handler(_PhotoMsg()))

    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert added == [], "a non-number payload was stored"
    assert payload not in logs, "a raw payload reached the log"


@pytest.mark.skipif(shutil.which("zbarimg") is None, reason="zbar is not installed")
def test_the_real_decoder_reads_a_generated_barcode(tmp_path):
    """The seam against the real binary: a Code 128 generated here decodes to its
    own payload. No real label photo is committed to the repo."""
    barcode = pytest.importorskip("barcode")
    from barcode.writer import ImageWriter

    path = barcode.get("code128", LABEL_NUMBER, writer=ImageWriter()).save(
        str(tmp_path / "code128"), {"write_text": False})

    assert asyncio.run(_REAL_DECODE(path)) == [LABEL_NUMBER]


# ──────────────────────────────────────────────
# Confirm before add: the decode is free, the registration is not
# ──────────────────────────────────────────────
def test_a_photo_spends_nothing_until_the_tap(monkeypatch):
    """The whole point of the prompt: a decode costs nothing, registering costs a
    17TRACK registration / a paid Ship24 call / a browser fetch. Asserted on the
    add pipeline's own seams, so a future refactor that looks up eagerly fails."""
    added, sent = _stub_pipeline(monkeypatch)
    _stub_tesseract(monkeypatch, barcodes=[LABEL_NUMBER])

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))

    prompt = _prompt_for(msg)
    assert LABEL_NUMBER in prompt.text, "the prompt must quote what was read"
    assert "Nothing has been looked up yet" in prompt.text
    assert added == [] and sent == [], "no lookup, no card, nothing spent"
    assert not any("Looking up" in t for t in sent), \
        "the add pipeline must not have run before the tap"


def test_only_the_person_who_sent_the_photo_can_confirm(monkeypatch):
    """A group button is everyone's to press, but the confirmation is not: the
    store remembers who asked, exactly like the removal flows."""
    added, _sent = _stub_pipeline(monkeypatch)
    _stub_tesseract(monkeypatch, barcodes=[LABEL_NUMBER])

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))

    callback = _tap_confirm(msg, uid=999)          # someone else in the group

    assert added == [], "a bystander's tap must not register anything"
    assert callback.answers and "Only the person who sent the photo" in \
        callback.answers[0], "the tapper must be told why nothing happened"


def test_cancel_drops_a_pending_add(monkeypatch):
    """A pending add is a pending action like any other: /cancel clears it and
    nothing is registered."""
    added, _sent = _stub_pipeline(monkeypatch)
    _stub_tesseract(monkeypatch, barcodes=[LABEL_NUMBER])

    msg = _PhotoMsg()
    asyncio.run(main.photo_handler(msg))
    assert (CHAT, "add", LABEL_NUMBER) in storage.pending_confirmations

    asyncio.run(main.cancel_handler(msg))

    assert (CHAT, "add", LABEL_NUMBER) not in storage.pending_confirmations
    assert any("nothing changed" in t for t in _sent)
    assert added == []
