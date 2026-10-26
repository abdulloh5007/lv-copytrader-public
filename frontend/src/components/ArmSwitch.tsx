/**
 * ArmSwitch — the largest control on the page (BUILD_PROMPT §9), and the kill
 * switch that sits beside it.
 *
 * The arm flag is the operator's on/off (BUILD_PROMPT §5.5): armed, the AI's write
 * tools reach the broker; disarmed, the loop still runs and streams its full
 * reasoning while every write tool answers "System is disarmed — no order was sent".
 *
 * Asymmetric on purpose:
 *   - DISARMING IS IMMEDIATE. It is the emergency brake; a confirmation dialog in
 *     front of a brake is a bug.
 *   - ARMING ASKS. One plain yes/no through ConfirmDialog — no typed word, because
 *     the switch itself is already a deliberate, unmistakable control.
 *   - THE KILL SWITCH ASKS FOR A TYPED WORD, `t('arm.kill_confirm_word')` (`KILL`).
 *     It closes every position AND cancels every pending order carrying our magic
 *     number, and only THEN disarms — that order matters, since disarming first
 *     would make its own closes answer "system is disarmed" and the switch would do
 *     nothing. Positions opened outside LV Copytrader are never touched.
 *
 * State is a word, never a colour alone: the track paints `--long` when armed and
 * `--text-dim` / `--border` when not, and the label beside it always reads ARMED or
 * DISARMED (ARMING… / DISARMING… while a request is in flight).
 *
 * The component owns the two requests (`arm()` and `killSwitch()` from api/client,
 * the latter sending `{confirm: "KILL"}` for us) and renders failures inline next to
 * the control — never as a toast. The parent stays the source of truth for `armed`:
 * pass the value from `GET /api/health` / `GET /api/trading/state` and refresh it in
 * `onArmedChange`. A locally confirmed state is held only until the prop catches up.
 */
