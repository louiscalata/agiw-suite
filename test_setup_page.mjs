import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./web/setup.mjs', import.meta.url), 'utf8')
  .replace("'/monitor-store.mjs'", JSON.stringify(new URL('./web/monitor-store.mjs', import.meta.url).href));
const { setupSteps, setupProgress, setupNote, paintSetup, mountSetup: mountStoredSetup } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
// The transport itself is exercised in test_live_stream; these fixtures drive the consumer's store boundary.
function setupStore({fetch, setTimeout, now = () => Date.now() / 1000}) {
  let state = {snapshot: null, snapshotStatus: {fresh: false, available: false}}, listener, stopped = false;
  const tick = async () => {
    try {
      const reply = await fetch('/api/snapshot', {cache: 'no-store'});
      const snapshot = reply.ok ? await reply.json() : null;
      const stamp = snapshot?.fullSampledAt ?? snapshot?.sampledAt, age = now() - stamp;
      state = {snapshot, snapshotStatus: {available: Boolean(snapshot), fresh: Number.isFinite(stamp) && age >= -1 && age <= 3}};
    } catch (_) { state = {...state, snapshotStatus: {fresh: false, available: false}}; }
    if (!stopped) { listener?.(state); setTimeout(tick, 1000); }
  };
  return {getState: () => state, subscribe(fn) { listener = fn; fn(state); return () => { listener = null; }; },
    start: tick, destroy() { stopped = true; }};
}
function mountSetup(options) {
  const store = options.store || setupStore(options);
  const mounted = mountStoredSetup({...options, store});
  return {destroy() { mounted.destroy(); store.destroy?.(); }};
}

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const byId = steps => Object.fromEntries(steps.map(s => [s.id, s]));

const snapshot = {
  host: 'mac', activityKnown: true,
  sources: [{ id: 'lmstudio-api', state: 'live' }, { id: 'lms-ps', state: 'live' }],
  models: [{ id: 'a', loaded: true }, { id: 'b', loaded: false }],
  machines: [{ host: 'mac', platform: 'mac', state: 'up' }, { host: 'pc', platform: 'windows', state: 'down' }],
};
const components = { nisi: { version: '0.2.0', integrity: 'verified', entry: '/Users/someone/secret/nisi.mjs' }, node: { found: true, path: '/usr/local/bin/node' }, check: { state: 'not-run' } };
const jev = { configured: false, state: 'unavailable', message: 'Secure Jev credential storage is unavailable.' };

test('a working Mac shows what is done and what is next', () => {
  const s = byId(setupSteps({ snapshot, fresh: true, components, jev }));
  assert.equal(s.monitor.status, 'DONE');
  assert.equal(s.lmstudio.status, 'DONE');
  assert.equal(s.activity.status, 'DONE');
  assert.equal(s.model.status, 'DONE');
  assert.equal(s.model.evidence, '1 model loaded of 2 installed.');
  assert.equal(s.nisi.evidence, 'Nisi 0.2.0 package integrity verified.');
  assert.equal(s.node.status, 'DONE');
  assert.equal(s.selfcheck.status, 'NOT YET');
  assert.equal(s.selfcheck.link, '/components');
  assert.equal(s.jev.status, 'OPTIONAL');
  assert.equal(s.machines.evidence, '1 other machine known, none up.');
  assert.deepEqual(setupProgress(Object.values(s)), { done: 6, total: 7 });
});

test('no paths from the replies are shown', () => {
  const all = JSON.stringify(setupSteps({ snapshot, fresh: true, components, jev }));
  assert.doesNotMatch(all, /\/Users\/|\/usr\/local/);
});

test('without a fresh sample the runtime steps are unknown, not failed', () => {
  const s = byId(setupSteps({ snapshot, fresh: false, components, jev }));
  assert.equal(s.monitor.status, 'NOT YET');
  for (const id of ['lmstudio', 'activity', 'model']) assert.equal(s[id].status, 'UNKNOWN', id);
  const none = setupSteps({ snapshot: null, fresh: false, components: null, jev: null });
  assert.deepEqual(none.map(step => [step.id, step.status]), [['monitor', 'UNKNOWN']], 'with no snapshot the edition is not known, so only the monitor is listed');
  assert.deepEqual(setupProgress(none), { done: 0, total: 1 });
});

