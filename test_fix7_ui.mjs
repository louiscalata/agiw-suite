import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { activitySummary, agiwPeersRows, localCallersView, memoryView, pcAdaptersView, pcReportView, vitalsView, windowsLaneActivitySummary, windowsLaneView } from './web/map-layout.mjs';

test('unknown resident inventory cannot become a quiet PC report', () => {
  const view = pcReportView({ via: 'share', ageSeconds: 1, health: 'ok', modelsComplete: false,
    models: [{ loadedState: 'loaded', activity: 'idle' }, { loadedState: 'unknown' }] }, { feedFresh: true });
  assert.match(view.words, /activity unknown/);
  assert.equal(view.tiny, 'Reports activity unknown');
  const vitals = vitalsView({ fresh: true, pcFresh: true, worker: { state: 'advertised' }, pcReport: view });
  assert.equal(vitals.pc.tone, 'ok');
});

test('headless status needs a current heartbeat and subtracts heartbeat age from expiry', () => {
  const old = windowsLaneView({ state: 'advertised', ageSeconds: 7200, headless: { state: 'off' } }, null, { feedFresh: true });
  assert.equal(old.headless.label, 'Headless unknown');
  const fresh = windowsLaneView({ state: 'advertised', ageSeconds: 10, headless: { state: 'on', expiresInSeconds: 300 } }, null, { feedFresh: true, snapshotAge: 5 });
  assert.equal(fresh.headless.label, 'Headless on · 0h 4m left');
  const malformed = windowsLaneView({ state: 'garbage', ageSeconds: 0, headless: { state: 'on', expiresInSeconds: 900 } }, null, { feedFresh: true });
  assert.equal(malformed.headless.label, 'Headless unknown');
});

test('null slot activity renders the exact lane inspector summary without claiming idle', () => {
  assert.deepEqual(windowsLaneActivitySummary({ up: true, live: false, busy: null, total: null }), { title: 'Up · activity unknown', tone: 'muted' });
});

test('unknown lane slots and unknown job outcomes stay neutral', () => {
  const lane = windowsLaneView({ state: 'advertised', ageSeconds: 0, lanes: { fast: { up: true, model: 'x', slotsBusy: null, slotsTotal: null } } }, null, { feedFresh: true });
  assert.equal(lane.lanes[0].busy, null);
  const summary = activitySummary([{ key: 'x', state: 'unknown', kind: 'pc-job', target: 'PC' }], { live: true });
  assert.equal(summary.tone, 'muted');
  assert.equal(summary.line, 'Outcome unknown');
  assert.equal(summary.tiles.find(([label]) => label === 'Failed')[1], '0');
  const activity = vitalsView({ fresh: true, feed: [], jobs: { feedAvailable: false } }).activity;
  assert.equal(activity.detail, 'Activity unknown (feed unavailable or stale)');
  assert.equal(activity.short, 'Unknown');
  assert.equal(vitalsView({ fresh: false, feed: [], activityAvailable: true }).activity.detail, 'Activity unknown (feed unavailable or stale)');
  assert.equal(vitalsView({ fresh: true, feed: [], activityAvailable: false }).activity.short, 'Unknown');
});

test('caller names follow the producer format and preserve rejected or overflow counts', () => {
  const names = ['client (helper)', ...Array.from({ length: 7 }, (_, i) => `caller-${i}`), 'bad/name'].map(name => ({ name }));
  const view = localCallersView(names, { feedFresh: true });
  assert.equal(view.names[0], 'client (helper)');
  assert.match(view.text, /1 unnamed caller/);
  assert.match(view.text, /\+2 more/);
  assert.doesNotMatch(localCallersView([{ name: 'bad/name' }], { feedFresh: true }).text, /None/);
});

test('adapter summary names every card even when one load reading is unknown', () => {
  const view = pcAdaptersView([{ name: 'NVIDIA GeForce RTX 3080 Ti', utilizationPercent: 35 },
    { name: 'AMD Radeon RX 5700 XT', utilizationPercent: null }], { feedFresh: true });
  assert.equal(view.chip, 'GPU 35%/unknown');
  assert.match(view.tile, /RX 5700 XT unknown/);
  assert.match(view.rows[1][1], /load unknown/);
});

test('failed memory process enumeration is distinguishable from no consumers', () => {
  const view = memoryView({ level: 'watch', consumers: [], consumersKnown: false }, { feedFresh: true });
  assert.equal(view.consumersKnown, false);
});

test('refused peer sources and stale peer ages remain explicit', () => {
  assert.equal(agiwPeersRows([], { feedFresh: true, source: { reason: 'peers-unreadable' } })[0][1], 'Unknown (peer folder unreadable)');
  const [row] = agiwPeersRows([{ host: 'linux-node', platform: 'linux', version: '1', ageSeconds: 600, fresh: false,
    modelsComplete: true, models: [{ loadedState: 'loaded' }], source: 'agiw-peers' }], { feedFresh: true, host: 'windows' });
  assert.match(row[1], /10 min ago/);
  assert.match(row[1], /at last report/);
});

test('app wires Windows peer source, hides unknown recovery/activity as unknown, and reports stale live sources honestly', () => {
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  assert.match(app, /agiwPeersRows\(snapshot\?\.peers,\{feedFresh:fresh\(\),snapshotAge:Math\.max\(0,sampleAge\(\)\),host:snapshot\?\.host,source:Array\.isArray\(snapshot\?\.sources\)\?snapshot\.sources\.find\(s=>s\.id==='agiw-peers'\):null\}\)/);
  assert.match(app, /recoveryKnown=isFresh&&snapshot\.host==='mac'/);
  assert.match(app, /live unknown \(/);
  assert.match(app, /agiwPeersRows\(snapshot\?\.peers/);
});
