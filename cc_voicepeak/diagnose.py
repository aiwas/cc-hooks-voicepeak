"""環境診断 (``cc-voicepeak check``).

WSL から Windows の EXE を叩く構成は失敗ポイントが多いので、どこで詰まっている
のかを 1 コマンドで切り分けられるようにしてある。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .bridge import Bridge, detect_bridge, interop_enabled, is_wsl, resolve_exe
from .config import Config, unknown_keys
from .errors import CcVoicepeakError
from .player import find_powershell

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class CheckItem:
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    def line(self) -> str:
        mark = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}[self.status]
        text = f"{mark} {self.name}"
        if self.detail:
            text += f": {self.detail}"
        if self.hint and self.status != OK:
            text += f"\n       → {self.hint}"
        return text


_APT_PACKAGES = {"paplay": "pulseaudio-utils", "aplay": "alsa-utils", "ffplay": "ffmpeg"}


def _command_player_check(backend: str) -> CheckItem:
    found = shutil.which(backend)
    return CheckItem(
        f"再生 ({backend})",
        OK if found else FAIL,
        found or "見つかりません",
        f"apt install {_APT_PACKAGES.get(backend, backend)}",
    )


def _player_check(backend: str, bridge: Bridge, volume: Optional[int] = None) -> CheckItem:
    """``player.backend`` の設定に対して、実際に使える再生手段があるかを見る."""
    if backend == "none":
        return CheckItem("再生 (none)", OK, "再生せず合成だけ行います")
    if backend in ("paplay", "aplay", "ffplay"):
        return _command_player_check(backend)

    if backend == "powershell" or (backend == "auto" and bridge.name == "wsl"):
        found = find_powershell()
        if found:
            if volume is not None:
                # SoundPlayer には音量の API が無い
                return CheckItem(
                    "再生 (powershell.exe)",
                    WARN,
                    f"{found} (player.volume は無視されます)",
                    "音量を変えるには player.backend を paplay か ffplay にしてください",
                )
            return CheckItem("再生 (powershell.exe)", OK, found)
        if backend == "powershell":
            return CheckItem(
                "再生 (powershell.exe)",
                FAIL,
                "見つかりません",
                "/etc/wsl.conf の [interop] appendWindowsPath=true を確認してください",
            )
        # auto なので WSL 側のコマンドへ落ちる。どれも無ければ無音になる

    for candidate in ("paplay", "aplay", "ffplay"):
        found = shutil.which(candidate)
        if found:
            return CheckItem(f"再生 (auto → {candidate})", OK, found)
    return CheckItem(
        "再生 (auto)",
        WARN,
        "使える再生コマンドがありません (合成のみ行います)",
        "apt install pulseaudio-utils で paplay を入れるか player.backend を設定してください",
    )


def run_checks(cfg: Config, do_synth: bool = False) -> List[CheckItem]:
    items: List[CheckItem] = []
    bridge: Optional[Bridge] = None

    # 1. 実行環境
    if is_wsl():
        distro = os.environ.get("WSL_DISTRO_NAME", "?")
        items.append(CheckItem("WSL 環境", OK, f"WSL_DISTRO_NAME={distro}"))
        if interop_enabled():
            items.append(CheckItem("WSL interop", OK, "binfmt_misc に登録済み"))
        else:
            items.append(
                CheckItem(
                    "WSL interop",
                    FAIL,
                    "/proc/sys/fs/binfmt_misc/WSLInterop が有効ではありません",
                    "/etc/wsl.conf に [interop] enabled=true / appendWindowsPath=true "
                    "を書いて wsl --shutdown してください",
                )
            )
        if shutil.which("wslpath"):
            items.append(CheckItem("wslpath", OK))
        else:
            items.append(CheckItem("wslpath", WARN, "見つかりません", "パス変換は /mnt 決め打ちになります"))
    else:
        items.append(
            CheckItem(
                "WSL 環境",
                WARN,
                "WSL ではありません (Linux ネイティブとして動作します)",
                "Windows の voicepeak.exe を使う場合は WSL 上で実行してください",
            )
        )

    # 2. ブリッジと一時ディレクトリ
    try:
        bridge = detect_bridge()
        items.append(CheckItem("ブリッジ", OK, bridge.name))
    except CcVoicepeakError as exc:
        items.append(CheckItem("ブリッジ", FAIL, str(exc)))
        return items

    temp_root: Optional[Path] = None
    try:
        # Linux 側へ落ちる場合は temp_root() 自身が所有者と権限を検査する
        temp_root = bridge.temp_root()
        temp_root.mkdir(parents=True, exist_ok=True)
        probe = temp_root / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        on_windows_fs = str(temp_root).startswith("/mnt/")
        items.append(
            CheckItem(
                "作業ディレクトリ",
                OK if on_windows_fs or bridge.name != "wsl" else WARN,
                str(temp_root),
                "Windows 側 (/mnt/c/...) に置くと wav の書き出しが速く確実です。"
                "CC_VOICEPEAK_TEMP で変更できます",
            )
        )
    except (OSError, CcVoicepeakError) as exc:
        items.append(
            CheckItem(
                "作業ディレクトリ",
                FAIL,
                f"{temp_root}: {exc}" if temp_root else str(exc),
                "CC_VOICEPEAK_TEMP を設定してください",
            )
        )

    if bridge.name == "wsl" and temp_root is not None:
        try:
            win = bridge.to_win(temp_root)
            items.append(CheckItem("パス変換", OK, f"{temp_root} -> {win}"))
        except CcVoicepeakError as exc:
            items.append(CheckItem("パス変換", FAIL, str(exc)))

    # 3. voicepeak
    exe: Optional[Path] = None
    try:
        exe = resolve_exe(bridge, cfg.get("voicepeak.exe"))
        items.append(CheckItem("voicepeak", OK, str(exe)))
    except CcVoicepeakError as exc:
        items.append(
            CheckItem(
                "voicepeak",
                FAIL,
                str(exc).splitlines()[0],
                "CC_VOICEPEAK_EXE か設定 voicepeak.exe でフルパスを指定してください",
            )
        )

    # 4. 再生系
    backend = str(cfg.get("player.backend", "auto"))
    player_item = _player_check(backend, bridge, cfg.get("player.volume"))
    items.append(player_item)
    if do_synth and player_item.status == OK and player_item.name.startswith("再生 (powershell"):
        # 実際に 1 回起動してみる (--synth のときだけ。起動に 1 秒ほどかかる)
        alive = powershell_available()
        items.append(
            CheckItem(
                "PowerShell 実行",
                OK if alive else FAIL,
                "起動を確認しました" if alive else "起動できません",
                "WSL interop が有効か (cat /proc/sys/fs/binfmt_misc/WSLInterop) 確認してください",
            )
        )

    # 5. 設定
    if cfg.sources:
        items.append(
            CheckItem("設定ファイル", OK, ", ".join(str(path) for path in cfg.sources))
        )
    else:
        items.append(CheckItem("設定ファイル", WARN, "未検出 (既定値で動作します)"))

    unknown = unknown_keys(cfg)
    if unknown:
        items.append(
            CheckItem(
                "設定キー",
                WARN,
                ", ".join(unknown),
                "既定値に無いキーです。綴り誤りの可能性があります",
            )
        )

    if cfg.get("voicepeak.narrator"):
        items.append(CheckItem("ナレーター", OK, str(cfg.get("voicepeak.narrator"))))
    else:
        items.append(
            CheckItem(
                "ナレーター",
                WARN,
                "未指定 (voicepeak の既定が使われます)",
                "cc-voicepeak narrators で一覧を確認できます",
            )
        )

    # 6. 実際に合成してみる
    if do_synth and exe is not None:
        from .pipeline import speak

        try:
            report = speak("接続テストです。", cfg, bridge=bridge)
            status = OK if report.ok_count else FAIL
            items.append(
                CheckItem("合成テスト", status, report.summary() or "; ".join(report.errors))
            )
        except CcVoicepeakError as exc:
            items.append(CheckItem("合成テスト", FAIL, str(exc)))

    return items


def wsl_notes() -> str:
    """WSL 固有の注意点をまとめたテキスト."""
    return """\
