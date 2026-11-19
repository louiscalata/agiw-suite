// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// Read-only check of a Nisi run journal, or of the journals and workflow reports
// in one directory. The summary carries counts, hashes and reason codes. It does
// not carry payloads, entry ids or parse excerpts.

import fs from 'node:fs';
import path from 'node:path';
import { readSerializedJournal } from './run-journal-store-v1.mjs';
import { reopen } from './run-journal-v1.mjs';
import { ADAPTER_STATUSES, REPAIR_STATUSES, RUN_OUTCOMES } from '../workflow/contracts.mjs';

export const MAX_FILE_BYTES = 64 * 1024 * 1024;
export const MAX_FILES = 500;
const STAGE_STATUSES = Object.freeze({
  intake: ['PASS'], authorizeContext: ADAPTER_STATUSES,
  draft: ['PASS', 'UNAVAILABLE'], staticChecks: ADAPTER_STATUSES,
  tests: ADAPTER_STATUSES, review: ADAPTER_STATUSES,
  repair: REPAIR_STATUSES, completion: ['UNAVAILABLE'],
});

function blank(targetKind, reason) {
  return { status: 'FAIL', targetKind, reason, checked: 0, passed: 0, failed: 0, skipped: 0, files: [] };
}

function relativeName(root, file) {
  const name = path.relative(root, file);
  if (name.startsWith(`..${path.sep}`) || name === '..' || path.isAbsolute(name)) return null;
  return name;
}

// Flags win over the environment. Exactly one of a journal file or a data directory.
export function parseValidateArgs(argv, env = process.env) {
  if (!Array.isArray(argv) || argv.some(value => typeof value !== 'string')) return { ok: false };
  let journal;
  let dataDir;
  let sawFlag = false;
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (flag !== '--journal' && flag !== '--data-dir') return { ok: false };
    sawFlag = true;
    const value = argv[index + 1];
    if (typeof value !== 'string' || value.length === 0 || value.startsWith('--')) return { ok: false };
    index += 1;
    if (flag === '--journal') {
      if (journal !== undefined) return { ok: false };
      journal = value;
    } else if (dataDir !== undefined) return { ok: false };
    else dataDir = value;
  }
  if (!sawFlag) {
    if (typeof env.NISI_JOURNAL === 'string' && env.NISI_JOURNAL.length > 0) journal = env.NISI_JOURNAL;
    if (typeof env.NISI_DATA_DIR === 'string' && env.NISI_DATA_DIR.length > 0) dataDir = env.NISI_DATA_DIR;
  }
  if ((journal === undefined) === (dataDir === undefined)) return { ok: false };
  return journal !== undefined ? { ok: true, journal } : { ok: true, dataDir };
}

function statOf(fileSystem, file) {
  try { return { stat: fileSystem.lstatSync(file) }; }
  catch (error) { return { error: error?.code === 'ENOENT' ? 'NOT_FOUND' : 'READ_FAILED' }; }
}

// The lstat size is only a first check: the file can grow or be replaced
// before it is opened. Read from one descriptor, check its fstat size, and
// never read more than maxBytes + 1 bytes, so the limit bounds what is read.
// O_NONBLOCK keeps the open from waiting on a FIFO swapped in after lstat; the
// fstat check below then refuses it. It has no effect on a regular file.
function boundedRead(fileSystem, file, maxBytes) {
  const noFollow = fileSystem.constants?.O_NOFOLLOW;
  let fd;
  try { fd = fileSystem.openSync(file, noFollow === undefined ? 'r' : fileSystem.constants.O_RDONLY | noFollow | (fileSystem.constants.O_NONBLOCK || 0)); }
  catch (error) { return { error: error?.code === 'ELOOP' ? 'SYMLINK' : 'READ_FAILED' }; }
  try {
    const stat = fileSystem.fstatSync(fd);
    if (!stat.isFile()) return { error: 'NOT_A_FILE' };
    if (stat.size > maxBytes) return { error: 'FILE_TOO_LARGE', bytes: stat.size };
    // One byte past the fstat size shows a file that grew during the read.
    const buffer = Buffer.alloc(stat.size + 1);
    let length = 0;
    while (length < buffer.length) {
      const count = fileSystem.readSync(fd, buffer, length, buffer.length - length, null);
      if (count === 0) break;
      length += count;
    }
    if (length > stat.size) return { error: 'FILE_CHANGED' };
    return { bytes: buffer.subarray(0, length) };
  } catch { return { error: 'READ_FAILED' }; }
  finally { try { fileSystem.closeSync(fd); } catch { /* the read result stands */ } }
}

