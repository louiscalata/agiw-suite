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
    ' |-?- LINUX    UNKNOWN  stale · authenticated · 42 s',
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
