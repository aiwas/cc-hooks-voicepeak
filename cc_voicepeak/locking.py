"""排他制御.

2 つの役割がある。

* **EXE の直列化** (:class:`ExeLock`)
  voicepeak は複数インスタンスの同時起動ができないため、合成 1 回ごとに
  ホスト全体でロックを取る。
* **読み上げセッションの管理** (:class:`SpeechSlot`)
  タスクが立て続けに終わったときに、前の読み上げへ割り込む/待つ/捨てるを制御する。
  Windows 側の再生プロセス (powershell) の PID も記録し、割り込み時に
  ``taskkill.exe`` で確実に止める。
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from .logging_util import get_logger

log = get_logger("lock")


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    path = Path(base) / "cc-voicepeak"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ExeLock:
    """voicepeak.exe の同時起動を防ぐファイルロック."""

    def __init__(self, name: str = "voicepeak.lock", timeout: float = 300.0):
        self.path = runtime_dir() / name
        self.timeout = timeout
        self._handle = None

    def __enter__(self) -> "ExeLock":
        self._handle = open(self.path, "a+")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() > deadline:
                    log.warning("EXE ロックの取得がタイムアウトしました: %s", self.path)
                    return self
                time.sleep(0.1)

    def __exit__(self, *_exc) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def taskkill(win_pid: int) -> None:
    """Windows 側のプロセスを止める (WSL からの割り込み用)."""
    try:
        subprocess.run(
            ["taskkill.exe", "/PID", str(win_pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            cwd="/mnt/c",
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("taskkill に失敗: %s", exc)


class SpeechSlot:
    """セッション単位で「いま読み上げ中のプロセス」を 1 つに保つ."""

    def __init__(self, key: str = "default"):
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)[:64]
        self.path = runtime_dir() / f"speech-{safe or 'default'}.json"

    # -- 状態の読み書き ----------------------------------------------------
    def read(self) -> Optional[dict]:
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        if isinstance(pid, int) and pid != os.getpid() and not _pid_alive(pid):
            return None
        return data

    def write(self, **fields) -> None:
        data = {"pid": os.getpid(), "started": time.time()}
        data.update(fields)
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.debug("状態ファイルの書き込みに失敗: %s", exc)

    def update(self, **fields) -> None:
        current = self.read() or {}
        current.update(fields)
        current["pid"] = os.getpid()
        try:
            self.path.write_text(json.dumps(current), encoding="utf-8")
        except OSError:
            pass

    def clear(self) -> None:
        current = self.read()
        if current and current.get("pid") not in (None, os.getpid()):
            return
        try:
            self.path.unlink()
        except OSError:
            pass

    # -- 割り込み ----------------------------------------------------------
    def interrupt(self) -> bool:
        """前の読み上げプロセスを止める. 止めた場合 True."""
        state = self.read()
        if not state:
            return False
        pid = state.get("pid")
        win_pid = state.get("win_pid")
        killed = False

        if isinstance(win_pid, int):
            taskkill(win_pid)
            killed = True

        if isinstance(pid, int) and pid != os.getpid() and _pid_alive(pid):
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(pid), sig)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(pid, sig)
                    except (ProcessLookupError, PermissionError):
                        break
                killed = True
                for _ in range(20):
                    if not _pid_alive(pid):
                        break
                    time.sleep(0.05)
                if not _pid_alive(pid):
                    break

        if killed:
            log.info("前の読み上げに割り込みました (pid=%s win_pid=%s)", pid, win_pid)
        return killed

    def busy(self) -> bool:
        return self.read() is not None

    def wait_until_free(self, timeout: float = 600.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.busy():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.2)
        return True
