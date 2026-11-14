import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {render, viewModel} from './web/eci-tuning.mjs';

function fixtureDocument() {
  class Node {
    constructor(tag) { this.tag = tag; this.children = []; this.value = ''; }
    set textContent(value) { this.value = String(value); this.children = []; }
    get textContent() { return this.value + this.children.map(child => child.textContent).join(' '); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.value = ''; this.children = children; }
  }
  return {createElement: tag => new Node(tag)};
}


test('cost and latency preserve run identity for repeated models', () => {
  const group = {eci:[], notes:[], components:{}, unknown_latency:[], unknown_cost:[],
    latency:['run-a','run-b'].map(run_id => ({run_id, model:'same-model', latency_s:1, status:'COMPUTED', measurement:{basis:'recorded',n_items:2,n_measured:2}}))};
  const view = viewModel({rankings:{baseline_protocol:group}});
  assert.deepEqual(view.sections[0].performance.map(row => row.slice(0,3)), [['latency','run-a','same-model'],['latency','run-b','same-model']]);
});

test('pair barriers retain descriptive overlap without claiming a winner', () => {
  const group = {eci:[], notes:[], components:{}, unknown_latency:[], unknown_cost:[], comparisons:[{
    left_run_id:'run-a',right_run_id:'run-b',comparable:false,reasons:['machine differs'],interval_relation:'overlap',uncertainty:'Not comparable; no winner established.'}]};
  const previous = globalThis.document;
  globalThis.document = fixtureDocument();
  try {
    const root = document.createElement('main');
    render({rankings:{baseline_protocol:group}}, root);
    assert.match(root.textContent, /run-a.*run-b/);
    assert.match(root.textContent, /machine differs/);
    assert.match(root.textContent, /recorded 90% intervals overlap/);
    assert.match(root.textContent, /Not comparable; no winner established/);
  } finally { if(previous === undefined) delete globalThis.document; else globalThis.document = previous; }
});

test('source date and policy origin stay explicit and screening is never titled ECI', () => {
  const previous = globalThis.document;
  globalThis.document = fixtureDocument();
  try {
    const root = document.createElement('main');
    render({dataset:{status:'bundled',as_of:'2026-11-10'},policy:{origin:'bundled reference',routes:{general:{model:null,status:'NOT_COMPUTED'}}}}, root);
    assert.match(root.textContent,/bundled catalog.*snapshot 2026-11-10/);
    assert.match(root.textContent,/Policy reference/);
    assert.doesNotMatch(root.textContent,/use baseline ECI provisionally/);
    render({dataset:{status:'configured',as_of:null,ingested_at:'2026-11-13T00:00:00Z'},policy:{origin:'saved',routes:{}}}, root);
    assert.match(root.textContent,/snapshot date unknown/);
    assert.match(root.textContent,/ingested 2026-11-13T00:00:00Z/);
    assert.match(root.textContent,/Saved policy/);
  } finally { if(previous === undefined) delete globalThis.document; else globalThis.document = previous; }
  const screen = viewModel({rankings:{fast_check:{hardware_views:[{hardware:{},plan_id:'fixture',note:'',rows:[],pending:[]}]}}}).fastChecks[0];
  assert.doesNotMatch(screen.title,/ECI/);
});


test('missing and unreadable policy remain unknown rather than NOT_COMPUTED', () => {
  for (const data of [{}, {policy:{origin:'unreadable',reason:'Saved policy could not be read.',routes:{}}}]) {
    assert.match(viewModel(data).defaultNote,/Policy unknown/);
    assert.doesNotMatch(viewModel(data).defaultNote,/NOT_COMPUTED/);
  }
  assert.match(viewModel({policy:{origin:'bundled reference',routes:{general:{model:null,status:'NOT_COMPUTED'}}}}).defaultNote,/NOT_COMPUTED/);
});


test('pending dataset reads retry until ready with a strict attempt bound', async () => {
  const source = readFileSync(new URL('./web/eci-tuning.mjs',import.meta.url),'utf8').replaceAll('export ', '');
  for (const readyAfter of [2, Infinity]) {
    const nodes = {tuningStatus:{textContent:''}, tuningEvidence:fixtureDocument().createElement('main')};
    const timers = []; let calls = 0;
    const context = {document:{...fixtureDocument(),getElementById:id=>nodes[id]},
      fetch:async () => ({ok:true,json:async () => ++calls >= readyAfter ? {dataset:{status:'configured',as_of:null},policy:{origin:'bundled reference',routes:{general:{status:'NOT_COMPUTED'}}}} : {dataset:{status:'unknown',pending:true,reason:'Dataset validation is in progress; retry shortly.'}}}),
      setTimeout:callback => timers.push(callback)};
    vm.runInNewContext(source, context);
    for(let step=0;step<12;step++) {
      await new Promise(setImmediate);
      if(!timers.length) break;
      timers.shift()();
    }
    await new Promise(setImmediate);
    assert.equal(calls,readyAfter===2 ? 2 : 10);
    assert.equal(timers.length,0);
    if(readyAfter===2) {
      assert.match(nodes.tuningStatus.textContent,/Recorded scores loaded/);
      assert.match(nodes.tuningEvidence.textContent,/configured dataset/);
    } else {
      assert.match(nodes.tuningStatus.textContent,/Reload this page.*unknown/);
      assert.match(nodes.tuningEvidence.textContent,/Dataset unknown.*validation is in progress/);
    }
  }
});


test('saved policy with a missing source displays its reason', () => {
  const view = viewModel({policy:{origin:'saved',routes:{general:{model:'example-model',run_id:'missing',status:'UNKNOWN',basis:'Old selection basis',reason:'Policy source run not in the loaded dataset.'}}}});
  assert.match(view.policy[0][3], /source run not in the loaded dataset/);
});
