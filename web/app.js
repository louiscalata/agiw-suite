import {formatTokens,summarizeReportedCalls} from '/usage-format.mjs';
import {onlineCodeModeView,routeQueueRows,nisiV02View,fixNisiStepsView,nisiRecoveryView,fixNisiEvidence,fixNisiMessage} from '/online-code-mode.mjs';
import {constellationLayout,fitGraph,prioritizeRuntimeModels,runtimeModelRoster,CORE_MODEL_IDS,hasAdvertisedWindowsWorker,afmView,jevView,modelStarSize,windowsJobsView,windowsLaneView,resolveLabelCollisions,clampCamera,nextNodeInDirection,reviewConsistencyView,headlessSwitchView,activityFeed,vitalsView,windowsLaneRows,nodeCaptionLayout,macGpuView,localCallersView,laneSpeeds,speedsText,jobFlags,activitySummary,pcGpuView,orbWebKey,orbWebLayout,orbWebActivity,orbWebPluck,orbWebPathsAt,orbWebFlashPath,pluckOffsets,createWebMotion,edgeCurvePath,webGlows,glowRadius,memoryView,memoryBannerView,memoryDismissal,memoryHoldAt,memoryAnnouncement,routeQueuedWords,macPeerView} from '/map-layout.mjs';
import {modelControlView,submitModelControl} from '/model-control-view.mjs';
const $=id=>document.getElementById(id), NS='http://www.w3.org/2000/svg';
const ACTIVE=new Set(['generating','busy']);
const COLORS=['#369984','#4c7cba','#8471af','#ca8b46','#a76882','#5f8c9d'];
const STATE={generating:'Generating',busy:'Busy',idle:'Idle',loaded:'Activity unknown',unloaded:'Not loaded',unknown:'Unknown',stale:'Stale'};
// LM Studio reports activity phases, not a completion percentage. These cadences follow only the observed phase.
const MODEL_AURA_MS={busy:2400,generating:1200};
const CLIENTS=[['codex','Codex'],['claude','Claude'],['opencode','OpenCode'],['cursor','Cursor'],['grok','Grok']];
// Client hues identify the app. A hollow node still means activity is unknown.
const CLIENT_COLORS={codex:'#70bbff',claude:'#ffae73',opencode:'#65e3b2',cursor:'#b99aff',grok:'#ff91c1'};
const CLIENT_STATE={observed:'Recorded model',configured:'Saved session choice',unknown:'Model unknown'};
let snapshot=null,connected=false,paused=false,view='runtime',modelScope='core',query='',selected=null,runId=null,modelControlStatus=null,modelControlError='',modelControlPosting=false;
let autoUnloadState=null,autoUnloadError='',autoUnloadPosting=false,autoUnloadPolling=false,autoUnloadRevision=0;
let onlineCodeActionStatus=null,onlineCodeActionPosting=false,onlineCodeActionPolling=false,onlineCodeActionRevision=0,onlineCodeSubmittingAction=null;
let fixInferenceRenderKey='',fixReturnTo=null;
const FIX_ACTIONS=new Map([['fix-local','Local runtime'],['fix-route','Route pipeline'],['fix-both','Local runtime and route'],['fix-nisi','Nisi Inference'],['fix-all','Inference']]);
const HEADLESS_ACTIONS=new Map([['headless-on','on'],['headless-off','off']]);
const LANE_COLORS={fast:'#7fc8e8',deep:'#a99be0'};
let graph={nodes:[],edges:[],labels:[]},camera={x:0,y:0,z:1},userCamera=false,drag=null,justDragged=false,inspectorNode=null,inspectorShape='',paintCamera='',layoutMode='wide',hoveredNode=null;
const nodeElements=new Map();
// Moving the focused node into a fresh viewport fires blur and focus; those must not re-render mid-move.
let reparenting=false;
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const svg=(tag,attrs={})=>{const n=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;};
const ageText=n=>!Number.isFinite(n)?'Age unknown':n< -1?'Clock mismatch':n<3?'Just sampled':n<60?`${Math.floor(n)}s ago`:n<3600?`${Math.floor(n/60)}m ago`:n<86400?`${Math.floor(n/3600)}h ago`:`${Math.floor(n/86400)}d ago`;
const name=id=>String(id).split('/').pop().replaceAll('-',' ').replace(/\b(gemma|qwen)\b/gi,s=>s[0].toUpperCase()+s.slice(1)).replace(/\bgpt\b/gi,'GPT').replace(/\boss\b/gi,'OSS').replace(/^./,s=>s.toUpperCase());
const decisionText=d=>typeof d==='string'?d:d&&typeof d==='object'?[d.choice,d.routeState,Number.isFinite(d.confidence)?'Classifier confidence '+Math.round(d.confidence*100)+'%':null].filter(Boolean).join(' · '):null;
const trim=(text,max=27)=>text.length>max?text.slice(0,max-1)+'…':text;
// Map captions use the family and size ("Gemma 4", "Qwen3.8 27b"); the inspector keeps the full ID.
const shortName=id=>trim(name(id).split(' ').slice(0,2).join(' '),12);
// Row ages follow sampledAt (the fast path re-reads its rows at that moment); feed freshness follows the
// last full sample, which a fast-path overlay never re-stamps.
const sampleAge=()=>snapshot&&Number.isFinite(snapshot.sampledAt)?Date.now()/1000-snapshot.sampledAt:NaN;
function feedAge(data,now){const at=data?.fullSampledAt??data?.sampledAt;return Number.isFinite(at)?now-at:NaN;}
const clockMismatch=()=>connected&&feedAge(snapshot,Date.now()/1000) < -1;
const fresh=()=>{const age=feedAge(snapshot,Date.now()/1000);return connected&&age>=-1&&age<=3;};
// Only a fresh feed that is not paused may say something is running now.
const liveNow=()=>fresh()&&!paused;
function modelActivity(m){const active=liveNow()&&snapshot?.activityKnown===true&&m.loaded===true&&ACTIVE.has(m.state);return{active,periodMs:active?MODEL_AURA_MS[m.state]:null};}
const modelObservationAge=m=>Number.isFinite(m.ageSeconds)&&Number.isFinite(sampleAge())?ageText(m.ageSeconds+Math.max(0,sampleAge())):'Age unknown';
const runs=()=>snapshot?.activity?.runs||[];
const SUBAGENT_UNKNOWN=Object.freeze({state:'unknown',active:null,ageSeconds:null,source:'No verified subagent lifecycle feed connected'});
const runKey=r=>r.traceKey||(r.source||'route')+':'+r.runId;
const runSource=r=>({'monitor-receipt':'Saved receipt','router-archive':'Router archive','router-active':'Current checkpoint'}[r.source]||'Route record');
const routeLabel=r=>r.status==='RESPONSE_VALIDATED'?'Response validation: RESPONSE_VALIDATED':'Route status: '+(r.status||'Unknown');
const validDuration=value=>Number.isSafeInteger(value)&&value>=0;
function durationText(value){if(!validDuration(value))return'Unknown';if(value<1000)return`${value} ms`;if(value<60000)return`${(value/1000).toFixed(2)} s`;return`${Math.floor(value/60000)}m ${((value%60000)/1000).toFixed(1)}s`;}
function runSummary(run){const summary=summarizeReportedCalls(Array.isArray(run.calls)?run.calls:[]);return{count:summary.callCount,timed:summary.timedCallCount,tokenKnown:summary.tokenKnownCount,duplicateIdentity:summary.duplicateIdentity,tokens:formatTokens(summary.totalTokens),duration:durationText(summary.elapsedMs)};}
function identifier(node,value){node.replaceChildren();for(const part of String(value).split(/([._:/-])/)){node.append(document.createTextNode(part));if(/^[._:/-]$/.test(part))node.append(el('wbr'));}node.title=String(value);}
function models(){const elapsed=snapshot?Math.max(0,Date.now()/1000-snapshot.sampledAt):0;return(snapshot?.models||[]).map(m=>!fresh()||(Number.isFinite(m.ageSeconds)&&m.ageSeconds+elapsed>(snapshot?.host==='windows'?3:m.host==='windows'?30:3))?{...m,state:'stale',loaded:null,queued:null}:m);}
function displayedModels(){return runtimeModelRoster(models(),{scope:modelScope,observedRows:snapshot?.models});}
function clientRows(){const input=Array.isArray(snapshot?.clients)?snapshot.clients:[];return CLIENTS.map(([id,label])=>{const entry=input.find(c=>c?.id===id)||{},seen=new Set();const raw=Array.isArray(entry.models)&&entry.models.length?entry.models:entry.model?[{id:entry.model,modelState:entry.modelState,observedAt:entry.observedAt,ageSeconds:entry.ageSeconds,source:entry.source}]:[];const recent=raw.filter(m=>{if(typeof m?.id!=='string'||!m.id.trim()||seen.has(m.id))return false;seen.add(m.id);return true;}).slice(0,3).map(m=>({...m,modelState:['observed','configured'].includes(m.modelState)?m.modelState:'unknown',activity:'unknown'}));return{id,label,model:typeof entry.model==='string'&&entry.model.trim()?entry.model:null,modelState:['observed','configured'].includes(entry.modelState)?entry.modelState:'unknown',observedAt:Number.isFinite(entry.observedAt)?entry.observedAt:null,ageSeconds:Number.isFinite(entry.ageSeconds)?entry.ageSeconds:null,activity:'unknown',source:typeof entry.source==='string'&&entry.source?entry.source:'Unavailable',detail:typeof entry.detail==='string'&&entry.detail?entry.detail:'No client model identity available.',models:recent,subagents:SUBAGENT_UNKNOWN};});}
function clientIdentityCounts(){const rows=clientRows(),observed=rows.filter(c=>c.models.some(m=>m.modelState==='observed')).length,configured=rows.filter(c=>!c.models.some(m=>m.modelState==='observed')&&c.models.some(m=>m.modelState==='configured')).length;return{observed,configured,unknown:rows.length-observed-configured,total:rows.length};}
const clientAge=c=>Number.isFinite(c.observedAt)?(c.observedAt>Date.now()/1000?'Clock mismatch':ageText(Date.now()/1000-c.observedAt)):Number.isFinite(c.ageSeconds)?ageText(c.ageSeconds+Math.max(0,Date.now()/1000-snapshot.sampledAt)):'Unknown';
function native(action){const bridge=window.webkit?.messageHandlers?.monitor;if(bridge)bridge.postMessage({action});else if(action==='expand')window.open(location.href,'_blank','noopener');}
function addNode(id,label,x,y,options={}){const n={id,label,x,y,color:'#88887e',r:7,subtitle:'',...options};graph.nodes.push(n);return n;}
function edge(a,b,color,dim=false,bend=0,flow=false){graph.edges.push({a,b,color,dim,bend,flow});}
function addLabel(label){graph.labels.push(label);}
function runtimeGraph(){
  const {visible:rows}=prioritizeRuntimeModels(displayedModels().visible);
  const clients=clientRows(),region=$('graphRegion');
  const layout=constellationLayout(rows.length,clients.map(c=>({modelCount:c.models.length})),{viewportWidth:region.clientWidth||409,viewportHeight:region.clientHeight||800,host:snapshot?.host});
  layoutMode=layout.portrait?'portrait':'wide';
  layout.labels.forEach(addLabel);
  const root=addNode('runtime',snapshot?.host==='windows'?'This PC':'This Mac',layout.runtime.x,layout.runtime.y,{root:true,hub:true,r:18,color:'#d7d7d7',subtitle:'LOCAL HUB',kind:'runtime'});
  rows.forEach((m,i)=>{
    const pos=layout.runtimeModels[i];
    const activity=modelActivity(m);
    const visual=modelStarSize(m);
    // Windows lanes: tokens/s from the change in the slots' decoded counts between two polls of the same request.
    const liveRate=activity.active&&m.host==='windows'&&Number.isFinite(m.metadata?.liveTokensPerSecond)&&m.metadata.liveTokensPerSecond>0?m.metadata.liveTokensPerSecond:null;
    const subtitle=paused&&ACTIVE.has(m.state)?`At pause: ${STATE[m.state]}`:ACTIVE.has(m.state)&&!activity.active?'Activity unverified':liveRate!==null?`${STATE[m.state]} · ${Math.round(liveRate)} tok/s`:STATE[m.state]||'Unknown';
    const n=addNode(`model:${m.host}:${m.id}`,shortName(m.id),pos.x,pos.y,{color:visual.color,r:visual.radius,subtitle,kind:'model',model:m,visual,active:activity.active,auraPeriodMs:activity.periodMs,unknown:['unloaded','unknown','stale','loaded'].includes(m.state)});
    edge(root.id,n.id,n.color,n.unknown,(i%2?1:-1)*22);
  });
  // Neighbouring loaded models in one row alternate their always-on captions below and above.
  let previous=null;for(const n of graph.nodes.filter(n=>n.kind==='model'&&n.model?.loaded===true).sort((a,b)=>a.y-b.y||a.x-b.x)){if(previous&&!previous.captionAbove&&Math.abs(previous.y-n.y)<1&&n.x-previous.x<110)n.captionAbove=true;previous=n;}
  const p=snapshot?.pipeline;
  const nisiC=Array.isArray(snapshot?.components)?snapshot.components.find(c=>c.id==='nisi'):null;
  const readiness=nisiC?.state==='ready'?' · PAIR READY':nisiC?.state==='partial'?' · NEEDS 2ND MODEL':nisiC?.state==='needs-action'?' · NEEDS ACTION':'';
  // Router concurrency: several routes may run at once; the node counts them (its inspector lists each one).
  const runningRoutes=routePipelines().filter(r=>r.live&&r.status==='running').length;
  const pipeline=addNode('pipeline','Nisi Inference',layout.pipeline.x,layout.pipeline.y,{color:'#8471af',r:9,subtitle:!fresh()?'ROUTE AGE UNKNOWN':p?.status==='installing'?'ROUTER INSTALL IN PROGRESS':p?.status==='running'&&runningRoutes>1?`ROUTES RUNNING · ${runningRoutes}`:p?.status==='running'?`ROUTE RUNNING · ${String(p.stage||'').replaceAll('_',' ').toUpperCase()}`:p?.status==='queued'?`ROUTE ${routeQueuedWords(p).toUpperCase()}`:p?.runId?'UNSETTLED RUN RECORDED':p?.status==='idle'?`ROUTE IDLE${readiness}`:p?.status==='recovery-required'||nisiC?.state==='unresolved'?'NISI RECORD PENDING':'ROUTE STATE UNKNOWN',kind:'pipeline',active:fresh()&&p?.status==='running',unknown:!p?.runId&&nisiC?.state!=='ready'});
  edge(root.id,pipeline.id,pipeline.color,true,0);
  const jevC=Array.isArray(snapshot?.components)?snapshot.components.find(c=>c.id==='jev'):null;
  const jev=jevView(jevC,{feedFresh:fresh(),snapshotAge:Math.max(0,sampleAge())});
  const jevSubtitle=jev.state==='unknown'?'STATUS UNKNOWN · HISTORY ONLY':jev.lastJudgedAgeSeconds!==null?`LAST JUDGMENT ${ageText(jev.lastJudgedAgeSeconds).toUpperCase()}`:jev.state==='configured'?'OPTED IN · NO JUDGMENT':'NO OPT-IN · NO JUDGMENT';
  const jevNode=addNode('jev','Jev',layout.jev.x,layout.jev.y,{color:'#9b81c9',r:6,subtitle:jevSubtitle,kind:'jev',jev,captionSide:layout.portrait?'right':null,unknown:true});
  edge(pipeline.id,jevNode.id,jevNode.color,true,0);
  if(snapshot?.host==='mac'){
    const afm=afmView(snapshot.afm,{feedFresh:fresh(),host:snapshot.host});
    const afmNode=addNode('afm','AFM',layout.afm.x,layout.afm.y,{color:'#77c6ac',r:8,subtitle:afm.state==='executable'?'ADAPTER EXECUTABLE · INFERENCE UNTESTED':afm.label.toUpperCase(),kind:'afm',afm,unknown:afm.state!=='executable'});
    edge(root.id,afmNode.id,afmNode.color,true,-12);
    const worker=snapshot.windowsWorker||{state:'unknown',detail:'Windows worker heartbeat not yet verified'};
    const age=Math.max(0,sampleAge()),heartbeatAge=Number.isFinite(worker.ageSeconds)?worker.ageSeconds+age:null;
    const heartbeatFresh=fresh()&&['advertised','degraded','stopped'].includes(worker.state)&&heartbeatAge!==null&&heartbeatAge>=0&&heartbeatAge<=60;
    const advertised=hasAdvertisedWindowsWorker(worker,{feedFresh:fresh(),snapshotAge:age});
    const rawJobs=snapshot.windowsJobs,journalKnown=rawJobs?.schemaVersion===1&&Array.isArray(rawJobs.inFlight)&&Array.isArray(rawJobs.recent);
    const jobs=windowsJobsView(journalKnown?rawJobs:null,{feedFresh:fresh(),snapshotAge:age}),job=jobs.inFlight[0],last=jobs.current?jobs.lastVerified:null;
    const condition=heartbeatFresh&&['degraded','stopped'].includes(worker.state)?worker.state:null;
    const subtitle=jobs.running?`JOB IN FLIGHT · ${trim(name(job.model||'default model'),18)} · ${Math.floor(job.ageSeconds)}s`:condition==='degraded'?'WORKER DEGRADED · NO MODEL LANE':condition==='stopped'?'WORKER STOPPED':last?`LAST INFERENCE · ${trim(name(last.model),18)} · ${last.elapsedSeconds.toFixed(1)}s`:advertised?'WORKER ADVERTISED · INFERENCE UNKNOWN':'WORKER UNVERIFIED · INFERENCE UNKNOWN';
    const lanes=windowsLaneView(worker,jobs,{feedFresh:fresh(),snapshotAge:age}),visibleLanes=heartbeatFresh&&lanes.visible,headlessKnown=heartbeatFresh&&lanes.headless.label!=='Headless unknown';
    // Flipped above the hub (zoomed out), the subtitle shares its band with the Mac hub's captions and the
    // nearest client branch, so it keeps only the status and time; the lane captions name the models.
    const brief=jobs.running?`JOB IN FLIGHT · ${Math.floor(job.ageSeconds)}s`:condition==='degraded'?'WORKER DEGRADED':condition==='stopped'?'WORKER STOPPED':last?`LAST ANSWER · ${last.elapsedSeconds.toFixed(1)}s`:advertised?'WORKER ADVERTISED':'WORKER UNVERIFIED';
    const node=addNode('windows-worker','Windows PC',layout.windows.x,layout.windows.y,{color:'#96acd5',r:12,subtitle:headlessKnown?`${subtitle} · ${lanes.headless.label.toUpperCase()}`:subtitle,subtitleAbove:brief,kind:'windows-worker',windowsWorker:worker,workerAdvertised:advertised,windowsJobs:jobs,windowsLanes:lanes,inFlight:jobs.running,verified:!jobs.running&&Boolean(last),unknown:!jobs.running&&!last});
    edge(root.id,node.id,node.color,!jobs.running,18,jobs.running);
    if(visibleLanes)lanes.lanes.forEach((lane,i)=>{const live=lanes.edges.some(e=>e.lane===lane.id&&e.live),offset=i?1:-1,pos=layout.windowsLanes[i]||layout.windowsLanes[0],n=addNode(`windows-lane:${lane.id}`,trim(lane.label,22),pos.x,pos.y,{color:LANE_COLORS[lane.id],r:7,subtitle:!lane.up?'DOWN':lane.total!==null?`${lane.busy}/${lane.total} BUSY`:'UP · SLOTS UNKNOWN',kind:'windows-lane',captionSide:layout.portrait&&region.clientWidth<520?'left':i?'right':'left',lane,headless:lanes.headless,active:lane.up&&lane.busy>0,inFlight:live,unknown:!lane.up});
      edge(node.id,n.id,n.color,!lane.up,offset*10,live);});
    if(visibleLanes)lanes.edges.forEach((e,i)=>{if(Object.hasOwn(CLIENT_COLORS,e.client))edge(`client:${e.client}`,`windows-lane:${e.lane}`,CLIENT_COLORS[e.client],!e.live,(i%2?1:-1)*24,e.live);});
    // Cluster headers name regions; the PC chip and node carry changing evidence.
    addLabel({text:'WINDOWS PC',x:layout.windowsLabel.x,y:layout.windowsLabel.y,align:'middle',kind:'windows'});
  }
  if(snapshot?.host==='windows'){
    // Windows edition: this PC's lanes are the local models above; the Mac is a LAN peer in the second constellation.
    const peer=macPeerView(snapshot.macPeer,{feedFresh:fresh(),snapshotAge:Math.max(0,sampleAge())});
    const node=addNode('mac-peer','Mac',layout.windows.x,layout.windows.y,{color:'#96acd5',r:12,subtitle:peer.subtitle,subtitleAbove:peer.brief,kind:'mac-peer',macPeer:peer,unknown:!peer.reachable});
    edge(root.id,node.id,node.color,!peer.reachable,18);
    peer.satellites.forEach((m,i)=>{const pos=layout.windowsLanes[i]||layout.windowsLanes[0],n=addNode(`mac-model:${i}`,shortName(m.id),pos.x,pos.y,{color:'#a99be0',r:7,subtitle:'LOADED ON MAC',kind:'mac-model',macModel:m,macPeer:peer,captionSide:layout.portrait&&region.clientWidth<520?'left':i?'right':'left'});
      edge(node.id,n.id,n.color,true,(i?1:-1)*10);});
    addLabel({text:'MAC · LAN PEER',x:layout.windowsLabel.x,y:layout.windowsLabel.y,align:'middle',kind:'mac-peer'});
  }
  clients.forEach((c,i)=>{const lane=layout.clientLanes[i],color=CLIENT_COLORS[c.id],parent=addNode(`client:${c.id}`,c.label,lane.x,lane.y,{root:true,r:9,color,subtitle:'SUBAGENT ACTIVITY UNKNOWN',kind:'client',client:c,unknown:!c.models.length});edge(root.id,parent.id,parent.color,true,(i-1)*28);
    if(!c.models.length){const pos=lane.models[0],unknown=addNode(`client-model:${c.id}:unknown`,'Unknown model',pos.x,pos.y,{color,r:5,subtitle:'UNKNOWN',kind:'client-model',client:c,clientModel:null,unknown:true});edge(parent.id,unknown.id,unknown.color,true);}
    c.models.forEach((m,j)=>{const pos=lane.models[j],n=addNode(`client-model:${c.id}:${j}`,trim(name(m.id),12),pos.x,pos.y,{color,r:6,subtitle:(CLIENT_STATE[m.modelState]||'Model unknown').toUpperCase(),kind:'client-model',client:c,clientModel:m,unknown:m.modelState==='unknown'});edge(parent.id,n.id,n.color,true,(j-(c.models.length-1)/2)*16);});
  });
}
function traceGraph(){
  const records=runs();if(!records.some(r=>runKey(r)===runId))runId=records[0]?runKey(records[0]):null;
  const run=records.find(r=>runKey(r)===runId);if(!run)return;
  const root=addNode('run:'+runKey(run),'Nisi Inference',440,330,{root:true,r:18,color:'#f06b24',subtitle:run.hostAcceptance==='REJECTED'?'HOST ACCEPTANCE: REJECTED':reviewConsistencyView(run).badge?reviewConsistencyView(run).badge.toUpperCase():run.status==='RESPONSE_VALIDATED'?'RESPONSE VALIDATION ONLY':(run.status||'UNKNOWN').toUpperCase(),kind:'run',run,reviewLevel:reviewConsistencyView(run).level});
  const calls=Array.isArray(run.calls)?run.calls:[];
  // Each edge groups a receipt under its recorded run. It never fabricates a causal
  // dependency between calls, nor claims a checkpoint is a currently running model.
  calls.slice(0,16).forEach((call,i)=>{
    const angle=(-115+i*(calls.length>1?255/(calls.length-1):0))*Math.PI/180;
    const id=`call:${runKey(run)}:${call.id||'unreported'}:${i}`;
    const model=call.servedModel||call.model||call.requestedModel||'Unreported model';
    const n=addNode(id,trim(name(model)),440+Math.cos(angle)*235,330+Math.sin(angle)*235,{r:9,color:COLORS[i%COLORS.length],subtitle:(call.role||call.stage||'call').toUpperCase()+' RECEIPT',kind:'call',call,run});
    edge(root.id,n.id,n.color);
    if(call.role==='intake'&&call.decision?.choice){const d=addNode(id+':decision',trim(call.decision.choice),n.x-145,n.y+45,{r:4,color:n.color,subtitle:'ROUTE ADVICE',kind:'call',call,run});edge(n.id,d.id,n.color);}
  });
  if(!calls.length){const n=addNode('checkpoint',run.stage||'Checkpoint',680,330,{color:'#8471af',subtitle:'EXECUTION UNKNOWN',kind:'run',run,unknown:true});edge(root.id,n.id,n.color,true);}
}
function makeGraph(){graph={nodes:[],edges:[],labels:[]};hoveredNode=null;view==='runtime'?runtimeGraph():traceGraph();}
function compactEvidence(){return window.matchMedia?.('(max-width: 820px)').matches===true;}
function focusNode(id){const g=id&&nodeElements.get(id);if(!g)return;g.focus({preventScroll:true});revealNode(id);}
function revealNode(id){const n=graph.nodes.find(node=>node.id===id);if(!n)return;const area=visibleMapArea(),x=camera.x+n.x*camera.z,y=camera.y+n.y*camera.z,m=48;if(x>=area.left+m&&x<=area.right-m&&y>=area.top+m&&y<=area.bottom-m)return;camera.x+=Math.max(area.left+m,Math.min(area.right-m,x))-x;camera.y+=Math.max(area.top+m,Math.min(area.bottom-m,y))-y;userCamera=true;applyCamera();}
function graphBounds(){const points=[...graph.nodes,...graph.labels];if(!points.length)return null;const xs=points.map(p=>p.x),ys=points.map(p=>p.y);return{minX:Math.min(...xs),maxX:Math.max(...xs),minY:Math.min(...ys),maxY:Math.max(...ys)};}
function boundCamera(){camera=clampCamera(camera,graphBounds(),visibleMapArea(),{keep:120});}
function hideOnlineCodeDetails(){const panel=$('onlineCodeDetails');panel.hidden=true;$('onlineCodeMode').setAttribute('aria-expanded','false');}
function hideFixInferenceDetails(){const panel=$('fixInferenceDetails');panel.hidden=true;fixReturnTo=null;$('fixInference').setAttribute('aria-expanded','false');}
function closeDrawer(restoreFocus=true){const previous=selected;$('drawer').hidden=true;selected=null;inspectorNode=null;userCamera=false;renderGraph();renderList();fitMap();if(restoreFocus)nodeElements.get(previous)?.focus({preventScroll:true});}
// Below the dock breakpoint, Browse covers the map. A choice closes it and
// returns focus to the chosen map node before the inspector replaces it.
function closeCompactSidebar(){if(dockedSidebar()||!document.body.classList.contains('sidebar-open'))return false;const inside=$('runtimeSidebar').contains(document.activeElement);setSidebarOpen(false);return inside;}
function nodeSelect(id){let refocus=false;if(compactEvidence()){hideOnlineCodeDetails();hideFixInferenceDetails();hideActivityDetails();refocus=closeCompactSidebar();}else if(!dockedSidebar())refocus=closeCompactSidebar();const fromDockedBrowser=dockedSidebar()&&$('runtimeSidebar').contains(document.activeElement),fitForDrawer=!userCamera&&$('drawer').hidden&&$('onlineCodeDetails').hidden&&$('fixInferenceDetails').hidden;if(selected!==id)modelControlError='';selected=id;inspectorNode=null;$('drawer').hidden=false;renderGraph();renderInspector();renderList();if(fitForDrawer)fitMap();else revealSelected();userCamera=true;if(fromDockedBrowser)$('drawer').focus({preventScroll:true});if(refocus)nodeElements.get(id)?.focus({preventScroll:true});}
// Orb web (spiderweb, 26-27 Sep): geometry only. It is rebuilt when a node moves or the topology changes, never on
// hover or on a stream tick that only changes status (#web's data-builds counts rebuilds). The reduced-motion dew is a
// separate overlay, cached the same way; a poke's highlight has its own path.
let webKey='',webLayout=null,webOverlayKey='',webBuilds=0,webPathEls=new Map(),webRest=new Map(),webTouched=new Set(),webDipped=new Set(),edgeGeoms=[];
let webMotion=null,pulseKey='',flashTimer=null,glowKey='',glowZoom=NaN,webPlucking=false;
const reducedMotion=()=>window.matchMedia?.('(prefers-reduced-motion: reduce)').matches===true,WEB_MINOR_ZOOM=.6;
const forcedColors=()=>window.matchMedia?.('(forced-colors: active)').matches===true;
function webHidden(){try{return window.getComputedStyle?.($('web')).display==='none';}catch(_){return false;}}
function renderWeb(){
  const layer=$('web');if(!layer)return null;
  if(!graph.web){const key=orbWebKey(graph.nodes,graph.edges);if(key!==webKey||!webLayout){webMotion?.stop();webKey=key;webLayout=orbWebLayout(graph.nodes,graph.edges);webOverlayKey='';pulseKey='';webPathEls=new Map();webRest=new Map();webTouched=new Set();
    const threads=webLayout.paths.map(p=>{const path=svg('path',{class:p.className,d:p.d});webPathEls.set(p.key,path);webRest.set(p.key,p.d);return path;});
    layer.replaceChildren(...threads,svg('g',{class:'web-live'}),svg('path',{class:'web-flash',d:''}));layer.dataset.paths=String(webLayout.pathCount);layer.dataset.builds=String(++webBuilds);}graph.web=webLayout;}
  // Live means fresh and unpaused. Only reduced motion marks live threads with static dew; with motion allowed the halo, the glow and
  // the heartbeat wobble say a job is in flight, and nothing travels along a thread.
  const activity=orbWebActivity(graph.web,{live:liveNow()?graph.nodes.filter(n=>n.active||n.inFlight).map(n=>n.id):[],motion:!reducedMotion()});
  if(activity.key!==webOverlayKey){webOverlayKey=activity.key;layer.querySelector('.web-live')?.replaceChildren(...(activity.dewPath?[svg('path',{class:'web-dew',d:activity.dewPath})]:[]));}
  return graph.web;
}
// Background glow: one soft radial glow per pulsing node, under the web. Rebuilt only when the set of pulsing nodes (or where they
// sit) changes; applyCamera only re-clamps the radius on zoom. Model glow cadence follows its observed activity phase.
// Forced colours hide the layer, so none are built there (a hidden glow would still run its animation).
function renderGlows(){
  const layer=$('glows');if(!layer)return;const {glows,key}=webGlows(graph.nodes,{fresh:fresh()&&!forcedColors()});if(key===glowKey)return;glowKey=key;glowZoom=camera.z;
  const radius=glowRadius(camera.z),colors=[...new Set(glows.map(g=>g.color))],defs=svg('defs');
  colors.forEach((color,i)=>{const gradient=svg('radialGradient',{id:`web-glow-${i}`});gradient.append(svg('stop',{offset:'0','stop-color':color}),svg('stop',{offset:'.4','stop-color':color,'stop-opacity':'.35'}),svg('stop',{offset:'1','stop-color':color,'stop-opacity':'0'}));defs.append(gradient);});
  layer.replaceChildren(...(glows.length?[defs,...glows.map(g=>svg('circle',{class:`glow ${g.tone}`,cx:g.x,cy:g.y,r:radius,fill:`url(#web-glow-${colors.indexOf(g.color)})`,'data-node':g.id,style:g.periodMs?`animation-duration:${g.periodMs}ms`:''}))]:[]));
  syncBeat();
}
// Halos and their background glows start on the same boundary of their own measured-phase cadence.
// Other live nodes and the dormant web wobble retain the shared 2 s beat.
const BEAT_MS=2000,BEAT_ANIMATIONS=new Set(['breathe','glow-beat']);
function syncBeat(){let list;try{list=document.getAnimations?.();}catch(_){return;}if(!Array.isArray(list))return;const now=Number(document.timeline?.currentTime);
  for(const a of list){if(!BEAT_ANIMATIONS.has(a.animationName))continue;const start=Number.isFinite(a.startTime)?a.startTime:now;if(!Number.isFinite(start))continue;const duration=a.effect?.getComputedTiming?.().duration,period=Number.isFinite(duration)&&duration>0?duration:BEAT_MS,aligned=Math.floor(start/period)*period;if(a.startTime!==aligned){try{a.startTime=aligned;}catch(_){}}}}
