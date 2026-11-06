/**
 * Tab strip for the native map. It only filters the drawing and restates
 * node fields already on the inference map. It does not probe or control.
 */
import {mapMonitors, mapEvidence, moveMonitorTab, DEMO_BANNER} from '/monitor-tabs.mjs';
import {appendEvidence} from '/monitor-board.mjs';

const DEMO_MAP = {
  fictional: true,
  label: DEMO_BANNER,
  nodes: [
    {id: 'mac', machine: 'mac', role: 'machine', label: 'Mac', health: 'ok'},
    {id: 'mac:lmstudio', machine: 'mac', role: 'lmstudio', label: 'LM Studio', health: 'ok', modelId: 'example-local', generationTokensPerSecond: 12.5},
    {id: 'pc', machine: 'pc', role: 'machine', label: 'PC', health: 'not_measured', reason: 'tunnel_down'},
    {id: 'pc:router', machine: 'pc', role: 'router', label: 'Router', health: 'not_measured', reason: 'tunnel_down'},
  ],
  edges: [],
  tunnel: {state: 'down', detail: 'Fictional tunnel. Not a live observation.'},
};

function demoRequested() {
  try { return /(?:^|[?&])demo=1(?:&|$)/.test(String(location.search)); }
  catch (_) { return false; }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function paint(detail) {
  const host = document.querySelector('#monitorTabs');
  const ledger = document.querySelector('#mapLedger');
  if (!host || host.getAttribute('data-jaryo-tabs') !== '1') return;
  const demo = demoRequested();
  const banner = document.querySelector('#jaryoDemo');
  if (banner) {
    banner.hidden = !demo;
    banner.textContent = demo ? `${DEMO_BANNER} Tabs below use a fictional map. The diagram uses the live feed.` : '';
  }
  const source = demo ? DEMO_MAP : detail?.map;
  const monitors = mapMonitors(source, {feedUnavailable: demo ? false : detail?.feedUnavailable === true || !source});
  const active = monitors.some(item => item.id === window.__jaryoMapTab) ? window.__jaryoMapTab : 'all';
  window.__jaryoMapTab = active;
  host.replaceChildren();
  for (const tab of monitors) {
    const button = el('button', 'j-tab');
    button.type = 'button';
    button.dataset.monitor = tab.id;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(tab.id === active));
    button.tabIndex = tab.id === active ? 0 : -1;
    button.append(el('span', 'j-glyph', tab.glyph), el('span', 'j-tab-label', tab.title), el('span', 'j-word', tab.word));
    host.append(button);
  }
  if (!ledger) return;
  ledger.hidden = false;
  const nodes = Array.isArray(source?.nodes) ? source.nodes : [];
  const visible = active === 'all' ? nodes : nodes.filter(node => node.id === active || `machine:${node.machine}` === active);
  ledger.replaceChildren(el('p', 'j-eyebrow', demo ? 'FICTIONAL MAP' : 'LIVE MAP'));
  ledger.append(el('h2', 'j-board-title', active === 'all' ? 'All inference' : (visible[0]?.label || active)));
  appendEvidence(ledger, mapEvidence(source, {feedUnavailable: demo ? false : detail?.feedUnavailable === true || !source, fictional: demo}));
  if (!visible.length) ledger.append(el('p', 'j-unavailable j-callout', '○ UNAVAILABLE · no map nodes in this sample'));
  const list = el('ul', 'j-steps');
  for (const node of visible) {
    const face = monitors.find(item => item.id === node.id);
    const row = el('li', 'j-step');
    const button = el('button', 'j-step-hit');
    button.type = 'button';
    button.append(el('span', 'j-glyph', face?.glyph || '?'), el('span', 'j-step-label', node.label || node.id), el('span', 'j-word', face?.word || 'UNKNOWN'));
    button.addEventListener('click', () => focusNode(node.id));
    row.append(button);
    if (face?.reason) row.append(el('p', 'j-step-reason', face.reason));
    list.append(row);
  }
  ledger.append(list);
  ledger.append(el('p', 'j-footnote', 'Choosing a map monitor hides other nodes in the diagram. It does not pause, cancel, or restart inference.'));
}

function focusNode(id) {
  window.__jaryoMapTab = id;
  window.__jaryoRerenderMap?.();
  const group = [...document.querySelectorAll('#map g')].find(node => node.dataset?.id === id);
  group?.dispatchEvent(new MouseEvent('click', {bubbles: true}));
}

function bind() {
  const host = document.querySelector('#monitorTabs');
  if (!host || host.dataset.mapBound === '1') return;
  host.dataset.mapBound = '1';
  host.addEventListener('click', event => {
    const tab = event.target.closest?.('[data-monitor]');
    if (!tab) return;
    window.__jaryoMapTab = tab.dataset.monitor;
    window.__jaryoRerenderMap?.();
    paint({map: window.__jaryoLastMap, feedUnavailable: window.__jaryoFeedUnavailable});
  });
  host.addEventListener('keydown', event => {
    const tabs = [...host.querySelectorAll('[role="tab"]')];
    const next = moveMonitorTab(tabs.map(tab => tab.dataset.monitor), window.__jaryoMapTab || 'all', event.key);
    if (!next) return;
    event.preventDefault();
    window.__jaryoMapTab = next;
    window.__jaryoRerenderMap?.();
    paint({map: window.__jaryoLastMap, feedUnavailable: window.__jaryoFeedUnavailable});
    tabs.find(tab => tab.dataset.monitor === next)?.focus();
  });
}

window.addEventListener('agiw-inference-map', event => {
  window.__jaryoLastMap = event.detail?.map || null;
  window.__jaryoFeedUnavailable = event.detail?.feedUnavailable === true;
  bind();
  paint(event.detail || {});
});

bind();
if (demoRequested()) paint({map: null, feedUnavailable: true});
