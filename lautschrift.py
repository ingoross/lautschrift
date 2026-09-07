#!/usr/bin/env python3
"""Lautschrift daemon — system-wide local dictation for Linux/Wayland.

- Hotkey: Copilot key by default (sends Meta+Shift+F23; we trigger on
  F23/code 193). Press once to start recording, press again to stop.
  Escape cancels recording or pending transcription without pasting.
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

# Select before importing GTK: PyGObject can initialize the display on import.
# GNOME Wayland has no client API for keep-above. Use its XWayland
# bridge for the overlay; clipboard and input still target the Wayland session.
os.environ["GDK_BACKEND"] = "x11"

import numpy as np
import gi

gi.require_version("Gtk", "3.0")
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
        self.win.get_style_context().add_class("laut-win")
        self.win.set_default_size(560, -1)
        self.win.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.win.set_resizable(False)
        self.win.set_title(APP_NAME)
        self.win.set_keep_above(True)
        self.win.set_accept_focus(False)
        self.win.set_focus_on_map(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_app_paintable(True)
        visual = self.win.get_screen().get_rgba_visual()
        if visual:
            self.win.set_visual(visual)

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.get_style_context().add_class("laut-box")
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        self.title = Gtk.Label(label="●  Recording …")
        self.title.get_style_context().add_class("laut-title")
        self.title.get_style_context().add_class("laut-rec")
        self.title.set_xalign(0)
        header.pack_start(self.spinner, False, False, 0)
        header.pack_start(self.title, True, True, 0)
        self.text = Gtk.Label(label="")
        self.text.get_style_context().add_class("laut-text")
        self.text.set_xalign(0)
        self.text.set_line_wrap(True)
        self.text.set_max_width_chars(46)
        box.pack_start(header, False, False, 0)
        box.pack_start(self.text, True, True, 0)
        self.win.add(box)
        box.show_all()
        self.spinner.hide()

    def show_recording(self) -> bool:
        self.text.set_text("")
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.title.set_text("●  Recording …")
        self.title.get_style_context().remove_class("laut-proc")
        self.title.get_style_context().add_class("laut-rec")
        self.win.show()
        self.win.set_keep_above(True)
        return False

    def show_processing(self) -> bool:
        self.spinner.set_visible(True)
        self.spinner.start()
        self.title.set_text("Transcribing …")
        self.title.get_style_context().remove_class("laut-rec")
        self.title.get_style_context().add_class("laut-proc")
        self.win.show()
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
        self.processing = False
        self.session = 0
        self.wav = WAV
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
                keys = d.capabilities().get(ecodes.EV_KEY, [])
                # Escape may live on a different device than the Copilot key.
                if TRIGGER_CODE in keys or ecodes.KEY_ESC in keys:
                    devs.append(d)
                else:
                    d.close()
            except OSError:
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
                    if ev.type != ecodes.EV_KEY or ev.value != 1:
                        continue
                    if ev.code == ecodes.KEY_ESC:
                        GLib.idle_add(self.cancel)
                    elif ev.code == TRIGGER_CODE:
                        now = time.monotonic()
                        if now - last < 0.3:  # debounce
                            continue
                        last = now
                        GLib.idle_add(self.toggle)

    # ---- Toggle ----
    def toggle(self) -> bool:
        if self.processing:
            return False
        if not self.recording:
            self._start()
        else:
            self._stop()
        return False  # idle_add: do not repeat

    def _start(self) -> None:
        try:
            cmd = record_command(str(RUNTIME / f"rec-{self.session + 1}.wav"))
        except FileNotFoundError as e:
            self._notify(f"⚠️  {e}")
            return
        self.session += 1
        self.wav = RUNTIME / f"rec-{self.session}.wav"
        self.recording = True
        self.stop_flag = threading.Event()
        play_sound(SOUND_START)
        self.wav.unlink(missing_ok=True)
        self.rec_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.overlay.show_recording()
        self.decode_thread = threading.Thread(target=self._live_decode,
                                              args=(self.session, self.stop_flag, self.wav),
                                              daemon=True)
        self.decode_thread.start()

    def _stop(self) -> None:
        self.recording = False
        self.processing = True
        self.stop_flag.set()
        play_sound(SOUND_STOP)
        # Keep the overlay visible and switch to "Transcribing …" (spinner)
        self.overlay.show_processing()
        self._stop_recorder()
        # Read before another session can replace or remove its recording.
        try:
            with wave.open(str(self.wav), "rb") as wf:
                frames = wf.readframes(wf.getnframes())
            samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        except (OSError, EOFError, wave.Error):
            samples = self._read_samples(self.wav)
        self.wav.unlink(missing_ok=True)
        threading.Thread(target=self._finalize, args=(self.session, samples),
                         daemon=True).start()

    def _stop_recorder(self) -> None:
        if self.rec_proc:
            try:
                self.rec_proc.send_signal(signal.SIGINT)
                self.rec_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.rec_proc.kill()
                self.rec_proc.wait()
            finally:
                self.rec_proc = None

    def cancel(self) -> bool:
        if not (self.recording or self.processing):
            return False
        self.session += 1  # invalidate queued live updates and final results
        self.recording = False
        self.processing = False
        self.stop_flag.set()
        self.overlay.hide()
        self._stop_recorder()
        self.wav.unlink(missing_ok=True)
        play_sound(SOUND_STOP)
        print("[lautschrift] cancelled.", flush=True)
        return False

    # ---- Live decoding ----
    @staticmethod
    def _read_samples(wav: Path) -> np.ndarray:
        try:
            raw = wav.read_bytes()
        except FileNotFoundError:
            return np.zeros(0, np.float32)
        if len(raw) <= 44:
            return np.zeros(0, np.float32)
        pcm = raw[44:]
        if len(pcm) % 2:
            pcm = pcm[:-1]
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    def _live_decode(self, session: int, stop_flag: threading.Event,
                     wav: Path) -> None:
        while not stop_flag.wait(DECODE_INTERVAL):
            samples = self._read_samples(wav)
            if samples.size < SAMPLE_RATE // 4:
                continue
            text = self.engine.decode(samples)
            GLib.idle_add(self._update_text, session, text)

    def _update_text(self, session: int, text: str) -> bool:
        if session == self.session and self.recording:
            self.overlay.set_text(text)
        return False

    def _finalize(self, session: int, samples: np.ndarray) -> None:
        text = self.engine.decode(samples)
        GLib.idle_add(self._finish, session, text)

    def _finish(self, session: int, text: str) -> bool:
        if session != self.session or not self.processing:
            return False
        self.overlay.hide()
        if not text:
            self.processing = False
            self._notify("⚠️  Nothing recognized.")
            return False
        # Allow the compositor to hide the overlay, and Escape to cancel
        # even after decoding, before touching the clipboard or pasting.
        GLib.timeout_add(200, self._deliver, session, text)
        return False

    def _deliver(self, session: int, text: str) -> bool:
        if session != self.session or not self.processing:
            return False
        self.processing = False
        out = text + (" " if TRAILING_SPACE else "")
        try:
            subprocess.run(["wl-copy"], input=out.encode(), check=False)
            subprocess.run(["wl-copy", "--primary"], input=out.encode(), check=False)
        except FileNotFoundError:
            pass
        self._paste()
        print(f"[lautschrift] -> {text}", flush=True)
        return False

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
