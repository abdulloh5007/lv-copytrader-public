import test from 'node:test';
import assert from 'node:assert/strict';
import { chartGeometry } from '../src/features/demo/geometry.ts';

test('empty or nonfinite history does not fabricate a chart', () => {
  assert.equal(chartGeometry([]), null);
  assert.equal(chartGeometry([{ date:'2026-10-21', profit:NaN }]), null);
});
test('flat and single-point histories have finite geometry', () => {
  for (const values of [[0], [0,0,0], [-25,-25], [100]]) {
    const chart = chartGeometry(values.map(profit=>({date:'2026-10-21', profit})));
    assert.ok(Number.isFinite(chart.zero));
    assert.ok(chart.points.every(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)));
    assert.ok(!/NaN|Infinity/.test(chart.path));
  }
});
test('loss and recovery preserve the actual profit values and ordering', () => {
  const profits=[100, -50, 20];
  const chart=chartGeometry(profits.map((profit,i)=>({date:`2026-10-${19+i}`,profit})));
  assert.deepEqual(chart.points.map(p=>p.profit), profits);
  assert.ok(chart.points[0].y < chart.points[1].y);
  assert.ok(chart.points[1].y > chart.points[2].y);
});
test('input is immutable and malformed values cannot break SVG coordinates', () => {
  const values=Object.freeze([Object.freeze({date:'2026-10-21',profit:0}), Object.freeze({date:'2026-10-22',profit:Infinity})]);
  const chart=chartGeometry(values);
  assert.equal(values.length,2);
  assert.equal(chart.points.length,1);
});
