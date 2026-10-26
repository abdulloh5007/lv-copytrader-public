import type {
  ApiError,
  LadderStatsOut,
  NotifyType,
  SettingsOut,
} from '@/api/client';
import { isApiError } from '@/api/client';
import type { TKey, Translate } from '@/i18n';
import { EMPTY_VALUE, formatNumber, useT } from '@/i18n';
import type { Theme } from '@/theme';
import {
  THEME_STORAGE_KEY,
  applyTheme,
  detectTheme as readTheme,
} from '@/theme';
import type { CSSProperties, ReactElement, ReactNode } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
export { THEME_STORAGE_KEY, applyTheme, readTheme };
export type { Theme };

export const PHONE_E164 = /^\+[1-9]\d{6,14}$/;

export const DIGITS = /^\d+$/;

export const SIGNED_DIGITS = /^-?\d+$/;

export const DECIMAL = /^\d+([.,]\d+)?$/;

export const WEEKDAYS: readonly { day: number; key: TKey }[] = [
  { day: 0, key: 'settings.day_mon' },
  { day: 1, key: 'settings.day_tue' },
  { day: 2, key: 'settings.day_wed' },
  { day: 3, key: 'settings.day_thu' },
  { day: 4, key: 'settings.day_fri' },
  { day: 5, key: 'settings.day_sat' },
  { day: 6, key: 'settings.day_sun' },
];

export const NOTIFY_ROWS: readonly {
  type: NotifyType;
  label: TKey;
  hint?: TKey;
}[] = [
  { type: 'opened', label: 'settings.notify_opened' },
  { type: 'modified', label: 'settings.notify_modified' },
  { type: 'closed', label: 'settings.notify_closed' },
  { type: 'rejected', label: 'settings.notify_rejected' },
  {
    type: 'attention',
    label: 'settings.notify_attention',
    hint: 'settings.notify_attention_hint',
  },
];

export type SettingsTabId =
  'telegram' | 'mt5' | 'provider' | 'notifier' | 'limits' | 'general';

export const SETTINGS_TABS: readonly {
  id: SettingsTabId;
  sectionId: string;
  tabKey: TKey;
}[] = [
  {
    id: 'telegram',
    sectionId: 'sp-set-telegram',
    tabKey: 'settings.telegram_tab',
  },
  { id: 'mt5', sectionId: 'sp-set-mt5', tabKey: 'settings.mt5_tab' },
  {
    id: 'provider',
    sectionId: 'sp-set-provider',
    tabKey: 'settings.provider_tab',
  },
  {
    id: 'notifier',
    sectionId: 'sp-set-notifier',
    tabKey: 'settings.notifier_tab',
  },
  { id: 'limits', sectionId: 'sp-set-limits', tabKey: 'settings.limits_tab' },
  {
    id: 'general',
    sectionId: 'sp-set-general',
    tabKey: 'settings.general_tab',
  },
];

export const TAB_FROM_HASH: Record<string, SettingsTabId> = {
  telegram: 'telegram',
  'sp-set-telegram': 'telegram',
  mt5: 'mt5',
  'sp-set-mt5': 'mt5',
  provider: 'provider',
  'sp-set-provider': 'provider',
  notifier: 'notifier',
  'sp-set-notifier': 'notifier',
  limits: 'limits',
  'sp-set-limits': 'limits',
  general: 'general',
  'sp-set-general': 'general',
};

export function tabFromHash(hash: string): SettingsTabId {
  const raw = decodeURIComponent(hash.replace(/^#/, '')).trim().toLowerCase();
  return TAB_FROM_HASH[raw] ?? 'telegram';
}

export const TIME_ZONES: readonly string[] = (() => {
  try {
    const intl = Intl as unknown as {
      supportedValuesOf?: (key: string) => string[];
    };
    if (typeof intl.supportedValuesOf === 'function')
      return intl.supportedValuesOf('timeZone');
  } catch {
    // A runtime without the table: fall through to the empty list.
  }
  return [];
})();

export const PAGE: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--sp-5)',
  width: '100%',
  maxWidth: '880px',
  margin: '0 auto',
};

