import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import {createMonitorStore, feedAge, validSnapshot, streamBackoff} from './web/monitor-store.mjs';

const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const iso = at => new Date(at * 1000).toISOString();
const fact = (id, at, life) => ({id, label: id, scope: 'lane', parentId: 'pc',
  observedAt: iso(at), expiresAt: iso(at + life), state: 'endpoint answering',
  completeness: 'complete', source: 'registry', evidence: {modelId: id}});
const snap = (at, extra = {}) => ({schemaVersion: 1, sampledAt: at, models: [], sources: [], ...extra});
const map = (at, extra = {}) => ({observedAt: iso(at), nodes: [], edges: [], ...extra});
const reply = (body, status = 200) => ({ok: status === 200, status, json: async () => body});

function harness({eventSource = true, ...options} = {}) {
  let time = 100, id = 0;
  const timers = new Map(), sources = [], requests = [], updates = [];
  class Source {
    constructor(url) { this.url = url; this.listeners = {}; this.closed = false; sources.push(this); }
    addEventListener(type, listener) { this.listeners[type] = listener; }
    emit(type, data) { this.listeners[type]?.(type === 'snapshot' ? {data: typeof data === 'string' ? data : JSON.stringify(data)} : {}); }
    close() { this.closed = true; }
  }
  const store = createMonitorStore({now: () => time, EventSource: eventSource ? Source : undefined,
    fetch: (url, init) => new Promise((resolve, reject) => requests.push({url, ...init, resolve, reject})),
    setTimeout: (fn, ms) => { const next = ++id; timers.set(next, {fn, at: time + ms / 1000}); return next; },
    clearTimeout: key => timers.delete(key), ...options});
  store.subscribe(state => updates.push(state));
  const advance = async ms => {
    const until = time + ms / 1000;
    for (;;) {
      const next = [...timers].filter(([, value]) => value.at <= until + 1e-10).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      time = next[1].at; timers.delete(next[0]); next[1].fn(); await flush();
    }
    time = until; await flush();
  };
  return {store, sources, requests, updates, timers, advance, now: () => time};
}

test('sample clocks and schema validation never infer freshness from delivery', () => {
  assert.equal(feedAge({sampledAt: 100, fullSampledAt: 97}, 101), 4);
  assert.equal(feedAge({sampledAt: 100, fullSampledAt: null}, 101), 1);
  assert.ok(Number.isNaN(feedAge({sampledAt: 100, fullSampledAt: 'soon'}, 101)));
  assert.ok(Number.isNaN(feedAge(null, 101)));
  for (const data of [null, snap(NaN), snap(100, {schemaVersion: 2}), snap(100, {models: {}})]) assert.equal(validSnapshot(data), false);
  assert.deepEqual([0, 1, 2, 3, 9, -1, 'x'].map(streamBackoff), [5000, 10000, 20000, 30000, 30000, 5000, 5000]);
});

for (const kind of ['snapshot', 'inference']) {
  test(`a future ${kind} poll is rejected with clock_skew and cannot freeze recovery`, async () => {
    const h = harness({eventSource: false});
    const data = kind === 'snapshot' ? snap : map;
    const send = async at => {
      const pending = h.store.refresh(kind);
      h.requests.at(-1).resolve(reply(data(at))); await pending;
    };
    await send(100);
    await send(3700);
    assert.deepEqual(h.store.getState()[kind], data(100));
    assert.deepEqual(h.store.getState()[`${kind}Status`].error, {stage: `${kind}_clock`,
      code: 'clock_skew', failedAt: 100, secondsAhead: 3600});
    assert.ok(h.store.getState().diagnostics.some(row => row.code === 'clock_skew'));
    await send(101);
    assert.deepEqual(h.store.getState()[kind], data(101));
    assert.equal(h.store.getState()[`${kind}Status`].error, null);
    await send(99);
    assert.deepEqual(h.store.getState()[kind], data(101), 'ordinary older replies remain rejected');
    h.store.destroy();
  });
}

