"""Error hierarchy, MT5 retcode sentences, and the HTTP mapping used by the REST layer.

This module is the leaf of the backend dependency graph: it imports nothing from
``app.*`` so that every other module (``app.trading.constants`` included) can import
it without a cycle. That is also why the retcode sentences live here as their own
table instead of being derived from ``app.trading.constants.RETCODE``.
"""

from __future__ import annotations

from typing import Any, Final


class SignalPilotError(Exception):
    """Base class for every error LV Copytrader raises on purpose."""

    code: str = "error"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message: str = message
        self.detail: dict[str, Any] = dict(detail or {})

    def __str__(self) -> str:
        """Return the human message, so ``str(exc)`` is always the sentence we wrote."""
        return self.message

    def __repr__(self) -> str:
        """Return a debug form carrying the code and the detail dict."""
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r}, detail={self.detail!r})"


class TradingError(SignalPilotError):
    """Something in the trading path is wrong and the caller must fix the request."""

    code: str = "trading_error"


class RpcError(TradingError):
    """The MT5 sidecar failed the call: transport, auth, or an ``ok: false`` body."""

    def __init__(
        self,
        message: str,
        *,
        rpc_code: int = 0,
        retcode: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = dict(detail or {})
        merged["rpc_code"] = rpc_code
        if retcode is not None:
            merged["retcode"] = retcode
        super().__init__(message, detail=merged)
        # rpc_code mirrors the sidecar's error.code: -1 transport, -2 auth, -3 mt5 call returned None.
        self.rpc_code: int = rpc_code
        self.retcode: int | None = retcode


class ProviderError(SignalPilotError):
    """An LLM provider call failed; ``kind`` is what the UI and ``to_http`` branch on."""

    code: str = "provider_error"

    # kind in {"auth","not_found","rate_limit","overloaded","bad_request",
    #          "transport","decode","context_window","unknown"}
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        kind: str = "unknown",
        detail: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = dict(detail or {})
        merged["kind"] = kind
        if status is not None:
            merged["status"] = status
        super().__init__(message, detail=merged)
        self.status: int | None = status
        self.kind: str = kind


class IngestError(SignalPilotError):
    """A Telegram message could not be classified, downloaded, or persisted."""

    code: str = "ingest_error"


class ConfigError(SignalPilotError):
    """A setting is missing, malformed, or contradicts another setting."""

    code: str = "config_error"


# --------------------------------------------------------------------------------------
# Retcode sentences.
#
# Every key of app.trading.constants.RETCODE is covered here, plus the other retcodes a
# live broker actually returns, so the model always reads a sentence it can act on rather
# than a bare number. Each entry is a lower-case fragment that a caller may extend with
# real figures, e.g. retcode_message(10019) + ": need 412.30 USD, have 180.05".
# --------------------------------------------------------------------------------------
_RETCODE_MESSAGES: Final[dict[int, str]] = {
    0: "the request passed validation (order_check reports success as retcode 0, not 10009)",
    10004: "the broker requoted: the price moved between the check and the send — re-read the tick and retry",
    10006: "the broker rejected the request — check that the symbol is tradable on this account",
    10007: "the request was cancelled in the terminal",
    10008: "the pending order was placed and is now waiting for its price",
    10009: "the request was executed in full",
    10010: "only part of the requested volume was filled — the remainder was rejected",
    10011: "the terminal failed to process the request (internal terminal error) — retry once",
    10012: "the request timed out — check whether it reached the server before resending it",
    10013: "the request is malformed: a required field is missing or holds an impossible value",
    10014: "invalid volume: it is below volume_min, above volume_max, or not a multiple of volume_step",
    10015: "invalid price: it is zero, carries more digits than the symbol allows, or sits on the wrong side of the market for this order type",
    10016: "invalid stops: the SL/TP is too close to the market or on the wrong side",
    10017: "trading is disabled for this account by the broker",
    10018: "the market is closed for this symbol",
    10019: "not enough free margin",
    10020: "the price changed between the check and the send — re-read the tick and retry",
    10021: "there are no quotes to process the request — the symbol has no live price right now",
    10022: "invalid expiration: this symbol does not accept that expiry — use GTC or a later time",
    10023: "the order changed underneath the request — re-read the order and retry",
    10024: "too many requests — slow down and retry",
    10025: "nothing changed: the values sent are already the current ones",
    10026: "the broker's server disabled algorithmic trading for this account",
    10027: "the terminal disabled algorithmic trading (Tools > Options > Expert Advisors)",
    10028: "the request is locked for processing by the dealer — wait and retry",
    10029: "the order or position is frozen: it is inside trade_freeze_level and cannot be modified until price moves away",
    10030: "unsupported filling mode for this symbol",
    10031: "no connection to the trade server",
    10032: "the operation is allowed on live accounts only",
    10033: "the account has reached the broker's limit on pending orders",
    10034: "the volume would exceed the broker's total volume limit for this symbol",
    10035: "the order type is invalid for this request",
    10036: "the position named by this ticket is already closed",
    10038: "the close volume is larger than the position's remaining volume",
    10039: "a close order already exists for this position",
    10040: "the account has reached the broker's limit on open positions",
    10041: "the pending order was rejected on activation and has been cancelled",
    10042: "only long positions are allowed on this symbol",
    10043: "only short positions are allowed on this symbol",
    10044: "only closing an existing position is allowed on this symbol",
    10045: "this account closes positions FIFO: close the oldest position on this symbol first",
    10046: "opposite positions on one symbol are prohibited on this account",
}

_NO_RESULT_MESSAGE: Final[str] = (
    "the terminal returned no result — the request failed before it reached the trade server "
    "(check the MT5 connection and the request fields)"
)

_PROVIDER_KIND_STATUS: Final[dict[str, int]] = {
    "auth": 401,
    "not_found": 404,
    "rate_limit": 429,
    "overloaded": 503,
    "bad_request": 400,
    "transport": 502,
}


def retcode_message(retcode: int | None, comment: str = "") -> str:
    """Return an actionable English sentence for an MT5 retcode, with the broker comment appended."""
    if retcode is None:
        base = _NO_RESULT_MESSAGE
    else:
        base = _RETCODE_MESSAGES.get(
            retcode, f"the broker rejected the request with retcode {retcode}"
        )
    text = (comment or "").strip()
    if text:
        return f"{base} (broker: {text})"
    return base


def to_http(exc: SignalPilotError) -> tuple[int, dict[str, Any]]:
    """Map an error onto its HTTP status and the ``{"error": {...}}`` body the API returns."""
    if isinstance(exc, ProviderError):
        status = _PROVIDER_KIND_STATUS.get(exc.kind, 500)
    elif isinstance(exc, (TradingError, ConfigError, IngestError)):
        status = 400
    else:
        status = 500
    body: dict[str, Any] = {
        "error": {"code": exc.code, "message": exc.message, "detail": exc.detail}
    }
    return status, body
