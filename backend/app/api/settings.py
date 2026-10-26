"""Settings REST API — the masked settings tree and every section that writes to it.

Routes (CONTRACTS.md §18, BUILD_PROMPT §4.4, §6, §7)::

    GET  /api/settings                  -> the whole masked tree
    PUT  /api/settings/mt5              POST /api/settings/mt5/test
    PUT  /api/settings/provider
    PUT  /api/settings/notifier         POST /api/settings/notifier/test
    POST /api/settings/notifier/detect  POST /api/settings/notifier/whoami
    PUT  /api/settings/limits
    PUT  /api/settings/general

Four rules shape every handler below.

**A secret is never returned.** Every secret field comes back as :data:`app.crypto.MASK`
with a sibling ``has_<field>`` boolean. No response is ever *built* from a stored secret:
the booleans come from :func:`app.db.has_value`, which reports whether the row carries a
value without decrypting it. The one place a plaintext secret is read is
:func:`_probe_secrets`, which decrypts each of them to find out whether they still can be
decrypted, registers them with the log redactor, and returns nothing. Acceptance criterion
15 is exactly this.

**A PUT that omits a secret leaves it alone.** ``app.schemas`` already normalizes an
omitted field, an explicit ``null`` and an echoed ``MASK`` to ``None`` (its
``_unmask_secret`` validator), so this module relies on that: ``None`` means *keep the
stored value*, an empty string means *clear it*, and anything else is a new secret. That
normalization is why the settings page can round-trip the tree it was given without
re-typing a key, and why a form that echoes the mask cannot silently overwrite the real
one with eight bullets.

**Clearing means clearing.** A cleared secret is stored as an empty, *non-secret* row, so
``has_value`` reports ``False`` and the row stops being flagged ``needs_reentry``. Writing
an encrypted empty string instead would leave ``has_password: true`` beside nothing.

**Every write emits ``settings.changed`` carrying key names, never values.** The event data
is ``{"section": ..., "keys": [...]}``: the frontend refetches the named section
(``SettingsChangedData``), and the key list is what the operator sees in a log. Note that
:func:`app.db.set_setting` also emits one event per key on its own; the summary event here
is the one the UI acts on.

The four ``POST`` probes — ``mt5/test``, ``notifier/test``, ``notifier/detect`` and
``notifier/whoami`` — answer **HTTP 200 with ``ok: false``** when the thing they probe is
unreachable or refuses, because their response models carry ``ok`` and ``message`` for
exactly that purpose. A failed probe is a *result* the settings page renders next to the
button, not an API error.
Genuine errors (an unreadable ``SECRET_KEY``, an unconfigured runtime) still go through
:func:`app.errors.to_http` and arrive as ``{"error":{"code","message","detail"}}``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app.ai.catalog import summarize as summarize_provider
from app.crypto import MASK
from app.db import (
    get_session,
    get_setting,
    has_value,
    needs_reentry_keys,
    session_scope,
    set_setting,
)
from app.errors import ConfigError, RpcError, SignalPilotError, to_http
from app.ai.prompts import SYSTEM_PROMPT
from app.models import Source
from app.operator_settings import (
    KEY_BE_AFTER_TP1,
    KEY_ORDER_LOT_SIZE,
    KEY_ORDERS_PER_SIGNAL,
    KEY_REVIEWER_ENABLED,
    KEY_MAX_ENTRY_DRIFT_USD,
    KEY_USE_FIXED_ORDER_LOTS,
    KEY_SYSTEM_PROMPT,
    KEY_TAG_FILTER_ENABLED,
    KEY_TAG_FILTER_TAG,
    ORDER_LOT_SIZE_DEFAULT,
    ORDERS_PER_SIGNAL_DEFAULT,
    TAG_FILTER_TAG_DEFAULT,
    be_after_tp1_enabled,
    max_entry_drift_usd,
    reviewer_enabled,
    tag_filter_enabled,
    use_fixed_order_lots,
)
from app.runtime import Runtime, get_runtime
from app.schemas import (
    GeneralSettings,
    GeneralSettingsOut,
    LadderStatsOut,
    LimitsOut,
    LimitsSettings,
    MT5Settings,
    Mt5SettingsOut,
    Mt5TestOut,
    NotifierBoundToOut,
    NotifierDetectOut,
    NotifierSettings,
    NotifierSettingsOut,
    NotifierTestOut,
    NotifierUpdateOut,
    NotifierWhoamiOut,
    NotifyTypes,
    ProviderSettings,
    ProviderSettingsOut,
    SettingsOut,
    SourceOut,
    TelegramSettingsOut,
    TelegramUserOut,
    TradingWindow,
)
from app.trading.constants import margin_mode_name
from app.trading import ladder_stats
from app.trading.rpc import RPC_CODE_AUTH, RPC_CODE_TRANSPORT
from app.util.logging import get_logger, register_secret

__all__ = ["router"]

log = get_logger("app.api.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"])


# --------------------------------------------------------------------------------------
# Settings keys, grouped by the section that owns them (CONTRACTS §6).
#
# These tuples are what the `settings.changed` event reports and the only place a key
# string is spelled out, so a rename is a one-line change here rather than a hunt.
# --------------------------------------------------------------------------------------

SECTION_MT5: Final[str] = "mt5"
SECTION_PROVIDER: Final[str] = "provider"
SECTION_NOTIFIER: Final[str] = "notifier"
SECTION_LIMITS: Final[str] = "limits"
SECTION_GENERAL: Final[str] = "general"

KEY_MT5_SERVER: Final[str] = "mt5.server"
KEY_MT5_LOGIN: Final[str] = "mt5.login"
KEY_MT5_PASSWORD: Final[str] = "mt5.password"

KEY_TG_API_ID: Final[str] = "tg.api_id"
KEY_TG_API_HASH: Final[str] = "tg.api_hash"
KEY_TG_PHONE: Final[str] = "tg.phone"
KEY_TG_SESSION: Final[str] = "tg.session"
KEY_TG_USER: Final[str] = "tg.user"

KEY_PROVIDER_KIND: Final[str] = "provider.kind"
KEY_PROVIDER_BASE_URL: Final[str] = "provider.base_url"
KEY_PROVIDER_API_KEY: Final[str] = "provider.api_key"
KEY_PROVIDER_MODEL: Final[str] = "provider.model"
KEY_PROVIDER_EXTRA_HEADERS: Final[str] = "provider.extra_headers"
KEY_PROVIDER_MAX_TOKENS: Final[str] = "provider.max_tokens"
KEY_PROVIDER_VISION_OVERRIDE: Final[str] = "provider.supports_vision_override"
KEY_PROVIDER_TOOLS_OVERRIDE: Final[str] = "provider.supports_tools_override"

KEY_NOTIFIER_TOKEN: Final[str] = "notifier.bot_token"
KEY_NOTIFIER_CHAT_ID: Final[str] = "notifier.chat_id"
KEY_NOTIFIER_ENABLED: Final[str] = "notifier.enabled"
KEY_NOTIFIER_TYPES: Final[str] = "notifier.types"
KEY_NOTIFIER_ADMIN_IDS: Final[str] = "notifier.admin_ids"
KEY_NOTIFIER_BOUND_TO: Final[str] = "notifier.bound_to"

KEY_LIMIT_MAX_VOLUME: Final[str] = "limits.max_volume_per_trade"
KEY_LIMIT_MAX_POSITIONS: Final[str] = "limits.max_open_positions"
KEY_LIMIT_MAX_DAILY_LOSS: Final[str] = "limits.max_daily_loss_percent"
KEY_LIMIT_ALLOWLIST: Final[str] = "limits.symbol_allowlist"
KEY_LIMIT_TRADING_HOURS: Final[str] = "limits.trading_hours"

KEY_DEFAULT_RISK: Final[str] = "trading.default_risk_percent"
KEY_LADDER_VOLUME: Final[str] = "trading.ladder_volume"
KEY_LOCALE: Final[str] = "ui.locale"

#: Every Fernet-encrypted settings key (the ``*`` rows of CONTRACTS §6). ``GET /api/settings``
#: touches each of them so an unreadable one is discovered here rather than at the moment a
#: trade needs it — see :func:`_probe_secrets`.
ENCRYPTED_SETTING_KEYS: Final[tuple[str, ...]] = (
    KEY_MT5_PASSWORD,
    KEY_TG_API_HASH,
    KEY_TG_SESSION,
    KEY_PROVIDER_API_KEY,
    KEY_NOTIFIER_TOKEN,
)

#: Sidecar-side ``error.code`` values, from the table at the top of ``mt5-sidecar/rpc_server.py``.
#: ``rpc.py`` owns the client-side codes (-1 transport, -2 auth, -3 null result); these three
#: can only come back inside an ``ok: false`` envelope and are what tells "the sidecar is up but
#: MT5 is not" apart from "nothing answered at all".
SIDECAR_CODE_UNKNOWN_METHOD: Final[int] = -4
SIDECAR_CODE_BAD_PARAMS: Final[int] = -5
SIDECAR_CODE_NOT_INITIALIZED: Final[int] = -6
SIDECAR_CODE_CALL_RAISED: Final[int] = -7

#: Fragments of ``mt5.last_error()`` that mean the broker refused these credentials. Matching
#: on them is what turns "initialize() failed" into "MT5 rejected the login", which is the
#: difference between an operator fixing their password and an operator restarting containers.
_LOGIN_REJECTED_MARKERS: Final[tuple[str, ...]] = (
    "invalid account",
    "invalid login",
    "invalid password",
    "wrong password",
    "authorization failed",
    "authorisation failed",
    "authorization denied",
    "account disabled",
    "account is disabled",
    "account blocked",
)

#: Fragments that mean the terminal never reached the broker at all — a wrong server name, a
#: server missing from this terminal build's list, or no route out of the container.
_SERVER_UNREACHABLE_MARKERS: Final[tuple[str, ...]] = (
    "no connection",
    "not connected",
    "server not found",
    "invalid server",
    "unknown server",
    "trade server",
    "network",
)

#: Fragments that mean ``terminal64.exe`` is not answering the IPC channel yet.
_IPC_MARKERS: Final[tuple[str, ...]] = ("ipc timeout", "ipc initialize", "ipc ")


# --------------------------------------------------------------------------------------
# Small coercions.
#
# Everything here is defensive on purpose: the settings rows are JSON written by earlier
# builds, by `.env` seeding, and (rarely) by hand, and a settings page that 500s because one
# row holds the wrong type is a settings page nobody can use to fix that row.
# --------------------------------------------------------------------------------------


def _error(exc: SignalPilotError) -> JSONResponse:
    """Render one LV Copytrader error as the ``{"error": {...}}`` envelope every route uses."""
    status, body = to_http(exc)
    return JSONResponse(status_code=status, content=body)


def _as_int(value: Any) -> int | None:
    """Coerce a stored value to an int, returning None for every spelling of 'not set'."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    """Coerce a stored value to a finite float, returning None for every spelling of 'not set'."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _one_line(value: Any) -> str:
    """Collapse a message or exception into one bounded log line.

    A pydantic ``ValidationError`` renders as half a screen of indented text; the log format
    is one structured line per event, so it is folded here rather than allowed to break it.
    """
    return " ".join(str(value).split())[:300]


