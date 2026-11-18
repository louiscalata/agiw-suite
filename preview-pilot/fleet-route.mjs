// Shared route check for the pilot programs. Known worker ids come from the router
// config (fleet-workers.json, kept equal to agiw.fleet_routing_plan.WORKERS by tests).
import {readFileSync} from 'node:fs';

const ID = /^[a-z0-9][a-z0-9-]{0,62}$/;
const record = obj => obj !== null && typeof obj === 'object' && !Array.isArray(obj);
const fields = (obj, ...keys) => record(obj) && Object.keys(obj).length === keys.length && keys.every(k => Object.hasOwn(obj, k));
const probability = n => Number.isFinite(n) && n >= 0 && n <= 1;

export function loadKnownWorkers(url = new URL('./fleet-workers.json', import.meta.url)) {
  const config = JSON.parse(readFileSync(url, 'utf8'));
  const workers = config?.workers;
  if (config?.schemaVersion !== 1 || !Array.isArray(workers) || workers.length === 0 ||
      !workers.every(w => typeof w === 'string' && ID.test(w)) || new Set(workers).size !== workers.length) {
    throw Error('ROUTER_CONFIG_INVALID');
  }
  return Object.freeze([...workers]);
}

export function checkRoute(cfg, known = loadKnownWorkers()) {
  const worker = cfg?.route?.worker;
  const route = cfg?.route;
  const local = route?.preanalysis === 'local' && route.reason === 'strict-local' && fields(route, 'worker', 'preanalysis', 'reason');
  // The worker already checked the request against this program's manifest and questions.
  const j = route?.jev, answers = j?.response?.answers;
  const judged = route?.preanalysis === 'jev' && route.reason === 'jev-preanalyzed' &&
    fields(route, 'worker', 'preanalysis', 'reason', 'jev') &&
    fields(j, 'plan', 'confidence', 'underspecified', 'jevModel', 'request', 'response') &&
    fields(j.response, 'model', 'answers') && fields(answers, 'plan', 'underspecified') &&
    ['direct', 'reason'].includes(j?.plan) && j.plan === answers?.plan?.choice &&
    typeof j.jevModel === 'string' && /^[A-Za-z0-9._-]{1,128}$/.test(j.jevModel) && j.jevModel === j.response.model &&
    (j.confidence === null ? fields(answers.plan, 'choice') :
      fields(answers.plan, 'choice', 'confidence') && probability(j.confidence) && j.confidence === answers.plan.confidence) &&
    fields(answers.underspecified, 'noul') && probability(j.underspecified) && j.underspecified < 0.5 &&
    j.underspecified === answers.underspecified.noul && fields(j.request, 'state', 'questions') &&
    fields(j.request.state, 'task') && record(j.request.state.task) && fields(j.request.questions, 'plan', 'underspecified') &&
    j.request.questions.plan?.type === 'choice' && j.request.questions.underspecified?.type === 'noul';
  if (typeof worker !== 'string' || !known.includes(worker) || cfg.workerId !== worker ||
      !(local || judged)) {
    throw Error('ROUTE_REFUSED');
  }
  return worker;
}
