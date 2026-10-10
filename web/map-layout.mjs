/** Stable, responsive coordinates for the monitor's constellation map. */
const ACTIVE_STATES = new Set(['busy', 'generating']);

/** A presentation choice for this Mac; telemetry and model control keep the full feed. */
export const CORE_MODEL_IDS = Object.freeze([
  'openai/gpt-oss-20b',
  'qwen/qwen3.8-27b',
  'google/gemma-4-26b-a4b-qat',
  'qwen/qwen3.6-35b-a3b',
  'google/gemma-3-4b',
  'text-embedding-nomic-embed-text-v1.5',
]);
const CORE_MODEL_SET = new Set(CORE_MODEL_IDS);
const modelRowKey = row => `${row?.host}\n${row?.id}`;
const lastObservedInUse = row => row?.loaded === true || ACTIVE_STATES.has(row?.state)
  || (Number.isSafeInteger(row?.queued) && row.queued > 0);

/** Keep a non-core Mac model visible when it was last reported loaded or in use. */
export function runtimeModelRoster(rows, { scope = 'core', observedRows = rows } = {}) {
  const input = Array.isArray(rows) ? rows : [];
  const observations = new Map((Array.isArray(observedRows) ? observedRows : [])
    .filter(row => row && typeof row === 'object').map(row => [modelRowKey(row), row]));
  const visible = [], hidden = [], operationalExtras = [];
  let coreDiscovered = 0, macDiscovered = 0;
  for (const row of input) {
    if (!row || typeof row !== 'object') continue;
    if (row.host !== 'mac') { visible.push(row); continue; }
    macDiscovered += 1;
    if (CORE_MODEL_SET.has(row.id)) { coreDiscovered += 1; visible.push(row); continue; }
    if (scope === 'all') { visible.push(row); continue; }
    if (lastObservedInUse(observations.get(modelRowKey(row)) || row)) {
      visible.push(row);
      operationalExtras.push(row);
    } else hidden.push(row);
  }
  return { visible, hidden, operationalExtras, coreDiscovered, macDiscovered };
}

export function prioritizeRuntimeModels(models, visibleLimit = Infinity) {
  const ordered = [...models].sort((a, b) => Number(ACTIVE_STATES.has(b.state)) - Number(ACTIVE_STATES.has(a.state))
    || Number(b.loaded === true) - Number(a.loaded === true)
    || String(a.id).localeCompare(String(b.id)));
  const active = ordered.filter(model => ACTIVE_STATES.has(model.state));
  const inactive = ordered.filter(model => !ACTIVE_STATES.has(model.state));
  const visible = [...active, ...inactive.slice(0, Math.max(0, visibleLimit - active.length))];
  const visibleSet = new Set(visible);
  return { visible, overflow: inactive.filter(model => !visibleSet.has(model)) };
}

/** A Windows host node is inventory-only and requires a recent advertised worker heartbeat. */
export function hasAdvertisedWindowsWorker(worker, { feedFresh, snapshotAge = 0, maxAge = 60 } = {}) {
  return Boolean(feedFresh && worker?.state === 'advertised'
    && Number.isFinite(worker.ageSeconds) && worker.ageSeconds >= 0
    && worker.ageSeconds + Math.max(0, snapshotAge) <= maxAge);
}

/** AFM is a separate Mac adapter lane, never part of the LM Studio Core 6 count. */
export function afmView(status, { feedFresh = false, host = 'mac' } = {}) {
  if (host !== 'mac') return { visible: false, state: 'unknown', label: 'Unavailable', tone: 'muted' };
  const allowed = ['executable', 'missing', 'untrusted', 'not-executable', 'unknown'];
  const state = feedFresh && status?.schemaVersion === 1 && status?.host === 'mac'
    && allowed.includes(status.state) ? status.state : 'unknown';
  const labels = {
    executable: 'Adapter executable',
    missing: 'Adapter missing',
    untrusted: 'Adapter untrusted',
    'not-executable': 'No execute permission',
    unknown: 'Adapter status unknown',
  };
  return { visible: true, state, label: labels[state], tone: state === 'executable' ? 'ok' : state === 'unknown' ? 'muted' : 'warn',
    inference: 'Not tested by passive feed' };
}

/** Jev's opt-in and archived judgment are passive metadata, never live health or generation. */
export function jevView(component, { feedFresh = false, snapshotAge = 0 } = {}) {
  const state = feedFresh && ['configured', 'unavailable'].includes(component?.state) ? component.state : 'unknown';
  const recordedAge = component?.lastJudgedAgeSeconds;
  const lastJudgedAgeSeconds = Number.isFinite(recordedAge) && recordedAge >= 0
    ? recordedAge + Math.max(0, Number.isFinite(snapshotAge) ? snapshotAge : 0) : null;
  return { state, lastJudgedAgeSeconds,
    detail: typeof component?.detail === 'string' ? component.detail : null };
}

/** Windows jobs from the Mac dispatcher journal, aged by the snapshot's own age. */
export function windowsJobsView(jobs, { feedFresh, snapshotAge = 0 } = {}) {
  const extra = Math.max(0, Number.isFinite(snapshotAge) ? snapshotAge : 0);
  const valid = Boolean(jobs && typeof jobs === 'object' && jobs.schemaVersion === 1);
  const aged = row => ({ ...row, ageSeconds: row.ageSeconds + extra });
  const inFlight = valid && Array.isArray(jobs.inFlight)
    ? jobs.inFlight.filter(row => row && Number.isFinite(row.ageSeconds) && Number.isFinite(row.timeoutSeconds))
      .map(aged).filter(row => row.ageSeconds <= row.timeoutSeconds + 30)
    : [];
  const recent = valid && Array.isArray(jobs.recent)
    ? jobs.recent.filter(row => row && Number.isFinite(row.ageSeconds) && typeof row.state === 'string').map(aged)
    : [];
  const last = valid ? jobs.lastSuccess : null;
  const lastVerified = last && typeof last.model === 'string' && Number.isFinite(last.elapsedSeconds)
    && Number.isFinite(last.ageSeconds) ? aged(last) : null;
  // "Current" only while the feed is fresh, the success is recent and no newer
  // job has settled differently; an old success stays history in the inspector.
  const newest = [...recent].sort((a, b) => a.ageSeconds - b.ageSeconds)[0];
  const current = Boolean(feedFresh) && Boolean(lastVerified) && lastVerified.ageSeconds <= 1800
    && (!newest || (newest.state === 'success' && newest.id === lastVerified.id));
  return { running: Boolean(feedFresh) && inFlight.length > 0, inFlight, recent, lastVerified, current };
}

const LANE_IDS = ['fast', 'deep'];
const CLIENT_LABEL = /^[a-z0-9][a-z0-9._-]{0,39}$/i;
const slotCount = value => Number.isSafeInteger(value) && value >= 0 ? value : null;

/** Windows headless lanes and the clients journaled against them; nothing is inferred beyond the snapshot. */
export function windowsLaneView(worker, jobs, { feedFresh, snapshotAge = 0 } = {}) {
  const fresh = Boolean(feedFresh), raw = worker?.lanes, extra = Math.max(0, Number.isFinite(snapshotAge) ? snapshotAge : 0);
  const beat = ['advertised', 'degraded', 'stopped'].includes(worker?.state) && Number.isFinite(worker?.ageSeconds)
    && worker.ageSeconds >= 0 && worker.ageSeconds + extra <= 300;
  const visible = fresh && beat && Boolean(raw) && typeof raw === 'object' && !Array.isArray(raw);
  const lanes = visible ? LANE_IDS.map(id => [id, raw[id]])
    .filter(([, lane]) => lane && typeof lane === 'object' && typeof lane.up === 'boolean'
      && typeof lane.model === 'string' && lane.model.trim() && lane.model.length <= 80)
    .map(([id, lane]) => {
      const busy = slotCount(lane.slotsBusy), total = slotCount(lane.slotsTotal), slots = busy !== null && total !== null && busy <= total;
      return { id, label: `${id} · ${lane.model.trim().split('/').pop()}`, up: lane.up, busy: slots ? busy : null, total: slots ? total : null };
    }) : [];
  const h = worker?.headless, left = Number.isSafeInteger(h?.expiresInSeconds) ? Math.floor(h.expiresInSeconds - extra) : null;
  const on = fresh && h?.state === 'on' && left !== null && left > 0;
  const label = on ? `Headless on · ${Math.floor(left / 3600)}h ${Math.floor(left % 3600 / 60)}m left`
    : fresh && (h?.state === 'off' || (h?.state === 'on' && left !== null)) ? 'Headless off' : 'Headless unknown';
  const laneIds = new Set(lanes.map(lane => lane.id)), edges = new Map();
  const liveLanes = new Set(fresh && Array.isArray(jobs?.inFlight)
    ? jobs.inFlight.filter(row => row && laneIds.has(row.lane)).map(row => row.lane) : []);
  lanes.forEach(lane => {
    lane.live = liveLanes.has(lane.id);
    // Last measured generation speed on this lane, from the newest successful journaled job.
    const done = Array.isArray(jobs?.recent) ? jobs.recent.filter(row => row && row.lane === lane.id && row.state === 'success'
      && Number.isFinite(row.predictedPerSecond) && row.predictedPerSecond > 0 && Number.isFinite(row.ageSeconds))
      .sort((a, b) => a.ageSeconds - b.ageSeconds)[0] : null;
    lane.rate = done ? done.predictedPerSecond : null;
    lane.rateAgeSeconds = done ? done.ageSeconds : null;
  });
  const rows = [...(Array.isArray(jobs?.inFlight) ? jobs.inFlight.map(row => [row, fresh]) : []),
    ...(Array.isArray(jobs?.recent) ? jobs.recent.map(row => [row, false]) : [])];
  for (const [row, live] of rows) {
    if (!row || typeof row.client !== 'string' || !CLIENT_LABEL.test(row.client) || !laneIds.has(row.lane)) continue;
    const client = row.client.toLowerCase(), key = `${client}\n${row.lane}`;
    if (!edges.has(key) || (live && !edges.get(key).live)) edges.set(key, { client, lane: row.lane, live });
  }
  // The Mac observer (or the dispatcher) rejected the heartbeat's lane detail: say so, not "absent".
  const lanesError = fresh && beat && typeof worker?.lanesError === 'string' && worker.lanesError.trim() !== '';
  // fresh and beat say why lanes are hidden: a stale feed, or a missing or old worker heartbeat.
  return { visible, fresh, beat, lanesError, headless: { on, label }, lanes, edges: [...edges.values()] };
}

/** Inspector rows for the PC lanes; a hidden lane list names its actual reason. */
export function windowsLaneRows(view) {
  if (view?.visible) {
    return view.lanes.length ? view.lanes.map(lane => [`Lane ${lane.label}`,
      [lane.up ? 'Up' : 'Down', lane.total !== null ? `${lane.busy}/${lane.total} slots busy` : 'Slots unknown'].join(' · ')])
      : [['Lanes', 'Lane detail malformed']];
  }
  if (!view?.fresh) return [['Lanes', 'Not reported (live feed stale)']];
  if (!view.beat) return [['Lanes', 'Not reported (worker heartbeat not fresh)']];
  return [['Lanes', view.lanesError ? 'Lane detail malformed' : 'Heartbeat has no lane detail']];
}

/**
 * Caption positions for one map node, in node-local layout units. `scale` grows as the map zooms
 * out (captions keep a readable size), so fixed layout gaps shrink relative to them. Side captions
 * (PC lanes) read outward. The PC hub's subtitle moves above the hub whenever, at this zoom, it would
 * reach the captions of the lanes hanging `laneGap` units below it. Above the hub it shares the band
 * with the Mac hub's captions and the nearest client branch, so it switches to the node's short
 * `subtitleAbove` (status and time only; the inspector keeps the full line). `subText` is the text to show.
 */
export function nodeCaptionLayout(node, scale, { laneGap = Infinity } = {}) {
  const r = node.r;
  if (node.captionSide) {
    const x = (r + 7) * (node.captionSide === 'left' ? -1 : 1), anchor = node.captionSide === 'left' ? 'end' : 'start';
    return { label: { x, y: 4 * scale, anchor }, sub: { x, y: 16 * scale, anchor }, subAbove: false, subText: node.subtitle };
  }
  const label = { x: 0, y: node.captionAbove ? -(r + 9 * scale) : r + (node.hub ? 30 : node.root ? 25 : 19) * scale, anchor: 'middle' };
  const below = r + (node.hub ? 44 : node.root ? 39 : 32) * scale;
  // Subtitle bottom (baseline + 2*scale descent) plus a 3-unit margin against a lane label's top
  // (baseline 4*scale, 12*scale font, about 10*scale above its baseline).
  const subAbove = node.kind === 'windows-worker' && below + 2 * scale + 3 > laneGap - 6 * scale;
  const subText = subAbove && typeof node.subtitleAbove === 'string' ? node.subtitleAbove : node.subtitle;
  return { label, sub: { x: 0, y: subAbove ? -(r + 9 * scale) : below, anchor: 'middle' }, subAbove, subText };
}