import { useEffect, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import { arm, isApiError, killSwitch } from '../api/client';
import type { KillSwitchOut } from '../api/client';
import { formatInteger, useCurrentLocale, useT } from '../i18n';
import type { Translate } from '../i18n';
import { ConfirmDialog } from './ConfirmDialog';

/* ─────────────────────────────────── helpers ────────────────────────────── */

/** Turn a thrown value into the sentence the operator should read. */
function describeFailure(cause: unknown, t: Translate): string | null {
  if (isApiError(cause)) {
    if (cause.isAborted) return null;
    if (cause.isNetwork) return t('err.network');
    return cause.message;
  }
  if (cause instanceof Error) return cause.message;
  return String(cause);
}

/** A headline plus the backend's own words, both next to the control that failed. */
function renderFailure(t: Translate, headline: string, detail: string | null): ReactElement {
  return (
    <div className="col col--tight" role="alert">
      <p className="field__error">{headline}</p>
      {detail ? <p className="field__hint">{t('status.error_detail', { detail })}</p> : null}
    </div>
  );
}

/* ──────────────────────────────────── props ─────────────────────────────── */

export interface ArmSwitchProps {
  /** The armed flag as the backend last reported it. */
  armed: boolean;
  /** Called with the new state after a successful arm, disarm or kill switch. */
  onArmedChange?: (armed: boolean) => void;
  /** Called with the kill switch's own report: how much it closed and cancelled. */
  onKillResult?: (result: KillSwitchOut) => void;
  /** Locks both controls — MT5 down, settings incomplete, no source selected. */
  disabled?: boolean;
  /**
   * Header density: the switch and the ARMED/DISARMED word, no kill switch
   * and no explanatory copy. The cockpit keeps the large control plus KILL.
   */
  compact?: boolean;
  className?: string;
}

/* ────────────────────────────────── component ───────────────────────────── */

export function ArmSwitch(props: ArmSwitchProps): ReactElement {
  const t = useT();
  const locale = useCurrentLocale();

  const [pending, setPending] = useState<'arm' | 'disarm' | null>(null);
  const [armFailure, setArmFailure] = useState<string | null>(null);
  const [armConfirmOpen, setArmConfirmOpen] = useState(false);

  const [killOpen, setKillOpen] = useState(false);
  const [killBusy, setKillBusy] = useState(false);
  const [killFailure, setKillFailure] = useState<string | null>(null);
  const [killNotice, setKillNotice] = useState<string | null>(null);

  /**
   * A state this component confirmed with the backend, held until the `armed` prop
   * catches up. Without it the switch would snap back for one render on every
   * successful toggle while the parent refetches.
   */
  const [confirmed, setConfirmed] = useState<boolean | null>(null);
  const armedPropRef = useRef(props.armed);
  useEffect(() => {
    if (armedPropRef.current === props.armed) return;
    armedPropRef.current = props.armed;
    setConfirmed(null);
  }, [props.armed]);

  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const armed = confirmed ?? props.armed;
  const busy = pending !== null || killBusy;
  const locked = props.disabled === true;

  /* ── requests ────────────────────────────────────────────────────────── */

  const applyArm = (next: boolean): void => {
    if (busy) return;
    setPending(next ? 'arm' : 'disarm');
    setArmFailure(null);
    setKillFailure(null);
    setKillNotice(null);

    // Every settle path clears `pending` in the SAME state batch that closes the
    // dialog, so the switch is already enabled again when ConfirmDialog's cleanup
    // restores focus to it. Split across a `.finally` it would not be, and a
    // keyboard operator would be dropped onto <body>.
    void arm(next)
      .then((result) => {
        if (!aliveRef.current) return;
        setPending(null);
        setArmConfirmOpen(false);
        setConfirmed(result.armed);
        props.onArmedChange?.(result.armed);
      })
      .catch((cause: unknown) => {
        if (!aliveRef.current) return;
        setPending(null);
        // The dialog stays open on failure and shows the reason, so a retry is one
        // click rather than the whole confirmation again.
        setArmFailure(describeFailure(cause, t));
      });
  };

  const runKillSwitch = (): void => {
    if (killBusy) return;
    setKillBusy(true);
    setKillFailure(null);
    setKillNotice(null);
    setArmFailure(null);

    void killSwitch()
      .then((result) => {
        if (!aliveRef.current) return;
        setKillBusy(false);
        setKillOpen(false);
        // The backend disarms as the last step of the kill switch itself.
        setConfirmed(false);
        setKillNotice(
          result.closed === 0 && result.cancelled === 0
            ? t('arm.kill_nothing')
            : t('arm.kill_done', {
                closed: formatInteger(result.closed, locale),
                cancelled: formatInteger(result.cancelled, locale),
              }),
        );
        props.onArmedChange?.(false);
        props.onKillResult?.(result);
      })
      .catch((cause: unknown) => {
        if (!aliveRef.current) return;
        setKillBusy(false);
        // The dialog stays open with the word still typed, so a transient network
        // failure costs a click rather than the whole confirmation again.
        setKillFailure(describeFailure(cause, t));
      });
  };

  const onToggle = (): void => {
    if (busy || locked) return;
    if (armed) {
      applyArm(false); // the brake never asks
      return;
    }
    setArmFailure(null);
    setArmConfirmOpen(true);
  };


  /* ── labels ──────────────────────────────────────────────────────────── */

  const stateWord =
    pending === 'arm'
      ? t('arm.arming')
      : pending === 'disarm'
        ? t('arm.disarming')
        : armed
          ? t('arm.armed')
          : t('arm.disarmed');

  const killWord = t('arm.kill_confirm_word');

  const armFailureNode: ReactNode = armFailure
    ? renderFailure(t, t('arm.failed'), armFailure)
    : null;
  const killFailureNode: ReactNode = killFailure
    ? renderFailure(t, t('arm.kill_failed'), killFailure)
    : null;

  const compact = props.compact === true;
  const wrapClass = compact
    ? props.className
      ? `sp-arm sp-arm--compact ${props.className}`
      : 'sp-arm sp-arm--compact'
    : props.className
      ? `col ${props.className}`
      : 'col';

  return (
    <div className={wrapClass}>
      {compact ? null : <span className="field__label">{t('arm.state')}</span>}

      <div className={compact ? 'row row--tight' : 'row row--wide row--wrap'}>
        <button
          type="button"
          role="switch"
          className={compact ? 'switch' : 'switch switch--lg'}
          aria-checked={armed}
          aria-label={t('arm.state')}
          aria-busy={pending !== null || undefined}
          data-on={armed ? 'true' : 'false'}
          title={armFailure ?? t('arm.arm_hint')}
          disabled={busy || locked}
          onClick={onToggle}
        >
          <span className="switch__track">
            <span className="switch__thumb" />
          </span>
          <span className="switch__label">{stateWord}</span>
        </button>

        {compact ? null : (
          <>
            <span className="spacer" />
            <button
              type="button"
              className="btn btn--danger"
              title={t('arm.kill_switch_hint')}
              disabled={killBusy || locked}
              onClick={() => {
                setKillFailure(null);
                setKillNotice(null);
                setKillOpen(true);
              }}
            >
              {killBusy ? t('arm.kill_running') : t('arm.kill_switch')}
            </button>
          </>
        )}
      </div>

      {compact ? null : (
        <>
          <p className="field__hint">{armed ? t('arm.armed_note') : t('arm.disarmed_note')}</p>
          {armConfirmOpen ? null : armFailureNode}
          {killOpen ? null : killFailureNode}
          {killNotice ? (
            <p className="field__hint" role="status">
              {killNotice}
            </p>
          ) : null}
        </>
      )}

      <ConfirmDialog
        open={armConfirmOpen}
        title={t('arm.arm')}
        body={t('arm.arm_hint')}
        confirmLabel={t('arm.arm')}
        busy={pending === 'arm'}
        busyLabel={t('arm.arming')}
        error={armFailureNode}
        onConfirm={() => applyArm(true)}
        onCancel={() => setArmConfirmOpen(false)}
      />

      <ConfirmDialog
        open={killOpen}
        danger
        title={t('arm.kill_confirm_title')}
        body={
          <>
            <p>{t('arm.kill_switch_hint')}</p>
            <p>{t('arm.kill_confirm_prompt', { word: killWord })}</p>
          </>
        }
        requireTyped={killWord}
        typedPlaceholder={t('arm.kill_confirm_placeholder', { word: killWord })}
        confirmLabel={t('arm.kill_switch')}
        busy={killBusy}
        busyLabel={t('arm.kill_running')}
        error={killFailureNode}
        onConfirm={runKillSwitch}
        onCancel={() => setKillOpen(false)}
      />
    </div>
  );
}

export default ArmSwitch;
