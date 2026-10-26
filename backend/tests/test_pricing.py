"""Pure-function tests for `app.trading.pricing` — the arithmetic that costs money.

No database, no network, no event loop, no MT5. Every function under test is pure: it takes a
`SymbolRef` and numbers and returns numbers. The refs are built from `tests.fake_mt5`'s
Exness-flavoured symbol table so the digit counts, point sizes, tick values and filling
bitmasks are the ones a real broker actually reports, not invented ones.

What is defended here, in the order BUILD_PROMPT raises it:

* **acceptance criterion 4** — on EURUSD (digits 5, point 0.00001, pip 0.0001) with a 3 000 USD
  balance, `risk_percent: 1` and a 20-pip stop, the volume is an exact multiple of
  `volume_step` and its loss at the stop is at most 30.00 USD and no worse than one step
  below it. Sizing rounds **down**, always: clamping up to `volume_min` would silently
  multiply the risk the model asked for, so that case is an error, not a correction (§2).
* **`split_volume` sums exactly** — a three-TP split must not lose or invent a step, and every
  part must clear `volume_min` or the broker answers `10014` (§5.3).
* **the filling-mode trap** — `symbol_info().filling_mode` is a *bitmask* (FOK=1, IOC=2, BOC=4)
  and `request["type_filling"]` is an *enum* (FOK=0, IOC=1, RETURN=2, BOC=3). Passing one into
  the other is the single most common bug in MT5 Python code and it surfaces only at the
  broker as `10030`. The bitmask must never come back out of `pick_filling`, which is asserted
  directly *and* round-tripped through `FakeMt5`, whose validator models both schemes (§1.3).
* **distance conversion and stop rounding** — every unit in both directions, rounded to the
  symbol's own digits, with `round_stop_away` always moving away from the market (§2).
* **stop validation says what is wrong** — a stop on the wrong side names the corrected side,
  and one inside `trade_stops_level` names the required distance and the actual one. A bare
  "invalid stops" tells the model nothing it can act on.
"""

from __future__ import annotations

import math

import pytest

from app.config import Settings
from app.errors import TradingError
from app.trading import pricing
from app.trading.constants import (
    ORDER_FILLING_BOC,
    ORDER_FILLING_FOK,
    ORDER_FILLING_IOC,
    ORDER_FILLING_RETURN,
    ORDER_TYPE_BUY,
    ORDER_TYPE_BUY_LIMIT,
    SYMBOL_FILLING_BOC,
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_PENDING,
)
from app.trading.rpc import to_ns
from app.trading.symbols import SymbolRef

from tests.fake_mt5 import RETCODE_INVALID_FILL, FakeMt5, default_symbols

# The four enum values `type_filling` accepts. Nothing outside this set may ever be returned,
# and in particular no value that only makes sense as a bitmask bit.
FILLING_ENUM = frozenset({ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN, ORDER_FILLING_BOC})

# Float noise: `1.085 - 1.083` is 0.0020000000000000018, so a loss computed from it lands a few
# parts in 1e15 above the exact figure. This tolerance absorbs that and nothing else — it is
# thirteen orders of magnitude below one volume_step of risk (2.00 USD on the sizing case).
EPS = 1e-9

# PIP_OVERRIDES is pinned empty so the operator's own .env cannot change what these tests mean.
SETTINGS = Settings(PIP_OVERRIDES={})

_TABLE = {row["name"]: row for row in default_symbols()}


def ref_for(name: str, *, settings: Settings = SETTINGS, **overrides: object) -> SymbolRef:
    """Build a SymbolRef from the fake broker's table, with fields overridden per test.

    The row goes through `to_ns` first, so the ref is built from the same attribute-access
    namespace `rpc.py` hands the trading layer — a field the fake stopped reporting would
    raise `AttributeError` here exactly as it would in production (§1.1).
    """
    row = dict(_TABLE[name])
    row.update(overrides)
    return SymbolRef.from_info(to_ns(row), settings)


# ======================================================================================
# Distances.
# ======================================================================================
def test_eurusd_reference_matches_the_broker_table() -> None:
    """The sizing case's preconditions, asserted before anything is computed from them."""
    ref = ref_for("EURUSDm")
    assert ref.digits == 5
    assert ref.point == pytest.approx(0.00001)
    assert ref.pip_size == pytest.approx(0.0001)
    assert ref.pip_convention == "fx_5digit"
    assert ref.volume_step == pytest.approx(0.01)
    assert ref.volume_min == pytest.approx(0.01)


