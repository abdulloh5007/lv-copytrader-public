"""The tool implementations that touch the account — the module where money moves.

Everything the model can *do* lives here. The read tools hand it numbers it must never
compute itself (BUILD_PROMPT §2); the write tools run one fixed gauntlet of checks before
anything reaches the broker (§5.3, §6); and every outcome comes back as a plain dict the
agent loop can put straight into a ``tool_result``.

Four rules run through the whole file.

**A rejection is a result, never an exception.** Every write tool answers
``{"ok": False, "message": "<something the model can act on>"}`` — "not enough free margin:
need 412.30 USD, have 180.05" rather than a traceback that kills the run. Exceptions do
escape the tool methods (a broken RPC link is not a trading decision), and
:meth:`Executor.dispatch` turns those into the same shape for the loop.

**The order of the gates is fixed and load-bearing** (§5.3). Posted stop validated when one
was given (a missing stop is an incomplete entry, not a refusal) → symbol resolved and
quoted → side/type valid → a posted stop on the right side and far enough away → volume →
an incomplete chain redirected to modify → entry band and stale-signal gate on a *new*
open → operator limits → duplicate signal → armed → margin headroom → ``order_check`` →
send. The entry gate compares the live fill with the channel's printed entry and the age
of the message that printed it; attaching a later ladder to legs that are already open
does not pass through it. The armed gate sits late on purpose: disarmed, the model still
gets the complete reasoning trail — the resolved symbol, the sized volume, the split
plan — and only the broker call is withheld. Broker writes are process-serial: one open,
modify, close or cancel at a time.

**Results are read as attributes, never as dicts** (§1.1). ``p.ticket``, ``si.digits``,
``result.retcode``. A missing field must raise ``AttributeError`` here rather than become a
silent ``None`` inside an order request. The one place ``.get()`` appears is on request dicts
this module built itself, which is not an RPC result at all.

**The executor owns the ``comment`` field.** There is no ``comment`` tool parameter (§5.3):
every order carries ``SP:<run_id[:8]>`` so §8 can re-link positions to their run after a
restart, and a model-written comment would destroy that tag.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final, Iterable, Sequence

from sqlalchemy import select, update

from app import events
from app.ai.tools import spec_by_name
from app.config import Settings
from app.db import get_setting, session_scope
from app.errors import RpcError, SignalPilotError, TradingError, retcode_message
from app.models import Message, Source, Trade, TradeEvent, utcnow
from app.operator_settings import (
    MAX_ENTRY_DRIFT_USD_DEFAULT,
    MODE_AI_MANAGED,
    MODE_BE_AFTER_TP1,
    be_after_tp1_enabled,
    max_entry_drift_usd,
    order_lot_size,
    orders_per_signal,
    use_fixed_order_lots,
)
from app.trading import guards, ladder_stats, pricing
from app.trading.entry_gate import (
    check_open,
    classic_market_entry_law,
    distance_from_printed,
    load_chain_anchor,
)
from app.trading.constants import (
    ORDER_TIME_GTC,
    ORDER_TIME_SPECIFIED,
    ORDER_TYPE_BUY,
    ORDER_TYPE_BUY_LIMIT,
    ORDER_TYPE_BUY_STOP,
    ORDER_TYPE_BUY_STOP_LIMIT,
    ORDER_TYPE_NAMES,
    ORDER_TYPE_SELL,
    ORDER_TYPE_SELL_LIMIT,
    PARTIAL_RETCODE,
    POSITION_TYPE_BUY,
    POSITION_TYPE_NAMES,
    SUCCESS_RETCODES,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_MODIFY,
    TRADE_ACTION_PENDING,
    TRADE_ACTION_REMOVE,
    TRADE_ACTION_SLTP,
    margin_mode_name,
    retcode_name,
    timeframe_value,
)
from app.trading.reconcile import make_comment
from app.trading.symbols import (
    Quote,
    SymbolRef,
    broker_day_start_ts,
    server_time_offset,
)
from app.util.logging import get_logger, redact

if TYPE_CHECKING:  # pragma: no cover - type-checker only; importing Runtime would cycle
    from app.runtime import Runtime

log = get_logger(__name__)


class _TaskReentrantLock:
    """An asyncio lock the holding task may acquire again.

    Opening a ladder retargets the cover with ``modify_position`` on the same task. A
    non-reentrant lock would deadlock that path. Every other task still waits, so two
    broker writes never run at once.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth: int = 0

    async def __aenter__(self) -> "_TaskReentrantLock":
        current = asyncio.current_task()
        if current is not None and current is self._owner:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = current
        self._depth = 1
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._depth -= 1
        if self._depth > 0:
            return
        self._owner = None
        self._depth = 0
        self._lock.release()

    def locked(self) -> bool:
        """Report whether any task currently holds the write lock."""
        return self._lock.locked()


#: One write lock per running event loop. A lock created at import binds to the first loop
#: that acquires it, and the test suite gives each test its own loop. Production runs one
#: loop, so there is still exactly one write lock for the process.
_BROKER_WRITE_LOCKS: dict[int, _TaskReentrantLock] = {}


def broker_write_lock() -> _TaskReentrantLock:
    """The broker-write mutex for the running loop, exposed so tests can see it is held."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    lock = _BROKER_WRITE_LOCKS.get(key)
    if lock is None:
        lock = _TaskReentrantLock()
        _BROKER_WRITE_LOCKS[key] = lock
    return lock


def _serialize_broker_write(method: Any) -> Any:
    """Run one write tool to completion before the next write tool starts."""

    @functools.wraps(method)
    async def _wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        async with broker_write_lock():
            return await method(self, *args, **kwargs)

    return _wrapped

__all__ = ["RunContext", "SendResult", "Executor"]


# --------------------------------------------------------------------------------------
# Module constants.
# --------------------------------------------------------------------------------------

#: The two retcodes that mean "the price moved underneath you": re-read the tick, rebuild
#: the price and try again, at most ``REQUOTE_RETRIES`` times (§1.3).
REQUOTE_RETCODES: Final[frozenset[int]] = frozenset({10004, 10020})

#: ``order_check`` reports success as retcode 0. Not 10009 — that catches people out (§1.3).
CHECK_OK_RETCODE: Final[int] = 0

#: How much of a source message is quoted back to the model on a position/order row.
EXCERPT_CHARS: Final[int] = 160

#: Order types that open a long. Everything else in ENUM_ORDER_TYPE opens a short.
BUY_ORDER_TYPES: Final[frozenset[int]] = frozenset(
    {ORDER_TYPE_BUY, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_BUY_STOP_LIMIT}
)

#: Deal entries that realise money. ``IN`` opens exposure and realises nothing.
REALIZING_DEAL_ENTRIES: Final[frozenset[int]] = frozenset({1, 2, 3})

#: Seconds of padding added to ``date_to`` on every history query. History times are on the
#: broker's clock (UTC+2/+3 at most brokers) and an unpadded window silently omits the deals
#: that were just made (§2).
HISTORY_FUTURE_PAD: Final[float] = 86400.0

#: Float slack for lot comparisons — volumes are decimal quantities carried in binary floats.
VOLUME_EPS: Final[float] = 1e-9

#: The `reason` the model gave for the write currently in flight. It reaches
#: :meth:`Executor.send_order` — which is handed only a request dict — without widening
#: ``RunContext``, whose fields are fixed by CONTRACTS §13. A ContextVar is the right tool:
#: each asyncio task carries its own copy, so two concurrent runs cannot read each other's.
_REASON: contextvars.ContextVar[str] = contextvars.ContextVar("sp_write_reason", default="")


# --------------------------------------------------------------------------------------
# Small formatting helpers. Every number the model reads is formatted once, here.
# --------------------------------------------------------------------------------------


def _trim(value: float, decimals: int = 8) -> str:
    """Render a float without trailing zeros, so 0.0100 prints as 0.01."""
    text = f"{float(value):.{decimals}f}".rstrip("0").rstrip(".")
    return text or "0"


def _lots(value: float) -> str:
    """Render a lot size the way a broker states it."""
    return _trim(float(value), 4)


def _price_text(value: float, digits: int) -> str:
    """Render a price at the symbol's own precision."""
    try:
        return f"{float(value):.{max(0, int(digits))}f}"
    except (TypeError, ValueError):
        return str(value)


def _money(value: float, currency: str) -> str:
    """Render an amount in account currency."""
    return f"{float(value):.2f} {currency}".strip()


def _join(items: Sequence[str], conjunction: str = "and") -> str:
    """Join a short list into readable English: 'a', 'a and b', 'a, b and c'."""
    values = [item for item in items if item]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])} {conjunction} {values[-1]}"


def _nearest_tp_trade(group: list[Trade]) -> Trade | None:
    """The leg whose take-profit sits closest to its entry — TP1 on a nearest-first split."""
    ranked: list[tuple[float, int, Trade]] = []
    for trade in group:
        try:
            tp = float(trade.tp or 0.0)
            entry = float(trade.entry_price or 0.0)
        except (TypeError, ValueError):
            continue
        if tp <= 0.0 or entry <= 0.0:
            continue
        ranked.append((abs(tp - entry), int(trade.split_index or 0), trade))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][2]


def _rows(result: Any) -> tuple[Any, ...]:
    """Normalise an MT5 collection result into a tuple of rows.

    ``positions_get``/``orders_get``/``history_deals_get`` answer with a tuple of namespaces,
    ``None`` when the terminal has nothing to say, and — for a single-ticket filter — very
    occasionally one bare namespace. All three become a tuple here.
    """
    if result is None:
        return ()
    if isinstance(result, (list, tuple)):
        return tuple(result)
    return (result,)


