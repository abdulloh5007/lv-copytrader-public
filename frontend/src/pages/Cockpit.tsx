/**
 * LV Copytrader — Cockpit, the landing page (BUILD_PROMPT §9).
 *
 * Three columns on desktop, stacked on mobile:
 *
 *   left    SignalFeed  — everything the channel posted, skipped rows included.
 *   centre  RunStream   — the current run streaming: thinking, then tool cards.
 *   right   AccountPanel, the three StatusDots, and the ARM / DISARM switch —
 *           the largest control on the page — with the kill switch beside it
 *           behind its typed confirmation.
 *
 * THE PAGE OWNS THE SOCKET. One `useStream` subscription (CONTRACTS §19) feeds a
 * `createEventFanout()` bus, and the two feed columns attach to that bus through
 * their `subscribe` prop, so there is exactly one WebSocket no matter how many
 * panels are mounted.
 *
 * THE STREAM IS A TAIL, NEVER A SOURCE OF TRUTH. On EVERY (re)connect — the very
 * first one included — state is refetched over REST rather than assuming nothing
 * was missed while the socket was down:
 *
 *   · the page itself refetches `tradingState()` and `getAccount()`;
 *   · `SignalFeed` refetches `getSignals()` off the same `'open'` status;
 *   · `RunStream`  refetches `getRuns()` off the same `'open'` status.
 *
 * Those two components already own their half of that contract, and duplicating
 * their fetches here would double every request on every reconnect. The fan-out's
 * status hop is what wires all four calls to one event.
 *
 * WHAT THE SHELL ALREADY OWNS. `App.tsx` runs the dashboard's single
 * `GET /api/health` poll and publishes it through `useAppStatus()`, and it
 * renders the global NEEDS-REENTRY banner above every route — this one included.
 * So the cockpit reads that snapshot rather than opening a second heartbeat, and
 * it raises its own re-entry banner ONLY for keys the shell has not reported
 * yet: a key learnt from a `system.warning` in the second after it happened,
 * which the shell's ten-second poll has not seen. It also pokes `refresh()` at
 * that moment, so the shell catches up and the page's stand-in disappears on its
 * own. Two identical banners would be worse than none.
 *
 * Standing notices sit above the grid and none of them is a toast — a message
 * about a broker connection or an unreadable secret must not disappear while the
 * operator is looking at something else (§9):
 *
 *   · the stand-in NEEDS-REENTRY banner described above, naming its keys;
 *   · the no-source banner: before a channel is picked nothing is ingested at
 *     all, and an empty feed would otherwise read as a fault;
 *   · a strip per `system.warning` reason — queue overflow, a failed
 *     notification, MT5 disconnected — deduplicated by reason with a repeat
 *     count, dismissible, and dismissed-for-good: the `hello` frame replays the
 *     last hundred envelopes on every reconnect, so each envelope is remembered
 *     by identity and a replay never resurrects a strip the operator cleared.
 *
 * Every string reaches the DOM through `t()`. A `system.warning` reason with no
 * dictionary entry falls back to the backend's own sentence, framed by
 * `status.error_detail` — the fallback CONTRACTS §4 documents — and the machine
 * reason always rides along as a monospace chip so the strip identifies itself
 * even when no prose exists for it.
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { CSSProperties, ReactElement } from 'react';
import { Link } from 'react-router-dom';

import {
  getAccount,
  isApiError,
  tradingState,
  type AccountOut,
  type ComponentStatus,
  type SignalOut,
  type StreamEvent,
  type TradingStateOut,
} from '../api/client';
import { useStream, type StreamStatus } from '../api/stream';
import { useAppStatus } from '../App';
import AccountPanel, { accountFromEvent } from '../components/AccountPanel';
import ArmSwitch from '../components/ArmSwitch';
import RunStream, { createEventFanout } from '../components/RunStream';
import SignalFeed from '../components/SignalFeed';
import StatusDots from '../components/StatusDots';
import {
  formatInteger,
  formatTime,
  hasKey,
  useCurrentLocale,
  useT,
  type Locale,
  type TKey,
  type Translate,
} from '../i18n';

/* ────────────────────────────── tunables ─────────────────────────────────── */

