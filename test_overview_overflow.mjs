// Node-only data and CSS contracts. These do not claim browser layout coverage.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { deriveMonitors, demoSnapshot } from './web/monitor-tabs.mjs';

const css = readFileSync(new URL('./web/jaryo.css', import.meta.url), 'utf8');
const html = readFileSync(new URL('./web/index.html', import.meta.url), 'utf8');
const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');

// Read declarations from an exact selector, including its narrow-screen rule.
// No fabricated bounding boxes or pretend CSS layout engine.
function declarations(selector) {
  const rules = css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g);
  const result = {};
  for (const [, head, body] of rules) {
    if (!head.trim().split(/\s*,\s*/).includes(selector)) continue;
    for (const declaration of body.split(';')) {
      const colon = declaration.indexOf(':');
      if (colon >= 0) result[declaration.slice(0, colon).trim()] = declaration.slice(colon + 1).trim();
    }
  }
  return result;
}

test('the live zoom toolbar has a right bound and wraps instead of growing the document', () => {
  // Geometry uses demo tabs, but the map and zoom percentage still use live data.
  assert.match(html, /id="zoomLevel">100%<\/span>/);
  assert.match(app, /\$\('zoomLevel'\)\.textContent=Math\.round\(camera\.z\*100\)\+'%'/);
  assert.match(app, /function fitMap\(\)[^\n]*fitGraph\([^\n]*applyCamera\(\)/);
  const toolbar = declarations('body.jaryo-app .map-controls');
  assert.equal(toolbar.right, '12px', 'bound the row in addition to its inherited left offset');
  assert.equal(toolbar['min-width'], '0');
  assert.equal(toolbar['flex-wrap'], 'wrap', 'font swaps and wider percentages may add a row');
  assert.ok(!toolbar.overflow, 'do not hide or scroll away controls to pass geometry');
});

test('legitimate unbroken client and model names stay intact in wrappable status labels', () => {
  const sample = demoSnapshot();
  const model = 'M'.repeat(200); // modelPair accepts 200 characters, without requiring separators.
  const client = 'C'.repeat(40); // clientMonitor accepts 40 characters.
  sample.clients[1].label = client;
  sample.pipeline.pipelines[1].requestedModel = model;
  const monitors = deriveMonitors(sample, { fictional: true, feedFresh: true });
  assert.ok(monitors.some(tab => tab.title === client));
  const session = monitors.find(tab => tab.workload?.runId === 'demo-claude');
  assert.ok(session.title.includes(model));

  // Execute the production tab painter with a small text-only DOM; verify the
  // long fixture reaches the same class whose shrink/wrap contract we check.
  const source = readFileSync(new URL('./web/monitor-board.mjs', import.meta.url), 'utf8');
  const element = (tag, className, text = '') => ({
    tag, className, textContent: text, dataset: {}, children: [],
    setAttribute() {}, append(...children) { this.children.push(...children); },
  });
  const host = { children: [], contains: () => false,
    replaceChildren() { this.children = []; },
    append(node) { this.children.push(node); }, querySelectorAll: () => [] };
  const paint = source.slice(source.indexOf('function paintTabs('), source.indexOf('\nexport function paintMonitorChrome'));
  runInNewContext(`${paint}\npaintTabs(host, monitors, 'all');`, {
    host, monitors, el: element, document: { activeElement: null }, nextTabFocus: () => null,
  });
  const labels = host.children.flatMap(tab => tab.children).filter(node => node.className === 'j-tab-label');
  assert.ok(labels.some(label => label.textContent.includes(model)));
  assert.ok(labels.some(label => label.textContent === client));

  const label = declarations('body.jaryo-app .j-tab-label');
  assert.equal(label['min-width'], '0', 'flex labels must shrink below an unbroken token');
  assert.equal(label['overflow-wrap'], 'anywhere');
  assert.equal(label['white-space'], 'normal');
  assert.equal(label.overflow, 'visible', 'keep all of the model name readable');
});

test('initial machine names and later activity ages can wrap inside narrow status chips', () => {
  assert.match(html, /<strong>Windows PC<\/strong>/, 'the initial label precedes the first live sample');
  assert.match(app, /strong\.replaceChildren\([^\n]*vital-age/, 'the live label can also grow with an age');
  assert.equal(declarations('body.jaryo-app .vital > span')['flex-wrap'], 'wrap');
  const label = declarations('body.jaryo-app .vital strong');
  assert.equal(label['max-width'], '100%');
  assert.equal(label['min-width'], '0');
  assert.equal(label['white-space'], 'normal');
  assert.equal(label['overflow-wrap'], 'anywhere');
  assert.ok(!label.overflow, 'wrap the label instead of clipping it');
});
