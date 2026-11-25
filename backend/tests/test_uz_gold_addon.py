from datetime import datetime, timezone

import pytest

from app.ai.uz_gold_addon import addon_channel_stop, addon_price, is_addon_post


def test_only_unambiguous_addon_instructions_resolve_against_original_entry():
    assert is_addon_post('77 dalivka minimal risk')
    assert addon_price('77 dalivka minimal risk', reference=3869.015) == 3877
    assert addon_price('3877 dalivka', reference=3869.015) == 3877
    assert addon_price('3900 dalivka min risk', reference=3892) == 3900
    for text in ('77 dalivka?', 'BTC 77 dalivka', '77 dalivka sell', 'dalivka', '77 dalivka minimal risk or buy'):
        assert not is_addon_post(text)
    assert addon_price('50 dalivka', reference=3900) is None
    assert is_addon_post('75dalivka 78 Sl')
    assert addon_price('75dalivka 78 Sl', reference=3674.7) == 3675
    assert addon_channel_stop('75dalivka 78 Sl', reference=3675) == 3678
    assert addon_channel_stop('75 dalivka SL 3678', reference=3675) == 3678
    assert addon_channel_stop('75 dalivka', reference=3675) is None


@pytest.mark.parametrize('text,side,expected_stop', [
    ('80dalivka 77 Sl', 'buy', 2377),
    ('80dalivka 83 Sl', 'sell', 2383),
])
async def test_compact_addon_uses_its_channel_stop(data_dir, text, side, expected_stop):
    from app.db import session_scope
    from app.models import Message
    first = '81-78 buy zone TP 84 TP 87' if side == 'buy' else '81-78 sell zone TP 75 TP 72'
    executor, broker, runtime, ctx, now = await seed_pair_and_addon(data_dir, first_text=first)
    async with session_scope() as session:
        row = await session.get(Message, ctx.message_row_id)
        row.text = text
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    addon = broker.positions[result['tickets'][0]]
    assert addon['sl'] == expected_stop


async def test_compact_addon_rejects_stop_on_wrong_side(data_dir):
    from app.db import session_scope
    from app.models import Message
    executor, broker, runtime, ctx, now = await seed_pair_and_addon(
        data_dir, first_text='81-78 sell zone TP 75 TP 72')
    existing = set(broker.positions)
    async with session_scope() as session:
        row = await session.get(Message, ctx.message_row_id)
        row.text = '80dalivka 77 Sl'
    result = await executor.open_uz_gold_addon(ctx)
    assert not result['ok'] and set(broker.positions) == existing


