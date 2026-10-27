"""Central configuration for LV Copytrader: every tunable constant lives in this one module.

Two import styles are supported and both read the same values:

    from app.config import settings ;  settings.MAGIC_NUMBER
    from app.config import MAGIC_NUMBER

Secrets (``SECRET_KEY``, ``MT5_PASSWORD``, ``SIDECAR_TOKEN``) are deliberately *not*
re-exported as module-level aliases: they can be bootstrapped or rotated at runtime, so
they must be read through ``settings`` where the current value is visible.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --------------------------------------------------------------------------------------
# Filesystem anchors. In development this file is <repo>/backend/app/config.py; inside the
# container the backend is copied to /app so it is /app/app/config.py and the repo root
# collapses to "/", which is why the container fallback is the explicit /app/.env below.
# --------------------------------------------------------------------------------------
_APP_DIR: Final[Path] = Path(__file__).resolve().parent
_BACKEND_DIR: Final[Path] = _APP_DIR.parent
_REPO_ROOT: Final[Path] = _BACKEND_DIR.parent
_CONTAINER_ENV_PATH: Final[str] = "/app/.env"
_ENV_FILE_VAR: Final[str] = "SP_ENV_FILE"

_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
)
_MARGIN_MODES: Final[frozenset[str]] = frozenset({"hedging", "netting"})
_EMPTY_MARGIN_MODES: Final[frozenset[str]] = frozenset({"", "none", "null", "auto", "detect"})


def _env_file_candidates() -> tuple[str, ...]:
    """Return the .env paths pydantic-settings should load, lowest priority first."""
    candidates: list[str] = [".env", str(_BACKEND_DIR / ".env"), str(_REPO_ROOT / ".env")]
    override = os.environ.get(_ENV_FILE_VAR, "").strip()
    if override:
        candidates.append(str(Path(override).expanduser()))
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered)


def _parse_mapping(value: Any, *, name: str) -> dict[str, Any]:
    """Accept a dict, a JSON object string, or a 'KEY=VALUE,KEY=VALUE' string as a mapping."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        if text[0] in "{[":
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} is not valid JSON: {exc}") from exc
            if not isinstance(decoded, dict):
                raise ValueError(f"{name} must be a JSON object, got {type(decoded).__name__}")
            return decoded
        pairs: dict[str, Any] = {}
        for chunk in text.replace(";", ",").split(","):
            item = chunk.strip()
            if not item:
                continue
            if "=" in item:
                key, _, raw = item.partition("=")
            elif ":" in item:
                key, _, raw = item.partition(":")
            else:
                raise ValueError(
                    f"{name} entry {item!r} must be JSON or 'KEY=VALUE' separated by commas"
                )
            pairs[key.strip()] = raw.strip()
        return pairs
    raise ValueError(f"{name} must be a mapping or a JSON object string, got {type(value).__name__}")


def _parse_float_sequence(value: Any, *, name: str) -> tuple[float, ...]:
    """Accept a sequence, a JSON array string, or a comma-separated string of numbers."""
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text[0] == "[":
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} is not valid JSON: {exc}") from exc
        else:
            value = [chunk for chunk in text.replace(";", ",").split(",") if chunk.strip()]
    if isinstance(value, (list, tuple)):
        try:
            return tuple(float(item) for item in value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must contain only numbers: {exc}") from exc
    raise ValueError(f"{name} must be a list of numbers, got {type(value).__name__}")


class Settings(BaseSettings):
    """Every environment variable and every tunable constant LV Copytrader reads."""

    model_config = SettingsConfigDict(
        env_file=_env_file_candidates(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- environment-backed deployment values ------------------------------------------
    SECRET_KEY: str | None = None
    MT5_SERVER: str = "Exness-MT5Trial15"
    MT5_LOGIN: int | None = None
    MT5_PASSWORD: str = ""
    MT5_INSTALLER_URL: str = "https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe"
    # Telegram seed. api_id/api_hash come from my.telegram.org and are issued ONCE per
    # application, so they belong to the deployment rather than to the account: the operator
    # can sign a different account in later without ever re-entering them. TELEGRAM_SESSION
    # carries an existing Telethon StringSession, which is what lets a fresh clone come up
    # already signed in instead of asking for a login code. All three seed the database only
    # when it holds no value yet, so the UI stays authoritative afterwards.
    TELEGRAM_API_ID: int | None = None
    TELEGRAM_API_HASH: str = ""
    TELEGRAM_SESSION: str = ""
    TELEGRAM_PHONE: str = ""
    SIDECAR_URL: str = "http://mt5-sidecar:18812"
    SIDECAR_TOKEN: str = "change-me"
    DATA_DIR: str = "/data"
    LOG_LEVEL: str = "INFO"
    #: Credentials for private APIs. Missing credentials fail closed by default.
    #: Explicit unauthenticated development mode is for isolated localhost use only. Set BOTH before exposing
    #: the app through a tunnel, a proxy or a forwarded port: the dashboard carries the ARM
    #: switch, the kill switch and manual order entry.
    SP_AUTH_USER: str = ""
    SP_AUTH_PASSWORD: str = ""
    SP_ALLOW_UNAUTHENTICATED: bool = False
    SP_ALLOWED_HOSTS: str = ""
    FRONTEND_DIST: str = "/app/static"

    # --- sidecar RPC transport ---------------------------------------------------------
    RPC_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0.0)
    RPC_CONNECT_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0.0)
    RPC_HEALTH_SECONDS: float = Field(default=10.0, gt=0.0)

    # --- order semantics ---------------------------------------------------------------
    MAGIC_NUMBER: int = Field(default=770077, ge=0)
    DEVIATION_POINTS: int = Field(default=20, ge=0)
    REQUOTE_RETRIES: int = Field(default=2, ge=0)

    # --- symbols -----------------------------------------------------------------------
    SYMBOL_CACHE_SECONDS: int = Field(default=3600, ge=1)
    MAX_SYMBOL_CANDIDATES: int = Field(default=8, ge=1)
    SYMBOL_LIST_LIMIT_DEFAULT: int = Field(default=300, ge=1)
    SYMBOL_LIST_LIMIT_MAX: int = Field(default=1000, ge=1)
    PIP_OVERRIDES: dict[str, float] = Field(default_factory=dict)

    # --- sizing and margin -------------------------------------------------------------
    DEFAULT_RISK_PERCENT: float = Field(default=1.0, gt=0.0)
    MARGIN_HEADROOM: float = Field(default=0.8, gt=0.0, le=1.0)
    FORCE_MARGIN_MODE: str | None = None
    MAX_TP_SPLITS: int = Field(default=3, ge=1)
    #: How many take-profit prices ``open_market`` / ``place_pending`` / ``evaluate_entry``
    #: will accept in one ``tp`` list. Ladders (4+ rungs) need this to be larger than
    #: ``MAX_TP_SPLITS`` so the far targets survive long enough to pick the runner.
    MAX_TP_INPUT: int = Field(default=12, ge=1)
    #: Fixed total lots for a multi-TP ladder (split ~50/50 into two legs). Not a risk %.
    LADDER_VOLUME: float = Field(default=0.10, gt=0.0)
    #: Points past entry used when locking a ladder runner to break-even after the cover TP.
    LADDER_BE_BUFFER_POINTS: int = Field(default=2, ge=1)
    #: How many fully-closed ladders per source decide suggested runner_mode.
    LADDER_STATS_WINDOW: int = Field(default=5, ge=1)

    # --- the stop-loss invariant -------------------------------------------------------
    SL_WATCHDOG_SECONDS: int = Field(default=60, ge=1)
    SL_FALLBACK_ATTEMPTS: int = Field(default=3, ge=1)
    SL_FALLBACK_POINTS: int = Field(default=500, ge=1)

    # --- Telegram ingest ---------------------------------------------------------------
    INGEST_QUEUE_MAX: int = Field(default=50, ge=1)
    #: How many ingest workers may run an agent at once. Broker writes stay serial
    #: underneath this; the cap is 3 so a burst cannot fan out without bound.
    INGEST_WORKERS: int = Field(default=3, ge=1, le=3)
    #: Hard cap on the channel playbook injected into triage. The reviewer clips to this.
    CHANNEL_BRIEF_MAX_LINES: int = Field(default=60, ge=20, le=60)
    #: How many days of Telegram history the reviewer fetches when the database is thin.
    #: Clamped to 3–4 in the reviewer even if a caller asks for more.
    CHANNEL_BRIEF_HISTORY_DAYS: int = Field(default=4, ge=3, le=4)
    #: Fewer stored posts than this, or a span shorter than two days, counts as thin.
    CHANNEL_BRIEF_MIN_MESSAGES: int = Field(default=12, ge=1)
    #: Output budget for the one-shot channel study. Not the full agent's MAX_TOKENS.
    CHANNEL_REVIEWER_MAX_TOKENS: int = Field(default=2048, ge=256)
    CATCHUP_LIMIT: int = Field(default=20, ge=0)
    TELEGRAM_FLOOD_SLEEP_THRESHOLD: int = Field(default=60, ge=0)

    # --- the agent loop ----------------------------------------------------------------
    #: Safety cap so a run that never calls finish still ends. Raised so a ladder is not
    #: cut off mid tool loop. Not a latency knob — do not lower it to make triage faster.
    MAX_TOOL_ITERATIONS: int = Field(default=32, ge=1)
    #: Output budget on every full-agent provider request. Anthropic requires the field.
    #: High on purpose so thinking models are not truncated. The yes/no triage call does
    #: not use this value; it has its own TRIAGE_MAX_TOKENS. The Settings key
    #: provider.max_tokens overrides this when the operator has saved one.
    MAX_TOKENS: int = Field(default=32768, ge=1)
    #: Budget for the pre-loop yes/no classifier only. Independent of MAX_TOKENS.
    TRIAGE_MAX_TOKENS: int = Field(default=256, ge=16)
    #: How close a parsed metal entry (XAU/XAG/XPT/XPD, in price units — dollars on
    #: XAUUSD) must sit to the live fill before any open is allowed. 7.0 is seven
    #: dollars. Farther than this, open_market and place_pending are refused on the
    #: fast path and on the full agent. The word "now" does not widen the band when
    #: a price is printed. A post older than STALE_SIGNAL_SECONDS is refused even
    #: inside the band. Either failure is enough.
    FAST_ENTRY_METAL_MARKET_DISTANCE: float = Field(default=7.0, gt=0.0)
    #: Same rule for every other symbol, measured in that symbol's pips. 5 pips on
    #: EURUSD is 0.0005. Metals ignore this and use the dollar distance above.
    FAST_ENTRY_DEFAULT_MARKET_PIPS: float = Field(default=5.0, gt=0.0)
    MAX_IMAGES_PER_RUN: int = Field(default=4, ge=0)
    STALE_SIGNAL_SECONDS: int = Field(default=600, ge=0)
    CONTEXT_SIGNALS: int = Field(default=10, ge=0)
    RECENT_SIGNALS_TOOL_MAX: int = Field(default=25, ge=1)

    # --- images ------------------------------------------------------------------------
    IMAGE_MAX_EDGE: int = Field(default=1568, ge=64)
    IMAGE_JPEG_QUALITY: int = Field(default=85, ge=1, le=100)
    IMAGE_MIN_QUALITY: int = Field(default=40, ge=1, le=100)
    IMAGE_MAX_BYTES: int = Field(default=3_500_000, ge=1)

    # --- provider ----------------------------------------------------------------------
    PROBE_CACHE_HOURS: int = Field(default=24, ge=0)
    PROVIDER_TIMEOUT_SECONDS: float = Field(default=180.0, gt=0.0)

    # --- notifications -----------------------------------------------------------------
    NOTIFY_RETRY_DELAYS: tuple[float, ...] = (1.0, 4.0)
    NOTIFY_DETECT_SECONDS: int = Field(default=60, ge=1)
    NOTIFY_POLL_SECONDS: float = Field(default=2.0, gt=0.0)

    # --- dashboard and storage ---------------------------------------------------------
    ACCOUNT_TICK_SECONDS: float = Field(default=3.0, gt=0.0)
    EVENT_QUEUE_MAX: int = Field(default=500, ge=1)
    MAX_CANDLES: int = Field(default=200, ge=1)
    DB_ECHO: bool = False

    # --- validators --------------------------------------------------------------------
    @field_validator("PIP_OVERRIDES", mode="before")
    @classmethod
    def _validate_pip_overrides(cls, value: Any) -> dict[str, float]:
        """Accept a dict, JSON object or KEY=VALUE string and normalise it to upper-case pip sizes."""
        raw = _parse_mapping(value, name="PIP_OVERRIDES")
        parsed: dict[str, float] = {}
        for key, item in raw.items():
            symbol = str(key).strip().upper()
            if not symbol:
                raise ValueError("PIP_OVERRIDES has an entry with an empty symbol name")
            try:
                size = float(item)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"PIP_OVERRIDES[{symbol}] must be a number, got {item!r}") from exc
            if size <= 0.0:
                raise ValueError(f"PIP_OVERRIDES[{symbol}] must be greater than zero, got {size}")
            parsed[symbol] = size
        return parsed

    @field_validator("NOTIFY_RETRY_DELAYS", mode="before")
    @classmethod
    def _validate_notify_retry_delays(cls, value: Any) -> tuple[float, ...]:
        """Accept a list, JSON array or comma-separated string of retry delays in seconds."""
        delays = _parse_float_sequence(value, name="NOTIFY_RETRY_DELAYS")
        for delay in delays:
            if delay < 0.0:
                raise ValueError(f"NOTIFY_RETRY_DELAYS must not contain a negative delay: {delay}")
        return delays

    @field_validator("FORCE_MARGIN_MODE", mode="before")
    @classmethod
    def _validate_force_margin_mode(cls, value: Any) -> str | None:
        """Accept 'hedging', 'netting' or an empty value meaning 'use the detected mode'."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("FORCE_MARGIN_MODE must be 'hedging', 'netting' or empty")
        mode = value.strip().lower()
        if mode in _EMPTY_MARGIN_MODES:
            return None
        if mode not in _MARGIN_MODES:
            raise ValueError(
                f"FORCE_MARGIN_MODE must be 'hedging', 'netting' or empty, got {value!r}"
            )
        return mode

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _validate_log_level(cls, value: Any) -> str:
        """Upper-case the log level and reject anything the logging module cannot use."""
        level = str(value).strip().upper()
        if level not in _LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL must be one of {', '.join(sorted(_LOG_LEVELS))}, got {value!r}"
            )
        return level

    @field_validator("SIDECAR_URL", mode="before")
    @classmethod
    def _validate_sidecar_url(cls, value: Any) -> str:
        """Strip whitespace and any trailing slash so the RPC path is joined cleanly."""
        url = str(value).strip().rstrip("/")
        if not url:
            raise ValueError("SIDECAR_URL must not be empty")
        return url

    @field_validator("DATA_DIR", "FRONTEND_DIST", mode="before")
    @classmethod
    def _validate_directory(cls, value: Any) -> str:
        """Expand a leading ~ and strip whitespace from a directory path."""
        path = str(value).strip()
        if not path:
            raise ValueError("a directory setting must not be empty")
        return str(Path(path).expanduser())

    # --- derived paths -----------------------------------------------------------------
    @property
    def db_path(self) -> str:
        """Absolute path of the SQLite database file."""
        return str(Path(self.DATA_DIR) / "signalpilot.db")

    @property
    def db_url(self) -> str:
        """SQLAlchemy async URL for the SQLite database."""
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def image_dir(self) -> str:
        """Directory holding images downloaded from the signal channel."""
        return str(Path(self.DATA_DIR) / "images")

    @property
    def telethon_session_dir(self) -> str:
        """Directory Telethon may use for any on-disk session material."""
        return str(Path(self.DATA_DIR) / "telethon")

    @property
    def env_file_path(self) -> str:
        """Path of the .env file to read and to write the bootstrapped SECRET_KEY into."""
        override = os.environ.get(_ENV_FILE_VAR, "").strip()
        if override:
            return str(Path(override).expanduser())
        for candidate in (_REPO_ROOT / ".env", _BACKEND_DIR / ".env"):
            if candidate.is_file():
                return str(candidate)
        if (_REPO_ROOT / "backend").is_dir():
            return str(_REPO_ROOT / ".env")
        return _CONTAINER_ENV_PATH

    def ensure_dirs(self) -> None:
        """Create the data, image and Telethon session directories if they do not exist."""
        for directory in (self.DATA_DIR, self.image_dir, self.telethon_session_dir):
            Path(directory).mkdir(parents=True, exist_ok=True)


settings = Settings()

# --------------------------------------------------------------------------------------
# Module-level aliases so `from app.config import MAGIC_NUMBER` works alongside
# `settings.MAGIC_NUMBER`. Secrets are omitted on purpose (see the module docstring).
# --------------------------------------------------------------------------------------
MT5_SERVER: str = settings.MT5_SERVER
MT5_LOGIN: int | None = settings.MT5_LOGIN
MT5_INSTALLER_URL: str = settings.MT5_INSTALLER_URL
SIDECAR_URL: str = settings.SIDECAR_URL
DATA_DIR: str = settings.DATA_DIR
LOG_LEVEL: str = settings.LOG_LEVEL
FRONTEND_DIST: str = settings.FRONTEND_DIST

RPC_TIMEOUT_SECONDS: float = settings.RPC_TIMEOUT_SECONDS
RPC_CONNECT_TIMEOUT_SECONDS: float = settings.RPC_CONNECT_TIMEOUT_SECONDS
RPC_HEALTH_SECONDS: float = settings.RPC_HEALTH_SECONDS

MAGIC_NUMBER: int = settings.MAGIC_NUMBER
DEVIATION_POINTS: int = settings.DEVIATION_POINTS
REQUOTE_RETRIES: int = settings.REQUOTE_RETRIES

SYMBOL_CACHE_SECONDS: int = settings.SYMBOL_CACHE_SECONDS
MAX_SYMBOL_CANDIDATES: int = settings.MAX_SYMBOL_CANDIDATES
SYMBOL_LIST_LIMIT_DEFAULT: int = settings.SYMBOL_LIST_LIMIT_DEFAULT
SYMBOL_LIST_LIMIT_MAX: int = settings.SYMBOL_LIST_LIMIT_MAX
PIP_OVERRIDES: dict[str, float] = settings.PIP_OVERRIDES

DEFAULT_RISK_PERCENT: float = settings.DEFAULT_RISK_PERCENT
MARGIN_HEADROOM: float = settings.MARGIN_HEADROOM
FORCE_MARGIN_MODE: str | None = settings.FORCE_MARGIN_MODE
MAX_TP_SPLITS: int = settings.MAX_TP_SPLITS
MAX_TP_INPUT: int = settings.MAX_TP_INPUT
LADDER_VOLUME: float = settings.LADDER_VOLUME
LADDER_BE_BUFFER_POINTS: int = settings.LADDER_BE_BUFFER_POINTS
LADDER_STATS_WINDOW: int = settings.LADDER_STATS_WINDOW

SL_WATCHDOG_SECONDS: int = settings.SL_WATCHDOG_SECONDS
SL_FALLBACK_ATTEMPTS: int = settings.SL_FALLBACK_ATTEMPTS
SL_FALLBACK_POINTS: int = settings.SL_FALLBACK_POINTS

INGEST_QUEUE_MAX: int = settings.INGEST_QUEUE_MAX
INGEST_WORKERS: int = settings.INGEST_WORKERS
CHANNEL_BRIEF_MAX_LINES: int = settings.CHANNEL_BRIEF_MAX_LINES
CHANNEL_BRIEF_HISTORY_DAYS: int = settings.CHANNEL_BRIEF_HISTORY_DAYS
CHANNEL_BRIEF_MIN_MESSAGES: int = settings.CHANNEL_BRIEF_MIN_MESSAGES
CHANNEL_REVIEWER_MAX_TOKENS: int = settings.CHANNEL_REVIEWER_MAX_TOKENS
CATCHUP_LIMIT: int = settings.CATCHUP_LIMIT
TELEGRAM_FLOOD_SLEEP_THRESHOLD: int = settings.TELEGRAM_FLOOD_SLEEP_THRESHOLD

MAX_TOOL_ITERATIONS: int = settings.MAX_TOOL_ITERATIONS
MAX_TOKENS: int = settings.MAX_TOKENS
TRIAGE_MAX_TOKENS: int = settings.TRIAGE_MAX_TOKENS
FAST_ENTRY_METAL_MARKET_DISTANCE: float = settings.FAST_ENTRY_METAL_MARKET_DISTANCE
FAST_ENTRY_DEFAULT_MARKET_PIPS: float = settings.FAST_ENTRY_DEFAULT_MARKET_PIPS
MAX_IMAGES_PER_RUN: int = settings.MAX_IMAGES_PER_RUN
STALE_SIGNAL_SECONDS: int = settings.STALE_SIGNAL_SECONDS
CONTEXT_SIGNALS: int = settings.CONTEXT_SIGNALS
RECENT_SIGNALS_TOOL_MAX: int = settings.RECENT_SIGNALS_TOOL_MAX

IMAGE_MAX_EDGE: int = settings.IMAGE_MAX_EDGE
IMAGE_JPEG_QUALITY: int = settings.IMAGE_JPEG_QUALITY
IMAGE_MIN_QUALITY: int = settings.IMAGE_MIN_QUALITY
IMAGE_MAX_BYTES: int = settings.IMAGE_MAX_BYTES

PROBE_CACHE_HOURS: int = settings.PROBE_CACHE_HOURS
PROVIDER_TIMEOUT_SECONDS: float = settings.PROVIDER_TIMEOUT_SECONDS

NOTIFY_RETRY_DELAYS: tuple[float, ...] = settings.NOTIFY_RETRY_DELAYS
NOTIFY_DETECT_SECONDS: int = settings.NOTIFY_DETECT_SECONDS
NOTIFY_POLL_SECONDS: float = settings.NOTIFY_POLL_SECONDS

ACCOUNT_TICK_SECONDS: float = settings.ACCOUNT_TICK_SECONDS
EVENT_QUEUE_MAX: int = settings.EVENT_QUEUE_MAX
MAX_CANDLES: int = settings.MAX_CANDLES
DB_ECHO: bool = settings.DB_ECHO

# --------------------------------------------------------------------------------------
# The parameter table the README publishes: (name, default, what it controls).
# --------------------------------------------------------------------------------------
PARAMETERS: list[tuple[str, object, str]] = [
    (
        "SECRET_KEY",
        None,
        "Fernet key that encrypts every secret stored in the database; generated on first "
        "start and written to .env when it is missing.",
    ),
    (
        "MT5_SERVER",
        "Exness-MT5Trial15",
        "Broker server name the MT5 terminal logs into; seeds the database setting the "
        "operator can later change in the UI.",
    ),
    (
        "MT5_LOGIN",
        None,
        "MT5 account number used for the initial login, so the MT5 status dot can go green "
        "from .env alone without any interactive step.",
    ),
    (
        "MT5_PASSWORD",
        "",
        "Password for that MT5 account; it is rendered into the terminal ini inside the "
        "sidecar and is never logged or returned by any endpoint.",
    ),
    (
        "MT5_INSTALLER_URL",
        "https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe",
        "URL the sidecar downloads the MT5 installer from; point it at the broker-branded "
        "installer when the generic one does not list your server.",
    ),
    (
        "TELEGRAM_API_ID",
        None,
        "Telegram application api_id from my.telegram.org; seeds the database so the "
        "operator never re-enters it when switching accounts.",
    ),
    (
        "TELEGRAM_API_HASH",
        "",
        "Telegram application api_hash paired with TELEGRAM_API_ID; stored encrypted and "
        "returned by every endpoint as the mask.",
    ),
    (
        "TELEGRAM_SESSION",
        "",
        "Existing Telethon StringSession; set it to bring a clone up already signed in, "
        "skipping the interactive login code. Stored encrypted.",
    ),
    (
        "TELEGRAM_PHONE",
        "",
        "Account phone in E.164 that the login flow starts from; convenience only, the UI "
        "can change it.",
    ),
    (
        "SIDECAR_URL",
        "http://mt5-sidecar:18812",
        "Base URL of the Wine sidecar's JSON-RPC endpoint on the compose network; the port "
        "is deliberately not published to the host.",
    ),
    (
        "SIDECAR_TOKEN",
        "change-me",
        "Shared secret sent as the X-SP-Token header on every RPC call and compared with "
        "hmac.compare_digest by the sidecar.",
    ),
    (
        "DATA_DIR",
        "/data",
        "Directory holding the SQLite database, downloaded signal images and any Telethon "
        "session material; mount it as a volume to survive restarts.",
    ),
    (
        "LOG_LEVEL",
        "INFO",
        "Logging verbosity for the backend; accepts the standard logging level names and is "
        "upper-cased before use.",
    ),
    (
        "FRONTEND_DIST",
        "/app/static",
        "Directory of the built Vite bundle that FastAPI serves as the dashboard.",
    ),
    (
        "RPC_TIMEOUT_SECONDS",
        30.0,
        "How long a single RPC call to the MT5 sidecar may take before it is treated as a "
        "transport failure.",
    ),
    (
        "RPC_CONNECT_TIMEOUT_SECONDS",
        10.0,
        "How long the RPC client waits to establish a connection to the sidecar.",
    ),
    (
        "RPC_HEALTH_SECONDS",
        10.0,
        "Interval between sidecar ping calls that drive the MT5 status dot.",
    ),
    (
        "MAGIC_NUMBER",
        770077,
        "Magic number stamped on every order this system sends; positions carrying a "
        "different magic are shown read-only as external and are never touched.",
    ),
    (
        "DEVIATION_POINTS",
        20,
        "Slippage tolerance in points allowed on every market order and market close.",
    ),
    (
        "REQUOTE_RETRIES",
        2,
        "How many times a market order is re-quoted and resent after a requote (10004) or "
        "price-changed (10020) retcode before the failure is reported to the model.",
    ),
    (
        "SYMBOL_CACHE_SECONDS",
        3600,
        "How long the cached broker symbol list is reused before it is refreshed.",
    ),
    (
        "MAX_SYMBOL_CANDIDATES",
        8,
        "Maximum number of ranked symbol candidates resolve_symbol hands back to the model, "
        "which always picks rather than being given one silent guess.",
    ),
    (
        "SYMBOL_LIST_LIMIT_DEFAULT",
        300,
        "How many symbol rows GET /api/trading/symbols returns when the caller names no "
        "limit; the response reports `truncated` honestly whenever it clips.",
    ),
    (
        "SYMBOL_LIST_LIMIT_MAX",
        1000,
        "Hard ceiling on that route's `limit` parameter, so one request can never ask the "
        "backend to serialise an unbounded symbol table.",
    ),
    (
        "PIP_OVERRIDES",
        {},
        "Per-symbol pip sizes that override the automatic pip convention, as a JSON object "
        'such as {"XAUUSDm": 0.1}; lets the operator correct any symbol without code changes.',
    ),
    (
        "DEFAULT_RISK_PERCENT",
        1.0,
        "Percentage of balance risked when the model supplies neither a volume nor a risk "
        "percent; the value actually used is echoed in every tool result.",
    ),
    (
        "MARGIN_HEADROOM",
        0.8,
        "Fraction of free margin an order may consume, checked with order_calc_margin before "
        "the order is sent.",
    ),
    (
        "FORCE_MARGIN_MODE",
        None,
        "Overrides the margin mode detected from the account with \"hedging\" or \"netting\" so "
        "both multi-TP branches can be tested against one demo account; empty means detect.",
    ),
    (
        "MAX_TP_SPLITS",
        3,
        "Maximum number of take-profit targets a classic (non-ladder) signal may be split "
        "across on a hedging account; extra targets are stored as unmanaged. Ladders of 4+ "
        "rungs are collapsed to two legs regardless of this cap.",
    ),
    (
        "MAX_TP_INPUT",
        12,
        "Maximum number of take-profit prices accepted on open_market / place_pending / "
        "evaluate_entry, so a full XAUUSD ladder can be passed in rather than truncated to "
        "the first three rungs.",
    ),
    (
        "LADDER_VOLUME",
        0.10,
        "Default total lots for a multi-TP ladder (typically split 0.05 + 0.05). The operator "
        "setting trading.ladder_volume overrides this; risk_percent is never used for ladders.",
    ),
    (
        "LADDER_BE_BUFFER_POINTS",
        2,
        "How many broker points past entry to push a ladder runner stop when locking it to "
        "break-even after the cover take-profit fills (be_after_cover mode only).",
    ),
    (
        "LADDER_STATS_WINDOW",
        5,
        "How many fully-closed ladders per signal source decide suggested runner_mode. "
        "Zero raw SL hits in that window suggests hold_channel_sl; any raw SL, or fewer "
        "than this many outcomes, suggests be_after_cover. Overridden by "
        "trading.ladder_stats_window.",
    ),
    (
        "SL_WATCHDOG_SECONDS",
        60,
        "Interval of the watchdog that hunts for open positions with our magic and no stop "
        "loss.",
    ),
    (
        "SL_FALLBACK_ATTEMPTS",
        3,
        "How many watchdog cycles the AI is asked to attach a missing stop before the "
        "executor stops asking and attaches one itself.",
    ),
    (
        "SL_FALLBACK_POINTS",
        500,
        "Distance in points from entry used for that fallback stop, or the broker's minimum "
        "stop distance when that is larger.",
    ),
    (
        "INGEST_QUEUE_MAX",
        50,
        "Bounded depth of the per-source ingest queue; on overflow the oldest message is "
        "dropped and a system warning is emitted.",
    ),
    (
        "INGEST_WORKERS",
        3,
        "How many ingest workers may run triage and the full agent at once (1–3). Broker "
        "writes (open, modify, close, cancel) stay strictly one at a time. The channel "
        "reviewer is a separate task and does not consume one of these slots.",
    ),
    (
        "CHANNEL_BRIEF_MAX_LINES",
        60,
        "Maximum lines kept in the active channel playbook. Triage injects that text and "
        "nothing longer. Backups of a previous channel are not injected.",
    ),
    (
        "CHANNEL_BRIEF_HISTORY_DAYS",
        4,
        "Days of Telegram history the channel reviewer fetches when the database sample "
        "for the active source is missing or thin. The reviewer clamps this to 3–4.",
    ),
    (
        "CHANNEL_BRIEF_MIN_MESSAGES",
        12,
        "Stored posts below this count, or spanning less than two days, count as a thin "
        "sample and the reviewer asks Telegram for recent history before writing the brief.",
    ),
    (
        "CHANNEL_REVIEWER_MAX_TOKENS",
        2048,
        "max_tokens for the one-shot channel study. Independent of MAX_TOKENS and of "
        "TRIAGE_MAX_TOKENS. The study does not run on a schedule.",
    ),
    (
        "CATCHUP_LIMIT",
        20,
        "Maximum number of messages fetched after a restart to cover the gap; on the very "
        "first run for a source nothing is fetched and the latest id becomes the baseline.",
    ),
    (
        "TELEGRAM_FLOOD_SLEEP_THRESHOLD",
        60,
        "Telethon sleeps through flood waits shorter than this many seconds and raises "
        "anything longer so it reaches the dashboard instead of hanging for hours.",
    ),
    (
        "MAX_TOOL_ITERATIONS",
        32,
        "Maximum tool-calling turns in one full agent run; on the cap a final tool-less turn "
        "is forced so every run ends with a recorded decision. Raised so the agent is not "
        "cut off mid-ladder. The yes/no triage call does not consume this budget.",
    ),
    (
        "MAX_TOKENS",
        32768,
        "max_tokens sent on every full-agent provider request; Anthropic's endpoint requires "
        "the field. High so a thinking model is not truncated. Settings key "
        "provider.max_tokens overrides it. The triage call uses TRIAGE_MAX_TOKENS instead "
        "and does not lower this budget.",
    ),
    (
        "TRIAGE_MAX_TOKENS",
        256,
        "max_tokens for the short yes/no classifier that runs before the tool loop, on the "
        "same provider and model as Settings. Not the full-agent budget.",
    ),
    (
        "FAST_ENTRY_METAL_MARKET_DISTANCE",
        7.0,
        "Price distance (dollars on XAUUSD) inside which a parsed gold/silver entry may "
        "be opened. Seven dollars. Farther than this, open_market and place_pending are "
        "refused — the fast path and the full agent both. The word now does not widen the "
        "band when a price is printed. A post older than STALE_SIGNAL_SECONDS is not "
        "opened even inside the band. Either failure refuses the open. Does not change "
        "MAX_TOKENS or MAX_TOOL_ITERATIONS.",
    ),
    (
        "FAST_ENTRY_DEFAULT_MARKET_PIPS",
        5.0,
        "How many pips count as 'at market' for a non-metal fast entry (5 pips on EURUSD "
        "is 0.0005). Metals use FAST_ENTRY_METAL_MARKET_DISTANCE instead.",
    ),
    (
        "MAX_IMAGES_PER_RUN",
        4,
        "Maximum number of images attached to one run's context; when an album is truncated "
        "the context states how many were dropped.",
    ),
    (
        "STALE_SIGNAL_SECONDS",
        600,
        "Age, measured from the earliest chain message that printed the entry, past which "
        "a new open is refused (10 minutes). The same age flags the post stale in the "
        "model's context. modify_existing on legs that are already open is not blocked.",
    ),
    (
        "CONTEXT_SIGNALS",
        10,
        "Number of recent messages from the active source included in every run's context.",
    ),
    (
        "RECENT_SIGNALS_TOOL_MAX",
        25,
        "Upper bound on the number of messages the get_recent_signals tool may return.",
    ),
    (
        "IMAGE_MAX_EDGE",
        1568,
        "Longest edge in pixels an image is downscaled to before it is sent to the model; "
        "lower it to cut token cost, raise it for legibility.",
    ),
    (
        "IMAGE_JPEG_QUALITY",
        85,
        "JPEG quality images are first re-encoded at.",
    ),
    (
        "IMAGE_MIN_QUALITY",
        40,
        "Lowest JPEG quality the re-encode loop will drop to while trying to fit the byte cap.",
    ),
    (
        "IMAGE_MAX_BYTES",
        3_500_000,
        "Maximum encoded size of a single image; quality is reduced in steps of ten until the "
        "image fits.",
    ),
    (
        "PROBE_CACHE_HOURS",
        24,
        "How long a model's probed vision and tool capabilities are cached per base URL and "
        "model before it is probed again.",
    ),
    (
        "PROVIDER_TIMEOUT_SECONDS",
        180.0,
        "Timeout for a streaming provider request, generous because thinking models are slow.",
    ),
    (
        "NOTIFY_RETRY_DELAYS",
        (1.0, 4.0),
        "Delays in seconds between retries of a failed Telegram notification, after which a "
        "system warning with reason notify_failed is emitted.",
    ),
    (
        "NOTIFY_DETECT_SECONDS",
        60,
        "How long the notifier polls getUpdates while detecting the operator's chat id after "
        "they press Test and detect chat.",
    ),
    (
        "NOTIFY_POLL_SECONDS",
        2.0,
        "Interval between those getUpdates polls.",
    ),
    (
        "ACCOUNT_TICK_SECONDS",
        3.0,
        "Interval at which account balance, equity and open profit are refreshed and pushed "
        "to the dashboard as account.tick events.",
    ),
    (
        "EVENT_QUEUE_MAX",
        500,
        "Depth of each WebSocket subscriber's event queue; the oldest event is dropped when a "
        "slow client fills it.",
    ),
    (
        "MAX_CANDLES",
        200,
        "Maximum number of OHLC bars the get_candles tool may return in one call.",
    ),
    (
        "DB_ECHO",
        False,
        "Echoes every SQL statement to the log; useful when debugging, noisy otherwise.",
    ),
]
