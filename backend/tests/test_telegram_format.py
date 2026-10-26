"""Markdown → Telegram HTML: tables as ``<pre>``, escaping, and clean splits."""

from __future__ import annotations

from app.telegram.format import (
    html_to_plain,
    markdown_to_telegram_html,
    prepare_telegram_html,
    split_telegram_html,
    telegram_html_is_safe,
)


def test_headings_bold_italic_code_and_links() -> None:
    html = markdown_to_telegram_html(
        "# Status\n\nGo **long** with *size* on `XAUUSDr` — see [the post](https://t.me/c/1/2)."
    )
    assert "<b>Status</b>" in html
    assert "<b>long</b>" in html
    assert "<i>size</i>" in html
    assert "<code>XAUUSDr</code>" in html
    assert '<a href="https://t.me/c/1/2">the post</a>' in html


def test_lists_become_bullets() -> None:
    html = markdown_to_telegram_html("- first\n- second\n1. numbered")
    assert "• first" in html
    assert "• second" in html
    assert "1. numbered" in html


def test_markdown_table_becomes_aligned_pre_block() -> None:
    source = (
        "| Symbol | Side | Entry |\n"
        "| --- | ---: | :---: |\n"
        "| XAUUSDr | BUY | 2380.50 |\n"
        "| EURUSDr | SELL | 1.08500 |\n"
    )
    html = markdown_to_telegram_html(source)
    assert html.startswith("<pre>")
    assert html.endswith("</pre>")
    assert telegram_html_is_safe(html)
    plain = html_to_plain(html)
    lines = plain.splitlines()
    assert "Symbol" in lines[0] and "Side" in lines[0] and "Entry" in lines[0]
    assert set(lines[1]) <= set("- ")
    assert "XAUUSDr" in lines[2] and "BUY" in lines[2]
    # Columns stay lined up: each data row shares the header's character columns.
    header_side = lines[0].index("Side")
    assert lines[2][header_side:header_side + 3] == "BUY"
    assert "<table>" not in html
    assert "|" not in plain


def test_unsafe_tags_are_escaped_and_bold_still_applies() -> None:
    html = markdown_to_telegram_html("drop <script>alert(1)</script> **now** & go")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>now</b>" in html
    assert "&amp; go" in html
    assert telegram_html_is_safe(html)


def test_javascript_url_is_not_an_anchor() -> None:
    html = markdown_to_telegram_html("[x](javascript:alert(1))")
    assert "<a " not in html
    assert "javascript:alert(1)" in html_to_plain(html)


def test_fenced_code_is_preformatted() -> None:
    html = markdown_to_telegram_html("```\nselect * from trades\n```")
    assert html == "<pre>select * from trades</pre>"


def test_existing_telegram_html_is_kept() -> None:
    original = "<b>OPENED</b> — <code>XAUUSDm</code>"
    assert prepare_telegram_html(original) == original


def test_plain_text_is_escaped() -> None:
    assert prepare_telegram_html("a < b") == "a &lt; b"


def test_split_prefers_paragraph_breaks_and_stays_under_the_limit() -> None:
    block = "word " * 200
    source = f"<b>One</b>\n\n{block}\n\n<b>Two</b>\n\n{block}"
    parts = split_telegram_html(source, limit=900)
    assert len(parts) >= 2
    assert all(len(part) <= 900 for part in parts)
    assert all(telegram_html_is_safe(part) for part in parts)
    assert "<b>One</b>" in parts[0]


def test_oversized_pre_is_split_and_rewrapped() -> None:
    inner = "A" * 120 + "\n" + "B" * 120
    source = f"<pre>{inner}</pre>"
    parts = split_telegram_html(source, limit=80)
    assert len(parts) >= 2
    assert all(part.startswith("<pre>") for part in parts)
    assert all(part.endswith("</pre>") for part in parts)
    assert all(telegram_html_is_safe(part) for part in parts)
    rejoined = "".join(html_to_plain(part) for part in parts)
    assert "A" * 120 in rejoined
    assert "B" * 120 in rejoined
