"""Session 8 regression: the dashboard's JS filter/grouping rules + boot guard.

group() lives in webapp/index.html (plain JS, no test harness), so the
rules are duplicated verbatim in tests/miniapp_filters.test.js and executed
here via node. If node is missing, this test skips — pytest still guards
everything else. The boot-init hardening (try/catch around tg.ready/expand
and the catch-all with bootFailed) is asserted structurally below, since a
blank page in Telegram is exactly what this guards against.
"""
import shutil
import subprocess
from pathlib import Path

JS_TEST = Path(__file__).with_name("miniapp_filters.test.js")
HTML = Path(__file__).resolve().parent.parent / "webapp" / "index.html"


def _script_blocks(html):
    """The inline script blocks, in document order. (The CDN <script src=...>
    tags are not inline, so they never appear in the split.)"""
    return [b.split("</script>")[0] for b in html.split("<script>")[1:]]


def _app_script(html):
    """The Mini App's own block, identified by its 'use strict' prologue, so
    adding another inline block can never silently redirect the checks below
    to the wrong code."""
    for block in _script_blocks(html):
        if block.lstrip().startswith("'use strict';"):
            return block
    raise AssertionError("app script block ('use strict' prologue) not found")


def test_international_rule_matches_js_copy():
    """The 🌍 International rule exists twice (server truth + client filter) and
    is duplicated verbatim in the JS test file — all three must agree."""
    html = HTML.read_text()
    src = JS_TEST.read_text()
    for line in ("const INTL_SLUGS = ['chinapost', 'hkpost', 'intl'];",
                 "const INTL_COUNTRIES = ['CN', 'HK'];"):
        assert line in html, f"missing in webapp/index.html: {line}"
        assert line in src, f"missing in miniapp_filters.test.js: {line}"
    start = html.index("function isIntl(p) {")
    end = html.index("\n}\n", start) + len("\n}\n")
    assert html[start:end] in src, "isIntl() drifted from webapp/index.html"


def test_international_view_markup():
    """The Pro-only 🌍 International view: its chip, its locked state for free
    users, the quota meter, and the endpoint it talks to."""
    html = HTML.read_text()
    assert 'data-f="international"' in html
    assert 'data-label="International"' in html
    assert ".chip.locked" in html, "the locked styling carries the gating"
    assert "function reflectIntlChip()" in html
    assert "loadInternational()" in html
    assert "api('/api/international'" in html
    assert "function quotaCard(" in html
    assert ".card.quota" in html and ".bar > i" in html
    assert "17TRACK API quota" in html
    # Free users must get the pitch, not a dead end.
    assert "if (chip.dataset.f === 'international' && !tier.pro)" in html
    assert "openProSheet();" in html
    # The view must survive a bot that predates the endpoint.
    assert "err.status = res.status;" in html
    # The Pro sheet must advertise it, or nobody discovers the feature.
    assert "International posts + 17TRACK quota view" in html


def test_international_endpoint_is_registered():
    """The route the view calls must exist on the served app."""
    import miniapp
    app = miniapp._build_miniapp_app()
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/api/international" in paths


def test_privacy_sheet_mentions_ephemeral_chat():
    """The Mini App's privacy sheet must state the chat-side behavior too."""
    html = HTML.read_text()
    assert "cards and /list delete themselves" in html


def test_401_self_heals_with_one_reload():
    """initData is a short-lived bearer token (1 h): a dashboard left open past
    the window must recover itself instead of showing a dead error. One reload
    (guarded, so it can't loop) gets a fresh signature from Telegram."""
    html = HTML.read_text()
    assert "initDataReload" in html
    assert "location.reload()" in html
    assert "Session expired — reopen the app from the bot menu." in html


def test_delivered_cards_advertise_retention():
    """Retention is a promise the UI repeats: delivered cards say how long they
    have left, and the sheet states the automatic deletion."""
    html = HTML.read_text()
    assert "function retentionNote(p)" in html
    assert "retention_days" in html          # the server's window drives the note
    assert "Auto-forgets in" in html
    assert "Deleted for you" in html


def test_no_manual_refresh_controls_in_the_app():
    """Updating is global and state-adaptive now: no button, no endpoint, no
    refresh budget may survive in the dashboard."""
    html = HTML.read_text()
    for gone in ("refreshAll", "act-refresh", "/api/refresh", "refreshes_used",
                 "refresh_limit", "refreshBudgetExhausted", "↻ Update"):
        assert gone not in html, f"manual-refresh UI still present: {gone}"
    # The tier row says what happens instead (its numbers are checked against the
    # live cadence in the test below, not against a literal).
    assert "packages · auto-updating" in html
    assert 'id="tierText"' in html


