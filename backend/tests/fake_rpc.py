"""A drop-in stand-in for ``app.trading.rpc.Mt5Rpc`` backed by :class:`FakeMt5`.

The real client speaks HTTP to the Wine sidecar. This one satisfies the same public
contract (CONTRACTS.md §9) against an in-process fake broker, so every module above
``rpc.py`` -- symbols, pricing, guards, executor, reconcile, loop -- can be tested
without Wine, without a broker and without a socket.

It deliberately reproduces the two shapes callers must handle:
  * results come back as ``SimpleNamespace`` (attribute access, ``AttributeError`` on
    a missing field) rather than dicts,
  * ``order_send`` / ``order_check`` may return ``None``.
"""

from __future__ import annotations

from typing import Any

from app.trading.rpc import to_ns

from tests.fake_mt5 import FakeMt5

# Methods whose payloads must stay as plain data (numeric arrays, scalars, bools).
_RAW_METHODS = frozenset({"copy_rates_from_pos", "symbol_select", "order_calc_margin", "ping"})


class FakeRpc:
    """Implements the Mt5Rpc surface over a :class:`FakeMt5` instance."""

    def __init__(self, broker: FakeMt5 | None = None) -> None:
        self.broker = broker or FakeMt5()
        self.closed = False
        self.unreachable = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def ping(self) -> bool:
        return not self.unreachable

    # ------------------------------------------------------------------ core
    async def call(self, method: str, **params: Any) -> Any:
        return to_ns(await self.call_raw(method, **params))

    async def call_raw(self, method: str, **params: Any) -> Any:
        if self.unreachable:
            from app.errors import RpcError

            raise RpcError("sidecar unreachable", rpc_code=-1)
        return self.broker.call(method, params)

    # ------------------------------------------------------------------ helpers
    async def account_info(self) -> Any:
        return await self.call("account_info")

    async def terminal_info(self) -> Any:
        return await self.call("terminal_info")

    async def symbols_get(self) -> Any:
        return await self.call("symbols_get")

    async def symbol_info(self, name: str) -> Any:
        return await self.call("symbol_info", symbol=name)

    async def symbol_select(self, name: str, enable: bool = True) -> bool:
        return bool(await self.call_raw("symbol_select", symbol=name, enable=enable))

    async def symbol_info_tick(self, name: str) -> Any:
        return await self.call("symbol_info_tick", symbol=name)

    async def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int) -> Any:
        return await self.call_raw(
            "copy_rates_from_pos", symbol=symbol, timeframe=timeframe, start=start, count=count
        )

    async def positions_get(self, **kw: Any) -> Any:
        return await self.call("positions_get", **kw)

    async def orders_get(self, **kw: Any) -> Any:
        return await self.call("orders_get", **kw)

    async def history_deals_get(self, date_from: float, date_to: float, **kw: Any) -> Any:
        return await self.call("history_deals_get", date_from=date_from, date_to=date_to, **kw)

    async def order_check(self, request: dict[str, Any]) -> Any:
        return await self.call("order_check", request=request)

    async def order_send(self, request: dict[str, Any]) -> Any:
        return await self.call("order_send", request=request)

    async def order_calc_margin(self, action: int, symbol: str, volume: float, price: float) -> float:
        value = await self.call_raw(
            "order_calc_margin", action=action, symbol=symbol, volume=volume, price=price
        )
        return float(value)
