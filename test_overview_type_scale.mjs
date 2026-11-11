// The overview keeps style.css for layout. Its lilac wash and violet client
// rules are gone, and the page paints one six-step scale, none under 12px.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const style = readFileSync(new URL('./web/style.css', import.meta.url), 'utf8');
const jaryo = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');

test('the overview stylesheet no longer paints the lilac wash or violet client rules', () => {
  assert.doesNotMatch(style, /#8a64ee|#7b5de3|#b6a1e8|#c4cad9|#c9b7ff|#d9c9ff|#8471af|#6d578f|#b99aff/);
  assert.doesNotMatch(style, /\.graph-region\{background:radial-gradient/);
  assert.doesNotMatch(style, /background-image:radial-gradient\(#c4cad9/);
  assert.match(style, /--violet:var\(--ink\)/);
});

test('the overview uses six type steps and none of them is under 12px', () => {
  const marker = 'Six steps, none under 12px';
  const block = jaryo.slice(jaryo.indexOf(marker));
  assert.ok(block.length > marker.length);
  for (const size of ['13px', '21px', '34px', '55px', '89px', '144px']) {
    assert.match(block, new RegExp(`--j-size-\\d: ${size};`));
  }
  assert.match(block, /font-size: var\(--j-size-2\) !important;/);
  assert.match(block, /font-family: "IBM Plex Sans", "Liberation Sans", "Noto Sans", sans-serif !important;/);
  assert.match(block, /font-family: "IBM Plex Mono"/);
  assert.doesNotMatch(block, /font-size:\s*(?:[0-9]|1[01])px/);
});
