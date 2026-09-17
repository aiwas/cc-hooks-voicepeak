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
from contextlib import contextmanager
from pathlib import Path

from .errors import LockTimeout, RuntimeDirError
from .fsutil import ensure_private_dir, xdg_state_home
from .logging_util import get_logger

log = get_logger("lock")


def runtime_dir() -> Path:
    """状態ファイルとロックの置き場所 (自分専用であることを保証する).

    ``$XDG_RUNTIME_DIR/cc-voicepeak`` を使い、無ければログと同じ
    ``$XDG_STATE_HOME/cc-voicepeak/run`` に落とす。``/tmp`` は使わない。
    全ユーザ共有の場所だと、他ユーザが先に同名のディレクトリを作って
    状態ファイルを置いたり ``voicepeak.lock`` を握り続けたりできる。
    :func:`~.fsutil.ensure_private_dir` の検査に通らなければ
    :class:`RuntimeDirError` にして続行しない。
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    path = Path(base) / "cc-voicepeak" if base else xdg_state_home() / "cc-voicepeak" / "run"
    try:
        return ensure_private_dir(path)
    except OSError as exc:
        raise RuntimeDirError(
            f"ランタイムディレクトリを安全に用意できません: {exc}\n"
            "XDG_RUNTIME_DIR か XDG_STATE_HOME で自分専用の場所を指定してください。"
        ) from exc


def _valid_pid(value: object) -> bool:
    """状態ファイルから読んだ値が PID として妥当か.

    ``isinstance(value, int)`` は ``bool`` も通すので型を厳密に見る。
    0 と負数は ``os.kill`` でプロセスグループ全体を指し、1 は init なので除く。
    """
    return type(value) is int and value > 1


class ExeLock:
    """voicepeak.exe の同時起動を防ぐファイルロック.

    取得できなかった場合に処理を続けると voicepeak が同時起動してしまうため、
    タイムアウトは :class:`LockTimeout` にする。
    """

    def __init__(self, name: str = "voicepeak.lock", timeout: float = 300.0):
        self.path = runtime_dir() / name
        self.timeout = timeout
        self.acquired = False
        self._handle = None

    def __enter__(self) -> ExeLock:
        self._handle = open(self.path, "a+")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    self._release()
                    raise
                if time.monotonic() > deadline:
                    self._release()
                    raise LockTimeout(
                        f"EXE ロックを {self.timeout:g} 秒待っても取得できませんでした: "
                        f"{self.path} / 他の読み上げが残っていないか確認してください。"
                    )
                time.sleep(0.1)
            else:
                self.acquired = True
                return self

    def __exit__(self, *_exc) -> None:
        self._release()

    def _release(self) -> None:
        if self._handle is None:
            return
        try:
            if self.acquired:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self.acquired = False


def pid_alive(pid: int) -> bool:
    """プロセスが生存しているか (他モジュールからも使う)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_token(pid: int) -> str | None:
    """``/proc/<pid>/stat`` の starttime. PID 再利用を見分けるために使う."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return None
    # comm フィールドは空白や ')' を含み得るので、最後の ')' 以降を見る。
    # 残りは stat の 3 番目 (state) から始まるので、22 番目の starttime は index 19。
    fields = raw.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else None


def _is_recorded_process(pid: int, token: str | None) -> bool:
    """状態ファイルに記録したプロセスが、いまも同じプロセスとして生きているか.

    ``token`` が無い状態ファイルは古いものとして扱う (現行の書き手は必ず付ける)。
    生存確認だけで信用すると、``{"pid": <他人の pid>}`` を置かれただけで
    そのプロセスグループへ割り込みの kill が飛ぶ。
    ``/proc`` を読めない場合は生存確認だけで妥協する。
    """
    if token is None or not pid_alive(pid):
        return False
    current = process_token(pid)
    return current is None or current == token


def _self_identity() -> dict:
    """状態ファイルに記録する「自分が何者か」."""
    pid = os.getpid()
    return {
        "pid": pid,
        "pid_token": process_token(pid),
        # killpg してよい相手かの判定に使う (下記 _may_kill_group)
        "pgid": os.getpgid(0),
        "sid": os.getsid(0),
    }


def _may_kill_group(pid: int, state: dict) -> bool:
    """``pid`` をプロセスグループごと止めてよいか.

    ``setsid`` したプロセスは自分だけの新しいセッションとプロセスグループを
    持つため ``pid == pgid == sid`` になる。デタッチした読み上げプロセスは
    この形なので、``killpg`` すれば子の voicepeak.exe やプレイヤも一緒に止まる。

    一方 ``--sync`` の hook は setsid されておらず、起動元 (Claude Code) と
    同じプロセスグループにいる。ここで ``killpg`` すると起動元ごと落ちるので、
    その場合は対象 1 プロセスだけを止める (子は ``win_pid`` の taskkill と
    プロセス終了に伴う後始末に委ねる)。

    判定は ``/proc`` の現在値で行い、記録値とも突き合わせる。状態ファイルは
    書き換えられ得るので、記録値だけを信用して ``killpg`` の可否を決めない。
    """
    try:
        pgid = os.getpgid(pid)
        sid = os.getsid(pid)
    except OSError:
        return False
    if pid != pgid or pid != sid:
        return False
    # 自分と同じグループなら、割り込む側まで巻き込むので許可しない
    try:
        if pgid == os.getpgid(0):
            return False
    except OSError:
        return False
    # 記録時と食い違っていれば、PID 再利用か状態ファイルの改変を疑う
    for key, value in (("pgid", pgid), ("sid", sid)):
        recorded = state.get(key)
        if recorded is not None and recorded != value:
            return False
    return True


def _signal_process(pid: int, sig: int, group: bool) -> bool:
    """``pid`` にシグナルを送る. 送れたら True."""
    if group:
        try:
            os.killpg(os.getpgid(pid), sig)
            return True
        except OSError:
            pass  # グループへ送れなければ単体で送る
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def taskkill(win_pid: int) -> bool:
    """Windows 側のプロセスを止める (WSL からの割り込み用). 止めたら True."""
    try:
        proc = subprocess.run(
            ["taskkill.exe", "/PID", str(win_pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            cwd="/mnt/c",
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("taskkill に失敗: %s", exc)
        return False
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
        log.debug("taskkill が異常終了しました (code=%d): %s", proc.returncode, detail[:200])
        return False
    return True


class SpeechSlot:
    """セッション単位で「いま読み上げ中のプロセス」を 1 つに保つ."""

    def __init__(self, key: str = "default"):
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)[:64]
        self.path = runtime_dir() / f"speech-{safe or 'default'}.json"

    # -- 状態の読み書き ----------------------------------------------------
    def read(self) -> dict | None:
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        if not _valid_pid(pid):
            return None
        if pid != os.getpid() and not _is_recorded_process(pid, data.get("pid_token")):
            return None
        return data

    def write(self, **fields) -> None:
        data = {**_self_identity(), "started": time.time()}
        data.update(fields)
        self._atomic_write(data)

    def update(self, **fields) -> None:
        current = self.read() or {}
        current.update(fields)
        current.update(_self_identity())
        self._atomic_write(current)

    def _atomic_write(self, data: dict) -> None:
        # 一時名にも PID を入れる。同じセッションキーの別プロセスと衝突すると、
        # 書きかけのファイルを読んで「スロット空き」と誤認される。
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.chmod(0o600)  # 読み上げの状態を他ユーザに見せない
            os.replace(tmp, self.path)
        except OSError as exc:
            log.debug("状態ファイルの書き込みに失敗: %s", exc)
            try:
                tmp.unlink()
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

        if type(win_pid) is int and win_pid > 0:
            killed = taskkill(win_pid)

        # PID 再利用で無関係なプロセスグループを止めないよう、記録時の
        # starttime と照合してから kill する (read() が妥当性を検査済み)
        if pid != os.getpid() and _is_recorded_process(pid, state.get("pid_token")):
            # setsid 済みのプロセスだけグループごと止める。同期経路の hook は
            # 起動元と同じグループにいるため、巻き込むと Claude Code が落ちる
            group = _may_kill_group(pid, state)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if not _signal_process(pid, sig, group):
                    break
                killed = True
                for _ in range(20):
                    if not pid_alive(pid):
                        break
                    time.sleep(0.05)
                if not pid_alive(pid):
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

    # -- 確保 --------------------------------------------------------------
    @contextmanager
    def _guard(self):
        """確保処理を直列化するロック (状態ファイルそのものは掴まない)."""
        lock_path = self.path.with_name(self.path.name + ".lock")
        handle = open(lock_path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    def acquire(self, policy: str = "replace", timeout: float = 600.0, **fields) -> bool:
        """スロットを確保する. できなければ False.

        空き確認と ``write()`` の間に別プロセスが割り込むと読み上げが重なるので、
        ``policy`` の処理ごとロックの中で行う。``policy`` は
        ``replace`` (前を止める) / ``queue`` (空くまで待つ) / ``skip`` (諦める)。
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._guard():
                if not self.busy():
                    self.write(**fields)
                    return True
                if policy == "skip":
                    return False
                if policy != "queue":
                    self.interrupt()
                    self.write(**fields)
                    return True
            # queue: ロックを離してから待つ (相手が clear できるようにする)
            if time.monotonic() > deadline:
                log.warning("前の読み上げが %.0f 秒で終わりませんでした", timeout)
                return False
            time.sleep(0.2)
