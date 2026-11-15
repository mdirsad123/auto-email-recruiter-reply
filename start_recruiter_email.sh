#!/usr/bin/env bash
# JobBot Recruiter Email — Ubuntu / Oracle Cloud FastAPI launcher
# Place this file in the project root, beside app/, requirements.txt and .env.
# Run: chmod +x start_recruiter_email.sh && ./start_recruiter_email.sh
# To refresh dependencies: ./start_recruiter_email.sh --install
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

log()  { printf '[JobBot] %s\n' "$*"; }
fail() { printf '[JobBot] ERROR: %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
  ""|--install) ;;
  --help|-h)
    printf 'Usage: ./start_recruiter_email.sh [--install]\n'
    printf 'Starts the recruiter FastAPI service; --install refreshes pip dependencies.\n'
    exit 0 ;;
  *) fail "Unknown option: $1 (use --help)" ;;
esac

[[ -f app/main.py ]] || fail "app/main.py not found. Put this script in the jobbot-recruiter-email project root."
[[ -f requirements.txt ]] || fail "requirements.txt not found."
[[ -f .env ]] || fail ".env not found. Copy .env.example to .env and configure it; never commit .env to Git."
command -v python3 >/dev/null 2>&1 || fail "python3 missing; install python3, python3-venv and python3-pip."

VENV="$PROJECT_DIR/.venv"
PY="$VENV/bin/python"
if [[ ! -x "$PY" ]]; then
  log "Creating Python virtual environment (.venv) ..."
  python3 -m venv "$VENV" || fail "Cannot create .venv. On Ubuntu: sudo apt update && sudo apt install -y python3-venv python3-pip"
fi

# First run installs requirements; later runs install only if key packages are absent.
if [[ "${1:-}" == "--install" ]] || ! "$PY" -c 'import fastapi,uvicorn,dotenv,jsonschema,selenium' >/dev/null 2>&1; then
  log "Installing/updating Python requirements ..."
  "$PY" -m pip install -r "$PROJECT_DIR/requirements.txt" || fail "pip install failed."
fi

export PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

# Diagnostic only: never echo tokens, passwords, email content or full .env.
"$PY" - <<'PY'
import os
import urllib.request
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path.cwd() / '.env', override=False)
port = os.getenv('API_PORT', '8090')
host = os.getenv('API_HOST', '0.0.0.0')
print(f'[JobBot] FastAPI configured on {host}:{port}')
print(f'[JobBot] Local health check: http://127.0.0.1:{port}/health')
web = any(os.getenv(k, 'false').strip().lower() in ('1', 'true', 'yes', 'on')
          for k in ('USE_CLAUDE_WEB', 'USE_CHATGPT_WEB'))
if web:
    address = os.getenv('CHROME_DEBUGGER_ADDRESS', '127.0.0.1:9222')
    try:
        with urllib.request.urlopen(f'http://{address}/json/version', timeout=2) as resp:
            data = resp.read(2048).decode('utf8', errors='ignore')
        if 'webSocketDebuggerUrl' in data:
            print('[JobBot] Browser debugger is reachable: ' + address)
        else:
            print('[JobBot] WARNING: Browser debugger did not return a usable Chrome DevTools response.')
    except Exception:
        print('[JobBot] WARNING: Claude/ChatGPT Web enabled but no Chrome DevTools session at ' + address)
        print('[JobBot] Web AI requests need a running, logged-in Linux Chromium/Chrome session on port 9222.')
    chrome_path = os.getenv('CHROME_PATH', '')
    if ':\\' in chrome_path or chrome_path.lower().endswith('.exe'):
        print('[JobBot] WARNING: CHROME_PATH in .env points to Windows. Set a Linux browser path on Ubuntu.')
    profile = os.getenv('CHROME_USER_DATA_DIR', '')
    if ':\\' in profile:
        print('[JobBot] WARNING: CHROME_USER_DATA_DIR in .env is a Windows path. Set a Linux path on Ubuntu.')
PY

log "Starting API (Ctrl+C to stop) ..."
# One worker/in-process lock is important for sequential Claude processing.
exec "$PY" -m app.main
