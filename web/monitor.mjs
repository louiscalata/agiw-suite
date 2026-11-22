import {fleetFacts} from './fleet-state.mjs';
import {getMonitorStore} from './monitor-store.mjs';
const NS = 'http://www.w3.org/2000/svg';
const LANES = {
  mac: {x: 120, y: 48},
  'mac:lmstudio': {x: 120, y: 160},
  'mac:veritas': {x: 120, y: 272},
  pc: {x: 460, y: 48},
};

export function place(nodes) {
  if ((nodes || []).some(node => node.role === 'machine' && (node.scope === 'machine' || node.observationSource === 'registry'))) {
    const roots = nodes.filter(node => node.role === 'machine');
    const placed = [], grouped = new Set();
    let top = 42;
    // Three columns keep the local machine and both remotes together. Further
    // machines get another row, sized to the tallest group above it.
    for (let start = 0; start < roots.length; start += 3) {
      const band = roots.slice(start, start + 3);
      let rows = 0;
      band.forEach((root, column) => {
        const x = 28 + column * 330;
        placed.push({...root, x, y: top});
        grouped.add(root.id);
        const children = nodes.filter(node => node.role !== 'machine' && node.machine === root.machine);
        rows = Math.max(rows, children.length);
        children.forEach((child, row) => {
          placed.push({...child, x: x + 12, y: top + (row + 1) * 72});
          grouped.add(child.id);
        });
      });
      top += (rows + 2) * 72;
    }
    for (const node of nodes.filter(node => !grouped.has(node.id))) {
      placed.push({...node, x: 40, y: top});
      top += 72;
    }
    return placed;
  }
  const placed = [];
  const extra = [];
  const configuredMachines = new Set((nodes || [])
    .filter(node => node.role === 'machine' && node.inventoryScope === 'configured-lanes')
    .map(node => node.machine));
  for (const node of nodes || []) {
    if (LANES[node.id] && !configuredMachines.has(node.machine)) placed.push({...node, ...LANES[node.id]});
    else extra.push(node);
  }
  // Keep additional machine roots beside their own lanes. One vertical row
  // per label avoids the former six-column overlap for long server identities.
  const roots = extra.filter(node => node.role === 'machine');
  const grouped = new Set();
  let y = 390;
  for (const root of roots) {
    placed.push({...root, x: 40, y});
    grouped.add(root.id);
    y += 60;
    for (const child of extra.filter(node => node.role !== 'machine' && node.machine === root.machine)) {
      placed.push({...child, x: 80, y});
      grouped.add(child.id);
      y += 60;
    }
    y += 30;
  }
  for (const node of extra.filter(node => !grouped.has(node.id))) {
    placed.push({...node, x: 40, y});
    y += 60;
  }
  return placed;
}

/** Keep repository model IDs intact, abbreviating only filesystem paths. */
export function displayModelId(value) {
  if (typeof value !== 'string') return '';
  return /^(?:\/|~[\\/]|[A-Za-z]:[\\/]|\\\\)/.test(value)
    || /[\\/].*\.(?:gguf|safetensors|bin)$/i.test(value)
    ? value.replaceAll('\\', '/').split('/').filter(Boolean).pop() || value : value;
}

const caption = value => String(value || '').length > 38 ? `${String(value).slice(0, 35)}…` : String(value || '');

/** The one word for a reading that is missing or unavailable (LOU-59). */
export const NOT_MEASURED = 'Not measured';

