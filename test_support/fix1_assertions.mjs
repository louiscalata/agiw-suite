export function checkFix1({packet, api, $, input, check}) {
  const {snapshot,status}=packet,{dimension,expected:e}=snapshot.fixtureScenario;
  if(dimension==='hands-off-signals'){
    check(snapshot.handsOff.active===e.active,'independent hands-off signal',snapshot.handsOff);
    check(status.handsOff.active===e.active,'published hands-off signal',status.handsOff);
    if(e.active===true && snapshot.fixtureScenario.inputs.owners!=='live' && snapshot.fixtureScenario.inputs.notification!==3)check(snapshot.handsOff.agiwOnly==null,'unknown attribution',snapshot.handsOff);
  }
  if(dimension==='route-inspector-clock'){
    api.select('pipeline');api.render();
    const text=$('inspector').textContent,graph=api.nodeSubtitle('pipeline');
    check(e.current ? text.includes(e.state==='running'?'Route running':'Route queued') : !/Route running|Route queued|Routed tasks \(|RouteRunning|RouteQueued/.test(text),'inspector observation clock',text);
    check(/ROUTE RUNNING|ROUTE QUEUED/.test(graph)===e.current,'graph observation clock',graph);
  }
  if(dimension==='activity-ui-cap'){
    const row=input.feed.find(r=>r.kind==='route'&&r.runId==='current-route');
    check(Boolean(row)&&row.state===(e.state==='running'?'in-flight':'queued'),'active route survives UI cap',input.feed);
    check(input.feed.truncated===e.truncated,'UI cap provenance',input.feed);
  }
  if(dimension==='history-helper-read'){
    const bound=snapshot.activity.historyBound;
    check(bound.truncated===e.truncated&&bound.missingFromAgeSeconds===e.floor,'failed-read history floor',bound);
  }
  if(dimension==='lan-observation-clock'){
    const mac=snapshot.macPeer,source=snapshot.sources.find(s=>s.id==='mac-peer');
    check((mac.state==='linked')===e.linked,'share clock independent of LAN',mac);
    check((mac.lanFailure==='refused')===e.current&&mac.lanFailureAgeSeconds===e.age || !e.current&&mac.lanFailure==null&&mac.lanFailureAgeSeconds==null,'LAN failure clock',mac);
    check(source.state===(e.current?'error':'unavailable'),'LAN source clock',source);
    check(mac.verified===false&&mac.address===null&&mac.latencyMs===null,'share never verifies LAN',mac);
    const limitation=e.current?'mac LM Studio LAN check failed (refused); cause unknown':'mac LM Studio LAN reachability unknown';
    check(status.limitations.includes(limitation),'published LAN clock',status.limitations);
  }
}
