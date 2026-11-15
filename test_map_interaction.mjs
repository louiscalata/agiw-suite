// Map page (/monitor) interaction on a DOM double. No browser, no sockets.
// The double is built from the ids in web/monitor.html, so the scripts are
// checked against the markup that ships. Rendering is not measured here.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as fleet from './web/fleet-state.mjs';
import * as mapTabs from './web/monitor-tabs.mjs';
import * as monitor from './web/monitor.mjs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const html = read('./web/monitor.html');
const monitorCss = read('./web/monitor.css');
const css = `${read('./web/jaryo.css')}\n${monitorCss}`;
const strip = text => text.replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
const shared = JSON.parse(read('./test_support/fleet-state.json'));
const NOW = shared.now * 1000;

const camel = name => name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());

class Element {
  constructor(doc, tag) {
    Object.assign(this, {ownerDocument: doc, tagName: String(tag).toLowerCase(), children: [], parentElement: null,
      attrs: {}, dataset: {}, style: {}, listeners: {}, className: '', hidden: false, text: '', tabIndex: -1, moves: 0});
  }
  get id() { return this.attrs.id || ''; }
  set id(value) { this.attrs.id = String(value); }
  setAttribute(key, value) {
    const text = String(value);
    if (key === 'class') this.className = text;
    else if (key === 'hidden') this.hidden = true;
    else if (key.startsWith('data-')) this.dataset[camel(key)] = text;
    else this.attrs[key] = text;
  }
  getAttribute(key) {
    if (key === 'class') return this.className || null;
    if (key.startsWith('data-')) return this.dataset[camel(key)] ?? null;
    return this.attrs[key] ?? null;
  }
  removeAttribute(key) {
    if (key === 'hidden') this.hidden = false;
    else if (key.startsWith('data-')) delete this.dataset[camel(key)];
    else delete this.attrs[key];
  }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this.text = String(value); }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentElement?.isConnected); }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  // Like a browser, taking a node out of the tree (or moving it) drops focus to the body.
  remove() {
    const doc = this.ownerDocument;
    if (doc.activeElement && doc.activeElement !== doc.body && this.contains(doc.activeElement)) {
      doc.activeElement = doc.body;
      doc.blurs += 1;
    }
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this);
    this.parentElement = null;
  }
  append(...nodes) { for (const node of nodes) { if (node.parentElement === this) this.moves += 1; node.remove(); node.parentElement = this; this.children.push(node); } }
  insertBefore(node, reference) {
    if (node.parentElement === this) this.moves += 1;
    node.remove();
    node.parentElement = this;
    const at = reference ? this.children.indexOf(reference) : -1;
    if (at < 0) this.children.push(node); else this.children.splice(at, 0, node);
    return node;
  }
  replaceChildren(...nodes) { for (const child of [...this.children]) child.remove(); this.text = ''; this.append(...nodes); }
  matches(selector) {
    const tag = selector.match(/^[a-z][\w-]*/i)?.[0];
    if (tag && this.tagName !== tag.toLowerCase()) return false;
    const id = selector.match(/#([\w:-]+)/)?.[1];
    if (id && this.id !== id) return false;
    for (const [, name] of selector.matchAll(/\.([\w-]+)/g)) if (!this.className.split(/\s+/).includes(name)) return false;
    for (const [, key, value] of selector.matchAll(/\[([^=\]]+)(?:="([^"]*)")?\]/g)) {
      const actual = this.getAttribute(key);
      if (value === undefined ? actual === null : actual !== value) return false;
    }
    return true;
  }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  querySelectorAll(selector) {
    const parts = selector.trim().split(/\s+/);
    const walk = (node, out) => { for (const child of node.children) { out.push(child); walk(child, out); } return out; };
    let scope = [this];
    for (const part of parts) scope = [...new Set(scope.flatMap(node => walk(node, []).filter(item => item.matches(part))))];
    return scope;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  dispatchEvent(event) {
    event.target ||= this;
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    for (let node = this; node; node = node.parentElement) for (const callback of node.listeners[event.type] || []) callback(event);
    return true;
  }
  click() { this.dispatchEvent({type: 'click', bubbles: true}); }
  key(name) { const event = {type: 'keydown', key: name, bubbles: true}; this.dispatchEvent(event); return event; }
  focus() { if (this.isConnected) this.ownerDocument.activeElement = this; }
}

/** One element per id in the shipped page, with the attributes written there. */
function documentFrom(markup) {
  const doc = {createElement: tag => new Element(doc, tag), createElementNS: (_, tag) => new Element(doc, tag)};
  doc.body = new Element(doc, 'body');
  doc.activeElement = doc.body;
  doc.blurs = 0;
  doc.querySelector = selector => doc.body.querySelector(selector);
  doc.querySelectorAll = selector => doc.body.querySelectorAll(selector);
  doc.dispatchEvent = () => true;
  for (const [, tag, attributes] of markup.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="[^"]+"[^>]*)>/g)) {
    const node = doc.createElement(tag);
    for (const [, key, value] of attributes.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) node.setAttribute(key, value ?? '');
    doc.body.append(node);
  }
  return doc;
}

