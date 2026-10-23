"""Delete-flow deactivation: removing a package must also stop tracking at
17TRACK — a local-only delete is half a deletion, and it leaks quota (17TRACK
counts accepted numbers against the account's cap).

/stoptrack is undocumented on any public 17TRACK page (their doc portal is
login-walled); it was found by calibrated probing and MEASURED against the
live API (2026-09-16): it answers the same accepted/rejected envelope as
/register, and a number that is not registered is rejected with error code
-18019902 ("does not register, please register first") — which for a delete
flow IS success: already gone is the goal.

The rule under test: a failed stop must never block or roll back a local
deletion, and every code path that deletes a package must attempt the stop.

The second concern here is the same privacy rule for register_17track: 17TRACK's
error envelopes echo the tracking number back verbatim, and tracker.log is a
longer-lived surface than the API response, so the log must carry the masked
form only — while keeping the error code/message that make it debuggable.
"""
import asyncio
import inspect
import time

import main
import carriers
import miniapp
import pro
import refresh


def _resp(accepted=(), rejected=()):
    return {"code": 0, "data": {"accepted": list(accepted), "rejected": list(rejected)}}


def test_accepted_numbers_count_as_stopped(monkeypatch):
    calls = []

    async def fake_post(path, payload, retries=3):
        calls.append((path, payload))
        return _resp(accepted=[{"number": "9400111206200074888350"}])

    monkeypatch.setattr(carriers, "_post_17track", fake_post)
    n = asyncio.run(carriers.stop_17track(["9400111206200074888350"]))
    assert n == 1
    assert calls == [("/stoptrack", [{"number": "9400111206200074888350"}])]


def test_not_registered_counts_as_already_gone(monkeypatch):
    async def fake_post(path, payload, retries=3):
        return _resp(rejected=[{"number": "9400111206200074888350",
                                "error": {"code": -18019902,
                                          "message": "does not register, please register first"}}])

    monkeypatch.setattr(carriers, "_post_17track", fake_post)
    assert asyncio.run(carriers.stop_17track(["9400111206200074888350"])) == 1


def test_unknown_rejection_is_not_success(monkeypatch):
    async def fake_post(path, payload, retries=3):
        return _resp(rejected=[{"number": "T1",
                                "error": {"code": -18019909, "message": "unknown"}}])

    monkeypatch.setattr(carriers, "_post_17track", fake_post)
    assert asyncio.run(carriers.stop_17track(["T1"])) == 0


def test_transport_failure_never_raises(monkeypatch):
    async def boom(path, payload, retries=3):
        raise RuntimeError("api down")

    monkeypatch.setattr(carriers, "_post_17track", boom)
    assert asyncio.run(carriers.stop_17track(["T1"])) == 0


def test_no_key_or_no_numbers_skips_the_call(monkeypatch):
    calls = []

    async def fake_post(path, payload, retries=3):
        calls.append(path)
        return _resp()

    monkeypatch.setattr(carriers, "_post_17track", fake_post)
    monkeypatch.setattr(carriers.config, "track17_api_key", "")
    assert asyncio.run(carriers.stop_17track(["T1"])) == 0
    monkeypatch.setattr(carriers.config, "track17_api_key", "k")
    assert asyncio.run(carriers.stop_17track([])) == 0
    assert calls == [], "nothing must be sent without a key or without numbers"


def test_retention_purge_also_stops_tracking(monkeypatch):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    storage = carriers.storage
    storage.track_sessions.clear()
    storage.track_sessions[7] = {
        "OLDDEL": {"name": "Box", "status": "Delivered", "last_updated": old},
        "MOVING": {"name": "Box", "status": "In Transit", "last_updated": old},
    }
    stopped = []

    async def fake_post(path, payload, retries=3):
        stopped.extend(x["number"] for x in payload)
        return _resp(accepted=[{"number": n["number"]} for n in payload])

    monkeypatch.setattr(carriers, "_post_17track", fake_post)
    monkeypatch.setattr(refresh, "_save_data_fn", lambda: None)
    try:
        purged = asyncio.run(refresh._auto_purge_delivered())
        assert purged == {7: 1}
        assert list(storage.track_sessions[7]) == ["MOVING"]
        assert stopped == ["OLDDEL"]
    finally:
        storage.track_sessions.clear()


def test_every_deletion_path_attempts_the_stop():
    """Source gate: every code path that deletes a package must also stop
    tracking at 17TRACK. The /forget flow funnels into the same
    confirm_remove/confirm_purge callbacks, so these five sites are the whole
    deletion surface."""
    for fn in (main.callback_confirm_remove, main.callback_confirm_purge,
               main.callback_confirm_clear, miniapp._api_remove,
               refresh._auto_purge_delivered):
        src = inspect.getsource(fn)
        assert "stop_17track" in src, \
            f"{fn.__name__} deletes locally without stopping 17TRACK"


# ──────────────────────────────────────────────
# register_17track logging: the provider's error envelope carries the number, so
# the log carries only its masked form (codes/messages stay, for debugging).
# ──────────────────────────────────────────────
FULL = "9400111206200074888350"
MASKED = "9400...8350"


