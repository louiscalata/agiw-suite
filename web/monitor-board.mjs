/**
 * Paints the monitor tab strip and the selected monitor's board.
 * View only: callers must not wire these controls to pause, repair, or model posts.
 */
import {demoSnapshot, DEMO_BANNER, jevGate, nextTabFocus, registryMachineView, registryModelName} from '/monitor-tabs.mjs';
export {registryMachineView, registryModelName};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function statusClass(status) {
  if (status === 'blocked') return 'j-blocked';
  if (status === 'unknown' || status === 'stale') return 'j-unknown';
  if (status === 'unavailable' || status === 'disconnected' || status === 'partial') return 'j-unavailable';
  return '';
}

function matrixClass(matrix) {
  if (matrix === 'Observed') return 'j-matrix-observed';
  if (matrix === 'Untested') return 'j-matrix-untested';
  if (matrix === 'Blocked') return 'j-matrix-blocked';
  return 'j-matrix-untested';
}

const MACHINE_NAMES = {mac: 'MAC', macos: 'MAC', windows: 'PC', linux: 'LINUX'};
/** A table entry by an own key only, so a snapshot string like 'constructor' never reaches a function. */
export const own = (table, key) => (typeof key === 'string' && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined);

/** Up, but the machine says it is degraded: by reasonCode, or (older PC rows without one) by reason 'partly-serving'. */
export function rowDegraded(row) {
  if (row?.state !== 'up') return false;
  if (row.reasonCode !== undefined && row.reasonCode !== null) return row.reasonCode === 'reported_degraded';
  return row.reason === 'partly-serving';
}

/**
 * Plain words for every machine reason code machine_state and peer_feed send. A code not in the
 * table but safe to show reads with its underscores as spaces; anything else is "not observed".
 * "never observed" (nothing ever arrived) and "went silent" (it answered before) stay distinct.
 */
export const REASON_WORDS = Object.freeze({
  observed: 'observed', reported_down: 'reported down', reported_degraded: 'reported degraded',
  not_observed: 'never observed', not_connected: 'went silent', not_configured: 'not configured', unavailable: 'unavailable',
  timeout: 'timed out', refused: 'connection refused', non_json: 'unreadable reply',
  oversize: 'reply too large', schema_invalid: 'unexpected reply', stale: 'reading too old',
  clock_skew: 'clock out of step', host_key_mismatch: 'host key changed', share_unmounted: 'share not mounted',
  asleep: 'asleep', untrusted: 'not trusted', unverified: 'not verified',
  identity_mismatch: 'identity does not match', read_error: 'read failed',
});

export function reasonWords(code) {
  if (typeof code !== 'string' || !/^[a-z_]{1,40}$/.test(code)) return 'not observed';
  return Object.prototype.hasOwnProperty.call(REASON_WORDS, code) ? REASON_WORDS[code] : code.replace(/_/g, ' ');
}

/**
 * The machine rows as a monospace peer map, this machine first. Words carry every state;
 * the link glyph repeats it: === up, -x- reported down, -?- unknown. Counts appear only when the
 * row says its model list is complete. A missing read is unknown, never down.
 */
