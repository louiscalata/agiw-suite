// F17 keeps booklet red for brand/primary fills, separate from status tints.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { cascadeStyle } from './test_support/css-cascade.mjs';
import { contrastRatio } from './jaryo/contract.mjs';

const css = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
const tokens = JSON.parse(readFileSync(new URL('./jaryo/tokens.json', import.meta.url), 'utf8'));
const accent = '#C21A27';

test('booklet red clears AA on paper and as a chip', () => {
  assert.match(css, /--j-accent:\s*#C21A27;/);
  assert.match(css, /--j-accent-ink:\s*#FFFFFF;/);
  assert.ok(contrastRatio(accent, '#ffffff') >= 4.5, 'red on white is normal text');
  assert.ok(contrastRatio('#ffffff', accent) >= 4.5, 'white on red is the primary label');
  assert.ok(contrastRatio(accent, tokens.palette.dark.surface) < 3, 'red cannot be used alone on the dark surface');
  assert.ok(contrastRatio(accent, '#000000') < 4.5, 'red on black is not a body colour');
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
