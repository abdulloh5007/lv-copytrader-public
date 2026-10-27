/**
 * LV Copytrader — i18n runtime.
 *
 * Contract (CONTRACTS §20, BUILD_PROMPT §10):
 *   - `t(key, vars?)` is the ONLY way a string reaches the DOM.
 *   - A missing key renders the key itself and warns on the console: gaps must be
 *     visible, never silent. The `<T>` component paints such a key in `--warn`.
 *   - The initial locale comes from `document.documentElement.lang`, which the
 *     pre-paint script in index.html already set, so nothing flashes English.
 *   - `setLocale` persists to localStorage 'sp-locale', updates <html lang> and
 *     re-renders every subscriber.
 *   - Numbers and dates go through Intl with the ACTIVE locale.
 *
 * This file is `.ts`, not `.tsx`, so components are built with `createElement`.
 *
 * Usage:
 *   const t = useT();                       // t('nav.cockpit')
 *   const { locale, setLocale } = useLocale();
 *   <T k="settings.limits_off_note" />      // paints --warn if the key is gone
 */
import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import type { ReactElement, ReactNode } from 'react';

import { en } from './en';
import type { TranslationKey } from './en';
import { ru } from './ru';
import { uz } from './uz';

/* ─────────────────────────── types and constants ────────────────────────── */

export type Locale = 'en' | 'ru' | 'uz';

/** Any dictionary key, with autocomplete, while still allowing computed keys. */
export type TKey = TranslationKey | (string & {});

export type TranslationVars = Record<string, string | number | null | undefined>;

export type Translate = (key: TKey, vars?: TranslationVars) => string;

export const LOCALES: readonly Locale[] = ['en', 'ru', 'uz'] as const;

/** Self-labelled, deliberately NOT translated — a language picker names itself. */
export const LOCALE_LABELS: Record<Locale, string> = { en: 'English', ru: 'Русский', uz: 'O‘zbekcha' };

/** The key the pre-paint bootstrap in index.html reads and writes. */
export const LOCALE_STORAGE_KEY = 'sp-locale';

/** BCP-47 tags handed to Intl for each app locale. */
export const INTL_LOCALES: Record<Locale, string> = { en: 'en-GB', ru: 'ru-RU', uz: 'uz-UZ' };

/** Rendered where a number or a date is genuinely absent. */
export const EMPTY_VALUE = '—';

const DICTS: Record<Locale, Record<string, string>> = { en, ru, uz };

export type { TranslationKey };
export { en, ru, uz };

/* ───────────────────────────── locale detection ─────────────────────────── */

export function normalizeLocale(value: unknown): Locale | null {
  if (typeof value !== 'string') return null;
  const v = value.trim().toLowerCase();
  if (v.startsWith('ru')) return 'ru';
  if (v.startsWith('en')) return 'en';
  if (v.startsWith('uz')) return 'uz';
  return null;
}

function fromDocument(): Locale | null {
  try {
    if (typeof document === 'undefined') return null;
    return normalizeLocale(document.documentElement.getAttribute('lang'));
  } catch {
    return null;
  }
}

function fromStorage(): Locale | null {
  try {
    if (typeof localStorage === 'undefined') return null;
    return normalizeLocale(localStorage.getItem(LOCALE_STORAGE_KEY));
  } catch {
    // localStorage throws outright in some privacy modes (BUILD_PROMPT §10).
    return null;
  }
}

function fromNavigator(): Locale | null {
  try {
    if (typeof navigator === 'undefined') return null;
    const candidates: string[] = [];
    if (Array.isArray(navigator.languages)) candidates.push(...navigator.languages);
    if (navigator.language) candidates.push(navigator.language);
    for (const candidate of candidates) {
      const hit = normalizeLocale(candidate);
      if (hit) return hit;
    }
    return null;
  } catch {
    return null;
  }
}

/** html[lang] (set before first paint) → localStorage → navigator → 'en'. */
export function detectLocale(): Locale {
  return fromDocument() ?? fromStorage() ?? fromNavigator() ?? 'en';
}

/* ─────────────────────────── module-level locale ────────────────────────── */

let currentLocale: Locale = detectLocale();

type LocaleListener = (locale: Locale) => void;
const listeners = new Set<LocaleListener>();

export function getLocale(): Locale {
  return currentLocale;
}