def _broker_iso(ts: Any) -> str | None:
    """Render a broker-clock unix timestamp as a naive ISO string, or ``None``.

    MT5 reports every tick, order and deal time on the trade server's clock. Formatting it
    as UTC therefore yields the broker's wall clock, which is what the model should read;
    the result carries no ``Z`` because it is not UTC.
    """
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    if value <= 0.0:
        return None
    try:
        return (
            datetime.fromtimestamp(value, tz=timezone.utc)
            .replace(tzinfo=None)
            .isoformat(timespec="seconds")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _utc_iso(value: datetime | None) -> str | None:
    """Render a stored (naive UTC) datetime as an ISO string."""
    if value is None:
        return None
    return value.isoformat(timespec="seconds")


def _excerpt(text: str | None) -> str:
    """Return the first EXCERPT_CHARS characters of a source message, whitespace collapsed."""
    if not text:
        return ""
    return " ".join(str(text).split())[:EXCERPT_CHARS]


def _as_float(value: Any) -> float | None:
    """Read a model-supplied number, returning ``None`` for anything that is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _ignored_risk_note(risk_percent: Any, because: str) -> str:
    """One sentence the model can read when a classic signal drops risk_percent."""
    parsed = _as_float(risk_percent)
    shown = repr(risk_percent) if parsed is None else f"{_trim(parsed)}%"
    return f"risk_percent {shown} was ignored: {because}"


def _as_int(value: Any) -> int | None:
    """Read a model-supplied ticket, returning ``None`` for anything that is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _side_of_order_type(order_type: int) -> str:
    """Return 'buy' or 'sell' for any ENUM_ORDER_TYPE value."""
    return "buy" if int(order_type) in BUY_ORDER_TYPES else "sell"


def _refuse(message: str, **extra: Any) -> dict[str, Any]:
    """Build the standard refusal envelope every write tool returns."""
    payload: dict[str, Any] = {"ok": False, "message": message}
    payload.update(extra)
    return payload


def _recorded_cause(ctx: RunContext, explicit: str | None) -> str:
    """Close-cause label stored on the trade. Explicit wins; origin is the fallback.

    ``channel_delete`` and ``full_close`` fit ``Trade.close_cause`` (16 chars).
    """
    if explicit:
        label = str(explicit).strip()
        if label:
            return label[:16]
    return {
        "kill_switch": "kill_switch",
        "manual": "manual",
        "channel_delete": "channel_delete",
        "full_close": "full_close",
    }.get(ctx.origin, "ai")


# --------------------------------------------------------------------------------------
# Public dataclasses.
# --------------------------------------------------------------------------------------


@dataclass
class RunContext:
    """Who is asking, on whose behalf, and whether the arm gate applies.

    ``bypass_arm`` is true in exactly two places: the kill switch's own closes — disarming
    first would make them refuse and the kill switch would do nothing (§5.5) — and the SL
    fallback, because a naked position is a worse state than an unwanted order (§6.4).
    """

    run_id: str
    signal_ref: str | None
    source_id: int | None
    message_row_id: int | None
    origin: str = "agent"
    bypass_arm: bool = False


@dataclass
class SendResult:
    """The outcome of one ``order_send``, already translated into English.

    ``ok`` is true for ``10008`` (a pending order was accepted), ``10009`` (a market order
    filled) and ``10010`` (part of it filled) — never for ``10009`` alone, which is the
    mistake that makes a system retry pending orders it already placed (§1.3).
    """

    ok: bool
    retcode: int | None
    retcode_name: str
    message: str
    ticket: int | None
    volume: float
    price: float
    slippage_points: float | None
    comment: str
    raw: dict[str, Any]

    @property
    def partial(self) -> bool:
        """Report whether the broker filled only part of the requested volume."""
        return self.retcode == PARTIAL_RETCODE


@dataclass
class _Leg:
    """One order in a possibly split plan: its own volume, its own target, its own ticket."""

    index: int
    volume: float
    tp: float
    request: dict[str, Any] = field(default_factory=dict)
    result: SendResult | None = None
    refusal: str | None = None
    #: ``"cover"`` / ``"runner"`` on a ladder; empty on a classic one-per-target split.
    role: str = ""

    @property
    def ok(self) -> bool:
        """Report whether this leg reached the broker and was accepted."""
        return self.result is not None and self.result.ok


@dataclass
class _Plan:
    """Everything computed before the broker is touched — the 'full reasoning trail' (§5.5).

    This object is what the disarmed path returns instead of an order: the resolved symbol,
    the validated stops, the sized volume, the split across targets, and the notes explaining
    every adjustment that was made along the way.
    """

    ref: SymbolRef
    quote: Quote
    side: str
    order_type: int
    pending: bool
    entry: float
    sl: float
    tps: list[float]
    unmanaged_tps: list[float]
    legs: list[_Leg]
    total_volume: float
    volume_source: str
    risk_percent: float | None
    margin_mode: str
    balance: float
    currency: str
    notes: list[str]
    expiration_ts: int | None = None
    pending_explanation: str = ""
    #: ``"ladder"`` when 4+ channel TPs were collapsed to cover+runner; else ``"standard"``.
    kind: str = "standard"
    #: Chosen runner-after-cover policy; only set on a ladder.
    runner_mode: str | None = None
    #: Server suggestion at decision time; only set on a ladder.
    suggested_runner_mode: str | None = None
    #: Formula bias (0/+1/+2) that produced suggested_runner_tp_index.
    runner_tp_bias: int | None = None
    suggested_runner_tp_index: int | None = None
    chosen_runner_tp_index: int | None = None

    def summary(self) -> dict[str, Any]:
        """Render the plan as the JSON the model reads, with every distance in three units."""
        ref = self.ref
        sl_units = pricing.distance_units(ref, abs(self.entry - self.sl)) if self.sl else None
        return {
            "symbol": ref.name,
            "side": self.side,
            "order_type": ORDER_TYPE_NAMES.get(self.order_type, str(self.order_type)),
            "pending": self.pending,
            "entry": self.entry,
            "sl": self.sl,
            "sl_distance": sl_units,
            "tp": list(self.tps),
            "unmanaged_tps": list(self.unmanaged_tps),
            "volume": self.total_volume,
            "volume_source": self.volume_source,
            "risk_percent": self.risk_percent,
            "margin_mode": self.margin_mode,
            "split_count": len(self.legs),
            "kind": self.kind,
            "runner_mode": self.runner_mode,
            "suggested_runner_mode": self.suggested_runner_mode,
            "runner_tp_bias": self.runner_tp_bias,
            "suggested_runner_tp_index": self.suggested_runner_tp_index,
            "chosen_runner_tp_index": self.chosen_runner_tp_index,
            "legs": [
                {"index": leg.index, "volume": leg.volume, "tp": leg.tp, "role": leg.role or None}
                for leg in self.legs
            ],
            "digits": ref.digits,
            "point": ref.point,
            "pip_size": ref.pip_size,
            "pip_convention": ref.pip_convention,
            "bid": self.quote.bid,
            "ask": self.quote.ask,
            "balance": self.balance,
            "currency": self.currency,
            "expires_at": _broker_iso(self.expiration_ts) if self.expiration_ts else None,
            "notes": list(self.notes),
        }


class Executor:
    """Every tool the model can call, and the only place an order is built or sent."""

    def __init__(self, runtime: "Runtime") -> None:
        """Bind to the shared service container; nothing is resolved until a tool runs."""
        self.runtime = runtime

    # ------------------------------------------------------------------ wiring
    # Read through the runtime on every access rather than caching in __init__: the
    # container is assembled field by field in the lifespan, and the executor is one of
    # the first things built.

    @property
    def settings(self) -> Settings:
        """The settings singleton this process was started with."""
        return self.runtime.settings

    @property
    def rpc(self) -> Any:
        """The MT5 sidecar client."""
        return self.runtime.rpc

    @property
    def resolver(self) -> Any:
        """The symbol resolver, with its cached symbol table."""
        return self.runtime.resolver

    @property
    def bus(self) -> Any:
        """The in-process event bus feeding the dashboard's WebSocket."""
        return self.runtime.bus

    @property
    def notifier(self) -> Any:
        """The operator's Telegram bot sender; may be ``None`` before the lifespan wires it."""
        return getattr(self.runtime, "notifier", None)

    # ------------------------------------------------------------------ account helpers

    async def _account_info(self) -> Any:
        """Return the account namespace, or explain that MT5 is not connected."""
        info = await self.rpc.account_info()
        if info is None:
            raise TradingError(
                "the terminal returned no account information — MetaTrader 5 is not "
                "connected or not logged in"
            )
        return info

    def _margin_mode(self, info: Any) -> tuple[str, str, bool]:
        """Return ``(effective, detected, forced)`` margin mode as literal strings.

        ``FORCE_MARGIN_MODE`` exists so both branches of the multi-TP logic can be exercised
        against one demo account (§1.3); when it is set the model is told the effective mode,
        because that is the one that decides whether its targets are split or merged.
        """
        detected = margin_mode_name(int(info.margin_mode))
        forced = self.settings.FORCE_MARGIN_MODE
        if forced:
            return str(forced).strip().lower(), detected, True
        return detected, detected, False

    async def _broker_now(self) -> float:
        """Return the current time on the broker's clock as a unix timestamp."""
        offset = await server_time_offset(self.rpc, self.resolver)
        return time.time() + offset

    async def _default_risk(self, session: Any) -> float:
        """Return the risk percent to use when the model supplies neither volume nor risk.

        The operator's ``trading.default_risk_percent`` wins when it is set, because that is
        the field the settings page writes; ``DEFAULT_RISK_PERCENT`` is the fallback.
        """
        try:
            stored = await get_setting(session, "trading.default_risk_percent", None)
        except Exception as exc:  # noqa: BLE001 - a settings read must not stop a trade
            log.warning("default_risk_read_failed error=%s", exc)
            stored = None
        risk = _as_float(stored)
        if risk is None or risk <= 0.0:
            risk = float(self.settings.DEFAULT_RISK_PERCENT)
        return risk

    async def _ladder_volume(self, session: Any) -> float:
        """Return the fixed lot size used for multi-TP ladders (never a risk percent).

        ``trading.ladder_volume`` is the operator knob on the settings page;
        ``LADDER_VOLUME`` (0.10) is the fallback so a missing row still opens 0.05+0.05.
        """
        try:
            stored = await get_setting(session, "trading.ladder_volume", None)
        except Exception as exc:  # noqa: BLE001 - a settings read must not stop a trade
            log.warning("ladder_volume_read_failed error=%s", exc)
            stored = None
        volume = _as_float(stored)
        if volume is None or volume <= 0.0:
            volume = float(self.settings.LADDER_VOLUME)
        return volume

    async def _ref_cache(self, names: Iterable[str]) -> dict[str, SymbolRef | None]:
        """Resolve each distinct symbol once, tolerating the ones that cannot be resolved.

        A symbol whose pip convention this broker does not fit must not take the whole
        positions table down with it, so the failure becomes a ``None`` entry and the row is
        rendered with raw prices and no unit conversion.
        """
        cache: dict[str, SymbolRef | None] = {}
        for raw in names:
            name = str(raw)
            if name in cache:
                continue
            try:
                cache[name] = await self.resolver.get(name)
            except SignalPilotError as exc:
                log.warning("symbol_ref_failed symbol=%s error=%s", name, exc)
                cache[name] = None
        return cache

    # ------------------------------------------------------------------ persistence helpers

    async def _trade_index(self, tickets: Sequence[int]) -> dict[int, dict[str, Any]]:
        """Join live tickets to their Trade rows and the source posts behind them.

        This is what turns "#500123" into "the XAUUSD buy from the 14:02 post", which is how
        a follow-up such as ``tp1 hit, move sl to be`` resolves to a real ticket (§5.2).
        """
        if not tickets:
            return {}
        index: dict[int, dict[str, Any]] = {}
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(Trade).where(Trade.ticket.in_([int(t) for t in tickets]))
                )
                trades = list(result.scalars().all())
                message_ids = {t.message_row_id for t in trades if t.message_row_id}
                messages: dict[int, Message] = {}
                if message_ids:
                    rows = await session.execute(
                        select(Message).where(Message.id.in_(list(message_ids)))
                    )
                    messages = {m.id: m for m in rows.scalars().all()}
                for trade in trades:
                    message = messages.get(trade.message_row_id or -1)
                    index[int(trade.ticket)] = {
                        "trade_id": trade.id,
                        "signal_ref": trade.signal_ref,
                        "run_id": trade.run_id,
                        "open_reason": trade.open_reason,
                        "requested_volume": trade.requested_volume,
                        "split_index": trade.split_index,
                        "split_count": trade.split_count,
                        "unmanaged_tps": list(trade.unmanaged_tps or []),
                        "ladder_role": trade.ladder_role,
                        "runner_mode": trade.runner_mode,
                        "suggested_runner_mode": trade.suggested_runner_mode,
                        "runner_tp_bias": trade.runner_tp_bias,
                        "suggested_runner_tp_index": trade.suggested_runner_tp_index,
                        "chosen_runner_tp_index": trade.chosen_runner_tp_index,
                        "source_excerpt": _excerpt(message.text) if message is not None else "",
                        "permalink": message.permalink if message is not None else None,
                        "message_id": message.message_id if message is not None else None,
                    }
        except Exception as exc:  # noqa: BLE001 - the live rows matter more than the join
            log.warning("trade_index_failed error=%s", exc)
        return index

    async def _permalink_for(self, ctx: RunContext) -> str | None:
        """Return the t.me link of the post this run is acting on, when there is one."""
        if not ctx.message_row_id:
            return None
        try:
            async with session_scope() as session:
                message = await session.get(Message, int(ctx.message_row_id))
                return message.permalink if message is not None else None
        except Exception as exc:  # noqa: BLE001 - a missing link never blocks a trade
            log.warning("permalink_lookup_failed message_row_id=%s error=%s", ctx.message_row_id, exc)
            return None

    async def _signal_age_seconds(self, ctx: RunContext) -> float | None:
        """Return how old the post behind this run is, on the wall clock.

        ``evaluate_entry`` takes the age as a parameter, but the tool schema deliberately does
        not expose it — the model would be guessing. The run already knows which message it is
        acting on, so the age is read from that row and the ``stale`` flag becomes a fact
        rather than a null (§5.4).
        """
        if not ctx.message_row_id:
            return None
        try:
            async with session_scope() as session:
                message = await session.get(Message, int(ctx.message_row_id))
                if message is None or message.posted_at is None:
                    return None
                posted = message.posted_at.replace(tzinfo=timezone.utc).timestamp()
                return max(0.0, time.time() - posted)
        except Exception as exc:  # noqa: BLE001 - an unknown age is not a reason to refuse
            log.warning("signal_age_lookup_failed message_row_id=%s error=%s", ctx.message_row_id, exc)
            return None

    async def _audit(
        self,
        ctx: RunContext,
        *,
        type: str,
        ticket: int | None,
        detail: dict[str, Any],
        retcode: int | None,
    ) -> None:
        """Write one TradeEvent row: the audit trail of every write that touched a ticket.

        Failures are logged and swallowed. A database that cannot record what just happened
        is a serious problem, but it is not a reason to raise into a code path that has
        already sent an order to the broker.
        """
        try:
            async with session_scope() as session:
                trade_id: int | None = None
                if ticket:
                    row = await session.execute(select(Trade).where(Trade.ticket == int(ticket)))
                    trade = row.scalar_one_or_none()
                    trade_id = trade.id if trade is not None else None
                session.add(
                    TradeEvent(
                        trade_id=trade_id,
                        ticket=int(ticket) if ticket else None,
                        run_id=ctx.run_id,
                        type=type,
                        detail=detail,
                        retcode=retcode,
                        retcode_name=retcode_name(retcode),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - never raise after the broker was touched
            log.error("trade_event_write_failed ticket=%s type=%s error=%s", ticket, type, exc)

    def _emit(self, event_type: str, **data: Any) -> None:
        """Push one event onto the bus; the dashboard is never worth an exception."""
        try:
            self.bus.emit(event_type, **data)
        except Exception as exc:  # noqa: BLE001
            log.warning("event_emit_failed type=%s error=%s", event_type, exc)

    def _notify(self, ntype: str, text: str, *, link: str | None = None) -> None:
        """Fire one notification in the background; a notification never delays a trade (§7)."""
        notifier = self.notifier
        if notifier is None:
            return
        try:
            notifier.fire(ntype, text, link=link)
        except Exception as exc:  # noqa: BLE001
            log.warning("notify_fire_failed type=%s error=%s", ntype, exc)

    def _render(self, builder: str, **kw: Any) -> str | None:
        """Render a notification through the notifier's locale-aware builder."""
        notifier = self.notifier
        if notifier is None:
            return None
        try:
            return str(getattr(notifier, builder)(**kw))
        except Exception as exc:  # noqa: BLE001 - a broken template must not stop a trade
            log.warning("notify_render_failed builder=%s error=%s", builder, exc)
            return None

    # ==================================================================================
    # READ TOOLS (BUILD_PROMPT §5.2)
    # ==================================================================================

    async def get_account(self) -> dict[str, Any]:
        """Report the account as the model must see it, including today's realized P/L.

        "Today" means since midnight on the **broker's** clock, not this machine's: MT5
        reports every deal time on the trade server's clock, and ``date_to`` is padded a day
        into the future or the deals just made fall outside the window (§2).
        """
        info = await self._account_info()
        effective, detected, forced = self._margin_mode(info)
        positions = _rows(await self.rpc.positions_get())
        magic = int(self.settings.MAGIC_NUMBER)
        ours = [p for p in positions if int(p.magic) == magic]
        currency = str(info.currency)

        realized, realized_ours, deal_count, day_start = await self._realized_today(magic)

        floating = sum(float(p.profit) for p in positions)
        margin_free = float(info.margin_free)
        return {
            "ok": True,
            "login": int(info.login),
            "name": str(info.name),
            "server": str(info.server),
            "balance": round(float(info.balance), 2),
            "equity": round(float(info.equity), 2),
            "margin": round(float(info.margin), 2),
            "margin_free": round(margin_free, 2),
            "margin_level": round(float(info.margin_level), 2),
            "currency": currency,
            "leverage": int(info.leverage),
            "margin_mode": effective,
            "margin_mode_detected": detected,
            "margin_mode_forced": forced,
            "open_positions": len(positions),
            "open_positions_ours": len(ours),
            "floating_profit": round(floating, 2),
            "realized_pl_today": realized,
            "realized_pl_today_ours": realized_ours,
            "realized_deals_today": deal_count,
            "broker_day_start": _broker_iso(day_start),
            "server_time": _broker_iso(await self._broker_now()),
            "trade_allowed_headroom": round(
                margin_free * float(self.settings.MARGIN_HEADROOM), 2
            ),
            "message": (
                f"balance {_money(float(info.balance), currency)}, equity "
                f"{_money(float(info.equity), currency)}, free margin "
                f"{_money(margin_free, currency)}; {len(positions)} open position(s), "
                f"{len(ours)} of them ours; realized today "
                f"{_money(realized, currency)} across {deal_count} closing deal(s); "
                f"margin mode {effective}"
                + (f" (forced in config; the broker reports {detected})" if forced else "")
            ),
        }

    async def _realized_today(self, magic: int) -> tuple[float, float, int, float]:
        """Sum today's closing deals on the broker's clock: (total, ours, count, day_start)."""
        day_start = await broker_day_start_ts(self.rpc, self.resolver)
        broker_now = await self._broker_now()
        deals = _rows(
            await self.rpc.history_deals_get(day_start, broker_now + HISTORY_FUTURE_PAD)
        )
        total = 0.0
        ours = 0.0
        count = 0
        for deal in deals:
            if int(deal.entry) not in REALIZING_DEAL_ENTRIES:
                continue
            if float(deal.time) < day_start:
                continue
            amount = (
                float(deal.profit) + float(deal.commission) + float(deal.swap) + float(deal.fee)
            )
            total += amount
            count += 1
            if int(deal.magic) == magic:
                ours += amount
        return round(total, 2), round(ours, 2), count, day_start

    async def resolve_symbol(self, query: str) -> dict[str, Any]:
        """Return up to MAX_SYMBOL_CANDIDATES ranked candidates — never one silent guess (§2)."""
        text = str(query or "").strip()
        candidates = await self.resolver.candidates(text)
        if not candidates:
            hint = await self.resolver.closest(text, 3)
            return {
                "ok": False,
                "message": f"no symbol matches '{text}'",
                "hint": hint,
                "query": text,
            }
        rows = [ref.to_dict() for ref in candidates]
        best = candidates[0]
        return {
            "ok": True,
            "query": text,
            "count": len(rows),
            "candidates": rows,
            "message": (
                f"{len(rows)} candidate(s) for '{text}', best first: "
                f"{_join([r['name'] for r in rows])}. {best.name} quotes at "
                f"{best.digits} digits with point {_trim(best.point)}, one pip = "
                f"{_trim(best.pip_size)} ({best.pip_convention}), volume "
                f"{_lots(best.volume_min)}–{_lots(best.volume_max)} in steps of "
                f"{_lots(best.volume_step)}, stops at least {best.stops_level_points} points "
                f"from price. Pick the exact name and use it in every later call."
            ),
        }

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        """Return the live two-sided price with the spread in points and pips."""
        quote = await self.resolver.quote(str(symbol))
        payload: dict[str, Any] = {"ok": True}
        payload.update(quote.to_dict())
        payload["message"] = (
            f"{quote.symbol} bid {quote.bid} / ask {quote.ask}, spread "
            f"{quote.spread_points} points ({round(quote.spread_pips, 2)} pips) at "
            f"{quote.server_time} broker time"
        )
        return payload

    async def price_from_distance(
        self,
        symbol: str,
        anchor: float,
        direction: str,
        distance: float,
        unit: str,
    ) -> dict[str, Any]:
        """Turn "20 pips below entry" into an absolute price — the server does the arithmetic."""
        ref = await self.resolver.get(str(symbol))
        anchor_value = _as_float(anchor)
        distance_value = _as_float(distance)
        if anchor_value is None:
            return _refuse(f"anchor must be a number, got {anchor!r}")
        if distance_value is None:
            return _refuse(f"distance must be a number, got {distance!r}")
        result = pricing.price_from_distance(
            ref, anchor_value, str(direction), distance_value, str(unit)
        )
        units = result["distance"]
        return {
            "ok": True,
            "symbol": ref.name,
            "price": result["price"],
            "anchor": result["anchor"],
            "direction": str(direction).strip().lower(),
            "unit": str(unit).strip().lower(),
            "distance": units,
            "convention": result["convention"],
            "digits": ref.digits,
            "point": ref.point,
            "pip_size": ref.pip_size,
            "message": (
                f"{_trim(distance_value)} {str(unit).strip().lower()} "
                f"{str(direction).strip().lower()} {_price_text(result['anchor'], ref.digits)} on "
                f"{ref.name} is {_price_text(result['price'], ref.digits)} — a move of "
                f"{_trim(units['points'])} points / {_trim(units['pips'])} pips "
                f"(pip convention: {result['convention']})"
            ),
        }

    async def evaluate_entry(
        self,
        symbol: str,
        side: str,
        signal_entry: float,
        sl: float,
        tp: Any = None,
        signal_age_seconds: float | None = None,
        source_id: int | None = None,
        message_row_id: int | None = None,
        signal_ref: str | None = None,
    ) -> dict[str, Any]:
        """The late-entry instrument, and the classic-entry law.

        Signals arrive minutes late and price has usually moved. This reports the facts —
        the distance travelled, the fraction of the stop it consumed, the reward-to-risk as
        posted against now, the age — and, for a classic signal, a hard entry action.
        Farther than ``trading.max_entry_drift_usd`` from the channel entry or zone is
        the only "price already moved" skip. At or inside that distance the result says
        ``must_open``. When fixed lots are on, lot size and reward-to-risk are not a
        reason to defer.
        """
        direction = str(side or "").strip().lower()
        if direction not in ("buy", "sell"):
            return _refuse(f"side must be 'buy' or 'sell', got {side!r}")
        entry_posted = _as_float(signal_entry)
        stop = _as_float(sl)
        if entry_posted is None or entry_posted <= 0.0:
            return _refuse(f"signal_entry must be a positive price, got {signal_entry!r}")
        if stop is None or stop <= 0.0:
            return _refuse(f"sl must be a positive price, got {sl!r}")

        ref = await self.resolver.get(str(symbol))
        quote = await self.resolver.quote(ref.name)
        targets: list[float] = []
        for item in tp if isinstance(tp, (list, tuple)) else ([] if tp is None else [tp]):
            value = _as_float(item)
            if value is not None and value > 0.0:
                targets.append(value)
        first_tp = targets[0] if targets else None
        ladder = pricing.is_tp_ladder(targets)

        entry_now = quote.ask if direction == "buy" else quote.bid
        # Signed so that positive always means "the move went against this entry".
        adverse = (entry_now - entry_posted) if direction == "buy" else (entry_posted - entry_now)
        stop_distance = abs(entry_posted - stop)
        fraction = (adverse / stop_distance) if stop_distance > 0.0 else None

        rr_posted = None
        rr_now = None
        if first_tp is not None and stop_distance > 0.0:
            rr_posted = round(abs(first_tp - entry_posted) / stop_distance, 3)
        risk_now = (entry_now - stop) if direction == "buy" else (stop - entry_now)
        if first_tp is not None and risk_now > 0.0:
            reward_now = (first_tp - entry_now) if direction == "buy" else (entry_now - first_tp)
            rr_now = round(reward_now / risk_now, 3)

        info = await self._account_info()
        currency = str(info.currency)
        balance = float(info.balance)
        async with session_scope() as session:
            risk_percent = await self._default_risk(session)
            ladder_volume = await self._ladder_volume(session)
            classic_orders = await orders_per_signal(session)
            classic_lot = await order_lot_size(session)
            classic_be = await be_after_tp1_enabled(session)
            fixed_lots = await use_fixed_order_lots(session)
            max_drift = await max_entry_drift_usd(session)
            anchor = None
            if message_row_id or signal_ref:
                anchor = await load_chain_anchor(
                    session, message_row_id=message_row_id, signal_ref=signal_ref
                )
            ladder_stats_payload = (
                (await ladder_stats.stats_for_source(session, source_id)).to_dict()
                if ladder
                else None
            )
        sized_posted = pricing.compute_volume(
            ref, balance, risk_percent, entry_posted, stop, currency
        )
        sized_now = pricing.compute_volume(ref, balance, risk_percent, entry_now, stop, currency)

        age = _as_float(signal_age_seconds)
        stale_after = int(self.settings.STALE_SIGNAL_SECONDS)
        stale = bool(age is not None and age > stale_after)

        measured_from = "signal_entry"
        measure_entry: float | None = entry_posted
        measure_zone: tuple[float, float] | None = None
        if anchor is not None and anchor.defined_entry and (
            anchor.entry is not None or anchor.zone is not None
        ):
            measured_from = "channel"
            measure_entry = anchor.entry
            measure_zone = anchor.zone
        abs_distance = distance_from_printed(entry_now, measure_entry, measure_zone)
        entry_law = classic_market_entry_law(
            distance=abs_distance,
            max_drift=max_drift,
            fixed_lots=fixed_lots,
            ladder=ladder,
            stale=stale,
            stop_invalid=risk_now <= 0.0,
        )

        moved = pricing.distance_units(ref, adverse)
        digits = ref.digits
        parts: list[str] = [
            f"{ref.name} is {_price_text(quote.bid, digits)}/{_price_text(quote.ask, digits)} now; "
            f"a {direction} would enter at {_price_text(entry_now, digits)} against the posted "
            f"{_price_text(entry_posted, digits)}"
        ]
        if adverse > 0:
            parts.append(
                f"price has run {_trim(moved['points'])} points ({_trim(moved['pips'])} pips) "
                f"AGAINST this entry"
            )
        elif adverse < 0:
            parts.append(
                f"price has moved {_trim(abs(moved['points']))} points "
                f"({_trim(abs(moved['pips']))} pips) IN FAVOUR of this entry"
            )
        else:
            parts.append("price is exactly at the posted entry")
        if fraction is not None:
            parts.append(
                f"that is {fraction * 100.0:.1f}% of the {_trim(pricing.points(ref, stop_distance))}"
                f"-point stop"
            )
        if risk_now <= 0.0:
            parts.append(
                f"the posted stop {_price_text(stop, digits)} is already on the wrong side of the "
                f"market for a {direction} — entering now would be stopped out immediately"
            )
        elif ladder:
            parts.append(
                "channel TP1 is not the cover target; cover take-profit is money R:R ≈ 1:1 "
                "from the fill, runner is a later ladder rung chosen by "
                "min(ceil((N+1)/2)+bias, N-1)"
            )
            if ladder_stats_payload and ladder_stats_payload.get("blurb"):
                parts.append(str(ladder_stats_payload["blurb"]))
                parts.append(
                    "pass runner_mode and optional runner_tp_index (±1 from suggested) on "
                    "open_market; do not recount channel history for these counts"
                )
        elif rr_posted is not None and rr_now is not None:
            parts.append(
                f"reward:risk was {rr_posted}:1 as posted and is {rr_now}:1 now"
                + (
                    " — that ratio is a fact, not a reason to defer"
                    if fixed_lots and not ladder
                    else ""
                )
            )
        elif first_tp is None:
            parts.append("no target was given, so reward:risk cannot be measured")
        if age is not None:
            parts.append(
                f"the signal is {int(age)}s old"
                + (f", past the {stale_after}s stale threshold" if stale else "")
            )
        # Risk-percent sizing is for single-target / classic 1–3 TP signals only.
        # Naming those lots on a ladder is what made the model open 0.01 at TP1.
        if not ladder:
            classic_count = min(classic_orders, len(targets)) if targets else 1
            parts.append(
                f"omit volume and risk_percent to open {classic_count} order(s) of "
                f"{_lots(classic_lot)} lots each "
                f"(trading.orders_per_signal={classic_orders}, "
                f"trading.order_lot_size={_lots(classic_lot)}), nearest targets first"
            )
            if len(targets) >= 2 and classic_be:
                parts.append(
                    "after TP1 fills the server moves the remaining stops to break-even "
                    "unless runner_mode is hold_channel_sl or a stop was already changed"
                )
            elif len(targets) >= 2:
                parts.append("break-even after TP1 is off for this account")
            if entry_law.skip_reason == "price_moved":
                parts.append(
                    f"SKIP this market entry: the live fill is {_trim(entry_law.price_distance)} "
                    f"from the channel entry, more than trading.max_entry_drift_usd "
                    f"({_trim(entry_law.max_drift)}). Price already moved is a skip only past "
                    "that distance. Do not open and do not place a pending order to chase it"
                )
            elif entry_law.must_open:
                parts.append(
                    f"MUST OPEN at market: the live fill is {_trim(entry_law.price_distance)} "
                    f"from the channel entry, within trading.max_entry_drift_usd "
                    f"({_trim(entry_law.max_drift)}). Do not defer or skip because the lots "
                    "look large for the account, because the stop is wide, or because "
                    "reward:risk to TP1 looks poor"
                )
            elif entry_law.skip_reason == "stop_invalid":
                parts.append(
                    "do not open: the posted stop is already through the market. That is not "
                    "a price-moved skip"
                )
            if fixed_lots:
                parts.append(
                    "trading.use_fixed_order_lots is on: operator sizing is the law. "
                    f"Open {classic_orders} order(s) of {_lots(classic_lot)} lots "
                    "(orders_per_signal × order_lot_size). Omit volume and risk_percent. "
                    "Do not pass a smaller lot and do not finish deferred because the size "
                    "feels large"
                )
            else:
                parts.append(
                    f"at {_trim(risk_percent)}% risk the size would be "
                    + (
                        f"{_lots(sized_now.volume)} lots now"
                        if sized_now.ok
                        else f"unsizable now ({sized_now.message})"
                    )
                    + (
                        f" against {_lots(sized_posted.volume)} lots at the posted entry"
                        if sized_posted.ok
                        else f" against unsizable at the posted entry ({sized_posted.message})"
                    )
                    + " only if you pass risk_percent. Fixed lots are off, so that percent "
                    "may size the trade, but a fill inside the drift is still an open"
                )

        ladder_plan: dict[str, Any] | None = None
        if ladder:
            bias = int((ladder_stats_payload or {}).get("suggested_runner_tp_bias") or 0)
            planned = pricing.plan_ladder_targets(
                ref, direction, entry_now, stop, targets, bias=bias
            )
            half = round(ladder_volume / 2.0, 8)
            ladder_plan = {
                "total_volume": ladder_volume,
                "cover": {
                    "role": "cover",
                    "volume": half,
                    "tp": planned.cover_tp,
                    "sl": stop,
                },
                "runner": {
                    "role": "runner",
                    "volume": half,
                    "tp": planned.runner_tp,
                    "sl": stop,
                    "tp_index": planned.chosen_index,
                },
                "unmanaged_tps": list(planned.unmanaged),
                "n": planned.n,
                "bias": planned.bias,
                "suggested_runner_tp_index": planned.suggested_index,
            }
            parts.append(
                f"this is a {len(targets)}-target ladder: do NOT pass risk_percent. The server "
                f"opens a fixed {_lots(ladder_volume)} lots as two legs of {_lots(half)}: cover "
                f"1R at {_price_text(planned.cover_tp, digits)}, runner at "
                f"{_price_text(planned.runner_tp, digits)}. Intermediate channel TPs are not "
                "broker targets"
            )
            if planned.suggested_index and planned.n:
                if ladder_stats_payload:
                    blurb_stats = ladder_stats.ChannelLadderStats(
                        source_id=ladder_stats_payload.get("source_id"),
                        source_title=str(ladder_stats_payload.get("source_title") or ""),
                        window=int(ladder_stats_payload.get("window") or 5),
                        sample_size=int(ladder_stats_payload.get("sample_size") or 0),
                        raw_sl_hits=int(ladder_stats_payload.get("raw_sl_hits") or 0),
                        be_stopouts=int(ladder_stats_payload.get("be_stopouts") or 0),
                        tp4_plus=int(ladder_stats_payload.get("tp4_plus") or 0),
                        clean_streak=int(ladder_stats_payload.get("clean_streak") or 0),
                        suggested_mode=str(
                            ladder_stats_payload.get("suggested_mode") or "be_after_cover"
                        ),
                        bootstrap=bool(ladder_stats_payload.get("bootstrap")),
                        suggested_runner_tp_bias=int(
                            ladder_stats_payload.get("suggested_runner_tp_bias") or 0
                        ),
                    )
                else:
                    blurb_stats = ladder_stats.empty_stats()
                parts.append(
                    ladder_stats.format_runner_tp_blurb(
                        index=int(planned.suggested_index),
                        n=int(planned.n),
                        price=float(planned.runner_tp),
                        stats=blurb_stats,
                    )
                )

        return {
            "ok": True,
            "symbol": ref.name,
            "side": direction,
            "bid": quote.bid,
            "ask": quote.ask,
            "server_time": quote.server_time,
            "signal_entry": entry_posted,
            "entry_now": entry_now,
            "sl": stop,
            "tp": targets,
            "distance_from_signal_entry": moved,
            "distance_as_fraction_of_sl": None if fraction is None else round(fraction, 4),
            "sl_distance_from_signal_entry": pricing.distance_units(ref, stop_distance),
            "sl_distance_from_now": pricing.distance_units(ref, risk_now),
            "rr_as_posted": rr_posted,
            "rr_now": rr_now,
            "signal_age_seconds": None if age is None else int(age),
            "stale": stale,
            "stale_after_seconds": stale_after,
            "risk_percent": None if ladder else risk_percent,
            "ladder": ladder,
            "ladder_volume": ladder_volume if ladder else None,
            "ladder_plan": ladder_plan,
            "suggested_runner_mode": (
                None if not ladder_stats_payload else ladder_stats_payload.get("suggested_mode")
            ),
            "suggested_runner_tp_index": (
                None if not ladder_plan else ladder_plan.get("suggested_runner_tp_index")
            ),
            "suggested_runner_tp_bias": (
                None
                if not ladder_stats_payload
                else ladder_stats_payload.get("suggested_runner_tp_bias")
            ),
            "ladder_stats": ladder_stats_payload,
            "volume_at_signal_entry": (
                None if ladder else (sized_posted.volume if sized_posted.ok else None)
            ),
            "volume_at_current_price": (
                None if ladder else (sized_now.volume if sized_now.ok else None)
            ),
            "volume_at_signal_entry_message": (
                "ladder: not sized from risk percent" if ladder else sized_posted.message
            ),
            "volume_at_current_price_message": (
                "ladder: not sized from risk percent" if ladder else sized_now.message
            ),
            "balance": round(balance, 2),
            "currency": currency,
            "digits": digits,
            "point": ref.point,
            "pip_size": ref.pip_size,
            "stops_level_points": ref.stops_level_points,
            "spread_points": quote.spread_points,
            "price_distance_abs": (
                None if entry_law.price_distance is None else round(float(entry_law.price_distance), 5)
            ),
            "max_entry_drift_usd": entry_law.max_drift,
            "within_entry_drift": entry_law.within_drift,
            "entry_action": entry_law.action,
            "must_open": entry_law.must_open,
            "skip_reason": entry_law.skip_reason,
            "defer_for_risk_forbidden": entry_law.defer_for_risk_forbidden,
            "use_fixed_order_lots": fixed_lots,
            "orders_per_signal": classic_orders,
            "order_lot_size": classic_lot,
            "measured_from": measured_from,
            "assessment": "; ".join(parts) + ".",
        }

    def _distance_row(self, ref: SymbolRef | None, level: float, price: float) -> dict | None:
        """Return the distance from ``price`` to ``level`` in price, points and pips."""
        if ref is None or not level:
            return None
        return pricing.distance_units(ref, abs(float(level) - float(price)))

    async def get_positions(self) -> dict[str, Any]:
        """Every open position, live from MT5, joined to the signal that created it."""
        rows = _rows(await self.rpc.positions_get())
        magic = int(self.settings.MAGIC_NUMBER)
        refs = await self._ref_cache(str(p.symbol) for p in rows)
        index = await self._trade_index([int(p.ticket) for p in rows])

        positions: list[dict[str, Any]] = []
        for p in rows:
            ref = refs.get(str(p.symbol))
            side = POSITION_TYPE_NAMES.get(int(p.type), str(p.type))
            entry = float(p.price_open)
            current = float(p.price_current)
            moved = (current - entry) if int(p.type) == POSITION_TYPE_BUY else (entry - current)
            link = index.get(int(p.ticket), {})
            positions.append(
                {
                    "ticket": int(p.ticket),
                    "symbol": str(p.symbol),
                    "side": side,
                    "type": int(p.type),
                    "volume": float(p.volume),
                    "entry_price": entry,
                    "current_price": current,
                    "sl": float(p.sl),
                    "tp": float(p.tp),
                    "profit": round(float(p.profit), 2),
                    "swap": round(float(p.swap), 2),
                    "profit_points": (
                        None if ref is None else round(pricing.points(ref, moved), 1)
                    ),
                    "profit_distance": None if ref is None else pricing.distance_units(ref, moved),
                    "distance_to_sl": self._distance_row(ref, float(p.sl), current),
                    "distance_to_tp": self._distance_row(ref, float(p.tp), current),
                    "stops_level_points": None if ref is None else ref.stops_level_points,
                    "digits": None if ref is None else ref.digits,
                    "magic": int(p.magic),
                    "comment": str(p.comment),
                    "external": int(p.magic) != magic,
                    "opened_at": _broker_iso(p.time),
                    "signal_ref": link.get("signal_ref"),
                    "run_id": link.get("run_id"),
                    "open_reason": link.get("open_reason"),
                    "split_index": link.get("split_index"),
                    "split_count": link.get("split_count"),
                    "unmanaged_tps": link.get("unmanaged_tps", []),
                    "ladder_role": link.get("ladder_role"),
                    "runner_mode": link.get("runner_mode"),
                    "suggested_runner_mode": link.get("suggested_runner_mode"),
                    "runner_tp_bias": link.get("runner_tp_bias"),
                    "suggested_runner_tp_index": link.get("suggested_runner_tp_index"),
                    "chosen_runner_tp_index": link.get("chosen_runner_tp_index"),
                    "source_excerpt": link.get("source_excerpt", ""),
                    "permalink": link.get("permalink"),
                    "stopless": not float(p.sl),
                }
            )

        external = sum(1 for row in positions if row["external"])
        stopless = [row["ticket"] for row in positions if row["stopless"] and not row["external"]]
        message = f"{len(positions)} open position(s)"
        if external:
            message += f", {external} of them external (another system or the operator's hand — read-only)"
        if stopless:
            message += f"; WITHOUT A STOP: {_join([f'#{t}' for t in stopless])}"
        return {
            "ok": True,
            "count": len(positions),
            "positions": positions,
            "message": message,
        }

    async def get_orders(self) -> dict[str, Any]:
        """Every pending order, live from MT5, joined to its originating signal."""
        rows = _rows(await self.rpc.orders_get())
        magic = int(self.settings.MAGIC_NUMBER)
        refs = await self._ref_cache(str(o.symbol) for o in rows)
        index = await self._trade_index([int(o.ticket) for o in rows])

        orders: list[dict[str, Any]] = []
        for o in rows:
            ref = refs.get(str(o.symbol))
            order_type = int(o.type)
            price = float(o.price_open)
            current = float(o.price_current)
            link = index.get(int(o.ticket), {})
            expiration = float(o.time_expiration)
            orders.append(
                {
                    "ticket": int(o.ticket),
                    "symbol": str(o.symbol),
                    "side": _side_of_order_type(order_type),
                    "type": order_type,
                    "order_type": ORDER_TYPE_NAMES.get(order_type, str(order_type)),
                    "volume": float(o.volume_current),
                    "volume_initial": float(o.volume_initial),
                    "price": price,
                    "current_price": current,
                    "sl": float(o.sl),
                    "tp": float(o.tp),
                    # A pending order holds no exposure, so there is nothing to make or lose
                    # on it yet; the field is present so every row has the same shape.
                    "profit": None,
                    "distance_to_price": self._distance_row(ref, price, current),
                    "distance_to_sl": self._distance_row(ref, float(o.sl), price),
                    "distance_to_tp": self._distance_row(ref, float(o.tp), price),
                    "stops_level_points": None if ref is None else ref.stops_level_points,
                    "digits": None if ref is None else ref.digits,
                    "magic": int(o.magic),
                    "comment": str(o.comment),
                    "external": int(o.magic) != magic,
                    "placed_at": _broker_iso(o.time_setup),
                    "expires_at": _broker_iso(expiration) if expiration else None,
                    "signal_ref": link.get("signal_ref"),
                    "run_id": link.get("run_id"),
                    "open_reason": link.get("open_reason"),
                    "unmanaged_tps": link.get("unmanaged_tps", []),
                    "ladder_role": link.get("ladder_role"),
                    "runner_mode": link.get("runner_mode"),
                    "suggested_runner_mode": link.get("suggested_runner_mode"),
                    "runner_tp_bias": link.get("runner_tp_bias"),
                    "suggested_runner_tp_index": link.get("suggested_runner_tp_index"),
                    "chosen_runner_tp_index": link.get("chosen_runner_tp_index"),
                    "source_excerpt": link.get("source_excerpt", ""),
                    "permalink": link.get("permalink"),
                }
            )

        external = sum(1 for row in orders if row["external"])
        message = f"{len(orders)} pending order(s)"
        if external:
            message += f", {external} of them external (read-only)"
        return {"ok": True, "count": len(orders), "orders": orders, "message": message}

    async def get_candles(self, symbol: str, timeframe: str, count: int) -> dict[str, Any]:
        """Return recent OHLC bars, oldest first, capped at MAX_CANDLES.

        A screenshot cannot say where price is *now*, how old the picture is, or whether the
        level drawn on it has already been taken out. This is how an image is grounded
        against reality before anything is traded off it (§5.2).
        """
        ref = await self.resolver.get(str(symbol))
        tf_name = str(timeframe).strip().upper()
        tf_value = timeframe_value(tf_name)
        cap = int(self.settings.MAX_CANDLES)
        requested = _as_int(count) or 0
        wanted = max(1, min(requested if requested > 0 else cap, cap))

        raw = await self.rpc.copy_rates_from_pos(ref.name, tf_value, 0, wanted)
        if raw is None:
            return _refuse(
                f"no {tf_name} candles for {ref.name} — the symbol may be unselected or the "
                f"terminal disconnected",
                symbol=ref.name,
                timeframe=tf_name,
            )

        candles: list[dict[str, Any]] = []
        for row in raw:
            # copy_rates_* is the one RPC payload that is plain data, not objects: the
            # sidecar sends the structured array through .astype(object).tolist(), so each
            # row arrives as [time, open, high, low, close, tick_volume, spread, real_volume].
            if isinstance(row, dict):
                values = [
                    row.get("time"),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    row.get("tick_volume"),
                    row.get("spread"),
                    row.get("real_volume"),
                ]
            elif isinstance(row, (list, tuple)):
                values = list(row) + [None] * (8 - len(row))
            else:
                continue
            candles.append(
                {
                    "time": _broker_iso(values[0]),
                    "time_ts": _as_float(values[0]),
                    "open": _as_float(values[1]),
                    "high": _as_float(values[2]),
                    "low": _as_float(values[3]),
                    "close": _as_float(values[4]),
                    "tick_volume": _as_float(values[5]),
                    "spread": _as_float(values[6]),
                    "real_volume": _as_float(values[7]),
                }
            )

        truncated = requested > cap
        last = candles[-1] if candles else None
        message = (
            f"{len(candles)} {tf_name} bar(s) for {ref.name}, oldest first"
            + (f"; the request for {requested} was capped at {cap}" if truncated else "")
            + (
                f"; the last bar closed at {_price_text(last['close'] or 0.0, ref.digits)} at "
                f"{last['time']} broker time"
                if last
                else "; the terminal returned no bars"
            )
        )
        return {
            "ok": True,
            "symbol": ref.name,
            "timeframe": tf_name,
            "count": len(candles),
            "requested": requested,
            "capped_at": cap if truncated else None,
            "digits": ref.digits,
            "candles": candles,
            "message": message,
        }

    async def get_recent_signals(self, limit: int) -> dict[str, Any]:
        """Return the last N posts from the active source with the trades each produced."""
        cap = int(self.settings.RECENT_SIGNALS_TOOL_MAX)
        requested = _as_int(limit) or cap
        wanted = max(1, min(requested, cap))

        async with session_scope() as session:
            source = (
                await session.execute(select(Source).where(Source.is_active.is_(True)))
            ).scalars().first()
            if source is None:
                return {
                    "ok": True,
                    "count": 0,
                    "signals": [],
                    "message": "no signal source is selected, so there is no history to read",
                }
            rows = (
                await session.execute(
                    select(Message)
                    .where(Message.source_id == source.id)
                    .order_by(Message.message_id.desc(), Message.revision.desc())
                    .limit(wanted)
                )
            ).scalars().all()
            message_ids = [m.id for m in rows]
            trades: list[Trade] = []
            if message_ids:
                trades = list(
                    (
                        await session.execute(
                            select(Trade).where(Trade.message_row_id.in_(message_ids))
                        )
                    ).scalars().all()
                )

            by_message: dict[int, list[Trade]] = {}
            for trade in trades:
                by_message.setdefault(int(trade.message_row_id or 0), []).append(trade)

            now = time.time()
            signals: list[dict[str, Any]] = []
            for m in rows:
                posted = m.posted_at.replace(tzinfo=timezone.utc).timestamp() if m.posted_at else None
                age = int(now - posted) if posted else None
                signals.append(
                    {
                        "message_id": int(m.message_id),
                        "signal_ref": m.signal_ref,
                        "revision": int(m.revision),
                        "kind": m.kind,
                        "status": m.status,
                        "skip_reason": m.skip_reason,
                        "catchup": bool(m.catchup),
                        "text": _excerpt(m.text),
                        "image_count": int(m.image_count),
                        "reply_to_message_id": m.reply_to_message_id,
                        "permalink": m.permalink,
                        "posted_at": _utc_iso(m.posted_at),
                        "age_seconds": age,
                        "trades": [
                            {
                                "ticket": int(t.ticket),
                                "kind": t.kind,
                                "symbol": t.symbol,
                                "side": t.side,
                                "volume": t.volume,
                                "entry_price": t.entry_price,
                                "sl": t.sl,
                                "tp": t.tp,
                                "status": t.status,
                                "close_cause": t.close_cause,
                                "profit": t.profit,
                                "profit_points": t.profit_points,
                                "unmanaged_tps": list(t.unmanaged_tps or []),
                                "opened_at": _utc_iso(t.opened_at),
                                "closed_at": _utc_iso(t.closed_at),
                            }
                            for t in sorted(
                                by_message.get(int(m.id), []), key=lambda row: row.ticket
                            )
                        ],
                    }
                )

        traded = sum(1 for s in signals if s["trades"])
        return {
            "ok": True,
            "source": source.title,
            "count": len(signals),
            "signals": signals,
            "message": (
                f"the last {len(signals)} message(s) from {source.title}, newest first; "
                f"{traded} of them produced trades"
            ),
        }

    async def get_channel_ladder_stats(self, *, source_id: int | None = None) -> dict[str, Any]:
        """Return server-computed last-N ladder stats for this source. Never recounts history."""
        async with session_scope() as session:
            stats = await ladder_stats.stats_for_source(session, source_id)
        payload = stats.to_dict()
        payload["ok"] = True
        payload["message"] = stats.blurb()
        return payload

    async def _maybe_record_ladder_outcome(
        self, signal_ref: str | None, *, ticket: int | None = None
    ) -> None:
        """If a ladder group is now fully closed, persist its outcome. Never raises."""
        ref = (signal_ref or "").strip() or None
        try:
            async with session_scope() as session:
                if not ref and ticket is not None:
                    row = await session.execute(
                        select(Trade.signal_ref).where(Trade.ticket == int(ticket))
                    )
                    ref = row.scalar_one_or_none()
                await ladder_stats.maybe_record_closed_group(session, signal_ref=ref)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not undo a close
            log.warning(
                "ladder_outcome_record_failed signal_ref=%s ticket=%s error=%s",
                ref,
                ticket,
                exc,
            )

    # ==================================================================================
    # THE WRITE PIPELINE (BUILD_PROMPT §5.3, §6)
    # ==================================================================================

    def _shape_volume(
        self, ref: SymbolRef, volume: Any
    ) -> tuple[float, list[str], str | None]:
        """Fit an explicitly requested lot size onto the broker's grid, never rounding up.

        Rounding a model-supplied volume *up* to ``volume_min`` would silently multiply the
        risk it asked for, so a volume below the minimum is refused and left for the model to
        resolve (§2). Clamping *down* to ``volume_max`` is safe and is stated in the result.
        """
        value = _as_float(volume)
        name = ref.name
        if value is None or value <= 0.0:
            return 0.0, [], f"volume must be a positive number of lots, got {volume!r}"

        notes: list[str] = []
        step = float(ref.volume_step)
        shaped = value
        if step > 0.0:
            shaped = round(math.floor(value / step + VOLUME_EPS) * step, 8)
            if abs(shaped - value) > VOLUME_EPS:
                notes.append(
                    f"{_lots(value)} lots is not a multiple of the {_lots(step)} lot step on "
                    f"{name}; rounded down to {_lots(shaped)}"
                )
        if shaped < float(ref.volume_min) - VOLUME_EPS:
            return (
                0.0,
                [],
                (
                    f"{_lots(value)} lots is below the {_lots(ref.volume_min)} minimum on "
                    f"{name} — send at least {_lots(ref.volume_min)} lots, or omit volume "
                    f"so a classic signal uses trading.order_lot_size"
                ),
            )
        if shaped > float(ref.volume_max) + VOLUME_EPS:
            capped = (
                round(math.floor(float(ref.volume_max) / step + VOLUME_EPS) * step, 8)
                if step > 0.0
                else float(ref.volume_max)
            )
            notes.append(
                f"{_lots(value)} lots is above the {_lots(ref.volume_max)} maximum on {name}; "
                f"clamped down to {_lots(capped)}"
            )
            shaped = capped
        return shaped, notes, None

    def _normalize_tps(
        self, ref: SymbolRef, side: str, tp: Any
    ) -> tuple[list[float], list[float], list[str], str | None]:
        """Read the model's target list into rounded prices, capped at MAX_TP_INPUT.

        Returns ``(targets, unmanaged, notes, refusal)``. A ladder (4+ rungs) keeps every
        accepted price so the runner can be chosen from TP4–TP6 rather than from the first
        three celebration levels. Classic 1–3 TP lists still cap at MAX_TP_SPLITS: anything
        beyond that is unmanaged. Silently keeping ``tp[0]`` and discarding the rest is the
        failure §5.3 exists to prevent.
        """
        if tp is None:
            return [], [], [], None
        items = list(tp) if isinstance(tp, (list, tuple)) else [tp]
        values: list[float] = []
        notes: list[str] = []
        for position, item in enumerate(items):
            value = _as_float(item)
            if value is None:
                return (
                    [],
                    [],
                    [],
                    f"tp[{position}] must be a number, got {item!r} — send absolute prices, or "
                    f"use price_from_distance to turn a distance into one",
                )
            if value <= 0.0:
                # 0 is how MT5 spells "no target"; a list of them is just an empty list.
                continue
            rounded = pricing.round_stop_away(ref, value, side, "tp")
            if rounded in values:
                notes.append(
                    f"two targets round to the same price {_price_text(rounded, ref.digits)} on "
                    f"{ref.name}; the duplicate was dropped"
                )
                continue
            values.append(rounded)

        input_cap = int(self.settings.MAX_TP_INPUT)
        unmanaged: list[float] = []
        if len(values) > input_cap:
            unmanaged = values[input_cap:]
            values = values[:input_cap]
            notes.append(
                f"{len(unmanaged) + input_cap} targets were given but MAX_TP_INPUT is "
                f"{input_cap}; {_join([_price_text(v, ref.digits) for v in unmanaged])} "
                f"{'is' if len(unmanaged) == 1 else 'are'} recorded as unmanaged and no order "
                f"carries {'it' if len(unmanaged) == 1 else 'them'}"
            )

        split_cap = int(self.settings.MAX_TP_SPLITS)
        if not pricing.is_tp_ladder(values) and len(values) > split_cap:
            extra = values[split_cap:]
            values = values[:split_cap]
            unmanaged = extra + unmanaged
            notes.append(
                f"{len(extra) + split_cap} targets were given but MAX_TP_SPLITS is {split_cap}; "
                f"{_join([_price_text(v, ref.digits) for v in extra])} "
                f"{'is' if len(extra) == 1 else 'are'} recorded as unmanaged and no order "
                f"carries {'it' if len(extra) == 1 else 'them'}"
            )
        return values, unmanaged, notes, None

    async def _prepare(
        self,
        ctx: RunContext,
        *,
        symbol: str,
        side: str,
        sl: Any,
        tp: Any,
        volume: Any,
        risk_percent: Any,
        pending: bool,
        order_type: str | None = None,
        price: Any = None,
        expiry_minutes: Any = None,
        runner_mode: Any = None,
        runner_tp_index: Any = None,
    ) -> tuple[_Plan | None, dict[str, Any] | None]:
        """Run checks 1 to 5 of §5.3 and return either a complete plan or a refusal.

        1. a posted stop, when the channel gave one (a missing stop is an incomplete entry)
        2. the symbol resolves and quotes   3. the side and, for a pending order, the inferred
        type are legal   4. a posted stop is on the right side and far enough from the market
        5. the volume is an explicit lot, the operator's classic order size, or —
        when trading.use_fixed_order_lots is off — a model risk_percent.
        Ladders and incomplete entries use trading.ladder_volume. With the setting
        on (the default), a classic signal ignores risk_percent and sizes as
        orders_per_signal × order_lot_size.
        """
        notes: list[str] = []

        # A missing stop is not a refusal. The channel prints the symbol and side first and
        # the stop later. Inventing one here is forbidden; orphans stay with the watchdog.
        stop_value = _as_float(sl)
        stopless = stop_value is None or stop_value <= 0.0

        # ---- 2. resolve the symbol exactly and quote it.
        ref = await self.resolver.get(str(symbol))
        quote = await self.resolver.quote(ref.name)
        if not ref.tradable:
            return None, _refuse(
                f"{ref.name} is not fully tradable on this account right now "
                f"(trade_mode={ref.trade_mode}) — the broker allows no new position here",
                symbol=ref.name,
            )

        # ---- 3. the side, and for a pending order the type inferred from side + price.
        direction = str(side or "").strip().lower()
        if direction not in ("buy", "sell"):
            return None, _refuse(f"side must be 'buy' or 'sell', got {side!r}")

        pending_explanation = ""
        if pending:
            entry_raw = _as_float(price)
            if entry_raw is None or entry_raw <= 0.0:
                return None, _refuse(
                    f"price must be a positive price for a pending order, got {price!r}"
                )
            entry = pricing.round_price(ref, entry_raw)
            inferred, pending_explanation = pricing.infer_pending_type(
                direction, str(order_type or ""), entry, quote.bid, quote.ask
            )
            if inferred is None:
                return None, _refuse(pending_explanation, symbol=ref.name, price=entry)
            resolved_type = inferred
            notes.append(pending_explanation)
        else:
            entry = quote.ask if direction == "buy" else quote.bid
            resolved_type = ORDER_TYPE_BUY if direction == "buy" else ORDER_TYPE_SELL

        # ---- 4. A posted stop must sit on the correct side and clear the broker's minimum
        # distance. Rounded away from the market FIRST, then validated, because rounding a
        # buy's stop up moves it toward price and can breach trade_stops_level (§2).
        # No posted stop: open without one and do not invent levels.
        if stopless:
            stop = 0.0
            notes.append(
                "the channel did not post a stop — opening without one. The stopless "
                "watchdog keeps watching this position until a later post supplies the level"
            )
        else:
            assert stop_value is not None
            stop = pricing.round_stop_away(ref, stop_value, direction, "sl")
            if abs(stop - stop_value) > 0.0:
                notes.append(
                    f"the stop {_price_text(stop_value, ref.digits)} was rounded away from the "
                    f"market onto the point grid at {_price_text(stop, ref.digits)}"
                )
            refusal = guards.check_sl_side(direction, entry, stop)
            if refusal:
                return None, _refuse(
                    refusal, stage="sl_side", symbol=ref.name, entry=entry, sl=stop
                )

        targets, unmanaged, tp_notes, tp_refusal = self._normalize_tps(ref, direction, tp)
        if tp_refusal:
            return None, _refuse(tp_refusal, symbol=ref.name)
        notes.extend(tp_notes)
        if stopless:
            if targets:
                notes.append(
                    "channel targets were recorded and not sent to the broker: cover 1R and "
                    "the runner take-profit need the posted stop, which is not here yet"
                )
                unmanaged = list(targets) + [price for price in unmanaged if price not in targets]
            targets = []
            ladder = False
        else:
            ladder = pricing.is_tp_ladder(targets)
            # Validate the stop against the market now. Take-profits are checked after the
            # split so a ladder's celebration rungs cannot refuse an order that will not
            # even carry them.
            refusal = guards.check_stops_level(
                ref,
                direction,
                entry,
                stop,
                0.0,
                quote.bid,
                quote.ask,
                pending=pending,
            )
            if refusal:
                return None, _refuse(
                    refusal,
                    stage="stops_level",
                    symbol=ref.name,
                    entry=entry,
                    sl=stop,
                    tp=None,
                    stops_level_points=ref.stops_level_points,
                    bid=quote.bid,
                    ask=quote.ask,
                )

        # ---- 5. the volume.
        # Ladders and incomplete (no posted stop) entries ignore both arguments and use
        # trading.ladder_volume. A classic signal honors an explicit volume. When
        # trading.use_fixed_order_lots is on (the default), a model risk_percent does
        # not size it: risk_percent=2 used to come out near 0.02 lots and split into
        # two 0.01 minimums, instead of orders_per_signal × order_lot_size. Off, that
        # percent sizes the trade again.
        wants_volume = volume is not None
        wants_risk = risk_percent is not None

        info = await self._account_info()
        balance = float(info.balance)
        currency = str(info.currency)
        effective_mode, detected_mode, forced_mode = self._margin_mode(info)
        if forced_mode:
            notes.append(
                f"margin mode is forced to {effective_mode} in config; the broker reports "
                f"{detected_mode}"
            )

        used_risk: float | None = None
        chosen_runner_mode: str | None = None
        suggested_runner_mode: str | None = None
        chosen_runner_tp_index: int | None = None
        suggested_runner_tp_index: int | None = None
        runner_tp_bias: int | None = None
        fixed_lots = True
        if not ladder and not stopless:
            async with session_scope() as session:
                fixed_lots = await use_fixed_order_lots(session)
        if ladder or stopless:
            async with session_scope() as session:
                ladder_volume = await self._ladder_volume(session)
                stats = await ladder_stats.stats_for_source(session, ctx.source_id)
            requested_mode = ladder_stats.normalize_runner_mode(runner_mode)
            if runner_mode is not None and str(runner_mode).strip() != "" and requested_mode is None:
                return None, _refuse(
                    "runner_mode must be 'be_after_cover' or 'hold_channel_sl' — "
                    "omit it to use the server's suggested mode from channel ladder stats",
                    stage="runner_mode",
                )
            suggested_runner_mode = stats.suggested_mode
            chosen_runner_mode = requested_mode or suggested_runner_mode
            runner_tp_bias = int(stats.suggested_runner_tp_bias)
            preview = None
            if ladder:
                preview = pricing.plan_ladder_targets(
                    ref, direction, entry, stop, targets, bias=runner_tp_bias
                )
            if preview is not None and preview.n > 0 and preview.suggested_index:
                suggested_runner_tp_index = int(preview.suggested_index)
                requested_index = None
                if runner_tp_index is not None and str(runner_tp_index).strip() != "":
                    raw_index = _as_float(runner_tp_index)
                    if raw_index is None or raw_index != int(raw_index):
                        return None, _refuse(
                            "runner_tp_index must be a whole 1-based TP number — omit it to "
                            "use the server's suggested runner TP",
                            stage="runner_tp_index",
                        )
                    requested_index = int(raw_index)
                    if abs(requested_index - suggested_runner_tp_index) > 1:
                        return None, _refuse(
                            f"runner_tp_index may shift the suggested index "
                            f"{suggested_runner_tp_index}/{preview.n} by at most ±1 "
                            f"(got {requested_index}) — state the reason in finish",
                            stage="runner_tp_index",
                        )
                    if requested_index < 1 or requested_index > preview.n:
                        return None, _refuse(
                            f"runner_tp_index must be between 1 and {preview.n} (numbered TPs, "
                            "LONG excluded)",
                            stage="runner_tp_index",
                        )
                chosen_runner_tp_index = requested_index or suggested_runner_tp_index
            notes.append(stats.blurb())
            if preview is not None and suggested_runner_tp_index and preview.n:
                notes.append(
                    ladder_stats.format_runner_tp_blurb(
                        index=suggested_runner_tp_index,
                        n=preview.n,
                        price=float(preview.runner_tp),
                        stats=stats,
                        chosen_index=chosen_runner_tp_index,
                    )
                )
            notes.append(
                f"runner_mode={chosen_runner_mode} (suggested {suggested_runner_mode})"
                + (
                    "; caution override of hold_channel_sl"
                    if (
                        requested_mode == ladder_stats.MODE_BE_AFTER_COVER
                        and suggested_runner_mode == ladder_stats.MODE_HOLD_CHANNEL_SL
                    )
                    else ""
                )
            )
            fixed_label = (
                "multi-TP ladder" if ladder else "incomplete entry with no posted stop"
            )
            if wants_risk:
                notes.append(
                    f"risk_percent {_trim(_as_float(risk_percent) or 0.0)}% was ignored: a "
                    f"{fixed_label} is a fixed {_lots(ladder_volume)} lots, never a percent "
                    f"of balance"
                )
            if wants_volume:
                notes.append(
                    f"explicit volume {_lots(_as_float(volume) or 0.0)} was ignored: a "
                    f"{fixed_label} uses the operator's fixed ladder size of "
                    f"{_lots(ladder_volume)} lots"
                )
            total, volume_notes, volume_refusal = self._shape_volume(ref, ladder_volume)
            if volume_refusal:
                return None, _refuse(
                    f"ladder volume {_lots(ladder_volume)} lots cannot be placed on {ref.name}: "
                    f"{volume_refusal} — not substituting a tiny risk-percent size",
                    stage="volume",
                    symbol=ref.name,
                )
            notes.extend(volume_notes)
            volume_source = "ladder_volume"
            if stopless:
                notes.append(
                    f"incomplete entry: {_lots(total)} lots fixed (trading.ladder_volume), "
                    f"split into two legs when the broker allows separate positions; "
                    f"risk_percent is not used and no stop or take-profit was invented"
                )
            else:
                notes.append(
                    f"ladder: {_lots(total)} lots fixed (trading.ladder_volume), split into two "
                    f"legs when the broker's volume_min allows; risk_percent is not used"
                )
        elif wants_volume:
            if wants_risk and not fixed_lots:
                return None, _refuse(
                    "supply exactly one of volume or risk_percent, not both — send volume for an "
                    "explicit lot size, or risk_percent to have the server size the trade from the "
                    "distance between entry and stop",
                    stage="volume_arguments",
                )
            total, volume_notes, volume_refusal = self._shape_volume(ref, volume)
            if volume_refusal:
                return None, _refuse(volume_refusal, stage="volume", symbol=ref.name)
            notes.extend(volume_notes)
            volume_source = "explicit"
            notes.append(
                f"{_lots(total)} lots as instructed; no risk sizing was applied"
            )
            if wants_risk:
                notes.append(
                    _ignored_risk_note(
                        risk_percent,
                        "trading.use_fixed_order_lots is on and an explicit volume was "
                        "supplied; that volume is the size that was used",
                    )
                )
                log.info(
                    "classic_risk_percent_ignored symbol=%s risk_percent=%s "
                    "use_fixed_order_lots=true volume_source=explicit",
                    ref.name,
                    risk_percent,
                )
        elif wants_risk and not fixed_lots:
            used_risk = _as_float(risk_percent)
            if used_risk is None or used_risk <= 0.0:
                return None, _refuse(
                    f"risk_percent must be a positive number, got {risk_percent!r}",
                    stage="volume",
                )
            sized = pricing.compute_volume(ref, balance, used_risk, entry, stop, currency)
            if not sized.ok:
                return None, _refuse(sized.message, stage="volume", symbol=ref.name)
            total = sized.volume
            volume_source = "risk_percent"
            notes.append(sized.message)
        else:
            # Classic signal, no explicit volume. With fixed lots on, a model
            # risk_percent is ignored and the operator lot is used. With it off and
            # no risk_percent, the same operator lot is the size.
            async with session_scope() as session:
                orders_n = await orders_per_signal(session)
                lot = await order_lot_size(session)
            if len(targets) > 1:
                targets = sorted(targets, key=lambda price: abs(float(price) - float(entry)))
            order_count = min(orders_n, len(targets)) if targets else 1
            if effective_mode != "hedging":
                order_count = 1
            if targets and len(targets) > order_count:
                extra = targets[order_count:]
                targets = targets[:order_count]
                unmanaged = list(extra) + [price for price in unmanaged if price not in extra]
                notes.append(
                    f"{len(extra) + order_count} targets were named but "
                    f"trading.orders_per_signal is {orders_n}; "
                    f"{_join([_price_text(v, ref.digits) for v in extra])} "
                    f"{'is' if len(extra) == 1 else 'are'} recorded as unmanaged"
                )
            each, volume_notes, volume_refusal = self._shape_volume(ref, lot)
            if volume_refusal:
                return None, _refuse(
                    f"order lot size {_lots(lot)} cannot be placed on {ref.name}: "
                    f"{volume_refusal}",
                    stage="volume",
                    symbol=ref.name,
                )
            notes.extend(volume_notes)
            total = round(each * order_count, 8)
            volume_source = "order_lot_size"
            if wants_risk:
                notes.append(
                    _ignored_risk_note(
                        risk_percent,
                        f"trading.use_fixed_order_lots is on, so a classic signal opens "
                        f"{order_count} order(s) of {_lots(each)} lots each "
                        f"(trading.orders_per_signal={orders_n} × "
                        f"trading.order_lot_size={_lots(each)}), never a percent of balance",
                    )
                )
                log.info(
                    "classic_risk_percent_ignored symbol=%s risk_percent=%s "
                    "use_fixed_order_lots=true orders_per_signal=%s order_lot_size=%s "
                    "volume_source=order_lot_size",
                    ref.name,
                    risk_percent,
                    orders_n,
                    each,
                )
            notes.append(
                f"{order_count} order(s) of {_lots(each)} lots each "
                f"(trading.orders_per_signal={orders_n}, trading.order_lot_size); "
                f"risk percent was not used"
            )

        # ---- multi-TP: ladders become two legs; incomplete entries use the same split
        # with no broker targets; classic signals stay one-per-target.
        if stopless:
            legs, split_notes, extra_unmanaged = self._split_incomplete_legs(
                ref, effective_mode, total
            )
        else:
            legs, split_notes, extra_unmanaged = self._split_legs(
                ref,
                effective_mode,
                total,
                targets,
                side=direction,
                entry=entry,
                sl=stop,
                ladder=ladder,
                runner_tp_bias=runner_tp_bias or 0,
                runner_tp_index=chosen_runner_tp_index,
            )
        notes.extend(split_notes)
        unmanaged = list(unmanaged) + list(extra_unmanaged)

        # Validate only the TPs that will actually be sent. A ladder TP that the market has
        # already passed is dropped rather than refusing the whole entry.
        valid_legs: list[_Leg] = []
        for leg in legs:
            if not leg.tp:
                valid_legs.append(leg)
                continue
            refusal = guards.check_stops_level(
                ref,
                direction,
                entry,
                stop,
                leg.tp,
                quote.bid,
                quote.ask,
                pending=pending,
            )
            if refusal:
                if ladder:
                    notes.append(
                        f"ladder {leg.role or 'leg'} take-profit "
                        f"{_price_text(leg.tp, ref.digits)} is not valid from the market "
                        f"({refusal}); that leg opens with no broker TP"
                    )
                    unmanaged.append(leg.tp)
                    leg.tp = 0.0
                    valid_legs.append(leg)
                    continue
                return None, _refuse(
                    refusal,
                    stage="stops_level",
                    symbol=ref.name,
                    entry=entry,
                    sl=stop,
                    tp=leg.tp,
                    stops_level_points=ref.stops_level_points,
                    bid=quote.bid,
                    ask=quote.ask,
                )
            valid_legs.append(leg)
        legs = valid_legs

        placed_targets = [leg.tp for leg in legs if leg.tp]
        total = round(sum(leg.volume for leg in legs), 8)

        expiration_ts: int | None = None
        if pending and expiry_minutes is not None:
            minutes = _as_float(expiry_minutes)
            if minutes is None or minutes <= 0.0:
                return None, _refuse(
                    f"expiry_minutes must be a positive number of minutes, got "
                    f"{expiry_minutes!r}"
                )
            expiration_ts = int(await self._broker_now() + minutes * 60.0)
            notes.append(
                f"the order expires at {_broker_iso(expiration_ts)} broker time "
                f"({int(minutes)} minutes from now) if it has not filled"
            )

        if not ladder and not stopless and len(legs) >= 2:
            requested = str(runner_mode or "").strip().lower()
            if requested == ladder_stats.MODE_HOLD_CHANNEL_SL:
                chosen_runner_mode = ladder_stats.MODE_HOLD_CHANNEL_SL
                notes.append(
                    "runner_mode=hold_channel_sl: remaining stops stay on the channel SL after TP1"
                )
            else:
                async with session_scope() as session:
                    be_on = await be_after_tp1_enabled(session)
                if be_on or requested in (MODE_BE_AFTER_TP1, ladder_stats.MODE_BE_AFTER_COVER):
                    chosen_runner_mode = MODE_BE_AFTER_TP1
                    notes.append(
                        "after TP1 the server moves the remaining stops to break-even"
                    )
                else:
                    notes.append("break-even after TP1 is off; remaining stops stay where they are")

        plan = _Plan(
            ref=ref,
            quote=quote,
            side=direction,
            order_type=resolved_type,
            pending=pending,
            entry=entry,
            sl=stop,
            tps=placed_targets,
            unmanaged_tps=unmanaged,
            legs=legs,
            total_volume=total,
            volume_source=volume_source,
            risk_percent=used_risk,
            margin_mode=effective_mode,
            balance=balance,
            currency=currency,
            notes=notes,
            expiration_ts=expiration_ts,
            pending_explanation=pending_explanation,
            kind="incomplete" if stopless else ("ladder" if ladder else "standard"),
            runner_mode=chosen_runner_mode,
            suggested_runner_mode=suggested_runner_mode,
            runner_tp_bias=runner_tp_bias,
            suggested_runner_tp_index=suggested_runner_tp_index,
            chosen_runner_tp_index=chosen_runner_tp_index,
        )
        return plan, None

    def _split_incomplete_legs(
        self,
        ref: SymbolRef,
        margin_mode: str,
        total: float,
    ) -> tuple[list[_Leg], list[str], list[float]]:
        """Two naked legs of the fixed ladder size, or one when the broker cannot split.

        An incomplete teaser has no posted stop and no broker targets. The cover and runner
        roles are stored so a later channel post can modify one leg to 1R and leave the
        other as the runner. Nothing here invents an SL or a TP.
        """
        notes: list[str] = []
        if margin_mode != "hedging":
            notes.append(
                f"incomplete entry on a {margin_mode} account: one position of "
                f"{_lots(total)} lots, no stop and no take-profit. When the channel posts "
                f"levels, modify this ticket — do not open a second trade"
            )
            return [_Leg(index=0, volume=total, tp=0.0, role="cover")], notes, []

        parts = pricing.split_volume(ref, total, 2)
        if len(parts) < 2:
            notes.append(
                f"{_lots(total)} lots cannot fund two legs of at least "
                f"{_lots(ref.volume_min)} on {ref.name}, so one position was opened with "
                f"no stop and no take-profit"
            )
            return [_Leg(index=0, volume=total, tp=0.0, role="cover")], notes, []
        notes.append(
            f"incomplete entry on a hedging account: {_lots(total)} lots split "
            f"{_lots(parts[0])} / {_lots(parts[1])} (cover and runner), both without a "
            f"stop and without a take-profit. When the channel posts SL and targets, "
            f"modify these legs — do not open a second trade"
        )
        return (
            [
                _Leg(index=0, volume=parts[0], tp=0.0, role="cover"),
                _Leg(index=1, volume=parts[1], tp=0.0, role="runner"),
            ],
            notes,
            [],
        )

    def _split_legs(
        self,
        ref: SymbolRef,
        margin_mode: str,
        total: float,
        targets: list[float],
        *,
        side: str,
        entry: float,
        sl: float,
        ladder: bool = False,
        runner_tp_bias: int = 0,
        runner_tp_index: int | None = None,
    ) -> tuple[list[_Leg], list[str], list[float]]:
        """Turn one volume and N targets into the orders that will actually be sent (§5.3).

        * no target        -> one order with ``tp = 0.0``; legal, and the model manages the exit
        * ladder (4+ TPs)  -> two legs, ~50/50: cover at money 1R, runner at formula index / LONG
        * hedging, N > 1   -> N sub-positions by largest remainder, summing exactly to ``total``
        * netting/exchange -> one order with ``tp[0]``; the rest are unmanaged and said so
        """
        notes: list[str] = []
        unmanaged: list[float] = []

        if ladder:
            planned = pricing.plan_ladder_targets(
                ref,
                side,
                entry,
                sl,
                targets,
                bias=int(runner_tp_bias),
                runner_tp_index=runner_tp_index,
            )
            notes.extend(planned.notes)
            unmanaged = list(planned.unmanaged)
            cover_tp = planned.cover_tp
            runner_tp = planned.runner_tp

            if margin_mode != "hedging":
                if runner_tp and abs(runner_tp - cover_tp) > 10 ** (-max(ref.digits, 0)):
                    unmanaged = [runner_tp] + [t for t in unmanaged if t != runner_tp]
                notes.append(
                    f"this is a {margin_mode} account, so the ladder cannot open two tickets: "
                    f"{_lots(total)} lots carry the 1R cover at "
                    f"{_price_text(cover_tp, ref.digits)}. The runner "
                    f"{_price_text(runner_tp, ref.digits)} is unmanaged — trail it yourself "
                    f"with modify_position if you keep the position past 1R"
                )
                return (
                    [_Leg(index=0, volume=total, tp=cover_tp, role="cover")],
                    notes,
                    unmanaged,
                )

            parts = pricing.split_volume(ref, total, 2)
            if len(parts) < 2:
                notes.append(
                    f"{_lots(total)} lots cannot fund two legs of at least "
                    f"{_lots(ref.volume_min)} on {ref.name}, so one cover order was placed at "
                    f"1R {_price_text(cover_tp, ref.digits)}; the runner "
                    f"{_price_text(runner_tp, ref.digits)} is unmanaged"
                )
                if runner_tp:
                    unmanaged = [runner_tp] + [t for t in unmanaged if t != runner_tp]
                return (
                    [_Leg(index=0, volume=total, tp=cover_tp, role="cover")],
                    notes,
                    unmanaged,
                )
            notes.append(
                f"ladder on a hedging account: {_lots(total)} lots split "
                f"{_lots(parts[0])} / {_lots(parts[1])} — cover 1R at "
                f"{_price_text(cover_tp, ref.digits)}, runner at "
                f"{_price_text(runner_tp, ref.digits)}. Intermediate channel TPs are not "
                f"broker targets"
            )
            return (
                [
                    _Leg(index=0, volume=parts[0], tp=cover_tp, role="cover"),
                    _Leg(index=1, volume=parts[1], tp=runner_tp, role="runner"),
                ],
                notes,
                unmanaged,
            )

        if not targets:
            return [_Leg(index=0, volume=total, tp=0.0)], notes, unmanaged

        if margin_mode != "hedging" and len(targets) > 1:
            unmanaged = targets[1:]
            notes.append(
                f"this is a {margin_mode} account, where one symbol carries a single net "
                f"position, so the volume cannot be split: only "
                f"{_price_text(targets[0], ref.digits)} was placed as the take profit. "
                f"{_join([_price_text(v, ref.digits) for v in unmanaged])} "
                f"{'is' if len(unmanaged) == 1 else 'are'} recorded as unmanaged — nothing "
                f"will close the position there, so call modify_position or close_position "
                f"when price reaches them"
            )
            return [_Leg(index=0, volume=total, tp=targets[0])], notes, unmanaged

        if len(targets) == 1:
            return [_Leg(index=0, volume=total, tp=targets[0])], notes, unmanaged

        parts = pricing.split_volume(ref, total, len(targets))
        if not parts:
            # compute_volume/_shape_volume already refused anything under volume_min, so this
            # is unreachable in practice; falling back to a single order is still the right
            # answer if a broker ever reports an odd step.
            notes.append(
                f"{_lots(total)} lots could not be split on the {_lots(ref.volume_step)} lot "
                f"step of {ref.name}; one order was placed at the first target instead"
            )
            unmanaged = targets[1:]
            return [_Leg(index=0, volume=total, tp=targets[0])], notes, unmanaged

        if len(parts) < len(targets):
            unmanaged = targets[len(parts) :]
            notes.append(
                f"{_lots(total)} lots cannot fund {len(targets)} sub-positions of at least "
                f"{_lots(ref.volume_min)} each, so the split was reduced to {len(parts)}; "
                f"{_join([_price_text(v, ref.digits) for v in unmanaged])} "
                f"{'is' if len(unmanaged) == 1 else 'are'} recorded as unmanaged"
            )
            targets = targets[: len(parts)]
        else:
            notes.append(
                f"hedging account: {_lots(total)} lots split into {len(parts)} sub-positions "
                f"({_join([_lots(p) for p in parts])}), one per target, all sharing one "
                f"signal reference and one magic number"
            )
        return (
            [_Leg(index=i, volume=parts[i], tp=targets[i]) for i in range(len(parts))],
            notes,
            unmanaged,
        )

    async def _gate(self, ctx: RunContext, plan: _Plan) -> dict[str, Any] | None:
        """Run checks 6 to 8 of §5.3: operator limits, the duplicate guard, then the arm gate.

        The arm gate is last of the three on purpose: disarmed, the model still receives the
        complete plan it would have executed, and only the broker call is withheld (§5.5).
        """
        async with session_scope() as session:
            refusal = await guards.check_optional_limits(
                session,
                symbol=plan.ref.name,
                volume=plan.total_volume,
                rpc=self.rpc,
                resolver=self.resolver,
                now_ts=time.time(),
            )
            if refusal:
                return _refuse(refusal, stage="operator_limit", plan=plan.summary())

            refusal = await guards.check_duplicate_signal(session, ctx.signal_ref)
            if refusal:
                return _refuse(
                    refusal,
                    stage="duplicate_signal",
                    signal_ref=ctx.signal_ref,
                    plan=plan.summary(),
                )

            if not ctx.bypass_arm:
                refusal = await guards.check_armed(session)
                if refusal:
                    return _refuse(refusal, stage="disarmed", would_have=plan.summary())
        return None

    async def _check_margin(self, plan: _Plan) -> dict[str, Any] | None:
        """Check 9 of §5.3: the whole plan must fit inside MARGIN_HEADROOM of free margin."""
        info = await self._account_info()
        margin_free = float(info.margin_free)
        currency = str(info.currency)
        required = 0.0
        for leg in plan.legs:
            required += await self.rpc.order_calc_margin(
                plan.order_type, plan.ref.name, leg.volume, plan.entry
            )
        headroom = margin_free * float(self.settings.MARGIN_HEADROOM)
        if required > headroom + 0.005:
            shortfall = required - headroom
            if plan.volume_source == "ladder_volume":
                margin_hint = (
                    f"not enough free margin for the fixed ladder size of "
                    f"{_lots(plan.total_volume)} lots on {plan.ref.name} "
                    f"({_join([_lots(leg.volume) for leg in plan.legs])} per leg): "
                    f"need {_money(required, currency)} and only "
                    f"{_money(headroom, currency)} of the {_money(margin_free, currency)} free "
                    f"margin is usable at the {float(self.settings.MARGIN_HEADROOM) * 100:.0f}% "
                    f"headroom — short by {_money(shortfall, currency)}. Not substituting a "
                    f"tiny risk-percent lot (that is how a 0.01 position appears). Free margin "
                    f"or lower trading.ladder_volume in settings"
                )
            else:
                margin_hint = (
                    f"not enough free margin: {_lots(plan.total_volume)} lots on "
                    f"{plan.ref.name} would need {_money(required, currency)} and only "
                    f"{_money(headroom, currency)} of the {_money(margin_free, currency)} free "
                    f"margin is usable at the {float(self.settings.MARGIN_HEADROOM) * 100:.0f}% "
                    f"headroom — short by {_money(shortfall, currency)}. Reduce the volume or "
                    f"the risk percent and try again"
                )
            return _refuse(
                margin_hint,
                stage="margin",
                required_margin=round(required, 2),
                margin_free=round(margin_free, 2),
                usable_margin=round(headroom, 2),
                shortfall=round(shortfall, 2),
                currency=currency,
                plan=plan.summary(),
            )
        plan.notes.append(
            f"margin check passed: {_money(required, currency)} required against "
            f"{_money(headroom, currency)} usable of {_money(margin_free, currency)} free"
        )
        return None

    # ==================================================================================
    # THE SEND ITSELF (BUILD_PROMPT §1.3)
    # ==================================================================================

    async def _reprice(self, symbol: str, order_type: int, ref: SymbolRef | None) -> float | None:
        """Re-read the tick and rebuild the price for the side being traded."""
        tick = await self.rpc.symbol_info_tick(symbol)
        if tick is None:
            return None
        bid = float(tick.bid)
        ask = float(tick.ask)
        if bid <= 0.0 or ask <= 0.0:
            return None
        price = ask if int(order_type) == ORDER_TYPE_BUY else bid
        return pricing.round_price(ref, price) if ref is not None else price

    @staticmethod
    def _audit_type(action: int, has_position: bool, ok: bool) -> str:
        """Map a request onto the TradeEvent type that records it."""
        if not ok:
            return "reject"
        if action == TRADE_ACTION_DEAL and has_position:
            return "close"
        if action in (TRADE_ACTION_DEAL, TRADE_ACTION_PENDING):
            return "open"
        if action in (TRADE_ACTION_SLTP, TRADE_ACTION_MODIFY):
            return "modify"
        if action == TRADE_ACTION_REMOVE:
            return "cancel"
        return "open"

    async def send_order(
        self, request: dict[str, Any], *, ctx: RunContext, expect_pending: bool
    ) -> SendResult:
        """Send one request, handle requotes, and record what happened — the single exit point.

        Three traps live in these forty lines (§1.3). ``order_send`` returns **None** when the
        request failed before the trade server was reached, so the null check comes before
        ``.retcode``. Success is ``{10008, 10009}``, not ``10009`` alone — a pending order is
        *placed*, not *done*, and code that misreads that retries orders it already has.
        ``10010`` is a partial fill and must be reported with the volume that actually filled.

        On ``10004`` (requote) or ``10020`` (price changed) the tick is re-read, the price is
        rebuilt for the side being traded and the request is retried at most
        ``REQUOTE_RETRIES`` times; the failure that follows states how far price moved.
        """
        symbol = str(request.get("symbol", "") or "")
        action = int(request.get("action", 0) or 0)
        raw_type = request.get("type")
        order_type = int(raw_type) if raw_type is not None else None
        requested_volume = float(request.get("volume", 0.0) or 0.0)
        has_position = "position" in request
        opening_price = float(request.get("price", 0.0) or 0.0)

        ref: SymbolRef | None = None
        if symbol:
            try:
                ref = await self.resolver.get(symbol)
            except SignalPilotError as exc:
                # Only the point size and the price rounding depend on this; the order itself
                # is already fully built, so a resolver hiccup must not cancel it.
                log.warning("send_order_symbol_ref_failed symbol=%s error=%s", symbol, exc)

        retries = max(0, int(self.settings.REQUOTE_RETRIES))
        movements: list[dict[str, Any]] = []
        attempts = 0
        result: Any = None

        while True:
            attempts += 1
            result = await self.rpc.order_send(request)
            if result is None:
                break
            retcode = int(result.retcode)
            retryable = (
                retcode in REQUOTE_RETCODES
                and action == TRADE_ACTION_DEAL
                and order_type in (ORDER_TYPE_BUY, ORDER_TYPE_SELL)
                and attempts <= retries
            )
            if not retryable:
                break
            rebuilt = await self._reprice(symbol, order_type, ref)
            if rebuilt is None:
                break
            previous = float(request.get("price", 0.0) or 0.0)
            movements.append({"attempt": attempts, "retcode": retcode, "from": previous, "to": rebuilt})
            request["price"] = rebuilt
            log.warning(
                "order_requote symbol=%s retcode=%d attempt=%d price %s -> %s",
                symbol,
                retcode,
                attempts,
                previous,
                rebuilt,
            )

        intended_price = float(request.get("price", 0.0) or 0.0)
        digits = ref.digits if ref is not None else 5
        movement_text = ""
        if movements:
            first = movements[0]["from"]
            last = movements[-1]["to"]
            drift = last - first
            drift_text = (
                f"{_trim(pricing.points(ref, drift))} points"
                if ref is not None and ref.point > 0.0
                else _trim(drift)
            )
            movement_text = (
                f" price moved from {_price_text(first, digits)} to {_price_text(last, digits)} "
                f"({drift_text}) across {len(movements)} requote(s)."
            )

        if result is None:
            message = retcode_message(None) + movement_text
            send_result = SendResult(
                ok=False,
                retcode=None,
                retcode_name=retcode_name(None),
                message=message,
                ticket=_as_int(request.get("position")) or _as_int(request.get("order")),
                volume=0.0,
                price=0.0,
                slippage_points=None,
                comment="",
                raw={
                    "attempts": attempts,
                    "price_movement": movements,
                    "requested_volume": requested_volume,
                },
            )
        else:
            retcode = int(result.retcode)
            comment = str(result.comment or "")
            filled = float(result.volume)
            fill_price = float(result.price)
            ok = retcode in SUCCESS_RETCODES or retcode == PARTIAL_RETCODE

            if has_position:
                ticket = _as_int(request.get("position"))
            elif "order" in request:
                ticket = _as_int(request.get("order"))
            else:
                ticket = _as_int(result.order) or _as_int(result.deal) or None

            slippage: float | None = None
            if (
                ok
                and action == TRADE_ACTION_DEAL
                and ref is not None
                and ref.point > 0.0
                and fill_price > 0.0
                and intended_price > 0.0
            ):
                raw_points = (fill_price - intended_price) / ref.point
                # Signed against the direction traded: positive is always the worse fill.
                slippage = round(
                    raw_points if order_type == ORDER_TYPE_BUY else -raw_points, 1
                )

            if retcode == PARTIAL_RETCODE:
                message = (
                    f"only {_lots(filled)} of the {_lots(requested_volume)} lots requested were "
                    f"filled at {_price_text(fill_price, digits)} — the rest was rejected. "
                    f"{retcode_message(retcode, comment)}"
                )
            elif ok:
                if action == TRADE_ACTION_PENDING or (expect_pending and action == TRADE_ACTION_DEAL):
                    message = (
                        f"order #{ticket} accepted at {_price_text(intended_price, digits)} for "
                        f"{_lots(requested_volume)} lots — it is on the book and waiting for its "
                        f"price, nothing is filled yet"
                    )
                elif action == TRADE_ACTION_DEAL:
                    message = (
                        f"filled {_lots(filled or requested_volume)} lots at "
                        f"{_price_text(fill_price, digits)}"
                        + (
                            f" ({_trim(slippage)} points of slippage against the intended "
                            f"{_price_text(intended_price, digits)})"
                            if slippage is not None
                            else ""
                        )
                    )
                else:
                    message = retcode_message(retcode, comment)
            else:
                message = retcode_message(retcode, comment) + movement_text

            if ok and movement_text:
                message += movement_text

            send_result = SendResult(
                ok=ok,
                retcode=retcode,
                retcode_name=retcode_name(retcode),
                message=message,
                ticket=ticket,
                volume=filled,
                price=fill_price,
                slippage_points=slippage,
                comment=comment,
                raw={
                    "retcode": retcode,
                    "comment": comment,
                    "order": _as_int(result.order),
                    "deal": _as_int(result.deal),
                    "volume": filled,
                    "price": fill_price,
                    "attempts": attempts,
                    "price_movement": movements,
                    "requested_volume": requested_volume,
                },
            )

        await self._audit(
            ctx,
            type=self._audit_type(action, has_position, send_result.ok),
            ticket=send_result.ticket,
            detail={
                "origin": ctx.origin,
                "signal_ref": ctx.signal_ref,
                "reason": _REASON.get(""),
                "request": {
                    key: value
                    for key, value in request.items()
                    if key
                    in (
                        "action",
                        "symbol",
                        "volume",
                        "type",
                        "price",
                        "sl",
                        "tp",
                        "position",
                        "order",
                        "deviation",
                        "magic",
                        "comment",
                        "type_time",
                        "type_filling",
                        "expiration",
                    )
                },
                "result": send_result.raw,
                "message": send_result.message,
                "slippage_points": send_result.slippage_points,
            },
            retcode=send_result.retcode,
        )
        self._emit_send(request, send_result, ctx, expect_pending=expect_pending, action=action)
        return send_result

    def _emit_send(
        self,
        request: dict[str, Any],
        result: SendResult,
        ctx: RunContext,
        *,
        expect_pending: bool,
        action: int,
    ) -> None:
        """Put the right event on the bus for one send, with enough data for the dashboard."""
        raw_type = request.get("type")
        order_type = int(raw_type) if raw_type is not None else None
        payload: dict[str, Any] = {
            "ticket": result.ticket,
            "symbol": request.get("symbol"),
            "side": None if order_type is None else _side_of_order_type(order_type),
            "order_type": None if order_type is None else ORDER_TYPE_NAMES.get(order_type),
            "volume": result.volume or _as_float(request.get("volume")) or 0.0,
            "requested_volume": _as_float(request.get("volume")),
            "price": result.price or _as_float(request.get("price")),
            "sl": _as_float(request.get("sl")),
            "tp": _as_float(request.get("tp")),
            "magic": _as_int(request.get("magic")),
            "comment": request.get("comment") or result.comment,
            "retcode": result.retcode,
            "retcode_name": result.retcode_name,
            "slippage_points": result.slippage_points,
            "message": result.message,
            "run_id": ctx.run_id,
            "signal_ref": ctx.signal_ref,
            "source_id": ctx.source_id,
            "message_row_id": ctx.message_row_id,
            "origin": ctx.origin,
            "reason": _REASON.get(""),
            "ok": result.ok,
        }

        if not result.ok:
            # `reason` names the warning category on the system.warning envelope, so the
            # model's own reason travels under its own key rather than colliding with it.
            warning = dict(payload)
            warning["trade_reason"] = warning.pop("reason", None)
            self._emit(events.SYSTEM_WARNING, reason="order_rejected", **warning)
            return
        if action == TRADE_ACTION_DEAL and "position" in request:
            self._emit(events.POSITION_CLOSED, **payload)
        elif action in (TRADE_ACTION_DEAL, TRADE_ACTION_PENDING):
            self._emit(
                events.ORDER_PLACED if expect_pending else events.POSITION_OPENED, **payload
            )
        elif action in (TRADE_ACTION_SLTP, TRADE_ACTION_MODIFY):
            self._emit(
                events.POSITION_MODIFIED,
                kind="order" if action == TRADE_ACTION_MODIFY else "position",
                **payload,
            )
        elif action == TRADE_ACTION_REMOVE:
            self._emit(events.ORDER_CANCELLED, **payload)

    # ==================================================================================
    # OPENING (BUILD_PROMPT §5.3)
    # ==================================================================================

    async def _persist_open(
        self,
        ctx: RunContext,
        plan: _Plan,
        leg: _Leg,
        result: SendResult,
        reason: str,
        unmanaged: list[float],
    ) -> int | None:
        """Write the Trade row for one accepted leg and return its id."""
        if not result.ticket:
            log.error(
                "trade_row_skipped symbol=%s reason=no_ticket retcode=%s",
                plan.ref.name,
                result.retcode,
            )
            return None
        try:
            async with session_scope() as session:
                trade = Trade(
                    source_id=ctx.source_id,
                    run_id=ctx.run_id,
                    message_row_id=ctx.message_row_id,
                    signal_ref=ctx.signal_ref,
                    ticket=int(result.ticket),
                    kind="order" if plan.pending else "position",
                    symbol=plan.ref.name,
                    side=plan.side,
                    order_type=plan.order_type,
                    volume=result.volume or leg.volume,
                    requested_volume=leg.volume,
                    entry_price=result.price or plan.entry,
                    sl=plan.sl,
                    tp=leg.tp,
                    status="pending" if plan.pending else "open",
                    magic=int(self.settings.MAGIC_NUMBER),
                    comment=str(leg.request.get("comment", "")),
                    external=False,
                    split_index=leg.index,
                    split_count=len(plan.legs),
                    unmanaged_tps=list(unmanaged),
                    ladder_role=leg.role or None,
                    runner_mode=plan.runner_mode,
                    suggested_runner_mode=plan.suggested_runner_mode,
                    suggested_runner_tp_index=plan.suggested_runner_tp_index,
                    chosen_runner_tp_index=plan.chosen_runner_tp_index,
                    runner_tp_bias=plan.runner_tp_bias,
                    open_reason=reason,
                    opened_at=utcnow(),
                )
                session.add(trade)
                await session.flush()
                # send_order audits the send before this row can exist — there is no ticket
                # until the broker answers — so the audit rows for this ticket are linked to
                # it now. Without this the history view can only join them by ticket.
                await session.execute(
                    update(TradeEvent)
                    .where(
                        TradeEvent.ticket == int(result.ticket),
                        TradeEvent.trade_id.is_(None),
                    )
                    .values(trade_id=trade.id)
                )
                return int(trade.id)
        except Exception as exc:  # noqa: BLE001 - the order exists; the row must not undo it
            log.error(
                "trade_row_write_failed ticket=%s symbol=%s error=%s",
                result.ticket,
                plan.ref.name,
                exc,
            )
            return None

    async def _execute_plan(
        self, ctx: RunContext, plan: _Plan, *, reason: str
    ) -> dict[str, Any]:
        """Run checks 10 and 11 of §5.3 for every leg, persist what succeeded, report both.

        Each leg is validated with ``order_check`` — whose success retcode is ``0``, not
        ``10009`` — and only then sent. A leg that fails does not cancel the ones that
        already worked: the result names the tickets that opened *and* the reason the rest
        did not, because a half-filled split the model cannot see is worse than either
        outcome on its own.
        """
        ref = plan.ref
        action = TRADE_ACTION_PENDING if plan.pending else TRADE_ACTION_DEAL
        try:
            filling = pricing.pick_filling(ref, action)
        except TradingError as exc:
            return _refuse(exc.message, stage="filling_mode", plan=plan.summary())

        comment = make_comment(ctx.run_id)
        currency = plan.currency
        digits = ref.digits

        for leg in plan.legs:
            request: dict[str, Any] = {
                "action": action,
                "symbol": ref.name,
                "volume": leg.volume,
                "type": plan.order_type,
                "price": plan.entry,
                "sl": plan.sl,
                "tp": leg.tp,
                "deviation": int(self.settings.DEVIATION_POINTS),
                "magic": int(self.settings.MAGIC_NUMBER),
                "comment": comment,
                "type_time": ORDER_TIME_GTC,
                "type_filling": filling,
            }
            if plan.pending and plan.expiration_ts:
                request["type_time"] = ORDER_TIME_SPECIFIED
                request["expiration"] = int(plan.expiration_ts)
            leg.request = request

            check = await self.rpc.order_check(request)
            if check is None:
                leg.refusal = retcode_message(None)
            else:
                check_retcode = int(check.retcode)
                if check_retcode != CHECK_OK_RETCODE:
                    leg.refusal = (
                        f"{retcode_message(check_retcode, str(check.comment))} — order_check "
                        f"rejected this before anything was sent; free margin would be "
                        f"{_money(float(check.margin_free), currency)} after it"
                    )
            if leg.refusal:
                await self._audit(
                    ctx,
                    type="reject",
                    ticket=None,
                    detail={
                        "stage": "order_check",
                        "symbol": ref.name,
                        "leg": leg.index,
                        "volume": leg.volume,
                        "tp": leg.tp,
                        "sl": plan.sl,
                        "reason": reason,
                        "message": leg.refusal,
                    },
                    retcode=None if check is None else int(check.retcode),
                )
                continue

            leg.result = await self.send_order(request, ctx=ctx, expect_pending=plan.pending)

        opened = [leg for leg in plan.legs if leg.ok]
        failed = [leg for leg in plan.legs if not leg.ok]

        for position, leg in enumerate(opened):
            # Classic splits record unmanaged targets once, on the first surviving row, so
            # the dashboard does not repeat them. Ladder legs each keep the leftover rungs:
            # trail_ladder_runners keys off unmanaged_tps, and the cover row may already be
            # gone from MT5 when the runner needs to lock to break-even.
            unmanaged_for_row = (
                plan.unmanaged_tps
                if plan.kind in ("ladder", "incomplete") or position == 0
                else []
            )
            await self._persist_open(
                ctx,
                plan,
                leg,
                leg.result,  # type: ignore[arg-type]
                reason,
                unmanaged_for_row,
            )

        if opened and not plan.pending and plan.kind == "ladder" and float(plan.sl or 0.0) > 0.0:
            await self._retarget_ladder_cover_from_fills(ctx, plan)

        tickets = [leg.result.ticket for leg in opened if leg.result and leg.result.ticket]
        filled_volume = round(
            sum((leg.result.volume or leg.volume) for leg in opened if leg.result), 8
        )
        fill_price = opened[0].result.price if opened and opened[0].result else None
        slippage = next(
            (leg.result.slippage_points for leg in opened if leg.result and leg.result.slippage_points is not None),
            None,
        )
        first = opened[0].result if opened else (failed[0].result if failed and failed[0].result else None)

        noun = "order" if plan.pending else "position"
        if opened and not failed:
            headline = (
                f"{len(opened)} {noun}{'s' if len(opened) != 1 else ''} "
                f"{'placed' if plan.pending else 'opened'} on {ref.name} {plan.side} for "
                f"{_lots(filled_volume)} lots with the stop at "
                f"{_price_text(plan.sl, digits)}"
            )
            if plan.tps:
                headline += (
                    f" and target(s) "
                    f"{_join([_price_text(t, digits) for t in plan.tps])}"
                )
            else:
                headline += " and no target (tp = 0), so manage the exit yourself"
        elif opened and failed:
            headline = (
                f"PARTIAL: {len(opened)} of {len(plan.legs)} {noun}s "
                f"{'placed' if plan.pending else 'opened'} on {ref.name} "
                f"({_join([f'#{t}' for t in tickets])}); {len(failed)} failed"
            )
        else:
            headline = f"nothing was {'placed' if plan.pending else 'opened'} on {ref.name}"

        failures = [
            {
                "leg": leg.index,
                "volume": leg.volume,
                "tp": leg.tp,
                "message": leg.refusal or (leg.result.message if leg.result else "no result"),
                "retcode": leg.result.retcode if leg.result else None,
                "retcode_name": leg.result.retcode_name if leg.result else None,
            }
            for leg in failed
        ]
        messages = [headline]
        # A leg the broker only partly filled succeeded, so its sentence would otherwise be
        # lost among the successes — and 10010 must always be reported with what filled (§1.3).
        messages.extend(
            f"leg {leg.index}: {leg.result.message}"
            for leg in opened
            if leg.result is not None and leg.result.partial
        )
        messages.extend(f"leg {item['leg']}: {item['message']}" for item in failures)
        if plan.unmanaged_tps:
            messages.append(
                f"unmanaged target(s) "
                f"{_join([_price_text(t, digits) for t in plan.unmanaged_tps])}: no order "
                f"carries them, so close or move a stop yourself when price gets there"
            )
        messages.extend(plan.notes)

        result: dict[str, Any] = {
            "ok": bool(opened),
            "message": ". ".join(m for m in messages if m),
            "tickets": tickets,
            "ticket": tickets[0] if len(tickets) == 1 else None,
            "filled_volume": filled_volume,
            "fill_price": fill_price,
            "slippage_points": slippage,
            "retcode": first.retcode if first else None,
            "retcode_name": first.retcode_name if first else retcode_name(None),
            "symbol": ref.name,
            "side": plan.side,
            "order_type": ORDER_TYPE_NAMES.get(plan.order_type, str(plan.order_type)),
            "entry": plan.entry,
            "sl": plan.sl,
            "tp": list(plan.tps),
            "unmanaged_tps": list(plan.unmanaged_tps),
            "volume": plan.total_volume,
            "risk_percent": plan.risk_percent,
            "volume_source": plan.volume_source,
            "margin_mode": plan.margin_mode,
            "kind": plan.kind,
            "runner_mode": plan.runner_mode,
            "suggested_runner_mode": plan.suggested_runner_mode,
            "runner_tp_bias": plan.runner_tp_bias,
            "suggested_runner_tp_index": plan.suggested_runner_tp_index,
            "chosen_runner_tp_index": plan.chosen_runner_tp_index,
            "split_count": len(plan.legs),
            "legs": [
                {
                    "index": leg.index,
                    "role": leg.role or None,
                    "volume": leg.volume,
                    "tp": leg.tp,
                    "ok": leg.ok,
                    "ticket": leg.result.ticket if leg.result else None,
                    "fill_price": leg.result.price if leg.result else None,
                    "filled_volume": leg.result.volume if leg.result else None,
                    "slippage_points": leg.result.slippage_points if leg.result else None,
                    "retcode": leg.result.retcode if leg.result else None,
                    "retcode_name": leg.result.retcode_name if leg.result else None,
                    "message": leg.refusal or (leg.result.message if leg.result else "no result"),
                }
                for leg in plan.legs
            ],
            "failures": failures,
            "partial_fill": any(leg.result.partial for leg in opened if leg.result),
            "notes": list(plan.notes),
            "currency": currency,
            "digits": digits,
            "signal_ref": ctx.signal_ref,
        }

        link = await self._permalink_for(ctx)
        if opened:
            payload = dict(
                symbol=ref.name, side=plan.side, order_type=result["order_type"],
                volume=filled_volume, requested_volume=plan.total_volume,
                filled_volume=filled_volume, risk_percent=plan.risk_percent,
                entry=fill_price or plan.entry, sl=plan.sl, tp=plan.tps,
                unmanaged_tps=plan.unmanaged_tps, tickets=tickets,
                signal_ref=ctx.signal_ref, digits=digits, currency=currency,
                legs=[dict(leg, sl=plan.sl) for leg in result["legs"] if leg["ok"]],
                pending=plan.pending, kind=plan.kind, runner_mode=plan.runner_mode,
                partial_fill=result["partial_fill"], failures=failures,
            )
            try:
                grouped = getattr(self.notifier, "opened", None)
                if callable(grouped):
                    grouped(ctx.run_id, link=link, **payload)
                else:
                    text = self._render("render_opened", **payload)
                    if text:
                        self._notify("opened", text, link=link)
            except Exception as exc:  # a notification must never change the trade result
                log.warning("notify_entry_failed error=%s", exc)
        if failed:
            text = self._render(
                "render_rejected",
                symbol=ref.name,
                side=plan.side,
                action="place_pending" if plan.pending else "open_market",
                retcode=failures[0]["retcode"],
                retcode_name=failures[0]["retcode_name"],
                message=failures[0]["message"],
                signal_ref=ctx.signal_ref,
                reason=reason,
            )
            if text:
                self._notify("rejected", text, link=link)
        return result

    async def _retarget_ladder_cover_from_fills(self, ctx: RunContext, plan: _Plan) -> None:
        """Move the cover take-profit to money 1R measured from the actual fill, not the quote.

        The plan was built against the bid/ask at send time. Slippage of a few points would
        otherwise leave the cover at the wrong R:R. A failure here is a note, not a rollback:
        the tickets already exist.
        """
        digits = plan.ref.digits
        quantum = 10 ** (-max(digits, 0))
        for leg in plan.legs:
            if leg.role != "cover" or not leg.ok or leg.result is None or not leg.result.ticket:
                continue
            fill = float(leg.result.price or plan.entry)
            new_tp = pricing.one_r_take_profit(plan.ref, plan.side, fill, plan.sl)
            if abs(new_tp - (leg.tp or 0.0)) < quantum:
                continue
            adjusted = await self.modify_position(
                ctx,
                ticket=int(leg.result.ticket),
                tp=new_tp,
                reason="ladder cover 1R from fill",
            )
            if adjusted.get("ok"):
                plan.notes.append(
                    f"cover ticket #{leg.result.ticket} take-profit moved to 1R from fill "
                    f"{_price_text(fill, digits)} -> {_price_text(new_tp, digits)}"
                )
                leg.tp = new_tp
            else:
                plan.notes.append(
                    f"cover 1R from fill could not be applied "
                    f"({adjusted.get('message') or 'modify refused'}); leaving "
                    f"{_price_text(leg.tp, digits)}"
                )
        plan.tps = [leg.tp for leg in plan.legs if leg.tp]

    async def trail_ladder_runners(self, *, quiet_signals: set[str] | None = None) -> dict[str, Any]:
        """Lock a ladder runner to break-even after its sibling cover take-profit fills.

        Groups our tickets by ``signal_ref``. When a two-leg split has leftover unmanaged
        channel TPs (the ladder signature) and only the farther-TP ticket is still live,
        *and* that group's stored ``runner_mode`` is ``be_after_cover`` (or missing, which
        is the safer default), the runner stop is moved to entry plus a small profit
        buffer. ``hold_channel_sl`` is a no-op: the channel stop stays so pullbacks can
        continue to far TPs. Already-BE stops and foreign magics are left alone.
        Protective: bypasses the arm gate the same way the SL fallback does.
        """
        rows = _rows(await self.rpc.positions_get())
        magic = int(self.settings.MAGIC_NUMBER)
        live: dict[int, Any] = {}
        for position in rows:
            try:
                if int(position.magic) != magic:
                    continue
                live[int(position.ticket)] = position
            except (AttributeError, TypeError, ValueError):
                continue

        async with session_scope() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.kind == "position",
                    Trade.split_count == 2,
                    Trade.external.is_(False),
                )
            )
            trades = list(result.scalars().all())

        by_ref: dict[str, list[Trade]] = {}
        for trade in trades:
            if not trade.signal_ref:
                continue
            by_ref.setdefault(str(trade.signal_ref), []).append(trade)

        trailed: list[int] = []
        skipped: list[str] = []
        buffer = int(self.settings.LADDER_BE_BUFFER_POINTS)

        for signal_ref, group in by_ref.items():
            labelled = any(
                str(trade.ladder_role or "") in ("cover", "runner") for trade in group
            )
            if not any(trade.unmanaged_tps for trade in group) and not labelled:
                continue
            live_trades = [trade for trade in group if int(trade.ticket) in live]
            gone = [trade for trade in group if int(trade.ticket) not in live]
            if len(live_trades) != 1 or not gone:
                continue
            runner_row = live_trades[0]
            position = live[int(runner_row.ticket)]
            entry = float(position.price_open)
            live_tp = float(position.tp or 0.0)
            gone_tp = float(gone[0].tp or 0.0)
            if abs(live_tp - entry) <= abs(gone_tp - entry) + 1e-9:
                skipped.append(f"#{runner_row.ticket} is the nearer leg, not the runner")
                continue
            stored_mode = ladder_stats.normalize_runner_mode(runner_row.runner_mode)
            if stored_mode is None:
                for member in group:
                    stored_mode = ladder_stats.normalize_runner_mode(member.runner_mode)
                    if stored_mode is not None:
                        break
            mode = stored_mode or ladder_stats.MODE_BE_AFTER_COVER
            if mode == ladder_stats.MODE_HOLD_CHANNEL_SL:
                skipped.append(
                    f"#{runner_row.ticket} hold_channel_sl: leaving channel SL"
                )
                log.info(
                    "ladder_runner_hold ticket=%s signal_ref=%s",
                    runner_row.ticket,
                    signal_ref,
                )
                continue
            side = POSITION_TYPE_NAMES.get(int(position.type), str(runner_row.side))
            current_sl = float(position.sl)
            if pricing.stop_is_at_or_beyond_breakeven(side, entry, current_sl):
                continue
            try:
                ref = await self.resolver.get(str(position.symbol))
            except SignalPilotError as exc:
                skipped.append(f"#{runner_row.ticket} {exc}")
                continue
            be = pricing.breakeven_stop(ref, side, entry, buffer_points=buffer)
            # Never loosen a stop that is already further into profit than this BE.
            if side == "buy" and current_sl >= be - 1e-12:
                continue
            if side == "sell" and current_sl <= be + 1e-12:
                continue
            ctx = RunContext(
                run_id=uuid.uuid4().hex,
                signal_ref=signal_ref,
                source_id=runner_row.source_id,
                message_row_id=runner_row.message_row_id,
                origin="watchdog",
                bypass_arm=True,
            )
            adjusted = await self.modify_position(
                ctx,
                ticket=int(runner_row.ticket),
                sl=be,
                reason="ladder runner to break-even after cover TP",
                notify=signal_ref not in (quiet_signals or set()),
            )
            if adjusted.get("ok"):
                trailed.append(int(runner_row.ticket))
                log.info(
                    "ladder_runner_be ticket=%s signal_ref=%s sl=%s",
                    runner_row.ticket,
                    signal_ref,
                    be,
                )
            else:
                skipped.append(
                    f"#{runner_row.ticket}: {adjusted.get('message') or 'modify refused'}"
                )

        return {
            "ok": True,
            "trailed": trailed,
            "skipped": skipped,
            "message": (
                f"ladder BE trail: {len(trailed)} runner(s) locked"
                if trailed
                else "ladder BE trail: nothing to move"
            ),
        }

    async def trail_after_tp1(self, *, quiet_signals: set[str] | None = None) -> dict[str, Any]:
        """Move remaining classic orders to breakeven once the nearest take-profit is gone.

        Ladders keep :meth:`trail_ladder_runners`. This path is the 2–3 target split:
        when TP1's ticket is no longer live and other tickets of the same signal still
        are, their stops move to entry plus the ladder breakeven buffer. A group whose
        stored mode is ``hold_channel_sl`` or ``ai_managed`` is left on the stop the
        model already chose. The setting ``trading.be_after_tp1`` turns the whole trail off.
        """
        async with session_scope() as session:
            enabled = await be_after_tp1_enabled(session)
        if not enabled:
            return {
                "ok": True,
                "trailed": [],
                "skipped": [],
                "message": "break-even after TP1 is off",
            }

        rows = _rows(await self.rpc.positions_get())
        magic = int(self.settings.MAGIC_NUMBER)
        live: dict[int, Any] = {}
        for position in rows:
            try:
                if int(position.magic) != magic:
                    continue
                live[int(position.ticket)] = position
            except (AttributeError, TypeError, ValueError):
                continue

        async with session_scope() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.kind == "position",
                    Trade.split_count >= 2,
                    Trade.external.is_(False),
                )
            )
            trades = list(result.scalars().all())

        by_ref: dict[str, list[Trade]] = {}
        for trade in trades:
            if not trade.signal_ref:
                continue
            if str(trade.ladder_role or "") in ("cover", "runner"):
                continue
            by_ref.setdefault(str(trade.signal_ref), []).append(trade)

        trailed: list[int] = []
        skipped: list[str] = []
        buffer = int(self.settings.LADDER_BE_BUFFER_POINTS)

        for signal_ref, group in by_ref.items():
            if len(group) < 2:
                continue
            modes = {str(trade.runner_mode or "") for trade in group}
            if ladder_stats.MODE_HOLD_CHANNEL_SL in modes or MODE_AI_MANAGED in modes:
                skipped.append(f"{signal_ref} already managed")
                continue
            if MODE_BE_AFTER_TP1 not in modes and any(mode for mode in modes):
                # A mode we do not recognise is a management instruction. Leave it.
                skipped.append(f"{signal_ref} has a non-default management mode")
                continue
            nearest = _nearest_tp_trade(group)
            if nearest is None or int(nearest.ticket) in live:
                continue
            survivors = [trade for trade in group if int(trade.ticket) in live and trade is not nearest]
            if not survivors:
                continue
            for trade in survivors:
                position = live[int(trade.ticket)]
                entry = float(position.price_open)
                side = POSITION_TYPE_NAMES.get(int(position.type), str(trade.side))
                current_sl = float(position.sl)
                if pricing.stop_is_at_or_beyond_breakeven(side, entry, current_sl):
                    continue
                try:
                    ref = await self.resolver.get(str(position.symbol))
                except SignalPilotError as exc:
                    skipped.append(f"#{trade.ticket} {exc}")
                    continue
                be = pricing.breakeven_stop(ref, side, entry, buffer_points=buffer)
                if side == "buy" and current_sl >= be - 1e-12:
                    continue
                if side == "sell" and current_sl > 0 and current_sl <= be + 1e-12:
                    continue
                ctx = RunContext(
                    run_id=uuid.uuid4().hex,
                    signal_ref=signal_ref,
                    source_id=trade.source_id,
                    message_row_id=trade.message_row_id,
                    origin="watchdog",
                    bypass_arm=True,
                )
                adjusted = await self.modify_position(
                    ctx,
                    ticket=int(trade.ticket),
                    sl=be,
                    reason="break-even after TP1",
                    notify=signal_ref not in (quiet_signals or set()),
                )
                if adjusted.get("ok"):
                    trailed.append(int(trade.ticket))
                    log.info(
                        "tp1_breakeven ticket=%s signal_ref=%s sl=%s",
                        trade.ticket,
                        signal_ref,
                        be,
                    )
                else:
                    skipped.append(
                        f"#{trade.ticket}: {adjusted.get('message') or 'modify refused'}"
                    )

        return {
            "ok": True,
            "trailed": trailed,
            "skipped": skipped,
            "message": (
                f"TP1 breakeven: {len(trailed)} order(s) locked"
                if trailed
                else "TP1 breakeven: nothing to move"
            ),
        }

    async def _mark_classic_managed(self, ticket: int) -> None:
        """Remember that a non-watchdog stop change already manages this classic group."""
        try:
            async with session_scope() as session:
                result = await session.execute(select(Trade).where(Trade.ticket == int(ticket)))
                trade = result.scalar_one_or_none()
                if trade is None or str(trade.ladder_role or "") in ("cover", "runner"):
                    return
                signal_ref = trade.signal_ref
                if not signal_ref:
                    trade.runner_mode = MODE_AI_MANAGED
                    return
                rows = await session.execute(
                    select(Trade).where(
                        Trade.signal_ref == signal_ref,
                        Trade.external.is_(False),
                    )
                )
                for member in rows.scalars():
                    if str(member.ladder_role or "") in ("cover", "runner"):
                        continue
                    member.runner_mode = MODE_AI_MANAGED
        except Exception as exc:  # noqa: BLE001 - the stop already moved
            log.warning("classic_managed_flag_failed ticket=%s error=%s", ticket, exc)

    def _channel_levels(self, plan: _Plan) -> list[float]:
        """Channel rungs still on the plan, nearest first, without the computed 1R cover.

        The cover leg's take-profit is money 1R, not a rung the channel printed. The
        runner leg and the unmanaged list are the channel prices. Sorted so a reply that
        was planned against a different entry can be re-run from the real fill.
        """
        raw: list[float] = [float(item) for item in plan.unmanaged_tps if item and float(item) > 0.0]
        for leg in plan.legs:
            if leg.role == "runner" and float(leg.tp or 0.0) > 0.0:
                raw.append(float(leg.tp))
        levels: list[float] = []
        seen: set[float] = set()
        for value in raw:
            if value in seen:
                continue
            seen.add(value)
            levels.append(value)
        levels.sort(reverse=(plan.side == "sell"))
        return levels

    def _runner_price_from_fill(self, plan: _Plan, entry: float) -> float:
        """Formula runner for this fill, advanced past 1R the same way a fresh ladder is."""
        levels = self._channel_levels(plan)
        anchor = float(entry) if entry > 0.0 else float(plan.entry)
        if not levels:
            return pricing.one_r_take_profit(plan.ref, plan.side, anchor, float(plan.sl))
        planned = pricing.plan_ladder_targets(
            plan.ref,
            plan.side,
            anchor,
            float(plan.sl),
            levels,
            bias=int(plan.runner_tp_bias or 0),
            runner_tp_index=plan.chosen_runner_tp_index,
        )
        return float(planned.runner_tp)

    def _chain_target(
        self, plan: _Plan, trade: Trade, *, runner_price: float | None = None
    ) -> float | None:
        """The take-profit to put on one existing leg when a later post supplies levels.

        A ladder cover is money 1R from that ticket's own fill, not from the price the
        reply wanted to open at. The runner is the formula price from that same fill, so
        a pending-style reply cannot park the runner inside the cover. A shorter levels
        update uses the posted targets and does not invent 1R.
        """
        role = str(trade.ladder_role or "")
        entry = float(trade.entry_price or 0.0) or float(plan.entry)
        if plan.kind == "ladder":
            if role == "runner":
                if runner_price is not None and float(runner_price) > 0.0:
                    return float(runner_price)
                return self._runner_price_from_fill(plan, entry)
            return pricing.one_r_take_profit(plan.ref, plan.side, entry, float(plan.sl))
        posted = [float(tp) for tp in plan.tps if tp and float(tp) > 0.0]
        if not posted:
            return None
        if role == "runner":
            return posted[-1]
        if role == "cover":
            return posted[0]
        index = int(trade.split_index or 0)
        return posted[min(index, len(posted) - 1)]

    async def _redirect_incomplete_chain(
        self, ctx: RunContext, plan: _Plan, *, reason: str
    ) -> dict[str, Any] | None:
        """Turn a second open into a modify when the chain already has stopless legs.

        Returns None when this call should go through the normal open path (nothing live
        yet, or the live legs already have a stop). A naked chain plus a posted stop is
        the teaser/ladder race: attaching levels here is what stops a second pair.
        """
        if float(plan.sl or 0.0) <= 0.0 or not ctx.signal_ref:
            return None
        async with session_scope() as session:
            rows = await guards.live_chain_trades(session, ctx.signal_ref)
        ours = [row for row in rows if not row.external]
        if not ours:
            return None
        if not all(float(row.sl or 0.0) <= 0.0 for row in ours):
            return None
        symbols = {str(row.symbol or "").upper() for row in ours}
        sides = {str(row.side or "").lower() for row in ours}
        if symbols != {plan.ref.name.upper()} or sides != {plan.side}:
            return None
        if not ctx.bypass_arm:
            async with session_scope() as session:
                refusal = await guards.check_armed(session)
            if refusal:
                return _refuse(
                    refusal,
                    stage="disarmed",
                    would_have={
                        "action": "modify_existing",
                        "tickets": [int(row.ticket) for row in ours],
                        **plan.summary(),
                    },
                )
        return await self._attach_levels_to_existing(ctx, plan, ours, reason=reason)

    async def _attach_levels_to_existing(
        self,
        ctx: RunContext,
        plan: _Plan,
        trades: list[Trade],
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Write the channel stop and cover/runner targets onto tickets that are already live."""
        ordered = sorted(trades, key=lambda row: (int(row.split_index or 0), int(row.ticket)))
        anchor = next((row for row in ordered if row.ladder_role == "cover"), ordered[0])
        runner_price: float | None = None
        if plan.kind == "ladder":
            runner_price = self._runner_price_from_fill(
                plan, float(anchor.entry_price or 0.0) or float(plan.entry)
            )
        legs_out: list[dict[str, Any]] = []
        applied: list[float] = []
        for trade in ordered:
            ticket = int(trade.ticket)
            target = self._chain_target(plan, trade, runner_price=runner_price)
            kwargs: dict[str, Any] = {
                "ticket": ticket,
                "sl": float(plan.sl),
                "reason": reason or "attach channel levels to the existing leg",
            }
            if target is not None and float(target) > 0.0:
                kwargs["tp"] = float(target)
            if str(trade.kind) == "order":
                adjusted = await self.modify_order(ctx, **kwargs)
            else:
                adjusted = await self.modify_position(ctx, **kwargs)
            ok = bool(adjusted.get("ok"))
            if ok and plan.kind == "ladder":
                await self._update_trade(
                    ticket,
                    runner_mode=plan.runner_mode,
                    suggested_runner_mode=plan.suggested_runner_mode,
                    runner_tp_bias=plan.runner_tp_bias,
                    suggested_runner_tp_index=plan.suggested_runner_tp_index,
                    chosen_runner_tp_index=plan.chosen_runner_tp_index,
                    unmanaged_tps=list(plan.unmanaged_tps),
                )
            new_tp = adjusted.get("tp") if ok else None
            if isinstance(new_tp, (int, float)) and not isinstance(new_tp, bool) and float(new_tp) > 0.0:
                applied.append(float(new_tp))
            legs_out.append(
                {
                    "ticket": ticket,
                    "role": trade.ladder_role,
                    "kind": trade.kind,
                    "ok": ok,
                    "sl": adjusted.get("sl") if ok else float(plan.sl),
                    "tp": new_tp if ok else target,
                    "message": adjusted.get("message"),
                }
            )
        succeeded = [leg for leg in legs_out if leg["ok"]]
        failed = [leg for leg in legs_out if not leg["ok"]]
        tickets = [int(leg["ticket"]) for leg in succeeded]
        if succeeded and not failed:
            headline = (
                f"existing incomplete legs were updated instead of opening a second entry: "
                f"{_join([f'#{ticket}' for ticket in tickets])}. "
                f"Channel stop {_price_text(plan.sl, plan.ref.digits)} is on"
                + (
                    f", targets {_join([_price_text(tp, plan.ref.digits) for tp in applied])}"
                    if applied
                    else ""
                )
                + ". No new order was sent"
            )
        elif succeeded and failed:
            headline = (
                f"PARTIAL: updated {_join([f'#{t}' for t in tickets])}; "
                f"{len(failed)} existing leg(s) could not be modified "
                f"({failed[0]['message']}). No new order was sent"
            )
        else:
            detail = failed[0]["message"] if failed else "no tickets"
            headline = (
                f"the chain already has incomplete legs and they could not be modified ({detail}). "
                f"No second order was sent"
            )
        log.info(
            "chain_levels_attached signal_ref=%s tickets=%s ok=%s kind=%s",
            ctx.signal_ref,
            [int(row.ticket) for row in ordered],
            len(succeeded) == len(legs_out) and bool(legs_out),
            plan.kind,
        )
        return {
            "ok": bool(succeeded) and not failed,
            "action": "modify_existing",
            "modified": True,
            "kind": plan.kind,
            "message": headline,
            "tickets": tickets,
            "ticket": tickets[0] if len(tickets) == 1 else None,
            "symbol": plan.ref.name,
            "side": plan.side,
            "sl": plan.sl,
            "tp": applied,
            "legs": legs_out,
            "failures": [
                {"ticket": leg["ticket"], "message": leg["message"]} for leg in failed
            ],
            "notes": list(plan.notes),
            "runner_mode": plan.runner_mode,
            "signal_ref": ctx.signal_ref,
            "volume": round(sum(float(row.volume or 0.0) for row in ordered), 8),
            "volume_source": plan.volume_source,
        }

    @_serialize_broker_write
    async def open_market(
        self,
        ctx: RunContext,
        *,
        symbol: str,
        side: str,
        sl: Any = None,
        tp: Any = None,
        volume: float | None = None,
        risk_percent: float | None = None,
        reason: str,
        runner_mode: str | None = None,
        runner_tp_index: int | None = None,
    ) -> dict[str, Any]:
        """Open a position at the market, running every §5.3 check in order.

        There is deliberately no ``comment`` parameter: the executor owns that field, because
        §8 recovers the ``run_id`` from it and a model-written comment would break the
        reconciliation tag. ``runner_mode`` is stored on ladder legs and executed by the
        watchdog / reconcile BE trail; ignored on classic 1–3 TP signals.
        ``runner_tp_index`` may shift the suggested numbered-TP index by at most ±1.
        A missing or zero ``sl`` is an incomplete entry: fixed ladder volume, no invented
        stop or target. The call is serial with every other broker write.
        """
        token = _REASON.set(str(reason or ""))
        try:
            plan, refusal = await self._prepare(
                ctx,
                symbol=symbol,
                side=side,
                sl=sl,
                tp=tp,
                volume=volume,
                risk_percent=risk_percent,
                pending=False,
                runner_mode=runner_mode,
                runner_tp_index=runner_tp_index,
            )
            if refusal is not None:
                return refusal
            assert plan is not None
            redirected = await self._redirect_incomplete_chain(
                ctx, plan, reason=str(reason or "")
            )
            if redirected is not None:
                return redirected
            refusal = await self._entry_open_gate(ctx, plan)
            if refusal is not None:
                return refusal
            refusal = await self._gate(ctx, plan)
            if refusal is not None:
                return refusal
            refusal = await self._check_margin(plan)
            if refusal is not None:
                return refusal
            return await self._execute_plan(ctx, plan, reason=str(reason or ""))
        finally:
            _REASON.reset(token)

    @_serialize_broker_write
    async def place_pending(
        self,
        ctx: RunContext,
        *,
        symbol: str,
        side: str,
        order_type: str,
        price: float,
        sl: Any = None,
        tp: Any = None,
        expiry_minutes: int | None = None,
        volume: float | None = None,
        risk_percent: float | None = None,
        reason: str,
        runner_mode: str | None = None,
        runner_tp_index: int | None = None,
    ) -> dict[str, Any]:
        """Place a pending order, inferring BUY/SELL × LIMIT/STOP from side, type and price.

        Success here is retcode ``10008`` — *placed*, not *done*. Nothing is on the book until
        price arrives, and treating ``10008`` as a failure is what makes a system place the
        same order twice (§1.3).
        """
        token = _REASON.set(str(reason or ""))
        try:
            plan, refusal = await self._prepare(
                ctx,
                symbol=symbol,
                side=side,
                sl=sl,
                tp=tp,
                volume=volume,
                risk_percent=risk_percent,
                pending=True,
                order_type=order_type,
                price=price,
                expiry_minutes=expiry_minutes,
                runner_mode=runner_mode,
                runner_tp_index=runner_tp_index,
            )
            if refusal is not None:
                return refusal
            assert plan is not None
            redirected = await self._redirect_incomplete_chain(
                ctx, plan, reason=str(reason or "")
            )
            if redirected is not None:
                return redirected
            refusal = await self._entry_open_gate(ctx, plan)
            if refusal is not None:
                return refusal
            refusal = await self._gate(ctx, plan)
            if refusal is not None:
                return refusal
            refusal = await self._check_margin(plan)
            if refusal is not None:
                return refusal
            return await self._execute_plan(ctx, plan, reason=str(reason or ""))
        finally:
            _REASON.reset(token)

    async def _entry_open_gate(self, ctx: RunContext, plan: _Plan) -> dict[str, Any] | None:
        """Refuse a new open that is far from the printed entry or older than 10 minutes.

        Runs only after ``_redirect_incomplete_chain``. A ladder that attaches levels to
        tickets already on the book is a modify, not an open, and does not reach here.
        The live tick on the plan is the price that is checked. A fresh cache is used
        only when that tick is missing; if neither can prove the fill is inside the band,
        the open is refused.
        """
        from app.trading.exposure import chain_exposure_closed

        if await chain_exposure_closed(ctx.signal_ref):
            log.info("entry_gate_skip reason=exposure_closed signal_ref=%s", ctx.signal_ref)
            return _refuse(
                "refusing open: the channel deleted this signal or posted a full close. "
                "No new order was sent.",
                stage="exposure_closed",
                reason="exposure_closed",
                plan=plan.summary(),
            )
        metal_distance: float | None = None
        if plan.kind != "ladder":
            try:
                async with session_scope() as session:
                    metal_distance = await max_entry_drift_usd(session)
            except Exception as exc:  # noqa: BLE001 - a missing knob still has a default
                log.warning("entry_drift_read_failed error=%s", exc)
                metal_distance = float(MAX_ENTRY_DRIFT_USD_DEFAULT)
        verdict = await check_open(
            origin=ctx.origin,
            bypass_arm=bool(ctx.bypass_arm),
            message_row_id=ctx.message_row_id,
            signal_ref=ctx.signal_ref,
            symbol_name=plan.ref.name,
            pip_size=float(plan.ref.pip_size),
            pip_convention=str(plan.ref.pip_convention or ""),
            side=plan.side,
            bid=float(plan.quote.bid),
            ask=float(plan.quote.ask),
            metal_distance=metal_distance,
        )
        if verdict.allow:
            return None
        log.info(
            "entry_gate_skip reason=%s symbol=%s side=%s entry=%s fill=%s distance=%s "
            "limit=%s age=%s price_source=%s signal_ref=%s",
            verdict.reason,
            plan.ref.name,
            plan.side,
            verdict.entry,
            verdict.fill,
            verdict.distance,
            verdict.limit,
            verdict.age_seconds,
            verdict.price_source,
            ctx.signal_ref,
        )
        return _refuse(
            verdict.message,
            stage="entry_gate",
            reason=verdict.reason,
            distance=verdict.distance,
            limit=verdict.limit,
            entry=verdict.entry,
            fill=verdict.fill,
            age_seconds=None if verdict.age_seconds is None else round(float(verdict.age_seconds), 3),
            price_source=verdict.price_source or None,
            plan=plan.summary(),
        )

    # ==================================================================================
    # MANAGING WHAT IS ALREADY OPEN (BUILD_PROMPT §5.3, §6.2)
    # ==================================================================================

    async def _find_position(self, ticket: Any) -> tuple[Any | None, dict[str, Any] | None]:
        """Fetch one open position by ticket, refusing external ones by name."""
        number = _as_int(ticket)
        if number is None:
            return None, _refuse(f"ticket must be an integer, got {ticket!r}")
        rows = _rows(await self.rpc.positions_get(ticket=number))
        if not rows:
            return None, _refuse(
                f"there is no open position with ticket #{number} — call get_positions for the "
                f"live list; it may already have been closed by its stop or target"
            )
        position = rows[0]
        magic = int(self.settings.MAGIC_NUMBER)
        if int(position.magic) != magic:
            return None, _refuse(
                f"position #{number} on {position.symbol} carries magic {int(position.magic)}, "
                f"not this system's {magic} — it belongs to another system or to the operator's "
                f"own hand and is read-only here",
                external=True,
                ticket=number,
            )
        return position, None

    async def _find_order(self, ticket: Any) -> tuple[Any | None, dict[str, Any] | None]:
        """Fetch one pending order by ticket, refusing external ones by name."""
        number = _as_int(ticket)
        if number is None:
            return None, _refuse(f"ticket must be an integer, got {ticket!r}")
        rows = _rows(await self.rpc.orders_get(ticket=number))
        if not rows:
            return None, _refuse(
                f"there is no pending order with ticket #{number} — call get_orders for the live "
                f"list; it may already have filled or been cancelled"
            )
        order = rows[0]
        magic = int(self.settings.MAGIC_NUMBER)
        if int(order.magic) != magic:
            return None, _refuse(
                f"order #{number} on {order.symbol} carries magic {int(order.magic)}, not this "
                f"system's {magic} — it belongs to another system and is read-only here",
                external=True,
                ticket=number,
            )
        return order, None

    async def _update_trade(self, ticket: int, **fields: Any) -> None:
        """Patch the Trade row for a ticket, if we have one; never raises."""
        try:
            async with session_scope() as session:
                row = await session.execute(select(Trade).where(Trade.ticket == int(ticket)))
                trade = row.scalar_one_or_none()
                if trade is None:
                    return
                for key, value in fields.items():
                    setattr(trade, key, value)
                trade.updated_at = utcnow()
        except Exception as exc:  # noqa: BLE001 - the broker already acted; log and move on
            log.error("trade_row_update_failed ticket=%s error=%s", ticket, exc)

    async def _modify_position_core(
        self,
        ctx: RunContext,
        *,
        ticket: Any,
        sl: Any,
        tp: Any,
        reason: str,
        notify_type: str,
        subject: str | None = None,
        notify: bool = True,
    ) -> dict[str, Any]:
        """Move a position's stop or target — the body behind modify_position and the SL fallback.

        ``sl=None`` means "leave the stop alone" and ``sl=0`` means "remove it", which §6.2
        refuses outright: without that distinction the model could satisfy §6.1 at open time
        and clear the stop a second later, legally. The field that is *not* being changed is
        re-sent at its current value — sending 0 there would clear it.

        The stop is validated against the **market**, not against the entry: moving a stop to
        break-even or into profit is the whole point of this tool, and it is a wrong-side
        stop only if it has crossed the price.
        """
        refusal = guards.check_sl_not_removed(_as_float(sl) if sl is not None else None)
        if refusal:
            return _refuse(refusal, stage="sl_not_removable", ticket=_as_int(ticket))

        position, refusal_dict = await self._find_position(ticket)
        if refusal_dict is not None:
            return refusal_dict
        assert position is not None

        number = int(position.ticket)
        side = POSITION_TYPE_NAMES.get(int(position.type), "buy")
        ref = await self.resolver.get(str(position.symbol))
        quote = await self.resolver.quote(ref.name)
        digits = ref.digits

        current_sl = float(position.sl)
        current_tp = float(position.tp)
        new_sl = current_sl
        new_tp = current_tp
        notes: list[str] = []

        if sl is not None:
            requested = _as_float(sl)
            if requested is None:
                return _refuse(f"sl must be a number, got {sl!r}", ticket=number)
            new_sl = pricing.round_stop_away(ref, requested, side, "sl")
            if abs(new_sl - requested) > 0.0:
                notes.append(
                    f"the stop {_price_text(requested, digits)} was rounded away from the market "
                    f"to {_price_text(new_sl, digits)}"
                )
        if tp is not None:
            requested_tp = _as_float(tp)
            if requested_tp is None:
                return _refuse(f"tp must be a number, got {tp!r}", ticket=number)
            # 0 is how MT5 spells "no target", and a target is optional (§5.3), so it passes.
            new_tp = (
                0.0
                if requested_tp <= 0.0
                else pricing.round_stop_away(ref, requested_tp, side, "tp")
            )

        if abs(new_sl - current_sl) < 1e-12 and abs(new_tp - current_tp) < 1e-12:
            return _refuse(
                f"position #{number} already has sl {_price_text(current_sl, digits)} and tp "
                f"{_price_text(current_tp, digits)} — nothing would change",
                ticket=number,
            )

        market = quote.bid if side == "buy" else quote.ask
        if new_sl:
            wrong_side = (side == "buy" and new_sl >= market) or (
                side == "sell" and new_sl <= market
            )
            if wrong_side:
                return _refuse(
                    (
                        f"a {side}'s stop must sit "
                        f"{'below' if side == 'buy' else 'above'} the market: "
                        f"{_price_text(new_sl, digits)} is "
                        f"{'above' if side == 'buy' else 'below'} the current "
                        f"{'bid' if side == 'buy' else 'ask'} "
                        f"{_price_text(market, digits)} — the position would be closed instantly. "
                        f"Close it with close_position if that is what you meant"
                    ),
                    ticket=number,
                    stage="sl_side",
                )
        # An SL-only change (tp is None) must not be blocked by an existing take-profit that
        # price has already approached or passed. That is the ladder BE-trail case: the
        # runner TP stays on the ticket, but the stop still has to lock to break-even.
        tp_for_validation = new_tp if tp is not None else 0.0
        refusal = guards.check_stops_level(
            ref, side, market, new_sl, tp_for_validation, quote.bid, quote.ask, pending=False
        )
        if refusal:
            return _refuse(refusal, ticket=number, stage="stops_level")

        if not ctx.bypass_arm:
            async with session_scope() as session:
                refusal = await guards.check_armed(session)
                if refusal:
                    return _refuse(
                        refusal,
                        stage="disarmed",
                        would_have={
                            "ticket": number,
                            "symbol": ref.name,
                            "side": side,
                            "sl": new_sl,
                            "tp": new_tp,
                            "old_sl": current_sl,
                            "old_tp": current_tp,
                            "reason": reason,
                        },
                    )

        request: dict[str, Any] = {
            "action": TRADE_ACTION_SLTP,
            "symbol": ref.name,
            "position": number,
            "sl": new_sl,
            "tp": new_tp,
        }

        check = await self.rpc.order_check(request)
        if check is not None and int(check.retcode) != CHECK_OK_RETCODE:
            message = (
                f"{retcode_message(int(check.retcode), str(check.comment))} — order_check "
                f"rejected the change before it was sent"
            )
            await self._audit(
                ctx,
                type="reject",
                ticket=number,
                detail={
                    "stage": "order_check",
                    "symbol": ref.name,
                    "sl": new_sl,
                    "tp": new_tp,
                    "reason": reason,
                    "message": message,
                },
                retcode=int(check.retcode),
            )
            self._notify_tool_refusal(
                "modify_position", {"symbol": ref.name, "ticket": number,
                                  "retcode": int(check.retcode)}, ctx, message
            )
            return _refuse(message, retcode=int(check.retcode), ticket=number)

        token = _REASON.set(str(reason or ""))
        try:
            result = await self.send_order(request, ctx=ctx, expect_pending=False)
        finally:
            _REASON.reset(token)

        link = await self._permalink_for(ctx)
        if not result.ok:
            text = self._render(
                "render_rejected",
                symbol=ref.name,
                side=side,
                action="modify_position",
                ticket=number,
                retcode=result.retcode,
                retcode_name=result.retcode_name,
                message=result.message,
                signal_ref=ctx.signal_ref,
                reason=reason,
            )
            if text:
                self._notify("rejected", text, link=link)
            return _refuse(
                result.message,
                ticket=number,
                retcode=result.retcode,
                retcode_name=result.retcode_name,
                symbol=ref.name,
            )

        await self._update_trade(number, sl=new_sl, tp=new_tp)
        if (
            sl is not None
            and abs(new_sl - current_sl) > 1e-12
            and str(ctx.origin or "") != "watchdog"
        ):
            await self._mark_classic_managed(number)
        changes: list[str] = []
        if abs(new_sl - current_sl) > 1e-12:
            changes.append(
                f"sl {_price_text(current_sl, digits)} -> {_price_text(new_sl, digits)}"
            )
        if abs(new_tp - current_tp) > 1e-12:
            changes.append(
                f"tp {_price_text(current_tp, digits)} -> {_price_text(new_tp, digits)}"
            )

        if notify_type == "attention":
            text = self._render(
                "render_attention",
                subject=subject or "sl_fallback",
                symbol=ref.name,
                ticket=number,
                detail=f"{_join(changes)} on the {side} of {_lots(float(position.volume))} lots",
                reason=reason,
            )
        else:
            text = self._render(
                "render_modified",
                symbol=ref.name,
                side=side,
                ticket=number,
                changes={key: (old, new) for key, old, new in
                         (("sl", current_sl, new_sl), ("tp", current_tp, new_tp))
                         if abs(new - old) > 1e-12},
                breakeven=(abs(new_sl-current_sl) > 1e-12 and
                           pricing.stop_is_at_or_beyond_breakeven(side, float(position.price_open), new_sl)),
                signal_ref=ctx.signal_ref,
                reason=reason,
                digits=digits,
            )
        if text and notify and reason != "ladder cover 1R from fill":
            self._notify(notify_type, text, link=link)

        return {
            "ok": True,
            "ticket": number,
            "symbol": ref.name,
            "side": side,
            "sl": new_sl,
            "tp": new_tp,
            "old_sl": current_sl,
            "old_tp": current_tp,
            "changed": changes,
            "retcode": result.retcode,
            "retcode_name": result.retcode_name,
            "distance_to_sl": self._distance_row(ref, new_sl, market),
            "distance_to_tp": self._distance_row(ref, new_tp, market),
            "digits": digits,
            "message": (
                f"position #{number} on {ref.name} updated: {_join(changes)}"
                + (f". {'. '.join(notes)}" if notes else "")
            ),
        }

    @_serialize_broker_write
    async def modify_position(
        self,
        ctx: RunContext,
        *,
        ticket: int,
        sl: float | None = None,
        tp: float | None = None,
        reason: str,
        notify: bool = True,
    ) -> dict[str, Any]:
        """Move an open position's stop or target with TRADE_ACTION_SLTP (§5.3)."""
        return await self._modify_position_core(
            ctx, ticket=ticket, sl=sl, tp=tp, reason=reason, notify_type="modified", notify=notify
        )

    @_serialize_broker_write
    async def modify_order(
        self,
        ctx: RunContext,
        *,
        ticket: int,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        reason: str,
    ) -> dict[str, Any]:
        """Change a pending order's price, stop or target with TRADE_ACTION_MODIFY.

        Every field the caller did not name is re-sent at its current value — including
        ``type_time`` and ``expiration``, which a broker will otherwise reset to
        good-till-cancelled. ``sl == 0`` is refused here exactly as it is on a position (§6.2).
        """
        refusal = guards.check_sl_not_removed(_as_float(sl) if sl is not None else None)
        if refusal:
            return _refuse(refusal, stage="sl_not_removable", ticket=_as_int(ticket))

        order, refusal_dict = await self._find_order(ticket)
        if refusal_dict is not None:
            return refusal_dict
        assert order is not None

        number = int(order.ticket)
        order_type = int(order.type)
        side = _side_of_order_type(order_type)
        kind = (
            "limit"
            if order_type in (ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT)
            else "stop"
        )
        ref = await self.resolver.get(str(order.symbol))
        quote = await self.resolver.quote(ref.name)
        digits = ref.digits

        current_price = float(order.price_open)
        current_sl = float(order.sl)
        current_tp = float(order.tp)
        new_price = current_price
        new_sl = current_sl
        new_tp = current_tp
        notes: list[str] = []

        if price is not None:
            requested = _as_float(price)
            if requested is None or requested <= 0.0:
                return _refuse(f"price must be a positive price, got {price!r}", ticket=number)
            new_price = pricing.round_price(ref, requested)
            inferred, explanation = pricing.infer_pending_type(
                side, kind, new_price, quote.bid, quote.ask
            )
            if inferred is None:
                return _refuse(explanation, ticket=number, stage="pending_type")
            if inferred != order_type:
                return _refuse(
                    (
                        f"order #{number} is a {ORDER_TYPE_NAMES.get(order_type, order_type)} and "
                        f"{_price_text(new_price, digits)} would make it a "
                        f"{ORDER_TYPE_NAMES.get(inferred, inferred)} — MT5 cannot change one into "
                        f"the other. Cancel this order and place a new one at that price"
                    ),
                    ticket=number,
                    stage="pending_type",
                )
            notes.append(explanation)
        if sl is not None:
            requested_sl = _as_float(sl)
            if requested_sl is None:
                return _refuse(f"sl must be a number, got {sl!r}", ticket=number)
            new_sl = pricing.round_stop_away(ref, requested_sl, side, "sl")
        if tp is not None:
            requested_tp = _as_float(tp)
            if requested_tp is None:
                return _refuse(f"tp must be a number, got {tp!r}", ticket=number)
            new_tp = (
                0.0
                if requested_tp <= 0.0
                else pricing.round_stop_away(ref, requested_tp, side, "tp")
            )

        if (
            abs(new_price - current_price) < 1e-12
            and abs(new_sl - current_sl) < 1e-12
            and abs(new_tp - current_tp) < 1e-12
        ):
            return _refuse(
                f"order #{number} already sits at {_price_text(current_price, digits)} with sl "
                f"{_price_text(current_sl, digits)} and tp {_price_text(current_tp, digits)} — "
                f"nothing would change",
                ticket=number,
            )

        if new_sl:
            refusal = guards.check_sl_side(side, new_price, new_sl)
            if refusal:
                return _refuse(refusal, ticket=number, stage="sl_side")
        refusal = guards.check_stops_level(
            ref, side, new_price, new_sl, new_tp, quote.bid, quote.ask, pending=True
        )
        if refusal:
            return _refuse(refusal, ticket=number, stage="stops_level")

        if not ctx.bypass_arm:
            async with session_scope() as session:
                refusal = await guards.check_armed(session)
                if refusal:
                    return _refuse(
                        refusal,
                        stage="disarmed",
                        would_have={
                            "ticket": number,
                            "symbol": ref.name,
                            "side": side,
                            "price": new_price,
                            "sl": new_sl,
                            "tp": new_tp,
                            "old_price": current_price,
                            "old_sl": current_sl,
                            "old_tp": current_tp,
                            "reason": reason,
                        },
                    )

        expiration = float(order.time_expiration)
        request: dict[str, Any] = {
            "action": TRADE_ACTION_MODIFY,
            "symbol": ref.name,
            "order": number,
            "price": new_price,
            "sl": new_sl,
            "tp": new_tp,
            "type_time": int(order.type_time),
            "expiration": int(expiration),
        }

        check = await self.rpc.order_check(request)
        if check is not None and int(check.retcode) != CHECK_OK_RETCODE:
            message = (
                f"{retcode_message(int(check.retcode), str(check.comment))} — order_check "
                f"rejected the change before it was sent"
            )
            await self._audit(
                ctx,
                type="reject",
                ticket=number,
                detail={"stage": "order_check", "symbol": ref.name, "message": message},
                retcode=int(check.retcode),
            )
            self._notify_tool_refusal(
                "modify_order", {"symbol": ref.name, "ticket": number,
                                  "retcode": int(check.retcode)}, ctx, message
            )
            return _refuse(message, retcode=int(check.retcode), ticket=number)

        token = _REASON.set(str(reason or ""))
        try:
            result = await self.send_order(request, ctx=ctx, expect_pending=True)
        finally:
            _REASON.reset(token)

        link = await self._permalink_for(ctx)
        if not result.ok:
            text = self._render(
                "render_rejected",
                symbol=ref.name,
                side=side,
                action="modify_order",
                ticket=number,
                retcode=result.retcode,
                retcode_name=result.retcode_name,
                message=result.message,
                signal_ref=ctx.signal_ref,
                reason=reason,
            )
            if text:
                self._notify("rejected", text, link=link)
            return _refuse(
                result.message,
                ticket=number,
                retcode=result.retcode,
                retcode_name=result.retcode_name,
                symbol=ref.name,
            )

        await self._update_trade(number, entry_price=new_price, sl=new_sl, tp=new_tp)
        changes: list[str] = []
        if abs(new_price - current_price) > 1e-12:
            changes.append(
                f"price {_price_text(current_price, digits)} -> {_price_text(new_price, digits)}"
            )
        if abs(new_sl - current_sl) > 1e-12:
            changes.append(f"sl {_price_text(current_sl, digits)} -> {_price_text(new_sl, digits)}")
        if abs(new_tp - current_tp) > 1e-12:
            changes.append(f"tp {_price_text(current_tp, digits)} -> {_price_text(new_tp, digits)}")

        text = self._render(
            "render_modified",
            symbol=ref.name,
            side=side,
            ticket=number,
            old_price=current_price,
            new_price=new_price,
            old_sl=current_sl,
            new_sl=new_sl,
            old_tp=current_tp,
            new_tp=new_tp,
            signal_ref=ctx.signal_ref,
            reason=reason,
            digits=digits,
        )
        if text:
            self._notify("modified", text, link=link)

        return {
            "ok": True,
            "ticket": number,
            "symbol": ref.name,
            "side": side,
            "order_type": ORDER_TYPE_NAMES.get(order_type, str(order_type)),
            "price": new_price,
            "sl": new_sl,
            "tp": new_tp,
            "old_price": current_price,
            "old_sl": current_sl,
            "old_tp": current_tp,
            "changed": changes,
            "retcode": result.retcode,
            "retcode_name": result.retcode_name,
            "digits": digits,
            "message": (
                f"order #{number} on {ref.name} updated: {_join(changes)}"
                + (f". {'. '.join(notes)}" if notes else "")
            ),
        }

    async def _realized_for_position(self, ticket: int) -> tuple[float | None, int]:
        """Sum the closing deals of one position — the realized P/L the operator is told."""
        try:
            deals = _rows(
                await self.rpc.history_deals_get(
                    0.0, time.time() + HISTORY_FUTURE_PAD, position=int(ticket)
                )
            )
        except SignalPilotError as exc:
            log.warning("history_deals_failed ticket=%s error=%s", ticket, exc)
            return None, 0
        total = 0.0
        count = 0
        for deal in deals:
            if int(deal.entry) not in REALIZING_DEAL_ENTRIES:
                continue
            total += (
                float(deal.profit) + float(deal.commission) + float(deal.swap) + float(deal.fee)
            )
            count += 1
        if count == 0:
            return None, 0
        return round(total, 2), count

    @_serialize_broker_write
    async def close_position(
        self,
        ctx: RunContext,
        *,
        ticket: int,
        volume: float | None = None,
        reason: str,
        cause: str | None = None,
    ) -> dict[str, Any]:
        """Close a position in full or in part.

        There is no close action in MT5: this is ``TRADE_ACTION_DEAL`` with the **opposite**
        type, the closing side's price and — the field that matters — ``position``. Omit it and
        on a hedging account the "close" silently becomes a new opposite position and exposure
        doubles with no error anywhere (§1.3).

        A partial close is the same request with a smaller volume, and its filling mode must
        allow partial fills: FOK is all-or-nothing, so ``pick_filling(partial=True)`` prefers
        IOC. Over-sending gives ``10038``.
        """
        position, refusal_dict = await self._find_position(ticket)
        if refusal_dict is not None:
            return refusal_dict
        assert position is not None

        number = int(position.ticket)
        side = POSITION_TYPE_NAMES.get(int(position.type), "buy")
        open_volume = float(position.volume)
        entry_price = float(position.price_open)
        ref = await self.resolver.get(str(position.symbol))
        quote = await self.resolver.quote(ref.name)
        digits = ref.digits
        notes: list[str] = []

        close_volume = open_volume
        if volume is not None:
            requested = _as_float(volume)
            if requested is None or requested <= 0.0:
                return _refuse(
                    f"volume must be a positive number of lots, got {volume!r}", ticket=number
                )
            step = float(ref.volume_step)
            close_volume = (
                round(math.floor(requested / step + VOLUME_EPS) * step, 8)
                if step > 0.0
                else requested
            )
            if abs(close_volume - requested) > VOLUME_EPS:
                notes.append(
                    f"{_lots(requested)} lots is not a multiple of the {_lots(step)} lot step; "
                    f"rounded down to {_lots(close_volume)}"
                )
            if close_volume > open_volume + VOLUME_EPS:
                return _refuse(
                    (
                        f"position #{number} holds {_lots(open_volume)} lots and cannot close "
                        f"{_lots(requested)} — over-sending is what the broker answers with "
                        f"10038. Omit volume to close all of it"
                    ),
                    ticket=number,
                )
            if close_volume < float(ref.volume_min) - VOLUME_EPS:
                return _refuse(
                    (
                        f"{_lots(requested)} lots is below the {_lots(ref.volume_min)} minimum on "
                        f"{ref.name}, so it cannot be closed as a partial — close the whole "
                        f"{_lots(open_volume)} lots instead"
                    ),
                    ticket=number,
                )
            remainder = round(open_volume - close_volume, 8)
            if VOLUME_EPS < remainder < float(ref.volume_min) - VOLUME_EPS:
                return _refuse(
                    (
                        f"closing {_lots(close_volume)} of {_lots(open_volume)} lots would leave "
                        f"{_lots(remainder)} lots open, below the {_lots(ref.volume_min)} minimum "
                        f"on {ref.name}, and the broker rejects that with 10014 — close at most "
                        f"{_lots(round(open_volume - float(ref.volume_min), 8))} lots, or close "
                        f"the whole position"
                    ),
                    ticket=number,
                )

        partial = close_volume < open_volume - VOLUME_EPS
        close_type = ORDER_TYPE_SELL if side == "buy" else ORDER_TYPE_BUY
        close_price = quote.bid if side == "buy" else quote.ask

        if not ctx.bypass_arm:
            async with session_scope() as session:
                refusal = await guards.check_armed(session)
                if refusal:
                    return _refuse(
                        refusal,
                        stage="disarmed",
                        would_have={
                            "ticket": number,
                            "symbol": ref.name,
                            "side": side,
                            "close_volume": close_volume,
                            "open_volume": open_volume,
                            "partial": partial,
                            "price": close_price,
                            "reason": reason,
                        },
                    )

        try:
            filling = pricing.pick_filling(ref, TRADE_ACTION_DEAL, partial=partial)
        except TradingError as exc:
            return _refuse(exc.message, ticket=number, stage="filling_mode")

        request: dict[str, Any] = {
            "action": TRADE_ACTION_DEAL,
            "symbol": ref.name,
            "volume": close_volume,
            "type": close_type,
            "position": number,
            "price": close_price,
            "deviation": int(self.settings.DEVIATION_POINTS),
            "magic": int(self.settings.MAGIC_NUMBER),
            "comment": make_comment(ctx.run_id),
            "type_time": ORDER_TIME_GTC,
            "type_filling": filling,
        }

        check = await self.rpc.order_check(request)
        if check is not None and int(check.retcode) != CHECK_OK_RETCODE:
            message = (
                f"{retcode_message(int(check.retcode), str(check.comment))} — order_check "
                f"rejected the close before it was sent"
            )
            await self._audit(
                ctx,
                type="reject",
                ticket=number,
                detail={
                    "stage": "order_check",
                    "symbol": ref.name,
                    "volume": close_volume,
                    "reason": reason,
                    "message": message,
                },
                retcode=int(check.retcode),
            )
            self._notify_tool_refusal(
                "close_position", {"symbol": ref.name, "ticket": number,
                                  "retcode": int(check.retcode)}, ctx, message
            )
            return _refuse(message, retcode=int(check.retcode), ticket=number)

        token = _REASON.set(str(reason or ""))
        try:
            result = await self.send_order(request, ctx=ctx, expect_pending=False)
        finally:
            _REASON.reset(token)

        link = await self._permalink_for(ctx)
        if not result.ok:
            text = self._render(
                "render_rejected",
                symbol=ref.name,
                side=side,
                action="close_position",
                ticket=number,
                retcode=result.retcode,
                retcode_name=result.retcode_name,
                message=result.message,
                signal_ref=ctx.signal_ref,
                reason=reason,
            )
            if text:
                self._notify("rejected", text, link=link)
            return _refuse(
                result.message,
                ticket=number,
                symbol=ref.name,
                retcode=result.retcode,
                retcode_name=result.retcode_name,
            )

        filled = result.volume or close_volume
        fill_price = result.price or close_price
        moved = (fill_price - entry_price) if side == "buy" else (entry_price - fill_price)
        profit_points = round(pricing.points(ref, moved), 1)
        realized, deal_count = await self._realized_for_position(number)
        cause = _recorded_cause(ctx, cause)

        if partial:
            await self._update_trade(
                number,
                volume=round(open_volume - filled, 8),
                profit_points=profit_points,
            )
        else:
            await self._update_trade(
                number,
                status="closed",
                close_cause=cause,
                close_price=fill_price,
                profit=realized,
                profit_points=profit_points,
                closed_at=utcnow(),
                volume=filled,
            )
            await self._maybe_record_ladder_outcome(ctx.signal_ref, ticket=number)

        currency = str((await self._account_info()).currency)
        text = self._render(
            "render_closed",
            symbol=ref.name,
            side=side,
            ticket=number,
            close_cause=cause,
            volume=filled,
            entry=entry_price,
            close_price=fill_price,
            profit=realized,
            profit_points=profit_points,
            currency=currency,
            signal_ref=ctx.signal_ref,
            reason=reason,
            digits=digits,
        )
        if text:
            self._notify("closed", text, link=link)

        message = (
            f"{'partially closed' if partial else 'closed'} #{number} on {ref.name}: "
            f"{_lots(filled)} of {_lots(open_volume)} lots at "
            f"{_price_text(fill_price, digits)} against an entry of "
            f"{_price_text(entry_price, digits)} ({_trim(profit_points)} points)"
        )
        if realized is not None:
            message += f", realized {_money(realized, currency)} across {deal_count} deal(s)"
        if partial:
            message += (
                f"; {_lots(round(open_volume - filled, 8))} lots remain open with their stop"
            )
        if notes:
            message += ". " + ". ".join(notes)

        return {
            "ok": True,
            "ticket": number,
            "symbol": ref.name,
            "side": side,
            "partial": partial,
            "filled_volume": filled,
            "remaining_volume": round(open_volume - filled, 8) if partial else 0.0,
            "fill_price": fill_price,
            "close_price": fill_price,
            "entry_price": entry_price,
            "slippage_points": result.slippage_points,
            "profit": realized,
            "profit_points": profit_points,
            "close_cause": cause,
            "currency": currency,
            "retcode": result.retcode,
            "retcode_name": result.retcode_name,
            "digits": digits,
            "message": message,
        }

    @_serialize_broker_write
    async def cancel_order(
        self, ctx: RunContext, *, ticket: int, reason: str, cause: str | None = None
    ) -> dict[str, Any]:
        """Remove a pending order from the book with TRADE_ACTION_REMOVE."""
        order, refusal_dict = await self._find_order(ticket)
        if refusal_dict is not None:
            return refusal_dict
        assert order is not None

        number = int(order.ticket)
        symbol = str(order.symbol)
        order_type = int(order.type)
        side = _side_of_order_type(order_type)
        price = float(order.price_open)
        order_volume = float(order.volume_current)

        if not ctx.bypass_arm:
            async with session_scope() as session:
                refusal = await guards.check_armed(session)
                if refusal:
                    return _refuse(
                        refusal,
                        stage="disarmed",
                        would_have={
                            "ticket": number,
                            "symbol": symbol,
                            "order_type": ORDER_TYPE_NAMES.get(order_type, str(order_type)),
                            "price": price,
                            "reason": reason,
                        },
                    )

        request: dict[str, Any] = {
            "action": TRADE_ACTION_REMOVE,
            "symbol": symbol,
            "order": number,
        }

        token = _REASON.set(str(reason or ""))
        try:
            result = await self.send_order(request, ctx=ctx, expect_pending=True)
        finally:
            _REASON.reset(token)

        link = await self._permalink_for(ctx)
        if not result.ok:
            text = self._render(
                "render_rejected",
                symbol=symbol,
                side=side,
                action="cancel_order",
                ticket=number,
                retcode=result.retcode,
                retcode_name=result.retcode_name,
                message=result.message,
                signal_ref=ctx.signal_ref,
                reason=reason,
            )
            if text:
                self._notify("rejected", text, link=link)
            return _refuse(
                result.message,
                ticket=number,
                symbol=symbol,
                retcode=result.retcode,
                retcode_name=result.retcode_name,
            )

        cause = _recorded_cause(ctx, cause)
        await self._update_trade(
            number, status="cancelled", close_cause=cause, closed_at=utcnow()
        )
        text = self._render(
            "render_closed",
            symbol=symbol,
            side=side,
            ticket=number,
            close_cause=cause,
            volume=order_volume,
            entry=price,
            close_price=price,
            signal_ref=ctx.signal_ref,
            reason=reason,
        )
        if text:
            self._notify("closed", text, link=link)

        return {
            "ok": True,
            "ticket": number,
            "symbol": symbol,
            "side": side,
            "order_type": ORDER_TYPE_NAMES.get(order_type, str(order_type)),
            "price": price,
            "volume": order_volume,
            "retcode": result.retcode,
            "retcode_name": result.retcode_name,
            "message": (
                f"pending order #{number} on {symbol} "
                f"({ORDER_TYPE_NAMES.get(order_type, order_type)} {_lots(order_volume)} lots at "
                f"{price}) was cancelled; nothing was opened by it"
            ),
        }

    async def notify(self, ctx: RunContext, *, text: str) -> dict[str, Any]:
        """Push a note to the operator's Telegram bot. Always succeeds, by design.

        A notification failure must never abort or delay a trade (§7), so this reports
        ``ok: true`` once the message has been handed to the background sender. Whether
        Telegram accepted it is the notifier's problem and lands as a ``system.warning``.
        """
        body = str(text or "").strip()
        if not body:
            return {"ok": True, "delivered": False, "message": "there was nothing to send"}
        link = await self._permalink_for(ctx)
        self._notify("attention", body, link=link)
        return {
            "ok": True,
            "delivered": self.notifier is not None,
            "message": (
                "the note was handed to the operator's Telegram bot"
                if self.notifier is not None
                else "no notifier is configured, so the note was recorded but not sent"
            ),
        }

    # ==================================================================================
    # CONTROL: the kill switch and the SL fallback (BUILD_PROMPT §5.5, §6.4)
    # ==================================================================================

    @_serialize_broker_write
    async def kill_switch(self, *, reason: str) -> dict[str, Any]:
        """Flatten everything this system opened, then disarm — in that order.

        The order is the whole point (§5.5): disarming first would make the kill switch's own
        closes return "System is disarmed" and the kill switch would do nothing. It runs with
        ``bypass_arm=True`` so it works while already disarmed, and it touches **only** tickets
        carrying ``MAGIC_NUMBER`` — a position someone else opened is never closed from here.
        """
        ctx = RunContext(
            run_id=uuid.uuid4().hex,
            signal_ref=None,
            source_id=None,
            message_row_id=None,
            origin="kill_switch",
            bypass_arm=True,
        )
        note = str(reason or "kill switch")
        magic = int(self.settings.MAGIC_NUMBER)
        log.warning("kill_switch_started run_id=%s reason=%s", ctx.run_id, note)

        positions = _rows(await self.rpc.positions_get())
        orders = _rows(await self.rpc.orders_get())
        ours_positions = [p for p in positions if int(p.magic) == magic]
        ours_orders = [o for o in orders if int(o.magic) == magic]
        untouched = (len(positions) - len(ours_positions)) + (len(orders) - len(ours_orders))

        outcomes: list[dict[str, Any]] = []
        closed = 0
        cancelled = 0
        failures: list[str] = []

        for position in ours_positions:
            ticket = int(position.ticket)
            try:
                result = await self.close_position(
                    ctx, ticket=ticket, reason=f"kill switch: {note}"
                )
            except SignalPilotError as exc:
                result = _refuse(exc.message)
            except Exception as exc:  # noqa: BLE001 - one bad ticket must not strand the rest
                log.exception("kill_switch_close_failed ticket=%d", ticket)
                result = _refuse(f"unexpected failure closing #{ticket}: {exc}")
            ok = bool(result.get("ok"))
            closed += 1 if ok else 0
            if not ok:
                failures.append(f"#{ticket}: {result.get('message')}")
            outcomes.append(
                {
                    "ticket": ticket,
                    "kind": "position",
                    "symbol": str(position.symbol),
                    "volume": float(position.volume),
                    "ok": ok,
                    "message": result.get("message"),
                    "profit": result.get("profit"),
                }
            )

        for order in ours_orders:
            ticket = int(order.ticket)
            try:
                result = await self.cancel_order(ctx, ticket=ticket, reason=f"kill switch: {note}")
            except SignalPilotError as exc:
                result = _refuse(exc.message)
            except Exception as exc:  # noqa: BLE001
                log.exception("kill_switch_cancel_failed ticket=%d", ticket)
                result = _refuse(f"unexpected failure cancelling #{ticket}: {exc}")
            ok = bool(result.get("ok"))
            cancelled += 1 if ok else 0
            if not ok:
                failures.append(f"#{ticket}: {result.get('message')}")
            outcomes.append(
                {
                    "ticket": ticket,
                    "kind": "order",
                    "symbol": str(order.symbol),
                    "volume": float(order.volume_current),
                    "ok": ok,
                    "message": result.get("message"),
                }
            )

        # Only now: everything of ours is flat, so the switch can be thrown.
        disarmed = True
        disarm_error: str | None = None
        try:
            await self.runtime.set_armed(False)
        except Exception as exc:  # noqa: BLE001 - report it; the closes already happened
            disarmed = False
            disarm_error = str(exc)
            log.exception("kill_switch_disarm_failed run_id=%s", ctx.run_id)

        message = (
            f"kill switch: closed {closed} of {len(ours_positions)} position(s) and cancelled "
            f"{cancelled} of {len(ours_orders)} pending order(s) carrying magic {magic}"
        )
        if untouched:
            message += f"; {untouched} external ticket(s) were left untouched"
        message += (
            "; the system is now disarmed"
            if disarmed
            else f"; DISARMING FAILED ({disarm_error}) — turn the switch off in the dashboard"
        )
        if failures:
            message += f". Failures: {'; '.join(failures)}"

        await self._audit(
            ctx,
            type="close",
            ticket=None,
            detail={
                "kill_switch": True,
                "reason": note,
                "closed": closed,
                "cancelled": cancelled,
                "external_untouched": untouched,
                "outcomes": outcomes,
                "disarmed": disarmed,
            },
            retcode=None,
        )
        self._emit(
            events.SYSTEM_WARNING,
            reason="kill_switch",
            run_id=ctx.run_id,
            closed=closed,
            cancelled=cancelled,
            external_untouched=untouched,
            disarmed=disarmed,
            message=message,
        )
        text = self._render("render_attention", subject="kill_switch", detail=message, reason=note)
        self._notify("attention", text or message)
        log.warning("kill_switch_finished %s", message)

        return {
            "ok": not failures and disarmed,
            "closed": closed,
            "cancelled": cancelled,
            "positions_found": len(ours_positions),
            "orders_found": len(ours_orders),
            "external_untouched": untouched,
            "disarmed": disarmed,
            "outcomes": outcomes,
            "failures": failures,
            "run_id": ctx.run_id,
            "message": message,
        }

    @_serialize_broker_write
    async def attach_fallback_sl(self, ticket: int) -> dict[str, Any]:
        """Put a stop on a position that has none, without asking anybody (§6.4).

        The level is ``max(SL_FALLBACK_POINTS, trade_stops_level)`` points from the position's
        **open** price, on the losing side. When price has already run past that level the stop
        is pulled back to the far edge of the broker's stops level instead of being sent to a
        certain ``10016``, and the adjustment is stated.

        This bypasses the arm gate on purpose: a naked position is a worse state than an
        unwanted order, and the operator is told that it happened.
        """
        ctx = RunContext(
            run_id=uuid.uuid4().hex,
            signal_ref=None,
            source_id=None,
            message_row_id=None,
            origin="watchdog",
            bypass_arm=True,
        )
        position, refusal = await self._find_position(ticket)
        if refusal is not None:
            return refusal
        assert position is not None

        number = int(position.ticket)
        side = POSITION_TYPE_NAMES.get(int(position.type), "buy")
        entry = float(position.price_open)
        ref = await self.resolver.get(str(position.symbol))
        quote = await self.resolver.quote(ref.name)
        digits = ref.digits

        distance_points = max(
            int(self.settings.SL_FALLBACK_POINTS), int(ref.stops_level_points)
        )
        distance = distance_points * ref.point
        candidate = (entry - distance) if side == "buy" else (entry + distance)

        # The stop must also clear the broker's minimum distance from the *current* price, or
        # the request is a guaranteed 10016 and the position stays naked.
        buffer_points = max(int(ref.stops_level_points), 1)
        market = quote.bid if side == "buy" else quote.ask
        adjusted = False
        if side == "buy":
            ceiling = market - buffer_points * ref.point
            if candidate > ceiling:
                candidate = ceiling
                adjusted = True
        else:
            floor_price = market + buffer_points * ref.point
            if candidate < floor_price:
                candidate = floor_price
                adjusted = True
        fallback_sl = pricing.round_stop_away(ref, candidate, side, "sl")

        reason = (
            f"automatic fallback stop: #{number} on {ref.name} has been open without a stop, so "
            f"one was attached {distance_points} points from the {_price_text(entry, digits)} "
            f"entry"
        )
        if adjusted:
            reason += (
                f" — price has already moved past that level, so the stop was placed at "
                f"{_price_text(fallback_sl, digits)}, the closest the broker allows to the "
                f"current {_price_text(market, digits)}"
            )

        result = await self._modify_position_core(
            ctx,
            ticket=number,
            sl=fallback_sl,
            tp=None,
            reason=reason,
            notify_type="attention",
            subject="sl_fallback",
        )
        await self._audit(
            ctx,
            type="fallback_sl",
            ticket=number,
            detail={
                "symbol": ref.name,
                "side": side,
                "entry_price": entry,
                "fallback_sl": fallback_sl,
                "distance_points": distance_points,
                "adjusted_for_market": adjusted,
                "ok": bool(result.get("ok")),
                "message": result.get("message"),
            },
            retcode=result.get("retcode"),
        )
        payload = dict(result)
        payload["fallback_sl"] = fallback_sl
        payload["distance_points"] = distance_points
        payload["adjusted_for_market"] = adjusted
        if payload.get("ok"):
            payload["message"] = (
                f"fallback stop attached to #{number} on {ref.name} at "
                f"{_price_text(fallback_sl, digits)}, {distance_points} points from the entry"
                + (" (pulled in to the closest level the broker allows)" if adjusted else "")
            )
        return payload

    @_serialize_broker_write
    async def close_signal_exposure(
        self,
        ctx: RunContext,
        *,
        signal_ref: str | None = None,
        reason: str,
        cause: str = "full_close",
    ) -> dict[str, Any]:
        """Close every open position and cancel every pending on one signal chain.

        Tickets come from the trade log for this ``signal_ref`` and every reply that
        hangs off the same teaser. The caller does not pass ticket numbers. External
        rows are left alone. One failure does not skip the remaining tickets.
        """
        ref = str(signal_ref or ctx.signal_ref or "").strip()
        if not ref:
            return _refuse(
                "close_signal_exposure needs a signal_ref (SIG:<source>:<message id>)"
            )
        note = str(reason or "").strip() or "channel closed the trade"
        label = _recorded_cause(ctx, cause)
        try:
            async with session_scope() as session:
                rows = await guards.live_chain_trades(session, ref)
                targets = [
                    (int(row.ticket), str(row.kind), str(row.symbol))
                    for row in rows
                    if not row.external
                ]
        except Exception as exc:  # noqa: BLE001 - a failed read must not pretend it closed
            log.warning("exposure_close_lookup_failed signal_ref=%s error=%s", ref, exc)
            return _refuse(
                f"could not read the tickets for {ref} ({exc}) — nothing was closed"
            )

        work = RunContext(
            run_id=ctx.run_id,
            signal_ref=ref,
            source_id=ctx.source_id,
            message_row_id=ctx.message_row_id,
            origin=ctx.origin,
            bypass_arm=ctx.bypass_arm,
        )
        closed: list[dict[str, Any]] = []
        cancelled: list[dict[str, Any]] = []
        failures: list[str] = []
        for ticket, kind, symbol in targets:
            try:
                if kind == "position":
                    result = await self.close_position(
                        work, ticket=ticket, reason=note, cause=label
                    )
                else:
                    result = await self.cancel_order(
                        work, ticket=ticket, reason=note, cause=label
                    )
            except SignalPilotError as exc:
                result = _refuse(exc.message, ticket=ticket)
            except Exception as exc:  # noqa: BLE001 - keep flattening the rest
                log.exception("exposure_close_ticket_failed ticket=%s", ticket)
                result = _refuse(f"unexpected failure on #{ticket}: {exc}", ticket=ticket)
            ok = bool(result.get("ok"))
            item = {
                "ticket": ticket,
                "kind": "position" if kind == "position" else "order",
                "symbol": symbol,
                "ok": ok,
                "message": result.get("message"),
            }
            if kind == "position":
                closed.append(item)
            else:
                cancelled.append(item)
            if not ok:
                failures.append(f"#{ticket}: {result.get('message')}")

        if not targets:
            message = f"no open position or pending order on {ref}"
        elif failures:
            message = (
                f"closed {sum(1 for item in closed if item['ok'])} position(s) and "
                f"cancelled {sum(1 for item in cancelled if item['ok'])} pending order(s) "
                f"on {ref}; {len(failures)} failed"
            )
        else:
            message = (
                f"closed {len(closed)} position(s) and cancelled {len(cancelled)} "
                f"pending order(s) on {ref}"
            )
        log.info(
            "exposure_close signal_ref=%s positions=%d orders=%d failures=%d cause=%s",
            ref,
            len(closed),
            len(cancelled),
            len(failures),
            label,
        )
        return {
            "ok": not failures,
            "signal_ref": ref,
            "cause": label,
            "closed": closed,
            "cancelled": cancelled,
            "failures": failures,
            "message": message,
        }

    # ==================================================================================
    # DISPATCH (BUILD_PROMPT §5.1)
    # ==================================================================================

    async def dispatch(self, name: str, args: dict, ctx: RunContext) -> dict[str, Any]:
        """Route one tool call from the agent loop, and never let it raise.

        A rejection is a *result* the model reads and reacts to, not an exception that kills
        the run: an unknown tool, a missing argument, a broker refusal and a dead sidecar all
        come back as ``{"ok": False, "message": ...}``. The arguments are checked against the
        very schema the model was shown (``app.ai.tools``), so the two can never drift.
        """
        tool = str(name or "")
        spec = spec_by_name(tool)
        if spec is None:
            return _refuse(
                f"unknown tool {tool!r} — call one of the tools you were given"
            )
        arguments: dict[str, Any] = args if isinstance(args, dict) else {}
        if args is not None and not isinstance(args, dict):
            return _refuse(
                f"{tool} expects an object of arguments, got {type(args).__name__}"
            )

        required = spec.input_schema.get("required") or []
        missing = [key for key in required if arguments.get(key) is None]
        if missing:
            return _refuse(
                f"{tool} is missing required argument(s): {_join(missing)} — the schema lists "
                f"{_join(list(required))} as required"
            )

        try:
            result = await self._route(tool, arguments, ctx)
            # Broker rejections already produce their own card inside the write method.
            if (
                tool in {"open_market", "place_pending", "modify_position", "modify_order",
                         "close_position", "cancel_order", "close_signal_exposure", "kill_switch"}
                and not result.get("ok") and "retcode" not in result
            ):
                self._notify_tool_refusal(tool, arguments, ctx, result.get("message"))
            return result
        except (RpcError, TradingError) as exc:
            log.warning("tool_failed tool=%s error=%s", tool, exc.message)
            self._notify_tool_refusal(tool, arguments, ctx, exc.message)
            return _refuse(exc.message, tool=tool, detail=exc.detail)
        except SignalPilotError as exc:
            log.warning("tool_failed tool=%s error=%s", tool, exc.message)
            self._notify_tool_refusal(tool, arguments, ctx, exc.message)
            return _refuse(exc.message, tool=tool, detail=exc.detail)
        except Exception as exc:  # noqa: BLE001 - a run must survive any single tool
            log.exception("tool_crashed tool=%s", tool)
            self._notify_tool_refusal(tool, arguments, ctx, str(exc))
            return _refuse(
                f"{tool} failed unexpectedly: {exc} — the account was not changed by this call",
                tool=tool,
            )

    def _notify_tool_refusal(
        self, tool: str, args: dict[str, Any], ctx: RunContext, message: Any
    ) -> None:
        text = self._render(
            "render_rejected", action=tool, symbol=args.get("symbol"),
            ticket=args.get("ticket"), retcode=args.get("retcode"), signal_ref=ctx.signal_ref,
            message=redact(str(message or "")),
        )
        if text:
            self._notify("rejected", text)

    async def _route(self, name: str, args: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        """Call the method behind one tool name with the arguments it declares."""
        if name == "get_account":
            return await self.get_account()
        if name == "resolve_symbol":
            return await self.resolve_symbol(str(args["query"]))
        if name == "get_quote":
            return await self.get_quote(str(args["symbol"]))
        if name == "price_from_distance":
            return await self.price_from_distance(
                str(args["symbol"]),
                args["anchor"],
                str(args["direction"]),
                args["distance"],
                str(args["unit"]),
            )
        if name == "evaluate_entry":
            age = args.get("signal_age_seconds")
            if age is None:
                age = await self._signal_age_seconds(ctx)
            return await self.evaluate_entry(
                str(args["symbol"]),
                str(args["side"]),
                args["signal_entry"],
                args["sl"],
                args.get("tp"),
                age,
                source_id=ctx.source_id,
                message_row_id=ctx.message_row_id,
                signal_ref=ctx.signal_ref,
            )
        if name == "get_positions":
            return await self.get_positions()
        if name == "get_orders":
            return await self.get_orders()
        if name == "get_candles":
            return await self.get_candles(
                str(args["symbol"]), str(args["timeframe"]), args["count"]
            )
        if name == "get_recent_signals":
            return await self.get_recent_signals(args["limit"])
        if name == "get_channel_ladder_stats":
            return await self.get_channel_ladder_stats(source_id=ctx.source_id)
        if name == "open_market":
            return await self.open_market(
                ctx,
                symbol=str(args["symbol"]),
                side=str(args["side"]),
                sl=args.get("sl"),
                tp=args.get("tp"),
                volume=args.get("volume"),
                risk_percent=args.get("risk_percent"),
                reason=str(args["reason"]),
                runner_mode=args.get("runner_mode"),
                runner_tp_index=args.get("runner_tp_index"),
            )
        if name == "place_pending":
            return await self.place_pending(
                ctx,
                symbol=str(args["symbol"]),
                side=str(args["side"]),
                order_type=str(args["order_type"]),
                price=args["price"],
                sl=args.get("sl"),
                tp=args.get("tp"),
                expiry_minutes=args.get("expiry_minutes"),
                volume=args.get("volume"),
                risk_percent=args.get("risk_percent"),
                reason=str(args["reason"]),
                runner_mode=args.get("runner_mode"),
                runner_tp_index=args.get("runner_tp_index"),
            )
        if name == "modify_position":
            return await self.modify_position(
                ctx,
                ticket=args["ticket"],
                sl=args.get("sl"),
                tp=args.get("tp"),
                reason=str(args["reason"]),
            )
        if name == "modify_order":
            return await self.modify_order(
                ctx,
                ticket=args["ticket"],
                price=args.get("price"),
                sl=args.get("sl"),
                tp=args.get("tp"),
                reason=str(args["reason"]),
            )
        if name == "close_position":
            return await self.close_position(
                ctx,
                ticket=args["ticket"],
                volume=args.get("volume"),
                reason=str(args["reason"]),
            )
        if name == "close_signal_exposure":
            supplied = args.get("signal_ref")
            return await self.close_signal_exposure(
                ctx,
                signal_ref=None if supplied is None else str(supplied),
                reason=str(args["reason"]),
                cause="full_close",
            )
        if name == "cancel_order":
            return await self.cancel_order(
                ctx, ticket=args["ticket"], reason=str(args["reason"])
            )
        if name == "notify":
            return await self.notify(ctx, text=str(args["text"]))
        if name == "finish":
            # The loop owns `finish`: it writes the decision onto the Run row and stops
            # iterating. Answering here keeps a loop that routes every call through dispatch
            # from recording its own terminator as a failure.
            return {
                "ok": True,
                "decision": str(args["decision"]),
                "summary": str(args["summary"]),
                "message": "the run was recorded as finished",
            }
        return _refuse(f"tool {name!r} has no implementation in the executor")
