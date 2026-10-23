"""payment_reconcile.py — manually credit a Solana payment the monitor missed.

Why this exists: the invoice pools were memory-only, so a restart discarded
every open invoice. A buyer who paid the exact amount they were shown then
matched nothing, and got no Pro and no receipt. The pools are persisted now
(pro.save_pending_payments), but a payment that already fell through that hole
still needs crediting — and a paid customer must never be told to pay again.
This script reads the transfer from the chain and runs the SAME production path
the monitor uses (pro._activate_pro), so the receipt line and the Telegram
welcome message are identical to an automatic activation.

Read-only by default: --scan and a bare --sig only report. Crediting requires
--apply, and --apply refuses to run while the bot is up, because the bot holds
pro_users in memory and would overwrite the credit on its next save; it also
refuses an amount that has already been credited (re-running it would stack a
second subscription — pass --force for a genuinely new payment). Safe order:
stop the bot -> --apply -> start the bot.

Usage (never prints the key, the token or a full tracking number):
  python3 scripts/payment_reconcile.py --scan
  python3 scripts/payment_reconcile.py --sig <signature> --chat <chat_id>
  python3 scripts/payment_reconcile.py --sig <signature> --chat <chat_id> --apply
  python3 scripts/payment_reconcile.py --notify --chat-tag chat:d8393378
  python3 scripts/payment_reconcile.py --sweep-deposits
  python3 scripts/payment_reconcile.py --deposit-secret --chat-tag chat:d8393378
  python3 scripts/payment_reconcile.py --audit [N]

The last two are about the per-chat deposit addresses (the manual/exchange payment
path). Money only ever moves by hand in v1: --sweep-deposits reports what is sitting
on those addresses, --deposit-secret prints ONE address's importable key so the
operator can sweep it in a wallet. Neither ever moves funds itself.
"""
import argparse
import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (loads .env first, like every entry point)
import pro  # noqa: E402
import storage  # noqa: E402


