import type {
  DialogOut,
  TelegramSettingsOut,
  TelegramUserOut,
} from '@/api/client';
import {
  isApiError,
  tgCredentials,
  tgDialogs,
  tgLogout,
  tgPassword,
  tgSendCode,
  tgSignIn,
} from '@/api/client';
import { DialogPicker } from '@/components/DialogPicker';
import { SecretField } from '@/components/SecretField';
import { useT } from '@/i18n';
import type { ReactElement } from 'react';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import {
  errorField,
  errorSentence,
  GRID,
  hasError,
  needsReentry,
  OK_NOTE,
  phoneNumber,
  positiveInt,
  requiredText,
  Section,
  SectionCommonProps,
  SUB_BOX,
  TextField,
  toInt,
  useAsyncAction,
} from './shared';
interface TelegramForm {
  apiId: string;
  apiHash: string | null;
  phone: string;
}

type TelegramErrors = Partial<
  Record<'api_id' | 'api_hash' | 'phone' | 'code' | 'password', string>
>;

type LoginStep = 'credentials' | 'code' | 'password';

function telegramForm(slice: TelegramSettingsOut): TelegramForm {
  return {
    apiId: slice.api_id === null ? '' : String(slice.api_id),
    apiHash: null,
    phone: slice.phone ?? '',
  };
}

function accountName(user: TelegramUserOut | null): string {
  if (!user) return '';
  const full = [user.first_name, user.last_name]
    .filter(Boolean)
    .join(' ')
    .trim();
  if (full) return full;
  if (user.username) return `@${user.username}`;
  if (user.phone) return user.phone;
  return String(user.id);
}