export function subscribeLocale(listener: LocaleListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function applyLocaleAttribute(locale: Locale): void {
  try {
    if (typeof document !== 'undefined') {
      document.documentElement.setAttribute('lang', locale);
      const url = new URL(location.href);
      if (url.searchParams.has('lang')) {
        url.searchParams.set('lang', locale);
        history.replaceState(history.state, '', url.href);
      }
    }
  } catch {
    // A detached document in a test environment: nothing to apply to.
  }
}

/**
 * Switch the active locale: persist it, stamp <html lang>, re-render everything
 * subscribed. Safe to call from outside React.
 */
export function setLocale(next: Locale): void {
  const locale = normalizeLocale(next);
  if (!locale) return;
  applyLocaleAttribute(locale);
  if (locale === currentLocale) return;
  currentLocale = locale;
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(LOCALE_STORAGE_KEY, locale);
    }
  } catch {
    // Preference is not persistable here; the session still switches.
  }
  for (const listener of Array.from(listeners)) listener(locale);
}

/* ──────────────────────────────── translate ─────────────────────────────── */

const warned = new Set<string>();

function warnMissing(locale: Locale, key: string): void {
  const marker = `${locale}:${key}`;
  if (warned.has(marker)) return;
  warned.add(marker);
  // Gaps must be loud. Rendering the key is only half of it.
  console.warn(`[i18n] missing key "${key}" for locale "${locale}"`);
}

export function hasKey(key: TKey, locale: Locale = currentLocale): boolean {
  return Object.prototype.hasOwnProperty.call(DICTS[locale], key as string);
}

const PLACEHOLDER = /\{(\w+)\}/g;

export function interpolate(template: string, vars?: TranslationVars): string {
  if (!vars) return template;
  return template.replace(PLACEHOLDER, (match, name: string) => {
    const value = vars[name];
    if (value === undefined || value === null) return match; // leave the gap visible
    return String(value);
  });
}

/** Translate in an explicit locale. `translate(locale, key, vars)`. */
export function translate(locale: Locale, key: TKey, vars?: TranslationVars): string {
  const dict = DICTS[locale] ?? DICTS.en;
  const raw = Object.prototype.hasOwnProperty.call(dict, key as string)
    ? dict[key as string]
    : undefined;
  if (raw === undefined) {
    warnMissing(locale, String(key));
    return String(key);
  }
  return interpolate(raw, vars);
}

/** Translate in the active locale. Usable outside React. */
export const t: Translate = (key, vars) => translate(currentLocale, key, vars);

/* ──────────────────────────────── context ───────────────────────────────── */

export interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: Translate;
  has: (key: TKey) => boolean;
  dict: Record<string, string>;
}

function buildValue(locale: Locale): I18nContextValue {
  return {
    locale,
    setLocale,
    t: (key, vars) => translate(locale, key, vars),
    has: (key) => hasKey(key, locale),
    dict: DICTS[locale],
  };
}

export const I18nContext = createContext<I18nContextValue>(buildValue(currentLocale));
I18nContext.displayName = 'I18nContext';

export interface I18nProviderProps {
  children?: ReactNode;
  /** Force a locale (tests, storybook). Omitted in the app: detection wins. */
  locale?: Locale;
}

export function I18nProvider(props: I18nProviderProps): ReactElement {
  const [locale, setLocaleState] = useState<Locale>(() => props.locale ?? getLocale());

  useEffect(() => {
    if (props.locale && props.locale !== getLocale()) setLocale(props.locale);
    else applyLocaleAttribute(getLocale());
    setLocaleState(getLocale());

    const unsubscribe = subscribeLocale((next) => setLocaleState(next));

    const onStorage = (event: StorageEvent): void => {
      if (event.key !== LOCALE_STORAGE_KEY) return;
      const next = normalizeLocale(event.newValue);
      if (next && next !== getLocale()) setLocale(next);
    };
    if (typeof window !== 'undefined') window.addEventListener('storage', onStorage);

    return () => {
      unsubscribe();
      if (typeof window !== 'undefined') window.removeEventListener('storage', onStorage);
    };
  }, [props.locale]);

  const value = useMemo<I18nContextValue>(() => buildValue(locale), [locale]);
  return createElement(I18nContext.Provider, { value }, props.children);
}

/** Everything the context carries. */
export function useI18n(): I18nContextValue {
  return useContext(I18nContext);
}

