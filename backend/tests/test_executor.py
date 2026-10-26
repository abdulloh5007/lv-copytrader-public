"""The executor driven against a fake broker, on the paths where money actually moves.

Every test here is an acceptance criterion from BUILD_PROMPT, and each one is written to
fail loudly if the specific trap it guards ever reopens:

  crit 5  a pending order succeeds at retcode 10008, not 10009, and is recorded once
  crit 6  multi-TP splits on hedging and reports unmanaged targets on netting
  crit 7  a missing stop is an incomplete entry; removing an existing stop is still refused
  crit 12 disarmed means nothing reaches the broker -- but the kill switch still fires
  §1.3    a close always carries `position`; order_send may return None; 10019 is data

The broker is ``tests.fake_mt5.FakeMt5`` (stateful, reproduces the retcode and filling-mode
semantics) reached through ``tests.fake_rpc.FakeRpc`` (satisfies the Mt5Rpc contract). No
Wine, no network, no MetaTrader5 package.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app import db as app_db
from app.config import settings as app_settings
from app.db import init_db, session_scope, set_setting
from app.events import EventBus
from app.models import LadderOutcome, Source, Trade
from app.runtime import Runtime
from app.trading.executor import Executor, RunContext
from app.trading.symbols import SymbolResolver

from tests.fake_mt5 import (
    ORDER_TYPE_BUY,
    POSITION_TYPE_BUY,
    TRADE_ACTION_DEAL,
    FakeMt5,
)
from tests.fake_rpc import FakeRpc

SIGNAL_REF = "SIG:1:4242"


class _NullNotifier:
    """Swallows notifications; a notification must never affect a trade's outcome.

    ``__getattr__`` covers every ``render_*`` builder the executor reaches for, so a new
    notification type cannot turn into a spurious failure in a trading test.
    """

    def fire(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        if name.startswith("render_"):
            return lambda *a, **k: ""
        raise AttributeError(name)


async def _build(data_dir: Path, *, margin_mode: int = 2, armed: bool = True,
                 balance: float = 3000.0) -> tuple[Executor, FakeMt5, Runtime]:
    """Assemble a real Executor over a fake broker, with the DB schema created."""
    await init_db()
    broker = FakeMt5(margin_mode=margin_mode, balance=balance)
    rpc = FakeRpc(broker)
    runtime = Runtime(settings=app_settings, rpc=rpc, resolver=None, bus=EventBus())
    runtime.resolver = SymbolResolver(rpc, app_settings)
    runtime.executor = Executor(runtime)
    runtime.notifier = _NullNotifier()
    async with session_scope() as session:
        await set_setting(session, "trading.armed", armed)
    return runtime.executor, broker, runtime


def _ctx(**kw: Any) -> RunContext:
    base = dict(run_id="a1b2c3d4e5f6", signal_ref=SIGNAL_REF, source_id=None,
                message_row_id=None, origin="agent")
    base.update(kw)
    return RunContext(**base)


async def _trades() -> list[Trade]:
    async with session_scope() as session:
        rows = await session.execute(select(Trade))
        return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Criterion 7 -- the one hard trading rule, closing in both directions
# ---------------------------------------------------------------------------


async def test_open_market_without_sl_opens_the_ladder_size(data_dir: Path) -> None:
    """An incomplete teaser opens now. The stop arrives later; nothing is invented.

    Hedging with the default 0.10 ladder size is two 0.05 legs. risk_percent and an
    explicit lot are ignored. A second open of the same signal_ref is still refused.
    """
    ex, broker, _ = await _build(data_dir)
    result = await ex.open_market(
        _ctx(),
        symbol="XAUUSDm",
        side="buy",
        sl=0.0,
        tp=[],
        volume=0.01,
        risk_percent=1.5,
        reason="teaser, no stop yet",
    )
    assert result["ok"] is True, result["message"]
    assert result["kind"] == "incomplete"
    assert result["volume_source"] == "ladder_volume"
    assert result["sl"] == 0.0
    assert result["tp"] == []
    assert result["risk_percent"] is None
    assert "ignored" in result["message"].lower()
    assert result["runner_mode"] == "be_after_cover"

    positions = list(broker.positions.values())
    assert len(positions) == 2
    assert sorted(p["volume"] for p in positions) == pytest.approx([0.05, 0.05])
    assert all(p["sl"] == 0.0 and p["tp"] == 0.0 for p in positions)

    roles = sorted(leg["role"] for leg in result["legs"])
    assert roles == ["cover", "runner"]
    rows = [t for t in await _trades() if t.kind == "position"]
    assert sorted(t.ladder_role for t in rows) == ["cover", "runner"]
    assert all(t.sl == 0.0 and t.tp == 0.0 for t in rows)
    assert {t.signal_ref for t in rows} == {SIGNAL_REF}

    again = await ex.open_market(
        _ctx(), symbol="XAUUSDm", side="buy", reason="same signal again"
    )
    assert again["ok"] is False
    assert len(broker.positions) == 2, "the duplicate signal_ref guard still blocks a second open"


async def test_modify_position_cannot_remove_the_stop(data_dir: Path) -> None:
    """§6.2: the back door. Setting a stop then clearing it a second later must be refused."""
    ex, broker, _ = await _build(data_dir)
    opened = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[], volume=0.10, reason="open with a stop")
    assert opened["ok"] is True, opened["message"]
    ticket = opened["ticket"] or opened["tickets"][0]
    assert broker.positions[ticket]["sl"] > 0

    refused = await ex.modify_position(_ctx(), ticket=ticket, sl=0.0, reason="clear the stop")
    assert refused["ok"] is False
    assert "cannot be removed" in refused["message"].lower()
    assert broker.positions[ticket]["sl"] > 0, "the stop is still on the position"


async def test_sl_on_the_wrong_side_is_refused_naming_the_correct_side(data_dir: Path) -> None:
    """§6.3: a buy's stop below entry. The refusal has to say which side is right."""
    ex, broker, _ = await _build(data_dir)
    result = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2400.00,
                                 tp=[], volume=0.10, reason="stop above a buy entry")
    assert result["ok"] is False
    assert "below" in result["message"].lower()
    assert broker.positions == {}


