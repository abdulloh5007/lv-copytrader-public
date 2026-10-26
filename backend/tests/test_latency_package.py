"""Latency package: identical edits, triage, serial broker writes, incomplete volume.

The agent loop and the ingest pool are allowed to overlap. These tests pin the pieces
that must not move with that change: an exact duplicate edit never starts a run, a clear
triage "no" never enters the tool loop, broker writes stay one at a time, and an entry
with no posted stop still uses the fixed ladder size.
"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select

from app.ai import loop as loop_mod
from app.ai.base import Capabilities, DoneDelta, TextDelta
from app.ai.triage import TRIAGE_SYSTEM, parse_triage_verdict
from app.config import (
    INGEST_WORKERS,
    MAX_TOKENS,
    MAX_TOOL_ITERATIONS,
    TRIAGE_MAX_TOKENS,
    Settings,
)
from app.db import init_db, session_scope
from app.events import EventBus
from app.models import Message, RunStep, Source
from app.telegram.ingest import IDENTICAL_EDIT_REASON
from app.trading.executor import broker_write_lock

from tests.test_executor import _build as build_executor
from tests.test_executor import _ctx
from tests.test_ingest import PNG, _build as build_ingestor
from tests.test_ingest import _messages, _png_bytes, photo_message, text_message


def test_caps_were_raised_not_lowered() -> None:
    """Fresh installs get the high full-agent budget. Triage has its own small one."""
    assert Settings.model_fields["MAX_TOKENS"].default == 32768
    assert Settings.model_fields["MAX_TOOL_ITERATIONS"].default == 32
    assert Settings.model_fields["TRIAGE_MAX_TOKENS"].default == 256
    assert Settings.model_fields["INGEST_WORKERS"].default == 3
    assert MAX_TOKENS >= 32768
    assert MAX_TOOL_ITERATIONS >= 32
    assert TRIAGE_MAX_TOKENS == 256
    assert 1 <= INGEST_WORKERS <= 3


def test_triage_reads_only_the_first_word() -> None:
    assert parse_triage_verdict("yes") == "yes"
    assert parse_triage_verdict("YES, this is an entry") == "yes"
    assert parse_triage_verdict("no.") == "no"
    assert parse_triage_verdict("n") == "no"
    assert parse_triage_verdict("maybe later") == "uncertain"
    assert parse_triage_verdict("") == "uncertain"


class _ScriptedProvider:
    """One scripted answer. Records the request the classifier actually sent."""

    kind = "openai"

    def __init__(self, text: str, *, boom: bool = False) -> None:
        self.text = text
        self.boom = boom
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
                "thinking_form": capabilities.thinking_form,
            }
        )

        async def _stream() -> Any:
            if self.boom:
                raise RuntimeError("gateway down")
            yield TextDelta(text=self.text)
            yield DoneDelta(stop_reason="end_turn", assistant_blocks=[])

        return _stream()


class _Runtime:
    def __init__(self, provider: _ScriptedProvider, model: str) -> None:
        self.settings = __import__("app.config", fromlist=["settings"]).settings
        self.bus = EventBus()
        self.executor = object()
        self._provider = provider
        self._model = model
        self._caps = Capabilities(
            supports_vision=True,
            supports_tools=True,
            thinking_form="adaptive",
            source="probed",
        )

    async def armed(self) -> bool:
        return True

    async def provider(self) -> tuple[Any, str, Capabilities]:
        return self._provider, self._model, self._caps


async def _seed_message(text: str) -> int:
    await init_db()
    async with session_scope() as session:
        source = Source(
            kind="telegram",
            chat_id=-100123,
            title="Gold",
            username="gold",
            entity_type="channel",
            is_active=True,
        )
        session.add(source)
        await session.flush()
        row = Message(
            source_id=int(source.id),
            message_id=19902,
            edit_date=0,
            revision=1,
            kind="text",
            status="processing",
            text=text,
        )
        session.add(row)
        await session.flush()
        return int(row.id)


async def _drive(data_dir: Path, provider: _ScriptedProvider, *, kind: str, text: str) -> dict[str, Any]:
    """Run one agent with the tool loop replaced by a flag, so a skip is observable."""
    del data_dir
    ran: list[str] = []

    async def _iterate(self: loop_mod._AgentRun) -> None:
        ran.append(self.kind)
        await self._finalize("finished", decision="acted", summary="full loop")

    original = loop_mod._AgentRun._iterate
    loop_mod._AgentRun._iterate = _iterate  # type: ignore[method-assign]
    try:
        await init_db()
        row_id = await _seed_message(text) if kind == "signal" else None
        runtime = _Runtime(provider, "operator-picked-model")
        result = await loop_mod.run_agent(
            runtime,  # type: ignore[arg-type]
            message_row_id=row_id,
            kind=kind,
            instruction=None if kind == "signal" else "attach a stop to #500001",
        )
    finally:
        loop_mod._AgentRun._iterate = original
    result["ran_full_loop"] = ran
    return result


async def test_triage_no_finishes_ignored_without_the_tool_loop(data_dir: Path) -> None:
    provider = _ScriptedProvider("no")
    result = await _drive(data_dir, provider, kind="signal", text="congrats, target smashed")
    assert result["status"] == "finished"
    assert result["decision"] == "ignored"
    assert result["iterations"] == 0
    assert result["ran_full_loop"] == []
    assert "tool loop did not run" in (result["summary"] or "")
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["model"] == "operator-picked-model"
    assert call["tools"] == []
    assert call["thinking_form"] is None
    assert call["max_tokens"] == TRIAGE_MAX_TOKENS
    assert call["system"] == TRIAGE_SYSTEM
    assert "atria" not in call["model"].lower()

    async with session_scope() as session:
        steps = list((await session.execute(select(RunStep).where(RunStep.name == "triage"))).scalars())
    assert len(steps) == 1
    assert steps[0].payload["verdict"] == "no"
    assert steps[0].payload["max_tokens"] == MAX_TOKENS
    assert steps[0].payload["max_tokens"] > TRIAGE_MAX_TOKENS


async def test_triage_yes_continues_into_the_full_loop_on_the_same_model(data_dir: Path) -> None:
    provider = _ScriptedProvider("yes")
    result = await _drive(data_dir, provider, kind="signal", text="XAUUSD BUY 4347")
    assert result["decision"] == "acted"
    assert result["ran_full_loop"] == ["signal"]
    assert len(provider.calls) == 1
    assert provider.calls[0]["model"] == "operator-picked-model"
    assert provider.calls[0]["tools"] == []


async def test_triage_error_fails_open_into_the_full_loop(data_dir: Path) -> None:
    provider = _ScriptedProvider("no", boom=True)
    result = await _drive(data_dir, provider, kind="signal", text="XAUUSD BUY 4347")
    assert result["ran_full_loop"] == ["signal"]
    assert result["decision"] == "acted"
    async with session_scope() as session:
        step = (await session.execute(select(RunStep).where(RunStep.name == "triage"))).scalars().one()
    assert step.payload["verdict"] == "uncertain"


async def test_watchdog_skips_triage(data_dir: Path) -> None:
    provider = _ScriptedProvider("no")
    result = await _drive(data_dir, provider, kind="sl_watchdog", text="")
    assert provider.calls == []
    assert result["ran_full_loop"] == ["sl_watchdog"]


async def test_identical_text_edit_is_not_enqueued(data_dir: Path) -> None:
    ingestor, runtime, source_id = await build_ingestor(data_dir)
    posted = datetime.now(UTC)
    first = await ingestor.handle_message(text_message(42, "XAUUSD BUY 4347", date=posted))
    same = text_message(
        42, "XAUUSD BUY 4347", date=posted, edit_date=posted + timedelta(seconds=10)
    )
    second = await ingestor.handle_message(same, edited=True)
    spaced = text_message(
        42, "XAUUSD BUY 4347 ", date=posted, edit_date=posted + timedelta(seconds=20)
    )
    third = await ingestor.handle_message(spaced, edited=True)

    assert first is not None and second is not None and third is not None
    rows = [row for row in await _messages() if row.message_id == 42]
    assert [row.revision for row in rows] == [1, 2, 3]
    assert [row.status for row in rows] == ["pending", "skipped", "pending"]
    assert rows[1].skip_reason == IDENTICAL_EDIT_REASON
    assert rows[1].text == rows[0].text
    assert ingestor._queue.qsize() == 2, "the identical edit never reaches a worker"
    emitted = [e["data"] for e in runtime.bus.recent() if e["type"] == "signal.received"]
    assert emitted[1]["status"] == "skipped"
    assert emitted[1]["skip_reason"] == IDENTICAL_EDIT_REASON
    del source_id


async def test_a_real_edit_after_an_identical_skip_continues_the_chain(data_dir: Path) -> None:
    ingestor, _runtime, _source_id = await build_ingestor(data_dir)
    posted = datetime.now(UTC)
    await ingestor.handle_message(text_message(7, "XAUUSD BUY 4347", date=posted))
    await ingestor.handle_message(
        text_message(
            7, "XAUUSD BUY 4347", date=posted, edit_date=posted + timedelta(seconds=10)
        ),
        edited=True,
    )
    await ingestor.handle_message(
        text_message(
            7,
            "XAUUSD BUY 4347\n#SL 4340",
            date=posted,
            edit_date=posted + timedelta(seconds=20),
        ),
        edited=True,
    )
    rows = await _messages()
    assert [row.revision for row in rows] == [1, 2, 3]
    assert rows[2].status == "pending"
    assert rows[2].skip_reason is None
    assert ingestor._queue.qsize() == 2


async def test_celebration_text_is_still_enqueued(data_dir: Path) -> None:
    """No wording filter. A celebration is a normal post until triage says no."""
    ingestor, _runtime, _source_id = await build_ingestor(data_dir)
    row_id = await ingestor.handle_message(
        text_message(8, "TP1 HIT congratulations team")
    )
    assert row_id is not None
    rows = await _messages()
    assert rows[0].status == "pending"
    assert rows[0].skip_reason is None
    assert ingestor._queue.qsize() == 1


def _other_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


class _SwitchingTelegram:
    """Writes whichever PNG the test has armed, so two edits can differ by bytes."""

    def __init__(self) -> None:
        self.payload = PNG
        self.downloads: list[int] = []
        self._n = 0

    async def download_image(self, message: Any, dest_dir: str) -> str | None:
        self._n += 1
        self.downloads.append(int(getattr(message, "id", 0) or 0))
        path = Path(dest_dir) / f"switch-{self._n:03d}.png"
        path.write_bytes(self.payload)
        return str(path)


async def test_identical_image_bytes_skip_and_new_bytes_enqueue(data_dir: Path) -> None:
    ingestor, runtime, _source_id = await build_ingestor(data_dir)
    telegram = _SwitchingTelegram()
    runtime.telegram = telegram
    posted = datetime.now(UTC)
    caption = "XAUUSD BUY 4347"
    await ingestor.handle_message(photo_message(11, caption=caption, date=posted))
    same = photo_message(
        11, caption=caption, date=posted, edit_date=posted + timedelta(seconds=10)
    )
    await ingestor.handle_message(same, edited=True)
    telegram.payload = _other_png()
    changed = photo_message(
        11, caption=caption, date=posted, edit_date=posted + timedelta(seconds=20)
    )
    await ingestor.handle_message(changed, edited=True)

    rows = await _messages()
    assert [row.status for row in rows] == ["pending", "skipped", "pending"]
    assert rows[1].skip_reason == IDENTICAL_EDIT_REASON
    assert ingestor._queue.qsize() == 2
    assert _png_bytes() != _other_png()


class _MissingFileTelegram:
    async def download_image(self, message: Any, dest_dir: str) -> str | None:
        del message
        return str(Path(dest_dir) / "not-written.png")


async def test_unreadable_new_image_fails_open(data_dir: Path) -> None:
    ingestor, runtime, _source_id = await build_ingestor(data_dir)
    posted = datetime.now(UTC)
    caption = "XAUUSD BUY 4347"
    await ingestor.handle_message(photo_message(12, caption=caption, date=posted))
    runtime.telegram = _MissingFileTelegram()
    edited = photo_message(
        12, caption=caption, date=posted, edit_date=posted + timedelta(seconds=10)
    )
    await ingestor.handle_message(edited, edited=True)
    rows = await _messages()
    assert rows[1].status == "pending"
    assert rows[1].skip_reason is None
    assert ingestor._queue.qsize() == 2


async def test_open_without_sl_on_netting_is_one_leg(data_dir: Path) -> None:
    ex, broker, _runtime = await build_executor(data_dir, margin_mode=0)
    result = await ex.open_market(
        _ctx(signal_ref="SIG:2:1"),
        symbol="XAUUSDm",
        side="buy",
        reason="teaser on netting",
    )
    assert result["ok"] is True, result["message"]
    assert result["kind"] == "incomplete"
    assert len(broker.positions) == 1
    position = next(iter(broker.positions.values()))
    assert position["volume"] == pytest.approx(0.10)
    assert position["sl"] == 0.0
    assert position["tp"] == 0.0


async def test_volume_min_collapses_an_incomplete_entry_to_one_leg(data_dir: Path) -> None:
    ex, broker, _runtime = await build_executor(data_dir)
    broker.symbols["XAUUSDm"]["volume_min"] = 0.10
    result = await ex.open_market(
        _ctx(signal_ref="SIG:2:2"),
        symbol="XAUUSDm",
        side="buy",
        reason="broker cannot split 0.10",
    )
    assert result["ok"] is True, result["message"]
    assert len(broker.positions) == 1
    assert next(iter(broker.positions.values()))["volume"] == pytest.approx(0.10)
    assert result["legs"][0]["role"] == "cover"


async def test_place_pending_without_sl_is_accepted(data_dir: Path) -> None:
    ex, broker, _runtime = await build_executor(data_dir)
    result = await ex.place_pending(
        _ctx(signal_ref="SIG:2:3"),
        symbol="XAUUSDm",
        side="buy",
        order_type="limit",
        price=2360.0,
        reason="limit teaser, stop later",
    )
    assert result["ok"] is True, result["message"]
    assert result["kind"] == "incomplete"
    assert result["sl"] == 0.0
    assert result["volume_source"] == "ladder_volume"
    assert len(broker.orders) == 2
    assert all(order["sl"] == 0.0 for order in broker.orders.values())


async def test_dispatch_open_market_without_an_sl_key(data_dir: Path) -> None:
    ex, broker, _runtime = await build_executor(data_dir)
    result = await ex.dispatch(
        "open_market",
        {"symbol": "XAUUSDm", "side": "buy", "reason": "schema omits sl"},
        _ctx(signal_ref="SIG:2:4"),
    )
    assert result["ok"] is True, result["message"]
    assert result["kind"] == "incomplete"
    assert len(broker.positions) == 2


async def test_two_opens_do_not_overlap_inside_order_send(data_dir: Path) -> None:
    ex, broker, runtime = await build_executor(data_dir)
    original = runtime.rpc.order_send
    active = 0
    peak = 0

    async def wrapped(request: dict[str, Any]) -> Any:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        try:
            return await original(request)
        finally:
            active -= 1

    runtime.rpc.order_send = wrapped
    first, second = await asyncio.gather(
        ex.open_market(
            _ctx(signal_ref="SIG:3:1"),
            symbol="XAUUSDm",
            side="buy",
            sl=2370.0,
            volume=0.10,
            tp=[],
            reason="first",
        ),
        ex.open_market(
            _ctx(signal_ref="SIG:3:2"),
            symbol="XAUUSDm",
            side="sell",
            sl=2390.0,
            volume=0.10,
            tp=[],
            reason="second",
        ),
    )
    assert first["ok"] is True, first["message"]
    assert second["ok"] is True, second["message"]
    assert peak == 1, f"order_send overlapped (peak={peak})"
    assert len(broker.positions) == 2


async def test_the_write_lock_reenters_for_the_same_task(data_dir: Path) -> None:
    ex, broker, _runtime = await build_executor(data_dir)
    opened = await ex.open_market(
        _ctx(signal_ref="SIG:3:3"),
        symbol="XAUUSDm",
        side="buy",
        sl=2370.0,
        volume=0.10,
        tp=[],
        reason="open",
    )
    assert opened["ok"] is True, opened["message"]
    ticket = opened["tickets"][0]
    async with broker_write_lock():
        moved = await asyncio.wait_for(
            ex.modify_position(_ctx(signal_ref="SIG:3:3"), ticket=ticket, sl=2365.0, reason="tighten"),
            timeout=2.0,
        )
    assert moved["ok"] is True, moved["message"]
    assert broker.positions[ticket]["sl"] == pytest.approx(2365.0, abs=0.05)
