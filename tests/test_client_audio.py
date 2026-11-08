"""Process ownership and non-blocking playback; no sound devices used here."""

import subprocess
import unittest
from unittest.mock import Mock, patch

from client_audio import SOUND_FILE, SoundPlayer


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.player = SoundPlayer()
        self.addCleanup(self.player.close)
        self.executable = self.enterContext(patch("client_audio.shutil.which", return_value="ffplay"))
        self.spawn = self.enterContext(patch("client_audio.subprocess.Popen"))
        self.process = self.spawn.return_value
        self.process.poll.return_value = None

    def test_play_uses_fixed_bundled_asset_without_shell_pipes_or_waiting(self):
        self.player.play()
        args = self.spawn.call_args.args[0]
        self.assertEqual(args[-1], str(SOUND_FILE))
        self.assertTrue(SOUND_FILE.is_file())
        self.assertEqual(SOUND_FILE.name, "sfx1.mp3")
        self.assertIn("-nodisp", args)
        self.assertIn("-autoexit", args)
        self.assertNotIn("shell", self.spawn.call_args.kwargs)
        self.assertEqual(self.spawn.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.process.wait.assert_not_called()

    def test_retrigger_kills_old_clip_without_waiting_then_poll_reaps_it(self):
        old, new = Mock(), Mock()
        old.poll.return_value = new.poll.return_value = None
        self.spawn.side_effect = [old, new]
        self.player.play()
        self.player.play()
        old.kill.assert_called_once()
        old.wait.assert_not_called()
        self.assertIn(old, self.player._retired)
        old.poll.return_value = -9
        self.assertIsNone(self.player.poll())
        self.assertNotIn(old, self.player._retired)
        self.assertIs(self.player._process, new)

    def test_natural_exit_is_reaped_and_failure_is_reported_once(self):
        self.player.play()
        self.process.poll.return_value = 0
        self.assertIsNone(self.player.poll())
        self.assertIsNone(self.player._process)
        self.process.poll.return_value = None
        self.player.play()
        self.process.poll.return_value = 1
        self.assertIn("audio device", self.player.poll())
        self.assertIsNone(self.player.poll())

    def test_stop_and_close_reap_process_and_late_play_does_nothing(self):
        self.player.play()
        self.player.stop()
        self.process.kill.assert_called_once()
        self.process.wait.assert_not_called()
        self.player.close()
        self.process.wait.assert_called_once()
        self.assertIsNone(self.player._process)
        self.assertEqual(self.player._retired, [])
        self.player.close()
        self.player.play()
        self.spawn.assert_called_once()

    def test_missing_ffplay_is_a_safe_actionable_error(self):
        self.executable.return_value = None
        with self.assertRaisesRegex(RuntimeError, "install FFmpeg"):
            self.player.play()
        self.spawn.assert_not_called()

    def test_missing_asset_does_not_start_a_player(self):
        with patch("client_audio.SOUND_FILE") as sound_file:
            sound_file.is_file.return_value = False
            with self.assertRaisesRegex(RuntimeError, "missing"):
                self.player.play()
        self.spawn.assert_not_called()

    def test_spawn_failure_is_safe_without_exposing_raw_process_details(self):
        self.spawn.side_effect = OSError("private process details")
        with self.assertRaisesRegex(RuntimeError, "Cannot start") as caught:
            self.player.play()
        self.assertNotIn("private process details", str(caught.exception))