export const PAGE_TITLE: CSSProperties = {
  fontSize: 'var(--fs-5)',
  lineHeight: 'var(--lh-tight)',
};

export const NOTE: CSSProperties = {
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-1)',
};

export const OK_NOTE: CSSProperties = {
  color: 'var(--ok)',
  fontSize: 'var(--fs-1)',
  fontWeight: 600,
};

export const BAD_NOTE: CSSProperties = {
  color: 'var(--danger)',
  fontSize: 'var(--fs-1)',
  fontWeight: 600,
};

export const CALM_NOTE: CSSProperties = {
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-2)',
  lineHeight: 'var(--lh)',
  maxWidth: '78ch',
};

export const STATEMENT_BOX: CSSProperties = {
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-2)',
  lineHeight: 'var(--lh)',
  padding: 'var(--sp-1) 0 var(--sp-1) var(--sp-3)',
  borderLeft: '2px solid var(--border-strong)',
};

export const GRID: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
  gap: 'var(--sp-3)',
  alignItems: 'start',
};

export const STATS_DL: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'auto 1fr',
  columnGap: 'var(--sp-3)',
  rowGap: 'var(--sp-1)',
  margin: 'var(--sp-2) 0 0',
  maxWidth: '480px',
};

export const STATS_DT: CSSProperties = {
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-1)',
  margin: 0,
};

export const STATS_DD: CSSProperties = {
  margin: 0,
  fontSize: 'var(--fs-1)',
};

export function LadderStatsPanel(props: {
  stats: LadderStatsOut | null | undefined;
}): ReactElement | null {
  const t = useT();
  const stats = props.stats;
  if (!stats) return null;
  const modeLabel =
    stats.suggested_mode === 'hold_channel_sl'
      ? t('settings.runner_mode_hold')
      : t('settings.runner_mode_be');
  return (
    <div style={{ ...STATEMENT_BOX, marginTop: 'var(--sp-3)' }}>
      <div
        style={{
          color: 'var(--text)',
          fontWeight: 600,
          marginBottom: 'var(--sp-1)',
        }}
      >
        {t('settings.ladder_stats_heading')}
      </div>
      <p style={{ ...NOTE, margin: 0 }}>{t('settings.ladder_stats_hint')}</p>
      <dl style={STATS_DL}>
        <dt style={STATS_DT}>{t('settings.ladder_stats_suggested')}</dt>
        <dd style={STATS_DD} className="mono">
          {modeLabel}
        </dd>
        <dt style={STATS_DT}>{t('settings.ladder_stats_streak')}</dt>
        <dd style={STATS_DD} className="mono">
          {formatNumber(stats.clean_streak, { maximumFractionDigits: 0 })}
        </dd>
        <dt style={STATS_DT}>{t('settings.ladder_stats_raw_sl')}</dt>
        <dd style={STATS_DD} className="mono">
          {formatNumber(stats.raw_sl_hits, { maximumFractionDigits: 0 })}
        </dd>
        <dt style={STATS_DT}>{t('settings.ladder_stats_be_stopouts')}</dt>
        <dd style={STATS_DD} className="mono">
          {formatNumber(stats.be_stopouts, { maximumFractionDigits: 0 })}
        </dd>
        <dt style={STATS_DT}>{t('settings.ladder_stats_tp4')}</dt>
        <dd style={STATS_DD} className="mono">
          {formatNumber(stats.tp4_plus, { maximumFractionDigits: 0 })}
        </dd>
        <dt style={STATS_DT}>{t('settings.ladder_stats_runner_tp_bias')}</dt>
        <dd style={STATS_DD} className="mono">
          {`${stats.suggested_runner_tp_bias != null && stats.suggested_runner_tp_bias > 0 ? '+' : ''}${formatNumber(stats.suggested_runner_tp_bias ?? 0, { maximumFractionDigits: 0 })}`}
        </dd>
        <dt style={STATS_DT}>
          {t('settings.ladder_stats_sample', {
            sample: formatNumber(stats.sample_size, {
              maximumFractionDigits: 0,
            }),
            window: formatNumber(stats.window, { maximumFractionDigits: 0 }),
          })}
        </dt>
        <dd style={STATS_DD} className="mono">
          {stats.source_title || EMPTY_VALUE}
        </dd>
      </dl>
      {stats.bootstrap ? (
        <p style={{ ...NOTE, margin: 'var(--sp-2) 0 0' }}>
          {t('settings.ladder_stats_bootstrap')}
        </p>
      ) : null}
    </div>
  );
}

