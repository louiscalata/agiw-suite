import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import * as layoutModule from './web/map-layout.mjs';
import { fitGraph, constellationLayout, prioritizeRuntimeModels, hasAdvertisedWindowsWorker, afmView, parseParameterCount, modelStarSize, windowsJobsView, windowsLaneView, resolveLabelCollisions, clampCamera, nextNodeInDirection, reviewConsistencyView, headlessSwitchView, activityFeed, vitalsView, windowsLaneRows, nodeCaptionLayout,
  macGpuView, localCallersView, speedsText, laneSpeeds, jobFlags, activitySummary, pcGpuView } from './web/map-layout.mjs';

test('Core 6 is an exact Mac presentation filter; All keeps the complete discovered inventory', () => {
  const expected = [
    'openai/gpt-oss-20b', 'qwen/qwen3.8-27b', 'google/gemma-4-26b-a4b-qat',
    'qwen/qwen3.6-35b-a3b', 'google/gemma-3-4b', 'text-embedding-nomic-embed-text-v1.5',
  ];
  assert.deepEqual(layoutModule.CORE_MODEL_IDS, expected);
  const rows = [...expected.map(id => ({ id, host: 'mac', state: 'unloaded', loaded: false })),
    ...['gemma-4-26b-tuned', 'gemma-4-26b-a4b-mtp-mlx', 'text-embedding-nomic-embed-text-v2-moe']
      .map(id => ({ id, host: 'mac', state: 'unloaded', loaded: false })),
    { id: 'pc-model', host: 'windows', state: 'unloaded', loaded: false }];
  const before = JSON.stringify(rows);
  const core = layoutModule.runtimeModelRoster(rows);
  assert.deepEqual(core.visible.map(row => row.id), [...expected, 'pc-model']);
  assert.deepEqual(core.hidden.map(row => row.id), rows.slice(6, 9).map(row => row.id));
  assert.deepEqual([core.coreDiscovered, core.macDiscovered, core.operationalExtras.length], [6, 9, 0]);
  assert.deepEqual(layoutModule.runtimeModelRoster(rows, { scope: 'all' }).visible, rows);
  assert.equal(JSON.stringify(rows), before, 'the complete source inventory is unchanged');
});

test('the proposed six-entry catalog shows all six in either scope', () => {
  const rows = layoutModule.CORE_MODEL_IDS.map(id => ({ id, host: 'mac', state: 'unloaded', loaded: false }));
  const core = layoutModule.runtimeModelRoster(rows);
  const all = layoutModule.runtimeModelRoster(rows, { scope: 'all' });
  assert.deepEqual(core.visible, rows);
  assert.deepEqual(all.visible, rows);
  assert.deepEqual(core.hidden, []);
  assert.equal(core.coreDiscovered, 6);
  assert.equal(core.macDiscovered, 6);
});

test('Core 6 retains exact non-core Mac rows with observed work or load, including a separate CLI alias', () => {
  const extra = [
    { id: 'gemma-4-26b-tuned', host: 'mac', state: 'idle', loaded: true },
    { id: 'gemma-4-26b-a4b-mtp-mlx', host: 'mac', state: 'busy', loaded: false },
    { id: 'text-embedding-nomic-embed-text-v2-moe', host: 'mac', state: 'idle', loaded: false, queued: 1 },
    { id: 'loaded-alias', modelKey: 'qwen/qwen3.8-27b', host: 'mac', state: 'idle', loaded: true },
    { id: 'idle-alias', modelKey: 'qwen/qwen3.8-27b', host: 'mac', state: 'unloaded', loaded: false },
    { id: 'uncertain-queue', host: 'mac', state: 'idle', loaded: false, queued: '1' },
  ];
  const roster = layoutModule.runtimeModelRoster(extra);
  assert.deepEqual(roster.visible.map(row => row.id), extra.slice(0, 4).map(row => row.id));
  assert.deepEqual(roster.hidden.map(row => row.id), ['idle-alias', 'uncertain-queue']);
  assert.equal(roster.coreDiscovered, 0, 'a modelKey alias is not a discovered core ID');
});

test('stale non-core observations stay inspectable without being painted live', () => {
  const raw = { id: 'gemma-4-26b-tuned', host: 'mac', state: 'busy', loaded: true, queued: 2 };
  const aged = { ...raw, state: 'stale', loaded: null, queued: null };
  const roster = layoutModule.runtimeModelRoster([aged], { observedRows: [raw] });
  assert.deepEqual(roster.visible, [aged]);
  assert.deepEqual([roster.visible[0].state, roster.visible[0].loaded, roster.visible[0].queued], ['stale', null, null]);
  assert.equal(layoutModule.runtimeModelRoster([aged], { observedRows: [] }).visible.length, 0);
});

function pointsFor(layout) {
  return [
    layout.runtime,
    ...layout.runtimeModels,
    layout.pipeline,
    layout.jev,
    layout.afm,
    ...layout.clientLanes.flatMap(lane => [{ x: lane.x, y: lane.y }, ...lane.models]),
    layout.windows,
    ...layout.windowsLanes,
    layout.windowsLabel,
    ...layout.labels.map(label=>({x:label.x,y:label.y})),
  ];
}

test('constellation keeps local models in an ordered cluster connected to the Mac hub', () => {
  const wide = constellationLayout(9, [{ modelCount: 3 }, { modelCount: 2 }, { modelCount: 1 }], { viewportWidth: 1262, viewportHeight: 1191 });
  const stacked = constellationLayout(9, [{ modelCount: 3 }, { modelCount: 2 }, { modelCount: 1 }], { viewportWidth: 639, viewportHeight: 1200 });
  assert.equal(wide.runtimeModels.length, 9);
  assert.equal(wide.clientLanes.length, 3);
  assert.deepEqual(wide.runtime,{x:736,y:595.5});
  assert.ok(wide.runtimeModels.every(node=>node.x<wide.runtime.x));
  const modelXs=[...new Set(wide.runtimeModels.map(node=>node.x))].sort((a,b)=>a-b);
  const modelYs=[...new Set(wide.runtimeModels.map(node=>node.y))].sort((a,b)=>a-b);
  assert.equal(modelXs.length,3);
  assert.equal(modelYs.length,3);
  assert.ok(modelXs[1]-modelXs[0]>=70&&modelYs[1]-modelYs[0]>=70);
  assert.ok(wide.clientLanes.every(lane=>lane.x>wide.runtime.x));
  assert.ok(wide.pipeline.y<wide.runtime.y);
  assert.deepEqual(wide.labels.map(label=>label.kind),['local','mac','clients','pipeline']);
  assert.ok(stacked.runtimeModels.every(node=>node.y>stacked.runtime.y));
  assert.equal(stacked.labels.some(label=>label.kind==='mac'),true);
  assert.ok(stacked.clientLanes.every(lane=>lane.y>stacked.runtime.y));
});

test('whole map fits on tall 639px compact and 820x660 wide windows', () => {
  const clients = Array.from({length: 5}, () => ({modelCount: 3}));
  const compactLayout = constellationLayout(9, clients, { viewportWidth: 639, viewportHeight: 1200 });
  const wideLayout = constellationLayout(9, clients, { viewportWidth: 820, viewportHeight: 660 });
  const compact = fitGraph(pointsFor(compactLayout), { left: 16, top: 86, width: 607, height: 1030 });
  const wide = fitGraph(pointsFor(wideLayout), { left: 16, top: 86, width: 788, height: 504 },{horizontalPadding:90,verticalPadding:46,maxScale:1.7});
  assert.ok(compact.scale >= 0.9, `639x1300 effective map scale was ${compact.scale}`);
  assert.ok(wide.scale >= 0.7, `820x660 effective map scale was ${wide.scale}`);
  for (const [fit, area] of [[compact, { left: 16, top: 86, width: 607, height: 1030 }], [wide, { left: 16, top: 86, width: 788, height: 504 }]]) {
    for (const point of pointsFor(fit === compact ? compactLayout : wideLayout)) {
      const x = fit.x + point.x * fit.scale;
      const y = fit.y + point.y * fit.scale;
      assert.ok(x >= area.left && x <= area.left + area.width, `x ${x} escaped fitted map`);
      assert.ok(y >= area.top && y <= area.top + area.height, `y ${y} escaped fitted map`);
    }
  }
});

test('short compact window separates the local grid from five client branches', () => {
  const layout = constellationLayout(9, Array.from({length:5}, () => ({modelCount:3})), {viewportWidth:690,viewportHeight:560});
  assert.equal(layout.portrait,false);
  const clientPoints=layout.clientLanes.flatMap(lane=>[{x:lane.x,y:lane.y},...lane.models]);
  const minimum=Math.min(...layout.runtimeModels.flatMap(model=>clientPoints.map(client=>Math.hypot(model.x-client.x,model.y-client.y))));
  assert.ok(minimum>=100, `local/client clearance was only ${minimum}px`);
  const fit=fitGraph(pointsFor(layout),{left:16,top:74,width:658,height:416},{horizontalPadding:90,verticalPadding:46,maxScale:1.7});
  assert.ok(fit.scale>=0.65, `short compact map shrank to ${fit.scale}`);
});

test('host cluster label follows the snapshot host', () => {
  const clients=[{modelCount:1}];
  assert.equal(constellationLayout(1,clients,{host:'mac'}).labels.find(label=>label.kind==='mac').text,'MAC');
  assert.equal(constellationLayout(1,clients,{host:'windows'}).labels.find(label=>label.kind==='mac').text,'PC');
});

test('AFM stays outside the six-model grid and reports metadata without live inference', () => {
  const map = constellationLayout(6, [{ modelCount: 1 }], { viewportWidth: 820, viewportHeight: 660 });
  assert.equal(map.runtimeModels.length, 6);
  assert.ok(Number.isFinite(map.afm.x) && Number.isFinite(map.afm.y));
  const status = { schemaVersion: 1, host: 'mac', state: 'executable', callability: 'permission-granted', inference: 'NOT_TESTED' };
  assert.deepEqual(afmView(status, { feedFresh: true, host: 'mac' }), {
    visible: true, state: 'executable', label: 'Adapter executable', tone: 'ok', inference: 'Not tested by passive feed',
  });
  assert.equal(afmView(status, { feedFresh: false, host: 'mac' }).state, 'unknown');
  assert.equal(afmView(status, { feedFresh: true, host: 'windows' }).visible, false);
  assert.equal(afmView({ ...status, state: 'generating' }, { feedFresh: true }).state, 'unknown');
});

test('Jev stays attached to Nisi Inference, clear of AFM and the Mac hub in compact and wide layouts', () => {
  for (const [width, height] of [[1150, 690], [820, 604], [560, 900]]) {
    const map = constellationLayout(6, Array.from({ length: 5 }, () => ({ modelCount: 1 })), { viewportWidth: width, viewportHeight: height });
    const separation = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
    assert.ok(separation(map.jev, map.pipeline) < 100, `${width}x${height}: Jev detached from route`);
    assert.ok(separation(map.jev, map.afm) > 75, `${width}x${height}: Jev overlaps AFM`);
    assert.ok(separation(map.jev, map.runtime) > 75, `${width}x${height}: Jev overlaps Mac hub`);
  }
  assert.deepEqual(layoutModule.jevView({ state: 'configured', detail: 'Opted in', lastJudgedAgeSeconds: 120 },
    { feedFresh: true, snapshotAge: 5 }), { state: 'configured', detail: 'Opted in', lastJudgedAgeSeconds: 125 });
  assert.equal(layoutModule.jevView({ state: 'configured', lastJudgedAgeSeconds: 120 }, { feedFresh: false }).state, 'unknown');
  assert.equal(layoutModule.jevView({ state: 'configured', lastJudgedAgeSeconds: -1 }, { feedFresh: true }).lastJudgedAgeSeconds, null);
  assert.equal(layoutModule.jevView({ state: 'generating', lastJudgedAgeSeconds: 0 }, { feedFresh: true }).state, 'unknown');
});

test('all installed rows remain individual nodes and active rows sort first', () => {
  const rows = [
    ...Array.from({ length: 20 }, (_, i) => ({ id: `idle-${i}`, state: 'idle', loaded: false })),
    { id: 'active-a', state: 'generating', loaded: true },
    { id: 'busy-b', state: 'busy', loaded: true },
  ];
  const result = prioritizeRuntimeModels(rows);
  assert.equal(result.visible.length, rows.length);
  assert.equal(result.overflow.length, 0);
  assert.deepEqual(result.visible.slice(0, 2).map(row => row.id), ['active-a', 'busy-b']);
});

test('Windows host node requires a fresh advertised worker heartbeat within 60 seconds', () => {
  const advertised={state:'advertised',ageSeconds:18,modelsAdvertised:['local/model-a','local/model-b'],detail:'Heartbeat inventory only'};
  assert.equal(hasAdvertisedWindowsWorker(advertised,{feedFresh:true,snapshotAge:2}),true);
  assert.equal(hasAdvertisedWindowsWorker(advertised,{feedFresh:false,snapshotAge:0}),false);
  assert.equal(hasAdvertisedWindowsWorker({...advertised,state:'unavailable'},{feedFresh:true}),false);
  assert.equal(hasAdvertisedWindowsWorker({...advertised,ageSeconds:59},{feedFresh:true,snapshotAge:2}),false);
  assert.equal(hasAdvertisedWindowsWorker({...advertised,ageSeconds:null},{feedFresh:true}),false);
  assert.equal(hasAdvertisedWindowsWorker({state:'live',ageSeconds:0},{feedFresh:true}),false);
});

test('PC chip and lanes stop claiming connection after the 60-second heartbeat boundary', () => {
  for (const age of [60, 61, 300]) {
    const worker = laneWorker({ ageSeconds: age });
    const pcFresh = hasAdvertisedWindowsWorker(worker, { feedFresh: true });
    const lanes = windowsLaneView(worker, null, { feedFresh: pcFresh });
    const chip = vitalsView({ fresh: true, pcFresh, worker, lanes, jobs: { running: false }, feed: [] }).pc;
    assert.equal(pcFresh, age === 60);
    assert.equal(lanes.visible, age === 60);
    assert.equal(chip.tone, age === 60 ? 'ok' : 'muted');
    if (age > 60) assert.match(chip.detail, /heartbeat stale or unknown/i);
  }
});

test('model node sizes use a bounded logarithmic count, then a labeled byte proxy',()=>{
  assert.equal(parseParameterCount('4B'),4_000_000_000);
  assert.equal(parseParameterCount('27B'),27_000_000_000);
  assert.equal(parseParameterCount('370M'),370_000_000);
  assert.equal(parseParameterCount('8x277M'),null);
  assert.equal(parseParameterCount('Infinity'),null);
  const small=modelStarSize({metadata:{parameters:'370M'}}),large=modelStarSize({metadata:{parameters:'27B'}});
  assert.ok(small.radius>=10&&small.radius<=22);
  assert.ok(large.radius>small.radius+4);
  assert.equal(large.basis,'parameter count');
  const proxy=modelStarSize({metadata:{parameters:'8x277M'},sizeBytes:12_000_000_000});
  assert.equal(proxy.basis,'installed size proxy');
  assert.match(proxy.value,/proxy/);
  assert.equal(modelStarSize({}).basis,'unknown');
});

test('primary hues encode type or positive capabilities without encoding activity',()=>{
  const embedding=modelStarSize({metadata:{type:'embedding',capabilities:{vision:true,toolUse:true,reasoning:false}}});
  assert.equal(embedding.color,'#bd9cff');
  assert.match(embedding.capabilityLabel,/embedding · vision · tool use/);
  assert.equal(modelStarSize({metadata:{capabilities:{reasoning:true}}}).color,'#70d7ed');
  assert.equal(modelStarSize({metadata:{capabilities:{vision:true}}}).color,'#f1b568');
  assert.equal(modelStarSize({metadata:{capabilities:{toolUse:true}}}).color,'#6bd6a2');
  assert.equal(modelStarSize({}).color,'#99a3b7');
});

