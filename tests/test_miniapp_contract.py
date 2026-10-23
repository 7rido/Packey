"""App-to-server contract: the ways the Mini App breaks silently.

A wrong element id and a renamed endpoint are invisible both to the JS parse
check in test_miniapp_filters.py and to every test that calls a handler
directly: the app dies mid-boot (null.onclick throws) or 404s into a toast at
runtime. These pin webapp/index.html against miniapp.py, so a rename on one
side fails here instead of on a user phone.
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "webapp" / "index.html"
MINIAPP = ROOT / "miniapp.py"


def _registered_routes():
    import miniapp
    app = miniapp._build_miniapp_app()
    return {r.resource.canonical for r in app.router.routes()
            if hasattr(r, "resource")}


def test_every_api_call_the_app_makes_is_a_registered_route():
    called = set(re.findall(r"api\('(/api/[A-Za-z0-9_-]+)", HTML.read_text()))
    assert called, "no api() calls found - did the app script move?"
    missing = sorted(called - _registered_routes())
    assert not missing, f"webapp calls unregistered endpoints: {missing}"


def test_the_manual_deposit_address_contract():
    """The server sends `deposit_address`; the sheet shows it only when it is there.

    Two silent failures this prevents: a renamed field (the app would quietly lose
    the manual path, and an exchange payer stays unattributable) and a block that
    defaults to visible — which would show the address from the PREVIOUS sheet, an
    address the buyer must not pay.
    """
    html = HTML.read_text()
    src = MINIAPP.read_text()
    assert '"deposit_address"' in src, "the invoice endpoint no longer sends the field"
    assert "invoice.deposit_address" in html, "the sheet no longer reads the field"
    assert 'id="payDepositBlock" style="display:none"' in html, \
        "the manual block must be hidden until this invoice's own address arrives"
    assert "depReset.style.display = 'none'" in html, \
        "a previous sheet's address must be cleared on open"
    copy_fn = html.split("async function copyDepositAddress", 1)[1].split(
        "function setPayToken", 1)[0]
    assert "copyText(" in copy_fn, \
        "the manual address must use the same clipboard path as the shared wallet"


def test_the_wallet_action_uses_the_os_breakout_not_the_webview():
    """The old buttons set window.location.href to a solana: URI — swallowed by
    Telegram's webview, and owned by no app on iOS at all, so they went nowhere. Inside
    Telegram the sanctioned breakout is Telegram.WebApp.openLink().

    Simplified 2026-09-24: one button, and it opens the Solana Pay link itself instead of
    a chain of per-wallet hops — the URI is the spec's own mechanism and the payer's
    wallet reads the recipient, amount, token, memo and reference out of it.
    """
    html = HTML.read_text()
    assert "function openExternal(url)" in html
    assert "Telegram.WebApp.openLink(url)" in html, "Telegram's breakout is missing"
    wallet = html.split("async function openWallet()", 1)[1].split("\nfunction ", 1)[0]
    assert "window.location.href" not in wallet, "navigating the webview is the bug"
    assert "openExternal(url)" in wallet, "the OS hand-off, not a webview navigation"
    assert "payUrl()" in wallet, "and it opens the payment link itself"
    assert "phantom.app/ul" not in wallet and "solflare.com/ul" not in wallet, (
        "no per-wallet browse hops: that is what the picker needed")


def test_the_wallet_action_promises_only_what_happens():
    """The single button says it opens the payer's wallet. What happens next is the
    wallet's own confirmation screen, so nothing claims a prefilled sheet."""
    html = HTML.read_text()
    row = html.split('id="openWalletBtn"', 1)[1].split("</button>", 1)[0]
    assert "Open in my wallet" in row, row
    assert "prefilled" not in row and "prefilled</span>" not in html
    assert 'id="wPhantom"' not in html, "the per-wallet rows are gone"


def test_the_phantom_choice_contract():
    """The server sends `phantom_url`; the sheet shows that button only when it is there.

    Same two silent failures the deposit-address contract guards against: a renamed field
    (the payer loses the wallet-scoped route — the way out on a device that hands Solana
    Pay links to another wallet) and a button that defaults to visible, which would offer
    the PREVIOUS sheet's URL — a payment to somebody else's invoice.
    """
    html = HTML.read_text()
    src = MINIAPP.read_text()
    assert '"phantom_url"' in src, "the invoice endpoint no longer sends the field"
    assert "invoice.phantom_url" in html, "the sheet no longer reads the field"
    assert 'id="phantomBtn" style="display:none"' in html, \
        "hidden until this invoice's own URL arrives"
    assert "phantomReset.style.display = 'none'" in html, \
        "and a previous sheet's URL is cleared on open"
    assert "openExternal(invoice.phantom_url + '?token=' + payToken)" in html, (
        "the choice opens that URL, carrying the token picked in the sheet")


