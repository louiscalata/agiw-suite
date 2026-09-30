// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// Sequential workflow orchestration. All file, process, model and persistence
// operations belong to trusted host adapters; this module does not perform them.
import { randomUUID } from 'node:crypto';
import {
  ADAPTER_STATUSES, REPAIR_STATUSES, RUN_OUTCOMES, WorkflowInputError,
  cloneFreeze, createCandidate, createTaskSpecification, freezeReport,
  sha256Text, stableStringify, validateCandidateForTask,
  validateAdapterIdentity, validateFindings,
} from './contracts.mjs';

const reject = code => { throw new WorkflowInputError(code); };
const codeOf = (error, fallback) => error instanceof WorkflowInputError ? error.code : fallback;
const label = stage => stage.replace(/[A-Z]/gu, letter => `_${letter}`).toUpperCase();
const text = value => typeof value === 'string' && value.trim().length > 0 && value.length <= 4096;
const exact = (value, keys, code) => {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) reject(code);
};
const emptyReport = (code, task = null) => freezeReport({
  schemaVersion: 1, runId: null, taskId: task?.taskId ?? null,
  taskFingerprint: task ? fingerprintTask(task) : null, mode: task?.mode ?? null,
  outcome: 'BLOCKED', workflowOutcome: 'BLOCKED', code, candidate: null,
  candidateFingerprint: null, repairAttempts: 0, stages: [],
  reportStored: false, reportStoreCode: null, storedReportSha256: null, reportStoreEvidence: null,
});
const fingerprintTask = task => sha256Text(`nisi/workflow-task/v1\0${stableStringify(task)}`);

function snapshotAdapters(task, source) {
  if (!source || typeof source !== 'object' || Array.isArray(source)) reject('ADAPTERS_INVALID');
  const method = (object, name, code) => {
    const fn = object?.[name];
    if (typeof fn !== 'function') reject(code);
    return fn.bind(object);
  };
  const authorize = method(source.authorizeContext, 'authorize', 'AUTHORIZATION_ADAPTER_REQUIRED');
  const check = method(source.staticChecks, 'check', 'STATIC_CHECK_ADAPTER_REQUIRED');
  const tests = method(source.tests, 'run', 'TEST_ADAPTER_REQUIRED');
  if (!Array.isArray(source.reviewers) || source.reviewers.length !== task.policy.requiredReviewers) reject('REVIEW_ADAPTER_COUNT');
  const reviewers = Object.freeze(source.reviewers.map(reviewer => Object.freeze({
    id: validateAdapterIdentity(reviewer, 'reviewer'),
    review: method(reviewer, 'review', 'REVIEW_ADAPTER_REQUIRED'),
  })));
  if (new Set(reviewers.map(reviewer => reviewer.id)).size !== reviewers.length) reject('DUPLICATE_REVIEWER');
  let author = null;
  if (task.mode === 'edit') {
    author = Object.freeze({
      id: validateAdapterIdentity(source.author, 'author'),
      draft: method(source.author, 'draft', 'AUTHOR_ADAPTER_REQUIRED'),
      repair: task.policy.repairBudget > 0 ? method(source.author, 'repair', 'REPAIR_ADAPTER_REQUIRED') : null,
    });
    if (reviewers.some(reviewer => reviewer.id === author.id)) reject('AUTHOR_REVIEWER_NOT_INDEPENDENT');
  }
  return Object.freeze({ authorize, check, tests, reviewers, author });
}

function validateEvidence(evidence, binding, extraKeys, stage) {
  exact(evidence, [...Object.keys(binding), ...extraKeys], `${label(stage)}_EVIDENCE_SCHEMA`);
  for (const key of Object.keys(binding)) {
    if (evidence[key] !== binding[key]) reject(`${label(stage)}_EVIDENCE_STALE`);
  }
}

