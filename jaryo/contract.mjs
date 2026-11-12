/** File-level contract for the kit. Returns human-readable problems. */
import { readFileSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { KIT_FILES } from './install.mjs';
import { isDeepStrictEqual } from 'node:util';
import { CHOICE_FILE, CHOSEN, OVERRIDES, IMPLEMENTATION } from './decision.mjs';
import { primitives as renderPrimitives, outputs, TOKEN_START, TOKEN_END } from './generate.mjs';
import { contrastRatio, checkContrast } from './contrast.mjs';
export { contrastRatio } from './contrast.mjs';

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
  '--j-font-display': 'var(--j-font)',
};

function read(root, rel) {
  return readFileSync(join(root, rel), 'utf8');
}

function sha256(root, rel) {
  return createHash('sha256').update(readFileSync(join(root, rel))).digest('hex');
}

export function checkPlatformVariants(tokens) {
  const problems = [];
  if (tokens?.provisionalTypeface !== 'Inter' || tokens?.surface !== 'black-white') {
    problems.push('foundation must remain Inter and black-white');
  }
  if (!isDeepStrictEqual(tokens?.appliedChoiceFile, CHOICE_FILE)) problems.push('choice file name or SHA-256 drifted');
  if (!isDeepStrictEqual(tokens?.chosenFamilyIds, CHOSEN)) problems.push('chosen family IDs must equal all 17 owner choices');
  if (!isDeepStrictEqual(tokens?.ownerOverrides, OVERRIDES)) problems.push('F17 owner override must record booklet red and 2026-11-11');
  for (const [key, expected] of Object.entries(IMPLEMENTATION)) {
    if (!isDeepStrictEqual(tokens?.[key], expected)) problems.push(`owner choice implementation drifted: ${key}`);
  }
  const web = tokens?.variants?.web;
  if (web?.scope !== 'full-observer-web' || web?.fonts?.text !== 'IBM Plex Sans'
      || web?.fonts?.figures !== 'IBM Plex Mono' || web?.fontLicense !== 'SIL Open Font License 1.1'
      || web?.accent !== '#C21A27' || JSON.stringify(web?.typeScalePx) !== '[13,21,34,55,89,144]') {
    problems.push('web variant must record implemented Plex typography, booklet accent and six-step scale');
  }
  if (JSON.stringify(tokens?.variants?.android) !== '{"inherits":"foundation"}') {
    problems.push('Android must inherit the foundation without web overrides');
  }
  return problems;
}

export function checkChoiceCss(css) {
  const start = css.indexOf(TOKEN_START), end = css.indexOf(TOKEN_END);
  const other = start >= 0 && end > start ? css.slice(0, start) + css.slice(end + TOKEN_END.length) : css;
  const problems = [];
  const names = '(?:bg|surface|text|muted|border|focus|invert(?:-ink)?|accent(?:-ink)?|status-(?:ok|run|blocked|unknown)|blocked-bg|sel-(?:sub|blocked|run))';
  if (new RegExp(`--j-${names}\\s*:`).test(other)) problems.push('palette token overridden outside the generated choice block');
  return problems;
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
  try {
    if (sha256(root, 'choices.json') !== CHOICE_FILE.sha256) fail('choices.json does not match the recorded SHA-256');
    if (!isDeepStrictEqual(JSON.parse(read(root, 'choices.json')).choices, CHOSEN)) fail('choice provenance differs from chosen family IDs');
  } catch (_) { fail('choice provenance is missing or invalid'); }
  let scale;
  try {
    const parsed = JSON.parse(tokens); scale = parsed.scale;
    problems.push(...checkPlatformVariants(parsed), ...checkContrast(parsed));
    if (read(root, 'primitives.css') !== renderPrimitives(parsed)) fail('primitives.css differs from generated token values');
    for (const [file, expected] of outputs(root)) {
      if (read(root, file) !== expected) fail(`${file} differs from the generated choices`);
      if (file === '../web/jaryo.css') problems.push(...checkChoiceCss(read(root, file)));
    }
  }
  catch (error) { fail(`token/export validation failed: ${error.message}`); scale = null; }
  if (!scale) fail('tokens.json has no scale');
  const primitives = (() => { try { return read(root, 'primitives.css'); } catch (_) { return ''; } })();
  if (!/@media \(prefers-color-scheme: light\)\s*\{\s*html:not\(\[data-theme="dark"\]\)/.test(primitives)) {
    fail('primitives.css lost the system light rule');
  }
  for (const token of ['--j-text', '--j-bg', '--j-border', '--j-invert', '--j-invert-ink', '--j-focus', '--j-motion']) {
    if (!primitives.includes(`${token}:`)) fail(`primitives.css lost ${token}`);
  }
  if (!primitives.includes('#000000') || !primitives.includes('#FFFFFF')) fail('primitives.css lost black or white');
  for (const [name, value] of Object.entries(SCALE)) {
    if (!primitives.includes(`${name}: ${value};`)) fail(`primitives.css missing ${name}: ${value}`);
  }
  if (scale && scale.motion !== '240ms') fail('scale motion is not 240ms');
  if (!primitives.includes('--j-motion: 0ms')) fail('primitives.css motion duration drifted');
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
  const patterns = (() => { try { return read(root, 'patterns.css'); } catch (_) { return ''; } })();
  if (!components.includes('@import url("./patterns.css")')) fail('components.css does not load the chosen patterns');
  if (/#[0-9a-fA-F]{3,8}\b|opacity\s*:/.test(patterns)) fail('patterns.css bypasses the palette or reduces contrast with opacity');
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
