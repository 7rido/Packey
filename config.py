"""Central configuration for the Telegram package tracker bot.

Every env-driven constant lives here. The .env file is loaded at the very
top of this module so all os.getenv(...) calls below see it — main.py
imports config before anything else that needs configuration.
"""
import os
import re
from pathlib import Path
from typing import Optional

# Load .env early so every os.getenv(...) below sees it. python-dotenv is
# optional — if it's not installed you can still export vars in your shell.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

__all__ = [
    "FREE_PACKAGE_LIMIT", "FREE_INTL_LIMIT", "PRO_PACKAGE_LIMIT", "PRO_INTL_LIMIT",
    "OWNER_CHAT_IDS", "UNLIMITED", "ADMIN_ALERTS", "WEBHOOK_TOKEN",
    "FREE_REFRESH_INTERVAL", "PRO_REFRESH_INTERVAL", "PRO_PRICE_USD",
    "OFD_FAST_PRO", "OFD_FAST_FREE",
    "SUBSCRIPTION_DAYS", "PAYMENT_EXPIRY_SECONDS",
    "SOLANA_RPC_URL", "BOT_USDC_ADDRESS", "USDC_MINT", "USDT_MINT",
    "ACCEPTED_MINTS", "SOL_POLL_SECONDS", "PRO_USERS_FILE", "PAYMENT_GRACE_SECONDS",
    "OPERATOR_WALLETS",
    "REFRESH_LIMITS_FILE", "DATA_FILE", "HTTP_TIMEOUT",
    "PENDING_PAYMENTS_FILE", "PRO_RECEIPTS_FILE", "INTL_REGISTRATIONS_FILE",
    "DEPOSIT_ADDRS_FILE", "DEPOSIT_SEED", "PRO_EXPIRY_WARNED_FILE",
    "MODEL_NAME", "REFRESH_CONCURRENCY", "REGISTER_RETRY_INTERVAL",
    "track17_api_key", "USPS_API_USERID", "USPS_API_PASSWORD",
    "USPS_WEBTOOLS_URL", "USPS_AUTH_BACKOFF", "USPS_BROWSER_TRACK",
    "USPS_BROWSER_ONDEMAND", "USPS_BROWSER_SLOTS_PER_CARRIER",
    "PRO_BROWSER_FIRST", "MINI_APP_URL",
    "MINI_APP_HOST",
    "SHARE_LINK_TTL_DAYS", "SHARE_LINKS_FILE", "DELIVERY_HISTORY_FILE",
    "DELIVERY_HISTORY_MAX",
    "MINI_APP_PORT", "WEBAPP_DIR", "INIT_DATA_MAX_AGE", "EPHEMERAL_TTL",
    "PORCH_REPING_SECONDS",
    "RETENTION_DAYS", "LOST_AFTER_DAYS", "DATA_ENCRYPTION_KEY",
    "USPS_SWEEP_MAX", "USPS_SWEEP_MIN_GAP", "SHIP24_DAILY_MAX",
    "SHIP24_COUNTER_FILE",
    "MINIAPP_RATE_MAX", "MINIAPP_RATE_WINDOW",
]

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
# ── Pro tier & Solana (USDC/USDT) payments ────
FREE_PACKAGE_LIMIT = 5
# Free chats may register this many international (17TRACK-only) packages per
# calendar month — the shared 17TRACK quota is finite, and without a cap one
# free chat could spend it freely. Resets on the 1st. Retired to 0: the free
# allowance lasted only briefly — international is Pro-only again, and the
# tally machinery stays so this knob can be raised without re-plumbing.
FREE_INTL_LIMIT = 0
PRO_PACKAGE_LIMIT = 15
# International posts (CN/HK and other national posts) have no source but
# 17TRACK, so each one spends a slot of the bot's own free quota (200 numbers).
# Pro therefore covers a fixed number per subscription, and the cap counts the
# REGISTRATIONS consumed in the current period (pro.count_international), not
# the packages currently stored: add/remove/re-add must not refill it.
PRO_INTL_LIMIT = 5
# Owner bypass: comma-separated chat IDs in OWNER_CHAT_IDS get unlimited
# packages + Pro treatment (intervals, UI) without payment.
OWNER_CHAT_IDS = {
    int(x) for x in (os.getenv("OWNER_CHAT_IDS") or "").replace(" ", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
# Operational alerts to OWNER_CHAT_IDS — the startup checks and background loops
# that fail (a rejected 17TRACK key, a poller cycle error, an undecryptable state
# file). On by default because those failures used to reach tracker.log only, and
# nobody watches a log: the September 2026 backup outage ran for days unnoticed.
# The backup script has its own switch (BACKUP_ALERTS); this one is bot-side.
ADMIN_ALERTS = (os.getenv("ADMIN_ALERTS") or "1").strip().lower() not in (
    "0", "false", "no", "off")
# Secret path segment for the 17TRACK webhook: the route becomes
# /webhook/17track/<WEBHOOK_TOKEN>, so only 17TRACK (and you) can call it. The
# token must be [A-Za-z0-9_-] and at least 16 chars — a shorter or stranger value
# is refused at startup rather than half-registered. Unset = today's public path,
# which works but lets anyone who knows a tracked number burn refresh quota.
WEBHOOK_TOKEN = (os.getenv("WEBHOOK_TOKEN") or "").strip()
if WEBHOOK_TOKEN and not (len(WEBHOOK_TOKEN) >= 16
                          and re.fullmatch(r"[A-Za-z0-9_-]+", WEBHOOK_TOKEN)):
    WEBHOOK_TOKEN = ""          # refuse a weak token: half-configured is worse
UNLIMITED = 1_000_000_000
# The tier baseline for In-Transit packages — refresh.CADENCE builds its
# "In Transit" row from these, so the schedule, /start's pitch and the
# Pro-activation message all quote the same two numbers.
FREE_REFRESH_INTERVAL = 14400  # 4 h in-transit checks (free)
PRO_REFRESH_INTERVAL = 7200    # 2 h in-transit checks (pro)
# Out-for-Delivery is transient (hours) and it is the status where freshness
# matters most — the parcel is on the truck, and it is Porch Guard's fallback
# when the 17TRACK webhook misses the delivery event. So OFD gets its own fast
# lane on both tiers; the Pro number is the free one's, sped up, like the tier
# baseline above. Config owns these because the /start pitch quotes the same
# table (tests/test_copy_truth.py renders the text from CADENCE, so a change
# here can never drift from the copy).
OFD_FAST_PRO = 600    # 10 min Out-for-Delivery checks (pro)
OFD_FAST_FREE = 1800  # 30 min Out-for-Delivery checks (free)
# Price and duration are THE operator knobs, and they live here and nowhere else:
# every surface reads them (the sheet via miniapp._api_create_invoice, the receipt
# and welcome text via pro), so a change can never leave the copy advertising a
# price or term the server would not sell. Both are frozen at startup — restart
# the bot after editing .env.
PRO_PRICE_USD = float(os.getenv("PRO_PRICE_USD", "1.00"))  # base; see pro.allocate_amount
# Length of one activation, in DAYS. Applies to NEW activations only: a buyer
# already credited keeps the end date they paid for. Floored at 1 day, because
# 0 would credit and expire in the same breath.
SUBSCRIPTION_DAYS = max(1, int(os.getenv("SUBSCRIPTION_DAYS", "7")))
PAYMENT_EXPIRY_SECONDS = 1800  # unpaid invoice stops being matchable-fast after 30 min
# Late payments still count: an expired invoice keeps its (unique) amount
# matchable for this long, so someone who pays a few minutes late is credited
# instead of losing the money in an unmatched transfer.
PAYMENT_GRACE_SECONDS = int(os.getenv("PAYMENT_GRACE_SECONDS", "86400"))  # 24 h
# Dormant for matching: the amount is now a unique per-invoice identifier, so
# payments are matched exactly (pro._match_payment compares micro-USDC). Kept
# only because .env may still set it; deliberately NOT in __all__ (dead name).
PAYMENT_TOLERANCE = 0.01       # ±1 cent (legacy)
SOLANA_RPC_URL = (os.getenv("SOLANA_RPC_URL") or
                  "https://api.mainnet-beta.solana.com").strip()
BOT_USDC_ADDRESS = (os.getenv("BOT_USDC_ADDRESS") or "").strip() or None
# Wallets WE control beyond the bot's own (your personal wallet, a treasury, a
# second machine). A transfer signed by one of these is our own money moving, and
# moving it INTO the receiving wallet looks exactly like a customer paying — which
# is how a refund float or a test transfer could buy somebody Pro. Addresses only:
# the bot never needs (and must never hold) their keys. Comma-separated.
def _wallet_list(raw: Optional[str]) -> frozenset:
    """Addresses from a comma/semicolon-separated env value, blanks dropped."""
    return frozenset(
        part.strip()
        for part in (raw or "").replace(";", ",").split(",")
        if part.strip())


OPERATOR_WALLETS = _wallet_list(os.getenv("OPERATOR_WALLETS"))
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
ACCEPTED_MINTS = {USDC_MINT: "USDC", USDT_MINT: "USDT"}
SOL_POLL_SECONDS = int(os.getenv("SOL_POLL_SECONDS", "10"))  # public RPC is rate-limited; 10s is safe
PRO_USERS_FILE = Path("pro_users.json")
# LEGACY: nothing reads or writes this file any more — manual refresh, and with it
# the daily refresh budget, was removed from the product (updating is global and
# automatic). Kept as a path because the backup script and its test ask the code
# for the file set, and the file on disk still holds pre-removal counters.
REFRESH_LIMITS_FILE = Path("refresh_limits.json")
# Open Solana invoices (the amount shown to a buyer IS their identifier) plus
# the grace pool and the recently-consumed amounts. Persisted because a restart
# used to throw every open invoice away: the buyer paid the amount they were
# shown, the monitor matched it against an empty pool and credited nobody.
PENDING_PAYMENTS_FILE = Path("pending_payments.json")
# Which deposit address was handed to which chat. Encrypted at rest (chat IDs
# inside, like every other state file): this is the map that de-pseudonymises the
# chain for exactly the chats that paid. Deterministic derivation means the file
# can be rebuilt from chat IDs, but it is kept as the auditable list of addresses
# this bot ever issued.
DEPOSIT_ADDRS_FILE = Path("deposit_addrs.json")
# Master seed (hex, 32 bytes) for the per-chat deposit addresses. Unset ⇒ that
# path is off and the monitor says so once at startup; the QR/reference flow keeps
# working. NEVER rotate this: every chat's address — and the funds sitting on it —
# is derived from it. Generate with:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
DEPOSIT_SEED = (os.getenv("DEPOSIT_SEED") or "").strip()
# Payment receipts (one entry per activation): the accounting trail behind every
# Pro credit, and the source for re-sending a receipt. Encrypted at rest because
# every line names the paying chat — this used to be a plaintext .log file.
PRO_RECEIPTS_FILE = Path("pro_receipts.json")

# Per-member 17TRACK registrations consumed in the member's current Pro billing
# period. The cost driver is a REGISTRATION (the shared 17TRACK quota slot is
# spent at register time), so counting stored cards would let add/remove churn
# consume unlimited quota. Encrypted at rest because it names members.
INTL_REGISTRATIONS_FILE = Path("intl_registrations.json")

# The once-per-subscription "expires today" warning ledger: chat_id → the expiry
# timestamp the warning was already sent for. Keyed by the expiry itself, so a
# renewal arms a fresh warning while no restart can ever re-send the old one.
# Encrypted at rest because it names members.
PRO_EXPIRY_WARNED_FILE = Path("pro_expiry_warned.json")

DATA_FILE = Path("track_data.json")
# How long a chat message that carries package data (cards, /list, /info)
# survives before the bot deletes it, so tracking numbers and names don't pile
# up in chat history — groups especially. 0 disables the cleanup.
EPHEMERAL_TTL = int(os.getenv("EPHEMERAL_TTL", "90"))
# Porch Guard: the moment a package is delivered the bot pings loudly, and if
# nobody taps the ✅ ack within this long it asks once more — then stops for good.
# A re-ping is a nudge, not a nag: porch_repinged latches after the first one.
PORCH_REPING_SECONDS = int(os.getenv("PORCH_REPING_SECONDS", "900"))
# Lifecycle deletion: a delivered package is terminal (never checked again), so
# the bot forgets it entirely after this many days — it shouldn't hold tracking
# numbers it no longer needs. 0 disables. Only "Delivered" is auto-purged:
# Exception/Expired packages can still resume, so those wait for the user.
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "14"))
# Funerals for lost packages: a package with no carrier event for this many
# days, while still in a non-terminal state, is declared lost — one ephemeral
# funeral notice, a memorial line on its card, and a resurrection notice if
# scans resume. 0 disables the whole lifecycle.
LOST_AFTER_DAYS = int(os.getenv("LOST_AFTER_DAYS", "30"))
# Dormant: the flat "every N seconds" poll was replaced by the state-adaptive
# schedule in refresh.CADENCE (Session 14) — the old comment here claimed a
# 288 calls/day/package quota budget, which was never how 17TRACK meters usage
# (it counts accepted tracking numbers, 200 of them). Kept only because .env
# may still set it; nothing schedules from this; NOT in __all__.
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "300"))
HTTP_TIMEOUT = 18.0
MODEL_NAME = "packeybot"
REFRESH_CONCURRENCY = 8  # max concurrent carrier lookups
# How long to wait before retrying 17TRACK registration after a -18019823
# "carrier policy / not yet scannable" rejection (label not scanned yet).
REGISTER_RETRY_INTERVAL = int(os.getenv("REGISTER_RETRY_INTERVAL", "1800"))  # 30 min

