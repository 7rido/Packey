"""Operator alerts: admin-only, throttled, and incapable of breaking the bot.

The bot-side counterpart of the backup script's failure alert. These exist because
the failures they report used to reach tracker.log only — a file nobody watches,
which is how the September 2026 backup outage ran for days unnoticed.
"""
import asyncio
import time
from pathlib import Path

import pytest

import botstate
import config
import main
import storage

ROOT = Path(__file__).resolve().parent.parent


class _Bot:
    """Records what it is asked to send; can fail on demand."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ALERTS", True)
    monkeypatch.setattr(config, "OWNER_CHAT_IDS", {111, -222})
    monkeypatch.setattr(botstate, "bot", None)
    botstate.reset_alert_throttle()
    yield
    botstate.reset_alert_throttle()


def test_an_alert_reaches_every_admin_and_nothing_else():
    fake = _Bot()
    botstate.set_bot(fake)
    assert asyncio.run(botstate.notify_admins("⚠️ something broke")) is True
    assert sorted(chat for chat, _ in fake.sent) == [-222, 111]
    assert all("something broke" in text for _, text in fake.sent)


def test_an_alert_never_raises_when_telegram_fails():
    """An alert that can throw would turn one failure into two."""
    botstate.set_bot(_Bot(fail=True))
    assert asyncio.run(botstate.notify_admins("⚠️ something broke")) is True


def test_alerts_are_throttled_per_key():
    """A poller failing every minute sends one message, not sixty."""
    fake = _Bot()
    botstate.set_bot(fake)
    assert asyncio.run(botstate.notify_admins("first", key="poll-cycle")) is True
    assert asyncio.run(botstate.notify_admins("second", key="poll-cycle")) is False
    assert len(fake.sent) == 2, "one per admin chat, not one per call"

    # A different problem is a different bucket: it must get through.
    assert asyncio.run(botstate.notify_admins("other", key="solana-monitor")) is True
    assert len(fake.sent) == 4


def test_the_window_reopens_after_the_gap():
    fake = _Bot()
    botstate.set_bot(fake)
    assert asyncio.run(botstate.notify_admins("first", key="k")) is True
    botstate._alert_sent_at["k"] -= botstate._ALERT_MIN_GAP + 1
    assert asyncio.run(botstate.notify_admins("again", key="k")) is True
    assert len(fake.sent) == 4


def test_the_knob_silences_them(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ALERTS", False)
    fake = _Bot()
    botstate.set_bot(fake)
    assert asyncio.run(botstate.notify_admins("⚠️ something broke")) is False
    assert fake.sent == []


def test_no_owner_ids_means_no_send(monkeypatch):
    monkeypatch.setattr(config, "OWNER_CHAT_IDS", set())
    fake = _Bot()
    botstate.set_bot(fake)
    assert asyncio.run(botstate.notify_admins("⚠️ something broke")) is False
    assert fake.sent == []


def test_before_the_bot_exists_it_alerts_over_the_token(monkeypatch):
    """The single-instance guard alerts before main() creates the Bot object."""
    posted = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            posted.append((url, data))

    monkeypatch.setattr(config, "_bot_token", "111:TEST-TOKEN")
    monkeypatch.setattr(botstate.httpx, "AsyncClient", _Client)
    assert asyncio.run(botstate.notify_admins("⚠️ something broke",
                                              key="instance")) is True
    assert len(posted) == 2, posted
    for url, data in posted:
        assert "111:TEST-TOKEN/sendMessage" in url
        assert "TEST-TOKEN" not in str(data.get("text")), "the token must never be text"


def test_every_failure_site_is_wired():
    """Static guard: an alert must not be silently dropped from a call site."""
    for name in ("main.py", "refresh.py", "pro.py"):
        assert "notify_admins" in (ROOT / name).read_text(), name


def test_the_fingerprint_follows_the_code(tmp_path):
    """New code must be detectable without a version constant to forget to bump."""
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    first = botstate.code_fingerprint(tmp_path)
    assert first == botstate.code_fingerprint(tmp_path), "must be stable"
    (tmp_path / "a.py").write_text("x = 2\n")
    assert botstate.code_fingerprint(tmp_path) != first, "an edit must change it"


def test_the_startup_note_reports_counts_and_what_is_new(monkeypatch):
    monkeypatch.setattr(botstate, "code_fingerprint", lambda root=None: "aaaa1111")
    note = botstate.startup_note(4242, {"chats": 3, "packages": 8, "pro": 1},
                                 url="https://x.test")
    assert "PID 4242" in note
    assert "8 package(s) in 3 chat(s)" in note and "1 active subscriber(s)" in note
    assert "Mini App URL: https://x.test" in note
    assert "New code" not in note, "the first start has nothing to compare against"
    assert "<" not in note and "<b>" not in note, \
        "the notice is plain text: it is sent without a parse mode"

    # A later start (outside the debounce window) with different code:
    state = botstate.load_state()
    state["last_notice_ts"] = 0.0
    botstate.save_state(state)
    monkeypatch.setattr(botstate, "code_fingerprint", lambda root=None: "bbbb2222")
    later = botstate.startup_note(4243, {"chats": 3, "packages": 8, "pro": 1})
    assert "aaaa1111" in later and "bbbb2222" in later, later
    assert "Mini App URL" not in later, "the URL did not change, so it is not news"


def test_a_crash_loop_reports_once(monkeypatch):
    """KeepAlive restarting every few seconds must not page the operator every time."""
    monkeypatch.setattr(botstate, "code_fingerprint", lambda root=None: "aaaa1111")
    first = botstate.startup_note(1, {"chats": 0, "packages": 0, "pro": 0})
    assert first, "the first start must report"
    assert botstate.startup_note(2, {"chats": 0, "packages": 0, "pro": 0}) == ""
    assert botstate.startup_note(3, {"chats": 0, "packages": 0, "pro": 0}) == ""
    assert botstate.load_state()["restart_count"] == 3, "counted, just not repeated"

    state = botstate.load_state()
    state["last_notice_ts"] = 0.0
    botstate.save_state(state)
    assert botstate.startup_note(4, {"chats": 0, "packages": 0, "pro": 0}), \
        "the window must reopen"


def test_the_notice_state_is_encrypted_and_private():
    botstate.startup_note(1, {"chats": 0, "packages": 0, "pro": 0})
    path = botstate._state_path()
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600", "state files are private"
    assert not path.read_bytes().startswith(b"{"), "and encrypted at rest"


def test_main_sends_the_notice_at_startup_and_it_is_backed_up():
    """Static: the wiring, and the new state file in the backup set."""
    main_src = (ROOT / "main.py").read_text()
    assert "botstate.startup_note(" in main_src
    assert 'notify_admins(_note' in main_src
    assert "botstate._state_path()" in (ROOT / "scripts" / "backup_state.sh").read_text()

def test_an_undecryptable_state_file_is_recorded(tmp_path):

    """The loaders carry on with an empty store — the operator still has to hear it."""
    path = tmp_path / "track_data.json"
    path.write_bytes(b"gAAAAA" + b"x" * 40)
    before = list(storage.locked_preserved)
    try:
        with pytest.raises(SystemExit):
            storage._read_protected(path)
        assert storage.locked_preserved[-1] == "track_data.json"
        assert list(tmp_path.glob("track_data.json.locked-*")), "the preserve must be timestamped"
    finally:
        storage.locked_preserved[:] = before


def test_a_read_only_reader_reports_instead_of_repairing(tmp_path, monkeypatch):
    """run.sh --status reaches state files through the same helper (the ship24
    counter). A status command must report, not repair: no .locked rename, no
    SystemExit — preserving a broken file is the owning (bot) process's move,
    so the operator still finds the bytes in place afterwards."""
    path = tmp_path / "track_data.json"
    path.write_bytes(b"gAAAAA" + b"x" * 40)
    monkeypatch.setattr(storage, "READ_ONLY", True)
    before = list(storage.locked_preserved)
    text, was_encrypted = storage._read_protected(path)   # must not raise
    assert text is None and was_encrypted
    assert path.read_bytes() == b"gAAAAA" + b"x" * 40, "read-only mode wrote nothing"
    assert not list(tmp_path.glob("track_data.json.locked*"))
    assert storage.locked_preserved == before


def test_the_watchdog_flags_a_stale_backup(tmp_path, monkeypatch):
    """The backup script alerts when a run fails; the watchdog catches the other
    silent mode — the job not running at all (newest archive older than 48h)."""
    import os
    backups = tmp_path / "packey"
    backups.mkdir()
    for i, hours in enumerate((50.0, 60.0)):
        archive = backups / f"20260926-{i:06d}.tar.enc"
        archive.write_bytes(b"x")
        stamp = time.time() - hours * 3600
        os.utime(archive, (stamp, stamp))
    monkeypatch.setattr(main, "_BACKUP_DIR", backups)
    monkeypatch.setattr(config, "ADMIN_ALERTS", True)
    sent = []

    async def fake_notify(text, key="general"):
        sent.append((text, key))
        return True

    monkeypatch.setattr(botstate, "notify_admins", fake_notify)
    asyncio.run(main.check_backup_staleness())
    assert sent, "a 50h-old newest archive must alert"
    assert "50 h old" in sent[0][0], sent


def test_the_watchdog_is_quiet_when_backups_are_fresh(tmp_path, monkeypatch):
    import os
    backups = tmp_path / "packey"
    backups.mkdir()
    for i, hours in enumerate((1.0, 2.0)):
        archive = backups / f"20260926-{i:06d}.tar.enc"
        archive.write_bytes(b"x")
        stamp = time.time() - hours * 3600
        os.utime(archive, (stamp, stamp))
    monkeypatch.setattr(main, "_BACKUP_DIR", backups)
    monkeypatch.setattr(config, "ADMIN_ALERTS", True)
    sent = []

    async def fake_notify(text, key="general"):
        sent.append((text, key))
        return True

    monkeypatch.setattr(botstate, "notify_admins", fake_notify)
    asyncio.run(main.check_backup_staleness())
    assert sent == [], "fresh backups must not alert"


def test_the_watchdog_alarms_when_no_archive_exists(tmp_path, monkeypatch):
    backups = tmp_path / "packey"
    backups.mkdir()                              # exists, empty: never backed up
    monkeypatch.setattr(main, "_BACKUP_DIR", backups)
    monkeypatch.setattr(config, "ADMIN_ALERTS", True)
    sent = []

    async def fake_notify(text, key="general"):
        sent.append((text, key))
        return True

    monkeypatch.setattr(botstate, "notify_admins", fake_notify)
    asyncio.run(main.check_backup_staleness())
    assert sent and "No backup archive" in sent[0][0], sent
