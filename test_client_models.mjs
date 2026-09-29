import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import {fitGraph,constellationLayout,focusedLaneLayout,prioritizeRuntimeModels,runtimeModelRoster,CORE_MODEL_IDS,hasAdvertisedWindowsWorker,afmView,jevView,modelStarSize,windowsJobsView,windowsLaneView} from './web/map-layout.mjs';

const layoutContext={fitGraph,constellationLayout,focusedLaneLayout,prioritizeRuntimeModels,runtimeModelRoster,CORE_MODEL_IDS,hasAdvertisedWindowsWorker,afmView,jevView,modelStarSize,windowsJobsView,windowsLaneView};

// Exercise the dashboard's actual projection and graph code without starting
// its polling loop or requiring a browser DOM.
const source=readFileSync(new URL('./web/app.js',import.meta.url),'utf8')
  .replace(/^import .*;\n/gm,'').split("$('pause').addEventListener")[0];
function project(clients,host='mac',width=820,height=660){
  const script=`${source}\nsnapshot={host,sampledAt:Date.now()/1000,models:[],clients,pipeline:{status:'idle'}};connected=true;runtimeGraph();({rows:clientRows(),ages:clientRows().map(clientAge),counts:clientIdentityCounts(),nodes:graph.nodes,edges:graph.edges})`;
  const context={...layoutContext,clients,host,Date,Set,Map,Math,Number,String,Array,Object,window:{innerWidth:width},document:{getElementById:id=>id==='graphRegion'?{clientWidth:width-230,clientHeight:height-114}:null}};
  return JSON.parse(JSON.stringify(runInNewContext(script,context)));
}

test('five client branches distinguish recorded model IDs from unsupported identities',()=>{
  const result=project([
    {id:'codex',label:'Codex',model:'gpt-6-sol',modelState:'observed',models:[{id:'gpt-6-sol',modelState:'observed',source:'session'},{id:'gpt-6-luna',modelState:'configured',source:'settings'}]},
    {id:'claude',label:'Claude',model:'Fable 5.1',modelState:'observed',models:[{id:'Fable 5.1',modelState:'observed',source:'session'}]},
    {id:'opencode',label:'OpenCode',model:'qwen3',modelState:'configured',models:[{id:'qwen3',modelState:'configured',source:'config'}]},
  ]);
  assert.deepEqual(result.rows.map(c=>[c.id,c.model,c.models.length]),[['codex','gpt-6-sol',2],['claude','Fable 5.1',1],['opencode','qwen3',1],['cursor',null,0],['grok',null,0]]);
  assert.equal(result.nodes.filter(n=>n.kind==='client').length,5);
  assert.equal(result.nodes.filter(n=>n.kind==='client-model').length,6);
  assert.ok(result.edges.some(e=>e.a==='client:codex'&&e.b==='client-model:codex:1'));
  assert.ok(result.nodes.some(n=>n.clientModel?.id==='gpt-6-luna'&&n.subtitle==='SAVED SESSION CHOICE'));
  assert.ok(result.nodes.some(n=>n.clientModel?.id==='gpt-6-sol'&&n.subtitle==='RECORDED MODEL'));
  assert.ok(result.nodes.some(n=>n.id==='client-model:cursor:unknown'&&n.unknown));
  assert.ok(result.nodes.some(n=>n.id==='client-model:grok:unknown'&&n.unknown));
});

