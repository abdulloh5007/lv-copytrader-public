# LV Copytrader — INTERFACE CONTRACTS (authoritative)

`docs/BUILD_PROMPT.md` is authoritative on **behaviour, traps and prohibitions**. Read it.
This file is authoritative on **interfaces**: module names, symbol names, signatures, ORM columns,
event names, tool names, REST routes, and JSON shapes. If they ever disagree on a *name*, this file wins.
Never invent a cross-module name that is not listed here. Never change one of these signatures.

Python 3.12. Backend is `import`-rooted at `backend/` so every module is `app.<pkg>.<mod>`.
Absolute imports only (`from app.trading.constants import ...`). Every package dir has `__init__.py`.
Async everywhere; no blocking I/O in the event loop (Pillow work goes through `asyncio.to_thread`).

Backend NEVER imports `MetaTrader5`. Only `mt5-sidecar/rpc_server.py` does.

---

## 0. Deviations from the tree in the build prompt (deliberate, documented in README)

* `backend/app/runtime.py` — added. A shared, typed service container (`Runtime`) held on
  `app.state.runtime`. Without it every module would reach into `main.py` or an untyped dict.
* `frontend/src/vite-env.d.ts`, `frontend/tsconfig.node.json` — added, required by Vite/TS.
* `frontend/src/components/*.css` is NOT used; page/component-specific CSS lives in
  `components.css` only if reusable, otherwise as a `<style>`-free class in `components.css`.
  (No CSS-in-JS, no colour literal outside `tokens.css`.)
* `backend/app/trading/ladder_stats.py` — added. Per-source last-N fully-closed ladder
  outcomes, suggested runner-after-cover mode, and suggested runner-TP bias. The AI must
  not recount channel history to invent these counts; it reads the server blurb /
  `get_channel_ladder_stats`.
* `backend/app/trading/exposure.py` — added in v1.1. `close_signal_exposure(signal_ref)`
  closes open positions and cancels pendings for one reply chain. A deleted channel
  post (`Message.deleted_at`) and a FULL CLOSE reply take this path without a model
  run. The same name is an agent tool; the server resolves tickets.
* `backend/app/ai/fast_entry.py` — added. After triage says yes, one quote against the
  parsed entry sends `open_market` before the tool loop, or skips. `buy now` / `sell now`
  is always a market open. A far quote does not place a pending order. A post older than
  `STALE_SIGNAL_SECONDS`, including an old catch-up, is not chased. It does not lower
  `MAX_TOKENS` or `MAX_TOOL_ITERATIONS`. A classic metal entry uses
  `trading.max_entry_drift_usd` (default 5 price units). A ladder of 4 or more
  targets keeps `FAST_ENTRY_METAL_MARKET_DISTANCE` (default 7.0). Non-metals use
  `FAST_ENTRY_DEFAULT_MARKET_PIPS` (default 5). `4316=44` is the price 4316.44.
Nothing else is added or omitted.

---

## 1. `app/config.py`

Pydantic-settings v2. Exposes a module-level singleton `settings` and the constants below as
**module-level UPPERCASE names on the same module** (so `from app.config import MAGIC_NUMBER` works
and `settings.MAGIC_NUMBER` also works — implement the constants as fields on `Settings` with these
defaults and re-export each as a module-level alias).

**Secrets are exempt from the alias rule.** `SECRET_KEY`, `MT5_PASSWORD` and `SIDECAR_TOKEN` are
reachable ONLY as `settings.<NAME>`, never as a module-level alias. `crypto.bootstrap_secret_key()`
mutates `settings.SECRET_KEY` at runtime, so an alias bound at import time would be a permanently
stale copy of the key that decrypts every stored secret — and the same hazard applies to any
rotatable credential. Do not "fix" this by adding them.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
```

Env-backed (read from `.env`):

| name | type | default |
|---|---|---|
| `SECRET_KEY` | `str \| None` | `None` (bootstrapped, see crypto) |
| `MT5_SERVER` | `str` | `"Exness-MT5Trial15"` |
| `MT5_LOGIN` | `int \| None` | `None` |
| `MT5_PASSWORD` | `str` | `""` |
| `MT5_INSTALLER_URL` | `str` | `"https://download.mql5.com/cdn/web/metaquotes.ltd/mt5/mt5setup.exe"` |
| `SIDECAR_URL` | `str` | `"http://mt5-sidecar:18812"` |
| `SIDECAR_TOKEN` | `str` | `"change-me"` |
| `DATA_DIR` | `str` | `"/data"` |
| `LOG_LEVEL` | `str` | `"INFO"` |
| `FRONTEND_DIST` | `str` | `"/app/static"` |

Tunable constants (fields with these exact names and defaults):

```
RPC_TIMEOUT_SECONDS=30.0  RPC_CONNECT_TIMEOUT_SECONDS=10.0  RPC_HEALTH_SECONDS=10.0
MAGIC_NUMBER=770077  DEVIATION_POINTS=20  REQUOTE_RETRIES=2
SYMBOL_CACHE_SECONDS=3600  MAX_SYMBOL_CANDIDATES=8
SYMBOL_LIST_LIMIT_DEFAULT=300  SYMBOL_LIST_LIMIT_MAX=1000
PIP_OVERRIDES: dict[str,float] = {}
DEFAULT_RISK_PERCENT=1.0  MARGIN_HEADROOM=0.8  FORCE_MARGIN_MODE: str|None = None
MAX_TP_SPLITS=3  MAX_TP_INPUT=12  LADDER_VOLUME=0.10  LADDER_BE_BUFFER_POINTS=2
LADDER_STATS_WINDOW=5
SL_WATCHDOG_SECONDS=60  SL_FALLBACK_ATTEMPTS=3  SL_FALLBACK_POINTS=500
INGEST_QUEUE_MAX=50  CATCHUP_LIMIT=20  TELEGRAM_FLOOD_SLEEP_THRESHOLD=60
INGEST_WORKERS=3  MAX_TOOL_ITERATIONS=32  MAX_TOKENS=32768  TRIAGE_MAX_TOKENS=256
FAST_ENTRY_METAL_MARKET_DISTANCE=7.0  FAST_ENTRY_DEFAULT_MARKET_PIPS=5.0  MAX_IMAGES_PER_RUN=4
STALE_SIGNAL_SECONDS=600  CONTEXT_SIGNALS=10  RECENT_SIGNALS_TOOL_MAX=25
IMAGE_MAX_EDGE=1568  IMAGE_JPEG_QUALITY=85  IMAGE_MIN_QUALITY=40  IMAGE_MAX_BYTES=3_500_000
PROBE_CACHE_HOURS=24  PROVIDER_TIMEOUT_SECONDS=180.0
NOTIFY_RETRY_DELAYS: tuple[float,...] = (1.0, 4.0)  NOTIFY_DETECT_SECONDS=60  NOTIFY_POLL_SECONDS=2.0
ACCOUNT_TICK_SECONDS=3.0  EVENT_QUEUE_MAX=500
MAX_CANDLES=200  DB_ECHO=False
```

Derived properties: `db_path -> str` (`{DATA_DIR}/signalpilot.db`),
`db_url -> str` (`sqlite+aiosqlite:///{db_path}`), `image_dir -> str` (`{DATA_DIR}/images`),
`telethon_session_dir`, `env_file_path -> str` (`.env` next to repo root, from `SP_ENV_FILE` or `/app/.env`).
`Settings.ensure_dirs()` creates `DATA_DIR` and `image_dir`.

---

## 2. `app/trading/constants.py`

