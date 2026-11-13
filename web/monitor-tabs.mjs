/**
 * View models for inference-monitor tabs.
 *
 * Tabs are a lens over one snapshot. Nothing here fetches, posts, pauses,
 * cancels, or starts inference. Session names come from the snapshot's own
 * client, route, job, and machine records — missing records stay absent.
 */
import {registryMachineRows, registryMachineView, registryModelName} from './map-layout.mjs';
import {CLOCK_SKEW_SECONDS, fleetFacts, fleetMachines, fleetLanes, fleetSummary, fleetMachineCoverage} from './fleet-state.mjs';
export {registryMachineRows, registryMachineView, registryModelName};

const ROUTE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/;
const CLIENT_ID = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const JOB_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,80}$/;
const MONITOR_QUERY = /^[A-Za-z0-9:._-]{1,120}$/;

const STATUS = {
  live: {status: 'live', glyph: '▶', word: 'RUNNING'},
  queued: {status: 'queued', glyph: '–', word: 'QUEUED'},
  admitting: {status: 'queued', glyph: '–', word: 'ADMITTING'},
  idle: {status: 'idle', glyph: '●', word: 'IDLE'},
  ok: {status: 'ok', glyph: '●', word: 'OK'},
  degraded: {status: 'degraded', glyph: '●', word: 'DEGRADED'},
  down: {status: 'down', glyph: '×', word: 'DOWN'},
  stopped: {status: 'down', glyph: '×', word: 'STOPPED'},
  blocked: {status: 'blocked', glyph: '×', word: 'BLOCKED'},
  stale: {status: 'stale', glyph: '~', word: 'STALE'},
  unknown: {status: 'unknown', glyph: '?', word: 'UNKNOWN'},
  pending: {status: 'unknown', glyph: '–', word: 'PENDING'},
  recorded: {status: 'idle', glyph: '–', word: 'RECORDED'},
  unavailable: {status: 'unavailable', glyph: '○', word: 'UNAVAILABLE'},
  disconnected: {status: 'disconnected', glyph: '⊘', word: 'DISCONNECTED'},
  // No machine of this kind is set up at all. Styled like unavailable; never confused with a configured one that is down.
  not_configured: {status: 'unavailable', glyph: '○', word: 'NOT CONFIGURED'},
  partial: {status: 'unavailable', glyph: '○', word: 'PARTIAL'},
};

function presentation(key, reason) {
  const base = STATUS[key] || STATUS.unknown;
  return {...base, reason: reason || null};
}

function liveView(options = {}) {
  return options.feedFresh === true && options.paused !== true && options.fictional !== true
    ? true
    : options.fictional === true && options.paused !== true;
}

function cap(item, options) {
  if (options.fictional === true) return item;
  if (options.paused === true || options.feedFresh !== true) {
    if (item.status === 'live' || item.status === 'queued') {
      return presentation('stale', options.paused
        ? 'View paused. This is the last sample, not a live claim.'
        : 'Feed is not fresh. This is not a live claim.');
    }
  }
  return item;
}

function text(value, max = 160) {
  return typeof value === 'string' && value.trim() && value.length <= max ? value.trim() : null;
}

function finiteAge(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function metric(label, value) {
  return {label, value: value == null || value === '' ? '—' : String(value)};
}

function step(id, label, key, reason, detail) {
  const face = presentation(key, reason);
  return {id, label, ...face, detail: detail || null};
}

function blankPipeline(note, {declared = false, incomplete = false} = {}) {
  return {declared, incomplete, note, steps: []};
}

function identity(fields) {
  return {
    machine: fields.machine ?? null,
    machineLabel: fields.machineLabel ?? 'Not reported',
    provider: fields.provider ?? null,
    providerLabel: fields.providerLabel ?? 'Not reported',
    model: fields.model ?? null,
    session: fields.session ?? null,
  };
}

function monitor(fields) {
  const face = fields.face;
  return {
    id: fields.id,
    kind: fields.kind,
    sessionKind: fields.sessionKind || null,
    title: fields.title,
    eyebrow: fields.eyebrow,
    status: face.status,
    glyph: face.glyph,
    word: face.word,
    reason: face.reason,
    fictional: fields.fictional === true,
    retained: fields.retained === true,
    identity: fields.identity,
    workload: fields.workload || null,
    analyses: fields.analyses || [],
    events: fields.events || [],
    pipeline: fields.pipeline,
    metrics: fields.metrics || [],
  };
}

const ANALYSIS_STATES = new Set(['pending', 'failed', 'skipped', 'unavailable', 'blocked', 'recorded', 'passed']);
const ANALYSIS_PAIRS = [['nisi', 'pre'], ['jev', 'pre'], ['nisi', 'post'], ['jev', 'post']];

function attemptToken(row) {
  const value = row?.attemptId ?? row?.attempt;
  if (typeof value === 'number' && Number.isInteger(value) && value >= 0 && value < 1000000) return String(value);
  if (typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,200}$/.test(value)) return value;
  return null;
}

function modelPair(row) {
  const requested = text(row?.requestedModel, 200) || text(row?.expectedModel, 200) || text(row?.model, 200);
  const served = text(row?.servedModel, 200);
  return {requestedModel: requested, servedModel: served};
}

function sessionTitle(machineLabel, model, role, runId) {
  return `${machineLabel} · ${model || 'model not reported'} · ${role} · ${String(runId).slice(-6)}`;
}

export function analysisSlots(records, {runId = null, attemptId = null} = {}) {
  const list = Array.isArray(records) ? records : [];
  return ANALYSIS_PAIRS.map(([tool, phase]) => {
    const found = list.find(row => row && row.tool === tool && row.phase === phase
      && attemptId != null && row.attemptId != null && String(row.attemptId) === String(attemptId)
      && (row.runId == null || row.runId === runId));
    if (!found || !ANALYSIS_STATES.has(found.state) || (found.state === 'passed' && !text(found.provenance, 240))) {
      return {
        tool, phase, state: 'unavailable', word: 'NOT AVAILABLE YET', runId, attemptId, evidenced: false, provenance: null,
        detail: attemptId == null
          ? 'Attempt was not reported, so this record stays unattached.'
          : found && found.state === 'passed'
            ? 'A passed analysis without provenance is not accepted.'
            : found
              ? 'Analysis record state was not pending, failed, skipped, unavailable, blocked, recorded, or passed.'
              : 'NOT AVAILABLE YET: no analysis producer receipt for this run, attempt, and phase.',
      };
    }
    return {
      tool, phase, state: found.state,
      runId: found.runId ?? runId,
      attemptId: found.attemptId ?? attemptId,
      evidenced: true,
      provenance: text(found.provenance, 240),
      verdict: text(found.verdict, 80),
      detail: text(found.detail, 240) || found.state,
    };
  });
}

function snapshotAnalyses(snapshot, workloadId) {
  if (!Array.isArray(snapshot?.analyses)) return [];
  return snapshot.analyses.filter(row => row && row.workloadId === workloadId);
}

function routeRows(snapshot) {
  const rows = snapshot?.pipeline?.pipelines;
  if (!Array.isArray(rows)) return [];
  return rows.filter(row => row && typeof row.runId === 'string' && ROUTE_ID.test(row.runId));
}

function jobRows(snapshot) {
  const rows = snapshot?.windowsJobs?.inFlight;
  if (!Array.isArray(rows)) return [];
  return rows.filter(row => row && typeof row.id === 'string' && JOB_ID.test(row.id));
}

function jobsClaimable(jobs, options) {
  if (!liveView(options) || !jobs || typeof jobs !== 'object') return false;
  if (jobs.liveAvailable !== true) return false;
  if (jobs.complete !== true || jobs.truncated === true || jobs.projectionComplete === false || jobs.inFlightTruncated === true) return false;
  if (options.fictional !== true) {
    if (Number.isFinite(options.snapshotAge) && options.snapshotAge < 0) return false;
    const elapsed = Number.isFinite(options.snapshotAge) ? Math.max(0, options.snapshotAge) : null;
    const age = Number.isFinite(jobs.sampledAt) ? (Number.isFinite(options.now) ? options.now : Date.now()/1000) - jobs.sampledAt
      : Number.isFinite(jobs.observationAgeSeconds) && elapsed !== null ? jobs.observationAgeSeconds + elapsed : null;
    if (age === null || age < 0 || age > 3) return false;
  }
  if (jobs.availability && jobs.availability.state === 'unknown') return false;
  return true;
}

function routeFace(row, options) {
  if (!row) return presentation('unknown', 'No route row.');
  if (row.recoveryRequired === true) return cap(presentation('blocked', 'Recovery is required on this route.'), options);
  if (!liveView(options)) return cap(presentation('live', 'Route listed.'), options);
  if (row.live === true && row.status === 'running') return presentation('live', 'Router lists this run as live.');
  if (row.status === 'waiting') return presentation('queued', 'Router lists this run as waiting.');
  if (row.status === 'admitting') return presentation('admitting', 'Router lists this run as being admitted.');
  if (row.status === 'running') return presentation('recorded', 'Status says running. The live lock was not verified in this sample.');
  if (row.status === 'changing') return presentation('unknown', 'Changing; checked again on the next sample.');
  return presentation('unknown', text(row.status) ? `Route status ${row.status}.` : 'Route status was not a known value.');
}

function macModels(snapshot) {
  return Array.isArray(snapshot?.models) ? snapshot.models.filter(row => row && row.host === 'mac') : [];
}

function localInventoryComplete(snapshot, options) {
  const source = (Array.isArray(snapshot?.sources) ? snapshot.sources : []).find(row => row?.id === 'lmstudio-api');
  return liveView(options) && source?.state === 'live' && source.modelsComplete === true
    && Number.isFinite(source.ageSeconds) && source.ageSeconds >= 0
    && source.ageSeconds + (Number.isFinite(options.snapshotAge) ? Math.max(0, options.snapshotAge) : 0) <= 3;
}

