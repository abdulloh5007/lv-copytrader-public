"""Distance conversion, volume sizing, stop validation, filling and pending-type inference.

This is the arithmetic the model must never do (BUILD_PROMPT §2). It names price levels and
relative distances; every multiplication, every division by a pip, every rounding and every
lot calculation happens here.

Every function in this module is **pure**: no I/O, no ``await``, no global state, no clock.
It takes a :class:`~app.trading.symbols.SymbolRef` (or, where the signature says ``si``, a
raw ``SimpleNamespace`` straight off ``symbol_info``) and returns numbers or plain dicts.
That is deliberate — this is the file whose mistakes cost money, so it is the file that must
be unit-testable without a broker, a database or an event loop.

Two rules run through the whole module:

* **Every distance the model sees is reported in all three units** — price, points and pips —
  by :func:`distance_units`, so the model never has to pick a unit or convert one.
* **Rounding always moves away from the market.** Rounding a buy's stop *up* moves it toward
  price and can breach ``trade_stops_level`` after validation already passed, which the broker
  reports as a bare ``10016``. :func:`round_stop_away` is how that is avoided.

``SymbolRef`` is imported only for typing: at runtime this module reads attributes off whatever
it is handed, so it works with both a ``SymbolRef`` and a raw ``symbol_info`` namespace, and it
never participates in an import cycle with ``app.trading.symbols``.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from app.errors import TradingError
from app.trading.constants import (
    ORDER_FILLING_BOC,
    ORDER_FILLING_FOK,
    ORDER_FILLING_IOC,
    ORDER_FILLING_RETURN,
    ORDER_TYPE_BUY_LIMIT,
    ORDER_TYPE_BUY_STOP,
    ORDER_TYPE_SELL_LIMIT,
    ORDER_TYPE_SELL_STOP,
    SYMBOL_FILLING_BOC,
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_EXECUTION_MARKET,
    TRADE_ACTION_PENDING,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from app.trading.symbols import SymbolRef

__all__ = [
    "points",
    "pips",
    "distance_units",
    "round_price",
    "round_stop_away",
    "price_from_distance",
    "VolumeResult",
    "compute_volume",
    "normalize_volume",
    "split_volume",
    "LADDER_MIN_TARGETS",
    "LONG_GAP_MULTIPLE",
    "LadderRunnerPick",
    "LadderTargets",
    "is_tp_ladder",
    "one_r_take_profit",
    "pick_ladder_runner",
    "pick_ladder_runner_tp",
    "plan_ladder_targets",
    "split_ladder_rungs",
    "suggest_runner_tp_index",
    "breakeven_stop",
    "stop_is_at_or_beyond_breakeven",
    "validate_stops",
    "pick_filling",
    "infer_pending_type",
]

# --------------------------------------------------------------------------------------
# Tolerances.
#
# Float division by a point size (1e-5) produces counts in the 1e5..1e7 range, where the
# last representable bit is around 1e-9. _GRID_EPS absorbs that when snapping a value onto
# the point or the volume-step grid, so 1.08500 / 0.00001 == 108499.99999999999 does not
# floor to one point below where the caller asked. It is six orders of magnitude smaller
# than one grid cell, so it can never move a value a whole step.
# --------------------------------------------------------------------------------------
_GRID_EPS: Final[float] = 1e-6
_VOLUME_EPS: Final[float] = 1e-9
_POINTS_EPS: Final[float] = 1e-6

_SIDES: Final[frozenset[str]] = frozenset({"buy", "sell"})
_KINDS: Final[frozenset[str]] = frozenset({"sl", "tp"})
_DIRECTIONS: Final[frozenset[str]] = frozenset({"above", "below"})
_UNITS: Final[frozenset[str]] = frozenset({"price", "points", "pips", "percent"})
_ORDER_TYPES: Final[frozenset[str]] = frozenset({"limit", "stop"})


# --------------------------------------------------------------------------------------
# Small formatting helpers. They exist so every message in this module quotes a number the
# operator would recognise, instead of 0.30000000000000004.
# --------------------------------------------------------------------------------------
def _name_of(ref: Any) -> str:
    """Return the symbol name for a message, tolerating a namespace without one."""
    return str(getattr(ref, "name", "") or "the symbol")


def _trim(value: float, decimals: int = 8) -> str:
    """Format a float without exponent notation and without trailing zeros."""
    text = f"{float(value):.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _fmt_lots(value: float) -> str:
    """Format a lot size: at least two decimals, so a zero volume reads as ``0.00``."""
    text = f"{float(value):.8f}".rstrip("0")
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def _fmt_price(value: float, digits: int) -> str:
    """Format a price at the symbol's own precision."""
    try:
        d = int(digits)
    except (TypeError, ValueError):
        d = 5
    if d < 0:
        d = 0
    if d > 12:
        d = 12
    return f"{float(value):.{d}f}"


def _side_of(side: str) -> str:
    """Normalise and validate a side string, raising a TradingError on anything else."""
    s = str(side or "").strip().lower()
    if s not in _SIDES:
        raise TradingError(
            f"unknown side {side!r} — use 'buy' or 'sell'",
            detail={"side": side, "allowed": sorted(_SIDES)},
        )
    return s


def _point_of(ref: Any) -> float:
    """Return the symbol's point size, refusing to divide by a broken one."""
    point = float(getattr(ref, "point", 0.0) or 0.0)
    if point <= 0.0:
        raise TradingError(
            f"cannot convert a distance for {_name_of(ref)}: its point size is {_trim(point)}",
            detail={"symbol": _name_of(ref), "point": point},
        )
    return point


