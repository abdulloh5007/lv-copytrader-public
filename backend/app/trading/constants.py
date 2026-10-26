"""Every MetaTrader 5 integer the backend needs, written as literals under its MQL5 name.

The MT5 Python package is Windows-only and is correctly absent from the backend's
dependencies — only ``mt5-sidecar/rpc_server.py`` imports it. So no value here is read
from the package and none is computed: every number is the literal MQL5 documents, which
is the only way the timeframe values (bit-packed) and the two different filling-mode
numbering schemes stay right.
"""

from __future__ import annotations

from typing import Final

from app.errors import TradingError

# --------------------------------------------------------------------------------------
# ENUM_TRADE_REQUEST_ACTIONS — the "action" field of an order_send/order_check request.
# --------------------------------------------------------------------------------------
TRADE_ACTION_DEAL: Final[int] = 1
TRADE_ACTION_PENDING: Final[int] = 5
TRADE_ACTION_SLTP: Final[int] = 6
TRADE_ACTION_MODIFY: Final[int] = 7
TRADE_ACTION_REMOVE: Final[int] = 8

# --------------------------------------------------------------------------------------
# ENUM_ORDER_TYPE — the "type" field of a request, and the type of an existing order.
# 0..5 are the only types this system ever sends; 6..8 exist so an order placed by hand
# in the terminal still renders under its real name instead of a bare integer.
# --------------------------------------------------------------------------------------
ORDER_TYPE_BUY: Final[int] = 0
ORDER_TYPE_SELL: Final[int] = 1
ORDER_TYPE_BUY_LIMIT: Final[int] = 2
ORDER_TYPE_SELL_LIMIT: Final[int] = 3
ORDER_TYPE_BUY_STOP: Final[int] = 4
ORDER_TYPE_SELL_STOP: Final[int] = 5
ORDER_TYPE_BUY_STOP_LIMIT: Final[int] = 6
ORDER_TYPE_SELL_STOP_LIMIT: Final[int] = 7
ORDER_TYPE_CLOSE_BY: Final[int] = 8

# --------------------------------------------------------------------------------------
# ENUM_ORDER_TYPE_FILLING — the "type_filling" field of a request. This is an ENUM.
# --------------------------------------------------------------------------------------
ORDER_FILLING_FOK: Final[int] = 0
ORDER_FILLING_IOC: Final[int] = 1
ORDER_FILLING_RETURN: Final[int] = 2
ORDER_FILLING_BOC: Final[int] = 3

# --------------------------------------------------------------------------------------
# symbol_info().filling_mode — a BITMASK, and a different numbering scheme from the enum
# above: bitmask 1 means "FOK allowed" while enum 1 means IOC. Feeding one into the other
# is the most common bug in MT5 Python code and it surfaces only at the broker, as
# retcode 10030 with the comment "Unsupported filling mode".
#
# These SYMBOL_FILLING_* names DO NOT EXIST in the MT5 Python package —
# mt5.SYMBOL_FILLING_IOC raises AttributeError — which is why they are literals here.
# Note also that ORDER_FILLING_RETURN is never a bit in filling_mode, so filling_mode == 0
# does not mean "no filling mode is allowed".
# --------------------------------------------------------------------------------------
SYMBOL_FILLING_FOK: Final[int] = 1
SYMBOL_FILLING_IOC: Final[int] = 2
SYMBOL_FILLING_BOC: Final[int] = 4

# --------------------------------------------------------------------------------------
# ENUM_ORDER_TYPE_TIME — the "type_time" field of a request.
# --------------------------------------------------------------------------------------
ORDER_TIME_GTC: Final[int] = 0
ORDER_TIME_SPECIFIED: Final[int] = 2

# --------------------------------------------------------------------------------------
# ENUM_POSITION_TYPE — the "type" field of an open position.
# --------------------------------------------------------------------------------------
POSITION_TYPE_BUY: Final[int] = 0
POSITION_TYPE_SELL: Final[int] = 1

# --------------------------------------------------------------------------------------
# ENUM_ACCOUNT_MARGIN_MODE — account_info().margin_mode. Only HEDGING allows several
# independent positions on one symbol, which is what multi-TP splitting needs.
# --------------------------------------------------------------------------------------
MARGIN_MODE_NETTING: Final[int] = 0
MARGIN_MODE_EXCHANGE: Final[int] = 1
MARGIN_MODE_HEDGING: Final[int] = 2

# --------------------------------------------------------------------------------------
# ENUM_SYMBOL_TRADE_MODE — symbol_info().trade_mode. FULL is the only fully tradable one.
# --------------------------------------------------------------------------------------
SYMBOL_TRADE_MODE_DISABLED: Final[int] = 0
SYMBOL_TRADE_MODE_LONGONLY: Final[int] = 1
SYMBOL_TRADE_MODE_SHORTONLY: Final[int] = 2
SYMBOL_TRADE_MODE_CLOSEONLY: Final[int] = 3
SYMBOL_TRADE_MODE_FULL: Final[int] = 4

