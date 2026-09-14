"""Cancellation must invalidate work already queued by the decoder."""
import threading
import unittest
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

if sys.platform == "win32":
    raise unittest.SkipTest("Linux/GTK implementation; Windows coverage is in test_windows.py")

import lautschrift as laut


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.daemon = laut.Daemon.__new__(laut.Daemon)
        d = self.daemon
        d.session = 1
        d.recording = True
        d.processing = False
        d.stop_flag = threading.Event()
        d.wav = Path(self.temp.name) / 'rec.wav'
        d.wav.write_bytes(b'recording')
        d.rec_proc = Mock()
        d.overlay = Mock()
        d._paste = Mock()
        d._notify = Mock()
        self.sound = patch.object(laut, 'play_sound').start()
        self.run = patch.object(laut.subprocess, 'run').start()
        self.addCleanup(patch.stopall)

    def test_cancel_stops_recorder_and_discards_audio(self):
        d = self.daemon
        proc = d.rec_proc
        self.assertFalse(d.cancel())
        proc.send_signal.assert_called_once_with(laut.signal.SIGINT)
        proc.wait.assert_called_once_with(timeout=3)
        self.assertTrue(d.stop_flag.is_set())
        self.assertFalse(d.recording)
        self.assertFalse(d.processing)
        self.assertFalse(d.wav.exists())
        d.overlay.hide.assert_called_once()
        self.run.assert_not_called()
        d._paste.assert_not_called()

    def test_cancel_during_final_decode_ignores_result(self):
        d = self.daemon
        d.recording, d.processing = False, True
        d.cancel()
        d.overlay.reset_mock()
        d._finish(1, 'discard this')
        d._deliver(1, 'discard this')
        self.run.assert_not_called()
        d._paste.assert_not_called()
        d.overlay.hide.assert_not_called()

    def test_cancel_between_final_result_and_paste(self):
        d = self.daemon
        d.recording, d.processing = False, True
        with patch.object(laut.GLib, 'timeout_add') as timer:
            d._finish(1, 'discard this')
        delay, callback, session, text = timer.call_args.args
        self.assertEqual(delay, 200)
        d.cancel()
        callback(session, text)
        self.run.assert_not_called()
        d._paste.assert_not_called()

    def test_old_results_cannot_change_new_recording(self):
        d = self.daemon
        old_event = d.stop_flag
        d.cancel()
        d.session += 1
        d.recording = True
        d.stop_flag = threading.Event()
        d.overlay.reset_mock()
        d._update_text(1, 'stale')
        d._finish(1, 'stale')
        d._deliver(1, 'stale')
        d.overlay.set_text.assert_not_called()
        d.overlay.hide.assert_not_called()
        self.assertTrue(old_event.is_set())
        self.assertFalse(d.stop_flag.is_set())
        d._update_text(d.session, 'current')
        d.overlay.set_text.assert_called_once_with('current')

    def test_idle_escape_does_nothing(self):
        d = self.daemon
        d.recording = False
        d.cancel()
        self.assertEqual(d.session, 1)
        d.overlay.hide.assert_not_called()
        d.rec_proc.send_signal.assert_not_called()

    def test_normal_completion_still_copies_and_pastes_once(self):
        d = self.daemon
        d.recording, d.processing = False, True
        d._deliver(1, 'hello')
        d._deliver(1, 'hello')
        self.assertEqual(self.run.call_count, 2)
        d._paste.assert_called_once()
        self.assertFalse(d.processing)

    def test_hotkey_during_processing_does_not_start_another_recorder(self):
        d = self.daemon
        d.recording, d.processing = False, True
        d._start = Mock()
        d.toggle()
        d._start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