WSL から Windows の voicepeak.exe を叩くときの要点
--------------------------------------------------
1. interop を有効にする (/etc/wsl.conf)
     [interop]
     enabled=true
     appendWindowsPath=true
   変更後は PowerShell で `wsl --shutdown` してから入り直す。

2. EXE は Linux パスのまま起動できる
     /mnt/c/Program\\ Files/VOICEPEAK/voicepeak.exe --help

3. 引数のパスは Windows 形式に変換する
     wslpath -w /mnt/c/tmp/a.wav   ->  C:\\tmp\\a.wav
   /mnt/<drive>/ 配下なら文字列置換で足りるので、このツールでは
   wslpath の呼び出しを省略して高速化している。

4. cwd は必ず /mnt/... 配下にする
   WSL 側のパスを cwd にすると "UNC パスはサポートされません" と警告され、
   cwd が C:\\Windows に差し替わる。-o を相対パスにしていると事故る。

5. wav は Windows ファイルシステム上に出す
   \\\\wsl.localhost\\ 経由の書き込みは遅く、失敗することもある。
   既定では Windows の %TEMP%\\cc-voicepeak を使う。見つからなければ
   ~/.cache/cc-voicepeak/work (自分専用・0700) に落とす。/tmp は使わない。

6. 音を鳴らすのも Windows 側にやらせる
   WSL には既定でサウンドデバイスが無い。powershell.exe の
   System.Media.SoundPlayer を常駐させて標準入力から wav パスを流し込む。
   WSLg で PulseAudio が使える環境なら paplay も選べる。

7. 止めるときは taskkill.exe
   Linux 側のプロセスを kill しても Windows 側が残ることがあるため、
   起動時に $PID を受け取っておき taskkill.exe /PID <pid> /T /F で止める。
"""


def powershell_available() -> bool:
    """実際に PowerShell を起動できるか (``check --synth`` から呼ぶ)."""
    executable = find_powershell()
    if executable is None:
        return False
    try:
        proc = subprocess.run(
            [executable, "-NoProfile", "-Command", "exit 0"],
            capture_output=True,
            timeout=30,
            cwd="/mnt/c" if Path("/mnt/c").is_dir() else None,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0