def _bot_running() -> str:
    """The command line of a live bot process, or '' when none is running."""
    try:
        out = subprocess.run(["pgrep", "-fl", "main.py"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except Exception:
        return ""
    for line in out.splitlines():
        if "payment_reconcile" in line:
            continue
        if "python" in line and "main.py" in line:
            return line.strip()
    return ""


def _invoice_shaped(micros: int) -> bool:
    """Is this amount one we could have issued (a price base + a 4-digit tail)?

    Every base still in play counts, not just today's price: after a price change an
    invoice created at the old price is still an invoice we issued.
    """
    return any(base <= micros <= base + pro._TAIL_MAX
               for base in pro._active_price_bases())


def _in_price_band(micros: int) -> bool:
    """Is this amount plausibly a payment for this product, exact or rounded?

    The monitor uses the same predicate (pro._in_price_band) for transfers into a
    deposit address, so it is defined once, next to the payment code — two copies
    of a price guard is how an operator's credit and the monitor's drift apart.
    """
    return pro._in_price_band(micros)


def _resolve_chat_tag(tag: str):
    """Turn a masked log tag (chat:d8393378) into the chat ID — in memory only.

    Logs and support reports carry the tag, never the ID, so an operator chasing
    an incident has "chat:d8393378" and needs the chat behind it. The tag is a
    keyed hash, so the only way back is to hash the candidates: every chat the
    bot already knows about. The ID is used for the credit and never printed.
    """
    storage.load_data(config.DATA_FILE)
    candidates = set(storage.track_sessions) | set(pro._pro_users)
    wanted = tag if tag.startswith("chat:") else f"chat:{tag}"
    hits = [cid for cid in candidates if storage.mask_chat(cid) == wanted]
    print(f"  tag {wanted}: {len(hits)} match(es) among {len(candidates)} known chat(s)")
    return hits[0] if len(hits) == 1 else None


def _last_receipt_for(chat_id):
    """(amount, mint, sig, payment_id) from the encrypted receipt trail.

    Reads through pro.load_receipts(), so a legacy plaintext pro_receipts.log is
    migrated (and removed) the first time this runs.
    """
    pro.load_receipts()
    entry = pro.last_receipt_for(chat_id)
    if not entry:
        return None, None, None, None
    mint = next((m for m, n in config.ACCEPTED_MINTS.items()
                 if n == entry.get("token")), "")
    return (entry.get("amount"), mint, entry.get("sig", ""),
            entry.get("payment_id", ""))


async def _notify(target_chat, sig: str) -> int:
    """Re-send a buyer's Pro receipt — never touches their subscription.

    Safe to run while the bot is up: this only sends a message, so there is no
    pro_users.json write to race with. The point is a buyer who says "I paid but
    got no message" — re-send the receipt instead of asking them to pay again.
    """
    pro.load_pro_state()
    now = time.time()
    expiry = pro._pro_users.get(target_chat, 0.0)
    if expiry <= now:
        print("that chat has no active Pro subscription — nothing to re-send")
        return 2

    amount = mint = payment_id = None
    if sig:
        tx = await pro._sol_rpc("getTransaction", [
            sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        info = pro._tx_payment_info(tx) if tx else None
        if info:
            amount, mint, _ = info
            payment_id = f"manual-{sig[:12]}"
        else:
            print("  note: that signature has no readable transfer; falling back "
                  "to the receipt on file")
    if amount is None:
        amount, mint, sig, payment_id = _last_receipt_for(target_chat)
    if amount is None:
        print("no receipt on file for that chat — pass --sig to name the payment")
        return 2

    token_env = (os.getenv("TELEGRAM_BOT_API_TOKEN") or "").strip()
    if not token_env:
        print("no bot token in the environment — cannot send")
        return 2
    from aiogram import Bot
    from aiogram.enums import ParseMode
    import botstate
    bot = Bot(token=token_env)
    botstate.set_bot(bot)
    try:
        await bot.send_message(
            target_chat,
            pro.pro_welcome_text(amount, mint, sig or "", payment_id, "manual",
                                 resent=True, activated_ts=now, expiry_ts=expiry),
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        print(f"send failed: {type(e).__name__}: {e}")
        return 1
    finally:
        await bot.session.close()
    print("✅ receipt re-sent (subscription untouched)")
    return 0


async def _describe(sig: str, tries: int = 3) -> None:
    """Report what a signature contains, retrying through RPC rate limits.

    The public RPC answers getTransaction with 429 under any real load, so a
    single attempt reports "unreadable" for transactions that are perfectly
    readable a second later — which is exactly how a payment gets missed.
    """
    tx = None
    for attempt in range(tries):
        tx = await pro._sol_rpc("getTransaction", [
            sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        if tx:
            break
        await asyncio.sleep(0.8 * (attempt + 1))
    if not tx:
        print(f"  {sig[:16]}…  unreadable after {tries} attempts (RPC rate limit)")
        return
    if pro._signed_by_us(tx):
        # The sweep the operator performs by hand lands here: it is our own money
        # moving, so "no accepted transfer into the wallet" would be misleading.
        print(f"  {sig[:16]}…  signed by one of our own wallets "
              "(sweep/consolidation) — not a payment, nothing to credit")
        return
    info = pro._tx_payment_info(tx)
    if not info:
        balances = (tx.get("meta") or {}).get("postTokenBalances") or []
        print(f"  {sig[:16]}…  no accepted stablecoin transfer into the wallet "
              f"({len(balances)} token balance(s) seen)")
        return
    amount, mint, memo = info
    micros = pro.to_micros(amount)
    token = config.ACCEPTED_MINTS.get(mint, mint)
    found = pro._match_payment(micros, memo)
    where = (f"matches invoice {found[0]}" if found
             else "NO open invoice matches this amount")
    shaped = "invoice-shaped" if _invoice_shaped(micros) else "NOT invoice-shaped"
    print(f"  {sig[:16]}…  {amount:.6f} {token}  {shaped}  {where}")


async def _deposit_balances(address: str) -> dict:
    """USDC/USDT balances on one address — the monitor's own reader.

    Delegated rather than reimplemented: the report and the balance watch must never
    disagree about what an address holds, and the parsing lives in one place.
    """
    return await pro._token_balances(address)


async def _sweep_deposits() -> int:
    """Read-only: what is sitting on the per-chat deposit addresses right now.

    No auto-sweep in v1 on purpose — moving customer money is a decision, not a
    background task. This reports the balances and the consolidatable total and
    stops there; the sweep itself is a manual wallet import (--deposit-secret).

    Deliberately NOT gated on DEPOSIT_SEED: the registry holds the addresses and the
    chain holds the balances, so this is exactly the report that must keep working
    when the seed is missing — that is when the money is stranded and the operator
    needs to see how much. Only exporting a key needs the seed.
    """
    if not pro._deposit_addrs:
        print("No deposit address has been handed out yet.")
        return 0
    if pro.deposit_seed_bytes() is None:
        print("NOTE: DEPOSIT_SEED is not set, so no deposit key can be exported from "
              "this machine. The balances below are real and still sweepable — but "
              "only with the seed that derived them. Do not discard that seed.")
    print(f"\n{len(pro._deposit_addrs)} deposit address(es):")
    totals = {name: 0.0 for name in config.ACCEPTED_MINTS.values()}
    for chat_id, record in sorted(pro._deposit_addrs.items()):
        address = str(record.get("address", ""))
        balances = await _deposit_balances(address)
        for name, amount in balances.items():
            totals[name] += amount
        held = "  ".join(f"{a:.6f} {n}" for n, a in balances.items())
        # The log-safe tag, not the chat id: a report gets pasted around.
        print(f"  {storage.mask_chat(chat_id)}  {address}  {held}")
        # The public RPC answers 429 to back-to-back calls.
        await asyncio.sleep(0.4)
    print("\nconsolidatable total: "
          + "  ".join(f"{a:.6f} {n}" for n, a in totals.items()))
    print("nothing was moved — sweeping is a manual step:")
    print("  --deposit-secret --chat-tag <tag>   (prints one address's key)")
    return 0


async def _uncredited_inbound(limit: int = 15) -> list:
    """Inbound transfers in the newest `limit` signatures that no receipt covers.

    The balance-vs-receipts difference cannot tell a missed credit from money that
    was never a Pro payment — and this wallet carries other traffic, where that
    difference is dominated by unrelated inflow. This can tell: a transfer into the
    wallet whose amount sits inside the price band, and whose signature appears in
    no receipt, is a credit that never happened.

    Transfers our own keys signed are excluded automatically (they are not payments
    at all), so a sweep of a deposit address never shows up as a false alarm — and
    an undeclared operator top-up shows up as exactly what it looks like, a payment
    nobody credited, which is the cue to list that wallet in OPERATOR_WALLETS.
    """
    receipts = {str(entry.get("sig")) for entry in pro._receipts}
    findings = []
    sigs = await pro._sol_rpc("getSignaturesForAddress",
                              [config.BOT_USDC_ADDRESS, {"limit": limit}])
    if not isinstance(sigs, list):
        return findings                      # unreadable/rate-limited: report nothing
    for entry in sigs:
        sig = str(entry.get("signature"))
        if not sig or entry.get("err") or sig in receipts:
            continue
        tx = await pro._sol_rpc("getTransaction", [
            sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        if not tx:
            continue
        info = pro._tx_payment_info(tx)
        if not info:
            continue                      # not a payment, or one of ours
        amount, mint, _memo = info
        if pro._in_price_band(pro.to_micros(amount)):
            findings.append((entry.get("blockTime"), amount,
                             config.ACCEPTED_MINTS.get(mint, mint), sig))
        # The public RPC answers 429 to back-to-back calls.
        await asyncio.sleep(0.4)
    return findings


async def _audit(limit: int = 15) -> int:
    """Read-only: does the chain agree with what we credited?

    Nothing ever leaves the receiving wallet, so its balance only grows and every
    unit of it must be explainable: every credit writes a receipt, and the receipt
    trail is complete. So

        balance ≈ Σ receipts + sweeps from deposit addresses + your own top-ups

    Anything left over is inbound that no receipt accounts for — and the case worth
    catching is a payment that arrived and was never credited. The number cannot
    name which it was: a sweep and a top-up are both invisible here, by design
    (nothing is signed on this machine). Treat it as a prompt to look, not a verdict.
    """
    sink = config.BOT_USDC_ADDRESS
    # Accounting integrity first, because it is free (no RPC) and it is the one thing
    # that cannot be reconstructed later: one signature must appear at most once. A
    # repeat means a single payment was credited more than once — measured on this
    # bot's own trail: a 0.010042 transfer produced five receipts inside 27 minutes
    # on 2026-09-18, before the amount and invoice guards existed. Every credit path
    # now refuses a spent signature, so anything printed here is historical.
    seen: dict = {}
    for entry in pro._receipts:
        sig = str(entry.get("sig") or "")
        if not sig:
            continue
        record = seen.setdefault(sig, {"count": 0, "tags": set()})
        record["count"] += 1
        if entry.get("chat_id"):
            record["tags"].add(storage.mask_chat(int(entry["chat_id"])))
    repeated = {sig: rec for sig, rec in seen.items() if rec["count"] > 1}
    if repeated:
        print(f"\n⚠️  {len(repeated)} signature(s) credited more than once "
              f"(a past double-credit — nothing here can re-credit today):")
        for sig, rec in repeated.items():
            print(f"  sig {sig[:16]}…  {rec['count']} receipts  ·  "
                  f"chat tags: {', '.join(sorted(rec['tags'])) or '—'}")
    else:
        print("\nreceipt trail: every credited signature appears exactly once")

    held = await _deposit_balances(sink)
    credited = {name: 0.0 for name in config.ACCEPTED_MINTS.values()}
    for entry in pro._receipts:
        name = str(entry.get("token", ""))
        if name in credited:
            credited[name] += float(entry.get("amount") or 0)
    print(f"\nreceiving wallet {sink[:6]}…{sink[-4:]}")
    for name, total in credited.items():
        print(f"  {name}:  held {held.get(name, 0.0):.6f}"
              f"  ·  credited {total:.6f}"
              f"  ·  unexplained {held.get(name, 0.0) - total:+.6f}")
    print(f"\n{len(pro._receipts)} receipt(s) on file"
          + (f", earliest {datetime.fromtimestamp(pro._receipts[0]['ts']):%Y-%m-%d}"
             if pro._receipts and pro._receipts[0].get("ts") else ""))
    print("unexplained = sweeps from deposit addresses + your own top-ups + any "
          "payment that arrived and was never credited")
    if pro._deposit_addrs:
        unswept = {name: 0.0 for name in config.ACCEPTED_MINTS.values()}
        for _chat_id, record in sorted(pro._deposit_addrs.items()):
            address = str(record.get("address", ""))
            if not address:
                continue
            for name, amount in (await _deposit_balances(address)).items():
                unswept[name] += amount
            # The public RPC answers 429 to back-to-back calls.
            await asyncio.sleep(0.4)
        print(f"\nstill on deposit addresses ({len(pro._deposit_addrs)} address(es), "
              "not yet swept): "
              + "  ".join(f"{a:.6f} {n}" for n, a in unswept.items()))
        print("  per-address detail: --sweep-deposits")
    print("\nthe headline is the other way round: a Pro payment is at most "
          f"${((int(round(config.PRO_PRICE_USD * 1_000_000)) + pro._TAIL_MAX) // 10_000 + 1) / 100:.2f}, "
          "so a large difference is unrelated traffic, not thousands of missed "
          "credits. Scanning signatures is what answers it:")

    findings = await _uncredited_inbound(limit)
    print(f"\ninvoice-shaped inbound with no receipt (newest {limit} signature(s)):")
    if not findings:
        print("  none — every in-band transfer into this wallet has a receipt")
    for when, amount, token, sig in findings:
        stamp = (datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")
                 if when else "unknown time")
        print(f"  {stamp}  {amount:.6f} {token}  sig {sig[:16]}…  (no receipt)")
    if findings:
        print("  credit one with: --sig <signature> --chat <id> --apply")
    return 0


def _export_deposit_secret(chat_id: int) -> int:
    """Print ONE deposit address's importable key, to the terminal only.

    The single sanctioned way for a key to leave this machine: it needs an explicit
    --chat-tag, it writes to stdout (never a log line, never a file), and what it
    prints controls that one address — the master DEPOSIT_SEED stays put.
    """
    keypair = pro.deposit_keypair_b58(chat_id)
    if keypair is None:
        print("DEPOSIT_SEED is not set — there is no deposit key to export.")
        return 2
    print(f"\ndeposit address : {pro.deposit_address(chat_id)}")
    print(f"private key     : {keypair}")
    print("\nImport it in a Solana wallet (Phantom → Add account → Import private "
          "key) and sweep the balance. Do not paste it into a chat or a log.")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sig", help="transaction signature to inspect")
    ap.add_argument("--chat", type=int, help="chat ID to credit (needed with --apply)")
    ap.add_argument("--chat-tag", dest="chat_tag",
                    help="masked chat tag from the logs (e.g. chat:d8393378) — "
                         "resolved in memory, never printed")
    ap.add_argument("--scan", nargs="?", type=int, const=15, default=None,
                    metavar="N", help="report the last N signatures (default 15)")
    ap.add_argument("--apply", action="store_true",
                    help="actually activate Pro (writes pro_users.json + receipt)")
    ap.add_argument("--notify", action="store_true",
                    help="re-send the Pro receipt to a chat WITHOUT changing the "
                         "subscription (safe while the bot runs)")
    ap.add_argument("--force", action="store_true",
                    help="allow --apply while the bot runs, for an amount that "
                         "is not invoice-shaped, or for an amount that has "
                         "already been credited")
    ap.add_argument("--sweep-deposits", dest="sweep_deposits", action="store_true",
                    help="read-only: balances on every per-chat deposit address, "
                         "plus the consolidatable total (moves nothing)")
    ap.add_argument("--deposit-secret", dest="deposit_secret", action="store_true",
                    help="print ONE deposit address's importable private key to the "
                         "terminal so it can be swept by hand (needs --chat-tag; "
                         "never logged)")
    ap.add_argument("--audit", nargs="?", type=int, const=15, default=None,
                    metavar="N",
                    help="read-only: the receiving wallet's balance vs the receipts "
                         "we credited, plus the newest N signatures (default 15) "
                         "checked for in-band transfers that have no receipt")
    args = ap.parse_args()

    # Load before anything is printed or decided: the header, the pools and the
    # deposit registry must describe what is on disk, not the empty process they
    # start in. (The header used to report "0 open invoice(s)" for exactly that
    # reason, which made every per-transfer verdict below meaningless.)
    pro.load_pro_state()

    # These two never touch the shared wallet, so they work even without it.
    if args.sweep_deposits:
        return await _sweep_deposits()
    if args.deposit_secret:
        if not args.chat_tag:
            print("--deposit-secret needs --chat-tag <tag>: one address at a time, "
                  "and the chat id never has to be typed out.")
            return 2
        target = _resolve_chat_tag(args.chat_tag)
        if target is None:
            print("that tag matched no known chat — check it against the logs.")
            return 2
        return _export_deposit_secret(target)

    if not config.BOT_USDC_ADDRESS:
        print("BOT_USDC_ADDRESS is not set — nothing to reconcile.")
        return 2
    wallet = config.BOT_USDC_ADDRESS[:6] + "…" + config.BOT_USDC_ADDRESS[-4:]
    deposits = (f"{len(pro._deposit_addrs)} deposit address(es)"
                if pro.deposit_seed_bytes() else "deposit addresses off")
    print(f"wallet {wallet} · base price ${config.PRO_PRICE_USD:.2f} · "
          f"{len(pro._pending_payments)} open invoice(s), "
          f"{len(pro._expired_payments)} in the grace pool · {deposits}")

    if args.audit is not None:
        return await _audit(args.audit)

    if args.notify:
        target = args.chat
        if target is None and args.chat_tag:
            target = _resolve_chat_tag(args.chat_tag)
        if target is None:
            print("\n--notify needs --chat <id> or --chat-tag <tag>")
            return 2
        return await _notify(target, args.sig or "")

    if args.scan is not None:
        sigs = await pro._sol_rpc("getSignaturesForAddress",
                                  [config.BOT_USDC_ADDRESS, {"limit": args.scan}])
        if not sigs:
            print("RPC returned no signatures (rate limit?) — try again.")
            return 1
        print(f"\nlast {len(sigs)} signature(s):")
        for s in sigs:
            sig = str(s.get("signature"))
            when = s.get("blockTime")
            stamp = (datetime.fromtimestamp(when).strftime("%m-%d %H:%M")
                     if when else "unknown time")
            if s.get("err"):
                print(f"  {stamp}  {sig[:16]}…  failed on-chain")
                continue
            print(f"  {stamp}  ", end="")
            await _describe(sig)
            # The public RPC answers getTransaction with 429 when asked too
            # quickly; spacing the calls is what makes a scan readable at all.
            await asyncio.sleep(0.4)
        if not args.sig:
            return 0

    if not args.sig:
        return 0

    print(f"\ntransaction {args.sig[:16]}…:")
    tx = await pro._sol_rpc("getTransaction", [
        args.sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
    if not tx:
        print("  unreadable — the public RPC may be rate-limiting. Retry shortly.")
        return 1
    if pro._signed_by_us(tx):
        print("  this transaction was signed by one of our own wallets (a sweep or "
              "consolidation), so there is nothing to credit — it is our own money "
              "moving, not a customer paying.")
        return 1
    info = pro._tx_payment_info(tx)
    if not info:
        print("  this transaction holds no accepted USDC/USDT transfer into the "
              "bot wallet, so there is nothing to credit.")
        return 1
    amount, mint, memo = info
    micros = pro.to_micros(amount)
    token = config.ACCEPTED_MINTS.get(mint, mint)
    print(f"  amount: {amount:.6f} {token} ({micros} micros)")
    print(f"  memo:   {memo or '(none)'}")
    print(f"  shape:  "
          f"{'invoice-shaped' if _invoice_shaped(micros) else 'rounded / not exact'}")

    target_chat = args.chat
    if target_chat is None and args.chat_tag:
        target_chat = _resolve_chat_tag(args.chat_tag)

    if not args.apply:
        if target_chat is not None:
            print("\ndry run — target chat resolved. Re-run with --apply.")
        else:
            print("\ndry run — add --chat <id> (or --chat-tag <tag>) and --apply.")
        return 0

    if target_chat is None:
        print("\n--apply needs --chat <id> (or --chat-tag <tag>) — the buyer's chat.")
        return 2
    if not _in_price_band(micros) and not args.force:
        print("\nrefusing: the amount is outside this product's price band, so it is "
              "not a subscription payment. Pass --force if you are certain.")
        return 2
    running = _bot_running()
    if running and not args.force:
        print(f"\nrefusing: the bot is running (pid {running.split()[0]}). It holds "
              "pro_users in memory and would overwrite this credit on its next "
              "save. Stop it first, or pass --force.")
        return 2

    # The same path the monitor uses, so the receipt and welcome message match.
    token_env = (os.getenv("TELEGRAM_BOT_API_TOKEN") or "").strip()
    if token_env:
        from aiogram import Bot
        import botstate
        botstate.set_bot(Bot(token=token_env))
        print("  telegram: the welcome message will be sent to the buyer")
    else:
        print("  telegram: no bot token in the environment — the credit is "
              "written, no message is sent")

    # State was already loaded at the top of main(), before this path could retire
    # or activate anything (see the ordering test in tests/test_payments.py).
    # An amount already in the consumed set has been credited once already: this
    # is the same payment re-run (or a duplicate transfer), and crediting it again
    # would stack a second subscription period. --force covers the one honest
    # case where an amount is reused for a genuinely new payment.
    micros = pro.to_micros(amount)
    if micros in pro._used_amounts and not args.force:
        print(f"\nrefusing: {amount:.6f} has already been credited (it is in the "
              "consumed set), so this would stack a second subscription. Pass "
              "--force if this really is a new payment reusing that amount.")
        return 2
    # Retire what this credit pays for, exactly as the monitor does before it
    # activates: otherwise the invoice stays matchable and the next monitor pass
    # — which rescans the newest signatures after the restart this script tells
    # you to do — credits the same transfer a second time.
    retired = pro.retire_credited_amount(micros)
    await pro._activate_pro(target_chat, amount, mint, args.sig,
                            payment_id=f"manual-{args.sig[:12]}",
                            matched_by="manual")
    print(f"\n✅ credit written · {retired} invoice(s) retired · receipt appended "
          "to the encrypted receipt trail")
    print("   restart the bot so the running process picks up the new Pro state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

