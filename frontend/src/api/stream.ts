/**
 * The dashboard's single WebSocket to `GET /api/stream` (CONTRACTS §19, BUILD §9).
 *
 * One socket carries every event of CONTRACTS §4. It reconnects on its own with exponential
 * backoff from 1 s to 30 s, and it calls `onStatus('open')` on **every** (re)connect —
 * including the first — because the contract for the caller is:
 *
 *     the stream is a live tail, never a source of truth.
 *
 * A reconnect means an unknown number of events were missed while the socket was down, so the
 * page must refetch its state over REST every time `'open'` arrives rather than assuming the
 * buffer covered the gap. The server's `hello` frame replays its last hundred envelopes
 * through `onEvent`, which redraws the recent past immediately, but a hundred is a buffer and
 * not a guarantee — REST is.
 *
 * Liveness: the client sends `{"type":"ping"}` every 25 s and the server answers `{"type":"pong"}`.
 * A socket that has sent nothing at all for 60 s is treated as dead — half-open TCP connections
 * stay `OPEN` in the browser forever — and is closed so the backoff can bring it back.
 */

import { useEffect, useRef } from 'react';

import {
  BASE_PATH,
  normalizeStreamEvent,
  type StreamEvent,
  type StreamHello,
} from './client';

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

/** First reconnect delay, and the floor for every later one. */
export const RECONNECT_MIN_MS = 1_000;
/** Ceiling for the reconnect delay; a backend that is down for an hour is retried every 30 s. */
export const RECONNECT_MAX_MS = 30_000;
/** Growth per failed attempt: 1s, 1.8s, 3.2s, 5.8s, 10.5s, 18.9s, 30s, 30s… */
export const RECONNECT_FACTOR = 1.8;
/** Fraction of the delay spread randomly around it, so many tabs do not retry in lockstep. */
export const RECONNECT_JITTER = 0.25;
/** How often the client pings. */
export const PING_INTERVAL_MS = 25_000;
/** Silence longer than this means the socket is dead even though the browser still says OPEN. */
export const PONG_TIMEOUT_MS = 60_000;
/** Close code used when the client hangs up a socket that stopped answering. */
const CLOSE_CODE_STALE = 4000;
/** Close code used when the caller disposes the stream deliberately. */
const CLOSE_CODE_NORMAL = 1000;

// ---------------------------------------------------------------------------
// Public shapes
// ---------------------------------------------------------------------------

/**
 * Connection state as the caller sees it.
 *
 * `'open'` is the only one that carries an obligation: refetch REST state.
 */
export type StreamStatus = 'connecting' | 'open' | 'closed';

/** Called once per event, in arrival order, `hello` replays included. */
export type StreamEventHandler = (event: StreamEvent) => void;

/** Called on every connection state change. */
export type StreamStatusHandler = (status: StreamStatus) => void;

/** What `createStream` hands back. */
export interface StreamHandle {
  /** Hang up for good: no further events, no further reconnects. Safe to call twice. */
  close(): void;
  /** The current connection state, for a caller that polls rather than subscribes. */
  getStatus(): StreamStatus;
}

