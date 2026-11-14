import assert from 'node:assert/strict';
import test from 'node:test';
import { modelControlView, submitModelControl } from './web/model-control-view.mjs';

const base={id:'vendor/model-q4',host:'mac',source:'lmstudio-api',state:'unloaded',loaded:false,modelKey:'vendor/model-q4'};
const context={host:'mac',feedFresh:true,supported:true};

test('load is enabled only for a fresh, supported, exact local unloaded inventory row',()=>{
  assert.equal(modelControlView(base,context).action,'load');
  assert.equal(modelControlView({...base,modelKey:'other'},context).action,null);
  assert.equal(modelControlView({...base,host:'windows'},context).action,null);
  assert.equal(modelControlView(base,{...context,feedFresh:false}).action,null);
  assert.equal(modelControlView(base,{...context,supported:false,reason:'CLI missing'}).reason,'CLI missing');
  assert.equal(modelControlView({...base,source:'amd-models'},context).action,null);
  assert.equal(modelControlView({...base,state:'busy',loaded:true},context).action,null);
});

test('unload needs one exact idle instance and no queued work',()=>{
  const model={...base,source:'lms-ps',state:'idle',loaded:true,queued:0,modelKey:base.id,instanceId:base.id,loadedInstanceIds:[base.id]};
  assert.equal(modelControlView(model,{...context,models:[model]}).action,'unload');
  for(const changed of [
    {...model,instanceId:'another'},
    {...model,loadedInstanceIds:[]},
    {...model,queued:1},
    {...model,state:'generating'},
    {...model,source:'amd-models'},
  ]) assert.equal(modelControlView(changed,{...context,models:[changed]}).action,null);
  assert.equal(modelControlView({...model,modelKey:'another'},{...context,models:[model]}).action,null);
  assert.equal(modelControlView({...model,loadedInstanceIds:[base.id,'extra']},{...context,models:[{...model,loadedInstanceIds:[base.id,'extra']}]}).action,'unload');
  assert.equal(modelControlView(model,{...context,models:[model],status:{status:'running',action:'load'}}).action,null);
  assert.equal(modelControlView(model,{...context,models:[model,model]}).action,null);
});

test('a loaded alias uses one API instance list to enable exact unload',()=>{
  const api={...base,loaded:true,state:'loaded',loadedInstanceIds:['alias-1']};
  const alias={...base,id:'alias-1',source:'lms-ps',state:'idle',loaded:true,queued:0,instanceId:'alias-1',loadedInstanceIds:null};
  assert.equal(modelControlView(alias,{...context,models:[api,alias]}).action,'unload');
  assert.equal(modelControlView(alias,{...context,models:[{...api,loadedInstanceIds:[]},alias]}).action,null);
});

test('selected-model POST is mocked and carries only the exact requested action and ID',async()=>{
  let seen;
  const result=await submitModelControl(async (url,options)=>{
    seen={url,options};
    return {ok:true,status:202,json:async()=>({status:'running',action:'load',modelId:base.id,operationId:'op-1'})};
  },'load',base.id);
  assert.equal(seen.url,'/api/models/control');
  assert.equal(seen.options.method,'POST');
  assert.equal(seen.options.headers['Content-Type'],'application/json');
  assert.deepEqual(JSON.parse(seen.options.body),{action:'load',modelId:base.id});
  assert.deepEqual(result,{ok:true,statusCode:202,outcome:'accepted',result:{status:'running',action:'load',modelId:base.id,operationId:'op-1'}});
});

test('mocked rejected POST preserves server feedback for the caller',async()=>{
  const result=await submitModelControl(async()=>({ok:false,status:503,json:async()=>({status:'error',message:'Model inventory changed'})}),'unload',base.id);
  assert.equal(result.ok,false);
  assert.equal(result.statusCode,503);
  assert.equal(result.result.message,'Model inventory changed');
});

test('a model-control POST with no answer is reported as an unknown outcome, never as unsent',async()=>{
  for(const lost of [async()=>{throw new TypeError('Load failed');},async()=>{throw new DOMException('The operation timed out.','TimeoutError');}]){
    let calls=0;
    const result=await submitModelControl(async(...args)=>{calls++;return lost(...args);},'load',base.id);
    assert.equal(calls,1,'one POST, no retry');
    assert.equal(result.ok,false);
    assert.equal(result.outcome,'unknown');
    assert.equal(result.statusCode,null);
    assert.match(result.result.message,/outcome unknown/);
    assert.match(result.result.message,/may or may not have taken effect/);
    assert.doesNotMatch(result.result.message,/could not|rejected|did not/i);
  }
});

test('an accepted POST with an unreadable answer stays unknown; an answered refusal is a refusal',async()=>{
  const unreadable=await submitModelControl(async()=>({ok:true,status:202,json:async()=>{throw new SyntaxError('bad json');}}),'load',base.id);
  assert.equal(unreadable.outcome,'unknown');
  assert.match(unreadable.result.message,/outcome unknown/);
  const refused=await submitModelControl(async()=>({ok:false,status:423,json:async()=>({status:'refused',message:'L2 is closed'})}),'load',base.id);
  assert.equal(refused.outcome,'refused');
  assert.equal(refused.result.message,'L2 is closed');
  const silent=await submitModelControl(async()=>({ok:false,status:503,json:async()=>{throw new SyntaxError('empty');}}),'load',base.id);
  assert.equal(silent.outcome,'refused');
  assert.equal(silent.statusCode,503);
});

test('the Monitor does not describe a lost model-control answer as unreachable',async()=>{
  const {readFileSync}=await import('node:fs');
  const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
  assert.doesNotMatch(app,/Could not reach local model control/);
});