test('client nodes disclose unknown subagent activity instead of presenting model variety as an agent count',()=>{
  const result=project([
    {id:'codex',model:'gpt-6-sol',modelState:'observed',models:[{id:'gpt-6-sol',modelState:'observed'},{id:'gpt-6-luna',modelState:'observed'}]},
    {id:'claude',model:'claude-opus-5-5',modelState:'observed',models:[{id:'claude-opus-5-5',modelState:'observed'}]},
    {id:'opencode',model:'kimi-k2.7-code',modelState:'configured',models:[{id:'kimi-k2.7-code',modelState:'configured'},{id:'glm-5.3',modelState:'configured'},{id:'nemotron-3-ultra-free',modelState:'configured'}]},
  ]);
  for(const id of ['codex','claude','opencode']){
    const hub=result.nodes.find(node=>node.id===`client:${id}`);
    assert.equal(hub.subtitle,'SUBAGENT ACTIVITY UNKNOWN');
    assert.equal(hub.active,undefined);
    assert.deepEqual(result.rows.find(row=>row.id===id).subagents,{
      state:'unknown',active:null,ageSeconds:null,source:'No verified subagent lifecycle feed connected',
    });
  }
  assert.equal(result.nodes.filter(node=>node.kind==='client-model'&&node.client.id==='opencode').length,3,
    'three saved model IDs remain identities and do not become a subagent count');
});

test('unavailable clients and missing identities remain explicit unknown branches',()=>{
  const result=project([{id:'claude',model:null,modelState:'unknown',source:'Unavailable',detail:'Session unreadable'}]);
  assert.equal(result.nodes.filter(n=>n.kind==='client').length,5);
  assert.equal(result.nodes.filter(n=>n.kind==='client-model'&&n.unknown).length,5);
  assert.ok(result.nodes.some(n=>n.id==='client-model:claude:unknown'&&n.subtitle==='UNKNOWN'));
  assert.equal(result.rows.find(c=>c.id==='claude').detail,'Session unreadable');
});

test('session recency and supplied activity never animate client models',()=>{
  const result=project([{id:'codex',model:'gpt-6-astra',modelState:'observed',observedAt:Date.now()/1000,activity:'generating',models:[{id:'gpt-6-astra',modelState:'observed',observedAt:Date.now()/1000,activity:'generating',source:'session'}]}]);
  assert.equal(result.rows[0].activity,'unknown');
  assert.equal(result.rows[0].models[0].activity,'unknown');
  assert.ok(result.nodes.filter(n=>n.kind==='client'||n.kind==='client-model').every(n=>!n.active));
});

test('future client timestamp is shown as a clock mismatch',()=>{
  const result=project([{id:'codex',model:'gpt-6-sol',modelState:'observed',
    observedAt:Date.now()/1000+30,models:[{id:'gpt-6-sol',modelState:'observed'}]}]);
  assert.equal(result.ages[0],'Clock mismatch');
});

test('future snapshot cannot keep a busy runtime model live',()=>{
  const script=`${source}\nsnapshot={host:'mac',sampledAt:Date.now()/1000+30,models:[{id:'local-model',host:'mac',state:'busy',loaded:true,ageSeconds:0}],clients:[],sources:[{id:'lms-ps',state:'live'}],activityKnown:true,pipeline:{status:'idle'}};connected=true;runtimeGraph();({fresh:fresh(),clockMismatch:clockMismatch(),rows:models().map(m=>({state:m.state,loaded:m.loaded})),activeNodes:graph.nodes.filter(n=>n.kind==='model'&&n.active).length})`;
  const result=JSON.parse(JSON.stringify(runInNewContext(script,{...layoutContext,Date,Set,Map,Math,Number,String,Array,Object,window:{innerWidth:820},document:{getElementById:id=>id==='graphRegion'?{clientWidth:590,clientHeight:546}:null}})));
  assert.equal(result.fresh,false);
  assert.equal(result.clockMismatch,true);
  assert.deepEqual(result.rows,[{state:'stale',loaded:null}]);
  assert.equal(result.activeNodes,0);
});

test('Windows and Mac snapshots retain the same client identity branches',()=>{
  const clients=[{id:'opencode',model:'qwen3',modelState:'configured',models:[{id:'qwen3',modelState:'configured',source:'config'}]}];
  for(const host of ['mac','windows']){
    const nodes=project(clients,host).nodes;
    assert.ok(nodes.some(n=>n.id==='client-model:opencode:0'&&n.label==='Qwen3'));
    assert.ok(nodes.some(n=>n.id==='client-model:claude:unknown'));
    assert.ok(nodes.some(n=>n.id==='client-model:cursor:unknown'));
    assert.ok(nodes.some(n=>n.id==='client-model:grok:unknown'));
    assert.equal(nodes.filter(n=>n.kind==='client'||n.kind==='client-model').some(n=>n.active),false);
  }
});