def _pip_of(ref: Any) -> float:
    """Return the symbol's pip size, refusing to divide by a broken one.

    ``pip_size`` exists on a :class:`SymbolRef`, never on a raw ``symbol_info`` namespace —
    "pip" is trader slang with no broker-side definition, so it is decided once in
    ``symbols.pip_size()`` and carried on the ref. A raw namespace here raises
    ``AttributeError``, which is the intended §1.1 behaviour: a silent ``None`` in the
    money path is worse than a traceback.
    """
    pip = float(ref.pip_size or 0.0)
    if pip <= 0.0:
        name = _name_of(ref)
        digits = getattr(ref, "digits", None)
        point = getattr(ref, "point", None)
        raise TradingError(
            f"pip-denominated distances are unavailable for {name}: this broker quotes it to "
            f"{digits} decimals, which matches no standard pip convention. Use an absolute price "
            f"or the 'points' unit (1 point = {point}), or have the operator set "
            f"PIP_OVERRIDES['{str(name).upper()}'] to this symbol's pip size.",
            detail={"symbol": name, "pip_size": pip, "digits": digits, "point": point},
        )
    return pip


def _digits_of(ref: Any) -> int:
    """Return the symbol's price precision as an int."""
    return int(getattr(ref, "digits", 5) or 0)


# --------------------------------------------------------------------------------------
# Distances.
# --------------------------------------------------------------------------------------
def points(si_or_ref: Any, price_distance: float) -> float:
    """Convert a price distance into broker points (``distance / point``).

    The sign is preserved: a negative distance means "below", which is what
    ``evaluate_entry`` needs when it reports how far price has run from the signal entry.
    """
    return float(price_distance) / _point_of(si_or_ref)


def pips(ref: "SymbolRef", price_distance: float) -> float:
    """Convert a price distance into pips using the ref's audited pip convention."""
    return float(price_distance) / _pip_of(ref)


def distance_units(ref: "SymbolRef", price_distance: float) -> dict:
    """Return one distance in all three units at once.

    ``{"price": 0.002, "points": 200.0, "pips": 20.0}`` — every distance the model is shown
    goes through this function, so it never has to know what a pip is on this symbol or do
    the conversion itself (§2). The sign is preserved in all three.
    """
    d = float(price_distance)
    row: dict = {
        "price": round(d, _digits_of(ref) + 2),
        "points": round(points(ref, d), 1),
    }
    # A symbol whose digit count matches no standard pip convention still has a price and a
    # point; only the pip is undefined. Reporting `pips: null` beside the two real units keeps
    # every other distance readable, where raising here would make a points-denominated call
    # fail for want of a unit it never asked for.
    try:
        row["pips"] = round(pips(ref, d), 2)
    except TradingError:
        row["pips"] = None
    return row


def round_price(ref: "SymbolRef", price: float) -> float:
    """Round a price to the symbol's own number of digits."""
    return round(float(price), _digits_of(ref))


def round_stop_away(ref: "SymbolRef", price: float, side: str, kind: str) -> float:
    """Round an SL or TP onto the point grid **away** from the market.

    ``kind`` is ``"sl"`` or ``"tp"``. For a buy the stop sits below and the target above, so
    the stop floors and the target ceils; for a sell it is the mirror image. Rounding the
    other way moves the level toward price and can breach ``trade_stops_level`` *after*
    :func:`validate_stops` already passed — which the broker returns as a bare ``10016``
    with nothing the model can act on (§2).
    """
    s = _side_of(side)
    k = str(kind or "").strip().lower()
    if k not in _KINDS:
        raise TradingError(
            f"unknown stop kind {kind!r} — use 'sl' or 'tp'",
            detail={"kind": kind, "allowed": sorted(_KINDS)},
        )
    point = _point_of(ref)
    steps = float(price) / point
    # A buy's stop and a sell's target both sit below the market, so both round down.
    downward = (s == "buy" and k == "sl") or (s == "sell" and k == "tp")
    if downward:
        grid = math.floor(steps + _GRID_EPS)
    else:
        grid = math.ceil(steps - _GRID_EPS)
    return round_price(ref, grid * point)


def price_from_distance(
    ref: "SymbolRef",
    anchor: float,
    direction: str,
    distance: float,
    unit: str,
) -> dict:
    """Turn "20 pips below entry" into an absolute price, server-side.

    ``direction`` is ``"above"`` or ``"below"``; ``unit`` is one of ``"price"``, ``"points"``,
    ``"pips"`` or ``"percent"``. The returned ``distance`` is the resulting price delta in all
    three units and is **signed** — negative when the direction was ``"below"`` — so the
    result always satisfies ``price == anchor + distance["price"]`` and the model never has
    to reconstruct the direction.
    """
    u = str(unit or "").strip().lower()
    if u not in _UNITS:
        raise TradingError(
            f"unknown unit {unit!r} — use one of: {', '.join(sorted(_UNITS))}",
            detail={"unit": unit, "allowed": sorted(_UNITS)},
        )
    d = str(direction or "").strip().lower()
    if d not in _DIRECTIONS:
        raise TradingError(
            f"unknown direction {direction!r} — use 'above' or 'below'",
            detail={"direction": direction, "allowed": sorted(_DIRECTIONS)},
        )
    dist = float(distance)
    if dist < 0.0:
        raise TradingError(
            f"distance must not be negative ({_trim(dist)}) — say which side with "
            f"direction='above' or direction='below' instead",
            detail={"distance": dist, "direction": d, "unit": u},
        )
    anchor_value = float(anchor)

    if u == "price":
        delta = dist
    elif u == "points":
        delta = dist * _point_of(ref)
    elif u == "pips":
        delta = dist * _pip_of(ref)
    else:  # percent — of the anchor, which is what "1% below entry" means
        delta = anchor_value * dist / 100.0

    signed = delta if d == "above" else -delta
    rounded_anchor = round_price(ref, anchor_value)
    price = round_price(ref, rounded_anchor + signed)
    return {
        "price": price,
        "convention": str(getattr(ref, "pip_convention", "") or "unknown"),
        "anchor": rounded_anchor,
        "distance": distance_units(ref, price - rounded_anchor),
    }