// Web motion: one requestAnimationFrame loop, only while a poke or a heartbeat wobble runs. Each frame rewrites just the moved web
// paths and edges (the cached web elements themselves are never replaced) and dips the poked star; its last frame restores rest.
function webMotionLoop(){if(!webMotion&&typeof window.requestAnimationFrame==='function')webMotion=createWebMotion({now:()=>performance.now(),requestFrame:f=>window.requestAnimationFrame(f),cancelFrame:id=>window.cancelAnimationFrame?.(id),setTimer:(f,ms)=>setTimeout(f,ms),clearTimer:id=>clearTimeout(id),draw:drawWebMotion,period:BEAT_MS});return webMotion;}
function drawWebMotion(state){
  const offsets=state?pluckOffsets(state.impulses,state.time):null,touched=new Set();
  if(offsets&&webLayout)for(const [key,d] of orbWebPathsAt(webLayout,offsets)){webPathEls.get(key)?.setAttribute('d',d);touched.add(key);}
  for(const key of webTouched)if(!touched.has(key)&&webRest.has(key))webPathEls.get(key)?.setAttribute('d',webRest.get(key));
  webTouched=touched;
  edgeGeoms.forEach((g,i)=>{const offset=offsets?.edges.get(i)||0;if(offset||g.moved){g.el.setAttribute('d',edgeCurvePath(g,offset));g.moved=Boolean(offset);}});
  const dips=offsets?.scale||new Map();
  for(const id of new Set([...webDipped,...dips.keys()])){const g=nodeElements.get(id);if(!g)continue;const scale=dips.get(id);for(const part of [g.children[2],g.children[3]])if(scale)part.setAttribute('transform',`scale(${scale.toFixed(3)})`);else part.removeAttribute('transform');}
  webDipped=new Set(dips.keys());
  // A poke also selects its star, which quiets the web; the web comes back to full strength while it is being plucked.
  const plucking=Boolean(state?.impulses.some(impulse=>impulse.plan.dip));if(plucking!==webPlucking){webPlucking=plucking;$('web')?.classList.toggle('plucking',plucking);}
}
// The heartbeat wobble runs only on a live feed (fresh, unpaused) with motion allowed; otherwise everything returns to rest.
function syncWebPulse(){
  if(webHidden()||!liveNow()||reducedMotion()||forcedColors()||!webLayout){if(webMotion){pulseKey='off';webMotion.stop();}return;}
  const ids=graph.nodes.filter(n=>n.active||n.inFlight).map(n=>n.id),key=`${webKey}#${ids.join('|')}`;if(key===pulseKey)return;
  const loop=webMotionLoop();if(!loop)return;pulseKey=key;loop.pulse(ids.length?orbWebPluck(webLayout,graph.nodes,graph.edges,ids,{beat:true}):null);
}
// A poke (click, tap, Enter or Space on a star) plucks its threads. A new poke restarts the wobble, it never stacks. Reduced motion or a
// paused or stale feed gets one brief highlight of those threads instead; forced colours get nothing.
function pokeWeb(id){
  const hidden=webHidden();if(hidden||forcedColors()||!webLayout){if(hidden&&webMotion){pulseKey='off';webMotion.stop();}return;}
  const plan=orbWebPluck(webLayout,graph.nodes,graph.edges,[id]);
  if(reducedMotion()||!liveNow()){flashWeb(plan);return;}
  webMotionLoop()?.poke(plan);
}
function flashWeb(plan){const layer=$('web'),flash=layer?.querySelector('.web-flash');if(!flash)return;flash.setAttribute('d',orbWebFlashPath(webLayout,plan));flash.classList.add('on');layer.classList.add('plucking');clearTimeout(flashTimer);flashTimer=setTimeout(()=>{flash.classList.remove('on');layer.classList.toggle('plucking',webPlucking);},650);}
function renderGraph(){
  $('emptyMap').hidden=graph.nodes.length>0;
  renderGlows();
  const web=renderWeb();
  $('edges').replaceChildren();edgeGeoms=[];
  const focusId=hoveredNode||selected,nearIds=new Set(focusId?[focusId]:[]);
  for(const e of graph.edges)if(focusId&&(e.a===focusId||e.b===focusId)){nearIds.add(e.a);nearIds.add(e.b);}
  // A hub-to-child edge runs straight along its web spoke; one behind a nearer sibling keeps its bend.
  for(const [i,e] of graph.edges.entries()){const a=graph.nodes.find(n=>n.id===e.a),b=graph.nodes.find(n=>n.id===e.b);if(!a||!b)continue;const bend=web?.edgeBends?.[i]??e.bend,dx=b.x-a.x,dy=b.y-a.y,distance=Math.max(1,Math.hypot(dx,dy)),insetA=Math.min(a.r*.65,distance*.12),insetB=Math.min(b.r*.65,distance*.12),sx=a.x+dx/distance*insetA,sy=a.y+dy/distance*insetA,ex=b.x-dx/distance*insetB,ey=b.y-dy/distance*insetB,cx=(sx+ex)/2-dy/distance*bend,cy=(sy+ey)/2+dx/distance*bend,related=focusId&&(e.a===focusId||e.b===focusId),geometry={sx,sy,cx,cy,ex,ey},path=svg('path',{d:edgeCurvePath(geometry),stroke:e.color,class:`edge${e.dim?' dim':''}${e.flow&&fresh()?' in-flight':''}${related?' highlighted':''}${focusId&&!related?' quieted':''}`});$('edges').append(path);edgeGeoms[i]={...geometry,el:path,moved:false};}
  // Rebuilt edges pick up a wobble that is mid-flight before they are painted.
  webMotion?.redraw();
  $('groups').replaceChildren();labelsPlacedAt=null;
  for(const item of graph.labels){const text=svg('text',{class:`cluster-label ${item.kind||''}`,'text-anchor':item.align||'middle',x:item.x,y:item.y});text.textContent=item.text;$('groups').append(text);}
  const ids=new Set(graph.nodes.map(n=>n.id));
  for(const[id,n]of nodeElements)if(!ids.has(id)){n.remove();nodeElements.delete(id);}
  $('map').classList.toggle('map-focus-active',Boolean(focusId));
  for(const n of graph.nodes){let g=nodeElements.get(n.id);if(!g){g=svg('g',{role:'button',tabindex:'0'});g.append(svg('circle',{class:'hit',r:35}),svg('circle',{class:'halo'}),svg('circle',{class:'ring'}),svg('circle',{class:'core'}),svg('text',{class:'label'}),svg('text',{class:'sub'}),svg('text',{class:'capability-line'}),svg('title'));g.addEventListener('click',()=>{if(!justDragged){nodeSelect(n.id);pokeWeb(n.id);}});g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();nodeSelect(n.id);pokeWeb(n.id);}else if(e.key.startsWith('Arrow')){e.preventDefault();focusNode(nextNodeInDirection(graph.nodes,n.id,e.key));}else if(e.key==='Escape'&&!$('drawer').hidden){e.preventDefault();closeDrawer();}});g.addEventListener('pointerenter',()=>{hoveredNode=n.id;renderGraph();});g.addEventListener('pointerleave',()=>{hoveredNode=null;renderGraph();});g.addEventListener('focus',()=>{if(reparenting)return;hoveredNode=n.id;renderGraph();});g.addEventListener('blur',()=>{if(reparenting)return;if(hoveredNode===n.id)hoveredNode=null;renderGraph();});nodeElements.set(n.id,g);$('nodes').append(g);}
    const match=query&&[n.label,n.lane?.label,n.model?.id,n.client?.label,n.clientModel?.id,n.run?.runId,n.call?.model,n.call?.servedModel].filter(Boolean).some(s=>s.toLowerCase().includes(query));
    const isNear=!focusId||nearIds.has(n.id),isFocused=focusId===n.id;
    g.setAttribute('class','node '+n.kind+(n.root?' root':'')+(n.hub?' hub':'')+(n.active&&fresh()?' active':'')+(n.inFlight&&fresh()?' in-flight':'')+(n.verified?' verified':'')+(n.reviewLevel?' review-'+n.reviewLevel:'')+(n.kind==='model'&&n.model?.loaded===true&&fresh()?' loaded':'')+(n.unknown?' unknown':'')+(selected===n.id?' selected':'')+(isNear&&focusId?' near':'')+(isFocused?' spotlight':'')+(query?(match?' match':' fade'):''));
    const exactIdentity=n.kind==='model'?n.model?.id:n.kind==='client-model'?n.clientModel?.id:null;
    g.setAttribute('color',n.color);g.setAttribute('transform',`translate(${n.x},${n.y})`);g.setAttribute('aria-label',[n.label,exactIdentity?'Model ID '+exactIdentity:null,n.subtitle,n.visual?.value,n.visual?.capabilityLabel].filter(Boolean).join('; '));g.setAttribute('aria-pressed',String(selected===n.id));g.children[7].textContent=[n.label,exactIdentity,n.subtitle,n.call?.status].filter(Boolean).join(' · ');
    const [hit,halo,ring,core,label,sub,capability]=g.children;halo.setAttribute('r',n.r+9);halo.setAttribute('stroke',n.active?'var(--green)':n.color);halo.setAttribute('style',n.auraPeriodMs?`animation-duration:${n.auraPeriodMs}ms`:'');ring.setAttribute('r',n.r+5);ring.setAttribute('stroke',n.color);core.setAttribute('r',n.r);core.setAttribute('fill',n.color);if(n.unknown)core.setAttribute('stroke',n.color);else core.removeAttribute('stroke');label.setAttribute('y',n.r+(n.root?32:22));label.textContent=n.label;sub.setAttribute('y',n.r+(n.root?50:37));sub.textContent=n.subtitle;capability.setAttribute('y',n.r+52);capability.setAttribute('font-size',`${8/Math.max(.1,camera.z)}px`);capability.textContent=n.kind==='model'?n.visual?.capabilityLabel||'Capabilities unknown':'';
  }
  applyCamera();renderModelAction();syncWebPulse();syncBeat();
}
function visibleMapArea(){
  const box=$('graphRegion').getBoundingClientRect(),heading=$('mapHeading').getBoundingClientRect(),banner=$('memoryBanner');
  // The memory banner (when shown) sits under the status chips; the map fits below both.
  const bannerBottom=banner&&!banner.hidden?banner.getBoundingClientRect().bottom-box.top+12:0;
  const area={left:16,right:box.width-16,top:Math.max(74,heading.bottom-box.top+16,bannerBottom),bottom:box.height-70};
  // The compact sheet slides up; measure panels where the slide ends, not mid-animation.
  let slide=0;try{const t=window.getComputedStyle?.($('evidenceRail')).transform;if(t&&t!=='none')slide=new DOMMatrixReadOnly(t).m42||0;}catch(_){}
  const panels=[$('drawer'),$('onlineCodeDetails'),$('fixInferenceDetails'),$('activityDetails')].filter(panel=>!panel.hidden).map(panel=>{const r=panel.getBoundingClientRect();return{left:r.left,right:r.right,top:r.top-slide,bottom:r.bottom-slide};});
  if(!panels.length)return area;
  const d={left:Math.min(...panels.map(panel=>panel.left))-box.left-14,right:Math.max(...panels.map(panel=>panel.right))-box.left+14,
    top:Math.min(...panels.map(panel=>panel.top))-box.top-14,bottom:Math.max(...panels.map(panel=>panel.bottom))-box.top+14};
  if(d.left>=area.right||d.right<=area.left||d.top>=area.bottom||d.bottom<=area.top)return area;
  const spaces=[{...area,right:Math.min(area.right,d.left)},{...area,left:Math.max(area.left,d.right)},{...area,bottom:Math.min(area.bottom,d.top)},{...area,top:Math.max(area.top,d.bottom)}]
    .filter(r=>r.right-r.left>=80&&r.bottom-r.top>=90).sort((a,b)=>(b.right-b.left)*(b.bottom-b.top)-(a.right-a.left)*(a.bottom-a.top));
  return spaces[0]||area;
}
function fitMap(){const area=visibleMapArea(),points=[...graph.nodes,...graph.labels.map(label=>({x:label.x,y:label.y}))],fit=fitGraph(points,{left:area.left,top:area.top,width:area.right-area.left,height:area.bottom-area.top},{horizontalPadding:90,verticalPadding:46,maxScale:1.7});if(!fit)return;camera={x:fit.x,y:fit.y,z:fit.scale};applyCamera();}
function revealSelected(){const n=graph.nodes.find(n=>n.id===selected);if(!n)return;const area=visibleMapArea(),width=area.right-area.left,height=area.bottom-area.top;if(width<=0||height<=0)return;const x=camera.x+n.x*camera.z,y=camera.y+n.y*camera.z;const marginX=Math.min(width/2,Math.max(65,n.label.length*3.5)),top=area.top+Math.min(26,height/3),bottom=area.bottom-Math.min(64,height/2);const targetX=Math.max(area.left+marginX,Math.min(area.right-marginX,x)),targetY=Math.max(top,Math.min(bottom,y));camera.x+=targetX-x;camera.y+=targetY-y;applyCamera();}
function applyCamera(){
  const transform=`translate(${camera.x},${camera.y}) scale(${camera.z})`;
  // WebKit can retain old SVG paint tiles when an ancestor transform changes.
  // Reparent the same nodes into a fresh viewport so zoom/pan invalidates the
  // entire old layer while preserving listeners and keyboard focus.
  if(paintCamera!==transform){paintCamera=transform;const old=$('viewport'),next=svg('g',{id:'viewport',transform});const focus=document.activeElement;const restore=old.contains(focus);reparenting=true;try{while(old.firstChild)next.append(old.firstChild);old.replaceWith(next);syncBeat();if(restore)focus.focus({preventScroll:true});}finally{reparenting=false;}}
  // Glows keep their radius in screen terms: re-clamped on zoom, never rebuilt. Zoomed far out, the small client and route webs
  // (a few pixels across) step aside and only the Mac, PC and trace-run webs stay.
  if(camera.z!==glowZoom){glowZoom=camera.z;const radius=glowRadius(camera.z);for(const glow of $('glows')?.querySelectorAll('.glow')||[])glow.setAttribute('r',radius);}
  $('web')?.classList.toggle('far',camera.z<WEB_MINOR_ZOOM);
  $('zoomLevel').textContent=Math.round(camera.z*100)+'%';$('footerNote').textContent=camera.z<.5?'Zoom in for labels · select any node for details':'Click or arrow through nodes · drag to pan · scroll to zoom';
  const laneYs=graph.nodes.filter(n=>n.kind==='windows-lane').map(n=>n.y);let pcSubAbove=false;
  for(const n of graph.nodes){const g=nodeElements.get(n.id);if(!g)continue;const label=g.children[4],sub=g.children[5],capability=g.children[6],hit=g.children[0],scale=Math.max(1,.78/camera.z);hit.setAttribute('r',Math.max(n.r+8,18/camera.z));label.setAttribute('visibility','visible');sub.setAttribute('visibility','visible');label.setAttribute('font-size',(n.hub?19:n.root?13:n.kind==='model'?11:12)*scale);sub.setAttribute('font-size',n.captionSide?Math.max(7*scale,9/camera.z):(n.hub?8:7)*scale);capability.setAttribute('font-size',`${8/camera.z}px`);
    // The PC hub's subtitle flips above the hub when, zoomed out, it would run into its lanes' captions.
    const place=nodeCaptionLayout(n,scale,{laneGap:n.kind==='windows-worker'?Math.min(Infinity,...laneYs.map(y=>y-n.y).filter(d=>d>0)):Infinity});label.setAttribute('y',place.label.y);sub.setAttribute('y',place.sub.y);capability.setAttribute('y',n.r+52*scale);
    const subText=place.subText??n.subtitle??'';if(sub.textContent!==subText)sub.textContent=subText;if(n.kind==='windows-worker')pcSubAbove=place.subAbove;
    // Side captions (PC lanes) read outward so neighbouring lanes never collide.
    if(n.captionSide)for(const [text,spot] of [[label,place.label],[sub,place.sub]]){text.setAttribute('x',spot.x);text.style.textAnchor=spot.anchor;}}
  // The flipped PC subtitle states what the "WINDOWS PC · …" header would, in the same band above the hub,
  // so the header steps aside while the subtitle is up there.
  [...$('groups').children].forEach((label,i)=>{label.setAttribute('font-size',`${11/camera.z}px`);label.setAttribute('visibility',graph.labels[i]?.kind==='windows'&&pcSubAbove?'hidden':'visible');});
  placeClusterLabels();
}
// Cluster labels are measured at the current zoom and moved off nodes, their always-on captions and each other.
const CAPTIONED=new Set(['runtime','client','windows-worker','windows-lane']);
let labelsPlacedAt=null;
function placeClusterLabels(){
  const texts=[...$('groups').children];if(!texts.length||texts.length!==graph.labels.length||labelsPlacedAt===camera.z)return;labelsPlacedAt=camera.z;
  const obstacles=[];
  for(const n of graph.nodes){const pad=n.r+6;obstacles.push({left:n.x-pad,right:n.x+pad,top:n.y-pad,bottom:n.y+pad});const g=nodeElements.get(n.id);if(!g||!(CAPTIONED.has(n.kind)||(n.kind==='model'&&n.model?.loaded===true)))continue;for(const caption of [g.children[4],g.children[5]]){try{const box=caption.getBBox();if(box.width)obstacles.push({left:n.x+box.x,right:n.x+box.x+box.width,top:n.y+box.y,bottom:n.y+box.y+box.height});}catch(_){}}}
  const labels=texts.map((text,i)=>{let box={width:0,height:11/camera.z};try{const b=text.getBBox();box={width:b.width,height:b.height};}catch(_){}const source=graph.labels[i];return{x:source.x,y:source.y,align:source.align,width:box.width,height:box.height*.8};});
  resolveLabelCollisions(labels,obstacles,{step:3/camera.z,maxShift:48/camera.z}).forEach((label,i)=>texts[i].setAttribute('y',label.y));
}
function zoom(factor,x,y){const area=visibleMapArea();x??=(area.left+area.right)/2;y??=(area.top+area.bottom)/2;const next=Math.max(.08,Math.min(3,camera.z*factor));camera.x=x-(x-camera.x)*next/camera.z;camera.y=y-(y-camera.y)*next/camera.z;camera.z=next;userCamera=true;boundCamera();applyCamera();}
function listItem(label,sub,selectedItem,cls,action,key){const b=el('button','item'+(selectedItem?' selected':'')),dot=el('span','dot '+cls),body=el('span','item-copy');body.append(el('strong',null,label),el('small',null,sub));b.append(dot,body);b.dataset.key=key;b.setAttribute('aria-pressed',String(selectedItem));b.addEventListener('click',action);return b;}
function selectRun(run){let refocus=false;if(compactEvidence()){hideOnlineCodeDetails();hideFixInferenceDetails();hideActivityDetails();refocus=closeCompactSidebar();}else if(!dockedSidebar())refocus=closeCompactSidebar();const fromDockedBrowser=dockedSidebar()&&$('runtimeSidebar').contains(document.activeElement),fitForDrawer=!userCamera&&$('drawer').hidden&&$('onlineCodeDetails').hidden&&$('fixInferenceDetails').hidden;runId=runKey(run);selected='run:'+runId;makeGraph();renderGraph();$('drawer').hidden=false;inspectorNode=null;renderInspector();renderList();if(fitForDrawer)fitMap();else revealSelected();userCamera=true;if(fromDockedBrowser)$('drawer').focus({preventScroll:true});if(refocus)nodeElements.get(selected)?.focus({preventScroll:true});}
function renderModelScope(){const controls=$('modelScopeControls'),note=$('modelScopeNote');controls.hidden=view!=='runtime'||snapshot?.host==='windows';note.hidden=controls.hidden;if(controls.hidden)return;controls.querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.modelScope===modelScope)));const roster=displayedModels();if(!snapshot){note.textContent='Waiting for local model inventory.';return;}if(modelScope==='all'){note.textContent=`All ${roster.macDiscovered} discovered Mac models shown. Activity counts still use the full inventory.`;return;}const extras=roster.operationalExtras.length,hidden=roster.hidden.length;note.textContent=`${roster.coreDiscovered} of ${CORE_MODEL_IDS.length} primary models discovered${extras?` · ${extras} other model${extras===1?'':'s'} shown from last reported use`:''}${hidden?` · ${hidden} other discovered under All discovered`:''}. Activity counts use the full inventory.`;}
function renderList(){const list=$('list');const focused=document.activeElement;const focusedKey=list.contains(focused)?focused?.closest('button')?.dataset.key:null;const scroll=list.scrollTop;list.replaceChildren();$('listTitle').textContent=view==='runtime'?'Runtime & clients':'Recent route records';
  if(view==='runtime'){const runtime=displayedModels().visible.filter(m=>m.id.toLowerCase().includes(query)).sort((a,b)=>Number(modelActivity(b).active)-Number(modelActivity(a).active)||Number(b.loaded===true)-Number(a.loaded===true)||a.id.localeCompare(b.id));if(runtime.length)list.append(el('p','list-section','Local runtime'));runtime.forEach(m=>{const id=`model:${m.host}:${m.id}`,activity=modelActivity(m),state=paused&&ACTIVE.has(m.state)?`At pause: ${STATE[m.state]}`:ACTIVE.has(m.state)&&!activity.active?'Activity unverified':STATE[m.state]||'Unknown';list.append(listItem(name(m.id),`${m.host==='mac'?'Mac':'Windows'} · ${state}`,selected===id,activity.active?'green':['unknown','stale','loaded','unloaded'].includes(m.state)?'unknown':'',()=>nodeSelect(id),id));});const advisor=jevView(Array.isArray(snapshot?.components)?snapshot.components.find(c=>c.id==='jev'):null,{feedFresh:fresh(),snapshotAge:Math.max(0,sampleAge())});if('jev route advisor'.includes(query)){list.append(el('p','list-section','Route advisor · outside Core 6'));list.append(listItem('Jev',advisor.state==='configured'?'Opt-in configured · health unverified':advisor.state==='unavailable'?'Opt-in unavailable':'Status unknown',selected==='jev','unknown',()=>nodeSelect('jev'),'jev'));}const afm=afmView(snapshot?.afm,{feedFresh:fresh(),host:snapshot?.host});if(afm.visible&&'apple foundation models afm'.includes(query)){list.append(el('p','list-section','On-device advisor · outside Core 6'));list.append(listItem('Apple Foundation Models',afm.label+' · inference untested',selected==='afm',afm.state==='executable'?'blue':'unknown',()=>nodeSelect('afm'),'afm'));}const clients=clientRows().filter(c=>[c.label,c.model,...c.models.map(m=>m.id)].filter(Boolean).some(s=>s.toLowerCase().includes(query)));if(clients.length)list.append(el('p','list-section','Client model identities'));clients.forEach(c=>{const id=`client:${c.id}`;list.append(listItem(c.label,`Subagents unknown · ${c.model||c.models[0]?.id||'model identity unknown'}`,selected===id,'unknown',()=>nodeSelect(id),id));});}
  else runs().filter(r=>r.runId.toLowerCase().includes(query)).forEach(r=>{const summary=runSummary(r),item=listItem(r.runId,`${runSource(r)} · ${ageText(r.ageSeconds)}`,runKey(r)===runId,/reject|fail|error/i.test(r.hostAcceptance||r.status)?'red':'blue',()=>selectRun(r),runKey(r));item.classList.add('run-item');identifier(item.querySelector('strong'),r.runId);const body=item.querySelector('.item-copy');body.append(el('small','run-status',routeLabel(r)),el('small','run-coverage',`${summary.count} recorded call${summary.count===1?'':'s'} · select for usage details`));if(r.hostAcceptance)body.append(el('small',r.hostAcceptance==='REJECTED'?'host-rejected':'',`Host acceptance: ${r.hostAcceptance}`));const review=reviewConsistencyView(r);if(review.level)body.append(el('span',`review-badge ${review.level}`,review.badge));list.append(item);});
  if(!list.children.length)list.append(el('p','empty-list',query&&view==='runtime'&&modelScope==='core'?'No Core 6 match. Choose All discovered to search other models.':query?'Nothing matches your search.':view==='runs'?'No completed records are available from the connected sources.':'Waiting for model inventory.'));if(focusedKey){const match=[...list.children].find(n=>n.dataset.key===focusedKey);match?.focus({preventScroll:true});}list.scrollTop=scroll;
}
// Panels open with a status dot, one plain line and one sentence; key facts follow as tiles, and the
// technical evidence waits behind closed disclosures.
function panelSummary(container,tone,line,meaning){const box=el('div','panel-summary'),copy=el('div','panel-summary-copy');box.dataset.tone=tone;copy.append(el('strong',null,trim(String(line),60)),el('p',null,meaning));box.append(el('i','status-dot'),copy);container.append(box);return box;}
function fillTiles(grid,items){grid.replaceChildren();for(const [label,value,wide] of items){const tile=el('div',wide?'tile tile-wide':'tile');tile.append(el('span','tile-label',label),el('strong','tile-value',knownValue(value)));grid.append(tile);}return grid;}
function factTiles(container,items){const grid=fillTiles(el('div','tiles'),items);container.append(grid);return grid;}
function disclosure(container,key,label){const box=el('details','panel-more');box.dataset.key=key;box.append(el('summary',null,label));container.append(box);return box;}
// A rebuild keeps which disclosures were open and the focused summary; it keeps the panel's scroll position
// too, unless the panel now shows another node (resetScroll), which opens at its summary.
function rebuildPanel(scroller,body,build,{resetScroll=false}={}){const open=new Set(),active=document.activeElement,focusedKey=body.contains(active)?active?.closest?.('details')?.dataset.key:null;for(const box of body.querySelectorAll('details'))if(box.open)open.add(box.dataset.key);const scroll=scroller.scrollTop;body.replaceChildren();build();for(const box of body.querySelectorAll('details')){if(open.has(box.dataset.key))box.open=true;if(focusedKey&&box.dataset.key===focusedKey)box.querySelector('summary')?.focus({preventScroll:true});}scroller.scrollTop=resetScroll?0:scroll;}
// A panel body's structure without its leaf text: tags, classes, disclosure keys and the text of mixed content.
// Two builds with one shape differ only in text that a refresh can patch in place.
function panelShape(node){const kids=[...node.children],own=kids.length?[...(node.childNodes||[])].filter(c=>c.nodeType===3).map(c=>c.nodeValue).join(''):'';return`${node.tagName}.${node.className}#${node.dataset?.key||''}${own?JSON.stringify(own):''}[${kids.map(panelShape).join(',')}]`;}
// Copies a fresh build's leaf text, tone and title onto the shown panel, element by element. Nothing is replaced,
// so an open disclosure, a press in progress, keyboard focus and the scroll position all stay where they are.
function patchPanel(shown,fresh){const a=[...shown.children],b=[...fresh.children];if(!b.length&&shown.textContent!==fresh.textContent)shown.textContent=fresh.textContent;
  if(shown.dataset&&fresh.dataset&&shown.dataset.tone!==fresh.dataset.tone){if(fresh.dataset.tone===undefined)delete shown.dataset.tone;else shown.dataset.tone=fresh.dataset.tone;}
  if((shown.title||'')!==(fresh.title||''))shown.title=fresh.title||'';a.forEach((child,i)=>patchPanel(child,b[i]));}
