/**
 * Typed fetch wrapper for the LV Copytrader REST API.
 *
 * Every route of CONTRACTS §18 has exactly one exported function here, and every JSON shape
 * it moves has exactly one exported type. Nothing in this file is invented: the field names
 * mirror `backend/app/schemas.py`, which is the authoritative projection of CONTRACTS.
 *
 * Error model (CONTRACTS §20): every failure surfaces as an `ApiError`.
 *   - the backend envelope `{"error":{"code","message","detail"}}` is unpacked into it verbatim;
 *   - a FastAPI validation body `{"detail":[...]}` becomes `code: "validation_error"` with the
 *     first field error as the message, so the Settings page can print something actionable;
 *   - a non-JSON body (a proxy's HTML 502, a gateway text page) becomes the first 200 characters;
 *   - a transport failure — DNS, offline, TLS, connection reset — becomes `status: 0`,
 *     `code: "network"`; an aborted request becomes `status: 0`, `code: "aborted"`.
 *
 * Secrets (CONTRACTS §18, BRIEF): `GET /api/settings` never returns a secret. It returns
 * `MASK` with a sibling `has_<field>` boolean. Sending `MASK` back in a PUT — or omitting the
 * field, or sending `null` — means "leave the stored value unchanged". An empty string clears it.
 * Use `isMasked()` before treating a settings value as something the operator typed.
 */

// ---------------------------------------------------------------------------
// Secret masking
// ---------------------------------------------------------------------------

/** The eight bullets every secret field renders as. Mirrors `app.crypto.MASK`. */
export const MASK = '••••••••';

/**
 * True when a value is the display mask rather than a real secret.
 *
 * A form must not treat the mask as user input: send it back untouched to leave the stored
 * secret alone, and only send a different string when the operator actually retyped one.
 */
export function isMasked(v: unknown): boolean {
  return typeof v === 'string' && v.trim() === MASK;
}

// ---------------------------------------------------------------------------
// Closed string sets (CONTRACTS §1.2, §3.2, §7, schemas.py)
// ---------------------------------------------------------------------------

/** Which vendor dialect the provider speaks. */
export type ProviderKind = 'anthropic' | 'openai';
/** Direction of a trade. Never rendered by colour alone (BUILD §11). */
export type Side = 'buy' | 'sell';
/** After a ladder cover (1R) closes: trail the runner to BE, or leave the channel stop. */
export type RunnerMode = 'be_after_cover' | 'hold_channel_sl';
/** The two pending-order families; the executor derives the MT5 enum from side + price. */
export type PendingKind = 'limit' | 'stop';
/** Which way `price_from_distance` walks away from its anchor. */
export type Direction = 'above' | 'below';
/** The units a distance may be expressed in (BUILD §2). */
export type DistanceUnit = 'pips' | 'points' | 'price' | 'percent';
/** MT5 timeframes, bit-packed server-side — never computed from minutes. */
export type TimeframeName = 'M1' | 'M5' | 'M15' | 'M30' | 'H1' | 'H4' | 'D1';
/** UI and notification locale. */
export type LocaleName = 'en' | 'ru';
/** Lifecycle of an ingested message row. */
export type MessageStatus =
  'pending' | 'processing' | 'processed' | 'skipped' | 'error';
/** Classified message kind; the set is closed so the feed never shows a raw class name. */
export type MessageKind =
  | 'text'
  | 'photo'
  | 'photo_caption'
  | 'voice'
  | 'video'
  | 'video_note'
  | 'audio'
  | 'sticker'
  | 'gif'
  | 'document'
  | 'service'
  | 'empty'
  | 'unsupported';
/** Lifecycle of an agent run. `incomplete` means `finish` was never called (BUILD §5.3). */
export type RunStatus = 'running' | 'finished' | 'incomplete' | 'error';
/** What the run decided, as reported by the mandatory `finish` call. */
export type RunDecision = 'acted' | 'ignored' | 'deferred';
/** What triggered a run. */
export type RunKind = 'signal' | 'sl_watchdog' | 'manual';
/** The kinds of persisted run step. */
export type RunStepType =
  'thinking' | 'text' | 'tool_call' | 'tool_result' | 'error' | 'provider';
/** Whether a trade row tracks a position or a resting order. */
export type TradeKind = 'position' | 'order';
/** Lifecycle of a trade row. */
export type TradeStatus = 'open' | 'pending' | 'closed' | 'cancelled';
/** Why a trade ended. */
export type CloseCause =
  'tp' | 'sl' | 'ai' | 'manual' | 'kill_switch' | 'unknown';
/** How the broker account nets exposure; decides whether multi-TP splitting is legal. */
export type MarginModeName = 'netting' | 'exchange' | 'hedging';
/** The three status dots. */
export type ComponentName = 'mt5' | 'telegram' | 'provider';
/** Notification categories, each with its own switch in settings (BUILD §7). */
export type NotifyType =
  'opened' | 'modified' | 'closed' | 'rejected' | 'attention';
/** Sections of the settings tree, as reported by the `settings.changed` event. */
export type SettingsSection =
  | 'mt5'
  | 'telegram'
  | 'provider'
  | 'notifier'
  | 'limits'
  | 'general'
  | 'trading';

// ---------------------------------------------------------------------------
// Small shared shapes
// ---------------------------------------------------------------------------

/** One status dot: green when `ok`, otherwise `error` is the sentence to show. */
export interface ComponentStatus {
  ok: boolean;
  error: string | null;
}

/** The three dots the cockpit header renders. */
export interface StatusMap {
  mt5: ComponentStatus;
  telegram: ComponentStatus;
  provider: ComponentStatus;
}

/** A distance reported in all three units so the UI never converts (BUILD §2). */
export interface Distance {
  price: number;
  points: number;
  pips: number;
}

/** Per-type notification switches (settings key `notifier.types`). */
export interface NotifyTypes {
  opened: boolean;
  modified: boolean;
  closed: boolean;
  rejected: boolean;
  attention: boolean;
}

/**
 * One allowed trading window. Serialized with the alias `from`, not `from_`.
 * The list ships empty, which means "always allowed" and is not evaluated at all.
 */
export interface TradingWindow {
  /** Monday = 0 .. Sunday = 6. */
  days: number[];
  /** Window start, HH:MM on a 24-hour clock. */
  from: string;
  /** Window end, HH:MM on a 24-hour clock. */
  to: string;
  /** IANA timezone name, default `UTC`. */
  tz: string;
}

/** Minimal acknowledgement returned by routes with nothing else to report. */
export interface OkResponse {
  ok: boolean;
  message: string;
}

// ---------------------------------------------------------------------------
// Auth — the sign-in gate in front of everything else
// ---------------------------------------------------------------------------

/**
 * `GET /api/auth/status` — the only route that answers while signed out.
 *
 * `required` is false when the deployment runs with no credentials configured
 * (localhost, single-user, no tunnel). The dashboard then never shows the login
 * page at all; it does NOT invent a lock the backend is not enforcing.
 */
export interface AuthStatusOut {
  authenticated: boolean;
  required: boolean;
  username: string | null;
}

/** `POST /api/auth/login` body. Sent once; the cookie carries every later request. */
export interface LoginInput {
  username: string;
  password: string;
}

/** `POST /api/auth/login` on success. The session itself lives in an HttpOnly cookie. */
export interface LoginOut {
  ok: boolean;
  username: string;
}

/** `POST /api/auth/logout` — clears the cookie server-side. */
export interface LogoutOut {
  ok: boolean;
}

/** How deep `retryAfterSeconds` will look for its number before giving up. */
const RETRY_AFTER_MAX_DEPTH = 4;

function readRetryAfter(node: unknown, depth: number): number | null {
  if (typeof node === 'number')
    return Number.isFinite(node) && node >= 0 ? node : null;
  if (typeof node === 'string') {
    // Either a bare "30", or the JSON body of a backend that answered outside the
    // error envelope and reached us as a text snippet.
    const asNumber = Number(node.trim());
    if (node.trim() !== '' && Number.isFinite(asNumber) && asNumber >= 0)
      return asNumber;
    if (depth >= RETRY_AFTER_MAX_DEPTH) return null;
    try {
      return readRetryAfter(JSON.parse(node) as unknown, depth + 1);
    } catch {
      return null;
    }
  }
  if (
    depth >= RETRY_AFTER_MAX_DEPTH ||
    typeof node !== 'object' ||
    node === null
  )
    return null;
  const record = node as Record<string, unknown>;
  for (const key of ['retry_after', 'retryAfter', 'retry_after_s', 'seconds']) {
    if (key in record) {
      const hit = readRetryAfter(record[key], depth + 1);
      if (hit !== null) return hit;
    }
  }
  // Only nested STRUCTURES are searched further. Walking into bare numbers would
  // happily return a `limit` or an `attempts` count as if it were a duration.
  for (const value of Object.values(record)) {
    if (typeof value !== 'object' || value === null) continue;
    const hit = readRetryAfter(value, depth + 1);
    if (hit !== null) return hit;
  }
  return null;
}

/**
 * The seconds a 429 asks the caller to wait, or null when it did not say.
 *
 * Deliberately tolerant about WHERE the number sits. A rate limit reaches the
 * browser as `{"error":{"detail":{"retry_after":30}}}` through the app's error
 * envelope, as `{"detail":{"retry_after":30}}` from a middleware that answered
 * before the handlers, and as a plain integer from a hand-rolled body. All three
 * mean the same thing to the operator staring at a locked form, so all three are
 * read rather than one being blessed and the others rendering "try again later".
 */
