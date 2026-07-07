#!/usr/bin/env python3
"""FluidVoice Lite Daemon — OS-weites Diktat für Linux/Wayland.

- Hotkey: Copilot-Taste (sendet Meta+Shift+F23; wir triggern auf F23/Code 193)
  1x = Aufnahme an, 2x = Aufnahme aus.
- STT: Parakeet TDT 0.6B v3 (int8, sherpa-onnx) — identisch zu FluidVoice.
- Live-Overlay: kleines GTK-Fenster zeigt den Text beim Sprechen (Puffer wird
  alle ~0,8 s neu dekodiert — der Offline-Parakeet kann kein echtes Streaming,
  das Neu-Dekodieren wirkt aber live).
- Bei Stopp: fertiger Text -> Zwischenablage (wl-copy) UND ins fokussierte
  Feld getippt (wtype).

Global lauffähig, weil wir die Tastatur direkt über evdev lesen (User in
Gruppe 'input'). Start:  .venv/bin/python fvld.py
"""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Gdk  # noqa: E402

from evdev import InputDevice, list_devices, ecodes  # noqa: E402

# --- Konfiguration --------------------------------------------------------
HERE = Path(__file__).resolve().parent
MODEL_DIR = Path(os.environ.get("FVL_MODEL_DIR", HERE / "models" / "parakeet-v3"))
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "fluidvoice-lite"
RUNTIME.mkdir(parents=True, exist_ok=True)
WAV = RUNTIME / "rec.wav"

TRIGGER_CODE = int(os.environ.get("FVL_TRIGGER_CODE", ecodes.KEY_F23))  # 193
NUM_THREADS = int(os.environ.get("FVL_THREADS", "6"))
DECODE_INTERVAL = float(os.environ.get("FVL_DECODE_INTERVAL", "0.8"))
TRAILING_SPACE = os.environ.get("FVL_TRAILING_SPACE", "1") == "1"
SAMPLE_RATE = 16000

# ydotool injiziert auf Kernel-Ebene (/dev/uinput) — funktioniert auf GNOME
# Wayland, wo wtype (virtual-keyboard-Protokoll) scheitert.
YDOTOOL_SOCKET = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/tmp"), ".ydotool_socket"
)
# Textausgabe: Zwischenablage + Einfüge-Kürzel (layout-unabhängig, exakte
# Umlaute). ydotool "type" scheitert am de-Layout (y/z vertauscht, Umlaute
# verschluckt), deshalb Paste statt Tippen.
# Terminals brauchen meist ctrl+shift+v, GUI-Apps ctrl+v.
PASTE_KEY = os.environ.get("FVL_PASTE_KEY", "ctrl+v")
_KEYCODES = {  # Linux input-event-Codes (layout-neutral)
    "ctrl": 29, "shift": 42, "alt": 56, "super": 125,
    "v": 47, "insert": 110,
}

# Start-/Stopp-Töne (wie bei FluidVoice). Über FVL_SOUND_* überschreibbar.
_FD = "/usr/share/sounds/freedesktop/stereo"
SOUND_START = os.environ.get("FVL_SOUND_START", f"{_FD}/message-new-instant.oga")
SOUND_STOP = os.environ.get("FVL_SOUND_STOP", f"{_FD}/complete.oga")


