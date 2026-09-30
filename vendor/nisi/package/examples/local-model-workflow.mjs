// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// Opt-in local inference example. The task generates JSON configuration, which
// is parsed and tested as data. Model-generated programs are never executed.
import assert from 'node:assert/strict';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createLocalChatAuthorAdapter, createLocalChatReviewerAdapter } from '../adapters/local-chat.mjs';
import { canonicalizeJSONV1 } from '../serialization/canonical-json-v1.mjs';
import { runWorkflow } from '../workflow/engine.mjs';

const USAGE = 'Usage: node examples/local-model-workflow.mjs http://127.0.0.1:1234/v1/chat/completions AUTHOR_MODEL REVIEWER_MODEL';

export function parseLocalModelCLI(args) {
  if (!Array.isArray(args) || args.length !== 3) throw new Error(`Expected exactly three arguments.\n${USAGE}`);
  const [endpoint, authorModel, reviewerModel] = args.map(value => typeof value === 'string' ? value.trim() : value);
  if ([endpoint, authorModel, reviewerModel].some(value => typeof value !== 'string' || value.trim() === '')) {
    throw new Error(`Endpoint and model names must be non-blank.\n${USAGE}`);
  }
  if (authorModel === reviewerModel) throw new Error(`Author and reviewer model names must differ.\n${USAGE}`);
  const noInference = () => { throw new Error('CLI validation must not invoke a model.'); };
  try {
    createLocalChatAuthorAdapter({ destination: 'LOOPBACK_HTTP', endpoint, model: authorModel, id: 'local.author', fetch: noInference });
    createLocalChatReviewerAdapter({ destination: 'LOOPBACK_HTTP', endpoint, model: reviewerModel, id: 'local.reviewer', fetch: noInference });
  } catch (error) {
    throw new Error(`Invalid local chat configuration (${error?.code ?? 'LOCAL_CHAT_CONFIG_INVALID'}). Use an HTTP endpoint at 127.0.0.1 or [::1], without credentials, query, or fragment; model names must be valid configured IDs.\n${USAGE}`);
  }
  return Object.freeze({ endpoint, authorModel, reviewerModel });
}

export async function runLocalModelExample({ endpoint, authorModel, reviewerModel, fetch, timeoutMs = 180_000 }) {
  if (authorModel === reviewerModel) throw new Error('This demonstration requires two different configured model names.');
  const config = { destination: 'LOOPBACK_HTTP', endpoint, timeoutMs, maxOutputTokens: 4096, ...(fetch ? {fetch} : {}) };
  const author = createLocalChatAuthorAdapter({ ...config, model: authorModel, id: 'local.author' });
  const reviewer = createLocalChatReviewerAdapter({ ...config, model: reviewerModel, id: 'local.reviewer' });
  const task = {
    taskId: 'example.retry-config', mode: 'edit', language: 'json', allowedFiles: ['retry-config.json'],
    protectedFiles: [], protectedSnapshots: {},
    acceptanceCriteria: [
      'Create retry-config.json as a JSON object with exactly maxRetries, retryDelayMs, and backoff.',
      'maxRetries must equal 3, retryDelayMs must equal 250, and backoff must equal "exponential".',
    ],
    policy: { repairBudget: 1, totalDeadlineMs: timeoutMs, requiredReviewers: 1, requireReportStore: false },
  };
  const parse = candidate => JSON.parse(canonicalizeJSONV1(Buffer.from(candidate.files[0].content, 'utf8')).canonical);
  const report = await runWorkflow(task, { adapters: {
    authorizeContext: { authorize: ({binding}) => ({status: 'PASS', evidence: {...binding, reason: ''}}) },
    author,
    staticChecks: { check({candidate, binding}) {
      const findings = [];
      try { parse(candidate); } catch { findings.push({code: 'JSON_INVALID', message: 'The configuration must satisfy the restricted JSON profile.'}); }
      return {status: findings.length ? 'FAIL' : 'PASS', evidence: {...binding, findings, reason: findings.length ? 'JSON parsing failed.' : ''}};
    } },
    tests: { run({candidate, binding}) {
      const config = parse(candidate);
      const assertions = [
        ['CONFIG_FIELDS', () => assert.deepEqual(Object.keys(config).sort(), ['backoff', 'maxRetries', 'retryDelayMs'])],
        ['RETRY_LIMIT', () => assert.equal(config.maxRetries, 3)],
        ['RETRY_DELAY', () => assert.equal(config.retryDelayMs, 250)],
        ['BACKOFF', () => assert.equal(config.backoff, 'exponential')],
      ];
      let assertionsPassed = 0;
      const failures = [];
      for (const [code, run] of assertions) {
        try { run(); assertionsPassed += 1; } catch { failures.push({code, message: `The ${code} acceptance assertion failed.`}); }
      }
      return {status: failures.length ? 'FAIL' : 'PASS', evidence: {...binding,
        assertionsExecuted: assertions.length, assertionsPassed, failures, reason: failures.length ? 'Acceptance assertions failed.' : ''}};
    } },
    reviewers: [reviewer],
  } });
  return { report, authorReceipts: author.receipts(), reviewerReceipts: reviewer.receipts() };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  let cli;
  try { cli = parseLocalModelCLI(process.argv.slice(2)); } catch (error) {
    console.error(error.message);
    process.exitCode = 2;
  }
  if (cli) {
    const result = await runLocalModelExample(cli);
    console.log(JSON.stringify(result, null, 2));
    if (result.report.outcome !== 'COMPLETED') process.exitCode = 1;
  }
}
