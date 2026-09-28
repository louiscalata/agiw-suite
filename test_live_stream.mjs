import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import test from 'node:test';

// Spec A (26 Sep 2026): the page follows /api/stream and polls only while the stream is down. The
// dashboard's own feed functions run here against a fake EventSource, fetch and timers.
const appSource = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
const definitions = appSource.replace(/^import .*;\n/gm, '').split("$('pause').addEventListener")[0];

function harness({ eventSource = true } = {}) {
  const timers = [], frames = [], fetches = [], sources = [];
  class FakeEventSource {
    constructor(url) { this.url = url; this.listeners = {}; this.closed = false; sources.push(this); }
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
    emit(type, event = {}) { for (const listener of this.listeners[type] || []) listener(event); }
    close() { this.closed = true; }
  }
  const context = {
    ...(eventSource ? { EventSource: FakeEventSource } : {}),
    fetch: url => new Promise(resolve => fetches.push({ url, resolve })),
    setTimeout: (fn, ms) => { timers.push({ fn, ms, id: timers.length + 1, cleared: false }); return timers.length; },
    clearTimeout: id => { const timer = timers.find(t => t.id === id); if (timer) timer.cleared = true; },
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    performance: { now: () => 0 }, window: { requestAnimationFrame: fn => frames.push(fn) }, document: {}, Date, JSON,
  };
  const api = runInNewContext(`${definitions}
let rendered=0,freshnessUpdates=0;render=()=>{rendered++;};updateFreshness=()=>{freshnessUpdates++;};
({openStream,validSnapshot,streamBackoff,feedAge,
  get state(){return{streaming:Boolean(feedStream),polling:pollActive,attempt:streamAttempt,connected,snapshot,rendered,freshnessUpdates};},
  setPaused(value){paused=value;}})`, context);
  const pending = () => timers.filter(t => !t.cleared && !t.ran);
  const run = timer => { timer.ran = true; timer.fn(); };
  return { api, timers, frames, fetches, sources, pending, run };
}

const snap = (sampledAt, extra = {}) => ({ schemaVersion: 1, models: [], sources: [], sampledAt, ...extra });
const message = data => ({ data: JSON.stringify(data) });
const tick = () => new Promise(resolve => setImmediate(resolve));

test('A: feedAge follows the last full sample; row ages keep using sampledAt', () => {
  const { api } = harness();
  assert.equal(api.feedAge({ sampledAt: 100, fullSampledAt: 97 }, 101), 4, 'an overlay re-stamps sampledAt, not fullSampledAt');
  assert.equal(api.feedAge({ sampledAt: 100 }, 101), 1, 'older servers without fullSampledAt');
  assert.equal(api.feedAge({ sampledAt: 100, fullSampledAt: null }, 101), 1);
  assert.ok(Number.isNaN(api.feedAge({ sampledAt: 100, fullSampledAt: 'soon' }, 101)), 'a malformed full-sample time is never fresh');
  assert.ok(Number.isNaN(api.feedAge(null, 101)));
  assert.match(appSource, /const fresh=\(\)=>\{const age=feedAge\(snapshot,Date\.now\(\)\/1000\);return connected&&age>=-1&&age<=3;\};/);
  assert.match(appSource, /const clockMismatch=\(\)=>connected&&feedAge\(snapshot,Date\.now\(\)\/1000\) < -1;/);
  assert.match(appSource, /const sampleAge=\(\)=>snapshot&&Number\.isFinite\(snapshot\.sampledAt\)\?Date\.now\(\)\/1000-snapshot\.sampledAt:NaN;/);
});

test('A: stream retry backs off 5 s, doubling to at most 30 s', () => {
  const { api } = harness();
  assert.deepEqual([0, 1, 2, 3, 4, 9, -1, 'x'].map(api.streamBackoff), [5000, 10000, 20000, 30000, 30000, 30000, 5000, 5000]);
});

