"""Telegram Mini App backend for the tracker bot.

Extracted from main.py (Session 5 of EXTRACTION_PLAN.md): initData
validation, the package-payload shape, all aiohttp views (package CRUD +
refresh + pro invoices), and the aiohttp app assembly.

miniapp.py imports config / storage directly; carriers / pro / ui / refresh
enter via lazy _carriers() / _pro() / _ui() / _refresh() accessors (kept from
the extraction era when a carriers→main edge still existed; today they just
defer heavy imports until first use). main.py re-imports every name here.
"""
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from html import escape
from string import Template
from typing import Dict, List, Optional
from urllib.parse import parse_qsl

from aiohttp import web

import config
import phantom_link
import phantom_page
import share_links
import solana_tx
import storage


def _ui():
    import ui
    return ui


def _pro():
    import pro
    return pro


def _carriers():
    import carriers
    return carriers

logger = logging.getLogger("tracker")


# ── Refresh-plumbing calls (Session 7a) ───────────────────────────
# _register_and_fetch / save live in refresh.py; miniapp reaches them through a
# lazy module accessor (consistent with _pro() etc).
def _refresh():
    import refresh
    return refresh




# ──────────────────────────────────────────────
# Telegram Mini App backend
# ──────────────────────────────────────────────
def _validate_init_data(init_data: str) -> Optional[dict]:
    """Validate Telegram Mini App initData per the official algorithm:
    secret = HMAC_SHA256(key=b'WebAppData', msg=bot_token); the initData's
    hash field must equal HMAC_SHA256(key=secret, msg=data_check_string).
    Returns the parsed user dict, or None when invalid or expired."""
    # Token source: config owns it (main() syncs config._bot_token at startup).
    _bot_token = config._bot_token
    if not init_data or not _bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
    except ValueError:
        return None
    given_hash = ""
    check_pairs = []
    for key, value in pairs:
        if key == "hash":
            given_hash = value
        else:
            check_pairs.append(f"{key}={value}")
    if len(given_hash) != 64:
        return None
    data_check_string = "\n".join(sorted(check_pairs))
    secret = hmac.new(b"WebAppData", _bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given_hash):
        return None
    parsed = dict(pairs)
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        return None
    if not auth_date or time.time() - auth_date > config.INIT_DATA_MAX_AGE:
        return None
    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return None
    return user


def _miniapp_user(request) -> Optional[dict]:
    """Extract + validate the 'tma <initData>' Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("tma "):
        return None
    return _validate_init_data(auth[4:].strip())


def _miniapp_chat_id(user_id: int) -> Optional[int]:
    """Private chats are keyed by the user's own id. Unknown users get None
    (callers 404/deny) — never map a user onto someone else's chat."""
    if user_id in storage.track_sessions:
        return user_id
    return None


# The 🌍 International view reports the cross-border lane: national posts that
# only 17TRACK can check (CN/HK and other countries' post offices). The rule
# itself lives in carriers.py so the Pro international allowance
# (pro.check_intl_limit) enforces exactly what this view shows.
def _is_international(info: dict) -> bool:
    return _carriers().is_international(info)


def _ship24():
    import ship24
    return ship24


def _package_payload(tracking_id: str, info: dict,
                     chat_id: Optional[int] = None) -> dict:
    """JSON shape for the Mini App — mirrors package_card() fields.
    chat_id (when known) enables the per-package next-check ETA."""
    return {
        "tracking_id": tracking_id,
        "name": info.get("name", "Package"),
        "status": info.get("status", "Pending"),
        "carrier": info.get("carrier", "unknown"),
        "carrier_name": info.get("carrier_name", "Unknown"),
        "carrier_url": info.get("carrier_url", ""),
        # Verified by 17TRACK — drives the international grouping client-side.
        "carrier_country": info.get("carrier_country", ""),
        "source": info.get("source", ""),
        "detail": info.get("detail", ""),
        # The package's first-person line (plain text; the app escapes on render).
        # Same server-side function the bot card uses — no copy table in JS — and
        # a lost package gets its memorial line here too.
        "character_line": _ui().character_line(
            tracking_id, info.get("name", "Package"),
            info.get("status", "Pending"),
            lost=info.get("lost") is True,
            lost_since=info.get("last_event_at") or info.get("last_updated")),
        "last_updated": info.get("last_updated", ""),
        "last_checked": info.get("last_checked", ""),
        "last_checked_ago": storage.time_since(info.get("last_checked") or info.get("last_updated")),
        "next_check_in": (_refresh().next_due_in(chat_id, tracking_id)
                          if chat_id is not None else None),
        "muted": bool(info.get("muted")),
        # Porch Guard: on unless the package was switched off. True (not
        # truthiness) so a file written before this field existed reads as armed.
        "porch_guard": bool(info.get("porch_guard", True)),
        # Display-only: "incoming" (default) or "outgoing". Every pre-existing
        # package reads as incoming.
        "direction": info.get("direction", "incoming"),
        "register_blocked": bool(info.get("register_blocked")),
        # The last lookup could not read the carrier, so the status above is the
        # last known one rather than a fresh reading. is True (not truthiness)
        # keeps files written before this field existed reading as not stale.
        "stale": info.get("last_lookup_failed") is True,
        "needs_pro": info.get("needs_pro") is True,
    }


# ──────────────────────────────────────────────
# Mini App HTTP handlers (aiohttp — served on MINI_APP_HOST:MINI_APP_PORT)
# ──────────────────────────────────────────────
async def _webapp_index(request):
    """Serve the static dashboard (webapp/index.html). No caching: iOS
    webviews aggressively cache pages, which would ship stale JS forever."""
    index = config.WEBAPP_DIR / "index.html"
    if not index.exists():
        return web.Response(status=404, text="webapp/index.html not found")
    return web.FileResponse(
        index,
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )


async def _webapp_debugging(request):
    """Serve the standalone debugging write-up (webapp/debugging.html).

    A public, static portfolio page linked from the operator's applications — no
    initData, no user data, no API. No-store for the same reason index.html uses
    it: an edited page must not be shadowed by a stale webview cache.
    """
    page = config.WEBAPP_DIR / "debugging.html"
    if not page.exists():
        return web.Response(status=404, text="webapp/debugging.html not found")
    return web.FileResponse(
        page,
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )


# Vendored assets are addressed by bare filename: the route must not be able to
# walk out of webapp/vendor/.
_VENDOR_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


async def _webapp_vendor(request):
    """Serve webapp/vendor/<name> — third-party JS from our own origin.

    A dedicated handler rather than add_static(): add_static() raises while the
    app is being BUILT when its directory is missing, so one absent static asset
    would take the whole Mini App down. Here it is just a 404, and the dashboard
    falls back to its own visible "QR unavailable" path.

    No-store for the same reason index.html uses it: an updated vendored file
    must not be shadowed by a stale iOS webview cache.
    """
    name = request.match_info.get("name", "")
    if not _VENDOR_NAME_RE.fullmatch(name):
        return web.Response(status=404, text="vendor asset not found")
    path = config.WEBAPP_DIR / "vendor" / name
    if not path.is_file():
        return web.Response(status=404, text="vendor asset not found")
    return web.FileResponse(
        path,
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )
def _tier_facts() -> dict:
    """The numbers the Pro sheet compares the two tiers with.

    The sheet used to spell the schedule out in its markup and drifted: it
    advertised 30/60-minute delivery-day checks while the poller ran 10/30. So the
    client asks for these instead, and a cadence change now reaches the copy on its
    own. The markup keeps baked-in values as a pre-load fallback, pinned to config
    by tests/test_miniapp_filters.py so even that fallback cannot lie.
    """
    cad = _refresh().CADENCE
    return {
        "free_packages": config.FREE_PACKAGE_LIMIT,
        "pro_packages": config.PRO_PACKAGE_LIMIT,
        "free_transit_min": cad["In Transit"][1] // 60,
        "pro_transit_min": cad["In Transit"][0] // 60,
        "free_ofd_min": cad["Out for Delivery"][1] // 60,
        "pro_ofd_min": cad["Out for Delivery"][0] // 60,
        "intl_included": config.PRO_INTL_LIMIT,
    }


async def _api_packages(request):
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    chat_id = _miniapp_chat_id(int(user["id"]))
    packages = storage.track_sessions.get(chat_id, {}) if chat_id is not None else {}
    items = [_package_payload(tid, info, chat_id)
             for tid, info in _ui().sort_packages(packages)]
    pro_flag = _pro().is_pro(chat_id) if chat_id is not None else False
    # Dashboard health strip: the same counters /status reads — but ONLY for
    # the operator. Quota headroom, Ship24 spend and sweep stats describe the
    # bot's global load, and this endpoint serves every authenticated user.
    # Everyone still gets next_check_in (top level): the client's poll loop
    # spaces itself by it, and it reveals cadence, not load.
    r = _refresh()
    is_owner = chat_id is not None and chat_id in config.OWNER_CHAT_IDS
    if is_owner:
        last_cycle = (r._parse_iso_epoch(refresh_at)
                      if (refresh_at := r.LAST_CYCLE_AT) else None)
        status = {
            "last_cycle_age_s": (max(0.0, time.time() - last_cycle)
                                 if last_cycle else None),
            "next_check_in": r.next_due_in(),
            "due_count": r.pending_due(),
            "sweep": dict(r.LAST_SWEEP),
            "quota": await _carriers().quota_snapshot(),
            "ship24_today": _ship24().calls_today(),
            "ship24_max": config.SHIP24_DAILY_MAX,
            "ship24_paused": _ship24().paused(),
        }
    else:
        status = None
    return web.json_response({
        "chat_id": chat_id,
        "packages": items,
        "status": status,
        "next_check_in": r.next_due_in(),
        "pro": pro_flag,
        "pro_expires_at": _pro()._pro_users.get(chat_id, 0.0) if pro_flag else 0,
        "pkg_limit": _pro().pkg_limit(chat_id or 0),
        "tier_facts": _tier_facts(),
        "retention_days": config.RETENTION_DAYS,
    })