def test_distance_units_reports_all_three_units_with_the_sign_kept() -> None:
    """Every distance the model sees carries price, points and pips at once (§2)."""
    ref = ref_for("EURUSDm")
    up = pricing.distance_units(ref, 0.0020)
    assert up == {"price": pytest.approx(0.002), "points": pytest.approx(200.0), "pips": pytest.approx(20.0)}
    down = pricing.distance_units(ref, -0.0020)
    assert down["points"] == pytest.approx(-200.0)
    assert down["pips"] == pytest.approx(-20.0)
    assert pricing.points(ref, 0.0020) == pytest.approx(200.0)
    assert pricing.pips(ref, 0.0020) == pytest.approx(20.0)


@pytest.mark.parametrize(
    ("direction", "distance", "unit", "expected"),
    [
        ("above", 20.0, "pips", 1.08700),
        ("below", 20.0, "pips", 1.08300),
        ("above", 200.0, "points", 1.08700),
        ("below", 200.0, "points", 1.08300),
        ("above", 0.0025, "price", 1.08750),
        ("below", 0.0025, "price", 1.08250),
        ("above", 1.0, "percent", 1.09585),
        ("below", 1.0, "percent", 1.07415),
    ],
)
def test_price_from_distance_every_unit_in_both_directions(
    direction: str, distance: float, unit: str, expected: float
) -> None:
    """"20 pips below entry" becomes an absolute price server-side, never in the model (§2)."""
    ref = ref_for("EURUSDm")
    out = pricing.price_from_distance(ref, 1.08500, direction, distance, unit)
    assert out["price"] == pytest.approx(expected, abs=5e-6)
    assert out["anchor"] == pytest.approx(1.08500)
    assert out["convention"] == "fx_5digit"
    # The result is self-consistent: price == anchor + distance["price"], signed.
    assert out["anchor"] + out["distance"]["price"] == pytest.approx(out["price"], abs=5e-6)
    sign = 1.0 if direction == "above" else -1.0
    assert math.copysign(1.0, out["distance"]["points"]) == sign


def test_price_from_distance_rounds_to_the_symbols_digits() -> None:
    """A 3.33-pip offset on 2-digit gold lands on the price grid, not between two ticks."""
    gold = ref_for("XAUUSDm")
    assert gold.digits == 2
    up = pricing.price_from_distance(gold, 2380.55, "above", 3.33, "pips")
    down = pricing.price_from_distance(gold, 2380.55, "below", 3.33, "pips")
    assert up["price"] == pytest.approx(2380.88)
    assert down["price"] == pytest.approx(2380.22)
    # Rounding to digits shrank the realised distance, and the result reports the realised one
    # rather than the requested one — the model reads what it will actually get.
    assert up["distance"]["pips"] == pytest.approx(3.3)
    for price in (up["price"], down["price"]):
        assert price == pytest.approx(round(price, 2))


def test_price_from_distance_rejects_a_negative_distance_and_an_unknown_unit() -> None:
    """Direction carries the sign; a negative distance is an ambiguity, not a shortcut."""
    ref = ref_for("EURUSDm")
    with pytest.raises(TradingError):
        pricing.price_from_distance(ref, 1.08500, "below", -20.0, "pips")
    with pytest.raises(TradingError):
        pricing.price_from_distance(ref, 1.08500, "below", 20.0, "ticks")
    with pytest.raises(TradingError):
        pricing.price_from_distance(ref, 1.08500, "sideways", 20.0, "pips")


# ======================================================================================
# Rounding away from the market.
# ======================================================================================
def test_round_stop_away_moves_a_buy_sl_down_and_a_sell_sl_up() -> None:
    """Rounding a stop toward price can breach stops_level after validation passed (§2)."""
    ref = ref_for("EURUSDm")
    price = 1.0845674
    point = ref.point

    buy_sl = pricing.round_stop_away(ref, price, "buy", "sl")
    sell_sl = pricing.round_stop_away(ref, price, "sell", "sl")

    assert buy_sl < price and buy_sl >= price - point
    assert sell_sl > price and sell_sl <= price + point
    assert buy_sl == pytest.approx(1.08456)
    assert sell_sl == pytest.approx(1.08457)


