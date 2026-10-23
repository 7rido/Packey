#!/usr/bin/env bash
# backup_state.sh — snapshot the bot's state files (and .env) to a second place.
#
# Why: everything lives on one Mac. The state files are encrypted at rest, but
# without .env's DATA_ENCRYPTION_KEY they cannot be read — so .env has to travel
# with them. A backup that cannot be restored is worse than none, so every
# snapshot is verified by decrypting each file with the key it shipped with.
#
# That bundle is ciphertext PLUS the key, so it is safe to keep anywhere only
# once it is encrypted itself — which is what this script now does: after
# verification the whole directory is tarred and encrypted (AES-256-CBC, PBKDF2)
# into <STAMP>.tar.enc and the plaintext directory is deleted. The passphrase
# that opens it lives OUTSIDE the snapshot: the operator's environment
# (BACKUP_PASSPHRASE, still the documented contract) or — for the unattended
# launchd job, which has no environment to put a secret in and a world-readable
# plist — the macOS Keychain under the service named below. It is never generated
# by this script: a passphrase that lives only beside the archive protects nothing
# against the machine being lost, so a run with no passphrase anywhere REFUSES
# (and now says so on Telegram instead of failing silently for five days).
#   Store it once, deliberately:
#     security add-generic-password -a "$USER" -s packey-backup -w
#   Read it back (for a restore, or to copy into your password manager):
#     security find-generic-password -w -s packey-backup
# Never in .env, never in the snapshot, never logged.
#
# A failed run must never leave .env in the staging directory: that is how five
# plaintext copies of the key accumulated in September 2026 (the pack step could
# not encrypt, and the directory was kept by design). Data files stay — they are
# ciphertext and the only copy of that day's state — but .env is stripped, and
# the operator is told on Telegram rather than finding out days later.
#
# The file list is asked from the code (config/main/miniapp own the paths), not
# hardcoded here — a new state file must not be able to miss its backup.
#
# Usage:
#   ./scripts/backup_state.sh                    # snapshot + prune (Keychain pass)
#   BACKUP_PASSPHRASE=… ./scripts/backup_state.sh   # explicit passphrase
#   ./scripts/backup_state.sh --list             # show archives
#   BACKUP_PASSPHRASE=… ./scripts/backup_state.sh --restore <archive> <dir>
# Env: BACKUP_DIR (default ~/Backups/packey), KEEP (default 14 archives),
#      BACKUP_LOG (default $BACKUP_DIR/backup.log),
#      BACKUP_PASSPHRASE (optional; exported for openssl. When absent, the
#      Keychain is consulted — store the passphrase there once with the
#      `security add-generic-password` line above, and keep a copy in your
#      password manager: losing it makes every archive unreadable, which is the
#      point of encrypting them),
#      BACKUP_KEYCHAIN_SERVICE (default packey-backup),
#      BACKUP_ALERTS=0 (suppress the failure Telegram message),
#      BACKUP_PLAINTEXT_OK=1 (TEMPORARY escape hatch: write the old plaintext
#      directory instead of an archive. It puts the key back beside the
#      ciphertext, so use it only to bridge a deploy and unset it afterwards.)
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1
BACKUP_DIR="${BACKUP_DIR:-$HOME/Backups/packey}"
KEEP="${KEEP:-14}"
BACKUP_LOG="${BACKUP_LOG:-$BACKUP_DIR/backup.log}"
KEYCHAIN_SERVICE="${BACKUP_KEYCHAIN_SERVICE:-packey-backup}"
STAMP="$(date '+%Y%m%d-%H%M%S')"
DEST="$BACKUP_DIR/$STAMP"          # plaintext staging dir; removed once packed
ARCHIVE="$DEST.tar.enc"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# A refusal or a failure has to reach the operator's log, not just launchd's stdout
# under /tmp — nobody watches that file, which is how five failed nights went
# unnoticed in September 2026. Only writes when the directory already exists, so a
# refusal still creates nothing.
log_problem() {
  local line
  line="$(printf '%s  %s' "$(date '+%Y-%m-%d %H:%M:%S')" "$*")"
  printf '%s\n' "$line"
  if [ -d "$BACKUP_DIR" ]; then
    printf '%s\n' "$line" >>"$BACKUP_LOG" 2>/dev/null || true
  fi
}

