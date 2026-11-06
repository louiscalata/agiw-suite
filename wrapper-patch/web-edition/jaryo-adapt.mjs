// Reviewed migration from the 317aad2 rules onto the current Jaryo web.
// Pins below are literals. They are not computed from the file being built.
// A statement that no longer exists is not given a dummy element: its rule is dropped
// only when the key is absent. A statement that is still present keeps its removal.
import { splitJs } from './engine.mjs';

export const PINS = {
  'app.js': {
    'function:renderList': '3df24fe44d98a4985be06c6344fe062bb0910efe3b8d64253c541c3d10395f72',
    'function:renderInspector': '4c6d1e941ed41fd2f248b45ec0662ed538ed672120f460a76c7e6d0270f4791a',
    'function:inspectorBody': '775869f952a75b67c4040815441ebbd819104a224f43c551c37aa325c37bae39',
    'function:renderVitals': '41f8987af023d7fecd860c342d7941727fca9516c97ac0f7856aec19bed7fa7c',
    'const:INSPECTOR_EYEBROW': 'e0d88566ac70bd0420289911192fb369018c65fe16262a65f8ba45b6c0a614fc',
    'function:openStream': '637edf71ec0a32021c05e1504d9999d29b7b42f46ab688aeb9f974b0dd28590f',
    'function:streamFailed': '464acda79b0754d358154db24271e2d59fc5caafdd84fc8b5bc22f405de78c2f',
    'function:startPolling': '8cca05d4b49f8d2ca93d5b316b4ef2d75957c67f6205dd5dc7f20a3071cb57f1',
    'function:stopPolling': '3ce064dabc4e7aaf494f4b813e29e2af1d42e8ae34f54dd06be28e3b0a66b137',
    'function:poll': '409f7cebf7cd54498789025071594c71739c6a8d0eaaed09ec54d2aa2a83cdb0',
    'function:vitalsInput': '74659ef908059005abab3e38301e0754f3ddb0e985f8b5db2f9d90383b40327f',
  },
  'map-layout.mjs': {
    'function:vitalsView': '13c85414ef029644b765f4b29e22597c737e497901ae401608a1a980aa6972e3',
  },
};

const TRANSPORT = [
  'var feedStopped=false,streamLastSnapshotAt=typeof streamLastSnapshotAt==="number"?streamLastSnapshotAt:0,streamNewestSampledAt=typeof streamNewestSampledAt==="number"?streamNewestSampledAt:-1,pollController=pollController||null;',
  'var modelControlEpoch=0;',
  'function acceptSnapshot(data){if(Boolean(snapshot?.demo)!==Boolean(data?.demo)){modelControlError="";modelControlStatus=null;modelControlEpoch++;}snapshot=data;}',
  'function checkStreamHealth(){if(feedStopped||document.hidden||!feedStream)return;if(performance.now()-streamLastSnapshotAt<10000)return;streamFailed();}',
  'setInterval(checkStreamHealth,1000);',
  "window.addEventListener('pagehide',()=>{feedStopped=true;pollController?.abort();const source=feedStream;feedStream=null;source?.close();clearTimeout(streamRetry);streamRetry=null;stopPolling();});",
  "window.addEventListener('pageshow',()=>{if(!feedStopped)return;feedStopped=false;openStream();});",
  'window.agiwFeed=Object.freeze({snapshot:()=>snapshot,fresh:()=>fresh()});',
  "document.documentElement.dataset.svReady='1';",
].join('\n');

