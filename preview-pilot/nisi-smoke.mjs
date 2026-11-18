// Private preview only. No Jev import, provider lookup, filesystem writes or generated-code execution.
import {createLocalChatAuthorAdapter} from './vendor/nisi-0.3.1/adapters/local-chat.mjs';
import {runWorkflow} from './vendor/nisi-0.3.1/workflow/engine.mjs';
let raw=''; for await (const chunk of process.stdin) {raw+=chunk; if(raw.length>8192) throw Error('INPUT_CAP');}
const cfg=JSON.parse(raw);
if(cfg.route?.worker!=='m5-macbook-pro'||cfg.route?.preanalysis!=='local'||cfg.route?.reason!=='strict-local') throw Error('ROUTE_REFUSED');
if(!['fixture','live'].includes(cfg.mode)) throw Error('MODE_REFUSED');
const u=new URL(cfg.endpoint);
if(u.protocol!=='http:'||u.hostname!=='127.0.0.1'||u.username||u.password||u.search||u.hash||u.pathname!=='/v1/chat/completions')throw Error('ENDPOINT_REFUSED');
const content='{"backoff":"exponential","maxRetries":3,"retryDelayMs":250}';
const fake=async()=>new Response(JSON.stringify({model:cfg.model,choices:[{index:0,message:{role:'assistant',content:JSON.stringify({candidate:{files:[{path:'retry-config.json',content}]},note:'Synthetic fixture, no inference.'})},finish_reason:'stop'}]}),{status:200});
const author=createLocalChatAuthorAdapter({destination:'LOOPBACK_HTTP',endpoint:cfg.endpoint,model:cfg.model,id:'preview.local-author',timeoutMs:15000,maxOutputTokens:256,maxRequestBytes:16384,maxResponseBytes:32768,...(cfg.mode==='fixture'?{fetch:fake}:{})});
const task={taskId:'agiw.preview.synthetic',mode:'edit',language:'json',allowedFiles:['retry-config.json'],protectedFiles:[],protectedSnapshots:{},acceptanceCriteria:['Create retry-config.json with exactly backoff exponential, maxRetries 3 and retryDelayMs 250.'],policy:{repairBudget:0,totalDeadlineMs:20000,requiredReviewers:1,requireReportStore:false}};
const valid=c=>{try{const o=JSON.parse(c.files[0].content);return c.files.length===1&&c.files[0].path==='retry-config.json'&&JSON.stringify(Object.keys(o).sort())==='["backoff","maxRetries","retryDelayMs"]'&&o.backoff==='exponential'&&o.maxRetries===3&&o.retryDelayMs===250;}catch{return false;}};
const finding={code:'CONFIG_MISMATCH',message:'Exact synthetic config contract not met.'};
const report=await runWorkflow(task,{adapters:{
 authorizeContext:{authorize:({binding})=>({status:'PASS',evidence:{...binding,reason:''}})},author,
 staticChecks:{check({candidate,binding}){const ok=valid(candidate);return {status:ok?'PASS':'FAIL',evidence:{...binding,findings:ok?[]:[finding],reason:ok?'':'Config invalid.'}};}},
 tests:{run({candidate,binding}){const ok=valid(candidate);return {status:ok?'PASS':'FAIL',evidence:{...binding,assertionsExecuted:1,assertionsPassed:ok?1:0,failures:ok?[]:[finding],reason:ok?'':'Config assertion failed.'}};}},
 reviewers:[{id:'preview.deterministic-checker',review({candidate,checks,tests,binding,reviewerId}){const ok=valid(candidate)&&checks.status==='PASS'&&tests.status==='PASS';return {status:ok?'PASS':'FAIL',evidence:{...binding,reviewerId,findings:ok?[]:[finding],reason:ok?'':'Contract failed.',summary:'Deterministic checker only; NOT independent LLM review.'}};}}]
}});
console.log(JSON.stringify({mode:cfg.mode,nisiVersion:'0.3.1',route:cfg.route,independentLLMReview:false,modelCalls:cfg.mode==='fixture'?0:author.receipts().length,report,authorReceipts:author.receipts()}));
process.exitCode=report.outcome==='COMPLETED'?0:1;
