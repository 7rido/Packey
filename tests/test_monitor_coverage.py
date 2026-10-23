"""The monitor must see payments where they actually land: the token accounts.

The 2026-09-24 miss: a 0.013828 USDT payment sat in our token account while the monitor
read getSignaturesForAddress(our wallet) — and a Solana Pay transfer does not include the
wallet as a party at all (it is only the OWNER of the destination token account), so the
signature was never in that list. Only two credits exist in the whole receipt trail,
which is what a blind spot like that looks like from outside.

These tests use the realistic shape (payer signs, money lands in the token account, our
wallet merely owns it) rather than the wallet-as-account shape the older fixtures used,
and they drive the real monitor loop, not a stand-in for it.
"""
import asyncio

import pytest

import config
import pro

WALLET = "OurWallet111111111111111111111111111111111"
ATA_USDC = "OurUsdcAta11111111111111111111111111111111"
ATA_USDT = "OurUsdtAta11111111111111111111111111111111"
PAYER = "PayerWallet1111111111111111111111111111111"
SIG = "5PaymentSig1111111111111111111111111111111111111111111111111111111"
CHAT = 9001
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _payment_tx(ata, owner=WALLET, sent=0.013828, mint=None, memo=None):
    """A jsonParsed Solana Pay transfer into one of OUR token accounts.

    `owner` is the wallet the caller watches — the shared wallet for the QR route, the
    chat's own address for a deposit — because that is what `_tx_payment_info` matches on.
    """
    mint = mint or config.USDT_MINT
    keys = [{"pubkey": PAYER, "signer": True, "writable": True},
            {"pubkey": ata, "writable": True},
            {"pubkey": mint, "writable": False},
            {"pubkey": TOKEN_PROGRAM, "writable": False}]
    tx = {"transaction": {"message": {"accountKeys": keys, "instructions": []}},
          "meta": {
              "preTokenBalances": [{"accountIndex": 1, "mint": mint, "owner": owner,
                                    "uiTokenAmount": {"uiAmount": 0.0}}],
              "postTokenBalances": [{"accountIndex": 1, "mint": mint, "owner": owner,
                                     "uiTokenAmount": {"uiAmount": sent}}],
          }}
    if memo:
        tx["transaction"]["message"]["instructions"] = [
            {"parsed": {"type": "memo", "info": {"memo": memo}}}]
    return tx


