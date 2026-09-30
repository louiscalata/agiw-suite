// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// Workflow Engine contracts for Nisi.
//
// This module has no filesystem, process, network, or model imports. It owns
// input normalization, immutable snapshots, and the small schemas consumed by
// workflow/engine.mjs. Adapter output is data; adapters remain trusted at the
// host boundary and are not proven truthful by these checks.

import { createHash } from 'node:crypto';

export const WORKFLOW_SCHEMA_VERSION = 1;
export const CANDIDATE_SCHEMA_VERSION = 1;
export const TASK_MODES = Object.freeze(['edit', 'review']);
export const ADAPTER_STATUSES = Object.freeze(['PASS', 'FAIL', 'NOT_RUN', 'UNAVAILABLE']);
export const REPAIR_STATUSES = Object.freeze(['REPAIRED', 'NO_CHANGE', 'FAIL', 'NOT_RUN', 'UNAVAILABLE']);
export const RUN_OUTCOMES = Object.freeze([
  'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED', 'TIMED_OUT', 'REPAIR_LIMIT', 'NO_PROGRESS',
]);

export class WorkflowInputError extends Error {
  constructor(code) {
    super(code);
    this.name = 'WorkflowInputError';
    this.code = code;
  }
}

const reject = code => { throw new WorkflowInputError(code); };
const ownKeys = value => JSON.stringify(Object.keys(value).sort());
const isPlainObject = value => value !== null && typeof value === 'object' && !Array.isArray(value) &&
  (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
const isId = value => typeof value === 'string' && /^[a-z][a-z0-9_.-]{0,95}$/u.test(value);
const isHex64 = value => typeof value === 'string' && /^[0-9a-f]{64}$/u.test(value);

export function cloneFreeze(value, seen = new WeakSet(), depth = 0) {
  if (depth > 64) reject('INPUT_DEPTH_LIMIT');
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) reject('INPUT_NUMBER_INVALID');
    return value;
  }
  if (typeof value !== 'object') reject('INPUT_VALUE_INVALID');
  if (seen.has(value)) reject('INPUT_CYCLE');
  seen.add(value);
  let copy;
  const descriptors = Object.getOwnPropertyDescriptors(value);
  if (Reflect.ownKeys(descriptors).some(key => typeof key !== 'string' ||
      !Object.hasOwn(descriptors[key], 'value'))) reject('INPUT_PROPERTY_INVALID');
  if (Array.isArray(value)) {
    if (Reflect.ownKeys(descriptors).length !== value.length + 1) reject('INPUT_ARRAY_INVALID');
    copy = [];
    for (let index = 0; index < value.length; index += 1) {
      if (!Object.hasOwn(descriptors, index)) reject('INPUT_ARRAY_INVALID');
      copy.push(cloneFreeze(descriptors[index].value, seen, depth + 1));
    }
  } else if (isPlainObject(value)) {
    copy = Object.create(null);
    for (const key of Object.keys(descriptors)) {
      if (!descriptors[key].enumerable) reject('INPUT_PROPERTY_INVALID');
      copy[key] = cloneFreeze(descriptors[key].value, seen, depth + 1);
    }
  } else reject('INPUT_OBJECT_INVALID');
  seen.delete(value);
  return Object.freeze(copy);
}

