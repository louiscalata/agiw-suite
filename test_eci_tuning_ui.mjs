import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {mkdtempSync, rmSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import test from 'node:test';
import {resolvePython} from './test_support/python-interpreter.mjs';
import {ci, metric, render, viewModel} from './web/eci-tuning.mjs';

test('CI formatting leaves missing values unknown', () => {
  assert.equal(ci({eci_posterior_mean:105.3759, eci_p05:94.31056, eci_p95:115.1379}), '105.38 [94.31, 115.14]');
  for (const value of [null, undefined, NaN, Infinity, '1']) assert.equal(metric(value), 'unknown');
});
test('comparison and policy provenance remain separate, with unknowns explicit', () => {
  const group = {eci:[{run_id:'a',model:'m',reason:'partial details',item_evidence:{observed:12,truncated:2,truncation_unknown:1,missing_parsed_answer:3}}], comparisons:[{left_run_id:'a',right_run_id:'b',comparable:false,reasons:['hardware profile differs'],uncertainty:'INCONCLUSIVE'}], notes:['No clean winner.'], components:{}, unknown_latency:['a'], unknown_cost:['a']};
  const view = viewModel({rankings:{baseline_protocol:group, pipeline_configured:group}, pending:[{model:'Pending', status:'NOT_COMPUTED'}], recommendation:{routes:{general:{model:'rec',run_id:'r1',basis:'test',status:'PROVISIONAL'}}}, policy:{origin:'saved',routes:{general:{model:'saved',run_id:'r2',basis:'selected'}}}, runtime_activation:{status:'NOT_APPLIED',source_run_id:null}});
  assert.equal(view.sections.length, 2);
  assert.match(view.sections[0].title, /Baseline/);
  assert.match(view.sections[1].title, /separate protocol/);
  assert.deepEqual(view.sections[0].evidence[0], ['a','12','2','1','3']);
  assert.equal(view.sections[0].rows[0][6], 'partial details');
  assert.deepEqual(view.sections[0].comparisons[0], ['a','b','Not comparable','hardware profile differs','INCONCLUSIVE']);
  assert.match(view.pending[0], /NOT_COMPUTED/);
  assert.deepEqual(view.recommendation[0], ['general','rec','r1','test','PROVISIONAL']);
  assert.deepEqual(view.policy[0], ['general','saved','r2','selected','unknown']);
  assert.deepEqual(view.activation, ['NOT_APPLIED','unknown']);
  assert.equal(view.policyOrigin, 'saved');
});
test('page uses local font fallbacks, read-only fetch and safe text rendering', () => {
  const source = readFileSync(new URL('./web/eci-tuning.mjs',import.meta.url),'utf8');
  const css = readFileSync(new URL('./web/eci-tuning.css',import.meta.url),'utf8');
  assert.match(css,/Jost, system-ui/); assert.match(css,/Newsreader, system-ui/);
  assert.doesNotMatch(css,/@import|url\(|shadow|glow/);
  assert.doesNotMatch(source,/innerHTML|method:\s*['"]POST|\/api\/inference\/fix/);
  assert.match(source,/textContent/); assert.match(source,/Status unknown/);
});

test('tuning page exposes guarded repair through an open-only Overview link', () => {
  const html=readFileSync(new URL('./web/eci-tuning.html',import.meta.url),'utf8');
  assert.match(html,/Diagnostics and repair/);
  assert.match(html,/href="\/#model-tuning"/);
  assert.match(html,/Confirm action/);
  assert.match(html,/target[^<]*effects[^<]*duration/i);
  assert.match(html,/Opening the tool does not start repair/);
  assert.match(html,/Closing the panel does not cancel a running repair/);
  assert.match(html,/Windows remains read-only/);
});

test('screening preserves denominators, deviations, pairs and uncertainty', () => {
  const row = {record_id:'a', created_at:'2026-11-12', baseline_id:'b', model:'synthetic',artifact_sha256:'fixture-hash', normalized_score:.8, interval:[.7,.9], verdict:'INCONCLUSIVE', finalist:true, full_run:'NOT_RUN',
    benchmarks:[{benchmark:'arc',planned:10,attempted:9,completed:8,correct:6,wrong:2,parsed:7,unparsed:2,cut_off:1,truncations:1,errors:1,timeouts:0,unknown_finish:1,stop_reason:'deadline',accuracy:.6,interval95:[.4,.8],normalized_score:.8,normalized_interval95:[.6,.9]}],
    comparisons:[{record_id:'b',verdict:'INCONCLUSIVE',benchmarks:{arc:{verdict:'INCONCLUSIVE',verification:'VERIFIED',reason:'incomplete pairs',n_expected:10,n_candidate:9,n_baseline:10,n_shared:8,both_correct:5,both_wrong:1,model_only:1,baseline_only:1,n_discordant:2,p_value:1,alpha:.05}}}]};
  const group = {title:'Best model for this hardware',hardware:{id:'fixture',cpu:'cpu-fixture'}, plan_id:'plan', runtime_settings:{temperature:0}, alpha:'0.05', admission_policy:{min_coverage:'1'}, note:'Full ECI NOT_RUN.', rows:[row], pending:[{model:'unknown', pre:'UNKNOWN', post:'REJECT', reason:'Missing evidence.'}],not_comparable:[{record_id:'a',other_record_id:'c',reasons:['settings differ']}]};
  const view = viewModel({rankings:{fast_check:{hardware_views:[group]}}});
  assert.equal(view.fastChecks.length, 1);
  const screen = view.fastChecks[0];
  assert.deepEqual(screen.conditions, [['Hardware profile','{"id":"fixture","cpu":"cpu-fixture"}'],['Frozen plan','plan'],['Runtime settings','{"temperature":0}'],['Alpha','0.05'],['Admission policy','{"min_coverage":"1"}']]);
  assert.doesNotMatch(screen.title, /Best model/);
  assert.match(screen.title, /Screening.*fixture/);
  assert.deepEqual(screen.rows[0], ['a','2026-11-12','b','synthetic','fixture-hash','0.80','[0.70, 0.90]','INCONCLUSIVE','Screening finalist · full run NOT_RUN','unknown']);
  assert.deepEqual(screen.benchmarks[0].slice(2,20), ['arc','10','9','8','6','2','7','2','1','1','1','0','1','deadline','0.60','[0.40, 0.80]','0.80','[0.60, 0.90]']);
  assert.deepEqual(screen.pairs[0].slice(0,10), ['Cohort peer','a','b','arc','INCONCLUSIVE','INCONCLUSIVE','VERIFIED','incomplete pairs','10','9']);
  assert.deepEqual(screen.notComparable[0], ['a','c','settings differ']);
  assert.match(screen.pending[0], /UNKNOWN.*REJECT/);
  assert.match(screen.note, /not a joint 95% interval/);
});

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

test('renderer labels policy, screening, pair outcomes and absent evidence', () => {
  const previous = globalThis.document;
  globalThis.document = fixtureDocument();
  try {
    const root = document.createElement('main');
    render({recommendation:{routes:{general:{model:'rec',run_id:'run-rec',basis:'fixture',status:'PROVISIONAL'}}},policy:{origin:'saved',routes:{general:{model:'saved',run_id:'run-policy',basis:'fixture'}}},runtime_activation:{status:'NOT_APPLIED',source_run_id:null},rankings:{fast_check:{hardware_views:[{
      title:'Best model for this hardware',hardware:{machine_label:'fixture-machine'},plan_id:'fixture-plan',note:'Full ECI NOT_RUN.',
      rows:[{record_id:'a',model:'fixture-model',normalized_score:.8,interval:[.7,.9],verdict:'INCONCLUSIVE',finalist:true,full_run:'NOT_RUN',benchmarks:[{benchmark:'arc',planned:5,attempted:4,parsed:3,unparsed:1,cut_off:1}],comparisons:[{record_id:'b',verdict:'INCONCLUSIVE',benchmarks:{arc:{verdict:'INCONCLUSIVE',n_shared:4,model_only:1,baseline_only:1}}}]}],
      pending:[{record_id:'p',baseline_id:'baseline',model:'fixture-pending',pre:'UNKNOWN',post:'REJECT',reason:'fixture reason',baseline_comparison:{arc:{verdict:'INCONCLUSIVE',reason:'incomplete pairs',n_shared:2}}}],not_comparable:[{record_id:'a',other_record_id:'c',reasons:['hardware differs']}]
    }]}}}, root);
    assert.match(root.textContent, /Recommendation.*run-rec/);
    assert.match(root.textContent, /Saved policy.*run-policy/);
    assert.match(root.textContent, /Activation by this tool; external router NOT_VERIFIED.*NOT_APPLIED.*unknown/);
    const section = root.children.find(node => node.tag === 'section' && node.children[0].textContent.includes('fixture-machine'));
    assert.ok(section);
    assert.doesNotMatch(section.textContent, /Best model for this hardware/);
    assert.match(section.textContent, /Screening finalist · full run NOT_RUN/);
    assert.match(section.textContent, /INCONCLUSIVE/);
    assert.match(section.textContent, /Unparsed.*Cut off/);
    assert.match(section.textContent, /Paired screening outcomes/);
    assert.match(section.textContent, /Bound baseline.*p.*baseline.*arc.*INCONCLUSIVE/);
    assert.match(section.textContent, /hardware differs/);
    assert.match(section.textContent, /fixture-pending.*UNKNOWN.*REJECT.*fixture reason/);
    assert.match(section.textContent, /separate from ECI/);
  } finally {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  }
});

test('ingested full ECI and fast check reach the DOM through the real Mac endpoint; invalid input stays unknown',
  {skip: process.platform === 'win32' ? 'Requires POSIX fcntl imported by the Mac server' : false}, t => {
  const python = resolvePython(t);
  if (!python) return;
  const directory = mkdtempSync(join(fileURLToPath(new URL('../', import.meta.url)), 'agiw-eci-ui-'));
  const program = `
import json, os, sys
from pathlib import Path
import eci_tuning as e
import server
from test_eci_fast_check import run

directory = Path(sys.argv[1])
os.environ['AGIW_ECI_TUNING_DATASET_DIR'] = str(directory)
os.environ['AGIW_ECI_TUNING_CONFIG'] = str(directory / 'policy.json')
data = e.ingest([e.ROOT / 'test_support/eci_tuning/results.json'])
data['fast_checks'] = [run()]
(directory / 'dataset.json').write_text(json.dumps(data), encoding='utf-8')
def request():
    handler = object.__new__(server.Handler)
    handler.path = '/api/eci-tuning'
    handler.same_origin = lambda: True
    handler.reply = lambda code, body, mime: (code, json.loads(body))
    return handler.do_GET()
valid = request()
(directory / 'dataset.json').write_text('{invalid json', encoding='utf-8')
invalid = request()
print(json.dumps([valid, invalid]))
`;
  try {
    const process = spawnSync(python.command, [...python.args, '-B', '-c', program, directory], {cwd: new URL('.', import.meta.url), encoding: 'utf8', timeout: 30000});
    assert.equal(process.status, 0, process.stderr);
    const [[validCode, valid], [invalidCode, invalid]] = JSON.parse(process.stdout);
    assert.equal(validCode, 200);
    assert.equal(invalidCode, 200);
    assert.equal(valid.dataset.status, 'configured');
    assert.equal(invalid.dataset.status, 'unknown');
    assert.ok(invalid.dataset.reason);
    const previous = globalThis.document;
    globalThis.document = fixtureDocument();
    try {
      const root = document.createElement('main');
      render(valid, root);
      assert.match(root.textContent, /configured dataset/);
      const full = root.children.find(node => node.tag === 'section' && /Baseline local protocol/.test(node.children[0].textContent));
      const screen = root.children.find(node => node.tag === 'section' && /Screening results.*fixture-machine-a/.test(node.children[0].textContent));
      assert.ok(full); assert.ok(screen);
      assert.match(full.textContent, /llama-3\.1-8b-instruct-q4km-epoch-eci.*105\.38/);
      assert.match(screen.textContent, /synthetic candidate/);
      assert.match(screen.textContent, /separate from ECI/);
      assert.doesNotMatch(full.textContent, /synthetic candidate/);
      render(invalid, root);
      assert.match(root.textContent, /Dataset unknown/);
      assert.ok(root.textContent.includes(invalid.dataset.reason));
      assert.doesNotMatch(root.textContent, /105\.38|synthetic candidate|ECI NOT_COMPUTED/);
    } finally {
      if (previous === undefined) delete globalThis.document;
      else globalThis.document = previous;
    }
  } finally { rmSync(directory, {recursive:true, force:true}); }
});