async def _api_international(request):
    """Pro-only 🌍 International view: the cross-border lane (national posts
    only 17TRACK can check) plus the 17TRACK quota it consumes, so subscribers
    can see what their plan is paying for."""
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    chat_id = _miniapp_chat_id(int(user["id"]))
    if chat_id is None:
        return web.json_response({"error": "not found"}, status=404)
    if not _pro().is_pro(chat_id):
        return web.json_response({
            "error": "International tracking is a Pro feature.",
            "pro": False,
            "pro_only": True,
        }, status=403)
    packages = storage.track_sessions.get(chat_id, {})
    intl = [(tid, info) for tid, info in _ui().sort_packages(packages)
            if _is_international(info)]
    quota = await _carriers().quota_snapshot()
    return web.json_response({
        "pro": True,
        "source": "17TRACK",
        "packages": [_package_payload(tid, info) for tid, info in intl],
        "quota": quota,
        # The member's own allowance, straight from the registration tally —
        # the same number the add gate enforces, not the stored-package count.
        "intl_used": _pro().count_international(chat_id),
        "intl_total": config.PRO_INTL_LIMIT,
    })


async def _api_remove(request):
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    chat_id = _miniapp_chat_id(int(user["id"]))
    if chat_id is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tracking_id = _ui()._clean_tracking_id(str(body.get("tracking_id", "")))
    pkgs = storage.track_sessions.get(chat_id, {})
    if tracking_id not in pkgs:
        # Self-heal: the client's list is stale (package already gone).
        # Log what it tried, tell it to reload, don't leave a ghost card.
        # A count, not prefixes: six characters of a tracking number is still a
        # partial, and a count answers the same question ("was it stored at all?").
        logger.warning("MiniApp remove 404: chat=%s tid=%s stored=%d",
                       storage.mask_chat(chat_id),
                       _carriers()._sanitize_tracking_id(tracking_id), len(pkgs))
        return web.json_response({"error": "not found", "removed": False,
                                  "reload": True}, status=404)
    del pkgs[tracking_id]
    _refresh().save_now()
    # Only stop the upstream registration if no other chat still tracks it.
    await _carriers().stop_17track_if_unshared([tracking_id], chat_id)
    return web.json_response({"removed": tracking_id})


async def _api_mute(request):
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    chat_id = _miniapp_chat_id(int(user["id"]))
    if chat_id is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tracking_id = _ui()._clean_tracking_id(str(body.get("tracking_id", "")))
    pkgs = storage.track_sessions.get(chat_id, {})
    if tracking_id not in pkgs:
        return web.json_response({"error": "not found"}, status=404)
    pkgs[tracking_id]["muted"] = not pkgs[tracking_id].get("muted")
    _refresh().save_now()
    return web.json_response({"package": _package_payload(tracking_id, pkgs[tracking_id])})


async def _api_porch(request):
    """Set one package's Porch Guard flag (no Pro gate — same as the bot toggle).

    The app sends the state it wants rather than a flip, so a double-tap cannot
    land on the opposite of what the card shows.
    """
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    chat_id = _miniapp_chat_id(int(user["id"]))
    if chat_id is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tracking_id = _ui()._clean_tracking_id(str(body.get("tracking_id", "")))
    pkgs = storage.track_sessions.get(chat_id, {})
    if tracking_id not in pkgs:
        return web.json_response({"error": "not found"}, status=404)
    pkgs[tracking_id]["porch_guard"] = bool(body.get("enabled", True))
    _refresh().save_now()
    return web.json_response({"package": _package_payload(tracking_id, pkgs[tracking_id])})


async def _api_direction(request):
    """Set one package's direction (incoming / outgoing).

    Outgoing is a Pro feature: a free caller gets 403 rather than a silent
    no-op, and the app hides the control. Existing outgoing values on a
    downgraded member's packages are left alone — only setting is gated.
    """
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    chat_id = _miniapp_chat_id(int(user["id"]))
    if chat_id is None:
        return web.json_response({"error": "not found"}, status=404)
    if not _pro().is_pro(chat_id):
        return web.json_response({"error": "outgoing_pro_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tracking_id = _ui()._clean_tracking_id(str(body.get("tracking_id", "")))
    want = str(body.get("direction", "")).strip().lower()
    if want not in ("incoming", "outgoing"):
        return web.json_response({"error": "bad direction"}, status=400)
    pkgs = storage.track_sessions.get(chat_id, {})
    if tracking_id not in pkgs:
        return web.json_response({"error": "not found"}, status=404)
    pkgs[tracking_id]["direction"] = want
    _refresh().save_now()
    return web.json_response({"package": _package_payload(tracking_id, pkgs[tracking_id])})


async def _api_add(request):
    """Add a package from the Mini App: tracking number + optional name.
    Mirrors track_handler's register → fetch flow."""
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    user_id = int(user["id"])
    chat_id = _miniapp_chat_id(user_id)
    if chat_id is None:
        chat_id = user_id  # first-ever add straight from the app
        storage.track_sessions[chat_id] = {}
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tracking_id = _ui()._clean_tracking_id(str(body.get("tracking_id", "")))
    name = (str(body.get("name", "")) or "").strip()[:64]
    if not _ui()._is_tracking_like(tracking_id):
        return web.json_response(
            {"error": "That doesn't look like a tracking number."}, status=400)
    # Everything from the duplicate/limit checks to the insert is one atomic
    # step: the lookup in between awaits the network, and two adds for the same
    # chat interleaved across it both pass the checks and both insert (over the
    # cap, plus a duplicate 17TRACK registration). Same lock as the bot's /track.
    async with storage.get_chat_lock(chat_id):
        if tracking_id in storage.track_sessions[chat_id]:
            return web.json_response(
                {"error": "Already tracking this number."}, status=409)
        can_add, limit_err = _pro().check_package_limit(chat_id)
        if not can_add:
            return web.json_response(
                {"error": limit_err, "limit_reached": True, "pro": _pro().is_pro(chat_id)},
                status=403)
        allowed, intl_err = _pro().check_intl_limit(chat_id, [tracking_id])
        if not allowed:
            # intl_limit (not limit_reached): the app shows this as an
            # explanation rather than opening the upgrade sheet. For a Pro
            # caller that is right (already paying); for a free caller the
            # message itself carries the upsell. intl_total is the CALLER's
            # limit so the app can show "used out of X" honestly.
            pro_chat = _pro().is_pro(chat_id)
            return web.json_response(
                {"error": intl_err, "intl_limit": True, "pro": pro_chat,
                 "intl_used": _pro().count_international(chat_id),
                 "intl_total": (config.PRO_INTL_LIMIT if pro_chat
                                else config.FREE_INTL_LIMIT)},
                status=403)

        info, reg_result = await _refresh()._register_and_fetch(
            tracking_id, is_pro=_pro().is_pro(chat_id), chat_id=chat_id)
        info["name"] = name or f"{user.get('first_name', 'Your')}'s package"
        info["added_by"] = user_id
        # Server-enforced: only a Pro chat may add as outgoing. A free caller
        # asking for it gets incoming (the app hides the control anyway).
        info["direction"] = ("outgoing"
                             if (str(body.get("direction", "")).strip().lower()
                                 == "outgoing" and _pro().is_pro(chat_id))
                             else "incoming")
        storage.track_sessions[chat_id][tracking_id] = info
        _refresh().save_now()
    logger.info("MiniApp: added %s (register=%s)", _carriers()._sanitize_tracking_id(tracking_id), reg_result)
    return web.json_response({"package": _package_payload(tracking_id, info)})


async def _api_rename(request):
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    chat_id = _miniapp_chat_id(int(user["id"]))
    if chat_id is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tracking_id = _ui()._clean_tracking_id(str(body.get("tracking_id", "")))
    name = (str(body.get("name", "")) or "").strip()[:64]
    if not name:
        return web.json_response({"error": "Name can't be empty."}, status=400)
    pkgs = storage.track_sessions.get(chat_id, {})
    if tracking_id not in pkgs:
        return web.json_response({"error": "not found"}, status=404)
    pkgs[tracking_id]["name"] = name
    _refresh().save_now()
    return web.json_response({"package": _package_payload(tracking_id, pkgs[tracking_id])})
async def _api_create_invoice(request):
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not config.BOT_USDC_ADDRESS:
        return web.json_response({"error": "Payments not configured"}, status=503)
    chat_id = _miniapp_chat_id(int(user["id"]))
    if chat_id is None:
        return web.json_response({"error": "no chat"}, status=400)
    # Replace any earlier pending invoice from this chat — superseded, not
    # discarded: it stays matchable (grace pool) so a wallet still holding the
    # old amount is credited and the amount can't go to another chat meanwhile.
    for pid in [p for p, v in _pro()._pending_payments.items() if v["chat_id"] == chat_id]:
        _pro().park_invoice(pid, _pro()._pending_payments.pop(pid))
    # Random, not "chat-timestamp": two invoices in the same second used to share
    # a key, so the superseded one overwrote the new one (or was dropped from the
    # grace pool) and a paying buyer could lose the invoice that identified them.
    # The chat id stays inside the record — the key must not name the payer.
    payment_id = f"inv-{secrets.token_hex(8)}"
    # Unique amount = base price + a free random micro-tail. That amount is the
    # payer's identifier, so two buyers at the same time stay distinguishable
    # even without a memo. `taken` covers pending, grace-pooled and recently
    # consumed amounts, so no two live invoices can share a number.
    amount_micros, amount = _pro().allocate_amount(_pro().open_amounts())
    amount_str = f"{amount_micros / 1_000_000:.6f}"
    # The memo is only a wallet-side hint, so it must not carry the chat ID
    # (Solana memos are public forever) — the amount identifies the payer.
    memo_token = _pro().new_memo_token()
    # The reference is the payer's PRIMARY identifier: a Solana Pay-compatible
    # wallet puts it in the transaction, so the payment is recognised even when the
    # wallet rounds the amount (the exact-amount trick cannot survive that). The
    # micro-tail stays because amounts must stay unique for the _used_amounts
    # dedup and for invoices created before references existed.
    reference = _pro().new_reference()
    # The manual path: a buyer who copies THIS address (into an exchange, say) sends
    # no reference, no memo and a rounded amount, so the address itself is what
    # identifies them. Registered lazily so deposit_addrs.json stays the auditable
    # list of addresses this bot ever handed out.
    deposit_address = _pro().deposit_address(chat_id)
    if deposit_address:
        _pro().register_deposit_address(chat_id, deposit_address)
    # register_invoice persists immediately (it writes the whole pool, so the
    # parked invoice above is saved too): the buyer can pay within seconds, and
    # a restart in between must not lose the amount that identifies them.
    _pro().register_invoice(payment_id, {
        "chat_id": chat_id, "amount": amount, "amount_micros": amount_micros,
        "memo": memo_token, "reference": reference,
        # The price this invoice was created at: a later price change must not strand
        # a payment for this invoice (pro._active_price_bases reads it back).
        "base_micros": int(round(config.PRO_PRICE_USD * 1_000_000)),
        # Kept on the record so the public pay page can offer the manual route too:
        # a buyer inside a wallet whose browser ignores the reference then has a way
        # to be identified. None when the feature is off.
        "deposit_address": deposit_address, "created": time.time(),
    })
    logger.info("Solana invoice created: %s for chat=%s (%.6f USDC/USDT) — unique "
                "amount identifies the payer", _pro().mask_invoice_id(payment_id),
                storage.mask_chat(chat_id), amount)

    def _pay_url(mint: str) -> str:
        # The shared builder carries label/message/memo + this invoice's reference
        # (base58 is URL-safe, so it needs no encoding). One reference per invoice,
        # so USDC, USDT and the backward-compatible URL all carry the same one.
        return _solana_pay_url(amount_str, mint, memo_token, reference=reference)

    return web.json_response({
        "payment_id": payment_id,
        "wallet": config.BOT_USDC_ADDRESS,
        "amount": amount,
        # Exact string for display: formatting the float to 2 decimals client-side
        # would erase the very digits that identify this payment.
        "amount_str": amount_str,
        # Public on-chain anyway (it rides in the transaction), and it is how this
        # invoice's payment is recognised — useful in tests and for support.
        "reference": reference,
        # None when DEPOSIT_SEED is unset: the Mini App then shows the shared wallet
        # only, exactly as before this feature existed.
        "deposit_address": deposit_address,
        # The Phantom deep-link route, offered as a CHOICE beside the link: on a device
        # whose default handler for "solana:" is another wallet, this is how the payer
        # pays from the wallet they meant, with the transaction we built. None when the
        # route is switched off or when Phantom has no public https URL to return to.
        "phantom_url": (_phantom_page_url(payment_id)
                        if config.PHANTOM_DEEPLINK else None),
        "price_usd": config.PRO_PRICE_USD,
        "subscription_days": config.SUBSCRIPTION_DAYS,
        "expires_in": config.PAYMENT_EXPIRY_SECONDS,
        # The QR/link stays the primary path; the personal address is the manual one —
        # an exchange transfer sends whatever amount its form allows, and there the
        # address is what identifies the payer, so it must not demand exactness.
        "note": ("One tap opens your wallet with this amount already in it — the link "
                 "is recognized automatically."
                 if not deposit_address else
                 "One tap opens your wallet with this amount already in it — the link "
                 "is recognized automatically. Paying from an exchange? Send any amount "
                 "to your personal address."),
        # Canonical per-token Solana Pay URLs (a URI can carry one mint only)
        "usdc_url": _pay_url(config.USDC_MINT),
        "usdt_url": _pay_url(config.USDT_MINT),
        # Backward-compatible default (USDC)
        "solana_pay_url": _pay_url(config.USDC_MINT),
    })


async def _api_privacy(request):
    """The /privacy policy for the Mini App sheet — same text the bot sends, so
    the two can't drift apart (ui.PRIVACY_TEXT is the only copy)."""
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"html": _ui().PRIVACY_TEXT})


