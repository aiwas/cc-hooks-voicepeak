"""整形 → 分割 → 直列合成 → 順次再生 のパイプライン.

    テキスト
      │  normalize.normalize()       Markdown を落として読み上げ向けに整形
      ▼
    ブロック列 (各 140 文字以内)
      │  splitter.split_text()       字句解析して自然な位置で分割
      ▼
    wav 列
      │  Synthesizer.synth_block()   voicepeak.exe を 1 ブロックずつ直列に起動
      ▼
    再生
         Player.enqueue()            合成できた順に流し込む (または連結して 1 本)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from .bridge import Bridge, detect_bridge, resolve_exe
from .config import Config
from .errors import CcVoicepeakError, PlayerError
from .logging_util import get_logger
from .normalize import normalize
from .player import Player, select_player
from .splitter import split_text, text_width
from .synth import SynthResult, Synthesizer, VoiceParams
from .wavutil import concat_wavs

log = get_logger("pipeline")


@dataclass
class SpeakReport:
    """1 回の読み上げの結果."""

    blocks: List[str] = field(default_factory=list)
    results: List[SynthResult] = field(default_factory=list)
    output: Optional[Path] = None
    elapsed: float = 0.0
    cancelled: bool = False
    player: str = ""

    @property
    def ok_count(self) -> int:
        return sum(1 for result in self.results if result.ok)

    @property
    def errors(self) -> List[str]:
        return [
            f"#{result.index}: {result.error}" for result in self.results if result.error
        ]

    def summary(self) -> str:
        parts = [
            f"{len(self.blocks)} ブロック",
            f"成功 {self.ok_count}",
            f"{self.elapsed:.1f} 秒",
        ]
        cached = sum(1 for result in self.results if result.cached)
        if cached:
            parts.append(f"キャッシュ {cached}")
        if self.errors:
            parts.append(f"失敗 {len(self.errors)}")
        if self.cancelled:
            parts.append("中断")
        return " / ".join(parts)


def build_voice_params(cfg: Config) -> VoiceParams:
    return VoiceParams(
        narrator=cfg.get("voicepeak.narrator"),
        emotion=cfg.get("voicepeak.emotion"),
        speed=cfg.get("voicepeak.speed"),
        pitch=cfg.get("voicepeak.pitch"),
    )


def prepare_blocks(text: str, cfg: Config) -> List[str]:
    """整形と分割だけを行う (``--dry-run`` からも使う)."""
    if cfg.get("normalize.enabled", True):
        text = normalize(text, cfg.section("normalize"))
    return split_text(
        text,
        limit=int(cfg.get("voicepeak.char_limit", 140)),
        min_fill=float(cfg.get("split.min_fill", 0.55)),
        width_mode=str(cfg.get("split.width_mode", "codepoints")),
        balance_tail=bool(cfg.get("split.balance_tail", True)),
        drop_empty=bool(cfg.get("split.drop_empty", True)),
    )


def build_synthesizer(cfg: Config, bridge: Optional[Bridge] = None) -> Synthesizer:
    bridge = bridge or detect_bridge()
    exe = resolve_exe(bridge, cfg.get("voicepeak.exe"))
    return Synthesizer(
        exe=exe,
        bridge=bridge,
        params=build_voice_params(cfg),
        char_limit=int(cfg.get("voicepeak.char_limit", 140)),
        input_mode=str(cfg.get("voicepeak.input_mode", "say")),
        timeout=int(cfg.get("voicepeak.timeout", 120)),
        retries=int(cfg.get("voicepeak.retries", 1)),
        use_cache=bool(cfg.get("voicepeak.cache", True)),
        cache_max_files=int(cfg.get("voicepeak.cache_max_files", 400)),
    )


def speak(
    text: str,
    cfg: Config,
    bridge: Optional[Bridge] = None,
    player: Optional[Player] = None,
    out_path: Optional[Path] = None,
    on_progress: Optional[Callable[[SynthResult, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    keep_files: bool = False,
) -> SpeakReport:
    """``text`` を読み上げる (合成は直列・再生は順次).

    Parameters
    ----------
    out_path:
        指定すると再生の代わりに連結 wav をこのパスへ保存する。
    on_progress:
        ブロックが 1 つ合成できるたびに ``(result, total)`` で呼ばれる。
    should_cancel:
        True を返すと以降の合成を打ち切る (割り込み用)。
    """
    started = time.monotonic()
    report = SpeakReport()

    bridge = bridge or detect_bridge()
    blocks = prepare_blocks(text, cfg)
    report.blocks = blocks
    if not blocks:
        log.info("読み上げる内容がありません")
        return report

    log.info(
        "%d ブロックに分割しました (最大 %d 文字)",
        len(blocks),
        int(max(text_width(block) for block in blocks)),
    )

    synth = build_synthesizer(cfg, bridge)
    concat_mode = bool(cfg.get("player.concat", False)) or out_path is not None

    if out_path is not None:
        active_player: Player = select_player("none", bridge)
    elif player is not None:
        active_player = player
    else:
        active_player = select_player(
            str(cfg.get("player.backend", "auto")), bridge, cfg.get("player.volume")
        )
    report.player = active_player.name

    if not concat_mode:
        try:
            active_player.start()
        except PlayerError as exc:
            log.warning("プレイヤの起動に失敗しました: %s", exc)

    try:
        for index, block in enumerate(blocks, start=1):
            if should_cancel is not None and should_cancel():
                report.cancelled = True
                log.info("中断されました (#%d 手前)", index)
                break

            result = synth.synth_block(index, block)
            report.results.append(result)
            if on_progress is not None:
                on_progress(result, len(blocks))

            if result.error:
                log.warning("#%d を合成できませんでした: %s", index, result.error)
                continue
            log.debug("#%d 合成完了 (%.2fs, cached=%s)", index, result.elapsed, result.cached)

            if not concat_mode:
                try:
                    active_player.enqueue(result.path)
                except PlayerError as exc:
                    log.warning("再生キューへの追加に失敗しました: %s", exc)

        wavs = [result.path for result in report.results if result.ok]

        if concat_mode and wavs:
            dest = Path(out_path) if out_path else synth.work_dir / "speech.wav"
            try:
                report.output = concat_wavs(wavs, dest)
            except (CcVoicepeakError, OSError) as exc:
                log.warning("wav の連結に失敗しました: %s", exc)
            if out_path is None and report.output is not None:
                try:
                    active_player.start()
                    active_player.enqueue(report.output)
                except PlayerError as exc:
                    log.warning("再生に失敗しました: %s", exc)

        active_player.finish()
    except KeyboardInterrupt:
        report.cancelled = True
        active_player.stop()
        raise
    finally:
        synth.prune_cache()
        # out_path を指定した場合、成果物は work_dir の外にあるので消してよい
        # (ブロックごとの中間 wav が残り続けていた)
        if out_path is not None or not keep_files:
            synth.cleanup()

    report.elapsed = time.monotonic() - started
    log.info("読み上げ完了: %s", report.summary())
    return report


def synth_to_file(text: str, cfg: Config, dest: Path, bridge: Optional[Bridge] = None) -> SpeakReport:
    """再生せず 1 本の wav にまとめて保存する."""
    report = speak(text, cfg, bridge=bridge, out_path=Path(dest), keep_files=True)
    if report.output is None and report.blocks:
        raise CcVoicepeakError("wav を生成できませんでした: " + "; ".join(report.errors))
    return report