function renderRunSummary(container,run,notes=container){const summary=runSummary(run),grid=factTiles(container,[['Recorded calls',String(summary.count)],['Reported-call tokens',summary.tokens],['Summed call time',summary.duration]]);grid.setAttribute('aria-label','Selected run summary');if(summary.duplicateIdentity)container.append(el('p','review-warning','Duplicate call identity recorded. Totals are withheld to avoid counting it twice.'));else if(summary.count&&((summary.tokenKnown===summary.count&&summary.tokens==='Unknown')||(summary.timed===summary.count&&summary.duration==='Unknown')))container.append(el('p','review-warning','A combined total exceeds the supported numeric range and remains unknown.'));notes.append(el('p','summary-note',`Scope: reported calls only. ${summary.tokenKnown} of ${summary.count} calls report complete token counts; ${summary.timed} of ${summary.count} report duration. Time sums these call durations, not route wall time. Additional invocations are not inferred.`));}
const COMPLETION_CODES=Object.freeze({COVERAGE_ASSESSED:'Coverage assessed',INPUT_INVALID:'Completion input invalid',VALIDATION_UNBOUND:'Validation binding invalid',TEST_EVIDENCE_INVALID:'Test evidence invalid',LITERAL_EVIDENCE_INVALID:'Literal evidence invalid',EVIDENCE_GATE_UNAVAILABLE:'Completion evidence gate unavailable'});
const COMPLETION_PROGRESS=Object.freeze({HISTORY_UNKNOWN:'Prior task history unknown',HISTORY_UNTRUSTED:'Prior task history untrusted',NO_PRIOR_MATCH:'No exact prior match in trusted history',SAME_RUN_REPLAY:'Same run replay',NO_PROGRESS:'Exact task, candidate, and evidence repeated across runs'});
const COMPLETION_VIEW_FIELDS=new Set(['verdict','code','criteriaTotal','criteriaVerified','criteriaUnassessed','progressStatus','stopReason']);
function completionEvidenceView(value){
  if(!value||typeof value!=='object'||Array.isArray(value))return null;
  const keys=Object.keys(value);
  if(keys.length!==COMPLETION_VIEW_FIELDS.size||keys.some(key=>!COMPLETION_VIEW_FIELDS.has(key)))return null;
  const total=value.criteriaTotal,verified=value.criteriaVerified,unassessed=value.criteriaUnassessed;
  if(![total,verified,unassessed].every(n=>Number.isSafeInteger(n)&&n>=0&&n<=64)||verified+unassessed!==total)return null;
  const verdict=total>0&&verified===total?'VERIFIED':verified>0?'PARTIAL':'UNVERIFIED';
  if(value.verdict!==verdict||typeof value.code!=='string'||!Object.hasOwn(COMPLETION_CODES,value.code)||(value.code!=='COVERAGE_ASSESSED'&&verified!==0)||typeof value.progressStatus!=='string'||!Object.hasOwn(COMPLETION_PROGRESS,value.progressStatus)||value.stopReason!==(value.progressStatus==='NO_PROGRESS'?'NO_PROGRESS':null))return null;
  return{verdict,code:value.code,progressStatus:value.progressStatus,total,verified,unassessed};
}
function renderCompletionEvidence(container,run){
  if(run.status!=='RESPONSE_VALIDATED')return;
  const section=el('section','completion-evidence'),heading=el('h3','panel-label','Completion evidence');heading.id='completionEvidenceHeading';section.setAttribute('aria-labelledby',heading.id);section.append(heading);
  const e=completionEvidenceView(run.completionEvidence);
  if(!e){section.append(el('p','completion-evidence-title','Criterion evidence unavailable'),el('p','detail-note','This route record has no valid criterion coverage verdict. Task completion remains unknown.'));container.append(section);return;}
  const title=e.verdict==='VERIFIED'?'All stated criteria evidenced':e.verdict==='PARTIAL'?'Some stated criteria evidenced':'Stated criteria unverified';
  section.append(el('p','completion-evidence-title',title));
  if(run.source==='monitor-receipt'&&run.completionEvidenceSource==='router-archive')section.append(el('p','detail-note','Criterion coverage comes from the matching router archive.'));
  const grid=factTiles(section,[['Criteria evidenced',String(e.verified)+' of '+String(e.total)],['Prior result check',COMPLETION_PROGRESS[e.progressStatus]]]);grid.setAttribute('aria-label','Criterion coverage and prior result status');
  section.append(el('p','detail-note',e.code==='COVERAGE_ASSESSED'?'Scope: stated criterion evidence only.':COMPLETION_CODES[e.code]+'. Criterion coverage remains unverified.'));
  section.append(el('p','detail-note',run.testsStatus==='NOT_RUN'?'Tests not run.':'Test execution not established in this record.'));
  section.append(el('p','detail-note','Workflow and release acceptance not established.'));
  if(e.progressStatus==='NO_PROGRESS')section.append(el('p','review-warning','No progress recorded: the same task, candidate, and evidence fingerprints were seen in a different completed run. This display does not retry or unload a model.'));
  container.append(section);
}
function detailPairs(container,rows){const dl=el('dl');rows.forEach(([k,v])=>dl.append(el('dt',null,k),el('dd',null,knownValue(v))));container.append(dl);}
const knownValue=value=>Array.isArray(value)?(value.length?value.map(knownValue).join(', '):'Unknown'):value===null||value===undefined||value===''?'Unknown':typeof value==='object'?JSON.stringify(value):String(value);
function capabilityDetail(value){
  if(!value||typeof value!=='object'||Array.isArray(value))return'Unknown';
  const flag=key=>value[key]===true?'Yes':value[key]===false?'No':'Unknown';
  const options=Array.isArray(value.reasoningOptions)?value.reasoningOptions.filter(item=>typeof item==='string'&&item.length<40).join(', ')
    :value.reasoningOptions===true?'Available':value.reasoningOptions===false?'None':'Unknown';
  if(['vision','toolUse','reasoning'].every(key=>value[key]!==true&&value[key]!==false)&&options==='Unknown')return'No capability flags reported';
  return`Vision: ${flag('vision')} · Tools: ${flag('toolUse')} · Reasoning: ${flag('reasoning')} · Reasoning options: ${options||'Unknown'}`;
}
function modelActionState(m){return modelControlView(m,{host:snapshot?.host,feedFresh:fresh(),supported:snapshot?.modelControl?.supported,reason:snapshot?.modelControl?.reason,status:modelControlStatus,models:snapshot?.models});}
function renderModelAction(){const dock=$('modelActionDock'),n=graph.nodes.find(node=>node.id===selected);if(view!=='runtime'||n?.kind!=='model'||n.model?.host!==snapshot?.host){dock.hidden=true;return;}dock.hidden=false;const m=n.model,state=modelActionState(m);$('modelActionTitle').textContent=m.name||name(m.id);$('modelActionStatus').textContent=modelControlError||(modelControlPosting?'Submitting request…':(modelControlStatus?.modelId===m.id&&modelControlStatus.status!=='idle')?`${modelControlStatus.status}${modelControlStatus.message?` · ${modelControlStatus.message}`:''}`:state.reason);const button=$('modelAction');button.textContent=state.action==='unload'?'Unload':'Load';button.disabled=modelControlPosting||!state.action;button.dataset.action=state.action||'';button.setAttribute('aria-label',`${state.action==='unload'?'Unload':'Load'} ${m.id}`);}
// Model control status matters only while its Load/Unload footer shows or a request is posting or running.
const modelControlPollWanted=()=>!$('modelActionDock').hidden||modelControlPosting||modelControlStatus?.status==='running';
async function pollModelControl(){try{const response=await fetch('/api/models/control',{cache:'no-store'});if(!response.ok)throw new Error('Control status unavailable');const result=await response.json();if(result&&typeof result.status==='string')modelControlStatus=result;}catch(_){modelControlStatus=null;}renderModelAction();}
function validAutoUnloadState(value){return value&&typeof value==='object'&&typeof value.enabled==='boolean'&&(value.configError==null||typeof value.configError==='string');}
function renderAutoUnload(){
  const button=$('autoUnloadToggle'),known=validAutoUnloadState(autoUnloadState),enabled=known&&autoUnloadState.enabled,invalid=known&&Boolean(autoUnloadState.configError);
  button.disabled=autoUnloadPosting||autoUnloadPolling||!known||invalid;
  button.textContent=autoUnloadPosting?'Saving…':known?(enabled?'Turn off':'Turn on'):'Unavailable';
  $('autoUnloadMode').textContent=known?(enabled?'On':'Off'):'State unknown';
  $('autoUnloadStatus').textContent=autoUnloadError||(
    invalid?'Invalid private setting; fix the configuration before changing it.':
    known?(typeof autoUnloadState.blocked==='string'&&autoUnloadState.blocked?trim(autoUnloadState.blocked,170):enabled?'Watching for safely idle models.':'Automatic unloading is off.'):
    'Checking app setting…');
}
async function fetchAutoUnload(enabled){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),3000);
  try{
    const writing=typeof enabled==='boolean';
    const response=await fetch('/api/models/auto-unload',{
      method:writing?'POST':'GET',cache:'no-store',credentials:'same-origin',signal:controller.signal,
      ...(writing?{headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})}:{})
    });
    if(!response.ok){const error=new Error('Auto-unload API unavailable');error.status=response.status;throw error;}
    const result=await response.json();
    if(!validAutoUnloadState(result))throw new Error('Invalid auto-unload state');
    return result;
  }finally{clearTimeout(timer);}
}
async function pollAutoUnload(force=false){
  if(autoUnloadPolling||autoUnloadPosting)return;
  autoUnloadPolling=true;const revision=autoUnloadRevision;
  if(force)autoUnloadState=null;
  autoUnloadError='';renderAutoUnload();
  try{
    const state=await fetchAutoUnload();
    if(revision===autoUnloadRevision&&!autoUnloadPosting)autoUnloadState=state;
  }catch(error){
    if(revision===autoUnloadRevision&&!autoUnloadPosting){autoUnloadState=null;autoUnloadError=error.status===501?'Available only in the Monitor app.':'Could not verify the app setting.';}
  }finally{autoUnloadPolling=false;if(revision===autoUnloadRevision&&!autoUnloadPosting)renderAutoUnload();}
}
async function requestAutoUnload(){
  if(autoUnloadPosting||autoUnloadPolling||!validAutoUnloadState(autoUnloadState)||autoUnloadState.configError)return;
  const before=autoUnloadState.enabled,desired=!before,revision=++autoUnloadRevision;
  let refresh=true;
  autoUnloadPosting=true;autoUnloadError='';renderAutoUnload();
  try{
    // Re-read before writing so a setting changed outside this view cannot be toggled backwards.
    const current=await fetchAutoUnload();
    if(current.configError||current.enabled!==before){autoUnloadState=current;autoUnloadError='Setting changed elsewhere. Review its current state and try again.';refresh=false;return;}
    const saved=await fetchAutoUnload(desired);
    if(saved.enabled!==desired)throw new Error('Setting not confirmed');
    autoUnloadState=saved;
  }catch(_){autoUnloadState=null;autoUnloadError='Could not confirm the setting. Reading it again; no retry was sent.';}
  finally{if(revision===autoUnloadRevision){autoUnloadPosting=false;renderAutoUnload();if(refresh)pollAutoUnload();}}
}
async function requestModelAction(){const n=graph.nodes.find(node=>node.id===selected);if(n?.kind!=='model')return;const state=modelActionState(n.model);if(!state.action||modelControlPosting)return;modelControlPosting=true;modelControlError='';renderModelAction();try{const response=await submitModelControl(fetch,state.action,n.model.id),result=response.result;if(!response.ok){modelControlError=result.message||`Request rejected (${response.statusCode}).`;modelControlStatus=result;}else if(result.status==='running'){modelControlStatus=result;}else{modelControlError=result.message||'The server did not confirm a running operation.';modelControlStatus=result;}}catch(_){modelControlError='Could not reach local model control.';}finally{modelControlPosting=false;}renderModelAction();pollModelControl();}
function headlessText(h,view){if(!view||view.headless.label==='Headless unknown')return'Unknown';return[view.headless.label,typeof h?.grantedBy==='string'?`granted by ${h.grantedBy}`:null,typeof h?.reason==='string'?h.reason:null].filter(Boolean).join(' · ');}
const INSPECTOR_EYEBROW={model:'LOCAL MODEL',afm:'ON-DEVICE ADVISOR',jev:'ROUTE ADVISOR','model-overflow':'INSTALLED MODEL INVENTORY',call:'MODEL RECEIPT',run:'ROUTE RECORD',pipeline:'SHARED ROUTE','windows-worker':'PC WORKER','windows-lane':'PC MODEL LANE','mac-peer':'LAN PEER','mac-model':'MODEL ON THE MAC',client:'CLIENT MODEL IDENTITY','client-model':'CLIENT MODEL IDENTITY',clients:'CLIENT MODEL IDENTITY'};
// The drawer is built off-page on every render and compared with what is shown, never keyed on raw aged objects:
// the same node with the same structure only has its text patched in place (ages tick without replacing a
// disclosure mid-click or moving focus); a new structure rebuilds; another node rebuilds from the top.
function renderInspector(){if($('drawer').hidden)return;const n=graph.nodes.find(n=>n.id===selected);if(!n){$('drawer').hidden=true;return;}
  const eyebrow=INSPECTOR_EYEBROW[n.kind]||'LOCAL RUNTIME',title=n.kind==='model'?name(n.model.id):n.kind==='afm'?'Apple Foundation Models':n.kind==='windows-lane'?`${n.lane.id==='fast'?'Fast':'Deep'} lane`:n.kind==='mac-model'?name(n.macModel.id):n.label;
  if($('inspectorEyebrow').textContent!==eyebrow)$('inspectorEyebrow').textContent=eyebrow;if($('inspectorTitle').textContent!==title)$('inspectorTitle').textContent=title;
  const body=$('inspector'),next=el('div'),sameNode=inspectorNode===n.id,jump=inspectorBody(n,next,{firstBuild:!sameNode})==='runs',shape=panelShape(next);
  // Fix Nisi Inference is the footer's primary action while a Nisi record is pending; the call-trace jump steps back beside it.
  // An open route run blocks the fix (its check stops at the first step), so then the fix is secondary and the call
  // traces, where that run is, lead again.
  const recovery=n.kind==='pipeline'?nisiRecovery({macOwner:snapshot?.host==='mac'}):null,fix=Boolean(recovery?.needed),lead=fix&&!recovery.blocked,jumpButton=$('drawerJumpButton'),fixButton=$('drawerFixButton');
  fixButton.hidden=!fix;fixButton.classList.toggle('panel-primary',lead);jumpButton.classList.toggle('panel-primary',!lead);const jumpText=lead?'Call traces →':'Explore call traces →';if(jumpButton.textContent!==jumpText)jumpButton.textContent=jumpText;$('drawerJump').hidden=!jump&&!fix;
  if(sameNode&&shape===inspectorShape){patchPanel(body,next);return;}
  inspectorNode=n.id;inspectorShape=shape;rebuildPanel($('drawer'),body,()=>body.append(...[...(next.childNodes||next.children)]),{resetScroll:!sameNode});
}
const capital=text=>String(text).replace(/^./,s=>s.toUpperCase());
// The route's pending Nisi record (online-code-mode nisiRecoveryView): the summary shows on any observer, the Fix action on the Mac owner.
function nisiRecovery({macOwner=true}={}){return nisiRecoveryView(snapshot?.pipeline,snapshot?.components,{feedFresh:fresh(),macOwner,snapshotAge:Math.max(0,sampleAge())});}
// The Mac hub's Memory section: the guard's level and numbers, the three largest users (approximate sizes) and its
// suggestions (text only). Open on the node's first build while memory is tight or critical.
function memorySection(body,n,memory,{open=false}={}){const box=disclosure(body,`${n.id}|memory`,memory.alert?`Memory · ${memory.label.toLowerCase()}`:'Memory');if(open)box.open=true;
  detailPairs(box,memory.rows);
  box.append(el('p','panel-label','Largest users'));
  if(memory.consumers.length){const users=el('ul','memory-list');for(const row of memory.consumers){const li=el('li');li.append(el('strong',null,row.label),document.createTextNode(` ${row.text}`));users.append(li);}box.append(users);}
  else box.append(el('p','detail-note',!memory.known?'Unknown.':memory.level==='ok'?'Measured only when memory is not OK.':'None reported.'));
  if(memory.suggestions.length){box.append(el('p','panel-label','Suggestions'));const list=el('ul','memory-list');memory.suggestions.forEach(text=>list.append(el('li',null,text)));box.append(list);}
  box.append(el('p','detail-note','Sizes are approximate: shared memory counts once per process. The memory guard never quits an app or unloads a model; its suggestions are text only.'));}
