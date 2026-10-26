/**
 * LV Copytrader — OrdersTable
 * ---------------------------------------------------------------------------
 * Pending orders, the same treatment PositionsTable gives open positions
 * (BUILD_PROMPT §9 "Positions", CONTRACTS §18 `/api/trading/orders`).
 *
 * Three things this file is careful about:
 *
 *  1. AN ORDER APPEARS EXACTLY ONCE. A pending order is accepted with retcode
 *     10008 (`TRADE_RETCODE_PLACED`), which is success — code that expects
 *     10009 marks it failed and places it a second time (BUILD §1.3). The
 *     backend gets that right; this table additionally de-duplicates by ticket
 *     so a doubled payload can never render two rows for one order.
 *
 *  2. THE TYPE IS SPELLED OUT. `BUY LIMIT` / `SELL STOP` are words, not colour,
 *     and each carries a directional arrow as a third carrier (BUILD §11).
 *
 *  3. `external` ROWS ARE READ-ONLY — dimmed, badged, no buttons, and a title
 *     saying they carry another magic number and are never touched (BUILD §8.2).
 */
import { Fragment, useCallback, useMemo, useState } from 'react';
import type { CSSProperties, ReactElement } from 'react';
import { Link } from 'react-router-dom';

import { isApiError, manualCancel, manualModifyOrder } from '@/api/client';
import type {
  Distance,
  ManualCancelInput,
  ManualModifyOrderInput,
  OrderOut,
  WriteResultOut,
} from '@/api/client';
import {
  EMPTY_VALUE,
  formatDateTime,
  formatNumber,
  formatPrice,
  formatVolume,
  useCurrentLocale,
  useT,
} from '@/i18n';
import type { Locale, TranslationKey, Translate } from '@/i18n';

/* ─────────────────────────────── props ──────────────────────────────────── */

/** How the parent turns a row into a link to the signal card that placed it. */
export type OrderSignalHref = (order: OrderOut) => string | null;

export interface OrdersTableProps {
  /** Live rows from `GET /api/trading/orders`. */
  orders: OrderOut[];
  /** True while the first fetch is in flight. */
  loading?: boolean;
  /** A fetch failure, already turned into a sentence by the caller. */
  error?: string | null;
  /** False paints the "this table may be stale" note. */
  mt5Online?: boolean;
  /** Called after any successful write so the parent can refetch. */
  onRefresh?: () => void;
  /** Override the cancel call — defaults to `POST /api/trading/manual/cancel`. */
  onCancel?: (input: ManualCancelInput) => Promise<WriteResultOut>;
  /** Override the modify call — defaults to `.../manual/modify-order`. */
  onModify?: (input: ManualModifyOrderInput) => Promise<WriteResultOut>;
  /** Link target for the originating signal card; default `/?signal=<id>`. */
  signalHref?: OrderSignalHref;
  /** Set false to drop the card chrome and render the table alone. */
  showHeader?: boolean;
}

/* ────────────────────────────── local styles ────────────────────────────── */

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
const COLUMN_COUNT = 11;

/* ───────────────────── MT5 order types (trading/constants.py) ───────────── */

/**
 * The MQL5 `ORDER_TYPE_*` integers. Written out rather than computed, exactly
 * as the backend's `trading/constants.py` does, and mapped to a dictionary key
 * so the type reads as a phrase in both locales.
 */
const ORDER_TYPE_LABELS: Record<number, TranslationKey> = {
  0: 'trade.type.buy',
  1: 'trade.type.sell',
  2: 'trade.type.buy_limit',
  3: 'trade.type.sell_limit',
  4: 'trade.type.buy_stop',
  5: 'trade.type.sell_stop',
};

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

/** Turn any thrown value into the sentence the row will show. */
function errorText(cause: unknown, t: Translate): string {
  if (isApiError(cause)) return cause.message;
  if (cause instanceof Error && cause.message) return cause.message;
  return t('err.generic');
}

/**
 * One row per ticket, first occurrence wins.
 *
 * The 10008 trap of BUILD §1.3 is a backend concern, but the cheapest possible
 * guarantee that "a pending order appears exactly once" is to make the render
 * itself incapable of showing it twice.
 */
