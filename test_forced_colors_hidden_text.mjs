import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
// Execute only the actual text audit. Never load Playwright or start the observer.
const source=readFileSync('checks/forced-colors-emulation.mjs','utf8');
const functionSource=source.slice(source.indexOf('async function hiddenText('),source.indexOf('\nconst pages ='));
function element(tagName, {parent = null, open = false, painted = true, style = {}} = {}) {
  const node = {tagName, parentElement:parent, children:[], open,
    namespaceURI:'http://www.w3.org/1999/xhtml',
    style:{display:'block',opacity:'1',visibility:'visible',color:'CanvasText',...style},
    matches(selector) {return selector === 'details:not([open])' && this.tagName === 'DETAILS' && !this.open;},
    contains(other) {for (; other; other = other.parentElement) if (other === this) return true; return false;},
    closest(selector) {for (let other = this; other; other = other.parentElement) {
      if (selector.includes('dialog:not([open])') && other.tagName === 'DIALOG' && !other.open) return other;
    } return null;},
    getBoundingClientRect:()=>({width:painted?100:0,height:painted?20:0})};
  parent?.children.push(node);
  return node;
}
function auditNodes(nodes) {
  nodes = [...nodes];
  const document={body:{},createTreeWalker:()=>({nextNode:()=>nodes.shift()})};
  return runInNewContext(functionSource+'; hiddenText({evaluate:fn=>fn()})',{document,NodeFilter:{SHOW_TEXT:4},getComputedStyle:node=>node.style});
}
function audit(open,painted) {
  const parent=element('DIALOG',{open,painted});
  return auditNodes(['Find a command','Close'].map(textContent=>({textContent,parentElement:parent})));
}
test('closed command dialog text is excluded from the forced-colors audit',async()=>{
  assert.equal((await audit(false,false)).length,0);
});
test('open command dialog text still fails when unpainted and passes when painted',async()=>{
  assert.deepEqual(Array.from(await audit(true,false)),['Find a command','Close']);
  assert.equal((await audit(true,true)).length,0);
});

test('closed details retain their summary audit and opened details audit all body text',async()=>{
  const details=element('DETAILS');
  const summary=element('SUMMARY',{parent:details});
  const summaryText=element('SPAN',{parent:summary,style:{color:'transparent'}});
  const body=element('DIV',{parent:details,painted:false});
  const nodes=[{textContent:'Show map',parentElement:summaryText},{textContent:'Lane details',parentElement:body}];
  assert.deepEqual(Array.from(await auditNodes(nodes)),['Show map']);
  summaryText.style.color='CanvasText';
  assert.equal((await auditNodes(nodes)).length,0);
  details.open=true;
  assert.deepEqual(Array.from(await auditNodes(nodes)),['Lane details']);
  body.getBoundingClientRect=()=>({width:100,height:20});
  assert.equal((await auditNodes(nodes)).length,0);
  body.style.color='transparent';
  assert.deepEqual(Array.from(await auditNodes(nodes)),['Lane details']);
});

test('nested closed disclosures suppress inner summaries but never later non-summary children',async()=>{
  const outer=element('DETAILS');
  element('SUMMARY',{parent:outer});
  const inner=element('DETAILS',{parent:outer});
  const summary=element('SUMMARY',{parent:inner,painted:false});
  const later=element('SUMMARY',{parent:inner,painted:false});
  const nodes=[{textContent:'Inner summary',parentElement:summary},{textContent:'Later summary',parentElement:later}];
  assert.equal((await auditNodes(nodes)).length,0);
  outer.open=true;
  assert.deepEqual(Array.from(await auditNodes(nodes)),['Inner summary']);
});

test('CSS-suppressed ancestors are excluded while visible text with hidden visibility still fails',async()=>{
  const parent=element('DIV',{style:{display:'none'}});
  const child=element('SPAN',{parent,painted:false});
  const nodes=[{textContent:'Optional controls',parentElement:child}];
  assert.equal((await auditNodes(nodes)).length,0);
  parent.style.display='block';
  child.style.visibility='hidden';
  assert.deepEqual(Array.from(await auditNodes(nodes)),['Optional controls']);
});
