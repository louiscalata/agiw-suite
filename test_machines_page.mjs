import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const board = new URL('./web/monitor-board.mjs', import.meta.url);
const boardSource = readFileSync(board, 'utf8').replace("'/monitor-tabs.mjs'", JSON.stringify(new URL('./web/monitor-tabs.mjs', import.meta.url).href));
const boardURL = `data:text/javascript;base64,${Buffer.from(boardSource).toString('base64')}`;
const source = readFileSync(new URL('./web/machines.mjs', import.meta.url), 'utf8').replace("'/monitor-board.mjs'", JSON.stringify(boardURL)).replace("'/fleet-state.mjs'", JSON.stringify(new URL('./web/fleet-state.mjs', import.meta.url).href));
const { machineFacts, machineTiles, mountMachines, paintMachines, laneFacts } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');

const mac = { host: 'mac', platform: 'mac', state: 'up', reasonCode: 'observed', trust: 'local', ageSeconds: 1.4, freshForSeconds: 3,
  modelsComplete: true, loadedCount: 2, busyCount: 1, promptTokensPerSecond: 812.4, generationTokensPerSecond: 4.25, handsOff: false };
const pc = { host: 'worker-pc', platform: 'windows', state: 'down', reasonCode: 'reported_down', trust: 'share-reported', ageSeconds: 7,
  freshForSeconds: 30, modelsComplete: false, loadedCount: null, busyCount: null, promptTokensPerSecond: null, generationTokensPerSecond: null, handsOff: null };
const facts = row => Object.fromEntries(machineFacts(row));

test('an observed machine lists its measured values', () => {
  assert.deepEqual(facts(mac), {
    State: 'Up', Why: 'Observed', 'Seen through': 'local', Age: '1 s', 'Fresh for': '3 s',
    'Models loaded': '2', 'Models busy': '1', 'Reading prompts': '812 tok/s', Writing: '4.3 tok/s', 'Hands off': 'No',
  });
});

test('missing values are not measured or unknown, never zero', () => {
  const f = facts(pc);
  assert.equal(f.State, 'Down');
  assert.equal(f.Why, 'Reported down');
  assert.equal(f.Models, 'Model list incomplete');
  assert.equal(f['Models loaded'], undefined);
  assert.equal(f['Reading prompts'], 'Not measured');
  assert.equal(f.Writing, 'Not measured');
  assert.equal(f['Hands off'], 'Unknown');
  assert.deepEqual(facts({}), { State: 'Unknown', Why: 'Not observed', 'Seen through': 'Unknown', Age: 'Unknown', 'Fresh for': 'Unknown',
    Models: 'Model list incomplete', 'Reading prompts': 'Not measured', Writing: 'Not measured', 'Hands off': 'Unknown' });
});

test('degraded is up but says so; a busy count may be unknown', () => {
  const f = facts({ ...mac, reasonCode: 'reported_degraded', busyCount: null });
  assert.equal(f.State, 'Up, degraded');
  assert.equal(f.Why, 'Reported degraded');
  assert.equal(f['Models busy'], 'Unknown');
});

test('this machine is first and named as such', () => {
  const tiles = machineTiles([pc, mac], 'mac');
  assert.deepEqual(tiles.map(t => [t.title, t.word, t.glyph, t.host]), [['This Mac', 'UP', '■', 'mac'], ['PC', 'DOWN', '×', 'worker-pc']]);
  assert.equal(machineTiles([mac, pc], 'windows')[0].title, 'This PC');
  assert.deepEqual(machineTiles(null, 'mac'), []);
});

test('a paired machine named by its UUID shows its label, or the host it reported marked as reported, never silently the reported host', () => {
  const uuid = '3f2b8c1e-5d4a-4b7e-9a10-6c2d7e8f9a0b';
  const row = { host: uuid, platform: 'windows', state: 'up', reasonCode: 'observed', trust: 'paired' };
  assert.equal(machineTiles([{ ...row, label: 'WORKER-PC (reported)', reportedHost: 'WORKER-PC' }], 'mac')[0].host, 'WORKER-PC (reported)');
  assert.equal(machineTiles([{ ...row, reportedHost: 'WORKER-PC' }], 'mac')[0].host, 'WORKER-PC (reported)');
  assert.equal(machineTiles([{ ...row, label: uuid }], 'mac')[0].host, uuid, 'a label that is the UUID again adds nothing');
  assert.equal(machineTiles([row], 'mac')[0].host, uuid, 'nothing better to show: keep the identifier');
  assert.equal(machineTiles([{ ...row, label: '<b>x</b>', reportedHost: '<i>' }], 'mac')[0].host, uuid, 'unsafe words never reach the page');
  assert.equal(machineTiles([{ host: 'mac', platform: 'mac', label: 'Other', reportedHost: 'Other' }], 'mac')[0].host, 'mac', 'a normal host is not replaced');
});