export const NARROW: CSSProperties = { maxWidth: '320px' };

export const SUB_BOX: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--sp-3)',
  padding: 'var(--sp-4) 0 0',
  borderTop: 'var(--border-w) solid var(--border)',
};

export const ALIGN_UNDER_LABEL: CSSProperties = {
  paddingTop: 'calc(var(--fs-1) * var(--lh) + var(--sp-1))',
};

export const FIELDSET: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--sp-2)',
  minWidth: 0,
};

export const WELL: CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  gap: 'var(--sp-2)',
  padding: 'var(--sp-2)',
  background: 'var(--surface-2)',
  border: 'var(--border-w) solid var(--border)',
  borderRadius: 'calc(var(--radius-sm) + var(--sp-2))',
};

export const ADMIN_BOX: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--sp-2)',
  padding: 'var(--sp-2) 0 var(--sp-2) var(--sp-3)',
  borderLeft: '2px solid var(--border)',
};

export const ADMIN_BOX_OPEN: CSSProperties = {
  ...ADMIN_BOX,
  borderColor: 'var(--warn)',
};

export const ADMIN_HEAD: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 'var(--sp-2)',
  flexWrap: 'wrap',
};

export const ADMIN_TITLE: CSSProperties = {
  fontSize: 'var(--fs-2)',
  fontWeight: 600,
  color: 'var(--text)',
};

export const ADMIN_STATE: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--sp-1)',
  minWidth: 0,
};

export const ADMIN_OPEN_TEXT: CSSProperties = {
  color: 'var(--warn)',
  fontSize: 'var(--fs-2)',
  fontWeight: 600,
  lineHeight: 'var(--lh)',
  maxWidth: '78ch',
};

export const ADMIN_LOCKED_TEXT: CSSProperties = {
  color: 'var(--accent)',
  fontSize: 'var(--fs-2)',
  fontWeight: 600,
  lineHeight: 'var(--lh)',
  maxWidth: '78ch',
};

export const BOUND_ROW: CSSProperties = {
  display: 'flex',
  alignItems: 'baseline',
  gap: 'var(--sp-2)',
  flexWrap: 'wrap',
  fontSize: 'var(--fs-1)',
  color: 'var(--text-dim)',
};

export const BOUND_NAME: CSSProperties = {
  color: 'var(--text)',
  fontWeight: 500,
};

export const NUMERIC_ID: CSSProperties = {
  flex: 'none',
  color: 'var(--text)',
  fontSize: 'var(--fs-1)',
};

export const SENDERS_BOX: CSSProperties = {
  maxHeight: '260px',
  border: 'var(--border-w) solid var(--border)',
  borderRadius: 'var(--radius)',
  background: 'var(--surface)',
};

export const LEGEND: CSSProperties = {
  fontSize: 'var(--fs-1)',
  fontWeight: 500,
  letterSpacing: '0.02em',
  color: 'var(--text-dim)',
};

export function detailRecord(error: ApiError): Record<string, unknown> | null {
  const detail = error.detail;
  if (typeof detail === 'object' && detail !== null && !Array.isArray(detail)) {
    return detail as Record<string, unknown>;
  }
  return null;
}

export const TELEGRAM_ERROR_KEYS: Record<string, TKey> = {
  PhoneCodeInvalidError: 'err.code_invalid',
  PhoneCodeExpiredError: 'err.code_expired',
  PhoneCodeEmptyError: 'err.required',
  PhoneNumberInvalidError: 'err.phone_invalid',
  PasswordHashInvalidError: 'err.password_invalid',
  SessionPasswordNeededError: 'settings.twofa_needed',
  AuthKeyUnregisteredError: 'err.not_authorized',
  SessionRevokedError: 'err.not_authorized',
};

