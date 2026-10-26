"""Deterministic Soft-channel template: #SOFT posts skip the LLM entirely.

Only posts whose text contains the ``#SOFT`` hashtag (any case) take this path.
A complete Soft parse opens exactly two market legs (TP1 and TP2, shared stop)
through :meth:`Executor.open_market`, which already applies arm, SL, sizing and
the duplicate guard. An incomplete Soft parse fails closed — recorded as an
error with a reason — and never falls through to the model. Posts without the
hashtag are left alone so the existing triage / fast-entry / LLM path runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.util.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "SOFT_PROVIDER_KIND",
    "SOFT_MODEL",
    "SoftParse",
    "has_soft_tag",
    "parse_soft",
]

#: Written onto ``runs.provider_kind`` so History shows the Soft path as template/auto.
SOFT_PROVIDER_KIND: str = "template"
#: Written onto ``runs.model`` alongside :data:`SOFT_PROVIDER_KIND`.
SOFT_MODEL: str = "soft-auto"

#: Case-insensitive hashtag. ``#software`` must not match; ``#SOFT`` / ``#soft`` must.
_SOFT_TAG: re.Pattern[str] = re.compile(r"(?i)(?:^|[^\w])#soft(?!\w)")

_SIDE_SELL: frozenset[str] = frozenset(
    {"short", "sell", "шорт", "продажа", "продавать", "продаём", "продаем"}
)
_SIDE_BUY: frozenset[str] = frozenset(
    {"long", "buy", "лонг", "покупка", "покупать", "покупаем", "покупаём"}
)

#: Prefer XAUUSD when the Soft channel prints ``GOLD | ЗОЛОТО | XAUUSD``.
_SYMBOL_ALIASES: dict[str, str] = {
    "xauusd": "XAUUSD",
    "xauusdm": "XAUUSD",
    "xauusdr": "XAUUSD",
    "gold": "XAUUSD",
    "золото": "XAUUSD",
    "xau": "XAUUSD",
}

_PRICE_TOKEN: str = (
    r"\d{1,6}(?:[.,=]\d{1,6})?"
)
_ZONE: re.Pattern[str] = re.compile(
    rf"(?P<a>{_PRICE_TOKEN})\s*[-–—/]\s*(?P<b>{_PRICE_TOKEN})"
)
_SINGLE: re.Pattern[str] = re.compile(rf"(?P<a>{_PRICE_TOKEN})")

_ENTRY_LABEL: re.Pattern[str] = re.compile(
    r"(?is)(?:зона\s*входа|entry\s*zone|entry(?:\s*price)?)\s*[:\-]?\s*(?P<body>[^\n]+)"
)
_SL_LABEL: re.Pattern[str] = re.compile(
    r"(?is)(?:стоп(?:\s*[-–—]?\s*лосс)?|stop(?:\s*loss)?|\bsl\b)\s*[:\-]?\s*(?P<body>[^\n]+)"
)
_TP1_LABEL: re.Pattern[str] = re.compile(
    r"(?is)(?:\btp\s*1\b|т[pр]\s*1|take\s*profit\s*1)\s*[:\-]?\s*(?P<body>[^\n]+)"
)
_TP2_LABEL: re.Pattern[str] = re.compile(
    r"(?is)(?:\btp\s*2\b|т[pр]\s*2|take\s*profit\s*2)\s*[:\-]?\s*(?P<body>[^\n]+)"
)


@dataclass(frozen=True)
class SoftParse:
    """Result of reading a Soft-tagged post. ``ok`` means every required field is present."""

    ok: bool
    reason: str | None = None
    side: str | None = None
    symbol: str | None = None
    entry: float | None = None
    entry_high: float | None = None
    sl: float | None = None
    tp1: float | None = None
    tp2: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form for a run step payload."""
        return {
            "ok": self.ok,
            "reason": self.reason,
            "side": self.side,
            "symbol": self.symbol,
            "entry": self.entry,
            "entry_high": self.entry_high,
            "sl": self.sl,
            "tp1": self.tp1,
            "tp2": self.tp2,
        }


def has_soft_tag(text: str) -> bool:
    """True when the post carries the ``#SOFT`` hashtag (any case), as a whole tag."""
    return _SOFT_TAG.search(text or "") is not None


def _parse_price_token(raw: str) -> float | None:
    """Parse one Soft price print. ``2654=20`` and ``2654,20`` are 2654.20."""
    text = re.sub(r"\s+", "", (raw or "").strip()).replace("＝", "=")
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,6})([.,=])(\d{1,6})", text)
    if match is not None:
        left, _sep, right = match.group(1), match.group(2), match.group(3)
        value = float(f"{int(left)}.{right}")
        return value if value > 0.0 else None
    if re.fullmatch(r"\d{1,6}(?:\.\d{1,5})?", text):
        value = float(text)
        return value if value > 0.0 else None
    return None


