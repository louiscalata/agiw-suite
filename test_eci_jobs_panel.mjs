import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import {jobsView, recordFacts, rejectedView, render} from './web/eci-tuning.mjs';

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
const section = (root, title) => root.children.find(node => node.tag === 'section' && node.children[0].textContent === title);

// The observer's answer with no engine (server.py, CHECK_JOBS_NOT_CONFIGURED).
const unconfigured = {status:'unknown', reason:'Check jobs are not configured.', owner:false, gate:'closed', jobs:[], active:null};
// An engine status after a restart: one job left interrupted, one finished with a record.
const configured = {status:'ok', reason:null, owner:true, gate:'closed', active:null, jobs:[
  {job_id:'a'.repeat(32), state:'interrupted', attempt:1, request:{target:'registry:pc:gemma', model_id:'google/gemma-4-12b-it', sample_set:'screening'}, reason:'Engine restarted while the job was running.', result:null},
  {job_id:'b'.repeat(32), state:'finished', attempt:2, request:{target:'registry:pc:ornith', model_id:'ornith-ai/ornith-1.5-9b', sample_set:'screening'}, reason:null, result:{record_id:'fc-fixture-1', post_decision:'ADMIT', benchmarks:{}}}
]};

test('unconfigured observer shows not configured, gate closed and no jobs', () => {
  const view = jobsView({http:200, data:unconfigured});
  assert.equal(view.message, 'Check jobs are not configured.');
  assert.deepEqual(view.facts, [['Status','unknown'],['Gate','closed'],['Engine owner','no'],['Reason','Check jobs are not configured.'],['Active job','none recorded']]);
  assert.deepEqual(view.rows, []);
});

test('configured status lists each job with target, model, sample set, state, attempt and record id', () => {
  const view = jobsView({http:200, data:configured});
  assert.equal(view.message, null);
  assert.deepEqual(view.facts[0], ['Status','ok']);
  assert.deepEqual(view.facts[2], ['Engine owner','yes']);
  assert.deepEqual(view.rows[0], ['a'.repeat(32),'registry:pc:gemma','google/gemma-4-12b-it','screening','interrupted','1','unknown','Engine restarted while the job was running.']);
  assert.deepEqual(view.rows[1], ['b'.repeat(32),'registry:pc:ornith','ornith-ai/ornith-1.5-9b','screening','finished','2','fc-fixture-1','unknown']);
  const running = jobsView({http:200, data:{...configured, active:'a'.repeat(32)}});
  assert.deepEqual(running.facts[4], ['Active job','a'.repeat(32)]);
});

test('a 404 from the Windows observer and any other failure stay unknown', () => {
  const windows = jobsView({http:404, data:null});
  assert.equal(windows.message, 'Check jobs are not available on this observer.');
  assert.equal(windows.facts[0][1], 'unknown');
  assert.deepEqual(windows.rows, []);
  for (const answer of [{http:null, data:null}, {http:503, data:{status:'unknown'}}, {http:200, data:null}, {http:200, data:[]}, undefined]) {
    const view = jobsView(answer);
    assert.equal(view.message, 'Check jobs unavailable. Status unknown.');
    assert.deepEqual(view.rows, []);
  }
  // A malformed job list is not shown as an empty queue.
  assert.equal(jobsView({http:200, data:{...configured, jobs:'none'}}).message, 'Job list unknown.');
});

test('rejected fast checks are listed when sent and unknown when the key is absent', () => {
  assert.equal(rejectedView({}), null);
  assert.equal(rejectedView({rejected_fast_checks:'x'}), null);
  assert.deepEqual(rejectedView({rejected_fast_checks:[]}), []);
  assert.deepEqual(rejectedView({rejected_fast_checks:[{record_id:'fc-bad', reason:'Record failed validation.'}, {reason:null}]}),
    [['fc-bad','Record failed validation.'],['unknown','unknown']]);
});