export function peerMap({machines, self, feedFresh}) {
  const rows = Array.isArray(machines) ? machines.filter(row => row && typeof row === 'object' && !Array.isArray(row)) : [];
  if (!rows.length) {
    return {text: 'MACHINES  UNKNOWN\n          no machine rows in this sample', label: 'Machines: unknown. No machine rows in this sample.'};
  }
  const hubIndex = Math.max(0, rows.findIndex(row => row.host === self || row.platform === self));
  const describe = row => {
    if (row.observationSource === 'registry') {
      const view = registryMachineView(row, {fresh: feedFresh === true});
      const reason = row.reasonCode && row.reasonCode !== 'observed' ? ` · ${reasonWords(row.reasonCode)}` : '';
      return {name: view.label.toUpperCase(), state: view.word, link: view.health === 'unknown' ? '-?-' : '===',
        detail: `${view.detail}${view.health === 'unknown' ? reason : ''}`};
    }
    const state = row.state === 'up' ? (rowDegraded(row) ? 'DEGRADED' : 'UP') : row.state === 'down' ? 'DOWN' : 'UNKNOWN';
    const link = state === 'DOWN' ? '-x-' : state === 'UNKNOWN' ? '-?-' : '===';
    const parts = [];
    if (state === 'UNKNOWN') parts.push(reasonWords(row.reasonCode));
    else if (state === 'DOWN') parts.push('reported down');
    else if (row.modelsComplete === true && Number.isInteger(row.loadedCount)) {
      parts.push(`${row.loadedCount} loaded / ${Number.isInteger(row.busyCount) ? row.busyCount : '?'} busy`);
    } else parts.push('model list incomplete');
    if (typeof row.trust === 'string' && /^[a-z-]{1,20}$/.test(row.trust)) parts.push(row.trust);
    if (typeof row.ageSeconds === 'number' && Number.isFinite(row.ageSeconds)) parts.push(`${Math.round(row.ageSeconds)} s`);
    const name = own(MACHINE_NAMES, row.platform) || String(row.host || 'MACHINE').toUpperCase().replace(/[^A-Z0-9-]/g, '').slice(0, 12) || 'MACHINE';
    return {name, state, link, detail: parts.join(' · ')};
  };
  const hub = describe(rows[hubIndex]);
  const peers = rows.filter((_, i) => i !== hubIndex).map(describe);
  const lines = [`THIS ${hub.name}`.padEnd(14) + `${hub.state.padEnd(9)}${hub.detail}`];
  for (const peer of peers) lines.push(` |${peer.link} ${peer.name.padEnd(8)} ${peer.state.padEnd(9)}${peer.detail}`);
  if (!peers.length) lines.push(' |    no other machine in this sample');
  if (feedFresh !== true) lines.push('          last sample, not live');
  const label = [`This ${hub.name.toLowerCase()} ${hub.state.toLowerCase()}`, ...peers.map(peer => `${peer.name.toLowerCase()} ${peer.state.toLowerCase()}, ${peer.detail}`)].join('. ') + (feedFresh === true ? '.' : '. Last sample, not live.');
  return {text: lines.join('\n'), label: `Machines: ${label}`};
}

function appendPeerMap(parent, context) {
  if (!parent || !context) return;
  const view = peerMap(context);
  const map = el('pre', 'j-schematic j-peer-map');
  map.tabIndex = 0;
  map.setAttribute('role', 'img');
  map.setAttribute('aria-label', view.label);
  map.textContent = view.text;
  parent.append(map);
}

export function appendEvidence(parent, evidence) {
  if (!parent || !evidence) return;
  const badges = evidence.badges || {};
  const schematic = el('pre', 'j-schematic');
  schematic.setAttribute('aria-label', 'Process liveness, lane readiness, and run attachment. These are separate.');
  schematic.textContent = `PROCESS  ${badges.process || 'UNKNOWN'}\nLANE     ${badges.lane || 'UNKNOWN'}\nATTACH   ${badges.attachment || 'UNKNOWN'}`;
  parent.append(schematic);
  const acceptance = (evidence.rows || []).find(row => row.id === 'acceptance');
  if (acceptance) {
    const line = acceptance.word === 'BLOCKED'
      ? 'BLOCKED · Acceptance · Windows review is not a pass'
      : `${acceptance.matrix} · ${acceptance.label} · ${acceptance.word}`;
    parent.append(el('p', `j-callout ${matrixClass(acceptance.matrix)}`, line));
  }
  const details = el('details', 'j-evidence');
  const summary = el('summary', null, 'Evidence detail');
  details.append(summary);
  details.append(el('p', 'j-note', 'Observed, Untested, and Blocked describe the evidence. Observed is not a pass. A heartbeat does not make a lane ready. Inventory is not disconnected.'));
  const list = el('ul', 'j-steps');
  for (const row of evidence.rows || []) {
    const item = el('li', `j-step ${matrixClass(row.matrix)}`);
    item.append(el('p', 'j-step-hit', `${row.matrix} · ${row.label}`));
    item.append(el('p', 'j-word', row.word || row.matrix));
    item.append(el('p', 'j-step-reason', `${row.detail || ''} Evidence: ${row.evidence || 'not recorded'}.`));
    list.append(item);
  }
  details.append(list);
  if (Array.isArray(acceptance?.ledger)) {
    const ledger = el('ul', 'j-steps');
    for (const row of acceptance.ledger) {
      const item = el('li', 'j-step j-matrix-blocked');
      item.append(el('p', 'j-step-hit', `${row.gate} · ${row.env}`));
      item.append(el('p', 'j-word', row.verdict));
      item.append(el('p', 'j-step-reason', `${row.state} · ${row.time} · ${row.receipt} · ${row.next}`));
      ledger.append(item);
    }
    details.append(ledger);
  }
  parent.append(details);
}

