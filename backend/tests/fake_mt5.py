"""An in-process fake of the MT5 sidecar's RPC surface.

The real sidecar (``mt5-sidecar/rpc_server.py``) speaks JSON over HTTP and is the only
process that may import the MT5 Python package. This fake implements the same *method surface*
with plain dicts so the whole Linux-side trading layer -- symbols, pricing, guards,
executor, reconcile, the agent loop -- is testable with no Wine, no broker and no network.

It models the behaviours that actually break real code:
  * a pending order succeeds with retcode 10008, a market order with 10009,
  * ``order_send`` returns ``None`` on a transport-style failure,
  * closing without ``position`` opens an opposite position instead of closing,
  * ``filling_mode`` is a bitmask while ``type_filling`` is an enum,
  * ``order_check`` succeeds at retcode 0,
  * netting merges positions on a symbol, hedging keeps them independent.
"""

from __future__ import annotations

import itertools
import time
from typing import Any

# MT5 integers, duplicated here on purpose: the fake stands in for the Windows side,
# so it must not import the backend's constants module.
TRADE_ACTION_DEAL = 1
TRADE_ACTION_PENDING = 5
TRADE_ACTION_SLTP = 6
TRADE_ACTION_MODIFY = 7
TRADE_ACTION_REMOVE = 8

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5

POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

RETCODE_DONE = 10009
RETCODE_PLACED = 10008
RETCODE_INVALID_VOLUME = 10014
RETCODE_INVALID_STOPS = 10016
RETCODE_NO_MONEY = 10019
RETCODE_INVALID_FILL = 10030
RETCODE_INVALID_CLOSE_VOLUME = 10038

_PENDING_TYPES = {ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP}


def _symbol(
    name: str,
    *,
    digits: int,
    point: float,
    bid: float,
    description: str = "",
    volume_min: float = 0.01,
    volume_step: float = 0.01,
    volume_max: float = 200.0,
    spread: int = 10,
    stops_level: int = 0,
    freeze_level: int = 0,
    tick_size: float | None = None,
    tick_value: float = 1.0,
    tick_value_loss: float | None = None,
    contract_size: float = 100000.0,
    filling_mode: int = SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC,
    trade_mode: int = 4,
    trade_exemode: int = 2,
    currency_profit: str = "USD",
    currency_margin: str = "USD",
    visible: bool = True,
    group: str = "Raw\\Forex",
) -> dict[str, Any]:
    """Build one ``symbol_info`` payload with broker-realistic fields."""
    ts = tick_size if tick_size is not None else point
    return {
        "name": name,
        "description": description or name,
        # MT5 reports `path` as the full Market Watch node -- group + separator + the symbol
        # name -- and the live Exness demo really does answer "Raw\\Forex\\XAUUSDr". The
        # picker groups on the folder, so the trailing leaf has to be there to be stripped.
        "path": f"{group}\\{name}",
        "digits": digits,
        "point": point,
        "bid": bid,
        "ask": round(bid + spread * point, digits),
        "spread": spread,
        "volume_min": volume_min,
        "volume_step": volume_step,
        "volume_max": volume_max,
        "trade_stops_level": stops_level,
        "trade_freeze_level": freeze_level,
        "trade_tick_size": ts,
        "trade_tick_value": tick_value,
        "trade_tick_value_loss": tick_value_loss if tick_value_loss is not None else tick_value,
        "trade_contract_size": contract_size,
        "filling_mode": filling_mode,
        "trade_mode": trade_mode,
        "trade_exemode": trade_exemode,
        "currency_profit": currency_profit,
        "currency_margin": currency_margin,
        "visible": visible,
        "time": int(time.time()),
    }


