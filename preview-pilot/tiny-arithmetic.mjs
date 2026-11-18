// Distinct transport/engine acceptance test; no model answer is prefilled.
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import {checkRoute} from './fleet-route.mjs';
import {installNetGuard} from './net-guard.mjs';
import {modelTimeoutMs,workflowDeadlineMs,reasoningFields,errorEvidence,maxTokens,outputSample,extractFinalJSON,responseFormatMode,responseFormatFor,requestMode,completionSettings,completionGrammarCall,chatCall,modelMatches} from './model-call.mjs';
import {runWorkflow,createCandidate} from './vendor/nisi-0.3.1/workflow/engine.mjs';
import {strictJSON} from './vendor/nisi-0.3.1/adapters/local-chat.mjs';
import {sha256Text,stableStringify} from './vendor/nisi-0.3.1/workflow/contracts.mjs';
let input='';for await(const c of process.stdin){input+=c;if(input.length>8192)throw Error('INPUT_CAP');}
const cfg=JSON.parse(input),u=new URL(cfg.endpoint);
if(cfg.mode!=='live')throw Error('MODE_REFUSED');
checkRoute(cfg);
installNetGuard(cfg.endpoint);
const MODEL_TIMEOUT_MS=modelTimeoutMs(cfg),REASONING=reasoningFields(cfg),MAX_TOKENS=maxTokens(cfg,64),FORMAT_MODE=responseFormatMode(cfg),REQUEST_MODE=requestMode(cfg),COMPLETION=REQUEST_MODE==='chat'?null:completionSettings(cfg);
const SCHEMA={type:'object',properties:{answer:{type:'integer'}},required:['answer'],additionalProperties:false};
if(typeof cfg.reportStore!=='string'||!path.isAbsolute(cfg.reportStore))throw Error('REPORT_STORE_PATH_REFUSED');
if(!(await fs.stat(cfg.reportStore)).isDirectory())throw Error('REPORT_STORE_DIR_REFUSED');
if(u.protocol!=='http:'||u.hostname!=='127.0.0.1'||u.username||u.password||u.search||u.hash||u.pathname!=='/v1/chat/completions')throw Error('ENDPOINT_REFUSED');
const receipts=[];
const author={id:'preview.tiny-arithmetic-author',async draft({binding}){
 const payload={model:cfg.model,messages:[{role:'system',content:'Answer the arithmetic question. Return only a JSON object with one key answer and an integer value.'},{role:'user',content:'What is 6 multiplied by 7?'}],temperature:0,max_tokens:MAX_TOKENS,stream:false,...REASONING,response_format:responseFormatFor(FORMAT_MODE,{name:'arithmetic_answer',schema:SCHEMA,fallback:{type:'json_object'}})};
 const started=Date.now();const receipt={requestedModel:cfg.model,maxOutputTokens:MAX_TOKENS,timeoutMs:MODEL_TIMEOUT_MS,disableThinking:cfg.disableThinking===true,finishReason:null,usage:null};receipts.push(receipt);
 try{
 const r=REQUEST_MODE==='chat'?await chatCall({endpoint:cfg.endpoint,payload,timeoutMs:MODEL_TIMEOUT_MS,receipt,allowFallback:FORMAT_MODE==='json-schema',parse:strictJSON,request:http.request})
  :await completionGrammarCall({endpoint:cfg.endpoint,messages:payload.messages,schema:SCHEMA,maxTokens:MAX_TOKENS,timeoutMs:MODEL_TIMEOUT_MS,receipt,settings:COMPLETION,parse:strictJSON,request:http.request});
 receipt.elapsedMs=Date.now()-started;receipt.reportedModel=r.model;receipt.usage=r.usage;receipt.finishReason=r.finishReason;
 if(r.finishReason!=='stop')receipt.outputSample=outputSample(r.content);
 if(r.finishReason!=='stop'||!modelMatches(r,REQUEST_MODE,cfg.model)||r.toolCalls||r.refusal)throw Error('FINISH_OR_MODEL_REFUSED');
 const rawContent=r.content;if(typeof rawContent!=='string')throw Error('CONTENT_REFUSED');let content;
 try{const x=extractFinalJSON(rawContent,strictJSON);content=x.content;if(x.stripped)receipt.preambleStripped=x.stripped;}catch(e){receipt.outputSample=outputSample(rawContent);throw e;}
 const candidate={files:[{path:'answer.json',content}]};receipt.status='RESPONSE_VALIDATED';
 return {candidate,evidence:{...binding,candidateFingerprint:createCandidate(candidate,{authorId:author.id}).fingerprint,note:'Actual local model output; host did not supply or correct its answer.'}};
 }catch(e){Object.assign(receipt,errorEvidence(e));receipt.status='FAILED';throw e;}
}};
const valid=c=>{try{const o=strictJSON(c.files[0].content);return c.files.length===1&&c.files[0].path==='answer.json'&&Object.keys(o).length===1&&Number.isInteger(o.answer)&&o.answer===6*7;}catch{return false;}};
const finding={code:'ARITHMETIC_INCORRECT',message:'Expected exactly integer answer equal to 6 times 7.'};
const task={taskId:'agiw.preview.tiny-arithmetic',mode:'edit',language:'json',allowedFiles:['answer.json'],protectedFiles:[],protectedSnapshots:{},acceptanceCriteria:['Exactly one integer answer field equal to six multiplied by seven.'],policy:{repairBudget:0,totalDeadlineMs:workflowDeadlineMs(cfg),requiredReviewers:1,requireReportStore:true}};
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
const report=await runWorkflow(task,{adapters:{author,authorizeContext:{authorize:({binding})=>({status:'PASS',evidence:{...binding,reason:''}})},staticChecks:{check({candidate,binding}){let ok;try{const o=strictJSON(candidate.files[0].content);ok=Object.keys(o).length===1&&Number.isInteger(o.answer);}catch{ok=false;}return {status:ok?'PASS':'FAIL',evidence:{...binding,findings:ok?[]:[finding],reason:ok?'':'Invalid JSON shape.'}};}},tests:{run({candidate,binding}){const ok=valid(candidate);return {status:ok?'PASS':'FAIL',evidence:{...binding,assertionsExecuted:1,assertionsPassed:ok?1:0,failures:ok?[]:[finding],reason:ok?'':'Incorrect answer.'}};}},reviewers:[{id:'preview.deterministic-arithmetic-check',review({candidate,checks,tests,binding,reviewerId}){const ok=valid(candidate)&&checks.status==='PASS'&&tests.status==='PASS';return {status:ok?'PASS':'FAIL',evidence:{...binding,reviewerId,findings:ok?[]:[finding],reason:ok?'':'Rejected candidate.',summary:'Deterministic arithmetic check, not independent LLM review.'}};}}]},reportStore});
console.log(JSON.stringify({mode:'live',nisiVersion:'0.3.1',route:cfg.route,independentLLMReview:false,modelCalls:receipts.length,report,reportStorePath,authorReceipts:receipts}));process.exitCode=report.outcome==='COMPLETED'?0:1;
