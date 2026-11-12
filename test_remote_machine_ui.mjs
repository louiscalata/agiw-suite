import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {detailRows, place} from './web/monitor.mjs';
import {mapMonitors} from './web/monitor-tabs.mjs';

const roots = [
  {id: 'mac', machine: 'mac', role: 'machine', label: 'Mac', health: 'not_measured'},
  {id: 'pc', machine: 'pc', role: 'machine', label: 'PC', health: 'ok'},
  {id: 'worker-b', machine: 'worker-b', role: 'machine', label: 'Worker B', health: 'not_measured'},
  {id: 'worker-c', machine: 'worker-c', role: 'machine', label: 'Worker C', health: 'ok'},
];
const lanes = [
  {id: 'worker-c:deep', machine: 'worker-c', role: 'llama', label: 'Deep C', health: 'ok', modelId: 'observed/model-c'},
  {id: 'worker-b:deep', machine: 'worker-b', role: 'llama', label: 'Deep B', health: 'not_measured', reason: 'timeout', modelId: null},
  {id: 'worker-b:fast', machine: 'worker-b', role: 'llama', label: 'Fast B', health: 'ok', modelId: 'observed/model-b'},
];

test('additional machine lanes stay grouped even when input interleaves machines', () => {
  const positions = place([...roots, ...lanes]);
  const byId = new Map(positions.map(node => [node.id, node]));
  assert.deepEqual([byId.get('mac').x, byId.get('mac').y], [120, 48]);
  assert.deepEqual([byId.get('pc').x, byId.get('pc').y], [460, 48]);
  const remote = positions.filter(node => node.id.startsWith('worker-'));
  assert.deepEqual(remote.map(node => node.id), [
    'worker-b', 'worker-b:deep', 'worker-b:fast', 'worker-c', 'worker-c:deep',
  ]);
  for (let index = 1; index < remote.length; index++) {
    assert.ok(remote[index].y - remote[index - 1].y >= 60);
  }
  assert.ok(byId.get('worker-b:deep').x > byId.get('worker-b').x);
});

test('map tabs retain arbitrary machine ids and keep failed identity unknown', () => {
  const map = {nodes: [...roots, ...lanes]};
  const tabs = mapMonitors(map);
  assert.deepEqual(tabs.map(tab => tab.id), ['all', ...map.nodes.map(node => node.id)]);
  const failed = tabs.find(tab => tab.id === 'worker-b:deep');
  assert.equal(failed.word, 'UNKNOWN');
  assert.equal(failed.machine, 'worker-b');
  assert.equal(Object.fromEntries(detailRows(lanes[1])).Model, '—');
  assert.equal(Object.fromEntries(detailRows(lanes[0])).Model, 'observed/model-c');
  assert.equal(mapMonitors(map, {feedUnavailable: true}).find(tab => tab.id === 'worker-c:deep').word, 'DISCONNECTED');
});

test('registry Mac and PC roots stay beside their stable production lane ids', () => {
  const configured = roots.slice(0, 3).map(node => ({...node, inventoryScope: 'configured-lanes'}));
  const production = [
    {id: 'mac:production', machine: 'mac', role: 'llama'},
    {id: 'pc:deep', machine: 'pc', role: 'llama'},
    {id: 'worker-b:deep', machine: 'worker-b', role: 'llama'},
  ];
  const placed = place([...configured, ...production]);
  assert.deepEqual(placed.map(node => node.id), ['mac', 'mac:production', 'pc', 'pc:deep', 'worker-b', 'worker-b:deep']);
  for (let index = 0; index < placed.length; index += 2) {
    assert.equal(placed[index + 1].y - placed[index].y, 60);
    assert.ok(placed[index + 1].x > placed[index].x);
  }
  // A stable registry lane may reuse an old fixed-map name; its machine still
  // controls the layout instead of stranding that lane at a legacy coordinate.
  production[1].id = 'pc:router';
  const reused = place([...configured, ...production]);
  assert.equal(reused.findIndex(node => node.id === 'pc:router'), reused.findIndex(node => node.id === 'pc') + 1);
});


