// Diagnostics destination: what is not live, and why. Source-level checks from the shared fleet
// fixture; no browser, socket or model is used. Visual rendering in a browser is not checked here.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {harness} from './test_support/monitor-harness.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const fixture = JSON.parse(read('./test_support/fleet-state.json'));
const html = read('./web/index.html');
class Clock extends Date {static now() {return fixture.now * 1000;}}
const iso = seconds => new Date(seconds * 1000).toISOString();

function sample() {
  const now = fixture.now;
  return {
    ...structuredClone(fixture.snapshot), host: 'mac', sampledAt: now, fullSampledAt: now,
    fleetState: structuredClone(fixture.fleetState),
    sources: [
      {id: 'lmstudio-api', label: 'LM Studio inventory', state: 'live', detail: 'Inventory read', ageSeconds: 1},
      {id: 'pipeline-router', label: 'Pipeline', state: 'unavailable', detail: 'Router journal not found', ageSeconds: null},
      {id: 'windows-jobs', label: 'Windows jobs', state: 'error', reasonCode: 'journal_invalid', detail: 'Journal line invalid', ageSeconds: 12},
      {id: 'router-archive', label: 'Router archive', state: 'recorded', detail: 'Archived routes', observationAgeSeconds: 40},
    ],
    windowsJobs: {schemaVersion: 1, journal: 'present', complete: true, inFlight: [], lastSuccess: null, recent: [
      {id: 'mac-job-success-0001', state: 'success', model: 'pc/fast', lane: 'fast', ageSeconds: 5, elapsedSeconds: 2},
      {id: 'mac-job-error-00002', state: 'error', model: 'pc/deep', lane: 'deep', ageSeconds: 30, elapsedSeconds: 9},
      {id: 'mac-job-invalid-003', state: 'invalid', model: 'pc/fast', lane: 'fast', ageSeconds: 60, elapsedSeconds: 1},
      {id: 'mac-job-cancel-0004', state: 'cancelled', model: 'pc/fast', lane: 'fast', ageSeconds: 70, elapsedSeconds: 1},
    ]},
    inference: {...structuredClone(fixture.snapshot.inference), tunnel: {state: 'down', reason: 'tunnel_down', detail: 'fixture',
      forwards: [
        {name: 'fast', id: 'fast', localPort: 21001, remotePort: 21101, open: true},
        {name: 'deep', id: 'deep', localPort: 21002, remotePort: 21102, open: false},
      ]}},
  };
}
const issues = [{stage: 'inference', code: 'TimeoutError', failedAt: fixture.now - 20}];

function view(snapshot = sample(), options = {}) {
  const h = harness({extra: {Date: Clock}});
  h.api.setFeed(snapshot);
  // Copy out of the page's realm so deep equality compares values only.
  return JSON.parse(JSON.stringify(h.api.diagnosticsView(snapshot, {feedFresh: true, paused: false, now: fixture.now, collector: issues, ...options})));
}

test('every source that is not live is listed with its code, scope and time', () => {
  const rows = view().sources;
  assert.deepEqual(rows.map(row => [row.scope, row.code, row.time]), [
    ['Source Pipeline (pipeline-router)', 'unavailable', 'time unknown'],
    ['Source Windows jobs (windows-jobs)', 'journal_invalid', iso(fixture.now - 12)],
    ['Source Router archive (router-archive)', 'recorded', iso(fixture.now - 40)],
  ]);
});

test('a stale feed lists every source as current state unknown, never live', () => {
  const rows = view(sample(), {feedFresh: false}).sources;
  assert.equal(rows.length, 4);
  assert.deepEqual(rows.find(row => row.scope.includes('lmstudio-api')).code, 'stale');
  assert.match(rows.find(row => row.scope.includes('lmstudio-api')).detail, /current state unknown/i);
  // Error and unavailable keep their own code: the last sample said so.
  assert.equal(rows.find(row => row.scope.includes('windows-jobs')).code, 'journal_invalid');
  const paused = view(sample(), {feedFresh: true, paused: true}).sources;
  assert.match(paused.find(row => row.scope.includes('lmstudio-api')).detail, /view paused/i);
});

test('every lane with a reason is listed from the fleet contract, answering lanes are not', () => {
  const rows = view().lanes;
  assert.deepEqual(rows.map(row => [row.scope, row.code, row.time]), [
    ['Lane Mac Pro deep (registry:pro:deep)', 'pending', 'time unknown'],
  ]);
  const snapshot = sample();
  const lane = snapshot.fleetState.facts.find(fact => fact.id === 'registry:pc:deep');
  Object.assign(lane, {state: 'endpoint unavailable', reason: 'timeout'});
  const failed = view(snapshot).lanes.find(row => row.scope.includes('registry:pc:deep'));
  assert.deepEqual([failed.code, failed.time], ['timeout', lane.observedAt]);
  const missing = view({...sample(), fleetState: undefined}).lanes;
  assert.equal(missing.length, 1);
  assert.match(missing[0].code, /NOT AVAILABLE YET/);
});

