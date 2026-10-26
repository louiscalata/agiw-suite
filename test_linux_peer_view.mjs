import test from 'node:test';
import assert from 'node:assert/strict';
import {linuxPeerView, agiwPeersRows} from './web/map-layout.mjs';

const fixture = () => ({host: 'linux-fixture', platform: 'linux', observedUnix: 100, health: 'up',
  modelsComplete: true, modelsTotal: 1,
  models: [{id: 'model', loadedState: 'loaded', activity: 'idle', slotsBusy: 0, slotsTotal: 1}],
  gpus: [], gpusComplete: true, gpusState: 'known', memory: {availablePercent: 30, level: 'ok'},
  network: {state: 'up', callersState: 'known', callers: [], listeners: []}});
const view = value => linuxPeerView(value, {feedFresh: true, now: 100});

test('Linux peer positive control preserves observed idle and explicit complete empty inventories', () => {
  const v = view(fixture());
  assert.equal(v.fields.loaded.text, '1');
  assert.equal(v.fields.gpus.text, '0');
  assert.equal(v.fields.callers.text, '0');
  assert.equal(v.fields['model:model:activity'].text, 'idle');
  assert.deepEqual(agiwPeersRows([fixture()], {feedFresh: true, now: 100}), v.rows);
});

test('Linux shared peer summaries never retain exact loaded counts after expiry or clock reversal', () => {
  for (const ageSeconds of [-1, 31]) {
    const peer = {...fixture(), ageSeconds, fresh: true};
    delete peer.observedUnix;
    const rows = agiwPeersRows([peer], {feedFresh: true});
    assert.equal(rows.find(([label]) => label.endsWith(' · Loaded models'))[1],
      'Unknown (snapshot stale, future or unavailable)');
    assert.doesNotMatch(rows.map(row => row[1]).join(' '), /\b1 loaded\b/);
  }
});

test('Linux peer stale, future, invalid or contradictory clocks cannot show current values', () => {
  for (const change of [{observedUnix: 89}, {observedUnix: 101}, {observedUnix: null, ageSeconds: 0},
    {observedUnix: '100', ageSeconds: 0}, {observedUnix: 100, observedAt: 'invalid'},
    {observedUnix: 100, observedAt: '1970-01-01T00:03:20Z'}, {fresh: 'true'}]) {
    const v = view({...fixture(), ...change});
    assert.equal(v.fresh, false, JSON.stringify(change));
    for (const value of Object.values(v.fields)) assert.equal(value.known, false);
    assert.doesNotMatch(v.rows.map(row => row[1]).join(' '), /\bidle\b|\bgenerating\b|\bdown\b|\bup\b/);
  }
});

test('Linux peer malformed inventory identities cannot certify an exact count', () => {
  for (const models of [null, {}, [{loadedState: 'loaded'}], [{id: '', loadedState: 'loaded'}],
    [{id: 'x', loadedState: 'loaded'}, {id: 'x', loadedState: 'inactive'}]]) {
    const v = view({...fixture(), models, modelsTotal: Array.isArray(models) ? models.length : 0});
    assert.equal(v.fields.modelsTotal.known, false);
    assert.equal(v.fields.loaded.known, false);
  }
});

test('Linux peer partial or inconsistent slot evidence withholds activity and rates', () => {
  for (const slots of [{slotsBusy: null}, {slotsTotal: null}, {slotsBusy: 1}, {slotsBusy: .5},
    {slotsBusy: 0, slotsTotal: 0}, {slotsTotal: 33}]) {
    const base = fixture();
    base.models[0] = {...base.models[0], ...slots, liveTokensPerSecond: 20};
    const v = view(base);
    for (const key of ['activity', 'slotsBusy', 'slotsTotal', 'liveTokensPerSecond'])
      assert.equal(v.fields[`model:model:${key}`].known, false, JSON.stringify(slots));
  }
});

test('Linux peer unvalidated caller rows cannot certify counts or display exact connection numbers', () => {
  for (const caller of [{name: 'codex', lane: 'gpu-wrapper', connections: null},
    {name: 'codex', lane: 'unknown', connections: 1}, {name: '/private/path', lane: 'gpu-wrapper', connections: 1}]) {
    const base = fixture();
    base.network.callers = [caller];
    const v = view(base);
    assert.equal(v.fields.callers.known, false);
    assert.ok(!v.rows.some(([label]) => label.endsWith(' · caller')));
  }
});

test('Linux peer memory summaries require a valid observed percentage', () => {
  for (const availablePercent of [null, -1, 101, '30', true]) {
    const v = view({...fixture(), memory: {level: 'ok', availablePercent}});
    assert.equal(v.fields.memory.known, false);
    assert.equal(v.fields.memoryLevel.known, false);
  }
});
