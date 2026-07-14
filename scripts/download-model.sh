#!/usr/bin/env bash
# Downloads the NVIDIA Parakeet TDT 0.6B v3 model (int8, ONNX) into
# models/parakeet-v3. Model © NVIDIA, CC-BY-4.0; int8 ONNX conversion by
# the k2-fsa/sherpa-onnx project.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)/models"
URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2"
ARCHIVE="$DIR/parakeet-v3-int8.tar.bz2"
EXTRACT="$DIR/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"

mkdir -p "$DIR"
if [ ! -d "$EXTRACT" ]; then
  echo "Downloading model (~465 MB) …"
  curl -L "$URL" -o "$ARCHIVE"
  echo "Extracting …"
  tar xjf "$ARCHIVE" -C "$DIR"
fi
ln -sfn "$EXTRACT" "$DIR/parakeet-v3"
echo "Done: $DIR/parakeet-v3"
ls -1 "$DIR/parakeet-v3"
