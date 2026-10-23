"""M11 + per-carrier slots: the browser tier recovers from a hung lookup
instead of wedging — and each carrier now holds its own slot, so a hang in
one carrier can't stall the others.

carriers._browser_slot(slug) owns one semaphore per carrier for both lookup
paths. A worker stuck outside Playwright's own timeouts (driver handshake,
wedged renderer) used to hold the semaphore forever; the watchdog must (a)
cap the wait, (b) REBUILD that carrier's lock on a hang so later lookups go
through, (c) never release a lock the caller was queued out of, and (d)
leave other carriers' slots untouched.
"""
import asyncio

import pytest

import carriers
import usps_browser


def test_a_hung_lookup_times_out_and_its_carrier_recovers(monkeypatch):
    monkeypatch.setattr(carriers.config, "USPS_BROWSER_TRACK", True)
    monkeypatch.setattr(carriers, "_browser_sems", {})
    monkeypatch.setattr(carriers, "_BROWSER_BODY_TIMEOUT_S", 0.1)

    seen = {}
    calls = []

    def fake_lookup(slug, tid):
        calls.append(tid)
        if "sem" not in seen:
            seen["sem"] = carriers._browser_sems.get(slug)
        if len(calls) == 1:
            import time
            time.sleep(1.0)  # wedged worker thread — never returns in time
            return ("Delivered", "unreachable result")
        return ("Delivered", "fine")

    monkeypatch.setattr(usps_browser, "get_browser_status", fake_lookup)

    async def scenario():
        tid = "9300111043900394559543"
        with pytest.raises(asyncio.TimeoutError):
            await carriers._check_browser(tid, "usps")
        # The watchdog rebuilt THIS carrier's lock rather than releasing it.
        assert carriers._browser_sems["usps"] is not seen["sem"]
        assert seen["sem"].locked()  # the abandoned worker still "holds" it
        # And the tier is usable again — no queueing behind the zombie.
        return await carriers._check_browser(tid, "usps")

    assert asyncio.run(scenario()) == ("Delivered", "fine")
    assert calls == ["9300111043900394559543"] * 2


def test_a_hang_rebuilds_only_that_carriers_slot(monkeypatch):
    monkeypatch.setattr(carriers, "_browser_sems", {})

    async def scenario():
        s_usps = asyncio.Semaphore(1)
        s_ups = asyncio.Semaphore(1)
        carriers._browser_sems.update({"usps": s_usps, "ups": s_ups})
        with pytest.raises(asyncio.TimeoutError):
            async with carriers._browser_slot("usps"):
                raise asyncio.TimeoutError("wedged worker")
        assert carriers._browser_sems["usps"] is not s_usps  # rebuilt
        assert carriers._browser_sems["ups"] is s_ups        # untouched

    asyncio.run(scenario())


def test_a_queue_wait_timeout_never_releases_a_lock_it_never_got(monkeypatch):
    monkeypatch.setattr(carriers.config, "USPS_BROWSER_TRACK", True)
    monkeypatch.setattr(carriers, "_browser_sems", {})
    monkeypatch.setattr(carriers, "_BROWSER_QUEUE_STEP_S", 0.05)
    monkeypatch.setattr(carriers, "_BROWSER_QUEUE_MAX_S", 0.15)
    monkeypatch.setattr(usps_browser, "get_browser_status",
                        lambda slug, tid: ("Delivered", "x"))

    async def scenario():
        await carriers._acquire_browser_sem("usps")  # someone holds the slot
        try:
            with pytest.raises(asyncio.TimeoutError):
                await carriers._check_browser("9300111043900394559543", "usps")
            # Giving up on the QUEUE must not bump the semaphore: the holder
            # still owns it exclusively.
            assert carriers._browser_sems["usps"].locked()
        finally:
            carriers._browser_sems["usps"].release()

    asyncio.run(scenario())


def test_carriers_run_in_parallel_slots(monkeypatch):
    """The scaling change: holding USPS's slot must not delay UPS's, while a
    second USPS session still waits (depth 1 per carrier)."""
    monkeypatch.setattr(carriers, "_browser_sems", {})
    monkeypatch.setattr(carriers, "_BROWSER_QUEUE_MAX_S", 0.2)

    async def scenario():
        await carriers._acquire_browser_sem("usps")  # USPS session in flight
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    carriers._acquire_browser_sem("usps"), timeout=0.1)
            ups_sem = await asyncio.wait_for(
                carriers._acquire_browser_sem("ups"), timeout=0.1)
            assert carriers._browser_sems["ups"] is ups_sem
            carriers._browser_sems["ups"].release()
        finally:
            carriers._browser_sems["usps"].release()

    asyncio.run(scenario())


def test_the_depth_knob_builds_wider_slots(monkeypatch):
    monkeypatch.setattr(carriers, "_browser_sems", {})
    monkeypatch.setattr(carriers.config, "USPS_BROWSER_SLOTS_PER_CARRIER", 2)
    assert carriers._slot_depth() == 2

    async def scenario():
        first = await carriers._acquire_browser_sem("usps")
        # Depth 2: one session in flight, the second goes straight through.
        second = await asyncio.wait_for(
            carriers._acquire_browser_sem("usps"), timeout=0.1)
        carriers._browser_sems["usps"].release()
        carriers._browser_sems["usps"].release()
        return first is second

    assert asyncio.run(scenario()) is True
