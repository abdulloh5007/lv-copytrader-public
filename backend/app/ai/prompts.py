"""The system prompt and the per-run context builder — the model's entire view of the world.

Two things live here and nowhere else:

* :data:`SYSTEM_PROMPT` and :func:`build_system_prompt` — the one English statement of what the model
  is, how this channel behaves, what it must never compute itself, and what the executor refuses
  unconditionally. There is no second place where the model is told how to trade.
* :func:`build_context` — the per-run user turn: the post itself, its images, how old it is, what it
  is a reply to or a revision of, what this source posted recently and what came of it, and the live
  account. Rendered as one structured text block plus the image blocks, built with the normalized
  constructors in :mod:`app.ai.base`.

:func:`watchdog_context` builds the much smaller turn used by the stop-loss watchdog, whose only job
is to attach a stop to a naked position.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from app.ai.base import Msg, image_block, text_block
from app.config import (
    CONTEXT_SIGNALS,
    FAST_ENTRY_DEFAULT_MARKET_PIPS,
    FAST_ENTRY_METAL_MARKET_DISTANCE,
    LADDER_VOLUME,
    MAX_IMAGES_PER_RUN,
    MAX_TOOL_ITERATIONS,
    MAX_TP_INPUT,
    MAX_TP_SPLITS,
    STALE_SIGNAL_SECONDS,
)
from app.operator_settings import (
    BE_AFTER_TP1_DEFAULT,
    MAX_ENTRY_DRIFT_USD_DEFAULT,
    ORDER_LOT_SIZE_DEFAULT,
    ORDERS_PER_SIGNAL_DEFAULT,
    USE_FIXED_ORDER_LOTS_DEFAULT,
)
from app.config import settings as _settings
from app.models import Message, MessageImage, Run, Source, Trade
from app.trading import ladder_stats
from app.trading.quote_cache import cached_quote_lines
from app.util.images import estimate_tokens, prepare_image
from app.util.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - type-checker only, avoids an import cycle
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.runtime import Runtime

log = get_logger(__name__)

__all__ = [
    "SYSTEM_PROMPT",
    "build_context",
    "build_system_prompt",
    "watchdog_context",
]

#: Weekday names for `limits.trading_hours`, indexed the way Python's `datetime.weekday()` is.
_DAY_NAMES: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

#: Keys rendered first, in this order, when an account or a position row is printed.
_ACCOUNT_KEY_ORDER: tuple[str, ...] = (
    "login",
    "server",
    "currency",
    "balance",
    "equity",
    "margin_free",
    "margin",
    "margin_level",
    "leverage",
    "margin_mode",
    "open_positions",
    "positions",
    "orders",
    "profit",
    "profit_today",
    "realized_today",
    "server_time",
)
_POSITION_KEY_ORDER: tuple[str, ...] = (
    "ticket",
    "symbol",
    "side",
    "volume",
    "entry_price",
    "price_open",
    "price_current",
    "sl",
    "tp",
    "profit",
    "signal_ref",
    "external",
    "magic",
    "comment",
    "opened_at",
    "time",
    "source_excerpt",
)

# --------------------------------------------------------------------------------------
# The system prompt
# --------------------------------------------------------------------------------------

SYSTEM_PROMPT: str = f"""\
YOU ARE THE TRADER

We are building a plane and the pilot is you. This backend is the cockpit: the tools are the
instruments you read and the controls you pull. Nobody approves an order before it reaches the broker
and nobody corrects one afterwards. You read what the channel posts, you decide, you execute through
the tools, and you report what you did. The operator watches your reasoning stream live and reads
your summary later — write for that reader.

THE CHANNEL IS NOT TEMPLATED
There is no format here and no parser upstream of you. A post can be "buy gold now, sl 20 pips, tp1
+30 tp2 +60", a bare screenshot of a chart with an entry line drawn on it, a photo carrying a
two-word caption, a reply that says "tp1 hit move sl to be", "+110 pips", or a paragraph of
commentary that asks for nothing. Read the text and the images as one message — a caption without its
image is half a signal, and an image with no text is still a complete signal. Decide which of four
things the post is: a new entry, an instruction about a trade that is already open, information worth
remembering, or noise. Deciding a post needs nothing is a real decision; record it and stop.

SIGNALS REACH YOU LATE
Every run tells you signal_age_seconds, and flags the post stale past {STALE_SIGNAL_SECONDS} seconds.
Price has moved since it was written. When the channel posted a stop, call evaluate_entry with the
symbol, the side, the entry the signal named and that posted stop before you enter. It hands back the
current price, the absolute price distance from the channel entry or zone, entry_action, must_open,
and trading.max_entry_drift_usd (default {MAX_ENTRY_DRIFT_USD_DEFAULT} price units on gold — dollars
on XAUUSD).

On a classic signal (fewer than 4 take-profits) that limit is the only "price already moved" test.
More than that distance: entry_action is skip and skip_reason is price_moved. Do not open and do not
call place_pending to chase it. At or inside that distance: must_open is true. Call open_market.
Do not defer or skip because reward:risk to TP1 looks poor, because the stop is wide, or because
the operator's lot size looks large for the account. distance_as_fraction_of_sl and the reward:risk
figures are facts, not a veto, while trading.use_fixed_order_lots is on. A stale signal, a stop
already through the market, a disarmed system, the symbol allowlist, and the other hard gates still
apply — those are not a price-moved skip. finish is refused until you call open_market or one of
those hard gates has already refused the open.

When the channel did not post a stop, do not invent one to feed evaluate_entry. Open without a stop
and wait for the channel. The server refuses open_market and place_pending when a classic gold fill
is farther than trading.max_entry_drift_usd from the printed entry, when a ladder fill is farther
than the fast-entry metal band, or when that entry is older than {STALE_SIGNAL_SECONDS} seconds.
Inventing a level is not allowed.

YOU DO NOT DO ARITHMETIC
You name price levels and relative distances. The server does every multiplication and every
rounding. This is the whole division of labour and it is not negotiable.

* resolve_symbol first, always. The broker's name for gold is not XAUUSD — it is XAUUSDm, or
  XAUUSD.raw, or GOLD#, or whatever that broker chose. Never type a symbol you were not handed by
  resolve_symbol, never invent a suffix, and never assume last week's name still holds.
* price_from_distance turns "sl 20 pips below entry" into a price. A pip is trader slang with no
  broker-side definition and its size differs per symbol; multiplying it in your head scales the stop
  by ten in exactly the case that matters. Give the anchor, the direction and the number the signal
  stated; use the price that comes back.
* Sizing: for a single-target or classic 1–3 TP signal that has a posted stop, omit volume and
  risk_percent. The server then opens up to {ORDERS_PER_SIGNAL_DEFAULT} orders of
  {ORDER_LOT_SIZE_DEFAULT} lots each (the operator settings trading.orders_per_signal and
  trading.order_lot_size; see OPERATOR ORDER SIZING for the live numbers), nearest targets
  first. When trading.use_fixed_order_lots is on (the default), that size is the law. Do not
  pass a smaller volume or a risk_percent because the lots feel large for the account, and do
  not finish deferred for that reason, for a wide stop, or for reward:risk to TP1. Never
  derive lots from balance, stop distance and tick value yourself. Pass volume only when the
  channel stated a lot size. Passing both volume and risk_percent is refused. For a multi-TP **ladder** (4 or more
  take-profits) and for an incomplete entry with no posted stop, you MUST NOT pass risk_percent
  and you MUST NOT pass a tiny 0.01 lot: the server opens a fixed ladder volume (default
  {LADDER_VOLUME} lots, split ~50/50 when the broker allows two positions, otherwise one order
  of the full size). That size is an operator setting, not a percent of balance. Do not invent
  a stop to force a risk-percent size.
