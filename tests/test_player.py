"""再生バックエンドのテスト.

``tests/fake_player.py`` を ``executable`` に渡すことで、常駐プレイヤの
標準入力プロトコル (PID 行 / wav パス / ``__QUIT__``) を実際に動かして確認する。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

from cc_voicepeak.bridge import LocalBridge
from cc_voicepeak.errors import PlayerError
from cc_voicepeak.player import CommandPlayer, PowershellPlayer

FAKE_PLAYER = Path(__file__).resolve().parent / "fake_player.py"


def write_wav(path: Path, seconds: float = 0.05) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(b"\x00\x00" * int(48000 * seconds))
    return path


class PlayerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-player-"))
        self.log = self.tmp / "player.log"
        self.wav = write_wav(self.tmp / "a.wav")
        self._env = dict(os.environ)
        os.environ["FAKE_PLAYER_LOG"] = str(self.log)
        os.environ.pop("FAKE_PLAYER_MODE", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def recorded(self):
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").split()

    def make_player(self) -> PowershellPlayer:
        # exec_cwd に使われるので、実在するディレクトリを指す bridge にする
        return PowershellPlayer(
            LocalBridge(temp_root=self.tmp), executable=str(FAKE_PLAYER)
        )


class PowershellPlayerTest(PlayerTestCase):
    def test_pid_line_is_parsed(self):
        player = self.make_player()
        player.start()
        try:
            self.assertEqual(player.win_pid, player.proc.pid)
        finally:
            player.stop()

    def test_enqueue_and_quit(self):
        player = self.make_player()
        player.start()
        player.enqueue(self.wav)
        player.finish(timeout=10)
        self.assertEqual(self.recorded(), [str(self.wav), "__QUIT__"])
        self.assertIsNone(player.proc, "finish() の後に参照が残っている")

    def test_finish_reaps_the_process(self):
        player = self.make_player()
        player.start()
        proc = player.proc
        player.finish(timeout=10)
        # wait() 済みなのでゾンビにならず、パイプも閉じている
        self.assertIsNotNone(proc.returncode)
        self.assertTrue(proc.stdout.closed)
        self.assertIsNone(player._reader)

    def test_stop_reaps_the_process(self):
        player = self.make_player()
        player.start()
        proc = player.proc
        with mock.patch("cc_voicepeak.locking.taskkill") as killer:
            player.stop()
        killer.assert_called_once()
        self.assertIsNotNone(proc.returncode)
        self.assertIsNone(player.proc)
        self.assertIsNone(player._reader)

    def test_enqueue_after_stop_raises(self):
        player = self.make_player()
        player.start()
        player.stop()
        # proc が None なので start() し直される。停止済みの参照は使わない
        try:
            player.enqueue(self.wav)
        finally:
            player.stop()

    def test_dead_player_fails_fast(self):
        os.environ["FAKE_PLAYER_MODE"] = "die"
        player = self.make_player()
        started = time.monotonic()
        with self.assertRaises(PlayerError):
            player.start()
        self.assertLess(time.monotonic() - started, 10, "PID 待ちでブロックしている")

    def test_missing_pid_does_not_block_forever(self):
        os.environ["FAKE_PLAYER_MODE"] = "silent"
        player = self.make_player()
        with mock.patch("cc_voicepeak.player.PID_WAIT_TIMEOUT", 0.5):
            started = time.monotonic()
            player.start()
        try:
            self.assertLess(time.monotonic() - started, 5)
            self.assertIsNone(player.win_pid)
        finally:
            player.stop()


class CommandPlayerTest(PlayerTestCase):
    def test_paplay_volume_is_scaled(self):
        command = CommandPlayer("paplay", volume=50)._command(self.wav)
        self.assertEqual(command[:3], ["paplay", "--volume", "32768"])

    def test_paplay_volume_is_clamped(self):
        command = CommandPlayer("paplay", volume=500)._command(self.wav)
        self.assertEqual(command[2], "65536")

    def test_ffplay_volume_is_passed_through(self):
        command = CommandPlayer("ffplay", volume=30)._command(self.wav)
        self.assertIn("-volume", command)
        self.assertEqual(command[command.index("-volume") + 1], "30")

    def test_unknown_backend_raises(self):
        with self.assertRaises(PlayerError):
            CommandPlayer("mplayer")._command(self.wav)

    def test_worker_survives_a_failing_backend(self):
        player = CommandPlayer("mplayer")
        player.enqueue(self.wav)
        player.enqueue(self.wav)
        player.finish(timeout=10)
        self.assertIsNone(player._thread)

    def test_playback_is_cut_off_when_it_hangs(self):
        class HangingPlayer(CommandPlayer):
            def _command(self, path):
                return [sys.executable, "-c", "import time; time.sleep(60)"]

        player = HangingPlayer("aplay")
        with mock.patch("cc_voicepeak.player.PLAYBACK_TIMEOUT_MARGIN", 0.5):
            player.enqueue(self.wav)
            started = time.monotonic()
            player.finish(timeout=15)
        self.assertLess(time.monotonic() - started, 15, "再生が打ち切られていない")

    def test_enqueue_after_finish_raises(self):
        player = CommandPlayer("aplay")
        player.start()
        thread = player._thread
        player.finish(timeout=10)
        thread.join(timeout=5)
        player._thread = thread  # finish() が外した参照を戻して死活判定を見る
        with self.assertRaises(PlayerError):
            player.enqueue(self.wav)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
