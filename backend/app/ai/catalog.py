"""Model listing, the one-shot capability probe, and the database-backed probe cache.

Three questions have to be answered before the agent loop can talk to an endpoint: which models it
offers, whether the chosen model accepts images and tools, and which thinking form it takes. This
module answers all three and remembers the answer.

The probe itself lives in each provider (`Provider.probe`), because only the provider knows its own
wire format. What lives here is everything vendor-independent around it:

* the fixtures the probe sends — a real 1x1 PNG and a one-parameter tool (BUILD_PROMPT §4.3). The
  image must decode: a corrupt payload makes a perfectly capable endpoint answer 400 and every model
  is then reported vision-less, which is exactly the models the operator wanted.
* the cache, keyed `f"{kind}|{normalized_base_url}|{model}"` and valid for `PROBE_CACHE_HOURS`, so
  the operator's throwaway request is spent once per model rather than once per agent run.
* the operator overrides. `supports_vision` / `supports_tools` are switches, not opinions: a
  self-hosted gateway can answer 400 for reasons that have nothing to do with the capability, and
  no heuristic survives that. When a switch is set it wins, and the verdict says `source="override"`.

Nothing here is written to the cache before the provider has actually answered, so a provider that
raises leaves the previous verdict — or no row at all — exactly as it was.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import (
    Capabilities,
    ModelInfo,
    Provider,
    ToolSpec,
    build_provider,
    normalize_base_url,
)
from app.config import settings
from app.crypto import MASK
from app.db import get_setting, has_value
from app.errors import ConfigError, ProviderError
from app.models import ProbeCache, utcnow
from app.util.logging import get_logger

__all__ = [
    "PROBE_MAX_TOKENS",
    "PROBE_MEDIA_TYPE",
    "PROBE_PNG_B64",
    "PROBE_PROMPT",
    "PROBE_TOOL",
    "effective_capabilities",
    "list_models",
    "probe_model",
    "summarize",
]

log = get_logger("app.ai.catalog")


# --------------------------------------------------------------------------------------
# Probe fixtures — the throwaway request of BUILD_PROMPT §4.3
# --------------------------------------------------------------------------------------

#: A real 1x1 white PNG, base64 with no ``data:`` prefix (the normalized image block of §16 carries
#: the raw payload; the OpenAI-compatible provider wraps it in a data URL itself, because Ollama and
#: several gateways reject remote image URLs outright). Written out by Pillow and verified to decode
#: — a broken payload would make every vision probe answer "unsupported" for the wrong reason.
PROBE_PNG_B64: str = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mP4//8/AAX+Av4zEpUUAAAAAElFTkSuQmCC"
)

#: The media type of :data:`PROBE_PNG_B64`; providers need it to build the image block.
PROBE_MEDIA_TYPE: str = "image/png"

#: The user text sent with the probe. Short on purpose: the answer is thrown away, only the HTTP
#: status matters.
PROBE_PROMPT: str = "Reply with the single word: ok"

#: ``max_tokens`` for the probe. **Never 1**: on every thinking-capable model a budget of 1 is a hard
#: 400 regardless of vision or tools, so a probe at 1 reports precisely the good models unsupported
#: (BUILD_PROMPT §4.3). 16 is enough for a one-word answer or a single tool call.
PROBE_MAX_TOKENS: int = 16

#: The tool offered during the probe: one required string parameter, no side effects, and a name that
#: cannot collide with a real tool in ``app.ai.tools``.
PROBE_TOOL: ToolSpec = ToolSpec(
    name="probe_echo",
    description=(
        "Connectivity check used once when this endpoint is configured. Call it with any short "
        "string; the result is discarded."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Any short string, for example 'ok'.",
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    },
)


# --------------------------------------------------------------------------------------
# Cache keys and value normalisation
# --------------------------------------------------------------------------------------

# Mirrors app.ai.base._DEFAULT_BASE_URLS so an empty base_url keys the cache under the URL the
# provider will really call. These two vendor origins are fixed; if one ever changes in base.py the
# only consequence of forgetting it here is one extra probe under a stale key.
_DEFAULT_BASE_URLS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}

# ProbeCache.source is exactly these two: a capability read out of the provider's own metadata
# ("declared", which is what Anthropic's /v1/models and OpenRouter's architecture block give us) or
# one established by a live request ("probed"). "override" is never stored — it is a settings value,
# not a fact about the endpoint.
_STORED_SOURCES: frozenset[str] = frozenset({"declared", "probed"})

# The two thinking configurations of BUILD_PROMPT §4.1. Anything else means "unknown yet".
_THINKING_FORMS: frozenset[str] = frozenset({"adaptive", "enabled"})

_TRUE_WORDS: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS: frozenset[str] = frozenset({"0", "false", "no", "off"})
_UNSET_WORDS: frozenset[str] = frozenset({"", "null", "none", "auto", "default", "inherit"})


def _normalize_kind(kind: str) -> str:
    """Canonicalise a provider kind, raising the same ConfigError build_provider would."""
    normalized = (kind or "").strip().lower()
    if normalized not in _DEFAULT_BASE_URLS:
        raise ConfigError(
            f"unknown provider kind {kind!r} — expected 'anthropic' or 'openai'",
            detail={"kind": kind},
        )
    return normalized


def _cache_key(kind: str, base_url: str, model: str) -> str:
    """Build the ProbeCache key: kind, the normalized base URL, and the model id."""
    url = normalize_base_url(base_url, default=_DEFAULT_BASE_URLS[kind])
    return f"{kind}|{url}|{model}"


def _normalize_thinking(value: Any) -> str | None:
    """Return a stored thinking form only when it is one this codebase knows how to send."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if text in _THINKING_FORMS else None


