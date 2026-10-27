import { PublicLayout } from '@/components/PublicLayout';
import { BrandMark, Icon } from '@/components/Icon';
import { useT, useCurrentLocale } from '@/i18n';
import { useDemo } from '@/features/demo/useDemo';
import { ProfitChart } from '@/features/demo/ProfitChart';
import { TradeHistory } from '@/features/demo/TradeHistory';
import '../styles/demo.css';

export default function Demo() {
  const t = useT();
  const locale = useCurrentLocale();
  const { data, viewers, fresh, historyFresh, failed } = useDemo();
  const account = data?.account;
  const bot = data?.copytrader;
  const currency = account?.currency || '';
  const money = (value: number | null | undefined, signed = false) =>
    value == null
      ? '—'
      : new Intl.NumberFormat(locale, {
          style: currency ? 'currency' : 'decimal',
          currency: currency || undefined,
          maximumFractionDigits: 2,
          signDisplay: signed ? 'exceptZero' : 'auto',
        }).format(value);
  const integer = (value: number | null | undefined) =>
    value == null ? '—' : new Intl.NumberFormat(locale).format(value);
  const age = data?.account_updated_at
    ? Math.max(0, Date.now() / 1000 - data.account_updated_at)
    : 0;
  const relativeUpdate = new Intl.RelativeTimeFormat(locale, {
    numeric: 'auto',
    style: 'short',
  }).format(
    -Math.floor(age < 60 ? age : age / 60),
    age < 60 ? 'second' : 'minute',
  );
  const statusLabel = fresh
    ? t('demo.live')
    : data?.account
      ? t('demo.updated', { time: relativeUpdate })
      : failed
        ? t('demo.connection_lost')
        : t('demo.connecting');
  const viewersLabel = t(
    viewers !== null && new Intl.PluralRules(locale).select(viewers) === 'one'
      ? 'demo.watching_one'
      : 'demo.watching',
  );
  const accountMode =
    account?.mode === 'demo'
      ? t('demo.demo_account')
      : account?.mode === 'real'
        ? t('demo.real_account')
        : t('demo.account');
  return (
    <PublicLayout demo>
      <div className="demo-page">
        <header className="demo-intro">
          <div className="demo-intro__meta">
            <div className="demo-eyebrow">
              <span className="demo-live-dot" data-live={fresh} />
              {statusLabel}
            </div>
            {viewers !== null && (
              <div
                className="demo-viewers"
                aria-label={`${integer(viewers)} ${viewersLabel}`}
                title={t('demo.viewers_hint')}
              >
                <Icon name="eye" size={16} />
                <span className="tnum">{integer(viewers)}</span>
                <span>{viewersLabel}</span>
              </div>
            )}
          </div>
          <h1>{t('demo.title')}</h1>
        </header>
        <section className="demo-flow" aria-label={t('demo.flow_label')}>
          <div className="demo-flow__step">
            <div className="demo-flow__icon">
              <img src="/assets/brand/telegram-plane.svg" width="26" height="26" alt="" />
            </div>
            <div>
              <strong>Telegram</strong>
            </div>
          </div>
          <span className="demo-flow__connector" aria-hidden="true">
            <Icon name="arrow" size={16} />
          </span>
          <div className="demo-flow__step">
            <div className="demo-flow__icon demo-flow__icon--brand">
              <BrandMark />
            </div>
            <div>
              <strong>LV Copytrader</strong>
            </div>
          </div>
          <span className="demo-flow__connector" aria-hidden="true">
            <Icon name="arrow" size={16} />
          </span>
          <div className="demo-flow__step">
            <div className="demo-flow__icon">
              <img src="/assets/showcase/icons/metatrader.svg" width="26" height="26" alt="" />
            </div>
            <div>
              <strong>MetaTrader 5</strong>
            </div>
          </div>
        </section>
        <div className="demo-summary">
          <section
            className="demo-panel demo-balance"
            aria-labelledby="demo-account-title"
            data-stale={!!account && !fresh}
          >
            <div className="demo-panel__heading">
              <h2 id="demo-account-title">{t('demo.balance')}</h2>
              <span className="demo-account-mode">{accountMode}</span>
            </div>
            <div
              className={
                account
                  ? 'demo-balance__value tnum'
                  : 'demo-balance__value demo-placeholder'
              }
            >
              {money(account?.balance)}
            </div>
            <dl className="demo-balance__details">
              <div>
                <dt>{t('demo.equity')}</dt>
                <dd className="tnum">{money(account?.equity)}</dd>
              </div>
              <div>
                <dt>{t('demo.floating')}</dt>
                <dd
                  className="tnum"
                  data-sign={
                    (account?.floating_profit || 0) > 0
                      ? 'positive'
                      : (account?.floating_profit || 0) < 0
                        ? 'negative'
                        : 'zero'
                  }
                >
                  {money(account?.floating_profit, true)}
                </dd>
              </div>
            </dl>
          </section>
          <section
            className="demo-panel demo-performance"
            aria-labelledby="demo-performance-title"
          >
            <div className="demo-panel__heading">
              <h2 id="demo-performance-title">{t('demo.win_rate')}</h2>
              <span className="demo-caption">{t('demo.copytrader')}</span>
            </div>
            <div className="demo-performance__row">
              <div
                className="demo-win-ring"
                style={
                  {
                    '--win': `${bot?.win_rate || 0}%`,
                  } as import('react').CSSProperties
                }
              >
                <span className="tnum">
                  {bot?.win_rate == null
                    ? '—'
                    : `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(bot.win_rate)}%`}
                </span>
              </div>
              <dl>
                <div>
                  <dt>{t('demo.total_trades')}</dt>
                  <dd className="tnum">{integer(bot?.closed)}</dd>
                </div>
                <div>
                  <dt>{t('demo.open_positions')}</dt>
                  <dd className="tnum">{integer(bot?.open)}</dd>
                </div>
              </dl>
            </div>
          </section>
        </div>
        <section
          className="demo-panel demo-result"
          aria-labelledby="demo-result-title"
        >
          <div className="demo-result__header">
            <div>
              <h2 id="demo-result-title">{t('demo.result')}</h2>
              <div
                className="demo-result__value tnum"
                data-sign={
                  (bot?.profit || 0) > 0
                    ? 'positive'
                    : (bot?.profit || 0) < 0
                      ? 'negative'
                      : 'zero'
                }
              >
                {money(bot?.profit, true)}
              </div>
            </div>
            <span
              className="demo-automation"
              data-running={!!data?.status.running}
            >
              <span />
              {t(data?.status.running ? 'demo.running' : 'demo.paused')}
            </span>
          </div>
          {bot ? (
            <ProfitChart values={bot.curve} currency={currency} />
          ) : (
            <div className="demo-chart-loading" aria-busy="true">
              <span className="skeleton skeleton--text" />
            </div>
          )}
          <div className="demo-result__legend">
            <span>{t('demo.result_note')}</span>
            <span
              title={
                data?.history_updated_at
                  ? new Date(data.history_updated_at * 1000).toLocaleString(
                      locale,
                    )
                  : undefined
              }
            >
              {t(
                historyFresh
                  ? 'demo.synced'
                  : data?.copytrader
                    ? 'demo.last_saved'
                    : 'demo.connecting',
              )}
            </span>
          </div>
        </section>
        <TradeHistory
          trades={bot?.trades}
          currency={currency}
          fresh={historyFresh}
        />
      </div>
    </PublicLayout>
  );
}