def _parse_price_or_zone(body: str) -> tuple[float | None, float | None]:
    """Return ``(low, high)`` for a zone, or ``(price, None)`` for a single print."""
    text = (body or "").strip()
    if not text:
        return None, None
    # Drop a trailing TP3 / noise word so "2654.2 TP3 ..." still yields the price.
    text = re.split(r"(?i)\b(?:tp\s*3|т[pр]\s*3|#)\b", text, maxsplit=1)[0].strip()
    zone = _ZONE.search(text)
    if zone is not None:
        low = _parse_price_token(zone.group("a"))
        high = _parse_price_token(zone.group("b"))
        if low is None or high is None:
            return None, None
        return (min(low, high), max(low, high))
    single = _SINGLE.search(text)
    if single is None:
        return None, None
    value = _parse_price_token(single.group("a"))
    return value, None


def _mid(low: float | None, high: float | None) -> float | None:
    if low is None:
        return None
    if high is None:
        return low
    return round((low + high) / 2.0, 5)


def _find_side(text: str) -> str | None:
    """Return ``buy`` / ``sell`` from Soft side words, or None when ambiguous/missing."""
    # Tokenise on non-letters so "ШОРТ," and "#SOFT" stay separate.
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё]+", text or "")
    found: set[str] = set()
    for token in tokens:
        key = token.casefold()
        if key in _SIDE_SELL:
            found.add("sell")
        elif key in _SIDE_BUY:
            found.add("buy")
    if len(found) == 1:
        return next(iter(found))
    return None


def _find_symbol(text: str) -> str | None:
    """Prefer ``XAUUSD`` from a Soft instrument line; fall back to GOLD / ЗОЛОТО."""
    upper = (text or "").upper()
    # Pipe-separated Soft banner: GOLD | ЗОЛОТО | XAUUSD — take XAUUSD when present.
    for chunk in re.split(r"[|\n,/]+", upper):
        token = re.sub(r"[^A-Z0-9А-ЯЁ]", "", chunk.strip())
        # Latinise the Cyrillic gold token for the alias table.
        folded = chunk.strip().casefold()
        if "xauusd" in folded or token.startswith("XAUUSD"):
            return "XAUUSD"
    # Second pass: any known gold alias anywhere in the post.
    for match in re.finditer(r"[A-Za-zА-Яа-яЁё0-9]+", text or ""):
        key = match.group(0).casefold()
        if key in _SYMBOL_ALIASES:
            return _SYMBOL_ALIASES[key]
    return None


def _labeled_price(
    pattern: re.Pattern[str], text: str, *, zone: bool = False
) -> tuple[float | None, float | None]:
    match = pattern.search(text or "")
    if match is None:
        return None, None
    low, high = _parse_price_or_zone(match.group("body"))
    if zone:
        return low, high
    if low is None:
        return None, None
    # For SL / TP a zone is unexpected; take the first number.
    return low, None


def parse_soft(text: str) -> SoftParse:
    """Parse a Soft-tagged post. Call only when :func:`has_soft_tag` is true.

    Required: side, symbol, entry (Зона входа), SL (Стоп), TP1, TP2. TP3 is ignored.
    Missing any required field returns ``ok=False`` with a reason — the caller must
    fail closed and must not hand the post to the LLM.
    """
    raw = text or ""
    missing: list[str] = []

    side = _find_side(raw)
    if side is None:
        missing.append("side (ШОРТ/ЛОНГ/SHORT/LONG/BUY/SELL)")

    symbol = _find_symbol(raw)
    if symbol is None:
        missing.append("symbol (GOLD | ЗОЛОТО | XAUUSD)")

    entry_low, entry_high = _labeled_price(_ENTRY_LABEL, raw, zone=True)
    entry = _mid(entry_low, entry_high)
    if entry is None:
        missing.append("entry (Зона входа)")

    sl, _ = _labeled_price(_SL_LABEL, raw)
    if sl is None:
        missing.append("SL (Стоп)")

    tp1, _ = _labeled_price(_TP1_LABEL, raw)
    if tp1 is None:
        missing.append("TP1")

    tp2, _ = _labeled_price(_TP2_LABEL, raw)
    if tp2 is None:
        missing.append("TP2")

    if missing:
        reason = "soft template incomplete: missing " + ", ".join(missing)
        log.info("soft_template_incomplete reason=%s", reason)
        return SoftParse(ok=False, reason=reason)

    assert side is not None and symbol is not None
    assert entry is not None and sl is not None and tp1 is not None and tp2 is not None
    return SoftParse(
        ok=True,
        side=side,
        symbol=symbol,
        entry=entry,
        entry_high=entry_high,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
    )
