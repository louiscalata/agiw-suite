// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {mountToolConnections, toolConnectionRows, validLoopbackEndpoint} from './web/tool-connections.mjs';

const discovered = (id = 'opencode:pc-llm') => ({id, source: 'opencode', transport: 'local',
  configured: true, enabledInOpenCode: true, agentPermission: 'unknown',
  transportTest: {state: 'not-tested'}, detailCode: 'CONFIGURED_ONLY'});
const managed = (id = 'local-tools', state = {state: 'not-tested'}) => ({id, source: 'agiw',
  transport: 'streamable-http', url: 'http://127.0.0.1:3333/mcp', configured: true,
  enabledInOpenCode: false, agentPermission: 'unknown', transportTest: state,
  detailCode: 'AGIW_TEST_REGISTRY_ONLY'});
const envelope = (...rows) => ({schemaVersion: 1, opencodeConfig: {state: 'readable'}, connectors: rows});
const response = (body, status = 200) => ({ok: status >= 200 && status < 300,
  status, json: async () => body});

class Element {
  constructor(tag, ownerDocument) {
    this.tagName = tag; this.ownerDocument = ownerDocument; this.children = [];
    this.dataset = {}; this.attributes = new Map(); this.events = new Map();
    this._textContent = ''; this.textWrites = 0; this.value = ''; this.disabled = false;
  }
  set textContent(value) { this._textContent = value; this.textWrites++; }
  get textContent() { return this._textContent; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name); }
  focus() { this.ownerDocument.activeElement = this; }
  addEventListener(name, handler) { this.events.set(name, handler); }
  querySelectorAll(selector) {
    const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
    return selector === 'button' ? descendants.filter(child => child.tagName === 'button') : [];
  }
  set innerHTML(_) { throw new Error('HTML injection forbidden'); }
}
const textOf = node => [node.textContent, ...node.children.map(textOf)].join(' ');
function fixture(fetcher) {
  const doc = {createElement: tag => new Element(tag, doc)};
  const nodes = new Map();
  for (const key of ['data-tool-add', 'data-tool-id', 'data-tool-url', 'data-tool-submit',
    'data-tool-status', 'data-tool-action-status', 'data-opencode-status',
    'data-opencode-list', 'data-managed-status', 'data-managed-list']) {
    nodes.set(`[${key}]`, new Element(key, doc));
  }
  const root = new Element('section', doc);
  root.querySelector = selector => nodes.get(selector) || null;
  const timers = [];
  const panel = mountToolConnections(root, {fetch: fetcher,
    setTimeout: (callback, delay) => { timers.push({callback, delay}); return timers.length; },
    clearTimeout() {}});
  const get = key => nodes.get(`[data-${key}]`);
  return {root, panel, timers, get};
}

test('the browser guard accepts only literal loopback MCP endpoints', () => {
  assert(validLoopbackEndpoint('http://127.0.0.1:3333/mcp'));
  assert(validLoopbackEndpoint('http://127.0.0.1:1024/'));
  for (const url of ['https://127.0.0.1:3333/mcp', 'http://localhost:3333/mcp',
    'http://127.0.0.1:80/mcp', 'http://127.0.0.1:65536/mcp',
    'http://127.0.0.1:03333/mcp', 'http://127.0.0.1:3333//mcp',
    'http://127.0.0.1:3333/../mcp', 'http://127.0.0.1:3333/mcp?token=x',
    'http://127.0.0.1:3333/mcp#x', 'http://127.0.0.1:3333/mcp%2fother',
    'http://127.0.0.1:3333/mcp\n']) assert.equal(validLoopbackEndpoint(url), false, url);
});

test('malformed status cannot create controls or a false permission claim', () => {
  for (const value of [envelope(discovered(), discovered()),
    envelope({...managed(), agentPermission: 'allow'}),
    envelope({...managed(), url: 'http://otherhost:3333/mcp'}),
    envelope({...discovered(), transportTest: {state: 'ready'}}),
    {...envelope(), opencodeConfig: {state: 'anything'}}]) {
    assert.throws(() => toolConnectionRows(value), /STATUS_INVALID/);
  }
});

