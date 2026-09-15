"""WSL <-> Windows のパス変換とプレイヤスクリプトのテスト.

WSL でなくても検証できる範囲 (文字列変換・スクリプト生成) を対象にする。
"""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from types import SimpleNamespace

from cc_voicepeak import bridge as bridge_module
from cc_voicepeak.bridge import (
    LocalBridge,
    WslBridge,
    _read_cached_wintemp,
    _write_cached_wintemp,
    detect_bridge,
    interop_enabled,
    resolve_exe,
)
from cc_voicepeak.errors import BridgeError
from cc_voicepeak.player import _PS_SCRIPT, _encoded_command, select_player


class WslPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = WslBridge()

    def test_mnt_path_to_windows_without_wslpath(self):
        with mock.patch.object(WslBridge, "_wslpath", side_effect=AssertionError):
            self.assertEqual(
                self.bridge.to_win("/mnt/c/Users/foo/a.wav"), "C:\\Users\\foo\\a.wav"
            )

    def test_drive_root(self):
        self.assertEqual(self.bridge.to_win("/mnt/d"), "D:\\")

    def test_windows_path_passes_through(self):
        self.assertEqual(self.bridge.to_win("C:\\tmp\\a.wav"), "C:\\tmp\\a.wav")
        self.assertEqual(self.bridge.to_win("C:/tmp/a.wav"), "C:\\tmp\\a.wav")

    def test_unc_path_passes_through(self):
        unc = "\\\\wsl.localhost\\Ubuntu\\tmp\\a.wav"
        self.assertEqual(self.bridge.to_win(unc), unc)

    def test_to_linux_from_windows_path(self):
        self.assertEqual(
            self.bridge.to_linux("C:\\Users\\foo\\a.wav"), Path("/mnt/c/Users/foo/a.wav")
        )

    def test_non_mnt_path_uses_wslpath(self):
        with mock.patch.object(
            WslBridge, "_wslpath", return_value="\\\\wsl.localhost\\Ubuntu\\home\\u\\a.wav"
        ) as called:
            result = self.bridge.to_win("/home/u/a.wav")
        self.assertTrue(result.startswith("\\\\wsl.localhost"))
        called.assert_called_once()

    def test_conversion_is_cached(self):
        with mock.patch.object(WslBridge, "_wslpath", return_value="X:\\a") as called:
            self.bridge.to_win("/home/u/a.wav")
            self.bridge.to_win("/home/u/a.wav")
        self.assertEqual(called.call_count, 1)

    def test_wslpath_failure_is_reported(self):
        with mock.patch("subprocess.run", side_effect=OSError("boom")):
            with self.assertRaises(BridgeError):
                self.bridge.to_win("/home/u/a.wav")

    def test_exec_cwd_avoids_unc(self):
        """Windows EXE の cwd は必ず /mnt 配下 (UNC 警告を避ける)."""
        with mock.patch.object(WslBridge, "temp_root", return_value=Path("/home/u/tmp")):
            self.assertEqual(self.bridge.exec_cwd(), "/mnt/c")
        with mock.patch.object(
            WslBridge, "temp_root", return_value=Path("/mnt/c/Temp/cc-voicepeak")
        ):
            self.assertEqual(self.bridge.exec_cwd(), "/mnt/c/Temp/cc-voicepeak")

    def test_temp_root_env_override(self):
        bridge = WslBridge()
        with mock.patch.dict(os.environ, {"CC_VOICEPEAK_TEMP": "/mnt/c/mytemp"}):
            self.assertEqual(bridge.temp_root(), Path("/mnt/c/mytemp"))


class InteropEnabledTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(bridge_module, "BINFMT_MISC_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name: str, body: str) -> None:
        (self.tmp / name).write_text(body, encoding="utf-8")

    def test_enabled_first_line(self):
        self.write("WSLInterop", "enabled\ninterpreter /init\nflags: PF\n")
        self.assertTrue(interop_enabled())

    def test_disabled_first_line(self):
        self.write("WSLInterop", "disabled\ninterpreter /init\nflags: PF\n")
        self.assertFalse(interop_enabled())

    def test_word_elsewhere_in_file_is_not_enough(self):
        self.write("WSLInterop", "disabled\ninterpreter /init/enabled\n")
        self.assertFalse(interop_enabled())

    def test_late_variant_is_accepted(self):
        self.write("WSLInterop-late", "enabled\n")
        self.assertTrue(interop_enabled())

    def test_missing_file(self):
        self.assertFalse(interop_enabled())


class TempRootTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(
            os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache")}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CC_VOICEPEAK_TEMP", None)
        # 実在する /mnt/c 配下を拾わないよう、既定候補を空ディレクトリに向ける
        missing = str(self.tmp / "missing")
        for name, value in (("WIN_USERS_DIR", missing), ("WIN_DRIVE_ROOTS", (missing,))):
            item = mock.patch.object(bridge_module, name, value)
            item.start()
            self.addCleanup(item.stop)

    def test_cached_wintemp_avoids_launching_cmd_exe(self):
        win_temp = self.tmp / "wintemp"
        win_temp.mkdir()
        _write_cached_wintemp(win_temp)
        with mock.patch.object(
            WslBridge, "_query_windows_env", side_effect=AssertionError("cmd.exe を起動した")
        ):
            self.assertEqual(WslBridge().temp_root(), win_temp / "cc-voicepeak")

    def test_windows_temp_is_queried_and_cached(self):
        win_temp = self.tmp / "AppData" / "Local" / "Temp"
        win_temp.mkdir(parents=True)
        with mock.patch.object(
            WslBridge, "_query_windows_env", return_value="C:\\Temp"
        ) as query, mock.patch.object(WslBridge, "to_linux", return_value=win_temp):
            self.assertEqual(WslBridge().temp_root(), win_temp / "cc-voicepeak")
        query.assert_called_once_with("TEMP")
        self.assertEqual(_read_cached_wintemp(), win_temp)

    def test_falls_back_to_tmpdir(self):
        with mock.patch.object(WslBridge, "_query_windows_env", return_value=None):
            with mock.patch.dict(os.environ, {"TMPDIR": str(self.tmp)}):
                self.assertEqual(WslBridge().temp_root(), self.tmp / "cc-voicepeak")

    def test_shared_windows_temp_is_not_a_candidate(self):
        """全ユーザ共有の C:\\Windows\\Temp へは落とさない.

        cache/<sha1>.wav は名前が予測でき、命中判定も「存在して 44 バイト超」
        だけなので、他ユーザが書ける場所だと wav を差し替えられる。
        実在しても候補に入れず、WSL 側へ落とすこと。
        """
        # /mnt/c 相当の直下に Windows/Temp を実在させる
        (self.tmp / "Windows" / "Temp").mkdir(parents=True)
        fallback = self.tmp / "wsl-side"
        fallback.mkdir()

        with mock.patch.object(bridge_module, "WIN_DRIVE_ROOTS", (str(self.tmp),)), \
                mock.patch.object(WslBridge, "_query_windows_env", return_value=None), \
                mock.patch.dict(os.environ, {"TMPDIR": str(fallback)}):
            root = WslBridge().temp_root()

        # 実在していても選ばれず、WSL 側へ落ちる
        self.assertEqual(root, fallback / "cc-voicepeak")

    def test_query_windows_env_uses_last_line(self):
        completed = SimpleNamespace(stdout="C:\\Users\\u\\AppData\\Local\\Temp\n", returncode=0)
        with mock.patch("subprocess.run", return_value=completed):
            self.assertEqual(
                WslBridge()._query_windows_env("TEMP"), "C:\\Users\\u\\AppData\\Local\\Temp"
            )

    def test_query_windows_env_ignores_unexpanded_variable(self):
        with mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="%TEMP%\n")):
            self.assertIsNone(WslBridge()._query_windows_env("TEMP"))

    def test_query_windows_env_handles_missing_cmd_exe(self):
        with mock.patch("subprocess.run", side_effect=OSError("cmd.exe なし")):
            self.assertIsNone(WslBridge()._query_windows_env("TEMP"))


class LocalBridgeTest(unittest.TestCase):
    def test_paths_pass_through(self):
        bridge = LocalBridge(temp_root=Path("/tmp/x"))
        self.assertEqual(bridge.to_win("/tmp/a.wav"), "/tmp/a.wav")
        self.assertEqual(bridge.to_linux("/tmp/a.wav"), Path("/tmp/a.wav"))
        self.assertEqual(bridge.temp_root(), Path("/tmp/x"))

    def test_env_override_is_read_when_temp_root_is_called(self):
        # WslBridge と評価タイミングを揃える (生成時ではなく参照時)
        bridge = LocalBridge()
        with mock.patch.dict(os.environ, {"CC_VOICEPEAK_TEMP": "/tmp/later"}):
            self.assertEqual(bridge.temp_root(), Path("/tmp/later"))


class DetectBridgeTest(unittest.TestCase):
    def test_force_local(self):
        self.assertEqual(detect_bridge("local").name, "local")

    def test_force_wsl(self):
        self.assertEqual(detect_bridge("wsl").name, "wsl")

    def test_unknown_mode(self):
        with self.assertRaises(BridgeError):
            detect_bridge("banana")

    def test_env_selects_bridge(self):
        with mock.patch.dict(os.environ, {"CC_VOICEPEAK_BRIDGE": "local"}):
            self.assertEqual(detect_bridge().name, "local")


class ResolveExeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.exe = self.tmp / "voicepeak.exe"
        self.exe.write_text("", encoding="utf-8")
        self.exe.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_configured_path_is_used(self):
        bridge = LocalBridge()
        self.assertEqual(resolve_exe(bridge, str(self.exe)), self.exe)

    def test_missing_configured_path_raises(self):
        with self.assertRaises(BridgeError):
            resolve_exe(LocalBridge(), "/nonexistent/voicepeak.exe")

    def test_windows_style_configured_path_is_converted(self):
        bridge = WslBridge()
        with mock.patch.object(WslBridge, "to_linux", return_value=self.exe):
            self.assertEqual(resolve_exe(bridge, "C:\\VOICEPEAK\\voicepeak.exe"), self.exe)

    def test_unc_configured_path_is_converted(self):
        bridge = WslBridge()
        with mock.patch.object(WslBridge, "to_linux", return_value=self.exe) as converted:
            resolve_exe(bridge, "\\\\wsl.localhost\\Ubuntu\\opt\\voicepeak.exe")
        converted.assert_called_once()

    def test_non_executable_path_raises(self):
        self.exe.chmod(0o644)
        with self.assertRaises(BridgeError) as caught:
            resolve_exe(LocalBridge(), str(self.exe))
        self.assertIn("実行権限", str(caught.exception))

    def test_autodetect_failure_raises(self):
        bridge = LocalBridge()
        with mock.patch.object(LocalBridge, "find_voicepeak", return_value=None):
            with self.assertRaises(BridgeError):
                resolve_exe(bridge, None)

    def test_wsl_autodetect_scans_user_profiles(self):
        users = self.tmp / "Users"
        for name in ("Public", "alice"):
            directory = users / name / "AppData/Local/Programs/VOICEPEAK"
            directory.mkdir(parents=True)
            (directory / "voicepeak.exe").write_text("", encoding="utf-8")
        target = users / "alice/AppData/Local/Programs/VOICEPEAK/voicepeak.exe"
        with mock.patch.object(bridge_module, "WIN_USERS_DIR", str(users)), mock.patch.object(
            bridge_module, "WIN_DRIVE_ROOTS", (str(self.tmp / "missing"),)
        ):
            # Public は Windows の組み込みプロファイルなので飛ばす
            self.assertEqual(WslBridge().find_voicepeak(), target)

    def test_wsl_autodetect_scans_program_files(self):
        bridge = WslBridge()
        real_is_file = Path.is_file
        target = "/mnt/c/Program Files/VOICEPEAK/voicepeak.exe"

        def fake_is_file(self):
            return str(self) == target or real_is_file(self)

        def fake_is_dir(self):
            return str(self) == "/mnt/c"

        with mock.patch.object(Path, "is_file", fake_is_file), mock.patch.object(
            Path, "is_dir", fake_is_dir
        ):
            self.assertEqual(str(bridge.find_voicepeak()), target)


class PlayerScriptTest(unittest.TestCase):
    def test_encoded_command_is_utf16_base64(self):
        encoded = _encoded_command("echo hi")
        self.assertEqual(base64.b64decode(encoded).decode("utf-16-le"), "echo hi")

    def test_player_script_reports_pid_and_plays_sync(self):
        self.assertIn("PID", _PS_SCRIPT)
        self.assertIn("PlaySync", _PS_SCRIPT)
        self.assertIn("__QUIT__", _PS_SCRIPT)

    def test_none_backend(self):
        self.assertEqual(select_player("none", LocalBridge()).name, "none")

    def test_wsl_bridge_defaults_to_powershell(self):
        with mock.patch("cc_voicepeak.player.find_powershell", return_value="powershell.exe"):
            player = select_player("auto", WslBridge())
        self.assertEqual(player.name, "powershell")

    def test_auto_falls_back_when_powershell_is_missing(self):
        # appendWindowsPath=false の環境。powershell を選ぶと無音になる
        def which(name):
            return "/usr/bin/paplay" if name == "paplay" else None

        with mock.patch("cc_voicepeak.player.find_powershell", return_value=None):
            with mock.patch("shutil.which", side_effect=which):
                player = select_player("auto", WslBridge())
        self.assertEqual(player.name, "paplay")

    def test_explicit_powershell_without_executable_raises(self):
        from cc_voicepeak.errors import PlayerError

        with mock.patch("cc_voicepeak.player.find_powershell", return_value=None):
            with self.assertRaises(PlayerError):
                select_player("powershell", WslBridge())

    def test_find_powershell_uses_known_install_path(self):
        from cc_voicepeak.player import POWERSHELL_FALLBACKS, find_powershell

        with mock.patch("shutil.which", return_value=None):
            with mock.patch.object(Path, "is_file", lambda self: True):
                self.assertEqual(find_powershell(), POWERSHELL_FALLBACKS[0])

    def test_missing_command_backend_raises(self):
        from cc_voicepeak.errors import PlayerError

        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(PlayerError):
                select_player("paplay", LocalBridge())
