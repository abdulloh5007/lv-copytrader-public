/**
 * AccountPanel — the account instruments of the cockpit (BUILD_PROMPT §9):
 * balance, equity, free margin, open P/L, today's realized P/L, leverage, margin
 * mode as the literal word, the open position count and the account currency.
 *
 * Every number renders in `.mono`, which carries `font-variant-numeric: tabular-nums`,
 * so a streaming equity changes digits without moving the row. P/L is coloured
 * `--long` / `--short` AND signed: the `+` or `-` carries the direction on its own,
 * which is what makes the panel readable in greyscale.
 *
 * FRESHNESS. The panel is fed by the `account.tick` stream event (CONTRACTS §19) —
 * the dashboard opens exactly one WebSocket, owned by the shell, so the parent
 * reduces the event and passes the snapshot down:
 *
 *     useStream((event) => {
 *       const next = accountFromEvent(event);
 *       if (next) setAccount(next);
 *     }, (status) => { if (status === 'open') void getAccount().then(setAccount); });
 *
 *     <AccountPanel account={account} />
 *
 * `updatedAt` is optional: with no explicit stamp the panel takes its own whenever
 * the `account` object identity changes, which every tick produces. If nothing has
 * arrived for `ACCOUNT_STALE_MS` (30 s) the numbers dim and a Stale chip appears —
 * a subtle treatment, because a stale reading is still a reading, but an operator
 * must never mistake a frozen instrument for a quiet market.
 */
import { useEffect, useRef, useState } from 'react';
import type { CSSProperties, ReactElement, ReactNode } from 'react';

import type { AccountOut, StreamEvent } from '../api/client';
import {
  EMPTY_VALUE,
  formatCurrency,
  formatInteger,
  formatRelative,
  formatSignedCurrency,
  useCurrentLocale,
  useT,
} from '../i18n';
import type { TKey } from '../i18n';

/** No tick for this long and the readout is treated as stale. */
export const ACCOUNT_STALE_MS = 30_000;

/** How often the panel re-evaluates staleness and its relative timestamp. */
const CLOCK_INTERVAL_MS = 1_000;

/** The three margin modes MT5 reports, each as its own word (BUILD_PROMPT §5.2). */
const MARGIN_MODE_KEYS: Record<string, TKey | undefined> = {
  hedging: 'account.hedging',
  netting: 'account.netting',
  exchange: 'account.exchange',
};

/**
 * Reduce a stream event into an account snapshot; `null` for every other event.
 * Exported so the shell that owns the single WebSocket can wire the panel up in
 * one line without duplicating the event's shape.
 */
export function accountFromEvent(event: StreamEvent): AccountOut | null {
  return event.type === 'account.tick' ? event.data.account : null;
}

/** P/L colour, paired with the sign the formatter prints. Never colour alone. */
function profitStyle(value: number | null | undefined): CSSProperties | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value) || value === 0) return undefined;
  return { color: value > 0 ? 'var(--long)' : 'var(--short)', fontWeight: 600 };
}

interface ReadoutProps {
  label: string;
  hint?: string;
  children: ReactNode;
}

/** One label-on-the-left, number-on-the-right instrument row. */
function Readout(props: ReadoutProps): ReactElement {
  return (
    <div className="row" title={props.hint}>
      <span className="field__label">{props.label}</span>
      <span className="spacer" />
      {props.children}
    </div>
  );
}

export interface AccountPanelProps {
  /** The latest snapshot; `null` while MT5 has told us nothing. */
  account: AccountOut | null;
  /** Epoch milliseconds of the last `account.tick`. Omit to let the panel stamp it. */
  updatedAt?: number | null;
  /** A first load is in flight and there is nothing to show yet. */
  loading?: boolean;
  /** A failed fetch, rendered next to the panel rather than as a toast. */
  error?: string | null;
  /** Wired to a Retry button when given. */
  onRetry?: () => void;
  className?: string;
}

