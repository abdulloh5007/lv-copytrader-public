import type {
  NotifierBinding,
  NotifierSettingsInput,
  NotifierSettingsOut,
  NotifierWhoamiUpdate,
  NotifyTypes,
} from '@/api/client';
import {
  detectNotifierChat,
  notifierWhoami,
  putNotifierSettings,
  testNotifier,
} from '@/api/client';
import { SecretField } from '@/components/SecretField';
import type { Translate } from '@/i18n';
import { useT } from '@/i18n';
import type { ClipboardEvent, KeyboardEvent, ReactElement } from 'react';
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  ADMIN_BOX,
  ADMIN_BOX_OPEN,
  ADMIN_HEAD,
  ADMIN_LOCKED_TEXT,
  ADMIN_OPEN_TEXT,
  ADMIN_STATE,
  ADMIN_TITLE,
  ALIGN_UNDER_LABEL,
  BOUND_NAME,
  BOUND_ROW,
  DIGITS,
  errorField,
  errorSentence,
  FIELDSET,
  GRID,
  hasError,
  LEGEND,
  NARROW,
  needsReentry,
  NOTE,
  NOTIFY_ROWS,
  NUMERIC_ID,
  OK_NOTE,
  positiveInt,
  Section,
  SectionCommonProps,
  SENDERS_BOX,
  TextField,
  ToggleRow,
  toInt,
  toOptionalInt,
  useAsyncAction,
  WELL,
  wholeNumber,
} from './shared';
interface NotifierForm {
  botToken: string | null;
  chatId: string;
  enabled: boolean;
  types: NotifyTypes;
  /** Telegram user ids allowed to bind the bot and receive notifications. */
  adminIds: number[];
}

type NotifierErrors = Partial<
  Record<'bot_token' | 'chat_id' | 'admin_ids', string>
>;

function notifierForm(notifier: NotifierSettingsOut): NotifierForm {
  return {
    botToken: null,
    chatId: notifier.chat_id === null ? '' : String(notifier.chat_id),
    enabled: notifier.enabled,
    types: { ...notifier.types },
    adminIds: [...notifier.admin_ids],
  };
}

const PASTED_ID = /\d{4,}/;

function botAccountName(
  firstName: string,
  username: string,
  t: Translate,
): string {
  const first = firstName.trim();
  const handle = username.trim();
  if (first && handle) return `${first} @${handle}`;
  if (first) return first;
  if (handle) return `@${handle}`;
  return t('app.unknown');
}

interface AdminAllowlistProps {
  /** Prefix for the ids this block mints; the section's `useId` value. */
  idPrefix: string;
  ids: readonly number[];
  boundTo: NotifierBinding | null;
  /** `null` until `whoami` has run; `[]` means it ran and nobody has written. */
  senders: readonly NotifierWhoamiUpdate[] | null;
  busy: boolean;
  whoamiBusy: boolean;
  /** One sentence — a rejected save or a failed `whoami` — under the controls. */
  error: string | null;
  onChange: (ids: number[]) => void;
  onWhoami: () => void;
}

