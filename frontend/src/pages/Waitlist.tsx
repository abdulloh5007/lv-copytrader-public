import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { Icon } from '@/components/Icon';
import { PublicLayout } from '@/components/PublicLayout';
import { joinWaitlist, SignupError } from '@/features/waitlist/api';
import { useT } from '@/i18n';

export default function Waitlist() {
  const t = useT();
  const id = useId();
  const [email, setEmail] = useState('');
  const [consent, setConsent] = useState(false);
  const [phase, setPhase] = useState<'idle' | 'busy' | 'success'>('idle');
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const success = useRef<HTMLHeadingElement>(null);
  useEffect(
    () => () => {
      controller.current?.abort();
      controller.current = null;
    },
    [],
  );
  useEffect(() => {
    if (phase === 'success') success.current?.focus();
  }, [phase]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy.current || phase === 'success') return;
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    busy.current = true;
    setPhase('busy');
    setError(null);
    const abort = new AbortController();
    controller.current = abort;
    const timeout = setTimeout(() => abort.abort(), 15_000);
    try {
      await joinWaitlist(
        email.trim(),
        consent,
        String(new FormData(form).get('website') || ''),
        abort.signal,
      );
      setPhase('success');
      setEmail('');
    } catch (cause) {
      if (abort.signal.aborted && controller.current !== abort) return;
      setPhase('idle');
      setError(
        cause instanceof SignupError && cause.status === 429
          ? t('waitlist.limited', { seconds: cause.retryAfter })
          : cause instanceof SignupError && cause.status === 422
            ? t('waitlist.invalid')
            : t('waitlist.error'),
      );
    } finally {
      clearTimeout(timeout);
      busy.current = false;
    }
  }
  return (
    <PublicLayout>
      <section className="waitlist" aria-labelledby={`${id}-title`}>
        <div className="release-tag">
          <span className="release-tag__dot" />
          {t('waitlist.eyebrow')}
        </div>
        {phase === 'success' ? (
          <div className="waitlist__success" role="status">
            <div className="success-mark">
              <Icon name="check" size={28} />
            </div>
            <h1
              ref={success}
              tabIndex={-1}
              className="hero-title"
              id={`${id}-title`}
            >
              {t('waitlist.success')}
            </h1>
            <p className="hero-description">{t('waitlist.success_note')}</p>
          </div>
        ) : (
          <>
            <h1 className="hero-title" id={`${id}-title`}>
              {t('waitlist.title')}
              <br />
              <span>{t('waitlist.title_dim')}</span>
            </h1>
            <p className="hero-description">{t('waitlist.description')}</p>
            <form
              className="waitlist__form"
              onSubmit={submit}
              aria-busy={phase === 'busy'}
            >
              <label className="sr-only" htmlFor={`${id}-email`}>
                {t('waitlist.email')}
              </label>
              <div className="email-form">
                <input
                  id={`${id}-email`}
                  type="email"
                  name="email"
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    setError(null);
                  }}
                  placeholder="you@example.com"
                  autoComplete="email"
                  autoCapitalize="none"
                  spellCheck={false}
                  maxLength={254}
                  required
                  disabled={phase === 'busy'}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? `${id}-error` : `${id}-consent`}
                />
                <button
                  className="btn btn--primary"
                  disabled={phase === 'busy'}
                  type="submit"
                >
                  {t(
                    phase === 'busy'
                      ? 'waitlist.submitting'
                      : 'waitlist.submit',
                  )}
                  <Icon name="arrow" size={16} />
                </button>
              </div>
              <div className="honeypot" aria-hidden="true">
                <label htmlFor={`${id}-website`}>Website</label>
                <input
                  id={`${id}-website`}
                  name="website"
                  type="text"
                  tabIndex={-1}
                  autoComplete="off"
                />
              </div>
              <label className="consent" id={`${id}-consent`}>
                <input
                  type="checkbox"
                  name="consent"
                  checked={consent}
                  onChange={(e) => setConsent(e.target.checked)}
                  required
                  disabled={phase === 'busy'}
                />
                <span>
                  {t('waitlist.consent')}{' '}
                  <Link to="/privacy">{t('waitlist.privacy_link')}</Link>
                </span>
              </label>
              {error && (
                <p id={`${id}-error`} className="field__error" role="alert">
                  {error}
                </p>
              )}
            </form>
            <div
              className="waitlist__pipeline"
              aria-label={t('waitlist.pipeline_label')}
            >
              <span>Telegram</span>
              <span className="pipeline-line" aria-hidden="true" />
              <span className="pipeline-core">LV</span>
              <span className="pipeline-line" aria-hidden="true" />
              <span>MetaTrader 5</span>
            </div>
          </>
        )}
        <Link className="waitlist__demo-link" to="/demo">
          {t('demo.open_demo')}
          <Icon name="arrow" size={14} />
        </Link>
      </section>
    </PublicLayout>
  );
}
