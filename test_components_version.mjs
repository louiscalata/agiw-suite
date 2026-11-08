import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('./web/components.js', import.meta.url), 'utf8');

function run(components) {
  const els = {};
  const element = () => ({ textContent: '', disabled: false, title: '', addEventListener() {} });
  const tag = { textContent: 'v0.2.0' };
  const document = {
    hidden: false, getElementById: id => (els[id] ||= element()), querySelector: q => (q === '#nisiHeading span' ? tag : null), addEventListener() {},
  };
  const window = { addEventListener() {}, webkit: undefined };
  const fetch = async path => path === '/api/components'
    ? { ok: true, status: 200, json: async () => components } : { ok: true, status: 200, json: async () => ({}) };
  const context = vm.createContext({ document, window, fetch, AbortSignal, setTimeout: () => 1, clearTimeout() {}, console });
  vm.runInContext(source, context);
  return new Promise(resolve => setTimeout(() => resolve({ tag: tag.textContent, integrity: els.integrity.textContent }), 30));
}

test('the Nisi version on Components comes from the reply, in a strict form only', async () => {
  const verified = version => ({ nisi: { integrity: 'verified', version, entry: '/x/nisi.mjs' }, node: { found: true }, check: { state: 'not-run' } });
  assert.deepEqual(await run(verified('0.2.0')), { tag: 'v0.2.0', integrity: 'Verified · public 0.2.0 payload' });
  assert.deepEqual(await run(verified('0.3.14')), { tag: 'v0.3.14', integrity: 'Verified · public 0.3.14 payload' });
  for (const bad of ['<b>9</b>', '1.2', '1.2.3.4', '1234.1.1', '1.2.3-rc1', ' 1.2.3', '1.2.3\n', '', null, undefined, 7, ['1.2.3'], { toString: () => '1.2.3' }]) {
    const out = await run(verified(bad));
    assert.equal(out.tag, 'v0.2.0', `the heading is left alone for ${JSON.stringify(bad)}`);
    assert.equal(out.integrity, 'Verified · public payload', JSON.stringify(bad));
  }
  const unverified = await run({ nisi: { integrity: 'mismatch', version: '0.9.9' }, node: {}, check: {} });
  assert.equal(unverified.integrity, 'Unavailable · package verification failed');
});

test('the version is written as text, never as markup, and no version is hard-coded in the script', () => {
  assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML/);
  assert.doesNotMatch(source, /public 0\.2\.0|'v0\.2\.0'/);
  assert.match(source, /tag\.textContent='v'\+version/);
});