# Empty-string env values (e.g. placeholder lines in .env) count as "not set".
_track17_env = (os.getenv("TRACK17_API_KEY") or "").strip()
track17_api_key = _track17_env or "default_api_key"

# USPS Web Tools Tracking API (TrackV2) — the official, free USPS endpoint.
# Unlike 17TRACK it also reports pre-first-scan packages, which makes
# it the best workaround for USPS numbers 17TRACK refuses to register yet.
# Get a free userid: https://www.usps.com/business/web-tools-apis/
USPS_API_USERID = (os.getenv("USPS_API_USERID") or "").strip() or None
USPS_API_PASSWORD = os.getenv("USPS_API_PASSWORD", "")
USPS_WEBTOOLS_URL = "https://secure.shippingapis.com/ShippingAPI.dll"
USPS_AUTH_BACKOFF = 1800  # pause USPS lookups after an authorization failure

# EXPERIMENTAL: drive USPS's site in a real browser (usps_browser.py) to read
# statuses that 17TRACK refuses. Requires: pip install playwright. Slow, fragile.
USPS_BROWSER_TRACK = (os.getenv("USPS_BROWSER_TRACK") or "").strip().lower() in ("1", "true", "yes")

# Run the browser-based USPS check ONLY on user commands (/track /update /refresh),
# not on the 5-min background cycle. Low traffic lets Akamai's bot flag decay.
USPS_BROWSER_ONDEMAND = (os.getenv("USPS_BROWSER_ONDEMAND") or "").strip().lower() in ("1", "true", "yes")

