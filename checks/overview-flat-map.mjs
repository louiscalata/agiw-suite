// The overview map is flat ink. A glow, halo blur, drop shadow, or radial wash fails this check.
// The runtime line under the headline must be upright Jost with normal tracking.
// Visible map labels must not keep a stroke, which paints an empty rectangle on the grid.
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
    const headline = await page.evaluate(() => {
      const em = document.querySelector('#headline em');
      if (!em) return { missing: true };
      const style = getComputedStyle(em);
      const spacing = parseFloat(style.letterSpacing);
      return {
        text: em.textContent,
        family: style.fontFamily,
        style: style.fontStyle,
        letterSpacing: style.letterSpacing,
        crushed: style.fontStyle !== 'normal' || (Number.isFinite(spacing) && spacing < -0.5) || !/Jost/.test(style.fontFamily),
      };
    });
    const masks = await page.evaluate(() => {
      const texts = [...document.querySelectorAll('#map text')];
      return texts.filter((el) => {
        const style = getComputedStyle(el);
        if (Number(style.opacity) === 0 || style.display === 'none' || style.visibility === 'hidden') return false;
        const width = parseFloat(style.strokeWidth);
        return style.stroke !== 'none' && Number.isFinite(width) && width > 0;
      }).slice(0, 8).map((el) => ({
        text: el.textContent.slice(0, 32),
        cls: el.getAttribute('class'),
        stroke: getComputedStyle(el).stroke,
        strokeWidth: getComputedStyle(el).strokeWidth,
      }));
    });
    if (hits.length || headline.crushed || headline.missing || masks.length) {
      failures.push({ scheme, hits, headline, masks });
    }
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
