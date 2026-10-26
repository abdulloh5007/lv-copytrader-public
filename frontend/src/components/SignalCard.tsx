/**
 * LV Copytrader — SignalCard.
 *
 * One row of the incoming-message column (BUILD_PROMPT §9).
 *
 * What the operator must be able to read without clicking anything:
 *   - the message text, exactly as the channel wrote it, newlines kept;
 *   - a KIND BADGE for every one of the thirteen classified kinds — the set is
 *     closed, so the feed never shows a raw Telethon class name (BUILD §3.2);
 *   - image thumbnails, opening full-size in a lightbox;
 *   - the age — "3m ago" — and a --warn STALE badge when the message reached the
 *     model later than the freshness threshold (BUILD §5.4);
 *   - a REVISION N badge when the channel edited the message: an edit is a
 *     revision, never a fresh signal (BUILD §3.3);
 *   - a reply affordance pointing at the parent message, because a reply is how
 *     a channel says "this one hit TP1";
 *   - a CATCH-UP badge for anything replayed after downtime (BUILD §8);
 *   - the t.me permalink as an external link when the chat type has one.
 *
 * SKIPPED MESSAGES RENDER GREYED WITH THEIR REASON VISIBLE. That is the whole
 * point of storing them: a voice note or a sticker cost zero provider calls, and
 * the operator can see for themselves that nothing was lost. The greying is done
 * with colour tokens rather than `opacity`, so the reason itself stays readable
 * — an unreadable explanation would defeat the feature.
 *
 * Clicking the card selects it; the selected card renders whatever the feed
 * passes as `children`, which is its runs.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent, ReactElement, ReactNode } from 'react';

import { tgImageUrl, type ImageOut, type MessageKind, type SignalOut } from '../api/client';
import {
  formatDateTime,
  formatDuration,
  formatRelative,
  toDate,
  useCurrentLocale,
  useT,
  type TKey,
} from '../i18n';

/* ────────────────────────────── public types ─────────────────────────────── */

export interface SignalCardProps {
  signal: SignalOut;
  /** Highlights the card and reveals `children`. */
  selected?: boolean;
  /** Fired when the card is clicked or its message region is activated. */
  onSelect?: (signal: SignalOut) => void;
  /**
   * Turns the reply affordance into a control that jumps to the parent.
   * Receives the parent's Telegram `message_id`, not a database row id.
   */
  onOpenParent?: (messageId: number, signal: SignalOut) => void;
  /** Shown under the message while the card is selected — the feed passes its runs. */
  children?: ReactNode;
  /** Clock for the age readout; the feed ticks one and shares it across every card. */
  now?: number;
  /**
   * True for a message that ARRIVED while the operator was watching, which is
   * the only case that plays the card's staggered enter. A card that was in the
   * first page of a fetch did not arrive — it was already there — and animating
   * sixty of those on mount is a wave, not an arrival.
   */
  entering?: boolean;
  className?: string;
}

/**
 * Mirrors `STALE_SIGNAL_SECONDS` in `app/config.py` (CONTRACTS §1).
 *
 * The backend applies this threshold to `signal_age_seconds` when it builds the
 * run context and tells the model `stale: true`; the badge here reports the same
 * verdict about the same number, so the feed and the model never disagree.
 */
export const STALE_SIGNAL_SECONDS = 600;

/** Collapsed height of the message text, in pixels. */
const TEXT_CLAMP_PX = 132;

/* ─────────────────────────────── small hooks ─────────────────────────────── */

/**
 * A clock that re-renders on an interval. `intervalMs <= 0` disables it, which is
 * how a card defers to a `now` its parent already ticks.
 */
export function useTicker(intervalMs: number): number {
  const [now, setNow] = useState<number>(() => Date.now());

  useEffect(() => {
    if (!Number.isFinite(intervalMs) || intervalMs <= 0) return;
    const id = window.setInterval(() => { setNow(Date.now()); }, intervalMs);
    return () => { window.clearInterval(id); };
  }, [intervalMs]);

  return now;
}

