import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { Icon } from '@/components/Icon';
import { PublicLayout } from '@/components/PublicLayout';
import { joinWaitlist, SignupError } from '@/features/waitlist/api';
import { useT, useLocale } from '@/i18n';

export default function Waitlist() {
  const t = useT();
  const { locale } = useLocale();
  const id = useId();
  const [email, setEmail] = useState('');
  const [consent, setConsent] = useState(false);
  const [phase, setPhase] = useState<'idle' | 'busy' | 'success'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [invalidEmail, setInvalidEmail] = useState(false);
  const errorMessage = useRef<HTMLParagraphElement>(null);
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
  useEffect(() => {
    if (error) errorMessage.current?.focus();
  }, [error]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy.current || phase === 'success') return;
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    busy.current = true;
    setPhase('busy');
    setError(null);
    setInvalidEmail(false);
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
      setInvalidEmail(cause instanceof SignupError && cause.status === 422);
      setError(
        abort.signal.aborted
          ? t('waitlist.timeout')
          : cause instanceof SignupError && cause.status === 429
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
    <PublicLayout star>
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
            <div className="waitlist__explore">
              <Link className="waitlist__text-link" to="/demo">
                {t('demo.open_demo')}<Icon name="arrow" size={16} />
              </Link>
            </div>
            <form
              className="waitlist__form"
              onSubmit={submit}
              aria-busy={phase === 'busy'}
              aria-label={t('waitlist.form_title')}
            >
              <label className="waitlist__email-label sr-only" htmlFor={`${id}-email`}>
                {t('waitlist.email')}
              </label>
              <div className="email-form">
                <input
                  id={`${id}-email`}
                  type="email"
                  inputMode="email"
                  name="email"
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    setError(null);
                    setInvalidEmail(false);
                  }}
                  placeholder="you@example.com"
                  autoComplete="email"
                  autoCapitalize="none"
                  spellCheck={false}
                  maxLength={254}
                  required
                  disabled={phase === 'busy'}
                  aria-invalid={invalidEmail || undefined}
                  aria-describedby={error ? `${id}-error` : undefined}
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
                  <Link to="/privacy" target="_blank" rel="noopener noreferrer">{t('waitlist.privacy_link')}</Link>
                </span>
              </label>
              {error && (
                <p ref={errorMessage} tabIndex={-1} id={`${id}-error`} className="field__error" role="alert">
                  {error}
                </p>
              )}
            </form>
            <a className="waitlist__story" href={`/assets/showcase/${locale}/index.html`}>
              <img
                className="waitlist__story-preview"
                src="/assets/previews/story.webp"
                alt={t('waitlist.story_preview')}
                width="1200"
                height="760"
                loading="lazy"
                decoding="async"
              />
              <span className="waitlist__story-action waitlist__text-link">
                {t('waitlist.story')}<Icon name="arrow" size={16} />
              </span>
            </a>
            <div className="waitlist__details">
              {(['hosting', 'control', 'open'] as const).map((feature) => (
                <div key={feature}>
                  <h2>{t(`waitlist.${feature}_title`)}</h2>
                  <p>{t(`waitlist.${feature}_note`)}</p>
                </div>
              ))}
            </div>
            <p className="waitlist__risk">{t('waitlist.risk')}</p>
          </>
        )}
        {phase === 'success' && (
          <Link className="waitlist__demo-link" to="/demo">
            {t('demo.open_demo')}<Icon name="arrow" size={14} />
          </Link>
        )}
      </section>
    </PublicLayout>
  );
}
