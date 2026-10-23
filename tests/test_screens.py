"""The six screens (screens.py): rendering rules, the notification gate, and the
17TRACK scan-list hand-off. Pure rendering is tested directly; nothing here
touches Telegram or the network."""
from datetime import datetime, timedelta, timezone

import pytest

import carriers
import refresh
import screens

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _iso(days=0.0):
    return (NOW - timedelta(days=days)).isoformat()


def _info(status="In Transit", **kw):
    base = {"name": "Phone case", "carrier_name": "UPS", "status": status,
            "last_updated": _iso(0.1), "last_event_at": _iso(0.1)}
    base.update(kw)
    return base


# ── pills + progress ────────────────────────────────────────────────────────
@pytest.mark.parametrize("info,kind", [
    (_info("In Transit"), "in_transit"),
    (_info("In Transit", last_event_at=_iso(3.5)), "delayed"),
    (_info("In Transit", last_event_at=_iso(2.5)), "in_transit"),
    (_info("Out for Delivery"), "out_for_delivery"),
    (_info("Exception"), "problem"),
    (_info("Delivery Attempted"), "problem"),
    (_info("In Transit", lost=True), "problem"),
    (_info("Delivered"), "delivered"),
    (_info("Info Received"), "other"),
])
def test_pill_kind(info, kind):
    assert screens.pill_kind(info, NOW) == kind


def test_progress_bar_has_four_segments_and_tracks_status():
    assert screens.progress_bar("Info Received") == "▰▱▱▱"
    assert screens.progress_bar("In Transit") == "▰▰▱▱"
    assert screens.progress_bar("Out for Delivery") == "▰▰▰▱"
    assert screens.progress_bar("Delivered") == "▰▰▰▰"
    assert len(screens.progress_bar("anything else")) == 4


# ── screen 1 ────────────────────────────────────────────────────────────────
EVENTS = [{"t": _iso(i), "d": f"Scan {i}", "l": "Austin, TX"} for i in range(5)]


def test_card_shows_three_recent_scans_and_collapses_the_rest():
    html = screens.tracking_card_html("1Z999", _info(events=EVENTS))
    assert "Scan 0" in html and "Scan 2" in html
    assert html.index("Scan 0") < html.index("Full history (5 events)")
    assert "<blockquote expandable>" in html and "Scan 4" in html


def test_card_escapes_user_controlled_text():
    html = screens.tracking_card_html("1Z<b>", _info(name="<script>x</script>"))
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_card_with_only_a_latest_scan_uses_it():
    html = screens.tracking_card_html("1Z999", _info(detail="Arrived at hub"))
    assert "Arrived at hub" in html and "Full history" not in html


@pytest.mark.skipif(not screens.RICH_AVAILABLE, reason="needs aiogram 3.31+")
def test_rich_card_builds_and_serialises():
    info = _info(events=[dict(EVENTS[0], lat=30.27, lon=-97.74)] + EVENTS[1:])
    msg = screens.tracking_card_rich("1Z999", info)
    dumped = msg.model_dump_json(exclude_none=True)
    for needle in ("Phone case", "Full history (5 events)", '"map"'):
        assert needle in dumped


@pytest.mark.skipif(not screens.RICH_AVAILABLE, reason="needs aiogram 3.31+")
def test_map_and_map_button_only_when_coordinates_were_returned():
    plain = _info(events=EVENTS)
    assert screens.last_scan_coordinates(plain) is None
    assert '"map"' not in screens.tracking_card_rich("x", plain).model_dump_json()
    kb = screens.tracking_card_keyboard("x", plain)
    assert not any("map" in (b.text or "").lower() for r in kb.inline_keyboard for b in r)
    located = _info(events=[dict(EVENTS[0], lat=30.27, lon=-97.74)])
    kb = screens.tracking_card_keyboard("x", located)
    assert any(b.url and "openstreetmap" in b.url for r in kb.inline_keyboard for b in r)


def test_card_never_claims_a_live_driver_position():
    html = screens.tracking_card_html("x", _info(events=EVENTS)).lower()
    for word in ("driver", "stops away", "stops_away"):
        assert word not in html


def test_mute_button_flips_label():
    off = screens.tracking_card_keyboard("x", _info()).inline_keyboard[0]
    on = screens.tracking_card_keyboard("x", _info(muted=True)).inline_keyboard[0]
    assert "Mute" in off[1].text and "Unmute" in on[1].text