def _as_text(value: Any) -> str:
    """Coerce a stored value to a plain string, mapping None onto the empty string."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _limit_number(value: Any, key: str) -> float | None:
    """Read a numeric operator limit exactly as ``guards._limit_number`` does.

    A null, an empty string, an unparsable value **and a non-positive one** all mean the
    limit is not evaluated. Reporting a stored ``0`` back as ``0`` would show the operator a
    limit that blocks every order while the guard quietly ignores it; the tree must say what
    is actually in force (BUILD §6).
    """
    number = _as_float(value)
    if number is None:
        if value not in (None, ""):
            log.warning("limit_ignored key=%s reason=not_a_number value=%r", key, value)
        return None
    if number <= 0.0:
        log.warning("limit_ignored key=%s reason=not_positive value=%r", key, value)
        return None
    return number


def _as_str_list(value: Any) -> list[str]:
    """Coerce a stored list of names into a list of non-empty strings."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _as_text(item).strip()
        if text:
            out.append(text)
    return out


def _as_admin_ids(value: Any) -> list[int]:
    """Read the stored admin allowlist exactly as ``notifier._coerce_admin_ids`` does.

    The two readers have to agree, or the settings page would show a list that is not the
    one being enforced — the single worst failure mode an allowlist has. A malformed entry
    is dropped here as it is there, and every drop is logged.
    """
    if not isinstance(value, (list, tuple)):
        if value not in (None, ""):
            log.warning("admin_ids_ignored reason=not_a_list value=%r", value)
        return []
    ids: list[int] = []
    seen: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            log.warning("admin_ids_ignored reason=not_a_number value=%r", item)
            continue
        number = _as_int(item)
        if number is None:
            log.warning("admin_ids_ignored reason=not_a_number value=%r", item)
            continue
        if number <= 0:
            log.warning("admin_ids_ignored reason=not_positive value=%r", item)
            continue
        if number in seen:
            continue
        seen.add(number)
        ids.append(number)
    return ids


def _as_bound_to(value: Any) -> NotifierBoundToOut | None:
    """Parse the stored `notifier.bound_to` row, or None when nothing was recorded."""
    if not isinstance(value, Mapping):
        return None
    user_id = _as_int(value.get("user_id"))
    if user_id is None:
        # A row with names but no id describes nobody the allowlist could ever hold, and
        # the settings tree promises a numeric id whenever this object is present at all.
        return None
    return NotifierBoundToOut(
        user_id=user_id,
        username=_as_text(value.get("username")).strip(),
        first_name=_as_text(value.get("first_name")).strip(),
    )


def _as_windows(value: Any) -> list[TradingWindow]:
    """Parse the stored trading windows, dropping any row that no longer validates.

    A malformed window costs that window, not the settings page: the operator has to be able
    to load the page to correct it. Every drop is logged with its index.
    """
    if not isinstance(value, (list, tuple)):
        return []
    windows: list[TradingWindow] = []
    for index, item in enumerate(value):
        try:
            windows.append(TradingWindow.model_validate(item))
        except Exception as exc:  # noqa: BLE001 - one bad row must not break the whole page
            log.warning("trading_window_ignored index=%d error=%s", index, _one_line(exc))
    return windows


