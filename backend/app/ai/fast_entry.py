"""Deterministic entry that reaches the broker before the full agent loop.

Triage still decides whether the post is a signal. When the answer is ``yes``, this
module reads the side and the printed entry, quotes the symbol once, and either calls
``open_market`` or leaves the post alone. It does not call the model, it does not read
candles, it does not place a pending order, and it does not change ``MAX_TOKENS`` or
``MAX_TOOL_ITERATIONS``.

The channel usually writes ``XAUUSD BUY 4316=44`` and omits the word "now". ``=`` with
a short fractional tail is a decimal point, so that price is 4316.44. The quote check
then decides:

* Explicit immediate language with no printed price (``buy now``, ``sell now``,
  ``at market``) is a market open. A printed price still has to sit inside the band.
  Farther than that is a miss, not a pending order, even when the post says now.
* A printed entry still close to the live fill is a market open. The channel posted
  while price was there; a late read should not refuse a near fill.
* A quote past the band is a miss. No limit or stop is left hanging. The executor
  refuses ``open_market`` and ``place_pending`` for the same print, so the full agent
  cannot market-chase it either.
* A post with a side and no entry price is not given a price from the quote. Market
  only when immediacy is explicit; otherwise wait for the levels.
* A post older than :data:`app.config.STALE_SIGNAL_SECONDS` (including an old catch-up)
  is not chased at market.

"Close" on a classic metal entry (fewer than four take-profits, including a bare
price) is ``trading.max_entry_drift_usd`` (default 5 price units — five dollars on
XAUUSD). A ladder of four or more targets keeps
:data:`app.config.FAST_ENTRY_METAL_MARKET_DISTANCE` (default ``7.0``). Every other
symbol uses :data:`app.config.FAST_ENTRY_DEFAULT_MARKET_PIPS` (default 5) times the
resolved pip size. A buy is measured against the ask, a sell against the bid. Past
that band, or older than the stale window, the open is refused. Either failure is enough.

Stops and targets are taken only from labeled prints (``SL``, ``TP1``, ``LONG`` as a
ceiling). Nothing is invented. An incomplete teaser omits ``sl`` and the executor
sizes it from ``trading.ladder_volume``. A reply whose chain already has naked legs
is planned as an ``open_market`` carrying the posted stop, which the executor turns
into ``modify_existing`` instead of a second pair. That modify is not a market chase,
so a late ladder reply may still attach levels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from app.config import settings
from app.db import session_scope
from app.operator_settings import MAX_ENTRY_DRIFT_USD_DEFAULT, max_entry_drift_usd
from app.trading.pricing import LADDER_MIN_TARGETS
from app.trading.symbols import ALIASES
from app.util.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "ALREADY_SENT_TITLE",
    "FastPlan",
    "FastRead",
    "FastSkip",
    "Route",
    "decide_route",
    "market_distance_limit",
    "parse_price_token",
    "plan_fast_entry",
    "read_post",
]

#: Heading injected into the user turn once the broker has accepted the fast order.
ALREADY_SENT_TITLE: str = "FAST ENTRY ALREADY SENT"

_CCY: frozenset[str] = frozenset(
    {
        "EUR",
        "GBP",
        "USD",
        "AUD",
        "NZD",
        "CAD",
        "CHF",
        "JPY",
        "XAU",
        "XAG",
        "XPT",
        "XPD",
        "BTC",
        "ETH",
        "XRP",
        "LTC",
    }
)
_SKIP: frozenset[str] = frozenset(
    {
        "BUY",
        "SELL",
        "LONG",
        "SHORT",
        "NOW",
        "SL",
        "TP",
        "STOP",
        "STOPLOSS",
        "LOSS",
        "TARGET",
        "HIT",
        "ENTRY",
        "MARKET",
        "LIMIT",
        "PENDING",
        "CLOSE",
        "CLOSED",
        "PARTIAL",
        "MOVE",
        "HOLD",
        "CHECK",
        "TOUCH",
        "COMPLETE",
        "SMASHED",
        "THE",
        "AND",
        "FOR",
        "WITH",
    }
)
_METAL_MARKERS: tuple[str, ...] = ("XAU", "XAG", "XPT", "XPD", "GOLD", "SILVER", "METAL")

# A price token. The first alternative is a decimal or an ``=`` fraction (4316=44,
# 4316.44, 1.0850). The second is a zone (4310-4320). The third is a bare print
# (4316) — at least three digits so a TP index is not a price.
_NUMBER: str = (
    r"\d{1,6}(?:\s*[=.,]\s*\d{1,5})?"
    r"|\d{3,6}\s*[-/–]\s*\d{3,6}"
)
_PRICE_FIND: re.Pattern[str] = re.compile(
    rf"\d{{1,6}}\s*[=.,]\s*\d{{1,5}}|\d{{3,6}}\s*[-/–]\s*\d{{3,6}}|\d{{3,6}}"
)
_SL_RE: re.Pattern[str] = re.compile(
    rf"(?:#\s*)?(?:SL|STOPLOSS|STOP\s+LOSS)\s*[:=]?\s*({_NUMBER})"
)
# The index is optional and must not eat the price. ``TP 4332`` used to bind the
# index group to ``43`` and the price to ``32`` because ``\d{0,2}`` sat in front of
# the number with only optional space between them. An index is a 1–2 digit token
# that is not the start of a longer number (``TP1 4324``, ``TP 1 4324``). A bare
# ``TP 4332`` is the price 4332.
_TP_RE: re.Pattern[str] = re.compile(
    rf"(?:#\s*)?(?:TP|TAKE\s+PROFIT|TARGET)"
    rf"(?:\s*(\d{{1,2}})(?!\d))?\s*[:=]?\s*({_NUMBER})"
)
#: A dropped metal decimal is recovered only when the corrected level sits this
#: close to the anchor. 42672 beside ~4285 is 4267.2, about $18. Wider than a
#: typical 8–15 dollar gold stop, and still the same price rather than a guess.
_METAL_DROPPED_DECIMAL_MAX: float = 40.0
_LONG_RE: re.Pattern[str] = re.compile(rf"\b(LONG|SHORT)\s*[:=]?\s*({_NUMBER})")
_TOKEN: re.Pattern[str] = re.compile(r"[A-Z][A-Z0-9.]{1,15}")
_NOW: re.Pattern[str] = re.compile(r"\bNOW\b|\bAT\s+MARKET\b|\bMARKET\s+ORDER\b")
_MANAGEMENT: re.Pattern[str] = re.compile(
    r"\b(?:CLOSE|CLOSED|CANCEL|PARTIAL|MOVE|HIT|SMASHED|COMPLETE|BREAKEVEN|BREAK\s+EVEN)\b"
)
_EMOJI: re.Pattern[str] = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FE0F"
    "\U0001F1E6-\U0001F1FF"
    "]+"
)


@dataclass(frozen=True)
class FastRead:
    """What the post itself prints. ``fresh`` is a new entry the fast path may open."""

    symbol: str | None
    side: str | None
    entry: float | None
    zone: tuple[float, float] | None
    sl: float | None
    tps: tuple[float, ...]
    now_hint: bool
    fresh: bool
    ambiguous: bool


@dataclass(frozen=True)
class Route:
    """Market versus pending, after the live quote is known."""

    action: str
    side: str | None = None
    entry: float | None = None
    pending_price: float | None = None
    order_type: str | None = None
    distance: float | None = None
    limit: float | None = None
    sl: float | None = None
    tps: tuple[float, ...] = ()
    now_hint: bool = False
    reason: str = ""


@dataclass(frozen=True)
class FastSkip:
    """The fast path looked and will not send an order. The note is for the later agent."""

    reason: str
    note: str
    distance: float | None = None
    limit: float | None = None
    entry: float | None = None
    side: str | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class FastPlan:
    """One broker call. ``intent`` is what we mean; the executor may answer ``modify_existing``."""

    tool: str
    arguments: dict[str, Any]
    intent: str
    distance: float | None
    limit: float | None
    entry: float | None
    side: str
    symbol: str
    now_hint: bool


def parse_price_token(raw: str) -> float | tuple[float, float] | None:
    """Parse one price print.

    ``4316=44`` is 4316.44: a short tail after ``=`` is the fractional part, which is
    how this channel writes a price without a dot. ``4316=4320`` (four or more digits
    on the right) and ``4310-4320`` are zones. A comma is a decimal point.
    """
    text = re.sub(r"\s+", "", (raw or "").strip()).replace("–", "-").replace("—", "-")
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,6})([=.,/\-])(\d{1,6})", text)
    if match is not None:
        left, sep, right = match.group(1), match.group(2), match.group(3)
        if sep in "-/" and len(left) >= 3 and len(right) >= 3:
            low, high = float(left), float(right)
            if low != high:
                return (min(low, high), max(low, high))
        if sep == "=" and len(right) <= 3:
            return float(f"{int(left)}.{right}")
        if sep == "=" and len(right) >= 4:
            low, high = float(left), float(right)
            if low != high:
                return (min(low, high), max(low, high))
        if sep in ".,":
            return float(f"{int(left)}.{right}")
        return None
    if re.fullmatch(r"\d{1,6}(?:\.\d{1,5})?", text):
        value = float(text)
        if value > 0.0:
            return value
    return None


def _normalize(text: str) -> str:
    """Upper-case the post and drop emoji, keeping the price punctuation."""
    cleaned = _EMOJI.sub(" ", text or "")
    cleaned = cleaned.replace("＝", "=").replace("–", "-").replace("—", "-")
    return cleaned.upper()


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace spans with spaces so a labeled stop cannot be read as the entry."""
    chars = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(chars), end)):
            chars[index] = " "
    return "".join(chars)


