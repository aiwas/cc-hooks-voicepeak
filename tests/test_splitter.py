"""分割ロジックのテスト."""

from __future__ import annotations

import time
import unittest

from cc_voicepeak.splitter import (
    PRIO_SENTENCE,
    _balance_tail,
    can_break_at,
    char_width,
    describe_blocks,
    find_break_points,
    split_text,
    text_width,
)

NL = chr(10)

LONG_JA = (
    "実装が完了しました。まず設定ファイルを読み込む処理を追加し、"
    "次にテキストを分割する処理を実装しました。"
    "voicepeak は一回の起動で百四十文字までしか受け付けないため、"
    "長い文章は必ず分割してから順番に音声へ変換する必要があります。"
    "テストは全部で二十三件あり、すべて成功しています。"
    "詳細な使い方はリポジトリの README を参照してください。"
)


class SplitLimitTest(unittest.TestCase):
    def assert_within_limit(self, blocks, limit):
        for block in blocks:
            self.assertLessEqual(
                text_width(block), limit, f"{len(block)} 文字のブロックが上限を超えた: {block!r}"
            )

    def test_short_text_is_not_split(self):
        self.assertEqual(split_text("完了しました。", 140), ["完了しました。"])

    def test_empty_text(self):
        self.assertEqual(split_text("   \n  ", 140), [])

    def test_blocks_respect_limit(self):
        for limit in (20, 40, 80, 140):
            blocks = split_text(LONG_JA * 3, limit=limit)
            self.assert_within_limit(blocks, limit)
            self.assertGreater(len(blocks), 1)

    def test_no_text_is_lost(self):
        blocks = split_text(LONG_JA, limit=60)
        joined = "".join(blocks)
        # 区切り位置の空白除去以外で文字が消えていないこと
        self.assertEqual(joined.replace(" ", ""), LONG_JA.replace(" ", ""))

    def test_prefers_sentence_boundaries(self):
        blocks = split_text(LONG_JA, limit=140)
        self.assertGreater(len(blocks), 1)
        for block in blocks[:-1]:
            self.assertTrue(
                block.endswith("。"), f"文末以外で切れている: ...{block[-12:]!r}"
            )

    def test_text_without_punctuation_is_hard_cut(self):
        text = "あ" * 500
        blocks = split_text(text, limit=140)
        self.assert_within_limit(blocks, 140)
        self.assertEqual("".join(blocks), text)
        self.assertEqual(len(blocks), 4)

    def test_latin_words_are_not_split(self):
        text = ("設定は " + "configuration " * 40).strip()
        blocks = split_text(text, limit=60)
        self.assert_within_limit(blocks, 60)
        for block in blocks:
            for word in block.split():
                if word.isascii() and word.isalpha():
                    self.assertIn(
                        word, ("設定は", "configuration"), f"英単語が途中で切れた: {word!r}"
                    )

    def test_min_fill_controls_block_length(self):
        loose = split_text(LONG_JA * 2, limit=140, min_fill=0.0)
        tight = split_text(LONG_JA * 2, limit=140, min_fill=0.9)
        self.assertGreaterEqual(len(loose), len(tight))

    def test_halfwidth_half_mode(self):
        text = "abcdefghij" * 30
        blocks = split_text(text, limit=20, width_mode="halfwidth_half")
        for block in blocks:
            self.assertLessEqual(text_width(block, "halfwidth_half"), 20)
            # 半角は 0.5 換算なのでコードポイントでは 40 文字入る
            self.assertLessEqual(len(block), 41)


class KinsokuTest(unittest.TestCase):
    def test_never_breaks_before_closing_punctuation(self):
        text = "これは「引用」です。次の文もあります。"
        for pos in range(len(text)):
            if text[pos] in "、。」":
                self.assertFalse(can_break_at(text, pos), f"pos={pos} で切れてしまう")

    def test_never_breaks_after_opening_bracket(self):
        text = "彼は「そうだね」と言った。"
        pos = text.index("「") + 1
        self.assertFalse(can_break_at(text, pos))

    def test_small_kana_stays_with_previous_char(self):
        text = "しゃっくりが止まらない状態になってしまいました。"
        for pos, ch in enumerate(text):
            if ch in "ゃっ":
                self.assertFalse(can_break_at(text, pos))

    def test_combining_marks_are_not_split(self):
        text = "パ" + "゙" + "テスト"
        self.assertFalse(can_break_at(text, 1))

    def test_emoji_zwj_sequence_is_not_split(self):
        text = "終了\U0001F468‍\U0001F4BBです"
        for pos in (3, 4):
            self.assertFalse(can_break_at(text, pos))

    def test_hard_cut_keeps_graphemes(self):
        text = ("あ" * 139) + "パ゙" + ("い" * 100)
        blocks = split_text(text, limit=140)
        for block in blocks:
            self.assertFalse(block.startswith("゙"))


