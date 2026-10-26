"""Entry band, stale clock, and the server quote cache.

The overnight failure: fast entry skipped a SELL printed at 4343.46 because price had
left it, then the full agent market-opened about $7 away after running ~18 minutes.
These tests pin the pure decisions and the executor path both tools share.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.ai.prompts import build_context
from app.config import settings as app_settings
from app.db import session_scope
from app.events import EventBus
from app.models import Message, Source
from app.runtime import Runtime
from app.trading.entry_gate import (
    EntryLawState,
    check_open,
    classic_market_entry_law,
    distance_from_printed,
    entry_law_blocks_finish,
    fill_price,
    note_entry_tool,
    price_for_gate,
)
from app.trading.executor import RunContext
from app.trading.quote_cache import (
    QUOTE_CACHE_CAP,
    QUOTE_CACHE_FRESH_SECONDS,
    QUOTE_CACHE_INTERVAL_SECONDS,
    QUOTE_CACHE_KEEP,
    CachedQuote,
    append_quote,
    cached_quote_lines,
    capture_once,
    fresh_quote,
    load_samples,
    trim_samples,
)

from tests.test_executor import _build as build_executor
from tests.test_ingest import CHAT_ID


def test_quote_cache_cadence_and_ring_are_the_agreed_bounds() -> None:
    assert QUOTE_CACHE_INTERVAL_SECONDS == 120.0
    assert QUOTE_CACHE_CAP == 20
    assert QUOTE_CACHE_KEEP == 10
    assert QUOTE_CACHE_FRESH_SECONDS > QUOTE_CACHE_INTERVAL_SECONDS


def test_distance_uses_the_fill_side_and_the_seven_dollar_band() -> None:
    """Sell 4343.46 against a 4333.46 bid is a $10 chase, outside the $7 band."""
    assert fill_price("sell", 4336.15, 4336.40) == 4336.15
    assert fill_price("buy", 4336.15, 4336.40) == 4336.40
    overnight = distance_from_printed(4336.15, 4343.46, None)
    assert overnight is not None
    assert overnight == abs(4336.15 - 4343.46)
    assert overnight > 7.0
    ten = distance_from_printed(4333.46, 4343.46, None)
    assert ten == 10.0
    assert ten > 7.0
    assert distance_from_printed(4343.46, 4343.46, None) == 0.0
    assert distance_from_printed(4345.46, 4343.46, None) == 2.0
    inside = distance_from_printed(4315.0, None, (4310.0, 4320.0))
    assert inside == 0.0
    outside = distance_from_printed(4322.5, None, (4310.0, 4320.0))
    assert outside == 2.5


def test_a_stale_cache_is_not_a_price_and_a_fresh_one_is() -> None:
    now = 1_000_000.0
    gold = CachedQuote(
        symbol="XAUUSDm",
        bid=4343.40,
        ask=4343.65,
        mid=4343.525,
        cached_at=now - 30.0,
        server_time="",
    )
    stale = CachedQuote(
        symbol="XAUUSDm",
        bid=4343.40,
        ask=4343.65,
        mid=4343.525,
        cached_at=now - QUOTE_CACHE_FRESH_SECONDS - 1.0,
        server_time="",
    )
    other = CachedQuote(
        symbol="EURUSDm",
        bid=1.08,
        ask=1.0801,
        mid=1.08005,
        cached_at=now - 10.0,
        server_time="",
    )
    live_bid, live_ask, source = price_for_gate(
        bid=4336.15, ask=4336.40, cached=gold, symbol="XAUUSDm", now=now
    )
    assert source == "live"
    assert live_bid == 4336.15

    missing_bid, missing_ask, missing = price_for_gate(
        bid=0.0, ask=0.0, cached=stale, symbol="XAUUSDm", now=now
    )
    assert missing == "missing"
    assert missing_bid is None and missing_ask is None

    cached_bid, cached_ask, cached = price_for_gate(
        bid=0.0, ask=0.0, cached=gold, symbol="XAUUSDm", now=now
    )
    assert cached == "cache"
    assert cached_bid == 4343.40

    wrong_bid, wrong_ask, wrong = price_for_gate(
        bid=None, ask=None, cached=other, symbol="XAUUSDm", now=now
    )
    assert wrong == "missing"
    assert wrong_bid is None and wrong_ask is None


def test_trim_drops_the_oldest_ten_when_the_cache_reaches_twenty(tmp_path: Path) -> None:
    path = tmp_path / "quote-cache.json"
    for index in range(19):
        append_quote(_sample(index), path=path)
    assert len(load_samples(path)) == 19

    kept = append_quote(_sample(19), path=path)
    assert len(kept) == 10
    assert [row["cached_at"] for row in kept] == [float(index) for index in range(10, 20)]

    grown = append_quote(_sample(20), path=path)
    assert len(grown) == 11
    assert grown[-1]["cached_at"] == 20.0

    # 30 is two trims: 30 -> 20, and 20 is still at the cap so the oldest 10 go again.
    bulky = [_sample(index) for index in range(30)]
    trimmed = trim_samples(bulky)
    assert len(trimmed) == 10
    assert [row["cached_at"] for row in trimmed] == [float(index) for index in range(20, 30)]


def test_cached_quote_lines_name_symbol_bid_ask_mid_and_age(tmp_path: Path) -> None:
    path = tmp_path / "quote-cache.json"
    empty = cached_quote_lines(now=100.0, path=path)
    assert empty[0] == "=== CACHED QUOTE ==="
    assert "No cached quote yet" in empty[1]

    append_quote(
        {
            "symbol": "XAUUSDm",
            "bid": 4336.15,
            "ask": 4336.40,
            "mid": 4336.275,
            "cached_at": 50.0,
            "server_time": "2026-09-23T04:00:00",
        },
        path=path,
    )
    lines = cached_quote_lines(now=92.0, path=path)
    text = "\n".join(lines)
    assert "symbol=XAUUSDm" in text
    assert "bid=4336.15" in text
    assert "ask=4336.4" in text
    assert "mid=4336.275" in text
    assert "age_seconds=42" in text
    assert "cached_at=" in text


def _sample(index: int) -> dict[str, float | str]:
    return {
        "symbol": "XAUUSDm",
        "bid": float(index),
        "ask": float(index) + 0.25,
        "mid": float(index) + 0.125,
        "cached_at": float(index),
        "server_time": "",
    }


async def _seed(
    text: str,
    *,
    message_id: int,
    reply_to: int | None = None,
    posted_at: datetime | None = None,
) -> tuple[int, int, str]:
    async with session_scope() as session:
        found = await session.execute(select(Source).where(Source.is_active.is_(True)).limit(1))
        source = found.scalars().first()
        if source is None:
            source = Source(
                kind="telegram",
                chat_id=CHAT_ID,
                title="Gold",
                username="gold",
                entity_type="channel",
                is_active=True,
            )
            session.add(source)
            await session.flush()
        row = Message(
            source_id=int(source.id),
            message_id=message_id,
            kind="text",
            status="pending",
            text=text,
            reply_to_message_id=reply_to,
            posted_at=posted_at or datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(row)
        await session.flush()
        return int(source.id), int(row.id), row.signal_ref


def _ctx(ref: str, source_id: int, row_id: int) -> RunContext:
    return RunContext(
        run_id="a1b2c3d4e5f6",
        signal_ref=ref,
        source_id=source_id,
        message_row_id=row_id,
        origin="agent",
    )


async def test_open_market_refuses_a_fill_ten_dollars_from_the_printed_sell(data_dir: Path) -> None:
    """Overnight-style chase: SELL 4343.46, market $10 away. No position, no pending."""
    ex, broker, _runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4333.46)
    source_id, row_id, ref = await _seed("XAUUSD SELL 4343.46", message_id=900)
    result = await ex.open_market(
        _ctx(ref, source_id, row_id),
        symbol="XAUUSDm",
        side="sell",
        reason="agent chased the teaser",
    )
    assert result["ok"] is False
    assert result["stage"] == "entry_gate"
    assert result["reason"] == "far_from_entry"
    assert result["distance"] == 10.0
    assert result["distance"] > 7.0
    assert broker.positions == {}
    assert broker.orders == {}

    pending = await ex.place_pending(
        _ctx(ref, source_id, row_id),
        symbol="XAUUSDm",
        side="sell",
        order_type="limit",
        price=4343.46,
        reason="pending to catch the missed print",
    )
    assert pending["ok"] is False
    assert pending["reason"] == "far_from_entry"
    assert broker.positions == {}
    assert broker.orders == {}


async def test_a_fresh_fill_inside_seven_dollars_still_opens(data_dir: Path) -> None:
    ex, broker, _runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4342.00)
    posted = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    source_id, row_id, ref = await _seed(
        "XAUUSD SELL 4343.46", message_id=901, posted_at=posted
    )
    result = await ex.open_market(
        _ctx(ref, source_id, row_id),
        symbol="XAUUSDm",
        side="sell",
        reason="still at the print",
    )
    assert result["ok"] is True, result["message"]
    assert broker.positions
    assert broker.orders == {}


async def test_eleven_minutes_refuses_even_when_price_is_inside_the_band(data_dir: Path) -> None:
    ex, broker, _runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4342.00)
    posted = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=11)
    source_id, row_id, ref = await _seed(
        "XAUUSD SELL 4343.46", message_id=902, posted_at=posted
    )
    result = await ex.open_market(
        _ctx(ref, source_id, row_id),
        symbol="XAUUSDm",
        side="sell",
        reason="late but close",
    )
    assert result["ok"] is False
    assert result["reason"] == "stale_signal"
    assert result["age_seconds"] > 600
    assert result["age_seconds"] < 900
    assert broker.positions == {}
    assert broker.orders == {}


async def test_nine_minutes_inside_the_band_still_opens(data_dir: Path) -> None:
    ex, broker, _runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4342.00)
    posted = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=9)
    source_id, row_id, ref = await _seed(
        "XAUUSD SELL 4343.46", message_id=903, posted_at=posted
    )
    result = await ex.open_market(
        _ctx(ref, source_id, row_id),
        symbol="XAUUSDm",
        side="sell",
        reason="inside ten minutes",
    )
    assert result["ok"] is True, result["message"]
    assert broker.positions
    assert broker.orders == {}


async def test_the_stale_clock_starts_at_the_teaser_not_the_ladder_reply(data_dir: Path) -> None:
    """The ladder is one minute old. The entry was printed twenty minutes earlier."""
    ex, broker, _runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4342.00)
    teaser_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=20)
    reply_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    source_id, _teaser_row, _teaser_ref = await _seed(
        "XAUUSD SELL 4343.46", message_id=910, posted_at=teaser_at
    )
    _source, reply_row, reply_ref = await _seed(
        "TP1 4330 TP2 4320 TP3 4310 TP4 4300 TP5 4290 TP6 4280 LONG 4200 #SL4355",
        message_id=911,
        reply_to=910,
        posted_at=reply_at,
    )
    assert _source == source_id
    result = await ex.open_market(
        _ctx(reply_ref, source_id, reply_row),
        symbol="XAUUSDm",
        side="sell",
        reason="ladder arrived, try to open",
    )
    assert result["ok"] is False
    assert result["reason"] == "stale_signal"
    assert broker.positions == {}


async def test_a_stale_chain_can_still_attach_levels_to_legs_already_open(data_dir: Path) -> None:
    """Modify is not an open. Aging the teaser must not block the ladder write."""
    ex, broker, _runtime = await build_executor(data_dir)
    posted = datetime.now(UTC).replace(tzinfo=None)
    source_id, row_id, ref = await _seed("XAUUSD BUY 2380.55", message_id=920, posted_at=posted)
    opened = await ex.open_market(
        _ctx(ref, source_id, row_id),
        symbol="XAUUSDm",
        side="buy",
        sl=0,
        tp=[],
        reason="incomplete teaser",
    )
    assert opened["ok"] is True, opened["message"]
    tickets = set(opened["tickets"])
    assert len(tickets) == 2

    async with session_scope() as session:
        row = await session.get(Message, row_id)
        assert row is not None
        row.posted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=20)

    ladder = await ex.open_market(
        _ctx(ref, source_id, row_id),
        symbol="XAUUSDm",
        side="buy",
        sl=2360.0,
        tp=[2395.0, 2400.0, 2405.0, 2410.0, 2415.0, 2420.0, 2450.0],
        reason="ladder on the existing legs",
    )
    assert ladder["ok"] is True, ladder["message"]
    assert ladder["action"] == "modify_existing"
    assert set(ladder["tickets"]) == tickets
    assert len(broker.positions) == 2
    assert broker.orders == {}


async def test_capture_once_persists_the_resolved_gold_quote(data_dir: Path) -> None:
    _ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 4336.15)
    sample = await capture_once(runtime)
    assert sample is not None
    assert sample["symbol"] == "XAUUSDm"
    assert sample["bid"] == 4336.15
    assert sample["ask"] > sample["bid"]
    assert sample["mid"] == round((sample["bid"] + sample["ask"]) / 2.0, 8)
    stored = load_samples()
    assert len(stored) == 1
    assert stored[0]["symbol"] == "XAUUSDm"
    assert fresh_quote("XAUUSDm") is not None


async def test_build_context_includes_the_latest_cached_quote(data_dir: Path) -> None:
    del data_dir
    append_quote(
        {
            "symbol": "XAUUSDm",
            "bid": 4336.15,
            "ask": 4336.40,
            "mid": 4336.275,
            "cached_at": datetime.now(UTC).timestamp() - 12,
            "server_time": "2026-09-23T04:00:00",
        }
    )
    from app.db import init_db

    await init_db()
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=CHAT_ID,
            title="Gold",
            username="gold",
            entity_type="channel",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        message = Message(
            source_id=int(source.id),
            message_id=77,
            kind="text",
            status="pending",
            text="XAUUSD SELL 4343.46",
        )
        session.add(message)
        await session.flush()
        message_id = int(message.id)

    runtime = Runtime(settings=app_settings, bus=EventBus())
    async with session_scope() as session:
        message = await session.get(Message, message_id)
        assert message is not None
        messages, _meta = await build_context(session, runtime, message)
    text = " ".join(
        block.get("text", "") for block in messages[0]["content"] if block.get("type") == "text"
    )
    assert "=== CACHED QUOTE ===" in text
    assert "symbol=XAUUSDm" in text
    assert "bid=4336.15" in text
    assert "ask=4336.4" in text
    assert "mid=4336.275" in text
    assert "age_seconds=" in text


async def test_a_printed_entry_with_no_price_is_refused(data_dir: Path) -> None:
    """A dead quote and an empty cache must not skip the band."""
    from app.db import init_db

    await init_db()
    _source, row_id, ref = await _seed("XAUUSD SELL 4343.46", message_id=930)
    verdict = await check_open(
        origin="agent",
        bypass_arm=False,
        message_row_id=row_id,
        signal_ref=ref,
        symbol_name="XAUUSDm",
        pip_size=0.1,
        pip_convention="metal_gold_0.10",
        side="sell",
        bid=0.0,
        ask=0.0,
    )
    assert verdict.allow is False
    assert verdict.reason == "price_unverified"


async def test_check_open_without_a_message_does_not_block_the_executor_tests(data_dir: Path) -> None:
    from app.db import init_db

    await init_db()
    del data_dir
    verdict = await check_open(
        origin="agent",
        bypass_arm=False,
        message_row_id=None,
        signal_ref="SIG:1:4242",
        symbol_name="XAUUSDm",
        pip_size=0.1,
        pip_convention="metal_gold_0.10",
        side="buy",
        bid=2380.55,
        ask=2380.80,
    )
    assert verdict.allow is True
    assert verdict.reason == "no_signal"


def test_within_five_dollars_must_open_and_fixed_lots_forbid_a_risk_deferral() -> None:
    """SELL 4156.78 with the live bid at 4155.97 is $0.81, not a skip."""
    moved = distance_from_printed(4155.97, 4156.78, None)
    assert moved == pytest.approx(0.81)
    law = classic_market_entry_law(
        distance=moved,
        max_drift=5.0,
        fixed_lots=True,
        ladder=False,
    )
    assert law.action == "open"
    assert law.must_open is True
    assert law.within_drift is True
    assert law.defer_for_risk_forbidden is True
    assert law.skip_reason == ""

    edge = classic_market_entry_law(
        distance=5.0, max_drift=5.0, fixed_lots=True, ladder=False
    )
    assert edge.must_open is True

    inside_zone = classic_market_entry_law(
        distance=distance_from_printed(4156.0, None, (4150.0, 4160.0)),
        max_drift=5.0,
        fixed_lots=True,
        ladder=False,
    )
    assert inside_zone.price_distance == 0.0
    assert inside_zone.must_open is True

    state = EntryLawState()
    note_entry_tool(state, "evaluate_entry", {"ok": True, "must_open": True})
    blocked = entry_law_blocks_finish(
        must_open=state.must_open,
        opened=state.opened,
        hard_blocked=state.hard_blocked,
        decision="deferred",
    )
    assert blocked is not None
    assert "must be opened" in blocked
    assert "lots look large" in blocked
    assert "reward:risk to TP1" in blocked

    note_entry_tool(state, "open_market", {"ok": False, "stage": "disarmed"})
    assert entry_law_blocks_finish(
        must_open=state.must_open,
        opened=state.opened,
        hard_blocked=state.hard_blocked,
        decision="deferred",
    ) is None


def test_more_than_five_dollars_is_the_only_price_moved_skip() -> None:
    law = classic_market_entry_law(
        distance=5.01, max_drift=5.0, fixed_lots=True, ladder=False
    )
    assert law.action == "skip"
    assert law.skip_reason == "price_moved"
    assert law.must_open is False
    assert law.within_drift is False

    outside_zone = classic_market_entry_law(
        distance=distance_from_printed(4166.5, None, (4150.0, 4160.0)),
        max_drift=5.0,
        fixed_lots=True,
        ladder=False,
    )
    assert outside_zone.price_distance == pytest.approx(6.5)
    assert outside_zone.skip_reason == "price_moved"

    ladder = classic_market_entry_law(
        distance=0.81, max_drift=5.0, fixed_lots=True, ladder=True
    )
    assert ladder.action == "not_classic"
    assert ladder.must_open is False
    assert entry_law_blocks_finish(
        must_open=ladder.must_open,
        opened=False,
        hard_blocked=False,
        decision="deferred",
    ) is None


async def test_classic_open_allows_five_dollars_and_refuses_six(data_dir: Path) -> None:
    """The hard gate uses trading.max_entry_drift_usd, not the seven-dollar ladder band."""
    ex, broker, _runtime = await build_executor(data_dir)
    posted = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=20)
    source_id, row_id, ref = await _seed(
        "XAUUSD SELL 4343.46 SL 4355 TP1 4330 TP2 4320",
        message_id=940,
        posted_at=posted,
    )
    broker.move("XAUUSDm", 4337.46)
    refused = await ex.open_market(
        _ctx(ref, source_id, row_id),
        symbol="XAUUSDm",
        side="sell",
        sl=4355.0,
        tp=[4330.0, 4320.0],
        reason="six dollars is already gone",
    )
    assert refused["ok"] is False
    assert refused["reason"] == "far_from_entry"
    assert refused["distance"] == pytest.approx(6.0)
    assert refused["limit"] == pytest.approx(5.0)
    assert broker.positions == {}

    broker.move("XAUUSDm", 4338.46)
    opened = await ex.open_market(
        _ctx(ref, source_id, row_id),
        symbol="XAUUSDm",
        side="sell",
        sl=4355.0,
        tp=[4330.0, 4320.0],
        reason="five dollars is still the entry",
    )
    assert opened["ok"] is True, opened["message"]
    assert broker.positions