def _skip_token(token: str) -> bool:
    if token in _SKIP:
        return True
    if re.fullmatch(r"TP\d{0,2}", token):
        return True
    if re.fullmatch(r"SL\d+", token) or re.fullmatch(r"STOP\d+", token):
        return True
    return False


def _looks_like_symbol(token: str) -> bool:
    """True for a channel instrument (``XAUUSD``, ``GOLD``, ``EURUSDm``), not for ``COMPLETE``."""
    if _skip_token(token):
        return False
    if token in ALIASES:
        return True
    compact = re.sub(r"[^A-Z0-9]", "", token)
    if compact in ALIASES:
        return True
    if re.fullmatch(r"(?:XAU|XAG|XPT|XPD)[A-Z]{3}[A-Z0-9]{0,4}", compact):
        return True
    if re.fullmatch(r"[A-Z]{6}[A-Z]{0,3}", compact):
        return compact[:3] in _CCY and compact[3:6] in _CCY
    return False


def _is_target_word(text: str, start: int, *, buy_sell: str | None) -> bool:
    """``LONG 4450`` after a ladder is a ceiling. ``XAUUSD LONG 4316`` is a side."""
    if buy_sell:
        return True
    return bool(re.search(r"\d\s*$", text[:start]))


def _direction(text: str) -> tuple[str | None, bool]:
    """Return ``(side, ambiguous)``. Both BUY and SELL in one post is ambiguous."""
    has_buy = re.search(r"\bBUY\b", text) is not None
    has_sell = re.search(r"\bSELL\b", text) is not None
    if has_buy and has_sell:
        return None, True
    if has_buy:
        return "buy", False
    if has_sell:
        return "sell", False
    for match in re.finditer(r"\b(LONG|SHORT)\b", text):
        if _is_target_word(text, match.start(), buy_sell=None):
            continue
        return ("buy" if match.group(1) == "LONG" else "sell"), False
    return None, False


