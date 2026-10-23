# Telegram Package Tracker Bot

Tracks packages (USPS, UPS, FedEx, DHL, OnTrac, Royal Mail, Canada Post…) and
sends Telegram notifications when a status changes. Live status comes from
[17TRACK](https://user.17track.net) (**Pro chats** — it is the Pro tier's
source), from a carrier's own tracking page via the headless browser tier
(**free chats**: USPS, UPS), from a carrier's official API where one is
configured (USPS Web Tools / USPS API v3), and when *no* source can read a
number an optional paid Ship24 lookup is tried last (see below); otherwise
cards show "Unavailable" with a link to the official page.

## Setup

```bash
# 1. Install dependencies (Python >= 3.9)
pip install -r requirements.txt

# 2. Configure the bot
cp .env.example .env   # or edit .env directly
#    TELEGRAM_BOT_API_TOKEN=123456:ABC...   (from @BotFather, required)
#    TRACK17_API_KEY=...                    (free: https://user.17track.net)
#    USPS_API_USERID=...                    (optional; free USPS Web Tools)
#    SHIP24_API_KEY=apik_...                (optional; PAID per-call fallback)

# 3. Run (./run.sh kills stale instances first — only one bot may poll at a time)
./run.sh                 # foreground
./run.sh --background    # background: logs go to tracker.log, then prints status
./run.sh --status        # status block only — counts, up/down, no ids (read-only)
```

`./run.sh --background` ends with a status block: bot/instances, console errors,
store size, active subscribers, open invoices, local + public HTTP, tunnel state
and the 17TRACK quota. It is counts and up/down only — nothing that identifies a
user — so it is safe to paste. `STATUS_WAIT` (default 8 s) tunes how long it lets
the bot settle first; `./tunnel.sh --check` gives the full tunnel report.

## Backups

Everything (the encrypted store, the receipts, the invoices and the `.env` that
opens them) lives on one machine, so a snapshot runs at 03:30 daily **and every
six hours** via `~/Library/LaunchAgents/com.packey.backup.plist` →
`~/Backups/packey/`:

```bash
# One-time: put the passphrase in the Keychain (the launchd job reads it there —
# its plist is world-readable, so a secret must not live in it). Keep a copy in
# your password manager as well: losing it makes every archive unreadable.
security add-generic-password -a "$USER" -s packey-backup -w
security find-generic-password -w -s packey-backup   # read it back

./scripts/backup_state.sh              # snapshot now (+ prune to the last 14)
./scripts/backup_state.sh --list       # which archives exist
./scripts/backup_state.sh --restore <archive.tar.enc> <empty-dir>
BACKUP_DIR=~/Dropbox/packey KEEP=30 ./scripts/backup_state.sh
export BACKUP_PASSPHRASE='…'           # or pass it in the environment
```

The state files are encrypted at rest, but a snapshot must include `.env` or the
backups cannot be read — which means the snapshot is ciphertext **plus the key**.
It is therefore safe to keep anywhere only in its encrypted form, which is what
the script produces: after verifying every file **by decrypting it** (with the
key the snapshot itself carries), the whole directory is tarred and encrypted
(AES-256-CBC, PBKDF2) into `<stamp>.tar.enc`, the plaintext directory is deleted,
and `--restore` re-verifies the decrypted copies before it writes them anywhere.
The passphrase lives outside the snapshot — the environment, or the Keychain —
never in `.env`, never in the archive, never logged. The script refuses a
destination inside the project. The file list is asked from the code (`config` /
`main` / `miniapp`), so a new state file cannot be silently left out.

> **Why every six hours as well as 03:30.** The calendar entry alone was not
> enough: the Mac is usually asleep at 03:30, and launchd does not reliably
> backfill a missed calendar job — September 2026 went days with no archive and
> nothing said so. `StartInterval` (21600s) is the sleep-proof half: when the
> interval elapses during sleep, the job runs at the next wake. `KEEP=28` is sized
> for that cadence (~4-5 runs a day ≈ a week of history). A previous plist is kept
> beside it as `com.packey.backup.plist.bak-*` — copy it back and
> `launchctl bootout`/`bootstrap` to revert.

> **Never open a `.tar.enc` by double-clicking it in Finder.** macOS cannot unpack
> it: Archive Utility leaves a `.cpgz` wrapper behind, and Finder may silently
> duplicate the file (as `<name>.tar.enc 2`), so the folder fills with copies of
> ciphertext. Look with `--list` and extract with `--restore` — nothing else
> understands the format.

