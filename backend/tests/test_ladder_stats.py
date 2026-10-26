"""Server-side ladder outcomes: classify, aggregate last-N, persist, inject the blurb.

The operator rule is that the model must never recount channel history to invent
raw_SL / BE_stopouts / TP4+. These tests pin the server as the only source of those numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.ai.prompts import build_context
from app.config import settings as app_settings
from app.db import init_db, session_scope, set_setting
from app.events import EventBus
from app.models import LadderOutcome, Message, Source, Trade
from app.runtime import Runtime
from app.trading.ladder_stats import (
    KEY_STATS_WINDOW,
    MODE_BE_AFTER_COVER,
    MODE_HOLD_CHANNEL_SL,
    OUTCOME_BE_STOPOUT,
    OUTCOME_OTHER,
    OUTCOME_RAW_SL,
    OUTCOME_TP4_PLUS,
    LadderLegView,
    aggregate_kinds,
    blurb_for,
    classify_group,
    maybe_record_closed_group,
    normalize_runner_mode,
    runner_hit_chosen_tp,
    stats_for_source,
    suggest_mode,
    suggest_runner_tp_bias,
)


def test_suggest_mode_bootstraps_to_be_until_the_window_is_full() -> None:
    """Fewer than N closed ladders stay on the safer trail."""
    assert suggest_mode([], window=5) == MODE_BE_AFTER_COVER
    assert suggest_mode([OUTCOME_TP4_PLUS] * 4, window=5) == MODE_BE_AFTER_COVER
    assert suggest_mode([OUTCOME_TP4_PLUS] * 5, window=5) == MODE_HOLD_CHANNEL_SL


def test_any_raw_sl_in_the_window_resets_to_be() -> None:
    kinds = [OUTCOME_TP4_PLUS, OUTCOME_BE_STOPOUT, OUTCOME_RAW_SL, OUTCOME_TP4_PLUS, OUTCOME_TP4_PLUS]
    assert suggest_mode(kinds, window=5) == MODE_BE_AFTER_COVER
    stats = aggregate_kinds(kinds, window=5, source_title="XAUUADTRADINGMASTER")
    assert stats.raw_sl_hits == 1
    assert stats.be_stopouts == 1
    assert stats.tp4_plus == 3
    assert stats.clean_streak == 2
    assert stats.suggested_mode == MODE_BE_AFTER_COVER
    assert stats.bootstrap is False


def test_five_clean_ladders_suggest_hold_channel_sl() -> None:
    kinds = [OUTCOME_TP4_PLUS, OUTCOME_BE_STOPOUT, OUTCOME_TP4_PLUS, OUTCOME_OTHER, OUTCOME_TP4_PLUS]
    stats = aggregate_kinds(kinds, window=5, source_title="Gold Signals")
    assert stats.raw_sl_hits == 0
    assert stats.suggested_mode == MODE_HOLD_CHANNEL_SL
    assert stats.clean_streak == 5
    blurb = blurb_for(stats)
    assert "raw_SL=0" in blurb
    assert "BE_stopouts=1" in blurb
    assert "TP4+=3" in blurb
    assert "Suggested: hold_channel_sl (streak 5)" in blurb
    assert "Runner-TP bias +1" in blurb
    assert "Override only with reason." in blurb
    assert "Bootstrap" not in blurb
    assert stats.suggested_runner_tp_bias == 1


def test_bootstrap_blurb_documents_the_safer_default() -> None:
    stats = aggregate_kinds([OUTCOME_TP4_PLUS, OUTCOME_TP4_PLUS], window=5, source_title="Gold")
    assert stats.bootstrap is True
    assert stats.suggested_mode == MODE_BE_AFTER_COVER
    blurb = stats.blurb()
    assert "Bootstrap: only 2/5 closed ladders" in blurb
    assert MODE_BE_AFTER_COVER in blurb
    assert "runner-TP bias stays +0" in blurb
    assert stats.suggested_runner_tp_bias == 0


def test_runner_tp_bias_table_is_deterministic() -> None:
    """Bootstrap 0; raw_SL 0; streak 3–4 → +1; strong TP4+ streak → +2. No random."""
    assert (
        suggest_runner_tp_bias(
            sample_size=2, window=5, raw_sl_hits=0, tp4_plus=2, clean_streak=2, bootstrap=True
        )
        == 0
    )
    assert (
        suggest_runner_tp_bias(
            sample_size=5, window=5, raw_sl_hits=1, tp4_plus=4, clean_streak=2
        )
        == 0
    )
    assert (
        suggest_runner_tp_bias(
            sample_size=5, window=5, raw_sl_hits=0, tp4_plus=1, clean_streak=4
        )
        == 1
    )
    assert (
        suggest_runner_tp_bias(
            sample_size=5, window=5, raw_sl_hits=0, tp4_plus=3, clean_streak=5
        )
        == 1
    )
    assert (
        suggest_runner_tp_bias(
            sample_size=5, window=5, raw_sl_hits=0, tp4_plus=4, clean_streak=5
        )
        == 2
    )
    strong = aggregate_kinds([OUTCOME_TP4_PLUS] * 5, window=5)
    assert strong.suggested_runner_tp_bias == 2
    raw = aggregate_kinds(
        [OUTCOME_TP4_PLUS, OUTCOME_RAW_SL, OUTCOME_TP4_PLUS, OUTCOME_TP4_PLUS, OUTCOME_TP4_PLUS],
        window=5,
    )
    assert raw.raw_sl_hits == 1
    assert raw.suggested_runner_tp_bias == 0


def test_runner_hit_chosen_tp_is_true_only_on_the_stored_tp() -> None:
    hit = LadderLegView(
        role="runner",
        side="buy",
        entry=2380.0,
        sl=2367.0,
        tp=2392.0,
        close_cause="tp",
        close_price=2392.0,
        profit=12.0,
    )
    miss = LadderLegView(
        role="runner",
        side="buy",
        entry=2380.0,
        sl=2367.0,
        tp=2392.0,
        close_cause="sl",
        close_price=2367.0,
        profit=-13.0,
    )
    cover = LadderLegView(
        role="cover",
        side="buy",
        entry=2380.0,
        sl=2367.0,
        tp=2393.0,
        close_cause="tp",
        close_price=2393.0,
        profit=13.0,
    )
    assert runner_hit_chosen_tp(hit) is True
    assert runner_hit_chosen_tp(miss) is False
    assert runner_hit_chosen_tp(cover) is False


def test_normalize_runner_mode_accepts_only_the_two_tokens() -> None:
    assert normalize_runner_mode("be_after_cover") == MODE_BE_AFTER_COVER
    assert normalize_runner_mode("HOLD_CHANNEL_SL") == MODE_HOLD_CHANNEL_SL
    assert normalize_runner_mode("trail") is None
    assert normalize_runner_mode("") is None
    assert normalize_runner_mode(None) is None


def test_classify_runner_tp_is_tp4_plus() -> None:
    runner = LadderLegView(
        role="runner",
        side="buy",
        entry=2380.0,
        sl=2367.0,
        tp=2395.0,
        close_cause="tp",
        close_price=2395.0,
        profit=150.0,
    )
    cover = LadderLegView(role="cover", side="buy", entry=2380.0, sl=2367.0, tp=2393.0, close_cause="tp")
    assert classify_group([cover, runner]) == OUTCOME_TP4_PLUS


def test_classify_runner_stopped_at_breakeven() -> None:
    runner = LadderLegView(
        role="runner",
        side="buy",
        entry=2380.0,
        sl=2380.02,
        tp=2395.0,
        close_cause="sl",
        close_price=2380.02,
        profit=0.5,
    )
    assert classify_group([runner]) == OUTCOME_BE_STOPOUT


def test_classify_runner_raw_channel_sl_loss() -> None:
    runner = LadderLegView(
        role="runner",
        side="buy",
        entry=2380.0,
        sl=2367.0,
        tp=2395.0,
        close_cause="sl",
        close_price=2367.0,
        profit=-130.0,
    )
    assert classify_group([runner]) == OUTCOME_RAW_SL


def test_classify_cover_only_1r_tp_is_not_tp4_plus() -> None:
    """Netting places only the cover ticket; its 1R TP is not a far-target hit."""
    cover = LadderLegView(
        role="cover",
        side="buy",
        entry=2380.0,
        sl=2367.0,
        tp=2393.0,
        close_cause="tp",
        close_price=2393.0,
        profit=13.0,
        unmanaged_tps=(2395.0, 2411.0),
        split_count=1,
    )
    assert classify_group([cover]) == OUTCOME_OTHER


def test_classify_ai_close_is_other() -> None:
    runner = LadderLegView(
        role="runner",
        side="buy",
        entry=2380.0,
        sl=2367.0,
        tp=2395.0,
        close_cause="ai",
        close_price=2388.0,
        profit=8.0,
    )
    assert classify_group([runner]) == OUTCOME_OTHER


def _trade_kwargs(**kw: object) -> dict[str, object]:
    values: dict[str, object] = dict(
        kind="position",
        symbol="XAUUSDm",
        side="buy",
        order_type=0,
        volume=0.05,
        entry_price=2380.0,
        sl=2367.0,
        tp=2393.0,
        status="closed",
        magic=int(app_settings.MAGIC_NUMBER),
        comment="SP:deadbeef",
        external=False,
        split_index=0,
        split_count=2,
        unmanaged_tps=[2395.0, 2411.0],
        close_cause="tp",
        close_price=2393.0,
        profit=13.0,
    )
    values.update(kw)
    return values


async def test_maybe_record_writes_one_row_when_both_legs_are_closed(data_dir: Path) -> None:
    await init_db()
    ref = "SIG:1:8801"
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=-1001,
            title="Gold Signals",
            username="goldsignals",
            entity_type="channel",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        session.add(
            Trade(
                **_trade_kwargs(
                    source_id=source.id,
                    signal_ref=ref,
                    ticket=88011,
                    ladder_role="cover",
                    runner_mode=MODE_BE_AFTER_COVER,
                    suggested_runner_mode=MODE_BE_AFTER_COVER,
                    suggested_runner_tp_index=4,
                    chosen_runner_tp_index=4,
                    runner_tp_bias=0,
                    close_cause="tp",
                    tp=2393.0,
                    close_price=2393.0,
                    profit=13.0,
                )
            )
        )
        session.add(
            Trade(
                **_trade_kwargs(
                    source_id=source.id,
                    signal_ref=ref,
                    ticket=88012,
                    split_index=1,
                    ladder_role="runner",
                    runner_mode=MODE_BE_AFTER_COVER,
                    suggested_runner_mode=MODE_BE_AFTER_COVER,
                    suggested_runner_tp_index=4,
                    chosen_runner_tp_index=4,
                    runner_tp_bias=0,
                    sl=2367.0,
                    tp=2395.0,
                    close_cause="sl",
                    close_price=2367.0,
                    profit=-65.0,
                )
            )
        )
        row = await maybe_record_closed_group(session, signal_ref=ref)
        assert row is not None
        assert row.kind == OUTCOME_RAW_SL
        assert row.runner_mode == MODE_BE_AFTER_COVER
        assert row.runner_hit_chosen_tp is False
        assert row.chosen_runner_tp_index == 4
        assert row.suggested_runner_tp_index == 4
        assert row.runner_tp_bias == 0
        again = await maybe_record_closed_group(session, signal_ref=ref)
        assert again is not None
        assert again.id == row.id

    async with session_scope() as session:
        found = list((await session.execute(select(LadderOutcome))).scalars().all())
    assert len(found) == 1
    assert found[0].signal_ref == ref
    assert found[0].kind == OUTCOME_RAW_SL


async def test_maybe_record_skips_a_group_that_still_has_a_live_leg(data_dir: Path) -> None:
    await init_db()
    ref = "SIG:1:8802"
    async with session_scope() as session:
        session.add(
            Trade(
                **_trade_kwargs(
                    signal_ref=ref,
                    ticket=88021,
                    ladder_role="cover",
                    status="closed",
                )
            )
        )
        session.add(
            Trade(
                **_trade_kwargs(
                    signal_ref=ref,
                    ticket=88022,
                    split_index=1,
                    ladder_role="runner",
                    status="open",
                    tp=2395.0,
                    close_cause=None,
                    close_price=None,
                    profit=None,
                )
            )
        )
        assert await maybe_record_closed_group(session, signal_ref=ref) is None
        count = (await session.execute(select(LadderOutcome))).scalars().all()
        assert count == []


async def test_stats_for_source_uses_last_n_newest_first(data_dir: Path) -> None:
    await init_db()
    base = datetime(2026, 1, 1)
    kinds_oldest_first = [
        OUTCOME_RAW_SL,
        OUTCOME_TP4_PLUS,
        OUTCOME_TP4_PLUS,
        OUTCOME_BE_STOPOUT,
        OUTCOME_TP4_PLUS,
        OUTCOME_TP4_PLUS,
    ]
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=-1002,
            title="XAUUADTRADINGMASTER",
            username="xau",
            entity_type="channel",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        sid = int(source.id)
        for index, kind in enumerate(kinds_oldest_first):
            session.add(
                LadderOutcome(
                    source_id=sid,
                    signal_ref=f"SIG:{sid}:{3000 + index}",
                    symbol="XAUUSDm",
                    side="buy",
                    kind=kind,
                    closed_at=base + timedelta(hours=index),
                )
            )
        other = Source(
            kind="telegram",
            chat_id=-1003,
            title="Other",
            username="other",
            entity_type="channel",
            is_active=False,
        )
        session.add(other)
        await session.flush()
        session.add(
            LadderOutcome(
                source_id=other.id,
                signal_ref=f"SIG:{other.id}:1",
                kind=OUTCOME_RAW_SL,
                closed_at=base + timedelta(days=9),
            )
        )

    async with session_scope() as session:
        stats = await stats_for_source(session, sid)
    # Last 5 newest-first drop the oldest raw_sl_hit.
    assert stats.sample_size == 5
    assert stats.raw_sl_hits == 0
    assert stats.tp4_plus == 4
    assert stats.be_stopouts == 1
    assert stats.suggested_mode == MODE_HOLD_CHANNEL_SL
    assert stats.clean_streak == 5
    assert stats.bootstrap is False
    assert "Suggested: hold_channel_sl" in stats.blurb()


async def test_window_setting_shortens_the_lookback(data_dir: Path) -> None:
    await init_db()
    async with session_scope() as session:
        await set_setting(session, KEY_STATS_WINDOW, 3)
        source = Source(
            kind="telegram",
            chat_id=-1004,
            title="Gold",
            username="gold",
            entity_type="channel",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        sid = int(source.id)
        base = datetime(2026, 2, 1)
        for index, kind in enumerate([OUTCOME_RAW_SL, OUTCOME_TP4_PLUS, OUTCOME_TP4_PLUS, OUTCOME_TP4_PLUS]):
            session.add(
                LadderOutcome(
                    source_id=sid,
                    signal_ref=f"SIG:{sid}:{4000 + index}",
                    kind=kind,
                    closed_at=base + timedelta(hours=index),
                )
            )
        stats = await stats_for_source(session, sid)
    assert stats.window == 3
    assert stats.raw_sl_hits == 0
    assert stats.suggested_mode == MODE_HOLD_CHANNEL_SL


async def test_build_context_injects_the_server_blurb_without_history_recount(
    data_dir: Path,
) -> None:
    """The user turn carries the ready-made blurb; it does not ask the model to recount posts."""
    await init_db()
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=-1005,
            title="XAUUADTRADINGMASTER",
            username="xau",
            entity_type="channel",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        sid = int(source.id)
        base = datetime(2026, 3, 1)
        for index in range(5):
            session.add(
                LadderOutcome(
                    source_id=sid,
                    signal_ref=f"SIG:{sid}:{5000 + index}",
                    kind=OUTCOME_TP4_PLUS if index < 4 else OUTCOME_BE_STOPOUT,
                    closed_at=base + timedelta(hours=index),
                )
            )
        message = Message(
            source_id=sid,
            message_id=42,
            edit_date=0,
            revision=1,
            kind="text",
            status="pending",
            text="BUY GOLD 2380 SL 2367 TP1 2383 TP2 2386 TP3 2389 TP4 2392 TP5 2395 LONG 2411",
        )
        session.add(message)
        await session.flush()
        message_id = int(message.id)

    runtime = Runtime(settings=app_settings, bus=EventBus())
    async with session_scope() as session:
        message = await session.get(Message, message_id)
        assert message is not None
        messages, _meta = await build_context(session, runtime, message)

    assert messages
    content = messages[0]["content"]
    text = " ".join(block.get("text", "") for block in content if block.get("type") == "text")
    assert "CHANNEL LADDER STATS (server-computed; do not recount history)" in text
    assert "raw_SL=0" in text
    assert "BE_stopouts=1" in text
    assert "TP4+=4" in text
    assert "Suggested: hold_channel_sl" in text
    assert "Override only with reason." in text
    assert "do not invent" in text.lower() or "Do not invent" in text
    # The recent-posts section exists for follow-ups, not as the stats source.
    assert "RECENT POSTS FROM THIS SOURCE" in text
    assert "get_recent_signals" in text
