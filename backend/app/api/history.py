"""`GET /api/history/*` — closed trades and the aggregates rendered above them.

Two routes, both read-only and both served entirely from SQLite (CONTRACTS §18,
BUILD_PROMPT §9 "History"):

* ``GET /api/history/trades``     — closed trades, newest first, every filter optional.
* ``GET /api/history/aggregates`` — win rate, average R, total P/L and a per-symbol
  breakdown, optionally narrowed to one signal source.

Three decisions worth stating, because each of them is a place where a plausible
shortcut would produce a number the operator cannot trust:

**R is computed from the prices actually recorded on the row.** ``avg_r`` is the mean
of ``profit_points / risk_points``, and both legs share the symbol's ``point``, so the
point size cancels: ``R = signed_price_move / |entry - sl|``. Computing it that way
keeps the whole endpoint free of MT5 — an aggregates request must answer while the
sidecar is down — and it is more precise than the stored ``profit_points``, which
``reconcile.py`` rounds to one decimal. Rows whose ``sl`` was never recorded (``0.0``)
carry no risk denominator at all and are **skipped**, never folded in as ``0R``: a stop
we cannot see is unknown risk, and averaging it as "break-even" flatters the figure.

**"When it closed" means ``closed_at``, falling back to ``updated_at``.** A row can be
marked closed by reconciliation before its exact close time is known, and a row that
sorted or filtered on a NULL ``closed_at`` would silently vanish from the very list
that exists to prove nothing was lost. ``updated_at`` is never NULL.

**A bare calendar date as ``date_to`` covers that whole day.** The dashboard's filter is
an ``<input type="date">``, so ``date_to=2026-08-24`` arrives as midnight; taking it
literally would drop every trade closed on the day the operator selected. Both bounds
are inclusive and both are UTC, matching the timezone-naive UTC every column stores.
"""

from __future__ import annotations

from datetime import UTC, datetime, time as clock_time
from typing import Annotated, Any, Final, Iterable, Sequence

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import SignalPilotError, to_http
from app.models import Trade
from app.schemas import (
    AggregatesOut,
    AggregatesQuery,
    HistoryQuery,
    HistoryTradesOut,
    SymbolAggregateOut,
    TradeOut,
)
from app.util.logging import get_logger

__all__ = ["closed_at_expr", "list_trades", "read_aggregates", "router", "trade_r"]

log = get_logger(__name__)

router = APIRouter(prefix="/api/history", tags=["history"])

#: The only `Trade.status` this module reports. A cancelled pending order never reached
#: the market, so it carries no realized P/L and belongs in the orders view, not here.
CLOSED_STATUS: Final[str] = "closed"

#: Money is rounded to cents for display; R and rates keep more resolution because they
#: are ratios, and a win rate of 1/3 rendered as 0.33 loses a tenth of a percent.
MONEY_DIGITS: Final[int] = 2
RATIO_DIGITS: Final[int] = 6
R_DIGITS: Final[int] = 4


# ---------------------------------------------------------------------------
# Time bounds
# ---------------------------------------------------------------------------


