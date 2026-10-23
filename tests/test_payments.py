"""Unique-amount payments: the amount is the payer's identifier.

The old scheme matched "within ±1 cent, but only while a single invoice is
pending", so two buyers at once were indistinguishable and a memo (which many
wallets never send) was the only reliable path. These tests pin the replacement:
allocate a unique amount, match it exactly in micro-USDC, honour late payments,
and always credit the right chat. Since then the reference (a Solana Pay wallet
attaches it) and the per-chat deposit address (the manual/exchange path) have
become the primary identifiers — the amount stays, for invoices written before
them and for the dedup it still keeps honest.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

import config
import miniapp
import pro
import storage


@pytest.fixture(autouse=True)
def _pin_price(monkeypatch):
    """These tests exercise the payment mechanics, which are price-agnostic.

    The operator's real price lives in .env (`PRO_PRICE_USD`, currently $1.00) and is
    read by the code, but a test must not change MEANING when the price changes: a
    deposit-transfer fixture of 0.017777 is "in band" only at a 0.01 base. So the suite
    pins a known price and the band tests read `config.PRO_PRICE_USD` rather than a
    literal. A test that deliberately changes the price still wins, because it applies
    its own monkeypatch after this fixture.
    """
    monkeypatch.setattr(config, "PRO_PRICE_USD", 0.01)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return type("M", (), {"message_id": 1})()


def _clear_state():
    pro._pending_payments.clear()
    pro._expired_payments.clear()
    pro._used_amounts.clear()


def _invoice(chat_id, created=None):
    """Allocate a real invoice the way the endpoint does and store it."""
    micros, amount = pro.allocate_amount(pro.open_amounts())
    pid = f"{chat_id}-{int(time.time() * 1000) % 10**6}"
    pro._pending_payments[pid] = {
        "chat_id": chat_id, "amount": amount, "amount_micros": micros,
        "memo": pro.new_memo_token(),
        "created": created if created is not None else time.time(),
    }
    return pid, micros


def _load_reconcile_module():
    """Import scripts/payment_reconcile.py by path — scripts/ is not a package."""
    import importlib.util
    path = (Path(__file__).resolve().parent.parent
            / "scripts" / "payment_reconcile.py")
    spec = importlib.util.spec_from_file_location("payment_reconcile", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAmountAllocation:
    def test_amount_is_base_price_plus_a_small_tail(self):
        _clear_state()
        micros, amount = pro.allocate_amount(set())
        base = int(round(config.PRO_PRICE_USD * 1_000_000))
        assert base < micros <= base + 9999, "tail outside the honest range"
        assert abs(amount - micros / 1_000_000) < 1e-9

    def test_two_invoices_never_share_an_amount(self):
        _clear_state()
        seen = set()
        try:
            for i in range(50):
                micros, _amt = pro.allocate_amount(pro.open_amounts())
                assert micros not in seen, "a live invoice already claimed this amount"
                seen.add(micros)
                pro._pending_payments[f"pid{i}"] = {
                    "chat_id": i, "amount_micros": micros, "amount": micros / 1e6,
                    "created": time.time()}
                assert micros in pro.open_amounts()
        finally:
            _clear_state()

    def test_open_amounts_include_grace_and_recently_used(self):
        _clear_state()
        try:
            pro._expired_payments["p1"] = {"chat_id": 1, "amount_micros": 11001,
                                           "created": time.time(),
                                           "expired_at": time.time()}
            pro._used_amounts[12002] = time.time()
            assert {11001, 12002} <= pro.open_amounts()
            # ...but an amount consumed long ago is free again.
            pro._used_amounts[13003] = time.time() - config.PAYMENT_GRACE_SECONDS - 60
            assert 13003 not in pro.open_amounts()
        finally:
            _clear_state()


class TestExactMatching:
    def test_exact_micro_match_identifies_the_chat(self):
        """Two buyers at once: each amount maps to exactly one chat."""
        _clear_state()
        try:
            _pid_a, micros_a = _invoice(7001)
            _pid_b, micros_b = _invoice(7002)
            assert micros_a != micros_b
            found = pro._match_payment(micros_a)
            assert found is not None and found[1]["chat_id"] == 7001
            found = pro._match_payment(micros_b)
            assert found is not None and found[1]["chat_id"] == 7002
        finally:
            _clear_state()

    def test_off_by_one_micro_does_not_match(self):
        """No tolerance any more: a different amount is not your payment."""
        _clear_state()
        try:
            _pid, micros = _invoice(7003)
            assert pro._match_payment(micros + 1) is None
            assert pro._match_payment(micros - 1) is None
        finally:
            _clear_state()

    def test_memo_is_a_fast_path_but_must_agree_on_amount(self):
        _clear_state()
        try:
            pid, micros = _invoice(7004)
            _other_pid, other_micros = _invoice(7005)
            my_memo = pro._pending_payments[pid]["memo"]
            found = pro._match_payment(micros, memo=my_memo)
            assert found and found[0] == pid
            # A memo naming an invoice that expects a *different* amount falls
            # back to the amount lookup instead of crediting the wrong chat.
            found = pro._match_payment(other_micros, memo=my_memo)
            assert found and found[1]["chat_id"] == 7005
            # A stale/unknown memo must not be trusted either.
            assert pro._match_payment(other_micros, memo="pkdeadbeefdead") is not None
        finally:
            _clear_state()

    def test_memos_are_unique_and_carry_no_chat_id(self):
        """Solana memos are public forever — they must not identify the payer."""
        _clear_state()
        try:
            tokens = {pro.new_memo_token() for _ in range(20)}
            assert len(tokens) == 20
            pid, micros = _invoice(7010)
            memo = pro._pending_payments[pid]["memo"]
            url = f"solana:WALLET?amount={micros / 1e6:.6f}&memo={memo}"
            assert "7010" not in url, "memo/URL must not leak the chat ID"
            assert memo in url
        finally:
            _clear_state()

    def test_late_payment_within_grace_is_honoured(self):
        """An expired invoice keeps its amount matchable, so paying late still
        activates instead of becoming stray money."""
        _clear_state()
        try:
            pid, micros = _invoice(7006,
                                   created=time.time() - config.PAYMENT_EXPIRY_SECONDS - 120)
            now = time.time()
            for p in [p for p, v in pro._pending_payments.items()
                      if now - v["created"] > config.PAYMENT_EXPIRY_SECONDS]:
                invoice = pro._pending_payments.pop(p)      # what the monitor does
                invoice["expired_at"] = now
                pro._expired_payments[p] = invoice
            assert pid in pro._expired_payments
            found = pro._match_payment(micros)
            assert found and found[1]["chat_id"] == 7006
        finally:
            _clear_state()

    def test_superseded_invoice_still_credits_its_own_chat(self):
        """Reopening the Pro sheet must not strand money: the invoice the user
        was first shown stays matchable for the grace window."""
        _clear_state()
        try:
            old_pid, old_micros = _invoice(7008)
            pro.park_invoice(old_pid, pro._pending_payments.pop(old_pid))
            assert old_pid in pro._expired_payments
            assert old_micros in pro.open_amounts()      # not reusable by others
            found = pro._match_payment(old_micros)
            assert found and found[1]["chat_id"] == 7008
            # A different chat's transfer of that amount still matches, because
            # the amount was never handed out twice.
            _new_pid, new_micros = _invoice(7009)
            assert new_micros != old_micros
        finally:
            _clear_state()

    def test_used_amount_cannot_credit_a_second_time(self):
        _clear_state()
        try:
            pid, micros = _invoice(7007)
            assert pro._match_payment(micros)
            pro._pending_payments.pop(pid, None)
            pro._used_amounts[micros] = time.time()     # consumed
            assert pro._match_payment(micros) is None   # duplicate transfer
            assert micros in pro.open_amounts()         # and not re-issued yet
        finally:
            _clear_state()

    def test_exact_match_rejects_an_ancient_transfer(self):
        """An old transfer must not pay an invoice that shares its amount.

        The grace pool keeps an expired invoice matchable for a whole day, so an
        exact amount can outlive its window: without the transfer's own block
        time, a transfer from before the invoice existed could pay it.
        """
        _clear_state()
        try:
            pid, micros = _invoice(7110)
            invoice = pro._pending_payments[pid]
            created = invoice["created"]
            window = config.PAYMENT_EXPIRY_SECONDS + config.PAYMENT_GRACE_SECONDS

            # A late-but-plausible payment still matches (created + 20 h).
            assert pro._match_payment(micros, tx_time=created + 20 * 3600)
            # Older than the invoice could have been: refused.
            assert pro._match_payment(micros, tx_time=created - 400) is None
            # Long past the grace window: refused too.
            assert pro._match_payment(micros,
                                      tx_time=created + window + 400) is None
            # The memo fast path is bounded the same way.
            memo = invoice["memo"]
            assert pro._match_payment(micros, memo, tx_time=created + 60)
            assert pro._match_payment(micros, memo, tx_time=created - 400) is None
            # Callers that pass no time keep the unbounded scan they always had.
            assert pro._match_payment(micros) is not None
        finally:
            _clear_state()

    def test_exact_match_rejects_a_used_amount(self):
        """Belt and braces on top of the monitor's match-then-consume order."""
        _clear_state()
        try:
            pid, micros = _invoice(7111)
            invoice = pro._pending_payments[pid]
            assert pro._match_payment(micros), "precondition: it matches"

            # The monitor consumes an amount only AFTER a successful match, so
            # the amount has to still be matchable at match time...
            found = pro._match_payment(micros)
            pro.retire_amount(found[1], micros)
            # ...and must not match again once it is consumed, even though the
            # invoice itself is still sitting in the live pool.
            assert micros in pro._used_amounts
            assert pid in pro._pending_payments, "the invoice is still live"
            assert pro._match_payment(micros) is None
            assert pro._match_payment(micros, memo=invoice["memo"]) is None
        finally:
            _clear_state()


class TestActivationMessage:
    """After an exact match the payer gets: welcome + confirmation + receipt."""

    def _activate(self, monkeypatch, tmp_path, chat_id, amount, pid="pid-1"):
        import botstate
        bot = _Bot()
        monkeypatch.setattr(botstate, "bot", bot)
        monkeypatch.chdir(tmp_path)          # pro_receipts.log lands in tmp
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS",
                            "BOTwallet1111111111111111111111111111111111")
        asyncio.run(pro._activate_pro(
            chat_id, amount, config.USDC_MINT,
            "5sIgNatUrEsIgNatUrEsIgNatUrEsIgNatUrEsIgNatUrE", pid))
        return bot.sent

    def test_welcome_receipt_and_identifying_digits(self, monkeypatch, tmp_path):
        pro._pro_users.pop(8001, None)
        try:
            sent = self._activate(monkeypatch, tmp_path, 8001, 0.012345)
            assert sent, "no activation message sent"
            _chat, text = sent[0]
            assert "Welcome to Pro!" in text
            assert "0.012345" in text, "the identifying digits must survive to the receipt"
            assert "Receipt ID:" in text and "pid-1" in text
            assert "Expires:" in text and "International tab" in text
        finally:
            pro._pro_users.pop(8001, None)

    def test_renewal_says_welcome_back_and_stacks(self, monkeypatch, tmp_path):
        pro._pro_users[8002] = time.time() + 3600
        try:
            sent = self._activate(monkeypatch, tmp_path, 8002, 0.012346)
            assert "Welcome back to Pro!" in sent[0][1]
            assert pro._pro_users[8002] > time.time() + 3600     # extended, not reset
        finally:
            pro._pro_users.pop(8002, None)

    def test_receipt_trail_keeps_six_decimals_and_stays_encrypted(self, monkeypatch,
                                                                 tmp_path):
        pro._pro_users.pop(8003, None)
        try:
            self._activate(monkeypatch, tmp_path, 8003, 0.012347)
            raw = config.PRO_RECEIPTS_FILE.read_bytes()
            assert raw.startswith(b"gAAAA"), "receipts must be encrypted at rest"
            assert b"8003" not in raw, "the paying chat must not appear in plaintext"
            entries = json.loads(
                storage._read_protected(config.PRO_RECEIPTS_FILE)[0])
            amounts = [f"{e['amount']:.6f}" for e in entries]
            assert "0.012347" in amounts, "cents would lose the identifier"
        finally:
            pro._pro_users.pop(8003, None)


class _Stop(BaseException):
    """Escapes the expiry loop: its own `except Exception` must not swallow it,
    so one asyncio.run() drives exactly the number of passes the test asks for."""