# ---------------------------------------------------------------------------
# Criterion 5 -- a pending order succeeds at 10008
# ---------------------------------------------------------------------------


async def test_pending_order_is_ok_at_retcode_10008_and_recorded_once(data_dir: Path) -> None:
    """Checking `retcode == 10009` would call this a failure and place it twice."""
    ex, broker, _ = await _build(data_dir)
    result = await ex.place_pending(_ctx(), symbol="XAUUSDm", side="buy", order_type="limit",
                                    price=2360.00, sl=2350.00, tp=[], volume=0.10,
                                    reason="buy the dip")
    assert result["ok"] is True, result["message"]
    assert result["retcode"] == 10008
    assert result["retcode_name"] == "PLACED"
    assert len(broker.orders) == 1

    rows = [t for t in await _trades() if t.kind == "order"]
    assert len(rows) == 1, "a pending order appears exactly once in the orders table"
    assert rows[0].signal_ref == SIGNAL_REF


# ---------------------------------------------------------------------------
# Criterion 6 -- multi-TP is first class, on both margin modes
# ---------------------------------------------------------------------------


async def test_three_tps_on_hedging_split_into_three_positions(data_dir: Path) -> None:
    """Volumes sum exactly to the whole, every part >= volume_min, one signal_ref."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    result = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[2390.0, 2400.0, 2410.0], volume=0.30,
                                 reason="three targets")
    assert result["ok"] is True, result["message"]

    positions = list(broker.positions.values())
    assert len(positions) == 3, "hedging splits one signal into one position per target"
    assert sum(p["volume"] for p in positions) == pytest.approx(0.30, abs=1e-9)

    si = broker.symbols["XAUUSDm"]
    assert all(p["volume"] >= si["volume_min"] - 1e-9 for p in positions)
    assert sorted(round(p["tp"], 2) for p in positions) == [2390.0, 2400.0, 2410.0]
    assert all(p["sl"] > 0 for p in positions), "every leg carries the stop"
    assert len({p["magic"] for p in positions}) == 1

    rows = [t for t in await _trades() if t.kind == "position"]
    assert len(rows) == 3
    assert {t.signal_ref for t in rows} == {SIGNAL_REF}, "all legs share one signal_ref"


async def test_three_tps_on_netting_opens_one_and_names_the_unmanaged_targets(
    data_dir: Path,
) -> None:
    """Silently keeping tp[0] and discarding the rest is the failure this prevents."""
    ex, broker, _ = await _build(data_dir, margin_mode=0)
    result = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[2390.0, 2400.0, 2410.0], volume=0.30,
                                 reason="three targets on netting")
    assert result["ok"] is True, result["message"]
    assert len(broker.positions) == 1, "netting merges; one position only"

    message = result["message"]
    assert "2400" in message and "2410" in message, (
        "the tool result must NAME the targets it is not managing, not drop them silently"
    )
    rows = [t for t in await _trades() if t.kind == "position"]
    assert len(rows) == 1
    assert rows[0].unmanaged_tps, "the extra targets are persisted on the trade row"


async def test_split_reduces_when_the_volume_cannot_fund_every_leg(data_dir: Path) -> None:
    """0.02 lots cannot make three legs of 0.01; reduce the count and say so."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    result = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[2390.0, 2400.0, 2410.0], volume=0.02,
                                 reason="tiny size, three targets")
    assert result["ok"] is True, result["message"]
    assert len(broker.positions) == 2
    assert sum(p["volume"] for p in broker.positions.values()) == pytest.approx(0.02, abs=1e-9)


# ---------------------------------------------------------------------------
# Criterion 12 -- disarmed sends nothing, but the kill switch still fires
# ---------------------------------------------------------------------------


async def test_disarmed_refuses_the_write_and_the_broker_is_untouched(data_dir: Path) -> None:
    """The reasoning still runs; `positions_get()` is unchanged."""
    ex, broker, _ = await _build(data_dir, armed=False)
    before = dict(broker.positions)
    result = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[], volume=0.10, reason="valid signal while disarmed")
    assert result["ok"] is False
    assert "disarmed" in result["message"].lower()
    assert broker.positions == before, "nothing reached the broker"
    assert [t for t in await _trades()] == [], "and nothing was recorded as a trade"


async def test_kill_switch_closes_while_disarmed_and_spares_foreign_magics(
    data_dir: Path,
) -> None:
    """Disarming first would make its own closes refuse and the kill switch do nothing."""
    ex, broker, runtime = await _build(data_dir, armed=True)
    opened = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[], volume=0.10, reason="ours")
    assert opened["ok"] is True, opened["message"]

    # A position belonging to someone else: different magic, no SP: comment.
    foreign = broker.next_ticket()
    broker.positions[foreign] = {
        "ticket": foreign, "symbol": "EURUSDm", "type": POSITION_TYPE_BUY, "volume": 0.50,
        "price_open": 1.08500, "sl": 1.08000, "tp": 0.0, "price_current": 1.08500,
        "profit": 0.0, "swap": 0.0, "magic": 999999, "comment": "someone else's robot",
        "time": 0, "time_msc": 0, "identifier": foreign, "reason": 0,
    }

    await runtime.set_armed(False)
    result = await ex.kill_switch(reason="operator pressed the switch")

    assert result["ok"] is True, result
    assert result["closed"] >= 1, "it closed our position even though we were disarmed"
    assert foreign in broker.positions, "a foreign magic is never closed from here"
    assert await runtime.armed() is False, "and it leaves the system disarmed"


# ---------------------------------------------------------------------------
# §1.3 -- the order-semantics traps
# ---------------------------------------------------------------------------


