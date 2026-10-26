"""Anthropic Messages API provider — streaming turns, model listing, declared capabilities.

Talks to ``POST {base_url}/v1/messages`` over ``httpx`` directly rather than through the ``anthropic``
SDK, because the agent loop needs one streaming iterator shape shared with the OpenAI-compatible
provider and because ``base_url`` is operator-overridable (an Anthropic-compatible gateway is a
legitimate target).

Three current API facts drive most of the code here (BUILD_PROMPT §4.1); each is commented again at
the line that honours it:

1. ``temperature`` / ``top_p`` / ``top_k`` are never sent. On the current Opus/Sonnet/Fable
   generation they are a hard 400 on every request, thinking or not, so this module has no knob for
   them and never reads one from settings.
2. Thinking is configured as ``{"type": "adaptive", "display": "summarized"}``. ``display`` defaults
   to ``"omitted"``, which streams ``thinking`` blocks whose text is empty — the dashboard's
   reasoning panel would render blank while the model was in fact thinking. The older
   ``{"type": "enabled", "budget_tokens": N}`` form is a 400 on current models and is used **only**
   as the fallback when a model rejects ``adaptive``; which form a model takes is decided once by
   :meth:`AnthropicProvider.probe` and travels in ``Capabilities.thinking_form``.
3. ``thinking`` / ``redacted_thinking`` blocks are echoed back byte-for-byte, in their original
   order, alongside the ``tool_use`` block of the same assistant turn, each with its ``signature``.
   Editing, reordering or dropping one is a 400. That is why :class:`DoneDelta` carries the whole
   reassembled turn: the loop appends exactly what came out of the stream.

Two more traps this module owns:

* ``stop_reason == "model_context_window_exceeded"`` arrives on an HTTP **200**, not on an error
  status. It is raised as ``ProviderError(kind="context_window")`` so the loop can trim and surface
  it instead of treating a truncated turn as a normal one.
* ``GET /v1/models`` is **not** the OpenAI envelope: there is no ``object: "list"``. It is
  ``{"data": [...], "has_more": bool, "first_id": ..., "last_id": ...}`` paged with
  ``limit``/``after_id``, and every entry carries a real ``capabilities`` object — so capability is
  read, not guessed and not probed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

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
    redacted_thinking_block,
    text_block,
    thinking_block,
    tool_use_block,
)
from app.ai.tools import to_anthropic
from app.config import PROVIDER_TIMEOUT_SECONDS
from app import __version__
from app.errors import ProviderError
from app.util.logging import get_logger, register_secret

log = get_logger(__name__)


# --------------------------------------------------------------------------------------
# Wire constants
# --------------------------------------------------------------------------------------

DEFAULT_BASE_URL: str = "https://api.anthropic.com"

_ANTHROPIC_VERSION: str = "2023-06-01"
_MESSAGES_PATH: str = "/v1/messages"
_MODELS_PATH: str = "/v1/models"

# /v1/models pages with limit/after_id and reports has_more; the page cap is a loop guard, not a
# limit on how many models we will report.
_MODELS_PAGE_LIMIT: int = 100
_MODELS_MAX_PAGES: int = 50

_CONNECT_TIMEOUT_SECONDS: float = 15.0
_MODELS_TIMEOUT_SECONDS: float = 30.0

# §4.3: the throwaway probe request uses max_tokens 16. `max_tokens: 1` is wrong — it 400s on every
# thinking-capable model regardless of vision or tools, so the probe would report "unsupported" for
# exactly the models worth using.
_PROBE_MAX_TOKENS: int = 16

# The one permitted use of the deprecated `enabled` thinking form, and only when `adaptive` 400s.
_FALLBACK_THINKING_BUDGET: int = 4096
_MIN_THINKING_BUDGET: int = 1024
# `enabled` requires max_tokens > budget_tokens; this is the headroom left for the visible answer.
_MIN_ANSWER_TOKENS: int = 512

_CONTEXT_WINDOW_STOP: str = "model_context_window_exceeded"

_ERROR_BODY_CHARS: int = 200

# A 1x1 white PNG, used by the live capability probe only. Kept here rather than imported from
# app.ai.catalog so the provider has no dependency on the module that caches its verdicts.
_PROBE_PNG_B64: str = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mP4//8/AAX+Av4zEpUUAAAAAElFTkSuQmCC"
)
_PROBE_PNG_MEDIA_TYPE: str = "image/png"

# One parameter, so a model that supports tools at all accepts it.
_PROBE_TOOL: ToolSpec = ToolSpec(
    name="signalpilot_probe",
    description="Capability probe. Never call this tool.",
    input_schema={
        "type": "object",
        "properties": {"ok": {"type": "string", "description": "Ignored."}},
        "required": [],
        "additionalProperties": False,
    },
)

# Error bodies are {"type":"error","error":{"type":...,"message":...}}. The error *type* is the
# better signal, so it is consulted before the status code.
_ERROR_TYPE_KINDS: dict[str, str] = {
    "authentication_error": "auth",
    "permission_error": "auth",
    "billing_error": "auth",
    "not_found_error": "not_found",
    "rate_limit_error": "rate_limit",
    "overloaded_error": "overloaded",
    "invalid_request_error": "bad_request",
    "request_too_large": "bad_request",
    "timeout_error": "transport",
    "api_error": "unknown",
}

# 529 is Anthropic's overloaded status — NOT 503. A 503 here is a proxy or gateway in front of the
# API, which is a transport failure from the operator's point of view.
_STATUS_KINDS: dict[int, str] = {
    400: "bad_request",
    401: "auth",
    403: "auth",
    404: "not_found",
    413: "bad_request",
    422: "bad_request",
    429: "rate_limit",
    500: "unknown",
    502: "transport",
    503: "transport",
    504: "transport",
    529: "overloaded",
}

_KIND_LEADS: dict[str, str] = {
    "auth": "key rejected",
    "not_found": "endpoint or model not found (check base_url and the model id)",
    "rate_limit": "rate limited",
    "overloaded": "the API is overloaded",
    "bad_request": "the API rejected the request",
    "transport": "host unreachable",
    "decode": "unreadable response",
    "context_window": "context window exceeded",
    "unknown": "the API call failed",
}


# --------------------------------------------------------------------------------------
# Small helpers over the declared capability tree
# --------------------------------------------------------------------------------------


def _supported(node: Any) -> bool:
    """Read a ``{"supported": bool}`` leaf of the declared capability tree."""
    if isinstance(node, dict):
        return bool(node.get("supported"))
    if isinstance(node, bool):
        return node
    return False


def _declared_vision(capabilities: dict[str, Any]) -> bool | None:
    """Return declared image support, or None when the entry does not declare it."""
    node = capabilities.get("image_input")
    if isinstance(node, dict) and "supported" in node:
        return bool(node["supported"])
    if isinstance(node, bool):
        return node
    return None


def _declared_thinking_form(capabilities: dict[str, Any]) -> str | None:
    """Return the thinking form a model declares: adaptive first, then the enabled fallback."""
    node = capabilities.get("thinking")
    if not isinstance(node, dict):
        return None
    types = node.get("types") if isinstance(node.get("types"), dict) else {}
    # "adaptive first" — the declared tree answers the question a live probe would ask.
    if _supported(types.get("adaptive")):
        return "adaptive"
    if _supported(types.get("enabled")):
        return "enabled"
    if not types and node.get("supported"):
        return "adaptive"
    return None


def _declared_context_length(entry: dict[str, Any]) -> int | None:
    """Read the context window from whichever field this endpoint spells it with."""
    for key in ("max_input_tokens", "context_window", "context_length"):
        value = entry.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            number = int(value.strip())
            if number > 0:
                return number
    return None


def _model_info(entry: Any) -> ModelInfo | None:
    """Normalize one ``/v1/models`` entry, or None when it carries no usable id."""
    if not isinstance(entry, dict):
        return None
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    model_id = model_id.strip()
    capabilities = entry.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    display = entry.get("display_name")
    return ModelInfo(
        id=model_id,
        display_name=str(display).strip() if isinstance(display, str) and display.strip() else model_id,
        supports_vision=_declared_vision(capabilities),
        # Tool use is universally supported on Claude models: every model served by /v1/messages
        # accepts the `tools` parameter, so this is a fact rather than a guess.
        supports_tools=True,
        context_length=_declared_context_length(entry),
        raw=entry,
    )


# --------------------------------------------------------------------------------------
# Streaming accumulator
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



class _StreamState:
    """Everything one streamed turn accumulates, keyed by content-block index."""

    __slots__ = ("blocks", "order", "model", "stop_reason", "input_tokens", "output_tokens", "saw_stop")

    def __init__(self, model: str) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self.order: list[int] = []
        self.model: str = model
        self.stop_reason: str | None = None
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.saw_stop: bool = False

    def open(self, index: int, record: dict[str, Any]) -> dict[str, Any]:
        """Register a content block at its index, keeping first-seen order."""
        if index not in self.blocks:
            self.order.append(index)
        self.blocks[index] = record
        return record


def _new_record(block_type: str, content_block: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the working record for one content block type."""
    source = content_block or {}
    if block_type == "text":
        return {"type": "text", "text": str(source.get("text") or "")}
    if block_type == "thinking":
        signature = source.get("signature")
        return {
            "type": "thinking",
            "thinking": str(source.get("thinking") or ""),
            "signature": str(signature) if signature else "",
        }
    if block_type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": str(source.get("data") or "")}
    if block_type == "tool_use":
        raw_input = source.get("input")
        return {
            "type": "tool_use",
            "id": str(source.get("id") or ""),
            "name": str(source.get("name") or ""),
            "input": dict(raw_input) if isinstance(raw_input, dict) else {},
            "json": "",
        }
    return {"type": "unknown", "raw": dict(source)}


