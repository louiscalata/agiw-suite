#!/usr/bin/env node
// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
import { realpathSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const MINIMUM_NODE_MAJOR = 22;
const runtimeMajor = Number.parseInt(process.versions.node.split('.')[0], 10);
const physicalEntry = realpathSync(fileURLToPath(import.meta.url));
const physicalEntryURL = pathToFileURL(physicalEntry);
const packageMetadata = runtimeMajor >= MINIMUM_NODE_MAJOR
  ? createRequire(physicalEntryURL)('../package.json')
  : { version: 'unavailable' };

const HELP = `Nisi ${packageMetadata.version}

Usage:
  nisi --help
  nisi --version
  nisi demo
  nisi local-model ENDPOINT AUTHOR_MODEL REVIEWER_MODEL
  nisi validate --journal FILE
  nisi validate --data-dir DIR

Commands:
  demo          Run the deterministic workflow example. No model is called.
  local-model   Run the JSON workflow against two configured loopback models.
  validate      Check one run journal, or the .jsonl journals and workflow reports
                in one directory. NISI_JOURNAL or NISI_DATA_DIR may replace the
                flag. The summary has counts and reason codes, not payloads.

Use an HTTP endpoint at literal 127.0.0.1 or [::1], without credentials, query,
or fragment. Configure two different model names.
The local-model command may make inference requests to the endpoint you provide.
`;

const USAGE_ERROR = `Invalid command or arguments. Run "nisi --help" for usage.`;
const MAX_ENDPOINT_LENGTH = 2_048;
const MAX_MODEL_NAME_LENGTH = 128;

// Transport exceptions may supply arbitrary codes; never print them verbatim.
const FAILURE_CODES = new Set([
  'ABORTED', 'AUTHOR_ID_INVALID', 'CANDIDATE_SCHEMA', 'CANDIDATE_FILES_INVALID',
  'CANDIDATE_FILE_SCHEMA', 'CANDIDATE_CONTENT_INVALID', 'CANDIDATE_DUPLICATE_PATH',
  'CANDIDATE_TOO_LARGE', 'PATH_INVALID', 'PATH_TRAVERSAL',
  'INPUT_DEPTH_LIMIT', 'INPUT_SIZE_LIMIT', 'INPUT_NUMBER_INVALID', 'INPUT_VALUE_INVALID', 'INPUT_CYCLE',
  'INPUT_PROPERTY_INVALID', 'INPUT_ARRAY_INVALID', 'INPUT_OBJECT_INVALID',
  'LOCAL_CHAT_SIGNAL_INVALID', 'LOCAL_CHAT_REASONING_EFFORT_INVALID', 'LOCAL_CHAT_CHOICES_INVALID', 'LOCAL_CHAT_EMPTY_RESPONSE',
  'LOCAL_CHAT_FINISH_REFUSED', 'LOCAL_CHAT_JSON_DUPLICATE_KEY',
  'LOCAL_CHAT_JSON_INVALID', 'LOCAL_CHAT_JSON_UNICODE', 'LOCAL_CHAT_MODEL_MISMATCH',
  'LOCAL_CHAT_NOTE_INVALID', 'LOCAL_CHAT_OUTPUT_SCHEMA', 'LOCAL_CHAT_DRAFT_INVALID',
  'LOCAL_CHAT_REPAIR_INVALID', 'LOCAL_CHAT_FINDINGS_INVALID', 'LOCAL_CHAT_FINDING_SCHEMA',
  'LOCAL_CHAT_FINDING_INVALID', 'LOCAL_CHAT_REQUEST_TOO_LARGE', 'LOCAL_CHAT_RESPONSE_INVALID',
  'LOCAL_CHAT_RESPONSE_TOO_LARGE', 'LOCAL_CHAT_RESPONSE_UNAVAILABLE', 'LOCAL_CHAT_RESPONSE_UTF8_INVALID',
  'LOCAL_CHAT_TIMEOUT', 'LOCAL_CHAT_UNAVAILABLE', 'LOCAL_CHAT_USAGE_INVALID',
]);
function adapterFailures(result) {
  const failures = [];
  for (const role of ['author', 'reviewer']) {
    const receipts = result?.[`${role}Receipts`];
    if (!Array.isArray(receipts)) continue;
    const receipt = receipts.findLast(item => item?.status === 'UNAVAILABLE');
    if (receipt && FAILURE_CODES.has(receipt.code) && ['draft', 'repair', 'review'].includes(receipt.operation)) {
      failures.push({ role, operation: receipt.operation, code: receipt.code });
    }
  }
  return failures;
}

function writeLine(stream, value) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const cleanup = ({ retainErrorListener = false } = {}) => {
      if (!retainErrorListener) stream.removeListener('error', onError);
      stream.removeListener('close', onClose);
    };
    const finish = (error, retainErrorListener = false) => {
      if (settled) return;
      settled = true;
      cleanup({ retainErrorListener });
      if (error) reject(error);
      else resolve();
    };
    const onError = error => finish(error ?? new Error('Output stream failed.'));
    const onClose = () => finish(new Error('Output stream closed before the write completed.'));
    stream.once('error', onError);
    stream.once('close', onClose);
    try {
      stream.write(`${value}\n`, error => finish(error, Boolean(error)));
    } catch (error) {
      finish(error);
    }
  });
}

