"""Telegram ingest: classify, dedup, persist, and hand posts to a small pool of agent workers.

Each source has one bounded queue and up to three workers (``INGEST_WORKERS``, default 3).
A free worker takes the next queued post immediately; it does not wait for another worker
to finish deciding or trading. A celebration must not sit in front of an entry for the
whole of a tool loop. Unrelated posts may run together. A teaser and the replies that hang
off it share one chain lock inside the agent loop, so a ladder reply cannot open a second
pair while the teaser run is still placing the first. Broker writes stay serial inside the
executor: two workers may classify and think together, and only one may open, modify, close
or cancel at a time. The channel reviewer is a fourth task with its own loop and does not
take one of these three slots. An edit whose text is byte-identical to the previous
revision, and which adds no new image, is stored and not enqueued.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.config import settings
from app.db import session_scope
from app.models import Message, MessageImage, Source
from app.operator_settings import SKIP_REASON_TAG, tag_filter_drops
from app.telegram.classify import classify, kind_is_processable, skip_reason
from app.trading import guards
from app.trading.exposure import (
    FULL_CLOSE_REASON,
    chain_exposure_closed,
    close_signal_exposure,
    is_full_close_text,
)
from app.telegram.links import permalink
from app.util.images import image_dimensions
from app.util.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only; importing Runtime here would risk a cycle
    from app.runtime import Runtime

log = get_logger(__name__)

#: Name of the first ingest worker in `runtime.tasks`. Further workers append ``-2``, ``-3``.
TASK_NAME: str = "ingest_worker"

#: Stored on an edit that repeated the previous revision exactly, so the chain can ignore it
#: when deciding whether a later edit of a skipped voice note should restart at revision 1.
IDENTICAL_EDIT_REASON: str = "identical_edit"

#: Kinds whose media is an image worth downloading. Every other kind stores no files.
IMAGE_KINDS: frozenset[str] = frozenset({"photo", "photo_caption"})

#: How long `stop()` waits for the worker to finish the item in flight before cancelling it.
STOP_TIMEOUT_SECONDS: float = 30.0

#: Error text stored on a message row is truncated to this many characters.
MAX_ERROR_CHARS: int = 2000

#: Telegram's channel marker: a supergroup/channel id is `-100` prefixed to its short id.
_CHANNEL_MARKER: int = 1_000_000_000_000

#: Fallback mime when neither the file extension nor the document says what an image is.
_DEFAULT_IMAGE_MIME: str = "image/jpeg"


def _sha256_file(path: str) -> str | None:
    """Hash a stored image, or None when the file cannot be read."""
    if not path:
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _utcnow() -> datetime:
    """Return the current UTC instant as a timezone-naive datetime (the storage convention)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _naive_utc(value: Any) -> datetime | None:
    """Convert a Telethon aware datetime to the naive-UTC form every column stores."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _short_chat_id(value: Any) -> int:
    """Normalise a chat id to its unmarked short form so `-100…` and `…` compare equal."""
    try:
        number = abs(int(value))
    except (TypeError, ValueError):
        return 0
    if number > _CHANNEL_MARKER:
        number -= _CHANNEL_MARKER
    return number


def _same_chat(source_chat_id: Any, message_chat_id: Any) -> bool:
    """Report whether a message belongs to the given source, tolerating the `-100` marker."""
    if message_chat_id is None:
        # Telethon could not resolve a peer for this update; the handler is registered for the
        # active source only, so trust the registration rather than dropping a real signal.
        return True
    left = _short_chat_id(source_chat_id)
    right = _short_chat_id(message_chat_id)
    return left != 0 and left == right


def _message_text(message: Any) -> str:
    """Return a message's text; the caption *is* the text, there is no separate field (§3.2)."""
    for attribute in ("raw_text", "message", "text"):
        value = getattr(message, attribute, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _album_text(items: Sequence[Any]) -> str:
    """Return the album's caption: the anchor's text, or the first non-empty one in the album."""
    for item in items:
        text = _message_text(item)
        if text:
            return text
    return ""


def _group_albums(messages: Sequence[Any]) -> list[list[Any]]:
    """Group catch-up messages that share a `grouped_id` into one album each, in id order."""
    groups: list[list[Any]] = []
    index: dict[int, list[Any]] = {}
    for message in messages:
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is None:
            groups.append([message])
            continue
        key = int(grouped_id)
        bucket = index.get(key)
        if bucket is None:
            bucket = []
            index[key] = bucket
            groups.append(bucket)
        bucket.append(message)
    return groups


class Ingestor:
    """Owns the ingest queue, the worker pool, and every write to `messages`."""

    def __init__(self, runtime: Runtime) -> None:
        """Build the bounded queue; nothing is consumed until `start()` runs."""
        self.runtime: Runtime = runtime
        self.settings = runtime.settings if getattr(runtime, "settings", None) else settings
        # Bounded on purpose (§3.5): a backlog that grows without limit turns a burst of posts
        # into an unbounded pile of stale agent runs. On overflow the OLDEST item is dropped.
        self._queue: asyncio.Queue[int | None] = asyncio.Queue(
            maxsize=max(1, int(self.settings.INGEST_QUEUE_MAX))
        )
        # One ingest at a time: see handle_message for what raced without it.
        self._ingest_lock: asyncio.Lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping: bool = False

    def _worker_count(self) -> int:
        """How many workers to start: the configured count, never outside 1–3."""
        try:
            raw = int(getattr(self.settings, "INGEST_WORKERS", 3))
        except (TypeError, ValueError):
            raw = 3
        return max(1, min(3, raw))

    def _drain_sentinels(self) -> None:
        """Drop stop markers left in the queue so a later start() does not exit immediately."""
        kept: list[int] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            if item is not None:
                kept.append(int(item))
        for item in kept:
            self._try_put(item)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the worker pool; calling it twice while workers are alive is a no-op."""
        self._stopping = False
        live = [task for task in self._tasks if not task.done()]
        if live:
            return
        self._drain_sentinels()
        self._tasks = []
        count = self._worker_count()
        for index in range(count):
            name = TASK_NAME if index == 0 else f"{TASK_NAME}-{index + 1}"
            task = asyncio.create_task(self.worker(), name=name)
            self._tasks.append(task)
            self.runtime.tasks[name] = task
        log.info("ingest_started workers=%d queue_max=%d", count, self._queue.maxsize)

    async def stop(self) -> None:
        """Stop accepting work and wind every worker down, cancelling any that hang."""
        self._stopping = True
        tasks = list(self._tasks)
        self._tasks = []
        for task in tasks:
            name = task.get_name()
            if self.runtime.tasks.get(name) is task:
                self.runtime.tasks.pop(name, None)
        pending = [task for task in tasks if not task.done()]
        for _task in pending:
            self._offer_sentinel()
        if not pending:
            log.info("ingest_stopped pending=%d", self._queue.qsize())
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(*pending, return_exceptions=True)),
                timeout=STOP_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("ingest_stop_timeout seconds=%.0f", STOP_TIMEOUT_SECONDS)
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            for task in pending:
                if not task.done():
                    task.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - a failed worker must not block shutdown
            log.warning("ingest_stop_worker_error error=%s", exc)
        log.info("ingest_stopped pending=%d", self._queue.qsize())

    def _try_put(self, item: int | None) -> bool:
        """Put one item on the queue without waiting; False means the queue is full."""
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            return False

    def _offer_sentinel(self) -> None:
        """Put the stop sentinel on the queue, making room for it when the queue is full."""
        if self._try_put(None):
            return
        try:
            self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        self._queue.task_done()
        self._try_put(None)

    # ------------------------------------------------------------------ queue

    async def enqueue(self, message_row_id: int) -> None:
        """Queue one processable row for the worker, dropping the OLDEST item on overflow."""
        if self._stopping:
            log.info("ingest_enqueue_refused row_id=%d reason=stopping", message_row_id)
            return
        row_id = int(message_row_id)
        if self._try_put(row_id):
            return
        dropped = await self._drop_oldest()
        if not self._try_put(row_id):
            # The worker refilled the slot between the drop and the put; report the newcomer
            # as the casualty rather than silently losing it.
            await self._report_overflow(row_id)
            return
        if dropped is not None:
            await self._report_overflow(dropped)

    async def _drop_oldest(self) -> int | None:
        """Remove the oldest queued row id to make room; returns it, or None if there was none."""
        try:
            oldest = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        self._queue.task_done()
        if oldest is None:
            # Never discard the stop sentinel: the worker would then never wind down.
            self._try_put(None)
            return None
        return int(oldest)

    async def _report_overflow(self, message_row_id: int) -> None:
        """Mark a dropped row and warn the dashboard, so an overflow is visible, not silent."""
        message = "dropped: the ingest queue overflowed before this signal could run"
        await self._mark_status(message_row_id, "error", error=message)
        self.runtime.bus.emit(
            events.SYSTEM_WARNING,
            reason="queue_overflow",
            message_row_id=message_row_id,
            queue_max=self._queue.maxsize,
            message=message,
        )
        log.warning("ingest_queue_overflow dropped_row_id=%d queue_max=%d", message_row_id, self._queue.maxsize)

    # ------------------------------------------------------------------ worker

    async def worker(self) -> None:
        """Consume the queue; one bad run never kills the worker. Several workers share it."""
        log.info("ingest_worker_started queue_max=%d", self._queue.maxsize)
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is None:
                        log.info("ingest_worker_sentinel")
                        return
                    await self._process(int(item))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the worker outlives every failed run
                    log.exception("ingest_worker_item_failed row_id=%s error=%s", item, exc)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            log.info("ingest_worker_cancelled")
            raise
        finally:
            log.info("ingest_worker_stopped")

    async def _process(self, message_row_id: int) -> None:
        """Run the agent for exactly one message row and record how it ended."""
        started = time.monotonic()
        log.info("ingest_run_start row_id=%d", message_row_id)
        if await self._skip_voided(message_row_id):
            log.info("ingest_run_skipped row_id=%d reason=exposure_closed", message_row_id)
            return
        if not await self._mark_status(message_row_id, "processing"):
            log.warning("ingest_run_skipped row_id=%d reason=row_missing", message_row_id)
            return
        try:
            # Imported lazily: app.ai.loop pulls in the whole provider stack, and importing it
            # at module level would also make ingest unusable while that module is being written.
            from app.ai.loop import run_agent

            result = await run_agent(self.runtime, message_row_id=message_row_id, kind="signal")
        except asyncio.CancelledError:
            await self._mark_status(message_row_id, "error", error="the agent run was cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - a failed run is data, not a crash
            detail = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            await self._mark_status(message_row_id, "error", error=detail)
            log.exception("ingest_run_failed row_id=%d error=%s", message_row_id, exc)
            self.runtime.bus.emit(
                events.SYSTEM_WARNING,
                reason="ingest_run_failed",
                message_row_id=message_row_id,
                message=detail,
            )
            return
        status = "processed"
        error: str | None = None
        decision: Any = None
        if isinstance(result, dict):
            decision = result.get("decision")
            if result.get("status") == "error":
                status = "error"
                error = str(result.get("error") or "the agent run ended in an error")[:MAX_ERROR_CHARS]
        await self._mark_status(message_row_id, status, error=error)
        log.info(
            "ingest_run_end row_id=%d status=%s decision=%s ms=%d",
            message_row_id,
            status,
            decision,
            int((time.monotonic() - started) * 1000),
        )

    async def handle_deleted(self, message_ids: Iterable[int]) -> None:
        """Mark channel posts deleted and flatten every chain they belong to.

        Telegram's ``UpdateDeleteChannelMessages`` carries ids, not text. Either the
        teaser or the ladder reply is enough: the close walks the reply chain and
        covers positions and pending orders. No model run is started.
        """
        ids = sorted({int(item) for item in message_ids if int(item) > 0})
        if not ids:
            return
        async with self._ingest_lock:
            await self._handle_deleted(ids)

    async def _handle_deleted(self, message_ids: list[int]) -> None:
        """Body of :meth:`handle_deleted`, always called under the ingest lock."""
        refs: list[str] = []
        source_id = 0
        try:
            async with session_scope() as session:
                source = await self._active_source(session)
                if source is None:
                    log.info("delete_no_active_source ids=%s", message_ids)
                    return
                source_id = int(source.id)
                result = await session.execute(
                    select(Message).where(
                        Message.source_id == source_id,
                        Message.message_id.in_(message_ids),
                    )
                )
                rows = list(result.scalars().all())
                if not rows:
                    log.info(
                        "delete_unknown source_id=%s ids=%s", source_id, message_ids
                    )
                    return
                now = _utcnow()
                seen_roots: set[int] = set()
                for row in rows:
                    if row.deleted_at is None:
                        row.deleted_at = now
                    root = await guards.root_message_id(session, source_id, int(row.message_id))
                    if root in seen_roots:
                        continue
                    seen_roots.add(root)
                    refs.append(f"SIG:{source_id}:{root}")
        except Exception as exc:  # noqa: BLE001 - a delete must not raise into Telethon
            log.exception("delete_mark_failed ids=%s error=%s", message_ids, exc)
            self.runtime.bus.emit(
                events.SYSTEM_WARNING,
                reason="delete_mark_failed",
                message_ids=message_ids,
                message=str(exc)[:MAX_ERROR_CHARS],
            )
            return
        for ref in refs:
            await self._flatten(
                ref,
                cause="channel_delete",
                reason="channel deleted the signal post",
                source_id=source_id or None,
            )
        log.info("delete_closed source_id=%s refs=%s", source_id, refs)

    async def _flatten(
        self,
        signal_ref: str,
        *,
        cause: str,
        reason: str,
        source_id: int | None = None,
        message_row_id: int | None = None,
    ) -> None:
        """Close the chain now. A missing executor is logged, not swallowed silently."""
        executor = getattr(self.runtime, "executor", None)
        if executor is None:
            log.warning("exposure_close_no_executor signal_ref=%s cause=%s", signal_ref, cause)
            self.runtime.bus.emit(
                events.SYSTEM_WARNING,
                reason="exposure_close_no_executor",
                signal_ref=signal_ref,
                cause=cause,
            )
            return
        try:
            result = await close_signal_exposure(
                executor,
                signal_ref,
                reason=reason,
                cause=cause,
                bypass_arm=True,
                source_id=source_id,
                message_row_id=message_row_id,
            )
        except Exception as exc:  # noqa: BLE001 - the mark already landed; report the close
            log.exception("exposure_close_failed signal_ref=%s error=%s", signal_ref, exc)
            self.runtime.bus.emit(
                events.SYSTEM_WARNING,
                reason="exposure_close_failed",
                signal_ref=signal_ref,
                message=str(exc)[:MAX_ERROR_CHARS],
            )
            return
        if not result.get("ok"):
            log.warning(
                "exposure_close_incomplete signal_ref=%s message=%s",
                signal_ref,
                result.get("message"),
            )
            self.runtime.bus.emit(
                events.SYSTEM_WARNING,
                reason="exposure_close_incomplete",
                signal_ref=signal_ref,
                message=str(result.get("message") or ""),
            )

    async def _skip_voided(self, message_row_id: int) -> bool:
        """Do not start a model run for a deleted post or a chain already flattened."""
        try:
            async with session_scope() as session:
                row = await session.get(Message, int(message_row_id))
                if row is None:
                    return False
                ref = row.signal_ref
                deleted = row.deleted_at is not None
                status = str(row.status or "")
                reason = row.skip_reason
                if deleted and status in ("pending", "processing") and reason != FULL_CLOSE_REASON:
                    row.status = "skipped"
                    row.skip_reason = reason or "deleted"
                    return True
                if reason == FULL_CLOSE_REASON:
                    return True
            if await chain_exposure_closed(ref):
                async with session_scope() as session:
                    row = await session.get(Message, int(message_row_id))
                    if row is not None and str(row.status or "") in ("pending", "processing"):
                        if row.skip_reason != FULL_CLOSE_REASON:
                            row.status = "skipped"
                            row.skip_reason = row.skip_reason or "exposure_closed"
                return True
        except Exception as exc:  # noqa: BLE001 - a failed check must not open a voided chain
            log.warning("ingest_void_check_failed row_id=%s error=%s", message_row_id, exc)
            return True
        return False

    async def _mark_status(self, message_row_id: int, status: str, *, error: str | None = None) -> bool:
        """Set a message row's status (and error); returns False when the row is gone."""
        try:
            async with session_scope() as session:
                row = await session.get(Message, int(message_row_id))
                if row is None:
                    return False
                row.status = status
                row.error = error
            return True
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not kill the worker
            log.warning("ingest_status_write_failed row_id=%s status=%s error=%s", message_row_id, status, exc)
            return False

    # ------------------------------------------------------------------ ingest

    async def handle_message(
        self,
        message: Any,
        *,
        catchup: bool = False,
        album: list | None = None,
        edited: bool = False,
    ) -> int | None:
        """Classify, dedup, persist and enqueue one message; returns its row id or None.

        Serialised on `_ingest_lock`. Telethon calls this straight from its handlers, so a post
        and an edit arriving seconds apart ran concurrently: both read the revision chain before
        either had committed, and the edit was stored as revision 1 instead of 2. Observed live
        on message 19189 (rev 1, 1, 2 — the middle one wrong). The unique index still stopped a
        true duplicate and `signal_ref` is revision-independent, so no trade was ever at risk;
        what broke was the audit trail and the "previous revision" the model is shown for an
        edit. Ingest is a handful of rows per minute — a lock here costs nothing.
        """
        async with self._ingest_lock:
            return await self._handle_message_guarded(
                message, catchup=catchup, album=album, edited=edited
            )

    async def _handle_message_guarded(
        self,
        message: Any,
        *,
        catchup: bool = False,
        album: list | None = None,
        edited: bool = False,
    ) -> int | None:
        """The body of :meth:`handle_message`, always called under the lock."""
        try:
            return await self._handle_message(message, catchup=catchup, album=album, edited=edited)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - an update handler must never raise into Telethon
            detail = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            log.exception("ingest_failed message_id=%s error=%s", getattr(message, "id", None), exc)
            self.runtime.bus.emit(
                events.SYSTEM_WARNING,
                reason="ingest_failed",
                message_id=getattr(message, "id", None),
                message=detail,
            )
            return None

    async def _handle_message(
        self,
        message: Any,
        *,
        catchup: bool,
        album: list | None,
        edited: bool,
    ) -> int | None:
        """The real body of `handle_message`, kept separate so nothing escapes to the caller."""
        message_id = int(getattr(message, "id", 0) or 0)
        if message_id <= 0:
            log.warning("ingest_no_message_id kind=%s", type(message).__name__)
            return None

        if self._deferred_to_album(message, album=album, catchup=catchup, edited=edited):
            log.debug("ingest_deferred_to_album message_id=%d grouped_id=%s", message_id,
                      getattr(message, "grouped_id", None))
            return None

        items: list[Any] = [m for m in (album or [message]) if m is not None] or [message]
        kind = classify(message)
        text = _album_text(items) if album else _message_text(message)
        if album and kind == "photo" and text:
            # A Telegram album carries its caption on one member only, which is not always the
            # anchor; a captioned album is a captioned photo whichever item holds the words.
            kind = "photo_caption"

        # edit_date is 0 for an un-edited message and NEVER NULL (§3.5): SQLite treats every NULL
        # as distinct, so a NULL here would let a Telethon reconnect replay insert a second row
        # for the same post — and the second row would open the same trade a second time. The
        # unique index (source_id, message_id, edit_date) only guards the replay if 0 is stored.
        raw_edit_date = getattr(message, "edit_date", None)
        edit_date = int(raw_edit_date.timestamp()) if edited and raw_edit_date is not None else 0

        received_at = _utcnow()
        posted_at = _naive_utc(getattr(message, "date", None)) or received_at
        # §3.3: the parent lookup and the context attachment happen in app.ai.prompts.build_context;
        # ingest's job is only to capture which message this one answers.
        reply_to = getattr(message, "reply_to_msg_id", None)
        reply_to_message_id = int(reply_to) if reply_to else None
        raw_grouped = getattr(message, "grouped_id", None)
        grouped_id = int(raw_grouped) if raw_grouped else None

        async with session_scope() as session:
            source = await self._active_source(session)
            if source is None:
                log.debug("ingest_no_active_source message_id=%d", message_id)
                return None
            if not _same_chat(source.chat_id, getattr(message, "chat_id", None)):
                log.debug(
                    "ingest_foreign_chat message_id=%d chat_id=%s source_chat_id=%s",
                    message_id,
                    getattr(message, "chat_id", None),
                    source.chat_id,
                )
                return None
            # Read every source field into a local NOW. A rollback below expires the ORM
            # instance, and touching an expired attribute afterwards emits a lazy SELECT
            # from outside SQLAlchemy's greenlet — which fails as MissingGreenlet and turns
            # a routine duplicate into an error.
            source_id = int(source.id)
            source_chat_id = int(source.chat_id)
            source_username = source.username
            source_entity_type = str(source.entity_type)
            source_last_message_id = int(source.last_message_id or 0)

            revision = await self._next_revision(session, source_id, message_id, edited=edited)
            processable = kind_is_processable(kind)
            # The tag filter runs before full-close and before the agent queue. A post
            # without the configured tag is still stored so the feed shows it.
            dropped_by_tag = processable and await tag_filter_drops(session, text)
            full_close = (
                not dropped_by_tag
                and not catchup
                and bool(reply_to_message_id)
                and processable
                and is_full_close_text(text)
            )
            if dropped_by_tag:
                status = "skipped"
                reason = SKIP_REASON_TAG
            elif full_close:
                status = "processed"
                reason = FULL_CLOSE_REASON
            elif processable:
                status = "pending"
                reason = None
            else:
                status = "skipped"
                reason = skip_reason(kind)
            row = Message(
                source_id=source_id,
                message_id=message_id,
                edit_date=edit_date,
                revision=revision,
                kind=kind,
                # A skipped row is STILL RECORDED and shown in the feed, so the operator can see
                # that nothing was lost — it just never reaches the model (acceptance criterion 8).
                status=status,
                skip_reason=reason,
                catchup=bool(catchup),
                text=text,
                reply_to_message_id=reply_to_message_id,
                permalink=permalink(source_entity_type, source_username, source_chat_id, message_id),
                posted_at=posted_at,
                received_at=received_at,
                grouped_id=grouped_id,
                image_count=0,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                # Dedup is the insert itself, never "select then insert": the race between a live
                # update and a reconnect replay is real, and only the unique index closes it.
                await session.rollback()
                log.info(
                    "ingest_duplicate source_id=%d message_id=%d edit_date=%d",
                    source_id,
                    message_id,
                    edit_date,
                )
                return None

            row_id = int(row.id)
            snapshot = self._snapshot(row)
            if message_id > source_last_message_id:
                source.last_message_id = message_id

        if full_close:
            parent_ref = f"SIG:{source_id}:{reply_to_message_id}"
            snapshot["images"] = []
            self._emit_signal(snapshot)
            await self._flatten(
                parent_ref,
                cause="full_close",
                reason="channel reply: full close",
                source_id=source_id,
                message_row_id=row_id,
            )
            log.info(
                "ingest_full_close row_id=%d message_id=%d parent=%s",
                row_id,
                message_id,
                parent_ref,
            )
            return row_id

        images: list[dict[str, Any]] = []
        downloaded: list[dict[str, Any]] = []
        if kind in IMAGE_KINDS:
            # Store EVERY image of an album (§3.4). The MAX_IMAGES_PER_RUN cap is applied at
            # context-build time; capping here would throw away data that cannot be fetched again.
            downloaded = await self._download_images(items)
            if downloaded:
                images = await self._persist_images(row_id, downloaded)
                snapshot["image_count"] = len(images)

        if (
            edited
            and snapshot["status"] == "pending"
            and await self._is_identical_edit(
                source_id=source_id,
                message_id=message_id,
                row_id=row_id,
                text=text,
                kind=kind,
                downloaded=downloaded,
            )
        ):
            await self._mark_identical_edit(row_id)
            snapshot["status"] = "skipped"
            snapshot["skip_reason"] = IDENTICAL_EDIT_REASON

        snapshot["images"] = images
        self._emit_signal(snapshot)

        if snapshot["status"] == "pending":
            # Skipped rows produce ZERO provider calls: they are never enqueued at all.
            await self.enqueue(row_id)
        log.info(
            "ingest_recorded row_id=%d message_id=%d rev=%d kind=%s status=%s images=%d catchup=%s edited=%s",
            row_id,
            message_id,
            snapshot["revision"],
            kind,
            snapshot["status"],
            len(images),
            bool(catchup),
            bool(edited),
        )
        return row_id

    @staticmethod
    def _deferred_to_album(message: Any, *, album: list | None, catchup: bool, edited: bool) -> bool:
        """Report whether Telethon's Album event owns this update and NewMessage must stand down.

        Telethon delivers an album as separate `NewMessage` updates *and* one reassembled `Album`
        event 0.5 s later. Handling both would write one row per album item plus one for the album,
        so the grouped `NewMessage` deliveries defer. A one-item "album" never fires `Album` and
        never carries a `grouped_id`, so it is unaffected; edits and catch-up carry their own
        grouping and are never deferred.
        """
        if album is not None or catchup or edited:
            return False
        return getattr(message, "grouped_id", None) is not None

    async def _active_source(self, session: AsyncSession) -> Source | None:
        """Return the single active source row, or None before the operator selected one."""
        result = await session.execute(
            select(Source).where(Source.is_active.is_(True)).order_by(Source.id).limit(1)
        )
        return result.scalars().first()

    async def _next_revision(
        self, session: AsyncSession, source_id: int, message_id: int, *, edited: bool
    ) -> int:
        """Return the revision for this row: an edit continues the chain, anything else starts one.

        An edit is a REVISION, not a new signal (§3.3) — treating it as a fresh signal is how one
        call becomes two positions. An edit of a row recorded `skipped` is re-classified from
        scratch (a voice note replaced by text becomes a real signal) and starts its own chain at 1.
        """
        if not edited:
            return 1
        result = await session.execute(
            select(Message.revision, Message.status, Message.skip_reason)
            .where(Message.source_id == source_id, Message.message_id == message_id)
            .order_by(Message.edit_date.desc(), Message.revision.desc(), Message.id.desc())
        )
        rows = result.all()
        if not rows:
            return 1
        # An identical-text skip is bookkeeping, not a voice note that was never a signal.
        # Ignore those rows when deciding whether the chain restarts; a skipped voice still
        # restarts at 1. The new number still continues past every stored revision.
        anchor = next(
            (row for row in rows if str(row[2] or "") != IDENTICAL_EDIT_REASON),
            None,
        )
        if anchor is None or str(anchor[1]) == "skipped":
            return 1
        return max(int(row[0] or 0) for row in rows) + 1

    @staticmethod
    def _snapshot(row: Message) -> dict[str, Any]:
        """Capture the feed-shaped fields of a row while its session is still open."""
        return {
            "id": int(row.id),
            "source_id": int(row.source_id),
            "message_id": int(row.message_id),
            "revision": int(row.revision),
            "kind": str(row.kind),
            "status": str(row.status),
            "skip_reason": row.skip_reason,
            "catchup": bool(row.catchup),
            "text": str(row.text or ""),
            "permalink": row.permalink,
            "posted_at": row.posted_at,
            "received_at": row.received_at,
            "image_count": int(row.image_count or 0),
            "reply_to_message_id": row.reply_to_message_id,
            "signal_ref": row.signal_ref,
        }

    def _emit_signal(self, snapshot: dict[str, Any]) -> None:
        """Emit SIGNAL_RECEIVED carrying the row's feed shape plus its true age and stale flag."""
        posted_at: datetime | None = snapshot.get("posted_at")
        received_at: datetime | None = snapshot.get("received_at")
        reference = received_at or _utcnow()
        age = 0.0
        if isinstance(posted_at, datetime):
            age = max(0.0, (reference - posted_at).total_seconds())
        payload: dict[str, Any] = {
            "id": snapshot["id"],
            "source_id": snapshot["source_id"],
            "message_id": snapshot["message_id"],
            "revision": snapshot["revision"],
            "kind": snapshot["kind"],
            "status": snapshot["status"],
            "skip_reason": snapshot["skip_reason"],
            "catchup": snapshot["catchup"],
            "text": snapshot["text"],
            "permalink": snapshot["permalink"],
            "posted_at": posted_at.isoformat() if isinstance(posted_at, datetime) else None,
            "received_at": received_at.isoformat() if isinstance(received_at, datetime) else None,
            "image_count": int(snapshot.get("image_count") or 0),
            "images": list(snapshot.get("images") or []),
            "reply_to_message_id": snapshot["reply_to_message_id"],
            "signal_ref": snapshot["signal_ref"],
            "runs_count": 0,
            "trades_count": 0,
            # A catch-up message must never look live: its true age travels with it from here
            # to the model's context (§8.4).
            "signal_age_seconds": round(age, 3),
            "stale": age > float(self.settings.STALE_SIGNAL_SECONDS),
        }
        self.runtime.bus.emit(events.SIGNAL_RECEIVED, **payload)

    async def _mark_identical_edit(self, message_row_id: int) -> None:
        """Record that this revision repeated the previous one and must not reach the model."""
        try:
            async with session_scope() as session:
                row = await session.get(Message, int(message_row_id))
                if row is None:
                    return
                row.status = "skipped"
                row.skip_reason = IDENTICAL_EDIT_REASON
        except Exception as exc:  # noqa: BLE001 - a missed badge must not drop the row
            log.warning("ingest_identical_mark_failed row_id=%s error=%s", message_row_id, exc)

    async def _is_identical_edit(
        self,
        *,
        source_id: int,
        message_id: int,
        row_id: int,
        text: str,
        kind: str,
        downloaded: list[dict[str, Any]],
    ) -> bool:
        """True when this edit's text matches the previous revision and no new image arrived.

        Comparison is byte-for-byte on the text and sha256 on image files. A new file that
        cannot be read fails open (the edit is enqueued) so a real chart is not dropped.
        An empty new image list is not a new image.
        """
        async with session_scope() as session:
            result = await session.execute(
                select(Message)
                .where(
                    Message.source_id == source_id,
                    Message.message_id == message_id,
                    Message.id != int(row_id),
                )
                .order_by(Message.edit_date.desc(), Message.revision.desc(), Message.id.desc())
                .limit(1)
            )
            previous = result.scalars().first()
            if previous is None:
                return False
            if (text or "") != (previous.text or ""):
                return False
            previous_paths = [str(image.path) for image in previous.images]
        if kind in IMAGE_KINDS and not downloaded:
            # The post is a chart and nothing was stored. That is not "no new image".
            return False
        previous_hashes: set[str] = set()
        for path in previous_paths:
            digest = _sha256_file(path)
            if digest is None:
                return False
            previous_hashes.add(digest)
        for item in downloaded:
            digest = _sha256_file(str(item.get("path") or ""))
            if digest is None or digest not in previous_hashes:
                return False
        return True

    # ------------------------------------------------------------------ images

    async def _download_images(self, items: Iterable[Any]) -> list[dict[str, Any]]:
        """Download every image of a message or album; a failed item never loses the message."""
        dest = self.settings.image_dir
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as exc:
            log.warning("ingest_image_dir_failed dir=%s error=%s", dest, exc)
            return []
        stored: list[dict[str, Any]] = []
        for item in items:
            try:
                path = await self.runtime.telegram.download_image(item, dest)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one unreadable photo is not a lost signal
                log.warning(
                    "ingest_image_download_failed message_id=%s error=%s",
                    getattr(item, "id", None),
                    exc,
                )
                continue
            if not path:
                continue
            width: int | None = None
            height: int | None = None
            try:
                width, height = await image_dimensions(path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - dimensions are metadata, not the payload
                log.warning("ingest_image_dimensions_failed path=%s error=%s", path, exc)
            try:
                size = int(os.path.getsize(path))
            except OSError:
                size = 0
            stored.append(
                {
                    "path": str(path),
                    "mime": self._image_mime(str(path), item),
                    "width": width,
                    "height": height,
                    "bytes": size,
                }
            )
        return stored

    @staticmethod
    def _image_mime(path: str, message: Any) -> str:
        """Return the stored image's mime: the file extension, then the document, then JPEG."""
        guessed, _ = mimetypes.guess_type(path)
        if guessed and guessed.startswith("image/"):
            return guessed
        document = getattr(message, "document", None)
        declared = getattr(document, "mime_type", "") or ""
        if isinstance(declared, str) and declared.startswith("image/"):
            return declared
        return _DEFAULT_IMAGE_MIME

    async def _persist_images(self, message_row_id: int, stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Write one MessageImage row per downloaded file and update the message's image_count."""
        out: list[dict[str, Any]] = []
        async with session_scope() as session:
            rows: list[MessageImage] = []
            for order_index, item in enumerate(stored):
                image = MessageImage(
                    message_row_id=message_row_id,
                    order_index=order_index,
                    path=item["path"],
                    mime=item["mime"],
                    width=item["width"],
                    height=item["height"],
                    bytes=int(item["bytes"]),
                )
                session.add(image)
                rows.append(image)
            await session.flush()
            row = await session.get(Message, message_row_id)
            if row is not None:
                row.image_count = len(rows)
            for image in rows:
                out.append(
                    {
                        "id": int(image.id),
                        "url": f"/api/telegram/image/{int(image.id)}",
                        "width": image.width,
                        "height": image.height,
                    }
                )
        return out

    # ------------------------------------------------------------------ service messages

    async def record_service(self, event: Any) -> None:
        """Record a ChatAction as a skipped `service` row so the feed shows it was not lost."""
        try:
            await self._record_service(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a service message must never break the handler
            log.exception("ingest_service_failed error=%s", exc)

    async def _record_service(self, event: Any) -> None:
        """The real body of `record_service`."""
        action_message = getattr(event, "action_message", None)
        message_id = int(getattr(action_message, "id", 0) or 0)
        received_at = _utcnow()
        posted_at = _naive_utc(getattr(action_message, "date", None)) or received_at
        # A ChatAction without an action message has no Telegram id to dedup on. Storing 0 for
        # every one of them would collapse them into a single row, so the arrival time acts as
        # the discriminator in the (source_id, message_id, edit_date) index instead.
        edit_date = 0 if message_id > 0 else int(received_at.replace(tzinfo=UTC).timestamp())
        text = self._service_text(event, action_message)

        async with session_scope() as session:
            source = await self._active_source(session)
            if source is None:
                return
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None:
                chat_id = getattr(action_message, "chat_id", None)
            if not _same_chat(source.chat_id, chat_id):
                return
            # Same rule as _handle_message: locals before any rollback can expire the row.
            source_id = int(source.id)
            source_chat_id = int(source.chat_id)
            source_username = source.username
            source_entity_type = str(source.entity_type)
            source_last_message_id = int(source.last_message_id or 0)
            row = Message(
                source_id=source_id,
                message_id=message_id,
                edit_date=edit_date,
                revision=1,
                kind="service",
                status="skipped",
                skip_reason=skip_reason("service"),
                catchup=False,
                text=text,
                reply_to_message_id=None,
                permalink=(
                    permalink(source_entity_type, source_username, source_chat_id, message_id)
                    if message_id > 0
                    else None
                ),
                posted_at=posted_at,
                received_at=received_at,
                grouped_id=None,
                image_count=0,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                log.info("ingest_service_duplicate source_id=%d message_id=%d", source_id, message_id)
                return
            snapshot = self._snapshot(row)
            if message_id > source_last_message_id:
                source.last_message_id = message_id

        snapshot["images"] = []
        self._emit_signal(snapshot)
        log.info("ingest_service_recorded row_id=%d message_id=%d text=%s", snapshot["id"], message_id, text)

    @staticmethod
    def _service_text(event: Any, action_message: Any) -> str:
        """Describe a service event in one short phrase for the feed badge."""
        action = getattr(action_message, "action", None)
        if action is not None:
            name = type(action).__name__
            if name.startswith("MessageAction"):
                name = name[len("MessageAction") :]
            if name:
                return name
        for attribute, label in (
            ("user_joined", "user joined"),
            ("user_added", "user added"),
            ("user_left", "user left"),
            ("user_kicked", "user removed"),
            ("new_title", "chat title changed"),
            ("new_photo", "chat photo changed"),
            ("photo", "chat photo changed"),
            ("pin", "message pinned"),
            ("created", "chat created"),
        ):
            if getattr(event, attribute, None):
                return label
        return "service message"

    # ------------------------------------------------------------------ catch-up

    async def catch_up(self) -> int:
        """Ingest the messages that arrived while the process was down (§8.4)."""
        async with session_scope() as session:
            source = await self._active_source(session)
            if source is None:
                log.debug("catchup_skipped reason=no_active_source")
                return 0
            # Locals, not attributes: `source` travels out of its session and is handed to
            # Telethon, and a lazy attribute load outside the session is IO in the wrong place.
            source_id = int(source.id)
            last_message_id = int(source.last_message_id or 0)

        if last_message_id <= 0:
            # The very first run for a source: record the current latest id as the baseline and
            # fetch nothing, so selecting a channel does not replay its backlog as live signals.
            return await self._record_baseline(source, source_id)

        limit = int(self.settings.CATCHUP_LIMIT)
        if limit <= 0:
            log.info("catchup_disabled source_id=%d limit=0", source_id)
            return 0
        try:
            fetched = await self.runtime.telegram.fetch_newer(source, limit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed catch-up must not abort startup
            detail = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            log.warning("catchup_fetch_failed source_id=%d error=%s", source_id, exc)
            self.runtime.bus.emit(
                events.SYSTEM_WARNING, reason="catchup_failed", source_id=source_id, message=detail
            )
            return 0

        messages = sorted(
            (m for m in (fetched or []) if int(getattr(m, "id", 0) or 0) > 0),
            key=lambda m: int(m.id),
        )
        recorded = 0
        highest = last_message_id
        for group in _group_albums(messages):
            anchor = group[0]
            # Catch-up groups its own albums, so the NewMessage/Album deferral never applies here.
            album = group if getattr(anchor, "grouped_id", None) is not None else None
            row_id = await self.handle_message(anchor, catchup=True, album=album)
            if row_id is not None:
                recorded += 1
            highest = max(highest, max(int(m.id) for m in group))

        if highest > last_message_id:
            await self._advance_last_message_id(source_id, highest)
        log.info(
            "catchup_done source_id=%d fetched=%d recorded=%d last_message_id=%d",
            source_id,
            len(messages),
            recorded,
            highest,
        )
        return recorded

    async def _record_baseline(self, source: Source, source_id: int) -> int:
        """Record the source's current latest message id without ingesting the backlog."""
        try:
            latest = await self.runtime.telegram.fetch_newer(source, 1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - without a baseline we simply try again later
            log.warning("catchup_baseline_failed source_id=%d error=%s", source_id, exc)
            return 0
        baseline = max((int(getattr(m, "id", 0) or 0) for m in (latest or [])), default=0)
        if baseline > 0:
            await self._advance_last_message_id(source_id, baseline)
        log.info("catchup_baseline source_id=%d last_message_id=%d backlog_replayed=no", source_id, baseline)
        return 0

    async def _advance_last_message_id(self, source_id: int, message_id: int) -> None:
        """Raise a source's `last_message_id` watermark; it never moves backwards."""
        async with session_scope() as session:
            source = await session.get(Source, int(source_id))
            if source is None:
                return
            if int(message_id) > int(source.last_message_id or 0):
                source.last_message_id = int(message_id)
