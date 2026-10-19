import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import {activityFeed,routeReviewCounts,memoryView,macGpuView,pcGpuView,newestRunsFirst,vitalsView} from './web/map-layout.mjs';
import {fitGraph,constellationLayout,focusedLaneLayout,prioritizeRuntimeModels,runtimeModelRoster,CORE_MODEL_IDS,hasAdvertisedWindowsWorker,afmView,jevView,modelStarSize,windowsJobsView,windowsJobsEvidence,windowsLaneView,macPeerView,macReportView,agiwPeersRows,friendlyModelName,pcReportView,pcAdaptersView} from './web/map-layout.mjs';

// The Mac map's Windows PC node names the link the PC feed came over, in the PC edition's wording for its own
// Mac node: the paired, signed LAN link is LINKED, an unsigned SharedChami file only REPORTED, and nothing when
// the feed is stale. Exercised through the production helper and through runtimeGraph itself.
const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
const start = app.indexOf('function pcLiveRows(');
const end = app.indexOf('\nfunction headlessView(', start);
assert.ok(start >= 0 && end > start && app.slice(start, end).includes('function pcLinkLabel('), 'helper source boundaries exist');
const formatters = ['ageText', 'capital'].map(name => app.match(new RegExp(`^const ${name}=.*;$`, 'm'))[0]).join('\n');
const pcLinkLabel = new Function('pcAdaptersView', `${formatters}\n${app.slice(start, end)}\nreturn pcLinkLabel;`)(pcAdaptersView);

const live = (over = {}) => ({via: 'lan', host: 'winpc01', ageSeconds: 4, modelsComplete: true,
  models: [{id: 'gpt-oss-20b', loadedState: 'loaded', activity: 'idle', slotsBusy: 0, slotsTotal: 2}], ...over});
const fresh = {feedFresh: true, snapshotAge: 1};

test('a fresh LAN feed is LINKED and a fresh share feed only REPORTED, as the PC words its Mac node', () => {
  assert.equal(pcLinkLabel(live({via: 'lan'}), fresh), 'LINKED VIA LAN');
  assert.equal(pcLinkLabel(live({via: 'share'}), fresh), 'REPORTED VIA SHARE');
});

test('stale, paused-equivalent, malformed or unknown-transport feeds carry no link word', () => {
  assert.equal(pcLinkLabel(live(), {feedFresh: false, snapshotAge: 0}), null);
  for (const [ageSeconds, snapshotAge] of [[29.1, 1], [31, 0], [-2, 0], [NaN, 0], [4, NaN]]) {
    assert.equal(pcLinkLabel(live({ageSeconds}), {feedFresh: true, snapshotAge}), null, `${ageSeconds}/${snapshotAge}`);
  }
  for (const value of [null, undefined, {}, live({models: null}), live({via: 'mdns'}), live({via: undefined})]) {
    assert.equal(pcLinkLabel(value, fresh), null);
  }
});

const layoutContext = {fitGraph,constellationLayout,focusedLaneLayout,prioritizeRuntimeModels,runtimeModelRoster,CORE_MODEL_IDS,hasAdvertisedWindowsWorker,afmView,jevView,modelStarSize,windowsJobsView,windowsJobsEvidence,windowsLaneView,macPeerView,macReportView,agiwPeersRows,friendlyModelName,pcReportView,pcAdaptersView};
const source = app.replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];
function pcNode(windowsLive, {paused = false, worker, jobs} = {}) {
  const extra = JSON.stringify({windowsLive, ...(worker ? {windowsWorker: worker} : {}), ...(jobs ? {windowsJobs: jobs} : {})});
  const script = `${source}\nsnapshot={host:'mac',sampledAt:Date.now()/1000,models:[],clients:[],sources:[{id:'windows-jobs',state:'recorded',observedAgeSeconds:0}],pipeline:{status:'idle'},...${extra}};connected=true;paused=${paused};runtimeGraph();graph.nodes.find(n=>n.kind==='windows-worker')`;
  const context = {...layoutContext, Date, Set, Map, Math, Number, String, Array, Object, JSON, window: {innerWidth: 820},
    document: {getElementById: id => id === 'graphRegion' ? {clientWidth: 590, clientHeight: 546} : null}};
  return JSON.parse(JSON.stringify(runInNewContext(script, context)));
}

