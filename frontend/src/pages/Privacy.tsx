import { PublicLayout } from '@/components/PublicLayout';
import { useT } from '@/i18n';
export default function Privacy() {
  const t = useT();
  return (
    <PublicLayout>
      <article className="privacy-page">
        <p className="eyebrow">LV Copytrader</p>
        <h1>{t('waitlist.privacy')}</h1>
        <p>{t('privacy.collection')}</p>
        <p>{t('privacy.storage')}</p>
        <p>{t('privacy.demo')}</p>
        <p>{t('privacy.retention')}</p>
        <p className="field__hint">{t('privacy.version')}</p>
      </article>
    </PublicLayout>
  );
}