def _drive_expiry(monkeypatch, ticks, now=None):
    """Run pro._pro_expiry_task for exactly `ticks` passes, on a fixed clock, with
    a recording bot and no state writes. Returns (sent messages, the clock dict —
    a test advances expiry-relative time by writing clock-relative values itself)."""
    clock = {"now": now if now is not None else time.time()}
    monkeypatch.setattr(pro.time, "time", lambda: clock["now"])
    sent = []

    class _Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    monkeypatch.setattr(pro.botstate, "bot", _Bot())
    monkeypatch.setattr(pro, "save_pro_users", lambda: None)
    monkeypatch.setattr(pro, "save_expiry_warnings", lambda: None)
    passes = {"n": 0}

    async def fake_sleep(_secs):
        passes["n"] += 1
        if passes["n"] >= ticks:      # the Nth sleep ENDS the Nth pass
            raise _Stop

    monkeypatch.setattr(pro.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(pro._pro_expiry_task())
    except _Stop:
        pass
    assert passes["n"] == ticks, f"ran {passes['n']} passes, asked for {ticks}"
    return sent, clock


class TestProExpiryWarning:
    """The last-day reminder: once, on the last day.

    The old gate warned at three days out — and re-warned on every process run
    within that window, because the warned set is in-memory. The operator asked
    for once, on the last day; these tests pin the quiet days as hard as the one
    that speaks.
    """

    CHAT = 4242

    def _isolated(self):
        """Empty the store the loop reads, and remember what was there."""
        saved_users, saved_warned = dict(pro._pro_users), dict(pro._expiry_warned)
        pro._pro_users.clear()
        pro._expiry_warned.clear()
        return saved_users, saved_warned

    def _restore(self, saved_users, saved_warned):
        pro._pro_users.clear()
        pro._pro_users.update(saved_users)
        pro._expiry_warned.clear()
        pro._expiry_warned.update(saved_warned)

    def test_warns_once_on_the_last_day(self, monkeypatch):
        saved = self._isolated()
        try:
            now = time.time()
            pro._pro_users[self.CHAT] = now + 20 * 3600       # inside the last day
            sent, _clock = _drive_expiry(monkeypatch, ticks=2)
            warns = [t for _c, t in sent if "expires today" in t]
            assert len(warns) == 1, f"exactly one warning expected, got {warns}"
        finally:
            self._restore(*saved)

    def test_silent_three_days_out(self, monkeypatch):
        """The behavior change: 2.5 days remaining used to warn immediately."""
        saved = self._isolated()
        try:
            pro._pro_users[self.CHAT] = time.time() + 60 * 3600
            sent, _clock = _drive_expiry(monkeypatch, ticks=1)
            assert sent == [], f"no nag before the last day, got {sent}"
        finally:
            self._restore(*saved)

    def test_expired_still_demotes_and_notices(self, monkeypatch):
        """Positive control: the demote path is untouched by the warning change."""
        saved = self._isolated()
        try:
            pro._pro_users[self.CHAT] = time.time() - 60      # already over
            sent, _clock = _drive_expiry(monkeypatch, ticks=1)
            assert self.CHAT not in pro._pro_users, "expired Pro must be demoted"
            assert any(c == self.CHAT and "expired" in t for c, t in sent), sent
        finally:
            self._restore(*saved)

    def test_renewal_rearms_the_warning_for_the_next_period(self, monkeypatch):
        """A renewal must clear the warned flag, or the NEXT subscription's last
        day would pass in silence — the opposite failure of nagging."""
        saved = self._isolated()
        try:
            now = time.time()
            pro._pro_users[self.CHAT] = now + 20 * 3600
            sent, _clock = _drive_expiry(monkeypatch, ticks=1)
            assert any("expires today" in t for _c, t in sent)
            # Renew through the real activation path, with only the persistence
            # stubbed out so the receipt trail stays untouched.
            monkeypatch.setattr(pro, "append_receipt", lambda entry: None)
            asyncio.run(pro._activate_pro(
                self.CHAT, 0.012345, config.USDC_MINT,
                "5RenewalSignatureRenewalSignatureRenewalSignature"))
            assert self.CHAT not in pro._expiry_warned, \
                "a renewal must re-arm the warning"
            # Advance to the NEW subscription's last day and expect the warning.
            new_expiry = pro._pro_users[self.CHAT]
            sent2, _clock2 = _drive_expiry(monkeypatch, ticks=1,
                                           now=new_expiry - 20 * 3600)
            assert any("expires today" in t for _c, t in sent2), sent2
        finally:
            self._restore(*saved)

    def test_a_restart_cannot_re_send_the_warning(self, monkeypatch):
        """The ledger is the point of the persistence: memory wiped, ledger
        reloaded from disk, and the last-day window stays silent. The old
        in-memory set re-sent the warning on every restart inside the last
        day — the operator ruled restarts must never re-notify."""
        saved = self._isolated()
        try:
            exp = time.time() + 20 * 3600
            pro._pro_users[self.CHAT] = exp
            pro._expiry_warned[self.CHAT] = exp
            pro.save_expiry_warnings()          # the real write, conftest-isolated
            pro._expiry_warned.clear()          # the restart
            pro.load_expiry_warnings()
            assert pro._expiry_warned.get(self.CHAT) == exp, "ledger survived"
            sent, _clock = _drive_expiry(monkeypatch, ticks=1)
            warns = [t for _c, t in sent if "expires today" in t]
            assert warns == [], f"a restart must not re-send, got {warns}"
        finally:
            self._restore(*saved)

    def test_the_ledger_prunes_finished_subscriptions(self):
        """Entries for subs that already ended can never match again (a renewal
        has a new expiry), so save/load drop them and the file stays bounded."""
        saved = self._isolated()
        try:
            pro._expiry_warned[self.CHAT] = time.time() - 60   # sub is over
            pro.save_expiry_warnings()
            pro._expiry_warned.clear()
            pro.load_expiry_warnings()
            assert self.CHAT not in pro._expiry_warned, "dead entries must drop"
        finally:
            self._restore(*saved)

    def test_a_missing_warned_file_is_not_an_error(self):
        saved = self._isolated()
        try:
            pro._expiry_warned.clear()
            pro.load_expiry_warnings()          # nothing on disk yet
            assert pro._expiry_warned == {}
        finally:
            self._restore(*saved)


class TestInvoiceEndpoint:
    class _Req:
        def __init__(self, headers):
            self.headers = headers

    def _auth(self, monkeypatch, user_id):
        monkeypatch.setattr(config, "_bot_token", "123456:TESTTOKEN")
        params = {"user": json.dumps({"id": user_id}, separators=(",", ":")),
                  "auth_date": str(int(time.time()))}
        check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", b"123456:TESTTOKEN", hashlib.sha256).digest()
        return {"Authorization": "tma " + urlencode(
            {**params, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})}

    def _create(self, headers):
        resp = asyncio.run(miniapp._api_create_invoice(self._Req(headers)))
        return json.loads(resp.body)

    def _with_chat(self, monkeypatch):
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS",
                            "BOTwallet1111111111111111111111111111111111")
        storage.track_sessions.clear()
        storage.track_sessions[9001] = {"RR123456789CN": {"name": "Box", "status": "In Transit"}}

    def test_two_calls_return_distinct_exact_amounts(self, monkeypatch):
        _clear_state()
        self._with_chat(monkeypatch)
        try:
            first = self._create(self._auth(monkeypatch, 9001))
            first_memo = pro._pending_payments[first["payment_id"]]["memo"]
            first_micros = pro._pending_payments[first["payment_id"]]["amount_micros"]
            second = self._create(self._auth(monkeypatch, 9001))
            assert first["amount_str"] != second["amount_str"], "identifier was reused"
            assert len(first["amount_str"].split(".")[1]) == 6
            assert float(first["amount_str"]) == first["amount"]
            assert f"amount={first['amount_str']}" in first["solana_pay_url"]
            assert f"amount={first['amount_str']}" in first["usdc_url"]
            # The note no longer asks for the exact dust amount: the reference is
            # what identifies the payer now, and a wallet that rounds still credits.
            assert "recognized automatically" in first["note"]
            assert "exact amount" not in first["note"]
            # Every URL for one invoice carries that invoice's reference, and the
            # stored record keeps it — that is what the monitor matches on.
            parked = (pro._pending_payments.get(first["payment_id"])
                      or pro._expired_payments[first["payment_id"]])
            stored_ref = parked["reference"]      # it survives the parking too
            assert stored_ref == first["reference"]
            assert f"reference={stored_ref}" in first["usdc_url"]
            assert f"reference={stored_ref}" in first["usdt_url"]
            assert f"reference={stored_ref}" in first["solana_pay_url"]
            # The Solana Pay memo travels to a public ledger: no chat ID in it.
            assert first["payment_id"] not in first["solana_pay_url"]
            assert f"memo={first_memo}" in first["usdt_url"]
            assert first_memo != pro._pending_payments[second["payment_id"]]["memo"]
            # one live invoice per chat: the later one replaces the earlier
            assert len(pro._pending_payments) == 1
            # …and the superseded one stays matchable, so a wallet still holding
            # the first amount is credited (to the same chat) and that amount
            # can't be handed to anyone else meanwhile.
            assert first_micros in pro.open_amounts()
            assert first_micros not in {
                inv["amount_micros"] for inv in pro._pending_payments.values()}
            found = pro._match_payment(first_micros)
            assert found and found[1]["chat_id"] == 9001
        finally:
            storage.track_sessions.clear()
            _clear_state()

    def test_invoice_records_micros_and_is_matchable(self, monkeypatch):
        _clear_state()
        self._with_chat(monkeypatch)
        try:
            body = self._create(self._auth(monkeypatch, 9001))
            invoice = pro._pending_payments[body["payment_id"]]
            assert invoice["amount_micros"] == int(round(float(body["amount_str"]) * 1e6))
            found = pro._match_payment(invoice["amount_micros"])
            assert found and found[1]["chat_id"] == 9001
        finally:
            storage.track_sessions.clear()
            _clear_state()

    def test_invoice_ids_are_unique_within_one_second(self, monkeypatch):
        """Two invoices in the same second used to share a key.

        The id was f"{chat_id}-{int(time.time())}", so the second create reused
        the first's key: the superseded invoice was parked under the same id the
        new one then took, and the buyer who paid the invoice they were shown
        could lose the credit.
        """
        _clear_state()
        self._with_chat(monkeypatch)
        frozen = time.time()
        monkeypatch.setattr(miniapp.time, "time", lambda: frozen)   # one second
        try:
            ids = [self._create(self._auth(monkeypatch, 9001))["payment_id"]
                   for _ in range(50)]
            assert len(set(ids)) == 50, "an invoice id was reused"
            assert all(i.startswith("inv-") for i in ids)
            known = set(pro._pending_payments) | set(pro._expired_payments)
            assert set(ids) <= known, "an invoice was overwritten by its sibling"
            # Every one resolves through the status endpoint (the cancel path is
            # the same dict lookup), parked or live.
            for pid in ids:
                resp = asyncio.run(miniapp._api_check_payment(
                    self._StatusReq(self._auth(monkeypatch, 9001), pid)))
                assert resp.status == 200, f"{pid} is no longer addressable"
                assert json.loads(resp.body) == {"confirmed": False, "pro": False}
        finally:
            storage.track_sessions.clear()
            _clear_state()

    class _StatusReq:
        def __init__(self, headers, payment_id):
            self.headers = headers
            self.query = {"id": payment_id}

    def test_invoice_creation_log_masks_identifiers(self, monkeypatch, caplog):
        """The creation line carried the raw id (which WAS the chat id plus a
        timestamp) right next to a masked chat field."""
        import logging as _logging
        _clear_state()
        # A 9-digit chat id can't collide with a 6-decimal amount in the log.
        chat = 987654321
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS",
                            "BOTwallet1111111111111111111111111111111111")
        storage.track_sessions.clear()
        storage.track_sessions[chat] = {"RR123456789CN": {"name": "Box",
                                                          "status": "In Transit"}}
        try:
            with caplog.at_level(_logging.INFO, logger="tracker"):
                body = self._create(self._auth(monkeypatch, chat))
            lines = "\n".join(r.getMessage() for r in caplog.records)
            assert "Solana invoice created" in lines, "the creation must be logged"
            assert body["payment_id"] not in lines, "the raw invoice id leaked"
            assert str(chat) not in lines, "the raw chat id leaked"
            assert storage.mask_chat(chat) in lines
            assert pro.mask_invoice_id(body["payment_id"]) in lines
        finally:
            storage.track_sessions.clear()
            _clear_state()


def test_tolerance_is_no_longer_used_for_matching():
    """The ±1 cent window made a flat price ambiguous; matching is exact now."""
    import inspect
    src = inspect.getsource(pro._match_payment) + inspect.getsource(pro.monitor_solana_payments)
    assert "PAYMENT_TOLERANCE" not in src
    assert "amount_micros" in src


def test_on_chain_float_recovers_the_invoice_amount_exactly():
    """RPC gives us a float; recovering micros must never drift by one."""
    for micros in (12_345, 10_001, 19_999, 1_000_000, 10_000_000):
        amount = micros / 1_000_000
        assert pro.to_micros(round(amount, 6)) == micros      # what the RPC path does
        assert pro.to_micros(amount) == micros


def test_reconcile_accepts_the_new_id_format():
    """Nothing may parse or rebuild an invoice id.

    The reconcile script has no invoice-id flag (it works from a signature or a
    masked chat tag), so what has to hold is that an id stays an opaque handle:
    the receipt trail quotes whatever shape it finds — old or new — and the log
    mask never exposes a legacy id's chat prefix.
    """
    root = Path(__file__).resolve().parent.parent
    script = (root / "scripts" / "payment_reconcile.py").read_text()
    frontend = (root / "webapp" / "index.html").read_text()
    for src in (script, frontend):
        assert "payment_id.split" not in src, "an invoice id must stay opaque"
    assert 'f"{chat_id}-' not in script and "int(time.time())" not in script, \
        "the legacy chat-timestamp id shape must not be rebuildable"

    new_id = "inv-0123456789abcdef"
    assert pro.mask_invoice_id(new_id) == "inv-01234567…"
    legacy = pro.mask_invoice_id("8098897948-1758000000")
    assert legacy.startswith("legacy-") and "8098897948" not in legacy

    # A re-sent receipt quotes its own id whatever shape it is (an already-open
    # legacy invoice must keep resolving, not break on the format change).
    for pid in (new_id, "8098897948-1758000000"):
        text = pro.pro_welcome_text(0.020000, config.USDC_MINT, "5sIg", pid,
                                    "manual", resent=True)
        assert pid in text, "the re-sent receipt must still quote its own id"



def test_open_invoices_survive_a_restart():
    """A restart used to discard every open invoice — the buyer paid the amount
    they were shown, the monitor matched it against an empty pool and credited
    nobody. The pools are persisted now, so the same amount still resolves."""
    _clear_state()
    pid, micros = _invoice(4242)
    pro.save_pending_payments()

    # Simulate the restart: memory gone, file on disk.
    _clear_state()
    assert pro._match_payment(micros) is None, "precondition: pools really empty"
    pro.load_pending_payments()

    found = pro._match_payment(micros)
    assert found is not None, "the paid amount must still identify its chat"
    assert found[0] == pid and found[1]["chat_id"] == 4242
    _clear_state()


def test_grace_pool_and_consumed_amounts_survive_a_restart():
    """Late payments and duplicate protection have to outlive a restart too: the
    grace pool is what honours a payment made minutes late, and the consumed
    amounts stop a duplicate transfer from hitting a fresh invoice."""
    _clear_state()
    pid, micros = _invoice(77)
    pro.park_invoice(pid, pro._pending_payments.pop(pid))
    pro.retire_amount({"amount_micros": 987_654})
    pro.save_pending_payments()

    _clear_state()
    pro.load_pending_payments()

    found = pro._match_payment(micros)
    assert found and found[0] == pid, "grace-pooled invoice must stay matchable"
    assert 987_654 in pro._used_amounts, "consumed amount must stay reserved"
    assert 987_654 in pro.open_amounts(), "and be avoided by new invoices"
    _clear_state()


