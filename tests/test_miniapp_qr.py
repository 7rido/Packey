"""The payment QR must render from our own origin, with the CDN blocked.

The dashboard used to load its QR library from a mutable CDN branch
(qrcodejs@master). This test starts the REAL aiohttp app — so the
/vendor/{name} route is exercised, not stubbed — stubs only the endpoints the
flow needs (including Telegram's own SDK, which the app requires to boot at all
outside the Telegram client), and blocks every other request that would leave
the machine. Then it clicks Upgrade and asserts the QR element ends up holding a
canvas/table, with no external request made.

It skips when playwright (or its chromium binary) is unavailable: playwright is
an optional extra in this repo (see test_config_surface.OPTIONAL), never a hard
dependency, so the suite stays green without it.
"""
import asyncio
import json
import threading

import pytest

# The app refuses to run outside Telegram ("Needs Telegram"), so the SDK has to
# exist for any browser test to get past the gate. This is Telegram's own
# first-party script, not the third-party CDN dependency under test, and the
# stub keeps the test hermetic (no internet).
TELEGRAM_STUB = """
window.Telegram = {WebApp: {
  initData: 'user=%7B%22id%22%3A%7D&auth_date=1&hash=stub',
  ready: function () {}, expand: function () {},
  HapticFeedback: {impactOccurred: function () {}, notificationOccurred: function () {},
                   selectionChanged: function () {}},
  showConfirm: function (m, cb) { if (cb) cb(true); }
}};
"""

# Canned server payloads: the sheet's own render path is what must draw the QR,
# so only OUR endpoints are stubbed and the app drives everything else.
PACKAGES = {"chat_id": 1, "packages": [], "status": {}, "pro": False,
            "pro_expires_at": 0, "pkg_limit": 3, "retention_days": 30}
INVOICE = {
    "payment_id": "inv-test",
    "wallet": "11111111111111111111111111111111111111111111",
    "amount": 0.010042,
    "amount_str": "0.010042",
    "price_usd": 0.01,
    "subscription_days": 7,
    "expires_in": 1800,
    "note": "Send this exact amount — its last digits identify your payment.",
    "usdc_url": "solana:11111111111111111111111111111111111111111111?amount=0.010042",
    "usdt_url": "solana:11111111111111111111111111111111111111111111?amount=0.010042",
    "solana_pay_url": "solana:11111111111111111111111111111111111111111111?amount=0.010042",
}


class _Server:
    """The real Mini App, on a random localhost port, in its own thread.

    It cannot live in the test's own `asyncio.run(...)`: that closes the loop as
    soon as it returns, and a closed loop leaves the listening socket accepted
    but never served — the page then hangs instead of failing. Playwright's sync
    API runs its own loop too, so the server gets one of its own.
    """

    def start(self):
        import miniapp
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

        self._ready = ready
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



def test_qr_renders_from_the_vendored_library_without_network():
    sync_api = pytest.importorskip("playwright.sync_api",
                                   reason="playwright is an optional extra")
    server = _Server().start()
    external, local = [], []
    try:
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as e:            # no chromium binary installed
                pytest.skip(f"chromium unavailable for playwright: {e}")
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})

                def _route(route):
                    url = route.request.url
                    if url.startswith("https://telegram.org/"):
                        route.fulfill(status=200,
                                      content_type="application/javascript",
                                      body=TELEGRAM_STUB)
                        return
                    if url.endswith("/api/packages"):
                        route.fulfill(status=200,
                                      content_type="application/json",
                                      body=json.dumps(PACKAGES))
                        return
                    if url.endswith("/api/create_invoice"):
                        route.fulfill(status=200,
                                      content_type="application/json",
                                      body=json.dumps(INVOICE))
                        return
                    if "127.0.0.1" in url:
                        local.append(url)
                        route.continue_()
                    else:
                        external.append((route.request.resource_type, url))
                        route.abort()

                page.route("**/*", _route)
                page.goto(f"http://127.0.0.1:{server.port}/", wait_until="load",
                          timeout=15000)
                page.wait_for_function(
                    "() => typeof document.getElementById('upgradeBtn').onclick"
                    " === 'function'", timeout=10000)

                # The QR library came from our own origin, never a CDN.
                assert any(u.endswith("/vendor/qrcode.min.js") for u in local), \
                    "the dashboard never requested the vendored QR library"
                assert page.evaluate("typeof window.QRCode") == "function", \
                    "the vendored QR library did not load"

                # Drive the app's own flow: Upgrade -> invoice -> QR render.
                page.click("#upgradeBtn")
                page.wait_for_timeout(600)
                markup = page.evaluate(
                    "document.getElementById('qr').innerHTML.toLowerCase()")
                assert "<canvas" in markup or "<table" in markup, \
                    f"the QR element got no code: {markup[:120]!r}"
                # Nothing executable may come from outside our own origin, and no
                # third-party asset may be fetched at all: the Google Fonts
                # stylesheet was the last one and it is gone (a font CDN sees the
                # user's IP on every open). The Telegram SDK is stubbed above.
                assert [u for kind, u in external if kind == "script"] == [], \
                    f"a script was fetched from outside: {external}"
                assert all("fonts.googleapis.com" not in u
                           and "fonts.gstatic.com" not in u
                           for _kind, u in external), external
            finally:
                browser.close()
    finally:
        server.stop()
