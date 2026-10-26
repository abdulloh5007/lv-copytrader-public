/**
 * LV Copytrader — PositionsTable
 * ---------------------------------------------------------------------------
 * Every open MT5 position, one dense row each, with the operator's manual
 * controls (BUILD_PROMPT §9 "Positions", CONTRACTS §18 `/api/trading/*`).
 *
 * Three rules this component exists to honour:
 *
 *  1. COLOUR IS NEVER THE ONLY CARRIER. The side is a badge that prints the
 *     word BUY / SELL next to a directional arrow, and every P/L carries an
 *     explicit sign as well as its colour (BUILD §11).
 *
 *  2. A STOP CAN NEVER BE REMOVED. The inline modify form refuses an empty or
 *     zero stop client-side with `t('err.required')`, which mirrors the server
 *     rule of BUILD §6.2 rather than waiting for it to answer.
 *
 *  3. `external` ROWS ARE READ-ONLY. A position carrying a foreign magic number
 *     belongs to someone else. It renders dimmed, with an EXTERNAL badge, with
 *     no action buttons at all, and with a title saying why (BUILD §8.2).
 *
 * Numbers all render through `.mono`, so a streaming price changes digits
 * without moving the row.
 */
import { Fragment, useCallback, useMemo, useState } from 'react';
import type { CSSProperties, ReactElement } from 'react';
import { Link } from 'react-router-dom';

import { isApiError, manualClose, manualModifyPosition } from '@/api/client';
import type {
  Distance,
  ManualCloseInput,
  ManualModifyPositionInput,
  PositionOut,
  Side,
  WriteResultOut,
} from '@/api/client';
import {
  EMPTY_VALUE,
  formatNumber,
  formatPrice,
  formatSignedCurrency,
  formatVolume,
  useCurrentLocale,
  useT,
} from '@/i18n';
import type { Locale, Translate } from '@/i18n';

/* ─────────────────────────────── props ──────────────────────────────────── */

/** How the parent turns a row into a link to the signal card that opened it. */
export type SignalHref = (position: PositionOut) => string | null;

export interface PositionsTableProps {
  /** Live rows from `GET /api/trading/positions`. */
  positions: PositionOut[];
  /** Account currency, for the P/L column. */
  currency?: string | null;
  /** True while the first fetch is in flight; renders skeleton rows. */
  loading?: boolean;
  /** A fetch failure, already turned into a sentence by the caller. */
  error?: string | null;
  /** False paints the "this table may be stale" note (the MT5 dot is down). */
  mt5Online?: boolean;
  /** Called after any successful write so the parent can refetch. */
  onRefresh?: () => void;
  /** Override the close call — defaults to `POST /api/trading/manual/close`. */
  onClose?: (input: ManualCloseInput) => Promise<WriteResultOut>;
  /** Override the modify call — defaults to `.../manual/modify-position`. */
  onModify?: (input: ManualModifyPositionInput) => Promise<WriteResultOut>;
  /** Link target for the originating signal card; default `/?signal=<id>`. */
  signalHref?: SignalHref;
  /** Set false to drop the card chrome and render the table alone. */
  showHeader?: boolean;
}

/* ────────────────────────────── local styles ────────────────────────────── */
/* Inline so this component owns its page-specific styling without adding a
   stylesheet, and so no colour literal ever leaves tokens.css. */

const UNIT: CSSProperties = { color: 'var(--text-dim)', fontSize: 'var(--fs-1)' };
const FORM_ROW: CSSProperties = { background: 'var(--surface-2)', whiteSpace: 'normal' };
const FORM_BOX: CSSProperties = { padding: 'var(--sp-2) 0', width: '100%' };
const FIELD_W: CSSProperties = { width: '140px' };
const FIELD_WIDE: CSSProperties = { minWidth: '220px', flex: '1 1 220px' };
const NOTE: CSSProperties = { color: 'var(--text-dim)', fontSize: 'var(--fs-1)' };
const OK_NOTE: CSSProperties = { color: 'var(--ok)', fontSize: 'var(--fs-1)' };
const BAD_NOTE: CSSProperties = { color: 'var(--danger)', fontSize: 'var(--fs-1)' };
const SIGNAL_CELL: CSSProperties = { maxWidth: '220px' };

/** A refusal that never reached the broker still has the write-result shape. */
const EMPTY_RESULT: WriteResultOut = {
  ok: false,
  message: '',
  ticket: null,
  tickets: [],
  filled_volume: null,
  fill_price: null,
  slippage_points: null,
  retcode: null,
  retcode_name: null,
};