function journalRecord(name, file, limits, fileSystem) {
  const found = statOf(fileSystem, file);
  if (found.error) return { name, kind: 'journal', status: 'FAIL', reason: found.error };
  if (found.stat.isSymbolicLink()) return { name, kind: 'journal', status: 'FAIL', reason: 'SYMLINK' };
  if (!found.stat.isFile()) return { name, kind: 'journal', status: 'FAIL', reason: 'NOT_A_FILE' };
  if (found.stat.size > limits.maxBytes) return { name, kind: 'journal', status: 'FAIL', reason: 'FILE_TOO_LARGE', bytes: found.stat.size };
  const bounded = boundedRead(fileSystem, file, limits.maxBytes);
  if (bounded.error) return { name, kind: 'journal', status: 'FAIL', reason: bounded.error, ...(bounded.bytes === undefined ? {} : { bytes: bounded.bytes }) };
  const read = readSerializedJournal({ path: file, fs: { readFileSync: () => bounded.bytes } });
  if (read.status !== 'READ') return { name, kind: 'journal', status: 'FAIL', reason: read.reason };
  const recovered = reopen(read.serialized);
  const entries = recovered.report.recoveredIds.length;
  if (recovered.report.status !== 'COMPLETE') {
    return { name, kind: 'journal', status: 'FAIL', reason: recovered.report.status, entries, bytes: read.bytes, sha256: read.sha256 };
  }
  return { name, kind: 'journal', status: 'PASS', reason: null, entries, bytes: read.bytes, sha256: read.sha256, verified: read.verified === true };
}

function reportOutcomeConsistent(value) {
  const workflowOutcome = value.workflowOutcome === undefined ? value.outcome : value.workflowOutcome;
  if (!RUN_OUTCOMES.includes(workflowOutcome)) return false;
  if (workflowOutcome !== value.outcome &&
      (!['BLOCKED', 'CANCELLED', 'TIMED_OUT'].includes(value.outcome) ||
       typeof value.reportStoreCode !== 'string' || value.reportStoreCode.length === 0 || value.reportStored === true)) return false;
  // Minimal legacy reports omit binding and task metadata. Check the recorded
  // results without requiring a complete modern report or inventing evidence.
  let endings = ['BLOCKED'];
  let stopped = false;
  let repairable = false;
  for (const { stage, status } of value.stages) {
    if (stopped && !(repairable && stage === 'repair')) return false;
    if (status === 'PASS') {
      endings = stage === 'review' ? ['COMPLETED'] : [];
      stopped = false;
      repairable = false;
    } else if (status === 'REPAIRED') {
      // A repeated candidate ends NO_PROGRESS even with a REPAIRED response.
      endings = ['NO_PROGRESS'];
      stopped = false;
      repairable = false;
    } else {
      stopped = true;
      repairable = status === 'FAIL' && ['staticChecks', 'tests', 'review'].includes(stage);
      endings = status === 'NO_CHANGE' ? ['NO_PROGRESS'] : status === 'UNAVAILABLE'
        ? ['BLOCKED', 'CANCELLED', 'TIMED_OUT'] : status === 'NOT_RUN' || stage === 'authorizeContext'
          ? ['BLOCKED'] : ['FAILED'];
      if (repairable && value.repairAttempts > 0) endings.push('REPAIR_LIMIT');
    }
  }
  return endings.includes(workflowOutcome);
}

function reportShape(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  if (value.schemaVersion !== 1 || (!RUN_OUTCOMES.includes(value.outcome) && !Array.isArray(value.stages))) return null;
  if (!RUN_OUTCOMES.includes(value.outcome) || !Array.isArray(value.stages)) return { malformed: true };
  const stagesOk = value.stages.every(stage => stage !== null && typeof stage === 'object' && !Array.isArray(stage)
    && typeof stage.stage === 'string' && Object.hasOwn(STAGE_STATUSES, stage.stage)
    && STAGE_STATUSES[stage.stage].includes(stage.status));
  if (!stagesOk) return { malformed: true };
  if (value.repairAttempts !== undefined && (!Number.isSafeInteger(value.repairAttempts) || value.repairAttempts < 0)) return { malformed: true };
  if (value.code !== undefined && value.code !== null && (typeof value.code !== 'string' || value.code.length > 128)) return { malformed: true };
  if (!reportOutcomeConsistent(value)) return { malformed: true };
  return {
    outcome: value.outcome,
    code: value.code ?? null,
    repairAttempts: Number.isSafeInteger(value.repairAttempts) ? value.repairAttempts : null,
    stageCount: value.stages.length,
  };
}