def test_round_stop_away_mirrors_for_take_profits() -> None:
    """A buy's target sits above and a sell's below, so the rounding direction mirrors."""
    ref = ref_for("EURUSDm")
    price = 1.0845674
    assert pricing.round_stop_away(ref, price, "buy", "tp") > price
    assert pricing.round_stop_away(ref, price, "sell", "tp") < price


def test_round_stop_away_rejects_an_unknown_side_or_kind() -> None:
    ref = ref_for("EURUSDm")
    with pytest.raises(TradingError):
        pricing.round_stop_away(ref, 1.08456, "long", "sl")
    with pytest.raises(TradingError):
        pricing.round_stop_away(ref, 1.08456, "buy", "stoploss")


# ======================================================================================
# Volume from risk — acceptance criterion 4.
# ======================================================================================
def test_compute_volume_acceptance_criterion_4() -> None:
    """3 000 USD, 1% risk, a 20-pip stop on EURUSD: an exact step multiple risking <= 30.00."""
    ref = ref_for("EURUSDm")
    entry = 1.08500
    sl = pricing.price_from_distance(ref, entry, "below", 20.0, "pips")["price"]
    assert sl == pytest.approx(1.08300)

    result = pricing.compute_volume(ref, 3000.0, 1.0, entry, sl, "USD")
    assert result.ok is True
    assert result.clamped is False

    # An exact multiple of volume_step: the broker answers 10014 to anything else.
    steps = result.volume / ref.volume_step
    assert abs(steps - round(steps)) < 1e-9
    assert result.volume == pytest.approx(0.15)

    target = 3000.0 * 1.0 / 100.0
    loss = result.volume * result.loss_per_lot
    assert loss <= target + EPS
    assert loss > target - ref.volume_step * result.loss_per_lot
    assert result.risk_amount == pytest.approx(loss)


@pytest.mark.parametrize("risk_percent", [0.5, 0.9, 1.0, 1.1, 1.7, 2.3, 5.0])
def test_compute_volume_always_rounds_down_never_up(risk_percent: float) -> None:
    """The step-rounding is a floor at every risk level, not a round-to-nearest."""
    ref = ref_for("EURUSDm")
    result = pricing.compute_volume(ref, 3000.0, risk_percent, 1.08500, 1.08300, "USD")
    assert result.ok is True

    target = 3000.0 * risk_percent / 100.0
    raw = target / result.loss_per_lot
    expected = math.floor(raw / ref.volume_step + 1e-9) * ref.volume_step
    assert result.volume == pytest.approx(expected)
    assert result.volume <= raw + 1e-12
    loss = result.volume * result.loss_per_lot
    assert loss <= target + EPS
    assert loss > target - ref.volume_step * result.loss_per_lot


def test_compute_volume_rounding_down_is_visible_on_a_non_exact_case() -> None:
    """1.1% wants 0.165 lots; it gets 0.16 and risks 32.00, not 34.00."""
    ref = ref_for("EURUSDm")
    result = pricing.compute_volume(ref, 3000.0, 1.1, 1.08500, 1.08300, "USD")
    assert result.ok is True
    assert result.volume == pytest.approx(0.16)
    assert result.volume * result.loss_per_lot == pytest.approx(32.0, abs=1e-6)


def test_compute_volume_below_minimum_refuses_and_does_not_clamp_up() -> None:
    """Clamping up to volume_min would silently multiply the intended risk, so it is an error."""
    ref = ref_for("EURUSDm")
    result = pricing.compute_volume(ref, 3000.0, 0.01, 1.08500, 1.08300, "USD")

    assert result.ok is False
    assert result.clamped is False
    assert result.volume < ref.volume_min
    assert result.volume != pytest.approx(ref.volume_min)
    assert result.volume == pytest.approx(0.0)
    # The message carries the number the model needs to decide: what the minimum lot costs.
    assert "below the 0.01 minimum" in result.message
    assert "2.00 USD" in result.message


def test_compute_volume_clamps_down_to_volume_max_and_says_so() -> None:
    """Clamping *down* is safe, so it happens — but it must be stated in the result (§2)."""
    ref = ref_for("EURUSDm")
    result = pricing.compute_volume(ref, 3000.0, 100000.0, 1.08500, 1.08300, "USD")
    assert result.ok is True
    assert result.clamped is True
    assert result.volume == pytest.approx(ref.volume_max)
    assert "clamped down" in result.message


