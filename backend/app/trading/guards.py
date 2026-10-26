"""The unconditional gates, the operator's optional limits, and the stopless-position watchdog.

Two rules shape this whole module.

**Guards return, they never raise.** Every public function here answers either ``None``
("this call may proceed") or a **refusal string** written for the model to read and act on.
A guard that threw would kill the agent run instead of giving the model something to work
with, so every database read, every RPC call and every settings value is wrapped. Where a
guard cannot evaluate itself it *fails closed* — a refusal is recoverable, a silently
skipped protection is not.

**Only three gates are unconditional** (BUILD_PROMPT §5.5, §6):

1. :func:`check_armed` — the operator's on/off switch.
2. The stop-loss rule — :func:`check_sl_present`, :func:`check_sl_not_removed`,
   :func:`check_sl_side`, :func:`check_stops_level` and :class:`SlWatchdog`.
3. :func:`check_duplicate_signal` — one signal never opens two live tickets.

Everything :func:`check_optional_limits` evaluates is an **operator limit that ships off**.
A ``null`` or empty limit is not evaluated at all — not read defensively, not compared
against a default, not evaluated and passed. It costs nothing and it blocks nothing.
There is no risk-based veto in this file that is enabled out of the box.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import datetime, time as _time, timedelta
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import settings
from app.db import get_setting, session_scope
from app.errors import SignalPilotError
from app.models import Message, StoplessEpisode, Trade, utcnow
from app.trading.constants import POSITION_TYPE_NAMES
from app.trading.pricing import validate_stops
from app.trading.symbols import BROKER_SUFFIXES, broker_day_start_ts
from app.util.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - type-checker only, and it breaks a cycle
    from app.runtime import Runtime
    from app.trading.symbols import SymbolRef

log = get_logger(__name__)

# --------------------------------------------------------------------------------------
# Refusal messages. The three below are named in CONTRACTS.md §12 and their exact text is
# part of the contract: the executor returns them verbatim to the model and the tests
# assert on them.
# --------------------------------------------------------------------------------------
SL_REQUIRED: Final[str] = "a stop loss is required — resend with sl"
SL_NOT_REMOVABLE: Final[str] = "the stop cannot be removed; move it instead"
DISARMED: Final[str] = "System is disarmed — no order was sent"

#: Trade statuses that mean "this ticket is alive at the broker right now".
LIVE_TRADE_STATUSES: Final[tuple[str, ...]] = ("open", "pending")

#: How far a reply chain is walked. A channel teaser plus its ladder reply is two hops;
#: the cap only stops a corrupted reply cycle from looping forever.
_CHAIN_WALK_LIMIT: Final[int] = 16

#: ``SIG:<source id>:<telegram message id>``. Edits share it; a reply does not.
_SIGNAL_REF_RE: Final[re.Pattern[str]] = re.compile(r"^SIG:(\d+):(\d+)$")

#: The settings keys :func:`check_optional_limits` reads. Every one ships null or empty.
LIMIT_KEYS: Final[tuple[str, ...]] = (
    "limits.symbol_allowlist",
    "limits.trading_hours",
    "limits.max_volume_per_trade",
    "limits.max_open_positions",
    "limits.max_daily_loss_percent",
)

# --------------------------------------------------------------------------------------
# ENUM_DEAL_ENTRY. app.trading.constants carries every integer the executor *sends*; these
# describe a deal the executor only ever *reads*, when it totals today's realized P/L.
# They are literals for the same reason every integer in constants.py is: the backend
# cannot import MetaTrader5 (BUILD_PROMPT §1.2).
#
# DEAL_ENTRY_IN opens volume and realizes nothing. The other three all close volume, so
# all three carry realized profit/swap/commission and all three count towards a daily loss.
# --------------------------------------------------------------------------------------
DEAL_ENTRY_IN: Final[int] = 0
DEAL_ENTRY_OUT: Final[int] = 1
DEAL_ENTRY_INOUT: Final[int] = 2
DEAL_ENTRY_OUT_BY: Final[int] = 3
REALIZING_DEAL_ENTRIES: Final[frozenset[int]] = frozenset(
    {DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY}
)

#: Weekday names for `trading_hours`, indexed the way the setting is: Monday == 0.
DAY_NAMES: Final[tuple[str, ...]] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

#: Every weekday, used when a `trading_hours` entry omits `days`.
ALL_DAYS: Final[frozenset[int]] = frozenset(range(7))

#: How many days ahead :meth:`_Window.next_start_after` looks for the next open window.
#: Nine covers every weekday plus the wrap onto the following week.
_NEXT_WINDOW_HORIZON_DAYS: Final[int] = 9

#: history_deals_get is padded a day into the future because broker-server time runs ahead
#: of UTC and a naive datetime is read as UTC by the MT5 package (BUILD_PROMPT §2).
_HISTORY_FUTURE_PAD_SECONDS: Final[float] = 86400.0


def _fmt(value: float) -> str:
    """Format a price or volume for a refusal message without exponent notation or noise."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(number)
    text = f"{number:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_volume(value: float) -> str:
    """Format a lot size: trailing zeros trimmed, but never fewer than two decimals.

    Lots are read as lots — `1.00`, not `1` — because a bare integer in a sentence about
    volume is the one place an operator could mistake a lot size for a ticket count.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(number)
    trimmed = f"{number:.8f}".rstrip("0")
    whole, _, fraction = trimmed.partition(".")
    fraction = (fraction + "00")[: max(2, len(fraction))]
    return f"{whole}.{fraction}"


def _is_real_price(value: Any) -> bool:
    """Report whether a value is a usable, strictly positive price level."""
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0


# ======================================================================================
# 1. The stop-loss rule (BUILD_PROMPT §6.1 - §6.3)
# ======================================================================================


def check_sl_present(sl: float | None) -> str | None:
    """Return the old refusal text when ``sl`` is not a real price.

    Opening no longer calls this. An incomplete channel post is allowed through without a
    stop; the stopless watchdog still covers a position that stays naked. The helper remains
    so a caller that wants the historical wording can ask for it.
    """
    if _is_real_price(sl):
        return None
    return SL_REQUIRED


def check_sl_not_removed(sl: float | None) -> str | None:
    """Refuse a modification that would take an existing stop away.

    The whole §6.2 back door lives in the difference between two falsy values, so this
    guard reads them as opposites:

    * ``sl is None`` — the caller is **not touching the stop**. ``modify_position`` and
      ``modify_order`` take ``sl`` and ``tp`` as optional fields, and a call that only
      moves the take profit must be allowed through. Returns ``None``.
    * ``sl == 0.0`` — the caller **explicitly asked to remove the stop**, because 0.0 is
      how MT5 spells "no stop loss" in a request. Refused with :data:`SL_NOT_REMOVABLE`.

    Without that distinction the executor would have to choose between refusing every
    take-profit-only edit and letting the model satisfy §6.1 at open time and clear the
    stop a second later, legally. Callers must therefore pass ``None`` — never ``0.0`` —
    for a field the model left out.

    A negative or non-finite ``sl`` is not a stop either, and is refused the same way.
    """
    if sl is None:
        return None
    if _is_real_price(sl):
        return None
    return SL_NOT_REMOVABLE


def check_sl_side(side: str, entry: float, sl: float) -> str | None:
    """Refuse a stop on the wrong side of the entry, naming the side it belongs on.

    A buy is stopped out below its entry and a sell above it. The message names the
    corrected side and the price to move past, so the model can fix it in one turn
    instead of guessing.
    """
    direction = str(side or "").strip().lower()
    if direction not in ("buy", "sell"):
        return f"side must be 'buy' or 'sell', got {side!r}"
    if not _is_real_price(sl):
        return SL_REQUIRED
    try:
        entry_price = float(entry)
        stop_price = float(sl)
    except (TypeError, ValueError):
        return f"entry and sl must be numbers, got entry={entry!r} sl={sl!r}"
    if not math.isfinite(entry_price) or entry_price <= 0.0:
        return f"the entry price must be a positive number, got {entry!r}"

    if direction == "buy":
        if stop_price < entry_price:
            return None
        return (
            f"a buy's stop loss must be below the entry: sl {_fmt(stop_price)} is not below "
            f"entry {_fmt(entry_price)} — move the stop below {_fmt(entry_price)}"
        )
    if stop_price > entry_price:
        return None
    return (
        f"a sell's stop loss must be above the entry: sl {_fmt(stop_price)} is not above "
        f"entry {_fmt(entry_price)} — move the stop above {_fmt(entry_price)}"
    )


def check_stops_level(
    ref: SymbolRef,
    side: str,
    entry: float,
    sl: float,
    tp: float,
    bid: float,
    ask: float,
    *,
    pending: bool,
) -> str | None:
    """Refuse stops the broker would reject with 10016, naming the required distance.

    The arithmetic belongs to :func:`app.trading.pricing.validate_stops`; this wrapper
    exists so the executor reaches every gate through one module and so a failure inside
    the validator becomes a refusal rather than an exception thrown into the agent loop.
    An unreadable symbol fails closed: a stop we cannot validate is not a stop we send.
    """
    try:
        return validate_stops(ref, side, entry, sl, tp, bid, ask, pending=pending)
    except SignalPilotError as exc:
        return exc.message
    except Exception as exc:  # noqa: BLE001 - a guard must never raise into the loop
        name = getattr(ref, "name", "the symbol")
        log.warning("stops_validation_failed symbol=%s error=%s", name, exc)
        return (
            f"the stop and take-profit levels could not be validated for {name} ({exc}) — "
            "no order was sent"
        )


# ======================================================================================
# 2. The armed gate (BUILD_PROMPT §5.5)
# ======================================================================================


async def check_armed(s: AsyncSession) -> str | None:
    """Refuse every write while the operator's ARM switch is off.

    Disarmed, the agent loop still runs end to end and the dashboard shows the full
    reasoning and the intended orders — only the broker call is withheld. An unreadable
    setting is treated as disarmed, because failing open here would trade on a database
    error. The kill switch and the SL fallback bypass this gate by not calling it
    (``RunContext.bypass_arm``), which is why disarming happens *after* the closes.
    """
    try:
        value = await get_setting(s, "trading.armed", False)
    except Exception as exc:  # noqa: BLE001 - a broken store must never arm the system
        log.warning("armed_check_failed error=%s", exc)
        return DISARMED
    return None if bool(value) else DISARMED


# ======================================================================================
# 3. The duplicate-signal guard (BUILD_PROMPT §5.5)
# ======================================================================================


def _describe_trades(rows: list[Trade]) -> str:
    """Render live tickets as `#500123 XAUUSDm buy 0.05`, comma separated."""
    return ", ".join(
        f"#{row.ticket} {row.symbol} {row.side} {_fmt_volume(row.volume)}" for row in rows
    )


def parse_signal_ref(signal_ref: str | None) -> tuple[int, int] | None:
    """Split ``SIG:<source>:<message id>``, or None when the text is not that shape."""
    if not signal_ref:
        return None
    match = _SIGNAL_REF_RE.match(str(signal_ref).strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


async def _reply_parent_id(session: AsyncSession, source_id: int, message_id: int) -> int | None:
    """The telegram id this post replies to, from its newest stored revision."""
    result = await session.execute(
        select(Message.reply_to_message_id)
        .where(Message.source_id == int(source_id), Message.message_id == int(message_id))
        .order_by(Message.revision.desc(), Message.id.desc())
        .limit(1)
    )
    value = result.scalar_one_or_none()
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


async def root_message_id(session: AsyncSession, source_id: int, message_id: int) -> int:
    """Walk ``reply_to`` to the root telegram message id of this chain.

    A teaser is its own root. A ladder reply's root is the teaser, even when the teaser
    row itself has not been stored yet — the reply still names that telegram id.
    """
    current = int(message_id)
    seen: set[int] = set()
    for _ in range(_CHAIN_WALK_LIMIT):
        if current in seen:
            break
        seen.add(current)
        parent = await _reply_parent_id(session, source_id, current)
        if parent is None or parent == current:
            return current
        current = parent
    return current


async def family_message_ids(session: AsyncSession, source_id: int, root_id: int) -> set[int]:
    """Every telegram message id in the chain: the root and replies that hang off it."""
    ids = {int(root_id)}
    frontier = {int(root_id)}
    for _ in range(_CHAIN_WALK_LIMIT):
        result = await session.execute(
            select(Message.message_id).where(
                Message.source_id == int(source_id),
                Message.reply_to_message_id.in_(list(frontier)),
            )
        )
        found = {int(item) for item in result.scalars().all() if item}
        new = found - ids
        if not new:
            break
        ids |= new
        frontier = new
    return ids


async def chain_signal_refs(session: AsyncSession, signal_ref: str | None) -> list[str]:
    """Signal refs that belong to one teaser/reply chain, including ``signal_ref`` itself.

    An edit keeps ``SIG:<source>:<message id>``. A reply mints a new one, which is why a
    duplicate check on the reply alone cannot see the teaser's tickets. The family is the
    root post plus every reply under it.
    """
    if not signal_ref:
        return []
    ref = str(signal_ref).strip()
    if not ref:
        return []
    parsed = parse_signal_ref(ref)
    if parsed is None:
        return [ref]
    source_id, message_id = parsed
    root = await root_message_id(session, source_id, message_id)
    ids = await family_message_ids(session, source_id, root)
    refs = {f"SIG:{source_id}:{mid}" for mid in ids}
    refs.add(ref)
    return sorted(refs)


async def chain_run_key(message_row_id: int) -> str | None:
    """The lock key for one ingested post: ``<source>:<root telegram id>``, or None.

    None means "do not serialise" — the row is gone, or this run is not a channel post.
    The teaser and every reply in its chain share one key. An unrelated post does not.
    """
    async with session_scope() as session:
        row = await session.get(Message, int(message_row_id))
        if row is None:
            return None
        root = await root_message_id(session, int(row.source_id), int(row.message_id))
        return f"{int(row.source_id)}:{root}"


async def live_chain_trades(session: AsyncSession, signal_ref: str | None) -> list[Trade]:
    """Live tickets opened anywhere in this post's reply chain."""
    refs = await chain_signal_refs(session, signal_ref)
    if not refs:
        return []
    result = await session.execute(
        select(Trade)
        .where(Trade.signal_ref.in_(refs))
        .where(Trade.status.in_(LIVE_TRADE_STATUSES))
        .order_by(Trade.split_index, Trade.ticket)
    )
    return list(result.scalars().all())