/** Number of `<th>`s, so a form row can span the whole table. */
const COLUMN_COUNT = 12;

/* ───────────────────────────── small helpers ────────────────────────────── */

/**
 * Parse an operator-typed number, accepting both decimal separators.
 * `null` means the field was left empty; `NaN` means it was not a number.
 */
function parseDecimal(raw: string): number | null {
  const cleaned = raw.replace(/\s/g, '').replace(',', '.');
  if (cleaned === '') return null;
  if (!/^-?\d*\.?\d*$/.test(cleaned) || cleaned === '.' || cleaned === '-') return Number.NaN;
  const value = Number(cleaned);
  return Number.isFinite(value) ? value : Number.NaN;
}

/** A price as text for an input's initial value; an unset level starts empty. */
function priceInputValue(value: number, digits: number): string {
  if (!Number.isFinite(value) || value === 0) return '';
  return value.toFixed(Math.max(0, Math.min(8, Math.trunc(digits))));
}

/** `200 points · 20.0 pips` — both units, because "pip" is broker-dependent. */
function distanceText(distance: Distance | null, t: Translate, locale: Locale): string {
  if (!distance) return EMPTY_VALUE;
  const points = formatNumber(distance.points, { maximumFractionDigits: 0 }, locale);
  const pips = formatNumber(distance.pips, { maximumFractionDigits: 1 }, locale);
  return `${points} ${t('trade.points')} · ${pips} ${t('trade.pips')}`;
}

/** The colour class for a signed money cell; the sign carries it too. */
function profitClass(value: number): string {
  if (value > 0) return 'num mono is-long';
  if (value < 0) return 'num mono is-short';
  return 'num mono is-dim';
}

/** Turn any thrown value into the sentence the row will show. */
function errorText(cause: unknown, t: Translate): string {
  if (isApiError(cause)) return cause.message;
  if (cause instanceof Error && cause.message) return cause.message;
  return t('err.generic');
}

/* ──────────────────────────── presentational bits ───────────────────────── */

/** BUY / SELL as a word plus an arrow, never colour alone. */
function SideBadge({ side, t }: { side: Side; t: Translate }): ReactElement {
  const long = side === 'buy';
  return (
    <span className={long ? 'badge badge--long' : 'badge badge--short'}>
      {t(long ? 'trade.buy_arrow' : 'trade.sell_arrow')}
    </span>
  );
}

/** The link back to the signal that opened this position, plus its permalink. */
function SignalCell({
  position,
  href,
  t,
}: {
  position: PositionOut;
  href: string | null;
  t: Translate;
}): ReactElement {
  const label = (position.signal_excerpt ?? '').trim() || position.signal_ref || '';
  const permalink = position.permalink;

  if (!href && !permalink) {
    return (
      <span className="is-dim" title={t('trade.signal_missing')}>
        {t('trade.signal_missing')}
      </span>
    );
  }

  return (
    <span className="row row--tight" style={SIGNAL_CELL}>
      {href ? (
        <Link to={href} title={label || t('trade.signal')}>
          {label || t('trade.signal')}
        </Link>
      ) : (
        <span title={label}>{label || t('trade.signal')}</span>
      )}
      {permalink ? (
        <a
          href={permalink}
          target="_blank"
          rel="noreferrer noopener"
          className="iconlink"
          title={t('feed.open_in_telegram')}
          aria-label={t('feed.open_in_telegram')}
        >
          <span aria-hidden="true">↗</span>
        </a>
      ) : null}
    </span>
  );
}

/* ──────────────────────────── the modify form ───────────────────────────── */

interface ModifyFormProps {
  position: PositionOut;
  busy: boolean;
  onSubmit: (input: ManualModifyPositionInput) => void;
  onCancel: () => void;
  t: Translate;
}

/**
 * Inline SL / TP editor.
 *
 * The stop is validated here before anything leaves the browser: empty or zero
 * is refused with `t('err.required')`, because BUILD §6.2 says a stop can be
 * moved but never removed. The hint states the rule so the refusal is not a
 * surprise.
 */
