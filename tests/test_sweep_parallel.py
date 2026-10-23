"""The sweep checks its carriers in parallel: each carrier holds its own
browser slot, so a slow batch in one carrier no longer delays the others —
and no single carrier's request rate changes (its slot depth stays 1 unless
USPS_BROWSER_SLOTS_PER_CARRIER says otherwise)."""
import asyncio
import time

import carriers
import refresh
import storage
import usps_browser

TIDS = ["94001102000999999999",   # → usps
        "1Z999AA10123456784"]     # → ups


def test_the_sweep_checks_carriers_in_parallel(monkeypatch):
    inflight = set()
    peak = {"n": 0}

    def fake_batch(slug, ids, budget):
        inflight.add(slug)
        peak["n"] = max(peak["n"], len(inflight))
        time.sleep(0.3)
        inflight.discard(slug)
        return {ids[0]: ("Delivered", "fine")}

    monkeypatch.setattr(usps_browser, "get_browser_statuses", fake_batch)
    monkeypatch.setattr(refresh, "_apply_lookup", lambda *a, **k: None)
    monkeypatch.setattr(carriers, "_BROWSER_BODY_TIMEOUT_S", 30.0)
    monkeypatch.setattr(carriers, "_BROWSER_QUEUE_MAX_S", 5.0)
    monkeypatch.setattr(carriers, "_browser_sems", {})
    session = {tid: {"name": "Pkg", "status": "In Transit"} for tid in TIDS}
    monkeypatch.setattr(storage, "track_sessions", {1: session})

    updates = asyncio.run(refresh._refresh_browser_batch(1, list(TIDS)))
    assert peak["n"] >= 2, "carriers must overlap, not queue"
    assert set(updates) == set(TIDS)
    # The per-carrier slots are plain semaphores at the configured depth.
    assert all(isinstance(s, asyncio.Semaphore)
               for s in carriers._browser_sems.values())


def test_a_stalled_carrier_batch_skips_only_itself(monkeypatch):
    """One carrier's watchdog fire must not cost the other carriers their
    results (per-carrier slots + per-carrier handling)."""
    def fake_batch(slug, ids, budget):
        if slug == "ups":
            time.sleep(2.0)  # wedged batch
        return {ids[0]: ("Delivered", "fine")}

    monkeypatch.setattr(usps_browser, "get_browser_statuses", fake_batch)
    monkeypatch.setattr(refresh, "_apply_lookup", lambda *a, **k: None)
    monkeypatch.setattr(carriers, "_BROWSER_BODY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(carriers, "_BROWSER_QUEUE_MAX_S", 5.0)
    monkeypatch.setattr(carriers, "_browser_sems", {})
    session = {tid: {"name": "Pkg", "status": "In Transit"} for tid in TIDS}
    monkeypatch.setattr(storage, "track_sessions", {1: session})

    updates = asyncio.run(refresh._refresh_browser_batch(1, list(TIDS)))
    assert "94001102000999999999" in updates    # USPS answered
    assert "1Z999AA10123456784" not in updates  # UPS stalled, skipped