_IMPLIED_BLOCK_TYPES: dict[str, str] = {
    "text_delta": "text",
    "thinking_delta": "thinking",
    "signature_delta": "thinking",
    "input_json_delta": "tool_use",
}


# --------------------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------------------


class AnthropicProvider:
    """The Anthropic implementation of the ``Provider`` protocol from ``app.ai.base``."""

    kind: str = "anthropic"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url: str = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._api_key: str = api_key or ""
        self._extra_headers: dict[str, str] = {
            str(name): str(value)
            for name, value in (extra_headers or {}).items()
            if value is not None and str(name).strip()
        }
        # So the key can never survive a log line, whatever formats it.
        register_secret(self._api_key)

    def __repr__(self) -> str:
        """Return a debug form that never carries the API key."""
        return f"AnthropicProvider(base_url={self._base_url!r}, has_key={bool(self._api_key)})"

    # ---------------------------------------------------------------- transport plumbing

    def _url(self, path: str) -> str:
        """Join the normalized base URL with an API path."""
        return f"{self._base_url}{path}"

    def _headers(self, *, stream: bool) -> dict[str, str]:
        """Build request headers; operator extras may override everything but the key's presence."""
        headers: dict[str, str] = {
            "User-Agent": f"lv-copytrader/{__version__}",
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
        }
        headers.update(self._extra_headers)
        if not any(name.lower() == "x-api-key" for name in headers):
            headers["x-api-key"] = self._api_key
        return headers

    @staticmethod
    def _timeout(seconds: float) -> httpx.Timeout:
        """Build the timeout used for one call: a short connect, a long read."""
        return httpx.Timeout(seconds, connect=_CONNECT_TIMEOUT_SECONDS)

    def _http_error(self, status: int | None, raw: Any) -> ProviderError:
        """Turn an error status and its body into a ProviderError carrying the server's message."""
        if isinstance(raw, (bytes, bytearray)):
            text = bytes(raw).decode("utf-8", "replace")
        elif isinstance(raw, str):
            text = raw
        else:
            text = ""
        body: Any = None
        if isinstance(raw, dict):
            body = raw
        elif text.strip():
            try:
                body = json.loads(text)
            except ValueError:
                body = None
        # Before anything else: a WAF in front of the endpoint answers 403 with an HTML block
        # page, which the branches below would report as a rejected key — sending the operator
        # to rotate a key that was never the problem.
        if _looks_like_waf(text or ""):
            return ProviderError(
                _waf_message(self._base_url),
                status=status,
                kind="transport",
                detail={"provider": self.kind, "base_url": self._base_url,
                        "blocked_by": "web firewall"},
            )

        error_type = ""
        message = ""
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error_obj = body["error"]
            error_type = str(error_obj.get("type") or "").strip()
            message = str(error_obj.get("message") or "").strip()
        elif isinstance(body, dict) and isinstance(body.get("message"), str):
            message = body["message"].strip()
        if not message:
            # A non-JSON body (an HTML error page from a proxy) reports its first characters.
            message = text.strip()[:_ERROR_BODY_CHARS]
        kind = _ERROR_TYPE_KINDS.get(error_type) or _STATUS_KINDS.get(status or 0) or "unknown"
        if kind == "transport" and status is not None:
            # A 502/503/504 came from a proxy in front of the API, not from a dead socket.
            lead = f"the endpoint failed before the API answered (HTTP {status})"
        else:
            lead = _KIND_LEADS.get(kind, _KIND_LEADS["unknown"])
        sentence = f"{lead} — {message}" if message else lead
        detail: dict[str, Any] = {"provider": self.kind, "base_url": self._base_url}
        if error_type:
            detail["error_type"] = error_type
        if message:
            detail["server_message"] = message
        return ProviderError(sentence, status=status, kind=kind, detail=detail)

    def _transport_error(self, exc: Exception, *, timeout: float) -> ProviderError:
        """Wrap an httpx transport failure with a cause the operator can act on."""
        if isinstance(exc, httpx.TimeoutException):
            return ProviderError(
                f"the API did not respond within {timeout:g}s",
                kind="transport",
                detail={"provider": self.kind, "base_url": self._base_url, "timeout": timeout},
            )
        return ProviderError(
            f"host unreachable: {exc}",
            kind="transport",
            detail={"provider": self.kind, "base_url": self._base_url},
        )

    def _decode_json(self, raw: bytes, *, status: int) -> dict[str, Any]:
        """Parse a 2xx body, reporting the first characters of anything that is not JSON."""
        text = raw.decode("utf-8", "replace")
        try:
            parsed = json.loads(text) if text.strip() else None
        except ValueError as exc:
            raise ProviderError(
                f"{_KIND_LEADS['decode']} — {text.strip()[:_ERROR_BODY_CHARS]}",
                status=status,
                kind="decode",
                detail={"provider": self.kind, "base_url": self._base_url},
            ) from exc
        if not isinstance(parsed, dict):
            raise ProviderError(
                f"{_KIND_LEADS['decode']} — expected a JSON object, got {type(parsed).__name__}",
                status=status,
                kind="decode",
                detail={"provider": self.kind, "base_url": self._base_url},
            )
        return parsed

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET one JSON document from the API."""
        timeout = min(_MODELS_TIMEOUT_SECONDS, float(PROVIDER_TIMEOUT_SECONDS))
        try:
            async with httpx.AsyncClient(timeout=self._timeout(timeout)) as client:
                response = await client.get(
                    self._url(path), params=params, headers=self._headers(stream=False)
                )
        except httpx.RequestError as exc:
            raise self._transport_error(exc, timeout=timeout) from exc
        if response.status_code >= 400:
            raise self._http_error(response.status_code, response.content)
        return self._decode_json(response.content, status=response.status_code)

    async def _post_json(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST one non-streaming Messages request and return the parsed reply."""
        timeout = float(PROVIDER_TIMEOUT_SECONDS)
        try:
            async with httpx.AsyncClient(timeout=self._timeout(timeout)) as client:
                response = await client.post(
                    self._url(_MESSAGES_PATH), json=body, headers=self._headers(stream=False)
                )
        except httpx.RequestError as exc:
            raise self._transport_error(exc, timeout=timeout) from exc
        if response.status_code >= 400:
            raise self._http_error(response.status_code, response.content)
        return self._decode_json(response.content, status=response.status_code)

    # ---------------------------------------------------------------- request construction

    def _thinking_config(self, thinking_form: str | None, max_tokens: int) -> tuple[dict[str, Any] | None, int]:
        """Return the thinking block for the request body and the max_tokens it requires.

        ``display: "summarized"`` is mandatory: it defaults to ``"omitted"``, which returns thinking
        blocks whose text is empty and leaves the dashboard's reasoning panel blank. ``enabled`` is
        the fallback form only — ``probe`` selects it exclusively for a model that 400s on
        ``adaptive`` — and it additionally requires ``max_tokens > budget_tokens``.
        """
        form = (thinking_form or "").strip().lower()
        if form == "enabled":
            budget = _FALLBACK_THINKING_BUDGET
            if max_tokens <= budget:
                budget = max(_MIN_THINKING_BUDGET, max_tokens // 2)
            if max_tokens <= budget:
                # The API demands headroom above the budget; raising the cap is the only legal fix.
                max_tokens = budget + _MIN_ANSWER_TOKENS
            return {"type": "enabled", "budget_tokens": budget}, max_tokens
        if form in ("", "none", "off", "disabled"):
            # No form: `probe` returns "adaptive" for every model that thinks at all, so an empty
            # form means this model has no extended thinking and any `thinking` block would 400.
            return None, max_tokens
        # "adaptive", and anything unrecognized — adaptive is the current-generation form and the
        # one that is always tried first.
        return {"type": "adaptive", "display": "summarized"}, max_tokens

    @staticmethod
    def _convert_block(block: Block) -> dict[str, Any] | None:
        """Convert one normalized block into its Anthropic shape, or None when it must be dropped."""
        source = block or {}
        block_type = str(source.get("type") or "")
        if block_type == "text":
            text = str(source.get("text") or "")
            # An empty text block is a 400 on the way back in, so it is dropped rather than sent.
            return {"type": "text", "text": text} if text.strip() else None
        if block_type == "image":
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": str(source.get("media_type") or "image/jpeg"),
                    "data": str(source.get("data") or ""),
                },
            }
        if block_type == "thinking":
            # Verbatim, signature included: the server signed this text and validates it on the way
            # back. Nothing here rewrites, trims or reorders it.
            out: dict[str, Any] = {"type": "thinking", "thinking": str(source.get("thinking") or "")}
            signature = source.get("signature")
            if signature:
                out["signature"] = str(signature)
            return out
        if block_type == "redacted_thinking":
            return {"type": "redacted_thinking", "data": str(source.get("data") or "")}
        if block_type == "tool_use":
            tool_input = source.get("input")
            return {
                "type": "tool_use",
                "id": str(source.get("id") or ""),
                "name": str(source.get("name") or ""),
                "input": dict(tool_input) if isinstance(tool_input, dict) else {},
            }
        if block_type == "tool_result":
            content = source.get("content")
            if isinstance(content, str):
                text = content
            else:
                text = json.dumps(content, ensure_ascii=False, default=str)
            return {
                "type": "tool_result",
                "tool_use_id": str(source.get("tool_use_id") or ""),
                # Empty tool_result content is a 400; every executor result is JSON, so this is a
                # belt-and-braces default rather than an expected path.
                "content": text if text.strip() else "(no output)",
                "is_error": bool(source.get("is_error")),
            }
        raise ProviderError(
            f"cannot send a {block_type!r} content block to Anthropic",
            kind="bad_request",
            detail={"block_type": block_type},
        )

    def _convert_messages(self, messages: Sequence[Msg]) -> list[dict[str, Any]]:
        """Convert the normalized conversation into Anthropic messages, order preserved.

        ``tool_result`` blocks belong to a ``user`` message and ``thinking``/``tool_use`` blocks to
        the ``assistant`` message they were streamed in; this conversion never moves a block between
        messages, because reordering the assistant turn is exactly what the signature check rejects.
        """
        out: list[dict[str, Any]] = []
        for message in messages or []:
            source = message or {}
            role = str(source.get("role") or "user").strip().lower()
            if role not in ("user", "assistant"):
                raise ProviderError(
                    f"unsupported message role {role!r} — Anthropic accepts 'user' and 'assistant'",
                    kind="bad_request",
                    detail={"role": role},
                )
            content = source.get("content")
            if isinstance(content, str):
                blocks: Sequence[Block] = [text_block(content)]
            elif isinstance(content, Sequence):
                blocks = list(content)
            else:
                blocks = []
            converted: list[dict[str, Any]] = []
            for block in blocks:
                rendered = self._convert_block(block)
                if rendered is not None:
                    converted.append(rendered)
            if not converted:
                continue
            out.append({"role": role, "content": converted})
        return out

    def _build_body(
        self,
        *,
        system: str,
        messages: Sequence[Msg],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        model: str,
        capabilities: Capabilities,
        stream: bool,
    ) -> dict[str, Any]:
        """Build the ``/v1/messages`` request body.

        Note what is absent and must stay absent: ``temperature``, ``top_p`` and ``top_k``. They are
        a hard 400 on the current generation and no caller can reach them from here. ``tool_choice``
        is not sent either — the default is ``auto``; under the ``enabled`` thinking fallback only
        ``auto`` and ``none`` would be legal anyway.
        """
        name = (model or "").strip()
        if not name:
            raise ProviderError(
                "no model selected — choose one in settings before running",
                kind="bad_request",
                detail={"provider": self.kind},
            )
        converted = self._convert_messages(messages)
        if not converted:
            raise ProviderError(
                "cannot call the model with an empty conversation",
                kind="bad_request",
                detail={"provider": self.kind, "model": name},
            )
        thinking, effective_max_tokens = self._thinking_config(
            capabilities.thinking_form if capabilities is not None else None,
            max(1, int(max_tokens)),
        )
        body: dict[str, Any] = {
            "model": name,
            "max_tokens": effective_max_tokens,
            "messages": converted,
        }
        if thinking is not None:
            body["thinking"] = thinking
        if system and system.strip():
            body["system"] = system
        rendered_tools = to_anthropic(list(tools or []))
        if rendered_tools:
            body["tools"] = rendered_tools
        if stream:
            body["stream"] = True
        return body

    # ---------------------------------------------------------------- models & capabilities

    async def list_models(self) -> list[ModelInfo]:
        """List every model this endpoint offers, following the ``has_more``/``after_id`` cursor.

        The envelope is Anthropic's, not OpenAI's: no ``object: "list"``, and each entry carries a
        real ``capabilities`` object that is read directly instead of probed.
        """
        out: list[ModelInfo] = []
        seen: set[str] = set()
        after_id: str | None = None
        for _ in range(_MODELS_MAX_PAGES):
            params: dict[str, Any] = {"limit": _MODELS_PAGE_LIMIT}
            if after_id:
                params["after_id"] = after_id
            body = await self._get_json(_MODELS_PATH, params)
            entries = body.get("data")
            if not isinstance(entries, list):
                raise ProviderError(
                    f"{_KIND_LEADS['decode']} — /v1/models returned no 'data' array",
                    kind="decode",
                    detail={"provider": self.kind, "base_url": self._base_url},
                )
            for entry in entries:
                info = _model_info(entry)
                if info is None or info.id in seen:
                    continue
                seen.add(info.id)
                out.append(info)
            if not bool(body.get("has_more")):
                break
            last_id = body.get("last_id")
            if not last_id and entries and isinstance(entries[-1], dict):
                last_id = entries[-1].get("id")
            if not isinstance(last_id, str) or not last_id.strip() or last_id == after_id:
                break
            after_id = last_id.strip()
        log.debug("anthropic list_models base_url=%s count=%d", self._base_url, len(out))
        return out

    async def probe(self, model: str) -> Capabilities:
        """Return what a model can do, read from its declared capabilities where possible.

        Anthropic declares ``image_input.supported`` and the thinking types it accepts, so the
        answer is a fact rather than the result of a throwaway request. A live probe runs only when
        the model is absent from the list (a gateway that serves ``/v1/messages`` but not a full
        catalogue), or when the entry carries no capability object.
        """
        name = (model or "").strip()
        if not name:
            raise ProviderError(
                "no model selected — choose one in settings before probing",
                kind="bad_request",
                detail={"provider": self.kind},
            )
        declared: ModelInfo | None = None
        try:
            for info in await self.list_models():
                if info.id == name:
                    declared = info
                    break
        except ProviderError as exc:
            # A rejected key, a rate limit or an unreachable host is the answer; only a missing or
            # unreadable catalogue falls through to the live probe.
            if exc.kind not in ("not_found", "bad_request", "decode"):
                raise
            log.debug("anthropic model list unavailable (%s) — probing %s live", exc.kind, name)
        if declared is not None:
            capabilities = declared.raw.get("capabilities")
            if isinstance(capabilities, dict) and capabilities:
                vision = _declared_vision(capabilities)
                if vision is not None:
                    return Capabilities(
                        supports_vision=vision,
                        supports_tools=True,
                        thinking_form=_declared_thinking_form(capabilities),
                        source="declared",
                    )
        return await self._live_probe(name)

    async def _live_probe(self, model: str) -> Capabilities:
        """Determine capability with throwaway requests, for a model the catalogue does not describe."""
        log.info("probing anthropic model=%s live (not described by /v1/models)", model)
        thinking_form: str | None = None
        accepted = False
        # adaptive first; `enabled` is tried only because adaptive was refused; then no thinking at
        # all, which is what an old non-thinking model accepts.
        for candidate in ("adaptive", "enabled", None):
            body = self._probe_body(model, thinking_form=candidate, with_image=False, with_tool=False)
            if await self._probe_ok(body):
                thinking_form = candidate
                accepted = True
                break
        if not accepted:
            raise ProviderError(
                f"model {model!r} refused a minimal request — check the model id and the endpoint",
                kind="bad_request",
                detail={"provider": self.kind, "base_url": self._base_url, "model": model},
            )
        supports_vision = await self._probe_ok(
            self._probe_body(model, thinking_form=thinking_form, with_image=True, with_tool=False)
        )
        supports_tools = await self._probe_ok(
            self._probe_body(model, thinking_form=thinking_form, with_image=False, with_tool=True)
        )
        return Capabilities(
            supports_vision=supports_vision,
            supports_tools=supports_tools,
            thinking_form=thinking_form,
            source="probed",
        )

    def _probe_body(
        self, model: str, *, thinking_form: str | None, with_image: bool, with_tool: bool
    ) -> dict[str, Any]:
        """Build one throwaway probe request isolating a single capability."""
        blocks: list[Block] = []
        if with_image:
            blocks.append({"type": "image", "media_type": _PROBE_PNG_MEDIA_TYPE, "data": _PROBE_PNG_B64})
        blocks.append(text_block("ok"))
        capabilities = Capabilities(
            supports_vision=True,
            supports_tools=True,
            thinking_form=thinking_form,
            source="probed",
        )
        return self._build_body(
            system="",
            messages=[{"role": "user", "content": blocks}],
            tools=[_PROBE_TOOL] if with_tool else [],
            max_tokens=_PROBE_MAX_TOKENS,
            model=model,
            capabilities=capabilities,
            stream=False,
        )

    async def _probe_ok(self, body: dict[str, Any]) -> bool:
        """Send one probe: 200 means supported, 400/422 mean unsupported, anything else propagates."""
        try:
            await self._post_json(body)
        except ProviderError as exc:
            if exc.kind == "bad_request":
                return False
            raise
        return True

    # ---------------------------------------------------------------- streaming

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
        """Stream one assistant turn, ending with a DoneDelta carrying the whole reassembled turn."""
        body = self._build_body(
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            model=model,
            capabilities=capabilities,
            stream=True,
        )
        return self._stream(body)

    async def _stream(self, body: dict[str, Any]) -> AsyncIterator[Delta]:
        """Drive the SSE response, yielding deltas and finally the reassembled assistant turn."""
        model = str(body.get("model") or "")
        state = _StreamState(model)
        timeout = float(PROVIDER_TIMEOUT_SECONDS)
        log.debug(
            "anthropic run model=%s max_tokens=%s messages=%d tools=%d thinking=%s",
            model,
            body.get("max_tokens"),
            len(body.get("messages") or []),
            len(body.get("tools") or []),
            (body.get("thinking") or {}).get("type", "off"),
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout(timeout)) as client:
                async with client.stream(
                    "POST", self._url(_MESSAGES_PATH), json=body, headers=self._headers(stream=True)
                ) as response:
                    if response.status_code >= 400:
                        raw = await response.aread()
                        raise self._http_error(response.status_code, raw)
                    pending: list[str] = []
                    async for line in response.aiter_lines():
                        line = line.rstrip("\r")
                        if not line:
                            for delta in self._consume(pending, state):
                                yield delta
                            continue
                        if line.startswith(":"):
                            continue  # SSE comment / keep-alive
                        if line.startswith("data:"):
                            pending.append(line[5:].lstrip(" "))
                        # `event:`, `id:` and `retry:` lines carry nothing the payload's own "type"
                        # field does not already say.
                    for delta in self._consume(pending, state):
                        yield delta
        except httpx.RequestError as exc:
            raise self._transport_error(exc, timeout=timeout) from exc
        if not state.saw_stop and state.stop_reason is None:
            raise ProviderError(
                "the model stream ended before the turn finished",
                kind="transport",
                detail={"provider": self.kind, "model": model, "blocks": len(state.order)},
            )
        stop_reason = state.stop_reason or "end_turn"
        log.debug(
            "anthropic run finished model=%s stop_reason=%s in=%d out=%d blocks=%d",
            model,
            stop_reason,
            state.input_tokens,
            state.output_tokens,
            len(state.order),
        )
        yield DoneDelta(stop_reason=stop_reason, assistant_blocks=self._assistant_blocks(state))

    def _consume(self, pending: list[str], state: _StreamState) -> Iterator[Delta]:
        """Parse one buffered SSE event and dispatch it; the buffer is cleared either way."""
        if not pending:
            return
        payload = "\n".join(pending)
        pending.clear()
        text = payload.strip()
        if not text or text == "[DONE]":
            return
        try:
            event = json.loads(text)
        except ValueError as exc:
            raise ProviderError(
                f"{_KIND_LEADS['decode']} — malformed SSE payload: {text[:_ERROR_BODY_CHARS]}",
                kind="decode",
                detail={"provider": self.kind, "model": state.model},
            ) from exc
        if not isinstance(event, dict):
            return
        yield from self._handle_event(event, state)

    def _handle_event(self, event: dict[str, Any], state: _StreamState) -> Iterator[Delta]:
        """Apply one SSE event to the accumulator and yield whatever deltas it produces."""
        event_type = str(event.get("type") or "")

        if event_type == "message_start":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            state.input_tokens = int(usage.get("input_tokens") or 0)
            state.output_tokens = int(usage.get("output_tokens") or 0)
            return

        if event_type == "content_block_start":
            index = int(event.get("index") or 0)
            content_block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
            block_type = str(content_block.get("type") or "")
            record = state.open(index, _new_record(block_type, content_block))
            if record["type"] == "unknown":
                log.warning("anthropic sent an unhandled content block type %r", block_type)
                return
            # A start event normally carries empty text, but honour it when it does not.
            if record["type"] == "text" and record["text"]:
                yield TextDelta(record["text"])
            elif record["type"] == "thinking" and record["thinking"]:
                yield ThinkingDelta(record["thinking"])
            return

        if event_type == "content_block_delta":
            index = int(event.get("index") or 0)
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            delta_type = str(delta.get("type") or "")
            record = state.blocks.get(index)
            if record is None:
                implied = _IMPLIED_BLOCK_TYPES.get(delta_type)
                if implied is None:
                    return
                record = state.open(index, _new_record(implied))
            if delta_type == "text_delta":
                piece = str(delta.get("text") or "")
                if piece:
                    record["text"] = record.get("text", "") + piece
                    yield TextDelta(piece)
                return
            if delta_type == "thinking_delta":
                piece = str(delta.get("thinking") or "")
                if piece:
                    record["thinking"] = record.get("thinking", "") + piece
                    yield ThinkingDelta(piece)
                return
            if delta_type == "signature_delta":
                # The signature arrives on its own delta, sometimes in pieces. It is accumulated onto
                # the open thinking block and echoed back untouched; a mismatch is a 400.
                piece = str(delta.get("signature") or "")
                if piece:
                    record["signature"] = record.get("signature", "") + piece
                return
            if delta_type == "input_json_delta":
                # `index` is the content-block index: stable for the whole turn, and the same key the
                # matching ToolCallDone is emitted under at content_block_stop.
                piece = str(delta.get("partial_json") or "")
                if piece:
                    record["json"] = record.get("json", "") + piece
                yield ToolCallDelta(
                    index=index,
                    id=record.get("id") or None,
                    name=record.get("name") or None,
                    partial_json=piece,
                )
                return
            return

        if event_type == "content_block_stop":
            index = int(event.get("index") or 0)
            record = state.blocks.get(index)
            if record is None or record.get("type") != "tool_use":
                return
            raw = str(record.get("json") or "")
            if not raw.strip():
                # No input_json_delta at all means a tool with no arguments: an empty string is {}.
                parsed: Any = record.get("input") or {}
            else:
                try:
                    parsed = json.loads(raw)
                except ValueError as exc:
                    raise ProviderError(
                        f"{_KIND_LEADS['decode']} — tool call arguments for "
                        f"{record.get('name') or '?'} were not valid JSON: {raw[:_ERROR_BODY_CHARS]}",
                        kind="decode",
                        detail={
                            "provider": self.kind,
                            "model": state.model,
                            "tool": record.get("name"),
                        },
                    ) from exc
            if not isinstance(parsed, dict):
                raise ProviderError(
                    f"{_KIND_LEADS['decode']} — tool call arguments for "
                    f"{record.get('name') or '?'} were {type(parsed).__name__}, not an object",
                    kind="decode",
                    detail={"provider": self.kind, "model": state.model, "tool": record.get("name")},
                )
            record["input"] = parsed
            yield ToolCallDone(
                id=str(record.get("id") or ""),
                name=str(record.get("name") or ""),
                input=parsed,
            )
            return

        if event_type == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                state.stop_reason = str(stop_reason)
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            if usage.get("input_tokens") is not None:
                state.input_tokens = int(usage.get("input_tokens") or 0)
            if usage.get("output_tokens") is not None:
                state.output_tokens = int(usage.get("output_tokens") or 0)
            yield UsageDelta(input_tokens=state.input_tokens, output_tokens=state.output_tokens)
            if state.stop_reason == _CONTEXT_WINDOW_STOP:
                # This one arrives on an HTTP 200, not on an error status. Treating it as a normal
                # turn would silently hand the loop a truncated answer.
                raise ProviderError(
                    "the conversation exceeded the model's context window — trim it and retry",
                    status=200,
                    kind="context_window",
                    detail={
                        "provider": self.kind,
                        "model": state.model,
                        "stop_reason": state.stop_reason,
                        "input_tokens": state.input_tokens,
                        "output_tokens": state.output_tokens,
                    },
                )
            return

        if event_type == "message_stop":
            state.saw_stop = True
            return

        if event_type == "error":
            # A mid-stream error event: HTTP 200 already went out, the failure is in the body.
            raise self._http_error(None, event)

        # "ping" and anything newer than this client: nothing to accumulate.
        return

    @staticmethod
    def _assistant_blocks(state: _StreamState) -> list[Block]:
        """Rebuild the assistant turn as normalized blocks, in the order the server streamed them.

        This list is what the loop appends to the conversation and echoes back with the next
        ``tool_result``: thinking blocks keep their text and signature exactly as received, and they
        keep their position relative to the ``tool_use`` block of the same turn.
        """
        out: list[Block] = []
        for index in state.order:
            record = state.blocks.get(index)
            if not record:
                continue
            record_type = record.get("type")
            if record_type == "text":
                text = str(record.get("text") or "")
                if text.strip():
                    out.append(text_block(text))
            elif record_type == "thinking":
                out.append(
                    thinking_block(
                        str(record.get("thinking") or ""),
                        str(record.get("signature") or "") or None,
                    )
                )
            elif record_type == "redacted_thinking":
                out.append(redacted_thinking_block(str(record.get("data") or "")))
            elif record_type == "tool_use":
                tool_input = record.get("input")
                out.append(
                    tool_use_block(
                        str(record.get("id") or ""),
                        str(record.get("name") or ""),
                        tool_input if isinstance(tool_input, dict) else {},
                    )
                )
        return out