function localActivityComplete(snapshot, options) {
  return localInventoryComplete(snapshot, options) && snapshot.activityKnown === true && macModels(snapshot).every(row =>
    typeof row.loaded === 'boolean' && Number.isFinite(row.ageSeconds) && row.ageSeconds >= 0
    && row.ageSeconds + (Number.isFinite(options.snapshotAge) ? Math.max(0, options.snapshotAge) : 0) <= 3
    && (!row.loaded || ['idle', 'busy', 'generating'].includes(row.state)));
}

function macFace(snapshot, options) {
  const models = macModels(snapshot);
  if (!liveView(options)) return cap(presentation('live', 'Mac model activity.'), options);
  const active = models.filter(row => row.loaded === true && (row.state === 'generating' || row.state === 'busy')
    && finiteAge(row.ageSeconds) != null && row.ageSeconds >= 0
    && row.ageSeconds + (Number.isFinite(options.snapshotAge) ? Math.max(0, options.snapshotAge) : 0) <= 3);
  if (active.length) return presentation('live', localActivityComplete(snapshot, options)
    ? `${active.length} Mac model${active.length === 1 ? '' : 's'} reported generating or busy.`
    : 'Mac activity reported; total unknown because fresh, complete activity evidence is unavailable.');
  if (!models.length) return presentation('unknown', 'No Mac model rows in this sample.');
  if (!localActivityComplete(snapshot, options)) return presentation('unknown', 'Mac activity count unknown: fresh, complete activity evidence is unavailable.');
  if (models.every(row => row.loaded === false)) return presentation('idle', 'No Mac model is loaded in this sample.');
  if (models.some(row => row.loaded === true)) return presentation('idle', 'Mac models are loaded. None reported generating or busy.');
  return presentation('unknown', 'Mac residency was not boolean in this sample.');
}

function workerFace(worker, jobs, options) {
  if (!worker || typeof worker !== 'object') return presentation('unknown', 'No PC worker record in this sample.');
  const state = worker.state;
  if (!liveView(options)) return cap(presentation(state === 'stopped' ? 'stopped' : 'live', 'PC worker.'), options);
  if (jobsClaimable(jobs, options) && jobRows({windowsJobs: jobs}).length) {
    return presentation('live', 'A PC job is in flight in this sample.');
  }
  if (state === 'stopped') return presentation('stopped', text(worker.detail) || 'The worker reports it has stopped.');
  if (state === 'degraded') return presentation('degraded', text(worker.detail) || 'Heartbeat is fresh and no lane answers.');
  if (state === 'advertised') return presentation(jobsClaimable(jobs, options) ? 'idle' : 'unknown', jobsClaimable(jobs, options) ? 'Worker heartbeat is advertised; the fresh, complete job sample lists no in-flight job.' : 'Worker heartbeat is advertised; job activity is unknown without fresh, complete job evidence.');
  if (state === 'unknown' || state == null) return presentation('unknown', text(worker.detail) || 'PC worker state is unknown.');
  return presentation('unknown', 'PC worker state was not a known value.');
}

function component(snapshot, id) {
  const rows = snapshot?.components;
  if (!Array.isArray(rows)) return null;
  return rows.find(row => row && row.id === id) || null;
}

function nisiFace(snapshot, options) {
  const pipeline = snapshot.pipeline;
  const nisi = component(snapshot, 'nisi');
  const routes = routeRows(snapshot);
  const running = routes.some(row => routeFace(row, options).status === 'live');
  if (nisi?.state === 'unresolved' || pipeline?.recoveryRequired === true || pipeline?.status === 'recovery-required') {
    return cap(presentation('blocked', text(nisi?.detail) || 'Nisi record is pending recovery.'), options);
  }
  if (running) return presentation('live', 'At least one listed route is live.');
  if (!liveView(options)) return cap(presentation('live', 'Nisi route.'), options);
  if (pipeline?.status === 'installing') return presentation('unavailable', 'Router install is in progress.');
  if (pipeline?.status === 'idle' && nisi?.state === 'needs-action') {
    return presentation('blocked', text(nisi.detail) || 'Nisi needs action. That is not an idle success.');
  }
  if (pipeline?.status === 'idle') return presentation('idle', text(nisi?.detail) || 'No unresolved route record.');
  if (pipeline?.status === 'unknown' || pipeline?.status == null) {
    return presentation('unknown', text(pipeline?.reason) || text(nisi?.detail) || 'Route observation is unknown.');
  }
  if (pipeline?.status === 'queued') return presentation('queued', 'Primary route is queued.');
  if (nisi?.state === 'partial') return presentation('partial', text(nisi.detail) || 'Nisi pair is incomplete.');
  if (nisi?.state === 'needs-action') return cap(presentation('blocked', text(nisi.detail) || 'Nisi needs action.'), options);
  if (nisi?.state === 'ready') return presentation('idle', text(nisi.detail) || 'Nisi pair is ready. That is not generation.');
  return presentation('unknown', text(pipeline?.status) ? `Route status ${pipeline.status}.` : 'Route status is unknown.');
}

function clientFace(client, attributed, options) {
  const live = attributed.find(row => row.face.status === 'live');
  if (live) return presentation('live', `Work attributed to ${client.label || client.id} is listed live.`);
  const queued = attributed.find(row => row.face.status === 'queued');
  if (queued) return presentation('queued', `Work attributed to ${client.label || client.id} is queued.`);
  if (!liveView(options) && attributed.length) {
    return cap(presentation('stale', `Work is attributed to ${client.label || client.id}. The feed is not a live claim.`), options);
  }
  if (!liveView(options)) return cap(presentation('unknown', text(client.detail) || 'Client activity is unknown.'), options);
  if (client.model || (Array.isArray(client.models) && client.models.length)) {
    return presentation('unknown', text(client.detail) || 'Recorded model identity. Current activity is unknown.');
  }
  return presentation('unavailable', text(client.detail) || 'No supported model identity in this sample. Activity is unknown.');
}

function pipelineListNote(snapshot) {
  const pipeline = snapshot?.pipeline;
  if (!pipeline || typeof pipeline !== 'object') return {incomplete: true, note: 'No route pipeline object in this sample.'};
  if (!Array.isArray(pipeline.pipelines)) return {incomplete: true, note: 'Route list is missing from this sample.'};
  if (pipeline.pipelinesComplete !== true || pipeline.runsTruncated === true) {
    return {incomplete: true, note: 'Route list is incomplete. Only rows present in this sample are shown.'};
  }
  return {incomplete: false, note: 'Route rows are the ones present in this sample.'};
}

function routeStep(row, options) {
  const face = routeFace(row, options);
  const where = row.host === 'mac' ? 'Mac' : row.host === 'windows' ? 'PC' : 'host not reported';
  const who = text(row.client) || 'client not reported';
  return step(row.runId, row.runId, face.status === 'live' ? 'live'
    : face.status === 'queued' && face.word === 'ADMITTING' ? 'admitting'
    : face.status === 'queued' ? 'queued'
    : face.status === 'blocked' ? 'blocked'
    : face.status === 'stale' ? 'stale'
    : face.status === 'idle' ? 'recorded'
    : 'unknown', face.reason, `${where} · ${who}${text(row.stage) ? ` · ${row.stage}` : ''}`);
}

function jobStep(row, claimable) {
  const lane = row.lane === 'fast' || row.lane === 'deep' ? row.lane : null;
  const key = claimable ? 'live' : 'unknown';
  const reason = claimable ? 'In-flight PC job in this sample.' : 'Job row is present. Liveness was not claimable in this sample.';
  return step(row.id, row.id, key, reason, [lane ? `${lane} lane` : 'lane not reported', text(row.client) || 'client not reported', text(row.model) || 'model not reported'].join(' · '));
}

function declaredSteps(snapshot, options) {
  const steps = snapshot?.pipeline?.steps;
  if (!Array.isArray(steps)) return [];
  const stage = text(snapshot.pipeline.stage, 64);
  const running = nisiFace(snapshot, options).status === 'live';
  return steps.slice(0, 12).filter(item => item && typeof item.id === 'string').map(item => {
    if (item.state === 'blocked') return step(item.id, text(item.label, 80) || item.id, 'blocked', 'This step is marked blocked.', null);
    if (running && stage && item.id === stage) return step(item.id, text(item.label, 80) || item.id, 'live', 'Current stage label on a live route. Earlier steps are not marked complete.', null);
    return step(item.id, text(item.label, 80) || item.id, 'pending', 'Declared step. Pending is not success.', null);
  });
}

function macMonitor(snapshot, options) {
  const models = macModels(snapshot);
  const face = cap(macFace(snapshot, options), options);
  const inventoryKnown = localInventoryComplete(snapshot, options);
  const loaded = inventoryKnown && models.every(row => typeof row.loaded === 'boolean') ? models.filter(row => row.loaded === true).length : null;
  return monitor({
    id: 'machine:mac',
    kind: 'machine',
    title: snapshot.host === 'mac' ? 'This Mac' : 'Mac',
    eyebrow: 'MACHINE',
    face,
    fictional: options.fictional,
    identity: identity({
      machine: 'mac',
      machineLabel: snapshot.host === 'mac' ? 'This Mac' : 'Mac',
      provider: 'lmstudio',
      providerLabel: 'LM Studio',
      model: models.find(row => row.loaded === true)?.id || null,
      session: null,
    }),
    pipeline: blankPipeline('Local model residency is not a Nisi route pipeline. Per-model state is the reading.'),
    metrics: [
      metric('Mac models in sample', inventoryKnown ? models.length : 'Unknown · fresh, complete inventory unavailable'),
      metric('Loaded', loaded == null || !liveView(options) ? null : loaded),
      metric('Generating or busy', localActivityComplete(snapshot, options) ? models.filter(row => row.loaded === true && (row.state === 'generating' || row.state === 'busy')).length : 'Unknown · complete activity evidence unavailable'),
    ],
  });
}

