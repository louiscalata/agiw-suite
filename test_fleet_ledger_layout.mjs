// Source/DOM layout checks only. These compute CSS track intervals without a
// browser; font rasterization, zoom and native assistive technology remain unrun.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {harness} from './test_support/monitor-harness.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const css = read('./web/jaryo.css');
const legacyCss = read('./web/style.css');
const html = read('./web/index.html');
const fixture = JSON.parse(read('./test_support/fleet-state.json'));

function selectorsOf(head) {
  const selectors = [];
  let depth = 0, start = 0;
  for (let index = 0; index < head.length; index++) {
    if (head[index] === '(') depth++;
    else if (head[index] === ')') depth--;
    else if (head[index] === ',' && depth === 0) {selectors.push(head.slice(start, index).trim()); start = index + 1;}
  }
  selectors.push(head.slice(start).trim());
  return selectors;
}
function declarations(suffix, {enlarged = false} = {}) {
  const result = {};
  for (const [, head, body] of css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = selectorsOf(head);
    if (!selectors.some(selector => (enlarged
      ? selector.startsWith('html:is([data-text-size=') && selector.includes('body.jaryo-app.fleet-ledger')
      : selector.startsWith('body.jaryo-app.fleet-ledger')) && selector.endsWith(suffix))) continue;
    for (const declaration of body.split(';')) {
      const colon = declaration.indexOf(':');
      if (colon >= 0) result[declaration.slice(0, colon).trim()] = declaration.slice(colon + 1).replace(/\s*!important\s*$/, '').trim();
    }
  }
  return result;
}
function pixels(value, label) {
  assert.match(value || '', /^\d+(?:\.\d+)?px$/, `${label} has an explicit pixel bound`);
  return Number.parseFloat(value);
}
function intervals(widths, gap = 0) {
  let start = 0;
  return widths.map(width => {const interval = {start, end: start + width, width}; start = interval.end + gap; return interval;});
}
function disjoint(boxes) {
  for (let index = 1; index < boxes.length; index++) assert.ok(boxes[index - 1].end <= boxes[index].start);
}
function sample() {
  const contract = structuredClone(fixture.fleetState);
  const machines = contract.facts.filter(fact => fact.scope === 'machine').slice(0, 3);
  const base = contract.facts.find(fact => fact.scope === 'lane');
  const facts = [...machines];
  for (const [index, count] of [2, 3, 1].entries()) {
    for (let lane = 0; lane < count; lane++) {
      const answering = index > 0;
      facts.push({...structuredClone(base), id: `${machines[index].id}:layout-${lane}`, parentId: machines[index].id,
        label: `Lane ${lane + 1}`, state: answering ? 'endpoint answering' : 'endpoint unavailable',
        reason: answering ? null : 'timeout', evidence: {...base.evidence, modelId: answering ? `model-${index}-${lane}` : null}});
    }
  }
  return {...structuredClone(fixture.snapshot), host: 'mac', sources: [], models: [], fleetState: {...contract, facts}};
}

test('the Fleet ledger precedes attention, controls and the initially collapsed map in DOM focus order', () => {
  assert.match(html, /<body[^>]*class="[^"]*fleet-ledger/);
  const positions = ['fleetOverview', 'machineLanes', 'needsAttention', 'runtimeSidebar', 'fleetMap']
    .map(id => html.indexOf(`id="${id}"`));
  assert.ok(positions.every(position => position >= 0));
  assert.deepEqual([...positions].sort((a, b) => a - b), positions);
  assert.match(html, /<details\b[^>]*id="fleetMap"[^>]*>\s*<summary\b[^>]*>Show map<\/summary>/);
  const mapTag = /<details\b[^>]*id="fleetMap"[^>]*>/.exec(html)[0];
  assert.doesNotMatch(mapTag, /\bopen(?:\s|=|>)/);
  assert.doesNotMatch(html, /LOCAL RUNTIME|id="headline"[^>]*>Fleet/);
});

