import {layout,onlineMode} from './production.mjs';
const {onlineCodeModeView}=onlineMode;
const pairs=body=>Object.fromEntries(body.querySelectorAll('dt').map(dt=>[dt.textContent,dt.parentNode.children[dt.parentNode.children.indexOf(dt)+1].textContent]));
const tiles=body=>Object.fromEntries(body.querySelectorAll('.tile').map(t=>[t.querySelector('.tile-label')?.textContent,t.querySelector('.tile-value')?.textContent]));
const doubtful=/unknown|unavailable|at least|stale|not current|incomplete|—/i;
export function checkTwoSided(item,api,$,fail){
  const d=item.snapshot,e=item.expect||{};
  const eq=(field,actual,want)=>{if(actual!==want)fail(item,field,{actual,want});};
  const exact=(field,value)=>{if(value==null||doubtful.test(String(value)))fail(item,field,value);};
  const age=Math.max(0,1800000000+(item.clockOffset||0)-d.sampledAt);
  const evidence=layout.windowsJobsEvidence(d.windowsJobs,d.sources.find(s=>s.id==='windows-jobs'),{feedFresh:api.liveNow(),snapshotAge:age});
  const jobs=layout.windowsJobsView(d.windowsJobs,{feedFresh:evidence.current,snapshotAge:age});
  const lanes=layout.windowsLaneView(d.windowsWorker,jobs,{feedFresh:api.liveNow(),snapshotAge:age});
  const worker=api.inspect({kind:'windows-worker',id:'pc',windowsWorker:d.windowsWorker,windowsJobs:jobs,windowsLanes:lanes,workerAdvertised:true});
  const w=pairs(worker);
  if(e.exact){
    const x=e.exact;
    for(const [id,want] of [['loadedCount',x.loaded],['activeCount',x.active],['queuedCount',x.queued]])eq('exact.summary.'+id,$(id).textContent,String(want));
    if(!$('modelScopeNote').textContent.startsWith(`${x.discovered} of 6 primary models discovered`))fail(item,'exact.catalog',$('modelScopeNote').textContent);
    const runtime=api.inspect({kind:'runtime'}),rt=tiles(runtime);
    eq('exact.runtime.Loaded',rt.Loaded,String(x.loaded));
    const callers=layout.localCallersView(d.localCallers,{feedFresh:api.liveNow()});
    eq('exact.callers',callers.count,x.callers);
    for(const model of api.modelRows()){
      if(model.representedByInstances)continue;
      const body=api.inspect({kind:'model',model}),p=pairs(body);
      eq('exact.model.residency',model.loaded,true);
      exact('exact.model.state',model.state);
      if(doubtful.test(body.querySelector('.panel-summary')?.textContent||''))fail(item,'exact.model.headline',body.textContent);
      if('Loaded instance count' in p)exact('exact.model.instances',p['Loaded instance count']);
    }
    const gpu=layout.macGpuView(d.macGpu,{feedFresh:api.liveNow(),snapshotAge:age});
    eq('exact.gpu.percent',gpu.percent,40);
    for(const [field,value]of gpu.rows)exact('exact.gpu.'+field,value);
    const memory=layout.memoryView(d.memory,{feedFresh:api.liveNow(),host:'mac'});
    eq('exact.memory.known',memory.known,true); eq('exact.memory.available',memory.available,50);
    eq('exact.memory.tile',memory.tile,'OK · 50% available · no swap');
    for(const [field,value]of memory.rows)exact('exact.memory.'+field,value);
    const pcGpu=layout.pcGpuView(d.windowsWorker,{feedFresh:api.liveNow(),snapshotAge:age});
    eq('exact.pcGpu.tile',pcGpu.tile,'40% · 1.0/24.0 GB · 55 °C');eq('exact.worker.version',pcGpu.version,'1.2.3');
    eq('exact.worker.jobCount',w['Jobs in flight'],String(x.inFlight));
    eq('exact.jobs.currentSuccess',jobs.current,x.lastCurrent);
    if(x.lastCurrent){exact('exact.worker.answer',w['Last validated answer']);exact('exact.worker.inference',w['Last validated inference']);}
    for(const [field,value]of layout.windowsLaneRows(lanes))exact('exact.'+field,value);
    api.scopeProjection();
    for(const lane of lanes.lanes){
      const t=tiles(api.inspect({kind:'windows-lane',lane,headless:lanes.headless}));
      eq('exact.lane.'+lane.id+'.slots',t.Slots,`${x.slots[lane.id]} of ${lane.total} busy`);
      if(x.lastCurrent){exact('exact.lane.'+lane.id+'.answer',t['Last answer']);exact('exact.lane.'+lane.id+'.speed',t['Last measured speed']);}
    }
    for(const [label,value]of layout.agiwPeersRows(d.peers,{feedFresh:api.liveNow(),snapshotAge:age,host:'windows',source:d.sources.find(s=>s.id==='agiw-peers')}))exact('exact.peer.'+label,value);
    const v=api.vitalsInput().vitals;
    for(const field of ['mac','pc','route'])exact('exact.vital.'+field,v[field].detail);
    const mode=onlineCodeModeView(d.onlineCodeMode,{feedFresh:api.liveNow()});
    eq('exact.route.idle',mode.taskLabel,'Idle; no active route');exact('exact.route.setup',mode.setupLabel);
    for(const [field,want]of Object.entries({'In flight':String(x.inFlight),Answered:String(x.answered),Failed:'0',Routes:'0'}))eq('exact.activity.'+field,tiles($('activityTiles'))[field],want);
    exact('exact.activity.headline',$('activityStatus').textContent);
  }
  if(e.activityExact){
    for(const [field,want]of Object.entries(e.activityExact))eq('journal.activity.'+field,tiles($('activityTiles'))[field],want);
    exact('journal.activity.headline',$('activityStatus').textContent);
  }
  if(e.catalogUnknown&&!doubtful.test($('modelScopeNote').textContent))fail(item,'catalog.loadedOnly',$('modelScopeNote').textContent);
  if('peerEmptyKnown' in e){
    const rows=layout.agiwPeersRows(d.peers,{feedFresh:true,source:d.sources.find(s=>s.id==='agiw-peers'),host:'windows'});
    const body=api.inspect({kind:'mac-peer'}).textContent;
    if(e.peerEmptyKnown){eq('peers.completeEmpty',rows[0][1],'None has published on SharedChami yet');}
    else for(const [field,value]of [['projection',rows[0][1]],['inspector',body]])if(/None has published/.test(value))fail(item,'peers.'+field,value);
  }
  if(e.emptyUnknown&&/No activity in the observed history|Nothing recorded yet|Nothing running now/.test($('activityList').textContent))fail(item,'activity.cachedAbsence',$('activityList').textContent);
  if(e.routeCounts){
    const c=e.routeCounts,mode=onlineCodeModeView(d.onlineCodeMode,{feedFresh:true});
    const s=api.onlineCodeSummary(mode,{macOwner:true,actionState:'idle',isFresh:true});
    api.updateOnlineCodeMode();
    const surfaces=[mode.label,mode.taskLabel,s.meaning,$('onlineCodeLabel').textContent,tiles($('onlineCodeTiles')).Route];
    const text=[...surfaces,s.line].join(' ');
    eq('route.queuedOnly',mode.queuedOnly,!c.truncated&&c.running===0);
    if(c.truncated){
      if(/(?:at least )?0 running|nothing is running|\b0 queued\b/.test(text))fail(item,'route.falseAbsence',text);
      for(const [i,value]of surfaces.entries())if(!/at least/.test(value)||/\bnull\b/.test(value))fail(item,'route.partialCount.'+i,value);
    }else{
      exact('route.complete',text);
      if(c.running&&/nothing is running/.test(text))fail(item,'route.runningIgnored',text);
    }
  }
  if(e.ageBoundary){
    const b=e.ageBoundary;
    if(b.source==='worker')eq('age.worker.visible',lanes.visible,b.known);
    else if(b.source==='memory')eq('age.memory.known',layout.memoryView(d.memory,{feedFresh:api.liveNow(),snapshotAge:age}).known,b.known);
    else if(b.source==='gpu')eq('age.gpu.known',layout.macGpuView(d.macGpu,{feedFresh:api.liveNow(),snapshotAge:age}).known,b.known);
    else if(b.source==='journal')eq('age.journal.current',evidence.current,b.known);
    else if(b.source==='callers'){
      eq('age.callers.known',layout.localCallersView(d.localCallers,{feedFresh:api.liveNow(),snapshotAge:age,ageSeconds:d.sources.find(s=>s.id==='local-callers').ageSeconds}).checked,b.known);
      const shown=pairs(api.inspect({kind:'runtime'}))['Local callers'];
      if(!b.known&&!/unknown/i.test(shown))fail(item,'age.callers.inspector',shown);
      if(b.known)exact('age.callers.inspector',shown);
    }
    else if(b.source==='model')eq('age.model.known',api.modelRows().every(m=>m.loaded!==null),b.known);
    else{
      const rows=layout.agiwPeersRows(d.peers,{feedFresh:api.liveNow(),snapshotAge:age,host:'windows'});
      eq('age.peer.current',/^Reported/.test(rows[0][1]),b.known);
    }
  }
  if(e.frozen){
    api.refreshEvidence();api.renderActivity(api.vitalsInput().feed);
    const capture=new Date(d.sampledAt*1000).toISOString();
    const surfaces={summary:$('subline').textContent,clock:$('sampleAge').textContent,
      activity:$('activityStatus').textContent+' '+$('activityMeaning').textContent+' '+$('activityList').textContent,
      online:['onlineCodeStatus','onlineCodeMeaning','onlineCodeTiles','onlineCodeRows'].map(id=>$(id).textContent).join(' '),worker:worker.textContent};
    api.scopeProjection();
    for(const node of api.graphState().nodes.filter(n=>['windows-lane','model','pipeline','runtime'].includes(n.kind)))surfaces[node.id]=api.inspect(node).textContent;
    if(!Object.values(surfaces).some(text=>text.includes(capture)))fail(item,'frozen.captureTime',surfaces.clock+' '+surfaces.summary);
    for(const [field,text]of Object.entries(surfaces))if(/\bnow\b/i.test(text))fail(item,'frozen.claimsNow.'+field,text);
  }
}
