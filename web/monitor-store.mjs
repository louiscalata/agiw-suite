import {fleetState} from './fleet-state.mjs';

// The only transport owner for snapshot, inference and SSE. Browser receive time
// diagnoses transport; only producer timestamps can renew an observation.
const finite = Number.isFinite;
const stamp = value => typeof value === 'string' && /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
  && finite(Date.parse(value)) ? Date.parse(value) / 1000 : finite(value) ? value : null;
export const feedAge = (data, now) => {
  const at = data?.fullSampledAt ?? data?.sampledAt;
  return finite(at) ? now - at : NaN;
};
export const validSnapshot = data => Boolean(data) && data.schemaVersion === 1
  && Array.isArray(data.models) && Array.isArray(data.sources) && finite(data.sampledAt);
export const streamBackoff = attempt => Math.min(30000, 5000 * 2 ** Math.min(3, Math.max(0, Math.floor(Number(attempt)) || 0)));
const validInference = data => Boolean(data) && Array.isArray(data.nodes) && !data.error;
const documentTime = (kind, data) => kind === 'snapshot' ? data?.sampledAt : stamp(data?.observedAt);
const diagnostics = data => (Array.isArray(data?.collectorDiagnostics) ? data.collectorDiagnostics : [])
  .filter(row => row && typeof row.stage === 'string' && /^[a-z0-9_-]{1,64}$/.test(row.stage)
    && typeof row.code === 'string' && /^[a-z0-9_-]{1,64}$/.test(row.code)
    && finite(row.failedAt) && finite(new Date(row.failedAt * 1000).getTime()))
  .slice(-16).map(row => ({stage: row.stage, code: row.code, failedAt: row.failedAt}));

export function projectEvidence(data, now) {
  if (!data) return null;
  const contract = fleetState(data, {now});
  return contract ? {...data, fleetState: contract} : {...data};
}

