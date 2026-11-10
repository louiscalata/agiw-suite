// Readings are one rule. The BLOCKED line is not set on a hatch, and the
// typeface notes live on the identity page rather than above the components wordmark.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');

test('blocked readings are a single rule and do not sit on hatching', () => {
  const css = read('./web/jaryo.css');
  assert.doesNotMatch(css, /repeating-linear-gradient/);
  assert.doesNotMatch(css, /3px double/);
  assert.match(css, /\.j-blocked \{[^}]*background-image: none;/);
  assert.match(css, /\.j-matrix-blocked \{[^}]*background-image: none;/);
  assert.match(css, /\.j-step \{[^}]*border-bottom: 1px solid var\(--j-border\);/);
});

test('typeface notes are on the identity page, and that page records the mark', () => {
  const components = read('./web/components.html');
  const identity = read('./web/identity.html');
  assert.doesNotMatch(components, /Typeface notes/);
  assert.match(components, /<main id="content" class="j-page" tabindex="-1">\s*<header>/);
  assert.match(identity, /Typeface notes/);
  assert.match(identity, /TYPE ONLY/);
  assert.match(identity, /The name is the identity/);
  assert.match(identity, /D4 Split is rejected/);
  assert.match(identity, /Inter/);
  assert.match(identity, /The favicon is the letters AG/);
  assert.match(identity, /p class="j-type j-type-sample j-plate j-plate-dark"/);
});