def _side_index(text: str, side: str | None) -> int | None:
    if side == "buy":
        match = re.search(r"\bBUY\b", text) or re.search(r"\bLONG\b", text)
    elif side == "sell":
        match = re.search(r"\bSELL\b", text) or re.search(r"\bSHORT\b", text)
    else:
        return None
    return None if match is None else match.start()


def _as_price(value: float | tuple[float, float] | None) -> tuple[float | None, tuple[float, float] | None]:
    if isinstance(value, tuple):
        return None, value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), None
    return None, None


def read_post(text: str) -> FastRead:
    """Read side, symbol, entry, and only the levels the channel labeled."""
    raw = _normalize(text)
    side, ambiguous = _direction(raw)
    buy_sell = side if re.search(r"\b(?:BUY|SELL)\b", raw) else None
    spans: list[tuple[int, int]] = []
    sl: float | None = None
    tps: list[tuple[int, float]] = []

    for match in _SL_RE.finditer(raw):
        parsed = parse_price_token(match.group(1))
        price, _zone = _as_price(parsed)
        if price is not None and sl is None:
            sl = price
        spans.append((match.start(), match.end()))

    for position, match in enumerate(_TP_RE.finditer(raw)):
        parsed = parse_price_token(match.group(2))
        price, _zone = _as_price(parsed)
        if price is None:
            spans.append((match.start(), match.end()))
            continue
        index_text = match.group(1)
        order = int(index_text) if index_text else 10_000 + position
        tps.append((order, price))
        spans.append((match.start(), match.end()))

    for match in _LONG_RE.finditer(raw):
        if not _is_target_word(raw, match.start(), buy_sell=buy_sell):
            continue
        parsed = parse_price_token(match.group(2))
        price, _zone = _as_price(parsed)
        if price is None:
            continue
        tps.append((20_000 + match.start(), price))
        spans.append((match.start(), match.end()))

    ordered: list[float] = []
    for _order, price in sorted(tps, key=lambda item: item[0]):
        if price not in ordered:
            ordered.append(price)

    blanked = _blank(raw, spans)
    anchor = _side_index(raw, side)
    prices = []
    for match in _PRICE_FIND.finditer(blanked):
        parsed = parse_price_token(match.group(0))
        if parsed is None:
            continue
        prices.append((match.start(), parsed))

    entry: float | None = None
    zone: tuple[float, float] | None = None
    if prices:
        if anchor is None:
            chosen = prices[0][1]
        else:
            chosen = min(prices, key=lambda item: abs(item[0] - anchor))[1]
        entry, zone = _as_price(chosen)

    symbol: str | None = None
    symbols = [
        (match.start(), match.group(0))
        for match in _TOKEN.finditer(raw)
        if _looks_like_symbol(match.group(0))
    ]
    if symbols:
        if anchor is None:
            symbol = symbols[0][1]
        else:
            symbol = min(symbols, key=lambda item: abs(item[0] - anchor))[1]

    now_hint = _NOW.search(raw) is not None
    managed = _MANAGEMENT.search(raw) is not None
    fresh = (
        not ambiguous
        and not managed
        and symbol is not None
        and side is not None
        and (entry is not None or zone is not None or now_hint)
    )
    return _with_metal_levels(
        FastRead(
            symbol=symbol,
            side=side,
            entry=entry,
            zone=zone,
            sl=sl,
            tps=tuple(ordered),
            now_hint=now_hint,
            fresh=fresh,
            ambiguous=ambiguous,
        ),
        _anchor_price(entry, zone),
        symbol,
    )