test('each missing piece says what the person does next', () => {
  const bare = { host: 'mac', activityKnown: false, sources: [{ id: 'lmstudio-api', state: 'error' }], models: [] };
  const s = byId(setupSteps({ snapshot: bare, fresh: true, components: { nisi: { integrity: 'mismatch' }, node: { found: false }, check: { state: 'failed', message: 'Node is too old.' } }, jev }));
  assert.match(s.lmstudio.next, /start the API server on 127\.0\.0\.1:1234/);
  assert.equal(s.activity.status, 'NOT YET');
  assert.equal(s.model.status, 'UNKNOWN');
  assert.equal(s.nisi.status, 'NOT YET');
  assert.match(s.node.next, /Node\.js 22/);
  assert.equal(s.selfcheck.status, 'FAILED');
  assert.equal(s.selfcheck.evidence, 'Node is too old.');
  const live = byId(setupSteps({ snapshot: { ...snapshot, models: [] }, fresh: true, components, jev }));
  assert.match(live.model.next, /Download a model/);
});

test('optional steps never count toward the required total', () => {
  const s = setupSteps({ snapshot: { ...snapshot, machines: [{ host: 'mac', state: 'up' }, { host: 'pc', platform: 'windows', state: 'up' }] }, fresh: true, components: { ...components, check: { state: 'passed' } }, jev: { configured: true } });
  assert.deepEqual(setupProgress(s), { done: 7, total: 7 });
  assert.equal(byId(s).machines.status, 'DONE');
  assert.equal(byId(s).jev.status, 'DONE');
});

test('the page only reads: every request is a GET to a known path', async () => {
  const calls = [];
  const fetch = async (path, options = {}) => { calls.push([path, options.method || 'GET']); return { ok: true, json: async () => ({}) }; };
  const document = { getElementById: () => null };
  let next = null;
  const handle = mountSetup({ document, fetch, setTimeout: fn => { next = fn; return 1; } });
  await new Promise(r => setImmediate(r));
  for (let i = 0; i < 11 && next; i++) { const fn = next; next = null; await fn(); }
  handle.destroy();
  assert.ok(calls.length >= 3);
  for (const [path, method] of calls) {
    assert.equal(method, 'GET');
    assert.ok(['/api/snapshot', '/api/components', '/api/components/jev'].includes(path), path);
  }
});