def _cadence_label(minutes):
    """The sheet's own formatting rule (`min` under an hour, whole hours above)."""
    return f"{minutes // 60} h" if minutes >= 60 and minutes % 60 == 0 else f"{minutes} min"


def test_the_pro_sheet_quotes_the_live_cadence_and_limits():
    """The sheet's numbers must equal what the poller and the limits actually do.

    It used to hard-code a schedule and drifted to "30 minutes / 60 min free"
    delivery-day checks while the poller ran 10/30 — a price sheet advertising a
    speed the bot doesn't deliver. The server now sends tier_facts on every load,
    and these assertions hold both halves: the payload IS config + the cadence
    table, and the markup's pre-load fallback says the same numbers.
    """
    import config
    import miniapp
    import refresh
    facts = miniapp._tier_facts()
    cad = refresh.CADENCE
    assert facts["pro_packages"] == config.PRO_PACKAGE_LIMIT
    assert facts["free_packages"] == config.FREE_PACKAGE_LIMIT
    assert facts["pro_ofd_min"] == cad["Out for Delivery"][0] // 60
    assert facts["free_ofd_min"] == cad["Out for Delivery"][1] // 60
    assert facts["pro_transit_min"] == cad["In Transit"][0] // 60
    assert facts["free_transit_min"] == cad["In Transit"][1] // 60
    assert facts["intl_included"] == config.PRO_INTL_LIMIT

    html = HTML.read_text()
    for span_id, value in (
        ("factProPkgs", str(facts["pro_packages"])),
        ("factFreePkgs", str(facts["free_packages"])),
        ("factProOfd", _cadence_label(facts["pro_ofd_min"])),
        ("factFreeOfd", _cadence_label(facts["free_ofd_min"])),
        ("factProTransit", _cadence_label(facts["pro_transit_min"])),
        ("factFreeTransit", _cadence_label(facts["free_transit_min"])),
        ("factIntl", str(facts["intl_included"])),
    ):
        assert f'<span id="{span_id}">{value}</span>' in html, \
            f"the sheet's fallback for {span_id} drifted from config ({value})"
    # …and the client must actually take them from the payload: a sheet that only
    # bakes numbers in is the bug this test exists for.
    assert "facts: data.tier_facts || null" in html
    assert "applyTierFacts();" in html


def test_the_sheet_leads_with_phantom_and_drops_the_dead_ios_link():
    """Confirmed on the user's iPhone 2026-09-25: "Open in my wallet" opens nothing —
    Telegram.WebApp.openLink cannot open a custom scheme, and on iOS no app owns
    "solana:" at all. So the route that works leads, and on iOS the generic button is not
    offered as if it did; elsewhere it stays as a labelled fallback."""
    html = HTML.read_text()
    assert html.index('id="phantomBtn"') < html.index('id="openWalletBtn"'), (
        "Phantom must come first in the sheet")
    assert "function isIOSClient()" in html
    assert "Telegram.WebApp.platform" in html, "Telegram reports the platform…"
    assert "iPhone|iPad|iPod" in html, "…and the user agent covers an older script"
    assert "phantomBtn.className = 'btn-open-ios'" in html, (
        "the route that works gets the primary styling")
    assert "getElementById('openWalletBtn').style.display = 'none'" in html, (
        "and on iOS the button that opens nothing is hidden")
    assert "other.textContent = 'Other wallets'" in html, (
        "elsewhere it is demoted to a fallback, not removed")
    assert "walletReset.className = 'btn-open-ios'" in html, (
        "a previous buyer's hiding must not leak into the next sheet")


def test_is_international_server_rule():
    """Server-side classification — the authoritative filter for the view."""
    import miniapp
    assert miniapp._is_international({"carrier": "chinapost"}) is True
    assert miniapp._is_international({"carrier": "hkpost"}) is True
    assert miniapp._is_international({"carrier": "intl"}) is True
    assert miniapp._is_international({"carrier": "unknown", "carrier_country": "CN"}) is True
    assert miniapp._is_international({"carrier": "usps", "carrier_country": "US"}) is False
    assert miniapp._is_international({"carrier": "ups"}) is False
    assert miniapp._is_international({}) is False


