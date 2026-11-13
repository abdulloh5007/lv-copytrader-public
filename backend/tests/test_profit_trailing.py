"""Exercise profit trailing entirely against the in-memory broker."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from app.db import session_scope, set_setting
from app.models import Message, Source, Trade
from app.trading.executor import RunContext
from tests.test_executor import _build


async def setup_trade(data_dir, side, *, without_targets=False):
    executor, broker, _ = await _build(data_dir, armed=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    chat = -1005555555555
    text = ('81-78 buy zone TP 84 TP 99' if side == 'buy'
            else '78-81 sell zone TP 76 TP 61')
    if without_targets:
        text = '81-78 buy zone' if side == 'buy' else '78-81 sell zone'
    async with session_scope() as s:
        source = Source(kind='telegram', chat_id=chat, title='gold', username=None,
                        entity_type='channel', is_active=True)
        s.add(source)
        await s.flush()
        msg = Message(source_id=source.id, message_id=1, kind='text', status='pending',
                      text=text, posted_at=now)
        s.add(msg)
        await s.flush()
        await set_setting(s, 'ingest.research_only_chat_id', chat)
        await set_setting(s, 'trading.be_after_tp1', True)
        await set_setting(s, 'trading.uz_gold_policy',
                          {'enabled': True, 'total_lots': 0.2, 'stop_distance': 15})
        cfg = {'enabled': True, 'source_id': source.id,
               'start_ts': now.replace(tzinfo=timezone.utc).timestamp()-1,
               'step': 2, 'distance': 5}
        await set_setting(s, 'trading.uz_gold_trailing', cfg)
        ctx = RunContext(run_id='abcd1234', source_id=source.id,
                         message_row_id=msg.id, signal_ref=msg.signal_ref, origin='agent')
    result = await executor.open_uz_gold_signal(ctx)
    assert result['ok'], result
    async with session_scope() as s:
        rows = list((await s.execute(select(Trade).order_by(Trade.split_index))).scalars().all())
    assert len(rows) == 2
    return executor, broker, rows[0].ticket, rows[1].ticket, cfg


@pytest.mark.parametrize('side', ['buy', 'sell'])
async def test_targetless_signal_uses_1r_then_breakeven_and_trailing(data_dir, side):
    ex, broker, cover, runner, _ = await setup_trade(data_dir, side, without_targets=True)
    first = broker.positions[cover]
    assert first['tp'] > 0 and broker.positions[runner]['tp'] == 0
    assert abs(abs(first['tp'] - first['price_open']) - abs(first['price_open'] - first['sl'])) < .01
    target = first['tp']
    broker.move('XAUUSDm', target + 1 if side == 'buy' else target - 1)
    del broker.positions[cover]
    async with session_scope() as s:
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.status = 'closed'; row.close_cause = 'tp'
    await ex.trail_after_tp1()
    be = broker.positions[runner]['sl']
    entry = broker.positions[runner]['price_open']
    assert (be >= entry if side == 'buy' else be <= entry)
    broker.move('XAUUSDm', target + 8 if side == 'buy' else target - 8)
    await ex.trail_profit()
    moved = broker.positions[runner]['sl']
    assert (moved > be if side == 'buy' else moved < be)
    assert broker.positions[runner]['tp'] == 0


@pytest.mark.parametrize('stop', ['', ' SL 40pips'])
async def test_late_channel_targets_do_not_restore_tp_after_trailing_starts(data_dir, stop):
    ex, broker, cover, runner, _ = await setup_trade(data_dir, 'buy', without_targets=True)
    target = broker.positions[cover]['tp']
    broker.move('XAUUSDm', target + 1)
    del broker.positions[cover]
    async with session_scope() as s:
        first = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        first.status = 'closed'; first.close_cause = 'tp'
        original = await s.get(Message, first.message_row_id)
        edit = Message(source_id=original.source_id, message_id=original.message_id, revision=2,
                       edit_date=1, kind='text', status='pending',
                       text='81-78 buy zone TP 84 TP 99' + stop, posted_at=original.posted_at)
        s.add(edit); await s.flush()
        ctx = RunContext(run_id='abcd9999', source_id=original.source_id,
                         message_row_id=edit.id, signal_ref=edit.signal_ref, origin='agent')
    await ex.trail_after_tp1()
    await ex.trail_profit()
    before = dict(broker.positions[runner])
    assert before['tp'] == 0
    result = await ex.update_uz_gold_signal(ctx)
    assert result['ok'], result
    assert broker.positions[runner]['tp'] == 0
    assert broker.positions[runner]['sl'] == before['sl']


@pytest.mark.parametrize('side', ['buy', 'sell'])
async def test_only_after_confirmed_tp_and_be_then_stepwise_and_never_loosen(data_dir, side):
    ex, broker, cover, runner, _ = await setup_trade(data_dir, side)
    original_tp = broker.positions[runner]['tp']
    await ex.trail_profit()
    assert broker.positions[runner]['tp'] == original_tp
    broker.move('XAUUSDm', 2385 if side == 'buy' else 2375)
    del broker.positions[cover]
    async with session_scope() as s:
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.status = 'closed'; row.close_cause = 'sl'
    await ex.trail_after_tp1()
    await ex.trail_profit()
    assert broker.positions[runner]['tp'] == original_tp
    async with session_scope() as s:
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.close_cause = 'tp'
    await ex.trail_after_tp1()
    be = broker.positions[runner]['sl']
    await ex.trail_profit()
    assert broker.positions[runner]['tp'] == 0
    assert broker.positions[runner]['sl'] == be
    broker.move('XAUUSDm', 2391 if side == 'buy' else 2369)
    await ex.trail_profit()
    stop = broker.positions[runner]['sl']
    assert (stop > be if side == 'buy' else stop < be)
    broker.move('XAUUSDm', 2392 if side == 'buy' else 2368)
    await ex.trail_profit()
    assert broker.positions[runner]['sl'] == stop
    broker.move('XAUUSDm', 2394 if side == 'buy' else 2366)
    # A fresh executor models a process restart: the broker-held SL is the anchor.
    ex = type(ex)(ex.runtime)
    await ex.trail_profit()
    moved = broker.positions[runner]['sl']
    assert (moved > stop if side == 'buy' else moved < stop)
    broker.move('XAUUSDm', 2392 if side == 'buy' else 2368)
    await ex.trail_profit()
    assert broker.positions[runner]['sl'] == moved
    async with session_scope() as s:
        row = (await s.execute(select(Trade).where(Trade.ticket == runner))).scalars().one()
        assert row.runner_mode == 'be_after_tp1'


async def test_existing_signals_do_not_enrol(data_dir):
    ex, broker, cover, runner, cfg = await setup_trade(data_dir, 'buy')
    async with session_scope() as s:
        message = (await s.execute(select(Message))).scalars().one()
        message.posted_at -= timedelta(seconds=100)
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.status = 'closed'; row.close_cause = 'tp'
    broker.move('XAUUSDm', 2385)
    del broker.positions[cover]
    await ex.trail_after_tp1()
    before = dict(broker.positions[runner])
    await ex.trail_profit()
    assert broker.positions[runner] == before


async def test_stale_tick_never_moves_stop(data_dir):
    ex, broker, cover, runner, _ = await setup_trade(data_dir, 'buy')
    async with session_scope() as s:
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.status = 'closed'; row.close_cause = 'tp'
    broker.move('XAUUSDm', 2385)
    del broker.positions[cover]
    await ex.trail_after_tp1()
    await ex.trail_profit()
    before = dict(broker.positions[runner])
    broker.move('XAUUSDm', 2391)
    original = ex.resolver.quote
    async def stale(name):
        return replace(await original(name), time_ts=1)
    ex.resolver.quote = stale
    await ex.trail_profit()
    assert broker.positions[runner] == before


async def test_stale_tick_does_not_remove_target(data_dir):
    ex, broker, cover, runner, _ = await setup_trade(data_dir, 'buy')
    async with session_scope() as s:
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.status = 'closed'; row.close_cause = 'tp'
    broker.move('XAUUSDm', 2385)
    del broker.positions[cover]
    await ex.trail_after_tp1()
    before = dict(broker.positions[runner])
    original = ex.resolver.quote
    async def stale(name):
        return replace(await original(name), time_ts=1)
    ex.resolver.quote = stale
    await ex.trail_profit()
    assert broker.positions[runner] == before


async def test_disarmed_does_not_remove_runner_target(data_dir):
    ex, broker, cover, runner, _ = await setup_trade(data_dir, 'buy')
    async with session_scope() as s:
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.status = 'closed'; row.close_cause = 'tp'
    broker.move('XAUUSDm', 2385)
    del broker.positions[cover]
    await ex.trail_after_tp1()
    before = dict(broker.positions[runner])
    async with session_scope() as s:
        await set_setting(s, 'trading.armed', False)
    await ex.trail_profit()
    assert broker.positions[runner] == before


async def test_rejected_take_profit_removal_keeps_stop_and_tp(data_dir):
    ex, broker, cover, runner, _ = await setup_trade(data_dir, 'buy')
    async with session_scope() as s:
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.status = 'closed'; row.close_cause = 'tp'
    broker.move('XAUUSDm', 2385)
    del broker.positions[cover]
    await ex.trail_after_tp1()
    before = dict(broker.positions[runner])
    broker.force_retcode = 10016
    await ex.trail_profit()
    assert broker.positions[runner] == before


async def test_fast_trailing_monitor_keeps_stopless_watchdog_cadence(data_dir):
    import asyncio
    from types import SimpleNamespace
    from app.trading.guards import SlWatchdog
    await _build(data_dir)
    async with session_scope() as s:
        await set_setting(s, 'trading.uz_gold_trailing', {'enabled': True})
    watchdog = SlWatchdog(SimpleNamespace(settings=SimpleNamespace(SL_WATCHDOG_SECONDS=60)))
    called = []
    async def fast():
        called.append('monitor')
        watchdog._stopping.set()
    async def full():
        called.append('stopless_scan')
        watchdog._stopping.set()
    watchdog._monitor_trades = fast
    watchdog.tick = full
    await asyncio.wait_for(watchdog._loop(), timeout=7)
    assert called == ['monitor']


async def test_global_policy_supports_another_source_and_non_gold_instrument(data_dir):
    executor, broker, _ = await _build(data_dir, armed=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with session_scope() as s:
        source = Source(kind='telegram', chat_id=-1001234567899, title='Other', username=None,
                        entity_type='channel', is_active=True)
        s.add(source); await s.flush()
        msg = Message(source_id=source.id, message_id=900, kind='text', status='pending',
                      text='EURUSD BUY 1.085 TP 1.087 TP 1.095', posted_at=now)
        s.add(msg); await s.flush()
        await set_setting(s, 'trading.be_after_tp1', True)
        await set_setting(s, 'trading.profit_trailing', {'enabled': True, 'step': 0.0002,
            'distance': 0.0005, 'start_ts': now.replace(tzinfo=timezone.utc).timestamp()-1})
        ctx = RunContext(run_id='aabbccdd', source_id=source.id, message_row_id=msg.id,
                         signal_ref=msg.signal_ref, origin='manual')
    opened = await executor.open_market(ctx, symbol='EURUSDm', side='buy', sl=1.080,
                                        tp=[1.087, 1.095], reason='simulated test')
    assert opened['ok'], opened
    async with session_scope() as s:
        rows = list((await s.execute(select(Trade).order_by(Trade.split_index))).scalars().all())
    assert len(rows) == 2
    cover, runner = rows[0].ticket, rows[1].ticket
    broker.move('EURUSDm', 1.088)
    del broker.positions[cover]
    async with session_scope() as s:
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.status = 'closed'; row.close_cause = 'tp'
    await executor.trail_after_tp1()
    await executor.trail_profit()
    assert broker.positions[runner]['tp'] == 0
    be = broker.positions[runner]['sl']
    broker.move('EURUSDm', 1.089)
    await executor.trail_profit()
    assert broker.positions[runner]['sl'] > be


async def test_policy_change_during_quote_check_cannot_remove_target(data_dir):
    ex, broker, cover, runner, cfg = await setup_trade(data_dir, 'buy')
    async with session_scope() as s:
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.status = 'closed'; row.close_cause = 'tp'
    broker.move('XAUUSDm', 2385)
    del broker.positions[cover]
    await ex.trail_after_tp1()
    before = dict(broker.positions[runner])
    original = ex.resolver.quote
    async def changed(name):
        quote = await original(name)
        cfg['distance'] = 9
        async with session_scope() as s:
            await set_setting(s, 'trading.uz_gold_trailing', cfg)
        return quote
    ex.resolver.quote = changed
    await ex.trail_profit()
    assert broker.positions[runner] == before


@pytest.mark.parametrize('same_source', [True, False])
async def test_legacy_cohort_survives_global_migration_without_enrolling_other_old_trades(data_dir, same_source):
    import time
    ex, broker, cover, runner, cfg = await setup_trade(data_dir, 'buy')
    policy = {'enabled': True, 'step': 2.0, 'distance': 5.0, 'start_ts': time.time(),
              'legacy_start_ts': cfg['start_ts'],
              'legacy_source_id': cfg['source_id'] if same_source else cfg['source_id'] + 1}
    async with session_scope() as s:
        await set_setting(s, 'trading.profit_trailing', policy)
        row = (await s.execute(select(Trade).where(Trade.ticket == cover))).scalars().one()
        row.status = 'closed'; row.close_cause = 'tp'
    broker.move('XAUUSDm', 2385)
    del broker.positions[cover]
    await ex.trail_after_tp1()
    old_tp = broker.positions[runner]['tp']
    await ex.trail_profit()
    assert broker.positions[runner]['tp'] == (0 if same_source else old_tp)