function validateResult(raw, stage, binding) {
  exact(raw, ['status', 'evidence'], `${label(stage)}_RESULT_SCHEMA`);
  if (!ADAPTER_STATUSES.includes(raw.status)) reject(`${label(stage)}_STATUS_INVALID`);
  const evidence = raw.evidence;
  const keys = stage === 'authorizeContext' ? ['reason'] : stage === 'tests'
    ? ['assertionsExecuted', 'assertionsPassed', 'failures', 'reason'] : stage === 'review'
      ? ['reviewerId', 'findings', 'summary', 'reason'] : ['findings', 'reason'];
  validateEvidence(evidence, binding, keys, stage);
  if (raw.status === 'PASS' ? evidence.reason !== '' : !text(evidence.reason)) reject(`${label(stage)}_REASON_INVALID`);
  if (stage === 'authorizeContext') return raw;
  const findings = stage === 'tests' ? evidence.failures : evidence.findings;
  validateFindings(findings, label(stage));
  if (raw.status === 'PASS' && findings.length !== 0) reject(`${label(stage)}_PASS_WITH_FINDINGS`);
  if (raw.status === 'FAIL' && findings.length === 0) reject(`${label(stage)}_FAIL_WITHOUT_FINDINGS`);
  if (['NOT_RUN', 'UNAVAILABLE'].includes(raw.status) && findings.length !== 0) reject(`${label(stage)}_NOT_RUN_WITH_FINDINGS`);
  if (stage === 'tests') {
    const { assertionsExecuted: executed, assertionsPassed: passed } = evidence;
    if (!Number.isSafeInteger(executed) || !Number.isSafeInteger(passed) || passed < 0 || executed < passed) reject('TEST_ASSERTION_COUNT_INVALID');
    if (raw.status === 'PASS' && (executed === 0 || executed !== passed)) reject('NO_TEST_ASSERTIONS_EXECUTED');
    if (raw.status === 'FAIL' && (executed === 0 || executed === passed)) reject('TEST_FAILURE_EVIDENCE_INVALID');
    if (['NOT_RUN', 'UNAVAILABLE'].includes(raw.status) && (executed !== 0 || passed !== 0 || findings.length !== 0)) reject('TEST_NOT_RUN_WITH_ASSERTIONS');
  }
  if (stage === 'review' && (!text(evidence.summary) || !text(evidence.reviewerId))) reject('REVIEW_EVIDENCE_INVALID');
  return raw;
}

// A timer cannot preempt synchronous JavaScript. Check the monotonic clock both
// before invocation and after settlement, rejecting a result that arrived late.
function createInvoker(signal, clock, totalDeadlineMs) {
  let previous;
  try { previous = clock(); } catch { reject('CLOCK_INVALID'); }
  if (!Number.isSafeInteger(previous) || previous < 0 || !Number.isSafeInteger(previous + totalDeadlineMs)) reject('CLOCK_INVALID');
  const deadline = previous + totalDeadlineMs;
  const guard = () => {
    try {
      if (signal?.aborted) return { kind: 'cancelled', code: 'ABORTED' };
      const now = clock();
      if (!Number.isSafeInteger(now) || now < previous) return { kind: 'error', code: 'CLOCK_INVALID' };
      previous = now;
      if (now >= deadline) return { kind: 'timed_out', code: 'DEADLINE_EXCEEDED' };
      return null;
    } catch { return { kind: 'error', code: 'CLOCK_INVALID' }; }
  };
  const invoke = async (fn, payload) => {
    const stopped = guard();
    if (stopped) return stopped;
    return new Promise(resolve => {
      const controller = new AbortController();
      let timer, settled = false;
      const finish = result => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try { signal?.removeEventListener('abort', onAbort); } catch { /* trusted host signal */ }
        resolve(result);
      };
      const onAbort = () => { controller.abort(); finish({ kind: 'cancelled', code: 'ABORTED' }); };
      timer = setTimeout(() => {
        controller.abort();
        finish({ kind: 'timed_out', code: 'DEADLINE_EXCEEDED' });
      }, deadline - previous);
      try { signal?.addEventListener('abort', onAbort, { once: true }); }
      catch { finish({ kind: 'error', code: 'SIGNAL_INVALID' }); }
      Promise.resolve().then(() => {
        if (settled) return;
        const stoppedBeforeCall = guard();
        if (stoppedBeforeCall) { controller.abort(); finish(stoppedBeforeCall); return; }
        return fn(Object.freeze({ ...payload, signal: controller.signal }));
      }).then(value => {
        if (settled) return;
        const stoppedAfterCall = guard();
        if (stoppedAfterCall) { controller.abort(); finish(stoppedAfterCall); return; }
        let snapshot;
        try { snapshot = cloneFreeze(value); }
        catch (error) {
          const stoppedDuringCopy = guard();
          if (stoppedDuringCopy) { controller.abort(); finish(stoppedDuringCopy); return; }
          finish({ kind: 'error', code: codeOf(error, 'ADAPTER_RESULT_INVALID') });
          return;
        }
        const stoppedAfterCopy = guard();
        if (stoppedAfterCopy) { controller.abort(); finish(stoppedAfterCopy); return; }
        finish({ kind: 'result', value: snapshot });
      }, () => finish(guard() ?? { kind: 'error', code: 'ADAPTER_EXCEPTION' }));
    });
  };
  return { invoke, guard };
}

/** Run a bounded edit/review task and return an immutable report. A COMPLETED
 * report means the configured checks passed; applying a candidate remains a
 * separate host action. Adapter identity and execution are host trust inputs. */
