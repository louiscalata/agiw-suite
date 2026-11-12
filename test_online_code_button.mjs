import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const html=readFileSync(new URL('./web/index.html',import.meta.url),'utf8');
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');

test('opening readiness only reads cached readiness and retained action status',()=>{
  assert.match(html,/<button id="onlineCodeMode"[^>]*type="button"/);
  const click=app.match(/\$\('onlineCodeMode'\)\.addEventListener\('click',\(\)=>\{[\s\S]*?\n\}\);/);
  assert.ok(click,'readiness click handler exists');
  assert.doesNotMatch(click[0],/requestOnlineCodeAction|reviewOnlineCodeAction|method:.*POST/);
  assert.match(click[0],/readCachedReadiness\(\)/);
  assert.match(click[0],/pollOnlineCodeAction\(\)/);
  assert.match(app,/fetch\('\/api\/online-code-mode\/readiness',\{method:'GET'/);
});

test('reviewed requests retain the single-operation and uncertain-status guards',()=>{
  assert.match(app,/onlineCodeActionPosting\|\|\['running','uncertain'\]\.includes\(onlineCodeActionStatus\?\.status\)/);
  assert.match(app,/The request may have reached the local monitor\. Checking its status; no automatic retry will occur/);
  assert.match(app,/async function requestOnlineCodeAction\(action,\{reviewed=false\}=\{\}\)/);
  assert.match(app,/scope:action\.slice\(4\),reviewed:true/);
});