# How many browser sessions ONE carrier may run at the same time. The four
# carriers already run in parallel with each other (each holds its own profile
# copy); this knob only multiplies the request rate a SINGLE carrier's bot
# defense sees — the one thing that historically got the tier flagged — so it
# defaults to 1. Raise only with a reason, and watch the throttle log.
USPS_BROWSER_SLOTS_PER_CARRIER = max(1, int(os.getenv("USPS_BROWSER_SLOTS_PER_CARRIER", "1")))

# Pro chats: read UPS/FedEx/DHL on the carrier's own page in the headless browser
# BEFORE spending 17TRACK, and let 17TRACK back a USPS number whose official tiers
# came up empty. 17TRACK's aggregate feed lagged a delivered UPS parcel by ~5 h
# (measured 2026-09-23); a carrier-direct read cannot lag the carrier. Set to 0 to
# restore the 17TRACK-first order exactly — the revert path if the browser tier
# degrades under primary-tier load.
PRO_BROWSER_FIRST = (os.getenv("PRO_BROWSER_FIRST") or "1").strip().lower() in ("1", "true", "yes")

# -- Phantom deep-link route (miniapp /pay/<id>/phantom) --
# The rescue path for a payment, not the default one: the pay page's in-browser handoff
# needs the wallet to inject a provider into OUR page, and "solana:" is answered by
# whichever wallet the OS likes — a payer landed in Coinbase Wallet's empty send screen
# (2026-09-24). This route talks to Phantom's own deep-link API instead ("phantom://"
# is answerable by Phantom alone) and carries the transaction we built, so the wallet
# composes nothing. It needs MINI_APP_URL to be an https URL to come back to, a PyNaCl
# install (X25519 + XSalsa20-Poly1305), and two Phantom round trips from the payer.
#   on  — the /pay/<id>/phantom route answers (the pay page offers it when signing fails)
#   off — the route 404s; nothing else about paying changes
PHANTOM_DEEPLINK = (os.getenv("PHANTOM_DEEPLINK") or "1").strip().lower() in ("1", "true", "yes")