test('every configured lane has its own semantic row, grouped under a machine with endpoint age and reasons', () => {
  class Clock extends Date {static now() {return fixture.now * 1000;}}
  const h = harness({extra: {Date: Clock}});
  const input = sample();
  h.api.setFeed(input);
  h.api.renderRegistryTable();
  const root = h.$('machineLanes');
  const table = root.querySelector('table');
  assert.ok(table, 'the landing ledger is a native table');
  assert.equal(table.querySelectorAll('col').length, 5);
  const headings = table.querySelector('thead').querySelectorAll('th');
  assert.equal(headings.length, 5);
  assert.deepEqual(headings.map(cell => cell.getAttribute('scope')), Array(5).fill('col'));
  const groups = table.querySelectorAll('tbody');
  assert.equal(groups.length, 3);
  assert.deepEqual(groups.map(group => group.querySelectorAll('tr').length), [2, 3, 1]);
  for (const group of groups) {
    const name = group.querySelector('th');
    assert.equal(name.getAttribute('scope'), 'rowgroup');
    assert.equal(name.getAttribute('rowspan'), String(group.querySelectorAll('tr').length));
  }
  for (const row of groups.flatMap(group => group.querySelectorAll('tr'))) {
    const endpoint = row.querySelectorAll('td')[2];
    assert.match(endpoint.querySelector('span').textContent, /[■?◐!×]\s*(?:endpoint answering|endpoint unavailable)/);
    assert.match(endpoint.querySelector('time').textContent, /\d+ s ago/);
    assert.ok(endpoint.querySelector('time').getAttribute('datetime'));
  }
  assert.equal(groups[0].querySelectorAll('tr').filter(row => /timeout/.test(row.textContent)).length, 2);
  assert.ok(groups.slice(1).every(group => !/timeout/.test(group.textContent)));
  const scroller = root.querySelector('.fleet-table-scroll');
  assert.equal(scroller.getAttribute('role'), 'region');
  assert.equal(scroller.getAttribute('tabindex'), '0');
  assert.ok(scroller.getAttribute('aria-label'));
});

