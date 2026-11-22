import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

test('the warning preload fails Node test children and inherited workers', () => {
  const directory = mkdtempSync(join(tmpdir(), 'agiw-node-warnings-'));
  const preload = fileURLToPath(new URL('./test_support/warnings-as-errors.cjs', import.meta.url));
  const environment = process.platform === 'win32' ? { SystemRoot: process.env.SystemRoot } : {};
  try {
    const fixture = join(directory, 'warning.mjs');
    const emit = "process.emitWarning('fixture warning', { code: 'AGIW_TEST_WARNING' });";
    const invoke = (...options) => spawnSync(process.execPath, [
      ...options, '--test', fixture,
    ], { cwd: directory, env: environment, encoding: 'utf8', timeout: 10000 });

    for (const source of [emit, `
      import { Worker } from 'node:worker_threads';
      const worker = new Worker(${JSON.stringify(emit)}, { eval: true });
      await new Promise((resolve, reject) => {
        worker.once('exit', resolve);
        worker.once('error', reject);
      });
    `]) {
      writeFileSync(fixture, source);
      assert.equal(invoke().status, 0);
      const result = invoke('--require', preload);
      assert.equal(result.status, 1, result.stderr);
      assert.match(result.stdout + result.stderr, /AGIW_TEST_WARNING/);
    }

    writeFileSync(fixture, "console.warn('fixture descriptive stderr');");
    const result = invoke('--require', preload);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout + result.stderr, /fixture descriptive stderr/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
