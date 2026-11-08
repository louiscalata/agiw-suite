import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const source = read('./web/simple-view.mjs').replace("'/model-control-view.mjs'", JSON.stringify(new URL('./web/model-control-view.mjs', import.meta.url).href));
const { zoomSwap, mountSimpleView } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const classList = () => { const set = new Set(); return { set, add: c => set.add(c), remove: c => set.delete(c), contains: c => set.has(c) }; };
const rig = ({ reduced = false, start = true, throwing = false, finished = 'resolve' } = {}) => {
  const log = [];
  const document = { documentElement: { classList: classList() } };
  if (start) {
    document.startViewTransition = update => {
      log.push('start');
      if (throwing) throw new Error('InvalidStateError');
      let release; const done = new Promise((resolve, reject) => { release = finished === 'reject' ? reject : resolve; });
      queueMicrotask(() => { update(); release(); });
      return { finished: done };
    };
  }
  const window = { matchMedia: query => ({ matches: reduced && query.includes('reduce') }) };
  return { document, window, log, change: () => log.push('change') };
};

test('motion allowed: the change runs once inside a transition and the class is removed after', async () => {
  const r = rig();
  assert.equal(zoomSwap(r, r.change), true);
  assert.ok(r.document.documentElement.classList.contains('j-zooming'));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(r.log, ['start', 'change']);
  assert.ok(!r.document.documentElement.classList.contains('j-zooming'));
});

test('reduced motion or no View Transitions: the change runs at once, plainly, never marked', () => {
  for (const options of [{ reduced: true }, { start: false }]) {
    const r = rig(options);
    assert.equal(zoomSwap(r, r.change), false);
    assert.deepEqual(r.log, ['change']);
    assert.ok(!r.document.documentElement.classList.contains('j-zooming'));
  }
  const noClassList = { document: { startViewTransition() { throw new Error('should not start'); }, documentElement: {} }, window: {} };
  let ran = 0;
  assert.equal(zoomSwap(noClassList, () => { ran += 1; }), false);
  assert.equal(ran, 1);
});

test('a transition that throws still changes the face exactly once and leaves no class', () => {
  const r = rig({ throwing: true });
  assert.equal(zoomSwap(r, r.change), false);
  assert.deepEqual(r.log, ['start', 'change']);
  assert.ok(!r.document.documentElement.classList.contains('j-zooming'));
});

test('a transition whose finished promise rejects still clears the class', async () => {
  const r = rig({ finished: 'reject' });
  zoomSwap(r, r.change);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.ok(!r.document.documentElement.classList.contains('j-zooming'));
});

test('the Overview/Simple switch animates, a second click before the swap flips back, and mount does not animate', async () => {
  const nodes = [];
  const node = tag => {
    const n = { tag, id: '', attrs: {}, children: [], listeners: {}, dataset: {}, _text: '',
      setAttribute(k, v) { this.attrs[k] = v; if (k === 'id') this.id = v; }, getAttribute(k) { return this.attrs[k]; },
      append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; },
      addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }, closest() { return null; }, remove() {}, after() {} };
    Object.defineProperty(n, 'textContent', { get() { return n._text; }, set(v) { n._text = v; } });
    nodes.push(n); return n;
  };
  const r = rig();
  const document = { ...r.document, documentElement: { dataset: {}, classList: classList() }, body: { append() {} },
    createElement: node, addEventListener() {}, removeEventListener() {}, getElementById: id => nodes.find(n => n.id === id) || null };
  document.startViewTransition = r.document.startViewTransition;
  const sw = node('button'); sw.id = 'advancedSwitch';
  const posts = [];
  const window = { ...r.window, addEventListener() {}, agiwPrefs: { view: 'advanced' }, webkit: { messageHandlers: { monitor: { postMessage: m => posts.push(m.action) } } } };
  const view = mountSimpleView({ window, document, fetch: async () => ({ ok: false }), setInterval: () => 0, clearInterval() {}, now: () => 0 });
  assert.equal(document.documentElement.dataset.view, 'advanced');
  assert.deepEqual(r.log, [], 'the first paint is not a transition');
  sw.listeners.click[0]({ target: sw, currentTarget: sw });
  assert.deepEqual(r.log, ['start'], 'the click starts a transition');
  sw.listeners.click[0]({ target: sw, currentTarget: sw });   // before the swap lands: asks to go back
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(document.documentElement.dataset.view, 'advanced');
  assert.deepEqual(posts, [], 'there and back again is no change, so the app is told nothing');
  assert.ok(!document.documentElement.classList.contains('j-zooming'));
  sw.listeners.click[0]({ target: sw, currentTarget: sw });   // a click after the page has settled
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(document.documentElement.dataset.view, 'simple');
  sw.listeners.click[0]({ target: sw, currentTarget: sw });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(document.documentElement.dataset.view, 'advanced');
  assert.deepEqual(posts, ['view-simple', 'view-advanced']);
  view.destroy();
});