export function stableStringify(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) reject('INPUT_NUMBER_INVALID');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  if (isPlainObject(value)) {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`;
  }
  reject('INPUT_VALUE_INVALID');
}

export function sha256Text(value) {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function relativePath(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 512 || value.includes('\\') ||
      value.startsWith('/') || /^[a-zA-Z]:/u.test(value) || value.includes('\0')) reject('PATH_INVALID');
  const parts = value.split('/');
  if (parts.some(part => part === '' || part === '.' || part === '..')) reject('PATH_TRAVERSAL');
  return parts.join('/');
}

function exactObject(value, expected, code = 'SCHEMA_INVALID') {
  if (!isPlainObject(value) || ownKeys(value) !== JSON.stringify([...expected].sort())) reject(code);
  return value;
}

function normalizeFiles(files, authorId) {
  if (!Array.isArray(files) || files.length === 0 || files.length > 256) reject('CANDIDATE_FILES_INVALID');
  const out = [];
  const seen = new Set();
  let totalBytes = 0;
  for (const raw of files) {
    exactObject(raw, ['path', 'content'], 'CANDIDATE_FILE_SCHEMA');
    const path = relativePath(raw.path);
    if (seen.has(path)) reject('CANDIDATE_DUPLICATE_PATH');
    if (typeof raw.content !== 'string' || raw.content.length > 1_048_576) reject('CANDIDATE_CONTENT_INVALID');
    totalBytes += Buffer.byteLength(raw.content, 'utf8');
    if (totalBytes > 8 * 1_048_576) reject('CANDIDATE_TOO_LARGE');
    seen.add(path);
    out.push({ path, content: raw.content });
  }
  out.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  return { schemaVersion: CANDIDATE_SCHEMA_VERSION, authorId, files: out };
}

export function createCandidate(value, { authorId } = {}) {
  value = cloneFreeze(value);
  exactObject(value, ['files'], 'CANDIDATE_SCHEMA');
  if (!isId(authorId)) reject('AUTHOR_ID_INVALID');
  const normalized = normalizeFiles(value.files, authorId);
  const frozen = cloneFreeze(normalized);
  const identity = { schemaVersion: CANDIDATE_SCHEMA_VERSION, files: frozen.files };
  return Object.freeze({ ...frozen, fingerprint: sha256Text(`nisi/workflow-candidate/v1\0${stableStringify(identity)}`) });
}

function normalizePolicy(value) {
  exactObject(value, ['repairBudget', 'totalDeadlineMs', 'requiredReviewers', 'requireReportStore'], 'POLICY_SCHEMA');
  if (!Number.isSafeInteger(value.repairBudget) || value.repairBudget < 0 || value.repairBudget > 100) reject('REPAIR_BUDGET_INVALID');
  if (!Number.isSafeInteger(value.totalDeadlineMs) || value.totalDeadlineMs < 1 || value.totalDeadlineMs > 86_400_000) reject('DEADLINE_INVALID');
  if (!Number.isSafeInteger(value.requiredReviewers) || value.requiredReviewers < 1 || value.requiredReviewers > 16) reject('REVIEWER_COUNT_INVALID');
  if (typeof value.requireReportStore !== 'boolean') reject('REPORT_STORE_POLICY_INVALID');
  return value;
}

export function createTaskSpecification(value) {
  value = cloneFreeze(value);
  exactObject(value, ['taskId', 'mode', 'language', 'allowedFiles', 'protectedFiles', 'protectedSnapshots', 'acceptanceCriteria', 'policy'], 'TASK_SCHEMA');
  if (!isId(value.taskId)) reject('TASK_ID_INVALID');
  if (!TASK_MODES.includes(value.mode)) reject('TASK_MODE_INVALID');
  if (typeof value.language !== 'string' || value.language.trim().length === 0 || value.language.length > 64) reject('LANGUAGE_INVALID');
  if (!Array.isArray(value.allowedFiles) || value.allowedFiles.length === 0 || value.allowedFiles.length > 1024) reject('ALLOWED_FILES_INVALID');
  const allowedFiles = [...new Set(value.allowedFiles.map(relativePath))].sort();
  if (allowedFiles.length !== value.allowedFiles.length) reject('ALLOWED_FILES_DUPLICATE');
  if (!Array.isArray(value.protectedFiles)) reject('PROTECTED_FILES_INVALID');
  const protectedFiles = [...new Set(value.protectedFiles.map(relativePath))].sort();
  if (protectedFiles.length !== value.protectedFiles.length) reject('PROTECTED_FILES_DUPLICATE');
  if (protectedFiles.some(path => !allowedFiles.includes(path))) reject('PROTECTED_FILE_OUT_OF_SCOPE');
  if (!isPlainObject(value.protectedSnapshots)) reject('PROTECTED_SNAPSHOTS_INVALID');
  if (ownKeys(value.protectedSnapshots) !== JSON.stringify(protectedFiles)) reject('PROTECTED_SNAPSHOTS_SCHEMA');
  for (const path of protectedFiles) if (!isHex64(value.protectedSnapshots[path])) reject('PROTECTED_SNAPSHOT_INVALID');
  if (!Array.isArray(value.acceptanceCriteria) || value.acceptanceCriteria.length === 0 || value.acceptanceCriteria.length > 128 ||
      !value.acceptanceCriteria.every(item => typeof item === 'string' && item.trim().length > 0 && item.length <= 2048)) reject('ACCEPTANCE_CRITERIA_INVALID');
  const policy = normalizePolicy(value.policy);
  return cloneFreeze({ ...value, allowedFiles, protectedFiles, protectedSnapshots: { ...value.protectedSnapshots }, policy });
}

export function validateCandidateForTask(candidate, task, authorId) {
  const normalized = createCandidate(candidate, { authorId });
  for (const file of normalized.files) {
    if (!task.allowedFiles.includes(file.path)) reject('CANDIDATE_FILE_OUT_OF_SCOPE');
    if (task.protectedFiles.includes(file.path) &&
        sha256Text(file.content) !== task.protectedSnapshots[file.path]) reject('PROTECTED_FILE_CHANGED');
  }
  return normalized;
}

export function validateFindings(findings, stage) {
  if (!Array.isArray(findings) || findings.length > 256) reject(`${stage.toUpperCase()}_FINDINGS_INVALID`);
  for (const finding of findings) {
    exactObject(finding, ['code', 'message'], `${stage.toUpperCase()}_FINDING_SCHEMA`);
    if (typeof finding.code !== 'string' || finding.code.trim().length === 0 || finding.code.length > 128 ||
        typeof finding.message !== 'string' || finding.message.trim().length === 0 || finding.message.length > 2048) reject(`${stage.toUpperCase()}_FINDING_INVALID`);
  }
  return findings;
}

export function validateAdapterIdentity(adapter, label) {
  if (!isPlainObject(adapter) || !isId(adapter.id)) reject(`${label.toUpperCase()}_IDENTITY_INVALID`);
  return adapter.id;
}

export function freezeReport(value) { return cloneFreeze(value); }