/** The WebSocket URL for this origin: same host, same port, `ws` or `wss` to match the page. */
export function streamUrl(): string {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${window.location.host}${BASE_PATH}/stream`;
}

/**
 * Delay before reconnect attempt number `attempt` (0-based), in milliseconds.
 *
 * Exponential from `RECONNECT_MIN_MS`, capped at `RECONNECT_MAX_MS`, then spread by
 * ±`RECONNECT_JITTER` and clamped back into range.
 */
export function backoffDelay(attempt: number): number {
  const raw =
    RECONNECT_MIN_MS * Math.pow(RECONNECT_FACTOR, Math.max(0, attempt));
  const capped = Math.min(RECONNECT_MAX_MS, raw);
  const jitter = 1 + (Math.random() * 2 - 1) * RECONNECT_JITTER;
  const spread = Math.round(capped * jitter);
  return Math.min(RECONNECT_MAX_MS, Math.max(RECONNECT_MIN_MS, spread));
}

/**
 * Report a handler's exception without killing the socket.
 *
 * Rethrowing asynchronously puts it in front of `window.onerror` and the console, where a bug
 * in a reducer belongs, while the stream keeps running — a broken card must not cost the
 * operator their price feed.
 */
function reportLater(cause: unknown): void {
  setTimeout(() => {
    throw cause;
  }, 0);
}

// ---------------------------------------------------------------------------
// createStream
// ---------------------------------------------------------------------------

/**
 * Open the dashboard stream and keep it open.
 *
 * @param onEvent  receives every event, in order; `hello` replays arrive here too.
 * @param onStatus receives every state change. Refetch REST state on `'open'`.
 */
export function createStream(
  onEvent: StreamEventHandler,
  onStatus?: StreamStatusHandler,
): StreamHandle {
  const url = streamUrl();

  let socket: WebSocket | null = null;
  let status: StreamStatus = 'closed';
  let attempt = 0;
  let disposed = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let pingTimer: ReturnType<typeof setInterval> | null = null;
  /** Timestamp of the last frame of any kind; a pong is simply the one that always arrives. */
  let lastFrameAt = 0;

  function setStatus(next: StreamStatus): void {
    if (status === next) return;
    status = next;
    if (!onStatus) return;
    try {
      onStatus(next);
    } catch (cause) {
      reportLater(cause);
    }
  }

  function emit(candidate: unknown): void {
    const event = normalizeStreamEvent(candidate);
    if (!event) return;
    try {
      onEvent(event);
    } catch (cause) {
      reportLater(cause);
    }
  }

  function clearReconnect(): void {
    if (reconnectTimer === null) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  function stopPing(): void {
    if (pingTimer === null) return;
    clearInterval(pingTimer);
    pingTimer = null;
  }

  function startPing(ws: WebSocket): void {
    stopPing();
    pingTimer = setInterval(() => {
      if (disposed || socket !== ws) return;
      if (ws.readyState !== WebSocket.OPEN) return;

      // Nothing has arrived in a minute: the socket is open only in the browser's opinion.
      if (Date.now() - lastFrameAt > PONG_TIMEOUT_MS) {
        stopPing();
        try {
          ws.close(CLOSE_CODE_STALE, 'no pong');
        } catch {
          // A socket that refuses to close is already gone; onclose still drives the reconnect.
        }
        return;
      }

      try {
        ws.send(JSON.stringify({ type: 'ping' }));
      } catch {
        // The send failed because the socket is dying; onclose will schedule the reconnect.
      }
    }, PING_INTERVAL_MS);
  }

  function scheduleReconnect(): void {
    if (disposed || reconnectTimer !== null) return;
    const delay = backoffDelay(attempt);
    attempt += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  /** Replay the `hello` frame's buffered envelopes so a reconnect redraws the recent past. */
  function replayHello(frame: StreamHello): void {
    const recent = frame.data?.recent;
    if (!Array.isArray(recent)) return;
    for (const candidate of recent) {
      emit(candidate);
    }
  }

  function handleFrame(raw: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw) as unknown;
    } catch {
      return; // Not JSON: nothing the contract describes, so nothing to do.
    }
    if (typeof parsed !== 'object' || parsed === null) return;

    lastFrameAt = Date.now();
    const type = (parsed as { type?: unknown }).type;

    if (type === 'pong') return;
    if (type === 'hello') {
      replayHello(parsed as StreamHello);
      return;
    }
    // The event set is closed (CONTRACTS §4): an unknown type is dropped rather than guessed at.
    emit(parsed);
  }

  function connect(): void {
    if (disposed) return;
    clearReconnect();
    setStatus('connecting');

    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (cause) {
      // A malformed URL or a blocked scheme: back off exactly as a refused connection would.
      reportLater(cause);
      setStatus('closed');
      scheduleReconnect();
      return;
    }
    socket = ws;

    ws.onopen = (): void => {
      if (disposed || socket !== ws) return;
      attempt = 0;
      lastFrameAt = Date.now();
      startPing(ws);
      // Every (re)connect, first one included: the caller refetches REST state here.
      setStatus('open');
    };

    ws.onmessage = (message: MessageEvent<unknown>): void => {
      if (disposed || socket !== ws) return;
      if (typeof message.data === 'string') handleFrame(message.data);
    };

    ws.onerror = (): void => {
      // `close` always follows `error`; scheduling here as well would double the backoff step.
    };

    ws.onclose = (): void => {
      if (socket !== ws) return;
      socket = null;
      stopPing();
      setStatus('closed');
      scheduleReconnect();
    };
  }

  function close(): void {
    disposed = true;
    clearReconnect();
    stopPing();

    const ws = socket;
    socket = null;
    if (ws) {
      // Detach first: a close during the handshake still fires `onclose`, and a live
      // `onclose` here would schedule the reconnect this call exists to prevent.
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      try {
        // Closing a socket that is still CONNECTING aborts the handshake immediately. Chrome
        // logs a warning about it, which is the price of StrictMode's mount/unmount/mount
        // never leaving a second connection alive on the backend.
        ws.close(CLOSE_CODE_NORMAL, 'client closed');
      } catch {
        // Already gone.
      }
    }

    setStatus('closed');
  }

  connect();

  return {
    close,
    getStatus: (): StreamStatus => status,
  };
}

// ---------------------------------------------------------------------------
// React binding
// ---------------------------------------------------------------------------

/**
 * Mount one stream for the lifetime of a component.
 *
 * The handlers are read through refs, so passing an inline arrow function does not tear the
 * socket down and build a new one on every render. The socket itself is created once, in an
 * effect with no dependencies, and closed in its cleanup — which is what makes StrictMode's
 * deliberate mount / unmount / mount leave exactly one socket behind rather than two.
 *
 * Refetch REST state inside `onStatus` when it is called with `'open'` (BUILD §9).
 */
export function useStream(
  onEvent: StreamEventHandler,
  onStatus?: StreamStatusHandler,
): void {
  const eventRef = useRef<StreamEventHandler>(onEvent);
  const statusRef = useRef<StreamStatusHandler | undefined>(onStatus);
  const handleRef = useRef<StreamHandle | null>(null);

  // Declared first so it runs before the connect effect on mount and after every later render.
  useEffect(() => {
    eventRef.current = onEvent;
    statusRef.current = onStatus;
  });

  useEffect(() => {
    // Defensive: a previous handle can only survive an interrupted cleanup, and two live
    // sockets would double every event in the feed.
    handleRef.current?.close();

    const handle = createStream(
      (event) => eventRef.current(event),
      (status) => statusRef.current?.(status),
    );
    handleRef.current = handle;

    return () => {
      handle.close();
      if (handleRef.current === handle) handleRef.current = null;
    };
  }, []);
}