function uniqueByTicket(orders: OrderOut[]): OrderOut[] {
  const seen = new Set<number>();
  const rows: OrderOut[] = [];
  for (const order of orders) {
    if (seen.has(order.ticket)) continue;
    seen.add(order.ticket);
    rows.push(order);
  }
  return rows;
}

/* ──────────────────────────── presentational bits ───────────────────────── */

/** `▲ Buy limit` — the direction is a word and an arrow, never colour alone. */
function OrderTypeBadge({ order, t }: { order: OrderOut; t: Translate }): ReactElement {
  const long = order.side === 'buy';
  const key = ORDER_TYPE_LABELS[order.order_type];
  const label = key ? t(key) : t('app.unknown');
  return (
    <span
      className={long ? 'badge badge--long' : 'badge badge--short'}
      title={order.order_type_name || label}
    >
      <span aria-hidden="true">{long ? '▲' : '▼'}</span>
      {label}
    </span>
  );
}

/** The link back to the signal that placed this order, plus its permalink. */
function SignalCell({
  order,
  href,
  t,
}: {
  order: OrderOut;
  href: string | null;
  t: Translate;
}): ReactElement {
  const label = (order.signal_excerpt ?? '').trim() || order.signal_ref || '';
  const permalink = order.permalink;

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
  order: OrderOut;
  busy: boolean;
  onSubmit: (input: ManualModifyOrderInput) => void;
  onCancel: () => void;
  t: Translate;
}

/**
 * Inline price / SL / TP editor for a pending order.
 *
 * The stop is validated before anything leaves the browser: empty or zero is
 * refused with `t('err.required')`, mirroring the server rule that a stop can
 * be moved but never removed (BUILD §6.2).
 */
