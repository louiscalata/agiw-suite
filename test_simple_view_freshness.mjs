import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const CLOCK = 1_700_000_000;
const source = readFileSync(new URL('./web/simple-view.mjs', import.meta.url), 'utf8')
  .replace("'/model-control-view.mjs'", JSON.stringify(new URL('./web/model-control-view.mjs', import.meta.url).href));
const { mountSimpleView } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function element(document, tag) {
  const node = {
    tag, className: '', id: '', children: [], attrs: {}, listeners: {}, _text: '', dataset: {},
    setAttribute(name, value) { this.attrs[name] = value; if (name === 'id') this.id = value; },
    getAttribute(name) { return this.attrs[name]; },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    closest() { return null; },
  };
  Object.defineProperty(node, 'textContent', {
    get() { return node.children.length ? node.children.map(child => child.textContent || '').join('') : node._text; },
    set(value) { node._text = value; node.children = []; },
  });
  document.nodes.push(node);
  return node;
}

function documentFor() {
  const document = { nodes: [], documentElement: { dataset: {} }, body: { append() {} }, tick: null };
  document.createElement = tag => element(document, tag);
  document.addEventListener = () => {};
  document.removeEventListener = () => {};
  document.getElementById = id => document.nodes.find(node => node.id === id) || null;
  document.body.append = node => { document.bodyNode = node; };
  return document;
}

const windowFor = () => ({
  agiwPrefs: { view: 'simple' },
  webkit: { messageHandlers: { monitor: { postMessage() {} } } },
  addEventListener() {},
});

function model(name = 'Example') {
  return {
    id: 'instance-1', name, host: 'mac', source: 'lms-ps', state: 'idle', queued: 0, loaded: true,
    modelKey: 'example', instanceId: 'instance-1', loadedInstanceIds: ['instance-1'],
  };
}

function snapshot(name = 'Example', extra = {}) {
  return {
    demo: false, sampledAt: CLOCK, fullSampledAt: CLOCK,
    sources: [{ id: 'lmstudio-api', state: 'live' }],
    modelControl: { supported: true, reason: '' },
    memory: { availablePercent: 40 },
    models: [model(name)],
    ...extra,
  };
}

const ok = body => ({ ok: true, json: async () => body });
const rejected = () => ({ ok: false, status: 503, json: async () => ({ status: 'starting' }) });
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };

function unloadButton(body) {
  const walk = node => [node, ...(node.children || []).flatMap(walk)];
  return walk(body).find(node => node.tag === 'button' && /^Unload /.test(node.textContent || ''));
}

function mount(fetch) {
  const document = documentFor();
  const posts = [];
  mountSimpleView({
    window: { ...windowFor(), webkit: { messageHandlers: { monitor: { postMessage: message => posts.push(message) } } } },
    document, fetch, now: () => CLOCK,
    setInterval(fn) { document.tick = fn; return 1; },
    clearInterval() {},
  });
  return { document, posts };
}

test('the face opens on Overview unless simple was saved', async () => {
  const opened = documentFor();
  mountSimpleView({
    window: { agiwPrefs: {}, webkit: { messageHandlers: { monitor: { postMessage() {} } } }, addEventListener() {} },
    document: opened, fetch: async () => rejected(), now: () => CLOCK,
    setInterval() { return 1; }, clearInterval() {},
  });
  await flush();
  assert.equal(opened.documentElement.dataset.view, 'advanced');
  const saved = documentFor();
  mountSimpleView({
    window: windowFor(),
    document: saved, fetch: async () => rejected(), now: () => CLOCK,
    setInterval() { return 1; }, clearInterval() {},
  });
  await flush();
  assert.equal(saved.documentElement.dataset.view, 'simple');
  assert.match(saved.bodyNode.textContent, /Inventory unknown/);
  assert.doesNotMatch(saved.bodyNode.textContent, /Unload /);
});

