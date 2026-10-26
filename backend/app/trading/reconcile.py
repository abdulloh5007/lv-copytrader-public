"""Startup and reconnect recovery: match MT5 back onto the database (BUILD_PROMPT §8).

The process will restart while positions are open. It will also lose the sidecar and get it
back. In both cases MT5 is the authority on what exists and the database is the authority on
*why* it exists, and this module is the only place where the two are made to agree again.

Four passes, in this order, because each one depends on the previous:

1. **Snapshot.** One ``positions_get()`` and one ``orders_get()``. If either call fails the
   whole reconcile stops here and reports it. A failed snapshot is indistinguishable from an
   empty account, and treating "the sidecar is down" as "every position vanished" would close
   every row in the database and notify the operator that trades they still hold are gone.

2. **Link.** Every live ticket is matched against :class:`~app.models.Trade` by ticket. An
   unknown ticket carrying ``settings.MAGIC_NUMBER`` is ours: its ``comment`` holds the
   ``SP:xxxxxxxx`` tag written by :func:`make_comment`, which names the :class:`~app.models.Run`
   that opened it, which names the message, which gives the ``signal_ref``. When no run matches
   the row is still created — unlinked, but *managed*: it carries our magic, so the SL watchdog
   and the kill switch must be able to see it. An unknown ticket with a **foreign** magic
   belongs to someone else. It is recorded ``external=True`` for display and is never modified,
   never closed, and never touched by the kill switch.

3. **Close what is missing.** A row marked ``open``/``pending`` whose ticket is absent from the
   snapshot was closed while we were down. ``history_deals_get`` over a padded window
   reconstructs the realized P/L, the close price, the close time and — from the deal's
   ``reason`` — whether it was the take profit, the stop, or a human. A pending order that
   vanished leaving no deal behind was never filled: it is ``cancelled``, not ``closed``.

4. **Catch up.** Messages that arrived while the process was down are *not* this module's
   business; :meth:`app.telegram.ingest.Ingestor.catch_up` owns that path, including the
   first-run baseline that stops a channel's backlog being replayed as live signals. Reconcile
   calls it and counts what it returned, nothing more.

The whole thing is wrapped: a reconcile failure logs loudly, emits ``system.warning``, and
returns zeroed counts. It never prevents the application from starting. An operator who cannot
start the app cannot fix the account either.

Broker time runs through every date here. MT5 reports deal and position times on the **broker's**
clock (usually UTC+2/+3), so every timestamp crossing this boundary goes through
:func:`_utc_from_broker` / :func:`_broker_from_utc`, and the history window is padded a day into
the future exactly as BUILD_PROMPT §2 requires.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Final, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import settings as _settings
from app.db import session_scope
from app.models import Message, Run, SystemState, Trade, TradeEvent, utcnow
from app.trading import ladder_stats, pricing
from app.trading.constants import (
    ORDER_TYPE_BUY,
    ORDER_TYPE_BUY_LIMIT,
    ORDER_TYPE_BUY_STOP,
    ORDER_TYPE_BUY_STOP_LIMIT,
    ORDER_TYPE_SELL,
    ORDER_TYPE_SELL_LIMIT,
    ORDER_TYPE_SELL_STOP,
    ORDER_TYPE_SELL_STOP_LIMIT,
    POSITION_TYPE_BUY,
    POSITION_TYPE_SELL,
)
from app.trading.symbols import server_time_offset
from app.util.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - type-checker only; importing Runtime here is a cycle
    from app.runtime import Runtime

__all__ = [
    "reconcile",
    "link_tickets",
    "close_missing",
    "make_comment",
    "run_id_from_comment",
    "COMMENT_PREFIX",
]

log = get_logger(__name__)

# --------------------------------------------------------------------------------------
# The order comment tag.
#
# `comment` is NOT a tool parameter (BUILD_PROMPT §5.3): the executor owns that field
# precisely so this module can read it back. MT5 caps a comment at 31 characters and some
# brokers decorate or truncate it, so the tag is short and is *searched* for rather than
# compared against the whole string.
# --------------------------------------------------------------------------------------
COMMENT_PREFIX: Final[str] = "SP:"

#: How many hex characters of ``Run.id`` (a 32-char uuid4 hex) the tag carries.
COMMENT_HEX_CHARS: Final[int] = 8

#: Shortest tail-truncated tag still specific enough to look a run up by prefix. Below this a
#: prefix would match far too many runs to be evidence of anything.
MIN_TAG_CHARS: Final[int] = 4

# Case-insensitive: a broker that upper-cases the whole comment must not cost us the link.
_TAG_RE: Final[re.Pattern[str]] = re.compile(
    rf"{COMMENT_PREFIX}([0-9a-fA-F]{{1,{COMMENT_HEX_CHARS}}})", re.IGNORECASE
)

# --------------------------------------------------------------------------------------
# History window.
#
# `date_from` is one hour before the last instant we know each missing row was still alive,
# and `date_to` is a day into the future. Both paddings exist for the same reason: MT5 dates
# are broker-server time and our clock is not, so a window that looks exact is not
# (BUILD_PROMPT §2). The lookback is bounded so a row left open for years cannot turn one
# startup call into a multi-year history scan; when the bound bites we say so.
# --------------------------------------------------------------------------------------
HISTORY_PAD_BEFORE_SECONDS: Final[float] = 3600.0
HISTORY_PAD_AFTER_SECONDS: Final[float] = 86400.0
HISTORY_MAX_LOOKBACK_DAYS: Final[int] = 365
HISTORY_DEFAULT_LOOKBACK_DAYS: Final[int] = 7

#: SQLite's default host-parameter limit is 999; chunk every ``IN (...)`` well under it.
TICKET_CHUNK: Final[int] = 400

#: Trade rows in these states are candidates for :func:`close_missing`.
LIVE_STATUSES: Final[tuple[str, ...]] = ("open", "pending")

# --------------------------------------------------------------------------------------
# ENUM_DEAL_ENTRY and ENUM_DEAL_REASON.
#
# These belong here rather than in `trading/constants.py`: that module is exactly the set
# named by BUILD_PROMPT §1.2 / CONTRACTS §2, and reconciliation is the only reader of the
# deal enums. They are literals from the MQL5 documentation for the same reason every other
# MT5 integer in this backend is — `MetaTrader5` is Windows-only and is never imported here.
# --------------------------------------------------------------------------------------
DEAL_ENTRY_IN: Final[int] = 0
DEAL_ENTRY_OUT: Final[int] = 1
DEAL_ENTRY_INOUT: Final[int] = 2
DEAL_ENTRY_OUT_BY: Final[int] = 3

#: Deal entries that take volume off a position — the ones that carry a close price.
CLOSING_ENTRIES: Final[frozenset[int]] = frozenset(
    {DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY}
)

DEAL_REASON_CLIENT: Final[int] = 0
DEAL_REASON_MOBILE: Final[int] = 1
DEAL_REASON_WEB: Final[int] = 2
DEAL_REASON_EXPERT: Final[int] = 3
DEAL_REASON_SL: Final[int] = 4
DEAL_REASON_TP: Final[int] = 5
DEAL_REASON_SO: Final[int] = 6
DEAL_REASON_ROLLOVER: Final[int] = 7
DEAL_REASON_VMARGIN: Final[int] = 8
DEAL_REASON_SPLIT: Final[int] = 9

#: Deal reason -> `Trade.close_cause`, restricted to the vocabulary CONTRACTS §7 allows
#: ("tp"|"sl"|"ai"|"manual"|"kill_switch"|"unknown"). A deal made by an expert or by the
#: Python API is this system (or another EA on the same terminal), which is the closest
#: allowed cause to the truth; a stop-out, a rollover or a split is none of them and stays
#: "unknown" with the raw reason recorded on the TradeEvent rather than being guessed at.
CLOSE_CAUSE_BY_DEAL_REASON: Final[dict[int, str]] = {
    DEAL_REASON_CLIENT: "manual",
    DEAL_REASON_MOBILE: "manual",
    DEAL_REASON_WEB: "manual",
    DEAL_REASON_EXPERT: "ai",
    DEAL_REASON_SL: "sl",
    DEAL_REASON_TP: "tp",
}

#: Human names for every deal reason, for the audit row and the log line.
DEAL_REASON_NAMES: Final[dict[int, str]] = {
    DEAL_REASON_CLIENT: "client",
    DEAL_REASON_MOBILE: "mobile",
    DEAL_REASON_WEB: "web",
    DEAL_REASON_EXPERT: "expert",
    DEAL_REASON_SL: "stop_loss",
    DEAL_REASON_TP: "take_profit",
    DEAL_REASON_SO: "stop_out",
    DEAL_REASON_ROLLOVER: "rollover",
    DEAL_REASON_VMARGIN: "variation_margin",
    DEAL_REASON_SPLIT: "split",
}

#: MT5 order/position type -> the lower-case side string the ORM and the tools use.
SIDE_BY_TYPE: Final[dict[int, str]] = {
    ORDER_TYPE_BUY: "buy",
    ORDER_TYPE_SELL: "sell",
    ORDER_TYPE_BUY_LIMIT: "buy",
    ORDER_TYPE_SELL_LIMIT: "sell",
    ORDER_TYPE_BUY_STOP: "buy",
    ORDER_TYPE_SELL_STOP: "sell",
    ORDER_TYPE_BUY_STOP_LIMIT: "buy",
    ORDER_TYPE_SELL_STOP_LIMIT: "sell",
}

#: Notification type strings (mirrors app.telegram.notifier.TYPE_*). Spelled as literals so
#: the trading package does not import the telegram package for two constants.
NOTIFY_CLOSED: Final[str] = "closed"

#: How much of an exception message is quoted into a warning payload.
MAX_ERROR_CHARS: Final[int] = 300


# --------------------------------------------------------------------------------------
# The comment tag
# --------------------------------------------------------------------------------------
def make_comment(run_id: str) -> str:
    """Return the MT5 order comment that ties a ticket back to its run.

    ``SP:`` plus the first eight hex characters of the run id — eleven characters inside
    MT5's 31-character limit, leaving room for a broker to decorate it.
    """
    return f"{COMMENT_PREFIX}{str(run_id)[:COMMENT_HEX_CHARS]}"


def run_id_from_comment(comment: str) -> str | None:
    """Recover the run-id prefix from an MT5 order comment, or ``None`` when there is none.

    The tag is *searched* for, not matched against the whole string: brokers routinely prepend
    or append their own text (``"[tp] SP:1a2b3c4d"``, ``"SP:1a2b3c4d cancelled"``), and a
    prefix comparison would throw the link away every time. The search is case-insensitive
    for the same reason.

    A tag whose hex run is shorter than :data:`COMMENT_HEX_CHARS` is accepted only when the
    comment ends there — that is what truncation looks like, and it is still a usable prefix
    for the ``startswith`` lookup — and only down to :data:`MIN_TAG_CHARS`, below which a
    prefix stops being evidence of anything. The result is lower-cased so it can be compared
    against ``Run.id``, which is ``uuid4().hex``.
    """
    if not comment:
        return None
    text = str(comment)
    for match in _TAG_RE.finditer(text):
        digits = match.group(1)
        if len(digits) == COMMENT_HEX_CHARS:
            return digits.lower()
        # Shorter than a full tag: only credible as the tail of a truncated comment.
        if len(digits) >= MIN_TAG_CHARS and match.end() == len(text):
            return digits.lower()
    return None


# --------------------------------------------------------------------------------------
# Small value types
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LiveSnapshot:
    """What MT5 says exists right now, plus whether it could be asked at all."""

    positions: tuple[Any, ...] = ()
    orders: tuple[Any, ...] = ()
    ok: bool = False
    error: str | None = None

    @property
    def tickets(self) -> set[int]:
        """Return every live ticket, positions and pending orders together."""
        live: set[int] = set()
        for row in self.positions:
            live.add(int(row.ticket))
        for row in self.orders:
            live.add(int(row.ticket))
        return live


@dataclass
class LinkResult:
    """Outcome of one linking pass over the live snapshot."""

    linked: int = 0
    external: int = 0
    orphans: int = 0
    created: int = 0
    updated: int = 0


@dataclass(frozen=True)
class SignalLink:
    """The signal a ``SP:`` tag resolved to, or the reason it did not resolve."""

    prefix: str | None = None
    run_id: str | None = None
    source_id: int | None = None
    message_row_id: int | None = None
    signal_ref: str | None = None
    ambiguous: bool = False

    @property
    def resolved(self) -> bool:
        """Report whether a run was found for the tag."""
        return self.run_id is not None


@dataclass
class ClosedRow:
    """One reconstructed close, ready to be emitted and notified after the flush."""

    ticket: int
    trade_id: int | None
    symbol: str
    side: str
    kind: str
    status: str
    volume: float
    entry_price: float
    close_price: float | None
    profit: float | None
    profit_points: float | None
    close_cause: str
    closed_at: datetime
    signal_ref: str | None
    external: bool
    digits: int | None
    deal_reason: int | None
    deal_count: int
    permalink: str | None = None
    tp_index: int | None = None
    ladder_role: str | None = None
    breakeven: bool = False
    remaining: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------------------
def _optional(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field MT5 documents but a broker or a test double may not send.

    Attribute access is the contract on RPC results (BUILD_PROMPT §1.1) and everything in the
    money path here uses it directly. This helper exists for the handful of *descriptive*
    fields — a deal's ``reason``, a position's ``comment`` — where the value only decorates an
    audit row and an ``AttributeError`` would abort a reconcile over a label.
    """
    try:
        value = getattr(obj, name)
    except AttributeError:
        return default
    return default if value is None else value


