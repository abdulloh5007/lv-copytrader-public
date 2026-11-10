import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useLocale, useT } from '@/i18n';
import { BrandMark } from './Icon';
import { Preferences } from './Preferences';


export function PublicLayout({
  children,
  login = false,
  demo = false,
  star = false,
  docs = false,
}: {
  children: ReactNode;
  login?: boolean;
  demo?: boolean;
  star?: boolean;
  docs?: boolean;
}) {
  const t = useT();
  const { locale } = useLocale();
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
          {login && <Link
            className="btn btn--sm"
            to="/waitlist"
          >
            {t('waitlist.link')}
          </Link>}
        </div>
      </header>
      <main className="public-main" id="public-main" tabIndex={-1}>
        {children}
      </main>
      {(star || demo) && <div data-public-star />}
      <footer className="public-footer">
        <span>© {new Date().getFullYear()} LV Copytrader</span>
        <nav className="public-footer__links" aria-label={t('public.footer_navigation')}>
          {docs && <a href={`https://docs.lvenc.site/${locale}/`}>{t('waitlist.docs')}</a>}
          <Link to="/privacy">{t('waitlist.privacy')}</Link>
        </nav>
      </footer>
    </div>
  );
}
