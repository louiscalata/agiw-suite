import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {
  DEMO_BANNER, createMonitorDesk, demoSnapshot, deriveMonitors, healthPresentation,
  initialMonitorId, mapMonitors, moveMonitorTab, nodeInMonitor,
} from './web/monitor-tabs.mjs';

const read = name => readFileSync(new URL(name, import.meta.url), 'utf8');

function byId(monitors, id) {
  const found = monitors.find(item => item.id === id);
  assert.ok(found, id);
  return found;
}

test('an empty observer does not invent Claude, Nisi, or PC sessions', () => {
  const monitors = deriveMonitors(null, {feedFresh: true});
  assert.deepEqual(monitors.map(item => item.id), ['all']);
  assert.equal(monitors[0].word, 'UNKNOWN');
  assert.equal(JSON.stringify(monitors).includes('Claude'), false);
  assert.equal(JSON.stringify(monitors).includes('RUNNING'), false);
});

test('client tabs come from the snapshot and stay unknown without attributed work', () => {
  const snapshot = {schemaVersion: 1, host: 'mac', clients: [
    {id: 'claude', label: 'Claude', model: 'claude-example', modelState: 'observed', activity: 'unknown', detail: 'Recorded model identity; current activity unknown', models: [{id: 'claude-example'}]},
  ], pipeline: {status: 'idle', pipelines: [], pipelinesComplete: true, steps: []}};
  const monitors = deriveMonitors(snapshot, {feedFresh: true});
  assert.equal(byId(monitors, 'client:claude').word, 'UNKNOWN');
  assert.match(byId(monitors, 'client:claude').reason, /activity unknown/i);
  assert.equal(monitors.some(item => item.id === 'client:cursor'), false);
  assert.equal(monitors.some(item => item.id === 'machine:pc'), false);
});

test('parallel mac route, PC job, and Claude route are independent monitors', () => {
  const snapshot = demoSnapshot();
  const before = JSON.stringify(snapshot);
  const monitors = deriveMonitors(snapshot, {feedFresh: true, fictional: true});
  assert.equal(JSON.stringify(snapshot), before);
  assert.equal(byId(monitors, 'route:nisi').word, 'RUNNING');
  assert.equal(byId(monitors, 'machine:pc').word, 'RUNNING');
  assert.equal(byId(monitors, 'client:claude').word, 'RUNNING');
  assert.equal(byId(monitors, 'client:cursor').word, 'UNAVAILABLE');
  assert.match(byId(monitors, 'client:cursor').reason, /No supported bounded Cursor/);
  assert.equal(byId(monitors, 'route:demo-claude').identity.provider, 'claude');
  assert.equal(byId(monitors, 'job:demo-pc-job').identity.machine, 'windows');
  assert.notEqual(byId(monitors, 'route:demo-claude').id, byId(monitors, 'job:demo-pc-job').id);

  const next = demoSnapshot();
  next.pipeline.pipelines.find(row => row.runId === 'demo-claude').live = false;
  next.pipeline.pipelines.find(row => row.runId === 'demo-claude').status = 'waiting';
  const updated = deriveMonitors(next, {feedFresh: true, fictional: true});
  assert.equal(byId(updated, 'client:claude').word, 'QUEUED');
  assert.deepEqual(byId(updated, 'machine:pc'), byId(monitors, 'machine:pc'));
  assert.equal(byId(updated, 'job:demo-pc-job').word, 'RUNNING');
});

test('a stale or paused feed cannot keep a running claim', () => {
  const snapshot = demoSnapshot();
  assert.equal(byId(deriveMonitors(snapshot, {feedFresh: false, fictional: false}), 'client:claude').word, 'STALE');
  assert.equal(byId(deriveMonitors(snapshot, {feedFresh: true, paused: true}), 'machine:pc').word, 'STALE');
  const quiet = demoSnapshot();
  quiet.windowsJobs = {inFlight: quiet.windowsJobs.inFlight, liveAvailable: false, complete: true};
  assert.notEqual(byId(deriveMonitors(quiet, {feedFresh: true}), 'machine:pc').word, 'RUNNING');
  assert.equal(byId(deriveMonitors(quiet, {feedFresh: true}), 'job:demo-pc-job').word, 'UNKNOWN');
});

test('incomplete route lists are marked and extra sessions are not invented', () => {
  const snapshot = demoSnapshot();
  snapshot.pipeline.pipelinesComplete = false;
  const nisi = byId(deriveMonitors(snapshot, {feedFresh: true, fictional: true}), 'route:nisi');
  assert.equal(nisi.pipeline.incomplete, true);
  assert.equal(nisi.pipeline.steps.filter(step => step.id === 'demo-claude' || step.id === 'demo-mac-nisi').length, 2);
  assert.equal(nisi.pipeline.steps.some(step => step.id === 'not-in-sample'), false);
});