def _as_notify_types(value: Any) -> NotifyTypes:
    """Parse the per-type notification switches, falling back to all-on defaults."""
    if not isinstance(value, Mapping):
        return NotifyTypes()
    known = set(NotifyTypes.model_fields)
    filtered = {str(key): bool(item) for key, item in value.items() if str(key) in known}
    return NotifyTypes(**filtered)


def _emit_changed(runtime: Runtime, section: str, keys: Sequence[str]) -> None:
    """Announce a settings write as key names only — a value never reaches the bus."""
    runtime.bus.emit(events.SETTINGS_CHANGED, section=section, keys=list(keys))
    log.info("settings_written section=%s keys=%s", section, ",".join(keys) or "none")


async def _write_secret(session: AsyncSession, key: str, value: str | None) -> bool:
    """Apply the secret-field rule to one key and report whether anything was written.

    ``None`` (an omitted field, an explicit null, or the echoed :data:`MASK`, all normalized
    by ``app.schemas``) leaves the stored value untouched. An empty string clears it, stored
    as a non-secret empty row so ``has_value`` reports ``False``. Anything else is a new
    secret: registered with the log redactor before it is written, so it can never surface in
    a log line even if a later edit logs the request body.
    """
    if value is None:
        return False
    text = value.strip() if isinstance(value, str) else _as_text(value).strip()
    if not text:
        await set_setting(session, key, "", secret=False)
        log.info("secret_cleared key=%s", key)
        return True
    register_secret(text)
    await set_setting(session, key, text, secret=True)
    return True


# --------------------------------------------------------------------------------------
# Section readers — each builds one branch of the masked tree.
# --------------------------------------------------------------------------------------


async def _mt5_section(session: AsyncSession) -> Mt5SettingsOut:
    """Build the MT5 section: the password is the mask when one is stored, else null."""
    stored_password = await has_value(session, KEY_MT5_PASSWORD)
    return Mt5SettingsOut(
        server=_as_text(await get_setting(session, KEY_MT5_SERVER, "")),
        login=_as_int(await get_setting(session, KEY_MT5_LOGIN, None)),
        password=MASK if stored_password else None,
        has_password=stored_password,
    )


async def _active_source(session: AsyncSession) -> SourceOut | None:
    """Return the one active signal source, or None before the operator picked a channel."""
    result = await session.execute(
        select(Source).where(Source.is_active.is_(True)).order_by(Source.id.desc()).limit(1)
    )
    row = result.scalars().first()
    return None if row is None else SourceOut.model_validate(row)


async def _telegram_section(session: AsyncSession, runtime: Runtime) -> TelegramSettingsOut:
    """Build the Telegram section: api_hash masked, plus the live account and active source.

    ``authorized`` and ``user`` come from the running :class:`TelegramService` when there is
    one; both of its accessors are documented never to raise, and the service itself is
    absent in unit tests, so the whole probe is guarded and degrades to the stored account.
    """
    stored_hash = await has_value(session, KEY_TG_API_HASH)
    api_id = _as_int(await get_setting(session, KEY_TG_API_ID, None))
    phone = _as_text(await get_setting(session, KEY_TG_PHONE, "")) or None
    stored_user = await get_setting(session, KEY_TG_USER, None)

    authorized = False
    user_payload: Any = stored_user
    service = getattr(runtime, "telegram", None)
    if service is not None:
        try:
            authorized = bool(await service.is_authorized())
            live = await service.me()
            if isinstance(live, Mapping):
                user_payload = dict(live)
        except Exception as exc:  # noqa: BLE001 - the settings page must load regardless
            log.warning("telegram_status_probe_failed error=%s", _one_line(exc))

    user: TelegramUserOut | None = None
    if isinstance(user_payload, Mapping) and user_payload.get("id") is not None:
        try:
            user = TelegramUserOut.model_validate(dict(user_payload))
        except Exception as exc:  # noqa: BLE001 - a stale cached account is not fatal
            log.warning("telegram_user_ignored error=%s", _one_line(exc))

    return TelegramSettingsOut(
        api_id=api_id,
        api_hash=MASK if stored_hash else None,
        has_api_hash=stored_hash,
        phone=phone,
        authorized=authorized,
        has_session=await has_value(session, "tg.session"),
        user=user,
        source=await _active_source(session),
    )


async def _provider_section(session: AsyncSession) -> ProviderSettingsOut:
    """Build the provider section from :func:`app.ai.catalog.summarize`.

    ``summarize`` is offline by design: it reports the last stored capability verdict and
    never starts a probe, because loading the settings page must not block on a provider
    round trip. It also returns the mask and ``has_api_key`` rather than the key.
    """
    return ProviderSettingsOut(**await summarize_provider(session))


async def _notifier_section(session: AsyncSession) -> NotifierSettingsOut:
    """Build the notifier section: the bot token is the mask when one is stored.

    ``admin_ids`` and ``bound_to`` are both here so the page can answer "who is this bot
    talking to" without a second request: the first is who is *allowed* to receive, the
    second is who the stored chat id was actually detected from. When the allowlist is empty
    the second one is the only check there is.
    """
    stored_token = await has_value(session, KEY_NOTIFIER_TOKEN)
    raw_chat_id = await get_setting(session, KEY_NOTIFIER_CHAT_ID, None)
    chat_id = _as_int(raw_chat_id)
    if chat_id is None and raw_chat_id not in (None, ""):
        # An @channelusername is legal for the Bot API but not for this field, whose type is
        # numeric on both sides of the wire; say so rather than silently rendering null.
        log.warning("notifier_chat_id_not_numeric value=%r", raw_chat_id)
    return NotifierSettingsOut(
        bot_token=MASK if stored_token else None,
        has_bot_token=stored_token,
        chat_id=chat_id,
        enabled=bool(await get_setting(session, KEY_NOTIFIER_ENABLED, False)),
        types=_as_notify_types(await get_setting(session, KEY_NOTIFIER_TYPES, None)),
        admin_ids=_as_admin_ids(await get_setting(session, KEY_NOTIFIER_ADMIN_IDS, [])),
        bound_to=_as_bound_to(await get_setting(session, KEY_NOTIFIER_BOUND_TO, None)),
    )


async def _limits_section(session: AsyncSession) -> LimitsOut:
    """Build the optional-limits section; null and empty both mean 'not evaluated' (BUILD §6)."""
    max_positions = _limit_number(
        await get_setting(session, KEY_LIMIT_MAX_POSITIONS, None), KEY_LIMIT_MAX_POSITIONS
    )
    return LimitsOut(
        max_volume_per_trade=_limit_number(
            await get_setting(session, KEY_LIMIT_MAX_VOLUME, None), KEY_LIMIT_MAX_VOLUME
        ),
        max_open_positions=None if max_positions is None else int(max_positions),
        max_daily_loss_percent=_limit_number(
            await get_setting(session, KEY_LIMIT_MAX_DAILY_LOSS, None), KEY_LIMIT_MAX_DAILY_LOSS
        ),
        symbol_allowlist=_as_str_list(await get_setting(session, KEY_LIMIT_ALLOWLIST, [])),
        trading_hours=_as_windows(await get_setting(session, KEY_LIMIT_TRADING_HOURS, [])),
    )


