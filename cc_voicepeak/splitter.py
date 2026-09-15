"""日本語テキストを voicepeak の 140 文字制限に収まるブロックへ分割する.

方針
----
voicepeak は 1 回の起動で 140 文字までしか受け付けないので、長文は必ず分割する。
単純に 140 文字ごとに切ると文の途中や単語の途中で切れて音声が不自然になるため、
次の 2 段構えで処理する。

1. **候補列挙 (字句解析)**
   文字列を走査して「ここで切ってもよい位置」を列挙し、それぞれに優先度を付ける。
   句点 > 改行 > 読点 > 括弧の外側 > 接続助詞・活用語尾 > 格助詞 >
   文字種の切り替わり > (禁則を満たす) 任意位置 の順。

2. **貪欲パッキング**
   先頭から、上限に収まる範囲のうち ``min_fill`` を超えた位置にある候補の中で
   最も優先度の高いものを選ぶ。これにより「ブロックを十分詰める (= EXE 起動回数
   を減らす)」と「自然な位置で切る」を両立させる。

いずれの候補も無い場合のみ、禁則処理付きのハードカットにフォールバックする。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 優先度
# ---------------------------------------------------------------------------
PRIO_PARAGRAPH = 100    # 空行 (段落境界)
PRIO_SENTENCE = 90      # 。！？. などの文末
PRIO_NEWLINE = 80       # 単独の改行
PRIO_COMMA = 70         # 、，, ；;
PRIO_MIDDOT = 64        # ：: … ―  ―
PRIO_BRACKET_CLOSE = 60 # 」』）】 の直後
PRIO_BRACKET_OPEN = 58  # 「『（【 の直前
PRIO_CONJ = 46          # 接続助詞・活用語尾 (〜ので / 〜から / 〜して / 〜が)
PRIO_PARTICLE = 38      # 格助詞の直後 (〜を / 〜に / 〜で)
PRIO_SCRIPT = 26        # 文字種の切り替わり
PRIO_SPACE = 20         # 空白の直後
PRIO_ANY = 8            # 禁則を満たす任意位置

# ---------------------------------------------------------------------------
# 文字集合
# ---------------------------------------------------------------------------
SENTENCE_ENDS = "。．！？!?｡"
# 文末記号の後ろに続いてよい (= まだ切らない) 文字
TRAILERS = "」』）〉》〕】〙〗”’\"')]｝】…"
OPEN_BRACKETS = "「『（(【〔〈《〘〖［[｛{“‘"
CLOSE_BRACKETS = "」』）)】〕〉》〙〗］]｝}”’"
COMMAS = "、，,；;"
MIDDOTS = "：:…―‐—〜~"

# 行頭禁則 (この文字の前では切らない)
NO_BREAK_BEFORE = set(
    "、。，．,.！？!?：；:;〕）｝」』】〉》〙〗］]}）"
    "”’\"'ーヽヾゝゞ々〻・…‥"
    "ぁぃぅぇぉっゃゅょゎゕゖ"
    "ァィゥェォッャュョヮヵヶ"
    "%％‰°℃゛゜"
)
# 行末禁則 (この文字の後ろでは切らない)
NO_BREAK_AFTER = set("「『（(【〔〈《〘〖［[｛{“‘＄$¥￥#＃@＠")

# 半角語 (英単語・数値・パス・識別子) の内部では切らない
WORDISH = re.compile(r"[0-9A-Za-z_\-.,:/@#+&'~%=?]")

def _by_length(words: Sequence[str]) -> Tuple[str, ...]:
    """長い接尾辞から順に照合するため、長さ降順で固定しておく."""
    return tuple(sorted(words, key=len, reverse=True))


# 接続助詞・活用語尾。ここで切ると比較的自然。
CONJ_SUFFIXES = _by_length((
    "ので", "のに", "から", "けれども", "けれど", "けども", "けど",
    "ですが", "ますが", "だが", "ですし", "ますし",
    "したが", "ため", "うえで", "あとで", "つつ", "ながら",
    "ましたが", "ました", "ください", "です", "ます",
    "であり", "でして", "まして", "して", "たり", "れば", "ならば",
))
# 直前が「て/で/が/し/ば」で終わる用言の切れ目
CONJ_SINGLE = ("て", "で", "が", "し", "ば")
# 格助詞
PARTICLES = _by_length(
    ("を", "に", "は", "も", "と", "へ", "や", "より", "まで", "での", "への")
)

# 文字種
_SCRIPT_OTHER = "other"


def _script_of(ch: str) -> str:
    if ch.isspace():
        return "space"
    code = ord(ch)
    if 0x3040 <= code <= 0x309F:
        return "hira"
    if 0x30A0 <= code <= 0x30FF or code == 0xFF70 or 0xFF66 <= code <= 0xFF9D:
        return "kata"
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or ch == "々":
        return "kanji"
    if ch.isdigit():
        return "digit"
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
        return "latin"
    return _SCRIPT_OTHER


# ---------------------------------------------------------------------------
# 文字幅
# ---------------------------------------------------------------------------
def char_width(ch: str, mode: str = "codepoints") -> float:
    """1 文字ぶんのカウント値.

    voicepeak はコードポイント数で 140 文字を判定するため既定は ``codepoints``。
    ``halfwidth_half`` は半角文字を 0.5 として数える (日本語換算) モード。
    """
    if mode == "halfwidth_half":
        return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.5
    return 1.0


def text_width(text: str, mode: str = "codepoints") -> float:
    if mode == "codepoints":
        return float(len(text))
    return sum(char_width(ch, mode) for ch in text)


def _cumulative_widths(text: str, mode: str) -> List[float]:
    """``widths[i]`` = ``text[:i]`` の幅. 長さは ``len(text) + 1``."""
    if mode == "codepoints":
        return [float(i) for i in range(len(text) + 1)]
    acc = [0.0]
    total = 0.0
    for ch in text:
        total += char_width(ch, mode)
        acc.append(total)
    return acc


# ---------------------------------------------------------------------------
# 候補列挙
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BreakPoint:
    """``text[:pos]`` と ``text[pos:]`` の境界."""

    pos: int
    priority: int


def _is_combining(ch: str) -> bool:
    if unicodedata.combining(ch):
        return True
    code = ord(ch)
    # 異体字セレクタ / ZWJ / 結合用の記号
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:
        return True
    if code in (0x200D, 0x200C, 0x20E3):
        return True
    if 0x1F3FB <= code <= 0x1F3FF:  # 肌の色の修飾子
        return True
    return False


def can_break_at(text: str, pos: int) -> bool:
    """禁則処理として ``pos`` で切ってよいか."""
    if pos <= 0 or pos >= len(text):
        return False
    prev_ch = text[pos - 1]
    next_ch = text[pos]
    if _is_combining(next_ch):
        return False
    if ord(prev_ch) in (0x200D, 0x200C):
        # ZWJ 直後 (絵文字の合字) では切らない
        return False
    if prev_ch == "\r" and next_ch == "\n":
        return False
    if next_ch in NO_BREAK_BEFORE:
        return False
    if prev_ch in NO_BREAK_AFTER:
        return False
    if WORDISH.match(prev_ch) and WORDISH.match(next_ch):
        return False
    return True


def _endswith_any(text: str, pos: int, suffixes: Sequence[str]) -> Optional[str]:
    """``text[:pos]`` の末尾に一致する接尾辞を返す.

    ``suffixes`` は長さ降順に並んでいる前提 (1 文字あたり 2 回呼ばれるので、
    ここで毎回 sorted() しない)。
    """
    for suffix in suffixes:
        start = pos - len(suffix)
        if start >= 0 and text.startswith(suffix, start):
            return suffix
    return None


def _sentence_end_pos(text: str, index: int) -> Optional[int]:
    """``index`` の文字が文末記号なら、閉じ括弧まで含めた境界位置を返す."""
    ch = text[index]
    if ch not in SENTENCE_ENDS:
        return None
    if ch in ".．":
        # 3.14 / v1.2 / e.g. のようなピリオドは文末扱いしない
        prev_ch = text[index - 1] if index > 0 else ""
        next_ch = text[index + 1] if index + 1 < len(text) else ""
        if prev_ch.isdigit() and next_ch.isdigit():
            return None
        if next_ch and not next_ch.isspace() and next_ch not in TRAILERS:
            return None
        if prev_ch and WORDISH.match(prev_ch) and len(prev_ch.encode()) == 1 and index >= 2:
            # "e.g." のような略記: 直前が 1 文字の英字なら文末としない
            if text[index - 2] in ".·・ " or (index >= 2 and text[index - 2] == "."):
                return None
    pos = index + 1
    while pos < len(text) and (text[pos] in SENTENCE_ENDS or text[pos] in TRAILERS):
        pos += 1
    return pos


def find_break_points(text: str) -> List[BreakPoint]:
    """``text`` 中の分割候補を位置順に返す (同じ位置は最高優先度のみ)."""
    best: dict = {}

    def offer(pos: int, priority: int) -> None:
        if not can_break_at(text, pos):
            return
        if best.get(pos, -1) < priority:
            best[pos] = priority

    length = len(text)
    for i, ch in enumerate(text):
        # 文末
        end = _sentence_end_pos(text, i)
        if end is not None:
            offer(end, PRIO_SENTENCE)

        # 改行 / 段落
        if ch == "\n":
            trailing = text[i + 1 :]
            blank = re.match(r"[ \t　]*\n", trailing)
            offer(i + 1, PRIO_PARAGRAPH if blank else PRIO_NEWLINE)

        if ch in COMMAS:
            offer(i + 1, PRIO_COMMA)
        elif ch in MIDDOTS:
            offer(i + 1, PRIO_MIDDOT)
        elif ch in CLOSE_BRACKETS:
            offer(i + 1, PRIO_BRACKET_CLOSE)
        elif ch in OPEN_BRACKETS:
            offer(i, PRIO_BRACKET_OPEN)
        elif ch.isspace():
            offer(i + 1, PRIO_SPACE)

        pos = i + 1
        if pos < length:
            # 接続助詞・活用語尾
            suffix = _endswith_any(text, pos, CONJ_SUFFIXES)
            if suffix:
                offer(pos, PRIO_CONJ)
            elif ch in CONJ_SINGLE and _script_of(text[pos]) != "hira":
                # 「〜して」+ 漢字/カタカナ のように、後続が助詞でない場合のみ
                offer(pos, PRIO_CONJ - 4)

            particle = _endswith_any(text, pos, PARTICLES)
            if particle and _script_of(text[pos]) != "hira":
                offer(pos, PRIO_PARTICLE)

            # 文字種の切り替わり
            left, right = _script_of(ch), _script_of(text[pos])
            if left != right and _SCRIPT_OTHER not in (left, right) and "space" not in (left, right):
                offer(pos, PRIO_SCRIPT)

            offer(pos, PRIO_ANY)

    return [BreakPoint(pos, prio) for pos, prio in sorted(best.items())]


# ---------------------------------------------------------------------------
# パッキング
# ---------------------------------------------------------------------------
_ONLY_PUNCT = re.compile(
    r"^[\s　、。，．,.！？!?：；:;（）\(\)「」『』【】\[\]{}〜~\-–—_=+*/\\|^`'\"…・]*$"
)


def _speakable(text: str) -> bool:
    return bool(text.strip()) and not _ONLY_PUNCT.match(text)


# 候補列挙は「上限に収まる範囲＋数文字」だけを見る。全文を毎回走査すると
# ブロック数 × 残り文字数で O(n^2) になり、長文で読み上げ開始が数十秒遅れる。
# 数文字の余裕は、後続文字の種類や閉じ括弧の判定に必要なぶん。
_LOOKAHEAD = 32


def _window_size(limit: int, width_mode: str) -> int:
    """上限に収まりうる最大の文字数 (＋先読み)."""
    # 1 文字の幅は halfwidth_half で最小 0.5、codepoints では 1.0
    span = limit * 2 if width_mode == "halfwidth_half" else limit
    return span + _LOOKAHEAD


def _hard_cut(text: str, limit_index: int) -> int:
    """禁則を満たす位置まで戻ってハードカットする位置を返す."""
    pos = min(limit_index, len(text) - 1)
    floor = max(1, int(limit_index * 0.5))
    while pos > floor and not can_break_at(text, pos):
        pos -= 1
    if pos <= 0:
        pos = min(limit_index, len(text))
    return pos


def split_text(
    text: str,
    limit: int = 140,
    min_fill: float = 0.55,
    width_mode: str = "codepoints",
    balance_tail: bool = True,
    drop_empty: bool = True,
) -> List[str]:
    """``text`` を幅 ``limit`` 以下のブロック列に分割する.

    Parameters
    ----------
    limit:
        1 ブロックの最大幅 (voicepeak の場合 140)。
    min_fill:
        「この割合を超えたら区切ってよい」しきい値。0.55 なら 77 文字以上詰めた
        あとで最も自然な候補を選ぶ。
    width_mode:
        :func:`char_width` のモード。
    balance_tail:
        末尾ブロックが極端に短くなったとき、直前のブロックとの分割位置を見直す。
    drop_empty:
        記号・空白のみのブロックを捨てる。
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    text = text.strip()
    if not text:
        return []

    blocks: List[str] = []
    remaining = text

    window_size = _window_size(limit, width_mode)

    while remaining:
        # 上限付近までしか見ないので、ウィンドウぶんだけ幅と候補を求める
        window = remaining[:window_size]
        widths = _cumulative_widths(window, width_mode)

        if len(window) == len(remaining) and widths[-1] <= limit:
            blocks.append(remaining)
            break

        # 上限に収まる最大の index
        max_index = 0
        for index in range(1, len(window) + 1):
            if widths[index] <= limit:
                max_index = index
            else:
                break
        if max_index <= 0:
            max_index = 1

        floor_width = limit * float(min_fill)
        candidates = [bp for bp in find_break_points(window) if bp.pos <= max_index]

        chosen: Optional[int] = None
        preferred = [bp for bp in candidates if widths[bp.pos] >= floor_width]
        pool = preferred or candidates
        if pool:
            best_prio = max(bp.priority for bp in pool)
            chosen = max(bp.pos for bp in pool if bp.priority == best_prio)
        if chosen is None:
            chosen = _hard_cut(remaining, max_index)

        head = remaining[:chosen]
        remaining = remaining[chosen:].lstrip()
        if head.strip():
            blocks.append(head.strip())

    if balance_tail and len(blocks) >= 2:
        blocks = _balance_tail(blocks, limit, min_fill, width_mode)

    if drop_empty:
        kept: List[str] = []
        for block in blocks:
            if _speakable(block):
                kept.append(block)
            elif kept:
                merged = kept[-1] + block
                if text_width(merged, width_mode) <= limit:
                    kept[-1] = merged
        blocks = kept

    return blocks


