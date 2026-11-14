import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { checkPlatformVariants } from './jaryo/contract.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const tokens = JSON.parse(read('./jaryo/tokens.json'));

test('the implemented web variant is scoped separately from the Android foundation', () => {
  assert.deepEqual(checkPlatformVariants(tokens), []);
  const css = read('./web/jaryo.css');
  assert.ok(css.includes(`--j-accent: ${tokens.variants.web.accent};`));
  for (const family of Object.values(tokens.variants.web.fonts)) {
    assert.ok(css.includes(`font-family: "${family}"`), family);
  }
  const sizes = [...css.matchAll(/--j-web-size-\d:\s*(\d+)px;/g)].map(match => Number(match[1]));
  assert.deepEqual(sizes, tokens.variants.web.typeScalePx);
  for (let i = 1; i <= 6; i++) assert.ok(css.includes(`--j-size-${i}: var(--j-web-size-${i});`));
  assert.match(read('./android/app/src/main/res/values/themes.xml'), /android:fontFamily">@font\/inter</);
  assert.doesNotMatch(read('./android/app/src/main/res/values/colors.xml'), /C47A3A|c47a3a/);
});

test('variant validation rejects cross-platform drift and invented approvals', () => {
  const mutations = [
    t => { t.provisionalTypeface = 'IBM Plex Sans'; },
    t => { t.surface = 'red'; },
    t => { t.variants.android.fonts = t.variants.web.fonts; },
    t => { t.variants.web.scope = 'all-platforms'; },
    t => { t.variants.web.fonts.text = 'Jost'; },
    t => { t.variants.web.accent = '#ff0000'; },
    t => { t.variants.web.typeScalePx = [12, 21, 34]; },
    t => { t.appliedChoiceFile = 'invented'; },
    t => { t.chosenFamilyIds = { text: 'invented' }; },
  ];
  for (const mutate of mutations) {
    const changed = structuredClone(tokens);
    mutate(changed);
    assert.ok(checkPlatformVariants(changed).length > 0, mutate.toString());
  }
});


// Markup-backed source check: derive each counter's tag from the shipped page,
// then require a matching explicit selector in the final Mono override. A rule
// for a nonexistent dd is insufficient. This source check does not claim native computed-font verification.
function assertOverviewCounterFonts(html, css) {
  const finalScale = css.slice(css.indexOf('Five readable roles across every monitor screen'));
  const monoRule = [...finalScale.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .find(([, , declarations]) => /font-family:\s*"IBM Plex Mono"[^;]*!important;/.test(declarations));
  assert.ok(monoRule, 'final Mono override exists after the blanket Sans rule');
  const selectors = monoRule[1].split(',').map(value => value.trim());
  const metrics = html.match(/<div class="metrics fleet-counters"[^>]*>([\s\S]*?)<aside\b/);
  assert.ok(metrics, 'actual overview metrics region exists');
  for (const id of ['activeCount', 'loadedCount', 'queuedCount', 'observedClientCount']) {
    const region = id === 'observedClientCount' ? html : metrics[1];
    const counter = region.match(new RegExp(`<([a-z][a-z0-9]*)\\b[^>]*\\bid="${id}"[^>]*>`));
    assert.ok(counter, `${id} exists in its real markup region`);
    const selector = id === 'observedClientCount'
      ? `body.jaryo-app #${id}` : `body.jaryo-app .metrics ${counter[1]}`;
    assert.ok(selectors.includes(selector), `${id} (${counter[1]}) must match the Mono override`);
  }
  assert.match(metrics[1], /<span>lanes answering<\/span>/);
  assert.ok(!selectors.includes('body.jaryo-app .metrics span'), 'counter labels remain body Sans');
  assert.ok(!selectors.includes('body.jaryo-app .metrics'), 'counter container must not change text roles');
}

test('actual overview counters use Mono while their text labels stay Sans', () => {
  assertOverviewCounterFonts(read('./web/index.html'), read('./web/jaryo.css'));
});

test('counter font coverage rejects nonexistent selectors, missing counters and tag drift', () => {
  const html = read('./web/index.html');
  const css = read('./web/jaryo.css');
  assert.throws(() => assertOverviewCounterFonts(html,
    css.replaceAll('body.jaryo-app .metrics strong,', 'body.jaryo-app .metrics dd,')), /activeCount.*Mono/);
  assert.throws(() => assertOverviewCounterFonts(html,
    css.replace('body.jaryo-app #observedClientCount,', 'body.jaryo-app #missingCounter,')), /observedClientCount.*Mono/);
  assert.throws(() => assertOverviewCounterFonts(html.replace('id="activeCount"', 'id="missingCounter"'), css), /activeCount exists/);
  assert.throws(() => assertOverviewCounterFonts(html.replace('<strong id="activeCount"', '<span id="activeCount"'), css), /activeCount.*Mono/);
});