export function retryAfterSeconds(error: unknown): number | null {
  if (!isApiError(error)) return null;
  const found = readRetryAfter(error.detail, 0);
  if (found === null) return null;
  return Math.max(0, Math.round(found));
}

// ---------------------------------------------------------------------------
// GET /api/health
// ---------------------------------------------------------------------------

/** Liveness plus everything the header needs on first paint. */
export interface HealthOut {
  ok: boolean;
  version: string;
  uptime_s: number;
  status: StatusMap;
  armed: boolean;
  /** Settings keys whose ciphertext no longer decrypts; the banner names them (BRIEF). */
  needs_reentry: string[];
}

// ---------------------------------------------------------------------------
// Settings — response tree
// ---------------------------------------------------------------------------

/** MT5 section of `GET /api/settings`; `password` is always `MASK` or null. */
export interface Mt5SettingsOut {
  server: string;
  login: number | null;
  password: string | null;
  has_password: boolean;
}

/** The logged-in Telegram user account. */
export interface TelegramUserOut {
  id: number;
  username: string | null;
  first_name: string | null;
  last_name: string | null;
  phone: string | null;
}

/** A signal source row; exactly one is active at a time (ASSUMPTIONS). */
export interface SourceOut {
  id: number;
  kind: string;
  chat_id: number;
  title: string;
  username: string | null;
  entity_type: string;
  is_active: boolean;
  last_message_id: number;
  created_at: string | null;
}

/** One entry of the channel picker. */
export interface DialogOut {
  chat_id: number;
  title: string;
  username: string | null;
  entity_type: string;
  is_channel: boolean;
}

/** Telegram section of `GET /api/settings`; `api_hash` is always `MASK` or null. */
export interface TelegramSettingsOut {
  api_id: number | null;
  api_hash: string | null;
  has_api_hash: boolean;
  phone: string | null;
  authorized: boolean;
  user: TelegramUserOut | null;
  source: SourceOut | null;
}

/** What a model can actually do — declared by the provider or probed once, then cached. */
export interface Capabilities {
  supports_vision: boolean;
  supports_tools: boolean;
  /** `adaptive`, `enabled`, or null when the model has no thinking mode (BUILD §4.1). */
  thinking_form: string | null;
  /** `declared` when read from the model list, `probed` when settled by a live request. */
  source: string;
}

/** One row of the model picker. Only `id` is guaranteed by every gateway (BUILD §4.3). */
export interface ModelInfo {
  id: string;
  display_name: string;
  supports_vision: boolean | null;
  supports_tools: boolean | null;
  context_length: number | null;
  /** The provider's own entry, unnormalized. Free-form by definition. */
  raw: Record<string, unknown>;
}

/** Provider section of `GET /api/settings`; `api_key` is always `MASK` or null. */
export interface ProviderSettingsOut {
  kind: string;
  base_url: string;
  api_key: string | null;
  has_api_key: boolean;
  model: string;
  extra_headers: Record<string, string>;
  max_tokens: number;
  supports_vision_override: boolean | null;
  supports_tools_override: boolean | null;
  capabilities: Capabilities | null;
}

/**
 * The Telegram account the notification bot currently answers to.
 *
 * `first_name` and `username` are whatever Telegram had at bind time; either can
 * be empty, because a Telegram account is required to have neither.
 */
export interface NotifierBinding {
  user_id: number;
  username: string;
  first_name: string;
}

/** Notifier section of `GET /api/settings`; `bot_token` is always `MASK` or null. */
export interface NotifierSettingsOut {
  bot_token: string | null;
  has_bot_token: boolean;
  chat_id: number | null;
  enabled: boolean;
  types: NotifyTypes;
  /**
   * Telegram user ids allowed to bind this bot and receive notifications.
   *
   * SHIPS EMPTY, and empty is not a restriction: whoever messages the bot first
   * binds it and starts receiving the operator's trade flow. Not a secret — an
   * id is public the moment its owner sends a message — so it is returned in
   * full, unmasked, and the UI prints it.
   */
  admin_ids: number[];
  /** Who the bot answers to today, or `null` when nobody has claimed it. */
  bound_to: NotifierBinding | null;
}

/** The optional operator limits as stored. Null and empty both mean "not evaluated" (BUILD §6). */
export interface LimitsOut {
  max_volume_per_trade: number | null;
  max_open_positions: number | null;
  max_daily_loss_percent: number | null;
  symbol_allowlist: string[];
  trading_hours: TradingWindow[];
}

/** Last-N closed-ladder aggregates for the active source (server-computed). */
export interface LadderStatsOut {
  source_id: number | null;
  source_title: string;
  window: number;
  sample_size: number;
  raw_sl_hits: number;
  be_stopouts: number;
  tp4_plus: number;
  clean_streak: number;
  suggested_mode: 'be_after_cover' | 'hold_channel_sl' | string;
  bootstrap: boolean;
  /** 0 / +1 / +2 added to ceil((N+1)/2); never random. */
  suggested_runner_tp_bias?: number;
  blurb: string;
}

/** Non-credential defaults. */
export interface GeneralSettingsOut {
  default_risk_percent: number;
  ladder_volume: number;
  locale: string;
  /** Classic signals: how many orders to open when the model omits a size. Default 2. */
  orders_per_signal: number;
  /** Lot size of each of those orders. Default 0.2. */
  order_lot_size: number;
  /**
   * When true (default), a classic signal ignores the model's risk percent and
   * opens `orders_per_signal` orders of `order_lot_size`. When false, that
   * percent sizes the trade again. An explicit volume is used either way.
   */
  use_fixed_order_lots: boolean;
  /**
   * Classic gold entries skip for "price already moved" only past this many
   * price units from the channel entry or zone. At or inside it they must open.
   * Default 5.
   */
  max_entry_drift_usd: number;
  /** Move remaining classic orders to breakeven after TP1. Default on. */
  be_after_tp1: boolean;
  /** Channel playbook generation. Default off. */
  reviewer_enabled: boolean;
  /** Drop Telegram posts that do not contain `tag_filter_tag`. Default off. */
  tag_filter_enabled: boolean;
  /** Case-insensitive substring. Default `#soft`. */
  tag_filter_tag: string;
  /** Standing LLM prompt shown in the editor (built-in text when nothing custom is stored). */
  system_prompt: string;
  /** Built-in prompt, so the editor can reset. */
  system_prompt_default: string;
  system_prompt_custom: boolean;
  /** Present on GET; omitted from PUT. Last-N ladder stats for the active source. */
  ladder_stats?: LadderStatsOut | null;
}

/** `GET /api/settings` — the whole masked settings tree. */
export interface SettingsOut {
  mt5: Mt5SettingsOut;
  telegram: TelegramSettingsOut;
  provider: ProviderSettingsOut;
  notifier: NotifierSettingsOut;
  limits: LimitsOut;
  general: GeneralSettingsOut;
  needs_reentry: string[];
}

// ---------------------------------------------------------------------------
// Settings — request bodies
// ---------------------------------------------------------------------------

/**
 * `PUT /api/settings/mt5`.
 *
 * `password`: omit it, send `null`, or echo back `MASK` to leave the stored password
 * unchanged. An empty string clears it.
 */
export interface Mt5SettingsInput {
  server: string;
  login: number;
  password?: string | null;
}

/**
 * `PUT /api/settings/provider`.
 *
 * `api_key`: omit it, send `null`, or echo back `MASK` to leave the stored key unchanged.
 * An empty string clears it. `base_url` is normalized server-side, so `host`, `host/` and
 * `host/v1` all store the same value (BUILD §4.3).
 */
export interface ProviderSettingsInput {
  kind: ProviderKind;
  base_url?: string;
  api_key?: string | null;
  model: string;
  extra_headers?: Record<string, string>;
  max_tokens?: number;
  supports_vision_override?: boolean | null;
  supports_tools_override?: boolean | null;
}

/**
 * `PUT /api/settings/notifier`.
 *
 * `bot_token`: omit it, send `null`, or echo back `MASK` to leave the stored token unchanged.
 * An empty string clears it.
 */
export interface NotifierSettingsInput {
  bot_token?: string | null;
  chat_id?: number | null;
  enabled?: boolean;
  types?: NotifyTypes;
  /**
   * The whole allowed list, replacing what is stored — this is a PUT, not a
   * patch, so an omitted list leaves the stored one alone and an empty array
   * clears it. The settings page always sends it.
   */
  admin_ids?: number[];
}

/** `PUT /api/settings/limits` — every field null or empty means the check never runs. */
export interface LimitsInput {
  max_volume_per_trade?: number | null;
  max_open_positions?: number | null;
  max_daily_loss_percent?: number | null;
  symbol_allowlist?: string[];
  trading_hours?: TradingWindow[];
}

/** `PUT /api/settings/general`. `default_risk_percent` has no upper bound by design (BUILD §2).
 * `ladder_volume` is the fixed lot size for multi-TP ladders (not a percent of balance).
 * `orders_per_signal` and `order_lot_size` size classic signals when the model omits a size.
 * `use_fixed_order_lots` (default on) ignores a model risk percent on that path.
 * `max_entry_drift_usd` (default 5) is the classic gold "price already moved" limit. */
export interface GeneralSettingsInput {
  default_risk_percent: number;
  ladder_volume: number;
  locale?: LocaleName;
  orders_per_signal?: number;
  order_lot_size?: number;
  use_fixed_order_lots?: boolean;
  max_entry_drift_usd?: number;
  be_after_tp1?: boolean;
  reviewer_enabled?: boolean;
  tag_filter_enabled?: boolean;
  tag_filter_tag?: string;
  /** Empty, or text identical to the built-in prompt, keeps the built-in text. */
  system_prompt?: string;
}

