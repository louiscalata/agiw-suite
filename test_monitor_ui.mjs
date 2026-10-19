import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import test from 'node:test';
import * as layout from './web/map-layout.mjs';
import * as usage from './usage-format.mjs';
import * as onlineMode from './web/online-code-mode.mjs';
import * as modelControl from './web/model-control-view.mjs';

// Review fixes, 26 Sep 2026 (monitor.json): the dashboard's own functions run against a small
// fake DOM, without its polling loop or a browser.
const html = readFileSync(new URL('./web/index.html', import.meta.url), 'utf8');
const css = readFileSync(new URL('./web/style.css', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
const definitions = appSource.replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];

class FakeClassList {
  constructor(node) { this.node = node; }
  get names() { return new Set(String(this.node.className).split(/\s+/).filter(Boolean)); }
  add(...names) { const set = this.names; names.forEach(n => set.add(n)); this.node.className = [...set].join(' '); }
  remove(...names) { const set = this.names; names.forEach(n => set.delete(n)); this.node.className = [...set].join(' '); }
  contains(name) { return this.names.has(name); }
  toggle(name, force) { const on = force === undefined ? !this.contains(name) : Boolean(force); on ? this.add(name) : this.remove(name); return on; }
}

class FakeElement {
  constructor(doc, tag, id = '') {
    Object.assign(this, { doc, tagName: tag.toUpperCase(), id, children: [], parentNode: null, dataset: {}, style: {},
      attributes: {}, hidden: false, scrollTop: 0, listeners: {}, className: '', ownText: '' });
    this.classList = new FakeClassList(this);
  }
  get textContent() { return this.children.length ? this.children.map(c => c.textContent).join('') : this.ownText; }
  set textContent(value) { this.detachChildren(); this.ownText = String(value); }
  // A browser moves focus to <body> when the focused element leaves the document.
  detachChildren() {
    for (const child of this.children) { if (child.contains(this.doc.activeElement)) this.doc.activeElement = this.doc.body; child.parentNode = null; }
    this.children = [];
  }
  append(...nodes) {
    for (let node of nodes) {
      if (typeof node === 'string') { const text = new FakeElement(this.doc, '#text'); text.ownText = node; node = text; }
      if (node.parentNode) node.parentNode.children = node.parentNode.children.filter(child => child !== node);
      node.parentNode = this; this.children.push(node);
    }
  }
  // Replacing an element's children resets the scroll of the box that actually scrolls: the nearest
  // ancestor marked as a scroller (the Live activity section), or the element itself.
  replaceChildren(...nodes) {
    this.detachChildren();
    let scroller = this; while (scroller && !scroller.scroller) scroller = scroller.parentNode;
    (scroller || this).scrollTop = 0; this.append(...nodes);
  }
  get lastElementChild() { return this.children.at(-1) || null; }
  contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }
  matches(selector) { return selector.startsWith('.') ? this.classList.contains(selector.slice(1)) : this.tagName === selector.toUpperCase(); }
  closest(selector) { for (let n = this; n; n = n.parentNode) if (n.matches(selector)) return n; return null; }
  querySelectorAll(selector) { const out = []; const walk = n => n.children.forEach(c => { if (c.matches(selector)) out.push(c); walk(c); }); walk(this); return out; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.doc.activeElement = this; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  click() { for (const listener of this.listeners.click || []) listener({ target: this }); }
  getBoundingClientRect() { return { left: 0, top: 0, right: 820, bottom: 604, width: 820, height: 604 }; }
}

function harness({ width = 1150, extra = {} } = {}) {
  const doc = { elements: new Map() };
  doc.body = new FakeElement(doc, 'body');
  doc.activeElement = doc.body;
  doc.createElement = tag => new FakeElement(doc, tag);
  doc.createTextNode = text => { const node = new FakeElement(doc, '#text'); node.ownText = String(text); return node; };
  doc.getElementById = id => {
    if (!doc.elements.has(id)) { const node = new FakeElement(doc, 'div', id); doc.body.append(node); doc.elements.set(id, node); }
    return doc.elements.get(id);
  };
  for (const id of ['drawer', 'onlineCodeDetails', 'fixInferenceDetails', 'activityDetails', 'aboutPanel', 'modelActionDock', 'memoryBanner', 'memoryBannerMark', 'fixInferencePlainSteps', 'drawerFixButton']) doc.getElementById(id).hidden = true;
  for (const id of ['vitalMac', 'vitalPc', 'vitalRoute', 'vitalActivity']) doc.getElementById(id).append(doc.createElement('strong'), doc.createElement('small'));
  doc.getElementById('sidebarToggle').append(doc.createElement('span'), doc.createElement('span'));
  for (const scope of ['core', 'all']) {
    const button = doc.createElement('button');
    button.dataset.modelScope = scope;
    doc.getElementById('modelScopeControls').append(button);
  }
  doc.getElementById('activityDetails').append(doc.getElementById('activityList'), doc.getElementById('activityAnnounce'));
  // The section scrolls (overflow-y:auto); the list inside it grows.
  doc.getElementById('activityDetails').scroller = true;
  const view = { width };
  const window = { matchMedia: query => ({ matches: Number(/max-width:\s*(\d+)px/.exec(query)?.[1] ?? 0) >= view.width }) };
  const api = runInNewContext(`${definitions}
;({renderActivity,renderVitals,renderSummary,vitalsInput,syncCompactUI,toggleActivity,toggleAbout,closeCompactSidebar,setSidebarOpen,
  rebuildPanel,disclosure,onlineCodeSummary,liveNow,renderInspector,modelControlPollWanted,onlineCodePollWanted,pollOnlineCodeAction,
  renderModelScope,renderList,setModelScope,
  renderAutoUnload,pollAutoUnload,requestAutoUnload,get autoUnloadState(){return autoUnloadState;},
  requestOnlineCodeAction,updateOnlineCodeMode,updateInferenceFix,openFixInference,closeFixInference,renderMemoryBanner,dismissMemoryBanner,
  get memoryDismissed(){return memoryDismissed;},
  get graph(){return graph;},
  // The rail without the map: selecting a node or closing the drawer only moves panels (the map's SVG is not faked here).
  spyRail(){const picked=[];nodeSelect=id=>{picked.push(id);selected=id;$('drawer').hidden=false;makeGraph();renderInspector();};closeDrawer=()=>{$('drawer').hidden=true;selected=null;inspectorNode=null;};fitMap=()=>{};revealSelected=()=>{};return picked;},
  setTraceView(){view='runs';},select(id){selected=id;makeGraph();},render(){makeGraph();renderInspector();},nodeSubtitle(id){return graph.nodes.find(n=>n.id===id)?.subtitle;},
  spyScope(){render=()=>{renderModelScope();makeGraph();renderList();};},
  scopeProjection(){renderModelScope();makeGraph();renderList();return graph.nodes.filter(n=>n.kind==='model').map(n=>n.model.id);},
  refreshEvidence(){renderGraph=()=>{};fitMap=()=>{};render();},
  get selected(){return selected;},
  setModelControl(status,posting=false){modelControlStatus=status;modelControlPosting=posting;},
  setAction(status){onlineCodeActionStatus=status;},
  setPaused(value){paused=value;},
  setFeed(value,isConnected=true){snapshot=value;connected=isConnected;},
  get lastPanel(){return lastEvidencePanel;},set lastPanel(value){lastEvidencePanel=value;},
  spyOpen(){const opened=[];openActivityRow=row=>opened.push(row.key);return opened;}})`,
  { ...layout, ...usage, ...onlineMode, ...modelControl, document: doc, window, performance: { now: () => 0 }, ...extra });
  return { api, doc, $: id => doc.getElementById(id), view };
}

const nowSeconds = () => Date.now() / 1000;
function summaryView(host, { ageSeconds = 0, connected = true, paused = false, inventory = false } = {}) {
  const now = 1_800_000_000;
  class ClockDate extends Date { static now() { return now * 1000; } }
  const result = harness({ extra: { Date: ClockDate } }), { api } = result;
  api.setFeed({ schemaVersion: 1, host, sampledAt: now - ageSeconds, fullSampledAt: now - ageSeconds,
    models: [], clients: [], activityKnown: false,
    sources: inventory ? [{ id: host === 'windows' ? 'lane-inventory' : 'lms-ps', state: 'live' }] : [] }, connected);
  api.setPaused(paused);
  api.renderSummary();
  return result;
}

for (const host of ['windows', 'mac']) {
  test(`Summary clock: ${host} names its local clock when the observer timestamp is ahead`, () => {
    const { $ } = summaryView(host, { ageSeconds: -30 });
    assert.equal($('headline').children[0].textContent, 'Local runtime');
    assert.equal($('headline').querySelector('em').textContent, 'clock mismatch.');
    assert.equal($('subline').textContent,
      `Observer timestamp is ahead of this ${host === 'windows' ? 'PC' : 'Mac'} clock; current activity cannot be verified. Captured at 2027-01-15T08:00:30.000Z.`);
  });

  test(`Summary clock: ${host} keeps paused, stale, disconnected and fresh copy`, () => {
    const cases = [
      { label: 'paused future sample', options: { ageSeconds: -30, paused: true }, em: 'view paused.',
        sub: 'Resume to verify current activity; the displayed model states are from the last sample.' },
      { label: 'stale sample', options: { ageSeconds: 4 }, em: 'signal stale.',
        sub: 'Reconnecting. Previous local observations have aged out.' },
      { label: 'disconnected future sample', options: { ageSeconds: -30, connected: false }, em: 'signal stale.',
        sub: 'Reconnecting. Previous local observations have aged out.' },
      { label: 'fresh sample without inventory', options: {}, em: 'signal unavailable.',
        sub: 'Local runtime sources are unavailable; local model activity is unknown.' },
      { label: 'fresh inventory without activity evidence', options: { inventory: true }, em: 'activity unknown.',
        sub: 'Model inventory is available. Local generation could not be verified.' },
    ];
    for (const { label, options, em, sub } of cases) {
      const { $ } = summaryView(host, options);
      assert.equal($('headline').children[0].textContent, 'Local runtime', label);
      assert.equal($('headline').querySelector('em').textContent, em, label);
      const frozen = options.paused || options.connected === false || Math.abs(options.ageSeconds || 0) > 3;
      const capture = new Date((1_800_000_000 - (options.ageSeconds || 0)) * 1000).toISOString();
      assert.equal($('subline').textContent, sub + (frozen ? ` Captured at ${capture}.` : ''), label);
    }
  });
}

// Producer shape: lane_model_row plus Collector's independently sampled process/caller metadata.
// Collector._lane retains the slot total but clears busy/rate when its poll result expires.
function laneModel(ageSeconds = 0, expired = false) {
  return { id: 'openai/gpt-oss-20b', name: 'gpt-oss-20b', host: 'windows',
    state: expired ? 'unknown' : 'generating', loaded: expired ? null : true,
    source: expired ? 'llama-health' : 'llama-slots', ageSeconds, queued: null,
    metadata: { lane: 'fast', port: 1235, device: 'CUDA0', servedModel: 'openai/gpt-oss-20b',
      laneStatus: expired ? 'stale' : 'busy', slotsTotal: 2, slotsBusy: expired ? null : 1,
      liveTokensPerSecond: expired ? null : 55, detail: expired ? 'Last lane answer is 4 s old (busy then)' : null,
      process: { name: 'llama-server', pid: 4242, cpuPercent: 8, workingSetBytes: 2 ** 30 },
      callers: [{ name: 'Codex', client: 'codex', connections: 2 }] } };
}

function laneInspector(row, { snapshotAge = 0, host = 'windows', paused = false, connected = true, laneFreshSeconds } = {}) {
  const now = 1_800_000_000;
  class ClockDate extends Date { static now() { return now * 1000; } }
  const result = harness({ extra: { Date: ClockDate } }), { api, $ } = result;
  api.setFeed({ schemaVersion: 1, host, sampledAt: now - snapshotAge, fullSampledAt: now - snapshotAge,
    models: [row], clients: [], sources: [], ...(laneFreshSeconds === undefined ? {} : { laneFreshSeconds }), activityKnown: row.source === 'llama-slots',
    pipeline: { status: 'idle' } }, connected);
  api.setPaused(paused);
  $('drawer').append($('inspector'));
  $('drawer').hidden = false;
  const key = `model:${row.host}:${row.id}`;
  api.select(key);
  api.renderInspector();
  const body = $('inspector');
  const pairs = Object.fromEntries(body.querySelectorAll('dt').map(dt =>
    [dt.textContent, dt.parentNode.children[dt.parentNode.children.indexOf(dt) + 1].textContent]));
  return { ...result, body, pairs, key };
}

test('Windows lane freshness follows a bounded collector window with a safe legacy fallback', () => {
  const current = laneInspector(laneModel(8), { laneFreshSeconds: 11.8 });
  assert.notEqual(current.api.nodeSubtitle(current.key), 'Stale');
  const expired = laneInspector(laneModel(11.9), { laneFreshSeconds: 11.8 });
  assert.equal(expired.api.nodeSubtitle(expired.key), 'Stale');
  for (const malformed of [0, -1, 61, NaN, '11.8']) {
    const fallback = laneInspector(laneModel(4), { laneFreshSeconds: malformed });
    assert.equal(fallback.api.nodeSubtitle(fallback.key), 'Stale', `fallback for ${malformed}`);
  }
});

test('Lane inspector: fresh readings and the exact Windows age boundary stay current', () => {
  for (const [age, snapshotAge] of [[0, .2], [2, 1]]) {
    const { pairs } = laneInspector(laneModel(age), { snapshotAge });
    assert.equal(pairs.Slots, '1/2 busy');
    assert.equal(pairs['Live decode'], '55 tok/s (between two slot polls)');
    assert.equal(pairs['Server process'], 'llama-server (pid 4242) · 8% CPU · 1.0 GB RAM');
    assert.equal(pairs['Connected now'], 'Codex ×2');
  }
});

test('Lane inspector: an expired lane on a fresh feed marks slots and decode as history only', () => {
  const { api, body, pairs, key } = laneInspector(laneModel(2.9), { snapshotAge: .2 });
  assert.equal(api.nodeSubtitle(key), 'Stale');
  assert.match(body.querySelector('.panel-summary').textContent, /^Evidence stale/);
  assert.equal(pairs.Slots, '1/2 busy (at last sample)');
  assert.equal(pairs['Live decode'], 'Unknown');
  // Process and caller observations came from this collection, independently of the old lane poll.
  assert.equal(pairs['Server process'], 'llama-server (pid 4242) · 8% CPU · 1.0 GB RAM');
  assert.equal(pairs['Connected now'], 'Codex ×2');
  assert.equal(pairs['Connected (at last sample)'], undefined);
});

test('Lane inspector: a collector-expired busy count is unknown even when the old total remains', () => {
  const { body, pairs } = laneInspector(laneModel(4, true), { snapshotAge: .2 });
  assert.match(body.querySelector('.panel-summary').textContent, /^Evidence stale/);
  assert.equal(pairs.Slots, 'Unknown');
  assert.doesNotMatch(pairs.Slots, /null/);
  assert.equal(pairs['Live decode'], 'Unknown');
});

test('Lane inspector: pausing makes both lane and collection readings historical', () => {
  const { pairs } = laneInspector(laneModel(), { paused: true });
  assert.equal(pairs.Slots, '1/2 busy (at last sample)');
  assert.equal(pairs['Live decode'], 'Unknown');
  assert.equal(pairs['Server process'], 'llama-server (pid 4242) · 8% CPU · 1.0 GB RAM (at last sample)');
  assert.equal(pairs['Connected (at last sample)'], 'Codex ×2');
  assert.equal(pairs['Connected now'], undefined);
});

test('Lane inspector: stale or disconnected feeds keep every observation historical', () => {
  for (const options of [{ snapshotAge: 4 }, { connected: false }]) {
    const { pairs } = laneInspector(laneModel(), options);
    assert.equal(pairs.Slots, '1/2 busy (at last sample)');
    assert.equal(pairs['Live decode'], 'Unknown');
    assert.match(pairs['Server process'], /\(at last sample\)$/);
    assert.equal(pairs['Connected (at last sample)'], 'Codex ×2');
  }
});

test('Lane inspector: absent or non-finite lane age cannot make slot and decode readings current', () => {
  for (const age of [undefined, null, '0', NaN, Infinity, -Infinity]) {
    const row = laneModel(); row.ageSeconds = age;
    const { pairs } = laneInspector(row);
    assert.equal(pairs.Slots, '1/2 busy (at last sample)', String(age));
    assert.equal(pairs['Live decode'], 'Unknown', String(age));
    assert.equal(pairs['Connected now'], 'Codex ×2', String(age));
    assert.doesNotMatch(pairs['Server process'], /at last sample/, String(age));
  }
});

test('Lane inspector: both slot counts must be integers and a zero busy count remains known', () => {
  for (const patch of [{ slotsBusy: null }, { slotsBusy: undefined }, { slotsBusy: false }, { slotsBusy: 1.5 },
    { slotsTotal: null }, { slotsTotal: undefined }, { slotsTotal: false }, { slotsTotal: 2.5 }]) {
    const row = laneModel(); Object.assign(row.metadata, patch);
    const { pairs } = laneInspector(row);
    assert.equal(pairs.Slots, 'Unknown', JSON.stringify(patch));
  }
  const row = laneModel(); row.metadata.slotsBusy = 0;
  assert.equal(laneInspector(row).pairs.Slots, '0/2 busy');
});

test('Lane inspector: shared model expiry preserves Mac local 3s and remote Windows 30s boundaries', () => {
  for (const [modelHost, age, stale] of [['mac', 3, false], ['mac', 3.01, true],
    ['windows', 30, false], ['windows', 30.01, true]]) {
    const row = laneModel(age); row.host = modelHost; row.state = 'idle';
    row.source = modelHost === 'mac' ? 'lms-ps' : 'llama-slots';
    if (modelHost === 'mac') row.metadata = {};
    const { api, body, key } = laneInspector(row, { host: 'mac' });
    assert.equal(api.nodeSubtitle(key), stale ? 'Stale' : 'Idle', `${modelHost}: ${age}s`);
    assert.match(body.querySelector('.panel-summary').textContent,
      stale ? /^Evidence stale/ : /^Loaded · idle/, `${modelHost}: ${age}s`);
  }
});

test('Lane inspector: Mac snapshots keep their existing Windows-row telemetry formatting', () => {
  for (const age of [31, undefined, null, NaN, Infinity, -Infinity]) {
    const row = laneModel(); row.ageSeconds = age;
    const { pairs } = laneInspector(row, { host: 'mac' });
    assert.equal(pairs.Slots, '1/2 busy', String(age));
    assert.equal(pairs['Live decode'], 'Unknown', String(age));
    assert.equal(pairs['Server process'], 'llama-server (pid 4242) · 8% CPU · 1.0 GB RAM', String(age));
    assert.equal(pairs['Connected now'], 'Codex ×2', String(age));
    assert.equal(pairs['Connected (at last sample)'], undefined, String(age));
  }
  // This guard preserves legacy Mac output; validating the missing count belongs to the Windows host only.
  const row = laneModel(31); row.metadata.slotsBusy = null;
  const { pairs } = laneInspector(row, { host: 'mac' });
  assert.equal(pairs.Slots, 'null/2 busy');
  assert.equal(pairs['Live decode'], 'Unknown');
  assert.equal(pairs['Server process'], 'llama-server (pid 4242) · 8% CPU · 1.0 GB RAM');
  assert.equal(pairs['Connected now'], 'Codex ×2');
});

const completionRun = (completionEvidence, testsStatus='NOT_RUN') => ({
  runId:'evidence-run',source:'router-archive',status:'RESPONSE_VALIDATED',
  stage:'finalValidation',host:'mac',client:'codex',calls:[],testsStatus,completionEvidence,
});
function completionInspector(run){
  const result=harness(),{api,$}=result;
  api.setFeed({schemaVersion:1,host:'mac',sampledAt:nowSeconds(),models:[],sources:[],
    activity:{runs:[run]}});
  api.setTraceView();
  api.select('run:'+run.source+':evidence-run');
  $('drawer').hidden=false;
  api.render();
  return result;
}
test('completion evidence stays scoped, accessible, and separate from response validation', () => {
  const evidence={verdict:'VERIFIED',code:'COVERAGE_ASSESSED',criteriaTotal:1,
    criteriaVerified:1,criteriaUnassessed:0,progressStatus:'HISTORY_UNKNOWN',stopReason:null};
  const {api,$}=completionInspector(completionRun(evidence));
  const body=$('inspector'),section=body.querySelector('section');
  assert.equal(section.getAttribute('aria-labelledby'),'completionEvidenceHeading');
  assert.equal(section.querySelector('h3').textContent,'Completion evidence');
  assert.match(section.textContent,/All stated criteria evidenced/);
  assert.match(section.textContent,/Criteria evidenced1 of 1/);
  assert.match(section.textContent,/Tests not run/);
  assert.match(section.textContent,/Workflow and release acceptance not established/);
  assert.doesNotMatch(section.textContent,/certified|safe to ship|hallucination.free/i);
  assert.match(body.textContent,/Response validated/);
  assert.match(body.textContent,/not a certification/);
  assert.equal(api.nodeSubtitle('run:router-archive:evidence-run'),'RESPONSE VALIDATION ONLY');
  const merged=completionInspector({...completionRun(evidence),source:'monitor-receipt',
    completionEvidenceSource:'router-archive'});
  assert.match(merged.$('inspector').textContent,/matching router archive/);
  assert.ok(cssRules(css).some(([media,head])=>!media&&head==='.completion-evidence'));
});
test('partial, unverified, unknown, and repeated results never become completion claims', () => {
  const partial={verdict:'PARTIAL',code:'COVERAGE_ASSESSED',criteriaTotal:2,
    criteriaVerified:1,criteriaUnassessed:1,progressStatus:'HISTORY_UNKNOWN',stopReason:null};
  assert.match(completionInspector(completionRun(partial)).$('inspector').textContent,/Some stated criteria evidenced/);
  const unverified={...partial,verdict:'UNVERIFIED',criteriaVerified:0,criteriaUnassessed:2,code:'VALIDATION_UNBOUND'};
  const error=completionInspector(completionRun(unverified)).$('inspector').textContent;
  assert.match(error,/Stated criteria unverified/);
  assert.match(error,/Validation binding invalid/);
  const unavailable=completionInspector(completionRun({...unverified,code:'EVIDENCE_GATE_UNAVAILABLE'})).$('inspector').textContent;
  assert.match(unavailable,/Completion evidence gate unavailable/);
  const unknown=completionInspector(completionRun(null)).$('inspector').textContent;
  assert.match(unknown,/Criterion evidence unavailable/);
  assert.doesNotMatch(unknown,/All stated criteria evidenced/);
  const forged=completionInspector(completionRun({...partial,verdict:'VERIFIED',code:'<img onerror=alert(1)>'})).$('inspector').textContent;
  assert.match(forged,/Criterion evidence unavailable/);
  assert.doesNotMatch(forged,/<img|All stated criteria evidenced/);
  const proofInjected=completionInspector(completionRun({...partial,proofReceipts:['private proof']})).$('inspector').textContent;
  assert.match(proofInjected,/Criterion evidence unavailable/);
  assert.doesNotMatch(proofInjected,/private proof/);
  const repeated=completionInspector(completionRun({...partial,progressStatus:'NO_PROGRESS',stopReason:'NO_PROGRESS'},null)).$('inspector').textContent;
  assert.match(repeated,/No progress recorded/);
  assert.match(repeated,/does not retry or unload/);
  assert.match(repeated,/Test execution not established/);
  const failed=completionInspector({...completionRun({...partial,verdict:'VERIFIED',criteriaVerified:2,criteriaUnassessed:0}),status:'CHECKS_FAILED'}).$('inspector').textContent;
  assert.doesNotMatch(failed,/All stated criteria evidenced|Completion evidence/);
});
const coreSixIds = [
  'openai/gpt-oss-20b', 'qwen/qwen3.8-27b', 'google/gemma-4-26b-a4b-qat',
  'qwen/qwen3.6-35b-a3b', 'google/gemma-3-4b', 'text-embedding-nomic-embed-text-v1.5',
];

test('AFM is a separate on-device lane with its own passive inspector and stale state', () => {
  const { api, $ } = harness();
  const feed = { schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(),
    models: coreSixIds.map(id => ({ id, host: 'mac', state: 'unloaded', loaded: false, ageSeconds: 0 })),
    sources: [{id: 'lmstudio-api', state: 'live', modelsComplete: true}], clients: [], pipeline: { status: 'idle' },
    afm: { schemaVersion: 1, host: 'mac', state: 'executable',
      callability: 'permission-granted', inference: 'NOT_TESTED' } };
  api.setFeed(feed);
  api.select('afm');
  $('drawer').hidden = false;
  api.render();
  api.renderList();
  assert.equal(api.scopeProjection().length, 6, 'AFM does not consume a Core 6 model slot');
  assert.equal($('inspectorEyebrow').textContent, 'ON-DEVICE ADVISOR');
  assert.match($('inspector').textContent, /Adapter executable/);
  assert.match($('inspector').textContent, /passive sample did not run a model/);
  assert.match($('inspector').textContent, /separate from the six LM Studio models/);
  assert.equal($('modelActionDock').hidden, true, 'AFM cannot use LM Studio load control');
  assert.equal($('list').querySelectorAll('button').filter(item => item.dataset.key === 'afm').length, 1);
  api.setFeed({ ...feed, sampledAt: nowSeconds() - 10 });
  api.render();
  assert.match(api.nodeSubtitle('afm'), /STATUS UNKNOWN/);
  assert.match($('inspector').textContent, /cannot be confirmed/);
  assert.doesNotMatch($('inspector').textContent, /The local adapter can be executed/);
});

test('Jev is a visible route child with recorded judgment history, never a live model or Core 6 slot', () => {
  const { api, $ } = harness();
  const feed = { schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(),
    models: coreSixIds.map(id => ({ id, host: 'mac', state: 'unloaded', loaded: false, ageSeconds: 0 })),
    clients: [], pipeline: { status: 'idle' }, components: [
      { id: 'nisi', state: 'ready', detail: 'pair ready' },
      { id: 'jev', state: 'configured', detail: 'Opted in; last judged a route 2 min ago', lastJudgedAgeSeconds: 120 },
    ] };
  api.setFeed(feed);
  api.select('jev');
  $('drawer').hidden = false;
  api.render();
  api.renderList();
  assert.equal(api.scopeProjection().length, 6);
  assert.equal($('inspectorEyebrow').textContent, 'ROUTE ADVISOR');
  assert.match(api.nodeSubtitle('jev'), /LAST JUDGMENT 2M AGO/);
  assert.match($('inspector').textContent, /Task admission and risk classification/);
  assert.match($('inspector').textContent, /2m ago/);
  assert.match($('inspector').textContent, /does not test its health or run a judgment/);
  assert.match($('inspector').textContent, /does not establish current Jev generation/);
  assert.equal($('modelActionDock').hidden, true);
  assert.equal($('list').querySelectorAll('button').filter(item => item.dataset.key === 'jev').length, 1);
  api.setFeed({ ...feed, sampledAt: nowSeconds() - 10 });
  api.render();
  assert.match(api.nodeSubtitle('jev'), /STATUS UNKNOWN · HISTORY ONLY/);
  assert.match($('inspector').textContent, /Jev status unknown/);
  api.setFeed({ ...feed, components: [{ id: 'jev', state: 'unavailable', detail: 'No private Jev opt-in file', lastJudgedAgeSeconds: null }] });
  api.render();
  assert.match(api.nodeSubtitle('jev'), /NO OPT-IN · NO JUDGMENT/);
  assert.match($('inspector').textContent, /Jev opt-in unavailable/);
  const rules = cssRules(css);
  assert.ok(rules.some(([media, head, body]) => !media && head === '.node.jev .label' && /opacity:\.9/.test(body)), 'Jev label stays visible at rest');
});

test('Core 6 starts as the accessible default for both map and list; All restores nine discovered Mac models', () => {
  assert.match(html, /id="modelScopeControls"[^>]*role="group"[^>]*aria-label="Mac model view"/);
  assert.match(html, /data-model-scope="core" aria-pressed="true"[^>]*>Core 6<\/button>/);
  assert.match(html, /data-model-scope="all" aria-pressed="false"[^>]*>All discovered<\/button>/);
  assert.match(appSource, /\[data-model-scope\][^\n]*addEventListener\('click',\(\)=>setModelScope/);
  const { api, $ } = harness();
  const extras = ['gemma-4-26b-tuned', 'gemma-4-26b-a4b-mtp-mlx', 'text-embedding-nomic-embed-text-v2-moe'];
  const rows = [...coreSixIds, ...extras].map(id => ({ id, host: 'mac', state: 'unloaded', loaded: false, ageSeconds: 0 }));
  api.setFeed({ schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(), models: rows, sources: [{id:'lmstudio-api',state:'live',modelsComplete:true,route:'direct',ageSeconds:0}], clients: [], pipeline: { status: 'idle' } });
  api.spyScope();
  const modelItems = () => $('list').querySelectorAll('button').filter(item => item.dataset.key?.startsWith('model:'));
  assert.deepEqual([...api.scopeProjection()].sort(), [...coreSixIds].sort());
  assert.equal(modelItems().length, 6);
  assert.match($('modelScopeNote').textContent, /6 of 6 primary models discovered.*3 other discovered under All discovered/);
  assert.deepEqual($('modelScopeControls').querySelectorAll('button').map(button => button.getAttribute('aria-pressed')), ['true', 'false']);
  api.setModelScope('all');
  assert.equal(modelItems().length, 9);
  assert.match($('modelScopeNote').textContent, /All 9 discovered Mac models shown/);
  assert.deepEqual($('modelScopeControls').querySelectorAll('button').map(button => button.getAttribute('aria-pressed')), ['false', 'true']);
  const hiddenId = `model:mac:${extras[0]}`;
  api.select(hiddenId);
  $('drawer').hidden = false;
  $('modelActionDock').hidden = false;
  api.setModelScope('core');
  assert.equal(api.selected, null);
  assert.equal($('drawer').hidden, true);
  assert.equal($('modelActionDock').hidden, true);
  assert.equal(modelItems().length, 6);

  // After the separate physical catalog cutover, All still reports only what LM Studio discovers.
  api.setFeed({ schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(),
    models: coreSixIds.map(id => ({ id, host: 'mac', state: 'unloaded', loaded: false, ageSeconds: 0 })),
    sources: [{id:'lmstudio-api',state:'live',modelsComplete:true,route:'direct',ageSeconds:0}], clients: [], pipeline: { status: 'idle' } });
  api.setModelScope('all');
  assert.equal(modelItems().length, 6);
  assert.match($('modelScopeNote').textContent, /All 6 discovered Mac models shown/);
});

test('client inspector separates model IDs from unavailable live subagent evidence', () => {
  const { api, $, body } = drawerHarness();
  api.setFeed({ schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(), models: [], sources: [], pipeline: { status: 'idle' },
    clients: [{ id: 'opencode', model: 'kimi-k2.7-code', modelState: 'configured', agents: { active: 99, state: 'measured' },
      models: ['kimi-k2.7-code', 'glm-5.3', 'nemotron-3-ultra-free'].map(id => ({ id, modelState: 'configured' })) }] });
  api.select('client:opencode');
  api.renderInspector();
  api.renderList();
  assert.equal(api.nodeSubtitle('client:opencode'), 'SUBAGENT ACTIVITY UNKNOWN');
  assert.match(body.textContent, /Subagent activity unknown/);
  assert.match(body.textContent, /Subagents in useUnknown/);
  assert.match(body.textContent, /Subagent sourceNo verified subagent lifecycle feed connected/);
  assert.match(body.textContent, /Subagent ageUnknown/);
  assert.match(body.textContent, /Model ID count3 distinct recorded or saved/);
  assert.doesNotMatch(body.textContent, /99 subagents|Subagents in use99/);
  const row = $('list').querySelectorAll('button').find(item => item.dataset.key === 'client:opencode');
  assert.match(row.textContent, /Subagents unknown/);
});

const pcSnapshot = (sampledAt, jobs) => ({ schemaVersion: 1, host: 'mac', sampledAt, models: [], sources: [{id:'windows-jobs',state:'recorded',ageSeconds:0}], clients: [],
  activity: {runs: [], complete: true, historyBound:{truncated:false}},
  pipeline: { status: 'idle', pipelinesComplete: true }, windowsWorker: { state: 'advertised', ageSeconds: 4, modelsAdvertised: ['Qwen3.8-27B'], detail: 'x' },
  windowsJobs: { schemaVersion: 1, journal: 'recorded', complete: true, unsettled: jobs?.inFlight?.length||0, inFlight: [], recent: [], lastSuccess: null, ...jobs } });
const deepJob = { id: 'mac-20260926-181500-aaaaaaaaaaaa', model: 'Qwen3.8-27B', client: 'claude', lane: 'deep', ageSeconds: 20, timeoutSeconds: 600 };

test('F1: after Pause or a stopped server the Activity chip and list stop saying a PC job is running', () => {
  const { api, $ } = harness();
  api.setFeed(pcSnapshot(nowSeconds() - 12, { inFlight: [deepJob] }));
  const { feed, vitals } = api.vitalsInput();
  assert.equal(feed[0].state, 'in-flight-stale');
  api.renderVitals();
  const chip = $('vitalActivity');
  assert.equal(chip.dataset.tone, 'muted');
  assert.equal(chip.querySelector('small').textContent, 'claude → PC deep in flight at last sample');
  assert.doesNotMatch(chip.getAttribute('aria-label'), /running/);
  assert.equal(vitals.pc.detail, 'Signal stale');
  $('activityDetails').hidden = false;
  api.renderActivity(feed);
  assert.match($('activityList').querySelector('small').textContent, /^In flight at last sample · /);
  assert.equal($('activityList').children[0].className, 'activity-row in-flight-stale');
  // The same job on a fresh feed is live.
  api.setFeed(pcSnapshot(nowSeconds(), { inFlight: [deepJob] }));
  api.renderVitals();
  assert.equal(chip.dataset.tone, 'live');
  assert.match($('activityList').querySelector('small').textContent, /^Running now · /);
});

const pcRow = (id, ageSeconds, extra = {}) => ({ key: `job:${id}`, kind: 'pc-job', client: 'codex', probe: false, target: 'PC fast',
  lane: 'fast', state: 'success', rate: '104 tok/s', elapsed: 1.5, ageSeconds, ...extra });

test('F2: age-only refreshes update rows in place, so keyboard focus, presses and scroll survive', () => {
  const { api, doc, $ } = harness();
  const opened = api.spyOpen();
  const list = $('activityList'), panel = $('activityDetails');
  api.renderActivity([pcRow('a', 5), pcRow('b', 30), pcRow('c', 70)]);
  const third = list.children[2].querySelector('button');
  third.focus();
  panel.scrollTop = 40;
  // Every row crosses a 15-second age bucket: previously a full rebuild each time.
  for (const extra of [1, 16, 31, 46]) {
    api.renderActivity([pcRow('a', 5 + extra), pcRow('b', 30 + extra), pcRow('c', 70 + extra)]);
    assert.equal(doc.activeElement, third, `focus kept after +${extra}s`);
    assert.equal(list.children[2].querySelector('button'), third, 'the row element is not replaced');
  }
  assert.equal(panel.scrollTop, 40);
  assert.match(third.querySelector('small').textContent, /· 1m ago$/);
  third.click();
  assert.deepEqual([...opened], ['job:c']);
});

test('F2: when rows do change, focus and scroll return to the same row, or to the panel if it left', () => {
  const { api, doc, $ } = harness();
  const list = $('activityList'), panel = $('activityDetails');
  api.renderActivity([pcRow('a', 5), pcRow('b', 30), pcRow('c', 70)]);
  list.children[1].querySelector('button').focus();
  // Follow-up 26 Sep: the section is the real scroller, so its position is the one that must survive.
  panel.scrollTop = 25;
  api.renderActivity([pcRow('new', 1), pcRow('a', 6), pcRow('b', 31), pcRow('c', 71)]);
  assert.equal(doc.activeElement.dataset.key, 'job:b');
  assert.ok(list.contains(doc.activeElement));
  assert.equal(panel.scrollTop, 25);
  api.renderActivity([pcRow('new', 2), pcRow('a', 7), pcRow('c', 72)]);
  assert.equal(doc.activeElement, $('activityDetails'));
});

test('F2: the list is not a live region; only rows that arrive while it is open are announced', () => {
  assert.match(html, /<ol id="activityList"><\/ol>/);
  assert.match(html, /<p id="activityAnnounce" class="visually-hidden" aria-live="polite" aria-atomic="true"><\/p>/);
  assert.match(css, /\.visually-hidden\{position:absolute!important;width:1px;height:1px;/);
  const { api, $ } = harness();
  api.setFeed(pcSnapshot(nowSeconds(), { recent: [{ id: 'mac-old', state: 'success', client: 'codex', lane: 'fast', ageSeconds: 50 }] }));
  api.toggleActivity();
  assert.equal($('activityAnnounce').textContent, '', 'opening the panel announces nothing');
  api.renderActivity([pcRow('mac-old', 51)]);
  assert.equal($('activityAnnounce').textContent, '', 'an age change announces nothing');
  api.renderActivity([pcRow('fresh', 1, { client: 'claude', state: 'in-flight', target: 'PC deep' }), pcRow('mac-old', 52)]);
  assert.equal($('activityAnnounce').textContent, 'New activity: Claude → PC deep');
});

test('F6: entering compact mode leaves one panel in the bottom sheet', () => {
  const { api, $, view } = harness({ width: 1150 });
  // Wide: Activity sits beside the inspector (openActivityRow -> nodeSelect keeps it).
  $('activityDetails').hidden = false; $('drawer').hidden = false;
  api.syncCompactUI();
  assert.equal($('activityDetails').hidden, false, 'wide mode keeps both');
  view.width = 800;
  api.syncCompactUI();
  assert.deepEqual([$('drawer').hidden, $('activityDetails').hidden], [false, true]);
  assert.equal($('vitalActivity').getAttribute('aria-expanded'), 'false');
  // No inspector: the most recently opened of Online Code, Fix and Activity stays.
  for (const [last, kept] of [['activityDetails', 'activityDetails'], ['fixInferenceDetails', 'fixInferenceDetails'], [null, 'onlineCodeDetails']]) {
    $('drawer').hidden = true;
    for (const id of ['onlineCodeDetails', 'fixInferenceDetails', 'activityDetails']) $(id).hidden = false;
    api.lastPanel = last;
    api.syncCompactUI();
    assert.deepEqual(['onlineCodeDetails', 'fixInferenceDetails', 'activityDetails'].filter(id => !$(id).hidden), [kept], `last opened ${last}`);
  }
  view.width = 1150;
  api.lastPanel = null;
  api.setFeed(pcSnapshot(nowSeconds(), {}));
  $('activityDetails').hidden = true;
  api.toggleActivity();
  assert.equal(api.lastPanel, 'activityDetails');
  assert.match(appSource, /panel\.hidden=false;lastEvidencePanel='onlineCodeDetails';/);
  assert.match(appSource, /panel\.hidden=false;lastEvidencePanel='fixInferenceDetails';/);
  assert.match(appSource, /function syncCompactUI\(\)\{[^\n]*if\(compact\)keepOneCompactPanel\(\);/);
});

// Top-level rules as [media query or '', selector list, declarations], in file order.
function cssRules(text) {
  const rules = [], source = text.replace(/\/\*[\s\S]*?\*\//g, '');
  const walk = (body, media) => {
    let i = 0;
    while (i < body.length) {
      const open = body.indexOf('{', i); if (open < 0) break;
      const head = body.slice(i, open).trim();
      if (head.startsWith('@media')) {
        let depth = 1, j = open + 1;
        while (depth && j < body.length) { if (body[j] === '{') depth++; else if (body[j] === '}') depth--; j++; }
        walk(body.slice(open + 1, j - 1), head.slice(6).trim()); i = j;
      } else {
        const close = body.indexOf('}', open);
        rules.push([media, head, body.slice(open + 1, close)]); i = close + 1;
      }
    }
  };
  walk(source, '');
  return rules;
}
const selectors = head => head.split(',').map(s => s.trim().replace(/\s+/g, ' '));

test('F4: the Activity chip (the only opener of Live activity) is never hidden; the route chip gives way', () => {
  const hiding = cssRules(css).filter(([, , body]) => /display:none/.test(body));
  for (const [media, head] of hiding) {
    for (const selector of selectors(head)) {
      assert.doesNotMatch(selector, /#vitalActivity|\.vital:nth-child/, `@media ${media} hides the Activity chip via ${selector}`);
    }
  }
  assert.ok(hiding.some(([media, head]) => /max-width:\s*640px/.test(media) && selectors(head).includes('#vitalRoute')));
  assert.ok(hiding.some(([media, head]) => /max-width:\s*480px/.test(media) && selectors(head).includes('#vitalRoute')));
  assert.match(appSource, /\$\('vitalActivity'\)\.addEventListener\('click',toggleActivity\)/);
});

test('F5: shown captions select their node; hidden ones never take clicks', () => {
  const rules = cssRules(css);
  const [[, clickable]] = rules.filter(([media, , body]) => !media && /pointer-events:visiblePainted/.test(body));
  const hits = selectors(clickable);
  const parse = selector => { const [compound, target] = selector.split(' '); return { classes: new Set(compound.split('.').filter(Boolean)), target }; };
  const covers = selector => { const want = parse(selector); return hits.some(hit => { const have = parse(hit);
    return have.target === want.target && [...have.classes].every(c => want.classes.has(c)); }); };
  // Every caption the stylesheet shows without hover or focus.
  const shown = rules.filter(([media, , body]) => !media && /(^|;)opacity:(?!0(;|$))/.test(body))
    .flatMap(([, head]) => selectors(head)).filter(s => /^\.node[.\w-]* \.(label|sub|capability-line)$/.test(s));
  assert.ok(shown.length >= 8);
  for (const selector of shown) assert.ok(covers(selector), `${selector} is visible but still passes clicks to the map`);
  // And nothing that is hidden by default is made clickable.
  for (const hit of hits) assert.ok(shown.some(s => s === hit || parse(s).target === parse(hit).target && [...parse(hit).classes].every(c => parse(s).classes.has(c))), `${hit} is not a shown caption`);
  assert.ok(!hits.includes('.node .label') && !hits.includes('.node .sub') && !hits.includes('.node text'));
  // The Nisi + Jev node's label is opacity 0 even when selected, so it opts back out after the rule above.
  const order = rules.map(([, head]) => head);
  const optOut = rules.findIndex(([media, head, body]) => !media && head === '.node.pipeline .label' && /pointer-events:none/.test(body));
  assert.ok(optOut > order.indexOf(clickable));
  // A click inside a node's <g> (its caption included) is the node's, never the background's.
  assert.match(appSource, /\$\('map'\)\.addEventListener\('click',e=>\{if\(justDragged\|\|e\.target\.closest\?\.\('\.node'\)\)return;showWholeMap\(\);\}\)/);
});

test('F10: About stays reachable in the popover through a map-controls button', () => {
  assert.match(html, /<div class="map-controls">[^\n]*<button id="aboutCompact" class="about-compact" type="button" aria-expanded="false" aria-controls="aboutPanel" aria-label="About this map" title="About this map">\?<\/button><\/div>/);
  const rules = cssRules(css);
  // Round 3 review: page-footer rules are scoped to the page footer (body>footer), never a panel's <footer>.
  const footerHidden = rules.find(([, head, body]) => head === 'body>footer' && /display:none/.test(body))[0];
  assert.ok(rules.some(([media, head]) => !media && selectors(head).includes('.map-controls .about-compact') && /display:none/.test(rules.find(r => r[1] === head)[2])));
  assert.ok(rules.some(([media, head, body]) => media === footerHidden && selectors(head).includes('.map-controls .about-compact') && /display:block/.test(body)),
    'the button appears under the same query that hides the footer');
  assert.match(appSource, /\$\('about'\)\.addEventListener\('click',toggleAbout\);\$\('aboutCompact'\)\.addEventListener\('click',toggleAbout\);/);
  const { api, $ } = harness();
  api.toggleAbout();
  assert.deepEqual([$('aboutPanel').hidden, $('about').getAttribute('aria-expanded'), $('aboutCompact').getAttribute('aria-expanded')], [false, 'true', 'true']);
  api.toggleAbout();
  assert.deepEqual([$('aboutPanel').hidden, $('about').getAttribute('aria-expanded'), $('aboutCompact').getAttribute('aria-expanded')], [true, 'false', 'false']);
  const whole = appSource.slice(appSource.indexOf('function showWholeMap(){'), appSource.indexOf('function syncAboutButtons(){'));
  assert.match(whole, /\$\('aboutPanel'\)\.hidden=true;syncAboutButtons\(\);/);
});

test('F11: in compact mode Browse (and the map controls) leave the layout under the sheet or the model dock', () => {
  const hidden = cssRules(css).filter(([media, , body]) => /max-width:\s*820px/.test(media) && !/max-height/.test(media) && /display:none/.test(body))
    .flatMap(([, head]) => selectors(head));
  for (const selector of ['body:has(.evidence-rail>:not([hidden])) .sidebar-toggle', 'body:has(#modelActionDock:not([hidden])) .sidebar-toggle',
    'body:has(#modelActionDock:not([hidden])) .map-controls', 'body:has(.evidence-rail>:not([hidden])) .map-controls'])
    assert.ok(hidden.includes(selector), `${selector} must be display:none at <=820 px`);
});

// UI pass and review follow-ups, 26 Sep 2026.

test('Follow-up 2: a paused view never says a PC job is running, even on a 0 s old snapshot', () => {
  const { api, $ } = harness();
  api.setFeed(pcSnapshot(nowSeconds(), { inFlight: [deepJob] }));
  api.setPaused(true);
  assert.equal(api.liveNow(), false);
  const { feed, vitals } = api.vitalsInput();
  assert.equal(feed[0].state, 'in-flight-stale');
  assert.deepEqual([vitals.activity.tone, vitals.activity.detail], ['muted', 'claude → PC deep in flight at last sample']);
  assert.notEqual(vitals.pc.detail, 'Signal stale', 'the rest of the strip still reads the fresh snapshot');
  $('activityDetails').hidden = false;
  api.renderVitals();
  assert.equal($('vitalActivity').dataset.tone, 'muted');
  assert.doesNotMatch($('vitalActivity').getAttribute('aria-label'), /running/);
  assert.match($('activityList').querySelector('small').textContent, /^In flight at last sample · /);
  assert.equal($('activityStatus').textContent, '1 job in flight at the last sample');
  api.setPaused(false);
  api.renderVitals();
  assert.equal($('vitalActivity').dataset.tone, 'live');
  assert.match($('activityList').querySelector('small').textContent, /^Running now · /);
  assert.deepEqual([$('activitySummary').dataset.tone, $('activityStatus').textContent], ['live', '1 job running now']);
});

test('C: activity rows show both speeds, a hit-token-limit badge and "Answer rejected (invalid)"', () => {
  const { api, $ } = harness();
  api.setFeed(pcSnapshot(nowSeconds(), { recent: [
    { id: 'mac-a', state: 'success', client: 'codex', lane: 'fast', predictedPerSecond: 104, promptPerSecond: 210, elapsedSeconds: 1.2, ageSeconds: 5, flags: ['hit-token-limit', 'bogus'] },
    { id: 'mac-b', state: 'invalid', client: 'claude', lane: 'deep', ageSeconds: 20 }] }));
  api.toggleActivity();
  const [first, second] = $('activityList').children;
  // Round 3 review: two lines under the title (what, how long, when; then both speeds); the badge rides on the title.
  assert.match(first.querySelector('small').textContent, /^Answered · 1\.2 s · \d+s ago$/);
  assert.equal(first.querySelector('.activity-speeds').textContent, 'reads 210 tok/s · writes 104 tok/s');
  assert.equal(first.querySelector('.activity-title').textContent, 'Codex → PC fasthit token limit');
  assert.equal(first.querySelector('.activity-title').querySelector('.flag-badge').textContent, 'hit token limit');
  assert.equal(first.className, 'activity-row limit', 'a token-limit hit gets the warning dot');
  assert.equal(first.querySelector('.activity-dot').style.background, undefined, 'no client colour over the warning dot');
  assert.match(second.querySelector('small').textContent, /^Answer rejected \(invalid\) · /);
  assert.equal(second.querySelector('.activity-speeds'), null, 'no speeds line for a row without speeds');
  assert.equal(second.className, 'activity-row failed');
  assert.equal(second.querySelector('.flag-badge'), null);
  assert.deepEqual([$('activitySummary').dataset.tone, $('activityStatus').textContent], ['muted', 'Route activity unknown']);
  assert.deepEqual($('activityTiles').children.map(tile => tile.textContent), ['In flightUnknown', 'Answered1', 'FailedUnknown', 'RoutesUnknown']);
});

test('C: a fresh GPU sample joins the Mac chip', () => {
  const { api, $ } = harness();
  const gpu = { model: 'Apple M5 Max', cores: 40, utilizationPercent: 46, rendererPercent: 40, tilerPercent: 12, allocatedBytes: 40_500_000_000, inUseBytes: 30_000_000_000, ageSeconds: 0 };
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), macGpu: gpu });
  api.renderVitals();
  // Round 3 review: the GPU follows what the models are doing, so it never reads as a model at work.
  assert.match($('vitalMac').querySelector('small').textContent, / · GPU 46%$/);
  api.setFeed({ ...pcSnapshot(nowSeconds() - 12, {}), macGpu: gpu });
  api.renderVitals();
  assert.equal($('vitalMac').querySelector('small').textContent, 'Signal stale');
});

test('Follow-up 5: a choice made in the compact Browse sidebar closes it and hands focus to the map', () => {
  const { api, doc, $, view } = harness({ width: 800 });
  $('runtimeSidebar').append($('search'));
  api.setSidebarOpen(true, true);
  assert.equal(doc.activeElement, $('search'));
  assert.equal(api.closeCompactSidebar(), true, 'focus was inside, so the caller refocuses the chosen node');
  assert.deepEqual([doc.body.classList.contains('sidebar-open'), $('sidebarToggle').getAttribute('aria-expanded'), $('runtimeSidebar').inert], [false, 'false', true]);
  assert.equal(api.closeCompactSidebar(), false, 'already closed');
  view.width = 1150;
  api.setSidebarOpen(true);
  assert.equal(api.closeCompactSidebar(), false, 'the wide sidebar is left alone');
  assert.equal(doc.body.classList.contains('sidebar-open'), true);
  for (const [fn, target] of [['nodeSelect', 'id'], ['selectRun', 'selected']]) {
    const line = appSource.slice(appSource.indexOf(`function ${fn}(`)).split('\n')[0];
    assert.match(line, /if\(compactEvidence\(\)\)\{hideOnlineCodeDetails\(\);hideFixInferenceDetails\(\);hideActivityDetails\(\);refocus=closeCompactSidebar\(\);\}/, fn);
    assert.ok(line.endsWith(`if(refocus)nodeElements.get(${target})?.focus({preventScroll:true});}`), fn);
  }
  assert.match(appSource, /\$\('runtimeSidebar'\)\.addEventListener\('keydown',event=>\{if\(event\.key==='Escape'&&document\.body\.classList\.contains\('sidebar-open'\)\)\{event\.preventDefault\(\);setSidebarOpen\(false\);\}\}\);/);
});

test('portfolio split docks an interactive browser on wide screens and retains the compact Browse gate', () => {
  assert.match(html, /<span class="brand-monogram" aria-hidden="true">AS<\/span>/);
  assert.match(html, /<span class="brand-wordmark"><strong>AGIW Suite<\/strong><small>Inference Monitor<\/small><\/span>/);
  const wide = cssRules(css).filter(([media]) => media === '(min-width:980px)');
  const has = (selector, pattern) => wide.some(([, head, body]) => selectors(head).includes(selector) && pattern.test(body));
  assert.ok(has('main', /display:grid;grid-template-columns:minmax\(0,1fr\) var\(--portfolio-rail\)/));
  assert.ok(has('.graph-region', /position:relative;inset:auto/));
  assert.ok(has('.sidebar', /transform:none;overflow-y:auto/));
  assert.ok(has('.evidence-rail', /width:var\(--portfolio-rail\)/));
  assert.match(html, /<aside id="drawer" class="drawer panel" hidden tabindex="-1"/);
  for (const method of ['nodeSelect', 'selectRun']) {
    const line = appSource.slice(appSource.indexOf(`function ${method}(`)).split('\n')[0];
    assert.match(line, /fromDockedBrowser=dockedSidebar\(\)&&\$\('runtimeSidebar'\)\.contains\(document\.activeElement\)/,
      `${method} records focus before the sidebar is visually replaced`);
    assert.match(line, /if\(fromDockedBrowser\)\$\('drawer'\)\.focus\(\{preventScroll:true\}\)/,
      `${method} moves focus to the visible inspector`);
  }
  const { api, $, view, doc } = harness({ width: 1150 });
  api.syncCompactUI();
  assert.equal(doc.body.classList.contains('portfolio-docked'), true);
  assert.equal($('runtimeSidebar').inert, false, 'wide information column remains interactive');
  assert.equal($('sidebarToggle').hidden, true, 'no redundant Browse toggle beside the docked column');
  view.width = 800;
  api.syncCompactUI();
  assert.equal(doc.body.classList.contains('portfolio-docked'), false);
  assert.equal($('runtimeSidebar').inert, true, 'compact drawer is inert until opened');
  assert.equal($('sidebarToggle').hidden, false);
  $('runtimeSidebar').append($('search'));
  api.setSidebarOpen(true, true);
  assert.equal($('runtimeSidebar').inert, false);
  assert.equal(doc.activeElement, $('search'));
  view.width = 900;
  api.syncCompactUI();
  assert.equal($('sidebarToggle').hidden, false, 'the intermediate width still uses Browse');
  assert.equal(api.closeCompactSidebar(), true, 'a choice closes the overlay at intermediate width');
  assert.equal($('runtimeSidebar').inert, true);
});

test('portfolio compact layout keeps the complete status route and controls in the viewport', () => {
  const rules = cssRules(css);
  const hasRule = (media, selector, pattern) => rules.some(([query, head, body]) =>
    query === media && selectors(head).includes(selector) && pattern.test(body));
  assert.ok(hasRule('(min-width:821px) and (max-width:979px) and (max-height:700px)', 'main',
    /height:calc\(100% - 58px\)/), 'the map fills the space below a short intermediate-width header');
  assert.ok(hasRule('(max-width:720px)', '.vitals',
    /display:grid;grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/), 'narrow status chips use two columns');
  assert.ok(hasRule('(max-width:720px)', '#vitalRoute', /display:flex/), 'the Nisi chip remains visible');
  assert.ok(hasRule('(max-width:720px)', '.topbar', /height:96px;display:grid/),
    'small screens give the AS identity and action controls separate rows');
  assert.ok(hasRule('(max-width:720px)', 'main', /height:calc\(100% - 96px\)/),
    'the map fills the viewport below the two-row header');
  assert.match(html, /id="pcSwitch"[^>]*aria-label="PC LLM"/, 'compact PC switch retains an accessible name');
});

test('Follow-up 6: a hidden status chip leaves the strip', () => {
  assert.ok(cssRules(css).some(([media, head, body]) => !media && head === '.vital[hidden]' && /display:none/.test(body)));
});

test('B: a panel rebuild keeps open disclosures, the focused summary and the scroll position', () => {
  const { api, doc, $ } = harness();
  const drawer = $('drawer'), body = $('inspector');
  drawer.append(body);
  const build = () => { api.disclosure(body, 'model:mac:x|tech', 'Technical details'); api.disclosure(body, 'model:mac:x|other', 'Other'); };
  api.rebuildPanel(drawer, body, build);
  const [tech] = body.children;
  tech.open = true;
  tech.querySelector('summary').focus();
  drawer.scrollTop = 120;
  api.rebuildPanel(drawer, body, build);
  const [nextTech, nextOther] = body.children;
  assert.notEqual(nextTech, tech, 'rebuilt');
  assert.deepEqual([nextTech.open, Boolean(nextOther.open)], [true, false]);
  assert.equal(doc.activeElement, nextTech.querySelector('summary'));
  assert.equal(drawer.scrollTop, 120);
  // Another node's disclosures start closed.
  api.rebuildPanel(drawer, body, () => api.disclosure(body, 'model:mac:y|tech', 'Technical details'));
  assert.equal(Boolean(body.children[0].open), false);
});

test('B: the Online Code summary leads with a running or failed check, then the route, in plain words', () => {
  const { api } = harness();
  const mode = state => ({ state, setupLabel: 'Checked 12s ago' });
  const summary = (state, extra = {}) => api.onlineCodeSummary(mode(state), { macOwner: true, actionState: 'idle', action: null, isFresh: true, ...extra });
  assert.deepEqual({ ...summary('ready') }, { tone: 'ok', line: 'Idle · no task running', meaning: 'Setup: checked 12s ago.' });
  assert.equal(summary('processing').tone, 'live');
  assert.equal(summary('unfinished').tone, 'warn');
  assert.deepEqual([summary('ready', { actionState: 'running', action: 'check-and-repair' }).tone, summary('ready', { actionState: 'running', action: 'check-and-repair' }).line], ['busy', 'Repair check running']);
  assert.equal(summary('ready', { actionState: 'error' }).tone, 'bad');
  assert.equal(summary('ready', { actionState: 'needs-action' }).tone, 'warn');
  assert.equal(summary('unknown', { isFresh: false }).line, 'Live feed stale');
  assert.equal(summary('ready', { macOwner: false }).line, 'Evidence only on this PC');
  for (const state of ['ready', 'processing', 'unfinished', 'unknown']) assert.ok(summary(state).line.length <= 60);
});

test('B: panels share one structure; technical detail starts closed; one sticky primary action; no text under 10 px', () => {
  const block = (id, close) => { const start = html.lastIndexOf('<', html.indexOf(`id="${id}"`)); return html.slice(start, html.indexOf(close, start) + close.length); };
  const panels = { onlineCodeDetails: block('onlineCodeDetails', '</section>'), fixInferenceDetails: block('fixInferenceDetails', '</section>'),
    activityDetails: block('activityDetails', '</section>'), drawer: block('drawer', '</aside>') };
  for (const [id, markup] of Object.entries(panels)) {
    assert.match(markup, /^<(section|aside) id="[^"]+" class="[^"]*\bpanel\b/, id);
    assert.match(markup, /<header class="panel-head">[\s\S]*?<h2[\s\S]*?class="[^"]*\bpanel-close\b[^"]*"[^>]*aria-label="Close /, id);
    assert.doesNotMatch(markup, /<details[^>]*\bopen\b/, `${id}: technical detail starts closed`);
  }
  for (const id of ['onlineCodeDetails', 'fixInferenceDetails', 'activityDetails'])
    assert.match(panels[id], /class="[^"]*\bpanel-summary\b[^"]*"[^>]*>(<i class="status-dot"><\/i>)?/, id);
  assert.match(panels.onlineCodeDetails, /<footer id="onlineCodeActions" class="panel-actions"><button id="onlineCodeRepair" class="mode-repair-button panel-primary" type="button" hidden>Check and repair<\/button><button id="pcHeadless"/);
  // 27 Sep: the private Nisi v0.2 integration is labelled "Nisi Inference" (internal ids stay).
  assert.match(panels.onlineCodeDetails, /<summary>Nisi Inference runtime evidence<\/summary>/);
  assert.match(panels.fixInferenceDetails, /<footer class="panel-actions"><button id="fixInferenceSubmit" class="fix-submit panel-primary" type="submit">Run check<\/button><\/footer><\/form>/);
  assert.doesNotMatch(panels.fixInferenceDetails, /name="fixScope"|id="fixInferenceScopes"/);
  assert.match(panels.fixInferenceDetails, /<details class="panel-more"><summary>What this does<\/summary><p class="fix-intro">/);
  assert.match(panels.drawer, /<div id="modelActionDock" class="model-action-dock panel-actions" hidden aria-live="polite">[\s\S]*<button id="modelAction" class="panel-primary"/);
  const rules = cssRules(css), rule = (selector, media = '') => rules.filter(([m, head]) => m === media && selectors(head).includes(selector)).map(([, , body]) => body).join('\n');
  assert.match(rule('.panel-head'), /position:sticky;top:0/);
  assert.match(rule('.panel-actions'), /position:sticky;bottom:0/);
  assert.match(rule('.evidence-rail .panel-head .panel-close'), /width:36px;height:36px/);
  assert.match(rule('.evidence-rail .panel-actions button'), /min-height:38px/);
  assert.doesNotMatch(css, /\.fix-scope|\.segmented|\.segments/);
  assert.match(rule('.evidence-rail', '(max-width:820px)'), /max-height:min\(46%,380px\);animation:sheet-up/);
  assert.match(css, /@media \(prefers-reduced-motion:reduce\)\{\.evidence-rail\{animation:none\}/);
  // Every size set by the panel rules is at least 10 px.
  const panelCss = css.slice(css.indexOf('/* Panels (26 Sep UI pass)'));
  for (const [, head, body] of cssRules(panelCss))
    for (const [, size] of body.matchAll(/font(?:-size)?:[^;]*?(\d+(?:\.\d+)?)px/g)) assert.ok(Number(size) >= 10, `${head} sets ${size}px`);
});

// Round 3 review follow-ups, 26 Sep 2026.

const settle = () => new Promise(resolve => setImmediate(resolve));
function netHarness(options = {}) {
  const fetches = [];
  const reply = { status: 'idle', operationId: 0, action: null, message: '', steps: [] };
  const extra = { fetch: url => { fetches.push(url); return Promise.resolve({ ok: true, json: async () => reply }); },
    setTimeout: () => 0, clearTimeout: () => {}, AbortController: class { constructor() { this.signal = {}; } abort() {} } };
  return { ...harness({ ...options, extra }), fetches, reply };
}

test('Round 3 (HIGH 2): an idle page polls nothing; model control only with its footer; the entry once, then with a panel or a check', async () => {
  const { api, $, fetches, reply } = netHarness();
  // Model control: only while the Load/Unload footer shows, a request is posting, or one is running.
  assert.equal(api.modelControlPollWanted(), false);
  $('modelActionDock').hidden = false;
  assert.equal(api.modelControlPollWanted(), true);
  $('modelActionDock').hidden = true;
  api.setModelControl({ status: 'running' });
  assert.equal(api.modelControlPollWanted(), true);
  api.setModelControl({ status: 'idle' }, true);
  assert.equal(api.modelControlPollWanted(), true);
  api.setModelControl({ status: 'idle' });
  assert.equal(api.modelControlPollWanted(), false);
  // The Online Code entry: nothing before a Mac snapshot, one read once it arrives, then nothing while idle.
  await api.pollOnlineCodeAction();
  assert.equal(fetches.length, 0, 'no snapshot yet');
  api.setFeed(pcSnapshot(nowSeconds(), {}));
  await api.pollOnlineCodeAction(); await settle();
  assert.deepEqual(fetches, ['/api/online-code-mode/entry']);
  for (let i = 0; i < 3; i++) await api.pollOnlineCodeAction();
  assert.equal(fetches.length, 1, 'idle, both panels closed: no periodic GET');
  assert.equal(api.onlineCodePollWanted(), false);
  // Read while Online Code or Fix inference is open ...
  for (const id of ['onlineCodeDetails', 'fixInferenceDetails']) {
    $(id).hidden = false;
    await api.pollOnlineCodeAction(); await settle();
    $(id).hidden = true;
  }
  assert.equal(fetches.length, 3);
  // ... or while a check is running or uncertain, until it settles.
  reply.status = 'running'; reply.operationId = 1; reply.action = 'check-and-repair';
  api.setAction({ status: 'running', operationId: 1, action: 'check-and-repair', message: '', steps: [] });
  await api.pollOnlineCodeAction(); await settle();
  assert.equal(fetches.length, 4);
  reply.status = 'ready';
  await api.pollOnlineCodeAction(); await settle();
  assert.equal(fetches.length, 5);
  await api.pollOnlineCodeAction();
  assert.equal(fetches.length, 5, 'settled: polling stops');
  // The page wires both intervals through these guards.
  assert.match(appSource, /setInterval\(\(\)=>\{if\(modelControlPollWanted\(\)\)pollModelControl\(\);\},1000\);/);
  assert.doesNotMatch(appSource, /setInterval\(pollModelControl,/);
  assert.match(appSource, /if\(onlineCodeActionPolling\|\|!onlineCodePollWanted\(\)\)return;/);
});

const pcWorker = extra => ({ state: 'advertised', ageSeconds: 4, modelsAdvertised: ['gpt-oss-20b'], detail: 'x',
  lanes: { fast: { up: true, model: 'gpt-oss-20b', kind: 'gpt-oss', slotsBusy: 0, slotsTotal: 2 }, deep: { up: true, model: 'Qwen3.8-27B', kind: 'qwen', slotsBusy: 1, slotsTotal: 1 } },
  headless: { state: 'on', expiresInSeconds: 7200 }, ...extra });
const pcGpu = { index: 0, name: 'NVIDIA GeForce RTX 4070', utilizationPercent: 32, memoryUsedMiB: 11980, memoryTotalMiB: 12282, temperatureC: 54, powerW: 118.5 };

function drawerHarness() {
  const h = harness();
  const drawer = h.$('drawer'), body = h.$('inspector');
  drawer.append(body); drawer.scroller = true; drawer.hidden = false;
  return { ...h, drawer, body };
}

test('A3 fix 6 finding 1: runtime residency and unchecked callers remain unknown', () => {
  for (const host of ['windows', 'mac']) {
    for (const residency of [[null, null], [true, null]]) {
      const { api, body } = drawerHarness();
      api.setFeed({ ...pcSnapshot(nowSeconds(), {}), host, activityKnown: false, localCallers: null,
        sources: [{ id: host === 'windows' ? 'lane-inventory' : 'lms-ps', state: 'live' }],
        models: residency.map((loaded, i) => ({ id: `model-${i}`, host, loaded, state: 'unknown', ageSeconds: 0 })) });
      api.select('runtime');
      api.renderInspector();
      assert.equal(tiles(body).find(([label]) => label === 'Loaded')[1], 'Unknown');
      assert.match(body.textContent, /Local callersUnknown/);
      assert.doesNotMatch(body.textContent, /Quiet|Not checked while local models are idle/);
    }
  }
});

test('A3 fix 6 finding 1: runtime counts require a live complete inventory and caller checks stay independent', () => {
  const cases = [
    { residency: [true, false], source: 'live', loaded: '1' },
    { residency: [false, false], source: 'live', loaded: '0' },
    { residency: [], source: 'live', loaded: '0' },
    { residency: [], source: 'unavailable', loaded: 'Unknown' },
    { residency: [true, false], source: 'recorded', loaded: 'Unknown' },
    { residency: [null, null], source: 'live', loaded: 'Unknown' },
  ];
  for (const host of ['windows', 'mac']) {
    for (const row of cases) {
      const { api, body } = drawerHarness();
      api.setFeed({ ...pcSnapshot(nowSeconds(), {}), host, activityKnown: true, localCallers: [],
        sources: [{ id: host === 'windows' ? 'lane-inventory' : 'lms-ps', state: row.source }],
        models: row.residency.map((loaded, i) => ({ id: `model-${i}`, host, loaded, state: loaded ? 'idle' : 'unknown', ageSeconds: 0 })) });
      api.select('runtime');
      api.renderInspector();
      assert.equal(tiles(body).find(([label]) => label === 'Loaded')[1], row.loaded);
      assert.match(body.textContent, /Local callersNone connected/);
      const headline = body.querySelector('.panel-summary').querySelector('strong').textContent;
      if (row.loaded === 'Unknown') assert.match(headline, /^(?:Quiet · loaded unknown|Activity unknown)$/);
      else assert.equal(headline, `Quiet · ${row.loaded} loaded`);
    }
  }
});

test('Round 3 (MEDIUM): the drawer patches aged text in place; it rebuilds only for a new structure, and a new node opens at the top', () => {
  const { api, doc, drawer, body } = drawerHarness();
  const at = nowSeconds();
  const snap = pcSnapshot(at, { inFlight: [deepJob], recent: [{ id: 'mac-r1', state: 'success', model: 'gpt-oss-20b', client: 'codex', lane: 'fast', ageSeconds: 30, elapsedSeconds: 1.2 }] });
  api.setFeed(snap);
  api.select('windows-worker');
  api.renderInspector();
  const summary = body.querySelector('.panel-summary'), details = body.querySelectorAll('details'), line = summary.querySelector('strong');
  assert.match(line.textContent, /^Job in flight · 20 s so far$/);
  details[0].open = true;
  details[0].querySelector('summary').focus();
  drawer.scrollTop = 90;
  // Two seconds later the same snapshot has aged: only text changes, so nothing is replaced.
  snap.sampledAt = at - 2;
  api.render();
  assert.equal(body.querySelector('.panel-summary'), summary, 'the summary element is kept');
  assert.equal(summary.querySelector('strong'), line);
  assert.equal(line.textContent, 'Job in flight · 22 s so far', 'its text is patched in place');
  assert.deepEqual(body.querySelectorAll('details'), details, 'the disclosures are the same elements');
  assert.equal(details[0].open, true);
  assert.equal(doc.activeElement, details[0].querySelector('summary'), 'focus stays on the disclosure');
  assert.equal(drawer.scrollTop, 90);
  // A new recent job changes the structure: a rebuild that keeps what was open, the focus and the scroll.
  snap.windowsJobs.recent.push({ id: 'mac-r2', state: 'cancelled', client: 'claude', lane: 'deep', ageSeconds: 50, elapsedSeconds: 3 });
  api.render();
  assert.notEqual(body.querySelector('.panel-summary'), summary, 'rebuilt');
  const [recent] = body.querySelectorAll('details');
  assert.equal(recent.querySelector('summary').textContent, 'Recent jobs (2)');
  assert.equal(recent.open, true);
  assert.equal(doc.activeElement, recent.querySelector('summary'));
  assert.equal(drawer.scrollTop, 90);
  // Another node opens at its summary, not mid-content.
  api.select('pipeline');
  api.renderInspector();
  assert.equal(drawer.scrollTop, 0);
  // The key is the rendered output, never a JSON of raw aged objects.
  assert.doesNotMatch(appSource, /detailKey/);
  assert.match(appSource, /if\(sameNode&&shape===inspectorShape\)\{patchPanel\(body,next\);return;\}/);
});

test('Round 3: the PC inspector shows the heartbeat GPU, the worker version and each in-flight job’s own timeout', () => {
  const { api, body } = drawerHarness();
  api.setFeed({ ...pcSnapshot(nowSeconds(), { inFlight: [{ ...deepJob, cancelRequested: true }] }), windowsWorker: pcWorker({ workerVersion: '1.2', gpus: [pcGpu] }) });
  api.select('windows-worker');
  api.renderInspector();
  const tiles = body.querySelector('.tiles').children;
  assert.deepEqual([tiles[0].textContent, tiles[2].textContent, tiles[3].textContent], ['ConnectionHeartbeat received', 'Worker version1.2', 'Heartbeat4s ago']);
  assert.match(tiles[1].textContent, /^PC LLM switchOn · (1h 59m|2h 0m)$/);
  const text = body.textContent;
  assert.match(text, /Model lanesFast lanegpt-oss-20b · Up · 0\/2 slots busy/);
  assert.match(text, /Deep laneQwen3\.8-27B · Up · 1\/1 slots busy/);
  assert.match(text, /Worker version1\.2/);
  assert.match(text, /GPU 0NVIDIA GeForce RTX 4070 · 32% busy · 11\.7 of 12\.0 GB · 54 °C · 119 W/);
  assert.match(text, /cancel requested · 20 s · times out at 10 min/);
  assert.doesNotMatch(text, / s of 630 s/);
  // A worker that sends no GPU says so in the visible PC graphics section.
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), windowsWorker: pcWorker({ gpus: null }) });
  api.render();
  assert.match(body.textContent, /PC GPUNot reported by the worker heartbeat/);
  // The chip's detail carries the reading too.
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), windowsWorker: pcWorker({ gpus: [pcGpu] }) });
  assert.match(api.vitalsInput().vitals.pc.detail, / · GPU 32% · 11\.7\/12\.0 GB · 54 °C$/);
});

test('the PC panel keeps a journaled answer distinct from a stale worker connection', () => {
  const { api, body } = drawerHarness();
  const answer = { id: 'mac-success', state: 'success', model: 'gpt-oss-20b', client: 'codex', lane: 'fast',
    ageSeconds: 28, elapsedSeconds: 2.1, promptPerSecond: 220, predictedPerSecond: 100 };
  const base = pcSnapshot(nowSeconds(), { recent: [answer], lastSuccess: answer });
  api.setFeed({ ...base, windowsWorker: pcWorker({ ageSeconds: 90, workerVersion: '1.3', gpus: [pcGpu] }) });
  api.select('windows-worker');
  api.renderInspector();
  assert.equal(api.nodeSubtitle('windows-lane:fast'), undefined, 'old worker heartbeat does not draw a live lane');
  assert.deepEqual(body.querySelector('.tiles').children.map(tile => tile.textContent),
    ['ConnectionUnknown', 'PC LLM switchUnknown', 'Worker versionUnknown', 'HeartbeatUnknown']);
  assert.match(body.textContent, /PC GPUUnknown \(no fresh worker heartbeat\)/);
  assert.match(body.textContent, /Fast laneUnknown \(no fresh lane heartbeat\)/);
  assert.match(body.textContent, /Last validated answer.*GPT OSS 20b.*recorded/s);
  assert.match(body.textContent, /Fast last measured speedreads 220 tok\/s · writes 100 tok\/s · \d+s ago · recorded answer/);

  api.setFeed({ ...base, windowsWorker: pcWorker({ workerVersion: '1.3', gpus: [pcGpu] }) });
  api.render();
  assert.match(api.nodeSubtitle('windows-lane:fast'), /0\/2 BUSY/);
  assert.match(body.textContent, /ConnectionHeartbeat received/);
  assert.match(body.textContent, /Worker version1\.3/);
  assert.match(body.textContent, /GPU 0NVIDIA GeForce RTX 4070 · 32% busy · 11\.7 of 12\.0 GB/);
  assert.match(body.textContent, /Fast lanegpt-oss-20b · Up · 0\/2 slots busy/);

  api.setFeed({ ...base, windowsJobs: { schemaVersion: 1 }, windowsWorker: pcWorker({ workerVersion: '1.3', gpus: [pcGpu] }) });
  api.render();
  assert.match(body.textContent, /Current jobUnknown \(live job feed unavailable\)/);
  assert.match(body.textContent, /Last validated answerUnknown/);
  assert.match(body.textContent, /Fast last measured speedUnknown/);
});

test('A3 UI: unknown lane health and unavailable job feeds stay unknown in the rendered PC panel', () => {
  const { api, body, $ } = drawerHarness();
  const worker = pcWorker({ state: 'unknown', detail: 'Lane ownership unavailable.', lanes: {
    fast: { up: null, health: { state: 'unknown', reason: 'owner-unverified' }, model: 'gpt-oss-20b', slotsBusy: 0, slotsTotal: 2 },
    deep: { up: false, status: 'no_identity', model: 'Qwen3.8-27B', slotsBusy: 0, slotsTotal: 1 },
  } });
  const unavailable = { schemaVersion: 1, availability: { state: 'unknown', reason: 'source-unavailable' },
    inFlight: [], recent: [], lastSuccess: null };
  api.setFeed({ ...pcSnapshot(nowSeconds(), unavailable), windowsWorker: worker });
  api.select('windows-worker');
  api.renderInspector();
  assert.match(body.textContent, /Lane availability unknown/);
  assert.match(body.textContent, /Lane ownership unavailable/);
  assert.match(body.textContent, /Fast lanegpt-oss-20b · Unknown · slots unknown · owner-unverified/);
  assert.match(body.textContent, /Deep laneQwen3\.8-27B · Unknown · slots unknown/);
  assert.match(body.textContent, /Current jobUnknown \(live job feed unavailable\)/);
  assert.match(body.textContent, /Last validated answerUnknown/);
  assert.match(body.textContent, /Jobs in flightUnknown \(live feed unavailable\)/);
  assert.doesNotMatch(body.textContent, /No job in flight|None journaled|Jobs in flight0/);
  const laneView = layout.windowsLaneView(worker, layout.windowsJobsView(unavailable, { feedFresh: true }), { feedFresh: true });
  assert.deepEqual(layout.windowsLaneRows(laneView), [
    ['Lane fast · gpt-oss-20b', 'Unknown · Slots unknown · owner-unverified'],
    ['Lane deep · Qwen3.8-27B', 'Unknown · Slots unknown'],
  ]);

  api.setFeed({ ...pcSnapshot(nowSeconds(), { ...unavailable, availability: { state: 'available' }, liveAvailable: false,
    recent: [{ id: 'done', state: 'success', client: 'codex', lane: 'fast', model: 'gpt-oss-20b', ageSeconds: 3, elapsedSeconds: 1 }] }), windowsWorker: worker });
  api.render();
  assert.match(body.textContent, /Current jobUnknown \(live job feed unavailable\)/);
  assert.doesNotMatch(body.textContent, /No job in flight/);

  // Older snapshots may only carry source health, without the new per-feed availability field.
  api.setFeed({ ...pcSnapshot(nowSeconds(), { schemaVersion: 1, inFlight: [], recent: [], lastSuccess: null }),
    sources: [{ id: 'windows-jobs', state: 'unavailable' }] });
  api.render();
  assert.match(body.textContent, /Current jobUnknown \(live job feed unavailable\)/);
  $('activityDetails').hidden = false;
  api.renderActivity([]);
  assert.equal($('activityStatus').textContent, 'PC job state unknown');
  assert.match($('activityMeaning').textContent, /current PC activity cannot be confirmed/);
  assert.match($('activityTiles').textContent, /In flightUnknownAnsweredUnknownFailedUnknown/);
  assert.match($('activityList').textContent, /PC job activity is unknown/);
});

test('A3 UI: Mac presence unknown uses presence wording in the view model', () => {
  const view = layout.macPeerView({ state: 'unknown', reason: 'no-report' }, { feedFresh: true });
  assert.equal(view.subtitle, 'PRESENCE UNKNOWN');
  assert.match(view.presenceDetail, /No presence report/);
  assert.doesNotMatch(view.subtitle, /LAN/);
  const { api, body } = drawerHarness();
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), host: 'windows', macPeer: { state: 'unknown', reason: 'no-report' } });
  api.select('mac-peer');
  api.renderInspector();
  api.render();
  assert.match(body.textContent, /Mac presence unknown/);
  assert.match(body.textContent, /No presence report/);
  assert.ok(api.graph.labels.some(label => label.text === 'MAC · PRESENCE'));
  assert.doesNotMatch(api.graph.labels.map(label => label.text).join(' '), /MAC · LAN PEER/);
});

