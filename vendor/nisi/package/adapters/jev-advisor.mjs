// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// Jev (TypeSafe System One) pre- and post-analysis for Nisi runs.
//
// The host supplies `judge`. A function receives `{ state, questions }` and
// resolves to `{ model, answers }`. The string `typesafe` selects the built-in
// client in jev-client.mjs, which reads TYPESAFE_API_KEY and sends `state`.
// A set key with no judge does not: one line goes to stderr and the call stays
// on the no-judge path. This module frames the questions, validates every typed
// answer and turns it into either adapter settings (pre) or a reviewer verdict
// (post). Jev's answers are judgments, not proof: the pre-analysis only picks
// settings, and the reviewer is one reviewer among the ones the task requires.

import { cloneFreeze, sha256Text, stableStringify } from '../workflow/contracts.mjs';
import { createJevJudge, JEV_API_KEY_ENV } from './jev-client.mjs';

const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = code => { const error = new Error(code); error.code = code; throw error; };
// Opt in to the hosted client. The same word is the harder runner's `--judge typesafe`.
export const REMOTE_JUDGE_OPT_IN = 'typesafe';
export const REMOTE_JUDGE_NOTICE = "TYPESAFE_API_KEY is set; the remote judge is off. Opt in with --judge typesafe or judge: 'typesafe'.";
// A function is the caller's judge. `typesafe` builds the client and forwards
// `fetchImpl` and `timeoutMs` only to that client. Any other value is refused.
// A set key with no judge prints one line and returns no judge, so nothing is sent.
const resolveJudge = (judge, fetchImpl, timeoutMs) => {
  if (typeof judge === 'function') return judge;
  if (judge === REMOTE_JUDGE_OPT_IN) {
    return createJevJudge({
      ...(fetchImpl ? { fetch: fetchImpl } : {}),
      ...(timeoutMs !== undefined ? { timeoutMs } : {}),
    });
  }
  if (judge !== undefined) fail('JEV_JUDGE_INVALID');
  if (process.env[JEV_API_KEY_ENV]) console.error(REMOTE_JUDGE_NOTICE);
  return undefined;
};
const probability = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1;

export const JEV_PLAN_CHOICES = Object.freeze({
  direct: 'A single lookup, extraction, classification or formatting step; the answer follows directly from the input.',
  reason: 'Several dependent steps, exact arithmetic, calendar or ordering rules, or edge cases that are easy to get wrong without working through them.',
});

// Settings for each plan choice, passed to createLocalChat*Adapter by the host.
export const DEFAULT_JEV_PLAN_POLICY = Object.freeze({
  direct: Object.freeze({ reasoningEffort: 'none', maxOutputTokens: 1024 }),
  reason: Object.freeze({ maxOutputTokens: 4096 }),
});

function checkAnswers(questions, response) {
  if (!isRecord(response) || typeof response.model !== 'string' || !isRecord(response.answers)) fail('JEV_RESPONSE_INVALID');
  const { answers } = response;
  const keys = Object.keys(questions);
  if (Object.keys(answers).length !== keys.length || keys.some(key => !isRecord(answers[key]))) fail('JEV_RESPONSE_INVALID');
  for (const key of keys) {
    const question = questions[key], answer = answers[key];
    if (answer.confidence !== undefined && !probability(answer.confidence)) fail('JEV_RESPONSE_INVALID');
    if (question.type === 'noul' && !probability(answer.noul)) fail('JEV_RESPONSE_INVALID');
    if (question.type === 'choice' && !Object.hasOwn(question.criteria, answer.choice)) fail('JEV_RESPONSE_INVALID');
  }
  return answers;
}

async function ask(judge, state, questions) {
  if (typeof judge !== 'function') fail('JEV_JUDGE_REQUIRED');
  const request = cloneFreeze({ state, questions });
  let response;
  try { response = await judge(request); } catch (error) { fail(typeof error?.code === 'string' && error.code.startsWith('JEV_') ? error.code : 'JEV_UNAVAILABLE'); }
  const answers = checkAnswers(questions, response);
  return { model: response.model, answers, requestSha256: sha256Text(stableStringify(request)) };
}