def _normalize_source(value: Any) -> str:
    """Return a storable capability source; anything unexpected counts as a live probe."""
    text = str(value or "").strip().lower()
    return text if text in _STORED_SOURCES else "probed"


def _as_override(value: Any) -> bool | None:
    """Read an operator override: None means "no override", anything else is a decision."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _UNSET_WORDS:
        return None
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def _as_positive_int(value: Any, fallback: int) -> int:
    """Coerce a stored number to a positive int, falling back when it is unusable."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _as_headers(value: Any) -> dict[str, str]:
    """Coerce a stored extra-headers value into a plain str->str map."""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(name): str(header)
        for name, header in value.items()
        if header is not None and str(name).strip()
    }


def _capabilities_dict(caps: Capabilities | None) -> dict[str, Any] | None:
    """Render a Capabilities as the JSON shape the settings and probe endpoints return."""
    if caps is None:
        return None
    return {
        "supports_vision": bool(caps.supports_vision),
        "supports_tools": bool(caps.supports_tools),
        "thinking_form": caps.thinking_form,
        "source": caps.source,
    }


def _normalize_capabilities(verdict: Any) -> Capabilities:
    """Normalize whatever a provider's probe returned into a Capabilities we can store.

    A provider that answers with a mapping instead of the dataclass would otherwise read as
    "no vision, no tools" through ``getattr``, which is a silent wrong verdict — the one failure
    mode this whole module exists to avoid.
    """
    if verdict is None:
        raise ProviderError(
            "the provider returned no capability verdict",
            kind="decode",
            detail={"verdict": None},
        )
    if isinstance(verdict, Mapping):
        read = verdict.get
    else:

        def read(name: str, default: Any = None) -> Any:
            return getattr(verdict, name, default)

    if read("supports_vision") is None and read("supports_tools") is None:
        raise ProviderError(
            "the provider returned an unreadable capability verdict",
            kind="decode",
            detail={"verdict": repr(verdict)[:200]},
        )
    return Capabilities(
        supports_vision=bool(read("supports_vision", False)),
        supports_tools=bool(read("supports_tools", False)),
        thinking_form=_normalize_thinking(read("thinking_form")),
        source=_normalize_source(read("source", "probed")),
    )


def _apply_overrides(
    caps: Capabilities, vision: bool | None, tools: bool | None
) -> Capabilities:
    """Let the operator's switches win over the probed verdict, marking the source accordingly."""
    if vision is None and tools is None:
        return caps
    return replace(
        caps,
        supports_vision=caps.supports_vision if vision is None else vision,
        supports_tools=caps.supports_tools if tools is None else tools,
        source="override",
    )


# --------------------------------------------------------------------------------------
# The cache itself
# --------------------------------------------------------------------------------------


def _is_fresh(checked_at: datetime | None, now: datetime) -> bool:
    """Report whether a cached verdict is still inside the PROBE_CACHE_HOURS window."""
    hours = settings.PROBE_CACHE_HOURS
    if hours <= 0 or checked_at is None:
        # PROBE_CACHE_HOURS = 0 is a deliberate "always re-probe", not a broken config.
        return False
    stamp = checked_at
    if stamp.tzinfo is not None:
        stamp = stamp.replace(tzinfo=None)
    # A clock that jumped forward leaves checked_at in the future; that difference is negative and
    # therefore fresh, which is what we want — it must not force an endless re-probe loop.
    return (now - stamp) <= timedelta(hours=hours)