Exactly the literals from BUILD_PROMPT §1.2, plus:
```python
ACTION_NAMES: dict[int,str]; ORDER_TYPE_NAMES: dict[int,str]
SUCCESS_RETCODES = frozenset({10008, 10009})
PARTIAL_RETCODE = 10010
def retcode_name(rc: int|None) -> str          # RETCODE.get(rc, f"RETCODE_{rc}"); None -> "NONE"
def margin_mode_name(v: int) -> str            # 0 netting / 1 exchange / 2 hedging
def timeframe_value(name: str) -> int          # raises TradingError on unknown
POSITION_TYPE_NAMES = {0: "buy", 1: "sell"}
PENDING_ORDER_TYPES = frozenset({2,3,4,5})
```

## 3. `app/errors.py`

```python
class SignalPilotError(Exception):
    code: str = "error"
    def __init__(self, message: str, *, detail: dict | None = None) -> None
    message: str; detail: dict
class TradingError(SignalPilotError): code="trading_error"
class RpcError(TradingError):                 # sidecar returned ok:false / transport failure
    def __init__(self, message, *, rpc_code: int = 0, retcode: int | None = None, detail=None)
class ProviderError(SignalPilotError): code="provider_error"
    def __init__(self, message, *, status: int | None = None, kind: str = "unknown", detail=None)
    # kind in {"auth","not_found","rate_limit","overloaded","bad_request","transport","decode","context_window","unknown"}
class IngestError(SignalPilotError): code="ingest_error"
class ConfigError(SignalPilotError): code="config_error"
def retcode_message(retcode: int|None, comment: str = "") -> str   # human sentence per §1.3 table
def to_http(exc: SignalPilotError) -> tuple[int, dict]             # (status, {"error": {...}})
```
`retcode_message` maps every key of `RETCODE` to an actionable English sentence
(e.g. `10019 -> "not enough free margin"`, `10016 -> "invalid stops"`, `10030 -> "unsupported filling mode"`),
appending the broker comment when non-empty.

## 4. `app/events.py`

```python
SIGNAL_RECEIVED="signal.received"; RUN_STARTED="run.started"; RUN_THINKING="run.thinking"
RUN_TEXT="run.text"; RUN_TOOL_CALL="run.tool_call"; RUN_TOOL_RESULT="run.tool_result"
RUN_FINISHED="run.finished"; PROVIDER_REQUEST="provider.request"
ORDER_PLACED="order.placed"; ORDER_CANCELLED="order.cancelled"
POSITION_OPENED="position.opened"; POSITION_MODIFIED="position.modified"
POSITION_CLOSED="position.closed"; ACCOUNT_TICK="account.tick"
SYSTEM_WARNING="system.warning"; STATUS_CHANGED="status.changed"; SETTINGS_CHANGED="settings.changed"

class EventBus:
    def subscribe(self) -> asyncio.Queue[dict]
    def unsubscribe(self, q) -> None
    def emit(self, type: str, **data) -> None     # sync, never blocks, drops oldest on a full queue
    def recent(self, limit: int = 200) -> list[dict]
bus = EventBus()                                  # module-level singleton
```
Envelope: `{"type": str, "ts": float (unix seconds), "seq": int, "data": {...}}`.
`emit` is safe to call from any thread only via the loop it was created on — call it from async code.

## 5. `app/crypto.py`

```python
def bootstrap_secret_key(env_path: str) -> tuple[str, bool]   # (key, was_generated); writes .env
def get_fernet() -> Fernet
def encrypt(value: str) -> str            # returns "enc:v1:<token>"
def decrypt(value: str) -> str            # raises CryptoError on failure
class CryptoError(SignalPilotError): code="crypto_error"
def is_encrypted(value: str|None) -> bool
MASK = "••••••••"      # eight bullets
def mask(value: str|None) -> str|None                          # None -> None, else MASK
```

## 6. `app/db.py`

```python
engine: AsyncEngine ; SessionLocal: async_sessionmaker[AsyncSession]
async def init_db() -> None                     # create_all + seed defaults + PRAGMA journal_mode=WAL, foreign_keys=ON
async def dispose_db() -> None
def session_scope() -> AsyncContextManager[AsyncSession]        # commits on exit, rolls back on error
async def get_session() -> AsyncIterator[AsyncSession]          # FastAPI dependency

# settings KV (table app_settings). `secret=True` values are Fernet-encrypted at rest.
async def get_setting(s: AsyncSession, key: str, default=None) -> Any        # JSON-decoded, decrypted
async def set_setting(s: AsyncSession, key: str, value: Any, *, secret: bool=False) -> None
async def get_settings_map(s: AsyncSession, prefix: str = "") -> dict[str, Any]
async def has_value(s: AsyncSession, key: str) -> bool
async def needs_reentry_keys(s: AsyncSession) -> list[str]
async def clear_needs_reentry(s: AsyncSession, key: str) -> None
```
Decrypt failure never raises out of `get_setting`: it returns `default`, sets `needs_reentry=True`
on the row, and emits `SYSTEM_WARNING` with `reason="needs_reentry"`.

**Settings keys** (exact strings — the only ones used anywhere):
```
mt5.server mt5.login mt5.password*                                (*secret)
tg.api_id tg.api_hash tg.phone tg.session* tg.phone_code_hash tg.user
provider.kind provider.base_url provider.api_key* provider.model
provider.extra_headers provider.max_tokens
provider.supports_vision_override provider.supports_tools_override
notifier.bot_token* notifier.chat_id notifier.enabled notifier.types notifier.admin_ids
notifier.bound_to
limits.max_volume_per_trade limits.max_open_positions limits.max_daily_loss_percent
limits.symbol_allowlist limits.trading_hours
trading.armed  trading.default_risk_percent  trading.ladder_volume
trading.ladder_stats_window  ui.locale
```
`trading.ladder_stats_window` defaults to `5` (compiled `LADDER_STATS_WINDOW`). It is the
number of fully-closed ladders per `source_id` used to suggest `runner_mode` and the
runner-TP bias. Not a Settings knob — operators keep `trading.ladder_volume`; the window is
documented here so a future override is a named key rather than a magic number.
`notifier.types` = `{"opened":true,"modified":true,"closed":true,"rejected":true,"attention":true}`.
`notifier.admin_ids` = `[]` or `[<telegram user_id>, ...]`. NOT a secret — an id is public the moment
its owner writes to the bot — so it is returned unmasked. **Empty is the shipped default and is not a
restriction**: whoever messages the bot first binds it and receives every trade notification. Non-empty
means only those accounts may bind the bot or receive notifications: `detect_chat` **skips** an
update from anyone else and keeps waiting rather than binding the first stranger, and if the window
expires having seen only non-admins the answer says exactly that — a different fault, with a different
fix, from "nobody messaged the bot". `send` refuses a chat id that is not one of these ids (a negative
group id can never match, by construction), logs it, records the failed `Notification` row and emits
`system.warning` with reason `notify_not_admin`; the refusal is quiet on the calling side and never
raises into the executor (§7). Detection and delivery are held to the SAME rule: while the list is in
force `detect_chat` will not bind a group chat even for an allowed sender, because a negative chat id
can never be a user id and `send` would then refuse every notification to it — binding it would leave
a bot that looks configured and is mute. That case has its own message too.
`notifier.bound_to` = `null` or `{"user_id":int,"username":str,"first_name":str}` — who `detect_chat`
bound, written beside `notifier.chat_id` by the same call and returned in the settings tree, so "who
is this bot talking to" is answerable. An unknown name is the empty string, never null, because the
page trims it; no numeric id means no object at all rather than an object naming nobody. While `admin_ids` is empty that identity is the
only check there is. Retyping `notifier.chat_id` through `PUT /api/settings/notifier` **clears** it: a
name read from one chat must never be shown beside a different chat's id.
`limits.trading_hours` = `[]` or `[{"days":[0..6],"from":"HH:MM","to":"HH:MM","tz":"UTC"}]`.
`trading.armed` defaults to `false`.

