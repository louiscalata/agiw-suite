// The App Store edition does not ship web/jaryo.mjs or web/jaryo.css, so it carries its own copy of the
// Text size control, the zoom rules and the preference contract (docs/monitor-preferences.md).
// These checks need no upstream checkout: they run the edition runtime text in a DOM double.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import { denylistHits } from './engine.mjs';
import { loadDenylist } from './build.mjs';
import * as rules from './appstore-rules.mjs';
import { adaptEdition, PREFERENCE_RUNTIME } from './jaryo-adapt.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const web = join(here, '..', '..', 'web');

function fakeWindow({ store = {}, seed, blocked = false, light = false } = {}) {
  const events = [];
  const window = {
    localStorage: {
      getItem(key) { if (blocked) throw new Error('blocked'); return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
      setItem(key, value) { if (blocked) throw new Error('blocked'); store[key] = String(value); },
    },
    matchMedia: () => ({ matches: light, addEventListener() {} }),
    dispatchEvent(event) { events.push(event); return true; },
    CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
  };
  if (seed !== undefined) window.agiwPreferenceSeed = seed;
  return { window, store, events };
}

function fakeDom({ theme = true } = {}) {
  const placed = [];
  const make = tag => ({
    tag, children: [], attrs: {}, listeners: {}, className: '', id: '', value: '', textContent: '',
    append(...n) { for (const child of n) if (child && typeof child === 'object') child.parent = this; this.children.push(...n); },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(t, f) { (this.listeners[t] ||= []).push(f); },
  });
  const themeLabel = { className: 'j-theme', after(node) { placed.push(node); } };
  const themeSelect = make('select');
  themeSelect.id = 'jaryoTheme';
  themeSelect.closest = selector => (selector === '.j-theme' ? themeLabel : null);
  const document = {
    documentElement: { dataset: {} },
    createElement: make,
    createTextNode: text => ({ text }),
    getElementById: id => (id === 'jaryoTheme' && theme ? themeSelect : placed.flatMap(n => n.children).find(n => n.id === id) || null),
    querySelector: selector => (selector === '#jaryoTheme' && theme ? themeSelect : null),
  };
  return { document, placed, themeSelect };
}

function run({ window, document }) {
  runInNewContext(`${PREFERENCE_RUNTIME}\n;({editionPreference, editionTextSize, mountEditionTextSize})`, { window, document, localStorage: window.localStorage });
  return { size: () => document.documentElement.dataset.textSize, select: () => document.getElementById('jaryoTextSize') };
}

test('the edition runtime restores a seeded size, mounts Text size beside Theme and saves nothing at load', () => {
  const { window, store, events } = fakeWindow({ seed: { 'agiw-text-size': '150' } });
  const { document, placed } = fakeDom();
  const page = run({ window, document });
  assert.equal(page.size(), '150');
  assert.equal(placed.length, 1);
  assert.equal(placed[0].className, 'j-theme j-text-size');
  assert.equal(page.select().attrs['aria-label'], 'Text size');
  assert.deepEqual(page.select().children.map(o => o.value), ['100', '125', '150', '200']);
  assert.equal(page.select().value, '150');
  assert.equal(events.length, 0, 'restoring is not a change');
  assert.equal(Object.hasOwn(store, 'agiw-text-size'), false, 'restoring does not save');
});

test('a saved size wins over the seed; invalid values fall through', () => {
  const runSize = options => { const { window } = fakeWindow(options); const { document } = fakeDom(); return run({ window, document }).size(); };
  assert.equal(runSize({ store: { 'agiw-text-size': '125' }, seed: { 'agiw-text-size': '200' } }), '125');
  assert.equal(runSize({ store: { 'agiw-text-size': '175' }, seed: { 'agiw-text-size': '200' } }), '200');
  assert.equal(runSize({ seed: { 'agiw-text-size': 200 } }), '100');
  assert.equal(runSize({ seed: Object.create({ 'agiw-text-size': '200' }) }), '100');
  assert.equal(runSize({ blocked: true, seed: { 'agiw-text-size': '200' } }), '200');
});

test('changing Text size saves it and publishes both preferences in one message', () => {
  const { window, store, events } = fakeWindow({ store: { 'agiw-theme': 'dark' } });
  const { document } = fakeDom();
  document.documentElement.dataset.themeChoice = 'dark';
  const page = run({ window, document });
  page.select().value = '200';
  page.select().listeners.change[0]();
  assert.equal(page.size(), '200');
  assert.equal(store['agiw-text-size'], '200');
  assert.equal(events.length, 1);
  assert.equal(events[0].type, 'agiw:preferences');
  assert.deepEqual({ ...events[0].detail }, { 'agiw-theme': 'dark', 'agiw-text-size': '200' });
});

test('without the Theme control the size still applies and nothing is mounted', () => {
  const { window } = fakeWindow({ store: { 'agiw-text-size': '125' } });
  const { document, placed } = fakeDom({ theme: false });
  assert.equal(run({ window, document }).size(), '125');
  assert.equal(placed.length, 0);
});

test('the edition theme reads the seed, saves only a change, and publishes it', () => {
  const seeded = fakeWindow({ seed: { 'agiw-theme': 'light' } });
  const dom = fakeDom();
  run({ window: seeded.window, document: dom.document });
  assert.equal(dom.document.documentElement.dataset.themeChoice, 'light');
  assert.equal(dom.document.documentElement.dataset.theme, 'light');
  assert.equal(dom.themeSelect.value, 'light');
  assert.equal(Object.hasOwn(seeded.store, 'agiw-theme'), false, 'restoring does not save');
  assert.equal(seeded.events.length, 0);
  dom.themeSelect.value = 'dark';
  dom.themeSelect.listeners.change[0]();
  assert.equal(dom.document.documentElement.dataset.theme, 'dark');
  assert.equal(seeded.store['agiw-theme'], 'dark');
  assert.equal(seeded.events.length, 1);
  assert.deepEqual({ ...seeded.events[0].detail }, { 'agiw-theme': 'dark', 'agiw-text-size': '100' });
  const saved = fakeWindow({ store: { 'agiw-theme': 'dark' }, seed: { 'agiw-theme': 'light' } });
  const other = fakeDom();
  run({ window: saved.window, document: other.document });
  assert.equal(other.document.documentElement.dataset.themeChoice, 'dark', 'a saved theme wins');
});

test('adaptEdition appends the preference runtime to app.js and the zoom rules to style.css, once', () => {
  const upstream = new Map(['index.html', 'app.js', 'map-layout.mjs', 'model-control-view.mjs', 'usage-format.mjs']
    .filter(name => existsSync(join(web, name))).map(name => [name, readFileSync(join(web, name), 'utf8')]));
  adaptEdition(rules, upstream);
  adaptEdition(rules, upstream);
  const appended = rules.js['app.js'].append.filter(text => text.includes('function mountEditionTextSize'));
  assert.equal(appended.length, 1);
  const style = rules.css['style.css'].append;
  for (const [size, zoom] of [['125', '1.25'], ['150', '1.5'], ['200', '2']]) {
    assert.equal(style.split(`html[data-text-size="${size}"] body{zoom:${zoom}}`).length - 1, 1, `zoom ${size}`);
  }
  // simple-view.css, which the edition ships, keeps the overview layouts for 150 and 200 percent.
  assert.match(readFileSync(join(web, 'simple-view.css'), 'utf8'), /html:is\(\[data-text-size="150"\],\[data-text-size="200"\]\) body\{/);
  assert.deepEqual(rules.ship.filter(name => name.startsWith('jaryo')), [], 'the edition still does not ship jaryo.mjs or jaryo.css');
});

test('the preference runtime is denylist-clean', () => {
  assert.deepEqual(denylistHits(PREFERENCE_RUNTIME, loadDenylist()), []);
});
