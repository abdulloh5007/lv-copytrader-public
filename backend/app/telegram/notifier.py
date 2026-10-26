"""Operator notifications sent through the operator's own Telegram bot (Bot API + httpx).

This module is deliberately separate from :mod:`app.telegram.client`. That one drives a
Telethon *user* account to read the signal channel; this one talks to the HTTP Bot API to
push messages *to* the operator. Nothing here imports Telethon.

Five properties this file exists to guarantee:

* **A notification never delays or aborts a trade.** :meth:`Notifier.fire` schedules the
  send as a background task and returns immediately; :meth:`Notifier.send` never raises.
* **The operator never has to look up their numeric chat id.** :meth:`Notifier.detect_chat`
  polls ``getUpdates`` and fills it in from the first message they send the bot.
* **The bot answers to the operator and nobody else.** ``notifier.admin_ids`` is an
  allowlist of Telegram *user* ids. While it is empty nothing is enforced — that is the
  behaviour this project shipped with, and an operator who never opened the field must not
  have their working bot go quiet — but the binding is *reported*, so "who is this bot
  talking to" is always answerable. Once it holds an id, :meth:`Notifier.detect_chat` will
  not bind anyone else and :meth:`Notifier.send` will not deliver anywhere else. Without
  that, a bot username that is shared or guessed is enough for a stranger to be handed
  every symbol, volume, entry, stop and realised P/L the account produces.
* **The operator talks to the bot with slash commands, never sticky buttons.** On start
  (and whenever the bot token is saved or rotated) we ``deleteMyCommands`` then
  ``setMyCommands`` so Telegram's command menu is LV Copytrader's ``/start``, ``/status``
  and ``/help`` — not leftovers from a previous bot token. Incoming DMs are answered
  only for ``notifier.admin_ids`` / the bound chat. A background poller consumes
  ``getUpdates`` for those commands; ``detect_chat`` and ``recent_senders`` take the
  same lock for their whole window so the poller cannot steal the update they are
  waiting for. This bot never installs a webhook, and it never calls
  ``deleteWebhook``: a 409 is reported as a conflict, the same way detection already
  did. No ``ReplyKeyboardMarkup`` / ``InlineKeyboardMarkup`` is sent; every
  ``sendMessage`` includes ``ReplyKeyboardRemove`` so a leftover button row from
  another product is cleared the next time we write.
* **Rich cards by default.** Notifications use ``sendRichMessage`` with rich HTML;
  explicit unsupported-format refusals fall back to classic ``sendMessage`` HTML.
  Trade cards we build ourselves are already Telegram HTML. AI notes, attention
  text and slash-command replies that arrive as markdown (headings, bold, lists,
  tables) are converted: tables become aligned ``<pre>`` blocks, because Telegram
  does not render GitHub-style pipes. Every interpolated value is escaped so a
  stray ``<`` cannot break the message or inject a tag. Bodies over Telegram's
  4096-character limit are split on paragraph boundaries rather than truncated.

The refusal path obeys the first property as hard as the delivery path does: a chat that is
not on the allowlist is refused *quietly* — logged, recorded, and announced as a
``SYSTEM_WARNING`` — and :meth:`Notifier.send` still returns ``False`` rather than raising
into the executor (§7).
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from app import __version__, events
from app.config import Settings
from app.config import settings as _module_settings
from app.crypto import MASK
from app.db import get_setting, session_scope, set_setting
from app.events import EventBus
from app.models import Notification
from app.telegram.format import (
    html_to_plain,
    prepare_telegram_html,
    split_telegram_html,
    telegram_html_to_rich_html,
)
from app.util.logging import get_logger, redact, register_secret

if TYPE_CHECKING:  # pragma: no cover - for type checkers only, no runtime import cycle
    from app.runtime import Runtime

log = get_logger(__name__)

__all__ = [
    "ALLOWED_BOT_COMMANDS",
    "BOT_COMMAND_NAMES",
    "DEFAULT_LOCALE",
    "DEFAULT_TYPES",
    "KEYBOARD_REMOVE",
    "LEGACY_BOT_COMMANDS",
    "LOCALES",
    "NOTIFY_TYPES",
    "Notifier",
    "NotifierConfig",
    "STRINGS",
    "TYPE_ATTENTION",
    "TYPE_CLOSED",
    "TYPE_MODIFIED",
    "TYPE_OPENED",
    "TYPE_REJECTED",
    "UpdateSender",
    "bot_command_menu",
    "command_sender_allowed",
    "parse_bot_command",
    "render",
    "prepare_telegram_html",
    "split_telegram_html",
]

# --------------------------------------------------------------------------------------
# Notification types — one on/off switch each, in settings key `notifier.types` (§7).
# --------------------------------------------------------------------------------------
TYPE_OPENED: Final[str] = "opened"
TYPE_MODIFIED: Final[str] = "modified"
TYPE_CLOSED: Final[str] = "closed"
TYPE_REJECTED: Final[str] = "rejected"
TYPE_ATTENTION: Final[str] = "attention"

NOTIFY_TYPES: Final[tuple[str, ...]] = (
    TYPE_OPENED,
    TYPE_MODIFIED,
    TYPE_CLOSED,
    TYPE_REJECTED,
    TYPE_ATTENTION,
)

#: The shape of `notifier.types`; an unknown type is treated as enabled so a new kind of
#: notification is never silently swallowed by an old settings row.
DEFAULT_TYPES: Final[dict[str, bool]] = {name: True for name in NOTIFY_TYPES}

# --------------------------------------------------------------------------------------
# Bot API transport.
# --------------------------------------------------------------------------------------
TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"

#: Telegram's hard limit for one text message.
MAX_MESSAGE_CHARS: Final[int] = 4096
#: How much raw text one notification may carry before it is truncated.
MAX_TEXT_CHARS: Final[int] = 3500
#: Per-value cap applied to free-text the model wrote (`reason`, `detail`, broker comments).
MAX_FIELD_CHARS: Final[int] = 600
#: Per-value cap for short values rendered in monospace (prices, tickets, symbols).
MAX_CODE_CHARS: Final[int] = 64
MAX_LINK_CHARS: Final[int] = 400
ELLIPSIS: Final[str] = "…"
ARROW: Final[str] = "→"

#: HTTP statuses worth retrying; everything else in the 4xx range is a permanent refusal
#: (a wrong token, a chat the bot was blocked in) that three attempts cannot fix.
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Ceiling for a `retry_after` the API asks us to honour, so a flood wait cannot pin the task.
MAX_RETRY_AFTER_SECONDS: Final[float] = 30.0

#: How long :meth:`Notifier.stop` waits for in-flight sends before cancelling them.
STOP_GRACE_SECONDS: Final[float] = 5.0

#: Cap for ``setMyCommands`` / ``deleteMyCommands`` so a Telegram outage cannot stall boot.
COMMAND_SYNC_TIMEOUT_SECONDS: Final[float] = 15.0

CONNECT_TIMEOUT_SECONDS: Final[float] = 10.0
READ_TIMEOUT_SECONDS: Final[float] = 25.0

#: Update kinds `detect_chat` listens for. A person pressing "start" produces `message`;
#: adding the bot to a channel and posting produces `channel_post`.
DETECT_ALLOWED_UPDATES: Final[tuple[str, ...]] = ("message", "channel_post")

#: Slash commands registered with ``setMyCommands`` and answered by the inbound poller.
#: Order is the order Telegram shows in the menu. Keep this set small and LV Copytrader-
#: specific: a token that previously served another product may still have ``/plan``
#: and friends in BotFather until we overwrite them.
BOT_COMMAND_NAMES: Final[tuple[str, ...]] = ("start", "status", "help")
ALLOWED_BOT_COMMANDS: Final[frozenset[str]] = frozenset(BOT_COMMAND_NAMES)

#: Names this notifier must never re-register. They belonged to a different product
#: that shared the token; they do nothing here.
LEGACY_BOT_COMMANDS: Final[frozenset[str]] = frozenset(
    {"plan", "apikeys", "autokey", "settings", "menu", "key", "keys"}
)

#: Sent with every ``sendMessage`` so a leftover reply keyboard is cleared. This is
#: ``ReplyKeyboardRemove``, not a keyboard of our own.
KEYBOARD_REMOVE: Final[dict[str, bool]] = {"remove_keyboard": True}

#: ``/start`` and ``/start@BotName extra`` — Telegram's command menu produces the first.
_BOT_COMMAND_RE: Final[re.Pattern[str]] = re.compile(
    r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:\s|$)",
)

#: How long :meth:`Notifier.recent_senders` polls before answering. Short on purpose: this
#: is the "what is my numeric id" lookup, pressed while the operator is looking at the page,
#: not the sixty-second detection window they press and then walk over to Telegram for.
WHOAMI_WINDOW_SECONDS: Final[float] = 15.0

#: How much of a sender's message text is echoed back so the operator can recognise which
#: message was theirs. It is identification, not a transcript.
MAX_SENDER_TEXT_CHARS: Final[int] = 120

#: Fields of an update that can carry the chat we are looking for, in preference order.
_UPDATE_MESSAGE_FIELDS: Final[tuple[str, ...]] = (
    "message",
    "channel_post",
    "edited_message",
    "edited_channel_post",
)

# --------------------------------------------------------------------------------------
# Operator-facing English sentences for the two setup calls. These are returned by the
# REST layer to the settings page, which renders them next to the field; the *notification
# bodies* below are the locale-aware part.
# --------------------------------------------------------------------------------------
TOKEN_REJECTED_MESSAGE: Final[str] = "the bot token was rejected by Telegram"
NO_TOKEN_MESSAGE: Final[str] = "no bot token was provided — paste the token @BotFather gave you"
NO_MESSAGE_RECEIVED_MESSAGE: Final[str] = "no message received — send /start to your bot"
WEBHOOK_CONFLICT_MESSAGE: Final[str] = (
    "this bot has a webhook set, which blocks getUpdates — delete the webhook and try again"
)
UNREACHABLE_MESSAGE: Final[str] = "could not reach Telegram"
NOT_CONFIGURED_MESSAGE: Final[str] = (
    "the notification bot is not configured — set a bot token and detect the chat"
)

#: The detection window expired, and every message that arrived in it came from someone who
#: is not on the admin allowlist. This is deliberately a *different* sentence from
#: NO_MESSAGE_RECEIVED_MESSAGE: "nobody messaged the bot" is fixed by opening Telegram, and
#: "somebody did, but they are not an admin" is fixed by editing the allowlist. Telling the
#: operator to press Start again when they already did is how a setup screen loses them.
NON_ADMIN_ONLY_MESSAGE: Final[str] = (
    "someone messaged the bot, but their Telegram user id is not in the admin list — add "
    "their id below if that account is yours, or send a message from one that is already on it"
)

#: A notification had a chat to go to, and the allowlist says that chat is not the operator.
NOT_ADMIN_CHAT_MESSAGE: Final[str] = (
    "the target chat is not in the admin list, so the notification was not delivered — "
    "detect the chat again from an admin account, or add this chat id to the admin list"
)

#: An account that IS on the allowlist wrote to the bot from a group. The allowlist holds
#: user ids, a group chat id is negative, and `send` therefore could never deliver there —
#: so binding it would produce a bot that looks configured and delivers nothing.
ADMIN_GROUP_CHAT_MESSAGE: Final[str] = (
    "an admin messaged the bot from a group chat — while the admin list is in force, "
    "notifications can only go to a private chat, so message the bot directly instead"
)

NO_RECENT_SENDERS_MESSAGE: Final[str] = (
    "nobody has messaged the bot recently — send it any message, then look again"
)

# --------------------------------------------------------------------------------------
# Locale strings. Both dictionaries carry exactly the same keys; the completeness check at
# the bottom of this block fails the import rather than letting a hole reach the operator.
# --------------------------------------------------------------------------------------
LOCALES: Final[tuple[str, ...]] = ("en", "ru")
DEFAULT_LOCALE: Final[str] = "en"

STRINGS: Final[dict[str, dict[str, str]]] = {
    "en": {
        'title.cover': '✅ COVER TP · 1R',
        'title.pending': '🕓 ORDER PLACED',
        'title.partial': '🟡 PARTIAL ENTRY',
        'title.tp': '✅ TP{index} TAKEN',
        'title.sl': '🔴 STOP LOSS',
        'title.be': '🛡 BREAK EVEN',
        'title.cancelled': '⚪ CANCELLED',
        'label.mode': 'Mode',
        'label.total': 'Total',
        'label.remaining': 'Remaining',
        'mode.standard': 'Standard',
        'mode.ladder': 'Ladder',
        'mode.incomplete': 'Entry awaiting levels',
        'mode.be_after_cover': 'Runner to break even after cover TP',
        'mode.be_after_tp1': 'Remaining positions to break even after TP1',
        'mode.hold_channel_sl': 'Keep channel SL',
        'mode.ai_managed': 'Managed by AI',
        'status.protected': 'at break even',
        'status.waiting': 'SL unchanged',
        'label.cover': 'Cover · 1R',
        'label.runner': 'Runner',
        "title.opened": "🟢 ENTRY",
        "title.modified": "✏️ UPDATED",
        "title.closed": "⚪ CLOSED",
        "title.rejected": "⛔ ERROR",
        "title.attention": "⚠️ ATTENTION",
        "label.symbol": "Symbol",
        "label.volume": "Volume",
        "label.filled": "Filled",
        "label.entry": "Entry",
        "label.price": "Price",
        "label.sl": "Stop loss",
        "label.tp": "Take profit",
        "label.risk": "Risk",
        "label.ticket": "Ticket",
        "label.tickets": "Tickets",
        "label.reason": "Reason",
        "label.close_price": "Close price",
        "label.pl": "P/L",
        "label.cause": "Cause",
        "label.code": "Retcode",
        "label.broker": "Broker",
        "label.detail": "Detail",
        "label.action": "Action",
        "label.order_type": "Order type",
        "label.expiry": "Expiry",
        "label.split": "Part",
        "label.unmanaged_tp": "Unmanaged targets",
        "label.slippage": "Slippage",
        "label.signal": "Signal",
        "cause.tp": "take profit",
        "cause.sl": "stop loss",
        "cause.ai": "closed by the AI",
        "cause.manual": "closed manually",
        "cause.kill_switch": "kill switch",
        "cause.channel_delete": "channel deleted the signal",
        "cause.full_close": "full close",
        "cause.unknown": "unknown",
        "subject.stopless": "position without a stop",
        "subject.sl_fallback": "fallback stop applied",
        "subject.mt5_disconnected": "MT5 disconnected",
        "subject.provider_failing": "the AI provider is failing",
        "subject.queue_overflow": "the ingest queue overflowed",
        "link.signal_post": "Signal post",
        "unit.points": "pts",
        "value.none": "—",
        "side.buy": "BUY",
        "side.sell": "SELL",
        "cmd.start.heading": "LV Copytrader notifier",
        "cmd.start.running": "The bot is running.",
        "cmd.start.bound": "Bound chat: {chat}",
        "cmd.start.unbound": "No chat is bound yet — use Detect chat in Settings.",
        "cmd.start.allowlist_on": "Admin allowlist: on",
        "cmd.start.allowlist_off": "Admin allowlist: off",
        "cmd.start.notify_on": "Notifications: on",
        "cmd.start.notify_off": "Notifications: off",
        "cmd.start.hint": "Commands: /start /status /help",
        "cmd.help.heading": "Commands",
        "cmd.status.heading": "Status",
        "cmd.status.mt5": "MT5",
        "cmd.status.telegram": "Telegram",
        "cmd.status.arm": "ARM",
        "cmd.status.ok": "ok",
        "cmd.status.down": "down",
        "cmd.status.armed": "armed",
        "cmd.status.disarmed": "disarmed",
        "cmd.unknown": "Unknown command. Use /help.",
        "cmd.menu.start": "Bot and binding status",
        "cmd.menu.status": "MT5 / Telegram / ARM summary",
        "cmd.menu.help": "List commands",
    },
    "ru": {
        'title.cover': '✅ TP ПОКРЫТИЯ · 1R',
        'title.pending': '🕓 ОРДЕР ВЫСТАВЛЕН',
        'title.partial': '🟡 ЧАСТИЧНЫЙ ВХОД',
        'title.tp': '✅ TP{index} ЗАБРАН',
        'title.sl': '🔴 СТОП-ЛОСС',
        'title.be': '🛡 БЕЗУБЫТОК',
        'title.cancelled': '⚪ ОРДЕР ОТМЕНЁН',
        'label.mode': 'Режим',
        'label.total': 'Всего',
        'label.remaining': 'Осталось',
        'mode.standard': 'Обычный',
        'mode.ladder': 'Лесенка',
        'mode.incomplete': 'Вход · ожидаем уровни',
        'mode.be_after_cover': 'После TP покрытия — остаток в б/у',
        'mode.be_after_tp1': 'После TP1 — остаток в б/у',
        'mode.hold_channel_sl': 'Сохраняем SL канала',
        'mode.ai_managed': 'Сопровождение ИИ',
        'status.protected': 'в б/у',
        'status.waiting': 'SL сохранён',
        'label.cover': 'Покрытие · 1R',
        'label.runner': 'Остаток',
        "title.opened": "🟢 ВХОД",
        "title.modified": "✏️ ИЗМЕНЕНО",
        "title.closed": "⚪ ЗАКРЫТО",
        "title.rejected": "⛔ ОШИБКА",
        "title.attention": "⚠️ ВНИМАНИЕ",
        "label.symbol": "Символ",
        "label.volume": "Объём",
        "label.filled": "Исполнено",
        "label.entry": "Вход",
        "label.price": "Цена",
        "label.sl": "Стоп-лосс",
        "label.tp": "Тейк-профит",
        "label.risk": "Риск",
        "label.ticket": "Тикет",
        "label.tickets": "Тикеты",
        "label.reason": "Причина",
        "label.close_price": "Цена закрытия",
        "label.pl": "Приб./убыток",
        "label.cause": "Причина закрытия",
        "label.code": "Код",
        "label.broker": "Брокер",
        "label.detail": "Подробности",
        "label.action": "Действие",
        "label.order_type": "Тип ордера",
        "label.expiry": "Истекает",
        "label.split": "Часть",
        "label.unmanaged_tp": "Неуправляемые цели",
        "label.slippage": "Проскальзывание",
        "label.signal": "Сигнал",
        "cause.tp": "тейк-профит",
        "cause.sl": "стоп-лосс",
        "cause.ai": "закрыто ИИ",
        "cause.manual": "закрыто вручную",
        "cause.kill_switch": "аварийная остановка",
        "cause.channel_delete": "канал удалил сигнал",
        "cause.full_close": "полное закрытие",
        "cause.unknown": "неизвестно",
        "subject.stopless": "позиция без стопа",
        "subject.sl_fallback": "установлен аварийный стоп",
        "subject.mt5_disconnected": "MT5 отключён",
        "subject.provider_failing": "ИИ-провайдер недоступен",
        "subject.queue_overflow": "очередь сообщений переполнена",
        "link.signal_post": "Сообщение-сигнал",
        "unit.points": "пт",
        "value.none": "—",
        "side.buy": "BUY",
        "side.sell": "SELL",
        "cmd.start.heading": "Уведомления LV Copytrader",
        "cmd.start.running": "Бот работает.",
        "cmd.start.bound": "Привязанный чат: {chat}",
        "cmd.start.unbound": "Чат ещё не привязан — нажмите «Определить чат» в настройках.",
        "cmd.start.allowlist_on": "Список админов: включён",
        "cmd.start.allowlist_off": "Список админов: выключен",
        "cmd.start.notify_on": "Уведомления: включены",
        "cmd.start.notify_off": "Уведомления: выключены",
        "cmd.start.hint": "Команды: /start /status /help",
        "cmd.help.heading": "Команды",
        "cmd.status.heading": "Статус",
        "cmd.status.mt5": "MT5",
        "cmd.status.telegram": "Telegram",
        "cmd.status.arm": "ARM",
        "cmd.status.ok": "ок",
        "cmd.status.down": "нет",
        "cmd.status.armed": "включён",
        "cmd.status.disarmed": "выключен",
        "cmd.unknown": "Неизвестная команда. Используйте /help.",
        "cmd.menu.start": "Статус бота и привязки",
        "cmd.menu.status": "Сводка MT5 / Telegram / ARM",
        "cmd.menu.help": "Список команд",
    },
}


def _assert_locales_complete() -> None:
    """Fail the import when the two locale tables do not carry exactly the same keys."""
    reference = set(STRINGS[DEFAULT_LOCALE])
    for locale in LOCALES:
        table = STRINGS.get(locale)
        if table is None:
            raise RuntimeError(f"notifier locale table {locale!r} is missing")
        keys = set(table)
        missing = sorted(reference - keys)
        extra = sorted(keys - reference)
        if missing or extra:
            raise RuntimeError(
                f"notifier locale {locale!r} is not complete: missing={missing} extra={extra}"
            )


_assert_locales_complete()


def normalize_locale(value: Any) -> str:
    """Map anything the settings row holds onto one of the supported locales."""
    text = str(value or "").strip().lower().replace("_", "-")
    if text.startswith("ru"):
        return "ru"
    return DEFAULT_LOCALE


def render(key: str, locale: str = DEFAULT_LOCALE, **values: Any) -> str:
    """Return one locale string, formatted with `values`; a missing key renders as itself."""
    table = STRINGS.get(normalize_locale(locale), STRINGS[DEFAULT_LOCALE])
    template = table.get(key)
    if template is None:
        return key
    if not values:
        return template
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


# --------------------------------------------------------------------------------------
# Formatting helpers. Every number the operator reads is produced here, never by the model.
# --------------------------------------------------------------------------------------
def _utcnow() -> datetime:
    """Current UTC time as the timezone-naive datetime every column stores."""
    return datetime.now(UTC).replace(tzinfo=None)


def _truncate(text: str, limit: int) -> str:
    """Cut `text` to `limit` characters, marking the cut with an ellipsis."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + ELLIPSIS