def test_the_app_pay_path_matches_the_registered_route():
    """A rename on either side sends the wallet to a 404 inside its own browser —
    invisible to every test that calls the handler directly."""
    assert "'/pay/'" in HTML.read_text(), "openWallet stopped building the pay path"
    assert "/pay/{payment_id}" in _registered_routes()


def test_every_element_the_app_binds_exists_in_the_markup():
    html = HTML.read_text()
    refs = set(re.findall(r"getElementById\('([A-Za-z0-9_:-]+)'\)", html))
    ids = set(re.findall(r'id="([A-Za-z0-9_:-]+)"', html))
    assert refs, "no getElementById() calls found - did the app script move?"
    missing = sorted(refs - ids)
    assert not missing, f"app binds ids missing from the markup: {missing}"


def test_privacy_payload_key_contract():
    src = MINIAPP.read_text()
    start = src.index("async def _api_privacy")
    body = src[start:src.index("\nasync def ", start + 1)]
    assert '"html"' in body, "/api/privacy no longer returns the html key"
    assert "r.html" in HTML.read_text(), "the sheet no longer reads r.html"


Q = chr(39)
KEY_LINE = re.compile("^ *" + Q + "([A-Za-z][A-Za-z ]*)" + Q + " *:", re.M)


def _keys(html, header):
    """Keys of a single-quoted JS object literal, e.g. STATUS_COLORS."""
    start = html.index(header) + len(header)
    return set(KEY_LINE.findall(html[start:html.index("};", start)]))


def test_route_map_covers_every_status_the_app_colours():
    """routeStep() had a blanket fallback, so Exception and Unavailable cards
    drew Label -> Transit (current) -> Delivered while the badge said otherwise.
    The rail map must therefore agree with the colour map key for key."""
    html = HTML.read_text()
    colors = _keys(html, "const STATUS_COLORS = {")
    steps = _keys(html, "const ROUTE_STEP = {")
    assert colors, "STATUS_COLORS keys not found"
    assert steps, "ROUTE_STEP keys not found"
    assert colors == steps, "maps disagree: " + repr(sorted(colors ^ steps))
    assert "hasOwnProperty.call(ROUTE_STEP, status)" in html
    # 2026-09-20: 4-step rail — Delivered got its own stop (step 4), separate
    # from Out for Delivery (step 3).
    assert "'Delivered':            4" in html
    assert "'Out for Delivery':     3" in html


def test_no_unpinned_cdn_scripts_in_the_dashboard():
    """The dashboard must not execute code fetched from a mutable remote branch.

    A changed upstream file runs with full dashboard privileges, right next to
    the Telegram initData credentials — and the library it loaded draws the
    Solana payment QR. Telegram's own SDK is the one deliberate exception: it is
    the platform's first-party endpoint (telegram.org), served versionless by
    design, and a vendored copy would go stale against the platform API.
    """
    html = HTML.read_text()
    srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)
    assert srcs, "no script srcs found - did the tags move?"
    external = [s for s in srcs if s.startswith(("http://", "https://", "//"))]
    assert external == ["https://telegram.org/js/telegram-web-app.js"], \
        f"unexpected external script(s): {external}"
    assert "vendor/qrcode.min.js" in srcs, "the QR library must be served locally"
    # The prompt's own check: no moving-branch reference anywhere under webapp/.
    hits = [str(p.relative_to(ROOT)) for p in (ROOT / "webapp").rglob("*")
            if p.is_file() and "@master" in p.read_text(errors="ignore")]
    assert hits == [], f"mutable branch reference(s) left in webapp/: {hits}"