def _stored_prompt_is_custom(stored: str) -> bool:
    """A blank row, or a save of the built-in text, is not a custom prompt."""
    text = stored.strip()
    if not text:
        return False
    return text != SYSTEM_PROMPT.strip()


async def _general_section(session: AsyncSession, runtime: Runtime) -> GeneralSettingsOut:
    """Build the general section: risk, lots, filters, the prompt, and channel stats."""
    risk = _as_float(await get_setting(session, KEY_DEFAULT_RISK, None))
    if risk is None or risk <= 0.0:
        risk = float(runtime.settings.DEFAULT_RISK_PERCENT)
    ladder = _as_float(await get_setting(session, KEY_LADDER_VOLUME, None))
    if ladder is None or ladder <= 0.0:
        ladder = float(runtime.settings.LADDER_VOLUME)
    locale = _as_text(await get_setting(session, KEY_LOCALE, "en")).strip().lower() or "en"
    orders = await get_setting(session, KEY_ORDERS_PER_SIGNAL, ORDERS_PER_SIGNAL_DEFAULT)
    try:
        orders_n = int(orders)
    except (TypeError, ValueError):
        orders_n = ORDERS_PER_SIGNAL_DEFAULT
    lot = _as_float(await get_setting(session, KEY_ORDER_LOT_SIZE, ORDER_LOT_SIZE_DEFAULT))
    if lot is None or lot <= 0.0:
        lot = ORDER_LOT_SIZE_DEFAULT
    stored_prompt = await get_setting(session, KEY_SYSTEM_PROMPT, "")
    if not isinstance(stored_prompt, str):
        stored_prompt = ""
    custom = _stored_prompt_is_custom(stored_prompt)
    tag = _as_text(await get_setting(session, KEY_TAG_FILTER_TAG, TAG_FILTER_TAG_DEFAULT)).strip()
    stats_out: LadderStatsOut | None = None
    try:
        stats = await ladder_stats.stats_for_source(session, None)
        stats_out = LadderStatsOut.model_validate(stats.to_dict())
    except Exception as exc:  # noqa: BLE001 - settings must still render
        log.warning("ladder_stats_settings_failed error=%s", exc)
    return GeneralSettingsOut(
        default_risk_percent=risk,
        ladder_volume=ladder,
        locale=locale,
        orders_per_signal=orders_n,
        order_lot_size=lot,
        use_fixed_order_lots=await use_fixed_order_lots(session),
        max_entry_drift_usd=await max_entry_drift_usd(session),
        be_after_tp1=await be_after_tp1_enabled(session),
        reviewer_enabled=await reviewer_enabled(session),
        tag_filter_enabled=await tag_filter_enabled(session),
        tag_filter_tag=tag,
        system_prompt=stored_prompt.strip() if custom else SYSTEM_PROMPT,
        system_prompt_default=SYSTEM_PROMPT,
        system_prompt_custom=custom,
        ladder_stats=stats_out,
    )


async def _probe_secrets(session: AsyncSession) -> None:
    """Decrypt every stored secret once, so an unreadable one is discovered here.

    ``needs_reentry`` is set lazily by :func:`app.db.get_setting`: a row is only flagged when
    something *reads* it and the decrypt fails. Nothing else in this module reads a secret —
    the masked tree is built from :func:`app.db.has_value` — so without this probe a key that
    no longer decrypts would stay invisible until an agent run needed it, which is precisely
    the moment the operator cannot be reading a banner. Touching all five here means the
    settings page names the broken ones the first time it is opened (BRIEF).

    The plaintext never leaves this function. It is registered with the log redactor on the
    way past, which is the one useful side effect of having decrypted it: from here on that
    literal is masked in every log line, whoever writes it.
    """
    for key in ENCRYPTED_SETTING_KEYS:
        try:
            if not await has_value(session, key):
                continue
            value = await get_setting(session, key, None)
        except Exception as exc:  # noqa: BLE001 - a probe must never break the settings page
            log.warning("secret_probe_failed key=%s error=%s", key, _one_line(exc))
            continue
        if isinstance(value, str) and value:
            register_secret(value)


async def _settings_tree(session: AsyncSession, runtime: Runtime) -> SettingsOut:
    """Assemble the whole masked tree, including the keys that failed to decrypt.

    ``needs_reentry`` is what lets the UI *name* the settings whose stored value no longer
    decrypts with the current ``SECRET_KEY``, instead of showing a banner that says something
    somewhere is broken (BRIEF). The same list is mirrored onto the runtime status so
    ``GET /api/health`` and the WebSocket hello frame agree with this response.

    A key that cannot be decrypted still reports ``has_<field>: true``: something *is* stored,
    it just cannot be read, and "stored but unreadable, re-enter it" is a different message
    from "nothing here yet".
    """
    await _probe_secrets(session)
    reentry = await needs_reentry_keys(session)
    runtime.status["needs_reentry"] = list(reentry)
    return SettingsOut(
        mt5=await _mt5_section(session),
        telegram=await _telegram_section(session, runtime),
        provider=await _provider_section(session),
        notifier=await _notifier_section(session),
        limits=await _limits_section(session),
        general=await _general_section(session, runtime),
        needs_reentry=reentry,
    )


# --------------------------------------------------------------------------------------
# GET /api/settings
# --------------------------------------------------------------------------------------