test('A3 UI: the lane inspector distinguishes unavailable job history from a known empty journal', () => {
  const { api, body } = drawerHarness();
  for (const sourceUnavailable of [false, true]) {
    api.setFeed({ ...pcSnapshot(nowSeconds(), sourceUnavailable ? {} : {
      availability: { state: 'unknown', reason: 'source-unavailable' },
    }), windowsWorker: pcWorker(), sources: sourceUnavailable ? [{ id: 'windows-jobs', state: 'unavailable' }] : [] });
    api.select('windows-lane:fast');
    api.renderInspector();
    const technical = body.querySelector('details');
    assert.match(technical.textContent, /Last measured speedUnknown \(job feed unavailable\)/);
    assert.doesNotMatch(technical.textContent, /No successful job journaled/);
  }
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), windowsWorker: pcWorker() });
  api.render();
  assert.match(body.querySelector('details').textContent, /Last measured speedNo successful job journaled/);
});

test('A3 UI: the Windows Mac chip labels absent reports as Mac presence in its title and accessible name', () => {
  const { api, $ } = harness();
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), host: 'windows', macPeer: { state: 'unknown', reason: 'no-report' } });
  api.renderVitals();
  const chip = $('vitalPc');
  assert.match(chip.title, /^Mac presence: /);
  assert.match(chip.title, /unknown/i);
  assert.equal(chip.getAttribute('aria-label'), chip.title);
  assert.doesNotMatch(chip.title, /LAN peer/);
});

