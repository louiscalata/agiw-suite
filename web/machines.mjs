/**
 * Machines: one tile per canonical fleet machine fact, with its own lane evidence.
 * Read only. Every value is the row's own; a missing one is "Not measured" or "Unknown", never 0.
 */
import {own, reasonWords, rowDegraded, registryMachineView, registryModelName} from '/monitor-board.mjs';
import {fleetState, fleetMachines, fleetLanes, fleetSummary, fleetWord, fleetAge, fleetMachineCoverage, fleetHostUp} from '/fleet-state.mjs';
import {getMonitorStore} from '/monitor-store.mjs';

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
  if (fleetState(machines, {now})) {
    const lanes = fleetLanes(machines, {now});
    return fleetMachines(machines, {now}).map(fact => {
      const children = lanes.filter(lane => lane.parentId === fact.id);
      const coverage = fleetSummary({schemaVersion: 1, facts: children}, {now});
      const laneCoverage = fleetMachineCoverage(fact, children);
      const unavailable = children.length > 0 && laneCoverage.counts.unavailable === children.length &&
        !fleetHostUp(fact);
      const evidence = fact.evidence, current = fleetHostUp(fact) && fact.completeness === 'complete' && evidence.modelsComplete === true;
      const facts = [['State', fleetWord(fact)], ['Source', fact.source || 'Unknown'],
        ['Observed', fact.observedAt || 'Unknown'], ['Age', fleetAge(fact)], ['Expires', fact.expiresAt || 'Unknown'],
        ['Completeness', fact.completeness || 'unknown'], ['Reason', fact.reason || 'Not reported'],
        ['Endpoints', coverage.text], ['Host / hardware health', 'Unknown; endpoint responses do not measure hardware']];
      for (const [label, field] of [['Models loaded', 'loadedCount'], ['Models busy', 'busyCount']])
        facts.push([label, current && Number.isSafeInteger(evidence[field]) && evidence[field] >= 0 ? String(evidence[field]) : 'Unknown']);
      const reporting = children.filter(lane => laneRuntime(lane).known).length;
      facts.push(['Runtime readings', children.length ? `${reporting} of ${children.length} ${children.length === 1 ? 'lane reports' : 'lanes report'} runtime readings` : 'No lanes configured']);
      return {key: fact.id, title: fact.label || fact.id, host: fact.id,
        glyph: unavailable ? laneCoverage.glyph : fleetHostUp(fact) && coverage.counts.answering > 0 ? '■' : '?',
        word: unavailable ? laneCoverage.text : fleetWord(fact), facts, lanes: children};
    });
  }
  if (!Array.isArray(machines)) return [];
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
  const canonical = lane?.scope === 'lane';
  const evidence = canonical ? lane.evidence || {} : lane;
  const model = registryModelName(evidence?.modelId);
  const health = canonical ? fleetWord(lane) : lane?.health === 'ok' ? 'UP' : lane?.health === 'degraded' ? 'DEGRADED' : lane?.health === 'down' ? 'DOWN' : 'UNKNOWN';
  const stamp = typeof lane?.observedAt === 'string' && Number.isFinite(Date.parse(lane.observedAt)) ? lane.observedAt : null;
  const seen = number(lane?.ageSeconds) && lane.ageSeconds >= 0 ? `${Math.round(lane.ageSeconds)} s ago` : stamp || 'Unknown';
  const code = lane?.reason;
  // Framing failures can include a digit (invalid_utf8); retain those public codes as words too.
  const reason = typeof code === 'string' && /^[a-z][a-z0-9_]{0,79}$/.test(code) && !/^[a-z_]{1,40}$/.test(code)
    ? code.replaceAll('_', ' ') : reasonWords(code);
  return {label: typeof lane?.label === 'string' ? lane.label : 'Lane', model, health,
    seen, observedAt: typeof lane?.observedAt === 'string' ? lane.observedAt : null,
    reason: canonical ? lane.reason || 'Not reported' : health === 'UP' ? null : sentence(reason),
    id: canonical ? lane.id : null, configured: evidence?.configuredEndpoint || evidence?.endpoint || 'Unknown',
    context: Number.isSafeInteger(evidence?.contextTokens) ? String(evidence.contextTokens) + ' tokens' : 'Unknown',
    thinking: ['on', 'off', 'unknown'].includes(evidence?.thinking) ? evidence.thinking : 'Unknown',
    timeout: number(evidence?.probeTimeoutSeconds) ? String(evidence.probeTimeoutSeconds) + ' s' : 'Unknown',
    probeState: evidence?.probeState || 'Unknown',
    lastSuccess: evidence?.lastSuccessAt || 'Unknown', lastSuccessModel: evidence?.lastSuccessModelId || null,
    runtime: canonical ? laneRuntime(lane).text : null};
}

