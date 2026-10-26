/**
 * LV Copytrader — History page
 * ---------------------------------------------------------------------------
 * Closed trades with their realized P/L and the signal that caused each, under
 * the per-source aggregates — win rate, average R, total P/L, count (BUILD_PROMPT
 * §9 "History", CONTRACTS §18 `/api/history/trades` and `/api/history/aggregates`).
 * `HistoryTable` renders the aggregates, the filter bar and the rows; this page
 * is the data plane behind it.
 *
 * Four decisions this file exists to make:
 *
 *  1. THE RANGE STARTS AT THE LAST 30 DAYS. An unbounded first read of a busy
 *     account is a slow page and a useless one — nobody opens this to look at
 *     last spring. The two date inputs are seeded from `defaultQuery()` in the
 *     browser's own calendar, which is exactly the value an `<input type="date">`
 *     round-trips, so nothing is reformatted on the way to the API.
 *
 *  2. AGGREGATES ARE PER SOURCE, NOT PER FILTER. `/api/history/aggregates` takes
 *     `source_id` only (CONTRACTS §18): narrowing the table to one symbol or one
 *     week does not narrow the win rate above it. The header names the source the
 *     numbers belong to so that is visible rather than assumed.
 *
 *  3. THE SYMBOL LIST ONLY GROWS. Filtering to XAUUSD returns XAUUSD rows alone,
 *     and a symbol picker rebuilt from those rows would collapse to a single
 *     option and trap the operator there. Every symbol ever seen is kept, so the
 *     way back out stays in the list.
 *
 *  4. THE STREAM IS A TAIL, REST IS THE TRUTH. A `position.closed` or
 *     `order.cancelled` event means a new row exists, so the page re-reads rather
 *     than appending a row it invented; every (re)connect re-reads for the same
 *     reason (stream.ts header). There is no poll here: history only changes when
 *     something closes, and something closing is an event.
 *
 * Every number renders through `.mono` with tabular figures, inside the table and
 * in this page's own header.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, ReactElement } from 'react';

import {
  getHistoryAggregates,
  getHistoryTrades,
  isApiError,
  tradingState,
} from '@/api/client';
import type {
  AggregatesOut,
  HistoryQuery,
  SourceOut,
  StreamEvent,
  TradeOut,
  TradingStateOut,
} from '@/api/client';
import { useStream } from '@/api/stream';
import type { StreamEventHandler, StreamStatus, StreamStatusHandler } from '@/api/stream';
import { HistoryTable } from '@/components/HistoryTable';
import { formatTime, useCurrentLocale, useT } from '@/i18n';
import type { Translate } from '@/i18n';

/* ─────────────────────────────── tunables ───────────────────────────────── */

/** How far back the two date inputs start. */
const DEFAULT_RANGE_DAYS = 30;

/** Row cap of the first read; `HistoryTable`'s own filter bar uses the same number. */
const DEFAULT_LIMIT = 200;

/**
 * How long a stream event waits for its neighbours before the refetch fires.
 * A kill switch closing four positions emits four events at once; this makes
 * them one read.
 */
const COALESCE_MS = 700;

/** The events that add or change a history row. */
const REFRESH_ON: ReadonlySet<StreamEvent['type']> = new Set<StreamEvent['type']>([
  'position.closed',
  'order.cancelled',
]);

/* ────────────────────────────── local styles ────────────────────────────── */
/* Inline so the page owns its own layout without a stylesheet, and so no colour
   literal ever leaves tokens.css. */

const PAGE: CSSProperties = { gap: 'var(--sp-5)' };
const HEAD: CSSProperties = { minHeight: 'var(--control-h)' };
const TITLE: CSSProperties = { fontSize: 'var(--fs-4)' };
const META: CSSProperties = { color: 'var(--text-dim)', fontSize: 'var(--fs-1)' };
const BANNER: CSSProperties = { padding: 'var(--sp-2) var(--sp-3)' };
/* `.field__error` is itself a flex box; without this it cannot shrink and a long
   broker message pushes the Retry button off the card instead of wrapping. */
const BANNER_MSG: CSSProperties = { flex: '1 1 auto', minWidth: 0 };

/* ─────────────────────────────── local types ────────────────────────────── */

/**
 * How a parent hands this page a socket it already owns.
 *
 * Without it the page opens its own (`StreamBridge` below), which is correct on
 * its own but means two sockets per tab once the app shell has one of its own.
 */
export type StreamSubscribe = (
  onEvent: StreamEventHandler,
  onStatus?: StreamStatusHandler,
) => () => void;

export interface HistoryPageProps {
  /** Reuse the shell's socket instead of opening one for this page. */
  subscribe?: StreamSubscribe;
}

/** `refetch(includeState)` — annotated so the recursive follow-up read typechecks. */
type Refetch = (includeState: boolean) => Promise<void>;

