#!/usr/bin/env bash
# tunnel.sh — expose the local Mini App backend (default 127.0.0.1:8080) via a
# public HTTPS URL, and keep it (and the bot) alive.
#
# ── Quick tunnel (default, no account) ─────────────────────────────
#   ./tunnel.sh
# Starts cloudflared, extracts the https://...trycloudflare.com URL,
# AUTO-WRITES it to .env as MINI_APP_URL, and restarts the bot so the
# menu button works immediately. No manual copy-paste needed anymore.
# NOTE: the URL changes whenever the tunnel restarts — it is TRANSIENT.
# If .env holds a stable URL (Tailscale Funnel) that still answers, the
# script refuses to overwrite it; pass --force to replace it anyway.
#
# ── Watch mode: supervise tunnel + bot (no terminal babysitting) ───
#   ./tunnel.sh --watch            # quick tunnel: re-mints on death, keeps
#                                  # .env and the bot in sync automatically.
#                                  # The bot's own self-heal then announces
#                                  # each fresh button to package chats.
#   ./tunnel.sh --watch --funnel   # Tailscale Funnel (stable URL): the URL
#                                  # step is a no-op; just health-checks the
#                                  # public URL and keeps the bot alive.
#   ./tunnel.sh --check            # one-shot status report, exits 0/1.
#                                  # Changes nothing — safe for debugging.
#
# ── Point the bot at a URL you already have (no minting) ───────────
#   ./tunnel.sh --set-url https://<host>
# Validates the URL, writes it to .env through the atomic 0600 writer, and
# restarts the bot. This is the RECOVERY path for a stale MINI_APP_URL — the
# case that bit us on 2026-09-26: a transient quick tunnel clobbered the stable
# Funnel URL, the quick tunnel then died (Cloudflare 530), and funnel-mode
# supervision cannot repair a wrong URL by design (it never rewrites .env).
# It starts, stops and mints nothing; it only repoints the bot.
# Knobs (env): WATCH_INTERVAL (s, default 60), WATCH_FAILS (default 2),
#              WATCH_LOG (default ~/Library/Logs/packey/packey_tunnel_watch.log).
#
# ── Status mode: auto-logging one-liner (read-only) ────────────────
#   ./tunnel.sh --status [seconds] # default 120 s; Ctrl-C stops it.
# Every interval it prints ONE line — bot / local + public HTTP / tunnel /
# agent state / store counts — and appends the same line to $STATUS_LOG, so a
# terminal left open is a live view and the file keeps the history. Read-only:
# it never starts, stops or rotates anything. The line is privacy-safe by
# construction (counts and up/down only — no chat ids, numbers or names).
# Knobs (env): STATUS_INTERVAL (s, default 120), STATUS_LOG
#              (default ~/Library/Logs/packey/packey_status.log).
# The --check dashboard reads its quota section from QUOTA_LOG (default
# tracker.log) and falls back to QUOTA_LOG_FALLBACK (default ~/Library/Logs/packey/bot.out).
# Survive reboots (user-level, no sudo):
#   cp com.packey.tunnel.plist ~/Library/LaunchAgents/
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.packey.tunnel.plist
#
# ── Named tunnel (permanent URL — needs a domain you control) ───────
# One-time setup (browser opens on the first command):
#   cloudflared tunnel login
#   cloudflared tunnel create packagesbot
# Optional, for a friendly name (needs a domain you control on Cloudflare):
#   cloudflared tunnel route dns packagesbot packey.example.com
# Then, every time:
#   ./tunnel.sh --named packagesbot https://packey.example.com
# The script writes MINI_APP_URL for you and restarts the bot — same
# convenience as quick mode. Without a domain, pass the tunnel's own URL:
#   ./tunnel.sh --named packagesbot https://<tunnel-uuid>.cfargotunnel.com
# The URL never changes again — set it once and forget it.
set -euo pipefail

cd "$(dirname "$0")"
PORT="${MINI_APP_PORT:-8080}"
CLOUDFLARED="${CLOUDFLARED:-$(command -v cloudflared 2>/dev/null || echo /opt/homebrew/bin/cloudflared)}"
# Watch/status logs share the bot's private log home (M16): not world-writable
# /tmp. The dir is created 0700 here as well, so tunnel.sh works standalone.
LOG_DIR="${HOME}/Library/Logs/packey"
mkdir -p "$LOG_DIR" 2>/dev/null && chmod 700 "$LOG_DIR" 2>/dev/null || true
WATCH_LOG="${WATCH_LOG:-$LOG_DIR/packey_tunnel_watch.log}"
STATUS_LOG="${STATUS_LOG:-$LOG_DIR/packey_status.log}"
# Where the dashboard finds the newest 17TRACK quota reading. Two producers write
# it and they land in DIFFERENT files: refresh.py's hourly watch goes to the bot's
# own log, main.py's startup verification ("17TRACK key verified — quota: …") goes
# to the console. Reading only the first is why the section said "no quota reading
# found in tracker.log" while run.sh --status happily showed 19/200 (2026-09-26).
QUOTA_LOG="${QUOTA_LOG:-tracker.log}"
QUOTA_LOG_FALLBACK="${QUOTA_LOG_FALLBACK:-$LOG_DIR/bot.out}"
STATUS_INTERVAL="${STATUS_INTERVAL:-120}"
WATCH_INTERVAL="${WATCH_INTERVAL:-60}"
WATCH_FAILS="${WATCH_FAILS:-2}"
QUICK_PID=""
QUICK_LOG=""

# NOTE: the cloudflared-presence guard sits further down, right before the
# tunnel-starting modes — the read-only modes (--check/--status/--selftest)
# and --watch --funnel must work on a machine without cloudflared.

# ── Shared watch helpers ─────────────────────────────────────
# log() writes ONLY to the watch log — never stdout. (An earlier version
# printed to both, which polluted `url=$(start_quick_tunnel)` captures and
# killed the watcher via a sed error under set -e. Console output goes
# through note() instead.)
log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$WATCH_LOG"
}

note() {  # console-only sibling of log()
  printf '%s\n' "$*"
}

env_url() {
  grep -m1 '^MINI_APP_URL=' .env 2>/dev/null | cut -d= -f2- || true
}

set_env_url() {  # $1 = new public URL
  # Validated first (see valid_public_url): a multiline or malformed value
  # must never reach the rewrite — BSD sed aborts on embedded newlines and,
  # with set -e, that would take the whole supervisor down (post-mortem).
  valid_public_url "$1" || { log "ERROR: refusing to write invalid URL: [$1]"; return 1; }
  local tmp
  tmp="$(mktemp -t packey_env)"
  if [[ -f .env ]] && grep -q '^MINI_APP_URL=' .env 2>/dev/null; then
    awk -v url="$1" '{ if ($0 ~ /^MINI_APP_URL=/) print "MINI_APP_URL=" url; else print $0 }' .env >"$tmp"
  else
    [[ -f .env ]] && cat .env >"$tmp" || true
    printf '\n# Mini App (managed by tunnel.sh)\nMINI_APP_URL=%s\n' "$1" >>"$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" .env
}

