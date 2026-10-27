import { useEffect } from 'react';

export function PublicPet() {
  useEffect(() => {
    let dispose: (() => void) | undefined;
    let css: HTMLLinkElement | undefined;
    let timer: number | undefined;
    let idle: number | undefined;
    let cancelled = false;

    const start = () => {
      if (cancelled) return;
      css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = '/assets/showcase/lumi.css?v=public-pet-5';
      document.head.append(css);
      const moduleUrl = new URL('/assets/showcase/lumi.js?v=public-pet-5', window.location.origin).href;
      void import(/* @vite-ignore */ moduleUrl).then((module: { mountPet: () => () => void }) => {
        if (!cancelled) dispose = module.mountPet();
      }).catch((error: unknown) => { console.error('[LV-1] Failed to load the companion', error); });
    };
    const schedule = () => {
      timer = window.setTimeout(() => {
        if ('requestIdleCallback' in window) idle = window.requestIdleCallback(start, { timeout: 1500 });
        else start();
      }, 1000);
    };
    if (document.readyState === 'complete') schedule();
    else window.addEventListener('load', schedule, { once: true });

    return () => {
      cancelled = true;
      window.removeEventListener('load', schedule);
      if (timer !== undefined) window.clearTimeout(timer);
      if (idle !== undefined && 'cancelIdleCallback' in window) window.cancelIdleCallback(idle);
      dispose?.();
      css?.remove();
    };
  }, []);
  return null;
}
