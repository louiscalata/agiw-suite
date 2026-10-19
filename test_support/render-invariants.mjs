import {readFileSync} from 'node:fs';
import {harness} from './monitor-harness.mjs';
import {layout,onlineMode} from './production.mjs';
import {checkDimensions} from './check-dimensions.mjs';
import {checkTwoSided} from './check-two-sided.mjs';
const {onlineCodeModeView}=onlineMode;

const cases=JSON.parse(readFileSync(0,'utf8')), failures=new Map();
const failedCases=new Set(), caseFailures=[];
const dimensions={},properties={};
let now=1800000000;
class Clock extends Date {static now(){return now*1000;}}
const {api,$}=harness({extra:{Date:Clock}});
function fail(item,field,actual){
  failedCases.add(item.label);
  properties[item.property||'no-false-certainty'].failedCases.add(item.label);
  caseFailures.push({case:item.label,property:item.property||'no-false-certainty',dimension:item.dimension||'original',input:item.input||item.changes,field,wrongOutput:actual});
  if(item.dimension)dimensions[item.dimension].failedCases.add(item.label);
  const key=field+'\n'+JSON.stringify(actual);
  const previous=failures.get(key);
  if(previous){previous.occurrences++;return;}
  failures.set(key,{case:item.label,field,wrongOutput:actual,occurrences:1});
}
function tiles(body){return Object.fromEntries(body.querySelectorAll('.tile').map(tile=>
  [tile.querySelector('.tile-label')?.textContent,tile.querySelector('.tile-value')?.textContent]));}