test('compact unload confirms in the face and does not submit control', () => {
  assert.doesNotMatch(source, /submitModelControl/);
  assert.doesNotMatch(source, /method:\s*['"]POST['"]/);
  assert.doesNotMatch(source, /feedFresh:\s*true/);
});

test('a rejected snapshot withdraws an eligible unload', async () => {
  let mode = 'eligible';
  const calls = [];
  const view = mount(async path => {
    calls.push(String(path));
    if (path === '/api/snapshot') return mode === 'eligible' ? ok(snapshot()) : rejected();
    return ok({ status: 'idle' });
  });
  await flush();
  const button = unloadButton(view.document.bodyNode);
  assert.ok(button);
  button.listeners.click[0]();
  assert.match(view.document.bodyNode.textContent, /Unloading stops it/);
  assert.deepEqual(calls.filter(path => path.startsWith('POST')), []);
  mode = 'reject';
  view.document.tick();
  await flush();
  assert.equal(unloadButton(view.document.bodyNode), undefined);
  assert.match(view.document.bodyNode.textContent, /Inventory unknown/);
  assert.match(view.document.bodyNode.textContent, /Control is unavailable/);
});

test('a rejected control reply keeps the new snapshot and withdraws unload', async () => {
  let controlOk = true;
  let label = 'First';
  let memory = 40;
  const view = mount(async path => {
    if (path === '/api/snapshot') return ok(snapshot(label, { memory: { availablePercent: memory } }));
    return controlOk ? ok({ status: 'idle' }) : rejected();
  });
  await flush();
  assert.equal(unloadButton(view.document.bodyNode).textContent, 'Unload First');
  controlOk = false;
  label = 'Second';
  memory = 55;
  view.document.tick();
  await flush();
  assert.equal(unloadButton(view.document.bodyNode), undefined);
  assert.match(view.document.bodyNode.textContent, /Control is unavailable/);
  assert.match(view.document.bodyNode.textContent, /55% available/);
  assert.doesNotMatch(view.document.bodyNode.textContent, /Unload /);
});

test('demo, non-live, stale, and undated samples do not offer unload', async () => {
  const cases = [
    [{ demo: true }, /sample data/],
    [{ sources: [{ id: 'lmstudio-api', state: 'unavailable' }] }, /isn't answering/],
    [{ sampledAt: CLOCK - 30, fullSampledAt: CLOCK - 30 }, /stale/],
    [{ sampledAt: undefined, fullSampledAt: undefined }, /Control is unavailable/],
  ];
  for (const [extra, pattern] of cases) {
    const view = mount(async path => path === '/api/snapshot' ? ok(snapshot('Example', extra)) : ok({ status: 'idle' }));
    await flush();
    assert.equal(unloadButton(view.document.bodyNode), undefined, JSON.stringify(extra));
    assert.match(view.document.bodyNode.textContent, pattern);
  }
});

test('an older poll cannot overwrite a newer result', async () => {
  const waiting = [];
  let hold = false;
  const view = mount(path => {
    if (!hold) return Promise.resolve(ok(path === '/api/snapshot' ? snapshot('Seed') : { status: 'idle' }));
    return new Promise(resolve => waiting.push({ path, resolve }));
  });
  await flush();
  assert.equal(unloadButton(view.document.bodyNode).textContent, 'Unload Seed');
  hold = true;
  view.document.tick();
  view.document.tick();
  await flush();
  const olderSnapshot = waiting.shift();
  const newerSnapshot = waiting.shift();
  newerSnapshot.resolve(rejected());
  await flush();
  olderSnapshot.resolve(ok(snapshot('Old')));
  await flush();
  assert.equal(unloadButton(view.document.bodyNode), undefined);
  assert.match(view.document.bodyNode.textContent, /Inventory unknown/);
  assert.doesNotMatch(view.document.bodyNode.textContent, /Old/);

  waiting.length = 0;
  view.document.tick();
  view.document.tick();
  await flush();
  const firstSnapshot = waiting.shift();
  const secondSnapshot = waiting.shift();
  secondSnapshot.resolve(ok(snapshot('Newest')));
  await flush();
  const secondControl = waiting.shift();
  assert.equal(secondControl.path, '/api/models/control');
  secondControl.resolve(ok({ status: 'idle' }));
  await flush();
  firstSnapshot.resolve(ok(snapshot('Stale')));
  await flush();
  const firstControl = waiting.find(item => item.path === '/api/models/control');
  firstControl?.resolve(ok({ status: 'idle' }));
  await flush();
  assert.equal(unloadButton(view.document.bodyNode).textContent, 'Unload Newest');
  assert.doesNotMatch(view.document.bodyNode.textContent, /Stale/);
});