test('Round 3 (LOW): a cancelled PC job reads "Cancelled"; a cancel request shows on the running row', () => {
  const { api, $ } = harness();
  api.setFeed(pcSnapshot(nowSeconds(), { inFlight: [{ ...deepJob, cancelRequested: true }],
    recent: [{ id: 'mac-c', state: 'cancelled', client: 'claude', lane: 'deep', elapsedSeconds: 11.5, ageSeconds: 30 }] }));
  api.toggleActivity();
  const [running, cancelled] = $('activityList').children;
  assert.match(running.querySelector('small').textContent, /^Running now · cancel requested · \d+s ago$/);
  assert.match(cancelled.querySelector('small').textContent, /^Cancelled · 11\.5 s · 30s ago$/);
  assert.equal(cancelled.className, 'activity-row cancelled');
  assert.equal(cancelled.querySelector('.activity-speeds'), null);
});

test('Round 3 (MEDIUM, LOW): Online Code leads with a just-finished check, waits for the feed, and hides an empty footer', () => {
  const { api } = harness();
  const mode = state => ({ state, setupLabel: 'Checked 12s ago' });
  const summary = (state, extra = {}) => ({ ...api.onlineCodeSummary(mode(state), { macOwner: true, actionState: 'idle', action: null, isFresh: true, ...extra }) });
  assert.deepEqual(summary('ready', { actionState: 'ready', action: 'check-and-repair', message: 'No pending records; readiness verified.' }),
    { tone: 'ok', line: 'Repair check complete', meaning: 'No pending records; readiness verified. Route idle.' });
  assert.deepEqual(summary('processing', { actionState: 'needs-action', action: 'fix-route', message: 'SharedChami is still mounted read-only.' }),
    { tone: 'warn', line: 'Route pipeline needs action', meaning: 'SharedChami is still mounted read-only. A routed task is processing.' });
  assert.deepEqual(summary('unknown', { actionState: 'error', action: 'headless-on', message: '' }),
    { tone: 'bad', line: 'Turn PC headless on failed', meaning: 'No detail was returned. Route state unknown.' });
  assert.equal(summary('ready', { actionState: 'error', action: null }).line, 'The latest check failed');
  // Processing and unfinished routes still outrank a finished (ready) check; idle does not.
  assert.equal(summary('processing', { actionState: 'ready', action: 'check-and-repair' }).line, 'A routed task is processing');
  assert.deepEqual(summary('ready', { hasFeed: false, macOwner: false }),
    { tone: 'muted', line: 'Waiting for the live feed', meaning: 'Nothing can be confirmed until the first snapshot arrives.' });
  for (const s of [summary('ready', { actionState: 'needs-action', action: 'check-and-repair', message: 'x' }), summary('ready', { actionState: 'error', action: 'fix-both' })])
    assert.ok(s.line.length <= 60);
});

