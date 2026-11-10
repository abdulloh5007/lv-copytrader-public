from app.ai.uz_gold_zone import parse_uz_gold_post


def test_two_digit_zone_uses_current_hundreds_as_owner_explained():
    post = parse_uz_gold_post('60-65 sell zone TP 45 TP 25', quote=4100)
    assert (post.kind, post.side, post.zone, post.targets) == ('entry', 'sell', (4160, 4165), (4145, 4125))
    assert post.sl is None and post.sl_pips is None


def test_observed_abbreviated_buy_targets_cross_hundred():
    post = parse_uz_gold_post('65-60 buy zone minimal risk TP 85 TP 105', quote=4065)
    assert (post.kind, post.zone, post.targets) == ('entry', (4060, 4065), (4085, 4105))
    assert post.sl is None


def test_observed_full_and_mixed_abbreviated_zones():
    assert parse_uz_gold_post('3800-805 sell zone TP 3780 TP 3760', quote=3803).zone == (3800,3805)
    assert parse_uz_gold_post('4290-295sell zone TP 4265 TP 4245', quote=4291).zone == (4290,4295)
    assert parse_uz_gold_post('Gold buy 30-27 TP 40 TP 50', quote=4130).zone == (4127,4130)
    assert parse_uz_gold_post('4217-22 sel zone TP 4195 TP 4175', quote=4219).side == 'sell'


def test_explicit_stop_pips_are_reported_not_converted_or_invented():
    post = parse_uz_gold_post('Gold 4146-42 buy zone Sl 30pips TP 4170 TP 4190', quote=4145)
    assert post.kind == 'entry' and post.sl_pips == 30 and post.sl is None


def test_conflicting_full_prices_or_instrument_are_rejected():
    assert parse_uz_gold_post('3813-08 buy zone TP 4833 TP 4853', quote=3810).reason == 'ambiguous_or_contradictory_target'
    assert parse_uz_gold_post('4103-07 sell zone TP 4092 TP 4982', quote=4105).kind == 'invalid'
    assert parse_uz_gold_post('82700-650 buy zone TP 82900 TP 83100', quote=4130).kind == 'invalid'
    assert parse_uz_gold_post('BTC 60-65 buy TP 85 TP 105', quote=4065).kind == 'invalid'
    assert parse_uz_gold_post('65-60 buy TP 85 TP 105', quote=None).kind == 'invalid'


def test_management_messages_do_not_become_new_entries():
    assert parse_uz_gold_post('B/u ga obqoying❗️', quote=4130).action == 'break_even_request'
    assert parse_uz_gold_post('Yoping buylarni', quote=4130).action == 'close_request'
    assert parse_uz_gold_post('09-14 reentry 20sl zone', quote=4130).action == 'reentry_request'
    assert parse_uz_gold_post('450pips 2-TP✅', quote=4130).kind == 'ignored'


async def test_research_mode_blocks_open_and_model_writes_even_when_armed(data_dir):
    from datetime import datetime, timezone
    from app.db import set_setting
    CHANNEL_CHAT_ID = -1005555555555
    from app.db import session_scope
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Example signals', username=None, entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', CHANNEL_CHAT_ID)
        row = Message(source_id=source.id, message_id=1, kind='text', status='pending', text='65-60 buy zone TP 85 TP 105', posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=row.id, signal_ref=row.signal_ref, origin='agent')
    result = await executor.open_market(ctx, symbol='XAUUSDm', side='buy', sl=2367, reason='test')
    assert not result['ok'] and result['stage'] == 'channel_profile'
    result = await executor.dispatch('open_market', {'symbol':'XAUUSDm', 'side':'buy', 'reason':'test'}, ctx)
    assert not result['ok'] and result['stage'] == 'channel_profile'
    assert not broker.positions


async def test_research_mode_never_calls_model_or_opens_a_trade(data_dir):
    from app.db import set_setting
    CHANNEL_CHAT_ID = -1005555555555
    from app.db import session_scope
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Example signals', username=None, entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', CHANNEL_CHAT_ID)
    provider = _ScriptedProvider('yes')
    _install_provider(runtime, provider)
    _, row_id = await _seed('65-60 buy zone TP 85 TP 105', message_id=101)
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'deferred'
    assert provider.calls == [] and not broker.positions
    from sqlalchemy import select
    from app.models import RunStep
    async with session_scope() as session:
        step = (await session.execute(select(RunStep).where(
            RunStep.run_id == result['run_id'], RunStep.name == 'uz_gold_zone'
        ))).scalars().one()
        assert step.payload['no_trade'] is True
        assert step.payload['post']['zone'] == [2360.0, 2365.0]
        assert step.payload['quote']['quote_freshness_verified'] is False


async def test_research_never_reinterprets_old_posts_using_a_current_quote(data_dir):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.db import session_scope, set_setting
    from app.models import Source, RunStep
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _ScriptedProvider, _seed
    chat_id = -1005555555555
    executor, broker, runtime = await _build(data_dir, armed=True)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Example signals', username=None, entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _, row_id = await _seed('65-60 buy zone TP 85 TP 105', message_id=102, posted_at=now-timedelta(hours=4))
    result = await _drive(runtime, _ScriptedProvider('yes'), row_id)
    async with session_scope() as session:
        step = (await session.execute(select(RunStep).where(RunStep.run_id == result['run_id'], RunStep.name == 'uz_gold_zone'))).scalars().one()
        assert step.payload['quote'] == {}
        assert step.payload['post']['reason'] == 'missing_or_invalid_gold_quote'
    assert not broker.positions


def test_teaser_without_targets_is_identified_as_entry_not_fabricated():
    post = parse_uz_gold_post('68-64 buy zone minimal risk', quote=4066)
    assert post.kind == 'entry' and post.zone == (4064, 4068)
    assert post.targets == () and post.sl is None


def test_entry_identity_does_not_guess_short_prices_or_turn_management_into_entries():
    from app.ai.uz_gold_zone import entry_identity
    assert entry_identity('68-64 buy zone') == ('buy', '68', '64')
    assert entry_identity('68-64 sell zone') == ('sell', '68', '64')
    assert entry_identity('68-64 buy reentry') is None


async def test_direction_edit_disarms_research_source_and_keeps_positions_untouched(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_ingest import _build, text_message
    chat_id = -1005555555555
    ingestor, runtime, source_id = await _build(data_dir)
    async with session_scope() as session:
        source = await session.get(Source, source_id)
        source.chat_id = chat_id
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
    await runtime.set_armed(True)
    await ingestor.handle_message(text_message(42, '68-64 buy zone', chat_id=chat_id))
    assert await runtime.armed()
    edited = await ingestor.handle_message(text_message(42, '68-64 sell zone', chat_id=chat_id, edit_date=datetime.now(timezone.utc)), edited=True)
    assert edited is not None and not await runtime.armed()


async def test_deleting_new_source_signal_disarms_without_closing_positions(data_dir):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_ingest import _build, text_message
    chat_id = -1005555555555
    ingestor, runtime, source_id = await _build(data_dir)
    async with session_scope() as session:
        source = await session.get(Source, source_id)
        source.chat_id = chat_id
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
    await runtime.set_armed(True)
    await ingestor.handle_message(text_message(45, '68-64 buy zone', chat_id=chat_id))
    calls=[]
    async def no_close(*args, **kwargs): calls.append((args,kwargs))
    ingestor._flatten=no_close
    await ingestor.handle_deleted([45])
    assert not await runtime.armed() and calls == []
