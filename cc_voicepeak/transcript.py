"""Claude Code のトランスクリプト (JSONL) から「読み上げる内容」を取り出す.

Stop hook が受け取る JSON には ``transcript_path`` しか入っていないため、
最終的なアシスタント応答は自分でトランスクリプトから拾う必要がある。

1 行 1 JSON で、アシスタントの発言は次の形をしている::

    {"type": "assistant", "isSidechain": false,
     "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "..."},
                             {"type": "tool_use", ...}]}}

``thinking`` / ``redacted_thinking`` / ``tool_use`` ブロックは読み上げ対象外。
サブエージェント (``isSidechain: true``) の発言も既定では除外する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .logging_util import get_logger

log = get_logger("transcript")

SKIP_BLOCK_TYPES = {"thinking", "redacted_thinking", "tool_use", "tool_result", "image"}


def iter_entries(path: Path) -> Iterator[Dict[str, Any]]:
    """JSONL を 1 行ずつ dict にして返す (壊れた行は飛ばす)."""
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError as exc:
        log.warning("トランスクリプトを読めません: %s (%s)", path, exc)


def extract_text(message: Dict[str, Any]) -> str:
    """assistant メッセージから読み上げ対象のテキストを取り出す."""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in SKIP_BLOCK_TYPES:
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts).strip()


def last_assistant_text(
    path: Path,
    include_sidechain: bool = False,
    max_lookback: int = 400,
) -> Optional[str]:
    """最後のアシスタント応答 (テキストを含むもの) を返す."""
    entries = list(iter_entries(path))
    if not entries:
        return None

    for entry in reversed(entries[-max_lookback:]):
        if entry.get("type") != "assistant":
            continue
        if entry.get("isSidechain") and not include_sidechain:
            continue
        if entry.get("isMeta"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = extract_text(message)
        if text:
            return text
    return None


def session_summary(path: Path) -> Dict[str, Any]:
    """デバッグ用: トランスクリプトの概要."""
    counts: Dict[str, int] = {}
    total = 0
    for entry in iter_entries(path):
        total += 1
        kind = str(entry.get("type", "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
    return {"entries": total, "types": counts}
