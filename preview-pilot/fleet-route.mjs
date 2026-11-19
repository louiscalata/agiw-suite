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
  if (typeof worker !== 'string' || !known.includes(worker) || cfg.workerId !== worker ||
      cfg.route.preanalysis !== 'local' || cfg.route.reason !== 'strict-local') {
    throw Error('ROUTE_REFUSED');
  }
  return worker;
}
