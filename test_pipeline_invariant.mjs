import test from 'node:test';
import {resolvePython} from './test_support/python-interpreter.mjs';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {readFileSync, writeFileSync} from 'node:fs';
import {createInterface} from 'node:readline';
import {runInNewContext} from 'node:vm';
import * as layout from './web/map-layout.mjs';
import * as usage from './usage-format.mjs';
import * as onlineMode from './web/online-code-mode.mjs';
import * as modelControl from './web/model-control-view.mjs';

// Reuse the established fake DOM without importing its registered tests. The
// renderer itself is unmodified app.js, including summary, vitals and inspector.
const appSource = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
const definitions = appSource.replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];
const harnessSource = readFileSync(new URL('./test_monitor_ui.mjs', import.meta.url), 'utf8');
const start = harnessSource.indexOf('class FakeClassList');
const end = harnessSource.indexOf('const nowSeconds');
assert(start > 0 && end > start, 'DOM harness boundaries must remain present');
const harness = runInNewContext(harnessSource.slice(start, end) + '\n;harness',
  {definitions, runInNewContext, layout, usage, onlineMode, modelControl});

function verify(row, ui) {
  const {snapshot: s, case: mutations, now} = row;
  const failures = [];
  const check = (id, ok, actual) => { if (!ok) failures.push({id, input: mutations, wrongOutput: actual}); };
  ui.api.setFeed(s);
  ui.api.renderSummary();
  ui.api.renderVitals();
  ui.api.renderModelScope();
  const summary = ui.$('headline').textContent + ' ' + ui.$('subline').textContent;
  const mac = s.machines.find(m => m.platform === 'mac');
  const fresh = true;
  const peerPartial = mode => ['partial', 'capped', 'partial-zero'].includes(mode);
  for (const platform of ['windows', 'linux']) {
    const mode = mutations[platform];
    const machine = s.machines.find(m => m.platform === platform);
    const view = layout.machineStatusView(machine, {now});
    if (mode) {
      check('peer-counts', machine.loadedCount === null && machine.busyCount === null, machine);
      check('peer-status', peerPartial(mode) || view.tone === 'muted', view);
    } else if (!mutations['feed-age']) check('healthy-peer', machine.loadedCount === 1 && machine.busyCount === 1 && view.tone === 'ok', machine);
  }
  const pc = layout.pcReportView(s.windowsLive, {feedFresh: fresh});
  if (peerPartial(mutations.windows)) {
    check('pc-partial-busy', !pc.known || !/(?<!at least )\b\d+ busy\b/.test(pc.words), pc);
  }
  if (mutations.windows === 'partial-zero') check('pc-partial-zero', (pc.known===false || /loaded unknown/.test(pc.words)) && !/\b0\b/.test(pc.words||''), pc);
  const inventoryDamaged = ['api', 'cli'].some(k => ['partial', 'capped', 'stale', 'future'].includes(mutations[k]))
    || Boolean(mutations['combined-cap']) || Boolean(mutations['model-age']) || (mutations.api && mutations.cli);
  if (inventoryDamaged) {
    check('local-counts', mac.loadedCount === null && mac.busyCount === null, mac);
    check('history-counts', s.history.at(-1).loaded === null && s.history.at(-1).active === null, s.history.at(-1));
    check('presence-counts', row.presence === null && Boolean(row.presenceError), row.presence);
    check('local-render', !/\b\d+ (?:loaded|active|generating|busy)\b|nothing running|\bidle\b|\bquiet\b/i.test(summary), summary);
    const nisi = s.components.find(c => c.id === 'nisi');
    check('route-model-pair', !/only .* loaded|no local model is loaded/.test(nisi.detail), nisi);
  }
  // The CLI can independently count loaded models, but cannot establish the
  // API's installed/discovered catalog when that source is missing.
  if (inventoryDamaged || mutations.api) {
    const scope = ui.$('modelScopeNote').textContent;
    check('inventory-total', /unknown|incomplete|at least|unavailable|stale/i.test(scope), scope);
  }
  if (mutations['model-age']) {
    for (const model of s.models) {
      ui.api.select(`model:${model.host}:${model.id}`);
      const subtitle = ui.api.nodeSubtitle(`model:${model.host}:${model.id}`);
      if (subtitle === undefined) {
        check('model-omission-reason', /unknown|incomplete|unavailable/i.test(ui.$('modelScopeNote').textContent), ui.$('modelScopeNote').textContent);
        continue;
      }
      check('model-age-state', !/^(Generating|Busy|Idle)$/.test(subtitle), subtitle);
      ui.$('drawer').hidden = false; ui.api.renderInspector();
      check('model-inspector-executed', ui.$('inspector').textContent.length > 0, ui.$('inspector').textContent);
      const pairs = Object.fromEntries(ui.$('inspector').querySelectorAll('dt').map(dt => [dt.textContent,
        dt.parentNode.children[dt.parentNode.children.indexOf(dt) + 1].textContent]));
      check('model-age-instances', !/^\d+$/.test(pairs['Loaded instance count'] || ''), pairs['Loaded instance count']);
    }
  }
  const callersSource = s.sources.find(r => r.id === 'local-callers');
  ui.api.select('runtime'); ui.$('drawer').hidden = false; ui.api.renderInspector();
  const runtimeText = ui.$('inspector').textContent;
  if (mutations.callers) check('caller-inspector', !/local models are idle|None connected|No local process/.test(runtimeText), runtimeText);
  const callerView = layout.localCallersView(s.localCallers, {feedFresh: true, source: callersSource});
  if (mutations.callers) {
    check('caller-certainty', callerView.checked === false && callerView.text === null, {callerView, callersSource});
  } else if (s.models.some(m => m.host === 'mac' && ['busy', 'generating'].includes(m.state)))
    check('healthy-callers', callerView.checked && callerView.names.includes('Codex'), callerView);
  if (!callerView.checked) check('caller-source-idle', callersSource.state !== 'idle' && !/models are idle/.test(callersSource.detail), callersSource);
  const {feed} = ui.api.vitalsInput();
  ui.api.renderActivity(feed);
  if (mutations.jobs || mutations.router || mutations.activity) {
    const activityText = ui.$('activityStatus').textContent + ' ' + ui.$('activityTiles').textContent;
    check('activity-total', !/Nothing running|Nothing recorded|In flight0|Answered0|Failed0|Routes0/.test(activityText), activityText);
  }
  if (mutations.router) {
    const route = onlineMode.onlineCodeModeView(s.onlineCodeMode, {feedFresh: true});
    check('router-idle', !/idle|no active route|nothing .*running/i.test(route.label + route.taskLabel), route);
    check('router-lanes', !/\b0\/[12]\b/.test(route.lanes.text), route.lanes);
    if (mutations.router === 'projection-cap') {
      check('router-projection-cap', s.onlineCodeMode.runCountsComplete === false && s.pipeline.pipelinesComplete === false, s.onlineCodeMode);
      ui.api.select('pipeline'); ui.$('drawer').hidden = false; ui.api.renderInspector();
      check('router-render-cap', /at least 8/i.test(ui.$('inspector').textContent), ui.$('inspector').textContent);
    }
  }
  if (mutations.worker) {
    check('worker-future', s.windowsWorker.state === 'unknown', s.windowsWorker);
  }
  if (mutations.jobs) {
    const jobsView = layout.windowsJobsView(s.windowsJobs, {feedFresh: true});
    check('job-current-unknown', !jobsView.running && !jobsView.current, jobsView);
    check('job-row-unknown', !feed.some(r => r.kind === 'pc-job' && r.state === 'in-flight'), feed);
  }
  if (['malformed', 'partial', 'capped', 'future', 'partial-success'].includes(mutations.jobs)) {
    const jobsSource = s.sources.find(r => r.id === 'windows-jobs');
    check('job-completeness', s.windowsJobs.complete === false && !/no job in flight/.test(jobsSource.detail), {jobs: s.windowsJobs, source: jobsSource});
  }
  if (mutations['feed-age']) {
    check('expired-summary', !/working now|quiet now/.test(summary), summary);
    for (const machine of s.machines) check('expired-machine', layout.machineStatusView(machine, {now, fresh: false}).tone === 'muted', machine);
    check('expired-presence', row.presence === null, row.presence);
  }
  if (mutations.profile && Object.keys(mutations).length === 1) {
    check('healthy-idle', /quiet now/.test(summary) && mac.busyCount === 0 && mac.loadedCount === (mutations.profile === 'empty' ? 0 : 1), summary);
  }
  if (!Object.keys(mutations).length) {
    check('healthy-local', mac.loadedCount === 1 && mac.busyCount === 1 && row.presence !== null, mac);
    check('healthy-render', /1 local model is working/.test(summary), summary);
  }
  return failures;
}

