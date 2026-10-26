"""Pydantic v2 request and response models for every REST route in CONTRACTS §18."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    StringConstraints,
    field_validator,
)

from app.ai.base import normalize_base_url
from app.config import (
    DEFAULT_RISK_PERCENT,
    LADDER_STATS_WINDOW,
    LADDER_VOLUME,
    MAX_CANDLES,
    MAX_TOKENS,
    MAX_TP_INPUT,
    SYMBOL_LIST_LIMIT_DEFAULT,
    SYMBOL_LIST_LIMIT_MAX,
)
from app.operator_settings import (
    BE_AFTER_TP1_DEFAULT,
    ORDER_LOT_SIZE_DEFAULT,
    ORDERS_PER_SIGNAL_DEFAULT,
    ORDERS_PER_SIGNAL_MAX,
    MAX_ENTRY_DRIFT_USD_DEFAULT,
    USE_FIXED_ORDER_LOTS_DEFAULT,
    REVIEWER_ENABLED_DEFAULT,
    SYSTEM_PROMPT_MAX_LENGTH,
    TAG_FILTER_ENABLED_DEFAULT,
    TAG_FILTER_TAG_DEFAULT,
    TAG_MAX_LENGTH,
)
from app.crypto import MASK
from app.errors import ConfigError

# ---------------------------------------------------------------------------
# Shared constrained types. The same rules the frontend enforces (BUILD §9).
# ---------------------------------------------------------------------------

#: E.164: a leading +, a non-zero country digit, then 6..14 more digits.
PHONE_E164_PATTERN = r"^\+[1-9]\d{6,14}$"
#: 24-hour clock, used by `limits.trading_hours` windows.
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ReasonStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
PhoneStr = Annotated[str, StringConstraints(strip_whitespace=True, pattern=PHONE_E164_PATTERN)]
HourMinuteStr = Annotated[str, StringConstraints(strip_whitespace=True, pattern=HHMM_PATTERN)]
CodeStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
TicketInt = Annotated[int, Field(gt=0)]
WeekdayInt = Annotated[int, Field(ge=0, le=6)]
HeaderMap = dict[str, str]

#: Take profit is a list because a signal routinely carries TP1/TP2/TP3, and some
#: channels post a full ladder of 4–8 rungs. The executor collapses ladders to two legs;
#: ``MAX_TP_SPLITS`` only caps classic one-per-target hedging splits.
TpList = Annotated[list[PositiveFloat], Field(max_length=MAX_TP_INPUT)]

ProviderKind = Literal["anthropic", "openai"]
Side = Literal["buy", "sell"]
PendingKind = Literal["limit", "stop"]
Direction = Literal["above", "below"]
DistanceUnit = Literal["pips", "points", "price", "percent"]
TimeframeName = Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
LocaleName = Literal["en", "ru"]
RunnerMode = Literal["be_after_cover", "hold_channel_sl"]
MessageStatus = Literal["pending", "processing", "processed", "skipped", "error"]
RunStatus = Literal["running", "finished", "incomplete", "error"]
RunDecision = Literal["acted", "ignored", "deferred"]
MarginModeName = Literal["netting", "exchange", "hedging"]


def _unmask_secret(value: str | None) -> str | None:
    """Turn the display mask back into None so an echoed secret means "leave unchanged"."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    if value.strip() == MASK:
        return None
    return value


def _normalized_base_url(value: str | None) -> str:
    """Accept host, host/ and host/v1 as one canonical base URL (BUILD §4.3)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        normalized = normalize_base_url(text)
    except ConfigError as exc:
        # Pydantic only turns ValueError into a field error; any other exception escapes
        # validation entirely and a malformed base_url becomes a 500 instead of a 422.
        raise ValueError(exc.message) from exc
    if not normalized:
        return ""
    if not normalized.lower().startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    return normalized


class RequestModel(BaseModel):
    """Base for request bodies: an unknown key is an error, never a silent no-op."""

    model_config = ConfigDict(extra="forbid")


class OutModel(BaseModel):
    """Base for responses: constructible directly from ORM rows, dataclasses and namespaces."""

    model_config = ConfigDict(from_attributes=True)


class ToolOutModel(OutModel):
    """Base for executor tool payloads: declared fields plus whatever extra context a tool adds."""

    model_config = ConfigDict(from_attributes=True, extra="allow")


# ---------------------------------------------------------------------------
# Error envelope — {"error": {"code", "message", "detail"}}
# ---------------------------------------------------------------------------


class ErrorBody(OutModel):
    """The body of the single error envelope every endpoint uses."""

    code: str = Field(description="Machine-readable class, e.g. trading_error.")
    message: str = Field(description="One actionable English sentence.")
    detail: dict[str, Any] = Field(default_factory=dict, description="Free-form context.")


class ErrorResponse(OutModel):
    """Error envelope returned with every non-2xx JSON response."""

    error: ErrorBody


class OkResponse(OutModel):
    """Minimal acknowledgement for routes with nothing else to report."""

    ok: bool = True
    message: str = ""


# ---------------------------------------------------------------------------
# Small shared shapes
# ---------------------------------------------------------------------------


class ComponentStatusOut(OutModel):
    """One status dot: MT5, Telegram or the AI provider."""

    ok: bool = False
    error: str | None = None


class StatusMapOut(OutModel):
    """The three status dots the cockpit header renders."""

    mt5: ComponentStatusOut = Field(default_factory=ComponentStatusOut)
    telegram: ComponentStatusOut = Field(default_factory=ComponentStatusOut)
    provider: ComponentStatusOut = Field(default_factory=ComponentStatusOut)


class DistanceOut(OutModel):
    """A distance reported in all three units so nobody has to convert (BUILD §2)."""

    price: float = 0.0
    points: float = 0.0
    pips: float = 0.0


class NotifyTypes(RequestModel):
    """Per-type notification switches (settings key `notifier.types`)."""

    opened: bool = True
    modified: bool = True
    closed: bool = True
    rejected: bool = True
    attention: bool = True


class TradingWindow(RequestModel):
    """One allowed trading window; the list ships empty, which means always allowed."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    days: Annotated[list[WeekdayInt], Field(min_length=1, max_length=7)] = Field(
        description="Days of the week the window applies to, Monday=0 .. Sunday=6."
    )
    from_: HourMinuteStr = Field(alias="from", description="Window start, HH:MM.")
    to: HourMinuteStr = Field(description="Window end, HH:MM.")
    tz: NonEmptyStr = Field(default="UTC", description="IANA timezone name.")


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


