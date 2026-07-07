#!/usr/bin/env python3
"""FluidVoice Lite — lokales Diktat für Linux/Wayland mit Parakeet TDT v3.

Selbe STT-Engine wie FluidVoice (nvidia/parakeet-tdt-0.6b-v3), hier via
sherpa-onnx (ONNX int8) auf CPU statt CoreML. 25 EU-Sprachen inkl. Deutsch,
automatische Spracherkennung.

Toggle-Ablauf (weil GNOME-Shortcuts nur beim Drücken feuern):
  1x Hotkey  -> Aufnahme startet (pw-record -> temp .wav)
  2x Hotkey  -> Aufnahme stoppt, Parakeet transkribiert, wtype tippt den Text
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

# --- Pfade / Konfig -------------------------------------------------------
HERE = Path(__file__).resolve().parent
MODEL_DIR = Path(
    os.environ.get("FVL_MODEL_DIR", HERE / "models" / "parakeet-v3")
)
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "fluidvoice-lite"
RUNTIME.mkdir(parents=True, exist_ok=True)
WAV = RUNTIME / "rec.wav"
PIDFILE = RUNTIME / "recorder.pid"

NUM_THREADS = int(os.environ.get("FVL_THREADS", "6"))
TRAILING_SPACE = os.environ.get("FVL_TRAILING_SPACE", "1") == "1"
SAMPLE_RATE = 16000


# --- kleine Helfer --------------------------------------------------------
def notify(msg: str, urgency: str = "normal") -> None:
    try:
        subprocess.run(
            ["notify-send", "-a", "FluidVoice Lite", "-u", urgency,
             "-h", "string:x-canonical-private-synchronous:fvl",
             "FluidVoice Lite", msg],
            check=False,
        )
    except FileNotFoundError:
        pass
    print(f"[fvl] {msg}", file=sys.stderr)


def is_recording() -> int | None:
    if not PIDFILE.exists():
        return None
    try:
        pid = int(PIDFILE.read_text().strip())
    except ValueError:
        PIDFILE.unlink(missing_ok=True)
        return None
    # läuft der Prozess noch?
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        PIDFILE.unlink(missing_ok=True)
        return None
    return pid


# --- Aufnahme -------------------------------------------------------------
def start_recording() -> None:
    WAV.unlink(missing_ok=True)
    proc = subprocess.Popen(
        ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1",
         "--format", "s16", str(WAV)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    PIDFILE.write_text(str(proc.pid))
    notify("🎙️  Aufnahme läuft … (Hotkey erneut = fertig)")


def stop_recording(pid: int) -> None:
    # SIGINT -> pw-record finalisiert die WAV-Datei sauber
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    # auf Prozessende / finalisierte Datei warten
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    PIDFILE.unlink(missing_ok=True)

    if not WAV.exists() or WAV.stat().st_size < 1024:
        notify("⚠️  Keine Audiodaten aufgenommen.", "critical")
        return

    notify("✍️  Transkribiere …")
    text = transcribe(WAV)
    if not text:
        notify("⚠️  Nichts erkannt.", "normal")
        return
    if TRAILING_SPACE:
        text += " "
    type_text(text)
    notify(f"✅  {text.strip()[:80]}")


# --- Transkription (Parakeet TDT v3 via sherpa-onnx) ----------------------
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
            f"Modelldatei nicht gefunden ({names}) in {MODEL_DIR}"
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


def transcribe(wav_path: Path) -> str:
    import numpy as np

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getsampwidth() == 2, "erwarte 16-bit PCM"
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

    rec = get_recognizer()
    stream = rec.create_stream()
    stream.accept_waveform(rate, samples)
    rec.decode_stream(stream)
    return stream.result.text.strip()


# --- Text einfügen --------------------------------------------------------
def type_text(text: str) -> None:
    try:
        subprocess.run(["wtype", text], check=True)
    except FileNotFoundError:
        # Fallback: in die Zwischenablage legen
        subprocess.run(["wl-copy"], input=text.encode(), check=False)
        notify("wtype fehlt — Text in Zwischenablage kopiert.", "critical")


# --- CLI ------------------------------------------------------------------
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
            print("usage: fvl.py transcribe <wav>", file=sys.stderr)
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
