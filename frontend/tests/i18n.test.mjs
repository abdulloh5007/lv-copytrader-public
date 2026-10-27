import test from 'node:test';
import assert from 'node:assert/strict';
import { en } from '../src/i18n/en.ts';
import { ru } from '../src/i18n/ru.ts';
import { uz } from '../src/i18n/uz.ts';

test('all three UI dictionaries cover the same keys and preserve interpolation variables', () => {
  const keys = Object.keys(en).sort();
  const placeholders = value => [...value.matchAll(/\{(\w+)\}/g)].map(match => match[1]).sort();
  for (const [language, dictionary] of Object.entries({en, ru, uz})) {
    assert.deepEqual(Object.keys(dictionary).sort(), keys, language);
    for (const key of keys) {
      assert.ok(dictionary[key].trim(), `${language}: ${key}`);
      assert.deepEqual(placeholders(dictionary[key]), placeholders(en[key]), `${language}: ${key}`);
    }
  }
});
