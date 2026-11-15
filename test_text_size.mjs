import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

// Text size belongs to the shared web/jaryo.mjs adapter, which every page loads, so the control
// does not depend on any one page's markup. These tests run the shipped module source in a DOM double.
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const strip = source => source.replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
const EXPORTS = '({PREFERENCE_KEYS, PREFERENCE_SEED, PREFERENCE_EVENT, TEXT_SIZES, textSizeChoice, readPreference, mountTextSize, applyTextSize, currentPreferences})';

function fakeWindow({ store = {}, seed, blocked = false } = {}) {
  const events = [];
  const window = {
    localStorage: {
      getItem(key) { if (blocked) throw new Error('blocked'); return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
      setItem(key, value) { if (blocked) throw new Error('blocked'); store[key] = String(value); },
    },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    dispatchEvent(event) { events.push(event); return true; },
    CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
  };
  if (seed !== undefined) window.agiwPreferenceSeed = seed;
  return { window, store, events };
}

function fakeDom({ theme = true } = {}) {
  const created = [];
  const placed = [];
  const listeners = {};
  const make = tag => {
    const node = { tag, children: [], attrs: {}, listeners: {}, className: '', id: '', value: '', textContent: '',
      append(...n) { for (const child of n) if (child && typeof child === 'object') child.parent = this; this.children.push(...n); },
      setAttribute(k, v) { this.attrs[k] = v; },
      addEventListener(t, f) { (this.listeners[t] ||= []).push(f); },
      closest(selector) {
        for (let at = this; at; at = at.parent) if (selector.startsWith('.') && ` ${at.className} `.includes(` ${selector.slice(1)} `)) return at;
        return null;
      } };
    created.push(node);
    return node;
  };
  const themeLabel = { after(node) { placed.push(node); } };
  const themeSelect = make('select');
  themeSelect.id = 'jaryoTheme';
  themeSelect.closest = selector => (selector === '.j-theme' ? themeLabel : null);
  const document = {
    documentElement: { dataset: {} },
    createElement: make,
    createTextNode: text => ({ text }),
    getElementById: id => (id === 'jaryoTheme' && theme ? themeSelect : placed.flatMap(n => n.children).find(n => n.id === id) || null),
    querySelector: selector => (selector === '#jaryoTheme' && theme ? themeSelect : null),
    querySelectorAll: () => [],
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  };
  return { document, placed, created, themeSelect, listeners };
}

/** Evaluates the shipped module the way a page does: top-level code runs once, synchronously. */
function loadPage({ window, document }) {
  const source = strip(read('./jaryo/theme.mjs')) + '\n' + strip(read('./web/jaryo.mjs')) + '\n' + EXPORTS;
  return runInNewContext(source, { window, document, localStorage: window.localStorage });
}

const api = loadPage({ window: fakeWindow().window, document: { documentElement: { dataset: {} } } });

test('only the offered sizes are accepted', () => {
  assert.deepEqual([...api.TEXT_SIZES], ['100', '125', '150', '200']);
  assert.equal(api.textSizeChoice('150'), '150');
  assert.equal(api.textSizeChoice(200), '200');
  for (const bad of [null, undefined, '', '175', '1000', 'large', '200%']) assert.equal(api.textSizeChoice(bad), '100');
});

test('one documented key set, seed name and change message', () => {
  assert.deepEqual({ ...api.PREFERENCE_KEYS }, { theme: 'agiw-theme', textSize: 'agiw-text-size' });
  assert.equal(api.PREFERENCE_SEED, 'agiwPreferenceSeed');
  assert.equal(api.PREFERENCE_EVENT, 'agiw:preferences');
  assert.ok(Object.isFrozen(api.PREFERENCE_KEYS));
  const doc = read('./docs/monitor-preferences.md');
  for (const name of ['agiw-theme', 'agiw-text-size', 'agiwPreferenceSeed', 'agiw:preferences']) assert.ok(doc.includes(name), name);
});

test('mounting restores the saved size, sits beside Theme, saves a change and publishes it', () => {
  const { window, store, events } = fakeWindow({ store: { 'agiw-text-size': '150' } });
  const { document, placed, created } = fakeDom();
  const label = api.mountTextSize({ window, document });
  assert.equal(placed[0], label);
  assert.equal(document.documentElement.dataset.textSize, '150');
  const select = created.find(n => n.tag === 'select' && n.id === 'jaryoTextSize');
  assert.equal(select.attrs['aria-label'], 'Text size');
  assert.deepEqual(select.children.map(o => o.textContent), ['100%', '125%', '150%', '200%']);
  assert.equal(select.value, '150');
  assert.equal(events.length, 0, 'restoring is not a change');
  select.value = '200';
  select.listeners.change[0]();
  assert.equal(document.documentElement.dataset.textSize, '200');
  assert.equal(store['agiw-text-size'], '200');
  assert.equal(events.length, 1);
  assert.equal(events[0].type, 'agiw:preferences');
  assert.deepEqual({ ...events[0].detail }, { 'agiw-theme': 'system', 'agiw-text-size': '200' });
});

test('mounting twice keeps one control', () => {
  const { window } = fakeWindow();
  const { document, placed } = fakeDom();
  const first = api.mountTextSize({ window, document });
  assert.equal(api.mountTextSize({ window, document }), first);
  assert.equal(placed.length, 1);
});

test('blocked storage still applies the size and still publishes the change', () => {
  const { window, events } = fakeWindow({ blocked: true });
  const { document, created } = fakeDom();
  api.mountTextSize({ window, document });
  assert.equal(document.documentElement.dataset.textSize, '100');
  const select = created.find(n => n.id === 'jaryoTextSize');
  select.value = '125';
  select.listeners.change[0]();
  assert.equal(document.documentElement.dataset.textSize, '125');
  assert.equal(events.at(-1).detail['agiw-text-size'], '125');
});

test('without a Theme control nothing is mounted', () => {
  const { document } = fakeDom({ theme: false });
  assert.equal(api.mountTextSize({ window: fakeWindow().window, document }), null);
});

test('a native seed supplies the size when this page has none saved; a saved value wins', () => {
  assert.equal(api.readPreference(fakeWindow({ seed: { 'agiw-text-size': '150' } }).window, 'agiw-text-size'), '150');
  assert.equal(api.readPreference(fakeWindow({ store: { 'agiw-text-size': '125' }, seed: { 'agiw-text-size': '150' } }).window, 'agiw-text-size'), '125');
  assert.equal(api.readPreference(fakeWindow({ blocked: true, seed: { 'agiw-text-size': '200' } }).window, 'agiw-text-size'), '200');
  // Invalid entries are skipped, not trusted: storage falls through to the seed, a bad seed to nothing.
  assert.equal(api.readPreference(fakeWindow({ store: { 'agiw-text-size': '175' }, seed: { 'agiw-text-size': '150' } }).window, 'agiw-text-size'), '150');
  for (const seed of [{ 'agiw-text-size': '175' }, { 'agiw-text-size': 150 }, 'agiw-text-size=150', null, []]) {
    assert.equal(api.readPreference(fakeWindow({ seed }).window, 'agiw-text-size'), null, JSON.stringify(seed));
  }
  const inherited = Object.create({ 'agiw-text-size': '200' });
  assert.equal(api.readPreference(fakeWindow({ seed: inherited }).window, 'agiw-text-size'), null, 'only own seed entries count');
  const hostile = fakeWindow().window;
  Object.defineProperty(hostile, 'agiwPreferenceSeed', { get() { throw new Error('denied'); } });
  assert.equal(api.readPreference(hostile, 'agiw-text-size'), null);
  assert.equal(api.readPreference(fakeWindow({ seed: { other: '150' } }).window, 'other'), null, 'only the documented keys are read');
});

test('loading the shipped module applies the saved size synchronously and mounts the control beside Theme', () => {
  const { window } = fakeWindow({ seed: { 'agiw-text-size': '200' } });
  const { document, placed, listeners } = fakeDom();
  loadPage({ window, document });
  // Applied while the module evaluates, without waiting for DOMContentLoaded or any later event.
  assert.equal(document.documentElement.dataset.textSize, '200');
  assert.equal(listeners.DOMContentLoaded, undefined);
  assert.equal(placed.length, 1);
  assert.equal(placed[0].className, 'j-theme j-text-size');
  assert.equal(placed[0].children[1].value, '200');
});

test('restoring at load saves nothing, so a later seed still applies until a person changes the size', () => {
  const { window, store } = fakeWindow({ seed: { 'agiw-text-size': '150' } });
  const { document } = fakeDom();
  loadPage({ window, document });
  assert.equal(document.documentElement.dataset.textSize, '150');
  assert.equal(Object.hasOwn(store, 'agiw-text-size'), false);
  assert.equal(Object.hasOwn(store, 'agiw-theme'), false, 'the theme is not saved at load either');
});

test('a page without a Theme control still gets the saved size applied', () => {
  const { window } = fakeWindow({ store: { 'agiw-text-size': '125' } });
  const { document, placed } = fakeDom({ theme: false });
  loadPage({ window, document });
  assert.equal(document.documentElement.dataset.textSize, '125');
  assert.equal(placed.length, 0);
});

const pages = readdirSync(new URL('./web/', import.meta.url)).filter(name => name.endsWith('.html'));

test('every page that loads jaryo.mjs has the Theme control the size mounts beside, and the zoom rules it needs', () => {
  const jaryoPages = pages.filter(name => read(`./web/${name}`).includes('src="/jaryo.mjs"'));
  assert.ok(jaryoPages.length >= 7, jaryoPages.join(','));
  for (const name of jaryoPages) {
    const html = read(`./web/${name}`);
    assert.match(html, /<label class="j-theme">Theme\s*<select id="jaryoTheme"/, `${name} Theme control`);
    assert.match(html, /href="\/jaryo\.css"/, `${name} loads jaryo.css`);
    assert.doesNotMatch(html, /jaryoTextSize/, `${name} needs no size markup`);
  }
  const css = read('./web/jaryo.css');
  for (const [size, zoom] of [['125', '1.25'], ['150', '1.5'], ['200', '2']]) {
    assert.ok(css.includes(`html[data-text-size="${size}"] body{zoom:${zoom}}`), `zoom ${size}`);
  }
});

test('simple-view.mjs no longer owns a second text-size control or key', () => {
  const simple = read('./web/simple-view.mjs');
  assert.doesNotMatch(simple, /agiw-text-size|mountTextSize|jaryoTextSize/);
});