# -- Headless browser sweep (refresh._sweep_browser) --
# The browser is the only tier that can read pre-first-scan USPS labels when no
# official USPS API is configured, so it runs as a due-driven sweep: one session
# per chat holding at most USPS_SWEEP_MAX numbers, and never two sweeps closer
# than USPS_SWEEP_MIN_GAP seconds. One session for many numbers is what keeps
# the Akamai bot flag asleep; a session per package is what wakes it.
USPS_SWEEP_MAX = max(1, int(os.getenv("USPS_SWEEP_MAX", "35")))
USPS_SWEEP_MIN_GAP = max(60, int(os.getenv("USPS_SWEEP_MIN_GAP", "300")))

# ── Ship24 (paid per-call fallback) ───────────────────────────
# Ship24 is the only source the bot pays per lookup, so it is spent only after
# every free tier has come up empty for a number (see carriers.fetch_track_status)
# and this cap bounds what a day of stuck-but-unreadable packages can cost.
# 0 disables the fallback without removing SHIP24_API_KEY. The count is
# persisted (SHIP24_COUNTER_FILE): it is a spending cap, so a restart must not
# hand the day's budget back — the old in-memory count reset on every restart.
SHIP24_DAILY_MAX = max(0, int(os.getenv("SHIP24_DAILY_MAX", "50")))
# Today's paid-call count: {"day": "YYYY-MM-DD", "calls": N}. Plain JSON on
# purpose — a call count is not sensitive (no chat ids, no tracking numbers).
SHIP24_COUNTER_FILE = Path("ship24_counter.json")

