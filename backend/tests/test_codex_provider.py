from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from app.ai.base import Capabilities, DoneDelta, ToolCallDone, ToolSpec, text_block, tool_result_block
from app.ai import codex_provider as bridge
from app.errors import ProviderError


async def test_tool_calls_are_returned_to_existing_executor_and_isolated(tmp_path, monkeypatch):
    observed = []
    async def server(ws):
        cid = id(ws)
        async for raw in ws:
            msg = json.loads(raw)
            method = msg.get('method')
            if method == 'initialized':
                continue
            if method == 'initialize':
                await ws.send(json.dumps({'id': msg['id'], 'result': {}}))
            elif method == 'thread/start':
                assert msg['params']['environments'] == []
                await ws.send(json.dumps({'id': msg['id'], 'result': {'thread': {'id': str(cid)}}}))
            elif method == 'turn/start':
                await ws.send(json.dumps({'id': msg['id'], 'result': {}}))
                await ws.send(json.dumps({'id': 99, 'method': 'item/tool/call', 'params': {
                    'tool': 'demo', 'callId': 'call-'+str(cid), 'arguments': {'symbol': 'XAUUSD'}}}))
            elif msg.get('id') == 99:
                observed.append(msg['result']['contentItems'][0]['text'])
                await ws.send(json.dumps({'method': 'item/agentMessage/delta', 'params': {'delta': 'done'}}))
                await ws.send(json.dumps({'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}))
    async with serve(server, '127.0.0.1', 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        monkeypatch.setattr(bridge, '_URL', f'ws://127.0.0.1:{port}')
        token = tmp_path / 'token'
        token.write_text('test')
        provider = bridge.CodexProvider(token)
        caps = Capabilities(supports_vision=True, supports_tools=True)
        tools = [ToolSpec('demo', 'demo only', {'type': 'object', 'properties': {}})]
        async def one(label):
            msgs = [{'role': 'user', 'content': [text_block(label)]}]
            first = [x async for x in provider.run(system='test', messages=msgs, tools=tools, max_tokens=20, model='gpt-6.1-terra', capabilities=caps)]
            call = next(x for x in first if isinstance(x, ToolCallDone))
            done = next(x for x in first if isinstance(x, DoneDelta))
            msgs += [{'role': 'assistant', 'content': done.assistant_blocks},
                     {'role': 'user', 'content': [tool_result_block(call.id, label)]}]
            second = [x async for x in provider.run(system='test', messages=msgs, tools=tools, max_tokens=20, model='gpt-6.1-terra', capabilities=caps)]
            assert next(x for x in second if isinstance(x, DoneDelta)).stop_reason == 'end_turn'
        await asyncio.gather(*(one(f'result-{n}') for n in range(3)))
        assert sorted(observed) == ['result-0', 'result-1', 'result-2']
        assert not provider._turns
        await provider.aclose()


async def test_unavailable_service_fails_without_tools_or_trade(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, '_URL', 'ws://127.0.0.1:1')
    token = tmp_path / 'token'
    token.write_text('test')
    provider = bridge.CodexProvider(token)
    with pytest.raises(ProviderError):
        async for _ in provider.run(system='test', messages=[], tools=[], max_tokens=20, model='gpt-6.1-terra', capabilities=Capabilities(False, False)):
            pass


async def test_duplicate_tool_call_id_is_never_returned_twice(tmp_path, monkeypatch):
    rejected = []
    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            method = msg.get('method')
            if method == 'initialize':
                await ws.send(json.dumps({'id': msg['id'], 'result': {}}))
            elif method == 'thread/start':
                await ws.send(json.dumps({'id': msg['id'], 'result': {'thread': {'id': 'thread'}}}))
            elif method == 'turn/start':
                await ws.send(json.dumps({'id': msg['id'], 'result': {}}))
                await ws.send(json.dumps({'id': 40, 'method': 'item/tool/call', 'params': {'tool': 'demo', 'callId': 'same', 'arguments': {}}}))
            elif msg.get('id') == 40:
                await ws.send(json.dumps({'id': 41, 'method': 'item/tool/call', 'params': {'tool': 'demo', 'callId': 'same', 'arguments': {}}}))
            elif msg.get('id') == 41:
                rejected.append('error' in msg)
                await ws.send(json.dumps({'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}))
    async with serve(server, '127.0.0.1', 0) as listener:
        monkeypatch.setattr(bridge, '_URL', f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
        token = tmp_path / 'token'; token.write_text('test')
        provider = bridge.CodexProvider(token)
        caps = Capabilities(False, True)
        tools = [ToolSpec('demo', 'demo', {'type': 'object'})]
        messages = [{'role': 'user', 'content': [text_block('demo')]}]
        first = [x async for x in provider.run(system='x', messages=messages, tools=tools, max_tokens=10, model='gpt-6.1-terra', capabilities=caps)]
        call = next(x for x in first if isinstance(x, ToolCallDone))
        done = next(x for x in first if isinstance(x, DoneDelta))
        messages += [{'role':'assistant', 'content':done.assistant_blocks}, {'role':'user', 'content':[tool_result_block(call.id, 'ok')]}]
        second = [x async for x in provider.run(system='x', messages=messages, tools=tools, max_tokens=10, model='gpt-6.1-terra', capabilities=caps)]
        assert not any(isinstance(x, ToolCallDone) for x in second)
        assert rejected == [True]
        await provider.aclose()


async def test_connection_loss_after_tool_does_not_allow_automatic_replay(tmp_path, monkeypatch):
    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get('method') == 'initialize':
                await ws.send(json.dumps({'id': msg['id'], 'result': {}}))
            elif msg.get('method') == 'thread/start':
                await ws.send(json.dumps({'id': msg['id'], 'result': {'thread': {'id': 'thread'}}}))
            elif msg.get('method') == 'turn/start':
                await ws.send(json.dumps({'id': msg['id'], 'result': {}}))
                await ws.send(json.dumps({'id': 40, 'method': 'item/tool/call', 'params': {'tool': 'demo', 'callId': 'once', 'arguments': {}}}))
            elif msg.get('id') == 40:
                await ws.close()
    async with serve(server, '127.0.0.1', 0) as listener:
        monkeypatch.setattr(bridge, '_URL', f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
        token = tmp_path / 'token'; token.write_text('test')
        provider = bridge.CodexProvider(token)
        caps = Capabilities(False, True)
        tools = [ToolSpec('demo', 'demo', {'type': 'object'})]
        messages = [{'role': 'user', 'content': [text_block('demo')]}]
        first = [x async for x in provider.run(system='x', messages=messages, tools=tools, max_tokens=10, model='gpt-6.1-terra', capabilities=caps)]
        call = next(x for x in first if isinstance(x, ToolCallDone))
        done = next(x for x in first if isinstance(x, DoneDelta))
        messages += [{'role': 'assistant', 'content': done.assistant_blocks}, {'role': 'user', 'content': [tool_result_block(call.id, 'ok')]}]
        with pytest.raises(ProviderError) as exc:
            async for _ in provider.run(system='x', messages=messages, tools=tools, max_tokens=10, model='gpt-6.1-terra', capabilities=caps):
                pass
        assert exc.value.kind == 'unknown'
        assert not provider._turns


def test_codex_is_an_opt_in_provider_without_api_key():
    from app.ai.base import build_provider
    from app.schemas import ProviderSettings
    provider = build_provider('codex', base_url='', api_key='')
    assert isinstance(provider, bridge.CodexProvider)
    settings = ProviderSettings(kind='codex', model='gpt-6.1-terra', base_url='')
    assert settings.kind == 'codex'


async def test_runtime_builds_codex_provider_when_selected(data_dir, monkeypatch):
    from app.ai.base import Capabilities
    from app.db import init_db, session_scope, set_setting
    from app.runtime import Runtime
    await init_db()
    async with session_scope() as session:
        await set_setting(session, 'provider.kind', 'codex')
        await set_setting(session, 'provider.model', 'gpt-6.1-terra')
        await set_setting(session, 'provider.base_url', '')
    async def fake_probe(self, model):
        assert model == 'gpt-6.1-terra'
        return Capabilities(True, True, source='declared')
    monkeypatch.setattr(bridge.CodexProvider, 'probe', fake_probe)
    runtime = Runtime()
    result = await runtime.provider()
    assert result is not None
    provider, model, caps = result
    assert isinstance(provider, bridge.CodexProvider)
    assert model == 'gpt-6.1-terra' and caps.supports_tools
    await provider.aclose()


async def test_agent_uses_existing_dispatcher_for_codex_tools(tmp_path, monkeypatch):
    from app.ai.loop import _AgentRun
    from app.events import EventBus
    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get('method') == 'initialize':
                await ws.send(json.dumps({'id': msg['id'], 'result': {}}))
            elif msg.get('method') == 'thread/start':
                await ws.send(json.dumps({'id': msg['id'], 'result': {'thread': {'id': 't'}}}))
            elif msg.get('method') == 'turn/start':
                await ws.send(json.dumps({'id': msg['id'], 'result': {}}))
                await ws.send(json.dumps({'id': 40, 'method': 'item/tool/call', 'params': {'tool': 'demo', 'callId': 'once', 'arguments': {'x': 2}}}))
            elif msg.get('id') == 40:
                await ws.send(json.dumps({'method': 'item/agentMessage/delta', 'params': {'delta': 'done'}}))
                await ws.send(json.dumps({'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}}))
    class FakeExecutor:
        def __init__(self): self.calls = []
        async def dispatch(self, name, arguments, ctx):
            self.calls.append((name, arguments, ctx.origin))
            return {'ok': True, 'value': 'synthetic'}
    class FakeRuntime:
        def __init__(self):
            self.bus = EventBus()
            self.executor = FakeExecutor()
        def set_status(self, *args): pass
    async with serve(server, '127.0.0.1', 0) as listener:
        monkeypatch.setattr(bridge, '_URL', f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
        token = tmp_path / 'token'; token.write_text('test')
        runtime = FakeRuntime()
        run = _AgentRun(runtime, run_id='test', message_row_id=None, kind='manual', instruction='demo')
        run.provider = bridge.CodexProvider(token)
        run.caps = Capabilities(False, True)
        run.model = 'gpt-6.1-terra'
        run.system_prompt = 'test only'
        run.messages = [{'role': 'user', 'content': [text_block('demo')]}]
        async def no_persist(*args, **kwargs): return None
        async def no_step(*args, **kwargs): return 1
        monkeypatch.setattr(run, '_persist_provider_call', no_persist)
        monkeypatch.setattr(run, '_persist_turn_text', no_persist)
        monkeypatch.setattr(run, '_announce_tool_call', no_persist)
        monkeypatch.setattr(run, '_add_step', no_step)
        specs = [ToolSpec('demo', 'demo', {'type': 'object'})]
        first = await run._turn(specs)
        run.messages.append({'role': 'assistant', 'content': first.assistant_blocks})
        await run._run_tools(first)
        assert runtime.executor.calls == [('demo', {'x': 2}, 'agent')]
        second = await run._turn(specs)
        assert second.stop_reason == 'end_turn' and second.text == 'done'
        await run.provider.aclose()


def test_failed_turn_reports_sanitized_reason_and_never_replays_after_tool():
    error = {'status': 'failed', 'error': {'message': '429 usage limit; authorization: Bearer test-secret'}}
    before = bridge._turn_failure(error, after_tool=False)
    assert before.kind == 'rate_limit'
    assert 'usage limit' in before.message
    assert 'test-secret' not in before.message
    after = bridge._turn_failure(error, after_tool=True)
    assert after.kind == 'unknown'


def test_auth_failure_is_not_retried_automatically():
    error = {'status': 'failed', 'error': {'message': '401 unauthorized'}}
    assert bridge._turn_failure(error, after_tool=False).kind == 'auth'
