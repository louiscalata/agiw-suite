// App Store edition of the shared web UI: what scripts/sync-web.sh ships in App/Resources/web.
//
// The private edition's panels are removed together with their code, not hidden (App Review guideline
// 2.3.1; release plan D3): Online Code, Fix inference, the PC LLM switch, the Windows PC hub and lanes, the
// Nisi + Jev route and its call traces, client model identities, live activity and local callers. What stays
// is the LM Studio inventory map, the model inspector, model control, the Mac status chip, sample-data
// labelling, and an "Open LM Studio" button (release plan feature matrix).
//
// Every rule is exact. A statement key that is missing, an anchor that matches the wrong number of times, or
// a replaced statement whose upstream text changed (sha256 pin) stops the sync; see engine.mjs.
import { overrides, feedRecovery } from './appstore-overrides.mjs';

/** Files the edition ships, in load order. online-code-mode.mjs is not shipped at all. */
export const ship = ['index.html', 'style.css', 'app.js', 'usage-format.mjs', 'map-layout.mjs', 'model-control-view.mjs', 'simple-view.mjs', 'simple-view.css'];

const ABOUT = '<h2>A map of evidence.</h2>'
  + '<p>The Mac hub shows what LM Studio\'s local server on this Mac reports: every installed model, and which ones are '
  + 'loaded. GPU figures come from macOS. When sample data is on, the models and figures are invented and labelled as '
  + 'sample data.</p>'
  + '<p>LM Studio\'s local API does not report whether a model is generating right now, so activity stays unknown '
  + 'instead of being guessed. Lines group observations; they are not network connections. Nothing here reads prompts '
  + 'or conversations, and nothing leaves this Mac.</p>'
  + '<p>Visual direction inspired by Jeview, an independent MIT-licensed visualizer.</p>';

export const html = {
  'index.html': {
    removeIds: ['onlineCodeMode', 'pcSwitch', 'fixInference', 'vitalPc', 'vitalRoute', 'vitalActivity',
      'onlineCodeDetails', 'fixInferenceDetails', 'activityDetails', 'drawerJump',
      'modelScopeControls', 'modelScopeNote', 'memoryBanner', 'memoryAnnounce', 'inferenceReceipts', 'monitorConnectionSwitches'],
    removeElements: ['<div class="client-summary"', '<div class="view-tabs"', '<p class="coverage">',
      '<section class="auto-unload-control"', '<p class="quiet"><a href="/components"', '<span class="agent-key"'],
    replaceContent: {
      emptyMap: '<h2>Nothing to show yet.</h2><p>Waiting for the first snapshot.</p>',
      aboutPanel: ABOUT,
    },
    edit: [
      ['<link rel="stylesheet" href="/connection-switches.css">', ''],
      // The Mac origin serves one document; the full web's Machines page has no edition view.
      ['<a href="/machines">Machines</a>\n', ''],
      ['<a href="/setup">Setup</a>\n', ''],
      ['<script src="/connection-switches.mjs" type="module"></script>', ''],
      ['<script src="/simple-view-private.mjs" type="module"></script>', ''],
      ['aria-label="Interactive constellation of local runtime and recorded client model evidence"', 'aria-label="Interactive map of the local runtime and its models"'],
      ['<title>AGIW Suite · Inference Monitor</title>', '<title>AGIW · Overview</title>'],
      ['<div class="brand"><h1 class="brand-wordmark"><strong class="j-type">AGIW</strong><small>Inference Monitor</small></h1></div>',
        '<div class="brand"><h1 class="brand-wordmark"><strong class="j-type">AGIW</strong><small>Inference Monitor</small></h1><span id="sampleData" class="sample-badge" role="status" title="Invented sample data, not read from this Mac" hidden>Sample data</span></div>'],
      ['<span id="connection" class="connection">CONNECTING</span>',
        '<button id="openRuntimeApp" class="runtime-app" type="button" title="Open LM Studio, or its website when it is '
        + 'not installed">Open LM Studio</button><span id="connection" class="connection">CONNECTING</span>'],
      ['aria-label="Runtime and client browser"', 'aria-label="Model browser"'],
      ['aria-label="Live status"', 'aria-label="Model and GPU status"'],
      ['placeholder="Find runtime, client, model or run…" aria-label="Find runtime, client, model or run"',
        'placeholder="Find a model…" aria-label="Find a model"'],
      ['<h2 id="listTitle">Runtime &amp; clients</h2>', '<h2 id="listTitle">Local models</h2>'],
      ['aria-label="Open runtime and client browser"', 'aria-label="Open model browser"'],
      ['aria-label="Interactive inference map"',
        'aria-label="Interactive map of the local runtime and its models"'],
      ['<span><i class="legend-line flow"></i>Windows job</span>', ''],
      ['<i class="legend-line recorded"></i>Recorded / advertised', '<i class="legend-line recorded"></i>Activity not reported'],
    ],
  },
};

