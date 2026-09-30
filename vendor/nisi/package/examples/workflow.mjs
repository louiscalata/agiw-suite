// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// A complete local demonstration: real Node syntax checks and assertions, a
// deterministic author/reviewer, one repair, and a report written/read on disk.
// Only the fixed source below is executed. This is not an untrusted-code sandbox.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createCandidate, runWorkflow } from '../workflow/engine.mjs';
import { sha256Text, stableStringify } from '../workflow/contracts.mjs';

export async function runExample() {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'nisi-workflow-'));
  const task = {
    taskId: 'example.answer', mode: 'edit', language: 'javascript',
    allowedFiles: ['answer.mjs'], protectedFiles: [], protectedSnapshots: {},
    acceptanceCriteria: ['Exports the number 42 as answer.'],
    policy: { repairBudget: 1, totalDeadlineMs: 10_000, requiredReviewers: 1, requireReportStore: true },
  };
  const make = (number, binding) => {
    const candidate = { files: [{ path: 'answer.mjs', content: `export const answer = ${number};\n` }] };
    return { candidate, evidence: { ...binding,
      candidateFingerprint: createCandidate(candidate, { authorId: 'example.author' }).fingerprint,
      note: 'Fixed local example; no model was called.',
    } };
  };
  const adapters = {
    authorizeContext: { authorize: ({ binding }) => ({ status: 'PASS', evidence: { ...binding, reason: '' } }) },
    author: {
      id: 'example.author',
      draft: ({ binding }) => make(41, binding),
      repair: ({ binding, candidate }) => {
        const result = make(42, binding);
        return { ...result, status: 'REPAIRED', evidence: { ...result.evidence, baseCandidateFingerprint: candidate.fingerprint } };
      },
    },
    staticChecks: {
      check({ candidate, binding }) {
        const result = spawnSync(process.execPath, ['--check', '--input-type=module'], {
          input: candidate.files[0].content, encoding: 'utf8', timeout: 2_000, maxBuffer: 16_384,
        });
        if (result.error) throw result.error;
        const findings = result.status === 0 ? [] : [{ code: 'SYNTAX_ERROR', message: 'Node rejected the module syntax.' }];
        return { status: findings.length ? 'FAIL' : 'PASS', evidence: { ...binding, findings, reason: findings.length ? 'Syntax check failed.' : '' } };
      },
    },
    tests: {
      async run({ candidate, binding }) {
        const module = await import(`data:text/javascript;base64,${Buffer.from(candidate.files[0].content).toString('base64')}`);
        let assertionsExecuted = 0, assertionsPassed = 0;
        const failures = [];
        for (const [code, assertion] of [
          ['ANSWER_TYPE', () => assert.equal(typeof module.answer, 'number')],
          ['ANSWER_VALUE', () => assert.equal(module.answer, 42)],
        ]) {
          assertionsExecuted += 1;
          try { assertion(); assertionsPassed += 1; }
          catch { failures.push({ code, message: code === 'ANSWER_VALUE' ? 'Expected answer to equal 42.' : 'Expected a number.' }); }
        }
        return { status: failures.length ? 'FAIL' : 'PASS', evidence: { ...binding,
          assertionsExecuted, assertionsPassed, failures, reason: failures.length ? 'An acceptance assertion failed.' : '',
        } };
      },
    },
    reviewers: [{ id: 'example.reviewer', review({ candidate, checks, tests, binding, reviewerId }) {
      const findings = candidate.files[0].content !== 'export const answer = 42;\n' || checks.status !== 'PASS' || tests.status !== 'PASS'
        ? [{ code: 'UNEXPECTED_CANDIDATE', message: 'Candidate or check evidence differs from this example contract.' }] : [];
      return { status: findings.length ? 'FAIL' : 'PASS', evidence: { ...binding, reviewerId, findings,
        reason: findings.length ? 'Example contract not met.' : '', summary: 'A separate deterministic callback checked the candidate and fresh check/test results. No model review occurred.',
      } };
    } }],
  };
  let reportPath;
  const reportStore = { async store({ report, reportSha256, binding }) {
    reportPath = path.join(directory, `${report.runId}.json`);
    const handle = await fs.open(reportPath, 'wx', 0o600);
    try { await handle.writeFile(JSON.stringify(report, null, 2) + '\n'); await handle.sync(); }
    finally { await handle.close(); }
    const readBack = JSON.parse(await fs.readFile(reportPath, 'utf8'));
    assert.equal(sha256Text(`nisi/run-report/v1\0${stableStringify(readBack)}`), reportSha256);
    return { status: 'PASS', evidence: { ...binding, outcome: report.outcome, reportSha256 } };
  } };
  const report = await runWorkflow(task, { adapters, reportStore });
  return { report, reportPath, directory };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const { report, reportPath } = await runExample();
  console.log(JSON.stringify({ outcome: report.outcome, code: report.code, repairAttempts: report.repairAttempts,
    stages: report.stages.map(({ stage, status }) => ({ stage, status })), reportStored: report.reportStored,
    storedReportSha256: report.storedReportSha256, reportPath, modelCalls: 0,
  }, null, 2));
  if (report.outcome !== 'COMPLETED' || !report.reportStored) process.exitCode = 1;
}
