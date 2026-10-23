"""scripts/rekey_dek.py — the DATA_ENCRYPTION_KEY rotation tool.

The tool re-encrypts every state file under a new 64-hex key and rewrites
.env, with rails: refuse while the bot runs, refuse without a fresh backup,
read-everything-before-writing-anything (phase 1), round-trip verify every
rewrite (phase 2), .env last with a rollback copy. Tests exercise each rail
against a seeded temp workspace — the real run needs a stopped bot and YOUR
key, which must never exist in this suite (or anywhere in the repo).
"""
import json
from pathlib import Path

import pytest

import config
import storage
from scripts.rekey_dek import (bot_running, main, newest_backup_age_hours,
                               rekey_all, state_files, update_env_file,
                               validate_new_key)

OLD_KEY = "old-secret-that-was-only-24-chars!"
NEW_KEY = "e" * 64
CHAT = 7


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A seeded mini-install: .env with the old key, one envelope store with a
    live session, plus a single-layer state file. Paths redirected here."""
    monkeypatch.setattr(config, "_bot_token", None, raising=False)
    monkeypatch.setattr(config, "DATA_ENCRYPTION_KEY", OLD_KEY)
    data = tmp_path / "track_data.json"
    monkeypatch.setattr(storage, "track_sessions", {CHAT: {
        "9400111206200074888350": {"name": "Box", "status": "In Transit"}}})
    storage.save_data(data)
    cooldown = tmp_path / "register_cooldown.json"
    storage._write_protected(cooldown, json.dumps({"9400111206200074888350": 1.0}))
    env = tmp_path / ".env"
    env.write_text(f"DATA_ENCRYPTION_KEY={OLD_KEY}\nTELEGRAM_BOT_API_TOKEN=1:t\n")
    monkeypatch.setattr(config, "DATA_FILE", data)
    return {"tmp": tmp_path, "data": data, "cooldown": cooldown, "env": env}


def test_happy_path_rotates_every_layer(workspace):
    """After the rotation every file opens under the NEW key (including the
    per-chat DEKs inside the envelope container), .env carries the new key,
    and the rollback copy holds the old one."""
    data, cooldown, env = workspace["data"], workspace["cooldown"], workspace["env"]
    counts = rekey_all([data, cooldown], OLD_KEY, NEW_KEY)
    assert counts["rekeyed"] == 2 and counts["envelope"] == 1

    config.DATA_ENCRYPTION_KEY = NEW_KEY          # what .env now says
    text, was_enc = storage._read_protected(data)
    assert was_enc
    parsed = storage._parse_container(json.loads(text))
    assert parsed[CHAT]["9400111206200074888350"]["name"] == "Box"
    text2, _ = storage._read_protected(cooldown)
    assert json.loads(text2) == {"9400111206200074888350": 1.0}

    pre = update_env_file(env, NEW_KEY)
    assert pre.name == ".env.pre-rekey" and pre.exists()
    assert f"DATA_ENCRYPTION_KEY={NEW_KEY}" in env.read_text()
    assert f"DATA_ENCRYPTION_KEY={OLD_KEY}" in pre.read_text()
    assert "TELEGRAM_BOT_API_TOKEN=1:t" in env.read_text(), "unrelated lines survive"


def test_deks_really_move_off_the_old_key(workspace):
    """The 2026-09-16 outage shape: outer layer re-keyed while a DEK stays
    sealed by the old master. The rotation must leave NO DEK openable by the
    old key."""
    data = workspace["data"]
    rekey_all([data], OLD_KEY, NEW_KEY)
    obj = json.loads(storage._decrypt_with(NEW_KEY, data.read_bytes()))
    for envelope in obj[storage._ENVELOPE_KEY].values():
        dek_new = storage._unwrap_dek(storage._fernet_for(NEW_KEY),
                                      envelope["dek"])
        assert dek_new is not None, "a DEK was not re-wrapped to the new key"
        assert storage._unwrap_dek(storage._fernet_for(OLD_KEY),
                                   envelope["dek"]) is None, \
            "a DEK still opens under the OLD key"


def test_validate_accepts_hex_or_long_passphrase():
    assert validate_new_key("a" * 64, OLD_KEY) == "a" * 64
    assert validate_new_key("correct horse battery staple 9999", OLD_KEY), \
        "a 32+ character passphrase is a valid new key"
    for bad in ("short", "a" * 31, OLD_KEY[:24], ""):
        with pytest.raises(ValueError):
            validate_new_key(bad, OLD_KEY)          # sub-32-char, non-hex: the old shape
    with pytest.raises(ValueError):
        validate_new_key(OLD_KEY, OLD_KEY)          # same key: nothing to rotate


def test_phase1_failure_aborts_before_any_write(workspace):
    """One unreadable file aborts the whole run: phase 1 writes nothing, so
    every file still opens under the OLD key afterwards."""
    broken = workspace["tmp"] / "broken.json"
    broken.write_bytes(b"gAAAA" + b"junk" * 8)      # ciphertext nobody opens
    config.DATA_ENCRYPTION_KEY = OLD_KEY
    with pytest.raises(RuntimeError, match="cannot be read under the current key"):
        rekey_all([workspace["data"], broken, workspace["cooldown"]],
                  OLD_KEY, NEW_KEY)
    assert broken.exists(), "read-only phase 1 must not rename the broken file"
    config.DATA_ENCRYPTION_KEY = OLD_KEY
    text, _ = storage._read_protected(workspace["data"])
    assert json.loads(text), "the store was touched despite the abort"


def test_bot_running_and_stale_backup_refuse(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "DATA_ENCRYPTION_KEY", OLD_KEY)
    monkeypatch.setattr("scripts.rekey_dek.bot_running", lambda: True)
    monkeypatch.setattr("scripts.rekey_dek.newest_backup_age_hours",
                        lambda: 1.0)
    assert main([]) == 2, "a running bot must refuse"

    monkeypatch.setattr("scripts.rekey_dek.bot_running", lambda: False)
    monkeypatch.setattr("scripts.rekey_dek.newest_backup_age_hours",
                        lambda: 40.0)
    assert main([]) == 2, "a stale backup must refuse"


def test_ship24_counter_and_warning_ledger_are_in_the_rekey_list(
        monkeypatch, tmp_path):
    """Found during the rekey work: the backup file list (and therefore the
    rotation list) omitted the Ship24 daily-spend counter — a restore would
    have handed the day's budget back. Both lists must carry it. The expiry
    warning ledger joins the same contract: it names chats, so a DEK rotation
    that skipped it would leave it unreadable (and re-arm old warnings)."""
    monkeypatch.setattr(config, "SHIP24_COUNTER_FILE",
                        tmp_path / "ship24_counter.json")
    monkeypatch.setattr(config, "PRO_EXPIRY_WARNED_FILE",
                        tmp_path / "pro_expiry_warned.json")
    names = [Path(p).name for p in state_files()]
    assert "ship24_counter.json" not in names       # absent on disk: skipped
    assert "pro_expiry_warned.json" not in names
    (tmp_path / "ship24_counter.json").write_bytes(b"gAAAA" + b"x" * 8)
    (tmp_path / "pro_expiry_warned.json").write_bytes(b"gAAAA" + b"x" * 8)
    names = [Path(p).name for p in state_files()]
    assert "ship24_counter.json" in names
    assert "pro_expiry_warned.json" in names