function ModifyForm({ position, busy, onSubmit, onCancel, t }: ModifyFormProps): ReactElement {
  const [sl, setSl] = useState(() => priceInputValue(position.sl, position.digits));
  const [tp, setTp] = useState(() => priceInputValue(position.tp, position.digits));
  const [reason, setReason] = useState('');
  const [slError, setSlError] = useState<string | null>(null);
  const [tpError, setTpError] = useState<string | null>(null);

  const submit = useCallback(() => {
    const slValue = parseDecimal(sl);
    const tpValue = parseDecimal(tp);

    let slMessage: string | null = null;
    if (slValue === null || slValue === 0) slMessage = t('err.required');
    else if (Number.isNaN(slValue)) slMessage = t('err.number');
    else if (slValue < 0) slMessage = t('err.positive_number');

    let tpMessage: string | null = null;
    if (tpValue !== null) {
      if (Number.isNaN(tpValue)) tpMessage = t('err.number');
      else if (tpValue < 0) tpMessage = t('err.positive_number');
    }

    setSlError(slMessage);
    setTpError(tpMessage);
    if (slMessage || tpMessage || slValue === null || Number.isNaN(slValue)) return;

    onSubmit({
      ticket: position.ticket,
      sl: slValue,
      tp: tpValue === null ? null : tpValue,
      reason: reason.trim() || t('trade.cause_manual'),
    });
  }, [onSubmit, position.ticket, reason, sl, t, tp]);

  const slId = `sp-pos-sl-${position.ticket}`;
  const tpId = `sp-pos-tp-${position.ticket}`;
  const reasonId = `sp-pos-reason-${position.ticket}`;

  return (
    <div className="col" style={FORM_BOX}>
      <strong>{t('trade.modify_title', { ticket: position.ticket })}</strong>
      <div className="row row--wrap row--top">
        <div className={slError ? 'field field--invalid' : 'field'} style={FIELD_W}>
          <label className="field__label" htmlFor={slId}>
            {t('trade.new_sl')}
          </label>
          <input
            id={slId}
            className="mono"
            type="text"
            inputMode="decimal"
            autoComplete="off"
            value={sl}
            disabled={busy}
            aria-invalid={slError ? 'true' : undefined}
            aria-describedby={slError ? `${slId}-err` : `${slId}-hint`}
            onChange={(event) => setSl(event.target.value)}
          />
          {slError ? (
            <p className="field__error" id={`${slId}-err`}>
              {slError}
            </p>
          ) : (
            <p className="field__hint" id={`${slId}-hint`}>
              {t('err.sl_not_removable')}
            </p>
          )}
        </div>

        <div className={tpError ? 'field field--invalid' : 'field'} style={FIELD_W}>
          <label className="field__label" htmlFor={tpId}>
            {t('trade.new_tp')}
          </label>
          <input
            id={tpId}
            className="mono"
            type="text"
            inputMode="decimal"
            autoComplete="off"
            value={tp}
            disabled={busy}
            aria-invalid={tpError ? 'true' : undefined}
            aria-describedby={tpError ? `${tpId}-err` : undefined}
            onChange={(event) => setTp(event.target.value)}
          />
          {tpError ? (
            <p className="field__error" id={`${tpId}-err`}>
              {tpError}
            </p>
          ) : (
            <p className="field__hint">{t('app.optional')}</p>
          )}
        </div>

        <div className="field" style={FIELD_WIDE}>
          <label className="field__label" htmlFor={reasonId}>
            {t('app.reason')}
          </label>
          <input
            id={reasonId}
            type="text"
            autoComplete="off"
            value={reason}
            disabled={busy}
            placeholder={t('trade.manual_reason_placeholder')}
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
      </div>

      <div className="row row--tight">
        <button type="button" className="btn btn--primary btn--sm" disabled={busy} onClick={submit}>
          {busy ? t('trade.modifying') : t('app.apply')}
        </button>
        <button type="button" className="btn btn--sm" disabled={busy} onClick={onCancel}>
          {t('app.cancel')}
        </button>
      </div>
    </div>
  );
}

/* ───────────────────────────── the close form ───────────────────────────── */

interface CloseFormProps {
  position: PositionOut;
  busy: boolean;
  onSubmit: (input: ManualCloseInput) => void;
  onCancel: () => void;
  t: Translate;
  locale: Locale;
}

