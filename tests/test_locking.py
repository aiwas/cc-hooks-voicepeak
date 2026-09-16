"""排他制御・割り込みのテスト."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cc_voicepeak.errors import LockTimeout, RuntimeDirError
from cc_voicepeak.locking import (
    ExeLock,
    SpeechSlot,
    process_token,
    runtime_dir,
    taskkill,
)


class SlotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-lock-"))
        self._env = dict(os.environ)
        os.environ["XDG_RUNTIME_DIR"] = str(self.tmp)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sleeping_child(self) -> subprocess.Popen:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        self.addCleanup(self.stop_child, child)
        return child

    @staticmethod
    def stop_child(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)

    def occupied_slot(self, key: str, token=...):
        """他プロセスが確保している状態のスロットを作る."""
        child = self.sleeping_child()
        slot = SpeechSlot(key)
        slot.path.write_text(
            json.dumps(
                {
                    "pid": child.pid,
                    "pid_token": process_token(child.pid) if token is ... else token,
                    "win_pid": None,
                }
            ),
            encoding="utf-8",
        )
        return slot, child

    @staticmethod
    def wait_for_exit(child: subprocess.Popen, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                return True
            time.sleep(0.05)
        return False


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
        slot, child = self.occupied_slot("s4")
        self.assertTrue(slot.interrupt())
        self.assertTrue(self.wait_for_exit(child), "プロセスが終了していない")

    def test_state_without_token_is_stale_and_never_killed(self):
        """pid_token の無い状態ファイルは古いものとして扱い、kill しない.

        生存確認だけで信用すると {"pid": <他人の pid>} を置かれただけで
        そのプロセスグループへ SIGTERM / SIGKILL が飛ぶ。
        """
        slot, child = self.occupied_slot("s4b", token=None)
        self.assertFalse(slot.busy())
        self.assertFalse(slot.interrupt())
        time.sleep(0.2)
        self.assertIsNone(child.poll(), "無関係なプロセスを止めてしまった")

    def test_bool_pid_is_not_a_pid(self):
        """isinstance(True, int) は True なので型を厳密に見る."""
        slot = SpeechSlot("s4c")
        slot.path.write_text(
            json.dumps({"pid": True, "pid_token": "1", "win_pid": True}), encoding="utf-8"
        )
        self.assertFalse(slot.busy())
        with mock.patch("cc_voicepeak.locking.taskkill") as killer:
            self.assertFalse(slot.interrupt())
        killer.assert_not_called()

    def test_pid_below_two_is_rejected(self):
        """0 / 負数はプロセスグループ全体、1 は init を指すので相手にしない."""
        for pid in (0, -1, 1):
            slot = SpeechSlot(f"s4d{pid}")
            slot.path.write_text(
                json.dumps({"pid": pid, "pid_token": "1"}), encoding="utf-8"
            )
            self.assertFalse(slot.busy(), f"pid={pid} を有効と見なした")

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

    def test_update_merges_fields(self):
        slot = SpeechSlot("s8")
        slot.write(chars=10)
        slot.update(win_pid=42)
        state = slot.read()
        self.assertEqual(state["win_pid"], 42)
        self.assertEqual(state["chars"], 10)

    def test_clear_keeps_another_process_entry(self):
        slot, _child = self.occupied_slot("s9")
        slot.clear()
        self.assertTrue(slot.path.exists(), "他プロセスの状態を消してはいけない")

    def test_state_file_is_private(self):
        slot = SpeechSlot("sA")
        slot.write()
        self.assertEqual(slot.path.stat().st_mode & 0o777, 0o600)

    def test_runtime_dir_is_private(self):
        self.assertEqual(runtime_dir().stat().st_mode & 0o777, 0o700)

    def test_runtime_dir_falls_back_to_state_home_not_tmp(self):
        """XDG_RUNTIME_DIR が無ければログと同じ $XDG_STATE_HOME 配下。/tmp は見ない."""
        shared = self.tmp / "shared-tmp"
        shared.mkdir()
        os.environ.pop("XDG_RUNTIME_DIR")
        os.environ["XDG_STATE_HOME"] = str(self.tmp / "state")
        os.environ["TMPDIR"] = str(shared)
        path = runtime_dir()
        self.assertEqual(path, self.tmp / "state" / "cc-voicepeak" / "run")
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertFalse((shared / "cc-voicepeak").exists())

    def test_runtime_dir_rejects_symlink(self):
        """他ユーザが先回りして置いたリンクを黙って使わない."""
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.tmp / "cc-voicepeak").symlink_to(elsewhere)
        with self.assertRaises(RuntimeDirError):
            runtime_dir()
        with self.assertRaises(RuntimeDirError):
            SpeechSlot("sX")
        with self.assertRaises(RuntimeDirError):
            ExeLock()

    def test_runtime_dir_rejects_foreign_owner(self):
        (self.tmp / "cc-voicepeak").mkdir(mode=0o700)
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(RuntimeDirError):
                runtime_dir()

    def test_temp_file_name_is_per_process(self):
        slot = SpeechSlot("sB")
        slot.write()
        leftovers = list(runtime_dir().glob("*.tmp"))
        self.assertEqual(leftovers, [], "一時ファイルが残っている")

    def test_wait_until_free_returns_true_when_free(self):
        self.assertTrue(SpeechSlot("sC").wait_until_free(timeout=0.3))

    def test_wait_until_free_times_out(self):
        slot = SpeechSlot("s7")
        with mock.patch.object(SpeechSlot, "busy", return_value=True):
            started = time.monotonic()
            self.assertFalse(slot.wait_until_free(timeout=0.3))
            self.assertLess(time.monotonic() - started, 5)


class ProcessTokenTest(SlotTestCase):
    def test_token_is_stable_for_the_same_process(self):
        self.assertEqual(process_token(os.getpid()), process_token(os.getpid()))

    def test_token_is_none_for_missing_process(self):
        self.assertIsNone(process_token(999999))

    def test_live_process_with_matching_token_is_busy(self):
        slot, _child = self.occupied_slot("t1")
        self.assertTrue(slot.busy())

    def test_reused_pid_is_treated_as_free(self):
        # 生きている PID でも、記録時の starttime と違えば別のプロセス
        slot, child = self.occupied_slot("t2", token="999999999")
        self.assertFalse(slot.busy())
        self.assertFalse(slot.interrupt())
        time.sleep(0.2)
        self.assertIsNone(child.poll(), "無関係なプロセスを止めてしまった")


class TaskkillTest(SlotTestCase):
    def test_non_zero_return_code_is_a_failure(self):
        completed = SimpleNamespace(returncode=128, stdout=b"", stderr=b"not found")
        with mock.patch("subprocess.run", return_value=completed):
            self.assertFalse(taskkill(4242))

    def test_zero_return_code_is_success(self):
        completed = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with mock.patch("subprocess.run", return_value=completed):
            self.assertTrue(taskkill(4242))

    def test_interrupt_reports_failed_taskkill(self):
        slot = SpeechSlot("t3")
        with mock.patch("cc_voicepeak.locking.taskkill", return_value=False):
            with mock.patch.object(
                SpeechSlot, "read", return_value={"pid": 999999, "win_pid": 4242}
            ):
                self.assertFalse(slot.interrupt())


class AcquireTest(SlotTestCase):
    def test_free_slot_is_acquired_with_fields(self):
        slot = SpeechSlot("a1")
        self.assertTrue(slot.acquire("replace", chars=12))
        state = slot.read()
        self.assertEqual(state["pid"], os.getpid())
        self.assertEqual(state["chars"], 12)

    def test_skip_gives_up(self):
        slot, child = self.occupied_slot("a2")
        self.assertFalse(slot.acquire("skip"))
        self.assertIsNone(child.poll(), "skip なのに前の読み上げを止めている")

    def test_replace_interrupts_previous(self):
        slot, child = self.occupied_slot("a3")
        self.assertTrue(slot.acquire("replace"))
        self.assertTrue(self.wait_for_exit(child), "前のプロセスが終了していない")
        self.assertEqual(slot.read()["pid"], os.getpid())

    def test_queue_times_out(self):
        slot, child = self.occupied_slot("a4")
        started = time.monotonic()
        self.assertFalse(slot.acquire("queue", timeout=0.3))
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(child.poll(), "queue なのに前の読み上げを止めている")

    def test_queue_waits_until_free(self):
        slot, _child = self.occupied_slot("a5")

        def release() -> None:
            time.sleep(0.3)
            slot.path.unlink()

        threading.Thread(target=release, daemon=True).start()
        self.assertTrue(slot.acquire("queue", timeout=10))
        self.assertEqual(slot.read()["pid"], os.getpid())


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

    def test_lock_timeout_raises(self):
        # 取得できないまま合成に進むと voicepeak が同時起動してしまう
        with ExeLock("t2.lock"):
            waiting = ExeLock("t2.lock", timeout=0.2)
            with self.assertRaises(LockTimeout):
                waiting.__enter__()
            self.assertFalse(waiting.acquired)
            self.assertIsNone(waiting._handle)

    def test_lock_is_released_after_use(self):
        lock = ExeLock("t3.lock", timeout=0.2)
        with lock:
            self.assertTrue(lock.acquired)
        self.assertFalse(lock.acquired)
        with ExeLock("t3.lock", timeout=0.2):
            pass
