"""Hardening of the browser tier, adapted onto the audited file.

Covers the five fixes that were adopted: the .created_at freshness marker
(copytree stamps the copy with the SOURCE dir's mtime), persisted throttle
state with strike backoff, per-number failure reasons (get_last_outcome),
a best-effort cross-process profile lock, and the opt-in USPS_DEBUG_DIR
capture. Feature tests are written against the hardened API; they fail on
the pre-hardening file (AttributeError) by construction.
"""
import json
import os
import sqlite3
import time

import pytest

import usps_browser as up


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    """Every test starts with clear throttle state, an isolated store and
    captures off, so a real browser_throttle.json left by the bot or an
    operator-set USPS_DEBUG_DIR can never leak into a test run."""
    monkeypatch.setattr(up, "_throttled_until", {})
    monkeypatch.setattr(up, "_throttle_strikes", {})
    monkeypatch.setattr(up, "_THROTTLE_STORE", str(tmp_path / "throttle.json"))
    monkeypatch.setattr(up, "_LAST_OUTCOME", {})
    monkeypatch.setattr(up, "USPS_DEBUG_DIR", None)
    yield


# ── profile freshness ────────────────────────────────────────────────────

def test_copytree_stamps_the_source_dir_age(tmp_path):
    # The reason the marker exists: shutil.copytree copystats the destination
    # root, so a JUST-built copy of an old source reads as old and the warm
    # check would rebuild it on every batch.
    import shutil
    src = tmp_path / "src"
    src.mkdir()
    (src / "Cookies").write_text("x")
    old = time.time() - 3 * 3600
    os.utime(str(src), (old, old))
    dst = shutil.copytree(str(src), str(tmp_path / "dst"))
    assert time.time() - os.path.getmtime(dst) > 2 * 3600


def _make_copy(path, marker_age=None):
    os.makedirs(str(path), exist_ok=True)
    if marker_age is not None:
        with open(os.path.join(str(path), up._CREATED_MARKER), "w") as fh:
            fh.write(str(time.time() - marker_age))
    return str(path)


def test_marker_age_is_what_the_warm_check_uses(monkeypatch, tmp_path):
    copy = _make_copy(tmp_path / "copy", marker_age=0)
    monkeypatch.setattr(up, "_PROFILE_COPY", copy)
    assert up._profile_copy_is_warm() is True
    # Past the 2h window the same marker says stale.
    old = _make_copy(tmp_path / "old", marker_age=3 * 3600)
    monkeypatch.setattr(up, "_PROFILE_COPY", old)
    assert up._profile_copy_is_warm() is False


def test_a_copy_without_a_marker_falls_back_to_mtime(monkeypatch, tmp_path):
    copy = _make_copy(tmp_path / "copy")  # no marker
    monkeypatch.setattr(up, "_PROFILE_COPY", copy)
    assert up._profile_copy_is_warm() is True  # fresh mtime
    os.utime(copy, (time.time() - 3 * 3600,) * 2)
    assert up._profile_copy_is_warm() is False


# ── throttle persistence + strike backoff ────────────────────────────────

def test_consecutive_refusals_double_the_pause_capped_at_8x(monkeypatch):
    monkeypatch.setattr(up, "USPS_BROWSER_BACKOFF", 100)
    monkeypatch.setattr(up.random, "uniform", lambda a, b: 1.0)  # no jitter
    pauses = []
    for _ in range(6):
        before = time.time()
        up._note_edge_refusal("usps", 403)
        pauses.append(up._throttled_until["usps"] - before)
    # 100 * 2**(strike-1), capped at 8x from the 4th consecutive refusal on.
    assert [round(p / 100) for p in pauses] == [1, 2, 4, 8, 8, 8]


def test_a_success_resets_the_strike_count(monkeypatch):
    monkeypatch.setattr(up, "USPS_BROWSER_BACKOFF", 100)
    monkeypatch.setattr(up.random, "uniform", lambda a, b: 1.0)
    up._note_edge_refusal("usps", 403)
    up._note_edge_refusal("usps", 403)
    assert up._throttle_strikes["usps"] == 2
    up._note_edge_success("usps")
    assert "usps" not in up._throttle_strikes
    assert "usps" not in up._throttled_until
    # And the next refusal starts over at 1x, not 4x.
    before = time.time()
    up._note_edge_refusal("usps", 403)
    assert round((up._throttled_until["usps"] - before) / 100) == 1