> **If the passphrase is missing from both places the run refuses** (fail closed —
> a passphrase stored only beside the archive protects nothing if the machine is
> lost), and it now **sends the owner a Telegram alert** instead of failing
> silently: in September 2026 five failed nightly runs left five plaintext copies
> of `.env` in the backup directory and nobody noticed for days. A failed run
> never leaves `.env` behind: the data files stay (ciphertext, and that day's only
> copy), the key file is stripped, and the reason is logged.
> `BACKUP_PLAINTEXT_OK=1` is a temporary bridge that writes the old unencrypted
> snapshot instead — it puts the key back beside the ciphertext, so unset it as
> soon as the passphrase is in place.

`.env` is loaded automatically (`python-dotenv`) so there's no need to export
variables by hand.

## Operator alerts

Failures that used to reach `tracker.log` only — a log nobody watches — also
message the owner chat(s) in `OWNER_CHAT_IDS`:

- a rejected 17TRACK key (the paid source silently falls back to slower tiers)
- a tracking-poller cycle error (statuses stop updating until it recovers)
- a Solana payment-monitor cycle error (open invoices stop being watched)
- a state file that could not be decrypted — it is preserved as `.locked` and
  that store starts empty, which would otherwise be silent
- a second instance starting and exiting on the single-instance lock

Each is throttled to **one alert per problem per 30 minutes**, so a loop failing
every minute sends one message rather than sixty. They go **only** to the owner
IDs, never to a user, and the text carries reasons, not identifiers — no tracking
number, no chat ID. `ADMIN_ALERTS=0` silences them. The backup script's failure
alert is separate and has its own switch (`BACKUP_ALERTS`, see above).

There is also one **non-failure** message: a startup notice when the bot comes up,
with its PID, the store counts, and what is new since the last start — a changed
code fingerprint (`🆕 New code: abc1234 → def5678`) or a changed Mini App URL. A
crash loop reports once per 15 minutes rather than once per restart; the
fingerprint it compares against lives in `admin_notify.json` (encrypted at rest,
mode 0600, included in the backups).

## Commands

Aliases: `/add` = `/track` · `/packages` = `/list` · `/rename` = `/name` · `/unmute` = `/mute`.
The bot registers a ☰ commands menu on startup.

```
/track <number> [name]   Add one or more packages (numbers space-separated)
/list                     Show all packages (cards + buttons, color-coded)
/info <id>                Show one package's card
/name <id> <name>         Rename a package
/mute <id>                Toggle notifications for one package
/remove <id>              Remove a package (asks first)
/forget                   Remove one without typing its number (asks first)
/purge                    Remove all delivered packages (asks first)
/clear                    Remove all packages (admin only in groups)
/cancel                   Drop a pending confirmation
/status                   Bot uptime, package & lookup stats
/help                     Detailed help + status color legend
```

**Operator-only** (registered in the operator's own ☰ menu via
`BotCommandScopeChat`, listed in their `/help` and nowhere else):

```
/ping <text>              Send <text> to every chat the bot knows, right now
/ping                     How many chats that would reach, without sending
```

`/ping` deliberately has **no confirmation step** — the operator asked for the
text to go out the moment it is sent (2026-09-26). The guards that remain are the
ones that stop accidents instead of asking questions: it works only for chat IDs
in `OWNER_CHAT_IDS`, sends are paced at ~20/s, a 5-minute cooldown blocks a
double-tap or a retry loop, a message over Telegram's 4096-character cap is
refused before anything goes out, the caller is never pinged, and every recipient
is counted back (`sent · blocked · failed`). Recipients are every chat holding
packages plus every Pro chat; a user who blocked the bot counts as *blocked*, not
as a failure.

Status colors: 🟢 Delivered · 🟣 Info Received · 🔵 In Transit · 🟠 Out for Delivery ·
🟡 Pickup · 🔶 Attempted · 🔴 Exception · ⚪ Pending/Unavailable

## Status sources

**Active:**
- **17TRACK** — the Pro tier's source: every carrier it accepts, with push
  events via the registered webhook. Free chats never spend its quota.
