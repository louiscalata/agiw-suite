import {planWithJev} from './vendor/nisi-0.3.1/adapters/jev-advisor.mjs';
import {stableStringify} from './vendor/nisi-0.3.1/workflow/contracts.mjs';

export function replayJudge(route) {
  return async request => {
    try {
      const {state, questions} = request;
      if (stableStringify({state, questions}) === stableStringify(route.jev.request)) return route.jev.response;
    } catch { /* Non-canonical input cannot match. */ }
    throw Object.assign(new Error('JEV_REPLAY_MISMATCH'), {code: 'JEV_REPLAY_MISMATCH'});
  };
}

// Called after checkRoute. The recorded response is the only judge available to the program.
export async function programPlan(cfg, task, maxTokens) {
  if (cfg.route.preanalysis === 'local') return {maxTokens};
  try {
    const {plan, settings, confidence, underspecified, evidence} = await planWithJev(task, {judge: replayJudge(cfg.route)});
    const {jevModel, requestSha256} = evidence;
    return {maxTokens: Math.min(maxTokens, settings.maxOutputTokens), jevPlan: {plan, confidence, underspecified, jevModel, requestSha256}};
  } catch {
    // Do not echo a mismatched request or the underlying exception.
    const {worker, preanalysis, reason} = cfg.route;
    const refusal = {mode: cfg.mode, nisiVersion: '0.3.1', route: {worker, preanalysis, reason}, independentLLMReview: false,
      modelCalls: 0, report: null, reportStorePath: null, authorReceipts: [], code: 'JEV_PLAN_REFUSED'};
    await new Promise(resolve => process.stdout.write(JSON.stringify(refusal) + '\n', resolve));
    process.exit(1);
  }
}