class Clock extends Date { static now() { return Clock.at; } }
Clock.at = NOW;

function sample(change = () => {}, extra = {}) {
  const fleetState = structuredClone(shared.fleetState);
  change(fleetState.facts);
  return {...structuredClone(shared.inference), fleetState, ...extra};
}
const setState = (facts, id, state) => { facts.find(fact => fact.id === id).state = state; };

function page({tabs = true} = {}) {
  Clock.at = NOW;
  const doc = documentFrom(html);
  const listeners = {};
  const window = {
    addEventListener(type, callback) { (listeners[type] ||= []).push(callback); },
    dispatchEvent(event) { for (const callback of listeners[event.type] || []) callback(event); return true; },
  };
  class CustomEvent { constructor(type, init = {}) { this.type = type; this.detail = init.detail; this.bubbles = init.bubbles === true; } }
  let listener;
  const state = (inference, error = null) => ({inference, inferenceStatus: {error}});
  const store = {subscribe(callback) { listener = callback; callback(state(null)); return () => {}; }, start() {}};
  const monitorContext = vm.createContext({Date: Clock, console, window, document: doc, CustomEvent, ...fleet,
    getMonitorStore: () => store});
  vm.runInContext(strip(read('./web/monitor.mjs')), monitorContext);
  if (tabs) {
    const tabsContext = vm.createContext({console, window, document: doc, CustomEvent, MouseEvent: CustomEvent,
      location: {search: ''}, ...mapTabs, appendEvidence() {}});
    vm.runInContext(strip(read('./web/monitor-map.mjs')), tabsContext);
  }
  const $ = id => doc.querySelector(`#${id}`);
  return {doc, window, $, monitorContext,
    publish(inference, error) { listener(state(inference, error)); },
    tab: id => $('monitorTabs').children.find(item => item.dataset.monitor === id),
    groups: () => $('map').children.filter(item => item.tagName === 'g'),
    lines: () => $('map').children.filter(item => item.tagName === 'line'),
    shown: () => $('map').children.filter(item => item.tagName === 'g' && !/\bis-outside-monitor\b/.test(item.className))
      .map(item => item.dataset.id),
    ledger: () => $('mapLedger').querySelectorAll('.j-step-label').map(item => item.textContent)};
}
const classes = node => node.className.split(/\s+/).filter(Boolean);

// ---- MAP: grouping, edge kinds, state marks, listener wording ----

test('a machine tab keeps its lanes visible in the diagram and the list', () => {
  const view = page();
  view.publish(sample());
  assert.deepEqual(view.shown().length, shared.fleetState.facts.length, 'All inference shows every fact');
  view.tab('registry:pc').click();
  assert.deepEqual(view.shown(), ['registry:pc', 'registry:pc:fast', 'registry:pc:deep']);
  assert.deepEqual(view.ledger(), ['Configured PC', 'PC fast', 'PC deep']);
  assert.equal(view.tab('registry:pc').getAttribute('aria-selected'), 'true');
  // Lines to hidden nodes are hidden with them; the PC's own lines stay.
  for (const line of view.lines()) {
    const inside = line.dataset.from === 'registry:pc';
    assert.equal(/\bis-outside-monitor\b/.test(line.className), !inside, `${line.dataset.from} to ${line.dataset.to}`);
  }
  view.tab('registry:pc:fast').click();
  assert.deepEqual(view.shown(), ['registry:pc:fast'], 'a lane tab shows that lane only');
  assert.deepEqual(view.ledger(), ['PC fast']);
  view.tab('all').click();
  assert.equal(view.shown().length, shared.fleetState.facts.length);
});