def _esc(value: Any, *, limit: int = MAX_FIELD_CHARS) -> str:
    """HTML-escape one interpolated value after trimming it to `limit` characters."""
    text = "" if value is None else str(value)
    return html.escape(_truncate(text.strip(), limit), quote=True)


def _bold(text: str) -> str:
    """Wrap an already-escaped fragment in a bold tag."""
    return f"<b>{text}</b>"


def _code(value: str) -> str:
    """Wrap an already-escaped fragment in a monospace tag."""
    return f"<code>{value}</code>"


def _num(value: Any, *, limit: int = MAX_CODE_CHARS) -> str:
    """Escape a pre-formatted number and wrap it in monospace."""
    return _code(_esc(value, limit=limit))


def _fmt_number(
    value: Any,
    decimals: int | None = None,
    *,
    signed: bool = False,
    min_decimals: int = 0,
) -> str | None:
    """Format a number for display, trimming trailing zeros when no precision is given."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text or None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if decimals is None:
        text = f"{number:.8f}".rstrip("0").rstrip(".")
        if text in ("", "-", "-0"):
            text = "0"
    else:
        text = f"{number:.{max(0, int(decimals))}f}"
    if min_decimals > 0:
        whole, _, frac = text.partition(".")
        text = f"{whole}.{frac.ljust(min_decimals, '0')}" if (frac or min_decimals) else whole
    if signed and not text.startswith("-"):
        text = f"+{text}"
    return text


def _fmt_price(value: Any, digits: Any = None) -> str | None:
    """Format a price at the symbol's digit count when it is known."""
    precision: int | None = None
    if digits is not None and not isinstance(digits, bool):
        try:
            candidate = int(digits)
        except (TypeError, ValueError):
            candidate = -1
        if 0 <= candidate <= 10:
            precision = candidate
    return _fmt_number(value, precision)


