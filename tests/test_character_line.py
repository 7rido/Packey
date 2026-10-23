"""Package-as-character: the first-person copy layer on the card.

The rules under test are structural on purpose — the wording is meant to be
edited freely, so these tests pin behaviour (never blank, deterministic per
package+status, present in the bot card and the app payload, absent from the
data-free push) rather than sentences. The one exception is the fallback line,
which must stay a single known string.
"""
import hashlib
import inspect
from html import escape

import carriers
import main
import refresh
import ui

# The canonical statuses, cross-checked against carriers._SHIP24_MILESTONES and
# ui.status_badge so a new status cannot slip in unvoiced.
CANONICAL = ("Info Received", "In Transit", "Out for Delivery",
             "Delivery Attempted", "Available for Pickup", "Delivered",
             "Exception", "Pending")
TID = "9400111206200074888350"


def _info(status="In Transit", name="Box"):
    return {"name": name, "status": status, "carrier": "usps",
            "carrier_name": "USPS", "carrier_url": "https://example.com",
            "detail": "Arrived at the hub", "last_updated": "2026-09-21T10:00:00+00:00"}


def test_the_canonical_status_list_matches_the_code():
    """If a status is added upstream, this test says so before the card does."""
    assert set(CANONICAL) == set(carriers._SHIP24_MILESTONES.values())
    for status in CANONICAL + ("Unavailable",):
        assert ui.character_line(TID, "Box", status).strip()


def test_every_status_gets_a_line_even_unknown_ones():
    """Never blank, never None: an empty card line is worse than plain copy."""
    for status in CANONICAL + ("Unavailable", "Unavailable (17TRACK)",
                               "Some Future Status", ""):
        line = ui.character_line(TID, "Box", status)
        assert isinstance(line, str) and line.strip(), status


def test_the_unknown_status_line_is_the_documented_fallback():
    assert ui.character_line(TID, "Box", "Some Future Status") == ui._CHARACTER_FALLBACK
    assert ui.character_line(TID, "Box", "") == ui._CHARACTER_FALLBACK


def test_the_variant_is_deterministic_and_picked_by_md5():
    """Same package + same status = same line, every render, every process.

    Pinned against the md5 formula so a switch to hash() (process-salted, and
    different in the app) would fail here rather than silently disagree.
    """
    for status in CANONICAL:
        first = ui.character_line(TID, "Box", status)
        assert ui.character_line(TID, "Box", status) == first
        variants = ui._CHARACTER_LINES[status]
        digest = hashlib.md5(f"{TID}|{status}".encode()).hexdigest()
        assert first == variants[int(digest, 16) % len(variants)].format(name="Box")


def test_both_variants_are_reachable():
    """Across 50 packages the in-transit line is not always the same one."""
    seen = {ui.character_line(f"TID{i:04d}", "Box", "In Transit")
            for i in range(50)}
    assert len(seen) == len(ui._CHARACTER_LINES["In Transit"]) > 1


def test_the_line_uses_the_package_name_and_nothing_else():
    line = ui.character_line(TID, "Blue Headphones", "In Transit")
    assert "Blue Headphones" in line
    assert TID not in line, "the line must not carry the tracking number"
    assert "<" not in line and ">" not in line, "plain text only"


def test_the_name_is_the_only_interpolation():
    """A name that needs escaping must not be able to break the card's HTML."""
    nasty = "<b>bold</b> & co"
    line = ui.character_line(TID, nasty, "Delivered")
    assert "<b>" not in line
    card = ui.package_card(TID, _info("Delivered", name=nasty))
    assert "<b>bold</b> & co" not in card, "an escaped name leaked as markup"
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; co" in card


def test_the_bot_card_carries_the_line():
    card = ui.package_card(TID, _info())
    # The card HTML-escapes everything user-visible, so the line arrives with
    # its apostrophes as entities — exactly like the name and detail do.
    assert escape(ui.character_line(TID, "Box", "In Transit")) in card
    # ...and it sits under the latest-scan line, not inside the facts above it.
    assert card.index("Latest:") < card.index(
        escape(ui.character_line(TID, "Box", "In Transit")))


def test_the_card_without_a_detail_line_still_carries_it():
    info = _info()
    info.pop("detail")
    card = ui.package_card(TID, info)
    assert escape(ui.character_line(TID, "Box", "In Transit")) in card
    assert "Latest:" not in card


def test_the_app_payload_carries_the_same_line():
    """One implementation: the app payload must match the bot card exactly."""
    info = _info()
    payload = main._package_payload(TID, info)
    assert payload["character_line"] == ui.character_line(TID, "Box", "In Transit")
    assert payload["character_line"].strip()


def test_the_memorial_line_replaces_the_status_voice():
    """A lost package stops talking as if it were moving. (Detection, funerals
    and resurrections live in tests/test_funerals.py.)"""
    info = _info()
    info["lost"] = True
    info["last_event_at"] = "2026-08-01T10:00:00+00:00"
    status_voice = ui.character_line(TID, "Box", "In Transit")
    memorial = ui.character_line(TID, "Box", "In Transit", lost=True,
                                 lost_since=info["last_event_at"])
    assert memorial != status_voice
    assert "lost since" in memorial
    card = ui.package_card(TID, info)
    assert escape(memorial) in card
    assert escape(status_voice) not in card, "the live voice survived the funeral"
    assert main._package_payload(TID, info)["character_line"] == memorial


def test_a_package_that_is_not_lost_keeps_its_voice():
    """is True, not truthiness: a legacy record with no lost key (or a cleared
    one) must not read as lost."""
    for value in (None, False, 0, ""):
        info = _info()
        if value is not None:
            info["lost"] = value
        # The card escapes its copy, so compare escaped (see the note above).
        assert escape(ui.character_line(TID, "Box", "In Transit")) in ui.package_card(TID, info)


def test_the_push_and_the_retention_notice_stay_dataless():
    """The character line belongs on the card; the push lands in permanent
    group history and must keep carrying nothing."""
    window = 3
    ping, _kb = refresh.build_global_ping(window)
    notice, _kb2 = refresh.build_retention_notice(window)
    for text in (ping, notice):
        assert "Box" not in text
        assert ui._CHARACTER_FALLBACK not in text
        for status in CANONICAL:
            for variant in ui._CHARACTER_LINES[status]:
                assert variant.format(name="Box") not in text
        assert TID not in text


def test_the_mini_app_renders_it_defensively():
    """Absent key → nothing rendered; never a blank line or 'undefined'."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent
            / "webapp" / "index.html").read_text()
    assert "p.character_line ?" in html, "the app must guard the key"
    assert "esc(p.character_line)" in html, "and escape it"
    assert "undefined" not in html.split("character_line")[1][:80]


def test_list_uses_its_own_compact_rendering():
    """The character line must not leak into /list's compact format."""
    assert "package_card" not in inspect.getsource(main.list_handler)
