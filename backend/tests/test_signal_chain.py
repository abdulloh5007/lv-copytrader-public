"""A teaser and its ladder reply are one chain, even when three workers are free.

The live failure: an incomplete teaser opened two stopless legs, and the ladder reply
started on another worker before that run finished. The reply has its own signal_ref, so
the duplicate guard did not see the teaser's tickets, and the reply placed a second pair.

These tests pin three facts. Related posts do not run together. A ladder reply on
stopless legs modifies those tickets. An unrelated post still runs, and can still open.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.ai import loop as loop_mod
from app.ai.prompts import _reply_section
from app.db import init_db, session_scope
from app.models import Message, Source, Trade
from app.telegram.ingest import Ingestor
from app.trading import guards
from app.trading.executor import RunContext

from tests.test_executor import _build as build_executor
from tests.test_ingest import CHAT_ID, _StubTelegram, text_message
LADDER_TPS = [2395.0, 2400.0, 2405.0, 2410.0, 2415.0, 2420.0, 2450.0]


async def _seed_chain() -> tuple[int, str, str, str, int, int]:
    """One active source, a teaser, a reply to it, and an unrelated post.

    Returns source id, the three signal refs, and the teaser and reply row ids.
    """
    await init_db()
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=CHAT_ID,
            title="SP Demo Signals",
            username="spdemo",
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
            status="pending",
            text="XAUUSD BUY",
        )
        reply = Message(
            source_id=source_id,
            message_id=101,
            kind="text",
            status="pending",
            text="TP1 2395 TP2 2400 TP3 2405 TP4 2410 TP5 2415 TP6 2420 LONG 2450 #SL2350",
            reply_to_message_id=100,
        )
        noise = Message(
            source_id=source_id,
            message_id=200,
            kind="text",
            status="pending",
            text="good morning",
        )
        session.add_all([teaser, reply, noise])
        await session.flush()
        return (
            source_id,
            teaser.signal_ref,
            reply.signal_ref,
            noise.signal_ref,
            int(teaser.id),
            int(reply.id),
        )


def _ctx(ref: str, source_id: int, row_id: int | None = None) -> RunContext:
    return RunContext(
        run_id="c0ffee" + "ab" * 13,
        signal_ref=ref,
        source_id=source_id,
        message_row_id=row_id,
        origin="agent",
    )


async def test_chain_refs_join_a_reply_to_its_teaser_and_leave_noise_alone(data_dir: Path) -> None:
    del data_dir
    source_id, teaser_ref, reply_ref, noise_ref, teaser_row, reply_row = await _seed_chain()
    async with session_scope() as session:
        teaser_family = await guards.chain_signal_refs(session, teaser_ref)
        reply_family = await guards.chain_signal_refs(session, reply_ref)
        noise_family = await guards.chain_signal_refs(session, noise_ref)
    assert teaser_family == reply_family
    assert teaser_ref in teaser_family and reply_ref in teaser_family
    assert noise_ref not in teaser_family
    assert noise_family == [noise_ref]
    assert await guards.chain_run_key(teaser_row) == await guards.chain_run_key(reply_row)
    assert await guards.chain_run_key(teaser_row) == f"{source_id}:100"


async def test_ladder_reply_modifies_stopless_legs_instead_of_opening(data_dir: Path) -> None:
    """The reply's own signal_ref must not open a second 0.05+0.05 pair."""
    ex, broker, _runtime = await build_executor(data_dir)
    source_id, teaser_ref, reply_ref, noise_ref, _teaser_row, reply_row = await _seed_chain()

    opened = await ex.open_market(
        _ctx(teaser_ref, source_id),
        symbol="XAUUSDm",
        side="buy",
        sl=0,
        tp=[],
        reason="incomplete teaser",
    )
    assert opened["ok"] is True, opened["message"]
    assert opened["kind"] == "incomplete"
    chain_tickets = set(opened["tickets"])
    assert len(chain_tickets) == 2
    assert len(broker.positions) == 2
    assert all(broker.positions[ticket]["sl"] == 0.0 for ticket in chain_tickets)

    naked_again = await ex.open_market(
        _ctx(reply_ref, source_id, reply_row),
        symbol="XAUUSDm",
        side="buy",
        sl=0,
        reason="reply has not got levels yet",
    )
    assert naked_again["ok"] is False
    assert "modify" in naked_again["message"].lower()
    assert len(broker.positions) == 2
    assert broker.orders == {}

    ladder = await ex.place_pending(
        _ctx(reply_ref, source_id, reply_row),
        symbol="XAUUSDm",
        side="buy",
        order_type="limit",
        price=2360.0,
        sl=2350.0,
        tp=LADDER_TPS,
        reason="ladder reply",
    )
    assert ladder["ok"] is True, ladder["message"]
    assert ladder["action"] == "modify_existing"
    assert ladder["kind"] == "ladder"
    assert set(ladder["tickets"]) == chain_tickets
    assert len(broker.positions) == 2, "a ladder reply must not open a second pair"
    assert broker.orders == {}, "and it must not park a second pair as pending orders"

    cover_tp = None
    runner_tp = None
    for ticket in chain_tickets:
        position = broker.positions[ticket]
        assert position["sl"] == 2350.0
        assert position["tp"] > position["price_open"]
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(select(Trade).where(Trade.ticket.in_(list(chain_tickets))))
            ).scalars()
        )
    assert {row.ladder_role for row in rows} == {"cover", "runner"}
    for row in rows:
        assert row.sl == 2350.0
        assert row.tp > 0.0
        assert row.signal_ref == teaser_ref
        if row.ladder_role == "cover":
            cover_tp = row.tp
        else:
            runner_tp = row.tp
    assert cover_tp is not None and runner_tp is not None
    assert runner_tp > cover_tp, "the runner stays beyond the 1R cover"

    again = await ex.place_pending(
        _ctx(reply_ref, source_id, reply_row),
        symbol="XAUUSDm",
        side="buy",
        order_type="limit",
        price=2360.0,
        sl=2350.0,
        tp=LADDER_TPS,
        reason="ladder reply repeated",
    )
    assert again["ok"] is False
    assert len(broker.positions) == 2
    assert broker.orders == {}

    noise = await ex.open_market(
        _ctx(noise_ref, source_id),
        symbol="XAUUSDm",
        side="buy",
        sl=0,
        reason="unrelated post",
    )
    assert noise["ok"] is True, noise["message"]
    assert len(broker.positions) == 4
    for ticket in noise["tickets"]:
        assert ticket not in chain_tickets
        assert broker.positions[ticket]["sl"] == 0.0