# A public Mini App URL is exactly one https URL token — no whitespace, no
# newlines, nothing else. Two shapes are legitimate: a cloudflared quick tunnel
# (*.trycloudflare.com) and a Tailscale Funnel (*.<tailnet>.ts.net, the stable
# option — funnel mode sets one of these once and never rewrites it).
valid_public_url() {
  local u="${1:-}"
  # ts.net Funnel names carry TWO labels before the domain
  # (machine.tailnet.ts.net), so the pattern must allow repeated dotted labels —
  # while still refusing anything that merely contains "ts.net"
  # (e.g. https://evil.ts.net.attacker.com).
  [[ "$u" =~ ^https://[a-z0-9-]+\.trycloudflare\.com$ \
     || "$u" =~ ^https://([a-z0-9-]+\.)+ts\.net$ ]]
}

# HTTP code for a URL; exactly "000" when the connection fails (dead
# tunnel / bot down). (curl prints 000 itself on failure, so normalize any
# longer/empty residue to a single 000 to avoid double-counting.)
# Palette: ANSI only on a TTY -- the --status loop appends output to a log
# file, so escapes must never leak into non-TTY captures (verified with cat -v).
# TTY gate + the no-color.org opt-out: NO_COLOR set to any value forces the
# plain palette even on a TTY. Escapes use $'...' quoting -- '\033[..]' in
# plain quotes would print literally.
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_OK=$'\033[0;32m'      # checkmark: green
  C_BAD=$'\033[0;31m'     # cross: red
  C_WARN=$'\033[0;33m'    # unknown: yellow
  C_HDR=$'\033[0;36m'     # section headers: cyan
  C_DIM=$'\033[2m'        # frame rules and labels: dim
  C_FOOT_OK=$'\033[1;32m' # footer HEALTHY: bold green
  C_FOOT_BAD=$'\033[1;31m' # footer UNHEALTHY: bold red
  C_RST=$'\033[0m'
else
  C_OK='' C_BAD='' C_WARN='' C_HDR='' C_DIM=''
  C_FOOT_OK='' C_FOOT_BAD='' C_RST=''
fi
_QUOTA_CELLS=20


http_code() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$1" 2>/dev/null || true)"
  [[ "$code" =~ ^[0-9]{3}$ ]] && printf '%s' "$code" || printf '000'
}

bot_alive() {  # local backend answers (401 = healthy, auth required)
  [[ "$(http_code "http://127.0.0.1:${PORT}/api/packages")" != "000" ]]
}

restart_bot() {
  log "restarting bot"
  ./run.sh --background >>"$WATCH_LOG" 2>&1 || log "WARNING: run.sh failed"
}

code_is_healthy() {  # $1 = HTTP code from the Mini App's /api/packages
  # Healthy means OUR server answered: 401 (the auth-guarded API, the normal
  # answer) or 200. Anything else means the app is unreachable — 000 (no DNS),
  # 404/405 (a DIFFERENT host answering, e.g. cloudflared's registration API
  # once written into .env), 5xx (Cloudflare has no origin). The old rule only
  # rejected 000, so a wrong-host URL looked "healthy" forever (post-mortem
  # 2026-09-15 18:2x: every tap on "Open the app" returned
  # {"success":false,...,"code":10005,"message":"Method Not Allowed"}).
  [[ "$1" == "401" || "$1" == "200" ]]
}

public_path_report() {  # $1 = MINI_APP_URL; rc 0 healthy / 1 unhealthy / 2 unknown
  # The 2026-09-20 MagicDNS blind spot: local resolution answered 200 while
  # the public ts.net record was missing, so the phone got NXDOMAIN while
  # every local check said 200. Everything here rides PUBLIC resolvers
  # (1.1.1.1) and an explicit edge IP -- never the system DNS view. A miss
  # degrades the report; it never aborts it (set -e is active).
  local url="$1" host ip pcode
  host="$(printf '%s' "$url" | sed -E 's#^https?://([^/:]+).*#\1#' || true)"
  if [[ -z "$host" ]]; then
    echo "? (unparseable URL)"
    return 2
  fi
  if ! command -v dig >/dev/null 2>&1; then
    echo "? (dig not found)"
    return 2
  fi
  ip="$(dig @1.1.1.1 +short "$host" A 2>/dev/null | head -1 || true)"
  if [[ -z "$ip" ]]; then
    echo "NXDOMAIN — no public record (fix: tailscale funnel --bg 8080)"
    return 1
  fi
  pcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
             --resolve "${host}:443:${ip}" "$url/api/packages" 2>/dev/null || true)"
  [[ "$pcode" =~ ^[0-9]{3}$ ]] || pcode=000
  if code_is_healthy "$pcode"; then
    echo "edge ${ip} · http ${pcode}"
    return 0
  fi
  echo "edge ${ip} · http ${pcode}"
  return 1
}


humanize_secs() {  # 7200 -> 2h, 180 -> 3m, 90 -> 90s (hours then minutes)
  local s="$1"
  if (( s >= 3600 && s % 3600 == 0 )); then echo "$(( s / 3600 ))h"
  elif (( s >= 60 && s % 60 == 0 )); then echo "$(( s / 60 ))m"
  else echo "${s}s"; fi
}


_quota_bar_cells() {  # $1 = percent -> filled cells out of _QUOTA_CELLS
  echo $(( _QUOTA_CELLS * $1 / 100 ))
}


_quota_bar() {  # $1 = percent -> 20-cell bar (uncolored; caller adds color)
  local pct="$1" filled bar="" i
  (( pct > 100 )) && pct=100
  (( pct < 0 )) && pct=0
  filled="$(_quota_bar_cells "$pct")"
  for (( i = 0; i < _QUOTA_CELLS; i++ )); do
    if (( i < filled )); then bar+="█"; else bar+="░"; fi
  done
  printf '%s' "$bar"
}


tunnel_section() {  # $1 = url, $2 = local code, $3 = public report, $4 = public rc
  local url="$1" code="$2" pub_out="$3" pub_rc="$4" mark
  if (( pub_rc == 0 )); then mark="${C_OK}✓${C_RST}"
  elif (( pub_rc == 1 )); then mark="${C_BAD}✗${C_RST}"
  else mark="${C_WARN}?${C_RST}"; fi
  echo "${C_HDR}│ tunnel${C_RST}"
  echo "│   url     ${C_DIM}${url}${C_RST}"
  echo "│   public  ${mark} ${pub_out}"
  echo "│   dns     $(public_dns_state "$(url_host "$url")")"
  echo "│   local   http ${code} (direct via 127.0.0.1:${PORT})"
  echo "│   process $(tunnel_proc_report)"
}


bot_section() {
  local pid="" etime=""
  pid="$(pgrep -f '[m]ain.py' 2>/dev/null | head -1 || true)"
  echo "${C_HDR}│ bot${C_RST}"
  if bot_alive && [[ -n "$pid" ]]; then
    etime="$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ' || true)"
    echo "│   state ${C_OK}✓${C_RST} up (PID ${pid}${etime:+, up ${etime}})"
  else
    echo "│   state ${C_BAD}✗${C_RST} down"
  fi
  echo "│   store  $(status_store_counts)"
}


quota_section() {
  local line used total pct filled bar color hard armed src from="" left="" note=""
  echo "${C_HDR}│ 17TRACK quota${C_RST}"
  # Two producers, two files (see QUOTA_LOG): refresh.py's hourly watch and
  # main.py's startup verification. Take the newest line from either, so the
  # section stops going blank whenever the hourly one has not run yet.
  for src in "$QUOTA_LOG" "$QUOTA_LOG_FALLBACK"; do
    [[ -f "$src" ]] || continue
    line="$(grep -aE 'quota: [0-9]+/[0-9]+ used' "$src" 2>/dev/null | tail -1 || true)"
    [[ -n "$line" ]] && { from="$src"; break; }
  done
  if [[ -z "$line" ]]; then
    echo "│   ? no quota reading in ${QUOTA_LOG} or ${QUOTA_LOG_FALLBACK}"
    return 0
  fi
  used="$(printf '%s' "$line" | grep -oE 'quota: [0-9]+/[0-9]+ used' |
          grep -oE '[0-9]+' | head -1 || true)"
  total="$(printf '%s' "$line" | grep -oE 'quota: [0-9]+/[0-9]+ used' |
           grep -oE '[0-9]+' | sed -n '2p' || true)"
  # The startup line carries "N remaining" instead of a percentage; the hourly one
  # carries "(NN%)". Accept either and derive what is missing.
  left="$(printf '%s' "$line" | grep -oE '[0-9]+ remaining' |
          grep -oE '[0-9]+' | head -1 || true)"
  pct="$(printf '%s' "$line" | grep -oE '\([0-9]+%\)' | tr -dc '0-9' || true)"
  if [[ -z "$pct" && "$used" =~ ^[0-9]+$ && "$total" =~ ^[0-9]+$ && "$total" != "0" ]]; then
    pct=$(( used * 100 / total ))
  fi
  if ! [[ "$used" =~ ^[0-9]+$ && "$total" =~ ^[0-9]+$ && "$pct" =~ ^[0-9]+$ ]]; then
    echo "│   ? could not parse: ${line:0:80}"
    return 0
  fi
  # Say where it came from when that is not the primary log: an empty tracker.log
  # next to a populated console is expected, not a fault.
  [[ "$from" != "$QUOTA_LOG" ]] && note=" · from $(basename "$from")"
  bar="$(_quota_bar "$pct")"
  filled="$(_quota_bar_cells "$pct")"
  if (( pct >= 85 )); then color="$C_BAD"
  elif (( pct >= 70 )); then color="$C_WARN"
  else color="$C_OK"; fi
  hard="$(grep -E '^TRACK17_HARD_PCT' carriers.py 2>/dev/null |
          grep -oE '[0-9]+([.][0-9]+)?' | head -1 || true)"
  armed="off"
  if [[ -n "$hard" ]] && awk -v a="$pct" -v b="$hard" 'BEGIN { exit ! (a >= b) }'; then
    armed="ARMED (non-critical lanes x2)"
  fi
  printf '│   %s%s%s %s%% (%s/%s%s) · stretch: %s%s\n' "$color" "$bar" "$C_RST" \
    "$pct" "$used" "$total" "${left:+, ${left} left}" "$armed" "$note"
}


