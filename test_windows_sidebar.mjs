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
    this.classList = {add: (...names) => { this.className += ' ' + names.join(' '); }};
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
    spySelection() { const picked=[]; nodeSelect=id=>{picked.push(id); selected=id; renderList();}; return picked; }})`,
  {...layout, ...usage, ...onlineMode, ...modelControl, document, Date: Clock});
  const host = options.host || 'windows';
  const feed = {schemaVersion: 1, host, sampledAt: now - (options.snapshotAge || 0),
    fullSampledAt: now - (options.feedAge || 0), activityKnown: true,
    models: [{id: 'openai/gpt-oss-20b', host, state: 'idle', loaded: true, ageSeconds: 0}],
    macPeer: options.macPeer === undefined ? peer() : options.macPeer,
    components: [], clients: [], sources: [], pipeline: {status: 'idle'},
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
