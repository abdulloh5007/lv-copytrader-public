from app.ai.uz_gold_plan import plan_gold_entry
import pytest


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


def test_invalid_direction_or_hedging_never_produce_a_plan():
    assert plan('60-65 buy sell zone TP 85 TP 95').action == 'reject'
    assert plan('60-65 sell zone TP 45 TP 25', margin_mode='netting').reason == 'two_positions_require_hedging'
    assert plan('60-65 sell zone TP 45 TP 25', total_lots=0.03).reason == 'invalid_two_leg_volume'
    assert plan('60-65 sell zone TP 45 TP 25', bid=float('nan')).reason == 'invalid_numeric_input'


def test_unusable_levels_fall_back_independently_and_keep_verified_protection():
    partial = plan('60-65 sell zone TP 3')
    assert partial.action == 'open' and partial.targets == ()
    assert partial.stop == 4176 and partial.ignored_levels == ('invalid_targets',)
    invalid_targets = plan('60-65 buy zone SL 4150 TP 45 TP 25')
    assert invalid_targets.action == 'open' and invalid_targets.targets == ()
    assert invalid_targets.stop == 4150 and invalid_targets.channel_stop_supplied
    invalid_stop = plan('60-65 sell zone SL 4100 TP 45 TP 25')
    assert invalid_stop.action == 'open' and invalid_stop.stop == 4176
    assert invalid_stop.targets == (4145, 4125) and not invalid_stop.channel_stop_supplied
    assert invalid_stop.ignored_levels == ('invalid_stop',)


def test_first_two_channel_targets_and_single_target_are_retained():
    assert plan('60-65 sell zone TP 45 TP 25 TP 15').targets == (4145, 4125)
    assert plan('60-65 sell zone TP 45').targets == (4145,)


def test_known_first_target_still_blocks_entry_when_later_target_is_invalid():
    assert plan('60-65 sell zone TP 45 TP 9000', bid=4144, ask=4144.1).reason == 'target_reached_before_entry'


def test_management_never_produces_an_entry():
    assert plan('B/u ga obqoying').action == 'ignore'
    assert plan('Yoping buylarni').action == 'ignore'
    assert plan('60-65 reentry TP 85 TP 95').action == 'ignore'
    assert plan('60-65 buy reentry TP 85 TP 95').action == 'open'


def test_channel_pip_stop_uses_ten_cents_per_gold_pip():
    result = plan('60-65 buy zone SL 30pips TP 85 TP 95')
    assert result.action == 'open' and abs(result.stop - (result.entry - 3)) < 1e-8
    assert result.channel_stop_pips == 30


def test_explicit_channel_price_takes_priority_over_fallback():
    buy = plan('60-65 buy zone SL 4150 TP 85 TP 95')
    sell = plan('60-65 sell zone SL 4170 TP 45 TP 25')
    assert buy.action == 'open' and buy.stop == 4150 and buy.channel_stop_supplied
    assert sell.action == 'open' and sell.stop == 4170 and sell.channel_stop_supplied


@pytest.mark.parametrize('side,text,stop', [
    ('buy', '81-78 buy zone SL 2370', 2370),
    ('sell', '78-81 sell zone SL 2390', 2390),
])
async def test_channel_stop_price_is_sent_on_both_protected_orders(data_dir, side, text, stop):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    _, row_id = await _seed(text, message_id=501)
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'acted', result
    assert len(broker.positions) == 2 and all(p['sl'] == stop for p in broker.positions.values())
    first = next(p for p in broker.positions.values() if p['tp'] > 0)
    assert abs(abs(first['tp'] - first['price_open']) - abs(first['price_open'] - stop)) < .01


@pytest.mark.parametrize('side,text', [('buy', '81-78 buy zone SL 40pips'),
                                       ('sell', '78-81 sell zone SL 40pips')])
async def test_initial_pip_stop_and_1r_follow_actual_slipped_fills(data_dir, side, text):
    from sqlalchemy import select
    from app.db import session_scope, set_setting
    from app.models import Source, Trade
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    original_send = broker.m_order_send
    deals = 0
    def slipped_send(request):
        nonlocal deals
        if request.get('action') == 1 and 'position' not in request:
            deals += 1
            broker.move('XAUUSDm', 2380 + deals * .1)
        return original_send(request)
    broker.m_order_send = slipped_send
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    _, row_id = await _seed(text, message_id=505)
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'acted', result
    assert deals == 2 and len(broker.positions) == 2
    for p in broker.positions.values():
        assert abs(abs(p['price_open'] - p['sl']) - 4) < .01
    cover = next(p for p in broker.positions.values() if p['tp'] > 0)
    assert abs(abs(cover['price_open'] - cover['tp']) - 4) < .01
    async with session_scope() as session:
        trades = list((await session.execute(select(Trade))).scalars().all())
    assert all(abs(t.sl - broker.positions[t.ticket]['sl']) < .01 for t in trades)