# ── screen 2 ────────────────────────────────────────────────────────────────
def test_list_filters():
    pk = {"a": _info("In Transit"),
          "b": _info("Delivered", last_updated=_iso(2)),
          "c": _info("Delivered", last_updated=_iso(20), name="Old")}
    assert "Phone case" in screens.package_list_text(pk, "active", NOW)
    assert "Old" not in screens.package_list_text(pk, "delivered", NOW)
    assert "Old" in screens.package_list_text(pk, "archive", NOW)
    assert "Nothing here" in screens.package_list_text({}, "active", NOW)


def test_list_never_prints_tracking_numbers():
    pk = {"1Z999AA10123456784": _info()}
    assert "1Z999AA10123456784" not in screens.package_list_text(pk, "active", NOW)


def test_list_keyboard_has_filters_and_add_buttons():
    kb = screens.package_list_keyboard("active").inline_keyboard
    texts = [b.text for row in kb for b in row]
    for want in ("Active", "Delivered", "Archive", "Add package", "Scan label"):
        assert any(want in t for t in texts)


# ── screen 3 ────────────────────────────────────────────────────────────────
def test_alert_buttons_are_green_and_red():
    row = screens.delivery_alert_keyboard("x").inline_keyboard[0]
    assert (row[0].style, row[1].style) == ("success", "danger")


def test_alert_headline_uses_the_window_when_known_else_says_today():
    win = _info("Out for Delivery", eta={"from": "2026-10-20T14:00:00+00:00",
                                         "to": "2026-10-20T16:00:00+00:00"})
    assert "2:00 to 4:00 PM" in screens.delivery_alert_text("x", win)
    assert "today" in screens.delivery_alert_text("x", _info("Out for Delivery"))


def test_followup_differs_when_nobody_was_home():
    assert "out" in screens.delivered_followup_text(_info(not_home=True))
    assert "Delivered" in screens.delivered_followup_text(_info())


# ── screen 5: settings gate ────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["Delivered", "Out for Delivery", "Exception",
                                    "Delivery Attempted", "In Transit", "Info Received"])
def test_defaults_notify_for_every_status_exactly_like_before(status):
    assert screens.notify_allowed(1, status, NOW.replace(hour=3)) is True


def test_toggles_gate_their_own_statuses_and_persist():
    screens.set_setting(1, "ofd", False)
    assert screens.notify_allowed(1, "Out for Delivery") is False
    assert screens.notify_allowed(1, "Delivered") is True
    assert screens.notify_allowed(2, "Out for Delivery") is True    # other chat
    screens._settings_cache = None                                  # re-read from disk
    assert screens.get_settings(1)["ofd"] is False


def test_quiet_hours_silence_everything_but_a_delivery():
    screens.set_setting(1, "quiet", True)
    night, day = NOW.replace(hour=23), NOW.replace(hour=12)
    assert screens.notify_allowed(1, "In Transit", night) is False
    assert screens.notify_allowed(1, "Exception", night) is False
    assert screens.notify_allowed(1, "Delivered", night) is True
    assert screens.notify_allowed(1, "In Transit", day) is True
    assert screens.notify_allowed(1, "In Transit", NOW.replace(hour=6)) is False


def test_settings_file_is_private():
    import os, stat
    screens.set_setting(5, "scans", False)
    assert stat.S_IMODE(os.stat(screens._settings_path()).st_mode) == 0o600


def test_unknown_setting_is_rejected():
    with pytest.raises(KeyError):
        screens.set_setting(1, "nope", True)


def test_settings_keyboard_shows_on_off_full_width():
    kb = screens.settings_keyboard(1).inline_keyboard
    assert all(len(row) == 1 for row in kb)
    labels = [row[0].text for row in kb]
    assert "Out for delivery: On" in labels and any("Quiet hours" in t for t in labels)
    assert labels[-1].endswith("Done")


