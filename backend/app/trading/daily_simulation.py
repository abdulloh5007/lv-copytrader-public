"""Durable, quote-only continuation after the real daily profit target."""
from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select, update

from app.db import session_scope
from app.models import DailySimulation
from app.trading import guards, pricing
from app.trading.performance import TZ
from app.trading.symbols import server_time_offset
from app.util.logging import get_logger

log = get_logger(__name__)
NOTICE = "Daily profit target reached. New broker entries are paused. Signal analysis and simulated P&L continue."


async def record(*, key, signal_ref, ref, side, volume, entry, sl, tp, currency, opened_at):
    gain = ref.trade_tick_value
    loss = ref.trade_tick_value_loss or gain
    if not signal_ref or side not in ("buy", "sell") or any(
            not math.isfinite(value) or value <= 0
            for value in (volume, entry, sl, tp, ref.trade_tick_size, gain, loss)):
        return False
    if (side == "buy" and sl >= tp) or (side == "sell" and sl <= tp):
        return False
    async with session_scope() as session:
        if await session.get(DailySimulation, key) is None:
            session.add(DailySimulation(
                key=key, signal_ref=signal_ref, day=datetime.fromtimestamp(opened_at, TZ).date().isoformat(),
                symbol=ref.name, side=side, currency=currency, volume=volume, entry=entry, sl=sl, tp=tp,
                gain_per_price=gain / ref.trade_tick_size, loss_per_price=loss / ref.trade_tick_size,
                opened_at=opened_at, searched_until=opened_at,
            ))
    return True


async def continue_at_cap(runtime, positions, tracked, currency, at):
    for position in positions:
        trade = tracked[int(position.ticket)]
        try:
            async with asyncio.timeout(2):
                ref = await runtime.resolver.get(trade.symbol)
                await record(key=f"closed-at-cap:{trade.id}", signal_ref=trade.signal_ref,
                             ref=ref, side=trade.side, volume=float(position.volume),
                             entry=float(position.price_current), sl=float(position.sl),
                             tp=float(position.tp), currency=currency, opened_at=at)
        except Exception:
            log.exception("daily_simulation_copy_failed")


async def advance(runtime, *, now: float | None = None) -> None:
    """Consume a bounded, chronological window per symbol. An RPC failure leaves its cursor intact."""
    now = time.time() if now is None else now
    async with session_scope() as session:
        positions = (await session.scalars(select(DailySimulation).where(
            DailySimulation.status == "open").order_by(DailySimulation.searched_until))).all()
        for row in positions:
            session.expunge(row)
    if not positions:
        return
    offset = await server_time_offset(runtime.rpc, runtime.resolver)
    grouped = defaultdict(list)
    for row in positions:
        grouped[row.symbol].append(row)
    for symbol, rows in grouped.items():
        start = math.floor(rows[0].searched_until + offset)
        end = min(math.floor(now + offset) - 2, start + 900)
        if start >= end:
            continue
        ticks = await runtime.rpc.copy_quote_ticks(symbol, start, end)
        if ticks is None:
            continue
        horizon = end - offset
        quotes = sorted(((stamp / 1000 - offset, bid, ask) for stamp, bid, ask in ticks
                         if bid is not None and ask is not None and math.isfinite(bid)
                         and math.isfinite(ask) and 0 < bid <= ask), key=lambda q: q[0])
        async with session_scope() as session:
            for row in rows:
                if row.searched_until >= horizon:
                    continue
                result = {"searched_until": horizon}
                long = row.side == "buy"
                sl, tp = row.sl, row.tp
                changes = iter(row.changes)
                change = next(changes, None)
                for stamp, bid, ask in quotes:
                    if not row.searched_until < stamp <= horizon:
                        continue
                    while change is not None and change["at"] <= stamp:
                        sl, tp = change["sl"], change["tp"]
                        change = next(changes, None)
                    price = bid if long else ask
                    stopped = price <= sl if long else price >= sl
                    gained = price >= tp if long else price <= tp
                    if stopped or gained:
                        exit_price = price if stopped else tp
                        distance = (exit_price - row.entry) * (1 if long else -1)
                        rate = row.gain_per_price if distance > 0 else row.loss_per_price
                        result.update(status="closed", closed_at=stamp, exit_price=exit_price,
                                      cause="sl" if stopped else "tp", pnl=round(distance * rate * row.volume, 2))
                        break
                await session.execute(update(DailySimulation).where(
                    DailySimulation.key == row.key, DailySimulation.status == "open",
                    DailySimulation.searched_until == row.searched_until,
                    DailySimulation.changes == row.changes,
                ).values(**result))


