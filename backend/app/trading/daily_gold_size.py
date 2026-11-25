"""Size a protected gold entry against the remaining daily profit goal."""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.trading import guards, pricing
from app.trading.daily_target import DailyGoal
from app.trading.symbols import Quote, SymbolRef

REWARD_RISK = 1.5


@dataclass(frozen=True)
class GoldDaySize:
    stop: float
    take_profit: float
    volume: float
    max_loss: float
    stop_loss: float
    take_profit_gain: float
    fallback_stop: bool
    fallback_target: bool


def _money_per_price(ref: SymbolRef) -> tuple[float, float]:
    tick = ref.trade_tick_size
    gain = ref.trade_tick_value
    loss = ref.trade_tick_value_loss or gain
    if any(not math.isfinite(value) or value <= 0 for value in (tick, gain, loss)):
        raise ValueError("broker tick values are unavailable")
    return loss / tick, gain / tick


def gold_size_error(ref: SymbolRef, quote: Quote, side: str, plan: GoldDaySize) -> str | None:
    loss, gain = _money_per_price(ref)
    sign = 1 if side == "buy" else -1
    entry = quote.ask if side == "buy" else quote.bid
    risk = (entry - plan.stop) * sign * loss * plan.volume
    reward = (plan.take_profit - entry) * sign * gain * plan.volume
    if risk <= 0 or risk > plan.max_loss + 1e-7 or reward + 1e-7 < risk * REWARD_RISK:
        return "price no longer meets the daily sizing risk and reward limits"
    return guards.check_stops_level(ref, side, entry, plan.stop, plan.take_profit,
                                    quote.bid, quote.ask, pending=False)


def size_gold_for_day(
    ref: SymbolRef, quote: Quote, side: str, *, channel_stop: float | None,
    fallback_distance: float, channel_targets: tuple[float, ...], goal: DailyGoal,
    max_volume: float,
) -> GoldDaySize | None:
    loss, gain = _money_per_price(ref)
    step = ref.volume_step
    if not math.isfinite(step) or step <= 0:
        raise ValueError("broker volume step is unavailable")
    sign = 1 if side == "buy" else -1
    entry = quote.ask if side == "buy" else quote.bid
    # A losing signal never increases the next position's risk budget.
    max_loss = min(goal.target, goal.remaining) / REWARD_RISK
    cushion = max(ref.point, quote.ask - quote.bid)
    fallback_stop = channel_stop is None
    fallback_target = not channel_targets
    if fallback_stop:
        # Keep the normal fallback distance when possible. Snap toward entry
        # before rounding SL away, so the minimum lot cannot exceed the budget.
        affordable = (math.floor((max_loss / (loss * ref.volume_min) - cushion) / ref.point + 1e-9)
                      * ref.point)
        distance = min(fallback_distance, affordable)
        channel_stop = entry - sign * distance
    stop = pricing.round_stop_away(ref, channel_stop, side, "sl")
    risk_per_lot = round((entry - stop) * sign, ref.digits) * loss
    if risk_per_lot <= 0:
        return None
    maximum = min(max_volume, ref.volume_max, max_loss / (risk_per_lot + cushion * loss))
    max_steps = math.floor(maximum / step + 1e-9)
    if round(max_steps * step, 8) < ref.volume_min:
        return None

    if not channel_targets:
        volume = round(max_steps * step, 8)
        # Leave room for the threshold to close the order before the fallback TP.
        distance = max(risk_per_lot * REWARD_RISK / gain, goal.remaining / (volume * gain))
        channel_targets = (entry + sign * (distance + ref.point),)

    best = None
    for target in channel_targets:
        tp = pricing.round_stop_away(ref, target, side, "tp")
        reward_per_lot = round((tp - entry) * sign, ref.digits) * gain
        if reward_per_lot < risk_per_lot * REWARD_RISK - 1e-7:
            continue
        # Pick the smallest legal lot that can reach the goal. A closer TP can
        # still be used for a smaller contribution when none can finish the day.
        needed_steps = math.ceil(goal.remaining / (reward_per_lot * step) - 1e-9)
        min_steps = math.ceil(ref.volume_min / step - 1e-9)
        volume = round(min(max_steps, max(min_steps, needed_steps)) * step, 8)
        plan = GoldDaySize(stop, tp, volume, max_loss, risk_per_lot * volume,
                           reward_per_lot * volume, fallback_stop, fallback_target)
        if gold_size_error(ref, quote, side, plan):
            continue
        if plan.take_profit_gain >= goal.remaining - 1e-7:
            return plan
        if best is None or plan.take_profit_gain > best.take_profit_gain:
            best = plan
    return best