test('Round 3: the Online Code panel hides the duplicate result for a finished check and has no empty footer without a feed', () => {
  const { api, $ } = harness();
  api.updateOnlineCodeMode();
  assert.deepEqual([$('onlineCodeStatus').textContent, $('onlineCodeActions').hidden], ['Waiting for the live feed', true]);
  assert.equal($('fixInferenceResult').querySelector('strong').textContent, 'Waiting for the live feed');
  // The top-bar tabs wait for the feed too, instead of calling this Mac a non-owner.
  assert.equal($('onlineCodeLabel').textContent, 'Waiting for the live feed');
  assert.match($('onlineCodeMode').getAttribute('aria-label'), /Waiting for the live feed/);
  assert.match($('fixInference').getAttribute('aria-label'), /Waiting for the live feed/);
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), onlineCodeMode: { state: 'ready', active: false, taskState: 'idle', setupState: 'fresh', setupAgeSeconds: 42 } });
  api.setAction({ status: 'ready', operationId: 1, action: 'check-and-repair', message: 'No pending records; readiness verified.', steps: [{ name: 'readiness', result: 'verified' }] });
  api.updateOnlineCodeMode();
  assert.deepEqual([$('onlineCodeStatus').textContent, $('onlineCodeMeaning').textContent], ['Repair check complete', 'No pending records; readiness verified. Route idle.']);
  assert.equal($('onlineCodeResult').hidden, true, 'the summary already carries the result');
  assert.equal($('onlineCodeSteps').hidden, false, 'its steps stay one click away');
  assert.equal($('onlineCodeActions').hidden, false);
  // The tab's subline is the short form; the aria label keeps the long one.
  assert.equal($('onlineCodeLabel').textContent, 'Idle · checked 42s ago');
  assert.match($('onlineCodeMode').getAttribute('aria-label'), /Current route: Task idle · setup checked 42s ago\./);
  // While a check runs, the result block (its live message) shows again.
  api.setAction({ status: 'running', operationId: 2, action: 'check-and-repair', message: 'Checking exact pending records.', steps: [] });
  api.updateOnlineCodeMode();
  assert.equal($('onlineCodeResult').hidden, false);
});

test('Fix inference explains the combined action in its idle summary', () => {
  const { api, $ } = harness();
  api.setFeed(pcSnapshot(nowSeconds(), {}));
  api.updateInferenceFix();
  const result = $('fixInferenceResult');
  assert.deepEqual([result.querySelector('strong').textContent, result.querySelector('p').textContent],
    ['Ready to check', 'Checks and repairs the Mac runtime, Windows route and Nisi Inference in one guarded run.']);
  assert.equal($('fixScopeNote').hidden, true);
  api.setAction({ status: 'ready', operationId: 3, action: 'fix-all', message: 'Inference verified.', steps: [] });
  api.updateInferenceFix();
  assert.deepEqual([result.querySelector('strong').textContent, result.querySelector('p').textContent], ['Inference checks complete', 'Inference verified.']);
});

test('Round 3 (MEDIUM): a chip that overflows steps down to its short, then tiny, form, measured per chip', () => {
  const { api, $, view } = harness({ width: 1150 });
  const budget = { vitalMac: 200, vitalPc: 400, vitalRoute: 400, vitalActivity: 110 };
  for (const [id, width] of Object.entries(budget)) {
    const small = $(id).querySelector('small');
    Object.defineProperty(small, 'clientWidth', { get: () => budget[id] });
    Object.defineProperty(small, 'scrollWidth', { get() { return this.textContent.length * 6.5; } });
  }
  api.setFeed({ ...pcSnapshot(nowSeconds(), { inFlight: [deepJob] }), macGpu: { utilizationPercent: 46, ageSeconds: 0 } });
  api.renderVitals();
  const chip = id => [$(id).querySelector('strong').textContent, $(id).querySelector('small').textContent];
  // Mac: the long form overflows, the short one fits. PC and route keep their long forms.
  assert.deepEqual(chip('vitalMac'), ['Mac', 'Activity unknown · GPU 46%']);
  assert.deepEqual(chip('vitalPc'), ['Windows PC', 'Headless unknown']);
  assert.deepEqual(chip('vitalRoute'), ['Nisi Inference', 'Idle']);
  assert.deepEqual(chip('vitalActivity'), ['Latest · 20s', 'claude → PC deep']);
  assert.equal($('vitalActivity').querySelector('strong').querySelector('.vital-age').textContent, '20s', 'the age is kept out of the uppercase label');
  // Tighter still: the Mac chip drops to its tiny form. Titles and aria labels keep the long text throughout.
  budget.vitalMac = 110;
  api.renderVitals();
  assert.deepEqual(chip('vitalMac'), ['Mac', 'Activity unknown']);
  assert.equal($('vitalMac').title, 'This Mac: Loaded unknown · activity unknown · GPU 46%');
  // Narrow windows start from the short forms.
  view.width = 800; budget.vitalMac = 400;
  api.renderVitals();
  assert.deepEqual(chip('vitalMac'), ['Mac', 'Activity unknown · GPU 46%']);
});

test('Round 3 (LOW): the PC chip stops pulsing when paused, like the Activity chip', () => {
  const { api, $ } = harness();
  api.setFeed({ ...pcSnapshot(nowSeconds(), { inFlight: [deepJob] }), windowsJobs: { schemaVersion: 1, journal: 'recorded', complete: true, inFlight: [deepJob], recent: [], lastSuccess: null } });
  api.renderVitals();
  assert.equal($('vitalPc').dataset.tone, 'live');
  api.setPaused(true);
  api.renderVitals();
  assert.deepEqual([$('vitalPc').dataset.tone, $('vitalActivity').dataset.tone], ['ok', 'muted']);
});

test('Round 3 (HIGH 1, MEDIUM, LOW): panel footers never inherit page-footer rules; the compact sheet, map text and chrome text sizes', () => {
  const rules = cssRules(css);
  // Every page-footer rule is scoped to the page footer; nothing styles a bare `footer`.
  for (const [media, head] of rules)
    for (const selector of selectors(head)) assert.doesNotMatch(selector, /(^|[\s>+~])footer\b(?![-\w])/.test(selector) && !/^body>footer\b/.test(selector) ? /./ : /$^/, `@media ${media}: ${selector}`);
  assert.ok(rules.some(([media, head, body]) => /max-width:820px\),\(max-height:700px\)|max-width:820px\), \(max-height:700px\)/.test(media) && head === 'body>footer' && /display:none/.test(body)));
  const panelActions = rules.filter(([media, head]) => !media && selectors(head).includes('.panel-actions')).map(([, , body]) => body).join(';');
  for (const declaration of ['height:auto', 'justify-content:flex-start', 'font-size:inherit', 'color:inherit']) assert.ok(panelActions.includes(declaration), declaration);
  // Compact sheet: no grab handle or its padding, tiles in one row where they fit, the scope legend for screen readers only.
  const compact = rules.filter(([media]) => media === '(max-width:820px)');
  const has = (selector, pattern) => compact.some(([, head, body]) => selectors(head).includes(selector) && pattern.test(body));
  assert.ok(has('.evidence-rail:before', /display:none/));
  assert.ok(has('.evidence-rail', /padding-top:0/));
  assert.ok(has('.tiles', /grid-template-columns:repeat\(auto-fit,minmax\(128px,1fr\)\)/));
  assert.ok(has('#fixScopeNote', /display:none/));
  // Map text that renders at 4-6 px behind the open sheet steps aside; cluster headers counter-scale (no fixed size).
  assert.ok(has('body:has(.evidence-rail>:not([hidden])) #groups text', /visibility:hidden/));
  assert.ok(has('body:has(.evidence-rail>:not([hidden])) .node:not(.selected) .sub', /visibility:hidden/));
  assert.ok(!rules.some(([, head, body]) => selectors(head).includes('.cluster-label') && /font-size/.test(body)));
  assert.match(appSource, /label\.setAttribute\('font-size',`\$\{11\/camera\.z\}px`\)/);
  // No chrome text under 10 px: the last rule that sizes each of these (at any width) sets at least 10 px.
  for (const selector of ['.mode-tab-copy small', '.pc-switch-copy small', '.mode-tab-copy strong', '.pc-switch-copy strong', '.tools .fix-tab', '.vital strong', '.connection', '.map-controls span']) {
    const sized = rules.filter(([, head, body]) => selectors(head).includes(selector) && /font(-size)?:[^;]*\d+(\.\d+)?px/.test(body));
    const last = sized.at(-1);
    assert.ok(last && !last[0], `${selector}: the last size rule applies at every width`);
    assert.ok(Number(/(\d+(?:\.\d+)?)px/.exec(last[2].match(/font(?:-size)?:[^;]*/)[0])[1]) >= 10, `${selector} is under 10 px`);
  }
  // Readable measure; a warning dot for limit hits; the activity age outside the uppercase run.
  assert.ok(rules.some(([media, head, body]) => !media && selectors(head).includes('.evidence-rail .panel-body p') && /max-width:60ch/.test(body)));
  assert.ok(rules.some(([, head, body]) => head === '.activity-row.limit .activity-dot' && /background:var\(--orange\)/.test(body)));
  assert.ok(rules.some(([, head, body]) => head === '.vital-age' && /text-transform:none/.test(body)));
});

// Orb web wiring, 26-27 Sep 2026: the web layer, its cache, the poke and the heartbeat, against the same fake DOM with SVG
// elements, a fake animation frame, fake timers and a fake clock (nothing here waits on real time).
function webHarness({ reduced = false, forced = false, hiddenWeb = false } = {}) {
  const doc = { elements: new Map() };
  doc.body = new FakeElement(doc, 'body');
  doc.activeElement = doc.body;
  doc.createElement = tag => new FakeElement(doc, tag);
  doc.createElementNS = (ns, tag) => {
    const node = new FakeElement(doc, tag), set = node.setAttribute.bind(node);
    node.setAttribute = (name, value) => { set(name, value); if (name === 'class') node.className = String(value); };
    node.removeAttribute = name => { delete node.attributes[name]; };
    return node;
  };
  doc.getElementById = id => {
    if (!doc.elements.has(id)) { const node = new FakeElement(doc, 'div', id); doc.body.append(node); doc.elements.set(id, node); }
    return doc.elements.get(id);
  };
  let clock = 5000, nextId = 1;
  const frames = new Map(), timers = new Map(), media = { reduce: reduced, forced, hiddenWeb };
  const window = { innerWidth: 1150, matchMedia: query => ({ matches: query.includes('reduced-motion') ? media.reduce : query.includes('forced-colors') ? media.forced : false }),
    getComputedStyle: node => ({ display: node.id === 'web' && media.hiddenWeb ? 'none' : 'block' }),
    setWebHidden: value => { media.hiddenWeb = value; },
    requestAnimationFrame: f => { const id = nextId++; frames.set(id, f); return id; }, cancelAnimationFrame: id => frames.delete(id) };
  const api = runInNewContext(`${definitions}
;({renderWeb,renderGlows,pokeWeb,syncWebPulse,build(){makeGraph();},
  get graph(){return graph;},get motion(){return webMotion;},get layout(){return webLayout;},get paths(){return webPathEls;},
  setFeed(value,isConnected=true){snapshot=value;connected=isConnected;},setPaused(value){paused=value;},setWebHidden(value){window.setWebHidden(value);}})`,
  { ...layout, ...usage, ...onlineMode, ...modelControl, document: doc, window, performance: { now: () => clock },
    setTimeout: (f, ms) => { const id = nextId++; timers.set(id, { f, at: clock + ms }); return id; }, clearTimeout: id => timers.delete(id) });
  const runFrames = (limit = 400) => { let n = 0; while (frames.size && n++ < limit) { clock += 1000 / 60; const [id, f] = frames.entries().next().value; frames.delete(id); f(clock); } return n; };
  return { api, doc, $: id => doc.getElementById(id), frames, timers, media, runFrames, advance(ms) { clock += ms; }, get clock() { return clock; } };
}
const webFeed = job => ({ ...pcSnapshot(nowSeconds(), { inFlight: job ? [deepJob] : [] }), fullSampledAt: nowSeconds(),
  models: [{ id: 'google/gemma-4-12b', host: 'mac', state: job ? 'generating' : 'idle', loaded: true, ageSeconds: 0, metadata: { parameters: '12B' } }],
  clients: [{ id: 'claude', model: 'claude-opus-5-5', modelState: 'observed' }], activityKnown: true,
  windowsWorker: { state: 'advertised', ageSeconds: 4, modelsAdvertised: ['Qwen3.8-27B'], detail: 'x',
    lanes: { fast: { up: true, model: 'gpt-oss-20b', slotsBusy: 0, slotsTotal: 2 }, deep: { up: true, model: 'Qwen3.8-27B', slotsBusy: job ? 1 : 0, slotsTotal: 1 } } } });