class TestTimingMatchParksTheTransferAmount:
    """A two-decimal wallet rounds the invoice, so the transfer's micros differ
    from the invoice's. Parking only the invoice's amount left the *sent* value
    unreserved: after a restart the replay of that rounded amount was no longer
    in the duplicate check (a plain membership test), so it timing-matched a
    different open invoice and credited a second time.
    """

    I = 12_590          # the invoice amount the buyer was shown
    R = 20_000          # what the wallet actually sent (rounded up)

    def test_retire_parks_both_the_invoice_and_the_transfer_amount(self):
        _clear_state()
        try:
            assert self.I != self.R, "precondition: the wallet rounded"
            pro.retire_amount({"amount_micros": self.I}, self.R)
            assert self.I in pro._used_amounts, "invoice amount must be parked"
            assert self.R in pro._used_amounts, "the sent amount must be parked too"
        finally:
            _clear_state()

    def test_both_amounts_survive_a_restart_and_block_a_replay(self):
        """The restart-replay scenario the bug broke."""
        _clear_state()
        try:
            pro.retire_amount({"amount_micros": self.I}, self.R)
            pro.save_pending_payments()

            _clear_state()                       # restart: memory gone, file stays
            assert pro._used_amounts == {}
            pro.load_pending_payments()

            assert self.I in pro._used_amounts, "invoice amount lost on restart"
            assert self.R in pro._used_amounts, "rounded amount lost on restart"
            # The monitor's duplicate path (~line 723) is exactly this test.
            assert self.R in pro._used_amounts, "a replay must be caught, not credited"
        finally:
            _clear_state()

    def test_both_are_reserved_for_the_grace_window_then_released(self):
        _clear_state()
        try:
            pro.retire_amount({"amount_micros": self.I}, self.R)
            # Freshly parked: a new invoice must avoid both while the window is
            # open (this is the deliberate trade-off of parking the rounded value).
            assert {self.I, self.R} <= pro.open_amounts()

            stale = time.time() - config.PAYMENT_GRACE_SECONDS - 60
            pro._used_amounts[self.I] = stale
            pro._used_amounts[self.R] = stale
            # Past the window the reserve lapses...
            assert not ({self.I, self.R} & pro.open_amounts()), \
                "a released amount must be issuable to a new invoice again"

            # ...and the monitor's prune drops the entries outright.
            now = time.time()
            for micros in [m for m, at in pro._used_amounts.items()
                           if now - at > config.PAYMENT_GRACE_SECONDS]:
                pro._used_amounts.pop(micros, None)   # what the monitor does

            assert self.I not in pro._used_amounts
            assert self.R not in pro._used_amounts
        finally:
            _clear_state()

    def test_single_argument_call_still_parks_the_invoice_amount(self):
        """The old call shape (default transfer_micros=None) stays working."""
        _clear_state()
        try:
            pro.retire_amount({"amount_micros": 987_654})
            assert 987_654 in pro._used_amounts
        finally:
            _clear_state()


def test_first_scan_no_longer_orphans_a_payment():
    """The old monitor marked the last 15 signatures as seen without looking at
    them, so a payment landing just before a restart was invisible forever."""
    import inspect
    src = inspect.getsource(pro.monitor_solana_payments)
    assert "first_scan" not in src, "the blanket startup skip must stay removed"
    assert "save_pending_payments()" in src, "the monitor must persist its pools"
    assert "retry_counts" in src, "an RPC failure must be retried, not dropped"




class TestRoundedPaymentFallback:
    """A real buyer sent 0.020000 USDT while an invoice for 0.012590 was open.

    A wallet that shows two decimals renders every invoice as 0.01 or 0.02, so
    the exact-amount identifier cannot be the only way to be paid. The fallback
    accepts a plausible amount only when exactly one invoice was open at the
    moment the transfer landed.
    """

    @staticmethod
    def _rounded():
        base = int(round(config.PRO_PRICE_USD * 1_000_000))
        return ((base + pro._TAIL_MAX) // 10_000 + 1) * 10_000

    def test_rounded_payment_matches_when_one_invoice_was_open(self):
        _clear_state()
        now = time.time()
        pid, _ = _invoice(9001, created=now)
        found = pro._match_by_timing(self._rounded(), now + 90)
        assert found and found[0] == pid and found[1]["chat_id"] == 9001
        _clear_state()

    def test_ambiguous_rounded_payment_is_refused(self):
        """Two buyers waiting at once: the amount cannot tell them apart."""
        _clear_state()
        now = time.time()
        _invoice(9001, created=now)
        _invoice(9002, created=now + 5)
        assert pro._match_by_timing(self._rounded(), now + 90) is None
        _clear_state()

    def test_amount_outside_the_price_band_is_refused(self):
        _clear_state()
        now = time.time()
        _invoice(9001, created=now)
        base = int(round(config.PRO_PRICE_USD * 1_000_000))
        assert pro._match_by_timing(base * 5, now + 90) is None, "overpay is not a price"
        assert pro._match_by_timing(base - 1, now + 90) is None, "underpay is not a price"
        _clear_state()

    def test_transfer_before_the_invoice_existed_is_refused(self):
        """Money that moved before the invoice was created belongs to nobody here."""
        _clear_state()
        now = time.time()
        _invoice(9001, created=now)
        assert pro._match_by_timing(self._rounded(), now - 600) is None
        _clear_state()

    def test_repeat_of_a_consumed_amount_is_not_recredited(self):
        """A duplicate of an already-paid amount must never hit a fresh invoice."""
        _clear_state()
        now = time.time()
        _invoice(9001, created=now)
        pro._used_amounts[self._rounded()] = now
        assert pro._match_by_timing(self._rounded(), now + 90) is None
        _clear_state()

    def test_monitor_tries_the_fallback_before_giving_up(self):
        import inspect
        src = inspect.getsource(pro.monitor_solana_payments)
        assert "_match_by_timing" in src, "the monitor must use the fallback"
        assert "blockTime" in src, "and judge 'open' by the transfer's own time"



class TestPriceChangeTolerance:
    """A price change must not strand money that is already in flight.

    The band used to be derived from the configured price alone (and the timing path
    kept its own copy of that rule), so raising the price would refuse a transfer for
    an invoice that was created — and quoted — at the old one: a real payment, refused,
    with the receipt claiming nothing happened. Each invoice now records the base it
    was created at, and one band rule serves every path.
    """

    CHAT = 7451

    @staticmethod
    def _old_price_invoice(created=None):
        pid = "inv-old-price"
        pro._pending_payments[pid] = {
            "chat_id": TestPriceChangeTolerance.CHAT, "amount": 0.010500,
            "amount_micros": 10_500, "base_micros": 10_000,
            "memo": pro.new_memo_token(), "reference": pro.new_reference(),
            "created": created if created is not None else time.time()}
        return pid

    def test_an_old_price_invoice_keeps_its_band_while_it_is_matchable(
            self, monkeypatch):
        _clear_state()
        monkeypatch.setattr(config, "PRO_PRICE_USD", 0.02)        # the price was raised
        try:
            self._old_price_invoice()
            assert 20_000 in pro._active_price_bases(), "today's price"
            assert 10_000 in pro._active_price_bases(), "and the invoice's"
            assert pro._in_price_band(10_500), "the old price is still a price"
            assert pro._in_price_band(20_000), "and so is the new one"
            assert not pro._in_price_band(5_000_000), "5 USDT still is not"

            # The old band retires with its invoice: nothing left to pay it for.
            _clear_state()
            assert not pro._in_price_band(10_500)
        finally:
            _clear_state()

    def test_a_rounded_payment_at_the_old_price_still_matches(self, monkeypatch):
        """The timing path is the one a rounding wallet lands on."""
        _clear_state()
        monkeypatch.setattr(config, "PRO_PRICE_USD", 0.02)
        try:
            now = time.time()
            pid = self._old_price_invoice(created=now)
            found = pro._match_by_timing(10_000, now + 5)         # the old base, rounded
            assert found is not None and found[0] == pid, \
                "a payment for the old price must still match its invoice"
        finally:
            _clear_state()

    def test_the_invoice_records_the_price_it_was_created_at(self, monkeypatch):
        harness = TestInvoiceEndpoint()
        _clear_state()
        harness._with_chat(monkeypatch)
        try:
            body = harness._create(harness._auth(monkeypatch, 9001))
            invoice = pro._pending_payments[body["payment_id"]]
            assert invoice["base_micros"] == int(round(config.PRO_PRICE_USD * 1_000_000))
            assert invoice["base_micros"] <= invoice["amount_micros"], \
                "the price is the floor the tail sits on"
        finally:
            storage.track_sessions.clear()
            _clear_state()



class TestReceiptCopy:
    """The confirmation message must state how the payment was actually matched.

    A receipt is a document the buyer keeps: claiming "the amount identified you"
    on a credit that was recognised by timing (or by an operator) would be a lie
    in writing. And a buyer who says "I paid but got no message" needs the receipt
    re-sent, not another payment.
    """

    SIG = ("5Ch99oBGKmDQ1H2buQh5cEcb4NjZAzt8kzHb3MYQMRyq53XVyuHtJft4dd"
           "UAyunvgBpk6hZm2URsTdTFLuDGvDEk")
    MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"   # USDT

    def _text(self, **kw):
        return pro.pro_welcome_text(0.020000, self.MINT, self.SIG,
                                    "manual-5Ch99oBGKmDQ", expiry_ts=1e9, **kw)

    def test_each_match_method_states_its_own_truth(self):
        amount = [l for l in self._text(matched_by="amount").splitlines()
                  if "Matched by" in l][0]
        timing = [l for l in self._text(matched_by="timing").splitlines()
                  if "Matched by" in l][0]
        manual = [l for l in self._text(matched_by="manual").splitlines()
                  if "Matched by" in l][0]
        assert "unique amount" in amount
        assert "open invoice at the time" in timing
        assert "manually" in manual
        assert len({amount, timing, manual}) == 3, "the three must not read alike"

    def test_receipt_quotes_the_real_payment(self):
        text = self._text(matched_by="manual")
        assert "0.020000 USDT (Solana)" in text, "amount + token"
        assert "solscan.io/tx/" + self.SIG in text, "on-chain link"
        assert "manual-5Ch99oBGKmDQ" in text, "receipt id"
        assert f"Track up to {config.PRO_PACKAGE_LIMIT} packages" in text

    def test_resent_copy_is_labelled_and_keeps_the_expiry(self):
        text = self._text(matched_by="manual", resent=True)
        assert "receipt" in text.splitlines()[0].lower()
        assert "copy" in text.splitlines()[0].lower()
        assert "Expires:" in text, "a re-sent receipt still shows when Pro ends"

    def test_activation_message_defaults_to_the_amount_claim(self, monkeypatch):
        """The automatic path (no matched_by) keeps the identifier wording."""
        import botstate
        bot = _Bot()
        monkeypatch.setattr(botstate, "bot", bot)
        _clear_state()
        asyncio.run(
            pro._activate_pro(4321, 0.010042, self.MINT, self.SIG, "inv-1"))
        assert bot.sent, "activation must notify the buyer"
        assert "unique amount" in bot.sent[0][1]
        _clear_state()

    def test_notify_never_touches_the_subscription(self):
        """A re-send must be safe to run while the bot is up: no state write."""
        import inspect
        src = inspect.getsource(pro.pro_welcome_text)
        assert "save_pro_users" not in src, "the text builder must not persist"
        script = (Path(__file__).resolve().parent.parent
                  / "scripts" / "payment_reconcile.py").read_text()
        notify = script[script.index("async def _notify"):
                        script.index("async def _describe")]
        assert "_activate_pro" not in notify, "--notify must not credit again"
        assert "save_pro_users" not in notify, "--notify must not write pro_users"


class TestReconcileApplyLoadsState:
    """`--apply` must load the store before it credits anyone.

    pro.save_pro_users() rewrites the whole file from the in-memory `_pro_users`
    dict, and a fresh payment_reconcile.py process starts with that dict empty.
    Crediting one chat without loading first therefore dumped every other Pro
    subscriber on the floor — silently, and only on the operator's manual path.
    These tests pin both the behaviour and the order of the two calls.
    """

    A = 61001          # already a Pro subscriber on file
    B = 61002          # the buyer --apply is crediting

    def test_crediting_one_chat_keeps_every_other_subscriber(self, monkeypatch,
                                                             tmp_path):
        import botstate
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(botstate, "bot", None)
        expiry_a = time.time() + 20 * 86400
        # Seed the tmp store the way it looks on disk: string keys, encrypted.
        storage._write_protected(config.PRO_USERS_FILE,
                                 json.dumps({str(self.A): expiry_a}))
        pro._pro_users.clear()
        try:
            # Exactly what main() now does for --apply: load, then activate.
            pro.load_pro_state()
            assert pro._pro_users.get(self.A) == expiry_a
            asyncio.run(pro._activate_pro(
                self.B, 0.010042, config.USDC_MINT,
                "5sIgNatUrEsIgNatUrEsIgNatUrEsIgNatUrEsIgNatUrE",
                payment_id="manual-5sIgNatUrEsI", matched_by="manual"))
            on_disk = json.loads(
                storage._read_protected(config.PRO_USERS_FILE)[0])
            assert float(on_disk[str(self.B)]) > time.time(), "B was not credited"
            assert float(on_disk[str(self.A)]) == expiry_a, \
                "the credit wiped a subscriber who was not involved"
        finally:
            pro._pro_users.clear()

    def test_apply_branch_loads_state_before_it_activates(self, monkeypatch,
                                                          tmp_path):
        """Ordering guard: a future refactor must not drop the load again."""
        calls = []
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TELEGRAM_BOT_API_TOKEN", raising=False)
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS",
                            "BOTwallet1111111111111111111111111111111111")

        async def _rpc(method, params):
            return {"ok": True}

        monkeypatch.setattr(pro, "_sol_rpc", _rpc)
        monkeypatch.setattr(pro, "_tx_payment_info",
                            lambda tx: (0.010042, config.USDC_MINT, ""))
        monkeypatch.setattr(pro, "load_pro_state",
                            lambda: calls.append("load"))

        async def _activate(*args, **kw):
            calls.append("activate")

        monkeypatch.setattr(pro, "_activate_pro", _activate)

        mod = _load_reconcile_module()
        monkeypatch.setattr(mod, "_bot_running", lambda: "")
        monkeypatch.setattr(sys, "argv",
                            ["payment_reconcile.py", "--sig",
                             "5sIgNatUrEsIgNatUrEsIgNatUrEsIgNatUrEsIgNatUrE",
                             "--chat", str(self.B), "--apply"])

        rc = asyncio.run(mod.main())

        assert rc == 0, "the --apply path should have completed"
        assert calls == ["load", "activate"], \
            "--apply must load the store before _activate_pro rewrites it"


