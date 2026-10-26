/**
 * LV Copytrader — ToolCallCard.
 *
 * One tool invocation, as the operator sees it in the live-reasoning column
 * (BUILD_PROMPT §9, §5.2/§5.3).
 *
 *   header  the tool name in `.mono` plus a status chip
 *   body    the arguments as a compact key/value list — formatted, NOT a raw
 *           JSON dump — with an expandable raw view kept one click away
 *   footer  the result, colour-coded by outcome:
 *             ok true  -> an --ok / --long tint
 *             ok false -> a --short tint (a refusal, not a crash: BUILD §5.3)
 *             pending  -> --text-dim
 *
 * WRITE TOOLS GET A HEAVIER TREATMENT THAN READ TOOLS. `open_market`,
 * `place_pending`, `modify_position`, `modify_order`, `close_position`,
 * `cancel_order`, `kill_switch` and `notify` touch the world; everything else
 * only looks at it. The heavy variant carries a 3px --warn rule, a filled
 * header and an uppercase marker, so a glance down the column separates
 * "the AI looked" from "the AI acted".
 *
 * A result carrying `retcode` / `retcode_name` prints BOTH — the number is what
 * the broker said, the name is what it means — and a refusal `message` renders
 * as plain readable prose rather than JSON, because *"not enough free margin:
 * need 412.30 USD, have 180.05"* is a sentence and should read like one.
 *
 * Nothing here may blow the column out: every long region is clamped behind an
 * expander.
 *
 * Rules honoured: no colour literal (every colour is a var(--token); `transparent`
 * and `color-mix()` over tokens are not palette values), every chrome string goes
 * through `t()`, every number carries `.mono`. Payload KEYS are printed verbatim
 * — they are the tool's real parameter names, and translating `sl` into "Stop
 * loss" would hide what the model actually sent.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import {
  EMPTY_VALUE,
  formatNumber,
  useCurrentLocale,
  useT,
  type Locale,
  type Translate,
} from '../i18n';

/* ────────────────────────────── public types ─────────────────────────────── */

/** Lifecycle of one tool call as the column knows it. */
export type ToolStatus = 'pending' | 'ok' | 'failed';

export interface ToolCallCardProps {
  /** The tool the model asked for, e.g. `open_market`. Printed verbatim in `.mono`. */
  name: string;
  /** The arguments the model chose. Free-form by contract (CONTRACTS §7 `RunStep.payload`). */
  args: Record<string, unknown>;
  /** `pending` until the executor answers. */
  status: ToolStatus;
  /** What the executor answered; `null` while pending. */
  result?: Record<string, unknown> | null;
  /** 1-based position in the run, rendered as `run.step`. */
  index?: number | null;
  /** The provider's tool_use id, shown in the raw view so a log line can be matched. */
  callId?: string | null;
  className?: string;
}

/**
 * Tools with side effects. Order tools plus `notify`: all of them leave a mark
 * outside this process, which is exactly what the heavier treatment announces.
 */
export const WRITE_TOOLS: ReadonlySet<string> = new Set([
  'open_market',
  'place_pending',
  'modify_position',
  'modify_order',
  'close_position',
  'cancel_order',
  'kill_switch',
  'notify',
]);

/** `finish` ends the run (BUILD §5.3) — neither a read nor a write. */
export const TERMINAL_TOOLS: ReadonlySet<string> = new Set(['finish']);

/* ───────────────────────────── payload helpers ───────────────────────────── */

/** Narrow an unknown payload member to a plain object. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Keys that carry a sentence and must render as prose, full width, wrapped. */
const PROSE_KEYS: ReadonlySet<string> = new Set([
  'reason',
  'message',
  'summary',
  'text',
  'hint',
  'description',
  'error',
  'note',
  'detail',
  'comment',
  'pip_convention',
]);

/** Keys handled by dedicated rows and therefore dropped from the generic list. */
const RESULT_SPECIAL_KEYS: ReadonlySet<string> = new Set([
  'ok',
  'message',
  'retcode',
  'retcode_name',
]);

