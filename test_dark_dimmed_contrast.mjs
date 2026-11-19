// Dark-theme dimmed or muted text must reach 4.5:1 on pure black (WCAG 2.x).
// Source-only CSS arithmetic, no browser: resolve the shipped muted tokens and
// secondary-text colours, composite any text opacity over #000000, then apply
// the WCAG relative-luminance contrast formula. The test fails if a colour
// drops below the floor, so a future dimming regression cannot pass silently.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = name => readFileSync(new URL(name, import.meta.url), 'utf8');
const sources = {
  style: read('./web/style.css'),
  jaryo: read('./web/jaryo.css'),
  monitor: read('./web/monitor.css'),
  components: read('./web/components.css'),
  primitives: read('./jaryo/primitives.css'),
};

const BLACK = '#000000';
const MIN = 4.5;
const HEX = /^#[0-9a-f]{6}$/i;

// WCAG 2.x relative luminance: sRGB channel linearisation, then the
// Rec. 709 weighted sum. Black has luminance 0, so the ratio is L/0.05 + 1.
function linear(channel) {
  const c = channel / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}
function luminance(color) {
  assert.ok(HEX.test(color), `expected opaque six-digit hex, got ${color}`);
  const raw = color.slice(1).match(/../g).map(v => parseInt(v, 16));
  return 0.2126 * linear(raw[0]) + 0.7152 * linear(raw[1]) + 0.0722 * linear(raw[2]);
}
function ratioOnBlack(color) {
  return (luminance(color) + 0.05) / 0.05;
}
function compositeOnBlack(color, opacity) {
  assert.ok(opacity > 0 && opacity <= 1, `opacity out of range: ${opacity}`);
  if (opacity === 1) return color;
  assert.ok(HEX.test(color), `expected opaque six-digit hex, got ${color}`);
  const channels = color.slice(1).match(/../g).map(v => Math.round(parseInt(v, 16) * opacity));
  return '#' + channels.map(v => v.toString(16).padStart(2, '0')).join('');
}
function check(label, file, color, opacity = 1) {
  const effective = compositeOnBlack(color, opacity);
  const ratio = ratioOnBlack(effective);
  assert.ok(ratio >= MIN,
    `${label} (${file}): ${effective} = ${ratio.toFixed(2)}:1 on ${BLACK}, need >= ${MIN}:1`);
  return { label, file, color, effective, ratio };
}

// Minimal CSS reader: comments off, then each `selector { ... }` declaration
// block. At-rule preludes are kept as their own empty entries; nested rules are
// still found independently, which is all this audit needs.
function declarations(source, selector, property) {
  const text = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(escaped + '\\s*\\{([^}]*)\\}', 'g');
  const found = [];
  for (const match of text.matchAll(pattern)) {
    for (const item of match[1].split(';')) {
      const colon = item.indexOf(':');
      if (colon < 0) continue;
      if (item.slice(0, colon).trim() !== property) continue;
      found.push(item.slice(colon + 1).trim().replace(/!important$/, '').trim());
    }
  }
  return found;
}
function hexValues(source, selector, property) {
  return declarations(source, selector, property).filter(value => HEX.test(value));
}
function firstHex(source, selector, property, label) {
  const values = hexValues(source, selector, property);
  assert.ok(values.length, `no opaque hex ${property} for ${label}`);
  return values[0];
}
function opacityOf(source, selector, label) {
  const values = declarations(source, selector, 'opacity').map(Number);
  assert.ok(values.length, `no opacity for ${label}`);
  return values[values.length - 1];
}

// The dark tokens these secondary-text rules reference.
const tokens = {
  '--muted': firstHex(sources.style, ':root', '--muted', '--muted'),
  '--ink': firstHex(sources.style, ':root', '--ink', '--ink'),
  '--portfolio-soft': firstHex(sources.style, ':root', '--portfolio-soft', '--portfolio-soft'),
  '--j-muted': firstHex(sources.jaryo, ':root', '--j-muted', '--j-muted'),
};
function resolve(value) {
  const match = /^var\((--[a-z-]+)(?:\s*,[^)]*)?\)$/.exec(value);
  if (!match) return value;
  assert.ok(tokens[match[1]], `unresolved token ${match[1]}`);
  return tokens[match[1]];
}

