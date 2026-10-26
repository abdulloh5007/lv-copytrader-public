"""The symbol picker and the optional limit it fills in — `GET /api/trading/symbols`.

This file defends one feature and one constraint that outranks it.

**The feature.** The operator should not have to type broker symbol names from memory.
``GET /api/trading/symbols`` imports the connected account's own table — names, groups,
digits, pip sizes, lot steps — so ``limits.symbol_allowlist`` is *chosen* from what the
account actually carries rather than guessed at. A real Exness demo answers with 312
symbols across 8 groups, and the route has to stay honest about that: ``total`` counts the
whole match, ``truncated`` says when the page clipped it, and the query matches across a
broker suffix so ``XAUUSD`` finds ``XAUUSDr``.

**The constraint (BUILD_PROMPT §6).** ``symbol_allowlist`` is an OPTIONAL operator limit and
it SHIPS DISABLED. **An empty list means every symbol is allowed and the limit is not
evaluated at all.** Making the list easy to fill in must not make it any easier to fill in
by accident, so the tests below pin the empty case from both ends: an empty allowlist
refuses nothing and does not even reach the broker, and a request that stores an empty list
stores an empty list. The three unconditional gates stay exactly three — ``armed``, the
stop-loss rule and the duplicate-signal guard — and nothing here is a fourth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest

from app.config import Settings
from app.db import get_setting, init_db, session_scope, set_setting
from app.trading.guards import check_optional_limits
from app.trading.symbols import SymbolResolver

from tests.fake_mt5 import FakeMt5, _symbol, default_symbols
from tests.fake_rpc import FakeRpc

SYMBOLS_URL = "/api/trading/symbols"
LIMITS_URL = "/api/settings/limits"
ALLOWLIST_KEY = "limits.symbol_allowlist"

# PIP_OVERRIDES pinned empty so the operator's own .env cannot change what these tests mean.
PINNED = Settings(PIP_OVERRIDES={})


# ======================================================================================
# Fixtures — the real app, then a fake broker swapped in behind it
# ======================================================================================


@pytest.fixture
async def client(data_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Boot the real application through its real lifespan, on this event loop.

    The app comes up with MT5 unreachable, exactly as it does in production when the
    terminal is restarting; the tests that need a broker attach one afterwards.
    """
    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            http.app = app  # the helper below reaches the Runtime through it
            yield http


def attach_broker(
    http: httpx.AsyncClient,
    *,
    symbols: list[dict[str, Any]] | None = None,
    unreachable: bool = False,
) -> FakeMt5:
    """Point the running app's Runtime at an in-process broker and return it."""
    broker = FakeMt5(symbols=symbols if symbols is not None else default_symbols())
    rpc = FakeRpc(broker)
    rpc.unreachable = unreachable
    runtime = http.app.state.runtime
    runtime.rpc = rpc
    runtime.resolver = SymbolResolver(rpc, PINNED)
    return broker


def row_named(body: dict[str, Any], name: str) -> dict[str, Any]:
    """Return one row of a symbols payload by name, failing the test when it is absent."""
    for row in body["symbols"]:
        if row["name"] == name:
            return row
    raise AssertionError(f"{name} is not in {[row['name'] for row in body['symbols']]}")


# ======================================================================================
# 1. The route reports what the broker says
# ======================================================================================


async def test_symbols_returns_the_brokers_own_rows_with_the_pip_rules_applied(
    client: httpx.AsyncClient,
) -> None:
    """Each row carries the broker's facts and the pip convention that decided its pip size.

    The picker shows ``pip_size`` because an operator choosing "gold" needs to see that this
    broker's gold moves 0.10 per pip; it shows ``pip_convention`` because a pip is trader
    slang with no broker-side definition (BUILD §2), and naming the rule that fired is what
    makes a wrong stop traceable to a rule rather than to a guess.
    """
    broker = attach_broker(client)

    body = (await client.get(SYMBOLS_URL)).json()

    assert body["ok"] is True
    assert body["mt5_unavailable"] is False
    assert body["total"] == len(broker.symbols)

    gold = row_named(body, "XAUUSDm")
    assert gold["description"] == "Gold vs US Dollar"
    assert gold["digits"] == 2
    assert gold["point"] == pytest.approx(0.01)
    assert gold["pip_size"] == pytest.approx(0.10)
    assert gold["pip_convention"] == "metal_gold_0.10"
    assert gold["tradable"] is True
    assert gold["currency_profit"] == "USD"
    # Read from the broker's table, never from a constant.
    assert gold["volume_min"] == broker.symbols["XAUUSDm"]["volume_min"]
    assert gold["volume_step"] == broker.symbols["XAUUSDm"]["volume_step"]
    assert gold["volume_max"] == broker.symbols["XAUUSDm"]["volume_max"]
    assert gold["stops_level_points"] == broker.symbols["XAUUSDm"]["trade_stops_level"]
    assert gold["spread_points"] == broker.symbols["XAUUSDm"]["spread"]

    euro = row_named(body, "EURUSDm")
    assert euro["pip_size"] == pytest.approx(0.0001)
    assert euro["pip_convention"] == "fx_5digit"
    yen = row_named(body, "USDJPYm")
    assert yen["pip_size"] == pytest.approx(0.01)
    assert yen["pip_convention"] == "fx_3digit_jpy"


