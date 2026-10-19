import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';
import {performance} from 'node:perf_hooks';
import {createInterface} from 'node:readline';
import {readFileSync, writeFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const layout = await import(process.env.AGIW_INVARIANT_LAYOUT || './web/map-layout.mjs');
const {agiwPeersRows, linuxPeerView} = layout;

// Existing dependency assertions name lanes; production detail identity names models.
// Translate only the key vocabulary, preserving every original dependency and control.
function dependencyFields(entry, view) {
  const fields = {...view.fields};
  for (const model of entry.snapshot.models || []) {
    if (!model.lane) continue;
    for (const name of ['loaded', 'activity', 'slotsBusy', 'slotsTotal', 'liveTokensPerSecond']) {
      const value = fields[`model:${encodeURIComponent(model.id)}:${name}`];
      if (value) fields[`model:${model.lane}:${name}`] = value;
    }
  }
  return fields;
}

function checkShape(entry, view, violations, label) {
  const fail = (group, field, output, expected) => violations.push({case: label, group,
    property: entry.property || 'no-false-certainty', dimension: entry.dimension, input: entry.coordinates, field, output, expected});
  for (const check of entry.expect) {
    if (check.invalidIdentity) {
      const details = Object.entries(view.fields).filter(([key]) => key.startsWith('model:'));
      if (!details.length) fail(check.group, 'invalid identity details', details, 'explicit unknown identity');
      for (const [key, value] of details.filter(([key]) => key.startsWith('model:invalid-')))
        if (value.known) fail(check.group, key, value.text, 'unknown');
    }
    if (check.allModelsUnknown) {
      for (const model of entry.snapshot.models) {
        for (const key of ['loadedState', 'activity', 'slotsBusy', 'slotsTotal', 'liveTokensPerSecond']) {
          const expected = ['loadedState', 'activity'].includes(key) ? 'unknown' : null;
          if (model[key] !== expected) fail(check.group, `published.${model.id}.${key}`, model[key], expected);
        }
      }
      for (const [key, value] of Object.entries(view.fields))
        if (key.startsWith('model:') && value.known) fail(check.group, key, value.text, 'unknown');
    }
    if (check.exactFields) {
      for (const [key, expected] of Object.entries(check.exactFields)) {
        const actual = view.fields[key];
        if (!actual?.known || actual.text !== expected) fail(check.group, key, actual, expected);
      }
      for (const [key, actual] of Object.entries(view.fields)) {
        if (key in check.exactFields) continue;
        if (!check.unknownFields.includes(key)) fail(check.group, key, actual, 'field missing from full oracle');
        else if (actual.known || !/^Unknown \(.+\)$/.test(actual.text)) fail(check.group, key, actual, 'explicitly unobserved source');
      }
      const expectedKeys = [...Object.keys(check.exactFields), ...check.unknownFields].sort();
      if (JSON.stringify(Object.keys(view.fields).sort()) !== JSON.stringify(expectedKeys))
        fail(check.group, 'full visible field set', Object.keys(view.fields).sort(), expectedKeys);
      const observations = view.rows.filter(([label]) => label.endsWith(' observation')).map(([label, text]) => [label.split(' · ')[1], text]);
      if (JSON.stringify(observations) !== JSON.stringify(check.observationRows)) fail(check.group, 'lane observations', observations, check.observationRows);
      const callers = view.rows.filter(([label]) => label.endsWith(' · caller')).map(([, text]) => text);
      if (JSON.stringify(callers) !== JSON.stringify(check.callerRows)) fail(check.group, 'caller rows', callers, check.callerRows);
    }
    if (check.path) {
      const value = check.path.reduce((v, key) => v?.[key], entry.snapshot);
      if (!Object.is(value, check.value)) fail(check.group, check.path.join('.'), value, check.value);
    }
    if ('aggregate' in check) {
      if (view.fields.modelsTotal.known !== check.aggregate)
        fail(check.group, 'modelsTotal', view.fields.modelsTotal.text, check.aggregate ? check.count : 'unknown');
      if (check.aggregate && view.fields.modelsTotal.text !== String(check.count))
        fail(check.group, 'modelsTotal', view.fields.modelsTotal.text, check.count);
      if (!check.aggregate && view.fields.loaded.known) fail(check.group, 'loaded', view.fields.loaded.text, 'unknown');
    }
    if (check.modelDetails) {
      const details = view.rows.filter(([key]) => key.endsWith(' · Loaded state'));
      if (details.length !== check.modelDetails.length)
        fail(check.group, 'model detail count', details, check.modelDetails.length);
      for (const model of check.modelDetails) {
        const matches = details.filter(([key]) => key.includes(` · ${model.id} (`) && key.includes(`(${model.lane})`));
        if (!matches.length || matches.some(([, text]) => model.state === 'unknown' ? !text.startsWith('Unknown (') : text !== model.state))
          fail(check.group, 'model identity/state', matches, model);
      }
    }
    if (check.ambiguousId) {
      for (const model of entry.snapshot.models.filter(row => row.id === check.ambiguousId)) {
        if (model.loadedState !== 'unknown' || model.activity !== 'unknown' || model.slotsBusy !== null)
          fail(check.group, 'ambiguous published model', model, 'unknown identity/activity and null counters');
      }
    }
    if (check.modelActivity) {
      for (const model of entry.snapshot.models) {
        if (model.activity !== check.modelActivity)
          fail(check.group, 'shared lane model activity', model, check.modelActivity);
        if (check.modelActivity === 'unknown' && ['slotsBusy', 'slotsTotal', 'liveTokensPerSecond'].some(key => model[key] !== null))
          fail(check.group, 'shared lane model counters', model, 'null');
      }
    }
    if ('callersKnown' in check) {
      if (view.fields.callers.known !== check.callersKnown)
        fail(check.group, 'callers', view.fields.callers.text, check.callersKnown);
      if (!check.callersKnown && /observed\)/.test(view.fields.callers.text))
        fail(check.group, 'caller reason', view.fields.callers.text, 'unavailable/incomplete/expired reason');
      if (entry.snapshot.network.callersState === 'unknown' &&
          (!entry.snapshot.network.callersReason || entry.snapshot.network.callersReason === 'observed'))
        fail(check.group, 'network.callersReason', entry.snapshot.network.callersReason, 'caller-specific unknown reason');
    }
    if ('fresh' in check) {
      if (view.fresh !== check.fresh) fail(check.group, 'fresh', view.fresh, check.fresh);
      if (!check.fresh) for (const [key, value] of Object.entries(view.fields)) {
        if (value.known) fail(check.group, key, value.text, 'unknown');
      }
    }
  }
}