/* ─────────────────────────────── derivations ─────────────────────────────── */

/**
 * How old the message was when LV Copytrader took delivery of it, in seconds.
 *
 * This — not "how old is it now" — is the number the backend hands the model, so
 * it is the number the STALE badge must judge. A catch-up message fetched forty
 * minutes after it was posted is stale forever, which is exactly right: it was
 * forty minutes old when the model read it.
 */
export function signalIngestAgeSeconds(signal: SignalOut, nowMs: number): number | null {
  const posted = toDate(signal.posted_at);
  if (!posted) return null;
  const received = toDate(signal.received_at);
  const reference = received ? received.getTime() : nowMs;
  return Math.max(0, (reference - posted.getTime()) / 1000);
}

/** True when the backend would have flagged this message `stale` in the context. */
export function isStaleSignal(signal: SignalOut, nowMs: number): boolean {
  const age = signalIngestAgeSeconds(signal, nowMs);
  return age !== null && age > STALE_SIGNAL_SECONDS;
}

/** The dictionary key for a message kind. The set is closed; `unsupported` catches the rest. */
const KIND_KEYS: Record<MessageKind, TKey> = {
  text: 'feed.kind.text',
  photo: 'feed.kind.photo',
  photo_caption: 'feed.kind.photo_caption',
  voice: 'feed.kind.voice',
  video: 'feed.kind.video',
  video_note: 'feed.kind.video_note',
  audio: 'feed.kind.audio',
  sticker: 'feed.kind.sticker',
  gif: 'feed.kind.gif',
  document: 'feed.kind.document',
  service: 'feed.kind.service',
  empty: 'feed.kind.empty',
  unsupported: 'feed.kind.unsupported',
};

/** The three kinds the agent actually reads (BUILD §3.2). */
const PROCESSABLE_KINDS: ReadonlySet<MessageKind> = new Set(['text', 'photo', 'photo_caption']);

export function kindKey(kind: MessageKind | string): TKey {
  return KIND_KEYS[kind as MessageKind] ?? 'feed.kind.unsupported';
}

/* ──────────────────────────────── lightbox ───────────────────────────────── */

interface LightboxProps {
  images: ImageOut[];
  index: number;
  onIndex: (index: number) => void;
  onClose: () => void;
}

/**
 * Full-size viewer. Escape closes, the arrow keys page, the backdrop closes on a
 * click that started and ended on the backdrop itself.
 */
function Lightbox({ images, index, onIndex, onClose }: LightboxProps): ReactElement | null {
  const t = useT();
  const count = images.length;

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      } else if (event.key === 'ArrowRight' && count > 1) {
        event.preventDefault();
        onIndex((index + 1) % count);
      } else if (event.key === 'ArrowLeft' && count > 1) {
        event.preventDefault();
        onIndex((index - 1 + count) % count);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => { window.removeEventListener('keydown', onKey); };
  }, [count, index, onIndex, onClose]);

  const current = images[index];
  if (!current) return null;

  return (
    <div
      className="modal__backdrop sp-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label={t('feed.image_alt', { index: index + 1 })}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="sp-lightbox__frame">
        <img
          className="sp-lightbox__img"
          src={tgImageUrl(current.id)}
          alt={t('feed.image_alt', { index: index + 1 })}
          width={current.width ?? undefined}
          height={current.height ?? undefined}
        />
        <div className="sp-lightbox__bar">
          {count > 1 ? (
            <>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => { onIndex((index - 1 + count) % count); }}
              >
                {t('app.previous')}
              </button>
              <span className="mono sp-lightbox__counter">
                {index + 1}/{count}
              </span>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => { onIndex((index + 1) % count); }}
              >
                {t('app.next')}
              </button>
            </>
          ) : null}
          <span className="spacer" />
          <button type="button" className="btn btn--sm" onClick={onClose}>
            {t('app.close')}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ─────────────────────────────── the card ────────────────────────────────── */

