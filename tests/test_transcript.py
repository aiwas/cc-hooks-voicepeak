"""トランスクリプト解析のテスト."""

from __future__ import annotations

import json
import shutil
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from cc_voicepeak.transcript import (
    extract_text,
    iter_entries,
    last_assistant_text,
    session_summary,
)

NL = chr(10)


def assistant(text_blocks, **extra):
    entry = {
        "type": "assistant",
        "isSidechain": False,
        "message": {"role": "assistant", "content": text_blocks},
    }
    entry.update(extra)
    return entry


def text_block(text: str):
    return [{"type": "text", "text": text}]


class TranscriptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-voicepeak-transcript-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_jsonl(self, entries, name: str = "transcript.jsonl") -> Path:
        path = self.tmp / name
        path.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + NL for entry in entries),
            encoding="utf-8",
        )
        return path

    def write_raw(self, body: str, name: str = "raw.jsonl") -> Path:
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return path


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

    def test_content_is_none(self):
        self.assertEqual(extract_text({"content": None}), "")

    def test_content_is_not_a_list(self):
        self.assertEqual(extract_text({"content": {"type": "text"}}), "")

    def test_content_is_missing(self):
        self.assertEqual(extract_text({}), "")

    def test_non_dict_blocks_are_skipped(self):
        content = ["生の文字列", {"type": "text", "text": "本文"}]
        self.assertEqual(extract_text({"content": content}), "本文")

    def test_block_without_text(self):
        self.assertEqual(extract_text({"content": [{"type": "text"}]}), "")


class LastAssistantTest(TranscriptTestCase):
    def test_returns_latest_text(self):
        path = self.write_jsonl(
            [
                {"type": "user", "message": {"role": "user", "content": "やって"}},
                assistant(text_block("古い応答")),
                assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
                assistant(text_block("新しい応答")),
            ]
        )
        self.assertEqual(last_assistant_text(path), "新しい応答")

    def test_skips_tool_only_last_message(self):
        path = self.write_jsonl(
            [
                assistant(text_block("本文です")),
                assistant([{"type": "tool_use", "name": "Bash", "input": {}}]),
            ]
        )
        self.assertEqual(last_assistant_text(path), "本文です")

    def test_ignores_sidechain_by_default(self):
        path = self.write_jsonl(
            [
                assistant(text_block("メイン")),
                assistant(text_block("サブ"), isSidechain=True),
            ]
        )
        self.assertEqual(last_assistant_text(path), "メイン")
        self.assertEqual(last_assistant_text(path, include_sidechain=True), "サブ")

    def test_ignores_meta_entries(self):
        path = self.write_jsonl(
            [assistant(text_block("本文")), assistant(text_block("メタ"), isMeta=True)]
        )
        self.assertEqual(last_assistant_text(path), "本文")

    def test_ignores_wrong_role(self):
        entry = assistant(text_block("役割違い"))
        entry["message"]["role"] = "user"
        path = self.write_jsonl([assistant(text_block("本文")), entry])
        self.assertEqual(last_assistant_text(path), "本文")

    def test_ignores_non_dict_message(self):
        path = self.write_jsonl(
            [
                assistant(text_block("本文")),
                {"type": "assistant", "isSidechain": False, "message": "文字列"},
            ]
        )
        self.assertEqual(last_assistant_text(path), "本文")

    def test_tolerates_broken_lines(self):
        path = self.write_raw(
            "{壊れた行" + NL + json.dumps(assistant(text_block("無事"))) + NL
        )
        self.assertEqual(last_assistant_text(path), "無事")

    def test_tolerates_valid_json_that_is_not_an_object(self):
        # JSON として妥当だが dict でない行
        path = self.write_raw(
            "[1, 2]" + NL + '"文字列"' + NL + "123" + NL
            + json.dumps(assistant(text_block("無事"))) + NL
        )
        self.assertEqual(last_assistant_text(path), "無事")

    def test_empty_file(self):
        self.assertIsNone(last_assistant_text(self.write_raw("")))

    def test_blank_lines_only(self):
        self.assertIsNone(last_assistant_text(self.write_raw(NL * 5)))

    def test_missing_file_returns_none(self):
        self.assertIsNone(last_assistant_text(Path("/nonexistent/x.jsonl")))

    def test_no_assistant_entries(self):
        path = self.write_jsonl([{"type": "user", "message": {"role": "user"}}])
        self.assertIsNone(last_assistant_text(path))


class LookbackTest(TranscriptTestCase):
    def transcript(self, count: int = 10) -> Path:
        entries = [assistant(text_block("本文"))]
        entries += [assistant([{"type": "tool_use", "name": "Bash", "input": {}}])] * count
        return self.write_jsonl(entries)

    def test_lookback_limits_the_search(self):
        path = self.transcript(count=10)
        # 本文は 11 件前にあるので、直近 3 件しか見なければ見つからない
        self.assertIsNone(last_assistant_text(path, max_lookback=3))
        self.assertEqual(last_assistant_text(path, max_lookback=50), "本文")

    def test_zero_means_unlimited(self):
        path = self.transcript(count=10)
        self.assertEqual(last_assistant_text(path, max_lookback=0), "本文")

    def test_negative_means_unlimited(self):
        path = self.transcript(count=10)
        self.assertEqual(last_assistant_text(path, max_lookback=-1), "本文")

    def test_long_transcript_is_not_read_into_memory(self):
        """直近 400 件のために JSONL 全体を抱え込まない."""
        entry = assistant(text_block("あ" * 200))
        path = self.write_jsonl([entry] * 5000)
        size = path.stat().st_size

        tracemalloc.start()
        try:
            self.assertIsNotNone(last_assistant_text(path))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        self.assertGreater(size, 2 * 1024 * 1024, "テスト用ファイルが小さすぎる")
        self.assertLess(peak, 2 * 1024 * 1024, f"ピーク {peak // 1024} KB は大きすぎる")


class IterEntriesTest(TranscriptTestCase):
    def test_is_lazy(self):
        path = self.write_jsonl([assistant(text_block("本文"))] * 3)
        entries = iter_entries(path)
        self.assertEqual(len(list(entries)), 3)

    def test_missing_file_yields_nothing(self):
        self.assertEqual(list(iter_entries(Path("/nonexistent/x.jsonl"))), [])


class SummaryTest(TranscriptTestCase):
    def test_summary_counts_entries(self):
        path = self.write_jsonl([assistant(text_block("a")), {"type": "user"}])
        summary = session_summary(path)
        self.assertEqual(summary["entries"], 2)
        self.assertEqual(summary["types"]["assistant"], 1)

    def test_summary_of_empty_file(self):
        self.assertEqual(session_summary(self.write_raw("")), {"entries": 0, "types": {}})

    def test_entries_without_type(self):
        path = self.write_jsonl([{"message": {}}])
        self.assertEqual(session_summary(path)["types"]["unknown"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