# The passphrase for a snapshot run: environment first, then the Keychain. It is
# never generated here — `tests/test_backup.py` pins that a missing passphrase
# refuses, and it is right to: the passphrase must be recoverable OFF this machine
# (a password manager), so inventing one and storing it beside the archive would
# quietly weaken the scheme to "whoever has the Mac has the backups".
#
# The Keychain exists for the unattended launchd job, which has no environment to
# carry a secret and a world-readable plist. Store it once, deliberately:
#     security add-generic-password -a "$USER" -s packey-backup -w
# and keep a copy in your password manager. Losing it makes every archive
# unreadable, which is the point of encrypting them.
resolve_passphrase() {
  if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
    export BACKUP_PASSPHRASE
    return 0
  fi
  BACKUP_PASSPHRASE="$(security find-generic-password -w -s "$KEYCHAIN_SERVICE" -a "$USER" 2>/dev/null || true)"
  if [ -z "$BACKUP_PASSPHRASE" ]; then
    BACKUP_PASSPHRASE="$(security find-generic-password -w -s "$KEYCHAIN_SERVICE" 2>/dev/null || true)"
  fi
  if [ -n "$BACKUP_PASSPHRASE" ]; then
    export BACKUP_PASSPHRASE
  fi
  return 0
}

# Tell the operator when an unattended run fails. Silent on a terminal (a manual
# run already prints), never fatal, and it carries no secret: just the reason.
# Reads the token and the owner chat ids out of .env the same way verify_snapshot
# does, and prints nothing.
notify_failure() {
  # Guards are `if` statements on purpose: a bare `[ cond ] && return` list returns
  # non-zero when the test fails, and under `set -e` in an unattended run that is
  # one more way to lose a backup for no reason.
  if [ "${BACKUP_ALERTS:-1}" = "0" ]; then return 0; fi
  if [ -t 0 ]; then return 0; fi            # interactive: the operator is watching
  # Never page the operator for a test run. tests/test_backup.py exercises this
  # refusal path deliberately, and pytest runs the script non-interactively — which
  # is exactly what the alert looks for. Without this guard every suite run sends
  # real Telegram messages (it did, in September 2026).
  if [ -n "${PYTEST_CURRENT_TEST:-}" ]; then return 0; fi
  # At most one alert per 12h. The job runs every 6 hours, so a broken passphrase
  # would otherwise ping four times a day about a state that has not changed.
  # (The refusal path has no BACKUP_DIR yet on a first run — then it just alerts.)
  local stamp="$BACKUP_DIR/.last_alert"
  if [ -f "$stamp" ] && [ -z "$(find "$stamp" -mmin +720 2>/dev/null)" ]; then
    return 0
  fi
  if [ -d "$BACKUP_DIR" ]; then
    touch "$stamp" 2>/dev/null || true
    chmod 600 "$stamp" 2>/dev/null || true
  fi
  python3 - "$1" <<'PY' || true
import pathlib, sys, urllib.parse, urllib.request

reason = sys.argv[1]
env = {}
try:
    for raw in pathlib.Path(".env").read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line[7:] if line.startswith("export ") else line
        key, sep, value = line.partition("=")
        if sep:
            env[key.strip()] = value.strip().strip('"').strip("'")
except OSError:
    sys.exit(0)

token = env.get("TELEGRAM_BOT_API_TOKEN")
owners = [c for c in (env.get("OWNER_CHAT_IDS") or "").replace(" ", "").split(",")
          if c.lstrip("-").isdigit()]
if not token or not owners:
    sys.exit(0)
text = ("⚠️ Packey backup FAILED — " + reason +
        "\nThe state files are NOT being backed up. Details: " +
        (env.get("BACKUP_LOG") or "~/Backups/packey/backup.log"))
for chat in owners:
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data=data, timeout=10).read()
    except Exception:
        pass