async def test_unverified_pip_stop_correction_is_reported_and_pauses_new_entries(data_dir):
    from sqlalchemy import select
    from app.db import session_scope, set_setting
    from app.models import Source, Trade
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    original_send = broker.m_order_send
    def false_success(request):
        if request.get('action') == 1 and 'position' not in request:
            broker.move('XAUUSDm', 2380.2)
        if request.get('action') == 6:
            p = broker.positions[int(request['position'])]
            return {'retcode': 10009, 'comment': 'Done', 'order': p['ticket'], 'deal': 0,
                    'volume': p['volume'], 'price': p['price_open'], 'request': dict(request)}
        return original_send(request)
    broker.m_order_send = false_success
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    _, row_id = await _seed('78-81 sell zone SL 40pips', message_id=509)
    result = await _drive(runtime, provider, row_id)
    assert result['status'] == 'error' and not await runtime.armed(), result
    assert broker.positions and all(p['sl'] > 0 for p in broker.positions.values())
    async with session_scope() as session:
        trades = list((await session.execute(select(Trade))).scalars().all())
    assert all(t.sl == broker.positions[t.ticket]['sl'] for t in trades)


@pytest.mark.parametrize('protected', [False, True])
async def test_channel_stop_price_edit_updates_exact_positions_without_loosening_breakeven(data_dir, protected):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    source_id, row_id = await _seed('78-81 sell zone', message_id=502, posted_at=now)
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'acted', result
    if protected:
        for p in broker.positions.values(): p['sl'] = p['price_open']
    before = {k: dict(v) for k, v in broker.positions.items()}
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=502, revision=2, edit_date=1,
                       kind='text', status='pending', text='78-81 sell zone SL 2390', posted_at=now)
        session.add(edit); await session.flush(); edit_id = edit.id
    result = await _drive(runtime, provider, edit_id)
    assert result['status'] == 'finished', result
    assert set(broker.positions) == set(before)
    for ticket, pos in broker.positions.items():
        assert pos['sl'] == (before[ticket]['sl'] if protected else 2390)


@pytest.mark.parametrize('conflict', [False, True])
async def test_edited_stop_and_targets_apply_together_only_to_original_signal(data_dir, conflict):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    source_id, row_id = await _seed('78-81 sell zone', message_id=503, posted_at=now)
    assert (await _drive(runtime, provider, row_id))['decision'] == 'acted'
    before = {k: dict(v) for k, v in broker.positions.items()}
    text = ('78-81 buy zone SL 2370 TP 2384 TP 2387' if conflict else
            '78-81 sell zone SL 2390 TP 2375 TP 2372')
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=503, revision=2, edit_date=1,
                       kind='text', status='pending', text=text, posted_at=now)
        session.add(edit); await session.flush(); edit_id = edit.id
    result = await _drive(runtime, provider, edit_id)
    if conflict:
        assert result['status'] == 'error' and broker.positions == before
    else:
        assert result['status'] == 'finished', result
        assert all(p['sl'] == 2390 for p in broker.positions.values())
        assert sorted(p['tp'] for p in broker.positions.values()) == [2372, 2375]


async def test_pip_stop_edit_uses_each_fill_and_recalculates_automatic_1r(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    source_id, row_id = await _seed('78-81 sell zone', message_id=504, posted_at=now)
    assert (await _drive(runtime, provider, row_id))['decision'] == 'acted'
    before = {k: dict(v) for k, v in broker.positions.items()}
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=504, revision=2, edit_date=1,
                       kind='text', status='pending', text='78-81 sell zone SL 40pips', posted_at=now)
        session.add(edit); await session.flush(); edit_id = edit.id
    result = await _drive(runtime, provider, edit_id)
    assert result['status'] == 'finished' and await runtime.armed(), result
    assert set(broker.positions) == set(before)
    for pos in broker.positions.values():
        assert abs(pos['sl'] - (pos['price_open'] + 4)) < .01
    first = next(p for p in broker.positions.values() if p['tp'] > 0)
    assert abs((first['price_open'] - first['tp']) - 4) < .01


async def test_channel_pip_stop_does_not_recalculate_a_previously_posted_target(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    source_id, row_id = await _seed('78-81 sell zone', message_id=506, posted_at=now)
    assert (await _drive(runtime, provider, row_id))['decision'] == 'acted'
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=506, revision=2, edit_date=1,
                       kind='text', status='pending', text='78-81 sell zone TP 75 TP 72', posted_at=now)
        session.add(edit); await session.flush(); edit_id = edit.id
    assert (await _drive(runtime, provider, edit_id))['decision'] == 'acted'
    original_targets = {ticket: p['tp'] for ticket, p in broker.positions.items()}
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=506, revision=3, edit_date=2,
                       kind='text', status='pending', text='78-81 sell zone SL 40pips', posted_at=now)
        session.add(edit); await session.flush(); edit_id = edit.id
    result = await _drive(runtime, provider, edit_id)
    assert result['status'] == 'finished', result
    for ticket, p in broker.positions.items():
        assert p['tp'] == original_targets[ticket]
        assert abs(p['sl'] - (p['price_open'] + 4)) < .01


