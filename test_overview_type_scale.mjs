// The overview keeps style.css for layout. Its lilac wash and violet client
// rules are gone. The Fleet screen overrides the legacy display scale with the
// redesign's five readable roles without changing other screens or Android.
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

test('Fleet uses 13/14/16/20/28px roles and caps every inherited display size at 28px', () => {
  const overrides = [...jaryo.matchAll(/body\.jaryo-app\.fleet-ledger\s*\{([^{}]*)\}/g)]
    .map(match => match[1]).join('\n');
  const roles = [...overrides.matchAll(/--j-size-(\d):\s*(\d+)px\s*;/g)];
  assert.equal(roles.length, 6, 'override all six legacy slots so inherited wordmarks and headings are bounded');
  assert.deepEqual([...new Set(roles.map(match => Number(match[2])))].sort((a, b) => a - b), [13, 14, 16, 20, 28]);
  assert.ok(roles.every(match => Number(match[2]) <= 28));
  assert.match(jaryo, /font-family: "IBM Plex Sans", "Liberation Sans", "Noto Sans", sans-serif !important;/);
  assert.match(jaryo, /font-family: "IBM Plex Mono"/);
  const scopedRules = [...jaryo.matchAll(/([^{}]*\.fleet-ledger[^{}]*)\{([^{}]*)\}/g)]
    .map(match => match[2]).join('\n');
  for (const match of scopedRules.matchAll(/font-size:\s*(\d+)px/g)) {
    assert.ok([13, 14, 16, 20, 28].includes(Number(match[1])), `unexpected Fleet type role ${match[1]}px`);
  }
});
