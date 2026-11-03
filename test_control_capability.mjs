import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {modelControlView} from './web/model-control-view.mjs';

test('native capability is attached only to same-origin POSTs', async () => {
  const swift = readFileSync(new URL('./Monitor.swift', import.meta.url), 'utf8');
  const match = swift.match(/let source = """\n([\s\S]*?)\n        """/);
  assert.ok(match, 'native startup must install the capability script');
  // Public fixture text, never a generated capability.
  const settings = {origin: 'http://127.0.0.1:8765', capability: 'fixture-only'};
  const script = match[1].replace('\\(settings)', JSON.stringify(settings));
  const sent = [];
  const context = {URL, Request, location: {origin: settings.origin, href: `${settings.origin}/monitor`},
    window: {fetch: async request => { sent.push(request); return {}; }}};
  vm.runInNewContext(script, context);
  await context.window.fetch('/api/components/jev/check', {method: 'POST', body: '{}'});
  await context.window.fetch('/api/snapshot');
  await context.window.fetch('http://127.0.0.1:9999/api/control', {method: 'POST'});
  await context.window.fetch(new Request(`${settings.origin}/api/models/control`, {method: 'POST', body: '{}'}));
  assert.equal(sent[0].headers.has('X-AGIW-Control'), true);
  assert.equal(sent[1].headers.has('X-AGIW-Control'), false);
  assert.equal(sent[2].headers.has('X-AGIW-Control'), false);
  assert.equal(sent[3].headers.has('X-AGIW-Control'), true);
  assert.equal(await sent[3].text(), '{}');
});

test('unconfirmed model operation disables controls and explains reconciliation', () => {
  const state = modelControlView({host: 'mac', source: 'lmstudio-api', loaded: false,
    state: 'unloaded', modelKey: 'qwen', id: 'qwen'}, {host: 'mac', feedFresh: true,
    supported: true, status: {status: 'failed', settlement: 'unconfirmed'}});
  assert.equal(state.action, null);
  assert.match(state.reason, /fresh inventory/i);
});

test('the actual control panel displays the settlement explanation', () => {
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  const render = app.split('\n').find(line => line.startsWith('function renderModelAction()'));
  const elements = Object.fromEntries(['modelActionDock', 'modelActionTitle', 'modelActionStatus', 'modelAction']
    .map(id => [id, {dataset: {}, setAttribute() {}}]));
  vm.runInNewContext(render + '\nrenderModelAction();', {
    $: id => elements[id], graph: {nodes: [{id: 'qwen', kind: 'model', model: {id: 'qwen', host: 'mac'}}]},
    selected: 'qwen', view: 'runtime', snapshot: {host: 'mac'}, name: value => value,
    modelActionState: () => ({action: null, reason: 'Waiting for fresh inventory to confirm settlement.'}),
    modelControlError: '', modelControlPosting: false,
    modelControlStatus: {modelId: 'qwen', status: 'failed', settlement: 'unconfirmed', message: 'Command failed'},
  });
  assert.match(elements.modelActionStatus.textContent, /fresh inventory/);
});