def _as_rows(result: Any) -> tuple[Any, ...]:
    """Normalise an RPC collection result into a tuple, treating ``None`` as empty.

    ``positions_get`` returns ``None`` rather than an empty tuple on some builds, and a
    ``None`` here would raise inside the first ``for`` loop of the money path.
    """
    if result is None:
        return ()
    if isinstance(result, tuple):
        return result
    if isinstance(result, list):
        return tuple(result)
    return (result,)


def _utc_from_broker(ts: Any, offset: float) -> datetime | None:
    """Convert a broker-clock unix timestamp into a naive UTC datetime, or ``None``."""
    try:
        value = float(ts) - float(offset)
    except (TypeError, ValueError):
        return None
    if value <= 0.0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _broker_from_utc(moment: datetime, offset: float) -> float:
    """Convert a naive UTC datetime into a broker-clock unix timestamp."""
    return moment.replace(tzinfo=timezone.utc).timestamp() + float(offset)


def _side_of(order_type: int, *, position: bool) -> str:
    """Return "buy"/"sell" for an MT5 type integer, defaulting to the long side.

    Position types (0/1) and order types (0..7) share the low two values, so one table
    serves both; ``position`` only decides which fallback reads better in a log line.
    """
    if position:
        if order_type == POSITION_TYPE_SELL:
            return "sell"
        if order_type == POSITION_TYPE_BUY:
            return "buy"
    return SIDE_BY_TYPE.get(int(order_type), "buy")


