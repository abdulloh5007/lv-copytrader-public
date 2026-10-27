import test from 'node:test';
import assert from 'node:assert/strict';
import { advanceStoryTime } from '../public/assets/showcase/story-speed.js';

const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);

test('signal runs at four times speed before the trade ends', () => {
  const result = advanceStoryTime(10, .5, true, 20);
  near(result.advance, 2);
  assert.equal(result.stop, false);
});

test('crossing the trade end returns to normal speed within the same frame', () => {
  const result = advanceStoryTime(19, .5, true, 20);
  near(result.advance, 1.25);
  assert.equal(result.stop, true);
});

test('trade completion exactly at frame boundary disables acceleration', () => {
  const result = advanceStoryTime(19, .25, true, 20);
  near(result.advance, 1);
  assert.equal(result.stop, true);
});

test('no acceleration after completion, even if a frame arrived late', () => {
  for (const time of [20, 21]) {
    const result = advanceStoryTime(time, .5, true, 20);
    near(result.advance, .5);
    assert.equal(result.stop, true);
  }
});

test('normal playback and an uninitialized trade advance safely', () => {
  near(advanceStoryTime(10, .5, false, 20).advance, .5);
  near(advanceStoryTime(0, .5, true, null).advance, 2);
});
