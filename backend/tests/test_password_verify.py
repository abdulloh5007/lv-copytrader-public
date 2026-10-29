"""Expensive checks cannot block the event loop, queue indefinitely or escape limits on cancel."""
import asyncio
import threading

import pytest

from app.util.admin_credentials import hash_password
from app.util.password_verify import VerificationBusy, verify


async def test_kdf_workers_are_bounded_even_when_a_caller_cancels():
    record = hash_password('bounded-worker-test-password')
    release = threading.Event()
    entered = threading.Event()
    lock = threading.Lock()
    counter = [0]
    def slow(*args):
        with lock:
            counter[0] += 1
            if counter[0] == 2:entered.set()
        assert release.wait(5)
        return True
    first = asyncio.create_task(verify(slow,'candidate',record))
    second = asyncio.create_task(verify(slow,'candidate',record))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        # This coroutine can run while both verifiers are occupied in worker threads.
        assert asyncio.get_running_loop().is_running()
        with pytest.raises(VerificationBusy):await verify(slow,'candidate',record)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):await first
        with pytest.raises(VerificationBusy):await verify(slow,'candidate',record)
    finally:
        release.set()
        await second
    for _ in range(20):
        try:
            assert await verify(lambda *args:True,'candidate',record)
            break
        except VerificationBusy:
            await asyncio.sleep(.01)
    else:
        pytest.fail('Password worker slots were not released')
