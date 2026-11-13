import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fleetState,fleetFacts,fleetMachines,fleetLanes,fleetSummary,fleetWord,fleetMachineCoverage} from './web/fleet-state.mjs';
const fixture = JSON.parse(readFileSync(new URL('./test_support/fleet-state.json', import.meta.url), 'utf8'));
const sample = {...fixture.snapshot, fleetState: fixture.fleetState};

test('the shared contract owns three configured machines, unmatched reports, endpoint words and coverage', () => {
  const options = {now: fixture.now};
  assert.deepEqual(fleetFacts(sample, options).map(fact => fact.id), fixture.fleetState.facts.map(fact => fact.id));
  assert.equal(fleetMachines(sample, options).length, 4);
  assert.equal(fleetLanes(sample, options).length, 4);
  assert.deepEqual(fleetSummary(sample, options).counts, fixture.fleetState.counts);
  assert.equal(fleetSummary(sample, options).text, fixture.fleetState.summary);
  assert.equal(fleetMachines(sample, options).find(fact => fact.id === 'registry:pc').state, 'unknown');
  assert.equal(fleetLanes(sample, options)[1].state, 'endpoint answering');
  assert.equal(fleetSummary({machines: sample.machines}, options).state, 'unknown');
  assert.deepEqual(fleetFacts({machines: sample.machines}, options), []);
});

test('stale facts age independently and partial totals stay unknown', () => {
  const early = fleetState(sample, {now: fixture.now + 4});
  assert.match(fleetWord(early.facts.find(fact => fact.id === 'mac:lmstudio')), /stale · 5 s ago/);
  assert.equal(early.facts.find(fact => fact.id === 'registry:pc:fast').state, 'endpoint answering');
  assert.equal(early.facts.find(fact => fact.id === 'peer:spare').state, 'reported');
  const late = fleetSummary(sample, {now: fixture.now + 6});
  assert.equal(late.counts.stale, 3);
  assert.equal(late.counts.unknown, 1);
  assert.equal(late.counts.busy, null);
  assert.equal(late.counts.loaded, null);
  assert.equal(late.counts.answering, null);
  assert.match(late.text, /Answering count unknown; 4 lanes configured; 3 stale:/);
  assert.match(late.text, /7 s ago/);
});

test('missing time, invalid state or duplicate id cannot create fresh evidence', () => {
  const base = fixture.fleetState.facts.find(fact => fact.scope === 'lane');
  const contract = {schemaVersion: 1, facts: [{...base, observedAt: null}, {...base, state: 'ok'}]};
  const facts = fleetFacts(contract, {now: fixture.now});
  assert.equal(facts.length, 1);
  assert.equal(facts[0].state, 'unknown');
  assert.equal(facts[0].ageSeconds, null);
});


test('expiry boundaries and clock skew use the contract clock rules', () => {
  const base = fixture.fleetState.facts.find(fact => fact.scope === 'lane');
  const one = fact => ({schemaVersion: 1, facts: [fact]});
  const expiry = Date.parse(base.expiresAt) / 1000;
  assert.equal(fleetFacts(one(base), {now: expiry})[0].state, 'endpoint answering');
  assert.equal(fleetFacts(one(base), {now: expiry + .001})[0].state, 'stale');
  const future = {...base, observedAt: new Date((fixture.now + 2)*1000).toISOString(), expiresAt: new Date((fixture.now + 5)*1000).toISOString()};
  assert.equal(fleetFacts(one(future), {now: fixture.now})[0].state, 'endpoint answering');
  assert.equal(fleetFacts(one(future), {now: fixture.now - .001})[0].state, 'unknown');
  for (const changed of [{observedAt: '2026-11-12T11:59:59'}, {expiresAt: '2026-11-12T11:59:58Z'}])
    assert.equal(fleetFacts(one({...base, ...changed}), {now: fixture.now})[0].state, 'unknown');
});


test('a down or partial report cannot publish zero loaded or busy models', () => {
  const base = fixture.fleetState.facts.find(fact => fact.id === 'mac');
  for (const evidence of [{...base.evidence,state:'down',loadedCount:0,busyCount:0}, {...base.evidence,modelsComplete:false}]) {
    const contract={schemaVersion:1,facts:[{...base,evidence}]};
    const summary=fleetSummary(contract,{now:fixture.now});
    assert.equal(summary.counts.loaded,null);assert.equal(summary.counts.busy,null);
  }
});


test('zero answering is known only for fresh complete unavailable coverage',()=>{
  const lane=fixture.fleetState.facts.find(fact=>fact.scope==='lane');
  const unavailable={...lane,state:'endpoint unavailable',reason:'timeout'};
  assert.equal(fleetSummary({schemaVersion:1,facts:[unavailable]},{now:fixture.now}).counts.answering,0);
  assert.equal(fleetSummary({schemaVersion:1,facts:[{...unavailable,observedAt:null}]},{now:fixture.now}).counts.answering,null);
});


