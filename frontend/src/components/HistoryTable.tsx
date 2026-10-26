/**
 * LV Copytrader — HistoryTable
 * ---------------------------------------------------------------------------
 * Closed trades with their realized P/L, the signal that caused each, and the
 * per-source aggregates above them (BUILD_PROMPT §9 "History",
 * CONTRACTS §18 `/api/history/trades` and `/api/history/aggregates`).
 *
 * Points of care:
 *
 *  • THE CLOSE CAUSE IS A WORD, never a code: take profit, stop loss, closed by
 *    the AI, closed manually, kill switch. `CloseCause` is a closed set in
 *    client.ts, and `unknown` is a real member of it rather than a hole.
 *
 *  • FILTERS VALIDATE IN PLACE. A start date after the end date renders next to
 *    the field, never as a toast that disappears (BUILD §9 last paragraph).
 *
 *  • Every number is `.mono` with tabular figures, and every P/L carries an
 *    explicit sign next to its colour.
 */
import { useCallback, useMemo, useState } from 'react';
import type { CSSProperties, ReactElement, ReactNode } from 'react';
import { Link } from 'react-router-dom';

import type {
  AggregatesOut,
  CloseCause,
  HistoryQuery,
  Side,
  TradeOut,
} from '@/api/client';
import {
  EMPTY_VALUE,
  formatDateTime,
  formatDuration,
  formatInteger,
  formatNumber,
  formatPrice,
  formatRatioPercent,
  formatSignedCurrency,
  formatSignedNumber,
  formatVolume,
  useCurrentLocale,
  useT,
} from '@/i18n';
import type { Locale, TranslationKey, Translate } from '@/i18n';

/* ─────────────────────────────── props ──────────────────────────────────── */

/** How the parent turns a row into a link to the signal card that caused it. */
export type TradeSignalHref = (trade: TradeOut) => string | null;

export interface HistoryTableProps {
  /** Rows from `GET /api/history/trades`. */
  trades: TradeOut[];
  /** Aggregates from `GET /api/history/aggregates`; null hides the panel body. */
  aggregates?: AggregatesOut | null;
  /** Account currency, for every money column. */
  currency?: string | null;
  /** True while a fetch is in flight. */
  loading?: boolean;
  /** A fetch failure, already turned into a sentence by the caller. */
  error?: string | null;
  /** The filter values currently in force, so the form can start from them. */
  initialQuery?: HistoryQuery;
  /**
   * Symbols offered by the symbol filter. When omitted, the symbols present in
   * `trades` are used, which is correct for an unfiltered first load.
   */
  symbols?: string[];
  /** Fires when the operator presses Apply or Reset. */
  onApply?: (query: HistoryQuery) => void;
  /** Link target for the originating signal card; default `/?signal=<id>`. */
  signalHref?: TradeSignalHref;
  /**
   * Price precision for a row. `TradeOut` does not carry `digits` (CONTRACTS
   * §7), so the default reads the decimals actually present in the stored
   * prices rather than inventing a precision the symbol does not have.
   */
  digitsFor?: (trade: TradeOut) => number;
  /** Set false to drop the card chrome and render filters + table alone. */
  showHeader?: boolean;
}

/* ────────────────────────────── local styles ────────────────────────────── */

const FILTER_BAR: CSSProperties = {
  padding: 'var(--sp-3)',
  borderBottom: 'var(--border-w) solid var(--border)',
};
const FIELD_W: CSSProperties = { width: '160px' };
const STAT_GRID: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
  gap: 'var(--sp-2)',
  padding: 'var(--sp-3)',
  borderBottom: 'var(--border-w) solid var(--border)',
};
const STAT_TILE: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--sp-1)',
  padding: 'var(--sp-2)',
  background: 'var(--surface-2)',
  border: 'var(--border-w) solid var(--border)',
  borderRadius: 'var(--radius)',
};
const STAT_LABEL: CSSProperties = {
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-1)',
  fontWeight: 600,
  letterSpacing: '0.04em',
  textTransform: 'uppercase',
};
const SUB_TITLE: CSSProperties = {
  padding: 'var(--sp-2) var(--sp-3)',
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-1)',
  fontWeight: 600,
  letterSpacing: '0.04em',
  textTransform: 'uppercase',
};
/* The split chip trails the reference it belongs to; the badge brings its own
   colour, so this is only the gap. (It used to borrow the permalink link's
   style object, which has since become the .iconlink class.) */
const SPLIT_CHIP: CSSProperties = { marginLeft: 'var(--sp-1)' };
const SIGNAL_CELL: CSSProperties = { maxWidth: '220px' };