test('switching monitors keeps each view and does not fetch or touch lifecycle', async () => {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = (...args) => { calls.push(args[0]); return Promise.reject(new Error('fetch')); };
  try {
    const desk = createMonitorDesk();
    desk.note(demoSnapshot(), {feedFresh: true, fictional: true});
    assert.equal(desk.select('not-a-monitor'), false);
    assert.equal(desk.active, 'all');
    desk.patch('all', {search: 'gemma', boardQuery: 'nisi', mapSelected: 'runtime'});
    assert.equal(desk.select('client:claude'), true);
    desk.patch('client:claude', {boardQuery: 'checks', boardSelected: 'demo-claude', listView: 'runs'});
    assert.equal(desk.select('machine:pc'), true);
    desk.patch('machine:pc', {boardQuery: 'fast'});
    const next = demoSnapshot();
    next.windowsJobs.inFlight[0].ageSeconds = 9;
    desk.note(next, {feedFresh: true, fictional: true});
    assert.equal(desk.active, 'machine:pc');
    assert.equal(desk.view('client:claude').boardQuery, 'checks');
    assert.equal(desk.view('client:claude').boardSelected, 'demo-claude');
    assert.equal(desk.view('client:claude').listView, 'runs');
    assert.equal(desk.view('all').search, 'gemma');
    assert.equal(desk.view('machine:pc').boardQuery, 'fast');
    assert.equal(byId(desk.monitors, 'client:claude').word, 'RUNNING');
    assert.equal(calls.length, 0);
  } finally {
    globalThis.fetch = original;
  }
});

test('a session that leaves the sample is not kept as running', () => {
  const desk = createMonitorDesk();
  desk.note(demoSnapshot(), {feedFresh: true, fictional: true});
  assert.equal(desk.select('route:demo-claude'), true);
  desk.patch('route:demo-claude', {boardQuery: 'kept'});
  const gone = demoSnapshot();
  gone.pipeline.pipelines = gone.pipeline.pipelines.filter(row => row.runId !== 'demo-claude');
  desk.note(gone, {feedFresh: true, fictional: true});
  assert.equal(desk.active, 'all');
  assert.equal(desk.monitors.some(item => item.id === 'route:demo-claude'), false);
  assert.equal(desk.view('route:demo-claude').boardQuery, 'kept');
  assert.notEqual(byId(desk.monitors, 'client:claude').word, 'RUNNING');
});

test('arrow keys move the monitor strip without wrapping into a control', () => {
  const ids = ['all', 'machine:mac', 'machine:pc', 'client:claude'];
  assert.equal(moveMonitorTab(ids, 'all', 'ArrowRight'), 'machine:mac');
  assert.equal(moveMonitorTab(ids, 'all', 'ArrowLeft'), 'client:claude');
  assert.equal(moveMonitorTab(ids, 'machine:pc', 'Home'), 'all');
  assert.equal(moveMonitorTab(ids, 'machine:pc', 'End'), 'client:claude');
  assert.equal(moveMonitorTab(ids, 'all', 'Tab'), null);
  assert.equal(moveMonitorTab(ids, 'all', 'Enter'), null);
});

test('a monitor deep link is only an id', () => {
  assert.equal(initialMonitorId('?demo=1&monitor=client%3Aclaude'), 'client:claude');
  assert.equal(initialMonitorId('?monitor=javascript:alert(1)'), null);
  assert.equal(initialMonitorId(''), null);
});

test('map tabs follow map nodes and status words, not a palette', () => {
  assert.equal(healthPresentation({health: 'not_measured', reason: 'tunnel_down'}).word, 'DISCONNECTED');
  assert.equal(healthPresentation({health: 'ok'}).word, 'OK');
  assert.equal(healthPresentation(null, {feedUnavailable: true}).word, 'DISCONNECTED');
  const monitors = mapMonitors({nodes: [{id: 'mac', label: 'Mac', health: 'not_measured', reason: 'stale'}]}, {});
  assert.deepEqual(monitors.map(item => item.id), ['all', 'mac']);
  assert.equal(monitors[1].word, 'STALE');
  assert.equal(JSON.stringify(monitors).includes('Claude'), false);
});

test('graph dimming matches the selected monitor and leaves All alone', () => {
  const monitors = deriveMonitors(demoSnapshot(), {feedFresh: true, fictional: true});
  const claude = byId(monitors, 'client:claude');
  assert.equal(nodeInMonitor(byId(monitors, 'all'), {id: 'windows-worker', kind: 'windows-worker'}), true);
  assert.equal(nodeInMonitor(claude, {id: 'client:claude', kind: 'client'}), true);
  assert.equal(nodeInMonitor(claude, {id: 'client-model:claude:0', kind: 'client-model'}), true);
  assert.equal(nodeInMonitor(claude, {id: 'windows-worker', kind: 'windows-worker'}), false);
  assert.equal(nodeInMonitor(byId(monitors, 'machine:pc'), {id: 'runtime', kind: 'model', model: {host: 'mac'}}), false);
});

test('the demo fixture is labeled fictional and the painter does not inject markup', () => {
  const demo = demoSnapshot();
  assert.equal(demo.fictional, true);
  assert.equal(demo.label, DEMO_BANNER);
  assert.match(DEMO_BANNER, /fictional/i);
  const board = read('./web/monitor-board.mjs');
  assert.equal(board.includes('innerHTML'), false);
  assert.match(board, /does not pause, cancel, or restart inference/);
  const css = read('./web/jaryo.css');
  assert.match(css, /outline: 3px dashed var\(--j-focus\)/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.doesNotMatch(css, /#C47A3A/i);
  const html = read('./web/index.html');
  assert.match(html, /id="monitorTabs"[^>]*role="tablist"/);
  assert.match(html, /data-jaryo-tabs="1"/);
  const app = read('./web/app.js');
  const select = app.slice(app.indexOf('function selectMonitor'), app.indexOf('function bindMonitorTabs'));
  assert.doesNotMatch(select, /paused\s*=|fetch\(|requestOnlineCode|requestModel|requestAutoUnload/);
  assert.match(app, /Switching monitors only changes this view|selectMonitor/);
});