def _register_with(monkeypatch, payload):
    """Drive register_17track with a canned /register envelope."""
    calls = []

    async def fake_post(path, body, retries=3):
        calls.append((path, body))
        return payload

    monkeypatch.setattr(carriers, "_post_17track", fake_post)
    monkeypatch.setattr(carriers.config, "track17_api_key", "k")
    carriers._register_blocked_until.pop(FULL, None)   # no cooldown short-circuit
    return asyncio.run(carriers.register_17track(FULL)), calls


def _log_lines(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


def test_redacted_rejected_keeps_codes_and_masks_numbers():
    out = carriers._redacted_rejected([
        {"number": FULL, "error": {"code": -1, "message": "nope"}},
        {"number": "SHORT"},                      # a malformed entry must not raise
    ])
    assert out[0] == {"number": MASKED, "error_code": -1, "error_message": "nope"}
    assert out[1] == {"number": "SHORT", "error_code": None, "error_message": None}


def test_outright_rejection_logs_the_masked_number_only(monkeypatch, caplog):
    """Top-level code != 0: the old call logged the whole `data` dict."""
    import logging as _logging
    payload = {"code": -18019900, "data": {"rejected": [
        {"number": FULL,
         "error": {"code": -18019900, "message": "quota exceeded"}}]}}

    with caplog.at_level(_logging.ERROR, logger="tracker"):
        result, calls = _register_with(monkeypatch, payload)

    assert result == "failed"
    assert calls == [("/register", [{"number": FULL, "auto_detection": True}])]
    lines = _log_lines(caplog)
    assert "register rejected outright" in lines, "the failure must still be logged"
    assert FULL not in lines, "the raw tracking number leaked into tracker.log"
    assert MASKED in lines, "the masked number must still identify the entry"
    assert "-18019900" in lines and "quota exceeded" in lines


def test_partial_rejection_logs_the_masked_number_only(monkeypatch, caplog):
    """code == 0 with rejected entries: the old call logged raw `rejected`."""
    import logging as _logging
    payload = {"code": 0, "data": {"accepted": [], "rejected": [
        {"number": FULL,
         "error": {"code": -18019909, "message": "unknown"}}]}}

    with caplog.at_level(_logging.ERROR, logger="tracker"):
        result, _calls = _register_with(monkeypatch, payload)

    assert result == "failed"
    lines = _log_lines(caplog)
    assert "register rejected" in lines
    assert FULL not in lines, "the raw tracking number leaked into tracker.log"
    assert MASKED in lines
    assert "-18019909" in lines and "unknown" in lines


# ──────────────────────────────────────────────
# Startup registration filter: 17TRACK quota is only spent where it can be used.
# ──────────────────────────────────────────────
USPS_TID = "9400111206200074888350"
INTL_TID = "RR123456789CN"        # CN post — 17TRACK is its only source
FREE_CHAT = 660001
PRO_CHAT = 660002


def _production_eligible(cid, tid):
    """The gate main() passes at startup, kept in one place for the test."""
    return carriers._use_17track(carriers.detect_carrier(tid)["code"],
                                 pro.is_pro(cid))


def _capture_registrations(monkeypatch):
    """Capture which numbers the startup pass would register."""
    attempted = []

    async def fake_register(number):
        attempted.append(number)
        return "registered"

    monkeypatch.setattr(carriers, "register_17track", fake_register)
    monkeypatch.setattr(carriers.config, "track17_api_key", "k")
    monkeypatch.setattr(carriers, "SOURCE_ROUTING", "matrix")
    return attempted


def test_startup_registration_skips_free_chats(monkeypatch):
    """A free chat's numbers can never be looked up through 17TRACK (Pro perk),
    so registering them at startup only spends quota slots for nothing."""
    attempted = _capture_registrations(monkeypatch)
    sessions = carriers.storage.track_sessions
    sessions.clear()
    assert pro.is_pro(FREE_CHAT) is False, "precondition: that chat is free"
    pro._pro_users[PRO_CHAT] = time.time() + 3600
    try:
        sessions[FREE_CHAT] = {USPS_TID: {"name": "p"}}
        sessions[PRO_CHAT] = {INTL_TID: {"name": "p"}}
        asyncio.run(carriers.re_register_all_17track(eligible=_production_eligible))
        assert attempted == [INTL_TID], \
            "only the pro chat's number may be registered at startup"
    finally:
        sessions.clear()
        pro._pro_users.pop(PRO_CHAT, None)


def test_startup_registration_respects_routing(monkeypatch):
    """Under SOURCE_ROUTING=matrix a USPS number with the official API configured
    is checked there, so 17TRACK must not be spent on it either."""
    attempted = _capture_registrations(monkeypatch)
    monkeypatch.setattr(carriers.config, "USPS_API_USERID", "TEST-USERID")
    assert carriers._use_17track("usps", True) is False, \
        "precondition: this number is routed to the official API"
    sessions = carriers.storage.track_sessions
    sessions.clear()
    pro._pro_users[PRO_CHAT] = time.time() + 3600
    try:
        sessions[PRO_CHAT] = {USPS_TID: {"name": "p"}}
        asyncio.run(carriers.re_register_all_17track(eligible=_production_eligible))
        assert attempted == [], "a routed-away number must not be registered"
    finally:
        sessions.clear()
        pro._pro_users.pop(PRO_CHAT, None)


def test_startup_call_site_passes_the_production_gate():
    """Source gate: main() must actually pass the predicate, or every stored
    number is registered again — the quota leak this filter exists to stop."""
    src = inspect.getsource(main.main)
    assert "re_register_all_17track(" in src and "eligible=" in src
    assert "_use_17track" in src


# ──────────────────────────────────────────────
# Shared numbers: deleting in one chat must not stop another chat's tracking.
# The 17TRACK registration is per-number, the delete is per-chat — so the stop
# is gated on "nobody else still holds it".
# ──────────────────────────────────────────────
SHARED = "9400111206200074888350"


def _record_stop_calls(monkeypatch):
    calls = []

    async def fake_post(path, payload, retries=3):
        calls.append((path, payload))
        return _resp(accepted=[{"number": n["number"]} for n in payload])

    monkeypatch.setattr(carriers, "_post_17track", fake_post)
    monkeypatch.setattr(carriers.config, "track17_api_key", "k")
    return calls


def test_shared_number_keeps_the_upstream_registration(monkeypatch):
    """Deleting a package in one chat must not stop a number another chat still
    tracks: that chat's card would stay but never update again."""
    calls = _record_stop_calls(monkeypatch)
    sessions = carriers.storage.track_sessions
    sessions.clear()
    try:
        sessions[7001] = {SHARED: {"name": "A"}}
        sessions[7002] = {SHARED: {"name": "B"}}
        del sessions[7001][SHARED]                 # what the caller does first

        assert asyncio.run(carriers.stop_17track_if_unshared([SHARED], 7001)) == 0
        assert calls == [], "the registration must stay for chat 7002"
        assert SHARED in sessions[7002], "the other chat's package is untouched"
    finally:
        sessions.clear()


def test_last_holder_still_stops_the_registration(monkeypatch):
    """With nobody else holding it, the stop happens exactly as before."""
    calls = _record_stop_calls(monkeypatch)
    sessions = carriers.storage.track_sessions
    sessions.clear()
    try:
        sessions[7001] = {SHARED: {"name": "A"}}
        del sessions[7001][SHARED]
        assert asyncio.run(carriers.stop_17track_if_unshared([SHARED], 7001)) == 1
        assert calls == [("/stoptrack", [{"number": SHARED}])]
    finally:
        sessions.clear()


def test_miniapp_remove_keeps_a_number_another_chat_still_tracks(monkeypatch):
    """The Mini App's remove is the call site that reported the bug."""
    calls = _record_stop_calls(monkeypatch)
    monkeypatch.setattr(refresh, "_save_data_fn", lambda: None)
    monkeypatch.setattr(miniapp, "_miniapp_user", lambda request: {"id": 7001})

    class _Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    sessions = carriers.storage.track_sessions
    sessions.clear()
    try:
        sessions[7001] = {SHARED: {"name": "A"}}
        sessions[7002] = {SHARED: {"name": "B"}}
        resp = asyncio.run(miniapp._api_remove(_Req({"tracking_id": SHARED})))
        assert resp.status == 200
        assert SHARED not in sessions[7001], "the local delete still happens"
        assert SHARED in sessions[7002], "the other chat keeps its package"
        assert calls == [], "and the upstream registration stays alive"

        # The last holder removing it does deregister.
        monkeypatch.setattr(miniapp, "_miniapp_user", lambda request: {"id": 7002})
        resp = asyncio.run(miniapp._api_remove(_Req({"tracking_id": SHARED})))
        assert resp.status == 200
        assert calls == [("/stoptrack", [{"number": SHARED}])], \
            "the last holder still stops it"
    finally:
        sessions.clear()


def test_retention_purge_keeps_a_number_another_chat_still_tracks(monkeypatch):
    """The purge spans chats, so its guard has to check every chat: a number
    forgotten in one chat while another still tracks it stays registered."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    calls = _record_stop_calls(monkeypatch)
    monkeypatch.setattr(refresh, "_save_data_fn", lambda: None)
    sessions = carriers.storage.track_sessions
    sessions.clear()
    try:
        sessions[7001] = {SHARED: {"name": "Box", "status": "Delivered",
                                   "last_updated": old}}
        sessions[7002] = {SHARED: {"name": "Box", "status": "In Transit",
                                   "last_updated": old}}
        purged = asyncio.run(refresh._auto_purge_delivered())
        assert purged == {7001: 1}, "only the old delivered copy is forgotten"
        assert SHARED in sessions[7002], "the other chat still tracks it"
        assert calls == [], "so the registration must not be stopped"
    finally:
        sessions.clear()