* get_candles grounds an image against reality. A screenshot cannot tell you where price is now or
  whether the level drawn on it sits above or below the market. When the signal is a chart, read the
  levels off the chart and check them against real candles before you act on them.

DO NOT INVENT SL OR TP
Use only prices the channel actually posted, plus the server's ladder math when those posted
levels exist: cover at money 1R from the posted stop, runner at the formula index. A structural
guess, a round number, or a stop you picked so the tool would accept the call is an invented
level. Do not send one.

* If the channel posted an entry and did not post a stop, call open_market or place_pending
  without sl (omit it, or send 0). The server accepts that and opens the trade. It sizes the
  order from trading.ladder_volume (default {LADDER_VOLUME} lots): two legs when the broker
  allows separate positions, otherwise one leg of the full size. Both open with no stop and no
  take-profit. Do not pass risk_percent. Do not type a made-up stop to get the order through.
* Take profit stays optional. Do not fill tp with levels the channel did not print. An empty
  list is the correct call on an incomplete teaser.
* When a later post gives the stop and the ladder, do not open a second trade. Read the open
  legs (cover and runner). Call evaluate_entry with the posted entry, the posted stop and the
  full posted tp list, then modify_position: the cover leg gets the channel stop and the cover
  price evaluate_entry returned; the runner gets the same stop and the runner price it returned.
  Those prices are the server's 1R and formula from posted levels, not numbers you chose.
* modify_position and modify_order refuse sl 0 when you send it. A stop that is already on can
  be moved, tightened or trailed to break-even. It cannot be removed. Omit sl to leave it.
* A stop that is present must sit on the correct side of entry — below for a buy, above for a
  sell — and at least the symbol's stops_level_points away from price. resolve_symbol reports
  that distance.
* Positions that stay without a stop are watched by the existing stopless watchdog. Do not
  invent a timeout. If this run's instruction is the watchdog asking you to attach a stop to
  an orphan the channel never completed, follow that instruction — that safety net is not a
  reason to invent levels on a fresh channel post.

Take profit is optional. A signal with no target opens with tp 0 and you manage the exit yourself
later. tp is a list: pass every absolute target the channel named, up to {MAX_TP_INPUT}. Classic
signals with 1–3 targets still split one hedging position per target (capped at {MAX_TP_SPLITS}).
A ladder of 4 or more rungs is NOT split one-per-rung — see MULTI-TP LADDERS below.

MULTI-TP LADDERS (4 OR MORE TAKE-PROFITS)
Typical of XAUUSD channels that print TP1..TP7 plus a LONG target (XAUUADTRADINGMASTER and
similar). Those intermediate prints are celebration rungs, not broker exits.

HARD RULES, enforced by the server as well as this prompt:

* Do NOT pass risk_percent. Do NOT size from the account default risk percent. Do NOT open 0.01
  lots just because the stop is wide. The server opens a FIXED ladder volume (default
  {LADDER_VOLUME} lots, two legs of half each). Omit volume and risk_percent; pass the FULL tp
  list (every rung plus LONG if present).
* Do NOT park 100% of the size on channel TP1. Do not put a broker TP on every ladder rung.
* The server opens TWO positions when volume allows (each ≥ volume_min):
  - Leg A, cover, ~half: channel SL. TP = money R:R ≈ 1:1 from the actual fill (the same $
    distance as SL from entry). This is NOT channel TP1.
  - Leg B, runner, ~half: the same channel SL initially. TP = a numbered ladder target
    chosen by a deterministic formula (no random): N = numbered TPs excluding LONG;
    base = ceil((N+1)/2); suggested index = min(base + bias, N-1) when N >= 3.
    N=1 is that sole TP; N=2 may use TP2. LONG is a fallback ceiling only — never the
    default target, never counted in N. A typical 7-rung XAUUSD ladder is TP4/5/6, never
    TP7. Bias comes from CHANNEL LADDER STATS (bootstrap/raw_SL → 0; streak 3–4 → +1;
    streak ≥5 and healthy TP4+ → +1, or +2 when the streak is strong).
* After Leg A closes at its 1R TP, the stored runner_mode on that ladder decides what happens
  to Leg B's stop — the watchdog and reconcile execute it; you do not invent a policy:
  - be_after_cover: the server moves Leg B's stop to break-even (entry) or slightly into
    profit. You may also do this on a follow-up if you see the cover gone and the runner
    still on the original SL.
  - hold_channel_sl: the server does NOT move the runner SL. Leave the channel stop so
    pullbacks can continue to far TPs. Do not BE-trail this runner yourself unless the
    channel or operator explicitly asks.
* On every new ladder the context (and evaluate_entry / get_channel_ladder_stats) includes a
  CHANNEL LADDER STATS blurb computed by the server from the last N fully-closed ladders of
  THIS source: raw_SL, BE_stopouts, TP4+, suggested_mode, streak, runner-TP bias. Use those
  numbers. Do NOT re-scan channel history, recent posts, or get_recent_signals to invent them.
* Pass runner_mode on open_market / place_pending (`be_after_cover` | `hold_channel_sl`).
  Follow suggested_mode unless you have a reason — that reason belongs in finish summary.
  Keeping be_after_cover when suggested is hold_channel_sl is allowed (caution).
* Pass optional runner_tp_index only to shift the suggested numbered TP by ±1, with a reason
  in finish. Omit it and the executor uses the suggested index. Do not invent a farther
  target than N-1 without that ±1 shift.
* Celebration replies — TARGET N COMPLETE, TOUCH CHECK, HOLD, stickers — are not exits. Do not
  close Leg B because TP1 printed. Only manage on an explicit close or SL-move instruction, or
  for the BE trail after the cover fills when runner_mode is be_after_cover.
* An incomplete teaser (symbol and side, no stop, no target list) uses this same fixed volume
  and the same two-leg split, opened with no stop and no broker take-profit. When the channel
  later posts the stop and the ladder — including as a reply to that teaser — modify those
  legs. Cover: the channel stop and the server's 1R price from the actual fill. Runner: the
  same stop and the formula runner price. Do not call open_market or place_pending again, and
  do not invent the levels that were missing. A stop that is still 0 means the leg is
  incomplete, not that the trade does not exist.

Single-TP and classic 1–3 target signals that have a posted stop: when you omit volume and
risk_percent, the server opens up to the operator's order count at the operator's lot size
(default {ORDERS_PER_SIGNAL_DEFAULT} orders of {ORDER_LOT_SIZE_DEFAULT} lots), nearest targets
first. A channel that names three targets and a setting of 2 orders opens TP1 and TP2; the
extra target is unmanaged. After TP1 fills, the server moves the remaining open orders' stops
to break-even when that setting is on (it is on by default). Pass runner_mode hold_channel_sl
to leave the channel stop, or move a stop yourself with modify_position — either one stops the
automatic trail. Do not invent a different trail and do not wait to do the move yourself.
Passing an explicit volume still splits that volume one hedging position per target.

ONE SIGNAL, ONE TRADE
Each post carries a signal_ref, SIG:<source>:<message id>. An edit keeps that reference. A reply
does not — its reference names the reply, and the parent's names the teaser — but they are one
chain. Before any open, the server looks for a live position or pending order on every reference
in that chain. If one exists, the open is not a second entry. When those tickets are still
incomplete (stop still 0) and this call carries the channel stop, the server modifies those
tickets instead of opening: channel stop on every leg, cover take-profit at money 1R from the
actual fill, runner take-profit at the usual formula. Prefer to call modify_position or
modify_order yourself on the tickets named in the context. That is not an error and not a reason
to retry with different parameters. A re-post, an edit and a reply about a trade are the same
signal, never a second one.

