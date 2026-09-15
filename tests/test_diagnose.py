"""``cc-voicepeak check`` の診断ロジックのテスト."""

from __future__ import annotations

import unittest
from unittest import mock

from cc_voicepeak.bridge import LocalBridge, WslBridge
from cc_voicepeak.diagnose import FAIL, OK, WARN, _player_check


def only(name: str, path: str):
    """指定したコマンドだけが PATH にある状態を作る."""
    return lambda candidate: path if candidate == name else None


class PlayerCheckTest(unittest.TestCase):
    def test_none_backend_is_ok_with_detail(self):
        item = _player_check("none", LocalBridge())
        self.assertEqual(item.status, OK)
        self.assertTrue(item.detail)

    def test_auto_on_non_wsl_reports_found_command(self):
        with mock.patch("shutil.which", side_effect=only("aplay", "/usr/bin/aplay")):
            item = _player_check("auto", LocalBridge())
        self.assertEqual(item.status, OK)
        self.assertIn("aplay", item.name)

    def test_auto_without_any_command_is_not_an_empty_warning(self):
        with mock.patch("shutil.which", return_value=None):
            item = _player_check("auto", LocalBridge())
        self.assertEqual(item.status, WARN)
        self.assertTrue(item.detail)
        self.assertTrue(item.hint)

    def test_auto_on_wsl_uses_powershell(self):
        with mock.patch("cc_voicepeak.diagnose.find_powershell", return_value="/x/powershell.exe"):
            item = _player_check("auto", WslBridge())
        self.assertEqual(item.status, OK)
        self.assertIn("powershell", item.name)

    def test_auto_on_wsl_falls_back_to_command_player(self):
        with mock.patch("cc_voicepeak.diagnose.find_powershell", return_value=None):
            with mock.patch("shutil.which", side_effect=only("paplay", "/usr/bin/paplay")):
                item = _player_check("auto", WslBridge())
        self.assertEqual(item.status, OK)
        self.assertIn("paplay", item.name)

    def test_volume_with_powershell_is_warned(self):
        # SoundPlayer には音量の API が無い
        with mock.patch("cc_voicepeak.diagnose.find_powershell", return_value="/x/ps.exe"):
            item = _player_check("auto", WslBridge(), volume=50)
        self.assertEqual(item.status, WARN)
        self.assertIn("player.volume", item.detail)
        self.assertIn("paplay", item.hint)

    def test_volume_with_command_player_is_fine(self):
        with mock.patch("shutil.which", side_effect=only("paplay", "/usr/bin/paplay")):
            item = _player_check("paplay", LocalBridge(), volume=50)
        self.assertEqual(item.status, OK)

    def test_explicit_powershell_without_executable_fails(self):
        with mock.patch("cc_voicepeak.diagnose.find_powershell", return_value=None):
            item = _player_check("powershell", WslBridge())
        self.assertEqual(item.status, FAIL)
        self.assertIn("appendWindowsPath", item.hint)

    def test_missing_command_backend_suggests_package(self):
        with mock.patch("shutil.which", return_value=None):
            item = _player_check("ffplay", LocalBridge())
        self.assertEqual(item.status, FAIL)
        self.assertIn("ffmpeg", item.hint)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
