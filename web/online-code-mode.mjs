/* Router-concurrency P2 (spec 6.13): the router may run several routed tasks at once. The snapshot lists
 * them (activeRuns, queuedRuns) with the Mac-wide lanes they hold; every field is re-checked here and
 * anything malformed is dropped, never guessed. */
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/;
const WORD = /^[a-z][a-z0-9_.-]{0,31}$/;
const STAGE_WORD = /^[A-Za-z][A-Za-z0-9_.:-]{0,63}$/;
// The same words as app.js ROUTE_STATUS_WORDS: one name per run state everywhere in the UI.
const RUN_STATES = { running: 'Running', waiting: 'Queued', admitting: 'Being admitted', unresolved: 'Unresolved',
  'archived-uncleared': 'Archived, record not cleared', unverified: 'Run lock unverified', unreadable: 'Record unreadable',
  changing: 'Changing; checked again next sample' };
const LANES = [['mac-pair', 'Mac pair', 1], ['pc-route', 'PC route', 1], ['pc-deep', 'PC deep', 1], ['pc-fast', 'PC fast', 2]];
const RESOURCES = { 'mac-pair': 'Mac pair', 'pc-route': 'PC route', 'pc-lane-deep': 'PC deep lane', 'pc-lane-fast': 'PC fast lane' };
const CLIENTS = ['codex', 'claude', 'opencode'];
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
const runIds = list => Array.isArray(list) ? list.filter(id => typeof id === 'string' && RUN_ID.test(id)).slice(0, 8) : [];

/** Each listed run as {runId, state, stateLabel, client, host, stage, live}; at most 8.  A record that is
 * unreadable or unverified while its run lock is held, and a run ID recorded in both journal layouts, say so. */
export function routeRunRows(mode) {
  const rows = Array.isArray(mode?.activeRuns) ? mode.activeRuns : [];
  return rows.filter(row => row && typeof row.runId === 'string' && RUN_ID.test(row.runId)).slice(0, 8).map(row => {
    const state = Object.hasOwn(RUN_STATES, row.state) ? row.state : 'unknown';
    const held = row.lockHeld === true && (state === 'unreadable' || state === 'unverified');
    return {
      runId: row.runId,
      state,
      stateLabel: (RUN_STATES[row.state] || 'State unknown') + (held ? ' (run lock held)' : '')
        + (row.collision === true ? ' · run ID in both journal layouts' : ''),
      client: CLIENTS.includes(row.client) ? row.client : null,
      host: ['mac', 'windows', 'auto'].includes(row.host) ? row.host : null,
      stage: typeof row.stage === 'string' && STAGE_WORD.test(row.stage) ? row.stage : null,
      live: row.live === true,
    };
  });
}

/** Each queued run as {runId, resource, waitingFor, secondsLeft, client}; the wait ages by `snapshotAge`.
 * Only a row whose phase is waiting or admitting is a queue row; anything else is dropped, never guessed. */
export function routeQueueRows(mode, { snapshotAge = 0 } = {}) {
  const rows = Array.isArray(mode?.queuedRuns) ? mode.queuedRuns : [];
  const age = Number.isFinite(snapshotAge) && snapshotAge > 0 ? snapshotAge : 0;
  return rows.filter(row => row && typeof row.runId === 'string' && RUN_ID.test(row.runId)
    && (row.phase === 'waiting' || row.phase === 'admitting')).slice(0, 8).map(row => {
    const resource = Object.hasOwn(RESOURCES, row.resource) ? row.resource : null;
    const left = Number.isFinite(row.secondsLeft) && row.secondsLeft >= 0 ? Math.max(0, Math.round(row.secondsLeft - age)) : null;
    return { runId: row.runId, resource, phase: row.phase,
      waitingFor: resource ? `waiting for ${RESOURCES[resource]}` : row.phase === 'admitting' ? 'being admitted' : 'waiting',
      secondsLeft: left, client: CLIENTS.includes(row.client) ? row.client : null };
  });
}