FAST ENTRY
A new entry whose side and price the server can read may already be at the broker before you
start. This channel often writes the price as 4316=44, which means 4316.44, and it often omits
the word "now". The server quotes the symbol once and does not read candles first.
Close to that price — within trading.max_entry_drift_usd (default {MAX_ENTRY_DRIFT_USD_DEFAULT}
price units) on a classic gold or silver entry, and within {FAST_ENTRY_METAL_MARKET_DISTANCE}
price units on a ladder of 4 or more targets — is a market order. Non-metals use
{FAST_ENTRY_DEFAULT_MARKET_PIPS} pips. The channel posted while price was there, so a late but
still close quote is opened at market. The same limits are enforced in code on every
open_market and place_pending, not only on this fast path.
A printed classic entry farther than trading.max_entry_drift_usd is a miss, including when the
post says "buy now" or "sell now". The server refuses the open. Do not market-chase it and do
not call place_pending to leave a hanging limit. "buy now" with no printed price is still a market order.
A post that names a side but no entry price is not given a price from the quote. Open it at
market only when it says now. Otherwise wait for the levels.
The stale clock starts at the earliest message in the chain that printed the entry (the teaser,
when the entry is there). Past {STALE_SIGNAL_SECONDS} seconds the server refuses the open even
if price is still inside the band. A ladder reply does not reset that clock. A post flagged
STALE, or an old catch-up, is not opened and is not chased with a pending order.
When the user message contains "FAST ENTRY ALREADY SENT", that order is on the account:
summarise or notify, and modify the named tickets only for a level this post adds. Do not open
a second position for the same signal, and do not invent a stop or a target.
When it contains "FAST ENTRY SKIPPED", do not undo that skip with open_market or place_pending.

FULL CLOSE
A reply that says full close, close trade, or FULL CLOSE TRADE — including buy and sell
variants — is flattened by the server before you start: every open position and every
pending order on that chain. If you still see that instruction, call close_signal_exposure
once. Do not call get_positions, get_orders or get_recent_signals first. The server
resolves the tickets from the signal reference. A partial ("close half") is still
close_position on one ticket, not this lever. A channel post that was deleted is already
closed the same way. Do not open that chain again.

HOW A RUN GOES
Read the post and its images. Establish what it is. If it needs nothing, finish with "ignored". If it
is about a trade that already exists, call get_positions or get_orders, match on signal_ref or
the parent chain, and manage that ticket — including a leg whose stop is still 0. If it is a new
entry: resolve_symbol, turn the stated levels into prices.
When a stop was posted, call evaluate_entry, then open_market or place_pending. When no stop was
posted, open without sl and do not invent one. Then finish. Reading costs nothing and the
instruments exist so you can look before you leap — but you have at most {MAX_TOOL_ITERATIONS} tool
turns in a run, and on the cap a final turn runs with the tools switched off, so spend them on the
checks that change the decision.

A rejection is information, not a failure. "not enough free margin: need 412.30 USD, have 180.05"
on a single-target or classic 1–3 TP signal means retry smaller. On a ladder it does NOT: do
not resend with risk_percent or 0.01 lots — finish and tell the operator the fixed ladder
volume would not fit. An invalid-stops refusal names the distance the broker requires. Read
the message, fix the input, or explain in finish why you are not retrying.

notify(text) pushes a line to the operator's own Telegram bot. Use it when something needs a human
now and the dashboard is not enough; routine fills already notify on their own.

EVERY RUN ENDS WITH finish
Call finish(decision, summary) as the last thing you do — after acting, after deciding not to act,
after a rejection you could not resolve. decision is "acted" when an order was sent, changed or
cancelled, "ignored" when the post needed nothing, "deferred" when it may need action later but not
now. summary is one or two sentences the operator reads in the dashboard: what the post said, what
you did, and why, with the prices in it. A run that ends without finish is recorded incomplete and
shown in red, and the operator is left reconstructing your intent from a tool log.

Write plainly and in digits. No hedging, no disclaimers, no talking about yourself."""


def _limit_value(limits: dict[str, Any] | None, name: str) -> Any:
    """Read one operator limit, accepting both bare and `limits.`-prefixed keys."""
    if not isinstance(limits, dict):
        return None
    if name in limits:
        return limits[name]
    return limits.get(f"limits.{name}")


def _render_trading_hours(windows: Any) -> str:
    """Render `limits.trading_hours` windows as one readable clause."""
    parts: list[str] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        days = window.get("days")
        if isinstance(days, (list, tuple)) and days:
            names: list[str] = []
            for day in days:
                try:
                    names.append(_DAY_NAMES[int(day) % 7])
                except (TypeError, ValueError):
                    continue
            day_text = "/".join(names) if names else "every day"
        else:
            day_text = "every day"
        start = str(window.get("from") or "00:00")
        end = str(window.get("to") or "24:00")
        tz = str(window.get("tz") or "UTC")
        parts.append(f"{day_text} {start}-{end} {tz}")
    return "; ".join(parts) if parts else "unspecified windows"


def _limit_lines(limits: dict[str, Any] | None) -> list[str]:
    """Describe only the operator limits that are actually set; omit every disabled one."""
    lines: list[str] = []

    max_volume = _limit_value(limits, "max_volume_per_trade")
    if isinstance(max_volume, (int, float)) and not isinstance(max_volume, bool) and max_volume > 0:
        lines.append(
            f"* Maximum volume per trade: {_num(max_volume)} lots. A larger order is refused, and so "
            f"is a risk_percent that sizes above it."
        )

    max_open = _limit_value(limits, "max_open_positions")
    if isinstance(max_open, (int, float)) and not isinstance(max_open, bool) and max_open > 0:
        lines.append(
            f"* Maximum open positions: {int(max_open)}. A new entry is refused while that many are "
            f"already open — close or let one finish before opening another."
        )

    max_daily = _limit_value(limits, "max_daily_loss_percent")
    if isinstance(max_daily, (int, float)) and not isinstance(max_daily, bool) and max_daily > 0:
        lines.append(
            f"* Daily loss cap: {_num(max_daily)} percent of balance, measured over the broker-server "
            f"day. Once today's realized loss reaches it, new entries are refused."
        )

    allowlist = _limit_value(limits, "symbol_allowlist")
    if isinstance(allowlist, (list, tuple)) and allowlist:
        names = ", ".join(str(item) for item in allowlist if str(item).strip())
        if names:
            lines.append(
                f"* Symbol allowlist: {names}. Any other instrument is refused, however good the "
                f"signal is."
            )

    hours = _limit_value(limits, "trading_hours")
    if isinstance(hours, (list, tuple)) and hours:
        lines.append(
            f"* Trading hours: {_render_trading_hours(hours)}. Outside those windows entries are "
            f"refused."
        )

    return lines


def _margin_mode_paragraph(
    margin_mode: str,
    *,
    ladder_volume: float,
    orders_per_signal: int,
    order_lot_size: float,
) -> str:
    """State the account's margin mode and exactly what it does to a multi-target signal."""
    mode = (margin_mode or "").strip().lower()
    half = round(float(ladder_volume) / 2.0, 8)
    classic_line = (
        f"When volume and risk_percent are both omitted, a classic signal opens at most "
        f"{int(orders_per_signal)} orders of {order_lot_size} lots each, nearest targets first. "
        f"Passing an explicit volume still splits that volume one position per target, up to "
        f"{MAX_TP_SPLITS}."
    )
    ladder_line = (
        f"A ladder of 4 or more take-profits is different: it always becomes two legs totalling "
        f"{ladder_volume} lots ({half} + {half} when the broker's volume_min allows) — cover at "
        f"money 1R and runner at min(ceil((N+1)/2)+bias, N-1) — never one ticket per "
        f"celebration rung, and never a "
        f"risk-percent size."
    )
    if mode == "hedging":
        return (
            f"Margin mode: HEDGING. Several independent positions may exist on one symbol, so a "
            f"classic signal naming 2–3 take-profit targets is split into one sub-position per "
            f"target — up to {MAX_TP_SPLITS} — all sharing one signal_ref and one magic number. "
            f"The volume is divided by largest remainder so the parts sum exactly to the whole "
            f"and every part respects volume_min and volume_step; if the total is too small to "
            f"split that way the split count is reduced and the tool result says so. Each "
            f"sub-position carries its own take profit and its own ticket, so managing one later "
            f"does not touch the others. {classic_line} {ladder_line}"
        )
    if mode in ("netting", "exchange"):
        label = "NETTING" if mode == "netting" else "EXCHANGE (netting semantics)"
        return (
            f"Margin mode: {label}. Positions on one symbol merge: a second buy adds to the existing "
            f"position and a sell reduces it, so there is no way to hold two different stops on one "
            f"instrument. A classic signal naming several take-profit targets opens ONE position "
            f"carrying the first target; the remaining targets are stored as unmanaged and named "
            f"back to you in the tool result. When that happens, say in finish which targets nobody "
            f"is managing, and manage them yourself later with modify_position or a partial "
            f"close_position. {ladder_line} On netting the two legs cannot be separate tickets, so "
            f"the cover 1R is placed and the runner is recorded as unmanaged. {classic_line}"
        )
    return (
        "Margin mode: unknown — the account has not reported it yet. Call get_account before you size "
        "a multi-target signal: on hedging a classic 2–3 TP split becomes one sub-position per "
        f"target, on netting only the first target is placed. {classic_line} {ladder_line}"
    )


