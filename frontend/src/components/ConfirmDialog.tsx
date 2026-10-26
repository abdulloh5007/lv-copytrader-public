/**
 * ConfirmDialog — the only modal shape in the dashboard.
 *
 * Contract (assignment + CONTRACTS §20):
 *   { open, title, body, confirmLabel, danger?, requireTyped?, onConfirm, onCancel }
 * plus four optional extras the cockpit needs and nothing else does:
 *   cancelLabel, busy + busyLabel (a request in flight behind the confirm button),
 *   error (rendered inside the dialog so a failed action can be retried without
 *   retyping), typedLabel / typedPlaceholder.
 *
 * Behaviour, all of it deliberate:
 *   - Rendered through a portal onto `document.body` as `.modal__backdrop > .modal`,
 *     the markup contract components.css documents. Nothing renders while closed.
 *   - `requireTyped` gates the confirm button on an EXACT string match. No trimming
 *     and no case folding: the kill switch is the caller for this and strictness is
 *     the feature. Enter inside the input confirms once the match is exact.
 *   - Focus is trapped: Tab and Shift+Tab cycle inside the dialog, a focus that
 *     escapes (a click on the backdrop) is pulled back, and closing restores focus
 *     to whatever was focused when the dialog opened.
 *   - Initial focus is the typed input when there is one, the CANCEL button on a
 *     `danger` dialog, and the confirm button otherwise. A destructive dialog must
 *     never sit with a stray Enter key one keystroke away from firing.
 *   - Escape cancels, unless a request is in flight — losing the progress view to a
 *     stray keypress is worse than one extra click. The CANCEL BUTTON stays enabled
 *     even then, so the operator is never trapped by a slow backend.
 *   - The backdrop does NOT close on click. Every caller here is consequential.
 *   - Body scroll is locked while open, ref-counted so nested dialogs unlock once.
 *
 * Every string reaches the DOM through `t()`; the caller passes already-translated
 * `title` / `body` / `confirmLabel`, and the defaults come from the dictionary.
 */
import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, ReactElement, ReactNode } from 'react';
import { createPortal } from 'react-dom';

import { useT } from '../i18n';

/* ───────────────────────────────── the exit ─────────────────────────────── */

/**
 * How long the closed dialog stays mounted so it can leave.
 *
 * The CSS exit is var(--dur-fast) (120ms); this is a hair longer so the last
 * frame is never cut off. Nothing user-visible waits on it: the focus goes back
 * to the trigger, the scroll unlocks and the listeners come off the moment
 * `open` flips — the leftover node is inert and pointer-events: none.
 */
const EXIT_MS = 140;

/* ────────────────────────────── body scroll lock ────────────────────────── */

let scrollLockCount = 0;
let scrollLockPrevious = '';

function lockBodyScroll(): void {
  if (typeof document === 'undefined') return;
  if (scrollLockCount === 0) {
    scrollLockPrevious = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
  }
  scrollLockCount += 1;
}

function unlockBodyScroll(): void {
  if (typeof document === 'undefined') return;
  scrollLockCount = Math.max(0, scrollLockCount - 1);
  if (scrollLockCount === 0) {
    document.body.style.overflow = scrollLockPrevious;
    scrollLockPrevious = '';
  }
}

/* ───────────────────────────────── focus trap ───────────────────────────── */

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

/** Every tabbable element inside `root`, in document order, laid-out ones only. */
function focusableWithin(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => !element.hasAttribute('hidden') && element.getClientRects().length > 0,
  );
}

/* ──────────────────────────────────── props ─────────────────────────────── */

export interface ConfirmDialogProps {
  /** Nothing is rendered — no portal, no listeners — while this is false. */
  open: boolean;
  /** Already translated by the caller. */
  title: string;
  /** A string is wrapped in a paragraph; anything else renders as given. */
  body: ReactNode;
  /** Already translated by the caller. */
  confirmLabel: string;
  /** Defaults to `t('app.cancel')`. */
  cancelLabel?: string;
  /** Paints the confirm button `--danger` and moves initial focus to CANCEL. */
  danger?: boolean;
  /**
   * The word the operator must type before confirming, compared EXACTLY.
   * Omit for a plain yes/no dialog.
   */
  requireTyped?: string | null;
  /** Label above the typed input. Defaults to `t('app.confirm')`. */
  typedLabel?: string;
  /** Placeholder inside the typed input. */
  typedPlaceholder?: string;
  /** A request is in flight: the confirm button is disabled and shows `busyLabel`. */
  busy?: boolean;
  /** Shown on the confirm button while `busy`. Defaults to `t('app.loading')`. */
  busyLabel?: string;
  /** Rendered inside the dialog so the operator can read it and retry. */
  error?: ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
}

/* ────────────────────────────────── component ───────────────────────────── */

