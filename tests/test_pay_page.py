"""The public pay page the wallets' in-app browsers land on.

Why it exists: a wallet can only show a PREFILLED payment sheet when its own
in-app browser navigates to the solana: URI (that is the Solana Pay mechanism — no
wallet has a "pay this URL" deep link). So the Mini App opens the wallet at this
page, and the page offers the handoff on a TAP — never automatically, because every
Solana wallet claims the "solana:" scheme, so the OS picks the handler and may hand
the URI to a wallet the payer did not choose. The payment_id is the whole
authentication, exactly like a share token, so the page must render the invoice's
public payment facts and nothing about the payer.
"""
import asyncio
import hashlib
import hmac
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

import config
import miniapp
import pro
import storage

WALLET = "BOTwallet1111111111111111111111111111111111"
USER = 9001


class _Req:
    """Duck-typed aiohttp request: what the route and the pay page touch."""

    def __init__(self, payment_id, body=None):
        self.match_info = {"payment_id": payment_id}
        self.path = f"/pay/{payment_id}"
        self.headers = {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _AuthReq:
    def __init__(self, headers):
        self.headers = headers


def _auth(monkeypatch, user_id=USER):
    monkeypatch.setattr(config, "_bot_token", "123456:TESTTOKEN")
    params = {"user": json.dumps({"id": user_id}, separators=(",", ":")),
              "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest()
    return {"Authorization": "tma " + urlencode(
        {**params, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """A wallet address, one chat, and empty pools — no live invoice leaks in."""
    saved_users = dict(pro._pro_users)
    saved_pending = dict(pro._pending_payments)
    saved_expired = dict(pro._expired_payments)
    monkeypatch.setattr(config, "BOT_USDC_ADDRESS", WALLET)
    storage.track_sessions.clear()
    storage.track_sessions[USER] = {"RR123456789CN": {"name": "Box",
                                                     "status": "In Transit"}}
    pro._pending_payments.clear()
    pro._expired_payments.clear()
    pro._our_token_accounts.clear()
    miniapp._pay_tx_hits.clear()
    yield
    storage.track_sessions.clear()
    pro._pro_users.clear()
    pro._pro_users.update(saved_users)
    pro._pending_payments.clear()
    pro._pending_payments.update(saved_pending)
    pro._expired_payments.clear()
    pro._expired_payments.update(saved_expired)
    pro._our_token_accounts.clear()
    miniapp._pay_tx_hits.clear()


def _invoice(monkeypatch):
    """A real invoice, created the way the app creates one."""
    resp = asyncio.run(miniapp._api_create_invoice(_AuthReq(_auth(monkeypatch))))
    return json.loads(resp.body)


def _pay(payment_id):
    return asyncio.run(miniapp._webapp_pay(_Req(payment_id)))


def test_the_uri_carries_what_the_wallet_sheet_shows():
    uri = miniapp._solana_pay_url("0.016055", config.USDC_MINT, "tok123")
    assert uri.startswith(f"solana:{WALLET}?")
    assert "amount=0.016055" in uri
    assert f"spl-token={config.USDC_MINT}" in uri
    assert "label=Packey%20Pro" in uri
    assert "message=Pro%20upgrade" in uri
    assert "memo=tok123" in uri
    assert "reference=" not in uri, "no reference until the invoice carries one"


def test_the_uri_gains_a_reference_when_the_invoice_has_one():
    """Forward compatibility with the reference-matching change: one builder, so a
    later edit cannot add the param to the page and drop it from the API."""
    uri = miniapp._solana_pay_url("0.016055", config.USDC_MINT, "tok123",
                                  reference="RefKey1111111111111111111111111111111111")
    assert "reference=RefKey" in uri
    assert "message=Pro%20upgrade" in uri, "the new param must not evict the old"


def test_the_page_shows_the_amount_and_never_the_payer(monkeypatch):
    inv = _invoice(monkeypatch)
    resp = _pay(inv["payment_id"])

    assert resp.status == 200
    assert inv["amount_str"] in resp.text, "the exact amount is the whole point"
    assert WALLET in resp.text
    assert f"solana:{WALLET}" in resp.text
    assert "vendor/qrcode.min.js" in resp.text, "the QR uses the vendored library"
    assert 'src="/vendor/qrcode.min.js"' in resp.text, (
        "an absolute path: the page lives at /pay/<id>, so a relative one would "
        "resolve to /pay/vendor/… and 404 — the QR would never draw")
    # The allowlist: nothing about the payer reaches a public page.
    assert str(USER) not in resp.text
    assert "chat_id" not in resp.text
    assert "RR123456789CN" not in resp.text, "no package data either"


def test_the_page_has_one_action_and_it_opens_the_solana_pay_link(monkeypatch):
    """Simplified 2026-09-24: one button, one tap, the URI the spec defines. The ladder
    of wallet bridges that used to live here never demonstrably worked on a payer's
    phone, and every extra control was another thing to get wrong on a payment screen."""
    inv = _invoice(monkeypatch)
    text = _pay(inv["payment_id"]).text
    assert 'id="hop"' in text and 'hop.textContent = "Open " + amount + " in my wallet"' in text
    assert "openExternal(urls[token]);" in text, "the one action opens the URI"
    assert "signAndSendTransaction" not in text and "wallet-standard" not in text, (
        "the bridge ladder is gone from the page")
    assert 'id="diag"' not in text, "and so is the diagnostics line"
    assert 'id="pay"' not in text and 'id="autost"' not in text, (
        "no second control competes with the button")
    assert '<div id="manual">' in text, "the escape hatch stays rendered"
    assert "QRCode.toCanvas" in text, "and the QR encodes the same URI"
    assert "window.location.href = urls[token]" not in text, (
        "still nothing navigates by itself: only the tap does")


def test_the_phantom_button_leads_the_pay_page_and_the_knob_removes_it(monkeypatch):
    """This page is the path a payer reaches when their wallet would not open, which is
    exactly the case the deep-link route exists for — so it is a full-width button above
    the QR, not the footnote it used to be (2026-09-25, after the iOS confirmation that
    the plain Solana Pay link opens nothing at all)."""
    monkeypatch.setattr(config, "MINI_APP_URL", "https://pay.example")
    inv = _invoice(monkeypatch)
    text = _pay(inv["payment_id"]).text
    assert "Pay in the Phantom app" in text and 'id="phantomGo"' in text
    assert f'data-base="{inv["phantom_url"]}"' in text, (
        "the button points at this invoice's own Phantom page")
    assert text.index('id="phantomGo"') < text.index('id="qr"'), (
        "and it leads: above the QR and the manual fallback")
    assert 'go.dataset.base + "?token=" + token' in text, (
        "it carries the token picked on this page, like the sheet's button does")
    assert 'id="rescue"' not in text, "one route, not two"

    monkeypatch.setattr(config, "PHANTOM_DEEPLINK", False)
    off = _pay(inv["payment_id"]).text
    assert 'id="phantomGo"' not in off and "Phantom app" not in off
    assert "if (go && go.dataset.base)" in off, (
        "the JS stays guarded, so the absent button is a no-op, not an error")


def test_the_invoice_offers_the_phantom_route_only_when_it_can_work(monkeypatch):
    """`phantom_url` is what makes the sheet's Phantom button appear: absent when the
    route is switched off, and absent when Phantom has no https URL to return to."""
    monkeypatch.setattr(config, "MINI_APP_URL", "https://pay.example")
    inv = _invoice(monkeypatch)
    assert inv["phantom_url"] == f"https://pay.example/pay/{inv['payment_id']}/phantom"

    monkeypatch.setattr(config, "MINI_APP_URL", None)
    assert _invoice(monkeypatch)["phantom_url"] is None, "no redirect target, no offer"

    monkeypatch.setattr(config, "MINI_APP_URL", "https://pay.example")
    monkeypatch.setattr(config, "PHANTOM_DEEPLINK", False)
    assert _invoice(monkeypatch)["phantom_url"] is None, "the knob switches it off"


def test_the_phantom_page_re_enables_its_button_when_the_payer_comes_back(monkeypatch):
    """A payer who returns from Phantom without answering gets no errorCode and no
    nonce/data: the page must not leave them a disabled button with a stale "opening"
    status (found in the UI audit, 2026-09-24). The manual facts stay visible too, so
    the payment is never lost — only the button was."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    inv = _invoice(monkeypatch)
    text = asyncio.run(miniapp._webapp_phantom(_Req(inv["payment_id"]))).text

    assert 'addEventListener("visibilitychange"' in text, (
        "the return is observable — that is when the payer needs the button back")
    assert 'addEventListener("pageshow"' in text, (
        "and a back/forward-cache return is covered too")
    assert "act.disabled = false" in text, "the button comes back"
    assert "submitted = true" in text, (
        "guarded: a completed payment is never un-submitted by a stray return")


def test_the_amount_and_address_can_both_be_copied_off_the_page(monkeypatch):
    """The exact amount IS the payer's identifier, so a wallet that swallowed the URI
    must still be able to get the address and the amount out of this page."""
    inv = _invoice(monkeypatch)
    text = _pay(inv["payment_id"]).text
    assert 'id="copyAmt"' in text and "Copy amount" in text
    assert 'querySelector(".amt")' in text, (
        "it must copy the amount the page shows, not recompute one")


def test_the_personal_address_is_offered_when_the_invoice_has_one(monkeypatch):
    """A buyer in a wallet whose browser ignores the reference has only the amount to
    be identified by — unless they can pay their own address, which is what this block
    offers. It must be the invoice's own address, and never the chat id."""
    monkeypatch.setattr(config, "DEPOSIT_SEED", "a1" * 32)
    inv = _invoice(monkeypatch)
    address = miniapp._pro().deposit_address(USER)
    assert address and inv["deposit_address"] == address, \
        "the endpoint must record the address the page is going to show"

    text = _pay(inv["payment_id"]).text
    assert address in text
    assert 'id="depAddr"' in text and "Copy my personal address" in text
    assert "Paying from an exchange?" in text
    assert str(USER) not in text, "still no chat id on a public page"


def test_a_page_without_a_personal_address_renders_cleanly(monkeypatch):
    """Legacy invoices (written before the field) and a disabled seed: no block, no
    empty placeholder left behind."""
    monkeypatch.setattr(config, "DEPOSIT_SEED", "")
    inv = _invoice(monkeypatch)
    assert inv["deposit_address"] is None

    text = _pay(inv["payment_id"]).text
    # The copy handler's JS is always present (it is guarded), so check the markup:
    assert 'id="depAddr"' not in text and 'id="copyDep"' not in text
    assert "Paying from an exchange?" not in text
    assert "$deposit_block" not in text, "the placeholder must not leak literally"


def test_a_hostile_address_value_is_escaped(monkeypatch):
    """The value comes from our own derivation, but the page is public: escaping it
    costs nothing and an injected string must never become markup."""
    inv = _invoice(monkeypatch)
    miniapp._pro()._pending_payments[inv["payment_id"]]["deposit_address"] = (
        "<script>alert(1)</script>")
    text = _pay(inv["payment_id"]).text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_an_unknown_id_gets_the_expired_page():
    resp = _pay("inv-0000000000000000")
    assert resp.status == 404
    assert "payment link has expired" in resp.text
    assert "Reopen Packey" in resp.text


def test_a_parked_invoice_still_pays(monkeypatch):
    """A superseded/expired amount stays matchable for 24h on purpose, so 404-ing
    its page would turn away a payment that WOULD be credited."""
    inv = _invoice(monkeypatch)
    pro._expired_payments[inv["payment_id"]] = pro._pending_payments.pop(inv["payment_id"])

    resp = _pay(inv["payment_id"])

    assert resp.status == 200
    assert inv["amount_str"] in resp.text
    assert "still counts for 24" in resp.text, "it must say so honestly"


def test_the_page_is_not_cached(monkeypatch):
    """Same rule as the share pages: an iOS webview must not keep a pay page."""
    import inspect
    src = inspect.getsource(miniapp._build_miniapp_app)
    assert '_api_no_store' in src, "the no-store middleware is what prevents that"


# ── POST /pay/<id>/tx: the wallet-scoped payment ─────────────────────────────
#
# Why this route exists: handing the wallet a `solana:` URI lets the OPERATING
# SYSTEM choose which wallet takes it (every Solana wallet registers the scheme), so
# a payer whose default is another wallet gets that wallet's empty send screen and
# loses the amount. A transaction has no scheme in it.
PAYER = pro._b58encode(bytes([5]) * 32)
PAYER_ACCOUNT = pro._b58encode(bytes([6]) * 32)
OUR_ACCOUNT = pro._b58encode(bytes([7]) * 32)
BLOCKHASH = pro._b58encode(bytes([8]) * 32)


def _fake_rpc(monkeypatch, calls, decimals=6, blockhash=BLOCKHASH):
    """A Solana RPC that answers the three questions the builder asks."""
    async def fake(method, params):
        calls.append(method)
        if method == "getTokenAccountsByOwner":
            address = PAYER_ACCOUNT if params[0] == PAYER else OUR_ACCOUNT
            return {"value": [{"pubkey": address, "account": {"data": {"parsed": {
                "info": {"tokenAmount": {"decimals": decimals, "uiAmount": 5.0}}}}}}]}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": blockhash}} if blockhash else {}
        raise AssertionError(f"unexpected RPC call {method}")
    monkeypatch.setattr(pro, "_sol_rpc", fake)


def _tx(payment_id, body=None):
    return asyncio.run(miniapp._pay_transaction(_Req(payment_id, body=body)))


def test_the_transaction_carries_the_invoice_amount_and_both_accounts(monkeypatch):
    """The whole point of the route: what the payer signs is what we built."""
    import base64
    import solana_tx
    calls = []
    _fake_rpc(monkeypatch, calls)
    inv = _invoice(monkeypatch)

    resp = _tx(inv["payment_id"], {"payer": PAYER, "token": "USDC"})
    data = json.loads(resp.body)

    assert resp.status == 200
    raw = base64.b64decode(data["transaction"])
    assert raw[:65] == b"\x01" + b"\x00" * 64, "one empty signature slot, no key here"
    for key in (PAYER, PAYER_ACCOUNT, OUR_ACCOUNT, config.USDC_MINT):
        assert solana_tx.pubkey(key) in raw, f"{key} must be an account"
    assert pro._pending_payments[inv["payment_id"]]["amount_micros"].to_bytes(
        8, "little") in raw, "the exact amount"
    assert f"solana:{WALLET}" not in resp.text, "no URL: this is a transaction"
    assert "getLatestBlockhash" in calls


def test_our_receiving_account_is_looked_up_once_per_process(monkeypatch):
    """It cannot change while the address is configured, and every build paying for it
    would be a needless RPC call on a public endpoint."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    inv = _invoice(monkeypatch)
    _tx(inv["payment_id"], {"payer": PAYER, "token": "USDC"})
    first = calls.count("getTokenAccountsByOwner")
    _tx(inv["payment_id"], {"payer": PAYER, "token": "USDC"})
    assert calls.count("getTokenAccountsByOwner") == first + 1, (
        "only the payer's own account is looked up again")


def test_a_payer_without_that_token_gets_an_honest_409(monkeypatch):
    """No token account means no transaction that could work; the page then offers the
    manual route, which needs none. The refusal carries a sentence for the payer, not
    just a code (the 2026-09-24 report)."""
    async def no_accounts(method, params):
        if method == "getTokenAccountsByOwner":
            return {"value": []}
        return {"value": {"blockhash": BLOCKHASH}}
    monkeypatch.setattr(pro, "_sol_rpc", no_accounts)
    inv = _invoice(monkeypatch)

    resp = _tx(inv["payment_id"], {"payer": PAYER, "token": "USDT"})
    body = json.loads(resp.body)
    assert resp.status == 409
    assert body["error"] == "no_payer_account"
    assert body["message"], "and something a payer can act on"


def test_a_missing_blockhash_is_refused_rather_than_signed_stale(monkeypatch):
    calls = []
    _fake_rpc(monkeypatch, calls, blockhash=None)
    inv = _invoice(monkeypatch)
    assert _tx(inv["payment_id"], {"payer": PAYER, "token": "USDC"}).status == 409


def test_a_mint_with_other_decimals_is_refused(monkeypatch):
    """The invoice amount is in 1e-6 units: for any other mint those micros would mean
    a different number, so building one would be a wrong amount."""
    calls = []
    _fake_rpc(monkeypatch, calls, decimals=9)
    inv = _invoice(monkeypatch)
    assert _tx(inv["payment_id"], {"payer": PAYER, "token": "USDC"}).status == 409


def test_the_endpoint_validates_before_it_touches_the_chain(monkeypatch):
    """A malformed key or an unknown token must never reach the RPC — and must never
    become an account in a transaction."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    inv = _invoice(monkeypatch)

    assert _tx(inv["payment_id"], {"payer": "not-base58!", "token": "USDC"}).status == 400
    assert _tx(inv["payment_id"], {"payer": PAYER, "token": "DOGE"}).status == 400
    assert _tx(inv["payment_id"], None).status == 400
    assert calls == [], "nothing was asked of the chain"


def test_an_unknown_invoice_is_not_answered(monkeypatch):
    """Same rule as the page: an id in neither pool reveals nothing."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    assert _tx("inv-0000000000000000", {"payer": PAYER, "token": "USDC"}).status == 404
    assert calls == []


def test_a_parked_invoice_can_still_be_paid(monkeypatch):
    """A superseded invoice stays matchable for a day: refusing its transaction would
    turn away a payment that WOULD be credited."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    inv = _invoice(monkeypatch)
    pro._expired_payments[inv["payment_id"]] = pro._pending_payments.pop(
        inv["payment_id"])
    assert _tx(inv["payment_id"], {"payer": PAYER, "token": "usdt"}).status == 200, (
        "the token name is case-insensitive")


def test_repeated_builds_are_bounded(monkeypatch):
    """The route is public (the id is the capability) and each call costs RPC round
    trips, so a loop must not become free load."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    inv = _invoice(monkeypatch)
    statuses = [_tx(inv["payment_id"], {"payer": PAYER, "token": "USDC"}).status
                for _ in range(miniapp._PAY_TX_PER_WINDOW + 2)]
    assert statuses[:miniapp._PAY_TX_PER_WINDOW] == [200] * miniapp._PAY_TX_PER_WINDOW
    assert statuses[-1] == 429


def test_the_transaction_route_is_registered_next_to_the_page():
    """A page that posts to a path nobody registered 404s into a dead button — the
    same silent class as a renamed /api/ endpoint, so it is pinned here."""
    app = miniapp._build_miniapp_app()
    canonical = {route.resource.canonical for route in app.router.routes()
                 if hasattr(route, "resource")}
    assert "/pay/{payment_id}" in canonical, "the page itself"
    assert "/pay/{payment_id}/tx" in canonical, "what the page posts to"


class _Server:
    """The real app on a random localhost port, in its own thread.

    Handler-level tests cannot see a route that was never registered, a middleware
    that eats an anonymous request, or a cache header that went missing — and every
    one of those reaches the payer as a dead payment screen. Same pattern as the QR
    test's server: its own loop, because a closed loop leaves the socket accepted but
    never served (the request then hangs instead of failing).
    """

    def start(self):
        from aiohttp import web

        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            runner = web.AppRunner(miniapp._build_miniapp_app())
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, "127.0.0.1", 0)
            loop.run_until_complete(site.start())
            self.loop, self.runner = loop, runner
            self.port = site._server.sockets[0].getsockname()[1]
            ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(timeout=10), "the Mini App server did not start"
        return self

    def stop(self):
        try:
            asyncio.run_coroutine_threadsafe(
                self.runner.cleanup(), self.loop).result(timeout=10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)

    def post(self, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()


def test_a_payer_without_telegram_auth_can_reach_the_transaction_route(monkeypatch):
    """Someone paying inside a wallet's in-app browser has no initData — the route is
    public by design (the id is the capability), so the middleware that gates /api/
    must not eat it, and the response must not be cacheable in that webview."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    inv = _invoice(monkeypatch)
    server = _Server().start()
    try:
        status, headers, body = server.post(
            f"/pay/{inv['payment_id']}/tx", {"payer": PAYER, "token": "USDC"})
        assert status == 200, body
        assert json.loads(body)["transaction"]
        assert headers.get("Cache-Control", "").startswith("no-store"), (
            "a wallet webview must not keep a stale transaction")
        assert server.post(f"/pay/{inv['payment_id']}/tx",
                           {"payer": "not-base58!"})[0] == 400
        assert server.post("/pay/inv-0000000000000000/tx", {"payer": PAYER})[0] == 404
    finally:
        server.stop()


def test_an_unusable_reference_costs_the_reference_not_the_payment(monkeypatch):
    """The reference is optional in the spec and only ever strengthens matching, so a
    legacy or hand-edited value that is not a public key must not turn a payable
    invoice into a manual one: the same transaction is built without it."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    inv = _invoice(monkeypatch)
    record = pro._pending_payments[inv["payment_id"]]

    good = json.loads(_tx(inv["payment_id"], {"payer": PAYER, "token": "USDC"}).body)
    record["reference"] = "not-a-pubkey!"
    fixed = _tx(inv["payment_id"], {"payer": PAYER, "token": "USDC"})
    assert fixed.status == 200, "the payment is still buildable"
    without = json.loads(fixed.body)["transaction"]

    record["reference"] = ""
    bare = json.loads(_tx(inv["payment_id"], {"payer": PAYER, "token": "USDC"}).body)
    assert without == bare["transaction"], "it is the transaction with no reference"
    assert without != good["transaction"], "the reference really was dropped"


def test_an_inexpressible_memo_is_dropped_rather_than_fatal(monkeypatch):
    """Same rule as the reference: the memo is a matching aid, and a transfer needs
    none."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    inv = _invoice(monkeypatch)
    pro._pending_payments[inv["payment_id"]]["memo"] = "x" * 600
    assert _tx(inv["payment_id"], {"payer": PAYER, "token": "USDC"}).status == 200


# ── the pages' own JavaScript, and what a paid link says ───────────────────────

def _page_script(html):
    """The page's inline script block (the QR library is an external script tag)."""
    blocks = [b.split("</script>")[0] for b in html.split("<script>")[1:]]
    assert blocks, "no inline script in the page"
    return blocks[-1]


def _assert_js_parses(code, tmp_path):
    """Compile a page's JS under a real engine without running it — the technique
    tests/test_miniapp_filters.py uses for the Mini App's blocks. These pages are
    string.Templates full of braces and escapes, where a stray token is invisible to a
    substring check and fatal on a payer's phone (the pay page froze exactly that way
    once)."""
    engine = shutil.which("osascript")
    if not engine:
        pytest.skip("osascript/JavaScriptCore unavailable — page JS unchecked")
    probe = tmp_path / "page_block.js"
    probe.write_text(code)
    # Encoding 4 = NSUTF8StringEncoding.
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
    res = subprocess.run([engine, "-l", "JavaScript", "-e", jxa],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().startswith("PARSE_OK"), res.stdout.strip()


def test_both_payment_pages_compile_under_a_real_js_engine(monkeypatch, tmp_path):
    """The two pages a payer lands on when a wallet will not open. Their scripts now
    carry state machines (auto-advance, refusal mapping), so a syntax error here is a
    payment nobody can finish — and neither page had a parser test before."""
    monkeypatch.setattr(config, "MINI_APP_URL", "https://pay.example")
    inv = _invoice(monkeypatch)
    pages = {
        "pay page": _pay(inv["payment_id"]).text,
        "phantom page": asyncio.run(miniapp._webapp_phantom(_Req(inv["payment_id"]))).text,
    }
    for label, html in pages.items():
        _assert_js_parses(_page_script(html), tmp_path)


def test_a_credited_invoice_is_not_reported_as_expired(monkeypatch):
    """The payer who just paid and reopened the link must not read "expired": a credited
    invoice leaves both pools, and the receipt trail is what remembers it (2026-09-25)."""
    inv = _invoice(monkeypatch)
    pid = inv["payment_id"]
    saved = list(pro._receipts)
    try:
        pro._pending_payments.pop(pid, None)
        assert _pay(pid).status == 404, "never credited: the honest dead link stands"
        pro._receipts.append({"chat_id": USER, "payment_id": pid})
        resp = _pay(pid)
        assert resp.status == 200, "a paid link is not a dead link"
        assert "Payment received" in resp.text and "Pro is active" in resp.text
    finally:
        pro._receipts[:] = saved


def test_the_phantom_page_states_every_number_and_state_the_payer_needs(monkeypatch):
    """The 2026-09-25 UI pass, pinned: the 30-minute window, the wallet that got
    connected, the manual route behind one disclosure — and the two bare API codes
    turned into sentences."""
    monkeypatch.setattr(config, "MINI_APP_URL", "https://pay.example")
    inv = _invoice(monkeypatch)
    text = asyncio.run(miniapp._webapp_phantom(_Req(inv["payment_id"]))).text

    # The window: the session TTL and the invoice window are both 30 minutes.
    assert "Complete within 30 minutes" in text
    # Copy: the subtitle no longer explains how the page is built.
    assert "Two taps: connect Phantom, then approve the exact payment it shows" in text
    assert "This page composes nothing" not in text
    # The manual route stays — one disclosure below, opened by every error path.
    assert '<details class="manual" id="manual">' in text
    assert "Or pay manually" in text and "openManual()" in text
    # The support trail is hidden until there is an error to explain.
    assert 'class="diag hide" id="diag"' in text
    assert 'diag.classList.remove("hide")' in text
    # A payer with no Phantom installed is told what the link does.
    assert "No Phantom yet? The " in text
    assert "link takes you to install it first." in text
    # The mapped codes and the auto-advance itself live in the page script.
    for needle in ('code === "too many requests"',
                   'code === "unknown payment"',
                   'st.textContent = "Finishing\\u2026"',
                   "Talking to Phantom",
                   "Continue in Phantom",
                   'post("/sign"',
                   'act.dataset.step = "sign"'):
        assert needle in text, f"the phantom page lost: {needle}"