/** A string long enough that it belongs on its own line rather than in a column. */
const PROSE_MIN_CHARS = 72;

/** Height in px at which the arguments list, the result body and the raw view clamp. */
const ARGS_CLAMP_PX = 200;
const RESULT_CLAMP_PX = 240;
const RAW_CLAMP_PX = 280;

/** How long the copy button reads "Copied" before returning to its label. */
const COPIED_RESET_MS = 1600;

/**
 * Pull the tool arguments out of a persisted `tool_call` step payload.
 *
 * The stream envelope is `{run_id, idx, id, name, input}` (CONTRACTS §4), but the
 * persisted `RunStep.payload` is documented free-form, so the shape is read
 * defensively and the payload itself is used when no wrapper key is present.
 */
export function extractToolArgs(payload: Record<string, unknown>): Record<string, unknown> {
  for (const key of ['input', 'arguments', 'args', 'params']) {
    const candidate = payload[key];
    if (isRecord(candidate)) return candidate;
  }
  return omitKeys(payload, ['run_id', 'idx', 'id', 'name', 'ok', 'type']);
}

/** The mirror of `extractToolArgs` for a persisted `tool_result` step. */
export function extractToolResult(payload: Record<string, unknown>): Record<string, unknown> {
  for (const key of ['result', 'output', 'content']) {
    const candidate = payload[key];
    if (isRecord(candidate)) return candidate;
  }
  return omitKeys(payload, ['run_id', 'idx', 'id', 'name', 'type']);
}

/** The tool_use id of a persisted step, when it carries one. */
export function extractToolCallId(payload: Record<string, unknown>): string | null {
  const id = payload.id ?? payload.tool_use_id ?? payload.tool_call_id;
  return typeof id === 'string' && id ? id : null;
}

function omitKeys(
  source: Record<string, unknown>,
  keys: readonly string[],
): Record<string, unknown> {
  const drop = new Set(keys);
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(source)) {
    if (drop.has(key)) continue;
    out[key] = value;
  }
  return out;
}

/**
 * Render one scalar for the cockpit.
 *
 * Numbers are ungrouped and capped at eight fraction digits: a price is read
 * against the row above it, and a thousands separator that appears at 10 000 and
 * vanishes at 9 999 makes a streaming column jump.
 */
function formatScalar(value: unknown, locale: Locale, t: Translate): string {
  if (value === null || value === undefined) return EMPTY_VALUE;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return EMPTY_VALUE;
    return formatNumber(value, { maximumFractionDigits: 8, useGrouping: false }, locale);
  }
  if (typeof value === 'boolean') return value ? t('app.yes') : t('app.no');
  if (typeof value === 'string') return value === '' ? EMPTY_VALUE : value;
  if (typeof value === 'bigint') return value.toString();
  return JSON.stringify(value) ?? EMPTY_VALUE;
}

/** True when the value renders as one short token rather than a block. */
function isScalar(value: unknown): boolean {
  return (
    value === null ||
    value === undefined ||
    typeof value === 'number' ||
    typeof value === 'boolean' ||
    typeof value === 'bigint' ||
    typeof value === 'string'
  );
}

/** A `side` argument is never colour alone — it prints ▲ BUY / ▼ SELL (BUILD §11). */
function sideBadge(value: unknown, t: Translate): ReactElement | null {
  if (typeof value !== 'string') return null;
  const v = value.trim().toLowerCase();
  if (v === 'buy' || v === 'long') {
    return <span className="badge badge--long">{t('trade.buy_arrow')}</span>;
  }
  if (v === 'sell' || v === 'short') {
    return <span className="badge badge--short">{t('trade.sell_arrow')}</span>;
  }
  return null;
}

const SIDE_KEYS: ReadonlySet<string> = new Set(['side', 'direction', 'position_side']);

/* ──────────────────────────────── clamp box ──────────────────────────────── */

export interface ClampBoxProps {
  /** Collapsed height in pixels. */
  maxPx: number;
  /** Anything that changes the measured height; re-measures when it changes. */
  signature?: unknown;
  children: ReactNode;
}

