from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.db import init_db, session_scope, set_setting
from app.models import Trade
from app.trading.daily_target import TARGET_REACHED, UNAVAILABLE, daily_target
from app.trading.performance import calendar_data, next_month, report_html, utc_start, week_start
from tests.test_executor import _build


def row(ticket, *, pnl=None, cause=None, at=None, status="closed", external=False):
    return Trade(
        ticket=ticket, symbol="XAUUSDm", kind="position", side="buy", order_type=0,
        status=status, volume=.1, entry_price=2380, magic=settings.MAGIC_NUMBER,
        external=external, signal_ref="SIG:1:20", closed_at=at, profit=pnl, close_cause=cause,
    )


async def test_calendar_uses_tashkent_date_and_keeps_missing_pnl_distinct(data_dir):
    await init_db()
    start = date(2026, 8, 1)
    end = next_month(start)
    assert week_start(start) == date(2026, 7, 27) and end == date(2026, 9, 1)
    boundary = utc_start(date(2026, 8, 3))
    async with session_scope() as session:
        session.add_all([
            row(1, pnl=20, cause="sl", at=boundary - timedelta(seconds=1)),
            row(2, pnl=-10, cause="sl", at=boundary),
            row(3, pnl=999, external=True, at=boundary),
            row(4, at=boundary + timedelta(days=1)),
        ])
    cfg = settings.model_copy(update={"PERFORMANCE_START_DATE": start})
    result = await calendar_data(cfg, start, end, today=date(2026, 8, 6))
    assert len(result["days"]) == 31
    assert result["days"][0]["state"] == "weekend"
    assert result["days"][1]["state"] == "win" and result["days"][1]["stops"] == 1
    assert result["days"][2]["net"] == -10
    assert result["days"][3]["state"] == "unknown"
    assert result["days"][4]["state"] == "idle"
    assert result["days"][6]["state"] == "future"
    assert (result["net"], result["active_days"], result["win_days"], result["loss_days"], result["unknown"]) == (10, 3, 1, 1, 1)
    attachment = report_html("month", result, "USD")
    assert b"+10.00 USD" in attachment and b"Unknown results: 1" in attachment
    assert b"SIG:1:20" not in attachment


async def test_daily_target_latches_and_does_not_close_unrelated_positions(data_dir, monkeypatch):
    executor, _, runtime = await _build(data_dir)
    runtime.settings = settings.model_copy(update={"DAILY_PROFIT_TARGET_PERCENT": 20})
    async with session_scope() as session:
        await set_setting(session, "trading.account_binding", {"login": 12, "server": "Demo"})
        session.add(row(55, status="open"))
    account = SimpleNamespace(login=12, server="Demo", balance=100)
    positions = [
        SimpleNamespace(ticket=55, type=0, magic=settings.MAGIC_NUMBER, symbol="XAUUSDm", profit=0, swap=0),
        SimpleNamespace(ticket=99, type=0, magic=0, symbol="XAUUSDm", profit=900, swap=0),
    ]
    closes = []

    async def account_info(): return account
    async def positions_get(): return positions
    async def orders_get(): return []
    async def deals(*args): return []
    async def offset(*args): return 0
    async def close(ctx, *, ticket, **kwargs):
        closes.append(ticket)
        return {"ok": False}

    monkeypatch.setattr(runtime.rpc, "account_info", account_info)
    monkeypatch.setattr(runtime.rpc, "positions_get", positions_get)
    monkeypatch.setattr(runtime.rpc, "orders_get", orders_get)
    monkeypatch.setattr(runtime.rpc, "history_deals_get", deals)
    monkeypatch.setattr(executor, "close_position", close)
    monkeypatch.setattr("app.trading.daily_target.server_time_offset", offset)

    now = datetime(2026, 11, 24, 18, 59, tzinfo=UTC)
    assert await daily_target(runtime, now=now) is None
    positions[0].profit = 20
    assert await daily_target(runtime, now=now + timedelta(seconds=30)) == TARGET_REACHED
    assert await daily_target(runtime, now=now + timedelta(seconds=40)) == TARGET_REACHED
    assert closes == [55] and await runtime.armed()
    assert await daily_target(runtime, now=now + timedelta(minutes=1)) is None
    account.login = 13
    assert await daily_target(runtime, now=now + timedelta(minutes=2)) == UNAVAILABLE
    assert closes == [55]


