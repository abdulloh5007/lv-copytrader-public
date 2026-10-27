"""The FastAPI application: lifespan wiring, one error envelope, and the built SPA.

Nothing in this module decides anything about trading, ingest or the model. It builds the
service container, starts and stops every long-lived component in a defined order, mounts the
routers that CONTRACTS.md §18 lists, and serves the dashboard. Every other behaviour lives in
the module that owns it.

Two properties are deliberate and load-bearing:

* **One failed startup step never stops the app from serving.** A missing sidecar, a Telegram
  session that will not connect or a provider that is not configured yet must all end as a red
  status dot with a sentence next to it — the operator fixes those in the dashboard, which they
  can only reach if the dashboard is up. So every step is guarded, in order, and the failure is
  logged and emitted as ``system.warning`` rather than raised.
* **The Telethon client is created here, inside the lifespan** (BUILD_PROMPT §3.6). Telethon
  binds to the loop that ran ``connect()`` and re-checks it on every request; creating it at
  import time or inside a second ``asyncio.run()`` is the ``RuntimeError: The asyncio event
  loop must not change after connection`` that this comment exists to prevent. See the note at
  the bottom of this file about ``--workers 1`` and ``--reload``.
"""

# Copyright 2026 Abdulloh (github.com/abdulloh5007)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE at the repository
# root. This software places real orders on a real brokerage account; NOTICE states the
# trading risk the licence itself does not cover.

from __future__ import annotations

import asyncio
import html
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Final

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__, crypto, events
from app.api import auth as auth_api
from app.api import demo as demo_api
from app.api import history as history_api
from app.api import provider as provider_api
from app.api import settings as settings_api
from app.api import stream as stream_api
from app.api import telegram as telegram_api
from app.api import trading as trading_api
from app.api import waitlist as waitlist_api
from app.config import settings
from app.db import dispose_db, init_db, session_scope
from app.errors import SignalPilotError, to_http
from app.models import Source, SystemState, default_system_state, utcnow
from app.reviewer.worker import ChannelReviewer
from app.runtime import Runtime
from app.schemas import HealthOut, StatusMapOut
from app.telegram.client import TelegramService
from app.telegram.ingest import Ingestor
from app.telegram.notifier import Notifier
from app.trading.constants import margin_mode_name
from app.trading.executor import Executor
from app.trading.guards import SlWatchdog
from app.trading.quote_cache import run_quote_cache
from app.trading.reconcile import reconcile
from app.trading.rpc import Mt5Rpc
from app.trading.symbols import SymbolResolver
from app.util.auth import AuthMiddleware, LoginRateLimiter
from app.util.security import SecurityMiddleware
from app.util.demo_guard import DemoGuard
from app.public_demo.service import PublicDemo
from app.util.logging import get_logger, register_secret, setup_logging

log = get_logger(__name__)

#: Name the account-tick task is registered under in ``Runtime.tasks``.
ACCOUNT_TICK_TASK: Final[str] = "account_tick"
BROKER_BRINGUP_TASK: Final[str] = "broker_bringup"
TELEGRAM_BRINGUP_TASK: Final[str] = "telegram_bringup"
QUOTE_CACHE_TASK: Final[str] = "quote_cache"

#: Hard ceiling on the startup catch-up. Telethon fetching twenty messages takes under a
#: second; a network that has stopped answering must not hold the port closed for minutes.
CATCHUP_TIMEOUT_SECONDS: Final[float] = 90.0

#: Longest error sentence copied into a log line or a ``system.warning``.
MAX_ERROR_CHARS: Final[int] = 400

#: Env flag that turns on CORS for `npm run dev` on port 5173. Off by default: the SPA is
#: served from this same origin in every real deployment, and Vite's dev server proxies
#: /api to :8000 anyway (frontend/vite.config.ts), so this is only for a dev setup that
#: points the browser straight at :8000 from :5173.
DEV_CORS_ENV: Final[str] = "SP_DEV_CORS"

#: Comma-separated override for the dev origins allowed when DEV_CORS_ENV is on.
DEV_CORS_ORIGINS_ENV: Final[str] = "SP_DEV_CORS_ORIGINS"

#: Default dev origins, both spellings of localhost because a browser treats them as
#: different origins and people type whichever one they remember.
DEFAULT_DEV_ORIGINS: Final[tuple[str, ...]] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

#: Values that read as "on" for an env flag.
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: HTTP status -> the machine-readable ``error.code`` the frontend switches on.
_HTTP_ERROR_CODES: Final[dict[int, str]] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limit",
    500: "error",
    502: "upstream_error",
    503: "unavailable",
    504: "upstream_timeout",
}


# =======================================================================================
# Small helpers
# =======================================================================================


def _short(exc: BaseException) -> str:
    """Render an exception as one short sentence fit for a log line or a warning event."""
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]