export function AccountPanel(props: AccountPanelProps): ReactElement {
  const t = useT();
  const locale = useCurrentLocale();
  const account = props.account;

  /* ── freshness ───────────────────────────────────────────────────────── */

  const [ownStamp, setOwnStamp] = useState<number>(() => Date.now());
  const seenRef = useRef<AccountOut | null>(account);
  useEffect(() => {
    if (seenRef.current === account) return;
    seenRef.current = account;
    setOwnStamp(Date.now());
  }, [account]);

  const tickedAt = props.updatedAt ?? ownStamp;

  // One re-render a second keeps both the stale flag and the "ticked …" line
  // honest. The tree is small and every figure is tabular, so nothing jitters.
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), CLOCK_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, []);

  const stale = account !== null && now - tickedAt >= ACCOUNT_STALE_MS;

  /* ── body ────────────────────────────────────────────────────────────── */

  let body: ReactNode;

  if (props.error) {
    body = (
      <div className="col" role="alert">
        <p className="field__error">{props.error}</p>
        {props.onRetry ? (
          <div className="row">
            <button type="button" className="btn btn--sm" onClick={props.onRetry}>
              {t('app.retry')}
            </button>
          </div>
        ) : null}
      </div>
    );
  } else if (!account && props.loading) {
    body = (
      <div className="col" aria-busy="true" aria-label={t('app.loading')}>
        <span className="skeleton skeleton--block" />
        <span className="skeleton skeleton--text" />
        <span className="skeleton skeleton--text" />
        <span className="skeleton skeleton--text" />
      </div>
    );
  } else if (!account) {
    body = (
      <div className="empty">
        <p className="empty__text">{t('account.no_data')}</p>
        {props.onRetry ? (
          <button type="button" className="btn btn--sm" onClick={props.onRetry}>
            {t('app.retry')}
          </button>
        ) : null}
      </div>
    );
  } else {
    const currency = account.currency;
    const marginModeKey = MARGIN_MODE_KEYS[account.margin_mode?.toLowerCase() ?? ''];

    body = (
      <>
        {account.name || account.server ? (
          <div className="row row--tight row--wrap">
            {account.name ? <span className="field__hint">{account.name}</span> : null}
            {account.server ? (
              <span className="field__hint" title={t('account.server')}>
                {account.server}
              </span>
            ) : null}
          </div>
        ) : null}

        <div className="row row--wide">
          <div className="col col--tight">
            <span className="field__label">{t('account.equity')}</span>
            <span className="mono mono--xl">{formatCurrency(account.equity, currency, locale)}</span>
          </div>
          <span className="spacer" />
          <div className="col col--tight" style={{ alignItems: 'flex-end' }}>
            <span className="field__label">{t('account.open_pl')}</span>
            <span className="mono mono--xl" style={profitStyle(account.profit)}>
              {formatSignedCurrency(account.profit, currency, locale)}
            </span>
          </div>
        </div>

        <div className="col col--tight">
          <Readout label={t('account.balance')}>
            <span className="mono">{formatCurrency(account.balance, currency, locale)}</span>
          </Readout>

          <Readout label={t('account.margin_free')}>
            <span className="mono">{formatCurrency(account.margin_free, currency, locale)}</span>
          </Readout>

          <Readout label={t('account.today_pl')} hint={t('account.today_pl_hint')}>
            <span className="mono" style={profitStyle(account.today_profit)}>
              {formatSignedCurrency(account.today_profit, currency, locale)}
            </span>
          </Readout>

          <Readout label={t('account.positions_open')}>
            <span className="mono">{formatInteger(account.open_positions, locale)}</span>
          </Readout>

          <Readout label={t('account.orders_pending')}>
            <span className="mono">{formatInteger(account.pending_orders, locale)}</span>
          </Readout>

          <Readout label={t('account.leverage')}>
            <span className="mono">
              {Number.isFinite(account.leverage)
                ? t('account.leverage_value', { value: account.leverage })
                : EMPTY_VALUE}
            </span>
          </Readout>

          <Readout label={t('account.margin_mode')} hint={account.margin_mode || undefined}>
            <span>{marginModeKey ? t(marginModeKey) : account.margin_mode || EMPTY_VALUE}</span>
          </Readout>

          <Readout label={t('account.currency')}>
            <span className="mono">{currency || EMPTY_VALUE}</span>
          </Readout>

          <Readout label={t('account.login')}>
            <span className="mono">
              {account.login === null ? EMPTY_VALUE : formatInteger(account.login, locale)}
            </span>
          </Readout>
        </div>
      </>
    );
  }

  return (
    <section className={props.className ? `card ${props.className}` : 'card'}>
      <header className="card__header">
        <h2 className="card__title">{t('account.title')}</h2>
        <div className="card__actions">
          {stale ? (
            <span
              className="badge badge--warn"
              title={t('account.updated', { time: formatRelative(tickedAt, locale, now) })}
            >
              {t('feed.stale')}
            </span>
          ) : null}
        </div>
      </header>

      {/* The dim for a stale readout is a CSS state (components.css), not an
          inline opacity: as a rule it can fade instead of snapping. */}
      <div className="card__body" data-stale={stale ? 'true' : undefined}>
        {body}
      </div>

      {account ? (
        <footer className="card__footer">
          {/* This sentence re-counts every second. `.tnum` keeps the interface
              face and gives it tabular digits, so the words around the number
              do not shuffle sideways once a second. */}
          <span className="field__hint tnum">
            {t('account.updated', { time: formatRelative(tickedAt, locale, now) })}
          </span>
        </footer>
      ) : null}
    </section>
  );
}

export default AccountPanel;
