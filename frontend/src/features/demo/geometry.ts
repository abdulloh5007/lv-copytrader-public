export const chartGeometry = (
  values: { date: string; profit: number }[],
  width = 900,
  height = 210,
) => {
  const valid = values.filter((v) => Number.isFinite(v.profit));
  if (!valid.length) return null;
  const low = Math.min(0, ...valid.map((v) => v.profit));
  const high = Math.max(0, ...valid.map((v) => v.profit));
  const span = high - low || 1;
  const top = high + span * 0.15;
  const bottom = low - span * 0.15;
  const points = valid.map((v, index) => ({
    ...v,
    x:
      16 +
      (valid.length === 1 ? 0.5 : index / (valid.length - 1)) * (width - 32),
    y: 12 + ((top - v.profit) / (top - bottom)) * (height - 24),
  }));
  return {
    points,
    path: points
      .map(
        (p, index) => `${index ? 'L' : 'M'}${p.x.toFixed(2)},${p.y.toFixed(2)}`,
      )
      .join(' '),
    zero: 12 + (top / (top - bottom)) * (height - 24),
  };
};