export const PROVIDER_KIND_KEYS: Record<string, TKey> = {
  auth: 'err.key_rejected',
  not_found: 'err.endpoint_not_found',
  transport: 'err.host_unreachable',
  rate_limit: 'err.rate_limit',
  overloaded: 'err.overloaded',
  bad_request: 'err.bad_request',
  context_window: 'err.context_window',
};

export function errorSentence(cause: unknown, t: Translate): string {
  if (isApiError(cause)) {
    if (cause.isNetwork) return t('err.network');

    const detail = detailRecord(cause);

    const telegramError = detail?.telegram_error;
    if (typeof telegramError === 'string') {
      const key = TELEGRAM_ERROR_KEYS[telegramError];
      if (key === 'err.flood_wait' || telegramError === 'FloodWaitError') {
        const seconds =
          typeof detail?.seconds === 'number' ? detail.seconds : 0;
        return t('err.flood_wait', { seconds });
      }
      if (key) return t(key);
    }

    if (cause.code === 'provider_error') {
      const kind = detail?.kind;
      if (typeof kind === 'string') {
        const key = PROVIDER_KIND_KEYS[kind];
        if (key) return t(key);
      }
    }

    if (cause.message) return cause.message;
    if (cause.status >= 500) return t('err.server');
    return t('err.generic');
  }
  if (cause instanceof Error && cause.message) return cause.message;
  return t('err.generic');
}

export function errorField(cause: unknown): string | null {
  if (!isApiError(cause)) return null;

  const detail = detailRecord(cause);
  const field = detail?.field;
  if (typeof field === 'string' && field) return field;

  if (Array.isArray(cause.detail)) {
    const named = fieldFromErrorList(cause.detail);
    if (named) return named;
  }

  // The backend re-dresses FastAPI's 422 as `{"error":{"detail":{"errors":[...]}}}`, so
  // the `loc` array arrives one level deeper than the shape above. Same data, same
  // anchoring: without this branch a rejected `admin_ids` reads as a footer message
  // instead of sitting under the field the operator just typed into.
  const errors = detail?.errors;
  if (Array.isArray(errors)) {
    const named = fieldFromErrorList(errors);
    if (named) return named;
  }
  return null;
}

export function fieldFromErrorList(errors: readonly unknown[]): string | null {
  if (errors.length === 0) return null;
  const first = errors[0] as { loc?: unknown };
  if (!Array.isArray(first?.loc)) return null;
  const named = first.loc.filter(
    // A list index arrives as a number (`["body","admin_ids",0]`); the field is the last
    // NAME, so the index is dropped rather than reported as the field "0".
    (part): part is string =>
      typeof part === 'string' && part !== 'body' && part !== 'query',
  );
  return named.length > 0 ? named[named.length - 1] : null;
}

export function requiredText(raw: string, t: Translate): string | null {
  return raw.trim() ? null : t('err.required');
}

export function positiveInt(
  raw: string,
  t: Translate,
  required: boolean,
): string | null {
  const value = raw.trim();
  if (!value) return required ? t('err.required') : null;
  if (!DIGITS.test(value)) return t('err.positive_int');
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0)
    return t('err.positive_int');
  return null;
}

export function wholeNumber(
  raw: string,
  t: Translate,
  required: boolean,
): string | null {
  const value = raw.trim();
  if (!value) return required ? t('err.required') : null;
  if (!SIGNED_DIGITS.test(value)) return t('err.integer');
  if (!Number.isSafeInteger(Number(value))) return t('err.integer');
  return null;
}

export function positiveNumber(
  raw: string,
  t: Translate,
  required: boolean,
): string | null {
  const value = raw.trim();
  if (!value) return required ? t('err.required') : null;
  if (!DECIMAL.test(value)) return t('err.positive_number');
  const parsed = Number(value.replace(',', '.'));
  if (!Number.isFinite(parsed) || parsed <= 0) return t('err.positive_number');
  return null;
}

