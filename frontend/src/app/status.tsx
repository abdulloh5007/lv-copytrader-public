import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { HealthOut, StatusMap } from '../api/client';
import { getHealth, isApiError } from '../api/client';
export const HEALTH_POLL_MS = 10_000;

export interface HealthSnapshot {
  /** The last successful `GET /api/health`, or null before the first one lands. */
  health: HealthOut | null;
  /** The three dots, pulled out of `health` for convenience. */
  status: StatusMap | null;
  /** The global arm flag as health last reported it. */
  armed: boolean;
  version: string | null;
  uptimeSeconds: number | null;
  /** Settings keys whose ciphertext no longer decrypts (CONTRACTS §6). */
  needsReentry: string[];
  /** True until the first answer of any kind arrives. */
  loading: boolean;
  /** True when the LAST poll failed; `health` still holds the last good answer. */
  failed: boolean;
  /** The failure's own words, for a tooltip. Null when there is nothing to add. */
  errorDetail: string | null;
  /** `Date.now()` of the last completed poll, successful or not. */
  lastCheckAt: number | null;
  /** Force a poll now — after arming, after saving settings, after a reconnect. */
  refresh: () => void;
}

export interface HealthState {
  health: HealthOut | null;
  loading: boolean;
  failed: boolean;
  errorDetail: string | null;
  lastCheckAt: number | null;
}

export const INITIAL_HEALTH: HealthState = {
  health: null,
  loading: true,
  failed: false,
  errorDetail: null,
  lastCheckAt: null,
};

export const NOOP = (): void => {};

export const EMPTY_SNAPSHOT: HealthSnapshot = {
  health: null,
  status: null,
  armed: false,
  version: null,
  uptimeSeconds: null,
  needsReentry: [],
  loading: true,
  failed: false,
  errorDetail: null,
  lastCheckAt: null,
  refresh: NOOP,
};

export function failureDetail(cause: unknown): string {
  if (isApiError(cause)) return cause.message;
  if (cause instanceof Error) return cause.message;
  return '';
}

export function useHealthPoll(intervalMs: number): HealthSnapshot {
  const [state, setState] = useState<HealthState>(INITIAL_HEALTH);
  const tickRef = useRef<() => void>(NOOP);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let inFlight: AbortController | null = null;

    const clearTimer = (): void => {
      if (timer === null) return;
      clearTimeout(timer);
      timer = null;
    };

    const isHidden = (): boolean => {
      try {
        return (
          typeof document !== 'undefined' &&
          document.visibilityState === 'hidden'
        );
      } catch {
        return false;
      }
    };

    const schedule = (delay: number): void => {
      clearTimer();
      if (cancelled) return;
      timer = setTimeout(() => {
        timer = null;
        void poll();
      }, delay);
    };

    const poll = async (): Promise<void> => {
      if (cancelled) return;
      if (isHidden()) {
        // Nobody is looking. Come back at the next tick, or sooner on visibilitychange.
        schedule(intervalMs);
        return;
      }

      inFlight?.abort();
      const controller = new AbortController();
      inFlight = controller;

      try {
        const health = await getHealth({ signal: controller.signal });
        if (cancelled || controller.signal.aborted) return;
        setState({
          health,
          loading: false,
          failed: false,
          errorDetail: null,
          lastCheckAt: Date.now(),
        });
      } catch (cause) {
        if (cancelled || controller.signal.aborted) return;
        if (isApiError(cause) && cause.isAborted) return;
        const detail = failureDetail(cause);
        setState((prev) => ({
          health: prev.health,
          loading: false,
          failed: true,
          errorDetail: detail === '' ? null : detail,
          lastCheckAt: Date.now(),
        }));
      } finally {
        if (inFlight === controller) inFlight = null;
        // A superseded request must not schedule: the request that replaced it does.
        if (!cancelled && !controller.signal.aborted) schedule(intervalMs);
      }
    };

    const pollNow = (): void => {
      if (cancelled || isHidden()) return;
      void poll();
    };

    tickRef.current = pollNow;

    if (typeof document !== 'undefined')
      document.addEventListener('visibilitychange', pollNow);
    if (typeof window !== 'undefined')
      window.addEventListener('online', pollNow);

    void poll();

    return () => {
      cancelled = true;
      clearTimer();
      inFlight?.abort();
      inFlight = null;
      tickRef.current = NOOP;
      if (typeof document !== 'undefined')
        document.removeEventListener('visibilitychange', pollNow);
      if (typeof window !== 'undefined')
        window.removeEventListener('online', pollNow);
    };
  }, [intervalMs]);

  const refresh = useCallback((): void => {
    tickRef.current();
  }, []);

  return useMemo<HealthSnapshot>(
    () => ({
      health: state.health,
      status: state.health ? state.health.status : null,
      armed: state.health ? state.health.armed === true : false,
      version: state.health ? state.health.version : null,
      uptimeSeconds: state.health ? state.health.uptime_s : null,
      needsReentry:
        state.health && Array.isArray(state.health.needs_reentry)
          ? state.health.needs_reentry
          : [],
      loading: state.loading,
      failed: state.failed,
      errorDetail: state.errorDetail,
      lastCheckAt: state.lastCheckAt,
      refresh,
    }),
    [state, refresh],
  );
}

export const AppStatusContext = createContext<HealthSnapshot>(EMPTY_SNAPSHOT);

export function useAppStatus(): HealthSnapshot {
  return useContext(AppStatusContext);
}