/** `POST /api/settings/mt5/test` — the real account behind the stored credentials. */
export interface Mt5TestOut {
  ok: boolean;
  login: number | null;
  name: string;
  server: string;
  balance: number;
  equity: number;
  currency: string;
  leverage: number;
  margin_mode: string;
  message: string;
}

/** `POST /api/settings/notifier/test` — `getMe` on the operator's bot. */
export interface NotifierTestOut {
  ok: boolean;
  bot_username: string;
  message: string;
}

/**
 * `POST /api/settings/notifier/detect` — chat id filled from the first incoming message.
 *
 * `user_id` and `username` name the *person* behind that chat, which is what the
 * admin list is keyed on; both are absent on a timeout, where `ok` is false.
 */
export interface NotifierDetectOut {
  ok: boolean;
  chat_id: number | null;
  chat_title: string;
  user_id: number | null;
  username: string;
  message: string;
}

/** One recent sender, as `POST /api/settings/notifier/whoami` reports them. */
export interface NotifierWhoamiUpdate {
  user_id: number;
  username: string;
  first_name: string;
  chat_id: number;
  /** What they wrote, so one sender is told apart from the next. May be empty. */
  text: string;
  /** The server's view of `admin_ids` at the moment of the call. */
  is_admin: boolean;
}

/** `POST /api/settings/notifier/whoami` — everyone who has written to the bot lately. */
export interface NotifierWhoamiOut {
  ok: boolean;
  updates: NotifierWhoamiUpdate[];
  message: string;
}

// ---------------------------------------------------------------------------
// Telegram account
// ---------------------------------------------------------------------------

/**
 * `POST /api/telegram/credentials`.
 *
 * `api_hash`: omit it, send `null`, or echo back `MASK` to leave the stored hash unchanged.
 * `phone` must be E.164 — a leading `+`, a non-zero country digit, then 6..14 more digits.
 */
export interface TelegramCredentialsInput {
  api_id: number;
  api_hash?: string | null;
  phone: string;
}

/** `POST /api/telegram/sign-in`. */
export interface TelegramSignInInput {
  code: string;
}

/** `POST /api/telegram/password` — the two-factor cloud password. */
export interface TelegramPasswordInput {
  password: string;
}

/** `POST /api/telegram/source` — pick the one active signal channel. */
export interface SourceSelectInput {
  chat_id: number;
}

/** `POST /api/telegram/send-code`. */
export interface SendCodeOut {
  ok: boolean;
  code_sent: boolean;
  message: string;
}

/** `POST /api/telegram/sign-in`; `needs_password` means 2FA is on and the next call is the password. */
export interface SignInOut {
  ok: boolean;
  needs_password: boolean;
  message: string;
}

/** `GET /api/telegram/status`. */
export interface TelegramStatusOut {
  connected: boolean;
  authorized: boolean;
  user: TelegramUserOut | null;
  source: SourceOut | null;
  error: string | null;
}

/** `GET /api/telegram/dialogs`. */
export interface DialogsOut {
  dialogs: DialogOut[];
}

