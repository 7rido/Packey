"""backup_state.sh — the snapshot must be restorable, and must cover everything.

A backup that silently misses a state file, or that cannot be read back with the
key it shipped, is worse than no backup: it is the illusion of safety. These
tests run the real script into a temp directory and check the parts that matter:
the file set comes from the code (so a new state file cannot be forgotten), the
snapshot is verified by decrypting it, the archive is encrypted at rest (it
carries .env, the key to every state file), a restore reproduces the captured
files, and a destination inside the repo is refused.

The passphrase is generated here, at runtime, and never written anywhere: a
snapshot is only as private as its passphrase, so the repo must not carry one.
"""
import hashlib
import re
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backup_state.sh"
PASSPHRASE = secrets.token_urlsafe(24)        # runtime only, never on disk
ARCHIVE_RE = re.compile(r"\d{8}-\d{6}\.tar\.enc")


def _run(backup_dir, args=(), **env):
    return subprocess.run(
        [str(SCRIPT), *args], capture_output=True, text=True, cwd=ROOT,
        env={**dict(subprocess.os.environ), "BACKUP_DIR": str(backup_dir),
             "BACKUP_PASSPHRASE": PASSPHRASE, **env})


def _archives(backup_dir):
    return sorted(p for p in Path(backup_dir).iterdir()
                  if p.is_file() and ARCHIVE_RE.fullmatch(p.name))


def _archive(backup_dir):
    found = _archives(backup_dir)
    assert found, "no .tar.enc archive was created"
    return found[-1]


def _extract(archive, dest):
    """Decrypt and unpack an archive the way an operator would."""
    dest.mkdir(parents=True, exist_ok=True)
    env = {**dict(subprocess.os.environ), "BACKUP_PASSPHRASE": PASSPHRASE}
    with open(archive, "rb") as fh:
        dec = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
             "-pass", "env:BACKUP_PASSPHRASE"],
            stdin=fh, capture_output=True, env=env)
    assert dec.returncode == 0, dec.stderr.decode()
    untar = subprocess.run(["tar", "-xzf", "-", "-C", str(dest)],
                           input=dec.stdout, capture_output=True)
    assert untar.returncode == 0, untar.stderr.decode()
    inner = [d for d in dest.iterdir() if d.is_dir()]
    assert len(inner) == 1, "the archive must hold exactly one snapshot directory"
    return inner[0]


def _state_files():
    """The state files the code declares (the script asks for the same list)."""
    sys.path.insert(0, str(ROOT))
    import config
    import main
    import miniapp
    return {p.name: p for p in (
        config.DATA_FILE, config.PRO_USERS_FILE, config.REFRESH_LIMITS_FILE,
        config.PENDING_PAYMENTS_FILE, config.PRO_RECEIPTS_FILE,
        config.PRO_EXPIRY_WARNED_FILE,
        config.DEPOSIT_ADDRS_FILE,
        config.INTL_REGISTRATIONS_FILE,
        config.SHARE_LINKS_FILE,
        config.DELIVERY_HISTORY_FILE, config.SHIP24_COUNTER_FILE,
        main.REG_COOLDOWN_FILE, miniapp._miniapp_url_state_path()) if p.exists()}



def test_script_is_syntax_valid():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_snapshot_covers_every_state_file_the_code_declares(tmp_path):
    """The list is asked from config/main/miniapp — not hardcoded in the script."""
    expected = set(_state_files())
    assert expected, "precondition: the bot has state files to back up"

    out = _run(tmp_path / "snap")
    assert out.returncode == 0, out.stdout + out.stderr
    inside = {p.name for p in
              _extract(_archive(tmp_path / "snap"), tmp_path / "unpacked").iterdir()}
    assert expected <= inside, f"missing from the snapshot: {expected - inside}"
    assert ".env" in inside, "the key that opens the backups must travel with them"


def test_snapshot_is_verified_by_decrypting_it(tmp_path):
    """Every JSON in the snapshot must be read back — not merely copied."""
    out = _run(tmp_path / "snap")
    assert out.returncode == 0, out.stdout + out.stderr
    jsons = list(_extract(_archive(tmp_path / "snap"),
                          tmp_path / "unpacked").glob("*.json"))
    match = re.search(r"verified=(\d+) skipped=(\d+) failed=(\d+)", out.stdout)
    assert match, f"no verification line in: {out.stdout}"
    verified, skipped, failed = (int(g) for g in match.groups())
    assert failed == 0, out.stdout
    assert verified + skipped == len(jsons), "every file must be accounted for"
    assert verified == len(jsons), "each state file must decrypt, not be skipped"


