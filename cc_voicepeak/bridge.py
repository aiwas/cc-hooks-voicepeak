"""WSL から Windows 側の EXE を叩くためのブリッジ.

WSL で Windows の実行ファイルを動かす際の落とし穴は以下の 4 つ。

1. **WSL interop が必要**
   ``/proc/sys/fs/binfmt_misc/WSLInterop`` (または ``WSLInterop-late``) が
   登録されていないと ``.exe`` を直接実行できない。``/etc/wsl.conf`` の
   ``[interop] enabled=true`` が前提。
2. **引数のパスは Windows 形式**
   ``-o /home/user/a.wav`` は Windows 側から見えない。``wslpath -w`` で
   ``\\\\wsl.localhost\\...`` か ``C:\\...`` に変換して渡す。
3. **カレントディレクトリ**
   WSL 側のパスを cwd にして Windows EXE を起動すると
   "UNC パスはサポートされません" と警告され cwd が ``C:\\Windows`` に
   差し替わる。cwd は必ず ``/mnt/<drive>/...`` 配下にする。
4. **出力先は Windows ファイルシステムに置く**
   9p 経由 (``\\\\wsl.localhost``) への書き込みは遅く、環境によっては失敗する。
   wav は Windows の ``%TEMP%`` 配下に出す。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .errors import BridgeError

_MNT_RE = re.compile(r"^/mnt/([a-zA-Z])(/.*)?$")

VOICEPEAK_EXE_CANDIDATES = (
    "Program Files/VOICEPEAK/voicepeak.exe",
    "Program Files/AHS/VOICEPEAK/voicepeak.exe",
    "Program Files (x86)/VOICEPEAK/voicepeak.exe",
    "Program Files (x86)/AHS/VOICEPEAK/voicepeak.exe",
)
VOICEPEAK_USER_CANDIDATES = (
    "AppData/Local/Programs/VOICEPEAK/voicepeak.exe",
    "AppData/Local/VOICEPEAK/voicepeak.exe",
)


def is_wsl() -> bool:
    """WSL 上で動いているか."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/sys/kernel/osrelease", encoding="utf-8", errors="replace") as handle:
            return "microsoft" in handle.read().lower()
    except OSError:
        return False


def interop_enabled() -> bool:
    """``.exe`` の直接実行 (binfmt_misc 経由) が有効か."""
    for name in ("WSLInterop", "WSLInterop-late"):
        path = Path("/proc/sys/fs/binfmt_misc") / name
        try:
            if path.is_file() and "enabled" in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


class Bridge:
    """パス変換と実行環境を抽象化する基底クラス."""

    name = "base"

    def to_win(self, path: os.PathLike | str) -> str:
        raise NotImplementedError

    def to_linux(self, win_path: str) -> Path:
        raise NotImplementedError

    def temp_root(self) -> Path:
        """wav の書き出し先 (Linux 側から見たパス)."""
        raise NotImplementedError

    def exec_cwd(self) -> str:
        """Windows EXE を起動するときの cwd (Linux 側から見たパス)."""
        raise NotImplementedError

    def find_voicepeak(self) -> Optional[Path]:
        raise NotImplementedError

    def popen_env(self) -> Dict[str, str]:
        return dict(os.environ)


class LocalBridge(Bridge):
    """パス変換なしでそのまま実行するブリッジ.

    テストや、Linux ネイティブの voicepeak / ダミー実装を叩く場合に使う。
    """

    name = "local"

    def __init__(self, temp_root: Optional[Path] = None, exe: Optional[Path] = None):
        env_root = os.environ.get("CC_VOICEPEAK_TEMP")
        self._temp = Path(
            temp_root
            or env_root
            or (Path(os.environ.get("TMPDIR", "/tmp")) / "cc-voicepeak")
        )
        self._exe = Path(exe) if exe else None

    def to_win(self, path: os.PathLike | str) -> str:
        return str(path)

    def to_linux(self, win_path: str) -> Path:
        return Path(win_path)

    def temp_root(self) -> Path:
        return self._temp

    def exec_cwd(self) -> str:
        return str(self._temp)

    def find_voicepeak(self) -> Optional[Path]:
        if self._exe and self._exe.exists():
            return self._exe
        found = shutil.which("voicepeak")
        return Path(found) if found else None


