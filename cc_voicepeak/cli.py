"""コマンドラインインターフェース.

    cc-voicepeak hook                 # Claude Code の hook から呼ばれる
    cc-voicepeak speak "テキスト"      # 手動で読み上げ
    cc-voicepeak split -f notes.md    # 分割結果だけ確認 (合成しない)
    cc-voicepeak check --synth        # 環境診断
    cc-voicepeak narrators            # 声の一覧
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from . import __version__
from .bridge import detect_bridge
from .config import VOICEPEAK_CHAR_LIMIT, Config, load_config
from .diagnose import FAIL, run_checks, wsl_notes
from .errors import CcVoicepeakError
from .locking import SpeechSlot
from .logging_util import get_logger, setup_logging
from .pipeline import prepare_blocks, speak, synth_to_file
from .splitter import text_width

log = get_logger("cli")


# ---------------------------------------------------------------------------
# 引数
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cc-voicepeak",
        description="Claude Code の結果を VOICEPEAK に読み上げさせる (WSL 対応)",
    )
    parser.add_argument("--version", action="version", version=f"cc-voicepeak {__version__}")
    parser.add_argument(
        "--config", action="append", metavar="PATH", help="追加で読み込む設定ファイル"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="ログを標準エラーにも出す")
    parser.add_argument("--log-level", choices=["debug", "info", "warning", "error", "off"])

    sub = parser.add_subparsers(dest="command", required=True)

    def add_voice_options(target: argparse.ArgumentParser) -> None:
        target.add_argument("--narrator", "-n", help="ナレーター名")
        target.add_argument("--emotion", "-e", help="感情表現 (例: happy=50)")
        target.add_argument("--speed", type=int, help="速度 50-200")
        target.add_argument("--pitch", type=int, help="ピッチ -300-300")
        target.add_argument("--exe", help="voicepeak.exe のパス")

    def add_text_options(target: argparse.ArgumentParser) -> None:
        target.add_argument("text", nargs="*", help="読み上げるテキスト")
        target.add_argument("--stdin", action="store_true", help="標準入力から読む")
        target.add_argument("-f", "--file", help="ファイルから読む")
        target.add_argument(
            "--limit",
            type=int,
            help=f"1 ブロックの文字数上限 (既定 {VOICEPEAK_CHAR_LIMIT})",
        )
        target.add_argument("--min-fill", type=float, help="ブロックをどれだけ詰めるか (0.0-1.0)")
        target.add_argument(
            "--no-normalize", action="store_true", help="Markdown 整形を行わない"
        )

    speak_cmd = sub.add_parser("speak", help="テキストを読み上げる")
    add_text_options(speak_cmd)
    add_voice_options(speak_cmd)
    speak_cmd.add_argument("-o", "--out", help="再生せず連結した wav を保存する")
    speak_cmd.add_argument(
        "--player", choices=["auto", "powershell", "paplay", "aplay", "ffplay", "none"]
    )
    speak_cmd.add_argument("--concat", action="store_true", help="全ブロックを連結してから再生")
    speak_cmd.add_argument("--dry-run", action="store_true", help="分割結果だけ表示する")
    speak_cmd.add_argument("--session", default="default", help="読み上げスロットのキー")
    speak_cmd.add_argument("--on-busy", choices=["replace", "queue", "skip"])
    speak_cmd.add_argument("--no-cache", action="store_true", help="wav キャッシュを使わない")

    split_cmd = sub.add_parser("split", help="分割結果を表示する (合成しない)")
    add_text_options(split_cmd)
    split_cmd.add_argument("--json", action="store_true", help="JSON で出力")

    hook_cmd = sub.add_parser("hook", help="Claude Code Hooks から呼ぶ")
    hook_cmd.add_argument("--event", help="hook_event_name を上書きする")
    hook_cmd.add_argument(
        "--sync", action="store_true", help="別プロセスに投げず、読み上げ完了まで待つ"
    )
    add_voice_options(hook_cmd)

    check_cmd = sub.add_parser("check", help="環境を診断する")
    check_cmd.add_argument("--synth", action="store_true", help="実際に 1 回合成してみる")
    check_cmd.add_argument("--notes", action="store_true", help="WSL 連携の注意点を表示")
    check_cmd.add_argument("--print-config", action="store_true", help="有効な設定を表示")

    sub.add_parser("narrators", help="利用できるナレーターを一覧する")
    emotions_cmd = sub.add_parser("emotions", help="ナレーターの感情パラメータを一覧する")
    emotions_cmd.add_argument("narrator", help="ナレーター名")

    settings_cmd = sub.add_parser(
        "install-hook", help="Claude Code 用の settings.json スニペットを表示する"
    )
    settings_cmd.add_argument(
        "--events",
        default="Stop,Notification",
        help="対象イベント (カンマ区切り, 既定 Stop,Notification)",
    )

    return parser


# ---------------------------------------------------------------------------
# 補助
# ---------------------------------------------------------------------------
def _overrides(args: argparse.Namespace) -> dict:
    mapping = {
        "voicepeak.narrator": getattr(args, "narrator", None),
        "voicepeak.emotion": getattr(args, "emotion", None),
        "voicepeak.speed": getattr(args, "speed", None),
        "voicepeak.pitch": getattr(args, "pitch", None),
        "voicepeak.exe": getattr(args, "exe", None),
        "voicepeak.char_limit": getattr(args, "limit", None),
        "split.min_fill": getattr(args, "min_fill", None),
        "player.backend": getattr(args, "player", None),
        "hook.on_busy": getattr(args, "on_busy", None),
        "log.level": getattr(args, "log_level", None),
    }
    if getattr(args, "no_normalize", False):
        mapping["normalize.enabled"] = False
    if getattr(args, "concat", False):
        mapping["player.concat"] = True
    if getattr(args, "no_cache", False):
        mapping["voicepeak.cache"] = False
    return {key: value for key, value in mapping.items() if value is not None}


_VOICE_FLAGS = (
    ("--narrator", "narrator"),
    ("--emotion", "emotion"),
    ("--speed", "speed"),
    ("--pitch", "pitch"),
    ("--exe", "exe"),
)


def _detach_options(args: argparse.Namespace) -> Tuple[List[str], List[str]]:
    """デタッチした読み上げプロセスへ引き継ぐ (グローバル引数, speak 引数)."""
    global_options: List[str] = []
    for path in getattr(args, "config", None) or []:
        global_options += ["--config", str(path)]
    if getattr(args, "verbose", False):
        global_options.append("--verbose")
    if getattr(args, "log_level", None):
        global_options += ["--log-level", str(args.log_level)]

    speak_options: List[str] = []
    for flag, name in _VOICE_FLAGS:
        value = getattr(args, name, None)
        if value is not None:
            speak_options += [flag, str(value)]
    return global_options, speak_options


def _load(args: argparse.Namespace) -> Config:
    paths = [Path(path) for path in (args.config or [])]
    cfg = load_config(extra_paths=paths, overrides=_overrides(args))
    setup_logging(
        level=str(cfg.get("log.level", "info")),
        file=cfg.get("log.file"),
        max_bytes=int(cfg.get("log.max_bytes", 1048576)),
        stderr=bool(args.verbose),
    )
    return cfg


def _read_text(args: argparse.Namespace) -> str:
    if getattr(args, "file", None):
        path = Path(args.file).expanduser()
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CcVoicepeakError(
                f"ファイルを読み込めません: {path} ({exc.strerror or exc})"
            ) from exc
    if getattr(args, "stdin", False) or not getattr(args, "text", None):
        if sys.stdin is None or sys.stdin.isatty():
            return " ".join(getattr(args, "text", []) or [])
        return sys.stdin.read()
    return " ".join(args.text)


# ---------------------------------------------------------------------------
# 各コマンド
# ---------------------------------------------------------------------------
def cmd_split(args: argparse.Namespace, text: Optional[str] = None) -> int:
    cfg = _load(args)
    text = _read_text(args) if text is None else text
    blocks = prepare_blocks(text, cfg)
    limit = int(cfg.get("voicepeak.char_limit", 140))

    if args.json:
        print(
            json.dumps(
                {
                    "limit": limit,
                    "blocks": [
                        {"index": i, "width": text_width(block), "text": block}
                        for i, block in enumerate(blocks, start=1)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        # 読み上げる内容が無いことは JSON 出力でも失敗として扱う
        return 0 if blocks else 1

    if not blocks:
        print("読み上げる内容がありません", file=sys.stderr)
        return 1
    total = sum(text_width(block) for block in blocks)
    print(f"{len(blocks)} ブロック / 合計 {int(total)} 文字 / 上限 {limit} 文字")
    for index, block in enumerate(blocks, start=1):
        width = int(text_width(block))
        flag = "!" if width > limit else " "
        print(f"{flag}[{index:>3}] {width:>4} 文字 | {block}")
    return 0


def cmd_speak(args: argparse.Namespace, text: Optional[str] = None) -> int:
    cfg = _load(args)
    text = _read_text(args) if text is None else text
    if not text.strip():
        print("読み上げるテキストがありません", file=sys.stderr)
        return 1

    if getattr(args, "dry_run", False):
        return cmd_split(argparse.Namespace(**{**vars(args), "json": False}), text=text)

    slot = SpeechSlot(args.session)
    policy = str(cfg.get("hook.on_busy", "replace"))
    if slot.busy():
        if policy == "skip":
            log.info("すでに読み上げ中なのでスキップします")
            return 0
        if policy == "queue":
            slot.wait_until_free()
        else:
            slot.interrupt()

    out_path = Path(args.out).expanduser() if args.out else None
    if out_path is not None:
        report = synth_to_file(text, cfg, out_path)
        print(f"{report.output} ({report.summary()})")
        return 0 if report.output else 1

    bridge = detect_bridge()
    from .player import select_player

    player = select_player(
        str(cfg.get("player.backend", "auto")), bridge, cfg.get("player.volume")
    )
    cancelled = threading.Event()

    def handle_signal(signum, _frame):  # pragma: no cover - シグナル経路
        log.info("シグナル %s を受け取りました", signum)
        cancelled.set()
        player.stop()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass

    slot.write(win_pid=None, text=text[:80])
    try:
        player.start()
    except CcVoicepeakError as exc:
        log.warning("プレイヤの起動に失敗しました: %s", exc)
    if player.win_pid:
        slot.update(win_pid=player.win_pid)

    def progress(result, total) -> None:
        if args.verbose:
            state = "cache" if result.cached else f"{result.elapsed:.1f}s"
            mark = "ok" if result.ok else f"NG ({result.error})"
            print(f"[{result.index}/{total}] {mark} {state}", file=sys.stderr)

    try:
        report = speak(
            text,
            cfg,
            bridge=bridge,
            player=player,
            on_progress=progress,
            should_cancel=cancelled.is_set,
        )
    finally:
        slot.clear()

    if args.verbose:
        print(report.summary(), file=sys.stderr)
    for error in report.errors:
        log.warning("%s", error)
    return 0 if report.ok_count or not report.blocks else 1


def cmd_hook(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from .hook import decorate, read_payload, resolve_text, spawn_detached

    payload = read_payload()
    if args.event:
        payload["hook_event_name"] = args.event

    text, reason = resolve_text(payload, cfg)
    if not text:
        log.info("読み上げをスキップ: %s", reason)
        return 0

    min_chars = int(cfg.get("hook.min_chars", 0))
    if len(text.strip()) < min_chars:
        log.info("短すぎるのでスキップ (%d 文字)", len(text.strip()))
        return 0

    text = decorate(text, cfg)
    session_key = str(payload.get("session_id") or "default")
    log.info("読み上げ対象 %d 文字 (%s, session=%s)", len(text), reason, session_key[:8])

    if args.sync or not cfg.get("hook.detach", True):
        speak_args = argparse.Namespace(
            **{
                **vars(args),
                "text": [],
                "stdin": False,
                "file": None,
                "out": None,
                "dry_run": False,
                "concat": False,
                "no_cache": False,
                "no_normalize": False,
                "limit": None,
                "min_fill": None,
                "player": None,
                "session": session_key,
                "on_busy": None,
            }
        )
        code = cmd_speak(speak_args, text=text)
        if code:
            # hook は Claude Code を止めないので、失敗はログに残すだけにする
            log.warning("読み上げが失敗しました (code=%d)", code)
        return 0

    global_options, speak_options = _detach_options(args)
    pid = spawn_detached(
        text,
        session_key,
        cfg,
        global_options=global_options,
        speak_options=speak_options,
    )
    log.info("読み上げプロセスを起動しました (pid=%d)", pid)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if args.notes:
        print(wsl_notes())
        return 0
    if args.print_config:
        print(json.dumps(cfg.as_dict(), ensure_ascii=False, indent=2))
        return 0

    items = run_checks(cfg, do_synth=args.synth)
    for item in items:
        print(item.line())
    failed = [item for item in items if item.status == FAIL]
    if failed:
        print(
            "\n" + f"{len(failed)} 件の問題があります。`cc-voicepeak check --notes` も参照してください。",
            file=sys.stderr,
        )
    return 1 if failed else 0


def cmd_narrators(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from .pipeline import build_synthesizer

    synth = build_synthesizer(cfg)
    for line in synth.list_narrators():
        print(line)
    return 0


def cmd_emotions(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from .pipeline import build_synthesizer

    synth = build_synthesizer(cfg)
    for line in synth.list_emotions(args.narrator):
        print(line)
    return 0


def cmd_install_hook(args: argparse.Namespace) -> int:
    events = [event.strip() for event in args.events.split(",") if event.strip()]
    command = "$CLAUDE_PROJECT_DIR/bin/cc-voicepeak hook"
    snippet = {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
            for event in events
        }
    }
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    print(
        "\n上記を .claude/settings.json (または ~/.claude/settings.json) にマージしてください。",
        file=sys.stderr,
    )
    return 0


_COMMANDS = {
    "speak": cmd_speak,
    "split": cmd_split,
    "hook": cmd_hook,
    "check": cmd_check,
    "narrators": cmd_narrators,
    "emotions": cmd_emotions,
    "install-hook": cmd_install_hook,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _COMMANDS[args.command]
    # hook から呼ばれている場合は、読み上げの失敗で Claude Code を止めない
    on_error = 0 if args.command == "hook" else 1
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except CcVoicepeakError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        log.error("%s", exc)
        return on_error
    except Exception as exc:  # noqa: BLE001 - hook を必ず 0 で終わらせるため
        print(f"エラー: {exc}", file=sys.stderr)
        log.exception("予期しないエラー: %s", exc)
        return on_error


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
