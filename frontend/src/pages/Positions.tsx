/**
 * LV Copytrader — Positions page
 * ---------------------------------------------------------------------------
 * Two stacked sections: open positions, then pending orders (BUILD_PROMPT §9
 * "Positions", CONTRACTS §18 `/api/trading/positions` and `/api/trading/orders`).
 * The tables themselves are `PositionsTable` and `OrdersTable`; this page is the
 * data plane behind them — fetching, freshness, and the manual write calls.
 *
 * Four decisions this file exists to make:
 *
 *  1. NO OPTIMISTIC UI. These are real orders at a real broker. A manual close,
 *     modify or cancel shows a pending state (the row's own button turns into
 *     "Closing…" / "Applying…" / "Cancelling…" and every control on that row is
 *     disabled) and then waits for the server. Nothing is drawn as done until
 *     the broker said so, and the row is only redrawn from a fresh REST read.
 *     While a write is in flight the 5-second poll is suspended, so a snapshot
 *     taken mid-flight can never paint a half-applied state over the answer.
 *
 *  2. THE STREAM IS A TAIL, REST IS THE TRUTH. `position.*` and `order.*` events
 *     trigger a refetch rather than patching rows in place, and every (re)connect
 *     refetches as well, because a reconnect means an unknown number of events
 *     were missed (stream.ts header, BUILD §9).
 *
 *  3. A 5-SECOND POLL IS THE FLOOR. Events make the page feel instant; the poll
 *     is what makes it correct when the socket is down, when the backend dropped
 *     an event, or when the broker changed something nobody told us about — a
 *     stop hit, a partial fill, a position closed from the terminal itself.
 *
 *  4. ONE REQUEST AT A TIME. Poll, event and manual-write refreshes all funnel
 *     through `refetch`, which coalesces: a call arriving while a fetch is in
 *     flight sets a flag and is served by one follow-up read instead of piling
 *     up N sockets against a single-threaded MT5 IPC channel.
 *
 * Rows carrying a foreign magic number render read-only; that rule lives in the
 * tables (BUILD §8.2) and this page never sends a write for one.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, ReactElement } from 'react';

import {
  getOrders,
  getPositions,
  isApiError,
  manualCancel,
  manualClose,
  manualModifyOrder,
  manualModifyPosition,
  tradingState,
} from '@/api/client';
import type {
  ManualCancelInput,
  ManualCloseInput,
  ManualModifyOrderInput,
  ManualModifyPositionInput,
  OrderOut,
  PositionOut,
  StreamEvent,
  TradingStateOut,
  WriteResultOut,
} from '@/api/client';
import { useStream } from '@/api/stream';
import type { StreamEventHandler, StreamStatus, StreamStatusHandler } from '@/api/stream';
import { OrdersTable } from '@/components/OrdersTable';
import { PositionsTable } from '@/components/PositionsTable';
import { formatTime, useCurrentLocale, useT } from '@/i18n';
import type { Translate } from '@/i18n';

/* ─────────────────────────────── tunables ───────────────────────────────── */

/** The floor: positions and orders are re-read this often no matter what. */
const POLL_INTERVAL_MS = 5_000;

/**
 * How long a stream event waits for its neighbours before the refetch fires.
 * One agent run opening a three-leg split emits three `position.opened` events
 * inside a few milliseconds; this turns them into one read.
 */
const COALESCE_MS = 300;