test('screening rows show wall time, quantization, size match and journal, unknown when not sent', () => {
  assert.deepEqual(recordFacts({record_id:'r1', wall_time_s:12.5, quantization:'Q4_K_M', provenance:{size_match:'equal'}, journal:'nisi:journal:abc'}),
    ['r1','12.50','Q4_K_M','equal','nisi:journal:abc']);
  assert.deepEqual(recordFacts({record_id:'r2'}), ['r2','unknown','unknown','unknown','unknown']);
  assert.deepEqual(recordFacts({record_id:'r3', wall_time_s:'12', provenance:'equal'}), ['r3','unknown','unknown','unknown','unknown']);
});

test('the page renders the jobs section, rejected list and record facts as text', () => {
  const doc = fixtureDocument();
  globalThis.document = doc;
  try {
    const root = doc.createElement('div');
    const group = {hardware:{id:'fixture'}, plan_id:'plan', note:'', rows:[{record_id:'r1', model:'m', wall_time_s:3, quantization:'Q4_K_M', provenance:{size_match:'unverified'}, journal:null}], pending:[{record_id:'p1', model:'n'}]};
    render({dataset:{status:'configured'}, rankings:{fast_check:{hardware_views:[group]}}}, root, {http:200, data:configured});
    const jobs = section(root, 'Check jobs');
    assert.match(jobs.textContent, /read-only/);
    assert.match(jobs.textContent, /interrupted.*fc-fixture-1/s);
    assert.match(section(root, 'Rejected fast checks').textContent, /unknown/);
    const screen = root.children.find(node => node.tag === 'section' && /Screening results/.test(node.children[0].textContent));
    assert.match(screen.textContent, /r1 3\.00 Q4_K_M unverified unknown/);
    assert.match(screen.textContent, /p1 unknown unknown unknown unknown/);

    render({dataset:{status:'configured'}, rejected_fast_checks:[{record_id:'fc-bad', reason:'Record failed validation.'}]}, root, {http:404, data:null});
    assert.match(section(root, 'Check jobs').textContent, /not available on this observer/);
    assert.match(section(root, 'Rejected fast checks').textContent, /fc-bad Record failed validation\./);

    render({dataset:{status:'configured'}, rejected_fast_checks:[]}, root, {http:200, data:unconfigured});
    assert.match(section(root, 'Check jobs').textContent, /Check jobs are not configured\./);
    assert.match(section(root, 'Rejected fast checks').textContent, /No rejected fast checks recorded\./);

    // The server caps the list and says so in rejected_fast_checks_note; the page shows that note.
    const note = '40 fast-check records failed validation and are not ranked. Showing the first 32.';
    render({dataset:{status:'configured'}, rejected_fast_checks:[{record_id:'fc-bad', reason:'Record failed validation.'}], rejected_fast_checks_note:note}, root, {http:200, data:unconfigured});
    assert.match(section(root, 'Rejected fast checks').textContent, /fc-bad Record failed validation\..*Showing the first 32\./s);
    render({dataset:{status:'configured'}, rejected_fast_checks:[], rejected_fast_checks_note:7}, root, {http:200, data:unconfigured});
    assert.equal(section(root, 'Rejected fast checks').children.length, 2);

    // An unknown dataset still shows the check-job status.
    render({dataset:{status:'unknown', reason:'Configured dataset.json was not found.'}}, root, {http:200, data:unconfigured});
    assert.match(root.textContent, /Dataset unknown/);
    assert.match(section(root, 'Check jobs').textContent, /not configured/);
  } finally { delete globalThis.document; }
});

