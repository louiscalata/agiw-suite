import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const tabs = new URL('./web/monitor-tabs.mjs', import.meta.url).href;
const source = readFileSync(new URL('./web/monitor-board.mjs', import.meta.url), 'utf8').replace("'/monitor-tabs.mjs'", JSON.stringify(tabs));
const { peerMap } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const mac = { host: 'mac', platform: 'mac', state: 'up', reasonCode: 'observed', trust: 'local', ageSeconds: 1.2, modelsComplete: true, loadedCount: 1, busyCount: 0 };
const linux = { host: 'linux', platform: 'linux', state: 'unknown', reasonCode: 'stale', trust: 'authenticated', ageSeconds: 41.6, modelsComplete: false, loadedCount: null };
const pc = { host: 'pc', platform: 'windows', state: 'down', reasonCode: 'reported_down', trust: 'share-reported', ageSeconds: 3 };
const lines = view => view.text.split('\n');

test('no rows is unknown, not an empty fleet', () => {
  for (const machines of [undefined, null, [], [null, 3]]) {
    assert.equal(peerMap({ machines, self: 'mac', feedFresh: true }).text, 'MACHINES  UNKNOWN\n          no machine rows in this sample');
  }
});

test('this machine is the hub and each peer has a link glyph and words', () => {
  const view = peerMap({ machines: [linux, pc, mac], self: 'mac', feedFresh: true });
  assert.deepEqual(lines(view), [
    'THIS MAC      UP       1 loaded / 0 busy · local · 1 s',
    ' |-?- LINUX    UNKNOWN  reading too old · authenticated · 42 s',
    ' |-x- PC       DOWN     reported down · share-reported · 3 s',
  ]);
});

test('counts appear only for a complete model list; a missing busy count is ?', () => {
  const partial = peerMap({ machines: [mac, { ...mac, host: 'pc', platform: 'windows', modelsComplete: false }], self: 'mac', feedFresh: true });
  assert.match(lines(partial)[1], /=== PC +UP +model list incomplete/);
  const noBusy = peerMap({ machines: [{ ...mac, busyCount: null }], self: 'mac', feedFresh: true });
  assert.match(lines(noBusy)[0], /1 loaded \/ \? busy/);
});

test('degraded is up but says degraded', () => {
  const view = peerMap({ machines: [mac, { ...mac, host: 'pc', platform: 'windows', reasonCode: 'reported_degraded' }], self: 'mac', feedFresh: true });
  assert.match(lines(view)[1], /^ \|=== PC +DEGRADED /);
});

test('a lone machine says so, and an old sample says it is not live', () => {
  const view = peerMap({ machines: [mac], self: 'mac', feedFresh: false });
  assert.deepEqual(lines(view).slice(1), [' |    no other machine in this sample', '          last sample, not live']);
  assert.match(view.label, /Last sample, not live\.$/);
});

test('the PC is the hub on the Windows edition', () => {
  const view = peerMap({ machines: [mac, pc], self: 'windows', feedFresh: true });
  assert.match(lines(view)[0], /^THIS PC +DOWN/);
  assert.match(lines(view)[1], /^ \|=== MAC /);
});

test('unexpected strings never reach the map as free text', () => {
  const view = peerMap({ machines: [mac, { host: 'x<script>', platform: 'other', state: 'unknown', reasonCode: 'Drop TABLE;', trust: 'very trusted!' }], self: 'mac', feedFresh: true });
  assert.match(lines(view)[1], /^ \|-\?- XSCRIPT +UNKNOWN +not observed$/);
  assert.match(view.text, /^[\x20-\x7e\n·]+$/);
});

test('reason codes read as plain words, and never-heard is not went-silent', async () => {
  const { reasonWords, REASON_WORDS } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
  assert.equal(reasonWords('not_observed'), 'never observed');
  assert.equal(reasonWords('not_connected'), 'went silent');
  assert.equal(reasonWords('some_new_code'), 'some new code');
  for (const bad of [undefined, null, 7, '', 'Drop TABLE;', '__proto__x!', 'toString']) assert.ok(/^[a-z ]+$/.test(reasonWords(bad)), String(bad));
  assert.equal(reasonWords('toString'), 'not observed');
  assert.equal(reasonWords('constructor'), 'constructor');
  // Every code the backend can send has its own words.
  const backend = readFileSync(new URL('./machine_state.py', import.meta.url), 'utf8').match(/UNKNOWN_REASONS = frozenset\(\{([^}]*)\}/)[1];
  for (const code of backend.match(/"([a-z_]+)"/g).map(q => q.slice(1, -1))) assert.ok(Object.prototype.hasOwnProperty.call(REASON_WORDS, code), code);
  const view = peerMap({ machines: [mac, { ...linux, reasonCode: 'not_observed' }, { ...pc, state: 'unknown', reasonCode: 'not_connected' }], self: 'mac', feedFresh: true });
  assert.match(lines(view)[1], /LINUX +UNKNOWN +never observed · authenticated · 42 s$/);
  assert.match(lines(view)[2], /PC +UNKNOWN +went silent · share-reported · 3 s$/);
});

test('the map names the Mac whether a PC row says mac or macos, and reads the PC superset row', () => {
  const pcSuperset = { id: 'pc', platform: 'windows', host: 'louisaurorar12', state: 'up', reasonCode: 'observed', trust: 'local', ageSeconds: 2,
    freshForSeconds: 30, modelsComplete: true, loadedCount: 1, busyCount: null, reason: 'serving' };
  for (const platform of ['mac', 'macos']) {
    const view = peerMap({ machines: [pcSuperset, { id: 'mac', platform, host: 'louismac', state: 'unknown', reasonCode: 'not_observed', trust: 'unverified' }], self: 'windows', feedFresh: true });
    assert.match(lines(view)[0], /^THIS PC +UP +1 loaded \/ \? busy · local · 2 s$/);
    assert.match(lines(view)[1], /^ \|-\?- MAC +UNKNOWN +never observed · unverified$/, platform);
  }
});

test('a PC row that says partly-serving is DEGRADED on the map, and odd platform words never reach a function', () => {
  const view = peerMap({ machines: [{ platform: 'windows', host: 'pc', state: 'up', reason: 'partly-serving', trust: 'local', ageSeconds: 0 }, { platform: 'constructor', host: 'x', state: 'unknown' }], self: 'windows', feedFresh: true });
  assert.match(lines(view)[0], /^THIS PC +DEGRADED /);
  assert.match(lines(view)[1], /^ \|-\?- X +UNKNOWN/);
  assert.match(view.text, /^[\x20-\x7e\n·]+$/);
  const shared = peerMap({ machines: [{ platform: 'windows', host: 'pc', state: 'up', reasonCode: 'observed', reason: 'partly-serving' }], self: 'windows', feedFresh: true });
  assert.match(lines(shared)[0], /^THIS PC +UP /);
});

test('a PC that is not configured has its own words, apart from one that was never heard', async () => {
  const { reasonWords, REASON_WORDS } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
  assert.equal(REASON_WORDS.not_configured, 'not configured');
  assert.equal(reasonWords('not_configured'), 'not configured');
  assert.notEqual(reasonWords('not_configured'), reasonWords('not_observed'));
  const view = peerMap({ machines: [mac, { host: 'pc', platform: 'windows', state: 'unknown', reasonCode: 'not_configured', trust: null, ageSeconds: null }], self: 'mac', feedFresh: true });
  assert.match(lines(view)[1], /^ \|-\?- PC +UNKNOWN +not configured$/);
});
