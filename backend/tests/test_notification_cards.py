"""Notification acceptance checks: native API, grouped entries, broker-confirmed TP + BE."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.events import EventBus
from app.telegram.notifier import Notifier, NotifierConfig
from app.trading.reconcile import monitor_closes
from tests.test_executor import _build, _ctx


def notifier() -> Notifier:
    result = Notifier(SimpleNamespace(settings=settings, bus=EventBus()))
    result._config = NotifierConfig(locale="ru")
    return result


def entry(ticket=1, signal="SIG:1:1", symbol="XAUUSDm", side="buy", **kw):
    return dict(symbol=symbol, side=side, signal_ref=signal, order_type="buy",
                volume=0.2, requested_volume=0.2, filled_volume=0.2, entry=2380,
                sl=2370, tickets=[ticket], kind="standard", digits=2,
                legs=[dict(ticket=ticket, volume=0.2, filled_volume=0.2,
                           fill_price=2380, tp=2390, sl=2370, ok=True)], **kw)


def test_two_entries_are_one_card_and_other_signals_are_separate():
    n = notifier()
    sent = []
    n.fire = lambda *a, **k: sent.append((a, k))
    n.begin_run("run")
    n.opened("run", **entry(1))
    n.opened("run", **entry(2))
    n.opened("run", **entry(3, signal="SIG:1:2"))
    assert not sent
    n.flush_run("run")
    assert len(sent) == 2
    text = sent[0][0][1]
    assert "0.20</code> + <code>0.20</code> = <code>0.40" in text
    assert "<code>1</code>, <code>2</code>" in text
    assert "ВХОД" in text and "Обычный" in text
    n.flush_run("run")
    assert len(sent) == 2


@pytest.mark.parametrize("status,word", [
    ({"close_cause": "tp", "tp_index": 1}, "TP1 ЗАБРАН"),
    ({"close_cause": "tp", "ladder_role": "cover"}, "TP ПОКРЫТИЯ"),
    ({"close_cause": "sl"}, "СТОП-ЛОСС"),
    ({"close_cause": "sl", "breakeven": True}, "БЕЗУБЫТОК"),
    ({"status": "cancelled"}, "ОТМЕНЁН"),
])
def test_close_statuses(status, word):
    text = notifier().render_closed(symbol="XAUUSDm", **status)
    assert word in text


def test_ladder_and_pending_and_partial_have_own_status():
    n = notifier()
    data = entry()
    data["kind"] = "ladder"
    data["runner_mode"] = "hold_channel_sl"
    data["legs"][0]["role"] = "cover"
    text = n.render_opened(**data)
    assert "Лесенка" in text and "Покрытие" in text and "Сохраняем SL канала" in text
    assert "ОРДЕР ВЫСТАВЛЕН" in n.render_opened(pending=True, **entry())
    assert "ЧАСТИЧНЫЙ ВХОД" in n.render_opened(partial_fill=True, **entry())


@pytest.mark.parametrize("unsupported,expected", [
    (set(), ["sendWidget"]),
    ({"sendWidget"}, ["sendWidget", "sendRichMessage"]),
    ({"sendWidget", "sendRichMessage"}, ["sendWidget", "sendRichMessage", "sendMessage"]),
])
async def test_widget_api_and_explicit_fallback(unsupported, expected):
    n = notifier()
    requests = []

    def handler(request):
        requests.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        if requests[-1][0] in unsupported:
            return httpx.Response(404, json={"ok": False, "description": "Method not found"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    n._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n._load_config = _config
    n._record = _record
    try:
        assert await n.send("opened", n.render_opened(**entry()))
        assert [r[0] for r in requests] == expected
        widget = requests[0][1]
        assert "<html>" in widget["html"] and "LV COPYTRADER" in widget["html"]
        assert "0.20" in widget["html"] and "caption" not in widget
        assert "rich_message" not in widget and "parse_mode" not in widget
        if "sendRichMessage" in expected:
            rich = next(body for method, body in requests if method == "sendRichMessage")
            assert rich["rich_message"]["html"].startswith("<h3>")
            assert "parse_mode" not in rich
    finally:
        await n.stop()


async def test_group_and_oversize_messages_use_rich_format():
    n = notifier(); requests = []
    def handler(request):
        requests.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    n._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async def group(): return NotifierConfig(token="test", chat_id=-100123, enabled=True, locale="ru")
    n._load_config = group; n._record = _record
    try:
        assert await n.send("closed", n.render_closed(symbol="XAUUSDm", close_cause="sl"))
        assert requests[-1][0] == "sendRichMessage"
        n._load_config = _config
        assert await n.send("attention", "<b>Attention</b>\n" + "large " * 200)
        assert requests[-1][0] == "sendRichMessage"
    finally:
        await n.stop()


def test_widget_escapes_untrusted_values_and_includes_complete_status():
    from app.telegram.widget import build_notification_widget
    from app.telegram.format import html_to_plain
    n = notifier()
    for body in [n.render_opened(**entry()), n.render_modified(symbol="XAUUSDm", breakeven=True),
                 n.render_closed(symbol="XAUUSDm", close_cause="tp", tp_index=1),
                 n.render_rejected(message="<script>alert(1)</script>"),
                 n.render_attention(detail="<img src=x onerror=alert(1)>"), n.render_cmd_help("ru")]:
        widget = build_notification_widget(body)
        assert widget and "LV COPYTRADER" in widget
        assert "<script>" not in widget and "<img src=" not in widget
        for token in ("XAUUSDm", "TP1", "ОШИБКА"):
            if token in html_to_plain(body):
                assert token in widget
    assert "&lt;script&gt;" in build_notification_widget(n.render_rejected(message="<script>alert(1)</script>"))


async def _config():
    return NotifierConfig(token="test", chat_id=1, enabled=True, locale="ru")


async def _record(*a, **kw):
    pass


@pytest.mark.parametrize("be_enabled", [True, False])
async def test_live_tp_card_reports_actual_remaining_protection(data_dir, be_enabled):
    from app.db import session_scope, set_setting

    ex, broker, runtime = await _build(data_dir)
    n = notifier()
    sent = []
    n.fire = lambda *a, **k: sent.append((a, k))
    runtime.notifier = n
    async with session_scope() as session:
        await set_setting(session, "trading.be_after_tp1", be_enabled)
    opened = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", volume=0.4,
                                 sl=2370, tp=[2390, 2400], reason="test grouped entry")
    assert opened["ok"]
    assert len(sent) == 1
    sent.clear()
    first, second = opened["tickets"]
    pos = broker.positions.pop(first)
    broker.move("XAUUSDm", 2395)
    broker.deals.append(dict(ticket=90001, position_id=first, symbol="XAUUSDm",
                             entry=1, reason=5, time=int(time.time() + broker.server_time_offset), price=2390,
                             volume=pos["volume"], profit=20, swap=0, commission=0))
    assert await monitor_closes(runtime) == 1
    assert len(sent) == 1
    text = sent[0][0][1]
    assert "TP1 ЗАБРАН" in text and str(second) in text
    assert ("в б/у" in text) is be_enabled
    assert ("SL сохранён" in text) is not be_enabled
    assert await monitor_closes(runtime) == 0
    assert len(sent) == 1


async def test_delayed_history_does_not_lose_tp_status(data_dir):
    ex, broker, runtime = await _build(data_dir)
    opened = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", sl=2370,
                                 tp=[2390, 2400], reason="delayed history")
    assert opened["ok"]
    first = opened["tickets"][0]
    broker.positions.pop(first)
    # Only an opening deal exists so far: the next history poll must still see this ticket.
    assert await monitor_closes(runtime) == 0
    from tests.test_executor import _trades
    assert next(t for t in await _trades() if t.ticket == first).status == "open"


def test_native_html_keeps_multiline_pre_and_bold_balanced():
    from app.telegram.format import telegram_html_to_rich_html
    text = "<b>Heading</b>\n<pre>a  b\n1  2</pre>\n<b>two\nlines</b>"
    rich = telegram_html_to_rich_html(text)
    assert "<pre>a  b\n1  2</pre>" in rich
    assert "<p><pre>" not in rich
    assert "<p><b>two\nlines</b></p>" in rich


async def test_preflight_refusal_has_error_card_without_broker_write(data_dir):
    ex, broker, runtime = await _build(data_dir, armed=False)
    n = notifier()
    sent = []
    n.fire = lambda *a, **k: sent.append((a, k))
    runtime.notifier = n
    result = await ex.dispatch("open_market", dict(symbol="XAUUSDm", side="buy", volume=0.2,
                               sl=2370, tp=2390, reason="disarmed"), _ctx())
    assert not result["ok"] and not broker.positions
    assert len(sent) == 1 and sent[0][0][0] == "rejected"
    assert "ОШИБКА" in sent[0][0][1]


async def test_failed_be_is_not_reported_as_protected(data_dir):
    ex, broker, runtime = await _build(data_dir)
    n = notifier()
    sent = []
    n.fire = lambda *a, **k: sent.append((a, k))
    runtime.notifier = n
    opened = await ex.open_market(_ctx(), symbol="XAUUSDm", side="buy", volume=0.4,
                                 sl=2370, tp=[2390, 2400], reason="test rejected BE")
    first, second = opened["tickets"]
    pos = broker.positions.pop(first)
    broker.move("XAUUSDm", 2395)
    broker.deals.append(dict(ticket=90002, position_id=first, symbol="XAUUSDm",
                             entry=1, reason=5, time=int(time.time()+broker.server_time_offset),
                             price=2390, volume=pos["volume"], profit=20, swap=0, commission=0))
    broker.force_retcode = 10016
    sent.clear()
    await monitor_closes(runtime)
    assert len(sent) == 2
    assert [a[0] for a, kw in sent] == ["rejected", "closed"]
    assert "SL сохранён" in sent[-1][0][1] and "в б/у" not in sent[-1][0][1]
    assert broker.positions[second]["sl"] == 2370


@pytest.mark.parametrize('event', ['opened', 'modified', 'closed', 'rejected', 'attention'])
async def test_each_notification_type_sends_one_widget_without_duplicate_caption(event):
    n = notifier(); requests = []
    def handler(request):
        requests.append((request.url.path.rsplit('/', 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})
    n._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n._load_config = _config; n._record = _record
    bodies = {
        'opened': n.render_opened(**entry()),
        'modified': n.render_modified(symbol='XAUUSDm', breakeven=True),
        'closed': n.render_closed(symbol='XAUUSDm', close_cause='tp', tp_index=1),
        'rejected': n.render_rejected(symbol='XAUUSDm', message='Rejected'),
        'attention': n.render_attention(detail='Check connection'),
    }
    try:
        assert await n.send(event, bodies[event])
        assert len(requests) == 1 and requests[0][0] == 'sendWidget'
        assert 'caption' not in requests[0][1]
        assert '<script' not in requests[0][1]['html'].lower()
    finally:
        await n.stop()


async def test_explicit_widget_rejection_falls_back_to_rich():
    n = notifier(); requests = []
    def handler(request):
        requests.append(request.url.path.rsplit('/', 1)[-1])
        return httpx.Response(400, json={'ok': False, 'description': 'Bad Request: widgets not supported in this chat'}) if len(requests) == 1 else httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})
    n._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n._load_config = _config; n._record = _record
    try:
        assert await n.send('attention', n.render_attention(detail='Test'))
        assert requests == ['sendWidget', 'sendRichMessage']
    finally:
        await n.stop()


async def test_ambiguous_widget_timeout_does_not_fall_back_to_another_format(monkeypatch):
    n = notifier(); methods = []
    monkeypatch.setattr(n._settings, 'NOTIFY_RETRY_DELAYS', ())
    def handler(request):
        methods.append(request.url.path.rsplit('/', 1)[-1])
        raise httpx.ReadTimeout('test timeout', request=request)
    n._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n._load_config = _config; n._record = _record
    try:
        assert not await n.send('attention', n.render_attention(detail='Connection lost'))
        assert methods == ['sendWidget']
    finally:
        await n.stop()


def test_widget_preserves_safe_signal_link_and_rejects_unsafe_links():
    from app.telegram.widget import build_notification_widget
    body = '<b>Event</b>\n<a href="https://t.me/c/123/456">Signal post</a>'
    widget = build_notification_widget(body)
    assert 'href="https://t.me/c/123/456"' in widget
    assert build_notification_widget('<b>Event</b>\n<a href="javascript:alert(1)">bad</a>') is None
    assert build_notification_widget('<b>Event</b>\n<a href="https://u:p@example.org/">bad</a>') is None