const whole = value => Number.isSafeInteger(value) && value >= 0;
/** Runtime readings (/metrics, /slots, /props) of one canonical lane: shown only while its endpoint is answering,
 *  and only values that are finite, non-negative and consistent. The registry probe reads /v1/models identity only. */
export function laneRuntime(lane) {
  if (lane?.state === 'stale') return {known: false, text: 'Not measured · lane evidence stale'};
  if (lane?.state === 'endpoint unavailable') return {known: false, text: 'Not measured · endpoint not answering'};
  if (lane?.state !== 'endpoint answering') return {known: false, text: 'Not measured · lane identity unknown'};
  const e = lane.evidence || {}, parts = [];
  if (number(e.promptTokensPerSecond) && e.promptTokensPerSecond >= 0) parts.push(`prompt ${rate(e.promptTokensPerSecond)}`);
  if (number(e.generationTokensPerSecond) && e.generationTokensPerSecond >= 0) parts.push(`writing ${rate(e.generationTokensPerSecond)}`);
  if (whole(e.slotsBusy) && whole(e.slotsTotal) && e.slotsTotal > 0 && e.slotsBusy <= e.slotsTotal) parts.push(`slots ${e.slotsBusy} of ${e.slotsTotal} busy`);
  if (whole(e.contextUsed) && whole(e.contextLimit) && e.contextLimit > 0 && e.contextUsed <= e.contextLimit) parts.push(`context ${e.contextUsed} of ${e.contextLimit} tokens`);
  if (whole(e.queueDepth)) parts.push(`queue ${e.queueDepth}`);
  if (parts.length) return {known: true, text: parts.join(' · ')};
  return {known: false, text: e.observationSource === 'registry' ? 'Not measured · the registry probe reads model identity only'
    : 'Not measured · not reported by this endpoint'};
}

const GIB = 2 ** 30;
const MEASURED = 'Not measured';
const notMeasured = why => `${MEASURED} · ${why}`;
const gb = bytes => `${(bytes / GIB).toFixed(1)} GB`;
const bytes = value => Number.isSafeInteger(value) && value >= 0 && value <= 2 ** 50 ? value : null;
const percent = value => number(value) && value >= 0 && value <= 100 ? Math.round(value) : null;
const inRange = (value, low, high) => number(value) && value >= low && value <= high;
const ageText = seconds => `${Math.round(seconds)} s ago`;
const MEMORY_LEVELS = new Set(['ok', 'watch', 'tight', 'critical']);
const PRESSURE = /^[a-z]{1,16}$/;
const GPU_NAME = /^[\x20-\x7e]{1,64}$/;
const reasonText = value => typeof value === 'string' && /^[\x20-\x7e]{1,160}$/.test(value) ? value : null;
/** Age of a reading from an epoch time, or a reason it cannot be read. */
function readingAge(at, now, limit) {
  if (!number(at) || !number(now)) return {why: 'reading time not reported'};
  if (now < at - 2) return {why: 'reading time is in the future (clock skew)'};
  const age = Math.max(0, now - at);
  return age > limit ? {why: `reading is ${Math.round(age)} s old`, age} : {age};
}