async def check_duplicate_signal(s: AsyncSession, signal_ref: str | None) -> str | None:
    """Refuse a second order for a signal chain that already has a live ticket.

    ``signal_ref`` is ``None`` for anything not born from a channel post — a manual
    override, a watchdog run — and those are never duplicates of anything, so the guard
    passes immediately.

    Otherwise every ``open``/``pending`` trade carrying this ``signal_ref``, or any other
    ref in the same reply chain (the teaser and its ladder reply are different refs), is
    named in the refusal with its ticket, symbol, side and volume. The model is told what
    exists rather than being told "no": it can then modify that ticket. Legs whose stop
    is still 0 are called out so a ladder reply attaches levels instead of opening again.
    This is a correctness rule, not a risk limit — it is what stops a Telethon reconnect
    replay, an edited message, or a parallel reply from opening the same trade twice.

    The trade log being unreadable also refuses, because "I cannot tell whether this is a
    duplicate" must not become "so I opened it".
    """
    if not signal_ref:
        return None
    try:
        rows = await live_chain_trades(s, signal_ref)
    except Exception as exc:  # noqa: BLE001 - fail closed, never silently allow a double
        log.warning("duplicate_check_failed signal_ref=%s error=%s", signal_ref, exc)
        return (
            f"the duplicate-signal guard could not read the trade log for {signal_ref} "
            f"({exc}) — no order was sent"
        )
    if not rows:
        return None

    positions = [row for row in rows if row.kind == "position"]
    orders = [row for row in rows if row.kind != "position"]
    count = len(rows)
    if positions and orders:
        noun = f"{count} live positions and pending orders"
    elif orders:
        noun = "a pending order" if count == 1 else f"{count} pending orders"
    else:
        noun = "a live position" if count == 1 else f"{count} live positions"
    caller = str(signal_ref).strip()
    same_ref = all((row.signal_ref or "") == caller for row in rows)
    subject = "this signal" if same_ref else "this signal chain"
    message = (
        f"{subject} already has {noun}: {_describe_trades(rows)} — no duplicate was opened"
    )
    naked = [row for row in rows if float(row.sl or 0.0) <= 0.0]
    if naked and len(naked) == len(rows):
        message += (
            ". These tickets are incomplete (stop still 0). Attach the channel stop and "
            "the ladder with modify_position on each open ticket and modify_order on each "
            "pending ticket: the cover leg gets the channel stop and the 1R target, the "
            "runner gets the same stop and the runner target. Do not call open_market or "
            "place_pending again"
        )
    return message