async def _api_share(request):
    """Mint the public share link for one package — the app's twin of the bot's
    Share button.

    Same semantics as the bot's share callback: the caller owns the chat (initData
    plus _miniapp_chat_id prove it, so no separate permission check), ONE live link
    per package — minting replaces the previous token, which is how a leaked URL
    dies — and the URL is a bearer token, so it travels over this signed channel
    and never lands in a chat. The share page itself allowlists what it shows and
    never carries the tracking number (miniapp._share_view).
    """
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    chat_id = _miniapp_chat_id(int(user["id"]))
    if chat_id is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tracking_id = _ui()._clean_tracking_id(str(body.get("tracking_id", "")))
    pkgs = storage.track_sessions.get(chat_id, {})
    if tracking_id not in pkgs:
        return web.json_response({"error": "not found"}, status=404)
    if not config.MINI_APP_URL:
        # No public URL configured: say so instead of minting a dead link
        # (same answer the bot's Share button gives).
        return web.json_response(
            {"error": "Share links aren't available right now"}, status=503)
    # Idempotent by default: the sheet SHOWS the link, so re-tapping Share must
    # not kill one the user may already have sent. A fresh token is minted only
    # when none exists or the sheet's explicit "New link" asked for it. (The bot's
    # Share keeps replace-on-tap — a chat message cannot be edited, so replacing
    # is its only revoke.)
    refresh = bool(body.get("refresh"))
    existing = share_links.live_for(chat_id, tracking_id)
    if existing and not refresh:
        token, replaced = existing, False
    else:
        token = share_links.mint(chat_id, tracking_id)
        replaced = existing is not None
        logger.info("Share link minted via the app for chat=%s (%s)",
                    storage.mask_chat(chat_id),
                    "replaced the previous link" if replaced else "first link")
    url = f"{config.MINI_APP_URL.rstrip('/')}/share/{token}"
    return web.json_response({
        "url": url,
        # 0 means the link never expires (the same "0 disables" convention as
        # SHARE_LINK_TTL_DAYS); the app phrases the sheet accordingly.
        "expires_in_days": config.SHARE_LINK_TTL_DAYS,
        "name": pkgs[tracking_id].get("name", "Package"),
        "replaced": replaced,
    })


async def _api_share_revoke(request):
    """Kill this chat's live share link for one package — the app's Revoke.

    The bot revokes with the button on its (ephemeral) share message; the app has
    no such message, so the sheet carries the revoke itself. False simply means
    there was nothing live — answering that plainly beats a 404 the sheet would
    have to interpret."""
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    chat_id = _miniapp_chat_id(int(user["id"]))
    if chat_id is None:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tracking_id = _ui()._clean_tracking_id(str(body.get("tracking_id", "")))
    if tracking_id not in storage.track_sessions.get(chat_id, {}):
        return web.json_response({"error": "not found"}, status=404)
    revoked = share_links.revoke_for(chat_id, tracking_id) > 0
    if revoked:
        logger.info("Share link revoked via the app for chat=%s",
                    storage.mask_chat(chat_id))
    return web.json_response({"revoked": revoked})


async def _api_check_payment(request):
    user = _miniapp_user(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    payment_id = request.query.get("id", "")
    target = _pro()._pending_payments.get(payment_id)
    if target is None:
        # Either paid (and removed) or expired — report pro state
        chat_id = _miniapp_chat_id(int(user["id"]))
        pro_flag = _pro().is_pro(chat_id) if chat_id is not None else False
        return web.json_response({"confirmed": pro_flag, "pro": pro_flag})
    if target["chat_id"] != _miniapp_chat_id(int(user["id"])):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"confirmed": False, "pro": False})


# One "dashboard moved" announcement per chat per 24h: the tunnel churn of
# 2026-09-26 delivered the same announcement three times in twelve minutes
# (18:01, 18:10, 18:13). In memory on purpose — the announce only fires when
# MINI_APP_URL actually changes, which is rare enough for a restart to reset.
_ANNOUNCE_MIN_GAP = 24 * 3600
_announced_at: Dict[int, float] = {}


# ──────────────────────────────────────────────
# API rate limiting (the backend is publicly reachable)
# ──────────────────────────────────────────────
# initData proves WHO the caller is, not that they are being reasonable: one
# valid initData can be replayed in a loop against /api/create_invoice or
# /api/check_payment, and every poll reaches the Solana RPC. Sliding window per
# Telegram user, in memory on purpose (a restart only relaxes a limit that
# exists to stop a runaway loop, not to bound a budget).
_rate_hits: Dict[int, List[float]] = {}
# How many distinct users the sliding-window table holds before it is dropped.
# A user who opens the app once leaves a key behind forever otherwise, and this
# one is keyed by USER (not by an invoice), so it grows with the user base
# rather than with traffic: 100k one-time users cost ~16 MB of resident memory
# for windows that expired minutes ago. _pay_tx_allowed caps its own table for
# exactly this reason; this is the same guard on the sibling table.
_RATE_USERS_MAX = 1024


def _rate_limited(user_id: int) -> bool:
    """Record one call and report whether this user is over budget."""
    now = time.monotonic()
    hits = [t for t in _rate_hits.get(user_id, [])
            if now - t < config.MINIAPP_RATE_WINDOW]
    if len(hits) >= config.MINIAPP_RATE_MAX:
        _rate_hits[user_id] = hits      # keep the pruned window, don't grow it
        return True
    hits.append(now)
    # Bounded memory: drop the whole table rather than evict selectively. Every
    # entry is a rate-limit WINDOW, not an allowance or a receipt — the read
    # above already discards expired timestamps, so an entry older than the
    # window is indistinguishable from a missing key and clearing cannot change
    # any verdict. It only relaxes a throttle that exists to stop a runaway
    # loop, and only for users who are not currently over budget.
    if len(_rate_hits) > _RATE_USERS_MAX and user_id not in _rate_hits:
        _rate_hits.clear()
    _rate_hits[user_id] = hits
    return False


