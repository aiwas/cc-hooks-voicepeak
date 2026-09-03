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
from pathlib import Path
from typing import List, Optional

from .bridge import Bridge
from .errors import PlayerError
from .logging_util import get_logger
from .wavutil import wav_duration

log = get_logger("player")

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
                f"powershell.exe を起動できません: {exc} / "
                "WSL interop が無効になっている可能性があります。"
            ) from exc

        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()
        self._pid_event.wait(timeout=20)
        log.debug("PowerShell プレイヤ起動 (win_pid=%s)", self._win_pid)

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
        if self.proc is None:
            return
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.write(b"__QUIT__\n")
                self.proc.stdin.flush()
                self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        wait_for = timeout if timeout is not None else self._total_seconds + 60.0
        try:
            self.proc.wait(timeout=max(wait_for, 10.0))
        except subprocess.TimeoutExpired:
            log.warning("再生プロセスが終わらないので停止します")
            self.stop()

    def stop(self) -> None:
        if self.proc is None:
            return
        from .locking import taskkill

        if self._win_pid:
            taskkill(self._win_pid)
        try:
            self.proc.kill()
        except OSError:
            pass
        self.proc = None


class CommandPlayer(Player):
    """WSL 側のコマンド (paplay / aplay / ffplay) で順次再生する."""

    def __init__(self, backend: str, volume: Optional[int] = None):
        self.name = backend
        self.volume = volume
        self._queue: "queue.Queue[Optional[Path]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._current: Optional[subprocess.Popen] = None
        self._stopped = threading.Event()

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
            try:
                self._current = subprocess.Popen(
                    self._command(item),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._current.wait()
            except OSError as exc:
                log.warning("再生に失敗しました (%s): %s", self.name, exc)
            finally:
                self._current = None

    def enqueue(self, path: Path) -> None:
        self.start()
        self._queue.put(Path(path))

    def finish(self, timeout: Optional[float] = None) -> None:
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def stop(self) -> None:
        self._stopped.set()
        current = self._current
        if current is not None:
            try:
                current.kill()
            except OSError:
                pass
        self._queue.put(None)


def select_player(backend: str, bridge: Bridge, volume: Optional[int] = None) -> Player:
    """設定値と環境から再生バックエンドを決める."""
    if backend == "none":
        return NullPlayer()
    if backend == "powershell":
        return PowershellPlayer(bridge)
    if backend in ("paplay", "aplay", "ffplay"):
        if shutil.which(backend) is None:
            raise PlayerError(f"{backend} が見つかりません")
        return CommandPlayer(backend, volume)

    # auto
    if bridge.name == "wsl":
        return PowershellPlayer(bridge)
    for candidate in ("paplay", "aplay", "ffplay"):
        if shutil.which(candidate):
            return CommandPlayer(candidate, volume)
    log.warning("再生コマンドが見つからないので合成のみ行います")
    return NullPlayer()
