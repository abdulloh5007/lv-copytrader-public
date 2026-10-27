import { useEffect } from 'react';

export function PublicPet() {
  useEffect(() => {
    let dispose: (() => void) | undefined;
    let cancelled = false;
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/assets/showcase/lumi.css?v=public-pet-4';
    document.head.append(css);
    const moduleUrl = new URL('/assets/showcase/lumi.js?v=public-pet-4', window.location.origin).href;
    void import(/* @vite-ignore */ moduleUrl).then((module: { mountPet: () => () => void }) => {
      if (!cancelled) dispose = module.mountPet();
    }).catch((error: unknown) => { console.error('[LV-1] Failed to load the companion', error); });
    return () => { cancelled = true; dispose?.(); css.remove(); };
  }, []);
  return null;
}