const compLabel=c=>c?({ready:'Pair ready',partial:'Needs 2nd model','needs-action':'Needs action','in-use':'In use'}[c.state]||capital(c.state||'Unknown')):'Unknown';
// Router concurrency (spec 6.13): the snapshot's per-run rows, re-checked; one stacked line each.
const ROUTE_RUN_ID=/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/,ROUTE_STATUS_WORDS={running:'Running',waiting:'Queued',admitting:'Being admitted',unresolved:'Unresolved','archived-uncleared':'Archived, record not cleared',unverified:'Run lock unverified',unreadable:'Record unreadable',changing:'Changing; checked again next sample'},ROUTE_RESOURCES={'mac-pair':'Mac pair','pc-route':'PC route','pc-lane-deep':'PC deep lane','pc-lane-fast':'PC fast lane'};
function routePipelines(){const rows=snapshot?.pipeline?.pipelines;return Array.isArray(rows)?rows.filter(r=>r&&typeof r.runId==='string'&&ROUTE_RUN_ID.test(r.runId)).slice(0,8):[];}
function routeRunLine(r){const who=CLIENTS.find(([id])=>id===r.client)?.[1],where=r.host==='windows'?'PC':r.host==='mac'?'Mac':null,left=Number.isFinite(r.secondsLeft)?`${Math.max(0,Math.round(r.secondsLeft-Math.max(0,sampleAge())))} s left`:null;return[r.runId,ROUTE_STATUS_WORDS[r.status]||'State unknown',r.status==='waiting'&&ROUTE_RESOURCES[r.resource]?`for ${ROUTE_RESOURCES[r.resource]}`:null,left&&['waiting','admitting'].includes(r.status)?left:null,who,where,typeof r.stage==='string'&&/^[A-Za-z][A-Za-z0-9_.:-]{0,63}$/.test(r.stage)?r.stage.replaceAll('_',' '):null].filter(Boolean).join(' · ');}
const headlessShort=h=>h?.on?h.label.replace(/^Headless on · /,'On · ').replace(/ left$/,''):h?.label==='Headless off'?'Off':'Unknown';
const jobStatus=row=>row.state==='success'?'Answered':row.state==='invalid'||row.state==='invalid-result'?'Answer rejected (invalid)':row.state==='in-flight'||!row.state?'In flight':capital(String(row.state).replaceAll('-',' '));
// One journaled PC job, without its ID: who asked which lane (unless the row's label says so), both speeds,
// the time and any flag.
function jobLine(row,{who=true}={}){return[row.model?name(row.model):'default model',who&&typeof row.client==='string'?row.client:null,who&&['fast','deep'].includes(row.lane)?`${row.lane} lane`:null,speedsText(row.promptPerSecond,row.predictedPerSecond),Number.isFinite(row.elapsedSeconds)?`${row.elapsedSeconds.toFixed(1)} s`:null,jobFlags(row).includes('hit-token-limit')?'hit token limit':null,row.cancelRequested===true?'cancel requested':null,Number.isFinite(row.timeoutSeconds)?`${Math.floor(row.ageSeconds)} s · times out at ${timeoutText(row.timeoutSeconds)}`:ageText(row.ageSeconds)].filter(Boolean).join(' · ');}
// A job's own timeout (the dispatcher's extra 30 s grace is its waiting margin, not the job's budget).
const timeoutText=seconds=>seconds<120?`${seconds} s`:`${Math.round(seconds/60)} min`;
// The drawer's body for one node; returns 'runs' when its footer should offer the call-trace view.
function inspectorBody(n,body,{firstBuild=true}={}){
  const tech=label=>disclosure(body,`${n.id}|tech`,label||'Technical details');
  if(n.kind==='jev'){
    const advisor=n.jev||jevView(null),last=advisor.lastJudgedAgeSeconds===null?'None archived':ageText(advisor.lastJudgedAgeSeconds);
    panelSummary(body,'muted',advisor.state==='configured'?'Jev opt-in configured':advisor.state==='unavailable'?'Jev opt-in unavailable':'Jev status unknown','Jev advises task admission and risk classification. This passive sample does not test its health or run a judgment.');
    factTiles(body,[['Role','Task admission and risk classification'],['Opt-in',advisor.state==='configured'?'Configured':advisor.state==='unavailable'?'Unavailable':'Unknown'],['Last judgment',last],['Live generation','Not observed']]);
    const box=tech();detailPairs(box,[['Parent route','Nisi Inference'],['Observed state',advisor.state],['Recorded detail',advisor.detail||'Unavailable'],['Latest result age',last],['Source','Local opt-in metadata and archived router judgment']]);
    box.append(el('p','detail-note','A configured opt-in is not a health check. An archived judgment records a past route decision; it does not mean Jev is generating now. Jev is outside the six LM Studio models.'));
    return null;
  }
  if(n.kind==='afm'){
    const adapter=n.afm||afmView(snapshot?.afm,{feedFresh:fresh(),host:snapshot?.host});
    panelSummary(body,adapter.tone,adapter.label,adapter.state==='executable'?'The local adapter file has execute permission. This passive sample did not run a model or measure an answer.':adapter.state==='unknown'?'The adapter state cannot be confirmed from this sample.':'The optional on-device intake adapter is unavailable.');
    factTiles(body,[['Role','Advisory intake'],['Adapter',adapter.label],['Inference','Not tested']]);
    const box=tech();detailPairs(box,[['Runtime','Apple Foundation Models'],['Host','This Mac'],['Source','Local executable metadata'],['Model invocation','None in this sample'],['Code drafting','Outside AFM role'],['Route authority','None']]);
    box.append(el('p','detail-note','AFM is separate from the six LM Studio models. A task-bound intake call may extract advisory requirements; the original task and owner checks remain authoritative.'));
    return null;
  }
  if(n.kind==='model'){const m=n.model,meta=m.metadata&&typeof m.metadata==='object'?m.metadata:{},activity=modelActivity(m),phase=STATE[m.state]||'Unknown';
    const title=activity.active?`${phase} now`:paused&&ACTIVE.has(m.state)?`Paused · last reported ${phase.toLowerCase()}`:ACTIVE.has(m.state)?'Activity unverified':{idle:'Loaded · idle',loaded:'Loaded · activity unknown',unloaded:'Not loaded',stale:'Evidence stale'}[m.state]||'State unknown';
    const meaning=activity.active?`Runtime reported ${phase.toLowerCase()} ${modelObservationAge(m)}. The aura repeats every ${(activity.periodMs/1000).toFixed(1)} s in this phase; completion progress is not reported.`:paused&&ACTIVE.has(m.state)?'This is the last reported phase before the view was paused. Resume for current activity.':ACTIVE.has(m.state)?'A current per-model activity signal cannot be verified.':m.loaded===true?'Resident in memory. Only a fresh, verified busy or generating signal animates this model.':m.state==='unloaded'?'Installed on this Mac, not in memory.':'The runtime has not reported this model recently.';
    panelSummary(body,activity.active?'live':paused?'muted':m.loaded===true?'ok':'muted',title,meaning);
    factTiles(body,[['State',phase],['Queued',formatTokens(m.queued)],['Size',meta.parameters?String(meta.parameters):m.sizeBytes?(m.sizeBytes/1e9).toFixed(1)+' GB':'Unknown'],['Capabilities',n.visual?.capabilityLabel||'Unknown'],['Context',formatTokens(m.context??meta.maxContext)]]);
    const box=tech();detailPairs(box,[['Model ID',m.id],['Host',m.host==='mac'?'This Mac':m.host==='windows'?'Windows':'Unknown'],['Friendly name',m.name],...(m.host==='windows'&&m.metadata?.lane?[['Lane',`${m.metadata.lane} · port ${m.metadata.port} · ${m.metadata.device==='none'?'CPU':m.metadata.device||'device unknown'}`],['Served model',m.metadata.servedModel||'Unknown'],['Slots',Number.isInteger(m.metadata.slotsTotal)?`${m.metadata.slotsBusy}/${m.metadata.slotsTotal} busy`:'Unknown'],['Live decode',Number.isFinite(m.metadata.liveTokensPerSecond)?`${m.metadata.liveTokensPerSecond} tok/s (between two slot polls)`:'Not generating, or not measurable yet'],['Lane detail',m.metadata.detail||'OK']]:[]),['Model key',m.modelKey],['Instance ID',m.instanceId],['Type',meta.type],['Publisher',meta.publisher],['Architecture',meta.architecture],['Quantization',meta.quantization],['Bits per weight',meta.bitsPerWeight],['Parameters',meta.parameters],['Star size basis',n.visual?.value],['Format',meta.format],['Capabilities',capabilityDetail(meta.capabilities)],['Loaded instances',meta.loadedInstances],['Loaded instance IDs',m.loadedInstanceIds],['Loaded instance count',meta.loadedInstanceCount],['TTL',m.ttl??meta.ttl],['Configuration',m.config??meta.config],['Queued requests',formatTokens(m.queued)],['Parallel slots',formatTokens(m.parallel)],['Observation age',modelObservationAge(m)],['Model size',m.sizeBytes?(m.sizeBytes/1e9).toFixed(2)+' GB':'Unknown'],['Source',m.source],['Completion progress','Not reported'],['Tokens / second','Unknown']]);box.append(el('p','detail-note','Aura speed follows the reported busy or generating phase. It does not represent a completion percentage. Loaded means resident in memory; only a fresh, verified activity signal animates the node.'));return null;}
  if(n.kind==='model-overflow'){panelSummary(body,'muted',`${n.models.length} more installed models`,'Grouped to keep the runtime lane readable; the sidebar lists each one.');const box=tech();detailPairs(box,[['Exact model IDs',n.models.map(m=>m.id).join(' · ')],['Activity',n.models.some(m=>ACTIVE.has(m.state))?'Active models are shown individually on the map':'No additional active models are hidden']]);return null;}
  if(n.kind==='call'){const c=n.call,status=typeof c.status==='string'?c.status:null;
    panelSummary(body,!status?'muted':/fail|error|reject/i.test(status)?'bad':'ok',status?`Receipt: ${status.replaceAll('_',' ').toLowerCase()}`:'Call recorded','A returned receipt reports a call result; it does not certify the candidate.');
    factTiles(body,[['Role',c.role||c.stage],['Duration',Number.isFinite(c.elapsedMs)?`${(c.elapsedMs/1000).toFixed(2)} s`:'Unknown'],['Tokens',formatTokens(c.usage?.totalTokens)]]);
    const box=tech();detailPairs(box,[['Model',c.servedModel||c.model||c.requestedModel||'Model identity unavailable'],['Requested model',c.requestedModel],['Reported model',c.servedModel],['Input tokens',formatTokens(c.usage?.inputTokens)],['Output tokens',formatTokens(c.usage?.outputTokens)],['Recorded route',n.run.runId],['Call identity',c.id],['Decision',decisionText(c.decision)]]);box.append(el('p','detail-note','A returned receipt establishes a reported call result. It does not certify the candidate or prove the model is working now.'));return null;}
  if(n.kind==='run'){const r=n.run,review=reviewConsistencyView(r),rejected=r.hostAcceptance==='REJECTED';
    panelSummary(body,rejected||review.level==='block'?'bad':review.level==='warn'?'warn':r.status==='RESPONSE_VALIDATED'?'ok':'muted',rejected?'The host rejected this route':review.level?review.badge:r.status==='RESPONSE_VALIDATED'?'Response validated':ROUTE_WORDS[r.status]||'Route recorded',review.level?review.text:r.status==='RESPONSE_VALIDATED'?'The response passed validation; that is not a certification.':'A recorded route; live execution is not inferred.');
    const notes=el('div');renderRunSummary(body,r,notes);renderCompletionEvidence(body,r);const box=tech();box.append(notes);
    const id=el('div','inspect-id run-id');identifier(id,r.runId);box.append(id);
    detailPairs(box,[['Source',runSource(r)],['Route status',routeLabel(r)],['Host acceptance',r.hostAcceptance||'Not recorded'],...(review.level?[['Review stage',review.stage],['Review rule',review.rule],['Review evidence',review.evidence]]:[]),['Last checkpoint',r.stage],['Client',r.client],['Recorded time',Number.isFinite(r.recordedAt)?new Date(r.recordedAt*1000).toLocaleString():'Unknown'],['Jev route advice',r.routeChoice],['Jev route state',r.routeState],['Live execution','Not inferred from this record']]);box.append(el('p','detail-note',r.note||'Edges group exact call receipts under this route. They do not imply an unrecorded dependency or correctness.'));return null;}
  if(n.kind==='pipeline'){const p=snapshot.pipeline||{},comp=id=>(Array.isArray(snapshot.components)?snapshot.components.find(c=>c.id===id):null),isFresh=fresh(),recovery=nisiRecovery();
    // A pending Nisi record leads the summary (its age and owner when the snapshot has them); Fix Nisi Inference is the footer action.
    if(recovery.needed)panelSummary(body,'warn',recovery.line,recovery.meaning);
    else{const routes=routePipelines(),running=routes.filter(r=>r.live&&r.status==='running').length;panelSummary(body,!isFresh?'muted':p.status==='installing'?'warn':p.status==='running'||p.status==='queued'?'live':p.runId?'warn':p.status==='idle'?'ok':'muted',!isFresh?'Route age unknown':p.status==='installing'?'Router install in progress':p.status==='running'&&running>1?`${running} routes running`:p.status==='running'?`Route running · ${String(p.stage||'stage unknown').replaceAll('_',' ')}`:p.status==='queued'?`Route ${routeQueuedWords(p)}`:p.runId?'Unsettled run recorded':p.status==='idle'?'Route idle':'Route state unknown','Nisi validates the work contract; Jev advises routing.');}
    // Every routed task the router lists, stacked: running, queued for a lane, or left unresolved.
    const routes=isFresh?routePipelines():[];if(routes.length){const list=el('div','route-runs');list.append(el('p','route-runs-head',`Routed tasks (${routes.length})`),...routes.map(r=>el('p','step-line route-run',routeRunLine(r))));body.append(list);}
    factTiles(body,[['Route',isFresh?(p.status==='recovery-required'?'Recovery required':capital(p.status||'Unknown')):'Unknown'],['Nisi',isFresh?compLabel(comp('nisi')):'Unknown'],['Jev',isFresh?compLabel(comp('jev')):'Unknown'],['Recovery',recovery.needed&&recovery.age?`Pending ${recovery.age}`:p.recoveryRequired?'Marker present':'None']]);
    const box=tech();detailPairs(box,[['Shared route',p.status],['Unresolved run',p.runId||'None recorded'],['Checkpoint',p.stage],['Recovery marker',p.recoveryRequired?'Present':'Not observed'],...(recovery.needed?[['Pending record age',recovery.age||'Not reported'],['Pending record owner',recovery.owner||'Not reported']]:[]),['Nisi',comp('nisi')?`${comp('nisi').state} · ${comp('nisi').detail}`:'Unknown'],['Jev',comp('jev')?`${comp('jev').state} · ${comp('jev').detail}`:'Unknown']]);box.append(el('p','detail-note',recovery.needed&&snapshot.host==='mac'?'This observer reads their recorded evidence and does not invoke them. Fix Nisi Inference opens the combined Fix inference panel; nothing runs until you press Run check.':'This observer reads their recorded evidence and does not invoke them.'));return'runs';}
  if(n.kind==='mac-peer'||n.kind==='mac-model'){const peer=macPeerView(snapshot?.macPeer,{feedFresh:fresh(),snapshotAge:Math.max(0,sampleAge())});
    panelSummary(body,peer.reachable?'ok':peer.state==='unreachable'?'warn':'muted',peer.reachable?'Mac answering on the LAN':peer.state==='unreachable'?'Mac not answering':'Mac reachability unknown',
      n.kind==='mac-model'?`${n.macModel.id} was listed as loaded by the Mac's LM Studio. A loaded model is not proof that it is generating.`:peer.reachable?"The Mac's LM Studio answered this PC's model listing. This is inventory only; it does not prove the Mac is generating.":peer.state==='unreachable'?'Neither the mDNS name nor the recorded addresses answered. The Mac may be asleep, off the network, or LM Studio may not be serving on the LAN.':'The first LAN probe has not finished, or its result is stale.');
    factTiles(body,[['Reachability',peer.rows[0][1]],['Round trip',peer.rows[2][1]],['Loaded',peer.loadedKnown?String(peer.loaded.length):'Unknown'],['Probe age',peer.rows[5][1]]]);
    const box=tech();detailPairs(box,[['Host','Mac (LAN peer)'],...peer.rows,['Listed models',peer.models.length?peer.models.map(m=>`${m.id} (${m.state})`).join(', '):'None or unknown']]);
    box.append(el('p','detail-note','Probed about every 10 seconds from this PC through the Mac\'s LM Studio model listing. The PC never loads, unloads or sends a prompt to the Mac from this view.'));return null;}
  if(n.kind==='windows-worker'){const worker=n.windowsWorker,jobs=n.windowsJobs||{inFlight:[],recent:[],lastVerified:null},last=jobs.lastVerified,lanes=n.windowsLanes,isFresh=fresh(),age=Math.max(0,sampleAge()),heartbeatAge=Number.isFinite(worker.ageSeconds)?worker.ageSeconds+age:null,heartbeatFresh=isFresh&&['advertised','degraded','stopped'].includes(worker.state)&&heartbeatAge!==null&&heartbeatAge>=0&&heartbeatAge<=60,advertised=n.workerAdvertised&&heartbeatFresh,condition=heartbeatFresh&&['degraded','stopped'].includes(worker.state)?worker.state:null,jobFeedKnown=isFresh&&snapshot?.windowsJobs?.schemaVersion===1&&Array.isArray(snapshot.windowsJobs.inFlight)&&Array.isArray(snapshot.windowsJobs.recent),job=jobs.running?jobs.inFlight[0]:null,modelIds=advertised&&Array.isArray(worker.modelsAdvertised)?worker.modelsAdvertised.join(', '):'Unknown';
    const who=job?.client?(CLIENTS.find(([id])=>id===job.client)?.[1]||job.client):'A client';
    panelSummary(body,job?'live':condition==='degraded'?'warn':condition==='stopped'?'bad':(last&&jobs.current)||advertised?'ok':'muted',job?`Job in flight · ${Math.floor(job.ageSeconds)} s so far`:condition==='degraded'?'Worker degraded':condition==='stopped'?'Worker stopped':last&&jobs.current?`Last answer ${ageText(last.ageSeconds)}`:advertised?'Worker heartbeat received':!heartbeatFresh?'Worker heartbeat unknown or stale':'Worker not verified',
      job?`${who} is waiting on the ${['fast','deep'].includes(job.lane)?`${job.lane} lane`:'PC'}.`:condition==='degraded'?'The heartbeat is fresh, but no model lane answers.':condition==='stopped'?'The worker reports it has stopped; start it on the PC.':last&&jobs.current?'A validated answer came back; that does not mean it is generating now.':advertised?'A fresh heartbeat advertises models; no recent answer is journaled.':'No fresh worker heartbeat; the dashed link is a configured lane, not a live one.');
    const lanesFresh=heartbeatFresh&&lanes?.visible,gpu=pcGpuView(worker,{feedFresh:isFresh,snapshotAge:age});
    factTiles(body,[['Connection',heartbeatFresh?condition?capital(condition):advertised?'Heartbeat received':'Worker reported':'Unknown'],['PC LLM switch',heartbeatFresh?headlessShort(lanes?.headless):'Unknown'],['Worker version',heartbeatFresh?gpu.version||'Unknown':'Unknown'],['Heartbeat',heartbeatFresh?ageText(heartbeatAge):'Unknown']]);
    const hardware=el('div','panel-section');hardware.append(el('p','panel-label','PC graphics'));detailPairs(hardware,gpu.rows.filter(([label])=>label!=='Worker version'));body.append(hardware);
    const laneRows=[];for(const id of ['fast','deep']){const lane=lanesFresh?lanes.lanes.find(item=>item.id===id):null,recorded=jobFeedKnown?laneSpeeds(jobs,id):null;
      laneRows.push([`${id==='fast'?'Fast':'Deep'} lane`,lane?`${lane.label.slice(id.length+3)} · ${lane.up?'Up':'Down'} · ${lane.total===null?'slots unknown':`${lane.busy}/${lane.total} slots busy`}`:'Unknown (no fresh lane heartbeat)']);
      laneRows.push([`${id==='fast'?'Fast':'Deep'} last measured speed`,recorded?.text?`${recorded.text} · ${ageText(recorded.ageSeconds)} · recorded answer`:'Unknown']);}
    const lanePanel=el('div','panel-section');lanePanel.append(el('p','panel-label','Model lanes'));detailPairs(lanePanel,laneRows);body.append(lanePanel);
    const jobPanel=el('div','panel-section');jobPanel.append(el('p','panel-label','Jobs from the Mac dispatcher journal'));detailPairs(jobPanel,[['Current job',jobFeedKnown?(job?jobLine(job):'No job in flight'):'Unknown (job feed stale or unavailable)'],['Last validated answer',last?`${jobLine(last)} · recorded${jobs.current?'':' · not current'}`:jobFeedKnown?'None journaled':'Unknown']]);body.append(jobPanel);
    if(jobs.recent.length){const box=disclosure(body,`${n.id}|recent`,`Recent jobs (${Math.min(8,jobs.recent.length)})`);detailPairs(box,jobs.recent.slice(0,8).map(row=>[`${jobStatus(row)} · …${String(row.id).slice(-8)}`,jobLine(row)]));}
    const box=tech();detailPairs(box,[['Host','Windows'],['Worker state',condition==='degraded'?'Degraded: no model lane answers':condition==='stopped'?'Stopped':advertised?'Advertised':'Unverified'],['Heartbeat age',heartbeatFresh?ageText(heartbeatAge):'Unknown (stale or unverified)'],['Advertised models',modelIds],['Worker detail',heartbeatFresh?worker.detail:'Unknown (stale or unverified)'],['Last validated inference',last?`${last.model} · ${last.elapsedSeconds.toFixed(1)} s · ${ageText(last.ageSeconds)}${jobs.current?'':' · not current'}`:'None journaled on this Mac'],['Jobs in flight',jobFeedKnown?String(jobs.inFlight.length):'Unknown (stale feed)'],['PC headless',heartbeatFresh?headlessText(worker.headless,n.windowsLanes):'Unknown (stale heartbeat)'],...gpu.rows.filter(([label])=>label!=='Worker version'),...windowsLaneRows(lanesFresh?lanes:{...lanes,visible:false,beat:false})]);
    box.append(el('p','detail-note',last||jobs.running?'Jobs come from the Mac dispatcher journal. A validated result shows that the Windows worker returned an answer for that job at that time; it does not prove the worker is generating now.':advertised?'A recent worker heartbeat advertises inventory only. It does not establish Windows inference, model residency, or native monitor availability.':'This observer has not verified a fresh worker heartbeat. The dashed link marks a configured lane, not a verified live connection.'));return null;}
  if(n.kind==='windows-lane'){const lane=n.lane,jobs=graph.nodes.find(node=>node.kind==='windows-worker')?.windowsJobs,speeds=laneSpeeds(jobs,lane.id),jobFeedKnown=fresh()&&snapshot?.windowsJobs?.schemaVersion===1&&Array.isArray(snapshot.windowsJobs.inFlight)&&Array.isArray(snapshot.windowsJobs.recent),current=jobFeedKnown&&jobs?.running?jobs.inFlight.find(row=>row.lane===lane.id):null,last=jobFeedKnown?[...(jobs?.recent||[])].filter(row=>row.lane===lane.id&&row.state==='success'&&Number.isFinite(row.elapsedSeconds)).sort((a,b)=>a.ageSeconds-b.ageSeconds)[0]:null;
    panelSummary(body,!lane.up?'bad':lane.live||lane.busy>0?'live':'ok',!lane.up?'Lane down':lane.live?'Job in flight now':lane.busy>0?`${lane.busy} of ${lane.total} slots busy`:'Up · idle',!lane.up?'The worker reports this lane’s model server is not answering.':'Slots come from a fresh worker heartbeat; speeds from the newest answer.');
    factTiles(body,[['Model',lane.label.slice(lane.id.length+3)],['Slots',lane.total!==null?`${lane.busy} of ${lane.total} busy`:'Unknown'],['Current job',jobFeedKnown?current?`${Math.floor(current.ageSeconds)} s so far`:'None in flight':'Unknown'],['Last answer',last?`${last.elapsedSeconds.toFixed(1)} s · ${ageText(last.ageSeconds)}`:'Unknown'],['Last measured speed',speeds.text?`${speeds.text} · ${ageText(speeds.ageSeconds)}`:'Unknown']]);
    if(speeds.limitHit){const flag=el('p','panel-line');flag.append(el('span','flag-badge','hit token limit'),document.createTextNode(' The newest answer stopped at its token limit.'));body.append(flag);}
    const box=tech();detailPairs(box,[['Lane',lane.id==='fast'?'Fast':'Deep'],['Model alias',lane.label.slice(lane.id.length+3)],['Last measured speed',speeds.text?`${speeds.text} · ${ageText(speeds.ageSeconds)}`:'No successful job journaled'],['PC headless',n.headless.label]]);box.append(el('p','detail-note','Lane state comes from a fresh Windows worker heartbeat relayed by the Mac dispatcher. Client lines come from the dispatcher journal; a dashed line is a recent job, a moving line a job in flight.'));return null;}
  if(n.kind==='client'){const c=n.client;
    panelSummary(body,'muted','Subagent activity unknown','No verified lifecycle feed is connected. The model branches show recorded or saved identities.');
    factTiles(body,[['Subagents in use','Unknown'],['Latest model',c.model?trim(name(c.model),24):'Unknown'],['Model identity',CLIENT_STATE[c.modelState]],['Model age',clientAge(c)]]);
    const box=tech();detailPairs(box,[['Subagent source',c.subagents.source],['Subagent age','Unknown'],['Model ID count',`${c.models.length} distinct recorded or saved`],['Model IDs',c.models.length?c.models.map(m=>m.id).join(' · '):'Unknown'],['Model source',c.source],['Model source detail',c.detail]]);box.append(el('p','detail-note','Model IDs and file ages do not establish a live subagent count or generation state. Select a model branch to inspect its own source.'));return null;}
  if(n.kind==='client-model'){const c=n.client,m=n.clientModel;
    panelSummary(body,m?'ok':'muted',m?CLIENT_STATE[m.modelState]:'Model unknown','A recorded identity is not a live generation signal.');
    factTiles(body,[['Client',c.label],['Identity',m?CLIENT_STATE[m.modelState]:'Unknown'],['Age',m?clientAge(m):'Unknown'],['Activity','Unknown']]);
    const box=tech();detailPairs(box,[['Model',m?.id],['Source',m?.source||c.source]]);box.append(el('p','detail-note','Client activity remains unknown.'));return null;}
  if(n.kind==='clients'){panelSummary(body,'muted','Subagent activity unknown','These branches show recorded model IDs or saved session choices.');const box=tech();detailPairs(box,[['Scope',CLIENTS.map(([,label])=>label).join(', ')],['Map lines','Client to recorded model identities'],['Subagent source',SUBAGENT_UNKNOWN.source]]);return null;}
  const rows=models(),isFresh=fresh(),known=liveNow()&&snapshot.activityKnown===true,active=rows.filter(m=>modelActivity(m).active).length,loaded=rows.filter(m=>m.loaded===true).length;
  const gpu=macGpuView(snapshot.macGpu,{feedFresh:isFresh,snapshotAge:Math.max(0,sampleAge())}),callers=localCallersView(snapshot.localCallers,{feedFresh:isFresh});
  panelSummary(body,!isFresh||paused?'muted':active?'live':known?'ok':'muted',paused?'View paused':!isFresh?'Signal stale':active?`${active} model${active===1?'':'s'} working`:known?`Quiet · ${loaded} loaded`:'Activity unknown',paused?'Resume to verify current model activity.':!isFresh?'Local observations have aged out; activity is unknown.':active?'The runtime reports local model activity right now.':known?'No local generation is reported right now.':'Inventory is available; generation could not be verified.');
  const memory=snapshot.memory===undefined?null:memoryView(snapshot.memory,{feedFresh:isFresh});
  factTiles(body,[['Working',known?String(active):'Unknown'],['Loaded',isFresh?String(loaded):'Unknown'],...(memory?[['Memory',memory.tile,true]]:[]),['GPU · all apps',gpu.tile,Boolean(memory)]]);
  if(callers.text&&callers.names.length)body.append(el('p','panel-line',callers.text));
  if(memory)memorySection(body,n,memory,{open:firstBuild&&memory.alert});
  const box=tech();detailPairs(box,[['Source','LM Studio runtime + inventory'],['Working models',known?String(active):'Unknown'],...gpu.rows,['Local callers',callers.checked?callers.names.join(', ')||'None connected':'Not checked while local models are idle'],['Map lines','Observation groups'],['Updates','Live stream, polling as fallback']]);box.append(el('p','detail-note','A light on the map is evidence, not decoration. Missing telemetry stays unknown. Select any model to inspect its state.'));return null;
}
function runtimeCopy(isFresh,known,activeCount,inventoryLive,loadedCount,queued){if(!isFresh)return{lead:'Local runtime',em:'signal stale.',sub:'Reconnecting. Previous local observations have aged out.'};if(activeCount)return{lead:'Local runtime',em:'working now.',sub:`${activeCount} local model${activeCount===1?' is':'s are'} working, as reported by the runtime.`};if(known)return{lead:'Local runtime',em:'quiet now.',sub:`${loadedCount===null?'Loaded count unknown':loadedCount+' loaded'}. ${queued===null?'Queue unknown':queued+' queued'}. No current local generation reported.`};if(inventoryLive)return{lead:'Local runtime',em:'activity unknown.',sub:'Model inventory is available. Local generation could not be verified.'};return{lead:'Local runtime',em:'signal unavailable.',sub:'Local runtime sources are unavailable; local model activity is unknown.'};}
function renderSummary(){const rows=models(),known=liveNow()&&snapshot.activityKnown===true,loaded=rows.filter(m=>m.loaded===true),active=rows.filter(m=>modelActivity(m).active),inventoryLive=snapshot.sources.some(s=>['lmstudio-api','lms-ps','amd-models',...(snapshot.host==='windows'?['lane-inventory']:[])].includes(s.id)&&s.state==='live'),hostInventoryLive=snapshot.sources.some(s=>['lmstudio-api','lms-ps',...(snapshot.host==='windows'?['lane-inventory']:[])].includes(s.id)&&s.state==='live'),loadedKnown=hostInventoryLive&&rows.every(m=>typeof m.loaded==='boolean');$('activeCount').textContent=known?active.length:'—';$('loadedCount').textContent=fresh()&&loadedKnown?loaded.length:'—';const queued=known&&loaded.every(m=>Number.isSafeInteger(m.queued))?loaded.reduce((n,m)=>n+m.queued,0):null;$('queuedCount').textContent=queued??'—';const copy=paused?{lead:'Local runtime',em:'view paused.',sub:'Resume to verify current activity; the displayed model states are from the last sample.'}:clockMismatch()?{lead:'Local runtime',em:'clock mismatch.',sub:'Observer timestamp is ahead of this Mac clock; current activity cannot be verified.'}:runtimeCopy(fresh(),known,active.length,inventoryLive,loadedKnown?loaded.length:null,queued);$('headline').replaceChildren(document.createTextNode(copy.lead),el('br'),el('em',null,copy.em));$('subline').textContent=copy.sub;const identities=clientIdentityCounts();$('observedClientCount').textContent=`${identities.observed+identities.configured}/${identities.total}`;$('clientIdentityDetail').textContent=`${identities.observed} recorded · ${identities.configured} saved choices · ${identities.unknown} unknown`;
  $('sources').replaceChildren();snapshot.sources.forEach(s=>{const row=el('div','source');row.append(el('strong',null,s.label),el('span',null,!fresh()&&s.state==='live'?'stale':s.state),el('small',null,s.detail));$('sources').append(row);});const liveSources=fresh()?snapshot.sources.filter(s=>s.state==='live').length:0,recordedSources=snapshot.sources.filter(s=>s.state==='recorded').length;$('sourceCount').textContent=`${liveSources} live · ${recordedSources} recorded`;
}
// Rebuilt or reparented paths restart CSS animations; a shared phase keeps the dash flow continuous.
function fixScopeNote(action,state,steps){if(action==='fix-nisi')return['ready','needs-action','error'].includes(state)?' No model was called and nothing was sent to the PC.':'';const probe=steps.find(step=>step&&step.name==='windows-inference');if(probe)return probe.result==='deferred'||probe.result==='not-run'?' No Windows model request was sent.':'';if(action==='fix-local')return' Model generation was not probed.';return['ready','needs-action','error'].includes(state)?' No Windows model request was sent.':'';}
// Fix Nisi Inference's recorded lines leave out the input digest prefix and the recovered file name (fixNisiEvidence).
function stepText(step,action){return typeof step==='string'?step:step&&typeof step==='object'?[step.name,step.result,action==='fix-nisi'?fixNisiEvidence(step.evidence):step.evidence,step.runId||step.jobId].filter(part=>typeof part==='string'&&part).join(' · '):'';}
function onlineActionName(action){return FIX_ACTIONS.get(action)||({'readiness':'Universal readiness check','check-and-repair':'Check and repair pending records','headless-on':'Turn PC headless on','headless-off':'Turn PC headless off'}[action])||'No action';}
function onlineActionStatusLabel(state,action){
  if(state==='submitting')return'Starting check';
  if(state==='running')return FIX_ACTIONS.has(action)?`${onlineActionName(action)} check running`:HEADLESS_ACTIONS.has(action)?'PC headless switch running':action==='check-and-repair'?'Repair check running':'Readiness check running';
  if(state==='ready')return action==='fix-local'?'Local server ready':action==='fix-nisi'?'Nisi Inference ready':FIX_ACTIONS.has(action)?`${onlineActionName(action)} checks complete`:HEADLESS_ACTIONS.has(action)?'PC headless switched':action==='check-and-repair'?'Repair check complete':'Universal readiness verified';
  return({'needs-action':'Action needed','error':'Check failed','uncertain':'Request status uncertain'}[state])||'Ready to check';
}
// Online Code panel summary: a running check first, then a check that just finished (failed, needs action or
// complete, with its own message), then what the route is doing, in plain words.
function onlineCodeSummary(mode,{macOwner,actionState,action,isFresh,hasFeed=true,message=''}){
  if(!hasFeed)return{tone:'muted',line:'Waiting for the live feed',meaning:'Nothing can be confirmed until the first snapshot arrives.'};
  if(!macOwner)return{tone:'muted',line:'Evidence only on this PC',meaning:'Run checks and repairs from the Mac owner.'};
  if(['submitting','running','uncertain'].includes(actionState))return{tone:'busy',line:onlineActionStatusLabel(actionState,action),meaning:'One bounded check is in progress; another request will not start.'};
  const said=action==='fix-nisi'?fixNisiMessage(message,actionState):message,detail=typeof said==='string'&&said.trim()?said.trim():'No detail was returned.',route={processing:mode.queuedOnly?'A routed task is queued.':'A routed task is processing.',unfinished:'An unfinished router task is open.',ready:'Route idle.',inactive:'Route idle.'}[mode.state]||'Route state unknown.',named=action&&action!=='readiness'?onlineActionName(action):null;
  if(actionState==='error')return{tone:'bad',line:named?`${named} failed`:'The latest check failed',meaning:`${detail} ${route}`};
  if(actionState==='needs-action')return{tone:'warn',line:named?`${named} needs action`:'The latest check needs action',meaning:`${detail} ${route}`};
  if(mode.state==='installing')return{tone:'warn',line:'Router install in progress',meaning:'The router refuses every command until its install or rollback finishes; route state is shown again then.'};
  // Only queued runs (waiting for admission or a lane) never read as processing: nothing is running yet.
  if(mode.state==='processing'&&mode.queuedOnly)return{tone:'live',line:mode.runCounts.queued>1?'Routed tasks are queued':'A routed task is queued',meaning:`${mode.runCounts.queued>1?`${mode.runCounts.queued} coding tasks are`:'A coding task is'} waiting for admission or a lane on the Nisi Inference route; nothing is running yet.`};
  if(mode.state==='processing'&&mode.runCounts&&mode.runCounts.running+mode.runCounts.queued>1)return{tone:'live',line:'Routed tasks are processing',meaning:`${mode.runCounts.running} running and ${mode.runCounts.queued} queued through the Nisi Inference route now; each is listed below.`};
  if(mode.state==='processing')return{tone:'live',line:'A routed task is processing',meaning:'A coding task is running through the Nisi Inference route now.'};
  if(mode.state==='unfinished')return{tone:'warn',line:'An unfinished router task',meaning:'A route was left open; Check and repair reconciles exact records.'};
  if(actionState==='ready')return{tone:'ok',line:onlineActionStatusLabel('ready',action),meaning:`${detail} ${route}`};
  if(mode.state==='ready'||mode.state==='inactive')return{tone:'ok',line:'Idle · no task running',meaning:`Setup: ${mode.setupLabel.replace(/^./,s=>s.toLowerCase())}.`};
  return isFresh?{tone:'muted',line:'Route state unknown',meaning:'No fresh Online Code Mode evidence is available.'}:{tone:'muted',line:'Live feed stale',meaning:'Nothing can be confirmed until the feed is fresh again.'};
}
function updateOnlineCodeMode(){
  const mode=onlineCodeModeView(snapshot?.onlineCodeMode,{feedFresh:fresh(),paused,reducedMotion:window.matchMedia?.('(prefers-reduced-motion: reduce)').matches===true});
  const button=$('onlineCodeMode');
  const macOwner=snapshot?.host==='mac';
  // Readiness, Fix and the PC LLM switch are Mac-owner actions; the Windows edition is read-only.
  const windowsHost=snapshot?.host==='windows';for(const id of ['onlineCodeMode','fixInference','pcSwitch'])if($(id).hidden!==windowsHost)$(id).hidden=windowsHost;
  const actionState=onlineCodeActionPosting?'submitting':onlineCodeActionStatus?.status||'idle';
  const action=onlineCodeActionPosting?onlineCodeSubmittingAction:onlineCodeActionStatus?.action;
  const actionActive=actionState==='submitting'||actionState==='running'||actionState==='uncertain';
  const routePriority=mode.state==='processing'||mode.state==='unfinished';
  const actionIndicator=routePriority?'':actionActive?' action-running':actionState==='needs-action'?' action-needs-action':actionState==='error'?' action-error':'';
  const actionLabel=actionActive?actionState==='uncertain'?'Checking request status…':FIX_ACTIONS.has(action)?'Inference check running…':HEADLESS_ACTIONS.has(action)?'PC headless switching…':action==='check-and-repair'?'Repair check running…':'Readiness check running…':actionState==='needs-action'?FIX_ACTIONS.has(action)?'Inference check needs action':HEADLESS_ACTIONS.has(action)?'PC headless needs action':action==='check-and-repair'?'Repair needs action':'Readiness needs action':actionState==='error'?'Check failed':mode.label;
  button.className=`mode-tab ${mode.state}${mode.blinking?' blinking':''}${actionIndicator}`;
  const tightTab=window.matchMedia?.('(max-width: 720px)').matches===true;$('onlineCodeLabel').textContent=!snapshot?(tightTab?'Connecting…':'Waiting for the live feed'):!macOwner?'Mac owner only':routePriority||actionLabel===mode.label?onlineCodeTabLabel(mode,tightTab):actionLabel;
  const actionAria=actionActive?'Check in progress. Open progress; another request will not start.':actionState==='needs-action'?'Latest check needs action. Open details or run a new bounded recovery check.':actionState==='error'?'Latest check failed. Open details or run a new bounded recovery check.':'Check exact pending records, safely reconcile eligible ones, and verify readiness.';
  const routeAria=`Current route: ${mode.label}.`;
  button.setAttribute('aria-label',!snapshot?'Check readiness. Waiting for the live feed; nothing can be checked yet.':!macOwner?'Online Code Mode evidence. Readiness and repair are available from the Mac owner only.':`Check readiness. ${routePriority?`${routeAria} ${actionAria}`:`${actionAria} ${routeAria}`}`);
  button.setAttribute('aria-busy',String(actionActive));
  button.title=!snapshot?'Waiting for the live feed.':!macOwner?'View evidence; this Windows observer cannot run the Mac owner check.':actionActive?'Open the current check; another request will not start.':'Check and safely repair exact pending records, then verify readiness. No task or model call.';
  // The panel's skeleton is static markup, so an open disclosure and keyboard focus survive each refresh.
  const priorScroll=$('onlineCodeDetails').scrollTop;
  const summary=onlineCodeSummary(mode,{macOwner,actionState,action,isFresh:fresh(),hasFeed:Boolean(snapshot),message:onlineCodeActionStatus?.message});
  $('onlineCodeSummary').dataset.tone=summary.tone;$('onlineCodeStatus').textContent=trim(summary.line,60);$('onlineCodeMeaning').textContent=summary.meaning;
  const nisi=Array.isArray(snapshot?.components)?snapshot.components.find(component=>component.id==='nisi'):null;
  fillTiles($('onlineCodeTiles'),[['Route',{processing:mode.runCounts.running+mode.runCounts.queued>1?`${mode.runCounts.running} running · ${mode.runCounts.queued} queued`:mode.queuedOnly?'Queued':'Processing',unfinished:'Unfinished',ready:'Idle',inactive:'Idle',installing:'Installing'}[mode.state]||'Unknown'],['Setup',/^Checked/.test(mode.setupLabel)?mode.setupLabel:{'Not checked recently':'Not checked'}[mode.setupLabel]||(/expired/.test(mode.setupLabel)?'Expired':/invalid/.test(mode.setupLabel)?'Invalid':'Unknown')],['Nisi',fresh()?compLabel(nisi):'Unknown'],['PC LLM',headlessShort(headlessView())]]);
  const result=$('onlineCodeResult'),statusLabel=!macOwner?'Mac owner only':onlineActionStatusLabel(actionState,action),steps=Array.isArray(onlineCodeActionStatus?.steps)?onlineCodeActionStatus.steps.map(step=>stepText(step,action)).filter(Boolean):[];
  // A finished check leads the summary with its own message, so its result block would only repeat it.
  result.hidden=!macOwner||['ready','needs-action','error'].includes(actionState)||!(onlineCodeActionPosting||onlineCodeActionStatus&&(onlineCodeActionStatus.status!=='idle'||onlineCodeActionStatus.message));
  result.className=`mode-action-result ${actionState}`;result.replaceChildren(el('strong',null,`Latest check · ${statusLabel}`),el('p',null,onlineCodeActionPosting?'Submitting one local request…':(action==='fix-nisi'?fixNisiMessage(onlineCodeActionStatus?.message,actionState):onlineCodeActionStatus?.message)||'No detail was returned.'));
  if(action)result.append(el('small',null,`Action: ${onlineActionName(action)}`));
  $('onlineCodeSteps').hidden=!macOwner||!steps.length;$('onlineCodeStepsLabel').textContent=`Check steps (${steps.length})`;$('onlineCodeStepRows').replaceChildren(...steps.map(value=>el('p','step-line',value)));
  const repairButton=$('onlineCodeRepair');
  repairButton.hidden=!macOwner;
  repairButton.disabled=actionActive;
  repairButton.setAttribute('aria-busy',String(actionActive&&action==='check-and-repair'));
  repairButton.title='Explicitly inspect and reconcile only exact, safe pending records. This may change router or Windows job state.';
  const headlessButton=$('pcHeadless'),headlessNext=headlessAction();
  headlessButton.hidden=!macOwner||headlessView().label==='Headless unknown';
  headlessButton.disabled=actionActive||!fresh();
  headlessButton.textContent=headlessNext==='headless-off'?'Turn PC LLM off':'Turn PC LLM on';
  headlessButton.setAttribute('aria-busy',String(actionActive&&HEADLESS_ACTIONS.has(action)));
  headlessButton.title=headlessNext==='headless-off'?'Release the Windows PC from headless LLM use.':'Hold the Windows PC as a headless LLM for 4 hours. This sends one short probe request to the PC.';
  // No visible action (no snapshot yet, a Windows observer): no empty sticky footer bar either.
  $('onlineCodeActions').hidden=repairButton.hidden&&headlessButton.hidden;
  const sw=headlessSwitchView(headlessView(),{macOwner,feedFresh:fresh(),pending:actionActive&&HEADLESS_ACTIONS.has(action),busy:actionActive}),pcSwitch=$('pcSwitch');
  pcSwitch.hidden=sw.hidden;pcSwitch.disabled=sw.disabled;pcSwitch.className=`pc-switch ${sw.state}`;pcSwitch.setAttribute('aria-checked',String(sw.checked));pcSwitch.setAttribute('aria-busy',String(sw.state==='pending'));pcSwitch.title=sw.title;$('pcSwitchLabel').textContent=sw.short;
  pcSwitch.setAttribute('aria-label',`PC headless LLM: ${sw.state==='on'?'on, '+sw.short:sw.state==='pending'?'switching':sw.state}`);
  const nisiStatus=fresh()&&nisi?`${nisi.state} · ${nisi.detail}`:'Unknown; no fresh Nisi component observation';
  const rows=[['Task',mode.taskLabel],['Setup',mode.setupLabel],['Nisi route adapter',nisiStatus],['PC headless',windowsLaneView(snapshot?.windowsWorker,null,{feedFresh:fresh(),snapshotAge:Math.max(0,sampleAge())}).headless.label],['Bound program',mode.client||'Unknown / unbound'],['Bound chat',mode.chatId||'Unknown / unbound'],['Route',mode.routeId||'Unknown / unbound'],['Evidence',mode.evidence],['Observed at',mode.observedAt||'Unknown'],['Live feed',fresh()?'Fresh':'Stale or unavailable'],
    // Router concurrency (spec 6.13): one row per listed run and per queued run, then the lanes and the policy.
    ...mode.runRows.map(r=>[`Run ${r.runId}`,[r.stateLabel,CLIENTS.find(([id])=>id===r.client)?.[1],r.host==='windows'?'PC':r.host==='mac'?'Mac':r.host==='auto'?'auto':null,r.stage?r.stage.replaceAll('_',' '):null].filter(Boolean).join(' · ')]),
    ...routeQueueRows(snapshot?.onlineCodeMode&&fresh()?snapshot.onlineCodeMode:null,{snapshotAge:Math.max(0,sampleAge())}).map(q=>[`Queued ${q.runId}`,[q.waitingFor,q.secondsLeft===null?null:`${q.secondsLeft} s left`,CLIENTS.find(([id])=>id===q.client)?.[1]].filter(Boolean).join(' · ')]),
    ...(mode.runRows.length||mode.queueRows.length?[['Lanes',mode.lanes.text]]:[]),['Admission',mode.admission],...(mode.runsTruncated?[['Listing','More runs than shown']]:[])];
  const rowsBox=$('onlineCodeRows');rowsBox.replaceChildren();detailPairs(rowsBox,rows);
  const privateVersion=nisiV02View(snapshot?.nisiV02,{feedFresh:fresh()});
  const privateSource=Array.isArray(snapshot?.sources)?snapshot.sources.find(source=>source.id==='nisi-v02-runtime'):null;
  const privateRows=[['Source',fresh()&&privateSource?`${privateSource.state} · ${privateSource.detail}`:'Unknown; monitor feed stale or unavailable'],['Version',privateVersion.version],['Runtime integrity',privateVersion.runtimeIntegrity],['Host bridge binding',privateVersion.hostBinding],['Changed host files',privateVersion.driftedHostFiles.length?privateVersion.driftedHostFiles.join(', '):privateVersion.hostBinding==='VERIFIED'?'None observed':'Unknown'],['Historical activation',privateVersion.activationStatus],['Activation verified at',privateVersion.activationVerifiedAt||'Unknown'],['Historical model probe',privateVersion.activationProbe],['Live model inference',privateVersion.liveInference],['Workflow acceptance',privateVersion.workflowAcceptance],['Release acceptance',privateVersion.releaseAcceptance]];
  const privateBox=$('onlineCodeV02Rows');privateBox.replaceChildren();detailPairs(privateBox,privateRows);
  $('onlineCodeDetails').scrollTop=priorScroll;
  updateInferenceFix();
}
// The top-bar tab's short route line ("Idle · checked 42s ago"); its aria label keeps the full wording.
function onlineCodeTabLabel(mode,tight=false){if(mode.state==='ready'||mode.state==='inactive'){const setup=mode.setupLabel;return`Idle · ${/^Checked/.test(setup)?setup.replace(/^Checked/,'checked').replace(tight?/ ago$/:/$^/,''):setup==='Not checked recently'?'not checked':/expired/.test(setup)?(tight?'expired':'receipt expired'):/invalid/.test(setup)?(tight?'invalid':'receipt invalid'):'setup unknown'}`;}
  // "Install in progress" covers a rollback too (the spec's umbrella term), as on every other surface.
  if(mode.state==='installing')return tight?'Install in progress':'Router install in progress';
  // Only queued runs: the tab says queued, never processing.
  if(mode.state==='processing'&&mode.queuedOnly)return mode.runCounts.queued>1?(tight?`${mode.runCounts.queued} queued`:`Queued · ${mode.runCounts.queued} tasks`):mode.client&&!tight?`Queued · ${mode.client}`:'Task queued';
  const several=mode.state==='processing'&&mode.runCounts&&mode.runCounts.running+mode.runCounts.queued>1;
  if(several)return tight?`${mode.runCounts.running} running · ${mode.runCounts.queued} queued`:`Processing · ${mode.runCounts.running} running · ${mode.runCounts.queued} queued`;
  return mode.state==='processing'?(mode.client&&!tight?`Processing · ${mode.client}`:'Task processing'):mode.state==='unfinished'?'Unfinished task':'Route unknown';}
