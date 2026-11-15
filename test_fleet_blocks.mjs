// Fleet recorded-check summary (brief screen 1). Pure view checks plus the
// Overview renderer in the shared DOM harness; no browser, socket or model run.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {recordedCheckSummary} from './web/fleet-state.mjs';
import {harness} from './test_support/monitor-harness.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const rows = view => Object.fromEntries(view.rows);
const NA = /^NOT AVAILABLE · /;

const screened = (record, model, finalist, created) => ({record_id: record, model, finalist, full_run: 'NOT_RUN',
  created_at: created, verdict: finalist ? 'INCONCLUSIVE' : 'WORSE', normalized_score: finalist ? .8 : .5});
const view = (hardware, plan, rowsIn, pending = []) => ({hardware, plan_id: plan, rows: rowsIn, pending,
  as_of: [...rowsIn, ...pending].map(row => row.created_at).sort().at(-1)});
const response = (fastViews, dataset = {status: 'configured', reason: null, as_of: '2026-11-10', ingested_at: '2026-11-11T08:00:00Z'}) => ({
  name: 'AGIW x ECI Model Tuning Tool', schema_version: 1, dataset,
  rankings: {baseline_protocol: {eci: []}, fast_check: {label: 'fast check', hardware_views: fastViews}},
  recommendation: {routes: {general: {model: null, status: 'NOT_COMPUTED'}}}, pending: [],
  policy: {origin: 'default', routes: {}}, runtime_activation: {status: 'NOT_APPLIED'}});

const supplied = response([
  view({id: 'pc-3080', machine_label: 'PC RTX 3080'}, 'plan-a', [
    screened('r1', 'model-a', true, '2026-11-12T10:00:00Z'),
    screened('r2', 'model-b', true, '2026-11-12T10:05:00Z'),
    screened('r3', 'model-c', false, '2026-11-12T09:00:00Z')]),
  view({id: 'mac-m4', machine_label: 'Mac M4 Max'}, 'plan-a', [screened('r4', 'model-d', true, '2026-11-13T07:30:00Z')],
    [{record_id: 'p1', model: 'model-e', created_at: '2026-11-13T08:15:00Z', pre: 'CHECK', post: 'REJECT'}]),
]);

test('Fleet shows the dataset source, the last recorded check and finalists per hardware when supplied', () => {
  const summary = recordedCheckSummary(supplied);
  assert.equal(summary.available, true);
  const r = rows(summary);
  assert.equal(r['Dataset source'], 'Configured dataset folder · as of 2026-11-10 · ingested 2026-11-11T08:00:00Z');
  assert.equal(r['Last recorded check'], '2026-11-13T08:15:00Z', 'the latest check across hardware, pending included');
  assert.equal(r['Screening finalists · PC RTX 3080 · plan plan-a'], 'model-a, model-b · 2 screening finalists of 3 screened · full run NOT_RUN');
  assert.equal(r['Screening finalists · Mac M4 Max · plan plan-a'], 'model-d · 1 screening finalist of 1 screened · full run NOT_RUN · 1 check not eligible');
  assert.equal(r['Screening finalists'], undefined, 'per-hardware rows replace the empty row');
  assert.match(rows(recordedCheckSummary(response([view({id: 'x'}, 'p', [screened('a', 'm1', undefined, '2026-11-12T10:00:00Z'), {...screened('b', 'm2', true, '2026-11-12T10:00:00Z'), finalist: 'true'}, {...screened('c', 'm3', true, '2026-11-12T10:00:00Z'), finalist: 1}])])))['Screening finalists · x · plan p'], /^No finalist · 0 screening finalists of 3 screened/, 'only finalist === true counts');
  assert.equal(rows(recordedCheckSummary({...supplied, dataset: {status: 'bundled', as_of: '2026-11-10'}}))['Dataset source'],
    'Bundled catalog · as of 2026-11-10');
});

