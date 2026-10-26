/**
 * LV Copytrader — SymbolPicker
 * ---------------------------------------------------------------------------
 * The symbol allowlist of BUILD_PROMPT §6, chosen from the REAL account rather
 * than typed from memory: `GET /api/trading/symbols` is the broker's own table,
 * so what an operator selects is a name MT5 will actually accept.
 *
 * This is a safety-shaped control, and the copy is the safety. `symbol_allowlist`
 * is an OPTIONAL operator limit that ships DISABLED, where **empty means every
 * symbol is allowed**. Two operators must be able to read the truth off the
 * screen without asking anyone:
 *
 *   • one who picks three symbols expecting "the AI will trade these", and
 *   • one who picks none expecting "the AI will trade nothing".
 *
 * So the state is printed as a SENTENCE and never as a bare count — "No
 * restriction — the AI may trade any symbol the broker offers" against
 * "Restricted to 3 symbols" — and it sits in a `role="status"` region above the
 * list, where it cannot be missed and where a screen reader hears it change. The
 * unrestricted state is NOT painted as a danger: it is the documented default,
 * not a fault. It reads dim; the restricted state reads accent, because a limit
 * in force is the state worth spotting.
 *
 * The other three things this component is careful about:
 *
 *  • SEARCH IS THE INTERACTION, NOT SCROLLING. The account carries well over a
 *    thousand symbols. Input is debounced 250 ms and every request is capped;
 *    when the server reports `truncated` the meta line says "showing 300 of 1284
 *    — refine the search" instead of pretending the list is complete.
 *
 *  • A SYMBOL THE BROKER WILL NOT TRADE IS A TRAP. `tradable: false` renders the
 *    row visibly disabled, with the reason spelled out, and selection is refused
 *    — but `aria-disabled`, never `disabled`, so the keyboard can still land on
 *    it and hear WHY. A row already on the allowlist can always be pressed to
 *    come OFF it, tradable or not: removal must never be the thing that is
 *    barred.
 *
 *  • `mt5_unavailable` IS NOT AN EMPTY ACCOUNT. When the terminal is down the
 *    panel says where the list comes from, what to do about it, and that the
 *    symbols already saved are still enforced — it never renders as "your broker
 *    offers nothing".
 *
 * Interaction shape is DialogPicker's, deliberately: the same search field, the
 * same flush 40 px rows in a bordered scroller, the same badges, so the two
 * pickers read as one product. The keyboard is a listbox — arrows move through
 * the results from the search field, Enter toggles, `aria-selected` carries the
 * state, and roving tabindex keeps the browser's own focus ring on the row the
 * operator is standing on.
 *
 * One performance note that is really a feel note: a capped search holds 300
 * rows, so the row is its own memoised component and every callback it receives
 * is stable. Without that, each keystroke rebuilt 300 rows and the search field
 * stalled under the operator's fingers; the arrow keys did the same. `move`
 * reads the active index from a ref for exactly this reason.
 */
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent, ReactElement } from 'react';

import { getTradableSymbols, isApiError } from '@/api/client';
import type {
  RequestOptions,
  SymbolRow,
  SymbolsAccountOut,
  SymbolsOut,
  SymbolsQuery,
} from '@/api/client';
import { EMPTY_VALUE, formatInteger, formatNumber, useCurrentLocale, useT } from '@/i18n';
import type { Locale, Translate } from '@/i18n';

/* ─────────────────────────────── constants ──────────────────────────────── */

/** How long the search field rests before a request goes out. */
const DEBOUNCE_MS = 250;

/** Rows per request. The server caps too; this is what the UI asks for. */
const DEFAULT_LIMIT = 300;

/* ─────────────────────────────── props ──────────────────────────────────── */

export interface SymbolPickerProps {
  /**
   * The allowlist as stored (`limits.symbol_allowlist`). An EMPTY list is the
   * shipped default and means every symbol is allowed — never "none allowed".
   */
  selected: string[];
  /** Fired with the next allowlist. The parent owns persistence (PUT /settings/limits). */
  onChange: (next: string[]) => void;
  /** Greys the whole control out — e.g. while the section is saving. */
  disabled?: boolean;
  /** Set false to drop the card chrome and render the picker alone. */
  showHeader?: boolean;
  /** Rows to request per search. */
  limit?: number;
  /** Override the fetch — defaults to `GET /api/trading/symbols`. */
  fetchSymbols?: (query: SymbolsQuery, options?: RequestOptions) => Promise<SymbolsOut>;
}