test('the diagram and the list use the same membership rule', () => {
  const tabsSource = read('./web/monitor-map.mjs');
  const rule = tabsSource.match(/const inMonitor = (\(active, node\) => [^;]+);/);
  assert.ok(rule, 'monitor-map.mjs states its membership rule in one place');
  const listed = vm.runInNewContext(`(${rule[1]})`);
  const nodes = [{id: 'm', machine: 'm'}, {id: 'm:a', machine: 'm'}, {id: 'n', machine: 'n'}, {id: 'machine:m', machine: 'x'}];
  for (const active of ['all', 'm', 'm:a', 'n', 'machine:m', 'absent']) {
    for (const node of nodes) assert.equal(listed(active, node), monitor.inMapMonitor(active, node), `${active} / ${node.id}`);
  }
  assert.equal(monitor.inMapMonitor('m', nodes[1]), true);
  assert.equal(monitor.inMapMonitor('m:a', nodes[0]), false);
  assert.equal(monitor.inMapMonitor('machine:m', nodes[1]), false, 'no invented "machine:" prefix');
});

test('a tab id that is no longer on the map shows every node instead of an empty diagram', () => {
  const view = page({tabs: false});
  view.window.__jaryoMapTab = 'registry:gone';
  view.publish(sample());
  assert.equal(view.shown().length, shared.fleetState.facts.length);
});

test('hosts, upstream and caller lines carry distinct classes and a text key', () => {
  const view = page({tabs: false});
  view.publish(sample(undefined, {edges: [
    {from: 'registry:pc:fast', to: 'registry:pc:deep', kind: 'upstream'},
    {from: 'mac:lmstudio', to: 'registry:pc:fast', kind: 'caller'},
    {from: 'mac', to: 'peer:spare', kind: 'mystery" onload="x'},
  ]}));
  const byKind = {};
  for (const line of view.lines()) (byKind[line.dataset.kind] ||= new Set()).add(line.className);
  assert.deepEqual(Object.keys(byKind).sort(), ['caller', 'hosts', 'other', 'upstream']);
  for (const [kind, names] of Object.entries(byKind)) {
    assert.deepEqual([...names], [`edge edge-${kind}`], `${kind} lines share one class`);
  }
  const key = view.$('mapKey');
  assert.equal(key.hidden, false);
  const entries = key.children.map(item => item.textContent);
  assert.equal(entries.length, 4);
  assert.equal(new Set(entries).size, 4);
  assert.match(entries[0], /^Solid line: /);
  assert.match(entries[1], /^Dashed line: /);
  assert.match(entries[2], /^Dotted line: /);
  assert.match(entries[3], /does not recognise/);
  // Each key row draws a sample with the class the diagram uses.
  assert.deepEqual(key.children.map(item => item.querySelector('line').className),
    ['edge edge-hosts', 'edge edge-upstream', 'edge edge-caller', 'edge edge-other']);
  // The key lists only what is drawn, and nothing when there are no lines.
  view.publish(sample());
  assert.deepEqual(view.$('mapKey').children.map(item => item.querySelector('line').className), ['edge edge-hosts']);
  vm.runInContext('draw({nodes: [{id: "solo", label: "Solo"}], edges: []}, null)', view.monitorContext);
  assert.equal(view.$('mapKey').hidden, true);
  assert.equal(view.$('mapKey').children.length, 0);
});

test('line kinds differ without colour: each class has its own dash pattern', () => {
  const patterns = ['hosts', 'upstream', 'caller', 'other'].map(kind => {
    const rule = monitorCss.match(new RegExp(`\\.j-map \\.edge-${kind}\\b[^{]*\\{[^}]*stroke-dasharray:\\s*([^;}]+)`));
    assert.ok(rule, `monitor.css sets a dash pattern for .edge-${kind}`);
    assert.match(rule[0], new RegExp(`\\.j-map-key \\.edge-${kind}\\b`), 'the key sample uses the same rule');
    return rule[1].trim();
  });
  assert.equal(new Set(patterns).size, 4);
});

