import assert from 'node:assert/strict';
import test from 'node:test';
import { onlineCodeModeView } from './web/online-code-mode.mjs';

const processing = {
  state: 'processing', active: true, blinking: true,
  client: 'opencode', chatId: 'chat-42', routeId: 'route-7',
  evidence: 'Matched live router pointer and process', observedAt: '2026-09-23T10:00:00Z',
};

test('fresh bound route is labeled processing and blinks with direct attribution', () => {
  const view = onlineCodeModeView(processing, { feedFresh: true });
  assert.equal(view.state, 'processing');
  assert.equal(view.blinking, true);
  assert.equal(view.client, 'opencode');
  assert.equal(view.chatId, 'chat-42');
  assert.equal(view.taskLabel, 'Routed task processing');
  assert.match(view.label, /opencode · chat-42/);
});

test('inactive wire state means an idle task and an unchecked setup', () => {
  const view = onlineCodeModeView({ ...processing, state: 'inactive', active: false,
    taskState: 'idle', setupState: 'absent' }, { feedFresh: true });
  assert.equal(view.state, 'inactive');
  assert.equal(view.blinking, false);
  assert.equal(view.label, 'Task idle · setup not checked recently');
  assert.equal(view.taskLabel, 'Idle; no active route');
  assert.equal(view.setupLabel, 'Not checked recently');
});

test('fresh setup receipt leaves task idle and carries no invented route or client binding', () => {
  const view = onlineCodeModeView({
    state: 'ready', active: false, blinking: false,
    client: null, chatId: null, routeId: null,
    taskState: 'idle', setupState: 'fresh', setupAgeSeconds: 12,
    evidence: 'Recent capability inventory passed',
    recentSessions: [{client: 'codex', chatId: 'not-bound'}],
  }, {feedFresh: true});
  assert.equal(view.state, 'ready');
  assert.equal(view.label, 'Task idle · setup checked 12s ago');
  assert.equal(view.setupLabel, 'Checked 12s ago');
  assert.equal(view.blinking, false);
  assert.equal(view.client, null);
  assert.equal(view.chatId, null);
  assert.equal(view.routeId, null);
});

test('stale feed, recorded/checkpoint-only, and unrecognized evidence stay unknown and never blink', () => {
  for (const [mode, options] of [
    [processing, { feedFresh: false }],
    [{ ...processing, state: 'checkpoint', source: 'recorded' }, { feedFresh: true }],
    [null, { feedFresh: true }],
  ]) {
    const view = onlineCodeModeView(mode, options);
    assert.equal(view.state, 'unknown');
    assert.equal(view.blinking, false);
    assert.equal(view.client, null);
    assert.equal(view.chatId, null);
    assert.equal(view.label, 'Online Code Mode · Unknown');
  }
});

test('pause and reduced motion suppress animation while preserving reliable active state', () => {
  for (const options of [{ feedFresh: true, paused: true }, { feedFresh: true, reducedMotion: true }]) {
    const view = onlineCodeModeView(processing, options);
    assert.equal(view.state, 'processing');
    assert.equal(view.blinking, false);
  }
});

test('missing attribution remains explicitly unknown and never comes from unrelated session fields', () => {
  const view = onlineCodeModeView({ ...processing, client: null, chatId: null, recentSessions: [{ client: 'claude', chatId: 'old' }] }, { feedFresh: true });
  assert.equal(view.client, null);
  assert.equal(view.chatId, null);
  assert.equal(view.label, 'Routed task processing');
  assert.equal(view.label.includes('claude'), false);
});

test('processing without explicit active evidence is not treated as active or animated', () => {
  const view = onlineCodeModeView({ ...processing, active: null }, { feedFresh: true });
  assert.equal(view.state, 'unknown');
  assert.equal(view.blinking, false);
  assert.equal(view.client, null);
  assert.equal(view.chatId, null);
  assert.equal(view.label, 'Online Code Mode · Unknown');
  const inactiveFlag = onlineCodeModeView({ ...processing, active: false }, { feedFresh: true });
  assert.equal(inactiveFlag.state, 'unknown');
  assert.equal(inactiveFlag.client, null);
  assert.equal(inactiveFlag.chatId, null);
});

