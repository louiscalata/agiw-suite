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

Commands:
  demo          Run the deterministic workflow example. No model is called.
  local-model   Run the JSON workflow against two configured loopback models.

The local-model command may make inference requests to the endpoint you provide.
`;

const USAGE_ERROR = `Invalid command or arguments. Run "nisi --help" for usage.`;
const MAX_ENDPOINT_LENGTH = 2_048;
const MAX_MODEL_NAME_LENGTH = 128;

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

  if (args.length === 1 && (args[0] === '--help' || args[0] === '-h')) {
    return await emit(stdout, HELP.trimEnd()) ? 0 : 2;
  }
  if (args.length === 1 && (args[0] === '--version' || args[0] === '-V')) {
    return await emit(stdout, packageMetadata.version) ? 0 : 2;
  }

  if (args.length === 1 && args[0] === 'demo') {
    try {
      const runExample = options.runExample ?? (await import(new URL('../examples/workflow.mjs', physicalEntryURL).href)).runExample;
      const { report } = await runExample();
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
