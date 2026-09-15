#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Bitte zuerst ./install-mac.sh ausführen."; exit 1; }
exec .venv/bin/python lautschrift_mac.py "$@"
