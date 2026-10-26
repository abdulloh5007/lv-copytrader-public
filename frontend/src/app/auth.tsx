import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { AuthStatusOut } from '../api/client';
import { authStatus, isApiError, logout as postLogout } from '../api/client';
export type AuthPhase = 'unknown' | 'open' | 'locked';

export interface AuthGate {
  phase: AuthPhase;
  /** True when the backend enforces a sign-in at all (BUILD: it can be off). */
  required: boolean;
  /** Who the backend says is signed in, when it says. */
  username: string | null;
  /** Ask `/api/auth/status` again — after a sign-in, and after a sign-out. */
  refresh: () => void;
  /** Drop the session cookie and return to the login page. */
  signOut: () => void;
  signingOut: boolean;
  /** The last sign-out failure, in the server's own words. */
  signOutError: string | null;
}

export interface AuthState {
  phase: AuthPhase;
  required: boolean;
  username: string | null;
}

export const INITIAL_AUTH: AuthState = {
  phase: 'unknown',
  required: false,
  username: null,
};

export function useAuthGate(): AuthGate {
  const [state, setState] = useState<AuthState>(INITIAL_AUTH);
  const [signingOut, setSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const statusRef = useRef<AbortController | null>(null);
  const busyRef = useRef(false);

  const load = useCallback((): void => {
    statusRef.current?.abort();
    const controller = new AbortController();
    statusRef.current = controller;

    void (async () => {
      try {
        const status: AuthStatusOut = await authStatus({
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        const required = status.required === true;
        const authenticated = status.authenticated === true;
        setState({
          phase: required && !authenticated ? 'locked' : 'open',
          required,
          username: status.username ?? null,
        });
      } catch (cause) {
        if (controller.signal.aborted) return;
        if (isApiError(cause) && cause.isAborted) return;
        if (
          isApiError(cause) &&
          (cause.status === 401 || cause.status === 403)
        ) {
          setState({ phase: 'locked', required: true, username: null });
          return;
        }
        setState({ phase: 'locked', required: true, username: null });
      } finally {
        if (statusRef.current === controller) statusRef.current = null;
      }
    })();
  }, []);

  useEffect(() => {
    load();
    return () => {
      statusRef.current?.abort();
      statusRef.current = null;
    };
  }, [load]);

  useEffect(() => {
    const lock = () =>
      setState({ phase: 'locked', required: true, username: null });
    window.addEventListener('sp:unauthorized', lock);
    return () => window.removeEventListener('sp:unauthorized', lock);
  }, []);

  const signOut = useCallback((): void => {
    if (busyRef.current) return;
    busyRef.current = true;
    setSigningOut(true);
    setSignOutError(null);

    void (async () => {
      try {
        await postLogout();
      } catch (cause) {
        const message =
          isApiError(cause) || cause instanceof Error
            ? cause.message
            : String(cause);
        setSignOutError(message === '' ? null : message);
      } finally {
        busyRef.current = false;
        setSigningOut(false);
        // Asked again either way: a failed response does not prove the cookie
        // survived, and the only authority on that is the backend.
        load();
      }
    })();
  }, [load]);

  return useMemo<AuthGate>(
    () => ({
      phase: state.phase,
      required: state.required,
      username: state.username,
      refresh: load,
      signOut,
      signingOut,
      signOutError,
    }),
    [state, load, signOut, signingOut, signOutError],
  );
}