class HealthOut(OutModel):
    """Liveness plus everything the header needs on first paint."""

    ok: bool = True
    version: str = ""
    uptime_s: float = 0.0
    status: StatusMapOut = Field(default_factory=StatusMapOut)
    armed: bool = False
    needs_reentry: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Auth — /api/auth/login, /api/auth/logout, /api/auth/status
# ---------------------------------------------------------------------------


class AuthLoginRequest(RequestModel):
    """POST /api/auth/login — the dashboard sign-in form.

    Neither field is stripped or length-checked beyond a sane ceiling: a passphrase may
    legitimately start or end with a space, and an empty one is *wrong credentials* (401,
    counted by the throttle), not a validation error the attacker gets for free.
    """

    username: str = Field(max_length=4096, description="The configured SP_AUTH_USER.")
    password: str = Field(max_length=4096, description="The configured SP_AUTH_PASSWORD.")


class AuthLoginOut(OutModel):
    """POST /api/auth/login — success, with the cookie in the Set-Cookie header."""

    ok: bool = True
    username: str = Field(default="", description="The signed-in operator's username.")


class AuthStatusOut(OutModel):
    """GET /api/auth/status — the only thing the SPA may ask before it has credentials.

    Deliberately three fields and no more: this route answers unauthenticated, so anything
    added here is published to whoever can reach the port. ``authenticated`` is true on a
    deployment with no login configured, because there the request *is* allowed through.
    """

    authenticated: bool = Field(description="Whether this request already carries a session.")
    required: bool = Field(description="Whether the deployment has a login configured at all.")
    username: str | None = Field(
        default=None, description="The signed-in username, or null when not signed in."
    )


# ---------------------------------------------------------------------------
# Settings — requests
# ---------------------------------------------------------------------------


class MT5Settings(RequestModel):
    """PUT /api/settings/mt5 — broker terminal credentials."""

    server: NonEmptyStr = Field(description="Broker server name, e.g. Exness-MT5Trial15.")
    login: PositiveInt = Field(description="MT5 account number, a positive integer.")
    password: str | None = Field(
        default=None,
        description=(
            "Investor or master password. Send null, omit the field, or echo back the mask "
            f"{MASK!r} to leave the stored password unchanged; an empty string clears it."
        ),
    )

    @field_validator("password", mode="before")
    @classmethod
    def _mask_means_unchanged(cls, value: str | None) -> str | None:
        return _unmask_secret(value)


class ProviderSettings(RequestModel):
    """PUT /api/settings/provider — which model flies the plane and how to reach it."""

    kind: ProviderKind = Field(description="anthropic or any OpenAI-compatible endpoint.")
    base_url: str = Field(
        default="",
        description="Endpoint root; host, host/ and host/v1 all normalize to the same value.",
    )
    api_key: str | None = Field(
        default=None,
        description=(
            f"Provider API key. Send null, omit the field, or echo back the mask {MASK!r} to "
            "leave the stored key unchanged; an empty string clears it."
        ),
    )
    model: NonEmptyStr = Field(description="Model id exactly as the provider lists it.")
    extra_headers: HeaderMap = Field(
        default_factory=dict,
        description="Extra request headers, for a self-hosted gateway that needs them.",
    )
    max_tokens: PositiveInt = Field(
        default=MAX_TOKENS,
        description="Required by Anthropic; also sent to OpenAI-compatible endpoints.",
    )
    supports_vision_override: bool | None = Field(
        default=None, description="Null uses the probed or declared capability."
    )
    supports_tools_override: bool | None = Field(
        default=None, description="Null uses the probed or declared capability."
    )

    @field_validator("api_key", mode="before")
    @classmethod
    def _mask_means_unchanged(cls, value: str | None) -> str | None:
        return _unmask_secret(value)

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize(cls, value: str | None) -> str:
        return _normalized_base_url(value)


def _clean_admin_ids(value: Any) -> list[int]:
    """Validate `notifier.admin_ids`: positive whole numbers, de-duplicated, order kept.

    Anything else is rejected rather than dropped. This is the opposite of what
    ``notifier._coerce_admin_ids`` does when *reading* a stored row, and deliberately so: a
    row already in the database has to be survivable, while a value the operator is typing
    right now has to be corrected. Silently discarding the id they meant to add would leave
    them looking at a list that does not contain it and no explanation of why.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError("admin_ids must be a list of Telegram user ids")
    ids: list[int] = []
    seen: set[int] = set()
    for item in value:
        if isinstance(item, bool) or item is None:
            raise ValueError(f"every admin id must be a whole number, got {item!r}")
        if isinstance(item, float):
            if item != int(item):
                raise ValueError(f"every admin id must be a whole number, got {item!r}")
            number = int(item)
        elif isinstance(item, int):
            number = item
        elif isinstance(item, str):
            text = item.strip()
            try:
                number = int(text)
            except ValueError as exc:
                raise ValueError(
                    f"every admin id must be a whole number, got {item!r} — a Telegram user "
                    "id is numeric, not a @username"
                ) from exc
        else:
            raise ValueError(f"every admin id must be a whole number, got {item!r}")
        if number <= 0:
            raise ValueError(f"a Telegram user id is a positive number, got {number}")
        if number in seen:
            continue
        seen.add(number)
        ids.append(number)
    return ids


class NotifierSettings(RequestModel):
    """PUT /api/settings/notifier — the operator's own Telegram bot."""

    bot_token: str | None = Field(
        default=None,
        description=(
            f"@BotFather token. Send null, omit the field, or echo back the mask {MASK!r} to "
            "leave the stored token unchanged; an empty string clears it."
        ),
    )
    chat_id: int | None = Field(
        default=None, description="Target chat; negative for groups, detected by the Test button."
    )
    enabled: bool = Field(default=False, description="Master switch for outgoing notifications.")
    types: NotifyTypes = Field(default_factory=NotifyTypes)
    admin_ids: list[PositiveInt] = Field(
        default_factory=list,
        description=(
            "Telegram USER ids allowed to bind and receive this bot, stored de-duplicated in "
            "the order given. EMPTY MEANS NOTHING IS ENFORCED and the first account to "
            "message the bot is bound, which is the behaviour this project shipped with. "
            "Once it is non-empty, detection ignores everyone else and no notification is "
            "delivered to a chat that is not one of these ids."
        ),
    )

    @field_validator("bot_token", mode="before")
    @classmethod
    def _mask_means_unchanged(cls, value: str | None) -> str | None:
        return _unmask_secret(value)

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _validate_admin_ids(cls, value: Any) -> list[int]:
        return _clean_admin_ids(value)


