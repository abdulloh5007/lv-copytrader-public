"""Tests for `app.trading.symbols` — resolution, pip conventions, quotes, broker clock.

No database, no network, no MT5: the resolver talks to `tests.fake_rpc.FakeRpc` over
`tests.fake_mt5.FakeMt5`, an in-process broker whose symbol table carries real Exness
suffixes and real digit counts. Everything asserted here is decided by `symbols.py` alone.

What is defended:

* **acceptance criterion 3** — `candidates("gold")` returns the broker's *actual* symbol,
  `XAUUSDm`, first, with its `pip_size` (0.10), the name of the rule that decided it
  (`metal_x10`) and its `stops_level_points` read from the broker rather than hardcoded.
  The last one is asserted against two different tables so a constant could not satisfy both.
* **the pip convention is a named branch, not a guess** (§2) — getting a pip wrong scales
  every stop by ten, so each branch is pinned: 5-digit FX, 3-digit JPY, 2-digit and 3-digit
  metals, plain-point instruments, and the operator's `PIP_OVERRIDES` escape hatch. A symbol
  whose digits fit no branch raises rather than inventing a pip.
* **resolution is a ranked list, never one silent guess** — exact and alias hits outrank a
  substring hit, the list is capped at `MAX_SYMBOL_CANDIDATES`, and a query that matches
  nothing still produces `closest()` names the model can try instead of an empty answer.
* **an unselected symbol quotes zeros rather than erroring** — `quote()` must select first
  and must retry once before calling the market closed.
* **the broker's clock is not this machine's clock** — offsets are measured from a live tick.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.errors import TradingError
from app.trading import symbols as symbols_mod
from app.trading.symbols import (
    ALIASES,
    BROKER_SUFFIXES,
    SymbolRef,
    SymbolResolver,
    broker_day_start_ts,
    pip_convention,
    pip_size,
    server_time_offset,
)

from tests.fake_mt5 import FakeMt5, default_symbols
from tests.fake_rpc import FakeRpc

# PIP_OVERRIDES pinned empty so the operator's own .env cannot change what these tests mean.
SETTINGS = Settings(PIP_OVERRIDES={})


def make_resolver(
    *, settings: Settings = SETTINGS, rows: list[dict] | None = None
) -> tuple[SymbolResolver, FakeMt5]:
    """Build a resolver over a fresh fake broker, returning both so the table can be read."""
    broker = FakeMt5(symbols=rows if rows is not None else default_symbols())
    return SymbolResolver(FakeRpc(broker), settings), broker


def si(**fields: object) -> SimpleNamespace:
    """A minimal `symbol_info` namespace for the pure pip functions."""
    base = {"name": "TESTUSD", "description": "", "digits": 5, "point": 0.00001}
    base.update(fields)
    return SimpleNamespace(**base)


# ======================================================================================
# Acceptance criterion 3.
# ======================================================================================
async def test_candidates_gold_resolves_to_the_brokers_own_symbol() -> None:
    """"gold" -> XAUUSDm with its real suffix, pip size, convention and stops level."""
    resolver, broker = make_resolver()

    found = await resolver.candidates("gold")

    assert found, "the alias pass must find gold on a table that only carries XAUUSDm"
    top = found[0]
    assert top.name == "XAUUSDm"
    assert top.pip_size == pytest.approx(0.10)
    assert top.pip_convention == "metal_gold_0.10"
    assert top.digits == 2
    assert top.point == pytest.approx(0.01)
    # Read from the broker, not from a constant.
    assert top.stops_level_points == broker.symbols["XAUUSDm"]["trade_stops_level"]


async def test_candidates_gold_carries_the_brokers_stops_level_not_a_constant() -> None:
    """The same query against a table with a non-zero stops level reports that level.

    Two tables, two different answers, one assertion each: a hardcoded `stops_level_points`
    could satisfy the test above by luck, and cannot satisfy both.
    """
    rows = default_symbols()
    for row in rows:
        if row["name"] == "XAUUSDm":
            row["trade_stops_level"] = 30
            row["trade_freeze_level"] = 15

    resolver, broker = make_resolver(rows=rows)
    top = (await resolver.candidates("gold"))[0]

    assert top.name == "XAUUSDm"
    assert top.stops_level_points == 30
    assert top.freeze_level_points == 15
    assert top.pip_size == pytest.approx(0.10)
    assert top.pip_convention == "metal_gold_0.10"
    assert broker.symbols["XAUUSDm"]["trade_stops_level"] == 30


async def test_resolve_row_shape_is_what_the_model_is_shown() -> None:
    """`to_dict()` is the tool-facing row: the model must see the convention it will get."""
    resolver, _ = make_resolver()
    row = (await resolver.candidates("gold"))[0].to_dict()

    assert row["name"] == "XAUUSDm"
    assert row["pip_size"] == pytest.approx(0.10)
    assert row["pip_convention"] == "metal_gold_0.10"
    assert set(row) >= {
        "name",
        "description",
        "digits",
        "point",
        "pip_size",
        "pip_convention",
        "volume_min",
        "volume_step",
        "volume_max",
        "stops_level_points",
        "freeze_level_points",
        "spread_points",
        "tradable",
    }


# ======================================================================================
# Pip conventions — the branch that fired is part of the answer.
# ======================================================================================
@pytest.mark.parametrize(
    ("name", "expected_pip", "expected_convention"),
    [
        ("EURUSDm", 0.0001, "fx_5digit"),
        ("GBPUSDm", 0.0001, "fx_5digit"),
        ("USDJPYm", 0.01, "fx_3digit_jpy"),
        ("XAUUSDm", 0.10, "metal_gold_0.10"),
        ("XAGUSDm", 0.01, "metal_silver_0.01"),
        ("US30m", 0.01, "point"),
        ("USTECm", 0.01, "point"),
        ("BTCUSDm", 0.01, "point"),
    ],
)
async def test_pip_convention_per_symbol(
    name: str, expected_pip: float, expected_convention: str
) -> None:
    """Each branch of the §2 table, pinned against a broker-realistic symbol."""
    resolver, _ = make_resolver()
    ref = await resolver.get(name)
    assert ref.pip_size == pytest.approx(expected_pip)
    assert ref.pip_convention == expected_convention


def test_pip_size_of_a_two_digit_metal_is_ten_points() -> None:
    """XAUUSD quotes 2 digits with point 0.01, and one pip there is 0.10, not 0.01."""
    gold = si(name="XAUUSD", digits=2, point=0.01, description="Gold vs US Dollar")
    assert pip_size(gold, settings=SETTINGS) == pytest.approx(0.10)
    assert pip_convention(gold, settings=SETTINGS) == "metal_gold_0.10"


def test_a_metal_is_recognised_by_its_description_when_the_name_hides_it() -> None:
    """Not every broker spells gold "XAU"; the description is the second test."""
    weird = si(name="GLDm", digits=2, point=0.01, description="Spot Gold")
    assert pip_convention(weird, settings=SETTINGS) == "metal_gold_0.10"
    assert pip_size(weird, settings=SETTINGS) == pytest.approx(0.10)


def test_pip_overrides_take_precedence_over_every_branch() -> None:
    """The operator corrects a symbol in config, without touching code (§2)."""
    settings = Settings(PIP_OVERRIDES={"eurusdm": 0.001})
    ref = si(name="EURUSDm", digits=5, point=0.00001)
    assert pip_size(ref, settings=settings) == pytest.approx(0.001)
    assert pip_convention(ref, settings=settings) == "override"


def test_an_undecidable_pip_raises_instead_of_guessing() -> None:
    """Digits that fit no branch must fail loudly and name the config key that fixes it."""
    exotic = si(name="WEIRDm", digits=1, point=0.1, description="Something exotic")
    with pytest.raises(TradingError) as excinfo:
        pip_size(exotic, settings=SETTINGS)
    assert "no pip convention" in str(excinfo.value)
    assert "PIP_OVERRIDES" in str(excinfo.value)


# ======================================================================================
# Suffixes and aliases.
# ======================================================================================
def test_broker_suffix_table_covers_the_ones_the_spec_names() -> None:
    lowered = {suffix.lower() for suffix in BROKER_SUFFIXES}
    assert {"m", "c", ".raw", "_i", "#", ".pro", "-ecn", ".stp"} <= lowered


def test_alias_table_covers_the_ones_the_spec_names() -> None:
    assert "XAUUSD" in ALIASES["GOLD"] and "XAUUSD" in ALIASES["XAU"]
    assert "XAGUSD" in ALIASES["SILVER"]
    assert "US30" in ALIASES["DOW"] and "US30" in ALIASES["DJ30"] and "US30" in ALIASES["US30"]
    assert "USTEC" in ALIASES["NAS100"] and "USTEC" in ALIASES["NDX"] and "USTEC" in ALIASES["USTEC"]
    assert "GER40" in ALIASES["DAX"] and "GER40" in ALIASES["GER40"]
    assert "BTCUSD" in ALIASES["BTC"]
    assert {"USOIL", "XTIUSD"} <= set(ALIASES["OIL"]) and {"USOIL", "XTIUSD"} <= set(ALIASES["WTI"])


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("XAUUSD", "XAUUSDm"),   # the suffix comes off the candidate
        ("XAUUSDm", "XAUUSDm"),  # exact
        ("xauusd", "XAUUSDm"),   # case-insensitive
        ("EUR/USD", "EURUSDm"),  # slashes are collapsed
        (" eurusd ", "EURUSDm"), # whitespace is stripped
        ("gold", "XAUUSDm"),
        ("XAU", "XAUUSDm"),
        ("silver", "XAGUSDm"),
        ("dow", "US30m"),
        ("us30", "US30m"),
        ("nas100", "USTECm"),
        ("nas", "USTECm"),
        ("ndx", "USTECm"),
        ("btc", "BTCUSDm"),
        ("bitcoin", "BTCUSDm"),
        ("oil", "USOILm"),
        ("wti", "USOILm"),
        ("cable", "GBPUSDm"),
        ("fiber", "EURUSDm"),
    ],
)
async def test_query_resolves_to_the_expected_symbol(query: str, expected: str) -> None:
    """A signal channel's vocabulary, mapped onto one broker's suffixed table."""
    resolver, _ = make_resolver()
    found = await resolver.candidates(query)
    assert found, f"{query!r} resolved to nothing"
    assert found[0].name == expected


async def test_an_exact_or_alias_hit_outranks_a_substring_hit() -> None:
    """Ranking is by pass order first: the alias pass beats a description substring."""
    rows = default_symbols()
    decoy = dict(rows[0])
    decoy.update({"name": "GOLDBASKETm", "description": "Gold Miners Basket"})
    rows.append(decoy)

    resolver, _ = make_resolver(rows=rows)
    names = [ref.name for ref in await resolver.candidates("gold")]

    assert names[0] == "XAUUSDm"          # alias pass
    assert "GOLDBASKETm" in names          # substring pass, but ranked below
    assert names.index("XAUUSDm") < names.index("GOLDBASKETm")

    # And the decoy is still reachable by its own name, at the exact pass.
    exact = await resolver.candidates("GOLDBASKETm")
    assert exact[0].name == "GOLDBASKETm"


async def test_candidates_are_capped_at_max_symbol_candidates() -> None:
    """The model gets a ranked shortlist, never the whole table."""
    settings = Settings(PIP_OVERRIDES={}, MAX_SYMBOL_CANDIDATES=3)
    resolver, _ = make_resolver(settings=settings)
    found = await resolver.candidates("USD")
    assert len(found) == 3
    assert len({ref.name for ref in found}) == 3


async def test_candidates_of_an_empty_or_unmatched_query_is_empty() -> None:
    resolver, _ = make_resolver()
    assert await resolver.candidates("") == []
    assert await resolver.candidates("   ") == []
    assert await resolver.candidates("ZZQQXX") == []


async def test_closest_always_offers_names_to_try() -> None:
    """Zero candidates still produces a usable hint; an empty list helps nobody."""
    resolver, _ = make_resolver()

    hint = await resolver.closest("ZZQQXX", 3)
    assert len(hint) == 3
    assert all(isinstance(name, str) and name for name in hint)

    near = await resolver.closest("eurus", 3)
    assert near[0] == "EURUSDm"

    assert await resolver.closest("gold", 0) == []


# ======================================================================================
# get() and quote().
# ======================================================================================
async def test_get_selects_the_symbol_in_market_watch_first() -> None:
    """An unselected symbol ticks zeros rather than erroring, so selection is not optional."""
    resolver, broker = make_resolver()
    assert "EURUSDm" not in broker.selected

    ref = await resolver.get("EURUSDm")

    assert ref.name == "EURUSDm"
    assert "EURUSDm" in broker.selected


async def test_get_of_an_unknown_symbol_raises_with_a_hint() -> None:
    resolver, _ = make_resolver()
    with pytest.raises(TradingError) as excinfo:
        await resolver.get("NOPEUSD")
    error = excinfo.value
    assert "not available" in str(error)
    assert error.detail["hint"], "a failed resolution must still suggest names to try"


async def test_get_requires_a_name() -> None:
    resolver, _ = make_resolver()
    with pytest.raises(TradingError):
        await resolver.get("   ")


async def test_quote_reports_the_spread_in_points_and_pips() -> None:
    resolver, broker = make_resolver()

    quote = await resolver.quote("EURUSDm")

    row = broker.symbols["EURUSDm"]
    assert quote.symbol == "EURUSDm"
    assert quote.bid == pytest.approx(row["bid"])
    assert quote.ask == pytest.approx(row["ask"])
    assert quote.spread_points == row["spread"]
    assert quote.spread_pips == pytest.approx(row["spread"] * row["point"] / 0.0001)
    assert quote.server_time and "T" in quote.server_time
    assert quote.to_dict()["spread_points"] == row["spread"]


async def test_quote_of_a_dead_symbol_says_market_closed_after_one_retry() -> None:
    """Two zero ticks in a row, 500 ms apart, is the market being closed — and says so."""
    resolver, broker = make_resolver()
    broker.zero_tick_symbols.add("EURUSDm")

    with pytest.raises(TradingError) as excinfo:
        await resolver.quote("EURUSDm")

    assert "no quotes for EURUSDm" in str(excinfo.value)
    tick_calls = [call for call in broker.calls if call[0] == "symbol_info_tick"]
    assert len(tick_calls) == 2, "a single zero tick is not yet a closed market"


async def test_quote_recovers_when_the_second_tick_is_live() -> None:
    """A just-selected symbol needs a moment; one retry is what decides it."""
    resolver, broker = make_resolver()
    broker.zero_tick_symbols.add("EURUSDm")

    original = broker.m_symbol_info_tick
    state = {"calls": 0}

    def flaky(symbol: str):
        state["calls"] += 1
        if state["calls"] >= 2:
            broker.zero_tick_symbols.discard("EURUSDm")
        return original(symbol)

    broker.m_symbol_info_tick = flaky  # type: ignore[method-assign]

    quote = await resolver.quote("EURUSDm")

    assert state["calls"] == 2
    assert quote.bid > 0.0 and quote.ask > 0.0


# ======================================================================================
# The symbol cache.
# ======================================================================================
async def test_refresh_caches_the_table_and_force_reloads_it() -> None:
    """`symbols_get()` is one round-trip over hundreds of rows; it is cached for an hour."""
    resolver, broker = make_resolver()

    assert await resolver.refresh() == len(broker.symbols)
    assert await resolver.refresh() == len(broker.symbols)
    assert len([call for call in broker.calls if call[0] == "symbols_get"]) == 1

    assert await resolver.refresh(force=True) == len(broker.symbols)
    assert len([call for call in broker.calls if call[0] == "symbols_get"]) == 2
    assert resolver.count == len(broker.symbols)


async def test_an_undecidable_symbol_does_not_break_an_unrelated_query() -> None:
    """One exotic instrument in the table cannot take resolution down with it.

    Behaviour deliberately changed after an adversarial audit: such a symbol is CARRIED with
    ``pip_size 0.0`` and a ``PIP_OVERRIDES`` note, not deleted. Dropping it denied tradable
    instruments that §2's alias table mandates — US30 quoted to one decimal, JP225 and many
    BTCUSD feeds to zero — and buried the remedy in a swallowed log the model never sees.
    """
    rows = default_symbols()
    broken = dict(rows[0])
    broken.update({"name": "WEIRDUSDm", "description": "Exotic", "digits": 1, "point": 0.1})
    rows.append(broken)

    resolver, _ = make_resolver(rows=rows)

    found = await resolver.candidates("USD")
    names = [ref.name for ref in found]
    assert "EURUSDm" in names, "the unrelated query still resolves normally"

    weird = next((ref for ref in found if ref.name == "WEIRDUSDm"), None)
    assert weird is not None, "a symbol with no pip convention is carried, not dropped"
    assert weird.pip_convention == "unresolved" and weird.pip_size == 0.0
    assert "PIP_OVERRIDES" in weird.to_dict()["pip_note"]


# ======================================================================================
# tradable.
# ======================================================================================
@pytest.mark.parametrize(("trade_mode", "expected"), [(4, True), (0, False), (1, False), (3, False)])
async def test_tradable_is_full_trade_mode_only(trade_mode: int, expected: bool) -> None:
    rows = default_symbols()
    for row in rows:
        if row["name"] == "EURUSDm":
            row["trade_mode"] = trade_mode
    resolver, _ = make_resolver(rows=rows)
    ref = await resolver.get("EURUSDm")
    assert ref.tradable is expected
    assert ref.to_dict()["tradable"] is expected


def test_symbol_ref_construction_fails_loudly_on_a_missing_field() -> None:
    """§1.1: a missing attribute raises here, rather than becoming a silent None downstream."""
    incomplete = si(name="EURUSDm", digits=5, point=0.00001)
    with pytest.raises(AttributeError):
        SymbolRef.from_info(incomplete, SETTINGS)


# ======================================================================================
# The broker clock.
# ======================================================================================
@pytest.fixture(autouse=True)
def _reset_offset_cache():
    """`server_time_offset` memoises for five minutes in module state; isolate every test."""
    symbols_mod._offset_value = None
    symbols_mod._offset_at = 0.0
    yield
    symbols_mod._offset_value = None
    symbols_mod._offset_at = 0.0


async def test_server_time_offset_is_measured_from_a_live_tick() -> None:
    """Exness sits at UTC+3; the offset is measured, never assumed, then snapped to 900 s."""
    resolver, broker = make_resolver()
    broker.server_time_offset = 3 * 3600

    offset = await server_time_offset(resolver._rpc, resolver)

    assert offset == pytest.approx(3 * 3600.0)
    assert offset % 900.0 == 0.0


async def test_server_time_offset_snaps_to_the_nearest_quarter_hour() -> None:
    """A network round-trip and the tick's own age are noise next to a quarter hour."""
    resolver, broker = make_resolver()
    broker.server_time_offset = 2 * 3600 + 1800 + 7  # UTC+2:30, plus latency

    offset = await server_time_offset(resolver._rpc, resolver)

    assert offset == pytest.approx(9000.0)