/** `POST /api/telegram/source`. */
export interface SourceSelectOut {
  ok: boolean;
  source: SourceOut | null;
  message: string;
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

/**
 * `POST /api/provider/test` — list the models these connection details can reach.
 *
 * `api_key`: omit it, send `null`, or echo back `MASK` to reuse the stored key, so the
 * operator can re-test without retyping it.
 */
export interface ProviderTestInput {
  kind: ProviderKind;
  base_url?: string;
  api_key?: string | null;
  extra_headers?: Record<string, string>;
}

/**
 * `POST /api/provider/probe` — one throwaway request that settles vision and tools.
 *
 * `force` ignores the `PROBE_CACHE_HOURS` cache and asks again.
 */
export interface ProviderProbeInput {
  kind: ProviderKind;
  base_url?: string;
  api_key?: string | null;
  model: string;
  extra_headers?: Record<string, string>;
  force?: boolean;
}

/** `POST /api/provider/test`. */
export interface ProviderTestOut {
  ok: boolean;
  models: ModelInfo[];
  message: string;
}

/** `GET /api/provider/models` — the last successful listing, cached in settings. */
export interface ProviderModelsOut {
  models: ModelInfo[];
}

// ---------------------------------------------------------------------------
// Trading — state and control
// ---------------------------------------------------------------------------

/** `POST /api/trading/arm`. */
export interface ArmInput {
  armed: boolean;
}

/** `POST /api/trading/kill-switch` — the confirmation string is literally `KILL`. */
export interface KillSwitchInput {
  confirm: 'KILL';
}

/** `POST /api/trading/arm`. */
export interface ArmOut {
  ok: boolean;
  armed: boolean;
}

/** `POST /api/trading/kill-switch` — closes everything with our magic, then disarms. */
export interface KillSwitchOut {
  ok: boolean;
  closed: number;
  cancelled: number;
  message: string;
}

/** `GET /api/trading/state` — everything the cockpit chrome needs in one call. */
export interface TradingStateOut {
  armed: boolean;
  mt5: ComponentStatus;
  telegram: ComponentStatus;
  provider: ComponentStatus;
  margin_mode: string | null;
  currency: string | null;
  default_risk_percent: number;
  ladder_volume: number;
  source: SourceOut | null;
  last_reconcile_at: string | null;
  needs_reentry: string[];
}

/** `GET /api/trading/account` — the `get_account()` shape (BUILD §5.2). */
export interface AccountOut {
  login: number | null;
  name: string;
  server: string;
  currency: string;
  leverage: number;
  balance: number;
  equity: number;
  margin: number;
  margin_free: number;
  margin_level: number;
  /** Floating P/L on open positions. */
  profit: number;
  margin_mode: string;
  open_positions: number;
  pending_orders: number;
  /** Realized P/L since midnight broker-server time (BUILD §2). */
  today_profit: number;
  server_time: string | null;
}

// ---------------------------------------------------------------------------
// Trading — live rows
// ---------------------------------------------------------------------------

/** One live MT5 position joined to the signal that created it. */
export interface PositionOut {
  ticket: number;
  symbol: string;
  side: Side;
  order_type: number;
  volume: number;
  price_open: number;
  price_current: number;
  sl: number;
  tp: number;
  profit: number;
  swap: number;
  commission: number;
  magic: number;
  comment: string;
  /** A foreign magic: display read-only, never modify or close, kill switch included (BUILD §8). */
  external: boolean;
  digits: number;
  pip_size: number;
  sl_distance: Distance | null;
  tp_distance: Distance | null;
  trade_id: number | null;
  run_id: string | null;
  signal_ref: string | null;
  signal_excerpt: string | null;
  message_row_id: number | null;
  permalink: string | null;
  opened_at_ts: number | null;
  ladder_role?: string | null;
  runner_mode?: RunnerMode | string | null;
  suggested_runner_mode?: RunnerMode | string | null;
  runner_tp_bias?: number | null;
  suggested_runner_tp_index?: number | null;
  chosen_runner_tp_index?: number | null;
}

/** One live MT5 pending order joined to the signal that created it. */
export interface OrderOut {
  ticket: number;
  symbol: string;
  side: Side;
  order_type: number;
  order_type_name: string;
  volume_current: number;
  volume_initial: number;
  price_open: number;
  price_current: number;
  sl: number;
  tp: number;
  magic: number;
  comment: string;
  external: boolean;
  digits: number;
  pip_size: number;
  sl_distance: Distance | null;
  tp_distance: Distance | null;
  trade_id: number | null;
  run_id: string | null;
  signal_ref: string | null;
  signal_excerpt: string | null;
  message_row_id: number | null;
  permalink: string | null;
  time_setup_ts: number | null;
  expiration_ts: number | null;
}

/** `GET /api/trading/positions`. */
export interface PositionsOut {
  ok: boolean;
  positions: PositionOut[];
}

/** `GET /api/trading/orders`. */
export interface OrdersOut {
  ok: boolean;
  orders: OrderOut[];
}

/** One OHLC bar. */
export interface CandleOut {
  /** Bar open time as unix seconds, broker-server clock. */
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  tick_volume: number;
  spread: number;
  real_volume: number;
}

/** `GET /api/trading/candles`; `count` is capped at `MAX_CANDLES = 200` server-side. */
export interface CandlesOut {
  ok: boolean;
  symbol: string;
  timeframe: string;
  count: number;
  candles: CandleOut[];
  message: string;
}

/** Query parameters of `GET /api/trading/candles`. */
export interface CandlesQuery {
  symbol: string;
  timeframe?: TimeframeName;
  count?: number;
}

// ---------------------------------------------------------------------------
// Trading — the broker's symbol table (`GET /api/trading/symbols`)
// ---------------------------------------------------------------------------

/**
 * One instrument exactly as the connected MT5 account offers it.
 *
 * A narrower projection of `SymbolRef.to_dict()` than `SymbolCandidateOut`: it carries
 * what an operator needs to judge a symbol — how big a pip is and which convention
 * produced it (BUILD §2), the live spread in points, and the smallest lot the broker
 * accepts — plus `path`, the broker's own folder, so `Forex\Majors` and `Crypto` are
 * distinguishable at a glance in a list of well over a thousand names.
 *
 * `tradable` mirrors `SymbolRef.tradable` — `trade_mode == SYMBOL_TRADE_MODE_FULL`
 * (CONTRACTS §10). False means the broker will not accept an order on this symbol from
 * this account, so a picker must render it disabled rather than let it be chosen.
 */
export interface SymbolRow {
  name: string;
  description: string;
  digits: number;
  point: number;
  pip_size: number;
  /** Which branch of the pip table fired, so the convention is auditable (BUILD §2). */
  pip_convention: string;
  volume_min: number;
  volume_step: number;
  volume_max: number;
  stops_level_points: number;
  spread_points: number;
  tradable: boolean;
  /** The broker's own grouping, backslash-separated, e.g. `Forex\Majors`. */
  path: string;
  currency_profit: string;
}

/** Which account the table was read from, so a symbol list is never anonymous. */
export interface SymbolsAccountOut {
  login: number | null;
  server: string;
}

/** Query parameters of `GET /api/trading/symbols`. */
export interface SymbolsQuery {
  /** Matched against name, description and path, case-insensitively. Empty lists from the top. */
  query?: string;
  /** True drops the symbols the broker will not trade instead of returning them flagged. */
  only_tradable?: boolean;
  /** Rows per response — the account carries well over a thousand symbols. */
  limit?: number;
}

/**
 * `GET /api/trading/symbols` — the live symbol table, filtered and capped.
 *
 * `total` counts every match, `symbols` carries at most `limit` of them, and `truncated`
 * says so out loud: a picker that silently shows 300 of 1284 teaches its operator that a
 * symbol does not exist when it was simply never sent.
 *
 * `mt5_unavailable` is the honest empty — the terminal is not connected, so the table
 * could not be read at all. It is NOT an account with no symbols, and the UI must never
 * render it as one.
 */
export interface SymbolsOut {
  ok: boolean;
  symbols: SymbolRow[];
  total: number;
  truncated: boolean;
  account: SymbolsAccountOut | null;
  mt5_unavailable: boolean;
}

// ---------------------------------------------------------------------------
// Trading — manual override request bodies (the same arguments the tools take)
// ---------------------------------------------------------------------------

/** `POST /api/trading/manual/resolve-symbol`. */
export interface ManualResolveSymbolInput {
  /** What the signal called it, e.g. `gold`. */
  query: string;
}

/** `POST /api/trading/manual/quote`. */
export interface ManualQuoteInput {
  symbol: string;
}

/** `POST /api/trading/manual/price-from-distance` — the server does every multiplication. */
export interface ManualPriceFromDistanceInput {
  symbol: string;
  anchor: number;
  direction: Direction;
  distance: number;
  unit: DistanceUnit;
}

/** `POST /api/trading/manual/evaluate-entry` — the late-entry instrument (BUILD §5.2). */
export interface ManualEvaluateEntryInput {
  symbol: string;
  side: Side;
  signal_entry: number;
  sl: number;
  tp?: number[];
}

/**
 * `POST /api/trading/manual/open` — market order.
 *
 * Exactly one of `volume` / `risk_percent`, or neither, in which case `DEFAULT_RISK_PERCENT`
 * applies. `sl` is required; a zero reaches the stop-loss guard, which answers with its own
 * refusal instead of a validation error (BUILD §6). `tp` is a list of up to `MAX_TP_SPLITS`.
 */
export interface ManualOpenInput {
  symbol: string;
  side: Side;
  sl: number;
  tp?: number[];
  volume?: number | null;
  risk_percent?: number | null;
  /** Ladder only. Omit to use the server's suggested_mode. */
  runner_mode?: RunnerMode | null;
  /** Ladder only. 1-based numbered TP; omit to follow suggested; ±1 max. */
  runner_tp_index?: number | null;
  reason: string;
}

/** `POST /api/trading/manual/pending` — limit or stop order; same sizing rules as `open`. */
export interface ManualPendingInput {
  symbol: string;
  side: Side;
  order_type: PendingKind;
  price: number;
  sl: number;
  tp?: number[];
  expiry_minutes?: number | null;
  volume?: number | null;
  risk_percent?: number | null;
  runner_mode?: RunnerMode | null;
  runner_tp_index?: number | null;
  reason: string;
}

/** `POST /api/trading/manual/modify-position`. `sl: 0` is refused — a stop cannot be removed. */
export interface ManualModifyPositionInput {
  ticket: number;
  sl?: number | null;
  tp?: number | null;
  reason: string;
}

/** `POST /api/trading/manual/modify-order`. `sl: 0` is refused — a stop cannot be removed. */
export interface ManualModifyOrderInput {
  ticket: number;
  price?: number | null;
  sl?: number | null;
  tp?: number | null;
  reason: string;
}

/** `POST /api/trading/manual/close` — null volume closes the whole position. */
export interface ManualCloseInput {
  ticket: number;
  volume?: number | null;
  reason: string;
}

/** `POST /api/trading/manual/cancel`. */
export interface ManualCancelInput {
  ticket: number;
  reason: string;
}

// ---------------------------------------------------------------------------
// Trading — tool result payloads
// ---------------------------------------------------------------------------

/** One ranked candidate from `resolve_symbol`; the `SymbolRef.to_dict()` shape. */
export interface SymbolCandidateOut {
  name: string;
  description: string;
  digits: number;
  point: number;
  pip_size: number;
  /** Which branch of the pip table fired, so the convention is auditable (BUILD §2). */
  pip_convention: string;
  volume_min: number;
  volume_step: number;
  volume_max: number;
  stops_level_points: number;
  freeze_level_points: number;
  spread_points: number;
  trade_tick_size: number;
  trade_tick_value: number;
  trade_tick_value_loss: number;
  contract_size: number;
  currency_profit: string;
  currency_margin: string;
  trade_mode: number;
  trade_exemode: number;
  filling_mode: number;
  visible: boolean;
  tradable: boolean;
}

/** `resolve_symbol` — up to eight ranked names, never one silent guess. */
export interface ResolveSymbolOut {
  ok: boolean;
  query: string;
  candidates: SymbolCandidateOut[];
  /** Closest names by edit distance, present only when nothing matched. */
  hint: string[];
  message: string;
}

/** `get_quote` — the `Quote.to_dict()` shape. */
export interface QuoteOut {
  symbol: string;
  bid: number;
  ask: number;
  spread_points: number;
  spread_pips: number;
  time_ts: number;
  server_time: string;
}

/** `price_from_distance` — an absolute price plus the convention that produced it. */
export interface PriceFromDistanceOut {
  price: number;
  convention: string;
  anchor: number;
  distance: Distance;
}

/** `evaluate_entry` — how much of the intended stop the delay already ate (BUILD §5.2). */
export interface EvaluateEntryOut {
  ok: boolean;
  symbol: string;
  side: string;
  signal_entry: number;
  sl: number;
  tp: number[];
  bid: number;
  ask: number;
  current_price: number;
  distance_from_signal_entry: Distance;
  distance_as_fraction_of_sl: number;
  rr_now: number | null;
  rr_as_posted: number | null;
  signal_age_seconds: number | null;
  /** True past `STALE_SIGNAL_SECONDS = 600`. */
  stale: boolean;
  volume_now: number | null;
  volume_as_posted: number | null;
  risk_percent: number;
  message: string;
}

/**
 * The envelope every write tool returns.
 *
 * A broker rejection is `ok: false` with an actionable message — never an exception, and never
 * an HTTP error. `tickets` carries every leg of a multi-TP split; `ticket` the single one.
 */
export interface WriteResultOut {
  ok: boolean;
  message: string;
  ticket: number | null;
  tickets: number[];
  filled_volume: number | null;
  fill_price: number | null;
  slippage_points: number | null;
  retcode: number | null;
  retcode_name: string | null;
}

// ---------------------------------------------------------------------------
// Signals, runs and steps
// ---------------------------------------------------------------------------

/** One stored image of a signal message. */
export interface ImageOut {
  id: number;
  /** Backend route serving the bytes; see `tgImageUrl`. */
  url: string;
  width: number | null;
  height: number | null;
}

/** One row of the signal feed, skipped messages included. */
export interface SignalOut {
  id: number;
  source_id: number;
  message_id: number;
  /** An edit is a revision of the same `message_id`, never a fresh signal (BUILD §3.3). */
  revision: number;
  kind: MessageKind;
  status: MessageStatus;
  /** Short phrase for the badge when `status === "skipped"`, e.g. `voice message`. */
  skip_reason: string | null;
  /** True when the row was fetched after downtime rather than received live (BUILD §8). */
  catchup: boolean;
  text: string;
  permalink: string | null;
  posted_at: string | null;
  received_at: string | null;
  image_count: number;
  images: ImageOut[];
  reply_to_message_id: number | null;
  signal_ref: string | null;
  /** Set when Telegram reported this channel post deleted. */
  deleted_at: string | null;
  runs_count: number;
  trades_count: number;
}

/** One agent run over one message. */
export interface RunOut {
  id: string;
  source_id: number | null;
  message_row_id: number | null;
  kind: RunKind;
  status: RunStatus;
  decision: RunDecision | null;
  summary: string | null;
  error: string | null;
  provider_kind: string | null;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  iterations: number;
  armed_at_start: boolean;
  started_at: string | null;
  finished_at: string | null;
}

/** One persisted step of a run: thinking, text, a tool call, its result, or an error. */
export interface RunStepOut {
  id: number;
  run_id: string;
  idx: number;
  type: RunStepType;
  /** Tool name on `tool_call` / `tool_result` steps, null otherwise. */
  name: string | null;
  /** Free-form by design: tool arguments, tool results and provider metadata all land here. */
  payload: Record<string, unknown>;
  ok: boolean | null;
  created_at: string | null;
}

/** A faithful projection of one `trades` row. */
export interface TradeOut {
  id: number;
  source_id: number | null;
  run_id: string | null;
  message_row_id: number | null;
  signal_ref: string | null;
  ticket: number;
  kind: TradeKind;
  symbol: string;
  side: Side;
  order_type: number;
  volume: number;
  requested_volume: number | null;
  entry_price: number;
  sl: number;
  tp: number;
  status: TradeStatus;
  magic: number;
  comment: string;
  external: boolean;
  /** Which leg of a multi-TP split this is, and how many legs there were (BUILD §5.3). */
  split_index: number;
  split_count: number;
  /** Targets beyond `MAX_TP_SPLITS`, or beyond leg one on a netting account. */
  unmanaged_tps: number[];
  ladder_role?: string | null;
  runner_mode?: RunnerMode | string | null;
  suggested_runner_mode?: RunnerMode | string | null;
  runner_tp_bias?: number | null;
  suggested_runner_tp_index?: number | null;
  chosen_runner_tp_index?: number | null;
  open_reason: string | null;
  close_cause: CloseCause | null;
  close_price: number | null;
  profit: number | null;
  profit_points: number | null;
  opened_at: string | null;
  closed_at: string | null;
  updated_at: string | null;
}

/** Query parameters of `GET /api/trading/signals`. */
export interface SignalsQuery {
  limit?: number;
  status?: MessageStatus;
}

/** Query parameters of `GET /api/trading/runs`. */
export interface RunsQuery {
  limit?: number;
}

/** `GET /api/trading/signals`. */
export interface SignalsOut {
  signals: SignalOut[];
}

/** `GET /api/trading/signals/{id}`. */
export interface SignalDetailOut {
  signal: SignalOut;
  runs: RunOut[];
  trades: TradeOut[];
}

/** `GET /api/trading/runs`. */
export interface RunsOut {
  runs: RunOut[];
}

/** `GET /api/trading/runs/{run_id}`. */
export interface RunDetailOut {
  run: RunOut;
  steps: RunStepOut[];
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

/** Query parameters of `GET /api/history/trades`; dates are ISO 8601 strings. */
export interface HistoryQuery {
  date_from?: string;
  date_to?: string;
  symbol?: string;
  limit?: number;
}

/** Query parameters of `GET /api/history/aggregates`. */
export interface AggregatesQuery {
  source_id?: number;
}

/** `GET /api/history/trades`. */
export interface HistoryTradesOut {
  trades: TradeOut[];
}

/** Per-symbol slice of the history aggregates. */
export interface SymbolAggregateOut {
  symbol: string;
  count: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_profit: number;
  avg_r: number;
}

/** `GET /api/history/aggregates` — win rate, average R and total P/L for the source. */
export interface AggregatesOut {
  win_rate: number;
  avg_r: number;
  total_profit: number;
  count: number;
  by_symbol: SymbolAggregateOut[];
}

// ---------------------------------------------------------------------------
// WebSocket events (CONTRACTS §4, §19)
// ---------------------------------------------------------------------------

/**
 * The envelope every bus event arrives in.
 *
 * `seq` is monotonic per backend process, so a client can detect a gap after a reconnect —
 * which is exactly why `stream.ts` refetches over REST on every (re)connect instead of
 * trusting the buffer.
 */
export interface EventEnvelope<TType extends string, TData> {
  type: TType;
  /** Unix seconds, server clock. */
  ts: number;
  seq: number;
  data: TData;
}

/** `signal.received` — a message was ingested, processable or skipped. */
export interface SignalReceivedData {
  signal: SignalOut;
}

/** `run.started` — the agent loop began on one message. */
export interface RunStartedData {
  run_id: string;
  kind: RunKind;
  source_id: number | null;
  message_row_id: number | null;
  signal_ref: string | null;
  provider_kind: string | null;
  model: string | null;
  /** The arm state captured when the run began; write tools honour the live one. */
  armed: boolean;
}

/** `run.thinking` — one delta of reasoning text, rendered in `--thinking` (BUILD §11). */
export interface RunThinkingData {
  run_id: string;
  text: string;
}

/** `run.text` — one delta of the model's visible answer. */
export interface RunTextData {
  run_id: string;
  text: string;
}

/** `run.tool_call` — the model asked for a tool, with the arguments it chose. */
export interface RunToolCallData {
  run_id: string;
  idx: number;
  id: string;
  name: string;
  input: Record<string, unknown>;
}

/** `run.tool_result` — what the executor answered; `ok: false` is a refusal, not a crash. */
export interface RunToolResultData {
  run_id: string;
  idx: number;
  id: string;
  name: string;
  ok: boolean;
  result: Record<string, unknown>;
}

/** `run.finished` — the run ended; `status: "incomplete"` means `finish` never came. */
export interface RunFinishedData {
  run_id: string;
  status: RunStatus;
  decision: RunDecision | null;
  summary: string | null;
  error: string | null;
  iterations: number;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
}

/**
 * `provider.request` — one call to the model.
 *
 * This is what makes "zero provider calls for a voice note" checkable rather than a claim
 * (BUILD §9), so it is emitted for every request including failures.
 */
export interface ProviderRequestData {
  run_id: string | null;
  provider_kind: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  ok: boolean;
  /** `model_context_window_exceeded` arrives here on an HTTP 200 (BUILD §4.1). */
  stop_reason?: string | null;
  error?: string | null;
}

/** `order.placed` — a pending order was accepted by the broker (retcode 10008). */
export interface OrderPlacedData {
  ticket: number;
  symbol: string;
  side: Side;
  order_type: number;
  order_type_name?: string;
  volume: number;
  price: number;
  sl: number;
  tp: number;
  run_id?: string | null;
  signal_ref?: string | null;
  reason?: string | null;
  retcode?: number | null;
  retcode_name?: string | null;
}

/** `order.cancelled` — a resting order was removed. */
export interface OrderCancelledData {
  ticket: number;
  symbol: string;
  run_id?: string | null;
  reason?: string | null;
}

/** `position.opened` — a market order filled; one event per leg of a multi-TP split. */
export interface PositionOpenedData {
  ticket: number;
  symbol: string;
  side: Side;
  volume: number;
  price: number;
  sl: number;
  tp: number;
  risk_percent?: number | null;
  /** Difference between the price asked for and the price filled at (BUILD §12). */
  slippage_points?: number | null;
  split_index?: number;
  split_count?: number;
  run_id?: string | null;
  signal_ref?: string | null;
  reason?: string | null;
  retcode?: number | null;
  retcode_name?: string | null;
}

/** `position.modified` — a stop or target moved; both the old and the new value are carried. */
export interface PositionModifiedData {
  ticket: number;
  symbol: string;
  sl?: number | null;
  tp?: number | null;
  old_sl?: number | null;
  old_tp?: number | null;
  run_id?: string | null;
  reason?: string | null;
}

/** `position.closed` — fully or partially, by the broker, the AI, the operator or the kill switch. */
export interface PositionClosedData {
  ticket: number;
  symbol: string;
  side: Side;
  volume: number;
  close_price: number;
  profit: number;
  profit_points?: number | null;
  cause: CloseCause;
  run_id?: string | null;
  reason?: string | null;
}

/** `account.tick` — the account snapshot, emitted every `ACCOUNT_TICK_SECONDS`. */
export interface AccountTickData {
  account: AccountOut;
}

/**
 * `system.warning` — something the operator must see but that did not stop the system.
 *
 * `reason` is a stable machine key — `needs_reentry`, `notify_failed`, `queue_overflow`,
 * `stopless_position`, `sl_fallback_applied`, `mt5_disconnected`, `provider_failing` — and is
 * what the UI translates. `message` is the English fallback when a key has no translation.
 */
export interface SystemWarningData {
  reason: string;
  message?: string;
  /** The settings key, when `reason === "needs_reentry"`. */
  key?: string | null;
  detail?: Record<string, unknown>;
}

/** `status.changed` — one of the three dots flipped. */
export interface StatusChangedData {
  key: ComponentName;
  ok: boolean;
  error: string | null;
}

/** `settings.changed` — a settings section was written; the UI refetches that section. */
export interface SettingsChangedData {
  section: SettingsSection;
}

export type SignalReceivedEvent = EventEnvelope<
  'signal.received',
  SignalReceivedData
>;
export type RunStartedEvent = EventEnvelope<'run.started', RunStartedData>;
export type RunThinkingEvent = EventEnvelope<'run.thinking', RunThinkingData>;
export type RunTextEvent = EventEnvelope<'run.text', RunTextData>;
export type RunToolCallEvent = EventEnvelope<'run.tool_call', RunToolCallData>;
export type RunToolResultEvent = EventEnvelope<
  'run.tool_result',
  RunToolResultData
>;
export type RunFinishedEvent = EventEnvelope<'run.finished', RunFinishedData>;
export type ProviderRequestEvent = EventEnvelope<
  'provider.request',
  ProviderRequestData
>;
export type OrderPlacedEvent = EventEnvelope<'order.placed', OrderPlacedData>;
export type OrderCancelledEvent = EventEnvelope<
  'order.cancelled',
  OrderCancelledData
>;
export type PositionOpenedEvent = EventEnvelope<
  'position.opened',
  PositionOpenedData
>;
export type PositionModifiedEvent = EventEnvelope<
  'position.modified',
  PositionModifiedData
>;
export type PositionClosedEvent = EventEnvelope<
  'position.closed',
  PositionClosedData
>;
export type AccountTickEvent = EventEnvelope<'account.tick', AccountTickData>;
export type SystemWarningEvent = EventEnvelope<
  'system.warning',
  SystemWarningData
>;
export type StatusChangedEvent = EventEnvelope<
  'status.changed',
  StatusChangedData
>;
export type SettingsChangedEvent = EventEnvelope<
  'settings.changed',
  SettingsChangedData
>;

/**
 * Every event the dashboard WebSocket carries, discriminated on `type`.
 *
 * A `switch (event.type)` narrows `event.data` to the matching payload with no cast.
 */
export type StreamEvent =
  | SignalReceivedEvent
  | RunStartedEvent
  | RunThinkingEvent
  | RunTextEvent
  | RunToolCallEvent
  | RunToolResultEvent
  | RunFinishedEvent
  | ProviderRequestEvent
  | OrderPlacedEvent
  | OrderCancelledEvent
  | PositionOpenedEvent
  | PositionModifiedEvent
  | PositionClosedEvent
  | AccountTickEvent
  | SystemWarningEvent
  | StatusChangedEvent
  | SettingsChangedEvent;

/** The `type` field of any stream event. */
export type StreamEventType = StreamEvent['type'];

export { EVENT_TYPES, isStreamEvent, normalizeStreamEvent } from './events';

/** Payload of the `hello` frame the server sends first on every connection (CONTRACTS §19). */
export interface StreamHelloData {
  status: StatusMap;
  armed: boolean;
  /** The last hundred envelopes, oldest first, so a reconnect can redraw immediately. */
  recent: StreamEvent[];
}

/** The first frame of every WebSocket connection. */
export interface StreamHello {
  type: 'hello';
  ts: number;
  data: StreamHelloData;
}

/** The server's answer to `{"type":"ping"}`. */
export interface StreamPong {
  type: 'pong';
  ts: number;
}

/** Anything the server may send down the socket. */
export type StreamFrame = StreamEvent | StreamHello | StreamPong;

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/** Everything an `ApiError` needs beyond its message. */
export interface ApiErrorInit {
  /** HTTP status, or 0 when the request never reached the server. */
  status: number;
  /** Machine-readable class: the backend's `error.code`, or one of the client codes below. */
  code: string;
  /** The backend's `error.detail`, the parsed body, or transport context. Free-form. */
  detail?: unknown;
}

/**
 * The single error type every function in this module throws.
 *
 * Client-side codes, used when the backend envelope is unavailable:
 *   `network`     — the request never reached the server (offline, DNS, TLS, reset).
 *   `aborted`     — the caller's `AbortSignal` fired.
 *   `validation_error` — a FastAPI 422 body, with the first field error as the message.
 *   `http_error`  — a non-2xx response whose body is not the error envelope.
 *   `bad_response` — a 2xx response whose body is not the JSON that was expected.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: unknown;

  constructor(message: string, init: ApiErrorInit) {
    super(message);
    this.name = 'ApiError';
    this.status = init.status;
    this.code = init.code;
    this.detail = init.detail ?? null;
    // Keeps `instanceof` working when the bundle is transpiled down to ES5 classes.
    Object.setPrototypeOf(this, ApiError.prototype);
  }

  /** True when the request never reached the backend, so retrying is meaningful. */
  get isNetwork(): boolean {
    return this.status === 0 && this.code === 'network';
  }

