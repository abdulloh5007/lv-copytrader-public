/**
 * LV Copytrader — ThinkingBlock.
 *
 * The model's reasoning, rendered so a glance separates deliberation from action
 * (BUILD_PROMPT §11):
 *
 *   colour      var(--thinking)
 *   size        13px  (var(--fs-2))
 *   treatment   a LEFT RULE — border-left in --thinking — and nothing else;
 *               no card, no fill, no border on the other three sides, which is
 *               what makes it read differently from a ToolCallCard at speed.
 *
 * STREAMING WITHOUT REMOUNTING. `run.thinking` arrives as a stream of deltas
 * (CONTRACTS §4). The caller accumulates them and passes the whole string down;
 * this component renders it as ONE text child of ONE stable `<p>`, so React
 * patches the text node in place. Nothing here is keyed by content, nothing
 * unmounts between deltas, and the browser therefore never loses the selection,
 * the scroll position or the caret animation mid-thought.
 *
 * COLLAPSE. A long deliberation would push every tool call off the screen, so
 * the block clamps and offers "show more". While the run is live it is expanded
 * — the operator is watching it arrive — and it collapses on its own once the
 * run ends, unless the operator has said otherwise, in which case their choice
 * wins for good.
 *
 * The type here is deliberately NOT tabular: `.mono`/`tabular-nums` belongs to
 * prices and tickets, and proportional figures read better inside prose
 * (base.css says so where `.mono` is declared).
 */
import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactElement } from 'react';

import { useT } from '../i18n';

/* ────────────────────────────── public types ─────────────────────────────── */

export interface ThinkingBlockProps {
  /** The reasoning accumulated so far. Grows delta by delta; never re-keyed. */
  text: string;
  /** True while `run.thinking` deltas are still arriving: shows the caret, stays open. */
  live?: boolean;
  /** Collapsed height in pixels. Default `THINKING_CLAMP_PX`. */
  collapsedPx?: number;
  /** Heading above the rule. Defaults to `t('run.thinking')`; pass `null` to drop it. */
  label?: string | null;
  className?: string;
}

/** Roughly eight lines at 13px/1.5 — enough to judge the shape of a thought. */
export const THINKING_CLAMP_PX = 156;

/* ─────────────────────────────── component ───────────────────────────────── */

export function ThinkingBlock(props: ThinkingBlockProps): ReactElement {
  const { text, live = false, collapsedPx = THINKING_CLAMP_PX, label, className } = props;
  const t = useT();

  const viewRef = useRef<HTMLDivElement | null>(null);
  const [overflowing, setOverflowing] = useState(false);
  /** `null` means "follow the run": open while live, collapsed once it ends. */
  const [override, setOverride] = useState<boolean | null>(null);

  const expanded = override ?? live;

  useLayoutEffect(() => {
    if (expanded) return;
    const el = viewRef.current;
    if (!el) return;
    setOverflowing(el.scrollHeight - el.clientHeight > 2);
  }, [text, expanded, collapsedPx]);

  const trimmed = useMemo(() => text.trimEnd(), [text]);
  const empty = trimmed.length === 0;

  const heading = label === null ? null : (label ?? t('run.thinking'));
  const clamped = overflowing && !expanded;

  const classes = ['sp-think', live ? 'is-live' : null, className].filter(Boolean).join(' ');

  return (
    <section className={classes} aria-live={live ? 'polite' : 'off'} aria-busy={live}>
      <style href="sp-thinkingblock" precedence="sp-component">{THINKING_CSS}</style>

      {heading !== null ? (
        <p className="sp-think__label">
          {heading}
          {live ? <span className="sp-think__pulse" aria-hidden="true" /> : null}
        </p>
      ) : null}

      <div
        ref={viewRef}
        className={clamped ? 'sp-think__view is-clamped' : 'sp-think__view'}
        style={expanded ? undefined : { maxHeight: `${collapsedPx}px` }}
      >
        {/*
          One paragraph, one text child, never re-keyed: this is the node the
          deltas patch in place. `empty` swaps the class rather than the element
          so the first delta does not remount the paragraph.
        */}
        <p className={empty ? 'sp-think__text is-empty' : 'sp-think__text'}>
          {empty ? (live ? t('run.waiting') : t('run.no_thinking')) : trimmed}
          {live && !empty ? <span className="sp-think__caret" aria-hidden="true" /> : null}
        </p>
      </div>

      {overflowing ? (
        <button
          type="button"
          className="btn btn--ghost btn--sm sp-think__toggle"
          aria-expanded={expanded}
          onClick={() => { setOverride(!expanded); }}
        >
          {expanded ? t('app.show_less') : t('app.show_more')}
        </button>
      ) : null}
    </section>
  );
}

export default ThinkingBlock;

/* ──────────────────────────────── styles ─────────────────────────────────── */

/**
 * Hoisted and de-duplicated by React 19's `<style href precedence>`, so a run
 * with a dozen thinking blocks still emits one rule set. Every colour is a
 * token; `transparent` and `color-mix()` over tokens are not palette values.
 */
const THINKING_CSS = `
.sp-think {
  --sp-think-bg: var(--surface);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-1);
  min-width: 0;
  padding-left: var(--sp-3);
  /* The left rule IS the treatment — no card, no fill, no other border. */
  border-left: 2px solid var(--thinking);
}

.sp-think__label {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-1);
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--thinking);
}

.sp-think__pulse {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--thinking);
  animation: sp-think-pulse 1.4s var(--ease) infinite;
}

.sp-think__view {
  position: relative;
  width: 100%;
  min-width: 0;
  overflow: hidden;
}

.sp-think__view.is-clamped::after {
  content: "";
  position: absolute;
  inset-inline: 0;
  bottom: 0;
  height: var(--sp-4);
  background: linear-gradient(to bottom, transparent, var(--sp-think-bg));
  pointer-events: none;
}

.sp-think__text {
  font-family: var(--font-ui);
  font-size: var(--fs-2);
  line-height: var(--lh);
  color: var(--thinking);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  /* Deliberately proportional: tabular figures belong to prices, not to prose. */
  font-variant-numeric: normal;
}

.sp-think__text.is-empty {
  color: var(--text-dim);
  font-style: italic;
}

.sp-think__caret {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--thinking);
  animation: sp-think-caret 1s steps(2, start) infinite;
}

.sp-think .sp-think__toggle {
  align-self: flex-start;
  color: var(--thinking);
}

@keyframes sp-think-pulse {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.25; }
}

@keyframes sp-think-caret {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0; }
}
`;