def build_system_prompt(
    *,
    margin_mode: str,
    armed: bool,
    source_title: str,
    limits: dict[str, Any],
    currency: str,
    ladder_volume: float | None = None,
    standing_prompt: str | None = None,
    orders_per_signal: int | None = None,
    order_lot_size: float | None = None,
    be_after_tp1: bool | None = None,
    use_fixed_order_lots: bool | None = None,
    max_entry_drift_usd: float | None = None,
) -> str:
    """Compose the full system prompt: the standing rules plus this account's current state.

    ``standing_prompt`` replaces the built-in rules when the operator saved a custom one.
    Account state, limits, and the live order-sizing numbers are still appended.
    """
    title = (source_title or "").strip() or "an unnamed channel"
    currency_code = (currency or "").strip()
    if currency_code:
        currency_line = (
            f"Account currency: {currency_code}. Every money figure in a tool result — balance, "
            f"free margin, profit, the cost of a stop — is in {currency_code}."
        )
    else:
        currency_line = (
            "Account currency: not reported yet. Call get_account before you read any money figure "
            "as a number of dollars."
        )
    lots = float(ladder_volume) if ladder_volume and ladder_volume > 0 else float(LADDER_VOLUME)
    order_count = (
        int(orders_per_signal)
        if orders_per_signal and int(orders_per_signal) > 0
        else ORDERS_PER_SIGNAL_DEFAULT
    )
    lot_each = (
        float(order_lot_size) if order_lot_size and float(order_lot_size) > 0 else ORDER_LOT_SIZE_DEFAULT
    )
    be_on = BE_AFTER_TP1_DEFAULT if be_after_tp1 is None else bool(be_after_tp1)
    fixed_on = USE_FIXED_ORDER_LOTS_DEFAULT if use_fixed_order_lots is None else bool(use_fixed_order_lots)
    try:
        drift = float(max_entry_drift_usd) if max_entry_drift_usd else float(MAX_ENTRY_DRIFT_USD_DEFAULT)
    except (TypeError, ValueError):
        drift = float(MAX_ENTRY_DRIFT_USD_DEFAULT)
    if drift <= 0.0:
        drift = float(MAX_ENTRY_DRIFT_USD_DEFAULT)
    base = (standing_prompt or "").strip() or SYSTEM_PROMPT

    sections: list[str] = [base]

    sections.append(
        "THIS ACCOUNT\n"
        f"Signal source: {title}. Every post in this run's context comes from that channel.\n"
        f"{currency_line}\n"
        f"{_margin_mode_paragraph(margin_mode, ladder_volume=lots, orders_per_signal=order_count, order_lot_size=lot_each)}"
    )

    sections.append(
        "OPERATOR ORDER SIZING\n"
        f"Classic signals (fewer than 4 take-profits): when volume and risk_percent are both "
        f"omitted, open up to {order_count} orders of {lot_each} lots each, nearest targets first. "
        f"Break-even after TP1 is {'on' if be_on else 'off'}. "
        f"hold_channel_sl, or a stop you already changed, leaves the channel stop in place. "
        f"Ladders of 4 or more take-profits ignore this count and use the ladder volume instead."
    )

    if fixed_on:
        fixed_line = (
            f"trading.use_fixed_order_lots is ON. Open {order_count} orders of {lot_each} lots. "
            "That size is the operator's law. Do not defer or skip because those lots feel too "
            "big for the account, because the stop is wide, or because reward:risk to TP1 looks "
            "poor. Omit volume and risk_percent."
        )
    else:
        fixed_line = (
            "trading.use_fixed_order_lots is OFF. A risk_percent from you may size a classic "
            "signal. A fill inside the drift is still an open: do not skip it only because "
            "price moved a little."
        )
    sections.append(
        "OPERATOR ENTRY LAW\n"
        "Classic market entries (fewer than 4 take-profits), gold and silver priced in "
        f"dollars: trading.max_entry_drift_usd is {_num(drift)} price units. Skip for price already "
        "moved ONLY when the live fill is more than that far from the channel entry or zone "
        "(absolute distance; a move in favour counts the same as a move against). At or inside "
        "it, you MUST call open_market. finish(deferred) and finish(ignored) are refused until "
        "that open is sent or a hard gate blocks it (disarmed, symbol allowlist, tag filter "
        "already applied upstream, stale signal, margin, a stop already through the market).\n"
        f"{fixed_line}"
    )

    if armed:
        sections.append(
            "SYSTEM STATE: ARMED\n"
            "Write tools reach the broker. Every order you send is a real order on this account, and "
            "it stays on after the run ends. Size it, stop it and justify it accordingly."
        )
    else:
        sections.append(
            "SYSTEM STATE: DISARMED\n"
            "Every write tool returns \"System is disarmed — no order was sent\" and nothing reaches "
            "the broker. Your reasoning, your tool calls and the exact orders you intended are still "
            "recorded and shown to the operator in full — that record is precisely what they read "
            "before arming the system. So reason exactly as you would when live: resolve the symbol, "
            "evaluate the entry, call open_market with only the levels the channel posted (omit the "
            "stop when none was posted), and let the refusal come back. Do not skip a check, do not "
            "shorten your reasoning, "
            "do not treat the run as a rehearsal. Report the refusal in finish and use decision "
            "\"deferred\" when the only reason you did not trade is that the system is disarmed."
        )

    limit_lines = _limit_lines(limits)
    if limit_lines:
        sections.append(
            "OPERATOR LIMITS IN FORCE\n"
            + "\n".join(limit_lines)
            + "\nA write tool blocked by one of these returns a refusal naming the limit and its "
            "value. Do not split an order to slip under a limit — report it in finish."
        )
    else:
        sections.append(
            "OPERATOR LIMITS IN FORCE\n"
            "None. Position size, open-position count, daily loss, symbol allowlist and trading hours "
            "are all switched off, so the only things that can refuse an order are the arm switch, "
            "a stop on the wrong side of price, the duplicate-signal guard and the broker itself."
        )

    return "\n\n".join(sections)


# --------------------------------------------------------------------------------------
# Formatting helpers — plain digits, never scientific notation
# --------------------------------------------------------------------------------------