export function parseParameterCount(value) {
  if (typeof value === 'number') return Number.isFinite(value) && value > 0 && value <= 1e15 ? value : null;
  if (typeof value !== 'string') return null;
  const match = value.trim().match(/^([\d,]+(?:\.\d+)?)\s*([KMBT])?(?:\s+parameters?)?$/i);
  if (!match) return null;
  const amount = Number(match[1].replaceAll(',', ''));
  const multiplier = ({ K: 1e3, M: 1e6, B: 1e9, T: 1e12 })[String(match[2] || '').toUpperCase()] || 1;
  const count = amount * multiplier;
  return Number.isFinite(count) && count > 0 && count <= 1e15 ? count : null;
}

export function modelStarSize(model) {
  const parameters = parseParameterCount(model?.metadata?.parameters ?? model?.parameters);
  const bytes = Number.isSafeInteger(model?.sizeBytes) && model.sizeBytes > 0 && model.sizeBytes <= 1e16 ? model.sizeBytes : null;
  const basis = parameters ? 'parameter count' : bytes ? 'installed size proxy' : 'unknown';
  const amount = parameters || bytes;
  const lower = basis === 'parameter count' ? 1e8 : 1e8;
  const upper = basis === 'parameter count' ? 1e12 : 1e12;
  const fraction = amount ? Math.max(0, Math.min(1, (Math.log10(amount) - Math.log10(lower)) / (Math.log10(upper) - Math.log10(lower)))) : 0;
  const radius = amount ? 10 + fraction * 12 : 11;
  const metadata = model?.metadata || {}, capabilities = metadata.capabilities || {};
  const type = typeof metadata.type === 'string' ? metadata.type.toLowerCase() : '';
  const color = /embed/.test(type) ? '#bd9cff' : capabilities.reasoning === true ? '#70d7ed'
    : capabilities.vision === true ? '#f1b568' : capabilities.toolUse === true ? '#6bd6a2' : '#99a3b7';
  const labels = [['vision',capabilities.vision],['tool use',capabilities.toolUse],['reasoning',capabilities.reasoning],['reasoning options',capabilities.reasoningOptions]]
    .filter(([,enabled]) => enabled === true).map(([label]) => label);
  if (/embed/.test(type)) labels.unshift('embedding');
  const capabilityKnown = ['vision','toolUse','reasoning','reasoningOptions'].some(key => typeof capabilities[key] === 'boolean');
  const value = parameters ? `${parameters.toLocaleString('en-US')} parameters`
    : bytes ? `${(bytes / 1e9).toFixed(2)} GB installed-size proxy` : 'Unknown';
  return { radius, basis, amount, value, parameters, bytes, color, capabilityLabel: labels.length ? labels.join(' · ') : capabilityKnown ? 'No listed capabilities' : 'Capabilities unknown' };
}

export function constellationLayout(runtimeCount, clients, { viewportWidth = 820, viewportHeight = 640, host = 'mac' } = {}) {
  const width = Math.max(320, viewportWidth), height = Math.max(420, viewportHeight);
  const portrait = width < 600 || height > width * 1.2;
  const compactLandscape = !portrait && width < 900;
  const hostLabel = host === 'windows' ? 'PC' : 'MAC';
  const center = portrait
    ? { x: width / 2, y: Math.max(92, height * .22) }
    : compactLandscape ? { x: width * .52, y: height / 2 }
      : { x: width / 2 + 105, y: height / 2 };
  const cols = Math.min(3, Math.max(1, runtimeCount));
  const rows = Math.ceil(runtimeCount / cols);
  const cellX = compactLandscape ? 60 : portrait ? Math.min(72, width * .17) : 86;
  const cellY = compactLandscape ? 70 : 76;
  // Models retain a compact, ordered group next to the Mac hub. This restores
  // the legibility of the original 3x3 inventory while preserving direct spokes.
  const modelCenter = portrait
    ? { x: center.x, y: center.y + 228 }
    : { x: center.x - (compactLandscape ? width * .25 : 245), y: center.y };
  const runtimeModels = Array.from({ length: runtimeCount }, (_, index) => {
    const col = index % cols, row = Math.floor(index / cols);
    return {
      x: modelCenter.x + (col - (Math.min(cols, runtimeCount) - 1) / 2) * cellX,
      y: modelCenter.y + (row - (rows - 1) / 2) * cellY,
    };
  });
  const clientLanes = clients.map((client, index) => {
    const count = Math.max(1, client.modelCount);
    if (portrait) {
      const x = width * ((index + 1) / (clients.length + 1));
      // Keep the client branches close enough to read as one constellation,
      // while leaving room for the last model row and its captions.
      const y = Math.max(height * .60, modelCenter.y + (rows - 1) * cellY / 2 + 116);
      const offsets = Array.from({ length: count }, (_, i) => (i - (count - 1) / 2) * 30);
      return { x, y, models: offsets.map((offset, i) => ({ x, y: y + 44 + i * 34 + offset * .25 })) };
    }
    const spreadStep = compactLandscape
      ? Math.min(110, height * .28 / Math.max(1, (clients.length - 1) / 2))
      : Math.min(150, height * .25);
    const spread = clients.length <= 1 ? 0 : (index - (clients.length - 1) / 2) * spreadStep;
    const x = center.x + (compactLandscape ? width * .24 : Math.min(width * .27, 225)), y = center.y + spread;
    const modelCols = Math.min(2, count), modelRows = Math.ceil(count / modelCols);
    return { x, y, models: Array.from({ length: count }, (_, i) => ({
      x: x + (compactLandscape ? 52 : 78) + (i % modelCols) * (compactLandscape ? 38 : 44),
      y: y + (Math.floor(i / modelCols) - (modelRows - 1) / 2) * 34,
    })) };
  });
  const pipeline = portrait
    ? { x: Math.max(40, center.x - Math.min(width * .35, 175)), y: center.y - 12 }
    : { x: center.x, y: Math.max(55, center.y - Math.min(height * .34, 210)) };
  // Jev is a child of Nisi Inference, offset from AFM and the Mac hub in both layouts.
  const jev = portrait
    ? { x: pipeline.x + 60, y: pipeline.y + 65 }
    : { x: pipeline.x + 10, y: pipeline.y + 70 };
  const afm = portrait
    ? { x: pipeline.x, y: center.y + 110 }
    : { x: center.x + (compactLandscape ? 100 : 125), y: pipeline.y + 65 };
  // The Windows PC is its own constellation: below the Mac hub in wide views,
  // beside it in portrait. Its lanes fan out under the PC hub, clear of clients.
  const windows = portrait
    ? { x: Math.min(width - 40, center.x + Math.min(width * .35, 175)), y: center.y - 12 }
    : { x: center.x, y: center.y + (compactLandscape ? 170 : Math.max(180, Math.min(height * .36, 240))) };
  const windowsLanes = portrait
    ? [0, 1].map(i => ({ x: windows.x, y: windows.y + 58 + i * 54 }))
    : [-1, 1].map(side => ({ x: windows.x + side * (compactLandscape ? 62 : 74), y: windows.y + 64 }));
  const labels = portrait ? [
    { text: 'NISI INFERENCE', x: pipeline.x, y: pipeline.y - 26, align: 'middle', kind: 'pipeline' },
    { text: hostLabel, x: center.x, y: center.y - 38, align: 'middle', kind: 'mac' },
    { text: `LOCAL MODELS · ${runtimeCount}`, x: modelCenter.x, y: modelCenter.y - rows * cellY / 2 - 26, align: 'middle', kind: 'local' },
    { text: 'CLIENTS', x: width / 2, y: clientLanes.length ? clientLanes[0].y - 34 : height * .69 - 34, align: 'middle', kind: 'clients' },
  ] : [
    { text: `LOCAL MODELS · ${runtimeCount}`, x: modelCenter.x, y: modelCenter.y - rows * cellY / 2 - 27, align: 'middle', kind: 'local' },
    { text: hostLabel, x: center.x, y: center.y - 38, align: 'middle', kind: 'mac' },
    { text: 'CLIENTS', x: clientLanes[0]?.x ?? center.x, y: clientLanes.length ? clientLanes[0].y - 42 : center.y - Math.min(height * .42, 260), align: 'middle', kind: 'clients' },
    { text: 'NISI INFERENCE', x: pipeline.x, y: pipeline.y - 27, align: 'middle', kind: 'pipeline' },
  ];
  return { runtime: center, runtimeModels, pipeline, jev, afm, windows, windowsLanes, windowsLabel: { x: windows.x, y: windows.y - 28 }, clientLanes, labels, radiusX: width * .38, radiusY: height * .42, portrait };
}

// Retain the old export name for older dashboard smoke checks while changing
// its geometry to the constellation layout.
export const focusedLaneLayout = constellationLayout;

/** Fit actual graph geometry into the map's currently visible viewport area. */
export function fitGraph(nodes, area, { horizontalPadding = 32, verticalPadding = 40, maxScale = 1.6, minScale = 0.08 } = {}) {
  if (!nodes.length || area.width <= 0 || area.height <= 0) return null;
  const xs = nodes.map(node => node.x);
  const ys = nodes.map(node => node.y);
  const minX = Math.min(...xs) - horizontalPadding;
  const maxX = Math.max(...xs) + horizontalPadding;
  const minY = Math.min(...ys) - verticalPadding;
  const maxY = Math.max(...ys) + verticalPadding;
  const scale = Math.max(minScale, Math.min(maxScale, area.width / (maxX - minX), area.height / (maxY - minY)));
  return {
    scale,
    x: area.left + area.width / 2 - (minX + maxX) / 2 * scale,
    y: area.top + area.height / 2 - (minY + maxY) / 2 * scale,
  };
}

const overlaps = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
const labelBox = (label, y) => {
  const half = label.width / 2, left = label.align === 'start' ? label.x : label.align === 'end' ? label.x - label.width : label.x - half;
  return { left, right: left + label.width, top: y - label.height, bottom: y + label.height * .25 };
};

/**
 * Moves cluster labels off nodes, node captions and earlier labels. Labels try
 * upward first, then downward, in fixed steps; a label that cannot clear keeps
 * its original place rather than jumping far from its group.
 */
export function resolveLabelCollisions(labels, obstacles, { step = 4, maxShift = 64 } = {}) {
  const placed = [...obstacles];
  return labels.map(label => {
    let y = label.y;
    const clear = candidate => !placed.some(box => overlaps(labelBox(label, candidate), box));
    if (!clear(y)) {
      for (let shift = step; shift <= maxShift; shift += step) {
        if (clear(label.y - shift)) { y = label.y - shift; break; }
        if (clear(label.y + shift)) { y = label.y + shift; break; }
      }
    }
    placed.push(labelBox(label, y));
    return { ...label, y };
  });
}

/** Keeps at least `keep` screen pixels of the graph inside the visible area while panning or zooming. */
export function clampCamera(camera, bounds, area, { keep = 80 } = {}) {
  if (!bounds || ![camera.x, camera.y, camera.z].every(Number.isFinite)) return camera;
  const left = camera.x + bounds.minX * camera.z, right = camera.x + bounds.maxX * camera.z;
  const top = camera.y + bounds.minY * camera.z, bottom = camera.y + bounds.maxY * camera.z;
  const keepX = Math.min(keep, (right - left) / 2, (area.right - area.left) / 2);
  const keepY = Math.min(keep, (bottom - top) / 2, (area.bottom - area.top) / 2);
  let x = camera.x, y = camera.y;
  if (right < area.left + keepX) x += area.left + keepX - right;
  else if (left > area.right - keepX) x -= left - (area.right - keepX);
  if (bottom < area.top + keepY) y += area.top + keepY - bottom;
  else if (top > area.bottom - keepY) y -= top - (area.bottom - keepY);
  return { ...camera, x, y };
}

const DIRECTIONS = { ArrowRight: [1, 0], ArrowLeft: [-1, 0], ArrowDown: [0, 1], ArrowUp: [0, -1] };

/** The nearest node in an arrow key's direction, within a 60-degree cone; null when none. */
export function nextNodeInDirection(nodes, fromId, key) {
  const from = nodes.find(node => node.id === fromId), direction = DIRECTIONS[key];
  if (!from || !direction) return null;
  let best = null, bestScore = Infinity;
  for (const node of nodes) {
    if (node.id === from.id) continue;
    const dx = node.x - from.x, dy = node.y - from.y, distance = Math.hypot(dx, dy);
    if (!distance) continue;
    const along = (dx * direction[0] + dy * direction[1]) / distance;
    if (along < Math.cos(Math.PI / 3)) continue;
    const score = distance * (2 - along);
    if (score < bestScore || (score === bestScore && String(node.id) < String(best.id))) { best = node; bestScore = score; }
  }
  return best ? best.id : null;
}