/** `const { locale, setLocale } = useLocale()`. */
export function useLocale(): { locale: Locale; setLocale: (locale: Locale) => void } {
  const ctx = useContext(I18nContext);
  const set = useCallback((next: Locale) => ctx.setLocale(next), [ctx]);
  return { locale: ctx.locale, setLocale: set };
}

/** The bare locale string, for a component that only formats. */
export function useCurrentLocale(): Locale {
  return useContext(I18nContext).locale;
}

/** `const t = useT()` — re-renders when the locale changes. */
export function useT(): Translate {
  return useContext(I18nContext).t;
}

/* ───────────────────────────── the <T> element ──────────────────────────── */

export interface TProps {
  /** Dictionary key. */
  k: TKey;
  /** `{name}` placeholders. */
  vars?: TranslationVars;
  /** Tag to render, default `span`. */
  as?: string;
  className?: string;
  title?: string;
  id?: string;
}

/**
 * Renders one key. When the key is missing it renders the key itself in
 * `var(--warn)` and marks the node `data-i18n-missing`, so a hole is visible on
 * the page and greppable in the DOM.
 */
export function T(props: TProps): ReactElement {
  const ctx = useContext(I18nContext);
  const missing = !ctx.has(props.k);
  const text = ctx.t(props.k, props.vars);

  const attrs: Record<string, unknown> = {};
  if (props.className !== undefined) attrs.className = props.className;
  if (props.title !== undefined) attrs.title = props.title;
  if (props.id !== undefined) attrs.id = props.id;
  if (missing) {
    attrs.style = { color: 'var(--warn)' };
    attrs['data-i18n-missing'] = 'true';
  }
  return createElement(props.as ?? 'span', attrs, text);
}

/* ──────────────────────────────── formatting ────────────────────────────── */

const numberFormatters = new Map<string, Intl.NumberFormat>();
const dateFormatters = new Map<string, Intl.DateTimeFormat>();

function numberFormatter(locale: Locale, options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const cacheKey = `${locale}|${JSON.stringify(options)}`;
  let formatter = numberFormatters.get(cacheKey);
  if (!formatter) {
    formatter = new Intl.NumberFormat(INTL_LOCALES[locale], options);
    numberFormatters.set(cacheKey, formatter);
  }
  return formatter;
}

function dateFormatter(locale: Locale, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const cacheKey = `${locale}|${JSON.stringify(options)}`;
  let formatter = dateFormatters.get(cacheKey);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(INTL_LOCALES[locale], options);
    dateFormatters.set(cacheKey, formatter);
  }
  return formatter;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

export type Numeric = number | null | undefined;

/** Grouped number. `formatNumber(1234.5)` → `1,234.5` / `1 234,5`. */
export function formatNumber(
  value: Numeric,
  options?: Intl.NumberFormatOptions,
  locale: Locale = currentLocale,
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return numberFormatter(locale, options ?? {}).format(value);
}

/** Whole number, grouped, no fraction digits. */
export function formatInteger(value: Numeric, locale: Locale = currentLocale): string {
  return formatNumber(value, { maximumFractionDigits: 0 }, locale);
}

/**
 * A price at the symbol's digits, ungrouped so a streaming quote never changes
 * width. Pair with `.mono` (`font-variant-numeric: tabular-nums`).
 */
export function formatPrice(
  value: Numeric,
  digits = 2,
  locale: Locale = currentLocale,
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  const d = Math.max(0, Math.min(8, Math.trunc(isFiniteNumber(digits) ? digits : 2)));
  return numberFormatter(locale, {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
    useGrouping: false,
  }).format(value);
}

/** A lot size: always two decimals, the way MT5 shows volume. */
export function formatVolume(value: Numeric, locale: Locale = currentLocale): string {
  return formatNumber(
    value,
    { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: false },
    locale,
  );
}

