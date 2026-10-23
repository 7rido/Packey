"""test_main_fixes.py — early smoke test for migrations + permission logic."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/fredp/Documents/PrivatePackageTracker")
import main

# --- Test 1: legacy schema migration ---
tmp = Path("/tmp/legacy_test.json")
tmp.write_text(json.dumps({
    "250444893": {
        "TID1": {"name": "A", "status": "Unavailable", "status_source": "none",
                 "lookup_ok": False, "diagnostic": "old msg"},
        "TID2": {"name": "B", "status": "In Transit", "status_source": "17TRACK",
                 "lookup_ok": True},
        "TID3": {"name": "C", "status": "Awaiting First Scan"},
    }
}))
main.DATA_FILE = tmp
main.load_data()
pkgs = main.track_sessions[250444893]
assert "status_source" not in pkgs["TID1"] and "lookup_ok" not in pkgs["TID1"]
assert "source" not in pkgs["TID1"]
assert pkgs["TID2"]["source"] == "17TRACK"
assert pkgs["TID3"]["status"] == "Pending" and pkgs["TID3"]["register_blocked"] is True
print("migration: OK")

# --- Test 2: permission logic (no network needed) ---
class Chat:
    def __init__(self, cid, ctype):
        self.id, self.type = cid, ctype


class User:
    def __init__(self, uid):
        self.id = uid


class Msg:
    def __init__(self, chat, user):
        self.chat, self.from_user = chat, user


priv = Msg(Chat(1, "private"), User(42))
assert asyncio.run(main.can_manage_package(priv, "TID1")) is True

main.track_sessions = {2: {"TID1": {"added_by": 42}}}
grp_owner = Msg(Chat(2, "group"), User(42))
grp_other = Msg(Chat(2, "group"), User(99))
assert asyncio.run(main.can_manage_package(grp_owner, "TID1")) is True
assert asyncio.run(main.can_manage_package(grp_other, "TID1")) is False
assert asyncio.run(main.can_manage_package(grp_other, "NOPE")) is False
print("permissions: OK")
print("ALL TESTS PASSED")