config_value() {  # $1 = config constant name -> its integer value, or "" if unknown
  # grep first: config.py holds plain literals for every lane constant. The
  # python3 fallback covers a value that ever becomes computed rather than written.
  local name="$1" val
  val="$(grep -m1 -E "^${name}[[:space:]]*=" config.py 2>/dev/null |
         sed -E 's/^[^=]*=[[:space:]]*//' | grep -oE '^[0-9]+' | head -1 || true)"
  if [[ -z "$val" ]]; then
    val="$(python3 -c "import config; print(int(config.${name}))" 2>/dev/null || true)"
  fi
  printf '%s' "$val"
}


resolve_ref() {  # $1 = a CADENCE token -> its number, or the token unchanged
  case "$1" in
    config.*)
      local v
      v="$(config_value "${1#config.}")"
      printf '%s' "${v:-$1}" ;;
    *) printf '%s' "$1" ;;
  esac
}


cadence_rows() {  # $1 = CADENCE block, $2 = pro interval, $3 = free interval
  # Echoes "name|pro|free" rows; resolves config.X references via $2/$3.
  local block="$1" pro_in="$2" free_in="$3" lane name a b
  printf '%s\n' "$block" | grep -E '": \(' | while IFS= read -r lane; do
    name="$(printf '%s' "$lane" | sed -E 's/^[[:space:]]*"([^"]+)".*/\1/')"
    a="$(printf '%s' "$lane" | sed -E 's/^[^(]*\(([^,]+).*/\1/' | tr -d ' ')"
    b="$(printf '%s' "$lane" | sed -E 's/^[^(]*\([^,]+, *([^)]+)\).*/\1/' | tr -d ' ')"
    case "$a" in
      *PRO_REFRESH_INTERVAL*) a="$pro_in" ;;
      *FREE_REFRESH_INTERVAL*) a="$free_in" ;;
      # Any OTHER config.X reference (OFD_FAST_PRO/FREE today) used to survive as a
      # literal and reach humanize_secs as "config.OFD_FAST_PROs" — arithmetic on a
      # string, so the row printed garbage and stderr noise (fixed 2026-09-26).
      *) a="$(resolve_ref "$a")" ;;
    esac
    case "$b" in
      *PRO_REFRESH_INTERVAL*) b="$pro_in" ;;
      *FREE_REFRESH_INTERVAL*) b="$free_in" ;;
      *) b="$(resolve_ref "$b")" ;;
    esac
    printf '%s|%s|%s\n' "$name" "$a" "$b"
  done
}