function appendIdentity(parent, monitor) {
  const grid = el('dl', 'j-identity');
  const workload = monitor.workload;
  const rows = [
    ['Workload', workload?.id || monitor.id],
    ['Machine', monitor.identity.machineLabel],
    ['Provider', monitor.identity.providerLabel],
    ['Requested model', workload?.requestedModel || 'Not reported'],
    ['Served model', workload?.servedModel || 'Not reported'],
    ['Run', workload?.runId || monitor.identity.session || 'None in this sample'],
    ['Attempt', workload?.attemptId || 'Not reported'],
  ];
  for (const [name, value] of rows) {
    grid.append(el('dt', null, name), el('dd', null, value));
  }
  parent.append(grid);
}

function appendMetrics(parent, metrics) {
  const grid = el('dl', 'j-metrics');
  for (const item of metrics || []) {
    const value = el('dd', null, item.value);
    if (item.title) value.setAttribute('title', item.title);
    grid.append(el('dt', null, item.label), value);
  }
  parent.append(grid);
}

function filteredSteps(monitor, query) {
  const needle = query.trim().toLowerCase();
  return (monitor.pipeline?.steps || []).filter(item => {
    if (!needle) return true;
    return `${item.label} ${item.word} ${item.reason || ''} ${item.detail || ''}`.toLowerCase().includes(needle);
  });
}

function appendStep(list, item, {pressed, onPress, open}) {
  const row = el('li', `j-step ${statusClass(item.status)}${pressed && item.status !== 'blocked' ? ' j-selected' : ''}${pressed && item.status === 'blocked' ? ' j-selected-blocked' : ''}`);
  const button = el('button', 'j-step-hit');
  button.type = 'button';
  button.setAttribute('aria-pressed', String(pressed));
  button.append(el('span', 'j-glyph', item.glyph), el('span', 'j-step-label', item.label), el('span', 'j-word', item.word));
  if (pressed) button.append(el('span', 'j-selected-word', 'SELECTED'));
  button.addEventListener('click', () => onPress(item.id));
  row.append(button);
  if (item.detail) row.append(el('p', 'j-step-detail', item.detail));
  if (item.reason) row.append(el('p', 'j-step-reason', item.reason));
  if (open) {
    const jump = el('button', 'j-open', 'Open monitor');
    jump.type = 'button';
    jump.dataset.openMonitor = item.id;
    row.append(jump);
  }
  list.append(row);
}

