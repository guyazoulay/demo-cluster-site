#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
SERVER_SCRIPT="$SCRIPT_DIR/server.py"

if ! command -v brew >/dev/null 2>&1; then
  osascript -e 'display dialog "Homebrew is required. Install it from https://brew.sh, then run this app again." buttons {"OK"} default button "OK" with icon caution'
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  brew install python
fi

if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  SERVER_PIDS=$(pgrep -f "$SERVER_SCRIPT" || true)
  if [[ -n "$SERVER_PIDS" ]]; then
    kill $SERVER_PIDS
    for _ in {1..50}; do
      if ! lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
        break
      fi
      sleep 0.1
    done
    if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
      osascript -e 'display dialog "The previous Demo Cluster Site server did not stop. Close it, then run this launcher again." buttons {"OK"} default button "OK" with icon caution'
      exit 1
    fi
  else
    osascript -e 'display dialog "Port 8765 is in use by another application. Close that application, then run this launcher again." buttons {"OK"} default button "OK" with icon caution'
    exit 1
  fi
fi

python3 "$SERVER_SCRIPT" &
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" 2>/dev/null || true }
trap cleanup EXIT INT TERM

for _ in {1..50}; do
  if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
    open "http://127.0.0.1:8765/?refresh=$(date +%s)"
    wait "$SERVER_PID"
    exit $?
  fi
  sleep 0.1
done

osascript -e 'display dialog "Demo Cluster Site did not start. Review the terminal output, then run this launcher again." buttons {"OK"} default button "OK" with icon caution'
exit 1