test('a valid recorded route without its owner is unfinished, never processing', () => {
  const view = onlineCodeModeView({
    state: 'unknown', active: null, blinking: false, taskState: 'unfinished',
    setupState: 'expired', setupAgeSeconds: 700, routeId: 'route-7',
    evidence: 'Matching live owner process unverified', observedAt: 'now',
  }, {feedFresh: true});
  assert.equal(view.state, 'unfinished');
  assert.equal(view.label, 'Unfinished router task');
  assert.equal(view.setupLabel, 'Receipt expired; readiness unverified');
  assert.equal(view.routeId, 'route-7');
  assert.equal(view.blinking, false);
  assert.match(view.evidence, /owner process unverified/);
});

test('expired or invalid setup receipt remains unknown even when the route is idle', () => {
  for (const setupState of ['expired', 'invalid']) {
    const view = onlineCodeModeView({state:'unknown', active:null, taskState:'idle', setupState,
      evidence:'Readiness unverified'}, {feedFresh:true});
    assert.equal(view.state, 'unknown');
    assert.equal(view.taskLabel, 'Idle; no active route');
    assert.equal(view.blinking, false);
    assert.equal(view.evidence, 'Readiness unverified');
  }
});

test('backend idle wire shape keeps expired and invalid setup visible in the glance label', () => {
  for (const setupState of ['expired', 'invalid']) {
    const view = onlineCodeModeView({
      state:'inactive', active:false, blinking:false, taskState:'idle', setupState,
      setupAgeSeconds:700, routeId:null, evidence:'Initialized router journal has no active route',
    }, {feedFresh:true});
    assert.equal(view.state,'inactive');
    assert.match(view.label,new RegExp(setupState));
    assert.equal(view.taskLabel,'Idle; no active route');
    assert.match(view.setupLabel,new RegExp(setupState==='expired'?'expired':'invalid'));
  }
});

// Router concurrency P2 (spec 6.13): several routed tasks, their queue, the lanes and the policy.
import { routeRunRows, routeQueueRows, routeLanesView, routeAdmissionText } from './web/online-code-mode.mjs';

const several = {
  ...processing, routeId: 'run-a', runCounts: { running: 2, queued: 1, unresolved: 0 },
  activeRuns: [{ runId: 'run-a', state: 'running', stage: 'backend_draft', host: 'mac', client: 'claude', live: true },
    { runId: 'run-b', state: 'running', stage: 'backend_review', host: 'windows', client: 'codex', live: true },
    { runId: '../bad', state: 'running' }, { runId: 'run-c', state: 'invented', client: 'someone', host: 'moon', stage: 'bad stage!' }],
  queuedRuns: [{ runId: 'run-q', phase: 'waiting', resource: 'pc-route', secondsLeft: 90, client: 'opencode' },
    { runId: 'run-r', phase: 'admitting', resource: 'nowhere', secondsLeft: -3 }],
  lanes: { 'mac-pair': { capacity: 1, holders: ['run-a', '../x'], waiting: [] }, 'pc-route': { holders: ['run-b'], waiting: ['run-q'] } },
};

test('two or more routed tasks read "Routed tasks processing · N running · M queued"; one keeps today\'s copy', () => {
  const view = onlineCodeModeView(several, { feedFresh: true });
  assert.equal(view.state, 'processing');
  assert.equal(view.label, 'Routed tasks processing · 2 running · 1 queued');
  assert.equal(view.taskLabel, 'Routed tasks processing · 2 running · 1 queued');
  assert.deepEqual(view.runCounts, { running: 2, queued: 1, unresolved: 0 });
  const one = onlineCodeModeView({ ...several, runCounts: { running: 1, queued: 0, unresolved: 3 } }, { feedFresh: true });
  assert.equal(one.label, 'Routed task processing · opencode · chat-42');
  assert.equal(onlineCodeModeView({ ...processing, runCounts: { running: 1, queued: 0 } }, { feedFresh: true }).label,
    'Routed task processing · opencode · chat-42');
});

