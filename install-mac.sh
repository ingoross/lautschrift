#!/usr/bin/env bash
# Lautschrift installer for macOS (Apple Silicon or Intel).
#   ./install-mac.sh             venv + packages + model
#   ./install-mac.sh --service   additionally install a launchd agent (autostart at login)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

PY="${PYTHON:-}"
for candidate in python3.13 python3.12 python3; do
    if [ -z "$PY" ] && command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; fi
done
[ -n "$PY" ] || { echo "Python 3.12 oder 3.13 installieren (z. B. brew install python@3.13)." >&2; exit 1; }

if [ ! -x .venv/bin/python ]; then
    say "Creating venv with $PY …"
    "$PY" -m venv .venv
fi
say "Installing Python packages …"
.venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip --upgrade >/dev/null
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements-mac.txt
say "Downloading model (skipped if present) …"
.venv/bin/python scripts/download_model.py

if [ "${1:-}" = "--service" ]; then
    PLIST="$HOME/Library/LaunchAgents/de.unfuture.lautschrift.plist"
    say "Installing launchd agent → $PLIST"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>de.unfuture.lautschrift</string>
  <key>ProgramArguments</key><array>
    <string>$HERE/.venv/bin/python</string>
    <string>$HERE/lautschrift_mac.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardErrorPath</key><string>$HOME/Library/Application Support/Lautschrift/launchd.log</string>
</dict></plist>
PL
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    say "Agent started. Stop: launchctl bootout gui/$(id -u) $PLIST"
fi

say "Done. Start with ./start-mac.sh (or ./install-mac.sh --service for autostart)."
say "On first start grant Python access under System Settings → Privacy & Security → Accessibility, Input Monitoring and Microphone."
