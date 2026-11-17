from app.ai.xauuad_template import parse_channel_post


def test_zone_and_teaser():
    post = parse_channel_post('✌️ XAUUSD BUY 4014=11 ✌️')
    assert (post.kind, post.side, post.zone) == ('entry', 'buy', (4011.0, 4014.0))


def test_sell_zone_with_zero_prefix_and_ten_targets():
    post = parse_channel_post('SELL 3905=08 ' + ' '.join(f'TP {p}' for p in range(3902, 3872, -3)) + ' SL 3918')
    assert post.kind == 'complete'
    assert post.side == 'sell' and post.zone == (3905.0, 3908.0)
    assert len(post.targets) == 10 and post.sl == 3918.0


def test_management_does_not_open_or_modify():
    assert parse_channel_post('( XAUUSD ) BUY TARGET 1 COMPLETE 30+ PIPS DONE').kind == 'management'
    assert parse_channel_post('HOLD INSHALLAH BIG FLY').kind == 'management'
    assert parse_channel_post('SL is part of trading').kind == 'management'
    assert parse_channel_post('SECOND BUY LIMIT ACTIVE').kind == 'management'


def test_rejects_inconsistent_and_incomplete_levels():
    assert parse_channel_post('XAUUSD BUY 3989=86 TP 3992 SL 4076').reason == 'levels_conflict_with_zone'
    assert parse_channel_post('XAUUSD BUY 3995=92 TP 3898 SL 3882').reason == 'levels_conflict_with_zone'
    assert parse_channel_post('SELL 3970=73 TP 3967 SL 3973').reason == 'levels_conflict_with_zone'
    assert parse_channel_post('XAUUSD BUY 4014=11 TP 4017').reason == 'incomplete_or_ambiguous_levels'
    assert parse_channel_post('XAUUSD BUY 4014=11 TP 4017 SL 4001 SL 4000').reason == 'incomplete_or_ambiguous_levels'
    assert parse_channel_post('XAUUSD BUY 4014=11 SELL').reason == 'ambiguous_entry_or_symbol'


def test_crossing_hundreds_and_ambiguous_zone():
    assert parse_channel_post('XAUUSD BUY 4002=99').zone == (3999.0, 4002.0)
    assert parse_channel_post('XAUUSD BUY 4050=00').kind == 'invalid'
    assert parse_channel_post('XAUUSD BUY 4000=40').kind == 'invalid'


def test_correlates_threaded_and_unthreaded_followups_without_reopening():
    from app.ai.xauuad_template import Teaser, match_teaser

    teaser = Teaser(100, 1000, 'XAUUSD BUY 4014=11')
    complete = 'XAUUSD BUY 4014=11 TP 4017 TP 4020 TP 4023 TP 4026 SL 4001'
    assert match_teaser(complete, reply_to=100, posted_at=1060, teasers=[teaser]).message_id == 100
    assert match_teaser(complete, reply_to=None, posted_at=1060, teasers=[teaser]).message_id == 100
    assert match_teaser(complete, reply_to=999, posted_at=1060, teasers=[teaser]).kind == 'missing'
    assert match_teaser(complete, reply_to=None, posted_at=1200, teasers=[teaser]).kind == 'missing'
    duplicate = Teaser(101, 1020, teaser.text)
    assert match_teaser(complete, reply_to=None, posted_at=1060, teasers=[teaser, duplicate]).kind == 'ambiguous'


def test_rejects_changed_entry_even_when_reply_points_to_teaser():
    from app.ai.xauuad_template import Teaser, match_teaser

    teaser = Teaser(100, 1000, 'XAUUSD BUY 3974=71')
    complete = 'XAUUSD BUY 3974=72 TP 3977 TP 3980 TP 3983 TP 3986 SL 3961'
    assert match_teaser(complete, reply_to=100, posted_at=1060, teasers=[teaser]).kind == 'conflict'