export function TelegramSection(props: SectionCommonProps): ReactElement {
  const { settings, reload } = props;
  const t = useT();
  const uid = useId();
  const slice = settings.telegram;

  const [form, setForm] = useState<TelegramForm>(() => telegramForm(slice));
  const [dirty, setDirty] = useState(false);
  const [errors, setErrors] = useState<TelegramErrors>({});
  const [step, setStep] = useState<LoginStep>('credentials');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [codeSent, setCodeSent] = useState(false);

  const [dialogs, setDialogs] = useState<DialogOut[]>([]);
  const [dialogsLoading, setDialogsLoading] = useState(false);
  const [dialogsError, setDialogsError] = useState<string | null>(null);

  const action = useAsyncAction(t);

  // `t` changes identity on every locale switch. Held in a ref, the fetchers
  // below keep a stable identity, so flipping the language does not re-issue a
  // Telegram call (the same pattern ModelPicker uses).
  const tRef = useRef(t);
  tRef.current = t;

  // A save returns the whole tree, so a section that is NOT being edited re-syncs
  // from the new slice; one that is keeps the operator's draft.
  useEffect(() => {
    if (!dirty) setForm(telegramForm(slice));
  }, [dirty, slice]);

  const patch = useCallback(
    (next: Partial<TelegramForm>) => {
      setForm((previous) => ({ ...previous, ...next }));
      setDirty(true);
      action.clear();
    },
    [action],
  );

  const anchor = useCallback(
    (cause: unknown): boolean => {
      const field = errorField(cause);
      if (
        field === 'api_id' ||
        field === 'api_hash' ||
        field === 'phone' ||
        field === 'code' ||
        field === 'password'
      ) {
        setErrors({ [field]: errorSentence(cause, t) });
        if (field === 'password') setStep('password');
        return true;
      }
      return false;
    },
    [t],
  );

  /* ── the dialog list, once the account is authorized ─────────────────── */

  const loadDialogs = useCallback(() => {
    setDialogsLoading(true);
    setDialogsError(null);
    tgDialogs()
      .then((result) => setDialogs(result.dialogs))
      .catch((cause: unknown) => {
        if (isApiError(cause) && cause.isAborted) return;
        setDialogsError(errorSentence(cause, tRef.current));
      })
      .finally(() => setDialogsLoading(false));
  }, []);

  useEffect(() => {
    if (!slice.authorized) {
      setDialogs([]);
      return;
    }
    loadDialogs();
  }, [loadDialogs, slice.authorized]);

  /* ── step 1: store the credentials, ask Telegram for a code ──────────── */

  const sendCode = useCallback(() => {
    const next: TelegramErrors = {};
    const apiId = positiveInt(form.apiId, t, true);
    if (apiId) next.api_id = apiId;
    if (!slice.has_api_hash && !form.apiHash) next.api_hash = t('err.required');
    if (form.apiHash !== null && !form.apiHash.trim())
      next.api_hash = t('err.required');
    const phone = phoneNumber(form.phone, t);
    if (phone) next.phone = phone;
    setErrors(next);
    if (hasError(next)) return;

    action.run(async () => {
      await tgCredentials({
        api_id: toInt(form.apiId),
        api_hash: form.apiHash ?? undefined,
        phone: form.phone.trim(),
      });
      const sent = await tgSendCode();
      setDirty(false);
      setCodeSent(sent.code_sent);
      setStep('code');
      await reload();
    }, anchor);
  }, [action, anchor, form, reload, slice.has_api_hash, t]);

  /* ── step 2: the code, and the 2FA password when the server asks ─────── */

  const signIn = useCallback(() => {
    const message = requiredText(code, t);
    setErrors(message ? { code: message } : {});
    if (message) return;

    action.run(async () => {
      const result = await tgSignIn({ code: code.trim() });
      if (result.needs_password) {
        setStep('password');
        setErrors({});
        return;
      }
      setCode('');
      setStep('credentials');
      await reload();
    }, anchor);
  }, [action, anchor, code, reload, t]);

  const submitPassword = useCallback(() => {
    const message = requiredText(password, t);
    setErrors(message ? { password: message } : {});
    if (message) return;

    action.run(async () => {
      await tgPassword({ password });
      setPassword('');
      setCode('');
      setStep('credentials');
      await reload();
    }, anchor);
  }, [action, anchor, password, reload, t]);

  const signOut = useCallback(() => {
    action.run(async () => {
      await tgLogout();
      setStep('credentials');
      setCode('');
      setPassword('');
      setCodeSent(false);
      setErrors({});
      await reload();
    });
  }, [action, reload]);

  /* ── render ──────────────────────────────────────────────────────────── */

  const authorized = slice.authorized;

  return (
    <Section
      id="sp-set-telegram"
      title={t('settings.telegram_section')}
      hint={t('settings.telegram_hint')}
      dirty={dirty && !authorized}
      actions={
        authorized ? (
          <button
            type="button"
            className="btn btn--sm"
            disabled={action.busy}
            onClick={signOut}
          >
            {t('settings.logout')}
          </button>
        ) : null
      }
    >
      {authorized ? (
        <div className="row row--wrap">
          <span className="badge badge--ok">{t('status.connected')}</span>
          <span>
            {t('settings.logged_in_as', { name: accountName(slice.user) })}
          </span>
          {slice.phone ? (
            <span className="mono mono--sm">{slice.phone}</span>
          ) : null}
        </div>
      ) : (
        <>
          <div style={GRID}>
            <TextField
              id={`${uid}-api-id`}
              label={t('settings.api_id')}
              value={form.apiId}
              mono
              inputMode="numeric"
              disabled={action.busy}
              error={errors.api_id ?? null}
              onChange={(value) => patch({ apiId: value })}
            />
            <SecretField
              id={`${uid}-api-hash`}
              label={t('settings.api_hash')}
              hasValue={slice.has_api_hash}
              value={form.apiHash}
              disabled={action.busy}
              error={errors.api_hash ?? null}
              needsReentry={needsReentry(
                settings.needs_reentry,
                'telegram.api_hash',
                'tg.api_hash',
              )}
              onChange={(value) => patch({ apiHash: value })}
            />
            <TextField
              id={`${uid}-phone`}
              label={t('settings.phone')}
              value={form.phone}
              type="tel"
              mono
              inputMode="tel"
              disabled={action.busy}
              hint={t('settings.phone_hint')}
              error={errors.phone ?? null}
              onChange={(value) => patch({ phone: value })}
            />
          </div>

          <div className="row row--wrap">
            <button
              type="button"
              className="btn"
              disabled={action.busy}
              onClick={sendCode}
            >
              {action.busy && step === 'credentials'
                ? t('app.saving')
                : t('settings.send_code')}
            </button>
            {codeSent ? (
              <span style={OK_NOTE} role="status">
                {t('settings.code_sent')}
              </span>
            ) : null}
          </div>

          {step !== 'credentials' ? (
            <div style={SUB_BOX}>
              <TextField
                id={`${uid}-code`}
                label={t('settings.code')}
                value={code}
                mono
                inputMode="numeric"
                autoComplete="one-time-code"
                disabled={action.busy || step === 'password'}
                hint={t('settings.code_hint')}
                error={errors.code ?? null}
                onChange={(value) => {
                  setCode(value);
                  setErrors((previous) => ({ ...previous, code: undefined }));
                }}
              />

              {step === 'password' ? (
                <>
                  <SecretField
                    id={`${uid}-2fa`}
                    label={t('settings.twofa')}
                    hasValue={false}
                    value={password}
                    disabled={action.busy}
                    autoComplete="current-password"
                    error={errors.password ?? null}
                    hint={t('settings.twofa_needed')}
                    onChange={(value) => {
                      setPassword(value ?? '');
                      setErrors((previous) => ({
                        ...previous,
                        password: undefined,
                      }));
                    }}
                  />
                </>
              ) : null}

              <div className="row row--wrap">
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={action.busy}
                  onClick={step === 'password' ? submitPassword : signIn}
                >
                  {action.busy ? t('app.saving') : t('settings.sign_in')}
                </button>
                <button
                  type="button"
                  className="btn btn--sm"
                  disabled={action.busy}
                  onClick={sendCode}
                >
                  {t('settings.send_code')}
                </button>
              </div>
            </div>
          ) : null}
        </>
      )}

      {action.error ? (
        <p className="field__error" role="alert">
          {action.error}
        </p>
      ) : null}

      {authorized ? (
        <DialogPicker
          dialogs={dialogs}
          source={slice.source}
          loading={dialogsLoading}
          error={dialogsError}
          disabled={action.busy}
          showHeader={false}
          onRefresh={loadDialogs}
          onSelected={() => {
            void reload();
          }}
        />
      ) : null}
    </Section>
  );
}
