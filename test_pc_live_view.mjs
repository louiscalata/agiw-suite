import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {pcAdaptersView} from './web/map-layout.mjs';

// Evaluate the production helper without starting app.js's DOM and polling loop,
// as the other app.js source-harness tests do. Use its real text formatters too.
const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
const start = app.indexOf('function pcLiveRows(');
const end = app.indexOf('\nfunction headlessView(', start);
assert.ok(start >= 0 && end > start, 'pcLiveRows source boundaries exist');
const formatters = ['ageText', 'capital'].map(name => {
  const source = app.match(new RegExp(`^const ${name}=.*;$`, 'm'));
  assert.ok(source, `${name} production formatter exists`);
  return source[0];
}).join('\n');
// pcAdaptersView is imported by app.js from map-layout.mjs; inject the real one into the harness scope.
const pcLiveRows = new Function('pcAdaptersView', `${formatters}\n${app.slice(start, end)}\nreturn pcLiveRows;`)(pcAdaptersView);

const model = (over = {}) => ({id: 'gpt-oss-20b', loadedState: 'loaded', activity: 'idle',
  slotsBusy: 0, slotsTotal: 2, ...over});
const feed = (over = {}) => ({via: 'share', host: 'louisaurorar12', ageSeconds: 4,
  models: [model()], ...over});
const fresh = {feedFresh: true, snapshotAge: 1};
const row = (view, label) => view.rows.find(([key]) => key === label)?.[1];

test('fresh share feed names the PC and preserves loaded, idle and zero busy slots', () => {
  assert.deepEqual(pcLiveRows(feed(), fresh), {
    title: 'Unsigned report from louisaurorar12',
    rows: [['gpt-oss-20b', 'Loaded · Idle · 0/2 slots busy'],
      ['Observed', '5s ago · via SharedChami files, unsigned']],
  });
});

test('LAN and share provenance use distinct signed and unsigned wording', () => {
  assert.equal(row(pcLiveRows(feed({via: 'lan'}), fresh), 'Observed'),
    '5s ago · via paired LAN link, signed');
  assert.equal(row(pcLiveRows(feed({via: 'share'}), fresh), 'Observed'),
    '5s ago · via SharedChami files, unsigned');
  assert.equal(pcLiveRows(feed({host: ''}), fresh).title, 'Unsigned report from the PC');
  // The paired link names the peer that signed the report; what the PC says it is doing stays its own report.
  assert.equal(pcLiveRows(feed({via: 'lan'}), fresh).title, 'From louisaurorar12 · paired link');
  assert.equal(pcLiveRows(feed({via: 'lan', host: ''}), fresh).title, 'From the PC · paired link');
});

test('a stale containing feed hides even a just-observed PC feed', () => {
  assert.equal(pcLiveRows(feed({ageSeconds: 0}), {feedFresh: false, snapshotAge: 0}), null);
});

test('the 30-second freshness boundary includes time since the enclosing snapshot', () => {
  assert.equal(row(pcLiveRows(feed({ageSeconds: 29}), fresh), 'Observed'),
    '30s ago · via SharedChami files, unsigned');
  for (const [ageSeconds, snapshotAge] of [[29.1, 1], [30, 0.1], [31, 0], [0, 31]]) {
    assert.equal(pcLiveRows(feed({ageSeconds}), {feedFresh: true, snapshotAge}), null);
  }
});

test('negative ages cannot become live when the other age offsets them', () => {
  for (const [ageSeconds, snapshotAge] of [[-2, 0], [-1, 2], [4, -1]]) {
    assert.equal(pcLiveRows(feed({ageSeconds}), {feedFresh: true, snapshotAge}), null,
      `ageSeconds=${ageSeconds}, snapshotAge=${snapshotAge}`);
  }
});

test('missing or nonfinite ages never show a PC report with an unknown age', () => {
  for (const age of [undefined, null, NaN, Infinity, -Infinity, '4']) {
    assert.equal(pcLiveRows(feed({ageSeconds: age}), fresh), null, `feed age ${String(age)}`);
    assert.equal(pcLiveRows(feed(), {feedFresh: true, snapshotAge: age}), null,
      `snapshot age ${String(age)}`);
  }
});

test('missing feeds and malformed model collections are hidden', () => {
  for (const live of [null, undefined, {}, feed({models: null}), feed({models: {}})]) {
    assert.equal(pcLiveRows(live, fresh), null);
  }
});

test('unknown loaded state stays Unknown and inactive says Not loaded', () => {
  for (const loadedState of ['unknown', 'unexpected', undefined, null]) {
    const view = pcLiveRows(feed({models: [{id: 'model', loadedState}]}), fresh);
    assert.equal(row(view, 'model'), 'Unknown');
  }
  const view = pcLiveRows(feed({models: [{id: 'model', loadedState: 'inactive', activity: 'unknown'}]}), fresh);
  assert.equal(row(view, 'model'), 'Not loaded');
});

test('finite reported token rates, including zero, appear without inventing a rate', () => {
  for (const rate of [0, 104.3]) {
    const view = pcLiveRows(feed({models: [model({liveTokensPerSecond: rate})]}), fresh);
    assert.equal(row(view, 'gpt-oss-20b'), `Loaded · Idle · 0/2 slots busy · ${rate} tok/s reported`);
  }
  for (const rate of [undefined, null, NaN, Infinity, -Infinity, '104.3', true]) {
    const view = pcLiveRows(feed({models: [model({liveTokensPerSecond: rate})]}), fresh);
    assert.equal(row(view, 'gpt-oss-20b'), 'Loaded · Idle · 0/2 slots busy');
  }
});