# Unauthenticated public surfaces have no initData to key a budget on, so they
# key on the client address: the 17TRACK webhook rebuilds an O(store) table per
# POST and the /pay pages scan the receipt trail per unknown id, so an
# anonymous flood is CPU amplification that grows with the user base
# (2026-10-04 audit). Coarse outer gate — the per-invoice /pay budgets on
# /connect and /sign stay theirs. Budgets are module constants on purpose:
# they are a DoS guard, not an operator tuning knob.
_IP_RATE_BUDGETS = (("/webhook/", 30), ("/pay/", 60))
_IP_RATE_WINDOW = 60.0
_IP_BUCKETS_MAX = 4096
_ip_rate_hits: Dict[str, List[float]] = {}


def _client_ip(request: web.Request) -> str:
    """The address to budget on. Behind the tunnel every direct peer is the
    proxy itself, so the trusted proxy's own addition to X-Forwarded-For (the
    LAST hop) is the client — spoofed earlier hops are ignored."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.remote or "unknown"


def _ip_rate_limited(bucket: str, ip: str, budget: int) -> bool:
    """One sliding window per (surface, client address) — the same discipline
    as _rate_limited, including its bounded-memory tradeoff."""
    now = time.monotonic()
    key = bucket + ip
    hits = [t for t in _ip_rate_hits.get(key, [])
            if now - t < _IP_RATE_WINDOW]
    if len(hits) >= budget:
        _ip_rate_hits[key] = hits      # keep the pruned window, don't grow it
        return True
    hits.append(now)
    if len(_ip_rate_hits) > _IP_BUCKETS_MAX and key not in _ip_rate_hits:
        _ip_rate_hits.clear()
    _ip_rate_hits[key] = hits
    return False


@web.middleware
async def _api_no_store(request, handler):
    """Two response headers every route gets, on one pass.

    Cache-Control/Pragma: the Telegram webview (WKWebView) caches same-URL GETs
    by default, which made a refetched /api/packages serve a stale body - the
    dashboard then only updated after remove + re-add. The HTML page already
    sets this; the data behind it must too.

    Referrer-Policy: `no-referrer` because the dashboard links out to the
    carrier's own tracking page, and that URL carries the tracking number. With
    no policy the browser still sent a Referer header, so clicking the link
    handed the carrier our address (the tunnel host) on top of the number it
    already has. `rel="noreferrer"` covers the same ground for the document's
    links; this pins it for every page we serve, share and pay included.
    """
    resp = await handler(request)
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@web.middleware
async def _api_rate_limit(request, handler):
    """Pre-gate for /api/ and /share/: spend the caller's budget before the
    handler runs.

    Views keep their own auth check — this middleware only adds the limit, so a
    route added later cannot forget it. An unauthenticated request is passed
    through so the views still answer their own 401 (which also means the
    anonymous /share/ visitors this gate now covers are not budgeted: only
    callers with a valid initData are — 256-bit tokens are the real defense
    there).
    """
    if not (request.path.startswith("/api/")
            or request.path.startswith("/share/")):
        for prefix, budget in _IP_RATE_BUDGETS:
            if request.path.startswith(prefix):
                ip = _client_ip(request)
                if _ip_rate_limited(prefix, ip, budget):
                    logger.warning("MiniApp rate limit (ip): %s exceeded %d "
                                   "calls / %ds on %s", ip, budget,
                                   int(_IP_RATE_WINDOW), request.path)
                    return web.json_response(
                        {"error": "too many requests"}, status=429)
                break
        return await handler(request)
    user = _miniapp_user(request)
    if user is None:
        return await handler(request)
    user_id = int(user["id"])
    if _rate_limited(user_id):
        logger.warning("MiniApp rate limit: %s exceeded %d calls / %ds on %s",
                       storage.mask_chat(user_id), config.MINIAPP_RATE_MAX,
                       int(config.MINIAPP_RATE_WINDOW), request.path)
        return web.json_response({"error": "too many requests"}, status=429)
    return await handler(request)


# ── Public share pages ────────────────────────────────────────────────────
# A share link is a bearer token: whoever holds the URL sees this one package's
# name, status and latest update. Unknown, expired, revoked and
# package-deleted all answer the SAME page, byte for byte, so the URL cannot be
# probed for which packages exist or which links are still alive.
_SHARE_404_PAGE = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>Link not available</title>"
    "</head><body style=\"margin:0;background:#06090f;color:#f2f5f7;"
    "font:15px/1.45 -apple-system,system-ui,sans-serif;display:flex;"
    "align-items:center;justify-content:center;min-height:100vh\">"
    "<div style=\"max-width:380px;padding:28px;text-align:center\">"
    "<div style=\"font-size:38px\">\U0001f512</div>"
    "<h1 style=\"font-size:18px;margin:12px 0 8px\">Link not available</h1>"
    "<p style=\"color:#8b98a5;margin:0\">This link is unknown, expired, or was "
    "revoked. Ask the person who shared it for a new one.</p>"
    "</div></body></html>"
)


def _share_view(info: dict, tracking_id: str) -> dict:
    """The allowlist a share page may show, built field by field on purpose:
    handing the renderer the stored record would leak the tracking number (and
    whatever the next feature adds to it) the first time nobody looked.

    character_line() takes the real id for its md5 variant pick — the id is
    used here and never stored in the view, so it cannot reach the page.
    """
    name = info.get("name", "Package")
    status = info.get("status", "Pending")
    return {
        "name": name,
        "status": status,
        "badge": _ui().status_badge(status),
        "carrier_name": info.get("carrier_name", "Unknown"),
        "detail": info.get("detail", ""),
        "character_line": _ui().character_line(
            tracking_id, name, status,
            lost=info.get("lost") is True,
            lost_since=info.get("last_event_at") or info.get("last_updated")),
        "last_updated": storage.format_ts(info.get("last_updated")),
    }


def _share_page(view: dict) -> str:
    """Minimal server-rendered page: inline CSS, no JS, no external requests.
    Colours are the app's own palette so a shared page looks like the card the
    owner sees."""
    detail = (f"<p style=\"margin:14px 0 0\">{escape(view['detail'])}</p>"
              if view["detail"] else "")
    voice = (f"<p style=\"margin:12px 0 0;color:#8b98a5;font-style:italic\">"
             f"{escape(view['character_line'])}</p>"
             if view["character_line"] else "")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex\">"
        f"<title>{escape(view['name'])} — package status</title></head>"
        "<body style=\"margin:0;background:#06090f;color:#f2f5f7;"
        "font:15px/1.45 -apple-system,system-ui,sans-serif;display:flex;"
        "justify-content:center;padding:22px\">"
        "<main style=\"width:100%;max-width:420px;background:#0b1119;"
        "border:1px solid rgba(255,255,255,.08);border-top:3px solid #ff7a18;"
        "border-radius:16px;padding:20px\">"
        f"<h1 style=\"font-size:19px;margin:0 0 10px\">{escape(view['name'])}</h1>"
        f"<p style=\"margin:0\">{escape(view['badge'])} "
        f"<b>{escape(view['status'])}</b></p>"
        f"{voice}"
        f"{detail}"
        "<p style=\"margin:16px 0 0;color:#8b98a5;font-size:13px\">"
        f"{escape(view['carrier_name'])} · updated "
        f"{escape(view['last_updated'])}</p>"
        "<p style=\"margin:12px 0 0;color:#5f6f7d;font-size:12px\">"
        "Shared from a package tracker. The tracking number is never shown "
        "here.</p>"
        "</main></body></html>"
    )


# ── Public pay page (the wallet's in-app browser lands here) ──────────────
# Why this page exists: Solana Pay's own mechanism for a PRE-FILLED payment sheet
# is the URI scheme — wallets register to handle it, and the parameters in the URI
# are what a wallet composes the transaction from. No wallet offers a "pay this
# URL" deep link, so the flow is: the Mini App opens the wallet at THIS page (a
# universal link), and the page — now running inside the wallet's in-app browser —
# offers a tap that hands the solana: URI over. The wallet then shows its own sheet
# with recipient, amount, token, label and message already filled in.
#
# The handoff must stay the payer's TAP, and the manual facts must be on screen
# before it. "solana:" is claimed by every Solana wallet (the spec has wallets
# register the scheme), so a webview navigation to it is resolved by the operating
# system — not by the wallet the payer chose. On a device whose default handler is a
# different wallet (Coinbase Wallet registers the scheme too) that app opens an empty
# send screen and the amount is lost: reported from the field. A tap cannot stop the
# misroute, but it means the payer has already seen the address and the amount on
# this page, and is back on them if control leaves.
#
# The payment_id is the whole authentication: inv- + 16 hex chars, unguessable, and
# already the capability token /api/check_payment accepts. So this route sits
# outside /api/ with no initData check — and it renders an ALLOWLIST: the amount,
# the wallet address, the two Solana Pay URIs and the expiry sentence. No chat id,
# no package data, nothing that identifies the payer.
_PAY_EXPIRED_PAGE = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>Payment link expired</title>"
    "</head><body style=\"margin:0;background:#06090f;color:#f2f5f7;"
    "font:15px/1.45 -apple-system,system-ui,sans-serif;display:flex;"
    "align-items:center;justify-content:center;min-height:100vh\">"
    "<div style=\"max-width:380px;padding:28px;text-align:center\">"
    "<div style=\"font-size:38px\">\u23f3</div>"
    "<h1 style=\"font-size:18px;margin:12px 0 8px\">This payment link has expired</h1>"
    "<p style=\"color:#8b98a5;margin:0\">Reopen Packey and start a new upgrade.</p>"
    "</div></body></html>"
)

# Shown when the invoice was CREDITED and the payer comes back to the link: it left
# both pools at that moment, so the expired page used to answer a payment that worked.
_PAY_PAID_PAGE = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<meta name=\"robots\" content=\"noindex\"><title>Payment received</title>"
    "</head><body style=\"margin:0;background:#06090f;color:#f2f5f7;"
    "font:15px/1.45 -apple-system,system-ui,sans-serif;display:flex;"
    "align-items:center;justify-content:center;min-height:100vh\">"
    "<div style=\"max-width:380px;padding:28px;text-align:center\">"
    "<div style=\"font-size:38px\">\u2705</div>"
    "<h1 style=\"font-size:18px;margin:12px 0 8px\">Payment received</h1>"
    "<p style=\"color:#8b98a5;margin:0\">Pro is active &mdash; reopen Packey.</p>"
    "</div></body></html>"
)


def _solana_pay_url(amount_str: str, mint: str, memo_token: str,
                    reference: Optional[str] = None) -> str:
    """One Solana Pay URI: recipient, amount, SPL token, label, message, memo.

    `label` and `message` are what the wallet's prefilled sheet shows, so both stay
    short and human. `reference` is appended when the invoice carries one (the
    reference-matching change): the API response and this page share this single
    builder, so a later edit cannot add a parameter to one path and drop it here.
    """
    url = (f"solana:{config.BOT_USDC_ADDRESS}"
           f"?amount={amount_str}&spl-token={mint}"
           f"&label=Packey%20Pro&message=Pro%20upgrade"
           f"&memo={memo_token}")
    if reference:
        url += f"&reference={reference}"
    return url


# $ placeholders only (string.Template): the page is full of CSS and JS braces, so a
# brace-based template would need escaping in every rule and every function body.
_PAY_PAGE = Template("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Pay for Packey Pro</title>
<style>
  /* Same design language as the Mini App sheet: one accent, one radius set, the
     system font stack (the app loads no webfont — privacy), so a payer moving
     between the sheet and this page sees one product. */
  :root { color-scheme: dark;
    --bg:#06090f; --card:#0b1119; --text:#f2f5f7; --muted:#8b98a5;
    --line:rgba(255,255,255,.08); --field:rgba(255,255,255,.06);
    --accent:#2f6bff; --accent-soft:#4f8cff;
    --font:-apple-system,system-ui,"Segoe UI",Roboto,sans-serif;
    --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
    --r-sm:10px; --r-md:14px; --r-lg:16px; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.45 var(--font); }
  .wrap { max-width:400px; margin:0 auto; padding:26px 20px 40px; }
  h1 { font-size:15px; color:var(--muted); font-weight:600; margin:0 0 6px; }
  .amt { font-size:38px; font-weight:700; letter-spacing:-0.5px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 18px; }
  .toks { display:flex; gap:8px; margin:0 0 16px; }
  .tok { flex:1; padding:10px 0; border-radius:var(--r-md); text-align:center;
         border:1px solid var(--line); background:var(--field); color:var(--text);
         font:inherit; font-weight:600; cursor:pointer; }
  .tok.on { background:var(--accent); border-color:transparent; color:#fff; }
  .qr { background:#fff; border-radius:var(--r-lg); padding:16px; display:flex;
        align-items:center; justify-content:center; min-height:230px; }
  .addr { margin:14px 0; padding:12px; border-radius:var(--r-sm); background:var(--field);
          border:1px solid var(--line); font-family:var(--mono);
          font-size:11.5px; word-break:break-all; }
  .copy { width:100%; padding:12px 0; border-radius:var(--r-md); border:1px solid var(--line);
          background:transparent; color:var(--text); font:inherit; font-weight:600;
          cursor:pointer; margin-bottom:10px; }
  .pay { display:block; text-align:center; text-decoration:none; padding:15px 0;
         border-radius:var(--r-md); background:var(--accent); color:#fff; font-weight:700;
         font-size:16px; }
  .status { margin-top:14px; color:var(--muted); font-size:13px; min-height:34px; }
  .diag { margin-top:12px; font-family:ui-monospace,Menlo,monospace; font-size:11.5px;
          color:#7d8b9a; line-height:1.5; word-break:break-all; }
  .hide { display:none; }
</style></head><body><div class="wrap">
  <h1>Pay for Packey Pro</h1>
  <p class="amt">$amount_str</p>
  <p class="sub">$expiry_text</p>
  <div class="toks">
    <button class="tok on" id="tUsdc">USDC</button>
    <button class="tok" id="tUsdt">USDT</button>
  </div>
  $phantom_block
  <div id="auto" class="hide">
    <button class="pay" id="hop">Pay</button>
  </div>
  <div id="manual">
    <div class="qr" id="qr"></div>
    <div class="addr" id="addr">$wallet</div>
    <button class="copy" id="copy">Copy address</button>
    <button class="copy" id="copyAmt">Copy amount</button>
    $deposit_block
    <div class="status" id="st">$note</div>
  </div>
</div>
<script defer src="/vendor/qrcode.min.js"></script>
<script>
(function () {
  var urls = { USDC: "$usdc_url", USDT: "$usdt_url" };
  var token = "USDC";
  var auto = document.getElementById("auto");
  var st = document.getElementById("st");
  var qr = document.getElementById("qr");

  function draw() {
    // The QR is the whole point of this page's fallback: it encodes the same Solana Pay
    // URI the button hands over, for a wallet on a second device.
    var u = urls[token];
    qr.innerHTML = "";
    try {
      if (window.QRCode && typeof QRCode.toCanvas === "function") {
        var c = document.createElement("canvas");
        qr.appendChild(c);
        QRCode.toCanvas(c, u, { width: 200, margin: 2,
          errorCorrectionLevel: "H",
          color: { dark: "#111111", light: "#ffffff" } },
          function (e) {
            if (e) qr.textContent = "QR unavailable — copy the address";
          });
      } else {
        qr.textContent = "QR unavailable — copy the address";
      }
    } catch (_) { qr.textContent = "QR unavailable — copy the address"; }
  }

  function pick() {
    document.getElementById("tUsdc").classList.toggle("on", token === "USDC");
    document.getElementById("tUsdt").classList.toggle("on", token === "USDT");
    // The Phantom page has no picker of its own, and asking it to pay a token this
    // wallet does not hold is the one refusal the payer cannot fix — so the choice
    // travels with the link (the plain href still works with no JavaScript at all).
    var go = document.getElementById("phantomGo");
    if (go && go.dataset.base) {
      go.setAttribute("href", go.dataset.base + "?token=" + token);
    }
    draw();
  }
  document.getElementById("tUsdc").onclick = function () { token = "USDC"; pick(); };
  document.getElementById("tUsdt").onclick = function () { token = "USDT"; pick(); };
  function copyText(v, ok) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(v).then(function () { st.textContent = ok; },
                                            function () { st.textContent = ok; });
    } else { st.textContent = ok; }
  }

  document.getElementById("copy").onclick = function () {
    copyText(document.getElementById("addr").textContent, "Address copied.");
  };
  // The exact amount is what identifies the payer, so it must be obtainable without
  // re-reading a 38px number off a phone screen.
  document.getElementById("copyAmt").onclick = function () {
    copyText(document.querySelector(".amt").textContent,
             "Amount copied — paste it as the exact amount.");
  };
  // The personal address block is absent when the invoice has none (an invoice from
  // before that feature, or DEPOSIT_SEED unset), so the handler is guarded.
  var copyDep = document.getElementById("copyDep");
  if (copyDep) {
    copyDep.onclick = function () {
      copyText(document.getElementById("depAddr").textContent,
               "Personal address copied.");
    };
  }

  draw();

  // ── The payment action ──────────────────────────────────────────────────────
  // One action: open the Solana Pay link. The URI carries the recipient, amount, token,
  // memo and reference and the payer's own wallet reads all of it, so nothing here has
  // to know which app claims the "solana:" scheme and no wallet is asked to read a page
  // of ours. This page is reached when a payer opens the payment link in a browser,
  // which is why the escape hatch stays on screen: the QR for a second device, and the
  // exact address and amount for a wallet that cannot read links at all.
  function openExternal(url) {
    try {
      if (window.Telegram && Telegram.WebApp &&
          typeof Telegram.WebApp.openLink === "function") {
        Telegram.WebApp.openLink(url);
        return;
      }
    } catch (_) {}
    location.href = url;
  }

  var hop = document.getElementById("hop");
  var amount = document.querySelector(".amt").textContent;
  hop.textContent = "Open " + amount + " in my wallet";
  auto.classList.remove("hide");

  hop.onclick = function () {
    hop.disabled = true;
    st.textContent = "Opening your wallet…";
    openExternal(urls[token]);
    setTimeout(function () {
      hop.disabled = false;
      if (!document.hidden) {
        st.textContent = "Nothing opened — scan the QR, or send " + amount
                       + " to the address below.";
      }
    }, 2000);
  };
})();
</script></body></html>""")