## 7. `app/models.py` — SQLAlchemy 2.0 declarative, `Base = DeclarativeBase`

All timestamps are timezone-naive **UTC** `datetime` unless the name ends in `_ts` (unix float).

```python
class AppSetting(Base):            # __tablename__ = "app_settings"
    key: str PK; value: str|None; is_secret: bool=False; needs_reentry: bool=False
    updated_at: datetime

class Source(Base):                # "sources"
    id: int PK; kind: str = "telegram"; chat_id: int (BigInteger); title: str
    username: str|None; entity_type: str            # "channel"|"supergroup"|"group"|"user"
    is_active: bool = False; last_message_id: int = 0; created_at: datetime

class Message(Base):               # "messages"
    id: int PK
    source_id: int FK->sources.id (index)
    message_id: int (BigInteger, index)
    edit_date: int = 0                       # NEVER NULL (§3.5)
    revision: int = 1
    kind: str                                # one of KINDS
    status: str                              # "pending"|"processing"|"processed"|"skipped"|"error"
    skip_reason: str|None
    catchup: bool = False
    text: str = ""
    reply_to_message_id: int|None
    permalink: str|None
    posted_at: datetime; received_at: datetime
    grouped_id: int|None                     # album id
    image_count: int = 0
    error: str|None
    __table_args__ = (UniqueConstraint("source_id","message_id","edit_date", name="uq_message_dedup"),
                      Index("ix_messages_source_msg", "source_id", "message_id"))
    # signal_ref property: f"SIG:{source_id}:{message_id}"   (revision-independent, §5.5)

class MessageImage(Base):          # "message_images"
    id: int PK; message_row_id: int FK->messages.id (index); order_index: int
    path: str; mime: str; width: int|None; height: int|None; bytes: int

class Run(Base):                   # "runs"
    id: str PK                                # uuid4().hex
    source_id: int|None FK; message_row_id: int|None FK->messages.id (index)
    kind: str = "signal"                      # "signal"|"sl_watchdog"|"manual"
    status: str                               # "running"|"finished"|"incomplete"|"error"
    decision: str|None                        # "acted"|"ignored"|"deferred"
    summary: str|None; error: str|None
    provider_kind: str|None; model: str|None
    input_tokens: int = 0; output_tokens: int = 0; latency_ms: int = 0
    iterations: int = 0; armed_at_start: bool = False
    started_at: datetime; finished_at: datetime|None

class RunStep(Base):               # "run_steps"
    id: int PK; run_id: str FK->runs.id (index); idx: int
    type: str            # "thinking"|"text"|"tool_call"|"tool_result"|"error"|"provider"
    name: str|None       # tool name
    payload: dict (JSON); ok: bool|None; created_at: datetime

class Trade(Base):                 # "trades"
    id: int PK
    source_id: int|None FK; run_id: str|None (index); message_row_id: int|None FK
    signal_ref: str|None (index)              # "SIG:<source>:<message_id>"
    ticket: int (BigInteger, unique index)
    kind: str                                 # "position"|"order"
    symbol: str; side: str                    # "buy"|"sell"
    order_type: int                           # MT5 ORDER_TYPE_* / POSITION_TYPE_*
    volume: float; requested_volume: float|None
    entry_price: float; sl: float = 0.0; tp: float = 0.0
    status: str                               # "open"|"pending"|"closed"|"cancelled"
    magic: int; comment: str = ""
    external: bool = False
    split_index: int = 0; split_count: int = 1
    unmanaged_tps: list (JSON) = []
    ladder_role: str|None                              # "cover"|"runner" on a two-leg ladder
    runner_mode: str|None                              # "be_after_cover"|"hold_channel_sl"
    suggested_runner_mode: str|None                    # server suggestion at open time
    suggested_runner_tp_index: int|None                # 1-based numbered TP; LONG excluded from N
    chosen_runner_tp_index: int|None                   # suggested, or suggested ±1
    runner_tp_bias: int|None                           # 0 / +1 / +2 at open; never random
    open_reason: str|None; close_cause: str|None       # "tp"|"sl"|"ai"|"manual"|"kill_switch"|"unknown"
    close_price: float|None; profit: float|None; profit_points: float|None
    opened_at: datetime|None; closed_at: datetime|None; updated_at: datetime

class LadderOutcome(Base):         # "ladder_outcomes" — one fully-closed ladder group
    id: int PK
    source_id: int|None FK->sources.id (index)
    signal_ref: str (unique index)                    # one row per group, never AI-filled
    run_id: str|None; symbol: str = ""; side: str = ""
    runner_mode: str|None; suggested_mode: str|None   # chosen vs suggested at open
    kind: str                                         # "raw_sl_hit"|"be_stopout"|"tp4_plus"|"other"
    runner_close_cause: str|None; runner_profit: float|None
    runner_tp: float|None; runner_sl: float|None; runner_close_price: float|None
    cover_close_cause: str|None; cover_profit: float|None
    suggested_runner_tp_index: int|None; chosen_runner_tp_index: int|None
    runner_tp_bias: int|None; runner_hit_chosen_tp: bool|None
    note: str|None
    closed_at: datetime; created_at: datetime

class TradeEvent(Base):            # "trade_events" — audit of every write that touched a ticket
    id: int PK; trade_id: int|None FK; ticket: int|None; run_id: str|None
    type: str        # "open"|"modify"|"close"|"partial_close"|"cancel"|"reject"|"reconcile"|"fallback_sl"
    detail: dict (JSON); retcode: int|None; retcode_name: str|None
    created_at: datetime

class StoplessEpisode(Base):       # "stopless_episodes"
    id: int PK; ticket: int (index); trade_id: int|None FK
    started_at: datetime; attempts: int = 0; notified: bool = False
    fallback_applied: bool = False; resolved_at: datetime|None

class Notification(Base):          # "notifications"
    id: int PK; type: str; text: str; ok: bool = False; error: str|None
    created_at: datetime; sent_at: datetime|None

class ProbeCache(Base):            # "probe_cache"
    id: int PK; cache_key: str (unique)       # f"{kind}|{base_url}|{model}"
    supports_vision: bool; supports_tools: bool
    thinking_form: str|None                   # "adaptive"|"enabled"|None
    source: str                               # "declared"|"probed"
    checked_at: datetime

class SystemState(Base):           # "system_state" — single row id=1, volatile runtime facts
    id: int PK (=1)
    mt5_connected: bool=False; mt5_error: str|None
    telegram_connected: bool=False; telegram_error: str|None
    provider_ok: bool=False; provider_error: str|None
    margin_mode: str|None; account_currency: str|None
    last_reconcile_at: datetime|None; updated_at: datetime
```

## 8. `app/runtime.py`

```python
@dataclass
class Runtime:
    settings: Settings
    rpc: "Mt5Rpc"
    resolver: "SymbolResolver"
    executor: "Executor"
    telegram: "TelegramService"        # app.telegram.client
    ingestor: "Ingestor"               # app.telegram.ingest
    notifier: "Notifier"               # app.telegram.notifier
    bus: EventBus
    tasks: dict[str, asyncio.Task]
    status: dict[str, Any]             # {"mt5":..,"telegram":..,"provider":..,"armed":bool,"needs_reentry":[...]}
    started_at: float
    async def armed(self) -> bool
    async def set_armed(self, value: bool) -> None
    async def provider(self) -> tuple["Provider", str, "Capabilities"] | None   # (provider, model, caps)
    def set_status(self, key: str, ok: bool, error: str | None = None) -> None  # emits STATUS_CHANGED on change
def get_runtime(request: Request) -> Runtime          # FastAPI dependency (request.app.state.runtime)
```