def _fmt_volume(value: Any) -> str | None:
    """Format a lot size, always with at least two decimals."""
    return _fmt_number(value, None, min_decimals=2)


def _fmt_percent(value: Any) -> str | None:
    """Format a risk percentage."""
    text = _fmt_number(value, 2)
    return None if text is None else f"{text}%"


def _fmt_money(value: Any, currency: Any) -> str | None:
    """Format realized P/L in the account currency, always signed."""
    text = _fmt_number(value, 2, signed=True)
    if text is None:
        return None
    unit = str(currency or "").strip()
    return f"{text} {unit}" if unit else text


def _fmt_points(value: Any, locale: str) -> str | None:
    """Format a signed point distance with the localized unit."""
    text = _fmt_number(value, None, signed=True)
    if text is None:
        return None
    return f"{text} {render('unit.points', locale)}"


def _first(source: Mapping[str, Any], *names: str) -> Any:
    """Return the first value in `source` under `names` that is not None."""
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return None


def _as_sequence(value: Any) -> list[Any]:
    """Normalise a scalar / list / tuple of take-profit levels into a list."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        return [chunk.strip() for chunk in text.replace(";", ",").split(",") if chunk.strip()]
    if isinstance(value, Sequence):
        return [item for item in value if item is not None]
    return [value]


def _is_zero(value: Any) -> bool:
    """True when a value is numerically zero, whatever type it arrived as."""
    if value is None:
        return True
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _same_number(left: Any, right: Any) -> bool:
    """True when two volumes are the same to within lot precision."""
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return str(left) == str(right)


def _price_list(value: Any, digits: Any) -> list[str]:
    """Format a list of price levels as monospace fragments, dropping unset (zero) ones."""
    rendered: list[str] = []
    for item in _as_sequence(value):
        if _is_zero(item):
            continue
        text = _fmt_price(item, digits)
        if text is not None:
            rendered.append(_num(text))
    return rendered


def _side_word(value: Any, locale: str) -> str | None:
    """Print the direction as the word BUY or SELL, never as a bare colour or a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        if number == 0:
            return render("side.buy", locale)
        if number == 1:
            return render("side.sell", locale)
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if "buy" in text or text in ("long", "0"):
        return render("side.buy", locale)
    if "sell" in text or text in ("short", "1"):
        return render("side.sell", locale)
    return None


def _prepare_html(text: str) -> str:
    """Return Telegram-safe HTML: keep our markup, convert markdown, escape the rest."""
    return prepare_telegram_html(text)


def _link_html(link: str | None, locale: str) -> str:
    """Render the source-post link as an anchor, or nothing when there is no usable link."""
    if not link:
        return ""
    url = str(link).strip()
    if len(url) > MAX_LINK_CHARS:
        return ""
    if not url.lower().startswith(("http://", "https://", "tg://")):
        return ""
    label = html.escape(render("link.signal_post", locale), quote=False)
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>'


def _coerce_chat_id(value: Any) -> int | str | None:
    """Accept a numeric chat id or an @channelusername, and reject anything else."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("@") and len(text) > 1:
        return text
    try:
        return int(text)
    except ValueError:
        return None


def _chat_title(chat: Mapping[str, Any]) -> str:
    """Best human name for a chat: its title, the person's name, its @username, or its id."""
    title = str(chat.get("title") or "").strip()
    if title:
        return title
    names = " ".join(
        str(chat.get(part) or "").strip() for part in ("first_name", "last_name")
    ).strip()
    if names:
        return names
    username = str(chat.get("username") or "").strip()
    if username:
        return f"@{username}"
    return str(chat.get("id") or "").strip() or "chat"


def _coerce_admin_ids(value: Any) -> tuple[int, ...]:
    """Normalise the stored ``notifier.admin_ids`` row into positive, de-duplicated ids.

    Defensive by design, like every other settings reader: the row is JSON that may have
    been written by an older build or by hand, and one bad entry must cost that entry, never
    the whole allowlist. Dropping a malformed id is the safe direction — it can only make
    the list *shorter*, and a shorter list refuses more, never less.
    """
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return ()
    if not isinstance(value, Sequence):
        return ()
    ids: list[int] = []
    seen: set[int] = set()
    for item in value:
        if item is None or isinstance(item, bool):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            log.warning("notifier_admin_id_ignored reason=not_a_number value=%r", item)
            continue
        if number <= 0:
            log.warning("notifier_admin_id_ignored reason=not_positive value=%r", item)
            continue
        if number in seen:
            continue
        seen.add(number)
        ids.append(number)
    return tuple(ids)


@dataclass(frozen=True)
class UpdateSender:
    """One ``getUpdates`` entry reduced to who sent it and where a reply would go.

    ``user_id`` is the Telegram *user* id — the thing ``notifier.admin_ids`` holds — and it
    is ``None`` for an update that has no human behind it, such as a channel post. That is
    not the same as zero and must never be coerced into one: an update with no user id can
    never match an allowlist entry, which is exactly the outcome wanted.
    """

    user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    chat_id: int | str | None = None
    chat_title: str = ""
    chat_type: str = ""
    text: str = ""


def _clean_name(value: Any) -> str | None:
    """Return a trimmed display name, or None when the field is absent or blank."""
    text = str(value or "").strip()
    return text or None


def _bound_to_row(sender: UpdateSender) -> dict[str, Any] | None:
    """The `notifier.bound_to` settings row: who the bot was bound to, or None.

    Either a real numeric user id with whatever names came with it, or nothing at all. A
    channel post carries no account: storing a row of nulls for it would claim we know who
    the recipient is, and the whole point of this row is that the claim can be checked.
    """
    if sender.user_id is None:
        return None
    return {
        "user_id": sender.user_id,
        "username": sender.username or "",
        "first_name": sender.first_name or "",
    }


def _stranger_hint(sender: UpdateSender) -> str:
    """Name the non-admin account that messaged, so the operator can act on the right id."""
    if sender.user_id is None:
        return (
            "that message did not come from a Telegram user account, so it can never match "
            "an admin id — send a direct message from your own account instead"
        )
    if sender.username:
        who = f"@{sender.username}"
    elif sender.first_name:
        who = sender.first_name
    else:
        who = ""
    if who:
        return f"the account that messaged is {who}, id {sender.user_id}"
    return f"the account that messaged has id {sender.user_id}"


def _sender_from_update(update: Mapping[str, Any]) -> UpdateSender | None:
    """Reduce one update to an :class:`UpdateSender`, or None when it carries no chat."""
    for name in _UPDATE_MESSAGE_FIELDS:
        message = update.get(name)
        if not isinstance(message, Mapping):
            continue
        chat = message.get("chat")
        if not isinstance(chat, Mapping) or chat.get("id") is None:
            continue
        chat_id = _coerce_chat_id(chat.get("id"))
        if chat_id is None:
            continue
        sender = message.get("from")
        user_id: int | None = None
        username: str | None = None
        first_name: str | None = None
        if isinstance(sender, Mapping):
            raw_id = sender.get("id")
            if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                user_id = raw_id
            else:
                try:
                    user_id = int(str(raw_id).strip())
                except (TypeError, ValueError):
                    user_id = None
            username = _clean_name(sender.get("username"))
            first_name = _clean_name(sender.get("first_name"))
        body = message.get("text")
        if not isinstance(body, str) or not body.strip():
            body = message.get("caption")
        text = _truncate(str(body or "").strip(), MAX_SENDER_TEXT_CHARS)
        return UpdateSender(
            user_id=user_id,
            username=username,
            first_name=first_name,
            chat_id=chat_id,
            chat_title=_chat_title(chat),
            chat_type=str(chat.get("type") or "").strip(),
            text=text,
        )
    return None


def parse_bot_command(text: str) -> str | None:
    """Return the slash-command name (lowercase, no leading slash), or None.

    Accepts ``/start``, ``/start@BotName`` and ``/start extra``. Telegram's command
    menu always places the command at the start of the message; anything else is
    ordinary text and is ignored.
    """
    match = _BOT_COMMAND_RE.match((text or "").strip())
    if match is None:
        return None
    return match.group(1).lower()


def bot_command_menu(locale: str = DEFAULT_LOCALE) -> list[dict[str, str]]:
    """The ``setMyCommands`` payload: LV Copytrader's three commands, nothing else."""
    loc = normalize_locale(locale)
    return [
        {
            "command": name,
            "description": render(f"cmd.menu.{name}", loc)[:256],
        }
        for name in BOT_COMMAND_NAMES
    ]


def command_sender_allowed(sender: UpdateSender, config: NotifierConfig) -> bool:
    """True when this inbound DM may receive a command reply.

    Populated ``admin_ids``: the sender's *user* id is on the list *and* the chat is
    one ``send`` would deliver to (a private chat whose id equals that user id). A
    group is refused the same way notifications are — answering ``/status`` in a
    group would publish ARM/MT5 state to everyone in it.

    Empty allowlist: only the already-bound chat, matching the "anyone could have
    bound this bot, but now it talks to that one place" rule. Unbound and
    unenforced means nobody gets a reply; ``detect_chat`` is the setup path.
    """
    if sender.chat_id is None:
        return False
    if config.enforced:
        return config.contains(sender.user_id) and config.allows(sender.chat_id)
    if config.chat_id is None:
        return False
    return sender.chat_id == config.chat_id


# --------------------------------------------------------------------------------------
# Cached settings and one normalized Bot API result.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class NotifierConfig:
    """The notifier-relevant settings, cached so the sync builders can read the locale."""

    token: str = ""
    chat_id: int | str | None = None
    enabled: bool = False
    types: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_TYPES))
    locale: str = DEFAULT_LOCALE
    #: `notifier.admin_ids` — Telegram USER ids, not chat ids. Empty means unenforced.
    admin_ids: tuple[int, ...] = ()

    @property
    def configured(self) -> bool:
        """True when there is both a token and a chat to send to."""
        return bool(self.token) and self.chat_id is not None

    @property
    def enforced(self) -> bool:
        """True when an allowlist exists and therefore has to be obeyed."""
        return bool(self.admin_ids)

    def contains(self, user_id: int | None) -> bool:
        """True when this Telegram user id is literally on the allowlist.

        Membership, not permission: with an empty allowlist this is ``False`` for everyone,
        which is what the "who has messaged this bot" listing wants to report. Use
        :meth:`allows` for the question "may this be delivered".
        """
        if user_id is None or isinstance(user_id, bool):
            return False
        return user_id in self.admin_ids

    def allows(self, chat_id: int | str | None) -> bool:
        """True when a notification may be delivered to this chat.

        An empty allowlist allows everything — today's behaviour, kept deliberately. A
        populated one allows only a numeric chat id that is itself an admin id: an
        ``@channelusername`` cannot be matched against a numeric allowlist, and guessing
        that it belongs to an admin is exactly the guess this whole file exists to stop.
        """
        if not self.admin_ids:
            return True
        if chat_id is None or isinstance(chat_id, bool) or not isinstance(chat_id, int):
            return False
        return chat_id in self.admin_ids