function headlessView(){return windowsLaneView(snapshot?.windowsWorker,null,{feedFresh:fresh(),snapshotAge:Math.max(0,sampleAge())}).headless;}
function headlessAction(){return headlessView().on?'headless-off':'headless-on';}
function updateInferenceFix(){
  const button=$('fixInference'),macOwner=snapshot?.host==='mac';
  const action=onlineCodeActionPosting?onlineCodeSubmittingAction:onlineCodeActionStatus?.action;
  const state=onlineCodeActionPosting?'submitting':onlineCodeActionStatus?.status||'idle';
  const active=['submitting','running','uncertain'].includes(state);
  const fixAction=FIX_ACTIONS.has(action);
  button.classList.toggle('fix-tab-running',active&&fixAction);
  button.classList.toggle('fix-tab-needs-action',fixAction&&state==='needs-action');
  button.classList.toggle('fix-tab-error',fixAction&&state==='error');
  button.setAttribute('aria-busy',String(active&&fixAction));
  button.setAttribute('aria-label',!snapshot?'Fix inference. Waiting for the live feed.':!macOwner?'Fix inference. Available only on the Mac owner.':active?'Fix inference is running. Open its progress; no second request can start.':'Fix inference. Check and repair inference and show its progress.');
  const submit=$('fixInferenceSubmit');
  submit.disabled=!macOwner||active;
  submit.textContent=active?'Check in progress…':'Run check';
  const renderKey=JSON.stringify([Boolean(snapshot),macOwner,action,state,onlineCodeActionPosting,onlineCodeActionStatus?.message,onlineCodeActionStatus?.steps]);
  if(renderKey!==fixInferenceRenderKey){fixInferenceRenderKey=renderKey;renderFixResult({macOwner,action,state,active,fixAction});}
  updateFixScopeNote();
}
// Idle, the summary explains the combined repair; once a check has run, it shows that check's result.
function renderFixResult({macOwner,action,state,active,fixAction}){
  // The result is the panel's summary: dot, one status line and its meaning; every step waits in a disclosure.
  const result=$('fixInferenceResult'),shown=fixAction?state:'idle';
  result.className=`mode-action-result panel-summary ${shown}`;
  result.dataset.tone=({submitting:'busy',running:'busy',uncertain:'busy',ready:'ok','needs-action':'warn',error:'bad'})[shown]||(active?'busy':'muted');
  result.replaceChildren();
  const scopeSummary=Boolean(snapshot)&&macOwner&&!active&&(!fixAction||state==='idle');result.dataset.scopeSummary=String(scopeSummary);
  const statusLabel=!snapshot?'Waiting for the live feed':!macOwner?'Mac owner only':scopeSummary?'Ready to check':fixAction?onlineActionStatusLabel(state,action):active?'Another check is in progress':'Ready to check';
  const detail=!snapshot?'Nothing can be checked until the first snapshot arrives.':!macOwner?'This observer shows evidence only. Use the Mac owner to run a fix.':scopeSummary?fixScopeText():fixAction?(onlineCodeActionPosting?'Submitting one local request…':(action==='fix-nisi'?fixNisiMessage(onlineCodeActionStatus?.message,state,{scoped:true}):onlineCodeActionStatus?.message)||'The local monitor has not returned a result yet.'):'Wait for the current Online Code Mode check to finish. No new request has been sent.';
  const copy=el('div','panel-summary-copy');copy.append(el('strong',null,statusLabel),el('p',null,detail));
  const steps=fixAction&&Array.isArray(onlineCodeActionStatus?.steps)?onlineCodeActionStatus.steps:[];
  if(fixAction&&!scopeSummary)copy.append(el('small',null,`Scope: ${onlineActionName(action)}.${fixScopeNote(action,state,steps)}`));
  result.append(el('i','status-dot'),copy);
  const lines=steps.map(step=>stepText(step,action)).filter(Boolean);
  // Fix Nisi Inference lists its steps in plain words under the result (not a live region: the summary above announces the outcome);
  // the recorded step lines stay in the disclosure.
  const plain=action==='fix-nisi'?fixNisiStepsView(steps):[],list=$('fixInferencePlainSteps');list.hidden=!plain.length;
  list.replaceChildren(...plain.map(step=>{const li=el('li','fix-step'),mark=el('span','fix-step-mark',step.mark),copy=el('span','fix-step-copy');li.dataset.tone=step.tone;mark.setAttribute('aria-hidden','true');copy.append(el('strong',null,step.label),document.createTextNode(' '),el('span',null,step.text));li.append(mark,copy);return li;}));
  $('fixInferenceSteps').hidden=!lines.length;$('fixInferenceStepsLabel').textContent=`Check steps (${lines.length})`;$('fixInferenceStepRows').replaceChildren(...lines.map(value=>el('p','step-line',value)));
}
const fixScopeText=()=> 'Checks and repairs the Mac runtime, Windows route and Nisi Inference in one guarded run.';
function updateFixScopeNote(){const note=fixScopeText(),result=$('fixInferenceResult'),idle=result.dataset.scopeSummary==='true';$('fixScopeNote').textContent=note;$('fixScopeNote').hidden=idle;if(idle){const line=result.querySelector('p');if(line&&line.textContent!==note)line.textContent=note;}}
// Inspector opening is read-only until Run check. The top button always requests the combined repair.
// Keep the running action visible while the shared guard blocks another request.
function openFixInference(scope){
  const fromInspector=scope==='nisi';
  // Where Escape or × returns to: the inspector this was opened from (closeFixInference).
  const from=fromInspector&&!$('drawer').hidden?selected:null;
  hideOnlineCodeDetails();
  if(compactEvidence())hideActivityDetails();
  if((compactEvidence()||fromInspector)&&!$('drawer').hidden)closeDrawer(false);
  const panel=$('fixInferenceDetails');
  panel.hidden=false;lastEvidencePanel='fixInferenceDetails';fixReturnTo=from;
  $('fixInference').setAttribute('aria-expanded','true');
  updateInferenceFix();pollOnlineCodeAction();
  panel.focus({preventScroll:true});
  if(selected)revealSelected();else if(!userCamera)fitMap();
}
// Closing Fix inference (Escape or ×) goes back where it was opened: the inspector it came from, with focus on its
// Fix Nisi Inference (or on the node when that action is gone), else the top-bar Fix tab.
function closeFixInference(){const back=fixReturnTo;hideFixInferenceDetails();
  if(back&&view==='runtime'&&graph.nodes.some(n=>n.id===back)){nodeSelect(back);const fix=$('drawerFixButton');if(!fix.hidden&&!$('drawer').hidden)fix.focus({preventScroll:true});else nodeElements.get(back)?.focus({preventScroll:true});return;}
  $('fixInference').focus({preventScroll:true});}
