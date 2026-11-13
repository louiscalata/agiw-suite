import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as fleet from './web/fleet-state.mjs';
import {place, rateText} from './web/monitor.mjs';

const source = readFileSync(new URL('./web/monitor.mjs', import.meta.url), 'utf8').replace(/^import .*;\r?\n/gm, '').replace(/export /g, '');
const documentAt = (at, nodes = [{id: 'mac:lmstudio', machine: 'mac', label: 'LM Studio',
  health: 'ok', modelId: 'qwen', generationTokensPerSecond: 80}]) => ({
  observedAt: new Date(at).toISOString(), nodes: nodes.map(node => ({observedAt: new Date(at).toISOString(), ...node})),
  fleetState: {schemaVersion: 1, facts: nodes.map(node => ({id: node.id, label: node.label || node.id, scope: node.role === 'machine' ? 'machine' : 'lane', parentId: node.machine || null, source: 'inference endpoint', observedAt: new Date(at).toISOString(), expiresAt: new Date(at + (node.machine === 'pc' ? 30000 : 3000)).toISOString(), state: node.health === 'ok' ? 'endpoint answering' : 'unknown', completeness: 'complete', evidence: node}))},
  edges: [], tunnel: {state: 'up'},
});

function harness(map = null, clock = Date) {
  class Element {
    constructor() { this.children = []; this.attributes = {}; this.dataset = {}; this.style = {}; this.hidden = true; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener(name, callback) { this[name] = callback; }
  }
  const elements = Object.fromEntries(['map', 'tunnel', 'detail', 'detailTitle', 'detailFacts'].map(id => [id, new Element()]));
  let listener;
  const starts = [];
  const state = (inference, error = null) => ({inference, inferenceStatus: {error}});
  const store = {subscribe(callback) { listener = callback; callback(state(map)); return () => {}; },
    start(options) { starts.push(options); }};
  const context = vm.createContext({Date: clock, URL, console, ...fleet,
    getMonitorStore: () => store,
    fetch: () => { throw new Error('consumer must read the store'); },
    setInterval: () => { throw new Error('consumer must use store ageing'); },
    document: {querySelector: selector => elements[selector.slice(1)],
      createElementNS: () => new Element(), createElement: () => new Element()},
  });
  vm.runInContext(source, context);
  return {elements, context, starts, publish(inference, error) { listener(state(inference, error)); }};
}

test('the map requests the shared inference feed once without its own transport', () => {
  const h = harness();
  assert.equal(h.starts.length, 1);
  assert.equal(h.starts[0].inference, true);
  assert.equal(h.elements.tunnel.textContent, 'Monitor feed unavailable');
});

test('a feed failure retains fresh endpoint facts and reports the unavailable feed', () => {
  const map = documentAt(Date.now()), h = harness(map);
  vm.runInContext("selected = 'mac:lmstudio'; show(current.nodes[0]);", h.context);
  h.publish(map, {code: 'request_failed'});
  const node = vm.runInContext('current.nodes[0]', h.context);
  assert.equal(node.health, 'ok');
  assert.equal(node.generationTokensPerSecond, 80);
  assert.equal(node.factState, 'endpoint answering', 'transport failure does not change a fact expiry');
  assert.equal(node.modelId, 'qwen');
  assert.equal(h.elements.detailFacts.children.some(item => String(item.textContent).includes('80')), true);
  assert.equal(h.elements.tunnel.textContent, 'Monitor feed unavailable');
});

test('a retained response with old timestamps cannot look healthy', () => {
  const h = harness(documentAt(Date.now() - 31000));
  assert.equal(vm.runInContext('current.nodes[0].health', h.context), 'not_measured');
});

test('canonical map retains membership while unsupported dynamic edges stay absent', () => {
  const h = harness();
  for (const [age, unavailable] of [[0, false], [31000, false], [0, true]]) {
    const map = documentAt(Date.now() - age, [
      {id: 'pc', role: 'machine', machine: 'pc', health: 'not_measured'},
      {id: 'pc:lane', machine: 'pc', health: 'not_measured', upstream: 'http://lane'},
    ]);
    map.edges = [{from: 'pc', to: 'pc:lane', kind: 'hosts'},
      {from: 'pc:router', to: 'pc:lane', kind: 'upstream'}, {from: 'pc:lane', to: 'client', kind: 'caller'}];
    h.context.fixtureMap = map;
    const result = vm.runInContext(`ageMap(fixtureMap, ${unavailable})`, h.context);
    assert.deepEqual(Array.from(result.edges, edge => edge.kind), ['hosts']);
    assert.equal(result.nodes.length, 2);
    assert.equal(map.edges.length, 3);
  }
});

test('a stale map cannot advertise an up tunnel', () => {
  const h = harness(documentAt(Date.now() - 31000));
  assert.equal(vm.runInContext('current.tunnel.state', h.context), 'not_measured');
  assert.doesNotMatch(h.elements.tunnel.textContent, /tunnel up/);
});

test('store ageing clears the accepting tunnel while a request has no response', () => {
  let now = Date.now();
  class Clock extends Date { static now() { return now; } }
  const map = documentAt(now);
  map.tunnel.detail = 'SSH local forwards are accepting connections.';
  const h = harness(map, Clock);
  assert.match(h.elements.tunnel.textContent, /accepting/);
  now += 31000;
  h.publish(map);
  assert.equal(vm.runInContext('current.tunnel.state', h.context), 'not_measured');
  assert.doesNotMatch(h.elements.tunnel.textContent, /accepting/);
  assert.equal(h.elements.tunnel.textContent, 'PC tunnel observation stale.');
});

test('failure stays visible on ageing emissions and clears on recovery', () => {
  for (const hadMap of [false, true]) {
    const map = documentAt(Date.now()), h = harness(hadMap ? map : null);
    h.publish(hadMap ? map : null, {code: 'deadline'});
    assert.equal(h.elements.tunnel.textContent, 'Monitor feed unavailable');
    h.publish(hadMap ? map : null, {code: 'deadline'});
    assert.equal(h.elements.tunnel.textContent, 'Monitor feed unavailable');
    h.publish(map);
    assert.equal(h.elements.tunnel.textContent, 'PC tunnel up');
  }
});

test('selection closes when the shared store removes its node', () => {
  const h = harness(documentAt(Date.now()));
  vm.runInContext("selected = 'mac:lmstudio'; show(current.nodes[0]);", h.context);
  h.publish(documentAt(Date.now(), []));
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
