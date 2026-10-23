"""The browser canary: one live package per carrier, once a day, alert the
operator on a health CHANGE.

Why this exists: the sweep's failure modes are silent by design — a carrier
whose widget breaks or whose edge starts refusing just produces no_data ticks
forever (DHL 2026-10-04: their SPA config 404s, the results never render, and
nobody would have known until a user asked where their update went). The
canary is the tripwire that turns that silence into a Telegram alert.

The alert must never leak: the tracking number appears only masked, and
notify_admins is replaced by a recorder here (the conftest already turns
ADMIN_ALERTS off, so the real sender can never run in tests).
"""
import asyncio
import time

import pytest

import botstate
import carriers
import refresh
import usps_browser as up

USPS = "9400111206200074888350"
UPS = "1Z1234567890123456"
DHL = "9999999999"


def _pkg(status="In Transit", last_checked="2026-10-04T15:00:00"):
    return {"status": status, "last_checked": last_checked}


@pytest.fixture
def canary(monkeypatch):
    """Fresh canary state, a live store with one package per carrier, and a
    notify_admins recorder. Tier fakes call _note_outcome so the canary's
    get_last_outcome() read sees the intended outcome."""
    sent = []

    async def _fake_notify(text, key="general"):
        sent.append((key, text))
        return True

    calls = []

    def _fake_tier(slug, tids, timeout_budget=120, headful=None):
        calls.append((slug, tuple(tids)))
        for t in tids:
            up._note_outcome(t, outcomes.get(slug, "ok"))
        return {}

    outcomes = {"usps": "ok", "ups": "ok", "dhl": "ok", "fedex": "ok"}
    monkeypatch.setattr(botstate, "notify_admins", _fake_notify)
    monkeypatch.setattr(refresh, "_last_canary_at", None)
    monkeypatch.setattr(refresh, "_canary_healthy", {})
    monkeypatch.setattr(refresh.storage, "track_sessions", {
        7: {USPS: _pkg(), UPS: _pkg(), DHL: _pkg()},
    })
    monkeypatch.setattr(up, "get_browser_statuses", _fake_tier)
    return {"sent": sent, "calls": calls, "outcomes": outcomes}


def _run():
    return asyncio.run(refresh._run_browser_canary())


def test_healthy_carriers_stay_silent(canary):
    """First observation, all healthy: record it, alert nobody."""
    _run()
    assert canary["sent"] == []
    assert refresh._canary_healthy == {"usps": True, "ups": True, "dhl": True}


def test_a_bad_outcome_alerts_once_then_reCOVERS(canary):
    """usps goes bad -> one alert; still bad -> quiet; ok again -> recovery."""
    canary["outcomes"]["usps"] = "no_data"
    _run()
    assert len(canary["sent"]) == 1
    key, text = canary["sent"][0]
    assert key == "canary-usps"
    assert "USPS" in text and "no_data" in text
    # Privacy: masked form only — never the raw number.
    assert USPS not in text and carriers._sanitize_tracking_id(USPS) in text

    _run()                                   # still broken: no second alert
    assert len(canary["sent"]) == 1

    canary["outcomes"]["usps"] = "ok"
    _run()                                   # recovery: exactly one more alert
    assert len(canary["sent"]) == 2
    assert "healthy again" in canary["sent"][1][1]


def test_carriers_without_a_live_package_are_skipped(canary):
    """No fedex package exists in the store, so fedex is never probed — a
    canary run against nothing would be noise, not signal."""
    _run()
    probed = {slug for slug, _ in canary["calls"]}
    assert "fedex" not in probed
    assert "fedex" not in refresh._canary_healthy


def test_disabled_slug_is_still_probed_for_recovery(canary, monkeypatch):
    """DHL is disabled in the browser tier (their widget cannot render), but
    the canary probes it anyway — one read a day is how a recovery gets
    noticed and re-enabling becomes worthwhile."""
    monkeypatch.setattr(up, "_DISABLED_SLUGS", frozenset({"dhl"}))
    assert "dhl" not in up.browser_slugs()   # the sweep really skips it
    _run()
    probed = {slug for slug, _ in canary["calls"]}
    assert "dhl" in probed


def test_terminal_and_stale_packages_are_not_probed(canary):
    """A Delivered package freezes (the sweep stops reading it), so it is
    useless as a health signal; the most recently checked live one wins."""
    refresh.storage.track_sessions[7][UPS]["status"] = "Delivered"
    refresh.storage.track_sessions[7][USPS]["last_checked"] = "2026-10-04T09:00:00"
    # DHL package goes terminal: dhl must not be probed at all now.
    refresh.storage.track_sessions[7][DHL]["status"] = "Delivered"
    _run()
    probed = {slug: tids for slug, tids in canary["calls"]}
    assert "dhl" not in probed
    assert probed["usps"] == (USPS,)         # the only live usps package


def test_due_gating_spawns_at_most_one_task_per_interval(monkeypatch):
    """_schedule_browser_canary is the loop hook: due once per interval, and
    the spawned task must not be garbage-collected (kept referenced)."""
    spawned = []

    async def _fake_run():
        spawned.append(1)

    monkeypatch.setattr(refresh, "_run_browser_canary", _fake_run)
    monkeypatch.setattr(refresh, "_last_canary_at", None)
    monkeypatch.setattr(refresh, "_canary_tasks", set())

    async def _scenario():
        refresh._schedule_browser_canary(time.monotonic())
        refresh._schedule_browser_canary(time.monotonic())  # not due: one task
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(spawned) == 1

        refresh._last_canary_at -= refresh._CANARY_INTERVAL_S + 1
        refresh._schedule_browser_canary(time.monotonic())
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(spawned) == 2

    asyncio.run(_scenario())