function paintBoard(board, monitor, desk) {
  const saved = desk.view(monitor.id);
  const focusFilter = document.activeElement?.id === 'monitorFilter';
  const caret = focusFilter ? document.activeElement.selectionStart : null;
  const resume = board.dataset.monitor === monitor.id
    ? board.querySelector('[data-jaryo-choices]')?.choiceViewState?.() : null;
  for (const old of board.querySelectorAll('[data-jaryo-choices]')) old.choiceCleanup?.();
  board.replaceChildren();
  board.hidden = false;
  board.dataset.monitor = monitor.id;
  const head = el('div', 'j-board-head');
  head.append(el('p', 'j-eyebrow', monitor.eyebrow), el('h2', 'j-board-title', monitor.title));
  head.querySelector('h2').id = 'monitorBoardTitle';
  board.append(head);
  const banner = el('p', `j-status ${statusClass(monitor.status)}`);
  banner.append(el('span', 'j-glyph', monitor.glyph), el('span', 'j-word', monitor.word));
  if (monitor.reason) banner.append(el('span', 'j-reason', monitor.reason));
  board.append(banner);
  if (monitor.fictional) board.append(el('p', 'j-demo', DEMO_BANNER));
  if (monitor.id === 'all') appendPeerMap(board, board.jaryoMachines);
  appendEvidence(board, monitor.evidence);
  appendIdentity(board, monitor);
  const filter = el('label', 'j-filter');
  filter.append(el('span', null, 'Filter this monitor'));
  const input = el('input');
  input.id = 'monitorFilter';
  input.type = 'search';
  input.value = saved.boardQuery || '';
  input.autocomplete = 'off';
  input.setAttribute('aria-label', `Filter ${monitor.title}`);
  input.addEventListener('input', () => {
    desk.patch(monitor.id, {boardQuery: input.value});
    paintBoard(board, monitor, desk);
  });
  filter.append(input);
  board.append(filter);
  const pipeline = monitor.pipeline || {steps: [], note: '', declared: false, incomplete: false};
  board.append(el('p', 'j-note', pipeline.note || ''));
  if (pipeline.incomplete) board.append(el('p', 'j-unknown j-callout', '? INCOMPLETE · the list below is only what this sample contained'));
  if (!pipeline.declared && !(pipeline.steps || []).length) {
    board.append(el('p', 'j-unavailable j-callout', '○ UNAVAILABLE · no pipeline rows were declared for this monitor'));
  }
  const steps = filteredSteps(monitor, saved.boardQuery || '');
  if ((saved.boardQuery || '') && !steps.length) board.append(el('p', 'j-note', 'Nothing in this monitor matches the filter.'));
  const list = el('ol', 'j-steps');
  for (const item of steps) {
    appendStep(list, item, {
      pressed: saved.boardSelected === item.id,
      open: monitor.kind === 'overview',
      onPress: id => {
        desk.patch(monitor.id, {boardSelected: id});
        paintBoard(board, monitor, desk);
      },
    });
  }
  const presentation = el('div');
  presentation.setAttribute('data-jaryo-choices', '');
  presentation.jaryoChoices = {
    steps, selectedId: saved.boardSelected, openMonitor: monitor.kind === 'overview', resume,
    onSelect: id => desk.patch(monitor.id, {boardSelected:id}),
  };
  presentation.append(list);
  board.append(presentation);
  if (typeof document.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
    document.dispatchEvent(new CustomEvent('jaryo:pipeline', {detail:presentation}));
  }
  const analysis = el('section', 'j-analysis');
  const gate = jevGate(monitor.analyses || []);
  analysis.append(el('p', 'j-eyebrow', 'NISI AND JEV ANALYSIS'));
  const gateLine = el('p', gate.open ? 'j-status' : 'j-unavailable j-callout');
  gateLine.append(el('span', 'j-glyph', gate.open ? '●' : '○'), el('span', 'j-word', gate.word));
  gateLine.append(el('span', 'j-reason', gate.blocker || 'Jev pre and post records are both passed. That is not this workload\'s run result.'));
  analysis.append(gateLine);
  analysis.append(el('p', 'j-note', 'Analysis state is not run success. Unavailable, skipped, pending, and failed Jev phases do not open the gate.'));
  const analysisList = el('ul', 'j-steps');
  for (const slot of monitor.analyses || []) {
    const negativeVerdict = slot.verdict === 'ROLLBACK' || slot.verdict === 'NOT_PASS';
    const row = el('li', `j-step ${negativeVerdict ? 'j-blocked' : statusClass(slot.state === 'passed' ? 'ok' : slot.state === 'failed' ? 'blocked' : slot.state === 'pending' ? 'unknown' : slot.state === 'skipped' ? 'idle' : slot.state)}`);
    row.append(el('p', 'j-step-hit', `${slot.tool} ${slot.phase}`));
    row.append(el('p', 'j-word', negativeVerdict ? slot.verdict : slot.word || (slot.state === 'unavailable' ? 'NOT AVAILABLE YET' : slot.state.toUpperCase())));
    row.append(el('p', 'j-step-reason', [slot.verdict ? `Verdict ${slot.verdict}` : null, slot.detail, `Run ${slot.runId || 'not reported'}`, `attempt ${slot.attemptId || 'not reported'}`].filter(Boolean).join(' · ') + '.'));
    analysisList.append(row);
  }
  if (!(monitor.analyses || []).length) analysis.append(el('p', 'j-unavailable j-callout', '○ NOT AVAILABLE YET · this monitor has no run or attempt to attach'));
  analysis.append(analysisList);
  board.append(analysis);
  appendMetrics(board, monitor.metrics);
  if (monitor.retained && monitor.kind === 'session') {
    const dismiss = el('button', 'j-open', 'Dismiss retained session');
    dismiss.type = 'button';
    dismiss.dataset.dismissMonitor = monitor.id;
    board.append(dismiss);
  }
  board.append(el('p', 'j-footnote', 'Switching monitors only changes this view. It does not pause, cancel, restart, or starve inference.'));
  if (focusFilter) {
    const next = board.querySelector('#monitorFilter');
    next?.focus();
    if (caret != null && next?.setSelectionRange) next.setSelectionRange(caret, caret);
  }
}

