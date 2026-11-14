// Overview map contrast (M26). Selecting or hovering a node used to fade every unrelated node as a
// whole group, so the captions and status words under it fell to about 1.5:1. This test resolves the
// web/style.css cascade for opacity and fill (CSS arithmetic, no browser), composites each caption
// over the map background and checks the measured ratio: at least 4.5:1 for text and 3:1 for the
// node mark (WCAG 1.4.3 and 1.4.11). De-emphasis may only touch decoration (edges, rings, halos) and
// the mark down to its 3:1 floor. The rendered result is the hosted overview-ink check.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const styleCss = readFileSync(new URL('./web/style.css', import.meta.url), 'utf8');
const jaryoCss = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');

function parseRules(css) {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = [];
  const context = [];
  let start = 0;
  let order = 0;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '}') { context.pop(); start = i + 1; continue; }
    if (char !== '{') continue;
    const prelude = text.slice(start, i).trim();
    if (prelude.startsWith('@')) {
      context.push(prelude.replace(/\s+/g, ''));
      start = i + 1;
      continue;
    }
    const end = text.indexOf('}', i);
    const declarations = [];
    for (const item of text.slice(i + 1, end).split(';')) {
      const colon = item.indexOf(':');
      if (colon < 0) continue;
      let value = item.slice(colon + 1).trim();
      const important = /!important$/.test(value);
      if (important) value = value.replace(/\s*!important$/, '');
      declarations.push({ property: item.slice(0, colon).trim(), value, important });
    }
    rules.push({ selectors: splitSelectors(prelude), declarations, at: [...context], order: order++ });
    i = end;
    start = end + 1;
  }
  return rules;
}

function splitSelectors(prelude) {
  const out = [];
  let depth = 0;
  let current = '';
  for (const char of prelude) {
    if (char === '(') depth++;
    if (char === ')') depth--;
    if (char === ',' && depth === 0) { out.push(current.trim()); current = ''; } else current += char;
  }
  if (current.trim()) out.push(current.trim());
  return out;
}

const rules = parseRules(styleCss);

// Only the media this test models are live; every width query and reduced motion are left out, and
// forced colors has its own hosted check (forced-colors-selection).
function atRulesActive(at, env) {
  return at.every(rule => {
    if (rule.startsWith('@supports')) return true;
    if (rule === '@media(prefers-contrast:more)') return env.moreContrast;
    return false;
  });
}

