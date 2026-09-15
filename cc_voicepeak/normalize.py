"""Claude Code の出力 (Markdown) を読み上げ向けのプレーンテキストに整形する.

Markdown 記号やコードブロック、URL、長いファイルパスをそのまま読ませると
聞いていられないので、読み上げ前に落とす/言い換える。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Sequence, Tuple

_FENCE = re.compile(r"^\s*(?:```+|~~~+)(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$")
_HR = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
_BLOCKQUOTE = re.compile(r"^\s{0,3}(?:>\s?)+")
_CHECKBOX = re.compile(r"^\[([ xX])\]\s*")

_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_AUTOLINK = re.compile(r"<((?:https?|ftp)://[^>\s]+)>")
_BARE_URL = re.compile(r"(?:https?|ftp)://[^\s<>()\[\]「」『』、。]+")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
# 強調記号は語の途中では扱わない。`max_total_chars` の _ や `2*3*4` の * を
# 巻き込まないため。前後の判定は ASCII 英数のみ (日本語を語中扱いすると
# 「**重要**な点」がマッチしなくなる)。
_BOLD_ITALIC = re.compile(
    r"(?<![A-Za-z0-9])(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1(?![A-Za-z0-9])"
)
# インラインコードの退避に使う目印
_CODE_MARK = "\x00"
_CODE_REF = re.compile(r"\x00(\d+)\x00")
_STRIKE = re.compile(r"~~(.+?)~~")
# 既知のタグ名に限定する。`</?[A-Za-z]\w*>` だと List<int> や Vec<T> のような
# 型表記まで削ってしまう。
_HTML_TAG_NAMES = (
    "a|abbr|article|aside|audio|b|blockquote|br|button|canvas|caption|cite|code|col"
    "|colgroup|dd|del|details|div|dl|dt|em|embed|fieldset|figcaption|figure|footer"
    "|form|h[1-6]|head|header|hr|html|i|iframe|img|input|ins|kbd|label|legend|li|main"
    "|mark|nav|noscript|object|ol|optgroup|option|output|p|param|picture|pre|progress"
    "|q|s|samp|script|section|select|small|source|span|strong|style|sub|summary|sup"
    "|table|tbody|td|textarea|tfoot|th|thead|time|tr|track|u|ul|var|video|wbr"
)
_HTML_TAG = re.compile(rf"</?(?:{_HTML_TAG_NAMES})(?:\s[^<>]*)?/?>", re.I)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TOOL_MARKER = re.compile(r"^\s*(?:⏺|●|·|✓|✗|⎿)\s*")

# パス: /a/b/c.py, ./src/x.ts, C:\a\b.txt, src/foo/bar.py:120
# 相対パスの分岐を ASCII に限定する。\w は Unicode 対応なので、そのままだと
# 「読み/書き」のような日本語まで拾ってしまう。
_PATHISH = re.compile(
    r"(?<![\w/\\.:])((?:[A-Za-z]:\\|~/|\./|/)[\w.\-/\\+@]{2,}"
    r"|(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_.\-]+)"
    r"(:\d+(?::\d+)?)?"
)
# ルート/ドライブ/明示的な相対指定で始まるか
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:\\|~/|\./|/)")
# 末尾要素の拡張子 (.py / .tsx など)
_PATH_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")

_EMOJI_RANGES: Sequence[Tuple[int, int]] = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F),
    (0x1F1E6, 0x1F1FF),
    (0xE0100, 0xE01EF),
)

# U+20E3 は囲み keycap の結合記号 (1️⃣ の末尾)。消さないと数字だけが残る。
_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x2060, 0x20E3, 0xFEFF}

# 矢印は落とさず読み替える。消すと「入力 → 出力」が「入力出力」になる。
_ARROW_FROM = "→⇒⟶➔➜➝➞➡⇨"
_ARROW_OTHER = "←↑↓↔⇐⇑⇓⇔⟵⟷"
_ARROW_FROM_RE = re.compile(f"[{_ARROW_FROM}]")
_ARROW_OTHER_RE = re.compile(f"[{_ARROW_OTHER}]")

DEFAULT_OPTIONS: Dict[str, object] = {
    "code_blocks": "placeholder",
    "code_block_placeholder": "コードブロック。",
    "inline_code": "read",
    "strip_urls": True,
    "url_placeholder": "リンク",
    "shorten_paths": True,
    "strip_emoji": True,
    "tables": "drop",
    "max_total_chars": 0,
    "truncated_suffix": "以下省略。",
    "replacements": [],
}


def _is_emoji(ch: str) -> bool:
    code = ord(ch)
    if code in _ZERO_WIDTH:
        return True
    for low, high in _EMOJI_RANGES:
        if low <= code <= high:
            return True
    return unicodedata.category(ch) == "So"


def strip_emoji(text: str) -> str:
    return "".join(" " if _is_emoji(ch) else ch for ch in text)


def shorten_path(match: "re.Match[str]") -> str:
    body = match.group(1)
    line = match.group(2) or ""
    if "://" in body:
        return body + line
    sep = "\\" if "\\" in body and "/" not in body else "/"
    tail = body.rstrip(sep).split(sep)[-1]
    if not tail:
        return body + line
    # 「2024/09/14」「AND/OR」のような非パスを短縮しないよう、相対表記は
    # 拡張子か行番号がある場合だけパスとして扱う
    if not _ABSOLUTE_PATH.match(body) and not line and not _PATH_EXT.search(tail):
        return body
    if line:
        return f"{tail} の{line.lstrip(':').split(':')[0]}行目"
    return tail


def _remove_code_blocks(lines: List[str], mode: str, placeholder: str) -> List[str]:
    """フェンスで囲まれたコードブロックを ``mode`` に従って処理する.

    閉じフェンスが無いまま終わった場合は、コードブロックではなかったとみなして
    本文へ戻す。フェンスを含むコード例や途中で切れた応答で、以降の本文が
    まるごと消えるのを防ぐため。
    """
    out: List[str] = []
    buffered: List[str] = []
    fence_at = -1
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            if in_fence:
                in_fence = False
                buffered = []
                fence_at = -1
            else:
                in_fence = True
                buffered = []
                fence_at = len(out)
                if mode == "placeholder" and placeholder:
                    out.append(placeholder)
            continue
        if in_fence:
            buffered.append(line)
            if mode == "read":
                out.append(line)
            continue
        out.append(line)
    if in_fence:
        out = out[:fence_at] + buffered
    return out


def _handle_tables(lines: List[str], mode: str) -> List[str]:
    out: List[str] = []
    for line in lines:
        # 区切り行は読み上げようがないので、先頭パイプの無い ---|--- も落とす
        if _TABLE_SEP.match(line):
            continue
        if _TABLE_ROW.match(line):
            if mode == "drop":
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            cells = [cell for cell in cells if cell]
            out.append("、".join(cells) + "。")
            continue
        out.append(line)
    return out


def _line_prefixes(line: str) -> str:
    line = _TOOL_MARKER.sub("", line)
    line = _BLOCKQUOTE.sub("", line)
    heading = _HEADING.match(line)
    if heading:
        body = heading.group(2).strip().rstrip("#").strip()
        if body and body[-1] not in "。.!?！？、":
            body += "。"
        return body
    stripped = _BULLET.sub("", line)
    if stripped != line:
        checkbox = _CHECKBOX.match(stripped)
        if checkbox:
            done = checkbox.group(1).lower() == "x"
            stripped = ("完了、" if done else "未完了、") + _CHECKBOX.sub("", stripped)
        line = stripped.strip()
        if line and line[-1] not in "。.!?！？、,":
            line += "、"
        return line
    return line


def normalize(text: str, options: Dict[str, object] | None = None) -> str:
    """読み上げ用に整形した文字列を返す."""
    opts = dict(DEFAULT_OPTIONS)
    if options:
        opts.update({k: v for k, v in options.items() if v is not None})

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(_CODE_MARK, "")  # 退避用の目印と衝突させない
    text = _HTML_COMMENT.sub(" ", text)

    lines = text.split("\n")
    lines = _remove_code_blocks(
        lines, str(opts["code_blocks"]), str(opts["code_block_placeholder"])
    )
    lines = _handle_tables(lines, str(opts["tables"]))

    cleaned: List[str] = []
    for line in lines:
        if _HR.match(line):
            continue
        cleaned.append(_line_prefixes(line))
    text = "\n".join(cleaned)

    # リンク・画像
    text = _IMAGE.sub(lambda m: m.group(1) or "画像", text)
    text = _LINK.sub(lambda m: m.group(1) or str(opts["url_placeholder"]), text)
    text = _AUTOLINK.sub(
        lambda m: str(opts["url_placeholder"]) if opts["strip_urls"] else m.group(1), text
    )
    if opts["strip_urls"]:
        text = _BARE_URL.sub(str(opts["url_placeholder"]), text)

    # 強調・打ち消し・インラインコード
    # インラインコードを先に退避する。あとから外すと `get_last_text` の _ や
    # `2*3*4` の * を強調記号として巻き込んでしまう。
    code_spans: List[str] = []

    def stash(match: "re.Match[str]") -> str:
        code_spans.append(match.group(1))
        return f"{_CODE_MARK}{len(code_spans) - 1}{_CODE_MARK}"

    text = _INLINE_CODE.sub(stash, text)
    text = _STRIKE.sub(r"\1", text)
    text = _BOLD_ITALIC.sub(r"\2", text)
    if opts["inline_code"] == "drop":
        text = _CODE_REF.sub(" ", text)
    else:
        text = _CODE_REF.sub(lambda m: code_spans[int(m.group(1))], text)

    text = _HTML_TAG.sub(" ", text)

    if opts["shorten_paths"]:
        text = _PATHISH.sub(shorten_path, text)

    # 矢印の読み替えは絵文字の除去より先に行う (➡ などは絵文字の範囲に入る)
    text = _ARROW_FROM_RE.sub("から", text)
    text = _ARROW_OTHER_RE.sub("、", text)

    if opts["strip_emoji"]:
        text = strip_emoji(text)

    for entry in opts.get("replacements") or []:  # type: ignore[union-attr]
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        pattern, replacement = entry
        try:
            text = re.sub(str(pattern), str(replacement), text)
        except re.error:
            continue

    text = _collapse(text)

    limit = int(opts.get("max_total_chars") or 0)
    if limit > 0 and len(text) > limit:
        text = _truncate(text, limit, str(opts["truncated_suffix"]))

    return text


_CJK = re.compile(r"[^\x00-\x7f]")


def _collapse(text: str) -> str:
    text = re.sub(r"[ \t\u00a0\u3000]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    # 全角文字どうしに挟まれた空白は不要 (「splitter.py の 120行目」→「〜の120行目」)
    text = re.sub(
        r"(?<=[^\x00-\x7f]) (?=[^\x00-\x7f])",
        "",
        text,
    )
    # 句読点の直前の空白を落とす
    text = re.sub(r"[ \t]+([。、，．！？!?」』）])", r"\1", text)
    # 記号の重複を整理
    text = re.sub(r"[、,]{2,}", "、", text)
    text = re.sub(r"。[、,]", "。", text)
    text = re.sub(r"、。", "。", text)
    text = re.sub(r"。{2,}", "。", text)
    # 箇条書きの行末に付けた「、」はポーズとして残す。以前はここで全行から
    # 落としていたため、_line_prefixes での付加が意味を持っていなかった。
    lines = [line.strip() for line in text.split("\n")]
    joined = "\n".join(line for line in lines if line).strip()
    return joined.rstrip("、,")  # 末尾に垂れ下がった読点だけ落とす


def _truncate(text: str, limit: int, suffix: str) -> str:
    head = text[:limit]
    for mark in ("。", "\n", "、", " "):
        index = head.rfind(mark)
        if index >= limit // 2:
            head = head[: index + 1]
            break
    return head.rstrip() + suffix