export function phoneNumber(raw: string, t: Translate): string | null {
  const value = raw.trim();
  if (!value) return t('err.required');
  return PHONE_E164.test(value) ? null : t('err.e164');
}

export function httpUrl(
  raw: string,
  t: Translate,
  required: boolean,
): string | null {
  const value = raw.trim();
  if (!value) return required ? t('err.required') : null;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return t('err.url');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
    return t('err.url');
  return null;
}

export function toNumber(raw: string): number {
  return Number(raw.trim().replace(',', '.'));
}

export function toInt(raw: string): number {
  return Number.parseInt(raw.trim(), 10);
}

export function toOptionalNumber(raw: string): number | null {
  return raw.trim() ? toNumber(raw) : null;
}

export function toOptionalInt(raw: string): number | null {
  return raw.trim() ? toInt(raw) : null;
}

export function needsReentry(
  keys: readonly string[],
  ...names: readonly string[]
): boolean {
  return names.some((name) => keys.includes(name));
}

export function hasError(
  errors: Record<string, string | null | undefined>,
): boolean {
  return Object.values(errors).some((value) => Boolean(value));
}

export interface TextFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string | null;
  hint?: ReactNode;
  type?: 'text' | 'tel' | 'time';
  inputMode?: 'text' | 'numeric' | 'decimal' | 'tel' | 'url';
  mono?: boolean;
  disabled?: boolean;
  optional?: boolean;
  placeholder?: string;
  autoComplete?: string;
  list?: string;
  ariaLabel?: string;
}

export function TextField(props: TextFieldProps): ReactElement {
  const t = useT();
  const describedBy = props.error
    ? `${props.id}-err`
    : props.hint
      ? `${props.id}-hint`
      : undefined;

  return (
    <div className={props.error ? 'field field--invalid' : 'field'}>
      <label className="field__label" htmlFor={props.id}>
        {props.label}
        {props.optional ? (
          <span style={NOTE}> · {t('app.optional')}</span>
        ) : null}
      </label>
      <input
        id={props.id}
        className={props.mono ? 'mono' : undefined}
        type={props.type ?? 'text'}
        value={props.value}
        disabled={props.disabled}
        placeholder={props.placeholder}
        autoComplete={props.autoComplete ?? 'off'}
        inputMode={props.inputMode}
        list={props.list}
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        aria-label={props.ariaLabel}
        aria-invalid={props.error ? 'true' : undefined}
        aria-describedby={describedBy}
        onChange={(event) => props.onChange(event.target.value)}
      />
      {props.error ? (
        <p className="field__error" id={`${props.id}-err`}>
          {props.error}
        </p>
      ) : props.hint ? (
        <p className="field__hint" id={`${props.id}-hint`}>
          {props.hint}
        </p>
      ) : null}
    </div>
  );
}

export interface ToggleRowProps {
  id: string;
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  hint?: ReactNode;
  disabled?: boolean;
}

export function ToggleRow(props: ToggleRowProps): ReactElement {
  const t = useT();
  return (
    <div className="field field--row">
      <span className="field__label" id={`${props.id}-label`}>
        {props.label}
      </span>
      <button
        type="button"
        className="switch"
        role="switch"
        id={props.id}
        aria-checked={props.checked}
        aria-labelledby={`${props.id}-label`}
        disabled={props.disabled}
        onClick={() => props.onChange(!props.checked)}
      >
        <span className="switch__track">
          <span className="switch__thumb" />
        </span>
        <span className="switch__label">
          {props.checked ? t('app.on') : t('app.off')}
        </span>
      </button>
      {props.hint ? <p className="field__hint">{props.hint}</p> : null}
    </div>
  );
}

export interface SegmentedOption<V extends string> {
  value: V;
  label: string;
}

export interface SegmentedProps<V extends string> {
  id: string;
  label: string;
  value: V;
  options: readonly SegmentedOption<V>[];
  onChange: (value: V) => void;
  hint?: ReactNode;
  disabled?: boolean;
}

