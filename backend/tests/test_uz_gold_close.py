"""Channel close-at-break-even classification and position association."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.ai.uz_gold_zone import parse_uz_gold_post
from app.db import session_scope, set_setting
from app.errors import ConfigError
from app.models import Message, Source
from app.trading.executor import RunContext
from tests.test_executor import _build


async def seed_repost(data_dir, *, root_text='78-81 buy zone', deleted=True,
                      intervening=False, deleted_seconds=50):
    executor, broker, runtime = await _build(data_dir, armed=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    chat_id = -1005555555555
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
        original = Message(source_id=source.id, message_id=100, kind='text', status='pending',
                           text='81-78 buy zone', posted_at=now)
        session.add(original); await session.flush()
        entry = RunContext(run_id='opening', source_id=source.id, message_row_id=original.id,
                           signal_ref=original.signal_ref, origin='agent')
        source_id, original_row_id = source.id, original.id
    assert (await executor.open_uz_gold_signal(entry))['ok']
    async with session_scope() as session:
        original = await session.get(Message, original_row_id)
        original.posted_at = now - timedelta(minutes=2)
        original.deleted_at = now - timedelta(seconds=deleted_seconds) if deleted else None
        if intervening:
            session.add(Message(source_id=source_id, message_id=101, kind='text', status='processed',
                                text='81-78 sell zone', posted_at=now - timedelta(seconds=45)))
        root = Message(source_id=source_id, message_id=102, kind='text', status='processed',
                       text=root_text, posted_at=now - timedelta(seconds=40))
        session.add(root)
        reply = Message(source_id=source_id, message_id=103, reply_to_message_id=102,
                        kind='text', status='pending', text='B/u da yoping narx entry zoneda',
                        posted_at=now)
        session.add(reply); await session.flush()
        ctx = RunContext(run_id='close', source_id=source_id, message_row_id=reply.id,
                         signal_ref=reply.signal_ref, origin='agent')
    return executor, broker, runtime, ctx


async def test_deleted_signal_repost_resolves_only_original_positions(data_dir):
    executor, broker, runtime, ctx = await seed_repost(data_dir)
    existing = set(broker.positions)
    row, trades = await executor._uz_gold_close_target(ctx)
    assert row.message_id == 103 and {t.ticket for t in trades} == existing
    assert len(existing) == 2 and set(broker.positions) == existing


async def test_direct_reply_resolves_its_own_signal_only(data_dir):
    executor, broker, runtime, ctx = await seed_repost(data_dir)
    async with session_scope() as session:
        row = await session.get(Message, ctx.message_row_id)
        row.reply_to_message_id = 100
        original = (await session.execute(select(Message).where(
            Message.source_id == ctx.source_id, Message.message_id == 100))).scalars().first()
        original.deleted_at = None
    existing = set(broker.positions)
    _, trades = await executor._uz_gold_close_target(ctx)
    assert {t.ticket for t in trades} == existing
    assert set(broker.positions) == existing


@pytest.mark.parametrize('options', [
    {'root_text': '78-81 sell zone'},
    {'root_text': '79-82 buy zone'},
    {'deleted': False},
    {'intervening': True},
    {'deleted_seconds': 300},
])
async def test_ambiguous_repost_does_not_resolve_a_position(data_dir, options):
    executor, broker, runtime, ctx = await seed_repost(data_dir, **options)
    existing = set(broker.positions)
    with pytest.raises(ConfigError):
        await executor._uz_gold_close_target(ctx)
    assert set(broker.positions) == existing


def test_breakeven_commentary_is_not_an_order():
    assert parse_uz_gold_post('B/u da yoping narx entry zoneda', quote=None).action == 'close_at_break_even_request'
    assert parse_uz_gold_post('B/u ga obqoying', quote=None).action == 'break_even_request'
    assert parse_uz_gold_post('b/u da save qildingiz dep o’ylayman', quote=None).kind == 'ignored'
    assert parse_uz_gold_post('Do not close at b/u', quote=None).kind == 'ignored'


@pytest.mark.parametrize('answer,expected', [
    ('close', 'close'), ('move', 'move'), ('comment', 'comment'),
    ('uncertain', 'uncertain'), ('close 123', 'uncertain'), ('', 'uncertain'),
])
async def test_management_classifier_has_no_tools_and_rejects_malformed_answers(answer, expected):
    from app.ai.base import Capabilities
    from app.ai.triage import classify_gold_breakeven
    from tests.test_fast_entry import _ScriptedProvider
    provider = _ScriptedProvider(answer)
    caps = Capabilities(supports_vision=False, supports_tools=True, thinking_form=None, source='test')
    result = await classify_gold_breakeven(provider, model='model', capabilities=caps,
                                           text='B/u da yoping narx entry zoneda')
    assert result == expected and len(provider.calls) == 1
    assert provider.calls[0]['tools'] == []


async def test_management_classifier_failure_never_guesses(data_dir):
    from app.ai.base import Capabilities
    from app.ai.triage import classify_gold_breakeven
    from tests.test_fast_entry import _ScriptedProvider
    provider = _ScriptedProvider('close', boom=True)
    caps = Capabilities(supports_vision=False, supports_tools=True, thinking_form=None, source='test')
    assert await classify_gold_breakeven(provider, model='model', capabilities=caps, text='B/u da yoping') == 'uncertain'