const FACTORY = [
  'export const FACTORY_STATION = Object.freeze({ width: 140, height: 64, column: 180, row: 96 });',
  'export function factoryLayout(runtimeCount, clients, { viewportWidth = 820, viewportHeight = 640, localOnly = true, sample = false } = {}) {',
  '  const width = Number.isFinite(viewportWidth) ? Math.max(416, viewportWidth) : 820;',
  '  const cols = width < 620 ? 2 : 3, portrait = cols === 2;',
  '  const left = 110, centerX = left + (cols - 1) * FACTORY_STATION.column / 2;',
  '  const at = (slot, top) => ({ x: left + (slot % cols) * FACTORY_STATION.column,',
  '    y: top + Math.floor(slot / cols) * FACTORY_STATION.row });',
  '  const runtime = localOnly ? { x: centerX, y: 70 } : at(0, 70);',
  '  const modelTop = localOnly ? 220 : 70 + Math.ceil(7 / cols) * FACTORY_STATION.row + 100;',
  '  const runtimeModels = Array.from({ length: runtimeCount }, (_, i) => at(i, modelTop));',
  '  const labels = [',
  '    { text: `${sample ? "SAMPLE MODELS" : "LOCAL MODELS"} · ${runtimeCount}`, x: centerX, y: modelTop - 62, align: "middle", kind: "local" },',
  '    { text: "OBSERVATION LINKS · NOT TASK ROUTES", x: centerX, y: 12, align: "middle", kind: "mac" },',
  '  ];',
  '  return { runtime, runtimeModels, labels, portrait };',
  '}',
  'export function observationRoute(from, to) {',
  '  return `M ${from.x} ${from.y} L ${to.x} ${to.y}`;',
  '}',
].join('\n');

const occurrences = (text, needle) => text.split(needle).length - 1;

function statementMap(source) {
  if (!source) return new Map();
  return new Map(splitJs(source).statements.map(statement => [statement.key, statement]));
}

function stripMissing(spec, map) {
  if (spec.drop) spec.drop = spec.drop.filter(key => map.has(key));
  for (const key of Object.keys(spec.replace || {})) if (!map.has(key)) delete spec.replace[key];
  for (const key of Object.keys(spec.edit || {})) {
    const statement = map.get(key);
    if (!statement) { delete spec.edit[key]; continue; }
    spec.edit[key] = spec.edit[key].filter(edit => occurrences(statement.text, edit[0]) === (edit[2] ?? 1));
    if (!spec.edit[key].length) delete spec.edit[key];
  }
  for (const key of Object.keys(spec.dropLines || {})) {
    const statement = map.get(key);
    if (!statement) { delete spec.dropLines[key]; continue; }
    const lines = statement.text.split('\n');
    spec.dropLines[key] = spec.dropLines[key].filter(range => {
      const found = lines.filter(line => line.startsWith(range.from)).length;
      return found === (range.count ?? 1);
    });
    if (!spec.dropLines[key].length) delete spec.dropLines[key];
  }
}

function ensureDrop(spec, map, key) {
  if (map.has(key) && !spec.drop.includes(key)) spec.drop.push(key);
}

function ensureReplace(spec, map, key, pin, text) {
  if (!map.has(key)) return;
  spec.replace[key] = { sha256: pin, text };
  if (spec.dropLines) delete spec.dropLines[key];
  if (spec.edit) delete spec.edit[key];
}

function ensureEdit(spec, map, key, from, to, expected = 1) {
  const statement = map.get(key);
  if (!statement || occurrences(statement.text, from) !== expected) return;
  const edits = spec.edit[key] ||= [];
  if (!edits.some(edit => edit[0] === from)) edits.push(expected === 1 ? [from, to] : [from, to, expected]);
}

