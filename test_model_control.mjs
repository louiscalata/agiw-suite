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
  assert.deepEqual(result,{ok:true,statusCode:202,result:{status:'running',action:'load',modelId:base.id,operationId:'op-1'}});
});

test('mocked rejected POST preserves server feedback for the caller',async()=>{
  const result=await submitModelControl(async()=>({ok:false,status:503,json:async()=>({status:'error',message:'Model inventory changed'})}),'unload',base.id);
  assert.equal(result.ok,false);
  assert.equal(result.statusCode,503);
  assert.equal(result.result.message,'Model inventory changed');
});
