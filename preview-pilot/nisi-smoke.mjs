// Private preview only. No Jev import, provider lookup or generated-code execution.
import fs from 'node:fs/promises';
import path from 'node:path';
import {checkRoute} from './fleet-route.mjs';
import {programTask} from './program-task.mjs';
import {installNetGuard} from './net-guard.mjs';
import {modelTimeoutMs,workflowDeadlineMs,reasoningFields,maxTokens} from './model-call.mjs';
import {createLocalChatAuthorAdapter} from './vendor/nisi-0.3.1/adapters/local-chat.mjs';
import {runWorkflow} from './vendor/nisi-0.3.1/workflow/engine.mjs';
import {sha256Text,stableStringify} from './vendor/nisi-0.3.1/workflow/contracts.mjs';
let raw=''; for await (const chunk of process.stdin) {raw+=chunk; if(raw.length>8192) throw Error('INPUT_CAP');}
const cfg=JSON.parse(raw);
checkRoute(cfg);
installNetGuard(cfg.endpoint);
if(!['fixture','live'].includes(cfg.mode)) throw Error('MODE_REFUSED');
if(typeof cfg.reportStore!=='string'||!path.isAbsolute(cfg.reportStore)) throw Error('REPORT_STORE_PATH_REFUSED');
if(!(await fs.stat(cfg.reportStore)).isDirectory()) throw Error('REPORT_STORE_DIR_REFUSED');
const u=new URL(cfg.endpoint);
if(u.protocol!=='http:'||u.hostname!=='127.0.0.1'||u.username||u.password||u.search||u.hash||u.pathname!=='/v1/chat/completions')throw Error('ENDPOINT_REFUSED');
const content='{"backoff":"exponential","maxRetries":3,"retryDelayMs":250}';
const fake=async()=>new Response(JSON.stringify({model:cfg.model,choices:[{index:0,message:{role:'assistant',content:JSON.stringify({candidate:{files:[{path:'retry-config.json',content}]},note:'Synthetic fixture, no inference.'})},finish_reason:'stop'}]}),{status:200});
const author=createLocalChatAuthorAdapter({destination:'LOOPBACK_HTTP',endpoint:cfg.endpoint,model:cfg.model,id:'preview.local-author',timeoutMs:modelTimeoutMs(cfg),...(reasoningFields(cfg).reasoning_effort?{reasoningEffort:'none'}:{}),maxOutputTokens:maxTokens(cfg,256),maxRequestBytes:16384,maxResponseBytes:32768,...(cfg.mode==='fixture'?{fetch:fake}:{})});
const task={...programTask(import.meta.url),protectedFiles:[],protectedSnapshots:{},policy:{repairBudget:0,totalDeadlineMs:workflowDeadlineMs(cfg),requiredReviewers:1,requireReportStore:true}};
const valid=c=>{try{const o=JSON.parse(c.files[0].content);return c.files.length===1&&c.files[0].path==='retry-config.json'&&JSON.stringify(Object.keys(o).sort())==='["backoff","maxRetries","retryDelayMs"]'&&o.backoff==='exponential'&&o.maxRetries===3&&o.retryDelayMs===250;}catch{return false;}};
const finding={code:'CONFIG_MISMATCH',message:'Exact synthetic config contract not met.'};
let reportStorePath=null;
const reportStore={async store({report,reportSha256,binding}){
 const target=path.join(cfg.reportStore,`${report.runId}.json`);
 const handle=await fs.open(target,'wx',0o600);
 try{
  await handle.writeFile(JSON.stringify(report,null,2)+'\n');
  // Node's permission model disables fsync; every other failure is fatal.
  try{await handle.sync();}catch(e){if(e?.code!=='ERR_ACCESS_DENIED')throw e;}
 }finally{await handle.close();}
 const readBack=JSON.parse(await fs.readFile(target,'utf8'));
 if(sha256Text(`nisi/run-report/v1\0${stableStringify(readBack)}`)!==reportSha256)throw Error('REPORT_SHA_MISMATCH');
 reportStorePath=target;
 return {status:'PASS',evidence:{...binding,outcome:report.outcome,reportSha256}};
}};
const report=await runWorkflow(task,{adapters:{
 authorizeContext:{authorize:({binding})=>({status:'PASS',evidence:{...binding,reason:''}})},author,
 staticChecks:{check({candidate,binding}){const ok=valid(candidate);return {status:ok?'PASS':'FAIL',evidence:{...binding,findings:ok?[]:[finding],reason:ok?'':'Config invalid.'}};}},
 tests:{run({candidate,binding}){const ok=valid(candidate);return {status:ok?'PASS':'FAIL',evidence:{...binding,assertionsExecuted:1,assertionsPassed:ok?1:0,failures:ok?[]:[finding],reason:ok?'':'Config assertion failed.'}};}},
 reviewers:[{id:'preview.deterministic-checker',review({candidate,checks,tests,binding,reviewerId}){const ok=valid(candidate)&&checks.status==='PASS'&&tests.status==='PASS';return {status:ok?'PASS':'FAIL',evidence:{...binding,reviewerId,findings:ok?[]:[finding],reason:ok?'':'Contract failed.',summary:'Deterministic checker only; NOT independent LLM review.'}};}}]
},reportStore});
console.log(JSON.stringify({mode:cfg.mode,nisiVersion:'0.3.1',route:cfg.route,independentLLMReview:false,modelCalls:cfg.mode==='fixture'?0:author.receipts().length,report,reportStorePath,authorReceipts:author.receipts()}));
process.exitCode=report.outcome==='COMPLETED'?0:1;
