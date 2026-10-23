"""Phantom deep-link requests for one Pro payment: connect, then sign.

Why this exists (2026-09-24): a payer reported landing in Coinbase Wallet with an empty
send screen. The cause is structural — "solana:" is claimed by EVERY Solana wallet, so
the operating system picks the handler — and the in-browser route we already have
depends on Phantom injecting a provider into our page. Phantom's deep-link API removes
both dependencies: `phantom://v1/<method>` is answerable by Phantom alone, and the
request carries an ALREADY-BUILT transaction, so the wallet composes nothing and no
scheme is resolved by anything else.

The protocol, from Phantom's deeplink docs (Phantom deep links / Encryption /
Specifying redirects):
  * connect  — the dapp sends `dapp_encryption_public_key` (a FRESH x25519 keypair per
    session, which Phantom recommends) and `redirect_link`. Phantom answers on the
    redirect with `phantom_encryption_public_key`, `nonce` and `data`, where `data` is
    ENCRYPTED with the Diffie-Hellman shared secret and decrypts to
    {"public_key": ..., "session": ...}: the payer's key and the session token.
  * later methods (signTransaction here) — the dapp sends the same public key, a fresh
    `nonce` and an encrypted `payload` containing {"transaction": <base58 unsigned
    transaction>, "session": ...}; Phantom encrypts its answer into `data` again.
    A refusal comes back as `errorCode`/`errorMessage` instead of `data`.
The cipher is NaCl's box — X25519 + XSalsa20-Poly1305 — which is what Phantom's own
reference implementations use (TweetNaCl on every platform). PyNaCl is the single
dependency this feature adds, and it is the only way to get XSalsa20-Poly1305 here:
`cryptography`, the project's existing crypto dependency, offers ChaCha20Poly1305 only.

Two deliberate choices:
  * `signTransaction`, not the deprecated `signAndSendTransaction`: Phantom hands back
    the signed transaction and we submit it through the RPC this bot already talks to,
    so the wallet stays out of the submission path and the signature comes to us.
  * the custom scheme first, the universal link returned as the fallback: the scheme is
    wallet-scoped (the whole point), while the universal link is what Phantom's docs
    recommend — and what an environment that ignores custom schemes needs.

No payer key ever reaches this module: the transaction we send is unsigned, and the
session is a bearer token between us and Phantom for one invoice.
"""
import base64
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import quote

from nacl.public import Box, PrivateKey, PublicKey

import solana_tx

logger = logging.getLogger(__name__)

PHANTOM_SCHEME = "phantom://v1"
PHANTOM_UNIVERSAL = "https://phantom.app/ul/v1"
CLUSTER = "mainnet-beta"

# One session per invoice, held in memory only. It contains the x25519 private key the
# session's encryption is bound to, so it must never be written anywhere: a restart
# costing the payer a second connect is the safe failure, a leaked key is not.
_SESSION_TTL = 1800
_SESSIONS: Dict[str, Dict[str, Any]] = {}


def _tag(payment_id: str) -> str:
    """An opaque correlator for logs: never the id itself (it is a capability)."""
    return hashlib.sha256(payment_id.encode("utf-8")).hexdigest()[:6]


def _decode_any(text: str) -> bytes:
    """Base58 first, base64 as a fallback — both appear across Phantom's docs.

    Tolerant on purpose: the deeplink pages show base58, some older universal-link
    pages show base64, and a wrong guess is indistinguishable from a wrong key at the
    point of use. Only a value that is neither raises.
    """
    try:
        decoded = solana_tx.b58decode(text)
        if decoded:
            return decoded
    except ValueError:
        pass
    try:
        return base64.b64decode(text, validate=True)
    except Exception:
        raise ValueError("value is neither base58 nor base64")


def _box(private: PrivateKey, phantom_public: str) -> Box:
    key_bytes = _decode_any(phantom_public)
    if len(key_bytes) != 32:
        raise ValueError("phantom_encryption_public_key must be 32 bytes")
    return Box(private, PublicKey(key_bytes))


def start_session(payment_id: str) -> Optional[Dict[str, Any]]:
    """Begin a session for one invoice: a fresh keypair, remembered until it expires.

    Returns None when a CONNECTED session already exists for this invoice. The
    payment_id is a public capability, so without this refusal anyone holding
    the invoice URL could replace a payer's mid-flight session out from under
    them (2026-10-04 audit). An UNCONNECTED session is still replaced: that is
    the recovery path for a connect that never completed."""
    _sweep(pending_insert=True)
    existing = _SESSIONS.get(payment_id)
    if existing and existing.get("session"):
        logger.info("Phantom connect refused for %s: session already connected",
                    _tag(payment_id))
        return None
    private = PrivateKey.generate()
    record = {
        "private": private,
        "dapp_public": solana_tx.b58encode(bytes(private.public_key)),
        "created": time.time(),
        # Filled in by complete_connect once Phantom answers.
        "phantom_public": "", "session": "", "payer": "",
    }
    _SESSIONS[payment_id] = record
    logger.info("Phantom session started for %s", _tag(payment_id))
    return record