async def _webapp_pay(request):
    """Public pay page: the payment_id in the URL is the whole authentication.

    Renders only the invoice's public payment facts (see the section comment): no
    chat id, no package data, nothing about the payer.
    """
    payment_id = request.match_info.get("payment_id", "")
    invoice = _pro()._pending_payments.get(payment_id)
    parked = False
    if invoice is None:
        # Superseded or past its 30-minute window. The amount stays matchable for a
        # day on purpose (a wallet may still be holding it), so the page keeps
        # working — 404-ing it would turn away a payment that WOULD be credited.
        # An id that is in neither pool gets the honest dead-link page.
        invoice = _pro()._expired_payments.get(payment_id)
        parked = invoice is not None
    if invoice is None:
        # Credited already? Then this link did its job — saying "expired" to a payer who
        # just paid reads as failure (2026-09-25).
        if _pro().payment_was_credited(payment_id):
            return web.Response(text=_PAY_PAID_PAGE, content_type="text/html")
        return web.Response(text=_PAY_EXPIRED_PAGE, content_type="text/html",
                            status=404)
    return web.Response(text=_pay_page(invoice, parked=parked, payment_id=payment_id),
                        content_type="text/html")


# How many transaction builds one invoice may ask for per rate window. Public by
# design (the payment_id is the capability) and every call costs RPC round trips, so
# a runaway loop must not become free load — but the payer legitimately retries after
# a rejected prompt, so the budget is a handful, not one.
_PAY_TX_PER_WINDOW = 6
_pay_tx_hits: Dict[str, List[float]] = {}


