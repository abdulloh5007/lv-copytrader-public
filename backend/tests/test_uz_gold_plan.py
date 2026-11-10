from app.ai.uz_gold_plan import plan_gold_entry


def plan(text, **kw):
    args = dict(bid=4161.0, ask=4161.1, tick_ts=1000, now_ts=1001, signal_ts=995,
                total_lots=0.4, volume_min=0.01, volume_max=200, volume_step=0.01,
                margin_mode='hedging', stop_distance=15)
    args.update(kw)
    return plan_gold_entry(text, **args)


def test_buy_has_two_targets_equal_legs_and_fixed_fifteen_dollar_stop():
    result = plan('60-65 buy zone TP 85 TP 95')
    assert result.action == 'open' and result.zone == (4160, 4165)
    assert result.entry == 4161.1 and abs(result.stop - 4146.1) < 1e-8
    assert result.lots == (0.2, 0.2) and result.targets == (4185, 4195)


def test_sell_stop_is_above_bid():
    result = plan('60-65 sell zone TP 45 TP 25')
    assert result.action == 'open' and result.side == 'sell'
    assert result.entry == 4161 and result.stop == 4176
    assert result.targets == (4145, 4125)


def test_quote_and_signal_must_be_current_and_inside_zone():
    text = '60-65 sell zone TP 45 TP 25'
    assert plan(text, tick_ts=990).reason == 'stale_quote'
    assert plan(text, signal_ts=800).reason == 'edited_or_stale_signal'
    assert plan(text, edited=True).reason == 'edited_or_stale_signal'
    assert plan(text, catchup=True).reason == 'edited_or_stale_signal'
    assert plan(text, bid=4158, ask=4158.1).reason == 'quote_outside_entry_zone'



def test_signal_wait_window_is_explicit_bounded_and_never_replays_edits():
    text = '60-65 sell zone TP 45 TP 25'
    assert plan(text, signal_ts=800).reason == 'edited_or_stale_signal'
    assert plan(text, signal_ts=800, max_signal_age=300).action == 'open'
    assert plan(text, signal_ts=700, max_signal_age=300).reason == 'edited_or_stale_signal'
    assert plan(text, signal_ts=800, max_signal_age=901).reason == 'invalid_signal_age_limit'
    assert plan(text, signal_ts=800, max_signal_age=300, edited=True).reason == 'edited_or_stale_signal'
    assert plan(text, signal_ts=800, max_signal_age=300, catchup=True).reason == 'edited_or_stale_signal'


def test_wait_is_rejected_when_first_target_is_already_passed():
    assert plan('60-65 sell zone TP 45 TP 25', bid=4144, ask=4144.1).reason == 'target_reached_before_entry'
    assert plan('60-65 buy zone TP 85 TP 95', bid=4185, ask=4185.1).reason == 'target_reached_before_entry'



def test_zone_tolerance_is_bounded_and_uses_executable_side_of_quote():
    sell = '4109-113 sell zone TP 4088 TP 4068'
    assert plan(sell, bid=4107, ask=4107.1, zone_tolerance=2).action == 'open'
    assert plan(sell, bid=4106.99, ask=4107.09, zone_tolerance=2).reason == 'quote_outside_entry_zone'
    buy = '60-65 buy zone TP 85 TP 95'
    assert plan(buy, bid=4157.9, ask=4158, zone_tolerance=2).action == 'open'
    assert plan(buy, bid=4157.8, ask=4157.9, zone_tolerance=2).reason == 'quote_outside_entry_zone'
    assert plan(buy, zone_tolerance=2.01).reason == 'invalid_zone_tolerance'
    assert plan('60-65 buy zone TP 66 TP 95', bid=4165.9, ask=4166, zone_tolerance=2).reason == 'target_reached_before_entry'