def _integer_digits(value: float) -> int:
    """How many digits sit left of the decimal. 4285.16 and 4285 are both 4."""
    return max(1, len(str(abs(int(value)))))


def _metal_price_context(symbol: str | None, anchor: float) -> bool:
    """True when a dropped decimal is a metal print, not an FX or index price.

    A named non-metal is never rewritten. A missing symbol still counts when the
    anchor itself is on gold's scale: a ladder line often omits ``XAUUSD`` and
    4285 does not overlap EURUSD, US30, or BTC.
    """
    if symbol:
        return _is_metal(symbol)
    return 800.0 <= anchor <= 8000.0


def _normalize_metal_print(
    value: float | None,
    anchor: float | None,
    *,
    symbol: str | None,
) -> float | None:
    """Recover one dropped decimal on a metal level when the reading is unique.

    This channel writes gold as ``4316=44`` (4316.44) and sometimes drops the
    point entirely: ``SL 42672`` next to an entry near 4285 is 4267.2, which is
    what the full agent reads. The shift is divide-by-10 or divide-by-100, chosen
    by how many extra digits the print has against the anchor. A level already on
    that scale (``SL 4308``), a pip count (``SL 20``), a non-metal, or a shift
    that is still far from the anchor is returned unchanged. Nothing is invented.
    """
    if value is None or anchor is None:
        return value
    if value <= 0.0 or anchor <= 0.0:
        return value
    if not _metal_price_context(symbol, anchor):
        return value
    # A written decimal (4267.2) is already the level. Only a bare integer can
    # be the missing-point form.
    if abs(value - round(value)) > 1e-6:
        return value
    extra = _integer_digits(value) - _integer_digits(anchor)
    if extra not in (1, 2):
        return value
    corrected = round(value / (10 ** extra), extra)
    if corrected <= 0.0 or _integer_digits(corrected) != _integer_digits(anchor):
        return value
    gap = abs(corrected - anchor)
    limit = _METAL_DROPPED_DECIMAL_MAX if anchor >= 200.0 else max(anchor * 0.15, 0.5)
    if gap > limit + 1e-9:
        return value
    if abs(value - anchor) < gap * 10.0:
        return value
    return corrected


