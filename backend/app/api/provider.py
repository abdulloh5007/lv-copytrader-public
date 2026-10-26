"""Provider API: test the connection, list models, probe one model's capabilities.

This is the feature the operator actually sees (BUILD_PROMPT §4.3), and its whole value is in the
error messages. "Failed to connect" tells nobody anything; the three routes here exist so the
settings page can say *which* of the four things went wrong:

* **401 → "key rejected"** — the endpoint is fine, the key is not.
* **404 → "endpoint or model not found — check base_url"** — the key may be fine; the URL is wrong.
* **connection error → "host unreachable"** — nothing answered at all.
* **non-JSON body → the first 200 characters** — usually a proxy's HTML login page, and seeing it
  is what tells the operator they are pointed at a captive portal rather than an LLM gateway.

Those sentences are written once, in the providers, and reach the client untouched. The mapping
from ``ProviderError.kind`` onto an HTTP status is ``app.errors.to_http``, so the status code and
the sentence can never drift apart.

Three details are load-bearing and are each annotated where they happen:

1. **The base URL is normalized first** (``app.ai.base.normalize_base_url``), so ``https://host``,
   ``https://host/`` and ``https://host/v1`` are one endpoint and not three.
2. **The model list is read through** ``app.ai.catalog.list_models``, which normalizes the
   envelope defensively — OpenRouter omits the top-level ``object`` and every per-entry
   ``object`` / ``owned_by``, so only ``data[i].id`` is guaranteed.
3. **An omitted or masked ``api_key`` falls back to the stored one.** Re-testing a connection
   must never require retyping a secret the operator cannot even read off the screen.

Errors are returned as the ``{"error": {...}}`` envelope of CONTRACTS §18 by returning a
``JSONResponse`` directly: FastAPI's ``HTTPException`` handler would wrap the body in
``{"detail": ...}`` and break that envelope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.ai.base import ModelInfo, normalize_base_url
from app.ai.catalog import list_models, probe_model
from app.db import get_setting, session_scope, set_setting
from fastapi import Depends
from app.runtime import Runtime, get_runtime
from app.errors import ConfigError, ProviderError, SignalPilotError, to_http
from app.schemas import (
    CapabilitiesOut,
    ModelInfoOut,
    ProviderModelsOut,
    ProviderProbeRequest,
    ProviderTestOut,
    ProviderTestRequest,
)
from app.util.logging import get_logger

router = APIRouter(prefix="/api/provider", tags=["provider"])

log = get_logger("app.api.provider")

#: Settings key holding the last successful model listing, so ``GET /api/provider/models`` can
#: answer without a network round trip (CONTRACTS §18). The stored shape is
#: ``{"kind", "base_url", "checked_at", "models": [...]}``.
#:
#: Each cached row keeps ``id``, ``display_name``, ``supports_vision``, ``supports_tools`` and
#: ``context_length`` — everything the picker renders — but **not** ``raw``. A gateway like
#: OpenRouter lists several hundred models and its per-entry vendor blob would turn one settings
#: row into hundreds of kilobytes of JSON that nothing reads. The live ``POST /test`` response
#: still carries ``raw`` in full.
MODELS_CACHE_KEY = "provider.models_cache"


# --------------------------------------------------------------------------------------
# Error rendering
# --------------------------------------------------------------------------------------


def _error(
    status: int, code: str, message: str, detail: dict[str, Any] | None = None
) -> JSONResponse:
    """Return the CONTRACTS §18 error envelope with an explicit status."""
    body = {"error": {"code": code, "message": message, "detail": dict(detail or {})}}
    return JSONResponse(status_code=status, content=body)


def _failure(exc: SignalPilotError) -> JSONResponse:
    """Render one of our own errors through ``to_http`` so kind and status cannot drift."""
    status, body = to_http(exc)
    if isinstance(exc, ProviderError):
        log.info(
            "provider_api_failure status=%d kind=%s message=%s",
            status,
            exc.kind,
            exc.message,
        )
    return JSONResponse(status_code=status, content=body)


def _unexpected(exc: Exception, *, action: str) -> JSONResponse:
    """Render a failure no provider classified, still naming what actually happened.

    Reaching this means a provider raised something outside its own error hierarchy. Reporting
    "unknown error" here would recreate exactly the uselessness §4.3 is written against, so the
    exception type and the first 200 characters of its text go into the detail.
    """
    text = str(exc).strip()
    log.warning("provider_%s_unexpected error=%s", action, type(exc).__name__, exc_info=True)
    return _error(
        500,
        "provider_error",
        f"the provider call failed unexpectedly: {type(exc).__name__}",
        {"kind": "unknown", "exception": type(exc).__name__, "body": text[:200]},
    )


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


async def _resolve_api_key(supplied: str | None) -> str:
    """Return the key to use: the one supplied, or the stored one when the field was left alone.

    ``ProviderTestRequest`` / ``ProviderProbeRequest`` already turn the display mask into ``None``,
    so ``None`` here means "the operator did not retype the secret" — which is the normal case
    every time they press Test a second time.
    """
    if supplied:
        text = supplied.strip()
        if text:
            return text
    async with session_scope() as session:
        stored = await get_setting(session, "provider.api_key", "")
    return str(stored or "").strip()


def _missing_key(kind: str, api_key: str) -> JSONResponse | None:
    """Return the 400 for an Anthropic call with no key at all; an empty key is legal elsewhere.

    OpenAI-compatible endpoints are routinely keyless — Ollama and LM Studio on localhost want no
    ``Authorization`` header — so only ``anthropic`` is checked. Saying so offline is better than
    spending a round trip to be told "key rejected" when nothing was ever entered.
    """
    if kind == "anthropic" and not api_key:
        return _error(
            400,
            "config_error",
            "no Anthropic API key is stored — paste the key from console.anthropic.com and test again",
            {"field": "api_key", "kind": kind},
        )
    return None


def _model_out(model: ModelInfo) -> ModelInfoOut:
    """Render a ModelInfo for the picker, keeping the vendor's own metadata in ``raw``."""
    raw = model.raw if isinstance(model.raw, dict) else {}
    return ModelInfoOut(
        id=str(model.id),
        display_name=str(model.display_name or model.id),
        supports_vision=model.supports_vision,
        supports_tools=model.supports_tools,
        context_length=model.context_length,
        raw=raw,
    )