def _balance_tail(
    blocks: List[str], limit: int, min_fill: float, width_mode: str
) -> List[str]:
    """最後のブロックが短すぎる場合、直前のブロックとまとめて再分割する."""
    tail = blocks[-1]
    if text_width(tail, width_mode) >= limit * 0.3:
        return blocks

    merged = blocks[-2] + ("" if blocks[-2].endswith("\n") else "") + tail
    if text_width(merged, width_mode) <= limit:
        return blocks[:-2] + [merged]

    widths = _cumulative_widths(merged, width_mode)
    half = text_width(merged, width_mode) / 2.0
    candidates = [
        bp
        for bp in find_break_points(merged)
        if widths[bp.pos] <= limit and text_width(merged[bp.pos :], width_mode) <= limit
    ]
    if not candidates:
        return blocks
    best_prio = max(bp.priority for bp in candidates)
    best = min(
        (bp for bp in candidates if bp.priority == best_prio),
        key=lambda bp: abs(widths[bp.pos] - half),
    )
    left, right = merged[: best.pos].strip(), merged[best.pos :].strip()
    if not left or not right:
        return blocks
    if text_width(right, width_mode) < limit * min_fill * 0.5:
        return blocks
    return blocks[:-2] + [left, right]


def describe_blocks(blocks: Sequence[str], width_mode: str = "codepoints") -> List[Tuple[int, float, str]]:
    """``(番号, 幅, テキスト)`` のリスト. ``--dry-run`` の表示用."""
    return [(i + 1, text_width(block, width_mode), block) for i, block in enumerate(blocks)]
