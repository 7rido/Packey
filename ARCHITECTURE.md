# ARCHITECTURE.md — the map of glm-tracker

One paragraph per module: what it owns, what it must never grow into.
This file is the "where does X live?" answer key — update it when a module's
job changes, not when its contents shuffle.

Last full review: 2026-09-29.

---

## The one-paragraph overview

Packey is a Telegram bot that tracks parcels across multiple independent
sources (17TRACK, official USPS APIs, a headless-browser tier, OnTrac scrape,
Ship24 as paid fallback), pushes proactive notifications (Porch Guard),
serves a Telegram Mini App dashboard, and sells a Pro tier settled in
Solana USDC/USDT. Single operator, single process, JSON state encrypted at
rest — deliberately no database.

## Module map

| Module | Owns | Must never grow into |
|---|---|---|
| `main.py` (~1,900) | Composition + aiogram 3 handlers: commands, menus, /help, /privacy, startup, re-exports for compat | Business logic — handlers call out, decide little |
| `config.py` (~290) | Every env-driven constant + .env load + `__all__` | Runtime behavior; secrets printing |
| `storage.py` (~590) | Envelope encryption (DEK per chat), atomic 0600 writes, per-chat locks, confirmation store, chat masking | Carrier or tier logic |
| `carriers.py` (~1,380) | Number-format detection, the six source clients, the routing matrix, quota accounting, `fetch_track_status` | Telegram/HTTP-surface concerns |
| `refresh.py` (~990) | The poller, cadence rules, browser sweep, quota watch, Porch Guard pings, webhook acceleration (`mark_due_now`) | Payment or UI logic |
| `pro.py` (~1,530) | Tiers (free/pro/owner), intl allowance tally + gate, Solana payment engine, receipts, admin alerts | Delivery/routing concerns |
| `miniapp.py` (~1,200) | aiohttp server: initData validation, `/api/*`, static serving, the 17TRACK webhook receiver | Carrier clients |
| `solana_tx.py` (~215) | Hand-rolled unsigned Solana tx bytes (base58/shortvec, legacy message layout) | Any network/key usage |
| `phantom_link.py` + `phantom_page.py` | Phantom deep-link sessions (X25519/NaCl) + the /pay Phantom page | Payment matching logic (that's pro.py) |
| `ui.py` (~980) | Shared card/text rendering (bot + Mini App), the OCR number-gate + `parse_track_args` | Source clients, persistence |
| `ship24.py` (~280) | Paid last-resort client, daily cap + pause | Tier gating decisions (asks pro) |
| `usps_api.py` (~140) | USPS OAuth API v3 client (template: thin async client, token cache) | Routing policy |
| `usps_browser.py` (~1,100) | The spy browser: profile copy, stealth, per-carrier page handlers, json-watch, batch/sweep budgets | Scheduling (refresh decides when) |
| `delivery_history.py` (~115) | The delivery ledger (training data for future predictions) — nothing user-visible reads it yet | Anything user-facing |
| `share_links.py` (~155) | Mint/revoke share tokens | Rendering |
| `botstate.py` (~20) | The live Bot handle + admin notify | Anything else — it is the cycle-breaker shim |
| `webapp/index.html` | The Mini App client (single file, inline JS) | Server logic — it renders, never decides |

## "Where does X live?" — the lookup table

| Question | Answer |
|---|---|
| Where is a package added? | `main._add_packages` (bot) / `miniapp._api_add` (Mini App) — both go through the same lock, limits, and registration |
| Package / intl limits + messages? | `pro.check_package_limit`, `pro.check_intl_limit` (limits: `config`) |
| The intl tally (used out of X)? | `pro._intl_registrations` + `record_intl_registration` (idempotent per period); period math in `_intl_period_end` |
| When packages get checked? | `refresh` — `CADENCE` (per status, pro/free), `next_due_in`, `_apply_lookup` (holds the Delivered-downgrade guard) |
| Delivery pings? | `refresh` Porch Guard (`_pending_porch_pings`) — muted packages still ping; global quiet hours do NOT exist yet |
| 17TRACK webhook? | Route + receiver in `miniapp.py` (`/webhook/17track/<WEBHOOK_TOKEN>`); registered manually in 17TRACK's console; capture-first 200 + anti-spoof (only stored numbers actionable) |
| Payments? | `pro.py` (invoices, matching, receipts) + `solana_tx.py` (tx bytes) + `phantom_*` (signing UX) |
| Which source answers a lookup? | `carriers.fetch_track_status` → `_use_17track` / routing matrix (`_17TRACK_ONLY_SLUGS`, `_PRO_GATED_SLUGS`, `PRO_BROWSER_FIRST`) |
| Log redaction (tokens, keys)? | `main.py` `_SecretRedactor` + `_PATH_SECRET_RE` on both root handlers |
| OCR from screenshots? | `main._ocr_file` (tesseract subprocess) + `ui.parse_track_args` (the format gate) |
| Encryption? | `storage.py` — `DATA_ENCRYPTION_KEY` seals per-chat envelopes; never bypass |
| The tunnel / public URL? | `tunnel.sh` + `MINI_APP_URL` (currently `https://app.597.works`; apex free for a landing page) |

## Frozen contracts — breaking these is breaking users, not code

1. Encrypted file formats (envelope schema, `.locked` wrong-key preservation)
2. Capability URL paths: `/share/<token>`, `/pay/<id>`, `/webhook/17track/<token>`
3. Telegram initData validation algorithm
4. `.env` key names and semantics (policed by `tests/test_config_surface.py`)
5. Status vocabulary strings (pinned by tests, recognized by users)
6. The 17TRACK webhook: 200-capture-first, per-(chat,tid) throttle, anti-oracle

## Import graph rule

No extracted module may import `main` — inject a callback or share a module
(`botstate.py` exists for exactly this). Layers, informally: config/storage
on the bottom; carriers/sources next; refresh/pro/miniapp orchestrate;
main + webapp are surfaces. Cyclic-looking lazy imports (e.g. delivery_history
→ refresh for a timestamp parser) are known scar tissue, not a pattern to copy.

## Tests map

| File | Guards |
|---|---|
| `test_config_surface.py` | .env.example ↔ code, requirements — a gate, not optional |
| `test_code_hygiene.py` | pyflakes over app modules |
| `test_intl_limit.py` | The intl allowance: gate, tally idempotence, copy-truth |
| `test_miniapp.py` / `_contract` / `_filters` | Mini App API behavior + payload/HTML pins |
| `test_copy_truth.py` | User-facing copy must match enforcement |
| `test_privacy.py` | Privacy claims ↔ code |
| `test_tracker.py`, `test_monitor_coverage.py` | Core tracking + poller coverage |
| `test_payments.py`, `test_phantom_routes.py` | Payment engine + deep links |
| `test_run_sh.py`, `test_backup.py`, `test_chat_tidy.py`, `test_ui_contract.py` | Ops + surfaces |

Run from project root: `python3 -m pytest tests/ -q`. Gates fail loudly —
that is their job.

## Where new features go

Cross-cutting reactions to delivery events (pings, digests, predictions,
stuck-radar, refunds) currently live in `refresh.py` — keep them small and
section-marked there until there are three or more, then extract a
subscribers/event-bus module FIRST (the agreed growth path: event bus →
sources/ split → domain/ pure core → repos; see session notes 2026-09-29).
Tier-surface changes (what free/pro see) touch five places: config limit,
pro gate, miniapp payload, webapp sheet, bot copy — all five, or the
copy-truth tests will catch you.