def _pay_tx_allowed(payment_id: str) -> bool:
    """Spend one invoice's transaction-build budget; report whether it had any left."""
    now = time.monotonic()
    hits = [t for t in _pay_tx_hits.get(payment_id, [])
            if now - t < config.MINIAPP_RATE_WINDOW]
    if len(hits) >= _PAY_TX_PER_WINDOW:
        _pay_tx_hits[payment_id] = hits
        return False
    hits.append(now)
    # Bounded memory: this dict is keyed by invoice id, and a visitor who never pays
    # would otherwise leave a key behind forever. Dropping the whole table is
    # harmless (the real defense is the 128-bit id plus the pool lookup).
    if len(_pay_tx_hits) > 256 and payment_id not in _pay_tx_hits:
        _pay_tx_hits.clear()
    _pay_tx_hits[payment_id] = hits
    return True


# What a payer is told when a route cannot be prepared. The code is for us; the message
# is what reaches their screen, and a real sentence is the point — the first version
# showed the bare word "unavailable", which told nobody anything (reported 2026-09-24).
_PHANTOM_REFUSAL = {
    "no_redirect": {
        "error": "no_redirect",
        "message": "This link cannot open Phantom. Send to the address below with this "
                   "exact amount instead."},
    "already_connected": {
        "error": "already_connected",
        "message": "A wallet is already connected to this invoice. Continue on that "
                   "device, or send to the address below with this exact amount."},
    "no_payer_account": {
        "error": "no_payer_account",
        "message": "This wallet holds no USDC or USDT on Solana. Fund it first, or "
                   "switch to the wallet you pay from and start over."},
    "no_receiving_account": {
        "error": "no_receiving_account",
        "message": "We cannot receive that token right now. Send to the address below "
                   "with this exact amount."},
    "no_blockhash": {
        "error": "no_blockhash",
        "message": "The Solana network is not answering. Try again in a moment, or send "
                   "to the address below."},
    "unsupported_decimals": {
        "error": "unsupported_decimals",
        "message": "That token cannot be invoiced. Try the other one, or send to the "
                   "address below."},
    "bad_amount": {
        "error": "bad_amount",
        "message": "This invoice cannot be paid — reopen Packey for a fresh one."},
    "unbuildable": {
        "error": "unbuildable",
        "message": "This payment could not be prepared. Send to the address below with "
                   "this exact amount."},
    "other": {
        "error": "unavailable",
        "message": "This payment could not be prepared. Send to the address below with "
                   "this exact amount."},
}


def _invoice_by_id(payment_id: str) -> Optional[dict]:
    """The invoice behind a payment_id, open or parked — or None.

    Both pools: an invoice past its 30-minute window stays matchable for a day, so
    refusing its payment routes would turn away a payment that WOULD be credited.
    """
    return (_pro()._pending_payments.get(payment_id)
            or _pro()._expired_payments.get(payment_id))


async def _json_body(request) -> dict:
    """A request body as a dict; anything else (no body, bad JSON) reads as empty."""
    try:
        body = await request.json()
    except Exception:
        body = None
    return body if isinstance(body, dict) else {}


def _phantom_page_url(payment_id: str) -> Optional[str]:
    """The deep-link page's own URL, or None when we cannot offer it.

    Phantom requires an absolute https URL for a redirect we want to receive ourselves:
    a custom scheme would go back to a native app, and our "app" is Telegram. MINI_APP_URL
    is that URL; without it the route cannot work, so it is not offered at all.
    """
    base = (config.MINI_APP_URL or "").strip()
    if not base.startswith("https://"):
        return None
    return f"{base.rstrip('/')}/pay/{payment_id}/phantom"


def _phantom_redirect(payment_id: str, step: str,
                      token: Optional[str] = None) -> Optional[str]:
    """Where Phantom must send the payer back to: the page, marked with the step.

    The token rides along on the connect hop: Phantom answers by appending its params to
    redirect_link, so a query param the page was opened with (the payer's USDC/USDT
    choice) is gone by the time the later sign request is built — and asking for a token
    the wallet does not hold is the one refusal the payer cannot fix by retrying.
    """
    page = _phantom_page_url(payment_id)
    if page is None:
        return None
    return f"{page}?step={step}" + (f"&token={token}" if token else "")


# ── The Phantom deep-link route (the no-browser rescue) ──────────────────────
# Why it exists: the in-browser handoff on /pay/<id> depends on the wallet injecting a
# provider into OUR page — and on the wallet being the one the payer chose, since
# "solana:" is answered by whichever wallet the operating system likes. The reported
# failure (2026-09-24: a payer landed in Coinbase Wallet with an empty send screen) is
# exactly one of those two. Phantom's deep-link API has neither dependency:
# "phantom://v1/…" is answerable by Phantom alone, and the request carries the
# transaction we already built, so the wallet composes nothing at all.
#
# Two round trips are inherent to that API (connect, then sign), so the payer returns to
# this page twice: once to be connected, once to have signed. The session lives in
# memory in phantom_link and expires; the redirect carries only "step", never a token.
async def _pay_phantom_connect(request):
    """Start a session and hand back the connect links (scheme first, URL second)."""
    if not config.PHANTOM_DEEPLINK:
        return web.json_response({"error": "disabled"}, status=404)
    payment_id = request.match_info.get("payment_id", "")
    if _invoice_by_id(payment_id) is None:
        return web.json_response({"error": "unknown payment"}, status=404)
    if not _pay_tx_allowed(payment_id):
        return web.json_response({"error": "too many requests"}, status=429)
    # The payer's token choice is read here so the connect hop can carry it back: the
    # auto-advance builds the sign request from the return URL, where this page's own
    # query params no longer exist.
    body = await _json_body(request)
    token = str(body.get("token") or "").upper()
    redirect = _phantom_redirect(payment_id, "connect",
                                 token if token in config.ACCEPTED_MINTS.values()
                                 else None)
    if redirect is None:
        # No public https URL configured: a redirect could never come back to us.
        return web.json_response(_PHANTOM_REFUSAL["no_redirect"], status=409)
    record = phantom_link.start_session(payment_id)
    if record is None:
        # A connected session must not be replaced: the payment_id is a public
        # capability, so this is whoever-is-asking protection for the payer
        # already mid-flow (2026-10-04 audit).
        return web.json_response(_PHANTOM_REFUSAL["already_connected"], status=409)
    return web.json_response(phantom_link.connect_links(
        record["dapp_public"], redirect, config.MINI_APP_URL or ""))


async def _pay_phantom_session(request):
    """Decrypt Phantom's connect answer into the session (payer key + session token)."""
    if not config.PHANTOM_DEEPLINK:
        return web.json_response({"error": "disabled"}, status=404)
    payment_id = request.match_info.get("payment_id", "")
    if _invoice_by_id(payment_id) is None:
        return web.json_response({"error": "unknown payment"}, status=404)
    body = await _json_body(request)
    record = None
    try:
        record = phantom_link.complete_connect(
            payment_id, str(body.get("phantom_encryption_public_key") or ""),
            str(body.get("nonce") or ""), str(body.get("data") or ""))
    except ValueError as e:
        logger.warning("Phantom connect response rejected: %s", e)
        return web.json_response({"error": "the connect answer could not be read"},
                                 status=400)
    # The payer's PUBLIC key goes back with the ok: the page shows it truncated (so a
    # payer who connected the wrong wallet finds out now, not at the sign step), and the
    # auto-advance path needs it without a reload. Nothing secret leaves here.
    return web.json_response({"ok": True,
                              "payer": str((record or {}).get("payer") or "")})


async def _pay_phantom_sign(request):
    """Build the payment transaction for the connected payer and encrypt it."""
    if not config.PHANTOM_DEEPLINK:
        return web.json_response({"error": "disabled"}, status=404)
    payment_id = request.match_info.get("payment_id", "")
    invoice = _invoice_by_id(payment_id)
    if invoice is None:
        return web.json_response({"error": "unknown payment"}, status=404)
    if not _pay_tx_allowed(payment_id):
        return web.json_response({"error": "too many requests"}, status=429)
    record = phantom_link.get_session(payment_id)
    if record is None or not record.get("session"):
        return web.json_response({"error": "not connected"}, status=409)
    body = await _json_body(request)
    token = str(body.get("token") or "USDC").upper()
    mint = {"USDC": config.USDC_MINT, "USDT": config.USDT_MINT}.get(token)
    if mint is None:
        return web.json_response({"error": "unknown token"}, status=400)
    redirect = _phantom_redirect(payment_id, "sign")
    if redirect is None:
        return web.json_response(_PHANTOM_REFUSAL["no_redirect"], status=409)
    # The payer's key comes from the session, not from the page: it is the key Phantom
    # named in the connect answer, and the transaction's fee payer and authority must be
    # that key or the wallet cannot sign it.
    payer = str(record["payer"])
    # One token, then the other: a payer whose wallet holds USDT should not be stopped by
    # a choice made on another screen, and the invoice's amount means the same in either
    # (the wallet shows the token it is asked to sign, so nothing is hidden).
    switched = False
    if not await _pro().payer_can_pay(payer, mint):
        other = config.USDT_MINT if mint == config.USDC_MINT else config.USDC_MINT
        if await _pro().payer_can_pay(payer, other):
            mint, switched = other, True
            token = config.ACCEPTED_MINTS[other]
    transaction, reason = await _pro().payment_transaction_parts(invoice, payer, mint)
    if not transaction:
        logger.warning("Phantom sign refused for %s: %s", token, reason)
        return web.json_response(_PHANTOM_REFUSAL.get(reason, _PHANTOM_REFUSAL["other"]),
                                 status=409)
    # Fingerprint the unsigned transaction's message so a wallet that modified
    # what it was asked to sign can be caught at submit time. The message
    # starts at byte 65 (shortvec(1) + the one empty 64-byte signature slot)
    # and is identical in the signed version — only the signature bytes change.
    record["tx_msg_hash"] = hashlib.sha256(transaction[65:]).hexdigest()
    links = phantom_link.sign_transaction_links(
        record, solana_tx.b58encode(transaction), redirect)
    return web.json_response({**links, "token": token, "switched": switched})


