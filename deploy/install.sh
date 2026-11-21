#!/bin/bash
# Sentinel installer — sets up a venv, installs deps, and loads the launchd
# agents (cycle every 15 min + report every 3h). Idempotent; safe to re-run.
set -euo pipefail

SENTINEL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SENTINEL_HOME="${SENTINEL_HOME:-$HOME/.sentinel}"
PYTHON="${PYTHON:-$(command -v python3)}"
LA_DIR="$HOME/Library/LaunchAgents"

echo "Sentinel dir : $SENTINEL_DIR"
echo "Sentinel home: $SENTINEL_HOME"
echo "Python       : $PYTHON"

mkdir -p "$SENTINEL_HOME/logs" "$LA_DIR"

# venv + deps
if [ ! -d "$SENTINEL_DIR/.venv" ]; then
  "$PYTHON" -m venv "$SENTINEL_DIR/.venv"
fi
VENV_PY="$SENTINEL_DIR/.venv/bin/python"
"$VENV_PY" -m pip install -q --upgrade pip
"$VENV_PY" -m pip install -q -e "$SENTINEL_DIR" || "$VENV_PY" -m pip install -q httpx anthropic google-api-python-client google-auth-oauthlib google-auth-httplib2

# render + install launchd plists
render() {
  sed -e "s|__PYTHON__|$VENV_PY|g" \
      -e "s|__SENTINEL_DIR__|$SENTINEL_DIR|g" \
      -e "s|__SENTINEL_HOME__|$SENTINEL_HOME|g" \
      -e "s|__PATH__|/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$HOME/.local/bin|g" \
      "$1"
}
for job in daemon cycle report; do
  plist="$LA_DIR/com.sentinel.$job.plist"
  render "$SENTINEL_DIR/deploy/launchd/com.sentinel.$job.plist" > "$plist"
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "loaded com.sentinel.$job"
done

echo
echo "Installed. Verify with:  launchctl list | grep sentinel"
echo "Run one cycle now:       PYTHONPATH=$SENTINEL_DIR $VENV_PY -m conductor.scheduler auto"
echo "Send a test report:      PYTHONPATH=$SENTINEL_DIR $VENV_PY -m conductor.scheduler report"