def get_session(payment_id: str) -> Optional[Dict[str, Any]]:
    _sweep()
    return _SESSIONS.get(payment_id)


def forget_session(payment_id: str) -> None:
    _SESSIONS.pop(payment_id, None)


def complete_connect(payment_id: str, phantom_public: str, nonce: str,
                     data: str) -> Dict[str, Any]:
    """Decrypt the connect response into the session (payer key + session token).

    The encryption page is explicit that this `data` is encrypted with the shared
    secret derived from `phantom_encryption_public_key`, so a response we cannot
    decrypt means those values belong to a different session — never a reason to guess.
    """
    record = _SESSIONS.get(payment_id)
    if record is None:
        raise ValueError("no session for this invoice (it expired, or this device "
                         "never started one)")
    opened = decrypt(_box(record["private"], phantom_public), nonce, data)
    payer = str(opened.get("public_key") or "")
    session = str(opened.get("session") or "")
    if not payer or not session:
        raise ValueError("the connect response carried no public key or session")
    record["phantom_public"] = phantom_public
    record["session"] = session
    record["payer"] = payer
    logger.info("Phantom session connected for %s", _tag(payment_id))
    return record


def encrypt(box: Box, payload: Mapping[str, Any]) -> Tuple[str, str]:
    """(nonce, payload) for one request: base58, with a fresh 24-byte nonce."""
    nonce = secrets.token_bytes(24)
    sealed = box.encrypt(json.dumps(dict(payload)).encode("utf-8"), nonce)
    return solana_tx.b58encode(nonce), solana_tx.b58encode(bytes(sealed.ciphertext))


def decrypt(box: Box, nonce: str, data: str) -> Dict[str, Any]:
    """Phantom's encrypted answer as a dict. Raises ValueError on anything unusable."""
    try:
        opened = box.decrypt(_decode_any(data), _decode_any(nonce))
    except Exception as e:                      # CryptoError, malformed encodings
        raise ValueError(f"could not decrypt Phantom's response: {e}")
    try:
        parsed = json.loads(opened.decode("utf-8"))
    except Exception:
        raise ValueError("Phantom's response is not JSON")
    if not isinstance(parsed, dict):
        raise ValueError("Phantom's response is not an object")
    return parsed


def _query(**params: str) -> str:
    """Every parameter URL-encoded, which the redirect_link spec requires."""
    return "&".join(f"{key}={quote(value, safe='')}" for key, value in params.items()
                    if value)


def connect_links(dapp_public: str, redirect: str, app_url: str) -> Dict[str, str]:
    """The connect request both ways: wallet-scoped scheme, then Phantom's URL."""
    query = _query(app_url=app_url, dapp_encryption_public_key=dapp_public,
                   redirect_link=redirect, cluster=CLUSTER)
    return {"scheme": f"{PHANTOM_SCHEME}/connect?{query}",
            "universal": f"{PHANTOM_UNIVERSAL}/connect?{query}"}


def sign_transaction_links(record: Mapping[str, Any], transaction_b58: str,
                           redirect: str) -> Dict[str, str]:
    """The signTransaction request for an already-connected session."""
    box = _box(record["private"], str(record.get("phantom_public") or ""))
    nonce, payload = encrypt(box, {"transaction": transaction_b58,
                                   "session": record.get("session") or ""})
    query = _query(dapp_encryption_public_key=str(record.get("dapp_public") or ""),
                   nonce=nonce, payload=payload, redirect_link=redirect)
    return {"scheme": f"{PHANTOM_SCHEME}/signTransaction?{query}",
            "universal": f"{PHANTOM_UNIVERSAL}/signTransaction?{query}"}


def decode_sign_response(record: Mapping[str, Any], nonce: str, data: str) -> str:
    """The signed transaction Phantom answers a sign request with (base58).

    The response is encrypted with the same shared secret as the request, so decoding
    it is also the check that the answer belongs to THIS session's conversation.
    """
    opened = decrypt(_box(record["private"], str(record.get("phantom_public") or "")),
                     nonce, data)
    signed = str(opened.get("transaction") or "")
    if not signed:
        raise ValueError("Phantom's answer carried no signed transaction")
    return signed


def _sweep(pending_insert: bool = False) -> None:
    """Drop expired sessions, and bound the table outright if it ever grew anyway."""
    now = time.time()
    for payment_id in [p for p, record in _SESSIONS.items()
                       if now - record.get("created", 0) > _SESSION_TTL]:
        _SESSIONS.pop(payment_id, None)
    # +1 headroom when a fresh session is about to be inserted, so the table
    # never ends a start_session above the cap (2026-10-04 audit).
    incoming = 1 if pending_insert else 0
    if len(_SESSIONS) + incoming > 256:         # invoices are not this plentiful
        # Evict the OLDEST rather than clearing the table: a burst must not
        # drop a session its payer is standing in mid-payment.
        overflow = len(_SESSIONS) + incoming - 256
        for stale in sorted(_SESSIONS,
                            key=lambda p: _SESSIONS[p].get("created", 0))[:overflow]:
            _SESSIONS.pop(stale, None)