/* ────────────────────────────── local styles ────────────────────────────── */

const BODY: CSSProperties = { padding: 0 };

const LIST_BOX: CSSProperties = {
  maxHeight: '360px',
  borderTop: 'var(--border-w) solid var(--border)',
};

const NOTE: CSSProperties = { color: 'var(--text-dim)', fontSize: 'var(--fs-1)' };

const EMPHASIS: CSSProperties = { color: 'var(--text)', fontSize: 'var(--fs-1)', fontWeight: 500 };

const BAD_NOTE: CSSProperties = { color: 'var(--danger)', fontSize: 'var(--fs-1)' };

/**
 * The paragraph that decides whether this control can be misread: what selecting
 * nothing means, and what selecting something means. Quiet, but boxed, exactly
 * like the "these ship disabled" statement above it in the Limits section.
 */
const STATEMENT: CSSProperties = {
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-2)',
  lineHeight: 'var(--lh)',
  padding: 'var(--sp-1) 0 var(--sp-1) var(--sp-3)',
  borderLeft: '2px solid var(--border-strong)',
};

/** The two-line live region: the state sentence, then what just happened. */
const STATE_BOX: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--sp-1)',
  minWidth: 0,
};

/** Restricted: a limit is in force, and that is worth spotting. Accent, not red. */
const STATE_ON: CSSProperties = {
  color: 'var(--accent)',
  fontSize: 'var(--fs-2)',
  fontWeight: 600,
  lineHeight: 'var(--lh)',
};

/** Unrestricted: the documented default. Calm — a warning here would be a lie. */
const STATE_OFF: CSSProperties = {
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-2)',
  fontWeight: 500,
  lineHeight: 'var(--lh)',
};

/**
 * The well holding the chosen symbols. Concentric: chips are `--radius-sm`
 * inside `--sp-2` of padding, so the well is the sum of the two.
 */
const WELL: CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  gap: 'var(--sp-2)',
  padding: 'var(--sp-2) 0',
  borderBottom: 'var(--border-w) solid var(--border)',
};

/** MT5 is down: warn, with what to do, never an empty-account shrug. */
const UNAVAILABLE: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--sp-2)',
  padding: 'var(--sp-1) 0 var(--sp-1) var(--sp-3)',
  borderLeft: '2px solid var(--warn)',
};

const WARN_TEXT: CSSProperties = {
  color: 'var(--warn)',
  fontSize: 'var(--fs-2)',
  fontWeight: 600,
  lineHeight: 'var(--lh)',
};


/* ───────────────────────────── small helpers ────────────────────────────── */

/**
 * The comparison key for an allowlist entry.
 *
 * `guards._check_symbol_allowlist` matches case-insensitively, so `XAUUSDM` typed
 * by hand yesterday and `XAUUSDm` picked from the table today are one entry here
 * too. What is STORED is always the broker's own spelling, taken from the row.
 */
function entryKey(name: string): string {
  return name.trim().toUpperCase();
}

/** Turn any thrown value into the sentence the picker will show. */
function errorText(cause: unknown, t: Translate): string {
  if (isApiError(cause)) return cause.message;
  if (cause instanceof Error && cause.message) return cause.message;
  return t('err.generic');
}

/**
 * A pip size or a lot size, at its own precision.
 *
 * Never `formatVolume`: its fixed two decimals would print a `volume_min` of
 * 0.001 as `0.00`, which is a lie about the smallest order the broker takes.
 */
function formatUnit(value: number, locale: Locale): string {
  if (!Number.isFinite(value)) return EMPTY_VALUE;
  return formatNumber(
    value,
    { minimumFractionDigits: 2, maximumFractionDigits: 8, useGrouping: false },
    locale,
  );
}

/* ──────────────────────────────── one row ───────────────────────────────── */