def _chunks(values: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    """Yield ``values`` in slices of at most ``size`` items."""
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _error_text(exc: BaseException) -> str:
    """Render an exception as a short one-line string for a warning payload."""
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]


def _emit(runtime: "Runtime", reason: str, **data: Any) -> None:
    """Push a ``system.warning`` onto the bus; never raises, never blocks."""
    try:
        runtime.bus.emit(events.SYSTEM_WARNING, reason=reason, **data)
    except Exception as exc:  # noqa: BLE001 - the dashboard is not worth a failed reconcile
        log.warning("reconcile_emit_failed reason=%s error=%s", reason, exc)


def _emit_event(runtime: "Runtime", type: str, **data: Any) -> None:
    """Push any event onto the bus; never raises, never blocks."""
    try:
        runtime.bus.emit(type, **data)
    except Exception as exc:  # noqa: BLE001 - see _emit
        log.warning("reconcile_emit_failed type=%s error=%s", type, exc)


def _notify(runtime: "Runtime", type: str, text: str, *, link: str | None = None) -> None:
    """Fire one operator notification in the background; never raises, never awaits."""
    notifier = getattr(runtime, "notifier", None)
    if notifier is None:
        return
    try:
        notifier.fire(type, text, link=link)
    except Exception as exc:  # noqa: BLE001 - a notification never breaks reconciliation
        log.warning("reconcile_notify_failed type=%s error=%s", type, exc)


async def _broker_offset(runtime: "Runtime") -> float:
    """Return broker-server seconds minus wall clock, or 0.0 when it cannot be measured."""
    rpc = getattr(runtime, "rpc", None)
    resolver = getattr(runtime, "resolver", None)
    if rpc is None or resolver is None:
        return 0.0
    try:
        return float(await server_time_offset(rpc, resolver))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unmeasured clock is a warning, not a stop
        log.warning("reconcile_clock_probe_failed error=%s", exc)
        return 0.0


# --------------------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------------------
async def _snapshot(runtime: "Runtime") -> LiveSnapshot:
    """Read every open position and pending order in one pass.

    Failure is reported, never guessed at. ``ok=False`` stops the caller from interpreting an
    unreachable sidecar as an emptied account — which would close every row in the database
    and tell the operator their live trades were gone.
    """
    rpc = getattr(runtime, "rpc", None)
    if rpc is None:
        return LiveSnapshot(ok=False, error="the MT5 RPC client is not wired into the runtime")
    try:
        positions = _as_rows(await rpc.positions_get())
        orders = _as_rows(await rpc.orders_get())
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported to the caller as ok=False
        return LiveSnapshot(ok=False, error=_error_text(exc))
    return LiveSnapshot(positions=positions, orders=orders, ok=True, error=None)


# --------------------------------------------------------------------------------------
# Pass 2 — link live tickets back onto the database
# --------------------------------------------------------------------------------------
async def _load_by_ticket(session: AsyncSession, tickets: Sequence[int]) -> dict[int, Trade]:
    """Load the Trade rows for a set of tickets, chunked under SQLite's parameter limit."""
    found: dict[int, Trade] = {}
    unique = sorted(set(int(t) for t in tickets))
    for chunk in _chunks(unique, TICKET_CHUNK):
        stmt = select(Trade).where(Trade.ticket.in_(list(chunk)))
        for row in (await session.execute(stmt)).scalars():
            found[int(row.ticket)] = row
    return found


async def _resolve_link(session: AsyncSession, comment: str) -> SignalLink:
    """Turn an order comment into the run, message and ``signal_ref`` behind it.

    An ambiguous prefix — more than one run whose id starts with it, which a truncated tag can
    produce — resolves to nothing. Attaching a ticket to the wrong signal would corrupt the
    duplicate guard of §5.5 for both signals, and an unlinked-but-managed ticket is a strictly
    better outcome than a mislinked one.
    """
    prefix = run_id_from_comment(comment)
    if prefix is None:
        return SignalLink()
    stmt = (
        select(Run)
        .where(Run.id.startswith(prefix, autoescape=True))
        .order_by(Run.started_at.desc())
        .limit(2)
    )
    runs = list((await session.execute(stmt)).scalars().all())
    if not runs:
        return SignalLink(prefix=prefix)
    if len(runs) > 1:
        return SignalLink(prefix=prefix, ambiguous=True)
    run = runs[0]
    signal_ref: str | None = None
    if run.message_row_id is not None:
        message = await session.get(Message, int(run.message_row_id))
        if message is not None:
            signal_ref = message.signal_ref
    return SignalLink(
        prefix=prefix,
        run_id=run.id,
        source_id=run.source_id,
        message_row_id=run.message_row_id,
        signal_ref=signal_ref,
    )


