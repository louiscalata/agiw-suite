// Local fake-DOM and projection replays only. These record excluded findings.
import {harness} from './monitor-harness.mjs';
import * as layout from '../web/map-layout.mjs';
const results=[];
const record=(finding,reproduces,evidence)=>results.push({finding,reproduces,evidence});
const NOW=1800000000;
class Clock extends Date {static now(){return NOW*1000;}}
const {api,$}=harness({extra:{Date:Clock}});
const sources=['pipeline-router','router-active','monitor-receipts','router-archive','windows-jobs'].map(id=>({id,state:'live',ageSeconds:0,observationAgeSeconds:0}));
const base={schemaVersion:1,host:'mac',sampledAt:NOW,fullSampledAt:NOW,models:[],sources,clients:[],components:[],activity:{runs:[],complete:true,historyBound:{truncated:false}},pipeline:{status:'idle',pipelines:[],pipelinesComplete:true},windowsJobs:{schemaVersion:1,journal:'recorded',complete:true,inFlight:[],recent:[]}};
try {api.setFeed({...base,sampledAt:1e20,fullSampledAt:1e20});api.setPaused(true);api.renderSummary();record('sol10-A1#1 / sol10-A4#5',false,{});}
catch(error){record('sol10-A1#1 / sol10-A4#5',error.name==='RangeError',{error:error.name});}
api.setPaused(false);api.setFeed(structuredClone(base));api.renderActivity(api.vitalsInput().feed);
const evidence=layout.windowsJobsEvidence(base.windowsJobs,sources.at(-1),{feedFresh:true});
record('sol10-A4#4',evidence.inFlightComplete&&/In flight0/.test($('activityTiles').textContent),{evidence,tiles:$('activityTiles').textContent});
const route={runId:'route',status:'running',stage:'backend_draft',live:true,ageSeconds:1};
const expired=structuredClone(base);expired.sampledAt=expired.fullSampledAt=NOW-2.5;expired.pipeline={...route,pipelines:[route],pipelinesComplete:true};expired.sources[0].observationAgeSeconds=29;
api.setFeed(expired);api.select('pipeline');const inspector=api.inspect(api.graph.nodes.find(n=>n.id==='pipeline')).textContent;
record('sol11-A3#3',/Route running|RouteRunning/.test(inspector),{graph:api.nodeSubtitle('pipeline'),inspector});
const jobs={complete:true,inFlight:[0,1].map(i=>({id:'open-'+i,lane:'fast',client:'codex',ageSeconds:1,timeoutSeconds:600})),recent:Array.from({length:10},(_,i)=>({id:'done-'+i,state:'success',ageSeconds:i+2}))};
const feed=layout.activityFeed(jobs,[{runId:'verified',activity:'running',ageSeconds:null,host:'mac',source:'router-active'}],{limit:12,feedFresh:true});
record('sol11-A3#4',!feed.some(row=>row.kind==='route'),{rows:feed.length,routeVisible:feed.some(row=>row.kind==='route'),summary:layout.activitySummary(feed,{live:true})});
console.log(JSON.stringify(results,null,2));
