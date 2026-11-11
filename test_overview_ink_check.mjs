import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { EventEmitter } from 'node:events';
import { chromaOf, isWebAccent } from './checks/ink-color.mjs';
import { overviewGeometry } from './checks/overview-geometry.mjs';

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
const run = new AsyncFunction('spawn', 'root', 'chromium', 'chromaOf', 'isWebAccent', 'overviewGeometry', 'setTimeout', 'clearTimeout',
  `${lifecycle}\nreturn failures;`);

function harness({ launchError, closeError, pageError, tabWaitError, startupError, timeout = false, paints = [], geometry = [] } = {}) {
  const calls = { kill: 0, launch: 0, close: 0, pageClose: 0, cases: [], urls: [], tabWaits: [] };
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
  const chromium = { launch: async options => {
    calls.launch++;
    calls.launchOptions = options;
    if (launchError) throw launchError;
    return {
      newPage: async ({ colorScheme, viewport }) => {
        calls.cases.push({ scheme: colorScheme, width: viewport.width });
        return {
          goto: async url => { calls.urls.push(url); if (pageError) throw pageError; },
          waitForTimeout: async () => {},
          locator: selector => ({ nth: index => ({ waitFor: async options => {
            calls.tabWaits.push({ selector, index, ...options });
            if (tabWaitError) throw tabWaitError;
          } }) }),
          evaluate: async callback => callback === overviewGeometry ? geometry : paints,
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
  return { calls, timers, result: run(spawn, '/unused', chromium, chromaOf, isWebAccent, overviewGeometry, setTimeout, clearTimeout) };
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

test('the actual check keeps ink validation at both widths and schemes', async () => {
  const unknown = { prop: 'color', value: 'oklch(70% 0.2 30)', tag: 'P', cls: '' };
  const h = harness({ paints: [
    { prop: 'color', value: 'rgb(242, 242, 242)', tag: 'P', cls: '' }, unknown,
  ] });
  const cases = [1280, 375].flatMap(width => ['light', 'dark'].map(scheme => ({ width, scheme })));
  assert.deepEqual(await h.result, cases.map(item => ({ ...item, colored: [unknown] })));
  assert.deepEqual(h.calls.cases, cases.map(({ width, scheme }) => ({ scheme, width })));
  assert.equal(h.calls.pageClose, 4);
  assert.equal(h.calls.close, 1);
  assert.equal(h.calls.kill, 1);
});

test('geometry failures fail every case and Chromium remains isolated', async () => {
  const geometry = [{ target: 'Model tuning', reason: 'text clipped on x' }];
  const h = harness({ geometry });
  const cases = [1280, 375].flatMap(width => ['light', 'dark'].map(scheme => ({ width, scheme })));
  assert.deepEqual(await h.result, cases.map(item => ({ ...item, geometry })));
  assert.deepEqual(h.calls.launchOptions.args, ['--use-mock-keychain', '--password-store=basic']);
  assert.equal(h.calls.urls.filter(url => url.endsWith('/')).length, 4);
  assert.equal(h.calls.urls.filter(url => url.endsWith('/?demo=1')).length, 4);
  assert.deepEqual(h.calls.tabWaits, Array(4).fill({ selector: '#monitorTabs .j-tab-label', index: 1, state: 'visible', timeout: 10000 }));
  assert.equal(h.calls.close, 1);
  assert.equal(h.calls.kill, 1);
});


test('only the exact opaque web accent passes the accent parser', () => {
  for (const value of ['rgb(194, 26, 39)', 'rgb(194 26 39)', 'rgba(194, 26, 39, 1)', 'rgb(194 26 39 / 100%)']) {
    assert.equal(isWebAccent(value), true, value);
  }
  for (const value of ['rgb(195, 26, 39)', 'rgba(194, 26, 39, 0.5)', 'rgb(194 26 39 / invalid)',
    'rgb(194 26)', 'rgb(194,26,39,1,0)', 'red', 'color(srgb 0.76 0.1 0.15)']) {
    assert.equal(isWebAccent(value), false, value);
  }
});

test('the actual ink check permits display accent but rejects colored readings and unknown colors', async () => {
  const rejected = [
    { value: 'rgb(194, 26, 39)', accentAllowed: false },
    { value: 'rgb(195, 26, 39)', accentAllowed: true },
    { value: 'color(srgb 1 0 0)', accentAllowed: true },
  ];
  const h = harness({ paints: [{ value: 'rgb(194, 26, 39)', accentAllowed: true }, ...rejected] });
  assert.deepEqual((await h.result).map(item => item.colored), Array(4).fill(rejected));
  assert.equal(h.calls.kill, 1);
});


test('missing demo tab fails readiness and still cleans up the browser and server', async () => {
  const failure = new Error('second visible tab timed out');
  const h = harness({ tabWaitError: failure });
  await assert.rejects(h.result, error => error === failure);
  assert.equal(h.calls.tabWaits.length, 1);
  assert.equal(h.calls.close, 1);
  assert.equal(h.calls.kill, 1);
});