interface OptionRowProps {
  row: SymbolRow;
  index: number;
  /** On the allowlist. Carried by `aria-selected`, by the wash and by a word. */
  selected: boolean;
  /** Pressing does something: tradable, or already selected so it can come off. */
  pressable: boolean;
  /** The one row in the list that Tab reaches; the arrows move which one. */
  tabbable: boolean;
  locale: Locale;
  t: Translate;
  onToggle: (row: SymbolRow) => void;
  onFocus: (index: number) => void;
  onKeyDown: (event: KeyboardEvent<HTMLButtonElement>, index: number) => void;
  registerRef: (index: number, node: HTMLButtonElement | null) => void;
}

/**
 * One instrument. Memoised, and every callback it takes is stable, so typing in
 * the search field or arrowing down the list re-renders two rows rather than
 * three hundred.
 */
const OptionRow = memo(function OptionRow(props: OptionRowProps): ReactElement {
  const { row, index, selected, pressable, tabbable, locale, t } = props;
  const { onToggle, onFocus, onKeyDown, registerRef } = props;

  return (
    <button
      ref={(node) => {
        registerRef(index, node);
      }}
      type="button"
      role="option"
      aria-selected={selected}
      aria-disabled={pressable ? undefined : true}
      tabIndex={tabbable ? 0 : -1}
      className={selected ? 'option option--on' : 'option'}
      onFocus={() => onFocus(index)}
      onKeyDown={(event) => onKeyDown(event, index)}
      onClick={() => {
        if (!pressable) return;
        onToggle(row);
      }}
    >
      <span className="option__main">
        <span className="option__title">
          <span className="mono option__name">{row.name}</span>
          <span className="option__desc">{row.description || EMPTY_VALUE}</span>
        </span>
        {/* The broker's own folder, so Forex\Majors and Crypto are one glance apart. */}
        <span className="option__sub">{row.path || EMPTY_VALUE}</span>
        {row.tradable ? null : (
          <span className="option__reason">
            <span className="badge badge--warn">{t('settings.symbol_not_tradable')}</span>{' '}
            {t('settings.symbol_not_tradable_hint')}
          </span>
        )}
      </span>

      <span className="option__nums">
        <span className="option__num">
          {t('settings.symbol_pip')}
          <span className="mono">{formatUnit(row.pip_size, locale)}</span>
          {/* The convention that produced it, verbatim: a pip is not inferable (BUILD §2). */}
          <span className="mono mono--sm">{row.pip_convention}</span>
        </span>
        <span className="option__num">
          {t('settings.symbol_spread')}
          <span className="mono">{formatInteger(row.spread_points, locale)}</span>
        </span>
        <span className="option__num">
          {t('settings.symbol_min_lot')}
          <span className="mono">{formatUnit(row.volume_min, locale)}</span>
        </span>
      </span>

      {selected ? <span className="badge badge--accent">{t('settings.symbols_on_list')}</span> : null}
    </button>
  );
});

/* ──────────────────────────────── component ─────────────────────────────── */

