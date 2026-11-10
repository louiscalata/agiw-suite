import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {resolveTheme} from './jaryo/theme.mjs';
import {
  DEMO_BANNER, analysisSlots, createMonitorDesk, demoSnapshot, deriveMonitors, healthPresentation,
  evidenceFor, initialMonitorId, jevGate, mapEvidence, mapMonitors, moveMonitorTab, nextTabFocus, nodeInMonitor,
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
  assert.equal(byId(monitors, 'route:mac:demo-claude').identity.provider, 'claude');
  assert.equal(byId(monitors, 'job:demo-pc-job').identity.machine, 'windows');
  assert.notEqual(byId(monitors, 'route:mac:demo-claude').id, byId(monitors, 'job:demo-pc-job').id);

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

test('a session that leaves the sample is retained disconnected and not called running', () => {
  const desk = createMonitorDesk();
  desk.note(demoSnapshot(), {feedFresh: true, fictional: true});
  assert.equal(desk.select('route:mac:demo-claude'), true);
  desk.patch('route:mac:demo-claude', {boardQuery: 'kept'});
  const gone = demoSnapshot();
  gone.pipeline.pipelines = gone.pipeline.pipelines.filter(row => row.runId !== 'demo-claude');
  desk.note(gone, {feedFresh: true, fictional: true});
  const kept = byId(desk.monitors, 'route:mac:demo-claude');
  assert.equal(desk.active, 'route:mac:demo-claude');
  assert.equal(kept.word, 'DISCONNECTED');
  assert.equal(kept.retained, true);
  assert.notEqual(kept.word, 'STALE');
  assert.notEqual(kept.word, 'UNKNOWN');
  assert.equal(desk.view('route:mac:demo-claude').boardQuery, 'kept');
  assert.equal(desk.dismiss('route:mac:demo-claude'), true);
  assert.equal(desk.active, 'all');
  assert.equal(desk.monitors.some(item => item.id === 'route:mac:demo-claude'), false);
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

test('workload ids stay put when the displayed model changes', () => {
  const desk = createMonitorDesk();
  desk.note(demoSnapshot(), {feedFresh: true, fictional: true});
  const before = byId(desk.monitors, 'job:demo-pc-job');
  assert.equal(before.workload.id, 'job:demo-pc-job');
  assert.equal(before.workload.requestedModel, 'example-20b');
  assert.equal(before.workload.servedModel, 'example-20b-served');
  assert.notEqual(before.workload.requestedModel, before.workload.servedModel);
  const renamed = demoSnapshot();
  renamed.windowsJobs.inFlight[0].requestedModel = 'example-20b-renamed';
  renamed.windowsJobs.inFlight[0].model = 'example-20b-renamed';
  desk.note(renamed, {feedFresh: true, fictional: true});
  const after = byId(desk.monitors, 'job:demo-pc-job');
  assert.equal(after.workload.id, before.workload.id);
  assert.notEqual(after.title, before.title);
  assert.match(after.title, /example-20b-renamed/);
});

test('an unselected tab keeps updating and a switch does not tick the subscription', () => {
  const desk = createMonitorDesk();
  desk.note(demoSnapshot(), {feedFresh: true, fictional: true});
  assert.equal(desk.select('client:claude'), true);
  const ticks = desk.subscriptionTicks;
  desk.select('machine:pc');
  assert.equal(desk.subscriptionTicks, ticks);
  const next = demoSnapshot();
  next.windowsJobs.inFlight[0].ageSeconds = 11;
  next.pipeline.pipelines.find(row => row.runId === 'demo-claude').ageSeconds = 9;
  desk.note(next, {feedFresh: true, fictional: true});
  assert.equal(desk.active, 'machine:pc');
  assert.equal(desk.subscriptionTicks, ticks + 1);
  assert.equal(byId(desk.monitors, 'client:claude').word, 'RUNNING');
  assert.equal(byId(desk.monitors, 'job:demo-pc-job').metrics.find(item => item.label === 'Age seconds').value, '11');
  assert.equal(desk.view('client:claude').boardQuery, '');
});

test('reconnect does not double-count and a new attempt drops prior events and analysis', () => {
  const desk = createMonitorDesk();
  const snap = demoSnapshot();
  snap.pipeline.pipelines.find(row => row.runId === 'demo-claude').attemptId = '1';
  desk.note(snap, {feedFresh: true, fictional: true});
  const first = byId(desk.monitors, 'route:mac:demo-claude');
  assert.equal(first.events.length, 1);
  assert.equal(desk.attachAnalysis('route:mac:demo-claude', {tool: 'nisi', phase: 'pre', state: 'passed', attemptId: '1', detail: 'baseline recorded', provenance: 'fixture nisi demo, modelCalls 0'}), 'RUNNING');
  assert.equal(byId(desk.monitors, 'route:mac:demo-claude').word, 'RUNNING');
  assert.equal(jevGate(byId(desk.monitors, 'route:mac:demo-claude').analyses).open, false);
  assert.equal(byId(desk.monitors, 'route:mac:demo-claude').analyses.find(item => item.tool === 'nisi' && item.phase === 'pre').state, 'passed');
  assert.equal(byId(desk.monitors, 'route:mac:demo-claude').analyses.find(item => item.tool === 'jev' && item.phase === 'post').state, 'unavailable');
  desk.note(snap, {feedFresh: true, fictional: true});
  assert.equal(byId(desk.monitors, 'route:mac:demo-claude').events.length, 1);
  assert.equal(desk.attachAnalysis('route:mac:demo-claude', {tool: 'jev', phase: 'post', state: 'passed', attemptId: '2'}), false);
  const retry = demoSnapshot();
  retry.pipeline.pipelines.find(row => row.runId === 'demo-claude').attemptId = '2';
  desk.note(retry, {feedFresh: true, fictional: true});
  const second = byId(desk.monitors, 'route:mac:demo-claude');
  assert.equal(second.workload.attemptId, '2');
  assert.equal(second.events.length, 1);
  assert.equal(second.events[0].attemptId, '2');
  assert.equal(second.analyses.find(item => item.tool === 'nisi' && item.phase === 'pre').state, 'unavailable');
  assert.equal(second.word, 'RUNNING');
});

test('2026-11-06 Mac and PC receipts keep the Jev gate closed', () => {
  const receipts = JSON.parse(read('./docs/data/jaryo-jev-phase-receipts.json'));
  assert.equal(receipts.pass_claimed, false);
  assert.equal(receipts.machines.mac.task_id, 'jaryo-liveval-20261106-022202');
  assert.equal(receipts.machines.pc.task_id, 'jaryo-liveval-20261106-022221');
  for (const phase of ['pre', 'post']) {
    assert.equal(receipts.machines.mac[phase].result, 'BLOCKED_NOT_RUN');
    assert.equal(receipts.machines.pc[phase].result, 'UNAVAILABLE_NOT_RUN');
  }
  const mac = receipts.machines.mac;
  const macSlots = analysisSlots(['pre', 'post'].map(phase => ({
    tool: 'jev', phase, state: 'blocked', provenance: mac.task_id, attemptId: '1',
    detail: `${mac[phase].result}: ${mac[phase].reason}`,
  })), {runId: mac.task_id, attemptId: '1'});
  const macGate = jevGate(macSlots);
  assert.equal(macGate.open, false);
  assert.equal(macGate.pre, 'blocked');
  assert.equal(macGate.post, 'blocked');
  const snapshot = {
    schemaVersion: 1, host: 'mac',
    models: [{id: 'lfm3-1.7b', host: 'mac', loaded: true, state: 'idle', ageSeconds: 1}],
    components: [mac.monitor_refresh.nisi, mac.monitor_refresh.jev],
    pipeline: {pipelines: [], pipelinesComplete: true, steps: []},
    clients: [],
  };
  const monitors = deriveMonitors(snapshot, {feedFresh: true});
  assert.equal(byId(monitors, 'machine:mac').word, 'IDLE');
  assert.equal(byId(monitors, 'route:nisi').metrics.find(item => item.label === 'Nisi').value, 'needs-action');
  assert.equal(byId(monitors, 'route:nisi').metrics.find(item => item.label === 'Jev').value, 'configured');
  assert.notEqual(byId(monitors, 'route:nisi').word, 'RUNNING');
  assert.equal(monitors.some(item => item.kind === 'session'), false);
  const pc = receipts.machines.pc;
  const pcSlots = ['pre', 'post'].map(phase => ({
    tool: 'jev', phase, state: 'unavailable', evidenced: true, provenance: pc.task_id,
    detail: `${pc[phase].result}: ${pc[phase].reason}`,
  }));
  assert.equal(jevGate(pcSlots).open, false);
  assert.equal(pc.monitor_health.service, 'agiw-win-observer');
  assert.equal(pc.monitor_health.pid, 2456);
});

test('authorized 0329 completions and STATUS OK do not open the Jev gate', () => {
  const macId = 'jaryo-auth0329-mac-20261106-033030';
  const pcId = 'jaryo-auth0329-pc-20261106-033057';
  const mac = jevGate([
    {tool: 'jev', phase: 'pre', state: 'recorded', evidenced: true, provenance: macId, verdict: 'NOT_PASS', detail: 'COMPLETED exit 0, 3/5 flags. Flags are checks, not a PASS.'},
    {tool: 'jev', phase: 'post', state: 'recorded', evidenced: true, provenance: macId, verdict: 'NOT_PASS', detail: 'COMPLETED exit 0, 1/4 flags. statedVerdict none.'},
  ]);
  assert.equal(mac.open, false);
  assert.equal(mac.preVerdict, 'NOT_PASS');
  assert.equal(mac.postVerdict, 'NOT_PASS');
  const pc = jevGate([
    {tool: 'jev', phase: 'pre', state: 'recorded', evidenced: true, provenance: pcId, verdict: 'PROCEED-WITH-CHANGES', detail: 'STATUS OK is engine success, not verdict PASS.'},
    {tool: 'jev', phase: 'post', state: 'recorded', evidenced: true, provenance: pcId, verdict: 'ROLLBACK', detail: 'STATUS OK. No observed acceptance passes. Advisory only; no repo edits to undo.'},
  ]);
  assert.equal(pc.open, false);
  assert.equal(pc.postVerdict, 'ROLLBACK');
  const synthetic = jevGate([
    {tool: 'jev', phase: 'pre', state: 'passed', evidenced: true, provenance: 'unit fixture, not a machine receipt', verdict: 'PASS'},
    {tool: 'jev', phase: 'post', state: 'passed', evidenced: true, provenance: 'exit 0', verdict: 'STATUS OK'},
  ]);
  assert.equal(synthetic.open, false);
  const idleMasked = deriveMonitors({
    schemaVersion: 1, host: 'mac',
    models: [{id: 'lfm3-1.7b', host: 'mac', loaded: true, state: 'idle', ageSeconds: 1}],
    components: [{id: 'nisi', state: 'needs-action', detail: 'Current Nisi installation binding is pending, invalid, stale or unavailable'}, {id: 'jev', state: 'configured', detail: 'Opted in; no archived Jev judgment yet'}],
    pipeline: {status: 'idle', pipelines: [], pipelinesComplete: true, steps: []},
    clients: [],
  }, {feedFresh: true});
  assert.equal(byId(idleMasked, 'route:nisi').word, 'BLOCKED');
  assert.equal(byId(idleMasked, 'machine:mac').word, 'IDLE');
  assert.equal(jevGate(byId(idleMasked, 'route:nisi').analyses).open, false);
});

test('unavailable Jev pre or post does not open the analysis gate', () => {
  const desk = createMonitorDesk();
  const snap = demoSnapshot();
  snap.pipeline.pipelines.find(row => row.runId === 'demo-claude').attemptId = '1';
  snap.analyses = [
    {workloadId: 'route:mac:demo-claude', tool: 'jev', phase: 'pre', state: 'unavailable', attemptId: '1', detail: 'helper storage unavailable'},
    {workloadId: 'route:mac:demo-claude', tool: 'jev', phase: 'post', state: 'unavailable', attemptId: '1', detail: 'hosted probe not run'},
  ];
  desk.note(snap, {feedFresh: true, fictional: true});
  const session = byId(desk.monitors, 'route:mac:demo-claude');
  assert.equal(session.word, 'RUNNING');
  const gate = jevGate(session.analyses);
  assert.equal(gate.open, false);
  assert.equal(gate.pre, 'unavailable');
  assert.equal(gate.post, 'unavailable');
  assert.equal(desk.attachAnalysis('route:mac:demo-claude', {tool: 'jev', phase: 'pre', state: 'passed', attemptId: '1', detail: 'no source'}), false);
  assert.equal(desk.attachAnalysis('route:mac:demo-claude', {tool: 'jev', phase: 'pre', state: 'passed', attemptId: '1', provenance: 'machine receipt not in this checkout', detail: 'claimed pass'}), 'RUNNING');
  assert.equal(jevGate(byId(desk.monitors, 'route:mac:demo-claude').analyses).open, false);
  const cleared = demoSnapshot();
  cleared.pipeline.pipelines.find(row => row.runId === 'demo-claude').attemptId = '1';
  cleared.analyses = [{workloadId: 'route:mac:demo-claude', tool: 'jev', phase: 'pre', state: 'unavailable', attemptId: '1', provenance: 'status read', detail: 'unavailable replaces a pass'}];
  desk.note(cleared, {feedFresh: true, fictional: true});
  assert.equal(byId(desk.monitors, 'route:mac:demo-claude').analyses.find(item => item.tool === 'jev' && item.phase === 'pre').state, 'unavailable');
  assert.equal(jevGate(byId(desk.monitors, 'route:mac:demo-claude').analyses).open, false);
});

test('unknown, stale, and disconnected stay different words', () => {
  assert.equal(healthPresentation({health: 'not_measured'}).word, 'UNKNOWN');
  assert.equal(healthPresentation({health: 'not_measured', reason: 'stale'}).word, 'STALE');
  assert.equal(healthPresentation({health: 'not_measured', reason: 'tunnel_down'}).word, 'DISCONNECTED');
  assert.notEqual(healthPresentation({health: 'not_measured'}).glyph, healthPresentation({health: 'not_measured', reason: 'stale'}).glyph);
  const incomplete = demoSnapshot();
  incomplete.pipeline.pipelinesComplete = false;
  const running = byId(deriveMonitors(incomplete, {feedFresh: true, fictional: true}), 'all').metrics.find(item => item.label === 'Running');
  assert.match(running.value, /^at least /);
});

test('a Windows snapshot keeps Mac presence, lane models, and queue rows distinct', () => {
  const snapshot = {
    schemaVersion: 1,
    host: 'windows',
    macPeer: {state: 'linked', via: 'sharedchami', configuredName: 'studio', verified: false, loadedCount: 1,
      models: [{id: 'example-mac', state: 'loaded'}],
      detail: 'Reported through SharedChami; unsigned share file, not verified'},
    windowsWorker: {state: 'advertised', ageSeconds: 0, lanes: {
      fast: {up: true, model: 'expected-fast', servedModel: 'served-fast', slotsBusy: 0, slotsTotal: 2},
      deep: {up: false, model: 'expected-deep', servedModel: null, slotsBusy: 0, slotsTotal: 1},
    }},
    windowsJobs: {liveAvailable: false, complete: true, inFlight: [], recent: [{id: 'old-run', model: 'served-fast', state: 'success'}]},
    pipeline: {status: 'queued', pipelines: [], pipelinesComplete: false, queue: {pending: 2, claimed: 0}},
    clients: [],
  };
  const monitors = deriveMonitors(snapshot, {feedFresh: true});
  const mac = byId(monitors, 'machine:mac');
  assert.equal(mac.title, 'Mac peer');
  assert.equal(mac.word, 'RECORDED');
  assert.notEqual(mac.word, 'RUNNING');
  assert.equal(mac.metrics.find(item => item.label === 'Verified').value, 'no');
  const pc = byId(monitors, 'machine:pc');
  assert.equal(pc.metrics.find(item => item.label === 'fast requested').value, 'expected-fast');
  assert.equal(pc.metrics.find(item => item.label === 'fast served').value, 'served-fast');
  assert.equal(pc.metrics.find(item => item.label === 'deep served').value, '—');
  assert.equal(monitors.some(item => item.id === 'job:old-run'), false);
  assert.equal(byId(monitors, 'route:nisi').pipeline.incomplete, true);
  const stale = deriveMonitors({...snapshot, macPeer: {...snapshot.macPeer, state: 'unknown', reason: 'stale', detail: 'Mac report is stale'}}, {feedFresh: true});
  assert.equal(byId(stale, 'machine:mac').word, 'STALE');
  const absent = deriveMonitors({...snapshot, macPeer: {...snapshot.macPeer, state: 'unknown', reason: 'no-report', detail: 'No presence'}}, {feedFresh: true});
  assert.equal(byId(absent, 'machine:mac').word, 'UNAVAILABLE');
});

test('inserting a tab does not steal focus; removing the focused tab moves to the active tab', () => {
  const ids = ['all', 'machine:mac', 'client:claude'];
  assert.equal(nextTabFocus('client:claude', [...ids, 'job:new'], 'client:claude'), 'client:claude');
  assert.equal(nextTabFocus(null, [...ids, 'job:new'], 'all'), null);
  assert.equal(nextTabFocus('route:gone', ids, 'all'), 'all');
});

test('the same run id on Mac and PC stays two sessions, and a late attempt does not inherit the other', () => {
  const snap = demoSnapshot();
  const macRow = snap.pipeline.pipelines.find(row => row.runId === 'demo-claude');
  macRow.attemptId = '1';
  snap.pipeline.pipelines.push({...macRow, host: 'windows', attemptId: '1'});
  snap.analyses = [{
    workloadId: 'route:mac:demo-claude', tool: 'jev', phase: 'post', state: 'recorded',
    attemptId: '1', provenance: 'fixture', verdict: 'NOT_PASS', detail: 'Mac attempt 1 only',
  }];
  const monitors = deriveMonitors(snap, {feedFresh: true, fictional: true});
  const mac = byId(monitors, 'route:mac:demo-claude');
  const pc = byId(monitors, 'route:windows:demo-claude');
  assert.notEqual(mac.id, pc.id);
  assert.match(mac.title, /^Mac ·/);
  assert.match(pc.title, /^Windows PC ·/);
  assert.equal(mac.analyses.find(item => item.tool === 'jev' && item.phase === 'post').detail, 'Mac attempt 1 only');
  assert.equal(pc.analyses.find(item => item.tool === 'jev' && item.phase === 'post').state, 'unavailable');
  const later = demoSnapshot();
  const moved = later.pipeline.pipelines.find(row => row.runId === 'demo-claude');
  moved.attemptId = '2';
  later.analyses = snap.analyses;
  const next = deriveMonitors(later, {feedFresh: true, fictional: true});
  assert.equal(byId(next, 'route:mac:demo-claude').workload.attemptId, '2');
  assert.equal(byId(next, 'route:mac:demo-claude').analyses.find(item => item.phase === 'post').state, 'unavailable');
  assert.equal(jevGate(byId(next, 'route:mac:demo-claude').analyses).open, false);
});

test('a disconnected session returns on the same id without a duplicate sample', () => {
  const desk = createMonitorDesk();
  const snap = demoSnapshot();
  desk.note(snap, {feedFresh: true, fictional: true});
  desk.select('route:mac:demo-claude');
  const runningEvents = byId(desk.monitors, 'route:mac:demo-claude').events.length;
  const gone = demoSnapshot();
  gone.pipeline.pipelines = gone.pipeline.pipelines.filter(row => row.runId !== 'demo-claude');
  desk.note(gone, {feedFresh: true, fictional: true});
  assert.equal(byId(desk.monitors, 'route:mac:demo-claude').word, 'DISCONNECTED');
  desk.note(snap, {feedFresh: true, fictional: true});
  const back = byId(desk.monitors, 'route:mac:demo-claude');
  assert.equal(back.word, 'RUNNING');
  assert.equal(back.retained, false);
  assert.equal(back.events.length, runningEvents);
  const ticks = desk.subscriptionTicks;
  const pcEvents = byId(desk.monitors, 'job:demo-pc-job').events.length;
  desk.select('machine:pc');
  assert.equal(desk.subscriptionTicks, ticks);
  assert.equal(byId(desk.monitors, 'route:mac:demo-claude').word, 'RUNNING');
  assert.equal(byId(desk.monitors, 'job:demo-pc-job').events.length, pcEvents);
  desk.note(snap, {feedFresh: true, fictional: true});
  assert.equal(byId(desk.monitors, 'route:mac:demo-claude').events.length, runningEvents);
});

test('the demo fixture is labeled fictional and the painter does not inject markup', () => {
  const demo = demoSnapshot();
  assert.equal(demo.fictional, true);
  assert.equal(demo.label, DEMO_BANNER);
  assert.match(DEMO_BANNER, /fictional/i);
  const board = read('./web/monitor-board.mjs');
  assert.equal(board.includes('innerHTML'), false);
  assert.match(board, /does not pause, cancel, restart, or starve inference/);
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

test('liveness, readiness, and attachment stay separate and acceptance stays blocked', () => {
  const empty = deriveMonitors(null, {feedFresh: true})[0];
  assert.equal(empty.evidence.rows.find(row => row.id === 'process').matrix, 'Untested');
  assert.equal(empty.evidence.rows.find(row => row.id === 'lane').word, 'NOT MEASURED');
  assert.equal(empty.evidence.badges.attachment, 'NO RUN');
  assert.equal(empty.evidence.rows.find(row => row.id === 'acceptance').matrix, 'Blocked');
  assert.equal(empty.evidence.rows.some(row => row.id === 'acceptance' && row.matrix === 'Observed'), false);

  const demo = byId(deriveMonitors(demoSnapshot(), {feedFresh: true, fictional: true}), 'all');
  assert.equal(demo.evidence.badges.process, 'RUNNING');
  assert.equal(demo.evidence.badges.lane, 'NOT READY');
  assert.match(demo.evidence.rows.find(row => row.id === 'lane').detail, /not readiness/);
  assert.equal(demo.evidence.rows.find(row => row.id === 'acceptance').matrix, 'Blocked');
  assert.match(demo.evidence.rows.find(row => row.id === 'acceptance').detail, /Windows review is not a pass/);
  assert.match(demo.evidence.rows.find(row => row.id === 'acceptance').detail, /one flag of four/);
  assert.equal(demo.evidence.rows.find(row => row.id === 'acceptance').ledger.some(row => row.verdict === 'PASS'), false);
  assert.equal(demo.evidence.rows.find(row => row.id === 'acceptance').ledger.find(row => row.receipt === 'astra1103-mac-DESK-20261106-114103').verdict, 'not acceptance');
  assert.equal(demo.evidence.rows.find(row => row.id === 'acceptance').ledger.find(row => row.receipt === 'mac-unsigned-build-20261106-115509').verdict, '0/4');
  assert.match(demo.evidence.rows.find(row => row.id === 'acceptance').ledger.find(row => row.receipt === 'mac-unsigned-build-20261106-115509').next, /317aad2/);
  assert.match(demo.evidence.rows.find(row => row.id === 'acceptance').ledger.find(row => row.receipt === 'astra1103-pc-WIN-20261106-114930').next, /WiX/);
  assert.equal(demo.evidence.rows.find(row => row.id === 'acceptance').ledger.find(row => row.gate === 'Fixture tests').next, 'Receipt verification pending');

  const unknownSample = {
    schemaVersion: 1, host: 'mac',
    models: [],
    clients: [{id: 'claude', label: 'Claude', model: 'claude-example', models: [{id: 'claude-example'}]}],
    pipeline: {status: 'idle', pipelines: [], pipelinesComplete: true, steps: []},
    components: [{id: 'nisi', state: 'ready', detail: 'No unresolved route.'}],
  };
  assert.equal(byId(deriveMonitors(unknownSample, {feedFresh: true}), 'all').word, 'UNKNOWN');
  const quiet = {
    schemaVersion: 1, host: 'mac',
    models: [{id: 'local', host: 'mac', loaded: false, state: 'idle', ageSeconds: 1}],
    clients: [],
    pipeline: {status: 'idle', pipelines: [], pipelinesComplete: true, steps: []},
    components: [{id: 'nisi', state: 'ready', detail: 'No unresolved route.'}],
  };
  assert.equal(byId(deriveMonitors(quiet, {feedFresh: true}), 'all').word, 'IDLE');
  assert.equal(healthPresentation({health: 'not_measured', modelId: 'listed-model', reason: 'not_measured'}).word, 'UNKNOWN');
  assert.equal(healthPresentation({health: 'not_measured', reason: 'tunnel_down', ageSeconds: 12}).word, 'DISCONNECTED');
  assert.match(healthPresentation({reason: 'tunnel_down', ageSeconds: 12}).reason, /12s/);

  const mismatch = demoSnapshot();
  mismatch.windowsWorker.lanes.fast.servedModel = 'other-model';
  mismatch.windowsWorker.lanes.fast.activityKnown = true;
  assert.equal(byId(deriveMonitors(mismatch, {feedFresh: true, fictional: true}), 'machine:pc').evidence.badges.lane, 'MISMATCH');

  const blocked = demoSnapshot();
  blocked.pipeline.status = 'idle';
  blocked.pipeline.pipelines = [];
  blocked.windowsJobs = {inFlight: [], liveAvailable: false};
  blocked.components = [{id: 'nisi', state: 'needs-action', detail: 'Install evidence INVALID.'}];
  const nisi = byId(deriveMonitors(blocked, {feedFresh: true, fictional: true}), 'route:nisi');
  assert.equal(nisi.word, 'BLOCKED');
  assert.equal(nisi.evidence.rows.find(row => row.id === 'nisi').word, 'NEEDS ACTION');
  assert.equal(nisi.evidence.rows.find(row => row.id === 'nisi').matrix, 'Blocked');
  assert.notEqual(nisi.word, 'RUNNING');

  const map = mapEvidence({nodes: [{id: 'mac:lmstudio', modelId: 'example-local', health: 'ok'}]}, {fictional: false});
  assert.equal(map.badges.process, 'SAMPLE');
  assert.equal(map.badges.lane, 'INVENTORY');
  assert.equal(map.badges.attachment, 'NO RUN');
  const down = mapEvidence(null, {feedUnavailable: true});
  assert.equal(down.badges.process, 'DISCONNECTED');
  assert.notEqual(down.badges.process, 'UNKNOWN');
  assert.notEqual(down.badges.process, 'IDLE');
  const missing = mapEvidence(null, {feedUnavailable: false});
  assert.equal(missing.badges.process, 'UNKNOWN');
  assert.notEqual(missing.badges.process, 'DISCONNECTED');
  assert.equal(map.rows.find(row => row.id === 'acceptance').matrix, 'Blocked');
  assert.equal(evidenceFor(null, {id: 'x', kind: 'machine', word: 'UNKNOWN', analyses: []}).rows.find(row => row.id === 'acceptance').matrix, 'Blocked');
});

test('components names an unavailable observer instead of staying on Checking', () => {
  const source = read('./web/components.js');
  const block = source.slice(source.indexOf('}catch{'), source.indexOf('}catch{') + 420);
  assert.match(block, /integrity/);
  assert.match(block, /runtime/);
  assert.match(block, /Unknown · observer unavailable/);
  assert.doesNotMatch(block, /Checking/);
});

test('provisional identity is unlabeled as a choice and the board no longer covers the map', () => {
  const tokens = read('./jaryo/tokens.json');
  assert.match(tokens, /"appliedChoiceFile": null/);
  assert.match(tokens, /"chosenFamilyIds": \{\}/);
  assert.match(tokens, /"logoSelection": null/);
  assert.match(tokens, /"selectedDirection": null/);
  assert.match(tokens, /"rejectedDirection": "d4-split"/);
  assert.match(tokens, /"provisionalTypeface": "Inter"/);
  assert.match(tokens, /"artworkApproval": null/);
  assert.match(tokens, /"surface": "black-white"/);
  assert.doesNotMatch(tokens, /Downloads|curated-85|F17\.01/);
  const page = read('./web/identity.html');
  assert.match(page, /TYPE ONLY/);
  assert.match(page, /D4 Split is rejected/);
  assert.match(page, /Inter/);
  const css = read('./web/jaryo.css');
  const marks = ['d1-stamp', 'd2-ledger', 'd3-schematic', 'd4-split', 'd5-signal'].map(name => read(`./jaryo/marks/${name}.svg`));
  assert.equal(new Set(marks).size, marks.length);
  for (const mark of marks) assert.match(mark, /currentColor/);
  const developed = read('./jaryo/marks/d4-split.svg');
  assert.notEqual(developed, read('./jaryo/marks/d4-split-before.svg'));
  assert.match(developed, /fill-rule="evenodd"/);
  assert.match(read('./jaryo/marks/d4-split-wordmark.svg'), /AGIW wordmark/);
  assert.match(read('./jaryo/marks/favicon.svg'), /#000000/);
  assert.match(read('./jaryo/marks/favicon.svg'), /#ffffff/);
  assert.match(read('./jaryo/marks/d4-split-on-light.svg'), /#000000/);
  assert.match(read('./jaryo/marks/d4-split-on-dark.svg'), /#ffffff/);
  assert.doesNotMatch(css, /d4-split\.svg/);
  assert.doesNotMatch(css, /d1-stamp\.svg/);
  assert.match(css, /font-family: "Jost"/);
  assert.match(css, /font-family: "Newsreader"/);
  assert.match(read('./web/index.html'), /AGIW Suite/);
  assert.doesNotMatch(read('./web/index.html'), /D4 direction/);
  assert.match(css, /grid-column: 1/);
  assert.match(css, /flex-direction: column/);
  assert.match(css, /#monitorBoard\.j-board \{\s*position: relative;/);
  assert.doesNotMatch(css, /\.j-board \{\s*position: absolute/);
  assert.equal(resolveTheme('system', true).resolved, 'light');
  assert.equal(resolveTheme('system', false).resolved, 'dark');
  assert.equal(resolveTheme('light', false).resolved, 'light');
  assert.equal(resolveTheme('graphite', true).mode, 'system');
  assert.match(read('./web/index.html'), /value="system"/);
  assert.doesNotMatch(read('./web/index.html'), /value="graphite"/);
  assert.match(read('./web/index.html'), /href="\/identity"/);
  assert.match(read('./web/monitor.html'), /href="\/identity"/);
  assert.match(read('./web/components.html'), /href="\/identity"/);
});

test('a PC snapshot shows the Mac monitor whether the Mac row says mac or macos', () => {
  for (const platform of ['mac', 'macos']) {
    const monitors = deriveMonitors({schemaVersion: 1, host: 'windows', machines: [
      {id: 'pc', platform: 'windows', state: 'up'}, {id: 'mac', platform, host: 'peer', state: 'unknown'}]}, {feedFresh: true});
    assert.ok(monitors.some(item => item.id === 'machine:mac'), platform);
  }
  const none = deriveMonitors({schemaVersion: 1, host: 'windows', machines: [{id: 'pc', platform: 'windows', state: 'up'}]}, {feedFresh: true});
  assert.equal(none.some(item => item.id === 'machine:mac'), false);
});

test('a PC lane node that is not configured says NOT CONFIGURED, and a configured PC with its tunnel down still says DISCONNECTED', () => {
  const none = healthPresentation({health: 'not_measured', reason: 'not_configured', machine: 'pc', id: 'pc:router', label: 'Router'});
  assert.equal(none.word, 'NOT CONFIGURED');
  // Only a PC node says "No PC": the Mac's Veritas reviewer and a node with no identity must not.
  assert.equal(healthPresentation({health: 'not_measured', reason: 'not_configured', machine: 'mac', id: 'mac:veritas', label: 'Veritas reviewer'}).reason, 'Veritas reviewer is not configured');
  assert.equal(healthPresentation({health: 'not_measured', reason: 'not_configured'}).reason, 'Not configured');
  assert.equal(healthPresentation({health: 'not_measured', reason: 'not_configured', id: 'pc:qwen3.8-27b'}).reason, 'No PC is configured');
  assert.equal(none.status, 'unavailable', 'styled like the other unavailable states');
  assert.equal(none.reason, 'No PC is configured');
  assert.equal(healthPresentation({health: 'not_measured', reason: 'tunnel_down', ageSeconds: 4}).word, 'DISCONNECTED');
  assert.equal(healthPresentation({health: 'not_measured', reason: 'not_configured'}, {feedUnavailable: true}).word, 'DISCONNECTED', 'no feed at all outranks it');
  assert.equal(healthPresentation({health: 'down', reason: 'not_configured'}).word, 'DOWN', 'a measured health outranks a reason');
  assert.equal(healthPresentation({health: 'not_measured', reason: 'not_configured'}).glyph, '○');
  // The Map's own node word (monitor.mjs statusWord) follows the same order: measured health, stale, not configured, disconnected.
  const map = read('./web/monitor.mjs');
  assert.ok(map.indexOf("node.reason === 'not_configured') return 'NOT CONFIGURED'") > map.indexOf("node.reason === 'stale') return 'STALE'"));
  assert.ok(map.indexOf("node.reason === 'not_configured') return 'NOT CONFIGURED'") < map.indexOf("node.reason === 'feed_unavailable'"));
});