def test_the_archive_is_encrypted_and_the_plaintext_is_gone(tmp_path):
    """The snapshot carries .env, so the archive must not leak it — and the
    plaintext directory it was built from must not survive the run."""
    snap = tmp_path / "snap"
    out = _run(snap)
    assert out.returncode == 0, out.stdout + out.stderr
    archive = _archive(snap)
    assert (archive.stat().st_mode & 0o777) == 0o600, "the archive must be 0600"
    raw = archive.read_bytes()
    for marker in (b"DATA_ENCRYPTION_KEY", b"TELEGRAM_BOT_API_TOKEN", b".env"):
        assert marker not in raw, f"plaintext {marker!r} found in the archive"
    assert not [d for d in snap.iterdir() if d.is_dir()], \
        "the plaintext snapshot directory must be removed"


def test_roundtrip_restores_byte_identical_state(tmp_path):
    """--restore must reproduce the files the snapshot captured."""
    live = _state_files()
    before = {name: hashlib.sha256(p.read_bytes()).hexdigest()
              for name, p in live.items()}
    snap = tmp_path / "snap"
    out = _run(snap)
    assert out.returncode == 0, out.stdout + out.stderr

    target = tmp_path / "restored"
    res = _run(snap, args=("--restore", str(_archive(snap)), str(target)))
    assert res.returncode == 0, res.stdout + res.stderr
    assert (target / ".env").is_file(), "the restored set must include .env"
    assert {p.name for p in target.iterdir()} >= set(before)
    for name, digest in before.items():
        restored = hashlib.sha256((target / name).read_bytes()).hexdigest()
        if restored == digest:
            continue
        # The running bot can rewrite a state file between the snapshot and the
        # restore — that is the only difference this test tolerates.
        now = hashlib.sha256(live[name].read_bytes()).hexdigest()
        assert now != digest, f"{name} differs and its live file did not change"


def test_pruning_keeps_only_the_newest(tmp_path):
    """KEEP=2 leaves the two newest archives, whatever else is in the directory."""
    target = tmp_path / "snap"
    target.mkdir(parents=True)
    for stamp in ("20200101-000001", "20200102-000002", "20200103-000003"):
        (target / f"{stamp}.tar.enc").write_bytes(b"old")
    out = _run(target, KEEP="2")
    assert out.returncode == 0, out.stdout + out.stderr
    kept = [p.name for p in _archives(target)]
    assert len(kept) == 2, kept
    assert "20200101-000001.tar.enc" not in kept, "the oldest archive must be pruned"


def test_missing_passphrase_everywhere_refuses_cleanly(tmp_path):
    """No passphrase in the environment AND none in the Keychain: refuse, leave nothing.

    "Missing" now means missing from both places: the Keychain is the unattended
    job's passphrase source (its plist cannot carry a secret), so this points the
    lookup at a service name that cannot exist.

    BACKUP_ALERTS=0 is belt and braces: the script also refuses to alert under
    pytest (see test_a_refusal_under_pytest_never_pages_the_operator), but a test
    must never be able to page the operator by any route.
    """
    target = tmp_path / "snap"
    env = {k: v for k, v in subprocess.os.environ.items()
           if k != "BACKUP_PASSPHRASE"}
    env["BACKUP_DIR"] = str(target)
    env["BACKUP_KEYCHAIN_SERVICE"] = f"pytest-no-such-item-{subprocess.os.getpid()}"
    env["BACKUP_ALERTS"] = "0"
    out = subprocess.run([str(SCRIPT)], capture_output=True, text=True,
                         cwd=ROOT, env=env)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "REFUSING" in out.stdout and "BACKUP_PASSPHRASE" in out.stdout
    assert not target.exists(), "a refusal must not leave a snapshot behind"


