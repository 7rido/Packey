# Project Rules — GLMTrackingbot (Telegram Package Tracker)

## Project overview
- Telegram bot tracking packages via 17TRACK / headless-browser USPS /
  Ship24 (paid fallback), with a Telegram Mini App (webapp/). Python >= 3.9,
  aiogram 3.x, httpx, python-dotenv, cryptography.
- Storage is local JSON: track_data.json (encrypted), pro_users.json,
  refresh_limits.json, register_cooldown.json, pending_payments.json (open
  Solana invoices — encrypted). No database.
- This is LIVE, running code used by real users. Assume the bot process is
  running right now.

## Project layout
- The module extraction (EXTRACTION_PLAN.md, Sessions 1–8) is DONE: main.py is
  ~1,900 lines and re-exports the extracted modules' names, so every call site
  and test still addresses main.<name>.
- Edit ONLY these modules (line counts at 2026-09-23):
  * main.py (~1,900) — aiogram 3 handlers, commands menu, /help, startup
  * config.py (~265) — every env-driven constant + the .env load
  * storage.py (~590) — encryption, per-chat envelopes, JSON persistence
  * carriers.py (~1,380) — carrier detection, 17TRACK, the USPS tiers (API v3 /
    Web Tools / browser), OnTrac, and the fetch_track_status dispatcher +
    source routing
  * refresh.py (~990) — poller, adaptive cadence, browser sweep, quota watch
  * pro.py (~1,530) — Pro tiers, Solana payments, receipts, intl allowance
  * miniapp.py (~1,200) — Mini App server + API, invoice views
  * solana_tx.py (~215, added 2026-09-24) — hand-rolled unsigned Solana
    transaction bytes for the wallet-scoped Pro payment (no SDK, no keys, no
    network: base58/shortvec + the legacy message layout)
  * phantom_link.py (~205, added 2026-09-24) — Phantom deep-link v1 requests
    (connect + signTransaction) over X25519/NaCl boxes, plus the in-memory
    per-invoice session; phantom_page.py (~185) — the /pay/<id>/phantom page
  * ui.py (~980) — shared card/text rendering (bot + Mini App)
  * ship24.py (~280) — paid last-resort client (daily cap + pause logic)
  * delivery_history.py (~115), share_links.py (~155)
  * usps_api.py (~140) — USPS OAuth API v3 client; usps_browser.py (~960) —
    headless browser tier (playwright stays an OPTIONAL lazy import)
  * botstate.py (~20) — the single live Bot handle
  * webapp/ (index.html + vendor/) — Mini App; tests/ (pytest)
- tests/test_code_hygiene.py (pyflakes over the app modules) and
  tests/test_config_surface.py (.env.example <-> code, requirements.txt) are
  gates, not suggestions — a new env knob must be documented in .env.example
  or the suite fails.
- NEVER read or edit scripts/archive/. scripts/*.py are manual probe scripts,
  not pytest tests. (The historical snapshot_2026-09-13_evening/ copy was
  deleted on 2026-09-27: it held a plaintext .env and a pre-encryption
  track_data.json, so it was a standing plaintext exposure rather than a
  rollback point worth keeping.)
- The project is NOT a git repo — be extra careful: no destructive edits,
  always show diffs clearly, suggest a backup before big changes.

## Secrets and privacy (hard rules)
- Plaintext on disk is limited to .env and *.log — both are never opened,
  printed or quoted (tracker.log* holds users' tracking numbers and chat IDs).
- Every *.json data file here is private: it holds chat IDs, tracking numbers,
  share tokens, deposit addresses or invoices. storage._write_protected keeps
  them encrypted at rest and mode 0600, but that is NOT a licence to quote one:
  with no encryption key available the same call writes plaintext, and a file
  from before encryption stays plaintext until its next write. Never weaken or
  bypass the encryption, and never write a plaintext version. The names below
  are examples, not the whole set: track_data.json + every track_data.* backup
  (corrupt / repaired / v1.bak hold the same data), pro_users.json,
  refresh_limits.json, register_cooldown.json, pending_payments.json,
  pro_receipts.json, deposit_addrs.json, share_links.json,
  delivery_history.json, mini_app_url.json, ship24_counter.json,
  intl_registrations.json.
- Never decrypt one just to look at it — not by opening the file, not by
  calling storage._read_protected from a scratch script, not by printing it
  into a chat, a report or a log. A repo-wide grep must not surface their
  contents either: search the code, not the data.
- For config structure, read .env.example instead.
- Never hardcode or echo tokens, API credentials, or chat IDs.

## Running and testing
- Only ONE bot instance may poll Telegram at a time (run.sh kills stale
  ones). Do NOT start main.py or run.sh yourself; the user runs the bot.
- Run tests from the project root: python3 -m pytest tests/ -q
- After edits: (1) python3 -m py_compile on changed files, (2) pytest,
  before reporting done.
- Do not restart or kill the user's bot process without being asked.

## Coding conventions
- Standard library + aiogram 3 + httpx + cryptography (+ pynacl, added 2026-09-24
  with the operator's approval for the Phantom deep-link route: X25519 +
  XSalsa20-Poly1305, which cryptography does not provide). Ask before
  adding any dependency.
- Async everywhere (httpx.AsyncClient, aiogram 3 handlers). No requests,
  no blocking sleeps inside handlers.
- Read line ranges, not whole files (main.py ~1,900 lines, carriers.py ~1,380).
  EXTRACTION_PLAN.md records what was already extracted and why — consult it
  before large changes, and keep the import graph acyclic: no extracted module
  may import main (inject a callback or share a module like botstate.py).
  New features go in clearly-marked sections with related helpers nearby; if a
  feature exceeds ~150 lines, propose extracting a module FIRST.
- Bot text style: emoji status colors 🟢🟣🔵🟠🟡🔶🔴⚪, concise; every new
  command gets a commands-menu registration and a /help entry.
- Mini App (webapp/index.html) changes must match the existing card/button
  style and status color legend.
- Comments explain WHY, not what.

## Change discipline
- Minimal, focused changes; no drive-by refactors or reformatting.
- Preserve confirmation flows for destructive actions (remove/purge/clear)
  and the existing mute/pro-user/refresh-limit behavior.
- track_data.json schema changes must load old files backward-compatibly.
- For any change touching main.py's core state or encryption, present a
  plan before editing.