def _anchor_price(entry: float | None, zone: tuple[float, float] | None) -> float | None:
    if entry is not None and entry > 0.0:
        return float(entry)
    if zone is not None:
        return (float(zone[0]) + float(zone[1])) / 2.0
    return None


def _with_metal_levels(read: FastRead, anchor: float | None, symbol: str | None) -> FastRead:
    """Apply :func:`_normalize_metal_print` to the labeled stop and targets."""
    named = symbol or read.symbol
    sl = _normalize_metal_print(read.sl, anchor, symbol=named)
    tps = tuple(
        price
        for price in (
            _normalize_metal_print(level, anchor, symbol=named) for level in read.tps
        )
        if price is not None
    )
    if sl == read.sl and tps == read.tps:
        return read
    return replace(read, sl=sl, tps=tps)


def _is_metal(symbol_name: str, pip_convention: str = "") -> bool:
    blob = f"{symbol_name} {pip_convention}".upper()
    return any(marker in blob for marker in _METAL_MARKERS)


def market_distance_limit(
    *,
    symbol_name: str,
    pip_size: float,
    pip_convention: str = "",
    metal_distance: float | None = None,
    default_pips: float | None = None,
) -> float:
    """Absolute price distance that still counts as 'at the entry'.

    Metals use ``FAST_ENTRY_METAL_MARKET_DISTANCE`` (default 7.0, dollars on XAUUSD).
    Everything else uses ``FAST_ENTRY_DEFAULT_MARKET_PIPS`` times the symbol's pip.
    """
    dollars = (
        float(metal_distance)
        if metal_distance is not None
        else float(settings.FAST_ENTRY_METAL_MARKET_DISTANCE)
    )
    pips = (
        float(default_pips)
        if default_pips is not None
        else float(settings.FAST_ENTRY_DEFAULT_MARKET_PIPS)
    )
    if _is_metal(symbol_name, pip_convention):
        return dollars
    return pips * float(pip_size)