test('the state class is on the node group the stylesheet targets, and every emitted class has a rule', () => {
  const view = page({tabs: false});
  view.monitorContext.fixture = {
    nodes: [
      {id: 'a', machine: 'a', role: 'machine', label: 'A', health: 'ok'},
      {id: 'a:1', machine: 'a', label: 'One', health: 'degraded'},
      {id: 'a:2', machine: 'a', label: 'Two', health: 'down'},
      {id: 'b', machine: 'b', role: 'machine', label: 'B', health: 'not_measured'},
      {id: 'b:1', machine: 'b', label: 'Unreported'},
      {id: 'b:2', machine: 'b', label: 'Odd', health: 'ok selected" onload="x'},
    ],
    edges: [{from: 'a', to: 'a:1', kind: 'hosts'}, {from: 'a:1', to: 'a:2', kind: 'upstream'},
      {from: 'a:2', to: 'b', kind: 'caller'}, {from: 'b', to: 'b:1', kind: 'unheard-of'}],
  };
  view.window.__jaryoMapTab = 'a';
  vm.runInContext('draw(fixture, "a:1")', view.monitorContext);
  const groups = Object.fromEntries(view.groups().map(group => [group.dataset.id, group]));
  assert.deepEqual(classes(groups.a), ['node', 'ok']);
  assert.deepEqual(classes(groups['a:1']), ['node', 'degraded', 'selected']);
  assert.deepEqual(classes(groups['a:2']), ['node', 'down']);
  assert.deepEqual(classes(groups.b), ['node', 'not_measured', 'is-outside-monitor']);
  assert.deepEqual(classes(groups['b:1']), ['node', 'not_measured', 'is-outside-monitor'], 'no reading is not measured');
  assert.deepEqual(classes(groups['b:2']), ['node', 'not_measured', 'is-outside-monitor'], 'an unknown value is not measured');
  for (const group of view.groups()) {
    assert.equal(group.children.find(item => item.tagName === 'circle').className, '', 'the mark itself carries no state class');
  }
  const emitted = new Set();
  const collect = node => { classes(node).forEach(name => emitted.add(name)); node.children.forEach(collect); };
  [...view.groups(), ...view.lines(), ...view.$('mapKey').children].forEach(collect);
  for (const name of ['node', 'ok', 'degraded', 'down', 'not_measured', 'selected', 'is-outside-monitor', 'sub',
    'edge', 'edge-hosts', 'edge-upstream', 'edge-caller', 'edge-other']) {
    assert.ok(emitted.has(name), `draw() emits ${name}`);
  }
  for (const name of emitted) {
    assert.match(css, new RegExp(`\\.j-map(?:-key)?\\b[^{}]*\\.${name}(?![\\w-])[^{}]*\\{`), `a .j-map rule targets .${name}`);
  }
});

test('the listener line uses listener vocabulary and never reports SSH or the PC as up or down', () => {
  assert.equal(monitor.tunnelLine(null), '');
  const detail = 'Local forward listeners not accepting on 127.0.0.1: 1334. '
    + 'This is listener state on this Mac, not SSH or PC reachability.';
  assert.equal(monitor.tunnelLine({state: 'down', detail}), detail, 'the collector detail is shown as written');
  const up = monitor.tunnelLine({state: 'up'}), down = monitor.tunnelLine({state: 'down'});
  for (const line of [up, down]) {
    assert.match(line, /^Local forward listeners are (?:not )?accepting connections\./);
    assert.match(line, /not SSH or PC reachability/);
    assert.doesNotMatch(line, /tunnel (?:is )?(?:up|down)/i);
  }
  assert.notEqual(up, down);
  for (const state of ['not_measured', 'unknown', undefined, 'UP']) {
    const line = monitor.tunnelLine({state});
    assert.match(line, /not measured/, `${state} is not read as accepting or closed`);
    assert.doesNotMatch(line, /accepting/);
  }
  assert.doesNotMatch(read('./web/monitor.mjs'), /PC tunnel (?:up|down)|SSH tunnel is down/);
});