def test_throttle_state_survives_a_restart(monkeypatch):
    monkeypatch.setattr(up, "USPS_BROWSER_BACKOFF", 100)
    monkeypatch.setattr(up.random, "uniform", lambda a, b: 1.0)
    up._note_edge_refusal("fedex", 403)
    up._note_edge_refusal("fedex", 403)
    # Simulate the restart: fresh in-memory state, then load from disk.
    monkeypatch.setattr(up, "_throttled_until", {})
    monkeypatch.setattr(up, "_throttle_strikes", {})
    up._load_throttle_state()
    assert up._throttle_strikes["fedex"] == 2
    assert up._throttled_until["fedex"] > time.time()
    # The escalated pause was persisted too, not just the base one.
    assert up._throttled_until["fedex"] - time.time() > 150


def test_a_corrupt_or_expired_store_is_ignored(monkeypatch, tmp_path):
    store = tmp_path / "throttle.json"
    store.write_text("not json at all")
    monkeypatch.setattr(up, "_THROTTLE_STORE", str(store))
    up._load_throttle_state()  # must not raise
    assert up._throttled_until == {}
    expired = {"usps": {"until": time.time() - 10, "strikes": 3}}
    store.write_text(json.dumps(expired))
    up._load_throttle_state()
    assert "usps" not in up._throttled_until
    assert "usps" not in up._throttle_strikes


def test_the_store_is_owner_only_and_written_atomically():
    up._note_edge_refusal("usps", 403)  # triggers a save
    mode = os.stat(up._THROTTLE_STORE).st_mode & 0o777
    assert mode == 0o600
    assert not os.path.exists(up._THROTTLE_STORE + ".tmp")


# ── failure reasons ──────────────────────────────────────────────────────

def test_an_empty_result_carries_a_reason(monkeypatch):
    monkeypatch.setattr(up, "CHROME_PROFILE", "/nonexistent/profile")
    monkeypatch.setattr(up, "_throttled_until", {})
    tid = "94001102000999999999"
    assert up.get_usps_statuses([tid]) == {}
    assert up.get_last_outcome(tid) == "no_profile"


def test_outcomes_are_bounded():
    for i in range(up._OUTCOME_MAX + 50):
        up._note_outcome(f"tid{i}", "no_data")
    assert len(up._LAST_OUTCOME) <= up._OUTCOME_MAX


def test_outcome_unknown_number_is_none():
    assert up.get_last_outcome("never-attempted-9400") is None


# ── cross-process profile lock ───────────────────────────────────────────

def test_a_second_session_sees_the_lock_as_busy(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "_profile_lock_path",
                        lambda copy_dir=None: str(tmp_path / "profile.lock"))
    fh1, busy1 = up._acquire_profile_lock(timeout_s=0.2)
    assert busy1 is False and fh1 is not None
    try:
        fh2, busy2 = up._acquire_profile_lock(timeout_s=0.2)
        assert busy2 is True and fh2 is None   # held by the first session
    finally:
        up._release_profile_lock(fh1)
    fh3, busy3 = up._acquire_profile_lock(timeout_s=0.2)
    assert busy3 is False and fh3 is not None  # free again after release
    up._release_profile_lock(fh3)


def test_an_unwritable_lock_location_degrades_to_unlocked(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "_profile_lock_path",
                        lambda copy_dir=None: str(tmp_path / "no-such-dir" / "x.lock"))
    fh, busy = up._acquire_profile_lock(timeout_s=0.2)
    assert fh is None and busy is False  # proceed WITHOUT the lock, not fail


# ── USPS_DEBUG_DIR capture ───────────────────────────────────────────────

def test_debug_capture_masks_numbers_and_emails_and_is_owner_only(
        monkeypatch, tmp_path):
    monkeypatch.setattr(up, "USPS_DEBUG_DIR", str(tmp_path / "caps"))
    body = ("Tracking Number: 9300111043900394559543 Delivered WILLISTON PARK "
            "reply to ops@example.com for help")
    up._save_debug_capture("usps", "9300111043900394559543", body)
    files = os.listdir(str(tmp_path / "caps"))
    assert len(files) == 1
    path = os.path.join(str(tmp_path / "caps"), files[0])
    raw = open(path).read()
    assert "9300111043900394559543" not in raw
    assert "ops@example.com" not in raw
    assert "[email]" in raw
    assert "9300...9543" in raw          # the same mask the logs use
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_debug_capture_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "USPS_DEBUG_DIR", None)
    up._save_debug_capture("usps", "9300111043900394559543", "body text")
    assert os.listdir(str(tmp_path)) == []


