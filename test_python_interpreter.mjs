import assert from 'node:assert/strict';
import test from 'node:test';
import {spawnSync} from 'node:child_process';
import {resolvePython} from './test_support/python-interpreter.mjs';

test('Python override is an executable path, including spaces', () => {
  const calls = [];
  const result = resolvePython({skip: assert.fail}, {env: {PYTHON: 'C:/test tools/python.exe'}, platform: 'win32',
    probe(command, args) { calls.push([command, args]); return {status: 0}; }});
  assert.deepEqual(result, {command: 'C:/test tools/python.exe', args: []});
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'C:/test tools/python.exe');
});

test('Python resolver falls through unavailable overrides to the Windows py -3 launcher', () => {
  const calls = [];
  const result = resolvePython({skip: assert.fail}, {env: {PYTHON: 'missing-python'}, platform: 'win32',
    probe(command, args) { calls.push([command, args]); return {status: command === 'py' ? 0 : 1}; }});
  assert.deepEqual(calls.map(([command]) => command), ['missing-python', 'python3.14', 'python3', 'py']);
  assert.deepEqual(result, {command: 'py', args: ['-3']});
  assert.equal(calls.at(-1)[1][0], '-3');
});

test('Python resolver uses python3 when python3.14 is absent', () => {
  const result = resolvePython({skip: assert.fail}, {env: {}, platform: 'darwin',
    probe(command) { return command === 'python3' ? {status: 0} : {error: new Error('ENOENT'), status: null}; }});
  assert.deepEqual(result, {command: 'python3', args: []});
});

test('Python resolver skips explicitly only after every platform candidate fails', () => {
  for (const platform of ['darwin', 'linux', 'win32']) {
    const calls = [], skips = [];
    const result = resolvePython({skip: reason => skips.push(reason)}, {env: {}, platform,
      probe(command) { calls.push(command); return {status: null, error: new Error('ENOENT')}; }});
    assert.equal(result, null);
    assert.deepEqual(calls, platform === 'win32' ? ['python3.14', 'python3', 'py'] : ['python3.14', 'python3']);
    assert.equal(skips.length, 1);
    assert.match(skips[0], /No Python 3 interpreter available/);
  }
});

test('POSIX publisher generators skip on win32 before launching Python', () => {
  const expected = [
    ['fix9 generative invariant: Python publishers -> JSON -> actual web renderer, two-sided exact/degraded/frozen properties and every single/pair degradation',
      'Requires POSIX fcntl through the Mac telemetry publisher'],
    ['Linux Python publisher -> JSON -> real web peer renderer: two-sided evidence contract',
      'Requires POSIX os.statvfs in the Linux publisher fixture'],
    ['generative invariant: real Python probes/publisher -> JSON -> dashboard renderer',
      'Requires POSIX fcntl imported by telemetry.py in the Mac publisher'],
    ['generated boundary dimensions: caps, journal lanes, Windows producers and process grammar',
      'Requires POSIX fcntl imported by telemetry.py in the Mac publisher'],
    ['two-sided generator: degraded certainty, healthy exactness, frozen capture time',
      'Requires POSIX fcntl imported by telemetry.py in the Mac publisher'],
    ['Python publisher → JSON → web renderer: single/pair degradations, healthy exact values and frozen capture times',
      'Requires POSIX fcntl imported by server.py in the Mac publisher'],
  ];
  const env = {...process.env, PATH: '', PYTHON: 'no-python-in-this-fixture'};
  delete env.NODE_TEST_CONTEXT;
  const child = spawnSync(process.execPath, ['--test-reporter=tap', '--input-type=module', '--eval',
    "Object.defineProperty(process, 'platform', {value: 'win32'}); " +
    "await import('./test_fix9_invariant.mjs'); await import('./test_linux_invariant.mjs'); " +
    "await import('./test_pipeline_invariant.mjs'); await import('./test_pipeline_invariants.mjs');"],
  {cwd: new URL('.', import.meta.url), env, encoding: 'utf8', timeout: 10000});
  assert.equal(child.status, 0, child.stderr || child.stdout);
  assert.match(child.stdout, /# tests 6\b/);
  assert.match(child.stdout, /# skipped 6\b/);
  assert.match(child.stdout, /# pass 0\b/);
  const results = child.stdout.split('\n').filter(line => /^ok \d+ - /.test(line))
    .map(line => line.replace(/^ok \d+ - /, ''));
  assert.deepEqual(results.sort(), expected.map(([name, reason]) => `${name} # SKIP ${reason}`).sort());
});
