import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./web/simple-view.mjs', import.meta.url), 'utf8')
  .replace("'/model-control-view.mjs'", JSON.stringify(new URL('./web/model-control-view.mjs', import.meta.url).href))
  .replace("'/monitor-store.mjs'", JSON.stringify(new URL('./web/monitor-store.mjs', import.meta.url).href));
const { textSizeChoice, mountTextSize, TEXT_SIZES } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function fakeDom() {
  const created = [];
  const make = tag => {
    const node = { tag, children: [], attrs: {}, listeners: {}, className: '', id: '', value: '', textContent: '',
      append(...n) { this.children.push(...n); }, setAttribute(k, v) { this.attrs[k] = v; },
      addEventListener(t, f) { (this.listeners[t] ||= []).push(f); } };
    created.push(node);
    return node;
  };
  const placed = [];
  const theme = { after(node) { placed.push(node); } };
  const document = {
    documentElement: { dataset: {} },
    createElement: make,
    createTextNode: text => ({ text }),
    getElementById: id => (id === 'jaryoTheme' ? { closest: () => theme } : null),
  };
  return { document, placed, created };
}

test('only the offered sizes are accepted', () => {
  assert.deepEqual(TEXT_SIZES, ['100', '125', '150', '200']);
  assert.equal(textSizeChoice('150'), '150');
  assert.equal(textSizeChoice(200), '200');
  for (const bad of [null, undefined, '', '175', '1000', 'large', '200%']) assert.equal(textSizeChoice(bad), '100');
});

test('mounting restores the saved size, sits beside Theme, and saves a change', () => {
  const store = { 'agiw-text-size': '150' };
  const window = { localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } } };
  const { document, placed, created } = fakeDom();
  const label = mountTextSize({ window, document });
  assert.equal(placed[0], label);
  assert.equal(document.documentElement.dataset.textSize, '150');
  const select = created.find(n => n.tag === 'select');
  assert.equal(select.id, 'jaryoTextSize');
  assert.equal(select.attrs['aria-label'], 'Text size');
  assert.deepEqual(select.children.map(o => o.textContent), ['100%', '125%', '150%', '200%']);
  assert.equal(select.value, '150');
  select.value = '200';
  select.listeners.change[0]();
  assert.equal(document.documentElement.dataset.textSize, '200');
  assert.equal(store['agiw-text-size'], '200');
});

test('blocked storage still applies the size', () => {
  const window = { localStorage: { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } } };
  const { document, created } = fakeDom();
  mountTextSize({ window, document });
  assert.equal(document.documentElement.dataset.textSize, '100');
  const select = created.find(n => n.tag === 'select');
  select.value = '125';
  select.listeners.change[0]();
  assert.equal(document.documentElement.dataset.textSize, '125');
});

test('without a Theme control nothing is mounted', () => {
  const { document } = fakeDom();
  document.getElementById = () => null;
  assert.equal(mountTextSize({ window: {}, document }), null);
});