test('every failed job and collector failure is listed with its code, scope and time', () => {
  const rows = view().jobs;
  assert.deepEqual(rows.map(row => [row.scope, row.code, row.time]), [
    ['Windows job mac-job-error-00002 · lane deep · pc/deep', 'error', iso(fixture.now - 30)],
    ['Windows job mac-job-invalid-003 · lane fast · pc/fast', 'invalid', iso(fixture.now - 60)],
    ['Collector stage inference', 'TimeoutError', iso(fixture.now - 20)],
  ]);
  const unknown = view({...sample(), windowsJobs: null}, {collector: []}).jobs;
  assert.equal(unknown.length, 1);
  assert.match(unknown[0].code, /NOT AVAILABLE YET/);
});

test('each local listener is accepting or not accepting, and the cause is unknown', () => {
  const rows = view().listeners;
  assert.deepEqual(rows.map(row => [row.scope, row.code]), [
    ['Listener fast · 127.0.0.1:21001', 'accepting'],
    ['Listener deep · 127.0.0.1:21002', 'not accepting'],
  ]);
  for (const row of rows) assert.match(row.detail, /cause unknown/);
  const stale = view(sample(), {feedFresh: false}).listeners;
  for (const row of stale) assert.match(row.detail, /at last sample/);
  const none = view({...sample(), inference: {...sample().inference, tunnel: null}}).listeners;
  assert.equal(none.length, 1);
  assert.match(none[0].code, /NOT AVAILABLE YET/);
});

test('tunnel ownership, physical cause and incident history read NOT AVAILABLE YET', () => {
  assert.deepEqual(view().unavailable.map(row => [row.scope, row.code]), [
    ['Tunnel ownership', 'NOT AVAILABLE YET'],
    ['Physical cause', 'NOT AVAILABLE YET'],
    ['Incident history', 'NOT AVAILABLE YET'],
  ]);
});

test('opening Diagnostics renders the list with GET requests only, and repair still needs the review', () => {
  assert.match(html, /id="diagnosticsIssueRows"/, 'the panel has a diagnostics list');
  const panel = html.slice(html.indexOf('id="fixInferenceDetails"'), html.indexOf('</section>', html.indexOf('id="fixInferenceDetails"')));
  assert.ok(panel.includes('id="diagnosticsIssueRows"'), 'the list sits in the Diagnostics and repair panel');
  const requests = [];
  const fetch = (url, options = {}) => { requests.push({url, method: options.method || 'GET'}); return new Promise(() => {}); };
  const timers = {setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {}};
  const h = harness({extra: {Date: Clock, fetch, AbortController, ...timers}});
  h.api.setFeed(sample());
  h.api.setCollector(issues);
  h.api.openFixInference();
  const text = h.$('diagnosticsIssueRows').textContent;
  for (const expected of ['pipeline-router', 'journal_invalid', 'registry:pro:deep', 'mac-job-error-00002', 'TimeoutError',
    '127.0.0.1:21002', 'not accepting', 'cause unknown', 'Tunnel ownership', 'Incident history', 'NOT AVAILABLE YET'])
    assert.ok(text.includes(expected), `${expected} is shown`);
  assert.ok(!text.includes('mac-job-success-0001') && !text.includes('mac-job-cancel-0004'), 'successful and cancelled jobs are not failures');
  assert.ok(requests.length > 0, 'opening reads cached readiness');
  assert.ok(requests.every(row => row.method === 'GET'), JSON.stringify(requests));
  // The repair form is unchanged: it opens a review, never a request.
  assert.match(read('./web/app.js'), /function reviewOnlineCodeAction\(action\)[\s\S]*?openActionReview\(/);
});

test('reopening with the same evidence keeps the rendered rows', () => {
  const fetch = () => new Promise(() => {});
  const h = harness({extra: {Date: Clock, fetch, AbortController, setTimeout: () => 1, clearTimeout() {}}});
  h.api.setFeed(sample());
  h.api.openFixInference();
  const first = h.$('diagnosticsIssueRows').children[0];
  h.api.renderDiagnostics();
  assert.equal(h.$('diagnosticsIssueRows').children[0], first, 'an unchanged view is not rebuilt');
});
