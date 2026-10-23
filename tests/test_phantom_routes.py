"""The /pay/<id>/phantom rescue route, end to end with Phantom simulated.

Why the route exists: the in-browser handoff needs the wallet to inject a provider into
OUR page, and "solana:" is answered by whichever wallet the OS prefers — a payer landed
in Coinbase Wallet's empty send screen (2026-09-24). Here the transaction travels inside
an encrypted deep-link payload that ONLY Phantom can answer, so these tests drive the
real handlers with a fake chain and a Phantom that does its half of the crypto for real.
"""
import asyncio
import json
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from nacl.public import Box, PrivateKey, PublicKey

import config
import miniapp
import phantom_link
import pro
import solana_tx

WALLET = "BOTwallet1111111111111111111111111111111111"
OURS = pro._b58encode(bytes([7]) * 32)
PAYER_ACCOUNT = pro._b58encode(bytes([6]) * 32)
PAYER = pro._b58encode(bytes([5]) * 32)
BLOCKHASH = pro._b58encode(bytes([8]) * 32)
SIGNATURE = "Sig" + "1" * 40
PUBLIC_URL = "https://pay.example"


class _Req:
    def __init__(self, payment_id, body=None):
        self.match_info = {"payment_id": payment_id}
        self.path = f"/pay/{payment_id}/phantom"
        self.headers = {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class Phantom:
    """Phantom's half: its own keypair, decrypting what we send and sealing its answer."""

    def __init__(self, dapp_public_b58):
        self.private = PrivateKey.generate()
        self.public_b58 = solana_tx.b58encode(bytes(self.private.public_key))
        self.box = Box(self.private, PublicKey(solana_tx.b58decode(dapp_public_b58)))

    def seal(self, payload):
        nonce = bytes(range(24))
        sealed = self.box.encrypt(json.dumps(payload).encode(), nonce)
        return solana_tx.b58encode(nonce), solana_tx.b58encode(bytes(sealed.ciphertext))

    def open_query(self, link):
        params = {key: unquote(values[0]) for key, values in
                  parse_qs(urlparse(link).query).items()}
        return json.loads(self.box.decrypt(solana_tx.b58decode(params["payload"]),
                                           solana_tx.b58decode(params["nonce"])).decode())


def _fake_rpc(monkeypatch, calls, payer_mints=("USDC",)):
    async def fake(method, params):
        calls.append(method)
        if method == "getTokenAccountsByOwner":
            owner, query = params[0], params[1]
            mint = (query or {}).get("mint")
            if owner == PAYER:
                # The payer holds what the test says and nothing else: this is what makes
                # the one-token-then-the-other fallback observable.
                if mint not in {getattr(config, f"{name}_MINT") for name in payer_mints}:
                    return {"value": []}
                address = PAYER_ACCOUNT
            else:
                address = OURS
            return {"value": [{"pubkey": address, "account": {"data": {"parsed": {
                "info": {"tokenAmount": {"decimals": 6, "uiAmount": 5.0}}}}}}]}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": BLOCKHASH}}
        if method == "sendTransaction":
            return SIGNATURE
        raise AssertionError(f"unexpected RPC call {method}")
    monkeypatch.setattr(pro, "_sol_rpc", fake)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    saved_pending = dict(pro._pending_payments)
    saved_expired = dict(pro._expired_payments)
    monkeypatch.setattr(config, "BOT_USDC_ADDRESS", WALLET)
    monkeypatch.setattr(config, "MINI_APP_URL", PUBLIC_URL)
    monkeypatch.setattr(config, "PHANTOM_DEEPLINK", True)
    pro._pending_payments.clear()
    pro._expired_payments.clear()
    pro._our_token_accounts.clear()
    miniapp._pay_tx_hits.clear()
    phantom_link._SESSIONS.clear()
    yield
    pro._pending_payments.clear()
    pro._pending_payments.update(saved_pending)
    pro._expired_payments.clear()
    pro._expired_payments.update(saved_expired)
    pro._our_token_accounts.clear()
    miniapp._pay_tx_hits.clear()
    phantom_link._SESSIONS.clear()


def _invoice():
    """A real invoice record, registered the way the app registers one."""
    payment_id = "inv-0123456789abcdef"
    pro._pending_payments[payment_id] = {
        "chat_id": 9001, "amount": 0.016055, "amount_micros": 16055,
        "memo": "pkabc123", "reference": pro.new_reference(),
        "deposit_address": None, "base_micros": 10000, "created": 0.0,
    }
    return payment_id


def _post(handler, payment_id, body=None):
    return asyncio.run(handler(_Req(payment_id, body)))


def _get(payment_id):
    return asyncio.run(miniapp._webapp_phantom(_Req(payment_id)))


def _json(response):
    return json.loads(response.body)


def _connected(monkeypatch, calls, payer_mints=("USDC",)):
    """Take the route as far as 'Phantom has connected', the way the page does."""
    _fake_rpc(monkeypatch, calls, payer_mints=payer_mints)
    payment_id = _invoice()
    started = _post(miniapp._pay_phantom_connect, payment_id)
    assert started.status == 200, "the session must exist before Phantom can answer it"
    phantom = Phantom(phantom_link.get_session(payment_id)["dapp_public"])
    nonce, data = phantom.seal({"public_key": PAYER, "session": "phantom-session-1"})
    answer = _post(miniapp._pay_phantom_session, payment_id, {
        "phantom_encryption_public_key": phantom.public_b58, "nonce": nonce,
        "data": data})
    return payment_id, phantom, answer


# ── the gate, and what an unknown invoice sees ──────────────────────────────

def test_the_whole_route_disappears_when_the_knob_is_off(monkeypatch):
    """A capability switch must actually switch the capability off — and do nothing."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    monkeypatch.setattr(config, "PHANTOM_DEEPLINK", False)
    payment_id = _invoice()
    for handler in (miniapp._pay_phantom_connect, miniapp._pay_phantom_session,
                    miniapp._pay_phantom_sign, miniapp._pay_phantom_submit):
        assert _post(handler, payment_id, {}).status == 404
    assert _get(payment_id).status == 404
    assert calls == [], "and nothing reached the chain"


def test_an_unknown_invoice_is_never_answered(monkeypatch):
    calls = []
    _fake_rpc(monkeypatch, calls)
    assert _post(miniapp._pay_phantom_connect, "inv-0000000000000000").status == 404
    assert _get("inv-0000000000000000").status == 404, "the expired page, as on /pay"
    assert calls == []


def test_without_a_public_url_the_route_refuses_rather_than_half_working(monkeypatch):
    """Phantom needs an absolute https redirect: with no MINI_APP_URL a payer could never
    be brought back, so the route says so instead of opening a dead end."""
    calls = []
    _fake_rpc(monkeypatch, calls)
    monkeypatch.setattr(config, "MINI_APP_URL", None)
    payment_id = _invoice()
    assert _post(miniapp._pay_phantom_connect, payment_id).status == 409
    assert phantom_link.get_session(payment_id) is None, "and no session was started"


# ── connect ─────────────────────────────────────────────────────────────────

def test_connect_returns_a_wallet_scoped_link_and_a_url_fallback(monkeypatch):
    calls = []
    _fake_rpc(monkeypatch, calls)
    payment_id = _invoice()
    response = _post(miniapp._pay_phantom_connect, payment_id)
    links = _json(response)

    assert response.status == 200
    assert links["scheme"].startswith("phantom://v1/connect?")
    assert links["universal"].startswith("https://phantom.app/ul/v1/connect?")
    redirect = parse_qs(urlparse(links["scheme"]).query)["redirect_link"][0]
    assert redirect == f"{PUBLIC_URL}/pay/{payment_id}/phantom?step=connect", (
        "the redirect must come back to this invoice's own page")
    assert "session" not in json.dumps(links), "no secrets in the link"
    assert phantom_link.get_session(payment_id)["session"] == "", "not connected yet"


def test_the_connect_answer_is_decrypted_into_the_session(monkeypatch):
    calls = []
    payment_id, phantom, answer = _connected(monkeypatch, calls)
    assert answer.status == 200 and _json(answer)["ok"] is True
    record = phantom_link.get_session(payment_id)
    assert (record["payer"], record["session"]) == (PAYER, "phantom-session-1")


def test_a_connect_answer_that_cannot_be_read_is_refused(monkeypatch):
    calls = []
    _fake_rpc(monkeypatch, calls)
    payment_id = _invoice()
    _post(miniapp._pay_phantom_connect, payment_id)
    response = _post(miniapp._pay_phantom_session, payment_id,
                     {"phantom_encryption_public_key": "AA", "nonce": "AA", "data": "AA"})
    assert response.status == 400
    assert phantom_link.get_session(payment_id)["session"] == "", "nothing stored"


# ── sign ────────────────────────────────────────────────────────────────────

def test_signing_is_refused_before_connecting(monkeypatch):
    calls = []
    _fake_rpc(monkeypatch, calls)
    payment_id = _invoice()
    assert _post(miniapp._pay_phantom_sign, payment_id, {"token": "USDC"}).status == 409


def test_the_signed_payload_carries_exactly_the_transaction_we_built(monkeypatch):
    """The point of the route: what Phantom shows the payer to sign is what we built —
    this invoice's amount, our address, the memo — with no amount or address coming from
    the page."""
    calls = []
    payment_id, phantom, _ = _connected(monkeypatch, calls)
    response = _post(miniapp._pay_phantom_sign, payment_id, {"token": "USDC"})
    links = _json(response)

    assert response.status == 200
    assert links["scheme"].startswith("phantom://v1/signTransaction?"), (
        "signTransaction, not the deprecated signAndSendTransaction")
    opened = phantom.open_query(links["scheme"])
    assert opened["session"] == "phantom-session-1"
    ours = asyncio.run(pro.payment_transaction_bytes(
        pro._pending_payments[payment_id], PAYER, config.USDC_MINT))
    assert opened["transaction"] == solana_tx.b58encode(ours), (
        "the wallet must be handed the transaction the backend built")


def test_a_wallet_holding_the_other_token_is_not_stopped(monkeypatch):
    """The token is picked on another screen. A payer whose wallet holds USDT must still
    be able to pay — and be told which token was used, since the wallet shows what it
    signs — instead of meeting a refusal they cannot act on."""
    calls = []
    payment_id, phantom, _ = _connected(monkeypatch, calls, payer_mints=("USDT",))
    response = _post(miniapp._pay_phantom_sign, payment_id, {"token": "USDC"})
    data = _json(response)

    assert response.status == 200
    assert (data["token"], data["switched"]) == ("USDT", True)
    ours, reason = asyncio.run(pro.payment_transaction_parts(
        pro._pending_payments[payment_id], PAYER, config.USDT_MINT))
    assert reason == ""
    assert phantom.open_query(data["scheme"])["transaction"] == solana_tx.b58encode(ours), (
        "it is the same invoice amount, in the token the wallet actually holds")


def test_a_wallet_holding_neither_token_gets_a_sentence(monkeypatch):
    """The reported failure (2026-09-24): the page said "unavailable" and told nobody
    anything. Now the refusal names the reason AND says what to do about it."""
    calls = []
    payment_id, _, _ = _connected(monkeypatch, calls, payer_mints=())
    response = _post(miniapp._pay_phantom_sign, payment_id, {"token": "USDC"})
    data = _json(response)

    assert response.status == 409
    assert data["error"] == "no_payer_account"
    assert "wallet holds no USDC or USDT" in data["message"], (
        "a payer gets a sentence, not a status code")
    # The payer is inside Phantom here, so the old "open the wallet you pay from" /
    # "send to the address below" advice confused them (2026-09-25): the sentence now
    # names the two things they can actually do. The page still opens its manual
    # disclosure on every refusal, so the address route stays one tap away.
    assert "Fund it first, or switch to the wallet you pay from" in data["message"]


def test_an_unknown_token_is_refused_before_building_anything(monkeypatch):
    calls = []
    payment_id, _, _ = _connected(monkeypatch, calls)
    before = list(calls)
    assert _post(miniapp._pay_phantom_sign, payment_id, {"token": "DOGE"}).status == 400
    assert calls == before, "nothing was asked of the chain"


# ── submit ──────────────────────────────────────────────────────────────────

def test_a_submit_without_a_sign_is_refused(monkeypatch):
    """/sign is what arms the integrity fingerprint: a session that reaches
    /submit without one has nothing to verify the bytes against, and /submit
    must never relay an unverified transaction (audit 2026-10-04). The honest
    sign-then-submit path is covered by the correct-transaction test below."""
    calls = []
    payment_id, phantom, _ = _connected(monkeypatch, calls)
    nonce, data = phantom.seal({"transaction": "signed-transaction-b58"})
    before = list(calls)
    response = _post(miniapp._pay_phantom_submit, payment_id,
                     {"nonce": nonce, "data": data})
    assert response.status == 409
    assert calls == before, "nothing was relayed to the chain"


def test_a_submission_that_cannot_be_read_is_refused(monkeypatch):
    calls = []
    payment_id, _, _ = _connected(monkeypatch, calls)
    assert _post(miniapp._pay_phantom_submit, payment_id,
                 {"nonce": "AA", "data": "AA"}).status == 400
    assert "sendTransaction" not in calls, "nothing was submitted"


def test_submitting_without_a_session_is_refused(monkeypatch):
    calls = []
    _fake_rpc(monkeypatch, calls)
    payment_id = _invoice()
    response = _post(miniapp._pay_phantom_submit, payment_id,
                     {"nonce": "a", "data": "b"})
    assert response.status == 409


# ── tx integrity: what came back must be what we built ─────────────────────

def test_the_unsigned_transaction_is_fingerprinted_during_sign(monkeypatch):
    """The hash stored in the session must match the real transaction's message
    bytes — otherwise the submit-side check is comparing against nothing."""
    calls = []
    payment_id, phantom, _ = _connected(monkeypatch, calls)
    _post(miniapp._pay_phantom_sign, payment_id, {"token": "USDC"})
    record = phantom_link.get_session(payment_id)
    assert "tx_msg_hash" in record, "the hash was not stored"
    assert isinstance(record["tx_msg_hash"], str)
    assert len(record["tx_msg_hash"]) == 64, "SHA-256 hex is 64 characters"
    # Also confirm it is STABLE (re-sign does not replace it unless the tx changes).
    old = record["tx_msg_hash"]
    _post(miniapp._pay_phantom_sign, payment_id, {"token": "USDC"})
    assert record["tx_msg_hash"] == old, "same parameters produce the same hash"


def test_a_submission_with_the_wrong_transaction_is_refused(monkeypatch):
    """The wallet (or anyone on the path) could modify the unsigned transaction
    before approving it. The signed result would decrypt, but its message bytes
    would not match the fingerprint taken at sign-request time — so the
    submission should be rejected before any money moves."""
    calls = []
    payment_id, phantom, _ = _connected(monkeypatch, calls)
    # Store the hash by going through sign.
    _post(miniapp._pay_phantom_sign, payment_id, {"token": "USDC"})
    # Phantom returns a DIFFERENT transaction than what we built: a modified one.
    nonce, data = phantom.seal({"transaction": solana_tx.b58encode(b"\xfe" * 100)})
    response = _post(miniapp._pay_phantom_submit, payment_id,
                     {"nonce": nonce, "data": data})
    assert response.status == 409
    assert "sendTransaction" not in calls, "nothing was sent to the chain"


def test_a_submission_with_the_correct_transaction_still_goes_through(monkeypatch):
    """The new integrity check must not break the normal flow: a wallet that
    signed exactly what we built should be accepted and submitted."""
    calls = []
    payment_id, phantom, _ = _connected(monkeypatch, calls)
    sign_rsp = _post(miniapp._pay_phantom_sign, payment_id, {"token": "USDC"})
    assert sign_rsp.status == 200
    # Extract the transaction we built — it is what Phantom signed.
    built = phantom.open_query(_json(sign_rsp)["scheme"])["transaction"]
    nonce, data = phantom.seal({"transaction": built})
    response = _post(miniapp._pay_phantom_submit, payment_id,
                     {"nonce": nonce, "data": data})
    assert response.status == 200
    assert _json(response) == {"ok": True, "signature": SIGNATURE}
    assert "sendTransaction" in calls


# ── the page and its limits ─────────────────────────────────────────────────

def test_the_page_offers_connect_then_pay_and_never_names_the_payer(monkeypatch):
    calls = []
    payment_id = _invoice()
    before = _get(payment_id).text
    assert "Connect Phantom" in before and 'data-step="connect"' in before
    assert "0.016055" in before and WALLET in before
    assert "9001" not in before and "chat_id" not in before, "no payer data, ever"

    payment_id, _, _ = _connected(monkeypatch, calls)
    after = _get(payment_id).text
    assert "Pay 0.016055 in Phantom" in after and 'data-step="sign"' in after
    assert "phantom-session-1" not in after, "the session token stays server-side"


def test_repeated_calls_are_bounded(monkeypatch):
    calls = []
    _fake_rpc(monkeypatch, calls)
    payment_id = _invoice()
    statuses = [_post(miniapp._pay_phantom_connect, payment_id).status
                for _ in range(miniapp._PAY_TX_PER_WINDOW + 2)]
    assert statuses[-1] == 429, "a loop must not be free load"


# ── auto-advance: the token rides the connect hop, the wallet rides the answer ──

def test_the_connect_hop_carries_the_payers_token_back(monkeypatch):
    """The page auto-advances from Phantom's connect answer, and at that moment its own
    query params are gone — Phantom returns to the redirect_link, not to the URL the page
    was opened with. So the token choice must ride on that redirect, or a USDT payer is
    silently asked for USDC on the second hop (found while wiring the auto-advance,
    2026-09-25)."""
    _fake_rpc(monkeypatch, [])
    payment_id = _invoice()
    with_token = _json(_post(miniapp._pay_phantom_connect, payment_id, {"token": "USDT"}))
    redirect = parse_qs(urlparse(with_token["scheme"]).query)["redirect_link"][0]
    assert redirect.endswith("?step=connect&token=USDT")

    bare = _json(_post(miniapp._pay_phantom_connect, payment_id, {}))
    plain = parse_qs(urlparse(bare["scheme"]).query)["redirect_link"][0]
    assert plain.endswith("?step=connect"), "no token offered, no token invented"


def test_the_connect_answer_names_the_wallet_the_page_should_show(monkeypatch):
    """A payer who connected the wrong (or an empty) wallet should learn it here rather
    than at the sign step, and the auto-advance needs the key without a reload. Only the
    PUBLIC key travels — and the page shows it truncated: recognisable, one line."""
    calls = []
    payment_id, _, answer = _connected(monkeypatch, calls)
    data = _json(answer)
    assert answer.status == 200 and data["ok"] is True
    assert data["payer"] == PAYER, "the page needs the key to name the wallet"
    text = _get(payment_id).text
    assert f"Connected: {PAYER[:4]}\u2026{PAYER[-4:]}" in text
    assert PAYER not in text, "and only in its truncated form"

    phantom_link._SESSIONS.clear()      # a payer who has not connected anything
    fresh = _get(_invoice()).text
    assert f"Connected: {PAYER[:4]}" not in fresh, "nothing to name before connecting"
    assert 'class="sub hide" id="who"' in fresh, (
        "the element still exists: the auto-advance fills it in place")


def test_a_credited_invoice_says_payment_received(monkeypatch):
    """A credited invoice leaves both pools, so a payer who paid and then reopened the
    link was told it had EXPIRED — a completed payment reading as failure (2026-09-25).
    The receipt trail already records the payment_id, so nothing new has to be kept."""
    _fake_rpc(monkeypatch, [])
    payment_id = _invoice()
    saved = list(pro._receipts)
    try:
        pro._pending_payments.pop(payment_id, None)
        assert _get(payment_id).status == 404, "not credited yet: the honest dead link"
        pro._receipts.append({"chat_id": 9001, "payment_id": payment_id})
        response = _get(payment_id)
        assert response.status == 200, "a paid link is not a dead link"
        assert "Payment received" in response.text
        assert "Pro is active" in response.text
    finally:
        pro._receipts[:] = saved