## 9. `app/trading/rpc.py`

```python
def to_ns(obj: Any) -> Any            # dict->SimpleNamespace, list[dict]->tuple[SimpleNamespace], recursive
class Mt5Rpc:
    def __init__(self, url: str, token: str, timeout: float = ..., connect_timeout: float = ...)
    async def start(self) -> None ; async def close(self) -> None
    async def call(self, method: str, **params) -> Any       # raises RpcError; returns to_ns(result)
    async def call_raw(self, method: str, **params) -> Any   # no namespace conversion (rates, lists of scalars)
    async def ping(self) -> bool                             # never raises
    # typed helpers, each returning namespaces:
    async def account_info(); async def terminal_info(); async def symbols_get()
    async def symbol_info(name); async def symbol_select(name, enable=True)
    async def symbol_info_tick(name); async def copy_rates_from_pos(symbol, timeframe, start, count)
    async def positions_get(**kw); async def orders_get(**kw)
    async def history_deals_get(date_from: float, date_to: float, **kw)
    async def order_check(request: dict); async def order_send(request: dict)
    async def order_calc_margin(action:int, symbol:str, volume:float, price:float)
```
`order_send`/`order_check` return `None` when the sidecar reports a null result — callers must null-check.
`RpcError.rpc_code` mirrors the sidecar's `error.code`; `-1` transport, `-2` auth, `-3` mt5 call returned None.

## 10. `app/trading/symbols.py`

```python
@dataclass(frozen=True)
class SymbolRef:
    name: str; description: str; digits: int; point: float; pip_size: float; pip_convention: str
    volume_min: float; volume_step: float; volume_max: float
    stops_level_points: int; freeze_level_points: int; spread_points: int
    trade_tick_size: float; trade_tick_value: float; trade_tick_value_loss: float
    contract_size: float; currency_profit: str; currency_margin: str
    trade_mode: int; trade_exemode: int; filling_mode: int; visible: bool
    path: str = ""                                # the broker's Market Watch GROUP, e.g. "Raw\Forex":
                                                  # symbol_info().path with its trailing symbol leaf
                                                  # removed. The one field read with getattr — it names
                                                  # a folder, never reaches an order, and a broker that
                                                  # omits it must leave the symbol listed without a group
                                                  # rather than drop it. NOT in to_dict(): the tool shape
                                                  # of resolve_symbol is unchanged.
    @property def tradable(self) -> bool          # trade_mode == 4 (SYMBOL_TRADE_MODE_FULL)
    def to_dict(self) -> dict                     # exactly the tool-facing shape of resolve_symbol rows
BROKER_SUFFIXES: tuple[str, ...]
ALIASES: dict[str, tuple[str, ...]]
def pip_size(si) -> float                          # si is a SimpleNamespace from symbol_info
def pip_convention(si) -> str                      # "override"|"fx_5digit"|"fx_3digit_jpy"|"three_digit_x10"
                                                   # |"metal_gold_0.10"|"metal_silver_0.01"
                                                   # |"metal_platinum_0.10"|"metal_palladium_0.10"|"point"
# A metal's pip is anchored to the METAL, never to a multiple of `point`: the live Exness
# demo quotes XAUUSDr at digits=3 while the classic feed uses digits=2, and `point * 10`
# would make the same instrument's pip differ by a factor of ten between them.
class SymbolResolver:
    def __init__(self, rpc: Mt5Rpc, settings: Settings)
    async def refresh(self, force: bool = False) -> int
    async def candidates(self, query: str) -> list[SymbolRef]
    async def catalog(self, *, query: str = "", only_tradable: bool = False,
                      limit: int | None = None) -> tuple[list[SymbolRef], int]
        # The operator's picker, not a trading decision: every row is built from the CACHED
        # symbols_get snapshot (one RPC round trip, not one per symbol, on a table of 300+),
        # and the second element is the match total BEFORE `limit`, so the caller can report
        # `truncated` honestly. `query` matches name and description case-insensitively and
        # across a broker suffix in both directions ("XAUUSD" finds "XAUUSDr"), with `path`
        # matched last. Ranking: exact name, exact-after-suffix, ALIASES hit, prefix, name
        # substring, description, group; ties break on (group, name).
    async def closest(self, query: str, n: int = 3) -> list[str]
    async def get(self, name: str) -> SymbolRef          # exact name, selects it, raises TradingError if absent
    async def quote(self, name: str) -> "Quote"          # selects, retries once after 0.5s on zero tick
@dataclass(frozen=True)
class Quote: symbol:str; bid:float; ask:float; spread_points:int; spread_pips:float
             time_ts:float; server_time:str
             def to_dict(self) -> dict
async def server_time_offset(rpc, resolver) -> float     # broker-server seconds minus time.time()
async def broker_day_start_ts(rpc, resolver) -> float
```

## 11. `app/trading/pricing.py`

```python
def points(si_or_ref, price_distance: float) -> float
def pips(ref: SymbolRef, price_distance: float) -> float
def distance_units(ref: SymbolRef, price_distance: float) -> dict   # {"price":..,"points":..,"pips":..}
def round_price(ref: SymbolRef, price: float) -> float
def round_stop_away(ref: SymbolRef, price: float, side: str, kind: str) -> float
    # kind in {"sl","tp"}; rounds AWAY from the market so rounding cannot breach stops_level (§2)
def price_from_distance(ref, anchor: float, direction: str, distance: float, unit: str) -> dict
    # direction in {"above","below"}; unit in {"pips","points","price","percent"}
    # -> {"price": float, "convention": str, "anchor": float, "distance": {...distance_units}}
@dataclass
class VolumeResult: ok: bool; volume: float; loss_per_lot: float; risk_amount: float
                    risk_percent: float; clamped: bool; message: str
def compute_volume(ref, balance: float, risk_percent: float, entry: float, sl: float,
                   currency: str) -> VolumeResult
def normalize_volume(ref, volume: float) -> float          # floor to step, round(...,8)
def split_volume(ref, total: float, parts: int) -> list[float]   # largest remainder, sums exactly
LADDER_MIN_TARGETS = 4
LONG_GAP_MULTIPLE = 2.0
def is_tp_ladder(targets) -> bool
def one_r_take_profit(ref, side, entry, sl) -> float   # money 1R from fill, NOT channel TP1
def split_ladder_rungs(targets) -> tuple[list[float], float | None]
    # numbered TPs vs optional LONG (last print if last gap ≥ 2× median inner gap)
def suggest_runner_tp_index(n, bias=0) -> int
    # 1-based. base = ceil((N+1)/2). N=1 → 1; N=2 → min(base+bias, 2);
    # N≥3 → min(base+bias, N-1). Floor 1. No random.
def pick_ladder_runner(targets, *, bias=0, index=None) -> LadderRunnerPick | None
def pick_ladder_runner_tp(targets, *, bias=0, index=None) -> float | None
def plan_ladder_targets(ref, side, entry, sl, channel_tps, *, bias=0,
                        runner_tp_index=None) -> LadderTargets
    # If the formula rung is not strictly beyond the 1R cover, advance to the next
    # numbered rung (still ≤ N-1). LONG is the fallback ceiling.
def breakeven_stop(ref, side, entry, *, buffer_points=2) -> float
def stop_is_at_or_beyond_breakeven(side, entry, sl) -> bool
def validate_stops(ref, side: str, entry: float, sl: float, tp: float,
                   bid: float, ask: float, *, pending: bool) -> str | None   # None == valid
def pick_filling(si, action: int, partial: bool = False) -> int             # exactly §1.3
def infer_pending_type(side: str, order_type: str, price: float, bid: float, ask: float) -> tuple[int|None, str]
    # -> (ORDER_TYPE_*, explanation) ; (None, why-it-is-impossible) on a contradiction
```

