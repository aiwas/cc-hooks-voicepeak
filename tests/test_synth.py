"""合成まわり (作業ディレクトリの扱い) のテスト."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from cc_voicepeak.bridge import LocalBridge
from cc_voicepeak.synth import (
    WORK_DIR_MAX_AGE,
    Synthesizer,
    new_work_dir,
    prune_work_dirs,
)

DEAD_PID = 999999


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