/** The router's review-summary consistency, as a badge; a legacy contradiction flag reads as a warning. */
export function reviewConsistencyView(run) {
  const record = run?.reviewConsistency;
  if (record && typeof record === 'object' && ['SUMMARY_REPORTS_DEFECT', 'SUMMARY_MAY_REPORT_DEFECT'].includes(record.status)) {
    const block = record.status === 'SUMMARY_REPORTS_DEFECT';
    const rule = typeof record.rule === 'string' && /^[A-Za-z0-9_.:-]{1,64}$/.test(record.rule) ? record.rule : null;
    const evidence = typeof record.evidence === 'string' && record.evidence.trim() ? record.evidence.trim().slice(0, 160) : null;
    return { level: block ? 'block' : 'warn', badge: block ? 'Review defect' : 'Review warning',
      text: block ? 'The reviewer found no structured findings, but its summary states a defect. The route was stopped.'
        : 'The reviewer found no structured findings, but its summary may describe a defect. Recorded for the owner; the route was not stopped.',
      rule, evidence, stage: record.stage === 'macReturn' ? 'Mac return review' : record.stage === 'backend' ? 'Mac edit review' : null };
  }
  if (run?.reviewSummaryContradiction === true) {
    return { level: 'warn', badge: 'Review warning', text: 'Review inconsistency recorded: the summary contradicts the empty findings.', rule: null, evidence: null, stage: null };
  }
  return { level: null, badge: null, text: null, rule: null, evidence: null, stage: null };
}

/** Top-bar PC headless switch: a checked state only from a fresh "on" lease, never while a request is pending. */
export function headlessSwitchView(headless, { macOwner = true, feedFresh = false, pending = false, busy = false } = {}) {
  const known = Boolean(headless) && headless.label !== 'Headless unknown';
  const state = pending ? 'pending' : !known ? 'unknown' : headless.on ? 'on' : 'off';
  const time = state === 'on' ? headless.label.replace(/^Headless on · /, '') : null;
  return {
    state, checked: state === 'on', hidden: !macOwner,
    disabled: !macOwner || !feedFresh || !known || pending || busy,
    short: state === 'pending' ? 'Switching…' : state === 'on' ? time : state === 'off' ? 'Off' : 'Unknown',
    title: state === 'on' ? 'The Windows PC is held as a headless LLM. Click to release it.'
      : state === 'off' ? 'Hold the Windows PC as a headless LLM for 4 hours. Sends one short probe request to the PC.'
        : state === 'pending' ? 'A PC headless request is in progress.' : 'PC headless state is unknown until a fresh worker heartbeat arrives.',
  };
}

const LANE_TARGET = { fast: 'PC fast', deep: 'PC deep' };
const CLIENT_NAMES = { codex: 'Codex', claude: 'Claude', opencode: 'OpenCode', cursor: 'Cursor', grok: 'Grok' };
const capitalize = text => String(text).replace(/^./, c => c.toUpperCase());
// Who sent a feed row, in words: a known client's name, the PC LLM switch probe, or the journaled label.
const who = row => row.probe ? 'the PC LLM switch probe' : CLIENT_NAMES[row.client] || row.client || 'an unknown client';
const rateText = value => Number.isFinite(value) ? `${value >= 20 ? Math.round(value) : value.toFixed(1)} tok/s` : null;
const JOB_FLAGS = new Set(['hit-token-limit']);
const FAILED_STATE = /error|timeout|fail|reject|invalid/i;

/** The known flags a journaled job carries; anything else is dropped. */
export function jobFlags(row) {
  return Array.isArray(row?.flags) ? [...new Set(row.flags.filter(flag => JOB_FLAGS.has(flag)))] : [];
}

/** Prompt (read) and generation (write) speed as one phrase; a missing or non-positive speed is left out. */
export function speedsText(readPerSecond, writePerSecond) {
  const speed = value => Number.isFinite(value) && value > 0 ? rateText(value) : null;
  const read = speed(readPerSecond), write = speed(writePerSecond);
  return [read ? `reads ${read}` : null, write ? `writes ${write}` : null].filter(Boolean).join(' · ') || null;
}

/** Both speeds of a lane's newest measured success (the job `windowsLaneView` takes its rate from). */
export function laneSpeeds(jobs, laneId) {
  const done = Array.isArray(jobs?.recent) ? jobs.recent.filter(row => row && row.lane === laneId && row.state === 'success'
    && Number.isFinite(row.predictedPerSecond) && row.predictedPerSecond > 0 && Number.isFinite(row.ageSeconds))
    .sort((a, b) => a.ageSeconds - b.ageSeconds)[0] : null;
  return done ? { text: speedsText(done.promptPerSecond, done.predictedPerSecond), ageSeconds: done.ageSeconds,
    limitHit: jobFlags(done).includes('hit-token-limit') } : { text: null, ageSeconds: null, limitHit: false };
}

const percent = value => Number.isInteger(value) && value >= 0 && value <= 100 ? value : null;
const byteCount = value => Number.isSafeInteger(value) && value >= 0 && value <= 2 ** 50 ? value : null;
// Memory amounts are binary gigabytes labelled "GB", as Activity Monitor and the memory guard write them, so the Mac GPU
// tile and the Memory section of one inspector read the same bytes as the same number (27 Sep: they showed 40.5 and 38).
const gigabytes = value => `${(value / 2 ** 30).toFixed(1)} GB`;

/** The Mac GPU sample (ioreg), only from a fresh feed and a sample at most 5 s old; unknown otherwise. */
export function macGpuView(gpu, { feedFresh = false, snapshotAge = 0 } = {}) {
  const extra = Math.max(0, Number.isFinite(snapshotAge) ? snapshotAge : 0);
  const sampled = Boolean(feedFresh) && Boolean(gpu) && typeof gpu === 'object' && !Array.isArray(gpu)
    && Number.isFinite(gpu.ageSeconds) && gpu.ageSeconds >= 0 && gpu.ageSeconds + extra <= 5;
  if (!sampled) return { known: false, percent: null, chip: null, tile: 'Unknown', rows: [] };
  const busy = percent(gpu.utilizationPercent), allocated = byteCount(gpu.allocatedBytes), inUse = byteCount(gpu.inUseBytes);
  const model = typeof gpu.model === 'string' && /^[A-Za-z0-9 ._()+-]{1,64}$/.test(gpu.model) ? gpu.model : null;
  const cores = Number.isSafeInteger(gpu.cores) && gpu.cores > 0 && gpu.cores <= 1024 ? gpu.cores : null;
  const renderer = percent(gpu.rendererPercent), tiler = percent(gpu.tilerPercent);
  // The sample is the whole Mac GPU (every app), so the tile says "busy" and "GPU memory", never a model's share.
  return { known: busy !== null, percent: busy, chip: busy !== null ? `GPU ${busy}%` : null,
    tile: [busy !== null ? `${busy}% busy` : null, allocated !== null ? `${gigabytes(allocated)} GPU memory` : null].filter(Boolean).join(' · ') || 'Unknown',
    rows: [['GPU', [model, cores !== null ? `${cores} cores` : null].filter(Boolean).join(' · ') || null],
      ['GPU utilisation', busy !== null ? `${busy}%` : null], ['Renderer · tiler', renderer !== null && tiler !== null ? `${renderer}% · ${tiler}%` : null],
      ['GPU memory allocated', allocated !== null ? gigabytes(allocated) : null], ['GPU memory in use', inUse !== null ? gigabytes(inUse) : null]] };
}

const CALLER_NAME = /^[A-Za-z0-9 ._+-]{1,40}$/;

/** Local processes connected to the Mac model server, names only. null from the server means not checked. */
export function localCallersView(callers, { feedFresh = false } = {}) {
  if (!feedFresh || !Array.isArray(callers)) return { checked: false, names: [], text: null };
  const names = [...new Set(callers.filter(caller => caller && typeof caller.name === 'string' && CALLER_NAME.test(caller.name))
    .map(caller => caller.name.trim()).filter(Boolean))].slice(0, 6);
  return { checked: true, names, text: names.length ? `Calling the model server now: ${names.join(', ')}` : 'No local process is calling the model server' };
}

const WORKER_VERSION = /^[0-9A-Za-z._+-]{1,16}$/;
const GPU_NAME = /^[\x20-\x7e]{1,64}$/;
const inRange = (value, low, high) => typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high;
const gib = mib => (mib / 1024).toFixed(1);

/**
 * The PC's GPUs and worker version from the worker heartbeat (worker 1.2, relayed by chami-dispatch and
 * checked again by telemetry). Read only from a fresh feed and a heartbeat at most 60 s old; every row is
 * checked a third time here, and one bad row makes the whole sample unknown. A GPU reading is load on the
 * card, not proof that a job is running.
 */
export function pcGpuView(worker, { feedFresh = false, snapshotAge = 0 } = {}) {
  const extra = Math.max(0, Number.isFinite(snapshotAge) ? snapshotAge : 0);
  const beat = Boolean(feedFresh) && ['advertised', 'degraded', 'stopped'].includes(worker?.state)
    && Number.isFinite(worker?.ageSeconds) && worker.ageSeconds >= 0 && worker.ageSeconds + extra <= 60;
  const version = beat && typeof worker.workerVersion === 'string' && WORKER_VERSION.test(worker.workerVersion) ? worker.workerVersion : null;
  const raw = beat && Array.isArray(worker.gpus) && worker.gpus.length >= 1 && worker.gpus.length <= 4 ? worker.gpus : [];
  const valid = raw.every(g => g && Number.isInteger(g.index) && g.index >= 0 && g.index <= 63 && typeof g.name === 'string'
    && GPU_NAME.test(g.name) && g.name.trim() === g.name && inRange(g.utilizationPercent, 0, 100) && inRange(g.memoryUsedMiB, 0, 1048576)
    && inRange(g.memoryTotalMiB, 1, 1048576) && g.memoryUsedMiB <= g.memoryTotalMiB && inRange(g.temperatureC, 0, 150) && inRange(g.powerW, 0, 2000))
    && new Set(raw.map(g => g.index)).size === raw.length;
  const gpus = valid ? [...raw].sort((a, b) => a.index - b.index) : [];
  const reading = g => `${Math.round(g.utilizationPercent)}% · ${gib(g.memoryUsedMiB)}/${gib(g.memoryTotalMiB)} GB · ${Math.round(g.temperatureC)} °C`;
  const first = gpus[0];
  return { known: Boolean(first), version, chip: first ? `GPU ${Math.round(first.utilizationPercent)}%` : null,
    text: first ? `GPU ${reading(first)}` : null, tile: first ? reading(first) : 'Unknown',
    rows: [['Worker version', version || 'Not reported'],
      ...(gpus.length ? gpus.map(g => [`GPU ${g.index}`, `${g.name} · ${Math.round(g.utilizationPercent)}% busy · ${gib(g.memoryUsedMiB)} of ${gib(g.memoryTotalMiB)} GB · ${Math.round(g.temperatureC)} °C · ${Math.round(g.powerW)} W`])
        : [['PC GPU', beat ? 'Not reported by the worker heartbeat' : 'Unknown (no fresh worker heartbeat)']])] };
}

/* Memory (mem_guard's snapshot.memory, 26-27 Sep). Level ok / watch / tight / critical, or unknown when macOS
 * pressure and availability are unreadable. Read only from a fresh feed, and every field is checked again here: a
 * level outside the set reads unknown, a bad number is left out, a bad consumer or suggestion row is dropped. The
 * consumers are grouped processes with approximate resident sizes (shared pages counted once per process), and the
 * guard measures them only when memory is not ok. Suggestions are text only: the guard never quits an app or
 * unloads a model. Sizes use the guard's own binary gigabytes, so they match its reasons and suggestions. */
export const MEMORY_RANK = Object.freeze({ ok: 0, watch: 1, tight: 2, critical: 3 });
const MEMORY_LABEL = { ok: 'OK', watch: 'Watch', tight: 'Tight', critical: 'Critical', unknown: 'Unknown' };
const MEMORY_TEXT = /^[^\x00-\x1f\x7f]{1,240}$/;
const GIB = 2 ** 30, MIB = 2 ** 20;
const memoryBytes = value => Number.isSafeInteger(value) && value >= 0 && value <= 2 ** 52 ? value : null;
const memoryText = value => typeof value === 'string' && MEMORY_TEXT.test(value) ? value.replaceAll('`', '').trim() || null : null;
/** A closed reading: only one of the four level strings counts (an array, an inherited key or any other value does not). */
const isMemoryLevel = value => typeof value === 'string' && Object.hasOwn(MEMORY_RANK, value);
const isAlertLevel = value => value === 'tight' || value === 'critical';