class LimitsSettings(RequestModel):
    """PUT /api/settings/limits — the optional operator limits, all shipped off (BUILD §6)."""

    max_volume_per_trade: PositiveFloat | None = Field(
        default=None, description="Null disables the check entirely."
    )
    max_open_positions: PositiveInt | None = Field(
        default=None, description="Null disables the check entirely."
    )
    max_daily_loss_percent: PositiveFloat | None = Field(
        default=None, description="Null disables the check entirely."
    )
    symbol_allowlist: list[NonEmptyStr] = Field(
        default_factory=list,
        description=(
            "Broker symbol names, e.g. XAUUSDr. Stored upper-cased and de-duplicated. "
            "EMPTY MEANS EVERY SYMBOL IS ALLOWED and the limit is not evaluated at all."
        ),
    )
    trading_hours: list[TradingWindow] = Field(
        default_factory=list, description="Empty means trading is allowed at any hour."
    )

    @field_validator("symbol_allowlist", mode="after")
    @classmethod
    def _normalize_allowlist(cls, value: list[str]) -> list[str]:
        """Upper-case, trim and de-duplicate the names while keeping the operator's order.

        The picker sends what the broker calls a symbol and a hand-typed list sends whatever
        the operator typed; both have to round-trip to the same stored value, because
        ``guards._check_symbol_allowlist`` compares upper-case and the settings page renders
        the list straight back into the picker's selection.

        An empty list stays empty. That is not a formatting detail: an empty allowlist is
        the shipped default and it means *every symbol is allowed* (BUILD §6), so nothing
        here may ever turn "no entries" into an entry.
        """
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            name = str(item).strip().upper()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out


