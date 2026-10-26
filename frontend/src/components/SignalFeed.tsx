/**
 * LV Copytrader — SignalFeed.
 *
 * The incoming-message column (BUILD_PROMPT §9): everything the channel posted,
 * newest first, skipped messages included.
 *
 * It seeds from `getSignals()` and prepends on `signal.received`. The two are
 * MERGED rather than swapped, so a message that arrives between the request
 * going out and the response coming back is never lost, and a reconnect refetch
 * — which the stream contract requires on every (re)connect — is idempotent
 * rather than destructive.
 *
 * Virtualisation is not required at this volume, but the RENDERED WINDOW IS
 * CAPPED: the operator scrolls a fixed number of cards while the count in the
 * footer tells the truth about how many exist. A cockpit that quietly grows a
 * ten-thousand-node column is a cockpit that stops repainting the price.
 *
 * Ordering is by database id, descending. That is ingest order, which is what
 * "newest first" means in a feed: a catch-up batch replayed after downtime
 * belongs at the top where the operator will see it, even though it was posted
 * an hour ago, and its own CATCH-UP badge says so.
 *
 * Selecting a card shows its runs underneath it, fetched once per selection.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactElement } from 'react';

import {
  getSignal,
  getSignals,
  isApiError,
  type RunOut,
  type SignalOut,
  type StreamEvent,
} from '../api/client';
import type { StreamStatus } from '../api/stream';
import { formatInteger, useCurrentLocale, useT } from '../i18n';
import { RunSummary, subscribeToDashboardStream, type StreamSubscribe } from './RunStream';
import SignalCard, { useTicker } from './SignalCard';

/* ────────────────────────────── public types ─────────────────────────────── */

export interface SignalFeedProps {
  /**
   * Attach to the page's socket — build one with `createEventFanout()` and pass
   * a STABLE reference; an inline arrow re-subscribes on every render. Omitted,
   * the feed shares the process-wide connection with every other component that
   * does the same, so there is still exactly one WebSocket (CONTRACTS §19).
   */
  subscribe?: StreamSubscribe;
  /** Controlled rows. When given the feed renders exactly these and never fetches. */
  signals?: SignalOut[];
  /** Controlled selection, by `SignalOut.id`. */
  selectedId?: number | null;
  /** Fired on every selection change, `null` when the selection is cleared. */
  onSelect?: (signal: SignalOut | null) => void;
  /** How many rows to request from the backend. */
  limit?: number;
  /** Hard cap on how many cards are in the DOM at once. */
  maxRendered?: number;
  /**
   * True when no signal source has been chosen yet: nothing is being ingested
   * and an empty feed would otherwise look like a fault (BUILD §3.5).
   */
  noSource?: boolean;
  /** Renders without the surrounding `.card` chrome, for embedding. */
  bare?: boolean;
  className?: string;
}

/** How many rows the feed asks for. */
const DEFAULT_LIMIT = 100;
/** How many cards may exist in the DOM at once. */
const DEFAULT_MAX_RENDERED = 60;
/** How many stream-delivered rows are kept in memory before the oldest are dropped. */
const STREAM_BUFFER = 200;
/** How often the age readouts refresh. */
const AGE_TICK_MS = 30_000;

/* ─────────────────────────────── the column ──────────────────────────────── */

