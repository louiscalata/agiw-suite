import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {detailRows, place} from './web/monitor.mjs';
import * as fleet from './web/fleet-state.mjs';
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

const shared = JSON.parse(readFileSync(new URL('./test_support/fleet-state.json', import.meta.url), 'utf8'));
test('map tabs retain exactly the shared fleet IDs and endpoint state words', () => {
  const map = {fleetState: shared.fleetState, nodes: [...roots, ...lanes]};
  const tabs = mapMonitors(map, {now: shared.now});
  assert.deepEqual(tabs.map(tab => tab.id), ['all', ...shared.fleetState.facts.map(fact => fact.id)]);
  assert.equal(tabs.find(tab => tab.id === 'registry:pro:deep').word, 'UNKNOWN');
  assert.equal(tabs.find(tab => tab.id === 'registry:pc:deep').word, 'ENDPOINT ANSWERING');
  assert.equal(Object.fromEntries(detailRows(lanes[1])).Model, '—');
  assert.equal(Object.fromEntries(detailRows(lanes[0])).Model, 'observed/model-c');
  assert.equal(mapMonitors(map, {now: shared.now, feedUnavailable: true}).find(tab => tab.id === 'registry:pc:deep').word, 'ENDPOINT ANSWERING');
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
const pageSource = readFileSync(new URL('./web/monitor.mjs', import.meta.url), 'utf8').replace(/^import .*;\r?\n/gm, '').replace(/export /g, '');
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
  const context = vm.createContext({...fleet, Date: clock, AbortController, fetch: fetcher,
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
  const document=JSON.parse(JSON.stringify(shared.fleetState)),delta=at/1000-shared.now;
  for(const fact of document.facts)for(const key of ['observedAt','expiresAt'])if(fact[key])fact[key]=new Date(Date.parse(fact[key])+delta*1000).toISOString();
  if(Object.keys(overrides).length){const lane=document.facts.find(fact=>fact.id==='registry:pc:deep');Object.assign(lane,overrides);if(overrides.evidence)lane.evidence={...lane.evidence,...overrides.evidence};}
  return {observedAt:new Date(at).toISOString(),fleetState:document,edges:[],nodes:[]};
}

test('actual map DOM uses the shared fleet membership and source-scoped state words',async()=>{
  const now=shared.now*1000;class Clock extends Date{static now(){return now;}}
  const h=mapPage(async()=>({ok:true,json:async()=>registryMap(now)}),Clock);await flush();
  assert.deepEqual(h.elements.map.children.filter(node=>node.dataset.id).map(node=>node.dataset.id),
    shared.fleetState.facts.filter(fact=>fact.scope==='machine').flatMap(root=>[root.id,...shared.fleetState.facts.filter(fact=>fact.parentId===root.id).map(fact=>fact.id)]));
  assert.match(h.lane('registry:pc:deep').textContent,/ENDPOINT ANSWERING/);
  assert.match(h.lane('registry:pro').textContent,/UNKNOWN/);
  assert.match(h.lane('peer:spare').textContent,/REPORTED/);
});

test('actual map DOM keeps reported model paths and untrusted labels as literal text',async()=>{
  const now=shared.now*1000;class Clock extends Date{static now(){return now;}}
  const full='C:\\models\\Glimmer.gguf',label='<img src=x> & Deep';
  const map=registryMap(now,{label,evidence:{modelId:full}}),h=mapPage(async()=>({ok:true,json:async()=>map}),Clock);await flush();
  const lane=h.lane('registry:pc:deep');assert.equal(lane.children[1].textContent,label);assert.equal(lane.children[1].children.length,0);
  assert.equal(lane.children[2].textContent,'reported Glimmer.gguf');assert.equal(lane.children[2].attributes.title,full);
  assert.match(lane.children[3].textContent,/ENDPOINT ANSWERING.*1.0 s ago/);
  h.select('registry:pc:deep');assert.equal(h.elements.detailTitle.textContent,label);
  assert.equal(h.elements.detailFacts.children.every(child=>child.children.length===0),true);
  assert.match(h.elements.detailFacts.textContent,/Last probe/);
});

test('actual fleet DOM ages independent facts while a refresh stalls',async()=>{
  let now=shared.now*1000;class Clock extends Date{static now(){return now;}}
  let calls=0,complete;const map=registryMap(now);
  const h=mapPage(()=>++calls===1?Promise.resolve({ok:true,json:async()=>map}):new Promise(resolve=>{complete=resolve;}),Clock);await flush();
  h.select('registry:pc:deep');assert.match(h.lane('registry:pc:deep').textContent,/ENDPOINT ANSWERING/);
  const pending=h.intervals[0]();now+=4000;await h.intervals[0]();
  assert.match(h.lane('mac:lmstudio').textContent,/STALE.*5.0 s ago/);
  assert.match(h.lane('registry:pc:deep').textContent,/ENDPOINT ANSWERING/);
  assert.match(h.lane('peer:spare').textContent,/REPORTED/);
  now+=2000;await h.intervals[0]();assert.match(h.lane('registry:pc:deep').textContent,/STALE.*7.0 s ago/);
  assert.match(h.elements.detailFacts.textContent,/Observation age7.0 s/);assert.equal(calls,2);
  complete({ok:true,json:async()=>map});await pending;
});

test('a pending fleet lane does not borrow the enclosing snapshot clock',async()=>{
  const now=shared.now*1000;class Clock extends Date{static now(){return now;}}
  const h=mapPage(async()=>({ok:true,json:async()=>registryMap(now)}),Clock);await flush();h.select('registry:pro:deep');
  assert.match(h.lane('registry:pro:deep').textContent,/UNKNOWN.*Not observed/);
  assert.match(h.elements.detailFacts.textContent,/Observation age—/);
  assert.match(h.elements.detailFacts.textContent,/Configured endpointhttp:\/\/127.0.0.1:21003/);
});