export function ConfirmDialog(props: ConfirmDialogProps): ReactElement | null {
  const t = useT();

  const open = props.open;
  const busy = props.busy ?? false;
  const danger = props.danger ?? false;
  const requireTyped = props.requireTyped ?? null;

  const [typed, setTyped] = useState('');

  /*
   * The dialog outlives `open` by one exit. `leaving` is what keeps the portal
   * mounted for those 140ms; every other effect in this component still keys
   * off `open`, so the close itself is instant and only the picture lags.
   */
  const [leaving, setLeaving] = useState(false);
  const wasOpen = useRef(open);
  useEffect(() => {
    const previous = wasOpen.current;
    wasOpen.current = open;
    if (open) {
      setLeaving(false);
      return undefined;
    }
    if (!previous) return undefined;
    setLeaving(true);
    const timer = window.setTimeout(() => { setLeaving(false); }, EXIT_MS);
    return () => { window.clearTimeout(timer); };
  }, [open]);

  const dialogRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  /** Read by the document-level Escape listener, which must not re-bind per render. */
  const latest = useRef<{ busy: boolean; onCancel: () => void }>({
    busy,
    onCancel: props.onCancel,
  });
  useEffect(() => {
    latest.current = { busy, onCancel: props.onCancel };
  });

  const baseId = useId();
  const titleId = `${baseId}-title`;
  const bodyId = `${baseId}-body`;
  const inputId = `${baseId}-typed`;
  const mismatchId = `${baseId}-mismatch`;

  // A dialog that opens always starts with an empty confirmation word.
  useEffect(() => {
    if (open) setTyped('');
  }, [open]);

  /*
   * One effect owns everything that is scoped to "the dialog is open": the scroll
   * lock, the Escape listener, the focus recapture and the initial focus. Keeping
   * them together is what guarantees the cleanup order — listeners off first, focus
   * restored last — which split effects could not promise.
   *
   * `danger` decides the initial focus target and is therefore a dependency;
   * `requireTyped` is not, because the input's existence is read from its ref.
   */
  useEffect(() => {
    if (!open) return undefined;
    if (typeof document === 'undefined') return undefined;

    const trigger =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;

    lockBodyScroll();

    const onDocumentKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== 'Escape') return;
      if (latest.current.busy) return;
      event.preventDefault();
      event.stopPropagation();
      latest.current.onCancel();
    };

    const onFocusIn = (event: FocusEvent): void => {
      const dialog = dialogRef.current;
      if (!dialog) return;
      const target = event.target;
      if (target instanceof Node && dialog.contains(target)) return;
      const nodes = focusableWithin(dialog);
      (nodes.length > 0 ? nodes[0] : dialog).focus();
    };

    document.addEventListener('keydown', onDocumentKeyDown, true);
    document.addEventListener('focusin', onFocusIn);

    const frame = window.requestAnimationFrame(() => {
      const initial =
        inputRef.current ?? (danger ? cancelRef.current : confirmRef.current) ?? dialogRef.current;
      initial?.focus();
    });

    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener('keydown', onDocumentKeyDown, true);
      document.removeEventListener('focusin', onFocusIn);
      unlockBodyScroll();
      // The portal is already detached by the time a passive cleanup runs, so this
      // does not fight the browser's own focus move to <body>.
      if (trigger && trigger.isConnected) trigger.focus();
    };
  }, [open, danger]);

  const matches = requireTyped === null || typed === requireTyped;
  const mismatch = requireTyped !== null && typed.length > 0 && typed !== requireTyped;
  const canConfirm = matches && !busy;

  const onDialogKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (event.key !== 'Tab') return;
    const dialog = dialogRef.current;
    if (!dialog) return;

    const nodes = focusableWithin(dialog);
    if (nodes.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }

    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;

    if (event.shiftKey) {
      if (active === first || active === dialog || !dialog.contains(active)) {
        event.preventDefault();
        last.focus();
      }
      return;
    }
    if (active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const onInputKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>): void => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    if (canConfirm) props.onConfirm();
  };

  if (!open && !leaving) return null;
  if (typeof document === 'undefined') return null;

  const describedBy = mismatch ? `${bodyId} ${mismatchId}` : bodyId;

  return createPortal(
    <div
      className={open ? 'modal__backdrop' : 'modal__backdrop is-leaving'}
      onKeyDown={onDialogKeyDown}
    >
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal={open ? 'true' : undefined}
        // On the way out it is a picture of a dialog, not a dialog: the trap is
        // already released and focus is already back on the trigger.
        aria-hidden={open ? undefined : 'true'}
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        aria-busy={busy || undefined}
        tabIndex={-1}
      >
        <header className="modal__header">
          <h2 className="modal__title" id={titleId}>
            {props.title}
          </h2>
        </header>

        <div className="modal__body" id={bodyId}>
          {typeof props.body === 'string' ? <p>{props.body}</p> : props.body}

          {requireTyped !== null ? (
            <div className={mismatch ? 'field field--invalid' : 'field'}>
              <label className="field__label" htmlFor={inputId}>
                {props.typedLabel ?? t('app.confirm')}
              </label>
              <input
                ref={inputRef}
                id={inputId}
                className="mono"
                type="text"
                value={typed}
                placeholder={props.typedPlaceholder}
                autoComplete="off"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                aria-invalid={mismatch || undefined}
                aria-describedby={describedBy}
                onChange={(event) => setTyped(event.target.value)}
                onKeyDown={onInputKeyDown}
              />
              {mismatch ? (
                <p className="field__error" id={mismatchId}>
                  {t('err.confirm_mismatch')}
                </p>
              ) : null}
            </div>
          ) : null}

          {props.error ? (
            typeof props.error === 'string' ? (
              <p className="field__error" role="alert">
                {props.error}
              </p>
            ) : (
              props.error
            )
          ) : null}
        </div>

        <footer className="modal__footer">
          <button
            ref={cancelRef}
            type="button"
            className="btn"
            onClick={props.onCancel}
          >
            {props.cancelLabel ?? t('app.cancel')}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={danger ? 'btn btn--danger' : 'btn btn--primary'}
            disabled={!canConfirm}
            onClick={props.onConfirm}
          >
            {busy ? props.busyLabel ?? t('app.loading') : props.confirmLabel}
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  );
}

export default ConfirmDialog;
