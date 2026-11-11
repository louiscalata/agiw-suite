import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('./web/style.css', import.meta.url), 'utf8');
const rules = [...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(([, selectors, body]) => ({
    selectors: selectors.split(',').map(selector => selector.trim()),
    declarations: Object.fromEntries(body.split(';').filter(item => item.includes(':')).map(item => {
      const colon = item.indexOf(':');
      return [item.slice(0, colon).trim(), item.slice(colon + 1).trim()];
    })),
  }));

function declarationsFor(selector) {
  const matches = rules.filter(rule => rule.selectors.includes(selector));
  assert.ok(matches.length, `${selector} exists`);
  return Object.assign({}, ...matches.map(rule => rule.declarations));
}

const tokens = declarationsFor(':root');
function resolveColor(value) {
  for (let depth = 0; value?.startsWith('var(') && depth < 10; depth++) {
    value = tokens[value.slice(4, -1)];
  }
  return value;
}

function luminance(value) {
  const color = resolveColor(value);
  assert.match(color, /^#[\da-f]{6}$/i, 'contrast assertion requires an opaque six-digit hex color');
  const channels = color.slice(1).match(/../g).map(channel => parseInt(channel, 16) / 255)
    .map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

test('repair submit labels retain contrast on their light normal and hover fills', () => {
  const base = declarationsFor('.fix-details .fix-submit');
  const hover = { ...base, ...declarationsFor('.fix-details .fix-submit:hover:not(:disabled)') };
  for (const state of [base, hover]) {
    const levels = [luminance(state.background), luminance(state.color)].sort((a, b) => b - a);
    assert.ok((levels[0] + 0.05) / (levels[1] + 0.05) >= 7, 'submit text has at least 7:1 contrast');
  }
});

test('ink-aliased fills never declare the same light text color', () => {
  let audited = 0;
  for (const { selectors, declarations } of rules) {
    if (!/^var\(--(?:blue|orange|green|red|violet|ink)\)$/.test(declarations.background)) continue;
    if (!declarations.color) continue;
    audited++;
    assert.notEqual(resolveColor(declarations.color), resolveColor(declarations.background), selectors.join(', '));
  }
  assert.ok(audited >= 5, 'the audit includes submit and other filled text controls');
});

test('rejected, warning and blocked states retain visible border cues without color', () => {
  for (const [selector, property, expected] of [
    ['.host-rejected', 'border-inline-start', /^3px solid /],
    ['.review-warning', 'border', /^2px dashed /],
    ['.review-warning.review-block', 'border', /^3px double /],
    ['.review-badge.warn', 'border', /^2px dashed /],
    ['.review-badge.block', 'border', /^3px double /],
    ['.vital[data-tone=warn]', 'border', /^2px dashed /],
  ]) {
    const declarations = declarationsFor(selector);
    assert.match(declarations[property], expected, selector);
    // Include later media rules so high-contrast overrides cannot silently narrow these borders.
    const [width, style] = declarations[property].split(/\s+/);
    const overrideWidth = declarations[`${property}-width`];
    const overrideStyle = declarations[`${property}-style`];
    if (overrideWidth) assert.ok(overrideWidth.split(/\s+/).every(value => parseFloat(value) >= parseFloat(width)), selector);
    if (overrideStyle) assert.ok(overrideStyle.split(/\s+/).every(value => value === style), selector);
  }
  assert.ok(Number(declarationsFor('.vital[data-tone=warn] small')['font-weight']) >= 600);
});

test('ready and error results use distinct solid and double border cues', () => {
  assert.match(declarationsFor('.mode-action-result.ready').border, /^2px solid /);
  assert.match(declarationsFor('.mode-action-result.error').border, /^3px double /);
});