export function SignalCard(props: SignalCardProps): ReactElement {
  const {
    signal,
    selected = false,
    onSelect,
    onOpenParent,
    children,
    now,
    entering = false,
    className,
  } = props;
  const t = useT();
  const locale = useCurrentLocale();

  // Own the clock only when the parent does not tick one.
  const internalNow = useTicker(now === undefined ? 30_000 : 0);
  const nowMs = now ?? internalNow;

  const [lightbox, setLightbox] = useState<number | null>(null);

  // The message text clamps itself rather than borrowing `ClampBox`: the clamp
  // control has to sit OUTSIDE the selection button, because a button nested
  // inside a button is not valid HTML and browsers reparent it.
  const textRef = useRef<HTMLSpanElement | null>(null);
  const [textExpanded, setTextExpanded] = useState(false);
  const [textOverflows, setTextOverflows] = useState(false);

  const skipped = signal.status === 'skipped';
  const errored = signal.status === 'error';
  const inFlight = signal.status === 'pending' || signal.status === 'processing';
  const processable = PROCESSABLE_KINDS.has(signal.kind);

  const ingestAge = signalIngestAgeSeconds(signal, nowMs);
  const stale = ingestAge !== null && ingestAge > STALE_SIGNAL_SECONDS;

  const images = signal.images;
  const hiddenImages = Math.max(0, signal.image_count - images.length);

  const postedTitle = useMemo(
    () =>
      `${t('feed.posted_at')}: ${formatDateTime(signal.posted_at, locale)}\n${t(
        'feed.received_at',
      )}: ${formatDateTime(signal.received_at, locale)}`,
    [t, locale, signal.posted_at, signal.received_at],
  );

  useLayoutEffect(() => {
    if (textExpanded) return;
    const el = textRef.current;
    if (!el) return;
    setTextOverflows(el.scrollHeight - el.clientHeight > 2);
  }, [signal.text, textExpanded]);

  const select = useCallback(() => { onSelect?.(signal); }, [onSelect, signal]);

  const onCardClick = useCallback(
    (event: ReactMouseEvent<HTMLElement>) => {
      const target = event.target as HTMLElement | null;
      // Links, buttons and thumbnails own their own clicks.
      if (target?.closest('a, button, [role="button"]')) return;
      select();
    },
    [select],
  );

  const skipReason = skipped
    ? (signal.skip_reason ?? '').trim() || t(kindKey(signal.kind))
    : null;

  const classes = [
    'sp-sig',
    skipped ? 'sp-sig--skipped' : null,
    errored ? 'sp-sig--error' : null,
    selected ? 'is-selected' : null,
    entering ? 'is-entering' : null,
    className,
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <article
      className={classes}
      data-kind={signal.kind}
      data-status={signal.status}
      data-signal-id={signal.id}
      data-message-id={signal.message_id}
      aria-current={selected ? 'true' : undefined}
      onClick={onCardClick}
    >
      <style href="sp-signalcard" precedence="sp-component">{SIGNAL_CARD_CSS}</style>

      {/* ---- badges ---- */}
      <div className="sp-sig__badges">
        <span
          className={processable && !skipped ? 'badge badge--accent' : 'badge badge--dim'}
          title={t('feed.kind')}
        >
          {t(kindKey(signal.kind))}
        </span>

        {skipped ? (
          <span className="badge badge--dim" title={t('feed.skipped_hint')}>
            {t('feed.skipped')}
          </span>
        ) : null}

        {errored ? (
          <span className="badge badge--short">{t('feed.status.error')}</span>
        ) : null}

        {inFlight ? (
          <span className="badge badge--accent">
            {signal.status === 'processing' ? t('feed.status.processing') : t('feed.status.pending')}
          </span>
        ) : null}

        {signal.revision > 1 ? (
          <span className="badge badge--warn" title={t('feed.revision_hint')}>
            {t('feed.revision_n', { n: signal.revision })}
          </span>
        ) : null}

        {stale ? (
          <span
            className="badge badge--warn"
            title={`${t('feed.stale_hint')}\n${t('feed.age')}: ${formatDuration(ingestAge, locale)}`}
          >
            {t('feed.stale')}
          </span>
        ) : null}

        {signal.deleted_at ? (
          <span className="badge badge--short" title={t('feed.deleted_hint')}>
            {t('feed.deleted')}
          </span>
        ) : null}

        {signal.catchup ? (
          <span className="badge badge--dim" title={t('feed.catchup_hint')}>
            {t('feed.catchup')}
          </span>
        ) : null}

        <span className="spacer" />

        <time
          className="sp-sig__age mono"
          dateTime={signal.posted_at ?? undefined}
          title={postedTitle}
        >
          {formatRelative(signal.posted_at, locale, nowMs)}
        </time>
      </div>

      {/* ---- the message itself; this region is the selection control ---- */}
      <button
        type="button"
        className="sp-sig__hit"
        onClick={select}
        aria-pressed={selected}
        title={t('feed.view_signal')}
      >
        <span
          ref={textRef}
          className={
            textOverflows && !textExpanded ? 'sp-sig__textwrap is-clamped' : 'sp-sig__textwrap'
          }
          style={textExpanded ? undefined : { maxHeight: `${TEXT_CLAMP_PX}px` }}
        >
          <span className={signal.text.trim() ? 'sp-sig__text' : 'sp-sig__text is-empty'}>
            {signal.text.trim() || t('feed.text_empty')}
          </span>
        </span>
      </button>

      {textOverflows ? (
        <button
          type="button"
          className="btn btn--ghost btn--sm sp-sig__more"
          aria-expanded={textExpanded}
          onClick={() => { setTextExpanded((v) => !v); }}
        >
          {textExpanded ? t('app.show_less') : t('app.show_more')}
        </button>
      ) : null}

      {/* ---- why it was skipped: never greyed into illegibility ---- */}
      {skipReason ? (
        <p className="sp-sig__skip">{t('feed.skip_reason', { reason: skipReason })}</p>
      ) : null}

      {/* ---- images ---- */}
      {images.length > 0 ? (
        <div className="sp-sig__thumbs">
          {images.map((image, i) => (
            <button
              type="button"
              className="sp-sig__thumb"
              key={image.id}
              onClick={() => { setLightbox(i); }}
              title={t('feed.image_alt', { index: i + 1 })}
            >
              <img
                src={tgImageUrl(image.id)}
                alt={t('feed.image_alt', { index: i + 1 })}
                loading="lazy"
                decoding="async"
              />
            </button>
          ))}
          {hiddenImages > 0 ? (
            <span className="badge badge--dim sp-sig__more-images">
              {t('feed.image_count', { count: signal.image_count })}
            </span>
          ) : null}
        </div>
      ) : signal.image_count > 0 ? (
        <p className="sp-sig__meta-line">{t('feed.image_count', { count: signal.image_count })}</p>
      ) : null}

      {/* ---- footer ---- */}
      <div className="sp-sig__foot">
        <span className="sp-sig__id mono" title={t('feed.kind')}>
          #{signal.message_id}
        </span>

        {signal.reply_to_message_id !== null ? (
          onOpenParent ? (
            <button
              type="button"
              className="btn btn--ghost btn--sm sp-sig__reply"
              title={t('feed.reply_hint')}
              onClick={() => {
                if (signal.reply_to_message_id !== null) {
                  onOpenParent(signal.reply_to_message_id, signal);
                }
              }}
            >
              {t('feed.reply_to', { id: signal.reply_to_message_id })}
            </button>
          ) : (
            <span className="sp-sig__meta-line" title={t('feed.reply_hint')}>
              {t('feed.reply_to', { id: signal.reply_to_message_id })}
            </span>
          )
        ) : null}

        {signal.trades_count > 0 ? (
          <span className="badge badge--long">{t('feed.trades_linked', { count: signal.trades_count })}</span>
        ) : null}

        {signal.runs_count > 0 ? (
          <span className="badge badge--dim">
            {t('run.steps')}
            <span className="badge__num">{signal.runs_count}</span>
          </span>
        ) : null}

        <span className="spacer" />

        {signal.permalink ? (
          <a
            className="sp-sig__link"
            href={signal.permalink}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t('feed.open_in_telegram')}
          </a>
        ) : (
          <span className="sp-sig__meta-line" title={t('feed.permalink_missing')}>
            {t('feed.permalink_missing')}
          </span>
        )}
      </div>

      {/* ---- the selected card shows its runs ---- */}
      {selected && children ? <div className="sp-sig__runs">{children}</div> : null}

      {lightbox !== null ? (
        <Lightbox
          images={images}
          index={lightbox}
          onIndex={setLightbox}
          onClose={() => { setLightbox(null); }}
        />
      ) : null}
    </article>
  );
}