test('read-only discovery and saved endpoints render separately without a tool or model call', async () => {
  const calls = [];
  const {panel, get, timers} = fixture(async (path, options) => {
    calls.push({path, options}); return response(envelope(discovered('opencode:literal-unsafe'), managed()));
  });
  await panel.ready;
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/tool-connectors');
  assert.equal(calls[0].options.method, undefined);
  assert.match(textOf(get('opencode-list')), /Agent permission: unknown/);
  assert.match(textOf(get('opencode-list')), /live connection and model use unverified/);
  assert.equal(get('opencode-list').querySelectorAll('button').length, 0);
  assert.match(textOf(get('managed-list')), /Configured · not tested/);
  assert.match(textOf(get('managed-list')), /Save does not contact the server/);
  assert.equal(get('managed-list').querySelectorAll('button').length, 2);
  assert.equal(timers.at(-1).delay, 8000);
  panel.destroy();
});

test('an invalid OpenCode entry and unknown enabled setting never appear enabled', async () => {
  const invalid = {...discovered('opencode:broken'), configured: false,
    enabledInOpenCode: null, transport: 'unknown', detailCode: 'INVALID_ENTRY'};
  const uncertain = {...discovered('opencode:uncertain'), enabledInOpenCode: null};
  const {panel, get} = fixture(async () => response(envelope(invalid, uncertain)));
  await panel.ready;
  assert.match(get('opencode-status').textContent, /2 MCP entries found/);
  assert.doesNotMatch(get('opencode-status').textContent, /2 configured/);
  assert.match(textOf(get('opencode-list')), /Invalid configuration entry/);
  assert.match(textOf(get('opencode-list')), /Enabled setting in inspected global file unknown/);
  assert.equal(get('opencode-list').querySelectorAll('button').length, 0);
  panel.destroy();
});

