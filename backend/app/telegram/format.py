"""Convert operator/AI markdown into Telegram HTML.

Classic sendMessage HTML has no tables and no headings. This module maps
markdown-like structure onto tags Telegram will actually render, and turns GFM
tables into aligned ``<pre>`` blocks (the only way columns stay lined up).

Every interpolated fragment is escaped. The output is safe to send with
``parse_mode=HTML``; a stray ``<`` from the model cannot become a tag.
"""

from __future__ import annotations

import html
import re
from typing import Final

__all__ = [
    "html_to_plain",
    "markdown_to_telegram_html",
    "prepare_telegram_html",
    "split_telegram_html",
    "telegram_html_is_safe",
    "telegram_html_to_rich_html",
]

#: Telegram's documented HTML tags. Anything else in a ``<`` position is treated
#: as text that still needs escaping, not as markup we produced.
_ALLOWED_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|span|tg-spoiler|tg-emoji|a|code|pre|blockquote)"
    r"(?:\s[^<>]*)?>",
    re.IGNORECASE,
)
_ANY_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^<>]*>")

_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^(\s*)(```|~~~)(.*)$")
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LIST_RE: Final[re.Pattern[str]] = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.+)$")
_QUOTE_RE: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}>\s?(.*)$")
_HR_RE: Final[re.Pattern[str]] = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$"
)
_CODE_RE: Final[re.Pattern[str]] = re.compile(r"`([^`]+)`")
_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE: Final[re.Pattern[str]] = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_STRIKE_RE: Final[re.Pattern[str]] = re.compile(r"~~(.+?)~~")
_ITALIC_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")

_SAFE_URL_PREFIXES: Final[tuple[str, ...]] = ("http://", "https://", "tg://")

#: Leave a little headroom under Telegram's 4096-character cap so a trailing
#: newline or an appended source-post link still fits.
DEFAULT_LIMIT: Final[int] = 4096


def telegram_html_is_safe(text: str) -> bool:
    """True when every ``<`` in `text` opens a tag Telegram's HTML parser accepts."""
    pos = text.find("<")
    while pos != -1:
        match = _ALLOWED_TAG_RE.match(text, pos)
        if match is None:
            return False
        pos = text.find("<", match.end())
    return True


def html_to_plain(text: str) -> str:
    """Strip tags and unescape entities, leaving the readable text."""
    return html.unescape(_ANY_TAG_RE.sub("", text))


def prepare_telegram_html(text: str) -> str:
    """Keep already-safe Telegram HTML; otherwise convert markdown and escape."""
    if "<" in text and telegram_html_is_safe(text):
        return text
    return markdown_to_telegram_html(text)


def markdown_to_telegram_html(text: str) -> str:
    """Map markdown-like text onto Telegram HTML. Always returns escaped markup."""
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    blocks: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(2)
            index += 1
            body: list[str] = []
            while index < total and not lines[index].lstrip().startswith(marker):
                body.append(lines[index])
                index += 1
            if index < total:
                index += 1
            blocks.append(_pre("\n".join(body)))
            continue
        if _looks_like_table_start(lines, index):
            table_lines: list[str] = []
            while index < total and (_is_table_row(lines[index]) or _is_table_separator(lines[index])):
                table_lines.append(lines[index])
                index += 1
            rendered = _render_table(table_lines)
            if rendered:
                blocks.append(rendered)
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            blocks.append(f"<b>{_inline(heading.group(2).strip())}</b>")
            index += 1
            continue
        if _LIST_RE.match(line):
            items: list[str] = []
            while index < total and _LIST_RE.match(lines[index]):
                items.append(lines[index])
                index += 1
            blocks.append(_render_list(items))
            continue
        if _QUOTE_RE.match(line):
            quoted: list[str] = []
            while index < total:
                match = _QUOTE_RE.match(lines[index])
                if match is None:
                    break
                quoted.append(match.group(1))
                index += 1
            inner = _inline("\n".join(quoted).strip())
            blocks.append(f"<blockquote>{inner}</blockquote>" if inner else "")
            continue
        if _HR_RE.match(line) and not _is_table_separator(line):
            blocks.append("────────")
            index += 1
            continue
        if not line.strip():
            index += 1
            continue
        paragraph: list[str] = [line]
        index += 1
        while index < total and not _is_block_boundary(lines, index):
            paragraph.append(lines[index])
            index += 1
        blocks.append(_inline("\n".join(paragraph)))
    return "\n".join(block for block in blocks if block)


def split_telegram_html(text: str, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Split HTML so each part is at most `limit` characters, preferring paragraph breaks.

    Preformatted blocks stay intact when they fit; an oversized ``<pre>`` is split
    inside its body and re-wrapped so each piece remains valid HTML.
    """
    cap = max(32, int(limit))
    source = text.strip("\n")
    if not source:
        return [""]
    if len(source) <= cap:
        return [source]

    parts: list[str] = []
    remaining = source
    while remaining:
        if len(remaining) <= cap:
            parts.append(remaining)
            break
        chunk, remaining = _take_chunk(remaining, cap)
        if chunk:
            parts.append(chunk)
            continue
        parts.append(remaining[:cap])
        remaining = remaining[cap:]
    return [part for part in parts if part] or [""]


# --------------------------------------------------------------------------------------
# Block helpers
# --------------------------------------------------------------------------------------


def _is_table_separator(line: str) -> bool:
    return bool(_TABLE_SEP_RE.match(line))


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    if stripped.count("|") < 2:
        return False
    return not _is_table_separator(stripped)


def _looks_like_table_start(lines: list[str], index: int) -> bool:
    if index >= len(lines) or not _is_table_row(lines[index]):
        return False
    if index + 1 >= len(lines):
        return False
    nxt = lines[index + 1]
    return _is_table_separator(nxt) or _is_table_row(nxt)


def _is_block_boundary(lines: list[str], index: int) -> bool:
    if index >= len(lines):
        return True
    line = lines[index]
    if not line.strip():
        return True
    if _FENCE_RE.match(line) or _HEADING_RE.match(line) or _LIST_RE.match(line):
        return True
    if _QUOTE_RE.match(line) or _HR_RE.match(line):
        return True
    return _looks_like_table_start(lines, index)


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _cell_plain(cell: str) -> str:
    """Table cells render as plain text inside ``<pre>`` so columns stay aligned."""
    text = cell.strip()
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2) or "", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("~~", "")
    return " ".join(text.split())


def _render_table(lines: list[str]) -> str:
    rows: list[list[str]] = []
    for line in lines:
        if _is_table_separator(line):
            continue
        if not _is_table_row(line):
            continue
        rows.append([_cell_plain(cell) for cell in _split_table_row(line)])
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    for row in rows:
        if len(row) < width:
            row.extend([""] * (width - len(row)))
    col_widths = [max(len(row[index]) for row in rows) for index in range(width)]
    rendered: list[str] = []
    for row_index, row in enumerate(rows):
        rendered.append("  ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(row)).rstrip())
        if row_index == 0 and len(rows) > 1:
            rendered.append("  ".join("-" * col_widths[i] for i in range(width)))
    return _pre("\n".join(rendered))


def _render_list(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        match = _LIST_RE.match(line)
        if match is None:
            continue
        marker = match.group(2)
        body = _inline(match.group(3).strip())
        if marker[-1] in ".)" and marker[:-1].isdigit():
            out.append(f"{marker} {body}")
        else:
            out.append(f"• {body}")
    return "\n".join(out)


def _pre(body: str) -> str:
    return f"<pre>{html.escape(body, quote=False)}</pre>"


# --------------------------------------------------------------------------------------
# Inline markdown
# --------------------------------------------------------------------------------------


def _inline(text: str) -> str:
    """Escape `text` and wrap recognised markdown spans in Telegram HTML tags."""
    stash: list[str] = []

    def hold(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    def restore(value: str) -> str:
        for index, fragment in enumerate(stash):
            value = value.replace(f"\x00{index}\x00", fragment)
        return value

    def code_sub(match: re.Match[str]) -> str:
        return hold(f"<code>{html.escape(match.group(1), quote=False)}</code>")

    def link_sub(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2).strip()
        lowered = url.lower()
        if not lowered.startswith(_SAFE_URL_PREFIXES):
            return html.escape(match.group(0), quote=False)
        safe_url = html.escape(url, quote=True)
        safe_label = html.escape(label, quote=False)
        return hold(f'<a href="{safe_url}">{safe_label}</a>')

    def bold_sub(match: re.Match[str]) -> str:
        inner = match.group(1) if match.group(1) is not None else match.group(2) or ""
        return hold(f"<b>{html.escape(inner, quote=False)}</b>")

    def strike_sub(match: re.Match[str]) -> str:
        return hold(f"<s>{html.escape(match.group(1), quote=False)}</s>")

    def italic_sub(match: re.Match[str]) -> str:
        return hold(f"<i>{html.escape(match.group(1), quote=False)}</i>")

    working = _CODE_RE.sub(code_sub, text)
    working = _LINK_RE.sub(link_sub, working)
    working = _BOLD_RE.sub(bold_sub, working)
    working = _STRIKE_RE.sub(strike_sub, working)
    working = _ITALIC_RE.sub(italic_sub, working)
    return restore(html.escape(working, quote=False))


# --------------------------------------------------------------------------------------
# Splitting
# --------------------------------------------------------------------------------------


def _entity_safe_end(window: str) -> str:
    """Drop a trailing unterminated tag or HTML entity so a cut cannot break markup."""
    last_lt = window.rfind("<")
    last_gt = window.rfind(">")
    if last_lt > last_gt:
        window = window[:last_lt]
    last_amp = window.rfind("&")
    last_sc = window.rfind(";")
    if last_amp > last_sc:
        window = window[:last_amp]
    return window


def _best_cut(text: str, limit: int) -> int:
    window = _entity_safe_end(text[:limit])
    if not window:
        return min(limit, len(text))
    floor = max(32, limit // 5)
    for sep in ("\n\n", "\n", " "):
        pos = window.rfind(sep)
        if pos >= floor:
            return pos + (2 if sep == "\n\n" else 1)
    return len(window)


def _open_pre_extra(chunk: str) -> int:
    """How many ``<pre>`` tags in `chunk` are still unclosed."""
    lowered = chunk.lower()
    return lowered.count("<pre") - lowered.count("</pre>")


def _take_chunk(text: str, limit: int) -> tuple[str, str]:
    cut = _best_cut(text, limit)
    if cut <= 0:
        return text[:limit], text[limit:]
    chunk = text[:cut]
    rest = text[cut:]
    extra_open = _open_pre_extra(chunk)
    if extra_open > 0:
        closer = "</pre>" * extra_open
        if len(chunk) + len(closer) > limit:
            cut = _best_cut(text, limit - len(closer))
            chunk = text[:cut]
            rest = text[cut:]
            extra_open = _open_pre_extra(chunk)
            closer = "</pre>" * extra_open if extra_open > 0 else ""
        if extra_open > 0:
            chunk = chunk + closer
            rest = ("<pre>" * extra_open) + rest
    return chunk.rstrip("\n"), rest.lstrip("\n")


def telegram_html_to_rich_html(text: str) -> str:
    """Give prepared Telegram HTML block structure without breaking multiline entities."""
    blocks: list[str] = []
    pending: list[str] = []
    stack: list[str] = []
    for line in text.splitlines():
        pending.append(line)
        fragment = "\n".join(pending).strip()
        if not fragment:
            pending.clear()
            continue
        for match in re.finditer(r"<(/?)([a-z-]+)\b[^>]*>", line, re.IGNORECASE):
            closing, name = match.group(1), match.group(2).lower()
            if closing:
                if stack and stack[-1] == name:
                    stack.pop()
            else:
                stack.append(name)
        if stack or not telegram_html_is_safe(fragment):
            continue
        if fragment.startswith(("<pre>", "<pre ", "<blockquote>", "<blockquote ")):
            blocks.append(fragment)
        else:
            tag = "h3" if not blocks else "p"
            blocks.append(f"<{tag}>{fragment}</{tag}>")
        pending.clear()
    if pending:
        # Defensive: the normal caller has already prepared and balanced this HTML.
        blocks.append(f"<p>{html.escape(html_to_plain(chr(10).join(pending)), quote=False)}</p>")
    return "".join(blocks)