function memoryGroup(snapshot, fresh, now) {
  const source = ['Source', 'Memory guard · macOS memory pressure'];
  const block = snapshot.memory;
  const fail = why => ({title: 'This Mac · memory', rows: [['Memory', notMeasured(why)], source, ['Age', 'Unknown']]});
  if (!fresh) return fail('the sample is not live');
  if (!block || typeof block !== 'object' || Array.isArray(block)) return fail('not reported in this sample');
  if (!MEMORY_LEVELS.has(block.level)) return fail(reasonText(list0(block.reasons)) || 'memory level unknown');
  const clock = readingAge(block.sampledAt, now, 10);
  if (clock.why) return fail(clock.why);
  const available = percent(block.availablePercent), pressure = typeof block.pressure === 'string' && PRESSURE.test(block.pressure) ? block.pressure : null;
  const ram = bytes(block.ramBytes);
  const reading = [available !== null ? `${available}% available` : null, pressure ? `pressure ${pressure}` : null].filter(Boolean).join(' · ');
  return {title: 'This Mac · memory', rows: [['Memory', reading || notMeasured('availability not reported')],
    ['Installed memory', ram !== null && ram >= GIB ? gb(ram) : notMeasured('not reported')], source, ['Age', ageText(clock.age)]]};
}
const list0 = value => Array.isArray(value) ? value[0] : null;

function macGpuGroup(snapshot, fresh, now, sampledAt) {
  const gpu = snapshot.macGpu && typeof snapshot.macGpu === 'object' && !Array.isArray(snapshot.macGpu) ? snapshot.macGpu : null;
  const sourceRow = Array.isArray(snapshot.sources) ? snapshot.sources.find(row => row?.id === 'mac-gpu') : null;
  const rows = [], source = ['Source', 'ioreg IOAccelerator'];
  if (!fresh) return {title: 'This Mac · GPU', rows: [['GPU', notMeasured('the sample is not live')], source, ['Age', 'Unknown']]};
  // The Metal ceiling is read once by the app shell, independently of the ioreg sample.
  const ceiling = gpu ? bytes(gpu.unifiedMemoryCeilingBytes) : null;
  const ceilingSource = gpu ? reasonText(gpu.unifiedMemoryCeilingSource) : null;
  const ceilingRow = ['Metal ceiling', ceiling !== null && ceiling >= GIB
    ? `${gb(ceiling)} of shared (unified) memory, not dedicated VRAM${ceilingSource ? ` · ${ceilingSource}` : ''}`
    : notMeasured(ceilingSource && ceilingSource.startsWith('unknown') ? ceilingSource : 'not reported in this sample')];
  const clock = gpu && number(gpu.ageSeconds) && gpu.ageSeconds >= 0 ? readingAge(sampledAt - gpu.ageSeconds, now, 10) : null;
  const sampled = gpu && clock && !clock.why && (gpu.utilizationPercent !== undefined || gpu.inUseBytes !== undefined || gpu.model !== undefined);
  if (!sampled) {
    const why = clock?.why || reasonText(sourceRow?.detail) || 'not reported in this sample';
    return {title: 'This Mac · GPU', rows: [['GPU', notMeasured(why)], ceilingRow, source, ['Age', 'Unknown']]};
  }
  const model = typeof gpu.model === 'string' && /^[A-Za-z0-9 ._()+-]{1,64}$/.test(gpu.model) ? gpu.model : null;
  const cores = Number.isSafeInteger(gpu.cores) && gpu.cores > 0 && gpu.cores <= 1024 ? gpu.cores : null;
  const busy = percent(gpu.utilizationPercent), inUse = bytes(gpu.inUseBytes);
  rows.push(['GPU', [model, cores !== null ? `${cores} cores` : null].filter(Boolean).join(' · ') || notMeasured('model not reported')],
    ['GPU load · all apps', busy !== null ? `${busy}%` : notMeasured('not reported by ioreg')],
    ['GPU memory in use', inUse !== null ? `${gb(inUse)} of shared memory` : notMeasured('not reported by ioreg')],
    ceilingRow, source, ['Age', ageText(clock.age)]);
  return {title: 'This Mac · GPU', rows};
}

const gpuRowValid = g => g && Number.isInteger(g.index) && g.index >= 0 && g.index <= 63 && typeof g.name === 'string'
  && GPU_NAME.test(g.name) && g.name.trim() === g.name && inRange(g.utilizationPercent, 0, 100) && inRange(g.memoryUsedMiB, 0, 1048576)
  && inRange(g.memoryTotalMiB, 1, 1048576) && g.memoryUsedMiB <= g.memoryTotalMiB && inRange(g.temperatureC, 0, 150) && inRange(g.powerW, 0, 2000);