test('every full-web page links Setup and both servers serve it', () => {
  for (const page of ['index', 'monitor', 'components', 'identity', 'machines', 'setup']) assert.match(read(`./web/${page}.html`), /<a href="\/setup"( aria-current="page")?>Setup<\/a>/);
  assert.match(read('./server.py'), /'\/setup': \('setup\.html'/);
  assert.match(read('./windows/agiw_win/server.py'), /"\/setup\.mjs": \("setup\.mjs"/);
  assert.doesNotMatch(source, /method:\s*'POST'|\/check'/);
});

test('a Node.js the self-check cannot use is not shown as installed', () => {
  const s = byId(setupSteps({ snapshot, fresh: true, components: { ...components, check: { state: 'needs-node', message: 'Install Node.js 22 or newer.' } }, jev }));
  assert.equal(s.node.status, 'NOT YET');
  assert.match(s.node.next, /Node\.js 22 or newer/);
  assert.equal(s.selfcheck.status, 'NOT YET');
  assert.match(s.selfcheck.evidence, /needs a usable Node\.js/);
  assert.equal(byId(setupSteps({ snapshot, fresh: true, components, jev })).node.status, 'DONE');
});

// What the PC sends today (hyphenated reason, 'macos') and what the backend is moving to (a superset with the Mac's fields).
const pcOld = { id: 'pc', name: 'PC', platform: 'windows', label: 'This PC', reason: 'serving', state: 'up', trust: 'local', loadedCount: 1 };
const pcNew = { ...pcOld, host: 'louisaurorar12', reasonCode: 'observed', freshForSeconds: 30, modelsComplete: true, busyCount: null, promptTokensPerSecond: null, generationTokensPerSecond: null };
const macFromPc = { id: 'mac', name: 'Mac', platform: 'macos', label: 'Mac', reason: 'unconfigured', state: 'unknown' };
const windowsSnapshot = machines => ({ host: 'windows', sampledAt: 1, machines });

test('the Windows edition lists its own steps and never the Mac app ones', () => {
  for (const row of [pcOld, pcNew]) {
    const steps = setupSteps({ snapshot: windowsSnapshot([row, macFromPc]), fresh: true, components: null, jev: null });
    assert.deepEqual(steps.map(s => s.id), ['monitor', 'lanes', 'machines']);
    assert.equal(byId(steps).monitor.status, 'DONE');
    assert.equal(byId(steps).lanes.status, 'DONE');
    assert.deepEqual(setupProgress(steps), { done: 2, total: 2 });
    const all = JSON.stringify(steps);
    assert.doesNotMatch(all, /LM Studio|Nisi|Node\.js|Keychain|lms/);
  }
});

test('the PC lane step reads reasonCode, or the older hyphenated reason, and never guesses', () => {
  const lane = (row, fresh = true) => byId(setupSteps({ snapshot: windowsSnapshot([row]), fresh, components: null, jev: null })).lanes;
  assert.equal(lane({ ...pcOld, reason: 'partly-serving' }).status, 'NOT YET');
  assert.match(lane({ ...pcOld, reason: 'partly-serving' }).evidence, /not all/);
  assert.equal(lane({ ...pcOld, reason: 'no-lane-serving', state: 'down' }).status, 'NOT YET');
  assert.match(lane({ ...pcOld, reason: 'no-lane-serving', state: 'down' }).next, /Start a model lane/);
  for (const reason of ['lane-state-unknown', 'unconfigured', 'config-unreadable', 'stale', 'reported-service-unknown', undefined, 'x<y']) {
    assert.equal(lane({ ...pcOld, reason }).status, 'UNKNOWN', String(reason));
  }
  assert.equal(lane({ ...pcNew, reasonCode: 'reported_degraded' }).status, 'NOT YET');
  assert.equal(lane({ ...pcNew, reasonCode: 'reported_down', state: 'down' }).status, 'NOT YET');
  assert.equal(lane({ ...pcNew, reasonCode: 'not_observed' }).status, 'UNKNOWN');
  assert.equal(lane({ ...pcNew, reasonCode: 'observed', reason: 'no-lane-serving' }).status, 'DONE', 'the shared word wins over the old one');
  assert.equal(lane(pcNew, false).status, 'UNKNOWN');
  assert.match(lane(pcNew, false).evidence, /fresh sample/);
  assert.equal(byId(setupSteps({ snapshot: windowsSnapshot([macFromPc]), fresh: true })).lanes.status, 'UNKNOWN');
  assert.equal(byId(setupSteps({ snapshot: windowsSnapshot([]), fresh: true })).lanes.status, 'UNKNOWN');
  assert.equal(byId(setupSteps({ snapshot: windowsSnapshot('x'), fresh: true })).lanes.status, 'UNKNOWN');
});

test('the Windows note shows only on the Windows edition', () => {
  assert.match(setupNote({ host: 'windows' }), /Windows edition.*Mac app/);
  assert.equal(setupNote(snapshot), null);
  assert.equal(setupNote(null), null);
});

test('freshness follows the contract: fullSampledAt (else sampledAt) within 3 seconds', async () => {
  const run = async (body, now) => {
    const els = {};
    const make = () => ({ children: [], className: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, append(...c) { this.children.push(...c); },
      replaceChildren(...c) { this.children = c; }, textContent: '', hidden: false });
    const document = { getElementById: id => (els[id] ||= make()), createElement: () => make() };
    let next = null;
    const handle = mountSetup({ document, fetch: async path => ({ ok: path === '/api/snapshot', json: async () => body }), setTimeout: fn => { next = fn; return 1; }, now: () => now });
    for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
    handle.destroy();
    return els.setupSteps.children[0].className;
  };
  const rowOf = body => run(body, 1000);
  assert.match(await rowOf({ host: 'mac', sampledAt: 999, fullSampledAt: 999 }), /j-setup-done/);
  assert.match(await rowOf({ host: 'mac', sampledAt: 999 }), /j-setup-done/, 'sampledAt alone still counts when there is no fullSampledAt');
  assert.match(await rowOf({ host: 'mac', sampledAt: 999, fullSampledAt: 990 }), /j-setup-not-yet/, 'a stale full sample is not saved by a fresh overlay');
  assert.match(await rowOf({ host: 'mac', sampledAt: 996 }), /j-setup-not-yet/, '4 seconds is outside the window');
  assert.match(await rowOf({ host: 'mac', sampledAt: 1001 }), /j-setup-done/, '1 second ahead is clock skew inside the window');
  assert.match(await rowOf({ host: 'mac', sampledAt: 1005 }), /j-setup-not-yet/, '5 seconds ahead is not');
});

test('a lane word counts only with the state that goes with it, and a bad word is never rescued by the old one', () => {
  const lane = row => byId(setupSteps({ snapshot: windowsSnapshot([row]), fresh: true })).lanes;
  for (const [row, expected] of [
    [{ ...pcNew, reasonCode: 'observed', state: 'down' }, 'UNKNOWN'], [{ ...pcNew, reasonCode: 'observed', state: 'unknown' }, 'UNKNOWN'],
    [{ ...pcNew, reasonCode: 'reported_degraded', state: 'down' }, 'UNKNOWN'], [{ ...pcNew, reasonCode: 'reported_down', state: 'up' }, 'UNKNOWN'],
    [{ ...pcNew, reasonCode: 'observed', state: 'up' }, 'DONE'], [{ ...pcNew, reasonCode: 'reported_degraded', state: 'up' }, 'NOT YET'],
    [{ ...pcNew, reasonCode: 'reported_down', state: 'down' }, 'NOT YET'],
    // present but unusable reasonCode: unknown, even though the old reason alone would have said serving
    [{ ...pcOld, reasonCode: 'Bad word!' }, 'UNKNOWN'], [{ ...pcOld, reasonCode: 5 }, 'UNKNOWN'], [{ ...pcOld, reasonCode: '' }, 'UNKNOWN'],
    // absent reasonCode: the old reason is read, but only a string that is one of the known words
    [{ ...pcOld, reasonCode: null }, 'DONE'], [{ ...pcOld, reasonCode: undefined }, 'DONE'],
    [{ ...pcOld, reason: ['serving'] }, 'UNKNOWN'], [{ ...pcOld, reason: { toString: () => 'serving' } }, 'UNKNOWN'], [{ ...pcOld, reason: 'constructor' }, 'UNKNOWN'],
    [{ ...pcOld, reason: '__proto__' }, 'UNKNOWN'], [{ ...pcOld, reason: 'toString' }, 'UNKNOWN'],
  ]) assert.equal(lane(row).status, expected, JSON.stringify(row));
});

test('an unknown lane state is given the remedy that fits why it is unknown', () => {
  const lane = reasonCode => byId(setupSteps({ snapshot: windowsSnapshot([{ ...pcNew, state: 'unknown', reasonCode }]), fresh: true })).lanes;
  assert.match(lane('not_observed').next, /lane-processes\.json/);
  assert.match(lane('unavailable').next, /lane-processes\.json/);
  assert.match(lane('stale').next, /Wait for a fresh sample/);
  assert.doesNotMatch(lane('stale').next, /lane-processes/);
  assert.match(lane('clock_skew').next, /PC clock/);
  assert.equal(lane('timeout').next, null, 'no remedy is invented for a reason with no known fix');
  assert.equal(lane(undefined).next, null);
});

test('the Windows monitor step says what to do when the sample is old, without naming a Start menu entry the package does not create', () => {
  const stale = byId(setupSteps({ snapshot: windowsSnapshot([pcNew]), fresh: false })).monitor;
  assert.equal(stale.status, 'NOT YET');
  assert.match(stale.next, /AGIW Monitor\.vbs/);
  assert.doesNotMatch(JSON.stringify(setupSteps({ snapshot: windowsSnapshot([pcNew]), fresh: false })), /Start menu/);
  assert.equal(byId(setupSteps({ snapshot: windowsSnapshot([pcNew]), fresh: undefined })).monitor.status, 'NOT YET');
});

test('this machine is the first row that names the host; a second PC or Mac is another machine', () => {
  const other = { ...pcNew, id: 'otherpc', host: 'otherpc', state: 'up' };
  assert.equal(byId(setupSteps({ snapshot: windowsSnapshot([pcNew, macFromPc, other]), fresh: true })).machines.status, 'DONE');
  assert.match(byId(setupSteps({ snapshot: windowsSnapshot([pcNew, macFromPc, other]), fresh: true })).machines.evidence, /^1 other machine is up/);
  assert.equal(byId(setupSteps({ snapshot: windowsSnapshot([pcNew, other]), fresh: true })).machines.status, 'DONE');
  assert.equal(byId(setupSteps({ snapshot: windowsSnapshot([pcNew]), fresh: true })).machines.evidence, 'No other machine in this sample.');
  assert.equal(byId(setupSteps({ snapshot: windowsSnapshot([macFromPc, pcNew]), fresh: true })).lanes.status, 'DONE', 'the PC row is found whichever position it is in');
  const twoMacs = { ...snapshot, machines: [{ host: 'mac', platform: 'mac', state: 'up' }, { host: 'mac2', platform: 'mac', state: 'up' }] };
  assert.equal(byId(setupSteps({ snapshot: twoMacs, fresh: true, components, jev })).machines.status, 'DONE');
});

test('the Windows note is painted visibly, and hidden again on the Mac', () => {
  const html = read('./web/setup.html');
  assert.match(html, /<p id="setupNote" hidden><\/p>/);
  assert.doesNotMatch(html.match(/<p id="setupNote"[^>]*>/)[0], /j-hint/, '.j-hint is visually hidden by jaryo.css');
  const els = {};
  const make = () => ({ children: [], className: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; }, textContent: '', hidden: true });
  const document = { getElementById: id => (els[id] ||= make()), createElement: () => make() };
  paintSetup(document, { snapshot: { host: 'windows', machines: [pcNew] }, fresh: true });
  assert.equal(els.setupNote.hidden, false);
  assert.match(els.setupNote.textContent, /Windows edition/);
  paintSetup(document, { snapshot, fresh: true, components, jev });
  assert.equal(els.setupNote.hidden, true);
  assert.equal(els.setupNote.textContent, '');
});

test('only a Mac snapshot makes the page ask for the Mac app routes, and only every ten seconds', async () => {
  const run = async bodies => {
    const calls = [];
    const els = {};
    const make = () => ({ children: [], className: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; }, textContent: '', hidden: false });
    const document = { getElementById: id => (els[id] ||= make()), createElement: () => make() };
    let i = 0, next = null;
    const fetch = async path => { calls.push(path); return path === '/api/snapshot' ? { ok: true, json: async () => bodies[Math.min(i++, bodies.length - 1)] } : { ok: true, json: async () => ({}) }; };
    const handle = mountSetup({ document, fetch, setTimeout: fn => { next = fn; return 1; }, now: () => 1000 });
    for (let n = 0; n < 4; n++) { await new Promise(r => setImmediate(r)); if (next) { const fn = next; next = null; await fn(); } }
    handle.destroy();
    return calls.filter(path => path !== '/api/snapshot');
  };
  assert.deepEqual(await run([{ host: 'windows', sampledAt: 999, machines: [pcNew] }]), [], 'the Windows edition answers 501 to these');
  assert.deepEqual(await run([null]), [], 'no snapshot, so not known to be a Mac');
  assert.deepEqual(await run([null, null, { host: 'mac', sampledAt: 999 }]), ['/api/components', '/api/components/jev'], 'asked once the host is known to be a Mac');
  assert.equal((await run([{ host: 'mac', sampledAt: 999 }])).length, 2, 'once, not on every one of several polls');
});

test('a timestamp that is not a number never makes the setup page live', async () => {
  const run = async stamp => {
    const els = {};
    const make = () => ({ children: [], className: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; }, textContent: '', hidden: false });
    const document = { getElementById: id => (els[id] ||= make()), createElement: () => make() };
    const handle = mountSetup({ document, fetch: async path => ({ ok: path === '/api/snapshot', json: async () => ({ host: 'mac', ...stamp }) }), setTimeout: () => 1, now: () => 1000 });
    for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
    handle.destroy();
    return els.setupSteps.children[0].className;
  };
  for (const stamp of ['999', [999], true, null, {}, '', NaN, Infinity]) {
    assert.match(await run({ sampledAt: stamp }), /j-setup-not-yet/, JSON.stringify(stamp));
    if (stamp !== null) assert.match(await run({ fullSampledAt: stamp, sampledAt: 999 }), /j-setup-not-yet/, `full ${JSON.stringify(stamp)}`);
  }
});