/** Full or partial close. A partial send is the same request with less volume. */
function CloseForm({
  position,
  busy,
  onSubmit,
  onCancel,
  t,
  locale,
}: CloseFormProps): ReactElement {
  const [partial, setPartial] = useState(false);
  const [volume, setVolume] = useState(() => formatVolume(position.volume, 'en'));
  const [reason, setReason] = useState('');
  const [volumeError, setVolumeError] = useState<string | null>(null);

  const submit = useCallback(() => {
    let requested: number | null = null;
    if (partial) {
      const parsed = parseDecimal(volume);
      if (parsed === null) {
        setVolumeError(t('err.required'));
        return;
      }
      if (Number.isNaN(parsed)) {
        setVolumeError(t('err.number'));
        return;
      }
      if (parsed <= 0) {
        setVolumeError(t('err.positive_number'));
        return;
      }
      if (parsed > position.volume) {
        setVolumeError(t('err.max', { max: formatVolume(position.volume, locale) }));
        return;
      }
      requested = parsed;
    }
    setVolumeError(null);
    onSubmit({
      ticket: position.ticket,
      volume: requested,
      reason: reason.trim() || t('trade.cause_manual'),
    });
  }, [locale, onSubmit, partial, position.ticket, position.volume, reason, t, volume]);

  const volumeId = `sp-pos-vol-${position.ticket}`;
  const reasonId = `sp-pos-close-reason-${position.ticket}`;

  return (
    <div className="col" style={FORM_BOX}>
      <strong>{t('trade.confirm_close_title', { ticket: position.ticket })}</strong>
      <p style={NOTE}>
        {partial
          ? t('trade.confirm_partial_body', {
              volume: volume || formatVolume(0, locale),
              total: formatVolume(position.volume, locale),
              symbol: position.symbol,
            })
          : t('trade.confirm_close_body', {
              side: t(position.side === 'buy' ? 'trade.buy' : 'trade.sell'),
              volume: formatVolume(position.volume, locale),
              symbol: position.symbol,
            })}
      </p>

      <div className="row row--wrap row--top">
        <div className="row row--tight" role="radiogroup" aria-label={t('trade.volume_to_close')}>
          <button
            type="button"
            role="radio"
            aria-checked={!partial}
            className={partial ? 'tab' : 'tab is-active'}
            disabled={busy}
            onClick={() => setPartial(false)}
          >
            {t('trade.close')}
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={partial}
            className={partial ? 'tab is-active' : 'tab'}
            disabled={busy}
            onClick={() => setPartial(true)}
          >
            {t('trade.partial_close')}
          </button>
        </div>

        {partial ? (
          <div className={volumeError ? 'field field--invalid' : 'field'} style={FIELD_W}>
            <label className="field__label" htmlFor={volumeId}>
              {t('trade.volume_to_close')}
            </label>
            <input
              id={volumeId}
              className="mono"
              type="text"
              inputMode="decimal"
              autoComplete="off"
              value={volume}
              disabled={busy}
              aria-invalid={volumeError ? 'true' : undefined}
              aria-describedby={volumeError ? `${volumeId}-err` : undefined}
              onChange={(event) => setVolume(event.target.value)}
            />
            {volumeError ? (
              <p className="field__error" id={`${volumeId}-err`}>
                {volumeError}
              </p>
            ) : null}
          </div>
        ) : null}

        <div className="field" style={FIELD_WIDE}>
          <label className="field__label" htmlFor={reasonId}>
            {t('app.reason')}
          </label>
          <input
            id={reasonId}
            type="text"
            autoComplete="off"
            value={reason}
            disabled={busy}
            placeholder={t('trade.manual_reason_placeholder')}
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
      </div>

      <div className="row row--tight">
        <button type="button" className="btn btn--danger btn--sm" disabled={busy} onClick={submit}>
          {busy ? t('trade.closing') : t('app.confirm')}
        </button>
        <button type="button" className="btn btn--sm" disabled={busy} onClick={onCancel}>
          {t('app.cancel')}
        </button>
      </div>
    </div>
  );
}

/* ──────────────────────────────── the table ─────────────────────────────── */

type OpenForm = { ticket: number; mode: 'close' | 'modify' } | null;

