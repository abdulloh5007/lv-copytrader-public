"""OpenAI-compatible provider: any endpoint that serves ``POST {base_url}/v1/chat/completions``.

Why Chat Completions and not ``/v1/responses``: Chat Completions is the endpoint *every* gateway
implements, it is not deprecated, and the two gateways this project is expected to meet most often —
OpenRouter and Ollama — do not serve ``/v1/responses`` at all. Targeting Responses would make the
"any OpenAI-compatible base URL" promise of BUILD_PROMPT §4.2 false for most of the field.

Everything an unusual gateway needs is operator-supplied: ``base_url``, ``api_key``, the model name,
and an **extra-headers map** that is merged into every request. That map is what lets a self-hosted
gateway (an internal proxy wanting ``X-Org-Id``, OpenRouter wanting ``HTTP-Referer`` / ``X-Title``,
a reverse proxy wanting a Cloudflare Access token) work with no code change at all — operator headers
are merged **last** and deliberately win over the defaults, including ``Authorization``.

Translation rules between the normalized blocks of :mod:`app.ai.base` and this wire format:

* a user turn with text and images becomes a ``content`` **parts** array; text-only turns stay a
  plain string, which is what the oldest gateways accept;
* images are **always** inlined as ``data:`` URLs — never an ``https`` URL. Ollama rejects remote
  URLs outright with ``image URLs are not currently supported`` and several other gateways silently
  drop them, so an https URL is never sent here under any circumstance;
* an assistant turn with tool calls becomes
  ``{"role": "assistant", "content": text or null, "tool_calls": [...]}``;
* ``thinking`` / ``redacted_thinking`` blocks have **no** Chat Completions equivalent, so they are
  dropped on the way out. They stay in the loop's normalized history because they are echoed back to
  Anthropic, where a missing or edited signature is a hard 400 (BUILD_PROMPT §4.1);
* ``tool_result`` blocks become separate ``{"role": "tool", "tool_call_id": ..., "content": ...}``
  messages, one per ``tool_use_id``, in order, emitted before the rest of that user turn so they sit
  directly behind the assistant message that called them.

``function.arguments`` arrives as a JSON **string** that is not guaranteed to be valid JSON — models
truncate it, double-encode it, or emit Python literals. Parsing happens inside a ``try`` and a failure
never kills the run: the call is reported as a :data:`PARSE_ERROR_TOOL_NAME` tool call whose input
carries the parse error and the raw fragment. That name is deliberately **not** a real tool, so the
agent loop answers it through its unknown-tool path with a ``tool`` message describing the problem and
the model simply calls the tool again with valid JSON. ``loop.py`` may special-case the name to render
a nicer message, but nothing breaks if it does not.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any, Final

import httpx

from app.ai.base import (
    Block,
    Capabilities,
    Delta,
    DoneDelta,
    ModelInfo,
    Msg,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallDone,
    ToolSpec,
    UsageDelta,
    normalize_base_url,
    text_block,
    thinking_block,
    tool_use_block,
)
from app.ai.tools import to_openai
from app.config import settings
from app import __version__
from app.errors import ProviderError
from app.util.images import data_url
from app.util.logging import get_logger, register_secret

log = get_logger(__name__)

#: Where an OpenAI-compatible endpoint lives when the operator leaves the base URL empty.
DEFAULT_BASE_URL: Final[str] = "https://api.openai.com"

#: Reported as the tool name when ``function.arguments`` will not parse. Not a real tool: the loop's
#: unknown-tool path turns it into a ``tool`` message and the model retries with valid JSON.
PARSE_ERROR_TOOL_NAME: Final[str] = "invalid_tool_arguments"

#: Chat Completions has no thinking switch to send — unlike Anthropic, where thinking must be turned
#: off explicitly, here "thinking disabled" simply means sending no reasoning parameter at all.
PROBE_MAX_TOKENS: Final[int] = 16

_ERROR_BODY_PREVIEW: Final[int] = 200
_ERROR_MESSAGE_LIMIT: Final[int] = 300
_RAW_ARGUMENTS_LIMIT: Final[int] = 400
_MAX_ADAPT_ATTEMPTS: Final[int] = 3
_CONNECT_TIMEOUT_CAP: Final[float] = 15.0

# finish_reason -> the normalized vocabulary. The normalized block format is Anthropic-shaped, so the
# stop reasons are too and the agent loop never has to know which vendor answered.
_STOP_REASONS: Final[dict[str, str]] = {
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "content_filter",
}

# A 400 that names one of these parameters is about the parameter, not about the model's abilities.
# Left: what we send. Right: what to send instead, or None to drop it.
_PARAM_ADAPTATIONS: Final[tuple[tuple[str, str | None], ...]] = (
    ("max_tokens", "max_completion_tokens"),
    ("stream_options", None),
)

_UNSUPPORTED_MARKERS: Final[tuple[str, ...]] = (
    "unsupported",
    "not supported",
    "unrecognized",
    "unknown",
    "unexpected",
    "extra_forbidden",
    "additional propert",
    "not permitted",
    "not allowed",
    "no longer supported",
)

_CONTEXT_WINDOW_MARKERS: Final[tuple[str, ...]] = (
    "context length",
    "context_length",
    "context window",
    "context_window",
    "maximum context",
    "too many tokens",
    "reduce the length of the messages",
    "prompt is too long",
)

_AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
_OVERLOADED_STATUSES: Final[frozenset[int]] = frozenset({500, 502, 503, 504, 529})
_BAD_REQUEST_STATUSES: Final[frozenset[int]] = frozenset({400, 422})

# SSE field lines that are not payload: "event: x", "id: x", "retry: 100".
_SSE_FIELD_RE: Final[re.Pattern[str]] = re.compile(r"^(event|id|retry)\s*:", re.IGNORECASE)

_PERMISSIVE_CAPABILITIES: Final[Capabilities] = Capabilities(
    supports_vision=True, supports_tools=True, thinking_form=None, source="assumed"
)

# A 1x1 transparent PNG, used only when app.ai.catalog cannot supply one (it is the owner of the
# canonical probe payload; this copy exists so probe() never depends on import order).
_FALLBACK_PROBE_PNG_B64: Final[str] = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mP4//8/AAX+Av4zEpUUAAAAAElFTkSuQmCC"
)

_FALLBACK_PROBE_MEDIA_TYPE: Final[str] = "image/png"

_FALLBACK_PROBE_PROMPT: Final[str] = "Reply with the single word: ok"

_FALLBACK_PROBE_TOOL: Final[ToolSpec] = ToolSpec(
    name="probe_tool",
    description="Capability probe. Never called for real work.",
    input_schema={
        "type": "object",
        "properties": {"ok": {"type": "boolean", "description": "Anything at all."}},
        "required": ["ok"],
        "additionalProperties": False,
    },
)


# --------------------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    """Trim a message to a length an operator will actually read, marking the cut."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…"


