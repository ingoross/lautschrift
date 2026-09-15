"""Small macOS boundary: global hotkey via CGEventTap, layout-neutral paste, key state."""
from __future__ import annotations

import logging
import threading

import Quartz
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSSound, NSWorkspace
from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

LOG = logging.getLogger("lautschrift")

# Virtual keycodes (physical keys, independent of the keyboard layout).
KEY_SPACE, KEY_ESCAPE, KEY_V, KEY_CMD, KEY_SHIFT = 49, 53, 9, 55, 56
MOD_MASK = (Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
            | Quartz.kCGEventFlagMaskAlternate | Quartz.kCGEventFlagMaskControl)
_TAP_DISABLED = (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput)


def accessibility_trusted(prompt=False):
    """Event taps and synthetic key presses require Accessibility (Bedienungshilfen)."""
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))


def hide_dock_icon():
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)


def frontmost_pid():
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.processIdentifier() if app is not None else None


def key_down(code):
    return bool(Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateCombinedSessionState, code))


def modifiers_down():
    flags = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateCombinedSessionState)
    return bool(flags & MOD_MASK)


def is_trigger(keycode, flags):
    """Option+Space: Option held, no Command/Control/Shift. Caps Lock, Fn and numpad bits are ignored."""
    return keycode == KEY_SPACE and (flags & MOD_MASK) == Quartz.kCGEventFlagMaskAlternate


def play_sound(path):
    """Play a short WAV asynchronously; returns the NSSound so the caller can keep it alive."""
    sound = NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
    if sound is None:
        raise OSError(f"Sound konnte nicht geladen werden: {path}")
    sound.play()
    return sound


def paste(shortcut="cmd+v"):
    """Post Cmd+V (or Cmd+Shift+V) as HID events; keycode-based, so umlaut layouts are fine."""
    modifiers = {"cmd+v": [KEY_CMD], "cmd+shift+v": [KEY_CMD, KEY_SHIFT]}[shortcut]
    flags = Quartz.kCGEventFlagMaskCommand | (Quartz.kCGEventFlagMaskShift if KEY_SHIFT in modifiers else 0)
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    events = []
    for code in modifiers:
        events.append(Quartz.CGEventCreateKeyboardEvent(source, code, True))
    down = Quartz.CGEventCreateKeyboardEvent(source, KEY_V, True)
    up = Quartz.CGEventCreateKeyboardEvent(source, KEY_V, False)
    Quartz.CGEventSetFlags(down, flags)
    Quartz.CGEventSetFlags(up, flags)
    events += [down, up]
    for code in reversed(modifiers):
        events.append(Quartz.CGEventCreateKeyboardEvent(source, code, False))
    if any(event is None for event in events):
        raise OSError("Einfügen nicht möglich: Tastaturereignis konnte nicht erzeugt werden. Text liegt in der Zwischenablage.")
    for event in events:
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


class HotkeyTap:
    """Session-wide key tap on its own run loop. The trigger combo is swallowed so
    Option+Space does not also insert a non-breaking space into the focused field."""

    def __init__(self, matcher, on_trigger):
        self.matcher, self.on_trigger = matcher, on_trigger
        self.loop = None
        self.tap = None
        self.ready = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name="hotkey-tap", daemon=True)
        self.thread.start()
        self.ready.wait(5)
        if self.error:
            raise OSError(self.error)

    def _callback(self, proxy, event_type, event, refcon):
        if event_type in _TAP_DISABLED:
            LOG.warning("Event tap was disabled by macOS (%s); re-enabling", event_type)
            Quartz.CGEventTapEnable(self.tap, True)
            return event
        if event_type == Quartz.kCGEventKeyDown:
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            if self.matcher(keycode, Quartz.CGEventGetFlags(event)):
                if not Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
                    try:
                        self.on_trigger()
                    except Exception:
                        LOG.exception("Hotkey handler failed")
                return None
        return event

    def _run(self):
        try:
            self.tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap, Quartz.kCGEventTapOptionDefault,
                Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown), self._callback, None)
            if self.tap is None:
                self.error = ("Globaler Hotkey nicht verfügbar. Bitte Python unter Systemeinstellungen → "
                              "Datenschutz & Sicherheit → Bedienungshilfen (und Eingabeüberwachung) erlauben und neu starten.")
                return
            source = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
            self.loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(self.loop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(self.tap, True)
        except Exception as exc:
            self.error = f"Hotkey-Registrierung fehlgeschlagen: {exc}"
            return
        finally:
            self.ready.set()
        Quartz.CFRunLoopRun()

    def ensure_enabled(self):
        """macOS silently disables a tap whose callback stalls (e.g. GIL held during model load
        under heavy system load). Re-enable from a periodic check so the hotkey never stays dead."""
        if self.tap is not None and not Quartz.CGEventTapIsEnabled(self.tap):
            LOG.warning("Event tap found disabled; re-enabling")
            Quartz.CGEventTapEnable(self.tap, True)

    def close(self):
        if self.tap is not None:
            Quartz.CGEventTapEnable(self.tap, False)
        if self.loop is not None:
            Quartz.CFRunLoopStop(self.loop)
        self.thread.join(2)
