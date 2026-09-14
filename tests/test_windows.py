"""Windows cancellation, delivery and native ABI regression checks (no microphone)."""
import ctypes
import queue
import unittest
import sys
from unittest.mock import Mock, patch

if sys.platform != "win32":
    raise unittest.SkipTest("Windows native implementation")

import numpy as np
from PySide6.QtWidgets import QApplication

import lautschrift_windows as laut
from windows_native import INPUT


class WindowsTests(unittest.TestCase):
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
        d.paste_key = "ctrl+v"
        d.notify = Mock()
        d.events = queue.Queue()
        d.live_pending = True
        self.patchers = [patch.object(laut, "key_down", return_value=False),
                         patch.object(laut, "modifiers_down", return_value=False),
                         patch.object(laut, "paste"), patch.object(laut, "user32")]
        self.mocks = [p.start() for p in self.patchers]
        self.mocks[-1].GetForegroundWindow.return_value = 42
        self.addCleanup(patch.stopall)
        self.cue = patch.object(laut, "play_cue").start()

    def test_start_cue_only_after_microphone_is_ready(self):
        d = self.d
        d.device = None
        with patch.object(laut, "Recorder") as recorder:
            d.start()
        recorder.return_value.start.assert_called_once()
        self.cue.assert_called_once_with("start")
        self.assertEqual(d.state, "recording")

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

    def test_native_input_structure_matches_windows_abi(self):
        self.assertEqual(ctypes.sizeof(INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_cancel_closes_audio_and_rejects_final_result(self):
        recorder = self.d.recorder
        self.d.cancel()
        self.d.deliver(1, "discard")
        recorder.close.assert_called_once()
        self.assertEqual(self.d.state, "idle")
        self.d.app.clipboard.assert_not_called()
        self.mocks[2].assert_not_called()

    def test_cancel_after_decode_before_delayed_delivery(self):
        d = self.d
        d.state = "processing"
        d.recorder = None
        d.events.put(("final", 1, "discard", None))
        with patch.object(laut.QTimer, "singleShot") as timer:
            d.tick()
            callback = timer.call_args.args[1]
        d.cancel()
        callback()
        d.app.clipboard.assert_not_called()

    def test_new_session_ignores_stale_live_and_final_results(self):
        d = self.d
        d.session = 2
        d.state = "processing"
        d.events.put(("live", 1, "stale", None))
        d.events.put(("final", 1, "stale", None))
        d.tick()
        d.overlay.display.assert_not_called()
        d.overlay.hide.assert_not_called()
        d.app.clipboard.assert_not_called()

    def test_normal_delivery_pastes_exact_unicode_only_once(self):
        d = self.d
        d.state = "processing"
        d.app.clipboard().text.return_value = "Grüße aus Köln. "
        d.deliver(1, "Grüße aus Köln.")
        d.deliver(1, "Grüße aus Köln.")
        d.app.clipboard().setText.assert_called_once_with("Grüße aus Köln. ")
        self.mocks[2].assert_called_once_with("ctrl+v")

    def test_focus_change_copies_without_pasting(self):
        d = self.d
        d.state = "processing"
        d.app.clipboard().text.return_value = "Text "
        self.mocks[-1].GetForegroundWindow.return_value = 77
        d.deliver(1, "Text")
        d.app.clipboard().setText.assert_called_once_with("Text ")
        self.mocks[2].assert_not_called()

    def test_held_modifiers_defer_clipboard_and_paste(self):
        self.d.state = "processing"
        self.mocks[1].return_value = True
        with patch.object(laut.QTimer, "singleShot") as timer:
            self.d.deliver(1, "Text")
        timer.assert_called_once()
        self.d.app.clipboard.assert_not_called()

    def test_escape_at_delivery_leaves_clipboard_untouched(self):
        self.d.state = "processing"
        self.mocks[0].return_value = True
        self.d.deliver(1, "Text")
        self.d.app.clipboard.assert_not_called()

    def test_recorder_error_discards_incomplete_audio(self):
        self.d.recorder.problem = "overflow"
        self.d.stop()
        self.assertEqual(self.d.state, "idle")
        self.d.notify.assert_called_once()
        self.d.app.clipboard.assert_not_called()

    def test_decode_error_resets_processing(self):
        self.d.state = "processing"
        self.d.events.put(("final", 1, None, "decode failed"))
        self.d.tick()
        self.assertEqual(self.d.state, "idle")
        self.d.notify.assert_called_once()

    def test_hotkey_during_processing_is_ignored(self):
        self.d.state = "processing"
        self.d.start = Mock()
        self.d.toggle()
        self.d.start.assert_not_called()

    def test_audio_buffer_is_bounded(self):
        recorder = laut.Recorder.__new__(laut.Recorder)
        import threading
        recorder.lock = threading.Lock()
        recorder.limit = 5
        recorder.frames = 0
        recorder.chunks = []
        recorder.callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
        recorder.callback(np.ones((8, 1), dtype=np.float32), 8, None, None)
        self.assertEqual(recorder.snapshot().size, 5)

    def test_default_audio_falls_back_to_working_windows_driver(self):
        with patch.object(laut.sd, "query_hostapis", return_value=[{"default_input_device": 9}]), \
             patch.object(laut.sd, "query_devices", return_value={"default_samplerate": 48000, "name": "Microphone"}), \
             patch.object(laut.sd, "InputStream", side_effect=[laut.sd.PortAudioError("driver error"), Mock()]) as stream:
            recorder = laut.Recorder()
        self.assertEqual(stream.call_args.kwargs["device"], 9)
        self.assertEqual(recorder.rate, 48000)

    def test_explicit_audio_device_is_not_silently_replaced(self):
        with patch.object(laut.sd, "query_hostapis") as apis, \
             patch.object(laut.sd, "query_devices", return_value={"default_samplerate": 48000, "name": "Microphone"}), \
             patch.object(laut.sd, "InputStream", side_effect=laut.sd.PortAudioError("driver error")):
            with self.assertRaises(RuntimeError):
                laut.Recorder(9)
        apis.assert_not_called()


if __name__ == "__main__":
    unittest.main()