/** A byte count as an approximate size ("12 GB", "3.4 GB", "512 MB", "under 1 MB"); null when it is not a sane count. */
export function memorySize(bytes) {
  if (memoryBytes(bytes) === null) return null;
  const gib = bytes / GIB;
  return gib >= 10 ? `${Math.round(gib)} GB` : gib >= 1 ? `${gib.toFixed(1)} GB` : bytes < MIB ? 'under 1 MB' : `${Math.round(bytes / MIB)} MB`;
}
/** A measured amount, one decimal ("37.7 GB", "512 MB"): the Memory rows and tile, which sit beside the GPU tile's figure. */
const memoryAmount = bytes => bytes >= GIB ? gigabytes(bytes) : memorySize(bytes);
/** "about 3.2 GB", or "under 1 MB" (never "about under 1 MB"). */
const approximately = bytes => { const size = memorySize(bytes); return size.startsWith('under') ? size : `about ${size}`; };
// The guard (mem_guard.decorate_consumers) already emits its users sorted by this weight, as a short list (named groups
// plus the top three others). Only a bounded prefix is sorted again here; past it the producer's order is trusted.
const CONSUMER_PREFIX = 256;

export function memoryView(memory, { feedFresh = false } = {}) {
  const block = feedFresh && memory && typeof memory === 'object' && !Array.isArray(memory) ? memory : null;
  const level = block && isMemoryLevel(block.level) ? block.level : 'unknown';
  const known = level !== 'unknown';
  const available = known && typeof block.availablePercent === 'number' && Number.isFinite(block.availablePercent)
    && block.availablePercent >= 0 && block.availablePercent <= 100 ? block.availablePercent : null;
  const swap = known ? memoryBytes(block.swapUsedBytes) : null, compressed = known ? memoryBytes(block.compressedBytes) : null;
  const gpu = known ? memoryBytes(block.gpuAllocBytes) : null;
  // Ranked like the guard ranks them: by what a user holds, resident memory or its GPU allocation, whichever is larger.
  const weight = row => Math.max(row.residentBytes, row.gpuAllocBytes || 0);
  const consumers = (known && Array.isArray(block.consumers) ? block.consumers.slice(0, CONSUMER_PREFIX) : [])
    .filter(row => row && typeof row === 'object' && memoryText(row.label) && memoryBytes(row.residentBytes) !== null
      && Number.isSafeInteger(row.processCount) && row.processCount >= 0 && row.processCount <= 1e6
      && (row.gpuAllocBytes === undefined || row.gpuAllocBytes === null || memoryBytes(row.gpuAllocBytes) !== null))
    .map(row => ({ label: memoryText(row.label).slice(0, 80), residentBytes: row.residentBytes, processCount: row.processCount,
      gpuAllocBytes: memoryBytes(row.gpuAllocBytes) }))
    .sort((a, b) => weight(b) - weight(a)).slice(0, 3)
    .map(row => {
      // The banner states the figure the ranking used: the GPU allocation when it is the larger one.
      const gpuLed = row.gpuAllocBytes !== null && row.gpuAllocBytes > row.residentBytes;
      return { ...row, weightBytes: weight(row), gpuLed, size: approximately(row.residentBytes),
        claim: gpuLed ? `holds ${approximately(row.gpuAllocBytes)} (GPU allocation)` : `uses ${approximately(row.residentBytes)}`,
        text: [approximately(row.residentBytes), `${row.processCount} process${row.processCount === 1 ? '' : 'es'}`,
          row.gpuAllocBytes ? `+ GPU allocation ${approximately(row.gpuAllocBytes)}` : null].filter(Boolean).join(' · ') };
    });
  const suggestions = (known && Array.isArray(block.suggestions) ? block.suggestions.slice(0, 16) : []).map(memoryText).filter(Boolean).slice(0, 3);
  const reasons = (known && Array.isArray(block.reasons) ? block.reasons.slice(0, 16) : []).map(memoryText).filter(Boolean).slice(0, 4);
  const paused = known && Array.isArray(block.paused) ? Math.min(block.paused.length, 256) : 0;
  const alert = isAlertLevel(level);
  const percent = available === null ? null : `${Math.round(available)}% available`;
  return { known, level, label: MEMORY_LABEL[level], alert, word: alert ? `Memory ${level}` : null, available, swap, compressed, gpu,
    consumers, suggestions, reasons, paused,
    tile: known ? [MEMORY_LABEL[level], percent, swap === null ? null : swap === 0 ? 'no swap' : `swap ${memoryAmount(swap)}`].filter(Boolean).join(' · ') : 'Unknown',
    rows: known ? [['Level', [MEMORY_LABEL[level], ...reasons].join(' · ')], ['Available', percent], ['Swap used', swap === null ? null : memoryAmount(swap)],
      ['Compressed', compressed === null ? null : memoryAmount(compressed)], ['GPU allocation', gpu === null ? null : memoryAmount(gpu)],
      ...(paused ? [['Paused jobs', `${paused} paused by the memory guard`]] : [])]
      : [['Level', block ? 'Unknown: macOS memory pressure is unreadable' : 'Unknown (no fresh sample)']] };
}

/* The guard classifies each once-a-second sample against fixed thresholds with no hysteresis, so a Mac sitting on a
 * threshold flaps (tight, watch, tight...). The banner's dismissal and its announcer each hold a level instead, as
 * { level, below, at } (below: when the level first fell under the held one; at: the last fresh sample's time, seconds):
 *  - a worse level releases the hold (the banner comes back and is announced);
 *  - ok releases it too (a later tight is a new episode);
 *  - a lower level lowers the hold only after MEMORY_EASE_SECONDS of consecutive fresh samples under it; a gap in the
 *    samples longer than MEMORY_SAMPLE_GAP_SECONDS (a stale feed, a paused view) restarts that clock;
 *  - an unknown level, a stale sample or a bad time changes nothing. */
export const MEMORY_EASE_SECONDS = 60, MEMORY_SAMPLE_GAP_SECONDS = 5;
const validHold = held => Boolean(held) && typeof held === 'object' && isAlertLevel(held.level) && Number.isFinite(held.at)
  && (held.below === null || Number.isFinite(held.below));

/** A new hold at a tight or critical level (dismissing the banner, or announcing it); null for any other level. */
export function memoryHoldAt(level, at) {
  return isAlertLevel(level) && Number.isFinite(at) ? { level, below: null, at } : null;
}

/** The next hold after one fresh sample at `level`, taken at `at` seconds (see above). */
export function memoryHold(held, level, at) {
  const kept = validHold(held) ? held : null;
  if (!isMemoryLevel(level) || !Number.isFinite(at)) return kept;
  if (kept === null || level === 'ok' || MEMORY_RANK[level] > MEMORY_RANK[kept.level]) return null;
  if (level === kept.level) return { level: kept.level, below: null, at };
  const since = kept.below !== null && at >= kept.at && at - kept.at <= MEMORY_SAMPLE_GAP_SECONDS ? kept.below : at;
  if (at - since >= MEMORY_EASE_SECONDS) return memoryHoldAt(level, at);
  return { level: kept.level, below: since, at };
}

/** Banner dismissal: the level the viewer dismissed, held as above. */
export const memoryDismissal = (dismissed, level, at) => memoryHold(dismissed, level, at);

/**
 * The polite announcer: it speaks when the banner appears after ok (or after a hold eased away) and when the level rises
 * in rank; a flap back to a level already announced, or a stale spell, says nothing again. Returns the next hold and
 * whether to speak.
 */
export function memoryAnnouncement(announced, view, { at, shown = false } = {}) {
  const kept = validHold(announced) ? announced : null;
  if (!view?.known) return { announced: kept, speak: false };
  const next = memoryHold(kept, view.level, at);
  if (shown && next === null && view.alert) return { announced: memoryHoldAt(view.level, at), speak: true };
  return { announced: next, speak: false };
}

// The banner keeps a user's label short so the level, the size and the suggestion stay in view; the Memory section
// keeps the full label (up to 80 characters).
const BANNER_LABEL = 32;
const bannerLabel = label => label.length > BANNER_LABEL ? `${label.slice(0, BANNER_LABEL - 1)}…` : label;

/**
 * The slim map banner: only for tight (amber) or critical (red, with "!") memory that has not been dismissed at this
 * level. It names the largest consumer with the figure the ranking used, and the first suggestion; the words always
 * say the level, so colour is never the only signal. Unknown, ok and watch show nothing.
 */
export function memoryBannerView(view, { dismissed = null } = {}) {
  const level = isMemoryLevel(view?.level) ? view.level : 'unknown';
  const held = validHold(dismissed) ? dismissed.level : null;
  if (!view?.alert || !isAlertLevel(level) || (held !== null && MEMORY_RANK[level] <= MEMORY_RANK[held])) return { show: false, level };
  const top = view.consumers[0], suggestion = view.suggestions[0];
  const detail = [top ? `${bannerLabel(top.label)} ${top.claim}` : view.reasons[0] || null, suggestion ? `Try: ${suggestion}` : null].filter(Boolean);
  const full = [top ? `${top.label} ${top.claim}` : view.reasons[0] || null, suggestion ? `Try: ${suggestion}` : null].filter(Boolean);
  return { show: true, level, mark: level === 'critical' ? '!' : null, title: view.word,
    text: detail.join(' · ') || 'Close apps you are not using.', full: full.join(' · ') || 'Close apps you are not using.',
    announce: `${view.word}.${top ? ` ${top.label} ${top.claim}.` : ''}${suggestion ? ` Try: ${suggestion}.` : ''}` };
}

/**
 * One time-ordered feed of what every agent sent where: Windows lane jobs from the dispatcher
 * journal and shared-router runs. Rows carry only recorded fields; nothing is inferred.
 * On a stale or paused feed an open job is only 'in-flight-stale': in flight at the last sample.
 */
export function activityFeed(jobs, runs, { limit = 8, feedFresh = true } = {}) {
  const rows = [];
  const pcRows = [...(Array.isArray(jobs?.inFlight) ? jobs.inFlight.map(row => [row, true]) : []),
    ...(Array.isArray(jobs?.recent) ? jobs.recent.map(row => [row, false]) : [])];
  for (const [row, open] of pcRows) {
    if (!row || !Number.isFinite(row.ageSeconds)) continue;
    const client = typeof row.client === 'string' && CLIENT_LABEL.test(row.client) ? row.client.toLowerCase() : null;
    // Only the in-flight list is running; a settled row keeps its recorded state (e.g. an invalid result).
    const state = open ? (feedFresh ? 'in-flight' : 'in-flight-stale')
      : typeof row.state === 'string' && /^[a-z-]{1,24}$/.test(row.state) ? row.state : 'unknown';
    rows.push({ key: `job:${row.id}`, kind: 'pc-job', client, probe: client === 'pc-llm-probe',
      target: LANE_TARGET[row.lane] || 'Windows PC', lane: LANE_TARGET[row.lane] ? row.lane : null, state,
      rate: state === 'success' ? rateText(row.predictedPerSecond) : null,
      speeds: state === 'success' ? speedsText(row.promptPerSecond, row.predictedPerSecond) : null,
      limitHit: !open && jobFlags(row).includes('hit-token-limit'), cancelRequested: open && row.cancelRequested === true,
      elapsed: Number.isFinite(row.elapsedSeconds) ? row.elapsedSeconds : null, ageSeconds: row.ageSeconds });
  }
  for (const run of Array.isArray(runs) ? runs : []) {
    if (!run || typeof run.runId !== 'string' || !Number.isFinite(run.ageSeconds)) continue;
    const client = typeof run.client === 'string' && CLIENT_LABEL.test(run.client) ? run.client.toLowerCase() : null;
    const review = reviewConsistencyView(run);
    const target = run.host === 'windows' ? 'Nisi Inference → PC' : run.host === 'mac' ? 'Nisi Inference → Mac' : 'Nisi Inference route';
    // A route the router lists as live (its run lock held, verified in the same sample) runs now, like
    // an in-flight PC job; a queued one waits for a lane. Everything else keeps its recorded status.
    const state = run.activity === 'running' ? (feedFresh ? 'in-flight' : 'in-flight-stale')
      : run.activity === 'queued' ? 'queued' : typeof run.status === 'string' ? run.status : 'unknown';
    rows.push({ key: `run:${run.source || 'route'}:${run.runId}`, kind: 'route', client, probe: false, target,
      lane: null, state, rate: null, speeds: null, limitHit: false,
      elapsed: null, ageSeconds: run.ageSeconds, runId: run.runId, review: review.level });
  }
  const seen = new Set();
  return rows.sort((a, b) => a.ageSeconds - b.ageSeconds).filter(row => !seen.has(row.key) && seen.add(row.key)).slice(0, limit);
}

