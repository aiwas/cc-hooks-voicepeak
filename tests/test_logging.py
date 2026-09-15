"""ログ設定のテスト.

hook プロセスとデタッチされた読み上げプロセスが同じファイルへ書くため、
ローテーションが重なっても行が壊れないことを実プロセスで確かめる。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cc_voicepeak.logging_util import MultiProcessRotatingFileHandler

REPO = Path(__file__).resolve().parent.parent

# "2026-09-15 12:00:00,000 INFO    [1234] cc_voicepeak.x: LINE-1234-7"
LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \w+ +\[\d+\] \S+: LINE-\d+-\d+$"
)

# 子プロセス側: 同じログファイルへひたすら書く
WRITER = """
import logging, os, sys
sys.path.insert(0, {repo!r})
from cc_voicepeak.logging_util import MultiProcessRotatingFileHandler

handler = MultiProcessRotatingFileHandler({path!r}, maxBytes=4096, backupCount=1)
handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)-7s [%(process)d] %(name)s: %(message)s")
)
logger = logging.getLogger("cc_voicepeak.writer")
logger.setLevel(logging.INFO)
logger.addHandler(handler)
for index in range({count}):
    logger.info("LINE-%d-%d", os.getpid(), index)
handler.close()
"""


class HandlerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-log-"))
        self.log = self.tmp / "cc-voicepeak.log"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_handler(self, max_bytes: int = 4096) -> MultiProcessRotatingFileHandler:
        handler = MultiProcessRotatingFileHandler(
            self.log, maxBytes=max_bytes, backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(handler.close)
        return handler

    def record(self, message: str) -> logging.LogRecord:
        return logging.LogRecord("cc_voicepeak.test", logging.INFO, __file__, 1, message, (), None)


class RotationTest(HandlerTestCase):
    def test_file_is_created_lazily(self):
        self.make_handler()
        self.assertFalse(self.log.exists(), "1 行も出していないのにファイルがある")

    def test_records_are_written(self):
        handler = self.make_handler()
        handler.emit(self.record("最初の行"))
        self.assertIn("最初の行", self.log.read_text(encoding="utf-8"))

    def test_reopens_after_another_process_rotates(self):
        handler = self.make_handler()
        handler.emit(self.record("前の行"))
        # 別プロセスがローテーションした状況を模す
        self.log.rename(self.tmp / "cc-voicepeak.log.1")
        handler.emit(self.record("あとの行"))
        self.assertTrue(self.log.exists(), "退避されたファイルへ書き続けている")
        self.assertIn("あとの行", self.log.read_text(encoding="utf-8"))

    def test_rotation_happens_at_the_limit(self):
        handler = self.make_handler(max_bytes=200)
        for index in range(50):
            handler.emit(self.record(f"{index}: " + "あ" * 20))
        self.assertTrue((self.tmp / "cc-voicepeak.log.1").exists())
        self.assertLessEqual(self.log.stat().st_size, 400)


class ConcurrentWriteTest(HandlerTestCase):
    def run_writers(self, count: int = 3, records: int = 120):
        script = WRITER.format(repo=str(REPO), path=str(self.log), count=records)
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=str(REPO),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(count)
        ]
        errors = []
        for proc in procs:
            _, stderr = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0)
            errors.append(stderr.decode("utf-8", errors="replace"))
        return errors

    def test_rotation_does_not_break_between_processes(self):
        """素の RotatingFileHandler だと doRollover が FileNotFoundError を撒く."""
        errors = self.run_writers()
        noisy = [text for text in errors if "Logging error" in text]
        self.assertEqual(noisy[:1], [], "ローテーションが衝突している")

    def test_written_lines_are_well_formed(self):
        self.run_writers(records=40)
        written = []
        for path in (self.tmp / "cc-voicepeak.log.1", self.log):
            if path.exists():
                written += path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(written, "1 行も書かれていない")
        broken = [line for line in written if not LINE.match(line)]
        self.assertEqual(broken[:3], [], "行が混ざっている")

    def test_lock_file_is_next_to_the_log(self):
        handler = self.make_handler()
        handler.emit(self.record("行"))
        self.assertTrue((self.tmp / "cc-voicepeak.log.lock").exists())


class LevelOffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-log-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, level: str):
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "CC_VOICEPEAK_LOG_LEVEL": level,
            "CC_VOICEPEAK_BRIDGE": "local",
        }
        env.pop("CC_VOICEPEAK_LOG_FILE", None)
        env.pop("CC_VOICEPEAK_CONFIG", None)
        # 対象外のイベントは info で「読み上げをスキップ」を 1 行出す
        payload = '{"hook_event_name": "PreToolUse", "message": "無視される"}'
        return subprocess.run(
            [sys.executable, "-m", "cc_voicepeak", "hook", "--sync"],
            input=payload.encode("utf-8"),
            capture_output=True,
            cwd=str(REPO),
            env=env,
            timeout=120,
        )

    def test_off_creates_no_log_file(self):
        self.assertEqual(self.run_cli("off").returncode, 0)
        self.assertFalse(
            (self.tmp / "state" / "cc-voicepeak").exists(),
            "off なのにログのディレクトリが作られている",
        )

    def test_info_creates_the_log_file(self):
        self.assertEqual(self.run_cli("info").returncode, 0)
        log = self.tmp / "state" / "cc-voicepeak" / "cc-voicepeak.log"
        self.assertTrue(log.exists())
        self.assertIn("スキップ", log.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
