// Shared route check for the pilot programs. Known worker ids come from the router
// config (fleet-workers.json, kept equal to agiw.fleet_routing_plan.WORKERS by tests).
import {readFileSync} from 'node:fs';

const ID = /^[a-z0-9][a-z0-9-]{0,62}$/;

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
  const local = route?.preanalysis === 'local' && route.reason === 'strict-local' && !Object.hasOwn(route, 'jev');
  // The worker already checked the request against this program's manifest and questions.
  const j = route?.jev, answers = j?.response?.answers;
  const judged = route?.preanalysis === 'jev' && route.reason === 'jev-preanalyzed' &&
    ['direct', 'reason'].includes(j?.plan) && j.plan === answers?.plan?.choice &&
    typeof j.jevModel === 'string' && j.jevModel === j.response.model &&
    j.confidence === (answers.plan.confidence ?? null) &&
    (j.confidence === null || (Number.isFinite(j.confidence) && j.confidence >= 0 && j.confidence <= 1)) &&
    Number.isFinite(j.underspecified) && j.underspecified === answers.underspecified?.noul &&
    j.request?.state?.task && j.request?.questions;
  if (typeof worker !== 'string' || !known.includes(worker) || cfg.workerId !== worker ||
      !(local || judged)) {
    throw Error('ROUTE_REFUSED');
  }
  return worker;
}