# ======================================================================================
# 4. The operator's optional limits — every one of them ships off (BUILD_PROMPT §6)
# ======================================================================================


def _limit_number(value: Any, key: str) -> float | None:
    """Read a numeric limit, returning None for every spelling of 'not set'."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        log.warning("limit_ignored key=%s reason=not_a_number value=%r", key, value)
        return None
    if not math.isfinite(number):
        log.warning("limit_ignored key=%s reason=not_finite value=%r", key, value)
        return None
    if number <= 0.0:
        # The settings page validates limits as positive; a zero or negative value that
        # reached the database is nonsense, and "block every order" is what DISARM is for.
        log.warning("limit_ignored key=%s reason=not_positive value=%r", key, value)
        return None
    return number


def _symbol_variants(name: str) -> set[str]:
    """Return a symbol name upper-cased, plus the same name minus one broker suffix.

    ``XAUUSDm`` yields ``{"XAUUSD", "XAUUSDM"}`` so an allowlist written in plain broker-free
    names permits the suffixed symbol the account actually trades, and the reverse too.
    """
    text = str(name or "").strip().upper()
    if not text:
        return set()
    variants = {text}
    for suffix in BROKER_SUFFIXES:
        upper = str(suffix).strip().upper()
        if not upper or len(upper) >= len(text):
            continue
        if text.endswith(upper):
            variants.add(text[: -len(upper)])
    return variants


def _check_symbol_allowlist(raw: Any, symbol: str) -> str | None:
    """Refuse a symbol the operator's allowlist does not cover; an empty list allows all."""
    if not isinstance(raw, (list, tuple)):
        if raw:
            log.warning("limit_ignored key=limits.symbol_allowlist reason=not_a_list value=%r", raw)
        return None
    entries = [str(item).strip() for item in raw if str(item).strip()]
    if not entries:
        return None
    name = str(symbol or "").strip()
    if not name:
        return None
    wanted = _symbol_variants(name)
    for entry in entries:
        if wanted & _symbol_variants(entry):
            return None
    return (
        f"symbol_allowlist does not include {name} — the allowed symbols are "
        f"{', '.join(entries)}; change the allowlist in settings to trade this one"
    )