async def test_modify_position_attaches_a_stop_to_a_stopless_leg(data_dir: Path) -> None:
    """The path the model is told to take: modify_position, not a second open."""
    ex, broker, _runtime = await build_executor(data_dir)
    source_id, teaser_ref, _reply_ref, _noise_ref, _teaser_row, _reply_row = await _seed_chain()
    opened = await ex.open_market(
        _ctx(teaser_ref, source_id),
        symbol="XAUUSDm",
        side="buy",
        sl=0,
        tp=[],
        reason="incomplete teaser",
    )
    assert opened["ok"] is True, opened["message"]
    by_role = {leg["role"]: leg["ticket"] for leg in opened["legs"]}
    cover = await ex.modify_position(
        _ctx(teaser_ref, source_id),
        ticket=by_role["cover"],
        sl=2370.0,
        tp=2392.0,
        reason="attach cover",
    )
    runner = await ex.modify_position(
        _ctx(teaser_ref, source_id),
        ticket=by_role["runner"],
        sl=2370.0,
        tp=2410.0,
        reason="attach runner",
    )
    assert cover["ok"] is True, cover["message"]
    assert runner["ok"] is True, runner["message"]
    assert broker.positions[by_role["cover"]]["sl"] == 2370.0
    assert broker.positions[by_role["cover"]]["tp"] == 2392.0
    assert broker.positions[by_role["runner"]]["sl"] == 2370.0
    assert broker.positions[by_role["runner"]]["tp"] == 2410.0
    assert len(broker.positions) == 2


def test_reply_context_tells_the_model_to_modify_incomplete_legs() -> None:
    parent = Message(
        source_id=1,
        message_id=100,
        kind="text",
        status="processed",
        text="XAUUSD BUY",
        posted_at=datetime(2026, 9, 22, tzinfo=UTC).replace(tzinfo=None),
    )
    cover = Trade(
        ticket=5001,
        kind="position",
        symbol="XAUUSDm",
        side="buy",
        order_type=0,
        volume=0.05,
        entry_price=4294.0,
        sl=0.0,
        tp=0.0,
        status="open",
        magic=770077,
        ladder_role="cover",
        split_index=0,
        split_count=2,
    )
    text = "\n".join(_reply_section(parent, None, [cover], 100))
    assert "INCOMPLETE LEGS" in text
    assert "#5001" in text
    assert "modify_position" in text
    assert "Do not call open_market or place_pending" in text
    assert "role cover" in text


