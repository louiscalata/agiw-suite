import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const html=readFileSync(new URL('./web/index.html',import.meta.url),'utf8');
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');

test('Model Tuning Tool retains accessible compatibility hooks without scope choices',()=>{
  assert.match(html,/<button id="fixInference"[^>]*aria-expanded="false"[^>]*aria-controls="fixInferenceDetails"/);
  assert.match(html,/<section id="fixInferenceDetails"[^>]*hidden[^>]*aria-labelledby="fixInferenceHeading"/);
  assert.doesNotMatch(html,/name="fixScope"|id="fixInferenceScopes"/);
  assert.match(html,/<button id="fixInferenceSubmit"[^>]*type="submit">Run check<\/button>/);
  assert.match(html,/Mac runtime, Windows route and Nisi Inference in one guarded run/);
  assert.match(html,/<h2 id="fixInferenceHeading">AGIW x ECI Model Tuning Tool<\/h2>/);
  assert.match(html,/href="\/eci-tuning">recorded ECI rankings/);
  assert.match(html,/Check and repair inference/);
});

test('toolbar opens without repair and only explicit Run check submits',()=>{
  for(const [id,event] of [['fixInference','click'],['fixInferenceForm','submit']]){
    const source=app.match(new RegExp(`\\$\\('${id}'\\)\\.addEventListener\\('${event}',[\\s\\S]*?\\n\\}\\);`));
    assert.ok(source,id);
    let listener,prevented=false;
    const sent=[];
    const $=()=>({addEventListener:(_,callback)=>{listener=callback;}});
    new Function('$','openFixInference','requestOnlineCodeAction',source[0])($,()=>sent.push('opened'),action=>sent.push(action));
    listener({preventDefault(){prevented=true;}});
    assert.deepEqual(sent,event==='click'?['opened']:['fix-all']);
    assert.equal(prevented,event==='submit');
  }
});

test('inspector opening is read-only and focuses the panel',()=>{
  const opener=app.match(/\nfunction openFixInference\(scope\)\{\n[\s\S]*?\n\}\n/);
  assert.ok(opener);
  assert.doesNotMatch(opener[0],/\bfetch\s*\(|\brequestOnlineCodeAction\s*\(|requestSubmit|\.submit\s*\(|\.click\s*\(/);
  assert.match(opener[0],/panel\.focus\(\{preventScroll:true\}\)/);
  assert.match(html,/Opening this panel from the toolbar, inspector or tuning page makes no repair request/);
});

test('unified fix uses the same guarded endpoint and preserves legacy actions',()=>{
  for(const action of ['fix-local','fix-route','fix-both','fix-nisi','fix-all'])assert.ok(app.includes(`['${action}',`));
  assert.match(app,/onlineCodeActionPosting\|\|\['running','uncertain'\]\.includes\(onlineCodeActionStatus\?\.status\)/);
  assert.match(app,/const path=fixAction\?'\/api\/eci-tuning\/repair'/);
  assert.match(app,/const body=fixAction\?\{scope:action\.slice\(4\)\}:\{action\}/);
  assert.match(app,/submit\.disabled=!macOwner\|\|active/);
  assert.match(html,/force-unmount and remount a stuck or missing SharedChami share/);
});

test('Fix panel renders every step and states when no Windows request was sent',()=>{
  const helpers=app.match(/function fixScopeNote[\s\S]*?\nfunction stepText[^\n]*\n/)[0];
  const {fixScopeNote,stepText}=new Function(`${helpers};return {fixScopeNote,stepText};`)();
  const deferred=[{name:'share-recovery',result:'deferred'}];
  assert.equal(fixScopeNote('fix-route','needs-action',deferred),' No Windows model request was sent.');
  assert.equal(fixScopeNote('fix-route','running',deferred),'');
  assert.equal(fixScopeNote('fix-both','ready',[{name:'windows-inference',result:'verified'}]),'');
  assert.equal(fixScopeNote('fix-route','needs-action',[{name:'windows-inference',result:'not-run'}]),' No Windows model request was sent.');
  assert.equal(fixScopeNote('fix-local','ready',[]),' Model generation was not probed.');
  assert.equal(stepText({name:'windows-inference',result:'unresolved',evidence:'code=TIMEOUT',jobId:'mac-x'}),'windows-inference · unresolved · code=TIMEOUT · mac-x');
  assert.doesNotMatch(app,/steps\.slice\(0,(8|16)\)/);
});

test('rankings page opens the same check panel through a read-only deep link',()=>{
  const page=readFileSync(new URL('./web/eci-tuning.html',import.meta.url),'utf8');
  assert.match(page,/href="\/#model-tuning"/);
  assert.match(page,/Run check/);
  const helper=app.match(/function openTuningLink\(\)\{[^\n]+/)[0];
  for(const hash of ['', '#other', '#model-tuning']){
    const opened=[];
    new Function('window','openFixInference',`${helper};openTuningLink();`)({location:{hash}},()=>opened.push('opened'));
    assert.equal(opened.length,hash==='#model-tuning'?1:0);
  }
  assert.doesNotMatch(helper,/requestOnlineCodeAction|fetch|submit/);
  assert.match(app,/window\.addEventListener\('hashchange',openTuningLink\);\nopenTuningLink\(\);/);
});
