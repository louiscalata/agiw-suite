// Machine detail (brief screen 2): capacity and runtime readings. Each reading
// carries its source and age; a missing one reads "Not measured" with the reason.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const board = new URL('./web/monitor-board.mjs', import.meta.url);
const boardSource = readFileSync(board, 'utf8').replace("'/monitor-tabs.mjs'", JSON.stringify(new URL('./web/monitor-tabs.mjs', import.meta.url).href));
const boardURL = `data:text/javascript;base64,${Buffer.from(boardSource).toString('base64')}`;
const source = readFileSync(new URL('./web/machines.mjs', import.meta.url), 'utf8').replace("'/monitor-board.mjs'", JSON.stringify(boardURL))
  .replace("'/fleet-state.mjs'", JSON.stringify(new URL('./web/fleet-state.mjs', import.meta.url).href))
  .replace("'/monitor-store.mjs'", JSON.stringify(new URL('./web/monitor-store.mjs', import.meta.url).href));
const {machineCapacity, machineTiles, laneFacts, paintMachines} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const shared = JSON.parse(readFileSync(new URL('./test_support/fleet-state.json', import.meta.url), 'utf8'));

const GiB = 2 ** 30, now = 1794484800;
const rows = group => Object.fromEntries(group.rows);
const group = (groups, title) => groups.find(item => item.title === title);
const NOT_MEASURED = /^Not measured · ./;
const macSnapshot = (over = {}) => ({schemaVersion: 1, host: 'mac', sampledAt: now - 1, fullSampledAt: now - 1,
  sources: [{id: 'mac-gpu', state: 'live', ageSeconds: 0, detail: 'GPU utilisation and memory sampled from ioreg'},
    {id: 'mem-guard', state: 'live', ageSeconds: 0, detail: 'Memory ok'}],
  memory: {level: 'ok', reasons: [], pressure: 'normal', availablePercent: 62, ramBytes: 64 * GiB, swapUsedBytes: 0,
    swapTotalBytes: 0, consumers: [], paused: [], suggestions: [], sampledAt: now - 1},
  macGpu: {model: 'Apple M4 Max', cores: 40, utilizationPercent: 12, allocatedBytes: 20 * GiB, inUseBytes: 18 * GiB, ageSeconds: 0,
    unifiedMemoryCeilingBytes: 48 * GiB, unifiedMemoryCeilingSource: 'Metal recommendedMaxWorkingSetSize (read by the app shell)'},
  windowsWorker: {state: 'advertised', ageSeconds: 4, gpus: [
    {index: 1, name: 'AMD Radeon RX 580', utilizationPercent: 3, memoryUsedMiB: 512, memoryTotalMiB: 8192, temperatureC: 40, powerW: 30},
    {index: 0, name: 'NVIDIA GeForce RTX 3080', utilizationPercent: 35, memoryUsedMiB: 8192, memoryTotalMiB: 10240, temperatureC: 61, powerW: 220}]},
  ...over});

test('memory, the Mac GPU and each PC GPU are separate readings with their source and age', () => {
  const groups = machineCapacity(macSnapshot(), {fresh: true, now});
  assert.deepEqual(groups.map(item => item.title), ['This Mac · memory', 'This Mac · GPU', 'Windows PC · GPU 0 · NVIDIA GeForce RTX 3080',
    'Windows PC · GPU 1 · AMD Radeon RX 580', 'Fleet capacity']);
  assert.deepEqual(rows(groups[0]), {Memory: '62% available · pressure normal', 'Installed memory': '64.0 GB',
    Source: 'Memory guard · macOS memory pressure', Age: '1 s ago'});
  assert.deepEqual(rows(groups[1]), {GPU: 'Apple M4 Max · 40 cores', 'GPU load · all apps': '12%',
    'GPU memory in use': '18.0 GB of shared memory',
    'Metal ceiling': '48.0 GB of shared (unified) memory, not dedicated VRAM · Metal recommendedMaxWorkingSetSize (read by the app shell)',
    Source: 'ioreg IOAccelerator', Age: '1 s ago'});
  assert.deepEqual(rows(groups[2]), {'GPU load · all apps': '35%', 'Dedicated memory': '8.0 of 10.0 GB', Temperature: '61 °C', Power: '220 W',
    Source: 'Windows worker heartbeat (relayed)', Age: '5 s ago'});
  assert.equal(rows(groups[3])['Dedicated memory'], '0.5 of 8.0 GB');
});

test('GPU assignment and archive capacity read NOT AVAILABLE YET', () => {
  for (const snapshot of [macSnapshot(), null, {}]) {
    const fleet = group(machineCapacity(snapshot, {fresh: true, now}), 'Fleet capacity');
    assert.match(rows(fleet)['GPU assignment'], /^NOT AVAILABLE YET · /);
    assert.match(rows(fleet)['Archive capacity'], /^NOT AVAILABLE YET · /);
  }
});