@pytest.mark.parametrize(
    ("field", "value"),
    [("trade_tick_size", 0.0), ("trade_tick_value", 0.0), ("volume_step", 0.0)],
)
def test_compute_volume_refuses_when_a_sizing_input_is_missing(field: str, value: float) -> None:
    """A zero tick size, tick value or step cannot size a trade; say which one is broken."""
    overrides: dict[str, object] = {field: value}
    if field == "trade_tick_value":
        overrides["trade_tick_value_loss"] = 0.0
    ref = ref_for("EURUSDm", **overrides)
    result = pricing.compute_volume(ref, 3000.0, 1.0, 1.08500, 1.08300, "USD")
    assert result.ok is False
    assert "cannot size the trade" in result.message


def test_compute_volume_refuses_a_zero_stop_distance() -> None:
    """entry == sl divides by zero; it is refused with the field named."""
    ref = ref_for("EURUSDm")
    result = pricing.compute_volume(ref, 3000.0, 1.0, 1.08500, 1.08500, "USD")
    assert result.ok is False
    assert "sl_distance" in result.message


def test_compute_volume_uses_the_losing_tick_value() -> None:
    """A stop costs the losing side, so trade_tick_value_loss wins over trade_tick_value."""
    ref = ref_for("EURUSDm", trade_tick_value=1.0, trade_tick_value_loss=2.0)
    result = pricing.compute_volume(ref, 3000.0, 1.0, 1.08500, 1.08300, "USD")
    assert result.loss_per_lot == pytest.approx(400.0, rel=1e-9)
    assert result.volume == pytest.approx(0.07)


# ======================================================================================
# Volume splitting — multi-TP on a hedging account.
# ======================================================================================
@pytest.mark.parametrize("total", [0.10, 0.07, 0.15, 0.33, 1.00, 12.34])
def test_split_volume_sums_exactly_to_the_normalized_total(total: float) -> None:
    """Three parts, no step lost and none invented, every part fundable (§5.3)."""
    ref = ref_for("EURUSDm")
    parts = pricing.split_volume(ref, total, 3)
    assert len(parts) == 3

    normalized = pricing.normalize_volume(ref, total)
    step = ref.volume_step
    # Compared in whole steps as well as in lots: the integer form cannot hide float drift.
    assert sum(round(part / step) for part in parts) == round(normalized / step)
    assert round(sum(parts), 8) == pytest.approx(normalized)
    assert all(part >= ref.volume_min - 1e-12 for part in parts)
    assert all(abs(part / step - round(part / step)) < 1e-9 for part in parts)


def test_split_volume_reduces_the_count_when_the_total_cannot_fund_three_parts() -> None:
    """Below `parts * volume_min` the split shrinks rather than emitting an unfillable leg."""
    ref = ref_for("EURUSDm")  # volume_min 0.01, volume_step 0.01

    two = pricing.split_volume(ref, 0.02, 3)
    assert two == [pytest.approx(0.01), pytest.approx(0.01)]
    assert round(sum(two), 8) == pytest.approx(pricing.normalize_volume(ref, 0.02))

    one = pricing.split_volume(ref, 0.01, 3)
    assert len(one) == 1
    assert one[0] == pytest.approx(ref.volume_min)


def test_split_volume_reduced_count_on_a_coarse_minimum() -> None:
    """The same rule with volume_min ten steps wide: 0.25 funds two legs of >= 0.10, not three."""
    ref = ref_for("EURUSDm", volume_min=0.10)
    parts = pricing.split_volume(ref, 0.25, 3)
    assert len(parts) == 2
    assert all(part >= ref.volume_min - 1e-12 for part in parts)
    assert round(sum(parts), 8) == pytest.approx(pricing.normalize_volume(ref, 0.25))
    assert parts == [pytest.approx(0.13), pytest.approx(0.12)]


def test_split_volume_edge_counts() -> None:
    """`parts <= 0` is empty; one part is the whole normalized total."""
    ref = ref_for("EURUSDm")
    assert pricing.split_volume(ref, 0.10, 0) == []
    assert pricing.split_volume(ref, 0.10, -1) == []
    assert pricing.split_volume(ref, 0.10, 1) == [pytest.approx(0.10)]