def _apply_position(row: Trade, live: Any, *, offset: float, magic: int, ours: bool) -> bool:
    """Copy an MT5 position onto a Trade row; return whether anything changed."""
    opened_at = _utc_from_broker(_optional(live, "time", 0), offset)
    values: dict[str, Any] = {
        "kind": "position",
        "symbol": str(live.symbol),
        "side": _side_of(int(live.type), position=True),
        "order_type": int(live.type),
        "volume": float(live.volume),
        "entry_price": float(live.price_open),
        "sl": float(live.sl),
        "tp": float(live.tp),
        "status": "open",
        "magic": magic,
        "comment": str(_optional(live, "comment", "") or "")[:64],
        "external": not ours,
    }
    if opened_at is not None:
        values["opened_at"] = opened_at
    changed = False
    for name, value in values.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    # A ticket that is alive at the broker is not closed here, whatever the row said.
    if row.closed_at is not None or row.close_price is not None or row.close_cause is not None:
        row.closed_at = None
        row.close_price = None
        row.close_cause = None
        row.profit = None
        row.profit_points = None
        changed = True
    return changed


def _apply_order(row: Trade, live: Any, *, offset: float, magic: int, ours: bool) -> bool:
    """Copy an MT5 pending order onto a Trade row; return whether anything changed."""
    opened_at = _utc_from_broker(_optional(live, "time_setup", 0), offset)
    values: dict[str, Any] = {
        "kind": "order",
        "symbol": str(live.symbol),
        "side": _side_of(int(live.type), position=False),
        "order_type": int(live.type),
        "volume": float(live.volume_current),
        "requested_volume": float(_optional(live, "volume_initial", live.volume_current)),
        "entry_price": float(live.price_open),
        "sl": float(live.sl),
        "tp": float(live.tp),
        "status": "pending",
        "magic": magic,
        "comment": str(_optional(live, "comment", "") or "")[:64],
        "external": not ours,
    }
    if opened_at is not None:
        values["opened_at"] = opened_at
    changed = False
    for name, value in values.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    if row.closed_at is not None or row.close_cause is not None:
        row.closed_at = None
        row.close_cause = None
        changed = True
    return changed


async def _link_tickets(
    runtime: "Runtime",
    session: AsyncSession,
    *,
    snapshot: LiveSnapshot | None = None,
    offset: float | None = None,
) -> LinkResult:
    """Reconcile every live ticket onto a Trade row. Shared body of :func:`link_tickets`.

    ``linked`` counts tickets carrying our magic that this pass attached to a signal — a new
    row created with a ``signal_ref``, or an existing row whose ``signal_ref`` went from
    ``None`` to a value. ``orphans`` counts tickets carrying our magic that end the pass with
    no ``signal_ref``: managed, but with no channel post behind them. ``external`` counts live
    tickets with a foreign magic, created or refreshed.
    """
    result = LinkResult()
    live = snapshot if snapshot is not None else await _snapshot(runtime)
    if not live.ok:
        _emit(
            runtime,
            "reconcile_snapshot_failed",
            message=live.error or "MT5 did not answer positions_get/orders_get",
        )
        log.error("reconcile_snapshot_failed error=%s", live.error)
        return result

    clock = offset if offset is not None else await _broker_offset(runtime)
    our_magic = int(getattr(runtime.settings, "MAGIC_NUMBER", _settings.MAGIC_NUMBER))

    tickets = live.tickets
    existing = await _load_by_ticket(session, sorted(tickets))
    link_cache: dict[str, SignalLink] = {}

    created_events: list[tuple[str, dict[str, Any]]] = []
    orphan_tickets: list[int] = []
    external_new: list[int] = []
    resurrected: list[int] = []

    async def _link_for(comment: str) -> SignalLink:
        """Resolve a comment to its signal once per distinct tag inside this pass."""
        prefix = run_id_from_comment(comment)
        if prefix is None:
            return SignalLink()
        cached = link_cache.get(prefix)
        if cached is None:
            cached = await _resolve_link(session, comment)
            link_cache[prefix] = cached
        return cached

    async def _handle(live_row: Any, *, is_position: bool) -> None:
        """Create or refresh the Trade row behind one live ticket."""
        ticket = int(live_row.ticket)
        magic = int(live_row.magic)
        ours = magic == our_magic
        comment = str(_optional(live_row, "comment", "") or "")
        row = existing.get(ticket)

        if row is None:
            link = await _link_for(comment) if ours else SignalLink()
            if ours and link.ambiguous:
                _emit(
                    runtime,
                    "reconcile_ambiguous_run",
                    ticket=ticket,
                    prefix=link.prefix,
                    message=(
                        f"ticket #{ticket} carries the tag {COMMENT_PREFIX}{link.prefix} but more "
                        "than one run matches that prefix — the ticket is managed but unlinked"
                    ),
                )
            row = Trade(
                ticket=ticket,
                source_id=link.source_id if ours else None,
                run_id=link.run_id if ours else None,
                message_row_id=link.message_row_id if ours else None,
                signal_ref=link.signal_ref if ours else None,
                kind="position" if is_position else "order",
                symbol=str(live_row.symbol),
                side="buy",
                order_type=int(live_row.type),
                volume=0.0,
                requested_volume=None,
                entry_price=0.0,
                sl=0.0,
                tp=0.0,
                status="open" if is_position else "pending",
                magic=magic,
                comment=comment[:64],
                external=not ours,
                split_index=0,
                split_count=1,
                unmanaged_tps=[],
                open_reason=None,
            )
            session.add(row)
            existing[ticket] = row
            result.created += 1
            if ours:
                if link.signal_ref is not None:
                    result.linked += 1
                else:
                    orphan_tickets.append(ticket)
            else:
                external_new.append(ticket)
            created_events.append(
                (
                    events.POSITION_OPENED if is_position else events.ORDER_PLACED,
                    {
                        "ticket": ticket,
                        "symbol": str(live_row.symbol),
                        "magic": magic,
                        "external": not ours,
                        "signal_ref": link.signal_ref if ours else None,
                        "reconciled": True,
                    },
                )
            )
            session.add(
                TradeEvent(
                    ticket=ticket,
                    run_id=link.run_id if ours else None,
                    type="reconcile",
                    detail={
                        "action": "recovered",
                        "kind": "position" if is_position else "order",
                        "magic": magic,
                        "external": not ours,
                        "comment": comment,
                        "tag": link.prefix,
                        "signal_ref": link.signal_ref if ours else None,
                        "ambiguous_tag": link.ambiguous,
                    },
                )
            )
        else:
            was_closed = row.status not in LIVE_STATUSES
            if row.external != (not ours):
                # Only reachable when MAGIC_NUMBER changed under a live account. The broker's
                # magic is the truth; say so rather than silently flipping ownership.
                _emit(
                    runtime,
                    "reconcile_ownership_changed",
                    ticket=ticket,
                    magic=magic,
                    our_magic=our_magic,
                    message=(
                        f"ticket #{ticket} now carries magic {magic} while MAGIC_NUMBER is "
                        f"{our_magic}; it is recorded as "
                        + ("external" if not ours else "ours")
                    ),
                )
            if ours and row.signal_ref is None:
                link = await _link_for(comment)
                if link.signal_ref is not None:
                    row.signal_ref = link.signal_ref
                    row.run_id = row.run_id or link.run_id
                    row.source_id = row.source_id if row.source_id is not None else link.source_id
                    row.message_row_id = (
                        row.message_row_id
                        if row.message_row_id is not None
                        else link.message_row_id
                    )
                    result.linked += 1
            if was_closed:
                resurrected.append(ticket)
            result.updated += 1
            if not ours:
                result.external += 1

        changed = (
            _apply_position(row, live_row, offset=clock, magic=magic, ours=ours)
            if is_position
            else _apply_order(row, live_row, offset=clock, magic=magic, ours=ours)
        )
        if changed:
            row.updated_at = utcnow()
        if ours and row.signal_ref is None and ticket not in orphan_tickets:
            orphan_tickets.append(ticket)

    for position in live.positions:
        await _handle(position, is_position=True)
    for order in live.orders:
        await _handle(order, is_position=False)

    result.external += len(external_new)
    result.orphans = len(orphan_tickets)

    await session.flush()

    for event_type, payload in created_events:
        _emit_event(runtime, event_type, **payload)

    if orphan_tickets:
        _emit(
            runtime,
            "reconcile_orphan_tickets",
            tickets=orphan_tickets,
            message=(
                f"{len(orphan_tickets)} ticket(s) carry magic {our_magic} but no signal could be "
                "recovered from their comment — they are managed but not linked to a channel post"
            ),
        )
    if external_new:
        _emit(
            runtime,
            "reconcile_external_tickets",
            tickets=external_new,
            message=(
                f"{len(external_new)} ticket(s) with a foreign magic are recorded read-only; "
                "LV Copytrader never modifies or closes them, including from the kill switch"
            ),
        )
    if resurrected:
        _emit(
            runtime,
            "reconcile_resurrected",
            tickets=resurrected,
            message=(
                f"{len(resurrected)} ticket(s) were marked closed in the database but are live at "
                "the broker — the database has been corrected from MT5"
            ),
        )

    log.info(
        "reconcile_linked positions=%d orders=%d created=%d updated=%d linked=%d "
        "external=%d orphans=%d",
        len(live.positions),
        len(live.orders),
        result.created,
        result.updated,
        result.linked,
        result.external,
        result.orphans,
    )
    return result


