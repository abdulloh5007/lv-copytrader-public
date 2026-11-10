"""Codex app-server bridge. No broker operations are performed in this module.

Each asyncio task owns one isolated, ephemeral thread. Dynamic tool requests are
returned to the existing agent loop, which alone can execute them with its normal
broker guards. The app-server must be local, authenticated, and run without shell
or filesystem access; see the deployment instructions.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import websockets

from app.ai.base import (Capabilities, DoneDelta, ModelInfo, TextDelta, ToolCallDone,
                         ToolSpec, text_block, tool_use_block)
from app.errors import ProviderError
from app.util.logging import redact

_MODEL = 'gpt-6.1-terra'
_URL = 'ws://127.0.0.1:18477'


def _turn_failure(turn: dict[str, Any], *, after_tool: bool) -> ProviderError:
    """Preserve a sanitized provider cause; never replay after a tool result."""
    error = turn.get('error')
    if isinstance(error, dict):
        raw = str(error.get('message') or error.get('code') or '')
    else:
        raw = str(error or '')
    message = ' '.join(redact(raw).split())[:400]
    low = raw.lower()
    if after_tool:
        kind = 'unknown'
    elif any(x in low for x in ('usage limit', 'rate limit', 'too many requests', '429', 'quota')):
        kind = 'rate_limit'
    elif any(x in low for x in ('unauthorized', 'unauthenticated', 'authentication', 'expired token', '401', '403')):
        kind = 'auth'
    elif any(x in low for x in ('overloaded', '503', '502', '504', '500')):
        kind = 'overloaded'
    else:
        kind = 'transport'
    return ProviderError('Codex: ' + message if message else 'Codex response did not complete; no provider reason was supplied', kind=kind)


@dataclass
class _Turn:
    ws: Any
    thread_id: str
    allowed: set[str]
    pending_id: Any = None
    call_id: str = ''
    backlog: list[dict[str, Any]] = field(default_factory=list)
    seen_calls: set[str] = field(default_factory=set)
    resumed_after_tool: bool = False


class CodexProvider:
    """Translate Codex turns to the application's provider protocol."""
    kind = 'codex'

    def __init__(self, token_file: str | Path, *, effort: str = 'medium') -> None:
        self.token_file = Path(token_file)
        self.effort = effort if effort in ('low', 'medium') else 'medium'
        self._turns: dict[asyncio.Task[Any], _Turn] = {}

    async def _verify(self) -> None:
        caps = Capabilities(supports_vision=False, supports_tools=False, source='declared')
        complete = False
        async for event in self.run(system='Reply briefly; do not call tools.',
                                    messages=[{'role': 'user', 'content': [text_block('Reply OK.')]}],
                                    tools=[], max_tokens=32, model=_MODEL, capabilities=caps):
            if isinstance(event, DoneDelta) and event.stop_reason == 'end_turn':
                complete = True
        if not complete:
            raise ProviderError('Codex verification did not complete', kind='transport')

    async def list_models(self) -> list[ModelInfo]:
        await self._verify()
        return [ModelInfo(id=_MODEL, display_name='GPT-6.1 Terra', supports_vision=True, supports_tools=True)]

    async def probe(self, model: str) -> Capabilities:
        if model != _MODEL:
            raise ProviderError('Codex model is not configured', kind='not_found')
        await self._verify()
        # Vision and function support are model-declared; the live text request
        # verifies the session and entitlement for the selected model.
        return Capabilities(supports_vision=True, supports_tools=True, source='declared')

    async def aclose(self) -> None:
        turns, self._turns = list(self._turns.values()), {}
        for state in turns:
            await state.ws.close()

    async def close_current(self) -> None:
        task = asyncio.current_task()
        if task is not None:
            state = self._turns.pop(task, None)
            if state is not None:
                await state.ws.close()

    async def _recv(self, state: _Turn, timeout: float = 90) -> dict[str, Any]:
        if state.backlog:
            return state.backlog.pop(0)
        raw = await asyncio.wait_for(state.ws.recv(), timeout=timeout)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ProviderError('invalid response from local Codex', kind='transport')
        return value

    async def _request(self, state: _Turn, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await state.ws.send(json.dumps({'id': request_id, 'method': method, 'params': params}))
        buffered: list[dict[str, Any]] = []
        try:
            while True:
                event = await self._recv(state)
                if event.get('id') == request_id and 'method' not in event:
                    if 'error' in event:
                        raise ProviderError('local Codex rejected '+method, kind='bad_request')
                    result = event.get('result')
                    if not isinstance(result, dict):
                        raise ProviderError('invalid local Codex response', kind='transport')
                    return result
                buffered.append(event)
        finally:
            state.backlog = buffered + state.backlog

    @staticmethod
    def _inputs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get('role') or 'user')
            content = message.get('content')
            blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else content
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = block.get('type')
                if kind == 'text':
                    items.append({'type': 'text', 'text': f'[{role}] {block.get("text", "")}', 'text_elements': []})
                elif kind == 'image' and block.get('data'):
                    media = str(block.get('media_type') or 'image/jpeg')
                    if media not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
                        raise ProviderError('unsupported image format for Codex', kind='bad_request')
                    items.append({'type': 'image', 'url': f'data:{media};base64,{block["data"]}'})
                elif kind == 'tool_result':
                    items.append({'type': 'text', 'text': '[tool result] '+str(block.get('content') or ''), 'text_elements': []})
        return items or [{'type': 'text', 'text': '(empty)', 'text_elements': []}]

    async def _start(self, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec], model: str) -> _Turn:
        if model != _MODEL:
            raise ProviderError('unsupported Codex model', kind='not_found')
        try:
            token = self.token_file.read_text(encoding='utf-8').strip()
            if not token:
                raise ValueError('empty local access token')
            ws = await websockets.connect(_URL, additional_headers={'Authorization': 'Bearer '+token},
                                          proxy=None, open_timeout=10, max_size=16*1024*1024)
        except Exception as exc:
            raise ProviderError('local Codex service is unavailable', kind='transport') from exc
        state = _Turn(ws=ws, thread_id='', allowed={spec.name for spec in tools})
        try:
            await self._request(state, 1, 'initialize', {'clientInfo': {'name': 'lv_copytrader', 'title': 'LV Copytrader', 'version': '1'}, 'capabilities': {'experimentalApi': True}})
            await ws.send(json.dumps({'method': 'initialized', 'params': {}}))
            definitions = [{'type': 'function', 'name': spec.name, 'description': spec.description,
                            'inputSchema': spec.input_schema} for spec in tools]
            opened = await self._request(state, 2, 'thread/start', {
                'model': model, 'cwd': '/tmp', 'ephemeral': True, 'environments': [],
                'approvalPolicy': 'never', 'sandbox': 'read-only',
                'baseInstructions': system, 'dynamicTools': definitions,
                'allowProviderModelFallback': False,
            })
            state.thread_id = str(opened['thread']['id'])
            await self._request(state, 3, 'turn/start', {
                'threadId': state.thread_id, 'model': model, 'effort': self.effort,
                'environments': [], 'input': self._inputs(messages),
            })
            return state
        except BaseException:
            await ws.close()
            raise

    async def run(self, *, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec],
                  max_tokens: int, model: str, capabilities: Capabilities) -> AsyncIterator[Any]:
        task = asyncio.current_task()
        if task is None:
            raise ProviderError('Codex requires an asyncio task', kind='transport')
        state = self._turns.get(task)
        if state is not None and not tools:
            await self.close_current()
            state = None
        if state is None:
            try:
                state = await self._start(system, messages, tools, model)
                self._turns[task] = state
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError('could not start local Codex', kind='transport') from exc
        elif state.pending_id is not None:
            result = None
            for block in (messages[-1].get('content') or []):
                if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('tool_use_id') == state.call_id:
                    result = block
                    break
            if result is None:
                await self.close_current()
                raise ProviderError('missing tool result for Codex', kind='bad_request')
            state.resumed_after_tool = True
            try:
                await state.ws.send(json.dumps({'id': state.pending_id, 'result': {
                    'contentItems': [{'type': 'inputText', 'text': str(result.get('content') or '')}],
                    'success': not bool(result.get('is_error')),
                }}))
            except Exception as exc:
                await self.close_current()
                raise ProviderError('Codex lost connection after a tool result; no automatic replay', kind='unknown') from exc
            state.pending_id = None
            state.call_id = ''
        chunks: list[str] = []
        try:
            while True:
                event = await self._recv(state)
                method = event.get('method')
                params = event.get('params') or {}
                if method == 'item/agentMessage/delta':
                    fragment = str(params.get('delta') or '')
                    if fragment:
                        chunks.append(fragment)
                        yield TextDelta(text=fragment)
                elif method == 'item/tool/call' and event.get('id') is not None:
                    name = str(params.get('tool') or '')
                    call_id = str(params.get('callId') or '')
                    args = params.get('arguments')
                    if name not in state.allowed or not call_id or call_id in state.seen_calls or not isinstance(args, dict):
                        await state.ws.send(json.dumps({'id': event['id'], 'error': {'code': -32602, 'message': 'Tool request denied'}}))
                        continue
                    state.seen_calls.add(call_id)
                    state.pending_id = event['id']
                    state.call_id = call_id
                    blocks = [text_block(''.join(chunks))] if chunks else []
                    blocks.append(tool_use_block(call_id, name, args))
                    yield ToolCallDone(id=call_id, name=name, input=args)
                    yield DoneDelta(stop_reason='tool_use', assistant_blocks=blocks)
                    return
                elif method and event.get('id') is not None:
                    # No terminal, approvals, filesystem, or additional tools.
                    await state.ws.send(json.dumps({'id': event['id'], 'error': {'code': -32601, 'message': 'Request denied'}}))
                elif method == 'turn/completed':
                    turn = params.get('turn') or {}
                    status = turn.get('status')
                    if status != 'completed':
                        raise _turn_failure(turn, after_tool=state.resumed_after_tool)
                    blocks = [text_block(''.join(chunks))] if chunks else []
                    await self.close_current()
                    yield DoneDelta(stop_reason='end_turn', assistant_blocks=blocks)
                    return
        except asyncio.CancelledError:
            await self.close_current()
            raise
        except ProviderError:
            await self.close_current()
            raise
        except Exception as exc:
            await self.close_current()
            raise ProviderError('local Codex connection failed', kind='unknown' if state.resumed_after_tool else 'transport') from exc