const taskState = task => {
  if (!isRecord(task) || !Array.isArray(task.acceptanceCriteria)) fail('JEV_TASK_INVALID');
  return { task: { taskId: task.taskId, mode: task.mode, language: task.language, allowedFiles: task.allowedFiles, acceptanceCriteria: task.acceptanceCriteria } };
};

// Pre-analysis: does this task need the model to reason, and is it answerable as written?
export async function planWithJev(task, { judge, policy = DEFAULT_JEV_PLAN_POLICY, fetch: fetchImpl, timeoutMs } = {}) {
  if (fetchImpl !== undefined && typeof fetchImpl !== 'function') fail('JEV_UNAVAILABLE');
  if (!isRecord(policy) || !isRecord(policy.direct) || !isRecord(policy.reason)) fail('JEV_POLICY_INVALID');
  const questions = {
    plan: { type: 'choice', instructions: 'How much working-through does a careful model need to answer `task` correctly?', criteria: { ...JEV_PLAN_CHOICES } },
    underspecified: { type: 'noul', instructions: 'Is `task` missing information needed to produce exactly one correct answer?' },
  };
  const { model, answers, requestSha256 } = await ask(resolveJudge(judge, fetchImpl, timeoutMs), taskState(task), questions);
  const plan = answers.plan.choice;
  return cloneFreeze({ plan, settings: { ...policy[plan] }, confidence: answers.plan.confidence ?? null,
    underspecified: answers.underspecified.noul, evidence: { jevModel: model, requestSha256 } });
}

// Post-analysis as a Nisi reviewer. By default it is advisory: it always passes
// and records Jev's probability in the review summary for the host to read.
// Measured on 31 labelled answers, right and wrong answers overlapped too much
// for Jev to gate a run: at a threshold of 0.3 it caught 10 of 13 wrong answers
// and failed 7 of 18 right ones; at the default 0.5 it caught 13 of 13 and
// failed 14 of 18. Pass `gate: true` to FAIL below `threshold`,
// which sends the candidate to repair.
export function createJevReviewer({ id, judge: given, gate = false, threshold = 0.5, fetch: fetchImpl, timeoutMs } = {}) {
  if (typeof id !== 'string' || !/^[a-z][a-z0-9_.-]{0,95}$/u.test(id)) fail('JEV_ID_INVALID');
  if (fetchImpl !== undefined && typeof fetchImpl !== 'function') fail('JEV_UNAVAILABLE');
  const judge = resolveJudge(given, fetchImpl, timeoutMs);
  if (typeof judge !== 'function') fail('JEV_JUDGE_REQUIRED');
  if (typeof gate !== 'boolean') fail('JEV_GATE_INVALID');
  if (!probability(threshold)) fail('JEV_THRESHOLD_INVALID');
  return Object.freeze({ id, async review({ task, candidate, tests, binding }) {
    const questions = {
      satisfies: { type: 'noul', instructions: 'Does `candidate` satisfy every item in `task.acceptanceCriteria` exactly, including values, ordering, types and the required output form?' },
    };
    const state = { ...taskState(task), candidate: { files: candidate?.files ?? [] }, testsPassed: tests?.status === 'PASS' };
    let verdict;
    try { verdict = await ask(judge, state, questions); }
    catch (error) {
      return { status: 'UNAVAILABLE', evidence: { ...binding, reviewerId: id, findings: [], summary: `Jev was unavailable (${error.code}).`, reason: error.code } };
    }
    const p = verdict.answers.satisfies.noul;
    const pass = !gate || p >= threshold;
    const summary = `Jev ${verdict.model}: probability the candidate satisfies every criterion ${p.toFixed(2)}${gate ? ` (gate below ${threshold})` : ' (advisory)'}.`;
    return { status: pass ? 'PASS' : 'FAIL', evidence: { ...binding, reviewerId: id,
      findings: pass ? [] : [{ code: 'JEV_LIKELY_INCORRECT', message: 'Jev judged that the candidate probably does not meet every acceptance criterion. Recheck each value, ordering rule and edge case.' }],
      summary, reason: pass ? '' : summary } };
  } });
}