def test_normalize_volume_floors_onto_the_step_grid() -> None:
    """Shaping a volume the broker will accept: floor to step, then clamp into [min, max]."""
    ref = ref_for("EURUSDm")
    assert pricing.normalize_volume(ref, 0.1749) == pytest.approx(0.17)
    assert pricing.normalize_volume(ref, 0.004) == pytest.approx(ref.volume_min)
    assert pricing.normalize_volume(ref, 10_000.0) == pytest.approx(ref.volume_max)


# ======================================================================================
# Filling mode — the bitmask/enum trap (§1.3).
# ======================================================================================
@pytest.mark.parametrize("name", sorted(_TABLE))
@pytest.mark.parametrize("partial", [False, True])
def test_pick_filling_pending_is_always_return(name: str, partial: bool) -> None:
    """A pending order is RETURN on every symbol; FOK or IOC there gives 10030."""
    ref = ref_for(name)
    assert pricing.pick_filling(ref, TRADE_ACTION_PENDING, partial=partial) == ORDER_FILLING_RETURN


def test_pick_filling_partial_close_prefers_ioc_over_fok() -> None:
    """FOK is all-or-nothing, so preferring it on a partial close is wrong."""
    ref = ref_for("EURUSDm")
    assert ref.filling_mode == SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC

    assert pricing.pick_filling(ref, TRADE_ACTION_DEAL, partial=True) == ORDER_FILLING_IOC
    assert pricing.pick_filling(ref, TRADE_ACTION_DEAL, partial=False) == ORDER_FILLING_FOK


def test_pick_filling_ioc_only_symbol_gets_ioc() -> None:
    """A symbol whose bitmask is IOC alone must not be handed FOK."""
    ref = ref_for("GBPUSDm")
    assert ref.filling_mode == SYMBOL_FILLING_IOC

    assert pricing.pick_filling(ref, TRADE_ACTION_DEAL, partial=False) == ORDER_FILLING_IOC
    assert pricing.pick_filling(ref, TRADE_ACTION_DEAL, partial=True) == ORDER_FILLING_IOC


@pytest.mark.parametrize(
    ("bitmask", "expected"),
    [
        (SYMBOL_FILLING_FOK, ORDER_FILLING_FOK),
        (SYMBOL_FILLING_IOC, ORDER_FILLING_IOC),
        (SYMBOL_FILLING_BOC, ORDER_FILLING_BOC),
        (SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC, ORDER_FILLING_FOK),
        (SYMBOL_FILLING_FOK | SYMBOL_FILLING_BOC, ORDER_FILLING_FOK),
        (SYMBOL_FILLING_IOC | SYMBOL_FILLING_BOC, ORDER_FILLING_IOC),
    ],
)
def test_pick_filling_never_returns_the_bitmask_where_an_enum_is_expected(
    bitmask: int, expected: int
) -> None:
    """The two schemes are different numbering; the bitmask must never leak into the enum.

    `filling_mode == 1` means "FOK allowed" while `type_filling == 1` means IOC — passing one
    through as the other is the bug that fails only at the broker, as 10030.
    """
    ref = ref_for("EURUSDm", filling_mode=bitmask)
    chosen = pricing.pick_filling(ref, TRADE_ACTION_DEAL)

    assert chosen == expected
    assert chosen in FILLING_ENUM
    # The single-bit cases are the ones where a pass-through would be invisible by luck:
    # FOK (bit 1 -> enum 0), IOC (bit 2 -> enum 1), BOC (bit 4 -> enum 3) all differ.
    if bitmask in (SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC, SYMBOL_FILLING_BOC):
        assert chosen != bitmask


@pytest.mark.parametrize("name", sorted(_TABLE))
@pytest.mark.parametrize("partial", [False, True])
def test_pick_filling_result_is_always_a_filling_enum(name: str, partial: bool) -> None:
    ref = ref_for(name)
    for action in (TRADE_ACTION_DEAL, TRADE_ACTION_PENDING):
        assert pricing.pick_filling(ref, action, partial=partial) in FILLING_ENUM