test('fit uses actual graph extents and handles empty or zero-sized areas', () => {
  const nodes = [{ x: 100, y: 100 }, { x: 700, y: 480 }];
  const fit = fitGraph(nodes, { left: 0, top: 0, width: 400, height: 400 });
  assert.ok(fit.scale > 0.4);
  assert.equal(fitGraph([], { left: 0, top: 0, width: 400, height: 400 }), null);
  assert.equal(fitGraph(nodes, { left: 0, top: 0, width: 0, height: 400 }), null);
});

test('node labels stay quiet until hover, keyboard focus, or selection', () => {
  const css = readFileSync(new URL('./web/style.css', import.meta.url), 'utf8');
  assert.match(css, /\.node \.label,\.node \.sub\{opacity:0/);
  assert.match(css, /\.node:hover \.label/);
  assert.match(css, /\.node:focus-visible \.label/);
  assert.match(css, /\.node\.selected \.label/);
});

test('windowsJobsView ages jobs, stops animating stale feeds and expired jobs', () => {
  const jobs = { schemaVersion: 1, inFlight: [
    { id: 'a', model: 'm', ageSeconds: 10, timeoutSeconds: 60 },
    { id: 'b', model: 'm', ageSeconds: 89, timeoutSeconds: 60 },
  ], recent: [{ id: 'c', state: 'success', ageSeconds: 5 }, { id: 'd', ageSeconds: 5 }],
  lastSuccess: { id: 'c', model: 'm', elapsedSeconds: 4, ageSeconds: 5 } };
  const live = windowsJobsView(jobs, { feedFresh: true, snapshotAge: 2 });
  assert.equal(live.running, true);
  assert.deepEqual(live.inFlight.map(row => [row.id, row.ageSeconds]), [['a', 12]]);
  assert.equal(windowsJobsView(jobs, { feedFresh: true, snapshotAge: 10 }).inFlight.length, 1);
  assert.equal(windowsJobsView(jobs, { feedFresh: false }).running, false);
  assert.deepEqual(live.recent.map(row => row.id), ['c']);
  assert.equal(live.lastVerified.ageSeconds, 7);
});

test('windowsJobsView treats missing, foreign or malformed feeds as empty', () => {
  for (const value of [undefined, null, {}, { schemaVersion: 2, inFlight: [{ ageSeconds: 1, timeoutSeconds: 1 }] },
    { schemaVersion: 1, inFlight: 'x', recent: {}, lastSuccess: { model: 3 } }]) {
    const view = windowsJobsView(value, { feedFresh: true });
    assert.equal(view.running, false);
    assert.deepEqual(view.inFlight, []);
    assert.equal(view.lastVerified, null);
  }
});

test('windowsJobsView marks a success current only when fresh, recent and not superseded', () => {
  const ok = { id: 'ok', state: 'success', model: 'm', elapsedSeconds: 4, ageSeconds: 60 };
  const base = { schemaVersion: 1, inFlight: [], recent: [ok], lastSuccess: ok };
  assert.equal(windowsJobsView(base, { feedFresh: true }).current, true);
  assert.equal(windowsJobsView(base, { feedFresh: false }).current, false);
  const old = { ...ok, ageSeconds: 3 * 86400 };
  assert.equal(windowsJobsView({ ...base, recent: [], lastSuccess: old }, { feedFresh: true }).current, false);
  const failedLater = { id: 'e', state: 'unresolved', ageSeconds: 10 };
  const view = windowsJobsView({ ...base, recent: [failedLater, ok] }, { feedFresh: true });
  assert.equal(view.current, false);
  assert.equal(view.lastVerified.id, 'ok');
});

const laneWorker = (extra = {}) => ({ state: 'advertised', ageSeconds: 4,
  lanes: { amd: { up: true }, fast: { up: true, model: 'gpt-oss-20b', kind: 'gpt-oss', slotsBusy: 1, slotsTotal: 2 },
    deep: { up: false, model: 'Qwen3.8-27B', kind: 'qwen', slotsBusy: null, slotsTotal: null } },
  headless: { state: 'on', reason: null, expiresInSeconds: 3 * 3600 + 25 * 60 + 40, grantedBy: 'inference-monitor' }, ...extra });

test('windowsLaneView is visible only for a fresh feed with a lanes object', () => {
  assert.equal(windowsLaneView(laneWorker(), null, { feedFresh: true }).visible, true);
  assert.equal(windowsLaneView(laneWorker(), null, { feedFresh: false }).visible, false);
  for (const lanes of [null, undefined, [], 'fast', 3]) {
    const view = windowsLaneView(laneWorker({ lanes }), { inFlight: [{ client: 'claude', lane: 'fast' }], recent: [] }, { feedFresh: true });
    assert.equal(view.visible, false);
    assert.deepEqual(view.lanes, []);
    assert.deepEqual(view.edges, []);
  }
  assert.equal(windowsLaneView(null, null, { feedFresh: true }).visible, false);
});

test('windowsLaneView maps fast and deep lanes and rejects malformed ones', () => {
  const view = windowsLaneView(laneWorker(), null, { feedFresh: true });
  assert.deepEqual(view.lanes, [
    { id: 'fast', label: 'fast · gpt-oss-20b', up: true, busy: 1, total: 2, live: false, rate: null, rateAgeSeconds: null },
    { id: 'deep', label: 'deep · Qwen3.8-27B', up: false, busy: null, total: null, live: false, rate: null, rateAgeSeconds: null },
  ]);
  const bad = windowsLaneView(laneWorker({ lanes: { fast: { up: 'yes', model: 'x' },
    deep: { up: true, model: 'Qwen3.8-27B', slotsBusy: 3, slotsTotal: 2 } } }), null, { feedFresh: true });
  assert.deepEqual(bad.lanes, [{ id: 'deep', label: 'deep · Qwen3.8-27B', up: true, busy: null, total: null, live: false, rate: null, rateAgeSeconds: null }]);
  assert.equal(windowsLaneView(laneWorker({ lanes: { fast: { up: true, model: 'openai/gpt-oss-20b' } } }), null, { feedFresh: true }).lanes[0].label, 'fast · gpt-oss-20b');
  assert.deepEqual(windowsLaneView(laneWorker({ lanes: { fast: { up: true, model: 'y', slotsBusy: -1, slotsTotal: 1.5 } } }), null, { feedFresh: true }).lanes,
    [{ id: 'fast', label: 'fast · y', up: true, busy: null, total: null, live: false, rate: null, rateAgeSeconds: null }]);
});

test('windowsLaneView draws one edge per client and lane, preferring a live job', () => {
  const jobs = {
    inFlight: [{ client: 'Claude', lane: 'fast' }, { client: 'codex', lane: 'deep' }, { client: 'claude', lane: 'fast' }],
    recent: [{ client: 'claude', lane: 'fast' }, { client: 'opencode', lane: 'deep' }, { client: 'opencode', lane: 'deep' },
      { client: null, lane: 'fast' }, { client: 'cursor', lane: null }, { client: 'bad client!', lane: 'fast' },
      { client: 'grok', lane: 'amd' }, { client: '-x', lane: 'fast' }, { client: 'a'.repeat(41), lane: 'deep' }],
  };
  const view = windowsLaneView(laneWorker(), jobs, { feedFresh: true });
  assert.deepEqual(view.edges, [
    { client: 'claude', lane: 'fast', live: true },
    { client: 'codex', lane: 'deep', live: true },
    { client: 'opencode', lane: 'deep', live: false },
  ]);
  const recentFirst = windowsLaneView(laneWorker(), { inFlight: [{ client: 'codex', lane: 'fast' }], recent: [{ client: 'codex', lane: 'fast' }] }, { feedFresh: true });
  assert.deepEqual(recentFirst.edges, [{ client: 'codex', lane: 'fast', live: true }]);
  const onlyFast = windowsLaneView(laneWorker({ lanes: { fast: laneWorker().lanes.fast } }), jobs, { feedFresh: true });
  assert.deepEqual(onlyFast.edges, [{ client: 'claude', lane: 'fast', live: true }]);
});

test('windowsLaneView headless label counts down and never claims on from stale or expired state', () => {
  assert.deepEqual(windowsLaneView(laneWorker(), null, { feedFresh: true }).headless, { on: true, label: 'Headless on · 3h 25m left' });
  assert.deepEqual(windowsLaneView(laneWorker(), null, { feedFresh: true, snapshotAge: 30 * 60 }).headless, { on: true, label: 'Headless on · 2h 55m left' });
  assert.deepEqual(windowsLaneView(laneWorker({ headless: { state: 'on', expiresInSeconds: 20 } }), null, { feedFresh: true, snapshotAge: 25 }).headless, { on: false, label: 'Headless off' });
  assert.deepEqual(windowsLaneView(laneWorker({ headless: { state: 'on', expiresInSeconds: null } }), null, { feedFresh: true }).headless, { on: false, label: 'Headless unknown' });
  assert.deepEqual(windowsLaneView(laneWorker({ headless: { state: 'on', expiresInSeconds: 12.5 } }), null, { feedFresh: true }).headless, { on: false, label: 'Headless unknown' });
  assert.deepEqual(windowsLaneView(laneWorker({ headless: { state: 'off', reason: 'expired' } }), null, { feedFresh: true }).headless, { on: false, label: 'Headless off' });
  assert.deepEqual(windowsLaneView(laneWorker(), null, { feedFresh: false }).headless, { on: false, label: 'Headless unknown' });
  assert.deepEqual(windowsLaneView(laneWorker({ lanes: null, headless: { state: 'on', expiresInSeconds: 59 } }), null, { feedFresh: true }).headless, { on: true, label: 'Headless on · 0h 0m left' });
  assert.deepEqual(windowsLaneView({ state: 'unknown' }, null, { feedFresh: true }).headless, { on: false, label: 'Headless unknown' });
});

test('app wires Windows lanes, the headless toggle and its endpoint through the shared action guard', () => {
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  const html = readFileSync(new URL('./web/index.html', import.meta.url), 'utf8');
  assert.match(html, /<button id="pcHeadless" class="mode-repair-button" type="button" hidden>/);
  assert.match(app, /const HEADLESS_ACTIONS=new Map\(\[\['headless-on','on'\],\['headless-off','off'\]\]\)/);
  assert.match(app, /headless\?'\/api\/online-code-mode\/headless'/);
  assert.match(app, /if\(headless\)body\.action=HEADLESS_ACTIONS\.get\(action\)/);
  assert.match(app, /headlessButton\.hidden=!macOwner/);
  assert.match(app, /headlessButton\.disabled=actionActive\|\|!fresh\(\)/);
  assert.match(app, /\$\('pcHeadless'\)\.addEventListener\('click',\(\)=>requestOnlineCodeAction\(headlessAction\(\)\)\)/);
  assert.match(app, /edge\(`client:\$\{e\.client\}`,`windows-lane:\$\{e\.lane\}`/);
});

test('windowsLaneView lights a lane for any fresh in-flight job and hides lanes without a fresh heartbeat', () => {
  const view = windowsLaneView(laneWorker(), { inFlight: [{ client: null, lane: 'deep' }], recent: [] }, { feedFresh: true });
  assert.deepEqual(view.lanes.map(lane => [lane.id, lane.live]), [['fast', false], ['deep', true]]);
  assert.deepEqual(view.edges, []);
  assert.equal(windowsLaneView(laneWorker({ state: 'unknown' }), null, { feedFresh: true }).visible, false);
  assert.equal(windowsLaneView(laneWorker({ ageSeconds: 290 }), null, { feedFresh: true, snapshotAge: 20 }).visible, false);
  assert.equal(windowsLaneView(laneWorker({ state: 'degraded' }), null, { feedFresh: true }).visible, true);
});

test('the Windows PC is its own constellation, clear of the local grid and client branches', () => {
  const clients = Array.from({length: 5}, () => ({modelCount: 3}));
  for (const [w, h] of [[1262, 800], [820, 660], [690, 560], [639, 1200], [400, 800]]) {
    const layout = constellationLayout(9, clients, {viewportWidth: w, viewportHeight: h});
    assert.equal(layout.windowsLanes.length, 2);
    const pc = [layout.windows, ...layout.windowsLanes];
    const others = [...layout.runtimeModels, ...layout.clientLanes.flatMap(lane => [{x: lane.x, y: lane.y}, ...lane.models]), layout.pipeline, layout.runtime];
    const clearance = Math.min(...pc.flatMap(a => others.map(b => Math.hypot(a.x - b.x, a.y - b.y))));
    assert.ok(clearance >= 60, `${w}x${h}: PC clearance only ${clearance.toFixed(1)}px`);
    if (!layout.portrait) {
      assert.ok(layout.windows.y > layout.runtime.y, `${w}x${h}: PC should sit below the Mac hub`);
      assert.ok(layout.windowsLanes.every(lane => lane.y > layout.windows.y));
    }
    assert.ok(layout.windowsLabel.y < layout.windows.y);
  }
});

test('the CLIENTS label is never clamped onto the first client node', () => {
  const layout = constellationLayout(9, Array.from({length: 5}, () => ({modelCount: 1})), {viewportWidth: 800, viewportHeight: 490});
  const label = layout.labels.find(item => item.kind === 'clients');
  assert.equal(label.y, layout.clientLanes[0].y - 42);
});

test('label collisions move labels off nodes and earlier labels, and never far', () => {
  const node = {left: 90, right: 110, top: 90, bottom: 110};
  const [moved, second] = resolveLabelCollisions([
    {x: 100, y: 105, width: 60, height: 12, align: 'middle'},
    {x: 100, y: 105, width: 60, height: 12, align: 'middle'},
  ], [node]);
  assert.ok(moved.y <= 90 || moved.y - 12 >= 110, `first label still overlaps at ${moved.y}`);
  assert.notEqual(second.y, moved.y);
  const clear = resolveLabelCollisions([{x: 400, y: 400, width: 40, height: 10}], [node]);
  assert.equal(clear[0].y, 400);
  const walled = resolveLabelCollisions([{x: 0, y: 0, width: 10, height: 10}], [{left: -500, right: 500, top: -500, bottom: 500}], {maxShift: 20});
  assert.equal(walled[0].y, 0);
});

test('camera clamping keeps part of the graph in view and leaves an in-view camera alone', () => {
  const bounds = {minX: 0, minY: 0, maxX: 400, maxY: 300}, area = {left: 0, top: 0, right: 800, bottom: 600};
  assert.deepEqual(clampCamera({x: 100, y: 100, z: 1}, bounds, area), {x: 100, y: 100, z: 1});
  const far = clampCamera({x: -5000, y: 9000, z: 1}, bounds, area);
  assert.equal(far.x + 400, 80);
  assert.equal(far.y, 520);
  assert.deepEqual(clampCamera({x: NaN, y: 0, z: 1}, bounds, area), {x: NaN, y: 0, z: 1});
  assert.deepEqual(clampCamera({x: 1, y: 2, z: 1}, null, area), {x: 1, y: 2, z: 1});
});

test('arrow keys move to the nearest node in a 60-degree cone', () => {
  const nodes = [{id: 'c', x: 0, y: 0}, {id: 'r', x: 100, y: 10}, {id: 'far', x: 300, y: 0}, {id: 'u', x: 5, y: -80}, {id: 'diag', x: 60, y: 80}];
  assert.equal(nextNodeInDirection(nodes, 'c', 'ArrowRight'), 'r');
  assert.equal(nextNodeInDirection(nodes, 'c', 'ArrowUp'), 'u');
  assert.equal(nextNodeInDirection(nodes, 'c', 'ArrowDown'), 'diag');
  assert.equal(nextNodeInDirection(nodes, 'c', 'ArrowLeft'), null);
  assert.equal(nextNodeInDirection(nodes, 'missing', 'ArrowLeft'), null);
  assert.equal(nextNodeInDirection(nodes, 'c', 'Enter'), null);
});

test('review consistency badges separate a stopped defect from a recorded warning', () => {
  const block = reviewConsistencyView({reviewConsistency: {status: 'SUMMARY_REPORTS_DEFECT', rule: 'defect', evidence: ' fails on empty input ', stage: 'backend'}});
  assert.deepEqual([block.level, block.badge, block.rule, block.evidence, block.stage], ['block', 'Review defect', 'defect', 'fails on empty input', 'Mac edit review']);
  const warn = reviewConsistencyView({reviewConsistency: {status: 'SUMMARY_MAY_REPORT_DEFECT', rule: 'bad rule!', stage: 'macReturn'}});
  assert.deepEqual([warn.level, warn.rule, warn.evidence, warn.stage], ['warn', null, null, 'Mac return review']);
  assert.equal(reviewConsistencyView({reviewSummaryContradiction: true}).level, 'warn');
  assert.equal(reviewConsistencyView({reviewConsistency: {status: 'OK'}}).level, null);
  assert.equal(reviewConsistencyView(null).level, null);
});

test('headless switch is checked only for a fresh on lease and locks while pending or busy', () => {
  const on = {on: true, label: 'Headless on · 3h 12m left'}, off = {on: false, label: 'Headless off'}, unknown = {on: false, label: 'Headless unknown'};
  const live = headlessSwitchView(on, {feedFresh: true});
  assert.deepEqual([live.state, live.checked, live.disabled, live.short], ['on', true, false, '3h 12m left']);
  assert.deepEqual([headlessSwitchView(off, {feedFresh: true}).short, headlessSwitchView(off, {feedFresh: true}).checked], ['Off', false]);
  assert.equal(headlessSwitchView(unknown, {feedFresh: true}).disabled, true);
  assert.equal(headlessSwitchView(on, {feedFresh: false}).disabled, true);
  assert.deepEqual([headlessSwitchView(on, {feedFresh: true, pending: true}).state, headlessSwitchView(on, {feedFresh: true, pending: true}).checked], ['pending', false]);
  assert.equal(headlessSwitchView(off, {feedFresh: true, busy: true}).disabled, true);
  assert.equal(headlessSwitchView(off, {macOwner: false}).hidden, true);
});

test('camera reparenting marks itself painted first and focus handlers ignore the move', () => {
  // Measured 2026-09-26: restoring focus after the reparent re-entered applyCamera before
  // paintCamera was updated, recursing until the stack overflowed.
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  assert.match(app, /if\(paintCamera!==transform\)\{paintCamera=transform;/);
  assert.match(app, /reparenting=true;try\{/);
  assert.match(app, /addEventListener\('focus',\(\)=>\{if\(reparenting\)return;/);
  assert.match(app, /addEventListener\('blur',\(\)=>\{if\(reparenting\)return;/);
});

test('lanes carry the newest successful measured rate and nothing from errors or other lanes', () => {
  const jobs = { inFlight: [], recent: [
    { id: 'a', state: 'success', lane: 'fast', predictedPerSecond: 104.1, ageSeconds: 30 },
    { id: 'b', state: 'success', lane: 'fast', predictedPerSecond: 90, ageSeconds: 10 },
    { id: 'c', state: 'error', lane: 'fast', predictedPerSecond: 999, ageSeconds: 1 },
    { id: 'd', state: 'success', lane: 'deep', predictedPerSecond: null, ageSeconds: 5 }] };
  const view = windowsLaneView(laneWorker(), jobs, { feedFresh: true });
  const fast = view.lanes.find(lane => lane.id === 'fast'), deep = view.lanes.find(lane => lane.id === 'deep');
  assert.deepEqual([fast.rate, fast.rateAgeSeconds], [90, 10]);
  assert.deepEqual([deep.rate, deep.rateAgeSeconds], [null, null]);
});

test('activity feed merges PC jobs and router runs newest first, labels probes, and caps its length', () => {
  const jobs = { inFlight: [{ id: 'live', client: 'opencode', lane: 'deep', ageSeconds: 2 }],
    recent: [{ id: 'j1', state: 'success', client: 'codex', lane: 'fast', predictedPerSecond: 104.08, elapsedSeconds: .5, ageSeconds: 138 },
      { id: 'j2', state: 'success', client: 'pc-llm-probe', lane: 'fast', predictedPerSecond: 96.5, ageSeconds: 400 },
      { id: 'j3', state: 'error', client: 'bad label!', lane: 'gpu', ageSeconds: 50 },
      { id: 'j4', state: 'invalid-result', client: 'pc-llm-probe', lane: 'fast', ageSeconds: 900 }] };
  const runs = [{ runId: 'r1', client: 'claude', host: 'windows', status: 'RESPONSE_VALIDATED', ageSeconds: 60, source: 'router-archive',
    reviewConsistency: { status: 'SUMMARY_MAY_REPORT_DEFECT' } }, { runId: 'r1', client: 'claude', ageSeconds: 60, source: 'router-archive' }];
  const feed = activityFeed(jobs, runs);
  assert.deepEqual(feed.map(row => row.key), ['job:live', 'job:j3', 'run:router-archive:r1', 'job:j1', 'job:j2', 'job:j4']);
  assert.deepEqual([feed[0].state, feed[0].target, feed[0].client], ['in-flight', 'PC deep', 'opencode']);
  assert.deepEqual([feed[1].client, feed[1].target, feed[1].state], [null, 'Windows PC', 'error']);
  assert.deepEqual([feed[2].target, feed[2].review], ['Nisi Inference → PC', 'warn']);
  assert.equal(feed[3].rate, '104 tok/s');
  assert.equal(feed[4].probe, true);
  assert.equal(activityFeed(jobs, runs).find(row => row.key === 'job:j4').state, 'invalid-result');
  assert.equal(activityFeed(jobs, runs, { limit: 2 }).length, 2);
  assert.deepEqual(activityFeed(null, null), []);
});

test('vitals summarise Mac, PC, route and the latest non-probe activity without inventing state', () => {
  const lanes = windowsLaneView(laneWorker(), { inFlight: [], recent: [{ id: 'a', state: 'success', lane: 'fast', predictedPerSecond: 105.1, ageSeconds: 9 }] }, { feedFresh: true });
  const feed = activityFeed({ inFlight: [], recent: [{ id: 'p', state: 'success', client: 'pc-llm-probe', lane: 'fast', ageSeconds: 1 },
    { id: 'c', state: 'success', client: 'codex', lane: 'fast', predictedPerSecond: 104, ageSeconds: 30 }] }, []);
  const v = vitalsView({ fresh: true, models: [{ state: 'idle', loaded: true }, { state: 'unloaded', loaded: false }], activityKnown: true,
    loadedKnown: true, lanes, worker: { state: 'advertised' }, jobs: { running: false }, pipeline: { status: 'idle' }, nisi: { state: 'ready' }, feed });
  assert.deepEqual(v.mac, { tone: 'ok', detail: '1 loaded · idle' });
  assert.equal(v.pc.tone, 'ok');
  assert.equal(v.pc.detail, 'Headless 3h 25m · 1/2 lanes · fast 105 tok/s');
  assert.deepEqual(v.route, { tone: 'ok', detail: 'Idle · pair ready' });
  assert.deepEqual(v.activity, { tone: 'ok', detail: 'codex → PC fast 104 tok/s', short: 'codex → PC fast' });
  // Round 3 review: the short PC chip names the lane its speed belongs to.
  assert.equal(v.pc.short, 'On 3h 25m · fast 105 tok/s');
  const stale = vitalsView({ fresh: false, feed: [] });
  assert.deepEqual([stale.mac.tone, stale.pc.detail, stale.route.detail, stale.activity.detail], ['muted', 'Signal stale', 'Route age unknown', 'No recorded activity']);
  assert.equal(vitalsView({ fresh: true, worker: { state: 'degraded' }, feed: [] }).pc.tone, 'warn');
  assert.equal(vitalsView({ fresh: true, worker: { state: 'degraded' }, feed: [] }).pc.short, 'Degraded');
  assert.equal(vitalsView({ fresh: true, feed: [] }).activity.short, 'None yet');
  assert.equal(vitalsView({ fresh: true, pipeline: { status: 'running', stage: 'backend_response' }, feed: [] }).route.detail, 'Running · backend response');
});

test('a click on empty map background closes every panel and refits; node clicks and drags do not', () => {
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  assert.match(app, /\$\('map'\)\.addEventListener\('click',e=>\{if\(justDragged\|\|e\.target\.closest\?\.\('\.node'\)\)return;showWholeMap\(\);\}\)/);
  const body = app.slice(app.indexOf('function showWholeMap(){'), app.indexOf('function hideActivityDetails(){'));
  for (const call of ['hideOnlineCodeDetails()', 'hideFixInferenceDetails()', 'hideActivityDetails()', 'setSidebarOpen(false)', "$('aboutPanel').hidden=true", 'closeDrawer(false)', 'fitMap()'])
    assert.ok(body.includes(call), `showWholeMap must call ${call}`);
});

// Review fixes, 26 Sep 2026 (monitor.json).

test('F1: on a stale or paused feed an open PC job is "in flight at last sample", never running now', () => {
  const jobs = { inFlight: [{ id: 'live', client: 'claude', lane: 'deep', ageSeconds: 32, timeoutSeconds: 600 }],
    recent: [{ id: 'old', state: 'success', client: 'codex', lane: 'fast', predictedPerSecond: 100, ageSeconds: 400 }] };
  const stale = activityFeed(jobs, [], { feedFresh: false });
  assert.deepEqual(stale.map(row => [row.key, row.state]), [['job:live', 'in-flight-stale'], ['job:old', 'success']]);
  const staleVitals = vitalsView({ fresh: false, feed: stale });
  assert.deepEqual(staleVitals.activity, { tone: 'muted', detail: 'claude → PC deep in flight at last sample', short: 'claude → PC deep' });
  // Even an in-flight row that reaches vitalsView with the feed stale cannot pulse live.
  const guarded = vitalsView({ fresh: false, feed: activityFeed(jobs, []) });
  assert.equal(guarded.activity.tone, 'muted');
  assert.doesNotMatch(guarded.activity.detail, /running/);
  const live = vitalsView({ fresh: true, feed: activityFeed(jobs, [], { feedFresh: true }) });
  assert.deepEqual([live.activity.tone, live.activity.detail], ['live', 'claude → PC deep running']);
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  // Follow-up 26 Sep: the activity path uses liveNow() (fresh and not paused), not fresh() alone.
  assert.match(app, /activityFeed\(jobs,snapshot\?\.host==='mac'\?runs\(\):\[\],\{limit:12,feedFresh:liveNow\(\)\}\)/);
  assert.match(app, /const liveNow=\(\)=>fresh\(\)&&!paused;/);
  assert.match(app, /row\.state==='in-flight-stale'\?'In flight at last sample'/);
});

test('F9: the route chip counts a route-stopping review defect apart from recorded warnings', () => {
  const runs = [{ runId: 'd1', status: 'REVIEW_FINDINGS', ageSeconds: 10, reviewConsistency: { status: 'SUMMARY_REPORTS_DEFECT' } },
    { runId: 'w1', status: 'RESPONSE_VALIDATED', ageSeconds: 20, reviewConsistency: { status: 'SUMMARY_MAY_REPORT_DEFECT' } },
    { runId: 'w2', status: 'RESPONSE_VALIDATED', ageSeconds: 30, reviewSummaryContradiction: true }];
  const base = { fresh: true, pipeline: { status: 'idle' }, nisi: { state: 'ready' } };
  assert.deepEqual(vitalsView({ ...base, feed: activityFeed(null, runs) }).route,
    { tone: 'warn', detail: 'Idle · pair ready · 1 review defect · 2 review warnings' });
  assert.deepEqual(vitalsView({ ...base, feed: activityFeed(null, runs.slice(1)) }).route,
    { tone: 'ok', detail: 'Idle · pair ready · 2 review warnings' });
  assert.deepEqual(vitalsView({ ...base, feed: activityFeed(null, [runs[0], { ...runs[0], runId: 'd2' }]) }).route,
    { tone: 'warn', detail: 'Idle · pair ready · 2 review defects' });
});

test('F7: the PC lane rows name why lanes are missing instead of always blaming the heartbeat', () => {
  const beat = laneWorker();
  assert.deepEqual(windowsLaneRows(windowsLaneView(beat, null, { feedFresh: true })),
    [['Lane fast · gpt-oss-20b', 'Up · 1/2 slots busy'], ['Lane deep · Qwen3.8-27B', 'Down · Slots unknown']]);
  assert.deepEqual(windowsLaneRows(windowsLaneView(beat, null, { feedFresh: false })), [['Lanes', 'Not reported (live feed stale)']]);
  assert.deepEqual(windowsLaneRows(windowsLaneView({ ...beat, ageSeconds: 400 }, null, { feedFresh: true })),
    [['Lanes', 'Not reported (worker heartbeat not fresh)']]);
  assert.deepEqual(windowsLaneRows(windowsLaneView({ state: 'unknown' }, null, { feedFresh: true })),
    [['Lanes', 'Not reported (worker heartbeat not fresh)']]);
  // A fresh 4-second-old heartbeat without (valid) lane detail, ready or degraded.
  for (const worker of [laneWorker({ lanes: null }), laneWorker({ state: 'degraded', lanes: null })])
    assert.deepEqual(windowsLaneRows(windowsLaneView(worker, null, { feedFresh: true })), [['Lanes', 'Heartbeat has no lane detail']]);
  // Follow-up 26 Sep: detail that was sent but rejected reads "Lane detail malformed", never "no lane detail".
  assert.deepEqual(windowsLaneRows(windowsLaneView(laneWorker({ lanes: { fast: { up: 'yes' } } }), null, { feedFresh: true })),
    [['Lanes', 'Lane detail malformed']]);
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  assert.match(app, /\.\.\.windowsLaneRows\(lanesFresh\?lanes:\{\.\.\.lanes,visible:false,beat:false\}\)\]/);
  assert.doesNotMatch(app, /function laneRows\(/);
});

// Approximate rendered text boxes: captions use the scale applyCamera gives them.
const CAPTION = { label: { font: 12, charWidth: .56 }, sub: { font: 7, charWidth: .6 } };
function captionBox(node, spot, text, kind, scale) {
  const font = CAPTION[kind].font * scale, width = text.length * font * CAPTION[kind].charWidth;
  const x = node.x + spot.x, y = node.y + spot.y, left = spot.anchor === 'end' ? x - width : spot.anchor === 'start' ? x : x - width / 2;
  return { left, right: left + width, top: y - font * .8, bottom: y + font * .25 };
}
const boxesMeet = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

test('F3: in the 820x660 popover the PC hub subtitle never lands on its lane captions', () => {
  // The popover's map area is 820x604 (56 px top bar, no footer); the drawer-fit zoom is about 48%.
  const layout = constellationLayout(9, Array.from({ length: 5 }, () => ({ modelCount: 3 })), { viewportWidth: 820, viewportHeight: 604 });
  const pc = { ...layout.windows, kind: 'windows-worker', r: 12 }, gap = layout.windowsLanes[0].y - pc.y;
  const lanes = layout.windowsLanes.map((pos, i) => ({ ...pos, kind: 'windows-lane', r: 7, captionSide: i ? 'right' : 'left',
    label: i ? 'deep · Qwen3.8-27B Q4…' : 'fast · gpt-oss-20b', sub: i ? 'DOWN' : '1/2 BUSY · 104 TOK/S' }));
  const pcSub = 'JOB IN FLIGHT · QWEN3.8 27B Q4_K_M · 32s · HEADLESS ON · 3H 19M LEFT';
  const hub = { ...layout.runtime, kind: 'runtime', r: 18, hub: true, root: true };
  const clash = (z, laneGap) => {
    const scale = Math.max(1, .78 / z), place = nodeCaptionLayout(pc, scale, { laneGap });
    const sub = captionBox(pc, place.sub, pcSub, 'sub', scale), label = captionBox(pc, place.label, 'Windows PC', 'label', scale);
    const laneBoxes = lanes.flatMap(lane => { const at = nodeCaptionLayout(lane, scale);
      return [captionBox(lane, at.label, lane.label, 'label', scale), captionBox(lane, at.sub, lane.sub, 'sub', scale)]; });
    return { place, sub, label, laneBoxes, scale };
  };
  // The unfixed placement (subtitle always below) overprinted both lane captions at 48%.
  assert.ok(clash(.48, Infinity).laneBoxes.some(box => boxesMeet(clash(.48, Infinity).sub, box)));
  for (let z = .3; z <= 1.7; z += .02) {
    const { place, sub, label, laneBoxes } = clash(z, gap);
    assert.ok(!laneBoxes.some(box => boxesMeet(sub, box)), `zoom ${Math.round(z * 100)}%: PC subtitle meets a lane caption`);
    if (z >= .4) assert.ok(!laneBoxes.some(box => boxesMeet(label, box)), `zoom ${Math.round(z * 100)}%: PC label meets a lane caption`);
    if (z >= .8) assert.equal(place.subAbove, false, 'zoomed in, the subtitle stays below like every other node');
  }
  for (const z of [.44, .48, .6]) {
    const { place, sub, scale } = clash(z, gap);
    assert.equal(place.subAbove, true);
    // Above the hub it stays clear of the Mac hub's own captions, and the WINDOWS PC cluster label finds room.
    const hubPlace = nodeCaptionLayout(hub, scale);
    assert.ok(!boxesMeet(sub, { ...captionBox(hub, hubPlace.sub, 'LOCAL HUB', 'sub', scale), bottom: hub.y + hubPlace.sub.y + 8 * scale * .25 }));
    const obstacles = [sub, captionBox(pc, place.label, 'Windows PC', 'label', scale),
      { left: pc.x - 18, right: pc.x + 18, top: pc.y - 18, bottom: pc.y + 18 },
      { left: hub.x - 60, right: hub.x + 60, top: hub.y, bottom: hub.y + hubPlace.sub.y + 3 * scale }];
    const cluster = { x: layout.windowsLabel.x, y: layout.windowsLabel.y, align: 'middle', width: 20 * 11 / z * .62, height: 11 / z * 1.2 * .8 };
    const [moved] = resolveLabelCollisions([cluster], obstacles, { step: 3 / z, maxShift: 48 / z });
    const box = { left: moved.x - cluster.width / 2, right: moved.x + cluster.width / 2, top: moved.y - cluster.height, bottom: moved.y + cluster.height * .25 };
    assert.ok(!obstacles.some(o => boxesMeet(box, o)), `zoom ${Math.round(z * 100)}%: WINDOWS PC label found no clear place`);
  }
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  assert.match(app, /const place=nodeCaptionLayout\(n,scale,\{laneGap:n\.kind==='windows-worker'\?/);
  assert.match(app, /sub\.setAttribute\('y',place\.sub\.y\)/);
});

test('F3: nodeCaptionLayout keeps every other caption exactly where applyCamera put it', () => {
  for (const scale of [1, 1.3, 2.6]) {
    for (const node of [{ r: 18, hub: true, root: true }, { r: 9, root: true }, { r: 11, kind: 'model' }, { r: 11, kind: 'model', captionAbove: true }]) {
      const place = nodeCaptionLayout(node, scale);
      assert.equal(place.label.y, node.captionAbove ? -(node.r + 9 * scale) : node.r + (node.hub ? 30 : node.root ? 25 : 19) * scale);
      assert.equal(place.sub.y, node.r + (node.hub ? 44 : node.root ? 39 : 32) * scale);
      assert.equal(place.subAbove, false);
    }
    for (const side of ['left', 'right']) {
      const place = nodeCaptionLayout({ r: 7, kind: 'windows-lane', captionSide: side }, scale);
      assert.deepEqual([place.label.x, place.label.y, place.sub.y, place.label.anchor], [side === 'left' ? -14 : 14, 4 * scale, 16 * scale, side === 'left' ? 'end' : 'start']);
    }
    // Without lanes below it the PC hub keeps its subtitle below.
    assert.equal(nodeCaptionLayout({ r: 12, kind: 'windows-worker' }, scale).subAbove, false);
  }
});

// UI pass and review follow-ups, 26 Sep 2026.

// A text box in layout units: font is the rendered size (already scaled), charWidth an upper estimate per em.
function textBox(node, spot, text, font, charWidth) {
  const width = text.length * font * charWidth, x = node.x + spot.x, y = node.y + spot.y;
  const left = spot.anchor === 'end' ? x - width : spot.anchor === 'start' ? x : x - width / 2;
  return { left, right: left + width, top: y - font * .8, bottom: y + font * .25 };
}

test('Follow-up 1: flipped above the hub, the PC subtitle is short and clears every caption above it (640x504, 820x604)', () => {
  const clients = Array.from({ length: 5 }, () => ({ modelCount: 3 })), names = ['Codex', 'Claude', 'OpenCode', 'Cursor', 'Grok'];
  const full = 'JOB IN FLIGHT · QWEN3.8 27B Q4_K_M · 32s · HEADLESS ON · 3H 19M LEFT';
  // Every short form runtimeGraph can produce, with the longest time each can show.
  const briefs = ['LAST ANSWER · 123.4s', 'JOB IN FLIGHT · 629s', 'WORKER ADVERTISED', 'WORKER UNVERIFIED', 'WORKER DEGRADED', 'WORKER STOPPED'];
  // The 640x560 window's map area is 640x504 and the 820x660 popover's 820x604 (56 px top bar, no footer);
  // with a sheet open and the PC selected they fit at 38% and 46% (measured). Checked from 34% up.
  for (const [w, h] of [[640, 504], [820, 604]]) {
    const layout = constellationLayout(9, clients, { viewportWidth: w, viewportHeight: h });
    const gap = layout.windowsLanes[0].y - layout.windows.y, hub = { ...layout.runtime, kind: 'runtime', r: 18, hub: true, root: true };
    let flipped = 0;
    for (let z = .34; z <= 1.7; z += .01) {
      const scale = Math.max(1, .78 / z), hubAt = nodeCaptionLayout(hub, scale);
      // What sits above the PC hub: the Mac hub's name and subtitle, and each client branch's star and always-on name.
      const above = [textBox(hub, hubAt.label, 'This Mac', 19 * scale, .56), textBox(hub, hubAt.sub, 'LOCAL HUB', 8 * scale, .6)];
      layout.clientLanes.forEach((lane, i) => {
        const node = { ...lane, kind: 'client', r: 9, root: true };
        above.push({ left: lane.x - 12, right: lane.x + 12, top: lane.y - 12, bottom: lane.y + 12 }, textBox(node, nodeCaptionLayout(node, scale).label, names[i], 13 * scale, .56));
      });
      for (const brief of briefs) {
        const pc = { ...layout.windows, kind: 'windows-worker', r: 12, subtitle: full, subtitleAbove: brief };
        const place = nodeCaptionLayout(pc, scale, { laneGap: gap });
        if (!place.subAbove) { assert.equal(place.subText, full, 'below the hub the full subtitle stays'); continue; }
        flipped++;
        assert.equal(place.subText, brief);
        const sub = textBox(pc, place.sub, place.subText, 7 * scale, .6);
        for (const box of above) assert.ok(!boxesMeet(sub, box), `${w}x${h} at ${Math.round(z * 100)}%: "${brief}" meets a caption above the hub`);
      }
    }
    assert.ok(flipped > 100, `${w}x${h}: the flip happens over the popover zoom range`);
    // Dropping only the headless suffix is not enough at 640x504: the model name reaches the fifth client branch.
    if (w === 640) {
      const scale = .78 / .44, pc = { ...layout.windows, kind: 'windows-worker', r: 12 };
      const medium = textBox(pc, nodeCaptionLayout(pc, scale, { laneGap: gap }).sub, 'JOB IN FLIGHT · QWEN3.8 27B Q4_K_M · 32s', 7 * scale, .6);
      const fifth = layout.clientLanes[4];
      assert.ok(boxesMeet(medium, { left: fifth.x - 12, right: fifth.x + 12, top: fifth.y - 12, bottom: fifth.y + 12 }));
    }
  }
  // The app gives the PC hub its short form and hides the static "WINDOWS PC" header while the subtitle is up there.
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  assert.match(app, /subtitleAbove:brief,kind:'windows-worker'/);
  assert.match(app, /const subText=place\.subText\?\?n\.subtitle\?\?'';if\(sub\.textContent!==subText\)sub\.textContent=subText;if\(n\.kind==='windows-worker'\)pcSubAbove=place\.subAbove;/);
  assert.match(app, /label\.setAttribute\('visibility',graph\.labels\[i\]\?\.kind==='windows'&&pcSubAbove\?'hidden':'visible'\)/);
  const brief = app.match(/const brief=([^;]+);/)[1];
  assert.doesNotMatch(brief, /HEADLESS|name\(/, 'the short form carries neither the headless suffix nor the model name');
});

test('Follow-up 1: nodeCaptionLayout switches to the short subtitle only when it flips', () => {
  const pc = { r: 12, kind: 'windows-worker', subtitle: 'LONG · HEADLESS ON', subtitleAbove: 'SHORT' };
  assert.deepEqual([nodeCaptionLayout(pc, 1, { laneGap: 64 }).subAbove, nodeCaptionLayout(pc, 1, { laneGap: 64 }).subText], [false, 'LONG · HEADLESS ON']);
  assert.deepEqual([nodeCaptionLayout(pc, 2, { laneGap: 64 }).subAbove, nodeCaptionLayout(pc, 2, { laneGap: 64 }).subText], [true, 'SHORT']);
  assert.equal(nodeCaptionLayout({ ...pc, subtitleAbove: undefined }, 2, { laneGap: 64 }).subText, 'LONG · HEADLESS ON');
  assert.equal(nodeCaptionLayout({ r: 7, kind: 'windows-lane', captionSide: 'left', subtitle: 'UP' }, 2).subText, 'UP');
});

test('Follow-up 4: a heartbeat whose lane detail was rejected says "Lane detail malformed", not "no lane detail"', () => {
  const rejected = windowsLaneView(laneWorker({ lanes: null, lanesError: 'malformed lane detail' }), null, { feedFresh: true });
  assert.equal(rejected.lanesError, true);
  assert.deepEqual(windowsLaneRows(rejected), [['Lanes', 'Lane detail malformed']]);
  assert.deepEqual(windowsLaneRows(windowsLaneView(laneWorker({ lanes: null, lanesError: null }), null, { feedFresh: true })), [['Lanes', 'Heartbeat has no lane detail']]);
  assert.equal(windowsLaneView(laneWorker({ lanes: null, lanesError: '  ' }), null, { feedFresh: true }).lanesError, false);
  // A stale feed or heartbeat still names that reason first.
  assert.deepEqual(windowsLaneRows(windowsLaneView(laneWorker({ lanes: null, lanesError: 'x' }), null, { feedFresh: false })), [['Lanes', 'Not reported (live feed stale)']]);
  assert.deepEqual(windowsLaneRows(windowsLaneView(laneWorker({ lanes: null, lanesError: 'x', ageSeconds: 400 }), null, { feedFresh: true })), [['Lanes', 'Not reported (worker heartbeat not fresh)']]);
});

test('C: the Mac GPU sample reads only when fresh and within bounds', () => {
  const gpu = { model: 'Apple M5 Max', cores: 40, utilizationPercent: 46, rendererPercent: 41, tilerPercent: 9, allocatedBytes: 40_500_000_000, inUseBytes: 31_200_000_000, ageSeconds: .4 };
  const view = macGpuView(gpu, { feedFresh: true, snapshotAge: 1 });
  // Round 3 review: "allocated" was vague; the tile says the whole GPU is this busy and holds this much memory.
  // 27 Sep: binary gigabytes, like the memory guard and the Memory section beside it (40.5e9 bytes read 37.7 GB there too).
  assert.deepEqual([view.known, view.percent, view.chip, view.tile], [true, 46, 'GPU 46%', '46% busy · 37.7 GB GPU memory']);
  assert.deepEqual(view.rows, [['GPU', 'Apple M5 Max · 40 cores'], ['GPU utilisation', '46%'], ['Renderer · tiler', '41% · 9%'],
    ['GPU memory allocated', '37.7 GB'], ['GPU memory in use', '29.1 GB']]);
  const unknown = { known: false, percent: null, chip: null, tile: 'Unknown', rows: [] };
  for (const [value, options] of [[gpu, { feedFresh: false }], [gpu, { feedFresh: true, snapshotAge: 5 }], [null, { feedFresh: true }],
    [[], { feedFresh: true }], [{ ...gpu, ageSeconds: -1 }, { feedFresh: true }], [{ ...gpu, ageSeconds: 'x' }, { feedFresh: true }]])
    assert.deepEqual(macGpuView(value, options), unknown);
  const odd = macGpuView({ ...gpu, utilizationPercent: 146, allocatedBytes: -1, inUseBytes: 1.5, model: '<b>', cores: 0 }, { feedFresh: true });
  assert.deepEqual([odd.known, odd.chip, odd.tile, odd.rows[0][1], odd.rows[4][1]], [false, null, 'Unknown', null, null]);
});

test('C: local callers are names only and deduplicated; null means not checked', () => {
  assert.deepEqual(localCallersView([{ pid: 1, name: 'opencode', connections: 2 }, { pid: 2, name: 'node', connections: 1 },
    { pid: 3, name: 'opencode', connections: 1 }, { pid: 4, name: 'rm -rf /;' }, null], { feedFresh: true }),
  { checked: true, names: ['opencode', 'node'], text: 'Calling the model server now: opencode, node' });
  assert.equal(localCallersView([], { feedFresh: true }).text, 'No local process is calling the model server');
  assert.deepEqual(localCallersView(null, { feedFresh: true }), { checked: false, names: [], text: null });
  assert.equal(localCallersView([{ name: 'node' }], { feedFresh: false }).checked, false);
  assert.equal(localCallersView(Array.from({ length: 9 }, (_, i) => ({ name: `p${i}` })), { feedFresh: true }).names.length, 6);
});

test('C: prompt and generation speeds and the hit-token-limit flag reach activity rows and lanes', () => {
  assert.equal(speedsText(210.4, 104.2), 'reads 210 tok/s · writes 104 tok/s');
  assert.equal(speedsText(null, 12.34), 'writes 12.3 tok/s');
  assert.equal(speedsText(55, null), 'reads 55 tok/s');
  assert.equal(speedsText(0, -1), null);
  assert.deepEqual(jobFlags({ flags: ['hit-token-limit', 'bogus', 'hit-token-limit'] }), ['hit-token-limit']);
  assert.deepEqual([jobFlags({ flags: 'hit-token-limit' }), jobFlags(null)], [[], []]);
  const jobs = { inFlight: [{ id: 'live', client: 'claude', lane: 'deep', ageSeconds: 2, flags: ['hit-token-limit'] }],
    recent: [{ id: 'a', state: 'success', client: 'codex', lane: 'fast', predictedPerSecond: 104, promptPerSecond: 210, ageSeconds: 30, flags: ['hit-token-limit'] },
      { id: 'b', state: 'success', client: 'codex', lane: 'fast', predictedPerSecond: 90, ageSeconds: 60 },
      { id: 'c', state: 'invalid', client: 'codex', lane: 'deep', predictedPerSecond: 50, promptPerSecond: 70, ageSeconds: 5 }] };
  const feed = activityFeed(jobs, []), row = id => feed.find(item => item.key === `job:${id}`);
  assert.deepEqual([row('a').rate, row('a').speeds, row('a').limitHit], ['104 tok/s', 'reads 210 tok/s · writes 104 tok/s', true]);
  assert.deepEqual([row('b').speeds, row('b').limitHit], ['writes 90 tok/s', false]);
  assert.deepEqual([row('c').speeds, row('c').state, row('live').limitHit], [null, 'invalid', false]);
  assert.deepEqual(laneSpeeds(jobs, 'fast'), { text: 'reads 210 tok/s · writes 104 tok/s', ageSeconds: 30, limitHit: true });
  assert.deepEqual(laneSpeeds(jobs, 'deep'), { text: null, ageSeconds: null, limitHit: false });
  assert.equal(windowsLaneView(laneWorker(), jobs, { feedFresh: true }).lanes.find(lane => lane.id === 'fast').rate, 104, 'same row as the lane rate');
  // An answer rejected as invalid reads as a failure on the chip.
  assert.equal(vitalsView({ fresh: true, feed: activityFeed({ recent: [jobs.recent[2]] }, []) }).activity.tone, 'warn');
});

test('C and follow-up 2: the Mac chip adds a known GPU sample; only a live activity path says running', () => {
  const base = { fresh: true, models: [{ state: 'idle', loaded: true }], activityKnown: true, loadedKnown: true, feed: [] };
  // Round 3 review: the idle short chip keeps the state ("Idle · GPU 46%"), so a whole-GPU figure never reads as work.
  assert.deepEqual(vitalsView({ ...base, gpu: { known: true, chip: 'GPU 46%' } }).mac, { tone: 'ok', detail: '1 loaded · idle · GPU 46%', short: 'Idle · GPU 46%' });
  assert.equal(vitalsView({ ...base, activityKnown: false, gpu: { known: true, chip: 'GPU 46%' } }).mac.short, 'Activity unknown · GPU 46%');
  assert.deepEqual(vitalsView({ ...base, models: [{ state: 'generating', loaded: true }], gpu: { known: true, chip: 'GPU 92%' } }).mac,
    { tone: 'live', detail: '1 generating · GPU 92%', short: '1 generating · GPU 92%' });
  assert.deepEqual(vitalsView({ ...base, gpu: { known: false, chip: null } }).mac, { tone: 'ok', detail: '1 loaded · idle' });
  assert.deepEqual(vitalsView({ ...base, fresh: false, gpu: { known: true, chip: 'GPU 46%' } }).mac, { tone: 'muted', detail: 'Signal stale' });
  const open = activityFeed({ inFlight: [{ id: 'j', client: 'claude', lane: 'deep', ageSeconds: 1 }] }, [], { feedFresh: true });
  assert.equal(vitalsView({ fresh: true, feed: open }).activity.tone, 'live');
  const paused = vitalsView({ fresh: true, feed: open, activityLive: false }).activity;
  assert.deepEqual([paused.tone, paused.detail], ['muted', 'claude → PC deep in flight at last sample']);
});

test('B: the Live activity summary says what is running, held, failed or quiet in one line', () => {
  const summary = activitySummary([]);
  assert.deepEqual([summary.tone, summary.line], ['muted', 'Nothing recorded yet']);
  assert.deepEqual(summary.tiles, [['In flight', '0'], ['Answered', '0'], ['Failed', '0'], ['Routes', '0']]);
  const jobs = { inFlight: [{ id: 'j', client: 'claude', lane: 'deep', ageSeconds: 1 }, { id: 'k', client: 'codex', lane: 'fast', ageSeconds: 3 }],
    recent: [{ id: 'a', state: 'success', client: 'codex', lane: 'fast', ageSeconds: 30 }, { id: 'e', state: 'error', client: 'codex', lane: 'fast', ageSeconds: 40 }] };
  const runs = [{ runId: 'r1', client: 'claude', status: 'RESPONSE_VALIDATED', ageSeconds: 50 }];
  const live = activitySummary(activityFeed(jobs, runs, { feedFresh: true }), { live: true });
  assert.deepEqual([live.tone, live.line], ['live', '2 jobs running now']);
  assert.deepEqual(live.tiles, [['In flight', '2'], ['Answered', '1'], ['Failed', '1'], ['Routes', '1']]);
  const held = activitySummary(activityFeed(jobs, runs, { feedFresh: false }), { live: false });
  assert.deepEqual([held.tone, held.line], ['muted', '2 jobs in flight at the last sample']);
  // "live" without live rows (or live rows without a live flag) never pulses.
  assert.equal(activitySummary(activityFeed(jobs, runs, { feedFresh: true }), { live: false }).tone, 'muted');
  const failed = activitySummary(activityFeed({ recent: [{ id: 'x', state: 'timeout', client: 'codex', ageSeconds: 2 }, jobs.recent[0]] }, []), { live: true });
  assert.deepEqual([failed.tone, failed.line], ['warn', 'The latest job did not answer']);
  assert.deepEqual(activitySummary(activityFeed({ recent: [jobs.recent[0]] }, []), { live: true }).line, 'Nothing running now');
  for (const s of [summary, live, held, failed]) assert.ok(s.line.length <= 60 && !/mac-|[0-9a-f]{8}/.test(s.line));
});

// Round 3 review follow-ups, 26 Sep 2026.

const GPU = { index: 0, name: 'NVIDIA GeForce RTX 4070', utilizationPercent: 32, memoryUsedMiB: 11980, memoryTotalMiB: 12282, temperatureC: 54, powerW: 118.5 };

test('Round 3: the PC GPU and worker version come from a fresh worker heartbeat, checked again', () => {
  const worker = laneWorker({ workerVersion: '1.2', gpus: [{ ...GPU, index: 1, name: 'Second', utilizationPercent: 3 }, GPU] });
  const view = pcGpuView(worker, { feedFresh: true, snapshotAge: 1 });
  assert.deepEqual([view.known, view.version, view.chip, view.text, view.tile],
    [true, '1.2', 'GPU 32%', 'GPU 32% · 11.7/12.0 GB · 54 °C', '32% · 11.7/12.0 GB · 54 °C']);
  assert.deepEqual(view.rows, [['Worker version', '1.2'], ['GPU 0', 'NVIDIA GeForce RTX 4070 · 32% busy · 11.7 of 12.0 GB · 54 °C · 119 W'],
    ['GPU 1', 'Second · 3% busy · 11.7 of 12.0 GB · 54 °C · 119 W']]);
  // A degraded heartbeat still reports its GPU.
  assert.equal(pcGpuView({ ...worker, state: 'degraded' }, { feedFresh: true }).text, 'GPU 32% · 11.7/12.0 GB · 54 °C');
  // Stale feed, old heartbeat, unknown worker: nothing, and the rows say why.
  for (const [value, options] of [[worker, { feedFresh: false }], [{ ...worker, ageSeconds: 58 }, { feedFresh: true, snapshotAge: 3 }],
    [{ ...worker, state: 'unknown' }, { feedFresh: true }], [null, { feedFresh: true }]]) {
    const unknown = pcGpuView(value, options);
    assert.deepEqual([unknown.known, unknown.version, unknown.chip, unknown.text, unknown.tile], [false, null, null, null, 'Unknown']);
    assert.deepEqual(unknown.rows, [['Worker version', 'Not reported'], ['PC GPU', 'Unknown (no fresh worker heartbeat)']]);
  }
  // A fresh heartbeat without GPUs (a pre-1.2 worker, or null) says so.
  assert.deepEqual(pcGpuView(laneWorker({ gpus: null }), { feedFresh: true }).rows,
    [['Worker version', 'Not reported'], ['PC GPU', 'Not reported by the worker heartbeat']]);
  // One bad row makes the whole sample unknown.
  for (const bad of [{ utilizationPercent: 101 }, { utilizationPercent: '32' }, { memoryUsedMiB: 12283 }, { memoryTotalMiB: 0 }, { temperatureC: 151 },
    { powerW: -1 }, { index: 1.5 }, { name: '<b>\u00e9</b>' }, { name: ' x' }, { powerW: NaN }])
    assert.equal(pcGpuView(laneWorker({ gpus: [GPU, { ...GPU, index: 1, ...bad }] }), { feedFresh: true }).known, false, JSON.stringify(bad));
  assert.equal(pcGpuView(laneWorker({ gpus: [GPU, GPU] }), { feedFresh: true }).known, false, 'duplicate index');
  assert.equal(pcGpuView(laneWorker({ gpus: Array.from({ length: 5 }, (_, i) => ({ ...GPU, index: i })) }), { feedFresh: true }).known, false);
  assert.equal(pcGpuView(laneWorker({ workerVersion: '1.2; rm -rf /', gpus: [GPU] }), { feedFresh: true }).version, null);
});

test('Round 3: the PC chip shows the GPU in its detail, names the fast lane when short and follows Pause', () => {
  const lanes = windowsLaneView(laneWorker(), { inFlight: [], recent: [{ id: 'a', state: 'success', lane: 'fast', predictedPerSecond: 104.2, ageSeconds: 9 }] }, { feedFresh: true });
  const pcGpu = pcGpuView(laneWorker({ gpus: [GPU] }), { feedFresh: true });
  const base = { fresh: true, lanes, worker: { state: 'advertised' }, feed: [], pcGpu };
  const v = vitalsView({ ...base, jobs: { running: false } });
  assert.deepEqual(v.pc, { tone: 'ok', detail: 'Headless 3h 25m · 1/2 lanes · fast 104 tok/s · GPU 32% · 11.7/12.0 GB · 54 °C', short: 'On 3h 25m · fast 104 tok/s' });
  assert.equal(vitalsView({ ...base, jobs: { running: true } }).pc.tone, 'live');
  // Paused (or any non-live view): a running job no longer pulses the PC chip; it reads as the worker.
  assert.equal(vitalsView({ ...base, jobs: { running: true }, activityLive: false }).pc.tone, 'ok');
  assert.equal(vitalsView({ ...base, worker: { state: 'degraded' }, jobs: { running: true }, activityLive: false }).pc.tone, 'warn');
});

test('Round 3: a cancelled PC job is settled: "cancelled" on the chip, muted, never a failure', () => {
  const jobs = { inFlight: [{ id: 'open', client: 'codex', lane: 'fast', ageSeconds: 4, cancelRequested: true }],
    recent: [{ id: 'c', state: 'cancelled', client: 'claude', lane: 'deep', elapsedSeconds: 11.5, ageSeconds: 2, predictedPerSecond: 40 }] };
  const feed = activityFeed(jobs, [], { feedFresh: true });
  const row = id => feed.find(item => item.key === `job:${id}`);
  assert.deepEqual([row('c').state, row('c').rate, row('c').speeds, row('c').cancelRequested], ['cancelled', null, null, false]);
  assert.deepEqual([row('open').state, row('open').cancelRequested], ['in-flight', true]);
  const chip = vitalsView({ fresh: true, feed: activityFeed({ recent: jobs.recent }, []) }).activity;
  assert.deepEqual([chip.tone, chip.detail], ['muted', 'claude → PC deep cancelled']);
  const summary = activitySummary(activityFeed({ recent: jobs.recent }, []), { live: true });
  assert.deepEqual([summary.tone, summary.line, summary.tiles[2]], ['ok', 'Nothing running now', ['Failed', '0']]);
});

test('Round 3: the Live activity meaning names the running job or the newest failure, not the plumbing', () => {
  const jobs = { inFlight: [{ id: 'j', client: 'claude', lane: 'deep', ageSeconds: 32.7 }, { id: 'k', client: 'opencode', lane: 'fast', ageSeconds: 3 }], recent: [] };
  assert.equal(activitySummary(activityFeed(jobs, [], { feedFresh: true }), { live: true }).meaning, 'OpenCode is waiting on the PC fast lane (3 s). 1 more running.');
  assert.equal(activitySummary(activityFeed({ inFlight: [jobs.inFlight[0]] }, [], { feedFresh: true }), { live: true }).meaning, 'Claude is waiting on the PC deep lane (32 s).');
  assert.equal(activitySummary(activityFeed({ inFlight: [{ id: 'p', client: 'pc-llm-probe', lane: 'fast', ageSeconds: 1 }] }, [], { feedFresh: true }), { live: true }).meaning,
    'The PC LLM switch probe is probing the PC fast lane (1 s).');
  const failed = (state, extra = {}) => activitySummary(activityFeed({ recent: [{ id: 'x', state, client: 'codex', lane: 'fast', ageSeconds: 2, ...extra }] }, []), { live: true }).meaning;
  assert.equal(failed('invalid'), 'Codex → PC fast had its answer rejected as invalid. Select it to open it on the map.');
  assert.equal(failed('timeout'), 'Codex → PC fast timed out. Select it to open it on the map.');
  assert.equal(failed('error', { client: 'unknown-agent' }), 'Unknown-agent → PC fast ended in an error. Select it to open it on the map.');
  for (const s of [activitySummary(activityFeed(jobs, [], { feedFresh: true }), { live: true })]) assert.doesNotMatch(s.meaning, /journal|archive|mac-/);
});

// Orb web, 26-27 Sep 2026 (Louis: "make the constellation map like a hybrid of spiderweb", then "make the background
// glow too when the node pulse" and "like when you poke a spiderweb it bounces"). The web is a pure function of the
// graph the dashboard draws; these tests build that graph with the dashboard's own runtimeGraph/traceGraph.
const {
  orbWebKey, orbWebLayout, orbWebActivity, orbWebPluck, orbWebPathsAt, orbWebFlashPath, pluckOffsets, pluckWave, pokeScale,
  createWebMotion, edgeCurvePath, webGlows, glowRadius, ORB_WEB_LIMITS, WEB_PLUCK,
} = layoutModule;
const orbAppSource = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8').replace(/^import .*;\n/gm, '').split("$('pause').addEventListener")[0];
const ORB_RUN = { runId: 'route-20260926-181200-7c1f', source: 'router-archive', status: 'RESPONSE_VALIDATED', stage: 'review', ageSeconds: 95,
  calls: ['intake', 'author', 'reviewer', 'repair', 'reviewer', 'judge'].map((role, i) => ({ id: `c${i}`, role, model: i % 2 ? 'google/gemma-4-12b' : 'qwen/qwen3.8-27b',
    ...(role === 'intake' ? { decision: { choice: 'mac-local', confidence: .9 } } : {}) })) };
function orbSnapshot(job) {
  const now = Date.now() / 1000;
  return { host: 'mac', sampledAt: now, fullSampledAt: now, activityKnown: true,
    models: [{ id: 'google/gemma-4-12b', host: 'mac', state: job ? 'generating' : 'idle', loaded: true, ageSeconds: 0, metadata: { parameters: '12B', capabilities: { vision: true } } },
      { id: 'qwen/qwen3.8-27b', host: 'mac', state: 'unloaded', loaded: false, ageSeconds: 0, metadata: { parameters: '27B', capabilities: { reasoning: true } } }],
    sources: [{ id: 'lms-ps', state: 'live' }], clients: [{ id: 'claude', model: 'claude-opus-5-5', modelState: 'observed' }, { id: 'codex', model: 'gpt-6-sol', modelState: 'observed' }],
    pipeline: { status: 'idle' }, components: [{ id: 'nisi', state: 'ready' }],
    windowsWorker: { state: 'advertised', ageSeconds: 4, modelsAdvertised: ['gpt-oss-20b'], detail: 'x', headless: { state: 'on', expiresInSeconds: 7000 },
      lanes: { fast: { up: true, model: 'gpt-oss-20b', kind: 'gpt-oss', slotsBusy: 0, slotsTotal: 2 }, deep: { up: true, model: 'Qwen3.8-27B Q4_K_M', kind: 'qwen', slotsBusy: job ? 1 : 0, slotsTotal: 1 } } },
    windowsJobs: { schemaVersion: 1, inFlight: job ? [{ id: 'mac-20260926-181500-aaaaaaaaaaaa', model: 'Qwen3.8-27B Q4_K_M', client: 'claude', lane: 'deep', ageSeconds: 32, timeoutSeconds: 600 }] : [],
      recent: [{ id: 'mac-20260926-181400-bbbbbbbbbbbb', state: 'success', model: 'gpt-oss-20b', client: 'codex', lane: 'fast', predictedPerSecond: 104, elapsedSeconds: 1.2, ageSeconds: 40 }], lastSuccess: null },
    activity: { runs: [ORB_RUN] } };
}
function graphFor({ width = 1150, height = 690, job = false, view = 'runtime' } = {}) {
  const script = `${orbAppSource}\nsnapshot=S;connected=true;view=V;makeGraph();({nodes:graph.nodes,edges:graph.edges})`;
  return JSON.parse(JSON.stringify(runInNewContext(script, { ...layoutModule, S: orbSnapshot(job), V: view, Date, Set, Map, Math, Number, String, Array, Object, JSON,
    window: { innerWidth: width, matchMedia: () => ({ matches: false }) }, document: { getElementById: id => id === 'graphRegion' ? { clientWidth: width, clientHeight: height } : null } })));
}
const ORB_SIZES = [[1150, 690], [820, 604], [640, 505], [560, 900]];
const orbNumbers = d => (d.match(/-?\d+(?:\.\d+)?(?:e-?\d+)?/g) || []).map(Number);
const commands = d => d.replace(/[^A-Za-z]/g, '');
const pathOf = (web, key) => web.paths.find(p => p.key === key)?.d;
// Quadratic segments of a ring path: [start, control, end] triples.
const ringQuads = d => { const n = orbNumbers(d), out = []; let [x, y] = n; for (let i = 2; i + 3 < n.length + 1; i += 4) { out.push([[x, y], [n[i], n[i + 1]], [n[i + 2], n[i + 3]]]); [x, y] = [n[i + 2], n[i + 3]]; } return out; };
const ORB_DEG = 180 / Math.PI;
const finiteDeep = value => typeof value === 'number' ? Number.isFinite(value) : value instanceof Map ? [...value.values()].every(finiteDeep)
  : Array.isArray(value) ? value.every(finiteDeep) : value && typeof value === 'object' ? Object.values(value).every(finiteDeep) : true;

test('orb web: three capture rings around the Mac and the PC, one tight ring around each client branch and the route; rings, faint fillers, then spokes', () => {
  for (const [width, height] of ORB_SIZES) {
    const g = graphFor({ width, height }), web = orbWebLayout(g.nodes, g.edges);
    assert.deepEqual(web.hubs.map(h => h.kind).sort(), ['client', 'client', 'client', 'client', 'client', 'pipeline', 'runtime', 'windows-worker'].sort(), `${width}x${height}`);
    assert.equal(web.primary, 'runtime');
    for (const hub of web.hubs) {
      const count = hub.major ? 3 : 1, minor = hub.major ? '' : ' web-minor';
      assert.equal(hub.major, ['runtime', 'windows-worker'].includes(hub.kind));
      assert.equal(hub.radii.length, count);
      assert.ok(hub.radii.every((r, i) => i === 0 || r > hub.radii[i - 1]) && hub.radii[0] > g.nodes.find(n => n.id === hub.id).r + 9, 'rings clear the halo and grow outward');
      assert.deepEqual(web.paths.filter(p => p.key.startsWith(`ring:${hub.id}:`)).map(p => p.className), (hub.major ? ['ring-in', 'ring-mid', 'ring-out'] : ['ring-in']).map(c => `web-ring ${c}${minor}`));
      for (let level = 0; level < count; level++) assert.equal((pathOf(web, `ring:${hub.id}:${level}`).match(/Q/g) || []).length, hub.spokes.length, 'one scallop per spoke gap');
      assert.equal((pathOf(web, `spokes:${hub.id}`) || '').split('M').length - 1, hub.spokes.filter(s => !s.filler && s.to).length);
      assert.equal((pathOf(web, `fillers:${hub.id}`) || '').split('M').length - 1, hub.spokes.filter(s => s.filler && s.to).length);
      for (const key of [`spokes:${hub.id}`, `fillers:${hub.id}`]) { const path = web.paths.find(p => p.key === key); if (path) assert.equal(path.className.endsWith(' web-minor'), !hub.major, key); }
    }
    assert.equal(web.pathCount, web.paths.length);
    assert.equal(new Set(web.paths.map(p => p.key)).size, web.paths.length, 'path keys are unique');
    assert.ok(web.pathCount <= web.hubs.reduce((n, hub) => n + 2 + hub.radii.length, 0) + web.bridges.length);
  }
});

test('orb web: every capture-ring segment sags toward its hub, by 10% of its chord (6% on the small webs)', () => {
  const g = graphFor(), web = orbWebLayout(g.nodes, g.edges);
  for (const hub of web.hubs) for (let level = 0; level < hub.radii.length; level++) for (const [p0, c, p1] of ringQuads(pathOf(web, `ring:${hub.id}:${level}`))) {
    const ratio = hub.major ? .1 : .06, chord = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]), mid = [(p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2];
    const curve = [(p0[0] + 2 * c[0] + p1[0]) / 4, (p0[1] + 2 * c[1] + p1[1]) / 4];
    const inward = Math.hypot(mid[0] - hub.x, mid[1] - hub.y) - Math.hypot(curve[0] - hub.x, curve[1] - hub.y);
    assert.equal(hub.scallop, ratio);
    assert.ok(Math.abs(inward - ratio * chord) < .25, `${hub.id}: sag ${inward.toFixed(2)} vs chord ${chord.toFixed(2)}`);
  }
});

test('orb web: a spoke toward every child; fillers close every gap wider than 45 degrees (60 on the small webs) and only those gaps', () => {
  for (const [width, height] of ORB_SIZES) {
    const g = graphFor({ width, height, job: true }), web = orbWebLayout(g.nodes, g.edges);
    for (const hub of web.hubs) {
      const limit = hub.major ? 45 : 60, angles = hub.spokes.map(s => s.angle), real = hub.spokes.filter(s => !s.filler).map(s => s.angle);
      const gaps = list => list.map((a, i) => ((list[(i + 1) % list.length] - a) * ORB_DEG + 360) % 360 || 360);
      assert.ok(Math.max(...gaps(angles)) <= limit + 1e-6, `${hub.id} ${width}x${height} gap ${Math.max(...gaps(angles))}`);
      const needed = real.length ? gaps(real).reduce((n, gap) => n + Math.ceil(gap / limit - 1e-9) - 1, 0) : Math.ceil(360 / limit);
      assert.equal(angles.length - real.length, needed, `${hub.id}: fillers only where a real gap exceeds ${limit} degrees`);
      // Every child (the first hub that links to it owns it) and a child hub's parent sit on a spoke. It reaches the far
      // rim under a solid data edge; under a dashed edge, and back to the parent hub, it stops at the first ring.
      const children = g.edges.filter(e => e.a === hub.id && g.edges.find(f => f.b === e.b && web.hubs.some(h => h.id === f.a)).a === hub.id).map(e => e.b);
      if (hub.parent) children.push(hub.parent);
      for (const id of children) {
        const child = g.nodes.find(n => n.id === id), spoke = hub.spokes.find(s => s.targets.includes(id));
        assert.ok(spoke && !spoke.filler, `${hub.id} has a spoke to ${id}`);
        const far = g.nodes.find(n => n.id === spoke.target), reach = Math.hypot(spoke.to[0] - hub.x, spoke.to[1] - hub.y), rim = Math.hypot(far.x - hub.x, far.y - hub.y) - far.r - 1;
        const near = spoke.targets.map(t => g.nodes.find(n => n.id === t)).reduce((a, b) => Math.hypot(b.x - hub.x, b.y - hub.y) < Math.hypot(a.x - hub.x, a.y - hub.y) ? b : a);
        const edge = g.edges.find(e => e.a === hub.id && e.b === near.id), dashed = spoke.back || Boolean(edge && (edge.dim || edge.flow));
        assert.equal(spoke.back, id === hub.parent || spoke.targets.includes(hub.parent));
        if (dashed) assert.ok(Math.abs(reach - Math.min(rim, hub.radii[0])) < 1e-6, `${hub.id} > ${id}: stops at the first ring`);
        else assert.ok(Math.abs(reach - rim) < 1e-6 && reach >= Math.hypot(child.x - hub.x, child.y - hub.y) - child.r - 1.001, `${hub.id} > ${id}: reaches its child`);
      }
    }
  }
});

test('orb web: beyond a hub\'s first ring no thread lies under a dashed edge, and no spoke is drawn twice', () => {
  const along = (a, b, p) => ((p[0] - a.x) * (b.x - a.x) + (p[1] - a.y) * (b.y - a.y)) / Math.hypot(b.x - a.x, b.y - a.y);
  const off = (a, b, p) => Math.abs((p[0] - a.x) * (b.y - a.y) - (p[1] - a.y) * (b.x - a.x)) / Math.hypot(b.x - a.x, b.y - a.y);
  let checked = 0;
  for (const job of [false, true]) for (const [width, height] of ORB_SIZES) {
    const g = graphFor({ width, height, job }), web = orbWebLayout(g.nodes, g.edges), byId = new Map(g.nodes.map(n => [n.id, n]));
    g.edges.forEach((edge, i) => {
      if (!(edge.dim || edge.flow) || web.edgeBends[i] !== 0) return;
      const a = byId.get(edge.a), b = byId.get(edge.b), length = Math.hypot(b.x - a.x, b.y - a.y);
      for (const hub of web.hubs) for (const spoke of hub.spokes) {
        if (!spoke.to || off(a, b, spoke.from) > .5 || off(a, b, spoke.to) > .5) continue;
        const [s0, s1] = [along(a, b, spoke.from), along(a, b, spoke.to)];
        if (Math.max(s0, s1) < 0 || Math.min(s0, s1) > length) continue;
        checked++;
        assert.ok(Math.hypot(spoke.to[0] - hub.x, spoke.to[1] - hub.y) <= hub.radii[0] + 1e-6, `${width}x${height}: ${hub.id}'s spoke runs under the dashed ${edge.a} > ${edge.b}`);
      }
    });
    for (const hub of web.hubs) for (const spoke of hub.spokes) if (spoke.back && spoke.to) assert.ok(Math.hypot(spoke.to[0] - hub.x, spoke.to[1] - hub.y) <= hub.radii[0] + 1e-6, `${hub.id}: the spoke back to ${hub.parent} is a stub`);
  }
  assert.ok(checked > 40, `dashed edges checked: ${checked}`);
  // Under a solid edge the spoke still runs the whole way: the PC's idle fast lane (the busy deep lane's edge is dashed).
  const g = graphFor({ job: true }), web = orbWebLayout(g.nodes, g.edges), pc = web.hubs.find(h => h.id === 'windows-worker');
  const reach = id => { const s = pc.spokes.find(sp => sp.targets.includes(id)); return Math.hypot(s.to[0] - pc.x, s.to[1] - pc.y); }, lane = g.nodes.find(n => n.id === 'windows-lane:fast');
  assert.ok(Math.abs(reach('windows-lane:fast') - (Math.hypot(lane.x - pc.x, lane.y - pc.y) - lane.r - 1)) < 1e-6);
  assert.ok(Math.abs(reach('windows-lane:deep') - pc.radii[0]) < 1e-6);
});

test('orb web: a client branch\'s small web ends above the name hanging under its star', () => {
  for (const [width, height] of ORB_SIZES) for (const job of [false, true]) {
    const g = graphFor({ width, height, job }), web = orbWebLayout(g.nodes, g.edges);
    const clients = web.hubs.filter(h => h.kind === 'client');
    assert.equal(clients.length, 5);
    for (const hub of clients) {
      // A client name is 13 px at scale 1 (applyCamera), its baseline nodeCaptionLayout's label.y; capitals rise about .75 em.
      const node = g.nodes.find(n => n.id === hub.id), top = nodeCaptionLayout(node, 1).label.y - .75 * 13;
      const lowest = Math.max(hub.radii.at(-1), ...hub.spokes.filter(s => s.to).map(s => s.to[1] - hub.y));
      assert.ok(lowest < top, `${hub.id}: its web reaches ${lowest.toFixed(1)} below the star, its name starts at ${top.toFixed(1)}`);
    }
  }
});

test('orb web: hubs with no, one and two children still read as whole webs', () => {
  const hub = { id: 'h', kind: 'windows-worker', x: 0, y: 0, r: 12 };
  const none = orbWebLayout([hub], []).hubs[0];
  assert.deepEqual([none.spokes.length, none.spokes.filter(s => s.filler).length], [8, 8]);
  assert.ok(none.spokes.every(s => s.to && Math.abs(Math.hypot(...s.to) - none.outer * 1.12) < 1e-9), 'fillers run just past the outer ring');
  const one = orbWebLayout([hub, { id: 'a', kind: 'windows-lane', x: 200, y: 0, r: 7 }], [{ a: 'h', b: 'a' }]).hubs[0];
  assert.deepEqual([one.spokes.length, one.spokes.filter(s => !s.filler).map(s => s.target)], [8, ['a']]);
  const two = orbWebLayout([hub, { id: 'a', kind: 'windows-lane', x: 200, y: 0, r: 7 }, { id: 'b', kind: 'windows-lane', x: -200, y: 0, r: 7 }], [{ a: 'h', b: 'a' }, { a: 'h', b: 'b' }]);
  assert.deepEqual([two.hubs[0].spokes.length, two.hubs[0].spokes.filter(s => s.filler).length], [8, 6]);
  assert.deepEqual(two.edgeBends, [0, 0]);
  // Two children in one direction share a spoke to the farther; the nearer runs straight, the farther keeps its bend.
  const row = orbWebLayout([hub, { id: 'n', kind: 'model', x: 80, y: 0, r: 8 }, { id: 'f', kind: 'model', x: 180, y: 0, r: 8 }], [{ a: 'h', b: 'n' }, { a: 'h', b: 'f' }]);
  assert.deepEqual(row.hubs[0].spokes.filter(s => !s.filler).map(s => [s.target, s.targets]), [['f', ['n', 'f']]]);
  assert.deepEqual(row.edgeBends, [0, null]);
});

test('orb web: neighbouring webs never overlap, at every viewport the map fits', () => {
  for (const [width, height] of ORB_SIZES) for (const view of ['runtime', 'runs']) {
    const g = graphFor({ width, height, view }), web = orbWebLayout(g.nodes, g.edges);
    for (const a of web.hubs) for (const b of web.hubs) if (a.id < b.id) assert.ok(a.outer + b.outer < Math.hypot(a.x - b.x, a.y - b.y), `${a.id} / ${b.id} at ${width}x${height}`);
  }
  const close = orbWebLayout([{ id: 'runtime', kind: 'runtime', x: 0, y: 0, r: 18 }, { id: 'pc', kind: 'windows-worker', x: 150, y: 0, r: 12 },
    { id: 'm', kind: 'model', x: -400, y: 0, r: 10 }, { id: 'lane', kind: 'windows-lane', x: 150, y: 400, r: 7 }],
  [{ a: 'runtime', b: 'pc' }, { a: 'runtime', b: 'm' }, { a: 'pc', b: 'lane' }]);
  assert.ok(close.hubs[0].outer + close.hubs[1].outer < 150, JSON.stringify(close.hubs.map(h => h.outer)));
});

test('orb web: spoke edges draw straight; a sibling hidden behind a nearer star and client-to-lane links keep their bends', () => {
  const g = graphFor({ job: true }), web = orbWebLayout(g.nodes, g.edges);
  const bend = (a, b) => web.edgeBends[g.edges.findIndex(e => e.a === a && e.b === b)];
  for (const [a, b] of [['runtime', 'windows-worker'], ['windows-worker', 'windows-lane:deep'], ['runtime', 'pipeline'], ['runtime', 'afm']]) assert.equal(bend(a, b), 0, `${a} > ${b}`);
  assert.equal(bend('runtime', 'client:codex'), null, 'AFM is the nearer child on this spoke; the client edge keeps its bend');
  assert.equal(bend('client:claude', 'windows-lane:deep'), null);
  const models = g.nodes.filter(n => n.kind === 'model').sort((a, b) => Math.abs(a.x - g.nodes[0].x) - Math.abs(b.x - g.nodes[0].x));
  assert.deepEqual(models.map(m => bend('runtime', m.id)), [0, null]);
  assert.equal(web.edgeBends.length, g.edges.length);
});

test('orb web: no bridge threads by default; opted in, each hangs between two hub webs with a catenary sag, and none in the trace view', () => {
  for (const [width, height] of ORB_SIZES) {
    const g = graphFor({ width, height });
    assert.deepEqual(orbWebLayout(g.nodes, g.edges).bridges, [], 'every line beyond a hub web is evidence: no silk between unrelated nodes');
    assert.ok(!orbWebLayout(g.nodes, g.edges).paths.some(p => p.key.startsWith('bridge:')));
    const web = orbWebLayout(g.nodes, g.edges, { maxBridges: 2 }), hubs = new Set(web.hubs.map(h => h.id));
    assert.ok(web.bridges.length <= 2);
    for (const bridge of web.bridges) {
      assert.ok(hubs.has(bridge.from) && hubs.has(bridge.to), 'bridges join hub webs, never two leaves');
      const [ax, ay, cx, cy, bx, by] = orbNumbers(pathOf(web, `bridge:${web.bridges.indexOf(bridge)}`)), chord = Math.hypot(bx - ax, by - ay);
      assert.ok(Math.abs(cx - (ax + bx) / 2) <= .1 && Math.abs(cy - (ay + by) / 2 - 2 * .06 * chord) <= .2, `sag of ${bridge.from} to ${bridge.to}`);
      const from = web.hubs.find(h => h.id === bridge.from);
      assert.ok(Math.abs(Math.hypot(ax - from.x, ay - from.y) - from.outer) < .2, 'a bridge leaves from the outer ring');
    }
  }
  assert.equal(orbWebLayout(...Object.values(graphFor({ width: 1150, height: 690 })), { maxBridges: 2 }).bridges.length, 2);
  assert.equal(orbWebLayout(...Object.values(graphFor({ view: 'runs' })), { maxBridges: 2 }).bridges.length, 0, 'the trace view has one hub: no thread between two calls');
});

test('orb web: the trace view is one orb around the run with every call on its own straight spoke', () => {
  for (const [width, height] of ORB_SIZES) {
    const g = graphFor({ width, height, view: 'runs' }), web = orbWebLayout(g.nodes, g.edges);
    assert.equal(web.hubs.length, 1); assert.equal(web.primary, g.nodes[0].id); assert.equal(web.hubs[0].radii.length, 3);
    const calls = g.nodes.filter(n => n.kind === 'call' && !n.id.endsWith(':decision'));
    assert.deepEqual(web.hubs[0].spokes.filter(s => !s.filler).map(s => s.target).sort(), calls.map(c => c.id).sort());
    assert.ok(web.hubs[0].spokes.some(s => s.filler), 'the open side of the arc is filled');
    g.edges.forEach((e, i) => assert.equal(web.edgeBends[i], e.a === web.primary ? 0 : null));
  }
});

test('orb web: deterministic, and only geometry or topology changes the cache key', () => {
  const g = graphFor({ job: true });
  assert.deepEqual(orbWebLayout(g.nodes, g.edges), orbWebLayout(structuredClone(g.nodes), structuredClone(g.edges)));
  const status = structuredClone(g.nodes).map(n => ({ ...n, subtitle: 'CHANGED', active: !n.active, inFlight: false, color: '#000', label: 'x' }));
  assert.equal(orbWebKey(status, g.edges), orbWebKey(g.nodes, g.edges), 'status, colour and captions do not rebuild the web');
  assert.equal(orbWebKey(graphFor({ job: false }).nodes, []), orbWebKey(g.nodes, []), 'a job moves no star (its client-to-lane link is a topology change)');
  const moved = structuredClone(g.nodes); moved[3].x += 1;
  assert.notEqual(orbWebKey(moved, g.edges), orbWebKey(g.nodes, g.edges));
  const dashed = structuredClone(g.edges); dashed[0].dim = !dashed[0].dim;
  assert.notEqual(orbWebKey(g.nodes, dashed), orbWebKey(g.nodes, g.edges), 'a solid edge turning dashed shortens its spoke');
  assert.notEqual(orbWebKey(g.nodes, g.edges.slice(1)), orbWebKey(g.nodes, g.edges));
});

test('orb web: bounded and finite on hostile input, motion included', () => {
  let seed = 7; const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const kinds = ['runtime', 'run', 'windows-worker', 'pipeline', 'client', 'model', 'windows-lane'];
  const nodes = Array.from({ length: 80 }, (_, i) => ({ id: `n${i % 70}`, kind: kinds[i % kinds.length], x: rand() * 900, y: rand() * 700, r: i % 9 ? rand() * 30 : NaN, active: i % 5 === 0, color: i % 2 ? 'red; x' : '#abc' }));
  nodes.push({ id: 'bad1', kind: 'runtime', x: NaN, y: 1 }, { id: 'bad2', kind: 'client', x: Infinity, y: 0 }, { id: 'bad3', kind: 'pipeline', x: 1e9, y: 0 }, null, { kind: 'client', x: 1, y: 1 });
  nodes.push({ id: 'twin-a', kind: 'client', x: 50, y: 50, r: 9 }, { id: 'twin-b', kind: 'client', x: 50, y: 50, r: 9 });
  const edges = Array.from({ length: 200 }, (_, i) => ({ a: `n${i % 70}`, b: `n${(i * 7 + 3) % 75}` }));
  edges.push({ a: 'n1', b: 'n1' }, { a: 'n1', b: 'missing' }, null, { a: 'bad1', b: 'n2' }, { a: 'twin-a', b: 'twin-b' });
  for (const options of [{}, { ringsMajor: NaN, ringsMinor: 99, scallop: Infinity, scallopMinor: -1, fillerGap: -5, fillerGapMinor: NaN, minorRing: 1e9, merge: 'x', fillerReach: -1, bridgeSag: 9, maxBridges: 1e9, bridgeMin: NaN }]) {
    const web = orbWebLayout(nodes, edges, options);
    assert.ok(web.hubs.length <= ORB_WEB_LIMITS.hubs && web.bridges.length <= ORB_WEB_LIMITS.bridges);
    assert.ok(web.pathCount <= ORB_WEB_LIMITS.hubs * (2 + ORB_WEB_LIMITS.rings) + ORB_WEB_LIMITS.bridges);
    for (const hub of web.hubs) assert.ok(hub.spokes.length <= ORB_WEB_LIMITS.spokesPerHub && hub.radii.length <= ORB_WEB_LIMITS.rings);
    for (const path of web.paths) assert.match(path.d, /^[MQLZ0-9 .-]*$/, path.key);
    assert.ok(finiteDeep(web));
    const plan = orbWebPluck(web, nodes, edges, nodes.filter(Boolean).map(n => n.id), { amplitude: 1e9, hops: 99 });
    assert.ok(plan.strands.length <= ORB_WEB_LIMITS.strands && finiteDeep(plan) && plan.strands.every(s => s.amplitude <= 2 * WEB_PLUCK.amplitude));
    for (let t = 0; t <= plan.duration * 1000 + 50; t += 50) {
      const offsets = pluckOffsets([{ plan, start: 0 }], t);
      assert.ok(finiteDeep(offsets));
      for (const d of orbWebPathsAt(web, offsets).values()) assert.doesNotMatch(d, /NaN|Infinity|undefined/);
    }
    const activity = orbWebActivity(web, { live: nodes.filter(Boolean).map(n => n.id), motion: false });
    assert.ok(activity.dew.length <= ORB_WEB_LIMITS.dew && finiteDeep(activity) && !/NaN|Infinity/.test(activity.key));
    assert.deepEqual(orbWebActivity(web, { live: ['n0'], motion: true }), { dew: [], dewPath: '', key: '' });
    assert.ok(webGlows(nodes, { fresh: true }).glows.length <= ORB_WEB_LIMITS.glows);
    assert.doesNotMatch(orbWebFlashPath(web, plan), /NaN|Infinity|undefined/);
  }
  assert.deepEqual(orbWebLayout(null, undefined), { primary: null, hubs: [], bridges: [], edgeBends: [], paths: [], pathCount: 0 });
  assert.deepEqual(orbWebActivity(null, null), { dew: [], dewPath: '', key: '' });
  assert.deepEqual(orbWebPluck(null, null, null, 'x').strands, []);
  assert.equal(orbWebPathsAt(null, pluckOffsets(null, 0)).size, 0);
});

test('orb web activity: nothing travels along a thread; under reduced motion static dew marks the live threads only', () => {
  const g = graphFor({ job: true }), web = orbWebLayout(g.nodes, g.edges);
  const live = g.nodes.filter(n => n.active || n.inFlight).map(n => n.id);
  assert.deepEqual(orbWebActivity(web, { live, motion: true }), { dew: [], dewPath: '', key: '' }, 'no drop and no dew with motion: the halo, glow and heartbeat carry a live job');
  const still = orbWebActivity(web, { live, motion: false });
  assert.equal(still.key, still.dewPath);
  assert.match(still.dewPath, /^(M-?[\d.]+ -?[\d.]+h\.01)+$/, 'each dot is a near-zero-length segment, not h0');
  const slots = web.hubs.flatMap(h => h.spokes.filter(sp => !sp.filler).map(sp => ({ hub: h.id, targets: sp.targets, x: h.x + Math.cos(sp.angle) * h.outer, y: h.y + Math.sin(sp.angle) * h.outer })));
  const marked = still.dew.map(([x, y]) => slots.find(slot => Math.abs(slot.x - x) < 1e-9 && Math.abs(slot.y - y) < 1e-9));
  const has = (hub, id) => marked.some(slot => slot.hub === hub && slot.targets.includes(id)), gemma = g.nodes.find(n => n.kind === 'model' && n.active).id;
  assert.equal(marked.length, 4);
  assert.ok(has('runtime', gemma) && has('runtime', 'windows-worker') && has('windows-worker', 'runtime') && has('windows-worker', 'windows-lane:deep'), JSON.stringify(marked));
  assert.ok(!marked.some(slot => slot.targets.includes('windows-lane:fast')), 'an idle lane never gets dew');
  const idle = graphFor(), idleWeb = orbWebLayout(idle.nodes, idle.edges);
  for (const motion of [true, false]) assert.deepEqual(orbWebActivity(idleWeb, { live: idle.nodes.filter(n => n.active || n.inFlight).map(n => n.id), motion }), { dew: [], dewPath: '', key: '' });
});

test('pluck wave: damped 3 Hz wobble, bounded by its amplitude, exactly 0 before the pluck and from its end on', () => {
  const values = Array.from({ length: 241 }, (_, i) => pluckWave(i / 200, 6));
  assert.equal(pluckWave(0, 6), 0); assert.equal(pluckWave(-1, 6), 0); assert.equal(pluckWave(WEB_PLUCK.duration, 6), 0); assert.equal(pluckWave(5, 6), 0);
  assert.ok(values.every(v => Math.abs(v) <= 6));
  // Peaks of successive half cycles shrink (damping); the first comes a quarter period in.
  const peaks = []; for (let k = 0; k < 7; k++) { const from = Math.round(k / 6 * 200), to = Math.round((k + 1) / 6 * 200); peaks.push(Math.max(...values.slice(from, to).map(Math.abs))); }
  assert.ok(peaks.every((p, i) => i === 0 || p < peaks[i - 1]), JSON.stringify(peaks));
  assert.ok(peaks[0] > 3.5 && peaks[5] < .5);
  const signs = values.map(Math.sign).filter(Boolean); let flips = 0; for (let i = 1; i < signs.length; i++) if (signs[i] !== signs[i - 1]) flips++;
  assert.ok(flips >= 6 && flips <= 7, `about 3.6 cycles in 1.2 s at 3 Hz: ${flips} zero crossings`);
  assert.ok(Math.abs(pluckWave(WEB_PLUCK.duration - .001, 6)) < 1e-4, 'the taper lands on rest without a step');
  const beat = { frequency: WEB_PLUCK.frequency, tau: WEB_PLUCK.beat.tau, duration: WEB_PLUCK.beat.duration };
  assert.equal(pluckWave(beat.duration, 2, beat), 0);
  for (const bad of [NaN, Infinity, undefined]) assert.equal(pluckWave(bad, 6), 0), assert.equal(pluckWave(.1, bad), 0);
});

test('poke scale: the star dips to .92, springs to 1.04 and rests at 1 by half a second', () => {
  const s = Array.from({ length: 61 }, (_, i) => pokeScale(i / 100));
  assert.equal(pokeScale(0), 1); assert.equal(pokeScale(.5), 1); assert.equal(pokeScale(2), 1); assert.equal(pokeScale(NaN), 1);
  assert.ok(Math.abs(Math.min(...s) - .92) < 1e-9 && Math.abs(Math.max(...s) - 1.04) < 1e-9);
  assert.ok(s.indexOf(Math.min(...s)) < s.indexOf(Math.max(...s)), 'dip first, then overshoot');
});

test('pluck plan: hop 0 is every thread on the poked star, the wave reaches rings and far ends 90 ms later at 45%, then 20%', () => {
  const g = graphFor({ job: true }), web = orbWebLayout(g.nodes, g.edges), mac = web.hubs.find(h => h.id === 'runtime');
  const plan = orbWebPluck(web, g.nodes, g.edges, ['runtime']);
  assert.equal(plan.dip, 'runtime'); assert.equal(plan.beat, false);
  const at = hop => plan.strands.filter(s => s.hop === hop);
  // Hop 0: all the Mac's spokes and fillers, every edge on the Mac, and the child hubs' spokes back to it.
  assert.deepEqual(at(0).filter(s => s.kind === 'spoke' && s.hub === 'runtime').map(s => s.index).sort((a, b) => a - b), mac.spokes.map((s, i) => s.to ? i : -1).filter(i => i >= 0));
  assert.deepEqual(at(0).filter(s => s.kind === 'edge').map(s => s.index).sort((a, b) => a - b), g.edges.map((e, i) => e.a === 'runtime' || e.b === 'runtime' ? i : -1).filter(i => i >= 0));
  const pc = web.hubs.find(h => h.id === 'windows-worker'), back = pc.spokes.findIndex(s => s.targets.includes('runtime'));
  assert.ok(at(0).some(s => s.kind === 'spoke' && s.hub === 'windows-worker' && s.index === back));
  assert.ok(at(0).every(s => s.delay === 0 && s.kind !== 'ring'));
  // Hop 1: the Mac's rings (inner first) and the PC's other spokes; hop 2: the PC's rings. Nothing further.
  assert.deepEqual(at(1).filter(s => s.kind === 'ring' && s.hub === 'runtime').map(s => [s.level, s.delay]), [[0, .09], [1, .125], [2, .16]]);
  assert.ok(at(1).some(s => s.kind === 'spoke' && s.hub === 'windows-worker' && s.index !== back));
  assert.ok(at(2).some(s => s.kind === 'ring' && s.hub === 'windows-worker') && at(2).every(s => s.delay >= .18));
  assert.ok(plan.strands.every(s => s.hop <= 2));
  assert.ok(plan.strands.filter(s => s.kind !== 'ring').every(s => s.delay === [0, .09, .18][s.hop]), 'each hop starts 90 ms after the one before');
  const peak = hop => Math.max(...at(hop).filter(s => s.kind !== 'ring').map(s => s.amplitude));
  assert.ok(peak(0) <= WEB_PLUCK.amplitude + 1e-9 && peak(1) <= .45 * WEB_PLUCK.amplitude + 1e-9 && peak(2) <= .2 * WEB_PLUCK.amplitude + 1e-9);
  for (const hub of web.hubs) { const gap = Math.min(hub.inner - hub.r, ...hub.radii.slice(1).map((r, i) => r - hub.radii[i]));
    assert.ok(plan.strands.filter(s => s.kind === 'ring' && s.hub === hub.id).every(s => s.amplitude <= .4 * gap + 1e-9), 'rings never swing into each other'); }
  assert.ok(Math.abs(plan.duration - (Math.max(...plan.strands.map(s => s.delay)) + WEB_PLUCK.duration)) < 1e-9);
  // A leaf: its spoke and its edges first, then its hub's web and the client that sent the job.
  const leaf = orbWebPluck(web, g.nodes, g.edges, ['windows-lane:deep']);
  assert.deepEqual(leaf.strands.filter(s => s.hop === 0).map(s => s.kind === 'edge' ? `${g.edges[s.index].a}>${g.edges[s.index].b}` : `${s.hub}#${pc.spokes[s.index].target}`).sort(),
    ['client:claude>windows-lane:deep', 'windows-worker#windows-lane:deep', 'windows-worker>windows-lane:deep'].sort());
  assert.ok(leaf.strands.some(s => s.kind === 'ring' && s.hub === 'windows-worker' && s.hop === 2));
  assert.ok(!leaf.strands.some(s => s.kind === 'ring' && s.hub === 'runtime'), 'three hops away: the Mac web stays still');
});

test('beat plan: a heartbeat moves only the working nodes\' own threads, at a third of a poke, shorter, without a dip', () => {
  const g = graphFor({ job: true }), web = orbWebLayout(g.nodes, g.edges);
  const ids = g.nodes.filter(n => n.active || n.inFlight).map(n => n.id), beat = orbWebPluck(web, g.nodes, g.edges, ids, { beat: true });
  assert.ok(ids.includes('windows-worker') && ids.includes('windows-lane:deep'));
  assert.equal(beat.dip, null); assert.equal(beat.wave.duration, WEB_PLUCK.beat.duration);
  assert.ok(beat.strands.length && beat.strands.every(s => s.hop === 0 && s.delay === 0 && s.kind !== 'ring' && s.amplitude <= WEB_PLUCK.amplitude / 3 + 1e-9));
  assert.ok(beat.strands.filter(s => s.kind === 'edge').every(s => ids.includes(g.edges[s.index].a) || ids.includes(g.edges[s.index].b)));
  assert.ok(!beat.strands.some(s => s.kind === 'edge' && g.edges[s.index].b === 'client:codex'), 'an idle client branch stays still');
  assert.deepEqual(orbWebPluck(web, g.nodes, g.edges, [], { beat: true }).strands, []);
});

test('pluck keyframes: every frame keeps each path\'s commands, the web returns to rest exactly, and hops start on time', () => {
  const g = graphFor({ job: true }), web = orbWebLayout(g.nodes, g.edges), rest = new Map(web.paths.map(p => [p.key, p.d]));
  const plan = orbWebPluck(web, g.nodes, g.edges, ['runtime']), start = 5000, frames = [];
  for (let t = start - 50; t <= start + plan.duration * 1000 + 100; t += 1000 / 60) frames.push([t, pluckOffsets([{ plan, start }], t)]);
  let moved = 0;
  for (const [t, offsets] of frames) {
    const paths = orbWebPathsAt(web, offsets);
    for (const [key, d] of paths) { assert.equal(commands(d), commands(rest.get(key)), `${key} keeps its command structure`); moved++; }
    if (t <= start || t >= start + plan.duration * 1000) assert.deepEqual([paths.size, offsets.edges.size, offsets.scale.size], [0, 0, 0], `rest at ${t - start} ms`);
    // Hop delays: nothing beyond hop 0 moves in the first 90 ms, no hop-2 thread before 180 ms.
    const elapsed = (t - start) / 1000;
    if (elapsed < .09) assert.ok(![...paths.keys()].some(k => k.startsWith('ring:') || k === 'fillers:windows-worker'), 'rings and the far hub\'s threads wait for the wave');
    if (elapsed < .18) assert.ok(!paths.has('ring:windows-worker:0'));
    for (const value of offsets.edges.values()) assert.ok(Math.abs(value) <= WEB_PLUCK.amplitude + 1e-9);
  }
  assert.ok(moved > 200);
  assert.equal(pluckOffsets([{ plan, start }], start + plan.duration * 1000).active, false);
  assert.equal(pluckOffsets([{ plan, start }], start + 10).active, true);
  // A vanishing displacement renders exactly the rest string: the moving and resting paths come from one builder.
  const hair = { spokes: new Map([['runtime', new Map(web.hubs[0].spokes.map((_, i) => [i, 1e-12]))]]), rings: new Map([['runtime:1', 1e-12]]), bridges: new Map([[0, 1e-12]]) };
  for (const [key, d] of orbWebPathsAt(web, hair)) assert.equal(d, rest.get(key), key);
  // Two impulses on one thread add up (a poke landing on a heartbeat), still bounded by their sum.
  const beat = orbWebPluck(web, g.nodes, g.edges, ['windows-worker'], { beat: true }), both = pluckOffsets([{ plan, start }, { plan: beat, start }], start + 80);
  const edge = g.edges.findIndex(e => e.a === 'runtime' && e.b === 'windows-worker');
  assert.ok(Math.abs(both.edges.get(edge) - pluckOffsets([{ plan, start }], start + 80).edges.get(edge) - pluckOffsets([{ plan: beat, start }], start + 80).edges.get(edge)) < 1e-9);
  assert.equal(pluckOffsets([{ plan, start }], start + 90).scale.get('runtime') < 1, true, 'the poked star is dipping');
  // The PC's stub back to the Mac lies on the line of the Mac's stub to the PC: mid-pluck both bow to the same side, as one thread.
  const mid = orbWebPathsAt(web, pluckOffsets([{ plan, start }], start + 60));
  const control = (key, hubId, target) => { const hub = web.hubs.find(h => h.id === hubId), spoke = hub.spokes.find(s => s.targets.includes(target) && !s.filler);
    const seg = mid.get(key).split('M').find(part => part.startsWith(`${orbNumbers(`M${spoke.from.map(v => Math.round(v * 10) / 10).join(' ')}`).join(' ')}Q`));
    const [x0, y0, cx, cy, x1, y1] = orbNumbers(seg); return [cx - (x0 + x1) / 2, cy - (y0 + y1) / 2]; };
  const macSide = control('spokes:runtime', 'runtime', 'windows-worker'), pcSide = control('spokes:windows-worker', 'windows-worker', 'runtime');
  assert.ok(Math.hypot(...macSide) > .3 && macSide[0] * pcSide[0] + macSide[1] * pcSide[1] > 0, `${macSide} vs ${pcSide}`);
  assert.ok(web.hubs.find(h => h.id === 'windows-worker').spokes.some(s => s.back) && !web.hubs.find(h => h.id === 'runtime').spokes.some(s => s.back));
});

test('edge curve path: unmoved it is the path renderGraph always drew; moved, only the control point shifts, perpendicular', () => {
  const g = { sx: 10, sy: 20, cx: 60, cy: 20, ex: 110, ey: 20 };
  assert.equal(edgeCurvePath(g), 'M10 20 Q60 20 110 20');
  assert.equal(edgeCurvePath(g, 0), edgeCurvePath(g)); assert.equal(edgeCurvePath(g, NaN), edgeCurvePath(g));
  assert.equal(edgeCurvePath(g, 3), 'M10 20 Q60 26 110 20');
  assert.equal(commands(edgeCurvePath({ sx: 1.5, sy: -2, cx: 7, cy: 8, ex: 30, ey: 40 }, -2.25)), 'MQ');
});

test('web motion loop: nothing at rest, a poke restarts instead of stacking, and heartbeats land on 2 s boundaries', () => {
  let time = 1234, nextId = 1; const frames = new Map(), timers = new Map(), draws = [];
  const loop = createWebMotion({ now: () => time, requestFrame: f => { const id = nextId++; frames.set(id, f); return id; }, cancelFrame: id => frames.delete(id),
    setTimer: (f, ms) => { const id = nextId++; timers.set(id, { f, at: time + ms }); return id; }, clearTimer: id => timers.delete(id), draw: state => draws.push(state) });
  const runFrames = until => { while (frames.size && time < until) { time += 1000 / 60; const [id, f] = frames.entries().next().value; frames.delete(id); f(time); } };
  const g = graphFor({ job: true }), web = orbWebLayout(g.nodes, g.edges), plan = orbWebPluck(web, g.nodes, g.edges, ['runtime']);
  assert.deepEqual([frames.size, timers.size, draws.length], [0, 0, 0], 'nothing scheduled at rest');
  loop.pulse(null); assert.equal(timers.size, 0);
  loop.poke(plan); assert.equal(frames.size, 1);
  runFrames(time + 300); loop.poke(plan); loop.poke(plan);
  assert.equal(frames.size, 1, 'repeated pokes share the one frame loop');
  const second = time;
  runFrames(time + 5000);
  assert.equal(frames.size, 0, 'the loop stops once settled');
  assert.equal(draws.at(-1), null, 'its last frame draws rest');
  const lastLive = draws.filter(Boolean).at(-1);
  assert.ok(lastLive.time - second < plan.duration * 1000 && lastLive.time - second > plan.duration * 1000 - 40, 'the second poke restarted the wobble');
  assert.equal(draws.filter(Boolean).every(s => s.impulses.length === 1), true, 'never two pokes at once');
  // Heartbeat: armed for the next 2 s boundary on the shared clock, runs its short wobble, then waits for the next boundary.
  const beatPlan = orbWebPluck(web, g.nodes, g.edges, ['windows-worker'], { beat: true });
  time = 10500; loop.pulse(beatPlan); loop.pulse(beatPlan);
  assert.deepEqual([timers.size, [...timers.values()][0].at], [1, 12000], 'one timer, at the next boundary');
  const fire = () => { const [id, t] = [...timers.entries()][0]; timers.delete(id); time = t.at; t.f(); };
  fire();
  assert.equal(loop.state.beat, true); assert.equal(frames.size, 1); assert.equal([...timers.values()][0].at, 14000);
  draws.length = 0; runFrames(13900);
  assert.ok(draws.filter(Boolean).every(s => s.impulses[0].start === 12000));
  assert.equal(frames.size, 0, 'idle between beats'); assert.equal(draws.at(-1), null);
  loop.pulse(null); assert.equal(timers.size, 0, 'no pulsing node, no timer');
  loop.poke(plan); loop.pulse(beatPlan); loop.stop();
  assert.deepEqual([frames.size, timers.size, draws.at(-1), loop.state.poke, loop.state.pulse], [0, 0, null, false, false]);
});

test('web motion loop: the heartbeat alone draws at most 30 frames a second on a 120 Hz display; a poke draws every frame', () => {
  let time = 0, nextId = 1; const frames = new Map(), timers = new Map(), draws = [];
  const loop = createWebMotion({ now: () => time, requestFrame: f => { const id = nextId++; frames.set(id, f); return id; }, cancelFrame: id => frames.delete(id),
    setTimer: (f, ms) => { const id = nextId++; timers.set(id, { f, at: time + ms }); return id; }, clearTimer: id => timers.delete(id), draw: state => draws.push(state) });
  const run = (hz, until) => { let n = 0; while (frames.size && time < until) { time += 1000 / hz; const [id, f] = frames.entries().next().value; frames.delete(id); f(time); n++; } return n; };
  const g = graphFor({ job: true }), web = orbWebLayout(g.nodes, g.edges);
  loop.pulse(orbWebPluck(web, g.nodes, g.edges, ['windows-worker'], { beat: true }));
  const [id, timer] = [...timers.entries()][0]; timers.delete(id); time = timer.at; timer.f();
  const beatFrames = run(120, time + 2000), beatDraws = draws.filter(Boolean);
  assert.ok(beatFrames > 100, `the beat keeps one frame loop for its 0.9 s (${beatFrames} frames)`);
  assert.ok(beatDraws.length >= 25 && beatDraws.length <= 29, `about 27 draws at 30 fps: ${beatDraws.length}`);
  assert.ok(beatDraws.slice(1).every((s, i) => s.time - beatDraws[i].time >= 1000 / 30 - 2), 'never two draws closer than a 30 fps frame');
  assert.deepEqual([draws.at(-1), frames.size, loop.state.draws], [null, 0, beatDraws.length + 1], 'it still lands exactly on rest');
  draws.length = 0; loop.poke(orbWebPluck(web, g.nodes, g.edges, ['runtime']));
  assert.equal(draws.length, 0); const pokeFrames = run(120, time + 300);
  assert.equal(draws.filter(Boolean).length, pokeFrames, 'a poke draws every frame');
});

test('web glows: one per working or in-flight node on a fresh feed, green for work, the node colour in flight, radius clamped on zoom', () => {
  const g = graphFor({ job: true });
  assert.deepEqual(webGlows(g.nodes, { fresh: false }), { glows: [], key: '' });
  const { glows, key } = webGlows(g.nodes, { fresh: true });
  const want = g.nodes.filter(n => n.active || n.inFlight);
  assert.deepEqual(glows.map(x => x.id), want.map(n => n.id));
  const pc = glows.find(x => x.id === 'windows-worker'), lane = glows.find(x => x.id === 'windows-lane:deep');
  assert.deepEqual([pc.tone, pc.color], ['flight', g.nodes.find(n => n.id === 'windows-worker').color]);
  assert.deepEqual([lane.tone, lane.color], ['working', '#63d6ac']);
  assert.deepEqual(webGlows(graphFor({ job: false }).nodes, { fresh: true }).glows, [], 'an idle map has no glow');
  assert.equal(webGlows(g.nodes.map(n => ({ ...n, subtitle: 'x', label: 'y' })), { fresh: true }).key, key, 'captions do not rebuild the glows');
  assert.equal(webGlows([{ id: 'a', x: 0, y: 0, inFlight: true, color: 'url(javascript:x)' }], { fresh: true }).glows[0].color, '#63d6ac');
  assert.equal(glowRadius(.77), 110); assert.equal(glowRadius(1), 110); assert.equal(glowRadius(3), 50); assert.equal(glowRadius(1.7), 88.2);
  for (const bad of [0, -1, NaN, undefined]) assert.equal(glowRadius(bad), 110);
  assert.ok([.1, .5, 1, 2, 3, 30].every(z => glowRadius(z) * z <= 150 + 1e-9 || glowRadius(z) === 24), 'never more than 150 screen pixels');
});

test('poke flash: reduced motion or a paused feed lights only the poked star\'s own threads, at rest', () => {
  const g = graphFor({ job: true }), web = orbWebLayout(g.nodes, g.edges);
  const leaf = orbWebFlashPath(web, orbWebPluck(web, g.nodes, g.edges, ['windows-lane:deep']));
  assert.equal((leaf.match(/M/g) || []).length, 1, 'a lane lights its one spoke');
  assert.match(leaf, /^M[-\d. ]+L[-\d. ]+$/);
  // A client hub lights its whole little web, the Mac's spoke to it and a bridge landing on it.
  const own = web.hubs.flatMap(h => h.spokes.filter(s => s.to && (h.id === 'client:codex' || s.targets.includes('client:codex'))));
  assert.ok(own.some(s => s.targets.includes('client:codex')));
  assert.equal((orbWebFlashPath(web, orbWebPluck(web, g.nodes, g.edges, ['client:codex'])).match(/M/g) || []).length, own.length + web.bridges.filter(b => b.from === 'client:codex' || b.to === 'client:codex').length);
  assert.equal(orbWebFlashPath(web, null), '');
});