test('the Windows PC node subtitle leads with the link word while the PC feed is fresh', () => {
  assert.equal(pcNode(live({via: 'lan'})).subtitle, 'LINKED VIA LAN · WORKER UNVERIFIED · INFERENCE UNKNOWN');
  assert.equal(pcNode(live({via: 'share'})).subtitle, 'REPORTED VIA SHARE · WORKER UNVERIFIED · INFERENCE UNKNOWN');
  // An advertised worker with no dispatcher evidence: the PC's own report replaces "inference unknown" (improvement 3).
  const worker = {state: 'advertised', ageSeconds: 4, modelsAdvertised: ['local/a'], detail: 'Heartbeat inventory only'};
  assert.equal(pcNode(live({via: 'lan'}), {worker}).subtitle, 'LINKED VIA LAN · 1 LOADED · IDLE');
});

test('the link word never reaches the short above-hub caption or claims activity', () => {
  const node = pcNode(live({via: 'lan'}));
  assert.equal(node.subtitleAbove, 'WORKER UNVERIFIED');
  assert.equal(node.unknown, true);
  assert.equal(node.inFlight, false);
});

test('a stale, paused or absent PC feed leaves the existing subtitle untouched', () => {
  for (const [windowsLive, options] of [[live({ageSeconds: 45}), {}], [live(), {paused: true}], [null, {}], [undefined, {}]]) {
    assert.equal(pcNode(windowsLive, options).subtitle, 'WORKER UNVERIFIED · INFERENCE UNKNOWN');
  }
});

// Improvement 3 (10 Oct 2026): with no dispatcher evidence, the node leads with the PC's own fresh report instead of
// "WORKER ADVERTISED · INFERENCE UNKNOWN". The live snapshot of 21:18:56Z, reduced to what the node reads.
const twoIdle = live({via: 'share', ageSeconds: 9.9, health: 'ok', models: [
  {id: 'gpt-oss-20b', loadedState: 'loaded', activity: 'idle', slotsBusy: 0, slotsTotal: 2},
  {id: 'Qwen3.8-27B Q4_K_M', loadedState: 'loaded', activity: 'idle', slotsBusy: 0, slotsTotal: 2}]});
const offWorker = {state: 'advertised', ageSeconds: 22.7, modelsAdvertised: ['gpt-oss-20b', 'Qwen3.8-27B Q4_K_M'], detail: 'Heartbeat inventory only',
  headless: {state: 'off', reason: 'turned off', expiresInSeconds: null},
  lanes: {fast: {up: true, model: 'gpt-oss-20b', slotsBusy: 0, slotsTotal: 2}, deep: {up: true, model: 'Qwen3.8-27B Q4_K_M', slotsBusy: 0, slotsTotal: 2}}};
const JOB_ID = 'mac-20261010-211800-' + 'd'.repeat(32);

test('an advertised PC with no dispatcher evidence leads with its own report, never live', () => {
  const node = pcNode(twoIdle, {worker: offWorker});
  assert.equal(node.subtitle, 'REPORTED VIA SHARE · 2 LOADED · IDLE · HEADLESS OFF');
  // The brief above the hub is unchanged, and a report neither pulses nor verifies inference.
  assert.equal(node.subtitleAbove, 'WORKER ADVERTISED');
  assert.deepEqual([node.inFlight, node.verified, node.unknown, Boolean(node.active)], [false, false, true, false]);
  const busy = live({via: 'lan', models: [{id: 'gpt-oss-20b', loadedState: 'loaded', activity: 'generating', slotsBusy: 1, slotsTotal: 2}]});
  assert.equal(pcNode(busy, {worker: offWorker}).subtitle, 'LINKED VIA LAN · 1 LOADED · 1 BUSY · HEADLESS OFF');
  assert.equal(pcNode(busy, {worker: offWorker}).inFlight, false);
  // The PC's own bad news names its subject, so it never reads as the LAN link being down or degraded.
  assert.equal(pcNode(live({health: 'down'}), {worker: offWorker}).subtitle, 'LINKED VIA LAN · PC REPORTS DOWN · HEADLESS OFF');
  const oneLaneDown = live({health: 'degraded', models: [{id: 'gpt-oss-20b', loadedState: 'loaded', activity: 'idle', slotsBusy: 0, slotsTotal: 2},
    {id: 'Qwen3.8-27B Q4_K_M', loadedState: 'unknown', activity: 'unknown'}]});
  assert.equal(pcNode(oneLaneDown, {worker: offWorker}).subtitle, 'LINKED VIA LAN · PC REPORTS DEGRADED · AT LEAST 1 LOADED · ACTIVITY UNKNOWN · HEADLESS OFF');
});