const gibText = mib => (mib / 1024).toFixed(1);
function gpuRows(g) {
  const load = inRange(g.utilizationPercent, 0, 100) ? `${Math.round(g.utilizationPercent)}%` : notMeasured('not reported');
  const memory = inRange(g.memoryUsedMiB, 0, 1048576) && inRange(g.memoryTotalMiB, 1, 1048576) && g.memoryUsedMiB <= g.memoryTotalMiB
    ? `${gibText(g.memoryUsedMiB)} of ${gibText(g.memoryTotalMiB)} GB` : notMeasured('not reported');
  return [['GPU load · all apps', load], ['Dedicated memory', memory],
    ['Temperature', inRange(g.temperatureC, 0, 150) ? `${Math.round(g.temperatureC)} °C` : notMeasured('not reported')],
    ['Power', inRange(g.powerW, 0, 2000) ? `${Math.round(g.powerW)} W` : notMeasured('not reported')]];
}

/** The Mac's PC GPUs from the relayed worker heartbeat: every row is checked and one bad row rejects them all. */
function pcHeartbeatGroups(snapshot, fresh, now, sampledAt) {
  const worker = snapshot.windowsWorker;
  const source = ['Source', 'Windows worker heartbeat (relayed)'];
  const fail = why => [{title: 'Windows PC · GPUs', rows: [['GPUs', notMeasured(why)], source, ['Age', 'Unknown']]}];
  if (!fresh) return fail('the sample is not live');
  if (!worker || typeof worker !== 'object' || !number(worker.ageSeconds) || worker.ageSeconds < 0) return fail('no worker heartbeat in this sample');
  const clock = readingAge(sampledAt - worker.ageSeconds, now, 60);
  if (clock.why) return fail(clock.age !== undefined ? `no fresh worker heartbeat (${Math.round(clock.age)} s old)` : clock.why);
  if (!Array.isArray(worker.gpus) || !worker.gpus.length) return fail('not reported by the worker heartbeat');
  if (worker.gpus.length > 4 || !worker.gpus.every(gpuRowValid) || new Set(worker.gpus.map(g => g.index)).size !== worker.gpus.length)
    return fail('heartbeat GPU rows failed validation');
  return [...worker.gpus].sort((a, b) => a.index - b.index).map(g => ({title: `Windows PC · GPU ${g.index} · ${g.name}`,
    rows: [...gpuRows(g), source, ['Age', ageText(clock.age)]]}));
}

/** The Windows edition's own adapters (nvidia-smi and Windows counters), sampled with the snapshot. */
function pcLocalGroups(snapshot, fresh, now, sampledAt) {
  const source = ['Source', 'This PC GPU counters'];
  const fail = why => [{title: 'This PC · GPUs', rows: [['GPUs', notMeasured(why)], source, ['Age', 'Unknown']]}];
  if (!fresh) return fail('the sample is not live');
  const clock = readingAge(sampledAt, now, 10);
  if (clock.why) return fail(clock.why);
  const gpus = Array.isArray(snapshot.pcGpus) ? snapshot.pcGpus.slice(0, 8).filter(g => g && typeof g.name === 'string' && GPU_NAME.test(g.name)) : [];
  if (!gpus.length) return fail('no adapter reading');
  return gpus.map(g => ({title: `This PC · GPU · ${g.name}`, rows: [...gpuRows(g), source, ['Age', ageText(clock.age)]]}));
}

/** Capacity readings by machine, each with its source and age. Only what a probe reported; GPU-to-lane assignment
 *  and archive capacity are not measured by any reading yet. */
export function machineCapacity(snapshot, {fresh = false, now = Date.now() / 1000} = {}) {
  const fleet = {title: 'Fleet capacity', rows: [['GPU assignment', 'NOT AVAILABLE YET · no reading links a lane to a GPU'],
    ['Archive capacity', 'NOT AVAILABLE YET · this monitor does not measure archive storage']]};
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) return [fleet];
  const sampledAt = number(snapshot.fullSampledAt) ? snapshot.fullSampledAt : snapshot.sampledAt;
  const live = fresh === true && number(sampledAt);
  const groups = [];
  if (snapshot.host === 'windows') groups.push(...pcLocalGroups(snapshot, live, now, sampledAt));
  else if (snapshot.host === 'mac') {
    groups.push(memoryGroup(snapshot, live, now), macGpuGroup(snapshot, live, now, sampledAt));
    if (snapshot.windowsWorker !== undefined) groups.push(...pcHeartbeatGroups(snapshot, live, now, sampledAt));
  }
  return [...groups, fleet];
}