function laneMetric(worker) {
  const lanes = worker?.lanes;
  if (!lanes || typeof lanes !== 'object') return '—';
  const parts = [];
  for (const name of ['fast', 'deep']) {
    const lane = lanes[name];
    if (!lane || typeof lane !== 'object') {
      parts.push(`${name} not reported`);
      continue;
    }
    const up = lane.up === true ? 'up' : lane.up === false ? 'down' : 'unknown';
    const slots = Number.isInteger(lane.slotsBusy) && Number.isInteger(lane.slotsTotal) ? `${lane.slotsBusy}/${lane.slotsTotal}` : 'slots —';
    parts.push(`${name} ${up} ${slots}`);
  }
  return parts.join(' · ');
}

function pcMonitor(snapshot, options) {
  const replacement = registryMachineRows(snapshot).find(row => row.replaces === 'legacy-pc');
  if (replacement) return registryMonitor(replacement, options, true);
  const worker = snapshot.windowsWorker;
  const jobs = snapshot.windowsJobs;
  const face = cap(workerFace(worker, jobs, options), options);
  const rows = jobRows(snapshot);
  const list = pipelineListNote(snapshot);
  return monitor({
    id: 'machine:pc',
    kind: 'machine',
    title: 'PC LLM',
    eyebrow: 'MACHINE',
    face,
    fictional: options.fictional,
    identity: identity({
      machine: 'windows',
      machineLabel: 'Windows PC',
      provider: 'llama',
      providerLabel: 'PC lanes',
      model: text(rows[0]?.model) || text(worker?.lanes?.fast?.model) || text(worker?.lanes?.deep?.model),
      session: rows[0]?.id || null,
    }),
    pipeline: {
      declared: rows.length > 0,
      incomplete: jobs?.complete === false,
      note: rows.length ? 'In-flight PC job records in this sample. Settled history stays on Activity.' : jobsClaimable(jobs, options) ? 'No in-flight PC job in this fresh, complete sample.' : 'PC job count unknown: fresh, complete job evidence is unavailable.',
      steps: rows.map(row => jobStep(row, jobsClaimable(jobs, options))),
    },
    metrics: [
      metric('Worker', text(worker?.state) || 'unknown'),
      metric('Lanes', laneMetric(worker)),
      metric('In-flight jobs', jobsClaimable(jobs, options) ? rows.length : 'Unknown · fresh, complete job evidence unavailable'),
      metric('Route list', list.incomplete ? 'incomplete' : 'present'),
      ...laneModelMetrics(worker),
    ],
  });
}

function registryMonitor(row, options, pc = false) {
  const view = registryMachineView(row, {fresh: liveView(options), now: options.now, snapshotAge: options.snapshotAge});
  const key = view.health === 'ok' ? 'ok' : view.health === 'degraded' ? 'degraded' : 'unknown';
  return monitor({
    id: pc ? 'machine:pc' : `machine:${row.host || row.id}`,
    kind: 'machine', title: pc ? 'PC LLM' : view.label, eyebrow: 'MACHINE',
    face: presentation(key, `${view.label} · ${view.detail}. Configured lane health; inference activity is not measured.`),
    fictional: options.fictional,
    identity: identity({machine: row.host || row.id, machineLabel: view.label, provider: 'registry', providerLabel: 'Registry lanes', model: null}),
    pipeline: {
      declared: view.lanes.length > 0, incomplete: false,
      note: 'Server-reported lane identities. Reachability is not an inference run or whole-machine inventory.',
      steps: view.lanes.map(lane => step(lane.id, lane.label, lane.health === 'ok' ? 'ok' : lane.health === 'degraded' ? 'degraded' : 'unknown',
        lane.reason ? lane.reason.replaceAll('_', ' ') : null)),
    },
    metrics: [metric('Lanes', view.detail), ...view.lanes.map(lane => {
      const model = registryModelName(lane.modelId);
      return {...metric(`${lane.label} model`, model.label), title: model.title};
    })],
  });
}

function nisiMonitor(snapshot, options) {
  const face = cap(nisiFace(snapshot, options), options);
  const list = pipelineListNote(snapshot);
  const jev = component(snapshot, 'jev');
  const routes = routeRows(snapshot).map(row => routeStep(row, options));
  const declared = declaredSteps(snapshot, options);
  return monitor({
    id: 'route:nisi',
    kind: 'route',
    title: 'Nisi Inference',
    eyebrow: 'ROUTE',
    face,
    fictional: options.fictional,
    identity: identity({
      machine: text(routeRows(snapshot)[0]?.host) === 'windows' ? 'windows' : text(routeRows(snapshot)[0]?.host) === 'mac' ? 'mac' : snapshot.host || null,
      machineLabel: text(routeRows(snapshot)[0]?.host) === 'windows' ? 'Windows PC' : text(routeRows(snapshot)[0]?.host) === 'mac' ? 'Mac' : 'Host not a single value',
      provider: 'nisi',
      providerLabel: 'Nisi',
      model: text(component(snapshot, 'nisi')?.authorModel) || null,
      session: text(snapshot.pipeline?.runId, 80),
    }),
    pipeline: {
      declared: declared.length > 0 || routes.length > 0,
      incomplete: list.incomplete,
      note: `${list.note} Jev state: ${text(jev?.state) || 'not in this sample'}. ${text(jev?.detail) || 'Jev generation is not observed here.'}`,
      steps: [...declared, ...routes],
    },
    metrics: [
      metric('Route status', text(snapshot.pipeline?.status) || 'unknown'),
      metric('Nisi', text(component(snapshot, 'nisi')?.state) || 'not in sample'),
      metric('Jev', text(jev?.state) || 'not in sample'),
      metric('Listed routes', liveView(options) && !list.incomplete ? routes.length : 'Unknown · route coverage incomplete or stale'),
    ],
  });
}

function attributed(snapshot, clientId, options) {
  const routes = routeRows(snapshot).filter(row => row.client === clientId).map(row => ({kind: 'route', face: routeFace(row, options), row}));
  const jobs = jobRows(snapshot).filter(row => row.client === clientId).map(row => ({
    kind: 'job',
    face: jobsClaimable(snapshot.windowsJobs, options) ? presentation('live', 'In-flight job attributed to this client.') : presentation('unknown', 'Job attributed to this client. Liveness was not claimable.'),
    row,
  }));
  return [...routes, ...jobs];
}

function clientMonitor(snapshot, client, options) {
  const label = text(client.label, 40) || client.id;
  const work = attributed(snapshot, client.id, options);
  const face = cap(clientFace(client, work, options), options);
  const model = text(client.model, 200) || text(client.models?.[0]?.id, 200);
  return monitor({
    id: `client:${client.id}`,
    kind: 'client',
    title: label,
    eyebrow: 'CLIENT',
    face,
    fictional: options.fictional,
    identity: identity({
      machine: null,
      machineLabel: 'Not a machine',
      provider: client.id,
      providerLabel: label,
      model,
      session: work[0]?.row.runId || work[0]?.row.id || null,
    }),
    pipeline: {
      declared: work.length > 0,
      incomplete: pipelineListNote(snapshot).incomplete && work.some(item => item.kind === 'route'),
      note: work.length
        ? 'Routes and in-flight PC jobs whose client field matches this identity.'
        : 'No route or in-flight PC job in this sample names this client. A recorded model is not live generation.',
      steps: work.map(item => item.kind === 'route'
        ? routeStep(item.row, options)
        : jobStep(item.row, jobsClaimable(snapshot.windowsJobs, options))),
    },
    metrics: [
      metric('Model identity', model),
      metric('Identity state', text(client.modelState) || 'unknown'),
      metric('Activity field', text(client.activity) || 'unknown'),
      metric('Attributed rows', work.length),
    ],
  });
}

