// The overview map is flat ink in both themes, including pseudos and focused controls.
// The disclosed machine/lane list retains full labels; coverage keeps Plex and normal tracking.
// Run only in hosted Linux CI, alongside overview-ink.mjs.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const fixture = JSON.parse(readFileSync(new URL('../test_support/fleet-state.json', import.meta.url), 'utf8'));

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
    // Use the shared fleet contract, independently of CI's configured endpoints.
    await page.route('**/api/stream', route => route.fulfill({ status: 503, body: '' }));
    await page.route('**/api/snapshot', async route => {
      const response = await route.fetch();
      const snapshot = await response.json();
      await response.dispose();
      await route.fulfill({ json: { ...snapshot, ...fixture.snapshot, sampledAt: Date.now() / 1000, fleetState: fixture.fleetState } });
    });
    await page.goto(`http://127.0.0.1:${port}/?demo=1`, { waitUntil: 'domcontentloaded' });
    const disclosure = page.locator('#fleetMap');
    if (await disclosure.getAttribute('open') !== null) throw new Error('map must start collapsed');
    const summary = page.locator('#fleetMap > summary');
    if ((await summary.textContent()).trim() !== 'Show map') throw new Error('full Show map label changed');
    await summary.click();
    await page.locator('#fleetMapList .fleet-map-node').first().waitFor({ state: 'visible' });
    await page.evaluate(() => document.fonts.ready);
    // Focus still exercises the flat-style rules on the map's actual controls.
    await page.locator('#fleetMapList .fleet-map-lanes .fleet-map-node').first().focus();
    const result = await page.evaluate(expected => {
      const region = document.querySelector('#fleetMap');
      const map = document.querySelector('#fleetMapList');
      const heading = document.querySelector('#headline');
      if (!region || !map || !heading) return { missing: 'map disclosure, machine/lane list or endpoint coverage' };
      const requiredLabels = expected.map(fact => ({ fact,
        element: [...map.querySelectorAll('.fleet-map-node')].find(el => el.dataset.node === fact.id) }));
      const missing = requiredLabels.filter(({fact, element}) => !element || !element.textContent.includes(fact.label)).map(({fact}) => fact.id);
      if (missing.length) return { missing };
      const painted = el => {
        for (let node = el; node instanceof Element; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
        }
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
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
      const texts = [...map.querySelectorAll('.fleet-map-node')];
      const masks = texts.filter(el => {
        const style = getComputedStyle(el);
        return (style.stroke !== 'none' && parseFloat(style.strokeWidth) > 0)
          || parseFloat(style.webkitTextStrokeWidth) > 0;
      }).map(el => ({ text: el.textContent.slice(0, 32), cls: el.getAttribute('class') }));
      // Missing or hidden labels cannot make the stroke check pass vacuously.
      const invisible = requiredLabels.map(({element}) => element).filter(el => {
        const style = getComputedStyle(el);
        return !el.textContent.trim() || !painted(el)
          || style.color === 'transparent' || style.color === 'rgba(0, 0, 0, 0)';
      }).map(el => ({ text: el.textContent.slice(0, 32), cls: el.getAttribute('class') }));
      const style = getComputedStyle(heading);
      const spacing = parseFloat(style.letterSpacing);
      const lanes = expected.filter(fact => fact.scope === 'lane').length;
      const coverage = new RegExp(`^[■?] (?:\\d+ of ${lanes} lanes answering|Answering count unknown; ${lanes} lanes configured)$`);
      const headline = {
        text: heading.textContent,
        family: style.fontFamily,
        letterSpacing: style.letterSpacing,
        crushed: !painted(heading) || !coverage.test(heading.textContent.trim())
          || (Number.isFinite(spacing) && spacing < 0) || !/^"?IBM Plex Sans"?(?:,|$)/.test(style.fontFamily),
      };
      return { hits: hits.slice(0, 20), halos, masks: masks.slice(0, 8), invisible, headline };
    }, fixture.fleetState.facts);
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
console.log('ok light and dark disclosed flat map, full machine/lane labels and Plex coverage tracking');
