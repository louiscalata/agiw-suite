import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fixNisiStepView, fixNisiStepsView, markerAgeText, nisiRecoveryView, fixNisiEvidence, fixNisiMessage, NISI_MARKER_MIN_AGE_SECONDS } from './web/online-code-mode.mjs';
import { memoryView, memoryBannerView, memoryDismissal, memoryHold, memoryHoldAt, memoryAnnouncement, memorySize, vitalsView, MEMORY_RANK,
  MEMORY_EASE_SECONDS, MEMORY_SAMPLE_GAP_SECONDS } from './web/map-layout.mjs';

// 27 Sep 2026: the pure helpers behind Fix Nisi Inference (Fix scope "nisi") and the memory UI (snapshot.memory from mem_guard).

const repair = readFileSync(new URL('./online_code_repair.py', import.meta.url), 'utf8');
const fixNisiSource = repair.slice(repair.indexOf('    def _fix_nisi(self)'), repair.indexOf('    def _headless(self'));

test('Fix Nisi Inference: every step the controller can record has a plain label, a sentence and a known tone', () => {
  assert.ok(fixNisiSource.length > 2000, 'the controller section was found');
  const pairs = [...fixNisiSource.matchAll(/_step\("([a-z-]+)", "([a-z-]+)"/g)].map(m => [m[1], m[2]]);
  // Recorded through variables: the server-idle verdicts, the journal outcome, and the status reads' own step names.
  pairs.push(['server-idle', 'busy'], ['server-idle', 'unknown'], ['journal', 'failed'], ['nisi-status', 'unavailable'], ['verify', 'unavailable']);
  assert.ok(pairs.length >= 40, `found ${pairs.length}`);
  for (const [name, result] of pairs) {
    const view = fixNisiStepView({ name, result });
    assert.ok(view, `${name} ${result}`);
    assert.doesNotMatch(view.text, /^Recorded:/, `${name} ${result} has a plain sentence`);
    assert.ok(['ok', 'info', 'warn', 'bad'].includes(view.tone), `${name} ${result}`);
    assert.equal(view.mark, { ok: '✓', info: '•', warn: '!', bad: '✕' }[view.tone]);
    assert.notEqual(view.label, name, `${name} has a plain label`);
  }
  // The ten step names of the spec, in the controller.
  for (const name of ['route-status', 'nisi-status', 'marker', 'owner-lock', 'server-idle', 'recover', 'pair', 'jev', 'verify', 'journal'])
    assert.ok(pairs.some(([n]) => n === name), name);
  // A stop is never a tick.
  for (const [name, result] of [['owner-lock', 'busy'], ['server-idle', 'busy'], ['marker', 'too-young'], ['route-status', 'active'], ['recover', 'mismatch'], ['pair', 'missing']])
    assert.notEqual(fixNisiStepView({ name, result }).tone, 'ok', `${name} ${result}`);
});

test('converge: a queued route never offers Fix Nisi Inference, and a refusing install fence has its own step words', () => {
  const view = nisiRecoveryView({ status: 'queued', runId: 'run-q', recoveryRequired: true, pendingMarkerObserved: true },
    [{ id: 'nisi', state: 'unresolved' }], { feedFresh: true });
  assert.equal(view.needed, false);
  const step = fixNisiStepView({ name: 'route-status', result: 'install-refusing' });
  assert.equal(step.tone, 'warn');
  assert.doesNotMatch(step.text, /^Recorded:|press|progress/);
  assert.match(fixNisiStepView({ name: 'route-status', result: 'install-in-progress' }).text, /install in progress/);
});

test('Fix Nisi Inference: step identities join the sentence only when they are printable and sane', () => {
  assert.deepEqual(fixNisiStepView({ name: 'marker', result: 'stale', ageSeconds: '33000', owner: 'anonymous (legacy)', inputSha256: '81dbab4f0000' }),
    { name: 'marker', result: 'stale', label: 'Pending record', text: '9 h 10 min old, owner anonymous (legacy): old enough to recover', tone: 'ok', mark: '✓' });
  assert.equal(fixNisiStepView({ name: 'marker', result: 'too-young', ageSeconds: '240', owner: 'route-1' }).text,
    '4 min old, owner route-1: too recent; waiting for a slow call to settle');
  assert.equal(fixNisiStepView({ name: 'pair', result: 'resident', author: 'google/gemma-4-26b-a4b-qat', reviewer: 'qwen/qwen3.8-27b' }).text,
    'google/gemma-4-26b-a4b-qat (author) + qwen/qwen3.8-27b (reviewer)');
  assert.equal(fixNisiStepView({ name: 'route-status', result: 'active', runId: 'marketscout.brainstorm.20260926.qwen' }).text,
    'An unresolved route run is open (marketscout.brainstorm.20260926.qwen); stopped');
  // Bad identities are left out; the base sentence stays.
  assert.equal(fixNisiStepView({ name: 'marker', result: 'stale', ageSeconds: '12abc', owner: 'x\ny' }).text, 'Old enough to recover');
  assert.equal(fixNisiStepView({ name: 'marker', result: 'stale', ageSeconds: 33000 }).text, 'Old enough to recover', 'the controller writes the age as a string');
  assert.equal(fixNisiStepView({ name: 'pair', result: 'resident', author: 'only-one' }).text, 'Two distinct models are resident');
  // An unknown step or result is shown as recorded, never as a success.
  assert.deepEqual(fixNisiStepView({ name: 'mystery', result: 'odd-thing' }), { name: 'mystery', result: 'odd-thing', label: 'mystery', text: 'Recorded: odd thing', tone: 'muted', mark: '•' });
  assert.equal(fixNisiStepView({ name: 'jev', result: 'maybe' }).tone, 'muted');
  assert.equal(fixNisiStepView({ name: '__proto__', result: 'constructor' }).label, '__proto__');
  // Unreadable entries are dropped; the list is bounded.
  assert.deepEqual(fixNisiStepsView([null, 'route-status', { name: 'x' }, { name: 'bad\nname', result: 'idle' }, { name: 'jev', result: 'é' }]), []);
  assert.equal(fixNisiStepsView(Array.from({ length: 50 }, () => ({ name: 'jev', result: 'opted-in' }))).length, 32);
  assert.deepEqual(fixNisiStepsView('nope'), []);
});

test('Fix Nisi Inference: an open router run with a pending record says the run blocks the fix, in the route chip\'s words', () => {
  const on = { feedFresh: true, macOwner: true };
  // The router's own 5-key marker (owner = runId), as a crashed route run leaves it.
  const both = nisiRecoveryView(pendingPipeline({ runId: 'route-20260927-011504', stage: 'review', pendingMarkerAgeSeconds: 7260, pendingMarkerOwner: 'route-20260927-011504' }), unresolved, on);
  assert.deepEqual([both.needed, both.blocked, both.line, both.age, both.owner, both.young], [true, true, 'Unsettled run recorded', '2 h 1 min', 'route-20260927-011504', false]);
  assert.equal(both.meaning, 'Route run route-20260927-011504 is still open, and a Nisi call left a pending record 2 h 1 min ago. '
    + 'Fix Nisi Inference stops at its first step until that run is resolved; its check names the manual steps.');
  assert.doesNotMatch(both.meaning, /recovers it only when/);
  // The chip reads the same words for the same state.
  const chip = vitalsView({ fresh: true, models: [], pipeline: { status: 'recovery-required', runId: 'route-20260927-011504' }, nisi: { state: 'unresolved' } }).route;
  assert.deepEqual(chip, { tone: 'warn', detail: both.line });
  // A legacy owner different from the run is named; a young record under an open run is still blocked, not "waiting".
  const legacy = nisiRecoveryView(pendingPipeline({ runId: 'route-9', pendingMarkerAgeSeconds: 120, pendingMarkerOwner: 'anonymous (legacy)' }), unresolved, on);
  assert.equal(legacy.meaning.split('. ')[0], 'Route run route-9 is still open, and a Nisi call left a pending record 2 min ago (owner: anonymous (legacy))');
  assert.deepEqual([legacy.blocked, legacy.young], [true, false]);
  // An unprintable run id still blocks (the chip reads any recorded run); it is just not printed.
  const odd = nisiRecoveryView(pendingPipeline({ runId: 'run\nid' }), unresolved, on);
  assert.deepEqual([odd.blocked, odd.line], [true, 'Unsettled run recorded']);
  assert.match(odd.meaning, /^A route run is still open, and a Nisi call left a pending record\. /);
  // No run: not blocked.
  assert.equal(nisiRecoveryView(pendingPipeline({ runId: null }), unresolved, on).blocked, false);
  assert.equal(nisiRecoveryView(pendingPipeline({ runId: '' }), unresolved, on).blocked, false);
});

test('Fix Nisi Inference: the young-record cut-off is the controller\'s NISI_MARKER_MIN_AGE (drift check)', () => {
  const cap = /^NISI_CALL_CAP_SECONDS = (\d+)$/m.exec(repair), rule = /^NISI_MARKER_MIN_AGE = max\((\d+), (\d+) \* NISI_CALL_CAP_SECONDS\)$/m.exec(repair);
  assert.ok(cap && rule, 'the controller still defines the cap and derives the minimum age as max(floor, k * cap)');
  assert.equal(NISI_MARKER_MIN_AGE_SECONDS, Math.max(Number(rule[1]), Number(rule[2]) * Number(cap[1])));
  const on = { feedFresh: true, macOwner: true };
  const at = seconds => nisiRecoveryView(pendingPipeline({ pendingMarkerAgeSeconds: seconds }), unresolved, on);
  assert.equal(at(NISI_MARKER_MIN_AGE_SECONDS - 1).young, true);
  assert.equal(at(NISI_MARKER_MIN_AGE_SECONDS).young, false);
  assert.match(at(NISI_MARKER_MIN_AGE_SECONDS - 1).meaning, new RegExp(`at least ${NISI_MARKER_MIN_AGE_SECONDS / 60} min old\\.$`));
  // The controller's own too-young sentence names the same minutes.
  assert.match(repair, /it is at least \{NISI_MARKER_MIN_AGE \/\/ 60\} min old so a slow call can settle/);
});

test('Fix Nisi Inference: the panel prints the controller\'s message without the digest, the repeated status or a second "no model"', () => {
  const ready = 'Nisi + Jev ready. Recovered the Nisi marker through the launcher (age 9 h 12 min, owner anonymous (legacy), input 81dbab4f0c2e). No model inference was run.';
  assert.equal(fixNisiMessage(ready, 'ready', { scoped: true }), 'Recovered the Nisi marker through the launcher (age 9 h 12 min, owner anonymous (legacy)).');
  // Without the scope note (the Online Code summary) the "no model" sentence stays; the digest never does.
  assert.equal(fixNisiMessage(ready, 'ready'), 'Recovered the Nisi marker through the launcher (age 9 h 12 min, owner anonymous (legacy)). No model inference was run.');
  const mixed = 'Recovered the Nisi marker through the launcher (age 2 h 1 min, owner route-7, input 3f9a1c0d2b7e). Nisi + Jev not ready: Nisi needs a second resident model. No model inference was run.';
  assert.equal(fixNisiMessage(mixed, 'needs-action', { scoped: true }), 'Recovered the Nisi marker through the launcher (age 2 h 1 min, owner route-7). Nisi + Jev not ready: Nisi needs a second resident model.');
  assert.equal(fixNisiMessage('Nisi + Jev ready. No Nisi marker needed recovery. No model inference was run.', 'ready', { scoped: true }), 'No Nisi marker needed recovery.');
  assert.equal(fixNisiMessage('Nisi Inference ready. No Nisi marker needed recovery. No model inference was run.', 'ready', { scoped: true }), 'No Nisi marker needed recovery.');
  // Running, a stop without the sentence, and non-strings are left as written.
  assert.equal(fixNisiMessage('Checking the route.', 'running', { scoped: true }), 'Checking the route.');
  assert.equal(fixNisiMessage('A Nisi call is still running; not recovering.', 'needs-action', { scoped: true }), 'A Nisi call is still running; not recovering.');
  assert.equal(fixNisiMessage(undefined, 'ready'), undefined);
  // The recorded evidence keeps its facts but not the digest prefix or the recovered file name.
  assert.equal(fixNisiEvidence('kind=codemode.nisi.pending.v1; age=9 h 12 min; input=81dbab4f0c2e; owner=anonymous (legacy)'), 'kind=codemode.nisi.pending.v1; age=9 h 12 min; owner=anonymous (legacy)');
  assert.equal(fixNisiEvidence('file=recovered-5b0e9c1f.json; remoteInferenceStopped=NOT_OBSERVED'), 'remoteInferenceStopped=NOT_OBSERVED');
  assert.equal(fixNisiEvidence('file=recovered-5b0e9c1f.json'), '');
  assert.equal(fixNisiEvidence(undefined), undefined);
});

test('Fix Nisi Inference: marker ages read like the controller writes them', () => {
  assert.deepEqual([45, 720, 33000, 266400, 0].map(markerAgeText), ['45 s', '12 min', '9 h 10 min', '3 d 2 h', '0 s']);
  for (const bad of [-1, NaN, Infinity, '60', null, 4000 * 86400]) assert.equal(markerAgeText(bad), null);
});

const pendingPipeline = extra => ({ status: 'recovery-required', recoveryRequired: true, pendingMarkerObserved: true, runId: null, ...extra });
const unresolved = [{ id: 'nisi', state: 'unresolved', detail: 'Nisi pending marker present; the owner must recover it' }, { id: 'jev', state: 'configured' }];

test('Fix Nisi Inference: the inspector offers it only for a pending record on a fresh Mac feed, with the age and owner when known', () => {
  const on = { feedFresh: true, macOwner: true };
  assert.deepEqual(nisiRecoveryView(pendingPipeline({ pendingMarkerAgeSeconds: 33000, pendingMarkerOwner: 'anonymous (legacy)' }), unresolved, { ...on, snapshotAge: 60 }),
    { needed: true, blocked: false, line: 'Nisi call left a pending record 9 h 11 min ago', age: '9 h 11 min', owner: 'anonymous (legacy)', young: false,
      meaning: 'Owner: anonymous (legacy). New Nisi work is refused until it is recovered. Fix Nisi Inference recovers it only when it can prove nothing is running.' });
  // Without the snapshot fields: no numbers at all.
  const plain = nisiRecoveryView(pendingPipeline({}), unresolved, on);
  assert.equal(plain.line, 'Nisi call left a pending record');
  assert.doesNotMatch(plain.line + plain.meaning, /\d/);
  // A young record may still be a running call.
  const young = nisiRecoveryView(pendingPipeline({ pendingMarkerAgeSeconds: 120, pendingMarkerOwner: 'route-7' }), unresolved, on);
  assert.equal(young.young, true);
  assert.match(young.meaning, /^Owner: route-7\. The call may still be running; Fix Nisi Inference waits until the record is at least 10 min old\.$/);
  // Either signal alone offers it: an unresolved component on an idle-looking route, or a recovery-required route.
  assert.equal(nisiRecoveryView({ status: 'idle' }, unresolved, on).needed, true);
  assert.equal(nisiRecoveryView(pendingPipeline({}), [], on).needed, true);
  // A router run that needs recovery (no pending marker): the route chip's words, no marker facts, and the fix is blocked.
  const run = nisiRecoveryView({ status: 'recovery-required', runId: 'route-20260927-1', pendingMarkerObserved: false, pendingMarkerAgeSeconds: 99 }, [{ id: 'nisi', state: 'ready' }], on);
  assert.deepEqual([run.line, run.blocked, run.age, run.owner], ['Unsettled run recorded', true, null, null]);
  assert.equal(run.meaning, 'Route run route-20260927-1 is still open and reports recovery required. Fix Nisi Inference stops at its first step until that run is resolved; its check names the manual steps.');
  // Never offered: stale feed, a Windows observer, a verified running route, a settled route, bad input.
  for (const [pipeline, components, options] of [[pendingPipeline({}), unresolved, { feedFresh: false, macOwner: true }],
    [pendingPipeline({}), unresolved, { feedFresh: true, macOwner: false }], [{ status: 'running', runId: 'r' }, unresolved, on],
    [{ status: 'idle' }, [{ id: 'nisi', state: 'ready' }], on], [null, null, on], [{ status: 'idle' }, [{ id: 'nisi', state: 'partial' }], on]])
    assert.equal(nisiRecoveryView(pipeline, components, options).needed, false);
  // Bad fields read as missing.
  const bad = nisiRecoveryView(pendingPipeline({ pendingMarkerAgeSeconds: -5, pendingMarkerOwner: 'a\u0000b' }), unresolved, on);
  assert.deepEqual([bad.line, bad.owner], ['Nisi call left a pending record', null]);
});

const GIB = 2 ** 30;
const tightBlock = (extra = {}) => ({ level: 'tight', reasons: ['only 14% of memory available', 'swap 3.2 GB (5% of RAM)'], pressure: 2, availablePercent: 14.2,
  ramBytes: 64 * GIB, swapUsedBytes: Math.round(3.2 * GIB), swapTotalBytes: 4 * GIB, compressedBytes: 9 * GIB, wiredBytes: 5 * GIB, vmFreeBytes: 200 * GIB, gpuAllocBytes: 30 * GIB,
  consumers: [{ group: 'browser', label: 'Safari', residentBytes: 6 * GIB, processCount: 31 },
    { group: 'llm-server', label: 'LM Studio', residentBytes: Math.round(21.4 * GIB), processCount: 4, gpuAllocBytes: 30 * GIB, models: ['qwen/qwen3.8-27b'] },
    { group: 'ios-simulator', label: 'iOS Simulators (iPhone 17 Pro)', residentBytes: Math.round(3.4 * GIB), processCount: 60 },
    { group: 'other', label: 'bad', residentBytes: -1, processCount: 1 }, { group: 'other', label: 'Xcode', residentBytes: 512 * 2 ** 20, processCount: 1 }],
  paused: [], suggestions: ['Unload idle model qwen/qwen3.8-27b from the monitor', 'Shut down unused iOS Simulators (`xcrun simctl shutdown all`)'],
  sampledAt: 1_000_000, ...extra });

test('Memory: sizes are approximate binary gigabytes, like the guard\'s own text', () => {
  assert.deepEqual([0, 1000, 512 * 2 ** 20, Math.round(3.4 * GIB), Math.round(21.4 * GIB)].map(memorySize), ['under 1 MB', 'under 1 MB', '512 MB', '3.4 GB', '21 GB']);
  for (const bad of [-1, 1.5, 2 ** 53, '5', null]) assert.equal(memorySize(bad), null);
});

test('Memory: the view keeps the level, the numbers, the three largest users and the suggestions; anything else reads unknown', () => {
  const view = memoryView(tightBlock(), { feedFresh: true });
  assert.equal(view.level, 'tight');
  assert.equal(view.alert, true);
  assert.equal(view.word, 'Memory tight');
  assert.equal(view.tile, 'Tight · 14% available · swap 3.2 GB');
  // Largest first by what they hold (the GPU allocation counts), three at most, bad rows dropped.
  assert.deepEqual(view.consumers.map(row => [row.label, row.text]), [
    ['LM Studio', 'about 21 GB · 4 processes · + GPU allocation about 30 GB'], ['Safari', 'about 6.0 GB · 31 processes'],
    ['iOS Simulators (iPhone 17 Pro)', 'about 3.4 GB · 60 processes']]);
  // Suggestions are text only (backticks dropped).
  assert.deepEqual(view.suggestions, ['Unload idle model qwen/qwen3.8-27b from the monitor', 'Shut down unused iOS Simulators (xcrun simctl shutdown all)']);
  assert.deepEqual(view.rows[0], ['Level', 'Tight · only 14% of memory available · swap 3.2 GB (5% of RAM)']);
  // Measured amounts carry one decimal, like the Mac GPU tile beside them (same binary gigabytes).
  assert.deepEqual(view.rows.slice(1), [['Available', '14% available'], ['Swap used', '3.2 GB'], ['Compressed', '9.0 GB'], ['GPU allocation', '30.0 GB']]);
  assert.deepEqual(memoryView(tightBlock({ paused: [{ pid: 1 }, { pid: 2 }] }), { feedFresh: true }).rows.at(-1), ['Paused jobs', '2 paused by the memory guard']);
  assert.equal(memoryView(tightBlock({ swapUsedBytes: 0 }), { feedFresh: true }).tile, 'Tight · 14% available · no swap');
  assert.equal(memoryView(tightBlock({ availablePercent: 140, swapUsedBytes: 'lots' }), { feedFresh: true }).tile, 'Tight');
  // Critical and ok.
  assert.equal(memoryView(tightBlock({ level: 'critical' }), { feedFresh: true }).word, 'Memory critical');
  const ok = memoryView(tightBlock({ level: 'ok', consumers: [], suggestions: [] }), { feedFresh: true });
  assert.deepEqual([ok.alert, ok.word, ok.label], [false, null, 'OK']);
  assert.equal(memoryView(tightBlock({ level: 'watch' }), { feedFresh: true }).alert, false);
  // Unknown: a stale feed, an unknown or invented level, no block. Nothing alarming, no numbers.
  for (const [block, fresh] of [[tightBlock(), false], [tightBlock({ level: 'unknown' }), true], [tightBlock({ level: 'panic' }), true], [null, true], [[], true]]) {
    const unknown = memoryView(block, { feedFresh: fresh });
    assert.deepEqual([unknown.known, unknown.level, unknown.alert, unknown.word, unknown.tile, unknown.consumers.length, unknown.suggestions.length],
      [false, 'unknown', false, null, 'Unknown', 0, 0]);
  }
  assert.deepEqual(memoryView(tightBlock({ level: 'unknown' }), { feedFresh: true }).rows, [['Level', 'Unknown: macOS memory pressure is unreadable']]);
  assert.deepEqual(memoryView(null, { feedFresh: false }).rows, [['Level', 'Unknown (no fresh sample)']]);
});

test('Memory: dismissing holds a level; a worse level or ok ends it, a lower one lowers it only after a minute of fresh samples', () => {
  assert.deepEqual(Object.keys(MEMORY_RANK), ['ok', 'watch', 'tight', 'critical']);
  assert.deepEqual([MEMORY_EASE_SECONDS, MEMORY_SAMPLE_GAP_SECONDS], [60, 5]);
  const run = (held, samples) => samples.reduce((h, [level, at]) => memoryDismissal(h, level, at), held);
  assert.deepEqual(memoryHoldAt('tight', 10), { level: 'tight', below: null, at: 10 });
  for (const level of ['ok', 'watch', 'unknown', ['tight'], 'toString']) assert.equal(memoryHoldAt(level, 10), null);
  assert.equal(memoryHoldAt('tight', NaN), null);
  assert.equal(memoryDismissal(null, 'critical', 1), null);
  assert.deepEqual(memoryDismissal(memoryHoldAt('tight', 0), 'tight', 1), { level: 'tight', below: null, at: 1 });
  assert.equal(memoryDismissal(memoryHoldAt('tight', 0), 'critical', 1), null, 'worse: the dismissal ends');
  assert.equal(memoryDismissal(memoryHoldAt('tight', 0), 'ok', 1), null, 'ok: a later tight is a new episode');
  // Lower: held until 60 s of consecutive fresh samples under it, then the bar drops (and a watch bar is no bar at all).
  const critical = memoryHoldAt('critical', 0);
  const ease = Array.from({ length: 61 }, (_, i) => ['tight', i + 1]);
  assert.deepEqual(run(critical, ease.slice(0, 59)), { level: 'critical', below: 1, at: 59 });
  assert.deepEqual(run(critical, ease.slice(0, 61)), { level: 'tight', below: null, at: 61 });
  assert.equal(run(critical, [...ease.slice(0, 61), ['critical', 62]]), null, 'critical, dismissed, a minute at tight, critical again: shown');
  assert.equal(run(memoryHoldAt('tight', 0), Array.from({ length: 61 }, (_, i) => ['watch', i + 1])), null);
  // A gap in the samples (stale feed, paused view) restarts the minute; so does a sample back at the held level.
  assert.deepEqual(run(critical, [['tight', 1], ['tight', 30], ['tight', 61]]), { level: 'critical', below: 61, at: 61 });
  const gapped = [...Array.from({ length: 30 }, (_, i) => ['tight', i + 1]), ...Array.from({ length: 61 }, (_, i) => ['tight', 40 + i])];
  assert.deepEqual(run(critical, gapped.slice(0, -1)), { level: 'critical', below: 40, at: 99 }, 'the minute restarts after the 10 s gap');
  assert.deepEqual(run(critical, gapped), { level: 'tight', below: null, at: 100 });
  assert.deepEqual(run(critical, [['tight', 1], ['critical', 2], ...Array.from({ length: 59 }, (_, i) => ['tight', i + 3])]), { level: 'critical', below: 3, at: 61 });
  // Unknown, a bad time and a bad level change nothing; a forged hold is no hold.
  const held = memoryHoldAt('tight', 0);
  for (const [level, at] of [['unknown', 1], ['tight', NaN], [['watch'], 1], ['__proto__', 1], ['hasOwnProperty', 1], [undefined, 1]])
    assert.equal(memoryDismissal(held, level, at), held);
  for (const forged of ['tight', { level: 'toString', below: null, at: 0 }, { level: ['tight'], below: null, at: 0 }, { level: 'tight', below: 'x', at: 0 }, { level: 'tight', at: 'x' }])
    assert.equal(memoryDismissal(forged, 'tight', 1), null);
});

test('Memory: a Mac flapping on the tight threshold neither brings a dismissed banner back nor announces it again', () => {
  // mem_guard.classify() has no hysteresis: tight and watch alternate every 2 s at the threshold (review-correctness flap).
  const flap = Array.from({ length: 30 }, (_, i) => [i % 4 < 2 ? 'tight' : 'watch', i + 1]);
  const view = level => memoryView(tightBlock({ level }), { feedFresh: true });
  // Dismissed at tight: hidden through the whole flap.
  let dismissed = memoryHoldAt('tight', 0), shown = 0;
  for (const [level, at] of flap) { dismissed = memoryDismissal(dismissed, level, at); if (memoryBannerView(view(level), { dismissed }).show) shown++; }
  assert.equal(shown, 0);
  assert.equal(dismissed.level, 'tight');
  // Not dismissed: the banner follows the level (it says what the guard says), but only its first appearance is spoken.
  let announced = null, spoken = 0;
  for (const [level, at] of flap) {
    const banner = memoryBannerView(view(level)), said = memoryAnnouncement(announced, view(level), { at, shown: banner.show });
    announced = said.announced; if (said.speak) spoken++;
  }
  assert.equal(spoken, 1);
  // A rise in rank is spoken; a stale spell is not a new appearance; ok then tight is.
  const say = (held, level, at, known = true) => memoryAnnouncement(held, known ? view(level) : memoryView(tightBlock({ level }), { feedFresh: false }), { at, shown: known && ['tight', 'critical'].includes(level) });
  let step = say(announced, 'critical', 40);
  assert.deepEqual([step.speak, step.announced.level], [true, 'critical']);
  step = say(step.announced, 'critical', 41, false);
  assert.deepEqual([step.speak, step.announced.level], [false, 'critical'], 'stale: the hold is kept');
  step = say(step.announced, 'critical', 50);
  assert.equal(step.speak, false, 'fresh again at the same level: silent');
  step = say(step.announced, 'ok', 51);
  assert.deepEqual([step.speak, step.announced], [false, null]);
  assert.equal(say(step.announced, 'tight', 52).speak, true, 'after ok, tight is a new episode');
  // Hidden (dismissed) banners are never spoken.
  assert.equal(memoryAnnouncement(null, view('tight'), { at: 1, shown: false }).speak, false);
});

test('Memory: the banner is for tight and critical only; it names the level in words, the largest user and the first suggestion', () => {
  const tight = memoryView(tightBlock(), { feedFresh: true }), critical = memoryView(tightBlock({ level: 'critical' }), { feedFresh: true });
  // LM Studio's GPU allocation (30 GB) is what ranks it first, so that is the figure the banner states.
  assert.deepEqual(memoryBannerView(tight), { show: true, level: 'tight', mark: null, title: 'Memory tight',
    text: 'LM Studio holds about 30 GB (GPU allocation) · Try: Unload idle model qwen/qwen3.8-27b from the monitor',
    full: 'LM Studio holds about 30 GB (GPU allocation) · Try: Unload idle model qwen/qwen3.8-27b from the monitor',
    announce: 'Memory tight. LM Studio holds about 30 GB (GPU allocation). Try: Unload idle model qwen/qwen3.8-27b from the monitor.' });
  assert.deepEqual([memoryBannerView(critical).mark, memoryBannerView(critical).title], ['!', 'Memory critical']);
  // Resident-led users keep "uses".
  const resident = memoryView(tightBlock({ consumers: [{ label: 'Safari', residentBytes: 6 * GIB, processCount: 31 }] }), { feedFresh: true });
  assert.equal(memoryBannerView(resident).text.split(' · ')[0], 'Safari uses about 6.0 GB');
  // Dismissed at this level: hidden; a worse level shows.
  assert.equal(memoryBannerView(tight, { dismissed: memoryHoldAt('tight', 0) }).show, false);
  assert.equal(memoryBannerView(critical, { dismissed: memoryHoldAt('tight', 0) }).show, true);
  assert.equal(memoryBannerView(tight, { dismissed: memoryHoldAt('critical', 0) }).show, false);
  assert.equal(memoryBannerView(tight, { dismissed: 'tight' }).show, true, 'a bare string is not a hold');
  // A long label is cut to 32 characters in the banner, so the size and the suggestion stay in view; the title,
  // the announcement and the Memory section keep it whole (mem_guard allows 80).
  const long = 'iOS Simulators (iPhone 17 Pro Max, iPad Pro 13-inch (M5), Apple Vision Pro 2)';
  const longView = memoryView(tightBlock({ consumers: [{ label: long, residentBytes: 25 * GIB, processCount: 118 }] }), { feedFresh: true });
  const longBanner = memoryBannerView(longView);
  assert.equal(longBanner.text, 'iOS Simulators (iPhone 17 Pro M… uses about 25 GB · Try: Unload idle model qwen/qwen3.8-27b from the monitor');
  assert.equal(longBanner.text.split(' uses ')[0].length, 32);
  assert.ok(longBanner.full.startsWith(`${long} uses about 25 GB`) && longBanner.announce.includes(long));
  assert.equal(longView.consumers[0].label, long);
  // Without measured users it falls back to the guard's first reason.
  assert.equal(memoryBannerView(memoryView(tightBlock({ consumers: [], suggestions: [] }), { feedFresh: true })).text, 'only 14% of memory available');
  for (const level of ['ok', 'watch', 'unknown']) assert.equal(memoryBannerView(memoryView(tightBlock({ level }), { feedFresh: true })).show, false, level);
  assert.equal(memoryBannerView(memoryView(tightBlock(), { feedFresh: false })).show, false, 'stale');
  assert.equal(memoryBannerView(null).show, false);
});

test('Memory: the GPU allocation ranks the users; the banner names the largest by that figure (mutant R1)', () => {
  // mem_guard's own order (LM Studio first by its 30 GB GPU allocation), then the same users in reverse.
  const gpuHeavy = [{ group: 'llm-server', label: 'LM Studio', residentBytes: 2 * GIB, processCount: 4, gpuAllocBytes: 30 * GIB },
    { group: 'browser', label: 'Safari', residentBytes: 6 * GIB, processCount: 31 }];
  for (const consumers of [gpuHeavy, [...gpuHeavy].reverse()]) {
    const view = memoryView(tightBlock({ consumers }), { feedFresh: true });
    assert.deepEqual(view.consumers.map(row => [row.label, row.weightBytes, row.gpuLed]), [['LM Studio', 30 * GIB, true], ['Safari', 6 * GIB, false]]);
    assert.match(memoryBannerView(view).text, /^LM Studio holds about 30 GB \(GPU allocation\) · /);
  }
  // The review's case: 3.2 GB resident + 38 GB GPU against 25 GB of simulators.
  const review = memoryView(tightBlock({ level: 'critical', consumers: [
    { group: 'ios-simulator', label: 'iOS Simulators', residentBytes: 25 * GIB, processCount: 60 },
    { group: 'llm-server', label: 'LM Studio', residentBytes: Math.round(3.2 * GIB), processCount: 3, gpuAllocBytes: 38 * GIB }] }), { feedFresh: true });
  assert.equal(review.consumers[0].text, 'about 3.2 GB · 3 processes · + GPU allocation about 38 GB');
  const banner = memoryBannerView(review);
  assert.match(banner.text, /^LM Studio holds about 38 GB \(GPU allocation\) · Try: /);
  assert.match(banner.announce, /^Memory critical\. LM Studio holds about 38 GB \(GPU allocation\)\. /);
  assert.doesNotMatch(banner.text + banner.announce, /uses about 3\.2 GB/);
  // A GPU allocation below the resident size does not lead.
  const small = memoryView(tightBlock({ consumers: [{ label: 'LM Studio', residentBytes: 21 * GIB, processCount: 4, gpuAllocBytes: 5 * GIB }] }), { feedFresh: true });
  assert.equal(memoryBannerView(small).text.split(' · ')[0], 'LM Studio uses about 21 GB');
  // Tiny users read "under 1 MB", never "about 0 MB"; a long unsorted list is sorted over a bounded prefix.
  const tiny = memoryView(tightBlock({ consumers: [{ label: 'helper', residentBytes: 1000, processCount: 1 }] }), { feedFresh: true });
  assert.equal(tiny.consumers[0].text, 'under 1 MB · 1 process');
  const many = [...Array.from({ length: 200 }, (_, i) => ({ label: `proc${i}`, residentBytes: 1000 + i, processCount: 1 })), { label: 'Giant', residentBytes: 50 * GIB, processCount: 1 }];
  assert.equal(memoryView(tightBlock({ consumers: many }), { feedFresh: true }).consumers[0].label, 'Giant');
});

test('Memory: the level is a closed reading; inherited keys, arrays and other values are unknown (mutant R4)', () => {
  for (const level of ['toString', 'hasOwnProperty', '__proto__', 'constructor', 'valueOf', ['tight'], ['critical'], 2, null, { toString: () => 'tight' }]) {
    const view = memoryView(tightBlock({ level }), { feedFresh: true });
    assert.deepEqual([view.known, view.level, view.label, view.alert, view.word, view.tile], [false, 'unknown', 'Unknown', false, null, 'Unknown'], String(level));
    assert.equal(memoryBannerView(view).show, false);
    assert.doesNotMatch(JSON.stringify(view), /native code|function/);
  }
  // A forged view (not from memoryView) cannot raise the banner either.
  assert.equal(memoryBannerView({ alert: true, level: ['critical'], word: 'Memory critical', consumers: [], suggestions: [], reasons: [] }).show, false);
  assert.equal(memoryBannerView({ alert: true, level: 'toString', word: 'x', consumers: [], suggestions: [], reasons: [] }).show, false);
  // The chip adds no word for them.
  const base = { fresh: true, models: [{ state: 'idle', loaded: true }], activityKnown: true, loadedKnown: true, feed: [] };
  assert.deepEqual(vitalsView({ ...base, memory: memoryView(tightBlock({ level: ['critical'] }), { feedFresh: true }) }).mac, { tone: 'ok', detail: '1 loaded · idle' });
});

test('Memory: the This Mac chip adds a short memory word only when tight or critical; the route chip names a pending Nisi record', () => {
  const base = { fresh: true, models: [{ state: 'idle', loaded: true }], activityKnown: true, loadedKnown: true, feed: [] };
  const tight = memoryView(tightBlock(), { feedFresh: true }), critical = memoryView(tightBlock({ level: 'critical' }), { feedFresh: true });
  assert.deepEqual(vitalsView({ ...base, memory: tight }).mac, { tone: 'warn', detail: '1 loaded · idle · Memory tight', short: 'Memory tight · Idle' });
  assert.equal(vitalsView({ ...base, memory: tight }).tiny.mac, 'Memory tight · Idle');
  // The micro form (a 360-480 px popover) keeps only the memory words; without them there is no micro form.
  assert.deepEqual(vitalsView({ ...base, memory: critical }).micro, { mac: 'Memory critical' });
  assert.deepEqual(vitalsView({ ...base, memory: memoryView(tightBlock({ level: 'watch' }), { feedFresh: true }) }).micro, {});
  assert.deepEqual(vitalsView({ ...base, fresh: false, memory: tight }).micro, {});
  // A generating model keeps the live tone; the GPU stays in the short form, after the memory word.
  assert.deepEqual(vitalsView({ ...base, models: [{ state: 'generating', loaded: true }], gpu: { known: true, chip: 'GPU 92%' }, memory: critical }).mac,
    { tone: 'live', detail: '1 generating · GPU 92% · Memory critical', short: 'Memory critical · 1 generating · GPU 92%' });
  // Ok, watch, unknown and a stale feed add nothing.
  for (const memory of [memoryView(tightBlock({ level: 'ok' }), { feedFresh: true }), memoryView(tightBlock({ level: 'watch' }), { feedFresh: true }), memoryView(null), null])
    assert.deepEqual(vitalsView({ ...base, memory }).mac, { tone: 'ok', detail: '1 loaded · idle' });
  assert.deepEqual(vitalsView({ ...base, fresh: false, memory: tight }).mac, { tone: 'muted', detail: 'Signal stale' });
  // Route chip.
  assert.deepEqual(vitalsView({ ...base, pipeline: { status: 'recovery-required' } }).route, { tone: 'warn', detail: 'Nisi record pending' });
  assert.deepEqual(vitalsView({ ...base, pipeline: { status: 'idle' }, nisi: { state: 'unresolved' } }).route, { tone: 'warn', detail: 'Nisi record pending' });
  assert.equal(vitalsView({ ...base, pipeline: { status: 'running', stage: 'backend_draft' }, nisi: { state: 'unresolved' } }).route.detail, 'Running · backend draft');
  assert.equal(vitalsView({ ...base, pipeline: { status: 'recovery-required', runId: 'r1' } }).route.detail, 'Unsettled run recorded');
  // A queued route (converge, Sol #8) is its own chip, never an unsettled run.
  // p2-readers converge (Sol terminology): "for a lane" only when the run's note names the lane it waits for.
  assert.deepEqual(vitalsView({ ...base, pipeline: { status: 'queued', runId: 'run-q', queuePhase: 'waiting', queueResource: 'mac-pair' },
    nisi: { state: 'unresolved' } }).route, { tone: 'live', detail: 'Queued for a lane' });
  assert.deepEqual(vitalsView({ ...base, pipeline: { status: 'queued', runId: 'run-q' }, nisi: { state: 'unresolved' } }).route,
    { tone: 'live', detail: 'Queued' });
  assert.equal(vitalsView({ ...base, fresh: false, pipeline: { status: 'recovery-required' } }).route.detail, 'Route age unknown');
});