def decide_route(
    read: FastRead,
    *,
    bid: float,
    ask: float,
    pip_size: float,
    symbol_name: str,
    pip_convention: str = "",
    metal_distance: float | None = None,
    default_pips: float | None = None,
    stale: bool = False,
) -> Route:
    """Choose a market open or a skip from the fill the order would actually use.

    A buy is measured against the ask, a sell against the bid. ``buy now`` / ``sell now``
    (and ``at market``) with no printed price is ``open_market`` when the quote is live.
    A printed entry, including one next to the word now, is ``open_market`` only inside
    the band. Outside the band the route is ``skip`` — this channel does not leave a
    hanging limit and does not market-chase. A stale post is ``skip`` even when the
    words say now. A side with no printed price and no immediate language is ``skip``;
    the quote is not copied in as an entry.
    """
    common = dict(side=read.side, sl=read.sl, tps=read.tps, now_hint=read.now_hint)
    if stale:
        return Route(action="skip", reason="stale", **common)
    if not read.fresh or read.side not in ("buy", "sell"):
        return Route(action="skip", reason="not_entry", **common)
    bid_value = float(bid or 0.0)
    ask_value = float(ask or 0.0)
    if bid_value <= 0.0 or ask_value <= 0.0:
        return Route(action="skip", reason="no_quote", **common)
    limit = market_distance_limit(
        symbol_name=symbol_name,
        pip_size=pip_size,
        pip_convention=pip_convention,
        metal_distance=metal_distance,
        default_pips=default_pips,
    )
    market = ask_value if read.side == "buy" else bid_value
    entry: float | None
    distance: float | None
    near: bool
    if read.zone is not None:
        low, high = read.zone
        if low - 1e-12 <= market <= high + 1e-12:
            entry, distance, near = None, 0.0, True
        elif market > high:
            entry, distance, near = None, market - high, False
        else:
            entry, distance, near = None, low - market, False
    elif read.entry is None:
        entry, distance, near = None, None, False
    else:
        entry = float(read.entry)
        distance = abs(market - entry)
        near = distance <= limit + 1e-9

    if read.now_hint and read.entry is None and read.zone is None:
        return Route(
            action="open_market",
            entry=None,
            distance=distance,
            limit=limit,
            reason="now",
            **common,
        )
    if read.entry is None and read.zone is None:
        return Route(action="skip", reason="no_entry", limit=limit, **common)
    if near:
        return Route(
            action="open_market",
            entry=entry if read.zone is None else read.entry,
            distance=distance,
            limit=limit,
            reason="now" if read.now_hint else "near",
            **common,
        )
    return Route(
        action="skip",
        entry=read.entry,
        distance=distance,
        limit=limit,
        reason="missed",
        **common,
    )


def _levels(read: FastRead) -> dict[str, Any]:
    """Only labeled stops and targets. An empty post contributes neither."""
    extra: dict[str, Any] = {}
    if read.sl is not None and read.sl > 0.0:
        extra["sl"] = float(read.sl)
    if read.tps:
        extra["tp"] = [float(price) for price in read.tps]
    return extra


def _round_trip(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 5)


def _open_reason(route: Route, symbol: str) -> str:
    if route.now_hint and route.entry is None and route.distance is None:
        return (
            f"fast entry: {route.side} {symbol} says now and names no printed price — "
            "opening at market"
        )
    if route.now_hint:
        return (
            f"fast entry: {route.side} {symbol} says now and the printed entry {route.entry} "
            f"is {_round_trip(route.distance)} from the market, within {_round_trip(route.limit)} "
            "— opening at market"
        )
    return (
        f"fast entry: {route.side} {symbol} entry {route.entry} is "
        f"{_round_trip(route.distance)} from the market, within {_round_trip(route.limit)} "
        f"(FAST_ENTRY band) — opening at market"
    )


def _plan_from_route(route: Route, symbol: str, read: FastRead) -> FastPlan | None:
    """Build the market call. A skip, including a far quote, is not a pending order."""
    if route.action != "open_market" or route.side not in ("buy", "sell"):
        return None
    arguments: dict[str, Any] = {
        "symbol": symbol,
        "side": route.side,
        "reason": _open_reason(route, symbol),
    }
    arguments.update(_levels(read))
    return FastPlan(
        tool="open_market",
        arguments=arguments,
        intent="open_market",
        distance=route.distance,
        limit=route.limit,
        entry=route.entry,
        side=route.side,
        symbol=symbol,
        now_hint=route.now_hint,
    )


def _is_stale(*, age_seconds: float | None, catchup: bool, stale: bool | None) -> bool:
    """True when a new open would be chasing a post the desk already calls old.

    ``stale`` is the context builder's flag (age past ``STALE_SIGNAL_SECONDS``). When
    the caller did not compute it, the same threshold is applied here. A catch-up whose
    age is unknown is treated as old: the backlog is what must not be market-chased.
    """
    if stale is True:
        return True
    if stale is False:
        return False
    if age_seconds is None:
        return bool(catchup)
    try:
        return float(age_seconds) > float(settings.STALE_SIGNAL_SECONDS)
    except (TypeError, ValueError):
        return bool(catchup)