test('missing pieces read NOT AVAILABLE with a reason, never zero or none', () => {
  const empty = rows(recordedCheckSummary(response([])));
  assert.match(empty['Last recorded check'], NA);
  assert.match(empty['Screening finalists'], NA);
  assert.match(rows(recordedCheckSummary(response([], {status: 'unknown', reason: 'Configured dataset.json was not found.'})))['Dataset source'],
    /^NOT AVAILABLE · Configured dataset\.json was not found\.$/);
  for (const bad of [null, undefined, {status: 'unknown'}, 'text', [], {dataset: {status: 'weird'}}]) {
    const summary = recordedCheckSummary(bad);
    for (const [label, text] of summary.rows) assert.match(text, NA, `${JSON.stringify(bad)} ${label}`);
  }
  assert.equal(recordedCheckSummary(null).available, false);
  // A group with only ineligible checks has no finalist reading, not "none".
  const pendingOnly = rows(recordedCheckSummary(response([view({id: 'x'}, 'p', [], [{record_id: 'q', created_at: '2026-11-01T00:00:00Z'}])])));
  assert.match(pendingOnly['Screening finalists · x · plan p'], /^NOT AVAILABLE · 1 check not eligible$/);
  // An unparseable timestamp is not a check time.
  assert.match(rows(recordedCheckSummary(response([view({id: 'x'}, 'p', [screened('a', 'm', true, 'yesterday')])])))['Last recorded check'], NA);
});

test('the summary never names a winner or a best model', () => {
  for (const input of [supplied, response([]), null]) {
    const text = recordedCheckSummary(input).rows.flat().join(' ') + ' ' + recordedCheckSummary(input).note;
    assert.doesNotMatch(text, /\bbest\b|\bwinner\b|\btop model\b|recommended/i);
  }
  assert.match(recordedCheckSummary(supplied).note, /not a ranking into one model/);
});

test('Overview reads /api/eci-tuning with GET only and paints the rows', async () => {
  const calls = [];
  const fetch = async (url, options = {}) => { calls.push({url, method: options.method || 'GET'}); return {ok: true, status: 200, json: async () => supplied}; };
  const {api, $} = harness({extra: {fetch}});
  await api.loadRecordedChecks();
  assert.deepEqual(calls, [{url: '/api/eci-tuning', method: 'GET'}]);
  const box = $('recordedCheckRows');
  const pairs = box.children.map(child => [child.tagName, child.textContent]);
  assert.deepEqual(pairs.slice(0, 4), [['DT', 'Dataset source'], ['DD', 'Configured dataset folder · as of 2026-11-10 · ingested 2026-11-11T08:00:00Z'],
    ['DT', 'Last recorded check'], ['DD', '2026-11-13T08:15:00Z']]);
  assert.match($('recordedCheckNote').textContent, /not a ranking into one model/);
});

test('an unreadable or failed read paints NOT AVAILABLE, not the previous reading', async () => {
  let reply = {ok: true, status: 200, json: async () => supplied};
  const {api, $} = harness({extra: {fetch: async () => reply}});
  await api.loadRecordedChecks();
  assert.match($('recordedCheckRows').textContent, /model-a/);
  for (const next of [{ok: false, status: 503, json: async () => supplied}, {ok: true, status: 200, json: async () => { throw new SyntaxError('x'); }}]) {
    reply = next;
    await api.loadRecordedChecks();
    assert.doesNotMatch($('recordedCheckRows').textContent, /model-a/);
    assert.match($('recordedCheckRows').textContent, /NOT AVAILABLE · recorded checks could not be read/);
  }
  reply = Promise.reject(new TypeError('network'));
  await api.loadRecordedChecks();
  assert.match($('recordedCheckRows').textContent, /NOT AVAILABLE · recorded checks could not be read/);
});

test('the Overview page has the recorded-check block after attention and before the controls', () => {
  const html = read('./web/index.html');
  const at = id => html.indexOf(`id="${id}"`);
  assert.ok(at('needsAttention') < at('recordedChecks') && at('recordedChecks') < at('runtimeSidebar'));
  assert.match(html, /<section id="recordedChecks" aria-labelledby="recordedChecksHeading"><h2 id="recordedChecksHeading">Recorded checks<\/h2>/);
  assert.match(html, /<dl id="recordedCheckRows">/);
  assert.match(html, /<a href="\/eci-tuning">/);
  const app = read('./web/app.js');
  assert.match(app, /loadRecordedChecks\(\);setInterval\(loadRecordedChecks,300000\);/, 'read once at start, then every five minutes');
});

test('a slower earlier read never replaces a newer one', async () => {
  const replies = [];
  const fetch = () => new Promise(resolve => replies.push(resolve));
  const {api, $} = harness({extra: {fetch}});
  const first = api.loadRecordedChecks(), second = api.loadRecordedChecks();
  replies[1]({ok: true, status: 200, json: async () => supplied});
  await second;
  replies[0]({ok: false, status: 503, json: async () => ({})});
  await first;
  assert.match($('recordedCheckRows').textContent, /model-a/);
});
