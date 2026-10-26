/**
 * LV Copytrader — theme.ts: the dark/light preference, and nothing else.
 *
 * This lived inside App.tsx until the login page needed the same toggle. It is a
 * module rather than a second copy because two implementations of "which theme is
 * on the page" drift, and the one that drifts is always the one the operator sees
 * first.
 *
 * The rules it implements, in the order the pre-paint script in index.html
 * implements them (BUILD_PROMPT §10):
 *
 *   1. `html[data-theme]` — already stamped before first paint. It WINS, because
 *      reading back what is on screen is the only way React's first render is
 *      guaranteed to agree with the pixels already there.
 *   2. `localStorage['sp-theme']` — the operator's explicit choice.
 *   3. `prefers-color-scheme`, defaulting to dark: this is an instrument panel.
 *
 * Nothing here bootstraps the theme. The inline script did that; these helpers
 * only read it back and write the operator's later choices.
 */
import { useCallback, useEffect, useState } from 'react';

/** The two themes tokens.css defines. There is no third. */
export type Theme = 'dark' | 'light';

/** The key the pre-paint bootstrap in index.html reads and writes. */
export const THEME_STORAGE_KEY = 'sp-theme';

export function normalizeTheme(value: unknown): Theme | null {
  if (typeof value !== 'string') return null;
  const v = value.trim().toLowerCase();
  return v === 'dark' || v === 'light' ? v : null;
}

/** The OS preference, defaulting to dark: this is an instrument panel. */
export function prefersDark(): boolean {
  try {
    return typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches;
  } catch {
    return true;
  }
}

/** The stored choice, or null when the operator has never picked one. */
export function storedTheme(): Theme | null {
  try {
    if (typeof localStorage === 'undefined') return null;
    return normalizeTheme(localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    // localStorage throws outright in some privacy modes (BUILD_PROMPT §10).
    return null;
  }
}

/**
 * The theme that is ALREADY on the page.
 *
 * `html[data-theme]` is read first and wins, because the pre-paint script already
 * resolved storage and the media query and painted with the answer.
 */
export function detectTheme(): Theme {
  try {
    if (typeof document !== 'undefined') {
      const stamped = normalizeTheme(document.documentElement.getAttribute('data-theme'));
      if (stamped) return stamped;
    }
  } catch {
    // A detached document in a test environment: fall through to the preferences.
  }
  return storedTheme() ?? (prefersDark() ? 'dark' : 'light');
}

/** Stamp the document without persisting: used when the OS scheme changes. */
export function stampTheme(theme: Theme): void {
  try {
    if (typeof document !== 'undefined') document.documentElement.dataset.theme = theme;
  } catch {
    // Nothing to stamp; the next reload re-runs the bootstrap.
  }
}

/** The operator's explicit choice: stamp the document AND persist it. */
export function applyTheme(theme: Theme): void {
  stampTheme(theme);
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Not persistable here; the session still switches.
  }
}

/**
 * The theme toggle's state.
 *
 * Four things can change it: this tab's toggle, another control in the same tab
 * (the settings page has one, the login page has one), another tab's toggle (the
 * `storage` event), and the OS — the last one only while the operator has made no
 * explicit choice, which is the behaviour the pre-paint script implements too.
 *
 * `storage` does NOT fire in the tab that wrote the key, so it cannot carry a
 * sibling control's choice across. `html[data-theme]` is the one thing every
 * writer stamps, so this watches the attribute.
 */
export function useTheme(): { theme: Theme; setTheme: (theme: Theme) => void } {
  const [theme, setThemeState] = useState<Theme>(() => detectTheme());

  const setTheme = useCallback((next: Theme): void => {
    applyTheme(next);
    setThemeState(next);
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const onStorage = (event: StorageEvent): void => {
      if (event.key !== THEME_STORAGE_KEY) return;
      const next = normalizeTheme(event.newValue);
      if (!next) return;
      stampTheme(next);
      setThemeState(next);
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  useEffect(() => {
    if (typeof document === 'undefined' || typeof MutationObserver === 'undefined') {
      return undefined;
    }
    const root = document.documentElement;
    const observer = new MutationObserver(() => {
      const stamped = normalizeTheme(root.getAttribute('data-theme'));
      if (stamped) setThemeState(stamped);
    });
    observer.observe(root, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let query: MediaQueryList;
    try {
      if (typeof matchMedia !== 'function') return undefined;
      query = matchMedia('(prefers-color-scheme: dark)');
    } catch {
      return undefined;
    }
    const onChange = (event: MediaQueryListEvent): void => {
      // An explicit choice outranks the OS, exactly as the bootstrap orders them.
      if (storedTheme() !== null) return;
      const next: Theme = event.matches ? 'dark' : 'light';
      stampTheme(next);
      setThemeState(next);
    };
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  return { theme, setTheme };
}

/**
 * True when the operator has asked the OS for less motion.
 *
 * CSS handles the animations themselves (base.css neutralises every one of them);
 * this exists for the handful of places where JavaScript holds a timer that only
 * makes sense if something is animating — the sign-in hand-off being the one.
 */
export function prefersReducedMotion(): boolean {
  try {
    return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}