def test_package_payload_exposes_carrier_country():
    """The client filter needs the verified origin country to classify."""
    import miniapp
    payload = miniapp._package_payload("RR123456789CN",
                                       {"carrier": "unknown", "carrier_country": "CN"})
    assert payload["carrier_country"] == "CN"
    assert payload["tracking_id"] == "RR123456789CN"


def _run_js_file(js_path, tmp_path):
    """Execute a JS file under node when present, else under macOS
    JavaScriptCore (`osascript -l JavaScript` — a real engine that ships with
    macOS and needs no install). Only skip when neither exists: silently
    skipping was how the JS side went unchecked while the app was broken."""
    if shutil.which("node"):
        subprocess.run(["node", str(js_path)], check=True, capture_output=True)
        return
    osascript = shutil.which("osascript")
    if not osascript:
        import pytest
        pytest.skip("no JS engine available (node/osascript) — JS rules unchecked")
    src = js_path.read_text()
    if src.startswith("#!"):
        src = src.split("\n", 1)[1]  # JXA chokes on the node shebang
    runnable = tmp_path / js_path.name
    runnable.write_text(src)
    res = subprocess.run([osascript, "-l", "JavaScript", str(runnable)],
                         capture_output=True, text=True)
    assert res.returncode == 0, f"JS rules failed under JavaScriptCore: {res.stdout}{res.stderr}"
    # JXA writes console.log to stderr, node writes it to stdout.
    assert "miniapp filter rules ok" in (res.stdout + res.stderr), res.stdout + res.stderr