# --------------------------------------------------------------------------------------
# Volume.
# --------------------------------------------------------------------------------------
@dataclass
class VolumeResult:
    """The outcome of sizing a trade from risk. ``ok=False`` is a message for the model."""

    ok: bool
    volume: float
    loss_per_lot: float
    risk_amount: float
    risk_percent: float
    clamped: bool
    message: str


def compute_volume(
    ref: "SymbolRef",
    balance: float,
    risk_percent: float,
    entry: float,
    sl: float,
    currency: str,
) -> VolumeResult:
    """Size a trade from account risk — BUILD_PROMPT §2, in that exact order.

    Rounds **down** to ``volume_step``, always. Clamping *up* to ``volume_min`` would
    silently multiply the risk the model asked for, so a volume below the minimum is an
    error the model must resolve — the message quotes what the minimum lot *would* risk in
    account currency and as a percent of balance, which is the number it needs to decide.
    Clamping *down* to ``volume_max`` is safe, so it happens, and it is stated in the result.

    ``risk_percent`` has no upper bound: the owner chose full autonomy and a cap here would
    be a risk veto (§6).
    """
    name = _name_of(ref)
    balance_value = float(balance)
    risk = float(risk_percent)
    entry_value = float(entry)
    sl_value = float(sl)

    # The losing side is what a stop actually costs; trade_tick_value_loss is 0.0 on some
    # symbols, hence the fallback to the symmetric trade_tick_value.
    tick_value = ref.trade_tick_value_loss or ref.trade_tick_value
    tick_size = ref.trade_tick_size
    volume_step = ref.volume_step
    volume_min = float(ref.volume_min)
    volume_max = float(ref.volume_max)
    # Snap the stop distance to the broker's price grid before it becomes money.
    # Both entry and sl are already rounded to `digits`, but their float difference is
    # not: abs(1.085 - 1.083) is 0.0020000000000000018, which drags a nominally 30.00 USD
    # risk to 30.000000000000025 -- above the number the operator was promised, for no
    # reason but binary representation. Rounding to `digits` restores the intended value.
    sl_distance = round(abs(entry_value - sl_value), int(ref.digits))

    for field, value in (
        ("trade_tick_size", tick_size),
        ("tick_value", tick_value),
        ("volume_step", volume_step),
        ("sl_distance", sl_distance),
    ):
        if not value:
            return VolumeResult(
                ok=False,
                volume=0.0,
                loss_per_lot=0.0,
                risk_amount=0.0,
                risk_percent=risk,
                clamped=False,
                message=f"cannot size the trade: {field} is {value} for {name}",
            )

    if balance_value <= 0.0:
        return VolumeResult(
            ok=False,
            volume=0.0,
            loss_per_lot=0.0,
            risk_amount=0.0,
            risk_percent=risk,
            clamped=False,
            message=(
                f"cannot size the trade: the account balance is {balance_value:.2f} "
                f"{currency} — there is nothing to risk"
            ),
        )
    if risk <= 0.0:
        return VolumeResult(
            ok=False,
            volume=0.0,
            loss_per_lot=0.0,
            risk_amount=0.0,
            risk_percent=risk,
            clamped=False,
            message=(
                f"cannot size the trade: risk_percent is {_trim(risk)} — it must be greater "
                f"than zero (pass an explicit volume instead if that is what you meant)"
            ),
        )

    loss_per_lot = (sl_distance / float(tick_size)) * float(tick_value)
    raw = (balance_value * risk / 100.0) / loss_per_lot
    volume = round(math.floor(raw / float(volume_step) + _VOLUME_EPS) * float(volume_step), 8)

    stop_units = distance_units(ref, sl_distance)
    clamped = False

    if volume < volume_min:
        min_risk = volume_min * loss_per_lot
        return VolumeResult(
            ok=False,
            volume=volume,
            loss_per_lot=loss_per_lot,
            risk_amount=volume * loss_per_lot,
            risk_percent=risk,
            clamped=False,
            message=(
                f"{_trim(risk)}% risk gives {_fmt_lots(volume)} lots, below the "
                f"{_fmt_lots(volume_min)} minimum; the minimum lot would risk "
                f"{min_risk:.2f} {currency} "
                f"({min_risk / balance_value * 100.0:.2f}%)"
            ),
        )

    wanted = volume
    if volume > volume_max:
        volume = round(volume_max, 8)
        clamped = True

    risk_amount = volume * loss_per_lot
    actual_percent = risk_amount / balance_value * 100.0

    if clamped:
        message = (
            f"{_trim(risk)}% risk would need {_fmt_lots(wanted)} lots; clamped down to the "
            f"{_fmt_lots(volume_max)} maximum for {name}, so the trade risks {risk_amount:.2f} "
            f"{currency} ({actual_percent:.2f}%) instead of the {_trim(risk)}% asked for"
        )
    else:
        message = (
            f"{_fmt_lots(volume)} lots on {name}: a stop {_trim(stop_units['points'])} points "
            f"({_trim(stop_units['pips'])} pips) away risks {risk_amount:.2f} {currency}, "
            f"{actual_percent:.2f}% of the {balance_value:.2f} {currency} balance "
            f"(asked for {_trim(risk)}%, rounded down to the {_trim(volume_step)} lot step)"
        )

    return VolumeResult(
        ok=True,
        volume=volume,
        loss_per_lot=loss_per_lot,
        risk_amount=risk_amount,
        risk_percent=risk,
        clamped=clamped,
        message=message,
    )