class GeneralSettings(RequestModel):
    """PUT /api/settings/general — defaults that are not credentials."""

    default_risk_percent: PositiveFloat = Field(
        description=(
            "Risk used when the model supplies neither volume nor risk_percent on a "
            "single-target (or classic 1–3 TP) signal. Deliberately has no upper bound — "
            "a cap here would be a risk veto (BUILD §2). Multi-TP ladders ignore this and "
            "use ladder_volume instead."
        )
    )
    ladder_volume: PositiveFloat = Field(
        default=LADDER_VOLUME,
        description=(
            "Fixed total lots for a multi-TP ladder (4+ take-profits), split ~50/50 into "
            "two legs. Not computed from risk percent. Default 0.10 (0.05 + 0.05)."
        ),
    )
    locale: LocaleName = Field(default="en", description="Locale for notification text.")
    orders_per_signal: int = Field(
        default=ORDERS_PER_SIGNAL_DEFAULT,
        ge=1,
        le=ORDERS_PER_SIGNAL_MAX,
        description=(
            "How many orders a classic signal (fewer than 4 take-profits) opens when the "
            "model omits volume and risk_percent. Nearest targets first; extras are unmanaged. "
            "Default 2."
        ),
    )
    order_lot_size: PositiveFloat = Field(
        default=ORDER_LOT_SIZE_DEFAULT,
        description=(
            "Lot size of each of those orders. Default 0.2, so two orders are 0.2 + 0.2. "
            "Not a risk percent and not the ladder total."
        ),
    )
    use_fixed_order_lots: bool = Field(
        default=USE_FIXED_ORDER_LOTS_DEFAULT,
        description=(
            "When true (the default), a classic signal ignores the model's risk_percent "
            "and opens orders_per_signal orders of order_lot_size. When false, risk_percent "
            "sizes the trade again. An explicit volume is used in both modes. Ladders ignore "
            "risk_percent either way. On, the model must not defer because that size looks large."
        ),
    )
    max_entry_drift_usd: PositiveFloat = Field(
        default=MAX_ENTRY_DRIFT_USD_DEFAULT,
        description=(
            "Classic gold-style market entries are skipped for 'price already moved' only "
            "when the live fill is farther than this many price units from the channel entry "
            "or zone. At or inside it the entry must be opened. Default 5."
        ),
    )
    be_after_tp1: bool = Field(
        default=BE_AFTER_TP1_DEFAULT,
        description=(
            "When true, a classic multi-order signal moves the remaining stops to breakeven "
            "after TP1 fills, unless the model chose hold_channel_sl or already changed a stop."
        ),
    )
    reviewer_enabled: bool = Field(
        default=REVIEWER_ENABLED_DEFAULT,
        description="When false, the channel reviewer does not generate a playbook.",
    )
    tag_filter_enabled: bool = Field(
        default=TAG_FILTER_ENABLED_DEFAULT,
        description=(
            "When true, only Telegram messages that contain tag_filter_tag reach the "
            "signal pipeline. Match is a case-insensitive substring."
        ),
    )
    tag_filter_tag: str = Field(
        default=TAG_FILTER_TAG_DEFAULT,
        max_length=TAG_MAX_LENGTH,
        description="Tag that must appear in a message when the filter is on. Default #soft.",
    )
    system_prompt: str = Field(
        default="",
        max_length=SYSTEM_PROMPT_MAX_LENGTH,
        description=(
            "Standing LLM system prompt. Empty, or text identical to the built-in prompt, "
            "keeps the built-in text. Account state is still appended at run time."
        ),
    )

    @field_validator("tag_filter_tag", mode="before")
    @classmethod
    def _clean_tag(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()[:TAG_MAX_LENGTH]

    @field_validator("system_prompt", mode="before")
    @classmethod
    def _clean_prompt(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value)


# ---------------------------------------------------------------------------
# Settings — responses
# ---------------------------------------------------------------------------


class Mt5SettingsOut(OutModel):
    """MT5 section of GET /api/settings; the password is always the mask."""

    server: str = ""
    login: int | None = None
    password: str | None = None
    has_password: bool = False
    #: Why the saved account could not be applied at the broker, if it could not.
    #:
    #: Storing credentials and applying them are different things: the sidecar reads MT5_*
    #: from its environment once, at boot, so a PUT that only wrote to the database would
    #: report success while the terminal stayed logged into the previous account. The write
    #: still succeeds when the broker side fails — the operator needs the page back to fix
    #: it — so the failure rides here rather than in an error envelope.
    apply_error: str | None = None


class TelegramUserOut(OutModel):
    """The logged-in Telegram user account."""

    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None


class SourceOut(OutModel):
    """A signal source row; exactly one is active at a time."""

    id: int
    kind: str = "telegram"
    chat_id: int
    title: str = ""
    username: str | None = None
    entity_type: str = "channel"
    is_active: bool = False
    last_message_id: int = 0
    created_at: datetime | None = None


class DialogOut(OutModel):
    """One entry of the channel picker."""

    chat_id: int
    title: str = ""
    username: str | None = None
    entity_type: str = "channel"
    is_channel: bool = False


class TelegramSettingsOut(OutModel):
    """Telegram section of GET /api/settings; api_hash is always the mask."""

    api_id: int | None = None
    api_hash: str | None = None
    has_api_hash: bool = False
    phone: str | None = None
    authorized: bool = False
    #: A session is stored, so a restart reconnects on its own and no re-login is needed.
    #: Reported separately from `authorized` because the two diverge exactly when it matters:
    #: right after a restart the session exists while the socket is still coming up.
    has_session: bool = False
    user: TelegramUserOut | None = None
    source: SourceOut | None = None


class CapabilitiesOut(OutModel):
    """What a given model can actually do — declared by the provider or probed once."""

    supports_vision: bool = False
    supports_tools: bool = False
    thinking_form: str | None = None
    source: str = "probed"


class ModelInfoOut(OutModel):
    """One row of the model picker. Only `id` is guaranteed by every gateway (BUILD §4.3)."""

    id: str
    display_name: str = ""
    supports_vision: bool | None = None
    supports_tools: bool | None = None
    context_length: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ProviderSettingsOut(OutModel):
    """Provider section of GET /api/settings; api_key is always the mask."""

    kind: str = "anthropic"
    base_url: str = ""
    api_key: str | None = None
    has_api_key: bool = False
    model: str = ""
    extra_headers: HeaderMap = Field(default_factory=dict)
    max_tokens: int = MAX_TOKENS
    supports_vision_override: bool | None = None
    supports_tools_override: bool | None = None
    capabilities: CapabilitiesOut | None = None


class NotifierBoundToOut(OutModel):
    """Who `detect_chat` bound the notifier to (settings key `notifier.bound_to`).

    This exists so "who is this bot talking to" is answerable from the settings tree alone.
    A chat id on its own is a number nobody can check; a number with a name and a @username
    beside it is something the operator can either recognise or refuse.
    """

    user_id: int = Field(description="Telegram user id of the account the bot was bound to.")
    username: str = Field(default="", description="Their @handle without the @, or empty.")
    first_name: str = Field(default="", description="Their display name, or empty.")


class NotifierSettingsOut(OutModel):
    """Notifier section of GET /api/settings; bot_token is always the mask."""

    bot_token: str | None = None
    has_bot_token: bool = False
    chat_id: int | None = None
    enabled: bool = False
    types: NotifyTypes = Field(default_factory=NotifyTypes)
    admin_ids: list[int] = Field(
        default_factory=list,
        description="Telegram user ids allowed to bind and receive. Empty = not enforced.",
    )
    bound_to: NotifierBoundToOut | None = Field(
        default=None, description="The account the stored chat id was detected from, if known."
    )


class LimitsOut(OutModel):
    """The optional limits as stored; null and empty both mean "not evaluated"."""

    max_volume_per_trade: float | None = None
    max_open_positions: int | None = None
    max_daily_loss_percent: float | None = None
    symbol_allowlist: list[str] = Field(default_factory=list)
    trading_hours: list[TradingWindow] = Field(default_factory=list)


class LadderStatsOut(OutModel):
    """Last-N closed-ladder aggregates for the active source (server-computed)."""

    source_id: int | None = None
    source_title: str = ""
    window: int = LADDER_STATS_WINDOW
    sample_size: int = 0
    raw_sl_hits: int = 0
    be_stopouts: int = 0
    tp4_plus: int = 0
    clean_streak: int = 0
    suggested_mode: str = "be_after_cover"
    bootstrap: bool = True
    suggested_runner_tp_bias: int = 0
    blurb: str = ""


class GeneralSettingsOut(OutModel):
    """Non-credential defaults."""

    default_risk_percent: float = DEFAULT_RISK_PERCENT
    ladder_volume: float = LADDER_VOLUME
    locale: str = "en"
    orders_per_signal: int = ORDERS_PER_SIGNAL_DEFAULT
    order_lot_size: float = ORDER_LOT_SIZE_DEFAULT
    use_fixed_order_lots: bool = USE_FIXED_ORDER_LOTS_DEFAULT
    max_entry_drift_usd: float = MAX_ENTRY_DRIFT_USD_DEFAULT
    be_after_tp1: bool = BE_AFTER_TP1_DEFAULT
    reviewer_enabled: bool = REVIEWER_ENABLED_DEFAULT
    tag_filter_enabled: bool = TAG_FILTER_ENABLED_DEFAULT
    tag_filter_tag: str = TAG_FILTER_TAG_DEFAULT
    #: The text the editor shows. The built-in prompt when nothing custom is stored.
    system_prompt: str = ""
    #: The built-in prompt, so the editor can reset without losing the original.
    system_prompt_default: str = ""
    system_prompt_custom: bool = False
    ladder_stats: LadderStatsOut | None = None


class SettingsOut(OutModel):
    """GET /api/settings — the whole masked settings tree."""

    mt5: Mt5SettingsOut = Field(default_factory=Mt5SettingsOut)
    telegram: TelegramSettingsOut = Field(default_factory=TelegramSettingsOut)
    provider: ProviderSettingsOut = Field(default_factory=ProviderSettingsOut)
    notifier: NotifierSettingsOut = Field(default_factory=NotifierSettingsOut)
    limits: LimitsOut = Field(default_factory=LimitsOut)
    general: GeneralSettingsOut = Field(default_factory=GeneralSettingsOut)
    needs_reentry: list[str] = Field(default_factory=list)


class Mt5TestOut(OutModel):
    """POST /api/settings/mt5/test — the real account behind the credentials."""

    ok: bool
    login: int | None = None
    name: str = ""
    server: str = ""
    balance: float = 0.0
    equity: float = 0.0
    currency: str = ""
    leverage: int = 0
    margin_mode: str = ""
    message: str = ""


class NotifierTestOut(OutModel):
    """POST /api/settings/notifier/test — getMe on the operator's bot."""

    ok: bool
    bot_username: str = ""
    message: str = ""


class NotifierDetectOut(OutModel):
    """POST /api/settings/notifier/detect — chat id filled from an allowed incoming message.

    The identity fields are not decoration. With an empty ``admin_ids`` this route binds
    whoever messaged first, and the only defence against that being the wrong person is
    showing the operator who it was.
    """

    ok: bool
    chat_id: int | None = None
    chat_title: str = ""
    message: str = ""
    user_id: int | None = Field(default=None, description="Telegram user id of the bound sender.")
    username: str = ""
    first_name: str = ""
    is_admin: bool = Field(
        default=False, description="Whether that user id is on the admin allowlist."
    )
    enforced: bool = Field(
        default=False, description="Whether an allowlist was in force for this attempt."
    )


class NotifierUpdateOut(OutModel):
    """One recent sender in POST /api/settings/notifier/whoami."""

    user_id: int
    username: str = ""
    first_name: str = ""
    chat_id: int
    text: str = ""
    is_admin: bool = False


class NotifierWhoamiOut(OutModel):
    """POST /api/settings/notifier/whoami — everyone who has messaged the bot recently.

    The route an operator needs before they can fill in an allowlist at all: it reports
    numeric user ids, so nobody has to leave the application for a third-party lookup bot.
    """

    ok: bool
    updates: list[NotifierUpdateOut] = Field(default_factory=list)
    message: str = ""
    enforced: bool = False


# ---------------------------------------------------------------------------
# Telegram account
# ---------------------------------------------------------------------------


class TelegramCredentials(RequestModel):
    """POST /api/telegram/credentials — the my.telegram.org application credentials."""

    api_id: PositiveInt = Field(description="Application api_id, a positive integer.")
    api_hash: str | None = Field(
        default=None,
        description=(
            f"Application api_hash. Send null, omit the field, or echo back the mask {MASK!r} "
            "to leave the stored hash unchanged; an empty string clears it."
        ),
    )
    phone: PhoneStr | None = Field(
        default=None,
        description=(
            "Account phone in E.164, e.g. +14155552671. Optional: api_id and api_hash belong to "
            "the APPLICATION and are entered once, while the phone belongs to whichever account "
            "is being connected, so it may be supplied later without re-entering the keys."
        ),
    )

    @field_validator("api_hash", mode="before")
    @classmethod
    def _mask_means_unchanged(cls, value: str | None) -> str | None:
        return _unmask_secret(value)


class TelegramSignIn(RequestModel):
    """POST /api/telegram/sign-in — the login code Telegram just sent."""

    code: CodeStr = Field(description="Login code from the Telegram app or SMS.")


class TelegramPassword(RequestModel):
    """POST /api/telegram/password — the two-factor password."""

    password: NonEmptyStr = Field(description="Two-factor cloud password.")


class SourceSelect(RequestModel):
    """POST /api/telegram/source — pick the one active signal channel."""

    chat_id: int = Field(description="Dialog id from GET /api/telegram/dialogs.")


class SendCodeOut(OutModel):
    """POST /api/telegram/send-code."""

    ok: bool
    code_sent: bool = False
    message: str = ""


class SignInOut(OutModel):
    """POST /api/telegram/sign-in."""

    ok: bool
    needs_password: bool = False
    message: str = ""


class TelegramStatusOut(OutModel):
    """GET /api/telegram/status."""

    connected: bool = False
    authorized: bool = False
    user: TelegramUserOut | None = None
    source: SourceOut | None = None
    error: str | None = None


class DialogsOut(OutModel):
    """GET /api/telegram/dialogs."""

    dialogs: list[DialogOut] = Field(default_factory=list)


class SourceSelectOut(OutModel):
    """POST /api/telegram/source."""

    ok: bool
    source: SourceOut | None = None
    message: str = ""


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ProviderTestRequest(RequestModel):
    """POST /api/provider/test — list models for these connection details."""

    kind: ProviderKind
    base_url: str = Field(default="", description="Normalized the same way as the stored value.")
    api_key: str | None = Field(
        default=None,
        description=f"Null, omitted, or the mask {MASK!r} reuses the stored key.",
    )
    extra_headers: HeaderMap = Field(default_factory=dict)

    @field_validator("api_key", mode="before")
    @classmethod
    def _mask_means_unchanged(cls, value: str | None) -> str | None:
        return _unmask_secret(value)

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize(cls, value: str | None) -> str:
        return _normalized_base_url(value)


class ProviderProbeRequest(RequestModel):
    """POST /api/provider/probe — one throwaway request that settles vision and tools."""

    kind: ProviderKind
    base_url: str = Field(default="")
    api_key: str | None = Field(
        default=None,
        description=f"Null, omitted, or the mask {MASK!r} reuses the stored key.",
    )
    model: NonEmptyStr
    extra_headers: HeaderMap = Field(default_factory=dict)
    force: bool = Field(default=False, description="Ignore the cached verdict and probe again.")

    @field_validator("api_key", mode="before")
    @classmethod
    def _mask_means_unchanged(cls, value: str | None) -> str | None:
        return _unmask_secret(value)

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize(cls, value: str | None) -> str:
        return _normalized_base_url(value)


class ProviderTestOut(OutModel):
    """POST /api/provider/test."""

    ok: bool
    models: list[ModelInfoOut] = Field(default_factory=list)
    message: str = ""


class ProviderModelsOut(OutModel):
    """GET /api/provider/models — the last successful listing."""

    models: list[ModelInfoOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Trading — state and control
# ---------------------------------------------------------------------------


class ArmRequest(RequestModel):
    """POST /api/trading/arm — the operator's on/off switch."""

    armed: bool = Field(description="True lets write tools reach the broker.")


class KillSwitchRequest(RequestModel):
    """POST /api/trading/kill-switch — closes everything, then disarms."""

    confirm: Literal["KILL"] = Field(description='Must be the literal string "KILL".')


class ArmOut(OutModel):
    """POST /api/trading/arm."""

    ok: bool = True
    armed: bool


class KillSwitchOut(OutModel):
    """POST /api/trading/kill-switch."""

    ok: bool
    closed: NonNegativeInt = 0
    cancelled: NonNegativeInt = 0
    message: str = ""


class TradingStateOut(OutModel):
    """GET /api/trading/state — everything the cockpit chrome needs in one call."""

    armed: bool = False
    mt5: ComponentStatusOut = Field(default_factory=ComponentStatusOut)
    telegram: ComponentStatusOut = Field(default_factory=ComponentStatusOut)
    provider: ComponentStatusOut = Field(default_factory=ComponentStatusOut)
    margin_mode: str | None = None
    currency: str | None = None
    default_risk_percent: float = DEFAULT_RISK_PERCENT
    ladder_volume: float = LADDER_VOLUME
    source: SourceOut | None = None
    last_reconcile_at: datetime | None = None
    needs_reentry: list[str] = Field(default_factory=list)


class AccountOut(ToolOutModel):
    """GET /api/trading/account — the `get_account()` shape (BUILD §5.2)."""

    login: int | None = None
    name: str = ""
    server: str = ""
    currency: str = ""
    leverage: int = 0
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 0.0
    profit: float = Field(default=0.0, description="Floating P/L on open positions.")
    margin_mode: str = Field(default="netting", description='Literal "hedging" / "netting".')
    open_positions: int = 0
    pending_orders: int = 0
    today_profit: float = Field(
        default=0.0, description="Realized P/L since midnight broker-server time."
    )
    server_time: str | None = None


# ---------------------------------------------------------------------------
# Trading — live rows
# ---------------------------------------------------------------------------


class PositionOut(ToolOutModel):
    """One live MT5 position joined to the signal that created it."""

    ticket: int
    symbol: str = ""
    side: str = "buy"
    order_type: int = 0
    volume: float = 0.0
    price_open: float = 0.0
    price_current: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    magic: int = 0
    comment: str = ""
    external: bool = Field(default=False, description="Foreign magic: read-only, never touched.")
    digits: int = 0
    pip_size: float = 0.0
    sl_distance: DistanceOut | None = None
    tp_distance: DistanceOut | None = None
    trade_id: int | None = None
    run_id: str | None = None
    signal_ref: str | None = None
    signal_excerpt: str | None = None
    message_row_id: int | None = None
    permalink: str | None = None
    opened_at_ts: float | None = None
    ladder_role: str | None = None
    runner_mode: str | None = None
    suggested_runner_mode: str | None = None
    runner_tp_bias: int | None = None
    suggested_runner_tp_index: int | None = None
    chosen_runner_tp_index: int | None = None


class OrderOut(ToolOutModel):
    """One live MT5 pending order joined to the signal that created it."""

    ticket: int
    symbol: str = ""
    side: str = "buy"
    order_type: int = 0
    order_type_name: str = ""
    volume_current: float = 0.0
    volume_initial: float = 0.0
    price_open: float = 0.0
    price_current: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 0
    comment: str = ""
    external: bool = False
    digits: int = 0
    pip_size: float = 0.0
    sl_distance: DistanceOut | None = None
    tp_distance: DistanceOut | None = None
    trade_id: int | None = None
    run_id: str | None = None
    signal_ref: str | None = None
    signal_excerpt: str | None = None
    message_row_id: int | None = None
    permalink: str | None = None
    time_setup_ts: float | None = None
    expiration_ts: float | None = None


class PositionsOut(OutModel):
    """GET /api/trading/positions."""

    ok: bool = True
    positions: list[PositionOut] = Field(default_factory=list)


class OrdersOut(OutModel):
    """GET /api/trading/orders."""

    ok: bool = True
    orders: list[OrderOut] = Field(default_factory=list)


class CandleOut(OutModel):
    """One OHLC bar as the sidecar returns it."""

    time: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    tick_volume: int = 0
    spread: int = 0
    real_volume: int = 0


class CandlesQuery(RequestModel):
    """GET /api/trading/candles query parameters."""

    symbol: NonEmptyStr
    timeframe: TimeframeName = "M15"
    count: Annotated[int, Field(ge=1, le=MAX_CANDLES)] = 100


class CandlesOut(ToolOutModel):
    """GET /api/trading/candles."""

    ok: bool = True
    symbol: str = ""
    timeframe: str = ""
    count: int = 0
    candles: list[CandleOut] = Field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# Trading — the account's symbol table (CONTRACTS §18, GET /api/trading/symbols)
# ---------------------------------------------------------------------------


class SymbolsQuery(RequestModel):
    """GET /api/trading/symbols query parameters."""

    query: str = Field(
        default="",
        max_length=64,
        description=(
            "Case-insensitive filter on the broker's name and description. It also matches "
            "across a broker suffix, so XAUUSD finds XAUUSDr. Empty lists the whole table."
        ),
    )
    only_tradable: bool = Field(
        default=False,
        description=(
            "Keep only symbols the broker currently allows both directions on "
            "(trade_mode == SYMBOL_TRADE_MODE_FULL). Off by default: an operator who cannot "
            "see a close-only or disabled symbol cannot understand why it is missing."
        ),
    )
    limit: Annotated[int, Field(ge=1, le=SYMBOL_LIST_LIMIT_MAX)] = SYMBOL_LIST_LIMIT_DEFAULT


class SymbolRow(OutModel):
    """One row of the symbol picker: what the broker says about one instrument.

    Every field is read off ``symbol_info``; nothing here is a default standing in for a
    value the broker did not give. ``pip_size`` and ``pip_convention`` come from the same
    ``symbols.py`` rules the executor sizes trades with, so the number the operator reads in
    the picker is the number the model will be handed for that symbol.
    """

    name: str
    description: str = ""
    digits: int = 0
    point: float = 0.0
    pip_size: float = 0.0
    pip_convention: str = ""
    volume_min: float = 0.0
    volume_step: float = 0.0
    volume_max: float = 0.0
    stops_level_points: int = 0
    spread_points: int = 0
    tradable: bool = False
    #: The broker's own Market Watch group, e.g. "Raw\Forex" — what makes a table of
    #: several hundred symbols navigable. Empty when the broker publishes no path.
    path: str = ""
    currency_profit: str = ""


class SymbolsAccountOut(OutModel):
    """Which account the rows above came from, so a stale list is recognisable as stale."""

    login: int | None = None
    server: str = ""


class SymbolsOut(OutModel):
    """GET /api/trading/symbols — the connected account's tradable instruments."""

    ok: bool = True
    symbols: list[SymbolRow] = Field(default_factory=list)
    total: int = Field(
        default=0,
        description="How many symbols matched before `limit` was applied.",
    )
    truncated: bool = Field(
        default=False,
        description=(
            "True when `total` exceeded `limit` and rows were clipped. Reported honestly: a "
            "picker that silently showed the first N would tell the operator a symbol does "
            "not exist when it simply did not fit."
        ),
    )
    account: SymbolsAccountOut = Field(default_factory=SymbolsAccountOut)
    mt5_unavailable: bool = False
    message: str = ""


# ---------------------------------------------------------------------------
# Trading — manual override requests (the same arguments the tools take)
# ---------------------------------------------------------------------------


class ManualResolveSymbolRequest(RequestModel):
    """POST /api/trading/manual/resolve-symbol."""

    query: NonEmptyStr = Field(description="What the signal called it, e.g. gold.")


class ManualQuoteRequest(RequestModel):
    """POST /api/trading/manual/quote."""

    symbol: NonEmptyStr = Field(description="Exact broker symbol name.")


class ManualPriceFromDistanceRequest(RequestModel):
    """POST /api/trading/manual/price-from-distance."""

    symbol: NonEmptyStr
    anchor: PositiveFloat = Field(description="Price the distance is measured from.")
    direction: Direction
    distance: PositiveFloat
    unit: DistanceUnit


class ManualEvaluateEntryRequest(RequestModel):
    """POST /api/trading/manual/evaluate-entry — the late-entry instrument."""

    symbol: NonEmptyStr
    side: Side
    signal_entry: PositiveFloat = Field(description="Entry price as the channel posted it.")
    sl: PositiveFloat = Field(description="Intended stop, used to scale the drift.")
    tp: TpList = Field(default_factory=list)


class ManualOpenRequest(RequestModel):
    """POST /api/trading/manual/open — market order."""

    symbol: NonEmptyStr
    side: Side
    sl: NonNegativeFloat = Field(
        description=(
            "Stop loss price the channel posted. Zero opens an incomplete entry with no "
            "stop; the server does not invent one. A zero is accepted by the schema so it "
            "reaches the executor instead of failing validation."
        )
    )
    tp: TpList = Field(
        default_factory=list,
        description=(
            "Posted take-profit prices. Empty opens with tp = 0.0. Do not invent levels."
        ),
    )
    volume: PositiveFloat | None = Field(
        default=None, description="Lots. Mutually exclusive with risk_percent."
    )
    risk_percent: PositiveFloat | None = Field(
        default=None,
        description="Percent of balance to risk. No upper bound by design (BUILD §2).",
    )
    runner_mode: RunnerMode | None = Field(
        default=None,
        description=(
            "Ladder only. After cover: be_after_cover trails the runner to BE; "
            "hold_channel_sl leaves the channel stop. Omit to use suggested_mode."
        ),
    )
    runner_tp_index: int | None = Field(
        default=None,
        description=(
            "Ladder only. 1-based numbered-TP index for the runner. Omit to use the "
            "server suggestion; at most ±1 from suggested."
        ),
    )
    reason: ReasonStr = Field(description="Quoted in the notification and the history row.")


class ManualPendingRequest(RequestModel):
    """POST /api/trading/manual/pending — limit or stop order."""

    symbol: NonEmptyStr
    side: Side
    order_type: PendingKind
    price: PositiveFloat = Field(description="Trigger price of the pending order.")
    sl: NonNegativeFloat = Field(
        description=(
            "Stop loss price the channel posted. Zero places the order with no stop. "
            "A zero is accepted by the schema so it reaches the executor."
        )
    )
    tp: TpList = Field(default_factory=list)
    expiry_minutes: PositiveInt | None = Field(
        default=None, description="Null leaves the order good till cancelled."
    )
    volume: PositiveFloat | None = None
    risk_percent: PositiveFloat | None = Field(
        default=None, description="No upper bound by design (BUILD §2)."
    )
    runner_mode: RunnerMode | None = Field(
        default=None,
        description="Ladder only. Same as open: be_after_cover or hold_channel_sl.",
    )
    runner_tp_index: int | None = Field(
        default=None,
        description="Ladder only. Same as open: suggested numbered TP ±1.",
    )
    reason: ReasonStr


class ManualModifyPositionRequest(RequestModel):
    """POST /api/trading/manual/modify-position."""

    ticket: TicketInt
    sl: NonNegativeFloat | None = Field(
        default=None,
        description=(
            "New stop. Null leaves it as is. A zero reaches the guard, which refuses to remove "
            "a stop rather than failing validation."
        ),
    )
    tp: NonNegativeFloat | None = Field(
        default=None, description="New target; zero clears the target, which is legal."
    )
    reason: ReasonStr


class ManualModifyOrderRequest(RequestModel):
    """POST /api/trading/manual/modify-order."""

    ticket: TicketInt
    price: PositiveFloat | None = None
    sl: NonNegativeFloat | None = Field(
        default=None,
        description="Null leaves it as is; a zero reaches the guard that refuses stop removal.",
    )
    tp: NonNegativeFloat | None = None
    reason: ReasonStr


class ManualCloseRequest(RequestModel):
    """POST /api/trading/manual/close — full or partial."""

    ticket: TicketInt
    volume: PositiveFloat | None = Field(
        default=None, description="Null closes the whole position."
    )
    reason: ReasonStr


class ManualCancelRequest(RequestModel):
    """POST /api/trading/manual/cancel."""

    ticket: TicketInt
    reason: ReasonStr


# ---------------------------------------------------------------------------
# Trading — tool result payloads
# ---------------------------------------------------------------------------


class SymbolCandidateOut(ToolOutModel):
    """One ranked candidate from `resolve_symbol`; the `SymbolRef.to_dict()` shape."""

    name: str
    description: str = ""
    digits: int = 0
    point: float = 0.0
    pip_size: float = 0.0
    pip_convention: str = ""
    volume_min: float = 0.0
    volume_step: float = 0.0
    volume_max: float = 0.0
    stops_level_points: int = 0
    freeze_level_points: int = 0
    spread_points: int = 0
    trade_tick_size: float = 0.0
    trade_tick_value: float = 0.0
    trade_tick_value_loss: float = 0.0
    contract_size: float = 0.0
    currency_profit: str = ""
    currency_margin: str = ""
    trade_mode: int = 0
    trade_exemode: int = 0
    filling_mode: int = 0
    visible: bool = False
    tradable: bool = False


class ResolveSymbolOut(ToolOutModel):
    """`resolve_symbol` — up to MAX_SYMBOL_CANDIDATES ranked names, never one silent guess."""

    ok: bool
    query: str = ""
    candidates: list[SymbolCandidateOut] = Field(default_factory=list)
    hint: list[str] = Field(
        default_factory=list, description="Closest names by edit distance when nothing matched."
    )
    message: str = ""

    @field_validator("hint", mode="before")
    @classmethod
    def _hint_as_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value


class QuoteOut(ToolOutModel):
    """`get_quote` — the `Quote.to_dict()` shape."""

    symbol: str
    bid: float = 0.0
    ask: float = 0.0
    spread_points: int = 0
    spread_pips: float = 0.0
    time_ts: float = 0.0
    server_time: str = ""


class PriceFromDistanceOut(ToolOutModel):
    """`price_from_distance` — an absolute price plus the convention that produced it."""

    price: float
    convention: str = ""
    anchor: float = 0.0
    distance: DistanceOut = Field(default_factory=DistanceOut)


class EvaluateEntryOut(ToolOutModel):
    """`evaluate_entry` — how much of the stop the delay already ate (BUILD §5.2)."""

    ok: bool = True
    symbol: str = ""
    side: str = ""
    signal_entry: float = 0.0
    sl: float = 0.0
    tp: list[float] = Field(default_factory=list)
    bid: float = 0.0
    ask: float = 0.0
    current_price: float = 0.0
    distance_from_signal_entry: DistanceOut = Field(default_factory=DistanceOut)
    distance_as_fraction_of_sl: float = 0.0
    rr_now: float | None = None
    rr_as_posted: float | None = None
    signal_age_seconds: float | None = None
    stale: bool = False
    volume_now: float | None = None
    volume_as_posted: float | None = None
    risk_percent: float = 0.0
    message: str = ""


class WriteResultOut(ToolOutModel):
    """The envelope every write tool returns; a broker rejection is ok:false, never an exception."""

    ok: bool
    message: str = ""
    ticket: int | None = None
    tickets: list[int] = Field(default_factory=list)
    filled_volume: float | None = None
    fill_price: float | None = None
    slippage_points: float | None = None
    retcode: int | None = None
    retcode_name: str | None = None


# ---------------------------------------------------------------------------
# Signals, runs and steps
# ---------------------------------------------------------------------------


class ImageOut(OutModel):
    """One stored image of a signal message."""

    id: int
    url: str = Field(description="Backend route serving the bytes: /api/telegram/image/{id}.")
    width: int | None = None
    height: int | None = None


class SignalOut(OutModel):
    """One row of the signal feed, skipped messages included."""

    id: int
    source_id: int
    message_id: int
    revision: int = 1
    kind: str = "text"
    status: str = "pending"
    skip_reason: str | None = None
    catchup: bool = False
    text: str = ""
    permalink: str | None = None
    posted_at: datetime | None = None
    received_at: datetime | None = None
    image_count: int = 0
    images: list[ImageOut] = Field(default_factory=list)
    reply_to_message_id: int | None = None
    signal_ref: str | None = None
    deleted_at: datetime | None = None
    runs_count: int = 0
    trades_count: int = 0


class RunOut(OutModel):
    """One agent run over one message."""

    id: str
    source_id: int | None = None
    message_row_id: int | None = None
    kind: str = "signal"
    status: str = "running"
    decision: str | None = None
    summary: str | None = None
    error: str | None = None
    provider_kind: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    iterations: int = 0
    armed_at_start: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunStepOut(OutModel):
    """One persisted step of a run: thinking, text, a tool call, its result, or an error."""

    id: int
    run_id: str
    idx: int = 0
    type: str = "text"
    name: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    ok: bool | None = None
    created_at: datetime | None = None


class TradeOut(OutModel):
    """A faithful projection of one `trades` row; live distances are computed elsewhere."""

    id: int
    source_id: int | None = None
    run_id: str | None = None
    message_row_id: int | None = None
    signal_ref: str | None = None
    ticket: int
    kind: str = "position"
    symbol: str = ""
    side: str = "buy"
    order_type: int = 0
    volume: float = 0.0
    requested_volume: float | None = None
    entry_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    status: str = "open"
    magic: int = 0
    comment: str = ""
    external: bool = False
    split_index: int = 0
    split_count: int = 1
    unmanaged_tps: list[float] = Field(default_factory=list)
    ladder_role: str | None = None
    runner_mode: str | None = None
    suggested_runner_mode: str | None = None
    runner_tp_bias: int | None = None
    suggested_runner_tp_index: int | None = None
    chosen_runner_tp_index: int | None = None
    open_reason: str | None = None
    close_cause: str | None = None
    close_price: float | None = None
    profit: float | None = None
    profit_points: float | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    updated_at: datetime | None = None


class SignalsQuery(RequestModel):
    """GET /api/trading/signals query parameters."""

    limit: Annotated[int, Field(ge=1, le=500)] = 50
    status: MessageStatus | None = None


class RunsQuery(RequestModel):
    """GET /api/trading/runs query parameters."""

    limit: Annotated[int, Field(ge=1, le=500)] = 50


class SignalsOut(OutModel):
    """GET /api/trading/signals."""

    signals: list[SignalOut] = Field(default_factory=list)


class SignalDetailOut(OutModel):
    """GET /api/trading/signals/{id}."""

    signal: SignalOut
    runs: list[RunOut] = Field(default_factory=list)
    trades: list[TradeOut] = Field(default_factory=list)


class RunsOut(OutModel):
    """GET /api/trading/runs."""

    runs: list[RunOut] = Field(default_factory=list)


class RunDetailOut(OutModel):
    """GET /api/trading/runs/{run_id}."""

    run: RunOut
    steps: list[RunStepOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class HistoryQuery(RequestModel):
    """GET /api/history/trades query parameters."""

    date_from: datetime | None = None
    date_to: datetime | None = None
    symbol: str | None = None
    limit: Annotated[int, Field(ge=1, le=1000)] = 200


class AggregatesQuery(RequestModel):
    """GET /api/history/aggregates query parameters."""

    source_id: PositiveInt | None = None


class HistoryTradesOut(OutModel):
    """GET /api/history/trades."""

    trades: list[TradeOut] = Field(default_factory=list)


class SymbolAggregateOut(OutModel):
    """Per-symbol slice of the history aggregates."""

    symbol: str
    count: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_profit: float = 0.0
    avg_r: float = 0.0


class AggregatesOut(OutModel):
    """GET /api/history/aggregates."""

    win_rate: float = 0.0
    avg_r: float = 0.0
    total_profit: float = 0.0
    count: int = 0
    by_symbol: list[SymbolAggregateOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# WebSocket /api/stream
# ---------------------------------------------------------------------------


class StreamEventOut(OutModel):
    """The raw event envelope the bus emits."""

    type: str
    ts: float
    seq: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class StreamHelloData(OutModel):
    """Payload of the hello frame: enough state to render without a REST round trip."""

    status: StatusMapOut = Field(default_factory=StatusMapOut)
    armed: bool = False
    recent: list[StreamEventOut] = Field(default_factory=list)


class StreamHello(OutModel):
    """First frame the server sends on every WebSocket connection."""

    type: Literal["hello"] = "hello"
    ts: float
    data: StreamHelloData = Field(default_factory=StreamHelloData)


class StreamPing(RequestModel):
    """The only frame a client may send."""

    type: Literal["ping"]


class StreamPong(OutModel):
    """The reply to a ping."""

    type: Literal["pong"] = "pong"
    ts: float