test('a future first stream sample preserves polling until a current sample recovers', async () => {
  const h = harness(); h.store.start({snapshot: true});
  h.sources[0].emit('snapshot', snap(3700));
  assert.equal(h.store.getState().snapshot, null);
  assert.deepEqual(h.store.getState().snapshotStatus.error, {stage: 'snapshot_clock',
    code: 'clock_skew', failedAt: 100, secondsAhead: 3600});
  assert.equal(h.store.getState().transport.streaming, false);
  assert.equal(h.store.getState().transport.polling, true);
  assert.equal(h.requests[0].signal.aborted, false, 'invalid stream must not cancel the bootstrap poll');
  h.requests[0].resolve(reply(snap(100))); await flush();
  assert.equal(h.store.getState().snapshot.sampledAt, 100);
  assert.equal(h.store.getState().snapshotStatus.error, null);
  await h.advance(5000);
  h.sources[1].emit('snapshot', snap(h.now()));
  assert.equal(h.store.getState().transport.streaming, true);
  assert.equal(h.store.getState().snapshot.sampledAt, h.now());
  assert.equal(h.store.getState().snapshotStatus.error, null);
  h.store.destroy();
});

test('stream permits at most two seconds of future clock tolerance', () => {
  const h = harness(); h.store.start({snapshot: true});
  h.sources[0].emit('snapshot', snap(102));
  assert.equal(h.store.getState().snapshot.sampledAt, 102);
  assert.equal(h.store.getState().snapshotStatus.ageSeconds, -2);
  assert.equal(h.store.getState().snapshotStatus.fresh, true);
  h.sources[0].emit('snapshot', snap(102.001));
  assert.equal(h.store.getState().snapshot.sampledAt, 102);
  assert.equal(h.store.getState().snapshotStatus.error.code, 'clock_skew');
  h.store.destroy();
});

for (const kind of ['snapshot', 'inference']) {
  test(`${kind} accepts and marks fresh the same two-second future boundary`, async () => {
    const h = harness({eventSource: false});
    const data = kind === 'snapshot' ? snap : map;
    const send = async at => {
      const pending = h.store.refresh(kind);
      h.requests.at(-1).resolve(reply(data(at))); await pending;
    };
    await send(101.5);
    assert.equal(h.store.getState()[`${kind}Status`].fresh, true);
    await send(102);
    assert.equal(h.store.getState()[`${kind}Status`].ageSeconds, -2);
    assert.equal(h.store.getState()[`${kind}Status`].fresh, true);
    await send(102.001);
    assert.deepEqual(h.store.getState()[kind], data(102));
    const error = h.store.getState()[`${kind}Status`].error;
    assert.equal(error.stage, `${kind}_clock`);
    assert.equal(error.code, 'clock_skew');
    assert.ok(Math.abs(error.secondsAhead - 2.001) < 1e-9);
    h.store.destroy();
  });

  test(`rejected ${kind} documents cannot replace accepted collector diagnostics`, async () => {
    const h = harness({eventSource: false});
    const data = kind === 'snapshot' ? snap : map;
    const kept = {stage: 'collect_snapshot', code: 'timeout', failedAt: 100};
    const rejected = {stage: 'inference', code: 'rejected', failedAt: 101};
    const send = async (body, status = 200) => {
      const pending = h.store.refresh(kind);
      h.requests.at(-1).resolve(reply(body, status)); await pending;
    };
    await send(data(100, {collectorDiagnostics: [kept]}));
    for (const [body, status] of [
      [data(99, {collectorDiagnostics: [rejected]}), 200],
      [data(3700, {collectorDiagnostics: [rejected]}), 200],
      [data(99), 200],
      [{collectorDiagnostics: [rejected]}, 200],
      [data(101, {collectorDiagnostics: [rejected]}), 503],
    ]) {
      await send(body, status);
      assert.deepEqual(h.store.getState()[kind], data(100, {collectorDiagnostics: [kept]}));
      assert.deepEqual(h.store.getState().diagnostics.filter(row => row.stage === kept.stage), [kept]);
      assert.equal(h.store.getState().diagnostics.some(row => row.code === 'rejected'), false);
    }
    await send(data(101, {collectorDiagnostics: [rejected]}));
    assert.deepEqual(h.store.getState().diagnostics, [rejected]);
    await send(data(102));
    assert.deepEqual(h.store.getState().diagnostics, []);
    h.store.destroy();
  });
}