def play_sound(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        subprocess.Popen(["pw-play", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        try:
            subprocess.Popen(["paplay", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass


# --- STT ------------------------------------------------------------------
class Engine:
    def __init__(self) -> None:
        import sherpa_onnx

        def pick(*names: str) -> str:
            for n in names:
                p = MODEL_DIR / n
                if p.exists():
                    return str(p)
            raise FileNotFoundError(f"{names} nicht in {MODEL_DIR}")

        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
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
        self._lock = threading.Lock()

    def decode(self, samples: np.ndarray) -> str:
        if samples.size == 0:
            return ""
        with self._lock:
            stream = self.rec.create_stream()
            stream.accept_waveform(SAMPLE_RATE, samples)
            self.rec.decode_stream(stream)
            return stream.result.text.strip()


# --- Overlay --------------------------------------------------------------
CSS = b"""
/* Fenster selbst transparent -> hinter den runden Ecken sieht man den Desktop */
window.fvl-win { background: transparent; }
.fvl-box { background: rgba(20,20,24,0.94); border-radius: 18px;
           padding: 18px 24px; margin: 14px;
           box-shadow: 0 10px 30px rgba(0,0,0,0.55); }
.fvl-title { color: #7aa2ff; font-weight: 700; font-size: 13px; }
.fvl-text { color: #f0f0f4; font-size: 15px; }
.fvl-rec { color: #ff5f6d; font-weight: 700; }
.fvl-proc { color: #7aa2ff; font-weight: 700; }
"""


class Overlay:
    def __init__(self, app: Gtk.Application) -> None:
        self.win = Gtk.ApplicationWindow(application=app)
        self.win.set_decorated(False)
        self.win.add_css_class("fvl-win")
        self.win.set_default_size(560, 0)
        self.win.set_resizable(False)
        try:
            self.win.set_can_focus(False)
        except Exception:
            pass

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("fvl-box")
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        self.title = Gtk.Label(label="●  Aufnahme läuft …")
        self.title.add_css_class("fvl-title")
        self.title.add_css_class("fvl-rec")
        self.title.set_xalign(0)
        header.append(self.spinner)
        header.append(self.title)
        self.text = Gtk.Label(label="")
        self.text.add_css_class("fvl-text")
        self.text.set_xalign(0)
        self.text.set_wrap(True)
        self.text.set_max_width_chars(46)
        box.append(header)
        box.append(self.text)
        self.win.set_child(box)

    def show_recording(self) -> bool:
        self.text.set_text("")
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.title.set_text("●  Aufnahme läuft …")
        self.title.remove_css_class("fvl-proc")
        self.title.add_css_class("fvl-rec")
        self.win.present()
        return False

    def show_processing(self) -> bool:
        self.spinner.set_visible(True)
        self.spinner.start()
        self.title.set_text("Transkribiere …")
        self.title.remove_css_class("fvl-rec")
        self.title.add_css_class("fvl-proc")
        self.win.present()
        return False

    def hide(self) -> bool:
        self.spinner.stop()
        self.win.set_visible(False)
        return False

    def set_text(self, s: str) -> bool:
        self.text.set_text(s or "…")
        return False


# --- Daemon ---------------------------------------------------------------
class Daemon:
    def __init__(self) -> None:
        self.engine = Engine()
        self.recording = False
        self.rec_proc: subprocess.Popen | None = None
        self.decode_thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.app = Gtk.Application(application_id="de.unfuture.fluidvoicelite")
        self.app.connect("activate", self._on_activate)

    # ---- GTK lifecycle ----
    def _on_activate(self, app: Gtk.Application) -> None:
        self.overlay = Overlay(app)
        app.hold()  # ohne sichtbares Fenster am Leben bleiben
        threading.Thread(target=self._key_loop, daemon=True).start()
        print("[fvld] bereit. Copilot-Taste drücken zum Diktieren.", flush=True)

    # ---- Hotkey (evdev, global) ----
    def _key_loop(self) -> None:
        devs = []
        for path in list_devices():
            try:
                d = InputDevice(path)
                caps = d.capabilities()
                if ecodes.EV_KEY in caps and TRIGGER_CODE in caps[ecodes.EV_KEY]:
                    devs.append(d)
            except Exception:
                pass
        # Fallback: alle Tastaturen
        if not devs:
            for path in list_devices():
                try:
                    d = InputDevice(path)
                    caps = d.capabilities()
                    if ecodes.EV_KEY in caps and ecodes.KEY_A in caps[ecodes.EV_KEY]:
                        devs.append(d)
                except Exception:
                    pass
        if not devs:
            print("[fvld] keine Tastatur gefunden (input-Gruppe?)", flush=True)
            return
        print(f"[fvld] lausche auf {len(devs)} Tastatur(en) für Code {TRIGGER_CODE}", flush=True)

        sel = selectors.DefaultSelector()
        for d in devs:
            sel.register(d, selectors.EVENT_READ)
        last = 0.0
        while True:
            for key, _ in sel.select():
                for ev in key.fileobj.read():
                    if ev.type == ecodes.EV_KEY and ev.code == TRIGGER_CODE and ev.value == 1:
                        now = time.monotonic()
                        if now - last < 0.3:  # Entprellen
                            continue
                        last = now
                        GLib.idle_add(self.toggle)

    # ---- Toggle ----
    def toggle(self) -> bool:
        if not self.recording:
            self._start()
        else:
            self._stop()
        return False  # idle_add: nicht wiederholen

    def _start(self) -> None:
        self.recording = True
        self.stop_flag.clear()
        play_sound(SOUND_START)
        WAV.unlink(missing_ok=True)
        self.rec_proc = subprocess.Popen(
            ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1",
             "--format", "s16", str(WAV)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.overlay.show_recording()
        self.decode_thread = threading.Thread(target=self._live_decode, daemon=True)
        self.decode_thread.start()

    def _stop(self) -> None:
        self.recording = False
        self.stop_flag.set()
        play_sound(SOUND_STOP)
        # Overlay sichtbar lassen und auf "Transkribiere …" (Spinner) umschalten
        self.overlay.show_processing()
        if self.rec_proc:
            try:
                self.rec_proc.send_signal(signal.SIGINT)
                self.rec_proc.wait(timeout=3)
            except Exception:
                pass
            self.rec_proc = None
        # Finaltext in eigenem Thread (blockiert GTK nicht -> Spinner läuft)
        threading.Thread(target=self._finalize, daemon=True).start()

    # ---- Live-Dekodierung ----
    @staticmethod
    def _read_samples() -> np.ndarray:
        try:
            raw = WAV.read_bytes()
        except FileNotFoundError:
            return np.zeros(0, np.float32)
        if len(raw) <= 44:
            return np.zeros(0, np.float32)
        pcm = raw[44:]
        if len(pcm) % 2:
            pcm = pcm[:-1]
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    def _live_decode(self) -> None:
        while not self.stop_flag.is_set():
            time.sleep(DECODE_INTERVAL)
            if self.stop_flag.is_set():
                break
            samples = self._read_samples()
            if samples.size < SAMPLE_RATE // 4:  # <0,25s ignorieren
                continue
            text = self.engine.decode(samples)
            GLib.idle_add(self.overlay.set_text, text)

    def _finalize(self) -> None:
        # sauber finalisierte Datei lesen
        samples = np.zeros(0, np.float32)
        try:
            with wave.open(str(WAV), "rb") as wf:
                rate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception:
            samples = self._read_samples()
        text = self.engine.decode(samples)
        if not text:
            GLib.idle_add(self.overlay.hide)
            self._notify("⚠️  Nichts erkannt.")
            return
        out = text + (" " if TRAILING_SPACE else "")
        # 1) Zwischenablage (clipboard + primary) — exakter Unicode-Text
        try:
            subprocess.run(["wl-copy"], input=out.encode(), check=False)
            subprocess.run(["wl-copy", "--primary"], input=out.encode(), check=False)
        except FileNotFoundError:
            pass
        # 2) Overlay ausblenden (Fokus zurück ins Zielfeld), dann einfügen
        GLib.idle_add(self.overlay.hide)
        time.sleep(0.2)
        self._paste()
        print(f"[fvld] -> {text}", flush=True)

    def _ensure_ydotoold(self) -> bool:
        """ydotoold-Daemon sicherstellen (öffnet /dev/uinput, kein Root nötig,
        da wir in Gruppe 'input' sind)."""
        if os.path.exists(YDOTOOL_SOCKET):
            return True
        try:
            subprocess.Popen(
                ["ydotoold", "--socket-path", YDOTOOL_SOCKET, "--socket-perm", "0600"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False
        for _ in range(60):  # bis ~3s auf Socket warten
            if os.path.exists(YDOTOOL_SOCKET):
                time.sleep(0.2)  # ydotoold kurz initialisieren lassen
                return True
            time.sleep(0.05)
        return False

    def _paste(self) -> None:
        """Einfüge-Kürzel injizieren (z.B. ctrl+v). Layout-unabhängig, weil nur
        Modifier + V als rohe Keycodes gesendet werden; der Text kommt exakt aus
        der Zwischenablage."""
        if not self._ensure_ydotoold():
            self._notify("ydotool(d) fehlt — Text nur in Zwischenablage.")
            return
        try:
            codes = [_KEYCODES[k] for k in PASTE_KEY.lower().split("+")]
        except KeyError:
            self._notify(f"Unbekanntes Paste-Kürzel: {PASTE_KEY}")
            return
        seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        env = {**os.environ, "YDOTOOL_SOCKET": YDOTOOL_SOCKET}
        try:
            subprocess.run(["ydotool", "key", *seq], env=env, check=False)
        except FileNotFoundError:
            self._notify("ydotool fehlt — Text nur in Zwischenablage.")

    @staticmethod
    def _notify(msg: str) -> None:
        subprocess.run(["notify-send", "-a", "FluidVoice Lite", "FluidVoice Lite", msg],
                       check=False)
        print(f"[fvld] {msg}", flush=True)

    def run(self) -> int:
        return self.app.run(None)


if __name__ == "__main__":
    raise SystemExit(Daemon().run())
