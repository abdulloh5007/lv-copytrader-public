"""One shared immutable snapshot, refreshed independently of public request volume."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import math
import re
import time
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import case, func, select

from app.config import settings
from app.db import session_scope
from app.models import Trade
from app.public_demo.limits import PublicLimits, Viewers
from app.util.auth import resolve_secret
from app.util.logging import get_logger

log = get_logger(__name__)


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and abs(result) < 1e15 else None
    except (TypeError, ValueError, OverflowError):
        return None


def stamp(value: datetime | None) -> str | None:
    return value.replace(tzinfo=UTC).isoformat() if value is not None else None


def account_projection(raw: Any) -> dict[str, Any] | None:
    def get(key):
        return raw.get(key) if isinstance(raw, dict) else getattr(raw, key, None)

    balance, equity = number(get("balance")), number(get("equity"))
    if balance is None or equity is None:
        return None
    currency = str(get("currency") or "")
    mode = get("trade_mode")
    return {
        "balance": balance,
        "equity": equity,
        "floating_profit": number(get("profit")),
        "currency": currency if re.fullmatch(r"[A-Z]{3}", currency) else "XXX",
        "mode": "demo" if mode == 0 else "real" if mode == 2 else "unknown",
    }


class PublicDemo:
    REFRESH = 5.0
    HISTORY_REFRESH = 15.0
    ACTIVE_FOR = 70.0

    def __init__(self, runtime: Callable[[], Any], clock=time.monotonic, wall=time.time):
        self.runtime, self.clock, self.wall = runtime, clock, wall
        self.limits = PublicLimits(clock)
        self.viewers = Viewers(lambda: resolve_secret(settings.SECRET_KEY), clock, wall)
        self.account: dict[str, Any] | None = None
        self.account_at: float | None = None
        self.history_at: float | None = None
        self.history: dict[str, Any] | None = None
        self.active_until = 0.0
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.refresh_count = 0
        self.raw = b""
        self.compressed = b""
        self.etag = ""
        self.built_at = 0.0
        self._publish(False, False, False)

    def demand(self) -> None:
        self.active_until = self.clock() + self.ACTIVE_FOR
        if self.clock() - self.built_at >= self.REFRESH or self.account_at is None:
            self.wake.set()

    def _publish(self, connected: bool, telegram: bool, running: bool) -> None:
        payload = {
            "account": self.account,
            "account_updated_at": self.account_at,
            "history_updated_at": self.history_at,
            "copytrader": self.history,
            "status": {"broker": connected, "telegram": telegram, "running": running},
            "generated_at": self.wall(),
        }
        # Only the explicit projection above is serialized. Never serialize an ORM
        # object, raw RPC result, exception, runtime status tree or settings object.
        self.raw = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
        self.compressed = gzip.compress(self.raw, compresslevel=5, mtime=0)
        self.etag = hashlib.sha256(self.raw).hexdigest()[:24]
        self.built_at = self.clock()

    async def read_history(self) -> dict[str, Any]:
        closed = (
            Trade.external.is_(False),
            Trade.magic == settings.MAGIC_NUMBER,
            Trade.kind == "position",
            Trade.status == "closed",
            Trade.closed_at.is_not(None),
        )
        async with session_scope() as session:
            totals = (
                await session.execute(
                    select(
                        func.count(Trade.id),
                        func.count(Trade.profit),
                        func.coalesce(func.sum(Trade.profit), 0),
                        func.coalesce(func.sum(case((Trade.profit > 0, 1), else_=0)), 0),
                        func.coalesce(func.sum(case((Trade.profit < 0, 1), else_=0)), 0),
                    ).where(*closed)
                )
            ).one()
            opened = await session.scalar(
                select(func.count(Trade.id)).where(
                    Trade.external.is_(False),
                    Trade.magic == settings.MAGIC_NUMBER,
                    Trade.kind == "position",
                    Trade.status == "open",
                )
            )
            rows = (
                await session.execute(
                    select(
                        Trade.symbol,
                        Trade.side,
                        Trade.volume,
                        Trade.entry_price,
                        Trade.close_price,
                        Trade.profit,
                        Trade.opened_at,
                        Trade.closed_at,
                    )
                    .where(*closed)
                    .order_by(Trade.closed_at.desc(), Trade.id.desc())
                    .limit(30)
                )
            ).all()
            day = func.date(Trade.closed_at)
            daily = (
                await session.execute(
                    select(day, func.sum(Trade.profit))
                    .where(*closed, Trade.profit.is_not(None))
                    .group_by(day)
                    .order_by(day.desc())
                    .limit(90)
                )
            ).all()
        total = number(totals[2])
        cumulative = (total or 0) - sum(number(row[1]) or 0 for row in daily)
        curve = []
        for day_value, profit in reversed(daily):
            cumulative += number(profit) or 0
            curve.append({"date": str(day_value), "profit": round(cumulative, 2)})
        return {
            "closed": int(totals[0]),
            "settled": int(totals[1]),
            "wins": int(totals[3]),
            "losses": int(totals[4]),
            "profit": round(total, 2) if total is not None and totals[1] else None,
            "win_rate": round(int(totals[3]) / int(totals[1]) * 100, 1) if totals[1] else None,
            "open": int(opened or 0),
            "curve": curve,
            "trades": [
                {
                    "symbol": str(row.symbol)[:64],
                    "side": row.side if row.side in ("buy", "sell") else "unknown",
                    "volume": number(row.volume),
                    "entry": number(row.entry_price),
                    "exit": number(row.close_price),
                    "profit": number(row.profit),
                    "opened_at": stamp(row.opened_at),
                    "closed_at": stamp(row.closed_at),
                }
                for row in rows
            ],
        }

    async def refresh(self) -> None:
        # The public request path never calls this method or waits for this lock.
        async with self.lock:
            runtime = self.runtime()
            connected = False
            if runtime is not None:
                try:
                    async with asyncio.timeout(2.5):
                        projection = account_projection(await runtime.rpc.account_info())
                    if projection is not None:
                        self.account, self.account_at, connected = projection, self.wall(), True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # Preserve last good numbers; publish disconnected + original time.
            if (
                self.history is None
                or self.history_at is None
                or self.wall() - self.history_at >= self.HISTORY_REFRESH
            ):
                try:
                    async with asyncio.timeout(3):
                        self.history = await self.read_history()
                    self.history_at = self.wall()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("public_demo_history_unavailable")
            status = getattr(runtime, "status", {}) if runtime is not None else {}
            self.refresh_count += 1
            self._publish(
                connected,
                bool(status.get("telegram", {}).get("ok")),
                bool(status.get("armed") and status.get("provider", {}).get("ok") and connected),
            )

    async def _run(self) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.clock() < self.active_until:
                try:
                    await self.refresh()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("public_demo_refresh_unavailable")
                await asyncio.sleep(self.REFRESH)
            self.wake.clear()

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="public_demo_cache")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