async function emit(stream, value) {
  try {
    await writeLine(stream, value);
    return true;
  } catch {
    return false;
  }
}

function summarize(report) {
  return {
    outcome: report?.outcome ?? 'ERROR',
    code: report?.code ?? null,
    repairAttempts: Number.isSafeInteger(report?.repairAttempts) ? report.repairAttempts : 0,
  };
}

export async function runCli(args, options = {}) {
  const stdout = options.stdout ?? process.stdout;
  const stderr = options.stderr ?? process.stderr;
  const nodeVersion = options.nodeVersion ?? process.versions.node;
  const currentMajor = typeof nodeVersion === 'string'
    ? Number.parseInt(nodeVersion.split('.')[0], 10)
    : Number.NaN;
  if (!Number.isSafeInteger(currentMajor) || currentMajor < MINIMUM_NODE_MAJOR) {
    await emit(stderr, 'Nisi requires Node.js 22 or newer.');
    return 2;
  }
  if (!Array.isArray(args) || args.some(value => typeof value !== 'string')) {
    await emit(stderr, USAGE_ERROR);
    return 2;
  }

  if (args.length === 0 || args.length === 1 && (args[0] === '--help' || args[0] === '-h')) {
    return await emit(stdout, HELP.trimEnd()) ? 0 : 2;
  }
  if (args.length === 1 && (args[0] === '--version' || args[0] === '-V')) {
    return await emit(stdout, packageMetadata.version) ? 0 : 2;
  }

  if (args[0] === 'validate') {
    // Resolve from the real CLI path. --preserve-symlinks-main would otherwise
    // look for ../history next to the symlink.
    const { parseValidateArgs, validateData } = await import(new URL('../history/validate-data.mjs', physicalEntryURL).href);
    const parsed = parseValidateArgs(args.slice(1), options.env ?? process.env);
    if (!parsed.ok) {
      await emit(stderr, USAGE_ERROR);
      return 2;
    }
    let summary;
    try { summary = validateData(parsed); }
    catch { return await emit(stderr, 'The journal check failed.') ? 1 : 2; }
    if (!await emit(stdout, JSON.stringify(summary, null, 2))) return 2;
    return summary.status === 'PASS' ? 0 : 1;
  }

  if (args.length === 1 && args[0] === 'demo') {
    try {
      const runExample = options.runExample ?? (await import(new URL('../examples/workflow.mjs', physicalEntryURL).href)).runExample;
      const { report } = await runExample({ keepReport: false });
      const written = await emit(stdout, JSON.stringify({ ...summarize(report), reportStored: report?.reportStored === true, modelCalls: 0 }, null, 2));
      if (!written) return 2;
      return report?.outcome === 'COMPLETED' && report?.reportStored === true ? 0 : 1;
    } catch {
      return await emit(stderr, 'The deterministic workflow demo failed.') ? 1 : 2;
    }
  }

  if (args[0] === 'local-model' && args.length === 4) {
    const [, endpoint, authorModel, reviewerModel] = args;
    if (endpoint.length > MAX_ENDPOINT_LENGTH || authorModel.length > MAX_MODEL_NAME_LENGTH || reviewerModel.length > MAX_MODEL_NAME_LENGTH) {
      await emit(stderr, USAGE_ERROR);
      return 2;
    }
    let local;
    let config;
    try {
      local = options.localModel ?? await import(new URL('../examples/local-model-workflow.mjs', physicalEntryURL).href);
      config = local.parseLocalModelCLI([endpoint, authorModel, reviewerModel]);
    } catch {
      await emit(stderr, USAGE_ERROR);
      return 2;
    }
    try {
      const result = await local.runLocalModelExample(config);
      const report = result?.report;
      const output = {
        ...summarize(report),
        authorCalls: Array.isArray(result?.authorReceipts) ? result.authorReceipts.length : 0,
        reviewerCalls: Array.isArray(result?.reviewerReceipts) ? result.reviewerReceipts.length : 0,
      };
      const failures = adapterFailures(result);
      if (failures.length) output.adapterFailures = failures;
      if (!await emit(stdout, JSON.stringify(output, null, 2))) return 2;
      return report?.outcome === 'COMPLETED' ? 0 : 1;
    } catch {
      // Deliberately omit provider errors because they can contain configured
      // endpoint or model input. Never echo CLI arguments in diagnostics.
      return await emit(stderr, 'The local-model workflow failed. Check the local endpoint and model configuration.') ? 1 : 2;
    }
  }

  await emit(stderr, USAGE_ERROR);
  return 2;
}

let invokedAsMain = false;
try {
  invokedAsMain = Boolean(process.argv[1]) && realpathSync(process.argv[1]) === physicalEntry;
} catch {
  // A missing argv path means this module was imported, not invoked.
}
if (invokedAsMain) {
  process.exitCode = await runCli(process.argv.slice(2));
}
