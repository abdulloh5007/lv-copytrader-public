import { useEffect, useState } from 'react';
import type { DemoSnapshot } from './types';

export function useDemo() {
  const [data, setData] = useState<DemoSnapshot | null>(null);
  const [viewers, setViewers] = useState<number | null>(null);
  const [failed, setFailed] = useState(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let presenceTimer: ReturnType<typeof setTimeout> | undefined;
    let pending: AbortController | undefined;
    let presencePending: AbortController | undefined;
    let etag = '';
    let failures = 0;
    const visible = () => !disposed && document.visibilityState !== 'hidden';
    const jitter = () => Math.round(Math.random() * 500);
    async function poll() {
      if (!visible()) return;
      pending?.abort();
      const controller = new AbortController();
      pending = controller;
      const timeout = setTimeout(() => controller.abort(), 10_000);
      let delay = 5000;
      try {
        const response = await fetch('/api/demo/snapshot', {
          signal: controller.signal,
          credentials: 'omit',
          cache: 'no-store',
          headers: etag ? { 'If-None-Match': etag } : {},
        });
        if (response.status === 429)
          delay = Math.max(
            5000,
            Math.min(
              60_000,
              (Number(response.headers.get('Retry-After')) || 10) * 1000,
            ),
          );
        if (response.status !== 304) {
          if (!response.ok) throw new Error('Unavailable');
          const next: DemoSnapshot = await response.json();
          if (
            !next ||
            typeof next.generated_at !== 'number' ||
            !next.status ||
            (next.copytrader !== null &&
              !Array.isArray(next.copytrader?.trades))
          )
            throw new Error('Invalid snapshot');
          if (disposed || pending !== controller) return;
          etag = response.headers.get('ETag') || '';
          setData(next);
        }
        failures = 0;
        if (!disposed) setFailed(false);
      } catch {
        if (disposed || pending !== controller) return;
        setFailed(true);
        failures += 1;
        delay = Math.max(
          delay,
          Math.min(30_000, 5000 * 2 ** Math.min(failures, 3)),
        );
      } finally {
        clearTimeout(timeout);
        const ownsRequest = pending === controller;
        if (ownsRequest) pending = undefined;
        if (visible() && ownsRequest)
          timer = setTimeout(poll, delay + jitter());
      }
    }
    async function heartbeat() {
      if (!visible()) return;
      presencePending?.abort();
      const controller = new AbortController();
      presencePending = controller;
      const timeout = setTimeout(() => controller.abort(), 8000);
      let delay = 20_000;
      try {
        const send = async () => {
          if (!visible() || controller.signal.aborted) return;
          const response = await fetch('/api/demo/presence', {
            method: 'POST',
            credentials: 'same-origin',
            signal: controller.signal,
            cache: 'no-store',
          });
          if (response.status === 429)
            delay = Math.max(
              delay,
              Math.min(
                60_000,
                (Number(response.headers.get('Retry-After')) || 20) * 1000,
              ),
            );
          if (!response.ok) throw new Error('Unavailable');
          const body = await response.json();
          if (
            !disposed &&
            presencePending === controller &&
            !controller.signal.aborted &&
            Number.isSafeInteger(body.viewers) &&
            body.viewers >= 1
          )
            setViewers(body.viewers);
        };
        // Serializes the initial cookie handshake across tabs in this browser.
        if (navigator.locks)
          await navigator.locks.request(
            'lvct-demo-presence',
            { signal: controller.signal },
            send,
          );
        else await send();
      } catch {
        if (!disposed && presencePending === controller) setViewers(null);
      } finally {
        clearTimeout(timeout);
        const ownsRequest = presencePending === controller;
        if (ownsRequest) presencePending = undefined;
        if (visible() && ownsRequest)
          presenceTimer = setTimeout(heartbeat, delay + jitter());
      }
    }
    function visibility() {
      clearTimeout(timer);
      clearTimeout(presenceTimer);
      pending?.abort();
      pending = undefined;
      presencePending?.abort();
      presencePending = undefined;
      if (visible()) {
        void poll();
        void heartbeat();
      }
    }
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('online', visibility);
    visibility();
    const clock = setInterval(() => {
      if (document.visibilityState !== 'hidden') setNow(Date.now());
    }, 1000);
    return () => {
      disposed = true;
      clearTimeout(timer);
      clearTimeout(presenceTimer);
      clearInterval(clock);
      pending?.abort();
      presencePending?.abort();
      document.removeEventListener('visibilitychange', visibility);
      window.removeEventListener('online', visibility);
    };
  }, []);
  const fresh =
    !!data?.account_updated_at &&
    now / 1000 - data.account_updated_at < 16 &&
    data.status.broker &&
    !failed;
  const historyFresh =
    !!data?.history_updated_at &&
    now / 1000 - data.history_updated_at < 40 &&
    !failed;
  return { data, viewers, fresh, historyFresh, failed };
}