const COLUMN_COUNT = 12;

/** How many rows to ask for when the operator has not said otherwise. */
const DEFAULT_LIMIT = 200;

/* ───────────────────────────── small helpers ────────────────────────────── */

/** The dictionary key for every member of the closed `CloseCause` set. */
const CAUSE_LABELS: Record<CloseCause, TranslationKey> = {
  tp: 'trade.cause_tp',
  sl: 'trade.cause_sl',
  ai: 'trade.cause_ai',
  manual: 'trade.cause_manual',
  kill_switch: 'trade.cause_kill_switch',
  unknown: 'trade.cause_unknown',
};

/** The badge tone each cause gets; the word is always printed alongside it. */
const CAUSE_TONE: Record<CloseCause, string> = {
  tp: 'badge badge--long',
  sl: 'badge badge--short',
  ai: 'badge badge--accent',
  manual: 'badge badge--dim',
  kill_switch: 'badge badge--warn',
  unknown: 'badge badge--dim',
};

/** The colour class for a signed money cell; the sign carries it too. */
function profitClass(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return 'num mono is-dim';
  if (value > 0) return 'num mono is-long';
  if (value < 0) return 'num mono is-short';
  return 'num mono is-dim';
}

/** Milliseconds a trade was held, or null when either end is missing. */
function heldSeconds(trade: TradeOut): number | null {
  if (!trade.opened_at || !trade.closed_at) return null;
  const opened = Date.parse(trade.opened_at);
  const closed = Date.parse(trade.closed_at);
  if (!Number.isFinite(opened) || !Number.isFinite(closed)) return null;
  return Math.max(0, (closed - opened) / 1000);
}

/** Decimals present in a price, so XAUUSD does not render as `3320.50000`. */
function decimalsOf(value: number | null): number {
  if (value === null || !Number.isFinite(value)) return 0;
  const text = String(value);
  const dot = text.indexOf('.');
  if (dot < 0 || text.includes('e') || text.includes('E')) return 0;
  return Math.min(8, text.length - dot - 1);
}

/**
 * The precision a row is rendered at: the widest decimal count its own prices
 * carry, never fewer than two, so entry and close line up in the column.
 */
function inferDigits(trade: TradeOut): number {
  return Math.max(2, decimalsOf(trade.entry_price), decimalsOf(trade.close_price));
}

/** `2026-08-24` from an ISO string, for an `<input type="date">`. */
function toDateInput(value: string | undefined): string {
  if (!value) return '';
  const match = /^(\d{4}-\d{2}-\d{2})/.exec(value);
  return match ? match[1] : '';
}

/* ──────────────────────────── presentational bits ───────────────────────── */

/** BUY / SELL as a word plus an arrow, never colour alone. */
function SideBadge({ side, t }: { side: Side; t: Translate }): ReactElement {
  const long = side === 'buy';
  return (
    <span className={long ? 'badge badge--long' : 'badge badge--short'}>
      {t(long ? 'trade.buy_arrow' : 'trade.sell_arrow')}
    </span>
  );
}

/** One aggregate readout: an uppercase label over one big monospace number. */
function StatTile({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: ReactNode;
  tone?: string;
  hint?: string;
}): ReactElement {
  return (
    <div style={STAT_TILE} title={hint}>
      <span style={STAT_LABEL}>{label}</span>
      <span className={tone ? `mono mono--lg ${tone}` : 'mono mono--lg'}>{value}</span>
    </div>
  );
}

/** The link back to the signal that caused this trade, plus its permalink. */
function SignalCell({
  trade,
  href,
  t,
}: {
  trade: TradeOut;
  href: string | null;
  t: Translate;
}): ReactElement {
  const label = trade.signal_ref ?? '';
  if (!href && !label) {
    return (
      <span className="is-dim" title={t('trade.signal_missing')}>
        {t('trade.signal_missing')}
      </span>
    );
  }
  return (
    <span className="row row--tight" style={SIGNAL_CELL}>
      {/* `signal_ref` is an identifier, not prose — it belongs in the numeric
          face with every other ticket and reference on the page (§20). */}
      {href ? (
        <Link className="mono mono--sm" to={href} title={label || t('trade.signal')}>
          {label || t('trade.signal')}
        </Link>
      ) : (
        <span className="mono mono--sm" title={label}>
          {label}
        </span>
      )}
      {trade.split_count > 1 ? (
        <span
          className="badge badge--dim"
          title={t('trade.split_hint')}
          style={SPLIT_CHIP}
        >
          {t('trade.split', { index: trade.split_index + 1, count: trade.split_count })}
        </span>
      ) : null}
    </span>
  );
}