def test_invalid_direction_targets_or_hedging_never_produce_a_plan():
    assert plan('60-65 buy zone TP 45 TP 25').action == 'reject'
    assert plan('60-65 buy zone TP 85').reason == 'requires_zero_or_two_targets'
    assert plan('60-65 sell zone TP 45 TP 25', margin_mode='netting').reason == 'two_positions_require_hedging'
    assert plan('60-65 sell zone TP 45 TP 25', total_lots=0.03).reason == 'invalid_two_leg_volume'
    assert plan('60-65 sell zone TP 45 TP 25', bid=float('nan')).reason == 'invalid_numeric_input'


def test_management_never_produces_an_entry():
    assert plan('B/u ga obqoying').action == 'ignore'
    assert plan('Yoping buylarni').action == 'ignore'
    assert plan('60-65 buy reentry TP 85 TP 95').action == 'ignore'


def test_channel_stop_is_recorded_but_fixed_distance_is_used():
    result = plan('60-65 buy zone SL 30pips TP 85 TP 95')
    assert result.action == 'open' and result.channel_stop_supplied
    assert abs(result.stop - (result.entry - 15)) < 1e-8


def test_initial_entry_without_targets_has_protective_stop_and_no_invented_tp():
    result = plan('60-65 buy zone minimal risk')
    assert result.action == 'open' and result.targets == ()
    assert result.stop is not None and abs(result.stop - (result.entry - 15)) < 1e-8
    assert result.lots == (0.2, 0.2)


async def test_fake_broker_opens_two_protected_legs_only_with_enabled_policy(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Example signals', username=None, entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        row = Message(source_id=source.id, message_id=201, kind='text', status='pending', text='81-78 buy zone minimal risk', posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=row.id, signal_ref=row.signal_ref, origin='agent')
    disabled = await executor.open_uz_gold_signal(ctx)
    assert not disabled['ok'] and not broker.positions
    async with session_scope() as session:
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.4, 'stop_distance': 15.0})
    result = await executor.open_uz_gold_signal(ctx)
    assert result['ok'], result
    assert len(broker.positions) == 2
    for position in broker.positions.values():
        assert position['volume'] == 0.2 and position['tp'] == 0
        assert abs(position['price_open'] - position['sl'] - 15) < 0.5
    again = await executor.open_uz_gold_signal(ctx)
    assert not again['ok'] and len(broker.positions) == 2


async def test_fake_broker_sets_both_targets_when_supplied_in_original(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Example signals', username=None, entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.4, 'stop_distance': 15.0})
        row = Message(source_id=source.id, message_id=202, kind='text', status='pending', text='81-78 buy zone TP 84 TP 87', posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=row.id, signal_ref=row.signal_ref, origin='agent')
    result = await executor.open_uz_gold_signal(ctx)
    assert result['ok'], result
    assert sorted(p['tp'] for p in broker.positions.values()) == [2384, 2387]
    assert all(abs(p['price_open'] - p['sl'] - 15) < 0.5 for p in broker.positions.values())


async def test_fake_broker_attaches_targets_then_break_even_without_extra_orders(data_dir):
    from datetime import datetime, timedelta, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Example signals', username=None, entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.4, 'stop_distance': 15.0})
        initial = Message(source_id=source.id, message_id=301, edit_date=0, revision=1, kind='text', status='processed', text='81-78 buy zone', posted_at=now)
        session.add(initial); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=initial.id, signal_ref=initial.signal_ref, origin='agent')
    opened = await executor.open_uz_gold_signal(ctx)
    assert opened['ok'], opened
    class Recorder:
        def __init__(self): self.sent = []
        def render_modified(self, **kw): return 'combined update'
        def fire(self, kind, text, **kw): self.sent.append((kind, text))
        def __getattr__(self, name):
            if name.startswith('render_'): return lambda **kw: ''
            raise AttributeError(name)
    recorder = Recorder()
    runtime.notifier = recorder
    async with session_scope() as session:
        edit = Message(source_id=ctx.source_id, message_id=301, edit_date=1, revision=2, kind='text', status='pending', text='81-78 buy zone TP 84 TP 87', posted_at=now)
        session.add(edit); await session.flush()
        update = RunContext(run_id='test1235', source_id=ctx.source_id, message_row_id=edit.id, signal_ref=edit.signal_ref, origin='agent')
    result = await executor.update_uz_gold_signal(update)
    assert result['ok'], result
    assert recorder.sent == [('modified', 'combined update')]
    assert sorted(p['tp'] for p in broker.positions.values()) == [2384, 2387]
    assert len(broker.positions) == 2
    duplicate = await executor.update_uz_gold_signal(update)
    assert duplicate['ok'] and len(duplicate['unchanged']) == 2 and len(broker.positions) == 2
    broker.move('XAUUSDm', 2383)
    await runtime.set_armed(False)
    async with session_scope() as session:
        be = Message(source_id=ctx.source_id, message_id=302, reply_to_message_id=301, kind='text', status='pending', text='B/u ga obqoying', posted_at=now+timedelta(seconds=20))
        session.add(be); await session.flush()
        be_ctx = RunContext(run_id='test1236', source_id=ctx.source_id, message_row_id=be.id, signal_ref=be.signal_ref, origin='agent')
    result = await executor.update_uz_gold_signal(be_ctx)
    assert result['ok'] and result['action'] == 'break_even', result
    assert recorder.sent == [('modified', 'combined update'), ('modified', 'combined update')]
    assert all(p['sl'] >= p['price_open'] for p in broker.positions.values())
    assert sorted(p['tp'] for p in broker.positions.values()) == [2384, 2387]


