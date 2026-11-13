import assert from 'node:assert/strict';
import {mkdtempSync, rmSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import test from 'node:test';
import {resolvePython} from './test_support/python-interpreter.mjs';
import {render} from './web/eci-tuning.mjs';

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

test('ingested full ECI and fast check reach the DOM through the Windows endpoint; invalid input stays unknown', t => {
  const python = resolvePython(t);
  if (!python) return;
  const directory = mkdtempSync(join(fileURLToPath(new URL('../', import.meta.url)), 'agiw-eci-win-ui-'));
  const program = `
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'windows'))
from agiw_win import server
import eci_tuning as e
from test_eci_fast_check import run

directory = Path(sys.argv[1])
os.environ['AGIW_ECI_TUNING_DATASET_DIR'] = str(directory)
os.environ['AGIW_ECI_TUNING_CONFIG'] = str(directory / 'policy.json')
data = e.ingest([e.ROOT / 'test_support/eci_tuning/results.json'])
# Ingest the fast-check fixture too, so both kinds of evidence pass through the loader.
raw = directory / 'input.json'
raw.write_text(json.dumps(dict(data, fast_checks=[run()])), encoding='utf-8')
data = e.ingest([raw])
(directory / 'dataset.json').write_text(json.dumps(data), encoding='utf-8')
def request():
    handler = object.__new__(server.Handler)
    handler.path = '/api/eci-tuning'
    handler.same_origin = lambda: True
    handler.reply = lambda code, body, mime: setattr(handler, 'response', (code, json.loads(body)))
    handler.do_GET()
    return handler.response
valid = request()
(directory / 'dataset.json').write_text('{invalid json', encoding='utf-8')
invalid = request()
print(json.dumps([valid, invalid]))
`;
  try {
    const result = spawnSync(python.command, [...python.args, '-B', '-c', program, directory], {cwd: new URL('.', import.meta.url), encoding: 'utf8', timeout: 30000});
    assert.equal(result.status, 0, result.stderr);
    const [[validCode, valid], [invalidCode, invalid]] = JSON.parse(result.stdout);
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