/**
 * Four glanceable vitals for the status strip; every value is from the snapshot or marked unknown.
 * `activityLive` (fresh and not paused) is the only thing that lets the activity chip say "running".
 */
/** How a queued primary route reads: "queued for a lane" only when its note says it waits for a known lane;
 * a run in admission reads "being admitted"; anything else is just "queued" (never a guessed lane). */
export function routeQueuedWords(pipeline) {
  const lane = pipeline?.queuePhase === 'waiting'
    && ['mac-pair', 'pc-route', 'pc-lane-deep', 'pc-lane-fast'].includes(pipeline?.queueResource);
  return lane ? 'queued for a lane' : pipeline?.queuePhase === 'admitting' ? 'being admitted' : 'queued';
}

export function vitalsView({fresh = false, pcFresh = fresh, models = [], activityKnown = false, loadedKnown = false, lanes = null, worker = null,
  jobs = null, pipeline = null, nisi = null, feed = [], gpu = null, pcGpu = null, memory = null, activityLive = fresh } = {}) {
  const knownNow = activityKnown && activityLive;
  const activeModels = fresh && knownNow ? models.filter(m => m.loaded === true && ACTIVE_STATES.has(m.state)) : [];
  const active = activeModels.length, loaded = models.filter(m => m.loaded === true).length;
  const activityLabel = active ? `${active} ${activeModels.every(m => m.state === 'generating') ? 'generating' : activeModels.every(m => m.state === 'busy') ? 'busy' : 'working'}` : null;
  const mac = !fresh ? { tone: 'muted', detail: 'Signal stale' }
    : active ? { tone: 'live', detail: activityLabel }
      : { tone: knownNow ? 'ok' : 'muted', detail: `${loadedKnown ? `${loaded} loaded` : 'Loaded unknown'} · ${knownNow ? 'idle' : 'activity unknown'}` };
  // A fresh GPU sample joins the Mac chip after what the models are doing ("1 loaded · idle · GPU 46%"), so a
  // whole-GPU figure never reads as a model at work. The short chip keeps the state and the GPU ("Idle · GPU 46%").
  if (fresh && gpu?.known) {
    mac.short = `${activityLabel || (knownNow ? 'Idle' : 'Activity unknown')} · ${gpu.chip}`;
    mac.detail = `${mac.detail} · ${gpu.chip}`;
  }
  // Tight or critical memory (memoryView) adds its word: last in the long form, first in the short and tiny ones so an
  // ellipsis never cuts it. The chip turns warn unless a model is working; the word, not the colour, says why.
  const memoryWord = fresh && memory?.alert && typeof memory.word === 'string' ? memory.word : null;
  if (memoryWord) {
    if (mac.tone !== 'live') mac.tone = 'warn';
    mac.short = `${memoryWord} · ${activityLabel || (knownNow ? 'Idle' : 'Activity unknown')}${gpu?.known ? ` · ${gpu.chip}` : ''}`;
    mac.detail = `${mac.detail} · ${memoryWord}`;
  }
  const laneUp = lanes?.visible ? lanes.lanes.filter(lane => lane.up) : [];
  const fast = lanes?.visible ? lanes.lanes.find(lane => lane.id === 'fast') : null;
  // Like the Activity chip, only a live (fresh, unpaused) view pulses for a running job.
  const pcState = !fresh || !pcFresh ? 'muted' : jobs?.running && activityLive ? 'live' : ['degraded', 'stopped'].includes(worker?.state) ? 'warn'
    : worker?.state === 'advertised' ? 'ok' : 'muted';
  const pcDetail = !fresh ? 'Signal stale' : !pcFresh ? 'Worker heartbeat stale or unknown' : worker?.state === 'degraded' ? 'Worker degraded · no lane answers'
    : worker?.state === 'stopped' ? 'Worker stopped'
      : [lanes?.headless?.on ? `Headless ${lanes.headless.label.replace(/^Headless on · /, '').replace(/ left$/, '')}` : lanes?.headless?.label === 'Headless off' ? 'Headless off' : 'Headless unknown',
        lanes?.visible ? `${laneUp.length}/${lanes.lanes.length} lanes` : null,
        fast && Number.isFinite(fast.rate) ? `fast ${rateText(fast.rate)}` : null, pcGpu?.known ? pcGpu.text : null].filter(Boolean).join(' · ');
  // A 'block' review stopped its route (a defect); a 'warn' was only recorded for the owner.
  const reviewDefects = feed.filter(row => row.kind === 'route' && row.review === 'block').length;
  const reviewWarnings = feed.filter(row => row.kind === 'route' && row.review === 'warn').length;
  // Several routed tasks may run at once (router concurrency): the chip counts them.
  const runningRoutes = Array.isArray(pipeline?.pipelines) ? pipeline.pipelines.filter(p => p && p.live === true && p.status === 'running').length : 0;
  const route = !fresh ? { tone: 'muted', detail: 'Route age unknown' }
    : pipeline?.status === 'installing' ? { tone: 'warn', detail: 'Router install in progress' }
    : pipeline?.status === 'running' && runningRoutes > 1 ? { tone: 'live', detail: `${runningRoutes} routes running` }
    : pipeline?.status === 'running' ? { tone: 'live', detail: `Running · ${String(pipeline.stage || 'stage unknown').replaceAll('_', ' ')}` }
      : pipeline?.status === 'queued' ? { tone: 'live', detail: routeQueuedWords(pipeline).replace(/^./, s => s.toUpperCase()) }
      : pipeline?.runId ? { tone: 'warn', detail: 'Unsettled run recorded' }
        // A Nisi call left its pending record (a live direct call writes one too, so the words stay neutral).
        : pipeline?.status === 'recovery-required' || nisi?.state === 'unresolved' ? { tone: 'warn', detail: 'Nisi record pending' }
        : { tone: reviewDefects ? 'warn' : nisi?.state === 'ready' ? 'ok' : 'muted', detail: [pipeline?.status === 'idle' ? 'Idle' : 'State unknown',
          nisi?.state === 'ready' ? 'pair ready' : nisi?.state === 'partial' ? 'needs 2nd model' : null,
          reviewDefects ? `${reviewDefects} review defect${reviewDefects === 1 ? '' : 's'}` : null,
          reviewWarnings ? `${reviewWarnings} review warning${reviewWarnings === 1 ? '' : 's'}` : null].filter(Boolean).join(' · ') };
  const headlessShort = lanes?.headless?.on ? `On ${lanes.headless.label.replace(/^Headless on · /, '').replace(/ left$/, '')}`
    : lanes?.headless?.label === 'Headless off' ? 'Headless off' : 'Headless unknown';
  const pcShort = !fresh ? 'Stale' : !pcFresh ? 'Heartbeat stale' : worker?.state === 'degraded' ? 'Degraded' : worker?.state === 'stopped' ? 'Stopped'
    : [headlessShort, fast && Number.isFinite(fast.rate) ? `fast ${rateText(fast.rate)}` : null].filter(Boolean).join(' · ');
  const last = feed.find(row => !row.probe) || feed[0];
  // Only a fresh, unpaused feed can say a job is running now; otherwise it was in flight at the last sample.
  const open = last && (last.state === 'in-flight' || last.state === 'in-flight-stale');
  const running = open && fresh && activityLive && last.state === 'in-flight';
  const activity = !last ? { tone: 'muted', detail: 'No recorded activity' }
    : { tone: running ? 'live' : open || last.state === 'cancelled' ? 'muted' : FAILED_STATE.test(last.state) ? 'warn' : 'ok',
      detail: [last.probe ? 'switch probe' : last.client || 'unknown client', '→', last.target,
        running ? 'running' : open ? 'in flight at last sample' : last.state === 'cancelled' ? 'cancelled' : last.rate].filter(Boolean).join(' ') };
  const short = { ...activity, detail: !last ? 'None yet' : [last.probe ? 'probe' : last.client || 'unknown', '→', last.target.replace('Nisi Inference → ', '').replace('Nisi Inference route', 'route')].join(' ') };
  // Tiny forms: what each chip still says when even its short form does not fit (the app steps down to them).
  const macTiny = activityLabel || (knownNow ? 'Idle' : 'Activity unknown');
  const tiny = !fresh ? {} : { mac: memoryWord ? `${memoryWord} · ${macTiny}` : macTiny,
    pc: !pcFresh ? 'Heartbeat stale' : ['degraded', 'stopped'].includes(worker?.state) ? pcShort : fast && Number.isFinite(fast.rate) ? `fast ${rateText(fast.rate)}` : headlessShort,
    route: route.detail.split(' · ')[0] };
  // Micro form (the popover can be 360 px wide): tight or critical memory keeps only its words on the Mac chip.
  const micro = fresh && memoryWord ? { mac: memoryWord } : {};
  return { mac, pc: { tone: pcState, detail: pcDetail, short: pcShort }, route, activity: { ...activity, short: short.detail },
    lastAgeSeconds: last ? last.ageSeconds : null, tiny, micro };
}

/**
 * The Live activity panel's opening summary: one plain status line, one sentence of meaning and
 * four counts over the rows it lists. Only a live (fresh, unpaused) feed can call a job running now.
 */
export function activitySummary(feed, { live = false } = {}) {
  const rows = Array.isArray(feed) ? feed : [];
  const running = rows.filter(row => row.state === 'in-flight').length;
  const open = running + rows.filter(row => row.state === 'in-flight-stale').length;
  const failed = rows.filter(row => FAILED_STATE.test(row.state)).length;
  const answered = rows.filter(row => row.kind === 'pc-job' && row.state === 'success').length;
  const routes = rows.filter(row => row.kind === 'route').length;
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const newest = rows.find(row => !row.probe) || rows[0];
  // The meaning names the job it is about: the newest running one, or the newest failure.
  const job = rows.find(row => row.state === 'in-flight' && !row.probe) || rows.find(row => row.state === 'in-flight');
  const lane = row => row.lane ? `the PC ${row.lane} lane` : row.kind === 'route' ? row.target : 'the PC';
  const waiting = job ? `${capitalize(who(job))} ${job.probe ? 'is' : 'is waiting on'} ${job.probe ? `probing ${lane(job)}` : lane(job)} (${Math.floor(job.ageSeconds)} s).${running > 1 ? ` ${running - 1} more running.` : ''}` : '';
  const failure = row => /invalid/i.test(row.state) ? 'had its answer rejected as invalid' : /timeout/i.test(row.state) ? 'timed out'
    : /error/i.test(row.state) ? 'ended in an error' : /reject/i.test(row.state) ? 'was rejected' : 'did not pass';
  // Live routes (router concurrency) run beside PC jobs; each is counted under its own name.
  const count = states => {
    const matching = rows.filter(row => states.includes(row.state)), routed = matching.filter(row => row.kind === 'route').length;
    return [matching.length - routed ? plural(matching.length - routed, 'job') : null, routed ? plural(routed, 'route') : null].filter(Boolean).join(' and ');
  };
  const summary = !rows.length ? { tone: 'muted', line: 'Nothing recorded yet', meaning: 'PC jobs and Nisi Inference routes appear here as agents send them.' }
    : running && live ? { tone: 'live', line: `${count(['in-flight'])} running now`, meaning: waiting }
      : open ? { tone: 'muted', line: `${count(['in-flight', 'in-flight-stale'])} in flight at the last sample`, meaning: 'The feed is paused or stale, so current progress is unknown.' }
        : FAILED_STATE.test(newest.state) ? { tone: 'warn', line: 'The latest job did not answer',
          meaning: `${capitalize(who(newest))} → ${newest.target} ${failure(newest)}. Select it to open it on the map.` }
          : { tone: 'ok', line: 'Nothing running now', meaning: 'Select a row to open its lane or route on the map.' };
  return { ...summary, tiles: [['In flight', String(open)], ['Answered', String(answered)], ['Failed', String(failed)], ['Routes', String(routes)]] };
}

