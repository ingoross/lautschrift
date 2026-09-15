"""macOS hotkey matching, cancellation and delivery checks (no microphone, no permissions)."""
import queue
import sys
import unittest
from unittest.mock import Mock, patch

if sys.platform != "darwin":
    raise unittest.SkipTest("macOS native implementation")

import numpy as np
import Quartz
from PySide6.QtWidgets import QApplication

import lautschrift_mac as laut
from mac_native import KEY_SPACE, is_trigger

ALT, CMD, SHIFT, CTRL, CAPS = (Quartz.kCGEventFlagMaskAlternate, Quartz.kCGEventFlagMaskCommand,
                               Quartz.kCGEventFlagMaskShift, Quartz.kCGEventFlagMaskControl,
                               Quartz.kCGEventFlagMaskAlphaShift)


class TriggerTests(unittest.TestCase):
    def test_option_space_matches(self):
        self.assertTrue(is_trigger(KEY_SPACE, ALT))
        self.assertTrue(is_trigger(KEY_SPACE, ALT | CAPS | Quartz.kCGEventFlagMaskNonCoalesced))

    def test_other_modifiers_do_not_match(self):
        self.assertFalse(is_trigger(KEY_SPACE, 0))
        self.assertFalse(is_trigger(KEY_SPACE, CMD))
        self.assertFalse(is_trigger(KEY_SPACE, ALT | CMD))
        self.assertFalse(is_trigger(KEY_SPACE, ALT | SHIFT))
        self.assertFalse(is_trigger(KEY_SPACE, ALT | CTRL))
        self.assertFalse(is_trigger(0, ALT))


class MacTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.d = laut.Dictation.__new__(laut.Dictation)
        d = self.d
        d.state = "recording"
        d.session = 1
        d.recorder = Mock()
        d.overlay = Mock()
        d.status_action = Mock()
        d.tray = Mock()
        d.app = Mock()
        d.target = 42
        d.paste_key = "cmd+v"
        d.notify = Mock()
        d.events = queue.Queue()
        d.live_pending = True
        d.hotkeys = None
        self.patchers = [patch.object(laut, "key_down", return_value=False),
                         patch.object(laut, "modifiers_down", return_value=False),
                         patch.object(laut, "paste"), patch.object(laut, "frontmost_pid", return_value=42)]
        self.mocks = [p.start() for p in self.patchers]
        self.addCleanup(patch.stopall)
        self.cue = patch.object(laut, "play_cue").start()

    def test_start_cue_only_after_microphone_is_ready(self):
        d = self.d
        d.device = None
        d.state = "idle"
        with patch.object(laut, "Recorder") as recorder:
            d.start()
        recorder.return_value.start.assert_called_once()
        self.cue.assert_called_once_with("start")
        self.assertEqual(d.state, "recording")
        self.assertEqual(d.target, 42)

    def test_failed_microphone_does_not_play_start_cue(self):
        self.d.device = None
        with patch.object(laut, "Recorder", side_effect=RuntimeError("no microphone")):
            self.d.start()
        self.cue.assert_not_called()

    def test_stop_cue_after_audio_is_closed_before_decode(self):
        d = self.d
        d.recorder.problem = ""
        d.recorder.rate = 16000
        d.recorder.snapshot.return_value = np.zeros(100, np.float32)
        d.engine = Mock()
        d.submit = Mock()
        order = Mock()
        order.attach_mock(d.recorder.close, "close")
        order.attach_mock(self.cue, "sound")
        order.attach_mock(d.submit, "decode")
        d.stop()
        self.assertEqual([c[0] for c in order.mock_calls], ["close", "sound", "decode"])
        self.cue.assert_called_once_with("stop")

    def test_cancel_closes_audio_and_rejects_final_result(self):
        d = self.d
        recorder = d.recorder
        d.cancel()
        recorder.close.assert_called_once()
        self.assertEqual(d.state, "idle")
        d.events.put(("final", 1, "veraltet", None))
        d.tick()
        d.overlay.hide.assert_called()
        self.mocks[2].assert_not_called()

    def test_tick_reenables_disabled_tap(self):
        d = self.d
        d.hotkeys = Mock()
        d.last_watchdog = 0.0
        d.state = "idle"
        d.tick()
        d.hotkeys.ensure_enabled.assert_called_once()
        d.tick()
        d.hotkeys.ensure_enabled.assert_called_once()

    def test_hotkey_event_toggles_on_qt_thread(self):
        d = self.d
        d.state = "idle"
        d.start = Mock()
        d.events.put(("hotkey", 0, None, None))
        d.tick()
        d.start.assert_called_once()

    def test_deliver_waits_for_modifier_release_then_pastes(self):
        d = self.d
        d.state = "processing"
        clipboard = Mock()
        clipboard.text.return_value = "Hallo Welt "
        d.app.clipboard.return_value = clipboard
        with patch.object(laut.QTimer, "singleShot") as later:
            self.mocks[1].return_value = True
            d.deliver(1, "Hallo Welt")
            later.assert_called_once()
        self.mocks[1].return_value = False
        d.deliver(1, "Hallo Welt")
        clipboard.setText.assert_called_with("Hallo Welt ")
        self.mocks[2].assert_called_once_with("cmd+v")
        self.assertEqual(d.state, "idle")

    def test_window_switch_copies_but_does_not_paste(self):
        d = self.d
        d.state = "processing"
        clipboard = Mock()
        clipboard.text.return_value = "Text "
        d.app.clipboard.return_value = clipboard
        self.mocks[3].return_value = 99
        d.deliver(1, "Text")
        self.mocks[2].assert_not_called()
        d.notify.assert_called()

    def test_escape_during_delivery_cancels(self):
        d = self.d
        d.state = "processing"
        self.mocks[0].return_value = True
        d.deliver(1, "Text")
        self.mocks[2].assert_not_called()
        self.assertEqual(d.state, "idle")


if __name__ == "__main__":
    unittest.main()
