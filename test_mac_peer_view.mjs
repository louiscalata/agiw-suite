import test from 'node:test';
import assert from 'node:assert/strict';
import {macPeerView,agiwPeersRows} from './web/map-layout.mjs';

const peer = (over = {}) => ({state: 'reachable', ageSeconds: 4, latencyMs: 3.6, address: '10.0.0.176', via: 'mdns',
  loadedCount: 1, expectedVerifyModel: 'openai/gpt-oss-20b', expectedVerifyLoaded: true,
  models: [{id: 'openai/gpt-oss-20b', state: 'loaded'}, {id: 'qwen/qwen3.8-27b', state: 'not-loaded'}], ...over});

test('reachable peer lists loaded models as inventory only', () => {
  const v = macPeerView(peer(), {feedFresh: true, snapshotAge: 1});
  assert.equal(v.state, 'reachable');
  assert.equal(v.subtitle, 'LAN 4 MS · 1 LOADED');
  assert.deepEqual(v.satellites.map(m => m.id), ['openai/gpt-oss-20b']);
  assert.equal(v.chip.tone, 'ok');
  assert.match(v.rows.find(([k]) => k === 'Expected verify model')[1], /loaded$/);
});

test('a stale feed or an old probe never reads reachable', () => {
  assert.equal(macPeerView(peer(), {feedFresh: false}).state, 'unknown');
  assert.equal(macPeerView(peer({ageSeconds: 40}), {feedFresh: true, snapshotAge: 6}).state, 'unknown');
  assert.equal(macPeerView(peer({ageSeconds: -1}), {feedFresh: true}).state, 'unknown');
});

test('unreachable and malformed inputs', () => {
  const down = macPeerView({state: 'unreachable', ageSeconds: 2, models: [{id: 'x', state: 'loaded'}]}, {feedFresh: true});
  assert.equal(down.subtitle, 'NOT ANSWERING ON THE LAN');
  assert.deepEqual(down.satellites, []);
  assert.equal(down.chip.tone, 'warn');
  for (const bad of [null, [], 'x', {state: 'bogus', ageSeconds: 1}]) assert.equal(macPeerView(bad, {feedFresh: true}).state, 'unknown');
});

test('v1 listing without loaded state says so', () => {
  const v = macPeerView(peer({loadedCount: null, models: [{id: 'm', state: 'listed'}]}), {feedFresh: true});
  assert.equal(v.loadedKnown, false);
  assert.match(v.subtitle, /LOADED STATE UNKNOWN/);
  assert.deepEqual(v.satellites, []);
});

test('model ids with control characters or spaces are dropped', () => {
  const v = macPeerView(peer({models: [{id: 'bad\nid', state: 'loaded'}, {id: 'a b', state: 'loaded'}, {id: 'ok', state: 'loaded'}]}), {feedFresh: true});
  assert.deepEqual(v.loaded.map(m => m.id), ['ok']);
});

test('linked through SharedChami reads as connected without claiming the LAN', () => {
  const v = macPeerView({state: 'linked', ageSeconds: 1, via: 'sharedchami', linkAgeSeconds: 8, loadedCount: 1,
    models: [{id: 'qwen/qwen3.8-27b', state: 'loaded'}]}, {feedFresh: true});
  assert.equal(v.reachable, true);
  assert.equal(v.linked, true);
  assert.equal(v.subtitle, 'LINKED VIA SHARE · 1 LOADED');
  assert.match(v.rows[0][1], /LM Studio not open to the LAN/);
  assert.equal(v.chip.short, 'Linked · 1 loaded');
});

test('peer rows name each AGIW instance and its freshness', () => {
  const rows = agiwPeersRows([{host: 'louis-m5-pro', fresh: true, platform: 'macos', edition: 'mac', version: '1.0.0', ageSeconds: 4,
    source: 'agiw-peers', models: [{id: 'a', loadedState: 'loaded'}]}, {host: 'bad host!', ageSeconds: 1}], {feedFresh: true});
  assert.equal(rows.length, 1);
  assert.match(rows[0][1], /^Linked · macos · mac · 1\.0\.0 · 1 loaded · 4 s ago/);
  assert.deepEqual(agiwPeersRows([], {feedFresh: true}), [['AGIW peers', 'None has published on SharedChami yet']]);
});

test('the reported loaded count wins over the validated name list, and the gap is disclosed', () => {
  const v = macPeerView({state: 'reachable', ageSeconds: 1, loadedCount: 2, models: [{id: 'bad name', state: 'loaded'}, {id: 'ok', state: 'loaded'}]}, {feedFresh: true});
  assert.equal(v.loadedCount, 2);
  assert.match(v.subtitle, /2 LOADED/);
  assert.equal(v.rows.find(([k]) => k === 'Loaded models')[1], 'ok · 1 more with an unreadable name');
});