def test_proposed_profile_opens_two_point_two_lots_and_targets_second_and_fourth():
    from app.ai.xauuad_template import Teaser, plan_channel_post

    teaser = Teaser(100, 1000, 'XAUUSD BUY 4014=11')
    entry = plan_channel_post(teaser.text)
    assert entry.action == 'open_without_sl' and entry.volume_per_order == 0.2
    assert entry.sl is None and entry.move_second_to_be_after_first
    followup = 'XAUUSD BUY 4014=11 TP 4017 TP 4020 TP 4023 TP 4026 TP 4029 SL 4001'
    result = plan_channel_post(followup, reply_to=100, posted_at=1060, teasers=[teaser])
    assert result.action == 'attach_levels' and result.teaser_id == 100
    assert result.sl == 4001 and result.targets == (4020, 4026)
    assert result.move_second_to_be_after_first
    assert plan_channel_post(followup, reply_to=100, posted_at=1060).action == 'reject'


def test_existing_legs_validation_accepts_stopless_and_never_widens_watchdog_stop():
    from app.ai.xauuad_template import ExistingLeg, Teaser, plan_channel_post, validate_existing_legs

    teaser = Teaser(100, 1000, 'XAUUSD BUY 4014=11')
    complete = 'XAUUSD BUY 4014=11 TP 4017 TP 4020 TP 4023 TP 4026 SL 4001'
    plan = plan_channel_post(complete, reply_to=100, posted_at=1060, teasers=[teaser])
    legs = [ExistingLeg(1, 'buy', 0.2, 'cover', 0), ExistingLeg(2, 'buy', 0.2, 'runner', 0)]
    assert validate_existing_legs(plan, legs) is None
    tighter_stop = [legs[0], ExistingLeg(2, 'buy', 0.2, 'runner', 4005)]
    assert validate_existing_legs(plan, tighter_stop) == 'would_widen_existing_stop'
    assert validate_existing_legs(plan, legs[:1]) == 'expected_two_distinct_legs'
    assert validate_existing_legs(plan, [legs[0], ExistingLeg(2, 'sell', 0.2, 'runner', 0)]) == 'side_or_volume_mismatch'


def test_fast_reader_scopes_zone_interpretation_to_selected_channel():
    from app.ai.fast_entry import read_post
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    source = read_post('XAUUSD BUY 4014=11', source_chat_id=CHANNEL_CHAT_ID)
    assert source.zone == (4011, 4014) and source.entry is None
    other = read_post('XAUUSD BUY 4014=11')
    assert other.entry == 4014.11 and other.zone is None
    bad = read_post('XAUUSD BUY 3995=92 TP 3898 SL 3882', source_chat_id=CHANNEL_CHAT_ID)
    assert bad.ambiguous and not bad.fresh