test('a null or unreadable reading is Not measured with the reason, never zero', () => {
  const unknownMemory = rows(group(machineCapacity(macSnapshot({memory: {level: 'unknown', reasons: ['memory probes failed'], availablePercent: null, sampledAt: now - 1}}), {fresh: true, now}), 'This Mac · memory'));
  assert.equal(unknownMemory.Memory, 'Not measured · memory probes failed');
  assert.equal(unknownMemory['Installed memory'], undefined, 'no figure from an unknown block');
  assert.equal(rows(group(machineCapacity(macSnapshot({memory: {...macSnapshot().memory, availablePercent: null}}), {fresh: true, now}), 'This Mac · memory')).Memory, 'pressure normal', 'a valid level with a null availablePercent shows no percentage');
  const noGpu = rows(group(machineCapacity(macSnapshot({macGpu: null, sources: [{id: 'mac-gpu', state: 'unavailable', detail: 'ioreg unavailable'}]}), {fresh: true, now}), 'This Mac · GPU'));
  assert.equal(noGpu.GPU, 'Not measured · ioreg unavailable');
  assert.equal(noGpu['Metal ceiling'], 'Not measured · not reported in this sample');
  // The Metal ceiling is read once by the app shell and survives an unavailable ioreg sample.
  const ceilingOnly = rows(group(machineCapacity(macSnapshot({macGpu: {unifiedMemoryCeilingBytes: 48 * GiB, unifiedMemoryCeilingSource: 'Metal recommendedMaxWorkingSetSize (read by the app shell)'}}), {fresh: true, now}), 'This Mac · GPU'));
  assert.match(ceilingOnly.GPU, NOT_MEASURED);
  assert.match(ceilingOnly['Metal ceiling'], /^48\.0 GB of shared/);
  const noCeiling = rows(group(machineCapacity(macSnapshot({macGpu: {...macSnapshot().macGpu, unifiedMemoryCeilingBytes: null, unifiedMemoryCeilingSource: 'unknown: not passed in by the app shell'}}), {fresh: true, now}), 'This Mac · GPU'));
  assert.equal(noCeiling['Metal ceiling'], 'Not measured · unknown: not passed in by the app shell');
  const partial = rows(group(machineCapacity(macSnapshot({macGpu: {...macSnapshot().macGpu, utilizationPercent: null, inUseBytes: -1}}), {fresh: true, now}), 'This Mac · GPU'));
  assert.equal(partial['GPU load · all apps'], 'Not measured · not reported by ioreg');
  assert.equal(partial['GPU memory in use'], 'Not measured · not reported by ioreg');
  // A stale sample, a stale heartbeat and rejected heartbeat rows.
  for (const title of ['This Mac · memory', 'This Mac · GPU', 'Windows PC · GPUs']) {
    const stale = group(machineCapacity(macSnapshot(), {fresh: false, now}), title);
    assert.ok(stale, title);
    assert.match(stale.rows[0][1], /^Not measured · the sample is not live$/, title);
    assert.equal(rows(stale).Age, 'Unknown');
  }
  const oldBeat = rows(group(machineCapacity(macSnapshot({windowsWorker: {state: 'advertised', ageSeconds: 90, gpus: macSnapshot().windowsWorker.gpus}}), {fresh: true, now}), 'Windows PC · GPUs'));
  assert.equal(oldBeat.GPUs, 'Not measured · no fresh worker heartbeat (91 s old)');
  const silent = rows(group(machineCapacity(macSnapshot({windowsWorker: {state: 'advertised', ageSeconds: 2, gpus: null}}), {fresh: true, now}), 'Windows PC · GPUs'));
  assert.equal(silent.GPUs, 'Not measured · not reported by the worker heartbeat');
  const bad = macSnapshot().windowsWorker.gpus.map((gpu, index) => index ? {...gpu, memoryUsedMiB: 20000} : gpu);
  const rejected = rows(group(machineCapacity(macSnapshot({windowsWorker: {state: 'advertised', ageSeconds: 2, gpus: bad}}), {fresh: true, now}), 'Windows PC · GPUs'));
  assert.equal(rejected.GPUs, 'Not measured · heartbeat GPU rows failed validation');
  assert.equal(machineCapacity(macSnapshot({windowsWorker: undefined}), {fresh: true, now}).some(item => item.title.startsWith('Windows PC')), false,
    'no PC block without a PC worker in the sample');
  // A future reading is clock skew, not a fresh value.
  assert.match(rows(group(machineCapacity(macSnapshot({memory: {...macSnapshot().memory, sampledAt: now + 30}}), {fresh: true, now}), 'This Mac · memory')).Memory,
    /^Not measured · reading time is in the future/);
});