test('table columns and counter tracks cannot overlap from 900 through 1600 pixels', () => {
  const table = declarations('.fleet-table');
  assert.equal(table['table-layout'], 'fixed');
  assert.equal(table.width, '100%');
  // A legacy nth-child TH width would outrank the colgroup and reintroduce the
  // tiny machine headers in the owner's screenshot despite correct new widths.
  assert.doesNotMatch(legacyCss, /#machineLanes[^{}]*\bth[^{}]*\{[^{}]*\bwidth\s*:/);
  const columns = ['machine', 'lane', 'model', 'endpoint', 'reason'].map(name => {
    const explicit = declarations(`.fleet-table col.${name}`);
    return pixels(explicit.width, `${name} column`);
  });
  const minimum = pixels(table['min-width'], 'table minimum');
  assert.ok(minimum >= columns.reduce((sum, width) => sum + width, 0));
  // 14px Plex headers have deliberately generous bounds; the table must retain
  // these minima even when its named horizontal scroller is narrower.
  for (const [index, minimumHeader] of [100, 90, 130, 170, 130].entries()) assert.ok(columns[index] >= minimumHeader);
  assert.equal(declarations('.fleet-table-scroll')['overflow-x'], 'auto');
  assert.equal(declarations('#machineLanes').position, 'static');
  assert.equal(declarations('#machineLanes')['max-height'], 'none');
  const counters = declarations('.fleet-counters');
  assert.equal(counters.display, 'grid');
  assert.match(counters['grid-template-columns'], /^repeat\(3,\s*minmax\(0,\s*1fr\)\)$/);
  const gap = pixels(counters.gap, 'counter gap');
  const child = declarations('.fleet-counters > div');
  assert.equal(child['min-width'], '0');
  const labels = declarations('.fleet-counters span');
  assert.equal(labels['overflow-wrap'], 'anywhere');
  assert.equal(labels['white-space'], 'normal');
  const root = declarations('body.jaryo-app.fleet-ledger');
  assert.equal(root['grid-template-columns'], 'var(--fleet-nav-width) minmax(0,1fr)');
  const nav = pixels(root['--fleet-nav-width'], 'desktop navigation width');
  const padding = pixels(declarations('#content').padding, 'main padding');
  for (let viewport = 900; viewport <= 1600; viewport++) {
    const available = viewport - nav - 2 * padding;
    const actualWidth = Math.max(minimum, available);
    const extra = (actualWidth - columns.reduce((sum, width) => sum + width, 0)) / columns.length;
    const columnBoxes = intervals(columns.map(width => width + extra));
    disjoint(columnBoxes);
    assert.ok(Math.abs(columnBoxes.at(-1).end - actualWidth) < 1e-8);
    assert.ok(columnBoxes.every((box, index) => box.width >= columns[index]));
    const counterBoxes = intervals(Array(3).fill((available - 2 * gap) / 3), gap);
    disjoint(counterBoxes);
    assert.ok(counterBoxes.every(box => box.width > 0));
    assert.ok(Math.abs(counterBoxes.at(-1).end - available) < 1e-8);
  }
});

test('Fleet excludes viewport reservations and the root document can scroll to every lane', () => {
  const rootRules = [...css.matchAll(/html:has\(body\.fleet-ledger\)\s*\{([^{}]*)\}/g)].map(match => match[1]).join('\n');
  assert.match(rootRules, /height:\s*auto/);
  assert.match(rootRules, /overflow-y:\s*auto/);
  assert.match(css, /body\.jaryo-app:not\(\.fleet-ledger\):not\(:has\(\.j-page\)\):not\(:has\(\.j-map\)\)\s*\{\s*grid-template-rows:\s*auto auto minmax\(0, 1fr\) auto/);
  const body = declarations('body.jaryo-app.fleet-ledger');
  assert.equal(body.height, 'auto');
  assert.equal(body.overflow, 'visible');
  assert.equal(body['grid-template-rows'], 'auto auto auto');
  const main = declarations('#content');
  assert.equal(main.display, 'flex');
  assert.equal(main['flex-direction'], 'column');
  assert.equal(main.height, 'auto');
  assert.equal(main.overflow, 'visible');
  const controls = declarations('.sidebar');
  assert.equal(controls.position, 'static');
  assert.equal(controls.height, 'auto');
  assert.equal(controls['max-height'], 'none');
  assert.equal(controls.visibility, 'visible');
});

test('150% and 200% text reflow Fleet into one column while retaining readable evidence scrollers', () => {
  assert.match(css, /html:is\(\[data-text-size="150"\],\[data-text-size="200"\]\) body\.jaryo-app\.fleet-ledger\s*\{/);
  const body = declarations('body.jaryo-app.fleet-ledger', {enlarged: true});
  assert.equal(body['grid-template-columns'], 'minmax(0,1fr)');
  assert.equal(body['grid-template-rows'], 'auto auto auto auto');
  const nav = declarations('> .j-nav', {enlarged: true});
  assert.equal(nav['grid-column'], '1');
  assert.equal(nav['grid-row'], '2');
  assert.equal(nav['flex-direction'], 'row');
  assert.equal(nav['flex-wrap'], 'wrap');
  const main = declarations('#content', {enlarged: true});
  assert.equal(main['grid-column'], '1');
  assert.equal(main['grid-row'], '3');
  const footer = declarations('> footer', {enlarged: true});
  assert.equal(footer['grid-column'], '1');
  assert.equal(footer['grid-row'], '4');
  assert.equal(declarations('.fleet-table-scroll')['overflow-x'], 'auto');
  assert.equal(declarations('#fleetMapList')['overflow-x'], 'auto');
  assert.equal(declarations('.fleet-map-node')['white-space'], 'nowrap');
});

test('the optional HTML map uses a nested list whose long labels scroll instead of colliding', () => {
  assert.match(html, /id="fleetMapList"/);
  const lane = declarations('.fleet-map-node');
  assert.equal(lane['white-space'], 'nowrap');
  const nested = declarations('.fleet-map-lanes');
  const margin = nested.margin.split(/\s+/);
  const padding = nested.padding.split(/\s+/);
  assert.ok(pixels(margin.at(-1), 'lane indentation') > 0);
  assert.ok(pixels(padding.at(-1), 'lane indentation padding') > 0);
  const map = declarations('#fleetMapList');
  assert.equal(map['overflow-x'], 'auto');
  class Clock extends Date {static now() {return fixture.now * 1000;}}
  const h = harness({extra: {Date: Clock}});
  h.api.setFeed(sample());
  h.api.renderRegistryTable();
  const root = h.$('fleetMapList');
  const machines = root.querySelector('.fleet-map-machines');
  assert.equal(machines.tagName, 'UL');
  assert.equal(machines.children.length, 3);
  assert.deepEqual(machines.children.map(item => item.querySelector('.fleet-map-lanes').children.length), [2, 3, 1]);
  for (const item of machines.children) {
    assert.equal(item.tagName, 'LI');
    const lanes = item.querySelector('.fleet-map-lanes');
    assert.equal(lanes.tagName, 'UL');
    assert.ok(lanes.children.every(lane => lane.tagName === 'LI' && lane.children.length === 1 && lane.children[0].tagName === 'BUTTON'));
  }
  const focused = root.querySelectorAll('button').find(button => button.dataset.node.endsWith(':layout-1'));
  focused.focus();
  root.scrollLeft = 82;
  h.api.renderRegistryTable();
  assert.equal(h.doc.activeElement.dataset.node, focused.dataset.node, 'a refresh restores focus to the same lane');
  assert.equal(root.scrollLeft, 82, 'a refresh preserves the horizontal reading position');
});

test('losing the fleet contract clears retained map labels and reasons instead of implying no issues', () => {
  class Clock extends Date {static now() {return fixture.now * 1000;}}
  const h = harness({extra: {Date: Clock}});
  const input = sample();
  h.api.setFeed(input);
  h.api.renderRegistryTable();
  assert.equal(h.$('fleetMapList').querySelectorAll('button').length, 9);
  assert.match(h.$('attentionReasons').textContent, /timeout/);
  delete input.fleetState;
  h.api.setFeed(input);
  h.api.renderRegistryTable();
  assert.equal(h.$('machineLanes').hidden, true);
  assert.equal(h.$('fleetMapList').querySelectorAll('button').length, 0);
  assert.doesNotMatch(h.$('attentionReasons').textContent, /timeout|No lane issues/);
  assert.match(h.$('attentionReasons').textContent, /evidence unavailable/i);
});
