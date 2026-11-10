/** Headless Chrome pass: install is not required. Serves this kit, then checks the page. */
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { installKit, startServer } from '../install.mjs';

const kitDemo = dirname(fileURLToPath(import.meta.url));
const chromeBin = existsSync('/usr/bin/google-chrome-stable') ? '/usr/bin/google-chrome-stable' : 'google-chrome';
const outDir = process.argv.includes('--out')
  ? process.argv[process.argv.indexOf('--out') + 1]
  : join(tmpdir(), 'jaryo-shots');

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.next = 0;
    this.pending = new Map();
    this.waiters = [];
    ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const pending = this.pending.get(message.id);
        this.pending.delete(message.id);
        clearTimeout(pending.timer);
        if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
        else pending.resolve(message.result || {});
      } else if (message.method) {
        this.waiters = this.waiters.filter((waiter) => {
          if (waiter.method !== message.method) return true;
          clearTimeout(waiter.timer);
          waiter.resolve(message.params || {});
          return false;
        });
      }
    });
  }

  send(method, params = {}, timeout = 20000) {
    const id = ++this.next;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`timeout ${method}`));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  evaluate(expression) {
    return this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }).then((result) => {
      if (result.exceptionDetails) {
        throw new Error(result.exceptionDetails.text || JSON.stringify(result.exceptionDetails));
      }
      return result.result.value;
    });
  }
}

async function waitForJson(port) {
  const started = Date.now();
  while (Date.now() - started < 15000) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (response.ok) return response.json();
    } catch (_) { /* chrome still starting */ }
    await sleep(100);
  }
  throw new Error('Chrome did not open a debugging port');
}

async function waitFor(cdp, expression, timeout = 8000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const value = await cdp.evaluate(expression);
    if (value) return value;
    await sleep(80);
  }
  throw new Error(`timed out waiting for ${expression}`);
}

async function shot(cdp, file) {
  const image = await cdp.send('Page.captureScreenshot', { format: 'png' });
  await writeFile(file, Buffer.from(image.data, 'base64'));
}

async function prepare(cdp, width, height, theme, motion) {
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: width < 600,
  });
  await cdp.send('Emulation.setEmulatedMedia', {
    features: [
      { name: 'prefers-color-scheme', value: theme },
      { name: 'prefers-reduced-motion', value: motion },
    ],
  });
}

async function openPage(cdp, url) {
  await cdp.send('Page.navigate', { url });
  await waitFor(cdp, `document.readyState === 'complete'`);
}

const viewports = [
  ['desktop', 1280, 800],
  ['mobile', 390, 844],
  ['narrow', 280, 640],
];

async function captureBefore(cdp, directory) {
  const saved = [];
  const root = '/tmp/jaryo-before';
  if (!existsSync(join(root, 'light.html'))) return saved;
  for (const theme of ['light', 'dark']) {
    const html = await readFile(join(root, `${theme}.html`), 'utf8');
    await writeFile(join(root, `${theme}.html`), html.replace('href="/primitives.css"', 'href="./primitives.css"'));
  }
  for (const theme of ['light', 'dark']) {
    for (const [name, width, height] of viewports) {
      await prepare(cdp, width, height, theme, 'no-preference');
      await openPage(cdp, `file://${root}/${theme}.html`);
      const file = join(directory, `before-${theme}-${name}.png`);
      await shot(cdp, file);
      saved.push(file);
    }
  }
  return saved;
}