async def seed_pair_and_addon(data_dir, *, enabled=True, price='80', reply=True,
                              first_text='81-78 buy zone TP 84 TP 87', small=False):
    from app.db import session_scope, set_setting
    from app.models import Message, Source
    from app.trading.executor import RunContext
    from tests.test_executor import _build
    executor, broker, runtime = await _build(data_dir, armed=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    chat_id = -1005555555555
    async with session_scope() as session:
        source = Source(kind='telegram', chat_id=chat_id, title='Gold', entity_type='channel', is_active=True)
        session.add(source); await session.flush()
        await set_setting(session, 'ingest.research_only_chat_id', chat_id)
        await set_setting(session, 'trading.uz_gold_policy', {
            'enabled': True, 'total_lots': .01 if small else .2,
            'stop_distance': 7 if small else 15, 'addon_enabled': enabled,
            'mode': 'small_account' if small else 'classic',
            'zone_tolerance': 2, 'entry_wait_seconds': 300,
        })
        first = Message(source_id=source.id, message_id=1001, kind='text', status='processed',
                        text=first_text, posted_at=now)
        session.add(first); await session.flush()
        first_ctx = RunContext(run_id='first', source_id=source.id, message_row_id=first.id,
                               signal_ref=first.signal_ref, origin='agent')
    result = await executor.open_uz_gold_signal(first_ctx)
    assert result['ok'], result
    async with session_scope() as session:
        post = Message(source_id=source.id, message_id=1002, kind='text', status='pending',
                       text=f'{price} dalivka minimal risk', posted_at=now,
                       reply_to_message_id=1001 if reply else None)
        session.add(post); await session.flush()
        ctx = RunContext(run_id='addon', source_id=source.id, message_row_id=post.id,
                         signal_ref=post.signal_ref, origin='uz_gold_addon')
    return executor, broker, runtime, ctx, now


async def test_small_account_opens_one_and_addon_once_per_signal(data_dir):
    from dataclasses import replace
    from app.db import session_scope
    from app.models import Message
    executor, broker, runtime, ctx, now = await seed_pair_and_addon(data_dir, price='82', small=True)
    assert len(broker.positions) == 1
    first = next(iter(broker.positions.values()))
    assert first['volume'] == .01 and first['tp'] == 2384
    assert abs(first['price_open'] - first['sl'] - 7) < .01
    broker.move('XAUUSDm', 2382)
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    addon = broker.positions[result['tickets'][0]]
    assert addon['volume'] == .01 and addon['tp'] == 2384
    assert abs(addon['price_open'] - addon['sl'] - 7) < .01
    async with session_scope() as session:
        another = Message(source_id=ctx.source_id, message_id=1003, kind='text', status='pending',
                          text='82 dalivka minimal risk', posted_at=now, reply_to_message_id=1001)
        session.add(another); await session.flush()
        second_ctx = replace(ctx, run_id='another', message_row_id=another.id, signal_ref=another.signal_ref)
    assert not (await executor.open_uz_gold_addon(second_ctx))['ok']
    assert len(broker.positions) == 2


async def test_small_account_fallback_target_is_one_to_one(data_dir):
    executor, broker, runtime, ctx, now = await seed_pair_and_addon(
        data_dir, price='82', first_text='81-78 buy zone', small=True)
    assert len(broker.positions) == 1
    first = next(iter(broker.positions.values()))
    assert first['volume'] == .01
    assert abs(first['price_open'] - first['sl'] - 7) < .01
    assert abs(first['tp'] - first['price_open'] - 7) < .01


async def test_small_account_fallback_stop_and_target_follow_actual_fill(data_dir, monkeypatch):
    from tests.fake_mt5 import FakeMt5
    send = FakeMt5.m_order_send
    def slipped(self, request):
        if request.get('action') == 1 and 'position' not in request:
            self.move('XAUUSDm', 2380.2)
        return send(self, request)
    monkeypatch.setattr(FakeMt5, 'm_order_send', slipped)
    executor, broker, runtime, ctx, now = await seed_pair_and_addon(
        data_dir, price='82', first_text='81-78 buy zone', small=True)
    first = next(iter(broker.positions.values()))
    assert abs(first['price_open'] - first['sl'] - 7) < .01
    assert abs(first['tp'] - first['price_open'] - 7) < .01


async def test_small_account_channel_edit_updates_only_primary_order(data_dir):
    from dataclasses import replace
    from app.db import session_scope
    from app.models import Message
    executor, broker, runtime, ctx, now = await seed_pair_and_addon(
        data_dir, price='82', first_text='81-78 buy zone', small=True)
    broker.move('XAUUSDm', 2382)
    opened = await executor.open_uz_gold_addon(ctx)
    assert opened['ok'], opened
    addon_before = dict(broker.positions[opened['tickets'][0]])
    async with session_scope() as session:
        edit = Message(source_id=ctx.source_id, message_id=1001, edit_date=1, revision=2,
                       kind='text', status='pending', text='81-78 buy zone TP 86 TP 88', posted_at=now)
        session.add(edit); await session.flush()
        update = replace(ctx, run_id='update', message_row_id=edit.id, signal_ref=edit.signal_ref, origin='agent')
    result = await executor.update_uz_gold_signal(update)
    assert result['ok'], result
    primary = next(p for ticket, p in broker.positions.items() if ticket != opened['tickets'][0])
    assert primary['tp'] == 2386
    addon_after = broker.positions[opened['tickets'][0]]
    assert addon_after['tp'] == addon_before['tp'] and addon_after['sl'] == addon_before['sl']


async def test_account_binding_refuses_all_broker_writes_on_account_mismatch(data_dir, monkeypatch):
    from app.db import session_scope, set_setting
    executor, broker, runtime, ctx, now = await seed_pair_and_addon(data_dir, small=True)
    sent = []
    monkeypatch.setattr(broker, 'm_order_send', lambda request: sent.append(request))
    async with session_scope() as session:
        await set_setting(session, 'trading.account_binding', {'login': 123456, 'server': 'Headway-Real'})
    result = await executor.send_order({'action': 1, 'symbol': 'XAUUSDm', 'type': 0,
                                        'volume': .01, 'price': 2380}, ctx=ctx, expect_pending=False)
    assert not result.ok and result.retcode_name == 'ACCOUNT_NOT_VERIFIED'
    assert sent == []


async def test_addon_opens_once_with_own_stop_first_tp_and_fixed_volume(data_dir):
    from dataclasses import replace
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir, price='82')
    first = next(p for p in broker.positions.values() if p['tp'] == 2384)
    original = {ticket: (p['sl'], p['tp']) for ticket, p in broker.positions.items()}
    broker.move('XAUUSDm', 2382)
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    assert len(broker.positions) == 3
    addon = broker.positions[result['tickets'][0]]
    assert addon['volume'] == .1 and addon['tp'] == first['tp']
    assert abs(addon['price_open'] - addon['sl'] - 15) < .01 and addon['sl'] != first['sl']
    assert all((broker.positions[ticket]['sl'], broker.positions[ticket]['tp']) == levels
               for ticket, levels in original.items())
    result = await executor.open_uz_gold_addon(replace(ctx, run_id='replayed_after_restart'))
    assert not result['ok'] and len(broker.positions) == 3


