// Positive and negative checks for the current Jaryo sources under the store edition rules.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { EditionError, sha256, splitJs, denylistHits } from './engine.mjs';
import { buildEdition, loadDenylist } from './build.mjs';
import { PINS } from './jaryo-adapt.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const upstreamDir = process.env.AGIW_WEB_UPSTREAM;
const webDir = process.env.AGIW_WEB_DIR;
const readDir = dir => {
  const names = ['index.html', 'style.css', 'app.js', 'usage-format.mjs', 'online-code-mode.mjs', 'map-layout.mjs', 'model-control-view.mjs', 'simple-view.mjs', 'simple-view.css'];
  return new Map(names.filter(name => existsSync(join(dir, name))).map(name => [name, readFileSync(join(dir, name), 'utf8')]));
};

test('reviewed pins are literals and match the current statements', { skip: !upstreamDir }, () => {
  const source = readFileSync(join(here, 'jaryo-adapt.mjs'), 'utf8');
  assert.doesNotMatch(source, /sha256\(/);
  const upstream = readDir(upstreamDir);
  for (const [file, pins] of Object.entries(PINS)) {
    const map = new Map(splitJs(upstream.get(file)).statements.map(statement => [statement.key, statement]));
    for (const [key, pin] of Object.entries(pins)) {
      assert.equal(source.includes(pin), true, pin);
      assert.equal(sha256(map.get(key).text), pin, `${file} ${key}`);
    }
  }
});

test('feature denylist file is unchanged from the wrapper archive', () => {
  const text = readFileSync(join(here, 'feature-denylist.txt'));
  assert.equal(createHash('sha256').update(text).digest('hex'),
    '03378746a130f9217b679a475a14ac14236c53e90c404c3fd3a5c426d0b761f9');
});

test('edition engine matches the archived fail-closed engine', () => {
  const text = readFileSync(join(here, 'engine.mjs'));
  assert.equal(createHash('sha256').update(text).digest('hex'),
    '159bb12d14edae6fca2d37643300dcac7c5e341d7d97c6e4dd2928c925ed576f');
});

test('dropping the pipeline denylist pattern would hide that word', () => {
  const full = loadDenylist();
  const weakened = full.filter(item => item.label !== 'route pipeline');
  assert.equal(denylistHits('const leak = "pipeline";\n', full).length, 1);
  assert.equal(denylistHits('const leak = "pipeline";\n', weakened).length, 0);
});

test('current Jaryo web builds a denylist-clean edition', { skip: !upstreamDir }, () => {
  const upstream = readDir(upstreamDir);
  const { files } = buildEdition(upstream);
  assert.deepEqual([...files.keys()], ['index.html', 'style.css', 'app.js', 'usage-format.mjs', 'map-layout.mjs', 'model-control-view.mjs', 'simple-view.mjs', 'simple-view.css']);
  const denylist = loadDenylist();
  for (const [name, text] of files) {
    assert.deepEqual(denylistHits(text, denylist).map(hit => `${name}:${hit.line}`), []);
  }
  const html = files.get('index.html');
  const sourceHtml = upstream.get('index.html');
  assert.match(sourceHtml, /id="fixInference"/);
  assert.match(sourceHtml, /Fix inference/);
  for (const id of ['onlineCodeMode', 'fixInference', 'pcSwitch', 'vitalPc', 'vitalRoute', 'vitalActivity', 'memoryBanner']) {
    assert.equal(html.includes(`id="${id}"`), false, id);
  }
  assert.doesNotMatch(files.get('app.js'), /function openFixInference|requestOnlineCodeAction/);
  assert.match(html, /src="\/simple-view\.mjs"/);
  assert.match(html, /href="\/simple-view\.css"/);
  assert.doesNotMatch(html, /jaryo\.css|jaryo\.mjs|simple-view-private|connection-switches/);
  assert.match(html, /<nav class="j-nav"/);
  assert.match(html, /data-screen="overview"/);
  assert.match(html, /data-screen="map"/);
  assert.match(html, /data-screen="components"/);
  assert.match(html, /data-screen="identity"/);
  assert.match(html, /id="jaryoTheme"/);
  assert.match(html, /id="screenComponents"/);
  assert.match(html, /id="screenIdentity"/);
  assert.match(html, /<title>AGIW · Overview<\/title>/);
  assert.match(sourceHtml, /<a href="\/" aria-current="page">Overview<\/a>/);
  assert.match(sourceHtml, /href="\/monitor"/);
  assert.match(sourceHtml, /href="\/components"/);
  assert.match(sourceHtml, /href="\/identity"/);
  assert.doesNotMatch(html, /href="\/monitor"|href="\/components"|href="\/identity"|<div class="j-chrome"/);
  assert.match(files.get('app.js'), /function showEditionScreen/);
  assert.doesNotMatch(files.get('app.js'), /\/api\/inference|\/api\/components|fix inference|online code/i);
  assert.match(files.get('style.css'), /prefers-color-scheme:light/);
  assert.match(files.get('style.css'), /data-theme="dark"/);
  assert.match(files.get('style.css'), /data-theme="light"/);
  const app = files.get('app.js');
  assert.equal((app.match(/function checkStreamHealth\(/g) || []).length, 1);
  assert.match(app, /window\.agiwFeed=Object\.freeze/);
  assert.match(app, /dataset\.svReady/);
  assert.doesNotMatch(app, /agiwRepair/);
  if (webDir) {
    for (const [name, text] of files) assert.equal(text, readFileSync(join(webDir, name), 'utf8'), name);
  }
});

test('a drifted pinned statement is refused', { skip: !upstreamDir }, () => {
  const upstream = readDir(upstreamDir);
  upstream.set('app.js', upstream.get('app.js').replace('function vitalsInput()', 'function vitalsInput ()'));
  assert.throws(() => buildEdition(upstream), error => error instanceof EditionError && error.problems.some(problem => problem.includes('function:vitalsInput')));
});

test('a missing simple-view file is refused', { skip: !upstreamDir }, () => {
  const upstream = readDir(upstreamDir);
  upstream.delete('simple-view.mjs');
  assert.throws(() => buildEdition(upstream), error => error instanceof EditionError && error.problems.some(problem => problem.includes('simple-view.mjs')));
});

test('a private word in the compact face is refused', { skip: !upstreamDir }, () => {
  const upstream = readDir(upstreamDir);
  upstream.set('simple-view.mjs', `${upstream.get('simple-view.mjs')}\nconst leak = "pipeline";\n`);
  assert.throws(() => buildEdition(upstream), error => error instanceof EditionError && error.problems.some(problem => problem.includes('pipeline')));
});

test('Xcode pin 27A266a stays in the distribution script', { skip: !process.env.AGIW_WRAPPER_ROOT }, () => {
  const text = readFileSync(join(process.env.AGIW_WRAPPER_ROOT, 'scripts', 'appstore-distribution.sh'), 'utf8');
  assert.match(text, /RELEASE_XCODE_BUILD=27A266a/);
  assert.doesNotMatch(text, /skip.*27A266a|27A266a.*skip/i);
});

function element(document, tag) {
  const node = {
    tag, children: [], className: '', textContent: '', id: '',
    dataset: {}, style: {},
    setAttribute(name, value) { this.attrs[name] = value; if (name === 'id') this.id = value; },
    getAttribute(name) { return this.attrs[name]; },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
    remove() {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    closest(selector) { return selector === `#${this.id}` ? this : null; },
    querySelectorAll() { return []; },
    attrs: {}, listeners: {},
  };
  Object.defineProperty(node, 'textContent', {
    get() { return node.children.length ? node.children.map(child => child.textContent || '').join('') : (node._text || ''); },
    set(value) { node._text = value; node.children = []; },
  });
  node._text = '';
  document.nodes.push(node);
  return node;
}

test('compact face keeps three rows, confirms unload, and posts no control fetch', { skip: !webDir }, async () => {
  const controlURL = `data:text/javascript;base64,${Buffer.from(readFileSync(join(webDir, 'model-control-view.mjs'))).toString('base64')}`;
  const source = readFileSync(join(webDir, 'simple-view.mjs'), 'utf8').replace("'/model-control-view.mjs'", JSON.stringify(controlURL));
  const { mountSimpleView } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
  const fetches = [];
  const posted = [];
  const document = { nodes: [], listeners: {}, documentElement: { dataset: {} }, body: { append() {} } };
  document.createElement = tag => element(document, tag);
  document.addEventListener = (type, fn) => { (document.listeners[type] ||= []).push(fn); };
  document.getElementById = id => document.nodes.find(node => node.id === id) || null;
  document.body.append = node => { document.bodyNode = node; };
  const window = {
    agiwPrefs: { view: 'simple' },
    webkit: { messageHandlers: { monitor: { postMessage: message => posted.push(message) } } },
    addEventListener() {},
  };
  const clock = 1_700_000_000;
  const snapshot = {
    demo: false,
    sampledAt: clock,
    fullSampledAt: clock,
    sources: [{ id: 'lmstudio-api', state: 'live' }, { id: 'lms-ps', state: 'unavailable' }],
    modelControl: { supported: true, unloadMode: 'instance' },
    memory: { availablePercent: 12 },
    models: [{ id: 'google/gemma-3-4b', name: 'Gemma 3 4B', host: 'mac', state: 'loaded', loaded: true,
      source: 'lmstudio-api', modelKey: 'google/gemma-3-4b', loadedInstanceIds: ['instance-1'] }],
  };
  const fetch = async path => {
    fetches.push(`GET ${path}`);
    const body = path === '/api/snapshot' ? snapshot : { status: 'idle' };
    return { ok: true, json: async () => body };
  };
  const timers = [];
  mountSimpleView({
    window, document, fetch, now: () => clock,
    setInterval: fn => { timers.push(fn); return timers.length; },
    clearInterval() {},
  });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  const body = document.bodyNode;
  const rows = body.children.filter(node => node.className === 'sv-row');
  assert.deepEqual(rows.map(row => row.getAttribute('data-row')), ['lmstudio', 'models', 'memory']);
  assert.equal(rows[0].getAttribute('data-tone'), 'ok');
  assert.match(body.textContent, /12%/);
  const unload = body.children.flatMap(row => row.children).find(node => node.tag === 'button' && /^Unload Gemma/.test(node.textContent));
  assert.ok(unload);
  unload.listeners.click[0]();
  assert.match(body.textContent, /AGIW can't see whether it's answering right now/);
  assert.deepEqual(fetches.filter(call => call.startsWith('POST')), []);
  const unavailable = { ...snapshot, demo: false, sources: [{ id: 'lmstudio-api', state: 'unavailable' }] };
  const second = [];
  const document2 = { nodes: [], listeners: {}, documentElement: { dataset: {} }, body: { append() {} } };
  document2.createElement = tag => element(document2, tag);
  document2.addEventListener = (type, fn) => { (document2.listeners[type] ||= []).push(fn); };
  document2.getElementById = () => null;
  document2.body.append = node => { document2.bodyNode = node; };
  const posted2 = [];
  const window2 = { agiwPrefs: { view: 'simple' }, webkit: { messageHandlers: { monitor: { postMessage: message => posted2.push(message) } } }, addEventListener() {} };
  const timers2 = [];
  mountSimpleView({
    window: window2, document: document2, now: () => clock,
    fetch: async path => { second.push(path); return { ok: true, json: async () => (path === '/api/snapshot' ? unavailable : { status: 'idle' }) }; },
    setInterval: fn => { timers2.push(fn); return 1; }, clearInterval() {},
  });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  const open = document2.bodyNode.children.flatMap(row => row.children).filter(node => node.textContent === 'Open LM Studio');
  assert.equal(open.length, 1);
  open[0].listeners.click[0]();
  assert.equal(posted2.at(-1)?.action, 'open-runtime-app');
  assert.equal(second.filter(path => path.startsWith('POST')).length, 0);
});

test('edition model control still refuses unload without an instance mode', { skip: !webDir }, async () => {
  const source = readFileSync(join(webDir, 'model-control-view.mjs'), 'utf8');
  const url = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
  const { modelControlView } = await import(url);
  const row = { id: 'google/gemma-3-4b', host: 'mac', source: 'lmstudio-api', state: 'loaded', loaded: true,
    modelKey: 'google/gemma-3-4b', loadedInstanceIds: ['instance-1'] };
  const on = { host: 'mac', feedFresh: true, supported: true, reason: '', status: null, models: [row] };
  assert.equal(modelControlView(row, on).action, null);
  assert.equal(modelControlView(row, { ...on, unloadMode: 'instance' }).action, 'unload');
  assert.equal(modelControlView({ ...row, loadedInstanceIds: ['instance-1', 'instance-2'] }, { ...on, unloadMode: 'instance' }).action, null);
});