test('dispatcher evidence and worker conditions still win over the report', () => {
  const inFlight = {schemaVersion: 1, journal:'recorded',complete:true, inFlight: [{id: JOB_ID, model: 'gpt-oss-20b', client: 'claude', lane: 'fast', ageSeconds: 7, timeoutSeconds: 60}], recent: [], lastSuccess: null};
  assert.match(pcNode(twoIdle, {worker: offWorker, jobs: inFlight}).subtitle, /^REPORTED VIA SHARE · JOB IN FLIGHT · GPT OSS 20b · \d+s · HEADLESS OFF$/);
  const done = {id: JOB_ID, state: 'success', model: 'gpt-oss-20b', elapsedSeconds: 6.25, ageSeconds: 30};
  assert.equal(pcNode(twoIdle, {worker: offWorker, jobs: {schemaVersion: 1, journal:'recorded',complete:true, inFlight: [], recent: [done], lastSuccess: done}}).subtitle,
    'REPORTED VIA SHARE · LAST INFERENCE · GPT OSS 20b · 6.3s · HEADLESS OFF');
  assert.equal(pcNode(twoIdle, {worker: {...offWorker, state: 'degraded'}}).subtitle, 'REPORTED VIA SHARE · WORKER DEGRADED · NO MODEL LANE · HEADLESS OFF');
  assert.equal(pcNode(twoIdle, {worker: {...offWorker, state: 'stopped'}}).subtitle, 'REPORTED VIA SHARE · WORKER STOPPED · HEADLESS OFF');
});

test('without the link word (stale, paused, old or unknown transport) the report never reaches the node', () => {
  const words = 'WORKER ADVERTISED · INFERENCE UNKNOWN · HEADLESS OFF';
  assert.equal(pcNode(twoIdle, {worker: offWorker, paused: true}).subtitle, words);
  assert.equal(pcNode({...twoIdle, ageSeconds: 30.5}, {worker: offWorker}).subtitle, words);
  assert.equal(pcNode({...twoIdle, via: 'mdns'}, {worker: offWorker}).subtitle, words);
  // An unverified (stale or missing) heartbeat keeps its own words even beside a fresh report.
  assert.equal(pcNode(twoIdle).subtitle, 'REPORTED VIA SHARE · WORKER UNVERIFIED · INFERENCE UNKNOWN');
});

// vitalsInput wires the chip (improvement 3): the report is read with liveNow() (fresh and unpaused) and only on the
// Mac host; the Windows edition's second chip is the Mac peer. Run through app.js itself, with vitalsView watched so
// the pcReport it receives is checked even where the Windows host replaces the chip afterwards.
function pcChip(windowsLive, {paused = false, host = 'mac', worker = offWorker, macPeer = null, peers = null, activity = null, components = null} = {}) {
  const seen = [];
  const extra = JSON.stringify({host, windowsLive, windowsWorker: worker, macPeer, peers, ...(activity ? {activity} : {}), ...(components ? {components} : {})});
  const script = `${source}\nsnapshot={sampledAt:Date.now()/1000,models:[],clients:[],sources:[{id:'windows-jobs',state:'recorded',observedAgeSeconds:0}],pipeline:{status:'idle'},...${extra}};connected=true;paused=${paused};vitalsInput().vitals`;
  const context = {...layoutContext, activityFeed, routeReviewCounts, memoryView, macGpuView, pcGpuView, newestRunsFirst,
    vitalsView: input => { seen.push(input.pcReport); return vitalsView(input); },
    Date, Set, Map, Math, Number, String, Array, Object, JSON, window: {innerWidth: 820}, document: {getElementById: () => null}};
  const vitals = JSON.parse(JSON.stringify(runInNewContext(script, context)));
  assert.equal(seen.length, 1);
  return {vitals, pcReport: seen[0]};
}

