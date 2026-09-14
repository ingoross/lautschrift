"""Native Windows dictation. Start with start-windows.cmd; no admin needed."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading
import time
import winsound

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QAbstractNativeEventFilter, QTimer, Qt, QLockFile
from PySide6.QtGui import QActionGroup, QColor, QCursor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QMessageBox, QProgressBar, QSystemTrayIcon, QVBoxLayout, QWidget

from stt import Engine
from windows_native import key_down, modifiers_down, paste, user32

LOG = logging.getLogger("lautschrift")


def play_cue(kind):
    """Play a short WAV without blocking the UI or relying on a system sound theme."""
    path = os.environ.get(f"LAUT_SOUND_{kind.upper()}",
                          str(Path(__file__).resolve().parent / "sounds" / f"{kind}.wav"))
    if not path:
        return
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except (RuntimeError, OSError):
        LOG.warning("Could not play %s sound: %s", kind, path, exc_info=True)


class Recorder:
    def __init__(self, device=None):
        self.lock = threading.Lock()
        self.chunks = []
        self.frames = 0
        self.problem = ""
        self.peak = 0.0
        # Some Windows drivers enumerate an MME default which cannot be opened.
        # Try each host API's default input; an explicit device is never replaced.
        candidates = [device]
        if device is None:
            candidates += list(dict.fromkeys(api["default_input_device"] for api in sd.query_hostapis()
                                            if api["default_input_device"] >= 0))
        errors = []
        for candidate in candidates:
            try:
                info = sd.query_devices(candidate, "input")
                self.rate = int(info["default_samplerate"])
                self.name = info["name"]
                self.limit = self.rate * 120
                self.stream = sd.InputStream(device=candidate, channels=1, samplerate=self.rate,
                                             dtype="float32", callback=self.callback)
                LOG.info("Audio device: %s, sample rate: %s", info["name"], self.rate)
                break
            except (sd.PortAudioError, ValueError) as exc:
                errors.append(str(exc))
        else:
            raise RuntimeError("Kein Mikrofonzugriff möglich. Windows-Mikrofonfreigabe und Audiogerät prüfen. " + "; ".join(errors))

    def callback(self, data, frames, timing, status):
        with self.lock:
            if status:
                self.problem = f"Audioaufnahme unterbrochen: {status}"
            if self.frames >= self.limit:
                return
            chunk = data[:self.limit - self.frames, 0].copy()
            self.peak = float(np.max(np.abs(chunk))) if chunk.size else 0.0
            self.chunks.append(chunk)
            self.frames += len(chunk)

    def start(self):
        try:
            self.stream.start()
        except Exception:
            self.stream.close()
            raise

    def snapshot(self):
        with self.lock:
            return np.concatenate(self.chunks) if self.chunks else np.empty(0, np.float32)

    def close(self):
        try:
            self.stream.stop()
        finally:
            self.stream.close()


class Overlay(QWidget):
    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setWindowTitle("Lautschrift")
        self.setFixedWidth(560)
        self.setStyleSheet("QWidget {background: #171b24; color: #edf2fa; font-family: 'Segoe UI'; font-size: 16px;} QLabel {background: transparent;}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        self.heading = QLabel()
        self.heading.setStyleSheet("color: #86adff; font-weight: 600;")
        self.text = QLabel()
        self.text.setTextFormat(Qt.PlainText)
        self.text.setWordWrap(True)
        self.level = QProgressBar()
        self.level.setRange(0, 1000)
        self.level.setTextVisible(False)
        self.level.setFixedHeight(4)
        self.level.setAccessibleName("Mikrofonpegel")
        self.level.setStyleSheet("QProgressBar {border: none; border-radius: 2px; background: #232c38;} QProgressBar::chunk {background: #48665f; border-radius: 2px;}")
        self.reset_level()
        self.hint = QLabel("Strg+Leertaste: fertig  ·  Esc: verwerfen")
        self.hint.setStyleSheet("font-size: 12px; color: #99a5bb;")
        for widget in (self.heading, self.text, self.level, self.hint):
            layout.addWidget(widget)

    def reset_level(self):
        self._level_value = 0.0
        self._level_time = time.monotonic()
        self.level.setValue(0)

    def set_level(self, peak):
        # Smooth attack and slower release keep individual audio blocks from flashing.
        now = time.monotonic()
        elapsed = min(now - self._level_time, 0.1)
        self._level_time = now
        target = float(np.clip((20 * np.log10(max(peak, 1e-8)) + 60) / 60, 0, 1))
        smoothing = 0.18 if target > self._level_value else 0.55
        self._level_value += (target - self._level_value) * (1 - np.exp(-elapsed / smoothing))
        self.level.setValue(round(self._level_value * 1000))

    def display(self, heading, text=None):
        self.heading.setText(heading)
        if text is not None:
            self.text.setText(text[-650:] or "…")
        self.adjustSize()
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(area.center().x() - self.width() // 2, area.center().y() - self.height() // 2)
        self.show()


def make_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor("#4169e1"))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 16, 16)
    painter.setBrush(QColor("white"))
    for x, h in ((16, 16), (26, 34), (36, 24), (46, 12)):
        painter.drawRoundedRect(x, 32 - h // 2, 5, h, 2, 2)
    painter.end()
    return QIcon(pixmap)


class Hotkeys(QAbstractNativeEventFilter):
    def __init__(self, app, toggle):
        super().__init__()
        self.app, self.toggle = app, toggle
        self.ids = []
        # Ctrl+Space, Copilot (Win+Shift+F23), plain F23.
        for ident, modifiers, key in ((1, 2, 0x20), (2, 12, 0x86), (3, 0, 0x86)):
            if user32.RegisterHotKey(None, ident, modifiers | 0x4000, key):
                self.ids.append(ident)
        app.installNativeEventFilter(self)

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam in self.ids:
                self.toggle()
                return True, 0
        return False, 0

    def close(self):
        self.app.removeNativeEventFilter(self)
        for ident in self.ids:
            user32.UnregisterHotKey(None, ident)
        self.ids.clear()


class Dictation:
    def __init__(self, app):
        self.app = app
        self.state = "loading"
        self.session = 0
        self.recorder = None
        self.engine = None
        self.live_pending = False
        self.last_decode = 0
        self.target = None
        self.events = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parakeet")
        self.interval = max(0.2, float(os.environ.get("LAUT_DECODE_INTERVAL", "0.8")))
        self.paste_key = os.environ.get("LAUT_PASTE_KEY", "ctrl+v")
        if self.paste_key not in ("ctrl+v", "ctrl+shift+v", "shift+insert"):
            raise ValueError("LAUT_PASTE_KEY: ctrl+v, ctrl+shift+v oder shift+insert verwenden.")
        device = os.environ.get("LAUT_INPUT_DEVICE")
        self.device = int(device) if device and device.isdigit() else device
        self.overlay = Overlay()
        self.tray = QSystemTrayIcon(make_icon(), app)
        self.menu = QMenu()
        self.status_action = self.menu.addAction("Sprachmodell wird geladen …")
        self.status_action.setEnabled(False)
        self.menu.addAction("Aufnahme starten / stoppen", self.toggle)
        self.menu.addAction("Aufnahme verwerfen", self.cancel)
        paste_menu = self.menu.addMenu("Einfügen mit")
        group = QActionGroup(paste_menu)
        for shortcut in ("ctrl+v", "ctrl+shift+v", "shift+insert"):
            action = paste_menu.addAction(shortcut)
            action.setCheckable(True)
            action.setChecked(shortcut == self.paste_key)
            group.addAction(action)
            action.triggered.connect(lambda checked, key=shortcut: setattr(self, "paste_key", key))
        self.menu.addSeparator()
        self.menu.addAction("Beenden", app.quit)
        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip("Lautschrift – Sprachmodell wird geladen")
        self.tray.show()
        self.hotkeys = Hotkeys(app, self.toggle)
        if 1 not in self.hotkeys.ids:
            self.notify("Strg+Leertaste ist bereits belegt. Aufnahme über das Taskleistensymbol starten.")
        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(25)
        self.submit("loaded", 0, Engine)
        app.aboutToQuit.connect(self.close)

    def notify(self, message):
        LOG.info(message)
        self.tray.showMessage("Lautschrift", message, QSystemTrayIcon.Information, 6000)

    def set_state(self, state):
        self.state = state
        label = {"idle": "Bereit · Strg+Leertaste", "recording": "Aufnahme läuft …",
                 "processing": "Text wird erkannt …", "error": "Fehler – bitte neu starten"}.get(state, state)
        self.status_action.setText(label)
        self.tray.setToolTip("Lautschrift – " + label)

    def submit(self, kind, session, function, *args):
        def work():
            try:
                result = function(*args)
                self.events.put((kind, session, result, None))
            except Exception as exc:
                LOG.exception("%s failed", kind)
                self.events.put((kind, session, None, str(exc)))
        self.pool.submit(work)

    def toggle(self):
        if self.state == "recording":
            self.stop()
        elif self.state == "idle":
            self.start()

    def start(self):
        recorder = None
        try:
            recorder = Recorder(self.device)
            recorder.start()
        except Exception as exc:
            self.notify(f"Mikrofon konnte nicht geöffnet werden: {exc}")
            return
        self.session += 1
        self.target = user32.GetForegroundWindow()
        self.recorder = recorder
        self.overlay.reset_level()
        self.last_decode = time.monotonic()
        self.set_state("recording")
        self.overlay.display("● Aufnahme läuft", "Jetzt sprechen …")
        play_cue("start")

    def stop(self):
        recorder, self.recorder = self.recorder, None
        try:
            recorder.close()
            if recorder.problem:
                raise RuntimeError(recorder.problem)
            samples = recorder.snapshot()
            self.audio_peak = float(np.max(np.abs(samples))) if samples.size else 0.0
            LOG.info("Recording completed: frames=%s rate=%s peak=%.6f", samples.size, recorder.rate, self.audio_peak)
        except Exception as exc:
            self.cancel()
            self.notify(str(exc))
            return
        self.set_state("processing")
        play_cue("stop")
        self.overlay.display("Text wird erkannt …")
        self.submit("final", self.session, self.engine.decode, samples, recorder.rate)

    def cancel(self):
        if self.state not in ("recording", "processing"):
            return
        self.session += 1
        if self.recorder:
            try:
                self.recorder.close()
            except Exception:
                LOG.exception("Recorder shutdown")
            self.recorder = None
        self.overlay.hide()
        self.set_state("idle")

    def tick(self):
        # Observe Escape without reserving it or swallowing it in other apps.
        if key_down(0x1B):
            self.cancel()
        while not self.events.empty():
            kind, session, result, error = self.events.get_nowait()
            if kind == "loaded":
                if error:
                    self.set_state("error")
                    self.notify(error)
                else:
                    self.engine = result
                    self.set_state("idle")
                    self.notify("Bereit. Strg+Leertaste startet und beendet das Diktat. Esc verwirft es.")
                continue
            if kind == "live":
                self.live_pending = False
            if session != self.session:
                continue
            if error:
                self.cancel()
                self.notify(f"Spracherkennung fehlgeschlagen: {error}")
            elif kind == "live" and self.state == "recording":
                self.overlay.display("● Aufnahme läuft", result)
            elif kind == "final" and self.state == "processing":
                self.overlay.hide()
                QTimer.singleShot(200, lambda sid=session, text=result: self.deliver(sid, text))
        if self.state == "recording":
            self.overlay.set_level(self.recorder.peak)
            if self.recorder.problem or not self.recorder.stream.active:
                self.cancel()
                self.notify("Mikrofonaufnahme unterbrochen. Bitte Audiogerät prüfen und erneut starten.")
            elif self.recorder.frames >= self.recorder.limit:
                self.stop()
            elif not self.live_pending and time.monotonic() - self.last_decode >= self.interval:
                self.last_decode = time.monotonic()
                self.live_pending = True
                self.submit("live", self.session, self.engine.decode, self.recorder.snapshot(), self.recorder.rate)

    def deliver(self, session, text, attempts=0):
        if session != self.session or self.state != "processing":
            return
        if key_down(0x1B):
            self.cancel()
            return
        if modifiers_down() and attempts < 40:
            QTimer.singleShot(50, lambda: self.deliver(session, text, attempts + 1))
            return
        self.set_state("idle")
        if not text:
            if getattr(self, "audio_peak", 1.0) < 0.001:
                self.notify("Mikrofonsignal sehr schwach oder stumm. Bitte Mikrofon-Stummtaste, Abstand und Windows-Eingangspegel prüfen.")
            else:
                self.notify("Keine Sprache erkannt.")
            return
        out = text + (" " if os.environ.get("LAUT_TRAILING_SPACE", "1") == "1" else "")
        self.app.clipboard().setText(out)
        if self.app.clipboard().text() != out:
            self.notify("Zwischenablage ist belegt; Text konnte nicht kopiert werden.")
            return
        if modifiers_down() or user32.GetForegroundWindow() != self.target:
            self.notify("Text kopiert. Fensterwechsel oder gehaltene Taste: bitte manuell einfügen.")
            return
        try:
            paste(self.paste_key)
        except OSError as exc:
            self.notify(str(exc))

    def close(self):
        self.cancel()
        self.timer.stop()
        self.hotkeys.close()
        self.tray.hide()
        self.pool.shutdown(wait=False, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--check", action="store_true", help="Load the model and check default audio settings without recording")
    args = parser.parse_args()
    if args.list_devices:
        print(sd.query_devices())
        return 0
    if args.check:
        engine = Engine()
        print("Sprachmodell erfolgreich geladen.")
        device = sd.query_devices(kind="input")
        sd.check_input_settings(channels=1, samplerate=device["default_samplerate"], dtype="float32")
        print(f"Mikrofon: {device['name']} ({device['default_samplerate']:.0f} Hz)")
        print("Windows-Abhängigkeiten und Modell OK.")
        return 0
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Lautschrift")
    runtime = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Lautschrift"
    runtime.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(runtime / "lautschrift.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
    lock = QLockFile(str(runtime / "lautschrift.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "Lautschrift", "Lautschrift läuft bereits. Siehe Taskleistensymbol.")
        return 0
    try:
        controller = Dictation(app)
        return app.exec()
    except Exception as exc:
        LOG.exception("Startup failed")
        QMessageBox.critical(None, "Lautschrift", str(exc))
        return 1
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
