"""Delivery-history logging: one record per delivery, nothing user-visible.

The record is the training data for the future delivery-window estimates, so
these tests pin the shape, the transition gate (never a duplicate), the
approximate-start flag for records that predate added_at, and that the history
is encrypted at rest.
"""
import asyncio
import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import delivery_history
import refresh
import storage

TID = "9400111206200074888350"
CARRIER = {"code": "usps", "name": "USPS", "url": "https://tools.usps.com"}
CHINA = {"code": "chinapost", "name": "China Post", "url": ""}


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _info(status="In Transit", **extra):
    info = {"name": "Box", "status": status, "carrier": "usps",
            "carrier_name": "USPS", "detail": "Departed the hub.",
            "last_updated": _iso(0.5), "added_at": _iso(5)}
    info.update(extra)
    return info


def _history():
    return delivery_history.load_history()


def _deliver(info, carrier=CARRIER, detail="Delivered, front door", **kwargs):
    refresh._apply_lookup(info, "Delivered", carrier, "USPS Browser", detail,
                          **kwargs)


def test_a_delivery_transition_records_transit_days():
    info = _info()
    _deliver(info)
    records = _history()
    assert len(records) == 1
    rec = records[0]
    assert set(rec) == {"carrier", "transit_days", "delivered_at",
                        "out_for_delivery_at", "international", "start_approximate"}
    assert rec["carrier"] == "usps"
    assert 4.9 < rec["transit_days"] < 5.1, rec          # added_at was 5 days ago
    assert rec["international"] is False
    assert rec["start_approximate"] is False
    assert rec["out_for_delivery_at"] is None            # this one never showed OFD
    assert refresh._parse_iso_epoch(rec["delivered_at"]) is not None


def test_an_international_delivery_is_marked_international():
    """The flag comes from the same helper the 🌍 view and the Pro allowance
    use — never a second detector."""
    info = _info(carrier="chinapost", carrier_name="China Post")
    _deliver(info, carrier=CHINA, detail="Delivered")
    assert _history()[0]["international"] is True
    assert _history()[0]["carrier"] == "chinapost"


def test_a_repeat_delivered_lookup_does_not_record_again():
    info = _info()
    _deliver(info)
    _deliver(info, detail="Delivered, left at the door")
    assert len(_history()) == 1, "a repeat lookup is not a second delivery"


def test_an_already_delivered_package_records_nothing():
    """Only the transition INTO Delivered is a delivery event."""
    info = _info(status="Delivered")
    _deliver(info, detail="Delivered")
    assert _history() == []


def test_legacy_records_use_the_last_known_event_and_flag_it():
    """No added_at: the start is the last event before the delivery (not the
    delivery moment, which would make every legacy record zero days), and the
    record says the start is approximate."""
    info = _info()
    info.pop("added_at")
    info["last_updated"] = _iso(2)
    _deliver(info)
    rec = _history()[0]
    assert rec["start_approximate"] is True
    assert 1.9 < rec["transit_days"] < 2.1, rec
    assert rec["transit_days"] > 1.0, "the delivery moment is not a start time"


@pytest.mark.parametrize("old,new", [("In Transit", "Out for Delivery"),
                                     ("Info Received", "In Transit"),
                                     ("Out for Delivery", "Delivery Attempted"),
                                     ("Pending", "Info Received")])
def test_non_delivery_transitions_record_nothing(old, new):
    info = _info(status=old)
    refresh._apply_lookup(info, new, CARRIER, "17TRACK", "Some new scan")
    assert _history() == []


def test_the_downgrade_guard_still_wins_and_records_nothing():
    """A lagging source must not un-deliver a package — and an un-delivery is
    not a delivery either. The claimant is the official USPS tier; 17TRACK's
    echo is the weaker witness the ranked guard ignores."""
    info = _info(status="Delivered", delivered_by="USPS API")
    refresh._apply_lookup(info, "In Transit", CARRIER, "17TRACK", "Departed hub")
    assert info["status"] == "Delivered"
    assert _history() == []


def test_the_history_file_round_trips_through_encrypted_storage():
    _deliver(_info())
    path = config.DELIVERY_HISTORY_FILE
    assert path.exists(), "the record must be persisted at the transition"
    assert storage._is_ciphertext(path), "history must be encrypted at rest"
    text, _was_enc = storage._read_protected(path)
    assert len(json.loads(text)) == 1
    assert _history()[0]["carrier"] == "usps"


def test_first_out_for_delivery_stamps_once_and_never_overwrites():
    """OFD → delivered is the duration the prediction layer trains on, so the
    first OFD scan is the moment that counts."""
    info = _info(status="In Transit")
    refresh._apply_lookup(info, "Out for Delivery", CARRIER, "USPS Browser", "On the truck")
    first = info["out_for_delivery_at"]
    assert refresh._parse_iso_epoch(first) is not None
    # A repeat OFD scan (same status, new detail) must not move the stamp.
    refresh._apply_lookup(info, "Out for Delivery", CARRIER, "USPS Browser", "Still on the truck")
    assert info["out_for_delivery_at"] == first
    # ...and neither must a later round trip through the status.
    refresh._apply_lookup(info, "In Transit", CARRIER, "17TRACK", "Back at the depot")
    refresh._apply_lookup(info, "Out for Delivery", CARRIER, "USPS Browser", "On the truck again")
    assert info["out_for_delivery_at"] == first


def test_the_delivery_record_carries_the_out_for_delivery_stamp():
    info = _info(status="In Transit")
    refresh._apply_lookup(info, "Out for Delivery", CARRIER, "USPS Browser", "On the truck")
    ofd = info["out_for_delivery_at"]
    _deliver(info)
    rec = _history()[0]
    assert rec["out_for_delivery_at"] == ofd
    assert (refresh._parse_iso_epoch(rec["delivered_at"])
            >= refresh._parse_iso_epoch(rec["out_for_delivery_at"]))


