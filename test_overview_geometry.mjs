import test from 'node:test';
import assert from 'node:assert/strict';
import { runInNewContext } from 'node:vm';
import { overviewGeometry, demoReadinessState } from './checks/overview-geometry.mjs';

const rect = (left, top, right, bottom) => ({ left, top, right, bottom, width: right - left, height: bottom - top });
function fixture(width = 375) {
  const element = (text, box, parent = null) => ({
    textContent: text, box, parentElement: parent,
    style: { display: 'block', visibility: 'visible', opacity: '1', overflowX: 'visible', overflowY: 'visible' },
    getBoundingClientRect() { return this.box; },
  });
  const root = element('', rect(0, 0, width, 812));
  root.clientWidth = root.scrollWidth = width;
  const body = element('', rect(0, 0, width, 1200), root);
  body.scrollWidth = width;
  const heading = element('AGIW Inference Monitor', rect(12, 10, width - 12, 48), body);
  const tuning = element('Model tuning', rect(12, 60, 150, 104), body);
  const tabs = [
    element('All inference', rect(12, 200, width - 12, 244), body),
    element('Recorded client with a longer label', rect(12, 250, width - 12, 330), body),
  ];
  const document = {
    documentElement: root, body,
    querySelector: selector => selector === '.brand-wordmark' ? heading : tuning,
    querySelectorAll: () => tabs,
    createRange: () => ({
      selectNodeContents(node) { this.node = node; },
      getBoundingClientRect() { return this.node.textBox || this.node.box; },
    }),
  };
  return { root, body, heading, tuning, tabs, element,
    run: () => JSON.parse(JSON.stringify(runInNewContext(`(${overviewGeometry.toString()})()`, {
      document, window: { innerHeight: 812 }, getComputedStyle: node => node.style,
    }))),
  };
}

test('wrapped labels and vertically scrollable document content fit at both widths', () => {
  for (const width of [375, 1280]) {
    const f = fixture(width);
    f.tabs[1].box = rect(12, 900, width - 12, 980);
    f.body.style.overflowY = 'auto';
    assert.deepEqual(f.run(), []);
  }
});

test('horizontal document overflow and offscreen headings fail', () => {
  const f = fixture();
  f.root.scrollWidth = 410;
  f.heading.box = rect(-20, 10, 300, 48);
  const failures = f.run();
  assert.ok(failures.some(row => row.target === 'document'));
  assert.ok(failures.some(row => row.target === 'heading' && row.reason === 'outside horizontal viewport'));
});

test('the full tuning label cannot be hidden, clipped or replaced by its short form', () => {
  for (const kind of ['hidden', 'clipped', 'short', 'offscreen']) {
    const f = fixture();
    if (kind === 'hidden') {
      f.tuning.parentElement = f.element('', rect(0, 0, 375, 812), f.body);
      f.tuning.parentElement.style.display = 'none';
    }
    if (kind === 'clipped') {
      f.tuning.style.overflowX = 'hidden';
      f.tuning.textBox = rect(12, 60, 220, 104);
    }
    if (kind === 'short') f.tuning.textContent = 'Tune';
    if (kind === 'offscreen') f.tuning.box = rect(12, -50, 150, -6);
    assert.ok(f.run().some(row => row.target === 'Model tuning'), kind);
  }
});

test('status tabs must populate and their rendered text must not be clipped', () => {
  const empty = fixture();
  empty.tabs.length = 0;
  assert.ok(empty.run().some(row => row.target === 'status tabs'));
  const clipped = fixture();
  const strip = clipped.element('', rect(0, 200, 375, 270), clipped.body);
  strip.style.overflowY = 'hidden';
  clipped.tabs[1].parentElement = strip;
  assert.ok(clipped.run().some(row => row.target === 'status tab 2' && row.reason === 'text clipped on y'));
});

test('a zero-area heading and horizontally overflowing tab text fail', () => {
  const f = fixture();
  f.heading.box = rect(0, 0, 0, 0);
  f.tabs[1].textBox = rect(12, 250, 450, 330);
  const failures = f.run();
  assert.ok(failures.some(row => row.target === 'heading' && row.reason === 'empty or zero area'));
  assert.ok(failures.some(row => row.target === 'status tab 2' && row.reason === 'outside horizontal viewport'));
});

test('a non-scrolling document cannot hide status tabs below its clipped edge', () => {
  const f = fixture();
  f.root.style.overflowY = 'hidden';
  f.tabs[1].box = rect(12, 900, 363, 980);
  assert.ok(f.run().some(row => row.target === 'status tab 2' && row.reason === 'text clipped on y'));
});


test('desktop heading font bounds need clearance inside the clipped viewport', () => {
  const f = fixture(1280);
  f.root.style.overflowY = f.body.style.overflowY = 'hidden';
  f.heading.box = rect(28, 0, 250, 72);
  f.heading.textBox = rect(28, -8, 250, 75);
  const clipped = f.run().filter(row => row.target === 'heading' && row.reason === 'text clipped on y');
  assert.equal(clipped.length, 2, 'both document clipping ancestors catch the text overhang');
  assert.equal(clipped[0].textBounds.top, -8);
  assert.equal(clipped[0].clipBounds.top, 0);
  // Top clearance moves actual text within the viewport without hiding overflow
  // or relaxing the geometry assertion. The UI uses the shared spacing token.
  f.heading.box = rect(28, 12, 250, 84);
  f.heading.textBox = rect(28, 4, 250, 87);
  assert.deepEqual(f.run(), []);
});


test('readiness diagnostics distinguish an absent second label from a hidden one', () => {
  const label = { tagName: 'SPAN', id: '', hidden: false, parentElement: null,
    getBoundingClientRect: () => ({ x: 0, y: 0, width: 0, height: 0 }) };
  let labels = [label];
  const host = { dataset: { fictional: '1' }, querySelectorAll: () => labels };
  const document = { readyState: 'complete', querySelectorAll: () => labels,
    querySelector: selector => selector === '#monitorTabs' ? host : { hidden: false } };
  const run = () => JSON.parse(JSON.stringify(runInNewContext(`(${demoReadinessState.toString()})()`, {
    document, getComputedStyle: () => ({ display: 'none', visibility: 'hidden', opacity: '1', overflowX: 'visible', overflowY: 'visible' }),
  })));
  assert.equal(run().secondExists, false);
  labels = [label, label];
  const hidden = run();
  assert.equal(hidden.secondExists, true);
  assert.equal(hidden.labelCount, 2);
  assert.equal(hidden.ancestors[0].display, 'none');
  assert.equal(hidden.ancestors[0].bounds.width, 0);
  assert.equal(hidden.fictional, '1');
});