/** The Mac-wide lanes with their router holders: "Mac pair 1 · PC route 1 · PC deep 1 · PC fast 2". */
export function routeLanesView(mode) {
  const lanes = mode?.lanes && typeof mode.lanes === 'object' ? mode.lanes : {};
  const rows = LANES.map(([id, label, capacity]) => {
    const lane = lanes[id] && typeof lanes[id] === 'object' ? lanes[id] : {};
    return { id, label, capacity, holders: runIds(lane.holders), waiting: runIds(lane.waiting) };
  });
  return {
    rows,
    label: rows.map(row => `${row.label} ${row.capacity}`).join(' · '),
    text: rows.map(row => `${row.label} ${row.holders.length}/${row.capacity}${row.holders.length ? ` (${row.holders.join(', ')})` : ''}`
      + (row.waiting.length ? `, ${row.waiting.length} waiting` : '')).join(' · '),
  };
}

/** Admission policy in words: the single-run router, single, multi, or single while shared runs drain. */
export function routeAdmissionText(mode) {
  const admission = mode?.admission;
  if (!admission || typeof admission !== 'object') return 'Unknown';
  if (admission.source === 'legacy-router') return 'Single-run router (one route at a time)';
  if (admission.source === 'invalid') return 'Policy file invalid; the router admits no new run';
  if (admission.source === 'fence-missing' || admission.source === 'fence-invalid') {
    return `Install fence ${admission.source === 'fence-missing' ? 'missing' : 'invalid'}; the router refuses every command`;
  }
  // Lanes hold 1 (Mac pair, PC route, PC deep) or 2 (PC fast) at once; see the Lanes row.  A caller whose own
  // CODEMODE_ROUTER_CONCURRENCY forces single is admitted alone; the Monitor cannot see callers' environments.
  if (admission.policy === 'multi') {
    return 'Multi: routes run at once, up to each lane’s capacity'
      + (admission.callerOverride === 'not-observable' ? ' (a caller with CODEMODE_ROUTER_CONCURRENCY=single runs alone)' : '');
  }
  if (admission.policy !== 'single') return 'Unknown';
  return admission.drainState === 'draining' ? `Single · draining ${runIds(admission.sharedHolders).length || 'shared'} run(s)`
    : admission.drainState === 'drained' ? 'Single' : 'Single · drain state unknown';
}