  /** True when the caller cancelled the request; the UI must stay silent about these. */
  get isAborted(): boolean {
    return this.code === 'aborted';
  }
}

/** Narrow an unknown catch binding to an `ApiError`. */
export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

// ---------------------------------------------------------------------------
// Fetch core
// ---------------------------------------------------------------------------

/** Every route lives under this prefix; the SPA is served by the same origin (ASSUMPTIONS). */
export const BASE_PATH = '/api';

/** How many characters of an unparseable body an `ApiError` carries. */
const BODY_SNIPPET_CHARS = 200;

/** Per-call options; `signal` is how a page cancels a request it no longer needs. */
export interface RequestOptions {
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

/** A query string value; `null` and `undefined` are dropped rather than sent as text. */
export type QueryValue = string | number | boolean | null | undefined;

/** Build a `?a=1&b=2` suffix, omitting every null or undefined value. */
export function buildQuery(
  params: Record<string, QueryValue> | undefined,
): string {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined) continue;
    search.append(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}

/** Collapse whitespace and cut to `BODY_SNIPPET_CHARS`, so an HTML error page stays readable. */
function snippet(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  if (flat.length <= BODY_SNIPPET_CHARS) return flat;
  return `${flat.slice(0, BODY_SNIPPET_CHARS)}…`;
}

/** Parse a body as JSON, returning `undefined` rather than throwing when it is not JSON. */
function tryParseJson(text: string): unknown {
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

/** True for the backend envelope `{"error":{"code","message","detail"}}`. */
function isErrorEnvelope(
  value: unknown,
): value is { error: { code?: unknown; message?: unknown; detail?: unknown } } {
  if (typeof value !== 'object' || value === null) return false;
  const inner = (value as { error?: unknown }).error;
  return typeof inner === 'object' && inner !== null;
}

/** Render FastAPI's `{"detail":[{"loc":[...],"msg":"..."}]}` as one sentence. */
function validationMessage(detail: unknown): string | undefined {
  if (!Array.isArray(detail) || detail.length === 0) return undefined;
  const first = detail[0] as { loc?: unknown; msg?: unknown };
  const msg = typeof first.msg === 'string' ? first.msg : undefined;
  if (!msg) return undefined;
  const loc = Array.isArray(first.loc)
    ? first.loc.filter((part) => part !== 'body' && part !== 'query').join('.')
    : '';
  return loc ? `${loc}: ${msg}` : msg;
}

/** Turn a non-2xx response into the `ApiError` the UI will render. */
function errorFromResponse(
  status: number,
  statusText: string,
  text: string,
): ApiError {
  const parsed = tryParseJson(text);

  if (isErrorEnvelope(parsed)) {
    const body = parsed.error;
    const message =
      typeof body.message === 'string' && body.message.trim()
        ? body.message
        : `${status} ${statusText}`.trim();
    const code =
      typeof body.code === 'string' && body.code ? body.code : 'http_error';
    return new ApiError(message, { status, code, detail: body.detail ?? null });
  }

  if (typeof parsed === 'object' && parsed !== null && 'detail' in parsed) {
    const detail = (parsed as { detail: unknown }).detail;
    const message =
      validationMessage(detail) ??
      (typeof detail === 'string' ? detail : undefined);
    if (message) {
      return new ApiError(message, {
        status,
        code: status === 422 ? 'validation_error' : 'http_error',
        detail,
      });
    }
    // A detail that is a STRUCTURE rather than a sentence — `{"retry_after": 30}` on a
    // 429 is the one the login page reads — is data the UI needs. Carrying it through
    // beats flattening the body into a text snippet nothing can parse.
    if (typeof detail === 'object' && detail !== null) {
      return new ApiError(
        `${status} ${statusText}`.trim() || `HTTP ${status}`,
        {
          status,
          code: status === 422 ? 'validation_error' : 'http_error',
          detail,
        },
      );
    }
  }

  const body = snippet(text);
  const message = body || `${status} ${statusText}`.trim() || `HTTP ${status}`;
  return new ApiError(message, {
    status,
    code: 'http_error',
    detail: body || null,
  });
}

/** Turn a thrown `fetch` rejection into the `ApiError` the UI will render. */
function errorFromTransport(cause: unknown, aborted: boolean): ApiError {
  if (
    aborted ||
    (cause instanceof DOMException && cause.name === 'AbortError')
  ) {
    return new ApiError('Request cancelled', {
      status: 0,
      code: 'aborted',
      detail: null,
    });
  }
  const reason = cause instanceof Error ? cause.message : String(cause);
  return new ApiError(reason || 'The server is unreachable', {
    status: 0,
    code: 'network',
    detail: reason || null,
  });
}

/**
 * Perform one request and decode its JSON body.
 *
 * A `204` and an empty 2xx body both resolve to `undefined`, cast to `T` — the routes that do
 * that are typed `void`, so nothing reads through the cast.
 */
async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...options?.headers,
  };
  // `same-origin` is the default, but it is written out because the whole auth model
  // depends on it: the session cookie set by POST /api/auth/login is what every later
  // request — and the WebSocket handshake — is authenticated by. Nothing here ever
  // reads or writes that cookie; the browser holds it.
  const init: RequestInit = { method, headers, credentials: 'same-origin' };
  if (options?.signal) init.signal = options.signal;
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let response: Response;
  try {
    response = await fetch(`${BASE_PATH}${path}`, init);
  } catch (cause) {
    throw errorFromTransport(cause, options?.signal?.aborted === true);
  }

