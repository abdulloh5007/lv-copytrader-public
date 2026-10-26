"""Provider-agnostic types for the AI layer: normalized blocks, streaming deltas, the Provider protocol.

Every provider implementation (Anthropic, OpenAI-compatible) translates its own wire format into the
normalized shapes documented here, so ``app.ai.loop`` never branches on vendor.

A conversation is a list of ``Msg``::

    {"role": "user" | "assistant", "content": [Block, ...]}

A ``Block`` is one of six shapes. Build them with the constructors in this module; no other module
hand-builds these dicts::

    {"type": "text",              "text": str}
    {"type": "image",             "media_type": str, "data": str}   # base64 payload, no "data:" prefix
    {"type": "thinking",          "thinking": str, "signature": str | None}
    {"type": "redacted_thinking", "data": str}
    {"type": "tool_use",          "id": str, "name": str, "input": dict}
    {"type": "tool_result",       "tool_use_id": str, "content": str, "is_error": bool}

``thinking`` and ``redacted_thinking`` blocks are echoed back to the provider exactly as received, in
order, alongside the ``tool_use`` block of the same assistant turn: Anthropic signs them and any edit,
reorder or omission is a hard 400 (BUILD_PROMPT §4.1).

``Provider.run`` streams ``Delta`` values in this order: zero or more ``ThinkingDelta`` /
``TextDelta`` / ``ToolCallDelta``, one ``ToolCallDone`` per completed tool call, at most one
``UsageDelta``, and exactly one ``DoneDelta`` last, carrying the assistant turn's blocks already
normalized and ready to be appended to the conversation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeAlias, runtime_checkable
from urllib.parse import urlsplit

from app.errors import ConfigError

Block: TypeAlias = dict[str, Any]
Msg: TypeAlias = dict[str, Any]

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_DEFAULT_BASE_URLS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}


# --------------------------------------------------------------------------------------
# Model / capability / tool descriptors
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One entry of a provider's model list, normalized across vendors."""

    id: str
    display_name: str = ""
    supports_vision: bool | None = None
    supports_tools: bool | None = None
    context_length: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a specific model on a specific endpoint can actually do."""

    supports_vision: bool
    supports_tools: bool
    thinking_form: str | None = None
    source: str = "probed"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool offered to the model: its name, its model-facing description and its JSON Schema."""

    name: str
    description: str
    input_schema: dict[str, Any]


# --------------------------------------------------------------------------------------
# Normalized block constructors
# --------------------------------------------------------------------------------------


def text_block(t: str) -> Block:
    """Build a normalized text block."""
    return {"type": "text", "text": t}


def image_block(media_type: str, data: str) -> Block:
    """Build a normalized image block from a raw base64 payload (no "data:" prefix)."""
    return {"type": "image", "media_type": media_type, "data": data}


def thinking_block(thinking: str, signature: str | None = None) -> Block:
    """Build a normalized thinking block; its signature must be echoed back unmodified."""
    return {"type": "thinking", "thinking": thinking, "signature": signature}


def redacted_thinking_block(data: str) -> Block:
    """Build a normalized redacted-thinking block; it must be echoed back unmodified."""
    return {"type": "redacted_thinking", "data": data}


def tool_use_block(id: str, name: str, input: dict[str, Any]) -> Block:
    """Build a normalized tool_use block."""
    return {"type": "tool_use", "id": id, "name": name, "input": dict(input)}


def tool_result_block(tool_use_id: str, content: str, is_error: bool = False) -> Block:
    """Build a normalized tool_result block."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": bool(is_error),
    }


# --------------------------------------------------------------------------------------
# Streaming deltas
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """A fragment of the model's reasoning text."""

    text: str


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A fragment of the model's visible answer text."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """A fragment of a tool call still being streamed; partial_json accumulates per index."""

    index: int
    id: str | None = None
    name: str | None = None
    partial_json: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallDone:
    """A tool call whose arguments finished streaming and parsed into a dict."""

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UsageDelta:
    """Token usage reported for the current provider request."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class DoneDelta:
    """The end of one assistant turn, with the turn's normalized blocks in wire order."""

    stop_reason: str
    assistant_blocks: list[Block] = field(default_factory=list)


Delta: TypeAlias = (
    ThinkingDelta | TextDelta | ToolCallDelta | ToolCallDone | UsageDelta | DoneDelta
)


# --------------------------------------------------------------------------------------
# The provider protocol
# --------------------------------------------------------------------------------------


@runtime_checkable
class Provider(Protocol):
    """The one interface both vendors satisfy, so the agent loop never branches on vendor."""

    kind: str

    async def list_models(self) -> list[ModelInfo]:
        """List the models this endpoint offers."""
        ...

    async def probe(self, model: str) -> Capabilities:
        """Determine whether a model accepts images, tools, and which thinking form it takes."""
        ...

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
        """Stream one assistant turn as Delta values, ending with exactly one DoneDelta."""
        ...


# --------------------------------------------------------------------------------------
# Construction helpers
# --------------------------------------------------------------------------------------


def normalize_base_url(url: str, *, default: str = "") -> str:
    """Canonicalize a base URL to an origin with no trailing slash and no trailing /v1."""
    raw = (url or "").strip()
    if not raw:
        return default
    if "://" not in raw:
        raise ConfigError(
            "base URL must start with http:// or https://",
            detail={"base_url": raw},
        )
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ConfigError(
            f"unsupported base URL scheme {parts.scheme!r} — use http or https",
            detail={"base_url": raw, "scheme": parts.scheme},
        )
    netloc = parts.netloc
    if not netloc:
        raise ConfigError("base URL has no host", detail={"base_url": raw})
    if "@" not in netloc:
        netloc = netloc.lower()
    path = parts.path.rstrip("/")
    if path.lower().endswith("/v1"):
        path = path[: -len("/v1")].rstrip("/")
    return f"{scheme}://{netloc}{path}"


def build_provider(
    kind: str,
    *,
    base_url: str,
    api_key: str,
    extra_headers: dict[str, str] | None = None,
) -> Provider:
    """Construct the provider implementation for a kind, importing the module lazily."""
    normalized_kind = (kind or "").strip().lower()
    if normalized_kind not in _DEFAULT_BASE_URLS:
        raise ConfigError(
            f"unknown provider kind {kind!r} — expected 'anthropic' or 'openai'",
            detail={"kind": kind},
        )
    url = normalize_base_url(base_url, default=_DEFAULT_BASE_URLS[normalized_kind])
    headers: dict[str, str] = {
        str(name): str(value)
        for name, value in (extra_headers or {}).items()
        if value is not None and str(name).strip()
    }
    if normalized_kind == "anthropic":
        from app.ai.anthropic_provider import AnthropicProvider

        return AnthropicProvider(base_url=url, api_key=api_key or "", extra_headers=headers)

    from app.ai.openai_provider import OpenAICompatibleProvider

    return OpenAICompatibleProvider(base_url=url, api_key=api_key or "", extra_headers=headers)
