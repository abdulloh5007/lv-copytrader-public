"""Server-side ladder outcome stats: last-N closed ladders per source, never AI-recounted.

A two-leg ladder (cover 1R + far runner) is one *group*. When every ticket in the group is
gone, one :class:`~app.models.LadderOutcome` row is written from the stored trades and deal
causes. The next entry reads an aggregate of the last ``trading.ladder_stats_window`` rows
and a ready-made blurb — the model must not reconstruct these counts from channel history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import LADDER_STATS_WINDOW
from app.db import get_setting
from app.models import LadderOutcome, Source, Trade, utcnow
from app.trading.pricing import stop_is_at_or_beyond_breakeven
from app.util.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "KEY_STATS_WINDOW",
    "MODE_BE_AFTER_COVER",
    "MODE_HOLD_CHANNEL_SL",
    "OUTCOME_BE_STOPOUT",
    "OUTCOME_OTHER",
    "OUTCOME_RAW_SL",
    "OUTCOME_TP4_PLUS",
    "RUNNER_MODES",
    "ChannelLadderStats",
    "LadderLegView",
    "aggregate_kinds",
    "blurb_for",
    "classify_group",
    "empty_stats",
    "format_runner_tp_blurb",
    "is_ladder_group",
    "maybe_record_closed_group",
    "normalize_runner_mode",
    "runner_hit_chosen_tp",
    "stats_for_source",
    "suggest_mode",
    "suggest_runner_tp_bias",
    "window_size",
]

#: Settings key: how many fully-closed ladders the suggestion looks at.
KEY_STATS_WINDOW: Final[str] = "trading.ladder_stats_window"

MODE_BE_AFTER_COVER: Final[str] = "be_after_cover"
MODE_HOLD_CHANNEL_SL: Final[str] = "hold_channel_sl"
RUNNER_MODES: Final[frozenset[str]] = frozenset({MODE_BE_AFTER_COVER, MODE_HOLD_CHANNEL_SL})

OUTCOME_RAW_SL: Final[str] = "raw_sl_hit"
OUTCOME_BE_STOPOUT: Final[str] = "be_stopout"
OUTCOME_TP4_PLUS: Final[str] = "tp4_plus"
OUTCOME_OTHER: Final[str] = "other"

_LIVE_STATUSES: Final[frozenset[str]] = frozenset({"open", "pending"})
_ROLE_RUNNER: Final[str] = "runner"
_ROLE_COVER: Final[str] = "cover"


@dataclass(frozen=True)
class LadderLegView:
    """The fields classify needs, stripped of the ORM so tests can build them by hand."""

    role: str = ""
    side: str = "buy"
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    status: str = "closed"
    close_cause: str | None = None
    close_price: float | None = None
    profit: float | None = None
    unmanaged_tps: tuple[Any, ...] = ()
    split_count: int = 1

    @classmethod
    def from_trade(cls, trade: Trade) -> "LadderLegView":
        """Project one :class:`~app.models.Trade` row."""
        unmanaged = trade.unmanaged_tps or []
        if not isinstance(unmanaged, (list, tuple)):
            unmanaged = []
        return cls(
            role=str(trade.ladder_role or ""),
            side=str(trade.side or "buy"),
            entry=float(trade.entry_price or 0.0),
            sl=float(trade.sl or 0.0),
            tp=float(trade.tp or 0.0),
            status=str(trade.status or ""),
            close_cause=trade.close_cause,
            close_price=None if trade.close_price is None else float(trade.close_price),
            profit=None if trade.profit is None else float(trade.profit),
            unmanaged_tps=tuple(unmanaged),
            split_count=int(trade.split_count or 1),
        )


@dataclass(frozen=True)
class ChannelLadderStats:
    """Last-N ladder outcomes for one source, plus the mode/bias the server suggests next."""

    source_id: int | None
    source_title: str
    window: int
    sample_size: int
    raw_sl_hits: int
    be_stopouts: int
    tp4_plus: int
    clean_streak: int
    suggested_mode: str
    bootstrap: bool
    suggested_runner_tp_bias: int = 0

    def blurb(self) -> str:
        """The one-line block injected into the AI prompt and tool results."""
        return blurb_for(self)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable shape for tool results and the settings tree."""
        return {
            "source_id": self.source_id,
            "source_title": self.source_title,
            "window": self.window,
            "sample_size": self.sample_size,
            "raw_sl_hits": self.raw_sl_hits,
            "be_stopouts": self.be_stopouts,
            "tp4_plus": self.tp4_plus,
            "clean_streak": self.clean_streak,
            "suggested_mode": self.suggested_mode,
            "bootstrap": self.bootstrap,
            "suggested_runner_tp_bias": self.suggested_runner_tp_bias,
            "blurb": self.blurb(),
        }