// The text of the first `@media (prefers-reduced-motion: no-preference)` block after the keyframes, braces balanced.
function motionBlock(css) {
  const from = css.indexOf('@keyframes j-zoom-in');
  const start = css.indexOf('@media (prefers-reduced-motion', from);
  const open = css.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === '{') depth += 1;
    if (css[i] === '}' && --depth === 0) return css.slice(start, i + 1);
  }
  throw new Error('unbalanced');
}

test('the CSS moves only transform, honours reduced motion, and keeps colour out of the transition', () => {
  const simple = read('./web/simple-view.css');
  const full = read('./web/jaryo.css');
  for (const [name, css] of [['simple-view.css', simple], ['jaryo.css', full]]) {
    assert.match(css, /@keyframes j-zoom-in\s*\{\s*from\s*\{\s*transform:\s*scale\(0?\.96\)\s*;?\s*\}\s*to\s*\{\s*transform:\s*none;?\s*\}/, name);
    const motion = motionBlock(css);
    assert.match(motion, /^@media \(prefers-reduced-motion:\s*no-preference\)/, name);
    assert.doesNotMatch(motion, /opacity|color|background|filter|blur|plus-lighter/, name);
    assert.match(motion, /mix-blend-mode:\s*normal/, name);
  }
  // Page-to-page zoom is opt-in per page through jaryo.css, only inside the no-preference query,
  // and only the four reading pages carry the article name (the map pages keep their own layout).
  const motion = motionBlock(full);
  assert.match(motion, /@view-transition \{ navigation: auto; \}/, '@view-transition sits inside the no-preference query');
  assert.equal([...full.matchAll(/@view-transition/g)].length, 1);
  assert.equal([...full.matchAll(/view-transition-name:/g)].length, 1);
  assert.match(motion, /body\.jaryo-app main\.j-page \{ view-transition-name: j-article; \}/);
  assert.doesNotMatch(read('./web/index.html') + read('./web/monitor.html'), /view-transition/);
});

test('every page that holds an article loads jaryo.css, so both sides of a page change opt in', () => {
  for (const page of ['components', 'identity', 'machines', 'setup', 'index', 'monitor']) {
    assert.match(read(`./web/${page}.html`), /<link rel="stylesheet" href="\/jaryo\.css">/, page);
  }
});

test('a transition that never calls back (no frames) still changes the face once after the limit', async () => {
  const r = rig();
  r.document.startViewTransition = () => { r.log.push('start'); return { finished: new Promise(() => {}) }; };
  const timers = [];
  r.window.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  r.window.clearTimeout = id => { timers[id - 1].cleared = true; };
  assert.equal(zoomSwap(r, r.change), true);
  assert.deepEqual(r.log, ['start'], 'nothing has changed yet');
  assert.equal(timers.length, 1);
  assert.ok(timers[0].ms >= 200 && timers[0].ms <= 1000, 'a short limit');
  timers[0].fn();
  timers[0].fn();
  assert.deepEqual(r.log, ['start', 'change'], 'once, however many times it fires');
});

test('the timer is cleared when the transition does call back in time', async () => {
  const r = rig();
  const timers = [];
  r.window.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  r.window.clearTimeout = id => { timers[id - 1].cleared = true; };
  zoomSwap(r, r.change);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(r.log, ['start', 'change']);
  assert.equal(timers[0].cleared, true);
});