class TestReceiptMigration:
    """The receipts used to be a plaintext .log naming the paying chat.

    Migrating has to be a one-way door: parse the legacy lines, write the
    encrypted file, and delete the plaintext — leaving it behind would defeat the
    reason for encrypting in the first place.
    """

    LEGACY = ("1758000000,8098897948,0.020000,USDT,5Ch99oSig,manual-5Ch99o,1758600000\n"
              "1758100000,8098897949,0.012347,USDC,4AbcdSig,inv-2,1758700000\n")

    def test_legacy_plaintext_log_is_migrated_then_deleted(self, monkeypatch, tmp_path):
        legacy = tmp_path / "pro_receipts.log"
        legacy.write_text(self.LEGACY)
        monkeypatch.chdir(tmp_path)
        pro._receipts.clear()

        pro.load_receipts()

        assert len(pro._receipts) == 2, "both legacy lines must survive"
        assert legacy.exists() is False, "the plaintext log must be removed"
        raw = config.PRO_RECEIPTS_FILE.read_bytes()
        assert raw.startswith(b"gAAAA"), "the migrated trail must be encrypted"
        assert b"8098897948" not in raw, "no chat id may remain in plaintext"

    def test_migrated_entries_are_readable_and_quote_correctly(self, monkeypatch,
                                                               tmp_path):
        legacy = tmp_path / "pro_receipts.log"
        legacy.write_text(self.LEGACY)
        monkeypatch.chdir(tmp_path)
        pro._receipts.clear()
        pro.load_receipts()

        entry = pro.last_receipt_for(8098897948)
        assert entry is not None
        assert entry["amount"] == 0.020000 and entry["token"] == "USDT"
        assert entry["sig"] == "5Ch99oSig"
        assert pro.last_receipt_for(12345) is None, "unknown chat has no receipt"

    def test_reconciliation_restores_what_receipts_prove_and_never_shortens(
            self, monkeypatch, tmp_path):
        """_activate_pro writes the receipt first and pro_users second; a crash
        between the two left a paying customer silently free after a restart
        (2026-10-04 audit). The receipt trail is the durable proof, so the
        startup reconciliation extends the store to it — and only extends:
        manual grants, comp time and a rollback must survive."""
        monkeypatch.chdir(tmp_path)
        pro._receipts.clear()
        later = time.time() + 86400 * 10
        earlier = time.time() + 86400
        pro._receipts.append({"ts": time.time(), "chat_id": 7401, "amount": 1.0,
                              "token": "USDC", "sig": "sig-recon-1",
                              "payment_id": "inv-recon-1", "expiry": later})
        pro._receipts.append({"ts": time.time(), "chat_id": 7402, "amount": 1.0,
                              "token": "USDC", "sig": "sig-recon-2",
                              "payment_id": "inv-recon-2", "expiry": earlier})
        try:
            pro._pro_users[7401] = 0.0        # the store lost the payment entirely
            pro._pro_users[7402] = later + 1  # the store already grants longer
            fixed = pro.reconcile_receipts()
            assert (7401, later) in fixed
            assert pro._pro_users[7401] == later, "receipt-proved expiry restored"
            assert pro._pro_users[7402] == later + 1, "the store is never shortened"
            assert all(c != 7402 for c, _ in fixed), "no correction where none is due"
        finally:
            pro._receipts.clear()
            pro._pro_users.clear()

    def test_reconciliation_never_resurrects_a_lapsed_receipt(self, monkeypatch,
                                                              tmp_path):
        """A receipt whose term already ended is history, not state: restoring
        it would put the lapsed chat back into _pro_users on every startup, and
        the expiry task would re-send "Your Pro subscription expired" after
        every restart (seen live 2026-10-04 19:01 — a bounce re-notified all
        three lapsed chats). Crash recovery only ever restores LIVE Pro."""
        monkeypatch.chdir(tmp_path)
        pro._receipts.clear()
        past = time.time() - 86400 * 7
        future = time.time() + 86400 * 10
        pro._receipts.append({"ts": past, "chat_id": 7501, "amount": 1.0,
                              "token": "USDC", "sig": "sig-recon-past",
                              "payment_id": "inv-recon-past", "expiry": past})
        pro._receipts.append({"ts": time.time(), "chat_id": 7502, "amount": 1.0,
                              "token": "USDC", "sig": "sig-recon-live",
                              "payment_id": "inv-recon-live", "expiry": future})
        try:
            fixed = pro.reconcile_receipts()
            assert fixed == [(7502, future)], "only the live receipt is a fix"
            assert 7501 not in pro._pro_users, "lapsed member must stay out"
            assert pro._pro_users.get(7502) == future, "live Pro restored"
        finally:
            pro._receipts.clear()
            pro._pro_users.clear()

    def test_a_missing_file_is_not_an_error(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        pro._receipts.clear()
        pro.load_receipts()          # nothing on disk yet
        assert pro._receipts == []



def test_cursor_pages_past_the_first_fifteen(monkeypatch):
    """A burst of more than 15 transfers must not lose the excess.

    The monitor looked at exactly one page of 15 signatures per cycle: anything
    older that had not been seen yet was never fetched. Paging walks back with a
    `before` cursor and stops as soon as it reaches ground already handled.
    """
    _clear_state()
    pages = [
        [{"signature": f"sig-new-{i:02d}"} for i in range(15)],
        [{"signature": "sig-known"}, {"signature": "sig-beyond-15"}],
    ]
    calls = []

    async def fake_rpc(method, params):
        assert method == "getSignaturesForAddress"
        calls.append(params[1])
        return pages[len(calls) - 1] if len(calls) <= len(pages) else []

    monkeypatch.setattr(pro, "_sol_rpc", fake_rpc)
    try:
        found = asyncio.run(pro._recent_signatures(known={"sig-known"}))
        assert len(calls) == 2, "both data pages must be fetched"
        assert calls[0] == {"limit": 15}, "the first page carries no cursor"
        assert calls[1]["before"] == "sig-new-14", \
            "the cursor is the oldest signature of the previous page"
        assert "sig-beyond-15" in [s["signature"] for s in found], \
            "a signature beyond the first 15 must be discovered"

        # Reaching handled ground stops the walk: steady state costs one call.
        calls.clear()
        asyncio.run(pro._recent_signatures(known={"sig-new-07"}))
        assert len(calls) == 1, "a known signature on page one must stop the paging"

        # ...and with nothing recognised the walk is bounded at three pages.
        pages[:] = [[{"signature": f"p{p}-{i}"} for i in range(15)]
                    for p in range(4)]
        calls.clear()
        asyncio.run(pro._recent_signatures(known=set()))
        assert len(calls) == 3, "the paging must stop after three pages"
    finally:
        _clear_state()


def test_reconcile_apply_retires_the_invoice_it_credits(monkeypatch, tmp_path):
    """A manual credit must consume the amount, like the monitor does.

    Otherwise the invoice stays matchable, and the monitor pass after the restart
    this script tells you to do (known_sigs is empty again) credits the same
    transfer a second time.
    """
    import botstate
    _clear_state()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(botstate, "bot", None)
    monkeypatch.setattr(config, "BOT_USDC_ADDRESS",
                        "BOTwallet1111111111111111111111111111111111")
    pid, micros = _invoice(7112)
    invoice = pro._pending_payments[pid]
    amount = micros / 1_000_000
    try:
        # The exact sequence --apply runs: load state, retire, activate.
        pro.load_pro_state()
        retired = pro.retire_credited_amount(micros)
        asyncio.run(pro._activate_pro(
            7112, amount, config.USDC_MINT, "5sIgNatUrEsIgNatUrEsIgNatUrEsIg",
            payment_id="manual-5sIgNatUrEsI", matched_by="manual"))

        assert retired == 1
        assert pid not in pro._pending_payments, "the invoice must be gone"
        assert pid not in pro._expired_payments, "and out of the grace pool too"
        assert micros in pro._used_amounts, "the amount must be consumed"
        # A monitor pass over the same transfer now matches nothing at all.
        assert pro._match_payment(micros, invoice["memo"], time.time()) is None
        assert pro._match_by_timing(micros, time.time()) is None
    finally:
        _clear_state()


def test_reconcile_apply_path_retires_before_it_activates():
    """Source gate: the script and the test above must not drift apart."""
    script = (Path(__file__).resolve().parent.parent
              / "scripts" / "payment_reconcile.py").read_text()
    body = script[script.index("    pro.load_pro_state()"):]
    assert "retire_credited_amount" in body, "--apply no longer retires the invoice"
    assert body.index("retire_credited_amount") < body.index("_activate_pro"), \
        "the retirement must happen before the credit"


def test_reconcile_apply_refuses_an_already_credited_amount(monkeypatch, capsys):
    """Re-running --apply for the same payment must not stack a second period.

    The retirement (above) stops the monitor from re-crediting; this stops the
    operator's own re-run. --force is the documented override.
    """
    mod = _load_reconcile_module()
    micros = 10_042                      # base + tail: inside the price band
    monkeypatch.setattr(config, "BOT_USDC_ADDRESS",
                        "BOTwallet1111111111111111111111111111111111")
    monkeypatch.delenv("TELEGRAM_BOT_API_TOKEN", raising=False)
    monkeypatch.setattr(mod, "_bot_running", lambda: "")
    monkeypatch.setattr(pro, "load_pro_state", lambda: None)   # seeded below

    async def _rpc(method, params):
        return {"ok": True}

    monkeypatch.setattr(pro, "_sol_rpc", _rpc)
    monkeypatch.setattr(pro, "_tx_payment_info",
                        lambda tx: (micros / 1_000_000, config.USDC_MINT, ""))
    activated, retired = [], []

    async def _activate(*args, **kw):
        activated.append(args)

    monkeypatch.setattr(pro, "_activate_pro", _activate)
    monkeypatch.setattr(pro, "retire_credited_amount",
                        lambda m: retired.append(m) or 0)

    saved = dict(pro._used_amounts)
    pro._used_amounts.clear()
    pro._used_amounts[micros] = time.time()        # already credited once
    argv = ["payment_reconcile.py", "--sig", "5sIgNatUrEsIg", "--chat", "6001",
            "--apply"]
    try:
        monkeypatch.setattr(sys, "argv", argv)
        rc = asyncio.run(mod.main())
        out = capsys.readouterr().out
        assert rc == 2, out
        assert "already been credited" in out and "--force" in out
        assert not activated, "an already-credited amount must not be credited again"
        assert not retired, "and nothing may be retired either"

        # --force is the documented override for a genuinely new payment.
        monkeypatch.setattr(sys, "argv", argv + ["--force"])
        rc = asyncio.run(mod.main())
        out = capsys.readouterr().out
        assert rc == 0, out
        assert len(activated) == 1, "--force must let the operator override"
        assert retired == [micros], "and then the amount is retired as usual"
    finally:
        pro._used_amounts.clear()
        pro._used_amounts.update(saved)


class _StopMonitor(BaseException):
    """Breaks the monitor's endless loop after exactly one cycle.

    BaseException on purpose: the loop catches Exception for its own errors, so an
    Exception-based sentinel would be swallowed and the test would sit there
    sleeping forever.
    """


class TestReferenceMatching:
    """The reference identifies the payer where the amount alone cannot.

    Real case 2026-09-23: a $0.013434 invoice was paid with 0.010001 USDT. A wallet
    that shows two decimals turned the amount into 0.01, the exact-micro-amount
    identifier matched nothing, no Pro arrived and the buyer retried five invoices.
    A Solana Pay reference travels in the transaction as a read-only account, so the
    buyer is identifiable no matter what the wallet did to the amount.
    """

    WALLET = "BOTwallet1111111111111111111111111111111111"
    CHAT = 7301

    def test_b58_encoding_matches_solana(self):
        # 32 zero bytes is the system program's address; a leading zero byte is a
        # leading '1'. Both catch an off-by-one alphabet, instantly and forever.
        assert pro._b58encode(b"\x00" * 32) == "1" * 32
        assert pro._b58encode(b"\x00" * 31 + b"\x01") == "1" * 31 + "2"

    def test_references_look_like_pubkeys_and_never_repeat(self):
        refs = [pro.new_reference() for _ in range(50)]
        assert len(set(refs)) == 50, "an invoice would be identifiable only by luck"
        assert all(32 <= len(r) <= 44 for r in refs), "not a pubkey-shaped string"
        assert all(c in pro._B58_ALPHABET for r in refs for c in r)

    def test_tx_references_reads_both_account_key_shapes(self):
        tx = {"transaction": {"message": {"accountKeys": [
            {"pubkey": "Sender1111", "signer": True}, "RefKey2222"]}}}
        assert pro._tx_references(tx) == {"Sender1111", "RefKey2222"}
        assert pro._tx_references({}) == set()

    def _rounded_tx(self, reference):
        """A jsonParsed transfer of 0.010000 that carries `reference`."""
        return {
            "transaction": {"message": {
                "accountKeys": [{"pubkey": "Sender1111", "signer": True},
                                {"pubkey": self.WALLET, "writable": True},
                                {"pubkey": reference, "signer": False,
                                 "writable": False}],
                "instructions": []}},
            "meta": {
                "preTokenBalances": [{"accountIndex": 1, "mint": config.USDC_MINT,
                                      "owner": self.WALLET,
                                      "uiTokenAmount": {"uiAmount": 0.0}}],
                "postTokenBalances": [{"accountIndex": 1, "mint": config.USDC_MINT,
                                       "owner": self.WALLET,
                                       "uiTokenAmount": {"uiAmount": 0.010000}}],
            },
        }
    def test_a_rounded_amount_is_credited_by_its_reference(self, monkeypatch,
                                                          tmp_path):
        """The 2026-09-23 failure, end to end through one monitor cycle."""
        import botstate
        _clear_state()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS", self.WALLET)
        monkeypatch.setattr(pro, "load_pro_state", lambda: None)  # keep the pool
        monkeypatch.setattr(botstate, "bot", None)               # no Telegram here

        reference = pro.new_reference()
        created = time.time()
        pro._pending_payments["inv-rounded"] = {
            "chat_id": self.CHAT, "amount": 0.013434, "amount_micros": 13434,
            "memo": pro.new_memo_token(), "reference": reference,
            "created": created}
        tx = self._rounded_tx(reference)
        assert pro._tx_payment_info(tx)[0] == 0.010000, "the wallet rounded it"
        assert pro._match_payment(10_000, "", created + 5) is None, \
            "the amount alone cannot identify this payment"

        credited = []

        async def _activate(chat_id, amount, mint, sig, payment_id="",
                            matched_by="amount"):
            credited.append((chat_id, amount, matched_by))

        monkeypatch.setattr(pro, "_activate_pro", _activate)
        page_calls = []

        async def fake_rpc(method, params):
            if method == "getSignaturesForAddress":
                page_calls.append(params)
                return ([{"signature": "sig-rounded", "blockTime": created + 5}]
                        if len(page_calls) == 1 else [])
            if method == "getTransaction":
                return tx
            return None

        monkeypatch.setattr(pro, "_sol_rpc", fake_rpc)

        async def _stop(_seconds):
            raise _StopMonitor()

        monkeypatch.setattr(pro.asyncio, "sleep", _stop)
        try:
            try:
                asyncio.run(pro.monitor_solana_payments())
            except _StopMonitor:
                pass

            assert credited == [(self.CHAT, 0.010000, "reference")], (
                f"a rounded payment must credit by reference, got {credited}")
            assert "inv-rounded" not in pro._pending_payments
            assert 13434 in pro._used_amounts, "the invoice amount is consumed"
            assert 10_000 in pro._used_amounts, "and so is the amount that arrived, "\
                "so a replay of the rounded transfer is caught after a restart"
            # Single-use: a restart re-scans the same signature with an empty known
            # set, and there is nothing left to credit.
            assert pro._match_by_reference(tx) is None
            assert pro._match_by_timing(13434, created + 5) is None
        finally:
            _clear_state()

    def test_a_reference_in_the_grace_pool_still_credits(self, monkeypatch,
                                                         tmp_path):
        """A late payment through an old QR must count, like the amount path does."""
        _clear_state()
        monkeypatch.chdir(tmp_path)
        reference = pro.new_reference()
        tx = self._rounded_tx(reference)
        pro._expired_payments["inv-parked"] = {
            "chat_id": 7302, "amount": 0.013434, "amount_micros": 13435,
            "memo": pro.new_memo_token(), "reference": reference,
            "created": time.time() - 3600, "expired_at": time.time() - 1800}
        try:
            found = pro._match_by_reference(tx)
            assert found is not None and found[0] == "inv-parked"
            assert found[1]["chat_id"] == 7302
        finally:
            _clear_state()

    def test_the_live_pool_wins_over_the_grace_pool(self, monkeypatch, tmp_path):
        """Only a hand-edited file could repeat a reference, but the choice the
        monitor makes must be deliberate rather than incidental."""
        _clear_state()
        monkeypatch.chdir(tmp_path)
        reference = pro.new_reference()
        tx = self._rounded_tx(reference)
        pro._expired_payments["inv-parked"] = {"chat_id": 1, "reference": reference}
        pro._pending_payments["inv-live"] = {"chat_id": 2, "reference": reference}
        try:
            assert pro._match_by_reference(tx)[0] == "inv-live"
        finally:
            _clear_state()

    def test_a_legacy_invoice_without_a_reference_still_matches_by_amount(self):
        """Invoices written before this change carry no reference key at all: they
        must fall through to the unchanged amount path, not break on it."""
        _clear_state()
        pid, micros = _invoice(7303)
        try:
            assert pro._pending_payments[pid].get("reference") is None
            assert pro._match_by_reference(self._rounded_tx("SomeOtherKey1")) is None
            found = pro._match_payment(micros)
            assert found is not None and found[0] == pid
        finally:
            _clear_state()

    def test_the_welcome_receipt_explains_a_reference_match(self, monkeypatch,
                                                            tmp_path):
        monkeypatch.chdir(tmp_path)
        text = pro.pro_welcome_text(0.010000, config.USDC_MINT, "5sIgNatUrEsIg",
                                    payment_id="inv-1", matched_by="reference",
                                    activated_ts=time.time(),
                                    expiry_ts=time.time() + 86400)
        assert "the payment link you used" in text
        assert "confirmed against your payment" not in text


def _b58decode(text: str) -> bytes:
    """Independent base58 decoder for the checks below.

    Same alphabet, opposite direction, written out here on purpose: if the encoder
    used the wrong alphabet or dropped the leading-zero rule, decoding a real
    mainnet address with this would not give its bytes back.
    """
    number = 0
    for char in text:
        number = number * 58 + pro._B58_ALPHABET.index(char)
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + body


class TestDepositAddresses:
    """The manual path: the address itself is the identifier.

    A reference only helps when the wallet speaks Solana Pay. An exchange — or any
    wallet that ignores the parameter — sends a bare transfer with no reference, no
    memo, and whatever amount its form rounded to. That is the 2026-09-23 0.010001
    USDT case: nothing left to match on. So every chat gets its own address, and
    anything accepted that lands on it belongs to that chat.
    """

    SEED = "a1" * 32
    WALLET = "BOTwallet1111111111111111111111111111111111"
    CHAT = 7401

    # RFC 8032 §7.1: seed -> public key. Pinned byte for byte because this decides
    # which address holds a customer's money — a wrong derivation is unrecoverable.
    RFC8032 = (
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"),
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"),
        ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
         "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025"),
    )

    @staticmethod
    def _seed(monkeypatch, value=SEED):
        monkeypatch.setattr(config, "DEPOSIT_SEED", value)

    @staticmethod
    def _tx(address, sent=0.010000, mint=None, memo=None):
        """A jsonParsed transfer into `address` — no reference, no memo by default."""
        mint = mint or config.USDT_MINT
        tx = {
            "transaction": {"message": {
                "accountKeys": [{"pubkey": "ExchangeHotWallet1111", "signer": True},
                                {"pubkey": address, "writable": True}],
                "instructions": []}},
            "meta": {
                "preTokenBalances": [{"accountIndex": 1, "mint": mint,
                                      "owner": address,
                                      "uiTokenAmount": {"uiAmount": 0.0}}],
                "postTokenBalances": [{"accountIndex": 1, "mint": mint,
                                       "owner": address,
                                       "uiTokenAmount": {"uiAmount": sent}}],
            },
        }
        if memo:
            tx["transaction"]["message"]["instructions"] = [
                {"parsed": {"type": "memo", "info": {"memo": memo}}}]
        return tx

    @staticmethod
    def _scan_rpc(address, signatures, tx):
        """A Solana RPC stub: one signature page for `address`, then the tx."""
        async def fake_rpc(method, params):
            if method == "getSignaturesForAddress":
                # params is [address, {"limit": n[, "before": sig]}] — the cursor
                # lives in the dict, not in the list (checking the list made this
                # stub hand back the same page three times).
                if params[0] == address and "before" not in params[1]:
                    return signatures
                return []
            if method == "getTransaction":
                return tx
            return None
        return fake_rpc



    def test_the_derivation_matches_rfc_8032(self):
        """The check that matters: the RFC's own vectors, not a round-trip.

        Both paths are pinned — the library one and the hand-written fallback —
        because a plausible-looking wrong key is an address nobody can spend from.
        """
        for seed_hex, public_hex in self.RFC8032:
            seed = bytes.fromhex(seed_hex)
            assert pro._ed25519_public_key_pure(seed).hex() == public_hex
            assert pro._ed25519_public_key(seed).hex() == public_hex

    def test_the_pure_fallback_agrees_with_the_library(self):
        """The fallback is what runs when `cryptography` is missing, and it is
        hand-written — so it is checked against the library, not against itself."""
        import secrets as _secrets
        if not pro._HAVE_ED25519_LIB:            # pragma: no cover
            return
        for _ in range(25):
            seed = _secrets.token_bytes(32)
            assert pro._ed25519_public_key_pure(seed) == \
                pro._ed25519_public_key(seed), "fallback disagrees with the library"

    def test_same_seed_and_chat_always_give_the_same_address(self, monkeypatch):
        self._seed(monkeypatch)
        first = pro.deposit_address(self.CHAT)
        assert first, "a configured seed must produce an address"
        assert pro.deposit_address(self.CHAT) == first
        # A different master seed is a different address (no cross-seed collision).
        self._seed(monkeypatch, "b2" * 32)
        assert pro.deposit_address(self.CHAT) != first

    def test_every_chat_gets_its_own_address(self, monkeypatch):
        self._seed(monkeypatch)
        addresses = [pro.deposit_address(self.CHAT + i) for i in range(100)]
        assert all(a and 32 <= len(a) <= 44 for a in addresses)
        assert len(set(addresses)) == 100, "two chats shared an address"
        assert all(c in pro._B58_ALPHABET for a in addresses for c in a)

    def test_the_address_is_the_base58_public_key_of_the_derived_secret(
            self, monkeypatch):
        self._seed(monkeypatch)
        secret = pro.deposit_secret(self.CHAT)
        assert secret is not None and len(secret) == 32
        assert _b58decode(pro.deposit_address(self.CHAT)) == \
            pro._ed25519_public_key(secret)
        # Anchors for the encoder itself: a real mainnet address round-trips, and
        # 32 zero bytes is the system program (base58 tends to lose those ones).
        for mint in (config.USDC_MINT, config.USDT_MINT):
            assert pro._b58encode(_b58decode(mint)) == mint
        assert pro._b58encode(b"\x00" * 32) == "1" * 32

    def test_the_export_is_the_importable_64_byte_key(self, monkeypatch):
        """Phantom imports base58(seed ‖ pubkey); that is what a manual sweep uses."""
        self._seed(monkeypatch)
        raw = _b58decode(pro.deposit_keypair_b58(self.CHAT))
        assert len(raw) == 64
        assert raw[:32] == pro.deposit_secret(self.CHAT)
        assert raw[32:] == pro._ed25519_public_key(pro.deposit_secret(self.CHAT))

    def test_a_rounded_exchange_transfer_is_credited_by_the_address(
            self, monkeypatch, tmp_path):
        """The 2026-09-23 case, through the deposit path that exists for it.

        The invoice was 0.013434, the buyer's exchange sent 0.010000 USDT — no
        reference, no memo, nothing that identifies them. The address does.
        """
        _clear_state()
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch)
        address = pro.deposit_address(self.CHAT)
        pro._pending_payments["inv-deposit"] = {
            "chat_id": self.CHAT, "amount": 0.013434, "amount_micros": 13434,
            "memo": pro.new_memo_token(), "reference": pro.new_reference(),
            "created": time.time()}
        credited = []

        async def _activate(chat_id, amount, mint, sig, payment_id="",
                            matched_by="amount"):
            credited.append((chat_id, amount, mint, matched_by))

        monkeypatch.setattr(pro, "_activate_pro", _activate)
        tx = self._tx(address)
        monkeypatch.setattr(pro, "_sol_rpc", self._scan_rpc(
            address, [{"signature": "sig-exchange", "blockTime": time.time()}], tx))
        try:
            known = set()
            assert asyncio.run(
                pro._scan_deposit_address(address, self.CHAT, known)) == 1
            assert credited == [(self.CHAT, 0.010000, config.USDT_MINT, "deposit")], \
                credited
            assert "inv-deposit" not in pro._pending_payments, \
                "the invoice must leave the pools, like the main flow"
            assert "inv-deposit" not in pro._expired_payments
            # Deliberately NOT parked in _used_amounts: rounded amounts repeat
            # across chats, and known_sigs already gives exactly-once per transfer.
            assert 10_000 not in pro._used_amounts
            # The same signature cannot be credited twice — this is what replaces
            # the amount dedup on this path.
            assert asyncio.run(
                pro._scan_deposit_address(address, self.CHAT, known)) == 0
        finally:
            _clear_state()

    def test_the_monitor_credits_a_transfer_to_the_personal_address(
            self, monkeypatch, tmp_path):
        """End to end through one monitor cycle: the address is polled and paid."""
        import botstate
        _clear_state()
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch)
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS", self.WALLET)
        monkeypatch.setattr(pro, "load_pro_state", lambda: None)
        monkeypatch.setattr(botstate, "bot", None)

        address = pro.deposit_address(self.CHAT)
        pro._deposit_addrs[self.CHAT] = {"address": address, "created": time.time()}
        pro._pending_payments["inv-deposit"] = {
            "chat_id": self.CHAT, "amount": 0.013434, "amount_micros": 13434,
            "memo": pro.new_memo_token(), "reference": pro.new_reference(),
            "created": time.time()}
        credited = []

        async def _activate(chat_id, amount, mint, sig, payment_id="",
                            matched_by="amount"):
            credited.append((chat_id, amount, matched_by))

        monkeypatch.setattr(pro, "_activate_pro", _activate)
        deposit_page = [{"signature": "sig-monitor-deposit", "blockTime": time.time()}]
        tx = self._tx(address)
        monkeypatch.setattr(pro, "_sol_rpc", self._scan_rpc(address, deposit_page, tx))

        async def _stop(_seconds):
            raise _StopMonitor()

        monkeypatch.setattr(pro.asyncio, "sleep", _stop)
        try:
            try:
                asyncio.run(pro.monitor_solana_payments())
            except _StopMonitor:
                pass
            assert credited == [(self.CHAT, 0.010000, "deposit")], credited
            assert "inv-deposit" not in pro._pending_payments
        finally:
            _clear_state()
            pro._deposit_addrs.clear()

    def test_an_amount_outside_the_price_band_is_not_credited(
            self, monkeypatch, tmp_path):
        """A deposit address is not an open credit line: 5 USDT is not a price.

        On the shared wallet the amount is an identifier, so a band check is only a
        sanity guard. Here the amount means nothing — somebody typing 5 USDT into
        an exchange form would otherwise buy Pro for a cent.
        """
        _clear_state()
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch)
        address = pro.deposit_address(self.CHAT)
        credited = []

        async def _activate(*args, **kw):
            credited.append(kw.get("matched_by"))

        monkeypatch.setattr(pro, "_activate_pro", _activate)
        page = [{"signature": "sig-too-much"},
                {"signature": "sig-too-little"}]
        monkeypatch.setattr(pro, "_sol_rpc", self._scan_rpc(
            address, page, self._tx(address, sent=5.0)))
        known = set()
        try:
            assert asyncio.run(
                pro._scan_deposit_address(address, self.CHAT, known)) == 0
            assert not credited
            assert {"sig-too-much", "sig-too-little"} <= known, \
                "an out-of-band transfer must not be re-fetched every cycle"
        finally:
            _clear_state()


    @staticmethod
    def _watch_rpc(held, tx=None, signatures=("sig-late",)):
        """Balance-aware stub: `held` maps mint → uiAmount for the watched address."""
        calls = []

        async def fake_rpc(method, params):
            calls.append(method)
            if method == "getTokenAccountsByOwner":
                amount = held.get(params[1]["mint"], 0.0)
                if not amount:
                    return {"value": []}
                return {"value": [{"account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"uiAmount": amount}}}}}}]}
            if method == "getSignaturesForAddress":
                if "before" in params[1]:
                    return []
                return [{"signature": sig, "blockTime": time.time()}
                        for sig in signatures]
            if method == "getTransaction":
                return tx
            return None

        return fake_rpc, calls

    def test_money_arriving_with_no_invoice_open_is_still_credited(
            self, monkeypatch, tmp_path):
        """The blind spot this closes, watched live on 2026-09-23: the invoice expired,
        the fast poll for that address stopped, and a transfer landing afterwards was
        invisible until the buyer happened to reopen the sheet."""
        _clear_state()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "DEPOSIT_ADDRS_FILE",
                            tmp_path / "deposit_addrs.json")
        self._seed(monkeypatch)
        address = pro.deposit_address(self.CHAT)
        pro._deposit_addrs[self.CHAT] = {"address": address, "created": 1.0}
        credited = []

        async def _activate(chat_id, amount, mint, sig, payment_id="",
                            matched_by="amount"):
            credited.append((chat_id, amount, matched_by, payment_id))

        monkeypatch.setattr(pro, "_activate_pro", _activate)
        fake, _calls = self._watch_rpc({config.USDT_MINT: 0.010000},
                                       tx=self._tx(address))
        monkeypatch.setattr(pro, "_sol_rpc", fake)
        try:
            assert asyncio.run(pro._deposit_balance_watch(set(), {})) == 1
            # No invoice was open, so the credit is a renewal with no invoice id.
            assert credited == [(self.CHAT, 0.010000, "deposit", "")], credited
        finally:
            _clear_state()
            pro._deposit_addrs.clear()

    def test_an_unchanged_balance_costs_only_the_two_balance_calls(
            self, monkeypatch, tmp_path):
        """A credited-but-unswept address keeps its balance forever: comparing against
        the last seen total is what stops this paging signatures every pass."""
        _clear_state()
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch)
        address = pro.deposit_address(self.CHAT)
        pro._deposit_addrs[self.CHAT] = {"address": address, "created": 1.0}
        fake, calls = self._watch_rpc({config.USDT_MINT: 0.010000})
        monkeypatch.setattr(pro, "_sol_rpc", fake)
        try:
            seen = {self.CHAT: 0.010000}          # already seen (credited, unswept)
            assert asyncio.run(pro._deposit_balance_watch(set(), seen)) == 0
            assert calls == ["getTokenAccountsByOwner"] * 2, calls
        finally:
            pro._deposit_addrs.clear()

    def test_a_chat_with_a_live_invoice_is_left_to_the_fast_path(self, monkeypatch,
                                                                 tmp_path):
        """The prompt route for a live invoice must not be duplicated here."""
        _clear_state()
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch)
        address = pro.deposit_address(self.CHAT)
        pro._deposit_addrs[self.CHAT] = {"address": address, "created": 1.0}
        _invoice(self.CHAT)                        # pending invoice for that chat
        fake, calls = self._watch_rpc({config.USDT_MINT: 0.010000})
        monkeypatch.setattr(pro, "_sol_rpc", fake)
        try:
            assert asyncio.run(pro._deposit_balance_watch(set(), {})) == 0
            assert calls == [], "a live-invoice address is the fast path's job"
        finally:
            _clear_state()
            pro._deposit_addrs.clear()

    def test_the_balance_watch_is_capped_and_says_so(self, monkeypatch, tmp_path,
                                                     caplog):
        _clear_state()
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch)
        pro._deposit_addrs.clear()
        for chat in range(7900, 7900 + 30):
            pro._deposit_addrs[chat] = {"address": pro.deposit_address(chat),
                                        "created": float(chat)}
        fake, calls = self._watch_rpc({})           # every address holds nothing
        monkeypatch.setattr(pro, "_sol_rpc", fake)
        try:
            with caplog.at_level(logging.WARNING, logger="tracker"):
                assert asyncio.run(pro._deposit_balance_watch(set(), {})) == 0
            assert calls.count("getTokenAccountsByOwner") == 2 * pro._DEPOSIT_POLL_LIMIT
            assert "capped" in caplog.text
        finally:
            pro._deposit_addrs.clear()


    def test_the_price_band_accepts_rounding_and_nothing_far_off(self):
        """What "inside the band" means, in numbers: the product price plus the
        rounded-up envelope a two-decimal wallet or exchange form sends."""
        base = int(round(config.PRO_PRICE_USD * 1_000_000))
        ceiling = ((base + pro._TAIL_MAX) // 10_000 + 1) * 10_000
        assert base == 10_000, "the free tier price is the band's floor"
        assert pro._in_price_band(base)
        assert pro._in_price_band(base + 1)
        assert pro._in_price_band(ceiling), "a two-decimal form sends exactly this"
        assert not pro._in_price_band(base - 1), "underpay is not a price"
        assert not pro._in_price_band(ceiling + 10_000)
        assert not pro._in_price_band(5_000_000), "5 USDT is not a subscription"

    def test_a_second_transfer_to_the_same_address_is_a_renewal(
            self, monkeypatch, tmp_path):
        """Two distinct transfers, both rounded to 0.01 — and both count.

        On the shared wallet the second would look like a duplicate of a consumed
        amount. Here the signature, not the amount, is what makes it once — so a
        buyer who tops up twice is credited twice instead of being told to wait.
        """
        _clear_state()
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch)
        address = pro.deposit_address(self.CHAT)
        pro._used_amounts[10_000] = time.time()      # as if 0.01 was seen before
        credited = []

        async def _activate(chat_id, amount, mint, sig, payment_id="",
                            matched_by="amount"):
            credited.append((sig, matched_by))

        monkeypatch.setattr(pro, "_activate_pro", _activate)
        page = [{"signature": "sig-first"}, {"signature": "sig-second"}]
        monkeypatch.setattr(pro, "_sol_rpc", self._scan_rpc(
            address, page, self._tx(address)))
        try:
            assert asyncio.run(
                pro._scan_deposit_address(address, self.CHAT, set())) == 2
            assert credited == [("sig-first", "deposit"), ("sig-second", "deposit")]
        finally:
            _clear_state()

    def test_a_credited_transfer_is_not_credited_again_after_a_restart(
            self, monkeypatch, tmp_path):
        """The signature set lives in memory; the receipts trail is the durable record.

        The deposit path deliberately skips the _used_amounts dedup (rounded amounts
        repeat across chats), so nothing else stops a restart from re-crediting a
        transfer that is still inside that address's newest page — a free renewal for
        whoever opens the sheet next.
        """
        import botstate
        _clear_state()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "DEPOSIT_ADDRS_FILE",
                            tmp_path / "deposit_addrs.json")
        monkeypatch.setattr(botstate, "bot", None)          # no Telegram in a test
        self._seed(monkeypatch)
        saved_receipts = list(pro._receipts)
        pro._receipts.clear()

        address = pro.deposit_address(self.CHAT)
        tx = self._tx(address)
        monkeypatch.setattr(pro, "_sol_rpc", self._scan_rpc(
            address, [{"signature": "sig-once", "blockTime": time.time()}], tx))
        try:
            pro._pending_payments["inv-first"] = {
                "chat_id": self.CHAT, "amount": 0.013434, "amount_micros": 13434,
                "memo": pro.new_memo_token(), "reference": pro.new_reference(),
                "created": time.time()}
            assert asyncio.run(
                pro._scan_deposit_address(address, self.CHAT, set())) == 1
            assert len(pro._receipts) == 1, "a credit must write a receipt"

            # Restart: empty signature set, the same transfer still on the chain, and
            # the chat opening the sheet again (so a fresh invoice is open).
            pro._pending_payments["inv-second"] = {
                "chat_id": self.CHAT, "amount": 0.013434, "amount_micros": 13435,
                "memo": pro.new_memo_token(), "reference": pro.new_reference(),
                "created": time.time()}
            assert asyncio.run(
                pro._scan_deposit_address(address, self.CHAT, set())) == 0, \
                "a credited transfer must not pay for a second subscription"
            assert len(pro._receipts) == 1, "no second receipt for the same transfer"
            assert "inv-second" in pro._pending_payments, "the new invoice stays open"
        finally:
            _clear_state()
            pro._receipts.clear()
            pro._receipts.extend(saved_receipts)

    def test_a_failed_transaction_is_never_credited(self, monkeypatch, tmp_path):
        _clear_state()
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch)
        address = pro.deposit_address(self.CHAT)
        credited = []

        async def _activate(*args, **kw):
            credited.append(kw.get("matched_by"))

        monkeypatch.setattr(pro, "_activate_pro", _activate)
        page = [{"signature": "sig-failed", "err": {"InstructionError": [0, "x"]}}]
        monkeypatch.setattr(pro, "_sol_rpc", self._scan_rpc(address, page, None))
        known = set()
        try:
            assert asyncio.run(
                pro._scan_deposit_address(address, self.CHAT, known)) == 0
            assert not credited
            assert "sig-failed" in known, \
                "a failed transfer is handled once, not retried forever"
        finally:
            _clear_state()

    def test_only_chats_with_a_live_invoice_are_polled(self, monkeypatch, tmp_path):
        """An expired invoice is already covered by its QR/reference path, so
        polling its address for the whole grace window is RPC work for nothing."""
        _clear_state()
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch)
        pro._deposit_addrs.clear()
        try:
            for chat in (7501, 7502, 7503):
                pro._deposit_addrs[chat] = {
                    "address": pro.deposit_address(chat), "created": float(chat)}
            _invoice(7501)                                  # live
            _invoice(7502)                                  # live
            pid, _micros = _invoice(7503)
            pro.park_invoice(pid, pro._pending_payments.pop(pid))   # grace only

            targets = pro._deposit_poll_targets()
            assert [chat for _address, chat in targets] == [7501, 7502]
        finally:
            _clear_state()
            pro._deposit_addrs.clear()

    def test_the_poll_list_is_capped_and_says_so(self, monkeypatch, tmp_path,
                                                 caplog):
        """The public RPC rate-limits per IP: a burst of invoices must not turn one
        cycle into forty calls."""
        _clear_state()
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch)
        pro._deposit_addrs.clear()
        try:
            for chat in range(7600, 7600 + 40):
                pro._deposit_addrs[chat] = {
                    "address": pro.deposit_address(chat), "created": float(chat)}
                _invoice(chat)
            with caplog.at_level(logging.WARNING, logger="tracker"):
                targets = pro._deposit_poll_targets()
            assert len(targets) == pro._DEPOSIT_POLL_LIMIT == 25
            assert "capped" in caplog.text
        finally:
            _clear_state()
            pro._deposit_addrs.clear()


    def test_without_the_seed_there_is_no_address_and_no_extra_polling(
            self, monkeypatch, tmp_path):
        """Unset or malformed, the feature is simply absent — never a bad address.

        A short seed is a guessable seed: every chat's key derives from it, so a
        half-configured value has to disable the path rather than half-work.
        """
        _clear_state()
        monkeypatch.chdir(tmp_path)
        for bad in ("", "   ", "not-hex", "ab" * 31, "ab" * 33):
            self._seed(monkeypatch, bad)
            assert pro.deposit_seed_bytes() is None
            assert pro.deposit_address(self.CHAT) is None
            assert pro.deposit_keypair_b58(self.CHAT) is None

        # Nothing handed out and no seed: there is nothing to poll (verification 3
        # — the fresh-install case, where the field is null and no work is added).
        self._seed(monkeypatch, "")
        _invoice(self.CHAT)
        try:
            assert pro._deposit_poll_targets() == [], \
                "an unset seed must not add any RPC work"
        finally:
            _clear_state()
            pro._deposit_addrs.clear()

        # An address handed out by an EARLIER seed is still watched, though: it is a
        # real address a buyer may still have in an exchange form, and the seed is
        # only needed to DERIVE an address — not to notice money arriving on one.
        self._seed(monkeypatch)
        address = pro.deposit_address(self.CHAT)
        pro._deposit_addrs[self.CHAT] = {"address": address, "created": time.time()}
        _invoice(self.CHAT)
        self._seed(monkeypatch, "")
        try:
            assert [chat for _address, chat in pro._deposit_poll_targets()] == \
                [self.CHAT]
        finally:
            _clear_state()
            pro._deposit_addrs.clear()

    def test_the_monitor_says_once_that_the_seed_is_missing(self, monkeypatch,
                                                            tmp_path, caplog):
        """Silence would be the worst outcome: money lands, nobody is credited, and
        nothing anywhere says why."""
        import botstate
        monkeypatch.chdir(tmp_path)
        self._seed(monkeypatch, "")
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS", self.WALLET)
        monkeypatch.setattr(botstate, "bot", None)

        async def fake_rpc(method, params):
            return []

        monkeypatch.setattr(pro, "_sol_rpc", fake_rpc)

        async def _stop(_seconds):
            raise _StopMonitor()

        monkeypatch.setattr(pro.asyncio, "sleep", _stop)
        with caplog.at_level(logging.INFO, logger="tracker"):
            try:
                asyncio.run(pro.monitor_solana_payments())
            except _StopMonitor:
                pass
        assert "DEPOSIT_SEED" in caplog.text
        assert "token_hex(32)" in caplog.text, "the log must say how to generate one"

    def test_the_registry_survives_a_restart_and_stays_encrypted(self, monkeypatch,
                                                                 tmp_path):
        """Verification 2: the same address comes back after a reload.

        The address is re-derived from the seed (so a lost file is recoverable),
        but the file is kept as the auditable list of addresses handed out — and it
        names the paying chats, so it is encrypted at rest like the other stores.
        """
        self._seed(monkeypatch)
        monkeypatch.setattr(config, "DEPOSIT_ADDRS_FILE",
                            tmp_path / "deposit_addrs.json")
        pro._deposit_addrs.clear()
        try:
            address = pro.deposit_address(self.CHAT)
            pro.register_deposit_address(self.CHAT, address)
            raw = (tmp_path / "deposit_addrs.json").read_bytes()
            if storage._ENCRYPTION_AVAILABLE:
                assert raw.startswith(b"gAAAA"), "the chat→address map was in the open"
                assert str(self.CHAT).encode() not in raw

            pro._deposit_addrs.clear()
            pro.load_deposit_addrs()
            assert pro._deposit_addrs[self.CHAT]["address"] == address
            assert pro.deposit_address(self.CHAT) == address
            # Registering the same pair again is a no-op, not a rewrite: the
            # "created" stamp is the age the poll cap orders by.
            stamp = pro._deposit_addrs[self.CHAT]["created"]
            pro.register_deposit_address(self.CHAT, address)
            assert pro._deposit_addrs[self.CHAT]["created"] == stamp
        finally:
            pro._deposit_addrs.clear()

    def test_the_invoice_hands_over_the_personal_address(self, monkeypatch):
        """The Mini App can only show the manual path if the endpoint carries it."""
        harness = TestInvoiceEndpoint()
        _clear_state()
        harness._with_chat(monkeypatch)
        pro._deposit_addrs.clear()
        self._seed(monkeypatch)
        try:
            body = harness._create(harness._auth(monkeypatch, 9001))
            address = pro.deposit_address(9001)
            assert body["deposit_address"] == address
            assert body["deposit_address"] != config.BOT_USDC_ADDRESS
            assert pro._deposit_addrs[9001]["address"] == address, \
                "the registry is the list of addresses this bot handed out"
            assert "personal address" in body["note"]
            assert "recognized automatically" in body["note"], \
                "the QR/link stays the primary path in the copy"
            assert "exact amount" not in body["note"]

            # With the seed unset the field is null and the copy stays as it was.
            self._seed(monkeypatch, "")
            plain = harness._create(harness._auth(monkeypatch, 9001))
            assert plain["deposit_address"] is None
            assert "personal address" not in plain["note"]
            assert "recognized automatically" in plain["note"]
        finally:
            storage.track_sessions.clear()
            _clear_state()
            pro._deposit_addrs.clear()

    def test_the_receipt_says_the_personal_address_identified_them(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        text = pro.pro_welcome_text(0.010000, config.USDT_MINT, "5sIgNatUrEsIg",
                                    payment_id="inv-9", matched_by="deposit",
                                    activated_ts=time.time(),
                                    expiry_ts=time.time() + 86400)
        assert "your personal deposit address" in text
        assert "confirmed against your payment" not in text



class TestDepositOperatorCommands:
    """The two operator commands for the deposit addresses.

    v1 moves no money automatically: --sweep-deposits is a read-only report and
    --deposit-secret prints ONE key to the terminal for a manual sweep. Both have to
    stay privacy-safe (the chat id is never printed) and the export must need an
    explicit tag, so a stray command cannot spray keys across a terminal.
    """

    SEED = "c3" * 32
    CHAT = 7701

    def _load(self, monkeypatch, tmp_path, seed=SEED):
        mod = _load_reconcile_module()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "DEPOSIT_SEED", seed)
        monkeypatch.setattr(config, "DEPOSIT_ADDRS_FILE",
                            tmp_path / "deposit_addrs.json")
        monkeypatch.setattr(config, "DATA_FILE", tmp_path / "track_data.json")
        pro._deposit_addrs.clear()
        return mod

    @staticmethod
    def _balance_rpc(amounts):
        """A getTokenAccountsByOwner stub: mint -> uiAmount for the fake address."""
        async def fake_rpc(method, params):
            if method != "getTokenAccountsByOwner":
                return None
            mint = params[1]["mint"]
            return {"value": [{"account": {"data": {"parsed": {"info": {
                "tokenAmount": {"uiAmount": amounts.get(mint, 0.0)}}}}}}]}
        return fake_rpc

    def test_sweep_without_a_seed_still_reports_stranded_balances(
            self, monkeypatch, tmp_path, capsys):
        """The report must not need the seed: addresses live in the registry and
        balances live on the chain. Losing the seed is exactly when the operator has
        to see how much money is stranded — refusing then is backwards."""
        mod = self._load(monkeypatch, tmp_path)                 # seeded
        address = pro.deposit_address(self.CHAT)
        pro._deposit_addrs[self.CHAT] = {"address": address, "created": time.time()}
        monkeypatch.setattr(config, "DEPOSIT_SEED", "")         # seed lost / removed
        monkeypatch.setattr(pro, "_sol_rpc", self._balance_rpc(
            {config.USDC_MINT: 0.013434}))

        async def _no_sleep(_seconds):
            return None

        monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(sys, "argv", ["payment_reconcile.py", "--sweep-deposits"])
        try:
            assert asyncio.run(mod.main()) == 0
            out = capsys.readouterr().out
            assert address in out and "0.013434 USDC" in out
            assert "DEPOSIT_SEED is not set" in out
            assert "cannot be exported" not in out          # it CAN be seen, not swept
            assert "Do not discard that seed" in out
        finally:
            pro._deposit_addrs.clear()

    def test_sweep_with_an_empty_registry_says_nothing_was_handed_out(
            self, monkeypatch, tmp_path, capsys):
        mod = self._load(monkeypatch, tmp_path, seed="")
        monkeypatch.setattr(sys, "argv", ["payment_reconcile.py", "--sweep-deposits"])
        assert asyncio.run(mod.main()) == 0
        assert "No deposit address has been handed out" in capsys.readouterr().out

    def test_sweep_reports_balances_and_moves_nothing(self, monkeypatch, tmp_path,
                                                      capsys):
        mod = self._load(monkeypatch, tmp_path)
        monkeypatch.delenv("TELEGRAM_BOT_API_TOKEN", raising=False)
        for chat in (self.CHAT, self.CHAT + 1):
            pro._deposit_addrs[chat] = {"address": pro.deposit_address(chat),
                                        "created": float(chat)}
        monkeypatch.setattr(pro, "_sol_rpc", self._balance_rpc(
            {config.USDC_MINT: 0.012345, config.USDT_MINT: 0.5}))

        async def _no_sleep(_seconds):
            return None

        monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(sys, "argv", ["payment_reconcile.py", "--sweep-deposits"])
        try:
            rc = asyncio.run(mod.main())
            out = capsys.readouterr().out
            assert rc == 0, out
            for chat in (self.CHAT, self.CHAT + 1):
                assert pro.deposit_address(chat) in out
                assert str(chat) not in out, "the report must not name a chat"
                assert storage.mask_chat(chat) in out
            # Two addresses, so each mint's balance counts twice.
            assert "0.024690 USDC" in out, "the USDC total must add up"
            assert "1.000000 USDT" in out, "the USDT total must add up"
            assert "nothing was moved" in out
        finally:
            pro._deposit_addrs.clear()

    def test_the_export_needs_an_explicit_chat_tag(self, monkeypatch, tmp_path,
                                                   capsys):
        """No tag, no key: the export must be a deliberate, single-address act."""
        mod = self._load(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv",
                            ["payment_reconcile.py", "--deposit-secret"])
        assert asyncio.run(mod.main()) == 2
        out = capsys.readouterr().out
        assert "--chat-tag" in out
        assert pro.deposit_keypair_b58(self.CHAT) not in out

    def test_the_export_prints_one_address_key_and_no_chat_id(self, monkeypatch,
                                                              tmp_path, capsys):
        mod = self._load(monkeypatch, tmp_path)
        storage.track_sessions[self.CHAT] = {}
        monkeypatch.setattr(sys, "argv",
                            ["payment_reconcile.py", "--deposit-secret",
                             "--chat-tag", storage.mask_chat(self.CHAT)])
        try:
            rc = asyncio.run(mod.main())
            out = capsys.readouterr().out
            assert rc == 0, out
            assert pro.deposit_address(self.CHAT) in out
            assert pro.deposit_keypair_b58(self.CHAT) in out
            assert str(self.CHAT) not in out, "a key and a chat id must not travel together"
            assert pro.deposit_keypair_b58(self.CHAT + 1) not in out, \
                "exactly one address is exported per invocation"
        finally:
            storage.track_sessions.pop(self.CHAT, None)
            pro._deposit_addrs.clear()