async def test_close_request_always_carries_the_position_ticket(data_dir: Path) -> None:
    """Omit `position` and the close silently OPENS an opposite position instead."""
    ex, broker, _ = await _build(data_dir)
    opened = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[], volume=0.10, reason="to be closed")
    ticket = opened["ticket"] or opened["tickets"][0]

    closed = await ex.close_position(_ctx(), ticket=ticket, reason="take it off")
    assert closed["ok"] is True, closed["message"]
    assert broker.positions == {}, "the position is gone, not doubled"

    sends = [p["request"] for m, p in broker.calls
             if m == "order_send" and p["request"].get("action") == TRADE_ACTION_DEAL
             and p["request"].get("type") != ORDER_TYPE_BUY]
    assert sends, "a closing DEAL was sent"
    assert all(r.get("position") for r in sends), (
        "every closing request carries `position`; without it MT5 opens an opposite trade"
    )


async def test_partial_close_leaves_the_remainder_open(data_dir: Path) -> None:
    """Partial close is the same request with a smaller volume; FOK forbids partial fills."""
    ex, broker, _ = await _build(data_dir)
    opened = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[], volume=0.10, reason="half out later")
    ticket = opened["ticket"] or opened["tickets"][0]

    result = await ex.close_position(_ctx(), ticket=ticket, volume=0.04, reason="bank some")
    assert result["ok"] is True, result["message"]
    assert broker.positions[ticket]["volume"] == pytest.approx(0.06, abs=1e-9)


async def test_order_send_returning_none_is_handled_not_crashed(data_dir: Path) -> None:
    """On a pre-server failure the package returns None, not a result object."""
    ex, broker, _ = await _build(data_dir)
    broker.fail_next_send = "transport"
    result = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[], volume=0.10, reason="doomed send")
    assert result["ok"] is False
    assert result["message"], "it reports a reason rather than raising AttributeError"
    assert broker.positions == {}


async def test_not_enough_money_comes_back_as_readable_data(data_dir: Path) -> None:
    """10019 is a message the model can retry smaller on, never an exception."""
    ex, broker, _ = await _build(data_dir, balance=50.0)
    result = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[], volume=100.0, reason="far too big")
    assert result["ok"] is False
    assert result["message"]
    assert broker.positions == {}


async def test_duplicate_signal_guard_blocks_a_second_position(data_dir: Path) -> None:
    """§5.5: the guard is unconditional -- a correctness rule, not a risk limit."""
    ex, broker, _ = await _build(data_dir)
    first = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                tp=[], volume=0.10, reason="first")
    assert first["ok"] is True, first["message"]

    second = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[], volume=0.10, reason="same signal again")
    assert second["ok"] is False
    assert len(broker.positions) == 1, "the same signal_ref never opens twice"


async def test_the_executor_owns_the_comment_field(data_dir: Path) -> None:
    """§8 recovers run_id from `SP:xxxxxxxx`; a model-written comment would destroy it."""
    ex, broker, _ = await _build(data_dir)
    ctx = _ctx(run_id="deadbeefcafe0001")
    opened = await ex.open_market(ctx, symbol="XAUUSDm", side="buy", sl=2370.00,
                                 tp=[], volume=0.10, reason="tagged")
    ticket = opened["ticket"] or opened["tickets"][0]
    assert broker.positions[ticket]["comment"] == "SP:deadbeef"


# ---------------------------------------------------------------------------
# Criterion 5, second clause -- the Orders table itself
# ---------------------------------------------------------------------------


async def test_a_pending_order_appears_exactly_once_in_get_orders(data_dir: Path) -> None:
    """The live Orders path, which no test could reach until the double carried price_current.

    An adversarial audit found `get_orders()` raising AttributeError against the fake, so the
    whole Orders path — criterion 5's second clause, `/api/trading/orders`, and the order branch
    of reconcile — sat behind 262 green tests without ever being executed.
    """
    ex, broker, _ = await _build(data_dir)
    placed = await ex.place_pending(_ctx(), symbol="XAUUSDm", side="buy", order_type="limit",
                                    price=2360.00, sl=2350.00, tp=[], volume=0.10,
                                    reason="buy the dip")
    assert placed["ok"] is True, placed["message"]

    listing = await ex.get_orders()
    rows = listing["orders"]
    assert len(rows) == 1, f"a pending order must appear exactly once, got {len(rows)}"

    row = rows[0]
    assert row["symbol"] == "XAUUSDm"
    assert row["signal_ref"] == SIGNAL_REF, "the row links back to the signal that created it"
    assert row.get("external") is not True, "our own magic is not an external order"
    assert float(row["sl"]) > 0.0

    # And it is genuinely the broker's view, not a DB echo: cancel it and it goes away.
    cancelled = await ex.cancel_order(_ctx(), ticket=row["ticket"], reason="changed my mind")
    assert cancelled["ok"] is True, cancelled["message"]
    assert (await ex.get_orders())["orders"] == []


async def test_get_positions_marks_a_foreign_magic_external(data_dir: Path) -> None:
    """The read path must label what the kill switch is forbidden to touch."""
    ex, broker, _ = await _build(data_dir)
    await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370.00, tp=[],
                         volume=0.10, reason="ours")

    foreign = broker.next_ticket()
    broker.positions[foreign] = {
        "ticket": foreign, "symbol": "EURUSDm", "type": POSITION_TYPE_BUY, "volume": 0.50,
        "price_open": 1.08500, "sl": 1.08000, "tp": 0.0, "price_current": 1.08500,
        "profit": 0.0, "swap": 0.0, "magic": 999999, "comment": "another robot",
        "time": 0, "time_msc": 0, "identifier": foreign, "reason": 0,
    }

    rows = (await ex.get_positions())["positions"]
    by_ticket = {r["ticket"]: r for r in rows}
    assert by_ticket[foreign]["external"] is True, "a foreign magic is shown read-only"
    ours = [r for r in rows if r["ticket"] != foreign][0]
    assert ours["external"] is False and ours["signal_ref"] == SIGNAL_REF


