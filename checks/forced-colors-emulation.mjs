// Chromium emulation of forced-colors: active. This is not a Windows High Contrast
// session and it does not claim a hardware pass.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

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

function mark(style) {
  const widths = ['borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth']
    .map((name) => parseFloat(style[name]) || 0);
  return {
    outline: style.outlineStyle !== 'none',
    border: widths.some((width) => width > 0),
    outlineStyle: style.outlineStyle,
    borderWidths: widths,
  };
}

const readStyle = (element) => {
  const style = getComputedStyle(element);
  return {
    outlineStyle: style.outlineStyle,
    borderTopWidth: style.borderTopWidth,
    borderRightWidth: style.borderRightWidth,
    borderBottomWidth: style.borderBottomWidth,
    borderLeftWidth: style.borderLeftWidth,
  };
};

async function visibleMark(locator, label) {
  const count = await locator.count();
  if (count < 1) throw new Error(`${label}: no element`);
  const style = await locator.first().evaluate(readStyle);
  const painted = mark(style);
  if (!painted.outline && !painted.border) {
    throw new Error(`${label}: no outline or border (${JSON.stringify(style)})`);
  }
  return painted;
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
  browser = await chromium.launch();
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
        const selected = page.locator('nav a[aria-current="page"]');
        await selected.waitFor({ state: 'visible' });
        await visibleMark(selected, `${label} selected option`);
        const theme = page.locator('#jaryoTheme');
        await visibleMark(theme, `${label} selected theme control`);
        await page.keyboard.press('Tab');
        const focused = page.locator(':focus');
        await visibleMark(focused, `${label} focused control`);
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
