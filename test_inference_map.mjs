import test from 'node:test';
import assert from 'node:assert/strict';
import {detailRows, place, rateText, tunnelLine} from './web/monitor.mjs';

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
