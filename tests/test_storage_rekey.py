"""Key-migration regression tests for the encrypted store (storage.py).

The outage these pin down (2026-09-16): track_data.json's outer layer had been
re-keyed to DATA_ENCRYPTION_KEY while the per-chat data keys (DEKs) inside it
were still wrapped with the bot token. The file opened, then every DEK unwrap
failed and the bot failed closed on its own data — it had to be rebuilt by hand.

Two rules follow, and both are tested here:
  1. a re-key must move BOTH layers (outer file and every DEK), and
  2. a re-key must never write a file it could not fully re-wrap — re-encrypting
     the outer layer while a DEK stays broken makes the file unreadable to the
     OLD key too, turning a recoverable file into a dead one.
"""
import json

import pytest

import config
import storage

TID = "9400111206200074888350"
TOKEN = "111:OLD-BOT-TOKEN"
NEW_KEY = "NEW-MASTER-KEY"
PKGS = {TID: {"name": "Box", "status": "In Transit"}}


@pytest.fixture
def sessions():
    """storage.track_sessions is process-wide; leave it as we found it."""
    storage.track_sessions.clear()
    yield storage.track_sessions
    storage.track_sessions.clear()


def _keys(monkeypatch, bot_token, encryption_key):
    """Point storage's key material at a specific (token, master key) pair."""
    monkeypatch.setattr(config, "_bot_token", bot_token, raising=False)
    monkeypatch.setattr(config, "DATA_ENCRYPTION_KEY", encryption_key, raising=False)


def _write_v2(path):
    """Write the container the way save_data() does, under the CURRENT keys."""
    storage._write_protected(path, json.dumps(storage._build_container(), indent=4))


def _dek_as_stored(path, secret):
    """The wrapped DEK on disk, read back through whichever key opens the file."""
    text = storage._decrypt_with(secret, path.read_bytes())
    assert text is not None, "the file did not open under the expected key"
    return json.loads(text)[storage._ENVELOPE_KEY]["7"]["dek"]


def test_rekey_moves_the_outer_layer_and_every_dek(tmp_path, monkeypatch, sessions):
    path = tmp_path / "track_data.json"
    sessions[7] = dict(PKGS)

    _keys(monkeypatch, TOKEN, None)        # written while the token was the master
    _write_v2(path)
    assert path.read_bytes().startswith(b"gAAAA")
    dek_under_token = _dek_as_stored(path, TOKEN)

    _keys(monkeypatch, TOKEN, NEW_KEY)     # operator sets DATA_ENCRYPTION_KEY
    text, was_encrypted = storage._read_protected(path)
    assert was_encrypted
    assert storage._parse_container(json.loads(text)) == {7: PKGS}

    # The token is rotated away: the data must still open (this is what failed).
    _keys(monkeypatch, None, NEW_KEY)
    text2, _ = storage._read_protected(path)
    assert storage._parse_container(json.loads(text2)) == {7: PKGS}
    assert storage._needs_rekey(path.read_bytes()) is False

    # ...and the blob key really moved: the old token can no longer open it.
    assert _dek_as_stored(path, NEW_KEY) != dek_under_token
    with pytest.raises(Exception):
        storage._fernet_for(TOKEN).decrypt(_dek_as_stored(path, NEW_KEY).encode())


def test_v1_flat_file_still_migrates(tmp_path, monkeypatch, sessions):
    """Pre-envelope files have no DEK to re-wrap — the outer move is the whole
    migration, and it must keep working."""
    path = tmp_path / "track_data.json"
    _keys(monkeypatch, TOKEN, None)
    storage._write_protected(path, json.dumps({"7": PKGS}))

    _keys(monkeypatch, TOKEN, NEW_KEY)
    text, _ = storage._read_protected(path)
    assert storage._parse_container(json.loads(text)) == {7: PKGS}

    _keys(monkeypatch, None, NEW_KEY)
    text2, _ = storage._read_protected(path)
    assert storage._parse_container(json.loads(text2)) == {7: PKGS}