  let text: string;
  try {
    text = await response.text();
  } catch (cause) {
    if (!response.ok)
      throw errorFromResponse(response.status, response.statusText, '');
    throw errorFromTransport(cause, options?.signal?.aborted === true);
  }

  if (
    response.status === 401 &&
    !path.startsWith('/auth/') &&
    typeof window !== 'undefined'
  ) {
    window.dispatchEvent(new Event('sp:unauthorized'));
  }
  if (!response.ok) {
    throw errorFromResponse(response.status, response.statusText, text);
  }

  if (response.status === 204 || text.trim() === '') {
    return undefined as T;
  }

  const parsed = tryParseJson(text);
  if (parsed === undefined) {
    throw new ApiError(
      snippet(text) || 'The server returned a body that is not JSON',
      {
        status: response.status,
        code: 'bad_response',
        detail: snippet(text) || null,
      },
    );
  }
  return parsed as T;
}

/** `GET` a JSON body. */
export function get<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>('GET', path, undefined, options);
}

/** `POST` a JSON body; pass no body for the routes that take none. */
export function post<T>(
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  return request<T>('POST', path, body, options);
}

/** `PUT` a JSON body. */
export function put<T>(
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  return request<T>('PUT', path, body, options);
}

/** `DELETE` a path. No route uses it today; it exists so one can be added without a wrapper. */
export function del<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>('DELETE', path, undefined, options);
}