# ──────────────────────────────────────────────
# Telegram Mini App (dashboard web app)
# ──────────────────────────────────────────────
# MINI_APP_URL is the PUBLIC HTTPS URL of the Mini App (e.g. a cloudflared /
# ngrok tunnel pointing at MINI_APP_HOST:MINI_APP_PORT). When set, /start and
# /list carry an "Open the app" button. The local aiohttp backend always
# starts, so the tunnel always has a target.
# ──────────────────────────────────────────────
# Data-at-rest encryption key
# ──────────────────────────────────────────────
# The master key for the local data files. Set this and the encrypted store no
# longer depends on the bot token: rotating the token can't brick the data, and
# a leaked token can't decrypt the disk. Existing files are re-encrypted under
# this key the first time they're read (transparent migration). Back it up —
# losing it means losing the stored data.
DATA_ENCRYPTION_KEY = (os.getenv("DATA_ENCRYPTION_KEY") or "").strip() or None

MINI_APP_URL = (os.getenv("MINI_APP_URL") or "").strip() or None
# Shared tracking links: the Share button on a card mints
# <MINI_APP_URL>/share/<token> — an unguessable URL anyone can open (no
# Telegram) that shows the package's name, status and latest update, and NEVER
# its tracking number. Links stop working after this many days; 0 means they
# never expire.
SHARE_LINK_TTL_DAYS = int(os.getenv("SHARE_LINK_TTL_DAYS", "30"))
# Token store for those links (encrypted at rest like the other sidecars).
SHARE_LINKS_FILE = Path("share_links.json")
# One record per delivery (carrier, transit days, when it landed, international,
# whether the start is approximate). Pure logging for the future delivery-window
# estimates — nothing user-visible reads it yet.
DELIVERY_HISTORY_FILE = Path("delivery_history.json")
# The sweep's last-run counters (timestamp, read, changed — no user data).
# Written by refresh so run.sh --status can report the RUNNING bot's sweep
# activity; a fresh `import refresh` in the status subprocess cannot see the
# live bot's in-memory LAST_SWEEP and always reported "never".
SWEEP_STATE_FILE = Path("sweep_state.json")
# How many delivery records to keep. A COUNT, not a days-based cut like
# RETENTION_DAYS: these records ARE the deliveries, so a time window would
# delete the training data this file exists to collect. It matters because the
# whole list is rewritten on every delivery, so an unbounded one makes each
# write costlier than the last. 0 = keep everything.
DELIVERY_HISTORY_MAX = int(os.getenv("DELIVERY_HISTORY_MAX", "1000"))
MINI_APP_HOST = (os.getenv("MINI_APP_HOST") or "127.0.0.1").strip()
MINI_APP_PORT = int(os.getenv("MINI_APP_PORT", "8080"))
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"
INIT_DATA_MAX_AGE = 3600  # reject replayed Telegram initData older than an hour
# Mini App API budget. The backend is reachable from the public internet (the
# tunnel forwards it) and a valid initData is the only gate, so a script that
# replays one could otherwise loop the invoice/payment endpoints and spin the
# Solana poller. Sliding window per Telegram user; the payment sheet polls
# check_payment every 5 s (= 12/min), so this stays far above legitimate use.
MINIAPP_RATE_MAX = max(1, int(os.getenv("MINIAPP_RATE_MAX", "60")))
MINIAPP_RATE_WINDOW = max(1.0, float(os.getenv("MINIAPP_RATE_WINDOW", "60")))
_bot_token: Optional[str] = None