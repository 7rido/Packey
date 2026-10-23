#!/usr/bin/env python3
"""rekey_dek.py — rotate DATA_ENCRYPTION_KEY to a proper 64-hex key.

Why: the live key was 24 characters, far below the documented format, so the
key that unlocks every state file was brute-force-weaker than it should be.
This script re-encrypts every state file under a new key and rewrites .env.

RUN WITH THE BOT STOPPED. The bot holds the same files in memory and saves
them under the OLD key — a concurrent run would undo this one.

Safety rails (all refuse before anything is written):
  * no bot process running (interpreter check, same logic as run.sh find_stale);
  * a fresh verified backup exists (newest ~/Backups/packey/*.tar.enc < 26h
    old) — the backup is the recovery path if this run dies midway;
  * the new key is exactly 64 hex characters and differs from the current one;
  * it is read via getpass — never argv (visible in ps/history), never printed.

Write order is crash-safest with the backup in place:
  1. Phase 1 reads every state file under the OLD key and JSON-parses it —
     one unreadable file aborts before anything is touched.
  2. Phase 2 rewrites each file under the NEW key. track_data.json goes
     through storage's envelope re-wrap (every per-chat DEK is re-wrapped too
     — re-keying only the outer layer is the outage of 2026-09-16), and each
     rewritten file is verified by a full round-trip read.
  3. .env is updated LAST, atomically. The previous .env is kept as
     .env.pre-rekey (0600) for rollback — delete it once the bot is up and
     healthy, since it contains the old key.

Usage:
  ./scripts/rekey_dek.py --dry-run    # verify + plan, write nothing
  ./scripts/rekey_dek.py              # do it (prompts for the new key)
"""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BACKUP_DIR = Path(os.path.expanduser("~/Backups/packey"))
BACKUP_MAX_AGE_H = 26          # daily cadence, plus slack
KEY_RE = re.compile(r"[0-9a-fA-F]{64}")


def bot_running() -> bool:
    """A Python process whose command line mentions main.py — the same net
    run.sh casts before killing. pgrep failing at all is a refusal: a check
    that cannot answer must not wave the rekey through."""
    try:
        out = subprocess.run(["pgrep", "-f", r"main\.py"],
                             capture_output=True, text=True, check=False)
    except OSError:
        return True
    for pid in out.stdout.split():
        if pid == str(os.getpid()):
            continue
        comm = subprocess.run(["ps", "-o", "comm=", "-p", pid],
                              capture_output=True, text=True).stdout.strip()
        base = comm.rsplit("/", 1)[-1].lower()
        if base.startswith(("python", "pypy")):
            return True
    return False


def newest_backup_age_hours() -> float:
    """Hours since the newest encrypted archive; inf when there is none."""
    newest = 0.0
    if BACKUP_DIR.is_dir():
        for arc in BACKUP_DIR.glob("*.tar.enc"):
            newest = max(newest, arc.stat().st_mtime)
    if not newest:
        return float("inf")
    return (time.time() - newest) / 3600.0


def validate_new_key(new_key: str, current_key: str) -> str:
    """Two accepted shapes: exactly 64 hex characters (the documented format),
    or a passphrase of at least 32 characters — the key schedule hashes any
    string, so entropy is what matters and the 24-char shape stays rejected."""
    new_key = (new_key or "").strip()
    is_hex = KEY_RE.fullmatch(new_key) is not None
    is_passphrase = len(new_key) >= 32
    if not (is_hex or is_passphrase):
        raise ValueError("the new key must be 64 hex characters "
                         "(python3 -c \"import secrets; print(secrets.token_hex(32))\") "
                         "or a passphrase of at least 32 characters")
    if new_key == (current_key or "").strip():
        raise ValueError("the new key equals the current one — nothing to rotate")
    return new_key


def state_files():
    """The same import-driven list the backup uses, plus the Ship24 counter
    (its daily-spend cap is state too — the backup list was missing it)."""
    import config, main, miniapp, botstate
    paths = [config.DATA_FILE, config.PRO_USERS_FILE, config.REFRESH_LIMITS_FILE,
             config.PENDING_PAYMENTS_FILE, config.PRO_RECEIPTS_FILE,
             config.PRO_EXPIRY_WARNED_FILE,
             config.DEPOSIT_ADDRS_FILE, config.INTL_REGISTRATIONS_FILE,
             config.SHARE_LINKS_FILE, config.DELIVERY_HISTORY_FILE,
             config.SHIP24_COUNTER_FILE,
             main.REG_COOLDOWN_FILE, miniapp._miniapp_url_state_path(),
             botstate._state_path()]
    return [p for p in paths if Path(p).exists()]


