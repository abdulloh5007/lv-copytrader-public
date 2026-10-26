import type {
  Mt5SettingsInput,
  Mt5SettingsOut,
  Mt5TestOut,
} from '@/api/client';
import { putMt5Settings, testMt5 } from '@/api/client';
import { SecretField } from '@/components/SecretField';
import type { TKey } from '@/i18n';
import { EMPTY_VALUE, formatCurrency, useCurrentLocale, useT } from '@/i18n';
import type { ReactElement } from 'react';
import { useCallback, useEffect, useId, useState } from 'react';
import {
  errorField,
  errorSentence,
  GRID,
  hasError,
  needsReentry,
  NOTE,
  positiveInt,
  requiredText,
  Section,
  SectionCommonProps,
  SUB_BOX,
  TextField,
  toInt,
  useAsyncAction,
} from './shared';
interface Mt5Form {
  server: string;
  login: string;
  password: string | null;
}

type Mt5Errors = Partial<Record<'server' | 'login' | 'password', string>>;

function mt5Form(slice: Mt5SettingsOut): Mt5Form {
  return {
    server: slice.server ?? '',
    login: slice.login === null ? '' : String(slice.login),
    password: null,
  };
}

const MARGIN_MODE_KEYS: Record<string, TKey> = {
  hedging: 'account.hedging',
  netting: 'account.netting',
  exchange: 'account.exchange',
};

export function Mt5Section(props: SectionCommonProps): ReactElement {
  const { settings, onSettings } = props;
  const t = useT();
  const locale = useCurrentLocale();
  const uid = useId();
  const slice = settings.mt5;

  const [form, setForm] = useState<Mt5Form>(() => mt5Form(slice));
  const [dirty, setDirty] = useState(false);
  const [errors, setErrors] = useState<Mt5Errors>({});
  const [result, setResult] = useState<Mt5TestOut | null>(null);

  const save = useAsyncAction(t);
  const test = useAsyncAction(t);

  useEffect(() => {
    if (!dirty) setForm(mt5Form(slice));
  }, [dirty, slice]);

  const patch = useCallback(
    (next: Partial<Mt5Form>) => {
      setForm((previous) => ({ ...previous, ...next }));
      setDirty(true);
      save.clear();
      test.clear();
      setResult(null);
    },
    [save, test],
  );

  const validate = useCallback((): Mt5Errors => {
    const next: Mt5Errors = {};
    const server = requiredText(form.server, t);
    if (server) next.server = server;
    const login = positiveInt(form.login, t, true);
    if (login) next.login = login;
    if (!slice.has_password && !form.password)
      next.password = t('err.required');
    if (form.password !== null && !form.password)
      next.password = t('err.required');
    setErrors(next);
    return next;
  }, [form, slice.has_password, t]);

  const anchor = useCallback(
    (cause: unknown): boolean => {
      const field = errorField(cause);
      if (field === 'server' || field === 'login' || field === 'password') {
        setErrors({ [field]: errorSentence(cause, t) });
        return true;
      }
      return false;
    },
    [t],
  );

  const body = useCallback(
    (): Mt5SettingsInput => ({
      server: form.server.trim(),
      login: toInt(form.login),
      // A secret the operator did not retype is omitted, never echoed back.
      password: form.password ?? undefined,
    }),
    [form],
  );

  const onSave = useCallback(() => {
    if (hasError(validate())) return;
    save.run(async () => {
      const next = await putMt5Settings(body());
      setDirty(false);
      onSettings(next);
    }, anchor);
  }, [anchor, body, onSettings, save, validate]);

  /**
   * `POST /settings/mt5/test` logs in with the STORED credentials, so the form
   * is written first — otherwise the button would test the previous account and
   * report a balance that has nothing to do with what is on screen.
   */
  const onTest = useCallback(() => {
    if (hasError(validate())) return;
    setResult(null);
    test.run(async () => {
      const next = await putMt5Settings(body());
      setDirty(false);
      onSettings(next);
      const outcome = await testMt5();
      if (!outcome.ok) {
        test.fail(outcome.message || t('settings.test_failed'));
        return;
      }
      setResult(outcome);
    }, anchor);
  }, [anchor, body, onSettings, t, test, validate]);

  const marginModeKey = result
    ? MARGIN_MODE_KEYS[result.margin_mode]
    : undefined;

  return (
    <Section
      id="sp-set-mt5"
      title={t('settings.mt5_section')}
      hint={t('settings.mt5_hint')}
      dirty={dirty}
      saving={save.busy}
      saved={save.done}
      error={save.error}
      onSave={onSave}
      actions={
        <button
          type="button"
          className="btn btn--sm"
          disabled={test.busy || save.busy}
          onClick={onTest}
        >
          {test.busy ? t('settings.testing') : t('settings.test_connection')}
        </button>
      }
    >
      <div style={GRID}>
        <TextField
          id={`${uid}-server`}
          label={t('settings.server')}
          value={form.server}
          mono
          disabled={save.busy || test.busy}
          error={errors.server ?? null}
          onChange={(value) => patch({ server: value })}
        />
        <TextField
          id={`${uid}-login`}
          label={t('settings.login')}
          value={form.login}
          mono
          inputMode="numeric"
          disabled={save.busy || test.busy}
          error={errors.login ?? null}
          onChange={(value) => patch({ login: value })}
        />
        <SecretField
          id={`${uid}-password`}
          label={t('settings.password')}
          hasValue={slice.has_password}
          value={form.password}
          disabled={save.busy || test.busy}
          error={errors.password ?? null}
          needsReentry={needsReentry(settings.needs_reentry, 'mt5.password')}
          onChange={(value) => patch({ password: value })}
        />
      </div>

      {test.error ? (
        <p className="field__error" role="alert">
          {t('settings.test_failed')} — {test.error}
        </p>
      ) : null}

      {result ? (
        <div style={SUB_BOX}>
          <div className="row row--wrap">
            <span className="badge badge--ok" role="status">
              {t('settings.test_ok')}
            </span>
            <span>
              {t('settings.mt5_test_result', {
                name: result.name,
                balance: formatCurrency(
                  result.balance,
                  result.currency,
                  locale,
                ),
                server: result.server,
              })}
            </span>
          </div>
          <div className="row row--wrap row--wide">
            <span className="row row--tight">
              <span style={NOTE}>{t('account.login')}</span>
              <span className="mono mono--sm">
                {result.login ?? EMPTY_VALUE}
              </span>
            </span>
            <span className="row row--tight">
              <span style={NOTE}>{t('account.equity')}</span>
              <span className="mono mono--sm">
                {formatCurrency(result.equity, result.currency, locale)}
              </span>
            </span>
            <span className="row row--tight">
              <span style={NOTE}>{t('account.leverage')}</span>
              <span className="mono mono--sm">
                {t('account.leverage_value', { value: result.leverage })}
              </span>
            </span>
            <span className="row row--tight">
              <span style={NOTE}>{t('account.margin_mode')}</span>
              <span>{marginModeKey ? t(marginModeKey) : t('app.unknown')}</span>
            </span>
          </div>
        </div>
      ) : null}
    </Section>
  );
}