## 12. `app/trading/guards.py`

Guards return `None` when they pass and a **refusal string** when they block. They never raise.

```python
SL_REQUIRED = "a stop loss is required — resend with sl"
SL_NOT_REMOVABLE = "the stop cannot be removed; move it instead"
DISARMED = "System is disarmed — no order was sent"
def check_sl_present(sl: float | None) -> str | None          # not called on open; missing sl is an incomplete entry
def check_sl_not_removed(sl: float | None) -> str | None
def check_sl_side(side: str, entry: float, sl: float) -> str | None
def check_stops_level(ref, side, entry, sl, tp, bid, ask, *, pending: bool) -> str | None
async def check_armed(s: AsyncSession) -> str | None
async def check_duplicate_signal(s: AsyncSession, signal_ref: str | None) -> str | None
    # Also refuses when any live ticket belongs to the same reply chain (root teaser and
    # every reply under it). A reply's own signal_ref differs from the teaser's; the guard
    # still treats them as one signal. Edits already share signal_ref.
async def check_optional_limits(s: AsyncSession, *, symbol: str, volume: float,
                                rpc, resolver, now_ts: float) -> str | None
class SlWatchdog:
    def __init__(self, runtime: Runtime)
    async def start(self) -> None ; async def stop(self) -> None
    async def tick(self) -> None                # one cycle; unit-testable
```

## 13. `app/trading/executor.py`

```python
@dataclass
class RunContext:
    run_id: str; signal_ref: str | None; source_id: int | None; message_row_id: int | None
    origin: str = "agent"          # "agent"|"manual"|"watchdog"|"kill_switch"
    bypass_arm: bool = False       # kill switch closes and the SL fallback only
class Executor:
    def __init__(self, runtime: Runtime)
    # ---- read tools
    async def get_account(self) -> dict
    async def resolve_symbol(self, query: str) -> dict
    async def get_quote(self, symbol: str) -> dict
    async def price_from_distance(self, symbol, anchor, direction, distance, unit) -> dict
    async def evaluate_entry(self, symbol, side, signal_entry, sl, tp=None,
                            signal_age_seconds=None, source_id=None) -> dict
        # Ladders include ladder_stats, suggested_runner_mode, suggested_runner_tp_index,
        # suggested_runner_tp_bias, and a concrete "Suggested runner TP: 5/7 (4359)." line.
    async def get_positions(self) -> dict
    async def get_orders(self) -> dict
    async def get_candles(self, symbol, timeframe, count) -> dict
    async def get_recent_signals(self, limit: int) -> dict
    async def get_channel_ladder_stats(self, *, source_id: int | None = None) -> dict
        # Last-N closed ladders for this source. Never recounts Telegram history.
    # ---- write tools
    async def open_market(self, ctx, *, symbol, side, sl, tp=None, volume=None,
                          risk_percent=None, reason, runner_mode=None,
                          runner_tp_index=None) -> dict
    async def place_pending(self, ctx, *, symbol, side, order_type, price, sl, tp=None,
                            expiry_minutes=None, volume=None, risk_percent=None, reason,
                            runner_mode=None, runner_tp_index=None) -> dict
    async def modify_position(self, ctx, *, ticket, sl=None, tp=None, reason) -> dict
    async def modify_order(self, ctx, *, ticket, price=None, sl=None, tp=None, reason) -> dict
    async def close_position(self, ctx, *, ticket, volume=None, reason) -> dict
    async def cancel_order(self, ctx, *, ticket, reason) -> dict
    async def notify(self, ctx, *, text) -> dict
    # ---- dispatch + control
    async def dispatch(self, name: str, args: dict, ctx: RunContext) -> dict
    async def kill_switch(self, *, reason: str) -> dict
    async def attach_fallback_sl(self, ticket: int) -> dict
    async def trail_ladder_runners(self) -> dict
        # After a two-leg ladder's cover TP fills: if stored runner_mode is be_after_cover
        # (or missing — safer default), lock the runner SL to break-even. hold_channel_sl
        # is a no-op (channel stop stays). Protective: bypasses the arm gate.
        # Does not use risk_percent.
    async def send_order(self, request: dict, *, ctx, expect_pending: bool) -> "SendResult"
@dataclass
class SendResult: ok: bool; retcode: int|None; retcode_name: str; message: str
                  ticket: int|None; volume: float; price: float; slippage_points: float|None
                  comment: str; raw: dict
```
Every tool method returns a **JSON-serialisable dict**. Write tools always include
`{"ok": bool, "message": str}` and, on success, `ticket`/`tickets`, `filled_volume`, `fill_price`,
`slippage_points`, `retcode`, `retcode_name`. A broker rejection is `ok:false` with an actionable
message — never an exception.

## 14. `app/trading/reconcile.py`

```python
async def reconcile(runtime: Runtime, *, cause: str = "startup") -> dict
    # -> {"linked":int,"external":int,"closed":int,"orphans":int,"catchup":int}
async def link_tickets(runtime, session) -> tuple[int,int]
async def close_missing(runtime, session) -> int
    # After every ticket of a ladder group is closed, writes one LadderOutcome row
    # (idempotent on signal_ref). The AI does not fill this form.
def run_id_from_comment(comment: str) -> str | None      # "SP:xxxxxxxx" -> the 8-hex prefix
def make_comment(run_id: str) -> str                     # f"SP:{run_id[:8]}"
```

## 14.1 `app/trading/ladder_stats.py`

```python
MODE_BE_AFTER_COVER = "be_after_cover"
MODE_HOLD_CHANNEL_SL = "hold_channel_sl"
OUTCOME_RAW_SL = "raw_sl_hit"; OUTCOME_BE_STOPOUT = "be_stopout"
OUTCOME_TP4_PLUS = "tp4_plus"; OUTCOME_OTHER = "other"
class ChannelLadderStats:   # source_id, source_title, window, sample_size,
                            # raw_sl_hits, be_stopouts, tp4_plus, clean_streak,
                            # suggested_mode, bootstrap, suggested_runner_tp_bias;
                            # .blurb() / .to_dict()
def suggest_mode(kinds, *, window=5) -> str
    # kinds newest-first. len < window → be_after_cover (safer bootstrap).
    # any raw_sl_hit in the window → be_after_cover. else hold_channel_sl.
def suggest_runner_tp_bias(*, sample_size, window=5, raw_sl_hits, tp4_plus,
                           clean_streak, bootstrap=None) -> int
    # Deterministic; never random. Bias table:
    #   sample_size < window (bootstrap)              → 0
    #   any raw_sl_hits in the window                 → 0  (stay at base)
    #   raw_SL=0 and clean_streak in {3,4}            → +1
    #   streak ≥ 5 and healthy tp4_plus               → +1
    #     healthy := tp4_plus >= max(2, (window+1)//2)   # window 5 → ≥ 3
    #   streak ≥ window and tp4_plus ≥ window-1       → +2 (still capped at N-1)
    #   otherwise                                     → 0
def aggregate_kinds(kinds, *, window=5, source_id=None, source_title="") -> ChannelLadderStats
def blurb_for(stats) -> str
    # "... Suggested: hold_channel_sl (streak 5). Runner-TP bias +1
    #  (formula: min(ceil((N+1)/2)+bias, N-1); LONG excluded from N). Override only with reason."
def format_runner_tp_blurb(*, index, n, price, stats, chosen_index=None) -> str
    # "Suggested runner TP: 5/7 (4359). Bias+1 from streak 5."
def classify_group(legs) -> str            # one of the OUTCOME_* literals
def runner_hit_chosen_tp(runner) -> bool   # runner closed at its stored / chosen TP
async def stats_for_source(session, source_id, *, window=None) -> ChannelLadderStats
async def maybe_record_closed_group(session, *, signal_ref, note=None) -> LadderOutcome | None
def normalize_runner_mode(value) -> str | None
```
Suggested-mode and runner-TP-bias rules are server-side only. The prompt injects
`stats.blurb()`; `evaluate_entry` / open add `format_runner_tp_blurb` once N is known.
The model chooses `runner_mode` on `open_market` / `place_pending` and may keep
`be_after_cover` when suggested is `hold_channel_sl` (caution), stating the reason in
`finish`. Optional `runner_tp_index` may shift the suggested numbered TP by at most ±1
(reason in `finish`); omitted, the executor uses the suggested index. Classic 1–3 TP
signals keep the existing split and ignore both knobs.