function AdminAllowlist(props: AdminAllowlistProps): ReactElement {
  const { idPrefix, ids, boundTo, senders, busy, whoamiBusy } = props;
  const { onChange, onWhoami } = props;
  const t = useT();

  const [draft, setDraft] = useState('');
  const [draftError, setDraftError] = useState<string | null>(null);
  const [lastAction, setLastAction] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const restricted = ids.length > 0;
  const inputId = `${idPrefix}-admin-id`;
  const hintId = `${inputId}-hint`;
  const errorId = `${inputId}-err`;

  /** Put one id on the list. Adding a known id says so and changes nothing. */
  const add = useCallback(
    (value: number): boolean => {
      if (ids.includes(value)) {
        setLastAction(null);
        setDraftError(t('settings.notifier_admin_duplicate', { id: value }));
        return false;
      }
      setDraftError(null);
      setLastAction(t('settings.notifier_admin_added', { id: value }));
      onChange([...ids, value]);
      return true;
    },
    [ids, onChange, t],
  );

  const remove = useCallback(
    (value: number): void => {
      setDraftError(null);
      setLastAction(t('settings.notifier_admin_removed', { id: value }));
      onChange(ids.filter((entry) => entry !== value));
    },
    [ids, onChange, t],
  );

  const clearAll = useCallback((): void => {
    setDraftError(null);
    setLastAction(t('settings.notifier_admins_cleared'));
    onChange([]);
  }, [onChange, t]);

  /** Validate what is in the field and put it on the list, emptying the field. */
  const commitDraft = useCallback((): void => {
    const raw = draft.trim();
    const invalid = positiveInt(raw, t, true);
    if (invalid) {
      setLastAction(null);
      setDraftError(invalid);
      return;
    }
    if (add(toInt(raw))) setDraft('');
  }, [add, draft, t]);

  /**
   * Enter adds. Backspace on an empty field takes the last chip back off, which
   * is the one gesture people expect from a chip input and the only way to undo
   * a mistyped id without reaching for the mouse.
   */
  const onDraftKeyDown = useCallback(
    (event: KeyboardEvent<HTMLInputElement>): void => {
      if (event.key === 'Enter') {
        // Nothing typed is not a mistake worth an error sentence.
        event.preventDefault();
        if (draft.trim()) commitDraft();
        return;
      }
      if (event.key === 'Backspace' && draft === '' && ids.length > 0) {
        event.preventDefault();
        remove(ids[ids.length - 1]);
      }
    },
    [commitDraft, draft, ids, remove],
  );

  /** A clean paste goes through untouched; a dirty one is mined for the id. */
  const onDraftPaste = useCallback(
    (event: ClipboardEvent<HTMLInputElement>): void => {
      const pasted = event.clipboardData.getData('text').trim();
      if (!pasted || DIGITS.test(pasted)) return;
      const found = PASTED_ID.exec(pasted);
      if (!found) return;
      event.preventDefault();
      setDraftError(null);
      setDraft(found[0]);
    },
    [],
  );

  /**
   * One row per person.
   *
   * `getUpdates` hands back every message it still holds, so an operator who
   * sent `/start` and then three more lines would otherwise appear four times.
   * The latest entry for a user wins: it carries the name Telegram has now.
   */
  const uniqueSenders = useMemo((): readonly NotifierWhoamiUpdate[] | null => {
    if (senders === null) return null;
    const byUser = new Map<number, NotifierWhoamiUpdate>();
    for (const row of senders) byUser.set(row.user_id, row);
    return [...byUser.values()];
  }, [senders]);

  /** Take a chip off and put the caret back where the next one is typed. */
  const removeFromChip = useCallback(
    (value: number): void => {
      remove(value);
      inputRef.current?.focus();
    },
    [remove],
  );

  return (
    <div style={restricted ? ADMIN_BOX : ADMIN_BOX_OPEN}>
      <div style={ADMIN_HEAD}>
        <span style={ADMIN_TITLE}>{t('settings.notifier_admins')}</span>
        <span
          className={restricted ? 'badge badge--accent' : 'badge badge--warn'}
        >
          {restricted
            ? t('settings.notifier_admins_locked_badge')
            : t('settings.notifier_admins_open_badge')}
        </span>
        {restricted ? (
          <span className="badge badge--dim">
            <span className="badge__num">{String(ids.length)}</span>
          </span>
        ) : null}
      </div>

      {/* The two states, plainly, where they cannot be missed — plus what the
          last press did, in the same live region so it is announced once. */}
      <div style={ADMIN_STATE} role="status">
        <p style={restricted ? ADMIN_LOCKED_TEXT : ADMIN_OPEN_TEXT}>
          {restricted
            ? t('settings.notifier_admins_locked')
            : t('settings.notifier_admins_open')}
        </p>
        {lastAction ? <p style={NOTE}>{lastAction}</p> : null}
      </div>

      {/* Who the bot answers to today. A bound account that is not on the list
          is one press from being on it — the commonest case right after the
          chat is detected. */}
      {boundTo ? (
        <p style={BOUND_ROW}>
          <span>{t('settings.notifier_bound_label')}</span>
          <span style={BOUND_NAME}>
            {botAccountName(boundTo.first_name, boundTo.username, t)}
          </span>
          <span className="mono" style={NUMERIC_ID}>
            {String(boundTo.user_id)}
          </span>
          {ids.includes(boundTo.user_id) ? null : (
            <button
              type="button"
              className="btn btn--sm"
              disabled={busy}
              onClick={() => add(boundTo.user_id)}
            >
              {t('settings.whoami_add')}
            </button>
          )}
        </p>
      ) : (
        <p style={BOUND_ROW}>{t('settings.notifier_bound_nobody')}</p>
      )}

      {restricted ? (
        <div
          style={WELL}
          role="group"
          aria-label={t('settings.notifier_admins_allowed')}
        >
          {ids.map((value) => (
            <span className="chip" key={value}>
              <span className="chip__label">{String(value)}</span>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                disabled={busy}
                aria-label={t('settings.notifier_admin_remove', { id: value })}
                onClick={() => removeFromChip(value)}
              >
                <span aria-hidden="true">✕</span>
              </button>
            </span>
          ))}
          <span className="spacer" />
          <button
            type="button"
            className="btn btn--sm"
            disabled={busy}
            onClick={clearAll}
          >
            {t('settings.notifier_admins_clear')}
          </button>
        </div>
      ) : null}

      <div className="row row--wrap row--top">
        <div
          className={draftError ? 'field field--invalid' : 'field'}
          style={NARROW}
        >
          <label className="field__label" htmlFor={inputId}>
            {t('settings.notifier_admin_id')}
          </label>
          <input
            id={inputId}
            ref={inputRef}
            className="mono"
            type="text"
            inputMode="numeric"
            value={draft}
            disabled={busy}
            autoComplete="off"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            aria-invalid={draftError ? 'true' : undefined}
            aria-describedby={draftError ? errorId : hintId}
            onChange={(event) => {
              setDraft(event.target.value);
              setDraftError(null);
            }}
            onKeyDown={onDraftKeyDown}
            onPaste={onDraftPaste}
          />
          {draftError ? (
            <p className="field__error" id={errorId}>
              {draftError}
            </p>
          ) : (
            <p className="field__hint" id={hintId}>
              {t('settings.notifier_admin_id_hint')}
            </p>
          )}
        </div>
        <div className="row row--tight" style={ALIGN_UNDER_LABEL}>
          <button
            type="button"
            className="btn"
            disabled={busy || !draft.trim()}
            onClick={commitDraft}
          >
            {t('settings.notifier_admin_add')}
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={onWhoami}
          >
            {whoamiBusy ? t('settings.whoami_busy') : t('settings.whoami')}
          </button>
        </div>
      </div>

      {/* A refused save or a failed getUpdates: about the block, not the field,
          so it sits on its own line rather than under the input. */}
      {props.error ? (
        <p className="field__error" role="alert">
          {props.error}
        </p>
      ) : null}

      <p className="field__hint">{t('settings.whoami_hint')}</p>

      {/* Everyone getUpdates still holds, each with the numeric id and one press
          that puts it on the list. This is the answer to "where do I find my
          Telegram id" — nobody should have to leave for @userinfobot. */}
      {uniqueSenders === null ? null : uniqueSenders.length === 0 ? (
        <p className="field__hint">{t('settings.whoami_none')}</p>
      ) : (
        <div
          className="scroll-y"
          style={SENDERS_BOX}
          role="group"
          aria-label={t('settings.whoami_senders')}
        >
          {uniqueSenders.map((row) => {
            // The chips are the live truth: `row.is_admin` is the server's view
            // from the moment of the call and goes stale as soon as one is added
            // here, so it is not what decides how the row reads.
            const already = ids.includes(row.user_id);
            const first = row.first_name.trim();
            const handle = row.username.trim();
            return (
              <button
                type="button"
                key={row.user_id}
                className={already ? 'option option--on' : 'option'}
                disabled={busy}
                aria-disabled={already ? 'true' : undefined}
                aria-label={
                  already
                    ? undefined
                    : t('settings.whoami_add_one', {
                        name: botAccountName(row.first_name, row.username, t),
                        id: row.user_id,
                      })
                }
                onClick={() => {
                  if (!already) add(row.user_id);
                }}
              >
                <span className="option__main">
                  <span className="option__title">
                    <span className="option__name">
                      {first || t('app.unknown')}
                    </span>
                    {handle ? (
                      <span className="option__desc">@{handle}</span>
                    ) : null}
                    <span className="mono" style={NUMERIC_ID}>
                      {String(row.user_id)}
                    </span>
                  </span>
                  {row.text ? (
                    <span className="option__sub">{row.text}</span>
                  ) : null}
                </span>
                <span className="option__nums">
                  <span
                    className={
                      already ? 'badge badge--ok' : 'badge badge--accent'
                    }
                  >
                    {already
                      ? t('settings.whoami_on_list')
                      : t('settings.whoami_add')}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function NotifierSection(props: SectionCommonProps): ReactElement {
  const { settings, onSettings } = props;
  const t = useT();
  const uid = useId();
  const slice = settings.notifier;

  const [form, setForm] = useState<NotifierForm>(() => notifierForm(slice));
  const [dirty, setDirty] = useState(false);
  const [errors, setErrors] = useState<NotifierErrors>({});
  const [botUsername, setBotUsername] = useState<string | null>(null);
  const [chatTitle, setChatTitle] = useState<string | null>(null);
  /** Null until `whoami` has answered once; the empty array is a real answer. */
  const [senders, setSenders] = useState<
    readonly NotifierWhoamiUpdate[] | null
  >(null);

  const save = useAsyncAction(t);
  const detect = useAsyncAction(t);
  const whoami = useAsyncAction(t);

  useEffect(() => {
    if (!dirty) setForm(notifierForm(slice));
  }, [dirty, slice]);

  const patch = useCallback(
    (next: Partial<NotifierForm>) => {
      setForm((previous) => ({ ...previous, ...next }));
      setDirty(true);
      save.clear();
    },
    [save],
  );

  /**
   * `requireToken` is true for the two buttons that talk to Telegram: `getMe`,
   * `getUpdates` and the whoami poll cannot run without a token, while storing
   * "notify me about closes" before the bot exists is harmless and should not
   * be blocked.
   */
  const validate = useCallback(
    (requireToken: boolean): NotifierErrors => {
      const next: NotifierErrors = {};
      if (requireToken && !slice.has_bot_token && !form.botToken?.trim()) {
        next.bot_token = t('err.required');
      }
      const chatId = wholeNumber(form.chatId, t, false);
      if (chatId) next.chat_id = chatId;
      return next;
    },
    [form.botToken, form.chatId, slice.has_bot_token, t],
  );

  const anchor = useCallback(
    (cause: unknown): boolean => {
      const field = errorField(cause);
      if (
        field === 'bot_token' ||
        field === 'chat_id' ||
        field === 'admin_ids'
      ) {
        setErrors({ [field]: errorSentence(cause, t) });
        return true;
      }
      return false;
    },
    [t],
  );

  const body = useCallback(
    (chatId?: number | null): NotifierSettingsInput => ({
      bot_token: form.botToken ?? undefined,
      chat_id: chatId === undefined ? toOptionalInt(form.chatId) : chatId,
      enabled: form.enabled,
      types: form.types,
      // Always sent, never omitted: this PUT replaces the list, so leaving it
      // out on a neighbouring edit would silently keep a stale allowlist.
      admin_ids: form.adminIds,
    }),
    [form],
  );

  const onSave = useCallback(() => {
    const next = validate(false);
    setErrors(next);
    if (hasError(next)) return;
    save.run(async () => {
      const updated = await putNotifierSettings(body());
      setDirty(false);
      onSettings(updated);
    }, anchor);
  }, [anchor, body, onSettings, save, validate]);

  /**
   * §7 in one button: store the token, `getMe` it, then poll `getUpdates` for up
   * to sixty seconds and write back whatever chat says `/start` first. Both
   * calls read the STORED token, so the save has to happen before the test.
   */
  const onDetect = useCallback(() => {
    const next = validate(true);
    setErrors(next);
    if (hasError(next)) return;
    setBotUsername(null);
    setChatTitle(null);

    detect.run(async () => {
      let updated = await putNotifierSettings(body());
      setDirty(false);
      onSettings(updated);

      const identity = await testNotifier();
      if (!identity.ok) {
        detect.fail(identity.message || t('settings.test_failed'));
        return;
      }
      setBotUsername(identity.bot_username);

      const found = await detectNotifierChat();
      if (!found.ok || found.chat_id === null) {
        detect.fail(found.message || t('settings.detect_timeout'));
        return;
      }
      setChatTitle(found.chat_title);
      setForm((previous) => ({ ...previous, chatId: String(found.chat_id) }));
      updated = await putNotifierSettings(body(found.chat_id));
      onSettings(updated);
    }, anchor);
  }, [anchor, body, detect, onSettings, t, validate]);

  /**
   * "Who has messaged the bot" — the operator's own Telegram id, found without
   * leaving the page. `getUpdates` runs against the STORED token, so this saves
   * first exactly as the detect button does.
   */
  const onWhoami = useCallback(() => {
    const next = validate(true);
    setErrors(next);
    if (hasError(next)) return;
    setSenders(null);

    whoami.run(async () => {
      const updated = await putNotifierSettings(body());
      setDirty(false);
      onSettings(updated);

      const found = await notifierWhoami();
      if (!found.ok) {
        whoami.fail(found.message || t('settings.whoami_none'));
        return;
      }
      setSenders(found.updates);
    }, anchor);
  }, [anchor, body, onSettings, t, validate, whoami]);

  const busy = save.busy || detect.busy || whoami.busy;

  return (
    <Section
      id="sp-set-notifier"
      title={t('settings.notifier_section')}
      hint={t('settings.notifier_hint')}
      dirty={dirty}
      saving={save.busy}
      saved={save.done}
      error={save.error}
      onSave={onSave}
      actions={
        <button
          type="button"
          className="btn btn--sm"
          disabled={busy}
          onClick={onDetect}
        >
          {detect.busy ? t('settings.detecting') : t('settings.detect_chat')}
        </button>
      }
    >
      <div style={GRID}>
        <SecretField
          id={`${uid}-bot-token`}
          label={t('settings.bot_token')}
          hasValue={slice.has_bot_token}
          value={form.botToken}
          disabled={busy}
          error={errors.bot_token ?? null}
          needsReentry={needsReentry(
            settings.needs_reentry,
            'notifier.bot_token',
          )}
          onChange={(value) => patch({ botToken: value })}
        />
        <TextField
          id={`${uid}-chat-id`}
          label={t('settings.chat_id')}
          value={form.chatId}
          mono
          disabled={busy}
          optional
          hint={t('settings.detect_hint')}
          error={errors.chat_id ?? null}
          onChange={(value) => patch({ chatId: value })}
        />
      </div>

      {detect.error ? (
        <p className="field__error" role="alert">
          {detect.error}
        </p>
      ) : null}
      {botUsername ? (
        <p style={OK_NOTE} role="status">
          {t('settings.bot_username', { username: botUsername })}
        </p>
      ) : null}
      {chatTitle ? (
        <p style={OK_NOTE} role="status">
          {t('settings.chat_detected', { title: chatTitle })}
        </p>
      ) : null}

      <AdminAllowlist
        idPrefix={uid}
        ids={form.adminIds}
        boundTo={slice.bound_to}
        senders={senders}
        busy={busy}
        whoamiBusy={whoami.busy}
        error={whoami.error ?? errors.admin_ids ?? null}
        onChange={(next) => patch({ adminIds: next })}
        onWhoami={onWhoami}
      />

      <ToggleRow
        id={`${uid}-enabled`}
        label={t('settings.notifier_enabled')}
        checked={form.enabled}
        disabled={busy}
        onChange={(checked) => patch({ enabled: checked })}
      />

      <fieldset style={FIELDSET}>
        <legend style={LEGEND}>{t('settings.notify_types')}</legend>
        {NOTIFY_ROWS.map((row) => (
          <ToggleRow
            key={row.type}
            id={`${uid}-notify-${row.type}`}
            label={t(row.label)}
            hint={row.hint ? t(row.hint) : undefined}
            checked={form.types[row.type]}
            disabled={busy || !form.enabled}
            onChange={(checked) =>
              patch({ types: { ...form.types, [row.type]: checked } })
            }
          />
        ))}
      </fieldset>
    </Section>
  );
}
