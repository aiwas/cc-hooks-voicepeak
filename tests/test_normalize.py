"""読み上げ用テキスト整形のテスト."""

from __future__ import annotations

import unittest

from cc_voicepeak.config import DEFAULTS
from cc_voicepeak.normalize import normalize

NL = chr(10)


def run(text: str, **options) -> str:
    opts = dict(DEFAULTS["normalize"])
    opts.update(options)
    return normalize(text, opts)


class NormalizeTest(unittest.TestCase):
    def test_headings_become_sentences(self):
        self.assertEqual(run("## 実装完了"), "実装完了。")

    def test_code_fence_becomes_placeholder(self):
        text = NL.join(["説明します。", "```python", "print(1)", "```", "以上です。"])
        result = run(text)
        self.assertNotIn("print", result)
        self.assertIn("コードブロック", result)

    def test_code_fence_can_be_dropped(self):
        text = NL.join(["説明します。", "```", "print(1)", "```"])
        self.assertEqual(run(text, code_blocks="drop"), "説明します。")

    def test_unclosed_fence_keeps_following_text(self):
        text = NL.join(["前。", "```py", "code()", "まだ続く本文です。"])
        result = run(text)
        self.assertIn("前。", result)
        self.assertIn("まだ続く本文です。", result)
        self.assertNotIn("コードブロック", result)

    def test_unclosed_fence_with_drop_mode(self):
        text = NL.join(["前。", "```", "まだ続く本文です。"])
        self.assertIn("まだ続く本文です。", run(text, code_blocks="drop"))

    def test_code_fence_can_be_read(self):
        text = NL.join(["説明します。", "```python", "print(1)", "```"])
        self.assertIn("print(1)", run(text, code_blocks="read"))

    def test_inline_code_is_read_without_backticks(self):
        self.assertEqual(run("`split_text` を直しました。"), "split_text を直しました。")

    def test_inline_code_keeps_snake_case(self):
        self.assertEqual(
            run("`get_last_assistant_text` を呼ぶ。"), "get_last_assistant_text を呼ぶ。"
        )

    def test_inline_code_keeps_dunder(self):
        self.assertEqual(run("`__init__` を定義。"), "__init__ を定義。")

    def test_snake_case_outside_backticks_is_kept(self):
        self.assertEqual(run("max_total_chars_value を見る。"), "max_total_chars_value を見る。")

    def test_asterisk_between_digits_is_kept(self):
        self.assertEqual(run("2*3*4 を計算。"), "2*3*4 を計算。")

    def test_inline_code_can_be_dropped(self):
        self.assertNotIn("split_text", run("`split_text` を直した。", inline_code="drop"))

    def test_links_read_label_only(self):
        self.assertEqual(run("[README](https://example.com/a) を見て。"), "README を見て。")

    def test_bare_url_is_replaced(self):
        self.assertEqual(run("詳細は https://example.com/x です。"), "詳細はリンクです。")

    def test_urls_can_be_kept(self):
        self.assertIn("https://", run("https://example.com", strip_urls=False))

    def test_paths_are_shortened(self):
        self.assertEqual(run("src/cc/splitter.py を修正。"), "splitter.py を修正。")

    def test_path_with_line_number(self):
        self.assertIn("120行目", run("/home/u/a/b.py:120 が原因です。"))

    def test_date_is_not_treated_as_path(self):
        self.assertEqual(run("2024/09/14 に実施しました。"), "2024/09/14 に実施しました。")

    def test_japanese_slash_is_not_treated_as_path(self):
        self.assertEqual(run("読み/書き の権限です。"), "読み/書きの権限です。")

    def test_and_or_is_not_treated_as_path(self):
        self.assertEqual(run("AND/OR を指定。"), "AND/OR を指定。")

    def test_windows_path_is_shortened(self):
        self.assertEqual(run("C:\\work\\a\\b.txt を開く。"), "b.txt を開く。")

    def test_relative_path_without_extension_is_kept(self):
        self.assertEqual(run("src/cc を見る。"), "src/cc を見る。")

    def test_emoji_is_stripped(self):
        self.assertEqual(run("完了しました\U0001F389"), "完了しました")

    def test_checkbox_is_verbalized(self):
        text = NL.join(["- [x] 分割", "- [ ] テスト"])
        result = run(text)
        self.assertIn("完了、分割", result)
        self.assertIn("未完了、テスト", result)

    def test_table_dropped_by_default(self):
        text = NL.join(["結果です。", "| 項目 | 値 |", "|---|---|", "| 件数 | 3 |"])
        self.assertEqual(run(text), "結果です。")

    def test_table_can_be_read(self):
        text = NL.join(["| 項目 | 値 |", "|---|---|", "| 件数 | 3 |"])
        result = run(text, tables="read")
        self.assertIn("件数、3。", result)

    def test_horizontal_rule_removed(self):
        self.assertEqual(run(NL.join(["前。", "---", "後。"])), "前。" + NL + "後。")

    def test_bold_markers_removed(self):
        self.assertEqual(run("**重要**な点です。"), "重要な点です。")

    def test_replacements_applied(self):
        result = run("PR を作成しました。", replacements=[["(?i)\\bPR\\b", "プルリク"]])
        self.assertEqual(result, "プルリクを作成しました。")

    def test_invalid_replacement_is_ignored(self):
        self.assertEqual(run("そのまま。", replacements=[["([", "x"], ["bad"]]), "そのまま。")

    def test_max_total_chars_truncates_at_sentence(self):
        text = "一つ目の文です。二つ目の文です。三つ目の文です。"
        result = run(text, max_total_chars=20, truncated_suffix="以下省略。")
        self.assertTrue(result.endswith("以下省略。"))
        self.assertLessEqual(len(result), 20 + len("以下省略。"))

    def test_tool_markers_removed(self):
        self.assertEqual(run("⏺ 完了しました。"), "完了しました。")

    def test_html_is_stripped(self):
        self.assertEqual(run("<b>強調</b>です。"), "強調です。")

    def test_html_with_attributes_is_stripped(self):
        self.assertEqual(run('<div class="x">本文</div>'), "本文")

    def test_generic_type_is_not_stripped(self):
        self.assertEqual(run("List<int> を返す。"), "List<int> を返す。")
        self.assertEqual(run("Vec<T> に詰める。"), "Vec<T> に詰める。")

    def test_blank_lines_collapsed(self):
        self.assertEqual(run(NL.join(["前。", "", "", "後。"])), "前。" + NL + "後。")
