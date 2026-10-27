import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useT } from '@/i18n';
import { BrandMark } from './Icon';
import { Preferences } from './Preferences';

export function PublicLayout({
  children,
  login = false,
  demo = false,
}: {
  children: ReactNode;
  login?: boolean;
  demo?: boolean;
}) {
  const t = useT();
  return (
    <div
      className={demo ? 'public-layout public-layout--demo' : 'public-layout'}
    >
      <a className="public-skip" href="#public-main">{t('public.skip')}</a>
      <header className="public-header">
        <Link to={demo ? '/demo' : '/waitlist'} className="brand" aria-label="LV Copy Trader">
          <BrandMark variant="logo" />
        </Link>
        <div className="public-header__actions">
          <Preferences />
          <Link
            className="btn btn--sm"
            to={login || demo ? '/waitlist' : '/login'}
          >
            {t(login || demo ? 'waitlist.link' : 'public.operator_login')}
          </Link>
        </div>
      </header>
      <main className="public-main" id="public-main" tabIndex={-1}>
        {children}
      </main>
      <footer className="public-footer">
        <span>© {new Date().getFullYear()} LV Copytrader</span>
        <Link to="/privacy">{t('waitlist.privacy')}</Link>
      </footer>
    </div>
  );
}