cadence_section() {
  local pro_in free_in block def_line def_pro def_free rows name pro_s free_s ann
  echo "${C_HDR}│ cadence (pro / free)${C_RST}"
  pro_in="$(grep -E '^PRO_REFRESH_INTERVAL' config.py 2>/dev/null |
            grep -oE '[0-9]+' | head -1 || true)"
  free_in="$(grep -E '^FREE_REFRESH_INTERVAL' config.py 2>/dev/null |
             grep -oE '[0-9]+' | head -1 || true)"
  block="$(sed -n '/^CADENCE: Dict/,/^}/p' refresh.py 2>/dev/null || true)"
  def_line="$(grep -E 'CADENCE.get.status,' refresh.py 2>/dev/null |
              sed -E 's/.*\(([^,]+), *([^)]+)\).*/\1|\2/' || true)"
  if [[ -z "$pro_in" || -z "$free_in" || -z "$block" || -z "$def_line" ]]; then
    echo "│   ? could not parse CADENCE"
    return 0
  fi
  def_pro="${def_line%%|*}"
  def_free="${def_line##*|}"
  rows="$(cadence_rows "$block" "$pro_in" "$free_in" || true)"
  if [[ -z "$rows" ]]; then
    echo "│   ? could not parse CADENCE"
    return 0
  fi
  while IFS='|' read -r name pro_s free_s; do
    [[ -z "$name" ]] && continue
    ann=""
    [[ "$name" == "Exception" ]] && ann="  (fast lane, never slowed)"
    printf '│   %-20s %s / %s%s\n' "$name" \
      "$(humanize_secs "$pro_s")" "$(humanize_secs "$free_s")" "$ann"
  done <<< "$rows"
  while IFS= read -r term; do
    [[ -z "$term" ]] && continue
    printf '│   %-20s -- (terminal: never re-checked)\n' "$term"
  done <<< "$(grep -E '^CADENCE_TERMINAL' refresh.py 2>/dev/null |
              grep -oE '"[A-Za-z ]+"' | tr -d '"' || true)"
  printf '│   %-20s %s / %s\n' "unknown default" \
    "$(humanize_secs "$def_pro")" "$(humanize_secs "$def_free")"
}


code_means_wrong_host() {  # $1 = HTTP code from the Mini App's /api/packages
  # Our API answers only 401 (no/invalid initData) or 200. Any other 4xx means
  # something ELSE is answering on that hostname — restarting the bot cannot fix
  # that, so it must always count as a tunnel failure (the 18:2x incident: .env
  # held cloudflared's registration API, which answers 405).
  [[ "$1" == 4?? && "$1" != "401" ]]
}

existing_url_is_worth_protecting() {  # $1 = current MINI_APP_URL ("" = none)
  # A stable URL that OUR server is still answering must never be silently
  # replaced by a transient quick tunnel (root cause of the 2026-09-18
  # outage: the funnel URL was clobbered, the quick tunnel later died, and
  # the funnel-mode supervisor — which never re-mints — could not repair it).
  [[ -n "$1" ]] && code_is_healthy "$(http_code "$1/api/packages")"
}

tunnel_url_from_log() {  # $1 = cloudflared log; prints the live tunnel URL or nothing
  # Success markers differ by cloudflared version: the "Your quick Tunnel has
  # been created!" box prints the URL on its own line, while some versions log
  # "Registered tunnel connection … url=https://…" inline. Require ONE of those
  # markers, then take the first trycloudflare URL that is not the registration
  # API. That API (api.trycloudflare.com) shows up in FAILED mints; mistaking it
  # for the tunnel URL once wrote it into .env and every "Open the app" tap got
  # {"success":false,...,"code":10005,"message":"Method Not Allowed"}.
  # (Post-mortem 2026-09-15 18:2x, plus the url=-only regression I shipped while
  #  fixing it — the box format is the one this cloudflared actually uses.)
  if ! grep -qE 'Registered tunnel connection|quick Tunnel has been created' "$1" 2>/dev/null; then
    return 0
  fi
  grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$1" 2>/dev/null \
    | grep -v '^https://api\.trycloudflare\.com$' \
    | head -1 || true
}

start_quick_tunnel() {
  # Sets NEW_URL (empty when cloudflared produced none in 30s) and
  # QUICK_PID/QUICK_LOG. Writes NOTHING to stdout — callers read NEW_URL, so
  # log() output can never pollute the value (post-mortem of the 01:50 bug).
  NEW_URL=""
  QUICK_LOG="$(mktemp -t packey_ct)"
  # shellcheck disable=SC2086
  nohup "$CLOUDFLARED" tunnel --url "http://127.0.0.1:${PORT}" >"$QUICK_LOG" 2>&1 &
  QUICK_PID=$!
  log "quick tunnel started pid=$QUICK_PID log=$QUICK_LOG"
  local url=""
  for _ in $(seq 1 30); do
    url="$(tunnel_url_from_log "$QUICK_LOG")"
    if valid_public_url "$url"; then
      NEW_URL="$url"
      break
    fi
    url=""
    sleep 1
  done
  [[ -z "$NEW_URL" ]] && log "ERROR: could not mint a tunnel URL (see $QUICK_LOG)"
  return 0
}

kill_quick_tunnel() {
  [[ -n "$QUICK_PID" ]] && kill "$QUICK_PID" 2>/dev/null || true
  pkill -f "cloudflared tunnel --url http://127.0.0.1:${PORT}" 2>/dev/null || true
  QUICK_PID=""
}

tailscale_bin() {  # PATH first; the macOS cask also installs this bundle path
  command -v tailscale 2>/dev/null ||
    { [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]] &&
        printf '%s' /Applications/Tailscale.app/Contents/MacOS/Tailscale; }
}


tailscale_funnel_on() {  # is the Funnel serving OUR port? (the stable path)
  local ts
  ts="$(tailscale_bin)" || return 1
  [[ -n "$ts" ]] || return 1
  "$ts" funnel status 2>/dev/null | grep -q "proxy http://127.0.0.1:${PORT}"
}


tunnel_proc_report() {  # specific to OUR tunnel: an unrelated cloudflared
  # (e.g. Cloudflare's own launchd service) must not report this tunnel healthy.
  # Funnel mode has no child process at all — the Funnel is served by the Tailscale
  # app — so "no tunnel process" read like a fault when it was the healthy steady
  # state. Name the mode instead (2026-09-26).
  if pgrep -f "cloudflared tunnel --url http://127.0.0.1:${PORT}" >/dev/null 2>&1; then
    echo "quick tunnel for port ${PORT} present"
  elif tailscale_funnel_on; then
    echo "tailscale funnel → 127.0.0.1:${PORT} (stable host)"
  elif pgrep -f 'tailscaled|tailscale funnel' >/dev/null 2>&1; then
    echo "tailscale present (funnel mode?)"
  else
    echo "no tunnel process for port ${PORT}"
  fi
}

url_host() {  # $1 = https URL -> bare hostname (no scheme/path)
  local host="${1#https://}"
  printf '%s' "${host%%/*}"
}


