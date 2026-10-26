"""Hard gates on every new open: entry band, then signal age.

Fast entry already skips a quote that has left the printed price. The full agent did
not: it could call ``open_market`` or ``place_pending`` minutes later and the executor
sent the order. Both tools now run :func:`check_open` after an incomplete chain has
been redirected to ``modify_existing`` and before anything is sent.

The band is :func:`app.ai.fast_entry.market_distance_limit`. Ladders of 4 or more
targets keep seven price units on metals (``FAST_ENTRY_METAL_MARKET_DISTANCE``,
dollars on XAUUSD). A classic market entry (fewer than 4 take-profits, and an
incomplete teaser) passes ``trading.max_entry_drift_usd`` (default 5) as that
metal distance: the only "price already moved" skip is a fill farther than that.
``FAST_ENTRY_DEFAULT_MARKET_PIPS`` still applies to everything that is not a metal.
A buy is measured against the ask and a sell against the bid, the same fill the
order would actually use. Absolute distance: a print past the band in favour is
refused the same way as one past it against. The band and the stale window are
conjunctive: either failure refuses the open.

The stale clock is the earliest message in the reply chain whose text parses a
printed entry or a zone (``read_post``). That is the teaser when the teaser carries
the price, not the ladder reply that arrives later. Age is wall-clock ``posted_at``
of the earliest revision of that message which already contains the entry. Past
``STALE_SIGNAL_SECONDS`` (10 minutes) the open is refused even when price is inside
the band. If no message in the chain parses an entry, the clock falls back to the
chain root (the teaser) and the band does not apply, because there is no printed
price to compare. A missing or stale quote does not skip the band: one live tick is
preferred, a fresh cached sample may stand in, and if neither can prove the fill is
inside the band the open is refused.

Manual, watchdog, and kill-switch origins are not channel entries. Attaching levels
to legs that are already open is a modify and does not reach this gate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.ai.fast_entry import market_distance_limit, read_post
from app.config import settings
from app.db import session_scope
from app.models import Message
from app.trading import guards
from app.trading.quote_cache import QUOTE_CACHE_FRESH_SECONDS, CachedQuote, fresh_quote
from app.util.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "ChainAnchor",
    "ClassicEntryLaw",
    "EntryLawState",
    "OpenGateVerdict",
    "check_open",
    "classic_market_entry_law",
    "distance_from_printed",
    "entry_law_blocks_finish",
    "fill_price",
    "load_chain_anchor",
    "note_entry_tool",
    "price_for_gate",
]

#: A failed open or pending is a hard block. The model had to try; it may then
#: finish and name the refusal. Disarmed, allowlist, stale, margin and a stop
#: already through the market all arrive as ``ok: false`` on those tools.
_OPEN_TOOLS: frozenset[str] = frozenset({"open_market", "place_pending"})

#: Origins that are not a channel entry. The operator's manual ticket, the stop
#: watchdog, and the kill switch do not carry a printed channel price.
_SKIP_ORIGINS: frozenset[str] = frozenset({"manual", "watchdog", "kill_switch"})


@dataclass(frozen=True)
class ChainAnchor:
    """Where the stale clock starts, and the printed price when the chain has one.

    ``defined_entry`` is true only when some stored message parsed an entry or a zone.
    ``posted_at`` is that message's clock. Otherwise ``posted_at`` is the chain root
    and the band has nothing to measure.
    """

    posted_at: datetime | None
    entry: float | None
    zone: tuple[float, float] | None
    side: str | None
    defined_entry: bool
    message_id: int | None


@dataclass(frozen=True)
class OpenGateVerdict:
    """The decision ``open_market`` / ``place_pending`` enforce. ``allow`` sends nothing when false."""

    allow: bool
    reason: str
    message: str = ""
    distance: float | None = None
    limit: float | None = None
    entry: float | None = None
    fill: float | None = None
    age_seconds: float | None = None
    price_source: str = ""


def fill_price(side: str, bid: float, ask: float) -> float | None:
    """The price an order would actually fill at. Buy pays the ask, sell hits the bid."""
    if side == "buy" and ask > 0.0:
        return float(ask)
    if side == "sell" and bid > 0.0:
        return float(bid)
    return None


def distance_from_printed(
    fill: float,
    entry: float | None,
    zone: tuple[float, float] | None,
) -> float | None:
    """Absolute distance from the printed entry, or from the nearest edge of a zone.

    Inside the zone the distance is 0. A point entry uses ``|fill - entry|``.
    """
    if zone is not None:
        low, high = (min(zone), max(zone))
        if low - 1e-12 <= fill <= high + 1e-12:
            return 0.0
        if fill > high:
            return float(fill - high)
        return float(low - fill)
    if entry is None:
        return None
    return abs(float(fill) - float(entry))


def price_for_gate(
    *,
    bid: float | None,
    ask: float | None,
    cached: CachedQuote | None,
    symbol: str,
    now: float,
    fresh_seconds: float = QUOTE_CACHE_FRESH_SECONDS,
) -> tuple[float | None, float | None, str]:
    """Live tick first. A fresh cache for the same symbol is the only fallback.

    A cache that is old, for another symbol, or missing returns ``missing``. The
    caller must refuse the open when a printed entry cannot be checked.
    """
    try:
        live_bid = float(bid or 0.0)
        live_ask = float(ask or 0.0)
    except (TypeError, ValueError):
        live_bid = 0.0
        live_ask = 0.0
    if live_bid > 0.0 and live_ask > 0.0:
        return live_bid, live_ask, "live"
    if cached is None or cached.symbol.upper() != str(symbol or "").strip().upper():
        return None, None, "missing"
    age = float(now) - float(cached.cached_at)
    if age < 0.0 or age > float(fresh_seconds) or cached.bid <= 0.0 or cached.ask <= 0.0:
        return None, None, "missing"
    return float(cached.bid), float(cached.ask), "cache"


def _stamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _age_seconds(posted_at: datetime | None, now: float) -> float | None:
    stamp = _stamp(posted_at)
    if stamp is None:
        return None
    return max(0.0, float(now) - stamp.replace(tzinfo=timezone.utc).timestamp())


def _printed(text: str) -> tuple[float | None, tuple[float, float] | None, str | None]:
    """Entry or zone from one post. An ambiguous side does not define an entry."""
    read = read_post(text or "")
    if read.ambiguous:
        return None, None, None
    if read.entry is None and read.zone is None:
        return None, None, None
    return read.entry, read.zone, read.side


def _fmt(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{round(float(value), 5):g}"


def _entry_label(entry: float | None, zone: tuple[float, float] | None) -> str:
    if zone is not None:
        low, high = (min(zone), max(zone))
        return f"zone {_fmt(low)}-{_fmt(high)}"
    return _fmt(entry)


async def _message_for_ref(session: Any, signal_ref: str | None) -> Message | None:
    parsed = guards.parse_signal_ref(signal_ref)
    if parsed is None:
        return None
    source_id, message_id = parsed
    result = await session.execute(
        select(Message)
        .where(Message.source_id == int(source_id), Message.message_id == int(message_id))
        .order_by(Message.revision.desc(), Message.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def load_chain_anchor(
    session: Any,
    *,
    message_row_id: int | None,
    signal_ref: str | None,
) -> ChainAnchor | None:
    """The entry clock for this run, or None when the run is not a stored channel post.

    Choice, recorded here so the open path and the prompt agree: walk the reply chain
    to every stored message, and take the earliest ``posted_at`` among messages whose
    text parses an entry or a zone. Within one telegram message, the earliest revision
    that already contains that entry is the clock — a later edit does not make an old
    print young again. If nothing in the chain parses an entry, the clock is the chain
    root (the teaser), or the earliest stored message when the root row itself was
    never ingested.
    """
    row: Message | None = None
    if message_row_id:
        row = await session.get(Message, int(message_row_id))
    if row is None and signal_ref:
        row = await _message_for_ref(session, signal_ref)
    if row is None:
        return None

    source_id = int(row.source_id)
    root = await guards.root_message_id(session, source_id, int(row.message_id))
    ids = await guards.family_message_ids(session, source_id, root)
    result = await session.execute(
        select(Message).where(
            Message.source_id == source_id,
            Message.message_id.in_(list(ids)),
        )
    )
    stored = list(result.scalars().all())
    if not stored:
        stored = [row]

    by_id: dict[int, list[Message]] = {}
    for item in stored:
        by_id.setdefault(int(item.message_id), []).append(item)

    defined: list[tuple[datetime, int, float | None, tuple[float, float] | None, str | None]] = []
    for message_id, revisions in by_id.items():
        revisions.sort(key=lambda item: (int(item.revision or 0), int(item.id or 0)))
        for revision in revisions:
            entry, zone, side = _printed(revision.text)
            if entry is None and zone is None:
                continue
            posted = _stamp(revision.posted_at)
            if posted is None:
                continue
            defined.append((posted, message_id, entry, zone, side))
            break

    if defined:
        defined.sort(key=lambda item: (item[0], item[1]))
        posted, message_id, entry, zone, side = defined[0]
        return ChainAnchor(
            posted_at=posted,
            entry=entry,
            zone=zone,
            side=side,
            defined_entry=True,
            message_id=message_id,
        )

    root_rows = by_id.get(int(root)) or stored
    stamps = [stamp for stamp in (_stamp(item.posted_at) for item in root_rows) if stamp is not None]
    posted = min(stamps) if stamps else None
    root_id = int(root) if int(root) in by_id else int(root_rows[0].message_id)
    return ChainAnchor(
        posted_at=posted,
        entry=None,
        zone=None,
        side=None,
        defined_entry=False,
        message_id=root_id,
    )


def _refuse(
    reason: str,
    message: str,
    *,
    distance: float | None = None,
    limit: float | None = None,
    entry: float | None = None,
    fill: float | None = None,
    age_seconds: float | None = None,
    price_source: str = "",
) -> OpenGateVerdict:
    return OpenGateVerdict(
        allow=False,
        reason=reason,
        message=message,
        distance=distance,
        limit=limit,
        entry=entry,
        fill=fill,
        age_seconds=age_seconds,
        price_source=price_source,
    )


@dataclass(frozen=True)
class ClassicEntryLaw:
    """Whether a classic market entry must be opened, or skipped for price.

    ``action`` is ``open`` when the fill is at or inside ``max_drift``, the signal
    is not stale, and the posted stop is still on the right side of the market.
    ``skip`` / ``price_moved`` is the only price-distance refusal, and it requires
    the absolute distance to be strictly greater than ``max_drift``. Reward-to-risk
    and lot size are not inputs: with fixed lots on, ``defer_for_risk_forbidden``
    says the operator size is the size.
    """

    action: str
    must_open: bool
    skip_reason: str
    price_distance: float | None
    max_drift: float
    within_drift: bool
    defer_for_risk_forbidden: bool
    fixed_lots: bool


def classic_market_entry_law(
    *,
    distance: float | None,
    max_drift: float,
    fixed_lots: bool,
    ladder: bool,
    stale: bool = False,
    stop_invalid: bool = False,
) -> ClassicEntryLaw:
    """Decide a classic market entry from price distance and the hard gates only.

    Ladders are ``not_classic``: they keep their own volume and the wider metal
    band. A missing distance does not invent a skip and does not force an open.
    """
    drift = float(max_drift)
    lots_fixed = bool(fixed_lots)
    moved = None if distance is None else float(distance)
    within = moved is not None and moved <= drift + 1e-9
    if ladder:
        return ClassicEntryLaw(
            action="not_classic",
            must_open=False,
            skip_reason="",
            price_distance=moved,
            max_drift=drift,
            within_drift=within,
            defer_for_risk_forbidden=False,
            fixed_lots=lots_fixed,
        )
    if moved is None:
        return ClassicEntryLaw(
            action="unmeasured",
            must_open=False,
            skip_reason="",
            price_distance=None,
            max_drift=drift,
            within_drift=False,
            defer_for_risk_forbidden=False,
            fixed_lots=lots_fixed,
        )
    if moved > drift + 1e-9:
        return ClassicEntryLaw(
            action="skip",
            must_open=False,
            skip_reason="price_moved",
            price_distance=moved,
            max_drift=drift,
            within_drift=False,
            defer_for_risk_forbidden=False,
            fixed_lots=lots_fixed,
        )
    if stale:
        return ClassicEntryLaw(
            action="skip",
            must_open=False,
            skip_reason="stale",
            price_distance=moved,
            max_drift=drift,
            within_drift=True,
            defer_for_risk_forbidden=False,
            fixed_lots=lots_fixed,
        )
    if stop_invalid:
        return ClassicEntryLaw(
            action="skip",
            must_open=False,
            skip_reason="stop_invalid",
            price_distance=moved,
            max_drift=drift,
            within_drift=True,
            defer_for_risk_forbidden=False,
            fixed_lots=lots_fixed,
        )
    return ClassicEntryLaw(
        action="open",
        must_open=True,
        skip_reason="",
        price_distance=moved,
        max_drift=drift,
        within_drift=True,
        defer_for_risk_forbidden=lots_fixed,
        fixed_lots=lots_fixed,
    )


@dataclass
class EntryLawState:
    """What this run has already learned about a required classic open.

    ``must_open`` sticks once any ``evaluate_entry`` said so. One accepted open
    clears the obligation. One refused open is a hard block: the model tried,
    and it may finish by naming that refusal.
    """

    must_open: bool = False
    opened: bool = False
    hard_blocked: bool = False


def note_entry_tool(state: EntryLawState, name: str, result: dict[str, Any]) -> None:
    """Update ``state`` from one tool result. Unknown tools are ignored."""
    if not isinstance(result, dict):
        return
    tool = str(name or "")
    if tool == "evaluate_entry" and result.get("must_open"):
        state.must_open = True
    if tool in _OPEN_TOOLS:
        if result.get("ok"):
            state.opened = True
        else:
            state.hard_blocked = True


def entry_law_blocks_finish(
    *,
    must_open: bool,
    opened: bool,
    hard_blocked: bool,
    decision: str,
) -> str | None:
    """Refuse a finish that dodges a classic entry the server already required.

    Returns the message the model must read, or None when finish may stand.
    """
    chosen = str(decision or "deferred").strip().lower() or "deferred"
    if not must_open or opened or hard_blocked:
        return None
    return (
        f"refusing finish({chosen!r}): this classic entry is within trading.max_entry_drift_usd "
        "of the channel price, so it must be opened at market. Do not defer or skip "
        "because the lots look large for the account, because the stop is wide, or "
        "because reward:risk to TP1 looks poor. Call open_market now. When "
        "trading.use_fixed_order_lots is on, omit volume and risk_percent so the "
        "server opens orders_per_signal orders of order_lot_size. A hard refusal "
        "(disarmed, symbol allowlist, stale signal, margin, a stop already through "
        "the market) is the only reason to finish without an order, and only after "
        "that refusal has come back from the tool."
    )


def _measure(
    *,
    anchor: ChainAnchor,
    side: str,
    bid: float | None,
    ask: float | None,
    symbol_name: str,
    pip_size: float,
    pip_convention: str,
    now: float,
    metal_distance: float | None = None,
) -> tuple[float | None, float, float | None, str]:
    """Return fill, band, distance, and where the price came from."""
    limit = market_distance_limit(
        symbol_name=symbol_name,
        pip_size=pip_size,
        pip_convention=pip_convention,
        metal_distance=metal_distance,
    )
    cached = None
    try:
        live_bid = float(bid or 0.0)
        live_ask = float(ask or 0.0)
    except (TypeError, ValueError):
        live_bid = 0.0
        live_ask = 0.0
    if not (live_bid > 0.0 and live_ask > 0.0):
        cached = fresh_quote(symbol_name, now=now)
    used_bid, used_ask, source = price_for_gate(
        bid=live_bid,
        ask=live_ask,
        cached=cached,
        symbol=symbol_name,
        now=now,
    )
    if used_bid is None or used_ask is None:
        return None, limit, None, source
    fill = fill_price(side, used_bid, used_ask)
    if fill is None:
        return None, limit, None, source
    distance = distance_from_printed(fill, anchor.entry, anchor.zone)
    return fill, limit, distance, source


async def check_open(
    *,
    origin: str,
    bypass_arm: bool,
    message_row_id: int | None,
    signal_ref: str | None,
    symbol_name: str,
    pip_size: float,
    pip_convention: str,
    side: str,
    bid: float | None,
    ask: float | None,
    now: float | None = None,
    metal_distance: float | None = None,
) -> OpenGateVerdict:
    """Refuse a new open that is far from the printed entry or older than 10 minutes.

    ``allow`` with reason ``no_signal`` means this call is not a stored channel post
    (the executor tests, a ticket with no message row). ``no_printed_entry`` means the
    chain has no parsed price, so the band cannot be applied; the stale clock still ran.
    """
    if bypass_arm or str(origin or "") in _SKIP_ORIGINS:
        return OpenGateVerdict(allow=True, reason="not_applicable")

    clock = time.time() if now is None else float(now)
    try:
        async with session_scope() as session:
            anchor = await load_chain_anchor(
                session, message_row_id=message_row_id, signal_ref=signal_ref
            )
    except Exception as exc:  # noqa: BLE001 - an unreadable chain must not become an open
        log.warning("entry_gate_lookup_failed signal_ref=%s error=%s", signal_ref, exc)
        return _refuse(
            "signal_age_unknown",
            "refusing open: this signal's age could not be read, so the "
            f"{int(settings.STALE_SIGNAL_SECONDS)}s limit cannot be checked. No order was sent.",
        )
    if anchor is None:
        return OpenGateVerdict(allow=True, reason="no_signal")

    age = _age_seconds(anchor.posted_at, clock)
    stale_after = float(settings.STALE_SIGNAL_SECONDS)
    if age is None:
        return _refuse(
            "signal_age_unknown",
            "refusing open: this signal's age could not be read, so the "
            f"{int(stale_after)}s limit cannot be checked. No order was sent.",
        )

    direction = str(side or "").strip().lower()
    if age > stale_after:
        fill, limit, distance, source = (None, None, None, "")
        if anchor.defined_entry:
            fill, limit, distance, source = _measure(
                anchor=anchor,
                side=direction,
                bid=bid,
                ask=ask,
                symbol_name=symbol_name,
                pip_size=pip_size,
                pip_convention=pip_convention,
                now=clock,
                metal_distance=metal_distance,
            )
        detail = (
            f"refusing open: the channel entry was printed {int(age)}s ago, past the "
            f"{int(stale_after)}s limit. Price being inside the band does not reopen it. "
            "No order was sent."
        )
        if distance is not None and limit is not None:
            detail += (
                f" Fill {_fmt(fill)} is {_fmt(distance)} from "
                f"{_entry_label(anchor.entry, anchor.zone)} (band {_fmt(limit)})."
            )
        return _refuse(
            "stale_signal",
            detail,
            distance=distance,
            limit=limit,
            entry=anchor.entry,
            fill=fill,
            age_seconds=age,
            price_source=source,
        )

    if not anchor.defined_entry:
        return OpenGateVerdict(
            allow=True,
            reason="no_printed_entry",
            age_seconds=age,
        )

    fill, limit, distance, source = _measure(
        anchor=anchor,
        side=direction,
        bid=bid,
        ask=ask,
        symbol_name=symbol_name,
        pip_size=pip_size,
        pip_convention=pip_convention,
        now=clock,
        metal_distance=metal_distance,
    )
    label = _entry_label(anchor.entry, anchor.zone)
    if fill is None or distance is None or source == "missing":
        return _refuse(
            "price_unverified",
            f"refusing open: no live or fresh cached quote could prove the fill is within "
            f"{_fmt(limit)} of the channel entry {label}. A missing quote is not permission "
            "to open. No order was sent.",
            limit=limit,
            entry=anchor.entry,
            age_seconds=age,
            price_source=source,
        )
    if distance > limit + 1e-9:
        side_name = "ask" if direction == "buy" else "bid"
        return _refuse(
            "far_from_entry",
            f"refusing {direction} open: fill {_fmt(fill)} ({side_name}, {source}) is "
            f"{_fmt(distance)} from the channel entry {label}, past the {_fmt(limit)} band. "
            "Not opening at market and not placing a pending order to catch up.",
            distance=distance,
            limit=limit,
            entry=anchor.entry,
            fill=fill,
            age_seconds=age,
            price_source=source,
        )
    return OpenGateVerdict(
        allow=True,
        reason="within_band",
        distance=distance,
        limit=limit,
        entry=anchor.entry,
        fill=fill,
        age_seconds=age,
        price_source=source,
    )