def test_pick_filling_falls_back_to_return_only_off_market_execution() -> None:
    """RETURN is never a bit in filling_mode, so filling_mode == 0 is not "nothing allowed"."""
    instant = ref_for("EURUSDm", filling_mode=0, trade_exemode=1)
    assert pricing.pick_filling(instant, TRADE_ACTION_DEAL) == ORDER_FILLING_RETURN


def test_pick_filling_raises_when_no_mode_is_possible() -> None:
    """Market execution with an empty bitmask has no legal answer; say which symbol and why."""
    market = ref_for("EURUSDm", filling_mode=0, trade_exemode=2)
    with pytest.raises(TradingError) as excinfo:
        pricing.pick_filling(market, TRADE_ACTION_DEAL)
    assert "no valid filling mode" in str(excinfo.value)
    assert "EURUSDm" in str(excinfo.value)


@pytest.mark.parametrize("name", ["EURUSDm", "GBPUSDm", "XAUUSDm"])
def test_pick_filling_is_accepted_by_a_broker_that_models_both_schemes(name: str) -> None:
    """Round-trip: FakeMt5 validates type_filling against the bitmask, exactly as MT5 does.

    This is the assertion that would have caught the pass-through bug in production code —
    the fake answers 10030 ("Unsupported filling mode") to a request built the wrong way.
    """
    broker = FakeMt5()
    broker.call("symbol_select", {"symbol": name, "enable": True})
    row = broker.symbols[name]
    ref = ref_for(name)

    for partial in (False, True):
        request = {
            "action": TRADE_ACTION_DEAL,
            "symbol": name,
            "volume": row["volume_min"],
            "type": ORDER_TYPE_BUY,
            "price": row["ask"],
            "sl": 0.0,
            "tp": 0.0,
            "type_filling": pricing.pick_filling(ref, TRADE_ACTION_DEAL, partial=partial),
        }
        assert broker.call("order_check", {"request": request})["retcode"] != RETCODE_INVALID_FILL

    pending = {
        "action": TRADE_ACTION_PENDING,
        "symbol": name,
        "volume": row["volume_min"],
        "type": ORDER_TYPE_BUY_LIMIT,
        "price": round(row["bid"] * 0.99, row["digits"]),
        "sl": 0.0,
        "tp": 0.0,
        "type_filling": pricing.pick_filling(ref, TRADE_ACTION_PENDING),
    }
    assert broker.call("order_check", {"request": pending})["retcode"] != RETCODE_INVALID_FILL


# ======================================================================================
# Stop validation.
# ======================================================================================
def test_validate_stops_accepts_a_correct_pair() -> None:
    ref = ref_for("EURUSDm", trade_stops_level=100)
    assert (
        pricing.validate_stops(
            ref, "buy", 1.08508, 1.08300, 1.08800, bid=1.08500, ask=1.08508, pending=False
        )
        is None
    )


def test_validate_stops_rejects_a_buy_sl_above_the_entry_naming_the_corrected_side() -> None:
    ref = ref_for("EURUSDm")
    message = pricing.validate_stops(
        ref, "buy", 1.08500, 1.08600, 0.0, bid=1.08500, ask=1.08508, pending=False
    )
    assert message is not None
    assert "BELOW" in message
    assert "1.08600" in message and "1.08500" in message


def test_validate_stops_rejects_a_sell_sl_below_the_entry_naming_the_corrected_side() -> None:
    ref = ref_for("EURUSDm")
    message = pricing.validate_stops(
        ref, "sell", 1.08500, 1.08400, 0.0, bid=1.08500, ask=1.08508, pending=False
    )
    assert message is not None
    assert "ABOVE" in message
    assert "1.08400" in message


def test_validate_stops_rejects_a_tp_on_the_wrong_side_too() -> None:
    ref = ref_for("EURUSDm")
    message = pricing.validate_stops(
        ref, "buy", 1.08500, 1.08300, 1.08200, bid=1.08500, ask=1.08508, pending=False
    )
    assert message is not None
    assert "take profit" in message
    assert "ABOVE" in message


def test_validate_stops_rejects_inside_trade_stops_level_naming_required_and_actual() -> None:
    """`10016` on its own tells the model nothing; the message must carry both figures (§2)."""
    ref = ref_for("EURUSDm", trade_stops_level=100)
    message = pricing.validate_stops(
        ref, "buy", 1.08508, 1.08480, 0.0, bid=1.08500, ask=1.08508, pending=False
    )
    assert message is not None
    assert "20.0 points" in message          # actual
    assert "requires at least 100 points" in message  # required
    assert "1.08400" in message              # a level that would work


