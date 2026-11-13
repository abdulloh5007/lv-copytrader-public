"""Optional, bounded Telegram reactions, independent of signal execution."""
from __future__ import annotations

import asyncio
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.dialects.sqlite import insert
from telethon import functions, types
from telethon.errors import (FloodWaitError, PremiumAccountRequiredError,
                             ReactionInvalidError, ReactionsTooManyError)

from app.db import get_setting, session_scope
from app.models import AutoReactionReceipt, utcnow
from app.util.logging import get_logger

log = get_logger(__name__)
REACTION_SETTING = 'tg.auto_reactions'
MAX_PENDING = 256
CACHE_SECONDS = 300
RPC_TIMEOUT = 12


class AutoReactions:
    """Queue post IDs on the active Telethon session, never on the trade path."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.queue: asyncio.Queue[tuple[int, int, int, Any]] = asyncio.Queue(maxsize=MAX_PENDING)
        self.pending: set[tuple[int, int]] = set()
        self.worker: asyncio.Task[None] | None = None
        self.cache: dict[int, tuple[float, list[Any]]] = {}
        self.prune_at = 0.0
        self.enabled = False

    async def refresh(self) -> None:
        async with session_scope() as s:
            cfg = await get_setting(s, REACTION_SETTING, {})
        self.enabled = isinstance(cfg, dict) and cfg.get('enabled') is True
        if not self.enabled:
            self.cancel()

    async def check(self) -> dict[str, Any]:
        """Read the live channel policy without reacting or changing the account."""
        client = self.service.client
        chat_id = self.service._active_chat_id
        if client is None or not chat_id or not self.service._active_source_id:
            return {'ok': False, 'enabled': self.enabled, 'available': 0, 'message': 'Telegram or source unavailable'}
        try:
            entity = await asyncio.wait_for(self.service._resolve_entity(chat_id), timeout=RPC_TIMEOUT)
            choices = await self._choices(client, entity, chat_id)
            return {'ok': True, 'enabled': self.enabled, 'available': len(choices),
                    'message': 'Channel reactions checked'}
        except Exception as exc:
            log.warning('auto_reaction_check_failed error=%s', type(exc).__name__)
            return {'ok': False, 'enabled': self.enabled, 'available': 0,
                    'message': 'Could not check channel reactions'}

    def enqueue(self, message: Any) -> None:
        if not self.enabled:
            return
        try:
            chat_id = int(getattr(message, 'chat_id', 0) or 0)
            message_id = int(getattr(message, 'id', 0) or 0)
            source_id = int(self.service._active_source_id or 0)
        except (TypeError, ValueError):
            return
        if not source_id or not message_id or chat_id != self.service._active_chat_id:
            return
        # Telegram service messages are not channel posts and may not accept reactions.
        if isinstance(message, types.MessageService):
            return
        key = (chat_id, message_id)
        if key in self.pending:
            return
        try:
            self.queue.put_nowait((source_id, chat_id, message_id, message))
        except asyncio.QueueFull:
            log.warning('auto_reaction_queue_full message_id=%s', message_id)
            return
        self.pending.add(key)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run(), name='telegram-auto-reactions')

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.worker = None
        self.queue = asyncio.Queue(maxsize=MAX_PENDING)
        self.pending.clear()
        self.cache.clear()

    async def _run(self) -> None:
        while True:
            queue = self.queue
            try:
                source_id, chat_id, message_id, message = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self._process(source_id, chat_id, message_id, message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Never affect signal ingestion.
                log.warning('auto_reaction_failed message_id=%s error=%s', message_id, type(exc).__name__)
            finally:
                self.pending.discard((chat_id, message_id))
                queue.task_done()

    async def _allowed_setting(self, source_id: int, chat_id: int, message: Any) -> bool:
        if self.service._active_source_id != source_id or self.service._active_chat_id != chat_id:
            return False
        posted = getattr(message, 'date', None)
        if not isinstance(posted, datetime):
            return False
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=UTC)
        async with session_scope() as s:
            cfg = await get_setting(s, REACTION_SETTING, {})
        if not isinstance(cfg, dict) or cfg.get('enabled') is not True:
            return False
        try:
            since = float(cfg['since'])
            # Telegram timestamps have one-second resolution.
            return since > 0 and posted.timestamp() >= int(since)
        except (KeyError, ValueError, TypeError, OverflowError):
            return False

    async def _claim(self, chat_id: int, message_id: int) -> bool:
        async with session_scope() as s:
            if time.monotonic() >= self.prune_at:
                await s.execute(delete(AutoReactionReceipt).where(
                    AutoReactionReceipt.created_at < utcnow() - timedelta(days=30)))
                self.prune_at = time.monotonic() + 3600
            stmt = insert(AutoReactionReceipt).values(chat_id=chat_id, message_id=message_id,
                                                       status='claimed', created_at=utcnow())
            result = await s.execute(stmt.on_conflict_do_nothing(index_elements=['chat_id', 'message_id']))
            return result.rowcount == 1

    async def _finish(self, chat_id: int, message_id: int, status: str) -> None:
        async with session_scope() as s:
            await s.execute(update(AutoReactionReceipt).where(
                AutoReactionReceipt.chat_id == chat_id, AutoReactionReceipt.message_id == message_id
            ).values(status=status))

    async def _rpc(self, client: Any, request: Any) -> Any:
        return await asyncio.wait_for(client(request, flood_sleep_threshold=0), timeout=RPC_TIMEOUT)

    async def _choices(self, client: Any, entity: Any, chat_id: int) -> list[Any]:
        cached = self.cache.get(chat_id)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        if isinstance(entity, types.Channel):
            full = await self._rpc(client, functions.channels.GetFullChannelRequest(entity))
        elif isinstance(entity, types.Chat):
            full = await self._rpc(client, functions.messages.GetFullChatRequest(entity.id))
        else:
            return []
        allowed = getattr(full.full_chat, 'available_reactions', None)
        if isinstance(allowed, types.ChatReactionsNone) or allowed is None:
            choices: list[Any] = []
        else:
            me = await asyncio.wait_for(client.get_me(), timeout=RPC_TIMEOUT)
            premium = bool(getattr(me, 'premium', False))
            if isinstance(allowed, types.ChatReactionsSome):
                choices = [x for x in allowed.reactions if isinstance(x, types.ReactionEmoji)
                           or (premium and isinstance(x, types.ReactionCustomEmoji))]
            elif isinstance(allowed, types.ChatReactionsAll):
                available = await self._rpc(client, functions.messages.GetAvailableReactionsRequest(hash=0))
                choices = [types.ReactionEmoji(emoticon=x.reaction) for x in getattr(available, 'reactions', [])
                           if not getattr(x, 'inactive', False)
                           and (premium or not getattr(x, 'premium', False)) and getattr(x, 'reaction', '')]
            else:
                choices = []
        self.cache[chat_id] = (time.monotonic(), choices)
        return choices

    async def _process(self, source_id: int, chat_id: int, message_id: int, message: Any) -> None:
        if not await self._allowed_setting(source_id, chat_id, message):
            return
        if not await self._claim(chat_id, message_id):
            return
        client = self.service.client
        if client is None:
            await self._finish(chat_id, message_id, 'failed')
            return
        try:
            read_retries = 0
            while True:
                send_started = False
                try:
                    if not await self._allowed_setting(source_id, chat_id, message):
                        await self._finish(chat_id, message_id, 'skipped')
                        return
                    entity = await asyncio.wait_for(self.service._resolve_entity(chat_id), timeout=RPC_TIMEOUT)
                    choices = await self._choices(client, entity, chat_id)
                    if not choices:
                        log.info('auto_reaction_unavailable message_id=%s', message_id)
                        await self._finish(chat_id, message_id, 'skipped')
                        return
                    current = await asyncio.wait_for(client.get_messages(entity, ids=message_id), timeout=RPC_TIMEOUT)
                    if current is None:
                        await self._finish(chat_id, message_id, 'skipped')
                        return
                    counts = getattr(getattr(current, 'reactions', None), 'results', None) or []
                    if any(getattr(item, 'chosen_order', None) is not None for item in counts):
                        await self._finish(chat_id, message_id, 'skipped')
                        return
                    if not await self._allowed_setting(source_id, chat_id, message):
                        await self._finish(chat_id, message_id, 'skipped')
                        return
                    reaction = secrets.choice(choices)
                    try:
                        send_started = True
                        await self._rpc(client, functions.messages.SendReactionRequest(
                            peer=entity, msg_id=message_id, reaction=[reaction]))
                    except ReactionsTooManyError:
                        # Telegram can limit unique reactions per post. A reaction
                        # already on the post is still available without adding a type.
                        existing = [item.reaction for item in counts
                                    if getattr(item, 'reaction', None) in choices]
                        if not existing:
                            raise
                        await self._rpc(client, functions.messages.SendReactionRequest(
                            peer=entity, msg_id=message_id, reaction=[secrets.choice(existing)]))
                    except (ReactionInvalidError, PremiumAccountRequiredError):
                        # These are definite Telegram rejections, so another free
                        # allowed emoji can be tried without repeating a sent reaction.
                        alternatives = [item for item in choices
                                        if item != reaction and isinstance(item, types.ReactionEmoji)]
                        if not alternatives:
                            raise
                        self.cache.pop(chat_id, None)
                        await self._rpc(client, functions.messages.SendReactionRequest(
                            peer=entity, msg_id=message_id, reaction=[secrets.choice(alternatives)]))
                    await self._finish(chat_id, message_id, 'sent')
                    log.info('auto_reaction_sent message_id=%s', message_id)
                    return
                except FloodWaitError as exc:
                    # Telegram definitively rejected the call; respect its wait outside ingest.
                    delay = max(1, int(exc.seconds))
                    log.warning('auto_reaction_flood_wait seconds=%s', delay)
                    await asyncio.sleep(delay)
                except (TimeoutError, OSError):
                    if send_started or read_retries >= 2:
                        raise
                    read_retries += 1
                    await asyncio.sleep(read_retries)
        except asyncio.CancelledError:
            raise
        except ReactionInvalidError:
            self.cache.pop(chat_id, None)
            await self._finish(chat_id, message_id, 'failed')
            log.warning('auto_reaction_invalid message_id=%s', message_id)
        except Exception as exc:
            self.cache.pop(chat_id, None)
            await self._finish(chat_id, message_id, 'failed')
            log.warning('auto_reaction_failed message_id=%s error=%s', message_id, type(exc).__name__)
