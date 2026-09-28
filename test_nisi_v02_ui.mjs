import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {nisiV02View} from './web/online-code-mode.mjs';

const observed = {
  schemaVersion: 1,
  integration: 'private-local-orchestration-route',
  version: '0.2.0-private.0',
  runtimeIntegrity: 'VERIFIED',
  hostBinding: 'DRIFT',
  driftedHostFiles: ['pipeline_integrations.py'],
  activationStatus: 'VERIFIED',
  activationVerifiedAt: '2026-09-23T17:08:39Z',
  activationProbe: {
    kind: 'historical-local-model-probe', workStatus: 'RESPONSE_VALIDATED',
    contract: 'PASS', tests: 'NOT_RUN', certification: 'NOT_RUN', accepted: false,
  },
  liveInference: 'UNKNOWN', workflowAcceptance: 'UNKNOWN',
  releaseAcceptance: 'NOT_ESTABLISHED',
};

test('Nisi Inference (the private v0.2 runtime): pin, drift, and historical probe stay separate from live inference', () => {
  const view = nisiV02View(observed, {feedFresh: true});
  assert.equal(view.runtimeIntegrity, 'VERIFIED');
  assert.equal(view.hostBinding, 'DRIFT');
  assert.deepEqual(view.driftedHostFiles, ['pipeline_integrations.py']);
  assert.match(view.activationProbe, /RESPONSE_VALIDATED.*tests NOT_RUN.*certification NOT_RUN.*accepted no/);
  assert.match(view.liveInference, /Unknown/);
  assert.match(view.workflowAcceptance, /Unknown/);
  assert.match(view.releaseAcceptance, /Not established/);
});

test('stale or malformed source cannot keep the Nisi Inference installation looking verified', () => {
  for (const [value, feedFresh] of [[observed, false], [{...observed, schemaVersion: 2}, true], [null, true]]) {
    const view = nisiV02View(value, {feedFresh});
    assert.equal(view.observed, false);
    assert.equal(view.runtimeIntegrity, 'UNKNOWN');
    assert.equal(view.hostBinding, 'UNKNOWN');
    assert.equal(view.activationProbe, 'Unknown');
    assert.match(view.liveInference, /Unknown/);
  }
});

test('untrusted acceptance fields cannot manufacture a green release or live model claim', () => {
  const view = nisiV02View({...observed, liveInference: 'GENERATING',
    workflowAcceptance: 'ACCEPTED', releaseAcceptance: 'READY'}, {feedFresh: true});
  assert.match(view.liveInference, /Unknown/);
  assert.match(view.workflowAcceptance, /Unknown/);
  assert.match(view.releaseAcceptance, /Not established/);
});

test('Nisi Inference: no visible "Nisi v0.2" label is left in the web files, and the internal ids stay', () => {
  const read = file => readFileSync(new URL(`./web/${file}`, import.meta.url), 'utf8');
  for (const file of ['index.html', 'app.js', 'map-layout.mjs', 'online-code-mode.mjs', 'model-control-view.mjs', 'style.css'])
    assert.doesNotMatch(read(file), /Nisi v0\.2/i, file);
  const html = read('index.html'), app = read('app.js');
  assert.match(html, /<summary>Nisi Inference runtime evidence<\/summary><div id="onlineCodeV02Rows"><\/div>/);
  assert.match(app, /source\.id==='nisi-v02-runtime'/);
  assert.match(app, /nisiV02View\(snapshot\?\.nisiV02,/);
});