function sessionMonitors(snapshot, options) {
  const sessions = [];
  for (const row of routeRows(snapshot)) {
    const face = cap(routeFace(row, options), options);
    const active = row.live === true || row.status === 'running' || row.status === 'waiting' || row.status === 'admitting' || row.recoveryRequired === true;
    if (!active) continue;
    const who = text(row.client) || 'Route';
    const models = modelPair(row);
    const attemptId = attemptToken(row);
    const machineLabel = row.host === 'mac' ? 'Mac' : row.host === 'windows' ? 'Windows PC' : 'Host not reported';
    const hostKey = row.host === 'mac' || row.host === 'windows' ? row.host : 'unscoped';
    const workloadId = `route:${hostKey}:${row.runId}`;
    sessions.push(monitor({
      id: workloadId,
      kind: 'session',
      sessionKind: 'route',
      title: sessionTitle(machineLabel, models.requestedModel, who, row.runId),
      eyebrow: 'SESSION',
      face,
      fictional: options.fictional,
      workload: {id: workloadId, runId: row.runId, attemptId, ...models, retained: false},
      analyses: analysisSlots(snapshotAnalyses(snapshot, workloadId), {runId: row.runId, attemptId}),
      identity: identity({
        machine: row.host === 'mac' || row.host === 'windows' ? row.host : null,
        machineLabel,
        provider: text(row.client),
        providerLabel: text(row.client) || 'Client not reported',
        model: models.requestedModel,
        session: row.runId,
      }),
      pipeline: {
        declared: true,
        incomplete: pipelineListNote(snapshot).incomplete,
        note: 'One router pipeline row. Stage labels are checkpoints, not proof that earlier stages finished.',
        steps: [routeStep(row, options)],
      },
      metrics: [
        metric('Run', row.runId),
        metric('Status', text(row.status) || 'unknown'),
        metric('Live lock', row.live === true ? 'yes' : row.live === false ? 'no' : 'not reported'),
        metric('Age seconds', finiteAge(row.ageSeconds)),
      ],
    }));
  }
  const claimable = jobsClaimable(snapshot.windowsJobs, options);
  for (const row of jobRows(snapshot)) {
    const face = cap(claimable ? presentation('live', 'In-flight PC job.') : presentation('unknown', 'In-flight row present. Liveness was not claimable.'), options);
    const models = modelPair(row);
    const attemptId = attemptToken(row);
    const workloadId = `job:${row.id}`;
    const lane = row.lane === 'fast' || row.lane === 'deep' ? row.lane : 'lane not reported';
    sessions.push(monitor({
      id: workloadId,
      kind: 'session',
      sessionKind: 'job',
      title: sessionTitle('Windows PC', models.requestedModel, lane, row.id),
      eyebrow: 'SESSION',
      face,
      fictional: options.fictional,
      workload: {id: workloadId, runId: row.id, attemptId, ...models, retained: false},
      analyses: analysisSlots(snapshotAnalyses(snapshot, workloadId), {runId: row.id, attemptId}),
      identity: identity({
        machine: 'windows',
        machineLabel: 'Windows PC',
        provider: text(row.client),
        providerLabel: text(row.client) || 'Client not reported',
        model: models.requestedModel,
        session: row.id,
      }),
      pipeline: {
        declared: true,
        incomplete: snapshot.windowsJobs?.complete === false,
        note: 'One in-flight PC job. This view does not cancel it.',
        steps: [jobStep(row, claimable)],
      },
      metrics: [
        metric('Job', row.id),
        metric('Lane', row.lane === 'fast' || row.lane === 'deep' ? row.lane : null),
        metric('Model', text(row.model)),
        metric('Age seconds', finiteAge(row.ageSeconds)),
      ],
    }));
  }
  return sessions;
}

function wantsMac(snapshot) {
  return snapshot.host === 'mac' || macModels(snapshot).length > 0
    || (Array.isArray(snapshot.machines) && snapshot.machines.some(row => row && (row.platform === 'macos' || row.platform === 'mac' || row.host === 'mac')));
}

function peerMacMonitor(snapshot, options) {
  const peer = snapshot.macPeer;
  if (snapshot.host !== 'windows' || !peer || typeof peer !== 'object') return null;
  const detail = text(peer.detail, 240);
  let face;
  if (!liveView(options)) face = cap(presentation('stale', detail || 'Mac peer observation is not a live claim.'), options);
  else if (peer.state === 'linked') face = presentation('recorded', detail || 'Share report linked. Not verified and not a LAN probe.');
  else if (peer.reason === 'stale') face = presentation('stale', detail || 'Mac report is stale.');
  else if (peer.reason === 'no-report' || peer.reason === 'unconfigured' || peer.reason === 'config-unreadable') face = presentation('unavailable', detail || 'Mac peer is not available.');
  else if (peer.reason === 'refused' || peer.reason === 'timeout' || peer.reason === 'unreachable' || peer.reason === 'unresolved') face = presentation('disconnected', detail || 'Mac peer is disconnected.');
  else face = presentation('unknown', detail || 'Mac peer state is unknown.');
  const models = Array.isArray(peer.models) ? peer.models : [];
  return monitor({
    id: 'machine:mac',
    kind: 'machine',
    title: 'Mac peer',
    eyebrow: 'MACHINE',
    face,
    fictional: options.fictional,
    identity: identity({
      machine: 'mac',
      machineLabel: text(peer.configuredName, 80) || text(peer.name, 80) || 'Mac',
      provider: 'share',
      providerLabel: peer.via === 'sharedchami' ? 'SharedChami presence' : 'Mac presence',
      model: text(models[0]?.id, 200),
      session: null,
    }),
    pipeline: blankPipeline('A share or presence record is not a Mac inference session and does not prove generation.'),
    metrics: [
      metric('State', text(peer.state) || 'unknown'),
      metric('Verified', peer.verified === true ? 'yes' : peer.verified === false ? 'no' : null),
      metric('Loaded count', Number.isInteger(peer.loadedCount) ? peer.loadedCount : null),
      metric('Reason', text(peer.reason, 80)),
    ],
  });
}

function laneModelMetrics(worker) {
  const lanes = worker?.lanes;
  const rows = [];
  if (!lanes || typeof lanes !== 'object') return rows;
  for (const name of ['fast', 'deep']) {
    const lane = lanes[name];
    if (!lane || typeof lane !== 'object') continue;
    rows.push(metric(`${name} requested`, text(lane.model, 200) || text(lane.expectedModel, 200)));
    rows.push(metric(`${name} served`, text(lane.servedModel, 200)));
  }
  if (typeof worker.lanesError === 'string') rows.push(metric('Lane error', text(worker.lanesError, 200)));
  return rows;
}

function wantsPc(snapshot) {
  return registryMachineRows(snapshot).some(row => row.replaces === 'legacy-pc')
    || snapshot.host === 'windows' || (snapshot.windowsWorker && typeof snapshot.windowsWorker === 'object')
    || (Array.isArray(snapshot.models) && snapshot.models.some(row => row && row.host === 'windows'));
}

function fleetOptions(options = {}) {
  return {now: options.now, fresh: options.feedFresh === true && options.paused !== true};
}

function factFace(fact) {
  const state = text(fact?.state, 80) || 'unknown';
  const key = state === 'stale' ? 'stale' : state === 'endpoint unavailable' ? 'down'
    : state === 'endpoint answering' ? 'ok' : 'unknown';
  const age = Number.isFinite(fact?.ageSeconds) ? ` · ${Math.floor(fact.ageSeconds)}s old` : '';
  return {...presentation(key, `${fact?.reason || (state === 'endpoint answering' ? 'Endpoint identity observed; host, hardware and inference activity unknown' : 'Evidence scope is unknown')}${age}`), word: state.toUpperCase()};
}

function machineFace(fact, lanes) {
  const coverage = fleetMachineCoverage(fact, lanes);
  return {...presentation(coverage.state, coverage.detail), word: coverage.text, glyph: coverage.glyph};
}

function fleetMachineMonitor(fact, lanes, options) {
  const children = lanes.filter(lane => lane.parentId === fact.id);
  const face = machineFace(fact, lanes);
  return monitor({
    id: `machine:${fact.id}`, kind: 'machine', title: fact.label || fact.id, eyebrow: 'MACHINE',
    face, fictional: options.fictional,
    identity: identity({machine: fact.id, machineLabel: fact.label || fact.id,
      provider: 'fleet', providerLabel: fact.source || 'Source unknown'}),
    pipeline: {declared: children.length > 0, incomplete: fact.completeness !== 'complete',
      note: 'Endpoint identity observations do not establish host, hardware or inference health.',
      steps: children.map(lane => ({id: lane.id, label: lane.label || lane.id, ...factFace(lane),
        detail: registryModelName(lane.modelId || lane.evidence?.modelId).label || null}))},
    metrics: [metric('ID', fact.id), metric('Source', fact.source), metric('Completeness', fact.completeness),
      ...children.map(lane => metric(lane.label || lane.id,
        `${lane.state}${lane.modelId || lane.evidence?.modelId ? ` · reported model ${registryModelName(lane.modelId || lane.evidence.modelId).label}` : ''}`))],
  });
}

function overview(children, snapshot, options) {
  const summary = fleetSummary(snapshot, fleetOptions(options));
  const blocked = children.filter(item => item.status === 'blocked');
  const scoped = blocked.map(item => `${item.title} blocked: ${item.reason || 'reason unknown'}`).join('; ');
  const reason = [summary.text, scoped].filter(Boolean).join(' · ');
  const face = {...presentation(summary.state === 'stale' ? 'stale' : 'unknown', reason),
    word: summary.state === 'coverage' ? summary.headline : summary.state === 'stale' ? 'STALE' : 'UNKNOWN',
    glyph: summary.counts.answering > 0 ? '■' : '?'};
  const list = snapshot ? pipelineListNote(snapshot) : {incomplete: true, note: 'No snapshot.'};
  return monitor({
    id: 'all', kind: 'overview', title: 'All inference', eyebrow: 'OVERVIEW', face,
    fictional: options.fictional,
    identity: identity({machine: text(snapshot?.host), machineLabel: 'Observer sample',
      provider: null, providerLabel: 'Scoped endpoint evidence'}),
    pipeline: {declared: children.length > 0, incomplete: list.incomplete,
      note: 'Workflows keep their own state. Endpoint coverage does not prove activity or readiness.',
      steps: children.map(item => ({id: item.id, label: item.title,
        status: item.status, word: item.word, glyph: item.glyph, reason: item.reason, detail: item.eyebrow}))},
    metrics: [metric('Endpoint coverage', summary.text),
      metric('Busy models', summary.counts.busy),
      metric('Route list', snapshot ? (list.incomplete ? 'incomplete' : 'present') : null)],
  });
}

function processReading(snapshot, monitor, options) {
  if (!snapshot) {
    return {word: monitor.word, matrix: 'Untested', detail: 'No snapshot. Process liveness was not measured.'};
  }
  if (options.fictional === true) {
    return {word: monitor.word, matrix: 'Observed', detail: 'Fictional sample. The process word is not a live claim.'};
  }
  if (options.feedFresh !== true || options.paused === true) {
    return {word: monitor.word, matrix: 'Observed', detail: 'A sample exists and the feed is not a fresh live claim. Liveness is not readiness.'};
  }
  return {word: monitor.word, matrix: 'Observed', detail: 'Process word from this sample. A fresh feed is not lane readiness, attachment, or acceptance.'};
}

