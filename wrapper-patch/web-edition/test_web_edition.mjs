// Tests for the edition transform and for the App Store web UI as bundled.
//
//   node --test scripts/web-edition/test_*.mjs
//
// The bundle tests read AGIW_WEB_DIR (default App/Resources/web, filled by scripts/sync-web.sh) and run the
// real app.js in a small DOM built from the real index.html, against a sample-data snapshot and a live one,
// with a fetch that answers like the app's WebResponder (unknown routes are 404). They are skipped when the
// folder has not been synced. The in-WebKit equivalent is the app's --selftest (scripts/selftest.sh).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createContext, runInContext } from 'node:vm';
import {
  EditionError, splitJs, transformJs, transformHtml, pruneCss, pruneImports, danglingReferences, missingIds, sha256,
  stripCommentLines, denylistHits,
} from './engine.mjs';
import { buildEdition, loadDenylist } from './build.mjs';
import { ship } from './appstore-rules.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const webDir = process.env.AGIW_WEB_DIR || join(root, 'App', 'Resources', 'web');
const fixture = name => join(root, 'Packages', 'AgiwCore', 'Tests', 'AgiwCoreTests', 'Fixtures', 'DemoGolden', name);
const goldenReady = existsSync(fixture('demo-idle.json'));
const bundled = goldenReady && ship.every(name => existsSync(join(webDir, name)));
const PRIVATE_IDS = ['onlineCodeMode', 'fixInference', 'pcSwitch', 'vitalPc', 'vitalRoute', 'vitalActivity',
  'onlineCodeDetails', 'fixInferenceDetails', 'activityDetails', 'pcHeadless', 'drawerJump',
  'autoUnloadToggle', 'modelScopeControls', 'modelScopeNote', 'memoryBanner', 'memoryAnnounce',
  'fixScopeNote', 'drawerFixButton'];

// ---------------------------------------------------------------------------------------------------------
// Engine

test('splitJs reproduces the source and keys statements by what they declare', () => {
  const source = [
    "import {a,b} from '/m.mjs';",
    '// lead comment',
    'const x=1,y=`t${a({})}`;',
    'function f(){',
    "  return /[}]/.test('}');",
    '}',
    '/* block',
    '   comment */',
    "$('go').addEventListener('click',()=>{",
    '  f();',
    '});',
    '',
  ].join('\n');
  const { statements } = splitJs(source);
  assert.deepEqual(statements.map(s => s.key), ['import:/m.mjs', 'const:x', 'function:f', 'on:go:click']);
  assert.deepEqual(statements[1].names, ['x', 'y']);
  assert.deepEqual(statements[3].lead, ['/* block', '   comment */']);
});

test('transformJs refuses drift: a missing statement, a wrong anchor count, a changed pinned statement', () => {
  const source = 'function keep(){return 1;}\nfunction old(){return 2;}\n';
  const problems = [];
  transformJs(source, {
    drop: ['function:gone'],
    edit: { 'function:keep': [['return 9;', 'return 0;']] },
    replace: { 'function:old': { sha256: sha256('function other(){}'), text: 'function old(){}' } },
  }, 'm.js', problems);
  assert.equal(problems.length, 3);
  assert.match(problems[0], /no top-level statement function:gone/);
  assert.match(problems[1], /changed upstream/);
  assert.match(problems[2], /expected 1 match/);
});

test('transformJs drops, replaces (sha-pinned), edits, drops line ranges and appends', () => {
  const source = 'function a(){\n  one();\n  two();\n  three();\n}\nfunction b(){return 1;}\nconst c=2;\n';
  const problems = [];
  const { text, dropped } = transformJs(source, {
    drop: ['const:c'],
    replace: { 'function:b': { sha256: sha256('function b(){return 1;}'), text: 'function b(){return 2;}' } },
    dropLines: { 'function:a': [{ from: '  two();', through: '  three();' }] },
    edit: { 'function:a': [['one()', 'uno()']] },
    append: ['a();'],
  }, 'm.js', problems);
  assert.deepEqual(problems, []);
  assert.equal(text, 'function a(){\n  uno();\n}\nfunction b(){return 2;}\na();\n');
  assert.deepEqual(dropped, ['c']);
});

test('dangling references and unexported imports are reported', () => {
  assert.equal(danglingReferences('function a(){return gone();}\n', ['gone'], 'm.js').length, 1);
  assert.equal(danglingReferences("const o={gone:1};const s='gone';\n", ['gone'], 'm.js').length, 0);
  const problems = [];
  const modules = new Map([['m.mjs', 'export function kept(){}\n']]);
  const { text } = pruneImports("import {kept,unused,missing} from '/m.mjs';\nkept();missing();\n", modules, 'app.js', problems);
  assert.match(text, /^import \{kept\} from '\/m\.mjs';/);
  assert.deepEqual(problems, ['app.js: uses missing from m.mjs, which the edition no longer exports']);
});

test('transformHtml removes whole elements by id and start tag, and refuses a missing id', () => {
  const html = '<div id="keep"><div id="gone"><div>x</div></div><p class="c">y</p></div>';
  const problems = [];
  assert.equal(transformHtml(html, { removeIds: ['gone'], removeElements: ['<p class="c">'] }, 'i.html', problems), '<div id="keep"></div>');
  transformHtml(html, { removeIds: ['absent'] }, 'i.html', problems);
  assert.equal(problems.length, 1);
  assert.deepEqual(missingIds("$('keep');$('gone');", '<div id="keep"></div><div data-id="gone"></div>'), ['gone']);
});