async def test_server_time_offset_assumes_utc_when_nothing_quotes() -> None:
    """Over a weekend every symbol can be closed; the account panel must not go down with it."""
    resolver, broker = make_resolver()
    broker.zero_tick_symbols.update(broker.symbols)

    assert await server_time_offset(resolver._rpc, resolver) == pytest.approx(0.0)


async def test_broker_day_start_is_midnight_on_the_brokers_clock() -> None:
    """"Today's realized P/L" means since midnight broker-server time, not local midnight."""
    resolver, broker = make_resolver()
    broker.server_time_offset = 3 * 3600

    start = await broker_day_start_ts(resolver._rpc, resolver)

    assert start % 86400.0 == 0.0
    broker_now = time.time() + 3 * 3600
    assert 0.0 <= broker_now - start < 86400.0


# ---------------------------------------------------------------------------------------
# An undecidable pip convention must not delete the symbol
# ---------------------------------------------------------------------------------------


def _odd_digit_table():
    """A broker table quoting indices and crypto the way real brokers commonly do."""
    from tests.fake_mt5 import _symbol

    return [
        _symbol("US30", digits=1, point=0.1, bid=38950.5, description="Dow Jones 30 Index"),
        _symbol("JP225", digits=0, point=1.0, bid=39100.0, description="Nikkei 225 Index"),
        _symbol("BTCUSD", digits=0, point=1.0, bid=64500.0, description="Bitcoin vs US Dollar"),
        _symbol("EURUSDm", digits=5, point=0.00001, bid=1.08500, description="Euro vs US Dollar"),
    ]