class TestDepositSecretsStayPrivate:
    """The key can move a customer's money, so it must never reach a log line.

    DEPOSIT_SEED is the master secret — every chat's spendable key is one hash away
    from it — and the derived key signs away that chat's balance. Both are therefore
    checked against the log output of the paths that actually touch them, rather
    than trusting a comment that says not to log them.
    """

    CHAT = 7801
    SEED = "d4" * 32
    WALLET = "BOTwallet1111111111111111111111111111111111"

    def test_neither_the_seed_nor_a_key_reaches_a_log_line(self, monkeypatch,
                                                           tmp_path, caplog):
        import botstate
        _clear_state()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "DEPOSIT_SEED", self.SEED)
        monkeypatch.setattr(config, "DEPOSIT_ADDRS_FILE",
                            tmp_path / "deposit_addrs.json")
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS", self.WALLET)
        monkeypatch.setattr(botstate, "bot", None)
        pro._deposit_addrs.clear()
        try:
            address = pro.deposit_address(self.CHAT)
            pro.register_deposit_address(self.CHAT, address)
            _invoice(self.CHAT)
            secrets_at_stake = (
                (pro.deposit_secret(self.CHAT).hex(), "derived secret"),
                (pro.deposit_seed_bytes().hex(), "master seed"),
                (pro.deposit_keypair_b58(self.CHAT), "exported keypair"),
            )

            async def _activate(*args, **kw):
                return None

            monkeypatch.setattr(pro, "_activate_pro", _activate)
            monkeypatch.setattr(pro, "_sol_rpc", TestDepositAddresses._scan_rpc(
                address, [{"signature": "sig-private"}],
                TestDepositAddresses._tx(address)))
            with caplog.at_level(logging.DEBUG, logger="tracker"):
                assert asyncio.run(
                    pro._scan_deposit_address(address, self.CHAT, set())) == 1
                pro.save_deposit_addrs()
            for value, what in secrets_at_stake:
                assert value not in caplog.text, f"the {what} reached a log line"
            # The address itself is public (it is shown to the buyer), so it may be
            # logged; the log line that mentions the credit must still be chat-tagged.
            assert str(self.CHAT) not in caplog.text
        finally:
            _clear_state()
            pro._deposit_addrs.clear()


