import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {place, rateText} from './web/monitor.mjs';

const source = readFileSync(new URL('./web/monitor.mjs', import.meta.url), 'utf8').replace(/export /g, '');
const flush = () => new Promise(resolve => setImmediate(resolve));
const documentAt = (at, nodes = [{id: 'mac:lmstudio', machine: 'mac', label: 'LM Studio',
  health: 'ok', modelId: 'qwen', generationTokensPerSecond: 80}]) => ({
  observedAt: new Date(at).toISOString(), nodes: nodes.map(node => ({observedAt: new Date(at).toISOString(), ...node})),
  edges: [], tunnel: {state: 'up'},
});

function harness(fetcher, clock = Date) {
  class Element {
    constructor() { this.children = []; this.attributes = {}; this.dataset = {}; this.style = {}; this.hidden = true; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener(name, callback) { this[name] = callback; }
  }
  const elements = Object.fromEntries(['map', 'tunnel', 'detail', 'detailTitle', 'detailFacts'].map(id => [id, new Element()]));
  const intervals = [];
  const timeouts = [];
  const context = vm.createContext({Date: clock, URL, AbortController, console, fetch: fetcher,
    setInterval: callback => { intervals.push(callback); return intervals.length; },
    setTimeout: callback => { timeouts.push(callback); return timeouts.length; }, clearTimeout: () => {},
    document: {querySelector: selector => elements[selector.slice(1)],
      createElementNS: () => new Element(), createElement: () => new Element()},
  });
  vm.runInContext(source, context);
  return {elements, intervals, timeouts, context};
}

test('the actual page permits only one refresh in flight', async () => {
  let calls = 0;
  let finish;
  const h = harness(() => { calls++; return new Promise(resolve => { finish = resolve; }); });
  h.intervals[0]();
  assert.equal(calls, 1);
  finish({ok: true, json: async () => documentAt(Date.now())});
  await flush();
});

test('a failed refresh clears health, measurements and selected details', async () => {
  let calls = 0;
  const h = harness(async () => {
    if (++calls > 1) throw new Error('offline');
    return {ok: true, json: async () => documentAt(Date.now())};
  });
  await flush();
  vm.runInContext("selected = 'mac:lmstudio'; show(current.nodes[0]);", h.context);
  await h.intervals[0]();
  await flush();
  const node = vm.runInContext('current.nodes[0]', h.context);
  assert.equal(node.health, 'not_measured');
  assert.equal(node.generationTokensPerSecond, null);
  assert.equal(node.modelId, null);
  assert.equal(h.elements.detailFacts.children.some(item => String(item.textContent).includes('80')), false);
  assert.equal(h.elements.tunnel.textContent, 'Monitor feed unavailable');
});

test('a successful response with old timestamps cannot look healthy', async () => {
  const h = harness(async () => ({ok: true, json: async () => documentAt(Date.now() - 31000)}));
  await flush();
  assert.equal(vm.runInContext('current.nodes[0].health', h.context), 'not_measured');
});

test('fresh not_measured observations keep links until expired or unavailable', async () => {
  const h = harness(async () => ({ok: true, json: async () => documentAt(Date.now())}));
  await flush();
  for (const [age, unavailable] of [[0, false], [31000, false], [0, true]]) {
    const map = documentAt(Date.now() - age, [{id: 'pc:lane', machine: 'pc', health: 'not_measured',
      upstream: 'http://lane'}]);
    map.edges = [
      {from: 'pc', to: 'pc:lane', kind: 'hosts'},
      {from: 'pc:router', to: 'pc:lane', kind: 'upstream'},
      {from: 'pc:lane', to: 'client', kind: 'caller'},
    ];
    h.context.fixtureMap = map;
    const result = vm.runInContext(`ageMap(fixtureMap, ${unavailable})`, h.context);
    assert.deepEqual(Array.from(result.edges, edge => edge.kind),
      age || unavailable ? ['hosts'] : ['hosts', 'upstream', 'caller']);
    assert.equal(map.edges.length, 3);
  }
});

test('a stale map cannot advertise an up tunnel', async () => {
  const h = harness(async () => ({ok: true, json: async () => documentAt(Date.now() - 31000)}));
  await flush();
  assert.equal(vm.runInContext('current.tunnel.state', h.context), 'not_measured');
  assert.doesNotMatch(h.elements.tunnel.textContent, /tunnel up/);
});

test('local expiry clears the accepting tunnel banner while a refresh is pending', async () => {
  let now = Date.now();
  class Clock extends Date { static now() { return now; } }
  let calls = 0;
  let finish;
  const map = documentAt(now);
  map.tunnel.detail = 'SSH local forwards are accepting connections.';
  const h = harness(() => {
    if (++calls === 1) return Promise.resolve({ok: true, json: async () => map});
    return new Promise(resolve => { finish = resolve; });
  }, Clock);
  await flush();
  assert.match(h.elements.tunnel.textContent, /accepting/);
  const pending = h.intervals[0]();
  now += 31000;
  await h.intervals[0]();
  assert.equal(calls, 2);
  assert.equal(vm.runInContext('current.tunnel.state', h.context), 'not_measured');
  assert.doesNotMatch(h.elements.tunnel.textContent, /accepting/);
  assert.equal(h.elements.tunnel.textContent, 'PC tunnel observation stale.');
  finish({ok: true, json: async () => documentAt(now)});
  await pending;
});

test('the feed failure banner persists through pending retries until a map is accepted', async () => {
  for (const hadMap of [false, true]) {
    let calls = 0;
    let finish;
    const h = harness(async () => {
      calls++;
      if (hadMap && calls === 1) return {ok: true, json: async () => documentAt(Date.now())};
      if (calls === (hadMap ? 2 : 1)) throw new Error('offline');
      return new Promise(resolve => { finish = resolve; });
    });
    await flush();
    if (hadMap) await h.intervals[0]();
    assert.equal(h.elements.tunnel.textContent, 'Monitor feed unavailable');
    const pending = h.intervals[0]();
    await h.intervals[0]();
    assert.equal(h.elements.tunnel.textContent, 'Monitor feed unavailable');
    finish({ok: true, json: async () => documentAt(Date.now())});
    await pending;
    assert.equal(h.elements.tunnel.textContent, 'PC tunnel up');
  }
});

test('a stalled refresh aborts and a later refresh can proceed', async () => {
  let calls = 0;
  const h = harness(async (_url, options) => {
    if (++calls === 1) return {ok: true, json: async () => documentAt(Date.now())};
    return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('timeout'))));
  });
  await flush();
  h.intervals[0]();
  h.timeouts.at(-1)();
  await flush();
  assert.equal(vm.runInContext('current.nodes[0].health', h.context), 'not_measured');
  h.intervals[0]();
  assert.equal(calls, 3);
  h.timeouts.at(-1)();
  await flush();
});

test('selection closes when its node disappears', async () => {
  let calls = 0;
  const h = harness(async () => ({ok: true, json: async () => documentAt(Date.now(), ++calls === 1
    ? [{id: 'mac:lmstudio', machine: 'mac', health: 'ok'}] : [])}));
  await flush();
  vm.runInContext("selected = 'mac:lmstudio'; show(current.nodes[0]);", h.context);
  await h.intervals[0]();
  await flush();
  assert.equal(h.elements.detail.hidden, true);
  assert.equal(vm.runInContext('selected', h.context), null);
});

test('extra nodes occupy distinct rows', () => {
  const points = place(Array.from({length: 8}, (_, id) => ({id: `caller-${id}`})));
  assert.equal(new Set(points.map(node => `${node.x},${node.y}`)).size, 8);
});

test('rates have bounded precision and invalid rates stay unknown', () => {
  assert.equal(rateText({generationTokensPerSecond: 37.123456789}), '37.1 tok/s');
  assert.equal(rateText({generationTokensPerSecond: NaN}), '');
});
