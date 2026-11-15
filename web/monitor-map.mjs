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

// A map tab is a fact id: a machine tab holds the machine and its lanes, a lane tab that lane.
// web/monitor.mjs draws with the same rule (inMapMonitor); test_map_interaction.mjs keeps them equal.
const inMonitor = (active, node) => active === 'all' || node.id === active || node.machine === active;

/**
 * Update the strip in place. A tab that stays keeps its element, so keyboard
 * focus survives every repaint; only tabs that leave the map are removed.
 */
function paintTabs(host, monitors, active) {
  const panel = document.querySelector('#mapPanel');
  const kept = new Map([...host.querySelectorAll('[role="tab"]')].map(tab => [tab.dataset.monitor, tab]));
  const focused = document.activeElement;
  const hadFocus = Boolean(focused) && focused !== host && host.contains(focused);
  const buttons = monitors.map(tab => {
    let button = kept.get(tab.id);
    kept.delete(tab.id);
    if (!button) {
      button = el('button', 'j-tab');
      button.type = 'button';
      button.dataset.monitor = tab.id;
      button.setAttribute('role', 'tab');
      button.append(el('span', 'j-glyph'), el('span', 'j-tab-label'), el('span', 'j-word'));
    }
    button.setAttribute('aria-selected', String(tab.id === active));
    if (panel && !panel.hidden) button.setAttribute('aria-controls', 'mapPanel');
    button.tabIndex = tab.id === active ? 0 : -1;
    for (const [name, value] of [['.j-glyph', tab.glyph], ['.j-tab-label', tab.title], ['.j-word', tab.word]]) {
      const part = button.querySelector(name);
      if (part && part.textContent !== String(value ?? '')) part.textContent = value ?? '';
    }
    return button;
  });
  // Departed tabs go first, so no kept tab is compared with one that is leaving. The tabs are then
  // placed around the focused one: a browser drops focus from a node that is moved, so it never is.
  for (const gone of kept.values()) gone.remove();
  const anchor = hadFocus ? buttons.indexOf(focused) : -1;
  buttons.forEach((button, index) => {
    if (index === anchor) return;
    const at = host.children[anchor < 0 || index < anchor ? index : [...host.children].indexOf(focused) + index - anchor];
    if (at !== button) { if (at) host.insertBefore(button, at); else host.append(button); }
  });
  if (panel) panel.setAttribute('aria-label', `Map monitor: ${monitors.find(item => item.id === active)?.title || 'All inference'}`);
  if (!hadFocus) return;
  // Focus stays in the strip: on the same tab when it is still there, else on the selected tab.
  const target = focused.isConnected ? focused : host.querySelector('[aria-selected="true"]');
  if (target && document.activeElement !== target) target.focus();
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
  paintTabs(host, monitors, active);
  if (!ledger) return;
  ledger.hidden = false;
  const nodes = Array.isArray(source?.nodes) ? source.nodes : [];
  const visible = nodes.filter(node => inMonitor(active, node));
  const previous = ledger.querySelector('[data-jaryo-choices]');
  const sameMonitor = ledger.dataset.monitor === active;
  const resume = sameMonitor ? previous?.choiceViewState?.() : null;
  previous?.choiceCleanup?.();
  if (!sameMonitor) delete ledger.dataset.selectedStep;
  ledger.dataset.monitor = active;
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
  const presentation = el('div');
  presentation.setAttribute('data-jaryo-choices', '');
  presentation.jaryoChoices = {
    steps: visible.map(node => {
      const face = monitors.find(item => item.id === node.id);
      return {id: node.id, name: node.label || node.id, word: face?.word || 'UNKNOWN', reason: face?.reason};
    }),
    selectedId: ledger.dataset.selectedStep, resume, openMonitor: true,
    onSelect: id => { ledger.dataset.selectedStep = id; },
  };
  presentation.append(list);
  ledger.append(presentation);
  document.dispatchEvent(new CustomEvent('jaryo:pipeline', {detail: presentation}));
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
  document.querySelector('#mapLedger')?.addEventListener('click', event => {
    const open = event.target.closest?.('[data-open-monitor]');
    if (open) focusNode(open.dataset.openMonitor);
  });
  host.addEventListener('click', event => {
    const tab = event.target.closest?.('[data-monitor]');
    if (!tab) return;
    window.__jaryoMapTab = tab.dataset.monitor;
    window.__jaryoRerenderMap?.();
    paint({map: window.__jaryoLastMap, feedUnavailable: window.__jaryoFeedUnavailable});
  });
  host.addEventListener('keydown', event => {
    const ids = [...host.querySelectorAll('[role="tab"]')].map(tab => tab.dataset.monitor);
    const next = moveMonitorTab(ids, window.__jaryoMapTab || 'all', event.key);
    if (!next) return;
    event.preventDefault();
    window.__jaryoMapTab = next;
    window.__jaryoRerenderMap?.();
    paint({map: window.__jaryoLastMap, feedUnavailable: window.__jaryoFeedUnavailable});
    // Look the tab up after the repaint, so focus lands on an element that is on the page.
    [...host.querySelectorAll('[role="tab"]')].find(tab => tab.dataset.monitor === next)?.focus();
  });
}

window.addEventListener('agiw-inference-map', event => {
  window.__jaryoLastMap = event.detail?.map || null;
  window.__jaryoFeedUnavailable = event.detail?.feedUnavailable === true;
  bind();
  paint(event.detail || {});
});

bind();
if (window.__jaryoMapDetail) paint(window.__jaryoMapDetail);
else if (demoRequested()) paint({map: null, feedUnavailable: false});