def _cache_row(model: ModelInfo) -> dict[str, Any]:
    """Render one cached model row — everything the picker needs, without the vendor blob."""
    return {
        "id": str(model.id),
        "display_name": str(model.display_name or model.id),
        "supports_vision": model.supports_vision,
        "supports_tools": model.supports_tools,
        "context_length": model.context_length,
    }


async def _store_models(kind: str, base_url: str, models: list[ModelInfo]) -> None:
    """Cache the last successful listing; a cache write must never fail the request itself."""
    payload = {
        "kind": kind,
        "base_url": base_url,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "models": [_cache_row(model) for model in models],
    }
    try:
        async with session_scope() as session:
            await set_setting(session, MODELS_CACHE_KEY, payload)
    except Exception as exc:  # noqa: BLE001 - the operator already has the list in hand
        log.warning("provider_models_cache_write_failed error=%s", exc)


# --------------------------------------------------------------------------------------
# POST /api/provider/test
# --------------------------------------------------------------------------------------


async def _record_provider_verdict(
    runtime: Runtime | None, *, ok: bool, error: str | None
) -> None:
    """Move the provider dot and persist the verdict so a restart remembers it.

    Without this the dot is written by exactly two things — start-up, which restores the last
    stored verdict, and an agent run, which needs a signal to have arrived first. So an
    operator could test the connection, watch the model list load, pick a model, and still be
    told "no AI provider configured yet". `_restore_status` in main.py even documents that the
    provider routes overwrite the dot; this is the code that makes that true.
    """
    if runtime is not None:
        runtime.set_status("provider", ok, error)
    try:
        from app.main import _persist_state

        await _persist_state(provider_ok=ok, provider_error=error)
    except Exception:  # noqa: BLE001 - the dot is live either way; persistence is a nicety
        log.warning("provider_state_persist_failed", exc_info=True)