async def test_symbols_carry_the_brokers_group_so_a_long_list_is_navigable(
    client: httpx.AsyncClient,
) -> None:
    """`path` is the broker's Market Watch folder, with the symbol leaf MT5 appends removed."""
    attach_broker(client)

    body = (await client.get(SYMBOLS_URL)).json()

    assert row_named(body, "XAUUSDm")["path"] == "Raw\\Forex"
    assert row_named(body, "US30m")["path"] == "Raw\\Indices"
    assert row_named(body, "BTCUSDm")["path"] == "Raw\\Crypto"
    assert row_named(body, "USOILm")["path"] == "Raw\\Energies"
    # Rows arrive grouped, which is the order the picker renders.
    paths = [row["path"] for row in body["symbols"]]
    assert paths == sorted(paths)


async def test_symbols_names_the_account_the_rows_came_from(client: httpx.AsyncClient) -> None:
    """A symbol list only means something beside the account that carries it."""
    attach_broker(client)

    body = (await client.get(SYMBOLS_URL)).json()

    assert body["account"]["login"] == 474436674
    assert body["account"]["server"] == "Exness-MT5Trial15"


# ======================================================================================
# 2. The query — including across a broker suffix
# ======================================================================================


async def test_query_matches_across_a_broker_suffix(client: httpx.AsyncClient) -> None:
    """"XAUUSD" must find "XAUUSDm": the suffix is the broker's, not the operator's.

    This is the same equivalence resolution uses. An operator who types the plain name and
    is told the symbol does not exist would go on to type it into the allowlist by hand —
    and that hand-typed name is exactly the one the guard has to match later.
    """
    attach_broker(client)

    body = (await client.get(SYMBOLS_URL, params={"query": "XAUUSD"})).json()

    assert body["ok"] is True
    assert [row["name"] for row in body["symbols"]][0] == "XAUUSDm"
    assert body["total"] >= 1

    # The case a prefix match could not rescue: the operator types the name their *other*
    # broker uses, suffix and all, and this account spells the same instrument differently.
    other = (await client.get(SYMBOLS_URL, params={"query": "XAUUSDr"})).json()
    assert [row["name"] for row in other["symbols"]][0] == "XAUUSDm"


async def test_query_matches_a_suffixed_name_against_a_plain_table(
    client: httpx.AsyncClient,
) -> None:
    """The equivalence reads backwards too: "XAUUSDm" finds a broker that quotes "XAUUSD"."""
    attach_broker(
        client,
        symbols=[
            _symbol("XAUUSD", digits=2, point=0.01, bid=2380.55, description="Gold vs US Dollar"),
            _symbol("EURUSD", digits=5, point=0.00001, bid=1.085, description="Euro vs US Dollar"),
        ],
    )

    body = (await client.get(SYMBOLS_URL, params={"query": "XAUUSDm"})).json()

    assert [row["name"] for row in body["symbols"]] == ["XAUUSD"]


async def test_query_matches_the_description_case_insensitively(
    client: httpx.AsyncClient,
) -> None:
    """"bitcoin" is not in any symbol name on this table; it is in a description."""
    attach_broker(client)

    body = (await client.get(SYMBOLS_URL, params={"query": "bitcoin"})).json()

    assert [row["name"] for row in body["symbols"]] == ["BTCUSDm"]