for (const kind of ['snapshot', 'inference']) {
  test(`a refused first ${kind} reply still reports startup collector diagnostics`, async () => {
    const h = harness({eventSource: false});
    const data = kind === 'snapshot' ? snap : map;
    const started = {stage: kind === 'snapshot' ? 'collect_snapshot' : 'inference',
      code: kind === 'snapshot' ? 'timeout' : 'collector_failed', failedAt: 99};
    const send = async (body, status = 200) => {
      const pending = h.store.refresh(kind);
      h.requests.at(-1).resolve(reply(body, status)); await pending;
    };
    // Before the first sample the servers answer 503 (snapshot) or an error
    // document (inference); that reply is the only carrier of the collector row.
    if (kind === 'snapshot') await send({status: 'starting', collectorDiagnostics: [started]}, 503);
    else await send({nodes: [], error: 'collector_failed', collectorDiagnostics: [started]});
    assert.equal(h.store.getState()[kind], null, 'no sample is invented');
    assert.deepEqual(h.store.getState().diagnostics, [started,
      {stage: `${kind}_request`, code: kind === 'snapshot' ? 'http_503' : 'malformed', failedAt: 100}]);
    await send(data(100));
    assert.deepEqual(h.store.getState()[kind], data(100));
    assert.deepEqual(h.store.getState().diagnostics, [], 'an accepted document without diagnostics clears them');
    await send({status: 'starting', collectorDiagnostics: [started]}, 503);
    assert.deepEqual(h.store.getState().diagnostics.map(row => row.stage), [`${kind}_request`],
      'after acceptance a refused reply no longer supplies collector rows');
    h.store.destroy();
  });
}

test('stream clock skew is bounded and retains accepted collector diagnostics', () => {
  const h = harness(); h.store.start({snapshot: true});
  const kept = {stage: 'collect_snapshot', code: 'timeout', failedAt: 100};
  h.sources[0].emit('snapshot', snap(100, {collectorDiagnostics: [kept]}));
  h.sources[0].emit('snapshot', snap(99, {collectorDiagnostics: []}));
  assert.deepEqual(h.store.getState().diagnostics, [kept]);
  h.sources[0].emit('snapshot', snap(Number.MAX_VALUE, {collectorDiagnostics: []}));
  const error = {stage: 'snapshot_clock', code: 'clock_skew', failedAt: 100, secondsAhead: 86400};
  assert.deepEqual(h.store.getState().snapshotStatus.error, error);
  assert.deepEqual(h.store.getState().diagnostics, [kept, error]);
  assert.equal(h.sources[0].closed, true);
  assert.equal(h.store.getState().transport.polling, true);
  assert.equal(h.store.getState().snapshot.sampledAt, 100);
  h.store.destroy();
});

test('a silent stream closes, starts bounded polling and recovers through retry', async () => {
  const h = harness(); h.store.start({snapshot: true});
  assert.equal(h.sources[0].url, '/api/stream');
  assert.equal(h.requests.length, 1, 'bootstrap polling continues until a valid stream message');
  h.requests[0].resolve(reply(snap(100))); await flush();
  await h.advance(4000);
  assert.equal(h.sources[0].closed, true);
  assert.equal(h.store.getState().snapshotStatus.error.code, 'silent');
  assert.equal(h.requests[0].url, '/api/snapshot');
  h.requests.at(-1).resolve(reply(snap(h.now()))); await flush();
  assert.equal(h.store.getState().snapshotStatus.fresh, true);
  assert.equal(h.store.getState().snapshotStatus.error, null);
  await h.advance(5000);
  assert.equal(h.sources.length, 2);
  assert.equal(h.store.getState().transport.polling, true, 'fallback survives reconnect handshake');
  assert.equal(h.store.getState().transport.connecting, true);
  const pending = h.requests.at(-1); assert.equal(pending.signal.aborted, false);
  h.sources[1].emit('snapshot', snap(h.now()));
  assert.equal(h.store.getState().transport.polling, false);
  assert.equal(pending.signal.aborted, true);
  pending.resolve(reply(snap(999))); await flush();
  assert.equal(h.store.getState().snapshot.sampledAt, h.now(), 'late poll cannot overwrite the stream');
  h.sources[0].emit('snapshot', snap(1000));
  assert.equal(h.store.getState().snapshot.sampledAt, h.now(), 'replaced stream is ignored');
  h.store.destroy(); assert.equal(h.timers.size, 0);
});