def rekey_all(files, old_key: str, new_key: str) -> dict:
    """Phase 1 (read under the old key) + phase 2 (write under the new one,
    round-trip verified). Raises before ANY write when phase 1 is not clean."""
    import config, storage

    # ── phase 1: everything must open and parse under the old key ──────────
    # READ_ONLY while reading: a file that cannot open must abort the run,
    # not be renamed to .locked by the loader's repair path.
    config.DATA_ENCRYPTION_KEY = old_key
    storage.READ_ONLY = True
    payloads = {}
    try:
        for f in files:
            f = Path(f)
            try:
                text, was_enc = storage._read_protected(f)
            except SystemExit as e:
                raise RuntimeError(f"{f.name}: {e}")
            if text is None:
                raise RuntimeError(
                    f"{f.name}: cannot be read under the current key — nothing "
                    "was written. Restore from backup / fix the key first.")
            try:
                json.loads(text)
            except ValueError as e:
                raise RuntimeError(f"{f.name}: does not parse as JSON ({e}) — "
                                   "nothing was written.")
            payloads[f] = text
    finally:
        storage.READ_ONLY = False
    print(f"phase 1 ok: {len(payloads)} file(s) opened under the old key")

    # ── phase 2: rewrite under the new key, round-trip verified ────────────
    config.DATA_ENCRYPTION_KEY = new_key
    rekeyed, plain = 0, 0
    for f, payload in payloads.items():
        if f == Path(config.DATA_FILE):
            # The envelope container: DEKs must move to the new master too.
            rewrapped = storage._rewrap_envelopes(payload,
                                                  extra_keys=(old_key,))
            if rewrapped is None:
                raise RuntimeError(
                    "track_data.json: a chat data key could not be re-wrapped "
                    "— nothing was written for it (fail-closed).")
            storage._write_protected(f, rewrapped)
            expect = rewrapped
            plain += 1
        else:
            storage._write_protected(f, payload)
            expect = payload
        text2, _ = storage._read_protected(f)
        if text2 is None or json.loads(text2) != json.loads(expect):
            raise RuntimeError(
                f"{f.name}: round-trip verification FAILED under the new key "
                "— restore from backup; the .env update was not reached.")
        rekeyed += 1
        print(f"  rekeyed {f.name}")
    return {"rekeyed": rekeyed, "envelope": plain}


def update_env_file(env_path: Path, new_key: str) -> Path:
    """Swap the key in .env (atomic, 0600), keeping the previous file as
    .env.pre-rekey for rollback. Returns the rollback path."""
    raw = env_path.read_text()
    pre = env_path.with_name(".env.pre-rekey")
    shutil.copy2(env_path, pre)
    os.chmod(pre, 0o600)
    lines = raw.splitlines(keepends=True)
    hit = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        for prefix in ("DATA_ENCRYPTION_KEY=", "export DATA_ENCRYPTION_KEY="):
            if stripped.startswith(prefix):
                eol = "\n" if line.endswith("\n") else ""
                comment = ""
                if " #" in line:                      # preserve a trailing comment
                    comment = " #" + line.split(" #", 1)[1].rstrip("\n")
                lines[i] = prefix[:line.index("=") + 1] + new_key + comment + eol
                hit = True
                break
        if hit:
            break
    if not hit:
        lines.append(f"DATA_ENCRYPTION_KEY={new_key}\n")
    tmp = env_path.with_name(".env.rekey-tmp")
    tmp.write_text("".join(lines))
    os.chmod(tmp, 0o600)
    tmp.replace(env_path)
    return pre


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    os.chdir(ROOT)
    if bot_running():
        print("REFUSING: a bot process is running — stop it first "
              "(./run.sh restart is NOT enough: check pgrep -f main.py).")
        return 2
    age = newest_backup_age_hours()
    if age > BACKUP_MAX_AGE_H:
        print(f"REFUSING: no verified backup newer than {BACKUP_MAX_AGE_H}h "
              f"(newest: {age:.0f}h ago). Run ./scripts/backup_state.sh first.")
        return 2

    import config, storage
    current = (config.DATA_ENCRYPTION_KEY or "").strip()
    if not current:
        print("REFUSING: no current DATA_ENCRYPTION_KEY is set — nothing to "
              "rotate (files would be plaintext or token-sealed).")
        return 2
    new_key = getpass.getpass(
        "New DATA_ENCRYPTION_KEY (input hidden; 64 hex, "
        "`python3 -c \"import secrets; print(secrets.token_hex(32))\"`): ")
    try:
        new_key = validate_new_key(new_key, current)
    except ValueError as e:
        print(f"REFUSING: {e}")
        return 2

    files = state_files()
    print(f"plan: {len(files)} state file(s), "
          f"{'DRY RUN — nothing will be written' if dry else 'live run'}")
    if dry:
        for f in files:
            print(f"  would rekey {Path(f).name}")
        return 0

    try:
        counts = rekey_all(files, current, new_key)
    except RuntimeError as e:
        print(f"ABORTED: {e}")
        return 1

    pre = update_env_file(ROOT / ".env", new_key)
    print(f"done: {counts['rekeyed']} file(s) rekeyed "
          f"({counts['envelope']} envelope container) · .env updated")
    print(f"rollback kept: {pre.name} (0600) — delete it once the bot is up "
          "and healthy; it contains the OLD key.")
    print("now: start the bot and check ./run.sh --status shows your chats "
          "and packages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
