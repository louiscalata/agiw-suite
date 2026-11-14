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

test('all monitor screens inherit the five role scale and Fleet caps inherited displays', () => {
  const generatedRoles = [...jaryo.matchAll(/--j-web-size-\d:\s*(\d+)px;/g)].map(match => Number(match[1]));
  assert.deepEqual(generatedRoles, [13, 14, 16, 20, 28, 28]);
  for (let i = 1; i <= 6; i++) assert.ok(jaryo.includes(`--j-size-${i}: var(--j-web-size-${i});`));
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

test('narrow monitor chrome preserves reading context and reflows without fixed column minima', () => {
  const overrides = jaryo.slice(jaryo.indexOf('/* Monitor visual roles and narrow-screen reflow.'));
  // Only the two tab hints are shown. The aria-live status lines on Machines and Setup keep the visually hidden utility.
  assert.match(overrides, /:is\(#monitorTabHint,#mapTabHint\)\.j-hint\s*\{[^}]*position:static;[^}]*overflow:visible;[^}]*clip-path:none/s);
  assert.doesNotMatch(overrides, /body\.jaryo-app \.j-hint\s*\{[^}]*(?:position:static|clip-path:none)/);
  for (const [page, id] of [['index', 'monitorTabHint'], ['monitor', 'mapTabHint']]) {
    assert.match(readFileSync(new URL(`./web/${page}.html`, import.meta.url), 'utf8'), new RegExp(`class="j-hint" id="${id}"`));
  }
  assert.match(overrides, /\.j-machine-grid\s*\{[^}]*minmax\(min\(100%,16rem\),1fr\)/);
  assert.match(overrides, /\.topbar, body\.jaryo-app \.j-tabs\s*\{\s*min-height:0/);
  assert.match(overrides, /@media \(max-width:700px\)[\s\S]*?> \.j-nav\s*\{[^}]*position:fixed;[^}]*overflow-x:auto/);
  assert.match(overrides, /@media \(max-width:1199px\)[\s\S]*?\.evidence-rail\s*\{[^}]*inset:auto 0 0/);
  const html = readFileSync(new URL('./web/index.html', import.meta.url), 'utf8');
  assert.match(html, /id="feedFreshness"/);
  assert.doesNotMatch(overrides, /#feedFreshness\s*\{[^}]*(?:display:none|visibility:hidden)/);
});

// Specificity as [ids, classes, elements]; enough for the selectors compared below.
function specificity(selector) {
  const flat = selector.replace(/:is\(([^)]*)\)/g, (_, list) => list.split(',')[0]);
  return [(flat.match(/#[\w-]+/g) || []).length,
    (flat.match(/\.[\w-]+|\[[^\]]*\]/g) || []).length,
    (flat.replace(/#[\w-]+|\.[\w-]+|\[[^\]]*\]/g, ' ').match(/\b[a-z]+\b/g) || []).length];
}
const beats = (a, b) => { for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i]; return false; };

test('enlarged text takes the phone nav out of fixed positioning so it cannot wrap into an overlay', () => {
  const simple = readFileSync(new URL('./web/simple-view.css', import.meta.url), 'utf8');
  const start = jaryo.indexOf('@media (max-width:700px) {\n  /* --j-nav-clear');
  const phone = jaryo.slice(start, jaryo.indexOf('\n}\n', start));
  const large = 'html:is([data-text-size="150"],[data-text-size="200"])';
  const rule = selector => {
    const at = phone.indexOf(`${selector} {`);
    assert.ok(at >= 0, `missing rule ${selector}`);
    return phone.slice(at + selector.length + 2, phone.indexOf('}', at));
  };
  const fixed = rule('body.jaryo-app > .j-nav');
  assert.match(fixed, /position:fixed;/);
  assert.match(fixed, /flex-flow:row nowrap;/);
  const flow = rule(`${large} body.jaryo-app > .j-nav`);
  for (const declaration of ['position:static;', 'inset:auto;', 'z-index:auto;', 'min-height:0;', 'flex-wrap:wrap;', 'overflow:visible;']) {
    assert.ok(flow.includes(declaration), `enlarged text nav sets ${declaration}`);
  }
  assert.ok(phone.indexOf(`${large} body.jaryo-app > .j-nav {`) > phone.indexOf('body.jaryo-app > .j-nav {'));
  // The overview also loads simple-view.css, whose enlarged-text nav rule wraps the bar but sets no position.
  const overview = /html:is\(\[data-text-size="150"\],\[data-text-size="200"\]\) body>\.j-nav\{([^}]*)\}/.exec(simple);
  assert.match(overview[1], /flex-wrap:wrap/);
  assert.doesNotMatch(overview[1], /position:/);
  assert.ok(beats(specificity(`${large} body.jaryo-app > .j-nav`), specificity(`${large} body>.j-nav`)),
    'the in-flow rule out-specifies the wrapping rule it is paired with');
  assert.ok(beats(specificity(`${large} body.jaryo-app > .j-nav`), specificity('body.jaryo-app > .j-nav')));
  // Nothing reserves room for a bar that is no longer fixed, and nothing stays lifted above it.
  const reset = rule(`${large} body.jaryo-app`);
  assert.match(reset, /--j-nav-clear:0px;/);
  assert.match(reset, /padding-bottom:env\(safe-area-inset-bottom,0px\);/);
  for (const selector of ['body.jaryo-app .jp-pager', 'body.jaryo-app.fleet-ledger .evidence-rail']) {
    assert.match(rule(selector), /var\(--j-nav-clear\)/, `${selector} follows the shared clearance`);
  }
  assert.match(readFileSync(new URL('./web/monitor.css', import.meta.url), 'utf8'),
    /@media \(max-width:700px\) \{\s*\.j-map #detail:not\(\[hidden\]\) \{ bottom:var\(--j-nav-clear,0px\); \}/);
  assert.doesNotMatch(phone, /(?:bottom|inset):[^;]*56px/, 'offsets above the nav use the shared clearance, not a repeated literal');
});

test('the script-added Commands button keeps its width in the one-row phone nav', () => {
  assert.match(jaryo, /body\.jaryo-app > \.j-nav a, body\.jaryo-app > \.j-nav \.j-theme, body\.jaryo-app > \.j-nav > \.jp-button \{ flex:0 0 auto; white-space:nowrap; \}/);
  assert.match(readFileSync(new URL('./web/jaryo.mjs', import.meta.url), 'utf8'), /jp-button/);
});

test('pages that cover the viewport keep content inside the side safe areas', () => {
  assert.match(jaryo, /body\.jaryo-app \{ padding-left:env\(safe-area-inset-left,0px\); padding-right:env\(safe-area-inset-right,0px\); \}/);
});
