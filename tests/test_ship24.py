"""Ship24 fallback: vocabulary mapping, paid-call discipline, and where the
tier sits in fetch_track_status.

Ship24 is the only source the bot pays for per lookup (~$0.05 at Ship24's
overage rate), so the expensive behaviours are what this file pins: it is asked
only after every free source has failed, a rejected key or a missing per-call
subscription pauses it instead of burning a call per cycle, the daily cap is
enforced, and an answer we don't recognise leaves the card's "Unavailable"
alone rather than inventing a status.
"""
import asyncio
import json
import time

import pytest

import carriers
import config
import main
import ship24
import storage

TID = "9400111206200074888350"


class _Resp:
    """Minimal httpx.Response stand-in (status + json + headers)."""

    def __init__(self, status=201, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _Client:
    """Records every paid call and answers with a canned response."""

    def __init__(self, resp=None):
        self.hits = []
        self._resp = resp if resp is not None else _Resp(201, _trackings("in_transit"))

    async def post(self, url, **kw):
        self.hits.append((url, kw))
        return self._resp


def _trackings(milestone, code=None, events=None):
    return {"data": {"trackings": [{
        "shipment": {"statusMilestone": milestone, "statusCode": code},
        "events": events if events is not None else [],
    }]}}


@pytest.fixture(autouse=True)
def _fresh_ship24(monkeypatch):
    """No key, no pause, full budget — the live .env key must not leak in."""
    monkeypatch.delenv("SHIP24_API_KEY", raising=False)
    monkeypatch.setattr(config, "SHIP24_DAILY_MAX", 50)
    ship24.reset_state()
    yield
    ship24.reset_state()


# ── Vocabulary ───────────────────────────────────────────────────────────
def test_every_milestone_maps_onto_our_statuses():
    expected = {
        "pending": "Pending",
        "info_received": "Info Received",
        "in_transit": "In Transit",
        "out_for_delivery": "Out for Delivery",
        "failed_attempt": "Delivery Attempted",
        "available_for_pickup": "Available for Pickup",
        "delivered": "Delivered",
        "exception": "Exception",
    }
    for milestone, status in expected.items():
        assert carriers._map_ship24_status(milestone) == status, milestone
    # the mapping is re-exported on main, like every other carriers name
    assert main._map_ship24_status is carriers._map_ship24_status


def test_mapping_tolerates_case_and_padding():
    assert carriers._map_ship24_status(" In_Transit ") == "In Transit"


def test_unknown_milestone_falls_back_to_the_status_code():
    assert carriers._map_ship24_status("brand_new", "delivery_delivered") == "Delivered"
    assert carriers._map_ship24_status("", "exception_return") == "Exception"


def test_unrecognised_answers_are_never_invented():
    """An unknown pair must leave the caller's 'Unavailable' in place."""
    assert carriers._map_ship24_status("weird_milestone", "weird_code") is None
    assert carriers._map_ship24_status(None, None) is None
    assert carriers._map_ship24_status("", "") is None


# ── Enabling ─────────────────────────────────────────────────────────────
def test_disabled_until_a_key_is_set(monkeypatch):
    assert ship24.enabled() is False
    monkeypatch.setenv("SHIP24_API_KEY", "  apik_trimmed  ")   # padding tolerated
    assert ship24.enabled() is True


def test_no_key_means_no_paid_call(monkeypatch):
    client = _Client()
    assert asyncio.run(ship24.check_ship24(TID, client)) is None
    assert client.hits == []


# ── The happy path ───────────────────────────────────────────────────────
def test_lookup_sends_the_documented_request_and_reads_the_newest_event(monkeypatch):
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    client = _Client(_Resp(201, _trackings(
        "out_for_delivery", "delivery_out_for_delivery",
        events=[{"status": "Out for Delivery", "occurrenceDatetime": "2026-01-01T10:20:06+01:00",
                 "order": None},
                {"status": "Arrived at hub", "occurrenceDatetime": "2026-01-01T06:00:00"}])))
    raw = asyncio.run(ship24.check_ship24(TID, client))

    assert raw == ("out_for_delivery", "delivery_out_for_delivery", "Out for Delivery")
    url, kw = client.hits[0]
    assert url == "https://api.ship24.com/public/v1/tracking/search"
    assert kw["json"] == {"trackingNumber": TID}
    assert kw["headers"]["Authorization"] == "Bearer apik_test"
    # the paid request must not be cut off by the bot-wide 18 s timeout
    assert kw["timeout"] == ship24.REQUEST_TIMEOUT > config.HTTP_TIMEOUT
    assert ship24.calls_today() == 1


def test_a_200_is_accepted_as_well_as_a_201(monkeypatch):
    """Ship24 answers 201 for a create-style call; tolerate 200 too."""
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    client = _Client(_Resp(200, _trackings("delivered")))
    assert asyncio.run(ship24.check_ship24(TID, client))[0] == "delivered"


def test_blank_shipment_is_rejected_but_missing_events_are_fine(monkeypatch):
    """No milestone AND no code means nothing to map; a bare milestone is enough."""
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    client = _Client(_Resp(201, {"data": {"trackings": [{"shipment": {}}]}}))
    assert asyncio.run(ship24.check_ship24(TID, client)) is None
    client2 = _Client(_Resp(201, _trackings("pending")))
    assert asyncio.run(ship24.check_ship24(TID, client2)) == ("pending", "", "")


# ── Failure discipline (this source costs money) ─────────────────────────
def test_rejected_key_pauses_instead_of_retrying(monkeypatch):
    monkeypatch.setenv("SHIP24_API_KEY", "apik_bad")
    client = _Client(_Resp(401, {"errors": [{"code": "auth_invalid_api_key"}],
                                "data": None}))
    assert asyncio.run(ship24.check_ship24(TID, client)) is None
    assert ship24.paused() is True
    # a dead key cannot fix itself this cycle, so no second request
    assert asyncio.run(ship24.check_ship24(TID, client)) is None
    assert len(client.hits) == 1


def test_missing_per_call_subscription_pauses_the_source(monkeypatch):
    """422 no_active_subscription is what a trackers-only account answers."""
    monkeypatch.setenv("SHIP24_API_KEY", "apik_trackers_only")
    client = _Client(_Resp(422, {"errors": [{"code": "no_active_subscription"}],
                                "data": None}))
    assert asyncio.run(ship24.check_ship24(TID, client)) is None
    assert ship24.paused() is True
    assert len(client.hits) == 1


def test_rate_limit_honours_retry_after(monkeypatch):
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    client = _Client(_Resp(429, {"errors": [{"code": "too_many_requests"}]},
                           headers={"Retry-After": "7"}))
    asyncio.run(ship24.check_ship24(TID, client))
    assert 5 < ship24._paused_until - time.time() <= 7


def test_server_error_does_not_pause_the_source(monkeypatch):
    """5xx is transient: try again next cycle instead of switching the tier off."""
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    client = _Client(_Resp(503, None))
    assert asyncio.run(ship24.check_ship24(TID, client)) is None
    assert ship24.paused() is False


def test_transport_error_is_not_fatal(monkeypatch):
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")

    class _Boom:
        hits = []

        async def post(self, url, **kw):
            _Boom.hits.append(url)
            raise RuntimeError("network down")

    assert asyncio.run(ship24.check_ship24(TID, _Boom())) is None
    assert len(_Boom.hits) == 1


def test_malformed_bodies_never_raise(monkeypatch):
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    payloads = [{}, {"data": None}, {"data": {}}, {"data": {"trackings": []}},
                {"data": {"trackings": [None]}},
                {"data": {"trackings": [{"shipment": None}]}},
                {"data": {"trackings": [{"shipment": {"statusMilestone": ""}}]}}]
    for payload in payloads:
        client = _Client(_Resp(201, payload))
        assert asyncio.run(ship24.check_ship24(TID, client)) is None, payload

    class _BadJson(_Resp):
        def json(self):
            raise ValueError("not json")

    assert asyncio.run(ship24.check_ship24(TID, _Client(_BadJson(201)))) is None


# ── The daily budget ─────────────────────────────────────────────────────
def test_daily_cap_stops_the_spending(monkeypatch):
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    monkeypatch.setattr(config, "SHIP24_DAILY_MAX", 2)
    client = _Client()
    for _ in range(3):
        asyncio.run(ship24.check_ship24(TID, client))
    assert len(client.hits) == 2
    assert ship24.calls_today() == 2


def test_zero_cap_disables_the_lookup_without_removing_the_key(monkeypatch):
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    monkeypatch.setattr(config, "SHIP24_DAILY_MAX", 0)
    client = _Client()
    assert asyncio.run(ship24.check_ship24(TID, client)) is None
    assert client.hits == []


def test_counter_survives_a_restart(monkeypatch):
    """A paid-call cap must not reset when the process restarts — a deploy would
    hand the whole day's budget back, which is exactly when a stuck package gets
    retried against the paid source."""
    import importlib
    from datetime import date

    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    client = _Client(_Resp(201, _trackings("in_transit")))
    asyncio.run(ship24.check_ship24(TID, client))
    assert ship24.calls_today() == 1
    # Encrypted at rest, like every other state file.
    assert config.SHIP24_COUNTER_FILE.read_bytes().startswith(b"gAAAA"), \
        "the paid-call counter must be encrypted at rest"
    assert json.loads(
        storage._read_protected(config.SHIP24_COUNTER_FILE)[0]) == {
        "day": date.today().isoformat(), "calls": 1}

    # Restart: memory gone, the count has to come back from disk.
    ship24._calls_made, ship24._calls_day = 0, None
    importlib.reload(ship24)

    assert ship24.calls_today() == 1, "the day's spend was forgotten on restart"


def test_loading_the_counter_never_writes(monkeypatch):
    """The loader runs at import time — during test collection too — so it must
    not write: a write there lands on the live path before any fixture has
    redirected it. A plaintext counter from the pre-encryption version is
    upgraded by the next paid call instead."""
    import importlib
    from datetime import date

    config.SHIP24_COUNTER_FILE.write_text(
        json.dumps({"day": date.today().isoformat(), "calls": 4}))
    before = config.SHIP24_COUNTER_FILE.read_bytes()

    ship24._calls_made, ship24._calls_day = 0, None
    importlib.reload(ship24)

    assert ship24.calls_today() == 4, "the legacy count must still load"
    assert config.SHIP24_COUNTER_FILE.read_bytes() == before, \
        "loading must not write the counter file"

    # The next save rewrites it encrypted — that is the migration.
    ship24._save_counter()
    assert config.SHIP24_COUNTER_FILE.read_bytes().startswith(b"gAAAA")
    assert json.loads(
        storage._read_protected(config.SHIP24_COUNTER_FILE)[0]) == {
        "day": date.today().isoformat(), "calls": 4}


def test_a_stale_counter_day_resets_on_load(monkeypatch):
    """Yesterday's spend is not today's: the day rolls over when it is loaded."""
    import importlib
    from datetime import date, timedelta

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    storage._write_protected(config.SHIP24_COUNTER_FILE,
                             json.dumps({"day": yesterday, "calls": 49}))
    ship24._calls_made, ship24._calls_day = 99, "stale-in-memory"

    importlib.reload(ship24)

    assert ship24.calls_today() == 0, "yesterday's spend must not eat today's cap"
    assert ship24._calls_day is None, "the stale day must not be carried over"


def test_over_cap_is_still_refused_after_a_restart(monkeypatch):
    """The cap is what the counter exists for: a persisted full day refuses."""
    import importlib
    from datetime import date

    monkeypatch.setattr(config, "SHIP24_DAILY_MAX", 3)
    storage._write_protected(config.SHIP24_COUNTER_FILE,
                             json.dumps({"day": date.today().isoformat(),
                                         "calls": 3}))
    importlib.reload(ship24)
    assert ship24.calls_today() == 3, "the persisted spend must load back"

    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    client = _Client(_Resp(201, _trackings("in_transit")))
    assert asyncio.run(ship24.check_ship24(TID, client)) is None
    assert client.hits == [], "a spent day must not buy another call"


# ── Where it sits in fetch_track_status ──────────────────────────────────
def _no_free_sources(monkeypatch):
    """Free tiers off or empty — the way a stuck number sees them."""
    monkeypatch.setattr(carriers.config, "track17_api_key", "default_api_key")
    monkeypatch.setattr(carriers.config, "USPS_API_USERID", None)
    monkeypatch.setattr(carriers.config, "USPS_BROWSER_TRACK", False)


def test_paid_fallback_answers_when_every_free_source_failed(monkeypatch):
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    _no_free_sources(monkeypatch)
    before = carriers.LOOKUP_OK["Ship24"]
    monkeypatch.setattr(carriers, "http_client", lambda: _Client(_Resp(
        201, _trackings("in_transit", events=[{"status": "Departed facility"}]))))

    status, _carrier, source, detail = asyncio.run(
        carriers.fetch_track_status("RR123456789CN"))

    assert status == "In Transit"
    assert source == "Ship24"
    assert detail == "Departed facility"
    assert carriers.LOOKUP_OK["Ship24"] == before + 1


def test_paid_fallback_stays_out_of_the_way_when_17track_answers(monkeypatch):
    """The point of the cap: never spend a billed call on a number a free
    source could already read."""
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    monkeypatch.setattr(carriers.config, "track17_api_key", "k")

    async def _fake_17track(tid):
        return "Delivered", None

    monkeypatch.setattr(carriers, "_check_17track", _fake_17track)
    paid = _Client(_Resp(201, _trackings("in_transit")))
    monkeypatch.setattr(carriers, "http_client", lambda: paid)

    status, _carrier, source, _detail = asyncio.run(carriers.fetch_track_status(TID))
    assert (status, source) == ("Delivered", "17TRACK")
    assert paid.hits == []


def test_no_paid_fallback_when_the_key_is_absent(monkeypatch):
    """The autouse fixture removes the key, so this is the shipped default."""
    _no_free_sources(monkeypatch)
    paid = _Client()
    monkeypatch.setattr(carriers, "http_client", lambda: paid)

    status, _carrier, source, _detail = asyncio.run(carriers.fetch_track_status(TID))
    assert status == "Unavailable"
    assert source == "None"
    assert paid.hits == []


# ── Who may spend a paid call (decision 2026-09-23) ───────────────────────
def test_a_free_chat_never_buys_a_ship24_call(monkeypatch):
    """Free chats must not spend the owner's money: when every free tier comes up
    empty the answer is Unavailable plus a recheck, not a billed lookup."""
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    _no_free_sources(monkeypatch)
    paid = _Client(_Resp(201, _trackings("in_transit")))
    monkeypatch.setattr(carriers, "http_client", lambda: paid)

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(TID, is_pro=False))

    assert (status, source) == ("Unavailable", "None")
    assert paid.hits == [], "a free chat bought a paid lookup"


def test_a_pro_chat_still_gets_the_paid_fallback(monkeypatch):
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    _no_free_sources(monkeypatch)
    paid = _Client(_Resp(201, _trackings("in_transit")))
    monkeypatch.setattr(carriers, "http_client", lambda: paid)

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(TID, is_pro=True))

    assert status == "In Transit" and source == "Ship24"
    assert paid.hits, "a Pro chat must still get the last-resort tier"


def test_an_unknown_tier_keeps_the_old_behaviour(monkeypatch):
    """is_pro=None means "tier unknown" (legacy callers) — the paid tier stays
    available, exactly like the _PRO_GATED_SLUGS gate that also tests `is False`."""
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    _no_free_sources(monkeypatch)
    paid = _Client(_Resp(201, _trackings("in_transit")))
    monkeypatch.setattr(carriers, "http_client", lambda: paid)

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(TID, is_pro=None))

    assert status == "In Transit" and source == "Ship24"
    assert paid.hits