test('vitalsInput: the Mac PC chip leads with a fresh, unpaused report', () => {
  const {vitals, pcReport} = pcChip(twoIdle);
  assert.equal(pcReport.known, true);
  assert.ok(vitals.pc.short.startsWith('Reports 2 loaded · idle'), vitals.pc.short);
  assert.deepEqual([vitals.pc.short, vitals.tiny.pc, vitals.pc.tone], ['Reports 2 loaded · idle · Headless off', 'Reports idle', 'ok']);
  // The PC's own "degraded" reaches the tiny form under a warn tone.
  const degraded = pcChip({...twoIdle, health: 'degraded'}).vitals;
  assert.deepEqual([degraded.tiny.pc, degraded.pc.tone], ['Reports degraded', 'warn']);
});

test('vitalsInput: a paused view reads no report, so the PC chip carries no "Reports" word', () => {
  const {vitals, pcReport} = pcChip(twoIdle, {paused: true});
  assert.deepEqual(pcReport, {known: false});
  for (const text of [vitals.pc.short, vitals.pc.detail, vitals.tiny.pc]) assert.ok(!text.includes('Reports'), text);
  assert.deepEqual([vitals.pc.short, vitals.tiny.pc], ['Headless off', 'Headless off']);
});

test('vitalsInput: the Windows host computes no report; its second chip is the Mac peer', () => {
  const {vitals, pcReport} = pcChip(twoIdle, {host: 'windows'});
  assert.equal(pcReport, null);
  const chip = macPeerView(null, {feedFresh: true, snapshotAge: 0}).chip;
  assert.deepEqual([vitals.pc, vitals.tiny.pc], [{tone: chip.tone, detail: chip.detail, short: chip.short}, chip.short]);
  for (const text of [vitals.pc.short, vitals.pc.detail, vitals.tiny.pc]) assert.ok(!text.includes('Reports'), text);
});

test('vitalsInput: the Windows Mac chip leads with the fresh AGIW peer report only', () => {
  const macPeer = {name:'mac-mini',configuredName:'mac-mini',state: 'reachable', ageSeconds: 5, latencyMs: 12, loadedCount: 1,
    models: [{id: 'gpt-oss-20b', state: 'loaded'}]};
  const report = {host: 'mac-mini', platform: 'macos', source: 'agiw-peers', fresh: true, modelsComplete: true, ageSeconds: 4, health: 'ok',
    models: [{id: 'gpt-oss-20b', loadedState: 'loaded', activity: 'generating', slotsBusy: 1}]};
  const {vitals} = pcChip(null, {host: 'windows', macPeer, peers: [report]});
  assert.deepEqual([vitals.pc.tone, vitals.pc.detail, vitals.pc.short, vitals.tiny.pc],
    ['ok', 'Reports 1 loaded · 1 busy · Reachable · 12 ms', 'Reports 1 loaded · 1 busy', 'Reports 1 busy']);
  // An independent LAN count that disagrees with the Mac's own report stays visible; an agreeing one is not repeated.
  const two = {...report, models: [...report.models, {id: 'qwen/qwen3.8-27b', loadedState: 'loaded', activity: 'idle'}]};
  assert.equal(pcChip(null, {host: 'windows', macPeer, peers: [two]}).vitals.pc.detail,
    'Reports 2 loaded · 1 busy · Reachable · 12 ms · LM Studio lists 1 loaded');
  const down = pcChip(null, {host: 'windows', macPeer, peers: [{...report, health: 'down'}]}).vitals;
  assert.equal(down.pc.tone, 'warn', 'only report health can lower an otherwise-ok peer chip');
  for (const invalid of [
    [{...report, source: 'mac-status.json'}], [{...report, fresh: false}], [{...report, ageSeconds: 30.1}],
    [{...report, platform: 'windows'}],
  ]) {
    const fallback = pcChip(null, {host: 'windows', macPeer, peers: invalid}).vitals;
    assert.deepEqual(fallback.pc, {tone: 'ok', detail: 'Reachable · 12 ms · 1 loaded', short: '1 loaded'});
  }
});