/**
 * Clamp a region and offer an expander only when it actually overflows.
 *
 * The overflow flag is measured while collapsed and then kept: once expanded the
 * region no longer overflows, and a naive re-measure would delete the control
 * that got the operator there.
 */
export function ClampBox({ maxPx, signature, children }: ClampBoxProps): ReactElement {
  const t = useT();
  const ref = useRef<HTMLDivElement | null>(null);
  const [overflowing, setOverflowing] = useState(false);
  const [expanded, setExpanded] = useState(false);

  useLayoutEffect(() => {
    if (expanded) return;
    const el = ref.current;
    if (!el) return;
    setOverflowing(el.scrollHeight - el.clientHeight > 2);
  }, [maxPx, signature, expanded, children]);

  const clamped = overflowing && !expanded;

  return (
    <div className="sp-clamp">
      <style href="sp-clampbox" precedence="sp-component">{CLAMP_CSS}</style>
      <div
        ref={ref}
        className={clamped ? 'sp-clamp__view is-clamped' : 'sp-clamp__view'}
        style={expanded ? undefined : { maxHeight: `${maxPx}px` }}
      >
        {children}
      </div>
      {overflowing ? (
        <button
          type="button"
          className="btn btn--ghost btn--sm sp-clamp__toggle"
          onClick={() => { setExpanded((v) => !v); }}
        >
          {expanded ? t('app.show_less') : t('app.show_more')}
        </button>
      ) : null}
    </div>
  );
}

/* ─────────────────────────────── key / value ─────────────────────────────── */

interface KeyValueListProps {
  data: Record<string, unknown>;
  /** Nesting depth; beyond `MAX_KV_DEPTH` a value degrades to compact JSON. */
  depth?: number;
}

const MAX_KV_DEPTH = 2;

/**
 * The compact key/value list.
 *
 * Keys print verbatim in a dim monospace face: they are protocol identifiers the
 * model chose, not interface copy, and a translated label would misreport what
 * was sent. Values are formatted — numbers tabular, booleans localised, a `side`
 * carrying its arrow badge — with arrays laid out as chips and nested objects
 * indented one level before degrading to JSON.
 */
function KeyValueList({ data, depth = 0 }: KeyValueListProps): ReactElement | null {
  const t = useT();
  const locale = useCurrentLocale();
  const entries = Object.entries(data).filter(([, value]) => value !== undefined);

  if (entries.length === 0) return null;

  return (
    <dl className="sp-kv">
      {entries.map(([key, value]) => {
        const prose =
          PROSE_KEYS.has(key) || (typeof value === 'string' && value.length >= PROSE_MIN_CHARS);
        const side = SIDE_KEYS.has(key) ? sideBadge(value, t) : null;

        let body: ReactNode;
        if (side) {
          body = side;
        } else if (Array.isArray(value)) {
          body =
            value.length === 0 ? (
              <span className="sp-kv__empty">{t('app.none')}</span>
            ) : (
              <span className="sp-kv__chips">
                {value.map((item, i) =>
                  isScalar(item) ? (
                    <span className="sp-kv__chip mono" key={`${key}-${String(i)}`}>
                      {formatScalar(item, locale, t)}
                    </span>
                  ) : (
                    <code className="sp-kv__chip mono" key={`${key}-${String(i)}`}>
                      {JSON.stringify(item)}
                    </code>
                  ),
                )}
              </span>
            );
        } else if (isRecord(value)) {
          body =
            depth < MAX_KV_DEPTH ? (
              <div className="sp-kv__nested">
                <KeyValueList data={value} depth={depth + 1} />
              </div>
            ) : (
              <code className="sp-kv__json mono">{JSON.stringify(value)}</code>
            );
        } else if (prose) {
          body = <span className="sp-kv__prose">{formatScalar(value, locale, t)}</span>;
        } else {
          body = <span className="mono sp-kv__num">{formatScalar(value, locale, t)}</span>;
        }

        return (
          <div className={prose || isRecord(value) ? 'sp-kv__row sp-kv__row--wide' : 'sp-kv__row'} key={key}>
            <dt className="sp-kv__key mono">{key}</dt>
            <dd className="sp-kv__val">{body}</dd>
          </div>
        );
      })}
    </dl>
  );
}

