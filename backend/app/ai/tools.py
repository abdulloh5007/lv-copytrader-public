"""The tool surface offered to the model — the single source of truth for names and schemas.

Every tool the AI can call is declared exactly once, here. ``app.ai.loop`` sends these specs to the
provider, ``app.trading.executor`` implements them, and the frontend renders their names; nothing
anywhere re-declares a tool name or a parameter. Descriptions are written for the model, not for a
developer: they carry the trading semantics that keep it out of trouble (stops are mandatory, lot
sizes are never computed by the model, a stop is never removed, every run ends with finish).

Order matters and is the order of BUILD_PROMPT §5.2 then §5.3: read tools first, write tools next,
``finish`` last.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from app.ai.base import ToolSpec
from app.config import MAX_CANDLES, MAX_TP_INPUT, MAX_TP_SPLITS, RECENT_SIGNALS_TOOL_MAX
from app.trading.constants import TIMEFRAMES

# The schema bounds the model sees are the configured limits themselves, so raising
# MAX_CANDLES in .env widens the tool instead of leaving the schema lying about it.
_TIMEFRAME_NAMES: list[str] = list(TIMEFRAMES)
_MAX_CANDLES: int = MAX_CANDLES
_RECENT_SIGNALS_MAX: int = RECENT_SIGNALS_TOOL_MAX
_MAX_TP_TARGETS: int = MAX_TP_INPUT
_MAX_TP_SPLITS: int = MAX_TP_SPLITS


TOOL_SPECS: list[ToolSpec] = [
    # ---------------------------------------------------------------- read tools
    ToolSpec(
        name="get_account",
        description=(
            "Read the live account: balance, equity, free margin, currency, leverage, open position "
            "count, today's realized P/L in broker-server time, and margin_mode as the literal string "
            "'hedging' or 'netting'. The margin mode decides what a classic 2–3 target signal can "
            "become: on hedging that volume is split into one position per take-profit, on netting "
            "only the first is placed and the rest are reported back as unmanaged. A ladder of 4+ "
            "TPs is always two fixed-lot legs (cover 1R + runner), not a percent of balance. Read "
            "this before sizing anything."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="resolve_symbol",
        description=(
            "Find the broker's real symbol for a trader's word such as 'gold', 'xauusd', 'us30' or "
            "'nas'. Brokers rename instruments and add suffixes (XAUUSDm, EURUSD.raw, US30.cash), so "
            "never guess or invent a symbol name: resolve first and pass the exact 'name' field back "
            "into every other tool. Returns up to 8 ranked candidates, each with description, digits, "
            "point, pip_size, pip_convention, volume_min/step/max, stops_level_points, spread_points "
            "and tradable. If more than one is plausible, choose by description and tradability. If "
            "nothing matches, the result names the closest symbols so you can try another word."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The instrument as the signal named it, e.g. 'gold', 'XAUUSD', 'US30'.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="get_quote",
        description=(
            "Current bid and ask for one exact broker symbol, with the spread in points and pips and "
            "the broker server time. Use the resolved symbol name, never the trader's word. A closed "
            "market or a symbol with no quotes comes back as an explicit failure, not as zeros, so a "
            "quote you receive is real."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Exact broker symbol name as returned by resolve_symbol.",
                },
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="price_from_distance",
        description=(
            "Turn a relative distance from a signal ('sl 20 pips below entry', 'tp 300 points above', "
            "'stop 0.5% away') into an absolute price. Never do this arithmetic yourself: a pip is "
            "trader slang with no broker-side definition and its size differs per symbol, so guessing "
            "it scales the stop by ten. Give the anchor price to measure from, the direction relative "
            "to that anchor, the number the signal stated and the unit it is in; the result is the "
            "price rounded to the symbol's digits plus the pip convention that was applied."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Exact broker symbol name as returned by resolve_symbol.",
                },
                "anchor": {
                    "type": "number",
                    "description": "The absolute price to measure from, e.g. the signal's entry price.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["above", "below"],
                    "description": "Whether the resulting price sits above or below the anchor.",
                },
                "distance": {
                    "type": "number",
                    "description": "The distance the signal stated, as a positive number.",
                },
                "unit": {
                    "type": "string",
                    "enum": ["pips", "points", "price", "percent"],
                    "description": (
                        "How to read 'distance': 'pips' trader pips, 'points' broker points (one "
                        "'point' is the last digit of the price), 'price' an absolute price offset, "
                        "'percent' a percentage of the anchor."
                    ),
                },
            },
            "required": ["symbol", "anchor", "direction", "distance", "unit"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="evaluate_entry",
        description=(
            "The late-entry instrument, and the tool that decides most trades. Signals reach you "
            "minutes after they were posted and price has usually already moved, so call this before "
            "every entry. Given the signal's intended entry, its stop and its targets it reports the "
            "current price, the distance from the signal entry in points, pips and price, "
            "'distance_as_fraction_of_sl' — how much of the intended stop the move has already eaten — "
            "the reward-to-risk if you enter now versus as posted, the age of the signal in seconds, "
            "and, for a classic signal, entry_action and must_open. trading.max_entry_drift_usd "
            "(default 5 price units on gold) is the only 'price already moved' test: farther than "
            "that, entry_action is skip and skip_reason is price_moved — do not open and do not "
            "place a pending order. At or inside it, must_open is true: call open_market. Do not "
            "defer because reward:risk to TP1 looks poor, because the stop is wide, or because "
            "the operator lot size looks large. When trading.use_fixed_order_lots is on, omit "
            "volume and risk_percent; the size is orders_per_signal times order_lot_size and it "
            "is not yours to shrink. A stale signal or a stop already through the market is a "
            "different refusal. The server also refuses an open outside that band."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Exact broker symbol name as returned by resolve_symbol.",
                },
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "The direction the signal called.",
                },
                "signal_entry": {
                    "type": "number",
                    "description": "The absolute entry price the signal stated or implied.",
                },
                "sl": {
                    "type": "number",
                    "description": "The absolute stop-loss price the signal stated or implied.",
                },
                "tp": {
                    "type": "array",
                    "items": {"type": "number"},
                    "maxItems": _MAX_TP_TARGETS,
                    "description": (
                        f"Absolute take-profit prices, 0 to {_MAX_TP_TARGETS} of them, nearest "
                        "first. Pass the FULL channel ladder when there are 4+ rungs — the server "
                        "collapses it to a 1R cover and a formula-picked runner "
                        "(min(ceil((N+1)/2)+bias, N-1), LONG excluded from N). Reward-to-risk in this "
                        "tool is still measured against the first price; omit for no target."
                    ),
                },
            },
            "required": ["symbol", "side", "signal_entry", "sl"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="get_positions",
        description=(
            "Every open position on the account, live from the terminal, each joined to the signal "
            "that created it (signal_ref) and an excerpt of that source message. This is how a "
            "follow-up such as 'tp1 hit, move sl to be' or 'close half' resolves to a real ticket, and "
            "how you avoid opening a second position for a signal that already has one. A two-leg "
            "ladder shows as two tickets sharing signal_ref: the nearer 1R TP is the cover, the "
            "farther TP is the runner — trail the runner to break-even after the cover hits, and "
            "do not close it on 'tp1 hit'. Rows marked external belong to another system or to the "
            "operator's own hand: read them for context, never modify or close them."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="get_orders",
        description=(
            "Every pending order on the account, live from the terminal, joined to its originating "
            "signal exactly like get_positions. Check here before placing a pending order for a signal "
            "you may already have handled, and to find the ticket a follow-up message refers to."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="get_candles",
        description=(
            "Recent OHLC bars for a symbol, oldest first, at most 200. A chart screenshot cannot tell "
            "you where price is now, how old the picture is, or whether the level drawn on it has "
            "already been taken out — use this to ground an image against real prices before acting on "
            "it, and to place a stop at a structural level rather than at a number you read off a "
            "picture."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Exact broker symbol name as returned by resolve_symbol.",
                },
                "timeframe": {
                    "type": "string",
                    "enum": _TIMEFRAME_NAMES,
                    "description": "Bar timeframe.",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_CANDLES,
                    "description": f"How many of the most recent bars to return, 1 to {_MAX_CANDLES}.",
                },
            },
            "required": ["symbol", "timeframe", "count"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="get_recent_signals",
        description=(
            "The last N messages from this channel with what was decided and what was traded for each. "
            "Use it when a message only makes sense in context — 'tp1 hit', '+110 pips', 'close it', "
            "'same setup as before' — so the follow-up attaches to the right earlier call instead of "
            "being read as a fresh signal. Do NOT use this to invent ladder runner-mode stats "
            "(raw_SL, BE_stopouts, TP4+): those come from get_channel_ladder_stats and the "
            "CHANNEL LADDER STATS block in context."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _RECENT_SIGNALS_MAX,
                    "description": f"How many recent messages to return, 1 to {_RECENT_SIGNALS_MAX}.",
                },
            },
            "required": ["limit"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="get_channel_ladder_stats",
        description=(
            "Server-computed ladder outcomes for this signal source over the last N fully-closed "
            "ladders (default 5): raw_SL (runner/ladder closed by the channel stop at a loss, not "
            "BE), BE_stopouts (runner stopped at breakeven after a BE trail), TP4+ (runner reached "
            "the far target), clean streak, suggested_mode (hold_channel_sl when the window "
            "has zero raw SL hits and is full; otherwise be_after_cover), and "
            "suggested_runner_tp_bias (0 bootstrap/raw_SL; +1 streak 3–4 or healthy TP4+; "
            "+2 strong streak). Also returns a ready-made blurb. Use this — or the CHANNEL "
            "LADDER STATS block already in context — when choosing runner_mode and "
            "runner_tp_index on a new ladder. Do not recount channel history or "
            "get_recent_signals to invent these numbers."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
    # ---------------------------------------------------------------- write tools
    ToolSpec(
        name="open_market",
        description=(
            "Open a position at the market now. Do not invent an SL or a TP the channel did not post. "
            "Omit 'sl' or send 0 when the channel has not posted a stop: the server accepts that "
            "incomplete entry, sizes it from trading.ladder_volume (two legs when the broker allows "
            "separate positions, otherwise one), and leaves both without a stop and without a "
            "take-profit. Do not pass risk_percent on that path. "
            "'tp' is a list of 0 to "
            f"{_MAX_TP_TARGETS} absolute take-profit prices; an empty list is legal and opens the "
            "position with no target for you to manage later. A classic 1–3 target list on a "
            "hedging account still splits one position per target (capped at "
            f"{_MAX_TP_SPLITS}). A ladder of 4+ rungs does NOT: the server opens TWO legs at the "
            "fixed ladder lot size (default 0.10, 0.05+0.05) — cover at money 1R from the fill, "
            "runner at min(ceil((N+1)/2)+bias, N-1) (LONG excluded from N, never the last "
            "numbered rung by default) — and ignores risk_percent and any tiny "
            "volume you pass. Never park the whole size on channel TP1. Pass runner_mode "
            "'be_after_cover' or 'hold_channel_sl' on a ladder (omit to use the server's "
            "suggested_mode from channel stats). Follow suggested_mode unless you have a reason "
            "in finish; keeping be_after_cover when suggested is hold_channel_sl is allowed "
            "(caution). Optional runner_tp_index shifts the suggested numbered TP by at most "
            "±1; omit it to follow the server. For non-ladder signals "
            "omit both 'volume' and 'risk_percent' to open the operator's orders-per-signal "
            "count at the operator's lot size (defaults 2 orders of 0.2). When "
            "trading.use_fixed_order_lots is on, that size is the law: do not pass a smaller "
            "volume or a risk_percent because the lots look large. Pass volume only when the "
            "channel stated a lot size. Sending both volume and risk_percent is refused. "
            "Never compute a lot size yourself. "
            "All prices are "
            "absolute; use price_from_distance for anything the signal expressed as a distance. "
            "'reason' is quoted verbatim in the operator's notification and in the trade history. "
            "Rejections come back as a readable message with the broker's reason. When "
            "trading.use_fixed_order_lots is on, do not retry a classic signal smaller — the "
            "operator lot size is the size. On a ladder do not substitute risk_percent or 0.01 lots. "
            "If this signal chain already has open or pending legs, including a teaser that is "
            "still without a stop, do not use this tool to add another entry — call "
            "modify_position. Passing the posted stop anyway modifies those tickets (cover at "
            "1R, runner at the formula target) and does not open a second pair. "
            "On a classic signal inside trading.max_entry_drift_usd you must call this tool. When "
            "trading.use_fixed_order_lots is on, omit volume and risk_percent. The server refuses "
            "this call when a classic gold fill is farther than trading.max_entry_drift_usd from "
            "the channel's printed entry (a ladder of 4 or more targets uses the wider fast-entry "
            "metal band), or when that entry is older than the stale threshold. Do not retry the "
            "refusal as place_pending."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Exact broker symbol name as returned by resolve_symbol.",
                },
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "'buy' to go long, 'sell' to go short.",
                },
                "sl": {
                    "type": "number",
                    "description": (
                        "Absolute stop-loss price the channel posted, below the entry for a buy and "
                        "above it for a sell, at least the symbol's stops_level_points away. Omit or "
                        "send 0 when the channel did not post a stop — the order opens without one. "
                        "Never invent a price. 0 means no stop, not a request to delete one later."
                    ),
                },
                "tp": {
                    "type": "array",
                    "items": {"type": "number"},
                    "maxItems": _MAX_TP_TARGETS,
                    "description": (
                        f"0 to {_MAX_TP_TARGETS} absolute take-profit prices, nearest first. Pass "
                        "the full ladder when the channel named 4+ rungs; the server will not put "
                        "a broker TP on every celebration level. Omit or send an empty list to "
                        "open with no target."
                    ),
                },
                "volume": {
                    "type": "number",
                    "description": (
                        "Explicit lot size for a single-target or classic 1–3 TP signal that has a "
                        "posted stop. Ignored on a 4+ TP ladder and on an incomplete entry with "
                        "no posted stop (the server uses trading.ladder_volume). On a classic "
                        "signal, omit it so the server uses trading.order_lot_size on each of "
                        "trading.orders_per_signal orders. Pass it only when the channel stated "
                        "a lot size; mutually exclusive with risk_percent."
                    ),
                },
                "risk_percent": {
                    "type": "number",
                    "description": (
                        "Percent of account balance to risk between entry and stop, for "
                        "single-target or classic 1–3 TP signals that have a posted stop. HARD "
                        "RULE: do not send this on a 4+ TP ladder or on an incomplete entry with "
                        "no posted stop — the server ignores it and opens trading.ladder_volume. "
                        "Mutually exclusive with volume on classic signals; omit both to use "
                        "trading.orders_per_signal and trading.order_lot_size."
                    ),
                },
                "runner_mode": {
                    "type": "string",
                    "enum": ["be_after_cover", "hold_channel_sl"],
                    "description": (
                        "On a ladder: after the 1R cover closes, be_after_cover moves the runner "
                        "SL to breakeven (the watchdog does this); hold_channel_sl leaves the "
                        "channel stop so pullbacks can continue to far TPs. Omit on a ladder to "
                        "use the server's suggested_mode from CHANNEL LADDER STATS. On a classic "
                        "multi-order signal, hold_channel_sl is the opt-out from the server's "
                        "break-even-after-TP1 trail; omit it and the server does that move. "
                        "Follow suggested on a ladder unless you give a reason in finish."
                    ),
                },
                "runner_tp_index": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Ladder only. 1-based numbered-TP index for the runner (LONG excluded "
                        "from N). Omit to use the server's suggested_runner_tp_index. You may "
                        "shift suggested by at most ±1; a larger jump is refused. State the "
                        "reason in finish."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence on why this trade is being opened, quoted to the operator and "
                        "stored in the trade history."
                    ),
                },
            },
            "required": ["symbol", "side", "reason"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="place_pending",
        description=(
            "Place a pending order at a stated price. The server refuses this call when a classic "
            "gold fill is farther than trading.max_entry_drift_usd from the channel's printed "
            "entry (a ladder uses the wider fast-entry metal band), and when that entry is older "
            "than the stale threshold — a pending order is not a way to catch a missed print. "
            "'order_type' is 'limit' to fill on a move "
            "back to a better price than the market, or 'stop' to fill on a breakout through the price; "
            "the server combines side, order_type and where the price sits relative to the market to "
            "pick BUY_LIMIT, SELL_LIMIT, BUY_STOP or SELL_STOP, and refuses an impossible combination "
            "by naming the type it inferred and why it cannot work. Do not invent an SL or a TP. "
            "Omit 'sl' or send 0 when the channel has not posted a stop — the server accepts the "
            "incomplete order and sizes it from trading.ladder_volume, same as open_market. "
            "'tp' is a list of 0 to "
            f"{_MAX_TP_TARGETS} absolute prices, handled exactly as in open_market (ladders of 4+ "
            "become two fixed-lot legs; classic 1–3 TPs split as before). Supply exactly one of "
            "'volume' or 'risk_percent' on a non-ladder signal, or neither for the account default; "
            "both is refused. Ladders ignore risk_percent. A successful pending order is accepted "
            "rather than filled, so nothing is on the book until price arrives. If this chain "
            "already has open or pending legs, including a stopless teaser, do not place a "
            "second pair — call modify_order or modify_position. Passing the posted stop "
            "anyway attaches it to those tickets and does not add another order."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Exact broker symbol name as returned by resolve_symbol.",
                },
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "The direction the order opens in once it fills.",
                },
                "order_type": {
                    "type": "string",
                    "enum": ["limit", "stop"],
                    "description": (
                        "'limit' fills on a move back to a better price than the market; 'stop' fills "
                        "on a breakout through the price."
                    ),
                },
                "price": {
                    "type": "number",
                    "description": "Absolute price the order waits at.",
                },
                "sl": {
                    "type": "number",
                    "description": (
                        "Absolute stop-loss price the channel posted, below 'price' for a buy and "
                        "above it for a sell. Omit or send 0 when the channel did not post a stop. "
                        "Never invent a price."
                    ),
                },
                "tp": {
                    "type": "array",
                    "items": {"type": "number"},
                    "maxItems": _MAX_TP_TARGETS,
                    "description": (
                        f"0 to {_MAX_TP_TARGETS} absolute take-profit prices, nearest first. "
                        "Ladders of 4+ rungs are collapsed the same way as open_market. Omit or "
                        "send an empty list to place the order with no target."
                    ),
                },
                "expiry_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Cancel the order automatically after this many minutes if it has not filled. "
                        "Omit to leave it good-till-cancelled."
                    ),
                },
                "volume": {
                    "type": "number",
                    "description": (
                        "Explicit lot size for a non-ladder signal. Ignored on a 4+ TP ladder. "
                        "Mutually exclusive with risk_percent."
                    ),
                },
                "risk_percent": {
                    "type": "number",
                    "description": (
                        "Percent of account balance to risk between the order price and the stop, "
                        "for non-ladder signals only. Do not send this on a 4+ TP ladder. "
                        "Mutually exclusive with volume; omit both to use trading.orders_per_signal "
                        "and trading.order_lot_size."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence on why this order is being placed, quoted to the operator and "
                        "stored in the trade history."
                    ),
                },
                "runner_mode": {
                    "type": "string",
                    "enum": ["be_after_cover", "hold_channel_sl"],
                    "description": (
                        "Same as open_market. On a classic multi-order signal, hold_channel_sl opts "
                        "out of break-even after TP1. On a ladder, omit to use suggested_mode."
                    ),
                },
                "runner_tp_index": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Ladder only. Same as open_market: 1-based numbered-TP index, at most ±1 "
                        "from suggested. Omit to follow the server."
                    ),
                },
            },
            "required": ["symbol", "side", "order_type", "price", "reason"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="modify_position",
        description=(
            "Move the stop or the target of an open position. This is how a later channel post "
            "attaches the stop and the server's cover/runner targets to an entry that opened "
            "without them: pass the channel's stop and the prices evaluate_entry returned, and "
            "do not invent a level the channel did not post. It is also how 'move sl to break even', "
            "'trail the stop under the last low' or 'tp2 now' become real. After a ladder cover "
            "hits its 1R TP, use this to lock the remaining runner to break-even only when "
            "that ladder's runner_mode is be_after_cover (or the channel explicitly asks). "
            "If runner_mode is hold_channel_sl, leave the channel stop so pullbacks can "
            "continue to far TPs. Do not close the runner on 'TARGET 1 COMPLETE'. A stop can never "
            "be removed: 'sl' of 0 is refused with a message saying to move it instead, so if you "
            "want the trade out of harm's way move the stop to entry rather than clearing it. Omit "
            "a field to leave it unchanged; both prices are absolute and must respect the symbol's "
            "minimum stop distance from the current market. The ticket comes from get_positions, "
            "and positions marked external must not be touched."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "integer",
                    "description": "Position ticket from get_positions.",
                },
                "sl": {
                    "type": "number",
                    "description": (
                        "New absolute stop-loss price. Omit to leave the stop unchanged. A value of 0 "
                        "is refused — a stop is never removed, only moved."
                    ),
                },
                "tp": {
                    "type": "number",
                    "description": (
                        "New absolute take-profit price. Omit to leave the target unchanged; 0 clears "
                        "the target, which is allowed because a target is optional."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence on why the position is being changed, quoted to the operator."
                    ),
                },
            },
            "required": ["ticket", "reason"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="modify_order",
        description=(
            "Change the entry price, the stop or the target of a pending order that has not filled yet "
            "— for example when the channel revises a level. A stop can never be removed: 'sl' of 0 is "
            "refused with a message saying to move it instead. Omit a field to leave it unchanged; all "
            "prices are absolute. The ticket comes from get_orders. If the order should not exist at "
            "all any more, cancel it instead of moving it out of the way."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "integer",
                    "description": "Pending order ticket from get_orders.",
                },
                "price": {
                    "type": "number",
                    "description": "New absolute price for the order to wait at. Omit to leave unchanged.",
                },
                "sl": {
                    "type": "number",
                    "description": (
                        "New absolute stop-loss price. Omit to leave the stop unchanged. A value of 0 "
                        "is refused — a stop is never removed, only moved."
                    ),
                },
                "tp": {
                    "type": "number",
                    "description": (
                        "New absolute take-profit price. Omit to leave the target unchanged; 0 clears "
                        "the target."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence on why the order is being changed, quoted to the operator."
                    ),
                },
            },
            "required": ["ticket", "reason"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="close_position",
        description=(
            "Close an open position, fully or partially. Omit 'volume' to close all of it; pass a "
            "volume smaller than the position's to scale out, which is what 'close half' or 'take "
            "partial at tp1' means — the remainder stays open with its stop. The volume must respect "
            "the symbol's volume step and must not exceed what is open, and the ticket comes from "
            "get_positions. Positions marked external are not yours to close."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "integer",
                    "description": "Position ticket from get_positions.",
                },
                "volume": {
                    "type": "number",
                    "description": (
                        "Lots to close. Omit to close the whole position; a smaller value closes part "
                        "of it and leaves the rest open."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence on why the position is being closed, quoted to the operator and "
                        "stored in the trade history."
                    ),
                },
            },
            "required": ["ticket", "reason"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="cancel_order",
        description=(
            "Cancel a pending order that has not filled — when the channel voids the setup, when price "
            "has run so far that the level no longer means anything, or when a newer message replaces "
            "it. This removes the order from the book; it does nothing to positions that already "
            "opened. The ticket comes from get_orders."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "integer",
                    "description": "Pending order ticket from get_orders.",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence on why the order is being cancelled, quoted to the operator."
                    ),
                },
            },
            "required": ["ticket", "reason"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="close_signal_exposure",
        description=(
            "Close every open position and cancel every pending order on one signal chain "
            "(the teaser, the ladder reply, the cover and the runner). Pass signal_ref as "
            "SIG:<source>:<message id> of this post or of the parent it replies to. Omit it "
            "to use this run's own chain. The server finds the MT5 tickets. Do not call "
            "get_positions, get_orders or get_recent_signals first — that only adds latency. "
            "Use this for a full close of the whole trade (FULL CLOSE TRADE, close trade, "
            "full close, including buy or sell). A partial scale-out is still close_position "
            "on one ticket."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "signal_ref": {
                    "type": "string",
                    "description": (
                        "SIG:<source>:<message id> of the chain to flatten. Omit to use the "
                        "signal this run is already about."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence on why the whole chain is being closed, quoted to the operator."
                    ),
                },
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="notify",
        description=(
            "Send a short message to the operator's Telegram. Use it for something a person should know "
            "that is not itself an order: a signal you deliberately skipped and why, an ambiguity you "
            "had to resolve, a channel message you could not read, or a risk worth flagging. It places, "
            "changes and closes nothing, and it is not a substitute for finish — the run still has to "
            "end with finish."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Plain text for the operator, a few sentences at most.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="finish",
        description=(
            "End the run. EVERY run must end with exactly this call, including runs where you did "
            "nothing at all: a run that ends without it is recorded as incomplete and shown to the "
            "operator in red. 'decision' is 'acted' when you sent at least one order, modification, "
            "close or cancel; 'ignored' when the message needed no trading action, such as chatter, an "
            "unrelated post, or a signal already handled; 'deferred' when it was a real signal you "
            "chose not to act on yet, for example because the fill is farther than "
            "trading.max_entry_drift_usd or the market was closed. finish(deferred) and "
            "finish(ignored) are refused when evaluate_entry returned must_open and you have not "
            "called open_market, unless a hard gate (disarmed, symbol allowlist, stale, margin, "
            "a stop already through the market) already blocked that open. Do not defer because "
            "the operator's fixed lot size looks large or because reward:risk to TP1 looks poor. "
            "'summary' is one or two sentences the operator reads as the record of "
            "this run — name the instrument, what you did and the reason, and if you refused or were "
            "refused, say what stopped you."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["acted", "ignored", "deferred"],
                    "description": (
                        "'acted' if anything was sent to the broker, 'ignored' if no action was "
                        "needed, 'deferred' if it was a real signal left alone for now."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "One or two sentences recording what happened and why, written for the "
                        "operator. If you overrode suggested runner_mode or runner_tp_index, "
                        "the reason belongs here."
                    ),
                },
            },
            "required": ["decision", "summary"],
            "additionalProperties": False,
        },
    ),
]


READ_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "get_account",
        "resolve_symbol",
        "get_quote",
        "price_from_distance",
        "evaluate_entry",
        "get_positions",
        "get_orders",
        "get_candles",
        "get_recent_signals",
        "get_channel_ladder_stats",
        "notify",
        "finish",
    }
)

WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "open_market",
        "place_pending",
        "modify_position",
        "modify_order",
        "close_position",
        "cancel_order",
        "close_signal_exposure",
    }
)

#: The only tools an ``sl_watchdog`` run may call.
#:
#: BUILD_PROMPT §6.4 defines that run as "an agent run whose only job is to attach a stop".
#: Handing it ``open_market``/``place_pending``/``close_position``/``cancel_order`` would leave
#: that scope enforced by nothing but a sentence in the instruction string — and the run carries
#: no ``signal_ref``, so the §5.5 duplicate guard cannot refuse anything it opens. Restricting the
#: schema is the enforcement: a tool the model is never shown is a tool it cannot call.
WATCHDOG_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "get_account",
        "get_positions",
        "get_quote",
        "resolve_symbol",
        "price_from_distance",
        "get_candles",
        "modify_position",
        "notify",
        "finish",
    }
)


def tools_for_kind(kind: str) -> list[ToolSpec]:
    """The tool schemas a run of this ``kind`` is allowed to see."""
    if kind == "sl_watchdog":
        return [spec for spec in TOOL_SPECS if spec.name in WATCHDOG_TOOL_NAMES]
    return list(TOOL_SPECS)


_SPECS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


def to_anthropic(specs: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Render tool specs in Anthropic's Messages API shape."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": deepcopy(spec.input_schema),
        }
        for spec in specs
    ]


def to_openai(specs: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Render tool specs in the OpenAI chat-completions function shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": deepcopy(spec.input_schema),
            },
        }
        for spec in specs
    ]


def spec_by_name(name: str) -> ToolSpec | None:
    """Look up one tool spec by its exact name."""
    return _SPECS_BY_NAME.get(name)