test('pruneCss drops matching selectors, empty rules and empty media blocks', () => {
  const css = '/* c */.a,.gone{x:1}.gone{y:2}@media(max-width:9px){.gone{z:3}}@media(max-width:8px){.a{z:4}}';
  const { text } = pruneCss(css, { patterns: [/\.gone/] });
  assert.equal(text, '.a{x:1}@media(max-width:8px){.a{z:4}}');
});

test('stripCommentLines keeps code, strings and regexes that look like comments', () => {
  const source = "// gone\nconst a='// kept';\n/*\n gone\n*/\nconst r=/\\/\\//;\n";
  assert.equal(stripCommentLines(source), "const a='// kept';\nconst r=/\\/\\//;\n");
});

test('the feature denylist matches private-edition terms and leaves ordinary words alone', () => {
  const denylist = loadDenylist();
  const hit = text => denylistHits(text, denylist).length > 0;
  for (const text of ['Initiate Online Code', "fetch('/api/inference/fix')", 'Windows PC', 'Nisi + Jev', 'PC LLM',
    'force-unmount', 'Cursor', 'client-model', 'traceGraph', 'jev route', 'AFM', 'Core 6',
    'auto-unload', 'memory-banner', 'model-scope', '.mode-v02-evidence', '.completion-evidence-title',
    '#fixScopeNote', '#drawerFixButton', '.view-tabs']) assert.ok(hit(text), text);
  for (const text of ['cursor:pointer', 'window.matchMedia', 'Jeview', 'clientWidth', 'e.clientX', 'LM Studio', 'Nisi Inference', 'Jev', 'NVIDIA PAIR']) {
    assert.ok(!hit(text), text);
  }
  assert.ok(EditionError);
});

// ---------------------------------------------------------------------------------------------------------
// The bundled App Store UI in a small DOM

const VOID = new Set(['meta', 'link', 'input', 'br', 'img', 'hr', 'wbr', 'source']);

class Node {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.tagName = tag.toUpperCase();
    this.nodeType = tag === '#text' ? 3 : 1;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.style = { setProperty(name,value) { this[name]=String(value); } };
    this.listeners = {};
    this.nodeValue = '';
    this.scrollTop = 0;
    const node = this;
    this.classList = {
      get set() { return new Set(String(node.className || '').split(/\s+/).filter(Boolean)); },
      add(...names) { const s = this.set; names.forEach(n => s.add(n)); node.className = [...s].join(' '); },
      remove(...names) { const s = this.set; names.forEach(n => s.delete(n)); node.className = [...s].join(' '); },
      contains(name) { return this.set.has(name); },
      toggle(name, force) { const on = force === undefined ? !this.contains(name) : Boolean(force); if (on) this.add(name); else this.remove(name); return on; },
    };
  }
  get children() { return this.childNodes.filter(c => c.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastElementChild() { return this.children.at(-1) || null; }
  get id() { return this.attributes.id || ''; }
  set id(value) { this.attributes.id = value; }
  get className() { return this.attributes.class || ''; }
  set className(value) { this.attributes.class = String(value); }
  get hidden() { return 'hidden' in this.attributes; }
  set hidden(value) { if (value) this.attributes.hidden = ''; else delete this.attributes.hidden; }
  get textContent() { return this.nodeType === 3 ? this.nodeValue : this.childNodes.map(c => c.textContent).join(''); }
  set textContent(value) { this.childNodes = []; if (String(value) !== '') this.append(String(value)); }
  get clientWidth() { return 820; }
  get clientHeight() { return 604; }
  append(...nodes) {
    for (let node of nodes) {
      if (typeof node === 'string') { const t = new Node(this.ownerDocument, '#text'); t.nodeValue = node; node = t; }
      node.parentNode?.removeChild(node);
      node.parentNode = this;
      this.childNodes.push(node);
    }
  }
  removeChild(node) { this.childNodes = this.childNodes.filter(c => c !== node); node.parentNode = null; }
  remove() { this.parentNode?.removeChild(this); }
  replaceChildren(...nodes) { for (const c of this.childNodes) c.parentNode = null; this.childNodes = []; this.append(...nodes); }
  replaceWith(node) { const p = this.parentNode; const i = p.childNodes.indexOf(this); node.parentNode?.removeChild(node); p.childNodes[i] = node; node.parentNode = p; this.parentNode = null; }
  contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }
  setAttribute(name, value) { if (name === 'class') this.className = value; else if (name === 'hidden') this.hidden = true; else this.attributes[name] = String(value); if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  removeEventListener(type, listener) { this.listeners[type] = (this.listeners[type] || []).filter(l => l !== listener); }
  click() { this.dispatch('click'); }
  dispatch(type, event = {}) { for (const l of this.listeners[type] || []) l({ target: this, preventDefault() {}, key: '', button: 0, ...event }); }
  matches(selector) { return selector.split(',').some(s => matchCompound(this, s.trim())); }
  closest(selector) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (n.matches(selector)) return n; return null; }
  querySelectorAll(selector) { const out = []; const walk = n => n.children.forEach(c => { if (c.matches(selector)) out.push(c); walk(c); }); walk(this); return out; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.ownerDocument.activeElement = this; this.dispatch('focus'); }
  getBoundingClientRect() { return { left: 0, top: 0, right: 820, bottom: 604, width: 820, height: 604 }; }
  getBBox() { throw new Error('no layout'); }
}