test('generative invariant: real Python probes/publisher -> JSON -> dashboard renderer',
  {skip: process.platform === 'win32' ? 'Requires POSIX fcntl imported by telemetry.py in the Mac publisher' : false}, async (t) => {
  const python = resolvePython(t); if (!python) return;
  let now = 1_800_000_000;
  class ClockDate extends Date { static now() { return now * 1000; } }
  const ui = harness({extra: {Date: ClockDate, machineStatusView: (machine, options) => layout.machineStatusView(machine, {...options, now})}});
  ui.$('drawer').append(ui.$('inspector')); ui.$('drawer').hidden = false;
  const child = spawn(python.command, [...python.args, '-B', 'invariant_pipeline.py'], {cwd: new URL('.', import.meta.url)});
  let stderr = ''; child.stderr.on('data', chunk => { stderr += chunk; });
  const finished = new Promise(resolve => child.on('close', resolve));
  const counts = {}, examples = {}, combinations = new Set();
  let cases = 0;
  for await (const line of createInterface({input: child.stdout})) {
    if (!line) continue;
    const row = JSON.parse(line);
    cases++; now = row.now;
    combinations.add(JSON.stringify(row.case));
    for (const failure of verify(row, ui)) {
      counts[failure.id] = (counts[failure.id] || 0) + 1;
      (examples[failure.id] ||= []).length < 3 && examples[failure.id].push(failure);
    }
  }
  const code = await finished;
  assert.equal(code, 0, stderr);
  assert.equal(cases, 1 + 10 * 8 + (10 * 9 / 2) * 8 * 8 + 16);
  assert.equal(combinations.size, cases, 'every single and pair must be distinct');
  const report = {cases, sources: 10, modes: 8, counts, examples};
  if (process.env.INVARIANT_REPORT) writeFileSync(process.env.INVARIANT_REPORT, JSON.stringify(report, null, 2) + '\n');
  console.log(`Invariant matrix: ${cases} cases, ${Object.keys(counts).length} violation categories`);
  assert.deepEqual(counts, {}, JSON.stringify(examples, null, 2));
});

