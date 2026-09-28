import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const html=readFileSync(new URL('./web/index.html',import.meta.url),'utf8');
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');

test('one top-button click uses the existing bounded repair and readiness action',()=>{
  assert.match(html,/<button id="onlineCodeMode"[^>]*type="button"/);
  const click=app.match(/\$\('onlineCodeMode'\)\.addEventListener\('click',\(\)=>\{[\s\S]*?\n\}\);/);
  assert.ok(click,'top-button click handler exists');
  assert.match(click[0],/requestOnlineCodeAction\('check-and-repair'\)/);
  assert.doesNotMatch(click[0],/requestOnlineCodeAction\('readiness'\)|\bfetch\s*\(/);
  assert.match(app,/action==='readiness'\?'\/api\/online-code-mode\/entry':'\/api\/online-code-mode\/repair'/);
  assert.match(app,/const body=fixAction\?\{scope:action\.slice\(4\)\}:\{action\}/);
});

test('top-button request shares the single-operation and uncertain-status guard',()=>{
  assert.match(app,/onlineCodeActionPosting\|\|\['running','uncertain'\]\.includes\(onlineCodeActionStatus\?\.status\)/);
  assert.match(app,/The request may have reached the local monitor\. Checking its status; no automatic retry will occur/);
  assert.match(app,/Check exact pending records, safely reconcile eligible ones, and verify readiness/);
});