def test_dek_that_opens_under_neither_key_leaves_the_file_untouched(
        tmp_path, monkeypatch, sessions):
    """A half-applied key change must not be able to destroy the file."""
    path = tmp_path / "track_data.json"
    sessions[7] = dict(PKGS)
    _keys(monkeypatch, TOKEN, None)
    _write_v2(path)

    obj = json.loads(storage._decrypt_with(TOKEN, path.read_bytes()))
    obj[storage._ENVELOPE_KEY]["7"]["dek"] = "not-a-dek"
    storage._write_protected(path, json.dumps(obj))
    before = path.read_bytes()

    _keys(monkeypatch, TOKEN, NEW_KEY)
    text, was_encrypted = storage._read_protected(path)      # must not raise
    assert was_encrypted and text is not None
    assert path.read_bytes() == before, "a failed re-wrap must not write"
    # The old key still opens the file, so the data is recoverable by hand.
    assert storage._decrypt_with(TOKEN, path.read_bytes()) is not None
    # ...and the load path still fails closed instead of starting fresh.
    with pytest.raises(storage._EnvelopeError):
        storage._parse_container(json.loads(text))


def test_rekey_refuses_to_write_without_key_material(tmp_path, monkeypatch, sessions):
    """With no key at all _write_protected writes PLAINTEXT; a migration must
    refuse rather than downgrade the file."""
    path = tmp_path / "track_data.json"
    sessions[7] = dict(PKGS)
    _keys(monkeypatch, TOKEN, None)
    _write_v2(path)
    before = path.read_bytes()

    _keys(monkeypatch, None, None)
    storage._rekey_file(path, storage._decrypt_with(TOKEN, before))
    assert path.read_bytes() == before

def test_a_read_only_reader_never_rewrites_a_rekey_due_file(
        tmp_path, monkeypatch, sessions):
    """--status reads the Ship24 counter through the same helper. A rekey-due
    file must open in place — the migration write belongs to the bot process;
    a status probe must not encrypt anything on disk as a side effect."""
    path = tmp_path / "track_data.json"
    sessions[7] = dict(PKGS)
    _keys(monkeypatch, TOKEN, None)        # written while the token was the master
    _write_v2(path)

    _keys(monkeypatch, TOKEN, NEW_KEY)     # rekey is now due
    monkeypatch.setattr(storage, "READ_ONLY", True)
    before = path.read_bytes()
    text, was_encrypted = storage._read_protected(path)
    assert was_encrypted and text is not None
    # The payload is returned as-is: it still opens under the OLD master
    # (that is what rekey-due means), and the DEKs are NOT re-wrapped.
    assert json.loads(text) == json.loads(storage._decrypt_with(TOKEN, before))
    assert path.read_bytes() == before, "read-only mode must not migrate the file"
    assert storage._needs_rekey(path.read_bytes()) is True  # still pending for the bot


def test_read_only_load_data_never_repairs_or_rewrites(
        tmp_path, monkeypatch, sessions):
    """--status counts the store through load_data(). Under READ_ONLY every
    repair path must be off: no .corrupt/.locked rename, no plaintext-migration
    rewrite — the probe degrades to "?" and leaves the bytes alone."""
    monkeypatch.setattr(storage, "READ_ONLY", True)

    # (a) undecryptable ciphertext: report, do not rename anything.
    path = tmp_path / "track_data.json"
    path.write_bytes(b"gAAAAA" + b"y" * 40)
    with pytest.raises(RuntimeError):
        storage.load_data(path)
    assert path.read_bytes() == b"gAAAAA" + b"y" * 40
    assert not list(tmp_path.glob("track_data.json.corrupt*"))
    assert not list(tmp_path.glob("track_data.json.locked*"))

    # (b) legacy plaintext store: parses and counts, but is not rewritten.
    path.write_text(json.dumps({"7": PKGS}))
    before = path.read_bytes()
    storage.load_data(path)
    assert storage.track_sessions == {7: PKGS}
    assert path.read_bytes() == before, "the read-only load migrated the file"
    assert not (tmp_path / "track_data.json.plaintext.bak").exists()