async def watch(runtime) -> None:
    while True:
        try:
            await advance(runtime)
        except Exception:
            log.exception("daily_simulation_quotes_failed")
        await asyncio.sleep(5)


async def revise(runtime, ctx, message, refs: set[str], *, break_even: bool) -> bool:
    """Apply linked, verified updates at their observed time in the virtual journal."""
    from app.ai.uz_gold_zone import parse_uz_gold_post, stop_only_post

    if not refs or message.catchup or (not message.edit_date and not message.reply_to_message_id):
        return False
    async with session_scope() as session:
        rows = (await session.scalars(select(DailySimulation).where(
            DailySimulation.signal_ref.in_(refs), DailySimulation.status == "open"))).all()
        primary = [row for row in rows if row.signal_ref == ctx.signal_ref]
        if not primary:
            primary = [row for row in rows if row.key.startswith("closed-at-cap:")]
        if len(primary) != 1:
            return False
        row = primary[0]
        if any(c["message"] == message.id for c in row.changes):
            return True
        parsed = parse_uz_gold_post(str(message.text or ""), quote=row.entry, fallback_levels=True)
        standalone = stop_only_post(str(message.text or ""))
        if not break_even and parsed.side != row.side and standalone is None:
            return False
        ref = await runtime.resolver.get(row.symbol)
        quote = await runtime.resolver.quote(ref.name)
        sl, tp = ((row.changes[-1]["sl"], row.changes[-1]["tp"]) if row.changes else (row.sl, row.tp))
        if break_even:
            sl = max(sl, row.entry) if row.side == "buy" else min(sl, row.entry)
        else:
            stop, pips = standalone if standalone is not None else (parsed.sl, parsed.sl_pips)
            if pips is not None:
                stop = row.entry + (1 if row.side == "sell" else -1) * pips * ref.pip_size
            if stop is not None:
                sl = pricing.round_stop_away(ref, stop, row.side, "sl")
            if parsed.targets:
                tp = pricing.round_stop_away(ref, parsed.targets[0], row.side, "tp")
        entry = quote.ask if row.side == "buy" else quote.bid
        if guards.check_stops_level(ref, row.side, entry, sl, tp, quote.bid, quote.ask, pending=False):
            return False
        row.changes = [*row.changes, {"at": time.time(), "message": message.id, "sl": sl, "tp": tp}]
    return True


async def summary(day: str) -> dict:
    async with session_scope() as session:
        rows = (await session.scalars(select(DailySimulation).where(
            DailySimulation.day == day).order_by(DailySimulation.opened_at))).all()
        items = [{"signal": row.signal_ref, "symbol": row.symbol, "side": row.side, "entry": row.entry,
                  "sl": row.changes[-1]["sl"] if row.changes else row.sl,
                  "tp": row.changes[-1]["tp"] if row.changes else row.tp,
                  "exit": row.exit_price, "volume": row.volume,
                  "currency": row.currency, "pnl": row.pnl, "status": row.status, "cause": row.cause,
                  "continuation": row.key.startswith("closed-at-cap:"), "tracked_until": row.searched_until}
                 for row in rows]
    totals = {}
    for item in items:
        if item["pnl"] is not None:
            currency = item["currency"]
            totals[currency] = round(totals.get(currency, 0) + item["pnl"], 2)
    return {"date": day, "estimated_gross_pnl": totals, "open": sum(i["status"] == "open" for i in items),
            "wins": sum((i["pnl"] or 0) > 0 for i in items), "losses": sum((i["pnl"] or 0) < 0 for i in items),
            "positions": items}


async def period_totals(first: str, end: str) -> list[dict]:
    async with session_scope() as session:
        rows = (await session.execute(select(
            DailySimulation.day, DailySimulation.currency, DailySimulation.pnl,
        ).where(DailySimulation.day >= first, DailySimulation.day < end))).all()
    grouped = {}
    for day, currency, pnl in rows:
        entry = grouped.setdefault((day, currency), {
            "date": day, "currency": currency, "gross": 0.0, "open": 0, "wins": 0, "losses": 0,
        })
        if pnl is None:
            entry["open"] += 1
        else:
            entry["gross"] += pnl
            entry["wins"] += int(pnl > 0)
            entry["losses"] += int(pnl < 0)
    return [grouped[key] for key in sorted(grouped)]