@dataclass(frozen=True)
class _Window:
    """One parsed `trading_hours` entry, resolved into its own timezone."""

    days: frozenset[int]
    start_minute: int
    end_minute: int
    tz_name: str
    tz: Any

    @property
    def full_day(self) -> bool:
        """Report whether this window covers its listed days entirely (`from` == `to`)."""
        return self.start_minute == self.end_minute

    @property
    def wraps(self) -> bool:
        """Report whether this window runs past midnight into the following day."""
        return self.end_minute < self.start_minute

    def contains(self, moment: datetime) -> bool:
        """Report whether an aware local datetime falls inside this window."""
        minute = moment.hour * 60 + moment.minute
        weekday = moment.weekday()
        if self.full_day:
            return weekday in self.days
        if not self.wraps:
            return weekday in self.days and self.start_minute <= minute < self.end_minute
        # A wrapping window belongs to the day it *starts* on, so the tail after midnight
        # is open only when the previous weekday is the listed one.
        previous = (weekday - 1) % 7
        if weekday in self.days and minute >= self.start_minute:
            return True
        return previous in self.days and minute < self.end_minute

    def next_start_after(self, now_ts: float) -> datetime | None:
        """Return the next local datetime this window opens at, or None if it never does."""
        try:
            local_now = datetime.fromtimestamp(now_ts, self.tz)
        except (OverflowError, OSError, ValueError):
            return None
        hour, minute = divmod(self.start_minute, 60)
        if hour > 23:
            return None
        for offset in range(_NEXT_WINDOW_HORIZON_DAYS):
            day = (local_now + timedelta(days=offset)).date()
            if day.weekday() not in self.days:
                continue
            try:
                candidate = datetime.combine(day, _time(hour=hour, minute=minute), tzinfo=self.tz)
                if candidate.timestamp() > now_ts:
                    return candidate
            except (OverflowError, OSError, ValueError):
                continue
        return None

    def label(self, moment: datetime) -> str:
        """Render a moment as `Mon 08:00 UTC`, in this window's own timezone."""
        return f"{DAY_NAMES[moment.weekday()]} {moment:%H:%M} {self.tz_name}"


def _parse_hhmm(value: Any) -> int | None:
    """Parse `HH:MM` (or `HH:MM:SS`) into minutes past midnight; `24:00` is 1440."""
    if value is None:
        return None
    parts = str(value).strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError):
        return None
    if hour == 24 and minute == 0:
        return 1440
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _parse_days(value: Any) -> frozenset[int] | None:
    """Parse a `days` list of 0-6 integers (Monday == 0); missing or empty means every day."""
    if value is None:
        return ALL_DAYS
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    days: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            return None
        try:
            day = int(item)
        except (TypeError, ValueError):
            return None
        if not 0 <= day <= 6:
            return None
        days.add(day)
    return frozenset(days) if days else ALL_DAYS