@router.post("/test", response_model=ProviderTestOut)
async def test_provider(
    payload: ProviderTestRequest,
    runtime: Runtime = Depends(get_runtime),
) -> Any:
    """List the models an endpoint offers, so the UI can render them searchable and selectable.

    Step 1 of §4.3. The base URL is normalized before anything else, the listing goes through
    ``catalog.list_models`` (which is what tolerates OpenRouter's non-standard envelope), and the
    result is cached so ``GET /api/provider/models`` can serve it later without a round trip.
    """
    kind = payload.kind
    try:
        # The request schema already normalized this; doing it again here is what makes the
        # route correct on its own terms rather than by accident of validation order.
        base_url = normalize_base_url(payload.base_url or "")
    except ConfigError as exc:
        return _failure(exc)

    api_key = await _resolve_api_key(payload.api_key)
    missing = _missing_key(kind, api_key)
    if missing is not None:
        return missing

    try:
        models = await list_models(kind, base_url, api_key, dict(payload.extra_headers))
    except SignalPilotError as exc:
        await _record_provider_verdict(runtime, ok=False, error=exc.message)
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001 - rendered with its real cause below
        await _record_provider_verdict(runtime, ok=False, error=str(exc))
        return _unexpected(exc, action="test")

    await _record_provider_verdict(runtime, ok=True, error=None)

    await _store_models(kind, base_url, models)
    log.info("provider_tested kind=%s count=%d", kind, len(models))
    return ProviderTestOut(
        ok=True,
        models=[_model_out(model) for model in models],
        message=(
            f"{len(models)} models available."
            if models
            else "The endpoint answered, but it lists no models."
        ),
    )


# --------------------------------------------------------------------------------------
# POST /api/provider/probe
# --------------------------------------------------------------------------------------


@router.post("/probe", response_model=CapabilitiesOut)
async def probe_provider(
    payload: ProviderProbeRequest,
    runtime: Runtime = Depends(get_runtime),
) -> Any:
    """Settle whether one model on one endpoint accepts images, tools, and which thinking form.

    Step 4 of §4.3, delegated to ``catalog.probe_model``: one throwaway request with
    ``max_tokens: 16`` (never 1 — a budget of 1 is a hard 400 on every thinking-capable model and
    would report precisely the good models unsupported), a 1x1 PNG and a one-parameter tool.

    The verdict is cached per ``(kind, base_url, model)`` for ``PROBE_CACHE_HOURS``; ``force``
    spends a fresh request instead of reading that cache, which is how the operator re-checks an
    endpoint they have just reconfigured.
    """
    kind = payload.kind
    try:
        base_url = normalize_base_url(payload.base_url or "")
    except ConfigError as exc:
        return _failure(exc)

    api_key = await _resolve_api_key(payload.api_key)
    missing = _missing_key(kind, api_key)
    if missing is not None:
        return missing

    try:
        async with session_scope() as session:
            caps = await probe_model(
                session,
                kind,
                base_url,
                api_key,
                payload.model,
                dict(payload.extra_headers),
                force=payload.force,
            )
    except SignalPilotError as exc:
        await _record_provider_verdict(runtime, ok=False, error=exc.message)
        return _failure(exc)
    except Exception as exc:  # noqa: BLE001 - rendered with its real cause below
        await _record_provider_verdict(runtime, ok=False, error=str(exc))
        return _unexpected(exc, action="probe")

    # A model that answered a probe is a model the loop can use: the dot says so from here,
    # instead of waiting for the first signal to arrive before the operator learns anything.
    await _record_provider_verdict(runtime, ok=True, error=None)
    log.info(
        "provider_probed kind=%s model=%s vision=%s tools=%s thinking=%s source=%s",
        kind,
        payload.model,
        caps.supports_vision,
        caps.supports_tools,
        caps.thinking_form,
        caps.source,
    )
    return CapabilitiesOut(
        supports_vision=bool(caps.supports_vision),
        supports_tools=bool(caps.supports_tools),
        thinking_form=caps.thinking_form,
        source=caps.source,
    )


# --------------------------------------------------------------------------------------
# GET /api/provider/models
# --------------------------------------------------------------------------------------


@router.get("/models", response_model=ProviderModelsOut)
async def models() -> Any:
    """Return the last successful model listing, cached by ``POST /test``.

    Offline by design: reopening the settings page re-renders the picker with the models the
    operator already fetched, instead of spending a provider round trip on every page load. An
    empty list simply means Test has not succeeded yet.
    """
    async with session_scope() as session:
        cached = await get_setting(session, MODELS_CACHE_KEY, None)

    rows: list[Any] = []
    if isinstance(cached, dict):
        rows = cached.get("models") or []
    elif isinstance(cached, list):
        # Tolerated shape: an older build (or a hand-edited row) storing the bare list.
        rows = cached

    out: list[ModelInfoOut] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("id") or "").strip()
        if not identifier:
            continue
        out.append(
            ModelInfoOut(
                id=identifier,
                display_name=str(row.get("display_name") or identifier),
                supports_vision=row.get("supports_vision"),
                supports_tools=row.get("supports_tools"),
                context_length=row.get("context_length"),
                raw=row.get("raw") if isinstance(row.get("raw"), dict) else {},
            )
        )
    return ProviderModelsOut(models=out)
