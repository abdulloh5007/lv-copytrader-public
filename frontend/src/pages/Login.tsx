import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { isApiError, login, retryAfterSeconds } from '@/api/client';
import { Icon, BrandMark } from '@/components/Icon';
import { PublicLayout } from '@/components/PublicLayout';
import { useT } from '@/i18n';

export interface LoginProps {
  onAuthenticated: () => void;
}
export function Login({ onAuthenticated }: LoginProps) {
  const t = useT();
  const id = useId();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lockedUntil, setLockedUntil] = useState(0);
  const [now, setNow] = useState(Date.now());
  const abortRef = useRef<AbortController | null>(null);
  const busyRef = useRef(false);
  const passwordRef = useRef<HTMLInputElement>(null);
  useEffect(
    () => () => {
      abortRef.current?.abort();
      abortRef.current = null;
    },
    [],
  );
  useEffect(() => {
    if (!lockedUntil) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [lockedUntil]);
  const remaining = Math.max(0, Math.ceil((lockedUntil - now) / 1000));
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busyRef.current || remaining) return;
    if (!event.currentTarget.reportValidity()) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    const controller = new AbortController();
    abortRef.current = controller;
    const timeout = setTimeout(() => controller.abort(), 15_000);
    try {
      await login(
        { username: username.trim(), password },
        { signal: controller.signal },
      );
      if (abortRef.current !== controller) return;
      setPassword('');
      onAuthenticated();
    } catch (cause) {
      if (abortRef.current !== controller) return;
      if (isApiError(cause) && cause.status === 429) {
        setLockedUntil(Date.now() + (retryAfterSeconds(cause) ?? 60) * 1000);
        setNow(Date.now());
      } else if (
        isApiError(cause) &&
        (cause.status === 401 || cause.status === 403)
      ) {
        setError(t('login.rejected'));
        setPassword('');
        passwordRef.current?.focus();
      } else {
        setError(t('login.unreachable'));
      }
    } finally {
      clearTimeout(timeout);
      busyRef.current = false;
      setBusy(false);
    }
  }
  return (
    <PublicLayout login>
      <section className="auth-card" aria-labelledby={`${id}-title`}>
        <div className="auth-card__intro">
          <BrandMark />
          <h1 id={`${id}-title`}>{t('login.heading')}</h1>
          <p>{t('login.subheading')}</p>
        </div>
        <form className="auth-form" onSubmit={submit} aria-busy={busy}>
          <div className="field">
            <label className="field__label" htmlFor={`${id}-user`}>
              {t('login.username')}
            </label>
            <input
              id={`${id}-user`}
              name="username"
              value={username}
              onChange={(e) => {
                setUsername(e.target.value);
                setError(null);
              }}
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              maxLength={200}
              required
              disabled={busy}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor={`${id}-password`}>
              {t('login.password')}
            </label>
            <div className="password-field">
              <input
                id={`${id}-password`}
                ref={passwordRef}
                name="password"
                type={visible ? 'text' : 'password'}
                value={password}
                onChange={(e) => {
                  setPassword(e.target.value);
                  setError(null);
                }}
                autoComplete="current-password"
                maxLength={1024}
                required
                disabled={busy}
                aria-invalid={error ? true : undefined}
                aria-describedby={error ? `${id}-error` : undefined}
              />
              <button
                type="button"
                className="icon-button"
                aria-label={t(
                  visible ? 'login.hide_password' : 'login.show_password',
                )}
                aria-pressed={visible}
                aria-controls={`${id}-password`}
                onClick={() => setVisible((v) => !v)}
              >
                <Icon name={visible ? 'eye-off' : 'eye'} />
              </button>
            </div>
          </div>
          {error && (
            <p id={`${id}-error`} className="field__error" role="alert">
              {error}
            </p>
          )}
          {remaining > 0 && (
            <p className="field__hint" role="status">
              {t('login.locked', { time: `${remaining}s` })}
            </p>
          )}
          <button
            className="btn btn--primary btn--block"
            type="submit"
            disabled={busy || remaining > 0}
          >
            {t(busy ? 'login.submitting' : 'login.submit')}
            <Icon name="arrow" size={16} />
          </button>
        </form>
        <Link className="auth-card__waitlist" to="/waitlist">
          {t('waitlist.login_link')}
          <Icon name="arrow" size={14} />
        </Link>
      </section>
    </PublicLayout>
  );
}
export default Login;
