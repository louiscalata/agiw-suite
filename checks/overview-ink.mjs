// Proves the overview paints ink. Chromium getComputedStyle, not a stylesheet guess.
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

const child = spawn('python3', ['-B', 'server.py', '--port', '0'], {
  cwd: root,
  stdio: ['ignore', 'pipe', 'pipe'],
});
let buffer = '';
const port = await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('observer did not print a port')), 20000);
  child.stdout.on('data', (chunk) => {
    buffer += chunk.toString();
    const line = buffer.split('\n').find((item) => item.includes('"port"'));
    if (!line) return;
    clearTimeout(timer);
    resolve(JSON.parse(line).port);
  });
});

const browser = await chromium.launch();
const failures = [];
try {
  for (const scheme of ['light', 'dark']) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, colorScheme: scheme });
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(800);
    const colored = await page.evaluate(() => {
      const chromaOf = (color) => {
        const match = String(color).match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
        if (!match) return 0;
        const [r, g, b] = [match[1], match[2], match[3]].map(Number);
        return Math.max(r, g, b) - Math.min(r, g, b);
      };
      const hits = [];
      for (const el of document.querySelectorAll('body *')) {
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
        for (const prop of ['color', 'backgroundColor', 'borderTopColor', 'fill', 'stroke', 'outlineColor']) {
          const value = style[prop];
          if (!value || value === 'none') continue;
          if (chromaOf(value) > 18) hits.push({ prop, value, tag: el.tagName, cls: String(el.className).slice(0, 80) });
        }
      }
      return hits.slice(0, 12);
    });
    if (colored.length) failures.push({ scheme, colored });
    await page.close();
  }
} finally {
  await browser.close();
  child.kill();
}

if (failures.length) {
  console.error(JSON.stringify(failures, null, 2));
  process.exit(1);
}
console.log('ok light and dark overview ink');