for (const invalid of ['{broken', JSON.stringify(snap(100, {models: {}})), JSON.stringify(snap(100, {schemaVersion: 2}))]) {
  test(`malformed stream falls back and retains evidence: ${invalid}`, async () => {
    const h = harness(); h.store.start({snapshot: true});
    h.sources[0].emit('snapshot', snap(100));
    h.sources[0].emit('snapshot', invalid);
    assert.equal(h.sources[0].closed, true);
    assert.equal(h.store.getState().snapshot.sampledAt, 100);
    assert.equal(h.store.getState().snapshotStatus.error.code, 'malformed');
    assert.equal(h.requests.length, 2);
    h.requests[1].resolve(reply(snap(101))); await flush();
    assert.equal(h.store.getState().snapshot.sampledAt, 101);
    assert.equal(h.store.getState().snapshotStatus.error, null);
    h.store.destroy();
  });
}

test('hung fetch and hung JSON obey deadlines even when abort is ignored; refreshes serialize', async () => {
  const h = harness({eventSource: false}); h.store.start({snapshot: true});
  const first = h.store.refresh('snapshot');
  assert.equal(first, h.store.refresh('snapshot'));
  assert.equal(h.requests.length, 1);
  await h.advance(1800); await first;
  assert.equal(h.requests[0].signal.aborted, true);
  assert.equal(h.store.getState().snapshotStatus.error.code, 'deadline');
  await h.advance(1000);
  let releaseJSON;
  h.requests[1].resolve({ok: true, json: () => new Promise(resolve => { releaseJSON = resolve; })}); await flush();
  await h.advance(1800);
  assert.equal(h.requests[1].signal.aborted, true);
  const recovery = h.store.refresh('snapshot');
  h.requests[2].resolve(reply(snap(h.now()))); await recovery;
  const kept = h.store.getState().snapshot.sampledAt;
  releaseJSON(snap(999)); h.requests[0].resolve(reply(snap(1000))); await flush();
  assert.equal(h.store.getState().snapshot.sampledAt, kept);
  assert.equal(h.store.getState().snapshotStatus.error, null);
  h.store.destroy();
});

test('a hung inference request cannot block snapshot recovery or the ageing clock', async () => {
  const h = harness({eventSource: false}); h.store.start({snapshot: true, inference: true});
  assert.deepEqual(h.requests.map(row => row.url), ['/api/snapshot', '/api/inference']);
  h.requests[0].resolve(reply(snap(100))); await flush();
  const before = h.updates.length;
  await h.advance(5000);
  assert.ok(h.updates.length > before + 3);
  assert.equal(h.store.getState().snapshotStatus.fresh, false);
  assert.equal(h.store.getState().snapshotStatus.ageSeconds, 5);
  assert.ok(h.requests.filter(row => row.url === '/api/inference').length >= 2);
  assert.ok(h.requests.filter(row => row.url === '/api/snapshot').length >= 2);
  h.store.destroy();
});