test('identity summary counts observed, configured-only, and unknown clients separately',()=>{
  const clients=[
    {id:'codex',model:'gpt-6-sol',modelState:'observed'},
    {id:'claude',model:'Fable 5.1',modelState:'configured'},
    {id:'opencode',model:null,modelState:'unknown'},
  ];
  assert.deepEqual(project(clients).counts,{observed:1,configured:1,unknown:3,total:5});
  assert.deepEqual(project([]).counts,{observed:0,configured:0,unknown:5,total:5});
});

test('advertised Windows worker connects to Mac hub without implying models or activity',()=>{
  const script=`${source}\nsnapshot={host:'mac',sampledAt:Date.now()/1000,models:[],clients:[],sources:[],windowsWorker:{state:'advertised',ageSeconds:4,modelsAdvertised:['local/a','local/b'],detail:'Heartbeat inventory only'},pipeline:{status:'idle'}};connected=true;runtimeGraph();({nodes:graph.nodes,edges:graph.edges})`;
  const result=JSON.parse(JSON.stringify(runInNewContext(script,{...layoutContext,Date,Set,Map,Math,Number,String,Array,Object,window:{innerWidth:820},document:{getElementById:id=>id==='graphRegion'?{clientWidth:590,clientHeight:546}:null}})));
  const windows=result.nodes.find(node=>node.kind==='windows-worker');
  assert.equal(windows.unknown,true);
  assert.equal(windows.subtitle,'WORKER ADVERTISED · INFERENCE UNKNOWN');
  assert.ok(result.edges.some(edge=>edge.a==='runtime'&&edge.b==='windows-worker'));
  assert.equal(result.nodes.filter(node=>node.kind==='model'&&node.model?.host==='windows').length,0);
  assert.equal(result.nodes.filter(node=>node.kind==='windows-worker').length,1);
});

test('unverified Windows lane stays visible without claiming a live connection',()=>{
  const result=project([],'mac');
  const windows=result.nodes.find(node=>node.kind==='windows-worker');
  assert.equal(windows.unknown,true);
  assert.equal(windows.workerAdvertised,false);
  assert.equal(windows.subtitle,'WORKER UNVERIFIED · INFERENCE UNKNOWN');
  assert.ok(result.edges.some(edge=>edge.a==='runtime'&&edge.b==='windows-worker'&&edge.dim));
  assert.equal(result.nodes.filter(node=>node.kind==='model'&&node.model?.host==='windows').length,0);
});

