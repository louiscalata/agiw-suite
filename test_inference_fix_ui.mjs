import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const html=readFileSync(new URL('./web/index.html',import.meta.url),'utf8');
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');

test('evaluation navigation and repair compatibility hooks expose separate reviewed scopes',()=>{
  assert.match(html,/<a[^>]*href="\/eci-tuning"[^>]*>Model evaluation<\/a>/);
  assert.match(html,/<button id="fixInference"[^>]*aria-expanded="false"[^>]*aria-controls="fixInferenceDetails"/);
  assert.match(html,/<span class="fix-tab-long">Model tuning<\/span>/);
  assert.match(html,/<section id="fixInferenceDetails"[^>]*hidden[^>]*aria-labelledby="fixInferenceHeading"/);
  const select=html.match(/<select id="fixInferenceScope"[\s\S]*?<\/select>/);
  assert.ok(select,'repair target is selected before review');
  assert.deepEqual([...select[0].matchAll(/<option value="([^"]+)"/g)].map(match=>match[1]),['local','route','nisi']);
  assert.match(html,/<button id="fixInferenceSubmit"[^>]*type="submit">Review repair[^<]*<\/button>/);
  assert.match(html,/href="\/eci-tuning">recorded ECI rankings/);
  assert.match(html,/SharedChami[^<]*manual|manual[^<]*SharedChami/i);
});

test('repair toolbar opens passively and form opens a selected-scope review without submitting',()=>{
  for(const [id,event] of [['fixInference','click'],['fixInferenceForm','submit']]){
    const source=app.match(new RegExp(`\\$\\('${id}'\\)\\.addEventListener\\('${event}',[\\s\\S]*?\\n\\}\\);`));
    assert.ok(source,id);
    let listener,prevented=false;
    const sent=[];
    const $=key=>key==='fixInferenceScope'?{value:'nisi'}:{addEventListener:(_,callback)=>{listener=callback;}};
    new Function('$','openFixInference','reviewOnlineCodeAction','requestOnlineCodeAction',source[0])($,()=>sent.push('opened'),action=>sent.push(action),()=>assert.fail('form must not submit a repair'));
    listener({preventDefault(){prevented=true;}});
    assert.deepEqual(sent,event==='click'?['opened']:['fix-nisi']);
    assert.equal(prevented,event==='submit');
  }
});

test('inspector opening is read-only and focuses the panel',()=>{
  const opener=app.match(/\nfunction openFixInference\(scope\)\{\n[\s\S]*?\n\}\n/);
  assert.ok(opener);
  assert.doesNotMatch(opener[0],/\bfetch\s*\(|\brequestOnlineCodeAction\s*\(|requestSubmit|\.submit\s*\(|\.click\s*\(/);
  assert.match(opener[0],/panel\.focus\(\{preventScroll:true\}\)/);
  assert.match(html,/Opening this panel[^<]*no repair request/);
});

test('repair submission requires review and preserves pending-operation guards',()=>{
  assert.match(app,/async function requestOnlineCodeAction\(action,\{reviewed=false\}=\{\}\)/);
  assert.match(app,/onlineCodeActionPosting\|\|\['running','uncertain'\]\.includes\(onlineCodeActionStatus\?\.status\)/);
  assert.match(app,/const path=fixAction\?'\/api\/eci-tuning\/repair'/);
  assert.match(app,/scope:action\.slice\(4\),reviewed:true/);
  assert.match(app,/submit\.disabled=!macOwner\|\|active/);
  assert.doesNotMatch(html,/force-unmount and remount/);
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

test('rankings page opens the repair panel through a read-only deep link',()=>{
  const page=readFileSync(new URL('./web/eci-tuning.html',import.meta.url),'utf8');
  assert.match(page,/href="\/#model-tuning"/);
  assert.match(page,/Confirm action/);
  const helper=app.match(/function openTuningLink\(\)\{[^\n]+/)[0];
  for(const hash of ['', '#other', '#model-tuning']){
    const opened=[];
    new Function('window','openFixInference',`${helper};openTuningLink();`)({location:{hash}},()=>opened.push('opened'));
    assert.equal(opened.length,hash==='#model-tuning'?1:0);
  }
  assert.doesNotMatch(helper,/requestOnlineCodeAction|fetch|submit/);
  assert.match(app,/window\.addEventListener\('hashchange',openTuningLink\);\nopenTuningLink\(\);/);
});