async def test_a_symbol_with_no_standard_pip_convention_still_resolves() -> None:
    """An adversarial audit measured `resolve_symbol("US30")` answering "no symbol matches".

    `digits` outside {2,3,4,5} is ordinary — indices quoted to one decimal, JP225 and many
    BTCUSD feeds to zero — and §2's alias table mandates exactly those instruments. Dropping
    the candidate denied a symbol that was sitting in the table and fully tradable, and buried
    the real reason in a swallowed exception the model never sees.
    """
    from app.config import settings as app_settings
    from app.trading.symbols import SymbolResolver

    from tests.fake_mt5 import FakeMt5
    from tests.fake_rpc import FakeRpc

    resolver = SymbolResolver(FakeRpc(FakeMt5(symbols=_odd_digit_table())), app_settings)

    for query, expected in (("US30", "US30"), ("DOW", "US30"), ("JP225", "JP225"), ("BTC", "BTCUSD")):
        candidates = await resolver.candidates(query)
        assert candidates, f"resolve_symbol({query!r}) found nothing for a symbol that exists"
        row = candidates[0].to_dict()
        assert row["name"] == expected
        assert row["tradable"] is True, "it is tradable; the model must be told so"
        assert row["pip_convention"] == "unresolved"
        assert row["pip_size"] == 0.0
        assert "PIP_OVERRIDES" in row["pip_note"], "the remedy must reach the model, not a log"