/* ────────────────────────────── the filter bar ──────────────────────────── */

interface FiltersProps {
  initial: HistoryQuery;
  symbols: string[];
  busy: boolean;
  onApply: (query: HistoryQuery) => void;
  t: Translate;
}

/**
 * Date range, symbol and row cap.
 *
 * Both dates are `<input type="date">`, whose value is already the ISO-8601
 * calendar date the API expects, so nothing is reformatted on the way out.
 */
function Filters({ initial, symbols, busy, onApply, t }: FiltersProps): ReactElement {
  const [from, setFrom] = useState(() => toDateInput(initial.date_from));
  const [to, setTo] = useState(() => toDateInput(initial.date_to));
  const [symbol, setSymbol] = useState(() => initial.symbol ?? '');
  const [limit, setLimit] = useState(() => String(initial.limit ?? DEFAULT_LIMIT));
  const [rangeError, setRangeError] = useState<string | null>(null);
  const [limitError, setLimitError] = useState<string | null>(null);

  const apply = useCallback(() => {
    if (from && to && from > to) {
      setRangeError(t('history.range_invalid'));
      return;
    }
    setRangeError(null);

    const parsedLimit = Number(limit.trim());
    if (limit.trim() !== '' && (!Number.isInteger(parsedLimit) || parsedLimit <= 0)) {
      setLimitError(t('err.positive_int'));
      return;
    }
    setLimitError(null);

    const query: HistoryQuery = {};
    if (from) query.date_from = from;
    if (to) query.date_to = to;
    if (symbol) query.symbol = symbol;
    query.limit = limit.trim() === '' ? DEFAULT_LIMIT : parsedLimit;
    onApply(query);
  }, [from, limit, onApply, symbol, t, to]);

  const reset = useCallback(() => {
    setFrom('');
    setTo('');
    setSymbol('');
    setLimit(String(DEFAULT_LIMIT));
    setRangeError(null);
    setLimitError(null);
    onApply({ limit: DEFAULT_LIMIT });
  }, [onApply]);

  return (
    <div className="row row--wrap row--top" style={FILTER_BAR} role="group" aria-label={t('history.filters')}>
      <div className={rangeError ? 'field field--invalid' : 'field'} style={FIELD_W}>
        <label className="field__label" htmlFor="sp-hist-from">
          {t('history.from')}
        </label>
        <input
          id="sp-hist-from"
          className="mono"
          type="date"
          value={from}
          disabled={busy}
          aria-invalid={rangeError ? 'true' : undefined}
          aria-describedby={rangeError ? 'sp-hist-range-err' : undefined}
          onChange={(event) => setFrom(event.target.value)}
        />
      </div>

      <div className={rangeError ? 'field field--invalid' : 'field'} style={FIELD_W}>
        <label className="field__label" htmlFor="sp-hist-to">
          {t('history.to')}
        </label>
        <input
          id="sp-hist-to"
          className="mono"
          type="date"
          value={to}
          disabled={busy}
          aria-invalid={rangeError ? 'true' : undefined}
          aria-describedby={rangeError ? 'sp-hist-range-err' : undefined}
          onChange={(event) => setTo(event.target.value)}
        />
        {rangeError ? (
          <p className="field__error" id="sp-hist-range-err">
            {rangeError}
          </p>
        ) : null}
      </div>

      <div className="field" style={FIELD_W}>
        <label className="field__label" htmlFor="sp-hist-symbol">
          {t('history.symbol')}
        </label>
        <select
          id="sp-hist-symbol"
          value={symbol}
          disabled={busy}
          onChange={(event) => setSymbol(event.target.value)}
        >
          <option value="">{t('history.all_symbols')}</option>
          {symbols.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </div>

      <div className={limitError ? 'field field--invalid' : 'field'} style={FIELD_W}>
        <label className="field__label" htmlFor="sp-hist-limit">
          {t('history.limit')}
        </label>
        <input
          id="sp-hist-limit"
          className="mono"
          type="text"
          inputMode="numeric"
          autoComplete="off"
          value={limit}
          disabled={busy}
          aria-invalid={limitError ? 'true' : undefined}
          aria-describedby={limitError ? 'sp-hist-limit-err' : undefined}
          onChange={(event) => setLimit(event.target.value)}
        />
        {limitError ? (
          <p className="field__error" id="sp-hist-limit-err">
            {limitError}
          </p>
        ) : null}
      </div>

      <div className="row row--tight">
        <button type="button" className="btn btn--primary btn--sm" disabled={busy} onClick={apply}>
          {t('history.apply')}
        </button>
        <button type="button" className="btn btn--sm" disabled={busy} onClick={reset}>
          {t('history.reset')}
        </button>
      </div>
    </div>
  );
}

/* ────────────────────────────── the aggregates ──────────────────────────── */

function Aggregates({
  aggregates,
  currency,
  locale,
  t,
}: {
  aggregates: AggregatesOut | null;
  currency: string | null;
  locale: Locale;
  t: Translate;
}): ReactElement {
  if (!aggregates || aggregates.count === 0) {
    return (
      <div style={STAT_GRID}>
        <p className="is-dim">{t('history.no_aggregates')}</p>
      </div>
    );
  }

  const profitTone =
    aggregates.total_profit > 0 ? 'is-long' : aggregates.total_profit < 0 ? 'is-short' : 'is-dim';

  return (
    <div style={STAT_GRID}>
      <StatTile
        label={t('history.win_rate')}
        value={formatRatioPercent(aggregates.win_rate, 1, locale)}
      />
      <StatTile
        label={t('history.avg_r')}
        hint={t('history.avg_r_hint')}
        value={formatSignedNumber(
          aggregates.avg_r,
          { minimumFractionDigits: 2, maximumFractionDigits: 2 },
          locale,
        )}
        tone={aggregates.avg_r > 0 ? 'is-long' : aggregates.avg_r < 0 ? 'is-short' : 'is-dim'}
      />
      <StatTile
        label={t('history.total_pl')}
        value={formatSignedCurrency(aggregates.total_profit, currency, locale)}
        tone={profitTone}
      />
      <StatTile
        label={t('history.trades_count')}
        value={formatInteger(aggregates.count, locale)}
      />
    </div>
  );
}

/* ─────────────────────────── the per-symbol table ───────────────────────── */

function BySymbol({
  aggregates,
  currency,
  locale,
  t,
}: {
  aggregates: AggregatesOut;
  currency: string | null;
  locale: Locale;
  t: Translate;
}): ReactElement | null {
  if (aggregates.by_symbol.length === 0) return null;
  return (
    <>
      <p style={SUB_TITLE}>{t('history.by_symbol')}</p>
      <div className="scroll-y scroll-y--x">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">{t('history.symbol')}</th>
              <th scope="col" className="num">
                {t('history.trades_count')}
              </th>
              <th scope="col" className="num">
                {t('history.wins')}
              </th>
              <th scope="col" className="num">
                {t('history.losses')}
              </th>
              <th scope="col" className="num">
                {t('history.win_rate')}
              </th>
              <th scope="col" className="num">
                {t('history.avg_r')}
              </th>
              <th scope="col" className="num num--wide">
                {t('history.total_pl')}
              </th>
            </tr>
          </thead>
          <tbody>
            {aggregates.by_symbol.map((row) => (
              <tr key={row.symbol}>
                <td>{row.symbol}</td>
                <td className="num mono">{formatInteger(row.count, locale)}</td>
                <td className="num mono">{formatInteger(row.wins, locale)}</td>
                <td className="num mono">{formatInteger(row.losses, locale)}</td>
                <td className="num mono">{formatRatioPercent(row.win_rate, 1, locale)}</td>
                <td className="num mono">
                  {formatSignedNumber(
                    row.avg_r,
                    { minimumFractionDigits: 2, maximumFractionDigits: 2 },
                    locale,
                  )}
                </td>
                <td className={profitClass(row.total_profit)}>
                  {formatSignedCurrency(row.total_profit, currency, locale)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

/* ──────────────────────────────── the table ─────────────────────────────── */

export function HistoryTable(props: HistoryTableProps): ReactElement {
  const {
    trades,
    aggregates = null,
    currency = null,
    loading = false,
    error = null,
    initialQuery,
    symbols,
    onApply,
    signalHref,
    digitsFor,
    showHeader = true,
  } = props;

  const t = useT();
  const locale = useCurrentLocale();
  const [query, setQuery] = useState<HistoryQuery>(() => initialQuery ?? { limit: DEFAULT_LIMIT });

  const symbolOptions = useMemo(() => {
    if (symbols) return symbols;
    const seen = new Set<string>();
    for (const trade of trades) seen.add(trade.symbol);
    return Array.from(seen).sort((a, b) => a.localeCompare(b));
  }, [symbols, trades]);

  const handleApply = useCallback(
    (next: HistoryQuery) => {
      setQuery(next);
      onApply?.(next);
    },
    [onApply],
  );

  const hrefFor = useCallback(
    (trade: TradeOut): string | null => {
      if (signalHref) return signalHref(trade);
      if (trade.message_row_id === null) return null;
      return `/?signal=${trade.message_row_id}`;
    },
    [signalHref],
  );

  const body = (
    <>
      <Aggregates aggregates={aggregates} currency={currency} locale={locale} t={t} />
      {aggregates ? (
        <BySymbol aggregates={aggregates} currency={currency} locale={locale} t={t} />
      ) : null}

      <Filters
        initial={query}
        symbols={symbolOptions}
        busy={loading}
        onApply={handleApply}
        t={t}
      />

      {error ? (
        <p className="field__error" role="alert">
          {error}
        </p>
      ) : null}

      <div className="scroll-y scroll-y--x">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">{t('trade.closed_at')}</th>
              <th scope="col" className="num">
                {t('trade.ticket')}
              </th>
              <th scope="col">{t('trade.symbol')}</th>
              <th scope="col">{t('trade.side')}</th>
              <th scope="col" className="num">
                {t('trade.volume')}
              </th>
              <th scope="col" className="num num--wide">
                {t('trade.entry')}
              </th>
              <th scope="col" className="num num--wide">
                {t('trade.close_price')}
              </th>
              <th scope="col" className="num num--wide">
                {t('trade.profit')}
              </th>
              <th scope="col" className="num">
                {t('trade.profit_points')}
              </th>
              <th scope="col">{t('trade.close_cause')}</th>
              <th scope="col" className="num">
                {t('history.duration')}
              </th>
              <th scope="col">{t('trade.signal')}</th>
            </tr>
          </thead>
          <tbody>
            {loading && trades.length === 0 ? (
              <tr>
                <td colSpan={COLUMN_COUNT}>
                  <span className="skeleton skeleton--text" aria-hidden="true" />
                  <span className="is-dim">{t('history.loading')}</span>
                </td>
              </tr>
            ) : null}

            {trades.map((trade) => {
              const cause: CloseCause = trade.close_cause ?? 'unknown';
              const held = heldSeconds(trade);
              const digits = digitsFor ? digitsFor(trade) : inferDigits(trade);
              return (
                <tr
                  key={trade.id}
                  className={trade.external ? 'is-readonly' : undefined}
                  title={trade.external ? t('trade.external_hint') : undefined}
                >
                  <td className="mono mono--sm">{formatDateTime(trade.closed_at, locale)}</td>
                  <td className="num mono">{trade.ticket}</td>
                  <td>{trade.symbol}</td>
                  <td>
                    <SideBadge side={trade.side} t={t} />
                  </td>
                  <td className="num mono">{formatVolume(trade.volume, locale)}</td>
                  <td className="num mono">{formatPrice(trade.entry_price, digits, locale)}</td>
                  <td className="num mono">
                    {trade.close_price === null
                      ? EMPTY_VALUE
                      : formatPrice(trade.close_price, digits, locale)}
                  </td>
                  <td className={profitClass(trade.profit)}>
                    {trade.profit === null
                      ? EMPTY_VALUE
                      : formatSignedCurrency(trade.profit, currency, locale)}
                  </td>
                  <td className={profitClass(trade.profit_points)}>
                    {trade.profit_points === null
                      ? EMPTY_VALUE
                      : formatSignedNumber(
                          trade.profit_points,
                          { maximumFractionDigits: 0 },
                          locale,
                        )}
                  </td>
                  <td>
                    <span className={CAUSE_TONE[cause]}>{t(CAUSE_LABELS[cause])}</span>
                  </td>
                  <td className="num mono mono--sm">
                    {held === null ? EMPTY_VALUE : formatDuration(held, locale)}
                  </td>
                  <td>
                    <SignalCell trade={trade} href={hrefFor(trade)} t={t} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        {!loading && trades.length === 0 ? (
          <div className="empty">
            <p className="empty__title">{t('history.empty')}</p>
          </div>
        ) : null}
      </div>
    </>
  );

  if (!showHeader) return body;

  return (
    <section className="card card--flush">
      <header className="card__header">
        <h2 className="card__title">{t('history.title')}</h2>
        <div className="card__actions">
          <span className="badge badge--dim">
            <span className="badge__num">{formatNumber(trades.length, undefined, locale)}</span>
            {t('history.trades_count')}
          </span>
        </div>
      </header>
      {body}
    </section>
  );
}

export default HistoryTable;