def normalize_runner_mode(value: Any) -> str | None:
    """Return a canonical runner mode, or ``None`` when the value is missing or unknown."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in RUNNER_MODES:
        return text
    return None


def suggest_runner_tp_bias(
    *,
    sample_size: int,
    window: int = LADDER_STATS_WINDOW,
    raw_sl_hits: int,
    tp4_plus: int,
    clean_streak: int,
    bootstrap: bool | None = None,
) -> int:
    """Deterministic bias added to ``base = ceil((N+1)/2)``. Never random.

    Bias table (window defaults to 5):

    * ``sample_size < window`` (bootstrap) → **0**
    * any ``raw_sl_hits`` in the window → **0** (stay at base; floor is base ≥ 1, cap N-1)
    * ``raw_SL = 0`` and clean streak **3–4** → **+1**
    * streak **≥ 5** and healthy ``tp4_plus`` → **+1**
      healthy := ``tp4_plus >= max(2, (window+1)//2)`` (window 5 → ≥ 3)
    * strong: streak **≥ window** and ``tp4_plus >= window-1`` (window 5 → streak 5 and TP4+ ≥ 4)
      → **+2**, still capped at N-1 when the index is applied
    * otherwise → **0**
    """
    size = max(1, int(window))
    is_bootstrap = bool(bootstrap) if bootstrap is not None else int(sample_size) < size
    if is_bootstrap or int(sample_size) < size:
        return 0
    if int(raw_sl_hits) > 0:
        return 0
    healthy = int(tp4_plus) >= max(2, (size + 1) // 2)
    strong = int(clean_streak) >= size and int(tp4_plus) >= max(1, size - 1)
    if strong and healthy:
        return 2
    if int(clean_streak) >= 5 and healthy:
        return 1
    if 3 <= int(clean_streak) <= 4:
        return 1
    return 0


def suggest_mode(kinds: Sequence[str], *, window: int = LADDER_STATS_WINDOW) -> str:
    """Pick the next runner mode from outcome kinds, newest first.

    Fewer than ``window`` closed ladders default to ``be_after_cover`` (safer bootstrap).
    Any raw channel-SL loss in the window also selects ``be_after_cover``. A full window
    with zero raw SL hits selects ``hold_channel_sl``.
    """
    size = max(1, int(window))
    recent = [str(kind) for kind in kinds[:size]]
    if len(recent) < size:
        return MODE_BE_AFTER_COVER
    if any(kind == OUTCOME_RAW_SL for kind in recent):
        return MODE_BE_AFTER_COVER
    return MODE_HOLD_CHANNEL_SL


def clean_streak(kinds: Sequence[str]) -> int:
    """Count newest-first consecutive ladders that were not a raw (non-BE) stop loss."""
    streak = 0
    for kind in kinds:
        if str(kind) == OUTCOME_RAW_SL:
            break
        streak += 1
    return streak


def aggregate_kinds(
    kinds: Sequence[str],
    *,
    window: int = LADDER_STATS_WINDOW,
    source_id: int | None = None,
    source_title: str = "",
) -> ChannelLadderStats:
    """Build stats from outcome kinds, newest first."""
    size = max(1, int(window))
    recent = [str(kind) for kind in kinds[:size]]
    raw = sum(1 for kind in recent if kind == OUTCOME_RAW_SL)
    be = sum(1 for kind in recent if kind == OUTCOME_BE_STOPOUT)
    tp4 = sum(1 for kind in recent if kind == OUTCOME_TP4_PLUS)
    bootstrap = len(recent) < size
    streak = clean_streak(recent)
    bias = suggest_runner_tp_bias(
        sample_size=len(recent),
        window=size,
        raw_sl_hits=raw,
        tp4_plus=tp4,
        clean_streak=streak,
        bootstrap=bootstrap,
    )
    return ChannelLadderStats(
        source_id=source_id,
        source_title=source_title or "",
        window=size,
        sample_size=len(recent),
        raw_sl_hits=raw,
        be_stopouts=be,
        tp4_plus=tp4,
        clean_streak=streak,
        suggested_mode=suggest_mode(recent, window=size),
        bootstrap=bootstrap,
        suggested_runner_tp_bias=bias,
    )


def empty_stats(
    *,
    window: int = LADDER_STATS_WINDOW,
    source_id: int | None = None,
    source_title: str = "",
) -> ChannelLadderStats:
    """The bootstrap shape: no closed ladders yet, so the safer mode is suggested."""
    return aggregate_kinds([], window=window, source_id=source_id, source_title=source_title)


def blurb_for(stats: ChannelLadderStats) -> str:
    """Render the operator-agreed one-liner; the model must paste this, not invent counts."""
    title = (stats.source_title or "").strip() or "this channel"
    bias = int(stats.suggested_runner_tp_bias)
    body = (
        f"Channel {title} last {stats.window} ladders: raw_SL={stats.raw_sl_hits}, "
        f"BE_stopouts={stats.be_stopouts}, TP4+={stats.tp4_plus}. Suggested: "
        f"{stats.suggested_mode} (streak {stats.clean_streak}). "
        f"Runner-TP bias {bias:+d} (formula: min(ceil((N+1)/2)+bias, N-1); LONG excluded from N). "
        "Override only with reason."
    )
    if stats.bootstrap:
        return (
            f"{body} Bootstrap: only {stats.sample_size}/{stats.window} closed ladders, "
            f"so suggested stays {MODE_BE_AFTER_COVER} and runner-TP bias stays +0."
        )
    return body


def format_runner_tp_blurb(
    *,
    index: int,
    n: int,
    price: float,
    stats: ChannelLadderStats,
    chosen_index: int | None = None,
) -> str:
    """Concrete one-liner once N and the price are known, e.g. ``Suggested runner TP: 5/7 (4359).``"""
    price_txt = f"{float(price):.8f}".rstrip("0").rstrip(".")
    bias = int(stats.suggested_runner_tp_bias)
    if stats.bootstrap:
        reason = (
            f"Bias{bias:+d} bootstrap ({stats.sample_size}/{stats.window} closed)"
        )
    elif stats.raw_sl_hits > 0:
        reason = f"Bias{bias:+d} from raw_SL={stats.raw_sl_hits}"
    elif bias == 2:
        reason = f"Bias+2 from streak {stats.clean_streak}, tp4+={stats.tp4_plus}"
    elif bias == 1:
        reason = f"Bias+1 from streak {stats.clean_streak}"
    else:
        reason = f"Bias{bias:+d} from streak {stats.clean_streak}"
    line = f"Suggested runner TP: {int(index)}/{int(n)} ({price_txt}). {reason}."
    if chosen_index is not None and int(chosen_index) != int(index):
        line += f" Chosen TP{int(chosen_index)} (suggested ±1)."
    return line


def is_ladder_group(legs: Sequence[LadderLegView | Trade]) -> bool:
    """True when these tickets are a cover+runner ladder, including a one-leg netting cover."""
    views = [_as_view(leg) for leg in legs]
    if not views:
        return False
    if any(view.role in (_ROLE_COVER, _ROLE_RUNNER) for view in views):
        return True
    if any(int(view.split_count) == 2 and view.unmanaged_tps for view in views):
        return True
    return False


def classify_group(legs: Sequence[LadderLegView | Trade]) -> str:
    """Classify a fully-closed ladder from the runner (or the only remaining ticket).

    * ``tp4_plus`` — runner closed at its far take-profit
    * ``be_stopout`` — runner stopped at/beyond breakeven (after a BE trail)
    * ``raw_sl_hit`` — runner (or the whole ladder) closed by the channel stop at a loss
    * ``other`` — AI / manual / kill-switch / unknown
    """
    views = [_as_view(leg) for leg in legs]
    runner = _pick_runner(views)
    if runner is None:
        return OUTCOME_OTHER
    return _classify_runner(runner)


async def window_size(session: AsyncSession) -> int:
    """Read ``trading.ladder_stats_window``, falling back to the compiled default."""
    try:
        stored = await get_setting(session, KEY_STATS_WINDOW, None)
    except Exception as exc:  # noqa: BLE001 - a missing knob must not block a trade
        log.warning("ladder_stats_window_read_failed error=%s", exc)
        stored = None
    try:
        value = int(stored)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = int(LADDER_STATS_WINDOW)
    return value if value >= 1 else int(LADDER_STATS_WINDOW)


async def stats_for_source(
    session: AsyncSession,
    source_id: int | None,
    *,
    window: int | None = None,
) -> ChannelLadderStats:
    """Aggregate the last N closed ladders for ``source_id`` (or the active source)."""
    size = int(window) if window is not None and int(window) >= 1 else await window_size(session)
    await session.flush()
    sid, title = await _resolve_source(session, source_id)
    if sid is None:
        return empty_stats(window=size, source_title=title)
    result = await session.execute(
        select(LadderOutcome)
        .where(LadderOutcome.source_id == sid)
        .order_by(LadderOutcome.closed_at.desc(), LadderOutcome.id.desc())
        .limit(size)
    )
    rows = list(result.scalars().all())
    kinds = [str(row.kind) for row in rows]
    return aggregate_kinds(kinds, window=size, source_id=sid, source_title=title)


async def maybe_record_closed_group(
    session: AsyncSession,
    *,
    signal_ref: str | None,
    note: str | None = None,
) -> LadderOutcome | None:
    """Write one outcome row when every ladder ticket for ``signal_ref`` is fully closed.

    Idempotent on ``signal_ref``. Returns ``None`` when the group is not a ladder, still
    has a live ticket, or was already recorded. Never raises on a duplicate insert.
    """
    ref = (signal_ref or "").strip()
    if not ref:
        return None
    await session.flush()
    existing = await session.execute(
        select(LadderOutcome).where(LadderOutcome.signal_ref == ref)
    )
    found = existing.scalar_one_or_none()
    if found is not None:
        return found

    result = await session.execute(select(Trade).where(Trade.signal_ref == ref))
    trades = list(result.scalars().all())
    if not is_ladder_group(trades):
        return None
    if any(str(trade.status) in _LIVE_STATUSES for trade in trades):
        return None
    closed = [trade for trade in trades if str(trade.status) == "closed"]
    if not closed:
        return None

    views = [LadderLegView.from_trade(trade) for trade in closed]
    runner_trade = _pick_runner_trade(closed)
    cover_trade = _pick_cover_trade(closed)
    kind = classify_group(views)
    closed_at = _latest_closed_at(closed) or utcnow()
    sample = runner_trade or cover_trade or closed[0]
    runner_view = None if runner_trade is None else LadderLegView.from_trade(runner_trade)
    hit = False if runner_view is None else runner_hit_chosen_tp(runner_view)
    row = LadderOutcome(
        source_id=sample.source_id,
        signal_ref=ref,
        run_id=sample.run_id,
        symbol=sample.symbol,
        side=sample.side,
        runner_mode=sample.runner_mode,
        suggested_mode=sample.suggested_runner_mode,
        kind=kind,
        runner_close_cause=None if runner_trade is None else runner_trade.close_cause,
        runner_profit=None if runner_trade is None else runner_trade.profit,
        runner_tp=None if runner_trade is None else float(runner_trade.tp or 0.0),
        runner_sl=None if runner_trade is None else float(runner_trade.sl or 0.0),
        runner_close_price=None if runner_trade is None else runner_trade.close_price,
        cover_close_cause=None if cover_trade is None else cover_trade.close_cause,
        cover_profit=None if cover_trade is None else cover_trade.profit,
        suggested_runner_tp_index=sample.suggested_runner_tp_index,
        chosen_runner_tp_index=sample.chosen_runner_tp_index,
        runner_tp_bias=sample.runner_tp_bias,
        runner_hit_chosen_tp=hit,
        note=(note or None),
        closed_at=closed_at,
        created_at=utcnow(),
    )
    # SAVEPOINT so a duplicate signal_ref cannot roll back the caller's close transaction.
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        found = (
            await session.execute(select(LadderOutcome).where(LadderOutcome.signal_ref == ref))
        ).scalar_one_or_none()
        return found
    log.info(
        "ladder_outcome_recorded signal_ref=%s kind=%s runner_mode=%s",
        ref,
        kind,
        sample.runner_mode,
    )
    return row


def _as_view(leg: LadderLegView | Trade) -> LadderLegView:
    """Accept either a Trade row or an already-projected view."""
    if isinstance(leg, LadderLegView):
        return leg
    return LadderLegView.from_trade(leg)


def _pick_runner(views: Sequence[LadderLegView]) -> LadderLegView | None:
    """The far leg: explicit runner role, else the ticket whose TP is farthest from entry."""
    named = [view for view in views if view.role == _ROLE_RUNNER]
    if named:
        return named[0]
    if not views:
        return None
    return max(views, key=lambda view: abs(float(view.tp or 0.0) - float(view.entry or 0.0)))


def _pick_runner_trade(trades: Sequence[Trade]) -> Trade | None:
    """ORM counterpart of :func:`_pick_runner`."""
    named = [trade for trade in trades if str(trade.ladder_role or "") == _ROLE_RUNNER]
    if named:
        return named[0]
    if not trades:
        return None
    return max(
        trades,
        key=lambda trade: abs(float(trade.tp or 0.0) - float(trade.entry_price or 0.0)),
    )


def _pick_cover_trade(trades: Sequence[Trade]) -> Trade | None:
    """The cover ticket when one was labelled, else the nearer-TP sibling of the runner."""
    named = [trade for trade in trades if str(trade.ladder_role or "") == _ROLE_COVER]
    if named:
        return named[0]
    runner = _pick_runner_trade(trades)
    for trade in trades:
        if trade is not runner:
            return trade
    return None


def runner_hit_chosen_tp(runner: LadderLegView) -> bool:
    """True when the runner closed at its stored TP (the chosen ladder rung).

    A netting cover-only ticket is not a runner hit. Used for future bias tuning; independent
    of :data:`OUTCOME_TP4_PLUS` so BE/AI closes stay ``False``.
    """
    if runner.role == _ROLE_COVER:
        return False
    tp = float(runner.tp or 0.0)
    if tp <= 0.0:
        return False
    cause = str(runner.close_cause or "")
    if cause == "tp" or _price_near(runner.close_price, tp):
        return True
    return False


def _classify_runner(runner: LadderLegView) -> str:
    """Decide the group's kind from the runner's close."""
    cause = str(runner.close_cause or "")
    entry = float(runner.entry or 0.0)
    sl = float(runner.sl or 0.0)
    tp = float(runner.tp or 0.0)
    close = runner.close_price
    profit = runner.profit
    side = runner.side

    hit_tp = cause == "tp" or _price_near(close, tp)
    if hit_tp and tp > 0.0:
        # A netting ladder only ever places the cover 1R ticket; that TP is not TP4+.
        if runner.role == _ROLE_COVER:
            return OUTCOME_OTHER
        return OUTCOME_TP4_PLUS

    at_be = False
    if sl > 0.0 and entry > 0.0:
        at_be = stop_is_at_or_beyond_breakeven(side, entry, sl)
    close_at_entry = _price_near(close, entry)
    hit_sl = cause == "sl" or _price_near(close, sl)

    if hit_sl or at_be or close_at_entry:
        if at_be or close_at_entry:
            return OUTCOME_BE_STOPOUT
        if _is_loss(side, entry, close, profit):
            return OUTCOME_RAW_SL
        if cause == "sl":
            return OUTCOME_RAW_SL

    if cause in {"ai", "manual", "kill_switch"}:
        return OUTCOME_OTHER
    if _is_loss(side, entry, close, profit) and not at_be:
        return OUTCOME_RAW_SL
    return OUTCOME_OTHER


def _is_loss(
    side: str,
    entry: float,
    close: float | None,
    profit: float | None,
) -> bool:
    """True when the close (or recorded profit) is on the losing side of entry."""
    if profit is not None and profit < 0.0:
        return True
    if close is None or entry <= 0.0:
        return False
    direction = str(side or "").strip().lower()
    if direction == "buy":
        return float(close) < float(entry)
    if direction == "sell":
        return float(close) > float(entry)
    return False


def _price_near(left: float | None, right: float | None) -> bool:
    """True when two prices are the same level for classification purposes."""
    if left is None or right is None:
        return False
    a = float(left)
    b = float(right)
    if a <= 0.0 or b <= 0.0:
        return False
    return abs(a - b) <= max(0.05, abs(a) * 1e-4, abs(b) * 1e-4)


def _latest_closed_at(trades: Sequence[Trade]) -> datetime | None:
    """The latest ``closed_at`` among the group, or ``None`` when none were stamped."""
    stamps = [trade.closed_at for trade in trades if trade.closed_at is not None]
    if not stamps:
        return None
    return max(stamps)


async def _resolve_source(
    session: AsyncSession, source_id: int | None
) -> tuple[int | None, str]:
    """Return ``(id, title)`` for the given source, or the active one when ``source_id`` is None."""
    if source_id is not None:
        source = await session.get(Source, int(source_id))
        if source is None:
            return int(source_id), ""
        return int(source.id), str(source.title or "")
    result = await session.execute(select(Source).where(Source.is_active.is_(True)))
    source = result.scalars().first()
    if source is None:
        return None, ""
    return int(source.id), str(source.title or "")