/* ───────────────────────────── small helpers ────────────────────────────── */

/** `2026-08-24` in the browser's own calendar — the value an `<input type="date">` carries. */
function isoDate(date: Date): string {
  const year = String(date.getFullYear()).padStart(4, '0');
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/** The filters the page opens with: the last 30 days, every symbol. */
function defaultQuery(): HistoryQuery {
  const to = new Date();
  const from = new Date(to.getTime());
  from.setDate(from.getDate() - DEFAULT_RANGE_DAYS);
  return { date_from: isoDate(from), date_to: isoDate(to), limit: DEFAULT_LIMIT };
}

/** Turn any thrown value into the sentence the banner will show. */
function errorText(cause: unknown, t: Translate): string {
  if (isApiError(cause)) return cause.message;
  if (cause instanceof Error && cause.message) return cause.message;
  return t('err.generic');
}

/**
 * Mounts one WebSocket for as long as it is rendered.
 *
 * It is a child rather than a `useStream` call in the page body so the page can
 * decline to open a socket at all when the shell handed it one — hooks cannot be
 * called conditionally, components can be rendered conditionally.
 */
function StreamBridge({
  onEvent,
  onStatus,
}: {
  onEvent: StreamEventHandler;
  onStatus: StreamStatusHandler;
}): null {
  useStream(onEvent, onStatus);
  return null;
}

/** Live / Connecting… / Stream offline — a word beside the colour, never colour alone. */
function StreamDot({ status, t }: { status: StreamStatus; t: Translate }): ReactElement {
  const tone =
    status === 'open' ? 'dot dot--ok' : status === 'connecting' ? 'dot dot--idle' : 'dot dot--bad';
  const word =
    status === 'open'
      ? t('app.stream_connected')
      : status === 'connecting'
        ? t('app.stream_connecting')
        : t('app.stream_disconnected');
  return (
    <span className={tone} title={t('status.stream')}>
      {word}
    </span>
  );
}

/* ──────────────────────────────── the page ──────────────────────────────── */

export function History(props: HistoryPageProps): ReactElement {
  const { subscribe } = props;

  const t = useT();
  const locale = useCurrentLocale();

  const [query, setQuery] = useState<HistoryQuery>(defaultQuery);
  const [trades, setTrades] = useState<TradeOut[]>([]);
  const [aggregates, setAggregates] = useState<AggregatesOut | null>(null);
  const [symbols, setSymbols] = useState<string[]>([]);
  const [source, setSource] = useState<SourceOut | null>(null);
  const [currency, setCurrency] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /** The raw thrown value, not a rendered string: it must re-render on a locale switch. */
  const [failure, setFailure] = useState<unknown>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('connecting');

  /** The filters the next read will use; a ref so a queued read picks up the newest. */
  const queryRef = useRef<HistoryQuery>(query);
  /** Every symbol ever returned, so filtering never shrinks the picker (rule 3). */
  const symbolsRef = useRef<Set<string>>(new Set<string>());
  /** The active source, once `GET /api/trading/state` has named it. */
  const sourceIdRef = useRef<number | null>(null);

  /** False from unmount onward: no state is written after the page is gone. */
  const aliveRef = useRef(true);
  /** A read is in flight. */
  const busyRef = useRef(false);
  /** A read was asked for while one was in flight; serve it once, afterwards. */
  const againRef = useRef(false);
  /** The next read should also refresh `GET /api/trading/state`. */
  const stateWantedRef = useRef(true);
  const timerRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const refetch: Refetch = useCallback(async (includeState: boolean): Promise<void> => {
    if (includeState) stateWantedRef.current = true;
    if (busyRef.current) {
      againRef.current = true;
      return;
    }
    busyRef.current = true;

    const controller = new AbortController();
    abortRef.current = controller;
    const options = { signal: controller.signal };
    const wantState = stateWantedRef.current;
    stateWantedRef.current = false;
    const activeQuery = queryRef.current;
    const activeSource = sourceIdRef.current;

    try {
      // On the very first read `source_id` is not known yet and is omitted, which
      // is the same answer: the backend aggregates the active source by default.
      const [tradesOut, aggregatesOut, nextState] = await Promise.all([
        getHistoryTrades(activeQuery, options),
        getHistoryAggregates(activeSource === null ? {} : { source_id: activeSource }, options),
        wantState
          ? tradingState(options).catch((cause: unknown): TradingStateOut | null => {
              if (isApiError(cause) && cause.isAborted) throw cause;
              // The source name and the account currency are chrome. A failure
              // here must not blank a table that did answer — retry it next read.
              stateWantedRef.current = true;
              return null;
            })
          : Promise.resolve<TradingStateOut | null>(null),
      ]);

      if (!aliveRef.current) return;

      setTrades(tradesOut.trades);
      setAggregates(aggregatesOut);

      let grew = false;
      for (const trade of tradesOut.trades) {
        if (symbolsRef.current.has(trade.symbol)) continue;
        symbolsRef.current.add(trade.symbol);
        grew = true;
      }
      if (grew) {
        setSymbols(Array.from(symbolsRef.current).sort((a, b) => a.localeCompare(b)));
      }

      if (nextState) {
        sourceIdRef.current = nextState.source ? nextState.source.id : null;
        setSource(nextState.source);
        if (nextState.currency) setCurrency(nextState.currency);
      }

      setFailure(null);
      setUpdatedAt(Date.now());
    } catch (cause) {
      if (!aliveRef.current) return;
      // An abort is this page cancelling its own request; it is not a failure.
      if (isApiError(cause) && cause.isAborted) return;
      if (wantState) stateWantedRef.current = true;
      setFailure(cause);
    } finally {
      busyRef.current = false;
      if (aliveRef.current) {
        setLoading(false);
        if (againRef.current) {
          againRef.current = false;
          void refetch(false);
        }
      }
    }
  }, []);

  /** Coalesce every trigger onto one timer, so a burst of events is one read. */
  const schedule = useCallback(
    (delayMs: number, includeState: boolean): void => {
      if (includeState) stateWantedRef.current = true;
      if (!aliveRef.current) return;
      if (timerRef.current !== null) return;
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        void refetch(false);
      }, delayMs);
    },
    [refetch],
  );

  const refreshNow = useCallback((): void => {
    void refetch(true);
  }, [refetch]);

  useEffect(() => {
    aliveRef.current = true;
    void refetch(true);

    return () => {
      aliveRef.current = false;
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [refetch]);

  /* ── the filter bar ───────────────────────────────────────────────────── */

  /**
   * `HistoryTable` owns the date, symbol and row-cap inputs and validates them
   * in place; this is where the values it settled on become a request. The
   * aggregates are not refetched — they do not read the filters (rule 2).
   */
  const applyQuery = useCallback(
    (next: HistoryQuery): void => {
      queryRef.current = next;
      setQuery(next);
      void refetch(false);
    },
    [refetch],
  );

  /* ── the stream ───────────────────────────────────────────────────────── */

  const handleEvent = useCallback(
    (event: StreamEvent): void => {
      if (REFRESH_ON.has(event.type)) {
        schedule(COALESCE_MS, false);
        return;
      }
      if (event.type === 'account.tick') {
        // Free: the currency every money column needs, without a request of its own.
        const code = event.data.account.currency;
        if (code) setCurrency(code);
      }
    },
    [schedule],
  );

  const handleStatus = useCallback(
    (status: StreamStatus): void => {
      setStreamStatus(status);
      // Every (re)connect refetches: the socket is a tail, REST is the truth.
      if (status === 'open') schedule(0, true);
    },
    [schedule],
  );

  useEffect(() => {
    if (!subscribe) return undefined;
    return subscribe(handleEvent, handleStatus);
  }, [subscribe, handleEvent, handleStatus]);

  /* ── render ───────────────────────────────────────────────────────────── */

  const errorMessage = failure === null ? null : errorText(failure, t);

  return (
    <div className="stack page" style={PAGE}>
      {subscribe ? null : <StreamBridge onEvent={handleEvent} onStatus={handleStatus} />}

      <header className="row row--wrap" style={HEAD}>
        <h1 style={TITLE}>{t('nav.history')}</h1>
        <span style={META}>{t('history.per_source')}</span>
        <span className="badge badge--dim" title={t('history.source_column')}>
          {source ? source.title : t('app.none')}
        </span>
        <span className="spacer" />
        <StreamDot status={streamStatus} t={t} />
        {updatedAt === null ? (
          <span style={META}>{t('app.loading')}</span>
        ) : (
          <>
            <span style={META}>{t('app.updated')}</span>
            <span className="mono mono--sm" style={META}>
              {formatTime(updatedAt, locale)}
            </span>
          </>
        )}
        <button type="button" className="btn btn--sm" onClick={refreshNow} disabled={loading}>
          {t('app.refresh')}
        </button>
      </header>

      {errorMessage ? (
        <section role="alert">
          <div className="row row--wrap" style={BANNER}>
            <span className="field__error" style={BANNER_MSG}>
              {errorMessage}
            </span>
            <button type="button" className="btn btn--sm" onClick={refreshNow} disabled={loading}>
              {t('app.retry')}
            </button>
          </div>
        </section>
      ) : null}

      <HistoryTable
        trades={trades}
        aggregates={aggregates}
        currency={currency}
        loading={loading}
        initialQuery={query}
        symbols={symbols}
        onApply={applyQuery}
      />
    </div>
  );
}

export default History;