async def test_a_pip_distance_on_such_a_symbol_refuses_with_the_remedy() -> None:
    """Failing loudly is the point: the pip size is unknown, so it must not be guessed."""
    from app.config import settings as app_settings
    from app.errors import TradingError
    from app.trading import pricing
    from app.trading.symbols import SymbolResolver

    from tests.fake_mt5 import FakeMt5
    from tests.fake_rpc import FakeRpc

    resolver = SymbolResolver(FakeRpc(FakeMt5(symbols=_odd_digit_table())), app_settings)
    ref = (await resolver.candidates("US30"))[0]

    with pytest.raises(TradingError) as excinfo:
        pricing.price_from_distance(ref, 38950.5, "below", 20, "pips")
    message = str(excinfo.value)
    assert "PIP_OVERRIDES" in message and "points" in message

    # Points and absolute prices still work — the symbol is not crippled, only its pip unit.
    result = pricing.price_from_distance(ref, 38950.5, "below", 200, "points")
    assert result["price"] == pytest.approx(38930.5, abs=1e-9)


# ---------------------------------------------------------------------------------------
# The suffix set, checked against a real broker's live symbol table
# ---------------------------------------------------------------------------------------


async def test_exness_r_suffix_resolves() -> None:
    """Measured against the live Exness-MT5Trial15 demo, which quotes XAUUSDr, not XAUUSDm.

    The suffix list originally carried "m" and not "r", so `resolve_symbol("gold")` reported
    "no symbol matches 'gold'" for an instrument sitting in `symbols_get()` — the failure is
    silent and total, and only a real broker exposes it. Exness's Raw-Spread accounts are the
    common case, so this is not an exotic table.
    """
    from app.config import settings as app_settings
    from app.trading.symbols import SymbolResolver

    from tests.fake_mt5 import FakeMt5, _symbol
    from tests.fake_rpc import FakeRpc

    rows = [
        _symbol("XAUUSDr", digits=2, point=0.01, bid=2380.55, description="Gold vs US Dollar"),
        _symbol("XAUEURr", digits=2, point=0.01, bid=2190.10, description="Gold vs Euro"),
        _symbol("BTCXAUr", digits=5, point=0.00001, bid=27.10, description="Bitcoin vs Gold"),
        _symbol("EURUSDr", digits=5, point=0.00001, bid=1.08500, description="Euro vs US Dollar"),
        _symbol("US30r", digits=1, point=0.1, bid=38950.5, description="Dow Jones 30"),
    ]
    resolver = SymbolResolver(FakeRpc(FakeMt5(symbols=rows)), app_settings)

    gold = await resolver.candidates("gold")
    assert gold, "resolve_symbol('gold') must find the broker's gold on an r-suffix table"
    assert gold[0].name == "XAUUSDr", (
        f"expected XAUUSDr first, got {[c.name for c in gold][:3]} — BTCXAUr and XAUEURr both "
        f"contain XAU, so ranking must prefer the exact alias target"
    )
    assert gold[0].pip_size == 0.10 and gold[0].pip_convention == "metal_gold_0.10"

    eur = await resolver.candidates("EURUSD")
    assert eur and eur[0].name == "EURUSDr"

    dow = await resolver.candidates("DOW")
    assert dow and dow[0].name == "US30r", "the alias table must survive the r suffix too"


