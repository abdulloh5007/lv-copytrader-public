from app.ai.uz_gold_zone import parse_uz_gold_post


def test_two_digit_zone_uses_current_hundreds_as_owner_explained():
    post = parse_uz_gold_post('60-65 sell zone TP 45 TP 25', quote=4100)
    assert (post.kind, post.side, post.zone, post.targets) == ('entry', 'sell', (4160, 4165), (4145, 4125))
    assert post.sl is None and post.sl_pips is None


def test_observed_abbreviated_buy_targets_cross_hundred():
    post = parse_uz_gold_post('65-60 buy zone minimal risk TP 85 TP 105', quote=4065)
    assert (post.kind, post.zone, post.targets) == ('entry', (4060, 4065), (4085, 4105))
    assert post.sl is None


def test_observed_uzbek_personal_entry_forms_use_the_same_zone_and_target_rules():
    from app.ai.uz_gold_zone import entry_identity
    sell = parse_uz_gold_post('07-11 selldaman', quote=4007.2)
    assert (sell.kind, sell.side, sell.zone, sell.targets) == ('entry', 'sell', (4007, 4011), ())
    assert entry_identity('07-11 selldaman') == ('sell', '07', '11')
    buy = parse_uz_gold_post('Gold buydaman 4115-10kirsangiz boladi\n\nTP 4135\nTP 4155', quote=4112)
    assert (buy.kind, buy.side, buy.zone, buy.targets) == ('entry', 'buy', (4110, 4115), (4135, 4155))
    assert entry_identity('Gold buydaman 4115-10kirsangiz boladi\nTP 4135\nTP 4155') == ('buy', '4115', '10')


def test_uzbek_personal_entry_does_not_match_questions_negations_or_other_instruments():
    from app.ai.uz_gold_zone import entry_identity
    for text in ('07-11 selldamanmi?', '07-11 selldaman emas', 'BTC 07-11 selldaman',
                 'avval 07-11 selldaman', '07-11 selldaman yopdim'):
        assert entry_identity(text) is None
        assert parse_uz_gold_post(text, quote=4007).kind != 'entry'


def test_observed_full_and_mixed_abbreviated_zones():
    assert parse_uz_gold_post('3800-805 sell zone TP 3780 TP 3760', quote=3803).zone == (3800,3805)
    assert parse_uz_gold_post('4290-295sell zone TP 4265 TP 4245', quote=4291).zone == (4290,4295)
    assert parse_uz_gold_post('Gold buy 30-27 TP 40 TP 50', quote=4130).zone == (4127,4130)
    assert parse_uz_gold_post('4217-22 sel zone TP 4195 TP 4175', quote=4219).side == 'sell'
    post = parse_uz_gold_post('3914-19 sell zone minimal risk\nTP 3870\nTP 3750\nSl 40pips', quote=3916)
    assert post.kind == 'entry' and post.targets == (3870, 3750) and post.sl_pips == 40


def test_explicit_stop_pips_are_reported_not_converted_or_invented():
    post = parse_uz_gold_post('Gold 4146-42 buy zone Sl 30pips TP 4170 TP 4190', quote=4145)
    assert post.kind == 'entry' and post.sl_pips == 30 and post.sl is None


def test_channel_stop_extracts_one_level_without_guessing_distance():
    import pytest
    from app.ai.uz_gold_zone import channel_stop
    assert channel_stop('3914-19 sell zone\nSl 40pips') == (None, 40)
    assert channel_stop('SL 3924.5') == (3924.5, None)
    assert channel_stop('TP 3870') == (None, None)
    with pytest.raises(ValueError, match='ambiguous_stop'):
        channel_stop('SL 3924 SL 40pips')
    with pytest.raises(ValueError, match='invalid_stop_distance'):
        channel_stop('SL 0 pips')


def test_market_and_personal_entry_recognize_channel_stop():
    from app.ai.uz_gold_zone import entry_identity
    text = 'Gold sell now SL 40pips TP 2375 TP 2372'
    parsed = parse_uz_gold_post(text, quote=2380)
    assert parsed.kind == 'entry' and parsed.side == 'sell' and parsed.sl_pips == 40
    assert parsed.targets == (2375, 2372) and entry_identity(text) == ('sell', None, None)
    parsed = parse_uz_gold_post('78-81 selldaman SL 2390', quote=2380)
    assert parsed.kind == 'entry' and parsed.side == 'sell' and parsed.sl == 2390


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


def test_stop_hit_and_future_reentry_is_an_announcement_not_an_instruction():
    for text in ('Stop urdi reentry beraman', 'Stop urdi, re-entry beraman❗️'):
        post = parse_uz_gold_post(text, quote=None)
        assert (post.kind, post.action) == ('ignored', 'reentry_announced')


def test_market_now_is_unambiguous_and_does_not_invent_a_zone():
    from app.ai.uz_gold_zone import entry_identity
    for text, side in [('Gold sell now', 'sell'), ('buy now', 'buy'), ('XAUUSD BUY NOW!', 'buy')]:
        post = parse_uz_gold_post(text, quote=None)
        assert (post.kind, post.side, post.zone, post.action) == ('entry', side, None, 'market_now')
        assert entry_identity(text) == (side, None, None)
    for text in ['do not sell now', 'BTC sell now', 'sell now or buy now', 'sell later',
                 'Gold sell now 4040', 'we sold gold now']:
        assert entry_identity(text) is None


def test_market_now_followup_targets_use_actual_entry_as_reference():
    post = parse_uz_gold_post('Gold sell now\nTP 4025\nTP 4005', quote=4045)
    assert (post.kind, post.side, post.zone, post.targets) == ('entry', 'sell', None, (4025, 4005))
    assert parse_uz_gold_post('Gold sell now TP 4050 TP 4060', quote=4045).kind == 'invalid'


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


async def test_market_now_adding_zone_keeps_armed_but_direction_flip_disarms(data_dir):
    from datetime import datetime, timedelta, timezone
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
    await ingestor.handle_message(text_message(46, 'Gold sell now', chat_id=chat_id))
    await ingestor.handle_message(text_message(46, 'Gold sell now 4045-50 TP 4025 TP 4005',
                                              chat_id=chat_id, edit_date=datetime.now(timezone.utc)), edited=True)
    assert await runtime.armed()
    await ingestor.handle_message(text_message(46, 'Gold buy now 4045-50 TP 4065 TP 4085',
                                              chat_id=chat_id, edit_date=datetime.now(timezone.utc) + timedelta(seconds=1)), edited=True)
    assert not await runtime.armed()


async def test_deleting_market_now_signal_disarms(data_dir):
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
    await ingestor.handle_message(text_message(47, 'Gold sell now', chat_id=chat_id))
    await ingestor.handle_deleted([47])
    assert not await runtime.armed()


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