export function SymbolPicker(props: SymbolPickerProps): ReactElement {
  const {
    selected,
    onChange,
    disabled = false,
    showHeader = true,
    limit = DEFAULT_LIMIT,
    fetchSymbols,
  } = props;

  const t = useT();
  const locale = useCurrentLocale();
  const uid = useId();
  const searchId = `${uid}-search`;
  const hintId = `${uid}-hint`;
  const listId = `${uid}-list`;

  /* ---- what the operator typed, and what has actually been requested ----- */
  const [query, setQuery] = useState('');
  const [applied, setApplied] = useState('');
  const [onlyTradable, setOnlyTradable] = useState(false);
  const [reloadTick, setReloadTick] = useState(0);

  /* ---- what came back --------------------------------------------------- */
  const [rows, setRows] = useState<SymbolRow[]>([]);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [account, setAccount] = useState<SymbolsAccountOut | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [failure, setFailure] = useState<string | null>(null);

  /* ---- keyboard position and the last thing that happened --------------- */
  const [activeIndex, setActiveIndex] = useState(-1);
  const [lastAction, setLastAction] = useState<string | null>(null);

  const searchRef = useRef<HTMLInputElement | null>(null);
  const rowRefs = useRef<(HTMLButtonElement | null)[]>([]);
  // The same number as `activeIndex`, readable without making `move` — and with
  // it every row's key handler — a new function on each arrow press.
  const activeRef = useRef(-1);

  // Held in a ref so an inline `fetchSymbols` prop cannot re-trigger the effect
  // on every parent render — that is a request loop, not a refresh.
  const fetchRef = useRef(fetchSymbols);
  useEffect(() => {
    fetchRef.current = fetchSymbols;
  }, [fetchSymbols]);

  /* ---- debounce: the first load is immediate, every keystroke waits ------ */
  const firstRun = useRef(true);
  useEffect(() => {
    const delay = firstRun.current ? 0 : DEBOUNCE_MS;
    firstRun.current = false;
    const timer = window.setTimeout(() => setApplied(query.trim()), delay);
    return () => window.clearTimeout(timer);
  }, [query]);

  /* ---- the request ------------------------------------------------------ */
  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    const call = fetchRef.current ?? getTradableSymbols;

    setLoading(true);
    setFailure(null);

    call(
      { query: applied || undefined, only_tradable: onlyTradable, limit },
      { signal: controller.signal },
    )
      .then((data) => {
        if (!alive) return;
        const list = Array.isArray(data.symbols) ? data.symbols : [];
        setRows(list);
        setTotal(typeof data.total === 'number' ? data.total : list.length);
        setTruncated(Boolean(data.truncated));
        setAccount(data.account ?? null);
        setUnavailable(Boolean(data.mt5_unavailable));
        activeRef.current = -1;
        setActiveIndex(-1);
        // `ok: false` without `mt5_unavailable` is a refusal with no story of
        // its own; say the list could not be read rather than show nothing.
        if (!data.ok && !data.mt5_unavailable) setFailure(t('settings.symbols_failed'));
      })
      .catch((cause: unknown) => {
        if (!alive) return;
        if (isApiError(cause) && cause.isAborted) return;
        setRows([]);
        setTotal(0);
        setTruncated(false);
        setUnavailable(false);
        setFailure(errorText(cause, t));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });

    return () => {
      alive = false;
      controller.abort();
    };
  }, [applied, limit, onlyTradable, reloadTick, t]);

  // The ref array outlives its rows; drop the tail so `move` cannot focus a node
  // that belonged to a result set two searches ago.
  useEffect(() => {
    rowRefs.current.length = rows.length;
  }, [rows]);

  /* ---- selection -------------------------------------------------------- */

  const selectedKeys = useMemo(() => new Set(selected.map(entryKey)), [selected]);
  const restricted = selected.length > 0;

  /** The whole point of the control, as a sentence. Never a bare count. */
  const stateSentence = useMemo(() => {
    if (!restricted) return t('settings.symbols_state_open');
    if (selected.length === 1) return t('settings.symbols_state_one');
    return t('settings.symbols_state_many', { count: formatInteger(selected.length, locale) });
  }, [locale, restricted, selected.length, t]);

  const remove = useCallback(
    (name: string) => {
      const wanted = entryKey(name);
      onChange(selected.filter((entry) => entryKey(entry) !== wanted));
      setLastAction(t('settings.symbols_removed', { name }));
    },
    [onChange, selected, t],
  );

  const toggle = useCallback(
    (row: SymbolRow) => {
      if (selectedKeys.has(entryKey(row.name))) {
        remove(row.name);
        return;
      }
      onChange([...selected, row.name]);
      setLastAction(t('settings.symbols_added', { name: row.name }));
    },
    [onChange, remove, selected, selectedKeys, t],
  );

  const clearAll = useCallback(() => {
    onChange([]);
    setLastAction(t('settings.symbols_cleared'));
  }, [onChange, t]);

  /** True when pressing the row would do something. Removal is never barred. */
  const pressable = useCallback(
    (row: SymbolRow): boolean =>
      !disabled && (row.tradable || selectedKeys.has(entryKey(row.name))),
    [disabled, selectedKeys],
  );

  /* ---- keyboard --------------------------------------------------------- */

  const registerRef = useCallback((index: number, node: HTMLButtonElement | null) => {
    rowRefs.current[index] = node;
  }, []);

  const setActive = useCallback((index: number) => {
    activeRef.current = index;
    setActiveIndex(index);
  }, []);

  const focusRow = useCallback(
    (index: number) => {
      setActive(index);
      rowRefs.current[index]?.focus();
    },
    [setActive],
  );

  const move = useCallback(
    (delta: number) => {
      const count = rowRefs.current.length;
      if (count === 0) return;
      const from = activeRef.current;
      const next =
        from < 0 ? (delta > 0 ? 0 : count - 1) : Math.min(count - 1, Math.max(0, from + delta));
      focusRow(next);
    },
    [focusRow],
  );

  const toSearch = useCallback(() => {
    setActive(-1);
    searchRef.current?.focus();
  }, [setActive]);

  const onSearchKeyDown = useCallback(
    (event: KeyboardEvent<HTMLInputElement>) => {
      switch (event.key) {
        case 'ArrowDown':
          event.preventDefault();
          move(1);
          break;
        case 'ArrowUp':
          event.preventDefault();
          move(-1);
          break;
        case 'Enter': {
          event.preventDefault();
          const row = activeRef.current >= 0 ? rows[activeRef.current] : undefined;
          if (row && pressable(row)) toggle(row);
          break;
        }
        case 'Escape':
          if (query) {
            event.preventDefault();
            setQuery('');
          }
          break;
        default:
          break;
      }
    },
    [move, pressable, query, rows, toggle],
  );

  const onRowKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
      switch (event.key) {
        case 'ArrowDown':
          event.preventDefault();
          move(1);
          break;
        case 'ArrowUp':
          event.preventDefault();
          // Off the top of the list is back into the search field, not nowhere.
          if (index === 0) toSearch();
          else move(-1);
          break;
        case 'Home':
          event.preventDefault();
          focusRow(0);
          break;
        case 'End':
          event.preventDefault();
          focusRow(rowRefs.current.length - 1);
          break;
        case 'Escape':
          event.preventDefault();
          toSearch();
          break;
        default:
          break;
      }
    },
    [focusRow, move, toSearch],
  );

  // Exactly one row is tab-reachable; the arrows move which one. Before the
  // first arrow that is the top row, so Tab never drops the list entirely.
  const focusIndex = activeIndex >= 0 ? Math.min(activeIndex, rows.length - 1) : 0;

  /* ---- render ----------------------------------------------------------- */

  const shown = formatInteger(rows.length, locale);
  const matched = formatInteger(total || rows.length, locale);

  const body = (
    <div className="stack" style={BODY}>
      {/* What an empty list means, and what a filled one means. Always visible. */}
      <p style={STATEMENT}>{t('settings.symbols_meaning')}</p>

      {/* The current state as a sentence, announced whenever it changes. */}
      <div style={STATE_BOX} role="status">
        <p style={restricted ? STATE_ON : STATE_OFF}>{stateSentence}</p>
        {lastAction ? <p style={NOTE}>{lastAction}</p> : null}
      </div>

      {restricted ? (
        <div style={WELL} role="group" aria-label={t('settings.symbols_selected')}>
          {selected.map((name, index) => (
            <span className="chip" key={`${entryKey(name)}-${index}`}>
              <span className="chip__label">{name}</span>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                disabled={disabled}
                aria-label={t('settings.symbols_remove_one', { name })}
                onClick={() => remove(name)}
              >
                <span aria-hidden="true">✕</span>
              </button>
            </span>
          ))}
          <span className="spacer" />
          <button type="button" className="btn btn--sm" disabled={disabled} onClick={clearAll}>
            {t('settings.symbols_clear')}
          </button>
        </div>
      ) : null}

      <div className="picker-toolbar">
        <div className="field">
          <label className="field__label" htmlFor={searchId}>
            {t('settings.search_symbols')}
          </label>
          <input
            id={searchId}
            ref={searchRef}
            type="search"
            value={query}
            disabled={disabled}
            placeholder={t('settings.symbols_search_placeholder')}
            autoComplete="off"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            aria-controls={rows.length > 0 ? listId : undefined}
            aria-describedby={hintId}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onSearchKeyDown}
          />
          <p className="field__hint" id={hintId}>
            {t('settings.symbols_keyboard_hint')}
          </p>
        </div>
        <div className="row row--tight row--wrap">
          <button
            type="button"
            className={onlyTradable ? 'tab is-active' : 'tab'}
            aria-pressed={onlyTradable}
            disabled={disabled}
            onClick={() => setOnlyTradable((value) => !value)}
          >
            {t('settings.symbols_only_tradable')}
          </button>
          <button
            type="button"
            className="btn btn--sm"
            disabled={disabled || loading}
            onClick={() => setReloadTick((tick) => tick + 1)}
          >
            {loading ? t('app.loading') : t('settings.symbols_reload')}
          </button>
        </div>
      </div>

      <div className="row row--wrap">
        {account ? (
          /*
           * The login is an IDENTIFIER, not a quantity: never grouped, or the
           * account on screen (474 436 674) stops matching the one the operator
           * typed into the MT5 section. Monospace and tabular, like every other
           * machine string in the cockpit.
           */
          <span className="mono mono--sm" style={NOTE}>
            {t('settings.symbols_account', {
              login: account.login === null ? EMPTY_VALUE : String(account.login),
              server: account.server || EMPTY_VALUE,
            })}
          </span>
        ) : null}
        <span className="spacer" />
        {rows.length > 0 ? (
          <span style={truncated ? EMPHASIS : NOTE}>
            {truncated
              ? t('settings.symbols_truncated', { shown, total: matched })
              : t('settings.symbols_count', { count: matched })}
          </span>
        ) : null}
      </div>

      {failure ? (
        <p style={BAD_NOTE} role="alert">
          {failure}
        </p>
      ) : null}

      {unavailable ? (
        /* Not an empty account: the table could not be read at all. */
        <div style={UNAVAILABLE}>
          <p style={WARN_TEXT}>{t('settings.symbols_unavailable')}</p>
          <p style={NOTE}>{t('settings.symbols_unavailable_hint')}</p>
          <div className="row">
            <button
              type="button"
              className="btn btn--sm"
              disabled={loading}
              onClick={() => setReloadTick((tick) => tick + 1)}
            >
              {t('settings.symbols_reload')}
            </button>
          </div>
        </div>
      ) : loading && rows.length === 0 ? (
        <p style={NOTE}>{t('settings.symbols_loading')}</p>
      ) : rows.length === 0 ? (
        <div className="empty">
          <p className="empty__title">{t('settings.symbols_empty')}</p>
          <p className="empty__text">{t('settings.symbols_empty_hint')}</p>
        </div>
      ) : (
        <div
          id={listId}
          className="scroll-y"
          style={LIST_BOX}
          role="listbox"
          aria-multiselectable="true"
          aria-label={t('settings.pick_symbols')}
        >
          {rows.map((row, index) => (
            <OptionRow
              key={row.name}
              row={row}
              index={index}
              selected={selectedKeys.has(entryKey(row.name))}
              pressable={pressable(row)}
              tabbable={index === focusIndex}
              locale={locale}
              t={t}
              onToggle={toggle}
              onFocus={setActive}
              onKeyDown={onRowKeyDown}
              registerRef={registerRef}
            />
          ))}
        </div>
      )}

      {/* A legend for columns that are not on screen is noise, not help. */}
      {rows.length > 0 ? (
        <p className="field__hint">{t('settings.symbols_numbers_hint')}</p>
      ) : null}
    </div>
  );

  if (!showHeader) return body;

  return (
    <section className="card card--flush">
      <header className="card__header">
        <h2 className="card__title">{t('settings.pick_symbols')}</h2>
        <div className="card__actions">
          <span className={restricted ? 'badge badge--accent' : 'badge badge--dim'}>
            {restricted ? (
              <>
                <span className="badge__num">{formatInteger(selected.length, locale)}</span>
                {t('settings.symbols_selected')}
              </>
            ) : (
              t('app.all')
            )}
          </span>
        </div>
      </header>
      {body}
    </section>
  );
}

export default SymbolPicker;