public_dns_state() {  # $1 = hostname -> what the PUBLIC resolvers say
  # The server's own answer for a ts.net/trycloudflare name comes from
  # MagicDNS-style local resolution and can be healthy while the public
  # record is missing (the 2026-09-20 outage: funnel "on", local 200s,
  # phones NXDOMAIN). Only an external resolver can see that gap.
  local host="$1" st
  if ! command -v dig >/dev/null 2>&1; then
    printf 'unverifiable (no dig)'
    return 0
  fi
  st="$(dig "@1.1.1.1" "$host" +noall +comments 2>/dev/null |
        grep -oE "status: [A-Z]+" | head -1)"
  if [[ "$st" != "status: NOERROR" ]]; then
    # NXDOMAIN (or resolver failure) — try the second public resolver
    # before declaring the record missing.
    st="$(dig "@8.8.8.8" "$host" +noall +comments 2>/dev/null |
          grep -oE "status: [A-Z]+" | head -1)"
  fi
  case "$st" in
    "status: NOERROR")    printf 'ok (public record exists)' ;;
    "status: NXDOMAIN")   printf 'NXDOMAIN (not published publicly)' ;;
    *)                    printf 'unverifiable (no answer)' ;;
  esac
}


status_report() {  # --check: framed read-only dashboard; exits 0 healthy / 1 not
  local url code="000" bot_state="down" pub_out="" pub_rc=0
  url="$(env_url)"
  if [[ -z "$url" ]]; then
    echo "MINI_APP_URL: (unset)"
    if bot_alive; then echo "bot: up (127.0.0.1:${PORT})"; else echo "bot: down"; fi
    return 1
  fi
  if bot_alive; then bot_state="up"; fi
  code="$(http_code "$url/api/packages")"
  # Public-path guard (the MagicDNS blind spot): public resolvers + an
  # explicit edge IP. rc 1 = dead, rc 2 = unverifiable (never flips health).
  pub_out="$(public_path_report "$url")" || pub_rc=$?
  echo "${C_HDR}┌─ packey tunnel dashboard ────────────────────────────${C_RST}"
  tunnel_section "$url" "$code" "$pub_out" "$pub_rc"
  bot_section
  quota_section
  cadence_section
  local healthy=1
  # Same health rule as the supervisor (code_is_healthy): a dead quick tunnel
  # answering 530 or a wrong host answering 404/405 is NOT healthy — the old
  # rule only rejected 000 and reported the 2026-09-18 outage as "healthy".
  code_is_healthy "$code" || healthy=0
  if (( pub_rc == 1 )); then healthy=0; fi   # rc 2 (unverifiable) never flips
  [[ "$bot_state" == "down" ]] && healthy=0
  if (( healthy )); then
    echo "└─ ${C_FOOT_OK}HEALTHY ✓${C_RST}"
    return 0
  fi
  echo "└─ ${C_FOOT_BAD}UNHEALTHY ✗${C_RST}"
  return 1
}

# ── Status mode ───────────────────────────────────────────────────
# One privacy-safe line per interval (counts / up-down only, never chat ids,
# numbers or names), printed for the terminal AND appended to $STATUS_LOG.
# Read-only: it must never start, stop or rotate anything — so every check is
# guarded and a failure reports as "?" instead of killing the loop.
status_store_counts() {  # "N chats / M packages", or "?" when unreadable
  python3 -c 'import config, storage; storage.load_data(config.DATA_FILE); print("%d chats / %d packages" % (len(storage.track_sessions), sum(len(v) for v in storage.track_sessions.values())))' 2>/dev/null \
    || printf '?'
}

status_agent_state() {  # what launchd knows about the supervisor
  local pid
  if ! launchctl list 2>/dev/null | grep -q 'com\.packey\.tunnel'; then
    printf 'NOT loaded'
    return 0
  fi
  pid="$(launchctl list 2>/dev/null | awk '/com\.packey\.tunnel/ {print $1}')"
  if [[ -z "$pid" || "$pid" == "-" ]]; then
    printf 'loaded (not running)'
  else
    printf 'running (PID %s)' "$pid"
  fi
}