test('run and queue rows are re-checked: malformed ids are dropped, unknown values stay unknown', () => {
  const view = onlineCodeModeView(several, { feedFresh: true });
  assert.deepEqual(view.runRows.map(r => r.runId), ['run-a', 'run-b', 'run-c']);
  assert.deepEqual(view.runRows[2], { runId: 'run-c', state: 'unknown', stateLabel: 'State unknown', client: null, host: null, stage: null, live: false });
  assert.deepEqual(routeQueueRows(several, { snapshotAge: 10 }), [
    { runId: 'run-q', resource: 'pc-route', phase: 'waiting', waitingFor: 'waiting for PC route', secondsLeft: 80, client: 'opencode' },
    { runId: 'run-r', resource: null, phase: 'admitting', waitingFor: 'being admitted', secondsLeft: null, client: null }]);
  const lanes = routeLanesView(several);
  assert.equal(lanes.label, 'Mac pair 1 · PC route 1 · PC deep 1 · PC fast 2');
  assert.equal(lanes.text, 'Mac pair 1/1 (run-a) · PC route 1/1 (run-b), 1 waiting · PC deep 0/1 · PC fast 0/2');
  assert.deepEqual(routeRunRows(null), []);
  // A stale feed shows no rows at all.
  const stale = onlineCodeModeView(several, { feedFresh: false });
  assert.deepEqual([stale.runRows, stale.queueRows, stale.runCounts], [[], [], { running: 0, queued: 0, unresolved: 0 }]);
});

test('an install in progress is its own state and never blinks; admission reads in plain words', () => {
  const view = onlineCodeModeView({ ...processing, taskState: 'install-in-progress' }, { feedFresh: true });
  assert.deepEqual([view.state, view.label, view.taskLabel, view.blinking], ['installing', 'Router install in progress', 'Router install in progress', false]);
  assert.equal(routeAdmissionText({ admission: { source: 'legacy-router' } }), 'Single-run router (one route at a time)');
  assert.equal(routeAdmissionText({ admission: { policy: 'single', drainState: 'draining', sharedHolders: ['run-a', 'run-b'] } }), 'Single · draining 2 run(s)');
  assert.equal(routeAdmissionText({ admission: { policy: 'single', drainState: 'drained' } }), 'Single');
  assert.equal(routeAdmissionText({ admission: { source: 'invalid', policy: null } }), 'Policy file invalid; the router admits no new run');
  assert.equal(routeAdmissionText({}), 'Unknown');
});

test('converge: a refusing install fence and a run changing under observation read in plain words', () => {
  assert.equal(routeAdmissionText({ admission: { source: 'fence-missing', policy: null } }), 'Install fence missing; the router refuses every command');
  assert.equal(routeAdmissionText({ admission: { source: 'fence-invalid', policy: null } }), 'Install fence invalid; the router refuses every command');
  assert.deepEqual(routeRunRows({ activeRuns: [{ runId: 'legacy-a', state: 'changing', live: false }] }).map(r => [r.state, r.stateLabel, r.live]),
    [['changing', 'Changing; checked again next sample', false]]);
});