def _warn(runtime: Runtime | None, reason: str, exc: BaseException) -> None:
    """Emit a ``system.warning`` for a failed step; never raises, whatever the bus does."""
    try:
        bus = runtime.bus if runtime is not None else events.bus
        bus.emit(events.SYSTEM_WARNING, reason=reason, message=_short(exc))
    except Exception:  # noqa: BLE001 - a warning that fails must stay silent, not cascade
        pass


async def _step(runtime: Runtime | None, name: str, awaitable: Awaitable[Any]) -> tuple[bool, Any]:
    """Run one guarded startup step and report (ok, result) instead of raising."""
    try:
        return True, await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - one failed step must never stop the app serving
        log.exception("startup_step_failed step=%s", name)
        _warn(runtime, f"startup_{name}_failed", exc)
        return False, None


def _env_flag(name: str) -> bool:
    """Return whether an environment flag is set to one of the truthy spellings."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _dev_cors_origins() -> list[str]:
    """Return the origins dev CORS allows, from the env override or the localhost pair."""
    raw = os.environ.get(DEV_CORS_ORIGINS_ENV, "").strip()
    if not raw:
        return list(DEFAULT_DEV_ORIGINS)
    return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]


async def _persist_state(**fields: Any) -> None:
    """Write volatile runtime facts onto the single ``system_state`` row; never raises.

    The row exists so a restart can show last-known state before MT5 answers again, and so
    ``GET /api/trading/state`` has a margin mode and an account currency to report.
    """
    if not fields:
        return
    try:
        async with session_scope() as session:
            row = await session.get(SystemState, 1)
            if row is None:
                row = default_system_state()
                session.add(row)
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = utcnow()
    except Exception as exc:  # noqa: BLE001 - a bookkeeping write never breaks a caller
        log.warning("system_state_write_failed fields=%s error=%s", ",".join(fields), exc)


async def _active_source_id() -> int | None:
    """Return the active signal source's id, or None when the operator has not picked one."""
    async with session_scope() as session:
        result = await session.execute(
            select(Source.id).where(Source.is_active.is_(True)).limit(1)
        )
        row = result.scalar_one_or_none()
    return int(row) if row is not None else None


def _effective_margin_mode(raw: Any) -> tuple[str, str, bool]:
    """Return (effective, detected, forced) margin mode, honouring FORCE_MARGIN_MODE (§1.3)."""
    detected = margin_mode_name(int(raw))
    forced = (settings.FORCE_MARGIN_MODE or "").strip().lower()
    if forced:
        return forced, detected, True
    return detected, detected, False


# =======================================================================================
# Startup steps
# =======================================================================================


def _build_runtime() -> Runtime:
    """Assemble the shared service container. Construction only — no I/O happens here."""
    rpc = Mt5Rpc(
        settings.SIDECAR_URL,
        settings.SIDECAR_TOKEN,
        timeout=settings.RPC_TIMEOUT_SECONDS,
        connect_timeout=settings.RPC_CONNECT_TIMEOUT_SECONDS,
    )
    resolver = SymbolResolver(rpc, settings)
    runtime = Runtime(settings=settings, rpc=rpc, resolver=resolver, bus=events.bus)
    # Assigned after construction because each of these takes the runtime itself: the
    # container is the one thing they all share. TelegramService takes a getter because it
    # is held *by* the runtime, and resolving it eagerly would close the import cycle.
    runtime.executor = Executor(runtime)
    runtime.telegram = TelegramService(lambda: runtime, settings)
    runtime.ingestor = Ingestor(runtime)
    runtime.reviewer = ChannelReviewer(runtime)
    runtime.notifier = Notifier(runtime)
    return runtime


async def _restore_status(runtime: Runtime) -> None:
    """Seed the status dots from the database so a restart does not blank the header.

    The MT5 dot is decided a moment later by an actual ping, and the Telegram dot by an
    actual connect. The provider dot has no cheap probe — probing costs a network round trip
    to a third party — so its last known verdict is restored from ``system_state`` and the
    provider routes overwrite it the next time the operator tests the connection.
    """
    async with session_scope() as session:
        row = await session.get(SystemState, 1)
    if row is not None and bool(row.provider_ok):
        runtime.set_status("provider", True, None)
    else:
        stored_error = (row.provider_error if row is not None else None) or (
            "no AI provider configured yet — add a key and pick a model in Settings"
        )
        runtime.set_status("provider", False, stored_error)
    # Loads `armed` and the needs_reentry list into runtime.status, so the very first
    # WebSocket hello and the first /api/health answer with the truth.
    await runtime.snapshot_status()