test('unsafe strings do not reach the page', () => {
  const [tile] = machineTiles([{ host: '<img src=x>', platform: 'other', state: 'weird', reasonCode: 'a<b', trust: 'x"y' }], 'mac');
  assert.equal(tile.title, 'Machine');
  assert.equal(tile.host, null);
  assert.equal(tile.word, 'UNKNOWN');
  const f = Object.fromEntries(tile.facts);
  assert.equal(f.Why, 'Not observed');
  assert.equal(f['Seen through'], 'Unknown');
});

test('every full-web page links Machines and both servers serve it', () => {
  for (const page of ['index', 'monitor', 'components', 'identity', 'machines']) assert.match(read(`./web/${page}.html`), /<a href="\/machines"( aria-current="page")?>Machines<\/a>/);
  assert.match(read('./web/machines.html'), /<a href="\/machines" aria-current="page">Machines<\/a>/);
  assert.match(read('./server.py'), /'\/machines': \('machines\.html'/);
  assert.match(read('./windows/agiw_win/server.py'), /"\/machines\.mjs": \("machines\.mjs"/);
  assert.match(read('./wrapper-patch/web-edition/appstore-rules.mjs'), /<a href="\/machines">Machines<\/a>\\n/);
});

test('a never-heard peer and a silent peer give different reasons', () => {
  assert.equal(facts({ state: 'unknown', reasonCode: 'not_observed' }).Why, 'Never observed');
  assert.equal(facts({ state: 'unknown', reasonCode: 'not_connected' }).Why, 'Went silent');
  assert.equal(facts({ state: 'unknown', reasonCode: 'share_unmounted' }).Why, 'Share not mounted');
});

// The PC sends hyphenated reasons and platform 'macos' today; the backend is adding the Mac's fields on top (a superset).
const pcRow = { id: 'pc', name: 'PC', platform: 'windows', label: 'This PC', reason: 'serving', state: 'up', trust: 'local', ageSeconds: 2, loadedCount: 1 };
const pcSuperset = { ...pcRow, host: 'worker-pc', reasonCode: 'observed', freshForSeconds: 30, modelsComplete: true, busyCount: 0,
  promptTokensPerSecond: null, generationTokensPerSecond: null };
const macOnPc = { id: 'mac', name: 'Mac', platform: 'macos', label: 'Mac', reason: 'stale', state: 'unknown', trust: 'relayed', ageSeconds: 70 };
const macOnPcSuperset = { ...macOnPc, host: 'desk-mac', platform: 'mac', reasonCode: 'stale', freshForSeconds: 30, modelsComplete: false, busyCount: null };

test('a PC-sent row set reads as tiles, with the Mac named even when it says macos', () => {
  for (const rows of [[pcRow, macOnPc], [pcSuperset, macOnPcSuperset], [pcSuperset, macOnPc]]) {
    const tiles = machineTiles(rows, 'windows');
    assert.deepEqual(tiles.map(t => t.title), ['This PC', 'Mac']);
    assert.deepEqual(tiles.map(t => t.word), ['UP', 'UNKNOWN']);
  }
  const superset = Object.fromEntries(machineFacts(pcSuperset));
  assert.equal(superset.Why, 'Observed');
  assert.equal(superset['Models loaded'], '1');
  assert.equal(superset['Models busy'], '0');
  assert.equal(superset['Reading prompts'], 'Not measured');
  assert.equal(superset['Fresh for'], '30 s');
  const old = Object.fromEntries(machineFacts(pcRow));
  assert.equal(old.Why, 'Observed', 'an up row is observed whichever reason words it carries');
  assert.equal(old.Models, 'Model list incomplete', 'a count with no completeness flag is never shown');
  assert.equal(old['Models loaded'], undefined);
  const stale = Object.fromEntries(machineFacts(macOnPcSuperset));
  assert.equal(stale.Why, 'Reading too old');
  assert.equal(machineTiles([pcSuperset], 'worker-pc')[0].title, 'This PC');
});

test('the page judges freshness by fullSampledAt within 3 seconds', async () => {
  const run = async (body, now = 1000) => {
    const els = {};
    const make = () => ({ children: [], className: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, append(...c) { this.children.push(...c); },
      replaceChildren(...c) { this.children = c; }, textContent: '' });
    const document = { getElementById: id => (els[id] ||= make()), createElement: () => make() };
    const handle = mountMachines({ document, fetch: async () => ({ ok: true, json: async () => body }), setTimeout: () => 1, now: () => now });
    for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
    handle.destroy();
    return els.machinesStatus.textContent;
  };
  assert.equal(await run({ host: 'mac', sampledAt: 999, fullSampledAt: 999 }), 'Live sample.');
  assert.equal(await run({ host: 'mac', sampledAt: 999 }), 'Live sample.');
  assert.equal(await run({ host: 'mac', sampledAt: 999, fullSampledAt: 990 }), 'Last sample, not live.');
  assert.equal(await run({ host: 'mac', sampledAt: 996 }), 'Last sample, not live.');
});

test('a machine that is up but degraded is a DEGRADED tile, whichever reason words say so', () => {
  const base = { host: 'pc', platform: 'windows', state: 'up', trust: 'local', ageSeconds: 1, freshForSeconds: 30, modelsComplete: true, loadedCount: 1, busyCount: 0 };
  for (const row of [{ ...base, reasonCode: 'reported_degraded' }, { ...base, reason: 'partly-serving' }, { ...base, reasonCode: null, reason: 'partly-serving' }]) {
    const [tile] = machineTiles([row], 'windows');
    assert.deepEqual([tile.word, tile.glyph], ['DEGRADED', '◐'], JSON.stringify(row));
    const f = Object.fromEntries(machineFacts(row));
    assert.equal(f.State, 'Up, degraded');
    assert.equal(f.Why, 'Reported degraded');
  }
  // The shared word wins over the older one, and a degraded word on a machine that is not up is not a claim about an up machine.
  assert.equal(machineTiles([{ ...base, reasonCode: 'observed', reason: 'partly-serving' }], 'windows')[0].word, 'UP');
  assert.equal(machineTiles([{ ...base, state: 'down', reasonCode: 'reported_degraded' }], 'windows')[0].word, 'DOWN');
  assert.equal(machineTiles([{ ...base, state: 'unknown', reason: 'partly-serving' }], 'windows')[0].word, 'UNKNOWN');
  assert.equal(machineTiles([{ ...base, reason: 'serving' }], 'windows')[0].word, 'UP');
});

test('names and states come from the tables by own key only', () => {
  for (const word of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf']) {
    const [tile] = machineTiles([{ host: 'x', platform: word, state: word }], 'mac');
    assert.equal(tile.title, 'x', word);
    assert.equal(tile.word, 'UNKNOWN', word);
    assert.equal(tile.glyph, '?', word);
    assert.equal(Object.fromEntries(machineFacts({ state: word })).State, 'Unknown', word);
  }
});

test('a timestamp that is not a number never makes the page live', async () => {
  const run = async stamp => {
    const els = {};
    const make = () => ({ children: [], className: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, append(...c) { this.children.push(...c); },
      replaceChildren(...c) { this.children = c; }, textContent: '' });
    const document = { getElementById: id => (els[id] ||= make()), createElement: () => make() };
    const handle = mountMachines({ document, fetch: async () => ({ ok: true, json: async () => ({ host: 'mac', ...stamp }) }), setTimeout: () => 1, now: () => 1000 });
    for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
    handle.destroy();
    return els.machinesStatus.textContent;
  };
  for (const stamp of ['999', [999], true, null, {}, '', NaN, Infinity]) {
    assert.equal(await run({ sampledAt: stamp }), 'Last sample, not live.', JSON.stringify(stamp));
    if (stamp !== null) assert.equal(await run({ fullSampledAt: stamp, sampledAt: 999 }), 'Last sample, not live.', `full ${JSON.stringify(stamp)}`);
  }
  assert.equal(await run({ sampledAt: 999 }), 'Live sample.');
  assert.equal(await run({ fullSampledAt: null, sampledAt: 999 }), 'Live sample.', 'a null full sample time is absent, so sampledAt is read');
});

const registryRow = (id, label, count = 1) => ({host: `registry:${id}`, id: `registry:${id}`, label,
  platform: 'remote', observationSource: 'registry', state: 'up', health: 'ok', freshForSeconds: 6,
  lanes: Array.from({length: count}, (_, i) => ({id: `registry:${id}:lane${i}`, label: `Lane ${i}`, health: 'ok',
    modelId: i ? 'ornith-ai/ornith-1.5-9b' : 'C:\\models\\glimmer.gguf', ageSeconds: 1,
    observedAt: new Date(Date.now() - 1000).toISOString()}))});

test('registry machine cards retain machine labels, lane identities and unknown states', () => {
  const pcRegistry = {...registryRow('pc', 'PC', 3), replaces: 'legacy-pc'};
  const pro = registryRow('macpro', 'Mac Pro');
  const tiles = machineTiles([mac, pcRegistry, pro], 'mac');
  assert.deepEqual(tiles.map(tile => [tile.title, tile.word]), [['This Mac', 'UP'], ['PC', 'UP'], ['Mac Pro', 'UP']]);
  assert.equal(tiles[1].lanes.length, 3);
  assert.equal(Object.fromEntries(tiles[1].facts).Lanes, '3 lanes up');
  assert.equal(Object.fromEntries(tiles[1].facts)['Models loaded'], undefined, 'lane identities are not loaded model counts');
  const unknown = {...pro, lanes: pro.lanes.map(lane => ({...lane, health: 'not_measured', modelId: null, reason: 'timeout'}))};
  const [tile] = machineTiles([unknown], 'mac');
  assert.equal(tile.word, 'UNKNOWN');
  assert.equal(laneFacts(tile.lanes[0]).health, 'UNKNOWN');
  assert.equal(laneFacts(tile.lanes[0]).reason, 'Timed out');
  assert.equal(machineTiles([pcRegistry], 'mac', {fresh: false})[0].word, 'UNKNOWN');
});

test('lane model paths show the filename while repository IDs stay intact', () => {
  for (const modelId of ['/models/author.gguf', 'C:\\models\\author.gguf', '~/models/author.gguf', 'models/author.gguf']) {
    const facts = laneFacts({label: 'Author', modelId, health: 'ok', ageSeconds: 2});
    assert.equal(facts.model.label, 'author.gguf');
    assert.equal(facts.model.title, modelId);
    assert.equal(facts.seen, '2 s ago');
  }
  assert.equal(laneFacts({modelId: 'google/gemma-4-12b-it'}).model.label, 'google/gemma-4-12b-it');
  assert.equal(laneFacts({}).health, 'UNKNOWN');
  assert.equal(laneFacts({}).seen, 'Unknown');
  assert.equal(laneFacts({observedAt: '2026-11-12T20:30:00Z'}).seen, '2026-11-12T20:30:00Z', 'a local lane timestamp remains visible without an age');
  assert.equal(laneFacts({observedAt: 'invalid'}).seen, 'Unknown');
  assert.equal(laneFacts({reason: 'invalid_utf8'}).reason, 'Invalid utf8');
  assert.equal(laneFacts({reason: 'upstream_error'}).reason, 'Upstream error');
  assert.equal(laneFacts({reason: '<script>'}).reason, 'Not observed');
});

const shared = JSON.parse(read('./test_support/fleet-state.json'));

test('Machines DOM reads membership and state words from the shared fleet fixture', () => {
  const els = {};
  const make = tag => ({tag, children: [], attrs: {}, textContent: '', className: '',
    setAttribute(k, v) { this.attrs[k] = v; }, append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; }});
  const document = {createElement: make, getElementById: id => (els[id] ||= make('div'))};
  const snapshot = {...shared.snapshot, fleetState: shared.fleetState};
  paintMachines(document, snapshot, {fresh: true, now: shared.now});
  const flatten = node => [node, ...node.children.flatMap(flatten)];
  const all = flatten(els.machinesBody);
  assert.equal(all.filter(node => node.className === 'j-machine').length, 4);
  assert.equal(all.filter(node => node.className === 'j-machine-lane').length, 4);
  assert.deepEqual(machineTiles(snapshot, 'mac', {now: shared.now}).map(tile => tile.key),
    shared.fleetState.facts.filter(fact => fact.scope === 'machine').map(fact => fact.id));
  assert.ok(all.some(node => node.textContent === 'endpoint answering'));
  assert.ok(all.some(node => node.textContent === 'Configured Mac Pro'));
  assert.ok(all.some(node => node.textContent === 'Unmatched peer'));
  assert.ok(all.some(node => node.textContent === 'Configured endpoint: http://127.0.0.1:21003'));
  assert.ok(all.some(node => node.textContent === 'Last successful probe: Unknown'));
  assert.ok(all.some(node => node.textContent === 'Last probe: 1 s ago'));
});

test('Machines expires each fact independently while keeping membership and unknown counts', () => {
  const snapshot = {...shared.snapshot, fleetState: shared.fleetState};
  const tiles = machineTiles(snapshot, 'mac', {now: shared.now + 4});
  assert.equal(tiles.length, 4);
  assert.match(tiles[0].word, /^stale · 5 s ago$/);
  const pc = tiles.find(tile => tile.key === 'registry:pc');
  assert.equal(pc.word, 'unknown');
  assert.equal(pc.lanes.every(lane => lane.state === 'endpoint answering'), true);
  assert.equal(Object.fromEntries(pc.facts)['Models loaded'], 'Unknown');
  assert.equal(Object.fromEntries(tiles.find(tile => tile.key === 'peer:spare').facts)['Models busy'], 'Unknown');
  assert.deepEqual(machineTiles({machines: shared.snapshot.machines}, 'mac'), [], 'no alternate inventory when the contract is unavailable');
});