def _parse_windows(raw: Any) -> tuple[list[_Window], list[str]]:
    """Parse the `trading_hours` setting into windows, collecting a reason per bad entry."""
    if not isinstance(raw, (list, tuple)):
        if raw:
            return [], [f"trading_hours must be a list, got {type(raw).__name__}"]
        return [], []
    windows: list[_Window] = []
    problems: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            problems.append(f"entry {index} is not an object")
            continue
        days = _parse_days(entry.get("days"))
        if days is None:
            problems.append(f"entry {index} has an invalid 'days' list")
            continue
        start = _parse_hhmm(entry.get("from"))
        end = _parse_hhmm(entry.get("to"))
        if start is None or start >= 1440:
            problems.append(f"entry {index} has an invalid 'from' time")
            continue
        if end is None:
            problems.append(f"entry {index} has an invalid 'to' time")
            continue
        tz_name = str(entry.get("tz") or "UTC").strip() or "UTC"
        try:
            zone = ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001 - zoneinfo raises several unrelated types
            problems.append(f"entry {index} has an unknown timezone {tz_name!r} ({exc})")
            continue
        windows.append(
            _Window(days=days, start_minute=start, end_minute=end, tz_name=tz_name, tz=zone)
        )
    return windows, problems


def _check_trading_hours(raw: Any, now_ts: float) -> str | None:
    """Refuse outside every configured window, naming the next one; an empty list is always."""
    if raw in (None, "", [], ()):
        return None
    windows, problems = _parse_windows(raw)
    if not windows:
        detail = "; ".join(problems) if problems else "it is empty after parsing"
        return (
            f"trading_hours is set but no usable window could be read from it ({detail}) — "
            "no order was sent; fix or clear trading_hours in settings"
        )
    if problems:
        log.warning("trading_hours_partial problems=%s", "; ".join(problems))

    for window in windows:
        try:
            local_now = datetime.fromtimestamp(now_ts, window.tz)
        except (OverflowError, OSError, ValueError):
            continue
        if window.contains(local_now):
            return None

    best: tuple[float, _Window, datetime] | None = None
    for window in windows:
        candidate = window.next_start_after(now_ts)
        if candidate is None:
            continue
        stamp = candidate.timestamp()
        if best is None or stamp < best[0]:
            best = (stamp, window, candidate)

    # Render "now" in the timezone of the window that answers, so both halves of the
    # sentence are on one clock.
    reference = windows[0] if best is None else best[1]
    try:
        now_label = reference.label(datetime.fromtimestamp(now_ts, reference.tz))
    except (OverflowError, OSError, ValueError):
        now_label = "now"
    if best is None:
        return (
            f"trading_hours does not allow trading right now ({now_label}) and no upcoming "
            "window was found in the next week — check trading_hours in settings"
        )
    _, window, moment = best
    return (
        f"trading_hours does not allow trading right now ({now_label}) — the next window "
        f"opens {window.label(moment)}; no order was sent"
    )


async def _check_max_open_positions(s: AsyncSession, limit: int) -> str | None:
    """Refuse when this system's own open positions already fill the operator's cap."""
    magic = int(settings.MAGIC_NUMBER)
    try:
        result = await s.execute(
            select(Trade)
            .where(Trade.kind == "position")
            .where(Trade.status == "open")
            .where(Trade.magic == magic)
            .order_by(Trade.ticket)
        )
        rows = list(result.scalars().all())
    except Exception as exc:  # noqa: BLE001 - an explicit limit is not skipped on an error
        log.warning("max_open_positions_check_failed error=%s", exc)
        return (
            f"max_open_positions is set to {limit} but the open positions could not be "
            f"counted ({exc}) — no order was sent"
        )
    count = len(rows)
    if count < limit:
        return None
    verb = "is" if count == 1 else "are"
    return (
        f"max_open_positions is set to {limit} and {count} {verb} already open "
        f"({_describe_trades(rows)}) — close one or change the limit in settings"
    )


async def _check_max_daily_loss(
    s: AsyncSession, limit: float, rpc: Any, resolver: Any, now_ts: float
) -> str | None:
    """Refuse once today's realized loss, as a percent of balance, has reached the cap.

    "Today" is the broker-server day (BUILD_PROMPT §2), so the window starts at
    :func:`app.trading.symbols.broker_day_start_ts` and ``date_to`` is padded a day into the
    future — a naive datetime is read as UTC by the MT5 package while the server usually
    runs at UTC+2/+3, and without the padding the deals just made are silently missed.

    The total is account-wide, every closing deal, not only this system's magic: the
    operator set a cap on what the account may lose in a day, and a manual trade spends
    the same money.
    """
    if rpc is None:
        return (
            f"max_daily_loss_percent is set to {_fmt(limit)}% but MT5 is not connected, so "
            "today's realized P/L could not be read — no order was sent"
        )
    try:
        account = await rpc.account_info()
        balance = float(account.balance)
        currency = str(account.currency)
    except Exception as exc:  # noqa: BLE001 - fail closed on an explicitly requested limit
        log.warning("daily_loss_account_read_failed error=%s", exc)
        return (
            f"max_daily_loss_percent is set to {_fmt(limit)}% but the account balance could "
            f"not be read ({exc}) — no order was sent"
        )
    if not math.isfinite(balance) or balance <= 0.0:
        return (
            f"max_daily_loss_percent is set to {_fmt(limit)}% but the account balance reads "
            f"{_fmt(balance)} {currency}, so the limit cannot be evaluated — no order was sent"
        )

    try:
        day_start = float(await broker_day_start_ts(rpc, resolver))
        deals = await rpc.history_deals_get(day_start, now_ts + _HISTORY_FUTURE_PAD_SECONDS)
    except Exception as exc:  # noqa: BLE001 - fail closed on an explicitly requested limit
        log.warning("daily_loss_history_read_failed error=%s", exc)
        return (
            f"max_daily_loss_percent is set to {_fmt(limit)}% but today's realized P/L could "
            f"not be read from the broker ({exc}) — no order was sent"
        )

    realized = 0.0
    skipped = 0
    for deal in tuple(deals or ()):
        try:
            entry = int(deal.entry)
        except (AttributeError, TypeError, ValueError):
            skipped += 1
            continue
        if entry not in REALIZING_DEAL_ENTRIES:
            continue
        try:
            realized += float(deal.profit) + float(deal.swap) + float(deal.commission)
        except (AttributeError, TypeError, ValueError):
            skipped += 1
            continue
    if skipped:
        log.warning("daily_loss_deals_skipped count=%d", skipped)

    if realized >= 0.0:
        return None
    loss_percent = (-realized / balance) * 100.0
    if loss_percent < limit:
        return None
    return (
        f"max_daily_loss_percent is set to {_fmt(limit)}% and today's realized loss is "
        f"already {loss_percent:.2f}% of balance ({realized:.2f} {currency} of "
        f"{balance:.2f} {currency} since the broker day started) — no order was sent; "
        "change the limit in settings to keep trading today"
    )