function verifyDimension(row, ui) {
  const {snapshot: s, expected: e, dimension: d, input} = row, failures = [];
  const check = (id, ok, actual) => { if (!ok) failures.push({id, dimension: d, input, wrongOutput: actual}); };
  ui.api.setPaused(d==='capture-range'&&input.frozen==='paused');
  ui.api.setFeed(s, !(d==='capture-range'&&input.frozen==='disconnected')); ui.api.renderSummary(); ui.api.renderVitals();
  const inspect = id => {ui.api.select(id); ui.$('drawer').hidden = false; ui.api.renderInspector(); return ui.$('inspector').textContent;};
  const {feed} = ui.api.vitalsInput(); ui.api.renderActivity(feed);
  const activityText = ui.$('activityStatus').textContent + ' ' + ui.$('activityTiles').textContent + ' ' + ui.$('activityList').textContent;
  if (d === 'capture-range') {
    const text=ui.$('subline').textContent;
    check('capture-range',e.representable?text.includes(new Date(input.capture*1000).toISOString()):text.includes('Capture time unknown'),text);
  }
  if (d === 'run-entry-kind') {
    check('run-kind-completeness',s.windowsJobs.complete===e.complete,s.windowsJobs);
    check('run-kind-neighbors',s.windowsJobs.recent.length===e.retained,s.windowsJobs.recent);
    check('run-kind-source',(row.source.state==='live')===e.complete,row.source);
  }
  if (d === 'combined-projection') {
    check('F1-pipeline-bound', s.pipeline.pipelinesComplete === e.complete, s.pipeline);
    check('F1-independent-counts', row.counts.runCountsComplete === e.countsComplete, row.counts);
    const text = inspect('pipeline');
    if (e.count > 8) check('F1-render-bound', /Routed tasks \(at least 8\)/.test(text), text);
  }
  if (d === 'active-bound') {
    check('F2-active-bound', s.activity.complete === e.complete && s.activity.historyBound.truncated === !e.complete, s.activity);
    if (!e.complete) {
      check('F2-render-bound', !/Nothing running|Nothing recorded|In flight0|Answered0|Failed0|Routes0/.test(activityText), activityText);
      check('F2-source-bound', row.sources[0].detail.includes('at least') || input.count <= 12, row.sources[0]);
    }
  }
  if (d === 'lane-journal') {
    const lanes = layout.windowsLaneView(s.windowsWorker, s.windowsJobs, {feedFresh: true, snapshotAge:input.snapshotAge});
    check('lane-rate-age', lanes.lanes.every(lane=>lane.rateAgeSeconds === 1+input.snapshotAge), lanes);
    check('F3-edge-liveness', lanes.edges.some(edge => edge.live) === e.live, lanes);
    check('lane-liveness', lanes.lanes.some(lane => lane.live) === e.live, lanes);
    check('duplicate-edge-attribution', JSON.stringify(lanes.edges.filter(edge=>edge.live).map(edge=>edge.lane).sort()) === JSON.stringify(e.liveLanes), lanes);
    check('lane-attribution', JSON.stringify(lanes.lanes.filter(lane=>lane.live).map(lane=>lane.id).sort()) === JSON.stringify(e.liveLanes), lanes);
    const view = layout.windowsJobsView(s.windowsJobs, {feedFresh: true});
    check('journal-positive-completeness', view.complete === e.known, view);
    for (const lane of input.lanes) {
      const text = inspect(`windows-lane:${lane}`);
      check('lane-render-liveness', /Job in flight now/.test(text) === e.liveLanes.includes(lane), text);
      if (!e.known) check('F4-current-job', !/None in flight/.test(text), text);
      if (input.slots === 'partial' && !e.live) check('partial-lane-idle', !/Up · idle/.test(text), text);
    }
  }
  if (d === 'invalid-result') {
    check('F5-invalid-terminal', s.windowsJobs.complete === false && row.source.state !== 'live', {jobs:s.windowsJobs, source:row.source});
    check('F5-current-job', !layout.windowsJobsView(s.windowsJobs, {feedFresh:true}).running, s.windowsJobs);
  }
  if (d === 'windows-producer') {
    check('F6-jobs-completeness', s.windowsJobs.complete === e.jobsComplete, s.windowsJobs);
    check('F6-pipeline-completeness', s.pipeline.pipelinesComplete === e.pipelineComplete, s.pipeline);
    check('F6-activity-completeness', s.activity?.complete === e.complete, s.activity ?? null);
    if (!e.complete) check('F6-no-false-zero', !/Nothing running|Nothing recorded|In flight0|Answered0|Failed0|Routes0/.test(activityText), activityText);
    else if (input.mode === 'empty') check('F6-healthy-empty', /PC job state unknown/.test(activityText)&&/Answered0Failed0Routes0/.test(activityText), activityText);
  }
  if (d === 'completeness-envelope') {
    const exact = /In flight0|Answered0|Failed0|Routes0/.test(activityText);
    check('F6-positive-envelope', exact === e.complete, activityText);
    if (!e.complete) check('F6-unknown-envelope', !/Nothing recorded|Nothing running/.test(activityText), activityText);
  }
  if (d === 'caller-record') {
    check('F7-malformed-callers', e.known ? Array.isArray(s.localCallers) && row.source.state === 'live' : s.localCallers === null && row.source.state === 'unavailable', {callers:s.localCallers, source:row.source});
    const view = layout.localCallersView(s.localCallers, {feedFresh:true,source:row.source});
    check('F7-caller-view', e.known ? view.checked : !view.checked && view.text === null, view);
  }
  return failures;
}

