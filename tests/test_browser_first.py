"""Pro headless-first routing (PRO_BROWSER_FIRST, on by default).

The live failure this pins: 17TRACK's aggregated feed still showed a UPS parcel
in transit ~5 hours after UPS itself had marked it delivered. A carrier-direct
read cannot lag the carrier, so a Pro chat's big four (UPS/FedEx/DHL) are read on
the carrier's own page BEFORE 17TRACK spends a lookup, and 17TRACK becomes the
fallback for a page that is blocked or finds nothing.

USPS is the deliberate exception in both tiers: its official API is the carrier's
own feed — faster than scraping, unblockable, and it reports pre-first-scan
labels — so the browser never precedes it. For Pro USPS the browser slots in
after the official APIs and before 17TRACK, which is what `official_empty` in
carriers._use_17track unlocks: once the official tier answered nothing, the rule
that keeps 17TRACK away from US domestic numbers no longer has a reason.

Everything is behind the PRO_BROWSER_FIRST kill switch: 0 restores the
17TRACK-first order exactly, which is the revert path if Akamai starts eating
lookups. Free chats and is_pro=None keep their old order in every case.
"""
import asyncio

import carriers

UPS = "1Z1234567890123456"       # ups — browser-capable, headless-first for Pro
USPS = "9400111206200074888350"  # usps — official API first, browser after it


class _Recorder:
    """Every tier's answer, plus who was asked."""

    def __init__(self):
        self.track17 = []
        self.browser = []
        self.ship24 = []
        self.api = []
        self.webtools = []
        self.order = []            # tier names in the order they were asked
        self.browser_result = ("Out for Delivery", "out for delivery now")
        self.track17_status = "In Transit"
        self.api_result = ("Delivered", "delivered")
        self.webtools_result = None
        self.rate_limited = False


def _install(monkeypatch, api_enabled=False, userid=None, ondemand=False,
             browser_first=True):
    """Wire all five tiers to recorders.

    Every knob these tests depend on is set explicitly — the live .env must never
    decide what a routing test asserts (USPS_BROWSER_ONDEMAND is on in production,
    which would silently skip the browser tier these tests are about).
    """
    rec = _Recorder()
    monkeypatch.setattr(carriers.config, "track17_api_key", "k-test")
    monkeypatch.setattr(carriers.config, "USPS_API_USERID", userid)
    monkeypatch.setattr(carriers.config, "USPS_BROWSER_TRACK", True)
    monkeypatch.setattr(carriers.config, "USPS_BROWSER_ONDEMAND", ondemand)
    monkeypatch.setattr(carriers.config, "PRO_BROWSER_FIRST", browser_first)
    monkeypatch.setattr(carriers, "SOURCE_ROUTING", "matrix")
    monkeypatch.setattr(carriers, "quota_pct", lambda: None)
    monkeypatch.setattr(carriers.usps_api, "enabled", lambda: api_enabled)

    async def fake_17track(tid):
        rec.track17.append(tid)
        rec.order.append("17track")
        if rec.rate_limited:
            raise carriers.Track17RateLimited("429")
        if rec.track17_status is None:
            return None, None
        return rec.track17_status, {
            "carrier": {"name": "UPS", "key": "k", "country": "US"},
            "detail": "moving"}

    monkeypatch.setattr(carriers, "_check_17track", fake_17track)

    async def fake_browser(tid, slug):
        rec.browser.append((tid, slug))
        rec.order.append("browser")
        return rec.browser_result

    monkeypatch.setattr(carriers, "_check_browser", fake_browser)

    async def fake_api(tid, client):
        rec.api.append(tid)
        rec.order.append("api")
        return rec.api_result

    monkeypatch.setattr(carriers.usps_api, "check_usps", fake_api)

    async def fake_webtools(tid):
        rec.webtools.append(tid)
        rec.order.append("webtools")
        return rec.webtools_result

    monkeypatch.setattr(carriers, "_check_usps_webtools", fake_webtools)
    monkeypatch.setattr(carriers.ship24, "enabled", lambda: True)

    async def fake_ship24(tid, client):
        rec.ship24.append(tid)
        rec.order.append("ship24")
        return ("delivered", "delivery_delivered", "Delivered, Left at front door")

    monkeypatch.setattr(carriers.ship24, "check_ship24", fake_ship24)
    return rec


