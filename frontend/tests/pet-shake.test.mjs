import test from 'node:test';
import assert from 'node:assert/strict';
import { createShakeDetector } from '../public/assets/showcase/pet-shake.js';

function run(points) {
  const detector = createShakeDetector(0, 0, 0);
  return points.map(([x, y, time]) => detector.sample(x, y, time));
}

test('recognizes deliberate fast back-and-forth shaking horizontally and vertically', () => {
  for (const points of [
    [[35,0,100],[0,0,200],[35,0,300],[0,0,400]],
    [[0,35,100],[0,0,200],[0,35,300],[0,0,400]],
  ]) assert.deepEqual(run(points), [false,false,false,true]);
});

test('ordinary dragging, small jitter and slow reversals do not trigger a shake', () => {
  assert.ok(run([[30,0,100],[60,0,200],[90,0,300],[120,0,400]]).every(v=>!v));
  assert.ok(run([[3,0,100],[0,0,200],[3,0,300],[0,0,400]]).every(v=>!v));
  assert.ok(run([[35,0,1000],[0,0,2000],[35,0,3000],[0,0,4000]]).every(v=>!v));
});

test('a recognized shake stays recognized until the drag ends', () => {
  const detector=createShakeDetector(0,0,0);
  for(const [x,t] of [[35,100],[0,200],[35,300],[0,400]]) detector.sample(x,0,t);
  assert.equal(detector.sample(0,0,500),true);
});
