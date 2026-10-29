"""Bound expensive password checks without blocking the trading event loop."""
from __future__ import annotations
import asyncio
from typing import Callable
import weakref

from app.util.admin_credentials import is_password_hash


class VerificationBusy(Exception):
    """The bounded password-check workers are busy; try again shortly."""


_slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def verify(check: Callable[..., bool], *args: str) -> bool:
    if not is_password_hash(args[-1]):
        return check(*args)
    loop = asyncio.get_running_loop()
    slots = _slots.setdefault(loop, asyncio.Semaphore(2))
    # Do not queue attacker-controlled jobs behind the CPU workers.
    if slots.locked():
        raise VerificationBusy()
    await slots.acquire()

    def work() -> bool:
        try:
            return check(*args)
        finally:
            # A disconnected/cancelled caller must not release a slot while its KDF still runs.
            try:
                loop.call_soon_threadsafe(slots.release)
            except RuntimeError:
                pass  # The application loop has already shut down.
    return await asyncio.shield(asyncio.to_thread(work))
