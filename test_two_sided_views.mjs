import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, writeFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const layout = await import(process.env.AGIW_INVARIANT_LAYOUT || './web/map-layout.mjs');
const {windowsJobsView, windowsLaneView, windowsLaneRows, activityFeed, activitySummary, windowsSharePublishRow} = layout;
const app = readFileSync(process.env.AGIW_INVARIANT_APP || new URL('./web/app.js', import.meta.url), 'utf8')
  .replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];
const NOW = 1800000000;
const success = (lane = 'fast') => ({id: 'answer', state: 'success', model: 'model:q4', lane, client: 'codex',
  ageSeconds: 5, elapsedSeconds: 2, predictedPerSecond: 30, promptPerSecond: 80});
const job = lane => ({id: 'open', lane, client: 'claude', ageSeconds: 1, timeoutSeconds: 120});
const worker = busy => ({state: 'advertised', ageSeconds: 1, headless: {state: 'off'},
  lanes: {fast: {up: true, model: 'fast-alias', slotsBusy: busy, slotsTotal: 2},
    deep: {up: true, model: 'deep-alias', slotsBusy: 0, slotsTotal: 1}}});

test('Two-sided jobs and frozen-view generator: exact healthy values, independent clocks, capture labels', () => {
  const violations = [], dimensions = {};
  let cases = 0, current;
  const begin = (property, dimension, input) => {
    current = {case: `${dimension}:${JSON.stringify(input)}`, property, dimension, input};
    cases++; dimensions[dimension] = (dimensions[dimension] || 0) + 1;
  };
  const equal = (field, output, expected) => {
    if (JSON.stringify(output) !== JSON.stringify(expected)) violations.push({...current, field, output, expected});
  };
  for (const variant of ['baseline', 'one-more-job', 'older-success', 'alias', 'recent-success', 'idle-lane'])
    for (const extra of [0, 2.999]) for (const reverse of [false, true]) for (const lane of ['fast', 'deep']) {
      begin('no-false-doubt', 'healthy-jobs', [variant, extra, reverse, lane]);
      const answer = success(lane);
      if (variant === 'alias') answer.model = 'vendor/alias:q8';
      if (variant === 'recent-success') answer.ageSeconds = .001;
      // A fresh lastSuccess can be newer than recent's retained rows. That list
      // is bounded and isn't a freshness clock for the independently held success.
      const recent = variant === 'one-more-job' ? [{...answer, id: 'older', state: 'timeout', ageSeconds: 40}]
        : variant === 'older-success' ? [{...answer, id: 'older', ageSeconds: 40}] : [answer];
      const inFlight = variant === 'idle-lane' ? [] : variant === 'one-more-job' ? [job(lane), {...job(lane), id: 'open-2'}] : [job(lane)];
      if (reverse) { recent.reverse(); inFlight.reverse(); }
      const raw = {schemaVersion: 1, complete: true, journal: 'recorded', inFlight, recent, lastSuccess: answer};
      const jobs = windowsJobsView(raw, {feedFresh: true, snapshotAge: extra});
      equal('jobs', Object.fromEntries(['running','inFlight','recent','lastVerified','current'].map(key=>[key,jobs[key]])), {running: inFlight.length > 0,
        inFlight: inFlight.map(row => ({...row, ageSeconds: row.ageSeconds + extra})),
        recent: recent.map(row => ({...row, ageSeconds: row.ageSeconds + extra})),
        lastVerified: {...answer, ageSeconds: answer.ageSeconds + extra}, current: true});
      const lanes = windowsLaneView(worker(variant === 'idle-lane' ? 0 : 1), jobs, {feedFresh: true, snapshotAge: extra});
      equal('lane rows', windowsLaneRows(lanes), [['Lane fast · fast-alias', `Up · ${variant === 'idle-lane' ? 0 : 1}/2 slots busy`], ['Lane deep · deep-alias', 'Up · 0/1 slots busy']]);
      equal('headless', lanes.headless, {on: false, label: 'Headless off'});
      const feed = activityFeed(jobs, [], {feedFresh: true});
      equal('feed cardinality', feed.length, inFlight.length + recent.length);
      const summary = activitySummary(feed, {live: true});
      equal('summary tiles', summary.tiles, [['In flight', String(inFlight.length)], ['Answered', String(recent.filter(row => row.state === 'success').length)],
        ['Failed', variant === 'one-more-job' ? '1' : '0'], ['Routes', '0']]);
    }
  for (const age of [-.001, 0, 1799.999, 1800, 1800.001]) for (const extra of [0, .001]) for (const fresh of [false, true]) {
    begin('two-sided', 'success-event-clock', [age, extra, fresh]);
    const answer = {...success(), ageSeconds: age};
    const jobs = windowsJobsView({schemaVersion: 1, complete: true, journal: 'recorded', recent: [answer], inFlight: [], lastSuccess: answer}, {feedFresh: fresh, snapshotAge: extra});
    equal('current success', jobs.current, fresh && age >= 0 && age + extra <= 1800);
  }
  // An old last-publish event does not stale a freshly observed publisher. The
  // observation freshness is supplied separately via feedFresh/snapshotAge.
  for (const eventAge of [0, 5, 86400]) for (const extra of [0, 2.999, 3.001]) {
    begin('two-sided', 'publish-observation-vs-event', [eventAge, extra]);
    equal('publish row', windowsSharePublishRow([{id: 'share-publish', outcome: 'ok', ageSeconds: eventAge}], {feedFresh: true, snapshotAge: extra}),
      ['This PC', extra <= 3 ? `Published ${Math.floor(eventAge + extra)} s ago` : 'Publishing state unknown']);
  }
  for (const state of ['empty', 'success', 'in-flight', 'failed', 'route'])
    for (const capturedAt of [0, NOW]) for (const elapsed of [0, 20, 3600]) {
      begin('capture-time', 'frozen-summary-and-app', [state, capturedAt, elapsed]);
      const feed = state === 'empty' ? [] : state === 'route' ? [{kind: 'route', state: 'in-flight-stale', ageSeconds: 1, target: 'Nisi Inference → Mac'}]
        : activityFeed({inFlight: state === 'in-flight' ? [job('fast')] : [], recent: ['success', 'failed'].includes(state)
          ? [{...success(), state: state === 'failed' ? 'timeout' : 'success'}] : []}, [], {feedFresh: false});
      const summary = activitySummary(feed, {live: false, capturedAt});
      const stamp = new Date(capturedAt * 1000).toISOString();
      equal('no now claim', /\bnow\b/i.test(summary.line + ' ' + summary.meaning), false);
      equal('summary capture time', (summary.line + ' ' + summary.meaning).includes(stamp), true);
      const elements = new Map();
      const document = {getElementById(id) { if (!elements.has(id)) elements.set(id, {textContent: '', classList: {toggle(){}}}); return elements.get(id); },
        body: {classList: {toggle(){}}}};
      class Clock extends Date { static now() { return (capturedAt + elapsed) * 1000; } }
      const api = runInNewContext(`${app}\n;renderVitals=renderMemoryBanner=syncWebPulse=updateOnlineCodeMode=()=>{};
        ({check(data){snapshot=data;connected=true;paused=true;updateFreshness();}})`, {...layout, document, Date: Clock});
      api.check({sampledAt: capturedAt, fullSampledAt: capturedAt});
      equal('app capture time', document.getElementById('sampleAge').textContent, `Captured ${stamp}`);
      equal('app paused state', document.getElementById('connection').textContent, 'VIEW PAUSED');
    }
  equal('case count', cases, 107);
  if (process.env.AGIW_VIEWS_REPORT) writeFileSync(process.env.AGIW_VIEWS_REPORT, JSON.stringify({cases, dimensions, violations}, null, 2) + '\n');
  console.log(`Two-sided views: ${cases} cases; ${violations.length} violations`);
  assert.equal(violations.length, 0, JSON.stringify(violations.slice(0, 15), null, 2));
});