@pytest.mark.parametrize('reply,valid', [('SL 40pips', True), ('SL 40pips close now', False)])
async def test_only_unambiguous_threaded_stop_reply_changes_matching_positions(data_dir, reply, valid):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    source_id, row_id = await _seed('Gold sell now', message_id=507, posted_at=now)
    assert (await _drive(runtime, provider, row_id))['decision'] == 'acted'
    before = {k: dict(v) for k, v in broker.positions.items()}
    async with session_scope() as session:
        item = Message(source_id=source_id, message_id=508, reply_to_message_id=507,
                       kind='text', status='pending', text=reply, posted_at=now)
        session.add(item); await session.flush(); reply_id = item.id
    result = await _drive(runtime, provider, reply_id)
    if valid:
        assert result['status'] == 'finished', result
        assert all(abs(p['sl'] - (p['price_open'] + 4)) < .01 for p in broker.positions.values())
    else:
        assert result['status'] == 'error' and broker.positions == before


async def test_observed_reentry_edit_with_distant_target_and_forty_pips(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    broker.move('XAUUSDm', 3916)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _, notice = await _seed('Stop urdi reentry beraman', message_id=12078, posted_at=now)
    assert (await _drive(runtime, provider, notice))['decision'] == 'ignored'
    source_id, first = await _seed('3914-19 sell zone minimal risk', message_id=12079, posted_at=now)
    assert (await _drive(runtime, provider, first))['decision'] == 'acted'
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=12079, revision=2, edit_date=1,
                       kind='text', status='pending', posted_at=now,
                       text='3914-19 sell zone minimal risk\nTP 3870\nTP 3750\nSL 40pips')
        session.add(edit); await session.flush(); edit_id = edit.id
    result = await _drive(runtime, provider, edit_id)
    assert result['status'] == 'finished', result
    assert sorted(p['tp'] for p in broker.positions.values()) == [3750, 3870]
    assert all(abs(p['sl'] - (p['price_open'] + 4)) < .01 for p in broker.positions.values())
    assert await runtime.armed()


@pytest.mark.parametrize('text,side', [('Gold sell now', 'sell'), ('Gold buy now', 'buy')])
def test_market_now_uses_fresh_quote_and_confirmed_protection(text, side):
    result = plan(text, total_lots=0.2)
    assert result.action == 'open' and result.side == side and result.zone is None
    assert result.lots == (0.1, 0.1) and not result.targets
    assert result.stop == (result.entry + 15 if side == 'sell' else result.entry - 15)
    assert plan(text, tick_ts=990).reason == 'stale_quote'
    assert plan(text, signal_ts=800).reason == 'edited_or_stale_signal'
    assert plan(text, edited=True).reason == 'edited_or_stale_signal'
    assert plan(text, catchup=True).reason == 'edited_or_stale_signal'


def test_observed_selldaman_signal_keeps_zone_and_risk_guards():
    args = dict(bid=4007.2, ask=4007.28, tick_ts=1000, now_ts=1001, signal_ts=995,
                total_lots=0.2, volume_min=0.01, volume_max=100, volume_step=0.01,
                margin_mode='hedging', stop_distance=15)
    result = plan_gold_entry('07-11 selldaman', **args)
    assert result.action == 'open' and result.side == 'sell' and result.zone == (4007, 4011)
    assert result.lots == (0.1, 0.1) and result.stop == 4022.2
    assert plan_gold_entry('07-11 selldaman', **{**args, 'bid': 4004, 'ask': 4004.1}).reason == 'quote_outside_entry_zone'
    assert plan_gold_entry('07-11 selldaman', **{**args, 'tick_ts': 990}).reason == 'stale_quote'


async def test_selldaman_zone_passes_one_ai_check_before_two_protected_legs(data_dir):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', username=None,
                           entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy',
                          {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    _, row_id = await _seed('78-81 selldaman', message_id=815)
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'acted', result
    assert len(provider.calls) == 1 and len(broker.positions) == 2
    assert all(p['volume'] == 0.1 and p['sl'] > p['price_open'] for p in broker.positions.values())


@pytest.mark.parametrize('side,targets,zone', [
    ('buy', (2384, 2387), '81-78'),
    ('sell', (2375, 2372), '78-81'),
])
@pytest.mark.parametrize('rapid', [False, True])
async def test_market_now_one_check_then_later_targets_only_update_same_positions(data_dir, side, targets, zone, rapid):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', username=None,
                           entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2,
                          'stop_distance': 15.0, 'entry_wait_seconds': 300, 'zone_tolerance': 2.0})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    source_id, row_id = await _seed(f'Gold {side} now', message_id=810, posted_at=now)
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'acted', result
    assert len(provider.calls) == 1 and len(broker.positions) == 2
    tickets = set(broker.positions)
    positions = list(broker.positions.values())
    assert all(p['volume'] == 0.1 and p['sl'] > 0 for p in positions)
    assert sum(p['tp'] == 0 for p in positions) == 1
    cover = next(p for p in positions if p['tp'])
    assert abs(abs(cover['tp'] - cover['price_open']) - abs(cover['price_open'] - cover['sl'])) < .01
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=810, edit_date=1, revision=2,
                       kind='text', status='pending',
                       text=f'Gold {side} now TP {targets[0]} TP {targets[1]}', posted_at=now)
        session.add(edit); await session.flush(); edit_id = edit.id
        if rapid:
            latest = Message(source_id=source_id, message_id=810, edit_date=2, revision=3,
                             kind='text', status='pending',
                             text=f'Gold {side} now {zone} TP {targets[0]} TP {targets[1]}', posted_at=now)
            session.add(latest); await session.flush(); latest_id = latest.id
    updated = await _drive(runtime, provider, edit_id)
    assert updated['decision'] == 'acted', updated
    assert set(broker.positions) == tickets and sorted(p['tp'] for p in broker.positions.values()) == sorted(targets)
    if not rapid:
        async with session_scope() as session:
            edit = Message(source_id=source_id, message_id=810, edit_date=2, revision=3,
                           kind='text', status='pending',
                           text=f'Gold {side} now {zone} TP {targets[0]} TP {targets[1]}', posted_at=now)
            session.add(edit); await session.flush(); latest_id = edit.id
    updated = await _drive(runtime, provider, latest_id)
    assert updated['decision'] == 'acted', updated
    assert set(broker.positions) == tickets and len(provider.calls) == 1