test('Orb web: the glow and web layers sit first inside the camera viewport, decorative; the key lists evidence only', () => {
  assert.match(html, /<g id="viewport"><g id="glows" aria-hidden="true"><\/g><g id="web" aria-hidden="true"><\/g><g id="edges"><\/g><g id="groups"><\/g><g id="nodes"><\/g><\/g>/);
  // No bridge threads between hub webs (the 27 Sep review), so no key entry has to explain away a line that looks like a link.
  assert.doesNotMatch(html, /silk|decoration, not a link/);
  assert.doesNotMatch(css, /legend-line\.silk/);
  assert.match(html, /<span><i class="legend-line flow"><\/i>Windows job<\/span><span class="cap-key">/);
  // applyCamera moves every child of the old viewport into the new one, so both layers ride along without a rebuild.
  assert.match(appSource, /while\(old\.firstChild\)next\.append\(old\.firstChild\);old\.replaceWith\(next\);syncBeat\(\);/);
  // The marching-dash leftovers are gone: in-flight edges are dashed and still, so there is no dash phase to keep in sync.
  assert.doesNotMatch(appSource, /syncFlowPhase/);
  assert.doesNotMatch(css, /edge-flow/);
  // Zoomed far out, the small client and route webs (a few pixels across) step aside.
  assert.match(appSource, /WEB_MINOR_ZOOM=\.6;/);
  assert.match(appSource, /\$\('web'\)\?\.classList\.toggle\('far',camera\.z<WEB_MINOR_ZOOM\);/);
  assert.match(css, /#web\.far \.web-minor\{display:none\}/);
});

test('Orb web: hairline thread token mixed from the ink, rings fading outward, fillers fainter, contrast and forced colours handled, dark theme only', () => {
  const rule = selector => { const at = css.lastIndexOf(`${selector}{`); assert.ok(at >= 0, selector); return css.slice(at + selector.length + 1, css.indexOf('}', at)); };
  const opacity = selector => Number(/opacity:([.\d]+)/.exec(rule(selector))[1]);
  assert.match(css, /:root\{--web-thread:#[0-9a-f]{6};--web-dew:#[0-9a-f]{6};--glow-peak:\.22\}/);
  assert.match(css, /@supports \(color:color-mix\(in oklab,red,blue\)\)\{:root\{--web-thread:color-mix\(in oklab,var\(--ink\) 80%,var\(--blue\)\);/);
  assert.doesNotMatch(css.slice(css.indexOf('/* Orb web (spiderweb')), /light-dark\(/, 'no light-dark(): Safari before 17.5 lacks it');
  assert.match(rule('#web path'), /^fill:none;stroke:var\(--web-thread\);stroke-width:1;vector-effect:non-scaling-stroke;/);
  assert.ok(opacity('#web .ring-in') > opacity('#web .ring-mid') && opacity('#web .ring-mid') > opacity('#web .ring-out'), 'rings fade outward');
  assert.ok(opacity('#web .web-filler') < opacity('#web .web-spoke') && opacity('#web .web-spoke') <= .30, 'faint fillers, low-opacity spokes');
  // High contrast: the data edges and captions get stronger (the prefers-contrast block), the decorative web steps back.
  assert.match(css, /@media\(prefers-contrast:more\)\{#web \.web-ring,#web \.web-filler,#web \.web-spoke,#web \.web-bridge\{stroke-opacity:\.6\}\}/);
  assert.doesNotMatch(css, /prefers-contrast:more\)\{#web[^}]*\{opacity/, 'never raised under high contrast');
  // Forced colours hide both layers and stop the hidden glows' animation (renderGlows builds none there either).
  assert.match(css, /@media\(forced-colors:active\)\{#web,#glows\{display:none\}#glows \.glow\{animation:none\}\.node\.model\.active \.halo\{filter:none\}\}/);
  // The monitor ships one dark theme; the stylesheet says so instead of claiming a light palette that does not exist.
  assert.match(css, /:root\{--paper:#000;[^}]*color-scheme:dark;/);
  assert.match(css, /\.graph-region\{position:absolute;inset:0;background:#000\}/);
  assert.match(css, /The monitor ships one dark theme \(color-scheme:dark; there is no light\n   palette\)/);
  assert.doesNotMatch(css, /A light palette sets|prefers-color-scheme/);
  assert.match(css, /#glows,#web\{pointer-events:none\}/);
  assert.match(css, /#map\.map-focus-active #web\{opacity:\.45\}\n#map\.map-focus-active #web\.plucking\{opacity:1\}/);
  assert.match(css, /\.node \.label,\.node \.sub,\.node \.capability-line\{stroke-width:5px\}/);
});

test('Orb web: nothing travels along a thread; reduced motion, pause and a stale feed stop the glow heartbeat and the dew; halos and glows share the 2 s beat', () => {
  assert.doesNotMatch(appSource + css, /web-glide|animateMotion/, 'the gliding drop is gone');
  assert.match(css, /\.paused #web \.web-dew,\.disconnected #web \.web-dew\{display:none\}/);
  assert.match(css, /@media\(prefers-reduced-motion:reduce\)\{#web\{transition:none\}\}/);
  assert.match(css, /\.node\.active \.halo,\.node\.in-flight \.halo\{transform-box:fill-box;transform-origin:center;animation:breathe 2s ease-out infinite\}/);
  assert.match(css, /#glows \.glow\{opacity:\.4;animation:glow-beat 2s ease-in-out infinite\}/);
  assert.match(css, /@keyframes glow-beat\{0%\{opacity:\.4\}28%\{opacity:1\}100%\{opacity:\.4\}\}/);
  assert.match(css, /\.paused #glows \.glow,\.disconnected #glows \.glow\{animation:none;opacity:\.2\}/);
  assert.match(css, /@media\(prefers-reduced-motion:reduce\)\{#glows \.glow\{animation:none;opacity:\.5\}\}/);
  // The older halo rules still stop the ring when paused, stale or under reduced motion (they win on !important or specificity).
  assert.match(css, /\.paused \.halo,\.disconnected \.halo\{animation:none!important;opacity:\.1!important\}/);
  assert.match(css, /@media\(prefers-reduced-motion:reduce\)\{\.node\.active \.halo\{animation:none!important\}\}/);
  assert.match(css, /@media\(prefers-reduced-motion:reduce\)\{\.node\.in-flight \.halo\{animation:none!important\}\}/);
  assert.match(css, /\.edge\.in-flight\{stroke-dasharray:6 6;opacity:\.95;stroke-width:2\.2\}/);
  // Each model halo and its glow align to their own phase cadence; other live nodes keep the 2 s default.
  assert.match(appSource, /const BEAT_MS=2000,BEAT_ANIMATIONS=new Set\(\['breathe','glow-beat'\]\);/);
  assert.match(appSource, /period=Number\.isFinite\(duration\)&&duration>0\?duration:BEAT_MS,aligned=Math\.floor\(start\/period\)\*period;/);
  assert.match(appSource, /applyCamera\(\);renderModelAction\(\);syncWebPulse\(\);syncBeat\(\);\n\}/);
  assert.match(appSource, /document\.body\.classList\.toggle\('disconnected',!fresh\(\)\);syncWebPulse\(\);/);
});

test('Orb web: click, tap, Enter and Space on a star select it and poke the web exactly once', () => {
  assert.match(appSource, /g\.addEventListener\('click',\(\)=>\{if\(!justDragged\)\{nodeSelect\(n\.id\);pokeWeb\(n\.id\);\}\}\);/);
  assert.match(appSource, /if\(e\.key==='Enter'\|\|e\.key===' '\)\{e\.preventDefault\(\);nodeSelect\(n\.id\);pokeWeb\(n\.id\);\}/);
  assert.equal((appSource.match(/pokeWeb\(n\.id\)/g) || []).length, 2);
});

test('Orb web: built once per layout; stream ticks and hover renders reuse the same thread elements', () => {
  const w = webHarness();
  w.api.setFeed(webFeed(true)); w.api.build(); w.api.renderWeb();
  const layer = w.$('web'), first = [...layer.children];
  assert.equal(layer.dataset.builds, '1');
  assert.equal(Number(layer.dataset.paths), w.api.layout.pathCount);
  assert.equal(first.filter(child => child.tagName === 'PATH' && /^web-(ring|filler|spoke|bridge)/.test(child.className)).length, w.api.layout.pathCount);
  const layout = w.api.layout;
  // Stream ticks (a new graph object each time, status and ages changing) and hover re-renders of the same graph.
  for (let tick = 0; tick < 6; tick++) { const feed = webFeed(true); feed.windowsJobs.inFlight[0].ageSeconds += tick; w.api.setFeed(feed); w.api.build(); w.api.renderWeb(); w.api.renderWeb(); }
  assert.equal(layer.dataset.builds, '1', 'status-only changes never rebuild the web');
  assert.equal(w.api.layout, layout);
  assert.ok(first.slice(0, layout.pathCount).every(path => layer.children.includes(path)), 'the same elements stay in place');
  // The job ending drops its client-to-lane link: a topology change, one rebuild, then cached again.
  for (let tick = 0; tick < 3; tick++) { w.api.setFeed(webFeed(false)); w.api.build(); w.api.renderWeb(); }
  assert.equal(layer.dataset.builds, '2');
  // A node that moves (a resize re-layout) rebuilds it.
  w.api.build(); w.api.graph.nodes[1].x += 12; w.api.renderWeb();
  assert.equal(layer.dataset.builds, '3');
});

test('Orb web: a poke plucks through one frame loop that restarts on a second poke, settles back to rest and stops', () => {
  const w = webHarness();
  w.api.setFeed(webFeed(false)); w.api.build(); w.api.renderWeb();
  const spokes = w.api.paths.get('spokes:runtime'), rest = spokes.getAttribute('d');
  w.api.pokeWeb('runtime');
  assert.equal(w.frames.size, 1);
  w.runFrames(12);
  const moving = spokes.getAttribute('d');
  assert.notEqual(moving, rest, 'mid-pluck the Mac spokes are displaced');
  assert.equal(moving.replace(/[^A-Z]/g, ''), rest.replace(/[^A-Z]/g, ''), 'same commands while moving');
  assert.ok(w.$('web').classList.contains('plucking'), 'the web is lifted out of the selection quiet while plucked');
  w.api.pokeWeb('runtime'); w.api.pokeWeb('runtime');
  assert.equal(w.frames.size, 1, 'pokes restart, they do not stack loops');
  const frames = w.runFrames();
  assert.ok(frames > 70 && frames < 120, `settles in about 1.5 s: ${frames} frames`);
  assert.equal(w.frames.size, 0);
  assert.equal(spokes.getAttribute('d'), rest, 'back to rest exactly');
  assert.equal(w.$('web').classList.contains('plucking'), false);
  assert.ok([...w.api.paths].every(([key, path]) => path.getAttribute('d') === w.api.layout.paths.find(p => p.key === key).d));
  assert.equal(w.timers.size, 0, 'an idle map arms no heartbeat');
});

test('Orb web: reduced motion or a paused feed turns a poke into one brief highlight; forced colours do nothing', () => {
  for (const setup of [{ reduced: true }, { paused: true }]) {
    const w = webHarness({ reduced: setup.reduced });
    w.api.setFeed(webFeed(false)); w.api.build(); w.api.renderWeb();
    if (setup.paused) w.api.setPaused(true);
    w.api.pokeWeb('client:claude');
    const flash = w.$('web').querySelector('.web-flash');
    assert.equal(w.frames.size, 0, JSON.stringify(setup));
    assert.ok(flash.classList.contains('on') && /^M/.test(flash.getAttribute('d')) && w.$('web').classList.contains('plucking'));
    const [timer] = w.timers.values(); w.advance(650); timer.f();
    assert.equal(flash.classList.contains('on') || w.$('web').classList.contains('plucking'), false, 'the highlight clears after 650 ms');
  }
  const w = webHarness({ forced: true });
  w.api.setFeed(webFeed(true)); w.api.build(); w.api.renderWeb(); w.api.pokeWeb('runtime'); w.api.syncWebPulse();
  assert.deepEqual([w.frames.size, w.timers.size, w.$('web').querySelector('.web-flash').getAttribute('d')], [0, 0, '']);
});

test('Orb web: a live job with motion allowed puts nothing on the threads; reduced motion gets static dew; forced colours build no glows', () => {
  const w = webHarness();
  w.api.setFeed(webFeed(true)); w.api.build(); w.api.renderWeb(); w.api.renderGlows();
  assert.equal(w.$('web').querySelector('.web-live').children.length, 0, 'no drop and no dew: the halo, glow and heartbeat carry the job');
  assert.ok(w.$('glows').querySelectorAll('.glow').length > 0, 'the glows are built');
  const reduced = webHarness({ reduced: true });
  reduced.api.setFeed(webFeed(true)); reduced.api.build(); reduced.api.renderWeb();
  assert.deepEqual(reduced.$('web').querySelector('.web-live').children.map(child => child.className), ['web-dew']);
  const forced = webHarness({ forced: true });
  forced.api.setFeed(webFeed(true)); forced.api.build(); forced.api.renderWeb(); forced.api.renderGlows();
  assert.equal(forced.$('glows').children.length, 0, 'the layer is hidden there, so no glow (and no glow animation) is built');
  assert.equal(forced.$('web').querySelector('.web-live').children.length, 0);
});

test('a verified model aura follows busy to generating cadence and stops when activity is paused or idle', () => {
  const w = webHarness();
  const modelGlow = () => w.$('glows').querySelectorAll('.glow').find(node => node.getAttribute('data-node')?.startsWith('model:'));
  const busy = webFeed(true); busy.models[0].state = 'busy';
  w.api.setFeed(busy); w.api.build(); w.api.renderGlows();
  assert.equal(w.api.graph.nodes.find(node => node.kind === 'model').auraPeriodMs, 2400);
  assert.equal(modelGlow()?.getAttribute('style'), 'animation-duration:2400ms');
  const first = modelGlow();
  const generating = webFeed(true);
  w.api.setFeed(generating); w.api.build(); w.api.renderGlows();
  assert.equal(w.api.graph.nodes.find(node => node.kind === 'model').auraPeriodMs, 1200);
  assert.equal(modelGlow()?.getAttribute('style'), 'animation-duration:1200ms');
  assert.notEqual(modelGlow(), first, 'the phase change refreshes the glow cadence');
  w.api.setPaused(true); w.api.build(); w.api.renderGlows();
  assert.equal(modelGlow(), undefined, 'a paused model has no active aura');
  w.api.setPaused(false); w.api.setFeed(webFeed(false)); w.api.build(); w.api.renderGlows();
  assert.equal(modelGlow(), undefined, 'an idle model has no active aura');
  assert.match(appSource, /halo\.setAttribute\('style',n\.auraPeriodMs\?`animation-duration:\$\{n\.auraPeriodMs\}ms`:''\)/);
});

test('Orb web: a live job arms one heartbeat on the next 2 s boundary; pause, a stale feed or reduced motion disarm it', () => {
  const w = webHarness();
  w.api.setFeed(webFeed(true)); w.api.build(); w.api.renderWeb(); w.api.syncWebPulse(); w.api.syncWebPulse();
  assert.equal(w.timers.size, 1, 'one beat timer');
  assert.equal([...w.timers.values()][0].at % 2000, 0, 'on the shared 2 s clock');
  const [timer] = w.timers.values(); w.advance(timer.at - w.clock); w.timers.clear(); timer.f();
  assert.equal(w.api.motion.state.beat, true);
  const lane = w.api.paths.get('spokes:windows-worker'), rest = lane.getAttribute('d');
  w.runFrames(8);
  assert.notEqual(lane.getAttribute('d'), rest, 'the PC web wobbles on the beat');
  w.api.setPaused(true); w.api.syncWebPulse();
  assert.deepEqual([w.frames.size, w.timers.size, lane.getAttribute('d')], [0, 0, rest], 'pause: nothing scheduled, back to rest');
  w.api.setPaused(false); w.api.syncWebPulse();
  assert.equal(w.timers.size, 1);
  w.media.reduce = true; w.api.syncWebPulse();
  assert.equal(w.timers.size, 0, 'reduced motion: no heartbeat wobble');
  w.media.reduce = false; w.api.syncWebPulse(); assert.equal(w.timers.size, 1);
  w.api.setFeed(webFeed(true), false); w.api.syncWebPulse();
  assert.equal(w.timers.size, 0, 'a lost feed disarms it');
});

test('Orb web: CSS hiding stops heartbeat and poke motion while preserving static edge layout', () => {
  const w = webHarness();
  w.api.setFeed(webFeed(true)); w.api.build(); w.api.renderWeb();
  assert.ok(w.api.layout.pathCount > 0, 'the cached layout remains available for static edge bends');
  assert.equal(w.api.layout.edgeBends.length, w.api.graph.edges.length);
  w.api.syncWebPulse();
  assert.equal(w.timers.size, 1, 'visible web retains its heartbeat');
  const [timer] = w.timers.values(); w.advance(timer.at - w.clock); w.timers.clear(); timer.f();
  assert.equal(w.frames.size, 1, 'visible web starts its heartbeat animation');
  w.api.setWebHidden(true); w.api.syncWebPulse();
  assert.deepEqual([w.frames.size, w.timers.size], [0, 0], 'hiding the web cancels scheduled work');
  assert.equal(w.api.motion.state.pulse, false, 'the hidden heartbeat is disarmed');
  assert.equal(w.api.motion.state.framePending, false, 'the queued frame is canceled');
  w.api.pokeWeb('runtime');
  assert.deepEqual([w.frames.size, w.timers.size], [0, 0], 'hidden web does not start a poke');
  assert.equal(w.$('web').querySelector('.web-flash').getAttribute('d'), '', 'hidden web does not build a flash path');
  w.api.setWebHidden(false); w.api.syncWebPulse();
  assert.equal(w.timers.size, 1, 'showing the web restores its live heartbeat');
});

// Fix Nisi + Jev and memory, 27 Sep 2026: the inspector's recovery summary and sticky action, the Fix panel's fourth
// scope and plain step list, and the memory tile, section, chip word and banner, run against the fake DOM.

const pendingFeed = (pipeline = {}, extra = {}) => ({ ...pcSnapshot(nowSeconds(), {}),
  pipeline: { status: 'recovery-required', recoveryRequired: true, pendingMarkerObserved: true, runId: null, stage: null, ...pipeline },
  components: [{ id: 'nisi', state: 'unresolved', detail: 'Nisi pending marker present; the owner must recover it' }, { id: 'jev', state: 'configured', detail: 'Opted in' }], ...extra });
const tiles = body => [...body.querySelector('.tiles').children].map(tile => [tile.querySelector('.tile-label').textContent, tile.querySelector('.tile-value').textContent, tile.className]);

test('Mac inspector: unavailable or incomplete inventory keeps the loaded count unknown', () => {
  const unavailable = [{ id: 'lmstudio-api', state: 'unavailable' }, { id: 'lms-ps', state: 'unavailable' }];
  const model = { id: 'local-model', host: 'mac', loaded: null, state: 'unknown', ageSeconds: 0 };
  for (const [sources, models] of [[unavailable, []], [[], []],
    [[{ id: 'lmstudio-api', state: 'recorded' }, unavailable[1]], []],
    [[...unavailable, { id: 'amd-models', state: 'live' }], []],
    [[{ id: 'lms-ps', state: 'live' }], [model]], [unavailable, [{ ...model, loaded: true }]]]) {
    const { api, body } = drawerHarness();
    api.setFeed({ ...pcSnapshot(nowSeconds(), {}), sources, models });
    api.select('runtime');
    api.renderInspector();
    assert.equal(tiles(body).find(([label]) => label === 'Loaded')[1], 'Unknown', JSON.stringify({ sources, models }));
  }
});

test('Mac inspector: a complete live inventory retains known zero and nonzero loaded counts', () => {
  for (const id of ['lmstudio-api', 'lms-ps']) {
    for (const models of [[], [{ id: 'local-model', host: 'mac', loaded: true, state: 'idle', ageSeconds: 0 }]]) {
      const { api, body } = drawerHarness();
      api.setFeed({ ...pcSnapshot(nowSeconds(), {}), sources: [{ id, state: 'live' }], models });
      api.select('runtime');
      api.renderInspector();
      assert.equal(tiles(body).find(([label]) => label === 'Loaded')[1], String(models.length), id);
    }
  }
});

for (const host of ['mac', 'windows']) {
  test(`${host} inspector: incomplete inventory cannot claim zero or Quiet`, () => {
    const id = host === 'windows' ? 'lane-inventory' : 'lmstudio-api';
    for (const [sources, models] of [[[], []], [[{ id, state: 'unavailable' }], []],
      [[{ id, state: 'live' }], [{ id: 'local-model', host, loaded: null, state: 'unknown', ageSeconds: 0 }]]]) {
      const { api, body } = drawerHarness();
      api.setFeed({ ...pcSnapshot(nowSeconds(), {}), host, activityKnown: true, sources, models });
      api.select('runtime'); api.renderInspector();
      assert.equal(tiles(body).find(([label]) => label === 'Loaded')[1], 'Unknown');
      assert.equal(body.querySelector('.panel-summary').querySelector('strong').textContent, 'Activity unknown');
    }
  });
  test(`${host} inspector: complete live inventory permits Quiet and exact loaded counts`, () => {
    for (const loaded of [false, true]) {
      const { api, body } = drawerHarness();
      api.setFeed({ ...pcSnapshot(nowSeconds(), {}), host, activityKnown: true,
        sources: [{ id: host === 'windows' ? 'lane-inventory' : 'lmstudio-api', state: 'live' }],
        models: [{ id: 'local-model', host, loaded, state: 'idle', ageSeconds: 0 }] });
      api.select('runtime'); api.renderInspector();
      assert.equal(tiles(body).find(([label]) => label === 'Loaded')[1], String(Number(loaded)));
      assert.equal(body.querySelector('.panel-summary').querySelector('strong').textContent, `Quiet · ${Number(loaded)} loaded`);
    }
  });
}

test('Runtime inspector: caller wording follows source evidence rather than assuming idle', () => {
  for (const [state, detail] of [['unavailable', 'Not checked while local model activity is unknown'],
    ['error', 'Could not list local callers'], ['unavailable', 'Direct model-server port is not verified']]) {
    const { api, body } = drawerHarness();
    api.setFeed({ ...pcSnapshot(nowSeconds(), {}), localCallers: null,
      sources: [{ id: 'local-callers', state, detail }] });
    api.select('runtime'); api.renderInspector();
    assert.ok(body.textContent.includes(`Local callers${detail}`));
  }
});

// The skipped-probe source state is A4's 'idle' or A1's 'not-checked' (the integrated
// collector emits the latter); the UI treats both the same way.
const SKIPPED_CALLER_SOURCES = [['idle', 'Not checked while local models are idle'],
  ['not-checked', 'Not sampled; no local model requests observed']];

test('Mac runtime: incomplete residency stays unknown in summary, callers and every chip form', () => {
  for (const [state, detail] of SKIPPED_CALLER_SOURCES) {
    const { api, $, body } = drawerHarness();
    api.setFeed({ ...pcSnapshot(nowSeconds(), {}), activityKnown: true, localCallers: null,
      sources: [{ id: 'lmstudio-api', state: 'live' }, { id: 'lms-ps', state: 'live' },
        { id: 'local-callers', label: 'Local callers', state, detail }],
      models: [{ id: 'example-model', host: 'mac', loaded: null, state: 'unknown', ageSeconds: 0 }],
      macGpu: { utilizationPercent: 40, ageSeconds: 0 } });
    api.renderSummary(); api.select('runtime'); api.renderInspector();
    assert.equal($('headline').querySelector('em').textContent, 'activity unknown.', state);
    assert.equal($('activeCount').textContent, '—', state);
    assert.equal($('loadedCount').textContent, '—', state);
    assert.equal($('queuedCount').textContent, '—', state);
    assert.match(body.textContent, /Local callersUnknown/, state);
    assert.doesNotMatch(body.textContent + $('sources').textContent, /idle|quiet|None connected|not sampled|no local model requests/i, state);
    assert.match($('sources').textContent, /Local callersunavailableLocal model activity could not be verified/, state);
    const chip = api.vitalsInput().vitals;
    assert.doesNotMatch(JSON.stringify([chip.mac, chip.tiny.mac]), /idle/i, state);
    assert.equal(chip.mac.detail, 'Loaded unknown · activity unknown · GPU 40%', state);
  }
});

test('Mac runtime: complete idle evidence permits idle caller wording', () => {
  for (const [state, detail] of SKIPPED_CALLER_SOURCES) {
    const { api, body } = drawerHarness();
    api.setFeed({ ...pcSnapshot(nowSeconds(), {}), activityKnown: true, localCallers: null,
      sources: [{ id: 'lms-ps', state: 'live' },
        { id: 'local-callers', state, detail }], models: [] });
    api.select('runtime'); api.renderInspector();
    assert.ok(body.textContent.includes(`Local callers${detail}`), state);
  }
});

test('Runtime inspector: a positive partial activity count is a lower bound in the headline and tiles', () => {
  const { api, $, body } = drawerHarness();
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), activityKnown: true,
    sources: [{ id: 'lmstudio-api', state: 'live' }, { id: 'lms-ps', state: 'live' }],
    models: [{ id: 'a', host: 'mac', loaded: true, state: 'generating', source: 'lms-ps', ageSeconds: 0 },
      { id: 'b', host: 'mac', loaded: null, state: 'unknown', ageSeconds: 0 }] });
  api.renderSummary(); api.select('runtime'); api.renderInspector();
  assert.equal(body.querySelector('.panel-summary').querySelector('strong').textContent, 'At least 1 model working');
  assert.equal(tiles(body).find(([label]) => label === 'Working')[1], 'at least 1');
  assert.match(body.textContent, /Working modelsat least 1/);
  assert.equal($('activeCount').textContent, 'at least 1');
  assert.equal($('queuedCount').textContent, '—');
});

test('Runtime counts: catalog aggregates do not count alongside loaded alias instances', () => {
  const { api, $, body } = drawerHarness();
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), activityKnown: true,
    sources: [{ id: 'lmstudio-api', state: 'live' }, { id: 'lms-ps', state: 'live' }],
    models: [{ id: 'provider/model', host: 'mac', loaded: true, state: 'generating', queued: 5, representedByInstances: true, ageSeconds: 0 },
      { id: 'alias-1', host: 'mac', loaded: true, state: 'generating', queued: 2, ageSeconds: 0 }] });
  api.renderSummary(); api.select('runtime'); api.renderInspector();
  assert.equal($('loadedCount').textContent, '1');
  assert.equal($('activeCount').textContent, '1');
  assert.equal($('queuedCount').textContent, '2');
  assert.equal(tiles(body).find(([label]) => label === 'Loaded')[1], '1');
  assert.equal(tiles(body).find(([label]) => label === 'Working')[1], '1');
  assert.equal(body.querySelector('.panel-summary').querySelector('strong').textContent, '1 model working');
});

for (const host of ['mac', 'windows']) {
  test(`${host} runtime: invalid caller names and capped lists stay visible as connected processes`, () => {
    for (const [localCallers, expected] of [[[{ name: ' ' }, { name: '\n' }], 'unknown process, unknown process'],
      [Array.from({ length: 9 }, (_, i) => ({ name: `process${i}` })), 'process0, process1, process2, process3, process4, process5 +3 more']]) {
      const { api, body } = drawerHarness();
      api.setFeed({ ...pcSnapshot(nowSeconds(), {}), host, localCallers });
      api.select('runtime'); api.renderInspector();
      assert.ok(body.textContent.includes(`Local callers${expected}`));
      assert.ok(body.textContent.includes(`Connected to the model server: ${expected}`));
      assert.doesNotMatch(body.textContent, /None connected|calling the model server/i);
    }
  });
}

test('Sources: stale and paused samples cannot report idle state or zero live sources', () => {
  for (const mode of ['stale', 'paused']) {
    const { api, $ } = harness();
    api.setFeed({ ...pcSnapshot(nowSeconds() - (mode === 'stale' ? 60 : 0), {}), sources: [
      { id: 'lms-ps', label: 'Runtime', state: 'live', detail: 'Live inventory' },
      { id: 'local-callers', label: 'Local callers', state: 'idle', detail: 'Not checked while local models are idle' },
      { id: 'record', label: 'Recorded', state: 'recorded', detail: 'Recorded inventory' },
      { id: 'failed', label: 'Failure', state: 'error', detail: 'Failed to read' },
      { id: 'missing', label: 'Missing', state: 'unavailable', detail: 'Missing file' }] });
    api.setPaused(mode === 'paused'); api.renderSummary();
    assert.equal($('sourceCount').textContent, `live unknown (${mode}) · 1 recorded`);
    assert.deepEqual($('sources').children.map(row => row.querySelector('span').textContent),
      ['stale', 'stale', 'stale', 'error', 'unavailable']);
    assert.doesNotMatch($('sources').textContent, /\bidle\b/);
    assert.match($('sources').textContent, /Current state unknown/);
  }
});

test('Runtime inspector: absent, stale and paused caller evidence stays unknown', () => {
  for (const mode of ['absent', 'stale', 'paused']) {
    const { api, body } = drawerHarness();
    api.setFeed({ ...pcSnapshot(nowSeconds() - (mode === 'stale' ? 60 : 0), {}), localCallers: mode === 'absent' ? null : [],
      sources: mode === 'absent' ? [] : [{ id: 'local-callers', state: 'idle', detail: 'Not checked while local models are idle' }] });
    api.setPaused(mode === 'paused'); api.select('runtime'); api.renderInspector();
    assert.match(body.textContent, /Local callersUnknown/);
    assert.doesNotMatch(body.textContent, /Not checked while local models are idle|None connected/);
  }
});

test('Fix Nisi + Jev: a pending Nisi record leads the Nisi + Jev inspector with its age and owner; its footer offers the fix', () => {
  const { api, $, body } = drawerHarness();
  api.setFeed(pendingFeed({ pendingMarkerAgeSeconds: 33000, pendingMarkerOwner: 'anonymous (legacy)' }));
  api.select('pipeline');
  api.renderInspector();
  const summary = body.querySelector('.panel-summary');
  assert.equal(summary.dataset.tone, 'warn');
  assert.equal(summary.querySelector('strong').textContent, 'Nisi call left a pending record 9 h 10 min ago');
  assert.match(summary.querySelector('p').textContent, /^Owner: anonymous \(legacy\)\. New Nisi work is refused until it is recovered\./);
  assert.deepEqual(tiles(body).map(([label, value]) => [label, value]), [['Route', 'Recovery required'], ['Nisi', 'Unresolved'], ['Jev', 'Configured'], ['Recovery', 'Pending 9 h 10 min']]);
  assert.match(body.textContent, /Pending record age9 h 10 minPending record owneranonymous \(legacy\)/);
  // The sticky footer: Fix Nisi + Jev is the primary action; the call-trace jump steps back beside it.
  assert.deepEqual([$('drawerJump').hidden, $('drawerFixButton').hidden, $('drawerJumpButton').classList.contains('panel-primary'), $('drawerJumpButton').textContent],
    [false, false, false, 'Call traces →']);
  assert.match(html, /<div id="drawerJump" class="panel-actions" hidden><button id="drawerFixButton" class="panel-primary drawer-fix" type="button" title="Opens Fix inference\. Nothing runs until you press Run check\." hidden>Fix Nisi Inference<\/button><button id="drawerJumpButton" class="panel-primary" type="button">Explore call traces →<\/button><\/div>/);
  // Without the snapshot's marker fields: the same words, no numbers.
  api.setFeed(pendingFeed());
  api.render();
  const plain = body.querySelector('.panel-summary');
  assert.equal(plain.querySelector('strong').textContent, 'Nisi call left a pending record');
  assert.doesNotMatch(plain.textContent, /\d/);
  assert.equal($('drawerFixButton').hidden, false);
  // A Windows observer reads the same summary but cannot run the fix.
  api.setFeed({ ...pendingFeed(), host: 'windows' });
  api.render();
  assert.equal(body.querySelector('.panel-summary').querySelector('strong').textContent, 'Nisi call left a pending record');
  assert.deepEqual([$('drawerFixButton').hidden, $('drawerJumpButton').classList.contains('panel-primary'), $('drawerJumpButton').textContent], [true, true, 'Explore call traces →']);
  // The Windows observer's technical note does not point at a Fix button it does not have.
  const note = [...body.querySelectorAll('.detail-note')].map(p => p.textContent).join(' ');
  assert.doesNotMatch(note, /Fix Nisi Inference opens the Fix panel/);
  api.setFeed(pendingFeed());
  api.render();
  assert.match([...body.querySelectorAll('.detail-note')].map(p => p.textContent).join(' '), /Fix Nisi Inference opens the combined Fix inference panel; nothing runs until you press Run check\./);
  // An open router run with the router's own marker blocks the fix: the summary says so in the route chip's words, the fix
  // steps back to a secondary action and the call traces (where that run is) lead again.
  api.setFeed(pendingFeed({ runId: 'route-20260927-011504', stage: 'review', pendingMarkerAgeSeconds: 7260, pendingMarkerOwner: 'route-20260927-011504' }));
  api.render();
  const blocked = body.querySelector('.panel-summary');
  assert.deepEqual([blocked.dataset.tone, blocked.querySelector('strong').textContent], ['warn', 'Unsettled run recorded']);
  assert.match(blocked.querySelector('p').textContent, /^Route run route-20260927-011504 is still open, and a Nisi call left a pending record 2 h 1 min ago\. Fix Nisi Inference stops at its first step until that run is resolved/);
  assert.deepEqual([$('drawerFixButton').hidden, $('drawerFixButton').classList.contains('panel-primary'), $('drawerJumpButton').classList.contains('panel-primary'), $('drawerJumpButton').textContent],
    [false, false, true, 'Explore call traces →']);
  api.renderVitals();
  assert.equal($('vitalRoute').querySelector('small').textContent, 'Unsettled run recorded', 'chip and inspector agree');
  api.select('pipeline');
  assert.equal(api.nodeSubtitle('pipeline'), 'UNSETTLED RUN RECORDED', 'and so does the node');
  // No open run: the fix leads again.
  api.setFeed(pendingFeed());
  api.render();
  assert.deepEqual([$('drawerFixButton').classList.contains('panel-primary'), $('drawerJumpButton').classList.contains('panel-primary')], [true, false]);
  // A verified running route, a settled route and a stale feed offer nothing.
  for (const feed of [pendingFeed({ status: 'running', runId: 'route-1', stage: 'backend_draft' }),
    { ...pcSnapshot(nowSeconds(), {}), pipeline: { status: 'idle' }, components: [{ id: 'nisi', state: 'ready', detail: 'pair' }] },
    { ...pendingFeed(), sampledAt: nowSeconds() - 30 }]) {
    api.setFeed(feed);
    api.render();
    assert.equal($('drawerFixButton').hidden, true);
    assert.equal($('drawerJump').hidden, false, 'the call-trace jump stays');
  }
  // A queued route (converge, Sol #8: its run lock is held while it waits for a lane) reads as queued in the chip,
  // the inspector and the node, never as an unsettled run, and offers no fix.
  api.setFeed(pendingFeed({ status: 'queued', runId: 'run-q', stage: null, queuePhase: 'waiting', queueResource: 'mac-pair' }));
  api.select('pipeline');
  api.renderInspector();
  assert.equal($('drawerFixButton').hidden, true);
  assert.equal(body.querySelector('.panel-summary').querySelector('strong').textContent, 'Route queued for a lane');
  api.renderVitals();
  assert.equal($('vitalRoute').querySelector('small').textContent, 'Queued for a lane');
  assert.equal(api.nodeSubtitle('pipeline'), 'ROUTE QUEUED FOR A LANE');
  // p2-readers converge (Sol terminology): a run in admission waits for admission, not a lane; with no phase
  // the words say only "queued" (the chip, the inspector and the node agree).
  for (const [phase, inspector, chip, node] of [['admitting', 'Route being admitted', 'Being admitted', 'ROUTE BEING ADMITTED'],
    [undefined, 'Route queued', 'Queued', 'ROUTE QUEUED']]) {
    api.setFeed(pendingFeed({ status: 'queued', runId: 'run-q', stage: null, queuePhase: phase, queueResource: null }));
    api.select('pipeline');
    api.renderInspector();
    assert.equal(body.querySelector('.panel-summary').querySelector('strong').textContent, inspector);
    api.renderVitals();
    assert.equal($('vitalRoute').querySelector('small').textContent, chip);
    assert.equal(api.nodeSubtitle('pipeline'), node);
  }
});

test('Fix Nisi + Jev: while a Nisi record is pending, no other node\'s inspector offers the fix (mutant R2)', () => {
  const { api, $ } = drawerHarness();
  const feed = pendingFeed({ pendingMarkerAgeSeconds: 33000 }, { activityKnown: true,
    models: [{ id: 'google/gemma-4-12b', host: 'mac', state: 'idle', loaded: true, ageSeconds: 0 }],
    clients: [{ id: 'claude', model: 'claude-opus-5-5', modelState: 'observed' }] });
  api.setFeed(feed);
  for (const id of ['runtime', 'model:mac:google/gemma-4-12b', 'windows-worker', 'client:claude', 'client-model:claude:0']) {
    api.select(id);
    api.renderInspector();
    assert.equal($('drawer').hidden, false, id);
    assert.equal($('drawerFixButton').hidden, true, id);
  }
  api.select('pipeline');
  api.renderInspector();
  assert.equal($('drawerFixButton').hidden, false);
});

test('Fix Nisi + Jev: the footer styles its primary and secondary buttons; secondary borders keep 3:1 against the sheet; PC lane captions stay AA', () => {
  const rules = cssRules(css), rule = head => rules.filter(([media, h]) => !media && h === head).map(([, , body]) => body).join(';');
  assert.match(rule('.evidence-rail .panel-actions #drawerFixButton.panel-primary'), /background:var\(--orange\)/);
  assert.match(rule('.evidence-rail .panel-actions #drawerFixButton:not(.panel-primary)'), /border:1px solid #777777;background:#181818/);
  assert.match(rule('.evidence-rail #drawerJumpButton'), /border:1px solid #777777/);
  assert.match(rule('.evidence-rail .panel-actions button:not(.panel-primary)'), /border-color:#777777/);
  assert.ok(!rules.some(([, head]) => head === '.evidence-rail .panel-actions #drawerFixButton'), 'the orange is for the primary form only');
  const lin = v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; };
  const lum = hex => { const n = parseInt(hex.slice(1), 16); return .2126 * lin(n >> 16) + .7152 * lin((n >> 8) & 255) + .0722 * lin(n & 255); };
  const ratio = (a, b) => (Math.max(lum(a), lum(b)) + .05) / (Math.min(lum(a), lum(b)) + .05);
  const sheet = /--sheet:(#[0-9a-f]{6})/.exec(css)[1];
  assert.ok(ratio('#777777', sheet) >= 3, `border ${ratio('#777777', sheet).toFixed(2)}:1`);
  assert.match(css, /\.node\.windows-lane \.sub\{opacity:\.92\}/);
});

test('Nisi inspector opens combined Fix inference without submitting and focuses the panel', async () => {
  const { api, doc, $, fetches } = netHarness();
  api.setFeed(pendingFeed());
  api.openFixInference('nisi');
  await settle();
  assert.equal($('fixInferenceDetails').hidden, false);
  assert.equal($('fixInference').getAttribute('aria-expanded'), 'true');
  assert.equal(doc.activeElement, $('fixInferenceDetails'));
  assert.match($('fixInferenceResult').querySelector('p').textContent, /Mac runtime, Windows route and Nisi Inference/);
  assert.ok(fetches.every(url => url === '/api/online-code-mode/entry'));
  api.setAction({ status: 'running', operationId: 5, action: 'fix-all', message: 'Checking.', steps: [] });
  api.openFixInference('nisi');
  assert.equal($('fixInferenceSubmit').disabled, true);
  assert.equal($('fixInferenceResult').querySelector('p').textContent, 'Checking.');
  assert.ok(fetches.every(url => url === '/api/online-code-mode/entry'));
});

test('Fix Nisi + Jev: closing the Fix panel returns to the inspector it was opened from; the top-bar tab path returns to the tab', () => {
  const { api, doc, $ } = netHarness();
  const drawer = $('drawer'), body = $('inspector');
  drawer.append(body);
  api.setFeed(pendingFeed());
  const picked = api.spyRail();
  api.select('pipeline');
  drawer.hidden = false;
  api.renderInspector();
  api.openFixInference('nisi');
  assert.deepEqual([drawer.hidden, $('fixInferenceDetails').hidden], [true, false], 'the panel takes the inspector\'s place');
  api.closeFixInference();
  assert.deepEqual([...picked], ['pipeline']);
  assert.deepEqual([$('fixInferenceDetails').hidden, drawer.hidden, $('fixInference').getAttribute('aria-expanded')], [true, false, 'false']);
  assert.equal(doc.activeElement, $('drawerFixButton'), 'focus is back on the control that opened it');
  // Opened from the top-bar tab (or after any other close): back to the tab, no inspector reopened.
  api.openFixInference();
  api.closeFixInference();
  assert.deepEqual([picked.length, doc.activeElement], [1, $('fixInference')]);
  assert.match(appSource, /function hideFixInferenceDetails\(\)\{const panel=\$\('fixInferenceDetails'\);panel\.hidden=true;fixReturnTo=null;/);
  assert.match(appSource, /\$\('fixInferenceClose'\)\.addEventListener\('click',closeFixInference\);/);
  assert.match(appSource, /if\(event\.key==='Escape'\)\{\n    event\.preventDefault\(\);\n    closeFixInference\(\);/);
});

test('Fix Nisi + Jev: a result lists its steps in plain words beside (not inside) the live summary; other scopes do not', () => {
  const { api, $ } = harness();
  api.setFeed(pendingFeed());
  const steps = [{ name: 'route-status', result: 'idle' }, { name: 'nisi-status', result: 'recovery-required' },
    { name: 'marker', result: 'stale', evidence: 'kind=codemode.nisi.pending.v1; age=9 h 10 min', ageSeconds: '33000', inputSha256: '81dbab4f0000', owner: 'anonymous (legacy)' },
    { name: 'owner-lock', result: 'busy' }, { name: 'journal', result: 'written' }];
  api.setAction({ status: 'needs-action', operationId: 6, action: 'fix-nisi', message: 'A Nisi call is still running; not recovering.', steps });
  api.updateInferenceFix();
  const result = $('fixInferenceResult'), list = $('fixInferencePlainSteps');
  assert.deepEqual([result.querySelector('strong').textContent, result.querySelector('p').textContent, result.querySelector('small').textContent],
    ['Action needed', 'A Nisi call is still running; not recovering.', 'Scope: Nisi Inference. No model was called and nothing was sent to the PC.']);
  assert.equal(list.hidden, false);
  assert.deepEqual(list.children.map(li => [li.dataset.tone, li.children[0].textContent, li.children[1].textContent]), [
    ['ok', '✓', 'Router No route run is open'], ['info', '•', 'Nisi A call left a pending record'],
    ['ok', '✓', 'Pending record 9 h 10 min old, owner anonymous (legacy): old enough to recover'],
    ['warn', '!', 'Owner lock A Nisi call is still running; not recovering'], ['ok', '✓', 'Fix journal Result saved to the fix journal']]);
  assert.ok(list.children.every(li => li.children[0].getAttribute('aria-hidden') === 'true'), 'the mark is decoration; the words carry it');
  // The recorded lines stay one click away.
  assert.equal($('fixInferenceSteps').hidden, false);
  assert.equal($('fixInferenceStepsLabel').textContent, 'Check steps (5)');
  // Not a live region: the summary announces the outcome once, and the detailed steps follow it.
  assert.match(html, /<p id="fixScopeNote" class="scope-note">[^<]*<\/p><ol id="fixInferencePlainSteps" class="fix-step-list" aria-label="Nisi Inference steps" hidden><\/ol><details id="fixInferenceSteps"/);
  assert.doesNotMatch(html.slice(html.indexOf('id="fixInferenceResult"'), html.indexOf('<p id="fixScopeNote"')), /fixInferencePlainSteps/);
  api.setAction({ status: 'ready', operationId: 7, action: 'fix-nisi', message: 'Nisi + Jev ready. No Nisi marker needed recovery. No model inference was run.', steps: steps.slice(0, 1) });
  api.updateInferenceFix();
  assert.equal(result.querySelector('strong').textContent, 'Nisi Inference ready');
  assert.equal(list.children.length, 1);
  // Said once each: the status line says ready, the message says what happened, the scope note says no model ran.
  assert.deepEqual([result.querySelector('p').textContent, result.querySelector('small').textContent],
    ['No Nisi marker needed recovery.', 'Scope: Nisi Inference. No model was called and nothing was sent to the PC.']);
  // A recovery: neither the summary nor the recorded lines copy the input digest prefix or the recovered file name.
  const recovered = [...steps.slice(0, 3), { name: 'recover', result: 'acknowledged', evidence: 'file=recovered-5b0e9c1f.json; remoteInferenceStopped=NOT_OBSERVED' }];
  recovered[2] = { ...recovered[2], evidence: 'kind=codemode.nisi.pending.v1; age=9 h 10 min; input=81dbab4f0000; owner=anonymous (legacy)' };
  api.setAction({ status: 'ready', operationId: 8, action: 'fix-nisi', steps: recovered,
    message: 'Nisi + Jev ready. Recovered the Nisi marker through the launcher (age 9 h 10 min, owner anonymous (legacy), input 81dbab4f0000). No model inference was run.' });
  api.updateInferenceFix();
  assert.equal(result.querySelector('p').textContent, 'Recovered the Nisi marker through the launcher (age 9 h 10 min, owner anonymous (legacy)).');
  const recorded = $('fixInferenceStepRows').children.map(line => line.textContent);
  assert.deepEqual(recorded.slice(2), ['marker · stale · kind=codemode.nisi.pending.v1; age=9 h 10 min; owner=anonymous (legacy)', 'recover · acknowledged · remoteInferenceStopped=NOT_OBSERVED']);
  assert.doesNotMatch(result.textContent + recorded.join(' '), /81dbab4f0000|recovered-5b0e9c1f/);
  // The Online Code panel reads the same result the same way.
  api.updateOnlineCodeMode();
  assert.doesNotMatch($('onlineCodeMeaning').textContent + $('onlineCodeStepRows').textContent, /81dbab4f0000|recovered-5b0e9c1f/);
  assert.doesNotMatch($('onlineCodeMeaning').textContent, /^Nisi \+ Jev ready\./);
  api.setAction({ status: 'ready', operationId: 9, action: 'fix-nisi', message: 'Nisi + Jev ready. No Nisi marker needed recovery. No model inference was run.', steps: steps.slice(0, 1) });
  api.updateInferenceFix();
  // Another scope keeps only its recorded lines.
  api.setAction({ status: 'ready', operationId: 8, action: 'fix-route', message: 'Route verified.', steps: [{ name: 'windows-inference', result: 'verified' }] });
  api.updateInferenceFix();
  assert.deepEqual([list.hidden, list.children.length], [true, 0]);
});

const GiB = 2 ** 30;
const memoryBlock = (level, extra = {}) => ({ level, reasons: ['only 14% of memory available'], pressure: 2, availablePercent: 14.2, ramBytes: 64 * GiB,
  swapUsedBytes: Math.round(3.2 * GiB), compressedBytes: 9 * GiB, gpuAllocBytes: 30 * GiB,
  consumers: [{ group: 'llm-server', label: 'LM Studio', residentBytes: Math.round(21.4 * GiB), processCount: 4, gpuAllocBytes: 30 * GiB },
    { group: 'browser', label: 'Safari', residentBytes: 6 * GiB, processCount: 31 }, { group: 'ios-simulator', label: 'iOS Simulators', residentBytes: 3 * GiB, processCount: 60 },
    { group: 'other', label: 'Xcode', residentBytes: GiB, processCount: 1 }],
  paused: [], suggestions: ['Unload idle model qwen/qwen3.8-27b from the monitor', 'Shut down unused iOS Simulators (`xcrun simctl shutdown all`)'], ...extra });

test('Memory: the Mac hub inspector has a Memory tile and a Memory section with the three largest users and the suggestions', () => {
  const { api, body } = drawerHarness();
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), memory: memoryBlock('tight') });
  api.select('runtime');
  api.renderInspector();
  assert.deepEqual(tiles(body), [['Working', 'Unknown', 'tile'], ['Loaded', 'Unknown', 'tile'], ['Memory', 'Tight · 14% available · swap 3.2 GB', 'tile tile-wide'],
    ['GPU · all apps', 'Unknown', 'tile tile-wide']]);
  const section = body.querySelectorAll('details').find(box => box.dataset.key === 'runtime|memory');
  assert.ok(section);
  assert.equal(section.querySelector('summary').textContent, 'Memory · tight');
  assert.equal(section.open, true, 'tight memory opens its section on the first build');
  const text = section.textContent;
  const [users, tips] = section.querySelectorAll('ul');
  assert.deepEqual(users.children.map(li => li.textContent), ['LM Studio about 21 GB · 4 processes · Mac GPU allocation about 30 GB (all apps)', 'Safari about 6.0 GB · 31 processes', 'iOS Simulators about 3.0 GB · 60 processes']);
  assert.equal(users.children[0].children[0].textContent, 'LM Studio', 'the name leads in bold');
  assert.doesNotMatch(text, /Xcode/, 'three users at most');
  assert.deepEqual(tips.children.map(li => li.textContent), ['Unload idle model qwen/qwen3.8-27b from the monitor', 'Shut down unused iOS Simulators (xcrun simctl shutdown all)']);
  assert.match(text, /never quits an app or unloads a model; its suggestions are text only\. Model sizes are LM Studio inventory file sizes, not measured memory\./);
  assert.match(text, /GPU allocation \(all apps\)30\.0 GB/, 'the Mac-wide allocation row says it counts every app');
  // Closed by the viewer, it stays closed across refreshes of the same node.
  section.open = false;
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), memory: memoryBlock('tight', { consumers: memoryBlock('tight').consumers.slice(0, 1) }) });
  api.render();
  assert.equal(Boolean(body.querySelectorAll('details').find(box => box.dataset.key === 'runtime|memory').open), false);
  // Ok: closed, and users are measured only when memory is not ok.
  const fresh = drawerHarness();
  fresh.api.setFeed({ ...pcSnapshot(nowSeconds(), {}), memory: memoryBlock('ok', { consumers: [], suggestions: [], availablePercent: 48, swapUsedBytes: 0 }) });
  fresh.api.select('runtime');
  fresh.api.renderInspector();
  const okSection = fresh.body.querySelectorAll('details').find(box => box.dataset.key === 'runtime|memory');
  assert.deepEqual([Boolean(okSection.open), okSection.querySelector('summary').textContent], [false, 'Memory']);
  assert.match(okSection.textContent, /Measured only when memory is not OK\./);
  assert.equal(tiles(fresh.body)[2][1], 'OK · 48% available · no swap');
  // Unknown and stale read Unknown; an older server without the block adds no tile and no section.
  fresh.api.setFeed({ ...pcSnapshot(nowSeconds(), {}), memory: memoryBlock('unknown') });
  fresh.api.render();
  assert.equal(tiles(fresh.body)[2][1], 'Unknown');
  fresh.api.setFeed(pcSnapshot(nowSeconds(), {}));
  fresh.api.render();
  assert.deepEqual(tiles(fresh.body).map(([label]) => label), ['Working', 'Loaded', 'GPU · all apps']);
  assert.equal(tiles(fresh.body)[2][2], 'tile');
  assert.equal(fresh.body.querySelectorAll('details').some(box => box.dataset.key === 'runtime|memory'), false);
});

test('Memory: in a popover-narrow strip the Mac chip steps down to just the memory words; its label keeps the rest', () => {
  const { api, $ } = harness({ width: 390 });
  const small = $('vitalMac').querySelector('small');
  let width = 100;
  Object.defineProperty(small, 'clientWidth', { get: () => width });
  Object.defineProperty(small, 'scrollWidth', { get() { return this.textContent.length * 6.5; } });
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), memory: memoryBlock('critical') });
  api.renderVitals();
  assert.equal(small.textContent, 'Memory critical');
  assert.match($('vitalMac').getAttribute('aria-label'), /^This Mac: .* · Memory critical$/);
  // Without tight or critical memory the fourth form is the tiny one again.
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), memory: memoryBlock('watch') });
  api.renderVitals();
  assert.equal(small.textContent, 'Activity unknown');
  // Too narrow even for the memory words (390 px): the chip keeps its tiny form, since a cut "Me…" would say less.
  width = 60;
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), memory: memoryBlock('critical') });
  api.renderVitals();
  assert.equal(small.textContent, 'Memory critical · Activity unknown');
});

test('Memory: the map banner shows for tight and critical only, is dismissible until memory gets worse, and announces only a new level', () => {
  // A controlled clock (the page's Date.now), so the one-minute ease and the stale spell are exact.
  let clock = 1_800_000_000_000;
  class ClockDate extends Date { static now() { return clock; } }
  const { api, doc, $ } = harness({ extra: { Date: ClockDate } });
  const banner = $('memoryBanner'), announce = $('memoryAnnounce');
  let heard = [], last = '';
  const listen = () => { if (announce.textContent && announce.textContent !== last) heard.push(announce.textContent); last = announce.textContent; };
  const tick = (level, seconds = 1, age = 0) => { clock += seconds * 1000; api.setFeed({ ...pcSnapshot(clock / 1000 - age, {}), memory: memoryBlock(level) }); api.renderMemoryBanner(); listen(); };
  const flap = (seconds, a = 'tight', b = 'watch') => { for (let i = 0; i < seconds; i++) tick(i % 4 < 2 ? a : b); };
  tick('tight', 0);
  // An older guard's row: the 30 GB GPU allocation ranked LM Studio first, but it is the whole Mac's (every app's), so the
  // banner states LM Studio's own 21 GB and names the allocation as the Mac's.
  assert.deepEqual([banner.hidden, banner.dataset.level, $('memoryBannerMark').hidden, $('memoryBannerTitle').textContent, $('memoryBannerText').textContent],
    [false, 'tight', true, 'Memory tight', 'LM Studio uses about 21 GB · Mac GPU allocation about 30 GB (all apps) · Try: Unload idle model qwen/qwen3.8-27b from the monitor']);
  assert.equal(banner.title, 'Memory tight · LM Studio uses about 21 GB · Mac GPU allocation about 30 GB (all apps) · Try: Unload idle model qwen/qwen3.8-27b from the monitor');
  assert.deepEqual(heard, ['Memory tight. LM Studio uses about 21 GB · Mac GPU allocation about 30 GB (all apps). Try: Unload idle model qwen/qwen3.8-27b from the monitor.']);
  // The same level is not announced again on the next sample.
  tick('tight');
  assert.equal(heard.length, 1);
  // The Mac chip carries the word too (colour is never the only signal).
  api.renderVitals();
  assert.match($('vitalMac').querySelector('small').textContent, /Memory tight/);
  assert.equal($('vitalMac').dataset.tone, 'warn');
  assert.match($('vitalMac').getAttribute('aria-label'), /Memory tight$/);
  // Not dismissed, a Mac flapping on the threshold (tight, watch every 2 s): the banner follows the guard's level, but
  // nothing is announced again.
  flap(12);
  assert.equal(heard.length, 1, `flap re-announced: ${heard.join(' | ')}`);
  tick('tight');
  // Dismissed: hidden for tight, focus moves to the This Mac chip; a flap at the threshold never brings it back.
  api.dismissMemoryBanner();
  assert.deepEqual([banner.hidden, api.memoryDismissed.level, doc.activeElement], [true, 'tight', $('vitalMac')]);
  flap(12);
  assert.equal(banner.hidden, true);
  tick('tight');
  assert.deepEqual([banner.hidden, heard.length], [true, 1]);
  // Worse: critical comes back, red with "!", and is announced.
  tick('critical');
  assert.deepEqual([banner.hidden, banner.dataset.level, $('memoryBannerMark').hidden, $('memoryBannerTitle').textContent], [false, 'critical', false, 'Memory critical']);
  assert.match(heard.at(-1), /^Memory critical\. /);
  assert.equal(heard.length, 2);
  // Dismiss critical; a quick flap to tight and back keeps it dismissed and silent.
  api.dismissMemoryBanner();
  flap(20, 'critical', 'tight');
  assert.deepEqual([banner.hidden, heard.length], [true, 2]);
  // A full minute at tight lowers the bar: critical again is a new rise, shown and announced.
  for (let i = 0; i < 61; i++) tick('tight');
  assert.equal(banner.hidden, true, 'still dismissed at tight');
  tick('critical');
  assert.deepEqual([banner.hidden, heard.length], [false, 3]);
  // A stale spell hides it; the same level on the next fresh sample is not announced again.
  tick('critical', 1, 30);
  assert.deepEqual([banner.hidden, announce.textContent], [true, ''], 'stale');
  tick('critical');
  assert.deepEqual([banner.hidden, heard.length], [false, 3]);
  // Recovered to ok: hidden and silent; tight later is new again.
  tick('ok');
  assert.deepEqual([banner.hidden, announce.textContent], [true, '']);
  tick('tight');
  assert.deepEqual([banner.hidden, heard.length], [false, 4]);
  // Unknown shows nothing alarming and does not reset a dismissal.
  api.dismissMemoryBanner();
  tick('unknown');
  assert.equal(banner.hidden, true);
  tick('tight');
  assert.equal(banner.hidden, true, 'still dismissed at tight');
  // Markup: hidden until needed, the mark is decoration, the dismiss control says what it does, one polite announcer.
  assert.match(html, /<div id="memoryBanner" class="memory-banner" role="group" aria-labelledby="memoryBannerTitle" data-level="tight" hidden><span id="memoryBannerMark" class="memory-banner-mark" aria-hidden="true" hidden>!<\/span>/);
  assert.match(html, /<button id="memoryBannerDismiss" class="memory-banner-dismiss" type="button" aria-label="Dismiss the memory warning until memory gets worse"/);
  assert.match(html, /<p id="memoryAnnounce" class="visually-hidden" aria-live="polite" aria-atomic="true"><\/p>/);
  assert.match(appSource, /function updateFreshness\(\)\{renderVitals\(\);renderMemoryBanner\(\);/);
  assert.match(appSource, /\$\('memoryBannerDismiss'\)\.addEventListener\('click',dismissMemoryBanner\);\$\('memoryBannerDetails'\)\.addEventListener\('click',openMemoryDetails\);/);
  // The rail steps below the banner only beside the map (the compact sheet sits at the bottom); critical is red, tight amber.
  const rules = cssRules(css);
  assert.ok(rules.some(([media, head, body]) => /min-width:821px/.test(media) && /#memoryBanner:not\(\[hidden\]\)\) \.evidence-rail/.test(head) && /top:102px/.test(body)));
  assert.ok(!rules.some(([media, head]) => !/min-width/.test(media) && /#memoryBanner:not\(\[hidden\]\)\) \.evidence-rail/.test(head)));
  assert.ok(rules.some(([media, head, body]) => !media && head === '.memory-banner[data-level=critical]' && /border-color:#c25a58/.test(body)));
  // A long banner wraps rather than cutting its suggestion: two lines at every width (no single-line ellipsis, which hid a
  // Nisi caveat at 1280 and 1512 px), the rail stepping lower up to 1100 px, three lines at 560 px.
  assert.ok(rules.some(([media, head, body]) => !media && head === '.memory-banner-copy' && /display:-webkit-box/.test(body) && /-webkit-line-clamp:2/.test(body) && /-webkit-box-orient:vertical/.test(body)));
  assert.ok(!rules.some(([, head, body]) => head === '.memory-banner-copy' && /nowrap|text-overflow/.test(body)), 'no width cuts the banner to one line');
  assert.ok(rules.some(([media, head, body]) => /min-width:821px\) and \(max-width:1100px/.test(media) && /#memoryBanner:not\(\[hidden\]\)\) \.evidence-rail/.test(head) && /top:108px/.test(body)));
  assert.ok(rules.some(([media, head, body]) => /max-width:560px/.test(media) && head === '.memory-banner-copy' && /-webkit-line-clamp:3/.test(body)));
  // Compact: the banner leaves the layout with the status chips while the bottom sheet is open.
  assert.match(css, /body:has\(\.evidence-rail>:not\(\[hidden\]\)\) #mapHeading\{display:none\}/);
  assert.ok(rules.some(([media, head, body]) => /max-width:\s*820px/.test(media) && head === 'body:has(.evidence-rail>:not([hidden])) #memoryBanner' && /display:none/.test(body)));
  assert.match(appSource, /chip\.focus\(\{preventScroll:true\}\);if\(document\.activeElement!==chip\)\$\('map'\)\.focus\(\{preventScroll:true\}\);/);
});

// Router concurrency P2 (spec 6.13), 27 Sep 2026: several routed tasks at once, run against the fake DOM.
const concurrentMode = () => ({ state: 'processing', active: true, blinking: true, client: 'claude', chatId: null, routeId: 'run-live',
  taskState: 'processing', setupState: 'fresh', setupAgeSeconds: 5, evidence: '2 routed task(s) running · 1 queued; Router run run-live is live',
  observedAt: 'now', runCounts: { running: 2, queued: 1, unresolved: 1 },
  activeRuns: [{ runId: 'run-live', state: 'running', stage: 'backend_draft', host: 'mac', client: 'claude', live: true },
    { runId: 'run-pc', state: 'running', stage: 'backend_review', host: 'windows', client: 'codex', live: true },
    { runId: 'run-dead', state: 'unresolved', stage: 'incomplete', host: null, client: null, live: false }],
  queuedRuns: [{ runId: 'run-queued', phase: 'waiting', resource: 'mac-pair', step: 'pre-begin', secondsLeft: 40, client: 'opencode' }],
  lanes: { 'mac-pair': { capacity: 1, holders: ['run-live'], waiting: ['run-queued'] }, 'pc-route': { capacity: 1, holders: ['run-pc'], waiting: [] },
    'pc-deep': { capacity: 1, holders: [], waiting: [] }, 'pc-fast': { capacity: 2, holders: ['run-pc'], waiting: [] } },
  admission: { policy: 'multi', source: 'file', drainState: 'not-applicable', draining: false, sharedHolders: [] },
  install: { state: 'installed', inProgress: false } });
const concurrentPipeline = () => ({ runId: 'run-live', status: 'running', stage: 'backend_draft', recoveryRequired: false,
  pipelines: [{ runId: 'run-live', status: 'running', stage: 'backend_draft', host: 'mac', client: 'claude', live: true },
    { runId: 'run-pc', status: 'running', stage: 'backend_review', host: 'windows', client: 'codex', live: true },
    { runId: 'run-queued', status: 'waiting', stage: null, host: null, client: 'opencode', live: true, resource: 'mac-pair', secondsLeft: 40 },
    { runId: 'run-dead', status: 'unresolved', stage: 'incomplete', host: null, client: null, live: false }] });

test('Router concurrency: the map node counts routes, its inspector stacks every run, and the chip agrees', () => {
  const { api, $, body } = drawerHarness();
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), onlineCodeMode: concurrentMode(), pipeline: concurrentPipeline(),
    components: [{ id: 'nisi', state: 'in-use', detail: 'Nisi call in flight for live route run-live' }, { id: 'jev', state: 'configured', detail: 'Opted in' }] });
  api.select('pipeline');
  assert.equal(api.nodeSubtitle('pipeline'), 'ROUTES RUNNING · 2');
  api.renderInspector();
  assert.equal(body.querySelector('.panel-summary').querySelector('strong').textContent, '2 routes running');
  const lines = [...body.querySelectorAll('.route-run')].map(p => p.textContent);
  assert.equal(body.querySelector('.route-runs-head').textContent, 'Routed tasks (4)');
  assert.deepEqual(lines, ['run-live · Running · Claude · Mac · backend draft', 'run-pc · Running · Codex · PC · backend review',
    'run-queued · Queued · for Mac pair · 40 s left · OpenCode', 'run-dead · Unresolved · incomplete']);
  assert.ok(tiles(body).some(([label, value]) => label === 'Nisi' && value === 'In use'));
  api.renderVitals();
  assert.equal($('vitalRoute').querySelector('small').textContent, '2 routes running');
  // One route: today's words.
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), pipeline: { ...concurrentPipeline(), pipelines: concurrentPipeline().pipelines.slice(0, 1) } });
  api.select('pipeline');
  assert.equal(api.nodeSubtitle('pipeline'), 'ROUTE RUNNING · BACKEND DRAFT');
  // An install in progress is its own state, never an unsafe journal.
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), pipeline: { status: 'installing', runId: null, pipelines: [] } });
  api.select('pipeline');
  assert.equal(api.nodeSubtitle('pipeline'), 'ROUTER INSTALL IN PROGRESS');
  // A stale feed lists nothing as running.
  api.setFeed({ ...pcSnapshot(nowSeconds() - 30, {}), pipeline: concurrentPipeline() });
  api.select('pipeline');
  api.renderInspector();
  assert.equal(body.querySelectorAll('.route-run').length, 0);
});

test('Router concurrency: the Online Code panel lists each run, each queued run, the lanes and the policy', () => {
  const { api, $ } = harness();
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), onlineCodeMode: concurrentMode(), pipeline: concurrentPipeline() });
  api.updateOnlineCodeMode();
  assert.equal($('onlineCodeLabel').textContent, 'Processing · 2 running · 1 queued');
  assert.deepEqual([$('onlineCodeStatus').textContent, $('onlineCodeMeaning').textContent],
    ['Routed tasks are processing', '2 running and 1 queued through the Nisi Inference route now; each is listed below.']);
  const text = $('onlineCodeRows').textContent;
  assert.match(text, /Run run-liveRunning · Claude · Mac · backend draft/);
  assert.match(text, /Run run-deadUnresolved · incomplete/);
  assert.match(text, /Queued run-queuedwaiting for Mac pair · 40 s left · OpenCode/);
  assert.match(text, /LanesMac pair 1\/1 \(run-live\), 1 waiting · PC route 1\/1 \(run-pc\) · PC deep 0\/1 · PC fast 1\/2 \(run-pc\)/);
  // Terms check: the Admission row must not contradict the Lanes row above it (PC fast holds 2).
  assert.match(text, /AdmissionMulti: routes run at once, up to each lane’s capacity/);
  assert.doesNotMatch(text, /one per lane/);
  assert.match($('onlineCodeTiles').textContent, /Route2 running · 1 queued/);
  // An install in progress.
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), onlineCodeMode: { state: 'unknown', active: null, taskState: 'install-in-progress',
    evidence: 'Router install in progress; route state is verified again when it finishes', runCounts: { running: 0, queued: 0, unresolved: 0 },
    admission: { policy: 'single', source: 'file', drainState: 'unknown', draining: true, sharedHolders: [] } } });
  api.updateOnlineCodeMode();
  // Terms check: the tab uses the same name for this state as the status line (a rollback is one too).
  assert.equal($('onlineCodeLabel').textContent, 'Router install in progress');
  assert.equal($('onlineCodeStatus').textContent, 'Router install in progress');
  assert.match($('onlineCodeRows').textContent, /AdmissionSingle · drain state unknown/);
  assert.doesNotMatch($('onlineCodeRows').textContent, /^Lanes|Lanes/);
});

test('Router concurrency terms: the narrow tab says "running" and "Install in progress", never "2 run" or "Installing"', () => {
  const { api, $ } = harness({ width: 700 });
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), onlineCodeMode: concurrentMode(), pipeline: concurrentPipeline() });
  api.updateOnlineCodeMode();
  assert.equal($('onlineCodeLabel').textContent, '2 running · 1 queued');
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), onlineCodeMode: { state: 'unknown', active: null, taskState: 'install-in-progress',
    evidence: 'Router install in progress; route state is verified again when it finishes', runCounts: { running: 0, queued: 0, unresolved: 0 },
    admission: { policy: 'single', source: 'file', drainState: 'unknown', draining: true, sharedHolders: [] } } });
  api.updateOnlineCodeMode();
  assert.equal($('onlineCodeLabel').textContent, 'Install in progress');
});