/** Turn passive router and launcher observations into conservative status copy. */
export function onlineCodeModeView(mode, { feedFresh = false, paused = false, reducedMotion = false } = {}) {
  const observed = Boolean(mode && feedFresh);
  const routeId = typeof mode?.routeId === 'string' && mode.routeId.trim() ? mode.routeId.trim() : null;
  const installing = observed && mode.taskState === 'install-in-progress';
  const processing = !installing && observed && mode.state === 'processing' && mode.active === true && routeId !== null;
  const idle = observed && mode.state === 'inactive' && mode.active === false;
  const ready = observed && mode.state === 'ready' && mode.active === false;
  const unfinished = observed && mode.state === 'unknown' && mode.active === null
    && mode.taskState === 'unfinished' && routeId !== null;
  const state = installing ? 'installing' : processing ? 'processing' : idle ? 'inactive' : ready ? 'ready'
    : unfinished ? 'unfinished' : 'unknown';
  const running = observed ? count(mode.runCounts?.running) : 0, queued = observed ? count(mode.runCounts?.queued) : 0;
  const unresolvedRuns = observed ? count(mode.runCounts?.unresolved) : 0;
  const several = processing && running + queued > 1;
  // Only queued runs (waiting for admission or a lane): listed and active, but nothing is running work yet,
  // so the copy never says "processing" and the tab never blinks.
  const queuedOnly = processing && (mode.taskState === 'queued' || (running === 0 && queued > 0));
  const verb = queuedOnly ? 'queued' : 'processing';
  const client = processing && typeof mode.client === 'string' && mode.client.trim() ? mode.client.trim() : null;
  const chatId = processing && typeof mode.chatId === 'string' && mode.chatId.trim() ? mode.chatId.trim() : null;
  const attribution = [client, chatId].filter(Boolean).join(' · ');
  const setupState = observed && typeof mode.setupState === 'string' ? mode.setupState
    : ready ? 'fresh' : idle ? 'absent' : 'unknown';
  const age = Number.isFinite(mode?.setupAgeSeconds) && mode.setupAgeSeconds >= 0
    ? Math.round(mode.setupAgeSeconds) : null;
  const setupLabel = setupState === 'fresh' ? `Checked${age === null ? '' : ` ${age}s ago`}`
    : setupState === 'absent' ? 'Not checked recently'
      : setupState === 'expired' ? 'Receipt expired; readiness unverified'
        : setupState === 'invalid' ? 'Receipt invalid or unreadable' : 'Unknown';
  const counts = `${running} running · ${queued} queued`;
  const taskLabel = installing ? 'Router install in progress'
    : several ? `Routed tasks ${verb} · ${counts}`
      : processing ? `Routed task ${verb}`
        : unfinished ? unresolvedRuns > 1 ? `${unresolvedRuns} unfinished router tasks` : 'Unfinished router task'
          : idle || ready || (observed && mode.taskState === 'idle') ? 'Idle; no active route' : 'Unknown';
  const idleLabel = idle ? setupState === 'expired' ? 'Task idle · setup receipt expired'
    : setupState === 'invalid' ? 'Task idle · setup receipt invalid'
      : setupState === 'fresh' ? `Task idle · setup checked${age === null ? '' : ` ${age}s ago`}`
        : 'Task idle · setup not checked recently' : null;
  const label = installing ? 'Router install in progress'
    : several ? `Routed tasks ${verb} · ${counts}`
      : processing ? `Routed task ${verb}${attribution ? ` · ${attribution}` : ''}`
        : unfinished ? 'Unfinished router task'
          : idle ? idleLabel
            : ready ? `Task idle · setup checked${age === null ? '' : ` ${age}s ago`}`
              : 'Online Code Mode · Unknown';
  return {
    state, label, taskLabel, setupLabel, client, chatId,
    routeId: processing || unfinished ? routeId : null,
    evidence: observed && typeof mode.evidence === 'string' && mode.evidence.trim()
      ? mode.evidence.trim() : 'No fresh Online Code Mode evidence available.',
    observedAt: observed && typeof mode.observedAt === 'string' ? mode.observedAt : null,
    blinking: processing && !queuedOnly && mode.blinking === true && !paused && !reducedMotion,
    queuedOnly,
    runCounts: { running, queued, unresolved: unresolvedRuns },
    runRows: observed ? routeRunRows(mode) : [],
    queueRows: observed ? routeQueueRows(mode) : [],
    lanes: routeLanesView(observed ? mode : null),
    admission: observed ? routeAdmissionText(mode) : 'Unknown',
    runsTruncated: observed && mode.runsTruncated === true,
  };
}

/** Keep private v0.2 installation evidence separate from the shared route. */
export function nisiV02View(record, { feedFresh = false } = {}) {
  const observed = feedFresh && record?.schemaVersion === 1
    && record.integration === 'private-local-orchestration-route';
  const choice = (value, allowed) => observed && allowed.includes(value) ? value : 'UNKNOWN';
  const activation = observed && record.activationProbe?.kind === 'historical-local-model-probe'
    ? record.activationProbe : null;
  const probe = activation
    ? `${choice(activation.workStatus, ['RESPONSE_VALIDATED', 'UNAVAILABLE', 'NOT_RUN'])} · contract ${choice(activation.contract, ['PASS', 'FAIL', 'NOT_RUN', 'UNAVAILABLE'])} · tests ${choice(activation.tests, ['PASS', 'FAIL', 'NOT_RUN', 'UNAVAILABLE'])} · certification ${choice(activation.certification, ['PASS', 'FAIL', 'NOT_RUN', 'UNAVAILABLE'])} · accepted ${activation.accepted === true ? 'yes' : activation.accepted === false ? 'no' : 'unknown'}`
    : 'Unknown';
  return {
    observed,
    version: observed && typeof record.version === 'string' ? record.version : 'Unknown',
    runtimeIntegrity: choice(record?.runtimeIntegrity, ['VERIFIED', 'UNKNOWN']),
    hostBinding: choice(record?.hostBinding, ['VERIFIED', 'DRIFT', 'UNKNOWN']),
    driftedHostFiles: observed && Array.isArray(record?.driftedHostFiles)
      ? record.driftedHostFiles.filter(name => typeof name === 'string' && name.length <= 80).slice(0, 8) : [],
    activationStatus: choice(record?.activationStatus, ['VERIFIED', 'NOT_VERIFIED', 'UNKNOWN']),
    activationVerifiedAt: observed && typeof record.activationVerifiedAt === 'string'
      ? record.activationVerifiedAt : null,
    activationProbe: probe,
    liveInference: 'Unknown; no current invocation feed',
    workflowAcceptance: 'Unknown; no accepted workflow receipt',
    releaseAcceptance: 'Not established by monitor evidence',
  };
}

