"""Bounded per-process rate limits and signed anonymous viewer leases."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from collections import OrderedDict
from typing import Callable


class Buckets:
    """Token buckets with bounded memory and O(1) admission/expiry."""

    def __init__(self, capacity=60.0, refill=6.0, max_keys=8192, clock=time.monotonic):
        self.capacity, self.refill, self.max_keys = capacity, refill, max_keys
        self.clock = clock
        self.entries: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def admit(self, key: str) -> int:
        now = self.clock()
        while self.entries and next(iter(self.entries.values()))[1] < now - 600:
            self.entries.popitem(last=False)
        previous = self.entries.pop(key, None)
        if previous is None:
            if len(self.entries) >= self.max_keys:
                return 60
            tokens = self.capacity
        else:
            tokens = min(self.capacity, previous[0] + max(0, now - previous[1]) * self.refill)
        wait = 0 if tokens >= 1 else max(1, math.ceil((1 - tokens) / self.refill))
        self.entries[key] = (tokens - 1 if not wait else tokens, now)
        return wait


class PublicLimits:
    def __init__(self, clock=time.monotonic):
        self.global_bucket = Buckets(3000, 1500, 1, clock)
        self.peers = Buckets(120, 12, 8192, clock)
        self.issuance = Buckets(20, 0.5, 8192, clock)
        self.salt = secrets.token_bytes(32)

    def peer(self, address: str) -> str:
        return hmac.new(self.salt, address.encode(), hashlib.sha256).hexdigest()

    def admit(self, peer: str) -> int:
        return self.global_bucket.admit("all") or self.peers.admit(peer)


class Viewers:
    COOKIE = "lvct_demo_viewer"
    COOKIE_TTL = 86400
    LEASE = 50.0

    def __init__(
        self, secret: Callable[[], str], clock=time.monotonic, wall=time.time, maximum=10000
    ):
        self.secret, self.clock, self.wall, self.maximum = secret, clock, wall, maximum
        self.active: OrderedDict[str, float] = OrderedDict()

    def _signature(self, payload: str) -> str:
        return hmac.new(
            self.secret().encode(), ("public-demo-viewer-v1:" + payload).encode(), hashlib.sha256
        ).hexdigest()

    def valid(self, token: str | None) -> bool:
        if not token or len(token) > 128 or not token.isascii():
            return False
        parts = token.split(".")
        if len(parts) != 3 or len(parts[0]) > 12 or not parts[0].isdigit() or len(parts[1]) != 32:
            return False
        try:
            int(parts[1], 16)
        except ValueError:
            return False
        payload = parts[0] + "." + parts[1]
        return 0 <= self.wall() - int(parts[0]) < self.COOKIE_TTL and hmac.compare_digest(
            parts[2], self._signature(payload)
        )

    def mint(self) -> str:
        payload = f"{int(self.wall())}.{secrets.token_hex(16)}"
        return payload + "." + self._signature(payload)

    def prune(self) -> None:
        now = self.clock()
        while self.active and next(iter(self.active.values())) <= now:
            self.active.popitem(last=False)

    @property
    def count(self) -> int:
        self.prune()
        return len(self.active)

    def touch(self, token: str) -> bool:
        self.prune()
        if token not in self.active and len(self.active) >= self.maximum:
            return False
        self.active[token] = self.clock() + self.LEASE
        self.active.move_to_end(token)
        return True