function laneReading(snapshot, options = {}, selected = null) {
  if (snapshot?.fleetState) {
    const allLanes = fleetLanes(snapshot, fleetOptions(options));
    const lanes = selected?.kind === 'machine' && selected.identity?.provider === 'fleet'
      ? allLanes.filter(lane => lane.parentId === selected.identity.machine) : allLanes;
    return lanes.some(lane => lane.state === 'endpoint answering')
      ? {word: 'ENDPOINT ANSWERING', matrix: 'Observed', detail: 'A lane answered its endpoint probe. Host, hardware and inference readiness are not measured.'}
      : {word: lanes.some(lane => lane.state === 'stale') ? 'STALE' : 'UNKNOWN', matrix: 'Untested', detail: 'No fresh answering endpoint evidence. Host, hardware and readiness remain unknown.'};
  }
  if (!snapshot) return {word: 'NOT MEASURED', matrix: 'Untested', detail: 'No snapshot.'};
  if (options.fictional !== true) return {word: 'UNKNOWN', matrix: 'Untested', detail: 'Fleet evidence has not been published.'};
  const registry = registryMachineRows(snapshot);
  if (registry.some(row => row.replaces === 'legacy-pc')) {
    const named = registry.some(row => Array.isArray(row.lanes) && row.lanes.some(lane => text(lane?.modelId, 1024)));
    return named
      ? {word: 'INVENTORY', matrix: 'Observed', detail: 'Registry lanes report model identities. Reachability and identity are not inference readiness or acceptance.'}
      : {word: 'UNKNOWN', matrix: 'Untested', detail: 'Registry lane identity is unknown. Legacy worker readings do not replace it.'};
  }
  const lanes = snapshot.windowsWorker?.lanes;
  let named = false;
  let mismatch = false;
  let servedReady = false;
  let upWithoutServed = false;
  if (lanes && typeof lanes === 'object') {
    for (const name of ['fast', 'deep']) {
      const lane = lanes[name];
      if (!lane || typeof lane !== 'object') continue;
      const expected = text(lane.model, 200) || text(lane.expectedModel, 200);
      const served = text(lane.servedModel, 200);
      if (expected || served) named = true;
      if (expected && served && expected !== served) mismatch = true;
      if (served && (!expected || expected === served) && lane.activityKnown === true) servedReady = true;
      if (lane.up === true && !served) upWithoutServed = true;
    }
  }
  const models = macModels(snapshot);
  if (models.length) named = true;
  if (mismatch) {
    return {word: 'MISMATCH', matrix: 'Blocked', detail: 'Requested lane model and served model differ. A model list is not readiness.'};
  }
  if (upWithoutServed) {
    return {word: 'NOT READY', matrix: 'Blocked', detail: 'A lane reported up without a served model. Up is not readiness.'};
  }
  if (servedReady) {
    return {word: 'READY', matrix: 'Observed', detail: 'Served model matches and the lane reported activityKnown. This is not a run and not acceptance.'};
  }
  if (named) {
    return {word: 'INVENTORY', matrix: 'Observed', detail: 'Names or residency are in the sample. Inventory is not lane readiness.'};
  }
  return {word: 'UNKNOWN', matrix: 'Untested', detail: 'Lane readiness was not measured in this sample.'};
}

function attachmentReading(snapshot, monitor, options) {
  const workload = monitor.workload;
  const slots = Array.isArray(monitor.analyses) ? monitor.analyses : [];
  const matched = slots.filter(slot => slot && slot.evidenced === true && workload?.attemptId
    && slot.attemptId != null && String(slot.attemptId) === String(workload.attemptId));
  if (monitor.kind === 'session') {
    if (!workload?.attemptId) {
      return {word: 'UNATTACHED', matrix: 'Untested', detail: 'Attempt was not reported, so records stay unattached.'};
    }
    if (!matched.length) {
      return {word: 'NOT AVAILABLE YET', matrix: 'Untested', detail: 'No analysis producer receipt matches this run and attempt.'};
    }
    return {word: 'ATTACHED', matrix: 'Observed', detail: 'Analysis matches this attempt. Attachment is not a Jev pass.'};
  }
  const routes = snapshot ? routeRows(snapshot) : [];
  const jobs = snapshot ? jobRows(snapshot) : [];
  if (!routes.length && !jobs.length) {
    const evidence = activityEvidence(snapshot?.activity, options);
    return evidence.complete && evidence.count === 0 && jobsClaimable(snapshot?.windowsJobs, options)
      ? {word: 'NO RUN', matrix: 'Observed', detail: 'Fresh, complete router and job evidence lists no current work.'}
      : {word: 'UNKNOWN', matrix: 'Untested', detail: 'Run absence is unknown: fresh, complete router and job evidence is unavailable.'};
  }
  return {word: 'UNATTACHED', matrix: 'Untested', detail: 'Runs are listed. This monitor is not an attempt-attached session.'};
}

function nisiReading(snapshot) {
  const nisi = snapshot ? component(snapshot, 'nisi') : null;
  if (!nisi) {
    return {id: 'nisi', label: 'Nisi', matrix: 'Untested', word: 'NOT MEASURED', detail: 'Nisi was not in this sample.', evidence: 'components.nisi'};
  }
  if (nisi.state === 'needs-action') {
    return {
      id: 'nisi', label: 'Nisi', matrix: 'Blocked', word: 'NEEDS ACTION',
      detail: text(nisi.detail) || 'Nisi needs action. That is not running and not a pass.',
      evidence: 'components.nisi.state',
    };
  }
  return {
    id: 'nisi', label: 'Nisi', matrix: 'Observed', word: String(nisi.state || 'unknown').toUpperCase(),
    detail: `${text(nisi.detail) || 'Nisi state is in the sample.'} This is not running proof and not a pass.`,
    evidence: 'components.nisi.state',
  };
}

function jevReading(monitor) {
  const slots = Array.isArray(monitor.analyses) ? monitor.analyses : [];
  const gate = jevGate(slots);
  const pre = slots.find(slot => slot.tool === 'jev' && slot.phase === 'pre');
  const post = slots.find(slot => slot.tool === 'jev' && slot.phase === 'post');
  const evidence = `pre attempt ${pre?.attemptId || 'not reported'} · post attempt ${post?.attemptId || 'not reported'}`;
  if (!slots.length || !gate.available) {
    return {id: 'jev', label: 'Jev correlation', matrix: 'Untested', word: 'NOT AVAILABLE YET', detail: 'No attempt-bound Jev pre/post producer receipts are available.', evidence};
  }
  if (gate.open) {
    return {id: 'jev', label: 'Jev correlation', matrix: 'Observed', word: 'GATE OPEN', detail: 'Both phases are passed with provenance and verdict PASS.', evidence};
  }
  return {id: 'jev', label: 'Jev correlation', matrix: 'Blocked', word: 'GATE BLOCKED', detail: gate.blocker, evidence};
}

const ACCEPTANCE_LEDGER = [
  {gate: 'Jev post', env: 'Mac', state: 'recorded', verdict: '0/4 flags', time: '2026-11-06 09:17 PST', receipt: 'jaryo-codex0917-mac-20261106-092057', next: 'Not PASS'},
  {gate: 'Jev post', env: 'PC', state: 'recorded', verdict: 'not a pass', time: '2026-11-06 03:29 and 09:17 PST', receipt: 'jaryo-auth0329-pc-20261106-033057', next: 'Remains open'},
  {gate: 'Jev post', env: 'Mac Astra', state: 'recorded', verdict: '1/4 FLAG', time: '2026-11-06 11:03 PST', receipt: 'astra1103-mac-A-20261106-110643', next: 'Not PASS'},
  {gate: 'Jev post', env: 'PC Astra', state: 'recorded', verdict: 'not a pass', time: '2026-11-06 11:12 PST', receipt: 'astra1103-pc-A2-20261106-111209', next: 'Remains open'},
  {gate: 'Mac archive', env: 'Mac Astra', state: 'recorded', verdict: 'not acceptance', time: '2026-11-06 11:41 file stamp', receipt: 'astra1103-mac-DESK-20261106-114103', next: 'Xcode 27B52 is the default. The distribution script pins 27A266a. No archive.'},
  {gate: 'Web sync', env: 'Mac Astra', state: 'recorded', verdict: 'not acceptance', time: '2026-11-06 11:49 file stamp', receipt: 'astra1103-mac-SYNC-20261106-114919', next: 'Local App Resources web is present. Fresh archive is unproven.'},
  {gate: 'Android contract', env: 'PC Astra', state: 'recorded', verdict: 'not acceptance', time: '2026-11-06 11:41 file stamp', receipt: 'astra1103-pc-AND-20261106-114125', next: 'Compose proposal. This client stays Java.'},
  {gate: 'Windows MSI', env: 'PC Astra', state: 'recorded', verdict: 'not acceptance', time: '2026-11-06 11:49 file stamp', receipt: 'astra1103-pc-WIN-20261106-114930', next: 'Visual Studio 18, MSBuild, and the dotnet SDK were present. WiX and the PC checkout were missing. The Windows review is not a pass.'},
  {gate: 'Mac unsigned Debug', env: 'Mac', state: 'recorded', verdict: '0/4', time: '2026-11-06 11:54 file stamp', receipt: 'mac-unsigned-build-20261106-115509', next: 'Exit 0 on recorded web revision 317aad2; current acceptance is unknown.'},
  {gate: 'Windows node tests', env: 'PC', state: 'recorded', verdict: 'not acceptance', time: '2026-11-06 11:58 file stamp', receipt: 'pc-win-node-20261106', next: '123 tests passed, excluding pipe-host. pipe-host failed with listen EACCES. The Windows review is not a pass.'},
  {gate: 'Android parser', env: 'Mac Astra', state: 'recorded', verdict: '0/4', time: '2026-11-06 11:56 file stamp', receipt: 'astra1103-mac-ANDJAVA-20261106-115608', next: 'Not PASS. Missing payload data stays UNKNOWN.'},
  {gate: 'Mac sync', env: 'Mac', state: 'recorded', verdict: '0/4', time: '2026-11-06 12:09 file stamp', receipt: 'mac-jaryo-sync-52e902d-20261106-120956', next: 'sync-web exit 1. simple-view.mjs is absent. Wrapper rules were not changed. Not PASS.'},
  {gate: 'Windows installer', env: 'PC', state: 'recorded', verdict: 'not acceptance', time: '2026-11-06 12:16 file stamp', receipt: 'pc-52e902d-windows-tests', next: '532 tests, 14 failed before the jaryo directory allowlist fix. The Windows review is not a pass.'},
];