async def _start_mt5(runtime: Runtime) -> bool:
    """Open the sidecar client, set the MT5 dot, and read the facts that only change rarely."""
    await runtime.rpc.start()
    if not await runtime.rpc.ping():
        message = (
            "the MT5 sidecar did not answer — check that the mt5-sidecar container is up and "
            f"that SIDECAR_URL ({settings.SIDECAR_URL}) and SIDECAR_TOKEN match it"
        )
        runtime.set_status("mt5", False, message)
        await _persist_state(mt5_connected=False, mt5_error=message)
        log.warning("mt5_unreachable url=%s", settings.SIDECAR_URL)
        return False

    runtime.set_status("mt5", True, None)
    symbols = 0
    try:
        symbols = await runtime.resolver.refresh(force=True)
    except Exception as exc:  # noqa: BLE001 - an empty symbol cache refills on first use
        log.warning("symbol_refresh_failed error=%s", exc)

    fields: dict[str, Any] = {"mt5_connected": True, "mt5_error": None}
    try:
        info = await runtime.rpc.account_info()
        effective, detected, forced = _effective_margin_mode(info.margin_mode)
        fields["margin_mode"] = effective
        fields["account_currency"] = str(info.currency)
        log.info(
            "mt5_ready login=%s server=%s currency=%s margin_mode=%s detected=%s forced=%s symbols=%d",
            int(info.login),
            str(info.server),
            str(info.currency),
            effective,
            detected,
            forced,
            symbols,
        )
    except Exception as exc:  # noqa: BLE001 - a ping that worked is still a working sidecar
        log.warning("account_info_failed error=%s", exc)
    await _persist_state(**fields)
    return True


async def _start_watchdog(runtime: Runtime) -> SlWatchdog:
    """Build the stop-loss watchdog and start its loop (BUILD §6.4)."""
    watchdog = SlWatchdog(runtime)
    await watchdog.start()
    return watchdog


async def _start_telegram(runtime: Runtime) -> None:
    """Create and connect the Telethon client, start the ingest worker, then catch up (§3.6, §8.4)."""
    try:
        await runtime.telegram.start()
        status = runtime.status.get("telegram")
        connected = bool(status.get("ok")) if isinstance(status, dict) else False
        error = status.get("error") if isinstance(status, dict) else None
        await _persist_state(telegram_connected=connected, telegram_error=error)

        # The pool starts either way. It is up to three consumers of the one ingest queue, and
        # nothing may enqueue behind its back later just because Telegram was down at boot.
        # The channel reviewer is a separate task: it does not take one of those slots.
        await runtime.ingestor.start()

        if not connected:
            log.info("catchup_skipped reason=telegram_not_connected")
            return

        # `connected` means the connect call returned, not that the client is servable yet.
        # Catch-up ran ~2 s ahead of `telegram_ready` and failed with "Telegram is reconnecting",
        # which is the one job catch-up exists to do (§8.4: pick up what arrived while we were
        # down). A second pass covered it, but a race that only self-heals by luck is not a design.
        for _ in range(20):
            service = getattr(runtime, "telegram", None)
            client = getattr(service, "client", None)
            if client is not None and client.is_connected():
                break
            await asyncio.sleep(0.5)

        source_id = await _active_source_id()
        if source_id is None:
            log.info("catchup_skipped reason=no_active_source")
            return
        try:
            recorded = await asyncio.wait_for(
                runtime.ingestor.catch_up(), timeout=CATCHUP_TIMEOUT_SECONDS
            )
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("catchup_timeout source_id=%d seconds=%.0f", source_id, CATCHUP_TIMEOUT_SECONDS)
            runtime.bus.emit(
                events.SYSTEM_WARNING,
                reason="catchup_failed",
                source_id=source_id,
                message="catch-up timed out",
            )
            return
        log.info("catchup_finished source_id=%d recorded=%d", source_id, recorded)
    finally:
        # After connect (or after we know it failed). A thin database sample can now
        # read Telegram. A brief that is already ready makes this a no-op.
        await _kick_reviewer(runtime)


async def _kick_reviewer(runtime: Runtime) -> None:
    """Ask the reviewer to study the active source once, if it does not have a brief yet."""
    reviewer = getattr(runtime, "reviewer", None)
    if reviewer is None:
        return
    try:
        await reviewer.kick()
    except Exception as exc:  # noqa: BLE001 - a study must not fail Telegram bring-up
        log.warning("channel_reviewer_kick_failed error=%s", exc)


# =======================================================================================
# The account tick
# =======================================================================================


async def _pending_order_count(runtime: Runtime) -> int:
    """Count live pending orders; 0 when the terminal cannot answer."""
    try:
        rows = await runtime.rpc.orders_get()
    except Exception as exc:  # noqa: BLE001 - a missing count must not break the tick
        log.debug("orders_get_failed error=%s", exc)
        return 0
    if rows is None:
        return 0
    if isinstance(rows, (tuple, list)):
        return len(rows)
    return 1


async def _account_snapshot(runtime: Runtime) -> dict[str, Any]:
    """Return the `AccountOut` payload the dashboard reads off `account.tick` (§18, §19).

    ``Executor.get_account`` is the model-facing shape and names the same numbers for the
    model's benefit — ``floating_profit`` and ``realized_pl_today``. The dashboard's typed
    ``AccountOut`` calls them ``profit`` and ``today_profit`` and also wants the pending-order
    count, so both spellings travel together: the extra keys are additive, and no consumer of
    either name has to know about the other.
    """
    raw = await runtime.executor.get_account()
    payload = dict(raw)
    payload.setdefault("profit", raw.get("floating_profit", 0.0))
    payload.setdefault("today_profit", raw.get("realized_pl_today", 0.0))
    if "pending_orders" not in payload:
        payload["pending_orders"] = await _pending_order_count(runtime)
    return payload