def _num(value: Any) -> str:
    """Format a number in plain digits with no exponent and no trailing zeros."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return "n/a"
        magnitude = abs(value)
        if value != 0.0 and magnitude < 1e-6:
            text = f"{value:.12f}"
        elif magnitude >= 1e15:
            return f"{value:.2f}"
        else:
            text = f"{value:.8f}"
        text = text.rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _scalar(value: Any) -> str:
    """Render one tool-result value on a single line."""
    if value is None:
        return "none"
    if isinstance(value, (bool, int, float)):
        return _num(value)
    if isinstance(value, str):
        return " ".join(value.split()) or '""'
    if isinstance(value, (list, tuple, dict)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _kv_line(row: dict[str, Any], preferred: tuple[str, ...]) -> str:
    """Render a dict as `key=value` pairs, preferred keys first, then the rest alphabetically."""
    parts: list[str] = []
    seen: set[str] = set()
    for key in preferred:
        if key in row:
            parts.append(f"{key}={_scalar(row[key])}")
            seen.add(key)
    for key in sorted(row):
        if key in seen:
            continue
        parts.append(f"{key}={_scalar(row[key])}")
    return " | ".join(parts) if parts else "(empty)"


def _fmt_dt(value: datetime | None) -> str:
    """Format a stored naive-UTC timestamp for the model."""
    if value is None:
        return "unknown"
    return f"{value.strftime('%Y-%m-%d %H:%M:%S')} UTC"


def _quote_text(text: str) -> list[str]:
    """Wrap a message body in explicit delimiters so its own newlines cannot be misread."""
    body = (text or "").strip()
    if not body:
        return ["(no text)"]
    return ["--- BEGIN TEXT ---", body, "--- END TEXT ---"]


def _rows_from(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Pull the list of row dicts out of a tool result, tolerating either shape."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        for value in payload.values():
            if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
                return [row for row in value]
    return []


def _trade_line(trade: Trade) -> str:
    """One compact audit line for a trade row."""
    parts = [
        f"ticket {trade.ticket}",
        f"{trade.symbol} {trade.side} {_num(trade.volume)} lots",
        f"entry {_num(trade.entry_price)}",
        f"sl {_num(trade.sl)}",
        f"tp {_num(trade.tp)}",
        f"{trade.kind}/{trade.status}",
    ]
    if trade.ladder_role:
        parts.append(f"role {trade.ladder_role}")
    if trade.split_count and trade.split_count > 1:
        parts.append(f"split {trade.split_index + 1} of {trade.split_count}")
    if trade.unmanaged_tps:
        parts.append(f"unmanaged targets {_scalar(trade.unmanaged_tps)}")
    if trade.status == "closed":
        parts.append(f"closed {_fmt_dt(trade.closed_at)} cause {trade.close_cause or 'unknown'}")
        if trade.profit is not None:
            parts.append(f"profit {_num(trade.profit)}")
    elif trade.opened_at is not None:
        parts.append(f"opened {_fmt_dt(trade.opened_at)}")
    if trade.external:
        parts.append("EXTERNAL — opened outside this system, never modify it")
    return " | ".join(parts)


def _run_line(run: Run) -> str:
    """One compact audit line for a past run."""
    parts = [f"run {run.id[:8]}", run.kind, run.status]
    if run.decision:
        parts.append(f"decision {run.decision}")
    summary = " ".join((run.summary or "").split())
    if summary:
        parts.append(summary)
    elif run.error:
        parts.append(f"error: {' '.join(run.error.split())}")
    return " | ".join(parts)


# --------------------------------------------------------------------------------------
# Database lookups used by the context builder
# --------------------------------------------------------------------------------------


async def _load_images(session: AsyncSession, message_row_id: int) -> list[MessageImage]:
    """Every stored image of a message, in the order it was posted."""
    result = await session.execute(
        select(MessageImage)
        .where(MessageImage.message_row_id == message_row_id)
        .order_by(MessageImage.order_index, MessageImage.id)
    )
    return list(result.scalars().all())


async def _merge_chain_trades(
    session: AsyncSession, signal_ref: str | None, already: list[Trade]
) -> list[Trade]:
    """Add live and closed tickets from the rest of this reply chain.

    The parent row's own ``signal_ref`` misses a sibling reply that opened under its own
    ref, and it misses nothing on the common path where the teaser owns the tickets. Either
    way the model has to see the legs before it decides to modify.
    """
    from app.trading.guards import chain_signal_refs

    try:
        refs = await chain_signal_refs(session, signal_ref)
    except Exception as exc:  # noqa: BLE001 - context still renders without the extra rows
        log.warning("chain_trades_failed signal_ref=%s error=%s", signal_ref, exc)
        return list(already)
    if not refs:
        return list(already)
    result = await session.execute(
        select(Trade).where(Trade.signal_ref.in_(refs)).order_by(Trade.id)
    )
    merged: dict[int, Trade] = {int(trade.id): trade for trade in already}
    for trade in result.scalars().all():
        merged[int(trade.id)] = trade
    return list(merged.values())


async def _load_trades(
    session: AsyncSession, signal_ref: str | None, message_row_ids: list[int]
) -> list[Trade]:
    """Trades linked to a signal, by its revision-independent ref or by any of its revisions."""
    clauses = []
    if signal_ref:
        clauses.append(Trade.signal_ref == signal_ref)
    if message_row_ids:
        clauses.append(Trade.message_row_id.in_(message_row_ids))
    if not clauses:
        return []
    result = await session.execute(
        select(Trade).where(or_(*clauses)).order_by(Trade.id)
    )
    return list(result.scalars().all())


async def _load_message_revision(
    session: AsyncSession, source_id: int, message_id: int, *, below_revision: int | None = None
) -> Message | None:
    """The newest stored revision of one post, optionally the newest strictly below a revision."""
    stmt = (
        select(Message)
        .where(Message.source_id == source_id, Message.message_id == message_id)
        .order_by(Message.revision.desc(), Message.id.desc())
        .limit(1)
    )
    if below_revision is not None:
        stmt = (
            select(Message)
            .where(
                Message.source_id == source_id,
                Message.message_id == message_id,
                Message.revision < below_revision,
            )
            .order_by(Message.revision.desc(), Message.id.desc())
            .limit(1)
        )
    result = await session.execute(stmt)
    return result.scalars().first()


async def _load_recent_messages(
    session: AsyncSession,
    source_id: int,
    exclude_row_id: int,
    exclude_message_id: int,
    limit: int,
) -> list[Message]:
    """The last N *other* posts from this source, ingested before this one, oldest first.

    Other revisions of the post being processed are excluded on purpose: they are the same signal
    and the revision section already carries them, so listing them here again would read as extra
    posts the channel made.
    """
    if limit <= 0:
        return []
    result = await session.execute(
        select(Message)
        .where(
            Message.source_id == source_id,
            Message.id < exclude_row_id,
            Message.message_id != exclude_message_id,
        )
        .order_by(Message.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    # Selected by ingest recency, displayed in the order the channel actually posted them, which
    # differs after a catch-up batch.
    rows.sort(key=lambda row: (row.posted_at or datetime.min, int(row.id)))
    return rows


async def _load_runs_for(session: AsyncSession, message_row_ids: list[int]) -> dict[int, list[Run]]:
    """Group every run of a set of messages by message row id."""
    if not message_row_ids:
        return {}
    result = await session.execute(
        select(Run).where(Run.message_row_id.in_(message_row_ids)).order_by(Run.started_at, Run.id)
    )
    grouped: dict[int, list[Run]] = {}
    for run in result.scalars().all():
        if run.message_row_id is None:
            continue
        grouped.setdefault(int(run.message_row_id), []).append(run)
    return grouped


async def _load_trades_for_refs(
    session: AsyncSession, refs: list[str]
) -> dict[str, list[Trade]]:
    """Group every trade of a set of signal refs by ref."""
    if not refs:
        return {}
    result = await session.execute(
        select(Trade).where(Trade.signal_ref.in_(refs)).order_by(Trade.id)
    )
    grouped: dict[str, list[Trade]] = {}
    for trade in result.scalars().all():
        if trade.signal_ref is None:
            continue
        grouped.setdefault(str(trade.signal_ref), []).append(trade)
    return grouped


async def _fetch_parent_text(runtime: Runtime, source: Source | None, message_id: int) -> str | None:
    """Fetch an untracked parent's text straight from Telegram; never raises."""
    if source is None:
        return None
    telegram = getattr(runtime, "telegram", None)
    client = getattr(telegram, "client", None) if telegram is not None else None
    if client is None:
        return None
    try:
        fetched = await client.get_messages(source.chat_id, ids=int(message_id))
    except Exception as exc:  # noqa: BLE001 - context building must survive a dead client
        log.warning("parent_fetch_failed message_id=%s error=%s", message_id, exc)
        return None
    if isinstance(fetched, (list, tuple)):
        fetched = fetched[0] if fetched else None
    if fetched is None:
        return None
    text = getattr(fetched, "message", None) or getattr(fetched, "raw_text", None) or ""
    cleaned = str(text).strip()
    return cleaned or None


