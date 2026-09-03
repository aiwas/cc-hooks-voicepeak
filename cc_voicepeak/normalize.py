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
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?")
_CHECKBOX = re.compile(r"^\[([ xX])\]\s*")

_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_AUTOLINK = re.compile(r"<((?:https?|ftp)://[^>\s]+)>")
_BARE_URL = re.compile(r"(?:https?|ftp)://[^\s<>()\[\]「」『』、。]+")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD_ITALIC = re.compile(r"(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1")
_STRIKE = re.compile(r"~~(.+?)~~")
_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TOOL_MARKER = re.compile(r"^\s*(?:⏺|●|·|✓|✗|⎿)\s*")

# パス: /a/b/c.py, ./src/x.ts, C:\a\b.txt, src/foo/bar.py:120
_PATHISH = re.compile(
    r"(?<![\w/\\.:])((?:[A-Za-z]:\\|~/|\./|/)[\w.\-/\\+@]{2,}|(?:[\w.\-]+/){1,}[\w.\-]+)"
    r"(:\d+(?::\d+)?)?"
)

_EMOJI_RANGES: Sequence[Tuple[int, int]] = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F),
    (0x1F1E6, 0x1F1FF),
    (0x2190, 0x21FF),
    (0x2900, 0x297F),
    (0xE0100, 0xE01EF),
)

_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF}

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
    if line:
        return f"{tail} の{line.lstrip(':').split(':')[0]}行目"
    return tail


def _remove_code_blocks(lines: List[str], mode: str, placeholder: str) -> List[str]:
    out: List[str] = []
    in_fence = False
    for line in lines:
        fence = _FENCE.match(line)
        if fence:
            in_fence = not in_fence
            if in_fence and mode == "placeholder":
                out.append(placeholder)
            continue
        if in_fence:
            if mode == "read":
                out.append(line)
            continue
        out.append(line)
    return out


def _handle_tables(lines: List[str], mode: str) -> List[str]:
    out: List[str] = []
    for line in lines:
        if _TABLE_SEP.match(line) and _TABLE_ROW.match(line):
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
    text = _STRIKE.sub(r"\1", text)
    text = _BOLD_ITALIC.sub(r"\2", text)
    if opts["inline_code"] == "drop":
        text = _INLINE_CODE.sub(" ", text)
    else:
        text = _INLINE_CODE.sub(r"\1", text)

    text = _HTML_TAG.sub(" ", text)

    if opts["shorten_paths"]:
        text = _PATHISH.sub(shorten_path, text)

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
    lines = [line.strip().rstrip("、,") for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _truncate(text: str, limit: int, suffix: str) -> str:
    head = text[:limit]
    for mark in ("。", "\n", "、", " "):
        index = head.rfind(mark)
        if index >= limit // 2:
            head = head[: index + 1]
            break
    return head.rstrip() + suffix