async function main() {
  await mkdir(outDir, { recursive: true });
  const installed = await mkdtemp(join(tmpdir(), 'jaryo-smoke-'));
  installKit(installed);
  const running = await startServer(0, installed);
  const debugPort = await freePort();
  const profile = await mkdtemp(join(tmpdir(), 'jaryo-chrome-'));
  const chrome = spawn(chromeBin, [
    '--headless=new',
    '--use-mock-keychain',
    '--password-store=basic',
    '--disable-gpu',
    '--no-first-run',
    '--disable-dev-shm-usage',
    '--disable-extensions',
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`,
    'about:blank',
  ], { stdio: 'ignore' });
  const report = {
    install: `node jaryo/cli.mjs install --dest ${installed}`,
    demo: `node ${installed}/cli.mjs demo --port ${running.port}`,
    url: `http://127.0.0.1:${running.port}/demo/index.html`,
    screenshots: [],
    checks: [],
  };
  let failed = false;
  const fail = (name, detail) => {
    failed = true;
    report.checks.push({ name, ok: false, detail });
  };
  const pass = (name, detail) => report.checks.push({ name, ok: true, detail });
  try {
    await waitForJson(debugPort);
    const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
    const page = targets.find((target) => target.type === 'page');
    if (!page) throw new Error('Chrome opened no page');
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve);
      ws.addEventListener('error', () => reject(new Error('Chrome socket failed')));
    });
    const cdp = new Cdp(ws);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    report.screenshots.push(...await captureBefore(cdp, outDir));

    for (const theme of ['light', 'dark']) {
      for (const [name, width, height] of viewports) {
        await prepare(cdp, width, height, theme, 'no-preference');
        await openPage(cdp, `${report.url}?theme=${theme}`);
        await waitFor(cdp, `document.querySelectorAll('.jk-station').length === 4`);
        const overflow = await cdp.evaluate(`(() => ({
          scroll: document.documentElement.scrollWidth,
          client: document.documentElement.clientWidth,
          stations: document.querySelectorAll('.jk-station').length,
          title: document.getElementById('jk-pipeline-title').textContent
        }))()`);
        const file = join(outDir, `after-${theme}-${name}.png`);
        await shot(cdp, file);
        report.screenshots.push(file);
        if (overflow.scroll > overflow.client + 1) fail(`overflow ${theme} ${name}`, overflow);
        else pass(`overflow ${theme} ${name}`, overflow);
        if (overflow.title !== 'Sample lane' || overflow.stations !== 4) fail(`pipeline ${theme} ${name}`, overflow);
      }
      await prepare(cdp, 1280, 800, theme, 'no-preference');
      await openPage(cdp, `${report.url}?theme=${theme}`);
      await waitFor(cdp, `document.querySelectorAll('.jk-station').length === 4`);
      await cdp.evaluate(`document.getElementById('states').scrollIntoView()`);
      await sleep(50);
      const file = join(outDir, `after-${theme}-desktop-states.png`);
      await shot(cdp, file);
      report.screenshots.push(file);
    }

    await prepare(cdp, 1280, 800, 'light', 'no-preference');
    await cdp.send('Emulation.setFocusEmulationEnabled', { enabled: true });
    await openPage(cdp, `${report.url}?theme=light`);
    await waitFor(cdp, `document.querySelectorAll('.jk-station').length === 4`);
    const keys = await cdp.evaluate(`(() => {
      const first = document.querySelector('.jk-station');
      first.focus();
      first.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
      const selected = document.querySelector('.jk-station[aria-pressed="true"]');
      const button = document.querySelector('.jk-button:not([disabled]):not([aria-disabled="true"])');
      button.focus({ focusVisible: true });
      const live = getComputedStyle(button).outlineStyle;
      const forced = getComputedStyle(document.querySelector('.jk-force-focus')).outlineStyle;
      const primary = getComputedStyle(document.querySelector('.jk-button-primary'));
      const tab = document.getElementById('jk-tab-holding');
      tab.focus();
      tab.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
      return {
        station: selected && selected.dataset.station,
        live,
        forced,
        primaryColor: primary.color,
        primaryBg: primary.backgroundColor,
        tab: document.querySelector('.jk-tab[aria-selected="true"]').textContent
      };
    })()`);
    if (keys.station !== 'check') fail('station keys', keys);
    else pass('station keys', keys);
    if (keys.live !== 'dashed' || keys.forced !== 'dashed') fail('focus ring', keys);
    else pass('focus ring', keys);
    if (keys.primaryColor === keys.primaryBg) fail('primary contrast', keys);
    else pass('primary contrast', keys);
    if (keys.tab !== 'Blocked') fail('tab keys', keys);
    else pass('tab keys', keys);

    await prepare(cdp, 1280, 800, 'dark', 'reduce');
    await openPage(cdp, `${report.url}?theme=dark`);
    await waitFor(cdp, `document.querySelector('.jk-belt')`);
    const motion = await cdp.evaluate(`getComputedStyle(document.querySelector('.jk-belt')).animationName`);
    if (motion !== 'none') fail('reduced motion', motion);
    else pass('reduced motion', motion);

    let axeSource;
    try {
      const response = await fetch('https://cdn.jsdelivr.net/npm/axe-core@4.10.3/axe.min.js');
      if (!response.ok) throw new Error(String(response.status));
      axeSource = await response.text();
    } catch (error) {
      fail('axe download', error.message);
      axeSource = '';
    }
    if (axeSource) {
      for (const theme of ['light', 'dark']) {
        await prepare(cdp, theme === 'light' ? 1280 : 280, theme === 'light' ? 800 : 640, theme, 'no-preference');
        await openPage(cdp, `${report.url}?theme=${theme}`);
        await waitFor(cdp, `document.querySelectorAll('.jk-station').length === 4`);
        await cdp.send('Runtime.evaluate', { expression: axeSource });
        const violations = await cdp.evaluate(`axe.run(document).then((result) => result.violations.map((item) => ({
          id: item.id, impact: item.impact, help: item.help, nodes: item.nodes.slice(0, 4).map((node) => node.target.join(' '))
        })))`);
        const serious = violations.filter((item) => item.impact === 'serious' || item.impact === 'critical');
        if (serious.length > 0) fail(`axe ${theme}`, serious);
        else pass(`axe ${theme}`, violations);
      }
    }
  } finally {
    chrome.kill('SIGKILL');
    await new Promise((resolve) => setTimeout(resolve, 300));
    await new Promise((resolve) => running.server.close(() => resolve()));
    await rm(profile, { recursive: true, force: true });
    await rm(installed, { recursive: true, force: true });
    await writeFile(join(outDir, 'smoke.json'), `${JSON.stringify(report, null, 2)}\n`);
  }
  console.log(JSON.stringify({ outDir, failed, checks: report.checks, screenshots: report.screenshots }, null, 2));
  if (failed) process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
