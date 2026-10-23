"""17TRACK HTTP 429: back off, name the cause, and never let it cost money.

The live failure this pins: a 429 arrives with a non-JSON body, so the client's
.json() raised "Extra data: line 1 column 5 (char 4)" — a parse error that hid the
real cause — the retry fired instantly into the same limit, and the tier that failed
then handed the number to the PAID Ship24 fallback.
"""
import asyncio
import collections

import pytest

import carriers

INTL = "RR123456789CN"    # 17TRACK's reserved lane: no other free tier can read it


class _Resp:
    """httpx.Response stand-in that fails to parse exactly when a real 429 does."""

    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.payload = {"ok": True} if payload is None else payload
        self.headers = headers or {}
        self.json_calls = 0

    def json(self):
        self.json_calls += 1
        if self.status_code >= 400:
            raise ValueError("Extra data: line 1 column 5 (char 4)")
        return self.payload


class _Client:
    """Returns the queued responses in order, then repeats the last one."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.hits = []

    async def post(self, url, **kw):
        self.hits.append(url)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


@pytest.fixture
def clock(monkeypatch):
    """A fake monotonic clock whose sleeps cost nothing but are all recorded.

    Needed for the limiter as much as for the retries: with a zero-cost sleep that
    does not advance the clock, the limiter's wait loop would spin forever.
    """
    state = {"now": 1000.0, "slept": []}

    def monotonic():
        return state["now"]

    async def sleep(seconds):
        # A hair past the requested time on purpose: the limiter slides its window
        # with `now - calls[0] > per`, so a clock that lands exactly on the boundary
        # makes it spin on zero-length sleeps.
        state["slept"].append(seconds)
        state["now"] += seconds + 0.005

    monkeypatch.setattr(carriers.time, "monotonic", monotonic)
    monkeypatch.setattr(carriers.asyncio, "sleep", sleep)
    # The limiter is module-level and shared with every other test in the suite, so
    # give this test a fresh window rather than inheriting whatever is in it.
    monkeypatch.setattr(carriers.track17_rate_limiter, "calls", collections.deque())
    return state


def _client(monkeypatch, responses):
    client = _Client(responses)
    monkeypatch.setattr(carriers, "http_client", lambda: client)
    return client


def test_a_429_backs_off_and_is_raised_as_rate_limited(clock, monkeypatch):
    client = _client(monkeypatch, [_Resp(429)])

    with pytest.raises(carriers.Track17RateLimited):
        asyncio.run(carriers._post_17track("/gettrackinfo", []))

    assert len(client.hits) == 3, "it retries instead of giving up on the first"
    assert clock["slept"] == [1.0, 2.0, 4.0], \
        "an instant retry adds pressure to the very limit that just fired"


def test_the_429_body_is_never_parsed(clock, monkeypatch):
    """The misleading log line came from .json() on a 429, so the cause has to stay
    a status code rather than become a parse error."""
    bad = _Resp(429)
    _client(monkeypatch, [bad])

    with pytest.raises(carriers.Track17RateLimited) as excinfo:
        asyncio.run(carriers._post_17track("/gettrackinfo", []))

    assert bad.json_calls == 0
    assert "rate limited" in str(excinfo.value)


def test_retry_after_is_honoured(clock, monkeypatch):
    _client(monkeypatch, [_Resp(429, headers={"Retry-After": "5"})])

    with pytest.raises(carriers.Track17RateLimited):
        asyncio.run(carriers._post_17track("/gettrackinfo", []))

    assert clock["slept"] == [5.0, 5.0, 5.0]


def test_a_hostile_retry_after_is_capped(clock, monkeypatch):
    """A header must not be able to park a refresh cycle for hours."""
    _client(monkeypatch, [_Resp(429, headers={"Retry-After": "99999"})])

    with pytest.raises(carriers.Track17RateLimited):
        asyncio.run(carriers._post_17track("/gettrackinfo", []))

    assert clock["slept"] == [60.0] * 3


def test_an_http_date_retry_after_falls_back_to_the_exponential(clock, monkeypatch):
    _client(monkeypatch,
            [_Resp(429, headers={"Retry-After": "Wed, 23 Sep 2026 12:00:00 GMT"})])

    with pytest.raises(carriers.Track17RateLimited):
        asyncio.run(carriers._post_17track("/gettrackinfo", []))

    assert clock["slept"] == [1.0, 2.0, 4.0]


def test_a_429_then_an_answer_is_not_an_error(clock, monkeypatch):
    good = _Resp(200, {"code": 0})
    client = _client(monkeypatch, [_Resp(429), good])

    assert asyncio.run(carriers._post_17track("/gettrackinfo", [])) == {"code": 0}
    assert len(client.hits) == 2 and good.json_calls == 1
    assert clock["slept"] == [1.0], "one back-off, then the good attempt"


def test_a_403_is_named_and_not_retried(clock, monkeypatch):
    """A request/auth problem will not heal by waiting, and its body is not JSON."""
    bad = _Resp(403)
    client = _client(monkeypatch, [bad])

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(carriers._post_17track("/gettrackinfo", []))

    assert not isinstance(excinfo.value, carriers.Track17RateLimited)
    assert "403" in str(excinfo.value)
    assert len(client.hits) == 1 and bad.json_calls == 0 and clock["slept"] == []


def test_check_17track_lets_a_rate_limit_through(clock, monkeypatch):
    """_check_17track must not fold a 429 into its "no data" answer: the caller needs
    the distinction to protect the paid tier."""
    async def rate_limited(*args, **kwargs):
        raise carriers.Track17RateLimited("429")

    monkeypatch.setattr(carriers, "_post_17track", rate_limited)

    with pytest.raises(carriers.Track17RateLimited):
        asyncio.run(carriers._check_17track(INTL))


def test_a_rate_limited_round_never_buys_a_ship24_call(clock, monkeypatch):
    """The leak: 17TRACK throttled → browser skipped → a BILLED lookup for the same
    number inside the same round."""
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    monkeypatch.setattr(carriers.config, "track17_api_key", "k")
    monkeypatch.setattr(carriers.config, "USPS_API_USERID", None)
    monkeypatch.setattr(carriers.config, "USPS_BROWSER_TRACK", False)

    async def rate_limited(tracking_id):
        raise carriers.Track17RateLimited("429")

    monkeypatch.setattr(carriers, "_check_17track", rate_limited)
    paid = _client(monkeypatch, [_Resp(201, {"data": {"trackings": []}})])

    status, _carrier, source, _detail = asyncio.run(carriers.fetch_track_status(INTL))

    assert (status, source) == ("Unavailable", "None")
    assert paid.hits == [], "a 429 cascaded into a billed Ship24 call"


class _SplitClient:
    """429 for 17TRACK, a plausible answer for Ship24 — and it counts each."""

    def __init__(self):
        self.track17_hits = 0
        self.ship24_hits = 0

    async def post(self, url, **kw):
        if "ship24" in url.lower():
            self.ship24_hits += 1
            return _Resp(201, {"data": {"trackings": []}})
        self.track17_hits += 1
        return _Resp(429)


def test_the_whole_chain_survives_a_429_without_spending_money(clock, monkeypatch):
    """The link that actually failed live, through the REAL layers: an httpx 429 →
    _post_17track → _check_17track → fetch_track_status. Nothing is stubbed except
    the HTTP client, because the bug was in how those layers handed the failure on:
    the 429 was swallowed as "no data" and the paid tier covered for it."""
    monkeypatch.setenv("SHIP24_API_KEY", "apik_test")
    monkeypatch.setattr(carriers.config, "track17_api_key", "k")
    monkeypatch.setattr(carriers.config, "USPS_API_USERID", None)
    monkeypatch.setattr(carriers.config, "USPS_BROWSER_TRACK", False)
    monkeypatch.setattr(carriers.storage, "_registration_on_cooldown",
                        lambda tracking_id: False)

    client = _SplitClient()
    monkeypatch.setattr(carriers, "http_client", lambda: client)

    status, _carrier, source, _detail = asyncio.run(carriers.fetch_track_status(INTL))

    assert client.track17_hits >= 2, "the 429 must have gone through the real client"
    assert (status, source) == ("Unavailable", "None")
    assert client.ship24_hits == 0, "a 429 became a billed call somewhere in the chain"


def test_the_limiter_holds_the_third_request_back(clock):
    """3 req/sec was the burst that tripped the live 429s: two go through inside the
    window, the third waits for it to slide."""
    limiter = carriers.RateLimiter(rate=2, per=1.0)

    asyncio.run(limiter.acquire())
    asyncio.run(limiter.acquire())
    assert clock["slept"] == [], "the first two go straight through"

    asyncio.run(limiter.acquire())
    assert clock["slept"] and clock["slept"][0] > 0, "the third must wait"

    assert carriers.track17_rate_limiter.rate <= 2, \
        "the shipped limiter must stay under the rate that was observed to fail"
