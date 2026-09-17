"""WAV の連結と長さ計算.

voicepeak が出力する wav はすべて同じフォーマットなので、``wave`` モジュールだけで
無劣化に連結できる (ffmpeg/sox 不要)。連結すればブロック間の切れ目が完全に消え、
再生も 1 回で済む。
"""

from __future__ import annotations

import os
import wave
from collections.abc import Sequence
from pathlib import Path

from .logging_util import get_logger

log = get_logger("wav")

# 壊れた wav を開く/読むときに出る例外。wave.Error だけでは捕まえきれない
# (ヘッダが途中で切れていると EOFError、閉じたファイルを読むと RuntimeError)。
WAVE_ERRORS = (OSError, wave.Error, EOFError, RuntimeError)

# RIFF/fmt/data の最小ヘッダ長
_MIN_HEADER = 44


def wav_duration(path: Path) -> float:
    """秒数を返す. 読めない場合は 0.0.

    ヘッダの ``nframes`` は途中で切れた wav では嘘になる (実際より長い秒数を
    返す)。再生時間の見積もりに使うので、実ファイルサイズから読めるぶんで
    頭打ちにする。
    """
    try:
        with wave.open(str(path), "rb") as reader:
            rate = reader.getframerate() or 1
            frame_size = max(reader.getsampwidth() * reader.getnchannels(), 1)
            frames = reader.getnframes()
            try:
                readable = max(path.stat().st_size - _MIN_HEADER, 0) // frame_size
            except OSError:
                readable = frames
            return max(min(frames, readable), 0) / float(rate)
    except WAVE_ERRORS:
        return 0.0


def _silence(params: wave._wave_params, seconds: float) -> bytes:
    """フレーム境界に揃えた無音."""
    frames = int(params.framerate * seconds)
    return b"\x00" * (frames * params.sampwidth * params.nchannels)


def concat_wavs(sources: Sequence[Path], dest: Path, gap_seconds: float = 0.0) -> Path | None:
    """``sources`` を ``dest`` に連結する.

    読めない wav やフォーマットの違う wav は読み飛ばし、1 件でも書ければ
    ``dest`` を返す。1 件も使えなければ ``None`` を返し、``dest`` は作らない。
    書き込みは一時ファイルへ行って最後に差し替えるので、失敗しても中途半端な
    ``dest`` が残らない。
    """
    usable = [path for path in sources if path.exists() and path.stat().st_size > _MIN_HEADER]
    if not usable:
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.{os.getpid()}.tmp")
    writer: wave.Wave_write | None = None
    params = None
    written = 0

    try:
        for index, path in enumerate(usable):
            try:
                with wave.open(str(path), "rb") as reader:
                    if writer is None:
                        params = reader.getparams()
                        writer = wave.open(str(tmp), "wb")
                        writer.setparams(params)
                    elif reader.getparams()[:3] != params[:3]:  # type: ignore[index]
                        log.warning(
                            "フォーマットが違う wav を飛ばします: %s", path.name
                        )
                        continue
                    writer.writeframes(reader.readframes(reader.getnframes()))
            except WAVE_ERRORS as exc:
                log.warning("wav を読めないので飛ばします: %s (%s)", path.name, exc)
                continue

            written += 1
            if gap_seconds > 0 and params is not None and index < len(usable) - 1:
                writer.writeframes(_silence(params, gap_seconds))  # type: ignore[union-attr]
    finally:
        if writer is not None:
            writer.close()

    if not written:
        _remove(tmp)
        return None

    try:
        os.replace(tmp, dest)
    except OSError as exc:
        log.warning("連結した wav を配置できませんでした: %s", exc)
        _remove(tmp)
        return None
    return dest


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def write_silence(dest: Path, seconds: float = 0.1, framerate: int = 48000) -> Path:
    """テスト用の無音 wav を書き出す."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(framerate)
        writer.writeframes(b"\x00\x00" * int(framerate * seconds))
    return dest
