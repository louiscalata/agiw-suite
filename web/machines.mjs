/**
 * Machines: one tile per machine row from /api/snapshot, this machine first, under the peer map.
 * Read only. Every value is the row's own; a missing one is "Not measured" or "Unknown", never 0.
 */
import {peerMap, reasonWords} from '/monitor-board.mjs';

const NAMES = {mac: 'Mac', windows: 'PC', linux: 'Linux'};
const STATE_WORDS = {up: 'Up', down: 'Down', unknown: 'Unknown'};
const GLYPHS = {up: '■', down: '×', unknown: '?'};

const safeWord = (value, max = 40) => (typeof value === 'string' && /^[A-Za-z0-9 _.:-]+$/.test(value) ? value.slice(0, max) : null);
const sentence = words => words.charAt(0).toUpperCase() + words.slice(1);
const number = value => typeof value === 'number' && Number.isFinite(value);

function rate(value) {
  return number(value) && value >= 0 ? `${value < 10 ? value.toFixed(1) : Math.round(value)} tok/s` : 'Not measured';
}

/** The facts for one machine row, as [label, text] pairs, in reading order. */
export function machineFacts(row) {
  const state = STATE_WORDS[row?.state] || 'Unknown';
  const degraded = row?.state === 'up' && row?.reasonCode === 'reported_degraded';
  const facts = [
    ['State', degraded ? 'Up, degraded' : state],
    ['Why', row?.state === 'up' && !degraded ? 'Observed' : sentence(reasonWords(row?.reasonCode))],
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

/** Tiles in display order: this machine first, then the rest in the sample's order. */
export function machineTiles(machines, self) {
  const rows = Array.isArray(machines) ? machines.filter(row => row && typeof row === 'object' && !Array.isArray(row)) : [];
  const hub = rows.findIndex(row => row.host === self || row.platform === self);
  const ordered = hub > 0 ? [rows[hub], ...rows.filter((_, i) => i !== hub)] : rows;
  return ordered.map((row, i) => {
    const name = NAMES[row.platform] || safeWord(row.host, 24) || 'Machine';
    const state = STATE_WORDS[row.state] ? row.state : 'unknown';
    return {
      key: safeWord(row.host, 40) || `row-${i}`,
      title: i === 0 && hub >= 0 ? `This ${name}` : name,
      host: safeWord(row.host, 40),
      glyph: GLYPHS[state],
      word: STATE_WORDS[state].toUpperCase(),
      facts: machineFacts(row),
    };
  });
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
  for (const tile of machineTiles(snapshot?.machines, snapshot?.host)) {
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
    const age = now() - snapshot?.sampledAt;
    paintMachines(document, snapshot, {fresh: number(age) && age >= -2 && age <= 5});
    timer = setTimeout(tick, 1000);
  };
  tick();
  return {destroy() { stopped = true; if (timer !== null) clearTimeout(timer); }};
}

if (typeof document !== 'undefined' && document.getElementById?.('machinesBody')) {
  mountMachines({document, fetch: (...args) => fetch(...args), setTimeout: (...args) => setTimeout(...args)});
}