/** Account currency, e.g. `formatCurrency(-12.4, 'USD')`. */
export function formatCurrency(
  value: Numeric,
  currency: string | null | undefined,
  locale: Locale = currentLocale,
  options?: Intl.NumberFormatOptions,
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  const code = (currency ?? '').trim().toUpperCase();
  const base: Intl.NumberFormatOptions = {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    ...(options ?? {}),
  };
  if (!/^[A-Z]{3}$/.test(code)) {
    const plain = numberFormatter(locale, base).format(value);
    return code ? `${plain} ${code}` : plain;
  }
  const currencyOptions: Intl.NumberFormatOptions = { ...base, style: 'currency', currency: code };
  try {
    // narrowSymbol keeps a USD account reading "$1,204.50" rather than "US$1,204.50".
    // Assigned through a record cast: the literal is only typed from lib ES2020 up,
    // and this module must compile whatever `lib` the tsconfig settles on.
    const withNarrowSymbol = { ...currencyOptions } as Record<string, unknown>;
    if (withNarrowSymbol.currencyDisplay === undefined) {
      withNarrowSymbol.currencyDisplay = 'narrowSymbol';
    }
    return numberFormatter(locale, withNarrowSymbol as Intl.NumberFormatOptions).format(value);
  } catch {
    try {
      return numberFormatter(locale, currencyOptions).format(value);
    } catch {
      // An exotic code Intl refuses: number + code rather than a thrown render.
      return `${numberFormatter(locale, base).format(value)} ${code}`;
    }
  }
}

function withSign(value: number, formatted: string): string {
  return value > 0 ? `+${formatted}` : formatted;
}

/** P/L with an explicit `+` — the sign is a second carrier next to the colour. */
export function formatSignedNumber(
  value: Numeric,
  options?: Intl.NumberFormatOptions,
  locale: Locale = currentLocale,
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return withSign(value, formatNumber(value, options, locale));
}

/** P/L in account currency with an explicit `+`. */
export function formatSignedCurrency(
  value: Numeric,
  currency: string | null | undefined,
  locale: Locale = currentLocale,
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return withSign(value, formatCurrency(value, currency, locale));
}

/** `value` is already a percentage (1.5 → `1.5%`), not a 0..1 ratio. */
export function formatPercent(
  value: Numeric,
  digits = 2,
  locale: Locale = currentLocale,
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  const d = Math.max(0, Math.min(6, Math.trunc(isFiniteNumber(digits) ? digits : 2)));
  return `${numberFormatter(locale, { minimumFractionDigits: 0, maximumFractionDigits: d }).format(value)}%`;
}

/** A 0..1 ratio rendered as a percentage (0.62 → `62%`). */
export function formatRatioPercent(
  value: Numeric,
  digits = 1,
  locale: Locale = currentLocale,
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return formatPercent(value * 100, digits, locale);
}

/* ─────────────────────────────── date and time ──────────────────────────── */

export type DateInput = Date | string | number | null | undefined;

/**
 * Backend timestamps are either unix seconds (`ts`, `*_ts`) or a timezone-naive
 * UTC ISO string (CONTRACTS §7). A naive string is stamped `Z` so the browser
 * does not read it as local time and show a trade three hours in the future.
 */
export function toDate(value: DateInput): Date | null {
  if (value === null || value === undefined) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    const ms = Math.abs(value) < 1e12 ? value * 1000 : value;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const raw = value.trim();
  if (!raw) return null;
  const naiveIso = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(raw);
  const normalized = naiveIso ? `${raw.replace(' ', 'T')}Z` : raw;
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

const DATE_OPTIONS: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
};

const TIME_OPTIONS: Intl.DateTimeFormatOptions = {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
};

/** `24.08.2026, 19:41:07` — 24-hour in both locales, cockpit style. */
export function formatDateTime(
  value: DateInput,
  locale: Locale = currentLocale,
  options?: Intl.DateTimeFormatOptions,
): string {
  const date = toDate(value);
  if (!date) return EMPTY_VALUE;
  return dateFormatter(locale, options ?? { ...DATE_OPTIONS, ...TIME_OPTIONS }).format(date);
}

export function formatDate(value: DateInput, locale: Locale = currentLocale): string {
  const date = toDate(value);
  if (!date) return EMPTY_VALUE;
  return dateFormatter(locale, DATE_OPTIONS).format(date);
}

export function formatTime(value: DateInput, locale: Locale = currentLocale): string {
  const date = toDate(value);
  if (!date) return EMPTY_VALUE;
  return dateFormatter(locale, TIME_OPTIONS).format(date);
}

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

/**
 * A compact duration in seconds: `45s`, `3m 20s`, `2h 05m`, `4d 06h`.
 * Unit suffixes come from the dictionary, so they translate.
 */