class WslBridge(Bridge):
    """WSL から Windows 側の EXE を叩くブリッジ."""

    name = "wsl"

    def __init__(self) -> None:
        self._win_cache: Dict[str, str] = {}
        self._temp_root: Optional[Path] = None

    # -- パス変換 ---------------------------------------------------------
    def to_win(self, path: os.PathLike | str) -> str:
        raw = str(path)
        if re.match(r"^[a-zA-Z]:[\\/]", raw) or raw.startswith("\\\\"):
            return raw.replace("/", "\\")
        cached = self._win_cache.get(raw)
        if cached:
            return cached

        match = _MNT_RE.match(os.path.normpath(raw))
        if match:
            drive = match.group(1).upper()
            rest = (match.group(2) or "").replace("/", "\\")
            result = f"{drive}:{rest}" if rest else f"{drive}:\\"
        else:
            result = self._wslpath("-w", raw)
        self._win_cache[raw] = result
        return result

    def to_linux(self, win_path: str) -> Path:
        match = re.match(r"^([a-zA-Z]):[\\/](.*)$", win_path)
        if match:
            drive = match.group(1).lower()
            rest = match.group(2).replace("\\", "/")
            return Path(f"/mnt/{drive}/{rest}")
        return Path(self._wslpath("-u", win_path))

    def _wslpath(self, flag: str, value: str) -> str:
        try:
            proc = subprocess.run(
                ["wslpath", flag, value],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BridgeError(f"wslpath の実行に失敗しました: {exc}") from exc
        if proc.returncode != 0:
            raise BridgeError(
                f"wslpath {flag} {value} が失敗しました: {proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    # -- 一時ディレクトリ --------------------------------------------------
    def exec_cwd(self) -> str:
        root = self.temp_root()
        if str(root).startswith("/mnt/"):
            return str(root)
        return "/mnt/c"

    def temp_root(self) -> Path:
        if self._temp_root is not None:
            return self._temp_root

        override = os.environ.get("CC_VOICEPEAK_TEMP")
        if override:
            self._temp_root = Path(override)
            return self._temp_root

        for candidate in self._windows_temp_candidates():
            if candidate and candidate.is_dir():
                self._temp_root = candidate / "cc-voicepeak"
                return self._temp_root

        # 最後の手段: WSL 側 (\\wsl.localhost 経由になるので遅い)
        self._temp_root = Path(os.environ.get("TMPDIR", "/tmp")) / "cc-voicepeak"
        return self._temp_root

    def _windows_temp_candidates(self) -> List[Optional[Path]]:
        candidates: List[Optional[Path]] = []
        cached = _read_cached_wintemp()
        if cached:
            candidates.append(cached)

        env_temp = self._query_windows_env("TEMP")
        if env_temp:
            path = self.to_linux(env_temp)
            _write_cached_wintemp(path)
            candidates.append(path)

        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if user:
            candidates.append(Path(f"/mnt/c/Users/{user}/AppData/Local/Temp"))
        candidates.append(Path("/mnt/c/Windows/Temp"))
        return candidates

    def _query_windows_env(self, name: str) -> Optional[str]:
        try:
            proc = subprocess.run(
                ["cmd.exe", "/d", "/c", f"echo %{name}%"],
                capture_output=True,
                text=True,
                timeout=20,
                cwd="/mnt/c",
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = proc.stdout.strip().splitlines()
        if not value:
            return None
        result = value[-1].strip()
        if not result or result.startswith("%"):
            return None
        return result

    # -- voicepeak.exe の探索 ---------------------------------------------
    def find_voicepeak(self) -> Optional[Path]:
        drives = [Path(f"/mnt/{letter}") for letter in ("c", "d", "e")]
        for drive in drives:
            if not drive.is_dir():
                continue
            for rel in VOICEPEAK_EXE_CANDIDATES:
                candidate = drive / rel
                if candidate.is_file():
                    return candidate

        users = Path("/mnt/c/Users")
        if users.is_dir():
            try:
                entries = sorted(users.iterdir())
            except OSError:
                entries = []
            for home in entries:
                if home.name in ("Public", "Default", "Default User", "All Users"):
                    continue
                for rel in VOICEPEAK_USER_CANDIDATES:
                    candidate = home / rel
                    if candidate.is_file():
                        return candidate
        return None


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "cc-voicepeak"


def _read_cached_wintemp() -> Optional[Path]:
    path = _cache_dir() / "wintemp"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(value) if value else None


def _write_cached_wintemp(value: Path) -> None:
    try:
        directory = _cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "wintemp").write_text(str(value), encoding="utf-8")
    except OSError:
        pass


def detect_bridge(force: Optional[str] = None) -> Bridge:
    """環境から適切なブリッジを選ぶ.

    ``force`` に ``"wsl"`` / ``"local"`` を渡すと固定できる
    (環境変数 ``CC_VOICEPEAK_BRIDGE`` でも指定可)。
    """
    mode = force or os.environ.get("CC_VOICEPEAK_BRIDGE") or "auto"
    if mode == "wsl":
        return WslBridge()
    if mode == "local":
        return LocalBridge()
    if mode != "auto":
        raise BridgeError(f"未知の bridge 指定です: {mode!r}")
    return WslBridge() if is_wsl() else LocalBridge()


def resolve_exe(bridge: Bridge, configured: Optional[str]) -> Path:
    """設定または自動探索で voicepeak の実体パスを決める."""
    if configured:
        raw = str(configured)
        path = bridge.to_linux(raw) if re.match(r"^[a-zA-Z]:[\\/]", raw) else Path(raw)
        if path.is_file():
            return path
        raise BridgeError(
            f"voicepeak.exe が見つかりません: {configured}\n"
            "voicepeak.exe の場所を設定 voicepeak.exe か環境変数 CC_VOICEPEAK_EXE で指定してください。"
        )

    found = bridge.find_voicepeak()
    if found:
        return found
    raise BridgeError(
        "voicepeak.exe を自動検出できませんでした。\n"
        "例: CC_VOICEPEAK_EXE='/mnt/c/Program Files/VOICEPEAK/voicepeak.exe'"
    )