async def _read_cache(
    session: AsyncSession, cache_key: str
) -> tuple[Capabilities, datetime | None] | None:
    """Read one cached verdict as plain columns, so no ORM row can go stale behind an upsert."""
    result = await session.execute(
        select(
            ProbeCache.supports_vision,
            ProbeCache.supports_tools,
            ProbeCache.thinking_form,
            ProbeCache.source,
            ProbeCache.checked_at,
        ).where(ProbeCache.cache_key == cache_key)
    )
    row = result.first()
    if row is None:
        return None
    caps = Capabilities(
        supports_vision=bool(row.supports_vision),
        supports_tools=bool(row.supports_tools),
        thinking_form=_normalize_thinking(row.thinking_form),
        source=_normalize_source(row.source),
    )
    return caps, row.checked_at


async def _store_verdict(
    session: AsyncSession, cache_key: str, caps: Capabilities, checked_at: datetime
) -> None:
    """Write a verdict as one atomic upsert — it is never half a row, even under a concurrent probe."""
    values: dict[str, Any] = {
        "cache_key": cache_key,
        "supports_vision": bool(caps.supports_vision),
        "supports_tools": bool(caps.supports_tools),
        "thinking_form": caps.thinking_form,
        "source": caps.source,
        "checked_at": checked_at,
    }
    statement = sqlite_insert(ProbeCache).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=["cache_key"],
        set_={name: value for name, value in values.items() if name != "cache_key"},
    )
    await session.execute(statement)
    await session.flush()


async def _cached_capabilities(
    session: AsyncSession, kind: str, base_url: str, model: str
) -> Capabilities | None:
    """Return the last stored verdict for an endpoint+model, fresh or not, without any network call."""
    name = (model or "").strip()
    if not name:
        return None
    entry = await _read_cache(session, _cache_key(_normalize_kind(kind), base_url, name))
    return None if entry is None else entry[0]


# --------------------------------------------------------------------------------------
# Provider lifecycle
# --------------------------------------------------------------------------------------


