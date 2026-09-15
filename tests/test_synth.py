"""合成まわり (作業ディレクトリの扱い) のテスト."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cc_voicepeak.bridge import LocalBridge
from cc_voicepeak.errors import SynthError
from cc_voicepeak.synth import (
    WORK_DIR_MAX_AGE,
    Synthesizer,
    new_work_dir,
    prune_work_dirs,
    redact_command,
)

DEAD_PID = 999999
FAKE = Path(__file__).resolve().parent / "fake_voicepeak.py"


class WorkDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-synth-"))
        self.run_root = self.tmp / "run"
        self.run_root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_dir(self, name: str, age: float = 0.0) -> Path:
        path = self.run_root / name
        path.mkdir()
        (path / "0001.wav").write_bytes(b"x" * 64)
        if age:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path


class PruneWorkDirsTest(WorkDirTestCase):
    def test_dead_process_directory_is_removed(self):
        stale = self.make_dir(f"{DEAD_PID}-abcdef")
        self.assertEqual(prune_work_dirs(self.tmp), 1)
        self.assertFalse(stale.exists())

    def test_live_process_directory_is_kept(self):
        alive = self.make_dir(f"{os.getpid()}-abcdef")
        self.assertEqual(prune_work_dirs(self.tmp), 0)
        self.assertTrue(alive.exists())

    def test_old_directory_is_removed_even_if_pid_is_alive(self):
        # PID 再利用や別ディストロとの衝突で消し損ねないための保険
        old = self.make_dir(f"{os.getpid()}-abcdef", age=WORK_DIR_MAX_AGE + 60)
        self.assertEqual(prune_work_dirs(self.tmp), 1)
        self.assertFalse(old.exists())

    def test_current_work_dir_is_kept(self):
        current = self.make_dir(f"{DEAD_PID}-current")
        self.assertEqual(prune_work_dirs(self.tmp, keep=current), 0)
        self.assertTrue(current.exists())

    def test_unparsable_name_is_removed(self):
        junk = self.make_dir("not-a-pid")
        self.assertEqual(prune_work_dirs(self.tmp), 1)
        self.assertFalse(junk.exists())

    def test_missing_run_root_is_not_an_error(self):
        self.assertEqual(prune_work_dirs(self.tmp / "nowhere"), 0)


class NewWorkDirTest(WorkDirTestCase):
    def test_directories_are_unique(self):
        first = new_work_dir(self.tmp)
        second = new_work_dir(self.tmp)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir() and second.is_dir())
        self.assertTrue(first.name.startswith(f"{os.getpid()}-"))

    def test_synthesizer_cleans_up_stale_dirs(self):
        stale = self.make_dir(f"{DEAD_PID}-abcdef")
        synth = Synthesizer(
            exe=Path("/nonexistent/voicepeak.exe"),
            bridge=LocalBridge(temp_root=self.tmp),
            use_cache=False,
        )
        try:
            self.assertFalse(stale.exists(), "前回の残骸が掃除されていない")
            self.assertTrue(synth.work_dir.is_dir())
            self.assertEqual(synth.work_dir.parent, self.run_root)
        finally:
            synth.cleanup()


class SynthesizerTestCase(unittest.TestCase):
    """本物の代わりに tests/fake_voicepeak.py を叩く."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-synth-"))
        self.calls = self.tmp / "calls.jsonl"
        self._env = dict(os.environ)
        for name in list(os.environ):
            if name.startswith("FAKE_VOICEPEAK_"):
                del os.environ[name]
        os.environ["FAKE_VOICEPEAK_LOG"] = str(self.calls)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, **kwargs) -> Synthesizer:
        options = {
            "exe": FAKE,
            "bridge": LocalBridge(temp_root=self.tmp),
            "use_cache": False,
        }
        options.update(kwargs)
        synth = Synthesizer(**options)
        self.addCleanup(synth.cleanup)
        return synth

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [
            json.loads(line)
            for line in self.calls.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class CacheTest(SynthesizerTestCase):
    def test_cache_hit_skips_the_exe(self):
        synth = self.make(use_cache=True)
        first = synth.synth_block(1, "キャッシュの確認です。")
        self.assertTrue(first.ok)
        second = synth.synth_block(2, "キャッシュの確認です。")
        self.assertTrue(second.cached)
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_broken_cache_falls_back_to_synthesis(self):
        synth = self.make(use_cache=True)
        synth.synth_block(1, "壊れたキャッシュの確認です。")
        for cached in synth.cache_dir.glob("*.wav"):
            cached.write_bytes(b"x" * 10)  # ヘッダにも満たない
        result = synth.synth_block(2, "壊れたキャッシュの確認です。")
        self.assertTrue(result.ok)
        self.assertFalse(result.cached)
        self.assertEqual(len(self.recorded_calls()), 2)

    def test_cache_write_is_atomic(self):
        synth = self.make(use_cache=True)
        synth.synth_block(1, "アトミック書き込みの確認です。")
        self.assertEqual(list(synth.cache_dir.glob("*.tmp")), [], "一時ファイルが残っている")
        cached = list(synth.cache_dir.glob("*.wav"))
        self.assertEqual(len(cached), 1)
        self.assertGreater(cached[0].stat().st_size, 44)

    def test_prune_cache_keeps_the_newest(self):
        synth = self.make(use_cache=True, cache_max_files=2)
        for index in range(4):
            synth.synth_block(index, f"{index}番目の文です。")
        synth.prune_cache()
        self.assertEqual(len(list(synth.cache_dir.glob("*.wav"))), 2)


class RetryTest(SynthesizerTestCase):
    def test_modes_alternate(self):
        synth = self.make(retries=3)
        self.assertEqual(
            synth._attempt_modes("ふつうの文です。"),
            ["say", "text_file", "say", "text_file"],
        )

    def test_leading_hyphen_stays_on_text_file(self):
        # -s の値がオプションと誤認されるので say には戻さない
        synth = self.make(retries=2)
        self.assertEqual(synth._attempt_modes("-から始まる文"), ["text_file"] * 3)

    def test_no_retry_means_one_attempt(self):
        self.assertEqual(self.make(retries=0)._attempt_modes("文です。"), ["say"])

    def test_say_failure_falls_back_to_text_file(self):
        os.environ["FAKE_VOICEPEAK_FAIL_MODE"] = "say"
        synth = self.make(retries=1)
        with mock.patch("cc_voicepeak.synth.RETRY_BACKOFF", 0.0):
            result = synth.synth_block(1, "フォールバックの確認です。")
        self.assertTrue(result.ok)
        self.assertEqual([call["mode"] for call in self.recorded_calls()], ["say", "text_file"])

    def test_permanent_failure_stops_repeating_the_same_mode(self):
        os.environ["FAKE_VOICEPEAK_LIMIT"] = "5"
        synth = self.make(retries=3)
        with mock.patch("cc_voicepeak.synth.RETRY_BACKOFF", 0.0):
            result = synth.synth_block(1, "上限を超える長さの文です。")
        self.assertFalse(result.ok)
        # 両モードを 1 回ずつ試したら諦める (4 回は起動しない)
        self.assertEqual([call["mode"] for call in self.recorded_calls()], ["say", "text_file"])

    def test_timeout_is_reported(self):
        os.environ["FAKE_VOICEPEAK_HANG"] = "5"
        synth = self.make(timeout=1, retries=0)
        result = synth.synth_block(1, "タイムアウトの確認です。")
        self.assertFalse(result.ok)
        self.assertIn("タイムアウト", result.error)


class SimpleRunTest(SynthesizerTestCase):
    def test_narrators_are_listed(self):
        self.assertIn("Fake Narrator A", self.make().list_narrators())

    def test_cp932_output_is_decoded(self):
        os.environ["FAKE_VOICEPEAK_ENCODING"] = "cp932"
        self.assertIn("happy", self.make().list_emotions("Fake Narrator A"))

    def test_failure_is_not_returned_as_a_narrator_list(self):
        synth = self.make(exe=Path(__file__).resolve().parent / "fake_failing.py")
        with self.assertRaises(SynthError):
            synth.list_narrators()


class RedactTest(unittest.TestCase):
    def test_say_text_is_replaced_by_length_and_hash(self):
        redacted = redact_command(["voicepeak", "-s", "秘密の本文です", "-o", "a.wav"])
        self.assertNotIn("秘密の本文です", redacted)
        self.assertEqual(redacted[0], "voicepeak")
        self.assertIn("7文字", redacted[2])
        self.assertIn("sha1:", redacted[2])
        self.assertEqual(redacted[3:], ["-o", "a.wav"])

    def test_other_tokens_are_kept(self):
        command = ["voicepeak", "-t", "/tmp/a.txt", "-o", "a.wav"]
        self.assertEqual(redact_command(command), command)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
