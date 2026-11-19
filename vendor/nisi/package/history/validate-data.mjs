// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// Read-only check of a Nisi run journal, or of the journals and workflow reports
// in one directory. The summary carries counts, hashes and reason codes. It does
// not carry payloads, entry ids or parse excerpts.

import fs from 'node:fs';
import path from 'node:path';
import { readSerializedJournal } from './run-journal-store-v1.mjs';
import { reopen } from './run-journal-v1.mjs';

export const MAX_FILE_BYTES = 64 * 1024 * 1024;
export const MAX_FILES = 500;
const OUTCOMES = Object.freeze(['COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED', 'TIMED_OUT', 'REPAIR_LIMIT', 'NO_PROGRESS']);

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

function journalRecord(name, file, limits, fileSystem) {
  const found = statOf(fileSystem, file);
  if (found.error) return { name, kind: 'journal', status: 'FAIL', reason: found.error };
  if (found.stat.isSymbolicLink()) return { name, kind: 'journal', status: 'FAIL', reason: 'SYMLINK' };
  if (!found.stat.isFile()) return { name, kind: 'journal', status: 'FAIL', reason: 'NOT_A_FILE' };
  if (found.stat.size > limits.maxBytes) return { name, kind: 'journal', status: 'FAIL', reason: 'FILE_TOO_LARGE', bytes: found.stat.size };
  const read = readSerializedJournal({ path: file, fs: fileSystem });
  if (read.status !== 'READ') return { name, kind: 'journal', status: 'FAIL', reason: read.reason };
  const recovered = reopen(read.serialized);
  const entries = recovered.report.recoveredIds.length;
  if (recovered.report.status !== 'COMPLETE') {
    return { name, kind: 'journal', status: 'FAIL', reason: recovered.report.status, entries, bytes: read.bytes, sha256: read.sha256 };
  }
  return { name, kind: 'journal', status: 'PASS', reason: null, entries, bytes: read.bytes, sha256: read.sha256, verified: read.verified === true };
}

function reportShape(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  if (value.schemaVersion !== 1 || !OUTCOMES.includes(value.outcome) || !Array.isArray(value.stages)) return null;
  const stagesOk = value.stages.every(stage => stage !== null && typeof stage === 'object' && !Array.isArray(stage)
    && typeof stage.stage === 'string' && stage.stage.length > 0 && stage.stage.length <= 128
    && typeof stage.status === 'string' && stage.status.length > 0 && stage.status.length <= 128);
  if (!stagesOk) return { malformed: true };
  if (value.repairAttempts !== undefined && (!Number.isSafeInteger(value.repairAttempts) || value.repairAttempts < 0)) return { malformed: true };
  if (value.code !== undefined && value.code !== null && (typeof value.code !== 'string' || value.code.length > 128)) return { malformed: true };
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
  let bytes;
  try { bytes = fileSystem.readFileSync(file); }
  catch { return { name, kind: 'workflow-report', status: 'FAIL', reason: 'READ_FAILED' }; }
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