export const js = {
  'app.js': {
    stripComments: true,
    drop: [
      'const:agiwRepair',
      'import:/online-code-mode.mjs', 'import:/inference-routes.mjs', 'function:renderInferenceReceipts',
      'function:renderWeb', 'function:webHidden', 'function:webMotionLoop', 'function:drawWebMotion', 'function:flashWeb',
      'const:CLIENTS', 'const:CLIENT_COLORS', 'const:CLIENT_STATE',
      'let:autoUnloadState', 'const:SUBAGENT_UNKNOWN', 'function:displayedModels',
      'function:renderModelScope', 'function:setModelScope',
      'const:COMPLETION_CODES', 'const:COMPLETION_PROGRESS', 'const:COMPLETION_VIEW_FIELDS',
      'function:completionEvidenceView', 'function:renderCompletionEvidence',
      'function:validAutoUnloadState', 'function:renderAutoUnload', 'function:fetchAutoUnload',
      'function:pollAutoUnload', 'function:requestAutoUnload',
      'function:nisiRecovery', 'function:memorySection', 'const:ROUTE_RUN_ID',
      'function:routePipelines', 'function:routeRunLine',
      'function:openFixInference', 'function:closeFixInference',
      'let:memoryDismissed', 'const:memoryClock', 'function:memoryNow',
      'function:renderMemoryBanner', 'function:dismissMemoryBanner', 'function:openMemoryDetails',
      'let:onlineCodeActionStatus', 'let:fixInferenceRenderKey', 'const:FIX_ACTIONS', 'const:HEADLESS_ACTIONS',
      'const:LANE_COLORS', 'const:decisionText',
      'const:runs', 'const:runKey', 'const:runSource', 'const:routeLabel',
      'function:runSummary', 'function:identifier',
      'function:clientRows', 'function:clientIdentityCounts', 'const:clientAge',
      'function:traceGraph', 'function:hideOnlineCodeDetails', 'function:hideFixInferenceDetails',
      'function:selectRun', 'function:renderRunSummary', 'function:headlessText',
      'const:capital', 'const:compLabel', 'const:headlessShort', 'const:jobStatus', 'function:jobLine', 'const:timeoutText',
      'function:fixScopeNote', 'function:stepText', 'function:onlineActionName', 'function:onlineActionStatusLabel',
      'function:onlineCodeSummary', 'function:updateOnlineCodeMode', 'function:onlineCodeTabLabel',
      'function:headlessView', 'function:headlessAction', 'function:updateInferenceFix', 'function:renderFixResult',
      'const:fixScopeText', 'function:updateFixScopeNote',
      'function:recordOnlineCodeAction', 'let:onlineCodeActionChecked', 'const:onlineCodePollWanted',
      'function:pollOnlineCodeAction', 'function:requestOnlineCodeAction',
      'const:ROUTE_WORDS', 'const:activityWho', 'const:activityStatus', 'const:activityMeta',
      'function:renderActivitySummary', 'let:activityKeys', 'function:renderActivity', 'function:openActivityRow',
      'function:hideActivityDetails', 'function:toggleActivity', 'let:lastEvidencePanel', 'function:keepOneCompactPanel',
      'on:onlineCodeMode:click', 'on:onlineCodeRepair:click', 'on:pcHeadless:click', 'on:pcSwitch:click',
      'on:fixInference:click', 'on:fixInferenceForm:submit',
      'on:fixInferenceDetails:keydown', 'on:fixInferenceClose:click',
      'on:onlineCodeDetails:keydown', 'on:onlineCodeClose:click',
      'on:vitalActivity:click', 'on:activityClose:click', 'on:activityDetails:keydown', 'on:drawerJumpButton:click',
      'on:drawerFixButton:click', 'on:memoryBannerDismiss:click', 'on:autoUnloadToggle:click',
      'doc:[data-model-scope]',
    ],
    replace: {
      ...overrides,
      ...feedRecovery,
      // The Mac chip only: no PC worker, lanes, route or activity feed.
      'function:vitalsInput': {
        sha256: '1a4a58ac1fbd59a6d63c867cd545cb443506a7d80d9b2dcc5f8589fdb2b35e3f',
        text: "function vitalsInput(){const rows=models(),hostLive=(snapshot?.sources||[]).some(s=>['lmstudio-api','lms-ps'].includes(s.id)&&s.state==='live');\n"
          + "  return{vitals:vitalsView({fresh:fresh(),models:rows,activityKnown:fresh()&&snapshot?.activityKnown===true,loadedKnown:hostLive&&rows.every(m=>typeof m.loaded==='boolean'),gpu:macGpuView(snapshot?.macGpu,{feedFresh:fresh(),snapshotAge:Math.max(0,sampleAge())})})};}",
      },
    },
    dropLines: {
      'function:runtimeGraph': [
        { from: '  const p=snapshot?.pipeline;', through: '  edge(root.id,pipeline.id,pipeline.color,true,0);' },
        { from: '  const jevC=', through: '  edge(pipeline.id,jevNode.id,jevNode.color,true,0);' },
        { from: "  if(snapshot?.host==='mac'){", through: '  }' },
        { from: '  clients.forEach((c,i)=>{', through: '  });' },
      ],
      'function:applyCamera': [{ from: "  const laneYs=graph.nodes.filter(n=>n.kind==='windows-lane')" }],
      'function:visibleMapArea': [{ from: '  // The memory banner' }, { from: '  const bannerBottom=' }],
    },
    edit: {
      'let:webKey': [["let webKey='',webLayout=null,webOverlayKey='',webBuilds=0,webPathEls=new Map(),webRest=new Map(),webTouched=new Set(),webDipped=new Set(),edgeGeoms=[];", 'let edgeGeoms=[];']],
      'let:webMotion': [["let webMotion=null,pulseKey='',flashTimer=null,glowKey='',glowZoom=NaN,webPlucking=false;", "let glowKey='',glowZoom=NaN;"]],
      'let:snapshot': [["view='runtime',modelScope='core',query", "view='runtime',query"]],
      'function:models': [["(snapshot?.host==='windows'?3:m.host==='windows'?30:3)", '3']],
      'function:runtimeGraph': [
        ['identityStationSlots(displayedModels().visible,modelStationSlots)', 'identityStationSlots(models(),modelStationSlots)'],
        ["  const clients=clientRows(),region=$('graphRegion');", "  const region=$('graphRegion');"],
        ['factoryLayout(Math.max(0,...placements.map(p=>p.slot+1)),clients.map(c=>({modelCount:c.models.length})),{', 'factoryLayout(Math.max(0,...placements.map(p=>p.slot+1)),[],{'],
        ["addNode('runtime',snapshot?.host==='windows'?'This PC':'This Mac',", "addNode('runtime',snapshot?.demo===true?'Sample Mac':'This Mac',"],
        ["subtitle:'LOCAL HUB',kind:'runtime'", "subtitle:snapshot?.demo===true?'SAMPLE HUB':'LOCAL HUB',kind:'runtime'"],
      ],
      'function:makeGraph': [["view==='runtime'?runtimeGraph():traceGraph();", 'runtimeGraph();']],
      'function:nodeSelect': [
        ['hideOnlineCodeDetails();hideFixInferenceDetails();hideActivityDetails();', ''],
        ["&&$('onlineCodeDetails').hidden&&$('fixInferenceDetails').hidden", ''],
      ],
      'function:renderGraph': [
        ["const web=view==='runtime'?null:renderWeb();if(view==='runtime'){$('web').replaceChildren();webLayout=null;webMotion?.stop();}", "const web=null;$('web').replaceChildren();"],
        ['webMotion?.redraw();', ''],
        ["view==='runtime'?observationRoute(a,b):edgeCurvePath(geometry)", 'observationRoute(a,b)'],
        ['[n.label,n.lane?.label,n.model?.id,n.client?.label,n.clientModel?.id,n.run?.runId,n.call?.model,n.call?.servedModel]',
          '[n.label,n.model?.id]'],
        ["(n.reviewLevel?' review-'+n.reviewLevel:'')", "''"],
        ["n.kind==='model'?n.model?.id:n.kind==='client-model'?n.clientModel?.id:null", "n.kind==='model'?n.model?.id:null"],
        ['[n.label,exactIdentity,n.subtitle,n.call?.status]', '[n.label,exactIdentity,n.subtitle]'],
      ],
      'function:renderGlows': [["['pipeline','jev'].includes(n.kind)?n.color:null", 'null']],
      'function:renderActivityMonitor': [
        ["heading=$('mapHeading').getBoundingClientRect(),banner=$('memoryBanner')", "heading=$('mapHeading').getBoundingClientRect()"],
        ['Math.max(heading.bottom,banner&&!banner.hidden?banner.getBoundingClientRect().bottom:0)', 'heading.bottom'],
      ],
      'function:visibleMapArea': [
        ["heading=$('mapHeading').getBoundingClientRect(),banner=$('memoryBanner')", "heading=$('mapHeading').getBoundingClientRect()"],
        ['Math.max(74,heading.bottom-box.top+16,bannerBottom)', 'Math.max(74,heading.bottom-box.top+16)'],
        ["[$('drawer'),$('onlineCodeDetails'),$('fixInferenceDetails'),$('activityDetails')]", "[$('drawer')]"],
      ],
      'function:applyCamera': [
        ["nodeCaptionLayout(n,scale,{laneGap:n.kind==='windows-worker'?Math.min(Infinity,...laneYs.map(y=>y-n.y).filter(d=>d>0)):Infinity})",
          'nodeCaptionLayout(n,scale)'],
        ["if(n.kind==='windows-worker')pcSubAbove=place.subAbove;", ''],
        ["graph.labels[i]?.kind==='windows'&&pcSubAbove?'hidden':'visible'", "'visible'"],
      ],
      'const:CAPTIONED': [["new Set(['runtime','client','windows-worker','windows-lane'])", "new Set(['runtime'])"]],
      'function:renderSummary': [["const identities=clientIdentityCounts();$('observedClientCount').textContent=`${identities.observed+identities.configured}/${identities.total}`;$('clientIdentityDetail').textContent=`${identities.observed} recorded · ${identities.configured} saved choices · ${identities.unknown} unknown`;", '']],
      'function:showWholeMap': [['hideOnlineCodeDetails();hideFixInferenceDetails();hideActivityDetails();', '']],
      // Sample data is labelled in the pill (ahead of LIVE) and in a badge that no width hides.
      'function:updateFreshness': [
        ['renderMemoryBanner();', ''],
        ['renderInferenceReceipts();', ''],
        ["$('connection').textContent=paused?'VIEW PAUSED':clockMismatch()?",
          "document.body.classList.toggle('sample-data',snapshot?.demo===true);$('sampleData').hidden=snapshot?.demo!==true;"
          + "$('connection').textContent=paused?'VIEW PAUSED':snapshot?.demo===true?'SAMPLE DATA':clockMismatch()?"],
        ["$('connection').className='connection '+(!paused&&fresh()?'live':'');",
          "$('connection').className='connection '+(!paused&&snapshot?.demo===true?'sample':!paused&&fresh()?'live':'');"],
        ['updateOnlineCodeMode();', ''],
      ],
      'function:setSidebarOpen': [["open?'Close runtime and client browser':'Open runtime and client browser'", "open?'Close model browser':'Open model browser'"]],
      'function:syncCompactUI': [['if(compact)keepOneCompactPanel();', '']],
      'function:render': [['renderModelScope();', '']],
      'on:sidebarToggle:click': [['if(opening)pollAutoUnload(true);', '']],
      // REST unload (SCOUT 1.9): the dock follows modelControl.unloadMode as well as `supported`.
      'function:modelActionState': [['models:snapshot?.models});', 'models:snapshot?.models,unloadMode:snapshot?.modelControl?.unloadMode});']],
      'on:closeDrawer:click': [["document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));", '']],
      'new:ResizeObserver': [['setInterval(pollOnlineCodeAction,1000);pollOnlineCodeAction();', '']],
    },
    append: [
      "$('openRuntimeApp').addEventListener('click',()=>native('open-runtime-app'));if(!window.webkit?.messageHandlers?.monitor)$('openRuntimeApp').hidden=true;",
    ],
  },
  'map-layout.mjs': {
    stripComments: true,
    drop: [
      'const:WEB_HUB_KINDS','const:WEB_TAU','const:webPoint','const:webRadius','const:webTurn','const:webAngleGap','const:webAlong','const:webCount','const:webBend','const:webBridgePath','const:webRingClass','const:webDashed','const:WEB_PLUCK','const:DIP_KEYS','function:webSpokesPath','function:webRingPath','function:orbWebKey','function:orbWebLayout','function:orbWebActivity','function:pluckWave','function:pokeScale','function:orbWebPluck','function:pluckOffsets','function:orbWebPathsAt','function:edgeCurvePath','function:orbWebFlashPath','function:createWebMotion',
      'function:hasAdvertisedWindowsWorker', 'function:windowsJobsView', 'const:LANE_IDS', 'const:CLIENT_LABEL',
      'const:slotCount', 'function:windowsLaneView', 'function:windowsLaneRows', 'function:reviewConsistencyView',
      'function:headlessSwitchView', 'const:LANE_TARGET', 'const:CLIENT_NAMES', 'const:capitalize', 'const:who',
      'const:rateText', 'const:JOB_FLAGS', 'const:FAILED_STATE', 'function:jobFlags', 'function:speedsText',
      'function:laneSpeeds', 'const:CALLER_NAME', 'function:localCallersView', 'const:WORKER_VERSION', 'const:GPU_NAME',
      'const:inRange', 'const:gib', 'function:pcGpuView', 'function:activityFeed', 'function:activitySummary',
      'const:CORE_MODEL_IDS', 'const:CORE_MODEL_SET', 'const:modelRowKey', 'const:lastObservedInUse',
      'stmt:|| (Number.isSafeInteger(row?.queued) &&', 'function:runtimeModelRoster',
      'function:afmView', 'function:jevView',
      'const:MEMORY_RANK', 'const:MEMORY_LABEL', 'const:MEMORY_TEXT', 'const:GIB', 'const:memoryBytes',
      'const:memoryText', 'const:isMemoryLevel', 'const:isAlertLevel', 'function:memorySize',
      'const:memoryAmount', 'const:approximately', 'const:CONSUMER_PREFIX', 'function:memoryView',
      'const:MEMORY_EASE_SECONDS', 'const:validHold', 'stmt:&& (held.below === null || Number.isFini',
      'function:memoryHoldAt', 'function:memoryHold', 'const:memoryDismissal', 'function:memoryAnnouncement',
      'const:BANNER_LABEL', 'const:bannerLabel', 'function:memoryBannerView', 'function:routeQueuedWords',
    ],
    replace: {
      'function:factoryLayout': {
        sha256: '3564084c43fd4db3842f3c6e6848f3f52ce62fb9ae4cca4b3ca135558fcff7c6',
        text: "export function factoryLayout(runtimeCount, clients, { viewportWidth = 820, viewportHeight = 640, localOnly = true } = {}) {\n  // Width determines station columns. A tall window must not collapse a usable wide grid.\n  const width = Number.isFinite(viewportWidth) ? Math.max(416, viewportWidth) : 820;\n  const cols = width < 620 ? 2 : 3, portrait = cols === 2;\n  const left = 110, centerX = left + (cols - 1) * FACTORY_STATION.column / 2;\n  const at = (slot, top) => ({ x: left + (slot % cols) * FACTORY_STATION.column,\n    y: top + Math.floor(slot / cols) * FACTORY_STATION.row });\n  const runtime = localOnly ? { x: centerX, y: 70 } : at(0, 70);\n  const modelTop = localOnly ? 220 : 70 + Math.ceil(7 / cols) * FACTORY_STATION.row + 100;\n  const runtimeModels = Array.from({ length: runtimeCount }, (_, i) => at(i, modelTop));\n  // Clients and their model identities use the same bounded-width production lanes.\n  // Each client starts a new row; its identities wrap without horizontal sprawl.\n  const labels = [\n    { text: `LOCAL MODELS · ${runtimeCount}`, x: centerX, y: modelTop - 62, align: 'middle', kind: 'local' },\n    { text: 'OBSERVATION LINKS · NOT TASK ROUTES', x: centerX, y: 12, align: 'middle', kind: 'mac' },\n  ];\n  return { runtime, runtimeModels, labels, portrait };\n}"},
      // The Mac chip's state and GPU figure; the private chips (PC, route, activity) are gone.
      'function:vitalsView': {
        sha256: '9c91f01b3f39d0a33763ae71f2ba67f639d9bb2e330ed2cafca9e2642c01d368',
        text: [
          'export function vitalsView({ fresh = false, models = [], activityKnown = false, loadedKnown = false, gpu = null } = {}) {',
          '  const active = fresh && activityKnown ? models.filter(m => m.loaded === true && ACTIVE_STATES.has(m.state)).length : 0, loaded = models.filter(m => m.loaded === true).length;',
          "  const mac = !fresh ? { tone: 'muted', detail: 'Signal stale' }",
          "    : active ? { tone: 'live', detail: `${active} generating` }",
          "      : { tone: 'ok', detail: `${loadedKnown ? `${loaded} loaded` : 'Loaded unknown'} · ${activityKnown ? 'idle' : 'activity unknown'}` };",
          '  if (fresh && gpu?.known) {',
          "    mac.short = `${active ? `${active} generating` : activityKnown ? 'Idle' : 'Activity unknown'} · ${gpu.chip}`;",
          '    mac.detail = `${mac.detail} · ${gpu.chip}`;',
          '  }',
          "  const tiny = !fresh ? {} : { mac: active ? `${active} generating` : activityKnown ? 'Idle' : 'Activity unknown' };",
          '  return { mac, lastAgeSeconds: null, tiny };',
          '}',
        ].join('\n'),
      },
    },
    dropLines: {
      'function:constellationLayout': [
        { from: '  const clientLanes = clients.map((client, index) => {', through: '  });' },
        { from: '  const pipeline = portrait', through: '    : [-1, 1].map(side =>' },
        { from: "    { text: 'NISI INFERENCE',", count: 2 },
        { from: "    { text: 'CLIENTS',", count: 2 },
      ],
    },
    edit: {
      'function:nodeCaptionLayout': [["node.kind === 'windows-worker' && below + 2 * scale + 3 > laneGap - 6 * scale", 'false']],
      'function:constellationLayout': [
        ["const hostLabel = host === 'windows' ? 'PC' : 'MAC';", "const hostLabel = 'MAC';"],
        ['pipeline, jev, afm, windows, windowsLanes, windowsLabel: { x: windows.x, y: windows.y - 28 }, clientLanes, ', ''],
      ],
    },
  },
  'model-control-view.mjs': {
    stripComments: true,
    edit: {
      'function:modelControlView': [
        ["host === 'windows' || ", '', 2],
        // REST unload (SCOUT 1.3 finding 3, 1.9, decision D-S2): this edition's rows come from the inventory only
        // (`lmstudio-api`, no activity), so with `unloadMode: "instance"` a loaded row whose one listed instance
        // is confirmed offers Unload, with the warning that activity is not visible; more than one instance
        // offers nothing. Same wording as the proposed upstream patch (.private-notes/web-ui-patches.md, item 3).
        ['models = [] } = {}) {', "models = [], unloadMode = null } = {}) {"],
        ["  if (model.state === 'stale')", [
          "  if (unloadMode === 'instance' && model.source === 'lmstudio-api' && model.loaded === true && model.state === 'loaded'",
          '    && model.modelKey === model.id && Array.isArray(model.loadedInstanceIds)) {',
          '    return model.loadedInstanceIds.length === 1',
          "      ? { action: 'unload', reason: 'Ready to unload. This edition cannot see whether the model is answering a request; unloading stops it.' }",
          "      : { action: null, reason: 'More than one instance is loaded; unload it in LM Studio.' };",
          '  }',
          "  if (model.state === 'stale')",
        ].join('\n')],
      ],
    },
  },
  'usage-format.mjs': { stripComments: true },
  'simple-view.mjs': { stripComments: true },
};