function reportRecord(name, file, limits, fileSystem) {
  const found = statOf(fileSystem, file);
  if (found.error) return { name, kind: 'workflow-report', status: 'FAIL', reason: found.error };
  if (found.stat.isSymbolicLink()) return { name, kind: 'workflow-report', status: 'FAIL', reason: 'SYMLINK' };
  if (!found.stat.isFile()) return { name, kind: 'workflow-report', status: 'FAIL', reason: 'NOT_A_FILE' };
  if (found.stat.size > limits.maxBytes) return { name, kind: 'workflow-report', status: 'FAIL', reason: 'FILE_TOO_LARGE', bytes: found.stat.size };
  const bounded = boundedRead(fileSystem, file, limits.maxBytes);
  if (bounded.error) return { name, kind: 'workflow-report', status: 'FAIL', reason: bounded.error, ...(bounded.bytes === undefined ? {} : { bytes: bounded.bytes }) };
  const { bytes } = bounded;
  let text;
  try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
  catch { return { name, kind: 'json', status: 'SKIP', reason: 'NOT_A_NISI_RECORD' }; }
  let parsed;
  try { parsed = JSON.parse(text); }
  catch { return { name, kind: 'json', status: 'SKIP', reason: 'NOT_A_NISI_RECORD' }; }
  const shape = reportShape(parsed);
  if (shape === null) return { name, kind: 'json', status: 'SKIP', reason: 'NOT_A_NISI_RECORD' };
  if (shape.malformed) return { name, kind: 'workflow-report', status: 'FAIL', reason: 'REPORT_SHAPE' };
  return {
    name, kind: 'workflow-report', status: 'PASS', reason: null, outcome: shape.outcome, code: shape.code,
    repairAttempts: shape.repairAttempts, stageCount: shape.stageCount, bytes: bytes.length,
  };
}

function finish(summary) {
  summary.checked = summary.files.filter(file => file.status !== 'SKIP').length;
  summary.passed = summary.files.filter(file => file.status === 'PASS').length;
  summary.failed = summary.files.filter(file => file.status === 'FAIL').length;
  summary.skipped = summary.files.filter(file => file.status === 'SKIP').length;
  if (summary.failed > 0) summary.reason = 'FILES_FAILED';
  else if (summary.passed > 0) {
    summary.status = 'PASS';
    summary.reason = null;
  } else summary.reason = summary.files.length === 0 ? 'EMPTY' : 'NOTHING_CHECKED';
  summary.files.sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  return summary;
}

function validateJournalTarget(journal, fileSystem, limits) {
  const found = statOf(fileSystem, journal);
  if (found.error) return blank('journal', found.error);
  if (found.stat.isSymbolicLink()) return blank('journal', 'SYMLINK');
  if (!found.stat.isFile()) return blank('journal', 'NOT_A_FILE');
  const summary = blank('journal', null);
  summary.files.push(journalRecord(path.basename(journal), journal, limits, fileSystem));
  return finish(summary);
}

function validateDirectory(dataDir, fileSystem, limits) {
  const found = statOf(fileSystem, dataDir);
  if (found.error) return blank('data-dir', found.error);
  if (found.stat.isSymbolicLink()) return blank('data-dir', 'SYMLINK');
  if (!found.stat.isDirectory()) return blank('data-dir', 'NOT_A_DIRECTORY');
  let names;
  try { names = fileSystem.readdirSync(dataDir); }
  catch { return blank('data-dir', 'READ_FAILED'); }
  const candidates = names.filter(name => name.endsWith('.jsonl') || name.endsWith('.json')).sort();
  if (candidates.length > limits.maxFiles) return blank('data-dir', 'TOO_MANY_FILES');
  const summary = blank('data-dir', null);
  for (const name of candidates) {
    const file = path.resolve(dataDir, name);
    if (relativeName(path.resolve(dataDir), file) === null) {
      summary.files.push({ name, kind: 'journal', status: 'FAIL', reason: 'PATH_ESCAPE' });
      continue;
    }
    summary.files.push(name.endsWith('.jsonl')
      ? journalRecord(name, file, limits, fileSystem)
      : reportRecord(name, file, limits, fileSystem));
  }
  return finish(summary);
}

export function validateData({ journal, dataDir, fs: fileSystem = fs, maxBytes = MAX_FILE_BYTES, maxFiles = MAX_FILES } = {}) {
  const limits = { maxBytes, maxFiles };
  if ((journal === undefined) === (dataDir === undefined)) return blank(null, 'INVALID_INPUT');
  if (typeof maxBytes !== 'number' || typeof maxFiles !== 'number') return blank(journal === undefined ? 'data-dir' : 'journal', 'INVALID_INPUT');
  return journal !== undefined ? validateJournalTarget(journal, fileSystem, limits) : validateDirectory(dataDir, fileSystem, limits);
}
