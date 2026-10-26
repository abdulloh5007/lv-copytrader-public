"""`GET /api/stream` — the dashboard WebSocket, and the one account ticker behind it.

CONTRACTS §19. The server speaks the raw event envelope of §4
(``{"type", "ts", "seq", "data"}``); the client may send exactly one frame,
``{"type": "ping"}``, and gets ``{"type": "pong"}`` back. Anything else it sends is
ignored rather than treated as an error, because a browser extension or a proxy
keep-alive must not be able to close an operator's cockpit.

Four things here are load-bearing:

**Subscribe first, then build the hello.** ``bus.recent()`` and the subscriber queue are
two views of the same stream. Reading the buffer *before* subscribing leaves a window in
which an event lands in neither — the one event the operator most wants to see, since it
arrived at the exact moment they hit reload. Subscribing first closes the gap and opens
the opposite one, duplicates, which is why the hello records the highest ``seq`` it saw
and the pump drops anything at or below it. A gap is invisible; a duplicate is not.

**Reading and writing run concurrently.** A naive ``receive`` → ``send`` loop only writes
when the client happens to speak, so the stream would freeze between the client's 25 s
pings and every event in that window would arrive in a burst. Two tasks race under
``asyncio.wait(FIRST_COMPLETED)`` and the loser is left pending, never cancelled
mid-frame.

**The subscriber queue is always released.** ``events.EventBus`` evicts the oldest
envelope from a full queue, so one abandoned subscriber does not merely leak memory — it
counts a drop on every emit for as long as the process lives, and ``bus.dropped`` (the
number an operator reads when events go missing) becomes noise. The ``finally`` block is
the whole point of the structure.

**One account ticker, process-wide.** ``ACCOUNT_TICK`` is a periodic MT5 read, and MT5's
IPC is a single serialized channel shared with the agent's order flow. A second ticker
would double that load for zero information. :func:`ensure_account_ticker` registers the
task under one key in ``Runtime.tasks`` and is a no-op if it is already running, so the
lifespan owns it and a connecting WebSocket can only ever confirm it is alive.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from typing import Any, Final

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import events
from app.config import Settings
from app.events import EventBus, bus
from app.runtime import STATUS_KEYS, Runtime
from app.schemas import AccountOut
from app.util.logging import get_logger

__all__ = [
    "ACCOUNT_TICK_TASK",
    "account_tick_once",
    "build_account_snapshot",
    "build_hello",
    "ensure_account_ticker",
    "router",
    "stop_account_ticker",
]

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["stream"])

#: How many envelopes the hello frame replays (CONTRACTS §19).
HELLO_RECENT: Final[int] = 100

#: How deep into the ring buffer the hello looks before selecting those envelopes.
#: Larger than HELLO_RECENT on purpose — see :func:`_hello_recent`.
HELLO_SCAN: Final[int] = 500

#: Key the single account ticker is registered under in ``Runtime.tasks``.
ACCOUNT_TICK_TASK: Final[str] = "account_tick"

#: A tick failure is logged in full the first time and then once every N cycles, so a
#: sidecar that is down for an hour costs the log file twelve lines instead of twelve
#: hundred — while never going completely silent.
TICK_LOG_EVERY: Final[int] = 20

#: Close codes. 1011 is "internal error": the app is up but not yet wired.
CLOSE_NOT_READY: Final[int] = 1011


# ---------------------------------------------------------------------------
# Frame encoding
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """Last-resort encoder for a value that slipped into an envelope un-serialised."""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:  # noqa: BLE001 - the fallback below always succeeds
            pass
    return str(value)


def _json_safe(value: Any) -> Any:
    """Return `value` with non-finite floats replaced by None and mappings normalised.

    ``NaN`` and ``Infinity`` are valid Python floats and invalid JSON: ``json.dumps``
    emits them as bare ``NaN`` tokens, ``JSON.parse`` rejects the frame, and the client
    silently drops an event it was told it would receive. MT5 can report a non-finite
    ``margin_level``, so this is a real path, not a theoretical one.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def encode_frame(payload: dict[str, Any]) -> str | None:
    """Encode one frame, or return None when it cannot be made into valid JSON."""
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        pass
    try:
        return json.dumps(
            _json_safe(payload), ensure_ascii=False, allow_nan=False, default=_json_default
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - both encoders failing
        log.warning("stream_frame_unencodable type=%s error=%s", payload.get("type"), exc)
        return None


async def _send(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    """Send one frame; return False when the socket is gone.

    An envelope that cannot be encoded is dropped on its own — one malformed payload
    must not take the operator's whole stream down with it.
    """
    text = encode_frame(payload)
    if text is None:
        return True
    try:
        await websocket.send_text(text)
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        return False
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        # uvicorn raises its own ClientDisconnected, which is not a subclass of any of the
        # three above, so a browser simply closing its tab produced an unhandled ASGI
        # traceback in the log. Matched by name rather than by importing a private class,
        # which would break on a uvicorn upgrade. Anything else still propagates.
        if type(exc).__name__ == "ClientDisconnected":
            return False
        raise
    return True


# ---------------------------------------------------------------------------
# The hello frame
# ---------------------------------------------------------------------------


def _hello_recent(event_bus: EventBus) -> tuple[list[dict[str, Any]], int]:
    """Return (envelopes to replay, highest seq observed) for the hello frame.

    The replay is the newest ``HELLO_RECENT`` envelopes with one deliberate exception:
    consecutive ``account.tick`` envelopes collapse to the freshest one. A tick fires
    every ``ACCOUNT_TICK_SECONDS`` (3 s by default), so a literal "last 100" taken after
    five idle minutes would be one hundred account snapshots and not a single signal,
    run or fill — exactly the history a reconnecting client opened the socket to rebuild.
    Each tick is a complete snapshot that supersedes the one before it, so nothing is
    lost by replaying only the last.

    The second element is the highest ``seq`` present in the buffer, replayed or not.
    Everything at or below it was already offered to the client, so the pump suppresses
    it if the same envelope also arrives on the subscriber queue.
    """
    scanned = event_bus.recent(HELLO_SCAN)
    high_seq = 0
    for envelope in scanned:
        seq = int(envelope.get("seq", 0) or 0)
        if seq > high_seq:
            high_seq = seq

    selected: list[dict[str, Any]] = []
    seen_tick = False
    for envelope in reversed(scanned):
        if envelope.get("type") == events.ACCOUNT_TICK:
            if seen_tick:
                continue
            seen_tick = True
        selected.append(envelope)
        if len(selected) >= HELLO_RECENT:
            break
    selected.reverse()
    return selected, high_seq


async def build_hello(runtime: Runtime | None, event_bus: EventBus) -> tuple[dict[str, Any], int]:
    """Build the hello frame and the sequence number the pump must start above."""
    status: dict[str, Any] = {key: {"ok": False, "error": None} for key in STATUS_KEYS}
    armed = False
    if runtime is not None:
        try:
            snapshot = await runtime.snapshot_status()
        except Exception as exc:  # noqa: BLE001 - the hello must go out regardless
            log.warning("stream_status_unavailable error=%s", exc)
        else:
            for key in STATUS_KEYS:
                entry = snapshot.get(key)
                if isinstance(entry, dict):
                    status[key] = {"ok": bool(entry.get("ok", False)), "error": entry.get("error")}
            armed = bool(snapshot.get("armed", False))

    recent, high_seq = _hello_recent(event_bus)
    frame: dict[str, Any] = {
        "type": "hello",
        "ts": time.time(),
        "data": {"status": status, "armed": armed, "recent": recent},
    }
    return frame, high_seq


# ---------------------------------------------------------------------------
# Client frames
# ---------------------------------------------------------------------------


def _frame_text(message: dict[str, Any]) -> str | None:
    """Extract the text of one received ASGI websocket message, if it carries any."""
    text = message.get("text")
    if isinstance(text, str):
        return text
    raw = message.get("bytes")
    if isinstance(raw, (bytes, bytearray)):
        try:
            return bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


async def _handle_client_message(websocket: WebSocket, message: dict[str, Any]) -> bool:
    """Act on one client frame; return False when the connection is finished.

    The contract admits exactly one client frame. Everything else — a binary frame,
    invalid JSON, an unknown type — is ignored in silence, which is the only behaviour
    that cannot be used to knock a dashboard offline.
    """
    if message.get("type") == "websocket.disconnect":
        return False
    text = _frame_text(message)
    if text is None:
        return True
    try:
        payload = json.loads(text)
    except ValueError:
        return True
    if isinstance(payload, dict) and payload.get("type") == "ping":
        return await _send(websocket, {"type": "pong", "ts": time.time()})
    return True


# ---------------------------------------------------------------------------
# The pump
# ---------------------------------------------------------------------------


async def _cancel(task: asyncio.Task | None) -> None:
    """Cancel one pending task and absorb whatever it raises on the way out."""
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _pump(
    websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]], min_seq: int
) -> None:
    """Forward bus envelopes and answer pings until either side hangs up.

    Reading and writing are separate tasks raced with ``FIRST_COMPLETED``; the one that
    did not win stays pending and is re-awaited on the next pass, so no frame is ever
    consumed and thrown away.
    """
    reader: asyncio.Task | None = asyncio.create_task(websocket.receive(), name="sp-stream-read")
    writer: asyncio.Task | None = asyncio.create_task(queue.get(), name="sp-stream-write")
    try:
        while True:
            done, _pending = await asyncio.wait(
                {task for task in (reader, writer) if task is not None},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if reader is not None and reader in done:
                message = reader.result()  # WebSocketDisconnect surfaces here.
                reader = None
                if not await _handle_client_message(websocket, message):
                    return
                reader = asyncio.create_task(websocket.receive(), name="sp-stream-read")
            if writer is not None and writer in done:
                envelope = writer.result()
                writer = None
                if int(envelope.get("seq", 0) or 0) > min_seq:
                    if not await _send(websocket, envelope):
                        return
                writer = asyncio.create_task(queue.get(), name="sp-stream-write")
    finally:
        await _cancel(reader)
        await _cancel(writer)


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def _runtime_of(websocket: WebSocket) -> Runtime | None:
    """Return the Runtime on the application state, or None before the lifespan ran."""
    return getattr(websocket.app.state, "runtime", None)


@router.websocket("/stream")
async def stream_endpoint(websocket: WebSocket) -> None:
    """Serve the dashboard WebSocket: hello, then every event, until the client leaves."""
    runtime = _runtime_of(websocket)
    if runtime is None:
        # Refuse the handshake rather than accepting and closing: the client's backoff
        # keeps growing instead of resetting to 1 s against a half-started process.
        await websocket.close(code=CLOSE_NOT_READY, reason="runtime is not initialised")
        return

    event_bus = runtime.bus or bus
    await websocket.accept()

    # Subscribe BEFORE reading the buffer, so nothing can fall between the two.
    queue = event_bus.subscribe()
    try:
        ensure_account_ticker(runtime)
        frame, min_seq = await build_hello(runtime, event_bus)
        if not await _send(websocket, frame):
            return
        await _pump(websocket, queue, min_seq)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad socket must not surface as a 500
        log.warning("stream_failed error=%s", exc)
    finally:
        # A leaked subscriber queue never drains: the bus evicts its oldest envelope on
        # every emit, forever, and counts a drop each time.
        event_bus.unsubscribe(queue)
        with contextlib.suppress(RuntimeError, ConnectionError):
            await websocket.close()


# ---------------------------------------------------------------------------
# The account ticker
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float:
    """Return `value` as a JSON-safe float, mapping NaN/Infinity and junk onto 0.0.

    ``AccountOut`` declares every one of these fields a number, and the dashboard's
    types agree, so a non-finite reading is normalised here rather than sent as ``null``
    for the client to guess at. It also keeps the fast JSON path fast: ``encode_frame``
    only walks a payload when it has to.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _rows(result: Any) -> tuple[Any, ...]:
    """Normalise an MT5 collection result into a tuple of rows."""
    if result is None:
        return ()
    if isinstance(result, (list, tuple)):
        return tuple(result)
    return (result,)


async def _mt5_reachable(runtime: Runtime) -> bool:
    """Report whether the sidecar is worth calling right now.

    The status map is read, never written: whoever owns the health poll owns that dot,
    and two writers with different error strings would flap it against each other. When
    the dot is already green this costs nothing; when it is red it costs one ``ping``.
    """
    entry = runtime.status.get("mt5")
    if isinstance(entry, dict) and entry.get("ok"):
        return True
    rpc = getattr(runtime, "rpc", None)
    if rpc is None:
        return False
    return await rpc.ping()


async def build_account_snapshot(runtime: Runtime) -> dict[str, Any] | None:
    """Return the live account in the ``AccountOut`` shape, or None when MT5 is down.

    The figures come from ``Executor.get_account()``, which is the one place that knows
    what "today" means on the broker's clock (BUILD_PROMPT §2); this function only
    renames its keys onto the contract's field names so the dashboard reads the same
    shape from the socket that ``GET /api/trading/account`` gives it over REST.
    """
    executor = getattr(runtime, "executor", None)
    if executor is None:
        return None
    if not await _mt5_reachable(runtime):
        return None

    account = await executor.get_account()
    pending = len(_rows(await runtime.rpc.orders_get()))
    snapshot = AccountOut(
        login=account.get("login"),
        name=str(account.get("name") or ""),
        server=str(account.get("server") or ""),
        currency=str(account.get("currency") or ""),
        leverage=int(account.get("leverage") or 0),
        balance=_finite(account.get("balance")),
        equity=_finite(account.get("equity")),
        margin=_finite(account.get("margin")),
        margin_free=_finite(account.get("margin_free")),
        margin_level=_finite(account.get("margin_level")),
        profit=_finite(account.get("floating_profit")),
        margin_mode=str(account.get("margin_mode") or ""),
        open_positions=int(account.get("open_positions") or 0),
        pending_orders=pending,
        today_profit=_finite(account.get("realized_pl_today")),
        server_time=account.get("server_time"),
    )
    return snapshot.model_dump(mode="json")


async def account_tick_once(runtime: Runtime) -> bool:
    """Emit one ``account.tick``; return whether an envelope actually went out.

    Nothing is read from MT5 while no one is subscribed. The dashboard is the only
    consumer of this event, and polling a single-threaded IPC channel every three
    seconds to feed an empty room would contend with the agent's own order flow for
    nothing.
    """
    if runtime.bus.subscriber_count() <= 0:
        return False
    snapshot = await build_account_snapshot(runtime)
    if snapshot is None:
        return False
    runtime.bus.emit(events.ACCOUNT_TICK, account=snapshot)
    return True


async def _account_tick_loop(runtime: Runtime) -> None:
    """Run :func:`account_tick_once` every ``ACCOUNT_TICK_SECONDS`` until cancelled."""
    settings: Settings = runtime.settings
    interval = max(0.5, float(settings.ACCOUNT_TICK_SECONDS))
    failures = 0
    log.info("account_ticker_started interval_s=%.1f", interval)
    try:
        while True:
            started = time.monotonic()
            try:
                await account_tick_once(runtime)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad cycle never kills the task
                failures += 1
                if failures == 1 or failures % TICK_LOG_EVERY == 0:
                    log.warning("account_tick_failed count=%d error=%s", failures, exc)
            else:
                if failures:
                    log.info("account_tick_recovered after=%d", failures)
                failures = 0
            # Sleep the remainder of the interval, so a slow read paces the loop instead
            # of stacking a queue of overdue ticks behind it.
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
    except asyncio.CancelledError:
        raise
    finally:
        log.info("account_ticker_stopped")


def ensure_account_ticker(runtime: Runtime) -> asyncio.Task:
    """Start the single process-wide account ticker, or return the running one.

    Registration happens with no await in between, so two simultaneous callers cannot
    both create a task. Safe to call from the lifespan and from every connection.
    """
    task = runtime.tasks.get(ACCOUNT_TICK_TASK)
    if task is not None and not task.done():
        return task
    task = asyncio.create_task(_account_tick_loop(runtime), name=ACCOUNT_TICK_TASK)
    runtime.tasks[ACCOUNT_TICK_TASK] = task
    return task


async def stop_account_ticker(runtime: Runtime) -> None:
    """Cancel the account ticker and wait for it to finish; a no-op when it never ran."""
    task = runtime.tasks.pop(ACCOUNT_TICK_TASK, None)
    await _cancel(task)