# ---------------------------------------------------------------------------
# Multi-TP ladders — two 0.05 legs, 1R cover, far runner, no risk_percent
# ---------------------------------------------------------------------------


_LADDER_TPS = [2383.0, 2386.0, 2389.0, 2392.0, 2395.0, 2398.0, 2401.0, 2411.0]


async def test_ladder_opens_two_fixed_lot_legs_not_one_per_rung(data_dir: Path) -> None:
    """8 channel TPs become cover 1R + runner TP4 (N=7 bias 0), 0.05+0.05, never the first rung."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    si = broker.symbols["XAUUSDm"]
    entry = float(si["ask"])
    stop = 2367.0
    result = await ex.open_market(
        _ctx(),
        symbol="XAUUSDm",
        side="buy",
        sl=stop,
        tp=list(_LADDER_TPS),
        risk_percent=2.0,
        reason="XAUUSD ladder",
    )
    assert result["ok"] is True, result["message"]
    assert result["kind"] == "ladder"
    assert result["volume_source"] == "ladder_volume"
    assert result["risk_percent"] is None
    positions = list(broker.positions.values())
    assert len(positions) == 2, "a ladder must not open one ticket per celebration rung"
    volumes = sorted(p["volume"] for p in positions)
    assert volumes == [pytest.approx(0.05), pytest.approx(0.05)]
    assert sum(p["volume"] for p in positions) == pytest.approx(0.10)

    # Prefer the tool result: the server names the two legs.
    roles = {leg["role"]: leg for leg in result["legs"]}
    assert "cover" in roles and "runner" in roles
    assert roles["cover"]["tp"] == pytest.approx(entry + (entry - stop), abs=0.02)
    assert roles["cover"]["tp"] != pytest.approx(_LADDER_TPS[0])
    assert roles["runner"]["tp"] == pytest.approx(2395.0)
    tps = sorted(round(p["tp"], 2) for p in positions)
    assert _LADDER_TPS[0] not in tps, "channel TP1 must not be a broker target"
    assert 2395.0 in tps
    assert result["suggested_runner_tp_index"] == 5
    assert result["chosen_runner_tp_index"] == 5
    assert result["runner_tp_bias"] == 0
    assert "ignored" in result["message"].lower()


async def test_ladder_ignores_explicit_min_lot_and_still_opens_0_10(data_dir: Path) -> None:
    """Passing volume=0.01 on a ladder must not silently open a 0.01 ticket on TP1."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    result = await ex.open_market(
        _ctx(),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        volume=0.01,
        reason="tiny lot on a ladder",
    )
    assert result["ok"] is True, result["message"]
    assert len(broker.positions) == 2
    assert sum(p["volume"] for p in broker.positions.values()) == pytest.approx(0.10)


async def test_ladder_margin_refusal_does_not_fall_back_to_min_lot(data_dir: Path) -> None:
    """Not enough margin for 0.10 lots is a refusal, not a 0.01 substitute."""
    ex, broker, _ = await _build(data_dir, margin_mode=2, balance=50.0)
    result = await ex.open_market(
        _ctx(),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        reason="ladder with no margin",
    )
    assert result["ok"] is False
    assert "ladder" in result["message"].lower()
    assert "0.01" in result["message"] or "tiny" in result["message"].lower()
    assert broker.positions == {}


async def test_classic_three_tps_still_split_one_per_target(data_dir: Path) -> None:
    """1–3 TP signals keep the existing hedging split; they are not ladders."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    result = await ex.open_market(
        _ctx(),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2390.0, 2400.0, 2410.0],
        volume=0.30,
        reason="classic three targets",
    )
    assert result["ok"] is True, result["message"]
    assert result.get("kind") == "standard"
    assert len(broker.positions) == 3


async def test_ladder_runner_moves_to_breakeven_after_cover_tp(data_dir: Path) -> None:
    """When the 1R cover ticket is gone, the remaining runner SL locks to BE."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    opened = await ex.open_market(
        _ctx(),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        reason="ladder then trail",
    )
    assert opened["ok"] is True, opened["message"]
    roles = {leg["role"]: leg for leg in opened["legs"]}
    cover_ticket = int(roles["cover"]["ticket"])
    runner_ticket = int(roles["runner"]["ticket"])
    cover_entry = float(broker.positions[cover_ticket]["price_open"])
    # Cover "hits TP": move price into profit but keep it below the runner TP (2395),
    # otherwise modify_position would see the runner target on the wrong side of the
    # market. 2390 is above the BE lock (~entry) so the new stop is not an instant close.
    broker.move("XAUUSDm", 2390.0)
    del broker.positions[cover_ticket]

    trailed = await ex.trail_ladder_runners()
    assert runner_ticket in trailed["trailed"], trailed
    runner = broker.positions[runner_ticket]
    assert runner["sl"] >= cover_entry - 1e-9
    assert runner["sl"] > 2367.0


async def test_ladder_ignores_account_default_risk_percent(data_dir: Path) -> None:
    """A huge default_risk_percent must not silently size a ladder via 2%/10% math."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    async with session_scope() as session:
        await set_setting(session, "trading.default_risk_percent", 50.0)
    result = await ex.open_market(
        _ctx(),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        reason="ladder with a huge default risk",
    )
    assert result["ok"] is True, result["message"]
    assert result["volume_source"] == "ladder_volume"
    assert result["risk_percent"] is None
    assert sum(p["volume"] for p in broker.positions.values()) == pytest.approx(0.10)


async def test_ladder_volume_setting_is_split_in_half(data_dir: Path) -> None:
    """trading.ladder_volume is the operator knob; 0.20 becomes 0.10 + 0.10."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    async with session_scope() as session:
        await set_setting(session, "trading.ladder_volume", 0.20)
    result = await ex.open_market(
        _ctx(),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        reason="custom ladder lots",
    )
    assert result["ok"] is True, result["message"]
    volumes = sorted(p["volume"] for p in broker.positions.values())
    assert volumes == [pytest.approx(0.10), pytest.approx(0.10)]
    assert result["volume"] == pytest.approx(0.20)