@pytest.mark.parametrize('scenario', ['same_signal', 'direction_flip', 'zone_changed', 'other_post', 'deleted'])
async def test_market_now_fast_edits_during_ai_do_not_lose_or_change_the_signal(data_dir, scenario):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', username=None,
                           entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2,
                          'stop_distance': 15.0, 'entry_wait_seconds': 300, 'zone_tolerance': 2.0})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    source_id, row_id = await _seed('Gold sell now', message_id=812, posted_at=now)
    async with session_scope() as session:
        middle_text = 'Gold sell now TP 2375 TP 2372'
        if scenario == 'zone_changed': middle_text = 'Gold sell now 78-80 TP 2375 TP 2372'
        middle = Message(source_id=source_id, message_id=812, revision=2, edit_date=1,
                         kind='text', status='pending', text=middle_text, posted_at=now)
        last_text = ('Gold buy now 78-81 TP 2384 TP 2387' if scenario == 'direction_flip'
                     else 'Gold sell now 78-81 TP 2375 TP 2372')
        last = Message(source_id=source_id, message_id=812, revision=3, edit_date=2,
                       kind='text', status='pending', text=last_text, posted_at=now,
                       deleted_at=now if scenario == 'deleted' else None)
        session.add_all([middle, last]); await session.flush(); latest_id = last.id
        if scenario == 'other_post':
            session.add(Message(source_id=source_id, message_id=813, revision=1, edit_date=0,
                                kind='text', status='pending', text='Close the trade', posted_at=now))
    result = await _drive(runtime, provider, row_id)
    assert len(provider.calls) == (0 if scenario == 'deleted' else 1)
    if scenario == 'same_signal':
        assert result['decision'] == 'acted', result
        assert len(broker.positions) == 2
        assert sorted(p['tp'] for p in broker.positions.values()) == [2372, 2375]
        tickets = set(broker.positions)
        repeated = await _drive(runtime, provider, latest_id)
        assert repeated['status'] == 'finished' and set(broker.positions) == tickets
    else:
        assert not broker.positions


