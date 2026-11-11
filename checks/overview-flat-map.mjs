// The overview map is flat ink in both themes, including ::before and focused edges.
// Labels retain their fill without a stroke; runtime emphasis keeps Plex and normal tracking.
// Run only in hosted Linux CI, alongside overview-ink.mjs.
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
let browser;
const failures = [];
try {
  let buffer = '';
  const port = await new Promise((resolve, reject) => {
    const fail = error => {
      clearTimeout(timer);
      reject(error);
    };
    const timer = setTimeout(() => fail(new Error('observer did not print a port')), 20000);
    child.once('error', fail);
    child.once('exit', () => fail(new Error('observer exited before printing a port')));
    child.stdout.on('data', chunk => {
      buffer += chunk.toString();
      const line = buffer.split('\n').slice(0, -1).find(item => item.includes('"port"'));
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
  browser = await chromium.launch({ args: ['--use-mock-keychain', '--password-store=basic'] });
  for (const scheme of ['light', 'dark']) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, colorScheme: scheme });
    // Fictional demo data guarantees nodes, labels and active states independently of CI's host.
    await page.goto(`http://127.0.0.1:${port}/?demo=1`, { waitUntil: 'domcontentloaded' });
    await page.locator('#nodes .node.hub').first().waitFor({ state: 'visible' });
    await page.locator('#groups .cluster-label').first().waitFor({ state: 'visible' });
    await page.evaluate(() => document.fonts.ready);
    // Keyboard focus exercises the highlighted-edge drop shadow and reveals the hub's labels.
    await page.locator('#nodes .node.hub').first().focus();
    await page.locator('#edges .edge.highlighted').first().waitFor({ state: 'visible' });
    await page.waitForFunction(() => {
      const sub = document.querySelector('#nodes .node.spotlight .sub');
      return sub && parseFloat(getComputedStyle(sub).opacity) >= 0.99;
    });
    const result = await page.evaluate(() => {
      const region = document.querySelector('.graph-region');
      const map = document.querySelector('#map');
      const em = document.querySelector('#headline em');
      if (!region || !map || !em) return { missing: 'graph region, map or runtime emphasis' };
      for (const selector of ['.cluster-label', '.node.hub .label', '.node.spotlight .sub']) {
        if (!map.querySelector(selector)) return { missing: selector };
      }
      const painted = el => {
        for (let node = el; node instanceof Element; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
        }
        return true;
      };
      const hits = [];
      for (const el of [region, ...region.querySelectorAll('*')]) {
        if (!painted(el)) continue;
        for (const pseudo of ['', '::before', '::after']) {
          const style = getComputedStyle(el, pseudo || null);
          const effects = {};
          for (const prop of ['filter', 'boxShadow', 'textShadow', 'backdropFilter']) {
            if (style[prop] && style[prop] !== 'none') effects[prop] = style[prop];
          }
          if (/radial-gradient|blur\(/.test(style.backgroundImage)) effects.backgroundImage = style.backgroundImage;
          if (Object.keys(effects).length) hits.push({ tag: el.tagName, cls: el.getAttribute('class'), pseudo, effects });
        }
      }
      const halos = [...map.querySelectorAll('.node .halo, #glows, .glow')]
        .filter(painted).map(el => el.getAttribute('class') || el.id);
      const texts = [...map.querySelectorAll('.cluster-label, .label, .sub, .capability-line')];
      const masks = texts.filter(el => {
        const style = getComputedStyle(el);
        return style.stroke !== 'none' && parseFloat(style.strokeWidth) > 0;
      }).map(el => ({ text: el.textContent.slice(0, 32), cls: el.getAttribute('class') }));
      // Missing or hidden labels cannot make the stroke check pass vacuously.
      const requiredLabels = [...map.querySelectorAll('.cluster-label, .node.hub .label, .node.spotlight .sub')];
      const invisible = requiredLabels.filter(el => {
        const style = getComputedStyle(el);
        return !el.textContent.trim() || !painted(el) || style.fill === 'none'
          || style.fill === 'transparent' || style.fill === 'rgba(0, 0, 0, 0)' || Number(style.fillOpacity) === 0;
      }).map(el => ({ text: el.textContent.slice(0, 32), cls: el.getAttribute('class') }));
      const style = getComputedStyle(em);
      const spacing = parseFloat(style.letterSpacing);
      const headline = {
        text: em.textContent,
        family: style.fontFamily,
        letterSpacing: style.letterSpacing,
        crushed: (Number.isFinite(spacing) && spacing < 0) || !/^"?IBM Plex Sans"?(?:,|$)/.test(style.fontFamily),
      };
      return { hits: hits.slice(0, 20), halos, masks: masks.slice(0, 8), invisible, headline };
    });
    if (result.missing || result.hits.length || result.halos.length || result.masks.length
        || result.invisible.length || result.headline.crushed) failures.push({ scheme, ...result });
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
console.log('ok light and dark flat map, unstroked labels and Plex runtime tracking');
