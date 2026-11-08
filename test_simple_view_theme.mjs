import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('./web/simple-view.css', import.meta.url), 'utf8');
const primitives = readFileSync(new URL('./jaryo/primitives.css', import.meta.url), 'utf8');
const simple = css.slice(0, css.indexOf('.j-bus{'));

test('the simple view does not choose its own theme from the operating system', () => {
  // An explicit Light or Dark in the Theme control must reach it; prefers-color-scheme alone ignored that.
  assert.doesNotMatch(simple, /prefers-color-scheme/);
});

test('its text, background, rules and button come from the Jaryo tokens', () => {
  assert.match(simple, /#svBody\{[^}]*color:var\(--j-text,#000\);background:var\(--j-bg,#fff\)/);
  assert.match(simple, /\.sv-row\{[^}]*border-top:1px solid var\(--j-border,#000\)/);
  assert.match(simple, /\.sv-row\[data-tone="ok"\]\{border-top-color:var\(--j-border,#000\)\}/);
  assert.match(simple, /#svBody button\{[^}]*color:var\(--j-invert-ink,#fff\);background:var\(--j-invert,#000\)/);
});

test('every token it uses exists, and the primitives resolve it for System, Light and Dark', () => {
  for (const token of ['--j-text', '--j-bg', '--j-border', '--j-invert', '--j-invert-ink']) {
    assert.ok(simple.includes(`var(${token},`), token);
    assert.match(primitives, new RegExp(`${token}:`), `${token} defined`);
  }
  assert.match(primitives, /html\[data-theme="light"\]/);
  assert.match(primitives, /html\[data-theme="dark"\]/);
  assert.match(primitives, /@media \(prefers-color-scheme: light\)\s*\{\s*html:not\(\[data-theme="dark"\]\)/);
});

test('the rest of the stylesheet (text zoom, transitions) is untouched', () => {
  assert.match(css, /html\[data-text-size="200"\] body\{zoom:2\}/);
  assert.match(css, /@keyframes j-zoom-in/);
});