test('Add uses an explicit save action; invalid inputs never post or trigger a protocol check', async () => {
  const posts = [];
  const {panel, get} = fixture(async (_path, options) => {
    if (options.method === 'POST') {
      posts.push(JSON.parse(options.body)); return response(envelope(managed()), 201);
    }
    return response(envelope());
  });
  await panel.ready;
  get('tool-id').value = 'Bad Name'; get('tool-url').value = 'http://127.0.0.1:3333/mcp';
  get('tool-add').events.get('submit')({preventDefault() {}});
  await Promise.resolve();
  assert.equal(posts.length, 0);
  assert.match(get('tool-action-status').textContent, /lowercase name/);
  get('tool-id').value = 'local-tools'; get('tool-url').value = 'http://evil.example/mcp';
  get('tool-add').events.get('submit')({preventDefault() {}});
  await Promise.resolve();
  assert.equal(posts.length, 0);
  assert.match(get('tool-action-status').textContent, /127\.0\.0\.1/);
  get('tool-url').value = 'http://127.0.0.1:3333/mcp';
  get('tool-add').events.get('submit')({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(posts, [{action: 'add', id: 'local-tools', url: 'http://127.0.0.1:3333/mcp'}]);
  assert.match(textOf(get('managed-list')), /Configured · not tested/);
  assert.equal(get('tool-id').value, '');
  assert.equal(get('tool-url').value, '');
  panel.destroy();
});

test('Test polls to a timestamped protocol result, never an OpenCode grant', async () => {
  let result = {state: 'not-tested'};
  const posts = [];
  const {panel, get, timers} = fixture(async (_path, options) => {
    if (options.method === 'POST') {
      posts.push(JSON.parse(options.body));
      result = {state: 'checking'};
      return response(envelope(discovered(), managed('local-tools', result)), 202);
    }
    return response(envelope(discovered(), managed('local-tools', result)));
  });
  await panel.ready;
  const buttons = get('managed-list').querySelectorAll('button');
  buttons[0].focus();
  await buttons[0].events.get('click')();
  assert.deepEqual(posts, [{action: 'test', id: 'local-tools'}]);
  assert.equal(timers.at(-1).delay, 1000);
  assert(get('managed-list').querySelectorAll('button')[0].disabled);
  assert.equal(get('tool-id').ownerDocument.activeElement, get('tool-id'),
    'a disabled Test action moves focus to the safe form, never Remove');
  assert.match(textOf(get('managed-list')), /No tool is invoked/);
  result = {state: 'ready', protocolVersion: '2025-11-25', toolCount: 2,
    checkedAtUnix: Date.UTC(2026, 9, 8, 8, 5) / 1000};
  await panel.refresh();
  assert.match(textOf(get('managed-list')), /Ready at 2026-10-08 08:05:00 UTC · protocol discovery only/);
  assert.match(textOf(get('managed-list')), /Agent permission and model use remain unverified/);
  assert.match(get('tool-action-status').textContent, /Protocol discovery completed/);
  assert.equal(get('managed-list').querySelectorAll('button')[0].disabled, false);
  assert.equal(timers.at(-1).delay, 8000);
  panel.destroy();
});

test('typed protocol and action errors remain visible without server-controlled HTML', async () => {
  const row = managed('local-tools', {state: 'error', code: 'MCP_UNREACHABLE', checkedAtUnix: 1791446700});
  const {panel, get} = fixture(async (_path, options) => options.method === 'POST'
    ? response({status: 'error', code: 'TEST_BUSY', message: '<img src=x onerror=alert(1)>'}, 409)
    : response(envelope(row)));
  await panel.ready;
  assert.match(textOf(get('managed-list')), /MCP_UNREACHABLE: The endpoint did not answer/);
  await panel.submit('test', 'local-tools');
  assert.match(get('tool-action-status').textContent, /Another endpoint test is in progress/);
  assert.doesNotMatch(get('tool-action-status').textContent, /img/);
  panel.destroy();
});

test('a GET begun before Disconnect cannot restore a removed endpoint; OpenCode rows cannot be disconnected', async () => {
  let resolveGet, reads = 0;
  const posts = [];
  const {panel, get} = fixture(async (_path, options) => {
    if (options.method === 'POST') {
      posts.push(JSON.parse(options.body)); return response(envelope(discovered()));
    }
    if (reads++ === 0) return response(envelope(discovered(), managed()));
    return new Promise(resolve => { resolveGet = resolve; });
  });
  await panel.ready;
  assert.equal(await panel.submit('disconnect', 'opencode:pc-llm'), false);
  const reading = panel.refresh();
  const disconnect = panel.submit('disconnect', 'local-tools');
  await disconnect;
  resolveGet(response(envelope(discovered(), managed())));
  await reading;
  assert.deepEqual(posts, [{action: 'disconnect', id: 'local-tools'}]);
  assert.equal(get('managed-list').children.length, 0);
  panel.destroy();
});

test('idle refresh keeps the focused action; Disconnect moves focus to the add form', async () => {
  let saved = true, changed = false;
  const {panel, get} = fixture(async (_path, options) => {
    if (options.method === 'POST') { saved = false; return response(envelope()); }
    return response(saved ? envelope(managed('local-tools', changed
      ? {state: 'ready', protocolVersion: '2025-11-25', toolCount: 1, checkedAtUnix: 1791446700}
      : {state: 'not-tested'})) : envelope());
  });
  await panel.ready;
  let testButton = get('managed-list').querySelectorAll('button')[0];
  const liveWrites = get('managed-status').textWrites;
  testButton.focus();
  await panel.refresh();
  assert.equal(get('managed-list').querySelectorAll('button')[0], testButton,
    'an unchanged poll keeps the row and virtual cursor in place');
  assert.equal(get('managed-status').textWrites, liveWrites,
    'an unchanged poll does not reannounce a live region');
  changed = true;
  await panel.refresh();
  assert.equal(get('managed-status').textWrites, liveWrites,
    'a changed row does not reannounce an unchanged collection status');
  testButton = get('managed-list').querySelectorAll('button')[0];
  assert.equal(testButton.ownerDocument.activeElement, testButton);
  const disconnect = get('managed-list').querySelectorAll('button')[1];
  disconnect.focus();
  await disconnect.events.get('click')();
  assert.equal(get('tool-id').ownerDocument.activeElement, get('tool-id'));
  assert.equal(get('managed-list').children.length, 0);
  assert.match(get('tool-action-status').textContent, /server and OpenCode settings are unchanged/);
  panel.destroy();
});

test('unsafe saved settings fail closed; the Components page exposes the feature and links from the map', async () => {
  const {panel, get} = fixture(async () => response({status: 'error', code: 'CONFIG_UNSAFE'}, 503));
  await panel.ready;
  assert.equal(get('tool-submit').disabled, true);
  assert.equal(get('managed-list').children.length, 0);
  assert.match(get('tool-status').textContent, /cannot be read safely/);
  panel.destroy();
  const components = readFileSync(new URL('./web/components.html', import.meta.url), 'utf8');
  const index = readFileSync(new URL('./web/index.html', import.meta.url), 'utf8');
  assert.match(components, /id="toolConnections"[^>]*data-tool-connections/);
  assert.match(components, /Nisi above is bundled core, not an MCP server/);
  assert.match(components, /Discovery reads one global OpenCode file/);
  assert.match(components, /do not put secrets in them/);
  assert.match(components, /Newer-only servers may not pass/);
  assert.match(components, /src="\/tool-connections\.mjs"/);
  assert.match(index, /href="\/components#toolConnections"/);
});