test('a pending component read cannot stop store ageing and its deadline permits a later read', async () => {
  const els = {}, timers = [];
  const make = () => ({children: [], className: '', setAttribute() {}, append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; }, textContent: '', hidden: false});
  const document = {getElementById: id => (els[id] ||= make()), createElement: make};
  let now = 1000, listener, reads = 0;
  let state = {snapshot: {...snapshot, sampledAt: 1000}, snapshotStatus: {available: true, fresh: true}};
  const store = {getState: () => state, subscribe(fn) { listener = fn; fn(state); return () => {}; }, start() {}};
  const handle = mountStoredSetup({document, store, now: () => now,
    fetch: () => { reads++; return reads <= 2 ? new Promise(() => {}) : Promise.resolve({ok: true, json: async () => ({})}); },
    setTimeout(fn, ms) { timers.push({fn, ms}); return timers.length; }, clearTimeout() {}});
  assert.match(els.setupSteps.children[0].className, /j-setup-done/);
  now += 4;
  state = {...state, snapshotStatus: {available: true, fresh: false}};
  listener(state);
  assert.match(els.setupSteps.children[0].className, /j-setup-not-yet/, 'ageing does not wait for components');
  assert.equal(reads, 2, 'component refreshes are serialized');
  for (const timer of timers) { assert.equal(timer.ms, 3000); timer.fn(); }
  for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
  now += 6;
  listener(state);
  for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads, 4, 'a deadline releases stalled reads even when fetch ignores abort');
  handle.destroy();
});