// ---- MAP2: keyboard, landmarks, pause, announcements, natural size ----

test('an arrow key leaves focus on a connected tab, and an incoming observation keeps it', () => {
  const view = page();
  view.publish(sample());
  view.tab('all').focus();
  const event = view.tab('all').key('ArrowRight');
  assert.equal(event.defaultPrevented, true);
  let focused = view.doc.activeElement;
  assert.equal(focused.isConnected, true, 'focus is on an element in the page');
  assert.equal(focused.dataset.monitor, 'mac');
  assert.equal(focused.getAttribute('aria-selected'), 'true');
  assert.equal(focused.tabIndex, 0);
  assert.equal(view.tab('all').tabIndex, -1);
  // A new observation repaints the strip. The focused tab is the same connected element.
  Clock.at = NOW + 1000;
  const strip = [...view.$('monitorTabs').children];
  view.doc.blurs = 0;
  view.publish(sample(facts => setState(facts, 'registry:pc:fast', 'endpoint unavailable')));
  assert.equal(view.doc.activeElement, focused);
  assert.equal(view.doc.activeElement.isConnected, true);
  assert.equal(view.doc.blurs, 0, 'the focused tab is never taken out of the page, so no blur reaches a screen reader');
  assert.deepEqual([...view.$('monitorTabs').children], strip, 'the strip is updated in place, not rebuilt');
  assert.match(view.tab('registry:pc:fast').textContent, /PC fast/);
  view.doc.activeElement.key('End');
  focused = view.doc.activeElement;
  assert.equal(focused.isConnected, true);
  assert.equal(focused.dataset.monitor, 'registry:pro:deep');
  // The focused monitor leaves the map: focus moves to the selected tab, not to a detached node.
  view.publish(sample(facts => { facts.splice(facts.findIndex(fact => fact.id === 'registry:pro:deep'), 1); }));
  assert.equal(view.tab('registry:pro:deep'), undefined);
  assert.equal(view.doc.activeElement.isConnected, true);
  assert.equal(view.doc.activeElement.dataset.monitor, 'all');
  assert.equal(view.doc.activeElement.getAttribute('aria-selected'), 'true');
});

test('a focused tab is not moved, and so not blurred, when an earlier tab leaves the map', () => {
  const view = page();
  view.publish(sample());
  view.tab('all').focus();
  view.doc.activeElement.key('End');
  const focused = view.doc.activeElement;
  const last = view.$('monitorTabs').children.at(-1);
  assert.equal(focused, last, 'focus is on the last tab');
  assert.ok(view.tab('mac'), 'the sample has a mac tab before the focused one');
  const others = [...view.$('monitorTabs').children].filter(tab => tab.dataset.monitor !== 'mac');
  Clock.at = NOW + 1000;
  view.doc.blurs = 0;
  view.$('monitorTabs').moves = 0;
  view.publish(sample(facts => { facts.splice(facts.findIndex(fact => fact.id === 'mac'), 1); }));
  assert.equal(view.tab('mac'), undefined, 'the departed tab is gone');
  assert.equal(view.doc.blurs, 0, 'the kept, focused tab is never detached or moved');
  assert.equal(view.$('monitorTabs').moves, 0, 'departed tabs are removed first, so no kept tab is moved at all');
  assert.equal(view.doc.activeElement, focused);
  assert.equal(focused.isConnected, true);
  assert.deepEqual([...view.$('monitorTabs').children], others, 'the kept tabs are the same elements in the same order');
  // A tab that arrives before the focused one is inserted without moving the focused one.
  Clock.at = NOW + 2000;
  view.publish(sample());
  assert.ok(view.tab('mac'), 'the tab is back');
  assert.equal(view.doc.blurs, 0, 'an arriving tab does not move the focused tab');
  assert.equal(view.doc.activeElement, focused);
  // The feed lists the facts in another order: the other tabs are placed around the focused one.
  for (const [at, order] of [[NOW + 3000, facts => facts.reverse()], [NOW + 4000, () => {}]]) {
    Clock.at = at;
    const data = sample(order);
    view.publish(data);
    const ids = [...view.$('monitorTabs').children].map(tab => tab.dataset.monitor);
    assert.deepEqual(ids, ['all', ...data.fleetState.facts.map(fact => fact.id)], 'the strip follows the order of the feed');
    assert.equal(view.doc.blurs, 0, 'a reorder moves the other tabs, not the focused one');
    assert.equal(view.doc.activeElement, focused);
  }
});

