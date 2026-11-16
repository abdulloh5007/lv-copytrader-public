"""Pure execution planning for abbreviated gold signals; never sends orders."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.ai.uz_gold_zone import parse_uz_gold_post


@dataclass(frozen=True)
class GoldEntryPlan:
    action: Literal['open', 'ignore', 'reject']
    reason: str = ''
    side: str | None = None
    zone: tuple[float, float] | None = None
    entry: float | None = None
    stop: float | None = None
    targets: tuple[float, ...] = ()
    lots: tuple[float, ...] = ()
    channel_stop_supplied: bool = False
    channel_stop_pips: float | None = None
    ignored_levels: tuple[str, ...] = ()


def plan_gold_entry(
    text: str, *, bid: float, ask: float, tick_ts: float, now_ts: float,
    signal_ts: float, total_lots: float, volume_min: float, volume_max: float,
    volume_step: float, margin_mode: str, stop_distance: float = 15.0,
    clock_offset: float = 0.0, edited: bool = False, catchup: bool = False,
    max_signal_age: float = 60.0, zone_tolerance: float = 0.0,
) -> GoldEntryPlan:
    """Build a two-target plan only; all broker writes require separate gates.

    `stop_distance` is in gold price units, not account-currency risk. The
    requested stop is relative to the quote; a real broker fill can differ.
    No existing positions are touched and no orders are sent by this function.
    """
    values = (bid, ask, tick_ts, now_ts, signal_ts, total_lots, volume_min,
              volume_max, volume_step, stop_distance, clock_offset, max_signal_age, zone_tolerance)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return GoldEntryPlan('reject', reason='invalid_numeric_input')
    if not 0 <= zone_tolerance <= 2:
        return GoldEntryPlan('reject', reason='invalid_zone_tolerance')
    if not 60 <= max_signal_age <= 900:
        return GoldEntryPlan('reject', reason='invalid_signal_age_limit')
    if bid <= 0 or ask < bid or stop_distance <= 0:
        return GoldEntryPlan('reject', reason='invalid_quote_or_stop')
    tick_age = now_ts - (tick_ts - clock_offset)
    if not 0 <= tick_age <= 5:
        return GoldEntryPlan('reject', reason='stale_quote')
    if edited or catchup or not 0 <= now_ts - signal_ts <= max_signal_age:
        return GoldEntryPlan('reject', reason='edited_or_stale_signal')
    if margin_mode.lower() != 'hedging':
        return GoldEntryPlan('reject', reason='two_positions_require_hedging')
    post = parse_uz_gold_post(text, quote=(bid + ask) / 2, fallback_levels=True)
    if post.kind in ('ignored', 'management'):
        return GoldEntryPlan('ignore', reason=post.action or 'not_an_entry')
    if post.kind != 'entry' or post.side is None or (post.zone is None and post.action != 'market_now'):
        return GoldEntryPlan('reject', reason=post.reason or 'invalid_signal')
    ignored_levels = list(post.ignored_levels)
    targets = post.targets[:2]
    entry = ask if post.side == 'buy' else bid
    # Do not enter (even with tolerance) when the first target is already reached.
    first_target = targets[0] if targets else post.verified_first_target
    if first_target is not None and ((post.side == 'sell' and bid <= first_target) or
                                     (post.side == 'buy' and ask >= first_target)):
        return GoldEntryPlan('reject', reason='target_reached_before_entry', side=post.side, zone=post.zone)
    if post.zone is not None and not post.zone[0] - zone_tolerance <= entry <= post.zone[1] + zone_tolerance:
        return GoldEntryPlan('reject', reason='quote_outside_entry_zone', side=post.side, zone=post.zone)
    try:
        total, minimum, maximum, step = map(lambda v: Decimal(str(v)),
                                             (total_lots, volume_min, volume_max, volume_step))
        if total <= 0 or minimum <= 0 or maximum < minimum or step <= 0:
            raise ValueError('invalid volume')
        half = total / 2
        if half < minimum or half > maximum or half % step != 0:
            raise ValueError('two equal legal legs required')
    except (InvalidOperation, ValueError):
        return GoldEntryPlan('reject', reason='invalid_two_leg_volume')
    distance = post.sl_pips * 0.1 if post.sl_pips is not None else stop_distance
    stop = (post.sl if post.sl is not None else
            entry - distance if post.side == 'buy' else entry + distance)
    if stop <= 0:
        return GoldEntryPlan('reject', reason='invalid_stop')
    if (post.side == 'buy' and stop >= entry) or (post.side == 'sell' and stop <= entry):
        stop = entry - stop_distance if post.side == 'buy' else entry + stop_distance
        if stop <= 0:
            return GoldEntryPlan('reject', reason='invalid_stop')
        ignored_levels.append('invalid_stop')
        return GoldEntryPlan('open', side=post.side, zone=post.zone, entry=entry, stop=stop,
                             targets=targets, lots=(float(half), float(half)),
                             ignored_levels=tuple(ignored_levels))
    return GoldEntryPlan('open', side=post.side, zone=post.zone, entry=entry, stop=stop,
                         targets=targets, lots=(float(half), float(half)),
                         channel_stop_supplied=post.sl is not None or post.sl_pips is not None,
                         channel_stop_pips=post.sl_pips, ignored_levels=tuple(ignored_levels))


async def wait_for_gold_entry(attempt, *, wait_seconds: float, sleep=None, monotonic=None):
    """Retry safe pre-order price/target changes; never retry a broker attempt.

    The caller must revalidate signal, account, quote, limits and arm state on
    each attempt. Cancellation or restart discards the wait; it is not replayed.
    """
    import asyncio
    import time

    if (isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float))
            or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 900):
        raise ValueError('invalid wait duration')
    sleep = sleep or asyncio.sleep
    monotonic = monotonic or time.monotonic
    deadline = monotonic() + wait_seconds
    result = await attempt()
    safe_reasons = {'gold channel entry refused: quote_outside_entry_zone',
                    'gold channel entry refused: targets_changed_before_order',
                    'gold channel add-on refused: quote_outside_addon_zone'}
    while (result.get('ok') is False
           and result.get('stage') == 'channel_profile'
           and result.get('message') in safe_reasons):
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        await sleep(min(1.0, remaining))
        if monotonic() >= deadline:
            break
        result = await attempt()
    return result