async def _after_mt5_reconnect(runtime: Runtime) -> None:
    """Re-read the symbol table and reconcile after the sidecar comes back (BUILD §8)."""
    log.info("mt5_reconnected — refreshing symbols and reconciling")
    try:
        await runtime.resolver.refresh(force=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("symbol_refresh_failed error=%s", exc)
    fields: dict[str, Any] = {}
    try:
        info = await runtime.rpc.account_info()
        effective, _detected, _forced = _effective_margin_mode(info.margin_mode)
        fields = {"margin_mode": effective, "account_currency": str(info.currency)}
    except Exception as exc:  # noqa: BLE001
        log.warning("account_info_failed error=%s", exc)
    if fields:
        await _persist_state(**fields)
    try:
        counts = await reconcile(runtime, cause="reconnect")
        log.info("reconcile_after_reconnect %s", counts)
    except Exception as exc:  # noqa: BLE001 - reconcile reports its own failures too
        log.warning("reconcile_after_reconnect_failed error=%s", exc)


async def _account_tick_loop(runtime: Runtime) -> None:
    """Push the account snapshot to the dashboard and keep the MT5 dot honest.

    Two cadences, one task. With a dashboard open the full snapshot goes out every
    ``ACCOUNT_TICK_SECONDS``, and its round trip doubles as the health check. With nobody
    watching there is no reason to pull balances and today's deals every three seconds, so the
    loop falls back to a bare ``ping`` every ``RPC_HEALTH_SECONDS`` — the dot stays truthful
    while the terminal is left alone.
    """
    interval = max(0.5, float(settings.ACCOUNT_TICK_SECONDS))
    health_every = max(1.0, float(settings.RPC_HEALTH_SECONDS))
    last_probe = 0.0
    was_connected: bool | None = None
    log.info("account_tick_started interval_s=%.1f health_s=%.1f", interval, health_every)

    while True:
        await asyncio.sleep(interval)
        now = time.monotonic()
        watching = runtime.bus.subscriber_count() > 0
        if not watching and (now - last_probe) < health_every:
            continue
        last_probe = now

        error: str | None
        try:
            if watching:
                account = await _account_snapshot(runtime)
                runtime.bus.emit(events.ACCOUNT_TICK, account=account)
                connected, error = True, None
            else:
                connected = await runtime.rpc.ping()
                error = None if connected else "the MT5 sidecar did not answer"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad cycle is a red dot, not a dead task
            connected, error = False, _short(exc)

        runtime.set_status("mt5", connected, error)
        if connected != was_connected:
            await _persist_state(mt5_connected=connected, mt5_error=error)
            if connected and was_connected is False:
                await _after_mt5_reconnect(runtime)
            elif not connected:
                log.warning("mt5_disconnected error=%s", error)
                runtime.bus.emit(
                    events.SYSTEM_WARNING, reason="mt5_disconnected", message=error or "MT5 is not answering"
                )
        was_connected = connected


# =======================================================================================
# Shutdown
# =======================================================================================


async def _cancel_tasks(runtime: Runtime) -> None:
    """Cancel every task the runtime owns and wait for each one to finish."""
    pending = list(runtime.tasks.items())
    for _name, task in pending:
        if not task.done():
            task.cancel()
    for name, task in pending:
        try:
            await task
        except asyncio.CancelledError:
            # Our own cancellation must still propagate; the task's own does not.
            if not task.cancelled():
                raise
        except Exception as exc:  # noqa: BLE001 - shutdown reports, it does not re-raise
            log.warning("shutdown_task_error task=%s error=%s", name, exc)
    runtime.tasks.clear()


async def _shutdown(runtime: Runtime | None, watchdog: SlWatchdog | None) -> None:
    """Wind everything down in reverse order, each step guarded so the next one still runs."""
    log.info("shutdown_started")
    if runtime is not None:
        try:
            await _cancel_tasks(runtime)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown_tasks_failed error=%s", exc)

    if watchdog is not None:
        try:
            await watchdog.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown_watchdog_failed error=%s", exc)

    if runtime is not None:
        for name in ("notifier", "ingestor", "telegram"):
            component = getattr(runtime, name, None)
            if component is None:
                continue
            try:
                await component.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown_component_failed component=%s error=%s", name, exc)
        if runtime.rpc is not None:
            try:
                await runtime.rpc.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown_rpc_failed error=%s", exc)

    try:
        await dispose_db()
    except Exception as exc:  # noqa: BLE001
        log.warning("shutdown_db_failed error=%s", exc)
    log.info("shutdown_complete")


# =======================================================================================
# Lifespan
# =======================================================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start every long-lived component in order, then stop them in reverse."""
    runtime: Runtime | None = None
    watchdog: SlWatchdog | None = None
    boot = time.time()
    try:
        # ---- 1. logging and directories -------------------------------------------------
        try:
            setup_logging(settings.LOG_LEVEL)
            # Registering the two env-borne secrets makes the redactor mask them even when a
            # third-party library prints a URL or a request header containing one.
            register_secret(settings.MT5_PASSWORD)
            register_secret(settings.SIDECAR_TOKEN)
            settings.ensure_dirs()
        except Exception as exc:  # noqa: BLE001
            log.exception("startup_step_failed step=logging")
            _warn(None, "startup_logging_failed", exc)
        log.info(
            "startup version=%s data_dir=%s frontend_dist=%s sidecar=%s log_level=%s",
            __version__,
            settings.DATA_DIR,
            settings.FRONTEND_DIST,
            settings.SIDECAR_URL,
            settings.LOG_LEVEL,
        )

        # ---- 2. SECRET_KEY --------------------------------------------------------------
        try:
            _key, generated = crypto.bootstrap_secret_key(settings.env_file_path)
            if generated:
                log.warning(
                    "secret_key_generated env_path=%s message=%s",
                    settings.env_file_path,
                    "SECRET_KEY was missing, so a new one was generated and written to .env — "
                    "back that file up: losing the key makes every stored secret unreadable",
                )
            else:
                log.info("secret_key_loaded env_path=%s", settings.env_file_path)
        except Exception as exc:  # noqa: BLE001 - unreadable secrets are needs_reentry, not a crash
            log.exception("startup_step_failed step=secret_key")
            _warn(None, "startup_secret_key_failed", exc)

        # ---- 3. database ----------------------------------------------------------------
        db_ok, _ = await _step(None, "db_init", init_db())

        # ---- 4. the service container ---------------------------------------------------
        try:
            runtime = _build_runtime()
            app.state.runtime = runtime
            log.info(
                "runtime_ready components=rpc,resolver,executor,telegram,ingestor,reviewer,notifier"
            )
        except Exception as exc:  # noqa: BLE001 - routes then answer 500 with the error envelope
            app.state.runtime = None
            log.exception("startup_step_failed step=runtime")
            _warn(None, "startup_runtime_failed", exc)

        if runtime is not None and db_ok:
            await _step(runtime, "status_restore", _restore_status(runtime))

        if runtime is not None:
            # The reviewer loop has to exist before Telegram bring-up, because that bring-up
            # kicks a one-shot study when the client is actually connected.
            await _step(runtime, "channel_reviewer", runtime.reviewer.start())

            # ---- 5-7. MT5, reconciliation and Telegram, in the BACKGROUND ---------------
            # None of these three gate the port. Each can take minutes against a cold
            # sidecar or a slow network, and a dashboard nobody can load is worth less than
            # a dashboard whose dots are honestly red. Both tasks live in runtime.tasks so
            # shutdown cancels them like any other.
            try:
                runtime.tasks[BROKER_BRINGUP_TASK] = asyncio.create_task(
                    _broker_bringup(runtime), name=BROKER_BRINGUP_TASK
                )
                runtime.tasks[TELEGRAM_BRINGUP_TASK] = asyncio.create_task(
                    _telegram_bringup(runtime), name=TELEGRAM_BRINGUP_TASK
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("startup_step_failed step=bringup")
                _warn(runtime, "startup_bringup_failed", exc)

            # ---- 8. notifier, SL watchdog, account tick ---------------------------------
            await _step(runtime, "notifier", runtime.notifier.start())
            started, made = await _step(runtime, "sl_watchdog", _start_watchdog(runtime))
            if started:
                watchdog = made
            try:
                runtime.tasks[ACCOUNT_TICK_TASK] = asyncio.create_task(
                    _account_tick_loop(runtime), name=ACCOUNT_TICK_TASK
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("startup_step_failed step=account_tick")
                _warn(runtime, "startup_account_tick_failed", exc)
            try:
                runtime.tasks[QUOTE_CACHE_TASK] = asyncio.create_task(
                    run_quote_cache(runtime), name=QUOTE_CACHE_TASK
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("startup_step_failed step=quote_cache")
                _warn(runtime, "startup_quote_cache_failed", exc)

        app.state.public_demo.start()
        log.info("startup_complete seconds=%.2f", time.time() - boot)
        yield
    finally:
        await app.state.public_demo.stop()
        await _shutdown(runtime, watchdog)


# =======================================================================================
# Error envelope — one shape for every failure the frontend can meet
# =======================================================================================


def _error_response(
    status: int, code: str, message: str, detail: dict[str, Any] | None = None
) -> JSONResponse:
    """Build the single ``{"error": {code, message, detail}}`` envelope of CONTRACTS §18."""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": str(message), "detail": detail or {}}},
    )


def _validation_message(errors: list[Any]) -> str:
    """Render pydantic's error list as one sentence naming the first offending field."""
    if not errors:
        return "the request body is not valid"
    first = errors[0] if isinstance(errors[0], dict) else {}
    location = ".".join(str(part) for part in (first.get("loc") or ()) if part != "body") or "body"
    message = str(first.get("msg") or "is not valid")
    extra = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"{location}: {message}{extra}"


async def _signalpilot_error_handler(request: Request, exc: SignalPilotError) -> JSONResponse:
    """Map every domain error onto its status and the error envelope (`app.errors.to_http`)."""
    status, body = to_http(exc)
    log.warning(
        "api_error path=%s code=%s status=%d message=%s",
        request.url.path,
        exc.code,
        status,
        exc.message,
    )
    return JSONResponse(status_code=status, content=jsonable_encoder(body))


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return a 422 in the same envelope, so the frontend parses one error shape, not two."""
    errors = jsonable_encoder([{k: v for k, v in error.items() if k not in {"input", "ctx"}} for error in exc.errors()])
    log.info("api_validation_error path=%s errors=%d", request.url.path, len(errors))
    return _error_response(422, "validation_error", _validation_message(errors), {"errors": errors})


async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """Re-dress Starlette's `{"detail": ...}` as the same envelope, headers included."""
    detail = exc.detail
    if isinstance(detail, str) and detail:
        message, body_detail = detail, {}
    else:
        message, body_detail = "the request could not be completed", {"detail": jsonable_encoder(detail)}
    code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    response = _error_response(exc.status_code, code, message, body_detail)
    for key, value in (getattr(exc, "headers", None) or {}).items():
        response.headers[key] = value
    return response


# =======================================================================================
# The SPA
# =======================================================================================


def _dev_placeholder(dist: str) -> str:
    """Return the plain page shown when the dashboard bundle has not been built."""
    where = html.escape(dist)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>LV Copytrader — dashboard not built</title>"
        "<style>"
        "body{margin:0;padding:2.5rem 1.5rem;background:#0B0E13;color:#E6EAF2;"
        "font:14px/1.6 system-ui,-apple-system,'Segoe UI',sans-serif}"
        "main{max-width:44rem;margin:0 auto}"
        "h1{font-size:1.35rem;margin:0 0 1rem}"
        "p{color:#8B95A8;margin:0 0 1rem}"
        "code,pre{font-family:ui-monospace,'SF Mono',Menlo,monospace;color:#E6EAF2}"
        "pre{background:#141922;border:1px solid #2A3242;border-radius:8px;padding:.85rem 1rem;"
        "overflow-x:auto}"
        "a{color:#4C9AFF}"
        "</style></head><body><main>"
        "<h1>LV Copytrader backend is running — the dashboard bundle is missing</h1>"
        f"<p>No <code>index.html</code> was found in <code>{where}</code>, so there is nothing "
        "to serve on this path. The API itself is up: try "
        "<a href=\"/api/health\">/api/health</a>.</p>"
        "<p>Build the SPA once and reload this page:</p>"
        "<pre>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>"
        "<p>Then point <code>FRONTEND_DIST</code> at the produced <code>frontend/dist</code> "
        "directory, or run the whole stack with <code>docker compose up --build</code>, which "
        "builds the bundle into the image. While working on the frontend, "
        "<code>npm run dev</code> serves it on port 5173 and proxies <code>/api</code> here.</p>"
        "</main></body></html>"
    )


class SpaAssets(StaticFiles):
    """StaticFiles that answers 404 for a missing directory instead of raising a 500.

    Starlette's own check runs on the FIRST REQUEST, not at mount time, so `check_dir=False`
    only delays the `RuntimeError: StaticFiles directory ... does not exist` until someone
    asks for an asset. Skipping the check entirely is what makes running the backend with no
    built bundle a supported dev state — and it means a bundle built later starts being served
    without restarting the process.
    """

    async def check_config(self) -> None:
        """Deliberately no directory assertion; a missing path is a 404, not a crash."""
        return None


def _safe_child(root: Path, relative: str) -> Path | None:
    """Resolve `relative` inside `root`, or None when it escapes or is not a plain file path."""
    if not relative or relative.endswith("/"):
        return None
    try:
        base = root.resolve(strict=False)
        target = (base / relative).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return None
    if target == base or not target.is_relative_to(base):
        return None
    return target


def spa_shell() -> Response:
    """Return the dashboard shell — the page that boots the client router and the login screen.

    One function with two callers, and that is the point: the catch-all below serves it to a
    logged-in operator, and `app.util.auth.AuthMiddleware` serves the *same* response to an
    unauthenticated navigation so the SPA can render its own login form. A second copy of this
    would be a second answer to "what does a browser get for a page it may not see yet".

    Never cached: the inline theme/locale bootstrap and the hashed asset names inside the shell
    change with every build, and a stale shell points at assets that no longer exist.
    """
    dist = Path(settings.FRONTEND_DIST)
    index = dist / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})
    return HTMLResponse(_dev_placeholder(str(dist)))