const ACCEPTANCE_ROW = {
  id: 'acceptance',
  label: 'Acceptance history · 2026-11-06',
  matrix: 'Observed',
  word: 'RECORDED',
  detail: 'The Windows review is not a pass. The Mac Jev check on 6 Nov 2026 at 09:17 had no passing flags. The Astra Mac check had one flag of four. The unsigned Debug build also had no passing flags, and that web tree is not this checkout. A PC analysis marked OK is not app acceptance. Exit 0, STATUS OK, and COMPLETED are not a pass.',
  evidence: 'jaryo-codex0917-mac-20261106-092057; astra1103-pc-A2-20261106-111209',
  ledger: ACCEPTANCE_LEDGER,
};

export function evidenceFor(snapshot, monitor, options = {}) {
  const process = processReading(snapshot, monitor, options);
  const lane = laneReading(snapshot, options, monitor);
  const attachment = attachmentReading(snapshot, monitor, options);
  return {
    badges: {process: process.word, lane: lane.word, attachment: attachment.word},
    rows: [
      {id: 'process', label: 'Process liveness', word: process.word, matrix: process.matrix, detail: process.detail, evidence: 'snapshot feed'},
      {id: 'lane', label: 'Lane readiness', word: lane.word, matrix: lane.matrix, detail: lane.detail, evidence: 'lane model versus served model'},
      {id: 'attach', label: 'Run attached', word: attachment.word, matrix: attachment.matrix, detail: attachment.detail, evidence: monitor.workload?.id || monitor.id},
      nisiReading(snapshot),
      jevReading(monitor),
    ],
  };
}

/** Dated source records belong to history; they never block a current run. */
export function historicalAcceptance() {
  return {...ACCEPTANCE_ROW, ledger: ACCEPTANCE_LEDGER.map(row => ({...row}))};
}

const RUN_PHASES = [['jev', 'pre'], ['nisi', 'journal'], ['jev', 'post']];
const tokenCount = value => Number.isSafeInteger(value) && value >= 0 ? value : null;

/** Receipt facts stay within one producer-bound run attempt. */
export function runEvidence(run) {
  const runId = text(run?.runId, 100), attemptId = attemptToken(run);
  const calls = (Array.isArray(run?.calls) ? run.calls : []).map(call => {
    const bound = Boolean(runId && attemptId && call?.parentRunId === runId && attemptToken(call) === attemptId);
    const pair = modelPair(call);
    const usage = bound && call?.usage && typeof call.usage === 'object' ? call.usage : {};
    const inputTokens = tokenCount(usage.inputTokens), outputTokens = tokenCount(usage.outputTokens), totalTokens = tokenCount(usage.totalTokens);
    const measured = inputTokens !== null && outputTokens !== null && totalTokens !== null && Number.isSafeInteger(inputTokens + outputTokens) && totalTokens === inputTokens + outputTokens;
    return {...call, ...pair, bound, servedModel: bound ? pair.servedModel : null,
      inputTokens: measured ? inputTokens : null, outputTokens: measured ? outputTokens : null,
      totalTokens: measured ? totalTokens : null};
  });
  const metered = calls.filter(call => call.bound && call.inputTokens !== null && call.outputTokens !== null
    && call.totalTokens === call.inputTokens + call.outputTokens).length;
  const expected = tokenCount(run?.usageCoverage?.expectedCallCount);
  const complete = Boolean(attemptId && run?.usageCoverage?.complete === true && expected !== null
    && expected === calls.length && metered === expected);
  const phases = RUN_PHASES.map(([tool, phase]) => {
    const record = (Array.isArray(run?.analysisSlots) ? run.analysisSlots : []).find(slot => slot?.tool === tool
      && slot.phase === phase && slot.runId === runId && attemptId !== null && attemptToken(slot) === attemptId);
    if (!record || record.evidenced !== true || record.state === 'unavailable') return {
      tool, phase, word: 'NOT AVAILABLE YET', detail: text(record?.detail, 300) || 'No attempt-bound producer receipt is available.',
    };
    return {tool, phase, word: String(record.state || 'unknown').toUpperCase(), detail: text(record.detail, 300)};
  });
  const recordedAt = Number.isFinite(run?.recordedAt) ? new Date(run.recordedAt * 1000) : null;
  return {runId, attemptId, calls, phases, complete,
    recordedAt: recordedAt && Number.isFinite(recordedAt.getTime()) ? recordedAt.toISOString() : null,
    usage: complete ? `${calls.reduce((sum, call) => sum + call.totalTokens, 0)} tokens` : 'Unknown',
    coverage: `${metered} of ${calls.length} reported calls have measured usage; expected calls ${expected === null ? 'unknown' : expected}. ${complete ? 'Complete attempt coverage.' : text(run?.usageCoverage?.detail, 300) || 'Whole-run usage is unknown.'}`,
    reason: [attemptId ? null : 'Attempt identity unavailable; receipts remain unattached.',text(run?.livenessReason,240)].filter(Boolean).join(' ') || null};
}

/** Freshness is producer time, independently of a fresh transport connection. */
export function activityEvidence(activity, options = {}) {
  const now = Number.isFinite(options.now) ? options.now : Date.now() / 1000;
  const age = Number.isFinite(activity?.sampledAt) ? now - activity.sampledAt : null;
  const maxAge = Number.isFinite(activity?.maxAgeSeconds) && activity.maxAgeSeconds > 0 ? Math.min(3, activity.maxAgeSeconds) : 3;
  const fresh = liveView(options) && age !== null && age >= -CLOCK_SKEW_SECONDS && age <= maxAge;
  const current = activity?.currentEvidence;
  const currentFresh = Number.isFinite(current?.observedAt) && Number.isFinite(current?.expiresAt)
    && current.expiresAt >= current.observedAt && now-current.observedAt >= -CLOCK_SKEW_SECONDS && now <= current.expiresAt && now-current.observedAt <= 3;
  let complete = fresh && currentFresh && current?.complete === true && tokenCount(current.count) !== null;
  const runs = (Array.isArray(activity?.runs) ? activity.runs : []).filter(run => run && typeof run.runId === 'string');
  const rawCurrent = (Array.isArray(activity?.currentKeys) ? runs.filter(run => activity.currentKeys.includes(run.traceKey)) : Array.isArray(activity?.current) ? activity.current : runs.filter(run => ['running', 'queued'].includes(run.activity) || run.source === 'router-active'));
  const currentRuns = rawCurrent.map(run => {
    if (!['running','queued'].includes(run.activity)) return run;
    const at=run.livenessObservedAt;
    return fresh && Number.isFinite(at) && now-at >= -CLOCK_SKEW_SECONDS && now-at <= 3 ? run
      : {...run,activity:'unknown',status:'unknown',livenessReason:'Current liveness unknown: original lock observation is missing, paused, or older than 3 seconds.'};
  });
  const history = Array.isArray(activity?.historyKeys) ? runs.filter(run => activity.historyKeys.includes(run.traceKey)) : Array.isArray(activity?.history) ? activity.history : runs.filter(run => !rawCurrent.includes(run));
  const consistent = current?.count === currentRuns.length && currentRuns.every(run => ['running', 'queued'].includes(run.activity));
  complete = complete && consistent;
  const reason = !fresh ? 'Activity observation is missing, paused, or stale.' : !complete
    ? !consistent && currentFresh && current?.complete === true ? 'Current run inventory contradicts the displayed records; totals and absence are unknown.'
      // A producer's own "complete" text is no longer true once its inventory has expired on this clock.
      : !currentFresh && current?.complete === true ? 'Run inventory is stale, ahead of this clock, or has no timestamp.' : text(activity?.currentEvidence?.reason, 300) || 'Activity sources are incomplete; absence and totals are unknown.' : 'Fresh, complete evidence for the observed run scope.';
  const bound = activity?.historyBound;
  const historyReason = bound?.truncated === false ? 'Bounded history; only retained source records are shown.'
    : 'History coverage is unknown or truncated; missing records are not zero outcomes.';
  return {fresh, complete, current: currentRuns, history, count: complete ? current.count : null, reason,
    historyReason, sampledAt: Number.isFinite(activity?.sampledAt) ? activity.sampledAt : null};
}

function withEvidence(snapshot, monitors, options) {
  return monitors.map(item => ({...item, evidence: evidenceFor(snapshot, item, options)}));
}