test('generated boundary dimensions: caps, journal lanes, Windows producers and process grammar',
  {skip: process.platform === 'win32' ? 'Requires POSIX fcntl imported by telemetry.py in the Mac publisher' : false}, async (t) => {
  const python = resolvePython(t); if (!python) return;
  class ClockDate extends Date { static now() { return 1_800_000_000_000; } }
  const ui = harness({extra:{Date:ClockDate}});
  ui.$('drawer').append(ui.$('inspector')); ui.$('drawer').hidden = false;
  const child = spawn(python.command, [...python.args, '-B', 'invariant_dimensions.py'], {cwd: new URL('.', import.meta.url)});
  let stderr=''; child.stderr.on('data', chunk=>{stderr+=chunk;});
  const finished=new Promise(resolve=>child.on('close', resolve));
  const counts={}, examples={}, dimensions={}, failingCases={}, failures=[], unique=new Set(); let cases=0;
  for await (const line of createInterface({input:child.stdout})) {
    if (!line) continue;
    const row=JSON.parse(line); cases++; unique.add(JSON.stringify([row.dimension,row.input]));
    dimensions[row.dimension]=(dimensions[row.dimension]||0)+1;
    const violations=verifyDimension(row,ui);
    if (violations.length) failingCases[row.dimension]=(failingCases[row.dimension]||0)+1;
    for (const failure of violations) {
      failures.push(failure); counts[failure.id]=(counts[failure.id]||0)+1;
      if ((examples[failure.id]||=[]).length<2) examples[failure.id].push(failure);
    }
  }
  assert.equal(await finished,0,stderr);
  assert.equal(cases,2574); assert.equal(unique.size,cases);
  const report={cases,dimensions,failingCases,counts,examples,failures};
  if (process.env.DIMENSION_REPORT) writeFileSync(process.env.DIMENSION_REPORT,JSON.stringify(report,null,2)+'\n');
  console.log(`Boundary dimensions: ${cases} cases, ${Object.keys(counts).length} violation categories`);
  assert.deepEqual(counts,{},JSON.stringify(examples,null,2));
});