test('machine headlines use scoped lane coverage even when host health is unmeasured', () => {
  const pc = {id: 'registry:pc', state: 'unknown'}, pro = {id: 'registry:pro', state: 'unknown'};
  const lane = (parentId, state = 'endpoint answering') => ({parentId, state});
  const lanes = [lane(pc.id), lane(pc.id), lane(pc.id), lane(pro.id)];
  assert.equal(fleetMachineCoverage(pc, lanes).text, '3 of 3 lanes answering');
  assert.equal(fleetMachineCoverage(pro, lanes).text, '1 of 1 lane answering');
  assert.equal(fleetMachineCoverage(pc, lanes).glyph, '■');
  assert.equal(fleetMachineCoverage(pc, lanes).detail, 'Host health not measured.');
  assert.equal(fleetMachineCoverage(pc, [lane(pc.id, 'endpoint unavailable')]).text, '0 of 1 lane answering');
  assert.equal(fleetMachineCoverage(pc, [lane(pc.id, 'unknown')]).text, 'UNKNOWN');
  assert.equal(fleetMachineCoverage(pc, [lane(pc.id, 'stale')]).text, '1 lane · evidence stale');
  assert.equal(fleetMachineCoverage({id: 'peer:report', state: 'reported'}, []).text, 'peer report observed');
});

test('zero answering lanes never earn an up glyph from a host report or partial probes', () => {
  const machine = {id: 'example', state: 'reported'};
  const lane = state => ({parentId: machine.id, state});
  for (const lanes of [[], [lane('unknown')], [lane('endpoint unavailable'), lane('unknown')]])
    assert.equal(fleetMachineCoverage(machine, lanes).glyph, '?');
  assert.equal(fleetMachineCoverage(machine, [lane('endpoint answering'), lane('unknown')]).glyph, '■');
});

test('an invalid registry stays visible with partial coverage and unknown zero', () => {
  const lane = {...fixture.fleetState.facts.find(fact => fact.scope === 'lane'), state: 'endpoint unavailable'};
  const registry = {id: 'remote-machines', scope: 'registry', state: 'unknown', reason: 'registry_invalid',
    observedAt: lane.observedAt, expiresAt: lane.expiresAt, evidence: {state: 'invalid'}};
  for (const facts of [[lane], []]) {
    const summary = fleetSummary({schemaVersion: 1, facts, registry}, {now: fixture.now});
    assert.match(summary.text, /registry invalid/);
    assert.match(summary.headline, /partial/i);
    assert.equal(summary.counts.answering, null);
    assert.equal(summary.completeness, 'partial');
  }
});

test('an answered endpoint with ambiguous identity stays unknown and says what answered', () => {
  const lane = {...fixture.fleetState.facts.find(fact => fact.scope === 'lane'), state: 'unknown',
    reason: 'multiple_loaded', evidence: {endpointAnswered: true}};
  assert.equal(fleetWord(lane), 'unknown · answered, identity unknown');
  assert.match(fleetSummary({schemaVersion: 1, facts: [lane]}, {now: fixture.now}).text, /answered, identity unknown/);
  assert.equal(fleetWord({...lane, state: 'stale'}).includes('answered'), false);
});

test('machine coverage distinguishes answered identity-unknown lanes from pending evidence', () => {
  const machine = {id: 'example', state: 'unknown'};
  const answered = {parentId: machine.id, state: 'unknown', evidence: {endpointAnswered: true}};
  const pending = {parentId: machine.id, state: 'unknown', ageSeconds: 1};
  assert.equal(fleetMachineCoverage(machine, [answered]).text, '1 lane answered · identity unknown');
  assert.equal(fleetMachineCoverage(machine, [answered, {...answered}]).text, '2 lanes answered · identity unknown');
  assert.equal(fleetMachineCoverage(machine, [answered, pending]).text, '1 of 2 lanes answered · identity unknown');
  assert.equal(fleetMachineCoverage(machine, [pending]).text, '1 lane · endpoint evidence pending');
  assert.equal(fleetMachineCoverage(machine, [{...answered, state: 'stale'}]).text, '1 lane · evidence stale');
  assert.equal(fleetMachineCoverage(machine, [answered]).glyph, '?');
});

test('answered-identity evidence expires and cannot survive a missing or future clock', () => {
  const base = fixture.fleetState.facts.find(fact => fact.scope === 'lane');
  for (const change of [{observedAt: null}, {observedAt: '2099-01-01T00:00:00Z'}, {expiresAt: base.observedAt}]) {
    const lane = {...base, state: 'unknown', reason: 'identity_unknown', evidence: {endpointAnswered: true}, ...change};
    const contract = {schemaVersion: 1, facts: [lane]};
    assert.doesNotMatch(fleetWord(fleetLanes(contract, {now: fixture.now})[0]), /answered/);
    assert.doesNotMatch(fleetSummary(contract, {now: fixture.now}).text, /answered/);
  }
});

test('registry completeness follows its original valid clock', () => {
  const base = fixture.fleetState.facts.find(fact => fact.scope === 'lane');
  const registry = {...base, scope: 'registry', state: 'reported', reason: 'registry_configured', completeness: 'complete'};
  const contract = {schemaVersion: 1, facts: [{...base, state: 'endpoint unavailable'}], registry};
  assert.equal(fleetSummary(contract, {now: fixture.now}).completeness, 'complete');
  for (const now of [fixture.now + 10, NaN])
    assert.equal(fleetSummary(contract, {now}).completeness, 'partial');
  registry.expiresAt = '2026-11-12T11:59:58Z';
  assert.equal(fleetSummary(contract, {now: fixture.now - 2}).completeness, 'partial');
});