class TestOurOwnTransfersAreNotPayments:
    """A sweep of deposit funds into the shared wallet must not buy anybody Pro.

    The shared wallet's incoming balance is the monitor's evidence that somebody
    paid — and our own money moving raises it too. Sweeping a deposit address into
    the shared wallet is exactly that (the workflow --deposit-secret exists for).
    Before the guard, a sweep of 0.017777 matched the 0.017777 invoice another chat
    had open and credited THEM for money that was not theirs; a rounded sweep was
    credited by timing instead. Found by simulating the operator's own sweep.
    """

    WALLET = "BOTwallet1111111111111111111111111111111111"
    SEED = "e5" * 32
    STRANGER = "SomeCustomerWallet111111111111111111111111"

    def _sweep_tx(self, sender, amount=0.017777, dest=None):
        """jsonParsed: `sender` moves `amount` USDT into `dest` (the shared wallet)."""
        dest = dest or self.WALLET
        return {
            "transaction": {"signatures": ["sig-sweep"],
                            "message": {
                                "accountKeys": [
                                    {"pubkey": sender, "signer": True,
                                     "writable": True},
                                    {"pubkey": dest, "signer": False,
                                     "writable": True}],
                                "instructions": []}},
            "meta": {
                "preTokenBalances": [{"accountIndex": 0, "mint": config.USDT_MINT,
                                      "owner": sender,
                                      "uiTokenAmount": {"uiAmount": float(amount)}},
                                     {"accountIndex": 1, "mint": config.USDT_MINT,
                                      "owner": dest,
                                      "uiTokenAmount": {"uiAmount": 0.0}}],
                "postTokenBalances": [{"accountIndex": 0, "mint": config.USDT_MINT,
                                       "owner": sender,
                                       "uiTokenAmount": {"uiAmount": 0.0}},
                                      {"accountIndex": 1, "mint": config.USDT_MINT,
                                       "owner": dest,
                                       "uiTokenAmount": {"uiAmount": float(amount)}}],
            },
        }

    def test_a_strangers_transfer_is_still_a_payment(self, monkeypatch, tmp_path):
        """The guard must not over-block: only OUR signatures disqualify a transfer."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS", self.WALLET)
        assert pro._signed_by_us(self._sweep_tx(self.STRANGER)) is False
        info = pro._tx_payment_info(self._sweep_tx(self.STRANGER))
        assert info is not None and info[0] == 0.017777, \
            "a customer paying from their own wallet is still a payment"

    def test_a_declared_operator_wallet_is_never_a_customer(self, monkeypatch,
                                                            tmp_path):
        """OPERATOR_WALLETS: our own wallet funding the receiving wallet is our
        money moving, not somebody paying — and only the listed wallets are."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS", self.WALLET)
        mine = "MyPersonalWallet1111111111111111111111111"
        monkeypatch.setattr(config, "OPERATOR_WALLETS", frozenset({mine}))

        top_up = self._sweep_tx(mine)
        assert pro._signed_by_us(top_up) is True
        assert pro._tx_payment_info(top_up) is None, "a top-up must not buy Pro"
        assert pro._tx_payment_info(self._sweep_tx(self.STRANGER)) is not None, \
            "an undeclared wallet is still a customer"

        # With the knob empty the old behaviour is exactly as it was.
        monkeypatch.setattr(config, "OPERATOR_WALLETS", frozenset())
        assert pro._signed_by_us(top_up) is False
        assert pro._tx_payment_info(top_up) is not None

    def test_the_operator_wallet_list_parses_like_an_env_value(self):
        """Operators type these by hand: commas, stray spaces, semicolons, blanks."""
        assert config._wallet_list(" A1 , ;A2,  A3 ,, ") == {"A1", "A2", "A3"}
        assert config._wallet_list("") == frozenset()
        assert config._wallet_list(None) == frozenset()
        assert config._wallet_list("only-one") == {"only-one"}


    def test_a_transfer_signed_by_the_shared_wallet_is_refused(self, monkeypatch,
                                                              tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS", self.WALLET)
        assert pro._signed_by_us(self._sweep_tx(self.WALLET)) is True
        assert pro._tx_payment_info(self._sweep_tx(self.WALLET)) is None

    def test_our_own_wallet_funding_a_deposit_address_is_refused(self, monkeypatch,
                                                                 tmp_path):
        """The other direction: we top a deposit address up ourselves. Our money
        must not look like that chat paying for Pro."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "DEPOSIT_SEED", self.SEED)
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS", self.WALLET)
        deposit = pro.deposit_address(9002)
        assert pro._tx_payment_info(self._sweep_tx(self.WALLET, dest=deposit),
                                    deposit) is None
        # ...while the same transfer from a stranger's wallet is that chat paying.
        assert pro._tx_payment_info(self._sweep_tx(self.STRANGER, dest=deposit),
                                    deposit) is not None


    def test_a_registry_address_sweeping_into_the_shared_wallet_is_not_credited(
            self, monkeypatch, tmp_path):
        """The operator's actual sweep, end to end through one monitor cycle."""
        import botstate
        _clear_state()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "DEPOSIT_SEED", self.SEED)
        monkeypatch.setattr(config, "DEPOSIT_ADDRS_FILE",
                            tmp_path / "deposit_addrs.json")
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS", self.WALLET)
        monkeypatch.setattr(botstate, "bot", None)
        monkeypatch.setattr(pro, "load_pro_state", lambda: None)

        sweeper = pro.deposit_address(9002)
        pro.register_deposit_address(9002, sweeper)   # as the endpoint does
        # Another chat is waiting for exactly the amount being swept in.
        pro._pending_payments["inv-victim"] = {
            "chat_id": 9001, "amount": 0.017777, "amount_micros": 17777,
            "memo": pro.new_memo_token(), "reference": pro.new_reference(),
            "created": time.time()}
        credited = []

        async def _activate(*args, **kw):
            credited.append(kw.get("matched_by"))

        monkeypatch.setattr(pro, "_activate_pro", _activate)
        tx = self._sweep_tx(sweeper)

        async def fake_rpc(method, params):
            if method == "getSignaturesForAddress":
                # The cursor lives in params[1], not in the [address, params] list.
                if params[0] == self.WALLET and "before" not in params[1]:
                    return [{"signature": "sig-sweep", "blockTime": time.time()}]
                return []
            if method == "getTransaction":
                return tx
            return None

        monkeypatch.setattr(pro, "_sol_rpc", fake_rpc)

        async def _stop(_seconds):
            raise _StopMonitor()

        monkeypatch.setattr(pro.asyncio, "sleep", _stop)
        try:
            try:
                asyncio.run(pro.monitor_solana_payments())
            except _StopMonitor:
                pass
            assert not credited, "our own sweep bought somebody Pro"
            assert "inv-victim" in pro._pending_payments, "the invoice must stay open"
        finally:
            _clear_state()
            pro._deposit_addrs.clear()



