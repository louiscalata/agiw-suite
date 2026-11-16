import test from 'node:test';
import assert from 'node:assert/strict';
import {place} from './web/monitor.mjs';

// The PC has no built-in lane layout. Retired lane ids get no reserved place
// in the drawing; any PC lane row is drawn after the fixed machine rows.
const retired = ['pc:router', 'pc:gpt-oss-20b', 'pc:qwen3.8-27b'];

test('retired PC lanes have no reserved place on the map', () => {
  const nodes = [
    {id: 'mac', machine: 'mac', role: 'machine', label: 'Mac', health: 'ok'},
    {id: 'pc', machine: 'pc', role: 'machine', label: 'PC', health: 'not_measured'},
    ...retired.map(id => ({id, machine: 'pc', role: 'llama', label: 'Unknown lane',
                           health: 'not_measured', reason: 'unknown_lane'})),
  ];
  const placed = Object.fromEntries(place(nodes).map(node => [node.id, node]));
  assert.equal(placed.pc.y, 48);
  for (const id of retired) assert.ok(placed[id].y >= 390, id);
  assert.equal(new Set(retired.map(id => placed[id].y)).size, retired.length);
});