/** The verb helpers as one object, per CONTRACTS §20 (`api.get/post/put`). */
export const api = { get, post, put, del } as const;

// ---------------------------------------------------------------------------
// Routes — health and settings (CONTRACTS §18)
// ---------------------------------------------------------------------------

/**
 * `GET /api/auth/status` — is a sign-in required, and is this browser signed in?
 *
 * The one route that answers 200 while signed out; everything else 401s. The shell
 * calls it before it renders anything, so nothing flashes.
 */
export function authStatus(options?: RequestOptions): Promise<AuthStatusOut> {
  return get<AuthStatusOut>('/auth/status', options);
}

/**
 * `POST /api/auth/login` — exchange the pair for a session cookie.
 *
 * Throws `ApiError` with status 401 for a wrong pair and 429 when the attempt
 * limiter has closed the door; `retryAfterSeconds()` reads the wait out of it.
 * Nothing is stored here: the cookie is HttpOnly and the browser owns it.
 */
export function login(
  body: LoginInput,
  options?: RequestOptions,
): Promise<LoginOut> {
  return post<LoginOut>('/auth/login', body, options);
}

/** `POST /api/auth/logout` — drop the session cookie. */
export function logout(options?: RequestOptions): Promise<LogoutOut> {
  return post<LogoutOut>('/auth/logout', undefined, options);
}

/** `GET /api/health` — liveness, the three dots, the arm state and any `needs_reentry` keys. */
export function getHealth(options?: RequestOptions): Promise<HealthOut> {
  return get<HealthOut>('/health', options);
}

/** `GET /api/settings` — the whole tree, every secret replaced by `MASK`. */
export function getSettings(options?: RequestOptions): Promise<SettingsOut> {
  return get<SettingsOut>('/settings', options);
}

/**
 * `PUT /api/settings/mt5` — broker terminal credentials.
 *
 * Sending `MASK` as `password`, sending `null`, or omitting it leaves the stored password
 * untouched; an empty string clears it. Returns the freshly masked settings tree.
 */
export function putMt5Settings(
  body: Mt5SettingsInput,
  options?: RequestOptions,
): Promise<SettingsOut> {
  return put<SettingsOut>('/settings/mt5', body, options);
}

/**
 * `POST /api/settings/mt5/test` — connect with the *stored* credentials and report the real
 * account: name, balance, equity, currency, leverage and margin mode. Save before testing.
 */
export function testMt5(options?: RequestOptions): Promise<Mt5TestOut> {
  return post<Mt5TestOut>('/settings/mt5/test', undefined, options);
}

/**
 * `PUT /api/settings/provider` — which model flies the plane and how to reach it.
 *
 * Sending `MASK` as `api_key`, sending `null`, or omitting it leaves the stored key untouched;
 * an empty string clears it. `base_url` is normalized server-side. Returns the settings tree.
 */
export function putProviderSettings(
  body: ProviderSettingsInput,
  options?: RequestOptions,
): Promise<SettingsOut> {
  return put<SettingsOut>('/settings/provider', body, options);
}

/**
 * `PUT /api/settings/notifier` — the operator's own Telegram bot.
 *
 * Sending `MASK` as `bot_token`, sending `null`, or omitting it leaves the stored token
 * untouched; an empty string clears it. Returns the settings tree.
 */
export function putNotifierSettings(
  body: NotifierSettingsInput,
  options?: RequestOptions,
): Promise<SettingsOut> {
  return put<SettingsOut>('/settings/notifier', body, options);
}

/** `POST /api/settings/notifier/test` — `getMe` against the stored token. Save before testing. */
export function testNotifier(
  options?: RequestOptions,
): Promise<NotifierTestOut> {
  return post<NotifierTestOut>('/settings/notifier/test', undefined, options);
}

/**
 * `POST /api/settings/notifier/detect` — polls `getUpdates` for up to `NOTIFY_DETECT_SECONDS`
 * and fills `chat_id` from the first incoming message, so nobody hunts for a numeric id by
 * hand (BUILD §7). Tell the operator to send `/start` to the bot before calling this.
 */
export function detectNotifierChat(
  options?: RequestOptions,
): Promise<NotifierDetectOut> {
  return post<NotifierDetectOut>(
    '/settings/notifier/detect',
    undefined,
    options,
  );
}

/**
 * `POST /api/settings/notifier/whoami` — the recent senders `getUpdates` still holds,
 * each with the numeric user id the admin list is keyed on.
 *
 * This is the answer to "where do I find my Telegram id": the operator presses the
 * button, sees themselves in the list, and adds their own id in one click, instead
 * of leaving the app for @userinfobot. Reads the STORED token, so save first.
 */
export function notifierWhoami(
  options?: RequestOptions,
): Promise<NotifierWhoamiOut> {
  return post<NotifierWhoamiOut>(
    '/settings/notifier/whoami',
    undefined,
    options,
  );
}

/**
 * `PUT /api/settings/limits` — the optional operator limits, all shipped off (BUILD §6).
 *
 * A null number or an empty list is not evaluated at all. Returns the settings tree.
 */
export function putLimits(
  body: LimitsInput,
  options?: RequestOptions,
): Promise<SettingsOut> {
  return put<SettingsOut>('/settings/limits', body, options);
}

/** `PUT /api/settings/general` — default risk, ladder lots, and notification locale. Returns the tree. */
export function putGeneral(
  body: GeneralSettingsInput,
  options?: RequestOptions,
): Promise<SettingsOut> {
  return put<SettingsOut>('/settings/general', body, options);
}

// ---------------------------------------------------------------------------
// Routes — Telegram account
// ---------------------------------------------------------------------------

/**
 * `POST /api/telegram/credentials` — store `api_id` / `api_hash` / `phone`.
 *
 * Sending `MASK` as `api_hash`, sending `null`, or omitting it leaves the stored hash
 * untouched. This is step one of the login; `tgSendCode` is step two.
 */
export function tgCredentials(
  body: TelegramCredentialsInput,
  options?: RequestOptions,
): Promise<OkResponse> {
  return post<OkResponse>('/telegram/credentials', body, options);
}

/** `POST /api/telegram/send-code` — ask Telegram to send the login code to the stored phone. */
export function tgSendCode(options?: RequestOptions): Promise<SendCodeOut> {
  return post<SendCodeOut>('/telegram/send-code', undefined, options);
}

/** `POST /api/telegram/sign-in` — submit the code; `needs_password` means 2FA is next. */
export function tgSignIn(
  body: TelegramSignInInput,
  options?: RequestOptions,
): Promise<SignInOut> {
  return post<SignInOut>('/telegram/sign-in', body, options);
}

/** `POST /api/telegram/password` — submit the two-factor cloud password. */
export function tgPassword(
  body: TelegramPasswordInput,
  options?: RequestOptions,
): Promise<OkResponse> {
  return post<OkResponse>('/telegram/password', body, options);
}

/** `POST /api/telegram/logout` — drop the session; ingest stops until a new login. */
export function tgLogout(options?: RequestOptions): Promise<OkResponse> {
  return post<OkResponse>('/telegram/logout', undefined, options);
}

/** `GET /api/telegram/status` — connection, authorization, the account and the active source. */
export function tgStatus(options?: RequestOptions): Promise<TelegramStatusOut> {
  return get<TelegramStatusOut>('/telegram/status', options);
}

/** `GET /api/telegram/dialogs` — the channels this account can read, for the picker. */
export function tgDialogs(options?: RequestOptions): Promise<DialogsOut> {
  return get<DialogsOut>('/telegram/dialogs', options);
}

/**
 * `POST /api/telegram/source` — make one dialog the active signal source.
 *
 * On the very first selection the backend records the current latest `message_id` as the
 * baseline and fetches nothing, so picking a channel never replays its backlog (BUILD §8).
 */
export function tgSelectSource(
  body: SourceSelectInput,
  options?: RequestOptions,
): Promise<SourceSelectOut> {
  return post<SourceSelectOut>('/telegram/source', body, options);
}

/**
 * `GET /api/telegram/image/{image_id}` — the URL of a stored signal image.
 *
 * This one is a URL builder, not a request: the bytes go straight into an `<img src>`.
 * `ImageOut.url` already carries the same string; use this when only the id is at hand.
 */
export function tgImageUrl(imageId: number): string {
  return `${BASE_PATH}/telegram/image/${encodeURIComponent(String(imageId))}`;
}

// ---------------------------------------------------------------------------
// Routes — provider
// ---------------------------------------------------------------------------

/**
 * `POST /api/provider/test` — list the models these connection details can reach.
 *
 * Failures report the real cause: a rejected key, an endpoint that is not there, an
 * unreachable host, or the first 200 characters of a body that is not JSON (BUILD §4.3).
 * Omitting `api_key` or sending `MASK` reuses the stored key.
 */
