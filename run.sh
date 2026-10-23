#!/usr/bin/env bash
# run.sh — start the Telegram Package Tracker cleanly.
#
# Kills any stale bot instances first: Telegram only allows ONE getUpdates
# connection per token, so a second `python3 main.py` just produces
# TelegramConflictError spam. This script prevents that footgun.
#
# Usage:
#   ./run.sh                  # foreground — logs to your terminal, Ctrl-C stops
#   ./run.sh --background     # nohup background, then prints a status block
#   ./run.sh --status         # status block only (read-only, changes nothing)
#   ./run.sh --track17-key X  # extra args pass straight through to main.py
#
# The status block is counts and up/down only (no chat ids, tracking numbers or
# names), so its output is safe to paste anywhere. STATUS_WAIT (default 8 s)
# tunes how long --background lets the bot settle before reporting.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Console logs live in a private home instead of world-writable /tmp: bot.out
# can carry chat ids inside tracebacks. The dir is created 0700 and the file
# chmod'ed 0600 at start; until the next restart the RUNNING bot still writes
# the old /tmp path, so the status block reads whichever file exists (new
# path preferred).
LOG_DIR="${HOME}/Library/Logs/packey"
BOT_LOG="$LOG_DIR/bot.out"
mkdir -p "$LOG_DIR" 2>/dev/null && chmod 700 "$LOG_DIR" 2>/dev/null || true
if [ ! -f "$BOT_LOG" ] && [ -f /tmp/bot.out ]; then BOT_LOG=/tmp/bot.out; fi
cd "$SCRIPT_DIR" || exit 1

# Interpreter: the project venv (Python 3.12, aiogram 3.31) when present, else
# whatever python3 is on PATH.
if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then PY="$SCRIPT_DIR/.venv/bin/python3"; else PY=python3; fi

# Find running bot instances. On macOS the interpreter is often
# /Library/.../Python.app/.../MacOS/Python main.py (not "python3 main.py"),
# so detect on '* main.py*' / '*/main.py*' and exclude wrapper shells. The
# command match alone is NOT enough — `vim main.py` or `tail -f ... main.py`
# contains it too — so a pid only counts when its executable is actually a
# Python interpreter. This function feeds `kill -9`; false positives here
# would kill the operator's editor with the bot's signal.
find_stale() {
  local out="" pid name base cmd
  for pid in $(pgrep -f 'main\.py' || true); do
    [ "$pid" = "$$" ] && continue
    name=$(ps -o comm= -p "$pid" 2>/dev/null || true)
    base="${name##*/}"
    case "$base" in bash|zsh|sh|fish|dash|ksh|grep|ps|pgrep) continue ;; esac
    case "$base" in
      python|python3|python3.*|Python|Python3*) ;;  # only an interpreter owns main.py
      *) continue ;;
    esac
    cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
    case "$cmd" in
      *" main.py"*|*"/main.py"*) out="$out $pid" ;;
    esac
  done
  echo "$out"
}

# ── Status block ───────────────────────────────────────────────────
# Printed after a background start, and on demand with --status. Counts and
# up/down only: no chat ids, tracking numbers or names, so the output is safe
# to paste into a chat. Self-contained (run.sh must still work if tunnel.sh is
# missing) and every probe is guarded — a dead tunnel or a locked store must
# report "?" rather than break the start command.
STATUS_WAIT="${STATUS_WAIT:-8}"   # seconds to let the bot settle before reporting
PORT="${MINI_APP_PORT:-8080}"

http_code() {  # exactly "000" when the connection fails
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$1" 2>/dev/null || true)"
  [[ "$code" =~ ^[0-9]{3}$ ]] && printf '%s' "$code" || printf '000'
}

env_url() {  # MINI_APP_URL from .env, without echoing the rest of that file
  grep -E '^[[:space:]]*MINI_APP_URL=' .env 2>/dev/null | tail -1 \
    | cut -d= -f2- | tr -d '"' | tr -d "'"
}

status_counts() {  # chats|packages|active-pro|open-invoices  (or ?|?|?|?)
  "$PY" -c '
import sys, time
sys.path.insert(0, ".")
try:
    import config, storage
    storage.READ_ONLY = True   # the loads below must never rename a .corrupt/
                               # .locked file or re-encrypt state: a status
                               # probe reports, it does not repair.
    import pro
    storage.load_data(config.DATA_FILE)
    chats = len(storage.track_sessions)
    pkgs = sum(len(v) for v in storage.track_sessions.values())
    pro.load_pro_state()          # also restores the persisted invoice pools
    active = sum(1 for e in pro._pro_users.values() if e > time.time())
    print("%d|%d|%d|%d" % (chats, pkgs, active, len(pro._pending_payments)))
except Exception:
    print("?|?|?|?")
' 2>/dev/null
}