def _mount_spa(app: FastAPI) -> None:
    """Serve the built dashboard: hashed assets as static files, everything else as index.html.

    Registered last, after every router, so `/api/...` always wins. The catch-all is what makes
    a hard refresh on `/positions` work: the browser asks the server for a path only the client
    router knows, and the server answers with the shell that contains that router.

    Both the mount and the catch-all resolve from disk per request and tolerate the directory
    not existing — running the backend alone in dev is a normal thing to do, and it must show a
    page explaining how to build the bundle rather than fail at import.
    """
    dist = Path(settings.FRONTEND_DIST)
    app.mount("/assets", SpaAssets(directory=str(dist / "assets"), check_dir=False), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Response:
        """Return a real file when one exists, otherwise the SPA shell."""
        if full_path == "api" or full_path.startswith("api/"):
            return _error_response(404, "not_found", f"no API route for /{full_path}")
        candidate = _safe_child(dist, full_path)
        if candidate is not None and candidate.is_file():
            # Hashed assets are immutable and may be cached; an HTML file is a shell whose
            # asset names change with every build, so it never is.
            headers = {"Cache-Control": "no-cache"} if candidate.suffix == ".html" else None
            return FileResponse(candidate, headers=headers)
        return spa_shell()


# =======================================================================================
# The application
# =======================================================================================


async def _broker_bringup(runtime: Runtime) -> None:
    """Connect to the sidecar and reconcile, off the startup path.

    This is deliberately NOT awaited in the lifespan. Reaching an absent sidecar costs
    RPC_TIMEOUT_SECONDS per call plus retries, and on a first `docker compose up` the
    sidecar spends *minutes* installing MT5 under Wine before it answers anything. Awaiting
    that here would keep uvicorn from accepting a single connection for the whole of it --
    and the dashboard is the only place the operator can watch the bring-up happen or fix
    the credentials that are stopping it. So the app serves immediately, the MT5 dot stays
    red until this task turns it green, and acceptance criterion 1 ("compose up serves the
    dashboard") holds even while the broker is still coming up.
    """
    try:
        await _start_mt5(runtime)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the dot carries the failure; startup does not
        log.exception("bringup_step_failed step=mt5")
        _warn(runtime, "startup_mt5_failed", exc)
    try:
        counts = await reconcile(runtime, cause="startup")
        log.info("reconcile_startup %s", counts)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("bringup_step_failed step=reconcile")
        _warn(runtime, "startup_reconcile_failed", exc)


async def _telegram_bringup(runtime: Runtime) -> None:
    """Connect Telethon, start the ingest worker and catch up -- also off the startup path.

    Same reasoning as the broker: a slow or unreachable Telegram must not hold the port shut.
    """
    try:
        await _start_telegram(runtime)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("bringup_step_failed step=telegram")
        _warn(runtime, "startup_telegram_failed", exc)


def _include_router(app: FastAPI, module: Any, name: str) -> None:
    """Include one API router, adding the `/api` prefix only if its routes lack one.

    Every path in CONTRACTS §18 lives under `/api`. A router that already declares that prefix
    is included as-is; one that declares bare paths is mounted under `/api` rather than landing
    at the site root where the SPA catch-all would swallow it.
    """
    router = getattr(module, "router", None)
    if router is None:
        raise RuntimeError(f"app.api.{name} does not define `router`")
    paths = [str(getattr(route, "path", "")) for route in getattr(router, "routes", [])]
    prefix = "" if any(path.startswith("/api") for path in paths) else "/api"
    app.include_router(router, prefix=prefix)


def create_app() -> FastAPI:
    """Build the FastAPI application: middleware, handlers, routers, health, SPA."""
    app = FastAPI(
        title="LV Copytrader",
        version=__version__,
        summary="An AI trader that reads a Telegram signal channel and drives MetaTrader 5.",
        lifespan=lifespan,
        # Kept under /api so the SPA owns every other path and the catch-all below stays a
        # single rule with a single exception.
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # ---- dev-only CORS ---------------------------------------------------------------
    if _env_flag(DEV_CORS_ENV):
        origins = _dev_cors_origins()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        log.warning("dev_cors_enabled origins=%s", ",".join(origins))

    # ---- one error shape --------------------------------------------------------------
    app.add_exception_handler(SignalPilotError, _signalpilot_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)

    # ---- authentication -----------------------------------------------------------------
    # Outermost on purpose: it must sit in front of the routers, the static files AND the
    # SPA catch-all, so nothing is reachable before a login. Pure ASGI, so it covers the
    # WebSocket too -- BaseHTTPMiddleware would not, and /api/stream is the live feed of
    # every run, position and account tick.
    #
    # `shell` is what keeps the login page reachable: an unauthenticated navigation is
    # answered with the dashboard bundle (which renders the login form and posts to
    # /api/auth/login) instead of a 401 the browser would paint as a blank error page.
    app.state.public_demo = PublicDemo(lambda: getattr(app.state, "runtime", None))
    from app.util.auth_state import SessionGuard
    app.state.auth_sessions = SessionGuard(
        Path(settings.DATA_DIR) / "auth-sessions.json",
        lambda: settings.SECRET_KEY or "",
        lambda: (settings.SP_AUTH_USER, settings.SP_AUTH_PASSWORD),
    )
    app.state.login_rate_limiter = LoginRateLimiter(
        storage_path=Path(settings.DATA_DIR) / "auth-rate.json",
        secret=lambda: settings.SECRET_KEY or "",
    )
    app.add_middleware(
        AuthMiddleware,
        username=settings.SP_AUTH_USER,
        password=settings.SP_AUTH_PASSWORD,
        secret=lambda: app.state.auth_sessions.secret,
        sessions=app.state.auth_sessions,
        allow_unconfigured=settings.SP_ALLOW_UNAUTHENTICATED,
        shell=spa_shell,
        limiter=app.state.login_rate_limiter,
    )
    app.add_middleware(DemoGuard, limits=app.state.public_demo.limits)
    allowed_hosts = [h.strip() for h in settings.SP_ALLOWED_HOSTS.split(",") if h.strip()]
    if allowed_hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts, www_redirect=False)
    app.add_middleware(
        SecurityMiddleware, frontend_dist=settings.FRONTEND_DIST,
        dev_origins=_dev_cors_origins() if _env_flag(DEV_CORS_ENV) else (),
    )
    if settings.SIDECAR_TOKEN in ("change-me", "changeme", "", "your-token"):
        log.warning(
            "SIDECAR_TOKEN is still the shipped placeholder — the sidecar will refuse to start. "
            "Generate one with 'openssl rand -base64 32' and put it in .env"
        )

    if settings.SP_AUTH_USER and settings.SP_AUTH_PASSWORD:
        log.info("dashboard authentication is ON (user=%s)", settings.SP_AUTH_USER)
    else:
        log.warning(
            "dashboard credentials are missing — private API remains locked unless explicit dev mode; "
            "set SP_AUTH_USER and SP_AUTH_PASSWORD in .env first"
        )

    # ---- routers (CONTRACTS §18, §19) --------------------------------------------------
    _include_router(app, auth_api, "auth")
    _include_router(app, waitlist_api, "waitlist")
    _include_router(app, demo_api, "demo")
    _include_router(app, settings_api, "settings")
    _include_router(app, telegram_api, "telegram")
    _include_router(app, provider_api, "provider")
    _include_router(app, trading_api, "trading")
    _include_router(app, history_api, "history")
    _include_router(app, stream_api, "stream")

    @app.get("/api/health", response_model=HealthOut, tags=["health"])
    async def health(request: Request) -> HealthOut:
        """Liveness plus everything the header needs on first paint. Never returns a secret."""
        runtime: Runtime | None = getattr(request.app.state, "runtime", None)
        if runtime is None:
            return HealthOut(ok=False, version=__version__, uptime_s=0.0)
        status = await runtime.snapshot_status()
        return HealthOut(
            ok=True,
            version=__version__,
            uptime_s=round(max(0.0, time.time() - runtime.started_at), 3),
            status=StatusMapOut(
                mt5=status.get("mt5") or {},
                telegram=status.get("telegram") or {},
                provider=status.get("provider") or {},
            ),
            armed=bool(status.get("armed", False)),
            needs_reentry=list(status.get("needs_reentry") or []),
        )

    # Last, so /api/* is matched before the catch-all that answers everything else.
    _mount_spa(app)
    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover - a convenience runner, not the deployment path
    import uvicorn

    # Deliberately hard-coded: workers=1 and no reload, for the reasons below.
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("SP_HOST", "0.0.0.0"),
        port=int(os.environ.get("SP_PORT", "8000")),
        workers=1,
        reload=False,
        log_config=None,
    )


# =======================================================================================
# RUN THIS WITH EXACTLY ONE UVICORN WORKER AND NO --reload. IN EVERY ENVIRONMENT.
#
#     uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
#
# Not a style preference — two concrete failures (BUILD_PROMPT §3.6):
#
#   * A SECOND WORKER IS A SECOND TELETHON CLIENT AND A SECOND INGEST QUEUE. Both processes
#     receive the same channel post, both run the agent on it, and both send the order. The
#     duplicate-signal guard is per-process state plus a database row written at the end of a
#     run, so two runs racing inside the same second slip past it. One channel post becomes
#     two positions, with real money behind them.
#
#   * --reload RE-EXECUTES MODULE IMPORT IN A NEW EVENT LOOP. Telethon binds itself to the loop
#     that ran connect() and re-checks it on every request, so the first call after a reload
#     raises `RuntimeError: The asyncio event loop must not change after connection` — and the
#     ingest stops with the process still up and the dashboard still green.
#
# The same rule is written into backend/Dockerfile's CMD and into the README. If you are about
# to add --reload to iterate faster: run `npm run dev` for the frontend instead (it proxies
# /api to this process) and restart this one by hand.
# =======================================================================================