# ── small fixes ──────────────────────────────────────────────────────────

def test_mask_matches_the_carriers_helper_for_every_shape():
    import carriers
    samples = ["", "12345678", "123456789", "1234567890",
               "9300111043900394559543", "1Z999AA10123456784"]
    for tid in samples:
        assert up._mask(tid) == carriers._sanitize_tracking_id(tid), tid


# ── block-page detection ─────────────────────────────────────────────────

def test_block_markers_match_deny_pages_and_not_result_pages():
    deny = ("Access Denied\nYou don't have permission to access this page.\n"
            "Reference #18.4f2e3217.1696000000.3a2b1c\n"
            "errors.edgesuite.net")
    assert up._looks_blocked(deny) is True
    assert up._looks_blocked("Request unsuccessful. Incapsula error ID: 11") is False
    # A long, legitimate result page must never match, marker or not.
    long_page = "Tracking Number: 9300111043900394559543\n" + ("x" * 5000)
    assert up._looks_blocked(long_page + " access denied") is False
    assert up._looks_blocked("") is False


def test_a_block_page_arms_the_same_escalating_pause(monkeypatch):
    monkeypatch.setattr(up, "USPS_BROWSER_BACKOFF", 100)
    monkeypatch.setattr(up.random, "uniform", lambda a, b: 1.0)
    before = time.time()
    up._note_block_page("usps")
    assert round((up._throttled_until["usps"] - before) / 100) == 1
    before = time.time()
    up._note_block_page("usps")
    assert round((up._throttled_until["usps"] - before) / 100) == 2  # strike 2
    # It persisted exactly like a hard 403 refusal would.
    assert json.load(open(up._THROTTLE_STORE))["usps"]["strikes"] == 2


def test_a_blocked_poll_stops_and_refuses_the_carrier(monkeypatch):
    tid = "9300111043900394559543"

    class _BlockedPage:
        def inner_text(self, _sel):
            return ("Access Denied\nReference #18.4f2e3217.1696000000.3a2b1c\n"
                    "errors.edgesuite.net")

    deadline = time.monotonic() + 4
    res = up._poll_page(_BlockedPage(), {"parse": lambda b, t: None},
                        tid, "usps", deadline)
    assert res is None
    assert up.get_last_outcome(tid) == "blocked"
    assert up.is_throttled("usps")   # armed — the retry loop will end too


def test_headful_override_resolves_per_call(monkeypatch):
    monkeypatch.setattr(up, "USPS_BROWSER_HEADFUL", False)
    assert up._resolve_headful(None) is False   # module default
    assert up._resolve_headful(True) is True    # one-call override
    monkeypatch.setattr(up, "USPS_BROWSER_HEADFUL", True)
    assert up._resolve_headful(None) is True
    assert up._resolve_headful(False) is False


def test_diagnose_profile_is_metadata_only(monkeypatch, tmp_path):
    copy = _make_copy(tmp_path / "copy", marker_age=0)
    jar = os.path.join(copy, "Cookies")
    con = sqlite3.connect(jar)
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
    con.execute("INSERT INTO cookies VALUES ('tools.usps.com', 'a', 'b')")
    con.commit()
    con.close()
    monkeypatch.setattr(up, "CHROME_PROFILE", str(tmp_path / "src"))
    monkeypatch.setattr(up, "_PROFILE_COPY", copy)
    report = up.diagnose_profile()
    assert report["copy_exists"] is True
    assert report["cookie_rows_total"] == 1
    blob = json.dumps(report)
    assert "tools.usps.com" not in blob   # host names stay out of reports
    assert "'b'" not in blob and '"b"' not in blob


# ── per-carrier profile slots ────────────────────────────────────────────

def test_each_carrier_gets_its_own_profile_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "_PROFILE_COPY", str(tmp_path / "base"))
    usps = up._profile_copy_for("usps")
    ups = up._profile_copy_for("ups")
    assert usps != ups
    assert usps.startswith(str(tmp_path / "base"))
    assert ups.startswith(str(tmp_path / "base"))