test('Mac empty activity stays unknown unless both job and route feeds are readable', () => {
  const { api } = harness();
  const unavailableJobs = { schemaVersion: 1, availability: { state: 'available' }, inFlight: [], recent: [], lastSuccess: null };
  const base = { ...pcSnapshot(nowSeconds(), unavailableJobs), sources: [
    { id: 'windows-jobs', state: 'unavailable' }, { id: 'router-active', state: 'live' },
  ], activity: { runs: [] } };
  api.setFeed(base);
  const empty = api.vitalsInput();
  assert.deepEqual([empty.feed.length, empty.vitals.activity.detail, empty.vitals.activity.short],
    [0, 'Activity unknown (feed unavailable or stale)', 'Unknown']);
  api.setFeed({ ...base, activity: { runs: [{ runId: 'route-live', source: 'router-active', status: 'running',
    activity: 'running', host: 'mac', client: 'codex', ageSeconds: 2 }] } });
  const withKnownRow = api.vitalsInput();
  assert.equal(withKnownRow.feed[0].runId, 'route-live');
  assert.match(withKnownRow.vitals.activity.detail, /codex →/);
});

test('Router concurrency: live and queued routes show in Live activity as running and queued', () => {
  const { api } = harness();
  const snapshot = { ...pcSnapshot(nowSeconds(), {}), activity: { runs: [
    { runId: 'run-live', status: 'running', activity: 'running', host: 'mac', client: 'claude', ageSeconds: 3 },
    { runId: 'run-queued', status: 'waiting', activity: 'queued', host: null, client: 'opencode', ageSeconds: 2 },
    { runId: 'run-dead', status: 'unresolved', activity: 'unknown', host: null, client: null, ageSeconds: 400 }] } };
  api.setFeed(snapshot);
  assert.equal(api.vitalsInput().feed.find(row => row.runId === 'run-live').state, 'in-flight-stale');
  api.setFeed({ ...snapshot, sources: [...snapshot.sources,
    { id: 'router-active', state: 'live', ageSeconds: 0 }] });
  const feed = api.vitalsInput().feed;
  const byRun = Object.fromEntries(feed.filter(row => row.kind === 'route').map(row => [row.runId, row.state]));
  assert.deepEqual(byRun, { 'run-live': 'in-flight', 'run-queued': 'queued', 'run-dead': 'unresolved' });
  // The summary names a live route as a route, beside PC jobs.
  assert.equal(layout.activitySummary(feed, { live: true }).line, '1 route running now');
  const mixed = layout.activityFeed({ inFlight: [deepJob] }, [{ runId: 'run-live', status: 'running', activity: 'running', host: 'mac', ageSeconds: 3 }]);
  assert.equal(layout.activitySummary(mixed, { live: true }).line, '1 job and 1 route running now');
  assert.equal(layout.activitySummary(layout.activityFeed({ inFlight: [deepJob] }, []), { live: true }).line, '1 job running now');
});

