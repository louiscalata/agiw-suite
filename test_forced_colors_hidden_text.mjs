import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
// Execute only the actual text audit. Never load Playwright or start the observer.
const source=readFileSync('checks/forced-colors-emulation.mjs','utf8');
const functionSource=source.slice(source.indexOf('async function hiddenText('),source.indexOf('\nconst pages ='));
function audit(open,painted) {
  const parent={namespaceURI:'http://www.w3.org/1999/xhtml',closest:selector=>selector.split(',').some(part=>part.trim()==='dialog'||(!open&&part.trim()==='dialog:not([open])')),getBoundingClientRect:()=>({width:painted?100:0,height:painted?20:0})};
  const nodes=['Find a command','Close'].map(textContent=>({textContent,parentElement:parent}));
  const document={body:{},createTreeWalker:()=>({nextNode:()=>nodes.shift()})};
  return runInNewContext(functionSource+'; hiddenText({evaluate:fn=>fn()})',{document,NodeFilter:{SHOW_TEXT:4},getComputedStyle:()=>({display:'block',opacity:'1',visibility:'visible',color:'CanvasText'})});
}
test('closed command dialog text is excluded from the forced-colors audit',async()=>{
  assert.equal((await audit(false,false)).length,0);
});
test('open command dialog text still fails when unpainted and passes when painted',async()=>{
  assert.deepEqual(Array.from(await audit(true,false)),['Find a command','Close']);
  assert.equal((await audit(true,true)).length,0);
});