test('terms: every run state has one name in the panel and the inspector; multi admission matches the lane capacities', async () => {
  const { readFile } = await import('node:fs/promises');
  const app = await readFile(new URL('./web/app.js', import.meta.url), 'utf8');
  const body = /ROUTE_STATUS_WORDS=\{([^}]*)\}/.exec(app)?.[1];
  assert.ok(body, 'app.js ROUTE_STATUS_WORDS not found');
  const words = Object.fromEntries([...body.matchAll(/(?:'([^']+)'|([A-Za-z]+)):'([^']*)'/g)].map(m => [m[1] || m[2], m[3]]));
  const states = ['running', 'waiting', 'admitting', 'unresolved', 'archived-uncleared', 'unverified', 'unreadable', 'changing'];
  assert.deepEqual(Object.keys(words).sort(), [...states].sort());
  for (const state of states) {
    assert.equal(routeRunRows({ activeRuns: [{ runId: 'run-t', state }] })[0].stateLabel, words[state], state);
  }
  // PC fast holds 2 at once, so "one per lane" was wrong; the words point at the capacities instead.
  const multi = routeAdmissionText({ admission: { policy: 'multi', source: 'file', drainState: 'not-applicable' } });
  assert.equal(multi, 'Multi: routes run at once, up to each lane’s capacity');
  assert.ok(routeLanesView({}).rows.some(row => row.capacity > 1));
});

// p2-readers converge (2026-09-27): each review finding reproduced, fixed and pinned.
test('converge: a queued-only primary reads "queued" and never blinks; a running one still does', () => {
  const queued = { ...processing, client: 'codex', chatId: null, taskState: 'queued', blinking: false,
    runCounts: { running: 0, queued: 1, unresolved: 0 } };
  const view = onlineCodeModeView(queued, { feedFresh: true });
  assert.deepEqual([view.state, view.queuedOnly, view.blinking, view.label, view.taskLabel],
    ['processing', true, false, 'Routed task queued · codex', 'Routed task queued']);
  // Even a snapshot that says blinking (an older server) never blinks while nothing runs.
  const older = onlineCodeModeView({ ...queued, taskState: 'processing', blinking: true }, { feedFresh: true });
  assert.deepEqual([older.queuedOnly, older.blinking, older.taskLabel], [true, false, 'Routed task queued']);
  const two = onlineCodeModeView({ ...queued, runCounts: { running: 0, queued: 2, unresolved: 0 } }, { feedFresh: true });
  assert.equal(two.label, 'Routed tasks queued · 0 running · 2 queued');
  const running = onlineCodeModeView({ ...processing, runCounts: { running: 1, queued: 1, unresolved: 0 } }, { feedFresh: true });
  assert.deepEqual([running.queuedOnly, running.blinking, running.label], [false, true, 'Routed tasks processing · 1 running · 1 queued']);
});

test('converge (Sol N8): only waiting and admitting rows are queue rows; a malformed phase is dropped, never "waiting"', () => {
  const rows = routeQueueRows({ queuedRuns: [{ runId: 'run-a', phase: 'running', resource: 'mac-pair' },
    { runId: 'run-b', phase: 'bogus' }, { runId: 'run-c' }, { runId: 'run-d', phase: 'waiting', resource: 'pc-route' },
    { runId: 'run-e', phase: 'admitting' }] });
  assert.deepEqual(rows.map(r => [r.runId, r.phase, r.waitingFor]),
    [['run-d', 'waiting', 'waiting for PC route'], ['run-e', 'admitting', 'being admitted']]);
});

test('converge: a held run lock beside an unreadable record, and a run ID in both layouts, say so; multi names the caller override', () => {
  const rows = routeRunRows({ activeRuns: [{ runId: 'run-u', state: 'unreadable', lockHeld: true, live: false },
    { runId: 'run-v', state: 'unverified', lockHeld: true }, { runId: 'run-w', state: 'unresolved', lockHeld: true },
    { runId: 'run-x', state: 'running', live: true, collision: true }] });
  assert.deepEqual(rows.map(r => r.stateLabel), ['Record unreadable (run lock held)', 'Run lock unverified (run lock held)',
    'Unresolved', 'Running · run ID in both journal layouts']);
  assert.equal(routeAdmissionText({ admission: { policy: 'multi', source: 'file', callerOverride: 'not-observable' } }),
    'Multi: routes run at once, up to each lane’s capacity (a caller with CODEMODE_ROUTER_CONCURRENCY=single runs alone)');
  assert.equal(routeAdmissionText({ admission: { policy: 'single', drainState: 'drained', callerOverride: null } }), 'Single');
});