async def test_scheduled_daily_target_starts_at_tashkent_midnight(data_dir):
    _, _, runtime = await _build(data_dir)
    runtime.settings = settings.model_copy(update={
        "DAILY_PROFIT_TARGET_PERCENT": 20,
        "DAILY_PROFIT_TARGET_START_DATE": date(2026, 11, 25),
    })
    midnight = datetime(2026, 11, 24, 19, 0, tzinfo=UTC)

    assert await daily_target(runtime, now=midnight - timedelta(seconds=1)) is None
    assert await daily_target(runtime, now=midnight) == UNAVAILABLE  # No verified account binding.


@pytest.mark.parametrize("side,text,tp,direction", [
    ("buy", "XAUUSD BUY 2378-2382 SL 2373 TP 2385 TP 2410", 2410, 1),
    ("sell", "XAUUSD SELL 2378-2382 SL 2387 TP 2375 TP 2350", 2350, -1),
])
async def test_daily_gold_entry_sizes_from_channel_and_closes_before_tp(data_dir, side, text, tp, direction):
    from app.models import Source
    from tests.test_fast_entry import _ScriptedProvider, _drive, _install_provider, _seed

    _, broker, runtime = await _build(data_dir, balance=100.08)
    runtime.settings = settings.model_copy(update={
        "DAILY_PROFIT_TARGET_PERCENT": 20,
        "DAILY_PROFIT_DYNAMIC_GOLD_SIZE": True,
    })
    account = broker.m_account_info()
    chat = -1005555555555
    async with session_scope() as session:
        session.add(Source(kind="telegram", chat_id=chat, title="Gold", entity_type="channel", is_active=True))
        await set_setting(session, "ingest.research_only_chat_id", chat)
        await set_setting(session, "trading.uz_gold_policy", {"enabled": True, "total_lots": 0.2, "stop_distance": 15})
        await set_setting(session, "trading.account_binding", {"login": account["login"], "server": account["server"]})

    provider = _ScriptedProvider("yes")
    _install_provider(runtime, provider)
    _, message = await _seed(text, message_id=9876)
    result = await _drive(runtime, provider, message)
    assert result["decision"] == "acted", result
    assert len(broker.positions) == 1
    position = next(iter(broker.positions.values()))
    assert position["volume"] == 0.01
    assert position["tp"] == tp
    assert (position["price_open"] - position["sl"]) * direction > 0
    broker.move("XAUUSDm", 2380 + 22 * direction)
    position["profit"] = broker.m_account_info()["profit"]
    assert await daily_target(runtime) == TARGET_REACHED
    assert not broker.positions and await runtime.armed()

    from app.trading.daily_simulation import summary
    from app.trading.performance import TZ

    broker.move("XAUUSDm", 2380)
    sends_before = sum(method == "order_send" for method, _ in broker.calls)
    _, shadow_message = await _seed(text, message_id=9877)
    shadow = await _drive(runtime, provider, shadow_message)
    assert "simulated trade" in shadow["summary"]
    assert sum(method == "order_send" for method, _ in broker.calls) == sends_before
    snapshot = await summary(datetime.now(TZ).date().isoformat())
    assert snapshot["open"] == 2
    assert {row["continuation"] for row in snapshot["positions"]} == {False, True}


async def test_gold_daily_sizing_fallback_uses_a_protected_order(data_dir):
    from app.trading.daily_gold_size import gold_size_error, size_gold_for_day
    from app.trading.daily_target import DailyGoal

    _, _, runtime = await _build(data_dir)
    ref = await runtime.resolver.get("XAUUSDm")
    quote = await runtime.resolver.quote(ref.name)
    goal = DailyGoal(20.02, 20.02)
    plan = size_gold_for_day(ref, quote, "buy", channel_stop=None, fallback_distance=15,
                            channel_targets=(), goal=goal, max_volume=1)
    assert plan is not None and plan.fallback_stop and plan.fallback_target
    assert plan.stop_loss <= goal.target / 1.5
    assert plan.take_profit_gain > goal.remaining
    assert gold_size_error(ref, quote, "buy", plan) is None
    assert size_gold_for_day(ref, quote, "buy", channel_stop=quote.ask - 16, fallback_distance=15,
                             channel_targets=(quote.ask + 60,), goal=goal, max_volume=1) is None