async def test_sell_addon_uses_own_fifteen_dollar_stop_and_first_target(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(
        data_dir, price='82', first_text='81-78 sell zone TP 75 TP 72')
    first = next(p for p in broker.positions.values() if p['tp'] == 2375)
    broker.move('XAUUSDm', 2382)
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    addon = broker.positions[result['tickets'][0]]
    assert addon['volume'] == .1 and addon['tp'] == 2375
    assert abs(addon['sl'] - addon['price_open'] - 15) < .01 and addon['sl'] != first['sl']


async def test_addon_fifteen_dollar_stop_uses_actual_slipped_fill(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(
        data_dir, price='82', first_text='81-78 sell zone TP 75 TP 72')
    broker.move('XAUUSDm', 2382)
    send = broker.m_order_send
    def slipped(request):
        if request.get('action') == 1 and 'position' not in request:
            broker.move('XAUUSDm', 2382.2)
        return send(request)
    broker.m_order_send = slipped
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'] and not result['safety_failures'], result
    addon = broker.positions[result['tickets'][0]]
    assert abs(addon['sl'] - addon['price_open'] - 15) < .01


@pytest.mark.parametrize(('enabled', 'price', 'reply'), [
    (False, '80', True), (True, '95', True), (True, '80', False),
])
async def test_addon_refuses_disabled_far_or_unlinked_posts(data_dir, enabled, price, reply):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir, enabled=enabled, price=price, reply=reply)
    assert not (await executor.open_uz_gold_addon(ctx))['ok']
    assert len(broker.positions) == 2


async def test_addon_does_not_depend_on_original_first_position_still_being_open(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    first_ticket = next(t for t, p in broker.positions.items() if p['tp'] == 2384)
    del broker.positions[first_ticket]
    assert (await executor.open_uz_gold_addon(ctx))['ok']
    assert len(broker.positions) == 2


async def test_addon_does_not_depend_on_original_stop_level(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(
        data_dir, price='96', first_text='81-78 sell zone TP 75 TP 72')
    broker.move('XAUUSDm', 2396)
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    assert len(broker.positions) == 3


async def set_saved_target(index, value):
    from sqlalchemy import select
    from app.db import session_scope
    from app.models import Trade
    async with session_scope() as session:
        trade = (await session.execute(select(Trade).where(
            Trade.signal_ref.like('%:1001'), Trade.split_index == index
        ))).scalars().one()
        trade.tp = value


async def mark_first_closed(broker, *, cause='sl', saved_tp=True):
    from sqlalchemy import select
    from app.db import session_scope
    from app.models import Trade
    ticket = next(t for t, p in broker.positions.items() if p['tp'] == 2375)
    del broker.positions[ticket]
    async with session_scope() as session:
        trade = (await session.execute(select(Trade).where(Trade.ticket == ticket))).scalars().one()
        trade.status = 'closed'
        trade.close_cause = cause
        trade.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if not saved_tp:
            trade.tp = 0


async def test_new_addon_after_confirmed_stop_uses_saved_tp_and_own_stop(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(
        data_dir, price='96', first_text='81-78 sell zone TP 75 TP 72')
    await mark_first_closed(broker)
    broker.move('XAUUSDm', 2396)
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    addon = broker.positions[result['tickets'][0]]
    assert addon['tp'] == 2375
    assert abs(addon['sl'] - addon['price_open'] - 15) < .01


async def test_addon_uses_own_1r_when_channel_has_no_target_and_follows_fill(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(
        data_dir, price='96', first_text='81-78 sell zone')
    broker.move('XAUUSDm', 2396)
    send = broker.m_order_send
    def slipped(request):
        if request.get('action') == 1 and 'position' not in request:
            broker.move('XAUUSDm', 2396.2)
        return send(request)
    broker.m_order_send = slipped
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'] and not result['safety_failures'], result
    addon = broker.positions[result['tickets'][0]]
    assert abs(addon['sl'] - addon['price_open'] - 15) < .01
    assert abs(addon['price_open'] - addon['tp'] - 15) < .01


async def test_addon_can_open_when_original_closed_by_take_profit(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(
        data_dir, price='96', first_text='81-78 sell zone TP 75 TP 72')
    await mark_first_closed(broker, cause='tp')
    broker.move('XAUUSDm', 2396)
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    assert len(broker.positions) == 2


async def test_addon_uses_own_1r_when_channel_did_not_publish_tp(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir, first_text='81-78 buy zone')
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    addon = broker.positions[result['tickets'][0]]
    assert abs(addon['price_open'] - addon['sl'] - 15) < .01
    assert abs(addon['tp'] - addon['price_open'] - 15) < .01


async def test_addon_uses_channel_first_target_even_if_first_trade_has_none(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    next(p for p in broker.positions.values() if p['tp'] == 2384)['tp'] = 0
    await set_saved_target(0, 0)
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    addon = broker.positions[result['tickets'][0]]
    assert addon['tp'] == 2384


async def test_addon_uses_own_stop_if_first_has_no_stop(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    next(p for p in broker.positions.values() if p['tp'] == 2384)['sl'] = 0
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    addon = broker.positions[result['tickets'][0]]
    assert abs(addon['price_open'] - addon['sl'] - 15) < .01


async def test_channel_addon_after_first_stop_runs_end_to_end(data_dir):
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(
        data_dir, price='96', first_text='81-78 sell zone TP 75 TP 72')
    await mark_first_closed(broker)
    broker.move('XAUUSDm', 2396)
    provider = _ScriptedProvider('yes')
    _install_provider(runtime, provider)
    result = await _drive(runtime, provider, ctx.message_row_id)
    assert result['decision'] == 'acted', result
    addon = next(p for p in broker.positions.values() if p['tp'] == 2375)
    assert abs(addon['sl'] - addon['price_open'] - 15) < .01


async def test_addon_works_when_original_signal_never_opened_any_order(data_dir):
    from sqlalchemy import delete
    from app.db import session_scope
    from app.models import Trade
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    broker.positions.clear()
    async with session_scope() as session:
        await session.execute(delete(Trade))
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    assert len(broker.positions) == 1
    addon = broker.positions[result['tickets'][0]]
    assert abs(addon['price_open'] - addon['sl'] - 15) < .01
    assert addon['tp'] == 2384


async def test_addon_uses_own_target_if_saved_targets_are_already_behind_price(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir, price='88')
    broker.move('XAUUSDm', 2388)
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    addon = broker.positions[result['tickets'][0]]
    assert abs(addon['tp'] - addon['price_open'] - 15) < .01


async def test_original_closed_marker_does_not_void_new_addon(data_dir):
    from sqlalchemy import select
    from app.db import session_scope
    from app.models import Message
    from app.trading.exposure import chain_exposure_closed
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    async with session_scope() as session:
        original = (await session.execute(select(Message).where(Message.message_id == 1001))).scalars().one()
        original.skip_reason = 'full_close'
    assert not await chain_exposure_closed(ctx.signal_ref)
    assert (await executor.open_uz_gold_addon(ctx))['ok']


async def test_addon_and_original_use_separate_run_locks_before_order(data_dir):
    from sqlalchemy import select
    from app.db import session_scope
    from app.models import Message
    from app.trading.guards import chain_run_key
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    async with session_scope() as session:
        original_id = (await session.execute(select(Message.id).where(Message.message_id == 1001))).scalar_one()
    assert await chain_run_key(original_id) != await chain_run_key(ctx.message_row_id)


async def test_addon_without_verifiable_source_direction_is_refused(data_dir):
    from app.db import session_scope
    from app.models import Message
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    async with session_scope() as session:
        row = await session.get(Message, ctx.message_row_id)
        row.reply_to_message_id = 999999
    result = await executor.open_uz_gold_addon(ctx)
    assert not result['ok'] and 'direction cannot be verified' in result['message']
    assert len(broker.positions) == 2


async def test_closing_original_signal_does_not_close_addon(data_dir):
    from app.trading.executor import RunContext
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    added = await executor.open_uz_gold_addon(ctx)
    assert added['ok'], added
    addon_ticket = added['tickets'][0]
    parent = RunContext(run_id='closeparent', source_id=ctx.source_id, message_row_id=None,
                        signal_ref=f'SIG:{ctx.source_id}:1001', origin='agent')
    result = await executor.close_signal_exposure(parent, reason='test original close')
    assert result['ok'], result
    assert list(broker.positions) == [addon_ticket]


async def test_closing_addon_does_not_close_original_signal(data_dir):
    from dataclasses import replace
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    added = await executor.open_uz_gold_addon(ctx)
    assert added['ok'], added
    addon_ticket = added['tickets'][0]
    result = await executor.close_signal_exposure(replace(ctx, run_id='closeaddon'), reason='test addon close')
    assert result['ok'], result
    assert addon_ticket not in broker.positions and len(broker.positions) == 2


async def test_reply_to_addon_closes_only_addon_not_original(data_dir):
    from app.db import session_scope
    from app.models import Message
    from app.trading.executor import RunContext
    executor, broker, runtime, ctx, now = await seed_pair_and_addon(data_dir)
    added = await executor.open_uz_gold_addon(ctx)
    assert added['ok'], added
    addon_ticket = added['tickets'][0]
    async with session_scope() as session:
        child = Message(source_id=ctx.source_id, message_id=1003, reply_to_message_id=1002,
                        kind='text', status='pending', text='close trade', posted_at=now)
        session.add(child); await session.flush()
        child_ctx = RunContext(run_id='closeaddonreply', source_id=ctx.source_id,
                               signal_ref=child.signal_ref, message_row_id=child.id, origin='agent')
    result = await executor.close_signal_exposure(child_ctx, reason='test addon reply close')
    assert result['ok'], result
    assert addon_ticket not in broker.positions and len(broker.positions) == 2


async def test_original_target_update_does_not_change_independent_addon(data_dir):
    from app.db import session_scope
    from app.models import Message
    from app.trading.executor import RunContext
    executor, broker, runtime, ctx, now = await seed_pair_and_addon(data_dir)
    added = await executor.open_uz_gold_addon(ctx)
    assert added['ok'], added
    addon_ticket = added['tickets'][0]
    async with session_scope() as session:
        edit = Message(source_id=ctx.source_id, message_id=1001, edit_date=1, revision=2,
                       kind='text', status='pending', text='81-78 buy zone TP 85 TP 88', posted_at=now)
        session.add(edit); await session.flush()
        update = RunContext(run_id='update', source_id=ctx.source_id, message_row_id=edit.id,
                            signal_ref=edit.signal_ref, origin='agent')
    result = await executor.update_uz_gold_signal(update)
    assert result['ok'], result
    assert len(broker.positions) == 3
    assert broker.positions[addon_ticket]['tp'] == 2384
    assert sorted(p['tp'] for p in broker.positions.values()) == [2384, 2385, 2388]


async def test_original_stop_update_does_not_change_independent_addon(data_dir):
    from app.db import session_scope
    from app.models import Message
    from app.trading.executor import RunContext
    executor, broker, runtime, ctx, now = await seed_pair_and_addon(data_dir, price='82')
    broker.move('XAUUSDm', 2382)
    added = await executor.open_uz_gold_addon(ctx)
    assert added['ok'], added
    addon_ticket = added['tickets'][0]
    async with session_scope() as session:
        reply = Message(source_id=ctx.source_id, message_id=1003, reply_to_message_id=1001,
                        kind='text', status='pending', text='SL 40pips', posted_at=now)
        session.add(reply); await session.flush()
        update = RunContext(run_id='stopupdate', source_id=ctx.source_id, message_row_id=reply.id,
                            signal_ref=reply.signal_ref, origin='agent')
    result = await executor.update_uz_gold_signal(update)
    assert result['ok'], result
    first = next(p for ticket, p in broker.positions.items()
                 if ticket != addon_ticket and p['tp'] == 2384)
    addon = broker.positions[addon_ticket]
    assert abs(addon['price_open'] - addon['sl'] - 15) < .01
    assert abs(first['price_open'] - first['sl'] - 4) < .01


async def test_addon_uncertain_broker_reply_is_never_retried(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    calls = 0
    async def lost_reply(request):
        nonlocal calls
        calls += 1
        raise RuntimeError('lost response')
    executor.rpc.order_send = lost_reply
    result = await executor.open_uz_gold_addon(ctx)
    assert not result['ok'] and result['safety_failures']
    result = await executor.open_uz_gold_addon(ctx)
    assert not result['ok'] and calls == 1 and len(broker.positions) == 2


async def test_addon_refuses_when_disarmed(data_dir):
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    await runtime.set_armed(False)
    assert not (await executor.open_uz_gold_addon(ctx))['ok']
    assert len(broker.positions) == 2


async def test_channel_message_dispatches_addon_and_records_decision(data_dir):
    from tests.test_fast_entry import _drive, _install_provider, _ScriptedProvider
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(data_dir)
    provider = _ScriptedProvider('yes')
    _install_provider(runtime, provider)
    result = await _drive(runtime, provider, ctx.message_row_id)
    assert result['decision'] == 'acted', result
    assert len(broker.positions) == 3


async def test_addon_does_not_guess_original_short_zone_when_no_original_order(data_dir):
    from sqlalchemy import delete, select
    from app.db import session_scope
    from app.models import Message, Trade
    executor, broker, runtime, ctx, _ = await seed_pair_and_addon(
        data_dir, price='04', first_text='81-78 sell zone TP 75 TP 72')
    broker.positions.clear()
    async with session_scope() as session:
        await session.execute(delete(Trade))
        original = (await session.execute(select(Message).where(Message.message_id == 1001))).scalars().one()
        original.text = '98-01 sell zone TP 90'
    broker.move('XAUUSDm', 2404)
    result = await executor.open_uz_gold_addon(ctx)
    assert result['ok'], result
    addon = broker.positions[result['tickets'][0]]
    assert abs(addon['price_open'] - addon['tp'] - 15) < .01
