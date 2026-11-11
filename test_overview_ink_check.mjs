import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { EventEmitter } from 'node:events';
import { chromaOf } from './checks/ink-color.mjs';

test('unknown color syntax fails the ink threshold', () => {
  for (const color of ['color-mix(in srgb, red 50%, white)', 'oklch(70% 0.2 30)',
    'color(srgb 1 0 0)', 'hsl(20 100% 50%)', 'red', 'rgb(1, 2)', 'rgb(1, 2, 3) junk']) {
    assert.ok(chromaOf(color) > 18, color);
  }
});

test('rgb colors retain their chroma and transparent colors are ignored', () => {
  for (const color of ['', 'none', 'transparent', 'rgb(242, 242, 242)',
    'rgb(42 42 42)', 'rgb(40% 40% 40%)', 'rgba(255, 0, 0, 0)', 'rgb(255 0 0 / 0%)']) {
    assert.equal(chromaOf(color), 0, color);
  }
  assert.equal(chromaOf('rgb(255, 20, 0)'), 255);
  assert.equal(chromaOf('rgba(255, 0, 0, 0.5)'), 255);
  assert.equal(chromaOf('rgb(255 0 0 / 50%)'), 255);
});

// Execute the actual lifecycle with fake child/browser objects, so these tests
// neither require Playwright nor spawn or kill any operating-system process.
const source = readFileSync(new URL('./checks/overview-ink.mjs', import.meta.url), 'utf8');
const lifecycle = source.slice(source.indexOf('const child = spawn('), source.indexOf('\nif (failures.length)'));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('spawn', 'root', 'chromium', 'chromaOf', 'setTimeout', 'clearTimeout',
  `${lifecycle}\nreturn failures;`);

function harness({ launchError, closeError, pageError, startupError, timeout = false, paints = [] } = {}) {
  const calls = { kill: 0, launch: 0, close: 0, pageClose: 0, schemes: [] };
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.kill = () => { calls.kill++; };
  const timers = new Set();
  const spawn = () => {
    if (!timeout) queueMicrotask(() => {
      if (startupError) child.emit('error', startupError);
      else child.stdout.emit('data', '{"port":12345}\n');
    });
    return child;
  };
  const chromium = { launch: async () => {
    calls.launch++;
    if (launchError) throw launchError;
    return {
      newPage: async ({ colorScheme }) => {
        calls.schemes.push(colorScheme);
        return {
          goto: async () => { if (pageError) throw pageError; },
          waitForTimeout: async () => {},
          evaluate: async () => paints,
          close: async () => { calls.pageClose++; },
        };
      },
      close: async () => { calls.close++; if (closeError) throw closeError; },
    };
  } };
  const setTimeout = callback => {
    timers.add(callback);
    if (timeout) queueMicrotask(callback);
    return callback;
  };
  const clearTimeout = timer => timers.delete(timer);
  return { calls, timers, result: run(spawn, '/unused', chromium, chromaOf, setTimeout, clearTimeout) };
}

test('the cleanup covers Chromium launch failure', async () => {
  const failure = new Error('launch failed');
  const h = harness({ launchError: failure });
  await assert.rejects(h.result, error => error === failure);
  assert.equal(h.calls.launch, 1);
  assert.equal(h.calls.kill, 1);
  assert.equal(h.timers.size, 0);
});

test('the cleanup still kills the child when browser cleanup fails', async () => {
  const failure = new Error('close failed');
  const h = harness({ closeError: failure });
  await assert.rejects(h.result, error => error === failure);
  assert.equal(h.calls.close, 1);
  assert.equal(h.calls.kill, 1);
});

test('page-loop failure closes the browser and kills the child', async () => {
  const failure = new Error('navigation failed');
  const h = harness({ pageError: failure });
  await assert.rejects(h.result, error => error === failure);
  assert.equal(h.calls.close, 1);
  assert.equal(h.calls.kill, 1);
});

test('startup error and port timeout also reach child cleanup', async () => {
  for (const options of [{ startupError: new Error('spawn failed') }, { timeout: true }]) {
    const h = harness(options);
    await assert.rejects(h.result, /spawn failed|observer did not print a port/);
    assert.equal(h.calls.launch, 0);
    assert.equal(h.calls.kill, 1);
    assert.equal(h.timers.size, 0);
  }
});

test('the actual check applies the helper to both overview schemes', async () => {
  const unknown = { prop: 'color', value: 'oklch(70% 0.2 30)', tag: 'P', cls: '' };
  const h = harness({ paints: [
    { prop: 'color', value: 'rgb(242, 242, 242)', tag: 'P', cls: '' }, unknown,
  ] });
  assert.deepEqual(await h.result, ['light', 'dark'].map(scheme => ({ scheme, colored: [unknown] })));
  assert.deepEqual(h.calls.schemes, ['light', 'dark']);
  assert.equal(h.calls.pageClose, 2);
  assert.equal(h.calls.close, 1);
  assert.equal(h.calls.kill, 1);
});