/* ───────────────────────────────── result ────────────────────────────────── */

interface ResultBodyProps {
  result: Record<string, unknown>;
}

/**
 * The result region.
 *
 * `message` leads, as prose — a refusal is a sentence written for a reader.
 * `retcode` and `retcode_name` get a dedicated row printing both. Everything
 * else falls through to the generic list.
 */
function ResultBody({ result }: ResultBodyProps): ReactElement {
  const t = useT();
  const locale = useCurrentLocale();

  const message = typeof result.message === 'string' ? result.message.trim() : '';
  const retcode = typeof result.retcode === 'number' ? result.retcode : null;
  const retcodeName = typeof result.retcode_name === 'string' ? result.retcode_name : null;
  const rest = useMemo(
    () => omitKeys(result, [...RESULT_SPECIAL_KEYS]),
    [result],
  );

  return (
    <div className="sp-tool__resultbody">
      {message ? <p className="sp-tool__message">{message}</p> : null}

      {retcode !== null || retcodeName ? (
        <p className="sp-tool__retcode">
          {retcode !== null ? (
            <span className="mono sp-tool__retcode-num">
              {formatNumber(retcode, { useGrouping: false }, locale)}
            </span>
          ) : null}
          {retcodeName ? <span className="badge badge--dim">{retcodeName}</span> : null}
        </p>
      ) : null}

      {Object.keys(rest).length > 0 ? <KeyValueList data={rest} /> : null}

      {!message && retcode === null && !retcodeName && Object.keys(rest).length === 0 ? (
        <p className="sp-tool__message sp-tool__message--dim">{t('app.none')}</p>
      ) : null}
    </div>
  );
}

/* ───────────────────────────────── the card ──────────────────────────────── */

/**
 * One tool invocation.
 *
 * Stable across a streaming update: the component re-renders with new props and
 * never remounts, so an open raw view stays open while a result lands.
 */
export function ToolCallCard(props: ToolCallCardProps): ReactElement {
  const { name, args, status, result = null, index = null, callId = null, className } = props;
  const t = useT();

  const heavy = WRITE_TOOLS.has(name);
  const terminal = TERMINAL_TOOLS.has(name);

  const [rawOpen, setRawOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const raw = useMemo(
    () => JSON.stringify({ name, id: callId, input: args, result }, null, 2),
    [name, callId, args, result],
  );

  const copy = useCallback(() => {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
    if (!clipboard || typeof clipboard.writeText !== 'function') return;
    clipboard.writeText(raw).then(
      () => { setCopied(true); },
      () => { /* a denied clipboard permission is not worth an alarm */ },
    );
  }, [raw]);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => { setCopied(false); }, COPIED_RESET_MS);
    return () => { window.clearTimeout(timer); };
  }, [copied]);

  const chipClass =
    status === 'ok' ? 'badge badge--ok' : status === 'failed' ? 'badge badge--short' : 'badge badge--dim';
  const chipLabel =
    status === 'ok' ? t('run.result_ok') : status === 'failed' ? t('run.result_failed') : t('run.running');

  const classes = [
    'sp-tool',
    `sp-tool--${status}`,
    heavy ? 'sp-tool--write' : terminal ? 'sp-tool--terminal' : 'sp-tool--read',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  const hasArgs = Object.keys(args).length > 0;

  return (
    <article className={classes} data-tool={name}>
      <style href="sp-toolcallcard" precedence="sp-component">{TOOL_CALL_CSS}</style>

      <header className="sp-tool__head">
        {index !== null ? (
          <span className="sp-tool__idx mono" title={t('run.step', { index })}>
            {index}
          </span>
        ) : null}
        <span className="sp-tool__name mono" title={t('run.tool_call')}>
          {name}
        </span>
        {heavy ? (
          <span className="badge badge--warn sp-tool__marker">{t('app.actions')}</span>
        ) : null}
        <span className="spacer" />
        <span className={chipClass}>{chipLabel}</span>
      </header>

      {hasArgs ? (
        <div className="sp-tool__section">
          <p className="sp-tool__legend">{t('run.arguments')}</p>
          <ClampBox maxPx={ARGS_CLAMP_PX} signature={args}>
            <KeyValueList data={args} />
          </ClampBox>
        </div>
      ) : null}

      <footer className={`sp-tool__result sp-tool__result--${status}`}>
        <p className="sp-tool__legend">{t('run.tool_result')}</p>
        {status === 'pending' || !result ? (
          <p className="sp-tool__message sp-tool__message--dim">{t('run.running')}</p>
        ) : (
          <ClampBox maxPx={RESULT_CLAMP_PX} signature={result}>
            <ResultBody result={result} />
          </ClampBox>
        )}
      </footer>

      <div className="sp-tool__tools">
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          aria-expanded={rawOpen}
          onClick={() => { setRawOpen((v) => !v); }}
        >
          {rawOpen ? t('app.collapse') : t('app.details')}
        </button>
        <button type="button" className="btn btn--ghost btn--sm" onClick={copy}>
          {copied ? t('app.copied') : t('run.copy_json')}
        </button>
      </div>

      {rawOpen ? (
        <ClampBox maxPx={RAW_CLAMP_PX} signature={raw}>
          <pre className="sp-tool__raw mono">{raw}</pre>
        </ClampBox>
      ) : null}
    </article>
  );
}