class BreakPointTest(unittest.TestCase):
    def test_sentence_end_includes_closing_quote(self):
        text = "彼は「終わった。」と言った。次へ進みます。"
        points = {bp.pos: bp.priority for bp in find_break_points(text)}
        expected = text.index("と言った")
        self.assertEqual(points.get(expected), PRIO_SENTENCE)

    def test_decimal_point_is_not_sentence_end(self):
        text = "円周率は3.14です。次の値は2.71です。"
        for bp in find_break_points(text):
            if bp.priority == PRIO_SENTENCE:
                self.assertIn(text[bp.pos - 1], "。")

    def test_newline_is_a_break_point(self):
        text = "一行目\n二行目"
        points = {bp.pos: bp.priority for bp in find_break_points(text)}
        self.assertIn(4, points)

    def test_bullet_list_splits_per_line(self):
        text = "\n".join(f"項目{i}を処理しました" for i in range(10))
        blocks = split_text(text, limit=40)
        for block in blocks:
            self.assertLessEqual(text_width(block), 40)


class OptionTest(unittest.TestCase):
    def test_non_positive_limit_raises(self):
        for limit in (0, -1):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    split_text("テキスト", limit=limit)

    def test_unknown_width_mode_raises(self):
        # 黙って codepoints 相当で動くと、数え方の違いに気づけない
        with self.assertRaises(ValueError):
            split_text("テキスト", width_mode="fullwidth")

    def test_drop_empty_false_keeps_symbol_only_blocks(self):
        text = "……" + NL + "本文です。"
        kept = split_text(text, limit=4, drop_empty=False)
        dropped = split_text(text, limit=4, drop_empty=True)
        self.assertGreater(len(kept), len(dropped))

    def test_crlf_input(self):
        blocks = split_text("一行目です。\r\n二行目です。", limit=10)
        self.assertNotIn("\r", "".join(blocks))
        for block in blocks:
            self.assertLessEqual(text_width(block), 10)

    def test_astral_characters_are_counted_as_one(self):
        # 補助面の文字 (𠀋 や 𝕏) はコードポイント 1 個として数える
        text = ("𠀋" * 40) + "。" + ("𝕏" * 40)
        blocks = split_text(text, limit=20)
        self.assertEqual("".join(blocks), text)
        for block in blocks:
            self.assertLessEqual(text_width(block), 20)

    def test_describe_blocks(self):
        rows = describe_blocks(["あい", "うえお"])
        self.assertEqual(rows, [(1, 2.0, "あい"), (2, 3.0, "うえお")])

    def test_char_width_modes(self):
        self.assertEqual(char_width("あ"), 1.0)
        self.assertEqual(char_width("a"), 1.0)
        self.assertEqual(char_width("あ", "halfwidth_half"), 1.0)
        self.assertEqual(char_width("a", "halfwidth_half"), 0.5)


class BalanceTailTest(unittest.TestCase):
    def test_merge_restores_the_space_between_words(self):
        # 連結しただけだと "wordtail" のように単語が繋がる
        merged = _balance_tail(["word", "tail"], 140, 0.55, "codepoints")
        self.assertEqual(merged, ["word tail"])

    def test_merge_does_not_insert_space_into_japanese(self):
        merged = _balance_tail(["完了しました。", "以上です。"], 140, 0.55, "codepoints")
        self.assertEqual(merged, ["完了しました。以上です。"])

    def test_existing_newline_is_kept_as_the_separator(self):
        merged = _balance_tail(["word" + NL, "tail"], 140, 0.55, "codepoints")
        self.assertEqual(merged, ["word" + NL + "tail"])

    def test_long_enough_tail_is_left_alone(self):
        blocks = ["あ" * 60, "い" * 60]
        self.assertEqual(_balance_tail(blocks, 140, 0.55, "codepoints"), blocks)


class PerformanceTest(unittest.TestCase):
    def test_long_text_is_split_quickly(self):
        """候補列挙が残り全文に対して走ると O(n^2) になり数十秒かかる."""
        base = "実装が完了しました。設定ファイルを読み込み、音声へ変換します。"
        text = (base * 500)[:30000]
        started = time.monotonic()
        blocks = split_text(text)
        elapsed = time.monotonic() - started
        self.assertGreater(len(blocks), 100)
        self.assertLess(elapsed, 5.0, f"30,000 文字の分割に {elapsed:.1f} 秒かかった")


if __name__ == "__main__":
    unittest.main()
