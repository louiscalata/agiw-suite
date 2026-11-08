import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const css = read('./web/jaryo.css');
const html = read('./web/index.html');

/** The declarations of the rule whose selector list contains `needle` (first match). */
function rule(needle, from = 0) {
  const start = css.indexOf(needle, from);
  assert.notEqual(start, -1, `no rule for ${needle}`);
  const open = css.indexOf('{', start);
  const close = css.indexOf('}', open);
  return { selector: css.slice(start, open), body: css.slice(open + 1, close), end: close };
}

const BUTTONS = 'body.jaryo-app #runtimeSidebar .view-tabs button,\nbody.jaryo-app #runtimeSidebar #modelScopeControls > button {';
const SELECTED = 'body.jaryo-app #runtimeSidebar .view-tabs button[aria-pressed="true"],\nbody.jaryo-app #runtimeSidebar #modelScopeControls > button[aria-pressed="true"] {';

test('the two sidebar switches share one rule with the monitor tabs\' size and face', () => {
  const tab = rule('.j-tab {');
  const shared = rule(BUTTONS);
  for (const [name, value] of [['min-height', '44px'], ['font', '12px/1.2 var(--j-font)']]) {
    assert.match(tab.body, new RegExp(`${name}:\\s*${value.replace(/[()/.]/g, '\\$&')}`), `.j-tab ${name}`);
    assert.match(shared.body, new RegExp(`${name}:\\s*${value.replace(/[()/.]/g, '\\$&')}`), `shared ${name} must equal the monitor tabs'`);
  }
  assert.doesNotMatch(shared.body, /Futura|serif|var\(--type-display\)/, 'no second typeface');
});

test('the selected option is inverted, and is the same fill the monitor tabs use', () => {
  const selected = rule(SELECTED);
  assert.match(selected.body, /background:\s*var\(--j-invert\)/);
  assert.match(selected.body, /color:\s*var\(--j-invert-ink\)/);
  assert.match(css, /\.j-tab\[aria-selected="true"\]\s*\{\s*background:\s*var\(--j-invert\);\s*color:\s*var\(--j-invert-ink\)/);
});

test('the old arrow glyph is gone, hover is an underline for a pointer only, and focus is left to the shared Jaryo ring', () => {
  assert.match(css, /#runtimeSidebar \.view-tabs button::after \{ content: none; \}/);
  const at = css.indexOf('@media (hover: hover) {\n  body.jaryo-app #runtimeSidebar .view-tabs button:hover');
  assert.notEqual(at, -1, 'the hover rule is inside a hover media query');
  const hover = css.slice(at, css.indexOf('\n}\n', at));
  assert.match(hover, /button:hover,[^{]*#modelScopeControls > button:hover\s*\{[^}]*text-decoration:\s*underline/);
  assert.match(hover, /\[aria-pressed="true"\]:hover[^{]*\{[^}]*text-decoration:\s*none/);
  // The block must not draw a second focus ring: the dashed 3 px one on `body.jaryo-app button:focus-visible` is the contract.
  const start = css.indexOf('/* One tab look for every switch between views.');
  const end = css.indexOf('body.jaryo-app #autoUnloadToggle,', start);
  assert.doesNotMatch(css.slice(start, end), /:focus-visible\s*[,{]|outline-offset/);
  assert.match(css, /body\.jaryo-app button:focus-visible,[\s\S]{0,400}outline:\s*3px dashed var\(--j-focus\)/);
});

test('the block uses tokens only: no colour literal, so the theme and the two-colour rule hold', () => {
  const start = css.indexOf('/* One tab look for every switch between views.');
  const end = css.indexOf('body.jaryo-app #autoUnloadToggle,', start);
  assert.ok(start > 0 && end > start);
  const block = css.slice(start, end);
  assert.doesNotMatch(block, /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/);
});

test('both groups exist in the page and keep aria-pressed, which is what the rules key on', () => {
  assert.match(html, /class="view-tabs"[^>]*>(?:(?!<\/div>).)*data-view="runtime" aria-pressed=/s);
  assert.match(html, /id="modelScopeControls"[^>]*>(?:(?!<\/div>).)*data-model-scope="core" aria-pressed=/s);
  assert.match(html, /id="monitorTabs" class="j-tabs" role="tablist"/);
});