PY
}

STAMP_RE='^[0-9]{8}-[0-9]{6}$'
ARCHIVE_RE='^[0-9]{8}-[0-9]{6}\.tar\.enc$'

# The passphrase is the one secret this script must never invent, read out of
# .env, or print: it comes from the environment only. Returns 1 for the
# plaintext escape hatch, and refuses (exit 2) otherwise.
require_passphrase() {
  if [ -n "${BACKUP_PASSPHRASE:-}" ] && printenv BACKUP_PASSPHRASE >/dev/null 2>&1; then
    return 0
  fi
  if [ "${BACKUP_PLAINTEXT_OK:-0}" = "1" ]; then
    return 1
  fi
  if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
    log_problem "REFUSING: BACKUP_PASSPHRASE is set but not EXPORTED — openssl reads it from the environment (export BACKUP_PASSPHRASE=…)."
  else
    log_problem "REFUSING: no BACKUP_PASSPHRASE — not in the environment, not in the Keychain ($KEYCHAIN_SERVICE). A snapshot carries .env, the key to every state file, so it must be encrypted at rest. Store one: security add-generic-password -a \"\$USER\" -s $KEYCHAIN_SERVICE -w"
  fi
  log_problem "          (temporary bridge: BACKUP_PLAINTEXT_OK=1 writes an UNENCRYPTED snapshot instead.)"
  notify_failure "no backup passphrase available"
  exit 2
}

# ── verification: can this snapshot be read with the key it shipped with? ───
# Uses storage's own reader rather than re-deriving the crypto here (a second
# implementation of the key schedule is a second thing to get wrong), and reads
# through temp copies so a failed check cannot rename files inside the snapshot.
# The key comes from the SNAPSHOT's own .env — that is the file the operator
# will have to restore with, and it is what the header promises. The ambient
# environment is only a fallback for a snapshot whose .env is missing.
verify_snapshot() {
  DEST="$1" python3 - <<'PY'
import json, os, pathlib, shutil, sys, tempfile

dest = pathlib.Path(os.environ["DEST"])

env_file = dest / ".env"
if env_file.is_file():
    for raw in env_file.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        name, sep, value = line.partition("=")
        if sep and name.strip() in ("DATA_ENCRYPTION_KEY", "TELEGRAM_BOT_API_TOKEN"):
            os.environ[name.strip()] = value.strip().strip('"').strip("'")

sys.path.insert(0, ".")
import storage

tmp = pathlib.Path(tempfile.mkdtemp(prefix="packey_verify_"))
ok = skipped = failed = 0
try:
    for p in sorted(dest.glob("*.json")):
        copy = tmp / p.name
        shutil.copy2(p, copy)
        try:
            text, _was_enc = storage._read_protected(copy)
        except (SystemExit, Exception):
            failed += 1
            print("  UNREADABLE:", p.name)
            continue
        if text is None:
            skipped += 1                  # unreadable-but-absent: nothing to check
            continue
        try:
            json.loads(text)              # must also be valid JSON after decrypt
            ok += 1
        except ValueError:
            failed += 1
            print("  NOT JSON:", p.name)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print(f"verified={ok} skipped={skipped} failed={failed}")
sys.exit(1 if failed else 0)
PY
}

if [ "${1:-}" = "--list" ]; then
  # Archives are what a snapshot is now; legacy plaintext directories are still
  # listed (marked) so they are never silently forgotten — they hold .env.
  ls -1 "$BACKUP_DIR" 2>/dev/null | grep -E "$ARCHIVE_RE" | tail -20 || true
  legacy="$(ls -1 "$BACKUP_DIR" 2>/dev/null | grep -E "$STAMP_RE" || true)"
  if [ -n "$legacy" ]; then
    printf '%s\n' "$legacy" | sed 's/$/  (LEGACY plaintext directory)/'
  fi
  exit 0
