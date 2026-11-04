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
  const placed = [];
  const extra = [];
  for (const node of nodes || []) {
    if (LANES[node.id]) placed.push({...node, ...LANES[node.id]});
    else extra.push(node);
  }
  extra.forEach((node, index) => placed.push({...node, x: 40 + (index % 6) * 120, y: 390 + Math.floor(index / 6) * 60}));
  return placed;
}

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
    ['Model', node.modelId || '—'],
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

function draw(map, selected) {
  const svg = document.querySelector('#map');
  const placed = place(map.nodes);
  svg.style.height = `${Math.max(420, ...placed.map(node => node.y + 40))}px`;
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
    group.setAttribute('class', 'node');
    group.dataset.id = node.id;
    const dot = document.createElementNS(NS, 'circle');
    dot.setAttribute('cx', node.x);
    dot.setAttribute('cy', node.y);
    dot.setAttribute('r', selected === node.id ? 9 : 7);
    dot.setAttribute('class', node.health || 'not_measured');
    const title = document.createElementNS(NS, 'text');
    title.setAttribute('x', node.x + 14);
    title.setAttribute('y', node.y - 2);
    title.textContent = node.label || node.id;
    const sub = document.createElementNS(NS, 'text');
    sub.setAttribute('x', node.x + 14);
    sub.setAttribute('y', node.y + 14);
    sub.setAttribute('class', 'sub');
    sub.textContent = [node.modelId, rateText(node)].filter(Boolean).join(' · ');
    group.append(dot, title, sub);
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
    facts.append(term, data);
  }
}

let current = null;
let selected = null;
let inFlight = false;

function ageMap(map, unavailable = false) {
  if (!map) return map;
  const age = Date.now() - Date.parse(map.observedAt);
  const tunnel = map.tunnel && (unavailable || !Number.isFinite(age) || age < -2000 || age > 30000)
    ? {state: 'not_measured', detail: 'PC tunnel observation stale.'} : map.tunnel;
  const nodes = (map.nodes || []).map(node => {
    const age = Date.now() - Date.parse(node.observedAt || map.observedAt);
    if (!unavailable && Number.isFinite(age) && age >= -2000 && age <= (node.machine === 'pc' ? 30000 : 3000)) return node;
    const unknown = {...node, health: 'not_measured', reason: unavailable ? 'feed_unavailable' : 'stale'};
    for (const field of ['modelId', 'upstream', 'detail', 'models', 'callers', 'promptTokensPerSecond',
      'generationTokensPerSecond', 'latencyMs', 'queueDepth', 'slotsBusy', 'slotsTotal',
      'contextUsed', 'contextLimit', 'vramUsedMiB', 'vramTotalMiB']) unknown[field] = null;
    return unknown;
  });
  const unknown = new Set(nodes.filter(node => node.health === 'not_measured').map(node => node.id));
  const edges = (map.edges || []).filter(edge => !['upstream', 'caller'].includes(edge.kind)
    || (!unknown.has(edge.from) && !unknown.has(edge.to)));
  return {...map, tunnel, nodes, edges};
}

function render() {
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
    document.querySelector('#tunnel').textContent = tunnelLine(current.tunnel);
  } catch (_) {
    current = ageMap(current, true);
    document.querySelector('#tunnel').textContent = 'Monitor feed unavailable';
  } finally {
    clearTimeout(timeout);
    inFlight = false;
    render();
  }
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
  tick();
  setInterval(tick, 2000);
}