async def test_market_now_direction_edit_between_legs_stops_and_disarms(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', username=None,
                           entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy',
                          {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    source_id, row_id = await _seed('Gold sell now', message_id=814, posted_at=now)
    send = executor.rpc.order_send
    calls = 0
    async def edit_after_first(request):
        nonlocal calls
        calls += 1
        response = await send(request)
        if calls == 1:
            async with session_scope() as session:
                session.add(Message(source_id=source_id, message_id=814, revision=2, edit_date=1,
                                    kind='text', status='pending', text='Gold buy now', posted_at=now))
        return response
    executor.rpc.order_send = edit_after_first
    result = await _drive(runtime, provider, row_id)
    assert result['status'] == 'error' and calls == 1 and not await runtime.armed()
    assert len(broker.positions) == 1 and next(iter(broker.positions.values()))['sl'] > 0


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
        assert position['volume'] == 0.2
        assert abs(position['price_open'] - position['sl'] - 15) < 0.5
    assert sum(position['tp'] == 0 for position in broker.positions.values()) == 1
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


async def test_incomplete_target_then_three_targets_update_same_two_positions(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = _ScriptedProvider('yes')
    _install_provider(runtime, provider)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    source_id, row_id = await _seed('81-78 buy zone TP 3', message_id=203, posted_at=now)
    opened = await _drive(runtime, provider, row_id)
    assert opened['decision'] == 'acted', opened
    assert len(broker.positions) == 2 and all(p['sl'] > 0 for p in broker.positions.values())
    assert sum(p['tp'] == 0 for p in broker.positions.values()) == 1
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=203, edit_date=1, revision=2,
                       kind='text', status='pending', text='81-78 buy zone TP 84 TP 87 TP 90', posted_at=now)
        session.add(edit); await session.flush()
        edit_id = edit.id
    result = await _drive(runtime, provider, edit_id)
    assert result['decision'] == 'acted', result
    assert len(broker.positions) == 2 and sorted(p['tp'] for p in broker.positions.values()) == [2384, 2387]


async def test_single_target_edit_updates_only_first_leg_without_duplicate(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = _ScriptedProvider('yes')
    _install_provider(runtime, provider)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    source_id, row_id = await _seed('81-78 buy zone', message_id=205, posted_at=now)
    assert (await _drive(runtime, provider, row_id))['decision'] == 'acted'
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=205, edit_date=1, revision=2,
                       kind='text', status='pending', text='81-78 buy zone TP 84', posted_at=now)
        session.add(edit); await session.flush()
        edit_id = edit.id
    result = await _drive(runtime, provider, edit_id)
    assert result['decision'] == 'acted', result
    assert len(broker.positions) == 2 and sorted(p['tp'] for p in broker.positions.values()) == [0, 2384]


@pytest.mark.parametrize(('text', 'expected_tps', 'fallback_stop'), [
    ('81-78 buy zone TP 84', [0, 2384], True),
    ('81-78 buy zone SL 2400 TP 84 TP 87', [2384, 2387], True),
    ('81-78 buy zone SL 2370 TP 3', None, False),
])
async def test_fake_broker_uses_valid_levels_and_fallbacks(data_dir, text, expected_tps, fallback_stop):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
        row = Message(source_id=source.id, message_id=204, kind='text', status='pending', text=text,
                      posted_at=datetime.now(timezone.utc).replace(tzinfo=None))
        session.add(row); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=row.id,
                         signal_ref=row.signal_ref, origin='agent')
    result = await executor.open_uz_gold_signal(ctx)
    assert result['ok'], result
    assert len(broker.positions) == 2
    if expected_tps is None:
        assert sum(p['tp'] == 0 for p in broker.positions.values()) == 1
    else:
        assert sorted(p['tp'] for p in broker.positions.values()) == expected_tps
    for position in broker.positions.values():
        if fallback_stop:
            assert abs(position['price_open'] - position['sl'] - 15) < .5
        else:
            assert position['sl'] == 2370


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


async def test_end_to_end_one_check_open_then_edit_targets_and_reply_break_even(data_dir):
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
    assert len(provider.calls) == 1 and len(broker.positions) == 2
    assert sorted(p['volume'] for p in broker.positions.values()) == [0.1, 0.1]
    assert all(abs(p['price_open'] - p['sl'] - 15) < 0.5 for p in broker.positions.values())
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=501, edit_date=1, revision=2, kind='text', status='pending', text='81-78 buy zone TP 84 TP 87', posted_at=now)
        session.add(edit); await session.flush(); edit_id = edit.id
    updated = await _drive(runtime, provider, edit_id)
    assert updated['decision'] == 'acted', updated
    assert len(provider.calls) == 1 and len(broker.positions) == 2
    assert sorted(p['tp'] for p in broker.positions.values()) == [2384, 2387]
    broker.move('XAUUSDm', 2383)
    async with session_scope() as session:
        reply = Message(source_id=source_id, message_id=502, reply_to_message_id=501, kind='text', status='pending', text='B/u ga obqoying', posted_at=now+timedelta(seconds=20))
        session.add(reply); await session.flush(); reply_id = reply.id
    be = await _drive(runtime, provider, reply_id)
    assert be['decision'] == 'acted', be
    assert len(provider.calls) == 1 and len(broker.positions) == 2
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


@pytest.mark.parametrize('signal', ['81-78 buy zone', 'Gold sell now'])
async def test_uncertain_single_ai_check_never_opens_a_position(data_dir, signal):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Example signals', username=None, entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
    provider=_ScriptedProvider('uncertain'); _install_provider(runtime, provider)
    _, row_id = await _seed(signal, message_id=701)
    result = await _drive(runtime, provider, row_id)
    assert result['status'] == 'error' and len(provider.calls) == 1
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


