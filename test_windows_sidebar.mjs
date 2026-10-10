import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import * as layout from './web/map-layout.mjs';
import * as usage from './usage-format.mjs';
import * as onlineMode from './web/online-code-mode.mjs';
import * as modelControl from './web/model-control-view.mjs';

const now = 1700000000;
const peer = (over = {}) => ({state: 'linked', via: 'sharedchami', ageSeconds: 2,
  linkAgeSeconds: 2, name: 'louis-m5-pro-macbook-pro', loadedCount: 3,
  models: [{id: 'qwen/qwen3.8-27b', state: 'loaded'},
    {id: 'google/gemma-4-26b-a4b-qat', state: 'loaded'},
    {id: 'openai/gpt-oss-20b', state: 'loaded'}], ...over});
const viewFor = (raw = peer(), options = {}) => layout.macPeerView(raw,
  {feedFresh: true, snapshotAge: 0, ...options});
const entries = (view, query = '') => {
  assert.equal(typeof layout.windowsMacListEntries, 'function', 'the Windows list helper is exported');
  return layout.windowsMacListEntries(view, query);
};
const keys = rows => Array.from(rows, row => row.key);
const text = rows => rows.map(row => `${row.title} ${row.subtitle}`).join(' ');
const causeClaim = /offline|asleep|LM Studio (?:is )?(?:closed|not[- ]listening)|firewall (?:is )?(?:blocking|dropping)|\bMac (?:is )?on (?:the )?(?:network|LAN)\b/i;

test('linked Mac sidebar entries use the map satellite order and attribute loaded models to an unverified report', () => {
  const view = viewFor(), rows = entries(view);
  assert.deepEqual(keys(rows), ['mac-peer', 'mac-model:0', 'mac-model:1']);
  assert.equal(rows[0].title, 'Mac');
  assert.match(rows[0].subtitle, /reported.*(?:share|SharedChami).*unverified/i);
  assert.equal(rows[0].tone, 'muted');
  assert.match(rows[1].title, /qwen3\.8.*27b/i);
  assert.match(rows[2].title, /gemma.*4.*26b/i);
  for (const row of rows.slice(1)) {
    assert.match(row.subtitle, /reported loaded.*Mac/i);
    assert.doesNotMatch(row.subtitle, /LM Studio|generating|busy|active/i);
  }
});

test('a proven LAN listing attributes the same sidebar satellites to LM Studio inventory', () => {
  const rows = entries(viewFor(peer({state: 'reachable', via: 'mdns', latencyMs: 4})));
  assert.deepEqual(keys(rows), ['mac-peer', 'mac-model:0', 'mac-model:1']);
  assert.equal(rows[0].tone, 'ok');
  assert.match(rows[0].subtitle, /reachable|answering/i);
  for (const row of rows.slice(1)) {
    assert.match(row.subtitle, /listed loaded.*LM Studio/i);
    assert.doesNotMatch(row.subtitle, /reported loaded|generating|busy|active/i);
  }
});

test('refusal and timeout remain distinct in the sidebar without invented cause or model residency', () => {
  for (const [reason, diagnosis] of [['refused', /refused/i], ['timeout', /no answer|timed out|timeout/i]]) {
    const rows = entries(viewFor(peer({state: 'unreachable', reason})));
    assert.deepEqual(keys(rows), ['mac-peer']);
    assert.equal(rows[0].tone, 'warn');
    assert.match(rows[0].subtitle, diagnosis);
    assert.doesNotMatch(text(rows), causeClaim);
    assert.doesNotMatch(text(rows), /\d+ loaded|generating|busy|active/i);
  }
});

test('expired share, expired LAN, and disconnected feeds keep the Mac entry and remove model entries', () => {
  for (const [raw, options] of [[peer({ageSeconds: 29}), {snapshotAge: 2}],
    [peer({state: 'reachable', ageSeconds: 44}), {snapshotAge: 2}],
    [peer(), {feedFresh: false}],
    [peer({state: 'unreachable', ageSeconds: 46, reason: 'refused'}), {}]]) {
    const rows = entries(viewFor(raw, options));
    assert.deepEqual(keys(rows), ['mac-peer']);
    assert.equal(rows[0].tone, 'muted');
    assert.match(rows[0].subtitle, /unknown|stale|pending/i);
    assert.doesNotMatch(text(rows), /refused|\d+ loaded|reported loaded|LM Studio/i);
  }
});

