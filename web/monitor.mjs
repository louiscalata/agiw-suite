const NS = 'http://www.w3.org/2000/svg';
const LANES = {
  mac: {x: 120, y: 48},
  'mac:lmstudio': {x: 120, y: 160},
  'mac:veritas': {x: 120, y: 272},
  pc: {x: 460, y: 48},
  'pc:router': {x: 460, y: 160},
  'pc:gpt-oss-20b': {x: 360, y: 300},
  'pc:qwen3.8-27b': {x: 560, y: 300},
};

export function place(nodes) {
  if ((nodes || []).some(node => node.role === 'machine' && node.observationSource === 'registry')) {
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

function number(value, suffix) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value}${suffix}` : '—';
}

export function detailRows(node) {
  const slots = node.slotsBusy == null && node.slotsTotal == null
    ? '—' : `${node.slotsBusy ?? '—'}/${node.slotsTotal ?? '—'}`;
  const context = node.contextUsed == null && node.contextLimit == null
    ? '—' : `${node.contextUsed ?? '—'}/${node.contextLimit ?? '—'}`;
  const vram = node.vramUsedMiB == null && node.vramTotalMiB == null
    ? '—' : `${node.vramUsedMiB ?? '—'}/${node.vramTotalMiB ?? '—'} MiB`;
  const rows = [
    ['Health', node.health || '—'],
    ['Model', (node.observationSource === 'registry' ? displayModelId(node.modelId) : node.modelId) || '—'],
    ['Upstream', node.upstream || '—'],
    ['Endpoint', node.endpoint || '—'],
    ['Prompt tok/s', number(node.promptTokensPerSecond, '')],
    ['Generation tok/s', number(node.generationTokensPerSecond, '')],
    ['Latency', number(node.latencyMs, ' ms')],
    ['Queue', node.queueDepth == null ? '—' : String(node.queueDepth)],
    ['Slots', slots],
    ['Context', context],
    ['VRAM', vram],
  ];
  if (node.observationSource === 'registry') {
    rows.push(['Observation age', typeof node.ageSeconds === 'number' && Number.isFinite(node.ageSeconds)
      ? `${node.ageSeconds.toFixed(1)} s` : '—']);
  }
  if (node.detail) rows.push(['Detail', node.detail]);
  if (node.reason) rows.push(['Reason', node.reason]);
  return rows;
}

export function tunnelLine(tunnel) {
  if (!tunnel) return '';
  return tunnel.detail || (tunnel.state === 'up' ? 'PC tunnel up' : 'PC tunnel down');
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
  const registryView = map.nodes.some(node => node.role === 'machine' && node.observationSource === 'registry');
  const tab = mapFocus();
  const width = Math.max(720, ...placed.map(node => node.x + (registryView ? 300 : 180)));
  const height = Math.max(420, ...placed.map(node => node.y + 48));
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.style.width = '100%';
  svg.style.height = 'auto';
  const byId = new Map(placed.map(node => [node.id, node]));
  svg.replaceChildren();
  for (const edge of map.edges || []) {
    const from = byId.get(edge.from);
    const to = byId.get(edge.to);
    if (!from || !to) continue;
    const line = document.createElementNS(NS, 'line');
    line.setAttribute('x1', from.x);
    line.setAttribute('y1', from.y);
    line.setAttribute('x2', to.x);
    line.setAttribute('y2', to.y);
    line.setAttribute('class', 'edge');
    svg.append(line);
  }
  for (const node of placed) {
    const group = document.createElementNS(NS, 'g');
    const dim = tab !== 'all' && tab !== node.id && tab !== `machine:${node.machine}`;
    group.setAttribute('class', `node${selected === node.id ? ' selected' : ''}${dim ? ' is-outside-monitor' : ''}`);
    group.dataset.id = node.id;
    const dot = document.createElementNS(NS, 'circle');
    dot.setAttribute('cx', node.x);
    dot.setAttribute('cy', node.y);
    dot.setAttribute('r', selected === node.id ? 9 : 7);
    dot.setAttribute('class', node.health || 'not_measured');
    const title = document.createElementNS(NS, 'text');
    title.setAttribute('x', node.x + 14);
    title.setAttribute('y', node.y - 2);
    title.textContent = registryView ? caption(node.label || node.id) : node.label || node.id;
    title.setAttribute('title', node.label || node.id);
    const sub = document.createElementNS(NS, 'text');
    sub.setAttribute('x', node.x + 14);
    sub.setAttribute('y', node.y + 14);
    sub.setAttribute('class', 'sub');
    const observationAge = node.observationSource === 'registry'
      ? (typeof node.ageSeconds === 'number' && Number.isFinite(node.ageSeconds)
        ? `${node.ageSeconds.toFixed(1)} s ago` : 'Not observed') : '';
    sub.textContent = registryView ? caption(displayModelId(node.modelId))
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
    if (name === 'Model' && node.modelId) data.setAttribute('title', node.modelId);
    facts.append(term, data);
  }
}

let current = null;
let selected = null;
let inFlight = false;
let feedUnavailable = false;

function ageMap(map, unavailable = false) {
  if (!map) return map;
  const age = Date.now() - Date.parse(map.observedAt);
  const tunnel = map.tunnel && (unavailable || !Number.isFinite(age) || age < -2000 || age > 30000)
    ? {state: 'not_measured', detail: 'PC tunnel observation stale.'} : map.tunnel;
  const expired = new Set();
  const nodes = (map.nodes || []).map(node => {
    const registry = node.observationSource === 'registry';
    // A registry observation has its own completion timestamp. A new snapshot
    // must never renew it while a probe or sampler is stalled.
    const age = Date.now() - Date.parse(registry ? node.observedAt : (node.observedAt || map.observedAt));
    const interval = typeof node.probeIntervalSeconds === 'number' && Number.isFinite(node.probeIntervalSeconds)
      && node.probeIntervalSeconds > 0 ? node.probeIntervalSeconds : 3;
    const maxAge = registry ? 2 * interval * 1000 : (node.machine === 'pc' ? 30000 : 3000);
    if (registry) node = {...node, ageSeconds: Number.isFinite(age) ? Math.max(0, age / 1000) : null};
    if (!unavailable && Number.isFinite(age) && age >= -2000 && age <= maxAge) return node;
    if (registry && !unavailable && node.observedAt == null && node.health === 'not_measured') return node;
    expired.add(node.id);
    const unknown = {...node, health: 'not_measured', reason: unavailable ? 'feed_unavailable' : 'stale'};
    for (const field of ['modelId', 'upstream', 'detail', 'models', 'callers', 'promptTokensPerSecond',
      'generationTokensPerSecond', 'latencyMs', 'queueDepth', 'slotsBusy', 'slotsTotal',
      'contextUsed', 'contextLimit', 'vramUsedMiB', 'vramTotalMiB']) unknown[field] = null;
    return unknown;
  });
  const edges = (map.edges || []).filter(edge => !['upstream', 'caller'].includes(edge.kind)
    || (!expired.has(edge.from) && !expired.has(edge.to)));
  return {...map, tunnel, nodes, edges};
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

async function tick() {
  current = ageMap(current);
  render();
  if (inFlight) return;
  inFlight = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 2000);
  try {
    const response = await fetch('/api/inference', {headers: {Accept: 'application/json'}, cache: 'no-store', signal: controller.signal});
    if (!response.ok) throw new Error(String(response.status));
    const map = await response.json();
    if (!map || !Array.isArray(map.nodes)) throw new Error('Invalid map');
    current = ageMap(map);
    feedUnavailable = false;
  } catch (_) {
    current = ageMap(current, true);
    feedUnavailable = true;
  } finally {
    clearTimeout(timeout);
    inFlight = false;
    render();
  }
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
  document.querySelector('#map').addEventListener('click', event => {
    const group = event.target.closest('g');
    const id = group?.dataset.id;
    if (!id || !current) return;
    selected = id;
    draw(current, selected);
    const node = current.nodes.find(item => item.id === id);
    if (node) show(node);
  });
  if (typeof window !== 'undefined') window.__jaryoRerenderMap = () => render();
  tick();
  setInterval(tick, 2000);
}
