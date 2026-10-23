"""Quick standalone check that TRACK17_API_KEY is valid.
Run: python3 check_17track_key.py
"""
import os
import sys

import requests

api_key = os.getenv("TRACK17_API_KEY")
if not api_key:
    print("TRACK17_API_KEY is not set in your environment.")
    sys.exit(1)

resp = requests.post(
    "https://api.17track.net/track/v2.4/getquota",
    headers={"17token": api_key, "Content-Type": "application/json"},
    json=[],
    timeout=10,
)

data = resp.json()
if data.get("code") == 0:
    q = data["data"]
    print("✅ Key is valid.")
    print(f"   Quota total:     {q['quota_total']}")
    print(f"   Quota used:      {q['quota_used']}")
    print(f"   Quota remaining: {q['quota_remain']}")
else:
    print(f"❌ Key check failed: {data}")
