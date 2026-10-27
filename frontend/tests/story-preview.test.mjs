import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const publicRoot = fileURLToPath(new URL('../public/', import.meta.url));

function jpegSize(bytes) {
  assert.equal(bytes.readUInt16BE(0), 0xffd8);
  let cursor = 2;
  while (cursor + 9 < bytes.length) {
    assert.equal(bytes[cursor++], 0xff);
    const marker = bytes[cursor++];
    const length = bytes.readUInt16BE(cursor);
    if ([0xc0, 0xc1, 0xc2].includes(marker)) {
      return [bytes.readUInt16BE(cursor + 5), bytes.readUInt16BE(cursor + 3)];
    }
    cursor += length;
  }
  throw new Error('JPEG does not contain dimensions');
}

for (const [path, locale] of [['index.html','ru'], ['n/index.html','en'], ['en/index.html','en'], ['ru/index.html','ru'], ['uz/index.html','uz']]) {
  test(`story ${path} exposes a reachable ${locale} social preview`, async () => {
    const html = await readFile(`${publicRoot}/assets/showcase/${path}`, 'utf8');
    const image = html.match(/<meta property="og:image" content="([^"]+)"/);
    assert.ok(image, 'missing Open Graph image');
    const url = new URL(image[1]);
    assert.equal(url.origin, 'https://lvenc.site');
    assert.equal(url.pathname, `/assets/previews/story-${locale}-20261027.jpg`);
    assert.match(html, /<meta property="og:title" content="[^"]+"/);
    assert.match(html, /<meta property="og:description" content="[^"]+"/);
    assert.match(html, /<meta name="twitter:card" content="summary_large_image"/);
    assert.deepEqual(jpegSize(await readFile(`${publicRoot}${url.pathname}`)), [1200, 630]);
  });
}