/** Mutates the edition rules so the current Jaryo sources match the same removal policy. Idempotent. */
export function adaptEdition(rules, upstream) {
  const maps = new Map();
  for (const name of ['app.js', 'map-layout.mjs', 'model-control-view.mjs', 'usage-format.mjs']) {
    maps.set(name, statementMap(upstream.get(name)));
  }
  for (const [file, pins] of Object.entries(PINS)) {
    const spec = rules.js[file];
    const map = maps.get(file);
    if (!spec?.replace || !map) continue;
    for (const [key, pin] of Object.entries(pins)) {
      if (spec.replace[key] && map.has(key)) spec.replace[key].sha256 = pin;
    }
  }
  for (const [file, spec] of Object.entries(rules.js)) {
    if (file.endsWith('.mjs') || file.endsWith('.js')) stripMissing(spec, maps.get(file) || new Map());
  }
  const inspector = rules.js['app.js'].replace?.['function:inspectorBody'];
  if (inspector?.text.includes('workflowInspector')) {
    inspector.text = inspector.text.replace('if(workflowInspector(n,body))return;\n  ', '');
  }
  const app = rules.js['app.js'];
  const appMap = maps.get('app.js');
  for (const key of [
    'import:/monitor-tabs.mjs', 'import:/monitor-board.mjs', 'let:monitorDesk',
    'function:desk', 'function:demoRequested', 'function:renderMonitorTabs',
    'function:paintMonitorScope', 'function:bindMonitorTabs', 'function:selectMonitor',
  ]) ensureDrop(app, appMap, key);
  ensureEdit(app, appMap, 'function:render', 'renderMonitorTabs();', '', 2);
  ensureEdit(app, appMap, 'function:render', 'paintMonitorScope();', '');
  ensureEdit(app, appMap, 'function:render', "$('runsTab').hidden=snapshot.host==='windows';if(view==='runs'&&snapshot.host==='windows')view='runtime';", '');
  ensureEdit(app, appMap, 'import:/map-layout.mjs', 'import {constellationLayout,', 'import {factoryLayout,constellationLayout,');
  ensureEdit(app, appMap, 'new:ResizeObserver', 'setInterval(pollOnlineCodeAction,1000);bindMonitorTabs();pollOnlineCodeAction();', '');
  ensureEdit(app, appMap, 'function:localRuntimeCounts', "...(host==='windows'?['lane-inventory']:[])", '');
  ensureEdit(app, appMap, 'function:renderGraph', 'const web=renderWeb();\n', 'const web=null;\n');
  ensureEdit(app, appMap, 'function:renderGraph', "const windowsMatches=snapshot?.host==='windows'&&view==='runtime'&&query?new Set(windowsMapListEntries(graph.nodes,{host:'windows',query,hostname:snapshot.hostId}).flatMap(entry=>entry.clientId?[entry.key,`client:${entry.clientId}`]:[entry.key])):null;\n", '');
  ensureEdit(app, appMap, 'function:renderGraph', 'windowsMatches?.has(n.id)||', '');
  ensureEdit(app, appMap, 'function:renderGraph', 'edgeCurvePath(geometry)', "'M '+geometry.sx+' '+geometry.sy+' L '+geometry.ex+' '+geometry.ey");
  for (const key of [
    'const:currentPipeline', 'function:windowsLaneLiveRows', 'function:jobEvidence',
    'function:windowsJobsSource', 'function:windowsJobsCurrent', 'const:CHIPS_WINDOWS', 'function:traceGraph',
    'function:activityHistoryComplete', 'function:pcLiveRows', 'function:pcLinkLabel',
  ]) ensureDrop(app, appMap, key);
  ensureReplace(app, appMap, 'function:modelFreshness', '9b4b1ec296870ff0003643afb1ca8355687678516966f4ed43948af52e8c12ed',
    "function modelFreshness(m,elapsed=snapshot?Math.max(0,Date.now()/1000-snapshot.sampledAt):0){const age=Number.isFinite(m.ageSeconds)?m.ageSeconds+elapsed:NaN;const expired=!fresh()||!Number.isFinite(age)||age<0||age>3;return{expired,current:!expired&&Number.isFinite(age),activityExpired:false};}");
  ensureReplace(app, appMap, 'function:localActivityKnown', '45fb4a3fbcb79cd7ee26a760cea5526d7c3a6eef01d73a9e8d4c05174d2cc852',
    "function localActivityKnown(rows){return snapshot?.activityKnown===true&&rows.every(m=>typeof m.loaded==='boolean');}");
  ensureReplace(app, appMap, 'function:setView', '62bfbd16e73b95a93ea5132d254407ff8c9c72257eab5a4d681ebee79f5d0b1d',
    "function setView(value){view=value;selected=null;runId=null;userCamera=false;$('drawer').hidden=true;document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view===view)));render();}");
  ensureReplace(app, appMap, 'const:CAPTIONED', '138a5abca15488ed9dd0745536e99a1dfdbcad7af81a78598a6c8193afb652eb',
    "const CAPTIONED=new Set(['runtime']);");
  ensureReplace(app, appMap, 'function:placeMacModelCaptions', 'ce79de5fa05c7a0e78acb37a81b9730de6f6bc84000afbc2e993d9fa10a5acd7',
    'function placeMacModelCaptions(){}');
  ensureReplace(app, appMap, 'function:activityComplete', '739fdcc6a631f75ccb330727e84e28e2cd93784158c35e3df63d5dd3450b66dc',
    "function activityComplete(){const sampled=snapshot?.activity?.sampledAt,age=sampled===undefined?0:Date.now()/1000-sampled;return fresh()&&Number.isFinite(age)&&age>=-1&&age<=3&&snapshot?.activity?.complete===true;}");
  ensureReplace(app, appMap, 'function:runtimeGraph', '1fb459944f9501996b777bfbb3e6924b931ecb83b94826f9526ba72d08390a9d',
    "function runtimeGraph(){const rows=models().filter(m=>m.host!=='linux');const region=$('graphRegion');const layout=factoryLayout(rows.length,[],{viewportWidth:region.clientWidth||820,viewportHeight:region.clientHeight||640,sample:snapshot?.demo===true});layout.labels.forEach(addLabel);const root=addNode('runtime',snapshot?.demo===true?'Sample Mac':'This Mac',layout.runtime.x,layout.runtime.y,{root:true,hub:true,r:18,color:'#d7d7d7',subtitle:snapshot?.demo===true?'SAMPLE HUB':'LOCAL HUB',kind:'runtime',factory:true});rows.forEach((m,i)=>{const pos=layout.runtimeModels[i];if(!pos)return;const activity=modelActivity(m);const visual=modelStarSize(m);const subtitle=paused&&ACTIVE.has(m.state)?`At pause: ${STATE[m.state]}`:STATE[m.state]||'Unknown';const n=addNode(`model:${m.host}:${m.id}`,shortName(m.id),pos.x,pos.y,{color:visual.color,r:visual.radius,subtitle,kind:'model',model:m,visual,active:activity.active===true,auraPeriodMs:activity.periodMs,unknown:['unloaded','unknown','stale','loaded'].includes(m.state),factory:true});edge(root.id,n.id,n.color,n.unknown,(i%2?1:-1)*22);});}");
  ensureReplace(app, appMap, 'function:pollModelControl', '8c14f658c12c34bab4c60416f90654bca5daf4f8665ae9c6156f066c68e9589e',
    "async function pollModelControl(){const epoch=modelControlEpoch;try{const response=await fetch('/api/models/control',{cache:'no-store'});if(epoch!==modelControlEpoch||snapshot?.demo===true){if(snapshot?.demo===true){modelControlStatus=null;modelControlError='';}return;}if(!response.ok)throw new Error('Control status unavailable');const result=await response.json();if(epoch!==modelControlEpoch||snapshot?.demo===true){modelControlStatus=null;modelControlError='';return;}if(result&&typeof result.status==='string')modelControlStatus=result;}catch(_){if(epoch===modelControlEpoch&&snapshot?.demo!==true)modelControlStatus=null;}renderModelAction();}");
  ensureReplace(app, appMap, 'function:renderSummary', 'ecb3ac4762b0c79402fc424dbfd29bf0d15b0d904b56289b4c9f248d8cfae30a',
    "function renderSummary(){const counts=localRuntimeCounts(),known=counts.busyCount!==null,active=counts.busyCount,loadedCount=counts.loadedCount;$('activeCount').textContent=active??(counts.observedBusy?`at least ${counts.observedBusy}`:'—');$('loadedCount').textContent=loadedCount??'—';$('queuedCount').textContent='—';const copy=paused?{lead:'Local runtime',em:'view paused.',sub:'Resume to verify current activity.'}:clockMismatch()?{lead:'Local runtime',em:'clock mismatch.',sub:'Observer timestamp is ahead of this Mac clock.'}:runtimeCopy(fresh(),known,active,counts.inventoryLive,loadedCount,null);$('headline').replaceChildren(document.createTextNode(copy.lead),el('br'),el('em',null,copy.em));$('subline').textContent=copy.sub;if(!liveNow())$('subline').textContent+=` ${captureText()}.`;if(!snapshot?.sources)return;const sourcesLive=liveNow();$('sources').replaceChildren();snapshot.sources.forEach(s=>{const row=el('div','source');row.append(el('strong',null,s.label),el('span',null,s.state),el('small',null,s.detail||''));$('sources').append(row);});const liveSources=snapshot.sources.filter(s=>s.state==='live').length;$('sourceCount').textContent=snapshot?.demo===true?`${snapshot.sources.filter(s=>s.state==='live').length} illustrative live · ${snapshot.sources.filter(s=>s.state==='recorded').length} illustrative recorded`:sourcesLive?`${liveSources} live`:'live unknown';}");
  const layout = rules.js['map-layout.mjs'];
  const layoutMap = maps.get('map-layout.mjs');
  for (const key of [
    'function:windowsJobsEvidence', 'function:windowsLaneActivitySummary', 'function:windowsMacCaptionShift',
    'function:windowsMapListEntries', 'function:windowsMacListEntries', 'function:windowsSharePublishRow',
    'function:routeReviewCounts', 'function:constellationLayout', 'const:focusedLaneLayout',
    'function:activitySummaryAt', 'function:pcAdaptersView',
  ]) ensureDrop(layout, layoutMap, key);
  ensureEdit(layout, layoutMap, 'function:machineStatusView', '{mac: 3, windows: 30, linux: 30}', '{mac: 3, linux: 30}');
  ensureEdit(layout, layoutMap, 'function:machineStatusView', "'share_unmounted', ", '');
  ensureEdit(layout, layoutMap, 'function:agiwPeersRows', "host !== 'windows' && ", '');
  ensureEdit(layout, layoutMap, 'function:agiwPeersRows', "${host === 'windows' && !current ? ' at last report' : ''}", '');
  if (!(app.append || []).some(line => String(line).includes('function checkStreamHealth()'))) {
    app.append = [...(app.append || []), TRANSPORT];
  }
  if (!(layout.append || []).some(line => String(line).includes('export function factoryLayout'))) {
    layout.append = [...(layout.append || []), FACTORY];
  }
  const html = rules.html['index.html'];
  const index = upstream.get('index.html') || '';
  html.removeIds = html.removeIds.filter(id => occurrences(index, `id="${id}"`) === 1);
  for (const id of ['jaryoTheme', 'jaryoDemo', 'monitorTabHint', 'monitorTabs', 'monitorBoard']) {
    if (occurrences(index, `id="${id}"`) === 1 && !html.removeIds.includes(id)) html.removeIds.push(id);
  }
  html.removeElements = (html.removeElements || []).filter(start => occurrences(index, start) === 1);
  // The Overview nav stays. It is the landing (href="/" aria-current="page").
  // The theme control needs jaryo.mjs, which this edition does not ship.
  // Map, Components, and Identity are separate documents outside the eight-file list.
  // The engine refuses those hrefs, so the anchors are removed here on purpose and named in APPLY.md.
  for (const start of ['<div class="j-chrome"', '<label class="j-theme"']) {
    if (occurrences(index, start) === 1 && !html.removeElements.includes(start)) html.removeElements.push(start);
  }
  html.edit = html.edit.filter(edit => occurrences(index, edit[0]) === (edit[2] ?? 1));
  for (const edit of html.edit) {
    if (edit[1].includes('id="openRuntimeApp"') && !edit[1].includes('id="advancedSwitch"')) {
      edit[1] = edit[1].replace('<span id="connection"', '<button id="advancedSwitch" type="button">Simple</button><span id="connection"');
    }
  }
  const additions = [
    ['<link rel="icon" href="/jaryo/marks/favicon.svg" type="image/svg+xml">\n', ''],
    ['<link rel="stylesheet" href="/jaryo.css">\n', ''],
    ['<script src="/jaryo.mjs" type="module"></script>\n', ''],
  ];
  for (const edit of additions) {
    if (occurrences(index, edit[0]) === 1 && !html.edit.some(item => item[0] === edit[0])) html.edit.push(edit);
  }
  for (const from of [
    '<a href="/monitor">Map</a>\n',
    '<a href="/components">Components</a>\n',
    '<a href="/identity">Identity</a>\n',
  ]) {
    if (occurrences(index, from) === 1 && !html.edit.some(item => item[0] === from)) html.edit.push([from, '']);
  }
  const about = html.replaceContent?.aboutPanel;
  if (about?.includes('Jeview')) {
    html.replaceContent.aboutPanel = about.replace('<p>Visual direction inspired by Jeview, an independent MIT-licensed visualizer.</p>', '');
  }
}