// Execute the actual app's literal-text DOM renderer without its polling loop.
const definitions = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8')
  .replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.ownText = ''; }
  append(...children) { this.children.push(...children); }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw new Error('peer evidence must render as literal text'); }
}
const dom = runInNewContext(`${definitions}\n;({render: detailPairs})`,
  {...layout, document: {createElement: tag => new Element(tag)}});

const omitted = ['cpu', 'disks', 'temperatures', 'gpuLock', 'pairCluster', 'authority', 'lastCodexRun', 'idleAnalysis'];
const dependencies = {
  ...Object.fromEntries(omitted.map(source => [source, []])),
  'pair-health': ['component:pair', 'lane:pair-llamacpp:health', 'model:pair-llamacpp:'],
  'pair-models': ['component:pair', 'lane:pair-llamacpp:health', 'model:pair-llamacpp:'],
  'wrapper-health': ['lane:gpu-wrapper-101:', 'model:gpu-wrapper-101:'],
  'wrapper-models': ['lane:gpu-wrapper-101:', 'model:gpu-wrapper-101:'],
  'wrapper-slots': ['lane:gpu-wrapper-101:activity', 'model:gpu-wrapper-101:activity',
    'model:gpu-wrapper-101:slots', 'model:gpu-wrapper-101:live'],
  processes: ['component:coordinator', 'lane:gpu-wrapper', 'lane:pair-llamacpp:', 'model:gpu-wrapper', 'model:pair-llamacpp:'],
  network: ['callers', 'listeners', 'lane:gpu-wrapper', 'lane:pair-llamacpp:', 'model:gpu-wrapper', 'model:pair-llamacpp:'],
  'model-counters': ['model:gpu-wrapper-101:activity', 'model:gpu-wrapper-101:slots', 'model:gpu-wrapper-101:live'],
  'gpu-fields': ['gpu:'], gpus: ['gpus', 'health'], memory: ['memory'], callers: ['callers'], listeners: ['listeners'],
  models: ['modelsTotal', 'loaded'], components: ['component:'], history: ['history:'], boot: ['health'], routes: ['routes'], 'coordinator-units': ['component:coordinator', 'lane:coordinator'], lanes: ['lane:', 'model:', 'modelsTotal', 'loaded'],
};
const rawDependencies = {
  tcp4: ['callers', 'listeners', 'model:', 'lane:gpu-wrapper', 'lane:pair-llamacpp:'],
  tcp6: ['callers', 'listeners', 'model:', 'lane:gpu-wrapper', 'lane:pair-llamacpp:'],
  'process-status': ['callers', 'model:gpu-wrapper', 'lane:gpu-wrapper'],
  'process-stat': ['callers', 'model:gpu-wrapper', 'lane:gpu-wrapper'],
  'process-command': ['callers', 'model:gpu-wrapper', 'lane:gpu-wrapper'],
  'process-exe': ['callers', 'model:gpu-wrapper', 'lane:gpu-wrapper'],
  'socket-visibility': ['callers', 'model:gpu-wrapper', 'lane:gpu-wrapper'],
  'gpu-inventory': ['gpus', 'health'], 'gpu-value': ['gpu:0:utilizationPercent'],
  'gpu-used': ['gpu:0:memoryUsedMiB'], 'gpu-total': ['gpu:0:memoryTotalMiB'],
  'gpu-temperature': ['gpu:0:temperatureC'], 'gpu-power': ['gpu:0:powerW'],
  'memory-file': ['memory'], 'boot-file': ['health', 'model:gpu-wrapper-201:live'],
};