function verifyTwoSided(row, ui) {
  const {snapshot:s, expected:e, dimension:d, input, now}=row, failures=[];
  const check=(id,ok,actual)=>{if(!ok)failures.push({id,dimension:d,input,wrongOutput:actual});};
  const frozen=d==='frozen-capture';
  ui.api.setPaused(frozen && input.frozen==='paused');
  ui.api.setFeed(s, !(frozen && input.frozen==='disconnected'));
  ui.api.renderSummary(); ui.api.renderVitals(); ui.api.renderModelScope();
  const {feed}=ui.api.vitalsInput(); ui.api.renderActivity(feed);
  const tiles=ui.$('activityTiles').textContent, activityText=ui.$('activityStatus').textContent+' '+tiles;
  const inspect=id=>{ui.api.select(id);ui.$('drawer').hidden=false;ui.api.renderInspector();return ui.$('inspector').textContent;};
  const pairs=()=>Object.fromEntries(ui.$('inspector').querySelectorAll('dt').map(dt=>[dt.textContent,dt.parentNode.children[dt.parentNode.children.indexOf(dt)+1].textContent]));
  if(d==='queue-entry') {
    check('F1-no-false-certainty',s.pipeline.queue?.complete===false && s.pipeline.pipelinesComplete===false && row.source.state==='incomplete',s.pipeline);
    check('F1-no-false-idle',s.pipeline.status==='unknown',s.pipeline.status);
  }
  if(d==='manifest-schema') {
    check('F2-manifest-completeness',s.windowsJobs.complete===e.complete,s.windowsJobs);
    check('F2-valid-neighbors-only',s.windowsJobs.recent.length===e.retained,s.windowsJobs.recent);
    if(input.mode==='zero-time')check('P2-zero-duration',s.windowsJobs.recent[0]?.elapsedSeconds===0&&s.windowsJobs.lastSuccess?.elapsedSeconds===0,s.windowsJobs);
    if(input.mode==='zero-tokens')check('P2-zero-rate',s.windowsJobs.recent[0]?.completionTokens===0&&s.windowsJobs.recent[0]?.approxPerSecond===0,s.windowsJobs);
    if(!e.complete)check('F2-no-exact-totals',!/Nothing recorded|Nothing running|In flight0|Answered0|Failed0|Routes0/.test(activityText),activityText);
    else check('P2-manifest-exact',tiles.startsWith('In flightUnknown')&&!/(Unknown|At least)/.test(tiles.slice('In flightUnknown'.length)),tiles);
  }
  if(d==='history-entry') {
    check('F3-history-completeness',s.activity.complete===false && s.activity.historyBound.truncated===true,s.activity);
    check('F3-retain-neighbors',s.activity.runs.length===e.retained,s.activity.runs);
    check('F3-no-exact-totals',!/Nothing recorded|Nothing running|In flight0|Answered0|Failed0|Routes0/.test(activityText),activityText);
  }
  if(d==='metric-identity') {
    check('F4-unique-metric-identity',new Set(s.windowsJobs.recent.map(r=>r.id)).size===input.count,s.windowsJobs.recent);
    check('F4-exact-call-totals',tiles===`In flightUnknownAnswered${e.answered}Failed${e.failed}Routes0`,tiles);
  }
  if(d==='activity-window') {
    check('P2-full-window-exact',e.complete?tiles===`In flightUnknownAnswered${input.count}Failed0Routes0`:!/^In flight0/.test(tiles),tiles);
  }
  if(d==='journal-observation') {
    const jobs=layout.windowsJobsView(s.windowsJobs,{feedFresh:true});
    check('P1-journal-observation',jobs.running===(e.current&&!input.success)&&jobs.current===(e.current&&input.success),jobs);
    check('P1-journal-observation-totals',e.current?!/Unknown|At least/.test(tiles):!/In flight0|Answered0|Failed0|Routes0/.test(tiles),tiles);
  }
  if(d==='route-event-age') {
    check('P2-route-observation',s.pipeline.pipelinesComplete===e.complete&&row.counts.runCountsComplete===e.complete,{pipeline:s.pipeline,counts:row.counts});
    const text=inspect('pipeline');
    check('P2-route-render',e.complete?text.includes(`Routed tasks (${input.count})`):text.includes('Routed tasks (at least 8)'),text);
  }
  if(d==='active-event-age') {
    check('P2-active-observation',s.activity.complete===e.complete,s.activity);
    check('P2-active-render',e.complete?tiles===`In flight${input.state==='running'?input.count:0}Answered0Failed0Routes${input.count}`:/At least 12/.test(tiles),tiles);
  }
  if(d==='windows-healthy') {
    check('P2-windows-counts',ui.$('loadedCount').textContent===String(input.count)&&ui.$('activeCount').textContent===String(input.busy?input.count:0)&&ui.$('queuedCount').textContent===`${input.busy?input.count:0}/${input.count*2}`,
      [ui.$('loadedCount').textContent,ui.$('activeCount').textContent,ui.$('queuedCount').textContent]);
    check('P2-windows-summary',ui.$('headline').textContent.includes(input.busy?'working now.':'quiet now.'),ui.$('headline').textContent);
    check('P2-windows-history',tiles==='In flightUnknownAnswered1Failed0Routes0',tiles);
    for(let i=0;i<input.count;i++){
      inspect(`model:windows:fixture/model-${i}`); const p=pairs();
      check('P2-windows-slots',p.Slots===`${input.busy?1:0}/2 busy`,p);
      if(input.busy)check('P2-windows-rate',p['Live decode']==='55 tok/s (between two slot polls)',p['Live decode']);
      check('P2-windows-identity',p['Served model']===`fixture/model-${i}`,p['Served model']);
    }
    check('P2-windows-event-age',s.windowsJobs.recent[0].ageSeconds===600,s.windowsJobs);
  }
  if(d==='cli-envelope') {
    const m=row.cli?.machines.find(m=>m.platform==='windows');
    check('F5-cli-valid-envelope',Boolean(m),row.error);
    if(m){
      check('F5-inventory-counts',input.complete===true && input.state==='up' ? m.loadedCount===input.count&&m.busyCount===input.count : m.loadedCount===null&&m.busyCount===null,m);
      check('F5-unknown-hands-off',input.state!=='unknown'||m.handsOff===null,m);
      if(input.state==='up')check('P2-cli-known-state',m.state==='up'&&m.handsOff===input.hands,m);
    }
  }
  if(d==='healthy-exact') {
    check('P2-summary-counts',ui.$('loadedCount').textContent===String(e.loaded)&&ui.$('activeCount').textContent===String(e.busy)&&ui.$('queuedCount').textContent===String(e.queued),[ui.$('loadedCount').textContent,ui.$('activeCount').textContent,ui.$('queuedCount').textContent]);
    check('P2-summary-label',ui.$('headline').textContent.includes(e.busy?'working now.':'quiet now.'),ui.$('headline').textContent);
    check('P2-inventory-scope',!/(unknown|unavailable|incomplete|at least|stale)/i.test(ui.$('modelScopeNote').textContent),ui.$('modelScopeNote').textContent);
    for(const m of s.machines){const view=layout.machineStatusView(m,{now,snapshotAge:now-s.sampledAt,fresh:true});check('P2-machine-values',view.tone==='ok'&&view.detail.includes(`${m.loadedCount} loaded`)&&view.detail.includes(`${m.busyCount} busy`),view);}
    const roster=ui.api.scopeProjection();
    for(const id of e.models){
      check('P2-model-roster',roster.includes(input.variation.startsWith('alias')?'fixture-alias':id),roster);
      inspect(`model:mac:${input.variation.startsWith('alias')?'fixture-alias':id}`);const p=pairs();
      check('P2-model-instance',p['Loaded instance count']==='1',p['Loaded instance count']);
      check('P2-model-phase',ui.api.nodeSubtitle(`model:mac:${input.variation.startsWith('alias')?'fixture-alias':id}`)===(id==='fixture/second'||!e.busy?'Idle':'Generating'),ui.api.nodeSubtitle(`model:mac:${input.variation.startsWith('alias')?'fixture-alias':id}`));
      if(id==='fixture/model')check('P2-model-fields',p['Queued requests']===String(e.queued)&&p['Parallel slots']==='2',p);
    }
    if(input.variation==='archive-event-age'){
      const source=ui.$('sources').children.find(row=>row.querySelector('strong')?.textContent==='Recent completed routes');
      check('P2-history-source',source && !/stale|unavailable|unknown|incomplete/i.test(source.textContent),source?.textContent);
    }
    check('P2-activity-exact',tiles===`In flight${e.open}Answered${e.answered}Failed${e.failed}Routes${e.routes}`,tiles);
    const jobs=layout.windowsJobsView(s.windowsJobs,{feedFresh:true,snapshotAge:now-s.sampledAt});
    check('P2-job-current',jobs.complete && jobs.current===e.currentSuccess && jobs.running===(e.open>0),jobs);
    inspect('windows-lane:fast');const laneText=ui.$('inspector').textContent;
    check('P2-lane-slots',laneText.includes(`Slots${e.slotsBusy} of 2 busy`),laneText);
    check('P2-lane-current',e.open?laneText.includes('Job in flight now'):laneText.includes('None in flight'),laneText);
    if(e.currentSuccess)check('P2-recent-success',laneText.includes('Last answer2.0 s')&&laneText.includes('writes 20 tok/s'),laneText);
    inspect('runtime');const p=pairs();
    check('P2-runtime-counts',p['Working models']===String(e.busy),p['Working models']);
    check('P2-callers',p['Local callers']==='Codex',p['Local callers']);
  }
  if(frozen){
    const capture=new Date(s.sampledAt*1000).toISOString();
    check('P3-capture-time',ui.$('subline').textContent.includes(capture),ui.$('subline').textContent);
    const surfaces={summary:ui.$('headline').textContent+' '+ui.$('subline').textContent,
      sources:ui.$('sources').textContent,
      activity:activityText+' '+ui.$('activityMeaning').textContent+' '+ui.$('activityList').textContent,
      vitals:['vitalMac','vitalPc','vitalRoute','vitalActivity'].map(id=>ui.$(id).textContent).join(' ')};
    for(const id of ['runtime',...s.models.filter(m=>m.host===s.host).map(m=>`model:${m.host}:${m.id}`),'pipeline','windows-worker','windows-lane:fast']){
      const text=inspect(id); if(!ui.$('drawer').hidden)surfaces[id]=text;
    }
    for(const [surface,text]of Object.entries(surfaces))check('P3-no-now',!/\bnow\b/i.test(text),{surface,text});
  }
  return failures;
}