test('fresh envelopes, diagnostic publications and one recovered lane never renew another fact', async () => {
  const h = harness(); h.store.start({snapshot: true, inference: true});
  const facts = [fact('fast', 100, 2), fact('slow', 100, 20)];
  h.sources[0].emit('snapshot', snap(100, {fleetState: {schemaVersion: 1, facts}}));
  h.requests[1].resolve(reply(map(100, {fleetState: {schemaVersion: 1, facts}}))); await flush();
  await h.advance(3000);
  for (const kind of ['snapshot', 'inference']) {
    const rows = h.store.getState()[kind].fleetState.facts;
    assert.equal(rows[0].state, 'stale'); assert.equal(rows[0].ageSeconds, 3);
    assert.equal(rows[1].state, 'endpoint answering');
    assert.equal(rows[0].observedAt, iso(100));
  }
  const diagnostic = {stage: 'collect_snapshot', code: 'io_error', failedAt: 103};
  h.sources[0].emit('snapshot', snap(103, {fullSampledAt: 100, fleetState: {schemaVersion: 1, facts}, collectorDiagnostics: [diagnostic]}));
  assert.deepEqual(h.store.getState().diagnostics, [diagnostic]);
  assert.equal(h.store.getState().snapshot.fleetState.facts[0].ageSeconds, 3);
  h.sources[0].emit('snapshot', snap(103, {fleetState: {schemaVersion: 1, facts: [fact('fast', 103, 2), facts[1]]}}));
  const rows = h.store.getState().snapshot.fleetState.facts;
  assert.equal(rows[0].ageSeconds, 0); assert.equal(rows[1].ageSeconds, 3);
  assert.equal(facts[0].state, 'endpoint answering', 'raw source is not mutated');
  await h.advance(4000);
  assert.equal(h.store.getState().snapshotStatus.fresh, false);
  assert.equal(h.store.getState().snapshot.fleetState.facts[0].state, 'stale');
  h.store.destroy();
});

test('errors and malformed inference retain original data and diagnostics; recovery clears errors', async () => {
  const h = harness();
  const kept = {stage: 'inference', code: 'timeout', failedAt: 99};
  let p = h.store.refresh('inference');
  h.requests[0].resolve(reply(map(100, {collectorDiagnostics: [kept]}))); await p;
  p = h.store.refresh('inference'); h.requests[1].resolve(reply({nodes: [], error: 'collector_failed',
    observedAt: iso(101), collectorDiagnostics: [{stage: 'inference', code: 'collector_failed', failedAt: 101}]})); await p;
  assert.equal(h.store.getState().inference.observedAt, iso(100));
  assert.equal(h.store.getState().inferenceStatus.error.code, 'malformed');
  assert.deepEqual(h.store.getState().diagnostics[0], kept);
  p = h.store.refresh('inference'); h.requests[2].resolve(reply(map(102))); await p;
  assert.equal(h.store.getState().inferenceStatus.error, null);
  assert.deepEqual(h.store.getState().diagnostics, []);
  p = h.store.refresh('inference'); h.requests[3].resolve(reply(map(99))); await p;
  assert.equal(h.store.getState().inference.observedAt, iso(102), 'older response discarded');
  h.store.destroy();
});

test('missing clocks are unknown; a frozen sample delivered repeatedly still goes stale', async () => {
  const h = harness(); h.store.start({snapshot: true});
  for (let i = 0; i < 5; i++) { h.sources[0].emit('snapshot', snap(100)); await h.advance(1000); }
  assert.equal(h.store.getState().snapshotStatus.fresh, false);
  assert.equal(h.store.getState().snapshotStatus.ageSeconds, 5);
  const p = h.store.refresh('inference'); h.requests.at(-1).resolve(reply({nodes: []})); await p;
  assert.equal(h.store.getState().inferenceStatus.ageSeconds, null);
  assert.equal(h.store.getState().inferenceStatus.fresh, false);
  h.store.destroy();
});

test('overview subscribes once, coalesces bursts and keeps a paused capture', () => {
  const source = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  const definitions = source.replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];
  let listener, started = 0, subscriptions = 0;
  const frames = [];
  const api = runInNewContext(`${definitions}\nlet rendered=0;render=()=>{rendered++;};updateFreshness=()=>{};
    ({openStream,pause(value){paused=value;},get state(){return {snapshot,rendered};}})`, {
    document: {}, window: {requestAnimationFrame: callback => frames.push(callback)},
    getMonitorStore: () => ({subscribe(fn) { subscriptions++; listener = fn; return () => {}; }, start() { started++; }}),
  });
  api.openStream(); api.openStream(); assert.equal(subscriptions, 1); assert.equal(started, 1);
  const emit = at => listener({snapshot: snap(at), snapshotStatus: {available: true}, transport: {streaming: true}, diagnostics: []});
  emit(100); emit(101); emit(102); assert.equal(frames.length, 1); frames.shift()();
  assert.equal(api.state.rendered, 1); assert.equal(api.state.snapshot.sampledAt, 102);
  api.pause(true); emit(103); frames.shift()(); assert.equal(api.state.snapshot.sampledAt, 102);
});

