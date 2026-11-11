// The overview ledger is not painted twice, and muted text is quieter than ink
// while still clearing AA for normal text.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { contrastRatio } from './jaryo/contract.mjs';

const css = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
const board = readFileSync(new URL('./web/monitor-board.mjs', import.meta.url), 'utf8');

test('muted ink clears AA on paper and on black', () => {
  assert.match(css, /--j-muted:\s*#b3b3b3;/);
  assert.match(css, /--j-muted:\s*#4d4d4d;/);
  assert.doesNotMatch(css, /--j-muted:\s*var\(--j-text\)/);
  assert.ok(contrastRatio('#4d4d4d', '#ffffff') >= 4.5);
  assert.ok(contrastRatio('#b3b3b3', '#000000') >= 4.5);
});

test('the overview does not paint the duplicate monitor board', () => {
  assert.match(app, /board\.dataset\.hideBoard='1'/);
  assert.match(board, /board\.dataset\.hideBoard === '1'/);
  assert.match(board, /board\.hidden = true/);
});
