import test from 'node:test';
import assert from 'node:assert/strict';
import {detailRows, displayModelId, place, rateText, tunnelLine} from './web/monitor.mjs';

const nodes = [
  {id: 'mac', label: 'Mac', health: 'ok', generationTokensPerSecond: 3},
  {id: 'mac:lmstudio', label: 'LM Studio', health: 'ok', modelId: 'gpt-oss-20b', generationTokensPerSecond: 3},
  {id: 'pc', label: 'PC', health: 'not_measured'},
  {id: 'pc:router', label: 'Router', health: 'not_measured', reason: 'tunnel_down'},
  {id: 'pc:router:caller:codex', label: 'Codex', role: 'client', health: 'ok'},
];

test('known lanes keep a stable place and a caller is still drawn', () => {
  const placed = place(nodes);
  assert.equal(placed.find(node => node.id === 'mac:lmstudio').x, 120);
  assert.equal(placed.find(node => node.id === 'pc:router').y, 160);
  assert.equal(placed.find(node => node.id === 'pc:router:caller:codex').y, 390);
});

test('a node shows health, the reported model and tok/s', () => {
  const rows = Object.fromEntries(detailRows(nodes[1]));
  assert.equal(rows.Health, 'ok');
  assert.equal(rows.Model, 'gpt-oss-20b');
  assert.equal(rows['Generation tok/s'], '3');
  assert.equal(rateText(nodes[1]), '3 tok/s');
  assert.equal(rateText(nodes[2]), '');
});

test('tunnel status is the collector detail', () => {
  assert.equal(tunnelLine(null), '');
  assert.equal(tunnelLine({state: 'down', detail: 'SSH tunnel is down.'}), 'SSH tunnel is down.');
});

test('registry machines and their lanes share compact columns without label rows colliding', () => {
  const roots = ['mac', 'registry:pc', 'registry:macpro'].map(id => ({id, machine: id, role: 'machine',
    observationSource: id === 'mac' ? undefined : 'registry'}));
  const input = [...roots, ...['fast', 'review', 'glimmer'].map(id => ({id: `registry:pc:${id}`, machine: 'registry:pc'})),
    {id: 'registry:macpro:glimmer', machine: 'registry:macpro'},
    {id: 'mac:lmstudio', machine: 'mac'}];
  const before = JSON.stringify(input), placed = place(input);
  assert.equal(JSON.stringify(input), before);
  assert.equal(placed.length, input.length);
  assert.deepEqual(roots.map(root => placed.find(node => node.id === root.id).y), [42, 42, 42]);
  const pc = placed.filter(node => node.machine === 'registry:pc');
  assert.deepEqual(pc.map(node => node.y), [42, 114, 186, 258]);
  assert.ok(pc.slice(1).every(node => node.x === pc[0].x + 12));
  assert.ok(Math.max(...placed.map(node => node.x + 300)) <= 1000);
  assert.ok(Math.max(...placed.map(node => node.y + 44)) <= 350);
  const fourth = {id: 'registry:fourth', machine: 'registry:fourth', role: 'machine', observationSource: 'registry'};
  assert.ok(place([...input, fourth]).find(node => node.id === fourth.id).y > 300);
});

test('registry model filesystem paths use basenames while repository IDs stay intact', () => {
  for (const full of ['/models/Glimmer.gguf', 'C:\\models\\Glimmer.gguf', 'models/Glimmer.gguf']) {
    assert.equal(displayModelId(full), 'Glimmer.gguf');
    assert.equal(Object.fromEntries(detailRows({observationSource: 'registry', modelId: full})).Model, 'Glimmer.gguf');
  }
  assert.equal(displayModelId('google/gemma-4-12b-it'), 'google/gemma-4-12b-it');
  assert.equal(Object.fromEntries(detailRows({modelId: '/legacy/full.gguf'})).Model, '/legacy/full.gguf');
});