status_block() {
  local pids n counts chats pkgs active invoices url code local_code tunnel errs quota
  pids="$(find_stale)"
  n="$(printf '%s' "$pids" | wc -w | tr -d ' ')"

  if [ "$n" = "0" ]; then
    printf '  %-9s DOWN — start it with ./run.sh --background\n' "bot"
  elif [ "$n" = "1" ]; then
    printf '  %-9s up (PID %s) · 1 instance\n' "bot" "$(printf '%s' "$pids" | tr -d ' ')"
  else
    printf '  %-9s %s INSTANCES — Telegram allows one poller; conflicts incoming\n' \
      "bot" "$n"
  fi

  errs="$(grep -cE 'Traceback|TelegramConflictError' "$BOT_LOG" 2>/dev/null || true)"
  if [ "${errs:-0}" = "0" ]; then
    printf '  %-9s clean\n' "console"
  else
    printf '  %-9s %s error line(s) — tail -f %s\n' "console" "$errs" "$BOT_LOG"
  fi

  counts="$(status_counts)"
  IFS='|' read -r chats pkgs active invoices <<<"$counts"
  printf '  %-9s %s chats / %s packages\n' "store" "$chats" "$pkgs"
  printf '  %-9s %s active subscriber(s) · %s open invoice(s)\n' \
    "pro" "$active" "$invoices"

  url="$(env_url)"
  local_code="$(http_code "http://127.0.0.1:${PORT}/api/packages")"
  if [ -z "$url" ]; then
    printf '  %-9s local %s · MINI_APP_URL unset (tunnel.sh writes it)\n' \
      "app" "$local_code"
    tunnel="no URL in .env"
  else
    code="$(http_code "$url/api/packages")"
    printf '  %-9s local %s · public %s  %s\n' "app" "$local_code" "$code" "$url"
    # Reachability is decided by the status the probe actually got, not by whether
    # a cloudflared process exists: the connector can be running while the route
    # is broken (530/403 from Cloudflare's edge means it cannot reach the origin —
    # exactly the failure this line exists to catch). The probe path is
    # /api/packages, which answers 401 when the bot is reachable and never answers
    # 403 itself, so 000, any 5xx and 403 all mean DOWN.
    if [ "$code" = "000" ] || [ "$code" = "403" ] || [ "${code#5}" != "$code" ]; then
      tunnel="DOWN"
    else
      tunnel="up"
    fi
    if pgrep -f 'cloudflared tunnel run' >/dev/null 2>&1; then
      tunnel="$tunnel · connector running"
    else
      tunnel="$tunnel · no connector"
    fi
  fi
  printf '  %-9s %s\n' "tunnel" "$tunnel"

  quota="$(grep -o 'quota: [0-9]*/[0-9]* used, [0-9]* remaining' "$BOT_LOG" 2>/dev/null \
    | tail -1)"
  printf '  %-9s %s\n' "17TRACK" "${quota:-not verified yet (check tracker.log)}"

  avail="$("$PY" -c '
try:
    import sys; sys.path.insert(0, ".")
    from usps_browser import browser_slugs, page_label
    slugs = browser_slugs()
    labels = [page_label(s) for s in slugs]
    print("available (" + ", ".join(labels) + ")")
except ImportError:
    print("not installed")
except Exception:
    print("n/a")
' 2>/dev/null)"
  printf '  %-9s %s\n' "browser" "$avail"

  sweep="$("$PY" -c '
import sys, time, json, pathlib
try:
    s = json.loads(pathlib.Path("sweep_state.json").read_text())
    r = s.get("read", 0)
    c = s.get("changed", 0)
    t = s.get("at", 0.0)
    ago = f"{int(time.time() - t) // 60}m ago" if t else "never"
    print(f"last sweep {ago} · {r} checked · {c} changed")
except Exception:
    print("n/a")
' 2>/dev/null)"
  printf '  %-9s %s\n' "browser" "$sweep"

  ship24="$("$PY" -c '
try:
    import sys; sys.path.insert(0, ".")
    import storage
    storage.READ_ONLY = True   # importing ship24 reads the counter via
                               # storage._read_protected: --status must never
                               # rename/rekey a state file as a side effect.
    from ship24 import calls_today, paused; import config as c
    t = calls_today()
    pfx = "paused · " if paused() else ""
    print(f"{pfx}{t}/{c.SHIP24_DAILY_MAX} today")
except Exception:
    print("n/a")
' 2>/dev/null)"
  printf '  %-9s %s\n' "ship24" "$ship24"
}

# 0) Status only: print the block and exit. Deliberately BEFORE the stale-kill
#    below — asking for a status must never stop the running bot.
if [ "${1:-}" = "--status" ] || [ "${1:-}" = "-s" ]; then
  echo "Packey status — $(date '+%Y-%m-%d %H:%M:%S')"
  status_block
  exit 0
fi

# 1) Kill existing bot instances (SIGINT = graceful: aiogram saves data).
PIDS=$(find_stale)
if [ -n "$PIDS" ]; then
  echo "Stopping stale bot instance(s):$PIDS"
  # shellcheck disable=SC2086
  kill -INT $PIDS 2>/dev/null || true
  sleep 3
  LEFTOVERS=$(find_stale)
  if [ -n "$LEFTOVERS" ]; then
    echo "Force-killing leftovers:$LEFTOVERS"
    # shellcheck disable=SC2086
    kill -9 $LEFTOVERS 2>/dev/null || true
    sleep 1
  fi
else
  echo "No stale instances found."
fi

# 2) Start fresh.
if [ "${1:-}" = "--background" ] || [ "${1:-}" = "-b" ]; then
  shift
  nohup "$PY" main.py "$@" > "$LOG_DIR/bot.out" 2>&1 &
  chmod 600 "$LOG_DIR/bot.out" 2>/dev/null || true
  echo "Bot started in background (PID $!)."
  echo "  logs:      tail -f tracker.log"
  echo "  console:   tail -f $LOG_DIR/bot.out"
  # Let the backend bind, the key verify and the first sweep fire, then report:
  # a start that looks fine but left the Mini App unreachable is the failure this
  # catches. STATUS_WAIT tunes the settle time.
  sleep "$STATUS_WAIT"
  echo
  echo "Packey status — $(date '+%Y-%m-%d %H:%M:%S')"
  status_block
  echo
  echo "  status again: ./run.sh --status   ·   tunnel detail: ./tunnel.sh --check"
else
  echo "Starting bot in foreground — Ctrl-C to stop."
  exec "$PY" main.py "$@"
fi