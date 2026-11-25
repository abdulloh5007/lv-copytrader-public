"""Optional daily target for copied positions. The operator's arm switch is independent."""
from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select

from app.db import get_setting, session_scope, set_setting
from app.models import Trade
from app.trading.daily_simulation import NOTICE, continue_at_cap
from app.trading.performance import TZ, utc_start
from app.trading.symbols import server_time_offset
from app.util.logging import get_logger

log = get_logger(__name__)
STATE_KEY = "performance.daily_target"
TARGET_REACHED = "Daily profit target reached. New copied entries resume next trading day."
UNAVAILABLE = "Daily profit target cannot be verified. No new copied entry was sent."


@dataclass(frozen=True)
class DailyGoal:
    target: float
    remaining: float


def dynamic_gold_size_enabled(config, *, now: datetime | None = None) -> bool:
    start = config.DAILY_PROFIT_TARGET_START_DATE
    return bool(config.DAILY_PROFIT_DYNAMIC_GOLD_SIZE and config.DAILY_PROFIT_TARGET_PERCENT
                and (start is None or (now or datetime.now(UTC)).astimezone(TZ).date() >= start))


async def daily_target(runtime, *, now: datetime | None = None) -> str | None:
    refusal, _ = await check_daily_goal(runtime, now=now)
    return refusal


async def check_daily_goal(runtime, *, now: datetime | None = None) -> tuple[str | None, DailyGoal | None]:
    """Check and latch the target under the lock that serializes broker writes."""
    percent = runtime.settings.DAILY_PROFIT_TARGET_PERCENT
    if not percent:
        return None, None
    from app.trading.executor import RunContext, broker_write_lock

    async with broker_write_lock():
        now = now or datetime.now(UTC)
        today = now.astimezone(TZ).date()
        start_date = runtime.settings.DAILY_PROFIT_TARGET_START_DATE
        if start_date is not None and today < start_date:
            return None, None
        day_start = utc_start(today)
        async with session_scope() as session:
            binding = await get_setting(session, "trading.account_binding")
            state = await get_setting(session, STATE_KEY, {})
            if not isinstance(state, dict):
                return UNAVAILABLE, None
            rows = (await session.execute(select(Trade).where(
                Trade.magic == runtime.settings.MAGIC_NUMBER, Trade.external.is_(False),
                Trade.signal_ref.is_not(None),
                or_(Trade.status.in_(("open", "pending")), Trade.closed_at >= day_start),
            ))).scalars().all()
        try:
            account = await runtime.rpc.account_info()
            if (not isinstance(binding, dict) or account is None
                    or int(account.login) != int(binding["login"])
                    or str(account.server).casefold() != str(binding["server"]).casefold()):
                return UNAVAILABLE, None
            identity = f"{account.login}@{account.server}"
            positions = await runtime.rpc.positions_get()
            orders = await runtime.rpc.orders_get()
            if positions is None or orders is None:
                return UNAVAILABLE, None
            tracked = {int(row.ticket): row for row in rows}
            live = [p for p in positions if int(p.ticket) in tracked and
                    tracked[int(p.ticket)].status == "open" and
                    int(p.magic) == runtime.settings.MAGIC_NUMBER and
                    p.symbol == tracked[int(p.ticket)].symbol and
                    int(p.type) == (0 if tracked[int(p.ticket)].side == "buy" else 1)]
            pending = [o for o in orders if int(o.ticket) in tracked and
                       tracked[int(o.ticket)].status == "pending" and
                       int(o.magic) == runtime.settings.MAGIC_NUMBER and
                       o.symbol == tracked[int(o.ticket)].symbol]
            if state.get("date") != today.isoformat() or state.get("account") != identity:
                balance = float(account.balance)
                floating = sum(float(p.profit) + float(p.swap) for p in live)
                if not math.isfinite(balance) or balance <= 0 or not math.isfinite(floating):
                    return UNAVAILABLE, None
                state = {"date": today.isoformat(), "account": identity, "balance": balance,
                         "opening_floating": floating, "reached": False, "attempted": [],
                         "started_at": now.timestamp()}
                async with session_scope() as session:
                    await set_setting(session, STATE_KEY, state)
            if not state["reached"]:
                offset = await server_time_offset(runtime.rpc, runtime.resolver)
                start = max(day_start.replace(tzinfo=UTC).timestamp(), float(state["started_at"])) + offset
                end = now.timestamp() + offset
                deals = await runtime.rpc.history_deals_get(start, end + 86400)
                if deals is None:
                    return UNAVAILABLE, None
                owned_ids = set(tracked) | {int(getattr(p, "identifier", p.ticket)) for p in live}
                net = sum(
                    float(d.profit) + float(d.swap) + float(d.commission) + float(getattr(d, "fee", 0))
                    for d in deals if start <= float(d.time) <= end and int(d.position_id) in owned_ids
                )
                net += sum(float(p.profit) + float(p.swap) for p in live) - state["opening_floating"]
                if not math.isfinite(net):
                    return UNAVAILABLE, None
                target = round(float(state["balance"]) * percent / 100, 2)
                if target <= 0:
                    return UNAVAILABLE, None
                if net < target:
                    return None, DailyGoal(target, target - net)
                state["reached"] = True
                state["reached_at"] = now.timestamp()
                async with session_scope() as session:
                    await set_setting(session, STATE_KEY, state)
                runtime.notifier.fire("attention", NOTICE if dynamic_gold_size_enabled(runtime.settings) else TARGET_REACHED)
            target = round(float(state["balance"]) * percent / 100, 2)
            if dynamic_gold_size_enabled(runtime.settings):
                await continue_at_cap(runtime, [p for p in live if int(p.ticket) not in state["attempted"]],
                                      tracked, str(account.currency), now.timestamp())
            # Cancel and close only tickets tied to this strategy. Never retry an
            # attempted ticket after a lost response. Broker SL/TP remain in place.
            for items, action in ((pending, runtime.executor.cancel_order), (live, runtime.executor.close_position)):
                for item in items:
                    ticket = int(item.ticket)
                    if ticket in state["attempted"]:
                        continue
                    state["attempted"].append(ticket)
                    async with session_scope() as session:
                        await set_setting(session, STATE_KEY, state)
                    trade = tracked[ticket]
                    ctx = RunContext(uuid.uuid4().hex, trade.signal_ref, trade.source_id,
                                     trade.message_row_id, origin="daily_target", bypass_arm=True)
                    result = await action(ctx, ticket=ticket, cause="daily_target", reason="Daily profit target")
                    if not result.get("ok"):
                        runtime.notifier.fire("attention", f"Daily target: verify ticket #{ticket} in MT5.")
            return TARGET_REACHED, DailyGoal(target, target)
        except Exception:
            log.exception("daily_target_check_failed")
            return (TARGET_REACHED if state.get("reached") else UNAVAILABLE), None


async def monitor_daily_target(runtime) -> None:
    while True:
        await asyncio.sleep(5)
        try:
            await daily_target(runtime)
        except Exception:
            log.exception("daily_target_monitor_failed")
