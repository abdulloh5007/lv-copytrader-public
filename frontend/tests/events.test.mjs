import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeStreamEvent, isStreamEvent } from '../src/api/events.ts';

const signal = { id: 31, source_id: 1, message_id: 42, revision: 1, kind: 'text', status: 'skipped', text: 'test signal', images: [], image_count: 0, runs_count: 0, trades_count: 0 };
const envelope = data => ({ type: 'signal.received', seq: 100, ts: 1720000000, data });

test('real flat ingest payload becomes the shape the live feed consumes', () => {
  const incoming = envelope(signal);
  const event = normalizeStreamEvent(incoming);
  assert.equal(event.data.signal.id, 31);
  assert.equal(event.data.signal.deleted_at, null);
  assert.equal(event.seq, 100);
  assert.equal(event.ts, 1720000000);
  assert.equal(isStreamEvent(event), true);
  assert.equal(incoming.data.signal, undefined);
});
test('nested older envelopes remain supported', () => {
  assert.equal(normalizeStreamEvent(envelope({ signal })).data.signal.message_id, 42);
});
test('malformed and unknown frames never reach feed reducers', () => {
  for (const value of [null, [], {}, envelope({}), envelope({ id: 1 }), envelope({ ...signal, images: null }), envelope({ ...signal, id: '31' }), { type: 'unknown', data: {} }]) {
    assert.equal(normalizeStreamEvent(value), null);
  }
});
test('non-signal event envelopes retain their payload', () => {
  const value = { type: 'account.tick', seq: 101, ts: 1720000001, data: { account: { equity: 100 } } };
  assert.equal(normalizeStreamEvent(value), value);
});
test('replayed flat signals are normalized identically to live arrivals', () => {
  const recent = [envelope(signal), envelope({ ...signal, id: 32 })];
  assert.deepEqual(recent.map(normalizeStreamEvent).map(v => v.data.signal.id), [31, 32]);
});
