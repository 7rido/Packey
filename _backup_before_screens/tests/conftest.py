"""Safety net: no test may touch the LIVE data files, or page the operator.

The bot runs on this machine, so `track_data.json` / `pro_users.json` /
`refresh_limits.json` / `register_cooldown.json` are production data. A test that
drives a real handler is one forgotten redirection away from rewriting the live
store with its own fixtures — which is exactly what happened once: a test called
`main.track_handler()`, the handler ends with `save_data()`, and the live store
was replaced by that one test chat (the running bot still held the real data in
memory, so a `save_data()` from the bot restored it, but a restart in that
window would have lost everything).

So the redirection is now automatic for the whole suite. Tests that want to
exercise saving just point the constant at their own tmp file on top of this —
`main.save_data()` reads `main.DATA_FILE` at call time, so both orders work.

The same net covers operator alerts: they leave the machine for real, and the
suite drives the failure paths that raise them on purpose. The backup script's
alert did exactly that (every suite run paged the owner) until it was fixed, so
the bot-side alert is off for the whole suite and only tests/test_admin_alerts.py
turns it back on — always with a fake Bot, never a real one.
"""
import pytest

import botstate
import config
import main
import usps_browser as up


@pytest.fixture(autouse=True)
def isolated_data_files(tmp_path, monkeypatch):
    """Every data file the bot writes goes to this test's tmp dir."""
    monkeypatch.setattr(main, "DATA_FILE", tmp_path / "track_data.json")
    monkeypatch.setattr(main, "REG_COOLDOWN_FILE",
                        tmp_path / "register_cooldown.json")
    monkeypatch.setattr(config, "PRO_USERS_FILE", tmp_path / "pro_users.json")
    monkeypatch.setattr(config, "REFRESH_LIMITS_FILE",
                        tmp_path / "refresh_limits.json")
    monkeypatch.setattr(config, "PENDING_PAYMENTS_FILE",
                        tmp_path / "pending_payments.json")
    monkeypatch.setattr(config, "PRO_RECEIPTS_FILE",
                        tmp_path / "pro_receipts.json")
    monkeypatch.setattr(config, "PRO_EXPIRY_WARNED_FILE",
                        tmp_path / "pro_expiry_warned.json")
    monkeypatch.setattr(config, "INTL_REGISTRATIONS_FILE",
                        tmp_path / "intl_registrations.json")
    monkeypatch.setattr(config, "DEPOSIT_ADDRS_FILE",
                        tmp_path / "deposit_addrs.json")
    monkeypatch.setattr(config, "SHIP24_COUNTER_FILE",
                        tmp_path / "ship24_counter.json")
    monkeypatch.setattr(config, "SHARE_LINKS_FILE",
                        tmp_path / "share_links.json")
    monkeypatch.setattr(config, "DELIVERY_HISTORY_FILE",
                        tmp_path / "delivery_history.json")
    monkeypatch.setattr(config, "ADMIN_ALERTS", False)
    # The tidy middleware's keep-input set is module state: one test marking a
    # message as un-acted-on must not leak into a later test's deletion assertion.
    monkeypatch.setattr(main, "_keep_input", set())
    # The startup notice keeps its state in a repo-root file (encrypted, 0600).
    # A test must never write that one: a stale fingerprint there would make the
    # next real start announce "new code" that never shipped.
    monkeypatch.setattr(botstate, "_STATE_FILE",
                        str(tmp_path / "admin_notify.json"))
    # browser_throttle.json is live state too (2026-10-04: throttle tests
    # armed DHL straight into the RUNNING bot's pause file — phantom "DHL
    # strikes" that never appeared in the bot's log, because they never
    # happened there). Isolate the store and the in-memory throttle state.
    monkeypatch.setattr(up, "_THROTTLE_STORE", str(tmp_path / "throttle.json"))
    monkeypatch.setattr(up, "_throttled_until", {})
    monkeypatch.setattr(up, "_throttle_strikes", {})
    monkeypatch.setattr(up, "_LAST_OUTCOME", {})
    monkeypatch.setattr(up, "USPS_DEBUG_DIR", None)
    yield