test('p2-readers converge: only queued routes read "queued" in the tab, the status line and the Route tile, and never blink', () => {
  const { api, $ } = harness();
  const queuedMode = () => ({ ...concurrentMode(), client: 'codex', routeId: 'run-queued', taskState: 'queued', blinking: false,
    evidence: 'Queued router run run-queued is waiting for mac-pair', runCounts: { running: 0, queued: 1, unresolved: 0 },
    activeRuns: [], lanes: { 'mac-pair': { capacity: 1, holders: [], waiting: ['run-queued'] } } });
  api.setFeed({ ...pcSnapshot(nowSeconds(), {}), onlineCodeMode: queuedMode(),
    pipeline: { runId: 'run-queued', status: 'queued', stage: null, queuePhase: 'waiting', queueResource: 'mac-pair',
      pipelines: [{ runId: 'run-queued', status: 'waiting', live: true, resource: 'mac-pair', secondsLeft: 40 }] } });
  api.updateOnlineCodeMode();
  assert.equal($('onlineCodeLabel').textContent, 'Queued · codex');
  assert.doesNotMatch($('onlineCodeMode').className, /blinking/);
  assert.deepEqual([$('onlineCodeStatus').textContent, $('onlineCodeMeaning').textContent],
    ['A routed task is queued', 'A coding task is waiting for admission or a lane on the Nisi Inference route; nothing is running yet.']);
  assert.match($('onlineCodeTiles').textContent, /RouteQueued/);
  assert.doesNotMatch($('onlineCodeLabel').textContent + $('onlineCodeStatus').textContent, /[Pp]rocessing/);
});

function autoUnloadHarness(replies) {
  const calls = [];
  const extra = {
    fetch: async (path, options) => {
      calls.push({ path, options });
      const reply = replies.shift();
      if (reply instanceof Error) throw reply;
      if (!reply) throw new Error('Unexpected auto-unload request');
      return { ok: reply.status === 200, status: reply.status, json: async () => reply.body };
    },
    setTimeout: () => 0, clearTimeout: () => {},
    AbortController: class { constructor() { this.signal = {}; } abort() {} }
  };
  return { ...harness({ extra }), calls };
}

test('auto-unload control starts disabled, reads app state, and writes one exact boolean after fresh read', async () => {
  assert.match(html, /id="autoUnloadToggle"[^>]*disabled/);
  assert.match(appSource, /\$\('autoUnloadToggle'\)\.addEventListener\('click',requestAutoUnload\)/);
  const off = { enabled: false, blocked: 'auto-unload is off', configError: null };
  const on = { enabled: true, blocked: 'Waiting for idle observation', configError: null };
  const { api, $, calls } = autoUnloadHarness([
    { status: 200, body: off }, { status: 200, body: off }, { status: 200, body: on }, { status: 200, body: on }
  ]);
  await api.pollAutoUnload(true);
  assert.equal($('autoUnloadMode').textContent, 'Off');
  assert.equal($('autoUnloadToggle').disabled, false);
  assert.equal($('autoUnloadToggle').textContent, 'Turn on');
  await api.requestAutoUnload();
  await settle();
  assert.equal($('autoUnloadMode').textContent, 'On');
  assert.equal($('autoUnloadToggle').textContent, 'Turn off');
  assert.match($('autoUnloadStatus').textContent, /Waiting for idle observation/);
  assert.deepEqual(calls.map(call => call.options.method), ['GET', 'GET', 'POST', 'GET']);
  assert.ok(calls.every(call => call.path === '/api/models/auto-unload' && call.options.credentials === 'same-origin'));
  assert.deepEqual(JSON.parse(calls[2].options.body), { enabled: true });
  assert.equal(calls[2].options.headers['Content-Type'], 'application/json');
});

test('auto-unload stays disabled for standalone, malformed, unavailable and invalid configuration states', async () => {
  const { api, $, calls } = autoUnloadHarness([
    { status: 501, body: { message: 'app only' } },
    { status: 200, body: { enabled: 'false' } },
    new Error('offline'),
    { status: 200, body: { enabled: false, configError: 'bad config', blocked: 'config invalid' } }
  ]);
  for (let i = 0; i < 3; i++) {
    await api.pollAutoUnload(true);
    assert.equal($('autoUnloadMode').textContent, 'State unknown');
    assert.equal($('autoUnloadToggle').disabled, true);
    await api.requestAutoUnload();
  }
  await api.pollAutoUnload(true);
  assert.equal($('autoUnloadMode').textContent, 'Off');
  assert.equal($('autoUnloadToggle').disabled, true);
  assert.match($('autoUnloadStatus').textContent, /Invalid private setting/);
  assert.equal(calls.length, 4);
  assert.ok(calls.every(call => call.options.method === 'GET'));
});