function audit() {
  const results = [];
  const add = (label, file, source, selector, property, options = {}) => {
    const value = declarations(source, selector, property)[0];
    assert.ok(value, `missing ${property} in ${selector}`);
    const color = resolve(value);
    assert.ok(HEX.test(color), `${label}: ${value} did not resolve to opaque hex`);
    const opacity = options.opacity !== undefined
      ? options.opacity
      : options.opacitySelector
        ? opacityOf(source, options.opacitySelector, label)
        : 1;
    results.push(check(label, file, color, opacity));
  };

  // 1. The muted CSS custom properties in every dark-theme declaration block.
  for (const hex of hexValues(sources.style, ':root', '--muted')) {
    results.push(check('--muted (dark theme)', 'web/style.css', hex));
  }
  for (const [name, source, file] of [
    ['--j-muted', sources.jaryo, 'web/jaryo.css'],
    ['--j-muted', sources.primitives, 'jaryo/primitives.css'],
  ]) {
    for (const selector of [':root', 'html[data-theme="dark"]']) {
      for (const hex of hexValues(source, selector, name)) {
        results.push(check(`${name} (dark theme)`, file, hex));
      }
    }
  }

  // 2. Hard-coded grey/tinted secondary text in web/*.css.
  add('.eyebrow', 'web/components.css', sources.components, '.eyebrow', 'color');
  add('dt', 'web/components.css', sources.components, 'dt', 'color');
  add('footer', 'web/components.css', sources.components, 'footer', 'color');
  add('h2 span', 'web/components.css', sources.components, 'h2 span', 'color');
  add('#result', 'web/components.css', sources.components, '#result', 'color');
  add('pre', 'web/components.css', sources.components, 'pre', 'color');
  // Disabled buttons keep the normal button ink and only carry the alpha.
  add('button:disabled', 'web/components.css', sources.components, 'button', 'color',
    { opacitySelector: 'button:disabled' });

  add('input::placeholder', 'web/style.css', sources.style, 'input::placeholder', 'color');
  add('.model-action-dock button:disabled', 'web/style.css', sources.style, '.model-action-dock button:disabled', 'color');
  add('.fix-details .fix-submit:disabled', 'web/style.css', sources.style, '.fix-details .fix-submit:disabled', 'color');
  add('.sidebar .eyebrow', 'web/style.css', sources.style, '.sidebar .eyebrow', 'color');
  add('.sidebar .quiet', 'web/style.css', sources.style, '.sidebar .quiet', 'color');
  add('.sidebar .metrics span', 'web/style.css', sources.style, '.sidebar .metrics span', 'color');
  add('.sidebar .view-tabs button', 'web/style.css', sources.style, '.sidebar .view-tabs button', 'color');
  add('.brand-wordmark small', 'web/style.css', sources.style, '.brand-wordmark small', 'color');
  add('.legend .agent-key span', 'web/style.css', sources.style, '.legend .agent-key span', 'color');
  add('.run-origin', 'web/style.css', sources.style, '.run-origin', 'color');
  add('.summary-note', 'web/style.css', sources.style, '.summary-note', 'color');
  add('.tile-label', 'web/style.css', sources.style, '.tile-label', 'color');
  add('.panel-label', 'web/style.css', sources.style, '.panel-label', 'color');
  add('.panel-eyebrow', 'web/style.css', sources.style, '.panel-eyebrow', 'color');
  add('.step-line', 'web/style.css', sources.style, '.step-line', 'color');
  add('.activity-empty', 'web/style.css', sources.style, '.activity-empty', 'color');
  add('.route-runs-head', 'web/style.css', sources.style, '.route-runs-head', 'color');

  add('.j-map-note', 'web/monitor.css', sources.monitor, '.j-map-note', 'color');
  add('.j-map-key', 'web/monitor.css', sources.monitor, '.j-map-key', 'color');

  add('.j-eyebrow', 'web/jaryo.css', sources.jaryo, '.j-eyebrow', 'color');
  add('.j-identity dt', 'web/jaryo.css', sources.jaryo, '.j-identity dt, .j-metrics dt', 'color');
  add('.j-filter', 'web/jaryo.css', sources.jaryo, '.j-filter', 'color');
  add('.j-map dt', 'web/jaryo.css', sources.jaryo, '.j-map dt', 'color');
  add('.j-brand span', 'web/jaryo.css', sources.jaryo, '.j-brand span', 'color');
  add('.j-page button:disabled', 'web/jaryo.css', sources.jaryo, '.j-page button:disabled', 'color');
  add('.j-map .node .sub', 'web/jaryo.css', sources.jaryo, '.j-map .node .sub', 'fill');
  add('.jp-step', 'web/jaryo.css', sources.jaryo, '.jp-step', 'color');

  // 3. Text rendered below full opacity; effective colour is blended over black.
  add('.cluster-label', 'web/style.css', sources.style, '.cluster-label', 'fill',
    { opacitySelector: '.cluster-label' });
  // The .96 opacity is declared on the shared rule whose last selector is
  // .node.windows-worker .label, while the fill is set on the hub rule.
  add('.node.hub .label', 'web/style.css', sources.style, '.node.hub .label', 'fill',
    { opacitySelector: '.node.windows-worker .label' });
  // The small warning text inherits its colour from .review-warning and only
  // carries the opacity, so pair the inherited ink with that rule's alpha.
  const warningInk = resolve(declarations(sources.style, '.review-warning', 'color').pop());
  results.push(check('.review-warning small (inherited ink)', 'web/style.css', warningInk,
    opacityOf(sources.style, '.review-warning small', '.review-warning small')));

  return results;
}

test('dark-theme dimmed and muted text clears 4.5:1 on pure black', () => {
  const results = audit();
  assert.ok(results.length >= 40, `expected a full audit, got ${results.length} cases`);
  const lowest = results.reduce((a, b) => (a.ratio <= b.ratio ? a : b));
  // A concrete assertion in addition to per-case checks keeps the floor honest.
  assert.ok(lowest.ratio >= MIN, `${lowest.label} is only ${lowest.ratio.toFixed(2)}:1`);
});

test('the guard actually rejects a sub-AA colour', () => {
  assert.ok(ratioOnBlack('#757575') >= MIN, '#757575 should sit just above the floor');
  assert.throws(() => check('canary', 'test', '#666666'), /need >= 4\.5:1/);
});