const unknown=value=>value==null||/unknown|unavailable|stale|at least|last report|—|not reported|incomplete|clock/i.test(String(value));
let rendered=0;
for(const item of cases){
  const property=item.property||'no-false-certainty';
  properties[property]||={cases:0,failedCases:new Set()};properties[property].cases++;
  if(item.dimension){dimensions[item.dimension]||={cases:0,failedCases:new Set()};dimensions[item.dimension].cases++;}
  const d=item.snapshot,c=item.changes;
  for(const error of item.errors)fail(item,'publisher',error);
  if(!d)continue;
  try{
    now=1800000000+(item.clockOffset||0);api.setPaused(Boolean(item.paused));
    if(item.initial){api.setFeed(item.initial,true);api.renderActivity(api.vitalsInput().feed);}
    api.setFeed(d,item.connected!==false);api.renderSummary();
    api.renderModelScope();
    const runtime=api.inspect({kind:'runtime'}),t=tiles(runtime);
    const vitals=api.vitalsInput().vitals;
    const caller=layout.localCallersView(d.localCallers,{feedFresh:api.liveNow()});
    const gpu=layout.macGpuView(d.macGpu,{feedFresh:api.liveNow()});
    const memory=layout.memoryView(d.memory,{feedFresh:api.liveNow(),host:'mac'});
    const lanes=layout.windowsLaneView(d.windowsWorker,null,{feedFresh:api.liveNow()});
    const peers=layout.agiwPeersRows(d.peers,{feedFresh:api.liveNow(),host:'windows'});
    const mode=onlineCodeModeView(d.onlineCodeMode,{feedFresh:api.liveNow()});
    // The actual app activity panel, including its empty/capped-history copy.
    api.renderActivity(api.vitalsInput().feed);
    const activity=$('activityStatus').textContent+' '+$('activityMeaning').textContent+' '+$('activityTiles').textContent+' '+$('activityList').textContent;
    rendered++;
    checkDimensions(item,api,$,fail);
    if(item.property)checkTwoSided(item,api,$,fail);
    if(item.label==='healthy'){
      for(const [field,actual,want] of [['loaded',$('loadedCount').textContent,'2'],
        ['active',$('activeCount').textContent,'1'],['callers',caller.count,1],['gpu',gpu.percent,40]]){
        if(actual!==want)fail(item,'healthy control '+field,actual);
      }
    }
    if(c.control==='empty'){
      for(const field of ['loadedCount','activeCount','queuedCount'])if($(field).textContent!=='0')fail(item,'healthy empty '+field,$(field).textContent);
      if(caller.count!==0)fail(item,'healthy empty callers',caller.count);
    }
    const cliGone=c.cli&&c.cli!=='partial';
    const apiGone=c.api&&c.api!=='partial';
    const inventoryLost=c['model-age']||c.snapshot||['stale','future'].includes(c.cli)
      ||(cliGone&&(apiGone||c.api==='partial'));
    const activityLost=c.cli||c['model-age']||c.snapshot;
    if(inventoryLost){
      if(!unknown($('modelScopeNote').textContent))fail(item,'roster.discovered',$('modelScopeNote').textContent);
      if(!unknown($('loadedCount').textContent))fail(item,'summary.loaded',$('loadedCount').textContent);
      if(!unknown(t.Loaded))fail(item,'inspector.loaded',t.Loaded);
      if(!c.snapshot&&d.history.at(-1).loaded!==null)fail(item,'history.loaded',d.history.at(-1).loaded);
      if(!c.snapshot&&item.presence?.loadedListComplete===true)fail(item,'presence.loadedListComplete',true);
      if(!c.snapshot&&d.components?.some(row=>row.id==='nisi'&&['ready','partial'].includes(row.state)))fail(item,'route.inventoryReadiness',d.components.find(row=>row.id==='nisi').state);
    }
    if(activityLost){
      if(!unknown($('activeCount').textContent))fail(item,'summary.active',$('activeCount').textContent);
      if(!unknown($('queuedCount').textContent))fail(item,'summary.queued',$('queuedCount').textContent);
      if(/\bquiet\b|\bidle\b/i.test($('headline').textContent+vitals.mac.detail))fail(item,'runtime.activity',vitals.mac.detail);
      if(!c.snapshot&&d.activityKnown)fail(item,'publisher.activityKnown',true);
    }
    if(c.callers||c.snapshot){
      if(caller.checked)fail(item,'callers.count',caller.count);
      if(/None connected|0 local process/i.test(runtime.textContent))fail(item,'callers.empty',runtime.textContent.slice(-300));
    }
    if(c['model-age']||c.snapshot){
      for(const row of api.modelRows()){
        const body=api.inspect({kind:'model',model:row});
        if(row.loaded!==null||['idle','busy','generating','unloaded'].includes(row.state))fail(item,'model.state',{loaded:row.loaded,state:row.state});
        if(/Resident in memory|Loaded · idle/.test(body.textContent))fail(item,'model.inspector',body.textContent.slice(0,170));
      }
    }
    if(c['api-instances']){
      for(const row of api.modelRows().filter(row=>row.id==='provider/a')){
        const body=api.inspect({kind:'model',model:row});
        const values=Object.fromEntries(body.querySelectorAll('dt').map(dt=>[dt.textContent,dt.parentNode.children[dt.parentNode.children.indexOf(dt)+1].textContent]));
        if(!unknown(values['Loaded instance count']))fail(item,'model.loadedInstanceCount',values['Loaded instance count']);
      }
    }
    if(c.router||c.snapshot){
      if(!unknown(vitals.route.detail))fail(item,'route.vital',vitals.route.detail);
      if(/idle|no active|nothing running/i.test(mode.label+' '+mode.taskLabel))fail(item,'route.mode',mode.taskLabel);
    }
    if(c.history||c.jobs||c.snapshot){
      if(/Nothing recorded yet|Nothing running now/.test(activity))fail(item,'activity.empty',activity.slice(0,250));
      const status=$('activityStatus').textContent;
      if(/\d+ (?:job|route)s? running now/.test(status)&&!/at least/i.test(status))fail(item,'activity.runningTotal',status);
      for(const [field,value] of Object.entries(tiles($('activityTiles'))))if(!unknown(value))fail(item,'activity.count.'+field,value);
    }
    if(c.jobs && ['malformed','partial','capped','future'].includes(c.jobs)){
      if(d.windowsJobs.complete===true)fail(item,'jobs.complete',true);
      const status=d.sources.find(s=>s.id==='windows-jobs');
      if(/no job in flight/.test(status?.detail))fail(item,'jobs.source',status.detail);
    }
    // A truncated log can still establish that PAIR wrote recently; its runtime
    // residency/counts cannot be inferred from that fact. Missing/expired writes
    // cannot establish up/down. The current UI only exposes this source detail.
    if(c.pair && !['partial','capped'].includes(c.pair)){
      if(d.pair.state!=='unknown')fail(item,'pair.state',d.pair.state);
    }
    if(c.gpu||c.snapshot){
      if(gpu.known)fail(item,'gpu.current',gpu.tile);
    }
    if(c.memory||c.snapshot){
      if(memory.known)fail(item,'memory.current',memory.tile);
    }
    if(c.worker||c.snapshot){
      if(lanes.visible&&layout.windowsLaneRows(lanes).some(([,text])=>/\bUp\b|\bDown\b|\d+\/\d+ slots/.test(text)))fail(item,'worker.lanes',layout.windowsLaneRows(lanes));
      if(c.worker&&!['partial','capped'].includes(c.worker)&&d.windowsWorker?.modelCount!=null)fail(item,'worker.modelCount',d.windowsWorker.modelCount);
    }
    if(c.peers||c.snapshot){
      for(const [,text]of peers){
        if(/(?:^| · )\d+ loaded(?: · |$)/.test(text))fail(item,'peer.loaded',text);
        if(/None has published/.test(text))fail(item,'peer.absent',text);
      }
    }
  }catch(error){fail(item,'renderer',`${error.name}: ${error.message}`);}
}
console.log(JSON.stringify({cases:cases.length,baseCases:cases.filter(c=>!c.dimension).length,rendered,failedCases:failedCases.size,properties:Object.fromEntries(Object.entries(properties).map(([key,value])=>[key,{cases:value.cases,failedCases:value.failedCases.size}])),dimensions:Object.fromEntries(Object.entries(dimensions).map(([key,value])=>[key,{cases:value.cases,failedCases:value.failedCases.size}])),violations:[...failures.values()],caseFailures},null,2));
process.exitCode=failures.size?1:0;