def test_a_non_out_for_delivery_package_records_a_null_stamp():
    _deliver(_info())
    assert _history()[0]["out_for_delivery_at"] is None


def test_added_at_is_set_when_a_package_is_added(monkeypatch):
    """added_at is the start of the transit measurement, so it must exist from
    the first write onward."""
    async def _lookup(tracking_id, allow_browser=None, is_pro=None, **kwargs):
        return "In Transit", CARRIER, "17TRACK", "Departed the hub"

    monkeypatch.setattr(refresh.carriers, "fetch_track_status", _lookup)
    info, _reg = asyncio.run(refresh._register_and_fetch(TID, is_pro=False))
    assert refresh._parse_iso_epoch(info["added_at"]) is not None


def test_nothing_user_visible_reads_the_history():
    """Logging only: there is deliberately nothing to see — the card, the Mini
    App payload, the push and the retention notice must not know it exists."""
    import miniapp
    import ui

    for module in (ui, miniapp):
        assert "delivery_history" not in inspect.getsource(module), module.__name__
    assert "delivery_history" not in inspect.getsource(refresh.build_global_ping)
    assert "delivery_history" not in inspect.getsource(refresh.build_retention_notice)


def test_the_log_line_carries_no_tracking_number_or_chat(caplog):
    """The one thing this feature writes to the log must stay as private as
    every other log line."""
    with caplog.at_level(logging.INFO, logger="tracker"):
        _deliver(_info())
    assert caplog.text.strip(), "the delivery should be logged"
    assert TID not in caplog.text
    assert "987654321" not in caplog.text
    assert "usps" in caplog.text


def test_logging_is_free_and_has_no_pro_gate():
    src = inspect.getsource(delivery_history)
    assert "pro." not in src and "is_pro" not in src


def test_the_history_file_is_declared_for_backup():
    """A new state file must not be able to miss its backup (the script's own
    rule), and tests must not be able to write the live one."""
    root = Path(__file__).resolve().parent.parent
    for name in ("scripts/backup_state.sh", "tests/test_backup.py",
                 "tests/conftest.py"):
        assert "DELIVERY_HISTORY_FILE" in (root / name).read_text(), name
    assert config.DELIVERY_HISTORY_FILE.name == "delivery_history.json"


def test_an_unreadable_history_is_not_fatal(monkeypatch):
    """History is disposable logging: a corrupt or wrongly-keyed file warns and
    starts fresh instead of blocking a delivery (or the bot)."""
    config.DELIVERY_HISTORY_FILE.write_text("not json at all")
    assert _history() == []
    _deliver(_info())
    assert len(_history()) == 1


def test_an_empty_history_file_is_fine():
    assert _history() == []
    assert config.DELIVERY_HISTORY_FILE.exists() is False


# ── the record cap (config.DELIVERY_HISTORY_MAX) ────────────────────────────
# Why it exists: the whole list is rewritten on every delivery, so an unbounded
# one makes each write costlier than the last. A COUNT, deliberately not a
# days-based cut like RETENTION_DAYS — these records ARE the deliveries, so
# trimming by age would delete the training data the file exists to collect.

def test_the_history_never_exceeds_the_cap(monkeypatch):
    monkeypatch.setattr(config, "DELIVERY_HISTORY_MAX", 5)
    for _ in range(12):
        _deliver(_info())
    assert len(_history()) == 5


def test_the_cap_keeps_the_NEWEST_records(monkeypatch):
    """The half a naive history[:-cap] gets backwards.

    The file is oldest-first and the prediction layer trains on recent
    deliveries, so the records dropped must be the OLD ones. Mutating the
    slice to [:-cap] fails this test and two others.
    """
    monkeypatch.setattr(config, "DELIVERY_HISTORY_MAX", 3)
    # A distinct carrier per delivery, so each record is identifiable in the
    # file — otherwise every record looks alike and "which ones survived"
    # cannot actually be checked.
    for code, name in (("usps", "USPS"), ("ups", "UPS"), ("fedex", "FedEx"),
                       ("dhl", "DHL"), ("ontrac", "OnTrac")):
        _deliver(_info(carrier=code, carrier_name=name),
                 carrier={"code": code, "name": name, "url": ""})
    # The three most recent survive; usps and ups are dropped.
    assert [r["carrier"] for r in _history()] == ["fedex", "dhl", "ontrac"]


def test_a_cap_of_zero_keeps_everything(monkeypatch):
    """The "0 disables it" convention, same as RETENTION_DAYS / LOST_AFTER_DAYS."""
    monkeypatch.setattr(config, "DELIVERY_HISTORY_MAX", 0)
    for _ in range(8):
        _deliver(_info())
    assert len(_history()) == 8


def test_an_already_oversized_file_trims_to_the_cap(monkeypatch):
    """A file written before the cap existed (or under a larger one) must self-trim
    on the next delivery rather than needing a migration — and must not raise."""
    monkeypatch.setattr(config, "DELIVERY_HISTORY_MAX", 4)
    for _ in range(9):
        _deliver(_info())
    for _ in range(2):          # cap already reached; the next write trims again
        _deliver(_info())
    assert len(_history()) == 4


def test_the_cap_defaults_to_a_thousand():
    """Pinned so the shipped default can't silently drift (and so an operator
    who never sets the knob gets the reviewed number, not an unbounded file)."""
    assert config.DELIVERY_HISTORY_MAX == 1000
