import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./web/setup.mjs', import.meta.url), 'utf8');
const { setupSteps, setupProgress, mountSetup } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const byId = steps => Object.fromEntries(steps.map(s => [s.id, s]));

const snapshot = {
  host: 'mac', activityKnown: true,
  sources: [{ id: 'lmstudio-api', state: 'live' }, { id: 'lms-ps', state: 'live' }],
  models: [{ id: 'a', loaded: true }, { id: 'b', loaded: false }],
  machines: [{ host: 'mac', platform: 'mac', state: 'up' }, { host: 'pc', platform: 'windows', state: 'down' }],
};
const components = { nisi: { version: '0.2.0', integrity: 'verified', entry: '/Users/someone/secret/nisi.mjs' }, node: { found: true, path: '/usr/local/bin/node' }, check: { state: 'not-run' } };
const jev = { configured: false, state: 'unavailable', message: 'Secure Jev credential storage is unavailable.' };

test('a working Mac shows what is done and what is next', () => {
  const s = byId(setupSteps({ snapshot, fresh: true, components, jev }));
  assert.equal(s.monitor.status, 'DONE');
  assert.equal(s.lmstudio.status, 'DONE');
  assert.equal(s.activity.status, 'DONE');
  assert.equal(s.model.status, 'DONE');
  assert.equal(s.model.evidence, '1 model loaded of 2 installed.');
  assert.equal(s.nisi.evidence, 'Nisi 0.2.0 package integrity verified.');
  assert.equal(s.node.status, 'DONE');
  assert.equal(s.selfcheck.status, 'NOT YET');
  assert.equal(s.selfcheck.link, '/components');
  assert.equal(s.jev.status, 'OPTIONAL');
  assert.equal(s.machines.evidence, '1 other machine known, none up.');
  assert.deepEqual(setupProgress(Object.values(s)), { done: 6, total: 7 });
});

test('no paths from the replies are shown', () => {
  const all = JSON.stringify(setupSteps({ snapshot, fresh: true, components, jev }));
  assert.doesNotMatch(all, /\/Users\/|\/usr\/local/);
});

test('without a fresh sample the runtime steps are unknown, not failed', () => {
  const s = byId(setupSteps({ snapshot, fresh: false, components, jev }));
  assert.equal(s.monitor.status, 'NOT YET');
  for (const id of ['lmstudio', 'activity', 'model']) assert.equal(s[id].status, 'UNKNOWN', id);
  const none = byId(setupSteps({ snapshot: null, fresh: false, components: null, jev: null }));
  for (const step of Object.values(none)) assert.equal(step.status, 'UNKNOWN', step.id);
});

test('each missing piece says what the person does next', () => {
  const bare = { host: 'mac', activityKnown: false, sources: [{ id: 'lmstudio-api', state: 'error' }], models: [] };
  const s = byId(setupSteps({ snapshot: bare, fresh: true, components: { nisi: { integrity: 'mismatch' }, node: { found: false }, check: { state: 'failed', message: 'Node is too old.' } }, jev }));
  assert.match(s.lmstudio.next, /start the API server on 127\.0\.0\.1:1234/);
  assert.equal(s.activity.status, 'NOT YET');
  assert.equal(s.model.status, 'UNKNOWN');
  assert.equal(s.nisi.status, 'NOT YET');
  assert.match(s.node.next, /Node\.js 22/);
  assert.equal(s.selfcheck.status, 'FAILED');
  assert.equal(s.selfcheck.evidence, 'Node is too old.');
  const live = byId(setupSteps({ snapshot: { ...snapshot, models: [] }, fresh: true, components, jev }));
  assert.match(live.model.next, /Download a model/);
});

test('optional steps never count toward the required total', () => {
  const s = setupSteps({ snapshot: { ...snapshot, machines: [{ host: 'mac', state: 'up' }, { host: 'pc', platform: 'windows', state: 'up' }] }, fresh: true, components: { ...components, check: { state: 'passed' } }, jev: { configured: true } });
  assert.deepEqual(setupProgress(s), { done: 7, total: 7 });
  assert.equal(byId(s).machines.status, 'DONE');
  assert.equal(byId(s).jev.status, 'DONE');
});

test('the page only reads: every request is a GET to a known path', async () => {
  const calls = [];
  const fetch = async (path, options = {}) => { calls.push([path, options.method || 'GET']); return { ok: true, json: async () => ({}) }; };
  const document = { getElementById: () => null };
  let next = null;
  const handle = mountSetup({ document, fetch, setTimeout: fn => { next = fn; return 1; } });
  await new Promise(r => setImmediate(r));
  for (let i = 0; i < 11 && next; i++) { const fn = next; next = null; await fn(); }
  handle.destroy();
  assert.ok(calls.length >= 3);
  for (const [path, method] of calls) {
    assert.equal(method, 'GET');
    assert.ok(['/api/snapshot', '/api/components', '/api/components/jev'].includes(path), path);
  }
});

test('every full-web page links Setup and both servers serve it', () => {
  for (const page of ['index', 'monitor', 'components', 'identity', 'machines', 'setup']) assert.match(read(`./web/${page}.html`), /<a href="\/setup"( aria-current="page")?>Setup<\/a>/);
  assert.match(read('./server.py'), /'\/setup': \('setup\.html'/);
  assert.match(read('./windows/agiw_win/server.py'), /"\/setup\.mjs": \("setup\.mjs"/);
  assert.doesNotMatch(source, /method:\s*'POST'|\/check'/);
});