test('the page reads /api/eci-jobs once with GET and offers no action', async () => {
  const source = readFileSync(new URL('./web/eci-tuning.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /method:|['"]button['"]|<button|\/api\/eci-jobs\/(preview|jobs)/);
  const doc = fixtureDocument();
  const nodes = {tuningStatus:{textContent:''}, tuningEvidence:doc.createElement('main')};
  const requests = [];
  const context = {document:{...doc, getElementById:id => nodes[id]}, setTimeout:() => {},
    fetch:async (url, options) => {
      requests.push([url, options]);
      if (url === '/api/eci-jobs') return {ok:false, status:404, json:async () => ({})};
      return {ok:true, status:200, json:async () => ({dataset:{status:'configured'}, policy:{origin:'saved', routes:{}}})};
    }};
  vm.runInNewContext(source.replaceAll('export ', ''), context);
  for (let step = 0; step < 5; step++) await new Promise(setImmediate);
  assert.deepEqual(requests.map(([url]) => url).sort(), ['/api/eci-jobs', '/api/eci-tuning']);
  assert.ok(requests.every(([, options]) => options?.method === undefined));
  assert.match(nodes.tuningEvidence.textContent, /Check jobs.*not available on this observer/s);
});

// Runs the browser block with an /api/eci-jobs fetch that answers as jobsFetch says; timers are collected, not run.
async function runPage(jobsFetch, dataset = {status:'configured'}) {
  const source = readFileSync(new URL('./web/eci-tuning.mjs', import.meta.url), 'utf8');
  const doc = fixtureDocument();
  const nodes = {tuningStatus:{textContent:''}, tuningEvidence:doc.createElement('main')};
  const timers = [];
  const context = {document:{...doc, getElementById:id => nodes[id]}, setTimeout:(fn, ms) => { timers.push([fn, ms]); },
    fetch:async url => url === '/api/eci-jobs' ? jobsFetch() : {ok:true, status:200, json:async () => ({dataset, policy:{origin:'saved', routes:{}}})}};
  vm.runInNewContext(source.replaceAll('export ', ''), context);
  const settle = async () => { for (let step = 0; step < 5; step++) await new Promise(setImmediate); };
  await settle();
  return {nodes, timers, settle};
}

test('a rejected jobs fetch leaves the scores rendered and the jobs status unknown', async () => {
  const {nodes} = await runPage(() => Promise.reject(new TypeError('network down')));
  assert.equal(nodes.tuningStatus.textContent, 'Recorded scores loaded. No live inference tested.');
  assert.match(nodes.tuningEvidence.textContent, /Pending scores/);
  assert.match(nodes.tuningEvidence.textContent, /Check jobs.*Check jobs unavailable\. Status unknown\./s);
});

test('a hanging jobs fetch does not delay the scores and reads unknown after the bounded wait', async () => {
  const {nodes, timers, settle} = await runPage(() => new Promise(() => {}));
  assert.equal(nodes.tuningStatus.textContent, 'Recorded scores loaded. No live inference tested.');
  assert.match(nodes.tuningEvidence.textContent, /Pending scores/);
  assert.doesNotMatch(nodes.tuningEvidence.textContent, /Check jobs/);
  const wait = timers.filter(([, ms]) => ms === 10000);
  assert.equal(wait.length, 1);
  wait[0][0]();
  await settle();
  assert.match(nodes.tuningEvidence.textContent, /Pending scores.*Check jobs.*Check jobs unavailable\. Status unknown\./s);
  assert.equal(nodes.tuningEvidence.children.filter(node => node.children[0]?.textContent === 'Check jobs').length, 1);
});

test('a dataset retry re-renders the scores and keeps the jobs section that already arrived', async () => {
  const {nodes, timers, settle} = await runPage(async () => ({ok:true, status:200, json:async () => unconfigured}), {status:'configured', pending:true});
  assert.match(nodes.tuningEvidence.textContent, /Check jobs are not configured\./);
  const retry = timers.filter(([, ms]) => ms === 1000);
  assert.equal(retry.length, 1);
  retry[0][0]();
  await settle();
  assert.equal(nodes.tuningEvidence.children.filter(node => node.children[0]?.textContent === 'Check jobs').length, 1);
  assert.match(nodes.tuningEvidence.textContent, /Check jobs are not configured\./);
});