async def test_fake_broker_refuses_direction_flip_and_unthreaded_break_even(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Example signals', username=None, entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.4, 'stop_distance': 15.0})
        first = Message(source_id=source.id, message_id=401, edit_date=0, revision=1, kind='text', status='processed', text='81-78 buy zone', posted_at=now)
        session.add(first); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=first.id, signal_ref=first.signal_ref, origin='agent')
    opened = await executor.open_uz_gold_signal(ctx)
    assert opened['ok'], opened
    before = {k:(v['sl'],v['tp']) for k,v in broker.positions.items()}
    async with session_scope() as session:
        edit = Message(source_id=ctx.source_id, message_id=401, edit_date=1, revision=2, kind='text', status='pending', text='81-78 sell zone TP 75 TP 72', posted_at=now)
        session.add(edit); await session.flush()
        update = RunContext(run_id='test1235', source_id=ctx.source_id, message_row_id=edit.id, signal_ref=edit.signal_ref, origin='agent')
    bad = await executor.update_uz_gold_signal(update)
    assert not bad['ok'] and 'conflict' in bad['message']
    async with session_scope() as session:
        be = Message(source_id=ctx.source_id, message_id=402, kind='text', status='pending', text='B/u ga obqoying', posted_at=now)
        session.add(be); await session.flush()
        be_ctx = RunContext(run_id='test1236', source_id=ctx.source_id, message_row_id=be.id, signal_ref=be.signal_ref, origin='agent')
    bad = await executor.update_uz_gold_signal(be_ctx)
    assert not bad['ok'] and 'unthreaded' in bad['message']
    assert {k:(v['sl'],v['tp']) for k,v in broker.positions.items()} == before