## 15. `app/telegram/*`

`classify.py`
```python
KINDS: frozenset[str]                 # exactly the set in §3.2
PROCESSABLE = frozenset({"text","photo","photo_caption"})
def classify(m) -> str                # exactly the branch order in §3.2
def kind_is_processable(kind: str) -> bool
def skip_reason(kind: str) -> str     # short English phrase for the badge, e.g. "voice message"
```
`links.py`
```python
def permalink(entity_type: str, username: str|None, chat_id: int, message_id: int) -> str | None
def channel_short_id(chat_id: int) -> int      # abs(chat_id) - 1000000000000
```
`client.py`
```python
class TelegramService:
    def __init__(self, runtime_getter, settings)     # runtime is set late to avoid a cycle
    client: TelegramClient | None
    async def start(self) -> None                    # connect if credentials exist; never raises out
    async def stop(self) -> None
    async def is_authorized(self) -> bool
    async def send_code(self, api_id, api_hash, phone) -> dict        # {"ok":True,"code_sent":True}
    async def sign_in(self, code: str) -> dict                        # {"ok":True,"needs_password":bool}
    async def sign_in_password(self, password: str) -> dict
    async def logout(self) -> dict
    async def me(self) -> dict | None
    async def dialogs(self) -> list[dict]      # [{"chat_id","title","username","entity_type","is_channel"}]
    async def select_source(self, chat_id: int) -> dict
    async def register_handlers(self) -> None  # NewMessage, MessageEdited, Album, ChatAction — active source only
    async def unregister_handlers(self) -> None
    async def download_image(self, message, dest_dir: str) -> str | None
    async def fetch_newer(self, source, limit: int) -> list                 # catch-up
    async def get_reply_message(self, message)
```
Telethon errors map through `TELEGRAM_ERROR_MESSAGES: dict[type, str]`.

`ingest.py`
```python
class Ingestor:
    def __init__(self, runtime: Runtime)
    async def start(self) -> None ; async def stop(self) -> None
    async def handle_message(self, message, *, catchup: bool=False, album: list|None=None,
                             edited: bool=False) -> int | None     # returns Message.id or None if deduped
    async def enqueue(self, message_row_id: int) -> None
    async def worker(self) -> None                                  # one per source, strictly serial
    async def catch_up(self) -> int
    async def record_service(self, event) -> None
```
`notifier.py`
```python
class Notifier:
    def __init__(self, runtime: Runtime)
    async def start(self); async def stop(self)
    async def send(self, type: str, text: str, *, link: str|None=None, force: bool=False) -> bool
    def fire(self, type: str, text: str, *, link: str|None=None) -> None    # background task, never awaited by callers
    async def test_token(self, token: str) -> dict          # getMe
    async def detect_chat(self, token: str) -> dict         # getUpdates poll, admin-gated
    async def recent_senders(self, token: str) -> dict     # ~15s getUpdates poll, sends NO
                                                           # offset, so it confirms nothing
    # builders (locale-aware via app.telegram.notifier.render, locale from settings ui.locale)
    def render_opened(self, **kw) -> str ; render_modified ; render_closed ; render_rejected ; render_attention
```

## 16. `app/ai/*`

`base.py`
```python
@dataclass class ModelInfo: id:str; display_name:str; supports_vision:bool|None
                            supports_tools:bool|None; context_length:int|None; raw:dict
@dataclass class Capabilities: supports_vision:bool; supports_tools:bool
                               thinking_form:str|None; source:str
@dataclass class ToolSpec: name:str; description:str; input_schema:dict
Msg = dict           # {"role":"user"|"assistant","content":[Block,...]}
# Block kinds (NORMALIZED, provider-agnostic):
#  {"type":"text","text":str}
#  {"type":"image","media_type":str,"data":str}                     base64, no data: prefix
#  {"type":"thinking","thinking":str,"signature":str|None}
#  {"type":"redacted_thinking","data":str}
#  {"type":"tool_use","id":str,"name":str,"input":dict}
#  {"type":"tool_result","tool_use_id":str,"content":str,"is_error":bool}
@dataclass class ThinkingDelta: text:str
@dataclass class TextDelta: text:str
@dataclass class ToolCallDelta: index:int; id:str|None; name:str|None; partial_json:str
@dataclass class ToolCallDone: id:str; name:str; input:dict
@dataclass class UsageDelta: input_tokens:int; output_tokens:int
@dataclass class DoneDelta: stop_reason:str; assistant_blocks:list[dict]
Delta = ThinkingDelta|TextDelta|ToolCallDelta|ToolCallDone|UsageDelta|DoneDelta
class Provider(Protocol):
    kind: str
    async def list_models(self) -> list[ModelInfo]
    async def probe(self, model: str) -> Capabilities
    def run(self, *, system: str, messages: list[Msg], tools: list[ToolSpec],
            max_tokens: int, model: str, capabilities: Capabilities) -> AsyncIterator[Delta]
def normalize_base_url(url: str, *, default: str = "") -> str    # strips trailing / and a trailing /v1
def build_provider(kind: str, *, base_url: str, api_key: str,
                   extra_headers: dict[str,str] | None = None) -> Provider
```
`anthropic_provider.py` → `class AnthropicProvider`, `openai_provider.py` → `class OpenAICompatibleProvider`.
Both are constructed only through `build_provider`. `kind` values: `"anthropic"`, `"openai"`.

`catalog.py`
```python
async def list_models(kind, base_url, api_key, extra_headers) -> list[ModelInfo]
async def probe_model(session, kind, base_url, api_key, model, extra_headers,
                      *, force: bool=False) -> Capabilities            # DB-cached PROBE_CACHE_HOURS
async def effective_capabilities(session, kind, base_url, api_key, model, extra_headers) -> Capabilities
    # applies provider.supports_*_override
PROBE_PNG_B64: str        # a 1x1 PNG
PROBE_TOOL: ToolSpec
```
`tools.py`
```python
TOOL_SPECS: list[ToolSpec]            # every tool of §5.2/§5.3, in that order, finish last
READ_TOOL_NAMES: frozenset[str]; WRITE_TOOL_NAMES: frozenset[str]
def to_anthropic(specs) -> list[dict]
def to_openai(specs) -> list[dict]
def spec_by_name(name: str) -> ToolSpec | None
```
Tool names, exactly: `get_account`, `resolve_symbol`, `get_quote`, `price_from_distance`,
`evaluate_entry`, `get_positions`, `get_orders`, `get_candles`, `get_recent_signals`,
`get_channel_ladder_stats`,
`open_market`, `place_pending`, `modify_position`, `modify_order`, `close_position`,
`cancel_order`, `notify`, `finish`.
`open_market` / `place_pending` accept optional `runner_mode` ∈ {`be_after_cover`,
`hold_channel_sl`} and optional `runner_tp_index` (1-based numbered TP, at most ±1 from
suggested). Omitted on a ladder uses the server's `suggested_mode` / suggested index.
Classic 1–3 TP signals ignore both.