test('a repaint does not take focus from elsewhere on the page', () => {
  const view = page();
  view.publish(sample());
  view.$('mapPause').focus();
  view.publish(sample(facts => setState(facts, 'registry:pc:fast', 'endpoint unavailable')));
  assert.equal(view.doc.activeElement, view.$('mapPause'));
});

test('tabs point at a labelled panel that is on the page and holds what they filter', () => {
  const view = page();
  view.publish(sample());
  const panel = view.$('mapPanel');
  assert.ok(panel, 'monitor.html has #mapPanel');
  assert.equal(panel.getAttribute('role'), 'tabpanel');
  assert.equal(panel.hidden, false);
  for (const tab of view.$('monitorTabs').children) assert.equal(tab.getAttribute('aria-controls'), 'mapPanel');
  assert.equal(panel.getAttribute('aria-label'), 'Map monitor: All inference');
  view.tab('registry:pc').click();
  assert.equal(panel.getAttribute('aria-label'), 'Map monitor: Configured PC');
  const open = html.indexOf('<div id="mapPanel"');
  assert.ok(open > 0);
  for (const id of ['content', 'map', 'mapKey', 'mapLedger']) {
    assert.ok(html.indexOf(`id="${id}"`) > open, `#${id} is inside the panel`);
  }
});

test('the page has exactly one main and one h1, and the navigation stays outside main', () => {
  assert.equal((html.match(/<main\b/g) || []).length, 1);
  assert.equal((html.match(/<\/main>/g) || []).length, 1);
  assert.equal((html.match(/<h1\b/g) || []).length, 1);
  const main = html.indexOf('<main'), end = html.indexOf('</main>');
  assert.ok(html.indexOf('</nav>') < main, 'nav comes before main');
  for (const marker of ['<h1', 'id="tunnel"', 'id="mapPause"', 'id="mapAnnounce"', 'id="monitorTabs"', 'id="mapPanel"', 'id="mapLedger"']) {
    const at = html.indexOf(marker);
    assert.ok(at > main && at < end, `${marker} is inside main`);
  }
  assert.match(html, /<h1[^>]*>Inference map<\/h1>/);
});

test('the captured time and the announcements are visible text, not the clipped hint style', () => {
  // .j-hint is clipped to one pixel in jaryo.css; these two lines must be readable on the page.
  for (const id of ['mapCaptured', 'mapAnnounce']) {
    const tag = html.match(new RegExp(`<p id="${id}"[^>]*>`))[0];
    assert.match(tag, /class="j-map-note"/);
    assert.doesNotMatch(tag, /j-hint/);
  }
  const rule = monitorCss.match(/\.j-map-note\s*\{([^}]*)\}/)[1];
  assert.doesNotMatch(rule, /clip|position:\s*absolute|height:\s*1px/);
  // An empty live region that is display:none may not announce its first message.
  assert.doesNotMatch(monitorCss, /#mapAnnounce[^{]*:empty[^{]*\{[^}]*display:\s*none/);
});