@pytest.mark.parametrize('broker_state', ['empty', 'position', 'pending', 'unavailable', 'orders_unavailable'])
async def test_superseded_target_edit_only_skips_when_broker_is_empty(data_dir, broker_state):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _ScriptedProvider

    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Gold', username=None,
                        entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy',
                          {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
        first = Message(source_id=source.id, message_id=808, revision=1, edit_date=0,
                        kind='text', status='processed', text='Gold sell now', posted_at=now)
        old = Message(source_id=source.id, message_id=808, revision=2, edit_date=1,
                      kind='text', status='pending', text='Gold sell now TP 4025 TP 4005', posted_at=now)
        new = Message(source_id=source.id, message_id=808, revision=3, edit_date=2,
                      kind='text', status='pending', text='Gold sell now 4045-50 TP 4025 TP 4005', posted_at=now)
        session.add_all([first, old, new]); await session.flush()
        old_id, new_id = old.id, new.id

    if broker_state == 'position':
        async def positions_get(**kwargs): return [object()]
        executor.rpc.positions_get = positions_get
    elif broker_state == 'pending':
        async def orders_get(**kwargs): return [object()]
        executor.rpc.orders_get = orders_get
    elif broker_state == 'unavailable':
        async def positions_get(**kwargs): return None
        executor.rpc.positions_get = positions_get
    elif broker_state == 'orders_unavailable':
        async def orders_get(**kwargs): return None
        executor.rpc.orders_get = orders_get

    result = await _drive(runtime, _ScriptedProvider('yes'), old_id)
    if broker_state == 'empty':
        assert result['status'] == 'finished' and result['decision'] == 'ignored'
        assert await runtime.armed()
        latest = await _drive(runtime, _ScriptedProvider('yes'), new_id)
        assert latest['status'] == 'finished' and latest['decision'] == 'ignored'
        assert await runtime.armed()
    else:
        assert result['status'] == 'error'
        assert await runtime.armed()
    assert not broker.positions and not broker.orders


async def test_superseded_edit_never_skips_a_locally_tracked_live_trade(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _ScriptedProvider

    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Gold', username=None,
                        entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy',
                          {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
        first = Message(source_id=source.id, message_id=809, revision=1, edit_date=0,
                        kind='text', status='processed', text='81-78 buy zone', posted_at=now)
        session.add(first); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=first.id,
                         signal_ref=first.signal_ref, origin='agent')
    assert (await executor.open_uz_gold_signal(ctx))['ok']
    async with session_scope() as session:
        old = Message(source_id=ctx.source_id, message_id=809, revision=2, edit_date=1,
                      kind='text', status='pending', text='81-78 buy zone TP 84 TP 87', posted_at=now)
        new = Message(source_id=ctx.source_id, message_id=809, revision=3, edit_date=2,
                      kind='text', status='pending', text='81-78 buy zone TP 85 TP 88', posted_at=now)
        session.add_all([old, new]); await session.flush(); old_id = old.id
    async def positions_get(**kwargs): return []
    executor.rpc.positions_get = positions_get
    result = await _drive(runtime, _ScriptedProvider('yes'), old_id)
    assert result['status'] == 'error' and await runtime.armed()
    assert len(broker.positions) == 2


async def test_superseded_target_update_rejects_latest_direction_flip(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _ScriptedProvider

    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Gold', username=None,
                        entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy',
                          {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
        first = Message(source_id=source.id, message_id=811, revision=1, edit_date=0,
                        kind='text', status='processed', text='Gold sell now', posted_at=now)
        session.add(first); await session.flush()
        ctx = RunContext(run_id='test1234', source_id=source.id, message_row_id=first.id,
                         signal_ref=first.signal_ref, origin='agent')
    assert (await executor.open_uz_gold_signal(ctx))['ok']
    before = {ticket: (p['sl'], p['tp']) for ticket, p in broker.positions.items()}
    async with session_scope() as session:
        old = Message(source_id=ctx.source_id, message_id=811, revision=2, edit_date=1,
                      kind='text', status='pending', text='Gold sell now TP 2375 TP 2372', posted_at=now)
        new = Message(source_id=ctx.source_id, message_id=811, revision=3, edit_date=2,
                      kind='text', status='pending', text='Gold buy now 78-81 TP 2384 TP 2387', posted_at=now)
        session.add_all([old, new]); await session.flush(); old_id = old.id
    result = await _drive(runtime, _ScriptedProvider('yes'), old_id)
    assert result['status'] == 'error' and await runtime.armed()
    assert {ticket: (p['sl'], p['tp']) for ticket, p in broker.positions.items()} == before


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


async def test_unconfigured_close_request_warns_without_closing_or_pausing(data_dir):
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
    assert result['status']=='error' and await runtime.armed()
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


async def test_waiting_signal_accepts_same_zone_target_edit_and_keeps_one_entry(data_dir):
    import asyncio
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555; now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2,
                          'stop_distance': 15.0, 'entry_wait_seconds': 300, 'zone_tolerance': 2})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    source_id, row_id = await _seed('84-86 buy zone', message_id=2504, posted_at=now)
    task = asyncio.create_task(_drive(runtime, provider, row_id))
    try:
        for _ in range(100):
            if getattr(runtime, 'uz_gold_waiting', False): break
            await asyncio.sleep(.02)
        assert getattr(runtime, 'uz_gold_waiting', False)
        async with session_scope() as session:
            session.add(Message(source_id=source_id, message_id=2504, revision=2, edit_date=1,
                                kind='text', status='pending', posted_at=now,
                                text='84-86 buy zone TP 90 TP 95'))
        broker.move('XAUUSDm', 2383)
        result = await asyncio.wait_for(task, timeout=8)
        assert result['decision'] == 'acted', repr(result)
        assert len(broker.positions) == 2
        assert sorted(p['tp'] for p in broker.positions.values()) == [2390, 2395]
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


@pytest.mark.parametrize('change', ['targets', 'direction', 'zone', 'other_text', 'other_post'])
async def test_zone_entry_fast_target_edit_after_ai_checks_is_safe(data_dir, change):
    from datetime import datetime, timedelta, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=52)
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2,
                          'stop_distance': 15.0, 'entry_wait_seconds': 300, 'zone_tolerance': 2.0})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    source_id, row_id = await _seed('2378-2381 sell zone', message_id=2901, posted_at=now)
    edited = {
        'targets': '2378-2381 sell zone\n\nTP 2375\nTP 2372',
        'direction': '2378-2381 buy zone TP 2385 TP 2388',
        'zone': '2377-2381 sell zone TP 2375 TP 2372',
        'other_text': '2378-2381 sell zone cancelled TP 2375 TP 2372',
        'other_post': '2378-2381 sell zone TP 2375 TP 2372',
    }[change]
    async with session_scope() as session:
        edit = Message(source_id=source_id, message_id=2901, edit_date=1, revision=2,
                       kind='text', status='pending', text=edited, posted_at=now)
        session.add(edit); await session.flush()
        if change == 'other_post':
            session.add(Message(source_id=source_id, message_id=2902, kind='text', status='pending',
                                text='close', posted_at=now))
    result = await _drive(runtime, provider, row_id)
    assert len(provider.calls) == 1
    if change == 'targets':
        assert result['decision'] == 'acted', result
        assert len(broker.positions) == 2 and sorted(p['tp'] for p in broker.positions.values()) == [2372, 2375]
    else:
        assert result['decision'] == 'ignored' and not broker.positions


