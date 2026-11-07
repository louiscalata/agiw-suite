import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const board = new URL('./web/monitor-board.mjs', import.meta.url);
const boardSource = readFileSync(board, 'utf8').replace("'/monitor-tabs.mjs'", JSON.stringify(new URL('./web/monitor-tabs.mjs', import.meta.url).href));
const boardURL = `data:text/javascript;base64,${Buffer.from(boardSource).toString('base64')}`;
const source = readFileSync(new URL('./web/machines.mjs', import.meta.url), 'utf8').replace("'/monitor-board.mjs'", JSON.stringify(boardURL));
const { machineFacts, machineTiles } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');

const mac = { host: 'mac', platform: 'mac', state: 'up', reasonCode: 'observed', trust: 'local', ageSeconds: 1.4, freshForSeconds: 3,
  modelsComplete: true, loadedCount: 2, busyCount: 1, promptTokensPerSecond: 812.4, generationTokensPerSecond: 4.25, handsOff: false };
const pc = { host: 'louisaurorar12', platform: 'windows', state: 'down', reasonCode: 'reported_down', trust: 'share-reported', ageSeconds: 7,
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
  assert.equal(f.Why, 'reported down');
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
  assert.equal(f.Why, 'reported degraded');
  assert.equal(f['Models busy'], 'Unknown');
});

test('this machine is first and named as such', () => {
  const tiles = machineTiles([pc, mac], 'mac');
  assert.deepEqual(tiles.map(t => [t.title, t.word, t.glyph, t.host]), [['This Mac', 'UP', '■', 'mac'], ['PC', 'DOWN', '×', 'louisaurorar12']]);
  assert.equal(machineTiles([mac, pc], 'windows')[0].title, 'This PC');
  assert.deepEqual(machineTiles(null, 'mac'), []);
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
