"""Failing fast when the sidecar is down, instead of paying the timeout on every request.

Found by actually running the app: with no sidecar reachable, `GET /api/trading/positions`
returned nothing for over a minute. The route's DB fallback was correct — it was just sitting
behind RPC_TIMEOUT_SECONDS times three attempts, on every single request. The dashboard looked
frozen, which is the opposite of what the degraded path exists for.

The fix belongs in the client, not the route: one process learns the sidecar is unreachable
once, and every caller after that — the read routes, the account tick, the SL watchdog — fails
in microseconds until a trial call says otherwise.
"""

from __future__ import annotations

import time

import pytest

from app.errors import RpcError
from app.trading.rpc import BREAKER_COOLDOWN_SECONDS, BREAKER_FAILURES, Mt5Rpc


def _rpc() -> Mt5Rpc:
    return Mt5Rpc("http://mt5-sidecar:18812", "token", timeout=30.0, connect_timeout=10.0)


async def test_the_breaker_opens_after_consecutive_transport_failures() -> None:
    """Two dead calls are enough to stop paying the timeout a third time."""
    rpc = _rpc()
    assert rpc.breaker_open is False

    for _ in range(BREAKER_FAILURES):
        rpc._breaker_record(ok=False, transport=True)

    assert rpc.breaker_open is True, "the breaker must open once the sidecar looks dead"

    started = time.monotonic()
    with pytest.raises(RpcError) as excinfo:
        await rpc.call("account_info")
    elapsed = time.monotonic() - started

    assert elapsed < 0.05, f"a short-circuited call took {elapsed:.3f}s; it should be instant"
    assert excinfo.value.detail.get("breaker") == "open"
    assert excinfo.value.detail.get("retryable") is False
    assert "unreachable" in excinfo.value.message


async def test_a_sidecar_that_answers_never_opens_the_breaker() -> None:
    """An ok:false body or a rejected token means the sidecar is UP and talking."""
    rpc = _rpc()
    for _ in range(BREAKER_FAILURES * 3):
        rpc._breaker_record(ok=False, transport=False)
    assert rpc.breaker_open is False, (
        "a broker-side refusal is an answer, not an outage — writing the sidecar off for it "
        "would blind the app to a terminal that is running fine"
    )


async def test_one_success_closes_the_breaker() -> None:
    """Recovery needs no restart and no timer to expire."""
    rpc = _rpc()
    for _ in range(BREAKER_FAILURES):
        rpc._breaker_record(ok=False, transport=True)
    assert rpc.breaker_open is True

    rpc._breaker_record(ok=True)
    assert rpc.breaker_open is False
    assert rpc._consecutive_failures == 0


async def test_the_breaker_lets_a_trial_call_through_after_the_cooldown() -> None:
    """It must not latch open: the sidecar comes back on its own after a restart."""
    rpc = _rpc()
    for _ in range(BREAKER_FAILURES):
        rpc._breaker_record(ok=False, transport=True)
    assert rpc.breaker_open is True

    # Wind the deadline into the past rather than sleeping the cooldown out.
    rpc._breaker_open_until = time.monotonic() - 0.01
    assert rpc.breaker_open is False, "after the cooldown one call must be allowed through"
    assert BREAKER_COOLDOWN_SECONDS > 0
