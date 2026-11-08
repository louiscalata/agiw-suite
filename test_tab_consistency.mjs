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

test('the old arrow glyph is gone, and hover is an underline for a pointer only on all three tab groups', () => {
  assert.match(css, /#runtimeSidebar \.view-tabs button::after \{ content: none; \}/);
  const at = css.indexOf('@media (hover: hover) {\n  body.jaryo-app .j-tab:hover');
  assert.notEqual(at, -1, 'the hover rule is inside a hover media query and covers the monitor tabs');
  const hover = css.slice(at, css.indexOf('\n}\n', at));
  assert.match(hover, /\.j-tab:hover,[^{]*view-tabs button:hover,[^{]*#modelScopeControls > button:hover\s*\{[^}]*text-decoration:\s*underline/);
  assert.match(hover, /\.j-tab\[aria-selected="true"\]:hover[^{]*\{[^}]*text-decoration:\s*none/);
});

test('focus on a tab-like control is drawn inside it, on all three groups, and takes the ink colour on the selected option', () => {
  // Outside, the monitor tab strip (overflow-y: auto, tabs flush with its edges) clips the ring, and between options it lands on the inverted neighbour.
  assert.match(css, /body\.jaryo-app \.j-tab:focus-visible,\s*body\.jaryo-app #runtimeSidebar \.view-tabs button:focus-visible,\s*body\.jaryo-app #runtimeSidebar #modelScopeControls > button:focus-visible\s*\{\s*outline:\s*3px dashed var\(--j-focus\);\s*outline-offset:\s*-4px;/);
  assert.match(css, /\.j-tab\[aria-selected="true"\]:focus-visible,[^{]*\[aria-pressed="true"\]:focus-visible,[^{]*\[aria-pressed="true"\]:focus-visible\s*\{\s*outline-color:\s*var\(--j-invert-ink\)/);
  assert.match(css, /body\.jaryo-app button:focus-visible,[\s\S]{0,400}outline:\s*3px dashed var\(--j-focus\)/, 'the shared ring stays for everything else');
});

test('pressed and expanded toggles are inverted, with no hard-coded grey, and the toolbar targets are 44px', () => {
  const at = css.indexOf('body.jaryo-app .tools #pause[aria-pressed="true"]');
  assert.notEqual(at, -1);
  const rule = css.slice(at, css.indexOf('}', at));
  for (const sel of ['.fix-tab[aria-expanded="true"]', '.vital[aria-expanded="true"]', '.legend-toggle[aria-expanded="true"]', '.about-compact[aria-expanded="true"]']) {
    assert.ok(rule.includes(sel), sel);
  }
  assert.match(rule, /background:\s*var\(--j-invert\)/);
  assert.match(rule, /color:\s*var\(--j-invert-ink\)/);
  const sizes = css.slice(css.indexOf('body.jaryo-app .tools .mode-tab,'));
  assert.match(sizes.slice(0, sizes.indexOf('}')), /\.tools #pause,[\s\S]*\.vital\s*\{\s*min-height:\s*44px/);
  // the legacy rule that painted Fix inference expanded with #202020 must still be beaten by the pressed rule above
  assert.match(read('./web/style.css'), /\.tools \.fix-tab\[aria-expanded=true\]\{border-color:var\(--blue\);background:#202020\}/);
});

test('the block uses tokens only: no colour literal, so the theme and the two-colour rule hold', () => {
  const start = css.indexOf('/* One tab look for every switch between views.');
  const end = css.indexOf('/* Focus on a tab-like control is drawn INSIDE it.', start);
  assert.ok(start > 0 && end > start);
  const block = css.slice(start, end);
  assert.doesNotMatch(block, /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/);
});

test('both groups exist in the page and keep aria-pressed, which is what the rules key on, and both are named groups', () => {
  assert.match(html, /class="view-tabs" role="group" aria-label="Map view"/);
  assert.match(html, /class="view-tabs"[^>]*>(?:(?!<\/div>).)*data-view="runtime" aria-pressed=/s);
  assert.match(html, /id="modelScopeControls"[^>]*>(?:(?!<\/div>).)*data-model-scope="core" aria-pressed=/s);
  assert.match(html, /id="monitorTabs" class="j-tabs" role="tablist"/);
});