test('on the Windows edition each PC GPU is read from its own counters', () => {
  const groups = machineCapacity({schemaVersion: 1, host: 'windows', sampledAt: now - 2, sources: [],
    pcGpus: [{name: 'NVIDIA GeForce RTX 3080', utilizationPercent: 20, memoryUsedMiB: 1024, memoryTotalMiB: 10240, temperatureC: null, powerW: null},
      {name: 'AMD Radeon RX 580', utilizationPercent: null, memoryUsedMiB: null, memoryTotalMiB: null, temperatureC: null, powerW: null}]}, {fresh: true, now});
  assert.deepEqual(groups.map(item => item.title), ['This PC · GPU · NVIDIA GeForce RTX 3080', 'This PC · GPU · AMD Radeon RX 580', 'Fleet capacity']);
  assert.deepEqual(rows(groups[0]), {'GPU load · all apps': '20%', 'Dedicated memory': '1.0 of 10.0 GB', Temperature: 'Not measured · not reported',
    Power: 'Not measured · not reported', Source: 'This PC GPU counters', Age: '2 s ago'});
  assert.equal(rows(groups[1])['GPU load · all apps'], 'Not measured · not reported');
  assert.equal(rows(groups[1])['Dedicated memory'], 'Not measured · not reported');
});

test('lane runtime readings come only from answering lanes, with each machine\'s coverage', () => {
  const snapshot = structuredClone(shared.snapshot);
  const facts = structuredClone(shared.fleetState.facts);
  const local = facts.find(fact => fact.id === 'mac:lmstudio');
  Object.assign(local.evidence, {promptTokensPerSecond: 812.4, generationTokensPerSecond: 4.25, slotsBusy: 1, slotsTotal: 2,
    contextUsed: 4096, contextLimit: 32768, queueDepth: 0});
  snapshot.fleetState = {...shared.fleetState, facts};
  const tiles = machineTiles(snapshot, 'mac', {now: shared.now});
  const mac = tiles.find(tile => tile.key === 'mac'), pc = tiles.find(tile => tile.key === 'registry:pc');
  assert.equal(laneFacts(mac.lanes[0]).runtime, 'prompt 812 tok/s · writing 4.3 tok/s · slots 1 of 2 busy · context 4096 of 32768 tokens · queue 0');
  assert.equal(Object.fromEntries(mac.facts)['Runtime readings'], '1 of 1 lane reports runtime readings');
  for (const lane of pc.lanes) assert.equal(laneFacts(lane).runtime, 'Not measured · the registry probe reads model identity only');
  assert.equal(Object.fromEntries(pc.facts)['Runtime readings'], '0 of 2 lanes report runtime readings');
  // A lane that is not answering shows no retained reading.
  const stale = {...local, state: 'stale'};
  assert.equal(laneFacts(stale).runtime, 'Not measured · lane evidence stale');
  assert.equal(laneFacts({...local, state: 'endpoint unavailable'}).runtime, 'Not measured · endpoint not answering');
  assert.equal(laneFacts({...local, state: 'unknown'}).runtime, 'Not measured · lane identity unknown');
  assert.equal(laneFacts({...local, evidence: {healthScope: 'x'}}).runtime, 'Not measured · not reported by this endpoint');
  assert.equal(laneFacts({...local, evidence: {promptTokensPerSecond: -1, slotsBusy: 3, slotsTotal: 2}}).runtime, 'Not measured · not reported by this endpoint',
    'negative or inconsistent numbers are not readings');
});

test('the page paints the capacity block with plain words and makes no request', () => {
  const make = tag => ({tag, children: [], className: '', attrs: {}, textContent: '', setAttribute(k, v) { this.attrs[k] = v; },
    append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; }});
  const els = {};
  const document = {getElementById: id => (els[id] ||= make('div')), createElement: make};
  const original = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async () => { requests++; throw new Error('no request expected'); };
  try { paintMachines(document, macSnapshot(), {fresh: true, now}); } finally { globalThis.fetch = original; }
  assert.equal(requests, 0);
  const text = node => node.textContent + node.children.map(text).join(' ');
  const capacity = els.machinesBody.children.find(node => node.className === 'j-machine-capacity');
  assert.ok(capacity, 'capacity section painted');
  assert.equal(capacity.attrs['aria-labelledby'], 'machinesCapacityHeading');
  assert.match(text(capacity), /Capacity and runtime readings/);
  assert.match(text(capacity), /Windows PC · GPU 0 · NVIDIA GeForce RTX 3080/);
  assert.match(text(capacity), /NOT AVAILABLE YET/);
});