async def test_evaluate_entry_returns_the_fixed_ladder_plan(data_dir: Path) -> None:
    ex, _, _ = await _build(data_dir)
    result = await ex.evaluate_entry(
        "XAUUSDm",
        "buy",
        2380.55,
        2367.0,
        tp=list(_LADDER_TPS),
    )
    assert result["ok"] is True
    assert result["ladder"] is True
    assert result["ladder_volume"] == pytest.approx(0.10)
    assert result["ladder_plan"]["cover"]["volume"] == pytest.approx(0.05)
    assert result["ladder_plan"]["runner"]["tp"] == pytest.approx(2395.0)
    assert result["suggested_runner_tp_index"] == 5
    assert result["suggested_runner_tp_bias"] == 0
    assert "Suggested runner TP: 5/7" in result["assessment"]
    assert "do NOT pass risk_percent" in result["assessment"]
    assert "risk the size would be" not in result["assessment"]
    assert result["ladder_stats"] is not None
    assert "Suggested:" in result["ladder_stats"]["blurb"]
    assert "pass runner_mode" in result["assessment"]
    assert "do not recount channel history" in result["assessment"]


async def test_ladder_open_stores_chosen_and_suggested_runner_mode(data_dir: Path) -> None:
    """Bootstrap suggested is be_after_cover; an explicit hold is stored as the choice."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    result = await ex.open_market(
        _ctx(signal_ref="SIG:1:9101"),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        runner_mode="hold_channel_sl",
        reason="hold despite bootstrap",
    )
    assert result["ok"] is True, result["message"]
    assert result["runner_mode"] == "hold_channel_sl"
    assert result["suggested_runner_mode"] == "be_after_cover"
    rows = await _trades()
    assert {row.runner_mode for row in rows} == {"hold_channel_sl"}
    assert {row.suggested_runner_mode for row in rows} == {"be_after_cover"}
    assert {row.ladder_role for row in rows} == {"cover", "runner"}


async def test_invalid_runner_mode_is_refused(data_dir: Path) -> None:
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    result = await ex.open_market(
        _ctx(signal_ref="SIG:1:9102"),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        runner_mode="yolo",
        reason="bad mode",
    )
    assert result["ok"] is False
    assert "runner_mode" in result["message"]
    assert broker.positions == {}


async def test_hold_channel_sl_does_not_move_runner_stop_after_cover(data_dir: Path) -> None:
    """The watchdog is a no-op for hold_channel_sl so pullbacks can still reach far TPs."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    opened = await ex.open_market(
        _ctx(signal_ref="SIG:1:9103"),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        runner_mode="hold_channel_sl",
        reason="hold the channel stop",
    )
    assert opened["ok"] is True, opened["message"]
    roles = {leg["role"]: leg for leg in opened["legs"]}
    cover_ticket = int(roles["cover"]["ticket"])
    runner_ticket = int(roles["runner"]["ticket"])
    original_sl = float(broker.positions[runner_ticket]["sl"])
    broker.move("XAUUSDm", 2390.0)
    del broker.positions[cover_ticket]

    trailed = await ex.trail_ladder_runners()
    assert runner_ticket not in trailed["trailed"], trailed
    assert any("hold_channel_sl" in item for item in trailed["skipped"])
    assert broker.positions[runner_ticket]["sl"] == pytest.approx(original_sl)
    assert broker.positions[runner_ticket]["sl"] == pytest.approx(2367.0, abs=0.05)


async def test_be_after_cover_still_trails_when_suggested_is_hold(data_dir: Path) -> None:
    """Caution override: the AI may keep BE even when the server suggests hold."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=-1099,
            title="Gold Signals",
            username="goldsignals",
            entity_type="channel",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        sid = int(source.id)
        base = datetime(2026, 4, 1)
        for index in range(5):
            session.add(
                LadderOutcome(
                    source_id=sid,
                    signal_ref=f"SIG:{sid}:{6100 + index}",
                    kind="tp4_plus",
                    closed_at=base + timedelta(hours=index),
                )
            )
    opened = await ex.open_market(
        _ctx(signal_ref="SIG:1:9104", source_id=sid),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        runner_mode="be_after_cover",
        reason="caution override of hold",
    )
    assert opened["ok"] is True, opened["message"]
    assert opened["suggested_runner_mode"] == "hold_channel_sl"
    assert opened["runner_mode"] == "be_after_cover"
    roles = {leg["role"]: leg for leg in opened["legs"]}
    cover_ticket = int(roles["cover"]["ticket"])
    runner_ticket = int(roles["runner"]["ticket"])
    cover_entry = float(broker.positions[cover_ticket]["price_open"])
    broker.move("XAUUSDm", 2390.0)
    del broker.positions[cover_ticket]

    trailed = await ex.trail_ladder_runners()
    assert runner_ticket in trailed["trailed"], trailed
    assert broker.positions[runner_ticket]["sl"] >= cover_entry - 1e-9


async def test_omitted_runner_mode_follows_suggested_hold_and_skips_be_trail(
    data_dir: Path,
) -> None:
    """A full clean window suggests hold; omitting runner_mode stores and executes that."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=-1098,
            title="Gold Signals",
            username="goldsignals",
            entity_type="channel",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        sid = int(source.id)
        base = datetime(2026, 5, 1)
        for index in range(5):
            session.add(
                LadderOutcome(
                    source_id=sid,
                    signal_ref=f"SIG:{sid}:{6200 + index}",
                    kind="tp4_plus",
                    closed_at=base + timedelta(hours=index),
                )
            )
    stats = await ex.get_channel_ladder_stats(source_id=sid)
    assert stats["suggested_mode"] == "hold_channel_sl"
    assert stats["raw_sl_hits"] == 0
    assert "Suggested: hold_channel_sl" in stats["blurb"]

    opened = await ex.open_market(
        _ctx(signal_ref="SIG:1:9105", source_id=sid),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        reason="follow suggested hold",
    )
    assert opened["ok"] is True, opened["message"]
    assert opened["runner_mode"] == "hold_channel_sl"
    assert opened["suggested_runner_mode"] == "hold_channel_sl"
    roles = {leg["role"]: leg for leg in opened["legs"]}
    cover_ticket = int(roles["cover"]["ticket"])
    runner_ticket = int(roles["runner"]["ticket"])
    original_sl = float(broker.positions[runner_ticket]["sl"])
    broker.move("XAUUSDm", 2390.0)
    del broker.positions[cover_ticket]

    trailed = await ex.trail_ladder_runners()
    assert runner_ticket not in trailed["trailed"], trailed
    assert broker.positions[runner_ticket]["sl"] == pytest.approx(original_sl)