test('Pause display updates sets aria-pressed, shows the captured time and stops redraw until resumed', () => {
  assert.match(html, /<button id="mapPause" type="button" aria-pressed="false">Pause display updates<\/button>/);
  const view = page();
  const detail = 'Local forward listeners on 127.0.0.1 are accepting connections. '
    + 'This is listener state on this Mac, not SSH or PC reachability.';
  view.publish(sample(undefined, {tunnel: {state: 'up', detail}, observedAt: new Date(NOW).toISOString()}));
  const pause = view.$('mapPause'), captured = view.$('mapCaptured');
  assert.equal(captured.hidden, true);
  const before = view.groups();
  pause.click();
  assert.equal(pause.getAttribute('aria-pressed'), 'true');
  assert.equal(captured.hidden, false);
  assert.match(captured.textContent, /paused/i);
  assert.ok(captured.textContent.includes(new Date(NOW).toLocaleTimeString()), captured.textContent);
  assert.equal(captured.getAttribute('data-captured-at'), new Date(NOW).toISOString());
  assert.match(captured.textContent, /not shown/, 'the paused view makes no current claim');
  // Newer observations arrive, including a feed failure. Nothing on the page moves.
  Clock.at = NOW + 2000;
  view.publish(sample(facts => setState(facts, 'registry:pc:fast', 'endpoint unavailable'),
    {tunnel: {state: 'down', detail: 'Local forward listeners not accepting on 127.0.0.1: 1334.'}, observedAt: new Date(NOW + 2000).toISOString()}));
  view.publish(null, {code: 'request_failed'});
  assert.deepEqual(view.groups(), before, 'the diagram is not redrawn');
  assert.equal(view.$('tunnel').textContent, detail);
  assert.equal(view.$('mapAnnounce').textContent, '');
  // The captured map can still be inspected.
  before.find(group => group.dataset.id === 'registry:pc:fast').click();
  assert.equal(view.$('detail').hidden, false);
  assert.equal(view.$('detailTitle').textContent, 'PC fast');
  assert.match(view.$('detailFacts').textContent, /endpoint answering/);
  assert.equal(pause.getAttribute('aria-pressed'), 'true');
  // Resume draws the newest state the store delivered while paused.
  pause.click();
  assert.equal(pause.getAttribute('aria-pressed'), 'false');
  assert.equal(captured.hidden, true);
  assert.equal(captured.textContent, '');
  assert.equal(view.$('tunnel').textContent, 'Monitor feed unavailable');
  view.publish(sample(facts => setState(facts, 'registry:pc:fast', 'endpoint unavailable')));
  const fast = view.groups().find(group => group.dataset.id === 'registry:pc:fast');
  assert.deepEqual(classes(fast).slice(0, 2), ['node', 'down']);
  assert.match(view.$('mapAnnounce').textContent, /PC fast: ENDPOINT ANSWERING to ENDPOINT UNAVAILABLE/);
});

test('state transitions are announced once per update in one batch; ages and the first sample are not', () => {
  assert.match(html, /<p id="mapAnnounce"[^>]*role="status"[^>]*aria-live="polite"[^>]*>/);
  const view = page();
  const region = view.$('mapAnnounce');
  view.publish(sample());
  assert.equal(region.textContent, '', 'the first sample is not a transition');
  Clock.at = NOW + 1000;
  view.publish(sample());
  assert.equal(region.textContent, '', 'an age tick is not announced');
  view.publish(sample(facts => { setState(facts, 'registry:pc:fast', 'endpoint unavailable'); setState(facts, 'registry:pc:deep', 'unknown'); }));
  assert.equal(region.textContent,
    'PC fast: ENDPOINT ANSWERING to ENDPOINT UNAVAILABLE. PC deep: ENDPOINT ANSWERING to UNKNOWN.');
  // The same state again says nothing new; the earlier line is not repeated as a fresh write.
  let writes = 0;
  const original = Object.getOwnPropertyDescriptor(Element.prototype, 'textContent');
  Object.defineProperty(region, 'textContent', {configurable: true, get() { return original.get.call(this); },
    set(value) { writes += 1; original.set.call(this, value); }});
  view.publish(sample(facts => { setState(facts, 'registry:pc:fast', 'endpoint unavailable'); setState(facts, 'registry:pc:deep', 'unknown'); }));
  assert.equal(writes, 0);
  // A node that leaves and one that arrives are named; a long batch is capped with a count.
  view.publish(sample(facts => {
    for (const id of ['peer:spare', 'registry:pro:deep']) facts.splice(facts.findIndex(fact => fact.id === id), 1);
    setState(facts, 'mac:lmstudio', 'unknown');
  }));
  assert.equal(writes, 1, 'one write per update');
  assert.equal(region.textContent, 'Local endpoint: ENDPOINT ANSWERING to UNKNOWN. '
    + 'PC fast: ENDPOINT UNAVAILABLE to ENDPOINT ANSWERING. PC deep: UNKNOWN to ENDPOINT ANSWERING. '
    + 'Unmatched peer is no longer on the map. And 1 more change.');
  view.publish(sample());
  assert.match(region.textContent, /Unmatched peer added: REPORTED\./);
});