test('PC memory includes the level and only a finite free percentage', () => {
  for (const availablePercent of [0, 55.6]) {
    const view = pcLiveRows(feed({memory: {level: 'ok', availablePercent}}), fresh);
    assert.equal(row(view, 'PC memory'), `Ok · ${availablePercent}% free`);
  }
  for (const availablePercent of [undefined, null, NaN, Infinity, '55.6']) {
    const view = pcLiveRows(feed({memory: {level: 'watch', availablePercent}}), fresh);
    assert.equal(row(view, 'PC memory'), 'Watch');
  }
  for (const memory of [undefined, null, {}, {availablePercent: 55.6}]) {
    assert.equal(row(pcLiveRows(feed({memory}), fresh), 'PC memory'), undefined);
  }
});

test('the model list is bounded while retaining memory and observation rows', () => {
  const view = pcLiveRows(feed({models: Array.from({length: 10}, (_, i) => model({id: `model-${i}`})),
    memory: {level: 'ok'}}), fresh);
  assert.deepEqual(view.rows.map(([label]) => label),
    [...Array.from({length: 8}, (_, i) => `model-${i}`), 'PC memory', 'Observed']);
});

// Improvement 3 (10 Oct 2026): every graphics adapter the PC reports, the AMD card included, from the live snapshot.
const ADAPTERS = [
  {name: 'AMD Radeon RX 5700 XT', utilizationPercent: 0, memoryUsedMiB: 827, memoryTotalMiB: 8152},
  {name: 'NVIDIA GeForce RTX 3080 Ti', utilizationPercent: 0, memoryUsedMiB: 11748, memoryTotalMiB: 12288, temperatureC: 37},
];

test('every reported graphics adapter sits between the model rows and PC memory', () => {
  const view = pcLiveRows(feed({models: [model(), model({id: 'Qwen3.8-27B Q4_K_M'})], gpus: ADAPTERS,
    memory: {level: 'ok', availablePercent: 53.5}}), fresh);
  assert.deepEqual(view.rows, [
    ['gpt-oss-20b', 'Loaded · Idle · 0/2 slots busy'],
    ['Qwen3.8-27B Q4_K_M', 'Loaded · Idle · 0/2 slots busy'],
    ['AMD Radeon RX 5700 XT', '0% busy · 0.8 of 8.0 GB'],
    ['NVIDIA GeForce RTX 3080 Ti', '0% busy · 11.5 of 12.0 GB · 37 °C'],
    ['PC memory', 'Ok · 53.5% free'],
    ['Observed', '5s ago · via SharedChami files, unsigned'],
  ]);
  // The attribution stays on the panel: its title and the Observed row name the PC and the unsigned transport.
  assert.equal(view.title, 'Unsigned report from louisaurorar12');
});

test('adapters share the 30-second gate and add nothing when none are reported', () => {
  assert.equal(pcLiveRows(feed({gpus: ADAPTERS, ageSeconds: 31}), {feedFresh: true, snapshotAge: 0}), null);
  assert.equal(pcLiveRows(feed({gpus: ADAPTERS}), {feedFresh: false, snapshotAge: 0}), null);
  for (const gpus of [undefined, null, [], {}, 'RTX']) {
    assert.deepEqual(pcLiveRows(feed({gpus}), fresh).rows.map(([label]) => label), ['gpt-oss-20b', 'Observed'], JSON.stringify(gpus));
  }
  // A malformed adapter list says so rather than vanishing or inventing a card.
  assert.equal(row(pcLiveRows(feed({gpus: [{name: '<b>\u00e9</b>'}]}), fresh), 'Graphics'), 'No adapter reading');
  assert.equal(row(pcLiveRows(feed({gpus: [{name: 'AMD Radeon RX 5700 XT'}]}), fresh), 'AMD Radeon RX 5700 XT'), 'load unknown');
});

// Review finding D2 (10 Oct 2026): a guest-writable share file can carry any current timestamp, so the share path is
// an unsigned report and is never worded as live or current activity, whatever it claims.
test('the share path is never labelled live, in its title, its rows or its rates', () => {
  const claims = [feed(), feed({ageSeconds: 0, health: 'ok'}),
    feed({models: [model({activity: 'generating', slotsBusy: 2, liveTokensPerSecond: 250})], gpus: ADAPTERS,
      memory: {level: 'critical', availablePercent: 2}}),
    feed({via: 'mdns'}), feed({via: undefined})];
  for (const live of claims) {
    const view = pcLiveRows(live, fresh);
    const text = [view.title, ...view.rows.flat()].join(' | ');
    assert.ok(view.title.startsWith('Unsigned report from '), view.title);
    assert.doesNotMatch(text, /\blive\b|\bnow\b|signed\)|paired link/i, text);
    assert.match(row(view, 'Observed'), /unsigned$/);
  }
  const rated = pcLiveRows(claims[2], fresh);
  assert.equal(row(rated, 'gpt-oss-20b'), 'Loaded · Generating · 2/2 slots busy · 250 tok/s reported');
});

// Review finding A1 (10 Oct 2026): the PC's probes.memory_level emits "tight" (6-12 % free, or commit >= 95 %); the
// inspector keeps its PC memory row for every level the PC emits.
test('every PC memory level, tight included, keeps the PC memory row', () => {
  for (const [level, words] of [['ok', 'Ok'], ['watch', 'Watch'], ['tight', 'Tight'], ['critical', 'Critical'], ['unknown', 'Unknown']]) {
    assert.equal(row(pcLiveRows(feed({memory: {level, availablePercent: 10}}), fresh), 'PC memory'), `${words} · 10% free`, level);
  }
});