export function PositionsTable(props: PositionsTableProps): ReactElement {
  const {
    positions,
    currency = null,
    loading = false,
    error = null,
    mt5Online = true,
    onRefresh,
    onClose,
    onModify,
    signalHref,
    showHeader = true,
  } = props;

  const t = useT();
  const locale = useCurrentLocale();

  const [form, setForm] = useState<OpenForm>(null);
  const [busyTicket, setBusyTicket] = useState<number | null>(null);
  const [results, setResults] = useState<Record<number, { ok: boolean; message: string }>>({});

  const closeCall = onClose ?? manualClose;
  const modifyCall = onModify ?? manualModifyPosition;

  const hrefFor = useCallback(
    (position: PositionOut): string | null => {
      if (signalHref) return signalHref(position);
      if (position.message_row_id === null) return null;
      return `/?signal=${position.message_row_id}`;
    },
    [signalHref],
  );

  const totals = useMemo(() => {
    let profit = 0;
    let external = 0;
    for (const position of positions) {
      profit += Number.isFinite(position.profit) ? position.profit : 0;
      if (position.external) external += 1;
    }
    return { profit, external };
  }, [positions]);

  const record = useCallback((ticket: number, result: WriteResultOut) => {
    setResults((previous) => ({
      ...previous,
      [ticket]: { ok: result.ok, message: result.message },
    }));
  }, []);

  const dismiss = useCallback((ticket: number) => {
    setResults((previous) => {
      const next = { ...previous };
      delete next[ticket];
      return next;
    });
  }, []);

  const runClose = useCallback(
    (input: ManualCloseInput) => {
      setBusyTicket(input.ticket);
      closeCall(input)
        .then((result) => {
          record(input.ticket, result);
          if (result.ok) {
            setForm(null);
            onRefresh?.();
          }
        })
        .catch((cause: unknown) => {
          record(input.ticket, { ...EMPTY_RESULT, message: errorText(cause, t) });
        })
        .finally(() => setBusyTicket(null));
    },
    [closeCall, onRefresh, record, t],
  );

  const runModify = useCallback(
    (input: ManualModifyPositionInput) => {
      setBusyTicket(input.ticket);
      modifyCall(input)
        .then((result) => {
          record(input.ticket, result);
          if (result.ok) {
            setForm(null);
            onRefresh?.();
          }
        })
        .catch((cause: unknown) => {
          record(input.ticket, { ...EMPTY_RESULT, message: errorText(cause, t) });
        })
        .finally(() => setBusyTicket(null));
    },
    [modifyCall, onRefresh, record, t],
  );

  const table = (
    <div className="scroll-y scroll-y--x">
      <table className="table">
        <thead>
          <tr>
            <th scope="col" className="num">
              {t('trade.ticket')}
            </th>
            <th scope="col">{t('trade.symbol')}</th>
            <th scope="col">{t('trade.side')}</th>
            <th scope="col" className="num">
              {t('trade.volume')}
            </th>
            <th scope="col" className="num num--wide">
              {t('trade.entry')}
            </th>
            <th scope="col" className="num num--wide">
              {t('trade.current_price')}
            </th>
            <th scope="col" className="num num--wide">
              {t('trade.sl')}
            </th>
            <th scope="col" className="num num--wide">
              {t('trade.tp')}
            </th>
            <th scope="col" className="num">
              {t('trade.distance_to_sl')}
            </th>
            <th scope="col" className="num num--wide">
              {t('trade.profit')}
            </th>
            <th scope="col">{t('trade.signal')}</th>
            <th scope="col">{t('app.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {loading && positions.length === 0 ? (
            <tr>
              <td colSpan={COLUMN_COUNT}>
                <span className="skeleton skeleton--text" aria-hidden="true" />
                <span className="is-dim">{t('app.loading')}</span>
              </td>
            </tr>
          ) : null}

          {positions.map((position) => {
            const result = results[position.ticket];
            const isOpen = form?.ticket === position.ticket;
            const busy = busyTicket === position.ticket;
            const digits = position.digits;
            const href = hrefFor(position);

            return (
              <Fragment key={position.ticket}>
                <tr
                  className={position.external ? 'is-readonly' : undefined}
                  title={position.external ? t('trade.external_hint') : undefined}
                >
                  <td className="num mono">{position.ticket}</td>
                  <td>{position.symbol}</td>
                  <td>
                    <SideBadge side={position.side} t={t} />
                  </td>
                  <td className="num mono">{formatVolume(position.volume, locale)}</td>
                  <td className="num mono">{formatPrice(position.price_open, digits, locale)}</td>
                  <td className="num mono">
                    {formatPrice(position.price_current, digits, locale)}
                  </td>
                  <td className={position.sl === 0 ? 'num mono is-dim' : 'num mono'}>
                    {position.sl === 0 ? (
                      <span className="badge badge--warn">{t('trade.no_sl')}</span>
                    ) : (
                      formatPrice(position.sl, digits, locale)
                    )}
                  </td>
                  <td className={position.tp === 0 ? 'num mono is-dim' : 'num mono'}>
                    {position.tp === 0 ? t('trade.no_tp') : formatPrice(position.tp, digits, locale)}
                  </td>
                  <td className="num mono mono--sm">
                    {distanceText(position.sl_distance, t, locale)}
                  </td>
                  <td className={profitClass(position.profit)}>
                    {formatSignedCurrency(position.profit, currency, locale)}
                  </td>
                  <td>
                    <SignalCell position={position} href={href} t={t} />
                  </td>
                  <td>
                    {position.external ? (
                      <span className="badge badge--dim" title={t('trade.external_hint')}>
                        {t('trade.external')}
                      </span>
                    ) : (
                      <span className="row row--tight">
                        <button
                          type="button"
                          className="btn btn--sm"
                          disabled={busy}
                          aria-expanded={isOpen && form?.mode === 'modify'}
                          onClick={() =>
                            setForm(
                              isOpen && form?.mode === 'modify'
                                ? null
                                : { ticket: position.ticket, mode: 'modify' },
                            )
                          }
                        >
                          {t('trade.modify')}
                        </button>
                        <button
                          type="button"
                          className="btn btn--danger btn--sm"
                          disabled={busy}
                          aria-expanded={isOpen && form?.mode === 'close'}
                          onClick={() =>
                            setForm(
                              isOpen && form?.mode === 'close'
                                ? null
                                : { ticket: position.ticket, mode: 'close' },
                            )
                          }
                        >
                          {t('trade.close')}
                        </button>
                      </span>
                    )}
                  </td>
                </tr>

                {result ? (
                  <tr>
                    <td colSpan={COLUMN_COUNT} style={FORM_ROW}>
                      <span className="row row--tight">
                        <span style={result.ok ? OK_NOTE : BAD_NOTE} role="status">
                          {result.ok
                            ? t('trade.action_ok', { message: result.message })
                            : t('trade.action_failed', { message: result.message })}
                        </span>
                        <button
                          type="button"
                          className="btn btn--ghost btn--sm"
                          onClick={() => dismiss(position.ticket)}
                        >
                          {t('app.dismiss')}
                        </button>
                      </span>
                    </td>
                  </tr>
                ) : null}

                {isOpen && !position.external ? (
                  <tr>
                    <td colSpan={COLUMN_COUNT} style={FORM_ROW}>
                      {form?.mode === 'modify' ? (
                        <ModifyForm
                          position={position}
                          busy={busy}
                          onSubmit={runModify}
                          onCancel={() => setForm(null)}
                          t={t}
                        />
                      ) : (
                        <CloseForm
                          position={position}
                          busy={busy}
                          onSubmit={runClose}
                          onCancel={() => setForm(null)}
                          t={t}
                          locale={locale}
                        />
                      )}
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>

      {!loading && positions.length === 0 ? (
        <div className="empty">
          <p className="empty__title">{t('positions.empty')}</p>
        </div>
      ) : null}
    </div>
  );

  if (!showHeader) return table;

  return (
    <section className="card card--flush">
      <header className="card__header">
        <h2 className="card__title">{t('positions.title')}</h2>
        <div className="card__actions">
          <span className="badge badge--dim">
            {t('positions.count', { count: positions.length })}
          </span>
          {totals.external > 0 ? (
            <span className="badge badge--dim" title={t('trade.external_hint')}>
              {t('positions.external_count', { count: totals.external })}
            </span>
          ) : null}
          <span style={UNIT}>{t('positions.total_pl')}</span>
          <span
            className={
              totals.profit > 0
                ? 'mono badge badge--long'
                : totals.profit < 0
                  ? 'mono badge badge--short'
                  : 'mono badge badge--dim'
            }
          >
            {formatSignedCurrency(totals.profit, currency, locale)}
          </span>
          {onRefresh ? (
            <button type="button" className="btn btn--sm" onClick={onRefresh} disabled={loading}>
              {t('positions.refresh')}
            </button>
          ) : null}
        </div>
      </header>

      {!mt5Online ? (
        <p className="field__error" role="status">
          {t('positions.mt5_offline')}
        </p>
      ) : null}
      {error ? (
        <p className="field__error" role="alert">
          {error}
        </p>
      ) : null}

      {table}
    </section>
  );
}

export default PositionsTable;
