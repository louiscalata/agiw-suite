// F17 uses Copper for accent/focus, separate from state.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { cascadeStyle } from './test_support/css-cascade.mjs';
import { contrastRatio } from './jaryo/contract.mjs';

const css = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
const tokens = JSON.parse(readFileSync(new URL('./jaryo/tokens.json', import.meta.url), 'utf8'));
const accent = '#C47A3A';

test('Copper fill has AA text contrast and is not normal text on paper', () => {
  assert.match(css, /--j-accent:\s*#C47A3A;/);
  assert.match(css, /--j-accent-ink:\s*#000000;/);
  assert.ok(contrastRatio('#000000', accent) >= 4.5, 'black on Copper is the primary label');
  assert.ok(contrastRatio(accent, tokens.palette.light.surface) < 4.5, 'Copper on white is not normal text');
  for (const palette of Object.values(tokens.palette)) {
    assert.ok(contrastRatio(palette.focus_ring, palette.bg) >= 3, 'focus boundary is visible');
    assert.ok(contrastRatio(palette.accent_ink, palette.accent_identity) >= 4.5, 'accent label is readable');
  }
});

test('the accent is a brand or primary fill; headlines and selected tabs use ink', () => {
  for (const selector of [
    'body.jaryo-app h1',
    'body.jaryo-app .brand-wordmark strong',
    'body.jaryo-app .j-type',
    'body.jaryo-app .j-nav a[aria-current="page"]',
    'body.jaryo-app .j-tab[aria-selected="true"]',
  ]) {
    assert.ok(css.includes(selector), selector);
  }
  assert.match(css, /\.jp-primary[^{}]*\{[^}]*color: var\(--j-accent-ink\);[^}]*background: var\(--j-accent\);/);
  assert.match(css, /\.j-callout,\s*body\.jaryo-app \.j-step,[\s\S]*color:\s*var\(--j-text\);/);
  assert.doesNotMatch(css, /\.j-blocked[^{]*\{[^}]*var\(--j-accent\)/);
  assert.doesNotMatch(css, /\.j-word[^{]*\{[^}]*var\(--j-accent\)/);
});

function assertSelectedLabels(source) {
  assert.match(source,/@media\s*\(forced-colors:\s*active\)/);
  for(const label of ['j-word','j-glyph','j-tab-label']) {
    const style=cascadeStyle(source,[{tag:'body',classes:['jaryo-app']},{tag:'button',classes:['j-tab'],attrs:{'aria-selected':'true'}},{tag:'span',classes:[label]}],{forced:true});
    assert.equal(style.color,'HighlightText',label);
    assert.equal(style['forced-color-adjust'],'none',label);
  }
}
test('selected tab labels use the winning forced-colors selection declarations',()=>assertSelectedLabels(css));
for(const rule of [
  'body.jaryo-app .j-tab[aria-selected="true"] .j-word { color: transparent; }',
  'body.jaryo-app .j-tab[aria-selected="true"] .j-glyph { color: HighlightText; color: transparent; }',
  '.j-tab-label { color: transparent !important; }',
])test(`selected-label check rejects a later invisible override: ${rule}`,()=>{
  assert.throws(()=>assertSelectedLabels(css+'\n@media (forced-colors: active) { '+rule+' }'));
});