def test_validate_stops_measures_a_pending_order_from_its_own_price() -> None:
    """A pending order's stop distance is measured from the order price, not from the market."""
    ref = ref_for("EURUSDm", trade_stops_level=100)
    # 30 points from the order price, far from the market: still a breach.
    message = pricing.validate_stops(
        ref, "buy", 1.08000, 1.07970, 0.0, bid=1.08500, ask=1.08508, pending=True
    )
    assert message is not None
    assert "the order price" in message
    assert "requires at least 100 points" in message


def test_validate_stops_reports_the_freeze_level() -> None:
    """Inside trade_freeze_level nothing can be modified at all — a distinct, named failure."""
    ref = ref_for("EURUSDm", trade_stops_level=0, trade_freeze_level=100)
    message = pricing.validate_stops(
        ref, "buy", 1.08508, 1.08480, 0.0, bid=1.08500, ask=1.08508, pending=False
    )
    assert message is not None
    assert "freeze level" in message


def test_validate_stops_refuses_without_a_live_quote() -> None:
    """A tick of zeros is what an unselected symbol returns; it cannot validate a distance."""
    ref = ref_for("EURUSDm", trade_stops_level=100)
    message = pricing.validate_stops(
        ref, "buy", 1.08508, 1.08300, 0.0, bid=0.0, ask=0.0, pending=False
    )
    assert message is not None
    assert "no live quote" in message


def test_validate_stops_ignores_a_zero_sl_because_that_is_guards_rule() -> None:
    """`sl == 0` means "no stop": §6 states that in the words the model is trained on."""
    ref = ref_for("EURUSDm", trade_stops_level=100)
    assert (
        pricing.validate_stops(
            ref, "buy", 1.08508, 0.0, 0.0, bid=1.08500, ask=1.08508, pending=False
        )
        is None
    )


# ======================================================================================
# Multi-TP ladders — cover 1R + runner at formula index, not one ticket per celebration rung.
# ======================================================================================
def test_is_tp_ladder_requires_four_positive_targets() -> None:
    """3 TPs stay classic; 4+ become a ladder even if a zero is mixed in."""
    assert pricing.is_tp_ladder([2390.0, 2400.0, 2410.0]) is False
    assert pricing.is_tp_ladder([2390.0, 2400.0, 2410.0, 2420.0]) is True
    assert pricing.is_tp_ladder([2390.0, 0.0, 2400.0, 2410.0, 2420.0]) is True


def test_one_r_take_profit_is_the_same_money_distance_as_the_stop() -> None:
    """Channel TP1 is irrelevant: buy 4345 / SL 4335 → cover TP 4355."""
    gold = ref_for("XAUUSDm")
    assert pricing.one_r_take_profit(gold, "buy", 4345.0, 4335.0) == pytest.approx(4355.0)
    assert pricing.one_r_take_profit(gold, "sell", 4345.0, 4355.0) == pytest.approx(4335.0)


def test_split_ladder_rungs_excludes_long_from_n() -> None:
    """Live XAUUSD shape: seven 3-point rungs then a 10-point LONG ceiling."""
    rungs = [4350.0, 4353.0, 4356.0, 4359.0, 4362.0, 4365.0, 4368.0, 4378.0]
    numbered, long_tp = pricing.split_ladder_rungs(rungs)
    assert numbered == rungs[:7]
    assert long_tp == pytest.approx(4378.0)
    equal = [2390.0, 2400.0, 2410.0, 2420.0]
    numbered4, long4 = pricing.split_ladder_rungs(equal)
    assert numbered4 == equal
    assert long4 is None


