"""Native macOS dictation. Start with start-mac.sh; hotkey is Option+Space."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading
import time

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QTimer, Qt, QLockFile
from PySide6.QtGui import QActionGroup, QColor, QCursor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QMessageBox, QProgressBar, QSystemTrayIcon, QVBoxLayout, QWidget

from stt import Engine
from mac_native import (KEY_ESCAPE, HotkeyTap, accessibility_trusted, frontmost_pid, hide_dock_icon,
                        is_trigger, key_down, modifiers_down, paste, play_sound)

LOG = logging.getLogger("lautschrift")
HOTKEY = "⌥ Leertaste"
_sounds = []  # keep NSSound objects alive until playback ends


def play_cue(kind):
    """Play a short, quiet WAV without blocking the UI (softer variants than Windows)."""
    path = os.environ.get(f"LAUT_SOUND_{kind.upper()}",
                          str(Path(__file__).resolve().parent / "sounds" / f"{kind}-soft.wav"))
    if not path:
        return
    try:
        _sounds.append(play_sound(path))
        del _sounds[:-2]
    except (RuntimeError, OSError):
        LOG.warning("Could not play %s sound: %s", kind, path, exc_info=True)


class Recorder:
    def __init__(self, device=None):
        self.lock = threading.Lock()
        self.chunks = []
        self.frames = 0
        self.problem = ""
        self.peak = 0.0
        try:
            info = sd.query_devices(device, "input")
            self.rate = int(info["default_samplerate"])
            self.name = info["name"]
            self.limit = self.rate * 120
            self.stream = sd.InputStream(device=device, channels=1, samplerate=self.rate,
                                         dtype="float32", callback=self.callback)
            LOG.info("Audio device: %s, sample rate: %s", info["name"], self.rate)
        except (sd.PortAudioError, ValueError) as exc:
            raise RuntimeError("Kein Mikrofonzugriff möglich. Mikrofonfreigabe unter Datenschutz & Sicherheit "
                               f"und Audiogerät prüfen. {exc}") from exc

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
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow)  # tool windows otherwise hide while another app is active
        self.setWindowTitle("Lautschrift")
        self.setFixedWidth(560)
        self.setStyleSheet("QWidget {background: #171b24; color: #edf2fa; font-size: 16px;} QLabel {background: transparent;}")
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
        self.hint = QLabel(f"{HOTKEY}: fertig  ·  Esc: verwerfen")
        self.hint.setStyleSheet("font-size: 12px; color: #99a5bb;")
        for widget in (self.heading, self.text, self.level, self.hint):
            layout.addWidget(widget)

    def reset_level(self):
        self._level_value = 0.0
        self._level_time = time.monotonic()
        self.level.setValue(0)

    def set_level(self, peak):
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


class Dictation:
    def __init__(self, app):
        self.app = app
        self.state = "loading"
        self.session = 0
        self.recorder = None
        self.engine = None
        self.hotkeys = None
        self.live_pending = False
        self.last_decode = 0
        self.target = None
        self.last_watchdog = 0.0
        self.events = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parakeet")
        self.interval = max(0.2, float(os.environ.get("LAUT_DECODE_INTERVAL", "0.8")))
        self.paste_key = os.environ.get("LAUT_PASTE_KEY", "cmd+v")
        if self.paste_key not in ("cmd+v", "cmd+shift+v"):
            raise ValueError("LAUT_PASTE_KEY: cmd+v oder cmd+shift+v verwenden.")
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
        for shortcut in ("cmd+v", "cmd+shift+v"):
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
        try:
            # The tap thread only enqueues; toggling happens on the Qt thread in tick().
            self.hotkeys = HotkeyTap(is_trigger, lambda: self.events.put(("hotkey", 0, None, None)))
        except OSError as exc:
            self.notify(str(exc) + " Aufnahme über das Menüleistensymbol starten.")
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
        label = {"idle": f"Bereit · {HOTKEY}", "recording": "Aufnahme läuft …",
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
        self.target = frontmost_pid()
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
        if key_down(KEY_ESCAPE):
            self.cancel()
        if self.hotkeys and time.monotonic() - self.last_watchdog >= 1.0:
            self.last_watchdog = time.monotonic()
            self.hotkeys.ensure_enabled()
        while not self.events.empty():
            kind, session, result, error = self.events.get_nowait()
            if kind == "hotkey":
                self.toggle()
                continue
            if kind == "loaded":
                if error:
                    self.set_state("error")
                    self.notify(error)
                else:
                    self.engine = result
                    self.set_state("idle")
                    self.notify(f"Bereit. {HOTKEY} startet und beendet das Diktat. Esc verwirft es.")
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
        if key_down(KEY_ESCAPE):
            self.cancel()
            return
        if modifiers_down() and attempts < 40:
            # Option is usually still held right after the stop hotkey; wait for release.
            QTimer.singleShot(50, lambda: self.deliver(session, text, attempts + 1))
            return
        self.set_state("idle")
        if not text:
            if getattr(self, "audio_peak", 1.0) < 0.001:
                self.notify("Mikrofonsignal sehr schwach oder stumm. Bitte Mikrofon, Abstand und Eingangspegel prüfen.")
            else:
                self.notify("Keine Sprache erkannt.")
            return
        out = text + (" " if os.environ.get("LAUT_TRAILING_SPACE", "1") == "1" else "")
        self.app.clipboard().setText(out)
        if self.app.clipboard().text() != out:
            self.notify("Zwischenablage ist belegt; Text konnte nicht kopiert werden.")
            return
        if modifiers_down() or frontmost_pid() != self.target:
            self.notify("Text kopiert. Fensterwechsel oder gehaltene Taste: bitte manuell einfügen.")
            return
        try:
            paste(self.paste_key)
            LOG.info("Delivered %d characters via %s", len(out), self.paste_key)
        except OSError as exc:
            self.notify(str(exc))

    def close(self):
        self.cancel()
        self.timer.stop()
        if self.hotkeys:
            self.hotkeys.close()
        self.tray.hide()
        self.pool.shutdown(wait=False, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--check", action="store_true", help="Load the model and check permissions and audio without recording")
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
        if accessibility_trusted(prompt=True):
            print("Bedienungshilfen-Freigabe: OK")
        else:
            print("Bedienungshilfen-Freigabe FEHLT: Systemeinstellungen → Datenschutz & Sicherheit → Bedienungshilfen → Python erlauben.")
            return 1
        print("macOS-Abhängigkeiten und Modell OK.")
        return 0
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Lautschrift")
    hide_dock_icon()
    runtime = Path.home() / "Library" / "Application Support" / "Lautschrift"
    runtime.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(runtime / "lautschrift.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
    lock = QLockFile(str(runtime / "lautschrift.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "Lautschrift", "Lautschrift läuft bereits. Siehe Menüleistensymbol.")
        return 0
    accessibility_trusted(prompt=True)  # opens the system prompt on first start
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