/** The events that change what these two tables show. */
const REFRESH_ON: ReadonlySet<StreamEvent['type']> = new Set<StreamEvent['type']>([
  'order.placed',
  'order.cancelled',
  'position.opened',
  'position.modified',
  'position.closed',
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
 * A shell that already streams passes `subscribe` and no second socket is made.
 */
export type StreamSubscribe = (
  onEvent: StreamEventHandler,
  onStatus?: StreamStatusHandler,
) => () => void;

export interface PositionsPageProps {
  /** Reuse the shell's socket instead of opening one for this page. */
  subscribe?: StreamSubscribe;
}

/** `refetch(includeState)` — annotated so the recursive follow-up read typechecks. */
type Refetch = (includeState: boolean) => Promise<void>;

/* ───────────────────────────── small helpers ────────────────────────────── */

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

export function Positions(props: PositionsPageProps): ReactElement {
  const { subscribe } = props;

  const t = useT();
  const locale = useCurrentLocale();

  const [positions, setPositions] = useState<PositionOut[]>([]);
  const [orders, setOrders] = useState<OrderOut[]>([]);
  const [state, setState] = useState<TradingStateOut | null>(null);
  const [currency, setCurrency] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /** The raw thrown value, not a rendered string: it must re-render on a locale switch. */
  const [failure, setFailure] = useState<unknown>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('connecting');

  /** False from unmount onward: no state is written after the page is gone. */
  const aliveRef = useRef(true);
  /** A read is in flight. */
  const busyRef = useRef(false);
  /** A read was asked for while one was in flight; serve it once, afterwards. */
  const againRef = useRef(false);
  /** The next read should also refresh `GET /api/trading/state`. */
  const stateWantedRef = useRef(true);
  /** Manual writes in flight; while any is, the poll holds off. */
  const pendingRef = useRef(0);
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

    try {
      const [positionsOut, ordersOut] = await Promise.all([
        getPositions(options),
        getOrders(options),
      ]);

      let nextState: TradingStateOut | null = null;
      if (wantState) {
        try {
          nextState = await tradingState(options);
        } catch (cause) {
          if (isApiError(cause) && cause.isAborted) throw cause;
          // The dots and the account currency are chrome. A failure here must
          // not blank the two tables that did answer — retry it on the next read.
          stateWantedRef.current = true;
        }
      }

      if (!aliveRef.current) return;
      setPositions(positionsOut.positions);
      setOrders(ordersOut.orders);
      if (nextState) {
        setState(nextState);
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

  /** The Refresh button in either table header: read everything, now. */
  const refreshNow = useCallback((): void => {
    void refetch(true);
  }, [refetch]);

  useEffect(() => {
    aliveRef.current = true;
    void refetch(true);

    const poll = window.setInterval(() => {
      // A write is in flight: the only answer that counts is the server's, and a
      // poll landing on top of it could paint a half-applied state (rule 1).
      if (pendingRef.current > 0) return;
      void refetch(false);
    }, POLL_INTERVAL_MS);

    return () => {
      aliveRef.current = false;
      window.clearInterval(poll);
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [refetch]);

  /* ── the stream ───────────────────────────────────────────────────────── */

  const handleEvent = useCallback(
    (event: StreamEvent): void => {
      if (REFRESH_ON.has(event.type)) {
        schedule(COALESCE_MS, false);
        return;
      }
      if (event.type === 'status.changed') {
        schedule(COALESCE_MS, true);
        return;
      }
      if (event.type === 'account.tick') {
        // Free: the currency the P/L column needs, without a request of its own.
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

  /* ── manual writes ────────────────────────────────────────────────────── */

  /**
   * Count the write, let it run to the server, then read the real state back.
   *
   * The result — success or refusal — is returned untouched to the table, which
   * prints the broker's own message under the row. Nothing here guesses at the
   * outcome and nothing here edits a row locally.
   */
  const runWrite = useCallback(
    (call: () => Promise<WriteResultOut>): Promise<WriteResultOut> => {
      pendingRef.current += 1;
      return call().finally(() => {
        pendingRef.current = Math.max(0, pendingRef.current - 1);
        schedule(COALESCE_MS, false);
      });
    },
    [schedule],
  );

  const closePosition = useCallback(
    (input: ManualCloseInput): Promise<WriteResultOut> => runWrite(() => manualClose(input)),
    [runWrite],
  );

  const modifyPosition = useCallback(
    (input: ManualModifyPositionInput): Promise<WriteResultOut> =>
      runWrite(() => manualModifyPosition(input)),
    [runWrite],
  );

  const cancelOrder = useCallback(
    (input: ManualCancelInput): Promise<WriteResultOut> => runWrite(() => manualCancel(input)),
    [runWrite],
  );

  const modifyOrder = useCallback(
    (input: ManualModifyOrderInput): Promise<WriteResultOut> =>
      runWrite(() => manualModifyOrder(input)),
    [runWrite],
  );

  /* ── render ───────────────────────────────────────────────────────────── */

  // Unknown is not the same as down: before the first answer the tables must not
  // claim MT5 is offline.
  const mt5Online = state ? state.mt5.ok : true;
  const errorMessage = failure === null ? null : errorText(failure, t);

  return (
    <div className="stack page" style={PAGE}>
      {subscribe ? null : <StreamBridge onEvent={handleEvent} onStatus={handleStatus} />}

      <header className="row row--wrap" style={HEAD}>
        <h1 style={TITLE}>{t('nav.positions')}</h1>
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

      <PositionsTable
        positions={positions}
        currency={currency}
        loading={loading}
        mt5Online={mt5Online}
        onRefresh={refreshNow}
        onClose={closePosition}
        onModify={modifyPosition}
      />

      <OrdersTable
        orders={orders}
        loading={loading}
        mt5Online={mt5Online}
        onRefresh={refreshNow}
        onCancel={cancelOrder}
        onModify={modifyOrder}
      />
    </div>
  );
}

export default Positions;