def test_refresh_gate_fails_open(monkeypatch):
    monkeypatch.setattr(screens, "notify_allowed",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert refresh._chat_allows(1, "Delivered") is True


# ── 17TRACK hand-off ────────────────────────────────────────────────────────
ITEM = {"track_info": {
    "time_metrics": {"estimated_delivery_date": {
        "source": "Official", "from": "2026-10-20T14:00:00-05:00",
        "to": "2026-10-20T16:00:00-05:00"}},
    "tracking": {"providers": [{"provider": {"name": "UPS"}, "events": [
        {"time_iso": "2026-10-18T08:00:00-05:00", "description": "Departed",
         "location": "Dallas, TX", "address": {"coordinates": {
             "latitude": 32.78, "longitude": -96.8}}},
        {"time_iso": "2026-10-19T07:00:00-05:00", "description": "Out for delivery",
         "location": "Austin, TX", "address": {"coordinates": None}},
        {"time_iso": "2026-10-17T08:00:00-05:00", "description": "  "},
    ]}]}}}


def test_scans_are_newest_first_skip_blanks_and_keep_coordinates_only_when_real():
    scans = carriers._extract_scans(ITEM)
    assert [s["d"] for s in scans] == ["Out for delivery", "Departed"]
    assert "lat" not in scans[0] and scans[1]["lat"] == 32.78


def test_eta_window_extracted():
    assert carriers._extract_eta(ITEM)["source"] == "Official"
    assert carriers._extract_eta({"track_info": {}}) is None


def test_extras_travel_beside_the_lookup_and_land_on_the_package():
    carriers._SCAN_EXTRAS.clear()
    carriers._stash_scan_extras("T1", ITEM)
    info = {"status": "In Transit"}
    refresh._store_scan_extras("T1", info)
    assert info["events"][0]["d"] == "Out for delivery" and info["eta"]["to"]
    assert carriers.take_scan_extras("T1") is None            # handed off once
    refresh._store_scan_extras("nothing", info)               # no extras: untouched
    assert len(info["events"]) == 2


def test_extras_cache_is_bounded():
    carriers._SCAN_EXTRAS.clear()
    for i in range(carriers._SCAN_EXTRAS_MAX + 25):
        carriers._stash_scan_extras(f"T{i}", ITEM)
    assert len(carriers._SCAN_EXTRAS) <= carriers._SCAN_EXTRAS_MAX


def test_on_toggles_are_green_off_toggles_are_not():
    screens.set_setting(1, "scans", False)
    rows = {r[0].text: r[0].style for r in screens.settings_keyboard(1).inline_keyboard[:4]}
    assert rows["Out for delivery: On"] == "success"
    assert rows["Every scan update: Off"] is None


class _FakeBot:
    def __init__(self, rich_ok):
        self.rich_ok, self.sent = rich_ok, []

    async def send_rich_message(self, **kw):
        if not self.rich_ok:
            raise RuntimeError("rich messages not supported here")
        self.sent.append(("rich", kw))
        return object()

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("html", text))
        return object()


def test_card_falls_back_to_html_when_rich_fails():
    import asyncio
    bot = _FakeBot(rich_ok=False)
    asyncio.run(screens._deliver_card(bot, 1, "1Z999", _info(events=EVENTS)))
    assert bot.sent[0][0] == "html" and "Scan 0" in bot.sent[0][1]


@pytest.mark.skipif(not screens.RICH_AVAILABLE, reason="needs aiogram 3.31+")
def test_card_uses_rich_when_it_works():
    import asyncio
    bot = _FakeBot(rich_ok=True)
    asyncio.run(screens._deliver_card(bot, 1, "1Z999", _info(events=EVENTS)))
    assert bot.sent[0][0] == "rich"


# ── layout matches the design sheet ─────────────────────────────────────────
def test_card_map_button_sits_on_its_own_row_and_refresh_is_primary():
    info = _info(events=[dict(EVENTS[0], lat=30.27, lon=-97.74)] + EVENTS[1:])
    rows = screens.tracking_card_keyboard("x", info).inline_keyboard
    assert [b.text for b in rows[0]][0].endswith("Refresh") and rows[0][0].style == "primary"
    assert len(rows[0]) == 2 and len(rows) == 2 and "map" in rows[1][0].text


def test_card_full_history_lists_every_event_and_a_next_step():
    html = screens.tracking_card_html("1Z999", _info(events=EVENTS))
    assert "Full history (5 events)" in html and "○ <i>Out for delivery</i>" in html


def test_list_rows_show_carrier_and_day_not_numbers():
    pk = {"1Z999AA10123456784": _info("Out for Delivery", eta={
        "from": "2026-10-19T14:00:00+00:00", "to": "2026-10-19T16:00:00+00:00"})}
    text = screens.package_list_text(pk, "active", NOW)
    assert "Your packages</b> · 1 active" in text and "UPS · today" in text
    assert "1Z999AA10123456784" not in text
    assert screens.package_subline(_info("In Transit", last_event_at=_iso(4)), NOW) == "UPS · delayed"
    assert screens.package_subline(_info("Exception"), NOW) == "UPS · exception"


def test_add_screen_layout_and_text():
    draft = {"tid": "9400", "carrier": "usps", "carrier_name": "USPS", "name": None}
    kb = screens.add_number_keyboard(draft).inline_keyboard
    assert [len(r) for r in kb] == [3, 2, 1]
    assert kb[2][0].style == "primary" and kb[2][0].text.endswith("Start tracking")
    text = screens.add_number_text(draft)
    assert "Carrier detected" in text and "Name this package?" in text and "9400" not in text