export default ToolCallCard;

/* ──────────────────────────────── styles ─────────────────────────────────── */

/**
 * Component-scoped CSS, hoisted and de-duplicated by React 19's `<style href
 * precedence>` support, so fifty cards in a column still emit one rule set.
 * Every colour is a token; `transparent` and `color-mix()` over tokens carry no
 * palette value of their own.
 */
const TOOL_CALL_CSS = `
.sp-tool {
  --sp-clamp-bg: var(--bg);
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: transparent;
  border: 0;
  border-left: 3px solid var(--border);
  border-radius: 0;
  overflow: hidden;
  padding: var(--sp-2) 0 var(--sp-2) var(--sp-3);
}

/*
 * THE ENTER. A tool call arrives in three beats, in the order it happens: the
 * model names the tool, then the arguments it chose, then — usually a second
 * or two later — the executor answers. Fading the whole card in as one
 * rectangle throws that order away. 90ms apart, 4px of travel each.
 *
 * The result footer is the SAME node throughout: pending becomes ok without a
 * remount, so this plays once, when the call appears, and never again when the
 * answer lands.
 */
.sp-tool__head {
  animation: sp-chunk-in var(--dur) var(--ease) both;
}

.sp-tool__section {
  animation: sp-chunk-in var(--dur) var(--ease) 90ms both;
}

.sp-tool__result,
.sp-tool__tools {
  animation: sp-chunk-in var(--dur) var(--ease) 180ms both;
}

/* Write tools touch the account: a --warn rule, not another box. */
.sp-tool--write {
  border-left-color: var(--warn);
}

.sp-tool--terminal {
  border-left-color: var(--accent);
}

.sp-tool__head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-height: var(--touch);
  padding: 0;
  background: transparent;
  border-bottom: 0;
}

.sp-tool--write .sp-tool__head {
  background: transparent;
}

.sp-tool__idx {
  flex: none;
  min-width: 2ch;
  font-size: var(--fs-1);
  color: var(--text-dim);
  text-align: right;
}

.sp-tool__name {
  font-size: var(--fs-2);
  font-weight: 600;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sp-tool--write .sp-tool__name {
  color: var(--warn);
}

.sp-tool .sp-tool__marker {
  height: 18px;
  padding: 0 var(--sp-1);
  letter-spacing: 0.06em;
}

.sp-tool__section,
.sp-tool__result {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2);
  min-width: 0;
}

.sp-tool__result {
  border-top: var(--border-w) solid var(--border);
}

/* Outcome tint. color-mix keeps the wash tied to the same token in both themes. */
.sp-tool__result--ok {
  background: color-mix(in srgb, var(--ok) 8%, transparent);
}

.sp-tool__result--failed {
  background: color-mix(in srgb, var(--short) 10%, transparent);
}

.sp-tool__result--pending {
  background: transparent;
}

.sp-tool__legend {
  font-size: var(--fs-1);
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-dim);
}

.sp-tool__message {
  font-size: var(--fs-2);
  line-height: var(--lh);
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.sp-tool__result--failed .sp-tool__message {
  color: var(--short);
  font-weight: 500;
}

.sp-tool__message--dim {
  color: var(--text-dim);
  font-weight: 400;
}

.sp-tool__retcode {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.sp-tool__retcode-num {
  font-size: var(--fs-2);
  font-weight: 600;
  color: var(--text-dim);
}

.sp-tool__resultbody {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}

/*
 * var(--sp-2) of padding-top, not 0: the result region above ends with its own
 * expander, and both rows carry the 40px skirt from .btn--sm. 8px there plus
 * 8px here is exactly the 16px that keeps the two targets from overlapping.
 */
.sp-tool__tools {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-2) var(--sp-2) var(--sp-1);
}

/* The gutter belongs to the wrapper, not to the <pre>: the expander under it
   is a sibling, and with the margin here it sat hard against the card's border
   with half its hit target clipped by the card's own overflow. */
.sp-tool > .sp-clamp {
  padding: 0 var(--sp-2) var(--sp-2);
  /* Not part of the card's own enter — it mounts when it is asked for, so it
     arrives on the same 4px of travel by itself. */
  animation: sp-chunk-in var(--dur) var(--ease) both;
}

.sp-tool__raw {
  margin: 0;
  padding: var(--sp-2);
  background: var(--surface-2);
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--fs-1);
  line-height: var(--lh);
  color: var(--text-dim);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* ---- key / value list ---- */

.sp-kv {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}

.sp-kv__row {
  display: grid;
  grid-template-columns: minmax(0, 13ch) minmax(0, 1fr);
  align-items: baseline;
  gap: var(--sp-2);
  min-width: 0;
}

.sp-kv__row--wide {
  grid-template-columns: minmax(0, 1fr);
  gap: 0;
}

.sp-kv__key {
  font-size: var(--fs-1);
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sp-kv__val {
  font-size: var(--fs-2);
  color: var(--text);
  min-width: 0;
}

.sp-kv__num {
  font-size: var(--fs-2);
  overflow-wrap: anywhere;
}

.sp-kv__prose {
  display: block;
  line-height: var(--lh);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.sp-kv__empty {
  color: var(--text-dim);
}

.sp-kv__chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
}

.sp-kv__chip {
  padding: 0 var(--sp-1);
  font-size: var(--fs-1);
  color: var(--text);
  background: var(--surface-2);
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-sm);
}

.sp-kv__json {
  font-size: var(--fs-1);
  color: var(--text-dim);
  overflow-wrap: anywhere;
}

.sp-kv__nested {
  padding-left: var(--sp-2);
  border-left: var(--border-w) solid var(--border);
  margin-top: var(--sp-1);
}
`;

/**
 * `ClampBox`'s own rules, hoisted separately so the primitive works anywhere —
 * a signal card's message text uses it with no ToolCallCard on the page.
 * `--sp-clamp-bg` lets the host name the colour the fade resolves into; it
 * falls back to `--surface`, which is what every data surface here sits on.
 */
const CLAMP_CSS = `
.sp-clamp {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-1);
  min-width: 0;
}

.sp-clamp__view {
  position: relative;
  width: 100%;
  min-width: 0;
  overflow: hidden;
}

.sp-clamp__view.is-clamped::after {
  content: "";
  position: absolute;
  inset-inline: 0;
  bottom: 0;
  height: var(--sp-5);
  background: linear-gradient(to bottom, transparent, var(--sp-clamp-bg, var(--surface)));
  pointer-events: none;
}

.sp-clamp .sp-clamp__toggle {
  align-self: flex-start;
}
`;