def normalize_volume(ref: "SymbolRef", volume: float) -> float:
    """Floor a volume onto the ``volume_step`` grid and clamp it into [min, max].

    This is the *shaping* helper — it makes a volume the broker will accept instead of a
    ``10014``. It is not sizing: :func:`compute_volume` is the one that refuses to round a
    risk-derived lot up to the minimum.
    """
    step = float(ref.volume_step or 0.0)
    volume_min = float(ref.volume_min)
    volume_max = float(ref.volume_max)
    value = float(volume)

    if step > 0.0:
        value = math.floor(value / step + _VOLUME_EPS) * step
    if value > volume_max:
        value = math.floor(volume_max / step + _VOLUME_EPS) * step if step > 0.0 else volume_max
    if value < volume_min:
        value = volume_min
    return round(value, 8)


def split_volume(ref: "SymbolRef", total: float, parts: int) -> list[float]:
    """Split a volume into ``parts`` pieces on the ``volume_step`` grid, largest remainder.

    The pieces sum **exactly** to ``normalize_volume(ref, total)`` and every piece is at
    least ``volume_min`` — that is what multi-TP splitting on a hedging account needs (§5.3).
    When the total cannot fund ``parts`` pieces, the longest fundable prefix is returned
    (``floor(total / volume_min)`` pieces) and the caller reports the reduced split count to
    the model. ``parts <= 0`` returns ``[]``.
    """
    count = int(parts)
    if count <= 0:
        return []

    step = float(ref.volume_step or 0.0)
    if step <= 0.0:
        raise TradingError(
            f"cannot split a volume on {_name_of(ref)}: its volume_step is {_trim(step)}",
            detail={"symbol": _name_of(ref), "volume_step": step},
        )

    total_volume = normalize_volume(ref, total)
    # Work in whole steps so the parts sum exactly instead of accumulating float error.
    units_total = int(math.floor(total_volume / step + _VOLUME_EPS))
    units_min = max(1, int(math.ceil(float(ref.volume_min) / step - _VOLUME_EPS)))
    if units_total < units_min:
        return []

    if units_total < count * units_min:
        count = units_total // units_min
        if count <= 0:
            return []

    base, remainder = divmod(units_total, count)
    return [round((base + (1 if i < remainder else 0)) * step, 8) for i in range(count)]


# --------------------------------------------------------------------------------------
# Multi-TP ladders (4+ channel targets): two broker legs, not one ticket per rung.
# --------------------------------------------------------------------------------------

#: A channel post with this many take-profits is a ladder, not a classic TP1/TP2/TP3 split.
LADDER_MIN_TARGETS: Final[int] = 4

#: Last print is LONG (excluded from N) when its gap is at least this multiple of the
#: median inner gap. Deterministic; no randomness.
LONG_GAP_MULTIPLE: Final[float] = 2.0


@dataclass(frozen=True)
class LadderRunnerPick:
    """The numbered-rung runner target for a channel ladder.

    ``N`` is the count of numbered TPs **excluding LONG**. LONG is a fallback ceiling only.
    ``index`` is 1-based into ``rungs``.
    """

    rungs: tuple[float, ...]
    long_tp: float | None
    n: int
    base: int
    bias: int
    suggested_index: int
    chosen_index: int
    runner_tp: float
    used_long_fallback: bool = False

    def blurb_price(self, digits: int = 2) -> str:
        """Compact price for the operator/AI one-liner."""
        return _fmt_price(self.runner_tp, digits).rstrip("0").rstrip(".")


@dataclass(frozen=True)
class LadderTargets:
    """The two prices actually sent to the broker for a ladder, plus leftover rungs."""

    cover_tp: float
    runner_tp: float
    unmanaged: tuple[float, ...]
    notes: tuple[str, ...]
    n: int = 0
    bias: int = 0
    base: int = 0
    suggested_index: int | None = None
    chosen_index: int | None = None
    long_tp: float | None = None


def is_tp_ladder(targets: Any) -> bool:
    """True when the signal named enough take-profits to be treated as a ladder."""
    if not isinstance(targets, (list, tuple)):
        return False
    count = 0
    for item in targets:
        try:
            if float(item) > 0.0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count >= LADDER_MIN_TARGETS


