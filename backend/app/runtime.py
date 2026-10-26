"""Shared, typed service container held on `app.state.runtime`."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import Request

from app import events
from app.config import Settings
from app.config import settings as _settings
from app.db import get_setting, needs_reentry_keys, session_scope, set_setting
from app.errors import ConfigError
from app.events import EventBus
from app.util.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - imports exist for type checkers only
    from app.ai.base import Capabilities, Provider
    from app.reviewer.worker import ChannelReviewer
    from app.telegram.client import TelegramService
    from app.telegram.ingest import Ingestor
    from app.telegram.notifier import Notifier
    from app.trading.executor import Executor
    from app.trading.rpc import Mt5Rpc
    from app.trading.symbols import SymbolResolver

log = get_logger(__name__)

#: The three status dots shown in the cockpit header.
STATUS_KEYS: tuple[str, ...] = ("mt5", "telegram", "provider")

#: Provider kinds that can be built from settings.
PROVIDER_KINDS: frozenset[str] = frozenset({"anthropic", "openai"})


def _default_status() -> dict[str, Any]:
    """Return the initial status map with every dot red and nothing to re-enter."""
    status: dict[str, Any] = {key: {"ok": False, "error": None} for key in STATUS_KEYS}
    status["armed"] = False
    status["needs_reentry"] = []
    return status


@dataclass
class Runtime:
    """Every long-lived service the request handlers and background tasks share."""

    settings: Settings = field(default_factory=lambda: _settings)
    rpc: Mt5Rpc = None  # type: ignore[assignment]
    resolver: SymbolResolver = None  # type: ignore[assignment]
    executor: Executor = None  # type: ignore[assignment]
    telegram: TelegramService = None  # type: ignore[assignment]
    ingestor: Ingestor = None  # type: ignore[assignment]
    reviewer: ChannelReviewer = None  # type: ignore[assignment]
    notifier: Notifier = None  # type: ignore[assignment]
    bus: EventBus = field(default_factory=lambda: events.bus)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=_default_status)
    started_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Initialise the private provider cache."""
        self._provider_cache_key: str | None = None
        self._provider_cached: Any = None

    # ---- armed state -------------------------------------------------------

    async def armed(self) -> bool:
        """Read the persisted `trading.armed` flag, failing closed when unreadable."""
        try:
            async with session_scope() as session:
                value = await get_setting(session, "trading.armed", False)
        except Exception as exc:  # noqa: BLE001 - a broken store must not arm the system
            log.warning("armed_read_failed error=%s", exc)
            return False
        armed = bool(value)
        self.status["armed"] = armed
        return armed

    async def set_armed(self, value: bool) -> None:
        """Persist the armed flag and broadcast the new status."""
        armed = bool(value)
        async with session_scope() as session:
            await set_setting(session, "trading.armed", armed)
        self.status["armed"] = armed
        self.bus.emit(events.STATUS_CHANGED, key="armed", status=self.status_payload(), armed=armed)

    # ---- status ------------------------------------------------------------

    def set_status(self, key: str, ok: bool, error: str | None = None) -> None:
        """Update one status entry and emit STATUS_CHANGED only when it changed."""
        entry: dict[str, Any] = {"ok": bool(ok), "error": error}
        if self.status.get(key) == entry:
            return
        self.status[key] = entry
        self.bus.emit(
            events.STATUS_CHANGED,
            key=key,
            status=self.status_payload(),
            armed=bool(self.status.get("armed", False)),
        )

    def status_payload(self) -> dict[str, Any]:
        """Return a copy of the cached status map, safe to serialise."""
        payload: dict[str, Any] = {}
        for key in STATUS_KEYS:
            entry = self.status.get(key)
            if isinstance(entry, dict):
                payload[key] = {"ok": bool(entry.get("ok", False)), "error": entry.get("error")}
            else:
                payload[key] = {"ok": False, "error": None}
        payload["armed"] = bool(self.status.get("armed", False))
        needs = self.status.get("needs_reentry") or []
        payload["needs_reentry"] = list(needs)
        return payload

    async def snapshot_status(self) -> dict[str, Any]:
        """Refresh armed + needs_reentry from the database and return the full status."""
        armed = await self.armed()
        try:
            async with session_scope() as session:
                keys = await needs_reentry_keys(session)
        except Exception as exc:  # noqa: BLE001 - health must answer even with a bad store
            log.warning("needs_reentry_read_failed error=%s", exc)
            keys = list(self.status.get("needs_reentry") or [])
        self.status["needs_reentry"] = list(keys)
        payload = self.status_payload()
        payload["armed"] = armed
        return payload

    # ---- provider ----------------------------------------------------------

    async def provider(self) -> tuple[Provider, str, Capabilities] | None:
        """Build or reuse the configured provider and return (provider, model, capabilities)."""
        async with session_scope() as session:
            kind = str(await get_setting(session, "provider.kind", "") or "")
            base_url = str(await get_setting(session, "provider.base_url", "") or "")
            api_key = str(await get_setting(session, "provider.api_key", "") or "")
            model = str(await get_setting(session, "provider.model", "") or "")
            raw_headers = await get_setting(session, "provider.extra_headers", {})
            extra_headers: dict[str, str] = {}
            if isinstance(raw_headers, dict):
                extra_headers = {str(k): str(v) for k, v in raw_headers.items()}
            if kind not in PROVIDER_KINDS or not model:
                return None
            if kind == "openai" and not base_url:
                return None
            if kind == "anthropic" and not api_key:
                return None
            # Imported here, below the guards: an unconfigured provider answers None without
            # ever touching the provider modules.
            from app.ai.catalog import effective_capabilities

            caps = await effective_capabilities(session, kind, base_url, api_key, model, extra_headers)

        from app.ai.base import build_provider

        cache_key = self._provider_cache_id(kind, base_url, api_key, model, extra_headers)
        if self._provider_cached is None or cache_key != self._provider_cache_key:
            stale = self._provider_cached
            provider = build_provider(kind, base_url=base_url, api_key=api_key, extra_headers=extra_headers)
            self._provider_cached = provider
            self._provider_cache_key = cache_key
            if stale is not None:
                await self._release_provider(stale)
        return self._provider_cached, model, caps

    def invalidate_provider(self) -> None:
        """Forget the cached provider so the next call rebuilds it from settings."""
        self._provider_cached = None
        self._provider_cache_key = None

    @staticmethod
    def _provider_cache_id(
        kind: str, base_url: str, api_key: str, model: str, extra_headers: dict[str, str]
    ) -> str:
        """Return a cache identity that changes whenever any provider input changes."""
        key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        headers = json.dumps(extra_headers, sort_keys=True, ensure_ascii=False)
        headers_hash = hashlib.sha256(headers.encode("utf-8")).hexdigest()[:16]
        return f"{kind}|{base_url}|{key_hash}|{model}|{headers_hash}"

    @staticmethod
    async def _release_provider(provider: Any) -> None:
        """Best-effort shutdown of a provider that has just been replaced."""
        closer = getattr(provider, "aclose", None) or getattr(provider, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - a stale client must not break the new one
            log.warning("provider_release_failed error=%s", exc)


def get_runtime(request: Request) -> Runtime:
    """FastAPI dependency returning the Runtime stored on the application state."""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise ConfigError("runtime is not initialised")
    return runtime