export function mapEvidence(source, {feedUnavailable = false, fictional = false, now} = {}) {
  const nodes = fleetFacts(source, {now}).filter(fact => fact.scope === 'lane' && fact.state === 'endpoint answering');
  const process = feedUnavailable
    ? {word: 'DISCONNECTED', matrix: 'Untested', detail: 'Monitor feed unavailable.'}
    : !source
    ? {word: 'UNKNOWN', matrix: 'Untested', detail: 'Map feed has no sample.'}
    : fictional
      ? {word: 'FICTIONAL', matrix: 'Observed', detail: 'Fictional map. Not a live process.'}
      : {word: 'SAMPLE', matrix: 'Observed', detail: 'Map sample is present. Node health is not lane readiness or a run.'};
  const named = nodes.some(node => node && (node.modelId || node.evidence?.modelId));
  const lane = named
    ? {word: 'INVENTORY', matrix: 'Observed', detail: 'A model id on the map is inventory. It is not readiness. Tokens per second are not readiness.'}
    : {word: 'NOT MEASURED', matrix: 'Untested', detail: 'No served-model readiness is on this map.'};
  const attachment = {word: 'NO RUN', matrix: 'Untested', detail: 'The map does not attach a run or attempt.'};
  return {
    badges: {process: process.word, lane: lane.word, attachment: attachment.word},
    rows: [
      {id: 'process', label: 'Process liveness', ...process, evidence: 'inference map sample'},
      {id: 'lane', label: 'Lane readiness', ...lane, evidence: 'node model id'},
      {id: 'attach', label: 'Run attached', ...attachment, evidence: 'map has no attempt id'},
    ],
  };
}

export function deriveMonitors(snapshot, options = {}) {
  if (options.fictional === true && !Number.isFinite(options.snapshotAge)) options={...options,snapshotAge:0};
  if (!Number.isFinite(options.snapshotAge) && Number.isFinite(snapshot?.sampledAt)) {
    const now=Number.isFinite(options.now)?options.now:Date.now()/1000;
    options={...options,snapshotAge:now-snapshot.sampledAt};
  }
  if (!snapshot || snapshot.schemaVersion !== 1) {
    const only = overview([], null, options);
    return withEvidence(null, [only], options);
  }
  const children = [];
  if (options.fictional === true && !snapshot.fleetState) {
    if (wantsMac(snapshot)) children.push(macMonitor(snapshot, options));
    else {
      const peer = peerMacMonitor(snapshot, options);
      if (peer) children.push(peer);
    }
    if (wantsPc(snapshot)) children.push(pcMonitor(snapshot, options));
    for (const row of registryMachineRows(snapshot)) {
      if (row.replaces !== 'legacy-pc') children.push(registryMonitor(row, options));
    }
  } else {
    const lanes = fleetLanes(snapshot, fleetOptions(options));
    for (const fact of fleetMachines(snapshot, fleetOptions(options))) children.push(fleetMachineMonitor(fact, lanes, options));
  }
  if (snapshot.pipeline && typeof snapshot.pipeline === 'object') children.push(nisiMonitor(snapshot, options));
  if (Array.isArray(snapshot.clients)) {
    for (const client of snapshot.clients) {
      if (!client || typeof client.id !== 'string' || !CLIENT_ID.test(client.id)) continue;
      children.push(clientMonitor(snapshot, client, options));
    }
  }
  children.push(...sessionMonitors(snapshot, options));
  return withEvidence(snapshot, [overview(children, snapshot, options), ...children], options);
}

function observationKey(monitor) {
  return [monitor.word, monitor.workload?.attemptId ?? '', monitor.workload?.requestedModel ?? '',
    monitor.workload?.servedModel ?? '', monitor.reason ?? ''].join('\n');
}

function mergeAnalyses(previous, fresh) {
  return fresh.map(slot => {
    if (slot.evidenced) return slot;
    const kept = (previous || []).find(item => item.tool === slot.tool && item.phase === slot.phase);
    return kept && kept.evidenced === true && kept.state !== 'unavailable' ? kept : slot;
  });
}

export function jevGate(analyses) {
  const slots = Array.isArray(analyses) ? analyses : [];
  const pre = slots.find(slot => slot.tool === 'jev' && slot.phase === 'pre');
  const post = slots.find(slot => slot.tool === 'jev' && slot.phase === 'post');
  const accepted = slot => Boolean(slot && slot.state === 'passed' && slot.verdict === 'PASS' && slot.evidenced === true && text(slot.provenance, 240));
  const bound = pre && post && pre.runId != null && pre.runId === post.runId
    && pre.attemptId != null && String(pre.attemptId) === String(post.attemptId);
  const open = Boolean(bound && accepted(pre) && accepted(post));
  const available = slots.some(slot => slot.tool === 'jev' && slot.evidenced === true && slot.state !== 'unavailable');
  return {
    open, available,
    word: open ? 'JEV GATE OPEN' : available ? 'JEV GATE BLOCKED' : 'NOT AVAILABLE YET',
    pre: pre?.state || 'unavailable',
    post: post?.state || 'unavailable',
    preVerdict: pre?.verdict || null,
    postVerdict: post?.verdict || null,
    blocker: open ? null : !available ? 'No attempt-bound Jev pre/post producer receipts are available.' : 'Jev pre and post need verdict PASS. Exit 0, STATUS OK, COMPLETED, and PROCEED-WITH-CHANGES are not a pass. A review that is not a pass does not open this gate.',
  };
}

function mergeSession(previous, fresh) {
  if (!previous || fresh.kind !== 'session') {
    const key = observationKey(fresh);
    return {...fresh, observationKey: key, events: [{seq: 1, attemptId: fresh.workload?.attemptId ?? null, word: fresh.word, key}]};
  }
  const previousAttempt = previous.workload?.attemptId ?? null;
  const nextAttempt = fresh.workload?.attemptId ?? null;
  const sameAttempt = previousAttempt === nextAttempt;
  const key = observationKey(fresh);
  let events = sameAttempt ? (previous.events || []).slice() : [];
  if (!sameAttempt || previous.observationKey !== key) {
    events = [...events, {seq: events.length + 1, attemptId: nextAttempt, word: fresh.word, key}];
  }
  const analyses = sameAttempt ? mergeAnalyses(previous.analyses, fresh.analyses) : fresh.analyses;
  return {...fresh, events, analyses, observationKey: key, retained: false};
}

function retainSession(previous) {
  const keepEvidenced = previous.status === 'blocked' || previous.status === 'down';
  const face = keepEvidenced
    ? {status: previous.status, glyph: previous.glyph, word: previous.word,
      reason: 'No longer in the latest sample. Retained until dismissed. Last evidenced state kept.'}
    : presentation('disconnected', 'No longer in the latest sample. Retained until dismissed.');
  const retained = {...previous, ...face, retained: true, workload: previous.workload ? {...previous.workload, retained: true} : null};
  if (retained.evidence) {
    retained.evidence = {
      ...retained.evidence,
      badges: {...retained.evidence.badges, process: face.word},
      rows: retained.evidence.rows.map(row => row.id === 'process'
        ? {...row, word: face.word, matrix: 'Observed', detail: face.reason}
        : row),
    };
  }
  return retained;
}

export function createMonitorDesk() {
  let active = 'all';
  let monitors = [];
  let ticks = 0;
  const views = new Map();
  const suppressed = new Set();
  const empty = () => ({
    listView: 'runtime',
    modelScope: 'core',
    mapSelected: null,
    search: '',
    boardQuery: '',
    boardSelected: null,
  });
  const viewFor = id => ({...empty(), ...views.get(id)});
  return {
    get active() { return active; },
    get monitors() { return monitors; },
    get subscriptionTicks() { return ticks; },
    view(id) { return viewFor(id); },
    patch(id, partial) {
      if (typeof id !== 'string' || !partial || typeof partial !== 'object') return;
      views.set(id, {...viewFor(id), ...partial});
    },
    select(id) {
      if (!monitors.some(item => item.id === id)) return false;
      active = id;
      if (!views.has(id)) views.set(id, empty());
      return true;
    },
    dismiss(id) {
      const item = monitors.find(monitor => monitor.id === id);
      if (!item || item.kind !== 'session') return false;
      suppressed.add(id);
      monitors = monitors.filter(monitor => monitor.id !== id);
      if (active === id) active = 'all';
      return true;
    },
    attachAnalysis(workloadId, record) {
      const item = monitors.find(monitor => monitor.id === workloadId && monitor.kind === 'session');
      if (!item || !record || !ANALYSIS_STATES.has(record.state)) return false;
      if (record.tool !== 'nisi' && record.tool !== 'jev') return false;
      if (record.phase !== 'pre' && record.phase !== 'post') return false;
      const attemptId = item.workload?.attemptId ?? null;
      if (attemptId == null || record.attemptId == null || String(record.attemptId) !== String(attemptId)) return false;
      if (record.runId != null && record.runId !== item.workload?.runId) return false;
      if (record.state === 'passed' && !text(record.provenance, 240)) return false;
      const slot = {
        tool: record.tool, phase: record.phase, state: record.state,
        runId: item.workload?.runId ?? null, attemptId,
        evidenced: true,
        provenance: text(record.provenance, 240),
        verdict: text(record.verdict, 80),
        detail: text(record.detail, 240) || record.state,
      };
      item.analyses = analysisSlots([...(item.analyses || []).filter(existing => existing.state !== 'unavailable'), slot], {
        runId: slot.runId, attemptId,
      });
      return item.word;
    },
    note(snapshot, options) {
      ticks += 1;
      const fresh = deriveMonitors(snapshot, options);
      const freshIds = new Set(fresh.map(item => item.id));
      for (const id of [...suppressed]) if (!freshIds.has(id)) suppressed.delete(id);
      const previousById = new Map(monitors.map(item => [item.id, item]));
      const visible = fresh
        .filter(item => !(item.kind === 'session' && suppressed.has(item.id)))
        .map(item => mergeSession(previousById.get(item.id), item));
      const visibleIds = new Set(visible.map(item => item.id));
      const retained = [];
      for (const previous of monitors) {
        if (previous.kind !== 'session' || visibleIds.has(previous.id) || suppressed.has(previous.id)) continue;
        retained.push(retainSession(previous));
      }
      const overviewItem = visible.find(item => item.id === 'all');
      const rest = visible.filter(item => item.id !== 'all');
      if (overviewItem && retained.length) {
        overviewItem.pipeline = {
          ...overviewItem.pipeline,
          note: `${overviewItem.pipeline.note} ${retained.length} session${retained.length === 1 ? '' : 's'} retained after leaving the sample.`,
          steps: [...overviewItem.pipeline.steps, ...retained.map(item => step(item.id, item.title, item.status === 'disconnected' ? 'disconnected' : item.status === 'blocked' ? 'blocked' : item.status === 'down' ? 'down' : 'unknown', item.reason, 'RETAINED'))],
        };
      }
      monitors = [...visible, ...retained];
      if (!monitors.some(item => item.id === active)) active = 'all';
      for (const item of monitors) if (!views.has(item.id)) views.set(item.id, empty());
      return monitors;
    },
  };
}

