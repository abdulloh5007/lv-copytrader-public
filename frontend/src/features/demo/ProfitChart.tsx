import { memo, useId, useMemo, useState } from 'react';
import { useT, useCurrentLocale } from '@/i18n';

import { chartGeometry } from './geometry';

export const ProfitChart = memo(function ProfitChart({
  values,
  currency,
}: {
  values: { date: string; profit: number }[];
  currency: string;
}) {
  const t = useT();
  const locale = useCurrentLocale();
  const id = useId().replaceAll(':', '');
  const [selected, setSelected] = useState<number | null>(null);
  const geometry = useMemo(() => chartGeometry(values), [values]);
  const format = (n: number) =>
    new Intl.NumberFormat(locale, {
      style: currency ? 'currency' : 'decimal',
      currency: currency || undefined,
      maximumFractionDigits: 2,
    }).format(n);
  const date = (v: string) =>
    new Intl.DateTimeFormat(locale, {
      day: 'numeric',
      month: 'short',
      timeZone: 'UTC',
    }).format(new Date(v + 'T12:00:00Z'));
  if (!geometry)
    return (
      <div className="demo-chart-empty">
        <span className="demo-chart-empty__line" />
        <p>{t('demo.no_trades')}</p>
      </div>
    );
  const current =
    selected === null
      ? geometry.points.at(-1)!
      : geometry.points[Math.min(selected, geometry.points.length - 1)];
  const positive = geometry.points.at(-1)!.profit >= 0;
  return (
    <div className="demo-chart" data-positive={positive}>
      <div className="demo-chart__readout" aria-hidden="true">
        <span>{date(current.date)}</span>
        <span className="mono">{format(current.profit)}</span>
      </div>
      <svg
        viewBox="0 0 900 210"
        role="img"
        aria-labelledby={`${id}-title`}
        tabIndex={0}
        onPointerMove={(e) => {
          const bounds = e.currentTarget.getBoundingClientRect();
          setSelected(
            Math.max(
              0,
              Math.min(
                geometry.points.length - 1,
                Math.round(
                  ((e.clientX - bounds.left) / bounds.width) *
                    (geometry.points.length - 1),
                ),
              ),
            ),
          );
        }}
        onPointerLeave={() => setSelected(null)}
        onBlur={() => setSelected(null)}
        onKeyDown={(e) => {
          if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
            e.preventDefault();
            setSelected((p) =>
              Math.max(
                0,
                Math.min(
                  geometry.points.length - 1,
                  (p ?? geometry.points.length - 1) +
                    (e.key === 'ArrowRight' ? 1 : -1),
                ),
              ),
            );
          }
        }}
      >
        <title id={`${id}-title`}>
          {t('demo.chart_label')} — {format(current.profit)},{' '}
          {date(current.date)}
        </title>
        <defs>
          <linearGradient id={`${id}-fill`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity=".14" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[35, 105, 175].map((y) => (
          <line
            key={y}
            x1="16"
            x2="884"
            y1={y}
            y2={y}
            className="demo-chart__grid"
          />
        ))}
        <line
          x1="16"
          x2="884"
          y1={geometry.zero}
          y2={geometry.zero}
          className="demo-chart__zero"
        />
        {geometry.points.length > 1 && (
          <path
            d={`${geometry.path} L884,210 L16,210 Z`}
            fill={`url(#${id}-fill)`}
            stroke="none"
          />
        )}
        <path
          d={geometry.path}
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
        {selected !== null && (
          <line
            x1={current.x}
            x2={current.x}
            y1="0"
            y2="210"
            className="demo-chart__cursor"
          />
        )}
        <circle
          cx={current.x}
          cy={current.y}
          r="4"
          fill="currentColor"
          stroke="var(--surface)"
          strokeWidth="2"
        />
      </svg>
      <div className="demo-chart__dates">
        <span>{date(geometry.points[0].date)}</span>
        <span>{date(geometry.points.at(-1)!.date)}</span>
      </div>
    </div>
  );
});
