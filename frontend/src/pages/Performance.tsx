import { useEffect, useState } from 'react';
import { PublicLayout } from '@/components/PublicLayout';
import { useLocale } from '@/i18n';
import '@/styles/performance.css';

type Day = {
  date: string; net: number; positions: number; wins: number; losses: number;
  stops: number; opened: number; unknown: number; state: string; level?: number; weekend: boolean;
};
type Period = {
  start: string; end: string; net: number; active_days: number; win_days: number;
  loss_days: number; unknown: number; days: Day[];
};
type Snapshot = { today: string; currency: string; timezone: string; week: Period; month: Period };

const copy = {
  ru: {
    title: 'Статистика торговли', week: 'Неделя', month: 'Месяц', net: 'Чистый результат',
    days: 'Торговых дней', wins: 'Дней в плюс', losses: 'Дней в минус', positions: 'Закрыто позиций',
    positive: 'Прибыльных', negative: 'Убыточных', stops: 'По стопу', loading: 'Загружаем статистику…',
    unavailable: 'Статистика пока недоступна', noTrades: 'Нет закрытых позиций', future: 'День ещё не наступил',
    untracked: 'Вне периода учёта', unknown: 'Часть результатов ещё не подтверждена', all: 'Весь месяц',
    scope: 'Результат по закрытиям · время Ташкента', idle: 'Без сделок', weekend: 'Выходной', flat: 'Нулевой итог',
    opened: 'Открыто позиций', entryOnly: 'Были входы, закрытий не было',
  },
  en: {
    title: 'Trading performance', week: 'Week', month: 'Month', net: 'Net result',
    days: 'Trading days', wins: 'Positive days', losses: 'Negative days', positions: 'Closed positions',
    positive: 'Profitable', negative: 'Unprofitable', stops: 'Stopped out', loading: 'Loading performance…',
    unavailable: 'Performance is unavailable', noTrades: 'No closed positions', future: 'Upcoming day',
    untracked: 'Outside tracking period', unknown: 'Some results are unconfirmed', all: 'Full month',
    scope: 'P&L by close date · Tashkent time', idle: 'No trades', weekend: 'Weekend', flat: 'Flat',
    opened: 'Opened positions', entryOnly: 'Entries without a close',
  },
  uz: {
    title: 'Savdo natijalari', week: 'Hafta', month: 'Oy', net: 'Sof natija',
    days: 'Savdo kunlari', wins: 'Foydali kunlar', losses: 'Zararli kunlar', positions: 'Yopilgan pozitsiyalar',
    positive: 'Foydali', negative: 'Zararli', stops: 'Stop bilan', loading: 'Natijalar yuklanmoqda…',
    unavailable: 'Natijalar hozircha mavjud emas', noTrades: 'Yopilgan pozitsiyalar yo‘q', future: 'Kelgusi kun',
    untracked: 'Hisob davridan tashqari', unknown: 'Ayrim natijalar tasdiqlanmagan', all: 'Butun oy',
    scope: 'Yopilish sanasi bo‘yicha natija · Toshkent vaqti', idle: 'Savdo yo‘q', weekend: 'Dam olish kuni', flat: 'Nol natija',
    opened: 'Ochilgan pozitsiyalar', entryOnly: 'Pozitsiyalar ochilgan, yopilish bo‘lmagan',
  },
};

const asDate = (day: string) => new Date(`${day}T00:00:00Z`);
const weekday = (day: string) => (asDate(day).getUTCDay() + 6) % 7;

function calendarWeeks(days: Day[]) {
  const entries: (Day | null)[] = [...Array<null>(weekday(days[0]!.date)).fill(null), ...days];
  while (entries.length % 7) entries.push(null);
  return Array.from({ length: entries.length / 7 }, (_, i) => entries.slice(i * 7, i * 7 + 7));
}