test('two-sided generator: degraded certainty, healthy exactness, frozen capture time',
  {skip: process.platform === 'win32' ? 'Requires POSIX fcntl imported by telemetry.py in the Mac publisher' : false}, async(t)=>{
  const python = resolvePython(t); if (!python) return;
  let now=1_800_000_000; class ClockDate extends Date{static now(){return now*1000;}}
  const ui=harness({extra:{Date:ClockDate,machineStatusView:(m,o)=>layout.machineStatusView(m,{...o,now})}});
  ui.$('drawer').append(ui.$('inspector')); ui.api.spyScope(); ui.api.setModelScope('all');
  const child=spawn(python.command,[...python.args,'-B','invariant_two_sided.py'],{cwd:new URL('.',import.meta.url)});
  let stderr='';child.stderr.on('data',chunk=>{stderr+=chunk;});const finished=new Promise(resolve=>child.on('close',resolve));
  const counts={},dimensions={},failingCases={},examples={},failures=[],unique=new Set();let cases=0;
  for await(const line of createInterface({input:child.stdout})){
    if(!line)continue;const row=JSON.parse(line);now=row.now;cases++;unique.add(JSON.stringify([row.dimension,row.input]));
    dimensions[row.dimension]=(dimensions[row.dimension]||0)+1;
    const violations=verifyTwoSided(row,ui);if(violations.length)failingCases[row.dimension]=(failingCases[row.dimension]||0)+1;
    for(const failure of violations){failures.push(failure);counts[failure.id]=(counts[failure.id]||0)+1;if((examples[failure.id]||=[]).length<2)examples[failure.id].push(failure);}
  }
  assert.equal(await finished,0,stderr);assert.equal(cases,347);assert.equal(unique.size,cases);
  const report={cases,dimensions,failingCases,counts,examples,failures};
  if(process.env.TWO_SIDED_REPORT)writeFileSync(process.env.TWO_SIDED_REPORT,JSON.stringify(report,null,2)+'\n');
  console.log(`Two-sided dimensions: ${cases} cases, ${Object.keys(counts).length} violation categories`);
  assert.deepEqual(counts,{},JSON.stringify(examples,null,2));
});
