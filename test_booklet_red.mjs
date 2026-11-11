// Booklet red is the display accent. Readings stay ink, and the accent is
// not the error colour. Ratios are WCAG relative luminance.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { contrastRatio } from './jaryo/contract.mjs';

const css = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
const accent = '#C21A27';

test('booklet red clears AA on paper and as a chip', () => {
  assert.match(css, /--j-accent:\s*#C21A27;/);
  assert.match(css, /--j-accent-ink:\s*#ffffff;/);
  assert.ok(contrastRatio(accent, '#ffffff') >= 4.5, 'red on white is normal text');
  assert.ok(contrastRatio('#ffffff', accent) >= 4.5, 'white on red is the current tab');
  assert.ok(contrastRatio(accent, '#000000') >= 3, 'red on black is large bold display');
  assert.ok(contrastRatio(accent, '#000000') < 4.5, 'red on black is not a body colour');
});

test('the accent is headlines, the wordmark, and the current tab', () => {
  for (const selector of [
    'body.jaryo-app h1',
    'body.jaryo-app .brand-wordmark strong',
    'body.jaryo-app .j-type',
    'body.jaryo-app .j-nav a[aria-current="page"]',
    'body.jaryo-app .j-tab[aria-selected="true"]',
  ]) {
    assert.ok(css.includes(selector), selector);
  }
  assert.match(css, /\.j-callout,\s*body\.jaryo-app \.j-step,[\s\S]*color:\s*var\(--j-text\);/);
  assert.doesNotMatch(css, /\.j-blocked[^{]*\{[^}]*var\(--j-accent\)/);
  assert.doesNotMatch(css, /\.j-word[^{]*\{[^}]*var\(--j-accent\)/);
});

test('selected tab labels use the forced-colors selection text', () => {
  const blocks = [...css.matchAll(/@media\s*\(forced-colors:\s*active\)\s*\{([\s\S]*?)^\}/gm)];
  assert.ok(blocks.length > 0, 'forced-colors rules exist');
  for (const [, block] of blocks) {
    const rules = [...block.matchAll(/([^{}]+)\{([^{}]*)\}/g)];
    for (const label of ['j-word', 'j-glyph', 'j-tab-label']) {
      const selector = `body.jaryo-app .j-tab[aria-selected="true"] .${label}`;
      const rule = rules.find(([, selectors]) => selectors.split(',').some((s) => s.trim() === selector));
      assert.ok(rule, `${selector} has a forced-colors rule`);
      assert.match(rule[2], /(?:^|;)\s*color:\s*HighlightText\s*;/);
      assert.match(rule[2], /(?:^|;)\s*forced-color-adjust:\s*none\s*;/);
    }
  }
});