def _as_text(value: Any) -> str:
    """Pull plain text out of the several shapes gateways use for a streamed text field."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "reasoning", "summary", "thinking"):
            inner = value.get(key)
            if isinstance(inner, str) and inner:
                return inner
        return ""
    if isinstance(value, (list, tuple)):
        return "".join(_as_text(item) for item in value)
    return ""


def _as_json_content(value: Any) -> str:
    """Render a tool result as the string the ``tool`` role carries."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _bool_from(value: Any) -> bool | None:
    """Read a capability flag that a gateway may express as a bool or as ``{"supported": bool}``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        inner = value.get("supported")
        if isinstance(inner, bool):
            return inner
    return None


def _positive_int(value: Any) -> int | None:
    """Return a positive int from anything a gateway might put in a numeric field, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
    return None


def _extract_error_message(text: str) -> str:
    """Return the endpoint's own explanation, or the first 200 characters of a non-JSON body."""
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        # BUILD_PROMPT §4.3: a non-JSON body is reported as its first 200 characters, because an
        # HTML login page or an nginx error is exactly what the operator needs to see.
        return _clip(raw, _ERROR_BODY_PREVIEW)
    if isinstance(payload, str):
        return _clip(payload, _ERROR_MESSAGE_LIMIT)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str) and error.strip():
            return _clip(error, _ERROR_MESSAGE_LIMIT)
        if isinstance(error, dict):
            for key in ("message", "detail", "description", "type", "code"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return _clip(value, _ERROR_MESSAGE_LIMIT)
        for key in ("message", "detail", "error_message", "msg"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(value, _ERROR_MESSAGE_LIMIT)
            if isinstance(value, list) and value:
                return _clip(json.dumps(value, ensure_ascii=False, default=str), _ERROR_MESSAGE_LIMIT)
    return _clip(json.dumps(payload, ensure_ascii=False, default=str), _ERROR_MESSAGE_LIMIT)


def _kind_for_status(status: int, body_lower: str) -> str:
    """Map an HTTP status onto the ProviderError kinds the UI and to_http() branch on."""
    if status in _AUTH_STATUSES:
        return "auth"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limit"
    if status in _OVERLOADED_STATUSES:
        return "overloaded"
    if status in _BAD_REQUEST_STATUSES:
        if any(marker in body_lower for marker in _CONTEXT_WINDOW_MARKERS):
            return "context_window"
        return "bad_request"
    return "unknown"


def _capability_hints(entry: dict[str, Any]) -> tuple[bool | None, bool | None]:
    """Read real capability metadata off a model-list entry; None means "unknown, go and probe".

    ``architecture.input_modalities`` and ``supported_parameters`` are facts published by the
    gateway, not guesses, so when they are present they beat a probe — including when they say *no*.
    Nothing here ever looks at the model's *name*: every gateway renames models (BUILD_PROMPT §4.3).
    """
    vision: bool | None = None
    tools: bool | None = None

    architecture = entry.get("architecture")
    if isinstance(architecture, dict):
        modalities = architecture.get("input_modalities")
        if isinstance(modalities, (list, tuple)) and modalities:
            vision = any(
                isinstance(item, str) and item.strip().lower() == "image" for item in modalities
            )
        else:
            modality = architecture.get("modality")
            if isinstance(modality, str) and modality.strip():
                # Older OpenRouter entries carry "text+image->text" instead of a list.
                inputs = modality.split("->")[0].lower()
                vision = "image" in inputs

    supported = entry.get("supported_parameters")
    if isinstance(supported, (list, tuple)) and supported:
        names = {str(item).strip().lower() for item in supported}
        tools = bool(names & {"tools", "tool_choice", "functions", "function_call"})

    capabilities = entry.get("capabilities")
    if isinstance(capabilities, dict):
        if vision is None:
            for key in ("vision", "image_input", "images", "image"):
                flag = _bool_from(capabilities.get(key))
                if flag is not None:
                    vision = flag
                    break
        if tools is None:
            for key in ("tools", "tool_use", "function_calling", "functions"):
                flag = _bool_from(capabilities.get(key))
                if flag is not None:
                    tools = flag
                    break

    return vision, tools


def _context_length(entry: dict[str, Any]) -> int | None:
    """Find the context window in whichever field this gateway decided to use."""
    for key in ("context_length", "context_window", "max_context_length", "max_model_len"):
        value = _positive_int(entry.get(key))
        if value is not None:
            return value
    top_provider = entry.get("top_provider")
    if isinstance(top_provider, dict):
        for key in ("context_length", "max_completion_tokens"):
            value = _positive_int(top_provider.get(key))
            if value is not None:
                return value
    return None


def _normalize_model_entry(entry: Any) -> ModelInfo | None:
    """Turn one model-list entry into a ModelInfo, treating only ``id`` as guaranteed."""
    if isinstance(entry, str):
        model_id = entry.strip()
        if not model_id:
            return None
        return ModelInfo(id=model_id, display_name=model_id, raw={"id": model_id})
    if not isinstance(entry, dict):
        return None

    raw_id = entry.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        # Ollama's native listing keys the model by "name"/"model" instead of "id".
        for key in ("name", "model"):
            candidate = entry.get(key)
            if isinstance(candidate, str) and candidate.strip():
                raw_id = candidate
                break
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    model_id = raw_id.strip()

    display = model_id
    for key in ("name", "display_name", "title"):
        candidate = entry.get(key)
        if isinstance(candidate, str) and candidate.strip():
            display = candidate.strip()
            break

    vision, tools = _capability_hints(entry)
    return ModelInfo(
        id=model_id,
        display_name=display,
        supports_vision=vision,
        supports_tools=tools,
        context_length=_context_length(entry),
        raw=entry,
    )


# --------------------------------------------------------------------------------------
# Streaming state
# --------------------------------------------------------------------------------------


#: Markers a WAF challenge page carries. A gateway behind Cloudflare answers a non-browser
#: User-Agent with 403 and an HTML block page, which looks exactly like a rejected key unless
#: you read the body — and reading it is how hours get lost. Measured against a live gateway:
#: `python-httpx/0.28.1` -> 403, a full browser UA -> 200 with the model list.
_WAF_MARKERS: Final[tuple[str, ...]] = (
    "attention required",
    "sorry, you have been blocked",
    "cf-error-details",
    "cloudflare",
    "just a moment",
    "checking your browser",
    "__cf_chl",
)


def _looks_like_waf(body: str) -> bool:
    """Report whether a body is a WAF block page rather than an API error."""
    lowered = body[:4000].lower()
    return "<html" in lowered and any(marker in lowered for marker in _WAF_MARKERS)


def _waf_message(host: str) -> str:
    """Explain a WAF block in terms of the one setting that fixes it."""
    return (
        f"{host} did not answer the API — a web firewall in front of it blocked the request "
        f"before it arrived. This is not the API key. Gateways behind Cloudflare commonly "
        f"refuse any non-browser User-Agent; add one under the provider's extra headers, for "
        f'example  User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        f'(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
    )



class _ToolCallBuffer:
    """One entry of ``delta.tool_calls[]``, reassembled across chunks."""

    __slots__ = ("index", "id", "name", "fragments", "call_type", "skipped")

    def __init__(self, index: int) -> None:
        self.index: int = index
        self.id: str | None = None
        self.name: str | None = None
        self.fragments: list[str] = []
        self.call_type: str | None = None
        self.skipped: bool = False

    @property
    def arguments(self) -> str:
        """The concatenated raw ``function.arguments`` JSON string."""
        return "".join(self.fragments)


class _StreamState:
    """Accumulates one assistant turn out of the SSE chunks and produces normalized Deltas."""

    def __init__(self, provider: "OpenAICompatibleProvider") -> None:
        self._provider = provider
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._tool_calls: list[_ToolCallBuffer] = []
        self._by_id: dict[str, _ToolCallBuffer] = {}
        self._finish_reason: str = ""
        self._usage: UsageDelta | None = None
        self.chunks_seen: int = 0

    # -- ingestion ---------------------------------------------------------------------

    def feed(self, chunk: Any) -> list[Delta]:
        """Consume one decoded SSE payload and return the Deltas it produced."""
        if not isinstance(chunk, dict):
            return []
        self.chunks_seen += 1
        deltas: list[Delta] = []

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = _usage_delta(usage)

        choices = chunk.get("choices")
        if not isinstance(choices, (list, tuple)) or not choices:
            # A chunk with no choices is either the usage-only trailer or an in-band error. An
            # error arriving on an HTTP 200 stream is how OpenRouter reports a mid-stream failure.
            error = chunk.get("error")
            if error is not None:
                raise self._provider.error_from_stream(error)
            return deltas

        choice = choices[0]
        if not isinstance(choice, dict):
            return deltas

        # "delta" while streaming; "message" when a gateway answers a stream request with a whole
        # completion, which several self-hosted ones still do.
        payload = choice.get("delta")
        if not isinstance(payload, dict):
            payload = choice.get("message")
        if isinstance(payload, dict):
            deltas.extend(self._feed_payload(payload))

        finish = choice.get("finish_reason") or choice.get("stop_reason")
        if isinstance(finish, str) and finish.strip():
            self._finish_reason = finish.strip()
        return deltas

    def _feed_payload(self, payload: dict[str, Any]) -> list[Delta]:
        """Turn one ``delta`` / ``message`` object into Deltas."""
        deltas: list[Delta] = []

        # Reasoning: not in the OpenAI spec, but OpenRouter exposes `reasoning` and DeepSeek-style
        # servers expose `reasoning_content`. Whichever arrives feeds the dashboard's thinking panel.
        seen_reasoning: set[str] = set()
        for key in ("reasoning", "reasoning_content", "thinking"):
            fragment = _as_text(payload.get(key))
            if fragment and fragment not in seen_reasoning:
                seen_reasoning.add(fragment)
                self._thinking.append(fragment)
                deltas.append(ThinkingDelta(text=fragment))

        content = _as_text(payload.get("content"))
        if content:
            self._text.append(content)
            deltas.append(TextDelta(text=content))

        tool_calls = payload.get("tool_calls")
        if isinstance(tool_calls, (list, tuple)):
            for position, item in enumerate(tool_calls):
                delta = self._feed_tool_call(position, item)
                if delta is not None:
                    deltas.append(delta)
        else:
            legacy = payload.get("function_call")
            if isinstance(legacy, dict):
                delta = self._feed_tool_call(0, {"type": "function", "function": legacy})
                if delta is not None:
                    deltas.append(delta)
        return deltas

    def _feed_tool_call(self, position: int, item: Any) -> ToolCallDelta | None:
        """Merge one tool-call fragment into its buffer, keyed by index (or id, or position)."""
        if not isinstance(item, dict):
            return None
        buffer = self._buffer_for(position, item)

        call_type = item.get("type")
        if isinstance(call_type, str) and call_type.strip():
            buffer.call_type = call_type.strip()
            if buffer.call_type != "function":
                # A "custom" tool call now exists alongside "function"; anything that is not a
                # function call is skipped rather than guessed at.
                if not buffer.skipped:
                    log.warning(
                        "openai provider: skipping tool call of unsupported type type=%s index=%s",
                        buffer.call_type,
                        buffer.index,
                    )
                buffer.skipped = True

        call_id = item.get("id")
        if isinstance(call_id, str) and call_id.strip():
            buffer.id = call_id.strip()
            self._by_id.setdefault(buffer.id, buffer)

        function = item.get("function")
        fragment = ""
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name.strip():
                # Some gateways resend the whole name on every fragment; others send it once.
                buffer.name = name.strip()
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                fragment = arguments
            elif isinstance(arguments, dict):
                # A gateway that already parsed the arguments for us. Re-encode so the single
                # accumulate-then-parse path below stays the only one.
                fragment = json.dumps(arguments, ensure_ascii=False, default=str)
            if fragment:
                buffer.fragments.append(fragment)

        if buffer.skipped:
            return None
        return ToolCallDelta(
            index=buffer.index, id=buffer.id, name=buffer.name, partial_json=fragment
        )

    def _buffer_for(self, position: int, item: dict[str, Any]) -> _ToolCallBuffer:
        """Find or create the buffer this fragment belongs to."""
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            index = None
        if index is None:
            call_id = item.get("id")
            if isinstance(call_id, str) and call_id.strip() and call_id.strip() in self._by_id:
                return self._by_id[call_id.strip()]
            # No index and no known id: a gateway sending complete calls in array order.
            index = position if position < len(self._tool_calls) else len(self._tool_calls)
        for buffer in self._tool_calls:
            if buffer.index == index:
                return buffer
        buffer = _ToolCallBuffer(index)
        self._tool_calls.append(buffer)
        return buffer

    # -- completion --------------------------------------------------------------------

    def finish(self) -> list[Delta]:
        """Close the turn: one ToolCallDone per call, at most one UsageDelta, then the DoneDelta."""
        deltas: list[Delta] = []
        blocks: list[Block] = []

        thinking_text = "".join(self._thinking).strip()
        if thinking_text:
            # Kept in the normalized history for the dashboard and for history rows. It is dropped
            # again on the way back out to this endpoint: Chat Completions has nowhere to put it.
            blocks.append(thinking_block(thinking_text, None))

        text = "".join(self._text).strip()
        if text:
            blocks.append(text_block(text))

        for buffer in sorted(self._tool_calls, key=lambda b: b.index):
            if buffer.skipped:
                continue
            if not buffer.name and not buffer.arguments:
                continue
            done = self._tool_call_done(buffer)
            deltas.append(done)
            blocks.append(tool_use_block(done.id, done.name, done.input))

        if self._usage is not None:
            deltas.append(self._usage)

        has_tool_use = any(block.get("type") == "tool_use" for block in blocks)
        deltas.append(
            DoneDelta(stop_reason=self._stop_reason(has_tool_use), assistant_blocks=blocks)
        )
        return deltas

    def _tool_call_done(self, buffer: _ToolCallBuffer) -> ToolCallDone:
        """Parse one call's arguments, degrading to the parse-error sentinel instead of raising."""
        call_id = buffer.id or f"call_{buffer.index}_{uuid.uuid4().hex[:8]}"
        name = buffer.name or ""
        raw = buffer.arguments.strip()

        if not name:
            return _parse_error_call(
                call_id,
                name="(unnamed)",
                raw=raw,
                reason="the tool call arrived without a function name",
            )
        if not raw:
            # A tool with no required parameters legitimately streams empty arguments.
            return ToolCallDone(id=call_id, name=name, input={})
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            log.warning(
                "openai provider: tool arguments did not parse tool=%s bytes=%d error=%s",
                name,
                len(raw),
                exc,
            )
            return _parse_error_call(call_id, name=name, raw=raw, reason=str(exc))
        if isinstance(parsed, dict):
            return ToolCallDone(id=call_id, name=name, input=parsed)
        return _parse_error_call(
            call_id,
            name=name,
            raw=raw,
            reason=f"the arguments decoded to {type(parsed).__name__}, not a JSON object",
        )

    def _stop_reason(self, has_tool_use: bool) -> str:
        """Normalize finish_reason onto the vocabulary the agent loop reads."""
        if has_tool_use:
            # Some gateways report "stop" even when they emitted tool calls; the blocks are the
            # fact, so the loop is told to run the tools either way.
            return "tool_use"
        raw = self._finish_reason.strip().lower()
        if not raw:
            return "end_turn"
        return _STOP_REASONS.get(raw, raw)


def _usage_delta(usage: dict[str, Any]) -> UsageDelta:
    """Read token counts, accepting both the OpenAI and the Anthropic-ish field names."""
    input_tokens = 0
    for key in ("prompt_tokens", "input_tokens"):
        value = _positive_int(usage.get(key))
        if value is not None:
            input_tokens = value
            break
    output_tokens = 0
    for key in ("completion_tokens", "output_tokens"):
        value = _positive_int(usage.get(key))
        if value is not None:
            output_tokens = value
            break
    return UsageDelta(input_tokens=input_tokens, output_tokens=output_tokens)


def _parse_error_call(call_id: str, *, name: str, raw: str, reason: str) -> ToolCallDone:
    """Build the sentinel call that reports unparsable arguments back to the model."""
    message = (
        f"The arguments for `{name}` were not valid JSON ({reason}). "
        f"Call `{name}` again with a single valid JSON object as its arguments."
    )
    return ToolCallDone(
        id=call_id,
        name=PARSE_ERROR_TOOL_NAME,
        input={
            "tool": name,
            "error": reason,
            "message": message,
            "arguments_received": _clip(raw, _RAW_ARGUMENTS_LIMIT),
        },
    )


# --------------------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------------------


class OpenAICompatibleProvider:
    """Any endpoint speaking OpenAI Chat Completions: OpenAI, OpenRouter, Groq, Together, LiteLLM,
    vLLM, LM Studio, Ollama, or an operator's own gateway.

    Construct it through :func:`app.ai.base.build_provider`; nothing else instantiates it.
    """

    kind: str = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url: str = normalize_base_url(base_url, default=DEFAULT_BASE_URL)
        self.api_key: str = (api_key or "").strip()
        self.extra_headers: dict[str, str] = {
            str(name): str(value)
            for name, value in (extra_headers or {}).items()
            if value is not None and str(name).strip()
        }
        self._timeout: float = float(
            timeout if timeout and timeout > 0 else settings.PROVIDER_TIMEOUT_SECONDS
        )
        self._transport = transport
        self._chat_url: str = f"{self.base_url}/v1/chat/completions"
        self._models_url: str = f"{self.base_url}/v1/models"
        self._host: str = httpx.URL(self.base_url).host or self.base_url
        # Wire-shape adaptations discovered from the endpoint's own 400s, remembered per instance so
        # the round trip is paid once rather than on every request.
        self._max_tokens_field: str = "max_tokens"
        self._send_stream_options: bool = True
        if self.api_key:
            register_secret(self.api_key)
        for header_name, header_value in self.extra_headers.items():
            if any(
                marker in header_name.lower()
                for marker in ("key", "token", "authorization", "secret")
            ):
                register_secret(header_value)

    # -- plumbing ----------------------------------------------------------------------

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        """Default headers with the operator's extra-headers map merged in last, so it wins."""
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": accept,
            # Identify honestly rather than as the HTTP library. A gateway behind a WAF may
            # still refuse this — the operator's extra-headers map, merged last below, is the
            # documented place to set a browser User-Agent for that one endpoint, instead of
            # every LV Copytrader install claiming to be Chrome to every provider.
            "User-Agent": f"lv-copytrader/{__version__}",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def _client(self) -> httpx.AsyncClient:
        """Build a client per operation; the Provider protocol has no lifecycle to hang one on."""
        timeout = httpx.Timeout(
            self._timeout,
            connect=min(_CONNECT_TIMEOUT_CAP, self._timeout),
        )
        kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    # -- error construction ------------------------------------------------------------

    def error_from_response(self, status: int, body: str, *, operation: str, url: str) -> ProviderError:
        """Turn an HTTP failure into the sentence that names the real cause (BUILD_PROMPT §4.3)."""
        provider_message = _extract_error_message(body)
        lower = (body or "").lower()
        kind = _kind_for_status(status, lower)
        suffix = f": {provider_message}" if provider_message else ""

        # Checked BEFORE the auth branch: a WAF answers 403, which is otherwise read as a
        # rejected key, and the operator then rotates a key that was never the problem.
        if _looks_like_waf(body or ""):
            return ProviderError(
                _waf_message(self._host),
                status=status,
                kind="transport",
                detail={"operation": operation, "url": url, "blocked_by": "web firewall"},
            )

        if kind == "auth":
            if status == 403:
                message = f"key rejected — {self._host} answered 403 Forbidden{suffix}"
            else:
                message = f"key rejected by {self._host} (HTTP 401){suffix}"
        elif kind == "not_found":
            message = (
                f"endpoint or model not found — check base_url "
                f"(HTTP 404 from {url}){suffix}"
            )
        elif kind == "rate_limit":
            message = f"rate limited by {self._host} (HTTP 429){suffix}"
        elif kind == "overloaded":
            message = f"{self._host} is unavailable (HTTP {status}){suffix}"
        elif kind == "context_window":
            message = f"the request exceeds the model's context window{suffix}"
        elif kind == "bad_request":
            message = f"{self._host} rejected the request (HTTP {status}){suffix}"
        else:
            message = f"{self._host} returned HTTP {status}{suffix}"

        return ProviderError(
            message,
            status=status,
            kind=kind,
            detail={
                "operation": operation,
                "url": url,
                "provider_message": provider_message,
                "base_url": self.base_url,
            },
        )

    def error_from_stream(self, error: Any) -> ProviderError:
        """Turn an in-band ``error`` object on a 200 stream into a ProviderError."""
        body = json.dumps({"error": error}, ensure_ascii=False, default=str)
        status = 0
        if isinstance(error, dict):
            status = _positive_int(error.get("code")) or _positive_int(error.get("status")) or 0
            if status < 100 or status > 599:
                status = 0
        if status:
            return self.error_from_response(
                status, body, operation="chat completion", url=self._chat_url
            )
        message = _extract_error_message(body) or "the endpoint reported an error mid-stream"
        return ProviderError(
            f"{self._host} failed mid-stream: {message}",
            kind="unknown",
            detail={"operation": "chat completion", "url": self._chat_url},
        )

    def _transport_error(self, exc: Exception, operation: str, url: str) -> ProviderError:
        """Name the transport failure honestly: unreachable host, or a timeout with its budget."""
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            message = (
                f"host unreachable: no connection to {self._host} "
                f"({type(exc).__name__}: {_clip(str(exc), 120)})"
            )
        elif isinstance(exc, httpx.TimeoutException):
            message = (
                f"host unreachable in time: {self._host} did not respond within "
                f"{self._timeout:.0f}s ({operation})"
            )
        else:
            message = (
                f"host unreachable: the request to {self._host} failed "
                f"({type(exc).__name__}: {_clip(str(exc), 120)})"
            )
        return ProviderError(
            message,
            kind="transport",
            detail={"operation": operation, "url": url, "base_url": self.base_url},
        )

    def _decode_json(self, body: str, *, operation: str, url: str) -> Any:
        """Decode a JSON body, reporting the first 200 characters when it is not JSON at all."""
        try:
            return json.loads(body)
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                f"{self._host} answered {operation} with a body that is not JSON: "
                f"{_clip(body, _ERROR_BODY_PREVIEW)}",
                kind="decode",
                detail={"operation": operation, "url": url, "error": str(exc)},
            ) from exc

    # -- request-shape adaptation ------------------------------------------------------

    def _adapt_body(self, body: dict[str, Any], status: int, text: str) -> dict[str, Any] | None:
        """Rewrite the request when a 400 names one of our parameters as unsupported.

        Without this, a parameter the endpoint dislikes would be reported as a missing *capability*
        (``probe`` reads 400 as "unsupported") and every run against that endpoint would fail with a
        message about vision or tools that has nothing to do with the truth.
        """
        if status not in _BAD_REQUEST_STATUSES:
            return None
        lower = (text or "").lower()
        if not lower:
            return None
        for param, replacement in _PARAM_ADAPTATIONS:
            if param not in body or param not in lower:
                continue
            names_it = any(marker in lower for marker in _UNSUPPORTED_MARKERS)
            if replacement is not None and replacement in lower:
                names_it = True
            if not names_it:
                continue
            adapted = dict(body)
            value = adapted.pop(param)
            if replacement is not None:
                adapted[replacement] = value
                self._max_tokens_field = replacement
                log.warning(
                    "openai provider: %s sends %s instead of %s", self._host, replacement, param
                )
            else:
                if param == "stream_options":
                    self._send_stream_options = False
                log.warning("openai provider: %s does not accept %s — dropped", self._host, param)
            return adapted
        return None

    # -- body construction -------------------------------------------------------------

    def _chat_body(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        stream: bool,
    ) -> dict[str, Any]:
        """Assemble the Chat Completions request body."""
        body: dict[str, Any] = {"model": model, "messages": messages}
        body[self._max_tokens_field] = int(max_tokens)
        if tools:
            body["tools"] = to_openai(list(tools))
        if stream:
            body["stream"] = True
            if self._send_stream_options:
                # Streamed responses omit usage unless this is asked for; dropped automatically on
                # the first endpoint that says it does not know the field.
                body["stream_options"] = {"include_usage": True}
        return body

    def _convert_messages(
        self, system: str, messages: Sequence[Msg], *, supports_vision: bool
    ) -> list[dict[str, Any]]:
        """Convert normalized blocks into Chat Completions messages."""
        out: list[dict[str, Any]] = []
        system_text = (system or "").strip()
        if system_text:
            out.append({"role": "system", "content": system_text})
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            content = message.get("content")
            if isinstance(content, str):
                blocks: list[Block] = [text_block(content)]
            elif isinstance(content, (list, tuple)):
                blocks = [block for block in content if isinstance(block, dict)]
            else:
                blocks = []
            if role == "assistant":
                out.extend(self._assistant_messages(blocks))
            else:
                out.extend(self._user_messages(blocks, supports_vision=supports_vision))
        return out

    def _user_messages(
        self, blocks: Sequence[Block], *, supports_vision: bool
    ) -> list[dict[str, Any]]:
        """Render a user turn: its tool results first, then its text and images."""
        tool_messages: list[dict[str, Any]] = []
        parts: list[dict[str, Any]] = []
        texts: list[str] = []
        image_count = 0
        dropped_images = 0

        for block in blocks:
            kind = block.get("type")
            if kind == "tool_result":
                content = _as_json_content(block.get("content"))
                if block.get("is_error") and not content.lower().startswith("error"):
                    # The tool role carries no error flag, so the failure has to be in the text.
                    content = f"Error: {content}"
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or ""),
                        "content": content or "(no output)",
                    }
                )
            elif kind == "text":
                text = str(block.get("text") or "")
                if text.strip():
                    texts.append(text)
                    parts.append({"type": "text", "text": text})
            elif kind == "image":
                media_type = str(block.get("media_type") or "image/jpeg")
                payload = str(block.get("data") or "")
                if not payload:
                    continue
                if not supports_vision:
                    dropped_images += 1
                    continue
                # Always a data URL. Ollama answers an https URL with "image URLs are not currently
                # supported" and several gateways drop it silently, which is worse.
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": data_url(payload, media_type),
                            "detail": "auto",
                        },
                    }
                )
                image_count += 1
            # thinking / redacted_thinking never appear on a user turn; anything else is ignored.

        if dropped_images:
            note = (
                f"[{dropped_images} image(s) omitted: the selected model does not accept images]"
            )
            texts.append(note)
            parts.append({"type": "text", "text": note})

        messages: list[dict[str, Any]] = list(tool_messages)
        if parts:
            if image_count:
                messages.append({"role": "user", "content": parts})
            else:
                messages.append({"role": "user", "content": "\n\n".join(texts)})
        return messages

    def _assistant_messages(self, blocks: Sequence[Block]) -> list[dict[str, Any]]:
        """Render an assistant turn, dropping thinking blocks on the way out."""
        texts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                text = str(block.get("text") or "")
                if text.strip():
                    texts.append(text)
            elif kind == "tool_use":
                arguments = block.get("input")
                if not isinstance(arguments, dict):
                    arguments = {}
                tool_calls.append(
                    {
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(arguments, ensure_ascii=False, default=str),
                        },
                    }
                )
            # thinking / redacted_thinking: no Chat Completions equivalent. They stay in the loop's
            # normalized history because Anthropic requires them echoed back verbatim (§4.1); here
            # they are simply not sent.
        content = "\n\n".join(texts).strip()
        if not content and not tool_calls:
            return []
        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return [message]

    # -- Provider protocol -------------------------------------------------------------

    async def list_models(self) -> list[ModelInfo]:
        """``GET {base_url}/v1/models``, normalized defensively — only ``data[i].id`` is guaranteed."""
        url = self._models_url
        async with self._client() as client:
            try:
                response = await client.get(url, headers=self._headers())
            except httpx.HTTPError as exc:
                raise self._transport_error(exc, "list models", url) from exc
            body = response.text
            status = response.status_code
        if status >= 400:
            raise self.error_from_response(status, body, operation="list models", url=url)

        payload = self._decode_json(body, operation="list models", url=url)
        entries: Any = None
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict):
            for key in ("data", "models", "result", "items"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    entries = candidate
                    break
        if entries is None:
            # OpenRouter drops the top-level "object" field, so its absence is never an error — but a
            # body with no array at all genuinely is one.
            raise ProviderError(
                f"{self._host} returned a model list with no array of models: "
                f"{_clip(body, _ERROR_BODY_PREVIEW)}",
                kind="decode",
                detail={"operation": "list models", "url": url},
            )

        models: list[ModelInfo] = []
        seen: set[str] = set()
        for entry in entries:
            info = _normalize_model_entry(entry)
            if info is None or info.id in seen:
                continue
            seen.add(info.id)
            models.append(info)
        return models

    async def probe(
        self,
        model: str,
        *,
        png_b64: str | None = None,
        media_type: str | None = None,
        prompt: str | None = None,
        tool: ToolSpec | None = None,
    ) -> Capabilities:
        """Find out whether this model really accepts images and tools on this endpoint.

        One throwaway request with ``max_tokens: 16`` — **not 1**, which 400s on every
        thinking-capable model regardless of vision or tools and would report the models the operator
        most wants as unsupported. When the combined request fails, the capabilities are attributed
        with follow-up requests rather than guessed at, so a broken model name never masquerades as
        "no vision, no tools".
        """
        payload = _probe_payload(png_b64, media_type, prompt, tool)
        probe_png, probe_media, probe_prompt, probe_tool = payload
        async with self._client() as client:
            if await self._probe_once(
                client,
                model,
                image=probe_png,
                media_type=probe_media,
                prompt=probe_prompt,
                tool=probe_tool,
            ):
                return Capabilities(
                    supports_vision=True,
                    supports_tools=True,
                    thinking_form=None,
                    source="probed",
                )
            # The combined request failed: establish that plain text works before blaming a
            # capability, then attribute the failure to vision, tools, or both.
            if not await self._probe_once(
                client,
                model,
                image=None,
                media_type=probe_media,
                prompt=probe_prompt,
                tool=None,
                strict=True,
            ):
                # _probe_once(strict=True) raises on failure, so this is unreachable in practice.
                raise ProviderError(
                    f"{self._host} rejected a minimal request for model {model!r}",
                    kind="bad_request",
                    detail={"operation": "probe", "model": model},
                )
            vision = await self._probe_once(
                client,
                model,
                image=probe_png,
                media_type=probe_media,
                prompt=probe_prompt,
                tool=None,
            )
            tools = await self._probe_once(
                client,
                model,
                image=None,
                media_type=probe_media,
                prompt=probe_prompt,
                tool=probe_tool,
            )
        return Capabilities(
            supports_vision=vision,
            supports_tools=tools,
            thinking_form=None,
            source="probed",
        )

    async def _probe_once(
        self,
        client: httpx.AsyncClient,
        model: str,
        *,
        image: str | None,
        media_type: str,
        prompt: str,
        tool: ToolSpec | None,
        strict: bool = False,
    ) -> bool:
        """Send one probe request. True on 2xx, False on 400/422, raise on anything else.

        A 400/422 is a statement about the payload — that is the capability answer. A 401, a 404 or a
        dead host is not: those are reported as themselves so the operator sees "key rejected"
        instead of "no vision". With ``strict`` even a 400 is raised, which is how the caller
        distinguishes a model that cannot do images from a model that does not exist.
        """
        parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if image:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url(image, media_type), "detail": "auto"},
                }
            )
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": parts if image else prompt}
        ]
        body = self._chat_body(
            model=model,
            messages=messages,
            tools=[tool] if tool else [],
            max_tokens=PROBE_MAX_TOKENS,
            stream=False,
        )

        status = 0
        text = ""
        for _ in range(_MAX_ADAPT_ATTEMPTS):
            try:
                response = await client.post(self._chat_url, json=body, headers=self._headers())
            except httpx.HTTPError as exc:
                raise self._transport_error(exc, "probe", self._chat_url) from exc
            status = response.status_code
            text = response.text
            if status < 400:
                return True
            adapted = self._adapt_body(body, status, text)
            if adapted is None:
                break
            body = adapted

        error = self.error_from_response(status, text, operation="probe", url=self._chat_url)
        if status in _BAD_REQUEST_STATUSES and not strict:
            log.info(
                "openai provider: probe says unsupported model=%s image=%s tools=%s status=%s",
                model,
                bool(image),
                bool(tool),
                status,
            )
            return False
        raise error

    def run(
        self,
        *,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec],
        max_tokens: int,
        model: str,
        capabilities: Capabilities,
    ) -> AsyncIterator[Delta]:
        """Stream one assistant turn. Returns the async iterator; nothing is sent until it is read."""
        return self._run(
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            model=model,
            capabilities=capabilities,
        )

    async def _run(
        self,
        *,
        system: str,
        messages: list[Msg],
        tools: list[ToolSpec],
        max_tokens: int,
        model: str,
        capabilities: Capabilities,
    ) -> AsyncIterator[Delta]:
        """Drive one streamed chat completion and yield normalized Deltas."""
        caps = capabilities or _PERMISSIVE_CAPABILITIES
        wire_messages = self._convert_messages(
            system, messages, supports_vision=bool(caps.supports_vision)
        )
        wire_tools: Sequence[ToolSpec] = tools if (tools and caps.supports_tools) else []
        body = self._chat_body(
            model=model,
            messages=wire_messages,
            tools=wire_tools,
            max_tokens=max_tokens,
            stream=True,
        )

        async with self._client() as client:
            for attempt in range(_MAX_ADAPT_ATTEMPTS):
                retry_body: dict[str, Any] | None = None
                try:
                    async with client.stream(
                        "POST",
                        self._chat_url,
                        json=body,
                        headers=self._headers(accept="text/event-stream"),
                    ) as response:
                        if response.status_code >= 400:
                            raw = await response.aread()
                            text = raw.decode("utf-8", "replace")
                            adapted = self._adapt_body(body, response.status_code, text)
                            if adapted is not None and attempt + 1 < _MAX_ADAPT_ATTEMPTS:
                                retry_body = adapted
                            else:
                                raise self.error_from_response(
                                    response.status_code,
                                    text,
                                    operation="chat completion",
                                    url=self._chat_url,
                                )
                        else:
                            async for delta in self._consume(response):
                                yield delta
                            return
                except httpx.HTTPError as exc:
                    raise self._transport_error(exc, "chat completion", self._chat_url) from exc
                if retry_body is None:
                    return
                body = retry_body

    async def _consume(self, response: httpx.Response) -> AsyncIterator[Delta]:
        """Parse the SSE body — or a whole JSON completion, when the endpoint ignored ``stream``."""
        state = _StreamState(self)
        content_type = (response.headers.get("content-type") or "").lower()

        if "event-stream" not in content_type and (
            "application/json" in content_type or "text/json" in content_type
        ):
            # Several self-hosted gateways answer a stream request with one complete JSON body.
            raw = await response.aread()
            text = raw.decode("utf-8", "replace")
            payload = self._decode_json(text, operation="chat completion", url=self._chat_url)
            for delta in state.feed(payload):
                yield delta
            for delta in state.finish():
                yield delta
            return

        undecodable = 0
        first_undecodable = ""
        data_lines: list[str] = []
        done = False

        async def flush(payload: str) -> list[Delta]:
            """Decode one SSE event's data and feed it to the accumulator."""
            nonlocal done, undecodable, first_undecodable
            text = payload.strip()
            if not text:
                return []
            if text == "[DONE]":
                done = True
                return []
            try:
                chunk = json.loads(text)
            except (TypeError, ValueError):
                undecodable += 1
                if not first_undecodable:
                    first_undecodable = text
                log.warning(
                    "openai provider: skipping an SSE payload that is not JSON: %s",
                    _clip(text, 120),
                )
                return []
            return state.feed(chunk)

        try:
            async for line in response.aiter_lines():
                line = line.rstrip("\r")
                if not line.strip():
                    if data_lines:
                        for delta in await flush("\n".join(data_lines)):
                            yield delta
                        data_lines = []
                    if done:
                        break
                    continue
                if line.startswith(":"):
                    continue  # An SSE comment: OpenRouter sends these as keep-alives.
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                if _SSE_FIELD_RE.match(line):
                    continue
                stripped = line.strip()
                if stripped.startswith("{"):
                    # A gateway emitting bare JSON lines instead of SSE frames.
                    for delta in await flush(stripped):
                        yield delta
        except httpx.HTTPError as exc:
            raise self._transport_error(exc, "chat completion", self._chat_url) from exc

        if data_lines and not done:
            for delta in await flush("\n".join(data_lines)):
                yield delta

        if state.chunks_seen == 0:
            if undecodable:
                raise ProviderError(
                    f"{self._host} streamed a body that is not server-sent events: "
                    f"{_clip(first_undecodable, _ERROR_BODY_PREVIEW)}",
                    kind="decode",
                    detail={"operation": "chat completion", "url": self._chat_url},
                )
            raise ProviderError(
                f"{self._host} returned an empty stream — no completion chunks arrived",
                kind="decode",
                detail={"operation": "chat completion", "url": self._chat_url},
            )

        for delta in state.finish():
            yield delta


