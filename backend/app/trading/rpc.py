"""Async JSON-RPC client for the MT5 sidecar, plus the ``dict -> SimpleNamespace`` conversion.

Everything the backend knows about MetaTrader 5 arrives through this module. The sidecar runs
the Windows-only ``MetaTrader5`` package under Wine and exposes exactly one endpoint::

    POST {SIDECAR_URL}/rpc      header X-SP-Token: <SIDECAR_TOKEN>
         {"method": "<name>", "params": {...}}
      200 -> {"ok": true,  "result": <json>}
      200 -> {"ok": false, "error": {"code": <int>, "message": "<str>", "retcode": <int|null>}}
      401 -> missing or wrong token

Three properties of that contract shape this file.

*Results are objects, not dicts.* :func:`to_ns` turns every JSON object inside a result into a
:class:`types.SimpleNamespace` and every array into a tuple, which is why every caller writes
``si.digits`` and ``p.ticket``. A missing field then raises ``AttributeError`` exactly where a
dict ``.get()`` would have handed the money path a silent ``None`` (BUILD_PROMPT §1.1).
:meth:`Mt5Rpc.call_raw` is the escape hatch for payloads that are plain data — the numeric rows
of ``copy_rates_from_pos``, the bool of ``symbol_select``, the float of ``order_calc_margin``.

*A null result is a real answer, not an error.* ``order_send`` returns ``None`` when the request
failed before it reached the trade server (BUILD_PROMPT §1.3) and ``symbol_info`` returns ``None``
for a symbol the broker does not offer. Neither raises here: ``to_ns(None) is None``, so a null
result travels through :meth:`Mt5Rpc.call` unchanged and the caller null-checks it.

*The sidecar is single-threaded on purpose.* Its ``http.server.HTTPServer`` thread is what
serialises MT5's IPC channel, which is not thread-safe (BUILD_PROMPT §1). So this client must
never fan out: the connection pool is capped at a single connection, and ``order_send`` /
``order_check`` are additionally serialised through an :class:`asyncio.Lock` so two agent runs
can never interleave a write.

Retries follow from the same reasoning. Only the idempotent read methods in
:data:`RETRYABLE_METHODS` are retried, at most twice, with a 0.25 s then 1 s backoff, and only
after a transport failure (``rpc_code == -1``). ``order_send`` and ``order_check`` are never
retried: a timeout does not mean the order was not filled, and a retried ``order_send`` is a
duplicate trade.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Final

import httpx

from app.config import settings
from app.errors import RpcError
from app.util.logging import get_logger

__all__ = ["to_ns", "Mt5Rpc", "RETRYABLE_METHODS", "WRITE_METHODS", "RETRY_BACKOFF_SECONDS"]

log = get_logger(__name__)

#: Path of the sidecar's single endpoint, joined onto ``SIDECAR_URL``.
RPC_PATH: Final[str] = "/rpc"

#: Shared-secret header the sidecar checks with ``hmac.compare_digest`` on every request.
TOKEN_HEADER: Final[str] = "X-SP-Token"

# rpc_code values this client produces itself. The sidecar's own ``error.code`` is passed
# through untouched; these three are the codes for failures that never reach MT5 at all.
RPC_CODE_TRANSPORT: Final[int] = -1
RPC_CODE_AUTH: Final[int] = -2
RPC_CODE_NULL_RESULT: Final[int] = -3

#: Idempotent reads. Re-running any of these cannot change the account, so a transport
#: failure may be retried. Every write method is deliberately absent.
RETRYABLE_METHODS: Final[frozenset[str]] = frozenset(
    {
        "account_info",
        "terminal_info",
        "symbols_get",
        "symbol_info",
        "symbol_info_tick",
        "positions_get",
        "orders_get",
        "history_deals_get",
        "copy_rates_from_pos",
        "ping",
        "order_calc_margin",
    }
)

#: Methods that must be serialised against each other and must never be retried.
#:
#: `relogin` joins the two order methods here for both reasons: it tears the MT5 channel down
#: and rebuilds it, so it must not run while an order is in flight, and a retried login is a
#: second authentication attempt against a broker that may lock the account for them.
WRITE_METHODS: Final[frozenset[str]] = frozenset({"order_send", "order_check", "relogin"})

#: Backoff before retry 1 and retry 2 of a retryable method — at most two retries.
RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (0.25, 1.0)

#: Consecutive transport failures before the breaker opens, and how long it stays open.
#:
#: Without this, every read route pays the full timeout budget — RPC_TIMEOUT_SECONDS times
#: three attempts — on every single request while the sidecar is down. Measured against a
#: dead sidecar, `GET /api/trading/positions` took over a minute and the dashboard looked
#: frozen, even though the route's DB fallback was correct and waiting behind it. One process
#: learns the sidecar is unreachable once, and every caller after that fails in microseconds
#: until a trial call says otherwise.
BREAKER_FAILURES: Final[int] = 2
BREAKER_COOLDOWN_SECONDS: Final[float] = 15.0

#: How much of a non-JSON or error body is quoted back to the operator.
BODY_EXCERPT_CHARS: Final[int] = 200


def to_ns(obj: Any) -> Any:
    """Convert a decoded JSON result into namespaces so callers use attribute access.

    ``dict`` becomes a :class:`types.SimpleNamespace` whose values are converted in turn;
    ``list``/``tuple`` become a ``tuple`` of converted items (so an array of MT5 objects is a
    tuple of namespaces and an array of numbers stays a tuple of numbers); every scalar,
    ``None`` included, is returned unchanged. That last clause is load-bearing:
    ``to_ns(None) is None`` is what lets a null ``order_send`` result reach its caller as
    ``None`` rather than as an empty namespace that would answer every attribute with
    ``AttributeError`` one line too late.
    """
    if isinstance(obj, dict):
        return SimpleNamespace(**{str(key): to_ns(value) for key, value in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(to_ns(item) for item in obj)
    return obj


def _excerpt(body: str) -> str:
    """Collapse whitespace in a response body and cut it to the quotable prefix."""
    if not body:
        return ""
    return " ".join(body.split())[:BODY_EXCERPT_CHARS]


def _response_excerpt(response: httpx.Response) -> str:
    """Return the quotable prefix of a response body, or ``""`` if it cannot be read."""
    try:
        return _excerpt(response.text)
    except (UnicodeDecodeError, httpx.HTTPError, ValueError):
        return ""


def _compact(payload: Any) -> str:
    """Render a request dict on one line for the INFO log, never raising on odd values."""
    try:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(payload)


class Mt5Rpc:
    """HTTP client for the MT5 sidecar: one endpoint, one connection, one write at a time.

    The client is created in :meth:`start` and released in :meth:`close`, both driven from
    FastAPI's lifespan, so no socket is opened at import time and none is left behind on
    shutdown. Calling any method before :meth:`start` raises :class:`RpcError` naming the fix
    rather than opening a connection behind the caller's back.
    """

    def __init__(
        self,
        url: str,
        token: str,
        timeout: float = settings.RPC_TIMEOUT_SECONDS,
        connect_timeout: float = settings.RPC_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        """Store the endpoint and its shared secret; no I/O happens until :meth:`start`."""
        self.url: str = str(url).rstrip("/")
        self.token: str = str(token)
        self.timeout: float = float(timeout)
        self.connect_timeout: float = float(connect_timeout)
        self._client: httpx.AsyncClient | None = None
        # Serialises order_send/order_check across concurrent agent runs. The sidecar would
        # serialise them anyway, but doing it here keeps a check and its send from being
        # separated by another run's write on the same account.
        self._write_lock: asyncio.Lock = asyncio.Lock()
        # Circuit breaker: consecutive transport failures, and when the breaker may next
        # let a single trial call through.
        self._consecutive_failures: int = 0
        self._breaker_open_until: float = 0.0

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Create the HTTP client. Idempotent, so a reconnect path may call it again."""
        if self._client is not None and not self._client.is_closed:
            return
        self._client = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
            headers={
                TOKEN_HEADER: self.token,
                "content-type": "application/json",
                "accept": "application/json",
            },
            # One connection, because the sidecar answers one request at a time anyway and a
            # pool would only queue requests inside a lock we cannot see.
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )
        log.info("MT5 RPC client ready: %s%s", self.url, RPC_PATH)

    async def close(self) -> None:
        """Close the HTTP client and drop it. Safe to call when never started."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
            log.info("MT5 RPC client closed")

    def _require_client(self) -> httpx.AsyncClient:
        """Return the live client or explain that the lifespan never started it.

        This is a wiring mistake, not a flaky network, so the error is marked unretryable:
        sleeping through the backoff schedule would only delay the message by 1.25 s.
        """
        client = self._client
        if client is None or client.is_closed:
            raise RpcError(
                "the MT5 RPC client is not started — call await rpc.start() from the app lifespan",
                rpc_code=RPC_CODE_TRANSPORT,
                detail={"retryable": False},
            )
        return client

    # ------------------------------------------------------------------ core
    async def call(self, method: str, **params: Any) -> Any:
        """Call the sidecar and return the result as namespaces (``None`` stays ``None``)."""
        return to_ns(await self._request(method, params))

    async def call_raw(self, method: str, **params: Any) -> Any:
        """Call the sidecar and return the decoded JSON untouched (rates, bools, floats)."""
        return await self._request(method, params)

    @property
    def breaker_open(self) -> bool:
        """Whether the sidecar is currently written off as unreachable."""
        return time.monotonic() < self._breaker_open_until

    def _breaker_check(self, method: str) -> None:
        """Fail immediately while the breaker is open, instead of paying the timeout again."""
        remaining = self._breaker_open_until - time.monotonic()
        if remaining <= 0.0:
            return
        raise RpcError(
            f"the MT5 sidecar is unreachable — not retrying {method} for another "
            f"{remaining:.0f}s",
            rpc_code=RPC_CODE_TRANSPORT,
            detail={"method": method, "breaker": "open", "retry_in_seconds": round(remaining, 1),
                    "retryable": False},
        )

    def _breaker_record(self, *, ok: bool, transport: bool = False) -> None:
        """Close the breaker on any success; open it after enough transport failures."""
        if ok:
            if self._consecutive_failures or self._breaker_open_until:
                log.info("rpc sidecar reachable again; breaker closed")
            self._consecutive_failures = 0
            self._breaker_open_until = 0.0
            return
        if not transport:
            # An ok:false body or an auth failure means the sidecar ANSWERED. It is up.
            self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= BREAKER_FAILURES and not self.breaker_open:
            self._breaker_open_until = time.monotonic() + BREAKER_COOLDOWN_SECONDS
            log.warning(
                "rpc sidecar unreachable %d times in a row; failing fast for %.0fs",
                self._consecutive_failures,
                BREAKER_COOLDOWN_SECONDS,
            )

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        """Dispatch one method, holding the write lock and applying the retry policy."""
        self._breaker_check(method)
        if method in WRITE_METHODS:
            # Never retried, always serialised: a second order_send for one intent is a
            # second trade, and there is no way to tell a lost reply from a lost order.
            async with self._write_lock:
                try:
                    result = await self._post(method, params)
                except RpcError as exc:
                    self._breaker_record(ok=False, transport=exc.rpc_code == RPC_CODE_TRANSPORT)
                    raise
                self._breaker_record(ok=True)
                return result
        return await self._with_retries(method, params)

    async def _with_retries(self, method: str, params: dict[str, Any]) -> Any:
        """Post ``method``, retrying transport failures when the method is idempotent."""
        retries = len(RETRY_BACKOFF_SECONDS) if method in RETRYABLE_METHODS else 0
        attempt = 0
        while True:
            try:
                result = await self._post(method, params)
            except RpcError as exc:
                self._breaker_record(ok=False, transport=exc.rpc_code == RPC_CODE_TRANSPORT)
                # Only a transport failure is retryable: an ok:false body is the broker's
                # considered answer, an auth failure will fail identically forever, and a
                # client that was never started will not start itself between attempts.
                retryable = exc.rpc_code == RPC_CODE_TRANSPORT and exc.detail.get("retryable", True)
                if not retryable or attempt >= retries or self.breaker_open:
                    raise
                delay = RETRY_BACKOFF_SECONDS[attempt]
                attempt += 1
                log.warning(
                    "rpc %s failed (%s); retry %d/%d in %.2fs",
                    method,
                    exc.message,
                    attempt,
                    retries,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                self._breaker_record(ok=True)
                return result

    async def _post(self, method: str, params: dict[str, Any]) -> Any:
        """Perform one POST /rpc and unwrap the envelope into a result or an :class:`RpcError`."""
        client = self._require_client()
        payload = {"method": method, "params": params}
        started = time.perf_counter()
        try:
            response = await client.post(RPC_PATH, json=payload)
        except httpx.TimeoutException as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            log.debug("rpc %s timed out after %.1f ms", method, elapsed_ms)
            raise RpcError(
                f"the MT5 sidecar did not answer {method} within {self.timeout:g}s",
                rpc_code=RPC_CODE_TRANSPORT,
                detail={"method": method, "elapsed_ms": round(elapsed_ms, 1)},
            ) from exc
        except httpx.HTTPError as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            log.debug("rpc %s transport error after %.1f ms: %s", method, elapsed_ms, exc)
            raise RpcError(
                f"cannot reach the MT5 sidecar at {self.url}{RPC_PATH}: {exc}",
                rpc_code=RPC_CODE_TRANSPORT,
                detail={"method": method, "elapsed_ms": round(elapsed_ms, 1)},
            ) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log.debug(
            "rpc %s -> HTTP %d in %.1f ms (params: %s)",
            method,
            response.status_code,
            elapsed_ms,
            ",".join(sorted(params)) or "none",
        )

        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise RpcError(
                "sidecar rejected the token",
                rpc_code=RPC_CODE_AUTH,
                detail={"method": method, "status": response.status_code},
            )

        excerpt = ""
        if response.status_code != httpx.codes.OK:
            excerpt = _response_excerpt(response)
            message = f"the MT5 sidecar returned HTTP {response.status_code} for {method}"
            raise RpcError(
                f"{message}: {excerpt}" if excerpt else message,
                rpc_code=RPC_CODE_TRANSPORT,
                detail={"method": method, "status": response.status_code, "body": excerpt},
            )

        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            excerpt = _response_excerpt(response)
            message = f"the MT5 sidecar answered {method} with a body that is not JSON"
            raise RpcError(
                f"{message}: {excerpt}" if excerpt else message,
                rpc_code=RPC_CODE_TRANSPORT,
                detail={"method": method, "body": excerpt},
            ) from exc

        if not isinstance(body, dict) or "ok" not in body:
            excerpt = _excerpt(_compact(body))
            message = f"the MT5 sidecar answered {method} with an unexpected envelope"
            raise RpcError(
                f"{message}: {excerpt}" if excerpt else message,
                rpc_code=RPC_CODE_TRANSPORT,
                detail={"method": method, "body": excerpt},
            )

        if body["ok"]:
            # A missing "result" key means a null result, which is a legitimate answer for
            # order_send, symbol_info and the *_get family. It is not an error here.
            return body.get("result")

        raise self._error_from_body(method, body)

    def _error_from_body(self, method: str, body: dict[str, Any]) -> RpcError:
        """Build the :class:`RpcError` described by an ``ok: false`` envelope."""
        error = body.get("error")
        if not isinstance(error, dict):
            return RpcError(
                f"the MT5 sidecar failed {method} without saying why",
                rpc_code=0,
                detail={"method": method, "body": _excerpt(_compact(body))},
            )

        raw_message = error.get("message")
        message = str(raw_message).strip() if raw_message else ""
        if not message:
            message = f"the MT5 sidecar failed {method}"

        try:
            code = int(error.get("code", 0))
        except (TypeError, ValueError):
            code = 0

        retcode_value = error.get("retcode")
        retcode: int | None
        try:
            retcode = int(retcode_value) if retcode_value is not None else None
        except (TypeError, ValueError):
            retcode = None

        log.warning("rpc %s failed: code=%s retcode=%s %s", method, code, retcode, message)
        return RpcError(message, rpc_code=code, retcode=retcode, detail={"method": method})

    # ------------------------------------------------------------------ health
    async def ping(self) -> bool:
        """Return whether the sidecar answers. Never raises — it is the health probe."""
        try:
            result = await self.call_raw("ping")
        except Exception as exc:  # noqa: BLE001 - a health probe reports, it does not propagate
            log.debug("rpc ping failed: %s", exc)
            return False
        if result is None:
            return False
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            return bool(result.get("ok", True))
        return True

    # ------------------------------------------------------------------ typed helpers
    async def account_info(self) -> Any:
        """Return the account namespace: balance, equity, margin_free, currency, margin_mode."""
        return await self.call("account_info")

    async def terminal_info(self) -> Any:
        """Return the terminal namespace, including ``trade_allowed`` and ``tradeapi_disabled``."""
        return await self.call("terminal_info")

    async def symbols_get(self) -> Any:
        """Return every symbol the broker offers, as a tuple of namespaces."""
        return await self.call("symbols_get")

    async def symbol_info(self, name: str) -> Any:
        """Return one symbol's namespace, or ``None`` when the broker has no such symbol."""
        return await self.call("symbol_info", symbol=name)

    async def symbol_select(self, name: str, enable: bool = True) -> bool:
        """Add or remove a symbol from Market Watch; an unselected symbol quotes zeros."""
        return bool(await self.call_raw("symbol_select", symbol=name, enable=enable))

    async def symbol_info_tick(self, name: str) -> Any:
        """Return the last tick namespace (``bid``, ``ask``, ``time``) or ``None``."""
        return await self.call("symbol_info_tick", symbol=name)

    async def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start: int, count: int
    ) -> Any:
        """Return OHLC rows as plain lists — numeric rows are data, not objects."""
        return await self.call_raw(
            "copy_rates_from_pos",
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            count=count,
        )

    async def positions_get(self, **kw: Any) -> Any:
        """Return open positions as a tuple of namespaces (filters: ``symbol``/``ticket``/``group``)."""
        return await self.call("positions_get", **kw)

    async def orders_get(self, **kw: Any) -> Any:
        """Return pending orders as a tuple of namespaces (filters: ``symbol``/``ticket``/``group``)."""
        return await self.call("orders_get", **kw)

    async def history_deals_get(self, date_from: float, date_to: float, **kw: Any) -> Any:
        """Return history deals in a broker-server time window, as a tuple of namespaces.

        Both bounds are POSIX timestamps in **broker-server** time, and ``date_to`` must be
        padded into the future by the caller (BUILD_PROMPT §2) or the deals just made go missing.
        """
        return await self.call("history_deals_get", date_from=date_from, date_to=date_to, **kw)

    async def relogin(self, *, login: int, password: str, server: str) -> Any:
        """Re-attach the terminal to a different account without restarting the sidecar.

        The sidecar reads its credentials from the environment once, at boot. Saving a new
        account in Settings must therefore tell it explicitly, or the change is stored and
        nothing happens at the broker. Not retried: it is a write, and a half-applied login
        is worse than a reported failure.
        """
        return await self.call("relogin", login=int(login), password=str(password), server=str(server))

    async def order_calc_margin(
        self, action: int, symbol: str, volume: float, price: float
    ) -> float:
        """Return the margin one order would require, in account currency."""
        value = await self.call_raw(
            "order_calc_margin", action=action, symbol=symbol, volume=volume, price=price
        )
        if value is None:
            raise RpcError(
                f"order_calc_margin returned no value for {symbol} — "
                "the symbol may be unselected or the terminal disconnected",
                rpc_code=RPC_CODE_NULL_RESULT,
                detail={"method": "order_calc_margin", "symbol": symbol},
            )
        return float(value)

    async def order_check(self, request: dict[str, Any]) -> Any:
        """Validate a request without touching the market. Success is retcode ``0``, not ``10009``.

        Returns ``None`` when the terminal produced no result, so callers must null-check
        before reading ``.retcode``. Never retried.
        """
        return await self.call("order_check", request=request)

    async def order_send(self, request: dict[str, Any]) -> Any:
        """Send one order. Never retried, serialised against every other write.

        Returns ``None`` when the request failed before reaching the trade server
        (BUILD_PROMPT §1.3), so the caller must null-check before reading ``.retcode``.
        Success is ``10009`` for a filled market order and ``10008`` for an accepted pending
        order; ``10010`` is a partial fill. There is nothing secret in an order request, so it
        is logged whole — it is the one line an operator needs when a broker rejects a trade.
        """
        log.info("order_send -> %s", _compact(request))
        started = time.perf_counter()
        result = await self.call("order_send", request=request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if result is None:
            log.info(
                "order_send <- no result in %.1f ms (the request failed before the trade server)",
                elapsed_ms,
            )
        else:
            # getattr is deliberate and confined to this log line: a log statement must never
            # be the thing that raises in the money path. Callers still use attribute access.
            log.info(
                "order_send <- retcode=%s ticket=%s deal=%s volume=%s price=%s comment=%s in %.1f ms",
                getattr(result, "retcode", None),
                getattr(result, "order", None),
                getattr(result, "deal", None),
                getattr(result, "volume", None),
                getattr(result, "price", None),
                getattr(result, "comment", ""),
                elapsed_ms,
            )
        return result
