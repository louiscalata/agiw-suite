import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { harness } from './test_support/monitor-harness.mjs';
import * as layout from './web/map-layout.mjs';
import { checkFix11 } from './test_support/fix11_assertions.mjs';
import { checkFix1 } from './test_support/fix1_assertions.mjs';

const now = 1_800_000_000;
let rendererNow = now;
class ClockDate extends Date { static now() { return rendererNow * 1000; } }

test('fix9 generative invariant: Python publishers -> JSON -> actual web renderer, two-sided exact/degraded/frozen properties and every single/pair degradation',
  {skip: process.platform === 'win32' ? 'Requires POSIX fcntl through the Mac telemetry publisher' : false}, () => {
  const child = spawnSync('python3', ['-B', 'test_support/fix9_cases.py'], {
    cwd: fileURLToPath(new URL('.', import.meta.url)), encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 });
  assert.equal(child.status, 0, child.stderr);
  const cases = child.stdout.trim().split('\n').map(line => JSON.parse(line));
  const failures = [], counts = {}, dimensions = {}, properties = {};
  for (const packet of cases) {
    const { host, changes, snapshot, status } = packet;
    const bad = new Set(changes.map(([sid]) => sid));
    const scenario = snapshot.fixtureScenario, dimension = scenario?.dimension || 'fix9';
    dimensions[dimension] ??= { cases: 0, failedCases: 0, violations: 0 };
    const exactIntegration = dimension==='hands-off-signals' ? scenario.expected.active!==null
      : ['route-inspector-clock','lan-observation-clock'].includes(dimension) ? scenario.expected.current
      : ['activity-ui-cap','history-helper-read'].includes(dimension) ? !scenario.expected.truncated : false;
    const property = dimension === 'frozen-capture' ? 'frozen-capture' : dimension === 'healthy-exact' || exactIntegration || ['route-semantics','journal-semantics','source-boundary','history-read','fast-overlay','journal-read'].includes(dimension) && scenario.expected.current ? 'healthy-exact' : 'degraded-safety';
    properties[property] ??= { cases: 0, failedCases: 0, violations: 0 };
    const before = failures.length;
    const check = (condition, field, output) => { if (!condition) failures.push({ host, property, dimension, changes, inputs: packet.inputs, field, output }); };
    rendererNow = scenario?.expected.rendererNow ?? now;
    const { api, $ } = harness({ extra: { Date: ClockDate } });
    try {
      api.setFeed(snapshot); if (dimension === 'frozen-capture') api.setPaused(scenario.expected.paused); $('drawer').hidden = false; api.renderSummary(); api.select('runtime'); api.render();
      const input = api.vitalsInput(); api.renderActivity(input.feed); api.renderVitals(input);
      if(['hands-off-signals','route-inspector-clock','activity-ui-cap','history-helper-read','lan-observation-clock'].includes(dimension))checkFix1({packet,api,$,input,check});
      if (['healthy-exact','route-semantics','journal-semantics','frozen-capture','source-boundary','history-read','fast-overlay','journal-read'].includes(dimension)) checkFix11({packet, api, $, input, check});
      if (dimension === 'route-clock') {
        const expected = scenario.expected, tiles = $('activityTiles').textContent;
        check(/RoutesUnknown/.test(tiles) === !expected.routesAvailable, 'route coverage clock', tiles);
        if (!expected.routesAvailable) check(!/Nothing running now|In flight0/.test($('activityStatus').textContent + tiles), 'expired inactivity', $('activityStatus').textContent + tiles);
        check(input.feed.some(row => row.kind === 'route' && row.state === 'in-flight') === expected.routeRunning,
          'route liveness clock', input.feed);
      }
      if (dimension === 'route-cap') {
        const expected = scenario.expected, bound = snapshot.activity.historyBound;
        check(bound.truncated === expected.truncated, 'collector truncation', bound);
        if (expected.missingActive) check(bound.missingFromAgeSeconds === 0, 'omitted live evidence floor', bound);
        check(input.feed.filter(row => row.kind === 'route' && row.state === 'in-flight').length === expected.visibleRunning,
          'bounded visible routes', input.feed);
        if (expected.truncated) {
          check(!/Nothing running now|In flight0|Routes0/.test($('activityStatus').textContent + $('activityTiles').textContent),
            'bounded inactivity', $('activityStatus').textContent + $('activityTiles').textContent);
          if (expected.visibleRunning) check(/^At least /.test($('activityStatus').textContent), 'bounded running total', $('activityStatus').textContent);
          if (expected.visibleRunning) check(/Routesat least /i.test($('activityTiles').textContent), 'bounded route tile', $('activityTiles').textContent);
        }
      }
      if (dimension === 'lane-window') {
        const expected = scenario.expected;
        check(snapshot.history.at(-1).loaded === (expected.inventory ? 2 : null), 'lane history deadline', snapshot.history.at(-1));
        check($('loadedCount').textContent === (expected.inventory ? '2' : '—'), 'lane UI inventory deadline', $('loadedCount').textContent);
        check(snapshot.activityKnown === expected.activity, 'lane activity deadline', snapshot.activityKnown);
        for (const lane of snapshot.lanes) {
          check((lane.slotsTotal != null) === expected.activity && (lane.slotsBusy != null) === expected.activity,
            'lane raw slots deadline', lane);
        }
        for (const lane of Object.values(snapshot.windowsWorker.lanes)) {
          check(lane.up === (expected.inventory ? true : null), 'lane health deadline', lane);
          check((lane.slotsTotal != null) === expected.activity && (lane.slotsBusy != null) === expected.activity,
            'lane worker slots deadline', lane);
        }
      }
      const inventory = bad.has('model-row') || (host === 'windows' ? bad.has('lane-inventory') || bad.has('lane-config') : bad.has('lms-ps') && bad.has('lmstudio-api'));
      const activity = inventory || bad.has(host === 'windows' ? 'llama-slots' : 'lms-ps');
      if (host === 'windows' && inventory) check(snapshot.machines?.find(m => m.role === 'this-machine')?.loadedCount == null, 'machine loaded total', snapshot.machines);
      if (inventory) {
        check(snapshot.history.at(-1).loaded === null, 'history loaded total', snapshot.history.at(-1));
        check($('loadedCount').textContent === '—', 'loaded total', $('loadedCount').textContent);
        const report = layout.pcReportView({ ...status, via: 'share', ageSeconds: 0 }, { feedFresh: true });
        check(!report.known || /unknown|at least/i.test(report.words), 'published residency', report.words);
      }
      if (activity) {
        check(snapshot.history.at(-1).active === null, 'history working total', snapshot.history.at(-1));
        check($('activeCount').textContent === '—', 'working total', $('activeCount').textContent);
        check(!/quiet|idle|working now/i.test($('headline').textContent), 'runtime headline', $('headline').textContent);
        check(!status.models?.some(m => ['idle', 'busy', 'generating'].includes(m.activity)), 'published activity', status.models);
      }
      if (bad.has(host === 'windows' ? 'lane-processes' : 'local-callers')) {
        const text = $('inspector').textContent;
        check(!/None connected|No local process is calling/.test(text), 'caller inspector', text);
        check(snapshot.localCallers === null || !Array.isArray(snapshot.localCallers), 'caller evidence', snapshot.localCallers);
      }
      if (bad.has(host === 'windows' ? 'codemode-runs' : 'windows-jobs')) {
        const tiles = $('activityTiles').textContent;
        check(/AnsweredUnknown/.test(tiles), 'answered total', tiles);
        check(/FailedUnknown/.test(tiles), 'failed total', tiles);
      }
      if (host === 'mac' && ['router-active', 'monitor-receipts', 'router-archive'].some(s => bad.has(s))) {
        check(/RoutesUnknown/.test($('activityTiles').textContent), 'route total', $('activityTiles').textContent);
        check(!/Nothing running now/.test($('activityStatus').textContent), 'combined activity', $('activityStatus').textContent);
      }
      if (bad.has('pipeline-router')) check(!/Idle|Running|queued/i.test(input.vitals.route.detail), 'Mac route state', input.vitals.route.detail);
      if (bad.has('mac-gpu')) check(!layout.macGpuView(snapshot.macGpu, { feedFresh: true }).known, 'Mac GPU reading', snapshot.macGpu);
      if (bad.has('windows-telemetry')) check(!layout.pcReportView(snapshot.windowsLive, { feedFresh: true }).known, 'PC report from Mac', snapshot.windowsLive);
      if (bad.has('windows-worker')) check(!layout.windowsLaneView(snapshot.windowsWorker, null, { feedFresh: true }).visible, 'PC lane health from Mac', snapshot.windowsWorker);
      if (bad.has('route-queue')) {
        check(!/Idle/.test(input.vitals.route.detail), 'route state', input.vitals.route.detail);
        check(status.routeQueue == null || Object.values(status.routeQueue).every(v => v == null), 'published queue counts', status.routeQueue);
      }
      if (bad.has('pc-gpu')) check(/Unknown/.test($('inspector').textContent) && snapshot.pcGpus.every(g => g.utilizationPercent == null), 'GPU reading', snapshot.pcGpus);
      if (bad.has('mem-guard')) check(snapshot.memory?.level === 'unknown', 'memory level', snapshot.memory);
      if (bad.has('pc-gpu') || bad.has('hands-off')) check(status.handsOff?.active === null, 'hands-off clear claim', status.handsOff);
      if (host==='windows' && bad.has('lane-processes') && !bad.has('pc-gpu') && !bad.has('hands-off')) check(status.handsOff?.active === false, 'clear independent of attribution', status.handsOff);
      if (bad.has('nvidia-pair')) check(status.pair?.processCount == null && status.pair?.engineCount == null, 'PAIR counts', status.pair);
      if (bad.has('pc-report')) check(/at least 1 busy/.test(input.vitals.pc.detail), 'partial busy count', input.vitals.pc.detail);
      if (bad.has('activity-live-counts')) check(/^At least 12 routes running now/.test($('activityStatus').textContent), 'capped running route count', $('activityStatus').textContent);
      if (bad.has('activity-counts')) {
        check(/Answeredat least 12/i.test($('activityTiles').textContent), 'capped visible count', $('activityTiles').textContent);
        check(!/Nothing running now/.test($('activityStatus').textContent), 'capped inactivity', $('activityStatus').textContent);
      }
      if (bad.has('agiw-peers')) {
        const peerView = layout.macPeerView(snapshot.macPeer, { feedFresh: true });
        check(!/\b\d+ loaded\b/i.test(JSON.stringify(peerView)), 'Mac presence residency', peerView);
        const peerRows = layout.agiwPeersRows(snapshot.peers, { feedFresh: true, host,
          source: snapshot.sources.find(s => s.id === 'agiw-peers') });
        check(!/\b\d+ loaded\b|None has published/.test(JSON.stringify(peerRows)), 'peer residency', peerRows);
      }
      if (activity && host === 'windows') {
        for (const model of snapshot.models) {
          api.select(`model:windows:${model.id}`); api.render();
          check(!/\d+\/\d+ busy/.test($('inspector').textContent), 'model slot inspector', $('inspector').textContent);
        }
      }
      if (!changes.length) {
        check($('loadedCount').textContent === '2', 'healthy loaded control', $('loadedCount').textContent);
        check($('activeCount').textContent === String(snapshot.fixtureExpectedWorking), 'healthy activity control', $('activeCount').textContent);
      }
    } catch (error) { failures.push({ host, dimension, changes, field: 'renderer exception', output: error.stack }); }
    counts[host] = (counts[host] || 0) + 1;
    dimensions[dimension].cases++;
    dimensions[dimension].violations += failures.length - before;
    dimensions[dimension].failedCases += Number(failures.length > before);
    properties[property].cases++;
    properties[property].violations += failures.length-before;
    properties[property].failedCases += Number(failures.length>before);
  }
  const examples = [...new Map(failures.map(f => [`${f.host}:${f.changes.map(c=>c[0]).join('+')}:${f.field}`, f])).values()];
  const report = { cases: cases.length, counts, dimensions, properties, violations: failures.length, failures };
  if (process.env.FIX10_REPORT) writeFileSync(process.env.FIX10_REPORT, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({ ...report, failures: undefined, examples: examples.slice(0, 30) }));
  assert.equal(dimensions.fix9.cases, 16881); // original coverage is retained verbatim
  assert.equal(dimensions['route-clock'].cases, 1568);
  assert.equal(dimensions['route-cap'].cases, 216);
  assert.equal(dimensions['lane-window'].cases, 120);
  assert.equal(dimensions['healthy-exact'].cases, 28);
  assert.equal(dimensions['route-semantics'].cases, 180);
  assert.equal(dimensions['journal-semantics'].cases, 90);
  assert.equal(dimensions['frozen-capture'].cases, 48);
  assert.equal(dimensions['source-boundary'].cases, 42);
  assert.equal(dimensions['history-read'].cases, 30);
  assert.equal(dimensions['fast-overlay'].cases, 36);
  assert.equal(dimensions['journal-read'].cases, 6);
  assert.equal(dimensions['hands-off-signals'].cases,72);
  assert.equal(dimensions['route-inspector-clock'].cases,40);
  assert.equal(dimensions['activity-ui-cap'].cases,48);
  assert.equal(dimensions['history-helper-read'].cases,16);
  assert.equal(dimensions['lan-observation-clock'].cases,48);
  assert.equal(cases.length, 19469);
  assert.equal(failures.length, 0, JSON.stringify(examples.slice(0, 12), null, 2));
});