// Memory banner: tight (amber) or critical (red, with "!") memory from the guard, naming the largest user and the first
// suggestion. Dismissing holds that level (memoryDismissal): a worse level or ok ends it, and a lower level lowers it only
// after a minute of fresh samples under it, so a Mac flapping on a threshold does not bring it back. The announcer holds
// the level it spoke the same way (memoryAnnouncement): it speaks when the banner appears after ok or the level rises,
// never again for a flap back or a stale spell. memoryClock is the sample time in seconds.
let memoryDismissed=null,memoryAnnounced=null;
const memoryClock=()=>Date.now()/1000;
function memoryNow(){return memoryView(snapshot?.memory,{feedFresh:fresh()});}
function renderMemoryBanner(){
  const banner=$('memoryBanner');if(!banner)return;const memory=memoryNow(),at=memoryClock();
  if(memory.known)memoryDismissed=memoryDismissal(memoryDismissed,memory.level,at);
  const view=memoryBannerView(memory,{dismissed:memoryDismissed}),was=banner.hidden;banner.hidden=!view.show;
  if(view.show){banner.dataset.level=view.level;$('memoryBannerMark').hidden=!view.mark;if($('memoryBannerTitle').textContent!==view.title)$('memoryBannerTitle').textContent=view.title;if($('memoryBannerText').textContent!==view.text)$('memoryBannerText').textContent=view.text;banner.title=`${view.title} · ${view.full}`;}
  const said=memoryAnnouncement(memoryAnnounced,memory,{at,shown:view.show});memoryAnnounced=said.announced;
  const announcer=$('memoryAnnounce');if(said.speak)announcer.textContent=view.announce;else if(!view.show&&announcer.textContent)announcer.textContent='';
  if(was!==banner.hidden&&snapshot&&!userCamera)fitMap();
}
// Focus goes to the This Mac chip (it keeps the memory word), or to the map when the chips are out of the layout.
function dismissMemoryBanner(){const memory=memoryNow();if(memory.alert)memoryDismissed=memoryHoldAt(memory.level,memoryClock());renderMemoryBanner();const chip=$('vitalMac');chip.focus({preventScroll:true});if(document.activeElement!==chip)$('map').focus({preventScroll:true});}
// The banner's Details opens the Mac hub with its Memory section open and focused.
function openMemoryDetails(){if(view!=='runtime')setView('runtime');if(!graph.nodes.some(n=>n.id==='runtime'))return;nodeSelect('runtime');const box=[...$('inspector').querySelectorAll('details')].find(d=>d.dataset.key==='runtime|memory');if(box){box.open=true;box.querySelector('summary')?.focus({preventScroll:true});
  // The drawer (only it) scrolls the section up under its sticky header.
  const drawer=$('drawer'),head=drawer.querySelector('.panel-head')?.getBoundingClientRect().height||0;drawer.scrollTop+=box.getBoundingClientRect().top-drawer.getBoundingClientRect().top-head-6;}}