- **Headless browser** (`usps_browser.py`, carrier table) — reads statuses from
  the carrier's own site when no official API is configured:
  **USPS** (the original — 17TRACK refuses pre-first-scan labels), **UPS**
  (`/track?tracknum=`, verified against a live delivered parcel) and **DHL**
  (form-flow: the SPA ignores URL parameters, so the engine fills the search
  box, dismisses the cookie-consent overlay and submits — verified live on
  DHL's own sample shipment). FedEx follows once its content renders reliably
  (the probe measured late XHR rendering). Runs as a due-driven sweep,
  gap-limited (≥5 min between sessions): USPS pre-scan numbers always, plus UPS
  numbers whose last lookup failed (a free browser retry before anything paid).
- **OnTrac direct** — OnTrac-brand numbers get one fetch of OnTrac's own
  tracking page before falling back.
- **Ship24** — paid last resort: only after every free tier failed, capped at
  `SHIP24_DAILY_MAX` calls/day.

**Dormant (env-gated, verified off — no code changes needed to revive):**
- **USPS API v3** (`USPS_CONSUMER_KEY`/`USPS_CONSUMER_SECRET`) — the preferred
  revival path: free, official, replaces the browser tier for USPS numbers.
- **USPS Web Tools** (`USPS_API_USERID`/`USPS_API_PASSWORD`) — legacy XML API,
  kept as fallback.

**Seeing it work — two mirrors of the same counters:** `/status` (bot,
admin-only) and the Mini App footer health line both show lookups **split by
tier** (`pro` / `free` — free never touches 17TRACK), the cached 17TRACK quota
with pressure, Ship24's paid calls today vs `SHIP24_DAILY_MAX`, and the browser
sweep's last read/changed. Cards carry per-package ETAs (`checked Xm ago ·
next ~Xm`) and a `🌍 Updates paused — Pro required` note on gated packages.
Status pings stay data-free (counts only), per the /privacy promise.

## USPS packages

17TRACK refuses to register a USPS number until the carrier has
logged its first *acceptance scan* — so brand-new labels show "Unavailable"
until then. USPS's own system reports those pre-scan statuses, so the bot can
use the official, free **USPS Web Tools Tracking API** instead:

1. Register for USPS Web Tools and get a userid:
   https://www.usps.com/business/web-tools-apis/
2. Put it in `.env`:
   ```
   USPS_API_USERID=your_userid_here
   USPS_API_PASSWORD=your_password_here    # optional, rarely needed
   ```

With that set, USPS packages are checked via USPS directly and "Label Created /
Pre-Shipment / Awaiting Item" states show as *Info Received* instead of
"Unavailable". If `USPS_API_USERID` is blank, the bot falls back to the
previous behavior (17TRACK only).

### Experimental: real-browser USPS tracking

If you don't have a Web Tools key, the bot can read USPS statuses by driving
the official site in a real browser (headless Chrome via `playwright`). It is
**opt-in, slow (~5-10 s/lookup), fragile, and grey-area** — for testing only:

```bash
pip install playwright
# .env: USPS_BROWSER_TRACK=1
```

The key trick: USPS/Akamai throttles fresh, cookie-less browser profiles (they
return 404). To pass, the bot **reuses a copy of your real Chrome profile**
(`~/Library/Application Support/Google/Chrome/Default` by default) whose
cookies USPS already trusts, then opens `tools.usps.com/tracking/<id>`
directly. Works on your own machine; set `USPS_CHROME_PROFILE` to override the
profile path. Fallback: a fresh browser session with form submit.

With the flag set, USPS numbers that 17TRACK refuses (e.g. "Shipping Label
Created") get a live status + the raw `Latest:` line instead of "Unavailable".

To keep request volume low (so Akamai's bot flag can decay), set
`USPS_BROWSER_ONDEMAND=1` — the browser then runs only for user-invoked lookups
(`/track`, and adding a package in the Mini App), never one package at a time on
the background cycle. The due-driven sweep is the deliberate exception, because
it is the only tier that can read a pre-first-scan label: it collects the numbers
that are due and checks them in **one** browser session per chat (single profile
copy + context), at most `USPS_SWEEP_MAX` (35) numbers, never two sessions closer
than `USPS_SWEEP_MIN_GAP` seconds — no per-package launches, and no single
session trips USPS/Akamai's bot flag. Larger batches are split into chunks of
`USPS_BATCH_MAX` numbers per session.

## Ship24 — optional paid last-resort lookup

Ship24 is an aggregator (1,600+ couriers) with a **per-call** API: one
synchronous lookup, no tracker to create, nothing extra to store. The bot uses
it as the **last resort only** — after 17TRACK, the official USPS APIs and the
USPS browser tier have all come up empty for a number. That is exactly the case
a user otherwise sees as "Unavailable" (a label the carrier hasn't scanned yet,
or a number no free source knows).

It is the only source that costs money per lookup (~$0.05 at Ship24's overage
rate), so two guards are built in:

- `SHIP24_DAILY_MAX` (default 50) caps billed lookups per local day; the count
  starts over on restart, and `0` disables the fallback while keeping the key.
- A rejected key (`auth_invalid_api_key`) or an account without the per-call
  product (`no_active_subscription`) pauses the source for an hour instead of
  re-paying for the same failure every cycle.

Setup:

1. Create an account at https://dashboard.ship24.com/onboarding and subscribe to
   the **per-call** product — the per-shipment "Trackers" API is a different
   product and answers `422 no_active_subscription` here.
2. Copy an API key (Integrations → API Keys; it looks like `apik_...`).
3. ```ini
   SHIP24_API_KEY=apik_your_key_here
   SHIP24_DAILY_MAX=50        # optional; billed lookups per day
   ```

Statuses come from Ship24's normalized `statusMilestone` (`info_received`,
`in_transit`, `out_for_delivery`, `failed_attempt`, `available_for_pickup`,
`delivered`, `exception`, `pending`), which map one-to-one onto the bot's own
vocabulary; the newest event text lands on the card as `detail`. `/status`
counts the source as `Ship24`, and every fallback writes one log line.

`scripts/ship24_probe.py` is the evaluation probe: it verifies an account
(auth, which product is subscribed, milestone coverage, latency, error codes)
before anything is wired up, and can archive the sample trackers it creates.

## Telegram Mini App (dashboard)

The bot ships with a **Mini App** — a native-feeling package dashboard inside
Telegram (button cards, mute/remove, Telegram theme colors).

**How it works:** `main.py` serves a tiny aiohttp backend (`/api/*` +
`webapp/index.html`) on `MINI_APP_HOST:MINI_APP_PORT` alongside the bot.
Telegram authenticates users by signing `initData` with the bot token; the
backend verifies that HMAC signature — no login system needed.

```bash
# 1. Expose the local backend over HTTPS (one time):
brew install cloudflared
./tunnel.sh                       # prints a https://...trycloudflare.com URL
                                  # (transient — see the warning below)

# 2. Put that URL in .env and restart:
MINI_APP_URL=https://....trycloudflare.com

# 3. Restart the bot — a "📦 Packages" button appears on the Telegram
#    menu button (and /start gets an "Open dashboard" button).
```

⚠️ **A quick-tunnel URL is transient** — it dies with the script and changes on
every restart. If `.env` holds a stable URL (Tailscale Funnel) that still
answers, `./tunnel.sh` refuses to overwrite it (pass `--force` to do it
deliberately). On 2026-09-18 a quick tunnel silently replaced the healthy
Funnel URL and later died, leaving the Mini App on HTTP 530 for hours.

Without `MINI_APP_URL` the backend still runs locally on
`127.0.0.1:8080` (see `MINI_APP_PORT`) but is not reachable from Telegram.

**Rate limiting:** because the backend is publicly reachable once the tunnel is
up, `/api/*` also carries a per-user sliding-window budget
(`MINIAPP_RATE_MAX` calls per `MINIAPP_RATE_WINDOW` seconds, default 60/60).
initData proves *who* the caller is, not that they are being reasonable — the
limit is what stops a replayed initData from looping `/api/check_payment` (each
poll reaches the Solana RPC). The payment sheet polls every 5 s, so the default
sits far above legitimate use; lower it only if you know your traffic.

On a machine that must stay up (the normal case), let the script supervise both
and keep an eye on it from a second terminal:

```bash
./tunnel.sh --watch            # supervises tunnel + bot, re-mints the URL on
                               # death, syncs .env, restarts the bot
./tunnel.sh --status           # a status line every 2 min (Ctrl-C stops it):
                               # bot / local+public HTTP / tunnel / agent / store
./tunnel.sh --status 30        # ...or any interval in seconds
./tunnel.sh --check            # one-shot report, exits 0/1, changes nothing
```

`--status` writes the same line it prints to `STATUS_LOG`
(`/tmp/packey_status.log` by default), so the history survives the terminal. The
line is privacy-safe by construction: counts and up/down states only — never a
chat ID, tracking number or package name.

Endpoints: `GET /api/packages`, `GET /api/international` (Pro), `GET /api/me`,
`POST /api/add`, `POST /api/rename`, `POST /api/remove`, `POST /api/mute`,
`POST /api/create_invoice`, `GET /api/check_payment`, `GET /api/privacy` — all
require the `Authorization: tma <initData>` header.
Smoke test: `python3 scripts/smoke_miniapp.py` (no Telegram needed).

## Pro subscription (Solana payments)

Pro (`.env`: `PRO_PRICE_USD` for `SUBSCRIPTION_DAYS` days) is paid in
**USDC or USDT on Solana** to `BOT_USDC_ADDRESS`. The Mini App opens the invoice
(`POST /api/create_invoice`) and polls `GET /api/check_payment`; the bot's
`pro.monitor_solana_payments` task watches the wallet through RPC.

**The amount identifies the payer.** A flat price can't tell two simultaneous
buyers apart, so every invoice is the base price **plus a random micro-tail**
(e.g. `1.000123`, never the bare price). `pro._match_payment` compares integer
micro-units, so an incoming transfer maps to exactly one chat — no memo, no
guessing, and no amount tolerance (off by one micro = not your payment). The
Solana Pay memo survives only as a fast path, and it's a random `pk…` token
rather than the old `chat_id-timestamp`: a public ledger must not record a
Telegram chat ID.

**How a wallet is handed the payment (2026-09-24, simplified).** One action: the Mini
App's sheet has a single **"Open in my wallet"** button that opens the invoice's **Solana
Pay link** (`GET /pay/<payment_id>` renders the same page for anyone who opens it in a
browser, with the same single button). The URI carries the recipient, the exact amount,
the mint, the memo and the reference, so the payer's own wallet reads all of it — and no
page of ours has to know which app claims the "solana:" scheme. What stays on screen is
the escape hatch: the QR (second device), the address and the amount, one tap each to
copy. A quiet line offers **"Pay through the Phantom app"**, which is the deep-link route
below — the way out when a wallet will not open at all.

Two earlier designs were removed here because they cost more than they earned: per-wallet
"open at our page" hops (Phantom/Solflare/Exodus rows) and an in-page ladder of wallet
bridges (`window.phantom.solana` / wallet-standard) that never demonstrably worked on a
payer's phone. Their endpoints — `POST /pay/<id>/tx` (in-browser signing) — are still
live and tested, one line away from being wired back in. The honest trade-off: a
`solana:` URI is resolved by the operating system, so on a device whose default handler
is another wallet, *that* wallet opens; the QR, the copied address and the amount remain
a complete payment route, and the Phantom route does not depend on the scheme at all.
`POST /pay/<id>/tx` (in-browser signing via the injected wallet provider) is kept live
and tested but is currently **unreferenced by the UI** — a deliberate leftover, one line
away from being wired back in if the wallet-bridge approach earns a second chance.

**When signing inside a browser cannot work (2026-09-24).** The in-browser route depends
on the wallet injecting a provider into OUR page — and on "solana:" being answered by
the wallet the payer chose, which it is not: the OS picks. A payer landed in Coinbase
Wallet's empty send screen twice. `GET /pay/<id>/phantom` is the rescue: it talks to
Phantom's own deep-link API (`PHANTOM_DEEPLINK=1`, default) so `phantom://v1/…` is
answerable by Phantom alone and the request carries the transaction we already built.
Two round trips are inherent (connect, then `signTransaction`) and the payer returns to
the page on each; the session (a fresh X25519 keypair per invoice + Phantom's session
token) lives in memory only, expires, and the redirect carries nothing but "step". The
pay page offers this link exactly where signing cannot work — no wallet bridge in that
browser at all, or a bridge that refused — and a refusal is submitted by us
(`sendTransaction`), not by the wallet. One new dependency: `pynacl`.

- **Late payments count.** An invoice stops being *open* after
  `PAYMENT_EXPIRY_SECONDS` (30 min) but its amount stays matchable for
  `PAYMENT_GRACE_SECONDS` (24 h), so paying a bit late still activates Pro
  instead of silently donating. (State is in-memory: a restart clears the pool.)
- **Consumed amounts are parked** for the same window, so a duplicate or late
  transfer can't accidentally credit somebody else's fresh invoice.
- **USDT can pay a USDC invoice** — the payer is identified by the amount, not
  by the token.
- **Every activation sends a receipt** (full 6-decimal amount, transaction link,
  receipt ID, benefits + new expiry) and appends a line to `pro_receipts.log`.
- **Stray transfers** are logged with amount and signature and nothing else
  happens — no money is ever silently attributed.
- `PAYMENT_TOLERANCE` is **no longer used for matching** (kept only so an
  existing `.env` keeps loading).

## The 3 international packages Pro covers

International posts (China Post, Hongkong Post, and any other national post)
have **no source but 17TRACK**, so every one of them permanently spends a slot
of the bot's own free quota (200 numbers). Pro therefore includes a fixed
allowance — `config.PRO_INTL_LIMIT = 3` — per subscription:

- The cap applies **while Pro is active** (an expired subscription lifts it) and
  is checked **before the first lookup**, from the number's format, so adding
  several CN numbers at once can't slip past one at a time.
- It counts **exactly what the 🌍 tab shows** — `carriers.is_international` is
  the single rule (a `chinapost`/`hkpost`/`intl` slug, or 17TRACK's verified
  CN/HK origin country for a number our own regex called "unknown"). Both the
  tab and `pro.check_intl_limit` call it, so they can't disagree.
- Inside the allowance nothing changes; on the 4th the bot/app explains the
  shared quota and points at Remove / `/forget` — never at "upgrade", because
  the user is already paying.
- Free users are unaffected: only the 🌍 *tab* is the Pro feature.
- `OWNER_CHAT_IDS` are exempt.

## Privacy text — one source

`/privacy` and the Mini App's 🔒 sheet render the **same** string
(`ui.PRIVACY_TEXT`): the bot sends it, the app fetches it over
`GET /api/privacy`. The sheet keeps a local fallback copy for the case of a bot
that predates the endpoint or a dead tunnel, but the policy itself is written
once — an edit can't promise something in one surface and not the other.

## Notes

- Groups: anyone can add packages; only admins (or the original adder) can
  rename/remove; only admins can `/clear`.
- **Updating is global and automatic** — there are no refresh/update buttons
  (bot or Mini App) and no manual-refresh budget: the background loop owns every
  lookup, paced by each package's own state (`refresh.CADENCE`: delivery-day
  numbers every 1 min on Pro, in-transit every 3 min Pro / 10 min free,
  pre-scan every 1 min Pro / 3 min free (pre-scan reads ride the browser
  sweep, so sub-5-minute requests run at its 5-min gap), delivered ones
  never again). Under
  17TRACK quota pressure the non-critical waits stretch ×2.
- **Notifications carry no package data.** A status change sends one ping per
  chat per cycle — "n of your packages changed" plus an *Open the app* button.
  No tracking number, name, status or carrier ever lands in chat history
  (group members included); details live in the Mini App behind signed initData.
  The one exception is the lost-package funeral/resurrection notice: naming the
  package is the whole joke, so it is sent **ephemerally** like a card — and in
  a group where the bot isn't an admin that delete fails and the notice stays.
  A muted package stays silent unless the change is a delivery or a problem.
- **Chat messages with package data self-destruct.** Cards (`/track`, `/info`)
  and the `/list` roster are deleted by the bot ~90 s after they're sent
  (`EPHEMERAL_TTL` in `.env`; `0` = keep forever), so tracking numbers and names
  don't accumulate in chat history. In a group where the bot isn't an admin the
  delete silently fails and the message simply stays.
- **Lifecycle deletion.** A delivered package is terminal (never checked again),
  so the bot forgets it entirely **14 days after delivery** (`RETENTION_DAYS` in
  `.env`; `0` = keep forever) and says so with a data-free notice. Only exactly
  `Delivered` is auto-purged — `Exception`/`Expired` can still resume, so those
  stay until the user decides. `/forget` deletes any package **without typing its
  tracking number** (numbered list → tap → normal confirmation).
- **`/list` shows no tracking numbers** — names, statuses and carriers only
  (`/forget` lists them the same way). Numbers live in the Mini App, which has a
  copy button per card.
- **The encrypted store is compartmentalized.** `track_data.json` keeps each
  chat's packages under its own random key, wrapped by the master key inside the
  file (v2 envelope format; older flat files load unchanged and upgrade on the
  next save, keeping a `track_data.v1.bak` ciphertext copy for rollback).
- **Logs carry no identifiers**: chat IDs are masked to a stable `chat:xxxxxxxx`
  tag and tracking numbers were already masked. Set `DATA_ENCRYPTION_KEY` (see
  `.env.example`) and the store no longer depends on the bot token at all —
  files re-key themselves on the first read.
- Carriers may refuse tracking numbers before the first acceptance scan —
  those packages start tracking automatically once scanned (17TRACK policy
  retries registration every 30 min).
- 17TRACK's free quota is **200 accepted tracking numbers**. The adaptive
  cadence plus source routing (official USPS APIs first, 17TRACK reserved for
  CN/HK/international posts) exist to keep the bot inside it.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```