function appendLanes(document, card, lanes) {
  const list = el(document, 'ul', 'j-machine-lanes');
  list.setAttribute('aria-label', 'Lanes');
  for (const lane of lanes) {
    const facts = laneFacts(lane);
    const item = el(document, 'li', 'j-machine-lane');
    const model = el(document, 'span', 'j-lane-model', facts.model.label);
    if (facts.model.title) model.setAttribute('title', facts.model.title);
    const seen = el(document, 'span', 'j-lane-seen', `Last probe: ${facts.seen}`);
    if (facts.observedAt) seen.setAttribute('title', facts.observedAt);
    item.append(el(document, 'strong', 'j-lane-label', facts.label), model,
      el(document, 'span', 'j-lane-health', facts.health), seen);
    if (facts.id) item.append(el(document, 'code', 'j-lane-id', facts.id),
      el(document, 'span', 'j-lane-configured', `Configured endpoint: ${facts.configured}`),
      el(document, 'span', 'j-lane-context', `Configured context: ${facts.context}; thinking: ${facts.thinking}`),
      el(document, 'span', 'j-lane-probe-state', `Probe: ${facts.probeState}; configured timeout: ${facts.timeout}`),
      el(document, 'span', 'j-lane-last-success', `Last successful probe: ${facts.lastSuccess}${facts.lastSuccessModel ? ' · reported model ' + facts.lastSuccessModel : ''}`),
      el(document, 'span', 'j-lane-runtime', `Runtime: ${facts.runtime}`));
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
export function paintMachines(document, snapshot, {fresh, now = Date.now() / 1000}) {
  const root = document.getElementById('machinesBody');
  if (!root) return;
  const status = document.getElementById('machinesStatus');
  if (status) status.textContent = !snapshot ? 'Snapshot unavailable. Machines are unknown.' : fresh ? 'Live sample.' : 'Last sample, not live.';
  const map = el(document, 'pre', 'j-schematic j-peer-map');
  const summary = fleetSummary(snapshot, {now});
  const view = {label: summary.text, text: fleetMachines(snapshot, {now}).map(fact => `${fact.label} (${fact.id}) · ${fleetWord(fact)}`).join('\n') || summary.text};
  map.setAttribute('role', 'img');
  map.setAttribute('aria-label', view.label);
  map.textContent = `${summary.text}\n${view.text}`;
  const grid = el(document, 'div', 'j-machine-grid');
  const tiles = machineTiles(snapshot, snapshot?.host, {fresh, now});
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
  const capacity = el(document, 'section', 'j-machine-capacity');
  capacity.setAttribute('aria-labelledby', 'machinesCapacityHeading');
  const heading = el(document, 'h2', null, 'Capacity and runtime readings');
  heading.id = 'machinesCapacityHeading';
  const capacityGrid = el(document, 'div', 'j-machine-grid');
  for (const group of machineCapacity(snapshot, {fresh, now})) {
    const card = el(document, 'section', 'j-machine j-machine-reading');
    card.setAttribute('aria-label', group.title);
    const facts = el(document, 'dl', 'j-machine-facts');
    for (const [label, text] of group.rows) facts.append(el(document, 'dt', null, label), el(document, 'dd', null, text));
    card.append(el(document, 'h3', 'j-machine-title', group.title), facts);
    capacityGrid.append(card);
  }
  capacity.append(heading, capacityGrid);
  root.replaceChildren(map, grid, capacity);
}

/** Subscribe once; the store owns refresh deadlines and local ageing. */
export function mountMachines({document, store = getMonitorStore(), now = () => Date.now() / 1000}) {
  const unsubscribe = store.subscribe(state => {
    paintMachines(document, state.snapshot, {fresh: state.snapshotStatus?.fresh === true, now: now()});
  });
  store.start({snapshot: true});
  return {destroy() { unsubscribe(); }};
}

if (typeof document !== 'undefined' && document.getElementById?.('machinesBody')) {
  mountMachines({document});
}