test('A: stream messages are validated like a poll and bursts coalesce into one render per frame', () => {
  const { api, sources, frames, fetches } = harness();
  api.openStream();
  assert.equal(sources.length, 1);
  assert.equal(sources[0].url, '/api/stream');
  assert.equal(fetches.length, 0, 'no polling while the stream is open');
  const now = Date.now() / 1000;
  for (let i = 0; i < 3; i++) sources[0].emit('snapshot', message(snap(now + i / 10)));
  assert.equal(frames.length, 1, 'one frame for three messages');
  assert.equal(api.state.connected, true);
  frames.shift()();
  assert.equal(api.state.rendered, 1);
  assert.equal(api.state.snapshot.sampledAt, now + .2, 'the newest message is the one rendered');
  const kept = api.state.snapshot;
  for (const bad of ['{not json', JSON.stringify({ schemaVersion: 2, models: [], sources: [], sampledAt: now }),
    JSON.stringify({ schemaVersion: 1, models: {}, sources: [], sampledAt: now }), JSON.stringify(snap(Infinity))]) {
    sources[0].emit('snapshot', { data: bad });
    assert.equal(api.state.connected, false);
    assert.equal(api.state.snapshot, kept);
  }
  // Round 3 review: invalid messages refresh freshness at most once per frame, never render.
  assert.equal(frames.length, 1, 'four invalid messages, one frame');
  assert.equal(api.state.freshnessUpdates, 0, 'nothing runs before the frame');
  frames.shift()();
  assert.deepEqual([api.state.freshnessUpdates, api.state.rendered], [1, 1]);
  // Paused: the connection is known, the picture is not replaced, and a burst is one freshness update.
  api.setPaused(true);
  for (let i = 0; i < 3; i++) sources[0].emit('snapshot', message(snap(now + 1 + i)));
  assert.equal(api.state.connected, true);
  assert.equal(api.state.snapshot, kept);
  assert.equal(frames.length, 1, 'three paused messages, one frame');
  frames.shift()();
  assert.deepEqual([api.state.freshnessUpdates, api.state.rendered], [2, 1]);
  // A queued render already refreshes freshness, so a paused message behind it queues nothing more.
  api.setPaused(false);
  sources[0].emit('snapshot', message(snap(now + 5)));
  api.setPaused(true);
  sources[0].emit('snapshot', message(snap(now + 6)));
  sources[0].emit('snapshot', { data: '{bad' });
  assert.equal(frames.length, 1);
  frames.shift()();
  assert.deepEqual([api.state.freshnessUpdates, api.state.rendered], [2, 2]);
  assert.match(appSource, /if\(!validSnapshot\(data\)\)\{connected=false;scheduleFreshness\(\);return;\}connected=true;streamAttempt=0;if\(!paused\)\{snapshot=data;scheduleRender\(\);\}else scheduleFreshness\(\);/);
});

test('A: an error or a 503 falls back to polling; the stream retries later and the two never overlap', async () => {
  const { api, sources, fetches, pending, run } = harness();
  api.openStream();
  const now = Date.now() / 1000;
  sources[0].emit('error');
  assert.equal(sources[0].closed, true);
  assert.deepEqual([api.state.streaming, api.state.polling], [false, true]);
  assert.equal(fetches.length, 1);
  assert.equal(fetches[0].url, '/api/snapshot');
  const retry = () => pending().find(t => t.ms >= 5000);
  assert.equal(retry().ms, 5000);
  // The poll answers and schedules the next one about a second later.
  fetches[0].resolve({ ok: true, json: async () => snap(now) });
  await tick(); await tick();
  assert.equal(api.state.connected, true);
  const nextPoll = pending().find(t => t.ms <= 1000);
  assert.ok(nextPoll, 'polling continues');
  // The retry reopens the stream and stops polling: the queued poll is cancelled.
  run(retry());
  assert.equal(sources.length, 2);
  assert.deepEqual([api.state.streaming, api.state.polling], [true, false]);
  assert.equal(nextPoll.cleared, true);
  // A poll still in flight when the stream reopened is discarded, and schedules nothing.
  sources[1].emit('error');
  const inFlight = fetches.at(-1);
  run(retry());
  inFlight.resolve({ ok: true, json: async () => snap(now + 50) });
  await tick(); await tick();
  assert.notEqual(api.state.snapshot?.sampledAt, now + 50);
  assert.equal(pending().filter(t => t.ms <= 1000).length, 0, 'no poll loop beside the open stream');
  // Each consecutive failure doubles the wait, up to 30 s; a good message resets it.
  const waits = [];
  for (let i = 0; i < 4; i++) { sources.at(-1).emit('error'); waits.push(retry().ms); run(retry()); }
  assert.deepEqual(waits, [20000, 30000, 30000, 30000]);
  sources.at(-1).emit('snapshot', message(snap(now)));
  sources.at(-1).emit('error');
  assert.equal(retry().ms, 5000);
  // Events from a stream that was already replaced are ignored.
  const count = sources.length;
  sources[0].emit('error');
  assert.equal(sources.length, count);
});

test('A: without EventSource the page polls, and the app opens the stream at start instead of polling', () => {
  const { api, fetches, sources } = harness({ eventSource: false });
  api.openStream();
  assert.deepEqual([sources.length, fetches.length, api.state.polling], [0, 1, true]);
  assert.match(appSource, /source=new EventSource\('\/api\/stream'\)/);
  assert.match(appSource, /source\.addEventListener\('snapshot',/);
  assert.match(appSource, /source\.addEventListener\('error',\(\)=>\{if\(feedStream===source\)streamFailed\(\);\}\);/);
  assert.match(appSource, /function streamFailed\(\)\{[^\n]*startPolling\(\);[^\n]*streamRetry=setTimeout\(openStream,streamBackoff\(streamAttempt\+\+\)\);\}/);
  assert.match(appSource, /function startPolling\(\)\{if\(pollActive\)return;pollActive=true;poll\(\+\+pollGeneration\);\}/);
  assert.match(appSource, /setInterval\(updateFreshness,1000\);/);
  assert.ok(appSource.trimEnd().endsWith('pollOnlineCodeAction();openStream();'));
  assert.doesNotMatch(appSource, /pollOnlineCodeAction\(\);poll\(\);/);
});