def test_preserves_are_timestamped_and_never_collide(tmp_path):
    """A second incident must not overwrite the first one's evidence: the old
    fixed .locked/.corrupt suffixes meant exactly that. Same-second incidents
    get a collision counter instead."""
    path = tmp_path / "track_data.json"
    path.write_bytes(b"1")
    p1 = storage._preserve_path(path, "locked")
    p1.write_bytes(b"1")
    p2 = storage._preserve_path(path, "locked")     # same second, exists already
    p2.write_bytes(b"2")
    assert p1 != p2 and p1.exists() and p2.exists()
    assert ".locked-" in p1.name and ".locked-" in p2.name


def test_plaintext_migration_deletes_the_bak_after_a_verified_roundtrip(
        tmp_path, monkeypatch, sessions):
    """The .plaintext.bak copy existed only for the window where the migration
    could still go wrong. Once the store reads back cleanly from disk, it is
    deleted instead of nagging the operator — a plaintext copy of every chat
    and package must not outlive its purpose."""
    path = tmp_path / "track_data.json"
    path.write_text(json.dumps({"7": PKGS}))       # legacy plaintext store
    bak = path.with_suffix(".plaintext.bak")
    storage.load_data(path)
    assert storage.track_sessions == {7: PKGS}
    assert storage._is_ciphertext(path), "the migration did not encrypt"
    assert storage._decrypt_payload(path.read_bytes()) is not None
    assert not bak.exists(), "a verified migration must delete the plaintext copy"


def test_plaintext_migration_keeps_the_bak_when_verification_fails(
        tmp_path, monkeypatch, sessions):
    """If the round-trip read cannot verify the migrated file, the plaintext
    copy stays — recoverability beats tidiness."""
    path = tmp_path / "track_data.json"
    path.write_text(json.dumps({"7": PKGS}))
    bak = path.with_suffix(".plaintext.bak")
    monkeypatch.setattr(storage, "_decrypt_payload", lambda raw: None)
    storage.load_data(path)
    assert bak.exists(), "an unverified migration must keep the plaintext copy"
    assert storage._is_ciphertext(path)


def test_dek_to_dek_rotation_rewraps_every_envelope(tmp_path, monkeypatch, sessions):
    """Rotating DATA_ENCRYPTION_KEY to a NEW key (neither the token nor the
    active master opens the old file) must still move every DEK: the rewrap
    needs the key being rotated AWAY from, passed explicitly. This is the
    storage capability scripts/rekey_dek.py is built on."""
    path = tmp_path / "track_data.json"
    sessions[7] = dict(PKGS)
    _keys(monkeypatch, None, OLD := "old-master-secret-24ch")
    _write_v2(path)

    _keys(monkeypatch, None, NEW := "new-master-secret-24ch")
    before = path.read_bytes()
    text = storage._decrypt_with(OLD, before)          # old master opens it
    rewrapped = storage._rewrap_envelopes(text, extra_keys=(OLD,))
    assert rewrapped is not None
    storage._write_protected(path, rewrapped)

    text2, _ = storage._read_protected(path)           # new master opens it now
    assert storage._parse_container(json.loads(text2)) == {7: PKGS}
    obj = json.loads(storage._decrypt_with(NEW, path.read_bytes()))
    for envelope in obj[storage._ENVELOPE_KEY].values():
        assert storage._unwrap_dek(storage._fernet_for(NEW),
                                   envelope["dek"]) is not None
        assert storage._unwrap_dek(storage._fernet_for(OLD),
                                   envelope["dek"]) is None, \
            "a DEK still opens under the old master"
