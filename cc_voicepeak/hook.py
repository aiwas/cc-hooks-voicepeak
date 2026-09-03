"""Claude Code Hooks のエントリポイント.

Claude Code は hook コマンドの終了を待つので、読み上げ (数十秒かかる) を
その場でやってしまうと会話が止まる。そこで hook プロセスは

1. stdin の JSON を読む
2. 読み上げるテキストを決める
3. 別プロセス (setsid 済み) に投げて即 exit 0

という流れにしてある。連続してタスクが終わったときの挙動は
``hook.on_busy`` で ``replace`` (割り込み) / ``queue`` (待つ) / ``skip`` から選ぶ。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import Config
from .logging_util import get_logger
from .transcript import last_assistant_text

log = get_logger("hook")

DETACH_ENV = "CC_VOICEPEAK_DETACHED"


def read_payload(stream=None) -> Dict[str, Any]:
    """hook の stdin (JSON) を読む. JSON でなければ ``{"message": <生テキスト>}``."""
    stream = stream or sys.stdin
    try:
        raw = stream.read()
    except (OSError, UnicodeDecodeError):
        return {}
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"message": raw}
    return payload if isinstance(payload, dict) else {"message": str(payload)}


def resolve_text(payload: Dict[str, Any], cfg: Config) -> Tuple[Optional[str], str]:
    """``(読み上げるテキスト, 理由)`` を返す. テキストが None ならスキップ."""
    event = str(payload.get("hook_event_name") or "")
    events = cfg.get("hook.events", []) or []

    if event and event not in events:
        if not (event == "SubagentStop" and cfg.get("hook.subagent")):
            return None, f"{event} は hook.events の対象外"

    if event in ("Stop", "SubagentStop", "SessionEnd", ""):
        transcript = payload.get("transcript_path")
        if not transcript:
            message = payload.get("message")
            return (str(message), event or "message") if message else (None, "transcript_path なし")
        path = Path(str(transcript)).expanduser()
        text = last_assistant_text(path, include_sidechain=bool(payload.get("isSidechain")))
        if not text:
            return None, "アシスタント応答が見つからない"
        return text, event or "Stop"

    if event == "Notification":
        message = payload.get("message")
        if not message:
            return None, "message なし"
        prefix = str(cfg.get("hook.notification_prefix") or "")
        return prefix + str(message), event

    message = payload.get("message") or payload.get("text")
    if message:
        return str(message), event or "message"
    return None, f"{event} からテキストを取り出せない"


def decorate(text: str, cfg: Config) -> str:
    prefix = str(cfg.get("hook.prefix") or "")
    suffix = str(cfg.get("hook.suffix") or "")
    return f"{prefix}{text}{suffix}"


def spawn_detached(text: str, session_key: str, cfg: Config, config_paths=None) -> int:
    """読み上げ本体を別プロセスとして起動し、その pid を返す."""
    command = [
        sys.executable,
        "-m",
        "cc_voicepeak",
        "speak",
        "--stdin",
        "--session",
        session_key,
        "--on-busy",
        str(cfg.get("hook.on_busy", "replace")),
    ]
    for path in config_paths or []:
        command += ["--config", str(path)]

    env = dict(os.environ)
    env[DETACH_ENV] = "1"

    log_path = os.devnull
    with open(log_path, "wb") as sink:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=sink,
            stderr=sink,
            start_new_session=True,
            env=env,
            cwd=str(Path.cwd()),
        )
    try:
        assert proc.stdin is not None
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError) as exc:
        log.warning("読み上げプロセスへの書き込みに失敗しました: %s", exc)
    return proc.pid