export function providerTest(
  body: ProviderTestInput,
  options?: RequestOptions,
): Promise<ProviderTestOut> {
  return post<ProviderTestOut>('/provider/test', body, options);
}

/**
 * `POST /api/provider/probe` — settle vision and tools for one model.
 *
 * The verdict is cached per `(base_url, model)` for `PROBE_CACHE_HOURS = 24`; `force: true`
 * asks again. Anthropic reports capabilities directly, so no request leaves for that kind.
 */
export function providerProbe(
  body: ProviderProbeInput,
  options?: RequestOptions,
): Promise<Capabilities> {
  return post<Capabilities>('/provider/probe', body, options);
}

/** `GET /api/provider/models` — the last successful listing, without calling the provider. */
export function providerModels(
  options?: RequestOptions,
): Promise<ProviderModelsOut> {
  return get<ProviderModelsOut>('/provider/models', options);
}

// ---------------------------------------------------------------------------
// Routes — trading state and control
// ---------------------------------------------------------------------------

/** `GET /api/trading/state` — arm state, the three dots, margin mode, currency, active source. */
export function tradingState(
  options?: RequestOptions,
): Promise<TradingStateOut> {
  return get<TradingStateOut>('/trading/state', options);
}

/**
 * `POST /api/trading/arm` — the operator's on/off switch.
 *
 * Disarmed, the loop still runs and streams its full reasoning; every write tool answers
 * "System is disarmed — no order was sent" (BUILD §5.5).
 */
export function arm(armed: boolean, options?: RequestOptions): Promise<ArmOut> {
  const body: ArmInput = { armed };
  return post<ArmOut>('/trading/arm', body, options);
}

/**
 * `POST /api/trading/kill-switch` — close every position and cancel every pending order
 * carrying our magic, **then** disarm. Positions with a foreign magic are never touched.
 *
 * The confirmation string is literally `KILL`, and the UI types it out (BUILD §5.5).
 */
export function killSwitch(options?: RequestOptions): Promise<KillSwitchOut> {
  const body: KillSwitchInput = { confirm: 'KILL' };
  return post<KillSwitchOut>('/trading/kill-switch', body, options);
}

/** `GET /api/trading/account` — balance, equity, free margin, floating and today's realized P/L. */
export function getAccount(options?: RequestOptions): Promise<AccountOut> {
  return get<AccountOut>('/trading/account', options);
}

/** `GET /api/trading/positions` — live from MT5, each row joined to its originating signal. */
export function getPositions(options?: RequestOptions): Promise<PositionsOut> {
  return get<PositionsOut>('/trading/positions', options);
}

/** `GET /api/trading/orders` — live pending orders, each row joined to its originating signal. */
export function getOrders(options?: RequestOptions): Promise<OrdersOut> {
  return get<OrdersOut>('/trading/orders', options);
}

/** `GET /api/trading/candles` — recent OHLC, capped at `MAX_CANDLES = 200` bars. */
export function getCandles(
  query: CandlesQuery,
  options?: RequestOptions,
): Promise<CandlesOut> {
  const search = buildQuery({
    symbol: query.symbol,
    timeframe: query.timeframe,
    count: query.count,
  });
  return get<CandlesOut>(`/trading/candles${search}`, options);
}

/**
 * `GET /api/trading/symbols` — the symbol table of the connected MT5 account.
 *
 * Search is the primary interaction here, not scrolling, so every call carries the
 * operator's query and a `limit`; the response reports `total` and `truncated` so the
 * caller can state how much of the table it is not showing.
 *
 * `only_tradable` defaults to the server's choice when omitted. Leave it off to receive
 * the untradable symbols too — a picker that hides them answers "no such symbol" to a
 * search that did match, which is worse than showing the row and saying why it is barred.
 */
export function getTradableSymbols(
  query?: SymbolsQuery,
  options?: RequestOptions,
): Promise<SymbolsOut> {
  const search = buildQuery({
    query: query?.query,
    only_tradable: query?.only_tradable,
    limit: query?.limit,
  });
  return get<SymbolsOut>(`/trading/symbols${search}`, options);
}

// ---------------------------------------------------------------------------
// Routes — manual override (the operator pulling the same levers as the AI)
// ---------------------------------------------------------------------------

/** `POST /api/trading/manual/resolve-symbol` — ranked broker symbols for a loose name. */
export function manualResolveSymbol(
  body: ManualResolveSymbolInput,
  options?: RequestOptions,
): Promise<ResolveSymbolOut> {
  return post<ResolveSymbolOut>(
    '/trading/manual/resolve-symbol',
    body,
    options,
  );
}

/** `POST /api/trading/manual/quote` — bid, ask, spread in points and pips, server time. */
export function manualQuote(
  body: ManualQuoteInput,
  options?: RequestOptions,
): Promise<QuoteOut> {
  return post<QuoteOut>('/trading/manual/quote', body, options);
}

/** `POST /api/trading/manual/price-from-distance` — "20 pips below" as an absolute price. */
export function manualPriceFromDistance(
  body: ManualPriceFromDistanceInput,
  options?: RequestOptions,
): Promise<PriceFromDistanceOut> {
  return post<PriceFromDistanceOut>(
    '/trading/manual/price-from-distance',
    body,
    options,
  );
}

/** `POST /api/trading/manual/evaluate-entry` — how much of the stop the delay already ate. */
export function manualEvaluateEntry(
  body: ManualEvaluateEntryInput,
  options?: RequestOptions,
): Promise<EvaluateEntryOut> {
  return post<EvaluateEntryOut>(
    '/trading/manual/evaluate-entry',
    body,
    options,
  );
}

/**
 * `POST /api/trading/manual/open` — market order.
 *
 * A refusal — no stop, a breached limit, a broker rejection — comes back as `ok: false` with
 * a message, HTTP 200. Only a malformed request is an `ApiError`.
 */
export function manualOpen(
  body: ManualOpenInput,
  options?: RequestOptions,
): Promise<WriteResultOut> {
  return post<WriteResultOut>('/trading/manual/open', body, options);
}

/** `POST /api/trading/manual/pending` — limit or stop order; `ok: false` carries the refusal. */
export function manualPending(
  body: ManualPendingInput,
  options?: RequestOptions,
): Promise<WriteResultOut> {
  return post<WriteResultOut>('/trading/manual/pending', body, options);
}

/** `POST /api/trading/manual/modify-position` — move the stop or the target; never remove one. */
export function manualModifyPosition(
  body: ManualModifyPositionInput,
  options?: RequestOptions,
): Promise<WriteResultOut> {
  return post<WriteResultOut>('/trading/manual/modify-position', body, options);
}

/** `POST /api/trading/manual/modify-order` — move a resting order's price, stop or target. */
export function manualModifyOrder(
  body: ManualModifyOrderInput,
  options?: RequestOptions,
): Promise<WriteResultOut> {
  return post<WriteResultOut>('/trading/manual/modify-order', body, options);
}

/** `POST /api/trading/manual/close` — full or partial close of one position. */
export function manualClose(
  body: ManualCloseInput,
  options?: RequestOptions,
): Promise<WriteResultOut> {
  return post<WriteResultOut>('/trading/manual/close', body, options);
}

/** `POST /api/trading/manual/cancel` — remove one pending order. */
export function manualCancel(
  body: ManualCancelInput,
  options?: RequestOptions,
): Promise<WriteResultOut> {
  return post<WriteResultOut>('/trading/manual/cancel', body, options);
}

// ---------------------------------------------------------------------------
// Routes — signals, runs and history
// ---------------------------------------------------------------------------

/** `GET /api/trading/signals` — the feed, newest first, skipped messages included. */
export function getSignals(
  query?: SignalsQuery,
  options?: RequestOptions,
): Promise<SignalsOut> {
  const search = buildQuery({ limit: query?.limit, status: query?.status });
  return get<SignalsOut>(`/trading/signals${search}`, options);
}

/** `GET /api/trading/signals/{id}` — one message with its runs and the trades they opened. */
export function getSignal(
  id: number,
  options?: RequestOptions,
): Promise<SignalDetailOut> {
  return get<SignalDetailOut>(
    `/trading/signals/${encodeURIComponent(String(id))}`,
    options,
  );
}

/** `GET /api/trading/runs` — recent agent runs, newest first. */
export function getRuns(
  query?: RunsQuery,
  options?: RequestOptions,
): Promise<RunsOut> {
  const search = buildQuery({ limit: query?.limit });
  return get<RunsOut>(`/trading/runs${search}`, options);
}

/** `GET /api/trading/runs/{run_id}` — one run with every persisted step, in order. */
export function getRun(
  runId: string,
  options?: RequestOptions,
): Promise<RunDetailOut> {
  return get<RunDetailOut>(
    `/trading/runs/${encodeURIComponent(runId)}`,
    options,
  );
}

/** `GET /api/history/trades` — closed trades, filtered by date range and symbol. */
export function getHistoryTrades(
  query?: HistoryQuery,
  options?: RequestOptions,
): Promise<HistoryTradesOut> {
  const search = buildQuery({
    date_from: query?.date_from,
    date_to: query?.date_to,
    symbol: query?.symbol,
    limit: query?.limit,
  });
  return get<HistoryTradesOut>(`/history/trades${search}`, options);
}

/** `GET /api/history/aggregates` — win rate, average R and total P/L, overall and per symbol. */
export function getHistoryAggregates(
  query?: AggregatesQuery,
  options?: RequestOptions,
): Promise<AggregatesOut> {
  const search = buildQuery({ source_id: query?.source_id });
  return get<AggregatesOut>(`/history/aggregates${search}`, options);
}