`prompts.py`
```python
SYSTEM_PROMPT: str                    # English, one place
def build_system_prompt(*, margin_mode: str, armed: bool, source_title: str,
                        limits: dict, currency: str, ladder_volume: float | None = None) -> str
async def build_context(session, runtime, message_row) -> tuple[list[Msg], dict]
    # -> (messages, meta) ; meta carries {"images_used","images_dropped","stale","signal_age_seconds",...}
    # Injects CHANNEL LADDER STATS from stats_for_source (server blurb, not history recount).
```
`loop.py`
```python
async def run_agent(runtime, *, message_row_id: int | None, kind: str = "signal",
                    instruction: str | None = None) -> dict
    # persists Run + RunStep rows, streams every Delta onto the bus, enforces MAX_TOOL_ITERATIONS,
    # forces a final tool-less turn on the cap, records "incomplete" when finish never came.
```

**Four requirements handed forward from the provider layer — all mandatory:**

1. **Echo `DoneDelta.assistant_blocks` back VERBATIM.** Append the list to the normalized history
   unmodified and in order. The Anthropic provider preserves each thinking block's `signature`, but
   only survives the round trip if the loop does not edit, reorder or drop any block. A mismatch is a
   hard 400 (BUILD_PROMPT §4.1).
2. **An unknown tool name must produce an error `tool_result`, never abort the run.** The OpenAI
   provider returns unparsable `function.arguments` as a synthetic tool call named
   `openai_provider.PARSE_ERROR_TOOL_NAME` (`"invalid_tool_arguments"`) whose `input.message`
   explains the parse failure. The loop must feed that back as a normal `is_error` tool result so the
   model retries — that is the whole point of parsing inside a `try` (BUILD_PROMPT §4.2).
3. **Handle `ProviderError(kind="context_window")` raised mid-stream.** It arrives on an HTTP **200**
   with `stop_reason: "model_context_window_exceeded"`. Treat it as a hard stop: trim the context,
   record the run, surface it. Do not let it look like a transport error (BUILD_PROMPT §4.1).
4. **Write tools are gated by `armed`, `notify` and `finish` are not.** Use
   `tools.WRITE_TOOL_NAMES` for the gate; a disarmed run must still stream complete reasoning and
   tool calls (BUILD_PROMPT §5.5).

## 17. `app/util/*`

`images.py`
```python
async def prepare_image(path: str, *, max_edge: int, quality: int, min_quality: int,
                        max_bytes: int) -> tuple[str, str, int, int]     # (b64, media_type, w, h)
def data_url(b64: str, media_type: str) -> str
async def image_dimensions(path: str) -> tuple[int,int]
```
`logging.py`
```python
SECRET_KEYS: tuple[str, ...]
def redact(text: str) -> str
def register_secret(value: str | None) -> None    # runtime-registered secrets are redacted too
def setup_logging(level: str) -> None             # structured, single line, JSON-ish key=value
def get_logger(name: str) -> logging.Logger
```

## 18. REST API (all under `/api`, all JSON, errors as `{"error":{"code","message","detail"}}`)

```
GET    /api/health                       -> {ok, version, uptime_s, status:{mt5,telegram,provider}, armed, needs_reentry:[]}

POST   /api/auth/login                   {username, password} -> {ok, username}  + Set-Cookie sp_auth
                                         401 {"error":{"code":"invalid_credentials",...}}
                                         429 {"error":{"code":"too_many_attempts","detail":{"retry_after":int}}}
POST   /api/auth/logout                  -> {ok}  + clears sp_auth
GET    /api/auth/status                  -> {authenticated, required, username|null}   (never gated)

GET    /api/settings                     -> masked settings tree (see below)
PUT    /api/settings/mt5                 {server, login, password?}
POST   /api/settings/mt5/test            -> {ok, login, name, server, balance, equity, currency, leverage, margin_mode}
PUT    /api/settings/provider            {kind, base_url, api_key?, model, extra_headers, max_tokens,
                                          supports_vision_override, supports_tools_override}
PUT    /api/settings/notifier            {bot_token?, chat_id, enabled, types, admin_ids}
POST   /api/settings/notifier/test       -> {ok, bot_username}
POST   /api/settings/notifier/detect     -> {ok, chat_id, chat_title, message, user_id,
                                             username, first_name, is_admin, enforced}
POST   /api/settings/notifier/whoami     -> {ok, updates:[{user_id, username, first_name, chat_id,
                                                           text, is_admin}], message, enforced}
PUT    /api/settings/limits              {max_volume_per_trade, max_open_positions, max_daily_loss_percent,
                                          symbol_allowlist, trading_hours}
PUT    /api/settings/general             {default_risk_percent, ladder_volume, locale}

POST   /api/telegram/credentials         {api_id, api_hash, phone}
POST   /api/telegram/send-code           -> {ok, code_sent}
POST   /api/telegram/sign-in             {code} -> {ok, needs_password}
POST   /api/telegram/password            {password} -> {ok}
POST   /api/telegram/logout              -> {ok}
GET    /api/telegram/status              -> {connected, authorized, user, source}
GET    /api/telegram/dialogs             -> {dialogs:[...]}
POST   /api/telegram/source              {chat_id} -> {ok, source}
GET    /api/telegram/image/{image_id}    -> image bytes

POST   /api/provider/test                {kind, base_url, api_key?, extra_headers} -> {ok, models:[...]}
POST   /api/provider/probe               {kind, base_url, api_key?, model, extra_headers, force?} -> Capabilities
GET    /api/provider/models              -> {models:[...]}   (last successful list, cached in settings)

GET    /api/trading/state                -> {armed, mt5, margin_mode, currency, ...}
POST   /api/trading/arm                  {armed} -> {ok, armed}
POST   /api/trading/kill-switch          {confirm:"KILL"} -> {ok, closed, cancelled, message}
GET    /api/trading/account              -> get_account() shape
GET    /api/trading/positions            -> {positions:[...]}
GET    /api/trading/orders               -> {orders:[...]}
GET    /api/trading/candles?symbol&timeframe&count
GET    /api/trading/symbols?query&only_tradable&limit
                                         -> {ok, symbols:[SymbolRow], total, truncated,
                                             account:{login,server}, mt5_unavailable, message}
                                         SymbolRow = {name, description, digits, point, pip_size,
                                             pip_convention, volume_min, volume_step, volume_max,
                                             stops_level_points, spread_points, tradable,
                                             path, currency_profit}
POST   /api/trading/manual/resolve-symbol   {query}
POST   /api/trading/manual/quote            {symbol}
POST   /api/trading/manual/price-from-distance {symbol, anchor, direction, distance, unit}
POST   /api/trading/manual/evaluate-entry   {symbol, side, signal_entry, sl, tp?}
POST   /api/trading/manual/open             {symbol, side, sl, tp[], volume?, risk_percent?, reason}
POST   /api/trading/manual/pending          {symbol, side, order_type, price, sl, tp[], expiry_minutes?, volume?, risk_percent?, reason}
POST   /api/trading/manual/modify-position  {ticket, sl?, tp?, reason}
POST   /api/trading/manual/modify-order     {ticket, price?, sl?, tp?, reason}
POST   /api/trading/manual/close            {ticket, volume?, reason}
POST   /api/trading/manual/cancel           {ticket, reason}
GET    /api/trading/signals?limit&status    -> {signals:[SignalOut]}
GET    /api/trading/signals/{id}            -> {signal, runs:[RunOut], trades:[TradeOut]}
GET    /api/trading/runs?limit              -> {runs:[RunOut]}
GET    /api/trading/runs/{run_id}           -> {run, steps:[RunStepOut]}

GET    /api/history/trades?date_from&date_to&symbol&limit -> {trades:[TradeOut]}
GET    /api/history/aggregates?source_id    -> {win_rate, avg_r, total_profit, count, by_symbol:[...]}
GET    /api/stream                          -> WebSocket
```
`GET /api/trading/symbols` is the connected account's own symbol table, and it exists so the
operator **picks** `limits.symbol_allowlist` from what the broker actually carries instead of
typing names from memory. It is a read, not a restriction: `symbol_allowlist` remains an
OPTIONAL operator limit that ships empty, an empty list means every symbol is allowed and the
limit is **not evaluated at all** (BUILD §6), and the three unconditional gates stay exactly
three — `armed`, the stop-loss rule, the duplicate-signal guard. `query` filters on name and
description case-insensitively and also matches across a broker suffix, so `XAUUSD` finds
`XAUUSDr`; `ALIASES` ranks next, so "gold" leads with `XAUUSDr` exactly as it does for the
model, and the broker's group is matched last. `limit` defaults to `SYMBOL_LIST_LIMIT_DEFAULT`
(300) and is capped at `SYMBOL_LIST_LIMIT_MAX` (1000) by the schema — a larger value is a 422.
`total` counts the whole match and `truncated` says when the page clipped it, because silently
returning the first N would teach the operator that a symbol does not exist. An unreachable
terminal answers **200** with `mt5_unavailable: true`, no rows and a message, like every other
read route here: the settings page must still render, since it is where MT5 gets fixed. When the
resolver's cache is still warm the rows are served anyway and `message` says they are the *last*
table read, so a cached list is never presented as a live one.