/* Fix Nisi Inference (Fix scope "nisi", online_code_repair._fix_nisi). The controller records each step as
 * {name, result, ...identities}; these tables give each one a plain label and a short sentence. Each step
 * carries a mark that is a shape as well as a colour (✓ done, • noted, ! needs action, ✕ failed), so colour
 * is never the only signal. An unknown step or result is shown as recorded, never as a success. */
const STEP_MARKS = { ok: '✓', info: '•', warn: '!', bad: '✕', muted: '•' };
const NISI_STEP_LABELS = { 'route-status': 'Router', 'nisi-status': 'Nisi', marker: 'Pending record', 'owner-lock': 'Owner lock',
  'server-idle': 'Model server', recover: 'Recovery', pair: 'Model pair', jev: 'Jev', verify: 'Final check', journal: 'Fix journal' };
const NISI_STEP_RESULTS = {
  'route-status': { idle: ['ok', 'No route run is open'], busy: ['warn', 'A route task holds the router; stopped'],
    active: ['warn', 'An unresolved route run is open; stopped'], unavailable: ['bad', 'Router status unavailable; stopped'],
    'multiple-unresolved': ['warn', 'More than one route run needs owner review; stopped'],
    'install-in-progress': ['warn', 'Router install in progress; stopped'],
    'install-refusing': ['warn', 'The router refuses every command until its install is finished or rolled back; stopped'],
    'owner-action': ['warn', 'The router needs its owner; stopped'] },
  'nisi-status': { clear: ['ok', 'No pending call record'], 'recovery-required': ['info', 'A call left a pending record'],
    unavailable: ['bad', 'Status unavailable; stopped'] },
  marker: { stale: ['ok', 'Old enough to recover'], 'too-young': ['warn', 'Too recent; waiting for a slow call to settle'],
    'future-dated': ['warn', 'Dated in the future; stopped'], unsafe: ['bad', 'Failed its private-file checks; stopped'],
    missing: ['warn', 'Disappeared during the check; press Fix again'] },
  'owner-lock': { free: ['ok', 'Free: no Nisi call is running'], busy: ['warn', 'A Nisi call is still running; not recovering'],
    'router-busy': ['warn', 'A route task started; stopped'], 'router-absent': ['bad', 'Router owner lock missing; stopped'],
    missing: ['bad', 'Nisi owner lock missing; stopped'], unsafe: ['bad', 'Nisi owner lock is not private; stopped'],
    unavailable: ['bad', 'Could not be checked; stopped'] },
  'server-idle': { idle: ['ok', 'Idle in two samples 2 s apart'], busy: ['warn', 'Busy; try again when it is idle'],
    unknown: ['warn', 'State unknown; stopped'] },
  recover: { acknowledged: ['ok', 'The launcher recovered the pending record'], 'marker-changed': ['warn', 'The record changed first; nothing recovered'],
    'not-run': ['bad', 'State folder unreadable; nothing recovered'], interrupted: ['bad', 'Interrupted; check Nisi status before retrying'],
    'no-answer': ['bad', 'The launcher did not answer; not retried'], 'owner-busy': ['warn', 'A Nisi call took the lock; not recovered'],
    invalid: ['bad', 'Unexpected launcher result; not retried'], 'marker-still-present': ['bad', 'The record is still present after recovery'],
    unconfirmed: ['bad', 'The recovered file could not be confirmed'], mismatch: ['bad', 'The recovered file is not the record that was checked'] },
  pair: { resident: ['ok', 'Two distinct models are resident'], missing: ['warn', 'Nisi needs a second resident model'],
    ambiguous: ['warn', 'The resident models are ambiguous'], unavailable: ['bad', 'The loaded-model list is unavailable'] },
  jev: { 'opted-in': ['ok', 'Opted in'], 'not-opted-in': ['warn', 'Not opted in'], unavailable: ['bad', 'Status unavailable'] },
  verify: { clear: ['ok', 'Nisi reports no recovery needed'], 'recovery-required': ['warn', 'Nisi still reports recovery required'],
    'adapter-missing': ['warn', 'The Nisi adapter is not installed'], unavailable: ['bad', 'Nisi status unavailable'] },
  journal: { written: ['ok', 'Result saved to the fix journal'], failed: ['warn', 'The fix journal could not be written'] },
};
const STEP_TEXT = /^[\x20-\x7e]{1,128}$/;
const stepField = value => typeof value === 'string' && STEP_TEXT.test(value) ? value : null;
const lowerFirst = text => text.charAt(0).toLowerCase() + text.slice(1);

