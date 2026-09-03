"""WAV の連結と長さ計算.

voicepeak が出力する wav はすべて同じフォーマットなので、``wave`` モジュールだけで
無劣化に連結できる (ffmpeg/sox 不要)。連結すればブロック間の切れ目が完全に消え、
再生も 1 回で済む。
"""

from __future__ import annotations

import contextlib
import wave
from pathlib import Path
from typing import Optional, Sequence

from .errors import PlayerError


def wav_duration(path: Path) -> float:
    """秒数を返す. 読めない場合は 0.0."""
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as reader:
            rate = reader.getframerate() or 1
            return reader.getnframes() / float(rate)
    except (OSError, wave.Error):
        return 0.0


def concat_wavs(sources: Sequence[Path], dest: Path, gap_seconds: float = 0.0) -> Optional[Path]:
    """``sources`` を ``dest`` に連結する. 1 件でも読めれば ``dest`` を返す."""
    usable = [path for path in sources if path.exists() and path.stat().st_size > 44]
    if not usable:
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    writer: Optional[wave.Wave_write] = None
    params = None
    try:
        for path in usable:
            with contextlib.closing(wave.open(str(path), "rb")) as reader:
                if writer is None:
                    params = reader.getparams()
                    writer = wave.open(str(dest), "wb")
                    writer.setparams(params)
                elif reader.getparams()[:3] != params[:3]:  # type: ignore[index]
                    raise PlayerError(
                        f"wav のフォーマットが揃っていません: {path.name}"
                    )
                writer.writeframes(reader.readframes(reader.getnframes()))

            if gap_seconds > 0 and params is not None and path is not usable[-1]:
                silence = b"\x00" * int(
                    params.framerate * gap_seconds * params.sampwidth * params.nchannels
                )
                writer.writeframes(silence)  # type: ignore[union-attr]
    except wave.Error as exc:
        raise PlayerError(f"wav の連結に失敗しました: {exc}") from exc
    finally:
        if writer is not None:
            writer.close()

    return dest


def write_silence(dest: Path, seconds: float = 0.1, framerate: int = 48000) -> Path:
    """テスト用の無音 wav を書き出す."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(wave.open(str(dest), "wb")) as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(framerate)
        writer.writeframes(b"\x00\x00" * int(framerate * seconds))
    return dest
