// Pilot route check: worker ids come from preview-pilot/fleet-workers.json (router config).
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, writeFileSync, readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {checkRoute, loadKnownWorkers} from './preview-pilot/fleet-route.mjs';
import {programTask} from './preview-pilot/program-task.mjs';

const cfg = (worker, workerId = worker) => ({workerId, route: {worker, preanalysis: 'local', reason: 'strict-local'}});

test('router config lists the three fleet workers', () => {
  assert.deepEqual([...loadKnownWorkers()].sort(), ['dual-gpu-pc', 'm5-macbook-pro', 'monterey-mac-pro']);
});

test('every known worker id is accepted', () => {
  for (const worker of loadKnownWorkers()) assert.equal(checkRoute(cfg(worker)), worker);
});

test('each pilot uses its own manifest task', () => {
  const manifest = JSON.parse(readFileSync(new URL('./preview-pilot/program-tasks.json', import.meta.url), 'utf8'));
  for (const name of ['tiny-arithmetic.mjs', 'tiny-extraction.mjs', 'nisi-smoke.mjs']) {
    const url = new URL('./preview-pilot/' + name, import.meta.url);
    const source = readFileSync(url, 'utf8');
    assert.match(source, /const task=\{\.\.\.programTask\(import\.meta\.url\)/);
    assert.deepEqual(programTask(url), manifest.tasks[name]);
  }
  assert.throws(() => programTask(import.meta.url), /JEV_TASK_INVALID/);
});

test('child accepts the pre-analyzed route but refuses a missing judgment', () => {
  const config = cfg('dual-gpu-pc');
  Object.assign(config.route, {preanalysis: 'jev', reason: 'jev-preanalyzed'});
  assert.throws(() => checkRoute(config), /ROUTE_REFUSED/);
  const task = programTask(new URL('./preview-pilot/nisi-smoke.mjs', import.meta.url));
  config.route.jev = {
    plan: 'direct', confidence: null, underspecified: 0, jevModel: 'jev-test',
    request: {state: {task}, questions: {plan: {type: 'choice'}, underspecified: {type: 'noul'}}},
    response: {model: 'jev-test', answers: {plan: {choice: 'direct'}, underspecified: {noul: 0}}},
  };
  assert.equal(checkRoute(config), config.workerId);
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