async def test_closing_both_ladder_legs_records_the_outcome(data_dir: Path) -> None:
    """The executor writes a ladder_outcomes row when the last ticket of the group closes."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    opened = await ex.open_market(
        _ctx(signal_ref="SIG:1:9106"),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        runner_mode="be_after_cover",
        reason="close both legs",
    )
    assert opened["ok"] is True, opened["message"]
    roles = {leg["role"]: leg for leg in opened["legs"]}
    cover_ticket = int(roles["cover"]["ticket"])
    runner_ticket = int(roles["runner"]["ticket"])
    closed_cover = await ex.close_position(
        _ctx(signal_ref="SIG:1:9106"), ticket=cover_ticket, reason="cover out"
    )
    assert closed_cover["ok"] is True, closed_cover["message"]
    closed_runner = await ex.close_position(
        _ctx(signal_ref="SIG:1:9106"), ticket=runner_ticket, reason="runner out"
    )
    assert closed_runner["ok"] is True, closed_runner["message"]
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(LadderOutcome).where(LadderOutcome.signal_ref == "SIG:1:9106")
                )
            ).scalars().all()
        )
    assert len(rows) == 1
    assert rows[0].kind == "other"  # AI close, not a broker TP/SL
    assert rows[0].runner_mode == "be_after_cover"
    assert rows[0].runner_hit_chosen_tp is False
    assert rows[0].chosen_runner_tp_index == 5
    assert rows[0].runner_tp_bias == 0


async def test_ladder_open_uses_suggested_runner_tp_index(data_dir: Path) -> None:
    """Bootstrap bias 0 → formula TP4, advanced past 1R to TP5; stored on both legs."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    result = await ex.open_market(
        _ctx(signal_ref="SIG:1:9201"),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        reason="follow suggested runner TP",
    )
    assert result["ok"] is True, result["message"]
    assert result["suggested_runner_tp_index"] == 5
    assert result["chosen_runner_tp_index"] == 5
    assert result["runner_tp_bias"] == 0
    roles = {leg["role"]: leg for leg in result["legs"]}
    assert roles["runner"]["tp"] == pytest.approx(2395.0)
    rows = await _trades()
    assert {row.suggested_runner_tp_index for row in rows} == {5}
    assert {row.chosen_runner_tp_index for row in rows} == {5}
    assert {row.runner_tp_bias for row in rows} == {0}


async def test_runner_tp_index_may_shift_suggested_by_one(data_dir: Path) -> None:
    """AI ±1 from suggested TP5 → TP6 is stored as chosen; suggested stays 5."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    result = await ex.open_market(
        _ctx(signal_ref="SIG:1:9202"),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        runner_tp_index=6,
        reason="shift runner one rung",
    )
    assert result["ok"] is True, result["message"]
    assert result["suggested_runner_tp_index"] == 5
    assert result["chosen_runner_tp_index"] == 6
    roles = {leg["role"]: leg for leg in result["legs"]}
    assert roles["runner"]["tp"] == pytest.approx(2398.0)
    rows = await _trades()
    assert {row.chosen_runner_tp_index for row in rows} == {6}
    assert {row.suggested_runner_tp_index for row in rows} == {5}


async def test_runner_tp_index_shift_of_two_is_refused(data_dir: Path) -> None:
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    result = await ex.open_market(
        _ctx(signal_ref="SIG:1:9203"),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        runner_tp_index=7,
        reason="too far",
    )
    assert result["ok"] is False
    assert "runner_tp_index" in result["message"]
    assert broker.positions == {}


async def test_strong_streak_uses_bias_plus_two_on_open(data_dir: Path) -> None:
    """Five TP4+ closes → bias +2 → TP6 of N=7, still not the last numbered rung or LONG."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=-1097,
            title="Gold Signals",
            username="goldsignals",
            entity_type="channel",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        sid = int(source.id)
        base = datetime(2026, 6, 1)
        for index in range(5):
            session.add(
                LadderOutcome(
                    source_id=sid,
                    signal_ref=f"SIG:{sid}:{6300 + index}",
                    kind="tp4_plus",
                    closed_at=base + timedelta(hours=index),
                )
            )
    opened = await ex.open_market(
        _ctx(signal_ref="SIG:1:9204", source_id=sid),
        symbol="XAUUSDm",
        side="buy",
        sl=2367.0,
        tp=list(_LADDER_TPS),
        reason="strong streak runner",
    )
    assert opened["ok"] is True, opened["message"]
    assert opened["runner_tp_bias"] == 2
    assert opened["suggested_runner_tp_index"] == 6
    assert opened["chosen_runner_tp_index"] == 6
    roles = {leg["role"]: leg for leg in opened["legs"]}
    assert roles["runner"]["tp"] == pytest.approx(2398.0)
    assert roles["runner"]["tp"] != pytest.approx(2401.0)
    assert roles["runner"]["tp"] != pytest.approx(2411.0)