def _skip_note(
    reason: str,
    *,
    distance: float | None = None,
    limit: float | None = None,
    entry: float | None = None,
    catchup: bool = False,
) -> str:
    """The user-turn sentence that stops the later agent from undoing the skip."""
    if reason == "missed":
        return (
            "=== FAST ENTRY SKIPPED ===\n"
            f"The live quote is {_round_trip(distance)} from the printed entry {entry}, "
            f"past the {_round_trip(limit)} fast-entry band. This channel does not leave a "
            "hanging pending order for a missed print. Do not call place_pending or "
            "open_market to chase it. Wait for a follow-up or the ladder."
        )
    if reason == "stale":
        delivery = "an old catch-up" if catchup else "past the stale threshold"
        return (
            "=== FAST ENTRY SKIPPED ===\n"
            f"This post is {delivery} (older than {int(settings.STALE_SIGNAL_SECONDS)}s). "
            "Do not open at market and do not place a pending order to chase the old price."
        )
    return (
        "=== FAST ENTRY SKIPPED ===\n"
        "The post names a side but no entry price, and it does not say now. Do not invent "
        "a price from the quote. Wait for the ladder or a later post that names the entry. "
        "Open at market only when the post itself says buy now or sell now."
    )


async def _chain_rows(signal_ref: str | None) -> list[dict[str, Any]]:
    """Live chain tickets as plain dicts. The session must not outlive this copy."""
    if not signal_ref:
        return []
    from app.trading import guards

    async with session_scope() as session:
        rows = await guards.live_chain_trades(session, signal_ref)
        copied: list[dict[str, Any]] = []
        for row in rows:
            if bool(getattr(row, "external", False)):
                continue
            copied.append(
                {
                    "symbol": str(getattr(row, "symbol", "") or ""),
                    "side": str(getattr(row, "side", "") or ""),
                    "sl": float(getattr(row, "sl", 0.0) or 0.0),
                    "entry_price": float(getattr(row, "entry_price", 0.0) or 0.0),
                }
            )
        return copied


