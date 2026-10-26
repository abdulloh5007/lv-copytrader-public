"""Operator knobs the dashboard writes and the workers read.

The values live in ``app_settings``. Code defaults below are what a missing row means.
Nothing here is an environment secret.
"""

from __future__ import annotations

from typing import Any

KEY_REVIEWER_ENABLED: str = "reviewer.enabled"
KEY_TAG_FILTER_ENABLED: str = "ingest.tag_filter_enabled"
KEY_TAG_FILTER_TAG: str = "ingest.tag_filter_tag"
KEY_ORDERS_PER_SIGNAL: str = "trading.orders_per_signal"
KEY_ORDER_LOT_SIZE: str = "trading.order_lot_size"
KEY_USE_FIXED_ORDER_LOTS: str = "trading.use_fixed_order_lots"
KEY_MAX_ENTRY_DRIFT_USD: str = "trading.max_entry_drift_usd"
KEY_BE_AFTER_TP1: str = "trading.be_after_tp1"
KEY_SYSTEM_PROMPT: str = "ai.system_prompt"

REVIEWER_ENABLED_DEFAULT: bool = False
TAG_FILTER_ENABLED_DEFAULT: bool = False
TAG_FILTER_TAG_DEFAULT: str = "#soft"
ORDERS_PER_SIGNAL_DEFAULT: int = 2
ORDER_LOT_SIZE_DEFAULT: float = 0.2
#: Classic signals ignore the model's risk_percent and use the operator lot. Default on.
USE_FIXED_ORDER_LOTS_DEFAULT: bool = True
#: Classic gold-style entries skip for "price already moved" only past this many
#: price units from the channel entry or zone. At or inside it, the entry is opened.
MAX_ENTRY_DRIFT_USD_DEFAULT: float = 5.0
BE_AFTER_TP1_DEFAULT: bool = True

#: Stored on a classic multi-order group. The watchdog moves the rest to breakeven
#: after the nearest take-profit ticket is gone.
MODE_BE_AFTER_TP1: str = "be_after_tp1"
#: The model or the operator already moved a stop. The watchdog leaves that group alone.
MODE_AI_MANAGED: str = "ai_managed"

ORDERS_PER_SIGNAL_MAX: int = 12
TAG_MAX_LENGTH: int = 64
SYSTEM_PROMPT_MAX_LENGTH: int = 200_000

SKIP_REASON_TAG: str = "missing tag"


def message_has_tag(text: str, tag: str) -> bool:
    """True when ``tag`` occurs in ``text`` as a case-insensitive substring.

    ``#soft``, ``#SOFT`` and ``#SoFt`` all match a tag of ``#soft``. An empty tag
    matches nothing: the caller treats that as "filter is not armed".
    """
    needle = str(tag or "").strip().casefold()
    if not needle:
        return False
    return needle in str(text or "").casefold()


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_int(value: Any, default: int, *, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < lo:
        return lo
    if number > hi:
        return hi
    return number


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number <= 0.0:
        return default
    return number


def _get_setting():
    """Import the settings reader lazily so this module can be imported from ``app.db``."""
    from app.db import get_setting

    return get_setting


async def reviewer_enabled(session: Any) -> bool:
    """Whether the channel reviewer may generate a playbook. Default off."""
    get_setting = _get_setting()
    stored = await get_setting(session, KEY_REVIEWER_ENABLED, REVIEWER_ENABLED_DEFAULT)
    return _as_bool(stored, REVIEWER_ENABLED_DEFAULT)


async def tag_filter_enabled(session: Any) -> bool:
    """Whether untagged Telegram messages are kept out of the signal pipeline."""
    get_setting = _get_setting()
    return _as_bool(
        await get_setting(session, KEY_TAG_FILTER_ENABLED, TAG_FILTER_ENABLED_DEFAULT),
        TAG_FILTER_ENABLED_DEFAULT,
    )


async def tag_filter_drops(session: Any, text: str) -> bool:
    """True when this message must not enter the signal pipeline.

    The filter drops a message only when it is on, a tag is stored, and that tag
    is not a case-insensitive substring of the text. An empty tag does not drop
    anything: there is nothing to match.
    """
    enabled = await tag_filter_enabled(session)
    if not enabled:
        return False
    get_setting = _get_setting()
    tag = str(await get_setting(session, KEY_TAG_FILTER_TAG, TAG_FILTER_TAG_DEFAULT) or "").strip()
    if not tag:
        return False
    return not message_has_tag(text, tag)


async def orders_per_signal(session: Any) -> int:
    """How many classic orders one signal may open. Default 2."""
    get_setting = _get_setting()
    stored = await get_setting(session, KEY_ORDERS_PER_SIGNAL, ORDERS_PER_SIGNAL_DEFAULT)
    return _as_int(stored, ORDERS_PER_SIGNAL_DEFAULT, lo=1, hi=ORDERS_PER_SIGNAL_MAX)


async def order_lot_size(session: Any) -> float:
    """Lot size of each classic order when the model omits volume and risk. Default 0.2."""
    get_setting = _get_setting()
    stored = await get_setting(session, KEY_ORDER_LOT_SIZE, ORDER_LOT_SIZE_DEFAULT)
    return _as_float(stored, ORDER_LOT_SIZE_DEFAULT)


async def use_fixed_order_lots(session: Any) -> bool:
    """Whether a classic signal ignores the model's risk_percent. Default on.

    On, the size is ``orders_per_signal`` times ``order_lot_size``. Off, a
    ``risk_percent`` from the model sizes the trade the way it used to. An
    explicit volume is honored in both modes. On, that size is not a suggestion:
    the model must not defer because the lots look large, the stop is wide, or
    reward-to-risk to TP1 looks poor.
    """
    get_setting = _get_setting()
    stored = await get_setting(session, KEY_USE_FIXED_ORDER_LOTS, USE_FIXED_ORDER_LOTS_DEFAULT)
    return _as_bool(stored, USE_FIXED_ORDER_LOTS_DEFAULT)


async def max_entry_drift_usd(session: Any) -> float:
    """How far a classic gold fill may sit from the channel entry. Default 5.

    Absolute price units (dollars on XAUUSD). A live fill farther than this is
    the only "price already moved" skip. At or inside it, a classic market
    entry is opened.
    """
    get_setting = _get_setting()
    stored = await get_setting(session, KEY_MAX_ENTRY_DRIFT_USD, MAX_ENTRY_DRIFT_USD_DEFAULT)
    return _as_float(stored, MAX_ENTRY_DRIFT_USD_DEFAULT)


async def be_after_tp1_enabled(session: Any) -> bool:
    """Whether a classic multi-order signal locks the rest to breakeven after TP1."""
    get_setting = _get_setting()
    stored = await get_setting(session, KEY_BE_AFTER_TP1, BE_AFTER_TP1_DEFAULT)
    return _as_bool(stored, BE_AFTER_TP1_DEFAULT)


async def stored_system_prompt(session: Any) -> str:
    """The operator's system-prompt override, or ``""`` when the built-in text is in force."""
    get_setting = _get_setting()
    stored = await get_setting(session, KEY_SYSTEM_PROMPT, "")
    if not isinstance(stored, str):
        return ""
    return stored