async def link_tickets(runtime: "Runtime", session: AsyncSession) -> tuple[int, int]:
    """Match every live MT5 ticket onto a Trade row and return ``(linked, external)``."""
    result = await _link_tickets(runtime, session)
    return result.linked, result.external


# --------------------------------------------------------------------------------------
# Pass 3 — close the rows whose tickets are gone
# --------------------------------------------------------------------------------------
async def _load_live_rows(session: AsyncSession) -> list[Trade]:
    """Load every Trade row the database still believes is open or pending."""
    stmt = select(Trade).where(Trade.status.in_(list(LIVE_STATUSES))).order_by(Trade.id)
    return list((await session.execute(stmt)).scalars().all())


def _last_seen_utc(rows: Sequence[Trade]) -> tuple[datetime, bool]:
    """Return the earliest instant any missing row was still known alive, and whether it clamped.

    ``updated_at`` is exactly that instant: the last time this process wrote the row while it
    was open. Whatever closed the ticket happened after it, so a window starting there cannot
    miss the closing deal. ``opened_at`` is the fallback for a row written by an older path.
    """
    stamps: list[datetime] = []
    for row in rows:
        for value in (row.updated_at, row.opened_at):
            if isinstance(value, datetime):
                stamps.append(value)
                break
    now = utcnow()
    if not stamps:
        return now - timedelta(days=HISTORY_DEFAULT_LOOKBACK_DAYS), False
    earliest = min(stamps)
    floor = now - timedelta(days=HISTORY_MAX_LOOKBACK_DAYS)
    if earliest < floor:
        return floor, True
    return earliest, False


def _group_deals(deals: Sequence[Any]) -> dict[int, list[Any]]:
    """Group history deals by the position they belong to, in chronological order."""
    grouped: dict[int, list[Any]] = {}
    for deal in deals:
        position_id = int(deal.position_id)
        if position_id <= 0:
            continue
        grouped.setdefault(position_id, []).append(deal)
    for rows in grouped.values():
        rows.sort(key=lambda d: (float(_optional(d, "time", 0.0)), int(d.ticket)))
    return grouped


def _realized(deals: Sequence[Any]) -> float:
    """Sum profit, swap, commission and fee across every deal of one position.

    All four are part of what the account actually kept: a trade that made 12.00 of profit and
    paid 3.10 of commission and swap realized 8.90, and reporting only ``profit`` would make
    the history table disagree with the balance.
    """
    total = 0.0
    for deal in deals:
        total += float(deal.profit)
        total += float(deal.swap)
        total += float(deal.commission)
        total += float(_optional(deal, "fee", 0.0))
    return round(total, 2)


def _closing_deals(deals: Sequence[Any]) -> list[Any]:
    """Return the deals that took volume off the position, newest last."""
    return [d for d in deals if int(_optional(d, "entry", DEAL_ENTRY_IN)) in CLOSING_ENTRIES]


def _close_price(deals: Sequence[Any]) -> float | None:
    """Return the volume-weighted average price of the closing deals.

    A position closed in parts — a partial close, or a broker filling one exit across several
    deals — has no single close price, and quoting only the last one would misreport every
    scaled exit. Weighting by volume is what the P/L already reflects.
    """
    closing = _closing_deals(deals)
    if not closing:
        return None
    weighted = 0.0
    volume = 0.0
    for deal in closing:
        deal_volume = float(deal.volume)
        if deal_volume <= 0.0:
            continue
        weighted += float(deal.price) * deal_volume
        volume += deal_volume
    if volume <= 0.0:
        return float(closing[-1].price)
    return weighted / volume


def _close_time(deals: Sequence[Any], offset: float) -> datetime:
    """Return the UTC instant of the last closing deal, falling back to the last deal, then now."""
    candidates = _closing_deals(deals) or list(deals)
    for deal in reversed(candidates):
        moment = _utc_from_broker(_optional(deal, "time", 0), offset)
        if moment is not None:
            return moment
    return utcnow()