export default SignalCard;

/* ──────────────────────────────── styles ─────────────────────────────────── */

/**
 * Hoisted and de-duplicated by React 19's `<style href precedence>`: sixty cards
 * in the column still emit one rule set. Every colour is a var(--token).
 */
const SIGNAL_CARD_CSS = `
.sp-sig {
  --sp-clamp-bg: var(--surface);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
  padding: var(--sp-2) var(--sp-3);
  background: var(--surface);
  border-bottom: var(--border-w) solid var(--border);
  border-left: 2px solid transparent;
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}

/*
 * THE ARRIVAL. A message that lands while the operator is watching assembles
 * in the order it is read: what kind of message it is and how old, then what
 * it says, then what came of it. 90ms apart, 4px of travel — enough to see
 * the order, not enough to wait for it.
 *
 * is-entering is set by the feed for socket arrivals only, so a fetched page
 * of history renders at rest.
 */
.sp-sig.is-entering .sp-sig__badges {
  animation: sp-chunk-in var(--dur) var(--ease) both;
}

.sp-sig.is-entering .sp-sig__hit,
.sp-sig.is-entering .sp-sig__skip,
.sp-sig.is-entering .sp-sig__thumbs {
  animation: sp-chunk-in var(--dur) var(--ease) 90ms both;
}

.sp-sig.is-entering .sp-sig__foot {
  animation: sp-chunk-in var(--dur) var(--ease) 180ms both;
}

.sp-sig:hover {
  background: var(--surface-2);
}

.sp-sig.is-selected {
  background: var(--accent-dim);
  border-left-color: var(--accent);
}

/* Skipped: greyed with COLOUR, never with opacity — the reason has to stay
   readable, and opacity on the card cannot be undone by a child. */
.sp-sig--skipped {
  --sp-clamp-bg: var(--surface);
  background: var(--bg);
}

.sp-sig--skipped:hover {
  background: var(--surface);
}

.sp-sig--error {
  border-left-color: var(--short);
}

.sp-sig__badges {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
}

.sp-sig__age {
  flex: none;
  font-size: var(--fs-1);
  color: var(--text-dim);
  white-space: nowrap;
}

/* The message region is the accessible selection control: a real button, so the
   card is reachable from the keyboard without nesting interactive elements. */
.sp-sig__hit {
  display: block;
  width: 100%;
  min-width: 0;
  padding: 0;
  background: none;
  border: 0;
  text-align: start;
  cursor: pointer;
}

.sp-sig__textwrap {
  position: relative;
  display: block;
  width: 100%;
  min-width: 0;
  overflow: hidden;
}

.sp-sig__textwrap.is-clamped::after {
  content: "";
  position: absolute;
  inset-inline: 0;
  bottom: 0;
  height: var(--sp-4);
  background: linear-gradient(to bottom, transparent, var(--sp-clamp-bg, var(--surface)));
  pointer-events: none;
}

.sp-sig:hover .sp-sig__textwrap.is-clamped::after {
  background: linear-gradient(to bottom, transparent, var(--surface-2));
}

.sp-sig.is-selected .sp-sig__textwrap.is-clamped::after {
  background: none;
}

/* Height deliberately NOT overridden down to 20px any more: at 20px this
   button could not carry a 40px hit target without its skirt reaching into
   the message region above it, which is itself a control. */
.sp-sig .sp-sig__more {
  align-self: flex-start;
  padding: 0 var(--sp-1);
}

.sp-sig__text {
  display: block;
  font-size: var(--fs-3);
  line-height: var(--lh);
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.sp-sig--skipped .sp-sig__text {
  color: var(--text-dim);
}

.sp-sig__text.is-empty {
  color: var(--text-dim);
  font-style: italic;
}

.sp-sig__skip {
  padding-left: var(--sp-2);
  border-left: 2px solid var(--warn);
  font-size: var(--fs-1);
  line-height: var(--lh);
  color: var(--warn);
  overflow-wrap: anywhere;
}

.sp-sig__thumbs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
}

.sp-sig__thumb {
  flex: none;
  width: 96px;
  height: 64px;
  padding: 0;
  overflow: hidden;
  background: var(--surface-2);
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius);
  cursor: zoom-in;
  transition:
    border-color var(--dur-fast) var(--ease),
    transform var(--dur-fast) var(--ease);
}

.sp-sig__thumb:hover {
  border-color: var(--accent);
}

/* A thumbnail is a button, and a button that opens a lightbox should answer
   the press. Exactly 0.96, and off entirely for reduced motion below. */
.sp-sig__thumb:active {
  transform: scale(0.96);
}

@media (prefers-reduced-motion: reduce) {
  .sp-sig__thumb:active {
    transform: none;
  }
}

.sp-sig__thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  /* The frame's 1px border is this image's padding: 8 - 1. Without it the
     picture's corner rides a hair outside the frame's arc. */
  border-radius: calc(var(--radius) - var(--border-w));
}

/* A skipped message's imagery is greyed with the rest of it. */
.sp-sig--skipped .sp-sig__thumb img {
  filter: grayscale(1);
}

.sp-sig .sp-sig__more-images {
  height: 20px;
}

.sp-sig__foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
  font-size: var(--fs-1);
  color: var(--text-dim);
}

.sp-sig__id {
  font-size: var(--fs-1);
  color: var(--text-dim);
}

.sp-sig__meta-line {
  font-size: var(--fs-1);
  color: var(--text-dim);
}

.sp-sig .sp-sig__reply {
  padding: 0 var(--sp-1);
  color: var(--accent);
}

/* A one-line link in a meta row is 20px of text. Sat on a control-height box
   it can carry the same 8px skirt every button in the card carries and reach
   40px, without the row growing: the reply button beside it is already 24px. */
.sp-sig .sp-sig__link {
  position: relative;
  display: inline-flex;
  align-items: center;
  min-height: var(--control-h-sm);
  font-size: var(--fs-1);
  color: var(--accent);
  white-space: nowrap;
}

.sp-sig .sp-sig__link::after {
  content: "";
  position: absolute;
  inset: -8px 0;
}

.sp-sig__runs {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding-top: var(--sp-2);
  border-top: var(--border-w) solid var(--border);
}

/* ---- lightbox ---- */

.sp-lightbox {
  cursor: zoom-out;
}

.sp-lightbox__frame {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-width: min(1200px, 100%);
  max-height: 100%;
  min-height: 0;
  cursor: default;
}

.sp-lightbox__img {
  max-width: 100%;
  max-height: calc(100vh - var(--sp-8) - var(--sp-6));
  width: auto;
  height: auto;
  object-fit: contain;
  background: var(--surface);
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius);
}

/* padding var(--sp-1) so the bar's var(--radius) is exactly the buttons'
   var(--radius-sm) plus the gutter around them: 4 + 4 = 8. */
.sp-lightbox__bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: none;
  padding: var(--sp-1);
  background: var(--surface);
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius);
}

.sp-lightbox__counter {
  font-size: var(--fs-2);
  color: var(--text-dim);
}
`;
