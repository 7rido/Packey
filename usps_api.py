"""USPS Tracking API v3 — the new developer platform (developers.usps.com).

Replaces the legacy Web Tools XML API (USPS_API_USERID) and makes the
Akamai-gated browser tier unnecessary for USPS numbers.

Setup (one-time, ~10 min):
  1. https://cop.usps.com/cop-navigator?wf=API — create/sign in to a USPS
     Business Account
  2. My Apps -> Create App (Tracking is in the default product)
  3. Copy the Consumer Key + Consumer Secret into .env:
       USPS_CONSUMER_KEY=...
       USPS_CONSUMER_SECRET=...

Auth: OAuth2 client-credentials. Tokens are cached in-process and refreshed
60s before expiry; a 401 forces one re-auth and retry.
"""
import logging
import os
import time
from typing import Optional, Tuple

import httpx

LOGGER = logging.getLogger("tracker")


def _mask(tracking_id: str) -> str:
    """Mask a tracking number for logs (9400...3527). The helper lives in
    carriers.py; imported lazily so this module stays importable on its own."""
    from carriers import _sanitize_tracking_id
    return _sanitize_tracking_id(tracking_id)


TOKEN_URL_PATH = "/oauth2/v3/token"
TRACKING_PATH = "/tracking/v3/tracking"

_token: Optional[str] = None
_token_expires: float = 0.0


def _base_url() -> str:
    """Production base; override via USPS_API_BASE_URL for the TEM sandbox."""
    return (os.getenv("USPS_API_BASE_URL")
            or "https://apis.usps.com").rstrip("/")


def _cred(key: str) -> str:
    """Read credentials at call time so .env edits apply on restart without
    import-order surprises."""
    return (os.getenv(key) or "").strip()


def enabled() -> bool:
    return bool(_cred("USPS_CONSUMER_KEY") and _cred("USPS_CONSUMER_SECRET"))


def _reset_token() -> None:
    global _token, _token_expires
    _token = None
    _token_expires = 0.0


async def _get_token(client: httpx.AsyncClient) -> Optional[str]:
    """Fetch (and cache) an OAuth2 client-credentials token."""
    global _token, _token_expires
    if _token and time.time() < _token_expires:
        return _token
    try:
        resp = await client.post(
            f"{_base_url()}{TOKEN_URL_PATH}",
            json={
                "client_id": _cred("USPS_CONSUMER_KEY"),
                "client_secret": _cred("USPS_CONSUMER_SECRET"),
                "grant_type": "client_credentials",
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            LOGGER.warning("USPS API: token response had no access_token: %s",
                           str(data)[:160])
            return None
        ttl = int(data.get("expires_in") or 3600)
        _token = token
        _token_expires = time.time() + max(60, ttl - 60)
        LOGGER.info("USPS API: OAuth token acquired (valid %ds)", ttl)
        return token
    except Exception as e:
        LOGGER.warning("USPS API: token request failed: %s", e)
        return None


async def check_usps(tracking_id: str,
                     client: httpx.AsyncClient) -> Optional[Tuple[str, str]]:
    """Look up a tracking number. Returns (statusSummary, statusCategory) —
    raw USPS text; the caller maps it onto the bot's status vocabulary.
    Returns None on any failure (caller falls through to other sources)."""
    if not enabled():
        return None
    token = await _get_token(client)
    if not token:
        return None
    url = f"{_base_url()}{TRACKING_PATH}/{tracking_id}"
    for attempt in (1, 2):
        try:
            resp = await client.get(
                url,
                params={"expand": "SUMMARY"},
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/json"},
            )
            if resp.status_code == 401 and attempt == 1:
                # Token revoked/expired mid-life → re-auth once and retry.
                _reset_token()
                token = await _get_token(client)
                if not token:
                    return None
                continue
            if resp.status_code == 404:
                LOGGER.info("USPS API: %s not found yet (pre-scan?)",
                            _mask(tracking_id))
                return None
            resp.raise_for_status()
            data = resp.json()
            summary = data.get("trackingSummary") or data
            text = (summary.get("statusSummary") or "").strip()
            if not text:
                # The response body is not logged: it can carry the recipient's
                # city/state, and the number is what correlates the log line.
                LOGGER.info("USPS API: no statusSummary for %s", _mask(tracking_id))
                return None
            return text, (summary.get("statusCategory") or "").strip()
        except Exception as e:
            if attempt == 2:
                LOGGER.warning("USPS API lookup failed for %s: %s",
                               _mask(tracking_id), e)
            return None
    return None
