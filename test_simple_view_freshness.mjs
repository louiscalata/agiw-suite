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

test('an empty or schema-invalid control reply does not enable unload', async () => {
  const replies = [{}, { status: '' }, { status: 'error' }, { status: 1 }, { status: null }, { message: 'ok' }, []];
  for (const reply of replies) {
    const view = mount(async path => path === '/api/snapshot' ? ok(snapshot()) : ok(reply));
    await flush();
    assert.equal(unloadButton(view.document.bodyNode), undefined, JSON.stringify(reply));
    assert.match(view.document.bodyNode.textContent, /Control is unavailable/);
    assert.match(view.document.bodyNode.textContent, /Loaded/);
    assert.doesNotMatch(view.document.bodyNode.textContent, /No local model/);
  }
});

test('unknown inventory does not claim that no model is loaded', async () => {
  const cases = [
    [{ models: [{ id: 'mystery' }] }, /Model state unknown/],
    [{ models: [{ id: 'mystery', state: 'unknown', loaded: null }] }, /Model state unknown/],
    [{ models: [{ id: 'mystery', state: 'stale', loaded: true }] }, /Model state unknown/],
    [{ models: null }, /Model state unknown/],
    [{ sampledAt: CLOCK - 30, fullSampledAt: CLOCK - 30, models: [] }, /Model state unknown/],
  ];
  for (const [extra, pattern] of cases) {
    const view = mount(async path => path === '/api/snapshot' ? ok(snapshot('Example', extra)) : ok({ status: 'idle' }));
    await flush();
    assert.equal(unloadButton(view.document.bodyNode), undefined, JSON.stringify(extra));
    assert.match(view.document.bodyNode.textContent, pattern);
    assert.doesNotMatch(view.document.bodyNode.textContent, /No local model/);
  }
  const empty = mount(async path => path === '/api/snapshot' ? ok(snapshot('Example', { models: [] })) : ok({ status: 'idle' }));
  await flush();
  assert.equal(unloadButton(empty.document.bodyNode), undefined);
  assert.match(empty.document.bodyNode.textContent, /No local model/);
  const unloaded = { id: 'gone', host: 'mac', loaded: false, state: 'unloaded' };
  const allUnloaded = mount(async path => path === '/api/snapshot' ? ok(snapshot('Example', { models: [unloaded, { ...unloaded, id: 'gone-2', state: 'idle' }] })) : ok({ status: 'idle' }));
  await flush();
  assert.equal(unloadButton(allUnloaded.document.bodyNode), undefined);
  assert.match(allUnloaded.document.bodyNode.textContent, /No local model/);
  const loaded = mount(async path => path === '/api/snapshot' ? ok(snapshot()) : ok({ status: 'failed' }));
  await flush();
  assert.equal(unloadButton(loaded.document.bodyNode).textContent, 'Unload Example');
  assert.match(loaded.document.bodyNode.textContent, /Loaded/);
});

test('a malformed row keeps the inventory unknown in either order', async () => {
  const unloaded = { id: 'gone', host: 'mac', loaded: false, state: 'unloaded' };
  const unknown = { id: 'mystery', state: 'unknown', loaded: null };
  const malformed = ['row', 1, null];
  const lists = [
    [null, unloaded],
    [unloaded, null],
    [unknown, unloaded],
    [unloaded, unknown],
    [malformed[0], unloaded],
    [unloaded, malformed[1]],
    [null, model('Beside')],
    [model('Beside'), null],
  ];
  for (const models of lists) {
    const view = mount(async path => path === '/api/snapshot' ? ok(snapshot('Example', { models })) : ok({ status: 'idle' }));
    await flush();
    assert.equal(unloadButton(view.document.bodyNode), undefined, JSON.stringify(models));
    assert.match(view.document.bodyNode.textContent, /Model state unknown/);
    assert.doesNotMatch(view.document.bodyNode.textContent, /No local model/);
  }
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

test('the status ModelControl sends after a confirmed operation keeps the compact view answering', async () => {
  const reply = { status: 'succeeded', operationId: 'abc', action: 'unload', modelId: 'instance-1', settlement: 'confirmed',
    message: 'Model unload confirmed by fresh LM Studio inventory.', cancellationRequested: false };
  const view = mount(async path => path === '/api/snapshot' ? ok(snapshot()) : ok(reply));
  await flush();
  assert.doesNotMatch(view.document.bodyNode.textContent, /Control is unavailable/);
  assert.ok(unloadButton(view.document.bodyNode), 'the next unload is offered again');
  assert.match(view.document.bodyNode.textContent, /DONE, INVENTORY CONFIRMS/);
});

test('the compact memory line needs a fresh sample and a percentage that can be one', async () => {
  const cases = [
    [{ memory: { availablePercent: 40 } }, /40% available/],
    [{ memory: { availablePercent: 0 } }, /0% available/],
    [{ memory: { availablePercent: 100 } }, /100% available/],
    [{ memory: { availablePercent: 140 } }, /Memory not reported/],
    [{ memory: { availablePercent: -5 } }, /Memory not reported/],
    [{ memory: { availablePercent: '40' } }, /Memory not reported/],
    [{ memory: {} }, /Memory not reported/],
    [{ sampledAt: CLOCK - 100, fullSampledAt: CLOCK - 100, memory: { availablePercent: 40 } }, /Memory not reported/],
  ];
  for (const [extra, expected] of cases) {
    const view = mount(async path => path === '/api/snapshot' ? ok(snapshot('Example', extra)) : ok({ status: 'idle' }));
    await flush();
    assert.match(view.document.bodyNode.textContent, expected, JSON.stringify(extra));
  }
});