async def test_first_observed_edit_matches_1201_signal_and_never_replays(data_dir):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.db import session_scope, set_setting
    from app.models import Message, Source, Trade
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider
    executor, broker, runtime = await _build(data_dir, armed=True, balance=100000)
    broker.move('XAUUSDm', 3967)
    chat_id = -1005555555555
    posted = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=12)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy',
                          {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
        edit = Message(source_id=source.id, message_id=12059, revision=1,
                       edit_date=int((posted + timedelta(seconds=11)).replace(tzinfo=timezone.utc).timestamp()),
                       kind='text', status='pending', posted_at=posted,
                       text='3965-70 sell zone minimal risk\n\nTP 45\nTP 25')
        session.add(edit); await session.flush(); row_id = edit.id; source_id = source.id
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'acted', result
    assert len(provider.calls) == 1 and len(broker.positions) == 2
    assert sorted(p['tp'] for p in broker.positions.values()) == [3925, 3945]
    assert all(p['volume'] == 0.1 and abs(p['sl'] - p['price_open'] - 15) < 0.5
               for p in broker.positions.values())
    broker.positions.clear()
    async with session_scope() as session:
        trades = (await session.execute(select(Trade))).scalars().all()
        for trade in trades: trade.status = 'closed'
        original = Message(source_id=source_id, message_id=12059, revision=1, edit_date=0,
                           kind='text', status='pending', posted_at=posted,
                           text='3965-70 sell zone minimal risk TP 45 TP 25')
        session.add(original); await session.flush(); original_id = original.id
    repeated = await _drive(runtime, provider, original_id)
    assert repeated['decision'] == 'ignored' and not broker.positions


@pytest.mark.parametrize('condition', ['positions_unknown', 'orders_unknown', 'position_present', 'stale', 'catchup', 'future_edit'])
async def test_first_observed_edit_refuses_unverified_or_stale_inputs(data_dir, condition):
    from datetime import datetime, timedelta, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    posted = datetime.now(timezone.utc) - timedelta(seconds=120 if condition == 'stale' else 12)
    edited = posted + timedelta(seconds=11 if condition != 'future_edit' else 200)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy',
                          {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
        edit = Message(source_id=source.id, message_id=2904, revision=1, edit_date=int(edited.timestamp()),
                       kind='text', status='pending', posted_at=posted.replace(tzinfo=None),
                       catchup=condition == 'catchup', text='81-78 buy zone TP 84 TP 87')
        session.add(edit); await session.flush(); row_id = edit.id
    if condition in ('positions_unknown', 'position_present'):
        async def positions_get(**kwargs): return None if condition == 'positions_unknown' else [object()]
        executor.rpc.positions_get = positions_get
    if condition == 'orders_unknown':
        async def orders_get(**kwargs): return None
        executor.rpc.orders_get = orders_get
    calls = []
    send = executor.rpc.order_send
    async def track_send(request):
        calls.append(request)
        return await send(request)
    executor.rpc.order_send = track_send
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'ignored' and not broker.positions and not calls, result


async def test_first_observed_edit_is_not_replayed_after_a_skipped_run(data_dir):
    from datetime import datetime, timedelta, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider
    executor, broker, runtime = await _build(data_dir, armed=True)
    broker.move('XAUUSDm', 2370)
    chat_id = -1005555555555
    posted = datetime.now(timezone.utc) - timedelta(seconds=12)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy',
                          {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
        edit = Message(source_id=source.id, message_id=2905, revision=1,
                       edit_date=int((posted + timedelta(seconds=11)).timestamp()),
                       kind='text', status='pending', posted_at=posted.replace(tzinfo=None),
                       text='81-78 buy zone TP 84 TP 87')
        session.add(edit); await session.flush(); row_id = edit.id
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    first = await _drive(runtime, provider, row_id)
    assert first['decision'] == 'ignored' and not broker.positions
    broker.move('XAUUSDm', 2380)
    repeated = await _drive(runtime, provider, row_id)
    assert repeated['decision'] == 'ignored' and not broker.positions


async def test_first_observed_edit_does_not_trade_after_a_direction_correction(data_dir):
    from datetime import datetime, timedelta, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    posted = datetime.now(timezone.utc) - timedelta(seconds=12)
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy',
                          {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15.0})
        first = Message(source_id=source.id, message_id=2906, revision=1,
                        edit_date=int((posted + timedelta(seconds=10)).timestamp()), kind='text',
                        status='pending', posted_at=posted.replace(tzinfo=None), text='81-78 buy zone TP 84 TP 87')
        latest = Message(source_id=source.id, message_id=2906, revision=2,
                         edit_date=int((posted + timedelta(seconds=11)).timestamp()), kind='text',
                         status='pending', posted_at=posted.replace(tzinfo=None), text='81-78 sell zone TP 75 TP 72')
        session.add_all([first, latest]); await session.flush(); row_id = first.id
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'ignored' and not broker.positions


async def test_changed_targets_during_preorder_checks_rebuild_before_any_broker_send(data_dir):
    from datetime import datetime, timezone
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as s:
        s.add(Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True))
        await set_setting(s, 'ingest.research_only_chat_id', chat_id)
        await set_setting(s, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': 0.2,
                          'stop_distance': 15.0, 'entry_wait_seconds': 300, 'zone_tolerance': 2})
    provider = _ScriptedProvider('yes')
    _install_provider(runtime, provider)
    source_id, row_id = await _seed('84-86 buy zone', message_id=3101, posted_at=now)
    broker.move('XAUUSDm', 2383)
    original = executor._prepare
    injected = False
    async def prepare(*args, **kwargs):
        nonlocal injected
        result = await original(*args, **kwargs)
        if not injected:
            injected = True
            assert not broker.positions
            async with session_scope() as s:
                s.add(Message(source_id=source_id, message_id=3101, revision=2, edit_date=1,
                              kind='text', status='pending', posted_at=now,
                              text='84-86 buy zone TP 90 TP 95'))
        return result
    executor._prepare = prepare
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'acted', repr(result)
    assert len(broker.positions) == 2
    assert sorted(p['tp'] for p in broker.positions.values()) == [2390, 2395]
    opens = [params for name,params in broker.calls
             if name == 'order_send' and params.get('request', {}).get('action') == 1]
    assert len(opens) == 2


