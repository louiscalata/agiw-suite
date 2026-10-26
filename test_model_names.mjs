import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import * as layout from './web/map-layout.mjs';

// Frozen pre-extraction app.js formula: shared Mac and Windows labels must keep
// the same spelling, capitalization, whitespace, and String conversion.
const legacyName = id => String(id).split('/').pop().replaceAll('-', ' ')
  .replace(/\b(gemma|qwen)\b/gi, s => s[0].toUpperCase() + s.slice(1))
  .replace(/\bgpt\b/gi, 'GPT').replace(/\boss\b/gi, 'OSS').replace(/^./, s => s.toUpperCase());
const ids = ['qwen/qwen3.8-27b', 'google/gemma-4-26b-a4b-qat', 'openai/gpt-oss-20b',
  'claude-opus-5-5', 'slashless', '', '家族/模型-α-ß', 'familie/über-gemma-gpt-oss',
  'QWEN-GEMMA-GPT-OSS', 'org/gptoss-qwen3.8', 'org/multi/last-part', '/trailing/',
  '  spaces-id  ', '\nqwen-id', null, undefined, false, 42, ['org/model-name'], {}];
const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8')
  .replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];

test('shared model-name export is byte-identical to the original Mac and Windows formula', () => {
  assert.equal(typeof layout.friendlyModelName, 'function', 'the shared formatter is exported');
  for (const id of ids) assert.deepEqual(Buffer.from(layout.friendlyModelName(id), 'utf8'),
    Buffer.from(legacyName(id), 'utf8'), `unchanged bytes for ${String(id)}`);
});

test('actual app name call site preserves all legacy values', () => {
  const names = runInNewContext(`${app}\ninputs.map(id=>name(id))`, {...layout, inputs: ids});
  assert.deepEqual(Array.from(names), ids.map(legacyName));
});

test('actual app name delegates to the shared formatter without changing its input', () => {
  const input = {toString() { return 'org/original-id'; }}, calls = [];
  const result = runInNewContext(`${app}\nname(input)`, {...layout, input,
    friendlyModelName: id => { calls.push(id); return 'shared formatter result'; }});
  assert.equal(result, 'shared formatter result');
  assert.deepEqual(calls, [input]);
});

test('actual Windows Mac sidebar helper preserves names, original keys, and inventory guards', () => {
  const macIds = ids.slice(0, 4);
  for (const state of ['linked', 'reachable']) for (const id of macIds) {
    const anchor = id === 'openai/gpt-oss-20b' ? 'qwen/qwen3.8-27b' : 'openai/gpt-oss-20b';
    const peer = layout.macPeerView({state, ageSeconds: 0, loadedCount: 2,
      models: [{id: anchor, state: 'loaded'}, {id, state: 'loaded'}]},
    {feedFresh: true, snapshotAge: 0});
    const rows = layout.windowsMacListEntries(peer).filter(row => row.key.startsWith('mac-model:'));
    assert.deepEqual(rows.map(row => [row.key, row.title]),
      [['mac-model:0', legacyName(anchor)], ['mac-model:1', legacyName(id)]]);
    const matched = layout.windowsMacListEntries(peer, legacyName(id));
    assert.ok(matched.some(row => row.key === 'mac-model:1' && row.title === legacyName(id)));
    const stale = layout.windowsMacListEntries({...peer, current: false});
    assert.ok(!stale.some(row => row.key.startsWith('mac-model:')));
  }
  const invalid = layout.windowsMacListEntries({state: 'linked', current: true, reachable: true,
    satellites: [{id: '', state: 'loaded'}, {id: ['openai/gpt-oss-20b'], state: 'loaded'}]});
  assert.ok(!invalid.some(row => row.key.startsWith('mac-model:')),
    'extracting String conversion does not weaken Mac inventory validation');
});

test('actual Windows map helper preserves full names and unknown identity guards', () => {
  for (const [index, id] of ids.entries()) {
    const key = `client-model:codex:${index}`, expected = typeof id === 'string' && id.trim()
      ? legacyName(id) : 'Unknown model';
    const nodes = [{id: key, kind: 'client-model', client: {id: 'codex'}, clientModel: {id},
      subtitle: 'SAVED SESSION CHOICE'}];
    const rows = layout.windowsMapListEntries(nodes, {host: 'windows', query: expected || id});
    assert.deepEqual(rows.map(row => [row.key, row.title, row.subtitle, row.tone]),
      [[key, expected, 'Saved session choice', 'muted']]);
    assert.deepEqual(layout.windowsMapListEntries(nodes, {host: 'windows'}), []);
    assert.deepEqual(layout.windowsMapListEntries(nodes, {host: 'mac', query: expected || id}), []);
  }
});