/** Seconds in the controller's own words ("45 s", "12 min", "9 h 5 min", "3 d 2 h"); null for anything but a sane count. */
export function markerAgeText(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0 || seconds > 3650 * 86400) return null;
  const s = Math.floor(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h} h ${m % 60} min` : `${Math.floor(h / 24)} d ${h % 24} h`;
}

/** One recorded Fix Nisi Inference step as {label, text, tone, mark}; null when it has no printable name and result. */
export function fixNisiStepView(step) {
  const name = step && typeof step === 'object' ? stepField(step.name) : null;
  const result = name ? stepField(step.result) : null;
  if (!name || !result) return null;
  const known = Object.hasOwn(NISI_STEP_RESULTS, name) && Object.hasOwn(NISI_STEP_RESULTS[name], result) ? NISI_STEP_RESULTS[name][result] : null;
  let [tone, text] = known || ['muted', `Recorded: ${result.replaceAll('-', ' ')}`];
  if (name === 'marker') {
    const age = typeof step.ageSeconds === 'string' && /^\d{1,10}$/.test(step.ageSeconds) ? markerAgeText(Number(step.ageSeconds)) : null;
    const owner = stepField(step.owner), facts = [age ? `${age} old` : null, owner ? `owner ${owner}` : null].filter(Boolean);
    if (facts.length) text = `${facts.join(', ')}: ${lowerFirst(text)}`;
  }
  if (name === 'pair' && result === 'resident' && stepField(step.author) && stepField(step.reviewer)) text = `${step.author} (author) + ${step.reviewer} (reviewer)`;
  if (name === 'route-status' && result === 'active' && stepField(step.runId)) text = `An unresolved route run is open (${step.runId}); stopped`;
  return { name, result, label: Object.hasOwn(NISI_STEP_LABELS, name) ? NISI_STEP_LABELS[name] : name, text, tone, mark: STEP_MARKS[tone] };
}

/** The recorded steps in order, each in plain words (bounded; unreadable entries are left out). */
export function fixNisiStepsView(steps) {
  return Array.isArray(steps) ? steps.slice(0, 32).map(fixNisiStepView).filter(Boolean) : [];
}

/**
 * How old a pending record must be before Fix Nisi Inference recovers it: online_code_repair.NISI_MARKER_MIN_AGE, which is
 * max(600, 4 * NISI_CALL_CAP_SECONDS). test_fix_nisi_memory_ui.mjs reads both from the controller and fails on drift.
 */
export const NISI_MARKER_MIN_AGE_SECONDS = 600;
const MARKER_OWNER = /^[\x20-\x7e]{1,128}$/;
/**
 * Whether the Nisi Inference inspector offers "Fix Nisi Inference", and the summary it opens with. Only a fresh Mac
 * feed whose route reads recovery-required, or whose Nisi component is unresolved (a pending call record),
 * offers it, and never while the route is verified running (a live call owns its record). The age and owner
 * come from the snapshot's pending-marker fields when it has them, the age advancing by the time since the
 * sample; without them the summary gives no numbers.
 *
 * An open router run (pipeline.runId) blocks it: the controller's first step stops on any active run and names the
 * manual steps (online_code_repair ROUTE_ACTIVE_MESSAGE). Then the summary leads with the run, in the route chip's own
 * words ("Unsettled run recorded"), and `blocked` tells the inspector to offer the fix only as a secondary action.
 */
export function nisiRecoveryView(pipeline, components, { feedFresh = false, macOwner = true, snapshotAge = 0 } = {}) {
  const nisi = Array.isArray(components) ? components.find(c => c && c.id === 'nisi') : null;
  const record = nisi?.state === 'unresolved' || pipeline?.pendingMarkerObserved === true;
  // A queued route is live too (its run lock is held while it waits for a lane): like a running one, it never offers the fix.
  const needed = Boolean(feedFresh) && Boolean(macOwner) && pipeline?.status !== 'running' && pipeline?.status !== 'queued'
    && (pipeline?.status === 'recovery-required' || nisi?.state === 'unresolved');
  if (!needed) return { needed: false, blocked: false, line: null, meaning: null, age: null, owner: null, young: false };
  const extra = Number.isFinite(snapshotAge) && snapshotAge > 0 ? snapshotAge : 0;
  const raw = record ? pipeline?.pendingMarkerAgeSeconds : null;
  const seconds = typeof raw === 'number' && Number.isFinite(raw) && raw >= 0 ? raw + extra : null;
  const age = seconds === null ? null : markerAgeText(seconds);
  const owner = record && typeof pipeline?.pendingMarkerOwner === 'string' && MARKER_OWNER.test(pipeline.pendingMarkerOwner) ? pipeline.pendingMarkerOwner : null;
  const young = age !== null && seconds < NISI_MARKER_MIN_AGE_SECONDS;
  // Open like the route chip reads it (any recorded runId); the id itself is printed only when it is printable.
  const open = typeof pipeline?.runId === 'string' && pipeline.runId.trim() !== '';
  const run = open && MARKER_OWNER.test(pipeline.runId) ? pipeline.runId : null;
  const blockedText = 'Fix Nisi Inference stops at its first step until that run is resolved; its check names the manual steps.';
  if (open) {
    const runText = run ? `Route run ${run} is still open` : 'A route run is still open';
    return { needed: true, blocked: true, line: 'Unsettled run recorded', age: record ? age : null, owner: record ? owner : null, young: false,
      meaning: [record ? `${runText}, and a Nisi call left a pending record${age ? ` ${age} ago` : ''}${owner && owner !== run ? ` (owner: ${owner})` : ''}.` : `${runText} and reports recovery required.`,
        blockedText].join(' ') };
  }
  if (!record) {
    return { needed: true, blocked: false, line: 'The route reports recovery required', age: null, owner: null, young: false,
      meaning: 'No pending Nisi record is reported. Fix Nisi Inference checks the route and Nisi, and names the next step.' };
  }
  return { needed: true, blocked: false, line: age ? `Nisi call left a pending record ${age} ago` : 'Nisi call left a pending record', age, owner, young,
    meaning: [owner ? `Owner: ${owner}.` : null, young ? `The call may still be running; Fix Nisi Inference waits until the record is at least ${NISI_MARKER_MIN_AGE_SECONDS / 60} min old.`
      : 'New Nisi work is refused until it is recovered. Fix Nisi Inference recovers it only when it can prove nothing is running.'].filter(Boolean).join(' ') };
}

/** Fix Nisi Inference's evidence tokens without the input digest prefix or the recovered file name ("input=…", "file=…"). */
export function fixNisiEvidence(evidence) {
  if (typeof evidence !== 'string') return evidence;
  return evidence.split(/;\s*/).filter(token => token && !/^(input|file)=/.test(token)).join('; ');
}

/**
 * The controller's Fix Nisi Inference message as the panel prints it: without the input digest prefix (the snapshot never
 * copies it either); once ready, without the leading "Nisi Inference ready." (or a legacy label) that the status line already says; and, where
 * the panel's scope note already says no model was called (`scoped`), without the controller's own "No model inference
 * was run." Anything else is left as written.
 */
export function fixNisiMessage(message, state, { scoped = false } = {}) {
  if (typeof message !== 'string') return message;
  let text = message.replace(/,\s*input [0-9a-f]{6,64}(?=\))/g, '');
  if (state === 'ready') text = text.replace(/^(?:Nisi Inference|Nisi \+ Jev) ready\.\s*/, '');
  if (scoped && ['ready', 'needs-action', 'error'].includes(state)) text = text.replace(/\s*No model inference was run\.\s*$/, '');
  return text.trim() || message.trim();
}
