import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {programTask} from './preview-pilot/program-task.mjs';

const vendor = new URL('./preview-pilot/vendor/nisi-0.3.1/', import.meta.url);
const advisor = new URL('adapters/jev-advisor.mjs', vendor);
const options = {skip: existsSync(advisor) ? false : 'pinned Nisi checkout missing'};
const smoke = new URL('./preview-pilot/nisi-smoke.mjs', import.meta.url);

test('replay compares canonical requests, refusing changes to either part', options, async () => {
  const {replayJudge} = await import('./preview-pilot/jev-replay.mjs');
  const request = {state: {a: 1, b: 2}, questions: {plan: {type: 'choice'}}};
  const response = {model: 'jev-test', answers: {plan: {choice: 'reason'}}};
  const judge = replayJudge({jev: {request, response}});
  assert.equal(await judge({questions: request.questions, state: {b: 2, a: 1}}), response);
  for (const mismatch of [{...request, state: {}}, {...request, questions: {}}, {}]) {
    await assert.rejects(judge(mismatch), {code: 'JEV_REPLAY_MISMATCH'});
  }
});

test('smoke replays the adapter request and refuses a different task before workflow', options, async t => {
  const {planWithJev} = await import(advisor);
  const {programPlan} = await import('./preview-pilot/jev-replay.mjs');
  const task = programTask(smoke);
  const response = {model: 'jev-test', answers: {plan: {choice: 'direct'}, underspecified: {noul: 0}}};
  let request;
  const planned = await planWithJev(task, {judge: async req => { request = structuredClone(req); return response; }});
  const summary = {plan: 'direct', confidence: null, underspecified: 0, jevModel: 'jev-test'};
  const route = {worker: 'dual-gpu-pc', preanalysis: 'jev', reason: 'jev-preanalyzed', jev: {...summary, request, response}};
  const reportStore = mkdtempSync(join(tmpdir(), 'jev-replay-'));
  t.after(() => rmSync(reportStore, {force: true, recursive: true}));
  const cfg = {mode: 'fixture', endpoint: 'http://127.0.0.1:9/v1/chat/completions', model: 'fixture-only',
    maxTokens: 4096, workerId: route.worker, route, reportStore};
  assert.equal((await programPlan(cfg, task, 4096)).maxTokens, 1024);
  assert.equal((await programPlan(cfg, task, 64)).maxTokens, 64);
  const run = () => spawnSync(process.execPath, [fileURLToPath(smoke)], {input: JSON.stringify(cfg), encoding: 'utf8'});
  const ok = run();
  assert.equal(ok.status, 0);
  assert.deepEqual(JSON.parse(ok.stdout).jevPlan, {...summary, requestSha256: planned.evidence.requestSha256});

  request.state.task.taskId += '-changed';
  const failed = run();
  assert.equal(failed.status, 1);
  const result = JSON.parse(failed.stdout);
  assert.equal(result.code, 'JEV_PLAN_REFUSED');
  assert.equal(result.report, null);
  assert.equal(result.modelCalls, 0);
});
