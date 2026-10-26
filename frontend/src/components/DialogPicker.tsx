/**
 * LV Copytrader — DialogPicker
 * ---------------------------------------------------------------------------
 * The channel list from `GET /api/telegram/dialogs`, searchable, one of which
 * becomes the active signal source (BUILD_PROMPT §9 "Settings", §3.5).
 *
 * The one thing an operator must be told before they press a row:
 *
 *   SELECTING A CHANNEL DOES NOT REPLAY ITS BACKLOG. On the first selection the
 *   backend records the channel's current latest `message_id` as the baseline
 *   and fetches nothing behind it, so a year of old calls never arrives as a
 *   burst of live signals (BUILD §8.4). That warning is not tucked into a
 *   tooltip; it sits above the list, always visible.
 *
 * A dialog with no public `username` is a private chat and has no `@handle` to
 * show — the column says so rather than rendering an empty cell.
 */
import { useCallback, useMemo, useState } from 'react';
import type { CSSProperties, ReactElement } from 'react';

import { isApiError, tgSelectSource } from '@/api/client';
import type { DialogOut, SourceOut, SourceSelectOut } from '@/api/client';
import { useT } from '@/i18n';
import type { Translate } from '@/i18n';

/* ─────────────────────────────── props ──────────────────────────────────── */

export interface DialogPickerProps {
  /** The operator's chats, from `GET /api/telegram/dialogs`. */
  dialogs: DialogOut[];
  /** The source currently in force, so its row can render as chosen. */
  source?: SourceOut | null;
  /** True while the dialog list is being fetched. */
  loading?: boolean;
  /** A fetch failure, already turned into a sentence by the caller. */
  error?: string | null;
  /** Re-request the dialog list. */
  onRefresh?: () => void;
  /**
   * Fired after a successful selection, so the parent can refresh its settings
   * tree. The component performs the call itself unless `onSelect` is given.
   */
  onSelected?: (result: SourceSelectOut) => void;
  /** Override the selection call — defaults to `POST /api/telegram/source`. */
  onSelect?: (chatId: number) => Promise<SourceSelectOut>;
  /** Greys the whole control out — e.g. while the account is not authorized. */
  disabled?: boolean;
  /** Set false to drop the card chrome and render the picker alone. */
  showHeader?: boolean;
}

/* ────────────────────────────── local styles ────────────────────────────── */

const LIST_BOX: CSSProperties = {
  maxHeight: '360px',
  borderTop: 'var(--border-w) solid var(--border)',
};
const OPTION: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 'var(--sp-2)',
  width: '100%',
  // A channel row is a control, and picking the wrong one changes what the AI
  // reads: 40px minimum, flush against its neighbours.
  minHeight: 'var(--touch)',
  padding: 'var(--sp-2)',
  textAlign: 'left',
  background: 'transparent',
  border: 0,
  borderBottom: 'var(--border-w) solid var(--border)',
  cursor: 'pointer',
  color: 'var(--text)',
};
const OPTION_ON: CSSProperties = { ...OPTION, background: 'var(--accent-dim)' };
const OPTION_MAIN: CSSProperties = { flex: '1 1 auto', minWidth: 0, overflow: 'hidden' };
const OPTION_TITLE: CSSProperties = {
  display: 'block',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};
const OPTION_SUB: CSSProperties = {
  display: 'block',
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-1)',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};
const BODY: CSSProperties = { padding: 0 };
const NOTE: CSSProperties = { color: 'var(--text-dim)', fontSize: 'var(--fs-1)' };
const WARN_NOTE: CSSProperties = {
  color: 'var(--warn)',
  fontSize: 'var(--fs-1)',
  padding: 'var(--sp-1) 0 var(--sp-1) var(--sp-3)',
  borderLeft: '2px solid var(--warn)',
};
const OK_NOTE: CSSProperties = { color: 'var(--ok)', fontSize: 'var(--fs-1)' };
const BAD_NOTE: CSSProperties = { color: 'var(--danger)', fontSize: 'var(--fs-1)' };

/* ───────────────────────────── small helpers ────────────────────────────── */

/** Turn any thrown value into the sentence the picker will show. */
function errorText(cause: unknown, t: Translate): string {
  if (isApiError(cause)) return cause.message;
  if (cause instanceof Error && cause.message) return cause.message;
  return t('err.generic');
}

/** Case-insensitive match on the title and, when there is one, the @handle. */
function matches(dialog: DialogOut, needle: string): boolean {
  if (!needle) return true;
  const lowered = needle.toLowerCase();
  return (
    dialog.title.toLowerCase().includes(lowered) ||
    (dialog.username ?? '').toLowerCase().includes(lowered)
  );
}

/* ──────────────────────────────── component ─────────────────────────────── */