export function Segmented<V extends string>(
  props: SegmentedProps<V>,
): ReactElement {
  return (
    <div className="field">
      <span className="field__label" id={`${props.id}-label`}>
        {props.label}
      </span>
      <div
        className="row row--tight"
        role="radiogroup"
        aria-labelledby={`${props.id}-label`}
      >
        {props.options.map((option) => {
          const on = option.value === props.value;
          return (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={on}
              className={on ? 'tab is-active' : 'tab'}
              disabled={props.disabled}
              onClick={() => props.onChange(option.value)}
            >
              {option.label}
            </button>
          );
        })}
      </div>
      {props.hint ? <p className="field__hint">{props.hint}</p> : null}
    </div>
  );
}

export interface SectionProps {
  id: string;
  title: string;
  hint?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  onSave?: () => void;
  saving?: boolean;
  saved?: boolean;
  dirty?: boolean;
  error?: string | null;
}

export function Section(props: SectionProps): ReactElement {
  const t = useT();
  return (
    <section
      className="card"
      id={props.id}
      aria-labelledby={`${props.id}-title`}
    >
      <header className="card__header">
        <h2 className="card__title" id={`${props.id}-title`}>
          {props.title}
        </h2>
        <div className="card__actions">
          {props.dirty ? (
            <span className="badge badge--warn">
              {t('settings.unsaved_changes')}
            </span>
          ) : null}
          {props.actions}
        </div>
      </header>

      <div className="card__body">
        {props.hint ? (
          <details className="settings-help">
            <summary>{t('settings.help')}</summary>
            <p className="field__hint">{props.hint}</p>
          </details>
        ) : null}
        {props.children}
      </div>

      {props.onSave ? (
        <footer className="card__footer">
          <button
            type="button"
            className="btn btn--primary"
            disabled={props.saving}
            onClick={props.onSave}
          >
            {props.saving ? t('app.saving') : t('settings.save')}
          </button>
          {props.saved && !props.dirty ? (
            <span className="badge badge--ok" role="status">
              {t('settings.saved')}
            </span>
          ) : null}
          <span className="spacer" />
          {props.error ? (
            <span style={BAD_NOTE} role="alert">
              {props.error}
            </span>
          ) : null}
        </footer>
      ) : null}
    </section>
  );
}

export interface AsyncAction {
  busy: boolean;
  done: boolean;
  error: string | null;
  /**
   * Run `task`. `onError` gets first refusal on a failure: return true when it
   * has anchored the message under a field, false to let the section footer
   * carry it.
   */
  run: (
    task: () => Promise<void>,
    onError?: (cause: unknown) => boolean,
  ) => void;
  clear: () => void;
  fail: (message: string) => void;
}

export function useAsyncAction(t: Translate): AsyncAction {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards every setState after an await: a section can be unmounted while a
  // 60-second getUpdates poll is still in flight.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // A task that resolves after calling `fail` did NOT succeed: an `ok: false`
  // body is a refusal, not an exception, and must not raise the Saved badge.
  const failed = useRef(false);

  const run = useCallback(
    (task: () => Promise<void>, onError?: (cause: unknown) => boolean) => {
      failed.current = false;
      setBusy(true);
      setError(null);
      setDone(false);
      task()
        .then(() => {
          if (!alive.current || failed.current) return;
          setDone(true);
        })
        .catch((cause: unknown) => {
          if (!alive.current) return;
          if (isApiError(cause) && cause.isAborted) return;
          if (onError && onError(cause)) return;
          setError(errorSentence(cause, t));
        })
        .finally(() => {
          if (alive.current) setBusy(false);
        });
    },
    [t],
  );

  const clear = useCallback(() => {
    setDone(false);
    setError(null);
  }, []);

  const fail = useCallback((message: string) => {
    failed.current = true;
    setDone(false);
    setError(message);
  }, []);

  return { busy, done, error, run, clear, fail };
}

export interface SectionCommonProps {
  settings: SettingsOut;
  onSettings: (settings: SettingsOut) => void;
  reload: () => Promise<void>;
}
