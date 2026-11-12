/**
 * Machines: one tile per machine row from /api/snapshot, this machine first, under the peer map.
 * Read only. Every value is the row's own; a missing one is "Not measured" or "Unknown", never 0.
 */
import {own, peerMap, reasonWords, rowDegraded, registryMachineView, registryModelName} from '/monitor-board.mjs';

const NAMES = {mac: 'Mac', macos: 'Mac', windows: 'PC', linux: 'Linux'};
const STATE_WORDS = {up: 'Up', down: 'Down', unknown: 'Unknown'};
const GLYPHS = {up: '■', down: '×', unknown: '?'};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const safeWord = (value, max = 40) => (typeof value === 'string' && /^[A-Za-z0-9 _.:-]+$/.test(value) ? value.slice(0, max) : null);
const sentence = words => words.charAt(0).toUpperCase() + words.slice(1);
const number = value => typeof value === 'number' && Number.isFinite(value);

function rate(value) {
  return number(value) && value >= 0 ? `${value < 10 ? value.toFixed(1) : Math.round(value)} tok/s` : 'Not measured';
}

/** The facts for one machine row, as [label, text] pairs, in reading order. */
export function machineFacts(row, options) {
  if (row?.observationSource === 'registry') {
    const view = registryMachineView(row, options);
    return [['Lane health', view.word], ['Lanes', view.detail], ['Seen through', 'Registry'],
      ['Inventory', 'Configured lanes only; whole-machine inventory unknown']];
  }
  const state = own(STATE_WORDS, row?.state) || 'Unknown';
  const degraded = rowDegraded(row);
  const facts = [
    ['State', degraded ? 'Up, degraded' : state],
    ['Why', degraded ? 'Reported degraded' : row?.state === 'up' ? 'Observed' : sentence(reasonWords(row?.reasonCode))],
    ['Seen through', safeWord(row?.trust, 20) || 'Unknown'],
    ['Age', number(row?.ageSeconds) ? `${Math.round(row.ageSeconds)} s` : 'Unknown'],
    ['Fresh for', number(row?.freshForSeconds) ? `${Math.round(row.freshForSeconds)} s` : 'Unknown'],
  ];
  if (row?.modelsComplete === true && Number.isInteger(row?.loadedCount)) {
    facts.push(['Models loaded', String(row.loadedCount)]);
    facts.push(['Models busy', Number.isInteger(row?.busyCount) ? String(row.busyCount) : 'Unknown']);
  } else {
    facts.push(['Models', 'Model list incomplete']);
  }
  facts.push(['Reading prompts', rate(row?.promptTokensPerSecond)]);
  facts.push(['Writing', rate(row?.generationTokensPerSecond)]);
  facts.push(['Hands off', row?.handsOff === true ? 'Yes' : row?.handsOff === false ? 'No' : 'Unknown']);
  return facts;
}

/** The host line of a card. A paired machine's own row is named by its pairing UUID, which says nothing to a reader: show its label
 *  when it has one, else the host it reported, marked "(reported)" because a peer chose that word, else the identifier. */
function hostLine(row) {
  const host = safeWord(row?.host, 40);
  if (!host || !UUID.test(host)) return host;
  // The server's own label for a renamed peer ends in "(reported)"; parentheses are allowed here and nowhere else.
  const label = typeof row?.label === 'string' && /^[A-Za-z0-9 _.:()-]+$/.test(row.label) ? row.label.slice(0, 48) : null;
  if (label && !UUID.test(label)) return label;
  const reported = safeWord(row?.reportedHost, 40);
  if (reported && !UUID.test(reported)) return `${reported} (reported)`;
  return host;
}

/** Tiles in display order: this machine first, then the rest in the sample's order. */
export function machineTiles(machines, self, {fresh = true, now} = {}) {
  const rows = Array.isArray(machines) ? machines.filter(row => row && typeof row === 'object' && !Array.isArray(row)) : [];
  const hub = rows.findIndex(row => row.host === self || row.platform === self);
  const ordered = hub > 0 ? [rows[hub], ...rows.filter((_, i) => i !== hub)] : rows;
  return ordered.map((row, i) => {
    const registry = row.observationSource === 'registry' ? registryMachineView(row, {fresh, now}) : null;
    const name = own(NAMES, row.platform) || safeWord(row.host, 24) || 'Machine';
    const state = own(STATE_WORDS, row.state) ? row.state : 'unknown';
    const degraded = rowDegraded(row);
    return {
      key: safeWord(row.host, 40) || `row-${i}`,
      title: registry ? registry.label : i === 0 && hub >= 0 ? `This ${name}` : name,
      host: hostLine(row),
      glyph: registry ? registry.glyph : degraded ? '◐' : GLYPHS[state],
      word: registry ? registry.word : degraded ? 'DEGRADED' : STATE_WORDS[state].toUpperCase(),
      facts: machineFacts(row, {fresh, now}),
      lanes: registry ? registry.lanes : null,
    };
  });
}