def one_r_take_profit(ref: "SymbolRef", side: str, entry: float, sl: float) -> float:
    """Take-profit at the same price distance as the stop — money R:R ≈ 1:1 from ``entry``.

    This is **not** channel TP1. A buy at 4345 with SL 4335 (risk $10/unit) takes profit at
    4355, even if the channel's first celebration rung is 4348.
    """
    s = _side_of(side)
    digits = _digits_of(ref)
    distance = round(abs(float(entry) - float(sl)), digits)
    raw = float(entry) + distance if s == "buy" else float(entry) - distance
    return round_stop_away(ref, raw, s, "tp")


def _positive_levels(targets: Any) -> list[float]:
    """Unique positive prices, first-seen order (nearest first as the channel listed them)."""
    levels: list[float] = []
    seen: set[float] = set()
    if not isinstance(targets, (list, tuple)):
        return levels
    for item in targets:
        try:
            value = float(item)
        except (TypeError, ValueError):
            continue
        if value <= 0.0 or value in seen:
            continue
        seen.add(value)
        levels.append(value)
    return levels


def split_ladder_rungs(targets: Any) -> tuple[list[float], float | None]:
    """Split numbered channel TPs from an optional LONG ceiling.

    ``N`` is ``len(rungs)`` — LONG is **not** counted. The last print is LONG only when
    there are at least two inner gaps and the last gap is ≥ ``LONG_GAP_MULTIPLE`` times
    the median of the inner gaps (the live XAUUSD shape: equal 3-point rungs then a 10-point
    LONG). Equal-spaced lists have no LONG. Deterministic; no randomness.
    """
    levels = _positive_levels(targets)
    if len(levels) < 3:
        return levels, None
    gaps = [abs(levels[i] - levels[i - 1]) for i in range(1, len(levels))]
    inner = gaps[:-1]
    if not inner:
        return levels, None
    typical = float(statistics.median(inner))
    last_gap = float(gaps[-1])
    if typical > 0.0 and last_gap + 1e-12 >= typical * LONG_GAP_MULTIPLE:
        return levels[:-1], levels[-1]
    return levels, None


def suggest_runner_tp_index(n: int, bias: int = 0) -> int:
    """1-based runner index into the numbered TP list (LONG excluded from ``n``).

    ::

        base = ceil((N + 1) / 2)
        target_index = min(base + bias, cap)

    * ``N = 1`` → that sole TP.
    * ``N = 2`` → runner may use TP2 (cap = N). Documented exception for two-rung lists.
    * ``N >= 3`` → never the last numbered rung **by default** (cap = N-1).
    * Floor at 1. Bias is applied before the cap. No randomness.

    Examples: N=7 base 4, bias 0/1/2 → TP4/5/6 (not 7). N=4 base 3, cap 3 → TP3.
    N=5 base 3, bias 0/1/2 → TP3/4/4 (not 5).
    """
    size = int(n)
    if size <= 1:
        return 1 if size == 1 else 0
    base = int(math.ceil((size + 1) / 2.0))
    cap = size if size == 2 else size - 1
    return max(1, min(base + int(bias), cap))


def pick_ladder_runner(
    targets: Any,
    *,
    bias: int = 0,
    index: int | None = None,
) -> LadderRunnerPick | None:
    """Pick the runner rung from numbered TPs using :func:`suggest_runner_tp_index`.

    ``index`` is a 1-based override (the executor allows suggested ±1). LONG is used only
    when no numbered rung exists — a fallback ceiling, never the default target.
    """
    rungs, long_tp = split_ladder_rungs(targets)
    if not rungs:
        if long_tp is None:
            return None
        return LadderRunnerPick(
            rungs=(),
            long_tp=long_tp,
            n=0,
            base=0,
            bias=int(bias),
            suggested_index=0,
            chosen_index=0,
            runner_tp=float(long_tp),
            used_long_fallback=True,
        )
    n = len(rungs)
    suggested = suggest_runner_tp_index(n, int(bias))
    if index is None:
        chosen = suggested
    else:
        chosen = max(1, min(int(index), n))
    runner = rungs[chosen - 1]
    return LadderRunnerPick(
        rungs=tuple(rungs),
        long_tp=long_tp,
        n=n,
        base=int(math.ceil((n + 1) / 2.0)),
        bias=int(bias),
        suggested_index=suggested,
        chosen_index=chosen,
        runner_tp=float(runner),
        used_long_fallback=False,
    )


def pick_ladder_runner_tp(
    targets: Any,
    *,
    bias: int = 0,
    index: int | None = None,
) -> float | None:
    """Price-only wrapper around :func:`pick_ladder_runner` (bias 0 = the formula default)."""
    picked = pick_ladder_runner(targets, bias=bias, index=index)
    if picked is None:
        return None
    return picked.runner_tp


def _beyond_cover(side: str, cover: float, level: float, digits: int) -> bool:
    """True when ``level`` is strictly on the profit side of the 1R cover."""
    eps = 10 ** (-max(int(digits), 0))
    if _side_of(side) == "buy":
        return float(level) > float(cover) + eps
    return float(level) < float(cover) - eps