async def test_end_to_end_three_checks_open_then_edit_targets_and_reply_break_even(data_dir):
    from datetime import datetime, timedelta, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Example signals', username=None, entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
    provider = _ScriptedProvider('yes')
    _install_provider(runtime, provider)
    source_id, row_id = await _seed('81-78 buy zone', message_id=501, posted_at=now)
    opened = await _drive(runtime, provider, row_id)
    assert opened['decision'] == 'acted', opened
    assert len(provider.calls) == 3 and len(broker.positions) == 2
    assert sorted(p['volume'] for p in broker.positions.values()) == [0.1, 0.1]
    assert all(abs(p['price_open'] - p['sl'] - 15) < 0.5 for p in broker.positions.values())
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=501, edit_date=1, revision=2, kind='text', status='pending', text='81-78 buy zone TP 84 TP 87', posted_at=now)
        session.add(edit); await session.flush(); edit_id = edit.id
    updated = await _drive(runtime, provider, edit_id)
    assert updated['decision'] == 'acted', updated
    assert len(provider.calls) == 3 and len(broker.positions) == 2
    assert sorted(p['tp'] for p in broker.positions.values()) == [2384, 2387]
    broker.move('XAUUSDm', 2383)
    async with session_scope() as session:
        reply = Message(source_id=source_id, message_id=502, reply_to_message_id=501, kind='text', status='pending', text='B/u ga obqoying', posted_at=now+timedelta(seconds=20))
        session.add(reply); await session.flush(); reply_id = reply.id
    be = await _drive(runtime, provider, reply_id)
    assert be['decision'] == 'acted', be
    assert len(provider.calls) == 3 and len(broker.positions) == 2
    assert all(p['sl'] >= p['price_open'] for p in broker.positions.values())


def test_confirmed_volume_is_two_point_one_lot_legs():
    result = plan('60-65 buy zone TP 85 TP 95', total_lots=0.2)
    assert result.action == 'open' and result.lots == (0.1, 0.1)