// Compound selectors made of a tag or id, classes and :not(.class). Anything else (hover, focus,
// :has, attributes, child combinators) does not match the resting states modelled here.
function parseCompound(source) {
  const compound = { tag: null, id: null, classes: [], not: [], root: false };
  const pattern = /^(?:([a-z][a-z0-9-]*)|#([\w-]+)|\.([\w-]+)|:not\(\.([\w-]+)\)|(:root))/i;
  let rest = source;
  while (rest) {
    const match = rest.match(pattern);
    if (!match) return null;
    if (match[1]) compound.tag = match[1].toLowerCase();
    if (match[2]) compound.id = match[2];
    if (match[3]) compound.classes.push(match[3]);
    if (match[4]) compound.not.push(match[4]);
    if (match[5]) compound.root = true;
    rest = rest.slice(match[0].length);
  }
  return compound;
}

function parseSelector(selector) {
  if (/[>+~[]/.test(selector)) return null;
  const compounds = selector.split(/\s+/).map(parseCompound);
  return compounds.every(Boolean) ? compounds : null;
}

function matchesElement(compound, element) {
  if (compound.root) return element.tag === 'html';
  if (compound.tag && compound.tag !== element.tag) return false;
  if (compound.id && compound.id !== element.id) return false;
  if (!compound.classes.every(name => element.classes.includes(name))) return false;
  return !compound.not.some(name => element.classes.includes(name));
}

function matches(compounds, chain) {
  const last = compounds.length - 1;
  if (!matchesElement(compounds[last], chain[chain.length - 1])) return false;
  let index = chain.length - 2;
  for (let c = last - 1; c >= 0; c--) {
    while (index >= 0 && !matchesElement(compounds[c], chain[index])) index--;
    if (index < 0) return false;
    index--;
  }
  return true;
}

function specificity(compounds) {
  let ids = 0;
  let classes = 0;
  let tags = 0;
  for (const compound of compounds) {
    if (compound.id) ids++;
    classes += compound.classes.length + compound.not.length + (compound.root ? 1 : 0);
    if (compound.tag) tags++;
  }
  return ids * 1e6 + classes * 1e3 + tags;
}

function computed(chain, property, env) {
  let best = null;
  for (const rule of rules) {
    if (!atRulesActive(rule.at, env)) continue;
    for (const declaration of rule.declarations) {
      if (declaration.property !== property) continue;
      for (const selector of rule.selectors) {
        const compounds = parseSelector(selector);
        if (!compounds || !matches(compounds, chain)) continue;
        const rank = [declaration.important ? 1 : 0, specificity(compounds), rule.order];
        if (!best || rank[0] > best.rank[0] || (rank[0] === best.rank[0]
            && (rank[1] > best.rank[1] || (rank[1] === best.rank[1] && rank[2] >= best.rank[2])))) {
          best = { rank, value: declaration.value, selector };
        }
      }
    }
  }
  return best;
}

const html = { tag: 'html', classes: [] };

function token(name, env) {
  const hit = computed([html], name, env);
  assert.ok(hit, `${name} is defined on :root`);
  return hit.value;
}

function resolve(value, env) {
  for (let depth = 0; /^var\(--[\w-]+\)$/.test(value) && depth < 10; depth++) value = token(value.slice(4, -1), env);
  return value;
}

function rgb(hex) {
  assert.match(hex, /^#(?:[\da-f]{3}|[\da-f]{6})$/i, `contrast needs an opaque hex color, got ${hex}`);
  const digits = hex.length === 4 ? [...hex.slice(1)].map(digit => digit + digit).join('') : hex.slice(1);
  return digits.match(/../g).map(channel => parseInt(channel, 16));
}

function luminance(channels) {
  const [r, g, b] = channels.map(channel => channel / 255)
    .map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
  return r * 0.2126 + g * 0.7152 + b * 0.0722;
}

// Browsers composite group and element opacity in sRGB, so the painted color is a straight blend.
export function paintedContrast(foreground, background, alpha) {
  const fg = rgb(foreground);
  const bg = rgb(background);
  const painted = fg.map((channel, i) => alpha * channel + (1 - alpha) * bg[i]);
  const levels = [luminance(painted), luminance(bg)].sort((a, b) => b - a);
  return (levels[0] + 0.05) / (levels[1] + 0.05);
}

function chainFor({ kind, states = [], mapFocus, part, jaryo = false }) {
  const tag = part === 'core' ? 'circle' : 'text';
  return [
    html,
    { tag: 'body', classes: jaryo ? ['jaryo-app'] : [] },
    { tag: 'main', classes: [] },
    { tag: 'div', id: 'graphRegion', classes: ['graph-region'] },
    { tag: 'svg', id: 'map', classes: mapFocus ? ['map-focus-active'] : [] },
    { tag: 'g', id: 'nodes', classes: [] },
    { tag: 'g', classes: ['node', ...kind.split(' '), ...states] },
    { tag, classes: [part] },
  ];
}

function opacityOf(chain, env) {
  let alpha = 1;
  for (let i = 1; i <= chain.length; i++) {
    const hit = computed(chain.slice(0, i), 'opacity', env);
    if (hit) alpha *= parseFloat(hit.value);
  }
  return alpha;
}

// Node kinds app.js draws on the Overview map (web/app.js class list), and the states it adds.
const KINDS = ['model', 'model root', 'hub', 'client', 'client-model', 'clients', 'windows-lane', 'windows-worker',
  'mac-model', 'afm', 'jev', 'pipeline', 'registry-machine', 'registry-lane'];
const STATUS = [[], ['active'], ['unknown'], ['in-flight'], ['loaded'], ['verified'], ['active', 'in-flight']];
// The interaction states: resting, selected, selected while another node is hovered (the M26 case),
// related to the focus, unrelated to the focus, and search matches and misses.
const INTERACTION = [
  { name: 'resting', mapFocus: false, states: [] },
  { name: 'selected', mapFocus: true, states: ['selected', 'near', 'spotlight'] },
  { name: 'selected, another node hovered', mapFocus: true, states: ['selected'] },
  { name: 'near the focus', mapFocus: true, states: ['near'] },
  { name: 'unrelated to the focus', mapFocus: true, states: [] },
  { name: 'search miss', mapFocus: false, states: ['fade'] },
  { name: 'search miss, unrelated to the focus', mapFocus: true, states: ['fade'] },
  { name: 'selected search miss', mapFocus: true, states: ['selected', 'near', 'spotlight', 'fade'] },
  { name: 'search match', mapFocus: false, states: ['match'] },
];
const ENVS = [{ name: 'default', moreContrast: false }, { name: 'increased contrast', moreContrast: true }];

function* cases() {
  for (const env of ENVS) for (const kind of KINDS) for (const status of STATUS) for (const interaction of INTERACTION) {
    yield { env, kind, states: [...status, ...interaction.states], mapFocus: interaction.mapFocus, label: `${env.name}, ${kind}, ${[...status].join('+') || 'idle'}, ${interaction.name}` };
  }
}

function textFill(chain, env) {
  const fill = computed(chain, 'fill', env)?.value ?? 'var(--ink)';
  return resolve(fill === 'currentColor' ? 'var(--ink)' : fill, env);
}

test('the parser reads the rules this test depends on', () => {
  const selected = chainFor({ kind: 'model', states: ['selected'], mapFocus: true, part: 'label' });
  assert.equal(computed(selected, 'opacity', ENVS[0]).value, '1', 'a selected label is shown');
  assert.equal(textFill(chainFor({ kind: 'model', part: 'sub' }), ENVS[0]).toLowerCase(), resolve('var(--muted)', ENVS[0]).toLowerCase());
  assert.notEqual(resolve('var(--muted)', ENVS[1]), resolve('var(--muted)', ENVS[0]), 'increased contrast changes --muted');
  assert.ok(paintedContrast('#ffffff', '#000000', 1) > 20.9);
  assert.ok(Math.abs(paintedContrast('#777777', '#ffffff', 1) - 4.48) < 0.01);
});

test('no rule fades a whole map node, so captions keep their own opacity', () => {
  for (const rule of rules) {
    for (const declaration of rule.declarations) {
      if (declaration.property !== 'opacity' || parseFloat(declaration.value) >= 1) continue;
      for (const selector of rule.selectors) {
        const last = selector.trim().split(/\s+/).at(-1);
        assert.ok(!/^\.node(?:[.:]|$)/.test(last), `${selector}{opacity:${declaration.value}} fades the node group, captions included`);
      }
    }
  }
  assert.doesNotMatch(jaryoCss.replace(/\/\*[\s\S]*?\*\//g, ''), /\.node[^{}]*\{[^}]*\bopacity\s*:\s*0?\.\d/, 'jaryo.css does not fade map nodes');
});

test('every shown caption on the map keeps at least 4.5:1 against the map in every state', () => {
  let shown = 0;
  for (const item of cases()) {
    const background = resolve('var(--paper)', item.env);
    for (const part of ['label', 'sub', 'capability-line']) {
      const chain = chainFor({ ...item, part });
      const alpha = opacityOf(chain, item.env);
      const ownOpacity = parseFloat(computed(chain, 'opacity', item.env)?.value ?? '1');
      if (ownOpacity === 0) continue; // hidden until hover, focus or selection; not painted
      shown++;
      const ratio = paintedContrast(textFill(chain, item.env), background, alpha);
      assert.ok(ratio >= 4.5, `${item.label}: ${part} paints at ${ratio.toFixed(2)}:1 (opacity ${alpha.toFixed(3)})`);
    }
  }
  assert.ok(shown > 500, `the audit painted ${shown} captions`);
});

test('the selected node keeps full-opacity captions even while another node is hovered', () => {
  for (const env of ENVS) for (const kind of KINDS) for (const part of ['label', 'sub']) {
    const chain = chainFor({ kind, states: ['selected'], mapFocus: true, part });
    const own = parseFloat(computed(chain, 'opacity', env)?.value ?? '1');
    if (own === 0) continue;
    assert.ok(opacityOf(chain, env) >= 0.88, `${env.name}, ${kind} ${part}: ${opacityOf(chain, env)}`);
  }
});

// The selected node is exempt from the unrelated-to-focus dimming (:not(.selected)). Its mark keeps the
// opacity it has with no focus at all: full opacity for every kind except client-model, whose core is
// always .6. A .5 core would still pass the 3:1 mark check, so this is asserted on its own.
test('the selected node mark keeps full opacity while another node is hovered', () => {
  for (const env of ENVS) for (const kind of KINDS) for (const status of STATUS) {
    const label = `${env.name}, ${kind}, ${status.join('+') || 'idle'}`;
    const resting = opacityOf(chainFor({ kind, states: [...status], mapFocus: false, part: 'core' }), env);
    assert.equal(resting, kind === 'client-model' ? 0.6 : 1, `${label}: resting mark opacity ${resting}`);
    const hovered = opacityOf(chainFor({ kind, states: [...status, 'selected'], mapFocus: true, part: 'core' }), env);
    assert.equal(hovered, resting, `${label}, selected, another node hovered: mark dims to ${hovered}`);
  }
});

test('a de-emphasized node mark stays at 3:1 or better against the map', () => {
  for (const item of cases()) {
    if (item.states.includes('unknown')) continue; // a hollow ring in the background colour, drawn by its stroke
    const chain = chainFor({ ...item, part: 'core' });
    const fill = computed(chain, 'fill', item.env)?.value;
    const color = resolve(!fill || fill === 'currentColor' ? 'var(--ink)' : fill, item.env);
    const ratio = paintedContrast(color, resolve('var(--paper)', item.env), opacityOf(chain, item.env));
    assert.ok(ratio >= 3, `${item.label}: mark paints at ${ratio.toFixed(2)}:1`);
  }
});

function jaryoTheme(name) {
  const block = jaryoCss.match(new RegExp(`html\\[data-theme="${name}"\\]\\s*\\{([^}]*)\\}`));
  assert.ok(block, `jaryo.css defines the ${name} theme`);
  const value = property => block[1].match(new RegExp(`${property}:\\s*(#[\\da-fA-F]{6})`))[1];
  return { text: value('--j-text'), bg: value('--j-bg') };
}

test('the Jaryo black-and-white map keeps the same ratios in its dark and light themes', () => {
  assert.match(jaryoCss, /body\.jaryo-app #nodes \.node \.label,\s*body\.jaryo-app #nodes \.node \.sub,\s*body\.jaryo-app #nodes \.node \.capability-line \{ fill: var\(--j-text\)/);
  for (const theme of [jaryoTheme('dark'), jaryoTheme('light')]) {
    for (const item of cases()) {
      for (const part of ['label', 'sub', 'capability-line']) {
        const chain = chainFor({ ...item, part, jaryo: true });
        if (parseFloat(computed(chain, 'opacity', item.env)?.value ?? '1') === 0) continue;
        const ratio = paintedContrast(theme.text, theme.bg, opacityOf(chain, item.env));
        assert.ok(ratio >= 4.5, `Jaryo ${theme.bg}: ${item.label}: ${part} paints at ${ratio.toFixed(2)}:1`);
      }
      if (item.states.includes('unknown')) continue;
      const ratio = paintedContrast(theme.text, theme.bg, opacityOf(chainFor({ ...item, part: 'core', jaryo: true }), item.env));
      assert.ok(ratio >= 3, `Jaryo ${theme.bg}: ${item.label}: mark paints at ${ratio.toFixed(2)}:1`);
    }
  }
});