test('subscriber exceptions cannot strand initial startup, polling, ageing or healthy consumers', async () => {
  const h = harness({eventSource: false});
  const unsubscribe = h.store.subscribe(() => { throw new Error('view failed'); });
  h.store.start({snapshot: true});
  const pending = h.store.refresh('snapshot');
  h.requests[0].resolve(reply(snap(100))); await pending;
  assert.equal(h.store.getState().snapshot.sampledAt, 100);
  assert.ok(h.store.getState().diagnostics.some(row => row.code === 'render_failed'));
  const before = h.updates.length;
  await h.advance(5000);
  assert.ok(h.updates.length > before);
  assert.equal(h.store.getState().snapshotStatus.fresh, false);
  assert.ok(h.requests.length > 1);
  unsubscribe(); h.store.destroy(); assert.equal(h.timers.size, 0);
});

test('invalid diagnostic timestamps cannot poison a valid stream or the overview renderer', () => {
  const h = harness(); h.store.start({snapshot: true});
  h.sources[0].emit('snapshot', snap(100, {collectorDiagnostics: [
    {stage: 'inference', code: 'timeout', failedAt: 1e300},
    {stage: 'inference', code: 'timeout', failedAt: '100'},
    {stage: '/private/path', code: 'timeout', failedAt: 100}]}));
  assert.equal(h.store.getState().snapshotStatus.fresh, true);
  assert.deepEqual(h.store.getState().diagnostics, []);
  h.store.destroy();
});

test('repeated stream failures back off, valid recovery resets retry and start is idempotent', async () => {
  const h = harness(); h.store.start({snapshot: true}); h.store.start({snapshot: true});
  assert.equal(h.sources.length, 1);
  for (const wait of [5000, 10000, 20000, 30000]) {
    h.sources.at(-1).emit('error');
    const before = h.sources.length;
    // Avoid opening the next silent watchdog until the next deliberate failure.
    await h.advance(wait - 1); assert.equal(h.sources.length, before);
    await h.advance(1); assert.equal(h.sources.length, before + 1);
  }
  h.sources.at(-1).emit('snapshot', snap(h.now())); h.sources.at(-1).emit('error');
  const before = h.sources.length;
  await h.advance(5000); assert.equal(h.sources.length, before + 1);
  h.store.destroy();
});

test('overview paints initial collector stage, code and time without inventing a snapshot', () => {
  const source = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  const definitions = source.replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];
  const elements = new Map(), frames = [];
  const element = () => ({children: [], textContent: '', classList: {toggle() {}},
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; }});
  const document = {getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    createElement: element, body: element()};
  let listener;
  const api = runInNewContext(`${definitions}\nrenderVitals=renderMemoryBanner=syncWebPulse=updateOnlineCodeMode=()=>{};\n({openStream,get snapshot(){return snapshot;}})`, {
    document, window: {requestAnimationFrame: fn => frames.push(fn)},
    fleetLanes: () => [],
    getMonitorStore: () => ({subscribe(fn) { listener = fn; return () => {}; }, start() {}}),
  });
  api.openStream();
  listener({snapshot: null, snapshotStatus: {available: false}, transport: {streaming: false},
    diagnostics: [{stage: 'collect_snapshot', code: 'timeout', failedAt: 100}]});
  frames.shift()();
  const text = node => node.textContent + node.children.map(text).join(' ');
  const attention = text(elements.get('attentionReasons'));
  assert.match(attention, /Collector collect_snapshot: timeout/);
  assert.match(attention, /1970-01-01T00:01:40.000Z/);
  assert.equal(api.snapshot, null);
});
