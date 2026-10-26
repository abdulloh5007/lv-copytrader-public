"""Channel reviewer: one-shot brief, backup isolation, triage injection, separate slot."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from app.ai.base import Capabilities, DoneDelta, TextDelta
from app.ai.loop import _concurrent_run_limit, run_slots
from app.ai.triage import TRIAGE_SYSTEM, classify_signal, system_for_triage
from app.config import Settings
from app.config import settings as app_settings
from app.db import init_db, session_scope, set_setting
from app.operator_settings import KEY_REVIEWER_ENABLED
from app.events import EventBus
from app.models import Message, Source
from app.reviewer import worker as reviewer_worker
from app.reviewer.archive import backup_current, clip_lines, read_meta, write_ready
from app.reviewer.brief import (
    BriefIsolationError,
    active_brief_path,
    assert_copytrader_readable,
    load_active_brief,
)
from app.reviewer.layout import BACKUP_DIR, ROOT_DIR
from app.reviewer.study import history_days
from app.reviewer.worker import TASK_NAME, ChannelReviewer
from app.telegram.client import TelegramService


def test_brief_defaults() -> None:
    assert Settings.model_fields["CHANNEL_BRIEF_MAX_LINES"].default == 60
    assert Settings.model_fields["CHANNEL_BRIEF_HISTORY_DAYS"].default == 4
    assert Settings.model_fields["CHANNEL_BRIEF_MIN_MESSAGES"].default == 12
    assert history_days(4) == 4
    assert history_days(3) == 3
    assert history_days(99) == 4
    assert history_days(1) == 3


def test_reviewer_module_does_not_take_a_signal_slot() -> None:
    source = inspect.getsource(reviewer_worker)
    assert "run_slots" not in source
    assert TASK_NAME == "channel_reviewer"


def test_clip_lines_stops_at_sixty() -> None:
    text = "\n".join(f"line {index}" for index in range(80))
    clipped = clip_lines(text, 60)
    assert len(clipped.splitlines()) == 60
    assert "line 59" in clipped
    assert "line 60" not in clipped


def test_active_loader_never_returns_a_backup(tmp_path: Path) -> None:
    data = tmp_path / "data"
    secret = "BACKUP-SECRET-PHRASE"
    playbook = "# Channel playbook\n## Skip (noise)\ncelebration with TP smashed is noise\n"
    write_ready(
        data,
        brief=playbook,
        meta={"chat_id": -1001, "source_id": 1, "status": "ready", "attempts": 0},
        study={"origin": "db", "messages": [{"message_id": 1, "text": "secret study"}]},
        max_lines=60,
    )
    backup = backup_current(data)
    assert backup is not None
    (backup / "brief.md").write_text(secret + "\n", encoding="utf-8")
    write_ready(
        data,
        brief=playbook,
        meta={"chat_id": -1002, "source_id": 2, "status": "ready", "attempts": 0},
        study={"origin": "db", "messages": []},
        max_lines=60,
    )

    opened: list[str] = []
    original = Path.read_text

    def _spy(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(str(self))
        return original(self, *args, **kwargs)

    Path.read_text = _spy  # type: ignore[method-assign]
    try:
        loaded = load_active_brief(data)
    finally:
        Path.read_text = original  # type: ignore[method-assign]

    assert "TP smashed" in loaded
    assert secret not in loaded
    assert opened
    assert all(BACKUP_DIR not in Path(path).parts for path in opened)
    assert all("reviewer" not in Path(path).parts for path in opened)
    with pytest.raises(BriefIsolationError):
        assert_copytrader_readable(backup / "brief.md", data)

    link = active_brief_path(data)
    link.unlink()
    link.symlink_to(backup / "brief.md")
    with pytest.raises(BriefIsolationError):
        load_active_brief(data)


def test_triage_prompt_includes_only_the_active_brief(tmp_path: Path) -> None:
    data = tmp_path / "data"
    playbook = "# Channel playbook\n## Skip (noise)\nSkip posts that only say target smashed.\n"
    write_ready(
        data,
        brief=playbook,
        meta={"chat_id": 7, "source_id": 1, "status": "ready"},
        study={"origin": "db", "messages": []},
        max_lines=60,
    )
    backup = data / ROOT_DIR / BACKUP_DIR / "c1_old"
    backup.mkdir(parents=True)
    (backup / "brief.md").write_text("BACKUP-SECRET-PHRASE\n", encoding="utf-8")

    system = system_for_triage(str(data))
    assert system.startswith(TRIAGE_SYSTEM)
    assert "target smashed" in system
    assert "BACKUP-SECRET-PHRASE" not in system
    assert system_for_triage(str(tmp_path / "empty")) == TRIAGE_SYSTEM


class _Scripted:
    kind = "openai"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def run(self, *, system: str, messages: list[dict[str, Any]], tools: list[Any], max_tokens: int, model: str, capabilities: Capabilities) -> Any:
        self.calls.append({"system": system, "tools": list(tools), "model": model, "max_tokens": max_tokens})

        async def _stream() -> Any:
            yield TextDelta(text=self.text)
            yield DoneDelta(stop_reason="end_turn", assistant_blocks=[])

        return _stream()


async def test_classify_signal_sends_the_brief() -> None:
    provider = _Scripted("no")
    caps = Capabilities(supports_vision=False, supports_tools=True, thinking_form=None, source="test")
    verdict = await classify_signal(
        provider,  # type: ignore[arg-type]
        model="zenmux-model",
        capabilities=caps,
        text="TP1 smashed, congrats",
        channel_brief="## Skip (noise)\nA bare target-hit note is not a trade.",
    )
    assert verdict == "no"
    assert provider.calls[0]["tools"] == []
    assert "bare target-hit" in provider.calls[0]["system"]
    assert provider.calls[0]["model"] == "zenmux-model"


def _playbook(marker: str) -> str:
    lines = ["# Channel playbook", f"marker {marker}", "## Signals (act)", "BUY and SELL with a price."]
    lines.extend(f"- habit {index}" for index in range(40))
    return "\n".join(lines)


class _Provider:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.calls = 0

    def push(self, text: str) -> None:
        self.answers.append(text)

    def run(self, *, system: str, messages: list[dict[str, Any]], tools: list[Any], max_tokens: int, model: str, capabilities: Capabilities) -> Any:
        self.calls += 1
        text = self.answers.pop(0) if self.answers else _playbook("fallback")

        async def _stream() -> Any:
            yield TextDelta(text=text)
            yield DoneDelta(stop_reason="end_turn", assistant_blocks=[])

        return _stream()


class _GateProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    def run(self, *, system: str, messages: list[dict[str, Any]], tools: list[Any], max_tokens: int, model: str, capabilities: Capabilities) -> Any:
        async def _stream() -> Any:
            self.calls += 1
            which = self.calls
            self.started.set()
            await self.release.wait()
            if which == 1:
                text = "FIRST-BRIEF-SHOULD-DROP\n" + "\n".join(f"old {i}" for i in range(40))
            else:
                text = "SECOND-BRIEF-KEPT\n" + "\n".join(f"new {i}" for i in range(40))
            yield TextDelta(text=text)
            yield DoneDelta(stop_reason="end_turn", assistant_blocks=[])

        return _stream()


class _Message:
    def __init__(self, message_id: int, text: str, posted: datetime) -> None:
        self.id = message_id
        self.message = text
        self.raw_text = text
        self.date = posted.replace(tzinfo=UTC)
        self.media = None
        self.reply_to_msg_id = None


class _Client:
    def is_connected(self) -> bool:
        return True


class _Telegram:
    def __init__(self, messages: list[Any]) -> None:
        self.client = _Client()
        self.messages = messages
        self.calls: list[tuple[int, int, int]] = []

    async def fetch_recent_days(self, chat_id: int, *, days: int = 4, limit: int = 400) -> list[Any]:
        self.calls.append((int(chat_id), int(days), int(limit)))
        return list(self.messages)


class _Runtime:
    def __init__(self, provider: Any, telegram: Any) -> None:
        self.settings = app_settings
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.bus = EventBus()
        self.telegram = telegram
        self._provider = provider
        self._caps = Capabilities(supports_vision=True, supports_tools=True, thinking_form=None, source="test")

    async def provider(self) -> tuple[Any, str, Capabilities]:
        return self._provider, "operator-model", self._caps


async def _seed(chat_id: int, texts: list[str], *, span_days: float, active: bool = True) -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    async with session_scope() as session:
        if active:
            await session.execute(update(Source).values(is_active=False))
        source = Source(
            kind="telegram",
            chat_id=chat_id,
            title=f"Channel {chat_id}",
            username=None,
            entity_type="channel",
            is_active=active,
        )
        session.add(source)
        await session.flush()
        source_id = int(source.id)
        count = len(texts)
        for index, text in enumerate(texts):
            if count == 1 or span_days <= 0:
                posted = now
            else:
                posted = now - timedelta(days=span_days) + timedelta(days=span_days) * (index / (count - 1))
            session.add(
                Message(
                    source_id=source_id,
                    message_id=10_000 + index,
                    edit_date=0,
                    revision=1,
                    kind="text",
                    status="processed",
                    text=text,
                    posted_at=posted,
                    received_at=posted,
                )
            )
    return source_id


async def _enable_reviewer() -> None:
    """Existing studies were written when the reviewer always ran. The default is now off."""
    async with session_scope() as session:
        await set_setting(session, KEY_REVIEWER_ENABLED, True)


async def _ready(data: Path, marker: str) -> bool:
    if marker not in load_active_brief(data):
        return False
    meta = read_meta(data) or {}
    return meta.get("status") == "ready"


async def test_rich_database_skips_telegram_and_same_channel_does_not_restudy(data_dir: Path) -> None:
    await init_db()
    await _enable_reviewer()
    chat_id = -100555
    source_id = await _seed(
        chat_id,
        [f"XAUUSD BUY {4300 + index} SL 4290 TP 4310" for index in range(12)],
        span_days=3,
    )
    provider = _Provider()
    provider.push(_playbook("ALPHA-READY"))
    telegram = _Telegram([])
    reviewer = ChannelReviewer(_Runtime(provider, telegram))  # type: ignore[arg-type]
    try:
        await reviewer.start()
        for _ in range(100):
            if await _ready(data_dir, "ALPHA-READY"):
                break
            await asyncio.sleep(0.02)
        assert await _ready(data_dir, "ALPHA-READY")
        assert telegram.calls == []
        meta = read_meta(data_dir) or {}
        assert meta["origin"] == "db"
        assert meta["chat_id"] == chat_id
        calls = provider.calls
        await reviewer.on_source_selected(source_id=source_id, chat_id=chat_id)
        await asyncio.sleep(0.1)
        assert provider.calls == calls
    finally:
        await reviewer.stop()


async def test_thin_history_fetches_three_to_four_days(data_dir: Path) -> None:
    await init_db()
    await _enable_reviewer()
    chat_id = -100777
    await _seed(chat_id, ["good morning", "we are up today"], span_days=0)
    posted = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    telegram = _Telegram(
        [
            _Message(50 + index, f"XAUUSD BUY NOW {4400 + index}\nSL 4390\nTP 4410 TP 4420", posted)
            for index in range(6)
        ]
    )
    provider = _Provider()
    provider.push(_playbook("FROM-TELEGRAM"))
    reviewer = ChannelReviewer(_Runtime(provider, telegram))  # type: ignore[arg-type]
    try:
        await reviewer.start()
        for _ in range(100):
            if await _ready(data_dir, "FROM-TELEGRAM"):
                break
            await asyncio.sleep(0.02)
        assert await _ready(data_dir, "FROM-TELEGRAM")
        assert telegram.calls
        assert telegram.calls[0][0] == chat_id
        assert telegram.calls[0][1] in (3, 4)
        meta = read_meta(data_dir) or {}
        assert meta["origin"] == "db+telegram"
        study = (data_dir / "channel-briefs" / "reviewer" / "current" / "study.json").read_text(encoding="utf-8")
        assert "XAUUSD BUY NOW" in study
    finally:
        await reviewer.stop()


async def test_channel_change_backs_up_and_studies_the_new_source(data_dir: Path) -> None:
    await init_db()
    await _enable_reviewer()
    old_chat = -100101
    new_chat = -100202
    await _seed(
        old_chat,
        [f"EURUSD SELL {1.1 + index / 1000:.3f} SL 1.12" for index in range(12)],
        span_days=3,
    )
    provider = _Provider()
    provider.push(_playbook("OLD-CHANNEL-MARKER"))
    reviewer = ChannelReviewer(_Runtime(provider, _Telegram([])))  # type: ignore[arg-type]
    try:
        await reviewer.start()
        for _ in range(100):
            if await _ready(data_dir, "OLD-CHANNEL-MARKER"):
                break
            await asyncio.sleep(0.02)
        assert await _ready(data_dir, "OLD-CHANNEL-MARKER")

        new_id = await _seed(
            new_chat,
            [f"GBPUSD BUY {1.2 + index / 1000:.3f}" for index in range(12)],
            span_days=3,
        )
        provider.push(_playbook("NEW-CHANNEL-MARKER"))
        await reviewer.on_source_selected(source_id=new_id, chat_id=new_chat)
        for _ in range(100):
            if await _ready(data_dir, "NEW-CHANNEL-MARKER"):
                break
            await asyncio.sleep(0.02)
        loaded = load_active_brief(data_dir)
        assert "NEW-CHANNEL-MARKER" in loaded
        assert "OLD-CHANNEL-MARKER" not in loaded
        backups = list((data_dir / "channel-briefs" / "backups").glob("*/brief.md"))
        assert backups
        assert any("OLD-CHANNEL-MARKER" in path.read_text(encoding="utf-8") for path in backups)
        opened: list[str] = []
        original = Path.read_text

        def _spy(self: Path, *args: Any, **kwargs: Any) -> str:
            opened.append(str(self))
            return original(self, *args, **kwargs)

        Path.read_text = _spy  # type: ignore[method-assign]
        try:
            again = load_active_brief(data_dir)
        finally:
            Path.read_text = original  # type: ignore[method-assign]
        assert "OLD-CHANNEL-MARKER" not in again
        assert all("backups" not in Path(path).parts for path in opened)
    finally:
        await reviewer.stop()


async def test_stale_study_is_not_published_after_the_channel_changes(data_dir: Path) -> None:
    await init_db()
    await _enable_reviewer()
    old_chat = -100303
    new_chat = -100404
    await _seed(old_chat, [f"XAUUSD BUY {4200 + index}" for index in range(12)], span_days=3)
    provider = _GateProvider()
    reviewer = ChannelReviewer(_Runtime(provider, _Telegram([])))  # type: ignore[arg-type]
    try:
        await reviewer.start()
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        new_id = await _seed(new_chat, [f"XAGUSD SELL {30 + index}" for index in range(12)], span_days=3)
        await reviewer.on_source_selected(source_id=new_id, chat_id=new_chat)
        provider.release.set()
        for _ in range(100):
            if "SECOND-BRIEF-KEPT" in load_active_brief(data_dir):
                break
            await asyncio.sleep(0.02)
        loaded = load_active_brief(data_dir)
        assert "SECOND-BRIEF-KEPT" in loaded
        assert "FIRST-BRIEF-SHOULD-DROP" not in loaded
    finally:
        provider.release.set()
        await reviewer.stop()


async def test_reviewer_runs_while_all_signal_slots_are_held(data_dir: Path) -> None:
    await init_db()
    await _enable_reviewer()
    chat_id = -100909
    source_id = await _seed(chat_id, [f"NAS100 BUY {index}" for index in range(12)], span_days=3)
    provider = _Provider()
    provider.push(_playbook("DURING-FULL-POOL"))
    reviewer = ChannelReviewer(_Runtime(provider, _Telegram([])))  # type: ignore[arg-type]
    slots = run_slots()
    limit = _concurrent_run_limit()
    for _ in range(limit):
        await slots.acquire()
    try:
        await asyncio.wait_for(reviewer._study(source_id, chat_id), timeout=2)
    finally:
        for _ in range(limit):
            slots.release()
    assert "DURING-FULL-POOL" in load_active_brief(data_dir)
    meta = read_meta(data_dir) or {}
    assert meta["status"] == "ready"


async def test_overlong_model_output_is_clipped(data_dir: Path) -> None:
    await init_db()
    await _enable_reviewer()
    chat_id = -100808
    await _seed(chat_id, [f"US30 BUY {index}" for index in range(12)], span_days=3)
    provider = _Provider()
    provider.push("\n".join(f"line {index}" for index in range(90)))
    reviewer = ChannelReviewer(_Runtime(provider, _Telegram([])))  # type: ignore[arg-type]
    try:
        await reviewer.start()
        for _ in range(100):
            if load_active_brief(data_dir).strip():
                break
            await asyncio.sleep(0.02)
        lines = load_active_brief(data_dir).splitlines()
        assert len(lines) <= 60
        assert lines[0] == "line 0"
        assert "line 60" not in load_active_brief(data_dir)
    finally:
        await reviewer.stop()


async def test_reviewer_stays_idle_when_the_setting_is_off(data_dir: Path) -> None:
    """The default is off: kick and start must not call the model or write a brief."""
    await init_db()
    chat_id = -100111
    source_id = await _seed(chat_id, [f"XAUUSD BUY {index}" for index in range(12)], span_days=3)
    provider = _Provider()
    provider.push(_playbook("SHOULD-NOT-RUN"))
    reviewer = ChannelReviewer(_Runtime(provider, _Telegram([])))  # type: ignore[arg-type]
    try:
        await reviewer.start()
        await reviewer.on_source_selected(source_id=source_id, chat_id=chat_id)
        await asyncio.sleep(0.15)
        assert provider.calls == 0
        assert "SHOULD-NOT-RUN" not in load_active_brief(data_dir)
    finally:
        await reviewer.stop()


async def test_notify_reviewer_queues_the_selected_chat() -> None:
    queued: list[tuple[int, int]] = []

    class _Reviewer:
        async def on_source_selected(self, *, source_id: int, chat_id: int) -> None:
            queued.append((source_id, chat_id))

    class _RuntimeHolder:
        reviewer = _Reviewer()

    service = TelegramService(lambda: _RuntimeHolder(), app_settings)
    await service._notify_reviewer(15, -100123456)
    assert queued == [(15, -100123456)]


def test_copytrader_loader_has_no_backup_api() -> None:
    import app.reviewer.brief as brief

    assert not hasattr(brief, "backup_current")
    assert not hasattr(brief, "read_meta")
    assert not hasattr(brief, "read_backup")