test('the Windows Mac report chip keeps current LAN failure visible in every short form', () => {
  const macPeer={name:'mac-mini',configuredName:'mac-mini',state:'unreachable',reason:'timeout',ageSeconds:1,loadedCount:0,models:[]};
  const report={host:'mac-mini',platform:'macos',source:'agiw-peers',fresh:true,modelsComplete:true,ageSeconds:1,health:'ok',
    models:[{id:'a',loadedState:'loaded',activity:'idle'}]};
  const {vitals}=pcChip(null,{host:'windows',macPeer,peers:[report]});
  assert.equal(vitals.pc.short,'No answer · Reports 1 loaded · idle');
  assert.equal(vitals.tiny.pc,'No answer');
  assert.equal(vitals.micro.pc,'No answer');
});

test('vitalsInput: a current LAN failure survives the share link in every chip form', () => {
  const macPeer = {state: 'linked', name: 'mac-mini', configuredName: 'mac-mini', ageSeconds: 2, linkAgeSeconds: 2, loadedCount: 1, lanFailure: 'timeout', lanFailureAgeSeconds: 5,
    models: [{id: 'gpt-oss-20b', state: 'loaded'}]};
  const report = {host: 'mac-mini', platform: 'macos', source: 'agiw-peers', fresh: true, modelsComplete: true, ageSeconds: 2, health: 'ok',
    models: [{id: 'gpt-oss-20b', loadedState: 'loaded', activity: 'idle'}]};
  const {vitals} = pcChip(null, {host: 'windows', macPeer, peers: [report]});
  assert.deepEqual([vitals.pc.detail, vitals.pc.short, vitals.tiny.pc],
    ['Reports 1 loaded · idle · Reported via SharedChami (unverified) · LAN: No answer (timed out)', 'No answer · Reports 1 loaded · idle', 'No answer']);
});

test('vitalsInput: only the configured Mac is credited with a report, never the LAN cache name (Astra merge P2)', () => {
  const report = {host: 'mac-mini', platform: 'macos', source: 'agiw-peers', fresh: true, modelsComplete: true, ageSeconds: 2, health: 'ok',
    models: [{id: 'gpt-oss-20b', loadedState: 'loaded', activity: 'idle'}]};
  const base = {state: 'reachable', ageSeconds: 5, latencyMs: 12, loadedCount: 1, models: [{id: 'gpt-oss-20b', state: 'loaded'}]};
  // The LAN cache still names the previous Mac; the configured identity is mac-mini: the report counts.
  assert.equal(pcChip(null, {host: 'windows', macPeer: {...base, name: 'old-mac', configuredName: 'mac-mini'}, peers: [report]}).vitals.pc.short,
    'Reports 1 loaded · idle');
  // The LAN cache names mac-mini but nothing is configured: no report is attributed.
  assert.equal(pcChip(null, {host: 'windows', macPeer: {...base, name: 'mac-mini'}, peers: [report]}).vitals.pc.short, '1 loaded');
});

test('route review counts are computed only on the Mac host', () => {
  const flagged = {source: 'router-active', recordedAt: Date.now() / 1000, status: 'FAILED',
    reviewConsistency: {status: 'BLOCK', rule: 'review-block'}};
  const {vitals} = pcChip(null, {host: 'windows', activity: {runs: [flagged], historyBound: {truncated: false}},
    components: [{id: 'nisi', state: 'ready'}]});
  assert.equal(vitals.route.detail, 'Idle · pair ready');
  assert.doesNotMatch(vitals.route.detail, /review|defect|warning/i);
  assert.match(app, /routeReviews:snapshot\?\.host==='mac'\?routeReviewCounts\(runs\(\),snapshot\?\.activity\?\.historyBound\):null/);
});