function matchCompound(node, selector) {
  const m = selector.match(/^([a-z0-9]*)((?:[.#][\w-]+|\[[^\]]+\])*)$/i);
  if (!m) throw new Error(`fake DOM: unsupported selector ${selector}`);
  if (m[1] && node.tagName !== m[1].toUpperCase()) return false;
  for (const part of m[2].match(/[.#][\w-]+|\[[^\]]+\]/g) || []) {
    if (part[0] === '.' && !node.classList.contains(part.slice(1))) return false;
    if (part[0] === '#' && node.id !== part.slice(1)) return false;
    if (part[0] === '[') {
      const [, name, value] = part.match(/^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
      if (!(name in node.attributes)) return false;
      if (value !== undefined && node.attributes[name] !== value) return false;
    }
  }
  return true;
}

/** A document built from the real index.html (enough of HTML for this page: tags, attributes, text). */
function makeDocument(html) {
  const doc = { activeElement: null, listeners: {}, timeline: { currentTime: 0 } };
  const body = new Node(doc, 'body');
  const htmlNode = new Node(doc, 'html');
  const stack = [htmlNode];
  const bodyHtml = html.slice(html.indexOf('<body>') + 6, html.lastIndexOf('</body>')).replace(/<!--[\s\S]*?-->/g, '');
  const re = /<\/?([a-zA-Z0-9]+)([^>]*)>|([^<]+)/g;
  stack[0] = body;
  for (const m of bodyHtml.matchAll(re)) {
    const top = stack[stack.length - 1];
    if (m[3] !== undefined) { if (m[3].trim()) top.append(m[3].replace(/&amp;/g, '&')); continue; }
    if (m[0][1] === '/') { stack.pop(); continue; }
    const node = new Node(doc, m[1].toLowerCase());
    for (const a of m[2].matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) node.setAttribute(a[1], a[2] ?? '');
    top.append(node);
    if (!VOID.has(m[1].toLowerCase()) && !m[2].endsWith('/')) stack.push(node);
  }
  doc.body = body;
  doc.documentElement = htmlNode;
  doc.activeElement = body;
  doc.getElementById = id => body.querySelector(`#${id}`);
  doc.querySelectorAll = selector => body.querySelectorAll(selector);
  doc.querySelector = selector => body.querySelector(selector);
  doc.createElement = tag => new Node(doc, tag);
  doc.createElementNS = (_, tag) => new Node(doc, tag);
  doc.createTextNode = text => { const t = new Node(doc, '#text'); t.nodeValue = String(text); return t; };
  doc.getAnimations = () => [];
  doc.addEventListener = (type, l) => { (doc.listeners[type] ||= []).push(l); };
  return doc;
}

/** Runs the bundled app.js (its imports resolved from the bundle) and returns hooks into the page. */
async function bootBundledUI(snapshot, { simpleView = false, prefs, eventSource = false, freshPolling = false, deferSnapshots = false, deferControl = false, controlStatus = { status: 'idle', operationId: null, action: null, modelId: null, message: '', startedAt: null, finishedAt: null } } = {}) {
  const read = name => readFileSync(join(webDir, name), 'utf8');
  const doc = makeDocument(read('index.html'));
  const fetches = [];
  const streams = [];
  const pendingSnapshots = [];
  const pendingControls = [];
  const windowListeners = {};
  const errors = [];
  const posted = [];
  const response = (status, body, type) => ({ ok: status >= 200 && status < 300, status, headers: { get: () => type },
    json: async () => JSON.parse(body), text: async () => body });
  const fetch = async (path, init = {}) => {
    const method = String(init.method || 'GET').toUpperCase();
    fetches.push(`${method} ${path}`);
    if (method === 'GET' && path === '/api/snapshot') {
      const value = freshPolling ? { ...snapshot, sampledAt: snapshot.sampledAt + now / 1000, fullSampledAt: snapshot.sampledAt + now / 1000, observedAt: snapshot.sampledAt + now / 1000 } : snapshot;
      const reply = response(200, JSON.stringify(value), 'application/json; charset=utf-8');
      if (deferSnapshots) return new Promise(resolve => pendingSnapshots.push({ resolve: () => resolve(reply), signal: init.signal }));
      return reply;
    }
    if (method === 'GET' && path === '/api/models/control') {const reply=response(200,JSON.stringify(controlStatus),'application/json; charset=utf-8');return deferControl?new Promise(resolve=>pendingControls.push(()=>resolve(reply))):reply;}
    if (method === 'POST' && path === '/api/models/control') {
      return response(403, JSON.stringify({ status: 'error', message: 'Sample data: model controls are off.' }), 'application/json; charset=utf-8');
    }
    return response(404, 'Not found', 'text/plain; charset=utf-8');
  };
  // Timers are recorded, never scheduled: the test advances them by hand.
  const timers = [];
  let now = 0;
  const addTimer = (fn, ms, repeat) => { timers.push({ fn, at: now + (ms || 0), every: repeat ? ms : null }); return timers.length; };
  const clear = id => { if (timers[id - 1]) timers[id - 1].fn = null; };
  const window = {
    matchMedia: () => ({ matches: false }), getComputedStyle: () => ({ transform: 'none' }),
    addEventListener: (type, fn) => { (windowListeners[type] ||= []).push(fn); }, requestAnimationFrame: fn => addTimer(fn, 16, false), cancelAnimationFrame: clear,
    open: () => { throw new Error('window.open must not be called'); },
    removeEventListener: (type, fn) => { windowListeners[type] = (windowListeners[type] || []).filter(f => f !== fn); },
    dispatchEvent: event => { for (const fn of windowListeners[event.type] || []) fn(event); },
    Event: class { constructor(type) { this.type = type; } },
  };
  if (simpleView) {
    window.agiwEdition = { capabilities: { modelActivity: false, windows: false, route: false, fixInference: false, openRuntimeApp: true } };
    window.webkit = { messageHandlers: { monitor: { postMessage: message => posted.push(message) } } };
    window.agiwPrefs = prefs;
  }
  const context = createContext({
    document: doc, window, fetch, AbortController, performance: { now: () => now }, console,
    setTimeout: (fn, ms) => addTimer(fn, ms, false), clearTimeout: clear, setInterval: (fn, ms) => addTimer(fn, ms, true),
    clearInterval: clear, localStorage: { getItem: () => null, setItem: () => {} }, location: { href: 'agiw://app/' },
    ResizeObserver: class { observe() {} }, DOMMatrixReadOnly: class { constructor() { this.m42 = 0; } },
    Date: class extends Date { static now() { return snapshot.sampledAt * 1000 + now; } },
  });
  if (eventSource) context.EventSource = class {
    constructor(path) { this.path = path; this.readyState = 1; this.closed = false; this.listeners = {}; streams.push(this); }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    emit(type, value) { for (const fn of this.listeners[type] || []) fn(type === 'snapshot' ? { data: JSON.stringify(value) } : {}); }
    close() { this.closed = true; this.readyState = 2; }
  };
  // Load the imported modules, then the page script with its imports replaced by their bindings.
  const modules = {};
  const app = read('app.js');
  for (const m of app.matchAll(/^import \{([^}]*)\} from '\/([^']+)';$/gm)) {
    modules[m[2]] ??= await import(`data:text/javascript;base64,${Buffer.from(read(m[2])).toString('base64')}`);
    for (const name of m[1].split(',')) context[name.trim()] = modules[m[2]][name.trim()];
  }
  const script = app.replace(/^import .*$/gm, '')
    + '\n;({render,updateFreshness,nodeSelect,showWholeMap,pollModelControl,setControlError(value){modelControlError=value;},get controlError(){return modelControlError;},get controlStatus(){return modelControlStatus;},get snapshot(){return snapshot;},get graph(){return graph;}})';
  let page;
  try { page = runInContext(script, context, { filename: 'app.js' }); } catch (error) { errors.push(error); }
  let simple;
  if (simpleView) {
    const controlURL = `data:text/javascript;base64,${Buffer.from(read('model-control-view.mjs')).toString('base64')}`;
    const source = read('simple-view.mjs').replace("'/model-control-view.mjs'", JSON.stringify(controlURL));
    const { mountSimpleView } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    simple = mountSimpleView({ window, document: doc, fetch, now: () => (snapshot.sampledAt * 1000 + now) / 1000,
      setTimeout: (fn, ms) => addTimer(fn, ms, false), clearTimeout: clear,
      setInterval: (fn, ms) => addTimer(fn, ms, true), clearInterval: clear });
  }
  const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
  const advance = async ms => {
    const until = now + ms;
    for (let fired = 0; ; fired++) {
      await settle();
      const due = timers.filter(t => t.fn && t.at <= until).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      if (fired > 2000) { errors.push(new Error(`timer storm: more than 2000 timers due within ${ms} ms`)); break; }
      now = due.at;
      const fn = due.fn;
      if (due.every) due.at += due.every; else due.fn = null;
      try { fn(now); } catch (error) { errors.push(error); }
    }
    now = until;
    await settle();
  };
  await advance(1500);
  return { doc, $: id => doc.getElementById(id), fetches, errors, page, advance, streams, pendingSnapshots, pendingControls,
    simple, window, posted, dispatch: (type, detail) => { for (const fn of windowListeners[type] || []) fn({ type, detail }); } };
}

const snapshotFrom = name => {
  const snapshot = JSON.parse(readFileSync(fixture(name), 'utf8'));
  const t = Date.now() / 1000;
  Object.assign(snapshot, { sampledAt: t, fullSampledAt: t, observedAt: t });
  return snapshot;
};

test('check-web-edition.sh expects exactly the files the edition ships', () => {
  const script = readFileSync(join(root, 'scripts', 'check-web-edition.sh'), 'utf8');
  const expected = script.match(/^EXPECTED=\(([^)]*)\)/m)?.[1].trim().split(/\s+/);
  assert.deepEqual(expected, [...ship].sort());
});

test('current upstream builds exactly the eight shipped files', { skip: !process.env.AGIW_WEB_UPSTREAM }, () => {
  const sourceDir = process.env.AGIW_WEB_UPSTREAM;
  const upstream = new Map(readdirSync(sourceDir).map(name => [name, readFileSync(join(sourceDir, name), 'utf8')]));
  const { files } = buildEdition(upstream);
  assert.equal(files.size, 8);
  assert.deepEqual([...files.keys()], ship);
  for (const [name, text] of files) assert.equal(text, readFileSync(join(webDir, name), 'utf8'), name);
});

test('Simple assets keep the feed and fallback but exclude the private module and repair bridge', { skip: !bundled }, () => {
  const html = readFileSync(join(webDir, 'index.html'), 'utf8');
  assert.match(html, /<script src="\/simple-view\.mjs" type="module"><\/script>/);
  assert.match(html, /<link rel="stylesheet" href="\/simple-view\.css">/);
  assert.doesNotMatch(html, /simple-view-private|connection-switches/);
  assert.ok(!existsSync(join(webDir, 'simple-view-private.mjs')));
  const app = readFileSync(join(webDir, 'app.js'), 'utf8');
  assert.doesNotMatch(app, /agiwRepair/);
  assert.match(app, /window\.agiwFeed=Object\.freeze/);
  assert.match(app, /dataset\.svReady/);
  assert.equal((app.match(/function checkStreamHealth\(/g) || []).length, 1);
  for (const name of ['simple-view.mjs', 'simple-view.css']) {
    assert.deepEqual(denylistHits(readFileSync(join(webDir, name), 'utf8'), loadDenylist()), [], name);
  }
});

test('bundled index.html has none of the private-edition elements', { skip: !bundled && 'App/Resources/web not synced' }, () => {
  const html = readFileSync(join(webDir, 'index.html'), 'utf8');
  const doc = makeDocument(html);
  // Booleans only: a failing assert on a DOM node would try to print the whole cyclic tree.
  const present = PRIVATE_IDS.filter(id => doc.getElementById(id) !== null);
  assert.deepEqual(present, [], 'private-edition elements present');
  assert.ok(doc.getElementById('sampleData') !== null, 'the sample-data badge exists');
  assert.ok(doc.getElementById('openRuntimeApp') !== null, 'Open LM Studio exists');
  assert.ok(!existsSync(join(webDir, 'online-code-mode.mjs')), 'online-code-mode.mjs is not bundled');
});

test('every bundled file passes the feature denylist', { skip: !bundled && 'App/Resources/web not synced' }, () => {
  const denylist = loadDenylist();
  for (const name of ship) {
    const hits = denylistHits(readFileSync(join(webDir, name), 'utf8'), denylist);
    assert.deepEqual(hits.map(h => `${name}:${h.line} ${h.pattern}`), []);
  }
});

test('bundled CSS has no orphan selectors for removed private panels', { skip: !bundled && 'App/Resources/web not synced' }, () => {
  const css = readFileSync(join(webDir, 'style.css'), 'utf8');
  for (const selector of ['.mode-v02-evidence', '.mode-v02-note', '.completion-evidence',
    '#fixScopeNote', '#drawerFixButton', '.view-tabs']) {
    assert.ok(!css.includes(selector), `${selector} remains in bundled CSS`);
  }
});

test('sample data: the page labels it SAMPLE DATA, shows the badge and asks for no private route', { skip: !bundled && 'App/Resources/web not synced' }, async () => {
  const { $, fetches, errors, page, advance } = await bootBundledUI(snapshotFrom('demo-idle.json'));
  assert.deepEqual(errors.map(String), []);
  assert.equal($('connection').textContent, 'SAMPLE DATA');
  assert.equal($('sampleData').hidden, false);
  assert.match($('connection').className, /\bsample\b/);
  assert.equal($('mapHeading').getAttribute('aria-label'), 'Sample model and GPU status');
  assert.match($('vitalMac').getAttribute('aria-label'), /^Sample Mac:/);
  assert.match($('vitalMac').title, /^Sample Mac:/);
  assert.match($('vitalMac').textContent, /^Sample/);
  assert.notEqual($('headline').textContent, 'Waiting forthe first signal.');
  assert.equal(page.graph.nodes.filter(n => n.kind === 'model').length, 4, 'four sample models on the map');
  assert.deepEqual([...new Set(page.graph.nodes.map(n => n.kind))].sort(), ['model', 'runtime']);
  assert.equal(page.graph.nodes.find(n => n.kind === 'runtime')?.label, 'Sample Mac');
  assert.equal(page.graph.nodes.find(n => n.kind === 'runtime')?.subtitle, 'SAMPLE HUB');
  assert.ok(page.graph.labels.some(label => label.text === 'OBSERVATION LINKS · NOT TASK ROUTES'));
  assert.ok(page.graph.nodes.every(node => node.factory === true));
  assert.equal($('web').children.length, 0);
  assert.ok(page.graph.labels.some(label => label.text.startsWith('SAMPLE MODELS')));
  // Selecting a model opens the inspector and its Load/Unload dock with sample-data controls off.
  const model = page.graph.nodes.find(n => n.kind === 'model');
  page.nodeSelect(model.id);
  await advance(1200);
  assert.equal($('drawer').hidden, false);
  assert.equal($('inspectorEyebrow').textContent, 'LOCAL MODEL');
  assert.equal($('modelAction').disabled, true);
  assert.match($('inspector').textContent, /sample|Sample/);
  assert.doesNotMatch($('inspector').textContent, /Installed on this Mac/);
  page.showWholeMap();
  await advance(1200);
  page.nodeSelect(page.graph.nodes.find(n => n.kind === 'runtime').id);
  await advance(200);
  assert.match($('inspector').textContent, /Invented sample inventory/);
  assert.match($('inspector').textContent, /Sample GPU/);
  assert.match($('inspector').textContent, /Sample loaded/);
  assert.doesNotMatch($('inspector').textContent, /LM Studio runtime \+ inventory/);
  assert.doesNotMatch($('inspector').textContent, /GPU · all apps/);
  page.showWholeMap();
  await advance(200);
  assert.deepEqual(errors.map(String), []);
  const privateRoutes = fetches.filter(f => !/^GET \/api\/(snapshot|models\/control)$/.test(f));
  assert.deepEqual(privateRoutes, []);
});

test('REST control: the dock loads an unloaded row and unloads the one listed instance', { skip: !bundled && 'App/Resources/web not synced' }, async () => {
  const snapshot = snapshotFrom('demo-idle.json');
  snapshot.demo = false;
  snapshot.sources = snapshot.sources.filter(s => s.id !== 'sample-data');
  snapshot.modelControl = { supported: true, reason: 'Explicit local LM Studio model controls are available.', unloadMode: 'instance' };
  const { $, errors, page, advance } = await bootBundledUI(snapshot);
  // The feed ages with virtual time (fresh for 3 s), so each selection advances only a little.
  const select = async id => { page.nodeSelect(page.graph.nodes.find(n => n.kind === 'model' && n.model?.id === id).id); await advance(200); };
  await select('google/gemma-3-4b');
  assert.equal($('modelAction').textContent, 'Unload');
  assert.equal($('modelAction').disabled, false);
  assert.match($('modelActionStatus').textContent, /cannot see whether the model is answering a request/);
  await select('qwen/qwen3-8b');
  assert.equal($('modelAction').textContent, 'Load');
  assert.equal($('modelAction').disabled, false);
  assert.deepEqual(errors.map(String), []);
});

test('REST control: unload needs unloadMode "instance" and exactly one listed instance', { skip: !bundled && 'App/Resources/web not synced' }, async () => {
  const { modelControlView } = await import(`data:text/javascript;base64,${Buffer.from(readFileSync(join(webDir, 'model-control-view.mjs'))).toString('base64')}`);
  const row = { id: 'google/gemma-3-4b', host: 'mac', source: 'lmstudio-api', state: 'loaded', loaded: true, queued: null,
    modelKey: 'google/gemma-3-4b', loadedInstanceIds: ['google/gemma-3-4b'] };
  const on = { host: 'mac', feedFresh: true, supported: true, reason: '', status: null, models: [row] };
  assert.equal(modelControlView(row, { ...on, unloadMode: 'instance' }).action, 'unload');
  assert.equal(modelControlView(row, on).action, null, 'no unloadMode: no REST unload');
  const two = { ...row, loadedInstanceIds: ['google/gemma-3-4b', 'google/gemma-3-4b:2'] };
  assert.deepEqual(modelControlView(two, { ...on, unloadMode: 'instance' }),
    { action: null, reason: 'More than one instance is loaded; unload it in LM Studio.' });
  assert.equal(modelControlView({ ...row, loadedInstanceIds: null }, { ...on, unloadMode: 'instance' }).action, null);
  assert.equal(modelControlView({ ...row, modelKey: 'other' }, { ...on, unloadMode: 'instance' }).action, null);
  assert.equal(modelControlView(row, { ...on, unloadMode: 'instance', supported: false }).action, null);
  assert.equal(modelControlView(row, { ...on, unloadMode: 'instance', feedFresh: false }).action, null);
});

test('live data: the pill reads LIVE and the badge stays hidden', { skip: !bundled && 'App/Resources/web not synced' }, async () => {
  const snapshot = snapshotFrom('demo-idle.json');
  snapshot.demo = false;
  snapshot.sources = snapshot.sources.filter(s => s.id !== 'sample-data');
  const { $, errors, page } = await bootBundledUI(snapshot);
  assert.deepEqual(errors.map(String), []);
  assert.equal($('connection').textContent, 'LIVE · 1s');
  assert.equal($('sampleData').hidden, true);
  assert.equal($('mapHeading').getAttribute('aria-label'), 'This Mac model and GPU status');
  assert.match($('vitalMac').textContent, /^This Mac/);
  assert.equal(page.graph.nodes.find(n => n.kind === 'runtime')?.label, 'This Mac');
});


const liveFixture = () => {
  const snapshot = snapshotFrom('demo-idle.json');
  snapshot.demo = false;
  snapshot.sources = snapshot.sources.filter(s => s.id !== 'sample-data');
  return snapshot;
};

test('App Store Simple DOM has exactly three rows and preserves instance unload confirmation', { skip: !bundled }, async () => {
  const snapshot = liveFixture();
  snapshot.modelControl = { supported: true, reason: '', unloadMode: 'instance' };
  snapshot.activityKnown = false;
  snapshot.models = [{ id: 'google/gemma-3-4b', name: 'Gemma 3 4B', host: 'mac', state: 'loaded', loaded: true,
    source: 'lmstudio-api', ageSeconds: 0, modelKey: 'google/gemma-3-4b', instanceId: 'instance-1',
    loadedInstanceIds: ['instance-1'], sizeBytes: 4 * 2 ** 30 }];
  snapshot.sources = [{ id: 'lmstudio-api', state: 'live' }, { id: 'lms-ps', state: 'unavailable' }];
  snapshot.memory = { level: 'tight', availablePercent: 12, ramBytes: 16 * 2 ** 30, consumers: [], suggestions: [], paused: [] };
  const ui = await bootBundledUI(snapshot, { simpleView: true, freshPolling: true });
  await ui.advance(16000);
  assert.equal(ui.doc.documentElement.dataset.view, 'simple');
  assert.equal(ui.doc.documentElement.dataset.svReady, '1');
  const body = ui.$('svBody'), rows = body.querySelectorAll('.sv-row');
  assert.deepEqual(rows.map(row => row.getAttribute('data-row')), ['lmstudio', 'models', 'memory']);
  assert.equal(rows[0].getAttribute('data-tone'), 'ok', 'lms-ps is ignored');
  assert.match(rows[2].textContent, /12%/);
  assert.equal(body.querySelector('.sv-model-state').textContent, 'Loaded');
  assert.equal(body.querySelectorAll('.sv-not-in-use').length, 0);
  assert.doesNotMatch(body.textContent, /Right now|On your PC|Answering|Fix \d|Nisi|Jev|PAIR/);
  const unload = body.querySelectorAll('button').find(button => /^Unload Gemma/.test(button.textContent));
  assert.ok(unload, 'REST inventory instance can be unloaded with explicit confirmation');
  unload.click();
  await ui.advance(0);
  assert.match(body.querySelector('.sv-confirm').textContent,
    /AGIW can't see whether it's answering right now\. Unloading stops it\./);
  assert.equal(ui.fetches.filter(call => call.startsWith('POST')).length, 0, 'opening confirmation sends no control');
  assert.ok(ui.fetches.length > 0);
  assert.ok(ui.fetches.every(call => ['GET /api/snapshot', 'GET /api/models/control'].includes(call)));
  assert.deepEqual(ui.errors, []);
  ui.simple.destroy();
});

test('App Store Simple sample and unavailable feeds offer Open LM Studio', { skip: !bundled }, async () => {
  for (const demo of [true, false]) {
    const snapshot = demo ? snapshotFrom('demo-idle.json') : liveFixture();
    snapshot.sources = [{ id: 'lmstudio-api', state: 'unavailable' }];
    snapshot.modelControl = { supported: true, reason: '', unloadMode: 'instance' };
    const ui = await bootBundledUI(snapshot, { simpleView: true, freshPolling: true });
    await ui.advance(16000);
    const body = ui.$('svBody');
    assert.equal(body.querySelectorAll('.sv-row').length, 3);
    assert.match(body.textContent, demo ? /Showing sample data, not your Mac/ : /LM Studio isn't answering/);
    const open = body.querySelectorAll('button').filter(button => button.textContent === 'Open LM Studio');
    assert.equal(open.length, 1);
    open[0].click();
    assert.equal(ui.posted.at(-1)?.action, 'open-runtime-app');
    assert.equal(ui.fetches.filter(call => call.startsWith('POST')).length, 0);
    assert.deepEqual(ui.errors, []);
    ui.simple.destroy();
  }
});

test('Simple native preference initializes, posts toggles and accepts idempotent replay without echo', { skip: !bundled }, async () => {
  const ui = await bootBundledUI(liveFixture(), { simpleView: true, prefs: { view: 'advanced' }, freshPolling: true });
  assert.equal(ui.doc.documentElement.dataset.view, 'advanced');
  ui.$('pause').click();
  ui.$('advancedSwitch').click();
  assert.equal(ui.doc.documentElement.dataset.view, 'simple');
  assert.equal(ui.$('pause').getAttribute('aria-pressed'), 'false', 'Simple resumes a paused view');
  assert.equal(ui.posted.at(-1)?.action, 'view-simple');
  ui.$('advancedSwitch').click();
  assert.equal(ui.posted.at(-1)?.action, 'view-advanced');
  const count = ui.posted.length;
  ui.dispatch('agiw:view', 'simple');
  ui.dispatch('agiw:view', 'simple');
  assert.equal(ui.doc.documentElement.dataset.view, 'simple');
  assert.equal(ui.posted.length, count, 'replay must not echo a new preference message');
  assert.deepEqual(ui.errors, []);
  ui.simple.destroy();
});

test('silent open SSE falls back after ten seconds and rejects late retired callbacks', { skip: !bundled }, async () => {
  const snapshot = liveFixture();
  const ui = await bootBundledUI(snapshot, { eventSource: true, freshPolling: true });
  const first = ui.streams[0];
  first.emit('snapshot', snapshot);
  await ui.advance(8500);
  assert.equal(first.closed, false, 'receipt starts a fresh ten-second grace period');
  assert.equal(ui.fetches.filter(s => s === 'GET /api/snapshot').length, 0);
  await ui.advance(2000);
  assert.equal(first.closed, true, 'an open-but-silent stream must release its transport');
  assert.match(ui.$('connection').textContent, /^LIVE/);
  assert.equal(ui.fetches.filter(s => s === 'GET /api/snapshot').length, 1);
  first.emit('error');
  first.emit('snapshot', { ...snapshot, sampledAt: 1 });
  assert.notEqual(ui.page.snapshot.sampledAt, 1, 'retired stream cannot overwrite recovered data');
  await ui.advance(5000);
  assert.equal(ui.streams.length, 2, 'only one bounded reconnect is scheduled');
  const second = ui.streams[1];
  second.emit('snapshot', { ...snapshot, sampledAt: snapshot.sampledAt + 17 });
  const count = ui.fetches.filter(s => s === 'GET /api/snapshot').length;
  await ui.advance(2000);
  assert.equal(ui.fetches.filter(s => s === 'GET /api/snapshot').length, count, 'polling stops when the new stream owns transport');
  assert.deepEqual(ui.errors, []);
});

test('healthy sample and paused snapshots never cause stale-view transport resets', { skip: !bundled }, async () => {
  const snapshot = snapshotFrom('demo-idle.json');
  snapshot.demo = true;
  const ui = await bootBundledUI(snapshot, { eventSource: true });
  ui.streams[0].emit('snapshot', snapshot);
  await ui.advance(100);
  assert.equal(ui.$('connection').textContent, 'SAMPLE DATA');
  ui.$('pause').dispatch('click');
  for (let i = 0; i < 12; i++) {
    ui.streams[0].emit('snapshot', { ...snapshot, sampledAt: snapshot.sampledAt + i });
    await ui.advance(1000);
  }
  assert.equal(ui.$('connection').textContent, 'VIEW PAUSED');
  assert.equal(ui.streams.length, 1);
  assert.equal(ui.streams[0].closed, false);
  assert.equal(ui.page.snapshot.demo, true);
  assert.equal(ui.page.snapshot.sampledAt, snapshot.sampledAt, 'paused view stays frozen while transport keeps receiving');
  assert.equal(ui.fetches.filter(s => s === 'GET /api/snapshot').length, 0);
  assert.deepEqual(ui.errors, []);
});

test('page teardown aborts polling and prevents late callbacks or retries; restored page opens once', { skip: !bundled }, async () => {
  const snapshot = liveFixture();
  const ui = await bootBundledUI(snapshot, { eventSource: true, deferSnapshots: true });
  ui.streams[0].emit('error');
  assert.equal(ui.pendingSnapshots.length, 1);
  ui.dispatch('pagehide');
  assert.equal(ui.pendingSnapshots[0].signal.aborted, true);
  ui.pendingSnapshots[0].resolve();
  ui.streams[0].emit('snapshot', snapshot);
  await ui.advance(20000);
  assert.equal(ui.page.snapshot, null);
  assert.equal(ui.streams.length, 1);
  assert.equal(ui.fetches.filter(s => s === 'GET /api/snapshot').length, 1);
  ui.dispatch('pageshow');
  ui.dispatch('pageshow');
  assert.equal(ui.streams.length, 2);
  assert.equal(ui.streams.filter(s => !s.closed).length, 1);
  assert.deepEqual(ui.errors, []);
});

test('hidden stream is left alone and silently stale transport recovers when visible', { skip: !bundled }, async () => {
  const snapshot = liveFixture();
  const ui = await bootBundledUI(snapshot, { eventSource: true, freshPolling: true });
  ui.streams[0].emit('snapshot', snapshot);
  ui.doc.hidden = true;
  await ui.advance(20000);
  assert.equal(ui.streams[0].closed, false, 'native visibility gating intentionally suspends hidden delivery');
  assert.equal(ui.fetches.filter(s => s === 'GET /api/snapshot').length, 0);
  ui.doc.hidden = false;
  await ui.advance(1000);
  assert.equal(ui.streams[0].closed, true);
  assert.match(ui.$('connection').textContent, /^LIVE/);
  assert.deepEqual(ui.errors, []);
});

test('factory edition geometry keeps private stations absent and compact models separated', { skip: !bundled }, async () => {
  const map = await import(pathToFileURL(join(webDir,'map-layout.mjs')));
  for (const [viewportWidth, viewportHeight] of [[360,780],[820,660],[1260,900]]) {
    const layout=map.factoryLayout(12,[],{viewportWidth,viewportHeight});
    assert.deepEqual(Object.keys(layout).sort(), ['labels','portrait','runtime','runtimeModels']);
    assert.equal(layout.labels.length,2);
    for(let i=0;i<layout.runtimeModels.length;i++)for(let j=i+1;j<layout.runtimeModels.length;j++){
      const a=layout.runtimeModels[i],b=layout.runtimeModels[j];
      assert.ok(Math.abs(a.x-b.x)>=map.FACTORY_STATION.width || Math.abs(a.y-b.y)>=map.FACTORY_STATION.height);
    }
    assert.doesNotMatch(map.observationRoute(layout.runtime,layout.runtimeModels[0]),/[QC]/);
  }
  assert.equal(map.orbWebLayout,undefined);
  assert.equal(map.createWebMotion,undefined);
});

test('factory edition does not animate models when API activity is unknown', { skip: !bundled }, async () => {
  const snapshot=liveFixture();snapshot.activityKnown=false;
  snapshot.models=snapshot.models.map(model=>({...model,state:'generating',loaded:true}));
  const {page,$,errors}=await bootBundledUI(snapshot);
  assert.ok(page.graph.nodes.filter(node=>node.kind==='model').every(node=>node.active===false));
  assert.doesNotMatch($('vitalMac').textContent,/generating/);
  assert.equal($('web').children.length,0);
  assert.deepEqual(errors.map(String),[]);
});

test('edition live-to-sample transition counts rows and isolates late control replies', {skip:!bundled}, async()=>{
 const live=liveFixture();while(live.models.length<6)live.models.push({...live.models[0],id:`extra-${live.models.length}`});
 const ui=await bootBundledUI(live,{eventSource:true,deferControl:true,controlStatus:{status:'failed',message:'old memory refusal'}});
 ui.streams[0].emit('snapshot',live);await ui.advance(20);
 ui.page.setControlError('old memory refusal');const pending=ui.page.pollModelControl();
 const sample=snapshotFrom('demo-idle.json');sample.models=sample.models.slice(0,4);
 ui.streams[0].emit('snapshot',sample);await ui.advance(20);
 for(const resolve of ui.pendingControls)resolve();await pending;
 assert.equal(ui.page.controlError,'');assert.equal(ui.page.controlStatus,null);
 assert.ok(ui.page.graph.labels.some(l=>l.text==='SAMPLE MODELS · 4'));
 assert.match(ui.$('sourceCount').textContent,/illustrative live.*illustrative recorded/);
 ui.page.nodeSelect(`model:${sample.models[0].host}:${sample.models[0].id}`);
 assert.doesNotMatch(ui.$('modelActionStatus').textContent,/old memory refusal/);assert.equal(ui.$('modelAction').disabled,true);
 assert.deepEqual(ui.errors,[]);
});