test('auto-unload preflight refuses a stale toggle and an uncertain write is read back without retry', async () => {
  const off = { enabled: false, blocked: 'auto-unload is off' };
  const on = { enabled: true, blocked: 'waiting for idle' };
  const changed = autoUnloadHarness([{ status: 200, body: off }, { status: 200, body: on }]);
  await changed.api.pollAutoUnload(true);
  await changed.api.requestAutoUnload();
  await settle();
  assert.ok(changed.calls.every(call => call.options.method === 'GET'));
  assert.equal(changed.$('autoUnloadMode').textContent, 'On');
  assert.match(changed.$('autoUnloadStatus').textContent, /Setting changed elsewhere/);
  const uncertain = autoUnloadHarness([{ status: 200, body: off }, { status: 200, body: off }, new Error('reply lost'), { status: 200, body: on }]);
  await uncertain.api.pollAutoUnload(true);
  await uncertain.api.requestAutoUnload();
  await settle();
  assert.deepEqual(uncertain.calls.map(call => call.options.method), ['GET', 'GET', 'POST', 'GET']);
  assert.equal(uncertain.$('autoUnloadMode').textContent, 'On');
  assert.equal(uncertain.$('autoUnloadToggle').disabled, false);
});


test('unified repair posts scope all once and preserves owner and pending-operation guards', async () => {
  const calls=[];
  let release;
  const {api}=harness({extra:{fetch:(url,options)=>{
    calls.push({url,options});
    if(options?.method==='POST')return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({status:'running',operationId:17,action:'fix-all',message:'Checking.',steps:[]})});});
    return Promise.resolve({ok:true,json:async()=>({status:'running',operationId:17,action:'fix-all',message:'Checking.',steps:[]})});
  },setTimeout:()=>0,clearTimeout:()=>{},AbortController:class{constructor(){this.signal={};}abort(){}}}});
  await api.requestOnlineCodeAction('fix-all');
  assert.equal(calls.length,0,'no request without Mac owner snapshot');
  api.setFeed({...pendingFeed(),host:'windows'});
  await api.requestOnlineCodeAction('fix-all');
  assert.equal(calls.length,0,'observer cannot repair');
  api.setFeed(pendingFeed());
  const first=api.requestOnlineCodeAction('fix-all');
  await api.requestOnlineCodeAction('fix-all');
  assert.equal(calls.length,1,'posting blocks duplicate');
  assert.equal(calls[0].url,'/api/inference/fix');
  assert.equal(calls[0].options.body,JSON.stringify({scope:'all'}));
  release();await first;await settle();
  await api.requestOnlineCodeAction('fix-all');
  api.setAction({status:'uncertain',operationId:17,action:'fix-all'});
  await api.requestOnlineCodeAction('fix-all');
  assert.equal(calls.filter(call=>call.options?.method==='POST').length,1,'running and uncertain block duplicate');
});

test('395px Fix progress survives repeated snapshot and compact-layout refreshes', async () => {
  const {api,doc,$}=netHarness({width:395});
  api.spyRail();
  api.setFeed(pendingFeed());
  api.select('pipeline');
  $('drawer').hidden=false;
  api.openFixInference();
  await settle();
  api.setAction({status:'running',operationId:21,action:'fix-all',message:'Checking inference.',steps:[]});
  for(let tick=0;tick<5;tick++){
    api.setFeed({...pendingFeed(),sampledAt:nowSeconds()+tick});
    api.refreshEvidence();
    api.updateOnlineCodeMode();
    api.syncCompactUI();
    assert.equal($('fixInferenceDetails').hidden,false,`progress remains open at refresh ${tick}`);
    assert.equal($('fixInference').getAttribute('aria-expanded'),'true');
    assert.equal($('drawer').hidden,true);
    assert.equal(doc.activeElement,$('fixInferenceDetails'));
  }
});

test('Improvement 2: receipt-first runs from an older observer open on the newest route; the 17 d receipt keeps its badge but leaves the Route chip', () => {
  const { api, $ } = harness();
  const route = (runId, ageSeconds, extra = {}) => ({ runId, source: 'router-archive', traceKey: `router-archive:${runId}:${'b'.repeat(64)}`,
    status: 'RESPONSE_VALIDATED', activity: 'completed', ageSeconds, calls: [], ...extra });
  // The 10 Oct live order: the receipt first, today's router routes after it.
  const receipt = route('monitor-usage-format-20260923-01', 1482878, { source: 'monitor-receipt',
    traceKey: 'monitor-receipt:monitor-usage-format-20260923-01:unknown', hostAcceptance: 'REJECTED', reviewSummaryContradiction: true });
  const runs = [receipt, route('claude-agiw-mac-pair-live-20261010-a', 6295), route('marketscout-coverage-20261010-01', 6634, { status: 'PARTIAL' }),
    route('claude-agiw-nisi-jev-live-20261010-a', 9610)];
  // The observer's bound: nothing of the last 24 h was cut from its list (the 10 Oct live value).
  const historyBound = { truncated: true, missingFromAgeSeconds: 931805.706 };
  api.setFeed({ schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(), models: [], sources: [], clients: [], pipeline: { status: 'idle' },
    components: [{ id: 'nisi', state: 'ready' }], activity: { runs, historyBound } });
  api.renderVitals();
  assert.equal($('vitalRoute').title, 'Nisi Inference route: Idle · pair ready');
  assert.equal($('vitalRoute').dataset.tone, 'ok');
  // Call traces: the default trace and the list's first row are the newest route.
  api.setTraceView();
  api.render();
  api.renderList();
  const items = $('list').querySelectorAll('button');
  assert.deepEqual(items.map(item => item.dataset.key), [runs[1], runs[2], runs[3], receipt].map(run => run.traceKey));
  assert.deepEqual(items.map(item => item.getAttribute('aria-pressed')), ['true', 'false', 'false', 'false']);
  // The old receipt is still listed, with its review badge, host decision and age.
  const old = items.at(-1);
  assert.equal(old.querySelector('.review-badge').textContent, 'Review warning');
  assert.match(old.textContent, /Saved receipt · 17d ago/);
  assert.match(old.textContent, /Host acceptance: REJECTED/);
  // The Activity list keeps the flag too, beside its age.
  const { feed } = api.vitalsInput();
  $('activityDetails').hidden = false;
  api.renderActivity(feed);
  assert.match($('activityList').textContent, /review warning · 17d ago/);
  // A warning recorded within 24 h is still counted on the chip.
  api.setFeed({ schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(), models: [], sources: [], clients: [], pipeline: { status: 'idle' },
    components: [{ id: 'nisi', state: 'ready' }], activity: { runs: [...runs, { ...receipt, runId: 'fresh-receipt', traceKey: 'monitor-receipt:fresh-receipt:unknown', ageSeconds: 600 }], historyBound } });
  api.renderVitals();
  assert.equal($('vitalRoute').title, 'Nisi Inference route: Idle · pair ready · 1 review warning in 24 h');
  // Review fix: 12 PC jobs newer than a 2 h old route-stopping defect fill the 12-row Activity feed; the chip counts
  // the whole route list, so the defect still turns it amber.
  const defect = route('route-defect-2h', 7200, { status: 'REVIEW_FINDINGS', reviewConsistency: { status: 'SUMMARY_REPORTS_DEFECT' } });
  const pcJobs = { schemaVersion: 1, complete: true, inFlight: [], lastSuccess: null,
    recent: Array.from({ length: 12 }, (_, i) => ({ id: `job-${i}`, lane: 'fast', state: 'success', ageSeconds: 30 + i })) };
  const withJobs = bound => api.setFeed({ schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(), models: [], sources: [], clients: [], pipeline: { status: 'idle' },
    components: [{ id: 'nisi', state: 'ready' }], windowsJobs: pcJobs, activity: { runs: [...runs, defect], ...bound } });
  withJobs({ historyBound });
  api.renderVitals();
  const filled = api.vitalsInput().feed;
  assert.equal(filled.length, 12);
  assert.ok(filled.every(row => row.kind === 'pc-job'), 'the defect is not in the feed');
  assert.equal($('vitalRoute').title, 'Nisi Inference route: Idle · pair ready · 1 review defect in 24 h');
  assert.equal($('vitalRoute').dataset.tone, 'warn');
  // An observer that sends no bound (an older one), or one that cut a route of the window: a lower bound.
  for (const bound of [{}, { historyBound: { truncated: true, missingFromAgeSeconds: 3600 } }]) {
    withJobs(bound);
    api.renderVitals();
    assert.equal($('vitalRoute').title, 'Nisi Inference route: Idle · pair ready · at least 1 review defect in 24 h');
  }
});

test('machine headlines keep every C2 failure unknown despite an old idle or down report', () => {
  for (const platform of ['mac', 'windows', 'linux']) {
    for (const reasonCode of ['timeout', 'refused', 'non_json', 'oversize', 'schema_invalid', 'stale', 'host_key_mismatch', 'share_unmounted', 'asleep']) {
      const row = {platform, state: 'unknown', reasonCode, trust: 'authenticated', ageSeconds: null,
        modelsComplete: false, loadedCount: null, busyCount: null, generationTokensPerSecond: null};
      const view = layout.machineStatusView(row);
      assert.match(view.detail, /Unknown/);
      assert.ok(view.detail.includes(reasonCode.replaceAll('_', ' ')));
      assert.doesNotMatch([view.detail, view.short, view.tiny].join(' '), /\b(?:idle|down|quiet)\b|0 tok\/s/i);
    }
  }
});

test('machine summary overrides legacy idle and worker headline in the actual chip renderer', () => {
  const {api, $} = harness();
  api.setFeed({schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(), models: [],
    sources: [{id: 'lms-ps', state: 'live'}, {id: 'lmstudio-api', state: 'live'}],
    machines: ['mac', 'windows', 'linux'].map(platform => ({host: platform, platform, state: 'unknown',
      reasonCode: 'timeout', trust: 'unverified', ageSeconds: null, loadedCount: null, busyCount: null}))});
  api.renderVitals();
  for (const id of ['vitalMac', 'vitalPc']) {
    assert.match($(id).querySelector('small').textContent, /Unknown/);
    assert.doesNotMatch($(id).title, /idle|down|quiet|0 tok\/s/i);
  }
});

test('partial local inventory stays unknown in the summary, inspector and vitals input', () => {
  for (const withMachine of [false, true]) {
    for (const models of [[], [{id: 'retained', host: 'mac', loaded: false, state: 'unloaded', ageSeconds: 0}],
      [{id: 'retained', host: 'mac', loaded: true, state: 'idle', ageSeconds: 0}]]) {
      const {api, $, body} = drawerHarness();
      api.setFeed({host: 'mac', sampledAt: nowSeconds(), models, activityKnown: true,
        sources: [{id: 'lmstudio-api', state: 'live', modelsComplete: false}],
        ...(withMachine ? {machines: [{host: 'mac', platform: 'mac', state: 'up', trust: 'local', ageSeconds: 0,
          modelsComplete: false, loadedCount: null, busyCount: null}]} : {})});
      api.renderSummary();
      assert.equal($('loadedCount').textContent, '—');
      assert.equal($('activeCount').textContent, '—');
      assert.doesNotMatch($('headline').textContent + $('subline').textContent, /quiet|\b\d+ loaded\b/i);
      assert.match(api.vitalsInput().vitals.mac.detail, /Loaded unknown · activity unknown/);
      api.select('runtime');
      api.renderInspector();
      assert.deepEqual(tiles(body).slice(0, 2).map(row => row.slice(0, 2)), [['Working', 'Unknown'], ['Loaded', 'Unknown']]);
      assert.doesNotMatch(body.textContent, /quiet|\b0 loaded\b/i);
    }
  }
});

test('canonical local model counts agree across the summary, inspector and vitals despite aliases', () => {
  for (const busyCount of [0, 1]) {
    const {api, $, body} = drawerHarness();
    api.setFeed({host: 'mac', sampledAt: nowSeconds(), activityKnown: true,
      models: ['canonical', 'alias-one', 'alias-two'].map(id => ({id, host: 'mac', modelKey: 'canonical',
        loaded: true, state: busyCount ? 'generating' : 'idle', ageSeconds: 0})),
      sources: [{id: 'lms-ps', state: 'live', modelsComplete: true}],
      machines: [{host: 'mac', platform: 'mac', state: 'up', trust: 'local', ageSeconds: 0,
        modelsComplete: true, loadedCount: 1, busyCount}]});
    api.renderSummary();
    assert.equal($('loadedCount').textContent, '1');
    assert.equal($('activeCount').textContent, String(busyCount));
    assert.match($('subline').textContent, busyCount ? /1 local model is working/ : /1 loaded/);
    assert.match(api.vitalsInput().vitals.mac.detail, busyCount ? /^1 working/ : /^1 loaded · idle/);
    api.select('runtime');
    api.renderInspector();
    assert.deepEqual(tiles(body).slice(0, 2).map(row => row.slice(0, 2)), [['Working', String(busyCount)], ['Loaded', '1']]);
  }
});

test('a null or expired canonical count never falls back to an empty parsed model list', () => {
  for (const change of [{loadedCount: null}, {loadedCount: -1}, {loadedCount: true}, {loadedCount: 1.5},
    {loadedCount: Number.MAX_SAFE_INTEGER + 1}, {modelsComplete: false}, {state: 'unknown'}, {ageSeconds: 4}]) {
    const {api, $, body} = drawerHarness();
    api.setFeed({host: 'mac', sampledAt: nowSeconds(), models: [], activityKnown: true,
      sources: [{id: 'lms-ps', state: 'live', modelsComplete: true}],
      machines: [{host: 'mac', platform: 'mac', state: 'up', trust: 'local', ageSeconds: 0,
        modelsComplete: true, loadedCount: 0, busyCount: 0, ...change}]});
    api.renderSummary();
    assert.equal($('loadedCount').textContent, '—');
    assert.match(api.vitalsInput().vitals.mac.detail, /Loaded unknown · activity unknown/);
    api.select('runtime');
    api.renderInspector();
    assert.equal(tiles(body).find(row => row[0] === 'Loaded')[1], 'Unknown');
  }
});

test('a complete empty legacy inventory still reports a known zero', () => {
  const {api, $, body} = drawerHarness();
  api.setFeed({host: 'mac', sampledAt: nowSeconds(), models: [], activityKnown: true,
    sources: [{id: 'lms-ps', state: 'live', modelsComplete: true}]});
  api.renderSummary();
  assert.equal($('loadedCount').textContent, '0');
  assert.equal($('activeCount').textContent, '0');
  assert.match(api.vitalsInput().vitals.mac.detail, /^0 loaded · idle/);
  api.select('runtime');
  api.renderInspector();
  assert.equal(tiles(body).find(row => row[0] === 'Loaded')[1], '0');
});

test('canonical activity remains working without matching per-instance phase counts', () => {
  for (const [busyCount, state] of [[1, 'loaded'], [2, 'generating']]) {
    const {api, $} = harness();
    api.setFeed({host: 'mac', sampledAt: nowSeconds(), activityKnown: true,
      models: [{id: 'canonical', host: 'mac', loaded: true, state, ageSeconds: 0}],
      sources: [{id: 'lms-ps', state: 'live', modelsComplete: true}],
      machines: [{host: 'mac', platform: 'mac', state: 'up', trust: 'local', ageSeconds: 0,
        modelsComplete: true, loadedCount: busyCount, busyCount}]});
    api.renderSummary();
    assert.equal($('activeCount').textContent, String(busyCount));
    assert.equal(api.vitalsInput().vitals.mac.detail, `${busyCount} working`);
  }
});

test('the PC headline follows its usable share fallback without merging an uncorrelated peer', () => {
  for (const host of ['pc1', 'windows']) {
    const {api, $} = harness();
    api.setFeed({schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(), models: [], sources: [],
      windowsLive: {host, via: 'share', health: 'ok', ageSeconds: 0, models: [], modelsComplete: true},
      machines: [
        {id: 'paired-node', host: 'paired-node', platform: 'windows', state: 'unknown',
          reasonCode: 'not_connected', trust: 'unverified', ageSeconds: null},
        {id: host, host, platform: 'windows', state: 'up', reasonCode: 'observed', trust: 'share-reported',
          ageSeconds: 0, modelsComplete: true, loadedCount: 1, busyCount: 1}]});
    api.renderVitals();
    assert.match($('vitalPc').title, /Up · 1 loaded · 1 busy/);
  }
});

test('only fresh attributed machine reports show down and incomplete inventories keep unknown counts', () => {
  const row = {platform: 'linux', state: 'down', trust: 'authenticated', reasonCode: 'reported_down', ageSeconds: 10};
  assert.match(layout.machineStatusView(row).detail, /Down/);
  for (const changed of [{...row, ageSeconds: 31}, {...row, trust: 'unverified'}, {...row, ageSeconds: true}]) {
    assert.match(layout.machineStatusView(changed).detail, /Unknown/);
    assert.doesNotMatch(layout.machineStatusView(changed).detail, /Down/);
  }
  const partial = layout.machineStatusView({...row, state: 'up', modelsComplete: false, busyCount: 0, loadedCount: 0});
  assert.match(partial.detail, /loaded unknown · activity unknown/);
  const complete = layout.machineStatusView({...row, state: 'up', modelsComplete: true, busyCount: 2, loadedCount: 3, handsOff: true});
  assert.match(complete.detail, /3 loaded · 2 busy · hands off/);
});

test('hands off remains visible in every fresh machine headline form', () => {
  const row = {platform: 'windows', state: 'up', trust: 'authenticated', ageSeconds: 0,
    modelsComplete: true, loadedCount: 1, busyCount: 0, handsOff: true};
  const view = layout.machineStatusView(row);
  for (const form of ['detail', 'short', 'tiny']) assert.match(view[form], /hands off/, form);
  for (const handsOff of [false, undefined, 'true', 1]) {
    const ordinary = layout.machineStatusView({...row, handsOff});
    assert.deepEqual([ordinary.detail, ordinary.short, ordinary.tiny],
      ['● Up · 1 loaded · 0 busy', '● Up · 0 busy', '● Up']);
  }
});

test('375px PC chip rendering keeps hands off through short and overflow forms', () => {
  const {api, $} = harness({width: 375});
  const row = {host: 'pc', platform: 'windows', state: 'up', trust: 'authenticated', ageSeconds: 0,
    modelsComplete: true, loadedCount: 1, busyCount: 0, handsOff: true};
  api.setFeed({schemaVersion: 1, host: 'mac', sampledAt: nowSeconds(), models: [], sources: [], machines: [row]});
  const chip = $('vitalPc'), small = chip.querySelector('small');
  let availableWidth = 200;
  // Use the existing renderer's measured-overflow path; the harness does not supply browser font metrics.
  Object.defineProperty(small, 'clientWidth', {get: () => availableWidth});
  Object.defineProperty(small, 'scrollWidth', {get() { return this.textContent.length * 6.5; }});
  api.renderVitals();
  assert.equal(chip.querySelector('strong').textContent, 'PC');
  assert.equal(small.textContent, '● Up · hands off · 0 busy');
  availableWidth = 115;
  api.renderVitals();
  assert.equal(small.textContent, '● Up · hands off');
  assert.ok(small.scrollWidth <= small.clientWidth);
  assert.match(chip.title, /1 loaded · 0 busy · hands off/);
  assert.match(chip.getAttribute('aria-label'), /1 loaded · 0 busy · hands off/);
});

test('serving-aged machine headlines use original timestamps without adding snapshot age twice', () => {
  for (const observedAt of ['1970-01-01T00:16:11.000000+00:00', '1970-01-01T00:16:11Z']) {
    const row = {platform: 'windows', state: 'up', trust: 'authenticated', ageSeconds: 29,
      observedAt, modelsComplete: true, loadedCount: 3, busyCount: 2};
    const view = layout.machineStatusView(row, {snapshotAge: 2, now: 1000});
    assert.match(view.detail, /Up · 3 loaded · 2 busy/);
  }
});

test('machine headlines expire original observations even if the cached age looks fresh', () => {
  const row = {platform: 'windows', state: 'up', trust: 'authenticated', ageSeconds: 0,
    observedAt: '1970-01-01T00:16:09.000000+00:00', modelsComplete: true, loadedCount: 1, busyCount: 1};
  assert.match(layout.machineStatusView(row, {now: 1000}).detail, /Unknown/);
});

test('malformed machine observation timestamps cannot claim current health', () => {
  for (const observedAt of ['', 'invalid', '1970-01-01T00:16:40', 1000, true]) {
    const row = {platform: 'windows', state: 'up', trust: 'authenticated', ageSeconds: 0,
      observedAt, modelsComplete: true, loadedCount: 1, busyCount: 1};
    assert.match(layout.machineStatusView(row, {now: 1000}).detail, /Unknown/);
  }
});

test('future machine observation timestamps cannot claim current health', () => {
  const row = {platform: 'windows', state: 'up', trust: 'authenticated', ageSeconds: 0,
    observedAt: '1970-01-01T00:16:43Z', modelsComplete: true, loadedCount: 1, busyCount: 1};
  assert.match(layout.machineStatusView(row, {now: 1000}).detail, /Unknown/);
});

test('pass 8: activity age expires before the lane probe deadline', () => {
  const { api, key, pairs } = laneInspector(laneModel(8), { laneFreshSeconds: 11.8 });
  assert.doesNotMatch(api.nodeSubtitle(key), /Generating|Busy|Idle/i);
  assert.doesNotMatch(pairs.Slots, /1\/2 busy/);
});

test('pass 8: unavailable routes stay unknown beside a live PC job and in an empty panel', () => {
  const { api, $ } = harness();
  const jobs = { schemaVersion: 1, journal:'recorded', complete:true, unsettled:0, availability: { state: 'available' }, inFlight: [], recent: [], lastSuccess: null };
  const base = { ...pcSnapshot(nowSeconds(), jobs), host: 'mac', activity: { runs: [], complete:true, historyBound:{truncated:false} }, sources: [
    { id: 'windows-jobs', state: 'live', ageSeconds:0 }, ...['router-active', 'monitor-receipts', 'router-archive'].map(id => ({ id, state: 'unavailable' }))] };
  api.setFeed(base);
  api.renderActivity([pcRow('live', 1, { state: 'in-flight' })]);
  assert.match($('activityTiles').textContent, /RoutesUnknown/);
  api.renderActivity([]);
  assert.match($('activityStatus').textContent, /unknown/i);
  assert.match($('activityList').textContent, /route.*unknown/i);
  api.setFeed({ ...base, sources: [{ id: 'windows-jobs', state: 'live', ageSeconds:0 }, { id: 'router-active', state: 'live', ageSeconds:0 }] });
  api.renderActivity([]);
  assert.match($('activityTiles').textContent, /RoutesUnknown/);
  api.setFeed({ ...base, sources: [{ id: 'windows-jobs', state: 'live', ageSeconds:0 }, ...['router-active', 'monitor-receipts', 'router-archive'].map(id => ({ id, state: 'live', ageSeconds:0 }))] });
  api.renderActivity([]);
  assert.match($('activityTiles').textContent, /Routes0/);
  assert.match($('activityList').textContent, /Nothing recorded yet/);
});

test('pass 8: aged activity cannot leave the runtime headline or inspector quiet', () => {
  const now = 1_800_000_000;
  class ClockDate extends Date { static now() { return now * 1000; } }
  const { api, $ } = harness({ extra: { Date: ClockDate } });
  api.setFeed({ schemaVersion: 1, host: 'windows', sampledAt: now, fullSampledAt: now,
    models: [laneModel(8)], clients: [], laneFreshSeconds: 11.8, activityKnown: true,
    sources: [{ id: 'lane-inventory', state: 'live' }], pipeline: { status: 'idle' } });
  api.renderSummary();
  assert.equal($('activeCount').textContent, '—');
  assert.match($('headline').textContent, /activity unknown/i);
  $('drawer').append($('inspector'));
  $('drawer').hidden = false;
  api.select('runtime');
  api.renderInspector();
  assert.match($('inspector').querySelector('.panel-summary').textContent, /Activity unknown/);
  assert.doesNotMatch(api.vitalsInput().vitals.mac.detail, /idle|quiet/i);
});