def default_symbols() -> list[dict[str, Any]]:
    """An Exness-flavoured symbol table: real suffixes, real digit counts."""
    return [
        # XAUUSDm: 2 digits, point 0.01, so one pip is 0.10 (metal x10 branch).
        _symbol(
            "XAUUSDm", digits=2, point=0.01, bid=2380.55, description="Gold vs US Dollar",
            spread=25, stops_level=0, tick_size=0.01, tick_value=1.0, contract_size=100.0,
        ),
        # EURUSDm: 5 digits, point 0.00001, pip 0.0001. tick_value 1.0 per 0.00001 on 1 lot
        # means a 20 pip stop costs 200 * 1.0 = 200 USD per lot.
        _symbol(
            "EURUSDm", digits=5, point=0.00001, bid=1.08500, description="Euro vs US Dollar",
            spread=8, tick_size=0.00001, tick_value=1.0, contract_size=100000.0,
        ),
        _symbol(
            "USDJPYm", digits=3, point=0.001, bid=157.250, description="US Dollar vs Japanese Yen",
            spread=12, tick_size=0.001, tick_value=0.63, contract_size=100000.0,
        ),
        _symbol(
            "XAGUSDm", digits=3, point=0.001, bid=30.125, description="Silver vs US Dollar",
            spread=30, tick_size=0.001, tick_value=5.0, contract_size=5000.0,
        ),
        _symbol(
            "US30m", digits=2, point=0.01, bid=38950.50, description="Dow Jones 30 Index",
            spread=200, stops_level=50, tick_size=0.01, tick_value=0.01, contract_size=1.0,
            group="Raw\\Indices",
        ),
        _symbol(
            "USTECm", digits=2, point=0.01, bid=18420.75, description="US Tech 100 Index (NAS100)",
            spread=150, tick_size=0.01, tick_value=0.01, contract_size=1.0,
            group="Raw\\Indices",
        ),
        _symbol(
            "BTCUSDm", digits=2, point=0.01, bid=64500.00, description="Bitcoin vs US Dollar",
            spread=1500, tick_size=0.01, tick_value=0.01, contract_size=1.0,
            group="Raw\\Crypto",
        ),
        _symbol(
            "USOILm", digits=3, point=0.001, bid=78.450, description="Crude Oil WTI",
            spread=30, tick_size=0.001, tick_value=0.1, contract_size=1000.0,
            group="Raw\\Energies",
        ),
        # A symbol that only allows IOC, to exercise the filling-mode branch.
        _symbol(
            "GBPUSDm", digits=5, point=0.00001, bid=1.27100, description="Pound vs US Dollar",
            spread=12, tick_size=0.00001, tick_value=1.0, filling_mode=SYMBOL_FILLING_IOC,
        ),
    ]


