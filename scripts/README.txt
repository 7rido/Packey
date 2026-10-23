Kept diagnostics. Headless now works reliably (plain-UA scrub defeated Akamai's
brand check), so the old probe zoo lives in archive/ for history.

  dbg_tid.py            USPS widget probe (debug)
  test_headful.py       visible-window probe (historical; ship uses headless)
  probe_17track.py      live 17TRACK register + lookup probe
  ship24_probe.py       live Ship24 auth + tracker probe (source evaluation)
  probe_carrier_browser.py  can a headless Chrome read UPS/FedEx/DHL? (verdict + batch test)
  payment_reconcile.py  credit a Solana payment the monitor missed (scan / dry-run / --apply / --notify)
  backup_state.sh       snapshot + verify the state files and .env (daily via com.packey.backup.plist)
  test_handlers.py      offline drive of real /status /list /info handlers
  archive/              historical probes + check_17track_key.py

Missed-payment runbook (payment_reconcile.py):
  1. python3 scripts/payment_reconcile.py --scan        # find the payment, copy its signature
  2. Stop the bot (--apply refuses while it runs).
  3. python3 scripts/payment_reconcile.py --sig <signature> --chat <chat_id> --apply
  4. ./run.sh --background                              # start the bot again
  Notes: angle brackets are placeholders — paste real values, no brackets.
  --apply also refuses an already-credited amount or an out-of-band amount;
  --force overrides (rare). --notify re-sends a receipt without touching the
  subscription and is safe while the bot runs.
