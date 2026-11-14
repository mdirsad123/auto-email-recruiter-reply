#!/usr/bin/env bash
# Starts the shared Chromium remote-debugging browser in the background.
# Run from an XFCE/RustDesk graphical terminal on the Oracle Ubuntu machine.
set -Eeuo pipefail

PORT="${CHROME_DEBUG_PORT:-9222}"
PROFILE="${CHROME_USER_DATA_DIR:-$HOME/snap/chromium/common/jobbot-debug-profile}"
LOG_DIR="$HOME/.local/state/jobbot"
LOG_FILE="$LOG_DIR/chromium-debug.log"
BROWSER="$(command -v chromium || true)"

if [[ -z "$BROWSER" ]] && [[ -x /snap/bin/chromium ]]; then
  BROWSER=/snap/bin/chromium
fi
if [[ -z "$BROWSER" ]]; then
  echo "ERROR: Chromium not installed. Install with: sudo snap install chromium" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: curl is required. Install with: sudo apt install -y curl" >&2
  exit 1
fi
if [[ "$PORT" != 9222 && ! "$PORT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: CHROME_DEBUG_PORT must be a number" >&2
  exit 1
fi

check_port() {
  curl --noproxy '*' --silent --show-error --fail --max-time 2 \
    "http://127.0.0.1:${PORT}/json/version" 2>/dev/null | grep -q 'webSocketDebuggerUrl'
}

if check_port; then
  echo "OK: Chromium debugger already running at http://127.0.0.1:${PORT}"
  exit 0
fi

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "ERROR: No desktop DISPLAY detected." >&2
  echo "Open Terminal *inside your RustDesk / XFCE Ubuntu desktop* and run this file there." >&2
  exit 1
fi

mkdir -p "$PROFILE" "$LOG_DIR"
chmod 700 "$PROFILE"

# nohup + setsid detaches Chromium from the initiating terminal.
# Do not expose the Chrome DevTools port to the network.
nohup setsid "$BROWSER" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  >>"$LOG_FILE" 2>&1 </dev/null &
BROWSER_PID=$!
# Detach from the current shell job table if this is an interactive Bash shell.
disown "$BROWSER_PID" 2>/dev/null || true

for _ in {1..20}; do
  if check_port; then
    echo "OK: Chromium launched in background (PID: $BROWSER_PID)."
    echo "Debugger: http://127.0.0.1:${PORT}/json/version"
    echo "Profile: $PROFILE"
    echo "Log: $LOG_FILE"
    echo "You can close this terminal; keep your Ubuntu desktop session running."
    exit 0
  fi
  if ! kill -0 "$BROWSER_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done

echo "ERROR: Debugger did not become ready on port $PORT." >&2
echo "Check log: tail -n 40 '$LOG_FILE'" >&2
echo "If another Chromium window is using this profile, close it and retry." >&2
exit 1
