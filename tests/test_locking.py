"""排他制御・割り込みのテスト."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cc_voicepeak.locking import ExeLock, SpeechSlot, runtime_dir


class SlotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-lock-"))
        self._env = dict(os.environ)
        os.environ["XDG_RUNTIME_DIR"] = str(self.tmp)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)


class SpeechSlotTest(SlotTestCase):
    def test_runtime_dir_is_created(self):
        self.assertTrue(runtime_dir().is_dir())

    def test_slot_is_free_initially(self):
        self.assertFalse(SpeechSlot("s1").busy())

    def test_write_and_clear(self):
        slot = SpeechSlot("s1")
        slot.write(win_pid=1234)
        self.assertTrue(slot.busy())
        self.assertEqual(slot.read()["win_pid"], 1234)
        slot.clear()
        self.assertFalse(slot.busy())

    def test_stale_entry_from_dead_process_is_ignored(self):
        slot = SpeechSlot("s2")
        slot.path.write_text('{"pid": 999999, "win_pid": null}', encoding="utf-8")
        self.assertFalse(slot.busy())

    def test_session_keys_are_isolated(self):
        SpeechSlot("aaa").write()
        self.assertFalse(SpeechSlot("bbb").busy())

    def test_unsafe_key_is_sanitized(self):
        slot = SpeechSlot("../../etc/passwd")
        self.assertEqual(slot.path.parent, runtime_dir())

    def test_broken_state_file_is_ignored(self):
        slot = SpeechSlot("s3")
        slot.path.write_text("not json", encoding="utf-8")
        self.assertIsNone(slot.read())

    def test_interrupt_kills_running_process(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        slot = SpeechSlot("s4")
        slot.path.write_text(
            '{"pid": %d, "win_pid": null}' % child.pid, encoding="utf-8"
        )
        try:
            self.assertTrue(slot.interrupt())
            deadline = time.time() + 10
            while child.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(child.poll(), "プロセスが終了していない")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_interrupt_calls_taskkill_for_windows_pid(self):
        slot = SpeechSlot("s5")
        slot.path.write_text('{"pid": 999999, "win_pid": 4242}', encoding="utf-8")
        # 死んだ pid でもエントリは残しておく必要があるので直接読ませる
        with mock.patch("cc_voicepeak.locking.taskkill") as killer, mock.patch.object(
            SpeechSlot, "read", return_value={"pid": 999999, "win_pid": 4242}
        ):
            self.assertTrue(slot.interrupt())
        killer.assert_called_once_with(4242)

    def test_interrupt_on_free_slot_is_noop(self):
        self.assertFalse(SpeechSlot("s6").interrupt())

    def test_wait_until_free_times_out(self):
        slot = SpeechSlot("s7")
        with mock.patch.object(SpeechSlot, "busy", return_value=True):
            started = time.monotonic()
            self.assertFalse(slot.wait_until_free(timeout=0.3))
            self.assertLess(time.monotonic() - started, 5)


class ExeLockTest(SlotTestCase):
    def test_lock_is_exclusive_across_processes(self):
        script = (
            "import os, sys, time;"
            "sys.path.insert(0, %r);"
            "from cc_voicepeak.locking import ExeLock;"
            "lock = ExeLock('test.lock');"
            "lock.__enter__();"
            "print('acquired', flush=True);"
            "time.sleep(2);"
            "lock.__exit__()" % str(Path(__file__).resolve().parent.parent)
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            env={**os.environ, "XDG_RUNTIME_DIR": str(self.tmp)},
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), b"acquired")
            started = time.monotonic()
            with ExeLock("test.lock", timeout=10):
                waited = time.monotonic() - started
            self.assertGreater(waited, 0.5, "ロックが排他になっていない")
        finally:
            if child.stdout is not None:
                child.stdout.close()
            child.wait(timeout=15)

    def test_lock_timeout_does_not_raise(self):
        with ExeLock("t2.lock"):
            with ExeLock("t2.lock", timeout=0.2):
                pass
