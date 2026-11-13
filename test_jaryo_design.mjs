import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { EventEmitter } from 'node:events';
import * as fs from 'node:fs';
import { existsSync, readFileSync, realpathSync, symlinkSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import * as path from 'node:path';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { runInNewContext } from 'node:vm';
import { applyTheme, escapeHtml, nextIndex, pipelineMarkup, readThemeChoice } from './jaryo/behavior.mjs';
import { checkKit, contrastRatio } from './jaryo/contract.mjs';
import { parseArgs, run } from './jaryo/cli.mjs';
import { kitRoot } from './jaryo/install.mjs';
import { frameMessage, handleMessage, readFrames } from './jaryo/mcp.mjs';
import { resolveTheme } from './jaryo/theme.mjs';

const kit = kitRoot();
const repo = basename(kit) === 'jaryo' ? dirname(kit) : dirname(fileURLToPath(import.meta.url));

test('black and white contrast and the theme resolver stay strict', () => {
  assert.ok(contrastRatio('#000000', '#ffffff') >= 7);
  assert.equal(resolveTheme('system', true).resolved, 'light');
  assert.equal(resolveTheme('system', false).resolved, 'dark');
  assert.equal(resolveTheme('light', false).resolved, 'light');
  assert.equal(resolveTheme('graphite', true).mode, 'system');
  assert.equal(readThemeChoice('?theme=dark', 'light'), 'dark');
  assert.equal(readThemeChoice('', 'light'), 'light');
  assert.equal(readThemeChoice('?theme=graphite', 'dark'), 'dark');
  assert.equal(readThemeChoice('', null), 'system');
  const doc = { documentElement: { dataset: {} } };
  assert.equal(applyTheme(doc, 'light', false).resolved, 'light');
  assert.equal(doc.documentElement.dataset.theme, 'light');
  assert.equal(doc.documentElement.dataset.themeChoice, 'light');
});

test('station keys wrap, and pipeline text is escaped', () => {
  assert.equal(nextIndex(0, 4, 'ArrowRight'), 1);
  assert.equal(nextIndex(0, 4, 'ArrowLeft'), 3);
  assert.equal(nextIndex(3, 4, 'ArrowDown'), 0);
  assert.equal(nextIndex(2, 4, 'Home'), 0);
  assert.equal(nextIndex(2, 4, 'End'), 3);
  assert.equal(nextIndex(0, 4, 'Enter'), null);
  assert.equal(nextIndex(-1, 4, 'Home'), null);
  assert.equal(escapeHtml(`<a&b>"'`), '&lt;a&amp;b&gt;&quot;&#39;');
  const view = pipelineMarkup({
    name: 'Sample <lane>',
    note: 'Specimen',
    acceptance: 'Blocked',
    stations: [
      { id: 'ingress', name: 'Ingress', word: 'HOLDING', detail: 'Waiting' },
      { id: 'evidence', name: 'Evidence', word: 'BLOCKED', detail: 'Review' },
    ],
  });
  assert.match(view.strip, /role="toolbar"/);
  assert.match(view.strip, /Ingress/);
  assert.match(view.strip, /Evidence/);
  assert.match(view.strip, /aria-pressed="true"/);
  assert.match(view.detail, /id="jk-station-detail"/);
  assert.equal(view.name, 'Sample <lane>');
  assert.match(pipelineMarkup({ name: 'Sample <lane>', stations: [] }).strip, /Empty/);
  assert.match(pipelineMarkup({ stations: [{ id: 'a', name: '<b>', word: 'W', detail: 'D' }] }).strip, /&lt;b&gt;/);
});

test('the kit files agree', () => {
  const problems = checkKit(kitRoot());
  assert.deepEqual(problems, []);
});

test('check and help exit 0', () => {
  const lines = [];
  return run(['check'], { stdout: (line) => lines.push(line), stderr: (line) => lines.push(line) }).then((code) => {
    assert.equal(code, 0);
    assert.deepEqual(lines, ['ok']);
    assert.equal(parseArgs(['demo', '--port', '8731']).opts.port, '8731');
  });
});

test('javascript files parse', () => {
  for (const file of [
    'jaryo/behavior.mjs', 'jaryo/boot.js', 'jaryo/cli.mjs', 'jaryo/contract.mjs',
    'jaryo/install.mjs', 'jaryo/mcp.mjs', 'jaryo/theme.mjs',
  ]) {
    const result = spawnSync(process.execPath, ['--check', join(repo, file)], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
  }
});

test('a fresh install serves the pipeline page', async () => {
  const dest = await mkdtemp(join(tmpdir(), 'jaryo-kit-'));
  let linkDir = '';
  try {
    const installed = spawnSync(process.execPath, [join(repo, 'jaryo', 'cli.mjs'), 'install', '--dest', dest], { encoding: 'utf8' });
    assert.equal(installed.status, 0, installed.stderr);
    assert.match(installed.stdout, /Installed Jaryo kit/);
    linkDir = await mkdtemp(join(tmpdir(), 'jaryo-link-'));
    symlinkSync(dest, join(linkDir, 'kit'));
    // Execute the installed CLI and request handler, replacing only node:http's
    // socket boundary. This covers serving contracts without opening a port;
    // it does not establish TCP or browser integration.
    const entry = join(linkDir, 'kit', 'cli.mjs');
    const localProcess = new EventEmitter();
    localProcess.argv = [process.execPath, entry, 'demo', '--port', '0'];
    const lines = [], errors = [], listeners = [];
    const server = new EventEmitter();
    let handler, closed = false;
    server.listen = (port, host, ready) => { listeners.push({ port, host }); ready(); };
    server.address = () => ({ port: 0 });
    server.close = done => { closed = true; done(); };
    const executable = file => readFileSync(file, 'utf8')
      .replace(/^#![^\n]*\n/, '').replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '')
      .replaceAll('import.meta.url', JSON.stringify(pathToFileURL(realpathSync(file)).href));
    const context = {
      ...fs, ...path, fileURLToPath, pathToFileURL, URL, process: localProcess,
      createServer: callback => { assert.equal(handler, undefined); handler = callback; return server; },
      console: { log: line => lines.push(line), error: line => errors.push(line) },
    };
    runInNewContext(`${executable(join(linkDir, 'kit', 'install.mjs'))}\n${executable(entry)}`, context);
    await new Promise(resolve => setImmediate(resolve));
    try {
      assert.deepEqual(errors, []);
      assert.deepEqual(listeners, [{ port: 0, host: '127.0.0.1' }]);
      assert.match(lines.join('\n'), /Jaryo demo at http:\/\/127\.0\.0\.1:0\//);
      assert.equal(localProcess.listenerCount('SIGTERM'), 1, 'symlink entry runs the installed demo');
      const request = url => {
        const response = {};
        handler({ url }, {
          writeHead(status, headers = {}) { Object.assign(response, { status, headers }); },
          end(body = '') { response.body = Buffer.from(body).toString('utf8'); response.ended = true; },
        });
        assert.equal(response.ended, true, `response ended for ${url}`);
        return response;
      };
      const redirect = request('/');
      assert.equal(redirect.status, 302);
      assert.equal(redirect.headers.Location, '/demo/index.html');
      const page = { url: redirect.headers.Location, ...request(redirect.headers.Location) };
      const html = page.body;
      assert.equal(page.status, 200);
      assert.match(page.url, /\/demo\/index\.html$/);
      assert.match(html, /data-jk-pipeline/);
      assert.match(html, /AGIW/);
      assert.match(html, /Sample lane/);
      const pipelineResponse = request('/demo/pipeline.json');
      assert.equal(pipelineResponse.status, 200);
      const pipeline = JSON.parse(pipelineResponse.body);
      assert.equal(pipeline.stations.length, 4);
      const css = request('/components.css');
      assert.equal(css.status, 200);
      assert.match(css.body, /\.jk-station/);
      const installed = await import(pathToFileURL(join(dest, 'behavior.mjs')).href);
      const view = installed.pipelineMarkup(pipeline);
      for (const name of ['Ingress', 'Check', 'Run', 'Evidence']) assert.match(view.strip, new RegExp(name));
      assert.equal(request('/demo/%2e%2e/%2e%2e/package.json').status, 404);
    } finally {
      localProcess.emit('SIGTERM');
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(closed, true);
      assert.equal(localProcess.exitCode, undefined);
    }
  } finally {
    if (linkDir) await rm(linkDir, { recursive: true, force: true });
    await rm(dest, { recursive: true, force: true });
  }
});

test('the stdio tool installs a kit', async () => {
  const dest = await mkdtemp(join(tmpdir(), 'jaryo-mcp-'));
  const child = spawn(process.execPath, [join(kitRoot(), 'mcp.mjs')], { stdio: ['pipe', 'pipe', 'pipe'] });
  let buffer = Buffer.alloc(0);
  const messages = [];
  child.stdout.on('data', (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    const read = readFrames(buffer);
    buffer = read.rest;
    messages.push(...read.messages);
  });
  const send = (message) => child.stdin.write(frameMessage(message));
  try {
    send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'test', version: '0' } } });
    send({ jsonrpc: '2.0', method: 'notifications/initialized' });
    send({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'jaryo_install', arguments: { dest } } });
    const reply = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`no install reply: ${messages.length}`)), 8000);
      const look = () => {
        const found = messages.find((message) => message.id === 2);
        if (found) {
          clearTimeout(timer);
          resolve(found);
        }
      };
      child.stdout.on('data', look);
      look();
    });
    assert.equal(reply.error, undefined);
    assert.match(reply.result.content[0].text, /Installed Jaryo kit/);
    assert.equal(existsSync(join(dest, 'demo', 'pipeline.json')), true);
    const listed = handleMessage({ jsonrpc: '2.0', id: 3, method: 'tools/list' });
    assert.deepEqual(listed.result.tools.map((tool) => tool.name), ['jaryo_tokens', 'jaryo_install', 'jaryo_check']);
  } finally {
    child.stdin.end();
    child.kill('SIGTERM');
    await new Promise((resolve) => child.once('exit', () => resolve()));
    await rm(dest, { recursive: true, force: true });
  }
});
