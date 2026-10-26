import { useEffect, useRef, useState } from 'react';
import { Link, NavLink, useLocation } from 'react-router-dom';
import type { AuthGate } from '@/app/auth';
import type { HealthSnapshot } from '@/app/status';
import { useT } from '@/i18n';
import type { TKey } from '@/i18n';
import { ArmSwitch } from './ArmSwitch';
import { StatusDots } from './StatusDots';
import { BrandMark, Icon } from './Icon';
import type { IconName } from './Icon';
import { Preferences } from './Preferences';

const items: { to: string; key: TKey; icon: IconName }[] = [
  { to: '/', key: 'nav.cockpit', icon: 'overview' },
  { to: '/positions', key: 'nav.positions', icon: 'positions' },
  { to: '/history', key: 'nav.history', icon: 'history' },
  { to: '/settings', key: 'nav.settings', icon: 'settings' },
];
function Navigation({ mobile = false }: { mobile?: boolean }) {
  const t = useT();
  return (
    <nav
      className={mobile ? 'bottom-nav' : 'workspace-nav'}
      aria-label={t('nav.menu')}
    >
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === '/'}
          className={({ isActive }) =>
            isActive ? 'workspace-link is-active' : 'workspace-link'
          }
        >
          <Icon name={item.icon} size={17} />
          <span>{t(item.key)}</span>
        </NavLink>
      ))}
    </nav>
  );
}
export function AppHeader({
  snapshot,
  auth,
}: {
  snapshot: HealthSnapshot;
  auth: AuthGate;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const location = useLocation();
  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);
  useEffect(() => {
    const node = dialog.current;
    if (!node) return;
    if (open && !node.open) node.showModal();
    if (!open && node.open) node.close();
  }, [open]);
  return (
    <>
      <header className="workspace-header">
        <div className="workspace-header__row">
          <Link to="/" className="brand">
            <BrandMark />
            <span>
              LV <span className="brand-secondary">Copytrader</span>
            </span>
          </Link>
          <span className="workspace-divider" aria-hidden="true" />
          <span className="workspace-label">{t('workspace.label')}</span>
          <div className="workspace-header__spacer" />
          <div className="workspace-status">
            <StatusDots
              compact
              status={snapshot.health}
              loading={snapshot.loading}
              lastCheckAt={snapshot.lastCheckAt}
            />
          </div>
          <ArmSwitch
            compact
            armed={snapshot.armed}
            disabled={snapshot.health === null}
            onArmedChange={snapshot.refresh}
          />
          <div className="workspace-preferences">
            <Preferences />
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={() => setOpen(true)}
            aria-label={t('nav.toggle_menu')}
            aria-haspopup="dialog"
            aria-expanded={open}
          >
            <Icon name="menu" />
          </button>
        </div>
        <Navigation />
      </header>
      {snapshot.failed && (
        <div className="connection-banner" role="status">
          <span className="dot dot--warn">{t('app.request_failed')}</span>
          <button className="btn btn--ghost btn--sm" onClick={snapshot.refresh}>
            {t('app.retry')}
          </button>
        </div>
      )}
      <dialog
        ref={dialog}
        className="workspace-menu"
        aria-labelledby="workspace-menu-title"
        onCancel={() => setOpen(false)}
        onClose={() => setOpen(false)}
        onClick={(e) => {
          if (e.target === dialog.current) {
            const r = dialog.current!.getBoundingClientRect();
            if (
              e.clientX < r.left ||
              e.clientX > r.right ||
              e.clientY < r.top ||
              e.clientY > r.bottom
            )
              setOpen(false);
          }
        }}
      >
        <div className="workspace-menu__head">
          <h2 id="workspace-menu-title">{t('nav.drawer')}</h2>
          <button
            className="icon-button"
            aria-label={t('app.close')}
            onClick={() => setOpen(false)}
          >
            <Icon name="close" />
          </button>
        </div>
        {auth.username && <p className="field__hint">{auth.username}</p>}
        <Preferences />
        <Link to="/waitlist" className="workspace-menu__link">
          {t('waitlist.link')}
          <Icon name="arrow" size={16} />
        </Link>
        {auth.required && (
          <button
            className="workspace-menu__link"
            disabled={auth.signingOut}
            onClick={auth.signOut}
          >
            <span>
              {t(auth.signingOut ? 'auth.signing_out' : 'auth.sign_out')}
            </span>
            <Icon name="logout" size={16} />
          </button>
        )}
        {auth.signOutError && (
          <p className="field__error" role="alert">
            {auth.signOutError}
          </p>
        )}
      </dialog>
    </>
  );
}
export function AppBottomNav() {
  return <Navigation mobile />;
}
