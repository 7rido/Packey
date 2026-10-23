"""Tests for the Telegram Mini App backend (initData validation + API)."""
import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import quote

import pytest

import config
import main
import miniapp
import share_links
import storage

TOKEN = "123456:TEST-TOKEN-abc123"


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    # _validate_init_data reads the token from config (the single source of
    # truth); patch it there and restore afterwards. main._bot_token is a
    # legacy alias and no longer consulted by the validator.
    monkeypatch.setattr(main.config, "_bot_token", TOKEN)


def sign_init_data(user: dict, auth_date: int = None, token: str = TOKEN) -> str:
    """Build initData exactly the way Telegram signs it."""
    params = {
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    payload = "&".join(f"{k}={quote(v)}" for k, v in params.items())
    return f"{payload}&hash={digest}"


def test_valid_init_data():
    user = {"id": 42, "first_name": "Fred"}
    assert main._validate_init_data(sign_init_data(user)) == user


def test_invalid_hash_rejected():
    user = {"id": 42, "first_name": "Fred"}
    bad = sign_init_data(user, token="123456:WRONG-TOKEN")
    assert main._validate_init_data(bad) is None


def test_tampered_user_rejected():
    signed = sign_init_data({"id": 42, "first_name": "Fred"})
    tampered = signed.replace("Fred", "Eve")
    assert main._validate_init_data(tampered) is None


def test_expired_init_data_rejected():
    user = {"id": 42, "first_name": "Fred"}
    old = int(time.time()) - main.INIT_DATA_MAX_AGE - 60
    assert main._validate_init_data(sign_init_data(user, auth_date=old)) is None


def test_missing_hash_rejected():
    assert main._validate_init_data("user=%7B%7D&auth_date=123") is None


def test_no_token_configured_rejected(monkeypatch):
    monkeypatch.setattr(main.config, "_bot_token", None)
    user = {"id": 42, "first_name": "Fred"}
    assert main._validate_init_data(sign_init_data(user)) is None


def test_empty_user_rejected():
    assert main._validate_init_data(sign_init_data({})) is None


def test_miniapp_user_header():
    class FakeRequest:
        headers = {"Authorization": "tma " + sign_init_data({"id": 7, "first_name": "F"})}

    assert main._miniapp_user(FakeRequest()) == {"id": 7, "first_name": "F"}


def test_miniapp_user_bad_scheme():
    class FakeRequest:
        headers = {"Authorization": "Bearer nope"}

    assert main._miniapp_user(FakeRequest()) is None


def test_miniapp_chat_id_no_cross_chat_fallback():
    """An unknown user must never be mapped onto someone else's chat.

    The old fallback returned the only chat in the store when the caller had no
    session of their own, so any valid Telegram user who had never messaged the
    bot was silently authorized as that chat's owner — able to read and add
    packages on another account. Callers 404/deny on None; _api_add creates the
    caller's own session via its existing None branch instead.
    """
    saved = dict(main.track_sessions)
    try:
        main.track_sessions.clear()
        assert main._miniapp_chat_id(42) is None            # nothing tracked
        main.track_sessions[999] = {"X": {"name": "p"}}     # exactly one other chat
        assert main._miniapp_chat_id(42) is None, \
            "an unknown user must not inherit the only chat in the store"
        assert main._miniapp_chat_id(999) == 999            # its real owner still wins
        main.track_sessions[42] = {}
        assert main._miniapp_chat_id(42) == 42              # own chat wins
    finally:
        main.track_sessions.clear()
        main.track_sessions.update(saved)


def test_unknown_user_gets_an_empty_dashboard_not_another_chats():
    """/api/packages for a valid user with no session of their own: no chat_id,
    no packages. This is the data the old fallback handed to any stranger."""
    import miniapp

    class FakeRequest:
        headers = {"Authorization": "tma " + sign_init_data({"id": 42, "first_name": "Eve"})}

    saved = dict(main.track_sessions)
    try:
        main.track_sessions.clear()
        main.track_sessions[999] = {"X": {"name": "p", "status": "Pending"}}
        resp = asyncio.run(miniapp._api_packages(FakeRequest()))
        data = json.loads(resp.text)
        assert data["chat_id"] is None, "an unknown user must not get a chat id"
        assert data["packages"] == [], "another chat's packages leaked"
        assert data["pro"] is False
    finally:
        main.track_sessions.clear()
        main.track_sessions.update(saved)


def test_package_payload_shape():
    payload = main._package_payload(
        "9400111899560000000000",
        {"name": "Box", "status": "Delivered", "carrier_name": "USPS",
         "carrier_url": "https://example.com", "muted": True},
    )
    assert payload["name"] == "Box"
    assert payload["muted"] is True
    assert "last_checked_ago" in payload


def test_package_payload_carries_next_check_eta():
    """The dashboard's 'next ~4m' meta reads next_check_in; without a chat
    (or without a scheduled due time) it must be None, never a guess."""
    payload = main._package_payload(
        "9400111206200074888350",
        {"name": "Box", "status": "In Transit", "carrier_name": "USPS"},
    )
    assert payload["next_check_in"] is None   # no chat context → no ETA
    payload = main._package_payload(
        "9400111206200074888350",
        {"name": "Box", "status": "In Transit", "carrier_name": "USPS"},
        chat_id=1,
    )
    assert "next_check_in" in payload


def test_package_payload_carries_register_blocked():
    """Mini App must know when a number is awaiting its first carrier scan —
    the dashboard renders an explanatory note from this flag (see
    webapp/index.html .blocked-note)."""
    payload = main._package_payload(
        "9400111206200074888350",
        {"name": "Label", "status": "Pending", "carrier_name": "USPS",
         "register_blocked": True},
    )
    assert payload["status"] == "Pending"
    assert payload["register_blocked"] is True
    payload2 = main._package_payload(
        "9400111206200074888350",
        {"name": "Label", "status": "In Transit", "carrier_name": "USPS"},
    )
    assert payload2["register_blocked"] is False


def test_manual_refresh_is_gone():
    """Manual refresh was removed: the global adaptive loop owns updating, and
    no endpoint or view may offer a way around it."""
    import miniapp
    app = miniapp._build_miniapp_app()
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/api/refresh" not in paths
    assert "/api/packages" in paths          # the dashboard still reads
    assert "/api/international" in paths
    assert not hasattr(miniapp, "_api_refresh")
    assert not hasattr(miniapp, "_refresh_denied")


def test_packages_payload_carries_the_health_status_object(monkeypatch):
    """/api/packages must include the health strip's data for the OPERATOR —
    the footer strip renders exactly these keys. Everyone else gets only the
    cadence hint: quota headroom, Ship24 spend and sweep stats describe the
    bot's global load and are nobody else's business."""
    import miniapp

    class FakeRequest:
        headers = {"Authorization": "tma " + sign_init_data({"id": 7, "first_name": "F"})}

    saved = dict(main.track_sessions)
    saved_owners = set(config.OWNER_CHAT_IDS)
    try:
        monkeypatch.setattr(config, "OWNER_CHAT_IDS", {7})
        main.track_sessions.clear()
        main.track_sessions[7] = {"X": {"name": "p", "status": "Pending"}}
        resp = asyncio.run(miniapp._api_packages(FakeRequest()))
        data = json.loads(resp.text)
        st = data["status"]
        for key in ("last_cycle_age_s", "next_check_in", "due_count",
                    "sweep", "quota", "ship24_today", "ship24_max",
                    "ship24_paused"):
            assert key in st, f"health strip key missing: {key}"
        assert st["ship24_max"] >= 0
        assert "next_check_in" in data["packages"][0]

        # A non-owner's payload carries no operational counters at all —
        # only the cadence hint the client paces its polling with.
        monkeypatch.setattr(config, "OWNER_CHAT_IDS", set())
        resp = asyncio.run(miniapp._api_packages(FakeRequest()))
        data = json.loads(resp.text)
        assert data["status"] is None
        assert "next_check_in" in data  # the cadence hint, value may be None
        assert "quota" not in data and "sweep" not in data
        assert "ship24_today" not in data and "due_count" not in data
    finally:
        main.track_sessions.clear()
        main.track_sessions.update(saved)
        config.OWNER_CHAT_IDS.clear()
        config.OWNER_CHAT_IDS.update(saved_owners)


def test_packages_payload_carries_retention_window():
    """The dashboard needs the server's retention window to show the
    "auto-forgets in N days" note on delivered cards."""
    import inspect
    import miniapp
    src = inspect.getsource(miniapp._api_packages)
    assert '"retention_days"' in src and "RETENTION_DAYS" in src


def test_packages_payload_carries_the_tier_facts_the_sheet_shows():
    """The Pro sheet's numbers ride along with every dashboard load.

    Without this the sheet could only ever show its baked-in fallback — which is
    exactly how it came to advertise 30/60-minute delivery-day checks long after
    the poller had moved to 10/30.
    """
    import config
    import miniapp
    import refresh

    class FakeRequest:
        headers = {"Authorization": "tma " + sign_init_data({"id": 7, "first_name": "F"})}

    saved = dict(main.track_sessions)
    try:
        main.track_sessions.clear()
        main.track_sessions[7] = {"X": {"name": "p", "status": "Pending"}}
        resp = asyncio.run(miniapp._api_packages(FakeRequest()))
        facts = json.loads(resp.text)["tier_facts"]
    finally:
        main.track_sessions.clear()
        main.track_sessions.update(saved)
    cad = refresh.CADENCE
    assert facts == {
        "free_packages": config.FREE_PACKAGE_LIMIT,
        "pro_packages": config.PRO_PACKAGE_LIMIT,
        "free_transit_min": cad["In Transit"][1] // 60,
        "pro_transit_min": cad["In Transit"][0] // 60,
        "free_ofd_min": cad["Out for Delivery"][1] // 60,
        "pro_ofd_min": cad["Out for Delivery"][0] // 60,
        "intl_included": config.PRO_INTL_LIMIT,
    }


# ── API rate limiting (the backend is publicly reachable) ──────────────
# initData proves who the caller is, not that they are being reasonable: a
# replayed initData could otherwise loop /api/check_payment (each poll hits the
# Solana RPC) or farm /api/create_invoice. The payment sheet polls every 5 s, so
# the limit has to sit well above 12 calls/minute.
class _ApiReq:
    """Minimal request stand-in for the middleware: path + headers only."""

    def __init__(self, path, headers=None):
        self.path = path
        self.headers = headers or {}


def _api_headers(user_id=42):
    signed = sign_init_data({"id": user_id, "first_name": "Fred"})
    return {"Authorization": "tma " + signed}


async def _pass_through(request):
    return "handled"


def _call_middleware(path, headers):
    import miniapp
    return asyncio.run(miniapp._api_rate_limit(_ApiReq(path, headers), _pass_through))


@pytest.fixture(autouse=True)
def _clean_rate_state(monkeypatch):
    import miniapp
    monkeypatch.setattr(miniapp.config, "MINIAPP_RATE_MAX", 3)
    monkeypatch.setattr(miniapp.config, "MINIAPP_RATE_WINDOW", 60.0)
    miniapp._rate_hits.clear()
    yield
    miniapp._rate_hits.clear()


def test_rate_limit_blocks_a_looping_client():
    results = [_call_middleware("/api/check_payment", _api_headers())
               for _ in range(5)]
    assert results[:3] == ["handled"] * 3          # the budget is honoured
    assert results[3].status == 429                # then it is refused, not queued
    assert results[4].status == 429


def test_rate_limit_is_per_user():
    for _ in range(3):
        assert _call_middleware("/api/packages", _api_headers(42)) == "handled"
    assert _call_middleware("/api/packages", _api_headers(42)).status == 429
    # A different user has their own budget — one client can't lock anyone out.
    assert _call_middleware("/api/packages", _api_headers(99)) == "handled"


def test_rate_limit_table_stays_bounded_across_many_users():
    """The sliding-window table is keyed by USER, so it grew with the user base:
    a user who opened the app once left a key behind forever, because the
    timestamps inside it were pruned but the key never was (100k one-time users
    ≈ 16 MB of resident memory for windows that expired minutes ago).

    _pay_tx_allowed already capped its sibling table for exactly this reason.
    """
    import miniapp
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(miniapp, "_RATE_USERS_MAX", 50)
        for uid in range(5000):                  # far more users than the cap
            miniapp._rate_limited(uid)
        assert len(miniapp._rate_hits) <= 51, (
            f"the table kept {len(miniapp._rate_hits)} users; it must stay bounded")
    finally:
        monkey.undo()


def test_bounding_the_table_does_not_bypass_the_budget():
    """Dropping the table must relax a throttle, never defeat one: a caller who
    is genuinely over budget still gets refused after the clear."""
    import miniapp
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(miniapp.config, "MINIAPP_RATE_MAX", 3)
        monkey.setattr(miniapp.config, "MINIAPP_RATE_WINDOW", 60.0)
        monkey.setattr(miniapp, "_RATE_USERS_MAX", 2)
        for uid in range(200):                   # overflow the table repeatedly
            miniapp._rate_limited(uid)
        # Now exhaust one user's budget and confirm the refusal still holds.
        verdicts = [miniapp._rate_limited(7) for _ in range(6)]
        assert verdicts[:3] == [False] * 3
        assert verdicts[3] is True and verdicts[4] is True, (
            "clearing the table must not hand an over-budget caller a fresh window")
    finally:
        monkey.undo()


def test_rate_limit_lets_the_window_expire(monkeypatch):
    import miniapp
    monkeypatch.setattr(miniapp.config, "MINIAPP_RATE_MAX", 1)
    monkeypatch.setattr(miniapp.config, "MINIAPP_RATE_WINDOW", 0.05)
    assert _call_middleware("/api/packages", _api_headers()) == "handled"
    assert _call_middleware("/api/packages", _api_headers()).status == 429
    time.sleep(0.1)                                 # window passes
    assert _call_middleware("/api/packages", _api_headers()) == "handled"


def test_rate_limit_ignores_unauthenticated_and_non_api_requests():
    """The views still own their 401, and a static page load costs no budget."""
    assert _call_middleware("/api/packages", {"Authorization": "tma junk"}) == "handled"
    for _ in range(10):
        assert _call_middleware("/", {}) == "handled"


def test_app_wires_the_rate_limit_middleware():
    import miniapp
    app = miniapp._build_miniapp_app()
    names = [getattr(m, "__name__", "") for m in app._middlewares]
    assert names == ["_api_no_store", "_api_rate_limit"], "a route added later must inherit the limit and the no-store guard"

def test_no_store_middleware_sets_cache_headers():
    from aiohttp import web as aioweb
    import miniapp

    async def handler(request):
        return aioweb.json_response({"ok": True})

    resp = asyncio.run(miniapp._api_no_store(_ApiReq("/api/packages", _api_headers()), handler))
    assert resp.headers["Cache-Control"] == "no-store, must-revalidate"
    assert resp.headers["Pragma"] == "no-cache"


class _WebhookReq:
    """Minimal request stand-in for the webhook handler: a readable body."""

    def __init__(self, body: bytes):
        self._body = body
        self.content_length = len(body)

    async def read(self):
        return self._body


def test_webhook_routes_are_registered():
    import miniapp
    app = miniapp._build_miniapp_app()
    paths = {r.resource.canonical: r.method for r in app.router.routes()}
    assert paths.get("/webhook/17track") in ("GET", "*") or any(
        "/webhook/17track" in p for p in paths)


def test_webhook_verify_answers_200():
    import miniapp
    resp = asyncio.run(miniapp._webhook_17track_verify(_WebhookReq(b"")))
    assert resp.status == 200


def test_webhook_post_answers_200_and_logs_only_structure(caplog):
    import logging as _logging
    import miniapp
    payload = json.dumps({
        "event": "track_push",
        "data": {"num": "9400111206200074888350", "status": "Delivered",
                 "secret_field": "hunter2"},
        "items": [{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}],
    }).encode()
    with caplog.at_level(_logging.INFO, logger="tracker"):
        resp = asyncio.run(miniapp._webhook_17track(_WebhookReq(payload)))
    assert resp.status == 200
    captured = [r for r in caplog.records if "webhook capture" in r.getMessage()]
    assert captured, "the payload structure must be captured"
    line = captured[0].getMessage()
    assert "num" in line and "str(22)" in line and "secret_field" in line
    # The masked shape carries types and sizes — never values.
    assert "9400111206200074888350" not in line and "hunter2" not in line
    assert "Delivered" not in line


def test_webhook_survives_malformed_payload():
    import miniapp
    resp = asyncio.run(miniapp._webhook_17track(_WebhookReq(b"<not json>")))
    assert resp.status == 200


def test_webhook_marks_a_known_number_due(monkeypatch):
    import miniapp
    import refresh
    import storage
    tid = "9400111206200074888350"
    storage.track_sessions.clear()
    refresh._due_at.clear()
    refresh._webhook_kicked.clear()
    storage.track_sessions[7] = {tid: {"name": "Box", "status": "In Transit"}}
    # 17TRACK normalizes case — the match must be case-insensitive.
    payload = json.dumps({"data": {"num": tid.lower()}}).encode()
    try:
        resp = asyncio.run(miniapp._webhook_17track(_WebhookReq(payload)))
        assert resp.status == 200
        assert refresh._due_at.get((7, tid)) == 0.0, "the loop must pick it up next tick"
    finally:
        storage.track_sessions.clear()
        refresh._due_at.clear()
        refresh._webhook_kicked.clear()


def test_webhook_marks_every_chat_holding_the_number():
    import miniapp
    import refresh
    import storage
    tid = "1Z1234567890123456"
    storage.track_sessions.clear()
    refresh._due_at.clear()
    refresh._webhook_kicked.clear()
    storage.track_sessions[7] = {tid: {"name": "A", "status": "In Transit"}}
    storage.track_sessions[9] = {tid: {"name": "A", "status": "In Transit"}}
    payload = json.dumps({"num": tid}).encode()
    try:
        asyncio.run(miniapp._webhook_17track(_WebhookReq(payload)))
        # Both chats, asserted on the effect rather than a reported count.
        assert refresh._due_at[(7, tid)] == 0.0
        assert refresh._due_at[(9, tid)] == 0.0
    finally:
        storage.track_sessions.clear()
        refresh._due_at.clear()
        refresh._webhook_kicked.clear()


def test_webhook_ignores_numbers_we_do_not_track():
    """Anti-spoof: the URL carries no token, so an unauthenticated POST must not
    be able to invent work — unknown numbers are counted, never scheduled."""
    import miniapp
    import refresh
    import storage
    storage.track_sessions.clear()
    refresh._due_at.clear()
    refresh._webhook_kicked.clear()
    payload = json.dumps({"num": "1Z9999999999999999",
                          "note": "9400111206200074888350"}).encode()
    try:
        asyncio.run(miniapp._webhook_17track(_WebhookReq(payload)))
        assert refresh._due_at == {}
    finally:
        storage.track_sessions.clear()
        refresh._due_at.clear()
        refresh._webhook_kicked.clear()


def test_webhook_respects_min_reschedule_interval(monkeypatch):
    """A hit must not re-arm the same package in a loop.

    The webhook URL carries no secret, so without a floor anyone who knows a
    tracked number can keep every lookup permanently due — bypassing the cadence
    and spending the paid fallbacks.
    """
    import miniapp
    import refresh
    import storage
    tid = "1Z999AA10123456784"
    storage.track_sessions.clear()
    refresh._due_at.clear()
    refresh._webhook_kicked.clear()
    storage.track_sessions[7] = {tid: {"name": "Box", "status": "In Transit"}}
    payload = json.dumps({"num": tid}).encode()
    clock = [1000.0]
    monkeypatch.setattr(refresh.time, "monotonic", lambda: clock[0])
    try:
        asyncio.run(miniapp._webhook_17track(_WebhookReq(payload)))
        assert refresh._due_at[(7, tid)] == 0.0

        # The lookup re-derives the cadence; a second hit inside the floor must
        # leave that re-derived time alone.
        refresh._due_at[(7, tid)] = 999.0
        asyncio.run(miniapp._webhook_17track(_WebhookReq(payload)))
        assert refresh._due_at[(7, tid)] == 999.0, "a throttled hit moved the due time"

        # Past the floor it is due again.
        clock[0] += refresh.WEBHOOK_MIN_RESCHEDULE_SECONDS + 1
        asyncio.run(miniapp._webhook_17track(_WebhookReq(payload)))
        assert refresh._due_at[(7, tid)] == 0.0
    finally:
        storage.track_sessions.clear()
        refresh._due_at.clear()
        refresh._webhook_kicked.clear()


def test_webhook_skips_terminal_packages():
    """A delivered package has nothing left to learn, so the webhook must not
    pull it back into the loop — that is pure quota spend."""
    import miniapp
    import refresh
    import storage
    tid = "9400111206200074888350"
    storage.track_sessions.clear()
    refresh._due_at.clear()
    refresh._webhook_kicked.clear()
    storage.track_sessions[7] = {tid: {"name": "Box", "status": "Delivered"}}
    refresh._due_at[(7, tid)] = float("inf")      # what the cadence set
    payload = json.dumps({"num": tid}).encode()
    try:
        asyncio.run(miniapp._webhook_17track(_WebhookReq(payload)))
        assert refresh._due_at[(7, tid)] == float("inf"), \
            "a delivered package was re-armed"
        assert (7, tid) not in refresh._webhook_kicked
    finally:
        storage.track_sessions.clear()
        refresh._due_at.clear()
        refresh._webhook_kicked.clear()


def test_webhook_response_has_no_per_number_oracle():
    """The reply must not let a caller probe which numbers this bot tracks.

    The endpoint is unauthenticated (17TRACK's console has no token field), so
    the anti-oracle property is not "the response contains no numbers" — it is
    that a HIT and a MISS are indistinguishable to the caller. The handler used
    to return `matched`, which made a single-candidate POST a membership probe
    over the whole tracking store; this test fails if any field that varies with
    whether a match occurred is ever returned again.
    """
    import miniapp
    import refresh
    import storage
    tracked = "9400111206200074888350"
    untracked = "1Z9999999999999999"
    storage.track_sessions.clear()
    refresh._due_at.clear()
    refresh._webhook_kicked.clear()
    storage.track_sessions[7] = {tracked: {"name": "Box", "status": "In Transit"}}
    try:
        # Same shape, one candidate number each: one this bot tracks, one it
        # does not. Both get scheduled-or-not on the server side, silently.
        hit = asyncio.run(miniapp._webhook_17track(
            _WebhookReq(json.dumps({"num": tracked}).encode())))
        refresh._webhook_kicked.clear()      # let the miss not be throttled
        miss = asyncio.run(miniapp._webhook_17track(
            _WebhookReq(json.dumps({"num": untracked}).encode())))

        assert hit.status == 200 and miss.status == 200
        body = json.loads(hit.body)
        # The only counts may be ones the caller could already know.
        assert set(body) == {"ok", "received"}, "unexpected response fields"
        assert all(isinstance(v, (int, bool)) for v in body.values())
        raw = hit.body.decode()
        assert tracked not in raw and untracked not in raw, "the response names numbers"
        # THE property: identical replies, so a hit cannot be distinguished.
        assert hit.body == miss.body, \
            "the response varies with whether the number was tracked — that is the oracle"
        # And the server side really did differ, or the test proves nothing.
        assert (7, tracked) in refresh._due_at
        assert (7, untracked) not in refresh._due_at
    finally:
        storage.track_sessions.clear()
        refresh._due_at.clear()
        refresh._webhook_kicked.clear()


def test_webhook_malformed_payload_schedules_nothing():
    import miniapp
    import refresh
    refresh._due_at.clear()
    try:
        resp = asyncio.run(miniapp._webhook_17track(_WebhookReq(b"<not json>")))
        assert resp.status == 200
        assert refresh._due_at == {}
    finally:
        refresh._due_at.clear()


def test_payload_shape_never_carries_values():
    import miniapp
    secret = "9400111206200074888350"
    shape = repr(miniapp._payload_shape(
        {"num": secret, "nested": {"deep": [secret, None, True, 1]}}))
    assert secret not in shape and "hunter2" not in shape
    assert "str(22)" in shape and "num" in shape and "null" in shape and "bool" in shape
    assert "…1 more" in shape, "long lists must be truncated, never dumped"


def test_concurrent_adds_respect_limit(monkeypatch):
    """20 adds at once for one chat with 5 free slots: exactly 5 land.

    The limit check and the insert are separated by a network await, so without
    the per-chat lock every one of the 20 passes the check and inserts — over the
    cap, and each insert burns a 17TRACK registration.
    """
    import miniapp
    import refresh

    class FakeRequest:
        def __init__(self, headers, body):
            self.headers = headers
            self._body = body

        async def json(self):
            return self._body

    chat, slots = 4401, 5
    monkeypatch.setattr(main.config, "FREE_PACKAGE_LIMIT", slots)
    headers = {"Authorization": "tma " + sign_init_data(
        {"id": chat, "first_name": "F"})}

    async def fake_register(tracking_id, is_pro=None, chat_id=None):
        await asyncio.sleep(0.01)          # hold the critical section open
        return ({"name": "Box", "status": "Pending", "carrier": "usps"}, "ok")

    monkeypatch.setattr(refresh, "_register_and_fetch", fake_register)
    monkeypatch.setattr(refresh, "save_now", lambda: None)

    saved = dict(main.track_sessions)
    main.track_sessions.clear()
    main.track_sessions[chat] = {}
    # Python 3.9 binds a Lock to the loop that created it, and this test makes a
    # new loop: start from a clean registry so the lock belongs to this loop.
    storage.chat_locks.clear()
    try:
        async def add_all():
            return await asyncio.gather(*[
                miniapp._api_add(FakeRequest(
                    headers, {"tracking_id": f"94001112062000748883{i:02d}"}))
                for i in range(20)])

        responses = asyncio.run(add_all())

        stored = list(main.track_sessions[chat])
        assert len(stored) == slots, f"stored {len(stored)}, expected {slots}"
        assert len(set(stored)) == slots, "a duplicate tracking id was stored"
        ok = [r for r in responses if r.status == 200]
        refused = [r for r in responses if r.status == 403]
        assert len(ok) == slots
        assert len(refused) == 20 - slots
        assert all(r.status != 500 for r in responses), "a race must not 500"
        assert all(json.loads(r.body).get("limit_reached") for r in refused), \
            "the losers must get the normal limit error"
    finally:
        main.track_sessions.clear()
        main.track_sessions.update(saved)
        storage.chat_locks.clear()


def test_every_response_pins_the_referrer_policy():
    """The dashboard links out to the carrier's page with the tracking number in
    the URL, so a click used to also send a Referer header naming our host. The
    link is a feature we keep; the referrer is not. Pinned here so the header
    cannot be dropped from the shared middleware."""
    import asyncio

    from aiohttp import web
    import miniapp

    async def handler(_request):
        return web.Response(text="ok")

    # The middleware ignores the request, so a stub is enough.
    resp = asyncio.run(miniapp._api_no_store(None, handler))
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    # The cache-kill behaviour it already had must survive the addition.
    assert resp.headers["Cache-Control"] == "no-store, must-revalidate"
    assert resp.headers["Pragma"] == "no-cache"


class TestShareEndpoint:
    """POST /api/share — the app's twin of the bot's Share button.

    The link is a bearer token for a PUBLIC page, so the endpoint's contract is
    the privacy-sensitive part: the caller must own the chat, the URL must never
    carry the tracking number, and minting again must KILL the previous link
    (one live link per package is the revoke story in the app, where there is no
    revoke button on a chat message).
    """

    TID = "9400111206200074888350"
    USER = 42
    APP = "https://app.example"

    class _Req:
        def __init__(self, headers, body):
            self.headers = headers
            self._body = body

        async def json(self):
            return self._body

    def _isolated(self, monkeypatch):
        """No share-token writes to the real sidecar, no other chats' state."""
        monkeypatch.setattr(share_links, "save", lambda: None)
        saved_tokens = dict(share_links._tokens)
        share_links._tokens.clear()
        saved_sessions = dict(storage.track_sessions)
        storage.track_sessions.clear()
        return saved_tokens, saved_sessions

    def _restore(self, saved):
        share_links._tokens.clear()
        share_links._tokens.update(saved[0])
        storage.track_sessions.clear()
        storage.track_sessions.update(saved[1])

    def test_requires_init_data(self, monkeypatch):
        saved = self._isolated(monkeypatch)
        try:
            resp = asyncio.run(miniapp._api_share(
                self._Req({}, {"tracking_id": self.TID})))
            assert resp.status == 401
        finally:
            self._restore(saved)

    def test_unknown_package_is_404(self, monkeypatch):
        saved = self._isolated(monkeypatch)
        try:
            resp = asyncio.run(miniapp._api_share(
                self._Req(_api_headers(self.USER),
                          {"tracking_id": self.TID})))
            assert resp.status == 404
        finally:
            self._restore(saved)

    def test_mints_a_url_that_never_carries_the_tracking_number(self, monkeypatch):
        saved = self._isolated(monkeypatch)
        try:
            monkeypatch.setattr(config, "MINI_APP_URL", self.APP)
            storage.track_sessions[self.USER] = {
                self.TID: {"name": "Mom's gift", "status": "In Transit"}}
            resp = asyncio.run(miniapp._api_share(
                self._Req(_api_headers(self.USER),
                          {"tracking_id": self.TID})))
            assert resp.status == 200
            body = json.loads(resp.body)
            assert body["url"].startswith(self.APP + "/share/")
            assert self.TID not in body["url"], \
                "the tracking number must never ride in the share URL"
            assert body["name"] == "Mom's gift"
            assert body["expires_in_days"] == config.SHARE_LINK_TTL_DAYS
            # The minted token really is live and bound to this chat + package.
            token = body["url"].rsplit("/", 1)[1]
            rec = share_links.lookup(token)
            assert rec and rec["chat_id"] == self.USER
            assert rec["tracking_id"] == self.TID
        finally:
            self._restore(saved)

    def test_sharing_twice_shows_the_same_link(self, monkeypatch):
        """The sheet SHOWS the link, so a second Share tap must NOT re-mint: a
        user who already sent the link would have it die under them. Idempotent
        by default; only the sheet's explicit New link re-mints."""
        saved = self._isolated(monkeypatch)
        try:
            monkeypatch.setattr(config, "MINI_APP_URL", self.APP)
            storage.track_sessions[self.USER] = {
                self.TID: {"name": "Box", "status": "In Transit"}}
            req = self._Req(_api_headers(self.USER), {"tracking_id": self.TID})
            first = json.loads(asyncio.run(miniapp._api_share(req)).body)
            second = json.loads(asyncio.run(miniapp._api_share(req)).body)
            assert first["url"] == second["url"], \
                "re-tapping Share must show the same live link"
            assert first["replaced"] is False and second["replaced"] is False
            assert share_links.lookup(first["url"].rsplit("/", 1)[1]) is not None
            assert len(share_links._tokens) == 1, "no link pile-up"
        finally:
            self._restore(saved)

    def test_refresh_mints_a_fresh_link_and_kills_the_old_one(self, monkeypatch):
        """The sheet's New link button: the escape hatch for a link the user
        thinks leaked. The old token must be dead, not merely superseded."""
        saved = self._isolated(monkeypatch)
        try:
            monkeypatch.setattr(config, "MINI_APP_URL", self.APP)
            storage.track_sessions[self.USER] = {
                self.TID: {"name": "Box", "status": "In Transit"}}
            req = self._Req(_api_headers(self.USER), {"tracking_id": self.TID})
            first = json.loads(asyncio.run(miniapp._api_share(req)).body)
            fresh = json.loads(asyncio.run(miniapp._api_share(
                self._Req(_api_headers(self.USER),
                          {"tracking_id": self.TID, "refresh": True}))).body)
            assert fresh["url"] != first["url"]
            assert fresh["replaced"] is True, "the response must own the kill"
            assert share_links.lookup(first["url"].rsplit("/", 1)[1]) is None, \
                "the old link must be dead after a refresh"
            assert share_links.lookup(fresh["url"].rsplit("/", 1)[1]) is not None
            assert len(share_links._tokens) == 1
        finally:
            self._restore(saved)

    def test_revoke_kills_the_link(self, monkeypatch):
        """The sheet's Revoke: the app's answer to the bot's revoke button —
        without it, a leaked link could only wait out its TTL."""
        saved = self._isolated(monkeypatch)
        try:
            monkeypatch.setattr(config, "MINI_APP_URL", self.APP)
            storage.track_sessions[self.USER] = {
                self.TID: {"name": "Box", "status": "In Transit"}}
            body = json.loads(asyncio.run(miniapp._api_share(
                self._Req(_api_headers(self.USER),
                          {"tracking_id": self.TID}))).body)
            token = body["url"].rsplit("/", 1)[1]
            assert share_links.lookup(token) is not None

            resp = asyncio.run(miniapp._api_share_revoke(
                self._Req(_api_headers(self.USER),
                          {"tracking_id": self.TID})))
            assert json.loads(resp.body) == {"revoked": True}
            assert share_links.lookup(token) is None, "revoked must mean dead"
            # Revoking again: nothing live — a plain answer, not an error.
            resp2 = asyncio.run(miniapp._api_share_revoke(
                self._Req(_api_headers(self.USER),
                          {"tracking_id": self.TID})))
            assert json.loads(resp2.body) == {"revoked": False}
        finally:
            self._restore(saved)

    def test_another_users_package_is_not_found(self, monkeypatch):
        """The chat scoping: user 99 must not mint a link for user 42's package."""
        saved = self._isolated(monkeypatch)
        try:
            monkeypatch.setattr(config, "MINI_APP_URL", self.APP)
            storage.track_sessions[42] = {self.TID: {"name": "Box"}}
            resp = asyncio.run(miniapp._api_share(
                self._Req(_api_headers(99),
                          {"tracking_id": self.TID})))
            assert resp.status == 404
            assert not share_links._tokens, "nothing may be minted"
        finally:
            self._restore(saved)

    def test_without_a_public_url_it_refuses_instead_of_minting_a_dead_link(
            self, monkeypatch):
        saved = self._isolated(monkeypatch)
        try:
            monkeypatch.setattr(config, "MINI_APP_URL", None)
            storage.track_sessions[self.USER] = {
                self.TID: {"name": "Box", "status": "In Transit"}}
            resp = asyncio.run(miniapp._api_share(
                self._Req(_api_headers(self.USER),
                          {"tracking_id": self.TID})))
            assert resp.status == 503
            assert not share_links._tokens
        finally:
            self._restore(saved)

    def test_the_minted_url_renders_the_share_page_without_the_number(
            self, monkeypatch):
        """End to end: the token the endpoint returned opens the public page, the
        page shows the name, and the tracking number appears NOWHERE in it."""
        saved = self._isolated(monkeypatch)
        try:
            monkeypatch.setattr(config, "MINI_APP_URL", self.APP)
            storage.track_sessions[self.USER] = {
                self.TID: {"name": "Blue headphones", "status": "In Transit",
                           "carrier_name": "USPS", "detail": "Departed the hub"}}
            body = json.loads(asyncio.run(miniapp._api_share(
                self._Req(_api_headers(self.USER),
                          {"tracking_id": self.TID}))).body)
            token = body["url"].rsplit("/", 1)[1]

            class _ShareReq:
                match_info = {"token": token}

            page = asyncio.run(miniapp._webapp_share(_ShareReq()))
            assert page.status == 200
            assert "Blue headphones" in page.text
            assert self.TID not in page.text, "the number must never reach the page"
        finally:
            self._restore(saved)