function ModifyForm({ order, busy, onSubmit, onCancel, t }: ModifyFormProps): ReactElement {
  const [price, setPrice] = useState(() => priceInputValue(order.price_open, order.digits));
  const [sl, setSl] = useState(() => priceInputValue(order.sl, order.digits));
  const [tp, setTp] = useState(() => priceInputValue(order.tp, order.digits));
  const [reason, setReason] = useState('');
  const [priceError, setPriceError] = useState<string | null>(null);
  const [slError, setSlError] = useState<string | null>(null);
  const [tpError, setTpError] = useState<string | null>(null);

  const submit = useCallback(() => {
    const priceValue = parseDecimal(price);
    const slValue = parseDecimal(sl);
    const tpValue = parseDecimal(tp);

    let priceMessage: string | null = null;
    if (priceValue === null) priceMessage = t('err.required');
    else if (Number.isNaN(priceValue)) priceMessage = t('err.number');
    else if (priceValue <= 0) priceMessage = t('err.positive_number');

    let slMessage: string | null = null;
    if (slValue === null || slValue === 0) slMessage = t('err.required');
    else if (Number.isNaN(slValue)) slMessage = t('err.number');
    else if (slValue < 0) slMessage = t('err.positive_number');

    let tpMessage: string | null = null;
    if (tpValue !== null) {
      if (Number.isNaN(tpValue)) tpMessage = t('err.number');
      else if (tpValue < 0) tpMessage = t('err.positive_number');
    }

    setPriceError(priceMessage);
    setSlError(slMessage);
    setTpError(tpMessage);
    if (priceMessage || slMessage || tpMessage) return;
    if (priceValue === null || slValue === null) return;

    onSubmit({
      ticket: order.ticket,
      price: priceValue,
      sl: slValue,
      tp: tpValue === null ? null : tpValue,
      reason: reason.trim() || t('trade.cause_manual'),
    });
  }, [onSubmit, order.ticket, price, reason, sl, t, tp]);

  const priceId = `sp-ord-price-${order.ticket}`;
  const slId = `sp-ord-sl-${order.ticket}`;
  const tpId = `sp-ord-tp-${order.ticket}`;
  const reasonId = `sp-ord-reason-${order.ticket}`;

  return (
    <div className="col" style={FORM_BOX}>
      <strong>{t('trade.modify_title', { ticket: order.ticket })}</strong>
      <div className="row row--wrap row--top">
        <div className={priceError ? 'field field--invalid' : 'field'} style={FIELD_W}>
          <label className="field__label" htmlFor={priceId}>
            {t('trade.new_price')}
          </label>
          <input
            id={priceId}
            className="mono"
            type="text"
            inputMode="decimal"
            autoComplete="off"
            value={price}
            disabled={busy}
            aria-invalid={priceError ? 'true' : undefined}
            aria-describedby={priceError ? `${priceId}-err` : undefined}
            onChange={(event) => setPrice(event.target.value)}
          />
          {priceError ? (
            <p className="field__error" id={`${priceId}-err`}>
              {priceError}
            </p>
          ) : null}
        </div>

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

/* ──────────────────────────── the cancel form ───────────────────────────── */

interface CancelFormProps {
  order: OrderOut;
  busy: boolean;
  onSubmit: (input: ManualCancelInput) => void;
  onCancel: () => void;
  t: Translate;
}

/** Typed confirmation is reserved for the kill switch; this one states itself. */
function CancelForm({ order, busy, onSubmit, onCancel, t }: CancelFormProps): ReactElement {
  const [reason, setReason] = useState('');
  const key = ORDER_TYPE_LABELS[order.order_type];
  const typeLabel = key ? t(key) : t('app.unknown');
  const reasonId = `sp-ord-cancel-reason-${order.ticket}`;

  return (
    <div className="col" style={FORM_BOX}>
      <strong>{t('trade.confirm_cancel_title', { ticket: order.ticket })}</strong>
      <p style={NOTE}>
        {t('trade.confirm_cancel_body', { type: typeLabel, symbol: order.symbol })}
      </p>

      <div className="row row--wrap row--top">
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
        <button
          type="button"
          className="btn btn--danger btn--sm"
          disabled={busy}
          onClick={() =>
            onSubmit({ ticket: order.ticket, reason: reason.trim() || t('trade.cause_manual') })
          }
        >
          {busy ? t('trade.cancelling') : t('app.confirm')}
        </button>
        <button type="button" className="btn btn--sm" disabled={busy} onClick={onCancel}>
          {t('app.cancel')}
        </button>
      </div>
    </div>
  );
}

/* ──────────────────────────────── the table ─────────────────────────────── */

type OpenForm = { ticket: number; mode: 'cancel' | 'modify' } | null;

export function OrdersTable(props: OrdersTableProps): ReactElement {
  const {
    orders,
    loading = false,
    error = null,
    mt5Online = true,
    onRefresh,
    onCancel,
    onModify,
    signalHref,
    showHeader = true,
  } = props;

  const t = useT();
  const locale = useCurrentLocale();

  const [form, setForm] = useState<OpenForm>(null);
  const [busyTicket, setBusyTicket] = useState<number | null>(null);
  const [results, setResults] = useState<Record<number, { ok: boolean; message: string }>>({});

  const cancelCall = onCancel ?? manualCancel;
  const modifyCall = onModify ?? manualModifyOrder;

  const rows = useMemo(() => uniqueByTicket(orders), [orders]);
  const externalCount = useMemo(
    () => rows.reduce((total, order) => total + (order.external ? 1 : 0), 0),
    [rows],
  );

  const hrefFor = useCallback(
    (order: OrderOut): string | null => {
      if (signalHref) return signalHref(order);
      if (order.message_row_id === null) return null;
      return `/?signal=${order.message_row_id}`;
    },
    [signalHref],
  );

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

  const runCancel = useCallback(
    (input: ManualCancelInput) => {
      setBusyTicket(input.ticket);
      cancelCall(input)
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
    [cancelCall, onRefresh, record, t],
  );

  const runModify = useCallback(
    (input: ManualModifyOrderInput) => {
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
            <th scope="col">{t('trade.order_type')}</th>
            <th scope="col" className="num">
              {t('trade.volume')}
            </th>
            <th scope="col" className="num num--wide">
              {t('trade.price')}
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
            <th scope="col">{t('trade.expiry')}</th>
            <th scope="col">{t('trade.signal')}</th>
            <th scope="col">{t('app.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {loading && rows.length === 0 ? (
            <tr>
              <td colSpan={COLUMN_COUNT}>
                <span className="skeleton skeleton--text" aria-hidden="true" />
                <span className="is-dim">{t('app.loading')}</span>
              </td>
            </tr>
          ) : null}

          {rows.map((order) => {
            const result = results[order.ticket];
            const isOpen = form?.ticket === order.ticket;
            const busy = busyTicket === order.ticket;
            const digits = order.digits;

            return (
              <Fragment key={order.ticket}>
                <tr
                  className={order.external ? 'is-readonly' : undefined}
                  title={order.external ? t('trade.external_hint') : undefined}
                >
                  <td className="num mono">{order.ticket}</td>
                  <td>{order.symbol}</td>
                  <td>
                    <OrderTypeBadge order={order} t={t} />
                  </td>
                  <td className="num mono">{formatVolume(order.volume_current, locale)}</td>
                  <td className="num mono">{formatPrice(order.price_open, digits, locale)}</td>
                  <td className={order.sl === 0 ? 'num mono is-dim' : 'num mono'}>
                    {order.sl === 0 ? (
                      <span className="badge badge--warn">{t('trade.no_sl')}</span>
                    ) : (
                      formatPrice(order.sl, digits, locale)
                    )}
                  </td>
                  <td className={order.tp === 0 ? 'num mono is-dim' : 'num mono'}>
                    {order.tp === 0 ? t('trade.no_tp') : formatPrice(order.tp, digits, locale)}
                  </td>
                  <td className="num mono mono--sm">
                    {distanceText(order.sl_distance, t, locale)}
                  </td>
                  <td className={order.expiration_ts ? 'mono mono--sm' : 'is-dim'}>
                    {order.expiration_ts
                      ? formatDateTime(order.expiration_ts * 1000, locale)
                      : t('app.never')}
                  </td>
                  <td>
                    <SignalCell order={order} href={hrefFor(order)} t={t} />
                  </td>
                  <td>
                    {order.external ? (
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
                                : { ticket: order.ticket, mode: 'modify' },
                            )
                          }
                        >
                          {t('trade.modify')}
                        </button>
                        <button
                          type="button"
                          className="btn btn--danger btn--sm"
                          disabled={busy}
                          aria-expanded={isOpen && form?.mode === 'cancel'}
                          onClick={() =>
                            setForm(
                              isOpen && form?.mode === 'cancel'
                                ? null
                                : { ticket: order.ticket, mode: 'cancel' },
                            )
                          }
                        >
                          {t('trade.cancel_order')}
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
                          onClick={() => dismiss(order.ticket)}
                        >
                          {t('app.dismiss')}
                        </button>
                      </span>
                    </td>
                  </tr>
                ) : null}

                {isOpen && !order.external ? (
                  <tr>
                    <td colSpan={COLUMN_COUNT} style={FORM_ROW}>
                      {form?.mode === 'modify' ? (
                        <ModifyForm
                          order={order}
                          busy={busy}
                          onSubmit={runModify}
                          onCancel={() => setForm(null)}
                          t={t}
                        />
                      ) : (
                        <CancelForm
                          order={order}
                          busy={busy}
                          onSubmit={runCancel}
                          onCancel={() => setForm(null)}
                          t={t}
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

      {!loading && rows.length === 0 ? (
        <div className="empty">
          <p className="empty__title">{t('positions.orders_empty')}</p>
        </div>
      ) : null}
    </div>
  );

  if (!showHeader) return table;

  return (
    <section className="card card--flush">
      <header className="card__header">
        <h2 className="card__title">{t('positions.orders_title')}</h2>
        <div className="card__actions">
          <span className="badge badge--dim">
            {t('positions.orders_count', { count: rows.length })}
          </span>
          {externalCount > 0 ? (
            <span className="badge badge--dim" title={t('trade.external_hint')}>
              {t('positions.external_count', { count: externalCount })}
            </span>
          ) : null}
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

export default OrdersTable;
