"""Delivery history: the training data for future delivery-window estimates.

Nothing user-visible reads this yet — it is logging only. Every transition INTO
Delivered appends one record: the carrier, how long the package took, when it
landed, when the first Out-for-Delivery scan landed (the duration the prediction
layer actually trains on — transit days alone cannot), whether it crossed a
border, and whether the start time is real or a fallback.

`out_for_delivery_at` is stamped on the first OFD scan and never overwritten, so
repeat scans cannot move it; records for packages that were never observed in
OFD carry null there.

Transit is measured from `added_at` (when the user added the package), a field
that only exists going forward. Packages predating it fall back to the last
event we know of before the delivery and are flagged `start_approximate`, so the
prediction layer can weight or drop them. They are kept rather than skipped: an
in-flight package's delivery is valuable history even with a fuzzy start.

Encrypted at rest with the same protected-storage helpers as every other state
file, and tolerant of an unreadable one — this is disposable logging, so it warns
and starts fresh instead of refusing to start the bot.
"""
import json
import logging
from typing import List, Optional

import carriers
import config
import storage

logger = logging.getLogger("tracker")


def _parse_epoch(ts: Optional[str]) -> Optional[float]:
    """Stored ISO stamp → epoch seconds, or None when unusable.

    The same rule as retention/lost detection: a stamp we cannot read is never
    guessed at. It reuses refresh's parser rather than keeping a second
    implementation of that rule (imported lazily — refresh imports this module).
    """
    import refresh
    return refresh._parse_iso_epoch(ts)


def _build_record(info: dict, start_iso: Optional[str], delivered_iso: str) -> dict:
    """The one record shape the prediction layer will read."""
    delivered_at = _parse_epoch(delivered_iso)
    started_at = _parse_epoch(start_iso)
    transit_days = None
    if delivered_at is not None and started_at is not None:
        transit_days = (delivered_at - started_at) / 86400.0   # unrounded
    return {
        # The canonical carrier slug already on the record ("usps", "dhl", ...).
        "carrier": info.get("carrier") or "unknown",
        "transit_days": transit_days,
        "delivered_at": delivered_iso,
        # When the first Out for Delivery scan landed: the signal the future
        # delivery-window estimates actually train on (OFD → delivered per
        # carrier). null for packages that never showed OFD.
        "out_for_delivery_at": info.get("out_for_delivery_at"),
        # The same helper the international view / Pro allowance uses — never a
        # second detector.
        "international": carriers.is_international(info),
        # No added_at means the start is the last event we saw, not the moment
        # the user added it.
        "start_approximate": not info.get("added_at"),
    }


def record_delivery(info: dict, *, start_iso: Optional[str] = None) -> None:
    """Append one delivery record for a package that just became Delivered.

    `start_iso` lets the caller hand over the package's `last_updated` from
    BEFORE the delivery overwrote it — the closest thing a legacy record has to
    a start. Without it the stored `last_updated` is used (which, at this point,
    is the delivery itself).
    """
    start = info.get("added_at") or start_iso or info.get("last_updated")
    record = _build_record(info, start, storage.now_iso())
    history = load_history()
    history.append(record)
    # Bound the file: the whole list is rewritten on every delivery, so an
    # unbounded one makes each write costlier than the last. A COUNT cap, not a
    # time window — these records ARE the deliveries, so cutting by age would
    # throw away the training data this file exists to collect.
    cap = config.DELIVERY_HISTORY_MAX
    if cap > 0 and len(history) > cap:
        # [-cap:] drops from the FRONT: the newest records are the ones the
        # prediction layer trains on, and the file is oldest-first.
        history = history[-cap:]
    try:
        storage._write_protected(config.DELIVERY_HISTORY_FILE,
                                 json.dumps(history, indent=2))
    except OSError as e:
        logger.warning("Could not write delivery history: %s", e)
        return
    days = record["transit_days"]
    # Carrier and duration only: this log line must not carry a number or a chat.
    logger.info("\U0001f4e6 Delivery recorded: %s, %s transit days%s",
                record["carrier"],
                f"{days:.1f}" if days is not None else "unknown",
                " (approximate start)" if record["start_approximate"] else "")


def load_history() -> List[dict]:
    """Every recorded delivery, oldest first. Never raises."""
    path = config.DELIVERY_HISTORY_FILE
    if not path.exists():
        return []
    try:
        text, _was_enc = storage._read_protected(path)
    except SystemExit:
        # Wrong key: _read_protected already preserved the file as .locked.
        logger.warning("delivery_history.json unreadable (wrong key?) - preserved "
                       "as .locked, starting a new history")
        return []
    if text is None:
        logger.warning("Could not read delivery_history.json - ignoring")
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Could not parse delivery_history.json: %s", e)
        return []
    return data if isinstance(data, list) else []