export function DialogPicker(props: DialogPickerProps): ReactElement {
  const {
    dialogs,
    source = null,
    loading = false,
    error = null,
    onRefresh,
    onSelected,
    onSelect,
    disabled = false,
    showHeader = true,
  } = props;

  const t = useT();

  const [search, setSearch] = useState('');
  const [busyChatId, setBusyChatId] = useState<number | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<string | null>(null);

  const selectCall = useCallback(
    (chatId: number): Promise<SourceSelectOut> =>
      onSelect ? onSelect(chatId) : tgSelectSource({ chat_id: chatId }),
    [onSelect],
  );

  const filtered = useMemo(
    () => dialogs.filter((dialog) => matches(dialog, search.trim())),
    [dialogs, search],
  );

  const choose = useCallback(
    (dialog: DialogOut) => {
      setBusyChatId(dialog.chat_id);
      setFailure(null);
      setConfirmation(null);
      selectCall(dialog.chat_id)
        .then((result) => {
          if (result.ok) {
            setConfirmation(
              t('settings.source_selected', { title: result.source?.title ?? dialog.title }),
            );
            onSelected?.(result);
          } else {
            setFailure(result.message || t('err.generic'));
          }
        })
        .catch((cause: unknown) => setFailure(errorText(cause, t)))
        .finally(() => setBusyChatId(null));
    },
    [onSelected, selectCall, t],
  );

  const body = (
    <div className="stack" style={BODY}>
      {/* The one fact that changes what pressing a row means. Always visible. */}
      <p style={WARN_NOTE} role="note">
        {t('settings.source_change_hint')}
      </p>

      {source ? (
        <p style={OK_NOTE}>{t('settings.source_selected', { title: source.title })}</p>
      ) : (
        <p style={NOTE}>{t('settings.no_source')}</p>
      )}

      <div className="picker-toolbar">
        <div className="field">
          <label className="field__label" htmlFor="sp-dialog-search">
            {t('settings.search_channels')}
          </label>
          <input
            id="sp-dialog-search"
            type="search"
            autoComplete="off"
            value={search}
            disabled={disabled || loading}
            placeholder={t('app.search')}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        {onRefresh ? (
          <button
            type="button"
            className="btn btn--sm"
            disabled={disabled || loading}
            onClick={onRefresh}
          >
            {t('settings.refresh_dialogs')}
          </button>
        ) : null}
      </div>

      {error ? (
        <p className="field__error" role="alert">
          {error}
        </p>
      ) : null}
      {failure ? (
        <p style={BAD_NOTE} role="alert">
          {failure}
        </p>
      ) : null}
      {confirmation ? (
        <p style={OK_NOTE} role="status">
          {confirmation}
        </p>
      ) : null}

      {loading && dialogs.length === 0 ? (
        <p style={NOTE}>{t('settings.dialogs_loading')}</p>
      ) : filtered.length === 0 ? (
        <div className="empty">
          <p className="empty__title">{t('settings.dialogs_empty')}</p>
        </div>
      ) : (
        <div
          className="scroll-y"
          style={LIST_BOX}
          role="radiogroup"
          aria-label={t('settings.pick_channel')}
        >
          {filtered.map((dialog) => {
            const on = source?.chat_id === dialog.chat_id;
            const busy = busyChatId === dialog.chat_id;
            return (
              <button
                key={dialog.chat_id}
                type="button"
                role="radio"
                aria-checked={on}
                style={on ? OPTION_ON : OPTION}
                disabled={disabled || busy}
                onClick={() => choose(dialog)}
              >
                <span style={OPTION_MAIN}>
                  <span style={OPTION_TITLE}>{dialog.title}</span>
                  <span style={OPTION_SUB}>
                    {dialog.username ? (
                      <span className="mono">@{dialog.username}</span>
                    ) : (
                      /* No public handle: a private chat, nothing to show. */
                      t('app.none')
                    )}
                  </span>
                </span>
                <span className="mono mono--sm" style={NOTE}>
                  {dialog.chat_id}
                </span>
                {dialog.is_channel ? (
                  <span className="badge badge--dim">{t('settings.channel')}</span>
                ) : null}
                {on ? <span className="badge badge--accent">{t('app.on')}</span> : null}
                {busy ? <span className="badge badge--dim">{t('app.saving')}</span> : null}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );

  if (!showHeader) return body;

  return (
    <section className="card card--flush">
      <header className="card__header">
        <h2 className="card__title">{t('settings.pick_channel')}</h2>
        <div className="card__actions">
          <span className="badge badge--dim">
            <span className="badge__num">{filtered.length}</span>
            {t('settings.channel')}
          </span>
        </div>
      </header>
      {body}
    </section>
  );
}

export default DialogPicker;
