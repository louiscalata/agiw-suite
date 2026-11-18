// Pilot route check: worker ids come from preview-pilot/fleet-workers.json (router config).
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, writeFileSync, readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {checkRoute, loadKnownWorkers} from './preview-pilot/fleet-route.mjs';

const cfg = (worker, workerId = worker) => ({workerId, route: {worker, preanalysis: 'local', reason: 'strict-local'}});

test('router config lists the three fleet workers', () => {
  assert.deepEqual([...loadKnownWorkers()].sort(), ['dual-gpu-pc', 'm5-macbook-pro', 'monterey-mac-pro']);
});

test('every known worker id is accepted', () => {
  for (const worker of loadKnownWorkers()) assert.equal(checkRoute(cfg(worker)), worker);
});

test('unknown worker ids are rejected', () => {
  for (const bad of ['unknown-box', '', undefined, 'M5-MACBOOK-PRO', 'm5-macbook-pro ']) {
    assert.throws(() => checkRoute(cfg(bad)), /ROUTE_REFUSED/);
  }
});

test('workerId must match the routed worker and the route must be strict-local', () => {
  assert.throws(() => checkRoute(cfg('monterey-mac-pro', 'm5-macbook-pro')), /ROUTE_REFUSED/);
  assert.throws(() => checkRoute({workerId: 'dual-gpu-pc', route: {worker: 'dual-gpu-pc', preanalysis: 'jev', reason: 'jev-first'}}), /ROUTE_REFUSED/);
});

test('invalid router config is refused', () => {
  const dir = mkdtempSync(join(tmpdir(), 'fleet-route-'));
  for (const body of [{schemaVersion: 1, workers: []}, {schemaVersion: 2, workers: ['m5-macbook-pro']},
    {schemaVersion: 1, workers: ['Bad Id']}, {schemaVersion: 1, workers: ['a', 'a']}]) {
    const file = join(dir, 'w.json');
    writeFileSync(file, JSON.stringify(body));
    assert.throws(() => loadKnownWorkers(pathToFileURL(file)), /ROUTER_CONFIG_INVALID/);
  }
});

test('pilot programs use the shared route check, not a hardcoded worker', () => {
  for (const program of ['nisi-smoke.mjs', 'tiny-arithmetic.mjs', 'tiny-extraction.mjs']) {
    const src = readFileSync(new URL('./preview-pilot/' + program, import.meta.url), 'utf8');
    assert.match(src, /checkRoute\(cfg\)/);
    assert.doesNotMatch(src, /'m5-macbook-pro'/);
  }
});
