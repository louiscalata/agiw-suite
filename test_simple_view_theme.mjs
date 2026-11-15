import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const css = readFileSync(new URL('./web/simple-view.css', import.meta.url), 'utf8');
const primitives = readFileSync(new URL('./jaryo/primitives.css', import.meta.url), 'utf8');
const simple = css.slice(0, css.indexOf('.j-bus{'));

test('the simple view does not choose its own theme from the operating system', () => {
  // An explicit Light or Dark in the Theme control must reach it; prefers-color-scheme alone ignored that.
  assert.doesNotMatch(simple, /prefers-color-scheme/);
});

test('its text, background, rules and button come from the Jaryo tokens', () => {
  assert.match(simple, /#svBody\{[^}]*color:var\(--j-text,#000\);background:var\(--j-bg,#fff\)/);
  assert.match(simple, /\.sv-row\{[^}]*border-top:1px solid var\(--j-border,#000\)/);
  assert.match(simple, /\.sv-row\[data-tone="ok"\]\{border-top-color:var\(--j-border,#000\)\}/);
  assert.match(simple, /#svBody button\{[^}]*color:var\(--j-invert-ink,#fff\);background:var\(--j-invert,#000\)/);
});

test('every token it uses exists, and the primitives resolve it for System, Light and Dark', () => {
  for (const token of ['--j-text', '--j-bg', '--j-border', '--j-invert', '--j-invert-ink']) {
    assert.ok(simple.includes(`var(${token},`), token);
    assert.match(primitives, new RegExp(`${token}:`), `${token} defined`);
  }
  assert.match(primitives, /html\[data-theme="light"\]/);
  assert.match(primitives, /html\[data-theme="dark"\]/);
  assert.match(primitives, /@media \(prefers-color-scheme: light\)\s*\{\s*html:not\(\[data-theme="dark"\]\)/);
});

test('text zoom lives in jaryo.css, which every page loads; the transitions stay here', () => {
  const jaryo = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
  assert.match(jaryo, /html\[data-text-size="200"\] body\{zoom:2\}/);
  assert.doesNotMatch(css, /body\{zoom:/, 'one owner for the zoom rules');
  assert.match(css, /@keyframes j-zoom-in/);
});

// The Theme choice follows the same preference contract as Text size (docs/monitor-preferences.md).
const strip = source => source.replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
function loadTheme({ store = {}, seed, light = false } = {}) {
  const events = [];
  const window = {
    localStorage: { getItem: k => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
    matchMedia: () => ({ matches: light, addEventListener() {} }),
    dispatchEvent: event => { events.push(event); return true; },
    CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
  };
  if (seed !== undefined) window.agiwPreferenceSeed = seed;
  const listeners = {};
  const select = { value: '', addEventListener: (type, fn) => { listeners[type] = fn; }, closest: () => null };
  const document = { documentElement: { dataset: {} }, querySelector: q => (q === '#jaryoTheme' ? select : null), querySelectorAll: () => [], getElementById: () => null };
  const source = strip(readFileSync(new URL('./jaryo/theme.mjs', import.meta.url), 'utf8')) + '\n'
    + strip(readFileSync(new URL('./web/jaryo.mjs', import.meta.url), 'utf8'));
  runInNewContext(source, { window, document, localStorage: window.localStorage });
  return { document, select, listeners, events, store };
}

test('a native seed supplies the theme when this page has none saved; a saved theme wins', () => {
  assert.equal(loadTheme({ seed: { 'agiw-theme': 'light' } }).document.documentElement.dataset.themeChoice, 'light');
  assert.equal(loadTheme({ store: { 'agiw-theme': 'dark' }, seed: { 'agiw-theme': 'light' } }).document.documentElement.dataset.themeChoice, 'dark');
  assert.equal(loadTheme({ seed: { 'agiw-theme': 'sepia' } }).document.documentElement.dataset.themeChoice, 'system');
  assert.equal(loadTheme({ store: { 'agiw-theme': 'sepia' }, seed: { 'agiw-theme': 'dark' } }).document.documentElement.dataset.themeChoice, 'dark');
});

test('changing Theme saves it and publishes both preferences in one message; restoring publishes nothing', () => {
  const page = loadTheme({ store: { 'agiw-text-size': '150' } });
  assert.equal(page.events.length, 0);
  page.select.value = 'dark';
  page.listeners.change();
  assert.equal(page.store['agiw-theme'], 'dark');
  assert.equal(page.document.documentElement.dataset.theme, 'dark');
  assert.equal(page.events.length, 1);
  assert.equal(page.events[0].type, 'agiw:preferences');
  assert.deepEqual({ ...page.events[0].detail }, { 'agiw-theme': 'dark', 'agiw-text-size': '150' });
});
