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
      <header className="public-header">
        <Link to={demo ? '/demo' : '/waitlist'} className="brand">
          <BrandMark />
          <span>
            LV <span className="brand-secondary">Copytrader</span>
          </span>
        </Link>
        <div className="public-header__actions">
          <Preferences />
          <Link
            className="btn btn--sm"
            to={login || demo ? '/waitlist' : '/login'}
          >
            {t(login || demo ? 'waitlist.link' : 'login.submit')}
          </Link>
        </div>
      </header>
      <main className="public-main" id="public-main">
        {children}
      </main>
      <footer className="public-footer">
        <span>© {new Date().getFullYear()} LV Copytrader</span>
        <Link to="/privacy">{t('waitlist.privacy')}</Link>
      </footer>
    </div>
  );
}