/* Orb web (spiderweb, 26-27 Sep 2026: the judged "classic orb" variant with the judge's grafts and fixes, then the
 * 27 Sep visual review). A neutral web drawn under the constellation: straight spokes from each hub toward its
 * children (children in one direction share a spoke that runs to the farthest of them), fainter filler spokes so no
 * gap between neighbouring spokes exceeds 45 degrees (90 around the small client and route hubs), and capture rings
 * whose segments sag toward the hub between spokes (the orb-web scallop, 10% of the chord): three around the Mac, the
 * PC and a trace run, one tight ring around each client branch and the route, inside their captions.
 * Every line on the map beyond a hub's own web is evidence: a spoke runs the whole way to its child only under a solid
 * data edge. Under a dashed ("recorded / advertised" or in-flight) edge it stops at the hub's first ring, so the web
 * never fills the dashes' gaps, and a child hub's spoke back to its parent is always that short stub, so no thread is
 * drawn twice. Bridge threads between hub webs (a quadratic sag, the parabolic approximation of a shallow catenary)
 * are off by default (maxBridges 0): the review found them reading as links between unrelated nodes.
 * Pure and deterministic: the same nodes and edges give the same strings, every coordinate written is
 * finite, and the path count is bounded. It never adds a thread between two leaves.
 * Every thread is a quadratic Bezier (spokes and bridges "M Q", rings "M Q.. Z"), so a pluck only moves
 * control points: a moving path keeps its rest path's command structure, and at zero displacement it is the
 * rest path, character for character. */
export const ORB_WEB_LIMITS = Object.freeze({ hubs: 10, spokesPerHub: 24, rings: 6, bridges: 6, dew: 24, strands: 400, glows: 12 });
const WEB_HUB_KINDS = { runtime: 'major', run: 'major', 'windows-worker': 'major', pipeline: 'minor', client: 'minor' };
const WEB_TAU = Math.PI * 2, WEB_DEG = Math.PI / 180;
const webNum = value => { const rounded = Math.round(value * 10) / 10; return Object.is(rounded, -0) ? '0' : String(rounded); };
const webPoint = ([x, y]) => `${webNum(x)} ${webNum(y)}`;
const webNode = node => Boolean(node) && typeof node.id === 'string' && Number.isFinite(node.x) && Number.isFinite(node.y) && Math.abs(node.x) <= 1e6 && Math.abs(node.y) <= 1e6;
const webRadius = node => Number.isFinite(node?.r) && node.r >= 0 && node.r <= 200 ? node.r : 7;
const webTurn = angle => ((angle % WEB_TAU) + WEB_TAU) % WEB_TAU;
const webAngleGap = (a, b) => { const d = webTurn(a - b); return Math.min(d, WEB_TAU - d); };
const webAlong = (from, angle, distance) => [from.x + Math.cos(angle) * distance, from.y + Math.sin(angle) * distance];
const webCount = (value, low, high, fallback) => Number.isFinite(value) ? Math.max(low, Math.min(high, Math.round(value))) : fallback;
const webNodes = nodes => { const byId = new Map(); for (const node of Array.isArray(nodes) ? nodes : []) if (webNode(node) && !byId.has(node.id)) byId.set(node.id, node); return byId; };
// The control point of a thread from a to b pushed `offset` off its chord (the curve's midpoint moves half as far), plus a downward sag.
const webBend = (a, b, offset, sag = 0) => { const dx = b[0] - a[0], dy = b[1] - a[1], length = Math.hypot(dx, dy) || 1;
  return [(a[0] + b[0]) / 2 - dy / length * 2 * offset, (a[1] + b[1]) / 2 + dx / length * 2 * offset + 2 * sag]; };
function webSpokesPath(hub, filler, offsets) {
  let d = '';
  // A spoke back to the parent hub lies on the parent's own spoke, drawn the other way: it bends the other way round so the two move as one thread.
  hub.spokes.forEach((spoke, i) => { if (spoke.filler === filler && spoke.to) d += `M${webPoint(spoke.from)}Q${webPoint(webBend(spoke.from, spoke.to, (offsets?.get(i) || 0) * (spoke.back ? -1 : 1)))} ${webPoint(spoke.to)}`; });
  return d;
}
// A quadratic's midpoint sits halfway to its control point: pulling the control 2s toward the hub sags the thread by s.
// `offset` deepens (or, negative, relaxes) every scallop of the ring by that much.
function webRingPath(hub, level, offset = 0) {
  const radius = hub.radii[level], all = hub.spokes, points = all.map(spoke => webAlong(hub, spoke.angle, radius));
  return `M${webPoint(points[0])}` + all.map((spoke, i) => {
    const j = (i + 1) % all.length, end = j ? all[j].angle : all[0].angle + WEB_TAU, mid = (spoke.angle + end) / 2;
    const [x0, y0] = points[i], [x1, y1] = points[j], pull = 2 * (hub.scallop * Math.hypot(x1 - x0, y1 - y0) + offset);
    return `Q${webPoint([(x0 + x1) / 2 - Math.cos(mid) * pull, (y0 + y1) / 2 - Math.sin(mid) * pull])} ${webPoint(points[j])}`;
  }).join('') + 'Z';
}
const webBridgePath = (bridge, offset = 0) => `M${webPoint(bridge.a)}Q${webPoint(webBend(bridge.a, bridge.b, offset, bridge.sag))} ${webPoint(bridge.b)}`;
const webRingClass = (level, count) => level === 0 ? 'ring-in' : level === count - 1 ? 'ring-out' : 'ring-mid';

/** The web's cache key: node ids, kinds, positions and radii plus the edge topology and which edges are dashed
 *  (a spoke under a dashed edge is short). Status is left out, so a hover or a stream tick that moves nothing
 *  reuses the web that is already drawn. */
const webDashed = edge => Boolean(edge && (edge.dim || edge.flow));
export function orbWebKey(nodes, edges) {
  const n = (Array.isArray(nodes) ? nodes : []).map(node => node ? `${node.id}|${node.kind}|${webNum(Number(node.x))}|${webNum(Number(node.y))}|${webNum(webRadius(node))}` : '-');
  const e = (Array.isArray(edges) ? edges : []).map(edge => edge ? `${edge.a}>${edge.b}${webDashed(edge) ? '~' : ''}` : '-');
  return `${n.join(';')}#${e.join(';')}`;
}