def test_suggest_runner_tp_index_n4_n5_n7_with_bias() -> None:
    """Agreed formula: base = ceil((N+1)/2), cap N-1 when N>=3. No random."""
    # N=7 → base 4; bias 0/1/2 → TP4/5/6, never 7.
    assert pricing.suggest_runner_tp_index(7, 0) == 4
    assert pricing.suggest_runner_tp_index(7, 1) == 5
    assert pricing.suggest_runner_tp_index(7, 2) == 6
    # N=4 → base 3, cap 3 → TP3 even with extra bias.
    assert pricing.suggest_runner_tp_index(4, 0) == 3
    assert pricing.suggest_runner_tp_index(4, 1) == 3
    assert pricing.suggest_runner_tp_index(4, 2) == 3
    # N=5 → base 3, cap 4 → TP3 or TP4, not TP5.
    assert pricing.suggest_runner_tp_index(5, 0) == 3
    assert pricing.suggest_runner_tp_index(5, 1) == 4
    assert pricing.suggest_runner_tp_index(5, 2) == 4
    # Short lists: N=1 that sole TP; N=2 runner may use TP2.
    assert pricing.suggest_runner_tp_index(1, 0) == 1
    assert pricing.suggest_runner_tp_index(2, 0) == 2
    assert pricing.suggest_runner_tp_index(3, 0) == 2  # cap N-1


def test_pick_ladder_runner_uses_formula_not_hardcoded_fifth() -> None:
    """N=7 after dropping LONG; bias 0 is TP4, not the old prefer-5th policy."""
    rungs = [4350.0, 4353.0, 4356.0, 4359.0, 4362.0, 4365.0, 4368.0, 4378.0]
    assert pricing.pick_ladder_runner_tp(rungs, bias=0) == pytest.approx(4359.0)
    assert pricing.pick_ladder_runner_tp(rungs, bias=1) == pytest.approx(4362.0)
    assert pricing.pick_ladder_runner_tp(rungs, bias=2) == pytest.approx(4365.0)
    picked = pricing.pick_ladder_runner(rungs, bias=2)
    assert picked is not None
    assert picked.n == 7
    assert picked.chosen_index == 6
    assert picked.long_tp == pytest.approx(4378.0)
    # N=4 equal spacing: TP3, not TP4.
    assert pricing.pick_ladder_runner_tp(rungs[:4], bias=0) == pytest.approx(4356.0)
    assert pricing.pick_ladder_runner_tp(rungs[:4], bias=2) == pytest.approx(4356.0)
    # N=5: TP3 / TP4, never TP5.
    assert pricing.pick_ladder_runner_tp(rungs[:5], bias=0) == pytest.approx(4356.0)
    assert pricing.pick_ladder_runner_tp(rungs[:5], bias=1) == pytest.approx(4359.0)
    assert pricing.pick_ladder_runner_tp(rungs[:5], bias=2) == pytest.approx(4359.0)


def test_plan_ladder_targets_does_not_use_channel_tp1_as_cover() -> None:
    gold = ref_for("XAUUSDm")
    rungs = [4350.0, 4353.0, 4356.0, 4359.0, 4362.0, 4365.0, 4368.0, 4378.0]
    planned = pricing.plan_ladder_targets(gold, "buy", 4347.0, 4334.0, rungs, bias=0)
    assert planned.cover_tp == pytest.approx(4360.0)  # 4347 + 13
    # Formula TP4 (4359) sits inside 1R; server advances to TP5 so the runner stays farther.
    assert planned.runner_tp == pytest.approx(4362.0)
    assert planned.n == 7
    assert planned.suggested_index == 5
    assert planned.chosen_index == 5
    assert 4350.0 in planned.unmanaged
    assert 4378.0 in planned.unmanaged
    assert planned.cover_tp not in rungs or planned.cover_tp != rungs[0]
    shifted = pricing.plan_ladder_targets(
        gold, "buy", 4347.0, 4334.0, rungs, bias=0, runner_tp_index=6
    )
    assert shifted.runner_tp == pytest.approx(4365.0)
    assert shifted.chosen_index == 6
    assert any("advanced" in note for note in planned.notes)


def test_breakeven_stop_locks_a_few_points_into_profit() -> None:
    gold = ref_for("XAUUSDm")
    buy_be = pricing.breakeven_stop(gold, "buy", 4349.14, buffer_points=2)
    assert buy_be == pytest.approx(4349.16)
    assert pricing.stop_is_at_or_beyond_breakeven("buy", 4349.14, buy_be)
    sell_be = pricing.breakeven_stop(gold, "sell", 4349.14, buffer_points=2)
    assert sell_be == pytest.approx(4349.12)
    assert pricing.stop_is_at_or_beyond_breakeven("sell", 4349.14, sell_be)
    assert pricing.stop_is_at_or_beyond_breakeven("buy", 4349.14, 4334.0) is False