# --------------------------------------------------------------------------------------
# ENUM_SYMBOL_TRADE_EXECUTION — symbol_info().trade_exemode. pick_filling() reads this:
# ORDER_FILLING_RETURN is banned under Market execution (== SYMBOL_TRADE_EXECUTION_MARKET).
# --------------------------------------------------------------------------------------
SYMBOL_TRADE_EXECUTION_REQUEST: Final[int] = 0
SYMBOL_TRADE_EXECUTION_INSTANT: Final[int] = 1
SYMBOL_TRADE_EXECUTION_MARKET: Final[int] = 2
SYMBOL_TRADE_EXECUTION_EXCHANGE: Final[int] = 3

# --------------------------------------------------------------------------------------
# ENUM_TIMEFRAMES — bit-packed, NOT minutes. H1 really is 16385 (0x4001), not 60.
# Never compute these.
# --------------------------------------------------------------------------------------
TIMEFRAMES: Final[dict[str, int]] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408,
}

# --------------------------------------------------------------------------------------
# ENUM_TRADE_RETCODE — the retcodes this system reads. retcode_message() in app.errors
# turns each of these into a sentence the model can act on.
# --------------------------------------------------------------------------------------
RETCODE: Final[dict[int, str]] = {
    10004: "REQUOTE",
    10008: "PLACED",
    10009: "DONE",
    10010: "DONE_PARTIAL",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",
    10016: "INVALID_STOPS",
    10017: "TRADE_DISABLED",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10020: "PRICE_CHANGED",
    10021: "PRICE_OFF",
    10027: "CLIENT_DISABLES_AT",
    10030: "INVALID_FILL",
    10038: "INVALID_CLOSE_VOLUME",
    10045: "FIFO_CLOSE",
}

# A market order that filled returns DONE (10009); a PENDING order that was accepted
# returns PLACED (10008). Checking only for 10009 marks every limit order as a failure
# and retries it, which is how one signal becomes two orders.
SUCCESS_RETCODES: Final[frozenset[int]] = frozenset({10008, 10009})

# DONE_PARTIAL: succeeded, but the filled volume must be read and reported.
PARTIAL_RETCODE: Final[int] = 10010

ACTION_NAMES: Final[dict[int, str]] = {
    TRADE_ACTION_DEAL: "DEAL",
    TRADE_ACTION_PENDING: "PENDING",
    TRADE_ACTION_SLTP: "SLTP",
    TRADE_ACTION_MODIFY: "MODIFY",
    TRADE_ACTION_REMOVE: "REMOVE",
}

ORDER_TYPE_NAMES: Final[dict[int, str]] = {
    ORDER_TYPE_BUY: "BUY",
    ORDER_TYPE_SELL: "SELL",
    ORDER_TYPE_BUY_LIMIT: "BUY_LIMIT",
    ORDER_TYPE_SELL_LIMIT: "SELL_LIMIT",
    ORDER_TYPE_BUY_STOP: "BUY_STOP",
    ORDER_TYPE_SELL_STOP: "SELL_STOP",
    ORDER_TYPE_BUY_STOP_LIMIT: "BUY_STOP_LIMIT",
    ORDER_TYPE_SELL_STOP_LIMIT: "SELL_STOP_LIMIT",
    ORDER_TYPE_CLOSE_BY: "CLOSE_BY",
}

# Lower-case, because this is the "side" string the tools and the ORM use.
POSITION_TYPE_NAMES: Final[dict[int, str]] = {
    POSITION_TYPE_BUY: "buy",
    POSITION_TYPE_SELL: "sell",
}

# The pending types this system places and recognises. Stop-limit orders (6, 7) are
# deliberately excluded: the executor never creates them.
PENDING_ORDER_TYPES: Final[frozenset[int]] = frozenset(
    {
        ORDER_TYPE_BUY_LIMIT,
        ORDER_TYPE_SELL_LIMIT,
        ORDER_TYPE_BUY_STOP,
        ORDER_TYPE_SELL_STOP,
    }
)

_MARGIN_MODE_NAMES: Final[dict[int, str]] = {
    MARGIN_MODE_NETTING: "netting",
    MARGIN_MODE_EXCHANGE: "exchange",
    MARGIN_MODE_HEDGING: "hedging",
}


def retcode_name(rc: int | None) -> str:
    """Return the MQL5 short name of a trade retcode, or a printable placeholder."""
    if rc is None:
        return "NONE"
    return RETCODE.get(rc, f"RETCODE_{rc}")


def margin_mode_name(v: int) -> str:
    """Return the account margin mode as the literal string the tools and UI report."""
    return _MARGIN_MODE_NAMES.get(v, f"unknown_{v}")


def timeframe_value(name: str) -> int:
    """Translate a timeframe name such as 'H1' into its bit-packed MT5 integer."""
    key = str(name).strip().upper()
    value = TIMEFRAMES.get(key)
    if value is None:
        raise TradingError(
            f"unknown timeframe {name!r} — use one of: {', '.join(TIMEFRAMES)}",
            detail={"timeframe": name, "allowed": list(TIMEFRAMES)},
        )
    return value
