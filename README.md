# Packey 📦

A self-hosted Telegram bot that tracks packages across USPS, UPS, FedEx, DHL, OnTrac, and international carriers — and notifies you in real time when something changes.

Packey runs as a single Python process on your own machine (no cloud, no database), stores everything encrypted on disk, and ships with a Telegram Mini App dashboard for a native-feeling package view right inside Telegram.

## Features

- **Multi-carrier tracking** — USPS, UPS, FedEx, DHL, OnTrac, Royal Mail, China Post, and 1,600+ carriers via aggregators
- **Real-time notifications** — status changes ping your Telegram chat; delivery alerts with "Got it" / "Not home" buttons
- **Privacy-first** — tracking data is encrypted at rest (AES, per-chat keys); chat messages with package data auto-delete after 90 seconds; logs carry no identifiers
- **Headless browser tier** — reads statuses directly from carrier websites when APIs aren't available, with stealth techniques to avoid bot detection
- **Telegram Mini App** — a full package dashboard inside Telegram with theme-matching UI, mute/remove controls, and a payment sheet
- **Pro tier with Solana payments** — optional paid subscriptions via USDC/USDT on Solana, with micro-tail amount matching (no memo needed)
- **Smart source routing** — picks the cheapest/fastest source per carrier: official APIs → headless browser → 17TRACK → Ship24 (paid fallback)
- **Porch Guard** — delivery alerts with a re-ping if you don't acknowledge
- **Encrypted backups** — automated snapshots with Keychain-based passphrase, verification on both pack and restore
- **Operator tooling** — health canaries, admin alerts, carrier probes, payment reconciliation, and a smoke test suite

## Architecture

```
main.py              Bot commands + aiogram 3 handlers (composition root)
├── config.py        All env-driven constants
├── storage.py       Envelope encryption, atomic writes, per-chat locks
├── carriers.py      Carrier detection, source routing, 17TRACK/API clients
├── refresh.py       Background poller, cadence rules, browser sweep, Porch Guard
├── pro.py           Free/Pro tiers, Solana payment engine, receipts
├── miniapp.py       aiohttp backend for the Mini App (/api/*)
├── screens.py       Telegram UI screens (tracking card, alerts, settings)
├── ui.py            Shared card/text rendering (bot + Mini App)
├── usps_browser.py  Headless Chrome carrier scraper (USPS/UPS/FedEx/DHL)
├── usps_api.py      USPS Tracking API v3 client (OAuth2)
├── ship24.py        Paid per-call fallback client
├── solana_tx.py     Unsigned Solana transaction builder
├── phantom_*.py     Phantom wallet deep-link sessions
├── share_links.py   Shareable tracking link tokens
└── webapp/          Mini App frontend (single-file HTML + inline JS)
```

Single process, no database. JSON state files encrypted at rest with per-chat keys derived from a master `DATA_ENCRYPTION_KEY`. The bot, the Mini App backend, and the background poller all run in one asyncio loop.

## Requirements

