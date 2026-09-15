"""wav の再生.

WSL には既定でサウンドデバイスが無いので、既定では **Windows 側の PowerShell**
に再生させる。``System.Media.SoundPlayer`` を常駐させて標準入力から wav のパスを
1 行ずつ受け取り ``PlaySync()`` する形にしてあるので、

* powershell.exe の起動コスト (0.5 秒程度) を 1 回で済ませられる
* 合成できたブロックから順に流し込める (= 初音までが速い)
* 自分の ``$PID`` を最初に返させておけば ``taskkill.exe`` で確実に止められる

という 3 つを同時に満たせる。WSLg で PulseAudio が使える環境なら
``player.backend`` に ``paplay`` / ``aplay`` / ``ffplay`` も選べる。
"""

from __future__ import annotations

import base64
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional

from .bridge import Bridge
from .errors import PlayerError
from .logging_util import get_logger
from .wavutil import wav_duration

log = get_logger("player")

# 常駐プレイヤが PID を返すまでの待ち時間
PID_WAIT_TIMEOUT = 20.0
# 再生 1 件あたりの打ち切り時間に上乗せする余裕 (秒)
PLAYBACK_TIMEOUT_MARGIN = 30.0

# PATH に無いときに見に行く既定のインストール先
POWERSHELL_FALLBACKS = (
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/Program Files/PowerShell/7/pwsh.exe",
)

# 常駐プレイヤ (PowerShell)
_PS_SCRIPT = r"""
$ErrorActionPreference = 'Continue'
try { [Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
[Console]::Out.WriteLine("PID " + $PID)
[Console]::Out.Flush()
$player = New-Object System.Media.SoundPlayer
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq '') { continue }
    if ($line -eq '__QUIT__') { break }
    try {
        $player.SoundLocation = $line
        $player.Load()
        $player.PlaySync()
        [Console]::Out.WriteLine("PLAYED " + $line)
    } catch {
        [Console]::Out.WriteLine("ERROR " + $_.Exception.Message)
    }
    [Console]::Out.Flush()
}
"""


def _encoded_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


class Player:
    """再生バックエンドの共通インターフェース."""

    name = "base"

    def start(self) -> None:
        pass

    def enqueue(self, path: Path) -> None:
        raise NotImplementedError

    def finish(self, timeout: Optional[float] = None) -> None:
        pass

    def stop(self) -> None:
        pass

    @property
    def win_pid(self) -> Optional[int]:
        return None


class NullPlayer(Player):
    """再生しない (合成のみ / テスト用)."""

    name = "none"

    def __init__(self) -> None:
        self.played: List[Path] = []

    def enqueue(self, path: Path) -> None:
        self.played.append(Path(path))
        log.debug("再生スキップ: %s", path)


