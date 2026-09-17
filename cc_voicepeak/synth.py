"""voicepeak.exe を叩いて 1 ブロックずつ wav を作る.

voicepeak は 1 回の起動で 140 文字までしか受け付けず、しかも同時起動もできないので
「ブロックごとに EXE を起動 → 直列に合成」という形にしかならない。起動コストが
大きいので、同じ文面のキャッシュ再利用を入れてある。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .bridge import Bridge
from .errors import LockTimeout, SynthError
from .locking import ExeLock, pid_alive
from .logging_util import get_logger

log = get_logger("synth")

# 生存しているプロセスの作業ディレクトリでも、これより古ければ掃除する。
# PID 再利用や別ディストロとの PID 衝突で消し損ねないための保険。
WORK_DIR_MAX_AGE = 6 * 3600

# リトライの待ち時間 (秒)。試行ごとに倍にして上限で頭打ちにする。
RETRY_BACKOFF = 0.5
RETRY_BACKOFF_MAX = 3.0

# 再試行しても解消しない失敗 (文字数超過など)
_PERMANENT_ERROR = re.compile(r"too long|too many|文字数|上限", re.I)


def _is_permanent(exc: SynthError) -> bool:
    return bool(_PERMANENT_ERROR.search(str(exc)))


def redact_command(command: Sequence[str]) -> list[str]:
    """``-s`` の値を長さとハッシュに置き換えたコマンド表現.

    そのまま出すと ``log.level=debug`` でアシスタント応答の全文が
    ログファイルに平文で蓄積される。
    """
    out: list[str] = []
    redact_next = False
    for token in command:
        if redact_next:
            digest = hashlib.sha1(token.encode("utf-8")).hexdigest()[:8]
            out.append(f"<{len(token)}文字 sha1:{digest}>")
            redact_next = False
            continue
        out.append(token)
        redact_next = token in ("-s", "--say")
    return out

# 改行以外の制御文字 (voicepeak に渡すと落ちる可能性がある)
_CONTROL_CHARS = "".join(
    chr(code)
    for code in list(range(0x00, 0x0A)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)) + [0x7F]
)
_CONTROL = re.compile("[" + re.escape(_CONTROL_CHARS) + "]")
_SENTENCE_TAIL = "。．！？!?、，,;；:："
NEWLINE = chr(10)


def new_work_dir(root: Path) -> Path:
    """このプロセス専用の作業ディレクトリを作る.

    PID だけで名前を決めると、PID が再利用されたときに別プロセスの wav と
    混ざる。``mkdtemp`` で一意な接尾辞を付ける。
    """
    run_root = root / "run"
    run_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=str(run_root)))


def _owner_alive(entry: Path) -> bool:
    head = entry.name.split("-", 1)[0]
    return pid_alive(int(head)) if head.isdigit() else False


def prune_work_dirs(
    root: Path, keep: Path | None = None, max_age: float = WORK_DIR_MAX_AGE
) -> int:
    """終了したプロセスが残した作業ディレクトリを消し、消した数を返す.

    ``hook.on_busy = replace`` では対象プロセスが SIGKILL されるため、
    :func:`pipeline.speak` の ``finally`` による後始末が走らない。放っておくと
    Windows の ``%TEMP%\\cc-voicepeak\\run\\`` に wav が溜まり続ける。
    """
    try:
        entries = list((root / "run").iterdir())
    except OSError:
        return 0

    now = time.time()
    removed = 0
    for entry in entries:
        if entry == keep or not entry.is_dir():
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            age = max_age + 1
        if _owner_alive(entry) and age < max_age:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    if removed:
        log.debug("古い作業ディレクトリを %d 件削除しました", removed)
    return removed


@dataclass(frozen=True, slots=True)
class VoiceParams:
    """voicepeak に渡す声のパラメータ."""

    narrator: str | None = None
    emotion: str | None = None
    speed: int | None = None
    pitch: int | None = None

    def args(self) -> list[str]:
        args: list[str] = []
        if self.narrator:
            args += ["-n", str(self.narrator)]
        if self.emotion:
            args += ["-e", str(self.emotion)]
        if self.speed is not None:
            args += ["--speed", str(int(self.speed))]
        if self.pitch is not None:
            args += ["--pitch", str(int(self.pitch))]
        return args

    def key(self) -> str:
        return "|".join(
            str(value) for value in (self.narrator, self.emotion, self.speed, self.pitch)
        )


@dataclass(slots=True)
class SynthResult:
    index: int
    text: str
    path: Path
    cached: bool = False
    elapsed: float = 0.0
    error: str | None = None
    success: bool = False
    command: Sequence[str] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        # 一時ファイルは読み上げ後に消えるため、成否は合成時点で確定させる
        return self.error is None and self.success


def prepare_block(text: str) -> str:
    """1 ブロックを EXE に渡せる 1 行のテキストに整える.

    改行はそのまま渡すと環境によって扱いが変わるため、読点に寄せて 1 行にする。
    """
    text = _CONTROL.sub(" ", text)
    out: list[str] = []
    for ch in text:
        if ch == NEWLINE:
            if out and out[-1] in _SENTENCE_TAIL:
                continue
            if out:
                out.append("、")
            continue
        out.append(ch)
    result = "".join(out)
    result = re.sub(r"[ \t　]{2,}", " ", result).strip()
    return result.strip("、")


class Synthesizer:
    """ブロック列 -> wav 列."""

    def __init__(
        self,
        exe: Path,
        bridge: Bridge,
        params: VoiceParams | None = None,
        char_limit: int = 140,
        input_mode: str = "say",
        timeout: int = 120,
        retries: int = 1,
        use_cache: bool = True,
        cache_max_files: int = 400,
        work_dir: Path | None = None,
    ):
        self.exe = Path(exe)
        self.bridge = bridge
        self.params = params or VoiceParams()
        self.char_limit = char_limit
        self.input_mode = input_mode
        self.timeout = timeout
        self.retries = max(0, retries)
        self.use_cache = use_cache
        self.cache_max_files = cache_max_files

        root = bridge.temp_root()
        self.cache_dir = root / "cache"
        if work_dir:
            self.work_dir = Path(work_dir)
            self.work_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.work_dir = new_work_dir(root)
            # 前回 SIGKILL された読み上げの残骸をここで掃除する
            prune_work_dirs(root, keep=self.work_dir)
        if self.use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- キャッシュ --------------------------------------------------------
    def _cache_path(self, text: str) -> Path:
        digest = hashlib.sha1(f"{self.params.key()} {text}".encode()).hexdigest()
        return self.cache_dir / f"{digest}.wav"

    def _store_cache(self, text: str, source: Path) -> None:
        """合成した wav をキャッシュへ置く.

        直接コピーすると、並行プロセスが書きかけ (``st_size > 44`` を満たす)
        の wav を読み、途中までの音声を再生し得る。同一ディレクトリの一時名へ
        書いてから :func:`os.replace` で差し替える。
        """
        dest = self._cache_path(text)
        tmp = dest.with_name(f"{dest.stem}.{os.getpid()}.tmp")
        try:
            shutil.copyfile(source, tmp)
            os.replace(tmp, dest)
        except OSError as exc:
            log.debug("キャッシュの書き込みに失敗しました: %s", exc)
            try:
                tmp.unlink()
            except OSError:
                pass

    def prune_cache(self) -> None:
        if not self.use_cache or self.cache_max_files <= 0:
            return
        try:
            files = sorted(
                self.cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except OSError:
            return
        for stale in files[self.cache_max_files :]:
            try:
                stale.unlink()
            except OSError:
                pass

    # -- 合成 --------------------------------------------------------------
    def _attempt_modes(self, text: str) -> list[str]:
        """試行するモードの並びを返す (``retries + 1`` 件).

        以前は fallback だけを ``retries`` 回繰り返しており、元のモードが
        再試行されなかった。``say`` と ``text_file`` を交互に使う。
        """
        if text.startswith("-"):
            # -s の値がオプションと誤認されるので text_file に固定する
            return ["text_file"] * (self.retries + 1)

        primary = self.input_mode
        other = "say" if primary == "text_file" else "text_file"
        modes = [primary]
        while len(modes) <= self.retries:
            modes.append(other if len(modes) % 2 else primary)
        return modes

    def synth_block(self, index: int, text: str, out_path: Path | None = None) -> SynthResult:
        prepared = prepare_block(text)
        if not prepared:
            return SynthResult(index, text, Path(), error="空のブロック")

        if len(prepared) > self.char_limit:
            return SynthResult(
                index,
                prepared,
                Path(),
                error=f"{len(prepared)} 文字は上限 {self.char_limit} を超えています",
            )

        target = Path(out_path) if out_path else self.work_dir / f"{index:04d}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)

        if self.use_cache:
            cached = self._cache_path(prepared)
            if cached.exists() and cached.stat().st_size > 44:
                try:
                    shutil.copyfile(cached, target)
                    log.debug("キャッシュ命中 #%d", index)
                    return SynthResult(index, prepared, target, cached=True, success=True)
                except OSError:
                    pass

        modes = self._attempt_modes(prepared)

        last_error = "不明なエラー"
        command: Sequence[str] = ()
        exhausted: set = set()
        started = time.monotonic()
        for attempt, mode in enumerate(modes):
            if attempt:
                # 一過性の失敗 (EXE の後始末待ちなど) に備えて少し待つ
                time.sleep(min(RETRY_BACKOFF * 2 ** (attempt - 1), RETRY_BACKOFF_MAX))
            try:
                command = self._run(prepared, target, mode)
            except LockTimeout as exc:
                # 再試行しても待ち時間が伸びるだけなので諦める
                last_error = str(exc)
                log.warning("合成に失敗 #%d: %s", index, exc)
                break
            except SynthError as exc:
                last_error = str(exc)
                log.warning(
                    "合成に失敗 #%d (mode=%s, 試行 %d/%d): %s",
                    index,
                    mode,
                    attempt + 1,
                    len(modes),
                    exc,
                )
                if _is_permanent(exc):
                    # 同じモードで繰り返しても無駄。ただし -s と -t で
                    # 文字数の数え方が違う可能性があるので、別モードは試す
                    exhausted.add(mode)
                    if set(modes[attempt + 1 :]) <= exhausted:
                        log.warning("どのモードでも解消しないため諦めます #%d", index)
                        break
                continue
            elapsed = time.monotonic() - started
            if self.use_cache:
                self._store_cache(prepared, target)
            return SynthResult(
                index, prepared, target, elapsed=elapsed, success=True, command=command
            )

        return SynthResult(index, prepared, target, error=last_error, command=command)

    def _run(self, text: str, target: Path, mode: str) -> Sequence[str]:
        command: list[str] = [str(self.exe)]
        text_file: Path | None = None

        if mode == "text_file":
            text_file = target.with_suffix(".txt")
            # BOM なし UTF-8 / 末尾改行なしで書く
            text_file.write_bytes(text.encode("utf-8"))
            command += ["-t", self.bridge.to_win(text_file)]
        else:
            command += ["-s", text]

        command += ["-o", self.bridge.to_win(target)]
        command += self.params.args()

        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass

        log.debug("実行: %s", redact_command(command))
        try:
            with ExeLock():
                proc = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=self.timeout,
                    cwd=self.bridge.exec_cwd(),
                    env=self.bridge.popen_env(),
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise SynthError(f"voicepeak がタイムアウトしました ({self.timeout}s)") from exc
        except FileNotFoundError as exc:
            raise SynthError(
                f"voicepeak を実行できません: {self.exe} / "
                "WSL interop (/proc/sys/fs/binfmt_misc/WSLInterop) が有効か確認してください。"
            ) from exc
        except OSError as exc:
            raise SynthError(f"voicepeak の起動に失敗しました: {exc}") from exc
        finally:
            if text_file is not None:
                try:
                    text_file.unlink()
                except OSError:
                    pass

        stdout = _decode(proc.stdout)
        stderr = _decode(proc.stderr)
        if proc.returncode != 0:
            raise SynthError(
                f"voicepeak が異常終了しました (code={proc.returncode}): "
                f"{(stderr or stdout).strip()[:300]}"
            )
        if not target.exists() or target.stat().st_size <= 44:
            detail = (stderr or stdout).strip()[:300]
            raise SynthError(
                "wav が出力されませんでした: "
                + (detail or "出力先の書き込み権限を確認してください")
            )
        return tuple(command)

    # -- 情報取得 ----------------------------------------------------------
    def _simple_run(self, *args: str) -> str:
        try:
            with ExeLock():
                proc = subprocess.run(
                    [str(self.exe), *args],
                    capture_output=True,
                    timeout=60,
                    cwd=self.bridge.exec_cwd(),
                    check=False,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SynthError(f"voicepeak を実行できません: {exc}") from exc
        stdout = _decode(proc.stdout)
        stderr = _decode(proc.stderr)
        if proc.returncode != 0:
            # 検査しないと list_narrators() がエラーメッセージを声の一覧として返す
            raise SynthError(
                f"voicepeak が異常終了しました (code={proc.returncode}): "
                f"{(stderr or stdout).strip()[:300]}"
            )
        return stdout or stderr

    def check(self) -> str:
        return self._simple_run("--help")

    def list_narrators(self) -> list[str]:
        output = self._simple_run("--list-narrator")
        return [line.strip() for line in output.splitlines() if line.strip()]

    def list_emotions(self, narrator: str) -> list[str]:
        output = self._simple_run("--list-emotion", narrator)
        return [line.strip() for line in output.splitlines() if line.strip()]

    def cleanup(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)


def _decode(raw: bytes) -> str:
    if not raw:
        return ""
    for encoding in ("utf-8", "cp932", "utf-16-le"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
