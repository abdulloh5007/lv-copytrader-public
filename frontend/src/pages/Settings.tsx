import type { SettingsOut } from '@/api/client';
import { getSettings, isApiError } from '@/api/client';
import { GeneralSection } from '@/features/settings/GeneralSection';
import { LimitsSection } from '@/features/settings/LimitsSection';
import { Mt5Section } from '@/features/settings/Mt5Section';
import { NotifierSection } from '@/features/settings/NotifierSection';
import { ProviderSection } from '@/features/settings/ProviderSection';
import {
  errorSentence,
  NOTE,
  PAGE,
  PAGE_TITLE,
  SectionCommonProps,
  SETTINGS_TABS,
  SettingsTabId,
  tabFromHash,
} from '@/features/settings/shared';
import { TelegramSection } from '@/features/settings/TelegramSection';
import { useT } from '@/i18n';
import type { KeyboardEvent, ReactElement } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
export {
  applyTheme,
  readTheme,
  THEME_STORAGE_KEY,
} from '@/features/settings/shared';
export type { Theme } from '@/features/settings/shared';
export function Settings(): ReactElement {
  const t = useT();
  const location = useLocation();
  const navigate = useNavigate();
  const tab = tabFromHash(location.hash);

  const selectTab = useCallback(
    (id: SettingsTabId): void => {
      navigate(
        { pathname: '/settings', search: location.search, hash: id },
        { replace: true },
      );
    },
    [location.search, navigate],
  );

  useEffect(() => {
    const node = document.getElementById(`sp-set-tab-${tab}`);
    if (node instanceof HTMLElement) {
      node.scrollIntoView({ inline: 'center', block: 'nearest' });
    }
  }, [tab]);

  const onTabKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>, index: number): void => {
      const last = SETTINGS_TABS.length - 1;
      let next = index;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        next = index === last ? 0 : index + 1;
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        next = index === 0 ? last : index - 1;
      } else if (event.key === 'Home') {
        next = 0;
      } else if (event.key === 'End') {
        next = last;
      } else {
        return;
      }
      event.preventDefault();
      const id = SETTINGS_TABS[next].id;
      selectTab(id);
      const node = document.getElementById(`sp-set-tab-${id}`);
      if (node instanceof HTMLElement) node.focus();
    },
    [selectTab],
  );

  const [settings, setSettings] = useState<SettingsOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Stable across a locale switch, so changing the language does not abort and
  // re-issue the settings fetch that the mount effect owns.
  const tRef = useRef(t);
  tRef.current = t;

  const load = useCallback((signal?: AbortSignal) => {
    setLoading(true);
    setLoadError(null);
    getSettings(signal ? { signal } : undefined)
      .then((result) => {
        setSettings(result);
        setLoading(false);
      })
      .catch((cause: unknown) => {
        if (isApiError(cause) && cause.isAborted) return;
        setLoadError(errorSentence(cause, tRef.current));
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const reload = useCallback(async (): Promise<void> => {
    const next = await getSettings();
    setSettings(next);
  }, []);

  const onSettings = useCallback((next: SettingsOut) => setSettings(next), []);

  if (loading && !settings) {
    return (
      <div className="page" style={PAGE}>
        <h1 style={PAGE_TITLE}>{t('settings.title')}</h1>
        <p style={NOTE}>{t('app.loading')}</p>
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="page" style={PAGE}>
        <h1 style={PAGE_TITLE}>{t('settings.title')}</h1>
        <div className="empty">
          <p className="empty__title">{t('app.error')}</p>
          {loadError ? <p className="empty__text">{loadError}</p> : null}
          <button type="button" className="btn" onClick={() => load()}>
            {t('app.retry')}
          </button>
        </div>
      </div>
    );
  }

  // The re-entry banner is the shell's job (App.tsx renders it on every route,
  // with each key named); here the affected field carries the state itself.
  const common: SectionCommonProps = { settings, onSettings, reload };

  return (
    <div className="page" style={PAGE}>
      <div className="row row--wrap">
        <h1 style={PAGE_TITLE}>{t('settings.title')}</h1>
        <span className="spacer" />
        <button type="button" className="btn btn--sm" onClick={() => load()}>
          {t('app.refresh')}
        </button>
      </div>

      {loadError ? (
        <p className="field__error" role="alert">
          {loadError}
        </p>
      ) : null}

      <div
        className="tabs settings-tabs"
        role="tablist"
        aria-label={t('settings.title')}
      >
        {SETTINGS_TABS.map((item, index) => {
          const selected = tab === item.id;
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              id={`sp-set-tab-${item.id}`}
              className={selected ? 'tab is-active' : 'tab'}
              aria-selected={selected}
              aria-controls={`sp-set-panel-${item.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => selectTab(item.id)}
              onKeyDown={(event) => onTabKeyDown(event, index)}
            >
              {t(item.tabKey)}
            </button>
          );
        })}
      </div>

      <div
        className="settings-panel"
        role="tabpanel"
        id="sp-set-panel-telegram"
        aria-labelledby="sp-set-tab-telegram"
        hidden={tab !== 'telegram'}
      >
        <TelegramSection {...common} />
      </div>
      <div
        className="settings-panel"
        role="tabpanel"
        id="sp-set-panel-mt5"
        aria-labelledby="sp-set-tab-mt5"
        hidden={tab !== 'mt5'}
      >
        <Mt5Section {...common} />
      </div>
      <div
        className="settings-panel"
        role="tabpanel"
        id="sp-set-panel-provider"
        aria-labelledby="sp-set-tab-provider"
        hidden={tab !== 'provider'}
      >
        <ProviderSection {...common} />
      </div>
      <div
        className="settings-panel"
        role="tabpanel"
        id="sp-set-panel-notifier"
        aria-labelledby="sp-set-tab-notifier"
        hidden={tab !== 'notifier'}
      >
        <NotifierSection {...common} />
      </div>
      <div
        className="settings-panel"
        role="tabpanel"
        id="sp-set-panel-limits"
        aria-labelledby="sp-set-tab-limits"
        hidden={tab !== 'limits'}
      >
        <LimitsSection {...common} />
      </div>
      <div
        className="settings-panel"
        role="tabpanel"
        id="sp-set-panel-general"
        aria-labelledby="sp-set-tab-general"
        hidden={tab !== 'general'}
      >
        <GeneralSection {...common} />
      </div>
    </div>
  );
}

export const SettingsPage = Settings;

export default Settings;