def test_describe_explains_our_own_sweep_instead_of_hiding_it(monkeypatch, tmp_path,
                                                              capsys):
    """The operator checks a signature with --scan; a sweep must read as a sweep.

    "no accepted transfer into the wallet" is technically true of our own money
    moving, and completely unhelpful when the operator is standing there wondering
    why the consolidation they just did was not credited.
    """
    mod = _load_reconcile_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "DEPOSIT_SEED", "f6" * 32)
    monkeypatch.setattr(config, "DEPOSIT_ADDRS_FILE",
                        tmp_path / "deposit_addrs.json")
    monkeypatch.setattr(config, "BOT_USDC_ADDRESS",
                        "BOTwallet1111111111111111111111111111111111")
    pro._deposit_addrs.clear()
    try:
        sweeper = pro.deposit_address(7802)
        pro.register_deposit_address(7802, sweeper)
        sweep = {
            "transaction": {"signatures": ["sig-sweep"],
                            "message": {"accountKeys": [
                                {"pubkey": sweeper, "signer": True},
                                {"pubkey": config.BOT_USDC_ADDRESS,
                                 "signer": False}], "instructions": []}},
            "meta": {"preTokenBalances": [], "postTokenBalances": []},
        }

        async def _rpc(method, params):
            return sweep

        monkeypatch.setattr(pro, "_sol_rpc", _rpc)
        asyncio.run(mod._describe("5sIg" + "x" * 40))
        out = capsys.readouterr().out
        assert "sweep/consolidation" in out
        assert "nothing to credit" in out
    finally:
        pro._deposit_addrs.clear()



