import test from 'node:test';
import assert from 'node:assert/strict';
import {macPeerView} from './web/map-layout.mjs';

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