function number(value, suffix) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value}${suffix}` : NOT_MEASURED;
}

export function detailRows(node) {
  const slots = node.slotsBusy == null && node.slotsTotal == null
    ? NOT_MEASURED : `${node.slotsBusy ?? NOT_MEASURED}/${node.slotsTotal ?? NOT_MEASURED}`;
  const context = node.contextUsed == null && node.contextLimit == null
    ? NOT_MEASURED : `${node.contextUsed ?? NOT_MEASURED}/${node.contextLimit ?? NOT_MEASURED}`;
  const vram = node.vramUsedMiB == null && node.vramTotalMiB == null
    ? NOT_MEASURED : node.vramUsedMiB != null && node.vramTotalMiB != null
      ? `${node.vramUsedMiB}/${node.vramTotalMiB} MiB`
      : `${node.vramUsedMiB == null ? NOT_MEASURED : `${node.vramUsedMiB} MiB`}/${node.vramTotalMiB == null ? NOT_MEASURED : `${node.vramTotalMiB} MiB`}`;
  const rows = [
    [node.scope ? 'State' : 'Health', node.factState || node.health || NOT_MEASURED],
    [node.scope ? (node.factState === 'stale' ? 'Last reported model' : 'Reported model') : 'Model', (node.observationSource === 'registry' || node.scope ? displayModelId(node.modelId) : node.modelId) || NOT_MEASURED],
    ['Upstream', node.upstream || NOT_MEASURED],
    [node.scope ? 'Configured endpoint' : 'Endpoint', node.endpoint || NOT_MEASURED],
    ['Prompt tok/s', number(node.promptTokensPerSecond, '')],
    ['Generation tok/s', number(node.generationTokensPerSecond, '')],
    ['Latency', number(node.latencyMs, ' ms')],
    ['Queue', node.queueDepth == null ? NOT_MEASURED : String(node.queueDepth)],
    ['Slots', slots],
    ['Context', context],
    ['VRAM', vram],
  ];
  if (node.scope) {
    rows.push(['ID', node.id], ['Source', node.source || 'unknown'],
      ['Completeness', node.completeness || 'unknown'],
      [node.scope === 'lane' ? 'Last probe' : 'Observed time', node.evidence?.lastProbeAt || node.observedAt || 'Not observed'],
      ['Last success', node.lastSuccessAt || node.evidence?.lastSuccessAt || 'Not available'],
      ['Last successful model', node.evidence?.lastSuccessModelId || 'Not available'],
      ['Probe state', node.evidence?.probeState || 'Not reported'],
      ['Configured deadline', node.evidence?.probeTimeoutSeconds == null ? 'Not configured' : `${node.evidence.probeTimeoutSeconds} s`],
      ['Configured context', node.evidence?.contextTokens == null ? 'Not configured' : String(node.evidence.contextTokens)],
      ['Configured thinking', node.evidence?.thinking || 'Not configured']);
  }
  if (node.observationSource === 'registry' || node.scope) {
    rows.push(['Observation age', typeof node.ageSeconds === 'number' && Number.isFinite(node.ageSeconds)
      ? `${node.ageSeconds.toFixed(1)} s` : NOT_MEASURED]);
  }
  if (node.detail) rows.push(['Detail', node.detail]);
  if (node.reason) rows.push(['Reason', node.reason]);
  return rows;
}

/**
 * The collector reads local forward listeners with a TCP connect. That is not a
 * reading of SSH or of the PC, so the fallback words say listeners only, and a
 * state other than up or down is not measured.
 */
export function tunnelLine(tunnel) {
  if (!tunnel) return '';
  if (tunnel.detail) return tunnel.detail;
  const scope = 'This is local listener state, not SSH or PC reachability.';
  if (tunnel.state === 'up') return `Local forward listeners are accepting connections. ${scope}`;
  if (tunnel.state === 'down') return `Local forward listeners are not accepting connections. ${scope}`;
  return 'Local forward listener state was not measured.';
}

const HEALTH = new Set(['ok', 'degraded', 'down', 'not_measured']);
/** Only the four known readings become a class. Anything else is not measured. */
export function healthClass(health) {
  return HEALTH.has(health) ? health : 'not_measured';
}

/** Line kinds in key order. The class and the words travel together. */
export const EDGE_KEY = [
  {kind: 'hosts', className: 'edge-hosts', text: 'Solid line: a machine and an endpoint configured on it.'},
  {kind: 'upstream', className: 'edge-upstream', text: 'Dashed line: a router and an endpoint it lists as upstream.'},
  {kind: 'caller', className: 'edge-caller', text: 'Dotted line: an endpoint and a client reported as its caller.'},
  {kind: 'other', className: 'edge-other', text: 'Dash-dot line: a reported link of a kind this page does not recognise.'},
];
export function edgeKind(kind) {
  return EDGE_KEY.some(item => item.kind === kind && kind !== 'other') ? kind : 'other';
}

/** A map tab is a fact id. A machine tab holds the machine and its lanes; a lane tab holds that lane. */
export function inMapMonitor(tab, node) {
  return tab === 'all' || node.id === tab || node.machine === tab;
}

/** State changes between two drawn samples. No baseline means no transition. Ages and rates are not states. */
export function mapTransitions(previous, nodes) {
  if (!previous) return [];
  const changes = [], seen = new Set();
  for (const node of nodes || []) {
    seen.add(node.id);
    const word = statusWord(node), before = previous.get(node.id);
    if (!before) changes.push(`${node.label || node.id} added: ${word}`);
    else if (before.word !== word) changes.push(`${node.label || node.id}: ${before.word} to ${word}`);
  }
  for (const [id, before] of previous) if (!seen.has(id)) changes.push(`${before.label} is no longer on the map`);
  return changes;
}

/** One sentence per change, at most four, then a count. */
export function announcementText(changes, limit = 4) {
  if (!changes.length) return '';
  const rest = changes.length - limit;
  return changes.slice(0, limit).map(change => `${change}.`).join(' ')
    + (rest > 0 ? ` And ${rest} more change${rest === 1 ? '' : 's'}.` : '');
}

export function rateText(node) {
  if (Number.isFinite(node.generationTokensPerSecond)) return `${Math.round(node.generationTokensPerSecond * 10) / 10} tok/s`;
  if (Number.isFinite(node.promptTokensPerSecond)) return `${Math.round(node.promptTokensPerSecond * 10) / 10} prompt tok/s`;
  return '';
}

function mapFocus() {
  if (typeof window === 'undefined' || typeof window.__jaryoMapTab !== 'string') return 'all';
  return window.__jaryoMapTab || 'all';
}

function statusWord(node) {
  if (node.factState) return node.factState.toUpperCase();
  if (feedUnavailable) return 'DISCONNECTED';
  if (node.health === 'ok') return 'OK';
  if (node.health === 'degraded') return 'DEGRADED';
  if (node.health === 'down') return 'DOWN';
  if (node.reason === 'stale') return 'STALE';
  if (node.reason === 'not_configured') return 'NOT CONFIGURED';
  if (node.reason === 'feed_unavailable' || node.reason === 'tunnel_down') return 'DISCONNECTED';
  return 'UNKNOWN';
}

function draw(map, selected) {
  const svg = document.querySelector('#map');
  const placed = place(map.nodes);
  const registryView = map.nodes.some(node => node.role === 'machine' && (node.scope === 'machine' || node.observationSource === 'registry'));
  const requested = mapFocus();
  // A tab id that is no longer on the map filters nothing; an empty diagram would read as "no machines".
  const tab = placed.some(node => node.id === requested) ? requested : 'all';
  const width = Math.max(720, ...placed.map(node => node.x + (registryView ? 300 : 180)));
  const height = Math.max(420, ...placed.map(node => node.y + 48));
  // One user unit per CSS pixel: labels keep their size and the named region scrolls.
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.style.width = `${width}px`;
  svg.style.height = `${height}px`;
  const byId = new Map(placed.map(node => [node.id, node]));
  svg.replaceChildren();
  const kinds = new Set();
  for (const edge of map.edges || []) {
    const from = byId.get(edge.from);
    const to = byId.get(edge.to);
    if (!from || !to) continue;
    const line = document.createElementNS(NS, 'line');
    line.setAttribute('x1', from.x);
    line.setAttribute('y1', from.y);
    line.setAttribute('x2', to.x);
    line.setAttribute('y2', to.y);
    const kind = edgeKind(edge.kind);
    const outside = !inMapMonitor(tab, from) || !inMapMonitor(tab, to);
    line.setAttribute('class', `edge edge-${kind}${outside ? ' is-outside-monitor' : ''}`);
    line.dataset.from = from.id;
    line.dataset.to = to.id;
    line.dataset.kind = kind;
    if (!outside) kinds.add(kind);
    svg.append(line);
  }
  drawKey(kinds);
  for (const node of placed) {
    const group = document.createElementNS(NS, 'g');
    const dim = !inMapMonitor(tab, node);
    // The state class is on the group, where the .node.<state> circle rules read it; the mark has none.
    group.setAttribute('class', `node ${healthClass(node.health)}${selected === node.id ? ' selected' : ''}${dim ? ' is-outside-monitor' : ''}`);
    group.dataset.id = node.id;
    const dot = document.createElementNS(NS, 'circle');
    dot.setAttribute('cx', node.x);
    dot.setAttribute('cy', node.y);
    dot.setAttribute('r', selected === node.id ? 9 : 7);
    const title = document.createElementNS(NS, 'text');
    title.setAttribute('x', node.x + 14);
    title.setAttribute('y', node.y - 2);
    title.textContent = registryView ? caption(node.label || node.id) : node.label || node.id;
    title.setAttribute('title', node.label || node.id);
    const sub = document.createElementNS(NS, 'text');
    sub.setAttribute('x', node.x + 14);
    sub.setAttribute('y', node.y + 14);
    sub.setAttribute('class', 'sub');
    const observationAge = node.observationSource === 'registry' || node.scope
      ? (typeof node.ageSeconds === 'number' && Number.isFinite(node.ageSeconds)
        ? `${node.ageSeconds.toFixed(1)} s ago` : 'Not observed') : '';
    sub.textContent = registryView ? caption(node.modelId ? `${node.factState === 'stale' ? 'last reported' : 'reported'} ${displayModelId(node.modelId)}` : '')
      : [node.modelId, rateText(node), statusWord(node), observationAge].filter(Boolean).join(' · ');
    if (node.modelId) sub.setAttribute('title', node.modelId);
    const status = document.createElementNS(NS, 'text');
    status.setAttribute('x', node.x + 14);
    status.setAttribute('y', node.y + 29);
    status.setAttribute('class', 'sub');
    status.textContent = [rateText(node), statusWord(node), observationAge].filter(Boolean).join(' · ');
    const accessible = document.createElementNS(NS, 'title');
    accessible.textContent = [node.label || node.id, node.modelId, status.textContent, node.reason].filter(Boolean).join(' · ');
    group.append(dot, title, sub);
    if (registryView) group.append(status);
    group.append(accessible);
    svg.append(group);
  }
}

/** The text key names each line kind that is drawn, beside a sample with the same class. */
function drawKey(kinds) {
  const key = document.querySelector('#mapKey');
  if (!key) return;
  key.replaceChildren();
  for (const item of EDGE_KEY) {
    if (!kinds.has(item.kind)) continue;
    const row = document.createElement('li');
    const sample = document.createElementNS(NS, 'svg');
    sample.setAttribute('viewBox', '0 0 40 10');
    sample.setAttribute('aria-hidden', 'true');
    const line = document.createElementNS(NS, 'line');
    line.setAttribute('x1', 0);
    line.setAttribute('y1', 5);
    line.setAttribute('x2', 40);
    line.setAttribute('y2', 5);
    line.setAttribute('class', `edge ${item.className}`);
    sample.append(line);
    const words = document.createElement('span');
    words.textContent = item.text;
    row.append(sample, words);
    key.append(row);
  }
  key.hidden = kinds.size === 0;
}

function show(node) {
  const panel = document.querySelector('#detail');
  panel.hidden = false;
  document.querySelector('#detailTitle').textContent = node.label || node.id;
  const facts = document.querySelector('#detailFacts');
  facts.replaceChildren();
  for (const [name, value] of detailRows(node)) {
    const term = document.createElement('dt');
    term.textContent = name;
    const data = document.createElement('dd');
    data.textContent = value;
    if (/model$/i.test(name) && node.modelId) data.setAttribute('title', node.modelId);
    facts.append(term, data);
  }
}

let current = null;
let selected = null;
let feedUnavailable = false;
let paused = false;
let held = null;
let muted = false;
let baseline = null;

export function fleetMap(map, now = Date.now() / 1000) {
  if (!map) return map;
  const ids = new Set();
  const expired = new Set();
  const nodes = fleetFacts(map, {now}).map(fact => {
    const evidence = fact.evidence || {};
    const fresh = fact.state !== 'stale' && fact.state !== 'unknown';
    const node = {...evidence, ...fact, factState: fact.state,
      role: fact.scope === 'machine' ? 'machine' : evidence.role || 'llama',
      machine: fact.scope === 'machine' ? fact.id : fact.parentId,
      health: fact.state === 'endpoint answering' ? 'ok' : fact.state === 'endpoint unavailable' ? 'down' : 'not_measured',
      modelId: fact.modelId || evidence.modelId || null,
      endpoint: evidence.configuredEndpoint || evidence.endpoint || null,
      observationSource: fact.source};
    for (const key of ['promptTokensPerSecond', 'generationTokensPerSecond', 'latencyMs', 'queueDepth',
      'slotsBusy', 'slotsTotal', 'contextUsed', 'contextLimit', 'vramUsedMiB', 'vramTotalMiB']) {
      if (!fresh || String(fact.source).startsWith('registry')) node[key] = key === 'latencyMs' && fresh ? evidence[key] ?? null : null;
    }
    ids.add(node.id);
    if (!fresh) expired.add(node.id);
    return node;
  });
  const targets = new Map();
  const edges = (map.edges || []).filter(edge => {
    if (!ids.has(edge.from) || !ids.has(edge.to)
        || (['upstream', 'caller'].includes(edge.kind) && (expired.has(edge.from) || expired.has(edge.to)))) return false;
    if (!targets.has(edge.from)) targets.set(edge.from, new Set());
    targets.get(edge.from).add(edge.to);
    return true;
  });
  for (const node of nodes) {
    if (node.role !== 'machine' && ids.has(node.parentId)
        && !targets.get(node.parentId)?.has(node.id))
      edges.push({from: node.parentId, to: node.id, kind: 'hosts'});
  }
  return {...map, nodes, edges};
}

function ageMap(map, unavailable = false) {
  if (!map) return map;
  const age = Date.now() - Date.parse(map.observedAt);
  const tunnel = map.tunnel && (unavailable || !Number.isFinite(age) || age < -2000 || age > 30000)
    ? {state: 'not_measured', detail: 'Local forward listener observation is stale. Listener state not measured.'} : map.tunnel;
  return fleetMap({...map, tunnel});
}

function render() {
  document.querySelector('#tunnel').textContent = feedUnavailable ? 'Monitor feed unavailable' : tunnelLine(current?.tunnel);
  publishMap();
  if (!current) return;
  draw(current, selected);
  const node = (current.nodes || []).find(item => item.id === selected);
  if (node) show(node);
  else {
    selected = null;
    document.querySelector('#detail').hidden = true;
    document.querySelector('#detailFacts').replaceChildren();
  }
}

/** Rendering follows the shared store, including its independent ageing ticks. */
function followStore(state) {
  // A paused display keeps the captured drawing. The newest state waits for Resume.
  if (paused) { held = state; return; }
  feedUnavailable = Boolean(state.inferenceStatus?.error) || !state.inference;
  current = ageMap(state.inference, feedUnavailable);
  render();
  announce();
}

/** Batch the state changes of one update into one polite message. A missing map is not a set of departures. */
function announce() {
  if (!current) return;
  const changes = mapTransitions(baseline, current.nodes);
  baseline = new Map((current.nodes || []).map(node => [node.id, {label: node.label || node.id, word: statusWord(node)}]));
  const region = document.querySelector('#mapAnnounce');
  if (!region || muted || !changes.length) return;
  region.textContent = announcementText(changes);
}

function setPaused(next) {
  paused = next;
  const button = document.querySelector('#mapPause'), captured = document.querySelector('#mapCaptured');
  button?.setAttribute('aria-pressed', String(paused));
  if (captured) {
    const at = new Date(Date.now());
    captured.hidden = !paused;
    captured.textContent = paused
      ? `Display updates paused. This is the map as drawn at ${at.toLocaleTimeString()}. Newer observations are not shown until you resume.` : '';
    if (paused) captured.setAttribute('data-captured-at', at.toISOString());
    else captured.removeAttribute('data-captured-at');
  }
  if (!paused && held) {
    const state = held;
    held = null;
    followStore(state);
  }
}

function setMuted(next) {
  muted = next;
  document.querySelector('#mapMute')?.setAttribute('aria-pressed', String(muted));
  const region = document.querySelector('#mapAnnounce');
  if (region) region.textContent = muted
    ? 'Announcements muted. State changes are still drawn.' : 'Announcements on. State changes will be announced.';
}

function publishMap() {
  const detail = {map: current, feedUnavailable};
  if (typeof window === 'undefined') return;
  window.__jaryoLastMap = current;
  window.__jaryoFeedUnavailable = feedUnavailable;
  window.__jaryoMapDetail = detail;
  if (typeof window.dispatchEvent !== 'function') return;
  try {
    window.dispatchEvent(new CustomEvent('agiw-inference-map', {detail}));
  } catch (_) { /* view hook only */ }
}

if (typeof document !== 'undefined' && document.querySelector('#map')) {
  document.querySelector('#detailClose')?.addEventListener('click', () => {
    selected = null;
    document.querySelector('#detail').hidden = true;
    if (current) draw(current, null);
    document.querySelector('#content')?.focus();
  });
  document.querySelector('#map').addEventListener('click', event => {
    const group = event.target.closest('g');
    const id = group?.dataset.id;
    if (!id || !current) return;
    selected = id;
    draw(current, selected);
    const node = current.nodes.find(item => item.id === id);
    if (node) show(node);
  });
  document.querySelector('#mapPause')?.addEventListener('click', () => setPaused(!paused));
  document.querySelector('#mapMute')?.addEventListener('click', () => setMuted(!muted));
  if (typeof window !== 'undefined') window.__jaryoRerenderMap = () => render();
  const store = getMonitorStore();
  store.subscribe(followStore);
  store.start({inference: true});
}