test('malformed and partial raw evidence cannot invent a loaded Mac sidebar model', () => {
  for (const raw of [null, [], 'Mac', {state: 'bogus', ageSeconds: 0},
    peer({models: []}), peer({models: [{id: 'x', state: 'unknown'}]}),
    peer({models: [{id: 'x', state: 'listed'}]}),
    peer({models: [{id: 'bad\nid', state: 'loaded'}, {id: 'a b', state: 'loaded'}]})]) {
    const rows = entries(viewFor(raw));
    assert.deepEqual(keys(rows), ['mac-peer']);
    assert.doesNotMatch(text(rows), /reported loaded on|listed loaded by|0 loaded|generating/i);
  }
});

test('sidebar search discovers Mac, LAN peer, and configured host names even when evidence is stale', () => {
  const view = viewFor();
  for (const query of ['mac', 'lan peer', 'louis-m5-pro-macbook-pro']) {
    assert.ok(keys(entries(view, query)).includes('mac-peer'), `Mac discovered by ${query}`);
  }
  const stale = viewFor(peer(), {feedFresh: false});
  assert.deepEqual(keys(entries(stale, 'louis-m5-pro-macbook-pro')), ['mac-peer']);
  assert.match(text(entries(stale, 'louis-m5-pro-macbook-pro')), /stale|unknown/i);
});

test('search by a model ID or friendly title preserves the original map satellite index', () => {
  const view = viewFor();
  for (const query of ['google/gemma-4-26b-a4b-qat', 'Gemma 4 26b']) {
    const rows = entries(view, query);
    assert.ok(keys(rows).includes('mac-model:1'), `second satellite discovered by ${query}`);
    assert.ok(!keys(rows).includes('mac-model:0'), 'the filtered model is not assigned a new map key');
  }
  assert.deepEqual(entries(view, 'model-that-does-not-exist'), []);
});

test('configured host search accepts only a bounded plain hostname without changing evidence freshness', () => {
  assert.equal(viewFor(peer({name: '  louis-m5-pro-macbook-pro  '})).name, 'louis-m5-pro-macbook-pro');
  for (const name of [null, [], ['louis-m5-pro-macbook-pro'], {name: 'louis-m5-pro-macbook-pro'},
    true, 1, '', ' ', 'bad\nhost', 'bad\u007fhost', 'x'.repeat(254)]) {
    const view = viewFor(peer({name}));
    assert.equal(view.name, null);
    assert.deepEqual(entries(view, 'louis-m5-pro-macbook-pro'), []);
    assert.equal(view.state, 'linked', 'invalid search metadata does not discard valid share evidence');
  }
});

// Exercise the actual map and sidebar functions without polling, SVG painting,
// or browser control. Selection is the only external UI effect replaced here.
const definitions = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8')
  .replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];

