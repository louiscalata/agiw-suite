import * as fleet from './web/fleet-state.mjs';
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
  const lookup = app.split('\n').find(line => line.startsWith('function inspectorNodeFor('));
  const elements = Object.fromEntries(['modelActionDock', 'modelActionTitle', 'modelActionStatus', 'modelAction']
    .map(id => [id, {dataset: {}, setAttribute() {}}]));
  vm.runInNewContext(lookup + '\n' + render + '\nrenderModelAction();', {
    $: id => elements[id], canonicalNodeId: id => id, graph: {nodes: [{id: 'qwen', kind: 'model', model: {id: 'qwen', host: 'mac'}}]},
    selected: 'qwen', view: 'runtime', snapshot: {host: 'mac'}, name: value => value,
    modelActionState: () => ({action: null, reason: 'Waiting for fresh inventory to confirm settlement.'}),
    modelControlError: '', modelControlPosting: false,
    modelControlStatus: {modelId: 'qwen', status: 'failed', settlement: 'unconfirmed', message: 'Command failed'},
  });
  assert.match(elements.modelActionStatus.textContent, /fresh inventory/);
});

test('closing an inspector-only target restores its sidebar button and opens compact Browse', () => {
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  const close = app.split('\n').find(line => line.startsWith('function closeDrawer('));
  const key = 'model:mac:vendor/path["quoted"]', focused = [], opened = [];
  const button = {dataset: {key}, focus: () => focused.push('sidebar item')};
  const toggle = {focus: () => focused.push('Browse')}, drawer = {};
  const context = {selected: key, inspectorNode: key, userCamera: true, nodeElements: new Map(),
    $: id => id === 'drawer' ? drawer : id === 'list' ? {querySelectorAll: () => [button]} : toggle,
    renderGraph() {}, renderList() {}, fitMap() {}, dockedSidebar: () => false,
    setSidebarOpen: value => opened.push(value)};
  vm.runInNewContext(close + '\ncloseDrawer();', context);
  assert.deepEqual(focused, ['sidebar item']);
  assert.deepEqual(opened, [true]);
  assert.equal(drawer.hidden, true);
  assert.equal(context.selected, null);
  focused.length = 0;
  context.selected = 'filtered-away';
  vm.runInNewContext(close + '\ncloseDrawer();', context);
  assert.deepEqual(focused, ['Browse']);
});

function reviewedRepairHarness() {
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  const definitions = app.replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];
  const sent = [], elements = new Map();
  const document = {activeElement: null, getElementById(id) {
    if (!elements.has(id)) elements.set(id, {dataset: {}, hidden: true, open: false,
      setAttribute() {}, focus() {document.activeElement = this;},
      showModal() {this.open = true;}, close() {this.open = false;}});
    return elements.get(id);
  }};
  const api = vm.runInNewContext(definitions + `
updateOnlineCodeMode=()=>{};recordOnlineCodeAction=()=>{};pollOnlineCodeAction=()=>{};
snapshot={host:'mac'};
({requestOnlineCodeAction,reviewOnlineCodeAction,openActionReview,closeActionReview,confirmActionReview,
  scopes:[...REPAIR_SCOPES]})`, {
    document, AbortController, setTimeout: () => 1, clearTimeout() {},
    fetch: async (path, options) => {sent.push({path, ...options}); return {ok: true, json: async () => ({status: 'ready'})};},
  });
  return {api, sent, elements, document};
}

test('repair capability exposes exactly the separately reviewed local, route and Nisi scopes', () => {
  const {api} = reviewedRepairHarness();
  assert.deepEqual(Array.from(api.scopes).sort(), ['local', 'nisi', 'route']);
});

test('repair dispatch requires review and pins the scope in its POST', async () => {
  for (const scope of ['local', 'route', 'nisi']) {
    const {api, sent} = reviewedRepairHarness();
    await api.requestOnlineCodeAction(`fix-${scope}`);
    assert.deepEqual(sent, [], `unreviewed ${scope} must not dispatch`);
    await api.requestOnlineCodeAction(`fix-${scope}`, {reviewed: true});
    assert.equal(sent.length, 1);
    assert.equal(sent[0].path, '/api/eci-tuning/repair');
    assert.equal(sent[0].method, 'POST');
    assert.deepEqual(JSON.parse(sent[0].body), {scope, reviewed: true});
  }
});

test('bundled and readiness aliases cannot dispatch a repair even when marked reviewed', async () => {
  for (const action of ['fix-all', 'fix-both', 'check-and-repair', 'readiness', 'fix-share', 'unknown']) {
    const {api, sent} = reviewedRepairHarness();
    await api.requestOnlineCodeAction(action, {reviewed: true});
    assert.deepEqual(sent, [], action);
  }
});

test('a consequence review shows target, effects and honest duration before any dispatch', () => {
  const {api, sent, elements, document} = reviewedRepairHarness();
  const opener = {focus() {document.activeElement = this;}};
  document.activeElement = opener;
  api.openActionReview({title: 'Review repair', target: 'This Mac', effects: 'May start the local API.',
    duration: 'Completion time is unknown.'}, () => {throw new Error('opening must not confirm');});
  assert.deepEqual(sent, []);
  assert.equal(elements.get('actionReview').open, true);
  assert.match(elements.get('actionReviewHeading').textContent, /review/i);
  assert.equal(elements.get('actionReviewTarget').textContent, 'This Mac');
  assert.equal(elements.get('actionReviewEffects').textContent, 'May start the local API.');
  assert.equal(elements.get('actionReviewDuration').textContent, 'Completion time is unknown.');
  assert.equal(document.activeElement, elements.get('actionReviewCancel'), 'Cancel is focused');
  api.closeActionReview();
  assert.equal(document.activeElement, opener, 'focus returns to the opener');
  assert.deepEqual(sent, []);
});


test('an open review retains its scope and only the first confirmation dispatches', async () => {
  const {api, sent, document} = reviewedRepairHarness();
  document.activeElement = {focus() {}};
  api.reviewOnlineCodeAction('fix-route');
  api.reviewOnlineCodeAction('fix-local');
  assert.deepEqual(sent, [], 'reviewing either control sends nothing');
  api.confirmActionReview();
  api.confirmActionReview();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent.length, 1, 'duplicate confirmation must not dispatch again');
  assert.deepEqual(JSON.parse(sent[0].body), {scope: 'route', reviewed: true});
});