def test_the_vendored_qr_library_matches_its_recorded_hash():
    """Pinning is only real if the recorded digest matches the shipped bytes."""
    vendor = ROOT / "webapp" / "vendor" / "qrcode.min.js"
    assert vendor.is_file(), "the vendored QR library is missing"
    readme = (ROOT / "webapp" / "vendor" / "README").read_text()
    digest = hashlib.sha256(vendor.read_bytes()).hexdigest()
    assert digest in readme, "vendor/README does not record this file's sha256"
    # ...and the app can actually reach it: the route must be registered.
    assert "/vendor/{name}" in _registered_routes(), \
        "the vendor route is missing, so the QR library would 404"


def test_card_badge_reports_a_failed_lookup():
    """A stale status must say so on the card, or a failed lookup reads as"""
    """fresh news (and the field would rot unused, the way badge did)."""
    html = HTML.read_text()
    assert "p.stale ? " in html
    assert "last known" in html


def test_the_outside_telegram_fallback_triggers_on_empty_initdata():
    """telegram-web-app.js loads in ANY browser, so window.Telegram.WebApp exists in
    plain Safari too — with an EMPTY initData. The old `if (!T)` gate only caught
    "the script failed to load", so a browser visitor booted a half-app: every api()
    call 401s (nothing to sign with) and the Pro sheet's QR box sat on
    "Preparing QR…" forever. Verified by running the two lines under JavaScriptCore.
    """
    html = HTML.read_text()
    assert "const inTelegram = !!(T && T.initData);" in html
    assert "if (!inTelegram) {" in html, "the fallback must key on initData"
    assert "if (!T) {" not in html, "the object-only gate (the bug) is back"
    # The boot handshake: the DOM watchdog's 5s timer rewrites #list wholesale
    # when __packeyReady is still falsy — on the LANDING page too, which is how
    # the intro used to be destroyed ~5s after every browser visit (2026-09-28).
    # The landing branch must therefore mark itself booted.
    landing = html.split("if (!inTelegram) {", 1)[1].split("} else {", 1)[0]
    assert "__packeyReady = true" in landing, \
        "the landing must set the boot flag, or the watchdog eats it after 5s"


def test_the_boot_watchdog_tells_a_truncated_page_from_a_broken_script():
    """A page cut off mid-download (a fetch racing a file write, a dying tunnel)
    ends before the app script closes — the browser runs nothing, and the only
    honest message is 'reload', not 'syntax error'. The end marker is the tell:
    a complete page has it, a truncated one cannot."""
    html = HTML.read_text()
    assert html.count('id="pageEnd"') == 1, "the end marker must exist exactly once"
    watchdog = html.split("window.__packeyBootFail", 1)[1]
    assert "getElementById('pageEnd')" in watchdog, \
        "the 5s timer must check the marker before blaming the script"
    assert "didn't finish loading" in watchdog


def test_no_script_block_can_break_out_of_its_element():
    """A JS string containing a script-closing sequence ends the <script> element
    at that point in the HTML parser — the rest of the block becomes stray
    markup, later blocks turn into text, and the app renders nothing while every
    source-level check still passes. Balanced tags plus clean blocks is the
    structural guard."""
    import re
    html = HTML.read_text()
    # Count only REAL opening tags (line-start); "<script" also appears inside a
    # JS comment in the early watchdog, which the parser never treats as a tag.
    openings = len(re.findall(r"(?m)^\s*<script[ >]", html))
    assert openings == html.count("</script>"), "unbalanced script tags"
    for i, block in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
        assert "</script" not in block, \
            f"script block {i} contains a closing sequence — it would break out"


def test_the_fallback_hands_the_user_a_way_into_telegram():
    """A dead end is not a fallback: the panel must offer the bot, styled like the
    app's own buttons."""
    panel = HTML.read_text().split("if (!inTelegram) {", 1)[1].split("} else {", 1)[0]
    assert "Open Packey in Telegram" in panel
    assert 'href="https://t.me/packeybot"' in panel, "no way in"
    assert 'class="btn primary"' in panel, "it must look like the app's buttons"


def test_a_failed_invoice_is_also_shown_in_the_qr_box():
    """The reported symptom: the QR box kept its "Preparing QR…" placeholder after
    the failure was already known, so the sheet looked like it was still working."""
    sheet = HTML.read_text().split("async function openProSheet()", 1)[1]
    sheet = sheet.split("\nfunction closeProSheet", 1)[0]
    assert "st.textContent = e.message;" in sheet
    assert "esc(e.message)" in sheet, "the error never reaches the QR box"