async def test_teaser_opens_only_two_configured_legs_on_fake_broker(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from tests.test_executor import _build
    executor, broker, _ = await _build(data_dir)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test',
                        username='test', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        row = Message(source_id=source.id, message_id=1, kind='text', status='pending',
                      text='XAUUSD BUY 2381=78', posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=row.id,
                         signal_ref=row.signal_ref, origin='agent')
    result = await executor.open_market(ctx, symbol='XAUUSDm', side='buy', reason='test')
    assert result['ok'], result
    assert sorted(p['volume'] for p in broker.positions.values()) == [0.2, 0.2]
    assert result['runner_mode'] == 'be_after_cover'
    assert all(p['sl'] == 0 for p in broker.positions.values())


async def test_full_post_never_opens_second_pair_on_fake_broker(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from tests.test_executor import _build
    executor, broker, _ = await _build(data_dir)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test', username='test', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        row = Message(source_id=source.id, message_id=2, kind='text', status='pending', text='XAUUSD BUY 2381=78 TP 2384 TP 2387 TP 2390 TP 2393 SL 2367', posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=row.id, signal_ref=row.signal_ref, origin='agent')
    result = await executor.open_market(ctx, symbol='XAUUSDm', side='buy', sl=2367, tp=[2387,2393], reason='test')
    assert not result['ok'] and result['stage'] == 'channel_profile'
    assert not broker.positions


async def test_unthreaded_followup_attaches_tp2_tp4_without_duplicate(data_dir):
    from datetime import datetime, timedelta, timezone
    from app.db import session_scope
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from tests.test_executor import _build
    executor, broker, _ = await _build(data_dir)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test', username='test', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        teaser = Message(source_id=source.id, message_id=1, kind='text', status='processed', text='XAUUSD BUY 2381=78', posted_at=now)
        session.add(teaser); await session.flush()
        entry_ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=teaser.id, signal_ref=teaser.signal_ref, origin='agent')
    opened = await executor.open_market(entry_ctx, symbol='XAUUSDm', side='buy', reason='test')
    assert opened['ok'], opened
    async with session_scope() as session:
        followup = Message(source_id=entry_ctx.source_id, message_id=2, kind='text', status='pending', text='XAUUSD BUY 2381=78 TP 2384 TP 2387 TP 2390 TP 2393 SL 2367', posted_at=now + timedelta(seconds=60))
        session.add(followup); await session.flush()
        ctx = RunContext(run_id='test1235', source_id=entry_ctx.source_id, message_row_id=followup.id, signal_ref=followup.signal_ref, origin='agent')
    result = await executor.attach_xauuad_levels(ctx)
    assert result['ok'], result
    assert len(broker.positions) == 2
    assert sorted(p['tp'] for p in broker.positions.values()) == [2387, 2393]
    assert all(p['sl'] == 2367 for p in broker.positions.values())
    again = await executor.attach_xauuad_levels(ctx)
    assert again['ok'] and len(broker.positions) == 2
    cover = next(ticket for ticket, position in broker.positions.items() if position['tp'] == 2387)
    runner = next(ticket for ticket, position in broker.positions.items() if position['tp'] == 2393)
    entry = broker.positions[runner]['price_open']
    broker.move('XAUUSDm', 2390.0)
    del broker.positions[cover]
    trailed = await executor.trail_ladder_runners()
    assert runner in trailed['trailed'], trailed
    assert broker.positions[runner]['sl'] >= entry - 1e-9


async def test_unthreaded_signals_share_lock_and_direct_ai_writes_are_refused(data_dir):
    from datetime import datetime, timezone
    from app.ai.loop import _chain_key_for
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from app.db import session_scope
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build

    executor, broker, _ = await _build(data_dir)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test', username='test', entity_type='channel', is_active=True)
        session.add(source)
        await session.flush()
        rows = [Message(source_id=source.id, message_id=n, kind='text', status='pending',
                        text=text, posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
                for n, text in [(1, 'XAUUSD BUY 2381=78'),
                                (2, 'XAUUSD BUY 2381=78 TP 2384 TP 2387 TP 2390 TP 2393 SL 2367')]]
        session.add_all(rows)
        await session.flush()
        source_id, first_id, second_id = source.id, rows[0].id, rows[1].id
    assert await _chain_key_for(first_id, 'signal') == await _chain_key_for(second_id, 'signal')
    ctx = RunContext(run_id='test1234', source_id=source_id, message_row_id=first_id,
                     signal_ref=f'SIG:{source_id}:1', origin='agent')
    result = await executor.dispatch('open_market', {'symbol': 'XAUUSDm', 'side': 'buy', 'reason': 'test'}, ctx)
    assert not result['ok'] and result['stage'] == 'channel_profile'
    assert not broker.positions


async def test_missing_channel_message_cannot_bypass_source_guard(data_dir):
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from app.db import session_scope
    from app.models import Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, _ = await _build(data_dir)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test', username='test', entity_type='channel', is_active=True)
        session.add(source)
        await session.flush()
        source_id = source.id
    ctx = RunContext(run_id='test1234', source_id=source_id, message_row_id=999999,
                     signal_ref=f'SIG:{source_id}:1', origin='agent')
    result = await executor.dispatch('open_market', {'symbol': 'XAUUSDm', 'side': 'buy', 'reason': 'test'}, ctx)
    assert not result['ok']
    assert not broker.positions


async def test_signal_pipeline_opens_then_attaches_even_if_provider_goes_down(data_dir):
    from datetime import datetime, timedelta, timezone
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from app.db import session_scope
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed

    executor, broker, runtime = await _build(data_dir)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test', username='test',
                           entity_type='channel', is_active=True))
    provider = _ScriptedProvider('yes')
    _install_provider(runtime, provider)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _, first = await _seed('XAUUSD BUY 2381=78', message_id=900, posted_at=now)
    entry = await _drive(runtime, provider, first)
    assert entry['decision'] == 'acted', entry
    assert sorted(position['volume'] for position in broker.positions.values()) == [0.2, 0.2]
    _, second = await _seed('XAUUSD BUY 2381=78 TP 2384 TP 2387 TP 2390 TP 2393 SL 2367',
                            message_id=901, posted_at=now + timedelta(seconds=1))

    async def unavailable_provider():
        raise RuntimeError('provider unavailable')

    runtime.provider = unavailable_provider
    followup = await _drive(runtime, provider, second)
    assert followup['decision'] == 'acted', followup
    assert sorted(position['tp'] for position in broker.positions.values()) == [2387, 2393]
    assert all(position['sl'] == 2367 for position in broker.positions.values())
    assert len(broker.positions) == 2