def plan_ladder_targets(
    ref: "SymbolRef",
    side: str,
    entry: float,
    sl: float,
    channel_tps: Any,
    *,
    bias: int = 0,
    runner_tp_index: int | None = None,
) -> LadderTargets:
    """Collapse a channel ladder into cover (1R) + runner (formula index / LONG fallback)."""
    s = _side_of(side)
    digits = _digits_of(ref)
    cover = one_r_take_profit(ref, s, entry, sl)
    levels: list[float] = []
    if isinstance(channel_tps, (list, tuple)):
        seen: set[float] = set()
        for item in channel_tps:
            try:
                value = float(item)
            except (TypeError, ValueError):
                continue
            if value <= 0.0:
                continue
            rounded = round_stop_away(ref, value, s, "tp")
            if rounded in seen:
                continue
            seen.add(rounded)
            levels.append(rounded)

    picked = pick_ladder_runner(levels, bias=bias, index=runner_tp_index)
    notes: list[str] = []
    runner: float
    n = 0
    base = 0
    suggested_index: int | None = None
    chosen_index: int | None = None
    long_tp: float | None = None
    if picked is None:
        runner = cover
        notes.append(
            "the ladder named no usable far target, so both legs would share the 1R cover "
            f"price {_fmt_price(cover, digits)}"
        )
    else:
        runner = float(picked.runner_tp)
        n = picked.n
        base = picked.base
        suggested_index = picked.suggested_index
        chosen_index = picked.chosen_index
        long_tp = picked.long_tp
        if n > 0 and not _beyond_cover(s, cover, runner, digits):
            # Formula (or ±1 override) landed at or inside 1R — the runner must stay the
            # farther leg or the BE watchdog treats it as the cover. Advance to the next
            # numbered rung, still capped at N-1 (N=2 may use TP2). LONG is the ceiling.
            cap = n if n == 2 else max(1, n - 1)
            start = max(int(chosen_index or 1), 1)
            advanced = None
            advanced_i = None
            for i in range(start, cap + 1):
                level = picked.rungs[i - 1]
                if _beyond_cover(s, cover, level, digits):
                    advanced = float(level)
                    advanced_i = i
                    break
            if advanced is not None and advanced_i is not None:
                notes.append(
                    f"formula TP{chosen_index} {_fmt_price(runner, digits)} is not beyond "
                    f"the 1R cover {_fmt_price(cover, digits)}; advanced to TP{advanced_i} "
                    f"{_fmt_price(advanced, digits)} (cap {cap})"
                )
                runner = advanced
                chosen_index = advanced_i
                if runner_tp_index is None:
                    suggested_index = advanced_i
            elif picked.long_tp is not None and _beyond_cover(s, cover, picked.long_tp, digits):
                notes.append(
                    f"no numbered rung beyond 1R within cap {cap}; LONG "
                    f"{_fmt_price(picked.long_tp, digits)} used as fallback ceiling"
                )
                runner = float(picked.long_tp)
            else:
                notes.append(
                    f"cover and runner both sit at {_fmt_price(cover, digits)}; the two legs "
                    "share that target"
                )

    placed = {cover, runner}
    unmanaged = tuple(level for level in levels if level not in placed)
    if picked is not None and n > 0:
        cap = n if n == 2 else max(1, n - 1)
        notes.append(
            f"ladder cover take-profit is 1R at {_fmt_price(cover, digits)} (same money "
            f"distance as the stop, not channel TP1); runner is TP{chosen_index}/{n} at "
            f"{_fmt_price(runner, digits)} (base=ceil((N+1)/2)={base}, bias{int(bias):+d}, "
            f"cap {cap}; LONG is a fallback ceiling, not N)"
        )
    else:
        notes.append(
            f"ladder cover take-profit is 1R at {_fmt_price(cover, digits)} (same money distance "
            f"as the stop, not channel TP1); runner take-profit is {_fmt_price(runner, digits)}"
        )
    return LadderTargets(
        cover_tp=cover,
        runner_tp=float(runner),
        unmanaged=unmanaged,
        notes=tuple(notes),
        n=n,
        bias=int(bias),
        base=base,
        suggested_index=suggested_index,
        chosen_index=chosen_index,
        long_tp=long_tp,
    )


def breakeven_stop(
    ref: "SymbolRef",
    side: str,
    entry: float,
    *,
    buffer_points: int = 2,
) -> float:
    """Stop at entry, pushed a few points into profit so a BE lock cannot be a scratch SL.

    Rounding goes **into profit**, not back through the entry: a buy's BE stop ceils, a
    sell's floors. Using :func:`round_stop_away` with kind ``sl`` would do the opposite and
    could put the lock back on the losing side of entry.
    """
    s = _side_of(side)
    point = _point_of(ref)
    steps_away = max(int(buffer_points), 1)
    raw = float(entry) + steps_away * point if s == "buy" else float(entry) - steps_away * point
    grid = raw / point
    if s == "buy":
        snapped = math.ceil(grid - _GRID_EPS)
    else:
        snapped = math.floor(grid + _GRID_EPS)
    return round_price(ref, snapped * point)


def stop_is_at_or_beyond_breakeven(side: str, entry: float, sl: float) -> bool:
    """True when ``sl`` is already at entry or locked into profit."""
    s = str(side or "").strip().lower()
    if s == "buy":
        return float(sl) >= float(entry) - 1e-12
    if s == "sell":
        return float(sl) <= float(entry) + 1e-12
    return False