async def _pay_phantom_submit(request):
    """Decrypt the signed transaction, submit it ourselves, report the signature."""
    if not config.PHANTOM_DEEPLINK:
        return web.json_response({"error": "disabled"}, status=404)
    payment_id = request.match_info.get("payment_id", "")
    if _invoice_by_id(payment_id) is None:
        return web.json_response({"error": "unknown payment"}, status=404)
    record = phantom_link.get_session(payment_id)
    if record is None:
        return web.json_response({"error": "not connected"}, status=409)
    body = await _json_body(request)
    try:
        signed = phantom_link.decode_sign_response(
            record, str(body.get("nonce") or ""), str(body.get("data") or ""))
    except ValueError as e:
        logger.warning("Phantom sign response rejected: %s", e)
        return web.json_response({"error": "the signed transaction could not be read"},
                                 status=400)
    # Verify the wallet did not modify the transaction: the message portion
    # (bytes after the signature count and the one signature slot) must match
    # what the server built. The session holds a SHA-256 fingerprint of it,
    # computed at sign-request time.
    tx_hash = record.get("tx_msg_hash")
    if not tx_hash:
        # /sign is what arms that fingerprint. A session that reaches /submit
        # without one has nothing to verify the bytes against — and /submit
        # must never relay an unverified transaction, so refuse instead of
        # submitting whatever arrived.
        logger.warning("Phantom submit without a prior sign for %s", payment_id)
        return web.json_response({
            "error": "This payment session was never signed — start over "
                     "from the app. The address below always works."},
            status=409)
    try:
        signed_bytes = solana_tx.b58decode(signed)
        if hashlib.sha256(signed_bytes[65:]).hexdigest() != tx_hash:
            logger.warning("Phantom tx mismatch for %s: wallet modified the "
                           "transaction it was asked to sign",
                           payment_id)
            return web.json_response({
                "error": "Your wallet changed the payment details — please "
                         "start over from the app. The address below always works."},
                status=409)
    except (ValueError, IndexError):
        logger.warning("Phantom tx unreadable for %s: could not verify integrity",
                       payment_id)
        return web.json_response({
            "error": "The signed transaction could not be verified — start over "
                     "from the app, or use the address below."},
            status=400)
    signature = await _pro().submit_signed_transaction(signed)
    if not signature:
        return web.json_response({"error": "the network refused the transaction"},
                                 status=409)
    return web.json_response({"ok": True, "signature": signature})