async def test_a_metals_pip_does_not_depend_on_how_deep_the_broker_quotes_it() -> None:
    """The ten-times error, caught on a live broker.

    BUILD_PROMPT §2 says getting the pip wrong "scales every stop by ten", and this is exactly
    that case. The original metal branch returned `point * 10`, which is 0.10 on a 2-digit gold
    feed and 0.01 on a 3-digit one — the same instrument, a pip differing by a factor of ten.
    The live Exness demo quotes XAUUSDr at digits=3, so every gold stop would have been ten
    times tighter than the model asked for.

    A trader saying "20 pips on gold" means 2.00 in price on every feed. That is the invariant.
    """
    from types import SimpleNamespace

    from app.config import settings as app_settings
    from app.trading.symbols import pip_convention, pip_size

    from tests.fake_mt5 import _symbol

    def ref(name, digits, point, description):
        return SimpleNamespace(**_symbol(name, digits=digits, point=point, bid=4631.5,
                                         description=description))

    three = ref("XAUUSDr", 3, 0.001, "Gold vs US Dollar")     # live Exness
    two = ref("XAUUSDm", 2, 0.01, "Gold vs US Dollar")        # the classic feed

    assert pip_size(three, settings=app_settings) == 0.10
    assert pip_size(two, settings=app_settings) == 0.10
    assert pip_size(three, settings=app_settings) == pip_size(two, settings=app_settings), (
        "the same metal must have the same pip however deep the broker quotes it"
    )
    for si in (two, three):
        assert pip_convention(si, settings=app_settings) == "metal_gold_0.10"
        assert 20 * pip_size(si, settings=app_settings) == pytest.approx(2.00, abs=1e-9)

    silver = ref("XAGUSDr", 3, 0.001, "Silver vs US Dollar")
    assert pip_size(silver, settings=app_settings) == 0.01, "silver is not gold"
    assert pip_convention(silver, settings=app_settings) == "metal_silver_0.01"

    # A JPY pair at the same digit count must NOT be swept into the metal branch.
    jpy = ref("USDJPYr", 3, 0.001, "US Dollar vs Japanese Yen")
    assert pip_convention(jpy, settings=app_settings) == "fx_3digit_jpy"
    assert pip_size(jpy, settings=app_settings) == pytest.approx(0.01, abs=1e-12)