async def _release(provider: Provider) -> None:
    """Close a provider built for a single call; a leaked HTTP client must not break the caller."""
    closer = getattr(provider, "aclose", None) or getattr(provider, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 - shutdown is best effort, the answer is already ours
        log.warning("provider_release_failed error=%s", exc)


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


async def list_models(
    kind: str,
    base_url: str,
    api_key: str,
    extra_headers: dict[str, str] | None = None,
) -> list[ModelInfo]:
    """List the models an endpoint offers, normalized to ModelInfo.

    ProviderError propagates untouched: its ``kind`` is what tells the operator "key rejected"
    rather than "failed to connect" (BUILD_PROMPT §4.3), and flattening it here would throw that
    away.
    """
    normalized_kind = _normalize_kind(kind)
    provider = build_provider(
        normalized_kind,
        base_url=base_url,
        api_key=api_key or "",
        extra_headers=extra_headers,
    )
    try:
        models = await provider.list_models()
    finally:
        await _release(provider)
    result = list(models)
    log.info("models_listed kind=%s count=%d", normalized_kind, len(result))
    return result


async def probe_model(
    session: AsyncSession,
    kind: str,
    base_url: str,
    api_key: str,
    model: str,
    extra_headers: dict[str, str] | None = None,
    *,
    force: bool = False,
) -> Capabilities:
    """Return what a model can do, probing the endpoint at most once per PROBE_CACHE_HOURS.

    A fresh cache row answers without touching the network. On a miss (or with ``force``) the
    provider runs its own throwaway request and the verdict — vision, tools, the thinking form it
    accepts, and whether the provider declared it or had to ask — is written back as one atomic
    upsert **after** the provider has answered. A provider that raises therefore leaves the previous
    verdict untouched instead of replacing it with a half-written row.
    """
    normalized_kind = _normalize_kind(kind)
    name = (model or "").strip()
    if not name:
        raise ConfigError(
            "a model must be selected before its capabilities can be probed",
            detail={"kind": normalized_kind},
        )
    cache_key = _cache_key(normalized_kind, base_url, name)

    if not force:
        entry = await _read_cache(session, cache_key)
        if entry is not None and _is_fresh(entry[1], utcnow()):
            return entry[0]

    provider = build_provider(
        normalized_kind,
        base_url=base_url,
        api_key=api_key or "",
        extra_headers=extra_headers,
    )
    try:
        verdict = await provider.probe(name)
    finally:
        await _release(provider)

    caps = _normalize_capabilities(verdict)
    await _store_verdict(session, cache_key, caps, utcnow())
    log.info(
        "probe_stored kind=%s model=%s vision=%s tools=%s thinking=%s source=%s",
        normalized_kind,
        name,
        caps.supports_vision,
        caps.supports_tools,
        caps.thinking_form,
        caps.source,
    )
    return caps


async def effective_capabilities(
    session: AsyncSession,
    kind: str,
    base_url: str,
    api_key: str,
    model: str,
    extra_headers: dict[str, str] | None = None,
) -> Capabilities:
    """Return the capabilities the agent loop should actually believe.

    The probed (or cached) verdict first, then the operator's overrides on top:
    ``provider.supports_vision_override`` and ``provider.supports_tools_override`` are ``None`` when
    unset and win outright when set. A self-hosted endpoint can fail a probe for reasons that have
    nothing to do with the capability, which is why these switches exist — so when *both* are set
    and the probe itself fails, the switches still answer instead of the run dying on a setup check.
    Any override in play makes the verdict's ``source`` read ``"override"``.
    """
    vision = _as_override(await get_setting(session, "provider.supports_vision_override", None))
    tools = _as_override(await get_setting(session, "provider.supports_tools_override", None))
    try:
        caps = await probe_model(session, kind, base_url, api_key, model, extra_headers)
    except ProviderError as exc:
        if vision is None or tools is None:
            raise
        cached = await _cached_capabilities(session, kind, base_url, model)
        log.warning(
            "probe_failed_using_overrides kind=%s model=%s provider_kind=%s error=%s",
            kind,
            model,
            exc.kind,
            exc.message,
        )
        return Capabilities(
            supports_vision=vision,
            supports_tools=tools,
            thinking_form=cached.thinking_form if cached is not None else None,
            source="override",
        )
    return _apply_overrides(caps, vision, tools)


async def summarize(session: AsyncSession) -> dict[str, Any]:
    """Describe the configured provider for GET /api/settings — never the API key itself.

    Read-only and offline: it reports the last stored verdict (with the operator's overrides applied)
    and never starts a probe, because loading the settings page must not block on a provider round
    trip. ``api_key`` is the mask and ``has_api_key`` is the flag beside it; the real value never
    leaves the database.
    """
    kind = str(await get_setting(session, "provider.kind", "") or "").strip().lower()
    base_url = str(await get_setting(session, "provider.base_url", "") or "")
    model = str(await get_setting(session, "provider.model", "") or "")
    extra_headers = _as_headers(await get_setting(session, "provider.extra_headers", {}))
    max_tokens = _as_positive_int(
        await get_setting(session, "provider.max_tokens", settings.MAX_TOKENS),
        settings.MAX_TOKENS,
    )
    vision = _as_override(await get_setting(session, "provider.supports_vision_override", None))
    tools = _as_override(await get_setting(session, "provider.supports_tools_override", None))
    has_api_key = await has_value(session, "provider.api_key")

    caps: Capabilities | None = None
    if kind in _DEFAULT_BASE_URLS and model:
        try:
            cached = await _cached_capabilities(session, kind, base_url, model)
        except ConfigError as exc:
            # A base URL that no longer normalizes (hand-edited row, changed rules) costs the
            # capability line, not the settings page.
            log.warning("capabilities_lookup_failed kind=%s reason=%s", kind, exc.message)
            cached = None
        if cached is not None:
            caps = _apply_overrides(cached, vision, tools)
        elif vision is not None and tools is not None:
            # Never probed, but the operator declared both facts — that is a complete answer.
            caps = Capabilities(
                supports_vision=vision,
                supports_tools=tools,
                thinking_form=None,
                source="override",
            )

    return {
        "kind": kind,
        "base_url": base_url,
        "api_key": MASK if has_api_key else None,
        "has_api_key": has_api_key,
        "model": model,
        "extra_headers": extra_headers,
        "max_tokens": max_tokens,
        "supports_vision_override": vision,
        "supports_tools_override": tools,
        "capabilities": _capabilities_dict(caps),
    }
