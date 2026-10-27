import type { ErrorInfo, ReactElement, ReactNode } from 'react';
import { Component, lazy, Suspense, useCallback, useMemo } from 'react';
import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AppBottomNav, AppHeader } from './components/AppHeader';
import { PublicPet } from './components/PublicPet';
import type { TKey, Translate } from './i18n';
import { useT } from './i18n';

import Login from './pages/Login';

import type { AuthGate } from './app/auth';
import { useAuthGate } from './app/auth';
import { AppStatusContext, HEALTH_POLL_MS, useHealthPoll } from './app/status';
import './styles/app.css';
export type { AuthGate } from './app/auth';
export { AppStatusContext, useAppStatus } from './app/status';
export type { HealthSnapshot } from './app/status';
export { applyTheme, detectTheme, THEME_STORAGE_KEY } from './theme';

const Cockpit = lazy(() => import('./pages/Cockpit'));
const History = lazy(() => import('./pages/History'));
const Positions = lazy(() => import('./pages/Positions'));
const Settings = lazy(() => import('./pages/Settings'));
const Waitlist = lazy(() => import('./pages/Waitlist'));
const Privacy = lazy(() => import('./pages/Privacy'));
const Demo = lazy(() => import('./pages/Demo'));

export type { Theme } from './theme';

const MAIN_ID = 'sp-main';

AppStatusContext.displayName = 'AppStatusContext';

const REENTRY_LABELS: Record<string, readonly [TKey, TKey] | undefined> = {
  'mt5.password': ['settings.mt5_section', 'settings.password'],
  'tg.api_hash': ['settings.telegram_section', 'settings.api_hash'],
  'telegram.api_hash': ['settings.telegram_section', 'settings.api_hash'],
  'tg.session': ['settings.telegram_section', 'settings.sign_in'],
  'telegram.session': ['settings.telegram_section', 'settings.sign_in'],
  'provider.api_key': ['settings.provider_section', 'settings.api_key'],
  'notifier.bot_token': ['settings.notifier_section', 'settings.bot_token'],
};

function reentryLabel(key: string, t: Translate): string {
  const pair = REENTRY_LABELS[key];
  if (!pair) return key;
  return `${t(pair[0])} → ${t(pair[1])}`;
}

function reentryHref(keys: readonly string[]): string {
  const first = keys[0] ?? '';
  if (first.startsWith('mt5')) return '/settings#mt5';
  if (first.startsWith('tg.') || first.startsWith('telegram'))
    return '/settings#telegram';
  if (first.startsWith('provider')) return '/settings#provider';
  if (first.startsWith('notifier')) return '/settings#notifier';
  return '/settings';
}

function SkipLink(): ReactElement {
  const t = useT();
  return (
    <a className="sp-app__skip" href={`#${MAIN_ID}`}>
      {t('nav.skip_to_content')}
    </a>
  );
}

function ReentryBanner(props: {
  keys: readonly string[];
}): ReactElement | null {
  const t = useT();
  const names = useMemo(
    () => props.keys.map((key) => reentryLabel(key, t)).join(', '),
    [props.keys, t],
  );

  if (props.keys.length === 0) return null;

  return (
    <div className="sp-app__banner" role="status">
      <span className="dot dot--warn sp-app__banner-dot" aria-hidden="true" />
      <p className="sp-app__banner-text">
        {t('status.needs_reentry_banner', { keys: names })}
      </p>
      <Link
        className="btn btn--sm sp-app__banner-action"
        to={reentryHref(props.keys)}
      >
        {t('status.needs_reentry_action')}
      </Link>
    </div>
  );
}

interface BoundaryProps {
  children: ReactNode;
  fallback: (error: Error, reset: () => void) => ReactNode;
}

interface BoundaryState {
  error: Error | null;
}

class RouteErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  constructor(props: BoundaryProps) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: unknown): BoundaryState {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  override componentDidCatch(error: unknown, info: ErrorInfo): void {
    // The console is the only place a stack belongs; the page shows the message.
    console.error('[LV Copytrader] page crashed', error, info.componentStack);
  }

  private readonly reset = (): void => {
    this.setState({ error: null });
  };

  override render(): ReactNode {
    const { error } = this.state;
    if (error) return this.props.fallback(error, this.reset);
    return this.props.children;
  }
}

function PageCrash(props: { error: Error; onRetry: () => void }): ReactElement {
  const t = useT();
  return (
    <div className="sp-app__crash">
      <div className="empty">
        <p className="empty__title">{t('app.error')}</p>
        <p className="empty__text mono mono--sm">{props.error.message}</p>
        <div className="sp-app__crash-actions">
          <button
            type="button"
            className="btn btn--primary"
            onClick={props.onRetry}
          >
            {t('app.retry')}
          </button>
          <Link className="btn" to="/">
            {t('app.go_home')}
          </Link>
        </div>
      </div>
    </div>
  );
}

function AppShell(props: { auth: AuthGate }): ReactElement {
  const snapshot = useHealthPoll(HEALTH_POLL_MS);
  const location = useLocation();

  const renderCrash = useCallback(
    (error: Error, reset: () => void): ReactNode => (
      <PageCrash error={error} onRetry={reset} />
    ),
    [],
  );

  return (
    <AppStatusContext.Provider value={snapshot}>
      <div className="sp-app">
        <SkipLink />
        <AppHeader snapshot={snapshot} auth={props.auth} />
        <ReentryBanner keys={snapshot.needsReentry} />

        <main className="sp-app__main" id={MAIN_ID} tabIndex={-1}>
          {/* Keyed by path: navigating away from a crashed page clears the error. */}
          <RouteErrorBoundary key={location.pathname} fallback={renderCrash}>
            <Routes>
              <Route path="/" element={<Cockpit />} />
              <Route path="/positions" element={<Positions />} />
              <Route path="/history" element={<History />} />
              <Route path="/settings" element={<Settings />} />
              {/* Anything else is a typo or a stale bookmark: go to the cockpit. */}
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </RouteErrorBoundary>
        </main>
        <AppBottomNav />
      </div>
    </AppStatusContext.Provider>
  );
}

function ProtectedApp(): ReactElement | null {
  const auth = useAuthGate();
  const location = useLocation();

  if (auth.phase === 'unknown') return null;
  if (auth.phase === 'locked' && location.pathname === '/')
    return <Navigate to="/waitlist" replace />;
  if (auth.phase === 'locked') return <Login onAuthenticated={auth.refresh} />;
  return <AppShell auth={auth} />;
}

export function App(): ReactElement {
  const { pathname } = useLocation();
  const publicPath = pathname.replace(/\/+$/, '');
  const showPet = publicPath === '/waitlist' || publicPath === '/demo';
  return (
    <>
      {showPet && <PublicPet />}
      <Suspense
        fallback={
          <div className="page-loading" role="status">
            <span className="loading-dot" />
            <span>LV Copytrader</span>
          </div>
        }
      >
        <Routes>
          <Route path="/waitlist" element={<Waitlist />} />
          <Route path="/privacy" element={<Privacy />} />
          <Route path="/demo" element={<Demo />} />
          <Route path="*" element={<ProtectedApp />} />
        </Routes>
      </Suspense>
    </>
  );
}
export default App;
