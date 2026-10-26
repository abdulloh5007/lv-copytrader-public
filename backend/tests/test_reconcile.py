"""Startup recovery driven against a fake broker: BUILD_PROMPT acceptance criterion 11.

Criterion 11 in one sentence: *killing the backend mid-position and restarting reconciles that
position from MT5 by ticket and `magic`, re-links it via the `SP:` comment, and leaves positions
with a foreign `magic` untouched and marked `external` — including when the kill switch fires.*

That sentence hides five separate failure modes, and every test below pins exactly one of them:

  * a restart must not duplicate the row it already has for a live ticket,
  * a position opened moments before the crash has no row at all, and the only thing tying it
    back to its signal is the ``SP:xxxxxxxx`` comment the executor wrote,
  * an ``SP:`` tag that resolves to nothing must still produce a *managed* row — dropping it
    would hide the position from the SL watchdog and from the kill switch,
  * a foreign magic is read-only forever, kill switch included,
  * a ticket that died while we were down is closed from the deal history, over a window whose
    ``date_to`` is padded a day into the future because MT5 dates are broker-server time
    (BUILD_PROMPT §2) — an unpadded window silently returns no deals and the P/L is lost.

Plus §8's standing rule: a reconcile failure logs and degrades, and never stops the app starting.

The broker is ``tests.fake_mt5.FakeMt5`` reached through ``tests.fake_rpc.FakeRpc``, assembled
into a real :class:`app.runtime.Runtime` exactly as ``tests/test_executor.py`` does.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.config import settings as app_settings
from app.db import init_db, session_scope, set_setting
from app.events import EventBus
from app.models import Message, Run, Source, SystemState, Trade
from app.runtime import Runtime
from app.trading.executor import Executor, RunContext
from app.trading.reconcile import (
    COMMENT_PREFIX,
    HISTORY_PAD_AFTER_SECONDS,
    make_comment,
    reconcile,
    run_id_from_comment,
)
from app.trading.symbols import SymbolResolver, server_time_offset

from tests.fake_mt5 import (
    ORDER_TYPE_BUY,
    ORDER_TYPE_BUY_LIMIT,
    POSITION_TYPE_BUY,
    FakeMt5,
)
from tests.fake_rpc import FakeRpc

#: A deal made by the take profit. MQL5 ENUM_DEAL_REASON.DEAL_REASON_TP.
DEAL_REASON_TP = 5

#: MQL5 ENUM_DEAL_ENTRY.DEAL_ENTRY_OUT — the entry that takes volume off a position.
DEAL_ENTRY_OUT = 1

FOREIGN_MAGIC = 999999


class _NullNotifier:
    """Swallows notifications; a notification must never affect a reconciled outcome."""

    def fire(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        if name.startswith("render_"):
            return lambda *a, **k: ""
        raise AttributeError(name)


async def _build(data_dir: Path, *, armed: bool = True) -> tuple[Runtime, FakeMt5]:
    """Assemble a real Runtime over a fake broker, with the DB schema created."""
    await init_db()
    broker = FakeMt5()
    rpc = FakeRpc(broker)
    runtime = Runtime(settings=app_settings, rpc=rpc, resolver=None, bus=EventBus())
    runtime.resolver = SymbolResolver(rpc, app_settings)
    runtime.executor = Executor(runtime)
    runtime.notifier = _NullNotifier()
    async with session_scope() as session:
        await set_setting(session, "trading.armed", armed)
    return runtime, broker


def _ctx(**kw: Any) -> RunContext:
    base = dict(run_id="a1b2c3d4e5f6", signal_ref="SIG:1:4242", source_id=None,
                message_row_id=None, origin="agent")
    base.update(kw)
    return RunContext(**base)


async def _offset(runtime: Runtime) -> float:
    """Measure the broker clock through the same production helper reconcile uses.

    Hard-coding ``FakeMt5.server_time_offset`` here would make the history-window assertions
    agree with the fake instead of with the code under test.
    """
    return float(await server_time_offset(runtime.rpc, runtime.resolver))


async def _trades() -> list[Trade]:
    async with session_scope() as session:
        rows = await session.execute(select(Trade).order_by(Trade.id))
        return list(rows.scalars().all())


async def _trade(ticket: int) -> Trade:
    async with session_scope() as session:
        rows = await session.execute(select(Trade).where(Trade.ticket == ticket))
        found = list(rows.scalars().all())
    assert len(found) == 1, f"expected exactly one Trade row for #{ticket}, got {len(found)}"
    return found[0]


async def _seed_signal(run_id: str, *, message_id: int = 4242) -> tuple[int, int, str]:
    """Insert the Source -> Message -> Run chain a recovered `SP:` tag has to walk back up."""
    async with session_scope() as session:
        source = Source(kind="telegram", chat_id=-1001234567890, title="Gold Signals",
                        username="goldsignals", entity_type="channel", is_active=True)
        session.add(source)
        await session.flush()
        message = Message(source_id=source.id, message_id=message_id, edit_date=0, revision=1,
                          kind="text", status="processed", text="BUY GOLD 2380 SL 2370",
                          permalink=f"https://t.me/goldsignals/{message_id}")
        session.add(message)
        await session.flush()
        session.add(Run(id=run_id, source_id=source.id, message_row_id=message.id,
                        kind="signal", status="done", decision="trade"))
        await session.flush()
        return source.id, message.id, message.signal_ref


async def _seed_trade(**kw: Any) -> None:
    """Insert one Trade row, defaulted to a plain long gold position of ours."""
    values: dict[str, Any] = dict(
        kind="position", symbol="XAUUSDm", side="buy", order_type=ORDER_TYPE_BUY,
        volume=0.10, entry_price=2380.00, sl=2370.00, tp=2400.00, status="open",
        magic=int(app_settings.MAGIC_NUMBER), comment="SP:deadbeef", external=False,
        split_index=0, split_count=1, unmanaged_tps=[],
    )
    values.update(kw)
    async with session_scope() as session:
        session.add(Trade(**values))


def _put_position(broker: FakeMt5, *, magic: int, comment: str, offset: float,
                  symbol: str = "XAUUSDm", **kw: Any) -> int:
    """Put a position at the broker that no Trade row knows about yet."""
    ticket = broker.next_ticket()
    now = int(time.time() + offset)
    row: dict[str, Any] = {
        "ticket": ticket, "symbol": symbol, "type": POSITION_TYPE_BUY, "volume": 0.10,
        "price_open": 2380.00, "sl": 2370.00, "tp": 2400.00, "price_current": 2380.55,
        "profit": 5.50, "swap": 0.0, "magic": magic, "comment": comment,
        "time": now, "time_msc": now * 1000, "identifier": ticket, "reason": 3,
    }
    row.update(kw)
    broker.positions[ticket] = row
    return ticket


def _history_windows(broker: FakeMt5) -> list[dict[str, Any]]:
    """Every ``history_deals_get`` the code under test issued, with its arguments."""
    return [params for method, params in broker.calls if method == "history_deals_get"]


# ---------------------------------------------------------------------------
# Criterion 11 -- a live ticket the database already knows about
# ---------------------------------------------------------------------------


async def test_open_ticket_is_relinked_by_ticket_and_the_row_is_not_duplicated(
    data_dir: Path,
) -> None:
    """Crit 11: restarting over a live position must find its row, not write a second one.

    A reconcile that inserted a fresh row per live ticket would double every position in the
    dashboard and in the P/L after a single restart, and the duplicate-signal guard of §5.5
    would then be reasoning over two rows for one trade.
    """
    runtime, broker = await _build(data_dir)
    opened = await runtime.executor.open_market(
        _ctx(), symbol="XAUUSDm", side="buy", sl=2370.00, tp=[], volume=0.10,
        reason="open, then die",
    )
    assert opened["ok"] is True, opened["message"]
    ticket = opened["ticket"] or opened["tickets"][0]
    before = await _trade(ticket)
    assert before.status == "open"

    counts = await reconcile(runtime, cause="startup")

    async with session_scope() as session:
        state = await session.get(SystemState, 1)
    assert state is not None and state.last_reconcile_at is not None, (
        "reconcile ran to completion -- it did not abort partway and roll its work back, which "
        "is how a second INSERT for a live ticket would otherwise hide behind the unique index"
    )
    assert counts["closed"] == 0, "a ticket that is still live is never reconciled to closed"
    assert ticket in broker.positions, "reconcile never writes to the broker"
    assert len(await _trades()) == 1, "the existing row was matched by ticket, not duplicated"

    after = await _trade(ticket)
    assert after.id == before.id
    assert after.status == "open"
    assert after.closed_at is None and after.close_price is None
    assert after.signal_ref == "SIG:1:4242", "the link the executor wrote survives a reconcile"
    assert after.external is False


# ---------------------------------------------------------------------------
# Criterion 11 -- the row that was never written
# ---------------------------------------------------------------------------


async def test_position_with_no_row_is_recovered_and_relinked_from_the_sp_comment(
    data_dir: Path,
) -> None:
    """Crit 11 / §8.2: the process died between `order_send` and the INSERT.

    All that is left tying the position to the post that caused it is the `SP:xxxxxxxx` tag the
    executor wrote into `comment`. Reconcile must walk tag -> Run -> Message and put the
    `source_id`, `message_row_id` and `signal_ref` back onto a freshly created row; without
    that the trade shows up as belonging to nobody and its result is never attributed.
    """
    runtime, broker = await _build(data_dir)
    run_id = "deadbeef" + "0" * 24
    source_id, message_row_id, signal_ref = await _seed_signal(run_id)
    offset = await _offset(runtime)

    ticket = _put_position(broker, magic=int(app_settings.MAGIC_NUMBER),
                           comment=make_comment(run_id), offset=offset)
    assert await _trades() == [], "the crash left no row behind"

    counts = await reconcile(runtime, cause="startup")

    assert counts["linked"] == 1, "the tag resolved to a run and the ticket was linked"
    assert counts["orphans"] == 0

    row = await _trade(ticket)
    assert row.status == "open" and row.external is False
    assert row.run_id == run_id, "the full run id is recovered from an 8-hex prefix"
    assert row.source_id == source_id
    assert row.message_row_id == message_row_id
    assert row.signal_ref == signal_ref
    assert row.symbol == "XAUUSDm" and row.side == "buy"
    assert row.volume == pytest.approx(0.10)
    assert row.entry_price == pytest.approx(2380.00)
    assert row.sl == pytest.approx(2370.00), "the stop is read back from the broker, not guessed"


async def test_unmatched_sp_comment_still_becomes_a_managed_row(data_dir: Path) -> None:
    """Crit 11: no run matches the tag, and the position must still be ours to manage.

    Dropping it would be the dangerous outcome: a position carrying MAGIC_NUMBER that the
    database has never heard of is invisible to the stop-loss watchdog of §6 and to the kill
    switch of §5.5. Unlinked is acceptable; unmanaged is not.
    """
    runtime, broker = await _build(data_dir)
    offset = await _offset(runtime)
    ticket = _put_position(broker, magic=int(app_settings.MAGIC_NUMBER),
                           comment=f"{COMMENT_PREFIX}0badc0de", offset=offset)

    counts = await reconcile(runtime, cause="startup")

    assert counts["linked"] == 0, "nothing was linked -- there is no run to link to"
    assert counts["orphans"] == 1, "and the ticket is reported as managed-but-unlinked"

    row = await _trade(ticket)
    assert row.signal_ref is None and row.run_id is None
    assert row.external is False, "our magic means we manage it, linked or not"
    assert row.status == "open"
    assert row.magic == int(app_settings.MAGIC_NUMBER)


# ---------------------------------------------------------------------------
# Criterion 11 -- someone else's trade, kill switch included
# ---------------------------------------------------------------------------


async def test_foreign_magic_is_marked_external_and_survives_the_kill_switch(
    data_dir: Path,
) -> None:
    """Crit 11, the explicit clause: a foreign magic is read-only, *including* from the switch.

    Two ways to fail this, and both close a stranger's trade: reconcile "correcting" a position
    it has no row for, or the kill switch flattening everything `positions_get()` returns
    instead of only the tickets carrying MAGIC_NUMBER.
    """
    runtime, broker = await _build(data_dir)
    offset = await _offset(runtime)

    ours = await runtime.executor.open_market(
        _ctx(), symbol="XAUUSDm", side="buy", sl=2370.00, tp=[], volume=0.10, reason="ours",
    )
    assert ours["ok"] is True, ours["message"]
    our_ticket = ours["ticket"] or ours["tickets"][0]

    foreign = _put_position(broker, magic=FOREIGN_MAGIC, comment="someone else's robot",
                            offset=offset, symbol="EURUSDm", volume=0.50,
                            price_open=1.08500, sl=1.08000, tp=0.0, price_current=1.08500)
    snapshot = dict(broker.positions[foreign])

    counts = await reconcile(runtime, cause="startup")

    assert counts["external"] == 1
    row = await _trade(foreign)
    assert row.external is True, "a foreign magic is recorded read-only"
    assert row.magic == FOREIGN_MAGIC
    assert broker.positions[foreign] == snapshot, "reconcile changed nothing at the broker"

    # Now the switch, which is where criterion 11 says the rule must still hold.
    result = await runtime.executor.kill_switch(reason="operator pressed the switch")

    assert result["ok"] is True, result
    assert result["closed"] >= 1, "our own position was flattened"
    assert our_ticket not in broker.positions
    assert foreign in broker.positions, "the foreign position is STILL OPEN after the switch"
    assert broker.positions[foreign] == snapshot, "and untouched, not merely still present"

    touched = [
        p["request"].get("position")
        for method, p in broker.calls
        if method == "order_send" and p["request"].get("position")
    ]
    assert foreign not in touched, "no order_send ever named the foreign ticket"


# ---------------------------------------------------------------------------
# Criterion 11 / §8.3 -- closed while we were down
# ---------------------------------------------------------------------------


async def test_ticket_closed_while_down_is_reconciled_from_the_deal_history(
    data_dir: Path,
) -> None:
    """§8.3: the row says open, the broker has never heard of the ticket -- close it honestly.

    The realized P/L has to come from the deals (profit + swap + commission + fee), not from a
    guess, or the history table disagrees with the account balance. And the window's `date_to`
    must be padded a day into the future: MT5 dates are broker-server time and ours is not
    (§2), so an "up to now" window ends *before* deals the broker has already stamped and the
    close comes back empty.
    """
    runtime, broker = await _build(data_dir)
    offset = await _offset(runtime)
    ticket = 700123
    await _seed_trade(ticket=ticket, signal_ref="SIG:1:4242", entry_price=2380.00)
    assert ticket not in broker.positions, "the ticket is gone from the broker"

    closed_at_broker = int(time.time() + offset)
    broker.deals.append({
        "ticket": 900123, "order": 900123, "position_id": ticket, "symbol": "XAUUSDm",
        "type": 1, "entry": DEAL_ENTRY_OUT, "volume": 0.10, "price": 2390.50,
        "profit": 12.34, "commission": -0.60, "swap": -0.15, "fee": 0.0,
        "time": closed_at_broker, "time_msc": closed_at_broker * 1000,
        "magic": int(app_settings.MAGIC_NUMBER), "comment": "tp 2390.50",
        "reason": DEAL_REASON_TP,
    })

    counts = await reconcile(runtime, cause="startup")

    assert counts["closed"] == 1
    row = await _trade(ticket)
    assert row.status == "closed"
    assert row.close_price == pytest.approx(2390.50)
    assert row.profit == pytest.approx(11.59), (
        "realized P/L is profit + swap + commission + fee, not profit alone"
    )
    assert row.closed_at is not None
    assert row.close_cause == "tp", "the deal's `reason` says the take profit did it"
    assert row.profit_points == pytest.approx(1050.0)

    windows = _history_windows(broker)
    assert windows, "the deal history was actually read"
    window = windows[-1]
    broker_now = time.time() + offset
    assert window["date_to"] - broker_now >= HISTORY_PAD_AFTER_SECONDS - 5.0, (
        "date_to must be padded a day into the FUTURE on the broker's clock (§2); an "
        "unpadded window returns no deals and the realized P/L is lost"
    )
    assert window["date_from"] < broker_now, "and date_from reaches back before the close"
    assert window["date_from"] < window["date_to"]


async def test_pending_order_that_vanished_without_a_deal_is_cancelled_not_closed(
    data_dir: Path,
) -> None:
    """§8.3: a pending order that left no deal was never filled.

    Recording it "closed" with no price and no profit would put a phantom flat trade into the
    history and into every win-rate computed from it. It was cancelled, expired or deleted in
    the terminal, and `cancelled` is the only honest word for that.
    """
    runtime, broker = await _build(data_dir)
    ticket = 700456
    await _seed_trade(ticket=ticket, kind="order", status="pending",
                      order_type=ORDER_TYPE_BUY_LIMIT, entry_price=2360.00,
                      requested_volume=0.10, signal_ref="SIG:1:4243")
    assert broker.deals == [], "nothing ever filled"

    counts = await reconcile(runtime, cause="startup")

    assert counts["closed"] == 1, "the vanished row was reconciled"
    row = await _trade(ticket)
    assert row.status == "cancelled", "not 'closed' -- it never became a position"
    assert row.close_price is None
    assert row.profit is None, "a never-filled order realized nothing"
    assert row.closed_at is not None


# ---------------------------------------------------------------------------
# §8 -- reconcile never prevents startup
# ---------------------------------------------------------------------------


async def test_reconcile_degrades_to_zero_counts_when_the_sidecar_is_unreachable(
    data_dir: Path,
) -> None:
    """§8: a failed reconcile logs and returns; it never propagates.

    An operator who cannot start the app cannot fix the account either. The second half matters
    just as much: an unreachable sidecar is indistinguishable from an empty account, so nothing
    may be closed -- treating "MT5 is down" as "every position vanished" would close every live
    row and tell the operator trades they still hold are gone.
    """
    runtime, broker = await _build(data_dir)
    ticket = 700789
    await _seed_trade(ticket=ticket, signal_ref="SIG:1:4244")
    runtime.rpc.unreachable = True

    counts = await reconcile(runtime, cause="startup")

    assert isinstance(counts, dict)
    assert set(counts) == {"linked", "external", "closed", "orphans", "catchup"}
    assert all(value == 0 for value in counts.values()), counts

    row = await _trade(ticket)
    assert row.status == "open", (
        "an unreadable snapshot must never be read as 'every position closed'"
    )
    assert row.closed_at is None


async def test_a_malformed_snapshot_row_never_propagates_out_of_reconcile(
    data_dir: Path,
) -> None:
    """§8: the outer guard. Anything that throws inside reconcile is caught, logged, zeroed.

    The unreachable-sidecar case above is handled by an explicit `ok=False` branch and never
    reaches the wrapper, so it cannot prove the wrapper exists. A broker that answers
    `positions_get()` with a row missing `ticket` does: it blows up mid-pass, after the
    snapshot was judged healthy. That must still come back as counts, because a reconcile
    that refuses to finish must never be the thing that stops the app from starting.
    """
    runtime, broker = await _build(data_dir)
    ticket = 700999
    await _seed_trade(ticket=ticket, signal_ref="SIG:1:4245")
    # A position row with no `ticket` field at all -- attribute access on it raises.
    broker.positions[0] = {"symbol": "XAUUSDm", "type": POSITION_TYPE_BUY, "volume": 0.10,
                           "magic": int(app_settings.MAGIC_NUMBER), "comment": "SP:deadbeef"}

    counts = await reconcile(runtime, cause="startup")

    assert set(counts) == {"linked", "external", "closed", "orphans", "catchup"}
    assert all(value == 0 for value in counts.values()), counts

    row = await _trade(ticket)
    assert row.status == "open", "a half-failed reconcile must not half-close the database"


# ---------------------------------------------------------------------------
# The comment tag itself -- the only thing that survives a crash
# ---------------------------------------------------------------------------


def test_make_comment_and_run_id_from_comment_round_trip() -> None:
    """Crit 11 rests entirely on this pair: `SP:` is the only link a crash leaves behind.

    §5.3 forbids `comment` as a tool parameter precisely so the executor can own the field and
    this function can read it back. A broker decorating or upper-casing the comment must not
    cost the link, so the tag is searched for, not compared against the whole string.
    """
    run_id = "deadbeefcafe0001cafe0001cafe0001"
    assert make_comment(run_id) == "SP:deadbeef"
    assert len(make_comment(run_id)) <= 31, "MT5 caps an order comment at 31 characters"
    assert run_id_from_comment(make_comment(run_id)) == run_id[:8]

    assert run_id_from_comment("[tp] SP:deadbeef") == "deadbeef"
    assert run_id_from_comment("SP:deadbeef cancelled") == "deadbeef"
    assert run_id_from_comment("[sl 2370.00] SP:deadbeef -> closed") == "deadbeef"
    assert run_id_from_comment("SP:DEADBEEF") == "deadbeef", "a broker may upper-case it"

    assert run_id_from_comment("") is None
    assert run_id_from_comment("someone else's robot") is None