# ── Pro big four: carrier page → 17TRACK ──────────────────────────────────
def test_pro_big_four_reads_the_carrier_page_before_17track(monkeypatch):
    rec = _install(monkeypatch)
    ok_before = carriers.LOOKUP_OK["UPS Browser"]

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(UPS, is_pro=True))

    assert rec.browser == [(UPS, "ups")]
    assert rec.order == ["browser"], "the carrier's page is the first tier asked"
    assert (status, source) == ("Out for Delivery", "UPS Browser")
    assert rec.track17 == [], "17TRACK was spent on a number the carrier answered"
    assert carriers.LOOKUP_OK["UPS Browser"] == ok_before + 1, \
        "the new first tier must be tallied like every other one"


def test_pro_big_four_falls_back_to_17track_when_the_page_finds_nothing(monkeypatch):
    rec = _install(monkeypatch)
    rec.browser_result = None          # blocked by Akamai, or a page with no result

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(UPS, is_pro=True))

    assert rec.browser == [(UPS, "ups")], "the browser is still tried first"
    assert rec.track17 == [UPS], "an empty page must not lose the 17TRACK fallback"
    assert (status, source) == ("In Transit", "17TRACK")
    assert rec.browser.count((UPS, "ups")) == 1, "one browser read per round"


def test_on_demand_mode_cannot_neuter_the_pro_big_four_browser(monkeypatch):
    """USPS_BROWSER_ONDEMAND keeps an OCCASIONAL tier quiet. For a Pro big-4
    number the browser IS the primary tier, so the background cycle must still
    read it — otherwise a sweep would never see these numbers at all."""
    rec = _install(monkeypatch, ondemand=True)

    _status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(UPS, is_pro=True))

    assert rec.browser == [(UPS, "ups")] and source == "UPS Browser"


# ── Pro USPS: official APIs → browser → 17TRACK ───────────────────────────
def test_pro_usps_keeps_the_official_api_ahead_of_the_browser(monkeypatch):
    rec = _install(monkeypatch, api_enabled=True)

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(USPS, is_pro=True))

    assert (status, source) == ("Delivered", "USPS API")
    assert rec.order == ["api"], "the official API is the only tier that runs"
    assert rec.browser == [], "the carrier's own API is faster than scraping it"
    assert rec.track17 == [], "an official API serves this number"


def test_pro_usps_reaches_17track_once_every_official_tier_came_up_empty(monkeypatch):
    """The `official_empty` override: API v3, Web Tools and the browser have all
    been asked and answered nothing, so the official-API rule that normally keeps
    17TRACK away from US domestic numbers has no reason left — and a free lookup
    beats a billed one."""
    rec = _install(monkeypatch, api_enabled=True, userid="TESTUSERID")
    rec.api_result = None
    rec.browser_result = None

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(USPS, is_pro=True))

    assert rec.order == ["api", "webtools", "browser", "17track"], \
        "the order is API v3 → Web Tools → browser → 17TRACK"
    assert (rec.api, rec.webtools, rec.browser) == ([USPS], [USPS], [(USPS, "usps")])
    assert rec.track17 == [USPS], "17TRACK must be allowed as the fallback"
    assert (status, source) == ("In Transit", "17TRACK")
    assert rec.ship24 == [], "a free tier answered — no paid call was needed"


def test_pro_usps_never_asks_17track_before_the_browser(monkeypatch):
    """A Pro USPS number with no official API configured: the browser is still
    the tier that comes first, so 17TRACK's call is deferred behind it."""
    rec = _install(monkeypatch)
    rec.browser_result = None

    _status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(USPS, is_pro=True))

    assert rec.browser == [(USPS, "usps")] and rec.track17 == [USPS]
    assert rec.order == ["browser", "17track"], "17TRACK is deferred behind the browser"
    assert source == "17TRACK"


# ── The kill switch ───────────────────────────────────────────────────────
def test_the_kill_switch_restores_the_17track_first_order(monkeypatch):
    rec = _install(monkeypatch, browser_first=False)

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(UPS, is_pro=True))

    assert rec.order == ["17track"]
    assert (status, source) == ("In Transit", "17TRACK")
    assert rec.browser == [], "PRO_BROWSER_FIRST=0 must leave the browser where it was"


