import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';

const root = new URL('../public/assets/showcase/', import.meta.url);
const sources = ['lumi.js', 'lumi.css', 'lv1-action-rig.js', 'lv1-walk-rig.js'];

test('cached pet images are content-addressed and all referenced files exist', async () => {
  const refs = new Set();
  for (const name of sources) {
    const source = await readFile(new URL(name, root), 'utf8');
    for (const match of source.matchAll(/pets\/(?:lv1|lumi)\/[a-z0-9-]+-([a-f0-9]{12})\.(?:webp|png|svg)/g)) refs.add(match[0]);
  }
  assert.equal(refs.size, 3);
  for (const path of refs) {
    const digest = createHash('sha256').update(await readFile(new URL(path, root))).digest('hex').slice(0, 12);
    assert.match(path, new RegExp(`-${digest}\\.(?:webp|png|svg)$`));
  }
});