export function SignalFeed(props: SignalFeedProps): ReactElement {
  const {
    subscribe,
    signals: controlledSignals,
    selectedId: controlledSelectedId,
    onSelect,
    limit = DEFAULT_LIMIT,
    maxRendered = DEFAULT_MAX_RENDERED,
    noSource = false,
    bare = false,
    className,
  } = props;

  const t = useT();
  const locale = useCurrentLocale();
  const now = useTicker(AGE_TICK_MS);

  const controlled = controlledSignals !== undefined;

  const [seeded, setSeeded] = useState<SignalOut[]>([]);
  const [streamed, setStreamed] = useState<SignalOut[]>([]);
  const [loading, setLoading] = useState(!controlled);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const [selfSelectedId, setSelfSelectedId] = useState<number | null>(null);
  const selectedId = controlledSelectedId ?? selfSelectedId;

  const scrollRef = useRef<HTMLDivElement | null>(null);
  /** run_id -> message row id, learnt from `run.started`, used to age the status on `run.finished`. */
  const runOwners = useRef<Map<string, number>>(new Map());
  /**
   * Ids that came in on the socket while this feed was mounted. A card in this
   * set plays its arrival; every other card renders at rest. Insertion-ordered,
   * so the cap below drops the oldest first.
   */
  const arrivedRef = useRef<Set<number>>(new Set<number>());

  /* ---- seed ---- */

  useEffect(() => {
    if (controlled) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);

    getSignals({ limit }, { signal: controller.signal })
      .then((out) => {
        if (cancelled) return;
        setSeeded(out.signals);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        if (isApiError(cause) && cause.isAborted) return;
        setError(isApiError(cause) ? cause.message : String(cause));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [controlled, limit, reloadToken]);

  /* ---- live ---- */

  const patchStatus = useCallback(
    (messageRowId: number, patch: (signal: SignalOut) => SignalOut) => {
      const apply = (rows: SignalOut[]): SignalOut[] => {
        const index = rows.findIndex((row) => row.id === messageRowId);
        if (index < 0) return rows;
        const next = rows.slice();
        next[index] = patch(next[index] as SignalOut);
        return next;
      };
      setStreamed(apply);
      setSeeded(apply);
    },
    [],
  );

  const onEvent = useCallback(
    (event: StreamEvent) => {
      if (event.type === 'signal.received') {
        const incoming = event.data.signal;
        // Remember that THIS id arrived on the socket rather than in a fetch:
        // only an arrival plays the card's staggered enter. The set is capped
        // at the buffer it describes so a long session cannot grow it forever.
        const arrived = arrivedRef.current;
        arrived.add(incoming.id);
        while (arrived.size > STREAM_BUFFER) {
          const oldest: number | undefined = arrived.values().next().value;
          if (oldest === undefined) break;
          arrived.delete(oldest);
        }
        setStreamed((prev) => {
          const without = prev.filter((row) => row.id !== incoming.id);
          return [incoming, ...without].slice(0, STREAM_BUFFER);
        });
        return;
      }

      // The feed shows a message's lifecycle without refetching it: a run that
      // starts marks its message `processing`, and the run that ends marks it
      // `processed` — or `error`, which is a state the operator must see.
      if (event.type === 'run.started') {
        const { run_id: runId, message_row_id: messageRowId } = event.data;
        if (messageRowId === null) return;
        runOwners.current.set(runId, messageRowId);
        patchStatus(messageRowId, (signal) => ({
          ...signal,
          status: signal.status === 'skipped' ? signal.status : 'processing',
          runs_count: signal.runs_count + 1,
        }));
        return;
      }

      if (event.type === 'run.finished') {
        const messageRowId = runOwners.current.get(event.data.run_id);
        if (messageRowId === undefined) return;
        runOwners.current.delete(event.data.run_id);
        patchStatus(messageRowId, (signal) => ({
          ...signal,
          status:
            signal.status === 'skipped'
              ? signal.status
              : event.data.status === 'error'
                ? 'error'
                : 'processed',
        }));
      }
    },
    [patchStatus],
  );

  const onStatus = useCallback((status: StreamStatus) => {
    // A reconnect means an unknown gap: refetch rather than trust the buffer.
    if (status === 'open') setReloadToken((v) => v + 1);
  }, []);

  const eventRef = useRef(onEvent);
  const statusRef = useRef(onStatus);
  useEffect(() => {
    eventRef.current = onEvent;
    statusRef.current = onStatus;
  });

  useEffect(() => {
    if (controlled) return;
    const attach = subscribe ?? subscribeToDashboardStream;
    return attach(
      (event) => { eventRef.current(event); },
      (status) => { statusRef.current(status); },
    );
  }, [subscribe, controlled]);

  /* ---- merge ---- */

  const rows = useMemo(() => {
    if (controlledSignals) return controlledSignals.slice().sort((a, b) => b.id - a.id);
    const byId = new Map<number, SignalOut>();
    for (const row of seeded) byId.set(row.id, row);
    // The stream is fresher than a seed fetched before it arrived.
    for (const row of streamed) byId.set(row.id, row);
    return [...byId.values()].sort((a, b) => b.id - a.id);
  }, [controlledSignals, seeded, streamed]);

  const visible = useMemo(() => rows.slice(0, maxRendered), [rows, maxRendered]);

  const selected = useMemo(
    () => rows.find((row) => row.id === selectedId) ?? null,
    [rows, selectedId],
  );

  /* ---- selection ---- */

  const select = useCallback(
    (signal: SignalOut) => {
      const next = selectedId === signal.id ? null : signal;
      if (controlledSelectedId === undefined) setSelfSelectedId(next?.id ?? null);
      onSelect?.(next);
    },
    [selectedId, controlledSelectedId, onSelect],
  );

  const focusParent = useCallback(
    (messageId: number) => {
      const parent = rows.find((row) => row.message_id === messageId);
      if (parent) {
        if (controlledSelectedId === undefined) setSelfSelectedId(parent.id);
        onSelect?.(parent);
      }
      const container = scrollRef.current;
      const target = container?.querySelector(`[data-message-id="${String(messageId)}"]`);
      if (target instanceof HTMLElement) {
        target.scrollIntoView({ block: 'nearest' });
      }
    },
    [rows, controlledSelectedId, onSelect],
  );

  /* ---- the selected card's runs ---- */

  const [selectedRuns, setSelectedRuns] = useState<RunOut[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);

  useEffect(() => {
    if (selectedId === null) {
      setSelectedRuns([]);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setRunsLoading(true);
    setSelectedRuns([]);

    getSignal(selectedId, { signal: controller.signal })
      .then((detail) => {
        if (!cancelled) setSelectedRuns(detail.runs);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        if (isApiError(cause) && cause.isAborted) return;
        setSelectedRuns([]);
      })
      .finally(() => {
        if (!cancelled) setRunsLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [selectedId, reloadToken]);

  /* ---- render ---- */

  const showEmpty = !loading && error === null && rows.length === 0;

  const list = (
    <div className="sp-feed__scroll scroll-y scroll-y--grow" ref={scrollRef}>
      {loading && rows.length === 0 ? (
        <div className="sp-feed__loading">
          <span className="skeleton skeleton--block" />
          <span className="skeleton skeleton--block" />
          <span className="skeleton skeleton--block" />
        </div>
      ) : null}

      {error !== null ? (
        <div className="empty">
          <p className="empty__title">{t('app.request_failed')}</p>
          <p className="empty__text">{error}</p>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => { setReloadToken((v) => v + 1); }}
          >
            {t('app.retry')}
          </button>
        </div>
      ) : null}

      {showEmpty ? (
        <div className="empty">
          {noSource ? (
            <>
              <p className="empty__title">{t('feed.no_source')}</p>
              <p className="empty__text">{t('feed.select_source_hint')}</p>
            </>
          ) : (
            <p className="empty__text">{t('feed.empty')}</p>
          )}
        </div>
      ) : null}

      {visible.map((signal) => {
        const isSelected = signal.id === selectedId;
        return (
          <SignalCard
            key={signal.id}
            signal={signal}
            selected={isSelected}
            onSelect={select}
            onOpenParent={focusParent}
            now={now}
            entering={arrivedRef.current.has(signal.id)}
          >
            {isSelected ? (
              runsLoading ? (
                <span className="skeleton skeleton--text" />
              ) : selectedRuns.length > 0 ? (
                selectedRuns.map((run) => <RunSummary key={run.id} run={run} />)
              ) : (
                <p className="sp-feed__no-runs">{t('run.no_runs')}</p>
              )
            ) : null}
          </SignalCard>
        );
      })}
    </div>
  );

  const footer =
    rows.length > 0 ? (
      <div className="sp-feed__foot">
        <span className="sp-feed__foot-label">{t('app.count')}</span>
        <span className="mono sp-feed__foot-num" title={t('feed.title')}>
          {formatInteger(visible.length, locale)} / {formatInteger(rows.length, locale)}
        </span>
      </div>
    ) : null;

  if (bare) {
    return (
      <div className={['sp-feed', 'sp-feed--bare', className].filter(Boolean).join(' ')}>
        <style href="sp-signalfeed" precedence="sp-component">{SIGNAL_FEED_CSS}</style>
        {list}
        {footer}
      </div>
    );
  }

  return (
    <section
      className={['card', 'card--flush', 'card--grow', 'sp-feed', className].filter(Boolean).join(' ')}
    >
      <style href="sp-signalfeed" precedence="sp-component">{SIGNAL_FEED_CSS}</style>
      <header className="card__header">
        <h2 className="card__title">{t('feed.title')}</h2>
        <div className="card__actions">
          {selected ? (
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={() => {
                if (controlledSelectedId === undefined) setSelfSelectedId(null);
                onSelect?.(null);
              }}
            >
              {t('app.clear')}
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => { setReloadToken((v) => v + 1); }}
            title={t('app.refresh')}
          >
            {t('app.refresh')}
          </button>
        </div>
      </header>
      <div className="card__body sp-feed__body">{list}</div>
      {footer}
    </section>
  );
}

export default SignalFeed;

/* ──────────────────────────────── styles ─────────────────────────────────── */

const SIGNAL_FEED_CSS = `
.sp-feed {
  min-height: 0;
}

.sp-feed--bare {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
}

.sp-feed__body {
  padding: 0;
  gap: 0;
  min-height: 0;
}

.sp-feed__scroll {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
}

.sp-feed__loading {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
}

.sp-feed__no-runs {
  font-size: var(--fs-1);
  font-style: italic;
  color: var(--text-dim);
}

.sp-feed__foot {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: none;
  padding: var(--sp-1) var(--sp-3);
  font-size: var(--fs-1);
  color: var(--text-dim);
  background: transparent;
  border-top: var(--border-w) solid var(--border);
}

.sp-feed__foot-label {
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.sp-feed__foot-num {
  color: var(--text);
}
`;
