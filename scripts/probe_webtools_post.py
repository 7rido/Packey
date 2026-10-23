"""probe_webtools_post.py — one-shot probe for the USPS Web Tools TrackV2 transport.

Proves the 2026-10-04 audit fix: the TrackV2 request's XML — USERID/Password
included — travels in the form-urlencoded POST BODY, so no credentialed URL
ever exists for httpx's INFO request log, an exception message, or an upstream
access log to record. Nothing here is imported by the bot.

The old (leaky) transport is only BUILT, never sent: the probe asserts its URL
embeds the credentials and prints that fact WITHOUT ever showing the URL. The
new transport's URL is printed in full — it is credential-free by construction.

Usage (from the project root, which is where .env lives):
  python3 scripts/probe_webtools_post.py 9400...
"""
import asyncio
import sys
from urllib.parse import unquote
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import carriers
import config


def _demo_xml() -> str:
    """The same shape _check_usps_webtools.build_xml() produces — real
    credentials go in, nothing is printed."""
    attrs = f'USERID="{config.USPS_API_USERID}"'
    if config.USPS_API_PASSWORD:
        attrs += f' Password="{config.USPS_API_PASSWORD}"'
    return f'<TrackRequest {attrs}><TrackID ID="probe"></TrackID></TrackRequest>'


async def main() -> None:
    tid = sys.argv[1]
    masked = carriers._sanitize_tracking_id(tid)
    xml = _demo_xml()
    client = carriers.http_client()

    # 1. The old transport, BUILT but never sent: credentials land in the URL.
    old_req = client.build_request(
        "POST", config.USPS_WEBTOOLS_URL,
        params={"API": "TrackV2", "XML": xml})
    old_url = str(old_req.url)
    print(f"old transport (params=): USERID in URL: {'USERID=' in unquote(old_url)}"
          f"  (URL withheld; Password leg: "
          f"{'set' if config.USPS_API_PASSWORD else 'not configured'})")

    # 2. The new transport: the same XML goes in the body; the URL stays clean.
    new_req = client.build_request(
        "POST", config.USPS_WEBTOOLS_URL,
        data={"API": "TrackV2", "XML": xml},
        headers={"User-Agent": carriers.random_user_agent()})
    new_url = str(new_req.url)
    print(f"new transport (data=):   USERID in URL: "
          f"{'USERID=' in unquote(new_url) or 'Password=' in unquote(new_url)}")
    print(f"new transport request URL: {new_url}")
    print(f"new transport content-type: {new_req.headers.get('content-type')}")
    try:
        body = (await new_req.aread()).decode()
        print(f"new transport body carries the XML: {'USERID=' in unquote(body)}")
    except Exception as e:
        print(f"new transport body pre-read: {type(e).__name__} "
              f"(content-type above is the contract)")

    # 3. Live lookup through the edited function — only when the tier is on.
    if not config.USPS_API_USERID:
        print("live TrackV2 lookup: SKIPPED — no USPS_API_USERID in .env, the "
              "Web Tools tier is disabled in this deployment")
        return
    status = await carriers._check_usps_webtools(tid)
    if status:
        print(f"live TrackV2 lookup for {masked}: OK — {status[:200]!r}")
    else:
        print(f"live TrackV2 lookup for {masked}: None "
              f"(no result — check the auth-backoff window / logs)")


if __name__ == "__main__":
    asyncio.run(main())
