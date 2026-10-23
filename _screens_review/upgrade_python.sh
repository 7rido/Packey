#!/bin/bash
# Installs Python 3.12 next to the system 3.9 and builds ~/glm-tracker/.venv for Packey.
# Safe to re-run. It does NOT touch the running bot, run.sh, or the system Python.
set -euo pipefail

PROJ="$HOME/glm-tracker"
cd "$PROJ"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew was not found on PATH. Install it from https://brew.sh and run this again."
  exit 1
fi

echo "==> 1/6 Saving the current Python 3.9 package list (for reference)"
/usr/bin/python3 -m pip freeze > "$PROJ/_screens_review/py39_freeze.txt" 2>/dev/null || true

echo "==> 2/6 Installing Python 3.12 and zbar (barcode library)"
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
brew list zbar >/dev/null 2>&1 || brew install zbar
PY="$(brew --prefix python@3.12)/bin/python3.12"
"$PY" --version

echo "==> 3/6 Creating $PROJ/.venv"
[ -x "$PROJ/.venv/bin/python" ] || "$PY" -m venv "$PROJ/.venv"
VPY="$PROJ/.venv/bin/python"
"$VPY" -m pip install -q --upgrade pip

echo "==> 4/6 Installing the bot's packages (aiogram 3.31 for Rich Messages)"
grep -v '^aiogram' "$PROJ/requirements.txt" > /tmp/packey-req.txt
"$VPY" -m pip install -q -r /tmp/packey-req.txt "aiogram==3.31.0" playwright pyzbar

echo "==> 5/6 Downloading the Playwright browser"
"$VPY" -m playwright install chromium

echo "==> 6/6 Checking"
"$VPY" - <<'PYEOF'
import sys, aiogram
print("python ", sys.version.split()[0])
print("aiogram", aiogram.__version__)
for m in ("aiohttp", "httpx", "dotenv", "cryptography", "nacl", "playwright", "pyzbar"):
    __import__(m); print("ok     ", m)
PYEOF

echo
echo "Packages in the old 3.9 setup that are missing from the new one (blank = none):"
"$VPY" -m pip freeze 2>/dev/null | cut -d= -f1 | tr 'A-Z_' 'a-z-' | sort -u > /tmp/packey-new.txt
cut -d= -f1 "$PROJ/_screens_review/py39_freeze.txt" 2>/dev/null | tr 'A-Z_' 'a-z-' | sort -u > /tmp/packey-old.txt
comm -23 /tmp/packey-old.txt /tmp/packey-new.txt | tr '\n' ' '
echo
echo
echo "Done. Nothing was restarted. Tell Claude it finished and paste the lines above."