async def test_daily_simulation_replays_first_bid_or_ask_exit_without_broker_writes(data_dir, monkeypatch):
    from app.trading.daily_simulation import advance, record, summary
    from app.trading.performance import TZ

    _, broker, runtime = await _build(data_dir)
    ref = await runtime.resolver.get("XAUUSDm")
    start = datetime(2026, 11, 24, 12, tzinfo=UTC).timestamp()
    for side, stop, tp in (("buy", 2375, 2390), ("sell", 2385, 2370)):
        assert await record(key=side, signal_ref=f"test:{side}", ref=ref, side=side, volume=0.01,
                            entry=2380, sl=stop, tp=tp, currency="USD", opened_at=start)

    async def offset(*args):
        return 10800

    async def history(symbol, lower, upper):
        assert symbol == "XAUUSDm" and lower == start + 10800
        # At 2384.90 bid is still below the short SL, but ask trips it. Later the buy takes profit.
        return [[int((start + 10801) * 1000), 2384.90, 2385.20],
                [int((start + 10802) * 1000), 2391.00, 2391.20]]

    monkeypatch.setattr("app.trading.daily_simulation.server_time_offset", offset)
    monkeypatch.setattr(runtime.rpc, "copy_quote_ticks", history, raising=False)
    await advance(runtime, now=start + 5)
    await advance(runtime, now=start + 10)
    value = await summary(datetime.fromtimestamp(start, TZ).date().isoformat())
    assert value["open"] == 0 and value["wins"] == value["losses"] == 1
    assert value["estimated_gross_pnl"] == {"USD": 4.80}
    assert not any(method == "order_send" for method, _ in broker.calls)


async def test_performance_api_only_exposes_enabled_snapshot(data_dir, monkeypatch):
    import httpx

    from app.main import create_app

    monkeypatch.setattr(settings, "SP_ALLOW_UNAUTHENTICATED", False)
    monkeypatch.setattr(settings, "SP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setattr(settings, "PERFORMANCE_START_DATE", date(2026, 1, 1))
    monkeypatch.setattr(settings, "PERFORMANCE_PUBLIC", False)
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get("/api/performance")).status_code == 404
        assert (await client.get("/api/history/trades")).status_code == 401
        assert (await client.get("/api/performance/simulation")).status_code == 401
        monkeypatch.setattr(settings, "PERFORMANCE_PUBLIC", True)
        assert (await client.get("/api/performance")).status_code == 503
        app.state.performance_snapshot = {"today": "2026-11-24", "currency": "USD"}
        response = await client.get("/api/performance")
        assert response.json() == app.state.performance_snapshot
        assert response.headers["Cache-Control"] == "no-store"
        assert (await client.post("/api/performance")).status_code == 401


async def test_report_is_sent_once_and_only_to_a_configured_private_chat(data_dir):
    import httpx

    from app.db import get_setting
    from app.telegram.notifier import Notifier

    await init_db()
    runtime = SimpleNamespace(settings=settings)
    notifier = Notifier(runtime)
    calls = []

    def telegram(request):
        calls.append(request.read())
        assert request.url.path.endswith("/sendDocument")
        return httpx.Response(200, json={"ok": True})

    data = {"start": "2026-11-01", "end": "2026-11-30", "active_days": 2, "win_days": 1,
            "loss_days": 1, "net": 3.25}
    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
        notifier._client = client
        async with session_scope() as session:
            await set_setting(session, "notifier.bot_token", "12345:mock", secret=True)
            await set_setting(session, "notifier.enabled", True)
            await set_setting(session, "notifier.chat_id", -22)
        await notifier.send_performance_report("performance.report.month.2026-12-01", "month", data, b"<html>")
        assert not calls
        async with session_scope() as session:
            await set_setting(session, "notifier.chat_id", 22)
        key = "performance.report.month.2026-12-01"
        await notifier.send_performance_report(key, "month", data, b"<html>")
        await notifier.send_performance_report(key, "month", data, b"<html>")
        assert len(calls) == 1 and b"lvct-month-2026-11-01.html" in calls[0]
        async with session_scope() as session:
            assert await get_setting(session, key) == "sent"