async def test_the_alias_table_leads_the_results_for_a_traders_word(
    client: httpx.AsyncClient,
) -> None:
    """"gold" names no symbol on this table — the alias table is what makes it lead with gold.

    Without the alias band every hit for "gold" is a description match, ties break on the
    broker's group, and the picker's first row becomes ``BTCXAUr`` — Bitcoin priced in gold,
    which is not what anyone typing "gold" meant. The picker and the model must agree on
    what the word resolves to.
    """
    attach_broker(
        client,
        symbols=[
            _symbol("BTCXAUr", digits=5, point=0.00001, bid=0.027,
                    description="Bitcoin vs Gold", group="Raw\\CryptoCross"),
            _symbol("XAUUSDr", digits=3, point=0.001, bid=2380.5,
                    description="Gold vs US Dollar", contract_size=100.0),
            _symbol("XAUEURr", digits=3, point=0.001, bid=2100.0, description="Gold vs Euro",
                    contract_size=100.0),
        ],
    )

    body = (await client.get(SYMBOLS_URL, params={"query": "gold"})).json()

    names = [row["name"] for row in body["symbols"]]
    assert names[0] == "XAUUSDr", names
    assert set(names) == {"XAUUSDr", "XAUEURr", "BTCXAUr"}


async def test_query_matches_the_brokers_group_last(client: httpx.AsyncClient) -> None:
    """"indices" names no symbol and no description on this table — it names a folder.

    It ranks below every name and description hit, so asking for a whole asset class can
    never displace the symbol the operator actually typed.
    """
    attach_broker(client)

    body = (await client.get(SYMBOLS_URL, params={"query": "indices"})).json()

    assert {row["name"] for row in body["symbols"]} == {"US30m", "USTECm"}

    # And it stays last: "US" hits two names, one description and one group here.
    mixed = (await client.get(SYMBOLS_URL, params={"query": "US30"})).json()
    assert [row["name"] for row in mixed["symbols"]][0] == "US30m"


async def test_query_ranks_an_exact_name_above_a_substring(client: httpx.AsyncClient) -> None:
    """Ranking is what makes truncation safe: the relevant end of the list survives a clip."""
    attach_broker(
        client,
        symbols=[
            _symbol("EURUSDm", digits=5, point=0.00001, bid=1.085, description="Euro vs US Dollar"),
            _symbol("XEURUSDm", digits=5, point=0.00001, bid=1.085, description="Euro basket"),
            _symbol("EURUSDm.pro", digits=5, point=0.00001, bid=1.085, description="Euro vs USD"),
        ],
    )

    body = (await client.get(SYMBOLS_URL, params={"query": "EURUSD"})).json()

    names = [row["name"] for row in body["symbols"]]
    assert names[0] == "EURUSDm", names
    assert set(names) == {"EURUSDm", "XEURUSDm", "EURUSDm.pro"}


