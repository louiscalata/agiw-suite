// Chromium emulation of forced-colors: active. This is not a Windows High Contrast
// session and it does not claim a hardware pass.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { focusIsMarked, selectionIsMarked } from './forced-colors-selection.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  console.error('playwright is not installed. The Ubuntu CI job installs it before this check.');
  process.exit(1);
}

function startObserver() {
  const child = spawn('python3', ['-B', 'server.py', '--port', '0'], {
    cwd: root,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let buffer = '';
  const port = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('observer did not print a port')), 20000);
    child.stdout.on('data', (chunk) => {
      buffer += chunk.toString();
      const line = buffer.split('\n').find((item) => item.includes('"port"'));
      if (!line) return;
      clearTimeout(timer);
      resolve(JSON.parse(line).port);
    });
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`observer exited ${code} before printing a port`));
    });
  });
  return { child, port };
}

const grabStyle = (element) => {
  const style = getComputedStyle(element);
  return {
    backgroundColor: style.backgroundColor,
    color: style.color,
    borderTopColor: style.borderTopColor,
    borderRightColor: style.borderRightColor,
    borderBottomColor: style.borderBottomColor,
    borderLeftColor: style.borderLeftColor,
    borderTopWidth: style.borderTopWidth,
    borderRightWidth: style.borderRightWidth,
    borderBottomWidth: style.borderBottomWidth,
    borderLeftWidth: style.borderLeftWidth,
    outlineStyle: style.outlineStyle,
    outlineWidth: style.outlineWidth,
    outlineColor: style.outlineColor,
  };
};

async function readPair(page, selectedSelector, siblingSelector) {
  const selected = page.locator(selectedSelector);
  const sibling = page.locator(siblingSelector);
  await selected.first().waitFor({ state: 'visible' });
  await sibling.first().waitFor({ state: 'visible' });
  return page.evaluate(({ selectedSelector, siblingSelector, grab }) => {
    const probe = document.createElement('div');
    probe.style.cssText = 'forced-color-adjust:none;background:Highlight;color:HighlightText';
    document.body.append(probe);
    const highlight = getComputedStyle(probe).backgroundColor;
    probe.remove();
    const read = new Function(`return (${grab})`)();
    return {
      selected: read(document.querySelector(selectedSelector)),
      sibling: read(document.querySelector(siblingSelector)),
      highlight,
    };
  }, { selectedSelector, siblingSelector, grab: grabStyle.toString() });
}

async function requireMarked(page, selectedSelector, siblingSelector, label) {
  const pair = await readPair(page, selectedSelector, siblingSelector);
  if (!selectionIsMarked(pair.selected, pair.sibling, { highlight: pair.highlight })) {
    throw new Error(`${label}: selected control matches its sibling (${JSON.stringify(pair)})`);
  }
}

async function requireUnmarked(page, selectedSelector, siblingSelector, label) {
  const pair = await readPair(page, selectedSelector, siblingSelector);
  if (selectionIsMarked(pair.selected, pair.sibling, { highlight: pair.highlight })) {
    throw new Error(`${label}: broken fixture was accepted (${JSON.stringify(pair)})`);
  }
}

const brokenFixture = `<!doctype html><title>broken</title>
<style>
  a, button { color: ButtonText; background: transparent; border: 0; border-bottom: 2px solid transparent; }
  button { min-height: 44px; }
</style>
<nav>
  <a href="/" aria-current="page">Overview</a>
  <a href="/monitor">Map</a>
</nav>
<div role="tablist">
  <button class="j-tab" aria-selected="true">One</button>
  <button class="j-tab" aria-selected="false">Two</button>
</div>`;

const markedFixture = `<!doctype html><title>marked</title>
<style>
  a, button { color: ButtonText; background: ButtonFace; border: 0; border-bottom: 2px solid transparent; }
  a[aria-current="page"], button[aria-selected="true"] {
    forced-color-adjust: none;
    background: Highlight;
    color: HighlightText;
  }
  button { min-height: 44px; }
</style>
<nav>
  <a href="/" aria-current="page">Overview</a>
  <a href="/monitor">Map</a>
</nav>
<div role="tablist">
  <button class="j-tab" aria-selected="true">One</button>
  <button class="j-tab" aria-selected="false">Two</button>
</div>`;