fi

if [ "${1:-}" = "--restore" ]; then
  ARCHIVE_IN="${2:-}"
  TARGET="${3:-}"
  if [ -z "$ARCHIVE_IN" ] || [ -z "$TARGET" ]; then
    echo "usage: $0 --restore <archive.tar.enc> <empty-target-dir>" >&2
    exit 2
  fi
  [ -f "$ARCHIVE_IN" ] || { echo "REFUSING: no such archive: $ARCHIVE_IN" >&2; exit 2; }
  # Disaster recovery must work from the Keychain too — needing to remember an
  # export dance is exactly the wrong requirement at that moment.
  resolve_passphrase
  require_passphrase || {
    echo "REFUSING: --restore needs the passphrase — export BACKUP_PASSPHRASE, or store it in the Keychain as '$KEYCHAIN_SERVICE'." >&2
    exit 2; }
  # An empty target only: restoring over a directory that already holds state
  # (the live data dir, say) would destroy the very thing the backup is for.
  if [ -d "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
    echo "REFUSING: target is not empty: $TARGET" >&2
    exit 2
  fi
  TMP="$(mktemp -d "${TMPDIR:-/tmp}/packey_restore.XXXXXX")"
  trap 'rm -rf "$TMP"' EXIT
  if ! openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE \
        -in "$ARCHIVE_IN" 2>/dev/null | tar -xzf - -C "$TMP"; then
    echo "REFUSING: could not decrypt $ARCHIVE_IN (wrong passphrase, or damaged archive)" >&2
    exit 1
  fi
  SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [ -n "$SRC" ] || { echo "REFUSING: the archive holds no snapshot directory" >&2; exit 1; }
  # Same verification as the snapshot path, against the DECRYPTED copies and the
  # key the snapshot itself carries — never move unverified files into place.
  VERIFY="$(verify_snapshot "$SRC")" || {
    log "RESTORE REFUSED: verification failed: $VERIFY"; exit 1; }
  mkdir -p "$TARGET"
  (cd "$SRC" && cp -Rp . "$TARGET"/)      # includes .env
  log "restored $ARCHIVE_IN → $TARGET · $VERIFY"
  exit 0
fi

# A destination inside the repo would let a snapshot be copied into the next one.
# Canonicalise through the PARENT so a relative path (./tmpbackups) is judged
# like the absolute path it really is — the first version compared the raw string
# against an absolute prefix and waved it through.
mkdir -p "$(dirname "$BACKUP_DIR")"
BACKUP_DIR_ABS="$(cd "$(dirname "$BACKUP_DIR")" && pwd -P)/$(basename "$BACKUP_DIR")"
REPO_ABS="$(pwd -P)"
case "$BACKUP_DIR_ABS/" in
  "$REPO_ABS/"*)
    log "REFUSING: BACKUP_DIR must be outside the project ($BACKUP_DIR_ABS)"
    exit 2 ;;
esac
BACKUP_DIR="$BACKUP_DIR_ABS"

# The passphrase gate sits before anything is written, so a refusal leaves no
# snapshot (and no half-snapshot) behind. resolve_passphrase looks in the
# environment and then the Keychain (never generating one — see above), so the
# unattended job works without a secret in its world-readable plist, and a run
# with no passphrase anywhere still refuses — and now says so on Telegram.
PLAINTEXT=0
resolve_passphrase
require_passphrase || PLAINTEXT=1

mkdir -p "$DEST"
chmod 700 "$BACKUP_DIR" "$DEST"

