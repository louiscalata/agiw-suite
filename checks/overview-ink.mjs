// Proves the overview paints ink. Chromium getComputedStyle, not a stylesheet guess.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromaOf } from './ink-color.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  console.error('playwright is not installed. The Ubuntu CI job installs it before this check.');
  process.exit(1);
}

const child = spawn('python3', ['-B', 'server.py', '--port', '0'], {
  cwd: root,
  stdio: ['ignore', 'pipe', 'pipe'],
});
let browser;
const failures = [];
try {
  let buffer = '';
  const port = await new Promise((resolve, reject) => {
    const fail = (error) => {
      clearTimeout(timer);
      reject(error);
    };
    const timer = setTimeout(() => fail(new Error('observer did not print a port')), 20000);
    child.once('error', fail);
    child.once('exit', () => fail(new Error('observer exited before printing a port')));
    child.stdout.on('data', (chunk) => {
      buffer += chunk.toString();
      const line = buffer.split('\n').slice(0, -1).find((item) => item.includes('"port"'));
      if (!line) return;
      try {
        const port = JSON.parse(line).port;
        clearTimeout(timer);
        resolve(port);
      } catch (error) {
        fail(error);
      }
    });
  });
  browser = await chromium.launch();
  for (const scheme of ['light', 'dark']) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, colorScheme: scheme });
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(800);
    const paints = await page.evaluate(() => {
      const hits = [];
      for (const el of document.querySelectorAll('body *')) {
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
        for (const prop of ['color', 'backgroundColor', 'borderTopColor', 'fill', 'stroke', 'outlineColor']) {
          const value = style[prop];
          if (!value || value === 'none') continue;
          hits.push({ prop, value, tag: el.tagName, cls: String(el.className).slice(0, 80) });
        }
      }
      return hits;
    });
    const colored = paints.filter(({ value }) => chromaOf(value) > 18).slice(0, 12);
    if (colored.length) failures.push({ scheme, colored });
    await page.close();
  }
} finally {
  try {
    await browser?.close();
  } finally {
    child.kill();
  }
}

if (failures.length) {
  console.error(JSON.stringify(failures, null, 2));
  process.exit(1);
}
console.log('ok light and dark overview ink');