test('Linux Python publisher -> JSON -> real web peer renderer: two-sided evidence contract',
  {skip: process.platform === 'win32' ? 'Requires POSIX os.statvfs in the Linux publisher fixture' : false}, async () => {
  const started = performance.now(), digest = createHash('sha256');
  const realNow = Date.now;
  Date.now = () => (1800000000 + Number(process.env.AGIW_FIXTURE_WALL_OFFSET || 0)) * 1000;
  // Isolated Python: no user site, PYTHONPATH or startup configuration. The
  // generator adds only its explicit fixture and selected product source paths.
  const generated = spawn('python3', ['-I', '-S', '-B', 'linux/tests/generate_pipeline_cases.py'],
    {stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = '', caseCount = 0;
  const snapshots = new Map();
  generated.stderr.setEncoding('utf8');
  generated.stderr.on('data', chunk => { stderr += chunk; });
  const completed = new Promise((resolve, reject) => {
    generated.once('error', reject);
    generated.once('exit', (code, signal) => resolve({code, signal}));
  });
  const violations = [];
  try {
  for await (const line of createInterface({input: generated.stdout})) {
    digest.update(line + '\n');
    const entry = JSON.parse(line);
    if (Object.hasOwn(entry, 'snapshotId')) {
      assert.ok(!snapshots.has(entry.snapshotId), 'snapshot IDs must be unique');
      snapshots.set(entry.snapshotId, entry.snapshot);
    }
    if (Object.hasOwn(entry, 'snapshotRef')) {
      assert.ok(snapshots.has(entry.snapshotRef), 'snapshot reference must follow its JSON payload');
      entry.snapshot = snapshots.get(entry.snapshotRef);
    }
    caseCount++;
    const label = entry.family === 'shape' ? `${entry.dimension}: ${JSON.stringify(entry.coordinates)}`
      : (entry.family || 'boundary') + '/' + entry.activity + ': ' + (entry.mutations.map(x => x.join(':')).join(' + ') || 'healthy');
    if (entry.error) { violations.push({case: label, field: 'publisher', output: entry.error}); continue; }
    const options = {feedFresh: true, now: entry.now, ...entry.viewOptions};
    let view;
    try { view = linuxPeerView(entry.snapshot, options); }
    catch (error) {
      violations.push({case: label, group: entry.expect?.[0]?.group || 'renderer', property: entry.property,
        dimension: entry.dimension, input: entry.coordinates, field: 'renderer', output: String(error)});
      continue;
    }
    const body = new Element('main');
    dom.render(body, view.rows);
    assert.deepEqual(body.children[0].children.filter(node => node.tagName === 'dd').map(node => node.textContent),
      view.rows.map(row => row[1]), 'the actual DOM renderer must preserve every tested value');
    if (entry.family === 'shape') {
      checkShape(entry, view, violations, label);
      // Invalid/future feeds beside a valid feed must not contaminate its details.
      const good = {...entry.snapshot, host: 'valid-neighbor', observedUnix: entry.now,
        observedAt: new Date(entry.now * 1000).toISOString()};
      assert.deepEqual(agiwPeersRows([entry.snapshot, good], options),
        options.feedFresh ? [...view.rows, ...linuxPeerView(good, options).rows] : agiwPeersRows([], options));
      assert.doesNotMatch(view.rows.map(row => row[1]).join(' '), /nothing running|quiet|no callers|no routes/i);
      continue;
    }
    const fields = dependencyFields(entry, view);
    for (const [source] of entry.mutations) {
      if (omitted.includes(source)) {
        assert.equal(Object.hasOwn(entry.snapshot, source), false, 'private or unprojected source must stay omitted');
        if (!entry.snapshot.sourceReasons?.[source]) violations.push({case: label, field: source, output: 'omitted without reason'});
      }
    }
    if (!entry.mutations.length) {
      if (entry.family === 'raw') {
        for (const key of ['health', 'gpus', 'memory', 'callers', 'listeners', 'model:gpu-wrapper-201:activity']) {
          assert.equal(fields[key]?.known, true, `healthy filesystem fixture must preserve ${key}`);
        }
        assert.equal(fields['model:gpu-wrapper-201:activity'].text, entry.activity);
        if (entry.activity === 'generating') assert.equal(fields['model:gpu-wrapper-201:liveTokensPerSecond'].text, '10');
        continue;
      }
      for (const key of ['health', 'modelsTotal', 'loaded', 'gpus', 'memory', 'callers', 'listeners',
        'model:gpu-wrapper-101:activity', 'model:gpu-wrapper-101:slotsBusy']) {
        assert.equal(fields[key]?.known, true, `healthy fixture must preserve ${key}`);
      }
      assert.equal(view.fields.loaded.text, '2');
      assert.equal(view.fields.callers.text, '1');
      assert.equal(fields['model:gpu-wrapper-101:activity'].text, entry.activity);
      if (entry.activity === 'generating') assert.equal(fields['model:gpu-wrapper-101:liveTokensPerSecond'].text, '10');
      assert.ok(view.rows.some(([, text]) => text.includes('codex')));
      continue;
    }
    const expired = entry.mutations.some(([, mode]) => ['stale', 'future'].includes(mode));
    const checks = expired ? [''] : entry.mutations.flatMap(([source]) => (entry.family === 'raw' ? rawDependencies : dependencies)[source]);
    if (entry.mutations.some(([source]) => ['pair-health', 'pair-models', 'wrapper-health', 'wrapper-models', 'processes', 'network'].includes(source))) {
      checks.push('modelsTotal', 'loaded');
    }
    for (const [key, value] of Object.entries(fields)) {
      if (checks.some(prefix => key.startsWith(prefix)) && value.known) {
        violations.push({case: label, field: key, output: value.text});
      }
      if (!value.known) assert.match(value.text, /^Unknown \(.+\)$/, `${label}: unknown needs a reason`);
    }
    assert.doesNotMatch(view.rows.map(row => row[1]).join(' '), /nothing running|quiet|no callers|no routes/i);
    // Real integration into the shared inspector, not a test-only renderer.
    const rows = agiwPeersRows([entry.snapshot], {feedFresh: true, now: entry.now});
    assert.deepEqual(rows, view.rows, 'the real peer inspector must use the tested renderer unchanged');
    assert.ok(rows.some(([key]) => key.endsWith(' · Routes')));
  }
  const result = await completed;
  assert.equal(result.code, 0, stderr);
  } finally { Date.now = realNow; if (generated.exitCode === null) generated.kill(); }
  assert.equal(caseCount, process.env.AGIW_INVARIANT_TRUTH_ONLY === '1' ? 136 : process.env.AGIW_INVARIANT_SHAPES_ONLY === '1' ? 772 : 63288);
  const seconds = (performance.now() - started) / 1000, sha256 = digest.digest('hex');
  if (process.env.AGIW_INVARIANT_REPORT) writeFileSync(process.env.AGIW_INVARIANT_REPORT,
    JSON.stringify({cases: caseCount, violations, seconds, sha256}, null, 2) + '\n');
  console.log(`Publisher stream SHA-256: ${sha256}; runtime ${seconds.toFixed(3)} s`);
  console.log(`Linux invariant: ${caseCount} cases; ${violations.length} violations`);
  assert.equal(violations.length, 0, JSON.stringify(violations.slice(0, 30), null, 2));
});
