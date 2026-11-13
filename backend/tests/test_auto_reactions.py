"""Automatic reactions use a fake Telegram client and an isolated SQLite database."""
import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from telethon import functions, types
from telethon.errors import FloodWaitError

from app.db import init_db, session_scope, set_setting
from app.models import AutoReactionReceipt
from app.telegram.reactions import AutoReactions

CHAT = -1001234567890


class FakeTelegram:
    def __init__(self, allowed=None, premium=False):
        self.allowed = allowed or types.ChatReactionsSome([
            types.ReactionEmoji('👍'), types.ReactionEmoji('🔥'),
            types.ReactionCustomEmoji(42), types.ReactionPaid()])
        self.premium = premium
        self.manual = False
        self.sent = []
        self.requests = []
        self.flood_once = False
        self.fail_send = False

    async def __call__(self, request, **kwargs):
        self.requests.append(request)
        assert kwargs.get('flood_sleep_threshold') == 0
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(full_chat=SimpleNamespace(available_reactions=self.allowed))
        if isinstance(request, functions.messages.GetAvailableReactionsRequest):
            return SimpleNamespace(reactions=[SimpleNamespace(reaction='👍', inactive=False, premium=False),
                                               SimpleNamespace(reaction='👑', inactive=False, premium=True),
                                               SimpleNamespace(reaction='💤', inactive=True, premium=False)])
        if isinstance(request, functions.messages.SendReactionRequest):
            if self.flood_once:
                self.flood_once = False
                raise FloodWaitError(request=request, capture=1)
            if self.fail_send:
                raise TimeoutError('outcome unknown')
            self.sent.append(request)
            return SimpleNamespace()
        raise AssertionError(type(request))

    async def get_me(self):
        return SimpleNamespace(premium=self.premium)

    async def get_messages(self, entity, ids):
        entries = [SimpleNamespace(chosen_order=0)] if self.manual else []
        return SimpleNamespace(reactions=SimpleNamespace(results=entries))


def message(mid, *, age=0, chat=CHAT):
    return SimpleNamespace(id=mid, chat_id=chat, date=datetime.now(UTC)-timedelta(seconds=age))


async def manager_for(data_dir, *, allowed=None, premium=False, since=None):
    await init_db()
    async with session_scope() as s:
        await set_setting(s, 'tg.auto_reactions', {'enabled': True, 'since': since or time.time()-2})
    client = FakeTelegram(allowed=allowed, premium=premium)
    entity = types.Channel(id=1234567890, title='test', photo=types.ChatPhotoEmpty(), date=datetime.now(UTC), access_hash=1)
    async def resolve(_): return entity
    service = SimpleNamespace(client=client, _resolve_entity=resolve, _active_source_id=1, _active_chat_id=CHAT)
    manager = AutoReactions(service)
    await manager.refresh()
    return manager, client, service


async def receipts():
    async with session_scope() as s:
        return list((await s.execute(select(AutoReactionReceipt))).scalars().all())


async def test_new_posts_and_edits_are_deduplicated_across_restart(data_dir):
    manager, client, service = await manager_for(data_dir)
    manager.enqueue(message(10)); manager.enqueue(message(10)); manager.enqueue(message(11))
    await asyncio.wait_for(manager.queue.join(), 5)
    assert sorted(x.msg_id for x in client.sent) == [10, 11]
    assert all(isinstance(x.reaction[0], types.ReactionEmoji) for x in client.sent)
    assert {r.status for r in await receipts()} == {'sent'}
    second = AutoReactions(service); await second.refresh()
    second.enqueue(message(10))
    await asyncio.wait_for(second.queue.join(), 5)
    assert len(client.sent) == 2


async def test_disabled_old_foreign_and_manual_reactions_are_left_alone(data_dir):
    manager, client, service = await manager_for(data_dir, since=time.time()-2)
    manager.enqueue(message(1, age=60)); manager.enqueue(message(2, chat=CHAT-1))
    client.manual = True
    manager.enqueue(message(3))
    await asyncio.wait_for(manager.queue.join(), 5)
    assert client.sent == []
    assert [(r.message_id, r.status) for r in await receipts()] == [(3, 'skipped')]
    async with session_scope() as s:
        await set_setting(s, 'tg.auto_reactions', {'enabled': False, 'since': 0})
    await manager.refresh(); manager.enqueue(message(4))
    await asyncio.sleep(0)
    assert client.sent == [] and not manager.enabled


async def test_disabled_channel_and_premium_custom_reactions(data_dir):
    manager, client, service = await manager_for(data_dir, allowed=types.ChatReactionsNone())
    manager.enqueue(message(5)); await asyncio.wait_for(manager.queue.join(), 5)
    assert not client.sent
    client.allowed = types.ChatReactionsSome([types.ReactionCustomEmoji(123), types.ReactionPaid()])
    manager.cache.clear(); manager.enqueue(message(6)); await asyncio.wait_for(manager.queue.join(), 5)
    assert not client.sent
    client.premium = True
    manager.cache.clear(); manager.enqueue(message(7)); await asyncio.wait_for(manager.queue.join(), 5)
    assert len(client.sent) == 1 and isinstance(client.sent[0].reaction[0], types.ReactionCustomEmoji)


async def test_all_reactions_exclude_paid_premium_and_inactive_for_free_account(data_dir):
    manager, client, _ = await manager_for(data_dir, allowed=types.ChatReactionsAll())
    manager.enqueue(message(8)); await asyncio.wait_for(manager.queue.join(), 5)
    assert len(client.sent) == 1 and client.sent[0].reaction[0].emoticon == '👍'