/** Selectors of removed markup: pruned from style.css (a selector list keeps its other selectors). */
export const css = {
  'style.css': {
    patterns: [
      /#monitorConnectionSwitches/,
      /#inference(Receipts|UnassignedQueue|Route|Request|Workflow)|\.inference-receipt/,
      /\.(agent-key|agent-color)(?![\w-])/,
      /#(onlineCode|fixInference|pcSwitch|pcHeadless|vitalPc|vitalRoute|vitalActivity|activity[A-Z]|observedClientCount|clientIdentityDetail|drawerJump|memoryBanner|modelScope|autoUnload)/,
      /\.(client(s|-[\w-]+)?|windows[\w-]*|pipeline|lanes?|run|call|run-[\w-]+|route-[\w-]+|host-[\w-]+|review-[\w-]+|mode-(tab|detail|action|repair)[\w-]*|fix-[\w-]+|pc-switch[\w-]*|activity-[\w-]+|scope-[\w-]+|segment[\w-]*|step-rows?|headless[\w-]*|auto-unload[\w-]*|model-scope[\w-]*|memory-[\w-]*|afm|jev)(?![\w-])/,
      /#(fixScopeNote|drawerFixButton)/,
      /\.(mode-v02[\w-]*|completion-evidence[\w-]*|view-tabs)(?![\w-])/,
    ],
    keyframes: [],
    append: [
      '/* App Store edition */',
      '.sample-badge{flex-shrink:0;padding:2px 8px;border:1px solid currentColor;border-radius:0;font:600 10px/1.5 ui-monospace,Menlo,monospace;letter-spacing:.6px;text-transform:uppercase;white-space:nowrap;color:var(--paper);background:var(--ink)}',
      '.connection.sample{color:var(--ink)}',
      '.tools #openRuntimeApp{padding:6px 9px;color:var(--muted);white-space:nowrap}',
      '@media(max-width:560px){.tools #openRuntimeApp{display:none}}',
      '.vitals{justify-content:flex-start}.vitals .vital{flex:0 1 auto}',
      'html[data-theme="dark"]{color-scheme:dark;--paper:#000;--panel:#000;--ink:#fff;--muted:#fff;--line:#fff;--wash:transparent;--orange:#fff;--blue:#fff;--green:#fff;--violet:#fff;--red:#fff;--shadow:none}',
      'html[data-theme="light"]{color-scheme:light;--paper:#fff;--panel:#fff;--ink:#000;--muted:#000;--line:#000;--wash:transparent;--orange:#000;--blue:#000;--green:#000;--violet:#000;--red:#000;--shadow:none}',
      '@media(prefers-color-scheme:light){html:not([data-theme="dark"]){color-scheme:light;--paper:#fff;--panel:#fff;--ink:#000;--muted:#000;--line:#000;--wash:transparent;--orange:#000;--blue:#000;--green:#000;--violet:#000;--red:#000;--shadow:none}}',
      '@media(prefers-color-scheme:dark){html:not([data-theme="light"]){color-scheme:dark;--paper:#000;--panel:#000;--ink:#fff;--muted:#fff;--line:#fff;--wash:transparent;--orange:#fff;--blue:#fff;--green:#fff;--violet:#fff;--red:#fff;--shadow:none}}',
      // style.css still carries the dark monitor's literal colours. These follow the paper/ink pair instead,
      // so the map, the live status and the compact face read the same in light and dark.
      '.connection.live,.eyebrow,.sidebar .eyebrow,.panel-eyebrow,.tile-label{color:var(--ink)}',
      '.vital,.vital:hover,.panel-summary,.tile{color:var(--ink);background:var(--paper);border-color:var(--line);box-shadow:none;backdrop-filter:none}',
      '.vital strong{color:var(--ink)}',
      '.cluster-label,.cluster-label.local{fill:var(--ink);stroke:var(--paper)}',
      '.node .label,.node .sub,.node.hub .label{fill:var(--ink);stroke:var(--paper)}',
      '.node .core{stroke:var(--paper)}',
      '.node.unknown .core{fill:var(--paper)}',
      '.graph-region{background:var(--paper)}',
      '.map-controls,.legend,.drawer,.sidebar-toggle{background:var(--paper);color:var(--ink)}',
      // Black and white only: the capability palette and glow are set as SVG attributes, which CSS overrides.
      '.edge,.node .halo,.node .ring,.node.unknown .core{stroke:var(--ink)}',
      '.node.hub .core{fill:var(--ink);filter:none}',
      '.glow{display:none}',
      '.legend .legend-line,.legend .cap-color,.legend-dot{background:var(--ink)}',
      // The map hint and its About panel belong to the Map view only.
      'html:not([data-screen="map"]) #footerNote,html:not([data-screen="map"]) #about,html:not([data-screen="map"]) #aboutPanel{display:none}',
      // An empty evidence rail draws no rule across the map.
      '.evidence-rail:not(:has(>:not([hidden]))){border:0;background:transparent}',
      'html #svBody{color:var(--ink);background:var(--paper)}',
      'html #svBody .sv-row{border-top-color:var(--line)}',
      'html #svBody button{color:var(--paper);background:var(--ink)}',
      'body{display:grid;grid-template-columns:11rem minmax(0,1fr);grid-template-rows:auto minmax(0,1fr) auto;height:100%;overflow:hidden}',
      'body>.topbar{grid-column:1/-1}body>.j-nav{grid-column:1;grid-row:2}body>main{grid-column:2;grid-row:2;height:auto;min-height:0}body>footer{grid-column:1/-1}',
      '.j-nav{display:flex;flex-direction:column;gap:4px;padding:12px;border-right:1px solid var(--line);background:var(--paper);font-family:Inter,"Liberation Sans","Noto Sans",sans-serif;font-weight:600;letter-spacing:.02em}',
      '.j-nav button{min-height:44px;text-align:left;color:inherit}',
      '.j-nav button[aria-current="page"]{text-decoration:underline}',
      '.j-theme{display:flex;flex-direction:column;gap:4px;margin-top:12px;font-weight:600}',
      '.j-theme select{min-height:44px;color:inherit;background:var(--paper);border:1px solid var(--line)}',
      '.edition-word{font-family:Inter,"Liberation Sans","Noto Sans",sans-serif;font-weight:600;letter-spacing:.02em;font-size:32px}',
      '#screenComponents,#screenIdentity{flex:1;overflow:auto;padding:28px 32px}',
      'html[data-screen="overview"] #graphRegion,html[data-screen="components"] .sidebar,html[data-screen="components"] #graphRegion,html[data-screen="identity"] .sidebar,html[data-screen="identity"] #graphRegion,html[data-screen="map"] .sidebar{display:none}',
      'html:not([data-screen="map"]) #drawer,html:not([data-screen="map"]) #evidenceRail{display:none}',
      '.topbar,body>footer{background:var(--paper);color:var(--ink);border-color:var(--line)}',
      '.brand-wordmark strong,.brand-wordmark small{color:var(--ink)}',
      '.sidebar{background:var(--paper);color:var(--ink)}',
      '.sidebar h1,.sidebar h1 em{color:var(--ink)}',
      '.sidebar .search-label,.sidebar .search-label input{background:var(--paper);color:var(--ink);border-color:var(--line)}',
      'html[data-screen="overview"] main{display:flex;min-width:0;min-height:0}',
      'html[data-screen="overview"] .sidebar{position:relative;transform:none;left:auto;top:auto;right:auto;bottom:auto;grid-column:1/-1;grid-row:1;width:auto;height:100%;max-height:100%;overflow:auto;flex:1;border:0;border-radius:0;box-shadow:none;background:var(--paper);color:var(--ink);z-index:1}',
      'html[data-screen="overview"] .sidebar h1,html[data-screen="overview"] .sidebar h1 em,html[data-screen="overview"] .sidebar .quiet,html[data-screen="overview"] .metrics span{color:var(--ink)}',
      'html[data-screen="map"] #graphRegion{position:relative;inset:auto;grid-column:1/-1;display:block;height:100%;min-height:0;background:var(--paper);color:var(--ink)}',
      '#screenComponents,#screenIdentity{grid-column:1/-1;height:100%;overflow:auto;background:var(--paper);color:var(--ink)}',
      '#svBody{display:none}',
      'html[data-view="simple"] #svBody{display:flex;grid-column:1;grid-row:2;margin:0;padding:16px 28px;min-height:0;overflow:auto}',
      'html[data-view="simple"] .j-nav,html[data-view="simple"] main,html[data-view="simple"] footer{display:none}',
      'html[data-view="simple"] body{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr)}',
      '.topbar,.j-nav,body>footer,main{min-width:0}',
      '@media(max-width:979px){body{grid-template-columns:1fr;grid-template-rows:auto auto minmax(0,1fr) auto;overflow-x:hidden}body>.j-nav{grid-column:1;grid-row:2;flex-direction:row;flex-wrap:wrap;align-items:stretch;overflow:visible;border-right:0;border-bottom:1px solid var(--line)}body>main{grid-column:1;grid-row:3}.j-nav button{flex:1 1 8rem;text-align:left}.j-theme{flex:1 1 100%;flex-direction:row;align-items:center;margin:0;gap:8px}.j-theme select{flex:1;min-width:0}}',
      '@media(max-width:22.5rem){#screenComponents,#screenIdentity{padding:16px 12px}html[data-view="simple"] #svBody{padding:12px}.edition-word{font-size:24px}.j-nav{padding:8px}.topbar{height:auto;min-height:44px;padding:8px 10px}.brand-wordmark small{display:none}.tools{flex-wrap:wrap}}',
      '@media(prefers-reduced-motion:reduce){.j-nav button,.sidebar{transition:none}}',
    ].join('\n'),
  },
};