class FakeMt5:
    """Stateful fake broker. ``call(method, params)`` mirrors the sidecar's dispatch."""

    def __init__(
        self,
        *,
        balance: float = 3000.0,
        currency: str = "USD",
        margin_mode: int = 2,
        symbols: list[dict[str, Any]] | None = None,
    ) -> None:
        self.symbols: dict[str, dict[str, Any]] = {s["name"]: s for s in (symbols or default_symbols())}
        self.selected: set[str] = set()
        self.positions: dict[int, dict[str, Any]] = {}
        self.orders: dict[int, dict[str, Any]] = {}
        self.deals: list[dict[str, Any]] = []
        self.balance = balance
        self.currency = currency
        self.margin_mode = margin_mode
        self.leverage = 200
        self.trade_allowed = True
        self.tradeapi_disabled = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_next_send: str | None = None
        self.force_retcode: int | None = None
        self.zero_tick_symbols: set[str] = set()
        self.close_reason = 3  # DEAL_REASON_CLIENT; set 4 for TP, 5 for SL in a test
        self.server_time_offset = 3 * 3600  # broker sits at UTC+3, as Exness does
        self._ticket = itertools.count(500001)
        self._deal = itertools.count(900001)

    # ---------------------------------------------------------------- helpers
    def next_ticket(self) -> int:
        return next(self._ticket)

    def _si(self, name: str) -> dict[str, Any]:
        si = self.symbols.get(name)
        if si is None:
            raise KeyError(name)
        return si

    def _tick(self, name: str) -> dict[str, Any]:
        si = self._si(name)
        if name in self.zero_tick_symbols or name not in self.selected:
            # An unselected symbol quotes zeros rather than erroring -- exactly the trap
            # symbols.py must defend against.
            return {"time": int(time.time()), "bid": 0.0, "ask": 0.0, "last": 0.0,
                    "volume": 0, "time_msc": int(time.time() * 1000), "flags": 0, "volume_real": 0.0}
        return {"time": int(time.time() + self.server_time_offset), "bid": si["bid"], "ask": si["ask"],
                "last": si["bid"], "volume": 1, "time_msc": int((time.time() + self.server_time_offset) * 1000),
                "flags": 6, "volume_real": 1.0}

    def move(self, name: str, bid: float) -> None:
        """Move the market; the fill price of the next order follows."""
        si = self._si(name)
        si["bid"] = round(bid, si["digits"])
        si["ask"] = round(bid + si["spread"] * si["point"], si["digits"])

    def _equity(self) -> float:
        floating = 0.0
        for p in self.positions.values():
            si = self._si(p["symbol"])
            close_px = si["bid"] if p["type"] == POSITION_TYPE_BUY else si["ask"]
            sign = 1.0 if p["type"] == POSITION_TYPE_BUY else -1.0
            ticks = (close_px - p["price_open"]) / si["trade_tick_size"]
            floating += sign * ticks * si["trade_tick_value"] * p["volume"]
        return round(self.balance + floating, 2)

    # ---------------------------------------------------------------- dispatch
    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        self.calls.append((method, params))
        fn = getattr(self, f"m_{method}", None)
        if fn is None:
            raise ValueError(f"unknown method {method}")
        return fn(**params)

    # ---------------------------------------------------------------- methods
    def m_ping(self) -> dict[str, Any]:
        return {"ok": True, "ts": time.time()}

    def m_account_info(self) -> dict[str, Any]:
        equity = self._equity()
        margin = round(sum(p["volume"] * 100.0 for p in self.positions.values()), 2)
        return {
            "login": 474436674, "name": "LV Copytrader Demo", "server": "Exness-MT5Trial15",
            "currency": self.currency, "balance": round(self.balance, 2), "equity": equity,
            "margin": margin, "margin_free": round(equity - margin, 2),
            "margin_level": round(equity / margin * 100.0, 2) if margin else 0.0,
            "leverage": self.leverage, "margin_mode": self.margin_mode,
            "trade_allowed": self.trade_allowed, "trade_expert": True, "profit": round(equity - self.balance, 2),
        }

    def m_terminal_info(self) -> dict[str, Any]:
        return {
            "trade_allowed": self.trade_allowed, "tradeapi_disabled": self.tradeapi_disabled,
            "connected": True, "build": 4620, "name": "MetaTrader 5", "path": "C:/MT5",
            "community_account": False, "dlls_allowed": False,
        }

    def m_symbols_get(self, group: str | None = None) -> list[dict[str, Any]]:
        return [dict(s) for s in self.symbols.values()]

    def m_symbol_info(self, symbol: str) -> dict[str, Any] | None:
        try:
            return dict(self._si(symbol))
        except KeyError:
            return None

    def m_symbol_select(self, symbol: str, enable: bool = True) -> bool:
        if symbol not in self.symbols:
            return False
        if enable:
            self.selected.add(symbol)
        else:
            self.selected.discard(symbol)
        return True

    def m_symbol_info_tick(self, symbol: str) -> dict[str, Any] | None:
        try:
            return self._tick(symbol)
        except KeyError:
            return None

    def m_copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int) -> list[list[Any]]:
        si = self._si(symbol)
        now = int(time.time() + self.server_time_offset)
        step = max(60, timeframe * 60 if timeframe < 100 else 3600)
        out: list[list[Any]] = []
        for i in range(count):
            t = now - (count - i) * step
            base = si["bid"] - (count - i) * si["point"] * 5
            out.append([t, round(base, si["digits"]), round(base + si["point"] * 20, si["digits"]),
                        round(base - si["point"] * 20, si["digits"]), round(base + si["point"] * 3, si["digits"]),
                        100 + i, si["spread"], 100 + i])
        return out

    def m_positions_get(self, symbol: str | None = None, ticket: int | None = None,
                        group: str | None = None) -> list[dict[str, Any]]:
        rows = list(self.positions.values())
        if symbol:
            rows = [p for p in rows if p["symbol"] == symbol]
        if ticket:
            rows = [p for p in rows if p["ticket"] == ticket]
        return [dict(p) for p in rows]

    def m_orders_get(self, symbol: str | None = None, ticket: int | None = None,
                     group: str | None = None) -> list[dict[str, Any]]:
        rows = list(self.orders.values())
        if symbol:
            rows = [o for o in rows if o["symbol"] == symbol]
        if ticket:
            rows = [o for o in rows if o["ticket"] == ticket]
        return [dict(o) for o in rows]

    def m_history_deals_get(self, date_from: float, date_to: float, group: str | None = None,
                            ticket: int | None = None, position: int | None = None) -> list[dict[str, Any]]:
        rows = [d for d in self.deals if date_from <= d["time"] <= date_to]
        if position:
            rows = [d for d in rows if d["position_id"] == position]
        return [dict(d) for d in rows]

    def m_order_calc_margin(self, action: int, symbol: str, volume: float, price: float) -> float:
        si = self._si(symbol)
        return round(volume * si["trade_contract_size"] * price / self.leverage, 2)

    def _validate(self, request: dict[str, Any]) -> tuple[int, str]:
        """Shared validation for order_check and order_send. Returns (retcode, comment)."""
        if self.force_retcode is not None:
            return self.force_retcode, "forced"
        action = request.get("action")
        symbol = request.get("symbol")
        if symbol not in self.symbols:
            return 10013, "Invalid request"
        si = self._si(symbol)
        volume = float(request.get("volume") or 0.0)

        if action in (TRADE_ACTION_DEAL, TRADE_ACTION_PENDING):
            if volume <= 0 or volume < si["volume_min"] - 1e-9 or volume > si["volume_max"] + 1e-9:
                return RETCODE_INVALID_VOLUME, "Invalid volume"
            steps = volume / si["volume_step"]
            if abs(steps - round(steps)) > 1e-6:
                return RETCODE_INVALID_VOLUME, "Invalid volume"
            fill = request.get("type_filling")
            if action == TRADE_ACTION_PENDING:
                if fill != ORDER_FILLING_RETURN:
                    return RETCODE_INVALID_FILL, "Unsupported filling mode"
            else:
                allowed = si["filling_mode"]
                ok_fok = fill == ORDER_FILLING_FOK and (allowed & SYMBOL_FILLING_FOK)
                ok_ioc = fill == ORDER_FILLING_IOC and (allowed & SYMBOL_FILLING_IOC)
                ok_ret = fill == ORDER_FILLING_RETURN and si["trade_exemode"] != 2
                if not (ok_fok or ok_ioc or ok_ret):
                    return RETCODE_INVALID_FILL, "Unsupported filling mode"

        # Stops-level enforcement, the source of 10016 in the wild.
        stops = si["trade_stops_level"] * si["point"]
        ref = float(request.get("price") or si["bid"])
        for key in ("sl", "tp"):
            level = float(request.get(key) or 0.0)
            if level and stops and abs(ref - level) < stops - 1e-12:
                return RETCODE_INVALID_STOPS, "Invalid stops"

        if action == TRADE_ACTION_DEAL and request.get("position"):
            pos = self.positions.get(int(request["position"]))
            if pos is None:
                return 10013, "Invalid request"
            if volume > pos["volume"] + 1e-9:
                return RETCODE_INVALID_CLOSE_VOLUME, "Invalid close volume"

        if action in (TRADE_ACTION_DEAL, TRADE_ACTION_PENDING) and not request.get("position"):
            need = self.m_order_calc_margin(action, symbol, volume, ref)
            if need > self.m_account_info()["margin_free"]:
                return RETCODE_NO_MONEY, "Not enough money"
        return 0, "Done"

    def m_order_check(self, request: dict[str, Any]) -> dict[str, Any] | None:
        retcode, comment = self._validate(request)
        acc = self.m_account_info()
        symbol = request.get("symbol")
        volume = float(request.get("volume") or 0.0)
        margin = 0.0
        if symbol in self.symbols and volume:
            margin = self.m_order_calc_margin(
                request.get("action", TRADE_ACTION_DEAL), symbol, volume,
                float(request.get("price") or self._si(symbol)["ask"]),
            )
        return {
            "retcode": retcode, "comment": comment, "balance": acc["balance"], "equity": acc["equity"],
            "profit": acc["profit"], "margin": margin,
            "margin_free": round(acc["margin_free"] - margin, 2),
            "margin_level": acc["margin_level"],
            "request": dict(request),
        }

    def m_order_send(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if self.fail_next_send:
            self.fail_next_send = None
            return None  # the real package does exactly this on a pre-server failure

        retcode, comment = self._validate(request)
        if retcode != 0:
            return {"retcode": retcode, "comment": comment, "deal": 0, "order": 0,
                    "volume": 0.0, "price": 0.0, "request": dict(request)}

        action = request["action"]
        symbol = request["symbol"]
        si = self._si(symbol)
        now = int(time.time() + self.server_time_offset)

        if action == TRADE_ACTION_PENDING:
            ticket = self.next_ticket()
            self.orders[ticket] = {
                "ticket": ticket, "symbol": symbol, "type": request["type"],
                "volume_initial": request["volume"], "volume_current": request["volume"],
                "price_open": request["price"], "sl": request.get("sl", 0.0), "tp": request.get("tp", 0.0),
                # price_current and reason exist on a real MT5 TradeOrder. Omitting them made
                # get_orders() raise AttributeError against this double, which is why no test
                # could reach the Orders path at all -- the fake, not the code, was the gap.
                "price_current": si["bid"], "price_stoplimit": 0.0, "reason": 3,
                "magic": request.get("magic", 0), "comment": request.get("comment", ""),
                "time_setup": now, "type_time": request.get("type_time", 0),
                "time_expiration": request.get("expiration", 0), "state": 1,
            }
            # A pending order is PLACED (10008), not DONE (10009).
            return {"retcode": RETCODE_PLACED, "comment": "Placed", "order": ticket, "deal": 0,
                    "volume": request["volume"], "price": request["price"], "request": dict(request)}

        if action == TRADE_ACTION_SLTP:
            pos = self.positions.get(int(request["position"]))
            if pos is None:
                return {"retcode": 10013, "comment": "Invalid request", "order": 0, "deal": 0,
                        "volume": 0.0, "price": 0.0, "request": dict(request)}
            pos["sl"] = float(request.get("sl", pos["sl"]) or 0.0)
            pos["tp"] = float(request.get("tp", pos["tp"]) or 0.0)
            return {"retcode": RETCODE_DONE, "comment": "Done", "order": pos["ticket"], "deal": 0,
                    "volume": pos["volume"], "price": pos["price_open"], "request": dict(request)}

        if action == TRADE_ACTION_MODIFY:
            o = self.orders.get(int(request["order"]))
            if o is None:
                return {"retcode": 10013, "comment": "Invalid request", "order": 0, "deal": 0,
                        "volume": 0.0, "price": 0.0, "request": dict(request)}
            if request.get("price"):
                o["price_open"] = float(request["price"])
            if request.get("sl") is not None:
                o["sl"] = float(request["sl"])
            if request.get("tp") is not None:
                o["tp"] = float(request["tp"])
            return {"retcode": RETCODE_DONE, "comment": "Done", "order": o["ticket"], "deal": 0,
                    "volume": o["volume_current"], "price": o["price_open"], "request": dict(request)}

        if action == TRADE_ACTION_REMOVE:
            o = self.orders.pop(int(request["order"]), None)
            if o is None:
                return {"retcode": 10013, "comment": "Invalid request", "order": 0, "deal": 0,
                        "volume": 0.0, "price": 0.0, "request": dict(request)}
            return {"retcode": RETCODE_DONE, "comment": "Done", "order": o["ticket"], "deal": 0,
                    "volume": o["volume_current"], "price": o["price_open"], "request": dict(request)}

        # TRADE_ACTION_DEAL -- open, close or (with `position` omitted) accidentally open the opposite.
        otype = request["type"]
        volume = float(request["volume"])
        price = si["ask"] if otype == ORDER_TYPE_BUY else si["bid"]
        position_id = request.get("position")

        if position_id:
            pos = self.positions[int(position_id)]
            close_px = si["bid"] if pos["type"] == POSITION_TYPE_BUY else si["ask"]
            sign = 1.0 if pos["type"] == POSITION_TYPE_BUY else -1.0
            ticks = (close_px - pos["price_open"]) / si["trade_tick_size"]
            profit = round(sign * ticks * si["trade_tick_value"] * volume, 2)
            deal = next(self._deal)
            self.deals.append({
                "ticket": deal, "order": deal, "position_id": pos["ticket"], "symbol": symbol,
                "type": otype, "entry": 1, "volume": volume, "price": close_px, "profit": profit,
                "commission": 0.0, "swap": 0.0, "fee": 0.0, "time": now, "time_msc": now * 1000,
                # DEAL_REASON_CLIENT. Real deals carry a reason, and reconcile reads it to say
                # whether a close was a TP, an SL or a human -- which §7's "Closed" notification
                # quotes. Without it every reconciled close classifies as "unknown".
                "reason": self.close_reason, "position_id": pos["ticket"],
                "magic": pos["magic"], "comment": request.get("comment", ""),
            })
            self.balance = round(self.balance + profit, 2)
            if volume >= pos["volume"] - 1e-9:
                del self.positions[pos["ticket"]]
            else:
                pos["volume"] = round(pos["volume"] - volume, 8)
            return {"retcode": RETCODE_DONE, "comment": "Done", "order": deal, "deal": deal,
                    "volume": volume, "price": close_px, "request": dict(request)}

        ptype = POSITION_TYPE_BUY if otype == ORDER_TYPE_BUY else POSITION_TYPE_SELL
        if self.margin_mode != 2:
            # Netting: a same-symbol order merges into one position.
            existing = next((p for p in self.positions.values() if p["symbol"] == symbol), None)
            if existing is not None:
                if existing["type"] == ptype:
                    total = existing["volume"] + volume
                    existing["price_open"] = round(
                        (existing["price_open"] * existing["volume"] + price * volume) / total, si["digits"])
                    existing["volume"] = round(total, 8)
                else:
                    existing["volume"] = round(existing["volume"] - volume, 8)
                    if existing["volume"] <= 1e-9:
                        del self.positions[existing["ticket"]]
                deal = next(self._deal)
                merged_ticket = existing["ticket"] if existing["ticket"] in self.positions else 0
                return {"retcode": RETCODE_DONE, "comment": "Done", "order": merged_ticket,
                        "deal": deal, "volume": volume, "price": price, "request": dict(request)}

        ticket = self.next_ticket()
        self.positions[ticket] = {
            "ticket": ticket, "symbol": symbol, "type": ptype, "volume": volume,
            "price_open": price, "sl": float(request.get("sl") or 0.0), "tp": float(request.get("tp") or 0.0),
            "price_current": price, "profit": 0.0, "swap": 0.0,
            "magic": request.get("magic", 0), "comment": request.get("comment", ""),
            "time": now, "time_msc": now * 1000, "identifier": ticket, "reason": 3,
        }
        deal = next(self._deal)
        self.deals.append({
            "ticket": deal, "order": ticket, "position_id": ticket, "symbol": symbol, "type": otype,
            "entry": 0, "volume": volume, "price": price, "profit": 0.0, "commission": 0.0,
            "swap": 0.0, "fee": 0.0, "time": now, "time_msc": now * 1000,
            "magic": request.get("magic", 0), "comment": request.get("comment", ""),
        })
        # `order` is the ticket of the position this filled into, not the deal id: in MT5 a
        # position's identifier IS the order ticket that opened it, and callers close and
        # modify by that number. Returning the deal here would hand every caller a ticket
        # that positions_get() has never heard of.
        return {"retcode": RETCODE_DONE, "comment": "Done", "order": ticket, "deal": deal,
                "volume": volume, "price": price, "request": dict(request)}
