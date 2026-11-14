import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// A Components check POST that gets no answer may still have started on the observer, so the page must say the
// outcome is unknown. Only an answer that refuses the request, or a local guard before sending, may say it did not start.
const source = readFileSync(new URL('./web/components.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setTimeout(resolve, 20));

function page(post) {
  const els = {}, handlers = {}, posts = [];
  const element = id => ({
    id, textContent: '', disabled: false, title: '', open: false, isConnected: true,
    addEventListener(type, fn) { (handlers[id] ||= {})[type] = fn; },
    showModal() { this.open = true; }, close() { this.open = false; }, focus() {},
  });
  const document = {
    hidden: false, activeElement: null, getElementById: id => (els[id] ||= element(id)),
    querySelector: () => null, addEventListener() {},
  };
  const components = { nisi: { integrity: 'verified', version: '0.2.1', entry: '/x/nisi.mjs' }, node: { found: true }, check: { state: 'not-run' } };
  const fetch = async (path, options = {}) => {
    if (options.method === 'POST') { posts.push(path); return post(path); }
    if (path === '/api/components') return { ok: true, status: 200, json: async () => components };
    if (path === '/api/components/jev') return { ok: true, status: 200, json: async () => ({ configured: true, state: 'idle' }) };
    throw new Error('unexpected path ' + path);
  };
  const context = vm.createContext({ document, window: { addEventListener() {} }, fetch, AbortSignal, setTimeout: () => 1, clearTimeout() {}, console });
  vm.runInContext(source, context);
  const click = id => handlers[id].click();
  return {
    els, posts,
    async run(button) { await tick(); click(button); click('actionReviewConfirm'); await tick(); },
  };
}

const lostAnswer = async () => { throw new TypeError('Load failed'); };
const timedOut = async () => { throw new DOMException('The operation timed out.', 'TimeoutError'); };
const refused = async () => ({ ok: false, status: 503, json: async () => ({ status: 'error', message: 'busy' }) });

for (const [name, button, result] of [['self-check', 'check', 'result'], ['connection check', 'jevCheck', 'jevResult']]) {
  test(`a ${name} POST with no answer reports an unknown outcome, not a failure to start`, async () => {
    for (const post of [lostAnswer, timedOut]) {
      const p = page(post);
      await p.run(button);
      assert.equal(p.posts.length, 1, 'exactly one POST was attempted and none was retried');
      const text = p.els[result].textContent;
      assert.match(text, /outcome unknown/);
      assert.match(text, /may or may not have started/);
      assert.doesNotMatch(text, /could not start|did not start/);
    }
  });

  test(`a ${name} refused by the observer says it did not start`, async () => {
    const p = page(refused);
    await p.run(button);
    assert.equal(p.posts.length, 1);
    const text = p.els[result].textContent;
    assert.match(text, /did not start/);
    assert.match(text, /503/);
    assert.doesNotMatch(text, /outcome unknown/);
  });
}

test('the source no longer claims a check could not start after a lost answer', () => {
  assert.doesNotMatch(source, /could not start/);
});