test('the pure transition helper names changes, arrivals and departures and nothing else', () => {
  const before = new Map([['a', {label: 'A', word: 'OK'}], ['b', {label: 'B', word: 'STALE'}]]);
  assert.deepEqual(monitor.mapTransitions(null, [{id: 'a', label: 'A', factState: 'ok'}]), [], 'no baseline, no transition');
  assert.deepEqual(monitor.mapTransitions(before, [{id: 'a', label: 'A', factState: 'ok'}, {id: 'c', label: 'C', factState: 'unknown'}]),
    ['C added: UNKNOWN', 'B is no longer on the map']);
  assert.deepEqual(monitor.mapTransitions(before, [{id: 'a', label: 'A2', factState: 'ok', ageSeconds: 9}, {id: 'b', label: 'B', factState: 'stale'}]), []);
  assert.equal(monitor.announcementText([]), '');
  assert.equal(monitor.announcementText(['one', 'two']), 'one. two.');
  assert.equal(monitor.announcementText(['1', '2', '3', '4', '5', '6']), '1. 2. 3. 4. And 2 more changes.');
});

test('mute stops announcements, says so, and leaves the drawing live', () => {
  assert.match(html, /<button id="mapMute" type="button" aria-pressed="false">Mute announcements<\/button>/);
  const view = page();
  const mute = view.$('mapMute'), region = view.$('mapAnnounce');
  view.publish(sample());
  mute.click();
  assert.equal(mute.getAttribute('aria-pressed'), 'true');
  assert.match(region.textContent, /^Announcements muted\./);
  const said = region.textContent;
  view.publish(sample(facts => setState(facts, 'registry:pc:fast', 'endpoint unavailable')));
  assert.equal(region.textContent, said, 'a muted transition is not written to the live region');
  assert.deepEqual(classes(view.groups().find(group => group.dataset.id === 'registry:pc:fast')).slice(0, 2), ['node', 'down']);
  mute.click();
  assert.equal(mute.getAttribute('aria-pressed'), 'false');
  assert.match(region.textContent, /^Announcements on\./);
  view.publish(sample());
  assert.equal(region.textContent, 'PC fast: ENDPOINT UNAVAILABLE to ENDPOINT ANSWERING.', 'only changes after unmuting are spoken');
});

test('the graph keeps its natural size inside a named, focusable scroller', () => {
  const view = page({tabs: false});
  view.publish(sample());
  const svg = view.$('map');
  const [, , width, height] = svg.getAttribute('viewBox').split(' ').map(Number);
  assert.ok(width >= 720 && height >= 420);
  assert.equal(svg.getAttribute('width'), String(width));
  assert.equal(svg.getAttribute('height'), String(height));
  assert.equal(svg.style.width, `${width}px`);
  assert.equal(svg.style.height, `${height}px`);
  assert.doesNotMatch(read('./web/monitor.mjs'), /style\.width\s*=\s*'100%'/);
  assert.match(html, /<div class="j-map" id="content" role="region" aria-label="[^"]+" tabindex="0">\s*<svg id="map"/);
  assert.match(monitorCss, /\.j-map\s*\{[^}]*overflow-x:\s*auto/);
  assert.match(monitorCss, /\.j-map #map\s*\{[^}]*max-width:\s*none/);
  // The graph's grid track is as wide as the graph in every layout, so it cannot run under the detail column.
  const tracks = [...monitorCss.matchAll(/\.j-map(?::has\([^{]*\))?\s*\{[^}]*grid-template-columns:\s*([^;}]+)/g)].map(rule => rule[1].trim());
  assert.deepEqual(tracks, ['max-content', 'max-content minmax(260px,1fr)', 'max-content']);
  // Fourteen CSS pixels in the drawing are fourteen on screen: one user unit per pixel.
  assert.match(monitorCss, /\.node text\s*\{[^}]*font-size:\s*14px/);
});