async def check_optional_limits(
    s: AsyncSession,
    *,
    symbol: str,
    volume: float,
    rpc: Any,
    resolver: Any,
    now_ts: float,
) -> str | None:
    """Evaluate only the operator limits that are actually set, and refuse the first breach.

    Every limit ships null or empty and a null or empty limit is **not evaluated at all**:
    the settings read is the only work it costs. Order of evaluation is cheapest first, so
    a symbol that is not on the allowlist never spends an RPC round trip on history.

    Returns the refusal for the first limit that is breached, naming the limit, its value
    and the actual figure, so the model can report it in ``finish``.
    """
    try:
        raw: dict[str, Any] = {key: await get_setting(s, key, None) for key in LIMIT_KEYS}
    except Exception as exc:  # noqa: BLE001 - unreadable settings must not open the gate
        log.warning("optional_limits_read_failed error=%s", exc)
        return f"the operator limits could not be read ({exc}) — no order was sent"

    refusal = _check_symbol_allowlist(raw["limits.symbol_allowlist"], symbol)
    if refusal is not None:
        return refusal

    try:
        refusal = _check_trading_hours(raw["limits.trading_hours"], now_ts)
    except Exception as exc:  # noqa: BLE001 - a guard must never raise into the loop
        log.warning("trading_hours_check_failed error=%s", exc)
        return f"trading_hours could not be evaluated ({exc}) — no order was sent"
    if refusal is not None:
        return refusal

    volume_key = "limits.max_volume_per_trade"
    max_volume = _limit_number(raw[volume_key], volume_key)
    if max_volume is not None:
        try:
            requested = float(volume)
        except (TypeError, ValueError):
            requested = 0.0
        if requested > max_volume + 1e-9:
            return (
                f"max_volume_per_trade is set to {_fmt_volume(max_volume)} lots and this order "
                f"asks for {_fmt_volume(requested)} — reduce the volume or change the limit "
                "in settings"
            )

    positions_key = "limits.max_open_positions"
    max_positions = _limit_number(raw[positions_key], positions_key)
    if max_positions is not None:
        refusal = await _check_max_open_positions(s, int(max_positions))
        if refusal is not None:
            return refusal

    daily_loss_key = "limits.max_daily_loss_percent"
    max_daily_loss = _limit_number(raw[daily_loss_key], daily_loss_key)
    if max_daily_loss is not None:
        refusal = await _check_max_daily_loss(s, max_daily_loss, rpc, resolver, now_ts)
        if refusal is not None:
            return refusal

    return None


# ======================================================================================
# 5. The stopless-position watchdog (BUILD_PROMPT §6.4)
# ======================================================================================


@dataclass(frozen=True)
class _StoplessAction:
    """One position found without a stop, and the episode state that decides what to do."""

    ticket: int
    symbol: str
    side: str
    volume: float
    entry_price: float
    episode_id: int
    attempts: int
    announce: bool
    fallback_applied: bool


