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
test('view model preserves separate classes, overlap and pending status', () => {
  const group = {eci:[], notes:['90% CIs overlap; no clean winner.'], components:{}, unknown_latency:['a'], unknown_cost:['a']};
  const view = viewModel({rankings:{baseline_protocol:group, pipeline_configured:group}, pending:[{model:'Pending', status:'NOT_COMPUTED'}], policy:{routes:{general:{model:'fixture', confidence:'low-to-moderate'}}}});
  assert.equal(view.sections.length, 2);
  assert.match(view.sections[0].title, /baseline/);
  assert.match(view.sections[1].title, /separate protocol/);
  assert.match(view.pending[0], /NOT_COMPUTED/);
  assert.match(view.defaultNote, /NOT_APPLIED/);
  assert.match(view.sections[0].notes[0], /no clean winner/);
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

test('fast checks render in a separate hardware view with ties and no ECI promotion', () => {
  const group = {title:'Best model for this hardware as of 2026-11-12', hardware:{id:'fixture'}, plan_id:'plan', note:'Full ECI NOT_RUN.', rows:[{label:'fast check', model:'synthetic', normalized_score:.8, interval:[.7,.9], tie_with:['other'], verdict:'INCONCLUSIVE', finalist:true}], pending:[{model:'unknown', pre:'UNKNOWN', post:'REJECT', reason:'Missing evidence.'}]};
  const view = viewModel({rankings:{fast_check:{hardware_views:[group]}}});
  assert.equal(view.sections.length, 0);
  assert.equal(view.fastChecks.length, 1);
  assert.match(view.fastChecks[0].title, /Fast screening.*fixture/);
  assert.deepEqual(view.fastChecks[0].rows[0].slice(0, 3), ['fast check', 'synthetic', '0.80 [0.70, 0.90]']);
  assert.match(view.fastChecks[0].rows[0][3], /Tie.*INCONCLUSIVE/);
  assert.match(view.fastChecks[0].rows[0][4], /Finalist.*NOT_RUN/);
  assert.match(view.fastChecks[0].pending[0], /UNKNOWN.*REJECT/);
  assert.match(view.fastChecks[0].note, /not a joint 95% interval/);
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

test('renderer creates the supplied fast-check hardware table and pending reasons', () => {
  const previous = globalThis.document;
  globalThis.document = fixtureDocument();
  try {
    const root = document.createElement('main');
    render({rankings:{fast_check:{hardware_views:[{
      title:'Best model for this hardware as of fixture-date',
      hardware:{machine_label:'fixture-machine',cpu:'fixture-cpu',gpu:'fixture-gpu',memory:{bytes:4096},runtime_build_id:'fixture-build'},
      plan_id:'fixture-plan', note:'Full ECI NOT_RUN.',
      rows:[{label:'fast check',model:'fixture-model',normalized_score:.8,interval:[.7,.9],tie_with:['fixture-peer'],verdict:'INCONCLUSIVE',finalist:true}],
      pending:[{model:'fixture-pending',pre:'UNKNOWN',post:'REJECT',reason:'fixture reason'}]
    }]}}}, root);
    const section = root.children.find(node => node.tag === 'section' && node.children[0].textContent.includes('fixture-machine'));
    assert.ok(section, 'renderer must append a fast-check hardware section');
    assert.match(section.textContent, /fixture-model/);
    assert.match(section.textContent, /0.80 \[0.70, 0.90\]/);
    assert.match(section.textContent, /Tie · INCONCLUSIVE/);
    assert.match(section.textContent, /Finalist · full run NOT_RUN/);
    assert.match(section.textContent, /fixture-pending.*UNKNOWN.*REJECT.*fixture reason/);
    const tables = section.children.filter(node => node.tag === 'div').flatMap(node => node.children);
    assert.equal(tables.length, 1);
    assert.equal(tables[0].tag, 'table');
    assert.match(tables[0].children[0].textContent, /separate from ECI/);
  } finally {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  }
});

test('ingested full ECI and fast check reach the DOM through the real Mac endpoint; invalid input stays unknown', t => {
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
      const full = root.children.find(node => node.tag === 'section' && /baseline local protocol/.test(node.children[0].textContent));
      const screen = root.children.find(node => node.tag === 'section' && /Fast screening.*fixture-machine-a/.test(node.children[0].textContent));
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
