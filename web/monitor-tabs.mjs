/**
 * View models for inference-monitor tabs.
 *
 * Tabs are a lens over one snapshot. Nothing here fetches, posts, pauses,
 * cancels, or starts inference. Session names come from the snapshot's own
 * client, route, job, and machine records — missing records stay absent.
 */

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
  stale: {status: 'stale', glyph: '?', word: 'STALE'},
  unknown: {status: 'unknown', glyph: '?', word: 'UNKNOWN'},
  pending: {status: 'unknown', glyph: '–', word: 'PENDING'},
  recorded: {status: 'idle', glyph: '–', word: 'RECORDED'},
  unavailable: {status: 'unavailable', glyph: '○', word: 'UNAVAILABLE'},
  disconnected: {status: 'disconnected', glyph: '○', word: 'DISCONNECTED'},
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
    identity: fields.identity,
    pipeline: fields.pipeline,
    metrics: fields.metrics || [],
  };
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
  if (jobs.complete === false) return false;
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

function macFace(snapshot, options) {
  const models = macModels(snapshot);
  if (!liveView(options)) return cap(presentation('live', 'Mac model activity.'), options);
  const active = models.filter(row => row.loaded === true && (row.state === 'generating' || row.state === 'busy')
    && finiteAge(row.ageSeconds) != null && row.ageSeconds >= 0 && row.ageSeconds <= 3);
  if (active.length) return presentation('live', `${active.length} Mac model${active.length === 1 ? '' : 's'} reported generating or busy.`);
  if (!models.length) return presentation('unknown', 'No Mac model rows in this sample.');
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
  if (state === 'advertised') return presentation('idle', 'Worker heartbeat is advertised. No in-flight job is claimable in this sample.');
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
  if (pipeline.pipelinesComplete === false || pipeline.runsTruncated === true) {
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
  const loaded = models.every(row => typeof row.loaded === 'boolean') ? models.filter(row => row.loaded === true).length : null;
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
      metric('Mac models in sample', models.length),
      metric('Loaded', loaded == null || !liveView(options) ? null : loaded),
      metric('Generating or busy', liveView(options) ? models.filter(row => row.loaded === true && (row.state === 'generating' || row.state === 'busy')).length : null),
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
      note: rows.length ? 'In-flight PC jobs in this sample. Settled history stays on the activity panel.' : 'No in-flight PC job in this sample.',
      steps: rows.map(row => jobStep(row, jobsClaimable(jobs, options))),
    },
    metrics: [
      metric('Worker', text(worker?.state) || 'unknown'),
      metric('Lanes', laneMetric(worker)),
      metric('In-flight jobs', rows.length),
      metric('Route list', list.incomplete ? 'incomplete' : 'present'),
    ],
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
      metric('Listed routes', routes.length),
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
    sessions.push(monitor({
      id: `route:${row.runId}`,
      kind: 'session',
      sessionKind: 'route',
      title: `${who} · ${row.runId}`,
      eyebrow: 'SESSION',
      face,
      fictional: options.fictional,
      identity: identity({
        machine: row.host === 'mac' || row.host === 'windows' ? row.host : null,
        machineLabel: row.host === 'mac' ? 'Mac' : row.host === 'windows' ? 'Windows PC' : 'Host not reported',
        provider: text(row.client),
        providerLabel: text(row.client) || 'Client not reported',
        model: null,
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
    sessions.push(monitor({
      id: `job:${row.id}`,
      kind: 'session',
      sessionKind: 'job',
      title: `${text(row.client) || 'PC job'} · ${row.lane === 'fast' || row.lane === 'deep' ? row.lane : 'lane ?'}`,
      eyebrow: 'SESSION',
      face,
      fictional: options.fictional,
      identity: identity({
        machine: 'windows',
        machineLabel: 'Windows PC',
        provider: text(row.client),
        providerLabel: text(row.client) || 'Client not reported',
        model: text(row.model),
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
    || (Array.isArray(snapshot.machines) && snapshot.machines.some(row => row && (row.platform === 'macos' || row.host === 'mac')));
}

function wantsPc(snapshot) {
  return snapshot.host === 'windows' || (snapshot.windowsWorker && typeof snapshot.windowsWorker === 'object')
    || (Array.isArray(snapshot.models) && snapshot.models.some(row => row && row.host === 'windows'));
}

function overview(children, snapshot, options) {
  const running = children.filter(item => item.status === 'live').length;
  let face;
  if (!snapshot) face = presentation('unknown', 'Observer has not published a snapshot.');
  else if (options.fictional === true) face = presentation(running ? 'live' : 'idle', 'Fictional sample. Not a live observation.');
  else if (options.paused === true) face = presentation('stale', 'View paused. Tab counts are not live claims.');
  else if (options.feedFresh !== true) face = presentation('stale', 'Feed is not fresh. Tab counts are not live claims.');
  else if (running) face = presentation('live', `${running} monitor${running === 1 ? '' : 's'} report running work in this sample.`);
  else face = presentation('idle', 'No monitor reports running work in this sample.');
  const list = snapshot ? pipelineListNote(snapshot) : {incomplete: true, note: 'No snapshot.'};
  return monitor({
    id: 'all',
    kind: 'overview',
    title: 'All inference',
    eyebrow: 'OVERVIEW',
    face,
    fictional: options.fictional,
    identity: identity({
      machine: text(snapshot?.host) === 'mac' || text(snapshot?.host) === 'windows' ? snapshot.host : null,
      machineLabel: snapshot?.host === 'mac' ? 'Observer on this Mac' : snapshot?.host === 'windows' ? 'Observer on this PC' : 'Observer host not reported',
      provider: null,
      providerLabel: 'All providers in this sample',
      model: null,
      session: null,
    }),
    pipeline: {
      declared: children.length > 0,
      incomplete: list.incomplete,
      note: list.note,
      steps: children.map(item => step(item.id, item.title, item.status === 'live' ? 'live'
        : item.status === 'queued' ? 'queued'
        : item.status === 'blocked' ? 'blocked'
        : item.status === 'down' ? 'down'
        : item.status === 'degraded' ? 'degraded'
        : item.status === 'stale' ? 'stale'
        : item.status === 'disconnected' ? 'disconnected'
        : item.status === 'unavailable' ? 'unavailable'
        : item.status === 'idle' || item.status === 'ok' ? 'idle'
        : 'unknown', item.reason, item.eyebrow)),
    },
    metrics: [
      metric('Monitors', children.length),
      metric('Running', options.feedFresh === true && options.paused !== true ? running : null),
      metric('Route list', snapshot ? (list.incomplete ? 'incomplete' : 'present') : null),
    ],
  });
}

export function deriveMonitors(snapshot, options = {}) {
  if (!snapshot || snapshot.schemaVersion !== 1) {
    return [overview([], null, options)];
  }
  const children = [];
  if (wantsMac(snapshot)) children.push(macMonitor(snapshot, options));
  if (wantsPc(snapshot)) children.push(pcMonitor(snapshot, options));
  if (snapshot.pipeline && typeof snapshot.pipeline === 'object') children.push(nisiMonitor(snapshot, options));
  if (Array.isArray(snapshot.clients)) {
    for (const client of snapshot.clients) {
      if (!client || typeof client.id !== 'string' || !CLIENT_ID.test(client.id)) continue;
      children.push(clientMonitor(snapshot, client, options));
    }
  }
  children.push(...sessionMonitors(snapshot, options));
  return [overview(children, snapshot, options), ...children];
}

export function createMonitorDesk() {
  let active = 'all';
  let monitors = [];
  const views = new Map();
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
    note(snapshot, options) {
      monitors = deriveMonitors(snapshot, options);
      if (!monitors.some(item => item.id === active)) active = 'all';
      for (const item of monitors) if (!views.has(item.id)) views.set(item.id, empty());
      return monitors;
    },
  };
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

export function healthPresentation(node, {feedUnavailable = false} = {}) {
  if (feedUnavailable) return presentation('disconnected', 'Monitor feed unavailable');
  const reason = text(node?.reason, 80);
  if (node?.health === 'ok') return presentation('ok', reason);
  if (node?.health === 'degraded') return presentation('degraded', reason);
  if (node?.health === 'down') return presentation('down', reason);
  if (reason === 'stale') return presentation('stale', 'Observation stale');
  if (reason === 'feed_unavailable') return presentation('disconnected', 'Monitor feed unavailable');
  if (reason === 'tunnel_down') return presentation('disconnected', 'PC tunnel down');
  return presentation('unknown', reason ? reason.replaceAll('_', ' ') : 'Not measured');
}

export function mapMonitors(map, options = {}) {
  const nodes = Array.isArray(map?.nodes) ? map.nodes.filter(node => node && typeof node.id === 'string' && node.id.length <= 80) : [];
  const faces = nodes.map(node => ({node, face: healthPresentation(node, options)}));
  const unavailable = options.feedUnavailable === true || !map;
  const allFace = unavailable
    ? presentation('disconnected', 'Monitor feed unavailable')
    : faces.some(item => item.face.status === 'ok' || item.face.status === 'degraded' || item.face.status === 'down')
      ? presentation('ok', 'Map nodes in this sample. Words on each tab are the status.')
      : presentation('unknown', 'No measured health in this sample.');
  return [
    {id: 'all', title: 'All inference', eyebrow: 'MAP', ...allFace, fictional: false},
    ...faces.map(({node, face}) => ({
      id: node.id,
      title: text(node.label, 80) || node.id,
      eyebrow: text(node.role, 24) || 'NODE',
      ...face,
      fictional: false,
      machine: typeof node.machine === 'string' ? node.machine : null,
    })),
  ];
}

export function nodeInMonitor(item, node) {
  if (!item || item.id === 'all' || !node) return true;
  const id = typeof node.id === 'string' ? node.id : '';
  const kind = node.kind;
  if (item.kind === 'machine' && item.identity?.machine === 'mac') {
    return id === 'runtime' || kind === 'afm' || kind === 'model-overflow' || (kind === 'model' && node.model?.host !== 'windows');
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
      inFlight: [{id: 'demo-pc-job', client: null, lane: 'fast', model: 'example-20b', ageSeconds: 3}],
      recent: [],
    },
  };
}

export const DEMO_BANNER = DEMO_LABEL;