async def test_one_ai_check_gates_each_new_entry(data_dir):
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from app.db import session_scope
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed

    executor, broker, runtime = await _build(data_dir)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test', username='test',
                           entity_type='channel', is_active=True))
    provider = _ScriptedProvider('no')
    _install_provider(runtime, provider)
    _, first = await _seed('XAUUSD BUY 2381=78', message_id=990)
    await _drive(runtime, provider, first)
    assert len(provider.calls) == 1 and not broker.positions

    provider = _ScriptedProvider('yes')
    _install_provider(runtime, provider)
    _, second = await _seed('XAUUSD BUY 2381=78', message_id=991)
    result = await _drive(runtime, provider, second)
    assert len(provider.calls) == 1
    assert result['decision'] == 'acted', result
    assert sorted(position['volume'] for position in broker.positions.values()) == [0.2, 0.2]


async def test_watchdog_can_add_only_its_assigned_stop_even_when_disarmed(data_dir):
    from datetime import datetime, timezone
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from app.db import session_scope
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build

    executor, broker, runtime = await _build(data_dir)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test', username='test', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        row = Message(source_id=source.id, message_id=1, kind='text', status='pending',
                      text='XAUUSD BUY 2381=78', posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=row.id,
                         signal_ref=row.signal_ref, origin='agent')
    opened = await executor.open_market(ctx, symbol='XAUUSDm', side='buy', reason='test')
    assert opened['ok'], opened
    first, second = sorted(broker.positions)
    await runtime.set_armed(False)
    watch = RunContext(run_id='watch123', source_id=ctx.source_id, message_row_id=None,
                       signal_ref=None, origin='sl_watchdog', bypass_arm=True, watchdog_ticket=first)
    bad = await executor.dispatch('modify_position', {'ticket': second, 'sl': 2367, 'reason': 'test'}, watch)
    assert not bad['ok'] and bad['stage'] == 'watchdog_scope'
    bad = await executor.dispatch('modify_position', {'ticket': first, 'sl': 2367, 'tp': 2393, 'reason': 'test'}, watch)
    assert not bad['ok'] and bad['stage'] == 'watchdog_scope'
    bad = await executor.dispatch('open_market', {'symbol': 'XAUUSDm', 'side': 'buy', 'reason': 'test'}, watch)
    assert not bad['ok'] and bad['stage'] == 'watchdog_scope'
    good = await executor.dispatch('modify_position', {'ticket': first, 'sl': 2367, 'reason': 'test'}, watch)
    assert good['ok'], good
    assert broker.positions[first]['sl'] == 2367 and broker.positions[second]['sl'] == 0
    again = await executor.dispatch('modify_position', {'ticket': first, 'sl': 2368, 'reason': 'test'}, watch)
    assert not again['ok'] and again['stage'] == 'watchdog_scope'