export function nextTabFocus(previousFocusId, ids, active) {
  if (!Array.isArray(ids) || !ids.length) return null;
  if (previousFocusId && ids.includes(previousFocusId)) return previousFocusId;
  if (previousFocusId) return ids.includes(active) ? active : ids[0];
  return null;
}

export function moveMonitorTab(ids, active, key) {
  if (!Array.isArray(ids) || !ids.length) return null;
  const index = Math.max(0, ids.indexOf(active));
  if (key === 'ArrowRight') return ids[(index + 1) % ids.length];
  if (key === 'ArrowLeft') return ids[(index - 1 + ids.length) % ids.length];
  if (key === 'Home') return ids[0];
  if (key === 'End') return ids[ids.length - 1];
  return null;
}

export function initialMonitorId(search) {
  const match = /(?:^|[?&])monitor=([^&]*)/.exec(String(search || ''));
  if (!match || !match[1]) return null;
  let id;
  try { id = decodeURIComponent(match[1].replaceAll('+', ' ')); }
  catch (_) { return null; }
  return MONITOR_QUERY.test(id) ? id : null;
}

/** Says what is not configured. Only a PC lane node can say "No PC": the Mac's Veritas reviewer and any other node name themselves. */
function notConfiguredText(node) {
  const machine = text(node?.machine, 24);
  const label = text(node?.label, 40);
  if (machine === 'pc' || (typeof node?.id === 'string' && node.id.startsWith('pc:'))) return 'No PC is configured';
  return label ? `${label} is not configured` : 'Not configured';
}

export function healthPresentation(node, {feedUnavailable = false} = {}) {
  if (feedUnavailable) return presentation('disconnected', 'Monitor feed unavailable');
  if (node?.scope || node?.factState) return factFace({...node, state: node.factState || node.state});
  const reason = text(node?.reason, 80);
  if (node?.health === 'ok') return presentation('ok', reason);
  if (node?.health === 'degraded') return presentation('degraded', reason);
  if (node?.health === 'down') return presentation('down', reason);
  if (reason === 'stale') return presentation('stale', 'Observation stale');
  if (reason === 'feed_unavailable') return presentation('disconnected', 'Monitor feed unavailable');
  if (reason === 'not_configured') return presentation('not_configured', notConfiguredText(node));
  if (reason === 'tunnel_down') {
    const age = Number.isFinite(node?.ageSeconds) ? ` Last sample ${Math.floor(node.ageSeconds)}s.` : '';
    return presentation('disconnected', `PC tunnel down.${age}`);
  }
  return presentation('unknown', reason ? reason.replaceAll('_', ' ') : 'Not measured');
}

export function mapMonitors(map, options = {}) {
  const facts = fleetFacts(map, {now: options.now, fresh: options.feedUnavailable !== true});
  const summary = fleetSummary(map, {now: options.now, fresh: options.feedUnavailable !== true});
  const allFace = options.feedUnavailable === true || !map
    ? presentation('disconnected', 'Monitor feed unavailable')
    : {...presentation('unknown', summary.text), word: summary.state === 'coverage' ? summary.headline : 'UNKNOWN', glyph: summary.counts.answering > 0 ? '■' : '?'};
  return [
    {id: 'all', title: 'All inference', eyebrow: 'MAP', ...allFace, fictional: false},
    ...facts.map(fact => ({id: fact.id, title: fact.label || fact.id,
      eyebrow: fact.scope.toUpperCase(), ...(fact.scope === 'machine'
        ? machineFace(fact, facts.filter(item => item.scope === 'lane'))
        : factFace(fact)), fictional: false,
      machine: fact.scope === 'machine' ? fact.id : fact.parentId})),
  ];
}

export function nodeInMonitor(item, node) {
  if (!item || item.id === 'all' || !node) return true;
  const id = typeof node.id === 'string' ? node.id : '';
  const kind = node.kind;
  if (item.kind === 'machine' && item.identity?.provider === 'fleet') {
    const machine = item.identity.machine;
    return id === machine || node.parentId === machine || node.machine === machine;
  }
  if (item.kind === 'machine' && item.identity?.provider === 'registry') {
    const machine = item.identity.machine;
    return id === machine || node.machine === machine || node.source?.machine === machine || node.source?.id === machine
      || node.registryMachine?.row?.host === machine || node.registryMachine?.row?.id === machine;
  }
  if (item.kind === 'machine' && item.identity?.machine === 'mac') {
    return id === 'runtime' || kind === 'afm' || kind === 'model-overflow' || (kind === 'model' && node.model?.host !== 'windows')
      || (kind === 'registry-lane' && node.registryLane?.machine === 'mac');
  }
  if (item.kind === 'machine' && item.identity?.machine === 'windows') {
    return id === 'windows-worker' || kind === 'windows-lane' || (kind === 'model' && node.model?.host === 'windows');
  }
  if (item.kind === 'route') return id === 'pipeline' || id === 'jev' || kind === 'run' || kind === 'call';
  if (item.kind === 'client') {
    const provider = item.identity?.provider;
    return id === `client:${provider}` || id.startsWith(`client-model:${provider}:`) || id === `client-model:${provider}` || node.client === provider;
  }
  if (item.kind === 'session' && item.sessionKind === 'route') {
    return id === 'pipeline' || node.runId === item.identity?.session || node.run?.runId === item.identity?.session;
  }
  if (item.kind === 'session' && item.sessionKind === 'job') {
    return id === 'windows-worker' || kind === 'windows-lane' || node.jobId === item.identity?.session;
  }
  return true;
}

const DEMO_LABEL = 'DEMO · fictional monitors · not a live observation';

export function demoSnapshot() {
  return {
    fictional: true,
    label: DEMO_LABEL,
    schemaVersion: 1,
    host: 'mac',
    sampledAt: 0,
    models: [
      {id: 'example-local', host: 'mac', loaded: true, state: 'generating', ageSeconds: 1, name: 'Example local'},
    ],
    clients: [
      {id: 'codex', label: 'Codex', model: null, modelState: 'unknown', activity: 'unknown', detail: 'Fictional sample. No Codex model identity was recorded.', models: []},
      {id: 'claude', label: 'Claude', model: 'claude-example', modelState: 'observed', activity: 'unknown', detail: 'Fictional recorded identity. Activity comes only from an attributed route in this sample.', models: [{id: 'claude-example', modelState: 'observed'}]},
      {id: 'opencode', label: 'OpenCode', model: null, modelState: 'unknown', activity: 'unknown', detail: 'Fictional sample. No OpenCode session was recorded.', models: []},
      {id: 'cursor', label: 'Cursor', model: null, modelState: 'unknown', activity: 'unknown', detail: 'No supported bounded Cursor model metadata available; activity unknown', models: []},
      {id: 'grok', label: 'Grok', model: null, modelState: 'unknown', activity: 'unknown', detail: 'No supported bounded local Grok model metadata source; activity unknown', models: []},
    ],
    pipeline: {
      status: 'running',
      runId: 'demo-mac-nisi',
      stage: 'backend_review',
      pipelinesComplete: true,
      runsTruncated: false,
      recoveryRequired: false,
      steps: [
        {id: 'intake', label: 'Intake', state: 'pending'},
        {id: 'backend_draft', label: 'Author', state: 'pending'},
        {id: 'backend_review', label: 'Review', state: 'pending'},
        {id: 'checks', label: 'Checks', state: 'pending'},
      ],
      pipelines: [
        {runId: 'demo-mac-nisi', status: 'running', stage: 'backend_review', host: 'mac', client: null, live: true, ageSeconds: 4},
        {runId: 'demo-claude', status: 'running', stage: 'checks', host: 'mac', client: 'claude', live: true, ageSeconds: 2},
      ],
    },
    components: [
      {id: 'nisi', state: 'in-use', detail: 'Fictional in-flight Nisi call. Not a live observation.'},
      {id: 'jev', state: 'configured', detail: 'Fictional opt-in. Jev generation is not observed.'},
    ],
    windowsWorker: {
      state: 'advertised',
      ageSeconds: 2,
      detail: 'Fictional heartbeat.',
      lanes: {
        fast: {up: true, model: 'example-20b', slotsBusy: 1, slotsTotal: 2},
        deep: {up: false, model: null, slotsBusy: 0, slotsTotal: 1},
      },
    },
    windowsJobs: {
      liveAvailable: true,
      complete: true,
      inFlight: [{id: 'demo-pc-job', client: null, lane: 'fast', model: 'example-20b', requestedModel: 'example-20b', servedModel: 'example-20b-served', ageSeconds: 3}],
      recent: [],
    },
  };
}

export const DEMO_BANNER = DEMO_LABEL;