# --------------------------------------------------------------------------------------
# The per-run context
# --------------------------------------------------------------------------------------


async def _prepare_images(
    runtime: Runtime, rows: list[MessageImage]
) -> tuple[list[dict[str, Any]], int, int, list[str]]:
    """Prepare up to MAX_IMAGES_PER_RUN images; returns (blocks, used, tokens, failures)."""
    cfg = getattr(runtime, "settings", None) or _settings
    cap = int(getattr(cfg, "MAX_IMAGES_PER_RUN", MAX_IMAGES_PER_RUN))
    blocks: list[dict[str, Any]] = []
    tokens = 0
    failures: list[str] = []
    if cap <= 0:
        return blocks, 0, 0, failures
    for row in rows[:cap]:
        try:
            b64, media_type, width, height = await prepare_image(
                row.path,
                max_edge=int(cfg.IMAGE_MAX_EDGE),
                quality=int(cfg.IMAGE_JPEG_QUALITY),
                min_quality=int(cfg.IMAGE_MIN_QUALITY),
                max_bytes=int(cfg.IMAGE_MAX_BYTES),
            )
        except Exception as exc:  # noqa: BLE001 - one broken file must not kill the run
            log.warning("image_prepare_failed path=%s error=%s", row.path, exc)
            failures.append(f"image {row.order_index + 1} could not be decoded")
            continue
        blocks.append(image_block(media_type, b64))
        tokens += estimate_tokens(width, height)
    return blocks, len(blocks), tokens, failures


def _image_section(
    total: int, used: int, truncated: int, failures: list[str], cap: int
) -> list[str]:
    """Say exactly what happened to this message's images, including what was dropped."""
    lines: list[str] = ["--- IMAGES ---"]
    if total == 0:
        lines.append("No images were posted with this message.")
        return lines
    if used == 0:
        lines.append(
            f"{total} image(s) were posted with this message but none could be attached to this run."
        )
    elif used == 1:
        lines.append("1 image is attached below this text block.")
    else:
        lines.append(
            f"{used} images are attached below this text block, in the order they were posted."
        )
    if truncated > 0:
        lines.append(
            f"The channel posted {total} images as an album and a run carries at most {cap}: "
            f"{truncated} were dropped and you cannot see them. If the decision depends on what you "
            f"cannot see, say so in finish rather than guessing."
        )
    for failure in failures:
        lines.append(f"{failure} — it is not attached.")
    return lines


def _signal_section(
    message_row: Message,
    source: Source | None,
    age_seconds: int,
    stale: bool,
    stale_threshold: int,
) -> list[str]:
    """Identify the post: where it came from, when, how old it is, and what it says."""
    title = (source.title if source is not None else "").strip() or "unknown source"
    entity = (source.entity_type if source is not None else "") or "unknown"
    lines = [
        "=== INCOMING POST ===",
        f"Source: {title} ({entity})",
        f"Signal reference: {message_row.signal_ref}",
        f"Message id: {message_row.message_id} | revision: {message_row.revision} | "
        f"kind: {message_row.kind}",
        f"Posted at: {_fmt_dt(message_row.posted_at)}",
        f"Age now: {age_seconds} seconds",
    ]
    if message_row.permalink:
        lines.append(f"Link: {message_row.permalink}")
    else:
        lines.append("Link: none (this chat has no public permalink)")
    if message_row.catchup:
        lines.append(
            "Delivery: CATCH-UP — this post arrived while the system was offline and is only being "
            "read now. Its age is real, not a display artefact."
        )
    else:
        lines.append("Delivery: live")
    if stale:
        lines.append(
            f"STALE: this post is {age_seconds} seconds old, past the {stale_threshold} second "
            f"threshold. Price has moved. Call evaluate_entry before you decide anything about an "
            f"entry, and expect the move to have eaten part of the trade."
        )
    lines.append("")
    lines.append("--- POST TEXT ---")
    if (message_row.text or "").strip():
        lines.extend(_quote_text(message_row.text))
    elif message_row.image_count > 0:
        lines.append(
            "(empty — the channel posted an image with no caption. Everything this signal says is in "
            "the image attached below.)"
        )
    else:
        lines.append("(empty — this post carries no text and no image.)")
    return lines


def _reply_section(
    parent_row: Message | None,
    parent_text: str | None,
    parent_trades: list[Trade],
    reply_to_message_id: int,
) -> list[str]:
    """Attach the parent post and the trades that came out of it."""
    lines = ["=== THIS IS A REPLY ===", f"It replies to message {reply_to_message_id} in this channel."]
    if parent_row is not None:
        lines.append(
            f"Parent signal reference: {parent_row.signal_ref} | kind: {parent_row.kind} | "
            f"posted {_fmt_dt(parent_row.posted_at)} | revision {parent_row.revision}"
        )
        if parent_row.permalink:
            lines.append(f"Parent link: {parent_row.permalink}")
        lines.append("Parent text:")
        lines.extend(_quote_text(parent_row.text))
        if parent_row.image_count:
            lines.append(
                f"The parent post carried {parent_row.image_count} image(s); they are not attached to "
                f"this run."
            )
        if parent_trades:
            lines.append("Trades opened from the parent signal:")
            lines.extend(f"  {_trade_line(trade)}" for trade in parent_trades)
            live = [
                trade
                for trade in parent_trades
                if str(trade.status) in ("open", "pending") and not trade.external
            ]
            naked = [trade for trade in live if float(trade.sl or 0.0) <= 0.0]
            if naked:
                tickets = ", ".join(f"#{int(trade.ticket)}" for trade in naked)
                lines.append(
                    "INCOMPLETE LEGS: "
                    f"{tickets} {'is' if len(naked) == 1 else 'are'} already open or pending "
                    "with no stop. This reply's stop and ladder belong on those tickets. "
                    "Call modify_position on each open ticket and modify_order on each pending "
                    "ticket. The cover role gets the channel stop and the 1R price "
                    "evaluate_entry returns from the actual fill; the runner role gets the same "
                    "stop and the runner price. Do not call open_market or place_pending — a "
                    "second order is a duplicate entry for this chain."
                )
            lines.append(
                "Whatever this reply asks for — move the stop, attach the ladder, take partial "
                "profit, close it — applies to those tickets. Use modify_position or "
                "close_position on the ticket, not on a new order. TARGET N COMPLETE / TOUCH "
                "CHECK / HOLD / stickers on a ladder are not exits: if a cover 1R leg is gone "
                "and a runner is still on the original SL, trail that runner to break-even only "
                "when its runner_mode is be_after_cover; do not close it."
            )
        else:
            lines.append(
                "No trade was ever opened from the parent signal. If this reply asks you to manage "
                "something, there is nothing of ours to manage: check get_positions before assuming "
                "otherwise, and do not open a new position just because a reply mentions a target."
            )
    else:
        lines.append(
            "The parent post is NOT in the database — it predates this source being selected, so no "
            "trade is linked to it and there is no signal_ref for it."
        )
        if parent_text:
            lines.append("Parent text, fetched from Telegram:")
            lines.extend(_quote_text(parent_text))
        else:
            lines.append(
                "Its text could not be fetched either, so the only thing you know about the parent is "
                "what this reply says about it."
            )
        if parent_trades:
            lines.append(
                "Trades are nevertheless already linked to that chain (the teaser may have been "
                "ingested even though this revision of the parent text is missing):"
            )
            lines.extend(f"  {_trade_line(trade)}" for trade in parent_trades)
            lines.append(
                "Manage those tickets with modify_position or modify_order. Do not open a second "
                "entry. Legs whose stop is still 0 are incomplete: attach the channel stop and "
                "the ladder, do not call open_market or place_pending."
            )
        else:
            lines.append(
                "No trade is linked to that parent. Before acting on an instruction like "
                "\"move sl to be\", call get_positions and find a live position that actually "
                "matches it; if none does, finish with \"ignored\" and say the parent is untracked."
            )
    return lines


