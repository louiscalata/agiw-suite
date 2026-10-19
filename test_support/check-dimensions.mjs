import {layout,onlineMode} from './production.mjs';
const pairs=body=>Object.fromEntries(body.querySelectorAll('dt').map(dt=>[dt.textContent,dt.parentNode.children[dt.parentNode.children.indexOf(dt)+1].textContent]));
export function checkDimensions(item,api,$,fail){
  const d=item.snapshot,e=item.expect||{};
  if('idleScan' in e && (d.pipeline.status==='idle')!==e.idleScan)fail(item,'idle scan observation clock',d.pipeline);
  if('primaryState' in e){
    if(d.pipeline.status!==e.primaryState)fail(item,'verified primary survives cap',d.pipeline);
    if(e.primaryCurrent && (d.onlineCodeMode.active!==true || d.pipeline.pipelinesComplete!==e.primaryComplete))fail(item,'primary activity and bounded total',{pipeline:d.pipeline,mode:d.onlineCodeMode});
    const text=api.inspect({kind:'pipeline'}).textContent;
    if(e.primaryCurrent && !(e.primaryState==='running'?/Route running|routes running/:/Route queued/).test(text))fail(item,'primary inspector',text);
    if(!e.primaryCurrent && /Route running|Route queued|Routed tasks \(/.test(text))fail(item,'expired primary inspector',text);
  }
  if('waitersComplete' in e){
    const src=d.sources.find(s=>s.id==='windows-jobs');
    const evidence=layout.windowsJobsEvidence(d.windowsJobs,src,{feedFresh:true});
    if(evidence.inFlightComplete!==e.waitersComplete)fail(item,'explicit waiter coverage',evidence);
    const text=$('activityTiles').textContent;
    if(!e.waitersComplete && /In flight0/.test(text))fail(item,'missing waiter never zero',text);
    if(e.waitersComplete && !text.includes(`In flight${item.input.count}`))fail(item,'exact waiter count',text);
  }
  if(e.coverageIncomplete && /Nothing running now|In flight0|Routes0/.test($('activityStatus').textContent+$('activityTiles').textContent))fail(item,'partial route coverage never exact absence',$('activityTiles').textContent);
  if('lastCurrent' in e){
    const view=layout.windowsJobsView(d.windowsJobs,{feedFresh:true});
    if(view.current!==e.lastCurrent)fail(item,'jobs.successWindow',{current:view.current,expected:e.lastCurrent,age:view.lastVerified?.ageSeconds});
  }
  if('coreDiscovered' in e){
    const roster=layout.runtimeModelRoster(api.modelRows());
    for(const [field,actual,want]of [['coreDiscovered',roster.coreDiscovered,e.coreDiscovered],['runtimeStars',roster.visible.length,e.runtimeStars],['operationalExtras',roster.operationalExtras.length,0]])
      if(actual!==want)fail(item,field,{actual,want});
  }
  if(e.activity){
    const {count,limit,state}=e.activity;
    const runs=Array.from({length:count},(_,i)=>({runId:`route-${i}`,host:'mac',ageSeconds:i,activity:state,status:state}));
    const feed=layout.activityFeed(null,runs,{limit,feedFresh:false});
    const summary=layout.activitySummary(feed,{live:false});
    if(count>limit&&state==='running'&&!/at least/i.test(summary.line))fail(item,'activity.staleHeadline',summary.line);
  }
  if('jobKnown' in e || e.partialLane){
    api.scopeProjection();
    if(e.currentJobs===false){
      const graph=api.graphState();
      const pc=graph.nodes.find(n=>n.kind==='windows-worker');
      const liveLanes=graph.nodes.filter(n=>n.kind==='windows-lane'&&n.lane.live);
      if(pc?.inFlight||liveLanes.length)fail(item,'graph.staleJobActivity',{worker:pc?.inFlight,lanes:liveLanes.map(n=>n.lane.id)});
      const activity=api.vitalsInput().feed;
      if(activity.some(row=>row.kind==='pc-job'&&row.state==='in-flight'))fail(item,'activity.staleJobActivity',activity);
    }
    const jobs=layout.windowsJobsView(d.windowsJobs,{feedFresh:true}),lanes=layout.windowsLaneView(d.windowsWorker,jobs,{feedFresh:true});
    const worker=api.inspect({kind:'windows-worker',id:'pc',windowsWorker:d.windowsWorker,windowsJobs:jobs,windowsLanes:lanes,workerAdvertised:true});
    const evidence=pairs(worker);
    if(e.currentUnknown){
      const status=d.sources.find(s=>s.id==='windows-jobs');
      if(/no job in flight/.test(status?.detail||''))fail(item,'jobs.unsettledSource',status.detail);
      for(const field of ['Current job','Jobs in flight'])
        if(/no job|^0$/.test((evidence[field]||'').toLowerCase()))fail(item,'worker.unsettledWithoutClient',evidence[field]);
    }
    if(e.noAbsence){
      for(const field of ['Current job','Last validated answer','Last validated inference','Jobs in flight']){
        if(/no job|none journaled|^0$/.test((evidence[field]||'').toLowerCase()))fail(item,'worker.'+field,evidence[field]);
      }
      if(/^\d+$/.test(evidence['Jobs in flight']||''))fail(item,'worker.exactPartialCount',evidence['Jobs in flight']);
    }
    for(const lane of lanes.lanes){
      const body=api.inspect({kind:'windows-lane',lane,headless:lanes.headless});
      if(e.currentUnknown&&/None in flight/.test(body.textContent))fail(item,'lane.unsettledWithoutClient',body.textContent);
      if(e.noAbsence&&/None in flight|No successful job journaled/.test(body.textContent))fail(item,'lane.absence',body.textContent);
      if(e.partialLane===lane.id&&lane.busy===null&&/idle|0 of/i.test(body.textContent))fail(item,'lane.unknownSlots',body.textContent);
    }
    if(e.jobKnown){
      const expected=d.windowsJobs.inFlightTruncated?`at least ${d.windowsJobs.inFlight.length}`:d.windowsJobs.inFlight.length;
      if(evidence['Jobs in flight']!==String(expected))fail(item,'worker.knownCount',{actual:evidence['Jobs in flight'],want:String(expected)});
    }
  }
}