async def test_parallel_teaser_and_ladder_reply_do_not_open_four_legs(
    data_dir: Path,
) -> None:
    """Three workers are up. The reply waits for the teaser. Noise does not."""
    ex, broker, runtime = await build_executor(data_dir)
    runtime.telegram = _StubTelegram()  # type: ignore[assignment]
    ingestor = Ingestor(runtime)
    runtime.ingestor = ingestor
    async with session_scope() as session:
        session.add(
            Source(
                kind="telegram",
                chat_id=CHAT_ID,
                title="SP Demo Signals",
                username="spdemo",
                entity_type="channel",
                is_active=True,
            )
        )

    intervals: list[tuple[int, float, float]] = []
    live = 0
    peak = 0
    reply_result: dict[str, Any] = {}

    async def fake_execute(self: loop_mod._AgentRun) -> dict[str, Any]:
        nonlocal live, peak
        async with session_scope() as session:
            row = await session.get(Message, int(self.message_row_id or 0))
            assert row is not None
            reply_to = row.reply_to_message_id
            message_id = int(row.message_id)
            ref = row.signal_ref
            source_id = int(row.source_id)
            row_id = int(row.id)
        ctx = RunContext(
            run_id=self.run_id,
            signal_ref=ref,
            source_id=source_id,
            message_row_id=row_id,
            origin="agent",
        )
        live += 1
        peak = max(peak, live)
        started = time.monotonic()
        try:
            if reply_to:
                result = await ex.place_pending(
                    ctx,
                    symbol="XAUUSDm",
                    side="buy",
                    order_type="limit",
                    price=2360.0,
                    sl=2350.0,
                    tp=LADDER_TPS,
                    reason="ladder reply",
                )
                reply_result.update(result)
            elif message_id == 200:
                await asyncio.sleep(0.4)
                result = {"ok": True, "message": "noise ignored"}
            else:
                result = await ex.open_market(
                    ctx,
                    symbol="XAUUSDm",
                    side="buy",
                    sl=0,
                    tp=[],
                    reason="incomplete teaser",
                )
                assert result["ok"] is True, result["message"]
                await asyncio.sleep(0.4)
        finally:
            live -= 1
            intervals.append((message_id, started, time.monotonic()))
        return {
            "run_id": self.run_id,
            "status": "finished",
            "decision": "acted",
            "summary": str(result.get("message") or ""),
            "error": None,
        }

    original = loop_mod._AgentRun.execute
    loop_mod._AgentRun.execute = fake_execute  # type: ignore[method-assign]
    await ingestor.start()
    try:
        teaser_row = await ingestor.handle_message(text_message(100, "XAUUSD BUY"))
        noise_row = await ingestor.handle_message(text_message(200, "good morning"))
        reply_row = await ingestor.handle_message(
            text_message(101, "TP ladder #SL2350", reply_to_msg_id=100)
        )
        assert teaser_row and noise_row and reply_row
        await asyncio.wait_for(ingestor._queue.join(), timeout=10)
    finally:
        loop_mod._AgentRun.execute = original
        await ingestor.stop()

    by_message = {message_id: (enter, leave) for message_id, enter, leave in intervals}
    assert set(by_message) == {100, 101, 200}
    teaser_enter, teaser_leave = by_message[100]
    reply_enter, _reply_leave = by_message[101]
    noise_enter, noise_leave = by_message[200]
    assert reply_enter >= teaser_leave - 0.02, "the ladder reply overlapped the teaser run"
    assert noise_enter < teaser_leave and teaser_enter < noise_leave, (
        "an unrelated post was stuck behind the chain"
    )
    assert peak == 2, f"teaser, reply and noise were all in a run at once (peak={peak})"
    assert reply_result.get("action") == "modify_existing"
    assert len(broker.positions) == 2
    assert broker.orders == {}
    assert all(position["sl"] == 2350.0 and position["tp"] > 0.0 for position in broker.positions.values())