def _edit_section(
    message_row: Message, previous: Message | None, trades: list[Trade], runs: list[Run]
) -> list[str]:
    """Frame an edit as a revision of the same signal, never as a new one."""
    lines = [
        "=== THIS IS AN EDIT, NOT A NEW SIGNAL ===",
        f"This is revision {message_row.revision} of message {message_row.message_id}. The channel "
        f"edited its own post. It is the SAME signal, carrying the same signal reference "
        f"{message_row.signal_ref} — do not open a second position for it.",
    ]
    if previous is not None:
        lines.append(f"Previous revision ({previous.revision}) text:")
        lines.extend(_quote_text(previous.text))
        lines.append(
            "Compare it with the post text above and act only on what actually changed: a corrected "
            "stop is a modify_position, a corrected entry on an unfilled pending order is a "
            "modify_order, a cancelled call is a close or a cancel."
        )
    else:
        lines.append(
            "The previous revision is not stored, so you cannot diff the texts. Treat the post text "
            "above as the current truth and check get_positions for anything already open under this "
            "signal reference before acting."
        )
    if runs:
        lines.append("What earlier revisions of this signal already produced:")
        lines.extend(f"  {_run_line(run)}" for run in runs)
    if trades:
        lines.append("Trades already opened from this signal:")
        lines.extend(f"  {_trade_line(trade)}" for trade in trades)
    else:
        lines.append("No trade has been opened from this signal yet.")
    return lines


def _recent_section(
    rows: list[Message],
    runs_by_message: dict[int, list[Run]],
    trades_by_ref: dict[str, list[Trade]],
    limit: int,
) -> list[str]:
    """The recent history of this source with what each post came to."""
    lines = [f"=== RECENT POSTS FROM THIS SOURCE (up to {limit}, oldest first) ==="]
    if not rows:
        lines.append("Nothing else has been recorded from this source yet.")
        return lines
    for index, row in enumerate(rows, start=1):
        header = (
            f"[{index}] message {row.message_id} rev {row.revision} | {_fmt_dt(row.posted_at)} | "
            f"kind {row.kind} | status {row.status}"
        )
        if row.status == "skipped" and row.skip_reason:
            header += f" ({row.skip_reason})"
        lines.append(header)
        text = " ".join((row.text or "").split())
        if text:
            lines.append(f"    text: {text[:400]}{'…' if len(text) > 400 else ''}")
        elif row.image_count:
            lines.append(f"    text: (none — {row.image_count} image(s))")
        else:
            lines.append("    text: (none)")
        for run in runs_by_message.get(int(row.id), []):
            lines.append(f"    {_run_line(run)}")
        for trade in trades_by_ref.get(row.signal_ref, []):
            lines.append(f"    {_trade_line(trade)}")
    return lines


async def _account_section(runtime: Runtime) -> list[str]:
    """The live account, straight from the executor."""
    lines = ["=== ACCOUNT NOW ==="]
    executor = getattr(runtime, "executor", None)
    if executor is None:
        lines.append(
            "The account is unreachable in this run. Call get_account yourself before you size "
            "anything."
        )
        return lines
    try:
        account = await executor.get_account()
    except Exception as exc:  # noqa: BLE001 - a dead sidecar must not stop the run
        log.warning("context_account_failed error=%s", exc)
        lines.append(
            f"The account could not be read while this context was built ({_scalar(str(exc))}). Call "
            f"get_account yourself before you size anything."
        )
        return lines
    if not isinstance(account, dict) or not account:
        lines.append("The account returned nothing readable. Call get_account before sizing anything.")
        return lines
    seen: set[str] = set()
    for key in _ACCOUNT_KEY_ORDER:
        if key in account:
            lines.append(f"{key}: {_scalar(account[key])}")
            seen.add(key)
    for key in sorted(account):
        if key not in seen:
            lines.append(f"{key}: {_scalar(account[key])}")
    return lines


async def _positions_section(runtime: Runtime) -> list[str]:
    """Open positions and pending orders with the signal each came from."""
    lines = ["=== OPEN POSITIONS AND PENDING ORDERS ==="]
    executor = getattr(runtime, "executor", None)
    if executor is None:
        lines.append("Not available in this context. Call get_positions and get_orders yourself.")
        return lines

    async def _collect(name: str, *keys: str) -> list[dict[str, Any]] | None:
        method = getattr(executor, name, None)
        if method is None:
            return None
        try:
            payload = await method()
        except Exception as exc:  # noqa: BLE001 - never let a read kill the run
            log.warning("context_%s_failed error=%s", name, exc)
            return None
        return _rows_from(payload, *keys)

    positions = await _collect("get_positions", "positions")
    orders = await _collect("get_orders", "orders")

    if positions is None:
        lines.append("Open positions could not be read here — call get_positions yourself.")
    elif not positions:
        lines.append("No open positions.")
    else:
        lines.append(f"{len(positions)} open position(s):")
        lines.extend(f"  {_kv_line(row, _POSITION_KEY_ORDER)}" for row in positions)

    if orders is None:
        lines.append("Pending orders could not be read here — call get_orders yourself.")
    elif not orders:
        lines.append("No pending orders.")
    else:
        lines.append(f"{len(orders)} pending order(s):")
        lines.extend(f"  {_kv_line(row, _POSITION_KEY_ORDER)}" for row in orders)

    lines.append(
        "A row's signal_ref is the post it came from. A row marked external belongs to someone else — "
        "read it, never modify or close it."
    )
    return lines


def _ladder_stats_section(stats: ladder_stats.ChannelLadderStats) -> list[str]:
    """The ready-made blurb. The model must not reconstruct these counts from recent posts."""
    return [
        "=== CHANNEL LADDER STATS (server-computed; do not recount history) ===",
        stats.blurb(),
        "Pass runner_mode on open_market / place_pending. Follow suggested_mode unless you "
        "state a reason in finish. Keeping be_after_cover when suggested is hold_channel_sl "
        "is allowed (caution). Omit runner_tp_index to follow the suggested numbered TP; you "
        "may shift it ±1 with a reason in finish. Do not invent raw_SL / BE_stopouts / TP4+ "
        "from get_recent_signals.",
    ]