def test_a_refusal_under_pytest_never_pages_the_operator(tmp_path):
    """The suite runs this refusal deliberately — it must not send Telegram.

    pytest invokes the script non-interactively, which is exactly the condition the
    failure alert targets, so without a guard every suite run paged the owner for
    real (it did, in September 2026, until this test pinned it). A fake `python3`
    records whether the alert body ever ran: the refusal must happen before any
    python3 call, and no alert stamp may be left behind.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "python3-was-called"
    python3 = fake_bin / "python3"
    python3.write_text(f'#!/bin/sh\necho called >> "{marker}"\nexit 0\n')
    python3.chmod(0o755)

    target = tmp_path / "snap"
    target.mkdir()                 # exists, so an alert WOULD leave a stamp here
    env = {k: v for k, v in subprocess.os.environ.items()
           if k != "BACKUP_PASSPHRASE"}
    env["BACKUP_DIR"] = str(target)
    env["BACKUP_KEYCHAIN_SERVICE"] = f"pytest-no-such-item-{subprocess.os.getpid()}"
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["PYTEST_CURRENT_TEST"] = "tests/test_backup.py::test_a_refusal_under_pytest_never_pages_the_operator"
    out = subprocess.run([str(SCRIPT)], capture_output=True, text=True,
                         cwd=ROOT, env=env)

    assert out.returncode == 2, out.stdout + out.stderr
    assert "REFUSING" in out.stdout, "the refusal must still be reported"
    assert not marker.exists(), "the alert body ran during a test — it must not"
    assert not (target / ".last_alert").exists(), "a test run left an alert stamp"


def test_repeat_failures_alert_once_per_window(tmp_path):
    """An unattended failure alerts once per 12h, not once per run.

    The job now runs every 6 hours, so an unchanged failure would otherwise send
    four messages a day. The fake `python3` intercepts the send (nothing leaves the
    machine) and records how many times the alert body was reached.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "python3-was-called"
    python3 = fake_bin / "python3"
    python3.write_text(f'#!/bin/sh\necho called >> "{marker}"\nexit 0\n')
    python3.chmod(0o755)

    target = tmp_path / "snap"
    target.mkdir()
    env = {k: v for k, v in subprocess.os.environ.items()
           if k not in ("BACKUP_PASSPHRASE", "PYTEST_CURRENT_TEST")}
    env["BACKUP_DIR"] = str(target)
    env["BACKUP_KEYCHAIN_SERVICE"] = f"pytest-no-such-item-{subprocess.os.getpid()}"
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    first = subprocess.run([str(SCRIPT)], capture_output=True, text=True,
                           cwd=ROOT, env=env)
    assert first.returncode == 2, first.stdout + first.stderr
    assert marker.exists(), "the first failure must alert"
    assert (target / ".last_alert").exists(), "the alert window must be recorded"
    calls = marker.read_text().count("called")

    second = subprocess.run([str(SCRIPT)], capture_output=True, text=True,
                            cwd=ROOT, env=env)
    assert second.returncode == 2, second.stdout + second.stderr
    assert "REFUSING" in second.stdout, "the refusal is still reported"
    assert marker.read_text().count("called") == calls, "the second run must stay quiet"


def test_the_keychain_can_supply_the_passphrase(tmp_path):
    """The unattended job's path: no environment, passphrase from the Keychain.

    A fake `security` earlier on PATH keeps this hermetic and pins the exact lookup
    the script performs (find-generic-password, value to stdout) instead of trusting
    whatever happens to be in the developer's own Keychain. Then it proves the
    archive really is encrypted with THAT passphrase — a Keychain lookup that
    silently found nothing would still produce a file, just an unusable one.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    security = fake_bin / "security"
    security.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "find-generic-password" ]; then\n'
        "  printf %s pytest-passphrase\n  exit 0\n"
        "fi\n"
        "exit 1\n")
    security.chmod(0o755)

    target = tmp_path / "snap"
    env = {k: v for k, v in subprocess.os.environ.items()
           if k != "BACKUP_PASSPHRASE"}
    env["BACKUP_DIR"] = str(target)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    out = subprocess.run([str(SCRIPT)], capture_output=True, text=True,
                         cwd=ROOT, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert len(_archives(target)) == 1, "the Keychain passphrase must produce an archive"

    with open(_archive(target), "rb") as fh:
        dec = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
             "-pass", "pass:pytest-passphrase"],
            stdin=fh, capture_output=True)
    assert dec.returncode == 0, "the archive must open with the Keychain passphrase"


def test_a_destination_inside_the_project_is_refused():
    """Otherwise a snapshot could be copied into the next one."""
    out = _run(ROOT / "tmpbackups")
    assert out.returncode == 2, out.stdout
    assert "REFUSING" in out.stdout
    assert not (ROOT / "tmpbackups").exists(), "and nothing may be created"

    # Relative paths must be judged the same as absolute ones.
    out = _run("./tmpbackups_relative")
    assert out.returncode == 2, out.stdout
    assert not (ROOT / "tmpbackups_relative").exists()
