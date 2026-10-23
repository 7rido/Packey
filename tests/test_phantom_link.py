"""The Phantom deep-link handshake, with Phantom's half simulated for real.

Why this shape: the crypto cannot be smoke-tested on a device from here, so the tests
play Phantom — an x25519 keypair of their own, decrypting our payloads with the shared
secret and encrypting the answers the docs describe. If our box, our encodings or our
payload fields were wrong, Phantom's half would not decrypt them, and that would fail
here instead of an operator discovering it with a real payment.

No network, and no key of anyone's: the transaction string below is arbitrary.
"""
import base64
import json
import time
from urllib.parse import unquote

import pytest
from nacl.public import Box, PrivateKey, PublicKey

import phantom_link as pl
import solana_tx

PAYER = solana_tx.b58encode(bytes([9]) * 32)
TOKEN = "session-token-abc123"          # Phantom's session token, opaque to us
TX = "AQAAAAtransactionbytes"           # stands in for the base58 unsigned transaction
REDIRECT = "https://example.invalid/pay/inv-0123456789abcdef/phantom"


class Phantom:
    """Phantom's half of the handshake: its own keypair and the shared secret."""

    def __init__(self, dapp_public_b58):
        self.private = PrivateKey.generate()
        self.public_b58 = solana_tx.b58encode(bytes(self.private.public_key))
        self.box = Box(self.private, PublicKey(solana_tx.b58decode(dapp_public_b58)))

    def answer(self, payload, encoding="base58"):
        nonce = bytes(range(24))
        sealed = self.box.encrypt(json.dumps(payload).encode(), nonce)
        if encoding == "base64":
            return (base64.b64encode(nonce).decode(),
                    base64.b64encode(bytes(sealed.ciphertext)).decode())
        return solana_tx.b58encode(nonce), solana_tx.b58encode(bytes(sealed.ciphertext))

    def open(self, nonce_b58, payload_b58):
        return json.loads(self.box.decrypt(
            solana_tx.b58decode(payload_b58), solana_tx.b58decode(nonce_b58)).decode())


@pytest.fixture(autouse=True)
def clean_sessions():
    pl._SESSIONS.clear()
    yield
    pl._SESSIONS.clear()


def _connected(payment_id="inv-0123456789abcdef"):
    record = pl.start_session(payment_id)
    phantom = Phantom(record["dapp_public"])
    nonce, data = phantom.answer({"public_key": PAYER, "session": TOKEN})
    pl.complete_connect(payment_id, phantom.public_b58, nonce, data)
    return payment_id, record, phantom


# ── the links ───────────────────────────────────────────────────────────────

def test_the_connect_link_is_wallet_scoped_and_carries_both_forms():
    record = pl.start_session("inv-0123456789abcdef")
    links = pl.connect_links(record["dapp_public"], REDIRECT, "https://pay.example")
    assert links["scheme"].startswith("phantom://v1/connect?"), (
        "the scheme only Phantom answers, which is the point")
    assert links["universal"].startswith("https://phantom.app/ul/v1/connect?"), (
        "and Phantom's recommended URL as the fallback")
    for link in links.values():
        assert f"dapp_encryption_public_key={record['dapp_public']}" in link
        assert "cluster=mainnet-beta" in link
        assert "redirect_link=https%3A%2F%2Fexample.invalid" in link, (
            "the redirect must be URL-encoded, as the spec requires")


def test_the_sign_link_is_scoped_and_keeps_its_secrets_encrypted():
    _, _, _ = _connected()
    links = pl.sign_transaction_links(pl.get_session("inv-0123456789abcdef"), TX,
                                      REDIRECT)
    assert links["scheme"].startswith("phantom://v1/signTransaction?"), (
        "signTransaction, not the deprecated signAndSendTransaction")
    assert links["universal"].startswith("https://phantom.app/ul/v1/signTransaction?")
    for link in links.values():
        assert TOKEN not in link, "the session token must travel encrypted only"
        assert PAYER not in link, "and so must the payer's key"
        assert "payload=" in link and "nonce=" in link


def test_every_session_gets_its_own_key():
    first = pl.start_session("inv-0123456789abcdef")
    second = pl.start_session("inv-fedcba9876543210")
    assert first["dapp_public"] != second["dapp_public"], (
        "Phantom recommends a fresh x25519 keypair per session")


# ── the handshake, from Phantom's side ──────────────────────────────────────

def test_the_connect_response_is_decrypted_into_payer_and_session():
    """The encryption page: the dapp derives the shared secret from
    phantom_encryption_public_key and decrypts `data` to get the user's public key."""
    payment_id, record, phantom = _connected()
    assert record["payer"] == PAYER
    assert record["session"] == TOKEN
    assert record["phantom_public"] == phantom.public_b58
    assert pl.get_session(payment_id)["payer"] == PAYER, "and it stays in the store"


def test_a_base64_response_is_accepted_too():
    """Phantom's deeplink pages show base58 and older ones show base64: since a wrong
    guess is indistinguishable from a wrong key, both are read."""
    payment_id = "inv-0123456789abcdef"
    record = pl.start_session(payment_id)
    phantom = Phantom(record["dapp_public"])
    nonce, data = phantom.answer({"public_key": PAYER, "session": TOKEN},
                                 encoding="base64")
    pl.complete_connect(payment_id, phantom.public_b58, nonce, data)
    assert pl.get_session(payment_id)["session"] == TOKEN


def test_phantom_can_decrypt_our_sign_request():
    """Phantom's half: a mis-encoded or mis-keyed payload fails right here."""
    _, record, phantom = _connected()
    query = pl.sign_transaction_links(record, TX, REDIRECT)["scheme"].split("?", 1)[1]
    params = dict(part.split("=", 1) for part in query.split("&"))
    assert phantom.open(unquote(params["nonce"]), unquote(params["payload"])) == {
        "transaction": TX, "session": TOKEN}