class Element {
  constructor(document, tag) {
    Object.assign(this, {document, tagName: tag.toUpperCase(), children: [], parentNode: null,
      dataset: {}, attributes: {}, listeners: {}, className: '', ownText: '', scrollTop: 0});
    this.classList = {add: (...names) => { this.className += ' ' + names.join(' '); },
      toggle: (name, force) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        if (force ?? !names.has(name)) names.add(name); else names.delete(name);
        this.className = [...names].join(' ');
      }};
  }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) {
    for (const child of this.children) {
      if (child.contains(this.document.activeElement)) this.document.activeElement = this.document.body;
      child.parentNode = null;
    }
    this.children = []; this.ownText = ''; this.scrollTop = 0; this.append(...nodes);
  }
  get textContent() { return this.ownText + this.children.map(node => node.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this.ownText = String(value); }
  set innerHTML(_) { throw new Error('sidebar labels must be rendered as literal text'); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  contains(node) { for (let p = node; p; p = p.parentNode) if (p === this) return true; return false; }
  matches(selector) { return selector.startsWith('.') ? this.className.split(/\s+/).includes(selector.slice(1))
    : this.tagName === selector.toUpperCase(); }
  closest(selector) { for (let p = this; p; p = p.parentNode) if (p.matches(selector)) return p; return null; }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  click() { for (const listener of this.listeners.click || []) listener({target: this}); }
  focus() { this.document.activeElement = this; }
}

function sidebar(options = {}) {
  class Clock extends Date { static now() { return now * 1000; } }
  const document = {elements: new Map()};
  document.body = new Element(document, 'body'); document.activeElement = document.body;
  document.createElement = tag => new Element(document, tag);
  document.createElementNS = (_, tag) => new Element(document, tag);
  document.createTextNode = value => { const n = new Element(document, '#text'); n.textContent = value; return n; };
  document.getElementById = id => {
    if (!document.elements.has(id)) {
      const n = new Element(document, 'div'); n.id = id; document.body.append(n); document.elements.set(id, n);
    }
    return document.elements.get(id);
  };
  const region = document.getElementById('graphRegion'); region.clientWidth = 820; region.clientHeight = 604;
  const api = runInNewContext(`${definitions}
    ;({draw(data, options) {
      snapshot=data; connected=options.connected; paused=options.paused; view=options.view;
      query=options.query; selected=options.selected; makeGraph(); renderList();
    }, refresh() { makeGraph(); renderList(); },
    mapKeys() { return graph.nodes.filter(n=>n.kind==='mac-peer'||n.kind==='mac-model').map(n=>n.id); },
    mapNodes() { return graph.nodes; }, pcSummary() { return vitalsInput().vitals.mac; },
    paintClasses() {
      renderGlows=()=>{}; renderWeb=()=>null; applyCamera=()=>{};
      renderModelAction=()=>{}; syncWebPulse=()=>{}; syncBeat=()=>{};
      renderGraph();
      return Object.fromEntries([...nodeElements].map(([key,node])=>[key,node.getAttribute('class')]));
    },
    inspect(key, body) { const node=graph.nodes.find(n=>n.id===key); if(!node)throw Error('missing map node '+key); return inspectorBody(node,body); },
    spySelection() { const picked=[]; nodeSelect=id=>{picked.push(id); selected=id; renderList();}; return picked; }})`,
  {...layout, ...usage, ...onlineMode, ...modelControl, document, Date: Clock});
  const host = options.host || 'windows';
  const feed = {schemaVersion: 1, host, sampledAt: now - (options.snapshotAge || 0),
    fullSampledAt: now - (options.feedAge || 0), activityKnown: true,
    models: [{id: 'openai/gpt-oss-20b', host, state: 'idle', loaded: true, ageSeconds: 0}],
    macPeer: options.macPeer === undefined ? peer() : options.macPeer,
    hostId: options.hostId || 'louis-windows-pc',
    components: options.components || [], clients: options.clients || [],
    sources: options.sources || [], pipeline: options.pipeline || {status: 'idle'},
    activity: {runs: options.runs || []}};
  api.draw(feed, {connected: options.connected !== false, paused: options.paused === true,
    view: options.view || 'runtime', query: options.query || '', selected: options.selected || null});
  return {api, document, list: document.getElementById('list')};
}

const buttons = list => list.children.filter(node => node.tagName === 'BUTTON');
const macButtons = list => buttons(list).filter(node => /^mac-(?:peer|model:)/.test(node.dataset.key));
const sectionLabels = list => list.children.filter(node => node.className === 'list-section').map(node => node.textContent);

test('the actual Windows runtime sidebar places Mac peer after local runtime with the map keys and order', () => {
  for (const state of ['linked', 'reachable']) {
    const {api, list} = sidebar({macPeer: peer({state})});
    const labels = sectionLabels(list);
    assert.equal(labels[0], 'Local runtime');
    assert.equal(labels[1], 'Mac peer');
    assert.deepEqual(macButtons(list).map(node => node.dataset.key), Array.from(api.mapKeys()));
    assert.deepEqual(macButtons(list).map(node => node.dataset.key), ['mac-peer', 'mac-model:0', 'mac-model:1']);
    const nextSection = list.children.findIndex(node => node.textContent === 'Route advisor · outside Core 6');
    assert.ok(list.children.indexOf(macButtons(list).at(-1)) < nextSection, 'Mac section precedes the existing advisors');
  }
});

test('actual sidebar Mac items select their existing map keys and preserve focus and scroll on refresh', () => {
  const {api, document, list} = sidebar({selected: 'mac-model:1'});
  const picked = api.spySelection();
  for (const key of ['mac-peer', 'mac-model:0', 'mac-model:1']) {
    const item = macButtons(list).find(node => node.dataset.key === key);
    assert.ok(item, `selectable ${key}`); item.click();
    const selected = macButtons(list).find(node => node.dataset.key === key);
    assert.equal(selected.getAttribute('aria-pressed'), 'true');
    assert.match(selected.className, /\bselected\b/);
  }
  assert.deepEqual(Array.from(picked), ['mac-peer', 'mac-model:0', 'mac-model:1']);
  macButtons(list).at(-1).querySelector('strong').focus(); list.scrollTop = 239;
  api.refresh();
  assert.equal(document.activeElement.dataset.key, 'mac-model:1');
  assert.ok(list.contains(document.activeElement));
  assert.equal(list.scrollTop, 239);
});

test('actual Windows search finds Mac and model identities while retaining the second satellite key', () => {
  for (const query of ['mac', 'lan peer', 'louis-m5-pro-macbook-pro']) {
    const {list} = sidebar({query});
    assert.ok(macButtons(list).some(node => node.dataset.key === 'mac-peer'), `Mac found by ${query}`);
    assert.doesNotMatch(list.textContent, /No Core 6 match|Choose All discovered/);
  }
  for (const query of ['google/gemma-4-26b-a4b-qat', 'gemma 4 26b']) {
    const {list} = sidebar({query});
    assert.ok(macButtons(list).some(node => node.dataset.key === 'mac-model:1'));
    assert.ok(!macButtons(list).some(node => node.dataset.key === 'mac-model:0'));
  }
});

test('actual Windows sidebar removes loaded models and old diagnoses when share, probe, or feed evidence expires', () => {
  for (const options of [{macPeer: peer({ageSeconds: 29}), snapshotAge: 2},
    {macPeer: peer({state: 'reachable', ageSeconds: 44}), snapshotAge: 2},
    {feedAge: 4}, {connected: false},
    {macPeer: peer({state: 'unreachable', reason: 'refused', ageSeconds: 46})}]) {
    const {api, list} = sidebar({...options, query: 'mac'});
    assert.deepEqual(macButtons(list).map(node => node.dataset.key), ['mac-peer']);
    assert.deepEqual(Array.from(api.mapKeys()), ['mac-peer']);
    assert.match(macButtons(list)[0].textContent, /unknown|stale|pending/i);
    assert.doesNotMatch(macButtons(list)[0].textContent, /refused|loaded|LM Studio|generating/i);
  }
});

test('the actual Mac runtime sidebar retains its local roster and has no Windows Mac-peer section', () => {
  const {api, list} = sidebar({host: 'mac'});
  assert.deepEqual(Array.from(api.mapKeys()), []);
  assert.deepEqual(macButtons(list), []);
  assert.ok(!sectionLabels(list).includes('Mac peer'));
  assert.ok(buttons(list).some(node => node.dataset.key === 'model:mac:openai/gpt-oss-20b'));
  assert.ok(buttons(list).some(node => node.dataset.key === 'jev'));
  assert.ok(buttons(list).some(node => node.dataset.key === 'afm'));
});

test('the actual Windows runs sidebar retains route records and does not add Mac peer entries', () => {
  const run = {runId: 'run-example', source: 'router-archive', status: 'RESPONSE_VALIDATED', calls: []};
  const {list} = sidebar({view: 'runs', runs: [run]});
  assert.deepEqual(macButtons(list), []);
  assert.ok(!sectionLabels(list).includes('Mac peer'));
  assert.deepEqual(buttons(list).map(node => node.dataset.key), ['router-archive:run-example']);
  assert.match(list.textContent, /run-example|runexample/);
});

test('Windows empty search wording does not advertise the hidden Mac model-scope control', () => {
  const windows = sidebar({query: 'missing-model'}).list;
  assert.match(windows.textContent, /nothing matches|no (?:items|models|match|results)/i);
  assert.doesNotMatch(windows.textContent, /Core 6|All discovered/i);
  const mac = sidebar({host: 'mac', query: 'missing-model'}).list;
  assert.match(mac.textContent, /No Core 6 match\. Choose All discovered/);
});

const mapEntries = (nodes, options = {}) => {
  assert.equal(typeof layout.windowsMapListEntries, 'function', 'the Windows extra-map list helper is exported');
  return layout.windowsMapListEntries(nodes, options);
};
const nodeRows = api => JSON.parse(JSON.stringify(api.mapNodes()));
const identityClients = [
  {id: 'codex', model: 'org/gpt-6-sol', modelState: 'observed', source: 'session record',
    models: [{id: 'org/gpt-6-sol', modelState: 'observed', source: 'session record'},
      {id: 'org/review-identity-v2', modelState: 'configured', source: 'saved session'},
      {id: 'org/mystery-identity', modelState: 'bogus', activity: 'generating'}]},
  {id: 'claude', models: [{id: 'org/review-identity-v2', modelState: 'observed'},
    {id: 'org/review-identity-v2', modelState: 'configured'}, {id: null}, null]},
];
const childButtons = list => buttons(list).filter(node => node.dataset.key.startsWith('client-model:'));

test('Windows extra-map entries reuse existing hub and route keys, summary source, and bounded host search', () => {
  const {api} = sidebar(), nodes = nodeRows(api);
  const pcSummary = {detail: 'Quiet · 1 loaded', tone: 'ok'};
  const rows = mapEntries(nodes, {host: 'windows', hostname: 'louis-windows-pc', pcSummary});
  assert.deepEqual(keys(rows), ['runtime', 'pipeline']);
  assert.equal(rows[0].title, 'This PC'); assert.equal(rows[0].subtitle, pcSummary.detail);
  assert.equal(rows[0].tone, 'ok');
  assert.match(rows[1].title, /Nisi Inference/); assert.match(rows[1].subtitle, /route idle/i);
  for (const query of ['this pc', 'pc', 'windows', 'hub', 'louis-windows-pc'])
    assert.deepEqual(keys(mapEntries(nodes, {host: 'windows', query, hostname: 'louis-windows-pc', pcSummary})), ['runtime']);
  for (const query of ['nisi', 'inference', 'route'])
    assert.deepEqual(keys(mapEntries(nodes, {host: 'windows', query})), ['pipeline']);
  assert.deepEqual(mapEntries(nodes), [], 'the default non-Windows host cannot get Windows entries');
  for (const host of ['mac', null, ['windows'], 'WINDOWS'])
    assert.deepEqual(mapEntries(nodes, {host}), []);
  for (const malformed of [null, {}, 'nodes', true])
    assert.deepEqual(mapEntries(malformed, {host: 'windows'}), []);
});

test('extra-map route subtitles preserve stale unknown evidence and mark paused running history', () => {
  const running = sidebar({pipeline: {status: 'running', stage: 'verification'}});
  const normal = mapEntries(nodeRows(running.api), {host: 'windows', query: 'nisi'});
  assert.equal(normal[0].subtitle, 'Route running · verification');
  const paused = mapEntries(nodeRows(running.api), {host: 'windows', query: 'nisi', paused: true});
  assert.match(paused[0].subtitle, /^At pause: Route running · verification$/);
  for (const options of [{feedAge: 4}, {connected: false}]) {
    const {api} = sidebar({...options, pipeline: {status: 'running', stage: 'verification'}});
    const rows = mapEntries(nodeRows(api), {host: 'windows', query: 'route'});
    assert.match(rows[0].subtitle, /route age unknown/i);
    assert.doesNotMatch(rows[0].subtitle, /running|verification|idle/i);
  }
  const unknown = mapEntries(nodeRows(sidebar({pipeline: {status: 'unknown'}}).api),
    {host: 'windows', query: 'nisi'});
  assert.match(unknown[0].subtitle, /unknown/i);
});

test('extra-map client search preserves original keys and identifies saved or recorded models without residency claims', () => {
  const {api} = sidebar({clients: identityClients}), nodes = nodeRows(api);
  assert.ok(!mapEntries(nodes, {host: 'windows'}).some(row => row.key.startsWith('client-model:')));
  for (const query of ['org/review-identity-v2', 'review identity v2']) {
    const rows = mapEntries(nodes, {host: 'windows', query});
    assert.deepEqual(keys(rows), ['client-model:codex:1', 'client-model:claude:0']);
    assert.deepEqual(rows.map(row => row.clientId), ['codex', 'claude']);
    assert.deepEqual(rows.map(row => row.subtitle), ['Saved session choice', 'Recorded model']);
    assert.ok(rows.every(row => row.tone === 'muted'));
    assert.doesNotMatch(text(rows), /loaded|resident|generating|busy|active|\d+ subagents/i);
  }
  assert.deepEqual(mapEntries(nodes, {host: 'windows', query: 'codex'}), [],
    'matching the client label alone does not expand all of its children');
  const unknown = mapEntries(nodes, {host: 'windows', query: 'mystery identity'});
  assert.deepEqual(keys(unknown), ['client-model:codex:2']);
  assert.equal(unknown[0].subtitle, 'Model unknown');
  const placeholder = mapEntries(nodes, {host: 'windows', query: 'unknown model'});
  assert.ok(keys(placeholder).includes('client-model:cursor:unknown'));
  assert.ok(placeholder.every(row => row.subtitle === 'Model unknown' && row.tone === 'muted'));
});

test('actual Windows hub and Nisi searches select their existing nodes using the current summary and route source', () => {
  for (const [query, key] of [['this pc', 'runtime'], ['hub', 'runtime'], ['louis-windows-pc', 'runtime'],
    ['nisi', 'pipeline'], ['inference', 'pipeline'], ['route', 'pipeline']]) {
    const {api, list} = sidebar({query, sources: [{id: 'lane-inventory', state: 'live'}]});
    const item = buttons(list).find(node => node.dataset.key === key);
    assert.ok(item, `${query} finds ${key}`);
    assert.ok(nodeRows(api).some(node => node.id === item.dataset.key));
    if (key === 'runtime') assert.equal(item.querySelector('small').textContent, api.pcSummary().detail);
    else assert.match(item.querySelector('small').textContent, /route idle/i);
    const picked = api.spySelection(); item.click(); assert.deepEqual(Array.from(picked), [key]);
    assert.equal(buttons(list).find(node => node.dataset.key === key).getAttribute('aria-pressed'), 'true');
  }
  const stale = sidebar({query: 'nisi', feedAge: 4, pipeline: {status: 'running'}}).list;
  assert.match(stale.textContent, /route age unknown/i); assert.doesNotMatch(stale.textContent, /running/i);
  const paused = sidebar({query: 'nisi', paused: true, pipeline: {status: 'running'}}).list;
  assert.match(paused.textContent, /At pause: Route running/i);
});

test('actual friendly model search retains each parent and puts the matched identity under the correct client', () => {
  for (const query of ['org/review-identity-v2', 'review identity v2']) {
    const {api, list} = sidebar({clients: identityClients, query});
    const children = childButtons(list);
    assert.deepEqual(children.map(node => node.dataset.key), ['client-model:codex:1', 'client-model:claude:0']);
    for (const [client, childKey] of [['codex', 'client-model:codex:1'], ['claude', 'client-model:claude:0']]) {
      const parent = buttons(list).find(node => node.dataset.key === `client:${client}`);
      const child = children.find(node => node.dataset.key === childKey);
      assert.ok(parent, 'the client row remains on friendly-name matches');
      assert.equal(list.children.indexOf(child), list.children.indexOf(parent) + 1);
      assert.ok(nodeRows(api).some(node => node.id === childKey));
    }
    assert.match(children[0].textContent, /Saved session choice/);
    assert.match(children[1].textContent, /Recorded model/);
    assert.doesNotMatch(children.map(node => node.textContent).join(' '), /loaded|resident|generating|busy|\d+ subagents/i);
  }
});

test('actual exact-client-model selection keeps its own inspector, focus, and scroll without asserting activity', () => {
  const {api, document, list} = sidebar({clients: identityClients, query: 'review identity v2'});
  const picked = api.spySelection(), target = childButtons(list).find(node => node.dataset.key === 'client-model:codex:1');
  assert.ok(target); target.click();
  assert.deepEqual(Array.from(picked), ['client-model:codex:1']);
  const selected = childButtons(list).find(node => node.dataset.key === 'client-model:codex:1');
  assert.equal(selected.getAttribute('aria-pressed'), 'true'); selected.querySelector('strong').focus();
  list.scrollTop = 173; api.refresh();
  assert.equal(document.activeElement.dataset.key, 'client-model:codex:1');
  assert.ok(list.contains(document.activeElement)); assert.equal(list.scrollTop, 173);
  const body = new Element(document, 'main'); api.inspect('client-model:codex:1', body);
  assert.match(body.textContent, /Saved session choice/);
  assert.match(body.textContent, /org\/review-identity-v2/);
  assert.match(body.textContent, /ActivityUnknown|activity remains unknown/i);
  assert.doesNotMatch(body.textContent, /loaded|resident|generating now|\d+ subagents/i);
});

test('actual default and client-label-only lists retain the compact client rows without child expansion', () => {
  const ordinary = sidebar({clients: identityClients}).list;
  assert.deepEqual(childButtons(ordinary), []);
  assert.equal(buttons(ordinary).filter(node => node.dataset.key.startsWith('client:')).length, 5);
  assert.equal(buttons(ordinary).filter(node => node.dataset.key === 'runtime').length, 1);
  assert.equal(buttons(ordinary).filter(node => node.dataset.key === 'pipeline').length, 1);
  const byClient = sidebar({clients: identityClients, query: 'codex'}).list;
  assert.deepEqual(childButtons(byClient), []);
  assert.deepEqual(buttons(byClient).map(node => node.dataset.key), ['client:codex']);
});

test('new map-sidebar projections remain absent on Mac and runs views even with matching client identities', () => {
  for (const options of [{host: 'mac', query: 'review identity v2'},
    {view: 'runs', query: 'review identity v2'}, {host: 'mac', query: 'this pc'}, {view: 'runs', query: 'nisi'}]) {
    const {api, list} = sidebar({...options, clients: identityClients});
    assert.deepEqual(childButtons(list), []);
    assert.ok(!buttons(list).some(node => ['runtime', 'pipeline'].includes(node.dataset.key)));
    assert.deepEqual(mapEntries(nodeRows(api), {host: 'mac', query: options.query}), []);
  }
});

test('actual map search highlights Windows sidebar aliases and full model names while Mac and runs retain prior matching', () => {
  const matched = className => /\bmatch\b/.test(className) && !/\bfade\b/.test(className);
  const checks = [];
  for (const [query, key] of [['this pc', 'runtime'], ['pc', 'runtime'], ['windows', 'runtime'],
    ['hub', 'runtime'], ['louis-windows-pc', 'runtime'], ['nisi', 'pipeline'],
    ['inference', 'pipeline'], ['route', 'pipeline']]) {
    const {api, list} = sidebar({query, selected: key});
    assert.ok(buttons(list).some(node => node.dataset.key === key), `${query} has its sidebar target`);
    const classes = api.paintClasses();
    assert.match(classes[key], /\bselected\b/);
    checks.push({query, key, matched: matched(classes[key])});
  }
  for (const query of ['org/review-identity-v2', 'review identity v2']) {
    const {api, list} = sidebar({clients: identityClients, query, selected: 'client-model:codex:1'});
    const classes = api.paintClasses();
    for (const key of ['client:codex', 'client-model:codex:1', 'client:claude', 'client-model:claude:0']) {
      assert.ok(buttons(list).some(node => node.dataset.key === key), 'highlight key equals the displayed entry');
      checks.push({query, key, matched: matched(classes[key])});
    }
    assert.match(classes['client-model:codex:1'], /\bselected\b/);
    assert.ok(!matched(classes['client-model:codex:0']), 'the nonmatching sibling stays dimmed');
  }

  const macAlias = sidebar({host: 'mac', query: 'this pc'}).api.paintClasses();
  assert.match(macAlias.runtime, /\bfade\b/, 'Mac does not gain Windows hub aliases');
  const macFriendly = sidebar({host: 'mac', clients: identityClients, query: 'review identity v2'}).api.paintClasses();
  assert.match(macFriendly['client-model:codex:1'], /\bfade\b/, 'Mac keeps the existing truncated-caption/raw-ID matching');
  const macId = sidebar({host: 'mac', clients: identityClients, query: 'org/review-identity-v2'}).api.paintClasses();
  assert.ok(matched(macId['client-model:codex:1']), 'raw ID matching still works on Mac');
  const run = {runId: 'run-side-search', source: 'router-archive', status: 'RESPONSE_VALIDATED', calls: []};
  const routeClasses = sidebar({view: 'runs', query: 'route', runs: [run]}).api.paintClasses();
  assert.ok(Object.values(routeClasses).every(className => /\bfade\b/.test(className)),
    'Windows runtime route alias does not change runs matching');
  const nisiClasses = sidebar({view: 'runs', query: 'nisi', runs: [run]}).api.paintClasses();
  assert.ok(matched(nisiClasses['run:router-archive:run-side-search']), 'the existing run label still matches');
  assert.deepEqual(checks, checks.map(check => ({...check, matched: true})),
    'each new sidebar match and retained client parent has a highlighted map node');
});