async def test_confirmed_volume_and_be_only_after_verified_tp_close(data_dir):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.db import session_scope, set_setting
    from app.models import Message, Source, Trade
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Example signals', username=None, entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.be_after_tp1', True)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
        row = Message(source_id=source.id, message_id=601, kind='text', status='pending', text='81-78 buy zone TP 84 TP 87', posted_at=now)
        session.add(row); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=row.id, signal_ref=row.signal_ref, origin='agent')
    result = await executor.open_uz_gold_signal(ctx)
    assert result['ok'], result
    assert sorted(p['volume'] for p in broker.positions.values()) == [0.1, 0.1]
    cover = next(t for t,p in broker.positions.items() if p['tp'] == 2384)
    runner = next(t for t,p in broker.positions.items() if p['tp'] == 2387)
    initial_sl = broker.positions[runner]['sl']
    broker.move('XAUUSDm', 2386)
    del broker.positions[cover]
    async with session_scope() as session:
        trade = (await session.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        trade.status = 'closed'; trade.close_cause = 'sl'
    await executor.trail_after_tp1()
    assert broker.positions[runner]['sl'] == initial_sl
    async with session_scope() as session:
        trade = (await session.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        trade.close_cause = 'tp'
    moved = await executor.trail_after_tp1()
    assert runner in moved['trailed']
    assert broker.positions[runner]['sl'] >= broker.positions[runner]['price_open']


async def test_disagreeing_three_ai_checks_never_open_a_position(data_dir):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    class VotingProvider(_ScriptedProvider):
        def __init__(self):
            super().__init__(''); self.votes = ['yes', 'no', 'uncertain']
        def run(self, **kwargs):
            each = _ScriptedProvider(self.votes[len(self.calls)])
            stream = each.run(**kwargs); self.calls.extend(each.calls); return stream
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Example signals', username=None, entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
    provider=VotingProvider(); _install_provider(runtime, provider)
    _, row_id = await _seed('81-78 buy zone', message_id=701)
    result = await _drive(runtime, provider, row_id)
    assert result['status'] == 'error' and len(provider.calls) == 3
    assert not broker.positions


async def test_missing_broker_stop_disarms_and_blocks_second_leg(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        source=Source(kind='telegram',chat_id=chat_id,title='Example signals',username=None,entity_type='channel',is_active=True)
        session.add(source); await session.flush()
        await set_setting(session,'ingest.research_only_chat_id',chat_id)
        await set_setting(session,'trading.uz_gold_policy',{'enabled':True,'total_lots':0.2,'stop_distance':15.0})
        row=Message(source_id=source.id,message_id=702,kind='text',status='pending',text='81-78 buy zone',posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row);await session.flush()
        ctx=RunContext(run_id='test1234',source_id=source.id,message_row_id=row.id,signal_ref=row.signal_ref,origin='agent')
    send=executor.rpc.order_send
    async def broker_strips_stop(request):
        result=await send(request)
        if result is not None and int(getattr(result,'order',0) or 0) in broker.positions:
            broker.positions[int(result.order)]['sl']=0
        return result
    executor.rpc.order_send=broker_strips_stop
    result=await executor.open_uz_gold_signal(ctx)
    assert len(broker.positions) == 1 and result['safety_failures']
    assert not await runtime.armed()


async def test_unknown_first_order_outcome_disarms_without_sending_second(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        source=Source(kind='telegram',chat_id=chat_id,title='Example signals',username=None,entity_type='channel',is_active=True)
        session.add(source); await session.flush()
        await set_setting(session,'ingest.research_only_chat_id',chat_id)
        await set_setting(session,'trading.uz_gold_policy',{'enabled':True,'total_lots':0.2,'stop_distance':15.0})
        row=Message(source_id=source.id,message_id=703,kind='text',status='pending',text='81-78 buy zone',posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row);await session.flush()
        ctx=RunContext(run_id='test1234',source_id=source.id,message_row_id=row.id,signal_ref=row.signal_ref,origin='agent')
    calls=[]
    async def missing_response(request):
        calls.append(request)
        return None
    executor.rpc.order_send=missing_response
    result=await executor.open_uz_gold_signal(ctx)
    assert len(calls)==1 and not result['ok'] and result['safety_failures']
    assert not await runtime.armed()


async def test_identical_latest_edit_does_not_lose_target_update(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source=Source(kind='telegram',chat_id=chat_id,title='Example signals',username=None,entity_type='channel',is_active=True)
        session.add(source); await session.flush()
        await set_setting(session,'ingest.research_only_chat_id',chat_id)
        await set_setting(session,'trading.uz_gold_policy',{'enabled':True,'total_lots':0.2,'stop_distance':15.0})
        row=Message(source_id=source.id,message_id=704,edit_date=0,revision=1,kind='text',status='processed',text='81-78 buy zone',posted_at=now)
        session.add(row);await session.flush()
        ctx=RunContext(run_id='test1234',source_id=source.id,message_row_id=row.id,signal_ref=row.signal_ref,origin='agent')
    result=await executor.open_uz_gold_signal(ctx)
    assert result['ok'], result
    async with session_scope() as session:
        one=Message(source_id=ctx.source_id,message_id=704,edit_date=1,revision=2,kind='text',status='pending',text='81-78 buy zone TP 84 TP 87',posted_at=now)
        two=Message(source_id=ctx.source_id,message_id=704,edit_date=2,revision=3,kind='text',status='skipped',text='81-78 buy zone TP 84 TP 87',posted_at=now)
        session.add_all([one,two]);await session.flush()
        update=RunContext(run_id='test1235',source_id=ctx.source_id,message_row_id=one.id,signal_ref=one.signal_ref,origin='agent')
    result=await executor.update_uz_gold_signal(update)
    assert result['ok'], result
    assert sorted(p['tp'] for p in broker.positions.values())==[2384,2387]


async def test_second_leg_failure_disarms_and_preserves_first_protected_position(data_dir):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram',chat_id=chat_id,title='Example signals',username=None,entity_type='channel',is_active=True))
        await set_setting(session,'ingest.research_only_chat_id',chat_id)
        await set_setting(session,'trading.uz_gold_policy',{'enabled':True,'total_lots':0.2,'stop_distance':15.0})
    provider=_ScriptedProvider('yes'); _install_provider(runtime,provider)
    _, row_id=await _seed('81-78 buy zone',message_id=705)
    send=executor.rpc.order_send; calls=0
    async def fail_second(request):
        nonlocal calls
        calls+=1
        return None if calls==2 else await send(request)
    executor.rpc.order_send=fail_second
    result=await _drive(runtime,provider,row_id)
    assert result['status']=='error' and calls==2
    assert len(broker.positions)==1 and next(iter(broker.positions.values()))['sl']>0
    assert not await runtime.armed()


async def test_trade_record_failure_disarms_and_never_hides_open_positions(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id=-1005555555555
    async with session_scope() as session:
        source=Source(kind='telegram',chat_id=chat_id,title='Example signals',username=None,entity_type='channel',is_active=True)
        session.add(source);await session.flush()
        await set_setting(session,'ingest.research_only_chat_id',chat_id)
        await set_setting(session,'trading.uz_gold_policy',{'enabled':True,'total_lots':0.2,'stop_distance':15.0})
        row=Message(source_id=source.id,message_id=706,kind='text',status='pending',text='81-78 buy zone',posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row);await session.flush()
        ctx=RunContext(run_id='test1234',source_id=source.id,message_row_id=row.id,signal_ref=row.signal_ref,origin='agent')
    async def storage_failure(*args, **kwargs): return None
    executor._persist_open=storage_failure
    result=await executor.open_uz_gold_signal(ctx)
    assert result['ok'] and result['safety_failures'] and len(broker.positions)==2
    assert all(p['sl']>0 for p in broker.positions.values())
    assert not await runtime.armed()


async def test_unconfigured_close_request_pauses_without_closing_positions(data_dir):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id=-1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram',chat_id=chat_id,title='Example signals',username=None,entity_type='channel',is_active=True))
        await set_setting(session,'ingest.research_only_chat_id',chat_id)
        await set_setting(session,'trading.uz_gold_policy',{'enabled':True,'total_lots':0.2,'stop_distance':15.0})
    provider=_ScriptedProvider('yes');_install_provider(runtime,provider)
    _, row_id=await _seed('Yoping buylarni',message_id=707)
    result=await _drive(runtime,provider,row_id)
    assert result['status']=='error' and not await runtime.armed()
    assert not broker.positions and not broker.orders


async def test_disarmed_live_policy_cannot_open_even_if_ai_votes_yes(data_dir):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=False)
    chat_id=-1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram',chat_id=chat_id,title='Example signals',username=None,entity_type='channel',is_active=True))
        await set_setting(session,'ingest.research_only_chat_id',chat_id)
        await set_setting(session,'trading.uz_gold_policy',{'enabled':True,'total_lots':0.2,'stop_distance':15.0})
    provider=_ScriptedProvider('yes');_install_provider(runtime,provider)
    _, row_id=await _seed('81-78 buy zone',message_id=708)
    result=await _drive(runtime,provider,row_id)
    assert result['decision']=='ignored'
    assert not broker.positions and not broker.orders and not await runtime.armed()


async def test_source_change_before_entry_fails_closed(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime=await _build(data_dir,armed=True)
    chat_id=-1005555555555
    async with session_scope() as session:
        src=Source(kind='telegram',chat_id=chat_id,title='Example signals',username=None,entity_type='channel',is_active=False)
        session.add(src);await session.flush()
        await set_setting(session,'ingest.research_only_chat_id',chat_id)
        await set_setting(session,'trading.uz_gold_policy',{'enabled':True,'total_lots':0.2,'stop_distance':15.0})
        row=Message(source_id=src.id,message_id=709,kind='text',status='pending',text='81-78 buy zone',posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row);await session.flush()
        ctx=RunContext(run_id='test1234',source_id=src.id,message_row_id=row.id,signal_ref=row.signal_ref,origin='agent')
    result=await executor.open_uz_gold_signal(ctx)
    assert not result['ok'] and not broker.positions


async def test_wait_retries_only_outside_zone_and_stops_after_success():
    from app.ai.uz_gold_plan import wait_for_gold_entry
    clock = [0.0]
    calls = []
    async def sleep(seconds): clock[0] += seconds
    async def attempt():
        calls.append(clock[0])
        if clock[0] < 2: return {'ok': False, 'stage': 'channel_profile', 'message': 'gold channel entry refused: quote_outside_entry_zone'}
        return {'ok': True}
    assert await wait_for_gold_entry(attempt, wait_seconds=5, sleep=sleep, monotonic=lambda: clock[0]) == {'ok': True}
    assert calls == [0, 1, 2]


async def test_wait_does_not_retry_uncertain_broker_error_or_expired_timeout():
    from app.ai.uz_gold_plan import wait_for_gold_entry
    clock = [0.0]
    calls = []
    async def sleep(seconds): clock[0] += seconds
    async def attempt():
        calls.append(clock[0])
        return {'ok': False, 'stage': 'channel_profile', 'message': 'gold channel entry refused: quote_outside_entry_zone'}
    await wait_for_gold_entry(attempt, wait_seconds=2, sleep=sleep, monotonic=lambda: clock[0])
    assert calls == [0, 1]
    async def uncertain():
        calls.append('broker')
        return {'ok': False, 'message': 'broker result unknown'}
    await wait_for_gold_entry(uncertain, wait_seconds=5, sleep=sleep, monotonic=lambda: clock[0])
    assert calls.count('broker') == 1


async def test_waiting_signal_opens_on_return_to_zone_with_fake_broker(data_dir):
    import asyncio
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Example', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0, 'entry_wait_seconds': 300, 'zone_tolerance': 2})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    _, row_id = await _seed('84-86 buy zone TP 90 TP 95', message_id=2501, posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
    task = asyncio.create_task(_drive(runtime, provider, row_id))
    try:
        for _ in range(100):
            if getattr(runtime, 'uz_gold_waiting', False): break
            await asyncio.sleep(.02)
        assert getattr(runtime, 'uz_gold_waiting', False)
        assert not broker.positions
        await asyncio.sleep(.2)
        broker.move('XAUUSDm', 2383)
        result = await asyncio.wait_for(task, timeout=8)
        assert result['decision'] == 'acted', result
        assert sorted(p['volume'] for p in broker.positions.values()) == [0.1, 0.1]
        assert sorted(p['tp'] for p in broker.positions.values()) == [2390, 2395]
        assert all(abs(p['price_open'] - p['sl'] - 15) < 0.5 for p in broker.positions.values())
    finally:
        if not task.done():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass


async def test_waiting_signal_is_cancelled_by_a_new_channel_message(data_dir):
    import asyncio
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555; now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Example', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0, 'entry_wait_seconds': 300, 'zone_tolerance': 2})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    source_id, row_id = await _seed('84-86 buy zone TP 90 TP 95', message_id=2502, posted_at=now)
    task = asyncio.create_task(_drive(runtime, provider, row_id))
    try:
        for _ in range(100):
            if getattr(runtime, 'uz_gold_waiting', False): break
            await asyncio.sleep(.02)
        assert getattr(runtime, 'uz_gold_waiting', False)
        async with session_scope() as session:
            session.add(Message(source_id=source_id, message_id=2503, kind='text', status='pending', text='Yoping', posted_at=now))
        broker.move('XAUUSDm', 2383)
        result = await asyncio.wait_for(task, timeout=8)
        assert result['decision'] == 'ignored', result
        assert not broker.positions
        assert not getattr(runtime, 'uz_gold_waiting', False)
    finally:
        if not task.done():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass


async def test_waiting_entry_rechecks_edit_and_disarmed_state(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555; now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Example', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0, 'entry_wait_seconds': 300, 'zone_tolerance': 2})
        row = Message(source_id=source.id, message_id=2601, kind='text', status='pending', text='81-78 buy zone', posted_at=now)
        session.add(row); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=row.id, signal_ref=row.signal_ref, origin='agent', waiting_for_zone=True)
    await runtime.set_armed(False)
    assert not (await executor.open_uz_gold_signal(ctx))['ok']
    assert not broker.positions
    await runtime.set_armed(True)
    async with session_scope() as session:
        session.add(Message(source_id=ctx.source_id, message_id=2601, edit_date=1, revision=2, kind='text', status='pending', text='81-78 sell zone', posted_at=now))
    assert not (await executor.open_uz_gold_signal(ctx))['ok']
    assert not broker.positions
