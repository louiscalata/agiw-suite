// LOU-59: the owner's Jaryo calls on the Suite side (2026-11-19).
// Buttons show words, missing readings say "Not measured", and the kit reads the Jaryo kit tokens.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { detailRows, NOT_MEASURED } from './web/monitor.mjs';
import { kitVars, readKit } from './jaryo/generate.mjs';
import { KIT_TOKENS_SHA256 } from './jaryo/contract.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');

test('no web page has a glyph-only button', () => {
  for (const file of readdirSync(new URL('./web/', import.meta.url)).filter(name => name.endsWith('.html'))) {
    for (const [, text] of read(`./web/${file}`).matchAll(/<button\b[^>]*>([^<]*)<\/button>/g)) {
      assert.match(text, /[A-Za-z]{2,}/, `${file}: button text "${text}" has no word`);
    }
  }
});

test('missing readings say exactly "Not measured"', () => {
  assert.equal(NOT_MEASURED, 'Not measured');
  const rows = Object.fromEntries(detailRows({ observationSource: 'registry' }));
  for (const label of ['Health', 'Model', 'Prompt tok/s', 'Generation tok/s', 'Latency', 'Queue', 'Slots', 'Context', 'VRAM', 'Observation age']) {
    assert.equal(rows[label], 'Not measured', label);
  }
  assert.equal(Object.fromEntries(detailRows({ slotsBusy: 1 })).Slots, '1/Not measured');
  assert.equal(Object.fromEntries(detailRows({ vramUsedMiB: 512 })).VRAM, '512 MiB/Not measured');
  assert.equal(Object.fromEntries(detailRows({ vramUsedMiB: 512, vramTotalMiB: 1024 })).VRAM, '512/1024 MiB');
  const html = read('./web/index.html');
  assert.doesNotMatch(html, />—</);
  const app = read('./web/app.js');
  for (const id of ['activeCount', 'loadedCount', 'queuedCount']) assert.match(app, new RegExp(`\\['${id}',[^\\]]*'Not measured'`), id);
  assert.doesNotMatch(app, /fc\.\w+===null\?'—'|\?'—':fc\./);
});

test('the Suite kit reads the pinned Jaryo kit tokens', () => {
  const raw = readFileSync(new URL('./jaryo/vendor/jaryo.tokens.json', import.meta.url));
  assert.equal(createHash('sha256').update(raw).digest('hex'), KIT_TOKENS_SHA256);
  assert.match(read('./jaryo/vendor/SOURCE.md'), /Jaryo kit repository/);
  const vars = kitVars(readKit());
  assert.equal(vars['--jaryo-s1'], '4px');
  assert.equal(vars['--jaryo-hit'], '44px');
  const css = read('./jaryo/primitives.css');
  assert.match(css, /--jaryo-bg: #0c0c0c;/);
  assert.match(css, /--j-space-1: var\(--jaryo-s1\);/);
  assert.match(css, /--j-type-body: var\(--jaryo-fs-3\);/);
  assert.match(css, /--j-control-min: var\(--jaryo-hit\);/);
  // No kit token equals 28px, so the display step keeps its own value.
  assert.match(css, /--j-type-display: 28px;/);
});