export function createMonitorStore(options = {}) {
  const fetcher = options.fetch ?? globalThis.fetch?.bind(globalThis);
  const Source = Object.hasOwn(options, 'EventSource') ? options.EventSource : globalThis.EventSource;
  const now = options.now ?? (() => Date.now() / 1000);
  const later = options.setTimeout ?? globalThis.setTimeout;
  const cancel = options.clearTimeout ?? globalThis.clearTimeout;
  const deadlineMs = options.deadlineMs ?? 1800;
  const silentMs = options.silentMs ?? 4000;
  const pollMs = options.pollMs ?? 1000;
  const inferenceMs = options.inferenceMs ?? 2000;
  const ageMs = options.ageMs ?? 1000;
  const listeners = new Set();
  const channels = Object.fromEntries(['snapshot', 'inference'].map(kind => [kind,
    {data: null, receivedAt: null, error: null, collector: [], request: null, timer: null, enabled: false}]));
  let source = null, watchdog = null, retry = null, ageTimer = null, attempt = 0;
  let stopped = false, polling = false, streamReady = false, consumerError = null;

  function getState() {
    const time = now();
    const status = kind => {
      const channel = channels[kind];
      const at = stamp(channel.data?.observedAt);
      const ageSeconds = kind === 'snapshot' ? feedAge(channel.data, time)
        : at !== null ? time - at : NaN;
      return {available: channel.data !== null, receivedAt: channel.receivedAt,
        ageSeconds: finite(ageSeconds) ? ageSeconds : null,
        fresh: finite(ageSeconds) && ageSeconds >= -1 && ageSeconds <= (kind === 'snapshot' ? 3 : 30),
        error: channel.error};
    };
    return {snapshot: projectEvidence(channels.snapshot.data, time), inference: projectEvidence(channels.inference.data, time),
      snapshotStatus: status('snapshot'), inferenceStatus: status('inference'),
      transport: {streaming: source !== null && streamReady, connecting: source !== null && !streamReady, polling},
      diagnostics: [...channels.snapshot.collector, ...channels.inference.collector,
        ...Object.values(channels).map(channel => channel.error).filter(Boolean), ...(consumerError ? [consumerError] : [])]};
  }
  function deliver(listener, state) {
    try { listener(state); }
    catch (_) { consumerError = {stage: 'consumer', code: 'render_failed', failedAt: now()}; }
  }
  function emit() {
    if (stopped) return;
    const state = getState();
    // One broken view must not strand the refresh promise or the ageing timer.
    for (const listener of listeners) deliver(listener, state);
  }
  function fail(kind, stage, code) {
    channels[kind].error = {stage, code, failedAt: now()};
    emit();
  }
  function accept(kind, data) {
    const channel = channels[kind];
    channel.collector = diagnostics(data);
    const at = documentTime(kind, data), before = documentTime(kind, channel.data);
    // A late/older response cannot roll back the accepted evidence clock.
    if (channel.data && at !== null && before !== null && at < before) return;
    channel.data = data;
    channel.receivedAt = now();
    channel.error = null;
  }
  function clearRequest(kind) {
    const request = channels[kind].request;
    if (request) request.cancel();
  }
  function schedule(kind, delay) {
    const channel = channels[kind];
    cancel(channel.timer);
    if (!stopped && channel.enabled && (kind !== 'snapshot' || polling))
      channel.timer = later(() => { channel.timer = null; refresh(kind); }, delay);
  }
  function refresh(kind = 'snapshot') {
    if (!channels[kind]) throw new Error('Unknown monitor channel');
    const channel = channels[kind];
    if (stopped) return Promise.resolve();
    if (channel.request) return channel.request.promise;
    cancel(channel.timer); channel.timer = null;
    const controller = new AbortController();
    let finish, timer;
    const request = {promise: new Promise(resolve => { finish = resolve; }), cancel: null};
    const complete = (data, code, cancelled = false) => {
      if (channel.request !== request) return;
      channel.request = null;
      cancel(timer);
      if (!cancelled && !stopped) {
        if (code) fail(kind, `${kind}_request`, code);
        else { accept(kind, data); emit(); }
        schedule(kind, kind === 'snapshot' ? pollMs : inferenceMs);
      }
      finish();
    };
    request.cancel = () => { complete(null, null, true); controller.abort(); };
    channel.request = request;
    // Releasing ownership does not depend on fetch respecting AbortSignal. Both
    // headers and JSON decoding share this deadline; late completions are ignored.
    timer = later(() => { complete(null, 'deadline'); controller.abort(); }, deadlineMs);
    (async () => {
      try {
        const response = await fetcher(`/api/${kind}`, {cache: 'no-store', headers: {Accept: 'application/json'}, signal: controller.signal});
        if (channel.request !== request) return;
        const data = await response.json();
        if (channel.request !== request) return;
        if (diagnostics(data).length) channel.collector = diagnostics(data);
        if (!response.ok) { complete(null, `http_${Number.isInteger(response.status) ? response.status : 'error'}`); return; }
        if (!(kind === 'snapshot' ? validSnapshot(data) : validInference(data))) { complete(null, 'malformed'); return; }
        complete(data);
      } catch (_) { complete(null, 'unavailable'); }
    })();
    return request.promise;
  }
  function startPolling() {
    if (!channels.snapshot.enabled || stopped) return;
    polling = true;
    refresh('snapshot');
  }
  function closeStream() {
    const previous = source; source = null;
    streamReady = false;
    cancel(watchdog); watchdog = null;
    previous?.close();
  }
  function streamFailed(code) {
    closeStream();
    fail('snapshot', 'stream', code);
    startPolling();
    cancel(retry);
    if (typeof Source === 'function' && !stopped)
      retry = later(openStream, streamBackoff(attempt++));
  }
  function armWatchdog() {
    cancel(watchdog);
    watchdog = later(() => streamFailed('silent'), silentMs);
  }
  function openStream() {
    cancel(retry); retry = null;
    if (stopped || !channels.snapshot.enabled) return;
    if (typeof Source !== 'function') { startPolling(); return; }
    closeStream();
    let next;
    try { next = new Source('/api/stream'); }
    catch (_) { streamFailed('unavailable'); return; }
    source = next;
    next.addEventListener('snapshot', event => {
      if (source !== next || stopped) return;
      let data;
      try { data = JSON.parse(event.data); } catch (_) { streamFailed('malformed'); return; }
      if (!validSnapshot(data)) { streamFailed('malformed'); return; }
      // Keep fallback through the handshake. Only a valid message may take
      // ownership; an open TCP connection alone is not a working snapshot feed.
      streamReady = true;
      polling = false;
      cancel(channels.snapshot.timer); channels.snapshot.timer = null;
      clearRequest('snapshot');
      accept('snapshot', data);
      attempt = 0;
      armWatchdog();
      emit();
    });
    next.addEventListener('error', () => { if (source === next) streamFailed('unavailable'); });
    armWatchdog();
    startPolling();
    emit();
  }
  function age() {
    if (stopped) return;
    emit();
    if (!stopped) ageTimer = later(age, ageMs);
  }
  return {
    getState, refresh,
    subscribe(listener) { listeners.add(listener); deliver(listener, getState()); return () => listeners.delete(listener); },
    start({snapshot = false, inference = false} = {}) {
      if (stopped) return;
      if (ageTimer === null) ageTimer = later(age, ageMs);
      if (snapshot && !channels.snapshot.enabled) { channels.snapshot.enabled = true; openStream(); }
      if (inference && !channels.inference.enabled) { channels.inference.enabled = true; refresh('inference'); }
    },
    destroy() {
      stopped = true; closeStream(); cancel(retry); cancel(ageTimer);
      for (const kind of Object.keys(channels)) { cancel(channels[kind].timer); clearRequest(kind); }
      listeners.clear();
    },
  };
}

let singleton;
export function getMonitorStore() { return singleton ??= createMonitorStore(); }
