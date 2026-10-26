"""The channel reviewer: one background task, separate from the three signal workers.

It studies the active Telegram source once. A later message does not start another
study. Changing the source backs up the previous playbook and starts a fresh one.
The task never acquires the agent-run semaphore, so a long study cannot hold up a
free copytrader slot.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app import events
from app.db import session_scope
from app.models import Source
from app.operator_settings import KEY_REVIEWER_ENABLED, reviewer_enabled
from app.reviewer.archive import (
    attempts_for,
    backup_current,
    brief_ready_for,
    write_meta,
    write_ready,
)
from app.reviewer.study import (
    DEFAULT_MIN_MESSAGES,
    DEFAULT_REVIEWER_MAX_TOKENS,
    compose_brief,
    corpus_is_thin,
    history_days,
    load_db_messages,
    load_source,
    max_brief_lines,
    merge_corpus,
    render_corpus,
    study_artifact,
    telegram_message_view,
)
from app.util.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.runtime import Runtime

log = get_logger(__name__)

TASK_NAME: str = "channel_reviewer"
SETTINGS_TASK: str = "channel_reviewer_settings"

#: A study that keeps failing stops until the channel changes. Waiting for a
#: provider or for Telegram is not a failure and does not spend an attempt.
MAX_STUDY_ATTEMPTS: int = 3


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ChannelReviewer:
    """Owns the one-shot study loop for whichever source is active."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._lock: asyncio.Lock = asyncio.Lock()
        self._wake: asyncio.Event = asyncio.Event()
        self._job: tuple[int, int] | None = None
        self._stopping: bool = False
        self._task: asyncio.Task[None] | None = None
        self._settings_task: asyncio.Task[None] | None = None

    def _data_dir(self) -> str:
        return str(getattr(self.runtime.settings, "DATA_DIR", "") or "")

    def _min_messages(self) -> int:
        try:
            return max(1, int(getattr(self.runtime.settings, "CHANNEL_BRIEF_MIN_MESSAGES", DEFAULT_MIN_MESSAGES)))
        except (TypeError, ValueError):
            return DEFAULT_MIN_MESSAGES

    def _max_lines(self) -> int:
        return max_brief_lines(getattr(self.runtime.settings, "CHANNEL_BRIEF_MAX_LINES", 60))

    def _max_tokens(self) -> int:
        try:
            return max(1, int(getattr(self.runtime.settings, "CHANNEL_REVIEWER_MAX_TOKENS", DEFAULT_REVIEWER_MAX_TOKENS)))
        except (TypeError, ValueError):
            return DEFAULT_REVIEWER_MAX_TOKENS

    async def start(self) -> None:
        """Start the loop if it is not already running, then look at the active source."""
        self._stopping = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=TASK_NAME)
            self.runtime.tasks[TASK_NAME] = self._task
        if self._settings_task is None or self._settings_task.done():
            self._settings_task = asyncio.create_task(self._watch_settings(), name=SETTINGS_TASK)
            self.runtime.tasks[SETTINGS_TASK] = self._settings_task
        await self.kick()

    async def stop(self) -> None:
        """Cancel the loop and the settings watcher."""
        self._stopping = True
        self._wake.set()
        tasks = [task for task in (self._task, self._settings_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - shutdown must keep going
                log.warning("channel_reviewer_stop_failed error=%s", exc)
        self._task = None
        self._settings_task = None
        for name in (TASK_NAME, SETTINGS_TASK):
            current = self.runtime.tasks.get(name)
            if current is not None and current.done():
                self.runtime.tasks.pop(name, None)

    async def _enabled(self) -> bool:
        """Playbook generation stays off until the operator turns the setting on."""
        try:
            async with session_scope() as session:
                return await reviewer_enabled(session)
        except Exception as exc:  # noqa: BLE001 - a settings miss must not start a study
            log.warning("channel_reviewer_enabled_read_failed error=%s", exc)
            return False

    async def kick(self) -> None:
        """Study the active source when it does not already have a ready brief."""
        if not await self._enabled():
            log.info("channel_reviewer_disabled")
            return
        binding = await self._active_binding()
        if binding is None:
            return
        source_id, chat_id = binding
        await self.on_source_selected(source_id=source_id, chat_id=chat_id)

    async def on_source_selected(self, *, source_id: int, chat_id: int) -> None:
        """Back up the previous channel when the binding changed, then study once.

        Selecting the same chat again does nothing once its brief is ready. A failed
        study is not retried forever: after ``MAX_STUDY_ATTEMPTS`` only a new chat
        starts again. There is no timer.
        """
        if not await self._enabled():
            async with self._lock:
                self._job = None
            log.info("channel_reviewer_disabled source_id=%s chat_id=%s", source_id, chat_id)
            return
        data_dir = self._data_dir()
        source_id = int(source_id)
        chat_id = int(chat_id)
        async with self._lock:
            meta_chat = _meta_chat(data_dir)
            if meta_chat is not None and meta_chat != chat_id:
                destination = backup_current(data_dir)
                log.info(
                    "channel_brief_backed_up previous=%s next=%s path=%s",
                    meta_chat,
                    chat_id,
                    destination,
                )
            if brief_ready_for(data_dir, chat_id):
                if self._job is not None and self._job[1] == chat_id:
                    self._job = None
                log.info("channel_brief_ready chat_id=%s restudy=no", chat_id)
                return
            if attempts_for(data_dir, chat_id) >= MAX_STUDY_ATTEMPTS:
                log.info("channel_brief_gave_up chat_id=%s attempts=%d", chat_id, MAX_STUDY_ATTEMPTS)
                return
            self._job = (source_id, chat_id)
            self._wake.set()
            log.info("channel_study_queued source_id=%s chat_id=%s", source_id, chat_id)

    async def _loop(self) -> None:
        try:
            while not self._stopping:
                await self._wake.wait()
                if self._stopping:
                    return
                async with self._lock:
                    self._wake.clear()
                    job = self._job
                    self._job = None
                if job is None:
                    continue
                try:
                    await self._study(job[0], job[1])
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad study must not kill the task
                    log.exception("channel_reviewer_failed source_id=%s chat_id=%s", job[0], job[1])
        except asyncio.CancelledError:
            raise

    async def _watch_settings(self) -> None:
        """When the provider is saved, finish a study that was waiting on it.

        A source change is handled by ``on_source_selected`` directly. Reacting to
        that event here as well would queue a second study for the same switch.
        """
        queue = self.runtime.bus.subscribe()
        try:
            while not self._stopping:
                envelope = await queue.get()
                if not isinstance(envelope, dict):
                    continue
                if envelope.get("type") != events.SETTINGS_CHANGED:
                    continue
                data = envelope.get("data")
                if not isinstance(data, dict):
                    continue
                if not _provider_settings_changed(data) and not _reviewer_toggle_changed(data):
                    continue
                try:
                    if not await self._enabled():
                        async with self._lock:
                            self._job = None
                        log.info("channel_reviewer_disabled")
                        continue
                    await self.kick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep watching
                    log.warning("channel_reviewer_kick_failed error=%s", exc)
        except asyncio.CancelledError:
            raise
        finally:
            self.runtime.bus.unsubscribe(queue)

    async def _study(self, source_id: int, chat_id: int) -> None:
        if not await self._enabled():
            log.info("channel_study_skipped chat_id=%s reason=disabled", chat_id)
            return
        data_dir = self._data_dir()
        if brief_ready_for(data_dir, chat_id):
            return
        if not await self._still_current(chat_id):
            log.info("channel_study_discarded chat_id=%s reason=source_changed", chat_id)
            return

        rows = await load_db_messages(source_id)
        origin = "db"
        if corpus_is_thin(rows, min_messages=self._min_messages()):
            fetched = await self._telegram_recent(chat_id)
            if fetched is None:
                await self._write_status(
                    source_id,
                    chat_id,
                    status="waiting_history",
                    error="telegram history is not available yet",
                )
                return
            extra = [view for view in (telegram_message_view(item) for item in fetched) if view]
            if extra:
                rows = merge_corpus(rows, extra)
                origin = "db+telegram"
            elif not rows:
                await self._write_status(
                    source_id,
                    chat_id,
                    status="waiting_history",
                    error="no posts in the database or the recent telegram window",
                )
                return

        if not rows:
            await self._write_status(source_id, chat_id, status="waiting_history", error="no posts")
            return

        built = await self._provider()
        if built is None:
            await self._write_status(
                source_id,
                chat_id,
                status="waiting_provider",
                error="no provider configured",
            )
            return
        provider, model, capabilities = built
        described = await load_source(source_id)
        title = described[0] if described is not None else ""
        corpus = render_corpus(title, chat_id, rows)
        try:
            brief = await compose_brief(
                provider,
                model=model,
                capabilities=capabilities,
                corpus=corpus,
                max_tokens=self._max_tokens(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed study is recorded, not fatal
            log.warning("channel_study_failed chat_id=%s error=%s", chat_id, exc)
            await self._write_status(
                source_id,
                chat_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                bump_attempt=True,
            )
            return
        if not brief.strip():
            await self._write_status(
                source_id,
                chat_id,
                status="failed",
                error="the model returned an empty playbook",
                bump_attempt=True,
            )
            return

        async with self._lock:
            if not await self._enabled():
                log.info("channel_study_discarded chat_id=%s reason=disabled", chat_id)
                return
            if not await self._still_current(chat_id):
                log.info("channel_study_discarded chat_id=%s reason=source_changed", chat_id)
                return
            stored = write_ready(
                data_dir,
                brief=brief,
                meta={
                    "chat_id": int(chat_id),
                    "source_id": int(source_id),
                    "status": "ready",
                    "attempts": attempts_for(data_dir, chat_id),
                    "studied_at": _utcnow().isoformat(),
                    "origin": origin,
                    "message_count": len(rows),
                    "model": model,
                },
                study=study_artifact(rows, origin=origin),
                max_lines=self._max_lines(),
            )
        log.info(
            "channel_brief_ready chat_id=%s origin=%s messages=%d lines=%d",
            chat_id,
            origin,
            len(rows),
            stored.count("\n"),
        )

    async def _write_status(
        self,
        source_id: int,
        chat_id: int,
        *,
        status: str,
        error: str,
        bump_attempt: bool = False,
    ) -> None:
        data_dir = self._data_dir()
        async with self._lock:
            if not await self._still_current(chat_id):
                return
            attempts = attempts_for(data_dir, chat_id)
            if bump_attempt:
                attempts += 1
            write_meta(
                data_dir,
                {
                    "chat_id": int(chat_id),
                    "source_id": int(source_id),
                    "status": status,
                    "attempts": attempts,
                    "studied_at": _utcnow().isoformat(),
                    "error": error[:500],
                },
            )
        log.info("channel_study_status chat_id=%s status=%s attempts=%d", chat_id, status, attempts)

    async def _still_current(self, chat_id: int) -> bool:
        binding = await self._active_binding()
        return binding is not None and binding[1] == int(chat_id)

    async def _active_binding(self) -> tuple[int, int] | None:
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(Source).where(Source.is_active.is_(True)).order_by(Source.id).limit(1)
                )
                row = result.scalars().first()
                if row is None:
                    return None
                return int(row.id), int(row.chat_id)
        except Exception as exc:  # noqa: BLE001 - a bad store must not kill the reviewer
            log.warning("channel_reviewer_source_read_failed error=%s", exc)
            return None

    async def _provider(self) -> tuple[Any, str, Any] | None:
        getter = getattr(self.runtime, "provider", None)
        if not callable(getter):
            return None
        try:
            built = await getter()
        except Exception as exc:  # noqa: BLE001 - missing provider is a status, not a crash
            log.warning("channel_reviewer_provider_failed error=%s", exc)
            return None
        if not built:
            return None
        provider, model, capabilities = built
        return provider, str(model or ""), capabilities

    async def _telegram_recent(self, chat_id: int) -> list[Any] | None:
        """Fetch the last 3–4 days, or None when Telegram is not connected yet.

        An empty list means the fetch was attempted and returned nothing (or failed).
        None means "try again when the client is up" — not a spent study attempt.
        """
        telegram = getattr(self.runtime, "telegram", None)
        fetch = getattr(telegram, "fetch_recent_days", None)
        if telegram is None or not callable(fetch):
            return None
        client = getattr(telegram, "client", None)
        connected = getattr(client, "is_connected", None)
        try:
            up = bool(connected()) if callable(connected) else False
        except Exception:  # noqa: BLE001 - treat a confused client as not ready
            up = False
        if client is None or not up:
            return None
        days = history_days(getattr(self.runtime.settings, "CHANNEL_BRIEF_HISTORY_DAYS", 4))
        try:
            fetched = await fetch(int(chat_id), days=days, limit=300)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to whatever the database holds
            log.warning("channel_history_fetch_failed chat_id=%s error=%s", chat_id, exc)
            return []
        if not fetched:
            return []
        return list(fetched)


def _meta_chat(data_dir: str) -> int | None:
    from app.reviewer.archive import read_meta

    meta = read_meta(data_dir) or {}
    try:
        return int(meta.get("chat_id"))
    except (TypeError, ValueError):
        return None


def _reviewer_toggle_changed(data: dict[str, Any]) -> bool:
    """True when this settings event is the channel-reviewer switch."""
    key = str(data.get("key") or "")
    if key == KEY_REVIEWER_ENABLED:
        return True
    keys = data.get("keys")
    if isinstance(keys, (list, tuple)) and any(str(item) == KEY_REVIEWER_ENABLED for item in keys):
        return True
    return False


def _provider_settings_changed(data: dict[str, Any]) -> bool:
    key = str(data.get("key") or "")
    if key.startswith("provider."):
        return True
    if str(data.get("section") or "") == "provider":
        return True
    keys = data.get("keys")
    if isinstance(keys, (list, tuple)) and any(str(item).startswith("provider.") for item in keys):
        return True
    return False