// Execute the page's actual tick/aging/draw/click path against a minimal DOM.
// Markup assignments throw, so replacing a textContent write with innerHTML
// cannot silently make the escaping checks pass in this harness.
const pageSource = readFileSync(new URL('./web/monitor.mjs', import.meta.url), 'utf8').replace(/export /g, '');
const flush = () => new Promise(resolve => setImmediate(resolve));
function mapPage(fetcher, clock) {
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.style = {}; this.hidden = true; this.ownText = ''; }
    set innerHTML(_) { throw new Error('HTML insertion is forbidden for map text'); }
    get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this.ownText = String(value); this.children = []; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.ownText = ''; this.children = items; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener(name, callback) { this[name] = callback; }
    closest(tag) { return this.tagName === tag ? this : null; }
  }
  const elements = Object.fromEntries(['map', 'tunnel', 'detail', 'detailTitle', 'detailFacts'].map(id => [id, new Element(id)]));
  const intervals = [];
  const context = vm.createContext({Date: clock, AbortController, fetch: fetcher,
    setInterval: callback => { intervals.push(callback); return intervals.length; },
    setTimeout: () => 1, clearTimeout: () => {},
    document: {querySelector: selector => elements[selector.slice(1)],
      createElementNS: (_ns, tag) => new Element(tag), createElement: tag => new Element(tag)},
  });
  vm.runInContext(pageSource, context);
  return {elements, intervals, context,
    lane(id) { return elements.map.children.find(node => node.dataset.id === id); },
    select(id) { elements.map.click({target: this.lane(id)}); },
  };
}
function registryMap(at, overrides = {}) {
  return {observedAt: new Date(at).toISOString(), edges: [], nodes: [{
    id: 'registry:pc:deep', machine: 'registry:pc', role: 'llama',
    label: '<img src=x onerror=alert(1)> & Deep', health: 'ok',
    modelId: '<script>alert(1)</script> & model', observedAt: new Date(at).toISOString(),
    observationSource: 'registry', probeIntervalSeconds: 3, ageSeconds: 0, ...overrides,
  }]};
}

for (const state of ['ok', 'unknown', 'stale']) {
  test(`actual map DOM renders registry ${state} with literal labels and observation age`, async () => {
    const now = Date.parse('2026-01-01T00:00:00Z');
    class Clock extends Date { static now() { return now; } }
    const map = registryMap(now - (state === 'stale' ? 6500 : 500), state === 'unknown'
      ? {health: 'not_measured', reason: 'timeout', modelId: null} : {});
    const h = mapPage(async () => ({ok: true, json: async () => map}), Clock);
    await flush();
    const lane = h.lane('registry:pc:deep');
    assert.equal(lane.children[1].tagName, 'text');
    assert.equal(lane.children[1].textContent, '<img src=x onerror=alert(1)> & Deep');
    assert.equal(lane.children[1].children.length, 0);
    const sub = lane.children[2].textContent;
    assert.match(sub, state === 'ok' ? /OK/ : state === 'unknown' ? /UNKNOWN/ : /STALE/);
    if (state === 'ok') assert.match(sub, /<script>alert\(1\)<\/script> & model/);
    else assert.doesNotMatch(sub, /script|model/);
    h.select('registry:pc:deep');
    assert.equal(h.elements.detailTitle.textContent, '<img src=x onerror=alert(1)> & Deep');
    assert.match(h.elements.detailFacts.textContent, /Observation age(?:0.5|6.5) s/);
    assert.equal(h.elements.detailFacts.children.every(child => child.children.length === 0), true);
  });
}

test('actual registry DOM ages while a refresh stalls, using its interval instead of the peer expiry', async () => {
  let now = Date.parse('2026-01-01T00:00:00Z');
  class Clock extends Date { static now() { return now; } }
  let calls = 0;
  let complete;
  const map = registryMap(now, {machine: 'pc', probeIntervalSeconds: 4});
  const h = mapPage(() => ++calls === 1 ? Promise.resolve({ok: true, json: async () => map})
    : new Promise(resolve => { complete = resolve; }), Clock);
  await flush();
  h.select('registry:pc:deep');
  assert.match(h.lane('registry:pc:deep').textContent, /OK/);
  const pending = h.intervals[0]();
  now += 7500;
  await h.intervals[0]();
  assert.match(h.lane('registry:pc:deep').textContent, /OK/);
  assert.match(h.elements.detailFacts.textContent, /Observation age7.5 s/);
  now += 501;
  await h.intervals[0]();
  assert.match(h.lane('registry:pc:deep').textContent, /STALE/);
  assert.doesNotMatch(h.lane('registry:pc:deep').textContent, /script|model/);
  assert.match(h.elements.detailFacts.textContent, /Observation age8.0 s/);
  assert.equal(calls, 2);
  complete({ok: true, json: async () => map});
  await pending;
});

test('registry pending observation does not borrow a fresh snapshot timestamp', async () => {
  const now = Date.parse('2026-01-01T00:00:00Z');
  class Clock extends Date { static now() { return now; } }
  const map = registryMap(now, {observedAt: null, health: 'not_measured', modelId: null, reason: 'pending'});
  const h = mapPage(async () => ({ok: true, json: async () => map}), Clock);
  await flush();
  h.select('registry:pc:deep');
  assert.match(h.lane('registry:pc:deep').textContent, /UNKNOWN/);
  assert.match(h.elements.detailFacts.textContent, /Observation age—/);
});
