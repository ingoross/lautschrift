#!/usr/bin/env python3
"""Lautschrift CLI — minimal dictation toggle without overlay/daemon.

Same STT engine as the daemon (nvidia/parakeet-tdt-0.6b-v3 via sherpa-onnx,
ONNX int8 on CPU). 25 European languages incl. German, automatic language
detection.

Useful as an alternative to the daemon: bind `lautschrift_cli.py toggle` to
a compositor shortcut (e.g. a GNOME custom shortcut). Note that it types
the text with wtype, which works on wlroots compositors (Sway, Hyprland, …)
but NOT on GNOME — there, use the daemon (lautschrift.py) instead.

Toggle flow (compositor shortcuts only fire on key-down):
  1st hotkey -> recording starts (pw-record/parecord -> temp .wav)
  2nd hotkey -> recording stops, Parakeet transcribes, wtype types the text
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

APP_NAME = "Lautschrift"

# --- Paths / config ---------------------------------------------------------
HERE = Path(__file__).resolve().parent
MODEL_DIR = Path(
    os.environ.get("LAUT_MODEL_DIR", HERE / "models" / "parakeet-v3")
)
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "lautschrift"
RUNTIME.mkdir(parents=True, exist_ok=True)
WAV = RUNTIME / "rec.wav"
PIDFILE = RUNTIME / "recorder.pid"

NUM_THREADS = int(os.environ.get("LAUT_THREADS", "6"))
TRAILING_SPACE = os.environ.get("LAUT_TRAILING_SPACE", "1") == "1"
SAMPLE_RATE = 16000


# --- Small helpers ----------------------------------------------------------
def notify(msg: str, urgency: str = "normal") -> None:
    try:
        subprocess.run(
            ["notify-send", "-a", APP_NAME, "-u", urgency,
             "-h", "string:x-canonical-private-synchronous:lautschrift",
             APP_NAME, msg],
            check=False,
        )
    except FileNotFoundError:
        pass
    print(f"[lautschrift] {msg}", file=sys.stderr)


def record_command(wav: str) -> list[str]:
    """Recorder CLI: PipeWire (pw-record) preferred, PulseAudio as fallback."""
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1",
                "--format", "s16", wav]
    if shutil.which("parecord"):
        return ["parecord", f"--rate={SAMPLE_RATE}", "--channels=1",
                "--format=s16le", "--file-format=wav", wav]
    raise FileNotFoundError(
        "no audio recorder found — install pipewire-utils (pw-record) "
        "or pulseaudio-utils (parecord)"
    )


def is_recording() -> int | None:
    if not PIDFILE.exists():
        return None
    try:
        pid = int(PIDFILE.read_text().strip())
    except ValueError:
        PIDFILE.unlink(missing_ok=True)
        return None
    # is the process still alive?
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        PIDFILE.unlink(missing_ok=True)
        return None
    return pid


# --- Recording --------------------------------------------------------------
def start_recording() -> None:
    WAV.unlink(missing_ok=True)
    proc = subprocess.Popen(
        record_command(str(WAV)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    PIDFILE.write_text(str(proc.pid))
    notify("🎙️  Recording … (press the hotkey again to finish)")


def stop_recording(pid: int) -> None:
    # SIGINT -> pw-record finalizes the WAV file cleanly
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    # wait for the process to exit / the file to be finalized
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    PIDFILE.unlink(missing_ok=True)

    if not WAV.exists() or WAV.stat().st_size < 1024:
        notify("⚠️  No audio recorded.", "critical")
        return

    notify("✍️  Transcribing …")
    text = transcribe(WAV)
    if not text:
        notify("⚠️  Nothing recognized.", "normal")
        return
    if TRAILING_SPACE:
        text += " "
    type_text(text)
    notify(f"✅  {text.strip()[:80]}")


# --- Transcription (Parakeet TDT v3 via sherpa-onnx) -------------------------
_recognizer = None


def get_recognizer():
    global _recognizer
    if _recognizer is not None:
        return _recognizer
    import sherpa_onnx

    def pick(*names: str) -> str:
        for n in names:
            p = MODEL_DIR / n
            if p.exists():
                return str(p)
        raise FileNotFoundError(
            f"model file not found ({names}) in {MODEL_DIR} — "
            "run scripts/download-model.sh"
        )

    _recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=pick("encoder.int8.onnx", "encoder.onnx"),
        decoder=pick("decoder.int8.onnx", "decoder.onnx"),
        joiner=pick("joiner.int8.onnx", "joiner.onnx"),
        tokens=pick("tokens.txt"),
        num_threads=NUM_THREADS,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
        model_type="nemo_transducer",
    )
    return _recognizer


def read_samples(wav_path: Path):
    """Read 16-bit PCM; tolerate an unfinalized WAV header (parecord)."""
    import numpy as np

    try:
        with wave.open(str(wav_path), "rb") as wf:
            assert wf.getsampwidth() == 2, "expected 16-bit PCM"
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        if frames:
            return rate, np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        pass
    raw = wav_path.read_bytes()
    pcm = raw[44:]
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return SAMPLE_RATE, np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(wav_path: Path) -> str:
    rate, samples = read_samples(wav_path)
    rec = get_recognizer()
    stream = rec.create_stream()
    stream.accept_waveform(rate, samples)
    rec.decode_stream(stream)
    return stream.result.text.strip()


# --- Text output --------------------------------------------------------------
def type_text(text: str) -> None:
    try:
        subprocess.run(["wtype", text], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        # Fallback: put it in the clipboard
        subprocess.run(["wl-copy"], input=text.encode(), check=False)
        notify("wtype missing/failed — text copied to the clipboard.", "critical")


# --- CLI ----------------------------------------------------------------------
def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "toggle"

    if cmd == "toggle":
        pid = is_recording()
        if pid is None:
            start_recording()
        else:
            stop_recording(pid)
    elif cmd == "start":
        if is_recording() is None:
            start_recording()
    elif cmd == "stop":
        pid = is_recording()
        if pid is not None:
            stop_recording(pid)
    elif cmd == "transcribe":
        if len(sys.argv) < 3:
            print("usage: lautschrift_cli.py transcribe <wav>", file=sys.stderr)
            return 2
        print(transcribe(Path(sys.argv[2])))
    elif cmd == "status":
        pid = is_recording()
        print("recording" if pid else "idle")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
