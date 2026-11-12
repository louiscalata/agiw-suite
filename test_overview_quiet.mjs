// The overview ledger is not painted twice, and muted text is quieter than ink
// while still clearing AA for normal text.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { contrastRatio } from './jaryo/contract.mjs';

const css = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
const board = readFileSync(new URL('./web/monitor-board.mjs', import.meta.url), 'utf8');

test('recorded muted ink clears AA on every light and dark surface', () => {
  const tokens = JSON.parse(readFileSync(new URL('./jaryo/tokens.json', import.meta.url), 'utf8'));
  for (const palette of Object.values(tokens.palette)) {
    assert.ok(css.includes(`--j-muted: ${palette.muted};`));
    for (const background of [palette.bg, palette.surface]) assert.ok(contrastRatio(palette.muted, background) >= 4.5);
  }
});

test('the overview does not paint the duplicate monitor board', () => {
  assert.match(app, /board\.dataset\.hideBoard='1'/);
  assert.match(board, /board\.dataset\.hideBoard === '1'/);
  assert.match(board, /board\.hidden = true/);
});