function recordOnlineCodeAction(result,{submitted=false}={}){
  if(!result||typeof result!=='object'||!['idle','running','ready','needs-action','error'].includes(result.status)||!Number.isSafeInteger(result.operationId)||result.operationId<0||![null,'readiness','check-and-repair',...FIX_ACTIONS.keys(),...HEADLESS_ACTIONS.keys()].includes(result.action))throw new Error('Invalid Online Code Mode action status');
  const previous=onlineCodeActionStatus;
  if(!submitted&&previous?.rejected===true&&result.operationId<=previous.operationId)return;
  if(!submitted&&previous&&result.operationId<previous.operationId){
    onlineCodeActionStatus=['running','uncertain'].includes(previous.status)
      ?{status:'uncertain',message:'The monitor control restarted or returned an older operation while a check was pending. Refresh to inspect its current state; no second request was sent.',steps:[],operationId:previous.operationId,action:previous.action}
      :{status:'idle',message:'The monitor control restarted. Its earlier result is no longer available; no check was started.',steps:[],operationId:result.operationId,action:null};
    updateOnlineCodeMode();
    return;
  }
  if(!submitted&&previous?.status==='uncertain'&&result.status==='idle'&&result.operationId===previous.operationId)return;
  onlineCodeActionStatus={status:result.status,message:typeof result.message==='string'?result.message:'No detail was returned.',steps:Array.isArray(result.steps)?result.steps:[],operationId:result.operationId,action:result.action};
  updateOnlineCodeMode();
}
// The action status is read once when the first Mac snapshot arrives (a check may already be running), then
// only while Online Code or Fix inference is open or a check is running or uncertain: an idle page makes no GETs.
let onlineCodeActionChecked=false;
const onlineCodePollWanted=()=>!onlineCodeActionChecked||!$('onlineCodeDetails').hidden||!$('fixInferenceDetails').hidden||['running','uncertain'].includes(onlineCodeActionStatus?.status);
async function pollOnlineCodeAction(){
  if(snapshot?.host!=='mac')return;
  if(onlineCodeActionPolling||!onlineCodePollWanted())return;
  onlineCodeActionPolling=true;onlineCodeActionChecked=true;
  const revision=onlineCodeActionRevision,controller=new AbortController(),timer=setTimeout(()=>controller.abort(),3000);
  try{
    const response=await fetch('/api/online-code-mode/entry',{cache:'no-store',credentials:'same-origin',signal:controller.signal});
    if(!response.ok)throw new Error('Entry status unavailable');
    const result=await response.json();
    if(revision===onlineCodeActionRevision&&!onlineCodeActionPosting)recordOnlineCodeAction(result);
  }catch(_){
    if(revision===onlineCodeActionRevision&&!onlineCodeActionPosting&&onlineCodeActionStatus?.status==='running'){
      onlineCodeActionStatus={...onlineCodeActionStatus,status:'uncertain',message:'The local check may still be running. Waiting for its status; no second request was sent.'};updateOnlineCodeMode();
    }
  }finally{clearTimeout(timer);onlineCodeActionPolling=false;}
}
async function requestOnlineCodeAction(action){
  if(snapshot?.host!=='mac')return;
  if(!['readiness','check-and-repair',...FIX_ACTIONS.keys(),...HEADLESS_ACTIONS.keys()].includes(action)||onlineCodeActionPosting||['running','uncertain'].includes(onlineCodeActionStatus?.status))return;
  onlineCodeActionPosting=true;onlineCodeSubmittingAction=action;onlineCodeActionRevision++;updateOnlineCodeMode();
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),6000);
  try{
    const fixAction=FIX_ACTIONS.has(action),headless=HEADLESS_ACTIONS.has(action);
    const path=fixAction?'/api/inference/fix':headless?'/api/online-code-mode/headless':action==='readiness'?'/api/online-code-mode/entry':'/api/online-code-mode/repair';
    const body=fixAction?{scope:action.slice(4)}:{action};
    if(headless)body.action=HEADLESS_ACTIONS.get(action);
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),cache:'no-store',credentials:'same-origin',signal:controller.signal});
    const result=await response.json();
    if(!response.ok){
      onlineCodeActionStatus={status:'error',message:typeof result?.message==='string'?result.message.slice(0,240):'The local monitor rejected this request.',steps:[],operationId:onlineCodeActionStatus?.operationId||0,action,rejected:true};
      updateOnlineCodeMode();
      return;
    }
    recordOnlineCodeAction(result,{submitted:true});
  }catch(_){
    onlineCodeActionStatus={status:'uncertain',message:'The request may have reached the local monitor. Checking its status; no automatic retry will occur.',steps:[],operationId:onlineCodeActionStatus?.operationId||0,action};updateOnlineCodeMode();
  }finally{clearTimeout(timer);onlineCodeActionPosting=false;onlineCodeSubmittingAction=null;updateOnlineCodeMode();pollOnlineCodeAction();}
}
// The activity path uses liveNow(): a paused view is a still picture, so nothing in it is "running now".
function vitalsInput(){const worker=snapshot?.windowsWorker||null,age=Math.max(0,sampleAge()),heartbeatAge=Number.isFinite(worker?.ageSeconds)?worker.ageSeconds+age:null,pcFresh=fresh()&&heartbeatAge!==null&&heartbeatAge>=0&&heartbeatAge<=60&&['advertised','degraded','stopped'].includes(worker?.state),jobs=windowsJobsView(snapshot?.windowsJobs,{feedFresh:fresh(),snapshotAge:age}),lanes=windowsLaneView(worker,jobs,{feedFresh:pcFresh,snapshotAge:age}),feed=activityFeed(jobs,snapshot?.host==='mac'?runs():[],{limit:12,feedFresh:liveNow()});
  const rows=models(),hostLive=(snapshot?.sources||[]).some(s=>['lmstudio-api','lms-ps'].includes(s.id)&&s.state==='live'),nisi=Array.isArray(snapshot?.components)?snapshot.components.find(c=>c.id==='nisi'):null;
  const windowsHost=snapshot?.host==='windows';
  const vitals=vitalsView({fresh:fresh(),pcFresh,models:rows,activityKnown:fresh()&&snapshot?.activityKnown===true,loadedKnown:(hostLive||windowsHost&&(snapshot?.sources||[]).some(s=>s.id==='lane-inventory'&&s.state==='live'))&&rows.every(m=>typeof m.loaded==='boolean'),lanes,worker,jobs,pipeline:snapshot?.pipeline,nisi,feed,gpu:windowsHost?pcGpuView(worker,{feedFresh:pcFresh,snapshotAge:age}):macGpuView(snapshot?.macGpu,{feedFresh:fresh(),snapshotAge:age}),pcGpu:pcGpuView(worker,{feedFresh:pcFresh,snapshotAge:age}),memory:memoryView(snapshot?.memory,{feedFresh:fresh()}),activityLive:liveNow()});
  // Windows edition: the first chip is this PC (its lanes, GPU and memory); the second is the Mac as a LAN peer.
  if(windowsHost){const chip=macPeerView(snapshot?.macPeer,{feedFresh:fresh(),snapshotAge:age}).chip;vitals.pc={tone:chip.tone,detail:chip.detail,short:chip.short};if(vitals.tiny)vitals.tiny.pc=chip.short;if(vitals.micro)vitals.micro.pc=chip.short;}
  return{feed,vitals};}
// Status chips: id, view key, the label for title and aria, and the long and short visible labels.
const CHIPS=[['vitalMac','mac','This Mac','This Mac','Mac'],['vitalPc','pc','Windows PC','Windows PC','PC'],['vitalRoute','route','Nisi Inference route','Nisi Inference','Route'],['vitalActivity','activity','Latest activity','Activity','Latest']];
const CHIPS_WINDOWS=[['vitalMac','mac','This PC','This PC','PC'],['vitalPc','pc','Mac (LAN peer)','Mac','Mac'],['vitalRoute','route','Route queue','Route queue','Route'],['vitalActivity','activity','Latest activity','Activity','Latest']];
// Each chip has four forms: long (wide windows), short (narrow ones), tiny (the activity age dropped, the fewest
// words) and micro (only the Mac chip's memory word, when memory is tight or critical; otherwise the tiny form again);
// a chip that overflows steps down one form at a time, the largest overflow first.
const chipForms=c=>[[c.full,c.age,c.item.detail],[c.short,c.age,c.item.short||c.item.detail],[c.short,'',c.tiny||c.item.short||c.item.detail],[c.short,'',c.micro||c.tiny||c.item.short||c.item.detail]];
// The label keeps the activity age out of the uppercase run ("ACTIVITY · 32s", not "32S").
function setChip(b,label,age,text){const strong=b.querySelector('strong'),small=b.querySelector('small'),want=age?`${label} · ${age}`:label;if(strong.textContent!==want)strong.replaceChildren(...(age?[`${label} · `,el('span','vital-age',age)]:[label]));if(small.textContent!==text)small.textContent=text;}
const chipOverflow=b=>{const small=b.querySelector('small');return Math.max(0,(small.scrollWidth||0)-(small.clientWidth||0)-1);};
function renderVitals(){if(!snapshot)return;const {feed,vitals}=vitalsInput();
  const narrow=window.matchMedia?.('(max-width: 1000px)').matches===true;
  const age=Number.isFinite(vitals.lastAgeSeconds)?ageText(vitals.lastAgeSeconds).replace(' ago',''):'';
  $('vitalPc').hidden=!['mac','windows'].includes(snapshot.host);$('vitalPc').dataset.node=snapshot.host==='windows'?'mac-peer':'windows-worker';
  const chips=(snapshot.host==='windows'?CHIPS_WINDOWS:CHIPS).map(([id,key,label,full,short])=>({b:$(id),item:vitals[key],tiny:vitals.tiny?.[key],micro:vitals.micro?.[key],label,full,short,age:key==='activity'?age:'',form:narrow?1:0}));
  for(const c of chips){c.b.dataset.tone=c.item.tone;c.b.title=`${c.label}: ${c.item.detail}`;c.b.setAttribute('aria-label',`${c.label}: ${c.item.detail}`);setChip(c.b,...chipForms(c)[c.form]);}
  // Measured per chip: the chip with the largest overflow steps down one form, then the strip is measured again, so
  // one long chip no longer truncates all four and the rest keep their longer forms (the title keeps the long text).
  const stepDown=()=>{for(let steps=0;steps<12;steps++){const worst=chips.filter(c=>!c.b.hidden&&c.form<(c.last?2:3)).map(c=>[c,chipOverflow(c.b)]).filter(([,over])=>over>0).sort((a,b)=>b[1]-a[1])[0]?.[0];if(!worst)break;worst.form++;setChip(worst.b,...chipForms(worst)[worst.form]);}};
  stepDown();
  // The micro form stays only where it fits whole: a chip cut to "Me…" says less than its cut tiny form, which is also wider.
  for(const c of chips)if(c.form===3&&c.micro&&chipOverflow(c.b)>0){c.form=2;c.last=true;setChip(c.b,...chipForms(c)[2]);}
  if(chips.some(c=>c.last))stepDown();
  if(!$('activityDetails').hidden)renderActivity(feed);}
