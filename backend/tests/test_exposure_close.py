"""v1.1: a deleted channel post, or a FULL CLOSE reply, flattens the chain.

The close is a server function. It does not wait for a stop, and it does not ask
the model to list tickets. Deleting either the teaser or the ladder reply closes
cover, runner and pending orders on that chain. A partial ("close half") is not
a full close. A later open on the voided chain is refused.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app import __version__
from app.db import session_scope
from app.models import Message, Source, Trade
from app.telegram.ingest import Ingestor
from app.trading.executor import RunContext
from app.trading.exposure import is_full_close_text

from tests.test_executor import _build as build_executor
from tests.test_ingest import CHAT_ID, text_message


async def _ready(data_dir: Path):
    """Executor, broker and ingestor sharing one active source."""
    executor, broker, runtime = await build_executor(data_dir)
    ingestor = Ingestor(runtime)
    runtime.ingestor = ingestor
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=CHAT_ID,
            title="Gold Signals",
            username="goldsignals",
            entity_type="channel",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        source_id = int(source.id)
        teaser = Message(
            source_id=source_id,
            message_id=100,
            kind="text",
            status="processed",
            text="XAUUSD BUY",
        )
        ladder = Message(
            source_id=source_id,
            message_id=101,
            kind="text",
            status="processed",
            text="SL 2350 TP1 2395 TP2 2400 TP3 2405 TP4 2410",
            reply_to_message_id=100,
        )
        session.add_all([teaser, ladder])
        await session.flush()
        teaser_ref = teaser.signal_ref
        ladder_ref = ladder.signal_ref
    return executor, broker, runtime, ingestor, source_id, teaser_ref, ladder_ref


def _ctx(ref: str, source_id: int) -> RunContext:
    return RunContext(
        run_id="a1b2c3d4e5f6",
        signal_ref=ref,
        source_id=source_id,
        message_row_id=None,
        origin="agent",
    )


async def _open_legs(executor, ref: str, source_id: int) -> None:
    opened = await executor.open_market(
        _ctx(ref, source_id),
        symbol="XAUUSDm",
        side="buy",
        sl=0,
        tp=[],
        reason="teaser",
    )
    assert opened["ok"] is True, opened["message"]


async def test_full_close_phrases() -> None:
    assert is_full_close_text("FULL CLOSE TRADE")
    assert is_full_close_text("full close")
    assert is_full_close_text("close trade")
    assert is_full_close_text("FULL CLOSE BUY")
    assert is_full_close_text("CLOSE SELL TRADE")
    assert is_full_close_text("XAUUSD FULL CLOSE TRADE ✅")
    assert not is_full_close_text("close half")
    assert not is_full_close_text("partial close")
    assert not is_full_close_text("tp1 hit, close trade if you want later")
    assert not is_full_close_text("BUY GOLD NOW")


async def test_initial_public_version_is_0_1_0() -> None:
    assert __version__ == "0.1.0"


async def test_deleted_teaser_closes_positions_and_blocks_reopen(data_dir: Path) -> None:
    executor, broker, _runtime, ingestor, source_id, teaser_ref, _ladder = await _ready(data_dir)
    await _open_legs(executor, teaser_ref, source_id)
    assert len(broker.positions) == 2

    await ingestor.handle_deleted([100])

    assert broker.positions == {}
    async with session_scope() as session:
        rows = list((await session.execute(select(Message).where(Message.message_id == 100))).scalars())
        assert rows and all(row.deleted_at is not None for row in rows)
        trades = list((await session.execute(select(Trade))).scalars())
    assert trades and all(row.status == "closed" for row in trades)
    assert {row.close_cause for row in trades} == {"channel_delete"}

    again = await executor.open_market(
        _ctx(teaser_ref, source_id),
        symbol="XAUUSDm",
        side="buy",
        sl=0,
        reason="must not reopen a deleted signal",
    )
    assert again["ok"] is False
    assert again.get("reason") == "exposure_closed"
    assert broker.positions == {}


async def test_deleted_ladder_closes_the_teaser_chain(data_dir: Path) -> None:
    executor, broker, _runtime, ingestor, source_id, teaser_ref, _ladder = await _ready(data_dir)
    await _open_legs(executor, teaser_ref, source_id)
    assert len(broker.positions) == 2

    await ingestor.handle_deleted([101])

    assert broker.positions == {}
    async with session_scope() as session:
        ladder = (
            await session.execute(select(Message).where(Message.message_id == 101))
        ).scalars().first()
        teaser = (
            await session.execute(select(Message).where(Message.message_id == 100))
        ).scalars().first()
    assert ladder is not None and ladder.deleted_at is not None
    assert teaser is not None and teaser.deleted_at is None


async def test_deleted_signal_cancels_pending_orders(data_dir: Path) -> None:
    executor, broker, _runtime, ingestor, source_id, teaser_ref, _ladder = await _ready(data_dir)
    placed = await executor.place_pending(
        _ctx(teaser_ref, source_id),
        symbol="XAUUSDm",
        side="buy",
        order_type="limit",
        price=2360.0,
        sl=2350.0,
        tp=[],
        volume=0.10,
        reason="waiting",
    )
    assert placed["ok"] is True, placed["message"]
    assert broker.orders

    await ingestor.handle_deleted([100])

    assert broker.orders == {}
    async with session_scope() as session:
        trades = list((await session.execute(select(Trade))).scalars())
    assert trades and all(row.status == "cancelled" for row in trades)
    assert {row.close_cause for row in trades} == {"channel_delete"}


async def test_full_close_reply_flattens_without_enqueue(data_dir: Path) -> None:
    executor, broker, _runtime, ingestor, source_id, teaser_ref, _ladder = await _ready(data_dir)
    await _open_legs(executor, teaser_ref, source_id)

    row_id = await ingestor.handle_message(
        text_message(102, "FULL CLOSE TRADE", reply_to_msg_id=100)
    )
    assert row_id is not None
    assert broker.positions == {}
    assert ingestor._queue.qsize() == 0

    async with session_scope() as session:
        row = await session.get(Message, row_id)
        assert row is not None
        assert row.status == "processed"
        assert row.skip_reason == "full_close"
        trades = list((await session.execute(select(Trade))).scalars())
    assert {row.close_cause for row in trades} == {"full_close"}

    half = await ingestor.handle_message(
        text_message(103, "close half", reply_to_msg_id=100)
    )
    assert half is not None
    assert ingestor._queue.qsize() == 1


async def test_close_signal_exposure_tool_resolves_tickets(data_dir: Path) -> None:
    """The model passes a reason, not a ticket list."""
    executor, broker, _runtime, _ingestor, source_id, teaser_ref, _ladder = await _ready(data_dir)
    await _open_legs(executor, teaser_ref, source_id)
    assert len(broker.positions) == 2

    result = await executor.dispatch(
        "close_signal_exposure",
        {"reason": "channel said full close"},
        _ctx(teaser_ref, source_id),
    )
    assert result["ok"] is True, result["message"]
    assert len(result["closed"]) == 2
    assert broker.positions == {}
    assert "ticket" not in {"reason"}
