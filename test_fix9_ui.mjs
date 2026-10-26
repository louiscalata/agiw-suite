import assert from 'node:assert/strict';
import test from 'node:test';
import { activitySummary } from './web/map-layout.mjs';

test('fix9: completed PC history cannot prove total inactivity with unknown routes', () => {
  for (const state of ['success', 'error', 'unknown']) {
    const summary = activitySummary([{ kind: 'pc-job', state, target: 'PC', client: 'Codex' }],
      { live: true, jobsAvailable: true, liveJobsAvailable: true, routesAvailable: false });
    assert.doesNotMatch(summary.line, /Nothing running now/);
    assert.equal(summary.tiles.find(([label]) => label === 'In flight')[1], 'Unknown');
    assert.match(summary.meaning, /route.*unknown|route.*unavailable/i);
  }
});

test('fix9: capped activity rows give lower bounds, never exact totals or an idle claim', () => {
  const summary = activitySummary([{ kind: 'pc-job', state: 'success', target: 'PC', client: 'Codex' }],
    { live: true, jobsAvailable: true, liveJobsAvailable: true, routesAvailable: true, countsComplete: false });
  assert.equal(summary.tiles.find(([label]) => label === 'Answered')[1], 'At least 1');
  assert.equal(summary.tiles.find(([label]) => label === 'In flight')[1], 'Unknown');
  assert.doesNotMatch(summary.line, /Nothing running now/);
});