# --------------------------------------------------------------------------------------
# Stop validation.
# --------------------------------------------------------------------------------------
def _stop_reference(
    side: str, entry: float, bid: float, ask: float, *, pending: bool
) -> tuple[tuple[float, str], tuple[float, str]]:
    """Return ((sl_reference, label), (tp_reference, label)) for the distance checks.

    A pending order is measured from its own order price. A market order is measured from
    the quote *facing* each level — a buy's stop against the bid and its target against the
    ask, mirrored for a sell — which is the smaller of the two distances in both cases and
    therefore the reading that cannot under-estimate a ``trade_stops_level`` breach.
    """
    if pending:
        return (entry, "the order price"), (entry, "the order price")
    if side == "buy":
        return (bid, "the bid"), (ask, "the ask")
    return (ask, "the ask"), (bid, "the bid")


def validate_stops(
    ref: "SymbolRef",
    side: str,
    entry: float,
    sl: float,
    tp: float,
    bid: float,
    ask: float,
    *,
    pending: bool,
) -> str | None:
    """Return ``None`` when the stops are legal, or one actionable sentence when they are not.

    Checked in order: the SL is on the correct side of the entry, the TP is on the correct
    side, each level is at least ``trade_stops_level`` points from the price it is measured
    against, and neither sits inside ``trade_freeze_level``. Every failure names the required
    figure, the actual figure, and a price that would work — ``10016`` on its own tells the
    model nothing it can act on (§2).

    ``sl == 0`` means "no stop" and is not checked here. Opening without a posted stop is
    legal; the stopless watchdog covers a position that stays naked. ``tp == 0`` means
    "no target", which is also legal (§5.3).
    """
    name = _name_of(ref)
    s = str(side or "").strip().lower()
    if s not in _SIDES:
        return f"unknown side {side!r} — use 'buy' or 'sell'"

    digits = _digits_of(ref)
    point = float(getattr(ref, "point", 0.0) or 0.0)
    if point <= 0.0:
        return f"cannot validate stops for {name}: its point size is {_trim(point)}"

    entry_value = float(entry)
    sl_value = float(sl or 0.0)
    tp_value = float(tp or 0.0)
    bid_value = float(bid or 0.0)
    ask_value = float(ask or 0.0)

    if entry_value <= 0.0:
        return (
            f"the entry price for {name} must be a real price, got "
            f"{_fmt_price(entry_value, digits)}"
        )

    # 1. The stop is on the correct side of the entry.
    if sl_value:
        if s == "buy" and sl_value >= entry_value:
            return (
                f"the stop loss must sit BELOW the entry on a buy: entry "
                f"{_fmt_price(entry_value, digits)}, sl {_fmt_price(sl_value, digits)} — "
                f"move the sl below the entry, or send this as a sell"
            )
        if s == "sell" and sl_value <= entry_value:
            return (
                f"the stop loss must sit ABOVE the entry on a sell: entry "
                f"{_fmt_price(entry_value, digits)}, sl {_fmt_price(sl_value, digits)} — "
                f"move the sl above the entry, or send this as a buy"
            )

    # 2. The target is on the correct side of the entry, when there is one.
    if tp_value:
        if s == "buy" and tp_value <= entry_value:
            return (
                f"the take profit must sit ABOVE the entry on a buy: entry "
                f"{_fmt_price(entry_value, digits)}, tp {_fmt_price(tp_value, digits)} — "
                f"move the tp above the entry, or send this as a sell"
            )
        if s == "sell" and tp_value >= entry_value:
            return (
                f"the take profit must sit BELOW the entry on a sell: entry "
                f"{_fmt_price(entry_value, digits)}, tp {_fmt_price(tp_value, digits)} — "
                f"move the tp below the entry, or send this as a buy"
            )

    if not pending and (bid_value <= 0.0 or ask_value <= 0.0):
        return (
            f"no live quote for {name} (bid {_fmt_price(bid_value, digits)}, ask "
            f"{_fmt_price(ask_value, digits)}) — the stop distance cannot be validated, so "
            f"the market is closed or the symbol is unavailable"
        )

    (sl_ref, sl_label), (tp_ref, tp_label) = _stop_reference(
        s, entry_value, bid_value, ask_value, pending=pending
    )
    stops_level = int(getattr(ref, "stops_level_points", 0) or 0)
    freeze_level = int(getattr(ref, "freeze_level_points", 0) or 0)

    levels = (
        ("sl", "stop loss", sl_value, sl_ref, sl_label),
        ("tp", "take profit", tp_value, tp_ref, tp_label),
    )

    # 3. Each level is far enough from the price it is measured against.
    if stops_level > 0:
        for kind, label, level, market, market_label in levels:
            if not level:
                continue
            actual = abs(level - market) / point
            if actual + _POINTS_EPS < stops_level:
                sign = 1.0 if level > market else -1.0
                suggestion = round_stop_away(ref, market + sign * stops_level * point, s, kind)
                message = (
                    f"the {label} is {actual:.1f} points from {market_label} "
                    f"({_fmt_price(market, digits)}); {name} requires at least "
                    f"{stops_level} points — move it to "
                    f"{_fmt_price(suggestion, digits)} or further"
                )
                if freeze_level > 0 and actual + _POINTS_EPS < freeze_level:
                    message += (
                        f" (it is also inside the {freeze_level}-point freeze level, where the "
                        f"order cannot be modified at all)"
                    )
                return message

    # 4. Neither level sits inside the freeze zone, where nothing can be modified or closed.
    if freeze_level > 0:
        for kind, label, level, market, market_label in levels:
            if not level:
                continue
            actual = abs(level - market) / point
            if actual + _POINTS_EPS < freeze_level:
                sign = 1.0 if level > market else -1.0
                suggestion = round_stop_away(ref, market + sign * freeze_level * point, s, kind)
                return (
                    f"the {label} is {actual:.1f} points from {market_label} "
                    f"({_fmt_price(market, digits)}), inside the {freeze_level}-point freeze "
                    f"level of {name}: while price is that close the order cannot be modified "
                    f"or closed — move it to {_fmt_price(suggestion, digits)} or further"
                )

    return None