status_line() {
  local url local_code code tunnel bot counts
  url="$(env_url)"
  local_code="$(http_code "http://127.0.0.1:${PORT}/api/packages")"
  # Public code FIRST: the tunnel field classifies it below.
  if [[ -n "$url" ]]; then
    code="$(http_code "$url/api/packages")"
  else
    code="no-url"
  fi
  if pgrep -f 'main\.py' >/dev/null 2>&1; then
    bot="up (PID $(pgrep -f 'main\.py' | head -1))"
  else
    bot="DOWN"
  fi
  if pgrep -f 'cloudflared tunnel --url' >/dev/null 2>&1; then
    tunnel="up (quick)"
  elif [[ "$url" == https://*\.ts\.net ]] && code_is_healthy "$code"; then
    tunnel="up (funnel)"        # stable Tailscale Funnel: no cloudflared process
  else
    tunnel="DOWN"
  fi
  counts="$(status_store_counts)"
  printf '%s  bot: %s | local: %s | public: %s | tunnel: %s | agent: %s | store: %s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$bot" "$local_code" "$code" "$tunnel" \
    "$(status_agent_state)" "$counts"
}

status_loop() {
  local interval="${1:-$STATUS_INTERVAL}"
  [[ "$interval" =~ ^[0-9]+$ ]] || interval="$STATUS_INTERVAL"
  trap 'printf "status stopping\n"; exit 0' INT TERM
  printf 'status loop started (interval=%ss, log=%s) — Ctrl-C stops it\n' \
    "$interval" "$STATUS_LOG"
  while true; do
    local line
    line="$(status_line || printf '%s  status unavailable\n' "$(date '+%Y-%m-%d %H:%M:%S')")"
    printf '%s\n' "$line"
    printf '%s\n' "$line" >>"$STATUS_LOG" 2>/dev/null || true
    sleep "$interval"
  done
}

ROTATE_COOLDOWN="${ROTATE_COOLDOWN:-300}"
fails_watch=0
LAST_ROTATE=0

watch_loop() {  # $1 = funnel mode ("1" = stable URL, manage nothing but the bot)
  local funnel="$1"
  log "watch started (funnel=$funnel, interval=${WATCH_INTERVAL}s, fails-to-rotate=${WATCH_FAILS})"
  trap 'log "watch stopping"; kill_quick_tunnel; exit 0' INT TERM
  while true; do
    watch_tick "$funnel" "$LAST_ROTATE" || true  # a tick never aborts the loop
    sleep "$WATCH_INTERVAL"
  done
}

# One health-check pass. Always returns 0 so set -e can never kill the
# supervisor (post-mortem of the 01:50 bug). Reports back via LAST_ROTATE.
watch_tick() {
  local funnel="$1" last_rotate="$2" url="" code="000" now
  now="$(date +%s)"
  if [[ "$funnel" != "1" ]]; then
    url="$(env_url)"
    if [[ -n "$url" ]]; then
      code="$(http_code "$url/api/packages")"
    fi
    if [[ -z "$url" ]] || ! code_is_healthy "$code"; then
      # Reachability means the URL answered with OUR server's code (see
      # code_is_healthy): 000 no DNS, 5xx Cloudflare-without-origin (a dead
      # quick tunnel keeps answering 530 — the 17:05 miss), 404/405 a DIFFERENT
      # host answering (the 18:2x api.trycloudflare.com incident). Re-mint.
      if ! code_means_wrong_host "$code" && ! bot_alive; then
        # Cloudflare/tunnel answered but the origin didn't (5xx / no DNS): fix it
        # in place. Minting a new URL for a dead bot would churn the user's
        # button for nothing. A wrong-host code is excluded above — see
        # code_means_wrong_host.
        log "backend down behind code=$code — restarting bot (no rotation)"
        restart_bot
        fails_watch=0
        LAST_ROTATE="$last_rotate"
        return 0
      fi
      fails_watch=$((fails_watch + 1))
      log "tunnel unhealthy (${fails_watch}/${WATCH_FAILS}) url=${url:-none} code=$code"
      if ((fails_watch >= WATCH_FAILS)); then
        if ((now - last_rotate >= ROTATE_COOLDOWN)); then
          kill_quick_tunnel
          start_quick_tunnel
          if [[ -z "$NEW_URL" ]]; then
            log "rotate skipped: no URL minted"
          elif [[ "$NEW_URL" == "$(env_url)" ]]; then
            log "same URL survived — no bot restart"
          elif set_env_url "$NEW_URL"; then
            log ".env updated: MINI_APP_URL=$NEW_URL"
            restart_bot
            last_rotate="$now"
            # Just restarted the bot: re-checking it in this same tick only finds
            # the port not yet bound and force-kills the instance now starting.
            LAST_ROTATE="$last_rotate"
            return 0
          else
            log "rotate skipped: .env write refused"
          fi
        else
          log "rotate skipped: cooldown active ($((ROTATE_COOLDOWN - (now - last_rotate)))s left)"
        fi
        fails_watch=0
      fi
    else
      fails_watch=0
    fi
  else
    url="$(env_url)"
    if [[ -n "$url" ]]; then
      code="$(http_code "$url/api/packages")"
      if [[ "$code" == "000" || "$code" == 5?? ]]; then
        log "stable URL unreachable: $url (bot or funnel down — checking bot)"
      fi
    else
      log "funnel mode: MINI_APP_URL unset — set it to the tailscale URL"
    fi
  fi
  if bot_alive; then
    :
  else
    log "local backend down on :${PORT} — restarting bot"
    restart_bot
  fi
  LAST_ROTATE="$last_rotate"
  return 0
}

# ── Watch / check dispatch (before the cloudflared-only modes) ────
if [[ "${1:-}" == "--watch" ]]; then
  shift
  if [[ "${1:-}" == "--funnel" ]]; then
    watch_loop "1"
  else
    # Quick-tunnel watch re-mints URLs, so it needs the binary.
    command -v cloudflared >/dev/null 2>&1 || {
      echo "cloudflared is not installed (needed for quick-tunnel watch)."
      echo "Install it with:  brew install cloudflared"
      exit 1
    }
    watch_loop "0"
  fi
  exit 0
fi

# Recovery: point the bot at a URL the operator already has. Deliberately NOT a
# minting mode — when the stable Funnel URL is healthy and .env holds a dead
# quick-tunnel URL, minting a THIRD URL is the wrong repair and would churn every
# "Open the app" button again. Reuses the validated atomic writer + the same
# restart the quick path uses, so there is one write-path in this script.
if [[ "${1:-}" == "--set-url" ]]; then
  URL_IN="${2:-}"
  if [[ -z "$URL_IN" ]]; then
    echo "usage: ./tunnel.sh --set-url <https-url>"
    echo "  get the current stable URL from: tailscale funnel status"
    exit 2
  fi
  if ! valid_public_url "$URL_IN"; then
    echo "refusing: not a usable public URL: [$URL_IN]"
    echo "expected https://<name>.trycloudflare.com or a Tailscale Funnel URL"
    exit 2
  fi
  if [[ "$URL_IN" == "$(env_url)" ]]; then
    echo "MINI_APP_URL is already ${URL_IN} — nothing to do."
    exit 0
  fi
  set_env_url "$URL_IN" || { echo "ERROR: refusing to write invalid URL"; exit 1; }
  echo "✅ .env updated: MINI_APP_URL=${URL_IN}"
  ./run.sh --background || true
  echo "✅ Bot restarted — verify with: ./tunnel.sh --check"
  exit 0
fi

# Self-test: exercise the rotation write-path against a scratch dir (no
# cloudflared, no bot restart, no touch of the live tree). Exit 0 = safe.
if [[ "${1:-}" == "--selftest" ]]; then
  SELF_TMP="$(mktemp -d -t packey_selftest)"
  trap 'rm -rf "$SELF_TMP"' EXIT
  # Isolate the logs: the selftest must never write into the live supervisor's
  # WATCH_LOG (its probe lines used to confuse later log reading).
  WATCH_LOG="$SELF_TMP/watch.log"
  STATUS_LOG="$SELF_TMP/status.log"
  printf 'TELEGRAM_BOT_API_TOKEN=stub\nMINI_APP_URL=https://old.example.com\n' >"$SELF_TMP/.env"
  cd "$SELF_TMP" || exit 1
  ok=0; bad=0
  t() { # $1 = description, $2 = shell snippet; eval keeps quoted multi-word
    # commands intact (plain "$@" would try to exec the description itself —
    # the harness's own bug, found by the selftest going 0/10)
    local desc="$1" snippet="$2"
    if eval "$snippet" >/dev/null 2>&1; then ok=$((ok + 1)); echo "ok   - $desc";
    else bad=$((bad + 1)); echo "FAIL - $desc"; fi
  }
  t "validator accepts a well-formed URL" 'valid_public_url "https://abc-def123.trycloudflare.com"'
  t "validator accepts a Tailscale Funnel URL" 'valid_public_url "https://fredps-macbook-pro.tail5c3a54.ts.net" && ! valid_public_url "https://evil.ts.net.attacker.com"'
  t "validator rejects a log-polluted value" 'v="$(printf "line\nhttps://x.trycloudflare.com")"; ! valid_public_url "$v"'
  t "validator rejects garbage + empty" '! valid_public_url "nope" && ! valid_public_url ""'
  t "set_env_url writes a valid URL" 'set_env_url "https://new-1.trycloudflare.com"'
  t "written value round-trips" '[ "$(env_url)" = "https://new-1.trycloudflare.com" ]'
  t "set_env_url refuses polluted value" 'v="$(printf "LOG\nhttps://x.trycloudflare.com")"; ! set_env_url "$v"'
  t "set_env_url refuses garbage" '! set_env_url "nope"'
  t "new-file path appends MINI_APP_URL" 'rm .env && set_env_url "https://fresh.trycloudflare.com" && [ "$(env_url)" = "https://fresh.trycloudflare.com" ]'
  t "log() writes only to the file" 'o="$(log "probe")"; [ -z "$o" ] && grep -q probe "$WATCH_LOG"'
  t "start_quick_tunnel sets globals, prints nothing" 'grep -q "Writes NOTHING to stdout" "$OLDPWD/tunnel.sh"'
  # Regression (post-mortem 2026-09-15 18:2x): a FAILED mint logs cloudflared's
  # registration API URL — it must never be mistaken for the tunnel URL, or .env
  # gets a host that answers Cloudflare's {"code":10005,"Method Not Allowed"}.
  t "failed mint yields no URL" 'printf "%s\n" "INF Requesting new quick Tunnel on trycloudflare.com..." "ERR failed to request quick Tunnel: Post \"https://api.trycloudflare.com/tunnel\": EOF" >fail.log; [ -z "$(tunnel_url_from_log fail.log)" ]'
  t "api.trycloudflare.com is never accepted" 'printf "%s\n" "Registered tunnel connection connIndex=0 url=https://api.trycloudflare.com" >api.log; [ -z "$(tunnel_url_from_log api.log)" ]'
  t "registered connection yields the real URL" 'printf "%s\n" "INF Requesting new quick Tunnel" "INF Registered tunnel connection connIndex=0 connection=abc url=https://good-name-123.trycloudflare.com location=ewr04" >ok.log; [ "$(tunnel_url_from_log ok.log)" = "https://good-name-123.trycloudflare.com" ]'
  # This cloudflared prints the URL in the creation box (no url= anywhere), which
  # is the format that actually appears in production logs.
  t "creation-box format yields the URL" 'printf "%s\n" "INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |" "INF |  https://closer-oil-season-sure.trycloudflare.com                                          |" >box.log; [ "$(tunnel_url_from_log box.log)" = "https://closer-oil-season-sure.trycloudflare.com" ]'
  t "no success marker yields no URL" 'printf "%s\n" "INF probing https://stray-host.trycloudflare.com" >nostray.log; [ -z "$(tunnel_url_from_log nostray.log)" ]'
  t "extraction tolerates a missing log" '[ -z "$(tunnel_url_from_log /nonexistent/log)" ]'
  # Reachability rule: only OUR server's answers count (401 = auth-guarded API).
  t "health rule accepts our server (401/200)" 'code_is_healthy 401 && code_is_healthy 200'
  t "health rule rejects wrong host / gone / no-DNS" '! code_is_healthy 000 && ! code_is_healthy 404 && ! code_is_healthy 405 && ! code_is_healthy 530 && ! code_is_healthy 502'
  # A 4xx that isn't our 401 = a foreign host; a bot restart can't fix that, so
  # it must always count toward a re-mint.
  t "wrong-host detection" 'code_means_wrong_host 404 && code_means_wrong_host 405 && code_means_wrong_host 403 && ! code_means_wrong_host 401 && ! code_means_wrong_host 000 && ! code_means_wrong_host 530'
  # URL-protection guard for the bare quick mode (offline cases only): an
  # empty or unreachable URL is replaceable; a live check needs the network
  # and is exercised by the production supervisor, not here.
  t "protection guard: no URL is replaceable" '! existing_url_is_worth_protecting ""'
  t "protection guard: unreachable URL is replaceable" '! existing_url_is_worth_protecting "https://dead-host-xyz.invalid"'
  # The two decisions that caused the 2026-09-18 outage, proven offline by
  # stubbing http_code inside a SUBSHELL (an override would leak into the
  # later t-cases in this same shell):
  t "healthy URL is protected from overwrite" '(http_code() { echo 401; }; existing_url_is_worth_protecting "https://stable.ts.net")'
  t "dead URL (530) is replaceable" '(http_code() { echo 530; }; ! existing_url_is_worth_protecting "https://dead.ts.net")'
  # The bare quick mode must go through the hardened write-path — the raw
  # sed rewrite is what once wrote api.trycloudflare.com into .env. Patterns
  # avoid "$" on purpose: inside eval + set -u an accidental expansion of an
  # unset var aborts the whole selftest.
  t "bare mode guards the old URL before writing" 'grep -q "existing_url_is_worth_protecting .*OLD_URL" "$OLDPWD/tunnel.sh"'
  # --set-url (post-mortem 2026-09-26): the recovery path exists so a wrong
  # MINI_APP_URL is fixed without minting a third tunnel, and it must go through
  # the same validated writer as everything else — a raw sed here is the exact
  # bug class that clobbered the stable Funnel URL.
  t "--set-url writes through the validated setter" 'grep -q "set_env_url \"\$URL_IN\"" "$OLDPWD/tunnel.sh"'
  t "--set-url validates the URL first" 'grep -q "valid_public_url \"\$URL_IN\"" "$OLDPWD/tunnel.sh"'
  # Scoped to the quick-mode section: --named legitimately keeps its own sed
  # (custom domains don't fit valid_public_url's two allowed shapes).
  t "bare mode has no raw sed rewrite of MINI_APP_URL" '! sed -n "/^#.*Quick tunnel mode/,\$p" "$OLDPWD/tunnel.sh" | grep -q "sed -i .*MINI_APP_URL"'
  t "bare mode mints via the hardened parser" 'grep -q "URL=.*tunnel_url_from_log .*LOG" "$OLDPWD/tunnel.sh"'
  # 2026-09-20 dashboard: public-path verdict, quota bar, humanizer, and the
  # CADENCE parser (offline — dig and curl are stubbed in subshells).
  t "public NXDOMAIN is unhealthy" \
    '(dig() { echo ""; }; out=$(public_path_report "https://x.ts.net"); [[ $out == *NXDOMAIN* && $? == 1 ]])'
  t "public 401 via stubbed edge is healthy" \
    '(dig() { echo "209.177.145.192"; }; curl() { echo 401; }; out=$(public_path_report "https://x.ts.net"); [[ $out == *edge* && $? == 0 ]])'
  t "quota bar 78 pct fills 15 of 20 cells" \
    '[[ $(_quota_bar_cells 78) == 15 && $(_quota_bar 78) == ███████████████░░░░░ ]]'
  t "humanizer 7200/180/90" \
    '[[ $(humanize_secs 7200) == 2h && $(humanize_secs 180) == 3m && $(humanize_secs 90) == 90s ]]'
  # 2026-09-26 dashboard fixes: a config.X lane reference is resolved (it printed
  # "config.OFD_FAST_PROs" before), the quota section accepts either producer's line
  # format from either file, and funnel mode is named instead of reported missing.
  t "cadence resolves a non-PRO/FREE config ref" \
    '(config_value() { echo 600; }; rows=$(cadence_rows "\"Out for Delivery\": (config.OFD_FAST_PRO, 1800)" 7200 14400); [[ "$rows" == *"Out for Delivery|600|1800"* ]])'
  t "quota: startup line (no percent) derives one" \
    '(printf "x 17TRACK key verified - quota: 19/200 used, 181 remaining.\n" > q.log; QUOTA_LOG=q.log; QUOTA_LOG_FALLBACK=/nonexistent; out=$(quota_section); [[ "$out" == *"19/200"* && "$out" == *"181 left"* && "$out" == *"9%"* ]])'
  t "quota: hourly line keeps its percent" \
    '(printf "x 17TRACK quota: 19/200 used (10%%) - 7 packages tracked\n" > q2.log; QUOTA_LOG=q2.log; QUOTA_LOG_FALLBACK=/nonexistent; out=$(quota_section); [[ "$out" == *"10%"* ]])'
  t "quota: falls back to the second file and says so" \
    '(printf "x quota: 19/200 used, 181 remaining.\n" > q3.log; QUOTA_LOG=/nonexistent; QUOTA_LOG_FALLBACK=q3.log; out=$(quota_section); [[ "$out" == *"19/200"* && "$out" == *"q3.log"* ]])'
  t "process report names funnel mode instead of a fault" \
    '(pgrep() { return 1; }; tailscale_bin() { echo tailscale; }; tailscale() { echo "|-- / proxy http://127.0.0.1:8080"; }; out=$(tunnel_proc_report); [[ "$out" == *funnel* ]])'
  t "cadence parser resolves config refs" \
    'rows=$(cadence_rows "\"In Transit\": (config.PRO_REFRESH_INTERVAL, config.FREE_REFRESH_INTERVAL),
  \"Out for Delivery\": (1800, 3600)" 7200 14400); [[ "$rows" == *"In Transit|7200|14400"* && "$rows" == *"Out for Delivery|1800|3600"* ]]'
  echo "selftest: $ok passed, $bad failed"
  if [[ "$bad" == "0" ]]; then exit 0; else exit 1; fi
fi

if [[ "${1:-}" == "--check" ]]; then
  status_report
  exit $?
fi

# ── Status mode: auto-logging one-liner every N seconds (read-only) ──
if [[ "${1:-}" == "--status" ]]; then
  shift
  status_loop "${1:-}"
  exit 0
fi

# cloudflared is only needed by the modes below that START a tunnel; the
# read-only modes and --watch --funnel were already dispatched above.
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Install it with:  brew install cloudflared"
  exit 1
fi

# ── Named tunnel mode: permanent URL ───────────────────────────────
if [[ "${1:-}" == "--named" ]]; then
  TUNNEL="${2:-}"
  PUBLIC_URL="${3:-}"
  if [[ -z "$TUNNEL" ]]; then
    echo "usage: ./tunnel.sh --named <tunnel-name> [public-url]   (e.g. packagesbot https://packey.example.com)"
    exit 1
  fi
  # With a public URL, this matches quick mode: .env gets MINI_APP_URL and
  # the bot restarts BEFORE the tunnel takes the foreground — every
  # subsequent restart is just: ./tunnel.sh --named <name> <url>
  if [[ -n "$PUBLIC_URL" ]]; then
    if [[ ! "$PUBLIC_URL" =~ ^https:// ]]; then
      echo "ERROR: public-url must start with https:// (got: $PUBLIC_URL)"
      exit 1
    fi
    echo "Named URL: $PUBLIC_URL"
    if grep -q '^MINI_APP_URL=' .env 2>/dev/null; then
      sed -i '' "s|^MINI_APP_URL=.*|MINI_APP_URL=${PUBLIC_URL}|" .env
    else
      printf '\n# Mini App (managed by tunnel.sh)\nMINI_APP_URL=%s\n' "$PUBLIC_URL" >> .env
    fi
    echo "✅ .env updated: MINI_APP_URL=${PUBLIC_URL}"
    ./run.sh --background || true
    echo "✅ Bot restarted with the new MINI_APP_URL."
  else
    echo "(No public-url given — MINI_APP_URL is left as-is. Pass the"
    echo " tunnel's public URL as a 3rd arg to have .env updated.)"
  fi
  echo "Starting named tunnel '$TUNNEL' → http://127.0.0.1:${PORT}"
  echo "(If this fails with 'tunnel not found', run first:"
  echo "  cloudflared tunnel login && cloudflared tunnel create $TUNNEL)"
  exec cloudflared tunnel run --url "http://127.0.0.1:${PORT}" "$TUNNEL"
fi

# ── Quick tunnel mode: self-healing ────────────────────────────────
# A quick-tunnel URL is TRANSIENT: it dies with this script and changes on
# every restart. It must never silently replace a stable URL that still works
# (the 2026-09-18 outage: ./tunnel.sh overwrote the healthy Funnel URL, the
# quick tunnel later died, and the funnel-mode supervisor could not repair
# it). Pass --force to overwrite a healthy URL deliberately.
FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
  shift || true
fi
echo "Starting quick tunnel → http://127.0.0.1:${PORT}"
LOG="$(mktemp -t glm_tunnel)"
cloudflared tunnel --url "http://127.0.0.1:${PORT}" >"$LOG" 2>&1 &
CF_PID=$!
trap 'kill "$CF_PID" 2>/dev/null || true' EXIT

echo "Waiting for tunnel URL..."
URL=""
for _ in $(seq 1 30); do
  # Same parser as the supervisor: require a success marker and never accept
  # cloudflared's registration API (the 2026-09-15 18:2x incident — the old
  # bare regex happily extracted api.trycloudflare.com from a FAILED mint).
  URL="$(tunnel_url_from_log "$LOG")"
  [[ -n "$URL" ]] && break
  sleep 1
done

if [[ -z "$URL" ]]; then
  echo "ERROR: could not get a tunnel URL. cloudflared output:"
  tail -20 "$LOG"
  exit 1
fi
echo "Tunnel URL: $URL"

# Guard the stable URL before touching .env (public value, safe to print).
OLD_URL="$(env_url)"
if [[ "$FORCE" != "1" ]] && existing_url_is_worth_protecting "$OLD_URL"; then
  echo "⚠️  MINI_APP_URL is still healthy ($OLD_URL) — leaving .env untouched."
  echo "   This quick tunnel ($URL) is running but NOT wired into the bot."
  echo "   Pass --force to replace the stable URL deliberately."
  exit 0
fi

# Route the write through the validated, atomic setter (never raw sed).
set_env_url "$URL" \
  || { echo "ERROR: refusing to write invalid URL"; exit 1; }
echo "✅ .env updated: MINI_APP_URL=${URL}"
[[ -n "$OLD_URL" && "$OLD_URL" != "$URL" ]] \
  && echo "   (previous URL was: $OLD_URL — revert by editing .env if needed)"

# Restart the bot so the Telegram menu button points at the new URL
./run.sh --background || true
echo "✅ Bot restarted with the new MINI_APP_URL."
echo "⚠️  Keep this script running — the tunnel must stay alive (Ctrl-C stops it)."
echo "⚠️  This URL is temporary; use Tailscale Funnel (see header) for a stable one."
wait "$CF_PID"