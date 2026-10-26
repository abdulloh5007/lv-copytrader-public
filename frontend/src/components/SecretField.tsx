/**
 * LV Copytrader — SecretField
 * ---------------------------------------------------------------------------
 * A password-style input for a secret the BACKEND stores and never returns
 * (BUILD_PROMPT BRIEF, CONTRACTS §18: every secret comes back as `MASK` with a
 * sibling `has_<field>: boolean`).
 *
 * The contract this component implements, exactly:
 *
 *  • It NEVER renders a real secret, because it never has one. When
 *    `hasValue` is true it shows `MASK` in a read-only input beside a "stored"
 *    chip; the bullets are all the server ever sent.
 *
 *  • Pressing Replace clears the field for entry. Leaving the field untouched
 *    reports `null` to the parent, which then OMITS the key from its PUT, and
 *    the stored value survives.
 *
 *  • The show/hide toggle only ever reveals what the operator is typing right
 *    now. It is not offered while the stored mask is on screen — there is
 *    nothing behind those bullets to reveal.
 *
 * `onChange(null)` means "send nothing"; `onChange(string)` means "store this".
 * An empty string is a real value the operator typed, not the same as null, so
 * the parent can tell "left alone" from "cleared".
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { CSSProperties, ReactElement, ReactNode } from 'react';

import { MASK } from '@/api/client';
import { useT } from '@/i18n';

/* ─────────────────────────────── props ──────────────────────────────────── */

export interface SecretFieldProps {
  /**
   * The field label, ALREADY translated by the caller — e.g.
   * `t('settings.api_key')`. Passing the string rather than the key keeps this
   * component reusable for every secret in the settings tree.
   */
  label: string;
  /** True when the backend reports a stored value (`has_api_key`, `has_password`…). */
  hasValue: boolean;
  /**
   * What the operator has typed, or `null` while the stored value is untouched.
   * Controlled by the parent so a form can reset every field at once.
   */
  value: string | null;
  /** `null` = leave the stored secret alone; a string = store this instead. */
  onChange: (value: string | null) => void;
  /** DOM id; one is generated when the caller does not supply it. */
  id?: string;
  /** An extra sentence under the control, on top of the standard hint. */
  hint?: ReactNode;
  /** A validation message; renders next to the field, never as a toast. */
  error?: string | null;
  /** Greys the whole control out. */
  disabled?: boolean;
  /** Marks the field required when there is nothing stored yet. */
  required?: boolean;
  /** Placeholder for the entry state. */
  placeholder?: string;
  /** `autoComplete`; defaults to `new-password` so no manager offers to fill it. */
  autoComplete?: string;
  /**
   * True when this key is listed in `needs_reentry` — the stored ciphertext no
   * longer decrypts under the current `SECRET_KEY`, so the operator must retype
   * it even though `hasValue` was true.
   */
  needsReentry?: boolean;
}

/* ────────────────────────────── local styles ────────────────────────────── */

const CONTROL_ROW: CSSProperties = { alignItems: 'stretch' };
const INPUT_WRAP: CSSProperties = { flex: '1 1 220px', minWidth: 0 };
const MASK_INPUT: CSSProperties = { letterSpacing: '0.2em' };

/* ──────────────────────────────── component ─────────────────────────────── */

export function SecretField(props: SecretFieldProps): ReactElement {
  const {
    label,
    hasValue,
    value,
    onChange,
    id,
    hint,
    error = null,
    disabled = false,
    required = false,
    placeholder,
    autoComplete = 'new-password',
    needsReentry = false,
  } = props;

  const t = useT();
  const generatedId = useId();
  const fieldId = id ?? `sp-secret-${generatedId}`;
  const inputRef = useRef<HTMLInputElement | null>(null);

  /**
   * "Stored and untouched" is the only state that shows the mask. A field with
   * nothing stored, or one whose ciphertext no longer decrypts, opens straight
   * into entry — there is nothing to preserve.
   */
  const stored = hasValue && !needsReentry;
  const [editing, setEditing] = useState(() => !stored || value !== null);
  const [revealed, setRevealed] = useState(false);

  // A parent that resets its form (value back to null) returns this control to
  // the masked state rather than leaving an empty box that looks cleared.
  useEffect(() => {
    if (value === null && stored) {
      setEditing(false);
      setRevealed(false);
    }
  }, [stored, value]);

  const beginEdit = useCallback(() => {
    setEditing(true);
    setRevealed(false);
    onChange('');
    // Focus after the input swaps from read-only to editable.
    window.requestAnimationFrame(() => inputRef.current?.focus());
  }, [onChange]);

  const keepStored = useCallback(() => {
    setEditing(false);
    setRevealed(false);
    onChange(null);
  }, [onChange]);

  const describedBy: string[] = [];
  if (error) describedBy.push(`${fieldId}-err`);
  else describedBy.push(`${fieldId}-hint`);

  const showMask = stored && !editing;

  return (
    <div className={error ? 'field field--invalid' : 'field'}>
      <label className="field__label" htmlFor={fieldId}>
        {label}
        {stored ? (
          <>
            {' '}
            <span className="badge badge--ok" title={t('settings.secret_hint')}>
              {t('settings.secret_stored')}
            </span>
          </>
        ) : null}
        {needsReentry ? (
          <>
            {' '}
            <span className="badge badge--warn">{t('status.error')}</span>
          </>
        ) : null}
      </label>

      <div className="row row--tight" style={CONTROL_ROW}>
        <div style={INPUT_WRAP}>
          {showMask ? (
            /* Not the secret — the eight bullets the API sent in its place. */
            <input
              id={fieldId}
              ref={inputRef}
              className="mono"
              style={MASK_INPUT}
              type="text"
              value={MASK}
              readOnly
              disabled={disabled}
              aria-readonly="true"
              aria-describedby={describedBy.join(' ')}
            />
          ) : (
            <input
              id={fieldId}
              ref={inputRef}
              className="mono"
              type={revealed ? 'text' : 'password'}
              value={value ?? ''}
              disabled={disabled}
              required={required && !hasValue}
              placeholder={placeholder}
              autoComplete={autoComplete}
              spellCheck={false}
              autoCapitalize="off"
              autoCorrect="off"
              aria-invalid={error ? 'true' : undefined}
              aria-describedby={describedBy.join(' ')}
              onChange={(event) => onChange(event.target.value)}
            />
          )}
        </div>

        {showMask ? (
          <button
            type="button"
            className="btn btn--sm"
            disabled={disabled}
            onClick={beginEdit}
          >
            {t('settings.secret_change')}
          </button>
        ) : (
          <>
            <button
              type="button"
              className="btn btn--sm"
              disabled={disabled}
              aria-pressed={revealed}
              aria-controls={fieldId}
              onClick={() => setRevealed((previous) => !previous)}
            >
              {revealed ? t('app.show_less') : t('app.show_more')}
            </button>
            {stored ? (
              <button
                type="button"
                className="btn btn--sm"
                disabled={disabled}
                onClick={keepStored}
              >
                {t('settings.secret_keep')}
              </button>
            ) : null}
          </>
        )}
      </div>

      {error ? (
        <p className="field__error" id={`${fieldId}-err`}>
          {error}
        </p>
      ) : (
        <p className="field__hint" id={`${fieldId}-hint`}>
          {hint ?? t('settings.secret_hint')}
        </p>
      )}
    </div>
  );
}

export default SecretField;