async def test_stop_announcement_does_not_disable_next_separate_sell_signal(data_dir):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Example', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2,
                          'stop_distance': 15.0, 'entry_wait_seconds': 300, 'zone_tolerance': 2})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    _, announcement_id = await _seed('Stop urdi reentry beraman', message_id=12078)
    notice = await _drive(runtime, provider, announcement_id)
    assert notice['decision'] == 'ignored' and await runtime.armed()
    assert not broker.positions and not provider.calls
    broker.move('XAUUSDm', 3916)
    _, entry_id = await _seed('3914-19 sell zone minimal risk', message_id=12079)
    result = await _drive(runtime, provider, entry_id)
    assert result['decision'] == 'acted', repr(result)
    assert len(provider.calls) == 1 and len(broker.positions) == 2
    assert all(p['type'] == 1 and p['volume'] == .1 and p['sl'] > p['price_open']
               for p in broker.positions.values())


async def test_ambiguous_reentry_instruction_does_not_disable_trading(data_dir):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Example', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = _ScriptedProvider('yes'); _install_provider(runtime, provider)
    _, row_id = await _seed('09-14 reentry 20sl zone', message_id=12082)
    result = await _drive(runtime, provider, row_id)
    assert result['status'] == 'error' and await runtime.armed()
    assert not broker.positions


async def test_explicit_reentry_runs_through_one_check_and_broker_simulation(data_dir):
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider, _seed
    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Example', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2,
                          'stop_distance': 15.0, 'entry_wait_seconds': 300, 'zone_tolerance': 2})
    provider = _ScriptedProvider('yes')
    _install_provider(runtime, provider)
    broker.move('XAUUSDm', 3882)
    _, row_id = await _seed('80-85 sell reentry', message_id=12112)
    result = await _drive(runtime, provider, row_id)
    assert result['decision'] == 'acted', repr(result)
    assert len(provider.calls) == 1 and len(broker.positions) == 2
    assert all(p['type'] == 1 and p['volume'] == .1 and p['sl'] > p['price_open']
               for p in broker.positions.values())


async def test_separate_gold_messages_use_free_workers_without_duplicate_ai_calls(data_dir):
    import asyncio
    from app.ai.base import DoneDelta, TextDelta
    from app.ai.loop import run_agent
    from app.db import session_scope, set_setting
    from app.models import Source
    from tests.test_executor import _build
    from tests.test_fast_entry import _install_provider, _ScriptedProvider, _seed

    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingProvider(_ScriptedProvider):
        def __init__(self):
            super().__init__('no')

        def run(self, **kwargs):
            self.calls.append(kwargs)
            number = len(self.calls)

            async def stream():
                if number == 1:
                    first_started.set()
                    await release_first.wait()
                elif number == 2:
                    second_started.set()
                yield TextDelta(text='no')
                yield DoneDelta(stop_reason='end_turn', assistant_blocks=[])
            return stream()

    executor, broker, runtime = await _build(data_dir, armed=True)
    chat_id = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind='telegram', chat_id=chat_id, title='Example', entity_type='channel', is_active=True))
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {'enabled': True, 'total_lots': .2, 'stop_distance': 15})
    provider = BlockingProvider()
    _install_provider(runtime, provider)
    _, first_id = await _seed('81-78 buy zone', message_id=8001)
    first_task = asyncio.create_task(run_agent(runtime, message_row_id=first_id, kind='signal'))
    second_task = None
    try:
        await asyncio.wait_for(first_started.wait(), timeout=5)
        _, second_id = await _seed('81-78 sell zone', message_id=8002)
        second_task = asyncio.create_task(run_agent(runtime, message_row_id=second_id, kind='signal'))
        await asyncio.wait_for(second_started.wait(), timeout=5)
        assert not first_task.done()
        assert len(provider.calls) == 2
        assert not broker.positions
    finally:
        release_first.set()
        await asyncio.gather(first_task, *([second_task] if second_task else []), return_exceptions=True)