def _probe_payload(
    png_b64: str | None,
    media_type: str | None,
    prompt: str | None,
    tool: ToolSpec | None,
) -> tuple[str, str, str, ToolSpec]:
    """Resolve the probe fixtures, preferring the canonical ones in ``app.ai.catalog``.

    The catalog owns them, and it imports this module's factory in turn, so the import happens here
    inside the call rather than at module level, where it would be a cycle. The local fallbacks keep
    ``probe()`` usable on its own — for instance from a test that never touches the database.
    """
    image = png_b64
    image_media = media_type
    text = prompt
    probe_tool = tool
    if image is None or image_media is None or text is None or probe_tool is None:
        try:
            from app.ai import catalog
        except ImportError:  # pragma: no cover - only if catalog is unavailable
            log.warning("openai provider: app.ai.catalog unavailable, probing with local fixtures")
        else:
            if image is None:
                catalog_png = getattr(catalog, "PROBE_PNG_B64", None)
                if isinstance(catalog_png, str) and catalog_png:
                    image = catalog_png
            if image_media is None:
                catalog_media = getattr(catalog, "PROBE_MEDIA_TYPE", None)
                if isinstance(catalog_media, str) and catalog_media:
                    image_media = catalog_media
            if text is None:
                catalog_prompt = getattr(catalog, "PROBE_PROMPT", None)
                if isinstance(catalog_prompt, str) and catalog_prompt:
                    text = catalog_prompt
            if probe_tool is None:
                catalog_tool = getattr(catalog, "PROBE_TOOL", None)
                if isinstance(catalog_tool, ToolSpec):
                    probe_tool = catalog_tool
    return (
        image or _FALLBACK_PROBE_PNG_B64,
        image_media or _FALLBACK_PROBE_MEDIA_TYPE,
        text or _FALLBACK_PROBE_PROMPT,
        probe_tool or _FALLBACK_PROBE_TOOL,
    )
