/**
 * StatusDots — the three explicit health dots of the cockpit: MT5, Telegram and
 * the AI provider (BUILD_PROMPT §9).
 *
 * Each row is a `.dot` carrying the component's NAME as its text label plus a chip
 * carrying the STATE AS A WORD — Connected / Disconnected / Error / Checking… /
 * Unknown. The colour of the marker is a second carrier, never the only one, so the
 * panel survives a monochrome screenshot and a colour-blind reader.
 *
 * When a component is unhealthy its `error` string rides in the `title`, framed by
 * `t('status.error_detail')`. A healthy dot's title is the one-line explanation of
 * what that component actually is, because "Provider" means nothing on day one.
 *
 * The props take the health/status object as it arrives from the API, in either of
 * the two shapes CONTRACTS §18 hands out:
 *   - `GET /api/health`        → `HealthOut`, dots under `.status`
 *   - `GET /api/trading/state` → `TradingStateOut`, dots at the top level
 * and `status.changed` stream events patch either one in the parent.
 */
import type { ReactElement } from 'react';

import type { ComponentStatus, StatusMap } from '../api/client';
import { formatRelative, useCurrentLocale, useT } from '../i18n';
import type { TKey, Translate } from '../i18n';

/** Either envelope the backend reports the three dots in. */
export type StatusSource = StatusMap | { status: StatusMap };

/** Pull the three dots out of whichever envelope was handed in. */
export function toStatusMap(source: StatusSource | null | undefined): StatusMap | null {
  if (!source) return null;
  if ('status' in source) return source.status ?? null;
  return source;
}

interface DotTone {
  /** The `.dot--*` modifier from components.css. */
  dotClass: string;
  /** The `.badge--*` modifier for the state word. */
  badgeClass: string;
  /** The state, as a word. */
  word: string;
}

/**
 * Map one component's health onto its dot.
 *
 * There is no `.badge--danger` in components.css; `.badge--short` IS the `--danger`
 * outline (both read `--short`), so it is what a bad state wears. Healthy and
 * unknown states stay `--dim`: a cockpit is quiet until something matters.
 */
function toneFor(state: ComponentStatus | null | undefined, loading: boolean, t: Translate): DotTone {
  if (loading) {
    return { dotClass: 'dot--idle', badgeClass: 'badge--dim', word: t('status.checking') };
  }
  if (!state) {
    return { dotClass: 'dot--idle', badgeClass: 'badge--dim', word: t('status.unknown') };
  }
  if (state.ok) {
    return { dotClass: 'dot--ok', badgeClass: 'badge--dim', word: t('status.connected') };
  }
  return {
    dotClass: 'dot--bad',
    badgeClass: 'badge--short',
    word: state.error ? t('status.error') : t('status.disconnected'),
  };
}

interface DotSpec {
  key: 'mt5' | 'telegram' | 'provider';
  labelKey: TKey;
  shortKey: TKey;
  hintKey: TKey;
}

const DOTS: readonly DotSpec[] = [
  { key: 'mt5', labelKey: 'status.mt5', shortKey: 'status.mt5', hintKey: 'status.mt5_hint' },
  { key: 'telegram', labelKey: 'status.telegram', shortKey: 'status.telegram_short', hintKey: 'status.telegram_hint' },
  { key: 'provider', labelKey: 'status.provider', shortKey: 'status.provider_short', hintKey: 'status.provider_hint' },
] as const;

export interface StatusDotsProps {
  /** `HealthOut`, `TradingStateOut`, or a bare `StatusMap`. */
  status?: StatusSource | null;
  /** No answer yet: every dot reads "Checking…" rather than lying about being down. */
  loading?: boolean;
  /** When the status was last refreshed; appended to every tooltip. */
  lastCheckAt?: number | string | Date | null;
  /**
   * Header density: short labels, no state-word chips. The word still rides in
   * the tooltip, so a glance at colour is never the only carrier.
   */
  compact?: boolean;
  className?: string;
}

export function StatusDots(props: StatusDotsProps): ReactElement {
  const t = useT();
  const locale = useCurrentLocale();

  const map = toStatusMap(props.status);
  const loading = props.loading === true && map === null;
  const compact = props.compact === true;
  const checked =
    props.lastCheckAt === null || props.lastCheckAt === undefined
      ? null
      : formatRelative(props.lastCheckAt, locale);

  const groupClass = compact
    ? 'row row--tight sp-dots sp-dots--compact'
    : 'row row--wrap row--wide sp-dots';

  return (
    <div
      className={props.className ? `${groupClass} ${props.className}` : groupClass}
      role="group"
      aria-label={t('status.title')}
    >
      {DOTS.map((spec) => {
        const state = map ? map[spec.key] : null;
        const tone = toneFor(state, loading, t);
        const label = t(compact ? spec.shortKey : spec.labelKey);

        const lines: string[] = [`${t(spec.labelKey)} — ${tone.word}`, t(spec.hintKey)];
        if (state && !state.ok && state.error) {
          lines.push(t('status.error_detail', { detail: state.error }));
        }
        if (checked) lines.push(`${t('status.last_check')}: ${checked}`);

        return (
          <span className="row row--tight" key={spec.key} title={lines.join('\n')}>
            <span className={`dot ${tone.dotClass}`}>{label}</span>
            {compact ? null : <span className={`badge ${tone.badgeClass}`}>{tone.word}</span>}
          </span>
        );
      })}
    </div>
  );
}

export default StatusDots;