async def build_context(
    session: AsyncSession, runtime: Runtime, message_row: Message
) -> tuple[list[Msg], dict[str, Any]]:
    """Build the per-run user turn for one message, plus the meta the loop records."""
    cfg = getattr(runtime, "settings", None) or _settings
    stale_threshold = int(getattr(cfg, "STALE_SIGNAL_SECONDS", STALE_SIGNAL_SECONDS))
    context_signals = int(getattr(cfg, "CONTEXT_SIGNALS", CONTEXT_SIGNALS))
    image_cap = int(getattr(cfg, "MAX_IMAGES_PER_RUN", MAX_IMAGES_PER_RUN))

    now = datetime.now(UTC).replace(tzinfo=None)
    posted_at = message_row.posted_at or now
    age_seconds = max(0, int(round((now - posted_at).total_seconds())))
    stale = age_seconds > stale_threshold

    source = await session.get(Source, message_row.source_id)

    image_rows = await _load_images(session, int(message_row.id))
    image_blocks, images_used, image_tokens, image_failures = await _prepare_images(
        runtime, image_rows
    )
    truncated = max(0, len(image_rows) - image_cap)
    images_dropped = len(image_rows) - images_used

    is_reply = message_row.reply_to_message_id is not None
    is_edit = int(message_row.revision or 1) > 1
    parent_not_tracked = False

    sections: list[list[str]] = [
        _signal_section(message_row, source, age_seconds, stale, stale_threshold),
        _image_section(len(image_rows), images_used, truncated, image_failures, image_cap),
    ]

    if is_reply:
        parent_id = int(message_row.reply_to_message_id or 0)
        parent_row = await _load_message_revision(session, int(message_row.source_id), parent_id)
        parent_text: str | None = None
        parent_trades: list[Trade] = []
        if parent_row is None:
            parent_not_tracked = True
            parent_text = await _fetch_parent_text(runtime, source, parent_id)
        else:
            parent_trades = await _load_trades(
                session, parent_row.signal_ref, [int(parent_row.id)]
            )
        parent_ref = (
            parent_row.signal_ref
            if parent_row is not None
            else f"SIG:{int(message_row.source_id)}:{parent_id}"
        )
        parent_trades = await _merge_chain_trades(session, parent_ref, parent_trades)
        sections.append(
            _reply_section(parent_row, parent_text, parent_trades, parent_id)
        )

    if is_edit:
        previous = await _load_message_revision(
            session,
            int(message_row.source_id),
            int(message_row.message_id),
            below_revision=int(message_row.revision),
        )
        revision_ids = [int(previous.id)] if previous is not None else []
        signal_trades = await _load_trades(session, message_row.signal_ref, revision_ids)
        runs_by_message = await _load_runs_for(session, revision_ids)
        previous_runs = runs_by_message.get(int(previous.id), []) if previous is not None else []
        sections.append(_edit_section(message_row, previous, signal_trades, previous_runs))

    recent_rows = await _load_recent_messages(
        session,
        int(message_row.source_id),
        int(message_row.id),
        int(message_row.message_id),
        context_signals,
    )
    recent_runs = await _load_runs_for(session, [int(row.id) for row in recent_rows])
    recent_trades = await _load_trades_for_refs(
        session, sorted({row.signal_ref for row in recent_rows})
    )
    sections.append(_recent_section(recent_rows, recent_runs, recent_trades, context_signals))

    try:
        stats = await ladder_stats.stats_for_source(session, int(message_row.source_id))
        sections.append(_ladder_stats_section(stats))
    except Exception as exc:  # noqa: BLE001 - a stats miss must not kill the run
        log.warning("ladder_stats_context_failed error=%s", exc)

    sections.append(await _account_section(runtime))
    sections.append(await _positions_section(runtime))
    try:
        sections.append(cached_quote_lines())
    except Exception as exc:  # noqa: BLE001 - a cache miss must not kill the run
        log.warning("quote_cache_context_failed error=%s", exc)
        sections.append(
            [
                "=== CACHED QUOTE ===",
                "The quote cache could not be read. Call get_quote. An open the server cannot "
                "prove is inside the entry band is refused.",
            ]
        )

    closing = [
        "=== YOUR TURN ===",
        "Decide what this post is and act on it. Resolve the symbol before you name one. The "
        "CACHED QUOTE block is the server's latest sample — use it, and call get_quote when you "
        "need a live tick. Call evaluate_entry before any decision to enter. On a classic "
        "signal, obey must_open and entry_action: within trading.max_entry_drift_usd you open; "
        "farther than that you skip for price already moved and you do not chase. Do not defer "
        "a must_open entry because of reward:risk or lot size when trading.use_fixed_order_lots "
        "is on. The server refuses open_market and place_pending when the fill is outside that "
        "band or the entry is past the stale threshold. "
        "Do not invent a stop the channel did not post. Do not open a second trade when this "
        "signal reference, or any reply in its chain, already has open or pending legs — "
        "including legs whose stop is still 0. Modify those tickets (cover: channel stop and "
        "1R; runner: the same stop and the runner target). End the run with finish(decision, summary).",
    ]
    sections.append(closing)

    body = "\n\n".join("\n".join(section) for section in sections)
    content: list[dict[str, Any]] = [text_block(body)]
    content.extend(image_blocks)
    messages: list[Msg] = [{"role": "user", "content": content}]

    meta: dict[str, Any] = {
        "images_used": images_used,
        "images_dropped": images_dropped,
        "stale": stale,
        "signal_age_seconds": age_seconds,
        "catchup": bool(getattr(message_row, "catchup", False)),
        "is_reply": is_reply,
        "is_edit": is_edit,
        "parent_not_tracked": parent_not_tracked,
        "estimated_image_tokens": image_tokens,
    }
    return messages, meta


# --------------------------------------------------------------------------------------
# The stop-loss watchdog turn
# --------------------------------------------------------------------------------------


def watchdog_context(position: dict[str, Any], instruction: str) -> list[Msg]:
    """Build the one-job turn used by the SL watchdog: attach a stop to a naked position."""
    row = position if isinstance(position, dict) else {}
    attempts = int(getattr(_settings, "SL_FALLBACK_ATTEMPTS", 3))
    fallback_points = int(getattr(_settings, "SL_FALLBACK_POINTS", 500))
    note = " ".join((instruction or "").split())

    lines = [
        "=== STOPLESS POSITION — THIS RUN IS NOT A SIGNAL ===",
        "The stop-loss watchdog found an open position carrying this system's magic number with no "
        "stop loss attached. A position without a stop is the one state this system does not "
        "tolerate, and closing that gap is the only job of this run.",
        "",
        "--- POSITION ---",
        _kv_line(row, _POSITION_KEY_ORDER),
        "",
        "--- WHAT TO DO ---",
        "Attach a stop to that exact ticket with modify_position. Resolve the symbol to read its "
        "digits and stops_level_points, read the current price with get_quote, and look at recent "
        "candles with get_candles so the stop lands at a level that means something — below the last "
        "swing low for a buy, above the last swing high for a sell — rather than at an arbitrary "
        "distance. The stop must be on the correct side of entry and at least stops_level_points away "
        "from the current price.",
        "Do not open anything. Do not close anything. Do not touch any other ticket. Set sl only; "
        "leave tp as it is unless the position has none and a sensible target is obvious.",
        "If modify_position is refused, read the message — it names the required distance or the "
        "wrong side — fix the level and send it again.",
        f"If this position is still stopless after {attempts} watchdog cycles the executor stops "
        f"asking and attaches a blunt stop {fallback_points} points from entry by itself. A stop you "
        f"place now against real structure is better than that one.",
    ]
    if note:
        lines.extend(["", "--- OPERATOR / WATCHDOG NOTE ---", note])
    lines.extend(
        [
            "",
            "End with finish(decision, summary): \"acted\" if the stop is now attached, \"deferred\" "
            "if it was refused and you could not place it, and say the ticket and the level in the "
            "summary either way.",
        ]
    )

    return [{"role": "user", "content": [text_block("\n".join(lines))]}]