class PowershellPlayer(Player):
    """Windows 側の PowerShell に常駐させて順次再生させる."""

    name = "powershell"

    def __init__(self, bridge: Bridge, executable: str = "powershell.exe"):
        self.bridge = bridge
        self.executable = executable
        self.proc: Optional[subprocess.Popen] = None
        self._win_pid: Optional[int] = None
        self._pid_event = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._queued = 0
        self._total_seconds = 0.0

    def start(self) -> None:
        if self.proc is not None:
            return
        command = [
            self.executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            _encoded_command(_PS_SCRIPT),
        ]
        try:
            self.proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.bridge.exec_cwd(),
                bufsize=0,
            )
        except OSError as exc:
            raise PlayerError(
                f"{self.executable} を起動できません: {exc} / "
                "WSL interop が無効になっている可能性があります。"
            ) from exc

        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()
        try:
            self._await_pid(PID_WAIT_TIMEOUT)
        except PlayerError:
            self._release(self.proc)  # 起動に失敗してもパイプは片付ける
            raise
        log.debug("PowerShell プレイヤ起動 (win_pid=%s)", self._win_pid)

    def _await_pid(self, timeout: float) -> None:
        """常駐プレイヤが返す PID 行を待つ.

        PID が取れないと ``taskkill`` による割り込みができなくなる。
        起動に失敗した場合まで待ち続けないよう、待機中もプロセスの生存を見る。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._pid_event.wait(0.1):
                return
            if self.proc is not None and self.proc.poll() is not None:
                raise PlayerError(
                    f"{self.executable} が起動直後に終了しました / "
                    "WSL interop が無効になっている可能性があります。"
                )
        log.warning(
            "再生プロセスの PID を %.0f 秒で取得できませんでした (割り込みができません)",
            timeout,
        )

    def _read_output(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("PID "):
                try:
                    self._win_pid = int(line[4:].strip())
                except ValueError:
                    pass
                self._pid_event.set()
            elif line.startswith("ERROR "):
                log.warning("再生エラー: %s", line[6:])
            else:
                log.debug("player: %s", line)

    @property
    def win_pid(self) -> Optional[int]:
        return self._win_pid

    def enqueue(self, path: Path) -> None:
        if self.proc is None:
            self.start()
        assert self.proc is not None
        if self.proc.stdin is None or self.proc.poll() is not None:
            raise PlayerError("再生プロセスが終了しています")
        win_path = self.bridge.to_win(path)
        try:
            self.proc.stdin.write((win_path + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise PlayerError(f"再生プロセスへの書き込みに失敗しました: {exc}") from exc
        self._queued += 1
        self._total_seconds += wav_duration(Path(path))

    def finish(self, timeout: Optional[float] = None) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.write(b"__QUIT__\n")
                proc.stdin.flush()
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        wait_for = timeout if timeout is not None else self._total_seconds + 60.0
        try:
            proc.wait(timeout=max(wait_for, 10.0))
        except subprocess.TimeoutExpired:
            log.warning("再生プロセスが終わらないので停止します")
            self.stop()
            return
        self._release(proc)

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        from .locking import taskkill

        if self._win_pid:
            taskkill(self._win_pid)
        try:
            proc.kill()
        except OSError:
            pass
        self._release(proc)

    def _release(self, proc: subprocess.Popen) -> None:
        """終了を待ち切り、パイプと reader スレッドを片付ける.

        wait() を呼ばずに参照を捨てるとゾンビが残り、stdin/stdout の fd も
        リークする。reader スレッドは stdout の EOF で抜けるので、
        close() より先に join する。
        """
        self.proc = None
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill 後に残る異常系
            log.warning("再生プロセスが終了しません (pid=%s)", proc.pid)
        if self._reader is not None:
            self._reader.join(timeout=5)
            self._reader = None
        for stream in (proc.stdin, proc.stdout):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:  # pragma: no cover - 閉じ済みなら無視
                pass
        self._pid_event.clear()


class CommandPlayer(Player):
    """WSL 側のコマンド (paplay / aplay / ffplay) で順次再生する."""

    def __init__(self, backend: str, volume: Optional[int] = None):
        self.name = backend
        self.volume = volume
        self._queue: "queue.Queue[Optional[Path]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._current: Optional[subprocess.Popen] = None
        self._stopped = threading.Event()
        self._total_seconds = 0.0

    def _command(self, path: Path) -> List[str]:
        if self.name == "paplay":
            command = ["paplay"]
            if self.volume is not None:
                command += ["--volume", str(int(65536 * min(max(self.volume, 0), 100) / 100))]
            return command + [str(path)]
        if self.name == "aplay":
            return ["aplay", "-q", str(path)]
        if self.name == "ffplay":
            command = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
            if self.volume is not None:
                command += ["-volume", str(self.volume)]
            return command + [str(path)]
        raise PlayerError(f"未知の再生バックエンドです: {self.name}")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        while not self._stopped.is_set():
            item = self._queue.get()
            if item is None:
                break
            # aplay がデバイス待ちで止まると常駐プロセスが永久に残るので、
            # 再生時間から上限を決めて打ち切る
            limit = wav_duration(item) * 2 + PLAYBACK_TIMEOUT_MARGIN
            try:
                self._current = subprocess.Popen(
                    self._command(item),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._current.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                log.warning("再生が %.0f 秒で終わりません (%s)。停止します", limit, self.name)
                self._kill_current()
            except Exception as exc:  # noqa: BLE001 - ワーカを静かに終わらせない
                log.warning("再生に失敗しました (%s): %s", self.name, exc)
            finally:
                self._current = None

    def _kill_current(self) -> None:
        current = self._current
        if current is None:
            return
        try:
            current.kill()
            current.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - 異常系
            pass

    def enqueue(self, path: Path) -> None:
        self.start()
        if self._thread is not None and not self._thread.is_alive():
            raise PlayerError(f"再生スレッドが停止しています ({self.name})")
        self._queue.put(Path(path))
        self._total_seconds += wav_duration(Path(path))

    def finish(self, timeout: Optional[float] = None) -> None:
        self._queue.put(None)
        thread = self._thread
        if thread is None:
            return
        wait_for = timeout if timeout is not None else self._total_seconds + 60.0
        thread.join(timeout=max(wait_for, 10.0))
        if thread.is_alive():
            log.warning("再生スレッドが終わらないので停止します (%s)", self.name)
            self.stop()
            thread.join(timeout=5)
        self._thread = None

    def stop(self) -> None:
        self._stopped.set()
        self._kill_current()
        self._queue.put(None)


def find_powershell() -> Optional[str]:
    """PowerShell の実行ファイルを探す.

    ``/etc/wsl.conf`` で ``appendWindowsPath=false`` にしていると PATH から
    ``powershell.exe`` が消えるため、既定のインストール先も見に行く。
    """
    for name in ("powershell.exe", "pwsh.exe"):
        found = shutil.which(name)
        if found:
            return found
    for path in POWERSHELL_FALLBACKS:
        if Path(path).is_file():
            return path
    return None


def select_player(backend: str, bridge: Bridge, volume: Optional[int] = None) -> Player:
    """設定値と環境から再生バックエンドを決める."""
    if backend == "none":
        return NullPlayer()
    if backend == "powershell":
        found = find_powershell()
        if found is None:
            raise PlayerError(
                "powershell.exe が見つかりません / "
                "/etc/wsl.conf の [interop] appendWindowsPath=true を確認してください。"
            )
        return PowershellPlayer(bridge, executable=found)
    if backend in ("paplay", "aplay", "ffplay"):
        if shutil.which(backend) is None:
            raise PlayerError(f"{backend} が見つかりません")
        return CommandPlayer(backend, volume)

    # auto
    if bridge.name == "wsl":
        found = find_powershell()
        if found:
            if volume is not None:
                log.warning(
                    "player.volume は powershell バックエンドでは効きません "
                    "(SoundPlayer に音量の API が無いため)"
                )
            return PowershellPlayer(bridge, executable=found)
        # 見つからないまま powershell を選ぶと、合成だけ進んで無音になる
        log.warning(
            "powershell.exe が見つかりません "
            "(/etc/wsl.conf の [interop] appendWindowsPath=true を確認してください)"
        )
    for candidate in ("paplay", "aplay", "ffplay"):
        if shutil.which(candidate):
            return CommandPlayer(candidate, volume)
    log.warning("再生コマンドが見つからないので合成のみ行います")
    return NullPlayer()