async def test_a_metal_must_be_the_base_of_the_pair() -> None:
    """Seen on the live Exness demo: `resolve_symbol("gold")` returned BTCXAUr alongside XAUUSDr.

    A substring search for "XAU" gives Bitcoin-priced-in-gold the gold pip of 0.10, on an
    instrument quoted to five digits — wrong by three orders of magnitude, and the very
    mislabel `_metal_pip`'s docstring already claimed to avoid. Position is what separates
    them: XAUUSDr starts with the metal, BTCXAUr does not.
    """
    from types import SimpleNamespace

    from app.config import settings as app_settings
    from app.trading.symbols import pip_convention, pip_size

    from tests.fake_mt5 import _symbol

    def ref(name, digits, point, description):
        return SimpleNamespace(**_symbol(name, digits=digits, point=point, bid=100.0,
                                         description=description))

    gold = ref("XAUUSDr", 3, 0.001, "Gold vs US Dollar")
    gold_aud = ref("XAUAUDr", 3, 0.001, "Gold vs Australian Dollar")
    cross = ref("BTCXAUr", 5, 0.00001, "Bitcoin vs Gold")

    assert pip_convention(gold, settings=app_settings) == "metal_gold_0.10"
    assert pip_convention(gold_aud, settings=app_settings) == "metal_gold_0.10", (
        "gold against a currency other than the dollar is still gold"
    )
    assert pip_convention(cross, settings=app_settings) == "fx_5digit", (
        "a cross that merely mentions XAU is not a metal quote"
    )
    assert pip_size(cross, settings=app_settings) == pytest.approx(0.0001, abs=1e-12)
    assert pip_size(cross, settings=app_settings) != pip_size(gold, settings=app_settings)