def _rpc(monkeypatch, pages, tx=None, atas=None):
    """A Solana RPC stub whose signature pages are per ACCOUNT, not per call.

    That is the whole point: the wallet's page can be empty while the token account's
    page holds the payment — the exact situation the monitor used to miss.
    """
    calls = []
    token_accounts = dict(atas or {})
    token_accounts.setdefault(WALLET, {config.USDC_MINT: ATA_USDC,
                                       config.USDT_MINT: ATA_USDT})

    async def fake_rpc(method, params):
        calls.append((method, params[0] if params else None))
        if method == "getSignaturesForAddress":
            account = params[0]
            if "before" in (params[1] or {}):
                return []
            return pages.get(account, [])
        if method == "getTokenAccountsByOwner":
            owner, query = params[0], params[1]
            mint = (query or {}).get("mint")
            ata = (token_accounts.get(owner) or {}).get(mint)
            if not ata:
                return {"value": []}
            return {"value": [{"pubkey": ata, "account": {"data": {"parsed": {
                "info": {"tokenAmount": {"decimals": 6, "uiAmount": 1.0}}}}}}]}
        if method == "getTransaction":
            return tx
        raise AssertionError(f"unexpected RPC call {method}")

    monkeypatch.setattr(pro, "_sol_rpc", fake_rpc)
    return calls


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    saved_pending = dict(pro._pending_payments)
    saved_expired = dict(pro._expired_payments)
    saved_deposits = dict(pro._deposit_addrs)
    saved_users = dict(pro._pro_users)
    saved_used = dict(pro._used_amounts)
    monkeypatch.setattr(config, "BOT_USDC_ADDRESS", WALLET)
    monkeypatch.setattr(config, "SOL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(pro, "deposit_seed_bytes", lambda: None)
    pro._pending_payments.clear()
    pro._expired_payments.clear()
    pro._deposit_addrs.clear()
    pro._our_token_accounts.clear()
    # The amount a credited payment consumes is global state: without restoring it, the
    # next test's invoice can no longer match the same amount and fails for the wrong
    # reason (found by running one of these tests alone and watching it pass).
    pro._used_amounts.clear()
    yield
    pro._pending_payments.clear()
    pro._pending_payments.update(saved_pending)
    pro._expired_payments.clear()
    pro._expired_payments.update(saved_expired)
    pro._deposit_addrs.clear()
    pro._deposit_addrs.update(saved_deposits)
    pro._pro_users.clear()
    pro._pro_users.update(saved_users)
    pro._used_amounts.clear()
    pro._used_amounts.update(saved_used)
    pro._our_token_accounts.clear()


def _open_invoice(micros=13828, created=None):
    """One open invoice whose amount identifies this payment."""
    import time as _time
    pro._pending_payments["inv-0123456789abcdef"] = {
        "chat_id": CHAT, "amount": micros / 1_000_000, "amount_micros": micros,
        "memo": "pkabc123", "reference": pro.new_reference(), "deposit_address": None,
        "base_micros": 10000, "created": created if created is not None else _time.time(),
    }
    return "inv-0123456789abcdef"


def _signature_entry(signature=SIG):
    import time as _time
    return [{"signature": signature, "err": None, "blockTime": _time.time()}]


def _recorder(sink):
    """Stand in for _activate_pro: record (chat, mint, how) instead of notifying."""
    async def activate(chat_id, amount, mint, sig, payment_id="", matched_by="amount",
                       renewed=False):
        sink.append((chat_id, mint, matched_by))
    return activate


def _run_monitor(seconds: float = 0.08):
    """Drive the REAL monitor loop briefly, then cancel it.

    The loop is `while True` around a poll sleep, so it is exercised as a task: what the
    assertions see is what the production loop did, not a reimplementation of it. Its
    `except Exception` handler does not catch CancelledError (a BaseException in 3.8+),
    so cancelling it really stops it.
    """
    async def drive():
        task = asyncio.ensure_future(pro.monitor_solana_payments())
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except BaseException:            # CancelledError, and anything it left behind
            pass
    asyncio.run(drive())


# ── the watched accounts ────────────────────────────────────────────────────

def test_the_wallet_and_its_token_accounts_are_all_watched(monkeypatch):
    """The wallet alone is not enough, and that is the bug this file exists for."""
    _rpc(monkeypatch, {})
    assert asyncio.run(pro._watched_accounts(WALLET)) == [WALLET, ATA_USDC, ATA_USDT]


def test_signatures_from_every_account_are_merged_and_deduplicated(monkeypatch):
    """One signature can appear under two watched accounts (a transaction touching both
    a wallet and its token account). The caller credits per signature, so a duplicate
    here would be a duplicate credit."""
    _rpc(monkeypatch, {WALLET: _signature_entry("5Shared111111111111111111111111111"
                                               "1111111111111111111111111111111"),
                       ATA_USDC: _signature_entry("5Shared111111111111111111111111111"
                                                  "1111111111111111111111111111111")
                       + _signature_entry(SIG)})
    merged = asyncio.run(pro._signatures_for_watched(WALLET, set()))
    signatures = [str(entry["signature"]) for entry in merged]
    assert signatures.count(SIG) == 1
    assert len(signatures) == 2, f"one duplicate slipped through: {signatures}"


# ── the regression: a payment the wallet's own page never shows ─────────────

def test_a_payment_visible_only_in_the_token_account_is_credited(monkeypatch):
    """The exact 2026-09-24 case: nothing in the wallet's signature list, the payment in
    the token account's. Before the fix this credited nothing at all."""
    _open_invoice()
    _rpc(monkeypatch, {WALLET: [], ATA_USDT: _signature_entry()},
         tx=_payment_tx(ATA_USDT))
    credited = []
    monkeypatch.setattr(pro, "_activate_pro", _recorder(credited))

    _run_monitor()

    assert credited == [(CHAT, config.USDT_MINT, "amount")], (
        "the payment must be credited by the amount it carried")


def test_a_signature_in_both_pages_is_credited_exactly_once(monkeypatch):
    _open_invoice()
    _rpc(monkeypatch, {WALLET: _signature_entry(), ATA_USDT: _signature_entry()},
         tx=_payment_tx(ATA_USDT))
    credited = []
    monkeypatch.setattr(pro, "_activate_pro", _recorder(credited))

    _run_monitor()

    assert len(credited) == 1, f"credited {len(credited)} times"


def test_a_signature_already_in_the_receipt_trail_is_never_credited_again(monkeypatch):
    """The durable guard: the trail survives restarts, so re-scanning history after one
    cannot pay a buyer twice."""
    _open_invoice()
    _rpc(monkeypatch, {WALLET: [], ATA_USDT: _signature_entry()},
         tx=_payment_tx(ATA_USDT))
    monkeypatch.setattr(pro, "_credited_signatures", lambda: {SIG})
    credited = []
    monkeypatch.setattr(pro, "_activate_pro", _recorder(credited))

    _run_monitor()

    assert credited == [], "a credited signature was credited again"


# ── the dedup window: "recent" has to mean recent ───────────────────────────

def test_the_dedup_window_keeps_the_newest_signatures():
    """The monitor's dedup table was a plain set, and it was trimmed with
    list(known)[-300:]. A set has no order, so that kept an ARBITRARY subset —
    sig000 sat happily alongside sig599 — and the window did not mean "recent"
    at all. The cost was re-fetching transactions the public RPC then
    rate-limits. Now the oldest are dropped and the newest genuinely survive.
    """
    sigs = pro._OrderedSigs(f"sig{i:04d}" for i in range(500))
    sigs.trim(300)
    assert len(sigs) == 300
    assert "sig0499" in sigs, "the newest signature must survive the trim"
    assert "sig0000" not in sigs, "the oldest must be the one dropped"


def test_the_dedup_window_still_behaves_like_a_set():
    """Only .add(), `in` and len() are ever performed on it — and the tests
    themselves pass a plain set() into the functions that share it, so the
    ordered type must not narrow that contract."""
    sigs = pro._OrderedSigs()
    assert len(sigs) == 0
    sigs.add("a")
    sigs.add("a")                       # a set: adding twice is still one entry
    assert len(sigs) == 1 and "a" in sigs
    assert "b" not in sigs
    sigs.add("b")
    assert len(sigs) == 2


def test_trimming_the_window_twice_is_a_no_op():
    """The monitor trims on every cycle past the trigger; a second trim with no
    new signatures must not remove anything else."""
    sigs = pro._OrderedSigs(f"sig{i}" for i in range(400))
    sigs.trim(300)
    sigs.trim(300)
    assert len(sigs) == 300 and "sig399" in sigs
    sigs.add("sig-new")                 # and adding after a trim still works
    assert len(sigs) == 301 and "sig-new" in sigs


def test_seeding_orders_the_receipt_trail_oldest_first(monkeypatch):
    """_credited_signatures() hands back a SET, so seeding from it directly
    would preserve hash order — reproducing the very bug above. The receipts are
    consulted only to SORT, and the seam itself is still the function that
    produces the set (the durable exactly-once guard, which tests substitute).
    """
    trail = [{"sig": "sig-mid", "ts": 200.0},
             {"sig": "sig-new", "ts": 300.0},
             {"sig": "sig-old", "ts": 100.0}]
    monkeypatch.setattr(pro, "_receipts", trail)
    monkeypatch.setattr(pro, "_credited_signatures", lambda: {"sig-old", "sig-mid", "sig-new"})
    seeded = pro._seed_known_sigs()
    assert list(seeded._q) == ["sig-old", "sig-mid", "sig-new"], (
        "the seeded table must be chronological, or the first trim is arbitrary")
    seeded.trim(2)
    assert "sig-old" not in seeded and "sig-new" in seeded


def test_seeding_honours_a_substituted_credit_trail(monkeypatch):
    """A receipt trail that reports nothing must seed an EMPTY table, and one
    that reports a signature must seed it — i.e. the seed asks the guard rather
    than reading _receipts behind its back.
    """
    monkeypatch.setattr(pro, "_receipts", [])
    monkeypatch.setattr(pro, "_credited_signatures", lambda: {SIG})
    assert SIG in pro._seed_known_sigs()
    monkeypatch.setattr(pro, "_credited_signatures", lambda: set())
    assert len(pro._seed_known_sigs()) == 0


# ── the deposit-address path had the same flaw ──────────────────────────────

def test_a_deposit_payment_visible_only_in_its_token_account_is_credited(monkeypatch):
    """An exchange transfer lands in the deposit address's token account too, so the
    balance watch saw the money and the signature scan found nothing to credit."""
    deposit_wallet = "DepositWallet111111111111111111111111111111"
    deposit_ata = "DepositAta111111111111111111111111111111"
    _rpc(monkeypatch, {deposit_wallet: [], deposit_ata: _signature_entry()},
         tx=_payment_tx(deposit_ata, owner=deposit_wallet,
                        sent=config.PRO_PRICE_USD),
         atas={deposit_wallet: {config.USDT_MINT: deposit_ata}})
    credited = []
    monkeypatch.setattr(pro, "_activate_pro", _recorder(credited))

    count = asyncio.run(pro._scan_deposit_address(deposit_wallet, CHAT, set()))

    assert count == 1 and credited == [(CHAT, config.USDT_MINT, "deposit")]