export function formatDuration(seconds: Numeric, locale: Locale = currentLocale): string {
  if (!isFiniteNumber(seconds)) return EMPTY_VALUE;
  const total = Math.max(0, Math.round(Math.abs(seconds)));
  const unit = (key: TKey): string => translate(locale, key);
  if (total < MINUTE) return `${total}${unit('app.unit_s')}`;
  if (total < HOUR) {
    const m = Math.floor(total / MINUTE);
    const s = total % MINUTE;
    return s ? `${m}${unit('app.unit_m')} ${String(s).padStart(2, '0')}${unit('app.unit_s')}`
             : `${m}${unit('app.unit_m')}`;
  }
  if (total < DAY) {
    const h = Math.floor(total / HOUR);
    const m = Math.floor((total % HOUR) / MINUTE);
    return m ? `${h}${unit('app.unit_h')} ${String(m).padStart(2, '0')}${unit('app.unit_m')}`
             : `${h}${unit('app.unit_h')}`;
  }
  const d = Math.floor(total / DAY);
  const h = Math.floor((total % DAY) / HOUR);
  return h ? `${d}${unit('app.unit_d')} ${String(h).padStart(2, '0')}${unit('app.unit_h')}`
           : `${d}${unit('app.unit_d')}`;
}

/** Latency in milliseconds: `840ms`, `1.2s`. */
export function formatLatency(ms: Numeric, locale: Locale = currentLocale): string {
  if (!isFiniteNumber(ms)) return EMPTY_VALUE;
  if (Math.abs(ms) < 1000) {
    return `${formatNumber(Math.round(ms), { maximumFractionDigits: 0 }, locale)}${translate(locale, 'app.unit_ms')}`;
  }
  return `${formatNumber(ms / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 }, locale)}${translate(locale, 'app.unit_s')}`;
}

type RelativeFormatter = { format(value: number, unit: string): string };
type RelativeCtor = new (locale: string, options?: Record<string, unknown>) => RelativeFormatter;

/**
 * Read through a cast rather than the global type: `Intl.RelativeTimeFormat` is
 * only declared from lib ES2020 upward, and this must compile whatever the
 * tsconfig picks. When the runtime lacks it, the dictionary fallback is used.
 */
const RelativeTimeFormatCtor: RelativeCtor | undefined = (
  Intl as unknown as { RelativeTimeFormat?: RelativeCtor }
).RelativeTimeFormat;

const relativeFormatters = new Map<Locale, RelativeFormatter>();

function relativeFormatter(locale: Locale): RelativeFormatter | null {
  if (!RelativeTimeFormatCtor) return null;
  let formatter = relativeFormatters.get(locale);
  if (!formatter) {
    formatter = new RelativeTimeFormatCtor(INTL_LOCALES[locale], { numeric: 'auto' });
    relativeFormatters.set(locale, formatter);
  }
  return formatter;
}

/** `12 minutes ago` / `12 минут назад`. Accepts a Date, an ISO string or unix seconds. */
export function formatRelative(
  value: DateInput,
  locale: Locale = currentLocale,
  now: DateInput = new Date(),
): string {
  const date = toDate(value);
  if (!date) return EMPTY_VALUE;
  const reference = toDate(now) ?? new Date();
  const deltaSeconds = Math.round((date.getTime() - reference.getTime()) / 1000);
  const magnitude = Math.abs(deltaSeconds);

  const formatter = relativeFormatter(locale);
  if (formatter) {
    if (magnitude < 45) return formatter.format(deltaSeconds, 'second');
    if (magnitude < 45 * MINUTE) return formatter.format(Math.round(deltaSeconds / MINUTE), 'minute');
    if (magnitude < 22 * HOUR) return formatter.format(Math.round(deltaSeconds / HOUR), 'hour');
    if (magnitude < 26 * DAY) return formatter.format(Math.round(deltaSeconds / DAY), 'day');
    return formatter.format(Math.round(deltaSeconds / (30 * DAY)), 'month');
  }

  if (magnitude < 5) return translate(locale, 'time.just_now');
  const span = formatDuration(magnitude, locale);
  return deltaSeconds < 0
    ? translate(locale, 'time.ago', { value: span })
    : translate(locale, 'time.in', { value: span });
}

/** Seconds of signal age, the way the feed shows it. */
export function formatAge(seconds: Numeric, locale: Locale = currentLocale): string {
  if (!isFiniteNumber(seconds)) return EMPTY_VALUE;
  return formatDuration(seconds, locale);
}