async def test_classic_signal_ignores_risk_percent_and_uses_order_lot_size(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """risk_percent=2 must not shrink a classic signal into minimum-lot splits.

    The model used to size ~0.02 lots from 2% of balance and the hedge split turned
    that into 2×0.01, even when the operator settings were 2 orders of 0.2. A classic
    signal ignores that percent the way a ladder does. The lot here is 0.3 so the
    assertion is the stored setting, not the 0.2 default.
    """
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    async with session_scope() as session:
        await set_setting(session, "trading.orders_per_signal", 2)
        await set_setting(session, "trading.order_lot_size", 0.3)
    caplog.set_level("INFO", logger="app.trading.executor")
    result = await ex.open_market(
        _ctx(signal_ref="SIG:1:7710"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2390.0, 2400.0],
        risk_percent=2.0,
        reason="model passed risk_percent",
    )
    assert result["ok"] is True, result["message"]
    assert result["kind"] == "standard"
    assert result["volume_source"] == "order_lot_size"
    assert result["risk_percent"] is None
    assert result["volume"] == pytest.approx(0.6)
    positions = list(broker.positions.values())
    assert len(positions) == 2
    assert sorted(p["volume"] for p in positions) == pytest.approx([0.3, 0.3])
    assert "risk_percent 2% was ignored" in result["message"]
    assert "order_lot_size" in result["message"]
    assert any("classic_risk_percent_ignored" in rec.message for rec in caplog.records)


async def test_classic_explicit_volume_is_kept_when_risk_percent_is_also_sent(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A lot the channel or the model named is the size. risk_percent is only a note."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    caplog.set_level("INFO", logger="app.trading.executor")
    result = await ex.open_market(
        _ctx(signal_ref="SIG:1:7711"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2390.0, 2400.0],
        volume=0.30,
        risk_percent=2.0,
        reason="channel stated 0.30 lots",
    )
    assert result["ok"] is True, result["message"]
    assert result["volume_source"] == "explicit"
    assert result["risk_percent"] is None
    assert result["volume"] == pytest.approx(0.30)
    positions = list(broker.positions.values())
    assert len(positions) == 2
    assert sum(p["volume"] for p in positions) == pytest.approx(0.30)
    assert "risk_percent 2% was ignored" in result["message"]
    assert "explicit volume" in result["message"]
    assert any(
        "classic_risk_percent_ignored" in rec.message and "volume_source=explicit" in rec.message
        for rec in caplog.records
    )


async def test_classic_risk_percent_sizes_the_trade_when_fixed_lots_are_off(
    data_dir: Path,
) -> None:
    """Mode B: trading.use_fixed_order_lots off restores risk_percent sizing.

    The same 2% call that fixed lots would turn into 2×0.3 must come back as a
    risk-sized total instead. An explicit volume on its own is still that volume.
    Sending both is refused, which is how this path worked before fixed lots.
    """
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    async with session_scope() as session:
        await set_setting(session, "trading.use_fixed_order_lots", False)
        await set_setting(session, "trading.orders_per_signal", 2)
        await set_setting(session, "trading.order_lot_size", 0.3)
    sized = await ex.open_market(
        _ctx(signal_ref="SIG:1:7720"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2390.0, 2400.0],
        risk_percent=2.0,
        reason="risk percent is allowed",
    )
    assert sized["ok"] is True, sized["message"]
    assert sized["volume_source"] == "risk_percent"
    assert sized["risk_percent"] == pytest.approx(2.0)
    assert sized["volume"] != pytest.approx(0.6)
    assert sized["volume"] < 0.2
    assert "was ignored" not in sized["message"]
    assert len(broker.positions) == 2
    assert sum(p["volume"] for p in broker.positions.values()) == pytest.approx(sized["volume"])

    explicit = await ex.open_market(
        _ctx(signal_ref="SIG:1:7721"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2390.0, 2400.0],
        volume=0.30,
        reason="channel lot while risk sizing is allowed",
    )
    assert explicit["ok"] is True, explicit["message"]
    assert explicit["volume_source"] == "explicit"
    assert explicit["volume"] == pytest.approx(0.30)
    assert explicit["risk_percent"] is None

    open_before_both = set(broker.positions)
    both = await ex.open_market(
        _ctx(signal_ref="SIG:1:7722"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2390.0, 2400.0],
        volume=0.30,
        risk_percent=2.0,
        reason="both sizes while risk sizing is allowed",
    )
    assert both["ok"] is False
    assert "not both" in both["message"]
    assert set(broker.positions) == open_before_both


async def test_classic_signal_without_a_size_opens_two_lots_of_point_two(data_dir: Path) -> None:
    """Three channel targets, no volume: two nearest orders of 0.2, the far one unmanaged."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    result = await ex.open_market(
        _ctx(signal_ref="SIG:1:7701"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2410.0, 2390.0, 2400.0],
        reason="sized by the operator",
    )
    assert result["ok"] is True, result["message"]
    assert result["volume_source"] == "order_lot_size"
    positions = list(broker.positions.values())
    assert len(positions) == 2
    assert sorted(p["volume"] for p in positions) == pytest.approx([0.2, 0.2])
    assert sorted(round(p["tp"], 2) for p in positions) == [2390.0, 2400.0]
    rows = [t for t in await _trades() if t.kind == "position"]
    assert {row.runner_mode for row in rows} == {"be_after_tp1"}
    unmanaged = {round(price, 2) for row in rows for price in (row.unmanaged_tps or [])}
    assert 2410.0 in unmanaged


async def test_classic_netting_opens_one_order_of_the_lot_size(data_dir: Path) -> None:
    """A netting account cannot split, so the lot size is one order on the nearest target."""
    ex, broker, _ = await _build(data_dir, margin_mode=0)
    result = await ex.open_market(
        _ctx(signal_ref="SIG:1:7702"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2390.0, 2400.0, 2410.0],
        reason="netting lot",
    )
    assert result["ok"] is True, result["message"]
    assert len(broker.positions) == 1
    only = next(iter(broker.positions.values()))
    assert only["volume"] == pytest.approx(0.2)
    assert round(only["tp"], 2) == 2390.0


async def test_tp1_fill_moves_the_remaining_classic_stop_to_breakeven(data_dir: Path) -> None:
    """TP1 gone, the other classic order is still open: its stop moves to breakeven."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    opened = await ex.open_market(
        _ctx(signal_ref="SIG:1:7703"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2390.0, 2400.0, 2410.0],
        reason="then trail after tp1",
    )
    assert opened["ok"] is True, opened["message"]
    rows = [t for t in await _trades() if t.kind == "position"]
    nearest = min(rows, key=lambda row: abs(float(row.tp) - float(row.entry_price)))
    survivor = next(row for row in rows if row.ticket != nearest.ticket)
    entry = float(broker.positions[int(nearest.ticket)]["price_open"])
    broker.move("XAUUSDm", 2395.0)
    del broker.positions[int(nearest.ticket)]

    trailed = await ex.trail_after_tp1()
    assert int(survivor.ticket) in trailed["trailed"], trailed
    moved = broker.positions[int(survivor.ticket)]
    assert moved["sl"] >= entry - 1e-9
    assert moved["sl"] > 2370.0
    again = [t for t in await _trades() if t.ticket == int(survivor.ticket)]
    assert again[0].runner_mode == "be_after_tp1"


async def test_hold_channel_sl_skips_the_tp1_breakeven_trail(data_dir: Path) -> None:
    """The model asked to keep the channel stop. TP1 filling does not move the rest."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    opened = await ex.open_market(
        _ctx(signal_ref="SIG:1:7704"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2390.0, 2400.0],
        runner_mode="hold_channel_sl",
        reason="hold the channel stop",
    )
    assert opened["ok"] is True, opened["message"]
    rows = [t for t in await _trades() if t.kind == "position"]
    assert {row.runner_mode for row in rows} == {"hold_channel_sl"}
    nearest = min(rows, key=lambda row: abs(float(row.tp) - float(row.entry_price)))
    survivor = next(row for row in rows if row.ticket != nearest.ticket)
    original_sl = float(broker.positions[int(survivor.ticket)]["sl"])
    broker.move("XAUUSDm", 2395.0)
    del broker.positions[int(nearest.ticket)]

    trailed = await ex.trail_after_tp1()
    assert trailed["trailed"] == []
    assert broker.positions[int(survivor.ticket)]["sl"] == pytest.approx(original_sl)


async def test_a_model_stop_change_opts_the_group_out_of_tp1_breakeven(data_dir: Path) -> None:
    """A non-watchdog stop edit is a management instruction. The trail leaves it alone."""
    ex, broker, _ = await _build(data_dir, margin_mode=2)
    opened = await ex.open_market(
        _ctx(signal_ref="SIG:1:7705"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.00,
        tp=[2390.0, 2400.0],
        reason="then the model tightens",
    )
    assert opened["ok"] is True, opened["message"]
    rows = [t for t in await _trades() if t.kind == "position"]
    nearest = min(rows, key=lambda row: abs(float(row.tp) - float(row.entry_price)))
    survivor = next(row for row in rows if row.ticket != nearest.ticket)
    tightened = await ex.modify_position(
        _ctx(signal_ref="SIG:1:7705"),
        ticket=int(survivor.ticket),
        sl=2375.0,
        reason="tighten before tp1",
    )
    assert tightened["ok"] is True, tightened["message"]
    marked = [t for t in await _trades() if t.kind == "position"]
    assert {row.runner_mode for row in marked} == {"ai_managed"}
    kept = float(broker.positions[int(survivor.ticket)]["sl"])
    broker.move("XAUUSDm", 2395.0)
    del broker.positions[int(nearest.ticket)]

    trailed = await ex.trail_after_tp1()
    assert int(survivor.ticket) not in trailed["trailed"]
    assert broker.positions[int(survivor.ticket)]["sl"] == pytest.approx(kept)


async def test_evaluate_entry_must_open_a_classic_short_eighty_cents_from_entry(
    data_dir: Path,
) -> None:
    """The live miss: SELL 4156.78, bid 4155.97, fixed 2×0.2. That is an open."""
    ex, broker, _ = await _build(data_dir)
    broker.move("XAUUSDm", 4155.97)
    result = await ex.evaluate_entry(
        "XAUUSDm",
        "sell",
        4156.78,
        4168.0,
        tp=[4148.0, 4140.0],
    )
    assert result["ok"] is True, result.get("message")
    assert result["entry_now"] == pytest.approx(4155.97)
    assert result["price_distance_abs"] == pytest.approx(0.81)
    assert result["max_entry_drift_usd"] == pytest.approx(5.0)
    assert result["within_entry_drift"] is True
    assert result["entry_action"] == "open"
    assert result["must_open"] is True
    assert result["defer_for_risk_forbidden"] is True
    assert result["use_fixed_order_lots"] is True
    assert result["orders_per_signal"] == 2
    assert result["order_lot_size"] == pytest.approx(0.2)
    assessment = result["assessment"]
    assert "MUST OPEN" in assessment
    assert "Do not defer or skip because the lots" in assessment
    assert "operator sizing is the law" in assessment
    assert "only if you pass risk_percent" not in assessment
    assert "not a reason to defer" in assessment


async def test_evaluate_entry_skips_only_when_the_fill_is_more_than_five_dollars_away(
    data_dir: Path,
) -> None:
    ex, broker, _ = await _build(data_dir)
    broker.move("XAUUSDm", 4150.0)
    far = await ex.evaluate_entry(
        "XAUUSDm",
        "sell",
        4156.78,
        4168.0,
        tp=[4148.0],
    )
    assert far["entry_action"] == "skip"
    assert far["skip_reason"] == "price_moved"
    assert far["must_open"] is False
    assert far["price_distance_abs"] == pytest.approx(6.78)
    assert "SKIP this market entry" in far["assessment"]

    broker.move("XAUUSDm", 4151.78)
    edge = await ex.evaluate_entry(
        "XAUUSDm",
        "sell",
        4156.78,
        4168.0,
        tp=[4148.0],
    )
    assert edge["price_distance_abs"] == pytest.approx(5.0)
    assert edge["must_open"] is True
    assert edge["entry_action"] == "open"