`PUT /api/settings/notifier` stores `admin_ids` de-duplicated with the order kept, and **rejects**
anything that is not a positive whole number with a 422 whose message names the field — a dropped
entry would leave the operator looking at a list missing the id they just typed. It is a whole-section
write: `admin_ids: []` turns enforcement off again, and a `chat_id` that differs from the stored one
clears `notifier.bound_to`. `POST /api/settings/notifier/whoami` exists because an allowlist nobody
can populate is an allowlist nobody turns on: a Telegram user id appears nowhere in the Telegram
client, so without it the answer to "what is my id" is a third-party bot. It polls `getUpdates` for
~15 s **without an `offset`**, so it confirms nothing and the updates `/notifier/detect` is waiting on
are still queued afterwards; it returns one row per recent sender, each flagged `is_admin` against the
allowlist as it stands.

`PUT /api/settings/limits` stores `symbol_allowlist` upper-cased and de-duplicated, order kept.
An empty list stays empty — nothing may turn "no entries" into an entry.

**Authentication.** When `SP_AUTH_USER` and `SP_AUTH_PASSWORD` are set, every route above and the
WebSocket require either the `sp_auth` cookie (HttpOnly, SameSite=Lax, Secure over TLS, HMAC-signed
with `SECRET_KEY`, 12 h) or an HTTP Basic header — Basic is *accepted* but never *demanded*, so no
`WWW-Authenticate` is ever sent and no browser shows its native prompt. Four deliberate exceptions:
the three `/api/auth/*` routes above, `/assets/*` (the SPA bundle the login page is built from), and
any unauthenticated navigation (`Accept: text/html` on a non-`/api` path), which is answered with the
SPA shell and 200 so the app can render its own login screen. An unauthenticated API call is 401 in
the standard envelope with `code: "unauthorized"`; an unauthenticated WebSocket is closed with 1008
before it is accepted. Eight failed logins from one client IP refuse that IP with 429 for 60 s.

`GET /api/settings` returns:
```json
{"mt5":{"server":"...","login":123,"password":"••••••••","has_password":true},
 "telegram":{"api_id":123,"api_hash":"••••••••","has_api_hash":true,"phone":"+1...","authorized":true,"user":{...},"source":{...}},
 "provider":{"kind":"anthropic","base_url":"...","api_key":"••••••••","has_api_key":true,"model":"...",
             "extra_headers":{},"max_tokens":8192,"supports_vision_override":null,"supports_tools_override":null,
             "capabilities":{...}},
 "notifier":{"bot_token":"••••••••","has_bot_token":true,"chat_id":123,"enabled":true,"types":{...},
             "admin_ids":[123456789],"bound_to":{"user_id":123456789,"username":"…","first_name":"…"}},
 "limits":{...},"general":{"default_risk_percent":1.0,"ladder_volume":0.10,"locale":"en",
           "ladder_stats":{"source_id":null,"source_title":"","window":5,"sample_size":0,
             "raw_sl_hits":0,"be_stopouts":0,"tp4_plus":0,"clean_streak":0,
             "suggested_mode":"be_after_cover","bootstrap":true,
             "suggested_runner_tp_bias":0,"blurb":"..."}},
 "needs_reentry":["provider.api_key"]}
```
Every secret field is `MASK` with a sibling `has_<field>: bool`. A PUT that omits a secret field or sends
`MASK` leaves the stored value untouched.

## 19. WebSocket `/api/stream`

Server sends the raw event envelope of §4. On connect it first sends
`{"type":"hello","ts":..,"data":{"status":{...},"armed":bool,"recent":[...last 100 events...]}}`.
Client sends `{"type":"ping"}`; server replies `{"type":"pong"}`. No other client messages.

## 20. Frontend contracts

* `src/api/client.ts` — `api.get/post/put<T>(path, body?)`, throws `ApiError {status, code, message, detail}`.
  Exports typed functions mirroring §18 exactly, plus the TS types `SignalOut`, `RunOut`, `RunStepOut`,
  `TradeOut`, `AccountOut`, `SettingsOut`, `ModelInfo`, `Capabilities`, `StreamEvent`.
* `src/api/stream.ts` — `createStream(onEvent, onStatus)` with backoff 1s→30s (×1.8, jitter),
  refetches REST state on every (re)connect.
* `src/i18n/index.ts` — `t(key, vars?)`, `useLocale()`, `setLocale(l)`, `formatNumber`, `formatPrice`,
  `formatCurrency`, `formatDateTime`, `formatRelative`. Missing key renders the key in `--warn`.
  `ru.ts`/`en.ts` export `const dict: Record<string,string>` with identical key sets (enforced by a TS type).
* `src/styles/tokens.css` — the only file with colour literals; both `[data-theme]` blocks.
* Router: `/` Cockpit, `/positions`, `/history`, `/settings`.
* Every number renders through `.mono` (`font-variant-numeric: tabular-nums`).
* Every BUY/SELL indicator prints the word or an arrow next to the colour.

## 21. Verification harness (`backend/tests/`)

Pytest, no network, no MT5. A `FakeSidecar` implements the RPC surface in-process so
`executor.py`, `pricing.py`, `symbols.py`, `guards.py` and `loop.py` are testable.
Tests must cover acceptance criteria 3,4,5,6,7,8,9,10,11,12,15 at unit level.
