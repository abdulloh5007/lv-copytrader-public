"""REST surface of the trading layer: the cockpit's instruments and its controls.

Three groups of routes live here, and they answer to different rules.

**Read-only views** (``/state``, ``/account``, ``/positions``, ``/orders``, ``/candles``)
must never take the dashboard down with them. MetaTrader 5 lives in a separate container
behind a JSON-RPC hop; it restarts, it self-updates, it occasionally shows a modal nobody
can click. When it is unreachable these routes answer with the database's view of the world
and an ``mt5_unavailable`` flag instead of a 500, so the operator still sees the last known
state, the signal feed and the arm switch rather than an empty page.

**Manual overrides** (``/manual/*``) are the operator's own hands on the controls *and* the
Stage-2 verification surface: the whole trading layer is driven over JSON from here, with no
UI in the loop. They are not a back door. Every one of them goes through
``Executor.dispatch`` with ``RunContext(origin="manual")`` — the same method, the same
arguments and the same refusals the model gets, which means the stop-loss rule (BUILD §6),
the duplicate-signal guard and the ``armed`` gate all apply exactly as they do to the AI.
A refusal is therefore **data**: ``{"ok": false, "message": ...}`` on HTTP 200, never an
exception, because that is what makes "open without a stop and read the refusal" a check
anyone can run with curl.

Each manual *write* also records a ``Run`` of kind ``"manual"`` with its two steps and
streams the same ``run.*`` events an agent run does, so an order the operator placed by hand
appears in the live-reasoning column and in the run history beside the ones the AI placed.
Nothing the system does to the account is invisible.

**Feeds** (``/signals``, ``/runs``) are pure database reads. Skipped messages are included
on purpose (BUILD §3.2): the operator has to be able to see that a voice note was seen,
classified and deliberately not traded — silence would be indistinguishable from a bug.

The executor's tool payloads are the source of truth for content; this module adapts them to
the field names CONTRACTS §18 publishes to the frontend, and keeps the tool fields alongside
as extras so the JSON an operator reads here is the JSON the model read.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select

from app import events
from app.config import settings
from app.db import get_setting, session_scope
from app.models import Message, Run, RunStep, Source, SystemState, Trade, utcnow
from app.runtime import Runtime, get_runtime
from app.schemas import (
    AccountOut,
    ArmRequest,
    CandleOut,
    CandlesOut,
    CandlesQuery,
    ImageOut,
    KillSwitchRequest,
    ManualCancelRequest,
    ManualCloseRequest,
    ManualEvaluateEntryRequest,
    ManualModifyOrderRequest,
    ManualModifyPositionRequest,
    ManualOpenRequest,
    ManualPendingRequest,
    ManualPriceFromDistanceRequest,
    ManualQuoteRequest,
    ManualResolveSymbolRequest,
    OrderOut,
    PositionOut,
    RunOut,
    RunsQuery,
    RunStepOut,
    SignalOut,
    SignalsQuery,
    SourceOut,
    SymbolRow,
    SymbolsAccountOut,
    SymbolsOut,
    SymbolsQuery,
    TradeOut,
    TradingStateOut,
)
from app.trading.constants import ORDER_TYPE_NAMES, margin_mode_name
from app.trading.executor import EXCERPT_CHARS, RunContext
from app.util.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading"])

#: Route serving one stored signal image; the feed hands the browser this URL.
IMAGE_URL_TEMPLATE: str = "/api/telegram/image/{image_id}"

#: What a manual run's steps are called in ``run_steps.type``.
_STEP_CALL = "tool_call"
_STEP_RESULT = "tool_result"

#: The two executor origins this module uses. ``manual`` never bypasses the arm gate.
_ORIGIN_MANUAL = "manual"

#: Reason string stamped on every kill switch fired from the dashboard.
_KILL_REASON = "operator pressed the kill switch in the dashboard"


# ======================================================================================
# Small conversions
# ======================================================================================


def _excerpt(text: str | None) -> str:
    """Return the first EXCERPT_CHARS characters of a post, whitespace collapsed."""
    if not text:
        return ""
    return " ".join(str(text).split())[:EXCERPT_CHARS]


def _rows(result: Any) -> tuple[Any, ...]:
    """Normalise an MT5 collection result into a tuple of rows.

    ``positions_get`` / ``orders_get`` answer with a tuple of namespaces, ``None`` when the
    terminal has nothing to say, and — under a single-ticket filter — occasionally one bare
    namespace. All three become a tuple.
    """
    if result is None:
        return ()
    if isinstance(result, (list, tuple)):
        return tuple(result)
    return (result,)


def _iso_to_ts(value: Any) -> float | None:
    """Turn a broker-clock ISO string back into the unix timestamp it was rendered from.

    The executor renders MT5 times with ``datetime.fromtimestamp(ts, utc).replace(tzinfo=None)``,
    so reading the string back as UTC returns exactly the original broker-clock seconds. The
    frontend wants the number (it formats with the viewer's locale), the model wants the
    sentence, so both are published.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _dt_to_ts(value: datetime | None) -> float | None:
    """Turn a stored (naive UTC) datetime into unix seconds."""
    if value is None:
        return None
    try:
        return value.replace(tzinfo=timezone.utc).timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _float(value: Any, default: float = 0.0) -> float:
    """Read a number off a tool payload, falling back rather than raising."""
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _int(value: Any, default: int = 0) -> int:
    """Read an integer off a tool payload, falling back rather than raising."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error_text(exc: BaseException) -> str:
    """Render an exception as the one sentence an operator can act on."""
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _mt5_down(exc: BaseException) -> str:
    """Explain a failed live read in terms of what the operator is now looking at."""
    return (
        f"MetaTrader 5 is not answering ({_error_text(exc)}) — showing the last state "
        f"recorded in the database"
    )


def _not_found(code: str, message: str, **detail: Any) -> JSONResponse:
    """Return the project's error envelope with a 404, independent of any app-wide handler."""
    return JSONResponse(
        status_code=404,
        content={"error": {"code": code, "message": message, "detail": detail}},
    )


# ======================================================================================
# Database joins shared by several routes
# ======================================================================================


async def _ticket_links(tickets: Any) -> dict[int, dict[str, Any]]:
    """Map live tickets onto the ``trades`` rows that own them.

    The executor already joins the signal text and permalink onto every live row; what it
    does not publish is the database identity of the trade, and the frontend needs it to link
    a position to the post that opened it. A failure here loses the links, never the rows.
    """
    wanted = {_int(t) for t in tickets if t is not None}
    wanted.discard(0)
    if not wanted:
        return {}
    index: dict[int, dict[str, Any]] = {}
    try:
        async with session_scope() as session:
            result = await session.execute(select(Trade).where(Trade.ticket.in_(sorted(wanted))))
            for trade in result.scalars().all():
                index[int(trade.ticket)] = {
                    "trade_id": trade.id,
                    "message_row_id": trade.message_row_id,
                }
    except Exception as exc:  # noqa: BLE001 - the live rows matter more than the join
        log.warning("ticket_link_lookup_failed error=%s", exc)
    return index


async def _symbol_refs(runtime: Runtime, names: Any) -> dict[str, Any]:
    """Resolve each distinct symbol once, tolerating the ones that cannot be resolved.

    Only ``pip_size`` needs this — the executor already reports digits and every distance in
    all three units. A symbol whose convention this broker does not fit must not take the
    positions table down with it, so the failure becomes a ``None`` entry.
    """
    refs: dict[str, Any] = {}
    resolver = getattr(runtime, "resolver", None)
    if resolver is None:
        return refs
    for raw in names:
        name = str(raw or "")
        if not name or name in refs:
            continue
        try:
            refs[name] = await resolver.get(name)
        except Exception as exc:  # noqa: BLE001 - one unknown symbol is not a failed request
            log.debug("symbol_ref_failed symbol=%s error=%s", name, exc)
            refs[name] = None
    return refs


async def _message_index(message_ids: Any) -> dict[int, Message]:
    """Load the posts behind a set of trades, for the database-only view."""
    wanted = {_int(i) for i in message_ids if i is not None}
    wanted.discard(0)
    if not wanted:
        return {}
    index: dict[int, Message] = {}
    try:
        async with session_scope() as session:
            result = await session.execute(select(Message).where(Message.id.in_(sorted(wanted))))
            for row in result.scalars().all():
                index[int(row.id)] = row
    except Exception as exc:  # noqa: BLE001
        log.warning("message_index_failed error=%s", exc)
    return index


# ======================================================================================
# Live rows -> the shapes CONTRACTS §18 publishes
# ======================================================================================


async def _position_payloads(runtime: Runtime, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt ``executor.get_positions()`` rows to ``PositionOut``, extras preserved."""
    refs = await _symbol_refs(runtime, (row.get("symbol") for row in rows))
    links = await _ticket_links([row.get("ticket") for row in rows])
    out: list[dict[str, Any]] = []
    for row in rows:
        ref = refs.get(str(row.get("symbol") or ""))
        link = links.get(_int(row.get("ticket")), {})
        merged = dict(row)
        merged.update(
            {
                "order_type": _int(row.get("type")),
                "price_open": _float(row.get("entry_price")),
                "price_current": _float(row.get("current_price")),
                # MT5 reports no commission on a position — it sits on the deals. The field
                # exists so every row has one shape; history carries the real figure.
                "commission": _float(row.get("commission")),
                "digits": _int(row.get("digits"), 0 if ref is None else int(ref.digits)),
                "pip_size": 0.0 if ref is None else float(ref.pip_size),
                "sl_distance": row.get("distance_to_sl"),
                "tp_distance": row.get("distance_to_tp"),
                "signal_excerpt": row.get("source_excerpt") or None,
                "trade_id": link.get("trade_id"),
                "message_row_id": link.get("message_row_id"),
                "opened_at_ts": _iso_to_ts(row.get("opened_at")),
            }
        )
        out.append(PositionOut.model_validate(merged).model_dump(mode="json"))
    return out


async def _order_payloads(runtime: Runtime, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt ``executor.get_orders()`` rows to ``OrderOut``, extras preserved."""
    refs = await _symbol_refs(runtime, (row.get("symbol") for row in rows))
    links = await _ticket_links([row.get("ticket") for row in rows])
    out: list[dict[str, Any]] = []
    for row in rows:
        ref = refs.get(str(row.get("symbol") or ""))
        link = links.get(_int(row.get("ticket")), {})
        order_type = _int(row.get("type"))
        # The executor publishes the readable name under `order_type`; the contract wants the
        # MQL5 integer there and the name beside it, so both are read off the original row.
        type_name = str(row.get("order_type") or ORDER_TYPE_NAMES.get(order_type, str(order_type)))
        merged = dict(row)
        merged.update(
            {
                "order_type": order_type,
                "order_type_name": type_name,
                "volume_current": _float(row.get("volume")),
                "volume_initial": _float(row.get("volume_initial")),
                "price_open": _float(row.get("price")),
                "price_current": _float(row.get("current_price")),
                "digits": _int(row.get("digits"), 0 if ref is None else int(ref.digits)),
                "pip_size": 0.0 if ref is None else float(ref.pip_size),
                "sl_distance": row.get("distance_to_sl"),
                "tp_distance": row.get("distance_to_tp"),
                "signal_excerpt": row.get("source_excerpt") or None,
                "trade_id": link.get("trade_id"),
                "message_row_id": link.get("message_row_id"),
                "time_setup_ts": _iso_to_ts(row.get("placed_at")),
                "expiration_ts": _iso_to_ts(row.get("expires_at")),
            }
        )
        out.append(OrderOut.model_validate(merged).model_dump(mode="json"))
    return out


def _db_trade_payload(trade: Trade, message: Message | None, *, position: bool) -> dict[str, Any]:
    """Render one stored trade in the live row's shape, for the MT5-is-down view.

    Everything that only MT5 knows — the current price, the floating P/L, the live distances —
    is absent rather than invented, and ``from_database`` says so on the row itself. The times
    are UTC here, not broker-clock, because that is what the database stores.
    """
    common: dict[str, Any] = {
        "ticket": int(trade.ticket),
        "symbol": trade.symbol,
        "side": trade.side,
        "order_type": int(trade.order_type),
        "sl": float(trade.sl),
        "tp": float(trade.tp),
        "magic": int(trade.magic),
        "comment": trade.comment or "",
        "external": bool(trade.external),
        "digits": 0,
        "pip_size": 0.0,
        "sl_distance": None,
        "tp_distance": None,
        "trade_id": int(trade.id),
        "run_id": trade.run_id,
        "signal_ref": trade.signal_ref,
        "signal_excerpt": _excerpt(message.text) if message is not None else None,
        "message_row_id": trade.message_row_id,
        "permalink": message.permalink if message is not None else None,
        "open_reason": trade.open_reason,
        "split_index": int(trade.split_index),
        "split_count": int(trade.split_count),
        "unmanaged_tps": list(trade.unmanaged_tps or []),
        "ladder_role": trade.ladder_role,
        "runner_mode": trade.runner_mode,
        "suggested_runner_mode": trade.suggested_runner_mode,
        "runner_tp_bias": trade.runner_tp_bias,
        "suggested_runner_tp_index": trade.suggested_runner_tp_index,
        "chosen_runner_tp_index": trade.chosen_runner_tp_index,
        "from_database": True,
    }
    if position:
        common.update(
            {
                "volume": float(trade.volume),
                "price_open": float(trade.entry_price),
                "price_current": float(trade.entry_price),
                "profit": 0.0,
                "swap": 0.0,
                "commission": 0.0,
                "opened_at_ts": _dt_to_ts(trade.opened_at),
            }
        )
        return PositionOut.model_validate(common).model_dump(mode="json")
    common.update(
        {
            "order_type_name": ORDER_TYPE_NAMES.get(int(trade.order_type), str(trade.order_type)),
            "volume_current": float(trade.volume),
            "volume_initial": float(trade.requested_volume or trade.volume),
            "price_open": float(trade.entry_price),
            "price_current": float(trade.entry_price),
            "time_setup_ts": _dt_to_ts(trade.opened_at),
            "expiration_ts": None,
        }
    )
    return OrderOut.model_validate(common).model_dump(mode="json")


async def _db_trade_view(*, position: bool) -> list[dict[str, Any]]:
    """Return the stored open positions or pending orders, newest first."""
    kind = "position" if position else "order"
    status = "open" if position else "pending"
    try:
        async with session_scope() as session:
            result = await session.execute(
                select(Trade)
                .where(Trade.kind == kind, Trade.status == status)
                .order_by(Trade.id.desc())
            )
            trades = list(result.scalars().all())
    except Exception as exc:  # noqa: BLE001 - an unreadable database is still not a 500 here
        log.warning("db_trade_view_failed kind=%s error=%s", kind, exc)
        return []
    messages = await _message_index(t.message_row_id for t in trades)
    return [
        _db_trade_payload(trade, messages.get(_int(trade.message_row_id)), position=position)
        for trade in trades
    ]


# ======================================================================================
# Feed payloads
# ======================================================================================


def _image_payloads(message: Message) -> list[dict[str, Any]]:
    """Return the message's images as ``{id, url, width, height}`` rows, in album order."""
    rows: list[dict[str, Any]] = []
    for image in message.images or []:
        rows.append(
            ImageOut(
                id=int(image.id),
                url=IMAGE_URL_TEMPLATE.format(image_id=int(image.id)),
                width=image.width,
                height=image.height,
            ).model_dump(mode="json")
        )
    return rows


def _signal_payload(message: Message, runs_count: int, trades_count: int) -> dict[str, Any]:
    """Render one feed row: the post, what it produced, and how old it is.

    ``skipped`` rows are rendered exactly like the others, badge and all. The operator has to
    be able to see that a voice note was received, classified and deliberately not traded
    (BUILD §3.2) — a feed that hides them cannot distinguish "ignored" from "lost".
    """
    age: float | None = None
    if message.posted_at is not None:
        age = max(0.0, (utcnow() - message.posted_at).total_seconds())
    payload = SignalOut(
        id=int(message.id),
        source_id=int(message.source_id),
        message_id=int(message.message_id),
        revision=int(message.revision),
        kind=message.kind,
        status=message.status,
        skip_reason=message.skip_reason,
        catchup=bool(message.catchup),
        text=message.text or "",
        permalink=message.permalink,
        posted_at=message.posted_at,
        received_at=message.received_at,
        image_count=int(message.image_count),
        images=[ImageOut.model_validate(row) for row in _image_payloads(message)],
        reply_to_message_id=message.reply_to_message_id,
        signal_ref=message.signal_ref,
        deleted_at=message.deleted_at,
        runs_count=int(runs_count),
        trades_count=int(trades_count),
    ).model_dump(mode="json")
    payload["signal_age_seconds"] = None if age is None else int(age)
    payload["stale"] = bool(age is not None and age > float(settings.STALE_SIGNAL_SECONDS))
    payload["grouped_id"] = message.grouped_id
    payload["error"] = message.error
    return payload


def _run_payload(run: Run) -> dict[str, Any]:
    """Render one ``runs`` row."""
    return RunOut.model_validate(run).model_dump(mode="json")


def _step_payload(step: RunStep) -> dict[str, Any]:
    """Render one ``run_steps`` row."""
    return RunStepOut.model_validate(step).model_dump(mode="json")


def _trade_payload(trade: Trade) -> dict[str, Any]:
    """Render one ``trades`` row."""
    return TradeOut.model_validate(trade).model_dump(mode="json")


async def _counts(session: Any, message_ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
    """Count runs and trades per message row, in two grouped queries."""
    runs: dict[int, int] = {}
    trades: dict[int, int] = {}
    if not message_ids:
        return runs, trades
    run_rows = await session.execute(
        select(Run.message_row_id, func.count(Run.id))
        .where(Run.message_row_id.in_(message_ids))
        .group_by(Run.message_row_id)
    )
    for message_row_id, count in run_rows.all():
        runs[_int(message_row_id)] = _int(count)
    trade_rows = await session.execute(
        select(Trade.message_row_id, func.count(Trade.id))
        .where(Trade.message_row_id.in_(message_ids))
        .group_by(Trade.message_row_id)
    )
    for message_row_id, count in trade_rows.all():
        trades[_int(message_row_id)] = _int(count)
    return runs, trades


# ======================================================================================
# The manual path: one context, one dispatch, one recorded run
# ======================================================================================


def _manual_context() -> RunContext:
    """Build the RunContext every manual call runs under.

    ``bypass_arm`` stays False. The operator's controls are subject to the arm switch, the
    stop-loss rule and the duplicate guard exactly as the model's are — the only thing in the
    system that bypasses the arm gate is the kill switch's own closes and the SL fallback,
    and both live inside the executor (BUILD §5.5, §6.4).
    """
    return RunContext(
        run_id=uuid.uuid4().hex,
        signal_ref=None,
        source_id=None,
        message_row_id=None,
        origin=_ORIGIN_MANUAL,
        bypass_arm=False,
    )


def _no_executor(tool: str) -> dict[str, Any]:
    """Answer a manual call made before the trading layer finished starting."""
    return {
        "ok": False,
        "message": (
            f"the trading executor is not running yet, so {tool} did nothing — wait for MT5 "
            f"to come up and try again"
        ),
        "tool": tool,
    }


async def _dispatch(runtime: Runtime, tool: str, args: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
    """Run one tool through the executor's own dispatcher, which never raises."""
    executor = getattr(runtime, "executor", None)
    if executor is None:
        return _no_executor(tool)
    result = await executor.dispatch(tool, args, ctx)
    if not isinstance(result, dict):  # pragma: no cover - dispatch always returns a dict
        return {"ok": False, "message": f"{tool} returned an unreadable result", "tool": tool}
    result.setdefault("run_id", ctx.run_id)
    result.setdefault("origin", ctx.origin)
    return result


async def _manual_read(runtime: Runtime, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a read-only tool on the operator's behalf. Nothing is recorded: nothing changed."""
    return await _dispatch(runtime, tool, args, _manual_context())


async def _open_manual_run(
    ctx: RunContext, tool: str, args: dict[str, Any], call_id: str, armed: bool, started_at: datetime
) -> None:
    """Record the run and its tool_call step before the executor touches the account."""
    try:
        async with session_scope() as session:
            session.add(
                Run(
                    id=ctx.run_id,
                    source_id=None,
                    message_row_id=None,
                    kind=_ORIGIN_MANUAL,
                    status="running",
                    armed_at_start=bool(armed),
                    started_at=started_at,
                )
            )
            session.add(
                RunStep(
                    run_id=ctx.run_id,
                    idx=0,
                    type=_STEP_CALL,
                    name=tool,
                    payload={"id": call_id, "input": args, "origin": ctx.origin},
                    ok=None,
                )
            )
    except Exception as exc:  # noqa: BLE001 - bookkeeping never blocks the operator's control
        log.error("manual_run_open_failed run_id=%s tool=%s error=%s", ctx.run_id, tool, exc)


async def _close_manual_run(
    ctx: RunContext,
    tool: str,
    result: dict[str, Any],
    call_id: str,
    *,
    ok: bool,
    latency_ms: int,
) -> None:
    """Finish the run row and record the tool_result step.

    A refusal closes the run as ``finished`` with decision ``ignored``, not as ``error``:
    "the system is disarmed" is a correct outcome the operator asked for, and painting it red
    would teach them to ignore the colour that means a run broke.
    """
    try:
        async with session_scope() as session:
            run = await session.get(Run, ctx.run_id)
            if run is None:
                # The opening write failed, so there is no row to close and the step would
                # break the foreign key. The result still reached the caller; only the audit
                # trail is short, and the earlier failure was already logged.
                log.error("manual_run_missing run_id=%s tool=%s", ctx.run_id, tool)
                return
            run.status = "finished"
            run.decision = "acted" if ok else "ignored"
            run.summary = str(result.get("message") or "")[:2000]
            run.iterations = 1
            run.latency_ms = int(latency_ms)
            run.finished_at = utcnow()
            session.add(
                RunStep(
                    run_id=ctx.run_id,
                    idx=1,
                    type=_STEP_RESULT,
                    name=tool,
                    payload={"id": call_id, "result": result},
                    ok=ok,
                )
            )
    except Exception as exc:  # noqa: BLE001 - the account was already touched; only the log suffers
        log.error("manual_run_close_failed run_id=%s tool=%s error=%s", ctx.run_id, tool, exc)


async def _manual_write(runtime: Runtime, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a write tool on the operator's behalf, recorded and streamed like an agent run."""
    ctx = _manual_context()
    call_id = f"manual-{ctx.run_id[:8]}"
    try:
        armed = await runtime.armed()
    except Exception as exc:  # noqa: BLE001 - the executor's own gate is the one that decides
        log.warning("manual_armed_read_failed error=%s", exc)
        armed = False

    await _open_manual_run(ctx, tool, args, call_id, armed, utcnow())
    runtime.bus.emit(
        events.RUN_STARTED,
        run_id=ctx.run_id,
        kind=_ORIGIN_MANUAL,
        source_id=None,
        message_row_id=None,
        signal_ref=None,
        provider_kind=None,
        model=None,
        armed=armed,
    )
    runtime.bus.emit(
        events.RUN_TOOL_CALL, run_id=ctx.run_id, idx=0, id=call_id, name=tool, input=args
    )

    started = time.monotonic()
    result = await _dispatch(runtime, tool, args, ctx)
    latency_ms = int((time.monotonic() - started) * 1000.0)
    ok = bool(result.get("ok"))

    runtime.bus.emit(
        events.RUN_TOOL_RESULT,
        run_id=ctx.run_id,
        idx=1,
        id=call_id,
        name=tool,
        ok=ok,
        result=result,
    )
    await _close_manual_run(ctx, tool, result, call_id, ok=ok, latency_ms=latency_ms)
    runtime.bus.emit(
        events.RUN_FINISHED,
        run_id=ctx.run_id,
        status="finished",
        decision="acted" if ok else "ignored",
        summary=str(result.get("message") or ""),
        error=None,
        iterations=1,
        latency_ms=latency_ms,
        input_tokens=0,
        output_tokens=0,
    )
    return result


# ======================================================================================
# State and control
# ======================================================================================


async def _margin_facts(runtime: Runtime) -> tuple[str | None, str | None, datetime | None]:
    """Return (margin_mode, currency, last_reconcile_at), learning them once if needed.

    The two facts are cached in ``system_state`` because the cockpit chrome asks for them on
    every reconnect and neither changes while the account stays the same. When they are not
    cached yet a single ``account_info`` fills them in; if MT5 is down they stay null and the
    page renders without them rather than failing.
    """
    margin_mode: str | None = None
    currency: str | None = None
    last_reconcile: datetime | None = None
    try:
        async with session_scope() as session:
            state = await session.get(SystemState, 1)
            if state is not None:
                margin_mode = state.margin_mode
                currency = state.account_currency
                last_reconcile = state.last_reconcile_at
    except Exception as exc:  # noqa: BLE001
        log.warning("system_state_read_failed error=%s", exc)

    forced = settings.FORCE_MARGIN_MODE
    if forced:
        # The forced mode is the one that decides whether multi-TP splits, so it is the one
        # the cockpit must show (BUILD §1.3).
        margin_mode = str(forced).strip().lower()
    if margin_mode and currency:
        return margin_mode, currency, last_reconcile

    rpc = getattr(runtime, "rpc", None)
    if rpc is None:
        return margin_mode, currency, last_reconcile
    try:
        info = await rpc.account_info()
        if info is None:
            return margin_mode, currency, last_reconcile
        detected = margin_mode_name(int(info.margin_mode))
        margin_mode = str(forced).strip().lower() if forced else detected
        currency = str(info.currency)
    except Exception as exc:  # noqa: BLE001 - the state route never depends on MT5
        log.debug("margin_facts_probe_failed error=%s", exc)
        return margin_mode, currency, last_reconcile

    try:
        async with session_scope() as session:
            state = await session.get(SystemState, 1)
            if state is None:
                state = SystemState(id=1)
                session.add(state)
            state.margin_mode = margin_mode
            state.account_currency = currency
            state.updated_at = utcnow()
    except Exception as exc:  # noqa: BLE001
        log.warning("system_state_write_failed error=%s", exc)
    return margin_mode, currency, last_reconcile


@router.get("/state", response_model=None)
async def read_state(
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Everything the cockpit chrome needs in one call, and never a 500.

    This is the route the dashboard refetches on every WebSocket reconnect, so it reads the
    database and the cached status map rather than the broker. The MT5 dot going red *is* the
    report that MT5 is down; the page around it still has to render.
    """
    status = await runtime.snapshot_status()
    margin_mode, currency, last_reconcile = await _margin_facts(runtime)

    source_row: Source | None = None
    default_risk = float(settings.DEFAULT_RISK_PERCENT)
    ladder_volume = float(settings.LADDER_VOLUME)
    counts = {"signals": 0, "runs": 0, "positions": 0, "orders": 0, "trades": 0}
    try:
        async with session_scope() as session:
            found = await session.execute(
                select(Source).where(Source.is_active.is_(True)).order_by(Source.id.desc())
            )
            source_row = found.scalars().first()
            stored = await get_setting(session, "trading.default_risk_percent", None)
            risk = _float(stored, 0.0)
            if risk > 0.0:
                default_risk = risk
            stored_ladder = await get_setting(session, "trading.ladder_volume", None)
            ladder = _float(stored_ladder, 0.0)
            if ladder > 0.0:
                ladder_volume = ladder
            counts["signals"] = _int(
                (await session.execute(select(func.count(Message.id)))).scalar_one()
            )
            counts["runs"] = _int((await session.execute(select(func.count(Run.id)))).scalar_one())
            counts["trades"] = _int(
                (await session.execute(select(func.count(Trade.id)))).scalar_one()
            )
            counts["positions"] = _int(
                (
                    await session.execute(
                        select(func.count(Trade.id)).where(
                            Trade.kind == "position", Trade.status == "open"
                        )
                    )
                ).scalar_one()
            )
            counts["orders"] = _int(
                (
                    await session.execute(
                        select(func.count(Trade.id)).where(
                            Trade.kind == "order", Trade.status == "pending"
                        )
                    )
                ).scalar_one()
            )
    except Exception as exc:  # noqa: BLE001 - the header renders even with a sick database
        log.warning("trading_state_db_read_failed error=%s", exc)

    payload = TradingStateOut(
        armed=bool(status.get("armed", False)),
        mt5=status.get("mt5", {"ok": False, "error": None}),
        telegram=status.get("telegram", {"ok": False, "error": None}),
        provider=status.get("provider", {"ok": False, "error": None}),
        margin_mode=margin_mode,
        currency=currency,
        default_risk_percent=default_risk,
        ladder_volume=ladder_volume,
        source=SourceOut.model_validate(source_row) if source_row is not None else None,
        last_reconcile_at=last_reconcile,
        needs_reentry=list(status.get("needs_reentry") or []),
    ).model_dump(mode="json")
    payload["counts"] = counts
    payload["magic_number"] = int(settings.MAGIC_NUMBER)
    payload["mt5_unavailable"] = not bool(payload["mt5"]["ok"])
    return payload


@router.post("/arm", response_model=None)
async def set_arm(
    body: ArmRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Throw the operator's on/off switch and report the state that was persisted.

    Disarmed, the agent loop still runs end to end and streams its full reasoning; only the
    broker call is withheld (BUILD §5.5). The value is read back from the database rather
    than echoed, so what the header shows is what the guards will read.
    """
    await runtime.set_armed(bool(body.armed))
    armed = await runtime.armed()
    log.warning("arm_switch_set armed=%s", armed)
    return {"ok": True, "armed": armed}


@router.post("/kill-switch", response_model=None)
async def kill_switch(
    body: KillSwitchRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Close every position and cancel every order carrying our magic, then disarm.

    The confirmation string is enforced by the schema. The order — close, *then* disarm — is
    the whole point: disarming first would make the switch's own closes hit the arm gate and
    the kill switch would do nothing (BUILD §5.5). It therefore works while already disarmed,
    and it never touches a ticket with a foreign magic.

    If MT5 cannot be reached the closes are impossible, so the switch falls back to the one
    thing it can still guarantee: it disarms, and says so. That is not a bypass — it is the
    safe half of an emergency stop, and the failure is reported as ``ok: false``.
    """
    executor = getattr(runtime, "executor", None)
    if executor is None:
        return {
            "ok": False,
            "closed": 0,
            "cancelled": 0,
            "disarmed": False,
            "message": "the trading executor is not running, so nothing could be closed",
        }
    try:
        result = await executor.kill_switch(reason=_KILL_REASON)
    except Exception as exc:  # noqa: BLE001 - an emergency control reports, it does not throw
        log.exception("kill_switch_failed")
        disarmed = True
        try:
            await runtime.set_armed(False)
        except Exception as disarm_exc:  # noqa: BLE001
            disarmed = False
            log.exception("kill_switch_fallback_disarm_failed")
            log.error("kill_switch_fallback_disarm_error=%s", disarm_exc)
        message = (
            f"the kill switch could not reach the broker ({_error_text(exc)}), so nothing was "
            f"closed"
        )
        message += (
            "; the system has been disarmed so no new order can be sent"
            if disarmed
            else "; DISARMING ALSO FAILED — turn the switch off by hand"
        )
        runtime.bus.emit(
            events.SYSTEM_WARNING,
            reason="kill_switch_failed",
            disarmed=disarmed,
            message=message,
        )
        return {
            "ok": False,
            "closed": 0,
            "cancelled": 0,
            "disarmed": disarmed,
            "message": message,
        }
    result.setdefault("ok", False)
    result.setdefault("closed", 0)
    result.setdefault("cancelled", 0)
    result.setdefault("message", "")
    return result


# ======================================================================================
# Read-only views
# ======================================================================================


@router.get("/account", response_model=None)
async def read_account(
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Balance, equity, free margin, floating P/L and today's realized P/L on the broker's day.

    The payload is the model's ``get_account()`` shape with the contract's field names added
    beside it, so the dashboard and the AI read the same numbers from the same call.
    """
    executor = getattr(runtime, "executor", None)
    info: dict[str, Any] | None = None
    pending = 0
    failure: str | None = None
    if executor is None:
        failure = "the trading executor is not running yet"
    else:
        try:
            info = await executor.get_account()
            pending = len(_rows(await runtime.rpc.orders_get()))
        except Exception as exc:  # noqa: BLE001 - the panel degrades, it does not disappear
            log.warning("account_read_failed error=%s", exc)
            info = None
            failure = _mt5_down(exc)

    if info is None:
        margin_mode, currency, _ = await _margin_facts(runtime)
        payload = AccountOut(
            currency=currency or "",
            margin_mode=margin_mode or "netting",
        ).model_dump(mode="json")
        payload["ok"] = False
        payload["mt5_unavailable"] = True
        payload["message"] = failure or "MetaTrader 5 is not answering"
        return payload

    merged = dict(info)
    merged.update(
        {
            "profit": _float(info.get("floating_profit")),
            "today_profit": _float(info.get("realized_pl_today")),
            "pending_orders": pending,
        }
    )
    payload = AccountOut.model_validate(merged).model_dump(mode="json")
    payload["mt5_unavailable"] = False
    return payload


@router.get("/positions", response_model=None)
async def read_positions(
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Open positions, live from MT5, each joined to the post that opened it.

    Rows carrying a foreign magic come back marked ``external``: they are displayed and never
    modified, closed, or touched by the kill switch (BUILD §8).
    """
    executor = getattr(runtime, "executor", None)
    if executor is not None:
        try:
            result = await executor.get_positions()
            rows = [row for row in (result.get("positions") or []) if isinstance(row, dict)]
            return {
                "ok": True,
                "positions": await _position_payloads(runtime, rows),
                "count": len(rows),
                "message": str(result.get("message") or ""),
                "mt5_unavailable": False,
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("positions_read_failed error=%s", exc)
            message = _mt5_down(exc)
    else:
        message = "the trading executor is not running yet — showing the stored positions"

    positions = await _db_trade_view(position=True)
    return {
        "ok": False,
        "positions": positions,
        "count": len(positions),
        "message": message,
        "mt5_unavailable": True,
    }


@router.get("/orders", response_model=None)
async def read_orders(
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Pending orders, live from MT5, each joined to the post that placed it."""
    executor = getattr(runtime, "executor", None)
    if executor is not None:
        try:
            result = await executor.get_orders()
            rows = [row for row in (result.get("orders") or []) if isinstance(row, dict)]
            return {
                "ok": True,
                "orders": await _order_payloads(runtime, rows),
                "count": len(rows),
                "message": str(result.get("message") or ""),
                "mt5_unavailable": False,
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("orders_read_failed error=%s", exc)
            message = _mt5_down(exc)
    else:
        message = "the trading executor is not running yet — showing the stored orders"

    orders = await _db_trade_view(position=False)
    return {
        "ok": False,
        "orders": orders,
        "count": len(orders),
        "message": message,
        "mt5_unavailable": True,
    }


@router.get("/candles", response_model=None)
async def read_candles(
    query: Annotated[CandlesQuery, Query()],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Recent OHLC bars, oldest first, capped at ``settings.MAX_CANDLES``.

    The schema already rejects a count above the cap; the executor clamps it again, because
    the model calls the same code path through a tool where nothing validated the number.
    """
    executor = getattr(runtime, "executor", None)
    if executor is None:
        return {
            "ok": False,
            "symbol": query.symbol,
            "timeframe": query.timeframe,
            "count": 0,
            "candles": [],
            "message": "the trading executor is not running yet",
            "mt5_unavailable": True,
        }
    count = min(int(query.count), int(settings.MAX_CANDLES))
    try:
        result = await executor.get_candles(query.symbol, query.timeframe, count)
    except Exception as exc:  # noqa: BLE001
        log.warning("candles_read_failed symbol=%s error=%s", query.symbol, exc)
        return {
            "ok": False,
            "symbol": query.symbol,
            "timeframe": query.timeframe,
            "count": 0,
            "candles": [],
            "message": _mt5_down(exc),
            "mt5_unavailable": True,
        }

    candles: list[CandleOut] = []
    for row in result.get("candles") or []:
        if not isinstance(row, dict):
            continue
        candles.append(
            CandleOut(
                # The contract publishes bar time as unix seconds on the broker's clock; the
                # executor's ISO rendering of the same instant stays in `message`.
                time=_float(row.get("time_ts")),
                open=_float(row.get("open")),
                high=_float(row.get("high")),
                low=_float(row.get("low")),
                close=_float(row.get("close")),
                tick_volume=_int(row.get("tick_volume")),
                spread=_int(row.get("spread")),
                real_volume=_int(row.get("real_volume")),
            )
        )
    merged = dict(result)
    merged["candles"] = candles
    payload = CandlesOut.model_validate(merged).model_dump(mode="json")
    payload["mt5_unavailable"] = False
    return payload


# ======================================================================================
# The account's symbol table — the operator's picker for `limits.symbol_allowlist`
# ======================================================================================


async def _symbol_account(runtime: Runtime) -> tuple[SymbolsAccountOut, bool]:
    """Name the account the symbol rows came from, and say whether the terminal answered.

    A symbol list is only meaningful next to the account that carries it — a picker showing
    ``XAUUSDm`` while the terminal is logged into a broker that quotes ``XAUUSDr`` is how an
    allowlist gets filled with names that will never match. The live answer is preferred; if
    MT5 is not talking, the saved MT5 settings and finally the ``.env`` seed still say which
    account the operator configured, which is exactly what they need to read while fixing it.

    The second element is what makes that distinction visible: ``False`` means the account
    named here was read from configuration rather than from the terminal, so any rows served
    beside it came out of the resolver's cache and are the *last* table read, not a live one.
    """
    rpc = getattr(runtime, "rpc", None)
    if rpc is not None:
        try:
            info = await rpc.account_info()
            return SymbolsAccountOut(login=int(info.login), server=str(info.server)), True
        except Exception as exc:  # noqa: BLE001 - the picker degrades, it does not disappear
            log.debug("symbols_account_read_failed error=%s", exc)

    login: int | None = None
    server = ""
    try:
        async with session_scope() as session:
            stored_login = await get_setting(session, "mt5.login", None)
            stored_server = await get_setting(session, "mt5.server", None)
        if stored_login not in (None, ""):
            login = int(stored_login)
        server = str(stored_server or "")
    except Exception as exc:  # noqa: BLE001 - an unreadable store still leaves the .env seed
        log.debug("symbols_account_settings_failed error=%s", exc)

    if login is None and runtime.settings.MT5_LOGIN:
        login = int(runtime.settings.MT5_LOGIN)
    if not server:
        server = str(runtime.settings.MT5_SERVER or "")
    return SymbolsAccountOut(login=login, server=server), False


@router.get("/symbols", response_model=None)
async def read_symbols(
    query: Annotated[SymbolsQuery, Query()],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """List the connected account's instruments so the operator can pick, not type, an allowlist.

    This is a read of the broker's own table, and it is deliberately **not** a restriction.
    ``limits.symbol_allowlist`` ships empty, an empty allowlist means every symbol is allowed
    and is not evaluated at all (BUILD §6), and nothing on this route changes that: it hands
    the settings page the names the account actually carries and stops there. The three
    unconditional gates remain the ``armed`` switch, the stop-loss rule and the
    duplicate-signal guard.

    Two honesty rules shape the payload:

    * ``total`` and ``truncated`` describe the *whole* match, not the page. A real Exness
      account carries several hundred symbols and quietly returning the first ``limit`` of
      them would tell the operator an instrument does not exist when it merely did not fit.
    * MT5 being unreachable answers **200** with ``mt5_unavailable: true``, an empty list and
      a message, like every other read route here. The settings page has to render while the
      terminal is down, because the settings page is where the terminal gets fixed.
    """
    account, live = await _symbol_account(runtime)
    resolver = getattr(runtime, "resolver", None)
    if resolver is None:
        return SymbolsOut(
            ok=False,
            account=account,
            mt5_unavailable=True,
            message="the trading layer is not running yet — no symbol table has been read",
        ).model_dump(mode="json")

    try:
        refs, total = await resolver.catalog(
            query=query.query,
            only_tradable=query.only_tradable,
            limit=query.limit,
        )
    except Exception as exc:  # noqa: BLE001 - a down terminal is a state, not a 500
        log.warning("symbols_read_failed error=%s", exc)
        return SymbolsOut(
            ok=False,
            account=account,
            mt5_unavailable=True,
            message=(
                f"MetaTrader 5 is not answering ({_error_text(exc)}) — the symbol list is "
                f"unavailable; any allowlist already saved is unchanged"
            ),
        ).model_dump(mode="json")

    rows = [SymbolRow.model_validate(ref) for ref in refs]
    truncated = total > len(rows)
    if truncated:
        message = (
            f"{total} symbols match; showing the first {len(rows)} — search by name or "
            f"description to reach the rest"
        )
    else:
        message = f"{total} symbol{'' if total == 1 else 's'}"
    if not live:
        # Rows without a live account read came from the resolver's cache. Serving them as
        # though the terminal had just answered is the quiet kind of dishonesty this file
        # exists to avoid: the operator would have no way to tell a current table from the
        # one that was current before the terminal went away.
        message = (
            f"{message} — MetaTrader 5 is not answering, so this is the last symbol table "
            f"read from it"
        )
    return SymbolsOut(
        ok=True,
        symbols=rows,
        total=total,
        truncated=truncated,
        account=account,
        mt5_unavailable=False,
        message=message,
    ).model_dump(mode="json")


# ======================================================================================
# Manual overrides — the operator's controls and the Stage-2 verification surface
# ======================================================================================


@router.post("/manual/resolve-symbol", response_model=None)
async def manual_resolve_symbol(
    body: ManualResolveSymbolRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Rank the broker's symbols against a loose name — never one silent guess (BUILD §2)."""
    return await _manual_read(runtime, "resolve_symbol", body.model_dump())


@router.post("/manual/quote", response_model=None)
async def manual_quote(
    body: ManualQuoteRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Live bid/ask with the spread in points and pips, and the broker's clock."""
    return await _manual_read(runtime, "get_quote", body.model_dump())


@router.post("/manual/price-from-distance", response_model=None)
async def manual_price_from_distance(
    body: ManualPriceFromDistanceRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Turn "20 pips below entry" into an absolute price; the server does every multiplication."""
    return await _manual_read(runtime, "price_from_distance", body.model_dump())


@router.post("/manual/evaluate-entry", response_model=None)
async def manual_evaluate_entry(
    body: ManualEvaluateEntryRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """The late-entry instrument: how much of the intended stop the delay already ate."""
    return await _manual_read(runtime, "evaluate_entry", body.model_dump())


@router.post("/manual/open", response_model=None)
async def manual_open(
    body: ManualOpenRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Open a position at the market.

    Identical to the model's ``open_market``. A missing stop is an incomplete entry and is
    sent. A stop on the wrong side, a duplicate signal, an operator limit or a disarmed
    system all come back as ``{"ok": false, "message": ...}`` on HTTP 200 — the refusal is
    the answer, not an error.
    """
    return await _manual_write(runtime, "open_market", body.model_dump())


@router.post("/manual/pending", response_model=None)
async def manual_pending(
    body: ManualPendingRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Place a limit or stop order; success here is retcode 10008, *placed*, not *done*."""
    return await _manual_write(runtime, "place_pending", body.model_dump())


@router.post("/manual/modify-position", response_model=None)
async def manual_modify_position(
    body: ManualModifyPositionRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Move an open position's stop or target. ``sl: 0`` is refused — a stop is never removed."""
    return await _manual_write(runtime, "modify_position", body.model_dump())


@router.post("/manual/modify-order", response_model=None)
async def manual_modify_order(
    body: ManualModifyOrderRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Change a pending order's price, stop or target. ``sl: 0`` is refused the same way."""
    return await _manual_write(runtime, "modify_order", body.model_dump())


@router.post("/manual/close", response_model=None)
async def manual_close(
    body: ManualCloseRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Close a position in full, or in part when a smaller volume is given."""
    return await _manual_write(runtime, "close_position", body.model_dump())


@router.post("/manual/cancel", response_model=None)
async def manual_cancel(
    body: ManualCancelRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """Remove a pending order from the book."""
    return await _manual_write(runtime, "cancel_order", body.model_dump())


# ======================================================================================
# Feeds: signals, runs, steps
# ======================================================================================


@router.get("/signals", response_model=None)
async def list_signals(query: Annotated[SignalsQuery, Query()]) -> dict[str, Any]:
    """The dashboard feed: newest first, by database id, skipped rows included.

    Ingest order is what "newest first" means in a feed — a catch-up batch replayed after
    downtime belongs at the top, where the operator will see that it arrived late, not
    interleaved into yesterday by its posting time.
    """
    async with session_scope() as session:
        statement = select(Message).order_by(Message.id.desc()).limit(int(query.limit))
        if query.status is not None:
            statement = statement.where(Message.status == query.status)
        result = await session.execute(statement)
        messages = list(result.scalars().all())
        runs, trades = await _counts(session, [int(m.id) for m in messages])
    return {
        "signals": [
            _signal_payload(message, runs.get(int(message.id), 0), trades.get(int(message.id), 0))
            for message in messages
        ]
    }


@router.get("/signals/{signal_id}", response_model=None)
async def read_signal(signal_id: int) -> Any:
    """One post with every run it caused and every trade those runs produced.

    Trades are matched on the message row *and* on ``signal_ref``, because an edit creates a
    new message row while every revision of the same post shares one signal reference — the
    legs of a multi-TP split opened from revision 1 still belong to the post being read.
    """
    async with session_scope() as session:
        message = await session.get(Message, int(signal_id))
        if message is None:
            return _not_found("not_found", f"no signal with id {signal_id}", signal_id=signal_id)
        runs_result = await session.execute(
            select(Run)
            .where(Run.message_row_id == message.id)
            .order_by(Run.started_at.asc(), Run.id.asc())
        )
        runs = list(runs_result.scalars().all())
        trades_result = await session.execute(
            select(Trade)
            .where(
                or_(
                    Trade.message_row_id == message.id,
                    Trade.signal_ref == message.signal_ref,
                )
            )
            .order_by(Trade.id.asc())
        )
        trades = list(trades_result.scalars().all())
        counts_runs, counts_trades = await _counts(session, [int(message.id)])
        payload = _signal_payload(
            message,
            counts_runs.get(int(message.id), 0),
            counts_trades.get(int(message.id), 0),
        )
    return {
        "signal": payload,
        "runs": [_run_payload(run) for run in runs],
        "trades": [_trade_payload(trade) for trade in trades],
    }


@router.get("/runs", response_model=None)
async def list_runs(query: Annotated[RunsQuery, Query()]) -> dict[str, Any]:
    """Recent runs, newest first — agent runs, watchdog runs and manual actions alike."""
    async with session_scope() as session:
        result = await session.execute(
            select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(int(query.limit))
        )
        runs = list(result.scalars().all())
    return {"runs": [_run_payload(run) for run in runs]}


@router.get("/runs/{run_id}", response_model=None)
async def read_run(run_id: str) -> Any:
    """One run with every persisted step in order, so a finished run expands to what happened.

    Order is the message: thinking, then each tool call and its result, exactly as they
    occurred. ``idx`` is the ordering, not ``created_at`` — two steps written in the same
    millisecond must still render in the order the model produced them.
    """
    async with session_scope() as session:
        run = await session.get(Run, str(run_id))
        if run is None:
            return _not_found("not_found", f"no run with id {run_id}", run_id=run_id)
        result = await session.execute(
            select(RunStep).where(RunStep.run_id == run.id).order_by(RunStep.idx.asc(), RunStep.id.asc())
        )
        steps = list(result.scalars().all())
        payload = _run_payload(run)
    return {"run": payload, "steps": [_step_payload(step) for step in steps]}