async def _webapp_phantom(request):
    """The deep-link page: connect, sign, and come back here with each answer.

    Public like the pay page and for the same reason: the payment_id IS the capability
    (128 random bits), and this page renders only the invoice's public payment facts.
    """
    if not config.PHANTOM_DEEPLINK:
        return web.Response(status=404, text="Not found")
    payment_id = request.match_info.get("payment_id", "")
    invoice = _invoice_by_id(payment_id)
    if invoice is None:
        # A credited invoice leaves both pools; the payer who paid must not be told the
        # link expired (display-only check — matching still lives in the pools).
        if _pro().payment_was_credited(payment_id):
            return web.Response(text=_PAY_PAID_PAGE, content_type="text/html")
        return web.Response(text=_PAY_EXPIRED_PAGE, content_type="text/html", status=404)
    record = phantom_link.get_session(payment_id)
    connected = bool(record and record.get("session"))
    return web.Response(text=phantom_page.render(
        invoice, config.BOT_USDC_ADDRESS, connected,
        payer=str(record.get("payer") or "") if connected else "",
        minutes=max(1, int(config.PAYMENT_EXPIRY_SECONDS // 60))),
                        content_type="text/html")


async def _pay_transaction(request):
    """Build the unsigned payment transaction the wallet's in-app browser will sign.

    Why this route exists: the page cannot rely on handing the wallet a `solana:` URI,
    because that scheme is claimed by every Solana wallet and the operating system
    picks the handler — a payer defaulting to another wallet got that wallet's empty
    send screen and lost the amount. A transaction has no scheme in it: the page gives
    it to the provider that is already injected in that browser, so the recipient,
    amount, mint and memo are ours. No key is involved anywhere: the response is a
    transaction with an empty signature slot.

    Public by design, exactly like the pay page: the payment_id (128 random bits) is
    the whole capability. The response carries the invoice's public payment facts in
    binary form and nothing about the payer — their own public key is what they send
    us, and a public key identifies nobody in this system.
    """
    payment_id = request.match_info.get("payment_id", "")
    invoice = (_pro()._pending_payments.get(payment_id)
               or _pro()._expired_payments.get(payment_id))
    if invoice is None:
        return web.json_response({"error": "unknown payment"}, status=404)
    if not _pay_tx_allowed(payment_id):
        return web.json_response({"error": "too many requests"}, status=429)
    try:
        body = await request.json()
    except Exception:
        body = None
    body = body if isinstance(body, dict) else {}
    token = str(body.get("token") or "USDC").upper()
    mint = {"USDC": config.USDC_MINT, "USDT": config.USDT_MINT}.get(token)
    if mint is None:
        return web.json_response({"error": "unknown token"}, status=400)
    payer = str(body.get("payer") or "")
    try:
        # Checked here as well as in the builder: a malformed key must not reach the
        # RPC, and must never end up as an account in a transaction.
        solana_tx.pubkey(payer)
    except ValueError:
        return web.json_response({"error": "bad payer"}, status=400)
    transaction, reason = await _pro().payment_transaction_parts(invoice, payer, mint)
    if not transaction:
        # No token account for one of the two sides, no blockhash, or an amount we
        # cannot express: the page offers the manual route, which needs none of them.
        # 409, not 500 — the payer is not doing anything wrong and may well succeed by
        # other means. The message names the reason: a bare "unavailable" tells nobody.
        return web.json_response({**_PHANTOM_REFUSAL.get(reason, _PHANTOM_REFUSAL["other"]),
                                  "token": token}, status=409)
    return web.json_response({"transaction": solana_tx.to_base64(transaction),
                              "token": token})


def _pay_page(invoice: dict, parked: bool = False, payment_id: str = "") -> str:
    """Render the pay page for one invoice — allowlisted fields only.

    `payment_id` is only for the Phantom button's own URL: this page is the path a
    payer reaches when their wallet would not open, which is precisely the case the
    deep-link route exists for — so it leads with it rather than mentioning it at the
    bottom (the button it used to be was a footnote, and the route it pointed at is
    the one that works on iOS).
    """
    amount_str = f"{float(invoice.get('amount') or 0):.6f}"
    memo = invoice.get("memo", "")
    # The manual route (an exchange transfer carries no reference and no memo, so the
    # address itself is the identifier). Absent for invoices written before the field
    # existed, or when DEPOSIT_SEED is unset — then the block renders as nothing.
    deposit = str(invoice.get("deposit_address") or "")
    deposit_block = "" if not deposit else (
        '<p class="sub" style="margin-top:18px">Paying from an exchange? Send any '
        "amount of <b>USDC</b> or <b>USDT</b> on Solana to your own address below &mdash; "
        "the address itself identifies you.</p>"
        f'<div class="addr" id="depAddr">{escape(deposit)}</div>'
        '<button class="copy" id="copyDep">Copy my personal address</button>')
    minutes = max(1, int(config.PAYMENT_EXPIRY_SECONDS // 60))
    expiry_text = (
        "This amount was reserved earlier — a payment to it still counts for 24 "
        "hours, but reopen Packey for a fresh one." if parked else
        f"Pay within {minutes} minutes. Wallets that read payment links identify "
        f"you automatically; any other wallet should send exactly this amount — "
        f"the last digits identify you.")
    # The Phantom route IS this page's primary action: a payer lands here when the
    # in-browser wallet handoff failed, and the deep link has none of that handoff's
    # dependencies. data-base lets the token picker carry the choice into the link
    # without a second URL; the plain href covers JavaScript being off.
    phantom_url = (_phantom_page_url(payment_id)
                   if config.PHANTOM_DEEPLINK and payment_id else None)
    phantom_block = "" if not phantom_url else (
        f'<a class="pay" id="phantomGo" data-base="{escape(phantom_url)}" '
        f'href="{escape(phantom_url)}">Pay in the Phantom app</a>')
    return _PAY_PAGE.substitute(
        amount_str=amount_str,
        wallet=config.BOT_USDC_ADDRESS,
        expiry_text=expiry_text,
        note="One tap opens your wallet with this amount already in it — or scan the QR "
             "from another device.",
        deposit_block=deposit_block,
        phantom_block=phantom_block,
        # reference= rides along the moment the invoice carries one (the
        # reference-matching change): the builder already supports it, so this page
        # cannot silently drop the param later.
        usdc_url=_solana_pay_url(amount_str, config.USDC_MINT, memo,
                                 reference=invoice.get("reference")),
        usdt_url=_solana_pay_url(amount_str, config.USDT_MINT, memo,
                                 reference=invoice.get("reference")),
    )


async def _webapp_share(request):
    """Public status page for one package — the token in the URL is the auth."""
    record = share_links.lookup(request.match_info.get("token", ""))
    info = None
    if record is not None:
        try:
            chat_id = int(record.get("chat_id", 0))
        except (TypeError, ValueError):
            chat_id = None
        tracking_id = record.get("tracking_id")
        if chat_id is not None and tracking_id:
            info = storage.track_sessions.get(chat_id, {}).get(tracking_id)
    if info is None:
        # Unknown / expired / revoked / package since deleted: one page, so the
        # URL never reveals which of those it was.
        return web.Response(text=_SHARE_404_PAGE, content_type="text/html",
                            status=404)
    return web.Response(text=_share_page(_share_view(info, tracking_id)),
                        content_type="text/html")


def _build_miniapp_app() -> web.Application:
    app = web.Application(middlewares=[_api_no_store, _api_rate_limit])
    app.router.add_get("/", _webapp_index)
    # Public static write-up (no initData, no data, no API): a portfolio artifact
    # the operator links from job applications. Deliberately outside /api/.
    app.router.add_get("/debugging", _webapp_debugging)
    # Vendored third-party JS, served from our own origin (webapp/vendor/README).
    app.router.add_get("/vendor/{name}", _webapp_vendor)
    # Public share pages: the token in the URL is the whole authentication (no
    # initData exists for a gift's recipient), so this route is deliberately not
    # under /api/.
    app.router.add_get("/share/{token}", _webapp_share)
    # Same reasoning as /share/: the payment_id IS the capability token, so there is
    # no initData to check and it deliberately sits outside /api/.
    app.router.add_get("/pay/{payment_id}", _webapp_pay)
    app.router.add_post("/pay/{payment_id}/tx", _pay_transaction)
    # The deep-link rescue route: no provider injection, no "solana:" scheme.
    app.router.add_get("/pay/{payment_id}/phantom", _webapp_phantom)
    app.router.add_post("/pay/{payment_id}/phantom/connect", _pay_phantom_connect)
    app.router.add_post("/pay/{payment_id}/phantom/session", _pay_phantom_session)
    app.router.add_post("/pay/{payment_id}/phantom/sign", _pay_phantom_sign)
    app.router.add_post("/pay/{payment_id}/phantom/submit", _pay_phantom_submit)
    app.router.add_get("/api/packages", _api_packages)
    app.router.add_get("/api/international", _api_international)
    app.router.add_get("/api/privacy", _api_privacy)
    app.router.add_post("/api/create_invoice", _api_create_invoice)
    app.router.add_get("/api/check_payment", _api_check_payment)
    app.router.add_post("/api/add", _api_add)
    app.router.add_post("/api/rename", _api_rename)
    app.router.add_post("/api/remove", _api_remove)
    app.router.add_post("/api/mute", _api_mute)
    app.router.add_post("/api/porch", _api_porch)
    app.router.add_post("/api/direction", _api_direction)
    app.router.add_post("/api/share", _api_share)
    app.router.add_post("/api/share/revoke", _api_share_revoke)
    # 17TRACK push events (registered in their console as
    # <MINI_APP_URL>/webhook/17track/<WEBHOOK_TOKEN>). Not /api/: no initData
    # exists for a carrier's server, so the rate-limit middleware correctly skips
    # it. The path carries the secret segment — an unguessable URL is the
    # authentication, because the console has no field for a signature or
    # header. With no token configured the route is simply NOT registered:
    # serving the public path anyway let anyone who knew a tracked number burn
    # shared 17TRACK quota and CPU (2026-10-04 audit). Pushes are a latency
    # optimization — the poller stays the authority — so a missing token costs
    # freshness, not correctness.
    if config.WEBHOOK_TOKEN:
        hook = f"/webhook/17track/{config.WEBHOOK_TOKEN}"
        app.router.add_get(hook, _webhook_17track_verify)
        app.router.add_post(hook, _webhook_17track)
    else:
        logger.warning("17TRACK webhook DISABLED: WEBHOOK_TOKEN is not set — "
                       "set it in .env and update the console URL to "
                       "/webhook/17track/<token> to re-enable pushes.")
    return app


# ── 17TRACK webhook receiver ─────────────────────────────────────────────
# Their console posts tracking events here. This iteration is CAPTURE-FIRST:
# it answers 200 immediately (which also stops 17TRACK's ~15 s retry loop on
# the 404s the route's absence caused) and logs a masked STRUCTURAL
# fingerprint of each payload — field names, types and sizes, never values —
# so the real handler is written against the measured schema, not guesses.
# The URL carries no secret (the console has no token field), so the anti-
# spoof posture for the future handler is: only numbers already in our store
# are actionable, and the payload is never logged raw.

async def _webhook_17track_verify(request):
    """Console verification ping — must answer 200 for registration."""
    return web.json_response({"ok": True})


def _payload_shape(value, depth: int = 0):
    """Structure-only view of a JSON value: names, types, sizes — no content."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {str(k): _payload_shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        head = [_payload_shape(v, depth + 1) for v in value[:3]]
        return head + ([f"…{len(value) - len(head)} more"] if len(value) > 3 else [])
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, str):
        return f"str({len(value)})"
    if value is None:
        return "null"
    return type(value).__name__


def _collect_strings(value, out: list, depth: int = 0) -> None:
    """Every string in a JSON tree (the webhook schema is unverified, so the
    number hunt walks everything rather than trusting field names)."""
    if depth > 8:
        return
    if isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _collect_strings(v, out, depth + 1)
    elif isinstance(value, str):
        out.append(value)


async def _webhook_17track(request):
    try:
        data = json.loads(await request.read())
    except (ValueError, UnicodeDecodeError):
        logger.info("17TRACK webhook: non-JSON payload (%s bytes)",
                    request.content_length or 0)
        return web.json_response({"ok": True})
    logger.info("17TRACK webhook capture: %s", _payload_shape(data))
    # Anti-spoof: the URL carries no token (the console has no field for one),
    # so an unauthenticated POST must not be able to invent work. Only numbers
    # this bot ALREADY tracks are actionable — everything else is counted and
    # dropped. Case/whitespace tolerant: 17TRACK normalizes some formats.
    import refresh
    known = {}
    for chat_id, packages in storage.track_sessions.items():
        for tid in packages:
            known.setdefault(tid.strip().lower(), tid)
    strings: list = []
    _collect_strings(data, strings)
    scheduled = throttled = terminal = 0
    touched = set()
    for s in strings:
        real = known.get(s.strip().lower())
        if real and real not in touched:
            touched.add(real)
            counts = refresh.mark_due_now(real)
            scheduled += counts["matched"]
            throttled += counts["skipped_throttled"]
            terminal += counts["skipped_terminal"]
    logger.info("17TRACK webhook: %d payload string(s), %d matched a tracked "
                "number → %d due now (%d inside the reschedule floor, %d already "
                "delivered)", len(strings), len(touched), scheduled, throttled,
                terminal)
    # Counts only, and none of them about OUR data: `received` echoes the number
    # of strings the caller itself sent. The scheduled/throttled/terminal counts
    # stay in the log line above, which is private.
    #
    # `matched` used to be returned too, and that made this an ORACLE: one POST
    # carrying a single candidate number came back matched=1 if this bot tracks
    # it and matched=0 if it does not — a membership probe over the whole
    # tracking store, from an endpoint with no auth (17TRACK's console has no
    # token field). It is deliberately absent, and must not be re-added as a
    # "harmless count": any field that varies with whether a hit occurred
    # re-opens the probe. The behaviour is unchanged — only the reply.
    return web.json_response({"ok": True, "received": len(strings)})


def _miniapp_url_state_path():
    # Sibling of the other state files; owned by miniapp.py.
    from pathlib import Path
    return Path("mini_app_url.json")


def load_miniapp_url_state() -> Optional[str]:
    """Last MINI_APP_URL the bot announced with. None when never recorded."""
    path = _miniapp_url_state_path()
    if not path.exists():
        return None
    try:
        text, _was_enc = storage._read_protected(path)
        if text is None:
            return None
        data = json.loads(text)
        url = (data.get("mini_app_url") or "").strip() or None
        return url
    except (ValueError, TypeError, OSError) as e:
        logger.warning("Could not load mini_app_url.json: %s", e)
        return None


def save_miniapp_url_state(url: Optional[str]) -> None:
    """Record the announced MINI_APP_URL (encrypted at rest, like the
    other state files). A missing/empty URL clears the record."""
    path = _miniapp_url_state_path()
    try:
        storage._write_protected(path, json.dumps({"mini_app_url": url or ""}))
    except OSError as e:
        logger.warning("Could not save mini_app_url.json: %s", e)


async def announce_miniapp_url(bot, url: str) -> int:
    """Send one fresh "Open the app" button to every chat holding packages.

    Call when MINI_APP_URL changed since the last announcement: each
    message carries the CURRENT url, so stale buttons (baked into older
    messages at send time) self-heal. Returns the number of chats messaged.

    Rate-limited per chat: the tunnel churn of 2026-09-26 delivered the same
    announcement three times in twelve minutes (18:01, 18:10, 18:13) — one
    announcement per chat per 24h keeps the self-heal without the spam.
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    sent = 0
    skipped = 0
    chats = sorted(storage.track_sessions.keys())
    for chat_id in chats:
        if time.time() - _announced_at.get(chat_id, 0.0) < _ANNOUNCE_MIN_GAP:
            skipped += 1
            continue
        try:
            await bot.send_message(
                chat_id,
                "📦 <b>Packey dashboard moved</b> — tap below for the current app link:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Open the app", web_app=WebAppInfo(url=url)),
                ]]),
            )
            _announced_at[chat_id] = time.time()
            sent += 1
        except Exception as e:
            logger.warning("Mini App URL announcement failed for chat=%s: %s",
                           storage.mask_chat(chat_id), e)
    if sent and not skipped:
        # Record only when EVERY chat got it: a rate-limited chat keeps stale
        # buttons, and leaving the state unrecorded makes the next startup retry
        # the skipped ones instead of assuming they hold a working button.
        save_miniapp_url_state(url)
    if skipped:
        logger.info("Mini App URL announcement: %d chat(s) inside the 24h "
                    "rate limit — they will get it on the next start", skipped)
    logger.info("📣 Mini App URL announced to %d chat(s): %s", sent, url)
    return sent


def maybe_announce_miniapp_url(bot):
    """Startup helper: announce only when MINI_APP_URL exists AND differs
    from the last announced one. Returns a coroutine to await, or None."""
    url = (config.MINI_APP_URL or "").strip() or None
    if not url:
        return None
    if load_miniapp_url_state() == url:
        return None
    if not storage.track_sessions:
        # Nothing to point at yet — record silently so the first package
        # add doesn't trigger a redundant announcement later.
        save_miniapp_url_state(url)
        return None
    return announce_miniapp_url(bot, url)