# --------------------------------------------------------------------------------------
# Filling mode.
# --------------------------------------------------------------------------------------
def pick_filling(si: Any, action: int, partial: bool = False) -> int:
    """Choose ``type_filling`` for a request — BUILD_PROMPT §1.3, unchanged.

    ``si.filling_mode`` is a **bitmask** (FOK=1, IOC=2, BOC=4); ``request["type_filling"]``
    takes an **enum** (FOK=0, IOC=1, RETURN=2, BOC=3). Passing one into the other is the most
    common bug in MT5 Python code and it surfaces only at the broker as ``10030``.

    Accepts either a :class:`SymbolRef` or a raw ``symbol_info`` namespace — both carry
    ``filling_mode`` and ``trade_exemode``.
    """
    if action == TRADE_ACTION_PENDING:
        return ORDER_FILLING_RETURN  # pending orders are RETURN; FOK/IOC gives 10030
    fm = si.filling_mode
    if partial and (fm & SYMBOL_FILLING_IOC):
        return ORDER_FILLING_IOC  # FOK forbids partial fills
    if fm & SYMBOL_FILLING_FOK:
        return ORDER_FILLING_FOK
    if fm & SYMBOL_FILLING_IOC:
        return ORDER_FILLING_IOC
    if fm & SYMBOL_FILLING_BOC:
        return ORDER_FILLING_BOC
    if si.trade_exemode != SYMBOL_TRADE_EXECUTION_MARKET:
        return ORDER_FILLING_RETURN  # banned under Market execution
    raise TradingError(
        f"no valid filling mode for {si.name} "
        f"(filling_mode={fm}, exemode={si.trade_exemode})",
        detail={
            "symbol": getattr(si, "name", None),
            "filling_mode": fm,
            "trade_exemode": si.trade_exemode,
        },
    )


# --------------------------------------------------------------------------------------
# Pending order type.
# --------------------------------------------------------------------------------------
def infer_pending_type(
    side: str, order_type: str, price: float, bid: float, ask: float
) -> tuple[int | None, str]:
    """Pick ``BUY_LIMIT`` / ``SELL_LIMIT`` / ``BUY_STOP`` / ``SELL_STOP`` from side + type + price.

    A limit waits for price to come back to it, a stop waits for price to run to it, so the
    price's position relative to the market decides which of the four is legal. On a
    contradiction the second element names the type that was inferred, the price, the market
    it was compared against, and the type that would have been right — the broker's ``10015``
    says none of that.
    """
    s = str(side or "").strip().lower()
    if s not in _SIDES:
        return None, f"unknown side {side!r} — use 'buy' or 'sell'"
    ot = str(order_type or "").strip().lower()
    if ot not in _ORDER_TYPES:
        return None, f"unknown order_type {order_type!r} — use 'limit' or 'stop'"

    p = float(price)
    bid_value = float(bid or 0.0)
    ask_value = float(ask or 0.0)
    if p <= 0.0:
        return None, f"the order price must be a real price, got {_trim(p)}"
    if bid_value <= 0.0 or ask_value <= 0.0:
        return None, (
            f"there is no live quote to place a {s} {ot} against "
            f"(bid {_trim(bid_value)}, ask {_trim(ask_value)}) — the market is closed or the "
            f"symbol is unavailable"
        )

    price_text = _trim(p)
    bid_text = _trim(bid_value)
    ask_text = _trim(ask_value)

    if s == "buy":
        # A buy fills at the ask, so both buy pendings are measured against the ask.
        if ot == "limit":
            if p < ask_value:
                return ORDER_TYPE_BUY_LIMIT, (
                    f"a buy limit at {price_text} sits below the ask ({ask_text}) — placing "
                    f"BUY_LIMIT"
                )
            return None, (
                f"a buy limit must sit below the ask ({ask_text}); {price_text} is above it — "
                f"a buy stop is what you want there"
            )
        if p > ask_value:
            return ORDER_TYPE_BUY_STOP, (
                f"a buy stop at {price_text} sits above the ask ({ask_text}) — placing BUY_STOP"
            )
        return None, (
            f"a buy stop must sit above the ask ({ask_text}); {price_text} is below it — "
            f"a buy limit is what you want there"
        )

    # A sell fills at the bid, so both sell pendings are measured against the bid.
    if ot == "limit":
        if p > bid_value:
            return ORDER_TYPE_SELL_LIMIT, (
                f"a sell limit at {price_text} sits above the bid ({bid_text}) — placing "
                f"SELL_LIMIT"
            )
        return None, (
            f"a sell limit must sit above the bid ({bid_text}); {price_text} is below it — "
            f"a sell stop is what you want there"
        )
    if p < bid_value:
        return ORDER_TYPE_SELL_STOP, (
            f"a sell stop at {price_text} sits below the bid ({bid_text}) — placing SELL_STOP"
        )
    return None, (
        f"a sell stop must sit below the bid ({bid_text}); {price_text} is above it — "
        f"a sell limit is what you want there"
    )