/** Lane facts stay words, including missing health and observation time. Model paths retain a full title. */
export function laneFacts(lane) {
  const model = registryModelName(lane?.modelId);
  const health = lane?.health === 'ok' ? 'UP' : lane?.health === 'degraded' ? 'DEGRADED' : lane?.health === 'down' ? 'DOWN' : 'UNKNOWN';
  const stamp = typeof lane?.observedAt === 'string' && Number.isFinite(Date.parse(lane.observedAt)) ? lane.observedAt : null;
  const seen = number(lane?.ageSeconds) && lane.ageSeconds >= 0 ? `${Math.round(lane.ageSeconds)} s ago` : stamp || 'Unknown';
  const code = lane?.reason;
  // Framing failures can include a digit (invalid_utf8); retain those public codes as words too.
  const reason = typeof code === 'string' && /^[a-z][a-z0-9_]{0,79}$/.test(code) && !/^[a-z_]{1,40}$/.test(code)
    ? code.replaceAll('_', ' ') : reasonWords(code);
  return {label: typeof lane?.label === 'string' ? lane.label : 'Lane', model, health,
    seen, observedAt: typeof lane?.observedAt === 'string' ? lane.observedAt : null,
    reason: health === 'UP' ? null : sentence(reason)};
}

function appendLanes(document, card, lanes) {
  const list = el(document, 'ul', 'j-machine-lanes');
  list.setAttribute('aria-label', 'Lanes');
  for (const lane of lanes) {
    const facts = laneFacts(lane);
    const item = el(document, 'li', 'j-machine-lane');
    const model = el(document, 'span', 'j-lane-model', facts.model.label);
    if (facts.model.title) model.setAttribute('title', facts.model.title);
    const seen = el(document, 'span', 'j-lane-seen', `Last seen: ${facts.seen}`);
    if (facts.observedAt) seen.setAttribute('title', facts.observedAt);
    item.append(el(document, 'strong', 'j-lane-label', facts.label), model,
      el(document, 'span', 'j-lane-health', facts.health), seen);
    if (facts.reason) item.append(el(document, 'span', 'j-lane-reason', `Reason: ${facts.reason}`));
    list.append(item);
  }
  card.append(list);
}

function el(document, tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Paints the page from one snapshot (or null when it could not be read). */
export function paintMachines(document, snapshot, {fresh}) {
  const root = document.getElementById('machinesBody');
  if (!root) return;
  const status = document.getElementById('machinesStatus');
  if (status) status.textContent = !snapshot ? 'Snapshot unavailable. Machines are unknown.' : fresh ? 'Live sample.' : 'Last sample, not live.';
  const map = el(document, 'pre', 'j-schematic j-peer-map');
  const view = peerMap({machines: snapshot?.machines, self: snapshot?.host, feedFresh: fresh});
  map.setAttribute('role', 'img');
  map.setAttribute('aria-label', view.label);
  map.textContent = view.text;
  const grid = el(document, 'div', 'j-machine-grid');
  const tiles = machineTiles(snapshot?.machines, snapshot?.host, {fresh});
  if (tiles.some(tile => tile.lanes !== null) && Array.isArray(snapshot?.inference?.nodes)) {
    const local = tiles.find(tile => tile.title.startsWith('This '));
    if (local) {
      const machine = snapshot.host === 'windows' ? 'pc' : 'mac';
      local.lanes = snapshot.inference.nodes.filter(node => node && node.machine === machine && node.role !== 'machine' && node.role !== 'client')
        .map(node => ({...node, health: fresh ? node.health : 'unknown', reason: fresh ? node.reason : 'stale'}));
    }
  }
  if (!tiles.length) {
    const empty = el(document, 'p', 'j-empty', snapshot
      ? 'No machine rows in this sample. A missing read stays unknown.'
      : 'No machine rows. The snapshot could not be read, so every machine stays unknown.');
    empty.setAttribute('role', 'status');
    grid.append(empty);
  }
  for (const tile of tiles) {
    const card = el(document, 'section', 'j-machine');
    card.setAttribute('aria-label', `${tile.title}: ${tile.word.toLowerCase()}`);
    const head = el(document, 'h2', 'j-machine-title', tile.title);
    const state = el(document, 'p', `j-status${tile.word === 'UP' ? '' : tile.word === 'DOWN' ? ' j-blocked' : ' j-unknown'}`);
    state.append(el(document, 'span', 'j-glyph', tile.glyph), el(document, 'span', 'j-word', tile.word));
    const list = el(document, 'dl', 'j-machine-facts');
    for (const [label, text] of tile.facts) list.append(el(document, 'dt', null, label), el(document, 'dd', null, text));
    card.append(head);
    if (tile.host) card.append(el(document, 'p', 'j-machine-host', tile.host));
    card.append(state, list);
    if (tile.lanes !== null) appendLanes(document, card, tile.lanes);
    grid.append(card);
  }
  root.replaceChildren(map, grid);
}

/** Polls the snapshot once a second while the page is open. */
export function mountMachines({document, fetch, setTimeout, now = () => Date.now() / 1000}) {
  let stopped = false;
  let timer = null;
  const tick = async () => {
    let snapshot = null;
    try {
      const reply = await fetch('/api/snapshot', {cache: 'no-store'});
      if (reply.ok) {
        const body = await reply.json();
        snapshot = body && typeof body === 'object' && !Array.isArray(body) ? body : null;
      }
    } catch (_) {
      snapshot = null;
    }
    if (stopped) return;
    const stamp = snapshot?.fullSampledAt ?? snapshot?.sampledAt;
    const age = typeof stamp === 'number' ? now() - stamp : NaN;
    paintMachines(document, snapshot, {fresh: number(age) && age >= -1 && age <= 3});
    timer = setTimeout(tick, 1000);
  };
  tick();
  return {destroy() { stopped = true; if (timer !== null) clearTimeout(timer); }};
}

if (typeof document !== 'undefined' && document.getElementById?.('machinesBody')) {
  mountMachines({document, fetch: (...args) => fetch(...args), setTimeout: (...args) => setTimeout(...args)});
}