def test_ensure_and_prune_work_per_slot(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    con = sqlite3.connect(str(src / "Cookies"))
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
    con.executemany("INSERT INTO cookies VALUES (?,?,?)",
                    [("tools.usps.com", "a", "v"), ("mail.google.com", "b", "v")])
    con.commit()
    con.close()
    monkeypatch.setattr(up, "CHROME_PROFILE", str(src))
    monkeypatch.setattr(up, "_PROFILE_COPY", str(tmp_path / "base"))
    copy = up._ensure_profile_copy(up._profile_copy_for("ups"))
    assert copy == str(tmp_path / "base-ups")
    con = sqlite3.connect(os.path.join(copy, "Cookies"))
    hosts = {r[0] for r in con.execute("SELECT host_key FROM cookies")}
    con.close()
    assert hosts == {"tools.usps.com"}   # the slot's jar got pruned too
    assert os.path.isfile(os.path.join(copy, up._CREATED_MARKER))


def test_cleanup_removes_every_slot_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(up, "_PROFILE_COPY", str(tmp_path / "base"))
    made = [str(tmp_path / "base")] + [up._profile_copy_for(s)
                                       for s in ("usps", "ups", "dhl", "fedex")]
    for d in made:
        os.makedirs(d, exist_ok=True)
    up.cleanup_profile_copy()
    assert not any(os.path.isdir(d) for d in made)


# ── block-page detection on the spy path (UPS / FedEx) ──────────────────

def test_a_blocked_spy_page_costs_one_navigation_not_a_wait(monkeypatch):
    """FedEx's only signal is the spy: a 200 deny page used to burn the full
    api_wait_ms (25s+) waiting for an API window that could never fire, then
    retried into the same wall. Now the wait is cancelled the moment the deny
    page renders, the carrier is refused, and the outcome says blocked."""
    tid = "522093987024"

    parse_calls = []

    class _NavResp:
        status = 200

    class _Window:
        value = None
        exited = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.exited = exc
            return False                    # propagate — the wait is cancelled

    class _Page:
        def __init__(self):
            self.window = _Window()

        def expect_response(self, match, timeout=None):
            return self.window

        def goto(self, url, **kw):
            return _NavResp()

        def inner_text(self, _sel):
            return ("Access Denied\nReference #18.4f2e3217.1696000000.3a2b1c\n"
                    "errors.edgesuite.net")

    cfg = {"url": lambda t: f"https://www.fedex.com/wtrk/track/?trknbr={t}"}
    spy = {"match": lambda r: True, "wait_ms": 25000,
           "parse": lambda data, t: parse_calls.append(t) or ("Delivered", "x")}
    page = _Page()
    assert up._spy_attempt(page, spy, cfg, tid, "fedex", "FedEx", 1) == (None, False)
    assert parse_calls == []                # the deny page never reaches the parser
    assert page.window.exited is not None
    assert page.window.exited[0] is up._BlockPageSignal  # cancelled, not timed out
    assert up.get_last_outcome(tid) == "blocked"
    assert up.is_throttled("fedex")         # the retry loop ends at its top


def test_a_refused_api_response_arms_the_throttle(monkeypatch):
    """The carrier's own API refusing us (403/429 on the window that DID fire)
    is a hard edge refusal, not an empty payload to retry."""
    tid = "522093987024"

    class _ApiResp:
        status = 403

        @staticmethod
        def json():
            return {}

    class _Window:
        value = _ApiResp()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _NavResp:
        status = 200

    class _Page:
        def expect_response(self, match, timeout=None):
            return _Window()

        def goto(self, url, **kw):
            return _NavResp()

        def inner_text(self, _sel):
            return "an ordinary page shell"

    parse_calls = []
    cfg = {"url": lambda t: f"https://www.fedex.com/wtrk/track/?trknbr={t}"}
    spy = {"match": lambda r: True, "wait_ms": 25000,
           "parse": lambda data, t: parse_calls.append(t) or None}
    assert up._spy_attempt(_Page(), spy, cfg, tid, "fedex", "FedEx", 1) == (None, False)
    assert parse_calls == []                # a refused API is not an empty payload
    assert up.is_throttled("fedex")


def test_the_page_first_path_skips_the_spy_after_a_block(monkeypatch):
    """UPS reads the page first; when the poll hits a block page the throttle
    is already armed, so the spy fallback must not spend a window on the same
    wall."""
    tid = "1Z999AA10123456784"
    spy_calls = []

    class _NavResp:
        status = 200

    class _Page:
        def add_init_script(self, script):
            pass

        def goto(self, url, **kw):
            return _NavResp()

        def inner_text(self, _sel):
            return "Access Denied\nReference #18.4f2e3217.1696000000.3a2b1c"

    def fake_poll(page, cfg, tid_, slug, deadline):
        up._note_block_page(slug)
        up._note_outcome(tid_, "blocked")
        return None

    def fake_spy(*a, **k):
        spy_calls.append(a)
        return None, False

    monkeypatch.setattr(up, "_PROFILE_COPY", "/tmp")
    monkeypatch.setattr(up, "_ensure_profile_copy", lambda copy_dir=None: "/tmp")
    monkeypatch.setattr(up, "_human_pause", lambda page, moves=8: None)
    monkeypatch.setattr(up.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(up, "_poll_page", fake_poll)
    monkeypatch.setattr(up, "_spy_attempt", fake_spy)
    from tests.test_usps_browser import _fake_playwright
    _fake_playwright(monkeypatch, _Page())

    results = up._check_batch_chunk([tid], 120, "ups")
    assert results == {}
    assert spy_calls == []                  # no window spent on the blocked wall
    assert up.get_last_outcome(tid) == "blocked"
    assert up.is_throttled("ups")


def test_disabled_slugs_filter_browser_slugs(monkeypatch):
    """USPS_BROWSER_DISABLED_SLUGS opts carriers out of the browser tier: the
    sweep and the on-demand tier both consult browser_slugs(), so the filter
    must remove the slug entirely while leaving the others (and their labels)
    intact. Motivated 2026-10-04: dhl.com's tracking widget cannot render for
    any visitor (their MFE config 404s in every locale), so the DHL slot can
    only burn its attempt budget and answer no_data."""
    monkeypatch.setattr(up, "_DISABLED_SLUGS", frozenset({"dhl"}))
    slugs = up.browser_slugs()
    assert "dhl" not in slugs
    assert {"usps", "ups", "fedex"} <= set(slugs)
    assert up.page_label("dhl") == "DHL"   # label lookup stays harmless


def test_disabled_slugs_default_to_every_carrier(monkeypatch):
    """Default (knob unset) must keep today's behavior: all four carriers."""
    monkeypatch.setattr(up, "_DISABLED_SLUGS", frozenset())
    assert set(up.browser_slugs()) == {"usps", "ups", "dhl", "fedex"}


def test_disabled_slugs_parse_from_env():
    """The knob parses a CSV of slugs, case-insensitive, ignoring blanks —
    and an unset knob means no carrier is disabled."""
    assert up._parse_disabled_slugs(" DHL, fedex ,,") == frozenset({"dhl", "fedex"})
    assert up._parse_disabled_slugs("") == frozenset()
    assert up._parse_disabled_slugs(None) == frozenset()


def test_session_end_prune_runs_after_the_session_and_fails_soft(
        monkeypatch, tmp_path):
    """prune-end: once a chunk's Chrome is closed (and while the profile lock
    is still held), the cookies the session accumulated are stripped. A prune
    failure here is warn-only — the next session's ensure prune strips the jar
    before any browser opens it, so a transient failure must never discard the
    copy's warm state."""
    import sqlite3
    events = []
    copy = tmp_path / "slot"
    copy.mkdir()
    monkeypatch.setattr(up, "_profile_copy_for", lambda slug="usps": str(copy))
    monkeypatch.setattr(up, "_acquire_profile_lock",
                        lambda copy_dir=None, timeout_s=120.0: (object(), False))
    monkeypatch.setattr(up, "_release_profile_lock", lambda fh: None)

    def _fake_locked(chunk, budget, slug="usps", headful=None):
        events.append("locked")
        return {}

    monkeypatch.setattr(up, "_check_batch_chunk_locked", _fake_locked)

    def _fake_prune(copy_dir):
        events.append(("pruned", copy_dir))
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(up, "_prune_profile_cookies", _fake_prune)

    assert up._check_batch_chunk(["9400111206200074888350"], 30) == {}
    assert events[0] == "locked"                                  # session first…
    assert events[1] == ("pruned", str(copy))                     # …prune after
    assert copy.exists(), "warn-only: the warm copy must survive a failed prune"