export function orbWebLayout(nodes, edges, { ringsMajor = 3, ringsMinor = 1, scallop = .1, scallopMinor = .06, fillerGap = 45, fillerGapMinor = 60, minorRing = 12, merge = 6, fillerReach = 1.12, bridgeSag = .06, bridgeMin = 25, bridgeMax = 150, maxBridges = 0 } = {}) {
  const byId = webNodes(nodes), edgeList = Array.isArray(edges) ? edges : [];
  const links = edgeList.filter(edge => edge && edge.a !== edge.b && byId.has(edge.a) && byId.has(edge.b));
  const dashed = new Set(links.filter(webDashed).map(edge => `${edge.a}>${edge.b}`));
  const hubs = [...byId.values()].filter(node => Object.hasOwn(WEB_HUB_KINDS, node.kind)).slice(0, ORB_WEB_LIMITS.hubs);
  const hubIds = new Set(hubs.map(hub => hub.id));
  // A node belongs to the first hub that links to it, in graph order: a PC lane is the PC's child even
  // though client branches also link to it, so those cross links stay curved data edges, not spokes.
  const parent = new Map();
  for (const edge of links) if (hubIds.has(edge.a) && !parent.has(edge.b)) parent.set(edge.b, edge.a);
  const primary = hubs.find(hub => hub.kind === 'runtime') || hubs.find(hub => hub.kind === 'run') || hubs.find(hub => !parent.has(hub.id)) || null;
  const ratio = (value, fallback) => Number.isFinite(value) ? Math.max(0, Math.min(.3, value)) : fallback;
  const step = (value, fallback) => (Number.isFinite(value) ? Math.max(15, Math.min(120, value)) : fallback) * WEB_DEG;
  const scallopRatio = { major: ratio(scallop, .1), minor: ratio(scallopMinor, .06) };
  const fillerStep = { major: step(fillerGap, 45), minor: step(fillerGapMinor, 60) };
  const minorGap = Number.isFinite(minorRing) ? Math.max(10, Math.min(30, minorRing)) : 12;
  const mergeStep = (Number.isFinite(merge) ? Math.max(0, Math.min(20, merge)) : 6) * WEB_DEG;
  const reachRatio = Number.isFinite(fillerReach) ? Math.max(1, Math.min(1.3, fillerReach)) : 1.12;
  const occluded = new Set(), webs = [];
  for (const hub of hubs) {
    const hr = webRadius(hub), size = WEB_HUB_KINDS[hub.kind], major = size === 'major';
    const ids = new Set([...parent].filter(([, owner]) => owner === hub.id).map(([id]) => id));
    if (parent.has(hub.id)) ids.add(parent.get(hub.id));
    ids.delete(hub.id);
    const targets = [...ids].map(id => byId.get(id)).map(node => ({ id: node.id, r: webRadius(node),
      angle: webTurn(Math.atan2(node.y - hub.y, node.x - hub.x)), distance: Math.hypot(node.x - hub.x, node.y - hub.y) }))
      .filter(target => target.distance > hr + target.r + 2)
      .sort((a, b) => a.angle - b.angle || a.distance - b.distance || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    // Targets within `merge` degrees share one spoke that runs to the farthest of them.
    const groups = [];
    for (const target of targets) { const last = groups[groups.length - 1]; if (last && target.angle - last[last.length - 1].angle < mergeStep) last.push(target); else groups.push([target]); }
    if (groups.length > 1 && groups[0][0].angle + WEB_TAU - groups[groups.length - 1].at(-1).angle < mergeStep) groups[0] = [...groups.pop(), ...groups[0]];
    const grouped = groups.map(group => {
      const far = group.reduce((a, b) => b.distance > a.distance ? b : a), near = group.reduce((a, b) => b.distance < a.distance ? b : a);
      return { angle: far.angle, target: far.id, targets: group.map(target => target.id), near: near.id, reach: far.distance - far.r - 1 };
    }).sort((a, b) => a.angle - b.angle);
    const spokes = grouped.slice(0, ORB_WEB_LIMITS.spokesPerHub);
    // Only the nearest child of a shared spoke runs straight along it; the others (and any spoke past the limit) keep their bends.
    for (const spoke of grouped) for (const id of spoke.targets) if (!spokes.includes(spoke) || id !== spoke.near) occluded.add(`${hub.id}>${id}`);
    const fillers = [], fillerAt = fillerStep[size], alone = Math.max(3, Math.ceil(WEB_TAU / fillerAt - 1e-9));
    if (!spokes.length) for (let i = 0; i < alone; i++) fillers.push(webTurn(-Math.PI / 2 + i * WEB_TAU / alone));
    else spokes.forEach((spoke, i) => {
      const gap = i === spokes.length - 1 ? spokes[0].angle + WEB_TAU - spoke.angle : spokes[i + 1].angle - spoke.angle;
      const parts = Math.ceil(gap / fillerAt - 1e-9);
      for (let k = 1; k < parts; k++) fillers.push(webTurn(spoke.angle + gap * k / parts));
    });
    const all = [...spokes.map(spoke => ({ angle: spoke.angle, spoke })),
      ...fillers.slice(0, Math.max(0, ORB_WEB_LIMITS.spokesPerHub - spokes.length)).map(angle => ({ angle, spoke: null }))].sort((a, b) => a.angle - b.angle);
    // Ring size: well inside the nearest child and short of halfway to the nearest other hub, so neighbouring webs
    // never overlap. Minor hubs (client branches, the route) get one tight ring `minorRing` units out from the star,
    // inside the caption that hangs below it (a client's name starts about 22 units down).
    const nearTarget = Math.min(Infinity, ...targets.map(target => target.distance - target.r));
    const nearHub = Math.min(Infinity, ...hubs.filter(other => other !== hub).map(other => Math.hypot(other.x - hub.x, other.y - hub.y)));
    const rings = webCount(major ? ringsMajor : ringsMinor, 1, ORB_WEB_LIMITS.rings, major ? 3 : 1), inner = hr + (major ? 11 : minorGap);
    const outer = Math.max(inner + (rings - 1) * (major ? 9 : 6), Math.min(.58 * nearTarget, .46 * nearHub, major ? 130 : inner + (rings - 1) * 6));
    const radii = Array.from({ length: rings }, (_, i) => rings === 1 ? outer : inner + (outer - inner) * i / (rings - 1));
    const start = hr + 3;
    const entries = all.map(entry => {
      const spoke = entry.spoke, back = Boolean(spoke && parent.has(hub.id) && spoke.targets.includes(parent.get(hub.id)));
      // Only a spoke under a solid data edge runs to its child. Under a dashed edge, and back to the parent hub (whose own
      // spoke, or the dashed edge, already carries that line), it stops at the first ring.
      const end = !spoke ? outer * reachRatio : back || dashed.has(`${hub.id}>${spoke.near}`) ? Math.min(spoke.reach, radii[0]) : spoke.reach, drawn = end > start + 1;
      return { angle: entry.angle, filler: !spoke, target: spoke ? spoke.target : null, targets: spoke ? spoke.targets : [], back,
        from: drawn ? webAlong(hub, entry.angle, start) : null, to: drawn ? webAlong(hub, entry.angle, end) : null };
    });
    webs.push({ id: hub.id, kind: hub.kind, parent: parent.get(hub.id) ?? null, major, x: hub.x, y: hub.y, r: hr, inner, outer, radii, scallop: scallopRatio[size], spokes: entries });
  }
  // Bridges (off by default) tie neighbouring hub webs around the primary hub (outer ring to outer ring), longest first.
  const bridges = [];
  if (primary) {
    const webById = new Map(webs.map(web => [web.id, web]));
    const attach = (node, toward) => {
      const angle = Math.atan2(toward.y - node.y, toward.x - node.x), web = webById.get(node.id);
      if (!web) return webAlong(node, angle, webRadius(node) + 5);
      const spoke = web.spokes.reduce((best, s) => webAngleGap(s.angle, angle) < webAngleGap(best.angle, angle) ? s : best);
      return webAlong(node, spoke.angle, web.outer);
    };
    const around = hubs.filter(hub => hub !== primary).map(node => ({ node, angle: webTurn(Math.atan2(node.y - primary.y, node.x - primary.x)), distance: Math.hypot(node.x - primary.x, node.y - primary.y) }))
      .filter(entry => entry.distance > 1).sort((a, b) => a.angle - b.angle || a.distance - b.distance);
    const pairs = around.length < 2 ? [] : around.length === 2 ? [[around[0], around[1]]] : around.map((entry, i) => [entry, around[(i + 1) % around.length]]);
    const candidates = [];
    for (const [from, to] of pairs) {
      const gap = (around.length === 2 ? webAngleGap(to.angle, from.angle) : webTurn(to.angle - from.angle)) / WEB_DEG;
      if (gap < bridgeMin || gap > bridgeMax) continue;
      const a = attach(from.node, to.node), b = attach(to.node, from.node), chord = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (!(chord > 8)) continue;
      const sag = (Number.isFinite(bridgeSag) ? Math.max(0, Math.min(.2, bridgeSag)) : .06) * chord;
      candidates.push({ from: from.node.id, to: to.node.id, chord, a, b, sag });
    }
    candidates.sort((a, b) => b.chord - a.chord || (a.from < b.from ? -1 : a.from > b.from ? 1 : 0));
    bridges.push(...candidates.slice(0, webCount(maxBridges, 0, ORB_WEB_LIMITS.bridges, 0)));
  }
  // Spoke edges draw straight along their spoke; one hidden behind a nearer sibling keeps its bend.
  const edgeBends = edgeList.map(edge => edge && hubIds.has(edge.a) && parent.get(edge.b) === edge.a && !occluded.has(`${edge.a}>${edge.b}`) ? 0 : null);
  // Draw order per hub: capture rings (fading outward), then filler spokes, then spokes; the bridges last. A minor hub's
  // threads also carry web-minor, so the stylesheet can drop those small webs when the map is zoomed far out.
  const paths = [];
  for (const web of webs) {
    const minor = web.major ? '' : ' web-minor';
    web.radii.forEach((_, level) => paths.push({ key: `ring:${web.id}:${level}`, className: `web-ring ${webRingClass(level, web.radii.length)}${minor}`, d: webRingPath(web, level) }));
    const fillers = webSpokesPath(web, true), spokes = webSpokesPath(web, false);
    if (fillers) paths.push({ key: `fillers:${web.id}`, className: `web-filler${minor}`, d: fillers });
    if (spokes) paths.push({ key: `spokes:${web.id}`, className: `web-spoke${minor}`, d: spokes });
  }
  bridges.forEach((bridge, i) => paths.push({ key: `bridge:${i}`, className: 'web-bridge', d: webBridgePath(bridge) }));
  return { primary: primary ? primary.id : null, hubs: webs, bridges, edgeBends, paths, pathCount: paths.length };
}

/** Reduced motion only: static dew on a web's outermost ring where a spoke leads to a live node, or where a live
 *  hub's work arrived (its spoke back to its parent hub), so a PC job in flight marks Mac -> PC -> busy lane and
 *  never an idle lane. With motion allowed there is no dew and no travelling cue at all: the 27 Sep review removed
 *  the gliding drop (the costliest part of the web, mostly hidden under edges and stars), and the halo, glow and
 *  heartbeat already say a job is in flight. The caller decides what is live now (fresh and unpaused) and whether
 *  motion is allowed (no reduced motion). */
export function orbWebActivity(web, options) {
  const { live = [], motion = false } = options || {}, liveIds = new Set(Array.isArray(live) ? live : []), dew = [];
  if (motion !== true) for (const hub of Array.isArray(web?.hubs) ? web.hubs : []) for (const spoke of hub.spokes) {
    if (dew.length >= ORB_WEB_LIMITS.dew) break;
    if (!spoke.filler && (spoke.targets.some(id => liveIds.has(id)) || (liveIds.has(hub.id) && hub.parent !== null && spoke.targets.includes(hub.parent)))) dew.push(webAlong(hub, spoke.angle, hub.outer));
  }
  // A dot is a near-zero-length round-capped segment ("h.01", not "h0", which some engines skip with non-scaling strokes).
  const dewPath = dew.map(point => `M${webPoint(point)}h.01`).join('');
  return { dew, dewPath, key: dewPath };
}

/* Web motion (Louis 26 Sep: "like when you poke a spiderweb it bounces"). A poke plucks the threads touching a
 * node with a damped wobble perpendicular to each thread, A·e^(-t/τ)·sin(2πft) with f 3 Hz and τ .35 s over
 * 1.2 s, and the wave travels outward: one hop (the hub's capture rings and the threads at the far ends) 90 ms
 * later at 45%, a second hop at 20%. The poked star dips and springs back. Each 2 s heartbeat of a working or
 * in-flight node gives its own threads a third of that, shorter. WKWebView cannot animate a path's `d` from CSS,
 * so app.js runs a requestAnimationFrame loop that writes only the affected paths and stops once settled; these
 * pure helpers give it each frame. */
export const WEB_PLUCK = Object.freeze({ amplitude: 7, frequency: 3, tau: .35, duration: 1.2, hopDelay: .09, ringStep: .035,
  falloff: Object.freeze([1, .45, .2]), beat: Object.freeze({ share: 1 / 3, tau: .25, duration: .9 }), period: 2000 });

/** Displacement of a plucked thread `t` seconds after its pluck: 0 before it and from `duration` on, never
 *  more than `amplitude`, with a cosine taper over the last quarter so it lands on rest without a step. */
export function pluckWave(t, amplitude, { frequency = WEB_PLUCK.frequency, tau = WEB_PLUCK.tau, duration = WEB_PLUCK.duration } = {}) {
  if (!(t > 0) || !(t < duration) || !Number.isFinite(amplitude) || !(tau > 0) || !Number.isFinite(frequency)) return 0;
  const taper = t > duration * .75 ? Math.cos((t - duration * .75) / (duration * .25) * Math.PI / 2) ** 2 : 1;
  return amplitude * Math.exp(-t / tau) * Math.sin(2 * Math.PI * frequency * t) * taper;
}

/** The poked star's scale: down to .92, overshoot to 1.04, rest at 1 by .5 s (smoothstep between keys). */
const DIP_KEYS = [[0, 1], [.09, .92], [.26, 1.04], [.5, 1]];
export function pokeScale(t) {
  if (!(t > 0) || !(t < DIP_KEYS.at(-1)[0])) return 1;
  const i = DIP_KEYS.findIndex(([at]) => at > t), [t0, s0] = DIP_KEYS[i - 1], [t1, s1] = DIP_KEYS[i], u = (t - t0) / (t1 - t0);
  return s0 + (s1 - s0) * u * u * (3 - 2 * u);
}

/** Which threads a pluck of `ids` moves, when and how far. Hop 0 is every thread touching a plucked node,
 *  hop 1 the capture rings of a plucked hub and the threads at the far ends, hop 2 one step further; a beat
 *  (the heartbeat of working nodes) moves hop 0 only, at a third of the amplitude. Amplitudes are also capped
 *  by thread length (8% of a spoke or edge, 5% of a bridge) and ring spacing, so nothing kinks or crosses. */
export function orbWebPluck(web, nodes, edges, ids, { beat = false, amplitude = WEB_PLUCK.amplitude, hops = 2 } = {}) {
  const byId = webNodes(nodes), hubs = Array.isArray(web?.hubs) ? web.hubs : [], bridges = Array.isArray(web?.bridges) ? web.bridges : [];
  const hubIds = new Set(hubs.map(hub => hub.id));
  const sources = [...new Set((Array.isArray(ids) ? ids : [ids]).filter(id => typeof id === 'string' && (byId.has(id) || hubIds.has(id))))];
  const maxHop = beat ? 0 : webCount(hops, 0, 2, 2);
  const base = (Number.isFinite(amplitude) ? Math.max(0, Math.min(2 * WEB_PLUCK.amplitude, amplitude)) : WEB_PLUCK.amplitude) * (beat ? WEB_PLUCK.beat.share : 1);
  const threads = [];
  for (const hub of hubs) hub.spokes.forEach((spoke, index) => { if (spoke.to) threads.push({ kind: 'spoke', hub: hub.id, index, ends: [hub.id, ...spoke.targets], cap: .08 * Math.hypot(spoke.to[0] - spoke.from[0], spoke.to[1] - spoke.from[1]) }); });
  bridges.forEach((bridge, index) => threads.push({ kind: 'bridge', index, ends: [bridge.from, bridge.to], cap: .05 * bridge.chord }));
  (Array.isArray(edges) ? edges : []).forEach((edge, index) => { const a = byId.get(edge?.a), b = byId.get(edge?.b); if (a && b && a !== b) threads.push({ kind: 'edge', index, ends: [a.id, b.id], cap: .08 * Math.hypot(b.x - a.x, b.y - a.y) }); });
  const reach = new Map(sources.map(id => [id, 0])), hopOf = new Map();
  for (let hop = 0; hop <= maxHop; hop++) {
    threads.forEach((thread, k) => { if (!hopOf.has(k) && thread.ends.some(id => reach.get(id) === hop)) hopOf.set(k, hop); });
    threads.forEach((thread, k) => { if (hopOf.get(k) === hop) for (const id of thread.ends) if (!reach.has(id)) reach.set(id, hop + 1); });
  }
  const strands = [];
  threads.forEach((thread, k) => {
    const hop = hopOf.get(k); if (hop === undefined) return;
    const amp = Math.min(base * WEB_PLUCK.falloff[hop], thread.cap);
    if (amp >= .05) strands.push({ kind: thread.kind, ...(thread.hub ? { hub: thread.hub } : {}), index: thread.index, hop, delay: Math.round(hop * WEB_PLUCK.hopDelay * 1000) / 1000, amplitude: amp });
  });
  for (const hub of hubs) {
    const at = reach.get(hub.id); if (at === undefined || at + 1 > maxHop) continue;
    const gap = Math.min(hub.inner - hub.r, ...hub.radii.slice(1).map((radius, i) => radius - hub.radii[i]));
    hub.radii.forEach((_, level) => { const amp = Math.min(base * WEB_PLUCK.falloff[at + 1] * .5, .4 * gap);
      if (amp >= .05) strands.push({ kind: 'ring', hub: hub.id, level, hop: at + 1, delay: Math.round(((at + 1) * WEB_PLUCK.hopDelay + level * WEB_PLUCK.ringStep) * 1000) / 1000, amplitude: amp }); });
  }
  const bounded = strands.slice(0, ORB_WEB_LIMITS.strands);
  const wave = beat ? { frequency: WEB_PLUCK.frequency, tau: WEB_PLUCK.beat.tau, duration: WEB_PLUCK.beat.duration } : { frequency: WEB_PLUCK.frequency, tau: WEB_PLUCK.tau, duration: WEB_PLUCK.duration };
  return { nodes: sources, beat: Boolean(beat), dip: !beat && sources.length ? sources[0] : null, wave, strands: bounded,
    duration: Math.max(0, ...bounded.map(strand => strand.delay)) + wave.duration };
}

/** Every displacement at `time` (ms) from the running impulses ({plan, start} with start in ms), summed per
 *  thread: spokes by hub and spoke index, rings by "hub:level", bridges and edges by index, and the poked
 *  star's scale. `active` stays true until the last impulse has fully settled. */
export function pluckOffsets(impulses, time) {
  const out = { spokes: new Map(), rings: new Map(), bridges: new Map(), edges: new Map(), scale: new Map(), active: false };
  for (const impulse of Array.isArray(impulses) ? impulses : []) {
    const plan = impulse?.plan;
    if (!plan || !Array.isArray(plan.strands) || !Number.isFinite(impulse.start) || !Number.isFinite(time)) continue;
    const t = (time - impulse.start) / 1000;
    if (t < plan.duration) out.active = true;
    for (const strand of plan.strands) {
      const value = pluckWave(t - strand.delay, strand.amplitude, plan.wave);
      if (!value) continue;
      if (strand.kind === 'spoke') { const map = out.spokes.get(strand.hub) || new Map(); map.set(strand.index, (map.get(strand.index) || 0) + value); out.spokes.set(strand.hub, map); }
      else if (strand.kind === 'ring') { const key = `${strand.hub}:${strand.level}`; out.rings.set(key, (out.rings.get(key) || 0) + value); }
      else if (strand.kind === 'bridge' || strand.kind === 'edge') { const map = out[`${strand.kind}s`]; map.set(strand.index, (map.get(strand.index) || 0) + value); }
    }
    if (plan.dip) { const scale = pokeScale(t); if (scale !== 1) out.scale.set(plan.dip, scale); }
  }
  return out;
}

/** The web paths that `offsets` moves, keyed like orbWebLayout's paths, each with its rest path's commands. */
export function orbWebPathsAt(web, offsets) {
  const out = new Map();
  if (!offsets) return out;
  for (const hub of Array.isArray(web?.hubs) ? web.hubs : []) {
    const spokes = offsets.spokes?.get(hub.id);
    if (spokes?.size) for (const filler of [false, true]) if (hub.spokes.some((spoke, i) => spoke.filler === filler && spoke.to && spokes.get(i))) out.set(`${filler ? 'fillers' : 'spokes'}:${hub.id}`, webSpokesPath(hub, filler, spokes));
    hub.radii.forEach((_, level) => { const offset = offsets.rings?.get(`${hub.id}:${level}`); if (offset) out.set(`ring:${hub.id}:${level}`, webRingPath(hub, level, offset)); });
  }
  (Array.isArray(web?.bridges) ? web.bridges : []).forEach((bridge, i) => { const offset = offsets.bridges?.get(i); if (offset) out.set(`bridge:${i}`, webBridgePath(bridge, offset)); });
  return out;
}

/** A map edge's quadratic, its control point pushed `offset` off the chord; with no offset it is exactly the
 *  path renderGraph has always drawn. */
export function edgeCurvePath(g, offset = 0) {
  if (!offset || !Number.isFinite(offset)) return `M${g.sx} ${g.sy} Q${g.cx} ${g.cy} ${g.ex} ${g.ey}`;
  const dx = g.ex - g.sx, dy = g.ey - g.sy, length = Math.hypot(dx, dy) || 1;
  return `M${g.sx} ${g.sy} Q${g.cx - dy / length * 2 * offset} ${g.cy + dx / length * 2 * offset} ${g.ex} ${g.ey}`;
}

/** Reduced motion or a paused feed: a poke lights the threads it would have plucked (hop 0 only), at rest. */
export function orbWebFlashPath(web, plan) {
  const hubs = new Map((Array.isArray(web?.hubs) ? web.hubs : []).map(hub => [hub.id, hub]));
  let d = '';
  for (const strand of Array.isArray(plan?.strands) ? plan.strands : []) {
    if (strand.hop !== 0) continue;
    if (strand.kind === 'spoke') { const spoke = hubs.get(strand.hub)?.spokes[strand.index]; if (spoke?.to) d += `M${webPoint(spoke.from)}L${webPoint(spoke.to)}`; }
    else if (strand.kind === 'bridge' && web.bridges?.[strand.index]) d += webBridgePath(web.bridges[strand.index]);
  }
  return d;
}

/** The motion loop, with its clock and schedulers injected. poke(plan) restarts the single poke (never stacks);
 *  pulse(plan) arms a timer for the next heartbeat boundary (multiples of `period` ms on the same clock as the
 *  halo and glow animations), where the beat plan starts. A frame is requested only while an impulse is
 *  running; the last frame draws rest (draw(null)). pulse(null) disarms; stop() also cancels and draws rest.
 *  Nothing runs at rest: no frame and no timer without an impulse or a pulse plan. The heartbeat alone (about a
 *  pixel of wobble, for as long as a job runs) draws at most `beatFps` frames a second, whatever the display rate;
 *  a poke draws every frame. */
export function createWebMotion({ now, requestFrame, cancelFrame = () => {}, setTimer, clearTimer = () => {}, draw, period = WEB_PLUCK.period, beatFps = 30 } = {}) {
  let poke = null, beat = null, pulse = null, pending = false, frameId = null, timerId = null, lastBeat = -Infinity, lastDraw = -Infinity, frames = 0, draws = 0, beats = 0;
  // A frame due within 2 ms of the interval still draws, so a 60 Hz display keeps an even 30 fps rather than skipping to 20.
  const beatGap = Number.isFinite(beatFps) && beatFps > 0 ? 1000 / beatFps - 2 : 0;
  const impulses = () => [poke, beat].filter(Boolean);
  function settle(time) {
    if (poke && (time - poke.start) / 1000 >= poke.plan.duration) poke = null;
    if (beat && (time - beat.start) / 1000 >= beat.plan.duration) beat = null;
  }
  function step() {
    pending = false; frameId = null; frames++;
    const time = now(); settle(time);
    const running = impulses();
    if (running.length && !poke && time - lastDraw < beatGap) { run(); return; }
    lastDraw = running.length ? time : -Infinity; draws++;
    draw(running.length ? { impulses: running, time } : null);
    if (running.length) run();
  }
  function run() { if (!pending) { pending = true; frameId = requestFrame(step); } }
  function arm() {
    if (timerId !== null || !pulse) return;
    const time = now(), next = Math.max((Math.floor(time / period) + 1) * period, lastBeat + period);
    timerId = setTimer(onBeat, Math.max(0, next - time));
  }
  function onBeat() {
    timerId = null;
    if (!pulse) return;
    const start = Math.round(now() / period) * period;
    if (start > lastBeat) { lastBeat = start; beat = { plan: pulse, start }; beats++; run(); }
    arm();
  }
  return {
    poke(plan) { if (!plan) return; poke = { plan, start: now() }; run(); },
    pulse(plan) {
      pulse = plan && Array.isArray(plan.strands) && plan.strands.length ? plan : null;
      if (pulse) arm(); else if (timerId !== null) { clearTimer(timerId); timerId = null; }
    },
    redraw() { const time = now(); settle(time); const running = impulses(); if (running.length) draw({ impulses: running, time }); },
    stop() {
      poke = beat = pulse = null;
      if (timerId !== null) clearTimer(timerId);
      if (pending) cancelFrame(frameId);
      timerId = null; pending = false; frameId = null;
      draw(null);
    },
    get state() { return { poke: Boolean(poke), beat: Boolean(beat), pulse: Boolean(pulse), framePending: pending, timerPending: timerId !== null, frames, draws, beats }; },
  };
}

/* Background glow (Louis 26 Sep: "make the background glow too when the node pulse"): one soft radial glow
 * under the web per pulsing node (working or in flight on a fresh feed), green for work and the node's own
 * colour for a job in flight. Radius ~110 map units, clamped to 150 screen pixels so zooming in never floods
 * the map. */
const GLOW_WORKING = '#63d6ac';
const safeGlowColor = color => typeof color === 'string' && /^#[0-9a-f]{3,8}$/i.test(color) ? color.toLowerCase() : GLOW_WORKING;
export function glowRadius(zoom, { radius = 110, maxScreen = 150 } = {}) {
  const z = Number.isFinite(zoom) && zoom > 0 ? zoom : 1, base = Number.isFinite(radius) && radius > 0 ? radius : 110;
  return Math.round(Math.max(24, Math.min(base, (Number.isFinite(maxScreen) && maxScreen > 0 ? maxScreen : 150) / z)) * 10) / 10;
}
export function webGlows(nodes, { fresh = false } = {}) {
  if (fresh !== true) return { glows: [], key: '' };
  const glows = [...webNodes(nodes).values()].filter(node => node.active === true || node.inFlight === true).slice(0, ORB_WEB_LIMITS.glows)
    .map(node => ({ id: node.id, x: node.x, y: node.y, tone: node.active === true ? 'working' : 'flight', color: node.active === true ? GLOW_WORKING : safeGlowColor(node.color), periodMs: [1200, 2400].includes(node.auraPeriodMs) ? node.auraPeriodMs : null }));
  return { glows, key: glows.map(glow => `${glow.id}|${webNum(glow.x)}|${webNum(glow.y)}|${glow.color}|${glow.periodMs ?? ''}`).join(';') };
}

/**
 * Windows edition: the Mac as a LAN peer (snapshot.macPeer from the PC observer's background probe of the Mac's
 * LM Studio). The probe runs about every 10 s, so its result is aged by its own age plus the snapshot's and counts as
 * current for 45 s. A reachable listing is inventory only: it never says the Mac is generating.
 */
const PEER_MODEL = /^[\x21-\x7e]{1,120}$/;
const PEER_FRESH_SECONDS = 45;
export function macPeerView(peer, { feedFresh = false, snapshotAge = 0 } = {}) {
  const extra = Math.max(0, Number.isFinite(snapshotAge) ? snapshotAge : 0);
  const block = peer && typeof peer === 'object' && !Array.isArray(peer) ? peer : null;
  const age = block && Number.isFinite(block.ageSeconds) && block.ageSeconds >= 0 ? block.ageSeconds + extra : null;
  const current = Boolean(feedFresh) && age !== null && age <= PEER_FRESH_SECONDS;
  const state = current && ['reachable', 'unreachable'].includes(block.state) ? block.state : 'unknown';
  const reachable = state === 'reachable';
  const models = reachable && Array.isArray(block.models) ? block.models.slice(0, 64)
    .filter(m => m && typeof m.id === 'string' && PEER_MODEL.test(m.id) && ['loaded', 'not-loaded', 'listed', 'unknown'].includes(m.state)) : [];
  const loaded = models.filter(m => m.state === 'loaded');
  const loadedKnown = reachable && Number.isSafeInteger(block.loadedCount) && block.loadedCount >= 0;
  const latency = reachable && Number.isFinite(block.latencyMs) && block.latencyMs >= 0 && block.latencyMs < 60000 ? Math.round(block.latencyMs) : null;
  const address = reachable && typeof block.address === 'string' && /^[0-9.]{7,15}$/.test(block.address) ? block.address : null;
  const via = block?.via === 'mdns' ? 'mDNS name' : block?.via === 'recorded-ip' ? 'recorded address' : null;
  const loadedText = loadedKnown ? `${loaded.length} loaded` : 'loaded state unknown';
  const subtitle = reachable ? `LAN${latency !== null ? ` ${latency} MS` : ''} · ${loadedText.toUpperCase()}`
    : state === 'unreachable' ? 'NOT ANSWERING ON THE LAN' : 'LAN STATE UNKNOWN';
  const chip = !feedFresh ? { tone: 'muted', detail: 'Signal stale', short: 'Stale' }
    : reachable ? { tone: 'ok', detail: [`Reachable${latency !== null ? ` · ${latency} ms` : ''}`, loadedText].join(' · '), short: loadedKnown ? `${loaded.length} loaded` : 'Reachable' }
      : state === 'unreachable' ? { tone: 'warn', detail: 'Not answering on the LAN', short: 'No answer' }
        : { tone: 'muted', detail: 'LAN probe pending or stale', short: 'Unknown' };
  const expected = typeof block?.expectedVerifyModel === 'string' ? block.expectedVerifyModel : null;
  const rows = [
    ['Reachability', reachable ? 'Answering' : state === 'unreachable' ? 'Not answering' : 'Unknown'],
    ['Address', address ? `${address}${via ? ` (${via})` : ''}` : 'Unknown'],
    ['Round trip', latency !== null ? `${latency} ms` : 'Unknown'],
    ['Loaded models', loadedKnown ? (loaded.length ? loaded.map(m => m.id).join(', ') : 'None') : reachable ? 'Not reported by this listing' : 'Unknown'],
    ['Expected verify model', expected ? `${expected}${reachable && typeof block.expectedVerifyLoaded === 'boolean' ? (block.expectedVerifyLoaded ? ' · loaded' : ' · not loaded') : ''}` : 'Not recorded'],
    ['Probe age', age !== null ? `${Math.round(age)} s` : 'Unknown'],
    ['Detail', typeof block?.detail === 'string' ? block.detail.slice(0, 240) : 'Unknown'],
  ];
  return { state, reachable, current, age, latency, address, models, loaded, loadedKnown, subtitle,
    brief: reachable ? `LAN · ${loadedText.toUpperCase()}` : state === 'unreachable' ? 'NO ANSWER' : 'UNKNOWN',
    satellites: loaded.slice(0, 2), chip, rows };
}