def _assert_parses(code, tmp_path):
    """Parse JS with JavaScriptCore (node isn't installed here) without
    running it: `new Function(src)` compiles but never executes. Substring
    checks cannot see a syntax error — only a parser can."""
    osascript = shutil.which("osascript")
    if not osascript:
        import pytest
        pytest.skip("osascript/JavaScriptCore unavailable — JS parse unchecked")
    probe = tmp_path / "block_probe.js"
    probe.write_text(code)
    # Encoding 4 = NSUTF8StringEncoding. The async-wrapper retry keeps this
    # honest if a block ever gains top-level await/return.
    jxa = (
        'const s = $.NSString.stringWithContentsOfFileEncodingError("%s", 4, null).js;'
        'let err = null;'
        'try { new Function(s); } catch (e) { err = e.message; }'
        'if (err) {'
        '  try { new Function("return (async function () {" + s + "})"); err = null; }'
        '  catch (e2) { err = err + " | " + e2.message; }'
        '}'
        'err ? "PARSE_FAIL:" + err : "PARSE_OK:" + s.length;'
    ) % str(probe)
    res = subprocess.run([osascript, "-l", "JavaScript", "-e", jxa],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().startswith("PARSE_OK"), res.stdout.strip()


def test_miniapp_filter_rules_match_index_html(tmp_path):
    src = JS_TEST.read_text()
    html = (Path(__file__).resolve().parent.parent / "webapp" / "index.html").read_text()
    # The JS file must stay a verbatim copy of the grouping rules: extract
    # the two functions from index.html and require them byte-identical.
    for fn in ("function group(p) {", "function needsAttention(p) {"):
        start = html.index(fn)
        # crude block grab: from `fn` to the next blank-line-delimited `}`
        end = html.index("\n}\n", start) + len("\n}\n")
        block = html[start:end]
        assert block in src, f"{fn} drifted from webapp/index.html"
    _run_js_file(JS_TEST, tmp_path)


def test_app_script_parses_with_a_real_js_engine(tmp_path):
    """The check that would have caught the stray token that froze the app
    tonight: run the app's own block through a real JS engine."""
    html = HTML.read_text()
    _assert_parses(_app_script(html), tmp_path)


def test_independent_boot_watchdog(tmp_path):
    """The app is one script block, so a syntax error also kills its own error
    handling. A separate watchdog block must therefore come BEFORE it — and
    must itself parse — with the app signalling readiness once it has booted."""
    html = HTML.read_text()
    watchdog = html.index("window.__packeyBootFail")
    app_script = html.index("'use strict';")
    assert watchdog < app_script, "watchdog must be a separate block before the app script"
    assert 'id="watchdogRetry"' in html
    assert "window.__packeyReady = true" in html
    # two setters (boot + bootFailed hand-off) + watchdog listener + timer
    assert html.count("window.__packeyReady = true") >= 2
    assert html.count("window.__packeyReady") >= 4
    assert "if (reported) return;" in html  # never stack two failure messages
    # The watchdogs are only useful if they compile on their own — and now
    # EVERY inline block does: the head-first early watchdog, the DOM watchdog
    # below it, and the app itself.
    blocks = _script_blocks(html)
    assert len(blocks) == 3, f"expected early watchdog + watchdog + app, found {len(blocks)}"
    for block in blocks:
        _assert_parses(block, tmp_path)


def test_early_watchdog_precedes_the_render_blocking_scripts(tmp_path):
    """Blank page with NO message = the DOM watchdog never ran.

    A synchronous external <script src> that hangs (CDN unreachable on the
    device's network) freezes HTML parsing, so every watchdog further down is
    dead code and the webview shows pure white. The early watchdog must
    therefore sit BEFORE both external tags, and the optional QR library — used
    only for the Pro sheet's QR, and guarded at every use site — must not be
    synchronous.
    """
    html = HTML.read_text()
    early = html.index("Head-safe early watchdog")
    sdk = html.index("telegram.org/js/telegram-web-app.js")
    qr = html.index("qrcode.min.js")
    assert early < sdk and early < qr, "early watchdog must run before the CDN/SDK tags"
    assert '<script defer src="vendor/qrcode.min.js"' in html, \
        "the optional QR library must not block parsing"
    # It defers to the richer watchdog whenever that one is armed…
    assert "typeof window.__packeyBootFail === 'function'" in html
    # …and it must be able to paint without the app's DOM (no #list yet).
    assert "document.body || document.documentElement" in html
    # A SLOW-but-working load must not leave a stale error screen over a working
    # app: the overlay clears itself once the app reports ready — and so does
    # the independent watchdog's box, which a slow boot can have tripped first.
    assert "if (!window.__packeyReady) return;" in html
    assert "if (box) box.remove();" in html
    assert "window.__packeyErrorBox" in html
    _assert_parses(_script_blocks(html)[0], tmp_path)


def test_no_third_party_assets_block_the_render(tmp_path):
    """Every asset the dashboard fetches must come from its own origin.

    An asset that blocks the parser or first paint turns a slow/hung CDN into a
    blank or frozen webview — the failure class this dashboard already hit once
    (synchronous QR library). The Google Fonts stylesheet used to sit here: it was
    non-blocking (media=print swap + noscript fallback), but it was still a
    third-party request on every open, so it is gone — the CSS carries system
    fallbacks for all three families, which is what makes that safe.
    """
    html = HTML.read_text()
    assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html, \
        "the font CDN is back: it sees every user's IP on open"
    import re
    for tag in re.findall(r"<link\b[^>]*>", html):
        assert 'rel="stylesheet"' not in tag or "http" not in tag, \
            f"third-party stylesheet: {tag[:80]}"
    # QR library: deferred, and vendored (see the watchdog test for why).
    assert '<script defer src="vendor/qrcode.min.js"' in html
    # The one synchronous script (the Telegram SDK) is preconnected.
    assert '<link rel="preconnect" href="https://telegram.org">' in html
    assert '<link rel="dns-prefetch" href="https://telegram.org">' in html
    _assert_parses(_script_blocks(html)[0], tmp_path)


def test_boot_init_is_guarded():
    """Blank page in Telegram = unguarded boot. Assert the init hardening:
    tg.ready()/tg.expand() each in try/catch, a bootFailed() fallback with
    retry, and a catch-all routing boot errors to it."""
    html = (Path(__file__).resolve().parent.parent / "webapp" / "index.html").read_text()
    assert "function bootFailed(msg)" in html
    assert "try { tg.ready(); } catch (_) {}" in html
    assert "try { tg.expand(); } catch (_) {}" in html
    assert 'id="bootRetry"' in html
    assert "bootFailed((e && e.message)" in html


def test_the_carrier_link_does_not_leak_our_address():
    """The dashboard's "Official tracking" link points at the carrier's own page,
    and that URL carries the tracking number. `rel="noopener"` alone does NOT
    suppress the Referer header, so a click used to hand the carrier our tunnel
    host as well. The link itself is deliberately kept — it is a real feature —
    so the fix is to stop the referrer, not to remove the link."""
    html = (Path(__file__).resolve().parent.parent / "webapp" / "index.html").read_text()
    assert "rel=\"noopener noreferrer\"" in html, \
        "the carrier link lost noreferrer — the Referer leak is back"
    # The link is still there, escaped, with the number as its visible text.
    assert "${esc(p.carrier_url)}" in html
    assert "target=\"_blank\"" in html


def test_attention_ux_session9():
    """Session 9 polish: attention cards/headers are visually distinct,
    chips expose pressed state, status/summary updates are announced, and
    duplicate CSS rules are gone."""
    html = (Path(__file__).resolve().parent.parent / "webapp" / "index.html").read_text()
    assert ".card.attention" in html
    assert ".section-h.attention-h" in html
    assert "key === 'attention' ? ' attention-h' : ''" in html
    assert "group(p) === 'attention' ? ' attention' : ''" in html
    assert "@media (prefers-reduced-motion: reduce)" in html
    assert 'aria-pressed="true"' in html
    assert 'aria-pressed' in html and 'setAttribute' in html
    assert 'id="summary" aria-live="polite"' in html
    assert 'role="status" aria-live="polite"' in html
    # no duplicate selector blocks (the .qr-caption / .tier-text Session 9 fix)
    assert html.count(".tier-text {") == 1
    assert html.count(".qr-caption {") == 1


def test_unreachable_fails_loud_and_fast():
    """A dead tunnel / stale button must surface a named error quickly —
    never shimmer for 20s pretending to work. Assert the three pieces: a
    flagged unreachable error inside api(), the short first-paint timeout,
    and the fresh-/start guidance in the load error state."""
    html = (Path(__file__).resolve().parent.parent / "webapp" / "index.html").read_text()
    assert "err.unreachable = true" in html
    assert "Can't reach the server — send /start" in html
    assert "api('/api/packages', {}, 8000)" in html
    assert "from an old message" in html


def test_app_script_is_syntactically_plausible():
    """No JS parser available (node isn't installed), so lint the app's
    <script> block for the damage class we actually shipped once: a statement
    terminated mid-line and followed by stray tokens, which shows up as an
    unterminated string at the end of that line."""
    html = HTML.read_text()
    assert "☰ menu button" not in html, "stale residue must never come back"
    body = _app_script(html)
    assert body.count("{") == body.count("}"), "unbalanced braces in app script"
    assert body.count("(") == body.count(")"), "unbalanced parens in app script"

    state = "none"  # none | single | double | template
    for i, line in enumerate(body.splitlines(), start=1):
        j = 0
        while j < len(line):
            ch = line[j]
            if ch == "\\":
                j += 2  # escaped char — never a delimiter
                continue
            if state == "none":
                if ch == "'":
                    state = "single"
                elif ch == '"':
                    state = "double"
                elif ch == "`":
                    state = "template"
                elif ch == "/" and j + 1 < len(line) and line[j + 1] == "/":
                    break  # line comment: apostrophes in prose are harmless
            elif state == "single" and ch == "'":
                state = "none"
            elif state == "double" and ch == '"':
                state = "none"
            elif state == "template" and ch == "`":
                state = "none"
            j += 1
        if state in ("single", "double"):
            # A '…' or "…" string must close on the same line. Leftovers like
            # `'…div>';'s ☰ menu button…` end the line mid-string → syntax error.
            assert False, f"unterminated {state}-quoted string on script line {i}: {line.strip()[:90]}"
        # template literals may legitimately span lines → state carries over


def test_status_strip_and_pro_pause_are_wired():
    """The dashboard's health strip and the Pro-pause note read fields the
    server must actually send — pin both sides together so neither drifts."""
    html = HTML.read_text()
    py = (Path(__file__).resolve().parent.parent / "miniapp.py").read_text()
    assert 'id="syncLine"' in html, "health strip element missing"
    assert "updateSyncLine" in html and "appStatus" in html
    assert "needs_pro" in html, "the Pro-pause note must render"
    assert '"needs_pro"' in py and '"next_check_in"' in py, \
        "the payload must carry the fields the app renders"
    assert "next_due_in" in py, "ETA helper missing from refresh plumbing"


def test_boot_failed_is_top_level():
    """'use strict' makes block-scoped function declarations invisible to the
    catch-all at the end of the script, which calls bootFailed()."""
    html = (Path(__file__).resolve().parent.parent / "webapp" / "index.html").read_text()
    decl = html.index("function bootFailed(msg)")
    if_block = html.index("if (!inTelegram) {")
    assert decl < if_block, "bootFailed must be declared before the if/else"


def test_tunnel_watch_mode(tmp_path):
    """tunnel.sh --watch supervisor: syntax-valid, dispatch + health
    primitives present, and --check + --selftest prove the fixed behavior:
    the rotation write-path refuses log-polluted values (the 01:50 bug —
    stdout capture into sed under set -e), and --check reports liveness
    without changing anything. Health is modeled on code_is_healthy (401/200
    only) — the same rule the supervisor uses — so a dead tunnel answering
    530 or a wrong host answering 404/405 is NOT healthy (post-mortem
    2026-09-18: the old 'not 000' model called a 530 outage healthy)."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    script = root / "tunnel.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    out = subprocess.run([str(script), "--check"], capture_output=True,
                         text=True, cwd=root)
    # --check exits 0 only when BOTH halves are healthy (status_report:
    # code_is_healthy(code) && public path up && bot up), 1 otherwise. Both
    # states are valid for the test host; the contract under test is the
    # framed report shape + that convention. (Modeling only "public http: 000"
    # made the test fail spuriously whenever the bot was mid-restart while the
    # tunnel was fine, and pass spuriously when a dead tunnel answered 530.)
    #
    # The quota section is EXCLUDED from the health model: its "?" states
    # (no reading yet / unparseable) are informational and non-fatal, and
    # tracker.log rotates at ~1 MB, so the hourly quota line is legitimately
    # absent for up to an hour after every rotation (measured 2026-09-21
    # 23:13:48: rotation, then a HEALTHY ✓ report whose only "?" was the quota
    # line). Health lives in the tunnel + bot halves.
    head, _, rest = out.stdout.partition("17TRACK quota")
    _, _, tail = rest.partition("cadence (pro / free)")
    halves = head + tail
    healthy = (("http 401" in halves or "http 200" in halves)
               and "✗" not in halves and "? " not in halves)
    assert out.returncode in (0, 1)
    assert "packey tunnel dashboard" in out.stdout
    assert "https://" in out.stdout
    # public-dns guard-rail: both states are valid for the test host
    assert ("public record exists" in out.stdout
            or "not published publicly" in out.stdout)
    assert "17TRACK quota" in out.stdout
    assert "cadence (pro / free)" in out.stdout
    assert out.returncode == (0 if healthy else 1)
    selftest = subprocess.run([str(script), "--selftest"], capture_output=True,
                              text=True, cwd=root)
    # The selftest keeps its logs inside its own scratch dir (it must never
    # write into the live supervisor's watch log), so no WATCH_LOG needed here.
    assert selftest.returncode == 0, selftest.stdout + selftest.stderr
    assert "0 failed" in selftest.stdout
    plist = (root / "com.packey.tunnel.plist").read_text()
    assert "com.packey.tunnel" in plist and "--watch" in plist


class _FakeBot:
    """Minimal stand-in for aiogram Bot.send_message (records calls)."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return None


def test_miniapp_url_announce_only_on_change(tmp_path, monkeypatch):
    """Startup announces a fresh 'Open the app' button ONLY when
    MINI_APP_URL exists and differs from the last announced URL."""
    import main
    monkeypatch.chdir(tmp_path)
    main.storage.track_sessions.clear()
    main.storage.track_sessions[7] = {"T1": {"name": "Box"}}
    monkeypatch.setattr(main.config, "MINI_APP_URL", "https://a.example.com")
    assert main.load_miniapp_url_state() is None
    coro = main.maybe_announce_miniapp_url(_FakeBot())
    assert coro is not None
    coro.close()  # inspected only; the real await happens in main()
    import asyncio
    bot = _FakeBot()
    assert asyncio.run(main.announce_miniapp_url(bot, "https://a.example.com")) == 1
    assert len(bot.sent) == 1
    chat_id, text, kwargs = bot.sent[0]
    assert chat_id == 7 and "Packey dashboard moved" in text
    assert main.load_miniapp_url_state() == "https://a.example.com"
    # Same URL again → silent no-op (no extra messages).
    assert main.maybe_announce_miniapp_url(_FakeBot()) is None
    # Empty store → records silently without messaging.
    main.storage.track_sessions.clear()
    monkeypatch.setattr(main.config, "MINI_APP_URL", "https://b.example.com")
    assert main.maybe_announce_miniapp_url(_FakeBot()) is None
    assert main.load_miniapp_url_state() == "https://b.example.com"
    # No URL configured → no-op.
    monkeypatch.setattr(main.config, "MINI_APP_URL", None)
    assert main.maybe_announce_miniapp_url(_FakeBot()) is None