function paintTabs(host, monitors, active) {
  const focused = host.contains(document.activeElement) ? document.activeElement?.dataset?.monitor : null;
  host.replaceChildren();
  for (const tab of monitors) {
    const button = el('button', 'j-tab');
    button.type = 'button';
    button.dataset.monitor = tab.id;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(tab.id === active));
    button.tabIndex = tab.id === active ? 0 : -1;
    button.setAttribute('aria-controls', 'monitorBoard');
    button.title = tab.reason || tab.word;
    button.append(el('span', 'j-glyph', tab.glyph), el('span', 'j-tab-label', tab.title), el('span', 'j-word', tab.word));
    host.append(button);
  }
  const ids = monitors.map(tab => tab.id);
  const focusId = nextTabFocus(focused, ids, active);
  if (focusId) [...host.querySelectorAll('[role="tab"]')].find(tab => tab.dataset.monitor === focusId)?.focus();
  // Fleet tabs follow the ledger in the document. Initial and background paints
  // must not scroll past its heading; explicit tab focus still scrolls normally.
  else if (!document.body.classList.contains('fleet-ledger')) {
    const selected = [...host.querySelectorAll('[role="tab"]')].find(tab => tab.getAttribute('aria-selected') === 'true');
    if (selected?.scrollIntoView) {
      try { selected.scrollIntoView({block: 'nearest', inline: 'nearest'}); }
      catch (_) { /* older webviews */ }
    }
  }
}

export function paintMonitorChrome({host, board, desk, snapshot, feedFresh, paused, demo, preferredId}) {
  if (!host || !board || !desk) return;
  const source = demo ? demoSnapshot() : snapshot;
  const monitors = desk.note(source, {
    feedFresh: demo ? true : feedFresh === true,
    paused: demo ? false : paused === true,
    fictional: demo === true,
    now: Date.now()/1000,
    snapshotAge: Number.isFinite(source?.sampledAt) ? Math.max(0,Date.now()/1000-source.sampledAt) : undefined,
  });
  const applied = preferredId ? desk.select(preferredId) === true : false;
  const active = monitors.find(item => item.id === desk.active) || monitors[0];
  host.dataset.fictional = demo ? '1' : '0';
  const signature = JSON.stringify([demo, paused, active?.id, monitors.map(item => [item.id, item.word, item.title, item.retained === true, (item.analyses || []).map(slot => `${slot.tool}:${slot.phase}:${slot.state}`).join(','), item.pipeline?.incomplete, item.pipeline?.steps?.length, item.workload?.servedModel || '', item.workload?.requestedModel || '', item.evidence?.badges?.lane || '', (item.evidence?.rows || []).map(row => `${row.id}:${row.matrix}`).join(',')]), desk.view(active.id)]);
  const machines = Array.isArray(source?.machines) ? source.machines : null;
  board.jaryoMachines = {machines, self: source?.host, feedFresh: demo ? false : feedFresh === true && paused !== true};
  const machineSignature = JSON.stringify([board.jaryoMachines.feedFresh, (machines || []).map(row => row && [row.host, row.platform, row.label, row.state, row.health, row.reasonCode, row.trust, row.modelsComplete, row.loadedCount, row.busyCount, typeof row.ageSeconds === 'number' ? Math.round(row.ageSeconds) : null, row.lanes])]);
  const fullSignature = signature + machineSignature;
  if (host.dataset.signature === fullSignature && board.dataset.signature === fullSignature) return applied;
  host.dataset.signature = fullSignature;
  board.dataset.signature = fullSignature;
  paintTabs(host, monitors, active.id);
  if (board.dataset.hideBoard === '1') {
    board.hidden = true;
    for (const old of board.querySelectorAll('[data-jaryo-choices]')) old.choiceCleanup?.();
    board.replaceChildren();
  } else {
    paintBoard(board, active, desk);
  }
  document.body.classList.toggle('monitor-scoped', active.id !== 'all');
  return applied;
}
