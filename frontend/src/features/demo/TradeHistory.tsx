import { useState } from 'react';
import { Icon } from '@/components/Icon';
import { useCurrentLocale, useT } from '@/i18n';
import type { DemoTrade } from './types';

export function TradeHistory({
  trades,
  currency,
  fresh,
}: {
  trades: DemoTrade[] | undefined;
  currency: string;
  fresh: boolean;
}) {
  const t = useT();
  const locale = useCurrentLocale();
  const [limit, setLimit] = useState(8);
  const date = (value: string | null, full = false) =>
    value
      ? new Intl.DateTimeFormat(locale, {
          day: 'numeric',
          month: 'short',
          hour: '2-digit',
          minute: '2-digit',
          ...(full ? { year: 'numeric' } : {}),
        }).format(new Date(value))
      : '—';
  const price = (value: number | null) =>
    value === null
      ? '—'
      : new Intl.NumberFormat(locale, { maximumFractionDigits: 6 }).format(
          value,
        );
  const money = (value: number | null) =>
    value === null
      ? '—'
      : new Intl.NumberFormat(locale, {
          style: currency ? 'currency' : 'decimal',
          currency: currency || undefined,
          signDisplay: 'exceptZero',
          maximumFractionDigits: 2,
        }).format(value);
  return (
    <section
      className="demo-panel demo-history"
      aria-labelledby="demo-history-title"
    >
      <header className="demo-panel__heading">
        <h2 id="demo-history-title">{t('demo.history')}</h2>
        <span className="demo-caption">
          {!fresh && trades
            ? t('demo.last_saved')
            : t('demo.closed_trades', { count: trades?.length ?? 0 })}
        </span>
      </header>
      {trades === undefined ? (
        <div className="demo-history__loading" aria-busy="true">
          <span className="skeleton skeleton--text" />
          <span className="skeleton skeleton--text" />
          <span className="skeleton skeleton--text" />
        </div>
      ) : !trades.length ? (
        <div className="demo-empty">{t('demo.no_trades')}</div>
      ) : (
        <ol className="demo-trades">
          {trades.slice(0, limit).map((trade, index) => (
            <li key={`${trade.closed_at}:${trade.symbol}:${index}`}>
              <details className="demo-trade">
                <summary>
                  <span
                    className={`demo-direction demo-direction--${trade.side}`}
                  >
                    <svg
                      viewBox="0 0 20 20"
                      width="18"
                      height="18"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      aria-hidden="true"
                    >
                      <path
                        d={
                          trade.side === 'sell'
                            ? 'M5 5l10 10m-7 0h7V8'
                            : 'M5 15 15 5m-7 0h7v7'
                        }
                      />
                    </svg>
                  </span>
                  <span className="demo-trade__asset">
                    <strong>{trade.symbol}</strong>
                    <span>
                      {t(
                        trade.side === 'buy'
                          ? 'demo.buy'
                          : trade.side === 'sell'
                            ? 'demo.sell'
                            : 'demo.trade',
                      )}
                      <span className="demo-trade__volume">
                        {' '}
                        · {price(trade.volume)} {t('demo.lot')}
                      </span>
                    </span>
                  </span>
                  <time
                    className="demo-trade__time"
                    dateTime={trade.closed_at || undefined}
                  >
                    {date(trade.closed_at)}
                  </time>
                  <span
                    className="demo-trade__profit mono"
                    data-sign={
                      trade.profit === null
                        ? 'unknown'
                        : trade.profit > 0
                          ? 'positive'
                          : trade.profit < 0
                            ? 'negative'
                            : 'zero'
                    }
                  >
                    {money(trade.profit)}
                  </span>
                  <span className="demo-trade__chevron" aria-hidden="true">
                    ⌄
                  </span>
                </summary>
                <dl className="demo-trade__details">
                  <div>
                    <dt>{t('demo.entry')}</dt>
                    <dd className="mono">{price(trade.entry)}</dd>
                  </div>
                  <div>
                    <dt>{t('demo.exit')}</dt>
                    <dd className="mono">{price(trade.exit)}</dd>
                  </div>
                  <div>
                    <dt>{t('demo.volume')}</dt>
                    <dd className="mono">{price(trade.volume)}</dd>
                  </div>
                  <div>
                    <dt>{t('demo.opened')}</dt>
                    <dd>{date(trade.opened_at, true)}</dd>
                  </div>
                  <div>
                    <dt>{t('demo.closed')}</dt>
                    <dd>{date(trade.closed_at, true)}</dd>
                  </div>
                </dl>
              </details>
            </li>
          ))}
        </ol>
      )}
      {trades && trades.length > limit && (
        <div className="demo-history__more">
          <button
            className="btn btn--ghost btn--sm"
            type="button"
            onClick={() => setLimit((n) => n + 8)}
          >
            {t('demo.more')}
            <Icon name="arrow" size={14} />
          </button>
        </div>
      )}
    </section>
  );
}