class SlWatchdog:
    """Finds open positions of ours that have lost their stop and gets one back on them.

    Positions lose their stops for real reasons — a partial close, a hand edit in the
    terminal, a broker action — and a naked position discovered at the next drawdown is
    not acceptable. Every ``SL_WATCHDOG_SECONDS`` this lists positions carrying
    ``MAGIC_NUMBER`` with ``sl == 0.0`` and, per ticket, keeps one unresolved
    :class:`~app.models.StoplessEpisode`:

    * on the first cycle it notifies the operator **once** for the episode and warns the
      dashboard;
    * on each cycle it increments ``attempts`` and starts an agent run whose only job is
      to attach a stop;
    * once ``attempts`` passes ``SL_FALLBACK_ATTEMPTS`` it stops asking and calls
      :meth:`~app.trading.executor.Executor.attach_fallback_sl`, which **bypasses the arm
      gate** — a naked position is a worse state than an unwanted stop order;
    * when the stop reappears or the position closes, the episode is resolved and the
      chasing stops.

    Nothing here raises: one failing ticket must not take down the other tickets, and one
    failing cycle must not take down the task.
    """

    #: Key this watchdog's task is registered under in ``Runtime.tasks``.
    TASK_NAME: Final[str] = "sl_watchdog"

    def __init__(self, runtime: Runtime) -> None:
        """Bind the watchdog to the shared runtime; no task starts until :meth:`start`."""
        self.runtime = runtime
        self.settings = runtime.settings
        self._task: asyncio.Task | None = None
        self._stopping: asyncio.Event = asyncio.Event()

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the watchdog loop; calling it twice is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name=self.TASK_NAME)
        self.runtime.tasks[self.TASK_NAME] = self._task
        log.info("sl_watchdog_started interval_s=%d", int(self.settings.SL_WATCHDOG_SECONDS))

    async def stop(self) -> None:
        """Signal the loop to end, cancel it, and wait for it to finish."""
        self._stopping.set()
        task = self._task
        self._task = None
        self.runtime.tasks.pop(self.TASK_NAME, None)
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - shutdown must never raise
            log.warning("sl_watchdog_stop_error error=%s", exc)
        log.info("sl_watchdog_stopped")

    async def _loop(self) -> None:
        """Run :meth:`tick` every ``SL_WATCHDOG_SECONDS`` until stopped."""
        interval = max(1.0, float(self.settings.SL_WATCHDOG_SECONDS))
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad cycle never kills the task
                log.warning("sl_watchdog_cycle_failed error=%s", exc)

    # ---- one cycle ---------------------------------------------------------

    async def tick(self) -> None:
        """Run exactly one watchdog cycle. Directly unit-testable and never raises."""
        try:
            actions = await self._collect()
        except Exception as exc:  # noqa: BLE001 - a scan failure is logged, not raised
            log.warning("sl_watchdog_scan_failed error=%s", exc)
            return
        for action in actions:
            try:
                await self._act(action)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one ticket never blocks the rest
                log.warning(
                    "sl_watchdog_action_failed ticket=%s error=%s", action.ticket, exc
                )
        executor = getattr(self.runtime, "executor", None)
        if executor is None:
            return
        try:
            from app.trading.reconcile import monitor_closes

            await monitor_closes(self.runtime)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - BE trail must not take down the watchdog
            log.warning("ladder_be_trail_failed error=%s", exc)

    async def _collect(self) -> list[_StoplessAction]:
        """List our stopless positions, resolve stale episodes, and count one attempt each.

        The database session is closed before any agent run or fallback happens: those take
        seconds to minutes and hold their own sessions, and SQLite must not be left inside a
        write transaction across them.
        """
        rpc = self.runtime.rpc
        if rpc is None:
            return []
        positions = await rpc.positions_get()
        magic = int(self.settings.MAGIC_NUMBER)

        stopless: dict[int, dict[str, Any]] = {}
        for position in tuple(positions or ()):
            try:
                if int(position.magic) != magic:
                    continue  # someone else's position: read-only, never touched (§8)
                ticket = int(position.ticket)
                stop = float(position.sl)
                if stop != 0.0:
                    continue
                stopless[ticket] = {
                    "symbol": str(position.symbol),
                    "side": POSITION_TYPE_NAMES.get(int(position.type), str(position.type)),
                    "volume": float(position.volume),
                    "entry_price": float(position.price_open),
                }
            except (AttributeError, TypeError, ValueError) as exc:
                log.warning("sl_watchdog_bad_position error=%s", exc)
                continue

        actions: list[_StoplessAction] = []
        async with session_scope() as session:
            result = await session.execute(
                select(StoplessEpisode)
                .where(StoplessEpisode.resolved_at.is_(None))
                .order_by(StoplessEpisode.id)
            )
            open_episodes = list(result.scalars().all())

            live: dict[int, StoplessEpisode] = {}
            for episode in open_episodes:
                ticket = int(episode.ticket)
                if ticket not in stopless:
                    # The stop is back, or the position is gone. Either way it is over.
                    episode.resolved_at = utcnow()
                    log.info("sl_episode_resolved ticket=%d attempts=%d", ticket, episode.attempts)
                    continue
                previous = live.get(ticket)
                if previous is None:
                    live[ticket] = episode
                else:
                    # Two unresolved episodes for one ticket cannot both be current; keep
                    # the newest so `attempts` stays a single honest counter.
                    older = previous if previous.id < episode.id else episode
                    newer = episode if previous.id < episode.id else previous
                    older.resolved_at = utcnow()
                    live[ticket] = newer

            for ticket in sorted(stopless):
                info = stopless[ticket]
                episode = live.get(ticket)
                if episode is None:
                    trade_id = await self._trade_id_for(session, ticket)
                    episode = StoplessEpisode(
                        ticket=ticket,
                        trade_id=trade_id,
                        started_at=utcnow(),
                        attempts=0,
                        notified=False,
                        fallback_applied=False,
                        resolved_at=None,
                    )
                    session.add(episode)
                    log.info("sl_episode_opened ticket=%d", ticket)
                was_applied = bool(episode.fallback_applied)
                # `notified` is the once-per-episode latch, not `created`: a restart between
                # the INSERT and the notification would otherwise lose the one message the
                # operator gets about a naked position.
                announce = not bool(episode.notified)
                episode.attempts = int(episode.attempts) + 1
                episode.notified = True
                await session.flush()
                actions.append(
                    _StoplessAction(
                        ticket=ticket,
                        symbol=info["symbol"],
                        side=info["side"],
                        volume=info["volume"],
                        entry_price=info["entry_price"],
                        episode_id=int(episode.id),
                        attempts=int(episode.attempts),
                        announce=announce,
                        fallback_applied=was_applied,
                    )
                )
        return actions

    @staticmethod
    async def _trade_id_for(session: AsyncSession, ticket: int) -> int | None:
        """Return the Trade row id for a ticket, or None when we never recorded it."""
        result = await session.execute(select(Trade.id).where(Trade.ticket == ticket))
        return result.scalars().first()

    async def _act(self, action: _StoplessAction) -> None:
        """Notify on a new episode, then either ask the AI for a stop or attach one."""
        if action.announce:
            self._announce(action)
        if action.attempts > int(self.settings.SL_FALLBACK_ATTEMPTS):
            await self._apply_fallback(action)
            return
        await self._ask_agent(action)

    # ---- the two responses -------------------------------------------------

    def _announce(self, action: _StoplessAction) -> None:
        """Warn the dashboard and notify the operator exactly once per episode."""
        text = (
            f"Position #{action.ticket} {action.symbol} {action.side} {_fmt_volume(action.volume)} "
            f"opened at {_fmt(action.entry_price)} has NO stop loss. The AI is being asked to "
            f"attach one; after {int(self.settings.SL_FALLBACK_ATTEMPTS)} attempts a fallback "
            f"stop {int(self.settings.SL_FALLBACK_POINTS)} points from entry is applied "
            "automatically, armed or not."
        )
        self._emit(
            reason="stopless_position",
            ticket=action.ticket,
            symbol=action.symbol,
            side=action.side,
            volume=action.volume,
            episode_id=action.episode_id,
            message=text,
        )
        self._notify(text)
        log.warning("stopless_position ticket=%d symbol=%s", action.ticket, action.symbol)

    async def _ask_agent(self, action: _StoplessAction) -> None:
        """Run the agent with one job: put a stop on this ticket.

        Imported here rather than at module scope because ``app.ai.loop`` imports the
        executor, which imports this module — a real cycle, and this is the one call that
        closes it.

        The run is awaited so two cycles can never chase the same ticket at once and so
        ``attempts`` counts finished attempts rather than started ones. This run goes
        through the normal arm gate: disarmed, ``modify_position`` refuses and the three
        attempts burn down to the fallback, which is exactly the designed behaviour.
        """
        from app.ai.loop import run_agent

        instruction = (
            f"Position #{action.ticket} on {action.symbol} ({action.side} "
            f"{_fmt_volume(action.volume)} lots, opened at {_fmt(action.entry_price)}) currently "
            "has NO stop loss, which breaks the one unconditional trading rule. Your only job in "
            "this run is to give it one. Read the current price, choose a protective level on "
            "the correct side of the entry (below for a buy, above for a sell) that respects "
            f"the symbol's minimum stop distance, and call modify_position(ticket={action.ticket}, "
            "sl=<price>). Do not open, close, or modify anything else. Then call finish. "
            f"This is attempt {action.attempts} of "
            f"{int(self.settings.SL_FALLBACK_ATTEMPTS)} before a fallback stop "
            f"{int(self.settings.SL_FALLBACK_POINTS)} points from entry is attached automatically."
        )
        log.info(
            "sl_watchdog_agent_run ticket=%d attempt=%d", action.ticket, action.attempts
        )
        await run_agent(
            self.runtime,
            message_row_id=None,
            kind="sl_watchdog",
            instruction=instruction,
        )

    async def _apply_fallback(self, action: _StoplessAction) -> None:
        """Attach the fallback stop ourselves, bypassing the arm gate, and say that we did."""
        executor = self.runtime.executor
        if executor is None:
            log.error("sl_watchdog_no_executor ticket=%d", action.ticket)
            return
        result = await executor.attach_fallback_sl(action.ticket)
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        message = str(result.get("message", "")) if isinstance(result, dict) else str(result)

        if ok:
            first_time = not action.fallback_applied
            if first_time:
                async with session_scope() as session:
                    episode = await session.get(StoplessEpisode, action.episode_id)
                    if episode is not None:
                        episode.fallback_applied = True
            text = (
                f"Fallback stop attached to #{action.ticket} {action.symbol} "
                f"{action.side} {_fmt_volume(action.volume)} after {action.attempts} attempts: "
                f"{message}"
            )
            self._emit(
                reason="sl_fallback_applied",
                ticket=action.ticket,
                symbol=action.symbol,
                episode_id=action.episode_id,
                attempts=action.attempts,
                message=text,
            )
            if first_time:
                self._notify(text)
            log.warning("sl_fallback_applied ticket=%d message=%s", action.ticket, message)
            return

        text = (
            f"The fallback stop for #{action.ticket} {action.symbol} could not be attached: "
            f"{message}. The position is still without a stop."
        )
        self._emit(
            reason="sl_fallback_failed",
            ticket=action.ticket,
            symbol=action.symbol,
            episode_id=action.episode_id,
            attempts=action.attempts,
            message=text,
        )
        # Notify on the first failing cycle only: this repeats every SL_WATCHDOG_SECONDS
        # until the stop is on, and the operator does not need a message a minute.
        if action.attempts == int(self.settings.SL_FALLBACK_ATTEMPTS) + 1:
            self._notify(text)
        log.error("sl_fallback_failed ticket=%d message=%s", action.ticket, message)

    # ---- best-effort side channels ----------------------------------------

    def _emit(self, **data: Any) -> None:
        """Push a system warning onto the event bus; never raises."""
        try:
            self.runtime.bus.emit(events.SYSTEM_WARNING, **data)
        except Exception as exc:  # noqa: BLE001 - the dashboard is not worth a crash
            log.warning("sl_watchdog_emit_failed error=%s", exc)

    def _notify(self, text: str) -> None:
        """Fire an "attention" notification at the operator's bot; never raises, never awaits."""
        notifier = self.runtime.notifier
        if notifier is None:
            return
        try:
            notifier.fire("attention", text)
        except Exception as exc:  # noqa: BLE001 - a notification never blocks a stop
            log.warning("sl_watchdog_notify_failed error=%s", exc)
