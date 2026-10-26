"""In-process event bus: typed envelopes fanned out to the dashboard WebSocket."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from typing import Any, Final

from app.config import settings

SIGNAL_RECEIVED: Final[str] = "signal.received"
RUN_STARTED: Final[str] = "run.started"
RUN_THINKING: Final[str] = "run.thinking"
RUN_TEXT: Final[str] = "run.text"
RUN_TOOL_CALL: Final[str] = "run.tool_call"
RUN_TOOL_RESULT: Final[str] = "run.tool_result"
RUN_FINISHED: Final[str] = "run.finished"
PROVIDER_REQUEST: Final[str] = "provider.request"
ORDER_PLACED: Final[str] = "order.placed"
ORDER_CANCELLED: Final[str] = "order.cancelled"
POSITION_OPENED: Final[str] = "position.opened"
POSITION_MODIFIED: Final[str] = "position.modified"
POSITION_CLOSED: Final[str] = "position.closed"
ACCOUNT_TICK: Final[str] = "account.tick"
SYSTEM_WARNING: Final[str] = "system.warning"
STATUS_CHANGED: Final[str] = "status.changed"
SETTINGS_CHANGED: Final[str] = "settings.changed"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        SIGNAL_RECEIVED,
        RUN_STARTED,
        RUN_THINKING,
        RUN_TEXT,
        RUN_TOOL_CALL,
        RUN_TOOL_RESULT,
        RUN_FINISHED,
        PROVIDER_REQUEST,
        ORDER_PLACED,
        ORDER_CANCELLED,
        POSITION_OPENED,
        POSITION_MODIFIED,
        POSITION_CLOSED,
        ACCOUNT_TICK,
        SYSTEM_WARNING,
        STATUS_CHANGED,
        SETTINGS_CHANGED,
    }
)

#: How many envelopes are kept for late subscribers / the WebSocket hello frame.
RECENT_EVENTS: Final[int] = 500

#: Default number of envelopes returned by :meth:`EventBus.recent`.
RECENT_DEFAULT: Final[int] = 200


class EventBus:
    """Synchronous fan-out of event envelopes to bounded per-subscriber queues."""

    def __init__(self, queue_max: int | None = None, recent_max: int = RECENT_EVENTS) -> None:
        self._queue_max: int = int(queue_max if queue_max is not None else settings.EVENT_QUEUE_MAX)
        self._recent_max: int = int(recent_max)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._recent: deque[dict[str, Any]] = deque(maxlen=self._recent_max)
        self._seq = itertools.count(1)
        self.dropped: int = 0

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber queue and return it."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_max)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Drop a subscriber queue; unknown queues are ignored."""
        self._subscribers.discard(q)

    def emit(self, type: str, **data: Any) -> None:
        """Broadcast one event; never blocks and never raises."""
        try:
            envelope: dict[str, Any] = {
                "type": type,
                "ts": time.time(),
                "seq": next(self._seq),
                "data": data,
            }
            self._recent.append(envelope)
            for queue in tuple(self._subscribers):
                if not self._deliver(queue, envelope):
                    self.dropped += 1
        except Exception:  # noqa: BLE001 - emitting must never break a caller
            self.dropped += 1

    def recent(self, limit: int = RECENT_DEFAULT) -> list[dict[str, Any]]:
        """Return up to `limit` most recent envelopes, oldest first."""
        if limit <= 0:
            return []
        items = list(self._recent)
        return items[-limit:]

    def subscriber_count(self) -> int:
        """Return how many subscriber queues are currently attached."""
        return len(self._subscribers)

    def clear(self) -> None:
        """Forget every buffered envelope and detach every subscriber (tests only)."""
        self._recent.clear()
        self._subscribers.clear()
        self.dropped = 0

    def _deliver(self, queue: asyncio.Queue[dict[str, Any]], envelope: dict[str, Any]) -> bool:
        """Put one envelope on a queue, evicting its oldest item once when full.

        Evicting counts as a drop: the subscriber loses an event either way, and a
        counter that only moved when delivery failed outright would read 0 through
        every overflow -- which is precisely when someone is looking at it.
        """
        try:
            queue.put_nowait(envelope)
            return True
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return False
            self.dropped += 1
            try:
                queue.put_nowait(envelope)
                return True
            except asyncio.QueueFull:
                return False


bus = EventBus()


def emit(type: str, **data: Any) -> None:
    """Emit an event on the module-level singleton bus."""
    bus.emit(type, **data)
