import { LOCALES, LOCALE_LABELS, useLocale, useT } from '@/i18n';
import type { Locale } from '@/i18n';
import { useTheme } from '@/theme';
import { Icon } from './Icon';

export function Preferences() {
  const { locale, setLocale } = useLocale();
  const { theme, setTheme } = useTheme();
  const t = useT();
  return (
    <div className="preferences">
      <label className="icon-button locale-button locale-picker" title={t('app.language_toggle')}>
        <span aria-hidden="true">{locale.toUpperCase()}<span className="locale-picker__arrow">⌄</span></span>
        <select aria-label={t('app.language')} value={locale}
          onChange={(event) => setLocale(event.target.value as Locale)}>
          {LOCALES.map((value) => <option key={value} value={value}>{LOCALE_LABELS[value]}</option>)}
        </select>
      </label>
      <button
        className="icon-button"
        type="button"
        title={t('app.theme_toggle')}
        aria-label={`${t('app.theme_toggle')}: ${theme === 'dark' ? t('app.theme_light') : t('app.theme_dark')}`}
        onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
      >
        <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
      </button>
    </div>
  );
}