class TestAuditCommand:
    """--audit: the chain against the receipts.

    Nothing ever leaves the receiving wallet, so its balance only grows and every
    unit of it must be explainable by a receipt. The leftover is the only signal
    that can catch a payment that arrived and was never credited — so it must be
    reported honestly (a prompt to look, not a verdict) and read-only.
    """

    WALLET = "BOTwallet1111111111111111111111111111111111"
    CHATS = (8001, 8002, 8003)

    def _load(self, monkeypatch, tmp_path):
        mod = _load_reconcile_module()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config, "BOT_USDC_ADDRESS", self.WALLET)
        monkeypatch.setattr(config, "DEPOSIT_ADDRS_FILE",
                            tmp_path / "deposit_addrs.json")
        monkeypatch.setattr(config, "PRO_RECEIPTS_FILE",
                            tmp_path / "pro_receipts.json")
        monkeypatch.setattr(config, "DEPOSIT_SEED", "")
        monkeypatch.setattr(sys, "argv", ["payment_reconcile.py", "--audit"])
        pro._deposit_addrs.clear()
        saved = list(pro._receipts)
        pro._receipts.clear()

        async def _no_sleep(_seconds):
            return None

        monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

        async def _no_credit(*args, **kw):
            raise AssertionError("--audit must never credit anybody")

        monkeypatch.setattr(pro, "_activate_pro", _no_credit)
        return mod, saved

    def test_it_reports_what_no_receipt_explains(self, monkeypatch, tmp_path, capsys):
        from datetime import datetime as _dt
        mod, saved = self._load(monkeypatch, tmp_path)
        pro._receipts.extend([
            {"ts": 1758000000, "chat_id": self.CHATS[0], "amount": 0.010500,
             "token": "USDC"},
            {"ts": 1758100000, "chat_id": self.CHATS[1], "amount": 0.019000,
             "token": "USDC"},
            {"ts": 1758200000, "chat_id": self.CHATS[2], "amount": 0.500000,
             "token": "USDT"}])
        monkeypatch.setattr(pro, "_sol_rpc", TestDepositOperatorCommands._balance_rpc(
            {config.USDC_MINT: 0.044500, config.USDT_MINT: 2.000000}))
        try:
            assert asyncio.run(mod.main()) == 0
            out = capsys.readouterr().out
            assert "held 0.044500" in out and "credited 0.029500" in out
            assert "unexplained +0.015000" in out
            assert "held 2.000000" in out and "credited 0.500000" in out
            assert "unexplained +1.500000" in out
            assert "3 receipt(s) on file" in out
            assert f"earliest {_dt.fromtimestamp(1758000000):%Y-%m-%d}" in out
            assert "never credited" in out, "it must say what the number can mean"
            # A report ends up pasted somewhere: chat tags only, wallet masked.
            for chat_id in self.CHATS:
                assert str(chat_id) not in out, "the audit leaked a chat id"
            assert self.WALLET not in out, "the receiving wallet must be masked"
        finally:
            pro._receipts.clear()
            pro._receipts.extend(saved)


    def test_with_no_receipts_the_whole_balance_is_unexplained(self, monkeypatch,
                                                               tmp_path, capsys):
        """A wallet with money and an empty receipt trail is the loudest case."""
        mod, saved = self._load(monkeypatch, tmp_path)
        monkeypatch.setattr(pro, "_sol_rpc", TestDepositOperatorCommands._balance_rpc(
            {config.USDC_MINT: 0.020000}))
        try:
            assert asyncio.run(mod.main()) == 0
            out = capsys.readouterr().out
            assert "0 receipt(s) on file" in out
            assert "unexplained +0.020000" in out
        finally:
            pro._receipts.clear()
            pro._receipts.extend(saved)

    def test_it_reports_what_is_still_unswept_on_deposit_addresses(
            self, monkeypatch, tmp_path, capsys):
        """The other half of "what do we hold": money sitting on deposit addresses."""
        mod, saved = self._load(monkeypatch, tmp_path)
        monkeypatch.setattr(config, "DEPOSIT_SEED", "ab" * 32)
        address = pro.deposit_address(self.CHATS[0])
        pro._deposit_addrs[self.CHATS[0]] = {"address": address, "created": 1.0}

        async def fake_rpc(method, params):
            if method != "getTokenAccountsByOwner":
                return []                    # nothing to scan in this test
            if params[0] == self.WALLET or params[1]["mint"] != config.USDC_MINT:
                return {"value": []}
            return {"value": [{"account": {"data": {"parsed": {"info": {
                "tokenAmount": {"uiAmount": 0.013434}}}}}}]}

        monkeypatch.setattr(pro, "_sol_rpc", fake_rpc)
        try:
            assert asyncio.run(mod.main()) == 0
            out = capsys.readouterr().out
            assert "still on deposit addresses" in out
            assert "0.013434 USDC" in out
            assert "per-address detail: --sweep-deposits" in out
            assert str(self.CHATS[0]) not in out, "the audit leaked a chat id"
            assert address not in out, "the audit printed an address needlessly"
        finally:
            pro._receipts.clear()
            pro._receipts.extend(saved)
            pro._deposit_addrs.clear()


    def test_it_names_the_transfers_that_have_no_receipt(self, monkeypatch,
                                                         tmp_path, capsys):
        """The precise answer the balance comparison cannot give.

        0.017777 USDT in band with no receipt is a credit that never happened.
        Everything else must stay out of the list: an already-credited signature, an
        out-of-band amount (5 USDT is not a Pro payment), a failed transaction, and a
        transfer our own keys signed.
        """
        mod, saved = self._load(monkeypatch, tmp_path)
        pro._receipts.append({"ts": 1, "chat_id": self.CHATS[0], "amount": 0.010500,
                              "token": "USDC", "sig": "sig-already-credited"})
        ours = "OurOwnWallet111111111111111111111111111111"
        monkeypatch.setattr(config, "OPERATOR_WALLETS", frozenset({ours}))

        in_band = TestDepositAddresses._tx(self.WALLET, sent=0.017777)
        too_big = TestDepositAddresses._tx(self.WALLET, sent=5.0)
        ours_tx = TestDepositAddresses._tx(self.WALLET, sent=0.018000)
        ours_tx["transaction"]["message"]["accountKeys"][0]["pubkey"] = ours
        page = [{"signature": "sig-new", "blockTime": 1759000000},
                {"signature": "sig-already-credited", "blockTime": 1759000001},
                {"signature": "sig-too-big", "blockTime": 1759000002},
                {"signature": "sig-ours", "blockTime": 1759000003},
                {"signature": "sig-failed", "blockTime": 1759000004,
                 "err": {"InstructionError": [0, "x"]}}]
        by_sig = {"sig-new": in_band, "sig-already-credited": in_band,
                  "sig-too-big": too_big, "sig-ours": ours_tx}

        async def fake_rpc(method, params):
            if method == "getSignaturesForAddress":
                return page
            if method == "getTransaction":
                return by_sig.get(params[0])
            return {"value": []}

        monkeypatch.setattr(pro, "_sol_rpc", fake_rpc)
        try:
            assert asyncio.run(mod.main()) == 0
            out = capsys.readouterr().out
            assert "0.017777 USDT" in out and "sig-new" in out
            assert "no receipt" in out
            assert "credit one with: --sig" in out
            for skipped in ("sig-already-credited", "sig-too-big", "sig-ours",
                            "sig-failed"):
                assert skipped not in out, f"{skipped} must not be flagged"
            assert str(self.CHATS[0]) not in out
        finally:
            pro._receipts.clear()
            pro._receipts.extend(saved)

    def test_audit_flags_a_signature_credited_more_than_once(self, monkeypatch,
                                                             tmp_path, capsys):
        """The trail's integrity check: one payment must produce exactly one receipt.

        This is the cheapest check there is (no RPC) and it is the only one that
        cannot be rebuilt later — it is what surfaced this bot's own 2026-09-18
        incident, where a single 0.010042 transfer produced five receipts (across two
        chats) inside 27 minutes.
        """
        mod, saved = self._load(monkeypatch, tmp_path)
        for _ in range(3):
            pro._receipts.append({"ts": 1758000000, "chat_id": self.CHATS[0],
                                  "amount": 0.010042, "token": "USDT",
                                  "sig": "5sIgRepEaTeD" * 6})
        monkeypatch.setattr(pro, "_sol_rpc",
                            TestDepositOperatorCommands._balance_rpc({}))
        try:
            assert asyncio.run(mod.main()) == 0
            out = capsys.readouterr().out
            assert "credited more than once" in out
            assert "3 receipts" in out
            assert storage.mask_chat(self.CHATS[0]) in out, "the tag is actionable"
            assert str(self.CHATS[0]) not in out, "a chat id must never be printed"
        finally:
            pro._receipts.clear()
            pro._receipts.extend(saved)

    def test_audit_says_the_trail_is_clean_when_it_is(self, monkeypatch, tmp_path,
                                                      capsys):
        mod, saved = self._load(monkeypatch, tmp_path)
        pro._receipts.append({"ts": 1758000000, "chat_id": self.CHATS[0],
                              "amount": 0.010042, "token": "USDT",
                              "sig": "5sIgUnIqUe" * 6})
        monkeypatch.setattr(pro, "_sol_rpc",
                            TestDepositOperatorCommands._balance_rpc({}))
        try:
            assert asyncio.run(mod.main()) == 0
            out = capsys.readouterr().out
            assert "appears exactly once" in out
            assert "credited more than once" not in out
        finally:
            pro._receipts.clear()
            pro._receipts.extend(saved)

    def test_it_says_so_when_no_transfer_is_missing_a_receipt(self, monkeypatch,
                                                              tmp_path, capsys):
        mod, saved = self._load(monkeypatch, tmp_path)
        pro._receipts.append({"ts": 1, "chat_id": self.CHATS[0], "amount": 0.017777,
                              "token": "USDT", "sig": "sig-credited"})
        page = [{"signature": "sig-credited", "blockTime": 1759000000}]

        async def fake_rpc(method, params):
            if method == "getSignaturesForAddress":
                return page
            return {"value": []}

        monkeypatch.setattr(pro, "_sol_rpc", fake_rpc)
        try:
            assert asyncio.run(mod.main()) == 0
            out = capsys.readouterr().out
            assert "none — every in-band transfer into this wallet has a receipt" in out
        finally:
            pro._receipts.clear()
            pro._receipts.extend(saved)




def test_every_credit_writes_the_receipt_the_durable_dedup_relies_on():
    """_credited_signatures() is the durable half of exactly-once, so it is sound only
    while EVERY credit appends a receipt.

    That holds because there are exactly two sanctioned writers of the store,
    each with a narrow job: _activate_pro is the only place that GRANTS Pro (and
    it writes the receipt in the same breath), and reconcile_receipts only ever
    EXTENDS an expiry that a receipt in the durable trail already proved — a
    startup repair for a crash between the two writes (2026-10-04 audit). This
    test is what keeps it that way: a third grant site, a path that credits
    without a signature, or an early return between the grant and the receipt
    would each silently weaken the cross-restart dedup (a replay could then buy
    a second subscription).
    """
    import inspect as _inspect
    import re as _re

    writers = {}
    for name, obj in _inspect.getmembers(pro, _inspect.isfunction):
        count = len(_re.findall(r"(?m)^\s+_pro_users\[[^\]]+\]\s*=",
                                _inspect.getsource(obj)))
        if count:
            writers[name] = count
    assert writers == {"_activate_pro": 1, "reconcile_receipts": 1}, \
        f"unexpected writers of the Pro store: {writers}"

    activate = _inspect.getsource(pro._activate_pro)
    assert '"sig": sig' in activate, "the receipt must record the credited signature"
    grant_at = activate.index("_pro_users[chat_id] = ")
    receipt_at = activate.index("append_receipt(")
    assert grant_at < receipt_at, "the receipt must follow the grant"
    assert "return" not in activate[grant_at:receipt_at], \
        "a grant that can return before its receipt leaves the dedup blind"

    # No path can credit without a signature: the parameter is positional and has no
    # default, so omitting it is a TypeError rather than a silent no-trail credit.
    params = list(_inspect.signature(pro._activate_pro).parameters.items())
    assert params[3][0] == "sig", params
    assert params[3][1].default is _inspect.Parameter.empty, \
        "a default signature would let a path credit without naming the payment"


def test_no_test_fake_checks_the_paging_cursor_on_the_wrong_object():
    """The sharpest lesson in this file's history: a fake that ignores the paging
    cursor hands the same page back three times, and the test stays green while
    exercising one page of nothing. The cursor lives in params[1] (the params dict),
    never in the [address, params] list — this gate keeps the next fake honest.
    """
    import re as _re
    # Assembled so the pattern cannot match its own source line, and bounded so the
    # correct params[1] form (which starts with the wrong text) is not flagged.
    wrong = _re.compile('"before" not in ' + r"params(?!\[)")
    hits = [line.strip() for line in Path(__file__).read_text().splitlines()
            if wrong.search(line)]
    assert not hits, f"the cursor lives in params[1], not the argument list: {hits}"



def test_read_only_pro_load_never_reencrypts_the_file(tmp_path, monkeypatch):
    """--status's store probe calls pro.load_pro_state(). A plaintext pro file
    must load for counting without the load re-encrypting it on disk — that
    write belongs to the bot process, not to a status command."""
    monkeypatch.chdir(tmp_path)
    plaintext = json.dumps({"123456789": str(time.time() + 86400)})
    config.PRO_USERS_FILE.write_text(plaintext)
    monkeypatch.setattr(storage, "READ_ONLY", True)
    pro._pro_users.clear()
    try:
        pro.load_pro_state()
        assert pro._pro_users, "the plaintext file did not load at all"
    finally:
        pro._pro_users.clear()
    assert config.PRO_USERS_FILE.read_text() == plaintext, \
        "the read-only load rewrote the file on disk"