/** How many warning strips stay on screen at once; the newest win. */
const MAX_WARNINGS = 4;

/** How many event identities are remembered, so a `hello` replay is idempotent. */
const SEEN_WARNING_CAP = 512;

/**
 * How long a dismissed strip stays mounted so it can play its exit. The CSS
 * animation is var(--dur-fast) (120ms); this is a hair longer so the last frame
 * is never cut off.
 */
const STRIP_EXIT_MS = 140;

/** Breathing room under the columns when their height is measured. */
const COLUMN_BOTTOM_GAP = 16;

/** A column never measures shorter than this, however cramped the viewport. */
const MIN_COLUMN_HEIGHT = 320;

/** A re-measure under this many pixels is noise and is not worth a render. */
const HEIGHT_EPSILON = 2;

/**
 * `system.warning` reasons that already have their own banner and must not be
 * repeated as a strip. `needs_reentry` names its keys in the banner instead.
 */
const BANNER_REASONS: ReadonlySet<string> = new Set<string>(['needs_reentry']);

/**
 * Machine reason → the dictionary key that says exactly the same thing.
 *
 * Only exact matches belong here. A reason with no entry falls through to
 * `warn.<reason>` — which lights up on its own the day those keys are added —
 * and then to the backend's English sentence.
 */
const WARNING_KEYS: Readonly<Record<string, TKey>> = {
  queue_overflow: 'feed.queue_overflow',
  mt5_disconnected: 'err.mt5_disconnected',
  telegram_disconnected: 'err.telegram_disconnected',
};

/** Reasons that mean something is broken rather than something merely happened. */
const BAD_REASONS: ReadonlySet<string> = new Set<string>([
  'mt5_disconnected',
  'telegram_disconnected',
  'provider_failing',
  'stopless_position',
]);

/* ─────────────────────────────── local shapes ────────────────────────────── */

/** One standing warning strip, folded across every repeat of its reason. */
interface WarningStrip {
  /** The stable machine key from `SystemWarningData.reason`. */
  reason: string;
  /** The backend's English sentence, kept as the last-resort prose. */
  message: string | null;
  /** Unix seconds of the most recent occurrence. */
  ts: number;
  /** How many times this reason has arrived since it was last dismissed. */
  count: number;
}

/** `style` carrying a CSS custom property; React's `CSSProperties` has no index signature. */
type StyleWithVars = CSSProperties & Record<`--${string}`, string>;

/* ──────────────────────────────── helpers ────────────────────────────────── */

/** Turn a thrown value into the sentence the operator should read. */
function describeFailure(cause: unknown, t: Translate): string | null {
  if (isApiError(cause)) {
    if (cause.isAborted) return null;
    if (cause.isNetwork) return t('err.network');
    return cause.message;
  }
  if (cause instanceof Error) return cause.message;
  return String(cause);
}

/**
 * The prose for one warning reason, or `null` when nothing translatable exists.
 *
 * Order: an exact dictionary key → the `warn.<reason>` namespace → the backend's
 * own message framed as a quote. The machine reason is rendered separately as a
 * chip, so returning `null` leaves a strip that is still identifiable.
 */
function warningText(
  reason: string,
  message: string | null,
  t: Translate,
  locale: Locale,
): string | null {
  const mapped = WARNING_KEYS[reason];
  if (mapped !== undefined && hasKey(mapped, locale)) return t(mapped);

  const namespaced = `warn.${reason}`;
  if (hasKey(namespaced, locale)) return t(namespaced);

  const detail = message === null ? '' : message.trim();
  if (detail) return t('status.error_detail', { detail });

  return null;
}

/** Identity of one warning envelope, stable across a `hello` replay. */
function warningIdentity(event: StreamEvent): string {
  return `${String(event.seq)}:${String(event.ts)}:${event.type}`;
}

/** Merge one key into the needs-reentry list without disturbing its order twice. */
function withKey(keys: readonly string[], key: string): string[] {
  if (keys.includes(key)) return keys.slice();
  return [...keys, key].sort();
}

