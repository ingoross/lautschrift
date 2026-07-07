#!/usr/bin/env bash
# Lädt das Parakeet-TDT-0.6B-v3-Modell (int8, ONNX) und legt es unter
# models/parakeet-v3 ab — dieselben Gewichte wie FluidVoice, hier für sherpa-onnx.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)/models"
URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2"
ARCHIVE="$DIR/parakeet-v3-int8.tar.bz2"
EXTRACT="$DIR/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"

mkdir -p "$DIR"
if [ ! -d "$EXTRACT" ]; then
  echo "Lade Modell (~465 MB) …"
  curl -L "$URL" -o "$ARCHIVE"
  echo "Entpacke …"
  tar xjf "$ARCHIVE" -C "$DIR"
fi
ln -sfn "$EXTRACT" "$DIR/parakeet-v3"
echo "Fertig: $DIR/parakeet-v3"
ls -1 "$DIR/parakeet-v3"