export async function runWorkflow(taskInput, options = {}) {
  let task, adapters, candidate = null, signal, invoker, store = null, storeInvalid = false;
  try {
    task = createTaskSpecification(taskInput);
    if (!options || typeof options !== 'object' || Array.isArray(options) ||
        Reflect.ownKeys(options).some(key => !['adapters', 'candidate', 'candidateAuthorId', 'clock', 'reportStore', 'signal'].includes(key))) reject('OPTIONS_SCHEMA');
    adapters = snapshotAdapters(task, options.adapters);
    signal = options.signal;
    if (signal !== undefined && !(signal instanceof AbortSignal)) reject('SIGNAL_INVALID');
    const clock = options.clock ?? (() => Math.floor(performance.now()));
    if (typeof clock !== 'function') reject('CLOCK_INVALID');
    invoker = createInvoker(signal, clock, task.policy.totalDeadlineMs);
    if (task.mode === 'review') {
      if (!options.candidate || !options.candidateAuthorId) reject('REVIEW_CANDIDATE_REQUIRED');
      candidate = validateCandidateForTask(options.candidate, task, options.candidateAuthorId);
      if (adapters.reviewers.some(reviewer => reviewer.id === candidate.authorId)) reject('AUTHOR_REVIEWER_NOT_INDEPENDENT');
    }
    if (options.reportStore !== undefined) {
      const object = options.reportStore;
      const method = object?.store;
      if (typeof method === 'function') store = method.bind(object);
      else storeInvalid = true;
    }
    if (task.policy.requireReportStore && !store) reject('REPORT_STORE_REQUIRED');
  } catch (error) { return emptyReport(codeOf(error, 'INPUT_INVALID'), task); }

  const runId = randomUUID();
  const taskFingerprint = fingerprintTask(task);
  let repairAttempts = 0, outcome = null, code = null;
  const stages = [];
  const seen = new Set(candidate ? [candidate.fingerprint] : []);
  const binding = (fingerprint = candidate?.fingerprint ?? null) => Object.freeze({
    schemaVersion: 1, runId, taskFingerprint, attempt: repairAttempts, candidateFingerprint: fingerprint,
  });
  const record = (stage, status, evidence, failureCode = null) => stages.push(cloneFreeze({
    stage, status, candidateFingerprint: evidence.candidateFingerprint ?? null, code: failureCode, evidence,
  }));
  const stop = (stage, result, bound) => {
    outcome = result.kind === 'cancelled' ? 'CANCELLED' : result.kind === 'timed_out' ? 'TIMED_OUT' : 'BLOCKED';
    code = result.code;
    record(stage, 'UNAVAILABLE', { ...bound, reason: code }, code);
  };
  const payload = bound => ({ task, candidate, acceptanceCriteria: task.acceptanceCriteria, binding: bound });
  const stage = async (name, fn, extra = {}) => {
    const bound = binding();
    const call = await invoker.invoke(fn, { ...payload(bound), ...extra });
    if (call.kind !== 'result') { stop(name, call, bound); return null; }
    try {
      const result = validateResult(call.value, name, bound);
      if (name === 'review' && result.evidence.reviewerId !== extra.reviewerId) reject('REVIEWER_ID_MISMATCH');
      record(name, result.status, result.evidence);
      if (result.status !== 'PASS') {
        outcome = name !== 'authorizeContext' && result.status === 'FAIL' ? 'FAILED' : 'BLOCKED';
        code = `${label(name)}_${result.status === 'FAIL' ? 'FAILED' : result.status}`;
      }
      return result;
    } catch (error) { stop(name, { kind: 'error', code: codeOf(error, `${label(name)}_INVALID`) }, bound); return null; }
  };
  record('intake', 'PASS', { ...binding(), taskId: task.taskId, mode: task.mode, acceptanceCriteriaCount: task.acceptanceCriteria.length });
  await stage('authorizeContext', adapters.authorize);
  if (!outcome && task.mode === 'edit') {
    const bound = binding(null);
    const call = await invoker.invoke(adapters.author.draft, payload(bound));
    if (call.kind !== 'result') stop('draft', call, bound);
    else try {
      exact(call.value, ['candidate', 'evidence'], 'DRAFT_RESULT_SCHEMA');
      const drafted = validateCandidateForTask(call.value.candidate, task, adapters.author.id);
      validateEvidence(call.value.evidence, { ...bound, candidateFingerprint: drafted.fingerprint }, ['note'], 'draft');
      if (!text(call.value.evidence.note)) reject('DRAFT_NOTE_INVALID');
      candidate = drafted;
      seen.add(candidate.fingerprint);
      record('draft', 'PASS', call.value.evidence);
    } catch (error) { stop('draft', { kind: 'error', code: codeOf(error, 'DRAFT_INVALID') }, bound); }
  }

  while (!outcome) {
    const checks = await stage('staticChecks', adapters.check);
    const tests = !outcome ? await stage('tests', adapters.tests, { checks }) : null;
    if (!outcome) for (const reviewer of adapters.reviewers) {
      await stage('review', reviewer.review, { reviewerId: reviewer.id, checks, tests });
      if (outcome) break;
    }
    if (!outcome) {
      const stopped = invoker.guard();
      if (stopped) stop('completion', stopped, binding());
      else outcome = 'COMPLETED';
      break;
    }
    if (outcome !== 'FAILED' || task.mode !== 'edit') break;
    if (repairAttempts >= task.policy.repairBudget) {
      if (task.policy.repairBudget > 0) outcome = 'REPAIR_LIMIT';
      break;
    }
    const base = candidate.fingerprint;
    const previousStages = cloneFreeze(stages.filter(item => item.evidence.attempt === repairAttempts));
    repairAttempts += 1;
    const bound = binding(base);
    const call = await invoker.invoke(adapters.author.repair, { ...payload(bound), stages: previousStages });
    if (call.kind !== 'result') { stop('repair', call, bound); break; }
    try {
      const repair = call.value;
      exact(repair, ['status', 'candidate', 'evidence'], 'REPAIR_RESULT_SCHEMA');
      if (!REPAIR_STATUSES.includes(repair.status)) reject('REPAIR_STATUS_INVALID');
      let next = null;
      if (repair.status === 'REPAIRED') next = validateCandidateForTask(repair.candidate, task, adapters.author.id);
      else if (repair.candidate !== null) reject('REPAIR_UNUSED_CANDIDATE');
      validateEvidence(repair.evidence, { ...bound, candidateFingerprint: next?.fingerprint ?? base }, ['baseCandidateFingerprint', 'note'], 'repair');
      if (repair.evidence.baseCandidateFingerprint !== base) reject('REPAIR_BASE_STALE');
      if (!text(repair.evidence.note)) reject('REPAIR_NOTE_INVALID');
      record('repair', repair.status, repair.evidence);
      if (repair.status === 'NO_CHANGE' || (next && seen.has(next.fingerprint))) { outcome = 'NO_PROGRESS'; code = 'NO_PROGRESS'; break; }
      if (!next) { outcome = repair.status === 'FAIL' ? 'FAILED' : 'BLOCKED'; code = `REPAIR_${repair.status}`; break; }
      candidate = next;
      seen.add(candidate.fingerprint);
      outcome = null;
      code = null;
    } catch (error) { stop('repair', { kind: 'error', code: codeOf(error, 'REPAIR_INVALID') }, bound); break; }
  }

  const report = {
    schemaVersion: 1, runId, taskId: task.taskId, taskFingerprint, mode: task.mode,
    outcome, workflowOutcome: outcome, code, candidate, candidateFingerprint: candidate?.fingerprint ?? null,
    repairAttempts, stages, reportStored: false,
    reportStoreCode: storeInvalid ? 'REPORT_STORE_UNAVAILABLE' : null, storedReportSha256: null, reportStoreEvidence: null,
  };
  if (store) {
    const preliminary = freezeReport(report);
    const reportSha256 = sha256Text(`nisi/run-report/v1\0${stableStringify(preliminary)}`);
    const bound = binding();
    const call = await invoker.invoke(store, { report: preliminary, reportSha256, binding: bound });
    let storeCode = null;
    if (call.kind !== 'result') storeCode = call.code;
    else try {
      exact(call.value, ['status', 'evidence'], 'REPORT_STORE_RESULT_SCHEMA');
      if (!ADAPTER_STATUSES.includes(call.value.status)) reject('REPORT_STORE_STATUS_INVALID');
      validateEvidence(call.value.evidence, bound, ['outcome', 'reportSha256'], 'reportStore');
      if (call.value.evidence.outcome !== preliminary.outcome || call.value.evidence.reportSha256 !== reportSha256) reject('REPORT_STORE_EVIDENCE_STALE');
      report.reportStoreEvidence = call.value;
      if (call.value.status === 'PASS') { report.reportStored = true; report.storedReportSha256 = reportSha256; }
      else storeCode = `REPORT_STORE_${call.value.status}`;
    } catch (error) { storeCode = codeOf(error, 'REPORT_STORE_INVALID'); }
    if (storeCode) {
      report.reportStoreCode = storeCode;
      if (call.kind === 'cancelled' || call.kind === 'timed_out') {
        report.outcome = call.kind === 'cancelled' ? 'CANCELLED' : 'TIMED_OUT'; report.code = storeCode;
      } else if (task.policy.requireReportStore) { report.outcome = 'BLOCKED'; report.code = storeCode; }
    }
  }
  return freezeReport(report);
}

export { createCandidate, createTaskSpecification, RUN_OUTCOMES, sha256Text };