export default function Performance() {
  const { locale } = useLocale();
  const t = copy[locale];
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [failed, setFailed] = useState(false);
  const [tab, setTab] = useState<'week' | 'month'>('week');
  const [selected, setSelected] = useState<string>();
  const [monthWeek, setMonthWeek] = useState<number>();

  useEffect(() => {
    const controller = new AbortController();
    async function refresh() {
      try {
        const response = await fetch('/api/performance', { signal: controller.signal, credentials: 'omit' });
        if (!response.ok) throw new Error(String(response.status));
        const data: Snapshot = await response.json();
        setSnapshot(data);
        setFailed(false);
      } catch {
        if (!controller.signal.aborted) setFailed(true);
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 60_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, []);

  const period = snapshot?.[tab];
  const weeks = period ? calendarWeeks(period.days) : [];
  const chosenWeek = tab === 'month' && monthWeek !== undefined ? weeks[monthWeek] : undefined;
  const visible = chosenWeek ? [chosenWeek] : weeks;
  const visibleDays = visible.flat().filter((item): item is Day => item !== null);
  const day = visibleDays.find(item => item.date === selected)
    ?? visibleDays.find(item => item.date === snapshot?.today)
    ?? visibleDays[0];
  const weekTotals = chosenWeek && {
    net: visibleDays.reduce((sum, item) => sum + Math.round(item.net * 100), 0) / 100,
    active_days: visibleDays.filter(item => item.positions || item.opened).length,
    win_days: visibleDays.filter(item => item.state === 'win').length,
    loss_days: visibleDays.filter(item => item.state === 'loss').length,
    unknown: visibleDays.reduce((sum, item) => sum + item.unknown, 0),
  };
  const totals = weekTotals || period;
  const money = new Intl.NumberFormat(locale, { style: 'currency', currency: snapshot?.currency ?? 'USD', signDisplay: 'exceptZero' });
  const longDate = new Intl.DateTimeFormat(locale, { dateStyle: 'long', timeZone: 'UTC' });
  const shortDay = new Intl.DateTimeFormat(locale, { weekday: 'short', timeZone: 'UTC' });
  const cellText = (item: Day) => item.positions ? money.format(item.net) : '—';
  const stateText = (item: Day) => item.state === 'future' ? t.future
    : item.state === 'untracked' ? t.untracked : item.state === 'weekend' ? t.weekend
    : item.state === 'idle' ? t.noTrades : item.state === 'flat' ? t.flat : item.state === 'open' ? t.entryOnly
    : item.unknown ? t.unknown : money.format(item.net);

  return (
    <PublicLayout>
      <section className="performance">
        <header className="performance__heading">
          <h1>{t.title}</h1>
          <p>{t.scope}</p>
        </header>
        {!snapshot || !period || !totals ? (
          <p role="status">{failed ? t.unavailable : t.loading}</p>
        ) : <>
          <nav className="performance__switch" aria-label={t.title}>
            {(['week', 'month'] as const).map(value => (
              <button key={value} type="button" aria-pressed={tab === value} onClick={() => {
                setTab(value); setSelected(undefined); setMonthWeek(undefined);
              }}>{t[value]}</button>
            ))}
          </nav>
          <div key={tab} className="performance__period">
            <dl className="performance__metrics">
              {[[t.net, money.format(totals.net)], [t.days, totals.active_days],
                [t.wins, totals.win_days], [t.losses, totals.loss_days]].map(([label, value]) => (
                <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
              ))}
            </dl>
            {totals.unknown > 0 && <p role="status">{t.unknown} ({totals.unknown})</p>}
            <section className="performance__calendar" aria-label={t[tab]}>
              <h2>{longDate.formatRange(asDate(period.start), asDate(period.end))}</h2>
              {tab === 'month' && <nav className="performance__weeks" aria-label={t.week}>
                <button type="button" aria-pressed={monthWeek === undefined} onClick={() => {
                  setMonthWeek(undefined); setSelected(undefined);
                }}>{t.all}</button>
                {weeks.map((week, index) => (
                  <button key={index} type="button" aria-pressed={monthWeek === index}
                    onClick={() => { setMonthWeek(index); setSelected(week.find(Boolean)?.date); }}>
                    {t.week} {index + 1}
                  </button>
                ))}
              </nav>}
              <div className="performance__grid">
                {Array.from({ length: 7 }, (_, i) => <span className="performance__weekday" key={`w${i}`}>
                  {shortDay.format(new Date(Date.UTC(2024, 0, i + 1)))}
                </span>)}
                {visible.flat().map((item, index) => item ? (
                  <button key={item.date} type="button" data-state={item.state} data-level={item.level}
                    data-weekend={item.weekend} aria-pressed={day?.date === item.date}
                    aria-current={item.date === snapshot.today ? 'date' : undefined}
                    aria-label={`${longDate.format(asDate(item.date))}: ${stateText(item)}`}
                    onClick={() => setSelected(item.date)}>
                    <b>{Number(item.date.slice(-2))}</b><small>{cellText(item)}</small>
                  </button>
                ) : <span aria-hidden="true" key={`e${index}`} />)}
              </div>
              <div className="performance__legend">
                {[[t.wins, 'win'], [t.losses, 'loss'], [t.idle, 'idle'], [t.weekend, 'weekend'], [t.flat, 'flat']].map(([label, state]) =>
                  <span key={state} data-state={state}>{label}</span>)}
              </div>
            </section>
            {day && <section className="performance__detail" key={day.date} aria-live="polite">
              <h2>{longDate.format(asDate(day.date))}</h2>
              {day.unknown > 0 && <p>{t.unknown} ({day.unknown})</p>}
              {day.positions || day.opened ? <dl className="performance__metrics">
                {[[t.net, money.format(day.net)], [t.opened, day.opened], [t.positions, day.positions], [t.positive, day.wins],
                  [t.negative, day.losses], [t.stops, day.stops]].map(([label, value]) => (
                  <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
                ))}
              </dl> : <p>{stateText(day)}</p>}
            </section>}
          </div>
        </>}
      </section>
    </PublicLayout>
  );
}
