// Strict local mode stays honest in the pilot route check: hosted TypeSafe Jev
// evidence or labels never pass as a strict-local route (LOU-41).
import test from 'node:test';
import assert from 'node:assert/strict';
import {checkRoute} from './preview-pilot/fleet-route.mjs';
import {programTask} from './preview-pilot/program-task.mjs';

const W = 'm5-macbook-pro';
const strict = () => ({workerId: W, route: {worker: W, preanalysis: 'local', reason: 'strict-local'}});
const validJev = () => ({
  plan: 'direct', confidence: null, underspecified: 0, jevModel: 'jev-test',
  request: {state: {task: programTask(new URL('./preview-pilot/nisi-smoke.mjs', import.meta.url))},
    questions: {plan: {type: 'choice'}, underspecified: {type: 'noul'}}},
  response: {model: 'jev-test', answers: {plan: {choice: 'direct'}, underspecified: {noul: 0}}},
});

test('strict-local and correctly labelled Jev routes are both accepted', () => {
  assert.equal(checkRoute(strict()), W);
  const judged = strict();
  Object.assign(judged.route, {preanalysis: 'jev', reason: 'jev-preanalyzed', jev: validJev()});
  assert.equal(checkRoute(judged), W);
});

test('hosted Jev evidence or labels never pass as strict-local', () => {
  const variants = [
    r => { r.jev = validJev(); },
    r => { r.jev = {plan: 'direct'}; },
    r => { r.hosted = true; },
    r => { r.preanalysis = 'jev'; },
    r => { r.preanalysis = 'jev'; r.jev = validJev(); },
    r => { r.reason = 'jev-preanalyzed'; },
    r => { r.reason = 'jev-preanalyzed'; r.jev = validJev(); },
    r => { r.reason = 'jev-first'; },
    // checkRoute serves only the pilot programs and has no repo-patch exception,
    // so jev-not-applicable is refused for every route it sees.
    r => { r.reason = 'jev-not-applicable'; },
    r => { r.preanalysis = 'responses/gpt-6.2-sol'; },
  ];
  for (const mutate of variants) {
    const cfg = strict();
    mutate(cfg.route);
    assert.throws(() => checkRoute(cfg), /ROUTE_REFUSED/, JSON.stringify(cfg.route));
  }
});