async def plan_fast_entry(
    executor: Any,
    *,
    text: str,
    signal_ref: str | None,
    age_seconds: float | None = None,
    catchup: bool = False,
    stale: bool | None = None,
) -> FastPlan | FastSkip | None:
    """Plan the single broker call, or a skip the later agent must not undo.

    A chain that already has naked legs and a posted stop is a modify, not a second
    open, including when the reply itself is old. A fresh entry is resolved and quoted
    here — ``resolve_symbol`` and ``get_quote`` only — and then opened at market when
    the post says now or the quote is still inside the band. A far quote returns a
    skip, never ``place_pending``. Candles are not read.
    """
    read = read_post(text)
    if read.ambiguous:
        log.info("fast_entry_skip reason=ambiguous_side signal_ref=%s", signal_ref)
        return None

    rows = await _chain_rows(signal_ref)
    if rows:
        if read.sl is None or read.sl <= 0.0:
            log.info("fast_entry_skip reason=chain_already_open signal_ref=%s", signal_ref)
            return None
        if not all(float(row["sl"]) <= 0.0 for row in rows):
            log.info("fast_entry_skip reason=chain_already_stopped signal_ref=%s", signal_ref)
            return None
        # The broker name is case-sensitive (``XAUUSDm``). Compare folded, send the
        # spelling the ticket was opened under.
        raw_symbols = [str(row["symbol"]) for row in rows if str(row["symbol"])]
        sides = {str(row["side"]).lower() for row in rows}
        if len({name.upper() for name in raw_symbols}) != 1 or len(sides) != 1:
            return None
        side = next(iter(sides))
        symbol = raw_symbols[0]
        if side not in ("buy", "sell") or not symbol:
            return None
        if read.side is not None and read.side != side:
            log.info("fast_entry_skip reason=side_mismatch signal_ref=%s", signal_ref)
            return None
        anchor = _anchor_price(read.entry, read.zone)
        if anchor is None:
            for row in rows:
                price = float(row.get("entry_price") or 0.0)
                if price > 0.0:
                    anchor = price
                    break
        read = _with_metal_levels(read, anchor, symbol)
        arguments: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "sl": float(read.sl),
            "reason": (
                "fast entry: the chain already has incomplete legs; attaching the posted "
                "stop and targets instead of opening another pair"
            ),
        }
        if read.tps:
            arguments["tp"] = [float(price) for price in read.tps]
        return FastPlan(
            tool="open_market",
            arguments=arguments,
            intent="modify_existing",
            distance=None,
            limit=None,
            entry=read.entry,
            side=side,
            symbol=symbol,
            now_hint=read.now_hint,
        )

    if _is_stale(age_seconds=age_seconds, catchup=catchup, stale=stale) and (
        read.fresh or (read.symbol and read.side and not read.ambiguous)
    ):
        log.info(
            "fast_entry_skip reason=stale signal_ref=%s age=%s catchup=%s",
            signal_ref,
            age_seconds,
            catchup,
        )
        return FastSkip(
            reason="stale",
            note=_skip_note("stale", catchup=catchup),
            side=read.side,
            symbol=read.symbol,
            entry=read.entry,
        )

    if not read.fresh or read.symbol is None or read.side is None:
        if (
            read.symbol
            and read.side
            and not read.ambiguous
            and read.entry is None
            and read.zone is None
            and not read.now_hint
        ):
            log.info("fast_entry_skip reason=no_explicit_entry signal_ref=%s", signal_ref)
            return FastSkip(
                reason="no_entry",
                note=_skip_note("no_entry"),
                side=read.side,
                symbol=read.symbol,
            )
        return None

    resolved = await executor.resolve_symbol(read.symbol)
    if not isinstance(resolved, dict) or not resolved.get("ok"):
        log.info("fast_entry_skip reason=symbol signal_ref=%s query=%s", signal_ref, read.symbol)
        return None
    candidates = resolved.get("candidates")
    if not isinstance(candidates, list):
        return None
    chosen: dict[str, Any] | None = None
    for row in candidates:
        if isinstance(row, dict) and row.get("tradable", True) and row.get("name"):
            chosen = row
            break
    if chosen is None:
        return None
    symbol = str(chosen["name"])
    pip_size = float(chosen.get("pip_size") or 0.0)
    pip_convention = str(chosen.get("pip_convention") or "")
    read = _with_metal_levels(read, _anchor_price(read.entry, read.zone), symbol)
    if pip_size <= 0.0 and not _is_metal(symbol, pip_convention) and not _is_metal(read.symbol or ""):
        log.info("fast_entry_skip reason=pip_size symbol=%s", symbol)
        return None

    quote = await executor.get_quote(symbol)
    if not isinstance(quote, dict) or not quote.get("ok", True):
        return None
    try:
        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
    except (TypeError, ValueError):
        return None
    metal_distance: float | None = None
    if len(read.tps) < LADDER_MIN_TARGETS:
        try:
            async with session_scope() as session:
                metal_distance = await max_entry_drift_usd(session)
        except Exception as exc:  # noqa: BLE001 - the operator default still applies
            log.warning("fast_entry_drift_read_failed error=%s", exc)
            metal_distance = float(MAX_ENTRY_DRIFT_USD_DEFAULT)
    route = decide_route(
        read,
        bid=bid,
        ask=ask,
        pip_size=pip_size,
        symbol_name=symbol,
        pip_convention=pip_convention,
        metal_distance=metal_distance,
    )
    if route.action == "skip" and route.reason == "missed":
        log.info(
            "fast_entry_skip reason=missed symbol=%s bid=%s ask=%s distance=%s limit=%s",
            symbol,
            bid,
            ask,
            route.distance,
            route.limit,
        )
        return FastSkip(
            reason="missed",
            note=_skip_note(
                "missed",
                distance=route.distance,
                limit=route.limit,
                entry=route.entry,
            ),
            distance=route.distance,
            limit=route.limit,
            entry=route.entry,
            side=route.side,
            symbol=symbol,
        )
    plan = _plan_from_route(route, symbol, read)
    if plan is None:
        log.info(
            "fast_entry_skip reason=route symbol=%s bid=%s ask=%s",
            symbol,
            bid,
            ask,
        )
    return plan