async def test_channel_entry_refuses_wrong_side_and_stale_revision(data_dir):
    from datetime import datetime, timedelta, timezone
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from app.db import session_scope
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, _ = await _build(data_dir)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test', username='test', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        first = Message(source_id=source.id, message_id=21425, revision=1, edit_date=0, kind='text', status='pending', text='XAUUSD BUY 2381=78', posted_at=now)
        session.add(first); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=first.id, signal_ref=first.signal_ref, origin='agent')
    mismatch = await executor.open_market(ctx, symbol='XAUUSDm', side='sell', reason='test')
    assert not mismatch['ok'] and mismatch['stage'] == 'channel_profile'
    async with session_scope() as session:
        session.add(Message(source_id=ctx.source_id, message_id=21425, revision=2, edit_date=1, kind='text', status='pending', text='XAUUSD SELL 2381=78', posted_at=now + timedelta(seconds=1)))
    stale = await executor.open_market(ctx, symbol='XAUUSDm', side='buy', reason='test')
    assert not stale['ok'] and 'edited' in stale['message']
    assert not broker.positions


async def test_corrected_direction_disarms_before_queuing(data_dir):
    from datetime import datetime, timezone
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from app.db import session_scope
    from app.models import Source
    from tests.test_ingest import _build, text_message
    ingestor, runtime, source_id = await _build(data_dir)
    async with session_scope() as session:
        source = await session.get(Source, source_id)
        source.chat_id = CHANNEL_CHAT_ID
    await runtime.set_armed(True)
    first = await ingestor.handle_message(text_message(21425, 'XAUUSD BUY 4138=41', chat_id=CHANNEL_CHAT_ID))
    assert first is not None and await runtime.armed()
    corrected = await ingestor.handle_message(text_message(21425, 'XAUUSD SELL 4138=41', chat_id=CHANNEL_CHAT_ID, edit_date=datetime.now(timezone.utc)), edited=True)
    assert corrected is not None
    assert not await runtime.armed()


async def test_edit_between_legs_prevents_second_order(data_dir):
    from datetime import datetime, timezone
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from app.db import session_scope
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, _ = await _build(data_dir)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test', username='test', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        row = Message(source_id=source.id, message_id=21425, revision=1, edit_date=0, kind='text', status='pending', text='XAUUSD BUY 2381=78', posted_at=now)
        session.add(row); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=row.id, signal_ref=row.signal_ref, origin='agent')
    check = executor.rpc.order_check
    seen = 0
    async def edit_during_second_check(request):
        nonlocal seen
        seen += 1
        if seen == 2:
            async with session_scope() as session:
                session.add(Message(source_id=ctx.source_id, message_id=21425, revision=2, edit_date=1, kind='text', status='pending', text='XAUUSD SELL 2381=78', posted_at=now))
        return await check(request)
    executor.rpc.order_check = edit_during_second_check
    result = await executor.open_market(ctx, symbol='XAUUSDm', side='buy', reason='test')
    assert seen == 2
    assert len(broker.positions) == 1
    assert result['failures'] and 'edited' in result['failures'][0]['message']


async def test_unmatched_followup_disarms_trading_and_opens_nothing(data_dir):
    from app.ai.xauuad_template import CHANNEL_CHAT_ID
    from app.db import session_scope
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=CHANNEL_CHAT_ID, title='Test', username='test', entity_type='channel', is_active=True))
    provider = _ScriptedProvider('yes')
    _, row_id = await _seed('XAUUSD SELL 2381=78 TP 2375 TP 2372 TP 2369 TP 2366 SL 2393', message_id=123)
    result = await _drive(runtime, provider, row_id)
    assert result['status'] == 'error'
    assert not await runtime.armed()
    assert not broker.positions
