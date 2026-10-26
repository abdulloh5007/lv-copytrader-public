"""Soft #SOFT template: deterministic two-leg open that skips the LLM."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.ai import loop as loop_mod
from app.ai.base import Capabilities, DoneDelta, TextDelta
from app.ai.soft_template import (
    SOFT_MODEL,
    SOFT_PROVIDER_KIND,
    has_soft_tag,
    parse_soft,
)
from app.db import session_scope
from app.models import Message, Run, RunStep, Source
from app.trading.executor import RunContext

from tests.test_executor import _build as build_executor
from tests.test_ingest import CHAT_ID

SOFT_SHORT = """\
#SOFT

GOLD | ЗОЛОТО | XAUUSD

ШОРТ

Зона входа: 2654.20 - 2656.50
Стоп: 2662.00
TP1: 2648.00
TP2: 2642.00
TP3: 2635.00
"""

SOFT_LONG = """\
#soft

GOLD | ЗОЛОТО | XAUUSD

ЛОНГ

Зона входа: 2640.00
Стоп: 2634.00
TP1: 2648.00
TP2: 2655.00
"""

SOFT_MISSING_TP2 = """\
#SOFT
ШОРТ
GOLD | ЗОЛОТО | XAUUSD
Зона входа: 2654.20
Стоп: 2662.00
TP1: 2648.00
"""

NO_SOFT = "XAUUSD BUY 2380 SL 2370 TP1 2390 TP2 2400"


def test_has_soft_tag_is_case_insensitive_hashtag() -> None:
    assert has_soft_tag(SOFT_SHORT) is True
    assert has_soft_tag(SOFT_LONG) is True
    assert has_soft_tag("noise #SoFt noise") is True
    assert has_soft_tag(NO_SOFT) is False
    assert has_soft_tag("talk about #software updates") is False
    assert has_soft_tag("") is False


def test_parse_soft_short_sample() -> None:
    parsed = parse_soft(SOFT_SHORT)
    assert parsed.ok is True
    assert parsed.side == "sell"
    assert parsed.symbol == "XAUUSD"
    assert parsed.entry == 2655.35  # mid of 2654.20-2656.50
    assert parsed.entry_high == 2656.50
    assert parsed.sl == 2662.0
    assert parsed.tp1 == 2648.0
    assert parsed.tp2 == 2642.0


def test_parse_soft_long_variant() -> None:
    parsed = parse_soft(SOFT_LONG)
    assert parsed.ok is True
    assert parsed.side == "buy"
    assert parsed.symbol == "XAUUSD"
    assert parsed.entry == 2640.0
    assert parsed.sl == 2634.0
    assert parsed.tp1 == 2648.0
    assert parsed.tp2 == 2655.0


def test_parse_soft_missing_tp2_fails_closed() -> None:
    parsed = parse_soft(SOFT_MISSING_TP2)
    assert parsed.ok is False
    assert parsed.reason is not None
    assert "TP2" in parsed.reason


def test_no_soft_tag_is_not_a_soft_parse_target() -> None:
    assert has_soft_tag(NO_SOFT) is False


class _ScriptedProvider:
    """Records every model call so Soft paths can prove the LLM never ran."""

    def __init__(self, text: str = "yes") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[Any],
        max_tokens: int,
        model: str,
        capabilities: Capabilities,
    ) -> Any:
        self.calls.append(
            {
                "system": system,
                "tools": list(tools),
                "max_tokens": max_tokens,
                "model": model,
            }
        )

        async def _stream() -> Any:
            yield TextDelta(text=self.text)
            yield DoneDelta(stop_reason="end_turn", assistant_blocks=[])

        return _stream()


def _install_provider(runtime: Any, provider: _ScriptedProvider) -> None:
    caps = Capabilities(
        supports_vision=True,
        supports_tools=True,
        thinking_form="adaptive",
        source="probed",
    )

    async def _provider() -> tuple[Any, str, Capabilities]:
        return provider, "operator-picked-model", caps

    runtime.provider = _provider  # type: ignore[method-assign]


async def _seed(text: str, *, message_id: int) -> tuple[int, int]:
    async with session_scope() as session:
        found = await session.execute(select(Source).where(Source.is_active.is_(True)).limit(1))
        source = found.scalars().first()
        if source is None:
            source = Source(
                kind="telegram",
                chat_id=CHAT_ID,
                title="Soft",
                username="soft",
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
            posted_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(row)
        await session.flush()
        return int(source.id), int(row.id)


async def _drive(runtime: Any, provider: _ScriptedProvider, row_id: int) -> dict[str, Any]:
    _install_provider(runtime, provider)

    async def _noop_iterate(self: loop_mod._AgentRun) -> None:
        # Soft success / fail-closed never reaches iterate. A no-Soft post that falls
        # through would hang without a finish; finish immediately for those cases.
        self.status = loop_mod.STATUS_FINISHED
        self.decision = "ignored"
        self.summary = "test iterate stub finished without tools"

    original = loop_mod._AgentRun._iterate
    loop_mod._AgentRun._iterate = _noop_iterate  # type: ignore[method-assign]
    try:
        return await loop_mod.run_agent(runtime, message_row_id=row_id, kind="signal")
    finally:
        loop_mod._AgentRun._iterate = original


async def test_soft_short_opens_two_legs_and_skips_llm(data_dir: Path) -> None:
    """Complete Soft short → two market legs (TP1/TP2), provider never called."""
    _ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 2655.00)
    provider = _ScriptedProvider("yes")
    _source, row_id = await _seed(SOFT_SHORT, message_id=501)

    result = await _drive(runtime, provider, row_id)

    assert provider.calls == [], "Soft success must skip the LLM entirely"
    assert result["status"] == "finished"
    assert result["decision"] == "acted"
    assert result["provider_kind"] == SOFT_PROVIDER_KIND
    assert result["model"] == SOFT_MODEL
    soft = result["soft_template"]
    assert soft is not None and soft["ok"] is True
    assert soft["side"] == "sell"
    assert soft["tp1"] == 2648.0
    assert soft["tp2"] == 2642.0
    assert len(broker.positions) == 2
    tps = sorted(float(row["tp"]) for row in broker.positions.values())
    assert tps == [2642.0, 2648.0]
    assert all(float(row["sl"]) == 2662.0 for row in broker.positions.values())
    volumes = sorted(float(row["volume"]) for row in broker.positions.values())
    assert volumes[0] == volumes[1]
    assert volumes[0] > 0.0

    async with session_scope() as session:
        run = await session.get(Run, result["run_id"])
        assert run is not None
        assert run.provider_kind == SOFT_PROVIDER_KIND
        assert run.model == SOFT_MODEL
        steps = list(
            (
                await session.execute(
                    select(RunStep).where(RunStep.run_id == result["run_id"])
                )
            ).scalars()
        )
    assert any(step.type == "soft_template" for step in steps)
    assert any(step.name == "open_market" and step.type == "tool_call" for step in steps)


async def test_soft_long_variant_opens_buy_legs(data_dir: Path) -> None:
    _ex, broker, runtime = await build_executor(data_dir)
    broker.move("XAUUSDm", 2640.50)
    provider = _ScriptedProvider("yes")
    _source, row_id = await _seed(SOFT_LONG, message_id=502)

    result = await _drive(runtime, provider, row_id)

    assert provider.calls == []
    assert result["decision"] == "acted"
    assert result["soft_template"]["side"] == "buy"
    assert len(broker.positions) == 2
    # Fake broker stores POSITION_TYPE; buy is 0.
    assert all(int(row["type"]) == 0 for row in broker.positions.values())
    tps = sorted(float(row["tp"]) for row in broker.positions.values())
    assert tps == [2648.0, 2655.0]


async def test_no_soft_leaves_llm_path_unchanged(data_dir: Path) -> None:
    """Without #SOFT the Soft path is a no-op and the provider is still resolved."""
    _ex, broker, runtime = await build_executor(data_dir)
    provider = _ScriptedProvider("no")
    _source, row_id = await _seed(NO_SOFT, message_id=503)

    result = await _drive(runtime, provider, row_id)

    assert result.get("soft_template") is None
    assert provider.calls, "no Soft tag must keep the existing LLM / triage path"
    assert len(broker.positions) == 0
    assert result["provider_kind"] != SOFT_PROVIDER_KIND


async def test_soft_missing_tp2_fail_closed_skips_llm(data_dir: Path) -> None:
    """Soft tag with incomplete parse → error/skip with reason, no silent LLM."""
    _ex, broker, runtime = await build_executor(data_dir)
    provider = _ScriptedProvider("yes")
    _source, row_id = await _seed(SOFT_MISSING_TP2, message_id=504)

    result = await _drive(runtime, provider, row_id)

    assert provider.calls == [], "incomplete Soft must not fall through to the LLM"
    assert result["status"] == "error"
    assert result["error"] and "TP2" in result["error"]
    assert result["provider_kind"] == SOFT_PROVIDER_KIND
    assert result["model"] == SOFT_MODEL
    assert result["soft_template"]["ok"] is False
    assert len(broker.positions) == 0