async def test_query_that_matches_nothing_is_an_empty_list_not_an_error(
    client: httpx.AsyncClient,
) -> None:
    """A search with no hits is a fact about the account, not a failure of the route."""
    attach_broker(client)

    response = await client.get(SYMBOLS_URL, params={"query": "NOSUCHTHING"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["symbols"] == []
    assert body["total"] == 0
    assert body["truncated"] is False
    assert body["mt5_unavailable"] is False


async def test_only_tradable_filters_on_what_the_broker_currently_allows(
    client: httpx.AsyncClient,
) -> None:
    """`only_tradable` is opt-in: a close-only symbol is visible by default, and says so.

    Hiding it by default would leave the operator unable to see why a symbol they know the
    account carries is missing from the picker.
    """
    attach_broker(
        client,
        symbols=[
            _symbol("EURUSDm", digits=5, point=0.00001, bid=1.085, description="Euro vs US Dollar"),
            # trade_mode 3 is SYMBOL_TRADE_MODE_CLOSEONLY: not fully tradable.
            _symbol("OLDSTOCKr", digits=2, point=0.01, bid=10.0, description="Delisted",
                    trade_mode=3),
        ],
    )

    everything = (await client.get(SYMBOLS_URL)).json()
    assert {row["name"] for row in everything["symbols"]} == {"EURUSDm", "OLDSTOCKr"}
    assert row_named(everything, "OLDSTOCKr")["tradable"] is False

    tradable = (await client.get(SYMBOLS_URL, params={"only_tradable": "true"})).json()
    assert [row["name"] for row in tradable["symbols"]] == ["EURUSDm"]
    assert tradable["total"] == 1


# ======================================================================================
# 3. Truncation reports itself
# ======================================================================================


async def test_truncation_reports_itself_rather_than_hiding_the_rest(
    client: httpx.AsyncClient,
) -> None:
    """A clipped page says so, counts the whole match, and says how to reach the rest.

    A real Exness account carries well over a thousand symbols. Returning the first N and
    calling it the list would tell the operator an instrument does not exist when it simply
    did not fit — and they would then stop looking.
    """
    broker = attach_broker(client)
    total = len(broker.symbols)

    body = (await client.get(SYMBOLS_URL, params={"limit": 3})).json()

    assert len(body["symbols"]) == 3
    assert body["total"] == total
    assert body["truncated"] is True
    assert str(total) in body["message"]

    whole = (await client.get(SYMBOLS_URL, params={"limit": total})).json()
    assert len(whole["symbols"]) == total
    assert whole["total"] == total
    assert whole["truncated"] is False


async def test_a_page_that_fits_is_never_flagged_truncated(client: httpx.AsyncClient) -> None:
    """`truncated` describes the clip, not the page size."""
    attach_broker(client)

    body = (await client.get(SYMBOLS_URL, params={"limit": 1000})).json()

    assert body["truncated"] is False
    assert len(body["symbols"]) == body["total"]


async def test_limit_is_capped_so_one_request_cannot_ask_for_everything(
    client: httpx.AsyncClient,
) -> None:
    """The hard cap is 1000 and the schema enforces it before any work happens."""
    attach_broker(client)

    assert (await client.get(SYMBOLS_URL, params={"limit": 1000})).status_code == 200
    assert (await client.get(SYMBOLS_URL, params={"limit": 1001})).status_code == 422
    assert (await client.get(SYMBOLS_URL, params={"limit": 0})).status_code == 422


# ======================================================================================
# 4. MT5 unreachable degrades to 200
# ======================================================================================


async def test_unreachable_mt5_degrades_to_200_with_the_flag_set(
    client: httpx.AsyncClient,
) -> None:
    """Settings is where a broken terminal gets fixed, so this route must never 500."""
    attach_broker(client, unreachable=True)

    response = await client.get(SYMBOLS_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["mt5_unavailable"] is True
    assert body["symbols"] == []
    assert body["total"] == 0
    assert body["truncated"] is False
    assert "MetaTrader 5 is not answering" in body["message"]
    # And it says the saved limit was not touched, because it was not.
    assert "unchanged" in body["message"]


async def test_a_cached_table_served_while_mt5_is_down_says_it_is_the_last_one(
    client: httpx.AsyncClient,
) -> None:
    """Rows out of the resolver's cache must not be presented as a live read.

    The symbol table is cached for ``SYMBOL_CACHE_SECONDS``, so a terminal that dies after a
    successful read leaves the picker able to answer — and with no way for the operator to
    tell a current table from the one that was current before MT5 went away. The message
    says which it is, and the account falls back to the configured one.
    """
    broker = attach_broker(client)

    warm = (await client.get(SYMBOLS_URL)).json()
    assert warm["total"] == len(broker.symbols)
    assert warm["account"]["login"] == 474436674

    client.app.state.runtime.rpc.unreachable = True

    body = (await client.get(SYMBOLS_URL)).json()

    assert body["ok"] is True, "a warm cache still answers"
    assert body["total"] == len(broker.symbols)
    assert "MetaTrader 5 is not answering" in body["message"]
    assert "last symbol table" in body["message"]
    # The account is still named — from the stored settings or the .env seed, not invented.
    assert body["account"]["server"]


# ======================================================================================
# 5. The limit itself — PUT /api/settings/limits round-trips the names
# ======================================================================================


async def test_allowlist_round_trips_upper_cased_and_de_duplicated(
    client: httpx.AsyncClient,
) -> None:
    """What the picker sends and what a hand-typed list sends must store identically."""
    payload = {
        "max_volume_per_trade": None,
        "max_open_positions": None,
        "max_daily_loss_percent": None,
        "symbol_allowlist": [" xauusdm ", "XAUUSDm", "eurusdm", "XAUUSDM"],
        "trading_hours": [],
    }

    response = await client.put(LIMITS_URL, json=payload)

    assert response.status_code == 200, response.text
    stored = response.json()["limits"]["symbol_allowlist"]
    assert stored == ["XAUUSDM", "EURUSDM"], "upper-cased, de-duplicated, order kept"

    # It survives a re-read, and it is what the guard will compare against.
    again = (await client.get("/api/settings")).json()["limits"]["symbol_allowlist"]
    assert again == ["XAUUSDM", "EURUSDM"]
    async with session_scope() as session:
        assert await get_setting(session, ALLOWLIST_KEY, None) == ["XAUUSDM", "EURUSDM"]


async def test_an_empty_allowlist_stores_empty_and_stays_the_shipped_default(
    client: httpx.AsyncClient,
) -> None:
    """Nothing may turn "no entries" into an entry — empty is the shipped, disabled state."""
    fresh = (await client.get("/api/settings")).json()["limits"]
    assert fresh["symbol_allowlist"] == [], "the limit ships empty"

    filled = await client.put(
        LIMITS_URL,
        json={
            "max_volume_per_trade": None,
            "max_open_positions": None,
            "max_daily_loss_percent": None,
            "symbol_allowlist": ["XAUUSDM"],
            "trading_hours": [],
        },
    )
    assert filled.json()["limits"]["symbol_allowlist"] == ["XAUUSDM"]

    cleared = await client.put(
        LIMITS_URL,
        json={
            "max_volume_per_trade": None,
            "max_open_positions": None,
            "max_daily_loss_percent": None,
            "symbol_allowlist": [],
            "trading_hours": [],
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["limits"]["symbol_allowlist"] == []
    async with session_scope() as session:
        assert await get_setting(session, ALLOWLIST_KEY, None) == []


async def test_a_name_picked_from_the_route_is_accepted_by_the_limit(
    client: httpx.AsyncClient,
) -> None:
    """The whole point of the feature: pick a name, store it, and the guard permits it."""
    attach_broker(client)

    picked = row_named((await client.get(SYMBOLS_URL, params={"query": "gold"})).json(), "XAUUSDm")
    response = await client.put(
        LIMITS_URL,
        json={
            "max_volume_per_trade": None,
            "max_open_positions": None,
            "max_daily_loss_percent": None,
            "symbol_allowlist": [picked["name"]],
            "trading_hours": [],
        },
    )
    assert response.status_code == 200, response.text

    runtime = client.app.state.runtime
    async with session_scope() as session:
        refusal = await check_optional_limits(
            session,
            symbol=picked["name"],
            volume=0.01,
            rpc=runtime.rpc,
            resolver=runtime.resolver,
            now_ts=0.0,
        )
    assert refusal is None


# ======================================================================================
# 6. The guard itself — empty means "not evaluated at all" (BUILD §6)
# ======================================================================================


async def _refusal(symbol: str, *, allowlist: list[str] | None) -> tuple[str | None, FakeMt5]:
    """Store an allowlist (or nothing at all) and evaluate the optional limits once."""
    await init_db()
    broker = FakeMt5()
    rpc = FakeRpc(broker)
    resolver = SymbolResolver(rpc, PINNED)
    async with session_scope() as session:
        if allowlist is not None:
            await set_setting(session, ALLOWLIST_KEY, allowlist)
        refusal = await check_optional_limits(
            session, symbol=symbol, volume=0.10, rpc=rpc, resolver=resolver, now_ts=0.0
        )
    return refusal, broker


async def test_an_empty_allowlist_allows_every_symbol(data_dir: Path) -> None:
    """The shipped state. Empty is not "allow nothing" and not "allow a default set"."""
    for symbol in ("XAUUSDm", "EURUSDm", "BTCUSDm", "SOMETHING_NOBODY_LISTED"):
        refusal, _ = await _refusal(symbol, allowlist=[])
        assert refusal is None, f"an empty allowlist must not refuse {symbol}"


async def test_an_unset_allowlist_allows_every_symbol_and_costs_no_rpc(
    data_dir: Path,
) -> None:
    """A limit that was never written is not evaluated at all — not even read defensively."""
    refusal, broker = await _refusal("XAUUSDm", allowlist=None)

    assert refusal is None
    assert broker.calls == [], "an unset limit must not spend a broker round trip"


async def test_a_populated_allowlist_refuses_a_symbol_outside_it_by_name(
    data_dir: Path,
) -> None:
    """The refusal names the limit, its value and the symbol, so the model can report it."""
    refusal, _ = await _refusal("EURUSDm", allowlist=["XAUUSDM"])

    assert refusal is not None
    assert "symbol_allowlist" in refusal
    assert "EURUSDm" in refusal
    assert "XAUUSDM" in refusal


async def test_a_populated_allowlist_still_permits_the_suffix_variant(data_dir: Path) -> None:
    """"XAUUSD" on the list permits "XAUUSDm": the suffix belongs to the broker, not the rule.

    An operator who wrote the plain name meant the instrument, and the account quotes it with
    a suffix. Matching only the literal string would refuse the very trade the list allows.
    """
    refusal, _ = await _refusal("XAUUSDm", allowlist=["XAUUSD"])
    assert refusal is None

    # And backwards: a suffixed entry permits the plain name.
    refusal, _ = await _refusal("XAUUSD", allowlist=["XAUUSDR"])
    assert refusal is None

    # The equivalence is not a free pass — a different instrument is still refused.
    refusal, _ = await _refusal("XAGUSDm", allowlist=["XAUUSD"])
    assert refusal is not None
    assert "symbol_allowlist" in refusal