test('expired legacy machine states cannot claim up or absence', () => {
  for (const state of ['up', 'down', 'unknown']) {
    const machine = byId(setupSteps({snapshot: {...snapshot, machines: [{host: 'mac', state: 'up'}, {host: 'pc', state}]}, fresh: false})).machines;
    assert.equal(machine.status, 'UNKNOWN');
    assert.match(machine.evidence, /stale or unavailable/);
    assert.doesNotMatch(machine.evidence, /are up|is up|none up/);
  }
});

test('setup uses independently aged canonical endpoint facts rather than an enclosing sample clock', async () => {
  const {projectEvidence} = await import('./web/monitor-store.mjs');
  const at = second => new Date(second * 1000).toISOString();
  const facts = [
    {id: 'mac', scope: 'machine', state: 'reported', observedAt: at(100), expiresAt: at(103), evidence: {trust: 'local'}},
    {id: 'pc', scope: 'machine', state: 'unknown', observedAt: null, expiresAt: null},
    {id: 'pc:fast', parentId: 'pc', scope: 'lane', state: 'endpoint answering', observedAt: at(100), expiresAt: at(102)},
    {id: 'pc:slow', parentId: 'pc', scope: 'lane', state: 'endpoint answering', observedAt: at(100), expiresAt: at(106)},
  ];
  const input = {host: 'mac', sampledAt: 100, machines: [{host: 'pc', state: 'up'}], fleetState: {schemaVersion: 1, facts}};
  const partial = byId(setupSteps({snapshot: projectEvidence(input, 104), fresh: false})).machines;
  assert.equal(partial.status, 'DONE');
  assert.match(partial.evidence, /1 of 2 configured identity endpoints answering/);
  assert.match(partial.evidence, /Host health is not measured/);
  const expired = byId(setupSteps({snapshot: projectEvidence(input, 107), fresh: false})).machines;
  assert.equal(expired.status, 'UNKNOWN');
  assert.match(expired.evidence, /stale/);
});