/* ──────────────────────────────── the page ──────────────────────────────── */

export function Cockpit(): ReactElement {
  const t = useT();
  const locale = useCurrentLocale();

  /** The shell's one health poll (App.tsx). The page never opens a second one. */
  const shell = useAppStatus();
  const shellRefresh = shell.refresh;

  /** One socket for the whole page; every panel attaches to this fan-out. */
  const fanout = useMemo(createEventFanout, []);

  /* ---- trading state: arm flag, the three dots, the active source ---- */

  const [state, setState] = useState<TradingStateOut | null>(null);
  const [stateError, setStateError] = useState<string | null>(null);
  const [stateAt, setStateAt] = useState<number | null>(null);
  const [stateLoading, setStateLoading] = useState(true);

  /* ---- account snapshot ---- */

  const [account, setAccount] = useState<AccountOut | null>(null);
  const [accountError, setAccountError] = useState<string | null>(null);
  const [accountAt, setAccountAt] = useState<number | null>(null);
  const [accountLoading, setAccountLoading] = useState(true);

  /* ---- page chrome ---- */

  const [refreshToken, setRefreshToken] = useState(0);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>('connecting');
  const [everOpened, setEverOpened] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [warnings, setWarnings] = useState<WarningStrip[]>([]);
  const [reentry, setReentry] = useState<readonly string[]>([]);

  // `t` is read inside fetch callbacks that must not re-run when the locale
  // changes; a ref keeps their dependency list at the refresh token alone.
  const tRef = useRef<Translate>(t);
  useEffect(() => {
    tRef.current = t;
  });

  /* ---- REST refetch, on mount and on every (re)connect ---- */

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    setStateLoading(true);
    setAccountLoading(true);

    void tradingState({ signal: controller.signal })
      .then((next) => {
        if (cancelled) return;
        setState(next);
        setStateError(null);
        setStateAt(Date.now());
        // REST is authoritative for this list: a key the operator has re-entered
        // must disappear, which a union with the stream's additions would prevent.
        setReentry(next.needs_reentry.slice().sort());
        setStateLoading(false);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        const described = describeFailure(cause, tRef.current);
        if (described !== null) setStateError(described);
        setStateLoading(false);
      });

    void getAccount({ signal: controller.signal })
      .then((next) => {
        if (cancelled) return;
        setAccount(next);
        setAccountError(null);
        setAccountAt(Date.now());
        setAccountLoading(false);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        const described = describeFailure(cause, tRef.current);
        if (described !== null) setAccountError(described);
        setAccountLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [refreshToken]);

  const refresh = useCallback(() => {
    setRefreshToken((value) => value + 1);
  }, []);

  /* ---- warnings: deduplicated by reason, idempotent across a replay ---- */

  /**
   * Reasons whose strip has been dismissed and is playing its exit. The row is
   * still mounted — an exit cannot animate a node that is already gone — and is
   * removed for real when the timer below fires.
   */
  const [leavingWarnings, setLeavingWarnings] = useState<readonly string[]>([]);
  const exitTimers = useRef<Map<string, number>>(new Map());

  useEffect(
    () => () => {
      for (const timer of exitTimers.current.values())
        window.clearTimeout(timer);
      exitTimers.current.clear();
    },
    [],
  );

  const seenWarnings = useRef<Set<string>>(new Set<string>());
  const seenOrder = useRef<string[]>([]);

  /** True the first time an envelope identity is offered; false on every replay. */
  const markSeen = useCallback((identity: string): boolean => {
    if (seenWarnings.current.has(identity)) return false;
    seenWarnings.current.add(identity);
    seenOrder.current.push(identity);
    if (seenOrder.current.length > SEEN_WARNING_CAP) {
      const dropped = seenOrder.current.shift();
      if (dropped !== undefined) seenWarnings.current.delete(dropped);
    }
    return true;
  }, []);

  const pushWarning = useCallback(
    (reason: string, message: string | null, ts: number) => {
      // The same reason coming back while its strip is on the way out cancels the
      // exit: the operator dismissed the old occurrence, not this one.
      const pending = exitTimers.current.get(reason);
      if (pending !== undefined) {
        window.clearTimeout(pending);
        exitTimers.current.delete(reason);
        setLeavingWarnings((prev) => prev.filter((row) => row !== reason));
      }
      setWarnings((prev) => {
        const existing = prev.find((row) => row.reason === reason);
        const next: WarningStrip = {
          reason,
          message: message ?? existing?.message ?? null,
          ts,
          count: (existing?.count ?? 0) + 1,
        };
        return [next, ...prev.filter((row) => row.reason !== reason)].slice(
          0,
          MAX_WARNINGS,
        );
      });
    },
    [],
  );

  const dismissWarning = useCallback((reason: string) => {
    setWarnings((prev) => prev.filter((row) => row.reason !== reason));
  }, []);

  /**
   * Dismiss in two steps: mark the strip leaving, and take it out of the list
   * once it has left. The exit is a fixed 4px of travel and a fade — never a
   * height collapse, which would drag every strip under it up the screen while
   * the operator is still reading them.
   */
  const beginDismissWarning = useCallback(
    (reason: string) => {
      if (exitTimers.current.has(reason)) return;
      setLeavingWarnings((prev) =>
        prev.includes(reason) ? prev : [...prev, reason],
      );
      const timer = window.setTimeout(() => {
        exitTimers.current.delete(reason);
        setLeavingWarnings((prev) => prev.filter((row) => row !== reason));
        dismissWarning(reason);
      }, STRIP_EXIT_MS);
      exitTimers.current.set(reason, timer);
    },
    [dismissWarning],
  );

  /* ---- the one subscription ---- */

  const handleEvent = useCallback(
    (event: StreamEvent) => {
      // The panels see every event first; the page only reduces what it owns.
      fanout.publish(event);

      switch (event.type) {
        case 'account.tick': {
          const next = accountFromEvent(event);
          if (next === null) break;
          setAccount(next);
          setAccountAt(Date.now());
          setAccountError(null);
          setAccountLoading(false);
          break;
        }

        case 'status.changed': {
          const patch: ComponentStatus = {
            ok: event.data.ok,
            error: event.data.error,
          };
          const key = event.data.key;
          setState((prev) => {
            if (prev === null) return prev;
            if (key === 'mt5') return { ...prev, mt5: patch };
            if (key === 'telegram') return { ...prev, telegram: patch };
            return { ...prev, provider: patch };
          });
          setStateAt(Date.now());
          break;
        }

        case 'settings.changed': {
          // The active source, the arm flag and the dots all live in settings;
          // rather than guess which moved, ask for the state again.
          refresh();
          shellRefresh();
          break;
        }

        case 'system.warning': {
          if (!markSeen(warningIdentity(event))) break;
          const reason = event.data.reason;
          const message =
            typeof event.data.message === 'string' ? event.data.message : null;

          if (reason === 'needs_reentry') {
            const key = event.data.key;
            if (typeof key === 'string' && key.length > 0) {
              setReentry((prev) => withKey(prev, key));
            }
            // The shell owns the global banner; get it repolled rather than
            // leaving the page's stand-in up for ten seconds.
            shellRefresh();
          }

          if (BANNER_REASONS.has(reason)) break;
          pushWarning(reason, message, event.ts);
          break;
        }

        default:
          break;
      }
    },
    [fanout, markSeen, pushWarning, refresh, shellRefresh],
  );

  const handleStatus = useCallback(
    (status: StreamStatus) => {
      // The panels refetch their own halves off this same hop.
      fanout.publishStatus(status);
      setStreamStatus(status);
      if (status !== 'open') return;
      setEverOpened(true);
      refresh();
      // A reconnect is exactly the moment the shell's snapshot is least trustworthy.
      shellRefresh();
    },
    [fanout, refresh, shellRefresh],
  );

  useStream(handleEvent, handleStatus);

  /**
   * Keys the shell's banner is not naming yet. In the steady state this is
   * empty and the page shows nothing — the shell's banner is the banner.
   */
  const pendingReentry = useMemo(
    () => reentry.filter((key) => !shell.needsReentry.includes(key)),
    [reentry, shell.needsReentry],
  );

  /* ---- column height, measured rather than guessed ---- */

  const gridRef = useRef<HTMLDivElement | null>(null);
  const [columnHeight, setColumnHeight] = useState<number | null>(null);

  const bannerCount =
    warnings.length +
    (pendingReentry.length > 0 ? 1 : 0) +
    (state !== null && state.source === null ? 1 : 0) +
    (everOpened && streamStatus !== 'open' ? 1 : 0);

  useLayoutEffect(() => {
    const node = gridRef.current;
    if (node === null) return;

    const measure = (): void => {
      // `top` is viewport-relative, so a scrolled shell would otherwise report
      // more room than exists; the viewport itself is the ceiling.
      const top = node.getBoundingClientRect().top;
      const ceiling = window.innerHeight - COLUMN_BOTTOM_GAP;
      const available = Math.round(
        Math.min(ceiling, window.innerHeight - top - COLUMN_BOTTOM_GAP),
      );
      const next = Math.max(MIN_COLUMN_HEIGHT, available);
      setColumnHeight((prev) =>
        prev !== null && Math.abs(prev - next) < HEIGHT_EPSILON ? prev : next,
      );
    };

    measure();
    window.addEventListener('resize', measure, { passive: true });

    // The shell's chrome can change height without the window resizing — a nav
    // wrapping, a banner appearing above us. The epsilon guard is what keeps an
    // observer that watches the element it also resizes from looping.
    const observer =
      typeof ResizeObserver === 'undefined'
        ? null
        : new ResizeObserver(measure);
    if (observer !== null && node.parentElement !== null)
      observer.observe(node.parentElement);

    return () => {
      window.removeEventListener('resize', measure);
      if (observer !== null) observer.disconnect();
    };
  }, [bannerCount]);

  const gridStyle: StyleWithVars | undefined =
    columnHeight === null
      ? undefined
      : { '--cockpit-h': `${String(columnHeight)}px` };

  /* ---- derived chrome ---- */

  // `tradingState` is the page's own read; the shell's poll fills the gap before
  // it lands and after it fails, so the dots and the switch are never blank when
  // one of the two knows the answer.
  const armed = state?.armed ?? shell.armed;
  const controlsLocked = state === null && shell.health === null;
  const dotsSource = state ?? shell.status;
  const dotsLoading = stateLoading && shell.status === null;
  const dotsCheckedAt = stateAt ?? shell.lastCheckAt;
  const noSource = state !== null && state.source === null;
  const streamDotClass =
    streamStatus === 'open'
      ? 'dot dot--ok'
      : streamStatus === 'connecting'
        ? 'dot dot--idle'
        : 'dot dot--bad';
  const streamWord =
    streamStatus === 'open'
      ? t('app.stream_connected')
      : streamStatus === 'connecting'
        ? t('app.stream_connecting')
        : t('app.stream_disconnected');

  const onArmedChange = useCallback(
    (next: boolean) => {
      setState((prev) => (prev === null ? prev : { ...prev, armed: next }));
      shellRefresh();
    },
    [shellRefresh],
  );

  const onKillResult = useCallback(() => {
    // Everything just closed: read the balance back rather than infer it.
    refresh();
  }, [refresh]);

  const onSelectSignal = useCallback((signal: SignalOut | null) => {
    setSelectedId(signal === null ? null : signal.id);
  }, []);

  const onOpenSignal = useCallback((messageRowId: number) => {
    setSelectedId(messageRowId);
  }, []);

  /* ──────────────────────────────── render ──────────────────────────────── */

  return (
    <div className="col cockpit-page">
      <header className="page-heading">
        <div>
          <h1>{t('workspace.overview')}</h1>
          <p>{t('workspace.description')}</p>
        </div>
        <span className="page-live" data-live={streamStatus === 'open'}>
          {t(streamStatus === 'open' ? 'workspace.live' : 'workspace.offline')}
        </span>
      </header>
      <div className="cockpit__banners">
        {pendingReentry.length > 0 ? (
          <div className="cockpit__strip cockpit__strip--bad" role="alert">
            <span className="cockpit__strip-text">
              {t('status.needs_reentry_banner', {
                keys: pendingReentry.join(', '),
              })}
            </span>
            <Link className="btn btn--sm" to="/settings">
              {t('status.needs_reentry_action')}
            </Link>
          </div>
        ) : null}

        {noSource ? (
          <div className="cockpit__strip cockpit__strip--info" role="status">
            <span className="cockpit__strip-text">
              {t('status.no_source_banner')}
            </span>
            <Link className="btn btn--sm" to="/settings#telegram">
              {t('nav.settings')}
            </Link>
          </div>
        ) : null}

        {everOpened && streamStatus !== 'open' ? (
          <div className="cockpit__strip" role="status">
            <span className={streamDotClass}>{t('status.stream')}</span>
            <span className="cockpit__strip-text">{streamWord}</span>
          </div>
        ) : null}

        {warnings.map((warning) => {
          const prose = warningText(warning.reason, warning.message, t, locale);
          const bad = BAD_REASONS.has(warning.reason);
          const leaving = leavingWarnings.includes(warning.reason);
          const classes = [
            'cockpit__strip',
            bad ? 'cockpit__strip--bad' : null,
            leaving ? 'is-leaving' : null,
          ]
            .filter(Boolean)
            .join(' ');
          return (
            <div key={warning.reason} className={classes} role="status">
              <span className="badge badge--warn mono" title={warning.reason}>
                {warning.reason}
                {warning.count > 1 ? (
                  <span className="badge__num">
                    {formatInteger(warning.count, locale)}
                  </span>
                ) : null}
              </span>
              <span className="cockpit__strip-text">{prose}</span>
              <span className="cockpit__strip-time mono mono--sm">
                {formatTime(warning.ts, locale)}
              </span>
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                disabled={leaving}
                onClick={() => {
                  beginDismissWarning(warning.reason);
                }}
              >
                {t('app.dismiss')}
              </button>
            </div>
          );
        })}
      </div>

      <div className="cockpit" ref={gridRef} style={gridStyle}>
        <div className="cockpit__col cockpit__col--feed">
          <SignalFeed
            subscribe={fanout.subscribe}
            selectedId={selectedId}
            onSelect={onSelectSignal}
            noSource={noSource}
          />
        </div>

        <div className="cockpit__col cockpit__col--runs">
          <RunStream
            subscribe={fanout.subscribe}
            messageRowId={selectedId}
            onOpenSignal={onOpenSignal}
          />
        </div>

        <div className="cockpit__col cockpit__col--panel">
          <section className="card">
            <header className="card__header">
              <h2 className="card__title">{t('status.title')}</h2>
              <div className="card__actions">
                <span className={streamDotClass} title={streamWord}>
                  {t('status.stream')}
                </span>
              </div>
            </header>
            <div className="card__body">
              <ArmSwitch
                armed={armed}
                onArmedChange={onArmedChange}
                onKillResult={onKillResult}
                disabled={controlsLocked}
              />

              <StatusDots
                status={dotsSource}
                loading={dotsLoading}
                lastCheckAt={dotsCheckedAt}
              />

              {stateError !== null ? (
                <div className="col col--tight" role="alert">
                  <p className="field__error">{stateError}</p>
                  <div className="row">
                    <button
                      type="button"
                      className="btn btn--sm"
                      onClick={refresh}
                    >
                      {t('app.retry')}
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
          </section>

          {/*
            A failed refetch must not blank instruments that are still showing a
            real balance: AccountPanel renders `error` INSTEAD of the readout, so
            the failure is surfaced only when there is nothing to lose. With a
            snapshot on screen the panel's own STALE badge carries the truth.
          */}
          <AccountPanel
            account={account}
            updatedAt={accountAt}
            loading={accountLoading}
            error={account === null ? accountError : null}
            onRetry={refresh}
          />
        </div>
      </div>
    </div>
  );
}

export default Cockpit;
