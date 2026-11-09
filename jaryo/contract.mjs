/** File-level contract for the kit. Returns human-readable problems. */
import { readFileSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { KIT_FILES } from './install.mjs';

const MARKS = {
  'marks/d1-stamp.svg': 'ed0d82c886f58d4fb4e1d106e8b699df2436dc41a81fddc1d053386f810682ed',
  'marks/d2-ledger.svg': '3a15915f480ca68ad68feafbea35c06ced2e0adbc316ce78eb38b2f275d9b33a',
  'marks/d3-schematic.svg': '4affbffc3f8866bf798119f0243fe750c21e7b5ef8c24953bf69af342b4054fa',
  'marks/d4-split-before.svg': '2bca4dbfeffce1fd600f38074fb9cfcafac4e84313be735d86ba5f1d7aef4289',
  'marks/d4-split-on-dark.svg': 'd326f1051332217945b4e85ec1987669cf5e16043ddced03f7015df14650f0c5',
  'marks/d4-split-on-light.svg': '0f197fc6fdf21d137b9b59ac028c3a7785fc58202df1001ebf03f9aa6154546c',
  'marks/d4-split.svg': '3bb8ffba6ca98de1d0778afbf8c05cbf2df64f9212f1066c156eeb04b1092da5',
  'marks/d4-split-wordmark.svg': '82bca63e56b0d44f393a128d19527aff69965f5801f29af43166724dda5bbf16',
  'marks/d5-signal.svg': 'e3bdc18f5ee392c1a379ac66ce668abdb305d3c43ff2ce2b06daad856dced415',
  'marks/favicon.svg': '827a2ab31f0f69c8cd33c00945f538414bd8be90f768f18d99373450e9780477',
};

const SCALE = {
  '--j-space-1': '4px',
  '--j-space-2': '8px',
  '--j-space-3': '12px',
  '--j-space-4': '16px',
  '--j-space-5': '24px',
  '--j-space-6': '32px',
  '--j-space-7': '48px',
  '--j-space-8': '64px',
  '--j-type-meta': '11px',
  '--j-type-ui': '13px',
  '--j-type-body': '14px',
  '--j-type-name': '16px',
  '--j-type-title': '20px',
  '--j-type-display': '28px',
  '--j-leading-meta': '1.4',
  '--j-leading-ui': '1.4',
  '--j-leading-body': '1.45',
  '--j-leading-title': '1.2',
  '--j-leading-display': '1.15',
  '--j-tracking-wordmark': '0.02em',
  '--j-tracking-meta': '0.08em',
  '--j-border-width': '1px',
  '--j-focus-width': '3px',
  '--j-focus-offset': '3px',
  '--j-radius': '0px',
  '--j-control-min': '44px',
  '--j-sidebar': '220px',
  '--j-grid-size': '24px',
  '--j-bp-cover': '300px',
  '--j-bp-handset': '599px',
  '--j-bp-inner': '979px',
  '--j-bp-desktop': '980px',
  '--j-font-display': 'Inter, "Liberation Sans", "Noto Sans", sans-serif',
};

function read(root, rel) {
  return readFileSync(join(root, rel), 'utf8');
}

function sha256(root, rel) {
  return createHash('sha256').update(readFileSync(join(root, rel))).digest('hex');
}

export function contrastRatio(hexA, hexB) {
  const lum = (hex) => {
    const n = Number.parseInt(hex.slice(1), 16);
    const channels = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((channel) => {
      const s = channel / 255;
      return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  };
  const [hi, lo] = [lum(hexA), lum(hexB)].sort((a, b) => b - a);
  return (hi + 0.05) / (lo + 0.05);
}

export function checkKit(root) {
  const problems = [];
  const fail = (message) => problems.push(message);
  for (const rel of KIT_FILES) {
    try {
      if (!statSync(join(root, rel)).isFile()) fail(`${rel} is not a file`);
    } catch (_) {
      fail(`${rel} is missing`);
    }
  }
  let tokens = '';
  try { tokens = read(root, 'tokens.json'); }
  catch (_) { tokens = ''; }
  for (const piece of [
    '"appliedChoiceFile": null',
    '"chosenFamilyIds": {}',
    '"logoSelection": null',
    '"selectedDirection": null',
    '"rejectedDirection": "d4-split"',
    '"provisionalTypeface": "Inter"',
    '"artworkApproval": null',
    '"surface": "black-white"',
    '"chromeMark": "type-agiw"',
  ]) {
    if (!tokens.includes(piece)) fail(`tokens.json lost ${piece}`);
  }
  if (/Downloads|curated-85|F17\.01/.test(tokens)) fail('tokens.json names a choice file');
  let scale;
  try { scale = JSON.parse(tokens).scale; }
  catch (_) { scale = null; }
  if (!scale) fail('tokens.json has no scale');
  const primitives = (() => { try { return read(root, 'primitives.css'); } catch (_) { return ''; } })();
  if (!/@media \(prefers-color-scheme: light\)\s*\{\s*html:not\(\[data-theme="dark"\]\)/.test(primitives)) {
    fail('primitives.css lost the system light rule');
  }
  for (const token of ['--j-text', '--j-bg', '--j-border', '--j-invert', '--j-invert-ink', '--j-focus', '--j-motion']) {
    if (!primitives.includes(`${token}:`)) fail(`primitives.css lost ${token}`);
  }
  if (!primitives.includes('#000000') || !primitives.includes('#ffffff')) fail('primitives.css lost black or white');
  for (const [name, value] of Object.entries(SCALE)) {
    if (!primitives.includes(`${name}: ${value};`)) fail(`primitives.css missing ${name}: ${value}`);
  }
  if (scale && scale.motion !== '240ms') fail('scale motion is not 240ms');
  if (!primitives.includes('--j-motion: 240ms')) fail('primitives.css motion duration drifted');
  const ratio = contrastRatio('#000000', '#ffffff');
  if (ratio < 7) fail(`black/white contrast ${ratio} is below 7:1`);
  for (const [rel, hash] of Object.entries(MARKS)) {
    try {
      const actual = sha256(root, rel);
      if (actual !== hash) fail(`${rel} changed (${actual})`);
    } catch (_) {
      fail(`${rel} missing`);
    }
  }
  const components = (() => { try { return read(root, 'components.css'); } catch (_) { return ''; } })();
  const layout = (() => { try { return read(root, 'layout.css'); } catch (_) { return ''; } })();
  const motion = (() => { try { return read(root, 'motion.css'); } catch (_) { return ''; } })();
  const demoCss = (() => { try { return read(root, 'demo/demo.css'); } catch (_) { return ''; } })();
  for (const piece of [
    '.jk-button:hover',
    '.jk-button:focus-visible',
    '.jk-button:active',
    '.jk-button:disabled',
    '.jk-button[aria-busy="true"]',
    '.jk-field[aria-invalid="true"]',
    '.jk-empty',
    '.jk-alert',
    '.jk-station[aria-pressed="true"]',
    'outline: var(--j-focus-width) dashed var(--j-focus)',
    'forced-colors: active',
  ]) {
    if (!components.includes(piece)) fail(`components.css missing ${piece}`);
  }
  if (!layout.includes('@media (max-width: 300px)')) fail('layout.css missing the cover breakpoint');
  if (!layout.includes('@media (max-width: 599px)')) fail('layout.css missing the handset breakpoint');
  if (!layout.includes('@media (min-width: 980px)')) fail('layout.css missing the desktop sidebar breakpoint');
  if (!layout.includes('grid-template-columns: var(--j-sidebar)')) fail('desktop sidebar width is not the sidebar token');
  if (!/@media \(prefers-reduced-motion: reduce\)/.test(motion)) fail('motion.css missing reduced motion');
  if (!/@media \(prefers-reduced-motion: no-preference\)/.test(motion)) fail('motion.css missing the motion gate');
  if (!motion.includes('animation: none')) fail('reduced motion does not remove animation');
  for (const [name, css] of [['components.css', components], ['layout.css', layout], ['motion.css', motion], ['demo/demo.css', demoCss]]) {
    if (/#[0-9a-fA-F]{3,8}\b/.test(css)) fail(`${name} introduces a hex colour`);
    if (/opacity\s*:/.test(css)) fail(`${name} uses opacity`);
    for (const line of css.split('\n')) {
      const radius = /border-radius\s*:\s*([^;]+)/.exec(line);
      if (radius && radius[1].trim() !== 'var(--j-radius)' && radius[1].trim() !== '0' && radius[1].trim() !== '0px') {
        fail(`${name} rounds a corner`);
      }
    }
  }
  let html = '';
  try { html = read(root, 'demo/index.html'); }
  catch (_) { html = ''; }
  const heads = html.match(/<h1[\s>]/g) || [];
  if (heads.length !== 1) fail(`demo has ${heads.length} h1 elements`);
  for (const piece of [
    'lang="en"',
    'name="viewport"',
    'class="jk-wordmark">AGIW<',
    'class="jk-skip"',
    'aria-label="Sections"',
    'data-jk-pipeline',
    'id="states"',
    'id="motion"',
    'role="alert"',
    'aria-invalid="true"',
    'aria-describedby="jk-lane-error"',
    'role="tablist"',
    'aria-busy="true"',
    'disabled>Disabled',
    '<details class="jk-disclosure">',
    'boot.js',
    'behavior.mjs',
  ]) {
    if (!html.includes(piece)) fail(`demo page missing ${piece}`);
  }
  if (/<img\b|<svg\b/i.test(html)) fail('demo page uses an image mark');
  if (/#[0-9a-fA-F]{3,8}\b/.test(html)) fail('demo page hard-codes a colour');
  let pipeline;
  try { pipeline = JSON.parse(read(root, 'demo/pipeline.json')); }
  catch (_) { pipeline = null; }
  if (!pipeline || !Array.isArray(pipeline.stations) || pipeline.stations.length < 2) fail('pipeline.json has no stations');
  else {
    pipeline.stations.forEach((station) => {
      if (!/^[a-z0-9-]+$/.test(station.id || '')) fail(`station id ${station.id} is not a slug`);
      for (const key of ['name', 'word', 'detail']) {
        if (typeof station[key] !== 'string' || station[key].trim() === '') fail(`station ${station.id} missing ${key}`);
      }
    });
  }
  const readme = (() => { try { return read(root, 'README.md'); } catch (_) { return ''; } })();
  if (!readme.includes('node jaryo/cli.mjs install --dest')) fail('README lost the install command');
  if (!readme.includes('cli.mjs demo --port 8731')) fail('README lost the demo command');
  return problems;
}
