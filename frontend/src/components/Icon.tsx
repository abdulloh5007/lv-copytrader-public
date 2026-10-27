import type { CSSProperties } from 'react';
import { brandPaths } from './brandPaths';

export type IconName =
  | 'sun'
  | 'moon'
  | 'arrow'
  | 'check'
  | 'menu'
  | 'close'
  | 'overview'
  | 'positions'
  | 'history'
  | 'settings'
  | 'logout'
  | 'eye'
  | 'eye-off';
const paths: Record<IconName, string> = {
  sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.4 1.4m11.2 11.2L19 19M5 19l1.4-1.4M17.6 6.4 19 5',
  moon: 'M20.5 13A8.5 8.5 0 0 1 11 3.5 8.5 8.5 0 1 0 20.5 13Z',
  arrow: 'M4 12h15m-6-6 6 6-6 6',
  check: 'm5 12 4 4L19 6',
  menu: 'M4 7h16M4 12h16M4 17h16',
  close: 'm6 6 12 12M6 18 18 6',
  overview: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  positions: 'M5 4v16m0-6h4M12 4v16m0-13h4M19 4v16M3 8h4m3 7h4m3-5h4',
  history: 'M3 11a9 9 0 1 1 2.6 7.3M3 4v7h7m2-4v5l3 2',
  settings:
    'M9 4h6l1 3 3 1 2 5-3 2v3l-5 3-3-2-3 1-4-4 1-3-2-3 3-4 4 1ZM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
  logout: 'M9 4H4v16h5m6-12 4 4-4 4m-6-4h10',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Zm10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
  'eye-off':
    'm3 3 18 18M9.8 5.2A12 12 0 0 1 12 5c6 0 10 7 10 7a18 18 0 0 1-3 4M6 6C3.5 8 2 12 2 12s4 7 10 7a12 12 0 0 0 5-1',
};
export function Icon({
  name,
  size = 18,
  style,
}: {
  name: IconName;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      <path d={paths[name]} />
    </svg>
  );
}
interface BrandMarkProps {
  variant?: 'mini' | 'icon' | 'logo';
  markColor?: string;
  textColor?: string;
  highlightColor?: string;
}

export function BrandMark({ variant = 'mini', markColor, textColor, highlightColor }: BrandMarkProps) {
  const style = {
    '--brand-mark': markColor,
    '--brand-text': textColor,
    '--brand-highlight': highlightColor,
  } as CSSProperties;
  return (
    <span className={`brand-mark brand-mark--${variant}`} aria-hidden="true" style={style}>
      <svg viewBox={`0 0 ${variant === 'logo' ? brandPaths.width : 210} 100`} fill="none" focusable="false">
        <path d={brandPaths.highlight} fill="var(--brand-highlight, var(--brand-text, currentColor))" />
        <path d={brandPaths.mark} fill="var(--brand-mark, #219df3)" />
        {variant === 'logo' && brandPaths.wordmark.map((part, index) => (
          <path key={index} d={part.d} fill={part.fill === 'mark' ? 'var(--brand-mark, #219df3)' : 'var(--brand-text, currentColor)'} />
        ))}
      </svg>
    </span>
  );
}