const ROUTE_WORDS={RESPONSE_VALIDATED:'Validated',REVIEW_FINDINGS:'Review findings',CHECKS_FAILED:'Checks failed',NEEDS_OWNER_REVIEW:'Needs owner review',PARTIAL:'Partial',queued:'Queued for a lane',unresolved:'Unresolved',unreadable:'Record unreadable',changing:'Changing; checked again next sample'};
const activityWho=row=>row.probe?'PC LLM switch probe':row.client?(CLIENTS.find(([id])=>id===row.client)?.[1]||row.client):'Unknown client';
const activityStatus=row=>row.state==='in-flight'?'Running now':row.state==='in-flight-stale'?'In flight at last sample':row.kind==='route'?(ROUTE_WORDS[row.state]||String(row.state).replaceAll('_',' ').toLowerCase()):row.state==='success'?'Answered':row.state==='cancelled'?'Cancelled':row.state==='invalid'||row.state==='invalid-result'?'Answer rejected (invalid)':row.state;
// Two lines under the title: what happened, how long and when; then both speeds ("reads 210 tok/s · writes
// 104 tok/s", else the generation rate).
const activityMeta=row=>[activityStatus(row),row.cancelRequested?'cancel requested':null,Number.isFinite(row.elapsed)?`${row.elapsed.toFixed(1)} s`:null,row.review==='block'?'review defect':row.review==='warn'?'review warning':null,ageText(row.ageSeconds)].filter(Boolean).join(' · ');
function renderActivitySummary(feed){const s=activitySummary(feed,{live:liveNow()}),box=$('activitySummary'),key=JSON.stringify(s);if(box.dataset.key===key)return;box.dataset.key=key;box.dataset.tone=s.tone;$('activityStatus').textContent=s.line;$('activityMeaning').textContent=s.meaning;fillTiles($('activityTiles'),s.tiles);}
// Keys shown by the open list; null right after opening, so only rows that arrive later are announced.
let activityKeys=null;
// Rows are rebuilt only when one enters, leaves or changes state; ages update in place, so keyboard
// focus, a press in progress and the scroll position survive the once-a-second refresh.
function renderActivity(feed){const list=$('activityList'),key=JSON.stringify(feed.map(r=>[r.key,r.state,Boolean(r.limitHit),Boolean(r.cancelRequested)]));renderActivitySummary(feed);
  if(list.dataset.key===key){const rows=new Map([...list.children].map(li=>[li.dataset.key,li]));for(const row of feed){const small=rows.get(row.key)?.querySelector('small'),text=activityMeta(row);if(small&&small.textContent!==text)small.textContent=text;}return;}
  // The panel section is the scroller (the list grows inside it), so its scroll position is the one kept.
  const scroller=$('activityDetails'),focused=document.activeElement,focusedKey=list.contains(focused)?focused?.closest('button')?.dataset.key:null,scroll=scroller.scrollTop;
  const added=activityKeys?feed.filter(row=>!activityKeys.has(row.key)):[];activityKeys=new Set(feed.map(row=>row.key));
  list.dataset.key=key;list.replaceChildren();
  if(!feed.length)list.append(el('li','activity-empty','Nothing recorded yet. Ask the PC from any agent (pc_ask) or run a task through online code mode.'));
  for(const row of feed){const li=el('li',['activity-row',row.state==='in-flight'?'in-flight':row.state==='in-flight-stale'?'in-flight-stale':/error|timeout|fail|reject|invalid/i.test(row.state)?'failed':row.state==='cancelled'?'cancelled':null,row.limitHit?'limit':null].filter(Boolean).join(' ')),button=el('button','activity-open'),dot=el('i','activity-dot'),body=el('span');button.type='button';li.dataset.key=row.key;button.dataset.key=row.key;
    // A token-limit hit gets the warning dot (orange, from the stylesheet); every other row keeps its client's colour.
    if(!row.limitHit&&!li.classList.contains('failed'))dot.style.background=Object.hasOwn(CLIENT_COLORS,row.client)?CLIENT_COLORS[row.client]:'#8a90a0';
    // The token-limit badge rides on the title line, so it stays visible without a third line in the narrow rail.
    const title=el('span','activity-title');title.append(el('strong',null,`${activityWho(row)} → ${row.target}`));if(row.limitHit)title.append(el('span','flag-badge','hit token limit'));
    body.append(title,el('small',null,activityMeta(row)));const speeds=row.speeds||row.rate;if(speeds)body.append(el('small','activity-speeds',speeds));
    button.append(dot,body);button.addEventListener('click',()=>openActivityRow(row));li.append(button);list.append(li);}
  if(focusedKey)([...list.querySelectorAll('button')].find(b=>b.dataset.key===focusedKey)||$('activityDetails')).focus({preventScroll:true});
  scroller.scrollTop=scroll;
  // The list itself is not a live region (it would re-read every row); only new rows are announced.
  if(added.length)$('activityAnnounce').textContent=`New activity: ${added.map(row=>`${activityWho(row)} → ${row.target}`).join('; ')}`;}
function openActivityRow(row){if(row.kind==='route'){const run=runs().find(r=>`run:${r.source||'route'}:${r.runId}`===row.key);if(!run)return;if(view!=='runs'){view='runs';document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view==='runs')));}selectRun(run);return;}
  if(view!=='runtime')setView('runtime');const id=row.lane&&graph.nodes.some(n=>n.id===`windows-lane:${row.lane}`)?`windows-lane:${row.lane}`:'windows-worker';if(graph.nodes.some(n=>n.id===id))nodeSelect(id);}
// A click on empty map (or Escape on the map) closes every panel and shows the whole map again.
function showWholeMap(){hideOnlineCodeDetails();hideFixInferenceDetails();hideActivityDetails();if(document.body.classList.contains('sidebar-open'))setSidebarOpen(false);$('aboutPanel').hidden=true;syncAboutButtons();hoveredNode=null;if(!$('drawer').hidden)closeDrawer(false);else{userCamera=false;renderGraph();fitMap();}}
// Two About controls open one panel: the footer link, and a map-controls button for when the footer is hidden.
function syncAboutButtons(){for(const id of ['about','aboutCompact'])$(id).setAttribute('aria-expanded',String(!$('aboutPanel').hidden));}
function toggleAbout(){$('aboutPanel').hidden=!$('aboutPanel').hidden;syncAboutButtons();}
function hideActivityDetails(){$('activityDetails').hidden=true;$('vitalActivity').setAttribute('aria-expanded','false');}
function toggleActivity(){const panel=$('activityDetails');if(!panel.hidden){hideActivityDetails();if(!userCamera)fitMap();return;}
  if(compactEvidence()){hideOnlineCodeDetails();hideFixInferenceDetails();if(!$('drawer').hidden)closeDrawer(false);}
  panel.hidden=false;lastEvidencePanel='activityDetails';$('vitalActivity').setAttribute('aria-expanded','true');$('activityList').dataset.key='';$('activitySummary').dataset.key='';activityKeys=null;renderActivity(vitalsInput().feed);panel.focus({preventScroll:true});if(selected)revealSelected();else if(!userCamera)fitMap();}
// Compact mode is one bottom sheet: the inspector wins, otherwise the most recently opened panel stays.
let lastEvidencePanel=null;
function keepOneCompactPanel(){const panels=[['onlineCodeDetails',hideOnlineCodeDetails],['fixInferenceDetails',hideFixInferenceDetails],['activityDetails',hideActivityDetails]],open=panels.filter(([id])=>!$(id).hidden).map(([id])=>id);
  const keep=!$('drawer').hidden?null:open.includes(lastEvidencePanel)?lastEvidencePanel:open[0];for(const [id,hide] of panels)if(id!==keep&&!$(id).hidden)hide();}
function setLegend(open,remember=true){$('mapLegend').hidden=!open;$('legendToggle').setAttribute('aria-expanded',String(open));$('legendToggle').title=open?'Hide the map key':'Show the map key';if(remember){try{localStorage.setItem('inference-monitor.legend',open?'1':'0');}catch(_){}}}
function updateFreshness(){renderVitals();renderMemoryBanner();$('connection').textContent=paused?'VIEW PAUSED':clockMismatch()?'CLOCK MISMATCH':fresh()?(feedStream?'LIVE · STREAM':'LIVE · 1s'):snapshot?'STALE FEED':'CONNECTING';$('connection').className='connection '+(!paused&&fresh()?'live':'');document.body.classList.toggle('paused',paused);document.body.classList.toggle('disconnected',!fresh());syncWebPulse();$('sampleAge').textContent=snapshot?ageText(feedAge(snapshot,Date.now()/1000)):'—';updateOnlineCodeMode();if(!fresh()&&!paused&&snapshot){renderSummary();makeGraph();renderGraph();renderList();renderInspector();}}
function dockedSidebar(){return window.matchMedia?.('(max-width: 979px)').matches===false;}
function setSidebarOpen(open,focusSearch=false){const sidebar=$('runtimeSidebar'),toggle=$('sidebarToggle'),docked=dockedSidebar();if(!open&&!docked&&sidebar.contains(document.activeElement))toggle.focus({preventScroll:true});document.body.classList.toggle('sidebar-open',open);sidebar.inert=!open&&!docked;toggle.setAttribute('aria-expanded',String(open));toggle.setAttribute('aria-label',open?'Close runtime and client browser':'Open runtime and client browser');toggle.lastElementChild.textContent=open?'Close':'Browse';if(open&&focusSearch)$('search').focus({preventScroll:true});}
function syncCompactUI(){const compact=compactEvidence();document.body.classList.toggle('compact-ui',compact);if(compact)keepOneCompactPanel();const docked=dockedSidebar();document.body.classList.toggle('portfolio-docked',docked);$('sidebarToggle').hidden=docked;setSidebarOpen(docked?false:document.body.classList.contains('sidebar-open'));const region=$('graphRegion'),wanted=region.clientWidth<600||region.clientHeight>region.clientWidth*1.2?'portrait':'wide';if(snapshot&&view==='runtime'&&layoutMode!==wanted){userCamera=false;render();}else if(snapshot&&!userCamera)fitMap();}
function setView(value){view=value;selected=null;runId=null;userCamera=false;$('drawer').hidden=true;document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view===view)));render();}
function setModelScope(value){if(view!=='runtime'||!['core','all'].includes(value)||value===modelScope)return;modelScope=value;userCamera=false;if(selected?.startsWith('model:')&&!displayedModels().visible.some(m=>`model:${m.host}:${m.id}`===selected)){selected=null;$('drawer').hidden=true;$('modelActionDock').hidden=true;}render();}
function render(){renderModelScope();if(!snapshot)return;renderSummary();makeGraph();renderGraph();renderList();renderInspector();if(!userCamera)fitMap();updateFreshness();}
// Live feed: one Server-Sent Events stream (/api/stream, named event "snapshot") pushes each snapshot as it is
// published. While the stream is down the page polls /api/snapshot once a second and retries the stream after
// 5 s, doubling to 30 s. The two never run together; bursts of stream messages coalesce into one render per frame.
const validSnapshot=data=>Boolean(data)&&data.schemaVersion===1&&Array.isArray(data.models)&&Array.isArray(data.sources)&&Number.isFinite(data.sampledAt);
const streamBackoff=attempt=>Math.min(30000,5000*2**Math.min(3,Math.max(0,Math.floor(Number(attempt))||0)));
let feedStream=null,streamAttempt=0,streamRetry=null,pollGeneration=0,pollTimer=null,pollActive=false,renderQueued=false,freshnessQueued=false;
function scheduleRender(){if(renderQueued)return;renderQueued=true;(window.requestAnimationFrame||(frame=>setTimeout(frame,16)))(()=>{renderQueued=false;render();});}
// Paused and invalid messages only refresh freshness, also at most once per frame (a queued render does it too).
function scheduleFreshness(){if(freshnessQueued||renderQueued)return;freshnessQueued=true;(window.requestAnimationFrame||(frame=>setTimeout(frame,16)))(()=>{freshnessQueued=false;updateFreshness();});}
function openStream(){clearTimeout(streamRetry);streamRetry=null;if(typeof EventSource!=='function'){startPolling();return;}
  stopPolling();let source;try{source=new EventSource('/api/stream');}catch(_){streamFailed();return;}feedStream=source;
  source.addEventListener('snapshot',event=>{if(feedStream!==source)return;let data=null;try{data=JSON.parse(event.data);}catch(_){}
    if(!validSnapshot(data)){connected=false;scheduleFreshness();return;}connected=true;streamAttempt=0;if(!paused){snapshot=data;scheduleRender();}else scheduleFreshness();});
  // A dropped stream, or the server's 503 once three streams are open, falls back to polling.
  source.addEventListener('error',()=>{if(feedStream===source)streamFailed();});}
function streamFailed(){const source=feedStream;feedStream=null;source?.close();startPolling();clearTimeout(streamRetry);streamRetry=setTimeout(openStream,streamBackoff(streamAttempt++));}
function startPolling(){if(pollActive)return;pollActive=true;poll(++pollGeneration);}
function stopPolling(){pollActive=false;pollGeneration++;clearTimeout(pollTimer);pollTimer=null;}
async function poll(generation=pollGeneration){const started=performance.now(),c=new AbortController(),timer=setTimeout(()=>c.abort(),1800);try{const r=await fetch('/api/snapshot',{cache:'no-store',signal:c.signal});if(!r.ok)throw new Error('Unavailable');const data=await r.json();if(generation!==pollGeneration)return;if(!validSnapshot(data))throw new Error('Invalid snapshot');connected=true;if(!paused){snapshot=data;render();}}catch(_){if(generation===pollGeneration)connected=false;}finally{clearTimeout(timer);if(generation===pollGeneration&&pollActive){updateFreshness();pollTimer=setTimeout(()=>poll(generation),Math.max(100,1000-(performance.now()-started)));}}}
$('pause').addEventListener('click',()=>{paused=!paused;$('pause').textContent=paused?'Resume':'Pause';$('pause').setAttribute('aria-pressed',String(paused));if(snapshot)render();else updateFreshness();});$('expand').addEventListener('click',()=>native('expand'));$('quit').addEventListener('click',()=>native('quit'));if(!window.webkit?.messageHandlers?.monitor)$('quit').hidden=true;
$('sidebarToggle').addEventListener('click',()=>{const opening=!document.body.classList.contains('sidebar-open');setSidebarOpen(opening,true);if(opening)pollAutoUnload(true);});window.addEventListener('resize',syncCompactUI);syncCompactUI();
$('onlineCodeMode').addEventListener('click',()=>{
  const panel=$('onlineCodeDetails');
  hideFixInferenceDetails();
  if(compactEvidence())hideActivityDetails();
  if(compactEvidence()&&!$('drawer').hidden)closeDrawer(false);
  panel.hidden=false;lastEvidencePanel='onlineCodeDetails';
  $('onlineCodeMode').setAttribute('aria-expanded','true');
  panel.focus({preventScroll:true});
  if(selected)revealSelected();else if(!userCamera)fitMap();
  requestOnlineCodeAction('check-and-repair');
});
$('onlineCodeRepair').addEventListener('click',()=>requestOnlineCodeAction('check-and-repair'));
$('pcHeadless').addEventListener('click',()=>requestOnlineCodeAction(headlessAction()));
$('pcSwitch').addEventListener('click',()=>{if(!$('pcSwitch').disabled)requestOnlineCodeAction(headlessAction());});
$('fixInference').addEventListener('click',()=>{
  openFixInference();
  requestOnlineCodeAction('fix-all');
});
$('fixInferenceForm').addEventListener('submit',event=>{
  event.preventDefault();
  requestOnlineCodeAction('fix-all');
});
$('fixInferenceDetails').addEventListener('keydown',event=>{
  if(event.key==='Escape'){
    event.preventDefault();
    closeFixInference();
  }
});
$('fixInferenceClose').addEventListener('click',closeFixInference);
$('onlineCodeDetails').addEventListener('keydown',event=>{
  if(event.key==='Escape'){
    event.preventDefault();
    hideOnlineCodeDetails();
    $('onlineCodeMode').focus({preventScroll:true});
  }
});
$('onlineCodeClose').addEventListener('click',()=>{hideOnlineCodeDetails();$('onlineCodeMode').focus({preventScroll:true});});
document.querySelectorAll('.vital[data-node]').forEach(b=>b.addEventListener('click',()=>{if(view!=='runtime')setView('runtime');const id=b.dataset.node;if(graph.nodes.some(n=>n.id===id))nodeSelect(id);}));
$('vitalActivity').addEventListener('click',toggleActivity);
$('activityClose').addEventListener('click',()=>{hideActivityDetails();$('vitalActivity').focus({preventScroll:true});if(!userCamera)fitMap();});
$('activityDetails').addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();hideActivityDetails();$('vitalActivity').focus({preventScroll:true});}});
// Escape inside the node details closes them and returns focus to the node; inside Browse it closes the sidebar.
$('drawer').addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();closeDrawer();}});
$('runtimeSidebar').addEventListener('keydown',event=>{if(event.key==='Escape'&&document.body.classList.contains('sidebar-open')){event.preventDefault();setSidebarOpen(false);}});
$('drawerJumpButton').addEventListener('click',()=>setView('runs'));
$('drawerFixButton').addEventListener('click',()=>openFixInference('nisi'));
$('memoryBannerDismiss').addEventListener('click',dismissMemoryBanner);$('memoryBannerDetails').addEventListener('click',openMemoryDetails);
$('legendToggle').addEventListener('click',()=>setLegend($('mapLegend').hidden));
{let legend=null;try{legend=localStorage.getItem('inference-monitor.legend');}catch(_){}setLegend(legend==='1',false);}
$('modelAction').addEventListener('click',requestModelAction);setInterval(()=>{if(modelControlPollWanted())pollModelControl();},1000);
$('autoUnloadToggle').addEventListener('click',requestAutoUnload);setInterval(()=>{if(document.body.classList.contains('sidebar-open')||dockedSidebar())pollAutoUnload();},5000);
$('closeDrawer').addEventListener('click',()=>closeDrawer());$('about').addEventListener('click',toggleAbout);$('aboutCompact').addEventListener('click',toggleAbout);document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
document.querySelectorAll('[data-model-scope]').forEach(b=>b.addEventListener('click',()=>setModelScope(b.dataset.modelScope)));
$('search').addEventListener('input',e=>{query=e.target.value.toLowerCase();renderList();renderGraph();});$('zoomIn').addEventListener('click',()=>zoom(1.2));$('zoomOut').addEventListener('click',()=>zoom(1/1.2));$('fit').addEventListener('click',()=>{userCamera=false;fitMap();});
$('map').addEventListener('wheel',e=>{e.preventDefault();const b=$('map').getBoundingClientRect();zoom(Math.exp(-e.deltaY*.002),e.clientX-b.left,e.clientY-b.top);},{passive:false});
$('map').addEventListener('pointerdown',e=>{if(e.button!==0)return;drag={x:e.clientX,y:e.clientY,cx:camera.x,cy:camera.y,moved:false};justDragged=false;});
$('map').addEventListener('pointermove',e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.hypot(dx,dy)>4)drag.moved=true;if(!drag.moved)return;camera.x=drag.cx+dx;camera.y=drag.cy+dy;userCamera=true;boundCamera();$('map').classList.add('dragging');applyCamera();});
window.addEventListener('pointerup',()=>{justDragged=!!drag?.moved;drag=null;$('map').classList.remove('dragging');});window.addEventListener('pointercancel',()=>{drag=null;justDragged=false;$('map').classList.remove('dragging');});$('map').addEventListener('dblclick',()=>{userCamera=false;fitMap();});$('map').addEventListener('click',e=>{if(justDragged||e.target.closest?.('.node'))return;showWholeMap();});$('map').addEventListener('keydown',e=>{if(e.target!==$('map'))return;if(e.key==='+'||e.key==='=')zoom(1.2);else if(e.key==='-')zoom(1/1.2);else if(e.key==='0'){userCamera=false;fitMap();}else if(e.key.startsWith('Arrow')){e.preventDefault();const step=48,d={ArrowLeft:[step,0],ArrowRight:[-step,0],ArrowUp:[0,step],ArrowDown:[0,-step]}[e.key];if(d){camera.x+=d[0];camera.y+=d[1];userCamera=true;boundCamera();applyCamera();}}else if(e.key==='Home'){e.preventDefault();focusNode(graph.nodes.find(n=>n.hub||n.root)?.id);}else if(e.key==='Escape'){e.preventDefault();showWholeMap();}});
new ResizeObserver(()=>{if(!userCamera)fitMap();else if(!$('drawer').hidden)revealSelected();}).observe($('graphRegion'));setInterval(updateFreshness,1000);setInterval(pollOnlineCodeAction,1000);pollOnlineCodeAction();openStream();