def _naive_utc(value: datetime) -> datetime:
    """Return `value` as the timezone-naive UTC every timestamp column stores."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _range_start(value: datetime) -> datetime:
    """Return the inclusive lower bound of the date filter."""
    return _naive_utc(value)


def _range_end(value: datetime) -> datetime:
    """Return the inclusive upper bound, widening a bare calendar date to end of day."""
    end = _naive_utc(value)
    if end.time() == clock_time.min:
        # `2026-08-24` means the whole of the 24th, not the single instant midnight
        # begins — the dashboard's date input cannot express a time of day at all.
        return end.replace(hour=23, minute=59, second=59, microsecond=999999)
    return end


def closed_at_expr() -> ColumnElement[datetime]:
    """Return the "when it closed" expression used for both filtering and ordering."""
    return func.coalesce(Trade.closed_at, Trade.updated_at)


# ---------------------------------------------------------------------------
# R, wins and losses
# ---------------------------------------------------------------------------


def trade_r(
    side: str | None,
    entry_price: float | None,
    sl: float | None,
    close_price: float | None,
) -> float | None:
    """Return the trade's result in R, or ``None`` when the row cannot express one.

    ``R = profit_points / risk_points``. Both legs are that symbol's price distance
    divided by its ``point``, so ``point`` cancels and the ratio is computed from the
    prices themselves — which keeps this function pure, MT5-free and exact.

    ``None`` is returned, and the row is left out of ``avg_r`` entirely, when:

    * the stop was never recorded (``sl`` is ``0.0`` or missing) — unknown risk is not
      zero risk, and folding it in as ``0R`` would quietly improve the average;
    * the entry or close price is missing, which is the case for a row closed while the
      backend was down and never matched to a closing deal;
    * the stop sits exactly on the entry, which would divide by zero.
    """
    if entry_price is None or sl is None or close_price is None:
        return None
    entry = float(entry_price)
    stop = float(sl)
    close = float(close_price)
    if entry <= 0.0 or stop <= 0.0 or close <= 0.0:
        return None
    risk = abs(entry - stop)
    if risk <= 0.0:
        return None
    gain = (close - entry) if str(side) == "buy" else (entry - close)
    return gain / risk


class _Bucket:
    """Running totals for one slice of the history: overall, or one symbol."""

    __slots__ = ("count", "losses", "r_sum", "r_count", "total_profit", "wins")

    def __init__(self) -> None:
        """Start every counter at zero."""
        self.count: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.total_profit: float = 0.0
        self.r_sum: float = 0.0
        self.r_count: int = 0

    def add(self, profit: float | None, r: float | None) -> None:
        """Fold one closed trade into the bucket."""
        self.count += 1
        value = float(profit) if profit is not None else 0.0
        self.total_profit += value
        if value > 0.0:
            self.wins += 1
        elif value < 0.0:
            self.losses += 1
        if r is not None:
            self.r_sum += r
            self.r_count += 1

    @property
    def win_rate(self) -> float:
        """Return wins over every closed trade in the bucket, as a 0..1 ratio."""
        if self.count <= 0:
            return 0.0
        return round(self.wins / self.count, RATIO_DIGITS)

    @property
    def avg_r(self) -> float:
        """Return the mean R across the rows that could express one; 0.0 when none could."""
        if self.r_count <= 0:
            return 0.0
        return round(self.r_sum / self.r_count, R_DIGITS)

    @property
    def profit(self) -> float:
        """Return the total realized P/L in account currency, rounded to cents."""
        return round(self.total_profit, MONEY_DIGITS)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _closed_trades_stmt(
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    symbol: str | None = None,
    source_id: int | None = None,
) -> Select[Any]:
    """Build the shared WHERE clause: closed rows only, every filter optional."""
    closed_at = closed_at_expr()
    stmt: Select[Any] = select(Trade).where(Trade.status == CLOSED_STATUS)
    if date_from is not None:
        stmt = stmt.where(closed_at >= _range_start(date_from))
    if date_to is not None:
        stmt = stmt.where(closed_at <= _range_end(date_to))
    if symbol:
        text = symbol.strip()
        if text:
            # Case-insensitive so a hand-typed `xauusdm` finds the broker's `XAUUSDm`.
            stmt = stmt.where(func.upper(Trade.symbol) == text.upper())
    if source_id is not None:
        stmt = stmt.where(Trade.source_id == source_id)
    return stmt


async def list_trades(session: AsyncSession, query: HistoryQuery) -> list[Trade]:
    """Return the closed trades matching `query`, newest first, capped at its limit."""
    closed_at = closed_at_expr()
    stmt = _closed_trades_stmt(
        date_from=query.date_from,
        date_to=query.date_to,
        symbol=query.symbol,
    )
    stmt = stmt.order_by(closed_at.desc(), Trade.id.desc()).limit(int(query.limit))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def read_aggregates(session: AsyncSession, source_id: int | None) -> AggregatesOut:
    """Aggregate every closed trade, optionally narrowed to one signal source.

    The row set is exactly the one ``/api/history/trades`` lists unfiltered — foreign
    (``external``) rows included — so the totals reconcile with the table printed under
    them. A hidden filter here is how a summary and its own rows stop agreeing.
    """
    stmt = _closed_trades_stmt(source_id=source_id).with_only_columns(
        Trade.symbol,
        Trade.side,
        Trade.entry_price,
        Trade.sl,
        Trade.close_price,
        Trade.profit,
    )
    rows: Sequence[Any] = (await session.execute(stmt)).all()

    overall = _Bucket()
    by_symbol: dict[str, _Bucket] = {}
    for row in rows:
        r = trade_r(row.side, row.entry_price, row.sl, row.close_price)
        overall.add(row.profit, r)
        name = str(row.symbol or "")
        bucket = by_symbol.get(name)
        if bucket is None:
            bucket = _Bucket()
            by_symbol[name] = bucket
        bucket.add(row.profit, r)

    return AggregatesOut(
        win_rate=overall.win_rate,
        avg_r=overall.avg_r,
        total_profit=overall.profit,
        count=overall.count,
        by_symbol=_symbol_rows(by_symbol),
    )


def _symbol_rows(buckets: dict[str, _Bucket]) -> list[SymbolAggregateOut]:
    """Render the per-symbol buckets, busiest symbol first, ties broken by name."""
    items: Iterable[tuple[str, _Bucket]] = sorted(
        buckets.items(), key=lambda item: (-item[1].count, item[0])
    )
    return [
        SymbolAggregateOut(
            symbol=name,
            count=bucket.count,
            wins=bucket.wins,
            losses=bucket.losses,
            win_rate=bucket.win_rate,
            total_profit=bucket.profit,
            avg_r=bucket.avg_r,
        )
        for name, bucket in items
    ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/trades", response_model=HistoryTradesOut, summary="Closed trades, newest first")
async def get_history_trades(
    query: Annotated[HistoryQuery, Query()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HistoryTradesOut | JSONResponse:
    """Return closed trades, newest first, filtered by date range and symbol.

    `date_from` and `date_to` are UTC and both inclusive; a bare calendar date as
    `date_to` covers that entire day. Every filter is optional and `limit` caps the
    number of rows (default 200, hard ceiling 1000).
    """
    try:
        rows = await list_trades(session, query)
    except SignalPilotError as exc:
        status, body = to_http(exc)
        log.warning("history_trades_failed code=%s message=%s", exc.code, exc.message)
        return JSONResponse(status_code=status, content=body)
    return HistoryTradesOut(trades=[TradeOut.model_validate(row) for row in rows])


@router.get("/aggregates", response_model=AggregatesOut, summary="Win rate, average R, total P/L")
async def get_history_aggregates(
    query: Annotated[AggregatesQuery, Query()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AggregatesOut | JSONResponse:
    """Return the aggregates over every closed trade, optionally for one source.

    `win_rate` is a 0..1 ratio of profitable trades over closed trades. `avg_r` is the
    mean result in R over the rows that recorded a stop — rows without one are skipped,
    not counted as 0R. `total_profit` is in account currency, and `by_symbol` breaks all
    three down per symbol, busiest first.
    """
    try:
        return await read_aggregates(session, query.source_id)
    except SignalPilotError as exc:
        status, body = to_http(exc)
        log.warning("history_aggregates_failed code=%s message=%s", exc.code, exc.message)
        return JSONResponse(status_code=status, content=body)