@dataclass(frozen=True)
class _ApiResult:
    """One Bot API call, normalized into success, a reason, and whether retrying can help."""

    ok: bool
    result: Any = None
    error: str = ""
    status: int = 0
    retryable: bool = False
    entity_error: bool = False
    retry_after: float = 0.0


class Notifier:
    """Sends operator notifications through the operator's own Telegram bot."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime
        self._settings: Settings = getattr(runtime, "settings", None) or _module_settings
        self._bus: EventBus = getattr(runtime, "bus", None) or events.bus
        self._config = NotifierConfig()
        self._client: httpx.AsyncClient | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._watch_task: asyncio.Task[Any] | None = None
        self._command_task: asyncio.Task[Any] | None = None
        self._warned_unconfigured = False
        self._started = False
        #: Held by ``detect_chat`` / ``recent_senders`` for their whole window, and by
        #: the command poller for a single ``getUpdates``. Telegram allows only one
        #: consuming poller at a time; this lock is how detect and commands share it
        #: without eating each other's updates.
        self._updates_lock = asyncio.Lock()
        #: Next ``getUpdates`` offset shared by detect and the command poller.
        #: ``recent_senders`` is non-destructive and does not advance this.
        self._updates_offset = 0
        self._command_sync_lock = asyncio.Lock()
        self._entry_runs: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}

    def begin_run(self, run_id: str) -> None:
        """Collect entries across tool calls until the signal run finishes."""
        self._entry_runs.setdefault(run_id, {})

    def flush_run(self, run_id: str) -> None:
        for payload in self._entry_runs.pop(run_id, {}).values():
            link = payload.pop("link", None)
            self.fire(TYPE_OPENED, self.render_opened(**payload), link=link)

    def opened(self, run_id: str, *, link: str | None = None, **kw: Any) -> None:
        """One entry card per signal/instrument/direction, never by clock coincidence."""
        batch = self._entry_runs.get(run_id)
        if batch is None:
            self.fire(TYPE_OPENED, self.render_opened(**kw), link=link)
            return
        key = (kw.get("signal_ref"), kw.get("symbol"), kw.get("side"), kw.get("order_type"))
        previous = batch.get(key)
        if previous is None:
            batch[key] = dict(kw, link=link)
            return
        for name in ("legs", "tickets", "unmanaged_tps"):
            previous[name] = list(previous.get(name) or []) + list(kw.get(name) or [])
        for name in ("volume", "requested_volume", "filled_volume"):
            previous[name] = float(previous.get(name) or 0) + float(kw.get(name) or 0)
        previous["partial_fill"] = bool(previous.get("partial_fill") or kw.get("partial_fill"))

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Open the HTTP client, load settings, register slash commands, start the poller."""
        if self._started:
            return
        self._started = True
        # Reuse a client tests already injected; production start() has none yet.
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    READ_TIMEOUT_SECONDS,
                    connect=CONNECT_TIMEOUT_SECONDS,
                ),
                follow_redirects=False,
                headers={"User-Agent": f"lv-copytrader/{__version__}"},
            )
        await self._load_config()
        self._watch_task = asyncio.create_task(self._watch_settings(), name="notifier-settings")
        self._command_task = asyncio.create_task(
            self._poll_commands(), name="notifier-commands"
        )
        log.info(
            "notifier_started enabled=%s configured=%s locale=%s",
            self._config.enabled,
            self._config.configured,
            self._config.locale,
        )

    async def stop(self) -> None:
        """Let in-flight sends finish briefly, then cancel everything and close the client."""
        self._started = False
        for run_id in tuple(self._entry_runs):
            self.flush_run(run_id)
        for attr in ("_watch_task", "_command_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                log.warning("notifier_task_stop_failed task=%s error=%s", attr, exc)
        pending = tuple(self._tasks)
        if pending:
            _finished, still_running = await asyncio.wait(pending, timeout=STOP_GRACE_SECONDS)
            for task in still_running:
                task.cancel()
            if still_running:
                await asyncio.gather(*still_running, return_exceptions=True)
        self._tasks.clear()
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 - closing must not break shutdown
                log.warning("notifier_client_close_failed error=%s", exc)
        log.info("notifier_stopped")

    @property
    def locale(self) -> str:
        """The locale the builders currently render in."""
        return self._config.locale

    @property
    def config(self) -> NotifierConfig:
        """The cached notifier settings; refreshed on start, on send, and on settings changes."""
        return self._config

    # ---- sending -----------------------------------------------------------

    async def send(
        self, type: str, text: str, *, link: str | None = None, force: bool = False
    ) -> bool:
        """Send one notification, honouring the enable switches unless `force`; never raises."""
        ntype = (str(type or TYPE_ATTENTION).strip().lower() or TYPE_ATTENTION)[:16]
        body = text if isinstance(text, str) else str(text)
        try:
            config = await self._load_config()
            if not force and not self._type_enabled(config, ntype):
                log.debug("notify_suppressed type=%s", ntype)
                return False
            record = body if not link else f"{body}\n{link}"
            if not config.configured:
                self._warn_unconfigured(ntype)
                await self._record(ntype, record, ok=False, error=NOT_CONFIGURED_MESSAGE)
                return False
            if not config.allows(config.chat_id):
                # The allowlist is populated and this chat is not on it. Refuse here, before
                # the token and the body reach the wire: quietly, because the caller may be
                # the executor mid-trade, and loudly on the bus, because a notification the
                # operator expected has just been dropped and they must be told why.
                self._refuse_not_admin(ntype, config.chat_id)
                await self._record(ntype, record, ok=False, error=NOT_ADMIN_CHAT_MESSAGE)
                return False
            html_parts, plain_parts = self._compose_parts(body, link, config.locale)
            ok = True
            error: str | None = None
            for html_text, plain in zip(html_parts, plain_parts):
                payload = {
                    "chat_id": config.chat_id,
                    "text": html_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": dict(KEYBOARD_REMOVE),
                }
                ok, error = await self._send_with_retries(config.token, payload, plain, ntype, rich=True)
                if not ok:
                    break
            await self._record(ntype, record, ok=ok, error=error)
            if not ok:
                self._bus.emit(
                    events.SYSTEM_WARNING,
                    reason="notify_failed",
                    notification_type=ntype,
                    message=error or "the notification could not be delivered",
                )
            return ok
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a notification never breaks its caller
            log.warning("notify_failed type=%s error=%s", ntype, exc)
            self._bus.emit(
                events.SYSTEM_WARNING,
                reason="notify_failed",
                notification_type=ntype,
                message=redact(str(exc)),
            )
            return False

    def fire(self, type: str, text: str, *, link: str | None = None) -> None:
        """Schedule a send in the background and return immediately — trades never wait on it."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("notify_dropped_no_event_loop type=%s", type)
            return
        task = loop.create_task(self._fire(type, text, link), name=f"notify-{type}")
        # Holding the reference is load-bearing: asyncio keeps only a weak reference to a
        # task, so an unreferenced one can be garbage-collected mid-flight.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fire(self, type: str, text: str, link: str | None) -> None:
        """Body of the background task created by :meth:`fire`."""
        try:
            await self.send(type, text, link=link)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - already logged inside send()
            log.warning("notify_task_failed type=%s error=%s", type, exc)

    # ---- setup calls -------------------------------------------------------

    async def test_token(self, token: str) -> dict[str, Any]:
        """Validate a bot token with getMe and return the bot's username."""
        resolved = await self._resolve_token(token)
        if not resolved:
            return {"ok": False, "message": NO_TOKEN_MESSAGE}
        result = await self._call(resolved, "getMe", http_method="GET")
        if not result.ok:
            return {"ok": False, "message": self._describe(result), "error": result.error}
        info = result.result if isinstance(result.result, Mapping) else {}
        username = str(info.get("username") or "").strip()
        return {
            "ok": True,
            "bot_username": username,
            "bot_id": info.get("id"),
            "bot_name": str(info.get("first_name") or "").strip(),
            "message": f"@{username}" if username else "the bot token is valid",
        }

    async def detect_chat(self, token: str) -> dict[str, Any]:
        """Poll getUpdates until an allowed sender messages the bot, then store their chat.

        This exists to remove the one piece of friction that gets a self-hosted project
        abandoned: making the operator find their own numeric chat id by hand.

        With ``notifier.admin_ids`` **empty** it binds the first sender, exactly as it always
        has, and reports who that was — the caller is expected to show it, because a chat id
        with no name attached cannot be checked by the person who has to trust it.

        With ``notifier.admin_ids`` **populated** an update from anyone else is skipped and
        the poll keeps waiting. If the window then expires having seen only non-admins, the
        answer says so: "someone messaged the bot but their id is not on the list" is fixed
        by editing the list, while "nobody messaged the bot" is fixed by opening Telegram,
        and an operator who cannot tell those apart will do the wrong one repeatedly.
        """
        resolved = await self._resolve_token(token)
        if not resolved:
            return {"ok": False, "message": NO_TOKEN_MESSAGE}
        identity = await self._call(resolved, "getMe", http_method="GET")
        if not identity.ok:
            return {"ok": False, "message": self._describe(identity), "error": identity.error}
        info = identity.result if isinstance(identity.result, Mapping) else {}
        username = str(info.get("username") or "").strip()

        # Read once, up front: the allowlist in force when the operator pressed the button
        # is the one this attempt is judged against.
        config = await self._load_config()

        # Held for the whole detection window, including the sleeps between polls: the
        # command poller must not sneak a consuming getUpdates in between and confirm
        # the /start the operator just sent. recent_senders takes the same lock.
        async with self._updates_lock:
            return await self._detect_chat_locked(resolved, username, config)

    async def _detect_chat_locked(
        self, resolved: str, username: str, config: NotifierConfig
    ) -> dict[str, Any]:
        """Body of :meth:`detect_chat`; caller holds :attr:`_updates_lock`."""
        deadline = time.monotonic() + float(self._settings.NOTIFY_DETECT_SECONDS)
        poll_interval = float(self._settings.NOTIFY_POLL_SECONDS)
        last_error = ""
        stranger: UpdateSender | None = None
        group_sender: UpdateSender | None = None
        while True:
            params: dict[str, Any] = {
                "timeout": 0,
                "limit": 100,
                "allowed_updates": json.dumps(list(DETECT_ALLOWED_UPDATES)),
            }
            if self._updates_offset:
                params["offset"] = self._updates_offset
            result = await self._call(resolved, "getUpdates", params=params, http_method="GET")
            if result.ok:
                updates = result.result if isinstance(result.result, list) else []
                for update in updates:
                    if not isinstance(update, Mapping):
                        continue
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        # An increasing offset acknowledges what we have seen, so the same
                        # update is never handed back on the next poll — including the
                        # command poller, which shares this counter.
                        self._updates_offset = max(self._updates_offset, update_id + 1)
                    sender = _sender_from_update(update)
                    if sender is None or sender.chat_id is None:
                        continue
                    if config.enforced:
                        if not config.contains(sender.user_id):
                            # Remember them — the timeout message needs to name the id the
                            # operator would have to add — but do NOT bind them, and do not
                            # stop waiting for someone who is on the list.
                            stranger = sender
                            log.warning(
                                "notifier_detect_ignored_non_admin user_id=%s chat_id=%s",
                                sender.user_id,
                                sender.chat_id,
                            )
                            continue
                        if not config.allows(sender.chat_id):
                            # An admin, but writing from a group: the chat id is negative,
                            # so `send` would refuse every notification to it. Binding it
                            # would produce a bot that looks configured and delivers
                            # nothing, so detection and delivery are held to one rule.
                            group_sender = sender
                            log.warning(
                                "notifier_detect_ignored_group_chat user_id=%s chat_id=%s",
                                sender.user_id,
                                sender.chat_id,
                            )
                            continue
                    await self._store_binding(sender)
                    log.info(
                        "notifier_chat_detected chat_id=%s user_id=%s title=%s enforced=%s",
                        sender.chat_id,
                        sender.user_id,
                        sender.chat_title,
                        config.enforced,
                    )
                    # If they pressed Start (or /status /help) to bind, answer it here:
                    # detect consumed the update, so the command poller will never see it.
                    await self._dispatch_inbound(sender, self._config)
                    return {
                        "ok": True,
                        "chat_id": sender.chat_id,
                        "chat_title": sender.chat_title,
                        "chat_type": sender.chat_type,
                        "user_id": sender.user_id,
                        "username": sender.username,
                        "first_name": sender.first_name,
                        "is_admin": config.contains(sender.user_id),
                        "enforced": config.enforced,
                        "bot_username": username,
                    }
            else:
                last_error = result.error
                if result.status == 409:
                    return {
                        "ok": False,
                        "message": WEBHOOK_CONFLICT_MESSAGE,
                        "error": result.error,
                        "bot_username": username,
                    }
                if result.status in (401, 404):
                    return {
                        "ok": False,
                        "message": TOKEN_REJECTED_MESSAGE,
                        "error": result.error,
                        "bot_username": username,
                    }
                log.warning("notifier_detect_poll_failed error=%s", result.error)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))

        waited = int(self._settings.NOTIFY_DETECT_SECONDS)
        if group_sender is not None:
            # Reported ahead of `stranger`: this one messaged from an account that IS
            # allowed, so it is the nearer of the two to a working setup.
            log.warning("notifier_detect_only_group_chats chat_id=%s", group_sender.chat_id)
            return {
                "ok": False,
                "message": ADMIN_GROUP_CHAT_MESSAGE,
                "hint": f"that message came from chat {group_sender.chat_id}",
                "seen_user_id": group_sender.user_id,
                "seen_chat_id": group_sender.chat_id,
                "enforced": True,
                "bot_username": username,
                "waited_seconds": waited,
                "error": last_error,
            }
        if stranger is not None:
            # A completely different problem from silence, and it gets its own sentence.
            log.warning("notifier_detect_only_non_admins user_id=%s", stranger.user_id)
            return {
                "ok": False,
                "message": NON_ADMIN_ONLY_MESSAGE,
                "hint": _stranger_hint(stranger),
                "seen_user_id": stranger.user_id,
                "seen_username": stranger.username,
                "seen_first_name": stranger.first_name,
                "enforced": True,
                "bot_username": username,
                "waited_seconds": waited,
                "error": last_error,
            }
        hint = (
            f"open @{username} in Telegram and press Start, then try again"
            if username
            else "open the bot in Telegram and press Start, then try again"
        )
        return {
            "ok": False,
            "message": NO_MESSAGE_RECEIVED_MESSAGE,
            "hint": hint,
            "enforced": config.enforced,
            "bot_username": username,
            "waited_seconds": waited,
            "error": last_error,
        }

    async def recent_senders(self, token: str) -> dict[str, Any]:
        """Report everyone who has messaged the bot recently, flagged against the allowlist.

        This is the answer to "what is my numeric id", and it exists so populating
        ``notifier.admin_ids`` does not require leaving the application for a third-party
        lookup bot. It polls for :data:`WHOAMI_WINDOW_SECONDS` — long enough to press the
        button and then send a message, short enough to sit and watch — and returns as soon
        as it has anything.

        **It is non-destructive.** ``getUpdates`` is called with no ``offset``, so nothing is
        confirmed and nothing is consumed: the updates :meth:`detect_chat` needs are still
        pending when this returns. Duplicates are suppressed locally by ``update_id``
        instead, and one row is emitted per sender rather than per message, carrying their
        most recent text.

        The command poller is paused for this window (the same lock detect holds) so a
        consuming ``getUpdates`` cannot confirm the message the operator just sent to
        look up their id.
        """
        resolved = await self._resolve_token(token)
        if not resolved:
            return {"ok": False, "message": NO_TOKEN_MESSAGE, "updates": []}
        identity = await self._call(resolved, "getMe", http_method="GET")
        if not identity.ok:
            return {
                "ok": False,
                "message": self._describe(identity),
                "error": identity.error,
                "updates": [],
            }
        info = identity.result if isinstance(identity.result, Mapping) else {}
        username = str(info.get("username") or "").strip()

        config = await self._load_config()
        async with self._updates_lock:
            return await self._recent_senders_locked(resolved, username, config)

    async def _recent_senders_locked(
        self, resolved: str, username: str, config: NotifierConfig
    ) -> dict[str, Any]:
        """Body of :meth:`recent_senders`; caller holds :attr:`_updates_lock`."""
        deadline = time.monotonic() + WHOAMI_WINDOW_SECONDS
        poll_interval = float(self._settings.NOTIFY_POLL_SECONDS)
        seen_updates: set[int] = set()
        # Keyed by (user_id, chat_id) so one person sending five messages is one row; the
        # dict preserves insertion order, so the list reads oldest sender first.
        rows: dict[tuple[int, Any], dict[str, Any]] = {}
        last_error = ""
        while True:
            params: dict[str, Any] = {
                "timeout": 0,
                "limit": 100,
                "allowed_updates": json.dumps(list(DETECT_ALLOWED_UPDATES)),
            }
            result = await self._call(resolved, "getUpdates", params=params, http_method="GET")
            if result.ok:
                updates = result.result if isinstance(result.result, list) else []
                for update in updates:
                    if not isinstance(update, Mapping):
                        continue
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        if update_id in seen_updates:
                            continue
                        seen_updates.add(update_id)
                    sender = _sender_from_update(update)
                    if sender is None or sender.user_id is None or sender.chat_id is None:
                        # No user id means nothing that could ever go on the allowlist.
                        continue
                    rows[(sender.user_id, sender.chat_id)] = {
                        "user_id": sender.user_id,
                        "username": sender.username or "",
                        "first_name": sender.first_name or "",
                        "chat_id": sender.chat_id,
                        "text": sender.text,
                        "is_admin": config.contains(sender.user_id),
                    }
            else:
                last_error = result.error
                if result.status == 409:
                    return {
                        "ok": False,
                        "message": WEBHOOK_CONFLICT_MESSAGE,
                        "error": result.error,
                        "bot_username": username,
                        "updates": [],
                    }
                if result.status in (401, 404):
                    return {
                        "ok": False,
                        "message": TOKEN_REJECTED_MESSAGE,
                        "error": result.error,
                        "bot_username": username,
                        "updates": [],
                    }
                log.warning("notifier_whoami_poll_failed error=%s", result.error)
            if rows:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))

        listed = list(rows.values())
        log.info("notifier_whoami senders=%d enforced=%s", len(listed), config.enforced)
        return {
            "ok": True,
            "updates": listed,
            "message": "" if listed else NO_RECENT_SENDERS_MESSAGE,
            "enforced": config.enforced,
            "bot_username": username,
            "error": last_error,
        }

    # ---- inbound slash commands -------------------------------------------

    async def sync_bot_commands(self) -> bool:
        """Wipe leftover BotFather commands and register LV Copytrader's slash menu.

        ``setMyCommands`` overwrites one (scope, language) pair. A token reused from
        another product may have ``/plan`` sitting under ``en``, ``ru``, or the default
        language, so we delete those three first and then write our set to each.
        ``deleteWebhook`` is intentionally not called: this bot never installs a
        webhook, and a 409 from ``getUpdates`` is reported rather than "fixed".
        """
        config = await self._load_config()
        if not config.token:
            return False
        async with self._command_sync_lock:
            commands_by_locale = {
                "": bot_command_menu(config.locale),
                "en": bot_command_menu("en"),
                "ru": bot_command_menu("ru"),
            }
            for language_code in ("", "en", "ru"):
                delete_params: dict[str, Any] = {}
                if language_code:
                    delete_params["language_code"] = language_code
                deleted = await self._call(config.token, "deleteMyCommands", params=delete_params)
                if not deleted.ok:
                    log.warning(
                        "notifier_delete_commands_failed language=%s error=%s",
                        language_code or "default",
                        deleted.error,
                    )
                set_params: dict[str, Any] = {"commands": commands_by_locale[language_code]}
                if language_code:
                    set_params["language_code"] = language_code
                written = await self._call(config.token, "setMyCommands", params=set_params)
                if not written.ok:
                    log.warning(
                        "notifier_set_commands_failed language=%s error=%s",
                        language_code or "default",
                        written.error,
                    )
                    return False
            log.info("notifier_commands_registered locale=%s", config.locale)
            return True

    def _schedule_command_sync(self) -> None:
        """Register slash commands in the background so a Telegram outage cannot stall boot."""
        if not self._config.token:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._sync_bot_commands_safe(), name="notifier-set-commands")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sync_bot_commands_safe(self) -> None:
        """:meth:`sync_bot_commands` that never raises into the event loop."""
        try:
            await asyncio.wait_for(
                self.sync_bot_commands(), timeout=COMMAND_SYNC_TIMEOUT_SECONDS
            )
        except TimeoutError:
            log.warning("notifier_set_commands_timed_out")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - menu registration must not break the notifier
            log.warning("notifier_set_commands_failed error=%s", exc)

    async def handle_sender_command(self, sender: UpdateSender) -> bool:
        """Answer one inbound command from `sender` if they are allowed. Never raises."""
        try:
            config = await self._load_config()
            return await self._dispatch_inbound(sender, config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a command reply never breaks its caller
            log.warning("notifier_command_failed error=%s", exc)
            return False

    async def _poll_commands(self) -> None:
        """Consume getUpdates for slash commands whenever detect/whoami are not running.

        Choice, documented: there is one consuming offset (:attr:`_updates_offset`) and
        one lock (:attr:`_updates_lock`). This loop takes the lock for a single
        ``timeout=0`` poll; :meth:`detect_chat` and :meth:`recent_senders` hold it for
        their whole window. Updates that arrive during detection are handled by detect
        (and answered there if they are a command). We do not call ``deleteWebhook``.
        """
        try:
            while True:
                try:
                    await self._poll_commands_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep polling regardless
                    log.warning("notifier_command_poll_failed error=%s", exc)
                interval = float(self._settings.NOTIFY_POLL_SECONDS)
                if not self._config.token:
                    interval = max(interval, 5.0)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    async def _poll_commands_once(self) -> None:
        """One consuming getUpdates + command dispatch. No-op without a token."""
        config = await self._load_config()
        if not config.token:
            return
        async with self._updates_lock:
            # Token may have been cleared while we waited for detect/whoami.
            if not self._config.token:
                return
            params: dict[str, Any] = {
                "timeout": 0,
                "limit": 100,
                "allowed_updates": json.dumps(list(DETECT_ALLOWED_UPDATES)),
            }
            if self._updates_offset:
                params["offset"] = self._updates_offset
            result = await self._call(
                self._config.token, "getUpdates", params=params, http_method="GET"
            )
            if not result.ok:
                if result.status == 409:
                    log.warning(
                        "notifier_command_poll_webhook_conflict error=%s", result.error
                    )
                else:
                    log.warning("notifier_command_poll_failed error=%s", result.error)
                return
            updates = result.result if isinstance(result.result, list) else []
            config = self._config
            for update in updates:
                if not isinstance(update, Mapping):
                    continue
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._updates_offset = max(self._updates_offset, update_id + 1)
                sender = _sender_from_update(update)
                if sender is None:
                    continue
                await self._dispatch_inbound(sender, config)

    async def _dispatch_inbound(self, sender: UpdateSender, config: NotifierConfig) -> bool:
        """Reply to a known slash command from an allowed sender; ignore everything else."""
        command = parse_bot_command(sender.text)
        if command is None:
            return False
        if command in LEGACY_BOT_COMMANDS:
            # Named so a leftover /plan tap is an explicit no, not a mystery silence —
            # but only for someone we would have answered anyway.
            if not command_sender_allowed(sender, config):
                log.warning(
                    "notifier_command_ignored_non_admin user_id=%s chat_id=%s command=%s",
                    sender.user_id,
                    sender.chat_id,
                    command,
                )
                return False
            return await self._reply_command(sender, "unknown", config)
        if command not in ALLOWED_BOT_COMMANDS:
            if not command_sender_allowed(sender, config):
                return False
            return await self._reply_command(sender, "unknown", config)
        if not command_sender_allowed(sender, config):
            log.warning(
                "notifier_command_ignored_non_admin user_id=%s chat_id=%s command=%s",
                sender.user_id,
                sender.chat_id,
                command,
            )
            return False
        return await self._reply_command(sender, command, config)

    async def _reply_command(
        self, sender: UpdateSender, command: str, config: NotifierConfig
    ) -> bool:
        """Send the command body to `sender`'s chat. Never a keyboard of buttons."""
        if sender.chat_id is None or not config.token:
            return False
        locale = config.locale
        if command == "start":
            text = self.render_cmd_start(config)
        elif command == "status":
            text = await self._render_cmd_status(config)
        elif command == "help":
            text = self.render_cmd_help(locale)
        else:
            text = _esc(render("cmd.unknown", locale))
        html_parts, plain_parts = self._compose_parts(text, None, locale)
        ok = True
        error: str | None = None
        for html_text, plain in zip(html_parts, plain_parts):
            payload = {
                "chat_id": sender.chat_id,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": dict(KEYBOARD_REMOVE),
            }
            ok, error = await self._send_with_retries(config.token, payload, plain, "command")
            if not ok:
                break
        if not ok:
            log.warning("notifier_command_reply_failed command=%s error=%s", command, error)
        return ok

    def render_cmd_start(self, config: NotifierConfig) -> str:
        """``/start``: the bot is up, and whether a chat is bound."""
        locale = config.locale
        lines = [
            _bold(_esc(render("cmd.start.heading", locale), limit=MAX_CODE_CHARS)),
            _esc(render("cmd.start.running", locale)),
        ]
        if config.chat_id is not None:
            lines.append(_esc(render("cmd.start.bound", locale, chat=str(config.chat_id))))
        else:
            lines.append(_esc(render("cmd.start.unbound", locale)))
        lines.append(
            _esc(
                render(
                    "cmd.start.allowlist_on" if config.enforced else "cmd.start.allowlist_off",
                    locale,
                )
            )
        )
        lines.append(
            _esc(
                render(
                    "cmd.start.notify_on" if config.enabled else "cmd.start.notify_off",
                    locale,
                )
            )
        )
        lines.append(_esc(render("cmd.start.hint", locale)))
        return "\n".join(lines)

    async def _render_cmd_status(self, config: NotifierConfig) -> str:
        """``/status``: MT5 / Telegram / ARM from the runtime health snapshot."""
        try:
            health = await self._runtime.snapshot_status()
        except Exception as exc:  # noqa: BLE001 - a status command still has to answer
            log.warning("notifier_status_snapshot_failed error=%s", exc)
            health = self._runtime.status_payload() if self._runtime is not None else {}
        return self.render_cmd_status(config, health)

    def render_cmd_status(self, config: NotifierConfig, health: Mapping[str, Any] | None = None) -> str:
        """Render the ``/status`` body from a health mapping (runtime snapshot)."""
        locale = config.locale
        payload = dict(health or {})
        lines = [_bold(_esc(render("cmd.status.heading", locale), limit=MAX_CODE_CHARS))]
        lines.append(
            self._line("cmd.status.mt5", self._health_dot(payload.get("mt5"), locale), locale)
        )
        lines.append(
            self._line(
                "cmd.status.telegram",
                self._health_dot(payload.get("telegram"), locale),
                locale,
            )
        )
        armed = bool(payload.get("armed", False))
        arm = render("cmd.status.armed" if armed else "cmd.status.disarmed", locale)
        lines.append(self._line("cmd.status.arm", _esc(arm), locale))
        return "\n".join(lines)

    def render_cmd_help(self, locale: str | None = None) -> str:
        """``/help``: the three commands and nothing from the old product."""
        loc = normalize_locale(locale or self._config.locale)
        lines = [_bold(_esc(render("cmd.help.heading", loc), limit=MAX_CODE_CHARS))]
        for name in BOT_COMMAND_NAMES:
            lines.append(f"/{name} — {_esc(render(f'cmd.menu.{name}', loc))}")
        return "\n".join(lines)

    @staticmethod
    def _health_dot(entry: Any, locale: str) -> str:
        """``ok`` or ``down — error`` for one runtime status key."""
        mapping = entry if isinstance(entry, Mapping) else {}
        ok = bool(mapping.get("ok"))
        word = render("cmd.status.ok" if ok else "cmd.status.down", locale)
        error = str(mapping.get("error") or "").strip()
        if not ok and error:
            return f"{_esc(word)} — {_esc(error, limit=200)}"
        return _esc(word)

    # ---- message builders --------------------------------------------------

    def render_opened(self, **kw: Any) -> str:
        """Render the 'position opened' notification (§7: symbol, side, volume, entry, SL, TPs, risk, reason)."""
        locale = self._locale_of(kw)
        digits = _first(kw, "digits")
        title = "title.pending" if kw.get("pending") else "title.opened"
        if kw.get("partial_fill") or kw.get("failures"):
            title = "title.partial"
        lines = [self._head(title, kw, locale)]
        kind = str(kw.get("kind") or "standard")
        lines.append(self._line("label.mode", _esc(render(f"mode.{kind}", locale)), locale))
        legs = [leg for leg in kw.get("legs", []) if leg.get("ok", True)]
        if legs:
            total = sum(float(leg.get("filled_volume") or leg.get("volume") or 0) for leg in legs)
            volumes = [_fmt_volume(leg.get("filled_volume") or leg.get("volume")) for leg in legs]
            summary = " + ".join(_num(v) for v in volumes if v)
            if len(legs) > 1:
                summary += f" = {_num(_fmt_volume(total))}"
            lines.append(self._line("label.volume", summary, locale))
            for index, leg in enumerate(legs, 1):
                entry = _fmt_price(leg.get("fill_price") or kw.get("entry"), digits)
                tp = _fmt_price(leg.get("tp"), digits)
                sl = _fmt_price(leg.get("sl", kw.get("sl")), digits)
                role = leg.get("role")
                name = render(f"label.{role}", locale) if role in {"cover", "runner"} else str(index)
                parts = [f"{_bold(_esc(name))} · {_num(entry or '—')}"]
                if sl and not _is_zero(sl):
                    parts.append(f"SL {_num(sl)}")
                else:
                    parts.append("SL — ⚠️")
                if tp and not _is_zero(tp):
                    parts.append(f"TP {_num(tp)}")
                lines.append(" · ".join(parts))
        else:
            for label, value in (
                ("label.volume", _fmt_volume(_first(kw, "volume", "filled_volume", "total_volume"))),
                ("label.entry", _fmt_price(_first(kw, "entry", "entry_price", "fill_price", "price"), digits)),
                ("label.sl", _fmt_price(_first(kw, "sl", "stop_loss"), digits)),
            ):
                if value is not None:
                    lines.append(self._line(label, _num(value), locale))
            tps = _price_list(_first(kw, "tps", "tp", "take_profits"), digits)
            if tps:
                lines.append(self._line("label.tp", ", ".join(tps), locale))
        requested, filled = kw.get("requested_volume"), kw.get("filled_volume")
        if requested is not None and filled is not None and not _same_number(requested, filled):
            lines.append(self._line("label.filled", f"{_num(_fmt_volume(filled))} / {_num(_fmt_volume(requested))}", locale))
        mode = kw.get("runner_mode")
        if mode:
            lines.append(_esc(render(f"mode.{mode}", locale)))
        risk = _fmt_percent(_first(kw, "risk_percent", "risk"))
        if risk is not None:
            lines.append(self._line("label.risk", _num(risk), locale))
        unmanaged = _price_list(kw.get("unmanaged_tps"), digits)
        if unmanaged:
            lines.append(self._line("label.unmanaged_tp", ", ".join(unmanaged), locale))
        lines.extend(self._ticket_lines(kw, locale))
        return "\n".join(lines)

    def render_modified(self, **kw: Any) -> str:
        """Render the 'position/order modified' notification as old -> new per changed field."""
        locale = self._locale_of(kw)
        digits = _first(kw, "digits")
        title = "title.be" if kw.get("breakeven") else "title.modified"
        lines = [self._head(title, kw, locale)]
        lines.extend(self._ticket_lines(kw, locale))
        changes = self._collect_changes(kw)
        if changes:
            for field_name, old, new in changes:
                label_key, is_price = _CHANGE_FIELDS.get(field_name, ("", False))
                if is_price:
                    old_text = _fmt_price(old, digits)
                    new_text = _fmt_price(new, digits)
                elif field_name == "volume":
                    old_text = _fmt_volume(old)
                    new_text = _fmt_volume(new)
                else:
                    old_text = None if old is None else str(old)
                    new_text = None if new is None else str(new)
                none_text = render("value.none", locale)
                pair = (
                    f"{_num(old_text if old_text is not None else none_text)} {ARROW} "
                    f"{_num(new_text if new_text is not None else none_text)}"
                )
                if label_key:
                    lines.append(self._line(label_key, pair, locale))
                else:
                    lines.append(f"{_bold(_esc(field_name, limit=MAX_CODE_CHARS))} {pair}")
        else:
            detail = _first(kw, "changed", "detail", "what")
            if detail is not None:
                lines.append(self._line("label.detail", _esc(detail), locale))
        lines.extend(self._reason_lines(kw, locale))
        return "\n".join(lines)

    def render_closed(self, **kw: Any) -> str:
        """Render the 'position closed' notification: cause, plus realized P/L in currency and points."""
        locale = self._locale_of(kw)
        digits = _first(kw, "digits")
        cause = _first(kw, "close_cause", "cause")
        title = "title.closed"
        if kw.get("status") == "cancelled":
            title = "title.cancelled"
        elif cause == "tp":
            title = "title.cover" if kw.get("ladder_role") == "cover" else "title.tp"
        elif cause == "sl":
            title = "title.be" if kw.get("breakeven") else "title.sl"
        head = self._head(title, kw, locale)
        head = head.replace("{index}", str(kw.get("tp_index") or ""))
        lines = [head]

        cause = _first(kw, "close_cause", "cause")
        lines.append(self._line("label.cause", _esc(_cause_text(cause, locale)), locale))
        volume = _fmt_volume(_first(kw, "volume", "closed_volume", "filled_volume"))
        if volume is not None:
            lines.append(self._line("label.volume", _num(volume), locale))
        entry = _fmt_price(_first(kw, "entry", "entry_price"), digits)
        if entry is not None:
            lines.append(self._line("label.entry", _num(entry), locale))
        close_price = _fmt_price(_first(kw, "close_price", "price", "fill_price"), digits)
        if close_price is not None:
            lines.append(self._line("label.close_price", _num(close_price), locale))

        money = _fmt_money(_first(kw, "profit", "pl", "realized_profit"), _first(kw, "currency"))
        points = _fmt_points(_derive_points(kw), locale)
        none_text = render("value.none", locale)
        pl_value = f"{_num(money if money is not None else none_text)} · {_num(points if points is not None else none_text)}"
        lines.append(self._line("label.pl", pl_value, locale))
        lines.extend(self._ticket_lines(kw, locale))
        for remaining in kw.get("remaining", []):
            state = render("status.protected" if remaining.get("breakeven") else "status.waiting", locale)
            lines.append(f"{_num(remaining['ticket'])} · {_esc(state)} · SL {_num(_fmt_price(remaining.get('sl'), digits) or '—')}")
        return "\n".join(lines)

    def render_rejected(self, **kw: Any) -> str:
        """Render a broker rejection: the retcode name and the human sentence behind it."""
        locale = self._locale_of(kw)
        lines = [self._head("title.rejected", kw, locale)]
        action = _first(kw, "action", "tool", "operation")
        if action is not None:
            lines.append(self._line("label.action", _num(action), locale))
        retcode = _first(kw, "retcode")
        retcode_name = _first(kw, "retcode_name")
        if retcode is not None or retcode_name is not None:
            parts: list[str] = []
            if retcode is not None:
                parts.append(_num(retcode))
            if retcode_name is not None:
                parts.append(_bold(_esc(retcode_name, limit=MAX_CODE_CHARS)))
            lines.append(self._line("label.code", " ".join(parts), locale))
        broker = _first(kw, "message", "broker_message", "comment")
        if broker is not None:
            lines.append(self._line("label.broker", _esc(broker), locale))
        lines.extend(self._ticket_lines(kw, locale))
        lines.extend(self._signal_lines(kw, locale))
        lines.extend(self._reason_lines(kw, locale))
        return "\n".join(lines)

    def render_attention(self, **kw: Any) -> str:
        """Render an attention notice: a stopless episode, an SL fallback, MT5/provider/queue trouble."""
        locale = self._locale_of(kw)
        subject = _first(kw, "subject", "kind", "event")
        title = render("title.attention", locale)
        head = _bold(_esc(title, limit=MAX_CODE_CHARS))
        subject_text = _subject_text(subject, locale)
        if subject_text:
            head = f"{head} — {_esc(subject_text, limit=MAX_CODE_CHARS * 2)}"
        lines = [head]
        symbol = _first(kw, "symbol")
        if symbol is not None:
            lines.append(self._line("label.symbol", _num(symbol), locale))
        lines.extend(self._ticket_lines(kw, locale))
        detail = _first(kw, "detail", "message", "text")
        if detail is not None:
            lines.append(self._line("label.detail", _esc(detail), locale))
        lines.extend(self._reason_lines(kw, locale))
        return "\n".join(lines)

    # ---- builder helpers ---------------------------------------------------

    def _locale_of(self, kw: Mapping[str, Any]) -> str:
        """Locale for one render call: an explicit override, else the cached `ui.locale`."""
        override = kw.get("locale")
        if override:
            return normalize_locale(override)
        return self._config.locale

    def _line(self, label_key: str, value: str, locale: str) -> str:
        """One `<b>Label</b> value` row; `value` must already be escaped HTML."""
        label = html.escape(render(label_key, locale), quote=False)
        return f"{_bold(label)} {value}"

    def _head(self, title_key: str, kw: Mapping[str, Any], locale: str) -> str:
        """Headline: the event title, then the direction as a word, then the symbol."""
        head = _bold(html.escape(render(title_key, locale), quote=False))
        tail: list[str] = []
        side = _side_word(_first(kw, "side", "direction", "position_type", "order_side"), locale)
        if side:
            tail.append(_bold(html.escape(side, quote=False)))
        symbol = _first(kw, "symbol", "name")
        if symbol is not None:
            tail.append(_bold(_esc(symbol, limit=MAX_CODE_CHARS)))
        if tail:
            return f"{head} — {' '.join(tail)}"
        return head

    def _ticket_lines(self, kw: Mapping[str, Any], locale: str) -> list[str]:
        """Render the ticket, or the list of tickets a multi-TP split produced."""
        tickets = _as_sequence(_first(kw, "tickets"))
        if tickets:
            rendered = [_num(item) for item in tickets]
            if len(rendered) == 1:
                return [self._line("label.ticket", rendered[0], locale)]
            return [self._line("label.tickets", ", ".join(rendered), locale)]
        ticket = _first(kw, "ticket")
        if ticket is not None:
            return [self._line("label.ticket", _num(ticket), locale)]
        return []

    def _signal_lines(self, kw: Mapping[str, Any], locale: str) -> list[str]:
        """Render the signal_ref, so a notification names the channel post behind it."""
        signal_ref = _first(kw, "signal_ref")
        if signal_ref is None:
            return []
        text = str(signal_ref).strip()
        if not text:
            return []
        return [self._line("label.signal", _num(text), locale)]

    def _reason_lines(self, kw: Mapping[str, Any], locale: str) -> list[str]:
        """Render the model's `reason` — the sentence §5.3 requires on every write tool."""
        reason = _first(kw, "reason", "open_reason")
        if reason is None:
            return []
        text = str(reason).strip()
        if not text:
            return []
        return [self._line("label.reason", _esc(text), locale)]

    @staticmethod
    def _collect_changes(kw: Mapping[str, Any]) -> list[tuple[str, Any, Any]]:
        """Normalise every shape a caller may describe a modification with into old/new triples."""
        collected: list[tuple[str, Any, Any]] = []
        raw = kw.get("changes")
        if isinstance(raw, Mapping):
            for name, value in raw.items():
                if isinstance(value, Mapping):
                    collected.append(
                        (str(name), _first(value, "old", "from", "before"), _first(value, "new", "to", "after"))
                    )
                elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
                    collected.append((str(name), value[0], value[1]))
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for item in raw:
                if isinstance(item, Mapping):
                    name = _first(item, "field", "name", "what")
                    if name is None:
                        continue
                    collected.append(
                        (str(name), _first(item, "old", "from", "before"), _first(item, "new", "to", "after"))
                    )
                elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 3:
                    collected.append((str(item[0]), item[1], item[2]))
        for name in _CHANGE_FIELDS:
            old = _first(kw, f"old_{name}", f"{name}_old", f"previous_{name}")
            new = _first(kw, f"new_{name}", f"{name}_new")
            if old is None and new is None:
                continue
            if any(existing[0] == name for existing in collected):
                continue
            collected.append((name, old, new))
        return collected

    # ---- settings ----------------------------------------------------------

    async def _load_config(self) -> NotifierConfig:
        """Re-read the notifier settings; on a database failure keep the cached values."""
        try:
            async with session_scope() as session:
                token = str(await get_setting(session, "notifier.bot_token", "") or "").strip()
                chat_raw = await get_setting(session, "notifier.chat_id", None)
                enabled = bool(await get_setting(session, "notifier.enabled", False))
                types_raw = await get_setting(session, "notifier.types", None)
                admins_raw = await get_setting(session, "notifier.admin_ids", None)
                locale_raw = await get_setting(session, "ui.locale", DEFAULT_LOCALE)
        except Exception as exc:  # noqa: BLE001 - a broken store must not break notifications
            log.warning("notifier_settings_read_failed error=%s", exc)
            return self._config
        types = dict(DEFAULT_TYPES)
        if isinstance(types_raw, Mapping):
            for key, value in types_raw.items():
                types[str(key).strip().lower()] = bool(value)
        config = NotifierConfig(
            token=token,
            chat_id=_coerce_chat_id(chat_raw),
            enabled=enabled,
            types=types,
            locale=normalize_locale(locale_raw),
            admin_ids=_coerce_admin_ids(admins_raw),
        )
        if token:
            # Registered, never logged: from here on every log line masks this literal.
            register_secret(token)
        if config != self._config:
            previous = self._config
            self._warned_unconfigured = False
            self._config = config
            if self._started:
                self._on_config_changed(previous, config)
        return self._config

    def _on_config_changed(self, previous: NotifierConfig, config: NotifierConfig) -> None:
        """Re-register the slash menu when the token or locale changes; reset offset on rotate."""
        if previous.token != config.token:
            self._updates_offset = 0
        if config.token and (
            previous.token != config.token or previous.locale != config.locale
        ):
            self._schedule_command_sync()

    async def _watch_settings(self) -> None:
        """Reload the cached settings whenever anything emits SETTINGS_CHANGED."""
        queue = self._bus.subscribe()
        try:
            while True:
                envelope = await queue.get()
                if not isinstance(envelope, Mapping):
                    continue
                if envelope.get("type") != events.SETTINGS_CHANGED:
                    continue
                try:
                    await self._load_config()
                except Exception as exc:  # noqa: BLE001 - keep watching regardless
                    log.warning("notifier_settings_reload_failed error=%s", exc)
        except asyncio.CancelledError:
            raise
        finally:
            self._bus.unsubscribe(queue)

    async def _resolve_token(self, token: str | None) -> str:
        """Use the token the operator just typed, or the stored one when the field is masked."""
        candidate = str(token or "").strip()
        if candidate and candidate != MASK:
            register_secret(candidate)
            return candidate
        config = await self._load_config()
        return config.token

    async def _store_binding(self, sender: UpdateSender) -> None:
        """Persist the detected chat id **and who it belongs to**.

        The identity is stored beside the id because a bare number answers "where do
        notifications go" and not "who is this bot talking to", and the second question is
        the one that matters when the allowlist is empty and anyone could have bound it.
        """
        chat_id = sender.chat_id
        if chat_id is None:
            return
        try:
            async with session_scope() as session:
                await set_setting(session, "notifier.chat_id", chat_id)
                await set_setting(session, "notifier.bound_to", _bound_to_row(sender))
        except Exception as exc:  # noqa: BLE001 - report the detection even if the write fails
            log.warning("notifier_chat_id_persist_failed error=%s", exc)
            return
        self._config = replace(self._config, chat_id=chat_id)
        self._warned_unconfigured = False

    @staticmethod
    def _type_enabled(config: NotifierConfig, ntype: str) -> bool:
        """Every type has its own switch (§7); an unknown type defaults to on."""
        if not config.enabled:
            return False
        return bool(config.types.get(ntype, True))

    def _refuse_not_admin(self, ntype: str, chat_id: int | str | None) -> None:
        """Log and announce a notification refused because its chat is not an admin.

        Unlike :meth:`_warn_unconfigured` this is *not* rate-limited to once. Each refusal
        is a distinct notification the operator was expecting and did not get, and the point
        of the warning is that they can count them.
        """
        log.warning("notify_refused_not_admin type=%s chat_id=%s", ntype, chat_id)
        self._bus.emit(
            events.SYSTEM_WARNING,
            reason="notify_not_admin",
            notification_type=ntype,
            message=NOT_ADMIN_CHAT_MESSAGE,
        )

    def _warn_unconfigured(self, ntype: str) -> None:
        """Warn once per configuration that a notification had nowhere to go."""
        if self._warned_unconfigured:
            return
        self._warned_unconfigured = True
        log.warning("notify_unconfigured type=%s", ntype)
        self._bus.emit(
            events.SYSTEM_WARNING,
            reason="notify_unconfigured",
            notification_type=ntype,
            message=NOT_CONFIGURED_MESSAGE,
        )

    async def _record(self, ntype: str, text: str, *, ok: bool, error: str | None) -> None:
        """Persist the Notification row — sent or not, the operator can see what was meant."""
        try:
            async with session_scope() as session:
                session.add(
                    Notification(
                        type=ntype[:16],
                        text=text,
                        ok=bool(ok),
                        error=None if not error else redact(str(error))[:2000],
                        sent_at=_utcnow() if ok else None,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - the audit row must not break delivery
            log.warning("notification_persist_failed type=%s error=%s", ntype, exc)

    # ---- HTML composition --------------------------------------------------

    def _compose_parts(self, body: str, link: str | None, locale: str) -> tuple[list[str], list[str]]:
        """Build HTML (and plain-fallback) chunks that each fit Telegram's 4096 limit."""
        anchor = _link_html(link, locale)
        prepared = _prepare_html(body)
        reserve = len(anchor) + 1 if anchor else 0
        budget = max(32, MAX_MESSAGE_CHARS - reserve)
        html_parts = split_telegram_html(prepared, budget)
        if not html_parts:
            html_parts = [""]
        if anchor:
            extra = f"\n{anchor}"
            last = html_parts[-1]
            if len(last) + len(extra) <= MAX_MESSAGE_CHARS:
                html_parts[-1] = f"{last}{extra}"
            else:
                html_parts.append(anchor)
        plain_parts = [
            self._fit_plain(html_to_plain(part), MAX_MESSAGE_CHARS) for part in html_parts
        ]
        return html_parts, plain_parts

    def _compose(self, body: str, link: str | None, locale: str) -> str:
        """Build the HTML message: markdown or our markup, plus the source-post link."""
        parts, _plain = self._compose_parts(body, link, locale)
        return parts[0] if parts else ""

    def _compose_plain(self, body: str, link: str | None, locale: str) -> str:
        """Tag-free fallback used when Telegram refuses to parse the entities we sent."""
        _html_parts, plain_parts = self._compose_parts(body, link, locale)
        return plain_parts[0] if plain_parts else ""

    @staticmethod
    def _fit_plain(text: str, budget: int) -> str:
        """Escape plain text and shrink it until the escaped form fits Telegram's limit."""
        limit = min(MAX_TEXT_CHARS, max(32, budget))
        candidate = html.escape(_truncate(text, limit), quote=False)
        while len(candidate) > budget and limit > 32:
            shrunk = int(limit * budget / max(len(candidate), 1)) - 8
            limit = max(32, min(limit - 1, shrunk))
            candidate = html.escape(_truncate(text, limit), quote=False)
        return candidate[:budget]

    # ---- HTTP --------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        """Return the shared client, creating it when a setup call arrives before start()."""
        client = self._client
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
                follow_redirects=False,
                headers={"User-Agent": f"lv-copytrader/{__version__}"},
            )
            self._client = client
        return client

    async def _call(
        self,
        token: str,
        method: str,
        *,
        params: Mapping[str, Any] | None = None,
        http_method: str = "POST",
    ) -> _ApiResult:
        """Make one Bot API call and normalize its outcome; the token never reaches a log."""
        url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
        client = self._http()
        try:
            if http_method.upper() == "GET":
                response = await client.get(url, params=dict(params or {}))
            else:
                response = await client.post(url, json=dict(params or {}))
        except httpx.TimeoutException as exc:
            return _ApiResult(
                ok=False,
                error=f"{UNREACHABLE_MESSAGE}: the request timed out ({exc.__class__.__name__})",
                retryable=True,
            )
        except httpx.HTTPError as exc:
            detail = redact(str(exc)) or exc.__class__.__name__
            return _ApiResult(ok=False, error=f"{UNREACHABLE_MESSAGE}: {detail}", retryable=True)

        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, Mapping):
            snippet = redact(response.text or "")[:200]
            return _ApiResult(
                ok=False,
                error=f"Telegram returned a non-JSON response (HTTP {status}): {snippet}",
                status=status,
                retryable=status in RETRYABLE_STATUSES or status >= 500,
            )
        if body.get("ok") is True:
            return _ApiResult(ok=True, result=body.get("result"), status=status)

        description = str(body.get("description") or "").strip()
        error_code = body.get("error_code")
        code = error_code if isinstance(error_code, int) else status
        parameters = body.get("parameters")
        retry_after = 0.0
        if isinstance(parameters, Mapping):
            try:
                retry_after = float(parameters.get("retry_after") or 0.0)
            except (TypeError, ValueError):
                retry_after = 0.0
        lowered = description.lower()
        return _ApiResult(
            ok=False,
            error=description or f"Telegram returned HTTP {code}",
            status=code,
            retryable=code in RETRYABLE_STATUSES or code >= 500,
            entity_error=code == 400
            and ("parse entities" in lowered or "unsupported start tag" in lowered),
            retry_after=min(max(retry_after, 0.0), MAX_RETRY_AFTER_SECONDS),
        )

    async def _send_with_retries(
        self,
        token: str,
        payload: dict[str, Any],
        plain_fallback: str,
        ntype: str,
        *,
        rich: bool = False,
    ) -> tuple[bool, str | None]:
        """Send, retrying at NOTIFY_RETRY_DELAYS while retrying can plausibly help."""
        delays = tuple(float(delay) for delay in self._settings.NOTIFY_RETRY_DELAYS)
        attempt = 0
        used_fallback = False
        last_error = ""
        method = "sendMessage"
        fallback_payload = dict(payload)
        if rich:
            method = "sendRichMessage"
            payload = {key: value for key, value in payload.items()
                       if key not in {"text", "parse_mode", "disable_web_page_preview"}}
            payload["rich_message"] = {
                "html": telegram_html_to_rich_html(str(fallback_payload["text"]))
            }
        while True:
            result = await self._call(token, method, params=payload)
            if result.ok:
                return True, None
            last_error = result.error
            if method == "sendRichMessage" and (
                result.entity_error or result.status == 404
                or (result.status == 400 and any(word in result.error.lower()
                    for word in ("rich", "unsupported", "method not found", "not available")))
            ):
                method = "sendMessage"
                payload = fallback_payload
                continue
            if result.entity_error and not used_fallback:
                # Text the model wrote can contain a stray "<". Resend it tag-free rather
                # than losing the notification to a parse error.
                used_fallback = True
                payload = dict(payload)
                payload["text"] = plain_fallback
                log.warning("notify_entity_fallback type=%s", ntype)
                continue
            if attempt >= len(delays) or not result.retryable:
                break
            delay = max(delays[attempt], result.retry_after)
            attempt += 1
            log.warning(
                "notify_retry type=%s attempt=%s delay=%.1f error=%s",
                ntype,
                attempt,
                delay,
                result.error,
            )
            await asyncio.sleep(delay)
        log.warning("notify_giving_up type=%s error=%s", ntype, last_error)
        return False, last_error or "the notification could not be delivered"

    @staticmethod
    def _describe(result: _ApiResult) -> str:
        """Turn a failed setup call into the sentence the settings page shows."""
        if result.status in (401, 404):
            return TOKEN_REJECTED_MESSAGE
        if result.status == 409:
            return WEBHOOK_CONFLICT_MESSAGE
        if result.status == 0:
            return result.error or UNREACHABLE_MESSAGE
        return result.error or TOKEN_REJECTED_MESSAGE


# --------------------------------------------------------------------------------------
# Field maps used by the builders: (locale key, is-a-price).
# --------------------------------------------------------------------------------------
_CHANGE_FIELDS: Final[dict[str, tuple[str, bool]]] = {
    "sl": ("label.sl", True),
    "stop_loss": ("label.sl", True),
    "tp": ("label.tp", True),
    "take_profit": ("label.tp", True),
    "price": ("label.price", True),
    "entry": ("label.entry", True),
    "volume": ("label.volume", False),
    "expiry": ("label.expiry", False),
}

_CAUSE_KEYS: Final[dict[str, str]] = {
    "tp": "cause.tp",
    "take_profit": "cause.tp",
    "sl": "cause.sl",
    "stop_loss": "cause.sl",
    "ai": "cause.ai",
    "agent": "cause.ai",
    "manual": "cause.manual",
    "kill_switch": "cause.kill_switch",
    "killswitch": "cause.kill_switch",
    "channel_delete": "cause.channel_delete",
    "full_close": "cause.full_close",
    "unknown": "cause.unknown",
}

_SUBJECT_KEYS: Final[dict[str, str]] = {
    "stopless": "subject.stopless",
    "stopless_position": "subject.stopless",
    "stopless_episode": "subject.stopless",
    "sl_fallback": "subject.sl_fallback",
    "fallback_sl": "subject.sl_fallback",
    "mt5_disconnected": "subject.mt5_disconnected",
    "mt5_down": "subject.mt5_disconnected",
    "provider_failing": "subject.provider_failing",
    "provider_error": "subject.provider_failing",
    "queue_overflow": "subject.queue_overflow",
    "ingest_overflow": "subject.queue_overflow",
}


def _cause_text(cause: Any, locale: str) -> str:
    """Localize a close cause; an unknown one prints as itself rather than vanishing."""
    key = str(cause or "").strip().lower()
    if not key:
        return render("cause.unknown", locale)
    mapped = _CAUSE_KEYS.get(key)
    if mapped:
        return render(mapped, locale)
    return key.replace("_", " ")


def _subject_text(subject: Any, locale: str) -> str:
    """Localize an attention subject; an unknown one prints as itself."""
    key = str(subject or "").strip().lower()
    if not key:
        return ""
    mapped = _SUBJECT_KEYS.get(key)
    if mapped:
        return render(mapped, locale)
    return key.replace("_", " ")


def _derive_points(kw: Mapping[str, Any]) -> Any:
    """Use the supplied point result, or compute it from entry, close price and point size."""
    given = _first(kw, "profit_points", "points")
    if given is not None:
        return given
    entry = _first(kw, "entry", "entry_price")
    close = _first(kw, "close_price", "price", "fill_price")
    point = _first(kw, "point")
    side = str(_first(kw, "side", "direction") or "").strip().lower()
    if entry is None or close is None or point is None:
        return None
    try:
        entry_value = float(entry)
        close_value = float(close)
        point_value = float(point)
    except (TypeError, ValueError):
        return None
    if point_value <= 0.0:
        return None
    delta = close_value - entry_value
    if "sell" in side or side == "short":
        delta = -delta
    return delta / point_value
