import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
const at = css.indexOf('@media (forced-colors: active) {\n  body.jaryo-app .item.selected');
const block = at === -1 ? '' : css.slice(at, css.indexOf('\n}\n', at));

test('selected options take the palette selection colours in forced-colours mode', () => {
  assert.notEqual(at, -1, 'a forced-colors block for the selected state exists');
  // Every place the page marks a selection with an inverted fill must be covered, or it vanishes for High Contrast users.
  for (const sel of ['.item.selected', '.j-selected', '.j-tab[aria-selected="true"]', '.view-tabs button[aria-pressed="true"]', '#modelScopeControls > button[aria-pressed="true"]']) {
    assert.ok(block.includes(sel), sel);
  }
  assert.match(block, /forced-color-adjust:\s*none;\s*background:\s*Highlight;\s*color:\s*HighlightText;/);
});

test('every selected selector the normal theme inverts is also covered here', () => {
  // The normal rule that inverts these (so this test fails if someone adds a new inverted selection and forgets High Contrast).
  const normal = css.slice(0, at === -1 ? css.length : at);
  const inverted = [...normal.matchAll(/([^{}]*aria-(?:selected|pressed)="true"\][^{}]*)\{[^}]*background:\s*var\(--j-invert\)/g)]
    .flatMap(m => m[1].split(',').map(s => s.trim()))
    .filter(s => !s.includes(':hover') && !s.includes(':focus') && !s.includes('.tools') && !s.includes('.vital') && !s.includes('.map-controls'));
  assert.ok(inverted.length >= 3, 'found the inverted selections');
  for (const selector of inverted) {
    const tail = selector.replace(/^body\.jaryo-app\s*/, '');
    assert.ok(block.includes(tail), `${selector} must be covered for forced colours`);
  }
});

test('the block does not use author colours, only system colours', () => {
  assert.doesNotMatch(block, /#[0-9a-fA-F]{3,8}\b|rgba?\(|var\(--j-/);
});
