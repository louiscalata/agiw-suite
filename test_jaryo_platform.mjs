// Shared Jaryo contracts and the places the Mac edition is allowed to differ from the Windows web.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { routeAdmissionText } from './web/online-code-mode.mjs';
import { ship } from './wrapper-patch/web-edition/appstore-rules.mjs';
import { loadDenylist } from './wrapper-patch/web-edition/build.mjs';
import { denylistHits } from './wrapper-patch/web-edition/engine.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');

test('route admission text is the shared observer contract', () => {
  assert.equal(routeAdmissionText({ admission: { source: 'legacy-router' } }), 'Single-run router (one route at a time)');
  assert.equal(routeAdmissionText({ admission: { policy: 'single', drainState: 'draining', sharedHolders: ['run-a', 'run-b'] } }), 'Single · draining 2 run(s)');
  assert.equal(routeAdmissionText({ admission: { source: 'invalid', policy: null } }), 'Policy file invalid; the router admits no new run');
  assert.equal(routeAdmissionText({}), 'Unknown');
  assert.equal(routeAdmissionText({ admission: { policy: 'multi', source: 'file', drainState: 'not-applicable' } }).startsWith('Multi'), true);
});

test('Mac edition ships eight files and leaves online code out', () => {
  assert.deepEqual(ship, ['index.html', 'style.css', 'app.js', 'usage-format.mjs', 'map-layout.mjs', 'model-control-view.mjs', 'simple-view.mjs', 'simple-view.css']);
  assert.equal(ship.includes('online-code-mode.mjs'), false);
});

test('Windows full web keeps fix inference and online code beside simple view', () => {
  const install = read('./windows/install.ps1');
  for (const name of ['app.js', 'online-code-mode.mjs', 'simple-view.mjs', 'simple-view.css', 'index.html']) {
    assert.match(install, new RegExp(`'${name}'`));
  }
  const index = read('./web/index.html');
  assert.match(index, /id="fixInference"/);
  assert.match(index, /Fix inference/);
  const app = read('./web/app.js');
  assert.match(app, /windows/);
  assert.ok(denylistHits(app, loadDenylist()).length > 0, 'the full observer is not the restricted edition');
});

test('removing the pipeline denylist pattern would stop catching that word', () => {
  const full = loadDenylist();
  const weakened = full.filter(item => item.label !== 'route pipeline');
  assert.equal(denylistHits('const leak = "pipeline";\n', full).length, 1);
  assert.equal(denylistHits('const leak = "pipeline";\n', weakened).length, 0);
});