- Python 3.9+
- Google Chrome (for the headless browser tier)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A [17TRACK](https://user.17track.net) API key (free tier: 200 numbers)

Optional:
- [Playwright](https://playwright.dev/python/) for headless browser tracking
- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) or similar for HTTPS exposure
- [Ship24](https://dashboard.ship24.com) API key for paid fallback lookups
- USPS Web Tools or API v3 credentials for direct USPS tracking
- A Solana wallet + [Helius](https://helius.dev) RPC for Pro payments

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/7rido/Packey.git
cd Packey
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — at minimum set:
#   TELEGRAM_BOT_API_TOKEN  (from @BotFather)
#   TRACK17_API_KEY         (from 17TRACK)
#   DATA_ENCRYPTION_KEY     (generate: python3 -c "import secrets; print(secrets.token_hex(32))")

# 3. Run
./run.sh                  # foreground
./run.sh --background     # background (logs to tracker.log)
./run.sh --status         # check if it's running
```

## Configuration

All configuration lives in `.env`. See [`.env.example`](.env.example) for every option with inline documentation. The key groups:

| Group | Key variables | Purpose |
|---|---|---|
| **Core** | `TELEGRAM_BOT_API_TOKEN`, `TRACK17_API_KEY` | Bot identity and primary tracking source |
| **Encryption** | `DATA_ENCRYPTION_KEY` | Master key for all encrypted state files |
| **USPS** | `USPS_API_USERID`, `USPS_CONSUMER_KEY`, `USPS_BROWSER_TRACK` | Direct USPS tracking (API or browser) |
| **Browser** | `USPS_SWEEP_MAX`, `USPS_SWEEP_MIN_GAP`, `USPS_BROWSER_SLOTS_PER_CARRIER` | Headless Chrome pacing and stealth |
| **Mini App** | `MINI_APP_URL`, `MINI_APP_PORT` | Telegram Mini App dashboard |
| **Pro / Payments** | `BOT_USDC_ADDRESS`, `PRO_PRICE_USD`, `SUBSCRIPTION_DAYS` | Solana-based subscriptions |
| **Ship24** | `SHIP24_API_KEY`, `SHIP24_DAILY_MAX` | Paid last-resort lookups |
| **Privacy** | `EPHEMERAL_TTL`, `RETENTION_DAYS`, `LOST_AFTER_DAYS` | Data lifecycle and auto-deletion |

## Bot Commands

```
/track <number> [name]    Add a package (auto-detects carrier)
/list                     Show all packages
/info <id>                Package detail card
/rename <id> <name>       Rename a package
/mute <id>                Toggle notifications
/remove <id>              Remove a package
/forget                   Remove without typing the number
/purge                    Remove all delivered packages
/settings                 Notification preferences
/status                   Bot uptime and lookup stats
/help                     Full help + status legend
/privacy                  What data is stored and how
```

Status indicators: 🟢 Delivered · 🟣 Info Received · 🔵 In Transit · 🟠 Out for Delivery · 🟡 Pickup · 🔶 Attempted · 🔴 Exception · ⚪ Pending

## Mini App

The bot includes a Telegram Mini App — a native-feeling package dashboard accessible from a button in the chat. It requires an HTTPS tunnel to your machine:

```bash
# Start a Cloudflare tunnel (one time setup)
brew install cloudflared
./tunnel.sh                        # prints the public URL

# Add the URL to .env and restart
MINI_APP_URL=https://your-tunnel.trycloudflare.com
./run.sh --background
```

The Mini App provides: package cards with status and carrier info, mute/remove controls, international package tracking (Pro), and the Solana payment sheet for Pro subscriptions.

API endpoints (all require `Authorization: tma <initData>`):
`/api/packages`, `/api/international`, `/api/me`, `/api/add`, `/api/rename`, `/api/remove`, `/api/mute`, `/api/create_invoice`, `/api/check_payment`, `/api/privacy`

## How Tracking Works

Packey routes each package to the cheapest available source:

1. **Official APIs** — USPS Web Tools / API v3 (when configured)
2. **Headless browser** — reads the carrier's own tracking page (USPS, UPS, FedEx, DHL) using a stealth Chromium session with cookie reuse
3. **17TRACK** — aggregator API, reserved for international posts and carriers without other sources
4. **Ship24** — paid per-call fallback (~$0.05/lookup), only when everything else fails

The background poller checks packages on an adaptive cadence: delivery-day packages every 1–3 minutes, in-transit every 3–10 minutes, and delivered packages are never checked again (auto-purged after `RETENTION_DAYS`).

## Backups

Automated encrypted backups run via launchd (macOS):

```bash
# Setup: store the passphrase in Keychain
security add-generic-password -a "$USER" -s packey-backup -w

# Manual backup
./scripts/backup_state.sh

# List / restore
./scripts/backup_state.sh --list
./scripts/backup_state.sh --restore <archive.tar.enc> <empty-dir>
```

Backups include all encrypted state files plus `.env`. The archive is AES-256-CBC encrypted and verified (decrypted) on both pack and restore.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

52 test files, ~1,200 tests covering: carrier detection, payment matching, privacy claims vs code, Mini App API contracts, browser tier behavior, encryption, and config surface checks.

## Scripts

| Script | Purpose |
|---|---|
| `backup_state.sh` | Encrypted backup + restore of all state files |
| `smoke_carriers.py` | Live smoke test against real carrier sites |
| `smoke_miniapp.py` | Mini App API smoke test (no Telegram needed) |
| `payment_reconcile.py` | Credit a Solana payment the monitor missed |
| `probe_carrier_browser.py` | Test headless browser against each carrier |
| `probe_17track.py` | Verify 17TRACK API key and registration |
| `ship24_probe.py` | Evaluate Ship24 auth and tracker coverage |
| `rekey_dek.py` | Rotate the master encryption key |
| `fedex_spy.py` | FedEx page-load diagnostic |

## Privacy

- Tracking data is encrypted at rest with per-chat envelope encryption
- Chat messages with package data auto-delete (`EPHEMERAL_TTL`, default 90s)
- Notifications carry no package data — just "N packages changed" + a button
- `/list` shows names and statuses only — no tracking numbers in chat history
- Logs use masked chat IDs (`chat:xxxxxxxx`) and masked tracking numbers
- Delivered packages are auto-purged after `RETENTION_DAYS` (default 14)
- The bot and Mini App serve the same privacy text from a single source

## License

Private project. All rights reserved.