test('client branch colors are distinct and their model nodes inherit the same hue',()=>{
  const result=project([],'mac');
  const clients=result.nodes.filter(node=>node.kind==='client');
  assert.equal(new Set(clients.map(node=>node.color)).size,5);
  for(const client of clients){
    const model=result.nodes.find(node=>node.id===`client-model:${client.client.id}:unknown`);
    assert.equal(model.color,client.color);
  }
  const css=readFileSync(new URL('./web/style.css',import.meta.url),'utf8');
  assert.match(css,/\.node\.client \.label\{opacity:\.9;fill:currentColor/);
});

test('all local model stars stay in a compact grid with direct Mac hub spokes',()=>{
  const models=Array.from({length:9},(_,i)=>({id:`local/model-${i}`,host:'mac',state:'unloaded',loaded:false,
    metadata:{parameters:i===0?'370M':i===8?'27B':'4B',capabilities:{reasoning:i===8,vision:i===1}}}));
  const script=`${source}\nsnapshot={host:'mac',sampledAt:Date.now()/1000,models:modelRows,clients:[],sources:[],pipeline:{status:'idle'}};connected=true;modelScope='all';runtimeGraph();({nodes:graph.nodes,edges:graph.edges})`;
  const result=JSON.parse(JSON.stringify(runInNewContext(script,{...layoutContext,modelRows:models,Date,Set,Map,Math,Number,String,Array,Object,
    window:{innerWidth:639},document:{getElementById:id=>id==='graphRegion'?{clientWidth:607,clientHeight:1120}:null}})));
  const stars=result.nodes.filter(node=>node.kind==='model');
  assert.equal(stars.length,9);
  assert.equal(new Set(stars.map(node=>node.x)).size,3);
  assert.equal(new Set(stars.map(node=>node.y)).size,3);
  assert.ok(stars.every(node=>node.y>result.nodes.find(n=>n.id==='runtime').y));
  assert.equal(result.edges.filter(edge=>edge.a==='runtime'&&edge.b.startsWith('model:')).length,9);
  assert.ok(stars.find(node=>node.model.id==='local/model-8').r>stars.find(node=>node.model.id==='local/model-0').r);
});

test('quiet headline explicitly scopes status to local runtime',()=>{
  const script=`${source}\n({quiet:runtimeCopy(true,true,0,true,0,0),unknown:runtimeCopy(true,false,0,true,0,null)})`;
  const copy=JSON.parse(JSON.stringify(runInNewContext(script,{...layoutContext,Date,Set,Map,Math,Number,String,Array,Object})));
  assert.equal(copy.quiet.lead,'Local runtime');
  assert.equal(copy.quiet.em,'quiet now.');
  assert.match(copy.quiet.sub,/No current local generation reported/);
  assert.equal(copy.unknown.em,'activity unknown.');
  assert.doesNotMatch(JSON.stringify(copy),/Ready for work|cloud.*quiet/i);
});

test('in-flight Windows job animates the link and node without claiming generation',()=>{
  const now=Date.now()/1000;
  const jobs={schemaVersion:1,journal:'recorded',inFlight:[{id:'mac-20260925-081500-'+'d'.repeat(32),model:'gpt-oss-20b',ageSeconds:7,timeoutSeconds:60}],recent:[],lastSuccess:null};
  const script=`${source}\nsnapshot={host:'mac',sampledAt:${now},models:[],clients:[],sources:[],windowsWorker:{state:'advertised',ageSeconds:4,modelsAdvertised:['gpt-oss-20b'],detail:'x'},windowsJobs:${JSON.stringify(jobs)},pipeline:{status:'idle'}};connected=true;runtimeGraph();({nodes:graph.nodes,edges:graph.edges,labels:graph.labels})`;
  const result=JSON.parse(JSON.stringify(runInNewContext(script,{...layoutContext,Date,Set,Map,Math,Number,String,Array,Object,JSON,window:{innerWidth:820},document:{getElementById:id=>id==='graphRegion'?{clientWidth:590,clientHeight:546}:null}})));
  const windows=result.nodes.find(node=>node.kind==='windows-worker');
  assert.equal(windows.inFlight,true);
  assert.equal(windows.unknown,false);
  assert.match(windows.subtitle,/^JOB IN FLIGHT · GPT OSS 20b · \d+s$/);
  const link=result.edges.find(edge=>edge.a==='runtime'&&edge.b==='windows-worker');
  assert.equal(link.flow,true);
  assert.equal(link.dim,false);
  assert.ok(result.labels.some(label=>label.text==='WINDOWS PC'));
});

test('last validated Windows result is shown when nothing is in flight',()=>{
  const now=Date.now()/1000;
  const jobs={schemaVersion:1,journal:'recorded',inFlight:[],recent:[{id:'mac-20260925-081500-'+'d'.repeat(32),state:'success',model:'gpt-oss-20b',elapsedSeconds:6.25,ageSeconds:30}],lastSuccess:{id:'mac-20260925-081500-'+'d'.repeat(32),state:'success',model:'gpt-oss-20b',elapsedSeconds:6.25,ageSeconds:30}};
  const script=`${source}\nsnapshot={host:'mac',sampledAt:${now},models:[],clients:[],sources:[],windowsWorker:{state:'advertised',ageSeconds:4,modelsAdvertised:['gpt-oss-20b'],detail:'x'},windowsJobs:${JSON.stringify(jobs)},pipeline:{status:'idle'}};connected=true;runtimeGraph();({nodes:graph.nodes,edges:graph.edges})`;
  const result=JSON.parse(JSON.stringify(runInNewContext(script,{...layoutContext,Date,Set,Map,Math,Number,String,Array,Object,JSON,window:{innerWidth:820},document:{getElementById:id=>id==='graphRegion'?{clientWidth:590,clientHeight:546}:null}})));
  const windows=result.nodes.find(node=>node.kind==='windows-worker');
  assert.equal(windows.inFlight,false);
  assert.equal(windows.verified,true);
  assert.equal(windows.subtitle,'LAST INFERENCE · GPT OSS 20b · 6.3s');
  assert.ok(result.edges.find(edge=>edge.b==='windows-worker').dim);
});

test('an old or superseded Windows success does not paint the node verified',()=>{
  const now=Date.now()/1000;
  const ok={id:'mac-20260922-081500-'+'d'.repeat(32),state:'success',model:'gpt-oss-20b',elapsedSeconds:6.25,ageSeconds:3*86400};
  const jobs={schemaVersion:1,journal:'recorded',inFlight:[],recent:[],lastSuccess:ok};
  const script=`${source}\nsnapshot={host:'mac',sampledAt:${now},models:[],clients:[],sources:[],windowsWorker:{state:'unknown',ageSeconds:null,modelsAdvertised:[],detail:'x'},windowsJobs:${JSON.stringify(jobs)},pipeline:{status:'idle'}};connected=true;runtimeGraph();({nodes:graph.nodes,labels:graph.labels})`;
  const result=JSON.parse(JSON.stringify(runInNewContext(script,{...layoutContext,Date,Set,Map,Math,Number,String,Array,Object,JSON,window:{innerWidth:820},document:{getElementById:id=>id==='graphRegion'?{clientWidth:590,clientHeight:546}:null}})));
  const windows=result.nodes.find(node=>node.kind==='windows-worker');
  assert.equal(windows.verified,false);
  assert.equal(windows.subtitle,'WORKER UNVERIFIED · INFERENCE UNKNOWN');
  assert.ok(result.labels.some(label=>label.text==='WINDOWS PC'));
});

test('a degraded Windows worker says so instead of looking unverified',()=>{
  const now=Date.now()/1000;
  const script=`${source}\nsnapshot={host:'mac',sampledAt:${now},models:[],clients:[],sources:[],windowsWorker:{state:'degraded',ageSeconds:5,modelsAdvertised:[],detail:'x'},windowsJobs:{schemaVersion:1,inFlight:[],recent:[],lastSuccess:null},pipeline:{status:'idle'}};connected=true;runtimeGraph();({nodes:graph.nodes,labels:graph.labels})`;
  const result=JSON.parse(JSON.stringify(runInNewContext(script,{...layoutContext,Date,Set,Map,Math,Number,String,Array,Object,JSON,window:{innerWidth:820},document:{getElementById:id=>id==='graphRegion'?{clientWidth:590,clientHeight:546}:null}})));
  const windows=result.nodes.find(node=>node.kind==='windows-worker');
  assert.equal(windows.subtitle,'WORKER DEGRADED · NO MODEL LANE');
  assert.ok(result.labels.some(label=>label.text==='WINDOWS PC'));
});

test('Windows lanes hang off the PC node and link journaled clients, live only while in flight',()=>{
  const now=Date.now()/1000;
  const lanes={fast:{up:true,model:'gpt-oss-20b',kind:'gpt-oss',slotsBusy:1,slotsTotal:2},deep:{up:false,model:'Qwen3.8-27B',kind:'qwen',slotsBusy:null,slotsTotal:null}};
  const jobs={schemaVersion:1,inFlight:[{id:'mac-1',model:'gpt-oss-20b',ageSeconds:3,timeoutSeconds:120,client:'claude',lane:'fast'}],recent:[{id:'mac-0',state:'success',model:'Qwen3.8-27B',ageSeconds:90,client:'codex',lane:'deep'},{id:'mac-z',state:'success',ageSeconds:95,client:'nisi',lane:'fast',predictedPerSecond:135}],lastSuccess:null};
  const script=`${source}\nsnapshot={host:'mac',sampledAt:${now},models:[],clients:[],sources:[],windowsWorker:{state:'advertised',ageSeconds:4,modelsAdvertised:['gpt-oss-20b'],detail:'x',lanes:${JSON.stringify(lanes)},headless:{state:'on',reason:null,expiresInSeconds:7530,grantedBy:'inference-monitor'}},windowsJobs:${JSON.stringify(jobs)},pipeline:{status:'idle'}};connected=true;runtimeGraph();({nodes:graph.nodes,edges:graph.edges})`;
  const result=JSON.parse(JSON.stringify(runInNewContext(script,{...layoutContext,Date,Set,Map,Math,Number,String,Array,Object,JSON,window:{innerWidth:820},document:{getElementById:id=>id==='graphRegion'?{clientWidth:590,clientHeight:546}:null}})));
  const windows=result.nodes.find(node=>node.kind==='windows-worker');
  assert.match(windows.subtitle,/ · HEADLESS ON · 2H 5M LEFT$/);
  const fast=result.nodes.find(node=>node.id==='windows-lane:fast'),deep=result.nodes.find(node=>node.id==='windows-lane:deep');
  assert.equal(fast.label,'fast · gpt-oss-20b');assert.equal(fast.lane.rate,135);assert.equal(fast.subtitle,'1/2 BUSY');assert.equal(fast.inFlight,true);assert.equal(fast.active,true);
  assert.equal(deep.subtitle,'DOWN');assert.equal(deep.unknown,true);assert.equal(deep.inFlight,false);
  assert.ok(Math.hypot(fast.x-windows.x,fast.y-windows.y)<160);
  assert.ok(result.edges.some(e=>e.a==='windows-worker'&&e.b==='windows-lane:fast'&&e.flow&&!e.dim));
  assert.ok(result.edges.some(e=>e.a==='windows-worker'&&e.b==='windows-lane:deep'&&e.dim));
  assert.ok(result.edges.some(e=>e.a==='client:claude'&&e.b==='windows-lane:fast'&&e.flow&&!e.dim));
  assert.ok(result.edges.some(e=>e.a==='client:codex'&&e.b==='windows-lane:deep'&&!e.flow&&e.dim));
  assert.equal(result.edges.filter(e=>e.b.startsWith('windows-lane:')&&e.a.startsWith('client:')).length,2);
  const stale=`${source}\nsnapshot={host:'mac',sampledAt:${now-60},models:[],clients:[],sources:[],windowsWorker:{state:'advertised',ageSeconds:4,lanes:${JSON.stringify(lanes)},headless:{state:'on',expiresInSeconds:7500}},windowsJobs:${JSON.stringify(jobs)},pipeline:{status:'idle'}};connected=true;runtimeGraph();({nodes:graph.nodes,edges:graph.edges})`;
  const old=JSON.parse(JSON.stringify(runInNewContext(stale,{...layoutContext,Date,Set,Map,Math,Number,String,Array,Object,JSON,window:{innerWidth:820},document:{getElementById:id=>id==='graphRegion'?{clientWidth:590,clientHeight:546}:null}})));
  assert.equal(old.nodes.filter(node=>node.kind==='windows-lane').length,0);
  assert.doesNotMatch(old.nodes.find(node=>node.kind==='windows-worker').subtitle,/HEADLESS/);
});
