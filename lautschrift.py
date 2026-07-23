#!/usr/bin/env python3
"""Lautschrift daemon — system-wide local dictation for Linux/Wayland.

- Hotkey: Copilot key by default (sends Meta+Shift+F23; we trigger on
  F23/code 193). Press once to start recording, press again to stop.
- STT: NVIDIA Parakeet TDT 0.6B v3 (int8, via sherpa-onnx) — fully offline,
  25 European languages, automatic language detection.
- Live overlay: a small GTK window shows the text while you speak (the
  audio buffer is re-decoded every ~0.8 s — the offline Parakeet model
  cannot truly stream, but re-decoding feels live).
- On stop: the final text goes to the clipboard (wl-copy) AND is pasted
  into the focused field (ydotool).

Works across compositors because the keyboard is read directly via evdev
(user must be in the 'input' group). Start:  .venv/bin/python lautschrift.py
"""
from __future__ import annotations

import os
import selectors
import shutil
import signal
import socket
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

APP_NAME = "Lautschrift"

# --- Configuration ----------------------------------------------------------
HERE = Path(__file__).resolve().parent
MODEL_DIR = Path(os.environ.get("LAUT_MODEL_DIR", HERE / "models" / "parakeet-v3"))
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "lautschrift"
RUNTIME.mkdir(parents=True, exist_ok=True)
WAV = RUNTIME / "rec.wav"

TRIGGER_CODE = int(os.environ.get("LAUT_TRIGGER_CODE", ecodes.KEY_F23))  # 193
NUM_THREADS = int(os.environ.get("LAUT_THREADS", "6"))
DECODE_INTERVAL = float(os.environ.get("LAUT_DECODE_INTERVAL", "0.8"))
TRAILING_SPACE = os.environ.get("LAUT_TRAILING_SPACE", "1") == "1"
SAMPLE_RATE = 16000

# ydotool injects at the kernel level (/dev/uinput) — works on GNOME
# Wayland, where wtype (virtual-keyboard protocol) fails.
YDOTOOL_SOCKET = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/tmp"), ".ydotool_socket"
)
# Text output: clipboard + paste shortcut (layout-independent, exact
# Unicode). ydotool "type" breaks on non-US layouts (y/z swapped, umlauts
# dropped), hence paste instead of typing.
#
# Which shortcut inserts the clipboard differs per app, and terminal TUIs
# make it worse: they grab ctrl+v/ctrl+shift+v before the terminal can act.
#   ctrl+v        -> GUI apps; Claude Code pastes, but Codex maps it to
#                    "paste image" and errors on text-only clipboard.
#   ctrl+shift+v  -> most terminals' own paste; some GUI apps rebind it.
#   paste         -> the dedicated Paste key (KEY_PASTE). No TUI grabs it, so
#                    the terminal (e.g. Ghostty: `keybind = paste=...`) and
#                    GTK/Qt fields both treat it as "insert". Most robust.
PASTE_KEY = os.environ.get("LAUT_PASTE_KEY", "paste")
_KEYCODES = {  # Linux input-event codes (layout-neutral)
    "ctrl": 29, "shift": 42, "alt": 56, "super": 125,
    "v": 47, "insert": 110, "paste": 135,
}

# Start/stop sounds (freedesktop sound theme). Override via LAUT_SOUND_*.
_FD = "/usr/share/sounds/freedesktop/stereo"
SOUND_START = os.environ.get("LAUT_SOUND_START", f"{_FD}/message-new-instant.oga")
SOUND_STOP = os.environ.get("LAUT_SOUND_STOP", f"{_FD}/complete.oga")