def test_the_kill_switch_also_disables_the_usps_override(monkeypatch):
    """The override is part of the reorder, not a separate behavior: with the
    switch off a Pro USPS number whose API came up empty goes to the paid tier
    exactly as it did before 2026-09-23."""
    rec = _install(monkeypatch, api_enabled=True, browser_first=False)
    rec.api_result = None
    rec.browser_result = None          # the browser finds nothing either

    _status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(USPS, is_pro=True))

    assert rec.order == ["api", "browser", "ship24"]
    assert rec.browser == [(USPS, "usps")], "the browser keeps its old position"
    assert rec.track17 == [], "no 17TRACK fallback without the reorder"
    assert source == "Ship24"


# ── Free chats: already headless-first, and staying that way ──────────────
def test_free_big_four_still_reads_the_browser_first(monkeypatch):
    rec = _install(monkeypatch)

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(UPS, is_pro=False))

    assert rec.order == ["browser"]
    assert rec.browser == [(UPS, "ups")] and source == "UPS Browser"
    assert rec.track17 == [], "17TRACK is a Pro perk"
    assert rec.ship24 == [], "a free chat must not buy a paid lookup"


def test_free_usps_never_promotes_the_browser_over_the_api(monkeypatch):
    """Free USPS is the same deliberate exception as Pro: the official API is the
    carrier's own free feed, so the browser is not promoted ahead of it."""
    rec = _install(monkeypatch, api_enabled=True)

    _status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(USPS, is_pro=False))

    assert source == "USPS API"
    assert rec.order == ["api"]
    assert rec.browser == [] and rec.track17 == []


def test_free_usps_browser_backs_the_api_without_17track(monkeypatch):
    rec = _install(monkeypatch, api_enabled=True)
    rec.api_result = None

    _status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(USPS, is_pro=False))

    assert rec.order == ["api", "browser"], "the API is asked before the browser"
    assert rec.browser == [(USPS, "usps")] and source == "USPS Browser"
    assert rec.track17 == [], "the override is a Pro-only path"
    assert rec.ship24 == []


# ── Throttling and quota shedding are unchanged ───────────────────────────
def test_a_429_on_a_pro_big_four_lookup_still_skips_ship24(monkeypatch):
    """The browser-first path must not open a new route to a billed call: a 429
    from the fallback tier is still not a dead end."""
    rec = _install(monkeypatch)
    rec.browser_result = None
    rec.rate_limited = True

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(UPS, is_pro=True))

    assert rec.order == ["browser", "17track"]
    assert (status, source) == ("Unavailable", "None")
    assert rec.ship24 == [], "a 429 became a billed call"


def test_a_429_after_the_browser_still_skips_ship24(monkeypatch):
    """Same rule on the deferred Pro-USPS call — the 17TRACK tier's new second
    position in the order."""
    rec = _install(monkeypatch, api_enabled=True)
    rec.api_result = None
    rec.browser_result = None
    rec.rate_limited = True

    status, _carrier, source, _detail = asyncio.run(
        carriers.fetch_track_status(USPS, is_pro=True))

    assert rec.order == ["api", "browser", "17track"]
    assert (status, source) == ("Unavailable", "None")
    assert rec.ship24 == []


def test_the_override_does_not_bypass_quota_shedding(monkeypatch):
    """official_empty drops one rule, not the quota policy: past
    TRACK17_HARD_PCT a slug with another tier to fall back on is still shed.

    Built without _install(): that helper stubs quota_pct to None, which is
    exactly the reading this test must NOT have.
    """
    monkeypatch.setattr(carriers, "SOURCE_ROUTING", "matrix")
    monkeypatch.setattr(carriers.config, "USPS_API_USERID", "TESTUSERID")
    carriers.record_quota(950, 1000)          # 95% — past the hard threshold
    try:
        assert carriers._use_17track("usps", True, official_empty=True) is False
        assert carriers._use_17track("ups", True) is True   # 17TRACK's own lane
    finally:
        carriers._quota_state.clear()