async def test_flood_wait_is_respected_without_duplicate_send(data_dir):
    manager, client, _ = await manager_for(data_dir)
    client.flood_once = True
    manager.enqueue(message(9)); await asyncio.wait_for(manager.queue.join(), 5)
    assert len(client.sent) == 1
    assert [r.status for r in await receipts()] == ['sent']


async def test_unknown_send_outcome_is_not_retried_or_replaced_after_restart(data_dir):
    manager, client, service = await manager_for(data_dir)
    client.fail_send = True
    manager.enqueue(message(12)); await asyncio.wait_for(manager.queue.join(), 5)
    assert [r.status for r in await receipts()] == ['failed']
    client.fail_send = False
    again = AutoReactions(service); await again.refresh(); again.enqueue(message(12))
    await asyncio.wait_for(again.queue.join(), 5)
    assert not client.sent


async def test_reaction_network_delay_does_not_block_signal_ingest(data_dir):
    from app.config import settings
    from app.telegram.client import TelegramService
    manager, client, _ = await manager_for(data_dir)
    blocker = asyncio.Event()
    original = client.get_messages
    async def slow(entity, ids):
        await blocker.wait()
        return await original(entity, ids)
    client.get_messages = slow
    seen = []
    class Ingestor:
        async def handle_message(self, msg): seen.append(msg.id)
    service = TelegramService(lambda: SimpleNamespace(ingestor=Ingestor()), settings)
    service.client = client
    service._active_source_id = 1
    service._active_chat_id = CHAT
    service._resolve_entity = manager.service._resolve_entity
    await service.auto_reactions.refresh()
    msg = message(30); msg.grouped_id = None
    await asyncio.wait_for(service._handle_new_message(SimpleNamespace(chat_id=CHAT, message=msg)), .5)
    assert seen == [30] and not client.sent
    blocker.set()
    await asyncio.wait_for(service.auto_reactions.queue.join(), 5)
    assert len(client.sent) == 1


async def test_source_change_during_fetch_cannot_react_to_old_channel(data_dir):
    manager, client, service = await manager_for(data_dir)
    waiting = asyncio.Event()
    release = asyncio.Event()
    original = client.get_messages
    async def slow(entity, ids):
        waiting.set()
        await release.wait()
        return await original(entity, ids)
    client.get_messages = slow
    manager.enqueue(message(40))
    await asyncio.wait_for(waiting.wait(), 5)
    service._active_source_id = 2
    service._active_chat_id = CHAT-1
    release.set()
    await asyncio.wait_for(manager.queue.join(), 5)
    assert not client.sent and (await receipts())[0].status == 'skipped'


async def test_album_and_first_observed_edit_each_react_once(data_dir):
    from app.config import settings
    from app.telegram.client import TelegramService
    manager, client, _ = await manager_for(data_dir)
    seen = []
    class Ingestor:
        async def handle_message(self, msg, **kwargs): seen.append((msg.id, kwargs))
    service = TelegramService(lambda: SimpleNamespace(ingestor=Ingestor()), settings)
    service.client = client
    service._active_source_id = 1
    service._active_chat_id = CHAT
    service._resolve_entity = manager.service._resolve_entity
    await service.auto_reactions.refresh()
    a = message(41); a.grouped_id = 9; a.edit_date = datetime.now(UTC)
    b = message(42); b.grouped_id = 9
    await service._handle_message_edited(SimpleNamespace(chat_id=CHAT, message=a))
    await service._handle_album(SimpleNamespace(chat_id=CHAT, messages=[a, b]))
    await asyncio.wait_for(service.auto_reactions.queue.join(), 5)
    assert sorted(x.msg_id for x in client.sent) == [41, 42]
    assert len(seen) == 2


async def test_live_policy_probe_reads_allowed_free_reactions_without_sending(data_dir):
    manager, client, _ = await manager_for(data_dir)
    result = await manager.check()
    assert result['ok'] and result['enabled'] and result['available'] == 2
    assert client.sent == []


async def test_transient_read_failure_retries_before_sending(data_dir):
    manager, client, _ = await manager_for(data_dir)
    original = client.get_messages
    count = 0
    async def unstable(entity, ids):
        nonlocal count
        count += 1
        if count == 1:
            raise TimeoutError('read timed out')
        return await original(entity, ids)
    client.get_messages = unstable
    manager.enqueue(message(55))
    await asyncio.wait_for(manager.queue.join(), 5)
    assert count == 2 and len(client.sent) == 1


async def test_unique_reaction_limit_uses_an_existing_free_reaction(data_dir, monkeypatch):
    from telethon.errors import ReactionsTooManyError
    manager, client, _ = await manager_for(data_dir)
    async def existing(entity, ids):
        return SimpleNamespace(reactions=SimpleNamespace(results=[
            SimpleNamespace(chosen_order=None, reaction=types.ReactionEmoji('👍'))]))
    client.get_messages = existing
    original = FakeTelegram.__call__
    first = True
    async def limited(self, request, **kwargs):
        nonlocal first
        if isinstance(request, functions.messages.SendReactionRequest) and first:
            first = False
            raise ReactionsTooManyError(request=request)
        return await original(self, request, **kwargs)
    monkeypatch.setattr(FakeTelegram, '__call__', limited)
    monkeypatch.setattr('app.telegram.reactions.secrets.choice', lambda values: values[-1])
    manager.enqueue(message(56)); await asyncio.wait_for(manager.queue.join(), 5)
    assert len(client.sent) == 1 and client.sent[0].reaction[0].emoticon == '👍'