@router.get(
    "",
    response_model=SettingsOut,
    summary="The whole settings tree, every secret replaced by the mask",
)
async def get_settings(
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> Any:
    """Return every setting the dashboard renders, with no secret in the body.

    Each secret field is :data:`MASK` when something is stored and ``null`` when nothing is,
    beside a ``has_<field>`` boolean. The one thing this route does with the real values is
    check that they still decrypt, so ``needs_reentry`` can name the ones that do not.
    """
    try:
        return await _settings_tree(session, runtime)
    except SignalPilotError as exc:
        return _error(exc)


# --------------------------------------------------------------------------------------
# MT5 — PUT /api/settings/mt5 and POST /api/settings/mt5/test
# --------------------------------------------------------------------------------------


async def _write_mt5(session: AsyncSession, body: MT5Settings) -> list[str]:
    """Persist the MT5 credentials and return the keys that were actually written."""
    written = [KEY_MT5_SERVER, KEY_MT5_LOGIN]
    await set_setting(session, KEY_MT5_SERVER, body.server)
    await set_setting(session, KEY_MT5_LOGIN, int(body.login))
    if await _write_secret(session, KEY_MT5_PASSWORD, body.password):
        written.append(KEY_MT5_PASSWORD)
    return written


async def _apply_mt5(session: AsyncSession, runtime: Runtime) -> str | None:
    """Tell the sidecar to re-attach to the stored account. Returns an error to report, or None.

    Storing credentials is not applying them: the sidecar reads MT5_* from its environment once,
    at boot. Without this call, saving a new account in Settings would report success and leave
    the terminal logged into the old one -- which is worse than an error, because nothing looks
    wrong until the trades land in the wrong account.
    """
    server = await get_setting(session, KEY_MT5_SERVER)
    login = await get_setting(session, KEY_MT5_LOGIN)
    password = await get_setting(session, KEY_MT5_PASSWORD)
    if not (server and login and password):
        return "the account is incomplete — server, login and password are all required"
    try:
        info = await runtime.rpc.relogin(login=int(login), password=str(password), server=str(server))
    except SignalPilotError as exc:
        runtime.set_status("mt5", False, exc.message)
        return exc.message
    except Exception as exc:  # noqa: BLE001 - a settings write must not 500 on a broker problem
        runtime.set_status("mt5", False, str(exc))
        return str(exc)
    runtime.set_status("mt5", True, None)
    # The symbol table belongs to the account that was just replaced.
    try:
        await runtime.resolver.refresh(force=True)
    except Exception:  # noqa: BLE001 - the next resolve refreshes it anyway
        log.warning("symbol_refresh_after_relogin_failed", exc_info=True)
    log.info("mt5_account_applied login=%s server=%s", login, server)
    return None


@router.put(
    "/mt5",
    response_model=SettingsOut,
    summary="Store the broker terminal credentials",
)
async def put_mt5_settings(
    body: MT5Settings,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> Any:
    """Store server, login and (optionally) password, then return the freshly masked tree.

    Omitting ``password``, sending ``null`` or echoing the mask leaves the stored password
    untouched; an empty string clears it.
    """
    try:
        written = await _write_mt5(session, body)
        _emit_changed(runtime, SECTION_MT5, written)
        # Storing is not applying: tell the sidecar to re-attach to this account now.
        # A broker-side failure is reported in the tree, not raised -- the credentials are
        # saved either way, and the operator needs the page back to correct them.
        apply_error = await _apply_mt5(session, runtime)
        tree = await _settings_tree(session, runtime)
        if apply_error:
            # _settings_tree returns the SettingsOut model, not a dict.
            tree.mt5.apply_error = apply_error
        return tree
    except SignalPilotError as exc:
        return _error(exc)


def _sidecar_url(runtime: Runtime) -> str:
    """Return the sidecar endpoint as configured, for use in an operator-facing message."""
    rpc = getattr(runtime, "rpc", None)
    url = _as_text(getattr(rpc, "url", "")).strip()
    return url or _as_text(runtime.settings.SIDECAR_URL)


def _mt5_failure_reason(exc: RpcError, runtime: Runtime, server: str, login: int | None) -> str:
    """Turn one RPC failure into the specific sentence the operator can act on.

    "Failed to connect" is useless here: the three things that actually go wrong — nothing
    listening, the wrong shared secret, and a terminal that is running but not logged in —
    have three completely different fixes, and the sidecar's error code tells them apart
    (BUILD §1, §4.3's "report the real cause" applied to the broker side).
    """
    message = _as_text(exc.message).strip()
    lowered = message.lower()
    url = _sidecar_url(runtime)

    if exc.rpc_code == RPC_CODE_AUTH:
        return (
            "the MT5 sidecar rejected our token — SIDECAR_TOKEN in .env does not match the "
            "token the mt5-sidecar container was started with; fix .env and recreate both "
            "containers"
        )
    if exc.rpc_code == RPC_CODE_TRANSPORT:
        return (
            f"the MT5 sidecar is unreachable at {url} — the container is not running, is "
            f"still installing the terminal, or SIDECAR_URL points somewhere else ({message})"
        )
    if exc.rpc_code == SIDECAR_CODE_NOT_INITIALIZED:
        if "permanently unhealthy" in lowered:
            return (
                "the mt5-sidecar gave up restarting MetaTrader 5 after too many crashes — "
                f"read the sidecar logs and recreate the container ({message})"
            )
        if any(marker in lowered for marker in _LOGIN_REJECTED_MARKERS):
            account = login if login is not None else "the configured account"
            where = server or "the configured server"
            return (
                f"MetaTrader 5 rejected the login for {account} on {where} — check "
                "MT5_LOGIN, MT5_PASSWORD and MT5_SERVER in .env, then recreate the "
                f"mt5-sidecar container ({message})"
            )
        if any(marker in lowered for marker in _IPC_MARKERS):
            return (
                "the sidecar is running but terminal64.exe is not answering the IPC channel "
                "yet — give it a moment after startup, and use "
                "`docker compose --profile debug up` to look at the terminal if it persists "
                f"({message})"
            )
        if any(marker in lowered for marker in _SERVER_UNREACHABLE_MARKERS):
            return (
                f"MetaTrader 5 could not reach the trade server {server or '(unset)'} — the "
                "server name may be wrong, or this terminal build has no entry for it; the "
                "fix is usually the broker-branded installer in MT5_INSTALLER_URL "
                f"({message})"
            )
        return f"the sidecar is running but MetaTrader 5 is not logged in yet: {message}"
    if exc.rpc_code in (SIDECAR_CODE_UNKNOWN_METHOD, SIDECAR_CODE_BAD_PARAMS):
        return (
            "the MT5 sidecar does not understand this call — the backend and the sidecar are "
            f"from different builds; rebuild both images ({message})"
        )
    if exc.rpc_code == SIDECAR_CODE_CALL_RAISED:
        return f"the MetaTrader 5 call raised inside the sidecar: {message}"
    return f"the MT5 sidecar failed the call: {message}"


def _algo_trading_reason(terminal: Any) -> str | None:
    """Return the algo-trading refusal when the terminal will not accept orders, else None.

    ``tradeapi_disabled`` is *"Disable automatic trading through the external Python API"*.
    With either switch off every ``order_send`` comes back ``10027`` and nothing in this code
    is wrong, so the test must say so instead of reporting a healthy account (BUILD §1).
    """
    trade_allowed = bool(getattr(terminal, "trade_allowed", False))
    api_disabled = bool(getattr(terminal, "tradeapi_disabled", False))
    if trade_allowed and not api_disabled:
        return None
    return (
        "algo trading is disabled in the terminal — every order would come back "
        "TRADE_RETCODE_CLIENT_DISABLES_AT (10027). The sidecar sends Ctrl+E and, if that "
        "does not take, clicks the Algo Trading toolbar button after initialize(); if it "
        "is still off, click Algo Trading once in VNC "
        "(`docker compose --profile debug up`) and check Tools > Options > Expert Advisors "
        "(clear 'Disable automatic trading through the external Python API'). "
        "common.ini must be UTF-16LE with Account=0 and Profile=0. "
        f"trade_allowed={trade_allowed} tradeapi_disabled={api_disabled}"
    )


def _account_fields(
    info: Any, runtime: Runtime, fallback_server: str
) -> tuple[dict[str, Any], str, str]:
    """Read the account namespace into the ``Mt5TestOut`` fields, plus detected/forced mode.

    Attribute access is the contract on this side of the RPC (``rpc.to_ns``), and a missing
    attribute raising ``AttributeError`` is normally what we want. Not here: a connection
    *test* is the one call an operator makes precisely because something is wrong, so every
    read is guarded and coerced and the answer is always a sentence.
    """
    raw_mode = _as_int(getattr(info, "margin_mode", None))
    # Never guess this one: 0 is both "netting" and what a missing field coerces to, and
    # netting is the branch that forbids multi-TP splitting (BUILD §1.3).
    detected = "unknown" if raw_mode is None else margin_mode_name(raw_mode)
    forced = _as_text(runtime.settings.FORCE_MARGIN_MODE).strip().lower()
    fields: dict[str, Any] = {
        "login": _as_int(getattr(info, "login", None)),
        "name": _as_text(getattr(info, "name", "")),
        "server": _as_text(getattr(info, "server", "")) or fallback_server,
        "balance": round(_as_float(getattr(info, "balance", None)) or 0.0, 2),
        "equity": round(_as_float(getattr(info, "equity", None)) or 0.0, 2),
        "currency": _as_text(getattr(info, "currency", "")),
        "leverage": _as_int(getattr(info, "leverage", None)) or 0,
        "margin_mode": forced or detected,
    }
    return fields, detected, forced


@router.post(
    "/mt5/test",
    response_model=Mt5TestOut,
    summary="Connect to the terminal and report the real account",
)
async def test_mt5(
    body: MT5Settings | None = Body(default=None),
    runtime: Runtime = Depends(get_runtime),
) -> Any:
    """Write the credentials if any were sent, then report the account the terminal is on.

    The frontend calls this with no body, in which case the *stored* credentials are tested;
    sending an :class:`MT5Settings` body stores it first, so "Save and test" is one round
    trip. Either way the answer carries the **real** balance, equity, currency, account name,
    server, leverage and margin mode read from ``account_info``, plus the ``terminal_info``
    check that proves the terminal will actually trade.

    A failure is HTTP 200 with ``ok: false`` and a reason naming which of the three things
    went wrong — sidecar unreachable, login rejected, or algo trading disabled.

    One honest caveat, reported rather than hidden: the sidecar reads ``MT5_LOGIN`` /
    ``MT5_PASSWORD`` / ``MT5_SERVER`` from the environment when it boots and there is no RPC
    method that re-logs-in a running terminal. If the stored login differs from the one the
    terminal is on, the answer says so and names both, because a green tick for somebody
    else's account is worse than a red one.
    """
    def fail(message: str, **fields: Any) -> Mt5TestOut:
        """Turn the MT5 dot red with this reason and answer 200 with ``ok: false``."""
        runtime.set_status("mt5", False, message)
        return Mt5TestOut(ok=False, message=message, **fields)

    try:
        async with session_scope() as session:
            if body is not None:
                _emit_changed(runtime, SECTION_MT5, await _write_mt5(session, body))
            server = _as_text(await get_setting(session, KEY_MT5_SERVER, ""))
            login = _as_int(await get_setting(session, KEY_MT5_LOGIN, None))
    except SignalPilotError as exc:
        return _error(exc)

    rpc = getattr(runtime, "rpc", None)
    if rpc is None:
        return _error(ConfigError("the MT5 RPC client is not initialised"))

    try:
        info = await rpc.account_info()
        terminal = await rpc.terminal_info()
    except RpcError as exc:
        reason = _mt5_failure_reason(exc, runtime, server, login)
        log.warning("mt5_test_failed rpc_code=%s reason=%s", exc.rpc_code, reason)
        return fail(reason, login=login, server=server)

    if info is None:
        return fail(
            "the terminal returned no account information — MetaTrader 5 is attached but not "
            "logged in; check MT5_LOGIN, MT5_PASSWORD and MT5_SERVER in .env and recreate "
            "the mt5-sidecar container",
            login=login,
            server=server,
        )

    fields, detected_mode, forced_mode = _account_fields(info, runtime, server)
    live_login = fields["login"]

    if terminal is None:
        return fail(
            "the terminal answered account_info but not terminal_info — the MT5 connection "
            "is half open and orders cannot be trusted; restart the mt5-sidecar container",
            **fields,
        )

    refusal = _algo_trading_reason(terminal)
    if refusal is None and login is not None and live_login is not None and live_login != login:
        refusal = (
            f"the terminal is logged in as {live_login} on {fields['server']}, but the saved "
            f"login is {login} on {server or '(unset)'} — the sidecar reads its credentials "
            "from .env when it boots, so copy them into .env and recreate the mt5-sidecar "
            "container before trading"
        )
    if refusal is not None:
        return fail(refusal, **fields)

    message = (
        f"connected to {fields['name'] or 'the account'} ({live_login}) on {fields['server']}: "
        f"{fields['balance']:,.2f} {fields['currency']} balance, {fields['equity']:,.2f} "
        f"equity, 1:{fields['leverage']} leverage, {fields['margin_mode']} account"
    )
    if forced_mode:
        message += f" (FORCE_MARGIN_MODE is set, the broker reports {detected_mode})"
    runtime.set_status("mt5", True, None)
    log.info(
        "mt5_test_ok login=%s server=%s margin_mode=%s",
        live_login,
        fields["server"],
        fields["margin_mode"],
    )
    return Mt5TestOut(ok=True, message=message, **fields)


# --------------------------------------------------------------------------------------
# PUT /api/settings/provider
# --------------------------------------------------------------------------------------


@router.put(
    "/provider",
    response_model=SettingsOut,
    summary="Store which model flies the plane and how to reach it",
)
async def put_provider_settings(
    body: ProviderSettings,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> Any:
    """Store the provider configuration and drop any provider cached on the runtime.

    ``base_url`` arrives already normalized by ``app.schemas`` — ``host``, ``host/`` and
    ``host/v1`` all store the same value (BUILD §4.3). Omitting ``api_key``, sending ``null``
    or echoing the mask leaves the stored key untouched; an empty string clears it.

    The invalidation is the load-bearing part: :meth:`Runtime.provider` memoises the built
    client keyed by kind, base URL, key, model and headers, and an agent run that started
    before this write would otherwise keep talking to the old endpoint with the old key.

    There is no OAuth here and there never will be: consumer Claude and ChatGPT
    subscriptions have no public API, so the only credentials this accepts are an API key or
    any OpenAI-compatible base URL (BUILD §4.4).
    """
    try:
        written = [
            KEY_PROVIDER_KIND,
            KEY_PROVIDER_BASE_URL,
            KEY_PROVIDER_MODEL,
            KEY_PROVIDER_EXTRA_HEADERS,
            KEY_PROVIDER_MAX_TOKENS,
            KEY_PROVIDER_VISION_OVERRIDE,
            KEY_PROVIDER_TOOLS_OVERRIDE,
        ]
        await set_setting(session, KEY_PROVIDER_KIND, body.kind)
        await set_setting(session, KEY_PROVIDER_BASE_URL, body.base_url)
        await set_setting(session, KEY_PROVIDER_MODEL, body.model)
        await set_setting(session, KEY_PROVIDER_EXTRA_HEADERS, dict(body.extra_headers))
        await set_setting(session, KEY_PROVIDER_MAX_TOKENS, int(body.max_tokens))
        await set_setting(session, KEY_PROVIDER_VISION_OVERRIDE, body.supports_vision_override)
        await set_setting(session, KEY_PROVIDER_TOOLS_OVERRIDE, body.supports_tools_override)
        if await _write_secret(session, KEY_PROVIDER_API_KEY, body.api_key):
            written.append(KEY_PROVIDER_API_KEY)

        runtime.invalidate_provider()
        _emit_changed(runtime, SECTION_PROVIDER, written)
        return await _settings_tree(session, runtime)
    except SignalPilotError as exc:
        return _error(exc)


# --------------------------------------------------------------------------------------
# Notifier — PUT /api/settings/notifier, POST /test, POST /detect
# --------------------------------------------------------------------------------------


@router.put(
    "/notifier",
    response_model=SettingsOut,
    summary="Store the operator's own Telegram bot",
)
async def put_notifier_settings(
    body: NotifierSettings,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> Any:
    """Store the bot token, target chat, master switch, per-type switches and admin ids.

    This is a whole-section write for everything except the token: ``chat_id``, ``enabled``,
    ``types`` and ``admin_ids`` are stored exactly as sent, so a form that posts
    ``chat_id: null`` clears a previously detected chat and one that posts ``admin_ids: []``
    turns the allowlist off again. Load the tree, edit it, send it back — which is what
    ``/notifier/detect`` assumes when it tells the page to refetch.

    ``admin_ids`` is validated in :class:`app.schemas.NotifierSettings`, which rejects a
    non-positive or non-numeric id with a message anchored on the field rather than quietly
    dropping it (a dropped id would leave the operator staring at a list missing the entry
    they just typed).

    Changing ``chat_id`` by hand **clears** ``notifier.bound_to``. That row records who the
    detection bound, and once the number underneath it has been retyped it no longer
    describes anything — showing a name beside a chat it was never read from would be worse
    than showing no name at all.

    The running :class:`Notifier` picks the new values up on its own: it watches the bus for
    ``settings.changed`` and reloads its cached config, allowlist included.
    """
    try:
        written = [
            KEY_NOTIFIER_CHAT_ID,
            KEY_NOTIFIER_ENABLED,
            KEY_NOTIFIER_TYPES,
            KEY_NOTIFIER_ADMIN_IDS,
        ]
        previous_chat_id = _as_int(await get_setting(session, KEY_NOTIFIER_CHAT_ID, None))
        await set_setting(session, KEY_NOTIFIER_CHAT_ID, body.chat_id)
        await set_setting(session, KEY_NOTIFIER_ENABLED, bool(body.enabled))
        await set_setting(session, KEY_NOTIFIER_TYPES, body.types.model_dump())
        await set_setting(session, KEY_NOTIFIER_ADMIN_IDS, list(body.admin_ids))
        if body.chat_id != previous_chat_id:
            await set_setting(session, KEY_NOTIFIER_BOUND_TO, None)
            written.append(KEY_NOTIFIER_BOUND_TO)
        if await _write_secret(session, KEY_NOTIFIER_TOKEN, body.bot_token):
            written.append(KEY_NOTIFIER_TOKEN)
        _emit_changed(runtime, SECTION_NOTIFIER, written)
        return await _settings_tree(session, runtime)
    except SignalPilotError as exc:
        return _error(exc)


def _require_notifier(runtime: Runtime) -> Any:
    """Return the runtime's notifier or explain that the lifespan never built one."""
    notifier = getattr(runtime, "notifier", None)
    if notifier is None:
        raise ConfigError("the notifier is not initialised")
    return notifier


@router.post(
    "/notifier/test",
    response_model=NotifierTestOut,
    summary="Validate the stored bot token with getMe",
)
async def test_notifier(runtime: Runtime = Depends(get_runtime)) -> Any:
    """Call ``getMe`` on the stored bot token and report the bot's @username.

    The token is never sent by the client and never read here: an empty string tells
    :meth:`Notifier.test_token` to resolve the stored value itself, which also registers it
    with the log redactor. A rejected token is HTTP 200 with ``ok: false`` and the reason.
    """
    try:
        notifier = _require_notifier(runtime)
        result = await notifier.test_token("")
    except SignalPilotError as exc:
        return _error(exc)
    payload = result if isinstance(result, Mapping) else {}
    return NotifierTestOut(
        ok=bool(payload.get("ok")),
        bot_username=_as_text(payload.get("bot_username")),
        message=_as_text(payload.get("message")),
    )


@router.post(
    "/notifier/detect",
    response_model=NotifierDetectOut,
    summary="Fill the chat id from the first message the operator sends the bot",
)
async def detect_notifier_chat(runtime: Runtime = Depends(get_runtime)) -> Any:
    """Poll ``getUpdates`` until an allowed account messages the bot, then store their chat.

    The poll runs for up to ``NOTIFY_DETECT_SECONDS`` at ``NOTIFY_POLL_SECONDS`` intervals,
    so this request is deliberately long-lived; the UI is expected to tell the operator to
    send ``/start`` to the bot while it waits. Making them find their own numeric chat id by
    hand is the kind of small friction that gets a self-hosted project abandoned (BUILD §7).

    When ``notifier.admin_ids`` is populated, a message from anyone else does not bind
    anything and does not end the wait. If the window then expires having seen only
    non-admins, the ``message`` says exactly that — a different fault with a different fix
    from "nobody messaged the bot", and the operator has to be able to tell them apart.

    The response names the account that was bound (``user_id``, ``username``,
    ``first_name``). With an empty allowlist that identity is the *only* check on who this
    bot now reports to, so it is stored beside the chat id and returned here to be shown.

    :meth:`Notifier.detect_chat` persists both itself; the writes are repeated here because
    that method swallows a failed write to keep the detection reportable, and this route's
    contract is that a detected binding *is* stored.
    """
    try:
        notifier = _require_notifier(runtime)
        result = await notifier.detect_chat("")
    except SignalPilotError as exc:
        return _error(exc)

    payload = result if isinstance(result, Mapping) else {}
    ok = bool(payload.get("ok"))
    chat_id = _as_int(payload.get("chat_id"))
    user_id = _as_int(payload.get("user_id"))
    username = _as_text(payload.get("username")).strip()
    first_name = _as_text(payload.get("first_name")).strip()
    if ok and chat_id is not None:
        # No user id means a channel post, which has no account behind it to record.
        bound_to = (
            None
            if user_id is None
            else {"user_id": user_id, "username": username, "first_name": first_name}
        )
        try:
            async with session_scope() as session:
                await set_setting(session, KEY_NOTIFIER_CHAT_ID, chat_id)
                await set_setting(session, KEY_NOTIFIER_BOUND_TO, bound_to)
            _emit_changed(
                runtime, SECTION_NOTIFIER, [KEY_NOTIFIER_CHAT_ID, KEY_NOTIFIER_BOUND_TO]
            )
        except SignalPilotError as exc:
            return _error(exc)

    # The notifier reports a timeout as a short message plus a `hint` naming the bot to open
    # — or, when only strangers wrote in, naming the id that would have to be allowed; the
    # settings page renders one string, so they are joined here rather than dropped.
    message = _as_text(payload.get("message")).strip()
    hint = _as_text(payload.get("hint")).strip()
    if hint and hint not in message:
        message = f"{message} — {hint}" if message else hint
    return NotifierDetectOut(
        ok=ok,
        chat_id=chat_id,
        chat_title=_as_text(payload.get("chat_title")),
        message=message,
        user_id=user_id,
        username=username,
        first_name=first_name,
        is_admin=bool(payload.get("is_admin")),
        enforced=bool(payload.get("enforced")),
    )


@router.post(
    "/notifier/whoami",
    response_model=NotifierWhoamiOut,
    summary="List the accounts that have messaged the bot recently, with their user ids",
)
async def notifier_whoami(runtime: Runtime = Depends(get_runtime)) -> Any:
    """Report everyone who has messaged the bot recently, flagged against the allowlist.

    An allowlist the operator cannot populate is an allowlist nobody turns on. A Telegram
    *user* id is not shown anywhere in the Telegram client, so without this the answer to
    "what is my id" is "go and message a third-party bot", and the field stays empty — which
    is the state this whole feature exists to get out of.

    The poll is short (``WHOAMI_WINDOW_SECONDS``) and **non-destructive**: it sends no
    ``offset``, so it confirms nothing and the updates ``/notifier/detect`` is waiting for
    are still queued afterwards. One row per sender, each flagged ``is_admin`` against the
    allowlist as it stands right now.
    """
    try:
        notifier = _require_notifier(runtime)
        result = await notifier.recent_senders("")
    except SignalPilotError as exc:
        return _error(exc)

    payload = result if isinstance(result, Mapping) else {}
    rows: list[NotifierUpdateOut] = []
    raw_updates = payload.get("updates")
    if isinstance(raw_updates, Sequence) and not isinstance(raw_updates, (str, bytes)):
        for index, item in enumerate(raw_updates):
            if not isinstance(item, Mapping):
                continue
            user_id = _as_int(item.get("user_id"))
            chat_id = _as_int(item.get("chat_id"))
            if user_id is None or chat_id is None:
                # Only a numeric user id can ever go on the allowlist, so a row without one
                # is not something the operator could act on.
                log.warning("whoami_row_ignored index=%d reason=no_numeric_id", index)
                continue
            rows.append(
                NotifierUpdateOut(
                    user_id=user_id,
                    username=_as_text(item.get("username")).strip(),
                    first_name=_as_text(item.get("first_name")).strip(),
                    chat_id=chat_id,
                    text=_as_text(item.get("text")),
                    is_admin=bool(item.get("is_admin")),
                )
            )
    return NotifierWhoamiOut(
        ok=bool(payload.get("ok")),
        updates=rows,
        message=_as_text(payload.get("message")).strip(),
        enforced=bool(payload.get("enforced")),
    )


# --------------------------------------------------------------------------------------
# PUT /api/settings/limits
# --------------------------------------------------------------------------------------


@router.put(
    "/limits",
    response_model=SettingsOut,
    summary="Store the optional operator limits, which ship disabled",
)
async def put_limits(
    body: LimitsSettings,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> Any:
    """Store the optional limits exactly as sent — null stays null and empty stays empty.

    Every one of these ships off and a null or empty value is **not evaluated at all**
    (BUILD §6). That is why nothing here coerces a missing number to ``0``: zero is a limit
    that refuses every order, which would be a risk veto shipped enabled — the one thing the
    owner ruled out. The three unconditional gates are the ``armed`` switch, the stop-loss
    rule and the duplicate-signal guard; these are not among them.

    ``trading_hours`` is dumped by alias so each window stores ``{"days","from","to","tz"}``,
    matching the shape ``guards.check_optional_limits`` reads and the frontend's
    ``TradingWindow``.
    """
    try:
        written = [
            KEY_LIMIT_MAX_VOLUME,
            KEY_LIMIT_MAX_POSITIONS,
            KEY_LIMIT_MAX_DAILY_LOSS,
            KEY_LIMIT_ALLOWLIST,
            KEY_LIMIT_TRADING_HOURS,
        ]
        await set_setting(session, KEY_LIMIT_MAX_VOLUME, body.max_volume_per_trade)
        await set_setting(session, KEY_LIMIT_MAX_POSITIONS, body.max_open_positions)
        await set_setting(session, KEY_LIMIT_MAX_DAILY_LOSS, body.max_daily_loss_percent)
        await set_setting(session, KEY_LIMIT_ALLOWLIST, list(body.symbol_allowlist))
        await set_setting(
            session,
            KEY_LIMIT_TRADING_HOURS,
            [window.model_dump(by_alias=True) for window in body.trading_hours],
        )
        _emit_changed(runtime, SECTION_LIMITS, written)
        return await _settings_tree(session, runtime)
    except SignalPilotError as exc:
        return _error(exc)


# --------------------------------------------------------------------------------------
# PUT /api/settings/general
# --------------------------------------------------------------------------------------


@router.put(
    "/general",
    response_model=SettingsOut,
    summary="Store general defaults: risk, lots, filters, the prompt, and the locale",
)
async def put_general(
    body: GeneralSettings,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(get_runtime),
) -> Any:
    """Store the general defaults the dashboard edits in one form.

    ``orders_per_signal`` and ``order_lot_size`` size a classic signal when the model
    omits volume and risk percent (default 2 orders of 0.2 lots). ``use_fixed_order_lots``
    (default on) ignores a model ``risk_percent`` on that path; turn it off and
    ``risk_percent`` sizes the trade again. An explicit volume is used either way.
    ``max_entry_drift_usd`` (default 5) is the only "price already moved" skip for a
    classic gold entry: a fill farther than that is refused, and a fill at or inside
    it must be opened.
    Ladders of 4+ targets still use ``ladder_volume``. ``be_after_tp1`` moves the remaining classic orders to
    breakeven after TP1. ``reviewer_enabled`` gates playbook generation. The tag filter
    drops Telegram posts that do not contain ``tag_filter_tag``. A system prompt identical
    to the built-in text is stored as empty so later code changes of that text still apply.
    """
    try:
        prompt = body.system_prompt.strip()
        if prompt == SYSTEM_PROMPT.strip():
            prompt = ""
        written = [
            KEY_DEFAULT_RISK,
            KEY_LADDER_VOLUME,
            KEY_LOCALE,
            KEY_ORDERS_PER_SIGNAL,
            KEY_ORDER_LOT_SIZE,
            KEY_USE_FIXED_ORDER_LOTS,
            KEY_MAX_ENTRY_DRIFT_USD,
            KEY_BE_AFTER_TP1,
            KEY_REVIEWER_ENABLED,
            KEY_TAG_FILTER_ENABLED,
            KEY_TAG_FILTER_TAG,
            KEY_SYSTEM_PROMPT,
        ]
        await set_setting(session, KEY_DEFAULT_RISK, float(body.default_risk_percent))
        await set_setting(session, KEY_LADDER_VOLUME, float(body.ladder_volume))
        await set_setting(session, KEY_LOCALE, body.locale)
        await set_setting(session, KEY_ORDERS_PER_SIGNAL, int(body.orders_per_signal))
        await set_setting(session, KEY_ORDER_LOT_SIZE, float(body.order_lot_size))
        await set_setting(session, KEY_USE_FIXED_ORDER_LOTS, bool(body.use_fixed_order_lots))
        await set_setting(session, KEY_MAX_ENTRY_DRIFT_USD, float(body.max_entry_drift_usd))
        await set_setting(session, KEY_BE_AFTER_TP1, bool(body.be_after_tp1))
        await set_setting(session, KEY_REVIEWER_ENABLED, bool(body.reviewer_enabled))
        await set_setting(session, KEY_TAG_FILTER_ENABLED, bool(body.tag_filter_enabled))
        await set_setting(session, KEY_TAG_FILTER_TAG, body.tag_filter_tag.strip())
        await set_setting(session, KEY_SYSTEM_PROMPT, prompt)
        _emit_changed(runtime, SECTION_GENERAL, written)
        return await _settings_tree(session, runtime)
    except SignalPilotError as exc:
        return _error(exc)
