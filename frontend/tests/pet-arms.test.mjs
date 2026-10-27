import test from 'node:test';
import assert from 'node:assert/strict';
import { armPose } from '../public/assets/showcase/lv1-arm-kinematics.js';

test('both arm segments keep their length through all movement angles', () => {
  const shoulder = [126, 114], upper = 23.77, lower = 21.1;
  for (let a = -180; a <= 180; a += 15) for (let b = -180; b <= 180; b += 15) {
    const { elbow, hand } = armPose(shoulder, upper, lower, a, b);
    assert.ok(Math.abs(Math.hypot(elbow[0]-shoulder[0],elbow[1]-shoulder[1])-upper)<1e-9);
    assert.ok(Math.abs(Math.hypot(hand[0]-elbow[0],hand[1]-elbow[1])-lower)<1e-9);
  }
});