# From here on the staging directory holds .env — the key to every state file. A
# failed run must not leave it lying there: that is exactly how five plaintext
# copies accumulated in September 2026. The data files stay, because they are
# ciphertext and the only copy of that day's state. The plaintext escape hatch
# keeps everything, since a self-contained plaintext snapshot is what it is for.
trap 'rc=$?; if [ "$rc" -ne 0 ] && [ "${PLAINTEXT:-0}" != "1" ] && [ -f "$DEST/.env" ]; then
        rm -f "$DEST/.env"
        log "stripped .env from the leftover snapshot at $DEST (the data files stay)"
      fi
      exit "$rc"' EXIT

# ── the file set, from the code that owns it ───────────────────────────────
FILES="$(python3 - <<'PY'
import sys
sys.path.insert(0, ".")
import config, main, miniapp, botstate
paths = [config.DATA_FILE, config.PRO_USERS_FILE, config.REFRESH_LIMITS_FILE,
         config.PENDING_PAYMENTS_FILE, config.PRO_RECEIPTS_FILE,
         config.PRO_EXPIRY_WARNED_FILE,
         config.DEPOSIT_ADDRS_FILE,
         config.INTL_REGISTRATIONS_FILE, config.SHARE_LINKS_FILE,
         config.DELIVERY_HISTORY_FILE, config.SHIP24_COUNTER_FILE,
         main.REG_COOLDOWN_FILE, miniapp._miniapp_url_state_path(),
         botstate._state_path()]
print("\n".join(str(p) for p in paths if p.exists()))
PY
)"

copied=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  cp -p "$f" "$DEST/$(basename "$f")"
  chmod 600 "$DEST/$(basename "$f")"
  copied=$((copied + 1))
done <<<"$FILES"

# .env is the recovery key for all of the above — and never printed anywhere.
cp -p .env "$DEST/.env"
chmod 600 "$DEST/.env"

# ── verification (see verify_snapshot above): read every file back ─────────
VERIFY="$(verify_snapshot "$DEST")" || {
  log_problem "BACKUP FAILED verification: $VERIFY"
  notify_failure "a snapshot failed verification"
  exit 1
}

# ── pack: the snapshot holds the key, so encrypt the whole bundle ──────────
if [ "$PLAINTEXT" = "1" ]; then
  log "WARNING: BACKUP_PLAINTEXT_OK=1 — this snapshot is NOT encrypted, and it carries .env."
  kept_path="$DEST"
else
  umask 077                      # the archive is 0600 from the moment it exists
  if ! tar -czf - -C "$BACKUP_DIR" "$STAMP" \
        | openssl enc -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -out "$ARCHIVE"; then
    rm -f "$ARCHIVE"             # a half-written archive is worse than none
    log_problem "BACKUP FAILED: could not encrypt the snapshot — its data files are still at $DEST (.env was stripped)"
    notify_failure "could not encrypt the snapshot (is BACKUP_PASSPHRASE readable?)"
    exit 1
  fi
  chmod 600 "$ARCHIVE"
  rm -rf "$DEST"                 # plaintext gone: .env no longer sits beside the ciphertext
  kept_path="$ARCHIVE"
fi

# ── prune: keep the newest KEEP snapshots ──────────────────────────────────
pruned=0
for old in $(ls -1 "$BACKUP_DIR" 2>/dev/null | grep -E "$ARCHIVE_RE" \
             | sort -r | tail -n +$((KEEP + 1))); do
  rm -f "${BACKUP_DIR:?}/$old"
  pruned=$((pruned + 1))
done
if [ "$PLAINTEXT" = "1" ]; then
  # Escape-hatch runs keep the old plaintext layout, so prune it the old way too
  # (otherwise a bridge deploy would fill the disk with unencrypted snapshots).
  for old in $(ls -1 "$BACKUP_DIR" 2>/dev/null | grep -E "$STAMP_RE" \
               | sort -r | tail -n +$((KEEP + 1))); do
    rm -rf "${BACKUP_DIR:?}/$old"
    pruned=$((pruned + 1))
  done
fi

line="snapshot $STAMP · files=$copied + .env · $VERIFY · kept=$KEEP pruned=$pruned · $kept_path"
log "$line" | tee -a "$BACKUP_LOG"
