"""A hiccup at the provider must not cost a signal.

Two real channel signals were lost to a single HTTP 500 from the gateway: nginx returned an
error page, the run ended as `error` after one iteration, and the post was never acted on.
A trade opportunity is time-limited but not instantaneous, so a few seconds of backoff is far
cheaper than dropping it — while a rejected key or an unknown model fails identically forever
and must NOT be retried, because retrying only delays the operator learning what is wrong.
"""

from __future__ import annotations

import pytest

from app.ai.loop import MAX_PROVIDER_RETRIES, _RETRYABLE_PROVIDER_KINDS, _retry_delay
from app.errors import ProviderError


def test_only_transient_kinds_are_retried() -> None:
    """The split is the whole point: retry what might succeed, surface what never will."""
    for kind in ("overloaded", "rate_limit", "transport"):
        assert kind in _RETRYABLE_PROVIDER_KINDS, f"{kind} is transient and must be retried"
    for kind in ("auth", "not_found", "bad_request", "context_window"):
        assert kind not in _RETRYABLE_PROVIDER_KINDS, (
            f"{kind} fails the same way forever; retrying it only delays the real message"
        )


def test_a_gateway_500_is_classified_as_retryable() -> None:
    """The exact failure that lost the two signals."""
    from app.ai.openai_provider import _kind_for_status

    for status in (500, 502, 503, 504, 529):
        assert _kind_for_status(status, "") in _RETRYABLE_PROVIDER_KINDS, (
            f"HTTP {status} from a gateway must be retryable"
        )
    for status in (401, 403, 404):
        assert _kind_for_status(status, "") not in _RETRYABLE_PROVIDER_KINDS


def test_backoff_grows_and_is_bounded() -> None:
    exc = ProviderError("gateway down", status=500, kind="overloaded")
    delays = [_retry_delay(exc, attempt) for attempt in range(MAX_PROVIDER_RETRIES)]
    assert delays == sorted(delays), "each wait must be longer than the last"
    assert all(d <= 30.0 for d in delays), "no single wait may stall the serial ingest queue"
    assert delays[0] <= 5.0, "the first retry should be quick — a signal is time-limited"


def test_an_explicit_retry_after_is_honoured_but_capped() -> None:
    """The server knows better than our backoff — up to a point.

    The ingest queue is strictly serial by design (one run at a time), so a gateway asking for
    ten minutes would hold every later signal hostage behind this one.
    """
    polite = ProviderError("slow down", status=429, kind="rate_limit", detail={"retry_after": 7})
    assert _retry_delay(polite, 0) == pytest.approx(7.0)

    absurd = ProviderError("slow down", status=429, kind="rate_limit", detail={"retry_after": 600})
    assert _retry_delay(absurd, 0) == pytest.approx(30.0)

    nonsense = ProviderError("slow down", status=429, kind="rate_limit", detail={"retry_after": "soon"})
    assert 0 < _retry_delay(nonsense, 0) <= 30.0, "a junk header falls back to the backoff"


def test_the_budget_is_finite() -> None:
    """It retries, then it gives up and says so — a signal is not retried forever."""
    assert 1 <= MAX_PROVIDER_RETRIES <= 5
