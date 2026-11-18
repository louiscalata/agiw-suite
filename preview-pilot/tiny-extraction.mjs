// Distinct transport/engine acceptance test; no model answer is prefilled.
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import {runWorkflow,createCandidate} from './vendor/nisi-0.3.1/workflow/engine.mjs';
import {strictJSON} from './vendor/nisi-0.3.1/adapters/local-chat.mjs';
import {sha256Text,stableStringify} from './vendor/nisi-0.3.1/workflow/contracts.mjs';
let input='';for await(const c of process.stdin){input+=c;if(input.length>8192)throw Error('INPUT_CAP');}
const cfg=JSON.parse(input),u=new URL(cfg.endpoint);
if(cfg.mode!=='live'||cfg.route?.worker!=='m5-macbook-pro'||cfg.route?.preanalysis!=='local'||cfg.route?.reason!=='strict-local')throw Error('ROUTE_REFUSED');
if(typeof cfg.reportStore!=='string'||!path.isAbsolute(cfg.reportStore))throw Error('REPORT_STORE_PATH_REFUSED');
if(!(await fs.stat(cfg.reportStore)).isDirectory())throw Error('REPORT_STORE_DIR_REFUSED');
if(u.protocol!=='http:'||u.hostname!=='127.0.0.1'||u.username||u.password||u.search||u.hash||u.pathname!=='/v1/chat/completions')throw Error('ENDPOINT_REFUSED');
const receipts=[];
const author={id:'preview.tiny-extraction-author',async draft({binding}){
 const payload={model:cfg.model,messages:[{role:'system',content:'Extract the color named in the sentence. Return only a JSON object with one key color and a string value. Do not explain.'},{role:'user',content:'The car is red. What color is the car?'}],temperature:0,max_tokens:64,stream:false,response_format:{type:'json_schema',json_schema:{name:'extracted_color',strict:true,schema:{type:'object',properties:{color:{type:'string'}},required:['color'],additionalProperties:false}}}};
 const started=Date.now();const receipt={requestedModel:cfg.model,maxOutputTokens:64,finishReason:null,usage:null};receipts.push(receipt);
 const raw=await new Promise((resolve,reject)=>{const req=http.request({hostname:'127.0.0.1',port:u.port,path:u.pathname,method:'POST',headers:{'Content-Type':'application/json'},signal:AbortSignal.timeout(5000)},res=>{receipt.httpStatus=res.statusCode;if(res.statusCode!==200){res.destroy();reject(Error('HTTP_REFUSED'));return;}let s='';res.on('data',c=>{s+=c;if(Buffer.byteLength(s)>16384){res.destroy();reject(Error('RESPONSE_CAP'));}});res.on('end',()=>resolve(s));res.on('error',reject);});req.on('error',reject);req.end(JSON.stringify(payload));});
 const envelope=strictJSON(raw);receipt.elapsedMs=Date.now()-started;receipt.reportedModel=envelope.model;receipt.usage=envelope.usage??null;
 if(!Array.isArray(envelope.choices)||envelope.choices.length!==1)throw Error('CHOICES_REFUSED');const c=envelope.choices[0];receipt.finishReason=c.finish_reason;
 if(c.finish_reason!=='stop'||envelope.model!==cfg.model||c.message?.tool_calls?.length||c.message?.refusal)throw Error('FINISH_OR_MODEL_REFUSED');
 const content=c.message?.content;if(typeof content!=='string')throw Error('CONTENT_REFUSED');strictJSON(content);
 const candidate={files:[{path:'answer.json',content}]};receipt.status='RESPONSE_VALIDATED';
 return {candidate,evidence:{...binding,candidateFingerprint:createCandidate(candidate,{authorId:author.id}).fingerprint,note:'Actual local model output; host did not supply or correct its answer.'}};
}};
const valid=c=>{try{const o=strictJSON(c.files[0].content);return c.files.length===1&&c.files[0].path==='answer.json'&&Object.keys(o).length===1&&typeof o.color==='string'&&o.color==='red';}catch{return false;}};
const finding={code:'EXTRACTION_INCORRECT',message:'Expected exactly the color stated in the input sentence.'};
const task={taskId:'agiw.preview.tiny-extraction',mode:'edit',language:'json',allowedFiles:['answer.json'],protectedFiles:[],protectedSnapshots:{},acceptanceCriteria:['Exactly one string color field matching the car color in the input sentence.'],policy:{repairBudget:0,totalDeadlineMs:10000,requiredReviewers:1,requireReportStore:true}};
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
const report=await runWorkflow(task,{adapters:{author,authorizeContext:{authorize:({binding})=>({status:'PASS',evidence:{...binding,reason:''}})},staticChecks:{check({candidate,binding}){let ok;try{const o=strictJSON(candidate.files[0].content);ok=Object.keys(o).length===1&&typeof o.color==='string';}catch{ok=false;}return {status:ok?'PASS':'FAIL',evidence:{...binding,findings:ok?[]:[finding],reason:ok?'':'Invalid JSON shape.'}};}},tests:{run({candidate,binding}){const ok=valid(candidate);return {status:ok?'PASS':'FAIL',evidence:{...binding,assertionsExecuted:1,assertionsPassed:ok?1:0,failures:ok?[]:[finding],reason:ok?'':'Incorrect answer.'}};}},reviewers:[{id:'preview.deterministic-extraction-check',review({candidate,checks,tests,binding,reviewerId}){const ok=valid(candidate)&&checks.status==='PASS'&&tests.status==='PASS';return {status:ok?'PASS':'FAIL',evidence:{...binding,reviewerId,findings:ok?[]:[finding],reason:ok?'':'Rejected candidate.',summary:'Deterministic extraction check, not independent LLM review.'}};}}]},reportStore});
console.log(JSON.stringify({mode:'live',nisiVersion:'0.3.1',route:cfg.route,independentLLMReview:false,modelCalls:receipts.length,report,reportStorePath,authorReceipts:receipts}));process.exitCode=report.outcome==='COMPLETED'?0:1;