def test_the_signed_transaction_comes_back_out():
    payment_id, record, phantom = _connected()
    nonce, data = phantom.answer({"transaction": "signed-" + TX})
    opened = pl.decrypt(pl._box(record["private"], phantom.public_b58), nonce, data)
    assert opened["transaction"] == "signed-" + TX


# ── failure paths ───────────────────────────────────────────────────────────

def test_a_response_whose_secret_we_never_derived_is_refused():
    """A response we cannot decrypt belongs to a different session: not a reason to
    guess, and not a reason to store anything."""
    payment_id = "inv-0123456789abcdef"
    pl.start_session(payment_id)
    stranger_a, stranger_b = PrivateKey.generate(), PrivateKey.generate()
    sealed = Box(stranger_a, stranger_b.public_key).encrypt(
        json.dumps({"public_key": PAYER, "session": TOKEN}).encode(), bytes(range(24)))
    with pytest.raises(ValueError):
        pl.complete_connect(payment_id, solana_tx.b58encode(bytes(stranger_b.public_key)),
                            solana_tx.b58encode(bytes(range(24))),
                            solana_tx.b58encode(bytes(sealed.ciphertext)))
    assert pl.get_session(payment_id)["session"] == "", "and nothing was stored"


def test_a_different_phantom_keypair_addressed_to_us_is_a_valid_session():
    """Deliberately pinned: X25519 is symmetric, so a response from another Phantom
    keypair still opens when it is addressed to OUR public key. That is a legitimate
    session (Phantom may rotate its keypair), which is why the refusal above is about a
    shared secret we never derived, not about the keypair being unfamiliar."""
    payment_id = "inv-0123456789abcdef"
    record = pl.start_session(payment_id)
    phantom = Phantom(record["dapp_public"])
    nonce, data = phantom.answer({"public_key": PAYER, "session": TOKEN})
    pl.complete_connect(payment_id, phantom.public_b58, nonce, data)
    assert pl.get_session(payment_id)["payer"] == PAYER


def test_garbage_and_unknown_sessions_are_refused():
    payment_id, record, phantom = _connected()
    with pytest.raises(ValueError):
        pl.complete_connect("inv-fedcba9876543210", phantom.public_b58, "AA", "AA")
    for nonce, data in (("!!!", "!!!"), ("AA", "AA")):
        with pytest.raises(ValueError):
            pl.decrypt(pl._box(record["private"], phantom.public_b58), nonce, data)


def test_a_connect_response_without_key_or_session_is_refused():
    payment_id = "inv-0123456789abcdef"
    record = pl.start_session(payment_id)
    phantom = Phantom(record["dapp_public"])
    nonce, data = phantom.answer({"public_key": PAYER})       # no session token
    with pytest.raises(ValueError):
        pl.complete_connect(payment_id, phantom.public_b58, nonce, data)


# ── the store ───────────────────────────────────────────────────────────────

def test_sessions_expire_and_the_table_stays_bounded():
    pl._SESSION_TTL = 1
    for payment_id in ("inv-0123456789abcdef", "inv-fedcba9876543210"):
        pl.start_session(payment_id)["created"] = time.time() - 5
    assert pl.get_session("inv-0123456789abcdef") is None, "expired"
    for i in range(300):
        pl.start_session(f"inv-{i:016x}")
    assert len(pl._SESSIONS) <= 256, "a stranger looping this must not grow memory"


def test_a_connected_session_is_never_replaced():
    """The payment_id is a public capability: whoever holds the invoice URL must
    not be able to replace a payer's mid-flight session out from under them
    (2026-10-04 audit). An UNCONNECTED session can still be restarted — that is
    the recovery path for a connect that never completed."""
    payment_id = "inv-0123456789abcdef"
    first = pl.start_session(payment_id)
    phantom = Phantom(first["dapp_public"])
    nonce, data = phantom.answer({"public_key": PAYER, "session": TOKEN})
    pl.complete_connect(payment_id, phantom.public_b58, nonce, data)
    assert pl.start_session(payment_id) is None, \
        "a connected session must not be replaceable"
    assert pl.get_session(payment_id)["payer"] == PAYER, \
        "the connected payer keeps their session"
    other = "inv-fedcba9876543210"
    pl.start_session(other)
    assert pl.start_session(other) is not None, \
        "an unconnected session (connect never completed) can be restarted"


def test_overflow_evicts_the_oldest_session_not_everything():
    """The cap must stay a cap, but a burst must not drop a session its payer is
    standing in mid-payment (2026-10-04 audit)."""
    payment_id, _, _ = _connected()
    pl.get_session(payment_id)["created"] = time.time() + 1  # newer than the burst
    for i in range(300):
        pl.start_session(f"inv-{i:016x}")
    assert len(pl._SESSIONS) <= 256, "the table stays bounded"
    assert pl.get_session(payment_id) is not None, \
        "a payer mid-flow survives a burst of stranger sessions"


def test_forgetting_a_session_is_immediate():
    payment_id, _, _ = _connected()
    pl.forget_session(payment_id)
    assert pl.get_session(payment_id) is None
    with pytest.raises(ValueError):
        pl.complete_connect(payment_id, "AA", "AA", "AA")


# ── the shared encoder ──────────────────────────────────────────────────────

def test_the_new_encoder_matches_the_decoder_and_the_old_one():
    import pro
    for raw in (b"\x00", b"\x00\x01", bytes(range(32)), b"\xff" * 8, b""):
        assert solana_tx.b58decode(solana_tx.b58encode(raw)) == raw
        assert solana_tx.b58encode(raw) == pro._b58encode(raw)