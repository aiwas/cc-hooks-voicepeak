"""トランスクリプト解析のテスト."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cc_voicepeak.transcript import extract_text, last_assistant_text, session_summary


def write_jsonl(entries) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    for entry in entries:
        handle.write(json.dumps(entry, ensure_ascii=False) + chr(10))
    handle.close()
    return Path(handle.name)


def assistant(text_blocks, **extra):
    entry = {
        "type": "assistant",
        "isSidechain": False,
        "message": {"role": "assistant", "content": text_blocks},
    }
    entry.update(extra)
    return entry


class ExtractTest(unittest.TestCase):
    def test_plain_string_content(self):
        self.assertEqual(extract_text({"content": "こんにちは"}), "こんにちは")

    def test_skips_thinking_and_tool_use(self):
        content = [
            {"type": "thinking", "thinking": "内心の声"},
            {"type": "tool_use", "name": "Bash", "input": {}},
            {"type": "text", "text": "完了しました。"},
        ]
        self.assertEqual(extract_text({"content": content}), "完了しました。")

    def test_joins_multiple_text_blocks(self):
        content = [{"type": "text", "text": "前半。"}, {"type": "text", "text": "後半。"}]
        self.assertIn("前半。", extract_text({"content": content}))
        self.assertIn("後半。", extract_text({"content": content}))


class LastAssistantTest(unittest.TestCase):
    def test_returns_latest_text(self):
        path = write_jsonl(
            [
                {"type": "user", "message": {"role": "user", "content": "やって"}},
                assistant([{"type": "text", "text": "古い応答"}]),
                assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
                assistant([{"type": "text", "text": "新しい応答"}]),
            ]
        )
        self.assertEqual(last_assistant_text(path), "新しい応答")

    def test_skips_tool_only_last_message(self):
        path = write_jsonl(
            [
                assistant([{"type": "text", "text": "本文です"}]),
                assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
            ]
        )
        self.assertEqual(last_assistant_text(path), "本文です")

    def test_ignores_sidechain_by_default(self):
        path = write_jsonl(
            [
                assistant([{"type": "text", "text": "メイン"}]),
                assistant([{"type": "text", "text": "サブ"}], isSidechain=True),
            ]
        )
        self.assertEqual(last_assistant_text(path), "メイン")
        self.assertEqual(last_assistant_text(path, include_sidechain=True), "サブ")

    def test_tolerates_broken_lines(self):
        path = Path(tempfile.mkstemp(suffix=".jsonl")[1])
        path.write_text(
            "{壊れた行" + chr(10) + json.dumps(assistant([{"type": "text", "text": "無事"}])) + chr(10),
            encoding="utf-8",
        )
        self.assertEqual(last_assistant_text(path), "無事")

    def test_missing_file_returns_none(self):
        self.assertIsNone(last_assistant_text(Path("/nonexistent/x.jsonl")))

    def test_summary_counts_entries(self):
        path = write_jsonl([assistant([{"type": "text", "text": "a"}]), {"type": "user"}])
        summary = session_summary(path)
        self.assertEqual(summary["entries"], 2)
        self.assertEqual(summary["types"]["assistant"], 1)