def play_sound(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    for player in ("pw-play", "paplay"):
        if shutil.which(player):
            subprocess.Popen([player, path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return


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


# --- STT --------------------------------------------------------------------
class Engine:
    def __init__(self) -> None:
        import sherpa_onnx

        def pick(*names: str) -> str:
            for n in names:
                p = MODEL_DIR / n
                if p.exists():
                    return str(p)
            raise FileNotFoundError(
                f"{names} not found in {MODEL_DIR} — run scripts/download-model.sh"
            )

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


# --- Overlay ----------------------------------------------------------------
CSS = b"""
/* Transparent window -> the desktop shows through behind the rounded corners */
window.laut-win { background: transparent; }
.laut-box { background: rgba(20,20,24,0.94); border-radius: 18px;
            padding: 18px 24px; margin: 14px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.55); }
.laut-title { color: #7aa2ff; font-weight: 700; font-size: 13px; }
.laut-text { color: #f0f0f4; font-size: 15px; }
.laut-rec { color: #ff5f6d; font-weight: 700; }
.laut-proc { color: #7aa2ff; font-weight: 700; }
"""


class Overlay:
    def __init__(self, app: Gtk.Application) -> None:
        self.win = Gtk.ApplicationWindow(application=app)
        self.win.set_decorated(False)
        self.win.add_css_class("laut-win")
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
        box.add_css_class("laut-box")
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        self.title = Gtk.Label(label="●  Recording …")
        self.title.add_css_class("laut-title")
        self.title.add_css_class("laut-rec")
        self.title.set_xalign(0)
        header.append(self.spinner)
        header.append(self.title)
        self.text = Gtk.Label(label="")
        self.text.add_css_class("laut-text")
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
        self.title.set_text("●  Recording …")
        self.title.remove_css_class("laut-proc")
        self.title.add_css_class("laut-rec")
        self.win.present()
        return False

    def show_processing(self) -> bool:
        self.spinner.set_visible(True)
        self.spinner.start()
        self.title.set_text("Transcribing …")
        self.title.remove_css_class("laut-rec")
        self.title.add_css_class("laut-proc")
        self.win.present()
        return False

    def hide(self) -> bool:
        self.spinner.stop()
        self.win.set_visible(False)
        return False

    def set_text(self, s: str) -> bool:
        self.text.set_text(s or "…")
        return False


# --- Daemon -----------------------------------------------------------------
class Daemon:
    def __init__(self) -> None:
        self.engine = Engine()
        self.recording = False
        self.rec_proc: subprocess.Popen | None = None
        self.decode_thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.app = Gtk.Application(application_id="de.unfuture.lautschrift")
        self.app.connect("activate", self._on_activate)

    # ---- GTK lifecycle ----
    def _on_activate(self, app: Gtk.Application) -> None:
        self.overlay = Overlay(app)
        app.hold()  # stay alive without a visible window
        threading.Thread(target=self._key_loop, daemon=True).start()
        print("[lautschrift] ready. Press the hotkey to dictate.", flush=True)

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
        # Fallback: all keyboards
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
            print("[lautschrift] no keyboard found (is the user in the "
                  "'input' group?)", flush=True)
            return
        print(f"[lautschrift] listening on {len(devs)} keyboard(s) for "
              f"keycode {TRIGGER_CODE}", flush=True)

        sel = selectors.DefaultSelector()
        for d in devs:
            sel.register(d, selectors.EVENT_READ)
        last = 0.0
        while True:
            for key, _ in sel.select():
                for ev in key.fileobj.read():
                    if ev.type == ecodes.EV_KEY and ev.code == TRIGGER_CODE and ev.value == 1:
                        now = time.monotonic()
                        if now - last < 0.3:  # debounce
                            continue
                        last = now
                        GLib.idle_add(self.toggle)

    # ---- Toggle ----
    def toggle(self) -> bool:
        if not self.recording:
            self._start()
        else:
            self._stop()
        return False  # idle_add: do not repeat

    def _start(self) -> None:
        try:
            cmd = record_command(str(WAV))
        except FileNotFoundError as e:
            self._notify(f"⚠️  {e}")
            return
        self.recording = True
        self.stop_flag.clear()
        play_sound(SOUND_START)
        WAV.unlink(missing_ok=True)
        self.rec_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.overlay.show_recording()
        self.decode_thread = threading.Thread(target=self._live_decode, daemon=True)
        self.decode_thread.start()

    def _stop(self) -> None:
        self.recording = False
        self.stop_flag.set()
        play_sound(SOUND_STOP)
        # Keep the overlay visible and switch to "Transcribing …" (spinner)
        self.overlay.show_processing()
        if self.rec_proc:
            try:
                self.rec_proc.send_signal(signal.SIGINT)
                self.rec_proc.wait(timeout=3)
            except Exception:
                pass
            self.rec_proc = None
        # Final decode in its own thread (does not block GTK -> spinner runs)
        threading.Thread(target=self._finalize, daemon=True).start()

    # ---- Live decoding ----
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
            if samples.size < SAMPLE_RATE // 4:  # ignore <0.25 s
                continue
            text = self.engine.decode(samples)
            GLib.idle_add(self.overlay.set_text, text)

    def _finalize(self) -> None:
        # Read the cleanly finalized file
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
            self._notify("⚠️  Nothing recognized.")
            return
        out = text + (" " if TRAILING_SPACE else "")
        # 1) Clipboard (clipboard + primary) — exact Unicode text
        try:
            subprocess.run(["wl-copy"], input=out.encode(), check=False)
            subprocess.run(["wl-copy", "--primary"], input=out.encode(), check=False)
        except FileNotFoundError:
            pass
        # 2) Hide the overlay (focus returns to the target field), then paste
        GLib.idle_add(self.overlay.hide)
        time.sleep(0.2)
        self._paste()
        print(f"[lautschrift] -> {text}", flush=True)

    @staticmethod
    def _ydotoold_alive() -> bool:
        """A leftover socket *file* does not mean the daemon lives — ydotoold
        can die and leave it behind. Probe it: connecting to the (SOCK_DGRAM)
        socket succeeds while ydotoold is bound and fails with ECONNREFUSED
        once it is gone, which is exactly how `ydotool` itself detects it."""
        if not os.path.exists(YDOTOOL_SOCKET):
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.settimeout(0.3)
                s.connect(YDOTOOL_SOCKET)
            return True
        except OSError:
            return False

    def _ensure_ydotoold(self) -> bool:
        """Make sure the ydotoold daemon runs (opens /dev/uinput; no root
        needed if the user may write to /dev/uinput, see install.sh)."""
        if self._ydotoold_alive():
            return True
        # Stale socket from a dead daemon blocks re-binding — remove it first.
        try:
            os.unlink(YDOTOOL_SOCKET)
        except FileNotFoundError:
            pass
        try:
            subprocess.Popen(
                ["ydotoold", "--socket-path", YDOTOOL_SOCKET, "--socket-perm", "0600"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False
        for _ in range(60):  # wait up to ~3 s for a working socket
            if self._ydotoold_alive():
                return True
            time.sleep(0.05)
        return False

    def _paste(self) -> None:
        """Inject the paste shortcut (e.g. ctrl+v). Layout-independent since
        only modifiers + V are sent as raw keycodes; the text itself comes
        verbatim from the clipboard."""
        if not self._ensure_ydotoold():
            self._notify("ydotool(d) missing — text is in the clipboard only.")
            return
        try:
            codes = [_KEYCODES[k] for k in PASTE_KEY.lower().split("+")]
        except KeyError:
            self._notify(f"Unknown paste shortcut: {PASTE_KEY}")
            return
        seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        env = {**os.environ, "YDOTOOL_SOCKET": YDOTOOL_SOCKET}
        try:
            r = subprocess.run(["ydotool", "key", *seq], env=env,
                               stderr=subprocess.PIPE)
        except FileNotFoundError:
            self._notify("ydotool missing — text is in the clipboard only.")
            return
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace").strip()
            self._notify(f"paste failed ({err or 'ydotool error'}) — "
                         "text is in the clipboard only.")

    @staticmethod
    def _notify(msg: str) -> None:
        subprocess.run(["notify-send", "-a", APP_NAME, APP_NAME, msg],
                       check=False)
        print(f"[lautschrift] {msg}", flush=True)

    def run(self) -> int:
        return self.app.run(None)


if __name__ == "__main__":
    raise SystemExit(Daemon().run())