async function proveFixtures(page) {
  await page.setContent(brokenFixture);
  await requireUnmarked(page, '.j-tab[aria-selected="true"]', '.j-tab[aria-selected="false"]', 'broken tab');
  await requireUnmarked(page, 'nav a[aria-current="page"]', 'nav a:not([aria-current="page"])', 'broken nav');
  await page.setContent(markedFixture);
  await requireMarked(page, '.j-tab[aria-selected="true"]', '.j-tab[aria-selected="false"]', 'marked tab');
  await requireMarked(page, 'nav a[aria-current="page"]', 'nav a:not([aria-current="page"])', 'marked nav');
  console.log('ok fixture: transparent border rejected, Highlight accepted');
}

async function hiddenText(page) {
  return page.evaluate(() => {
    const problems = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      const text = node.textContent.replace(/\s+/g, ' ').trim();
      const next = walker.nextNode();
      if (text) {
        const el = node.parentElement;
        const skip = !el
          || el.closest('[hidden], [aria-hidden="true"], .visually-hidden, option, title')
          || el.namespaceURI === 'http://www.w3.org/2000/svg';
        if (!skip) {
          const style = getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          const transparent = style.color === 'transparent' || style.color === 'rgba(0, 0, 0, 0)';
          const shown = style.display !== 'none' && Number(style.opacity) !== 0;
          const painted = shown && style.visibility !== 'hidden' && !transparent && rect.width >= 1 && rect.height >= 1;
          if (shown && !painted) problems.push(text.slice(0, 80));
        }
      }
      node = next;
    }
    return problems;
  });
}

const pages = [
  ['/', 'Overview'],
  ['/monitor', 'Map'],
];

const observer = startObserver();
const failures = [];
let browser;
try {
  const port = await observer.port;
  browser = await chromium.launch({
    args: ['--use-mock-keychain', '--password-store=basic'],
  });
  const fixtureContext = await browser.newContext({
    forcedColors: 'active',
    colorScheme: 'light',
    viewport: { width: 1280, height: 800 },
  });
  const fixturePage = await fixtureContext.newPage();
  try {
    await proveFixtures(fixturePage);
  } catch (error) {
    failures.push(error.message);
    console.error(`fail fixture: ${error.message}`);
  }
  await fixtureContext.close();

  for (const scheme of ['light', 'dark']) {
    const context = await browser.newContext({
      forcedColors: 'active',
      colorScheme: scheme,
      viewport: { width: 1280, height: 800 },
    });
    for (const [path, name] of pages) {
      const page = await context.newPage();
      const label = `${scheme} ${name}`;
      try {
        await page.goto(`http://127.0.0.1:${port}${path}`, { waitUntil: 'domcontentloaded' });
        await page.locator('.j-tab[aria-selected="true"]').waitFor({ state: 'visible' });
        await requireMarked(page, '.j-tab[aria-selected="true"]', '.j-tab[aria-selected="false"]', `${label} selected tab`);
        await requireMarked(page, 'nav a[aria-current="page"]', 'nav a:not([aria-current="page"])', `${label} selected nav link`);
        await page.keyboard.press('Tab');
        const focused = await page.locator(':focus').evaluate(grabStyle);
        if (!focusIsMarked(focused)) {
          throw new Error(`${label} focused control has no outline (${JSON.stringify(focused)})`);
        }
        const hidden = await hiddenText(page);
        if (hidden.length) throw new Error(`${label} hidden text: ${hidden.join(' | ')}`);
        console.log(`ok ${label}`);
      } catch (error) {
        failures.push(error.message);
        console.error(`fail ${label}: ${error.message}`);
      }
      await page.close();
    }
    await context.close();
  }
} finally {
  if (browser) await browser.close();
  observer.child.kill('SIGTERM');
}

if (failures.length) {
  console.error(`${failures.length} forced-colors emulation failure(s)`);
  process.exit(1);
}
console.log('forced-colors emulation: light and dark, / and /monitor');
