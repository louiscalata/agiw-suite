// The overview map is flat ink. A glow, halo blur, drop shadow, or radial wash fails this check.
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
    await page.waitForTimeout(900);
    const hits = await page.evaluate(() => {
      const region = document.querySelector('.graph-region');
      const map = document.querySelector('#map');
      const roots = [region, map].filter(Boolean);
      const nodes = [];
      for (const rootNode of roots) nodes.push(rootNode, ...rootNode.querySelectorAll('*'));
      const seen = new Set();
      const out = [];
      const consider = (el, pseudo) => {
        const style = pseudo ? getComputedStyle(el, pseudo) : getComputedStyle(el);
        const record = {
          filter: style.filter,
          boxShadow: style.boxShadow,
          textShadow: style.textShadow,
          backdropFilter: style.backdropFilter,
          backgroundImage: style.backgroundImage,
        };
        const reasons = [];
        if (record.filter && record.filter !== 'none') reasons.push('filter');
        if (record.boxShadow && record.boxShadow !== 'none') reasons.push('box-shadow');
        if (record.textShadow && record.textShadow !== 'none') reasons.push('text-shadow');
        if (record.backdropFilter && record.backdropFilter !== 'none') reasons.push('blur');
        if (/radial-gradient|blur\(/.test(record.backgroundImage || '')) reasons.push('radial-gradient');
        if (!reasons.length) return;
        const key = `${el.tagName}|${String(el.className).slice(0, 40)}|${pseudo || ''}|${reasons.join(',')}`;
        if (seen.has(key)) return;
        seen.add(key);
        out.push({
          tag: el.tagName,
          cls: String(el.className).slice(0, 60),
          pseudo: pseudo || '',
          reasons,
          filter: record.filter,
          boxShadow: record.boxShadow,
          textShadow: record.textShadow,
          backdropFilter: record.backdropFilter,
          backgroundImage: record.backgroundImage.slice(0, 160),
        });
      };
      for (const el of nodes) {
        consider(el);
        consider(el, '::before');
        consider(el, '::after');
      }
      return out.slice(0, 20);
    });
    if (hits.length) failures.push({ scheme, hits });
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
console.log('ok flat map');