def _close_cause(deals: Sequence[Any]) -> tuple[str, int | None]:
    """Infer the close cause from the last closing deal's reason.

    MT5 records *why* each deal happened, which is the only trustworthy way to tell a take
    profit from a stop from a human clicking Close while we were down. A broker or a build
    that omits the field leaves the cause "unknown" rather than inventing one.
    """
    candidates = _closing_deals(deals) or list(deals)
    for deal in reversed(candidates):
        raw = _optional(deal, "reason", None)
        if raw is None:
            continue
        try:
            reason = int(raw)
        except (TypeError, ValueError):
            continue
        return CLOSE_CAUSE_BY_DEAL_REASON.get(reason, "unknown"), reason
    return "unknown", None


def _profit_points(side: str, entry: float, close: float | None, point: float | None) -> float | None:
    """Return the move from entry to close in points, signed so a winner is positive."""
    if close is None or not point or point <= 0.0 or entry <= 0.0:
        return None
    delta = (close - entry) if side == "buy" else (entry - close)
    return round(delta / point, 1)


async def _symbol_facts(
    runtime: "Runtime", cache: dict[str, tuple[float, int] | None], name: str
) -> tuple[float, int] | None:
    """Return ``(point, digits)`` for a symbol, cached, or ``None`` when the broker has no such symbol."""
    if name in cache:
        return cache[name]
    facts: tuple[float, int] | None = None
    rpc = getattr(runtime, "rpc", None)
    if rpc is not None:
        try:
            info = await rpc.symbol_info(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a missing symbol costs a label, not the close
            log.warning("reconcile_symbol_info_failed symbol=%s error=%s", name, exc)
            info = None
        if info is not None:
            try:
                facts = (float(info.point), int(info.digits))
            except (AttributeError, TypeError, ValueError):
                facts = None
    cache[name] = facts
    return facts


async def _fetch_deals(
    runtime: "Runtime", rows: Sequence[Trade], offset: float
) -> tuple[dict[int, list[Any]], bool, str | None]:
    """Pull the history window covering every missing row and group it by position.

    One call, not one per ticket: MT5's history call is expensive and the sidecar serialises
    every request onto a single thread, so a per-ticket loop over a restart with twelve open
    positions would be twelve round trips for data one window already contains.
    """
    rpc = getattr(runtime, "rpc", None)
    if rpc is None:
        return {}, False, "the MT5 RPC client is not wired into the runtime"

    last_seen, clamped = _last_seen_utc(rows)
    date_from = _broker_from_utc(last_seen, offset) - HISTORY_PAD_BEFORE_SECONDS
    # A day into the future, because `date_to` is broker-server time and our clock is not.
    date_to = time.time() + offset + HISTORY_PAD_AFTER_SECONDS
    if clamped:
        _emit(
            runtime,
            "reconcile_history_window_clamped",
            days=HISTORY_MAX_LOOKBACK_DAYS,
            message=(
                f"a row has been open longer than {HISTORY_MAX_LOOKBACK_DAYS} days; the deal "
                "history was searched from that bound and an older close may not be recovered"
            ),
        )
    log.info(
        "reconcile_history_window rows=%d from=%.0f to=%.0f span_days=%.1f offset=%+.0fs",
        len(rows),
        date_from,
        date_to,
        (date_to - date_from) / 86400.0,
        offset,
    )
    try:
        deals = _as_rows(await rpc.history_deals_get(date_from, date_to))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported; the rows still close as "unknown"
        return {}, False, _error_text(exc)
    return _group_deals(deals), True, None


async def _close_missing(
    runtime: "Runtime",
    session: AsyncSession,
    *,
    snapshot: LiveSnapshot | None = None,
    offset: float | None = None,
    closed_sink: list[ClosedRow] | None = None,
) -> int:
    """Close the rows whose tickets are gone from MT5. Shared body of :func:`close_missing`."""
    live = snapshot if snapshot is not None else await _snapshot(runtime)
    if not live.ok:
        _emit(
            runtime,
            "reconcile_snapshot_failed",
            message=live.error or "MT5 did not answer positions_get/orders_get",
        )
        log.error("reconcile_close_missing_skipped error=%s", live.error)
        return 0

    clock = offset if offset is not None else await _broker_offset(runtime)
    live_tickets = live.tickets
    rows = await _load_live_rows(session)
    missing = [row for row in rows if int(row.ticket) not in live_tickets]
    if not missing:
        return 0

    deals_by_position, history_ok, history_error = await _fetch_deals(runtime, missing, clock)
    if not history_ok:
        _emit(
            runtime,
            "reconcile_history_failed",
            message=(
                f"{len(missing)} ticket(s) are gone from MT5 but their deal history could not be "
                f"read ({history_error}); their realized P/L is unknown"
            ),
        )
        log.error("reconcile_history_failed error=%s", history_error)

    if closed_sink is not None and not history_ok:
        return 0
    currency = await _account_currency(runtime, session)
    facts_cache: dict[str, tuple[float, int] | None] = {}
    closed_rows: list[ClosedRow] = []

    for row in missing:
        ticket = int(row.ticket)
        deals = deals_by_position.get(ticket, [])
        was_pending = row.status == "pending"

        if was_pending and not deals:
            # Never filled: the order was removed, expired, or cancelled in the terminal.
            row.status = "cancelled"
            row.close_cause = "unknown"
            row.closed_at = utcnow()
            row.updated_at = utcnow()
            session.add(
                TradeEvent(
                    trade_id=row.id,
                    ticket=ticket,
                    run_id=row.run_id,
                    type="cancel",
                    detail={
                        "discovered_by": "reconcile",
                        "reason": "the pending order is gone from MT5 and left no deal behind",
                    },
                )
            )
            closed_rows.append(
                ClosedRow(
                    ticket=ticket,
                    trade_id=row.id,
                    symbol=row.symbol,
                    side=row.side,
                    kind=row.kind,
                    status="cancelled",
                    volume=float(row.volume),
                    entry_price=float(row.entry_price),
                    close_price=None,
                    profit=None,
                    profit_points=None,
                    close_cause="unknown",
                    closed_at=row.closed_at,
                    signal_ref=row.signal_ref,
                    external=bool(row.external),
                    digits=None,
                    deal_reason=None,
                    deal_count=0,
                )
            )
            continue

        if closed_sink is not None and not _closing_deals(deals):
            # A live poll may beat the broker history. Retry next cycle, keeping the row open.
            continue
        facts = await _symbol_facts(runtime, facts_cache, row.symbol)
        point = facts[0] if facts else None
        digits = facts[1] if facts else None

        if deals:
            profit = _realized(deals)
            close_price = _close_price(deals)
            if close_price is not None and digits is not None:
                close_price = round(close_price, digits)
            closed_at = _close_time(deals, clock)
            cause, deal_reason = _close_cause(deals)
        else:
            # The ticket is gone but no deal explains it: report it honestly rather than
            # inventing a price, and leave the row closed so the table stops lying.
            profit = None
            close_price = None
            closed_at = utcnow()
            cause, deal_reason = "unknown", None
            _emit(
                runtime,
                "reconcile_close_without_deals",
                ticket=ticket,
                symbol=row.symbol,
                message=(
                    f"ticket #{ticket} ({row.symbol}) is gone from MT5 but no deal was found for "
                    "it in the history window — it is recorded closed with an unknown result"
                ),
            )

        row.status = "closed"
        row.close_cause = cause
        row.close_price = close_price
        row.profit = profit
        row.profit_points = _profit_points(row.side, float(row.entry_price), close_price, point)
        row.closed_at = closed_at
        row.updated_at = utcnow()
        session.add(
            TradeEvent(
                trade_id=row.id,
                ticket=ticket,
                run_id=row.run_id,
                type="close",
                detail={
                    "discovered_by": "reconcile",
                    "close_cause": cause,
                    "deal_reason": deal_reason,
                    "deal_reason_name": (
                        DEAL_REASON_NAMES.get(deal_reason) if deal_reason is not None else None
                    ),
                    "deals": len(deals),
                    "close_price": close_price,
                    "profit": profit,
                    "currency": currency,
                    "was_pending": was_pending,
                },
            )
        )
        closed_rows.append(
            ClosedRow(
                ticket=ticket,
                trade_id=row.id,
                symbol=row.symbol,
                side=row.side,
                kind=row.kind,
                status="closed",
                volume=float(row.volume),
                entry_price=float(row.entry_price),
                close_price=close_price,
                profit=profit,
                profit_points=row.profit_points,
                close_cause=cause,
                closed_at=closed_at,
                signal_ref=row.signal_ref,
                external=bool(row.external),
                digits=digits,
                deal_reason=deal_reason,
                deal_count=len(deals),
            )
        )

    await _attach_permalinks(session, missing, closed_rows)
    await session.flush()

    seen_refs: set[str] = set()
    for row in missing:
        ref = (row.signal_ref or "").strip()
        if not ref or ref in seen_refs:
            continue
        seen_refs.add(ref)
        try:
            await ladder_stats.maybe_record_closed_group(session, signal_ref=ref)
        except Exception as exc:  # noqa: BLE001 - a stats row must not undo a reconcile close
            log.warning("ladder_outcome_reconcile_failed signal_ref=%s error=%s", ref, exc)

    for closed in closed_rows:
        _emit_event(
            runtime,
            events.ORDER_CANCELLED if closed.status == "cancelled" else events.POSITION_CLOSED,
            ticket=closed.ticket,
            trade_id=closed.trade_id,
            symbol=closed.symbol,
            side=closed.side,
            volume=closed.volume,
            entry_price=closed.entry_price,
            close_price=closed.close_price,
            profit=closed.profit,
            profit_points=closed.profit_points,
            close_cause=closed.close_cause,
            signal_ref=closed.signal_ref,
            external=closed.external,
            currency=currency,
            reconciled=True,
        )

    _emit(
        runtime,
        "reconcile_closed_while_down",
        tickets=[c.ticket for c in closed_rows],
        message=(
            f"{len(closed_rows)} ticket(s) were closed or cancelled while LV Copytrader was not "
            "watching; the database has been brought up to date from the deal history"
        ),
    )
    await _attach_close_state(session, closed_rows)
    if closed_sink is None:
        _notify_closed(runtime, closed_rows, currency)
    else:
        closed_sink.extend(closed_rows)

    for closed in closed_rows:
        log.info(
            "reconcile_closed ticket=%d symbol=%s status=%s cause=%s profit=%s points=%s deals=%d",
            closed.ticket,
            closed.symbol,
            closed.status,
            closed.close_cause,
            closed.profit,
            closed.profit_points,
            closed.deal_count,
        )
    return len(closed_rows)


async def _attach_close_state(session: AsyncSession, closed: Sequence[ClosedRow]) -> None:
    for item in closed:
        row = await session.get(Trade, item.trade_id) if item.trade_id is not None else None
        if row is None:
            continue
        item.ladder_role = row.ladder_role
        if item.close_cause == "tp":
            if row.ladder_role == "runner":
                item.tp_index = row.chosen_runner_tp_index
            elif row.ladder_role != "cover":
                item.tp_index = int(row.split_index or 0) + 1
        item.breakeven = (
            item.close_cause == "sl" and item.close_price is not None
            and pricing.stop_is_at_or_beyond_breakeven(item.side, item.entry_price, item.close_price)
        )
        if not item.signal_ref:
            continue
        rows = (await session.execute(select(Trade).where(
            Trade.signal_ref == item.signal_ref, Trade.symbol == item.symbol,
            Trade.side == item.side, Trade.status == "open", Trade.external.is_(False),
        ))).scalars().all()
        item.remaining = [dict(
            ticket=int(t.ticket), sl=t.sl,
            breakeven=pricing.stop_is_at_or_beyond_breakeven(t.side, float(t.entry_price), float(t.sl or 0)),
        ) for t in rows]


async def monitor_closes(runtime: "Runtime") -> int:
    """Observe broker closures, apply the existing BE policy, then send the actual outcome."""
    closed: list[ClosedRow] = []
    async with session_scope() as session:
        count = await _close_missing(runtime, session, closed_sink=closed)
    executor = getattr(runtime, "executor", None)
    quiet = {c.signal_ref for c in closed if c.signal_ref and not c.external}
    if executor is not None:
        try:
            # Successful BE moves appear in the TP card. Rejections still have their own card.
            await executor.trail_ladder_runners(quiet_signals=quiet)
            await executor.trail_after_tp1(quiet_signals=quiet)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("monitor_be_trail_failed error=%s", exc)
    if closed:
        async with session_scope() as session:
            await _attach_close_state(session, closed)
            currency = await _account_currency(runtime, session)
        _notify_closed(runtime, closed, currency)
    return count


async def close_missing(runtime: "Runtime", session: AsyncSession) -> int:
    """Close every open/pending row whose ticket no longer exists in MT5; return the count."""
    return await _close_missing(runtime, session)


async def _account_currency(runtime: "Runtime", session: AsyncSession) -> str | None:
    """Return the account currency for the notifications, from cached state or the account."""
    state = await session.get(SystemState, 1)
    if state is not None and state.account_currency:
        return str(state.account_currency)
    rpc = getattr(runtime, "rpc", None)
    if rpc is None:
        return None
    try:
        account = await rpc.account_info()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a missing currency costs a suffix, nothing more
        log.debug("reconcile_account_currency_failed error=%s", exc)
        return None
    if account is None:
        return None
    try:
        return str(account.currency)
    except AttributeError:
        return None


async def _attach_permalinks(
    session: AsyncSession, rows: Sequence[Trade], closed: Sequence[ClosedRow]
) -> None:
    """Fill each closed row's ``permalink`` so its notification links to the source post."""
    by_ticket = {int(row.ticket): row for row in rows}
    wanted = {
        int(row.message_row_id)
        for row in rows
        if row.message_row_id is not None and not row.external
    }
    if not wanted:
        return
    links: dict[int, str | None] = {}
    ids = sorted(wanted)
    for chunk in _chunks(ids, TICKET_CHUNK):
        stmt = select(Message.id, Message.permalink).where(Message.id.in_(list(chunk)))
        for message_id, permalink in (await session.execute(stmt)).all():
            links[int(message_id)] = permalink
    for item in closed:
        row = by_ticket.get(item.ticket)
        if row is None or row.message_row_id is None:
            continue
        item.permalink = links.get(int(row.message_row_id))


def _notify_closed(runtime: "Runtime", closed: Sequence[ClosedRow], currency: str | None) -> None:
    """Tell the operator about every close this reconcile discovered.

    External tickets are skipped: they belong to someone else, they are shown read-only, and
    an unsolicited notification about a trade LV Copytrader never touched is noise.
    """
    notifier = getattr(runtime, "notifier", None)
    if notifier is None:
        return
    for item in closed:
        if item.external:
            continue
        try:
            text = notifier.render_closed(
                symbol=item.symbol,
                side=item.side,
                volume=item.volume,
                entry_price=item.entry_price,
                close_price=item.close_price,
                profit=item.profit,
                profit_points=item.profit_points,
                currency=currency,
                close_cause=item.close_cause,
                status=item.status,
                tp_index=item.tp_index,
                ladder_role=item.ladder_role,
                breakeven=item.breakeven,
                remaining=item.remaining,
                ticket=item.ticket,
                signal_ref=item.signal_ref,
                digits=item.digits,
                reason=(
                    "cancelled while LV Copytrader was not watching"
                    if item.status == "cancelled"
                    else "closed while LV Copytrader was not watching"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a render failure never blocks the next one
            log.warning("reconcile_render_failed ticket=%d error=%s", item.ticket, exc)
            continue
        _notify(runtime, NOTIFY_CLOSED, text, link=item.permalink)


# --------------------------------------------------------------------------------------
# Pass 4 — the entry point
# --------------------------------------------------------------------------------------
async def _mark_reconciled() -> None:
    """Stamp ``SystemState.last_reconcile_at``, creating the singleton row if it is missing."""
    async with session_scope() as session:
        state = await session.get(SystemState, 1)
        if state is None:
            state = SystemState(id=1)
            session.add(state)
        state.last_reconcile_at = utcnow()
        state.updated_at = utcnow()


async def _run_catch_up(runtime: "Runtime") -> int:
    """Run the ingest catch-up if an ingestor is wired in, and return what it recorded.

    Catching up on Telegram is not this module's job — dedup, revisions, albums and the
    first-run baseline all live in :mod:`app.telegram.ingest`. Reconcile only triggers it,
    because a restart is exactly when the backlog needs draining.
    """
    ingestor = getattr(runtime, "ingestor", None)
    if ingestor is None:
        return 0
    try:
        return int(await ingestor.catch_up() or 0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed catch-up never fails the reconcile
        log.warning("reconcile_catchup_failed error=%s", exc)
        _emit(runtime, "reconcile_catchup_failed", message=_error_text(exc))
        return 0


async def reconcile(runtime: "Runtime", *, cause: str = "startup") -> dict[str, int]:
    """Bring the database back into agreement with MT5. Runs at startup and after a reconnect.

    Returns ``{"linked", "external", "closed", "orphans", "catchup"}``. Every failure inside is
    caught: the counts come back zeroed and the reason is logged and emitted, because a
    reconcile that refuses to finish must never be the thing that stops the app from starting.
    """
    counts: dict[str, int] = {
        "linked": 0,
        "external": 0,
        "closed": 0,
        "orphans": 0,
        "catchup": 0,
    }
    started = time.perf_counter()
    log.info("reconcile_started cause=%s", cause)

    try:
        # One snapshot for both passes: two reads a second apart would let a position close
        # between them and be linked by the first pass and closed by the second.
        snapshot = await _snapshot(runtime)
        offset = await _broker_offset(runtime)

        if snapshot.ok:
            async with session_scope() as session:
                link = await _link_tickets(runtime, session, snapshot=snapshot, offset=offset)
            counts["linked"] = link.linked
            counts["external"] = link.external
            counts["orphans"] = link.orphans

            closed: list[ClosedRow] = []
            async with session_scope() as session:
                counts["closed"] = await _close_missing(
                    runtime, session, snapshot=snapshot, offset=offset, closed_sink=closed
                )

            executor = getattr(runtime, "executor", None)
            if executor is not None:
                try:
                    quiet = {c.signal_ref for c in closed if c.signal_ref and not c.external}
                    await executor.trail_ladder_runners(quiet_signals=quiet)
                    await executor.trail_after_tp1(quiet_signals=quiet)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - BE trail must not fail reconcile
                    log.warning("reconcile_ladder_be_trail_failed error=%s", exc)
            if closed:
                async with session_scope() as session:
                    await _attach_close_state(session, closed)
                    currency = await _account_currency(runtime, session)
                _notify_closed(runtime, closed, currency)
        else:
            # Reported once here rather than twice by the two passes below it.
            _emit(
                runtime,
                "reconcile_snapshot_failed",
                cause=cause,
                message=(
                    "MT5 did not answer positions_get/orders_get, so no position was linked and "
                    f"nothing was closed: {snapshot.error}"
                ),
            )
            log.error("reconcile_snapshot_failed cause=%s error=%s", cause, snapshot.error)

        counts["catchup"] = await _run_catch_up(runtime)

        try:
            await _mark_reconciled()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the stamp is bookkeeping, not the work
            log.warning("reconcile_stamp_failed error=%s", exc)

    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - §8: a failed reconcile never blocks startup
        log.exception("reconcile_failed cause=%s error=%s", cause, exc)
        _emit(runtime, "reconcile_failed", cause=cause, message=_error_text(exc))
        return counts

    log.info(
        "reconcile_done cause=%s linked=%d external=%d closed=%d orphans=%d catchup=%d in %.0f ms",
        cause,
        counts["linked"],
        counts["external"],
        counts["closed"],
        counts["orphans"],
        counts["catchup"],
        (time.perf_counter() - started) * 1000.0,
    )
    return counts
