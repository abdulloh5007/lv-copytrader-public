import { useLocale, useT } from '@/i18n';
import { useTheme } from '@/theme';
import { Icon } from './Icon';

export function Preferences() {
  const { locale, setLocale } = useLocale();
  const { theme, setTheme } = useTheme();
  const t = useT();
  return (
    <div className="preferences">
      <button
        className="icon-button locale-button"
        type="button"
        title={t('app.language_toggle')}
        aria-label={t('app.language_toggle')}
        onClick={() => setLocale(locale === 'ru' ? 'en' : 'ru')}
      >
        {locale.toUpperCase()}
      </button>
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
