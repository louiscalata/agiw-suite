import { modelControlView } from '/model-control-view.mjs';
import {getMonitorStore} from '/monitor-store.mjs';

const BUS_SEGMENT = 12;

function busSegment(mode, frame) {
  const dashes = Array(BUS_SEGMENT).fill('-');
  if (mode === 'flow') dashes[((frame % BUS_SEGMENT) + BUS_SEGMENT) % BUS_SEGMENT] = '>';
  else if (mode === 'still') dashes[Math.floor(BUS_SEGMENT / 2)] = '>';
  else if (mode === 'broken') dashes[Math.floor(BUS_SEGMENT / 2)] = 'x';
  else if (mode === 'unknown') dashes[Math.floor(BUS_SEGMENT / 2)] = '?';
  else if (mode === 'stale') return '.'.repeat(BUS_SEGMENT);
  return dashes.join('');
}

/**
 * The observation bus as monospace text: LM Studio -> observer -> this view.
 * Only what the sample says is drawn. Words carry every state; the moving glyph is decoration
 * and only moves while the sample is fresh, the source is live, and motion is allowed.
 */
export function observationBus({ snapshot, fresh, studioLive, frame = 0, reducedMotion = false, paused = false }) {
  const rows = Array.isArray(snapshot?.models) && snapshot.models.every(row => row && typeof row === 'object' && !Array.isArray(row))
    ? snapshot.models : null;
  const demo = snapshot?.demo === true;
  let source = 'UNKNOWN', inventory = 'UNKNOWN', sample = 'NO SAMPLE YET', first = 'unknown', second = 'unknown';
  if (snapshot) {
    sample = paused ? 'PAUSED' : fresh ? 'FRESH' : 'STALE';
    if (demo) {
      source = 'SAMPLE DATA';
      first = 'still';
    } else if (!fresh) {
      source = 'STALE';
      first = 'stale';
    } else if (studioLive) {
      source = 'LIVE';
      first = fresh && !paused ? (reducedMotion ? 'still' : 'flow') : 'still';
    } else {
      source = 'NOT ANSWERING';
      first = 'broken';
    }
    if (rows && (fresh || demo)) {
      const loaded = rows.filter(row => row.loaded === true).length;
      inventory = `${rows.length} MODEL${rows.length === 1 ? '' : 'S'} / ${loaded} LOADED${demo ? ' (SAMPLE)' : ''}`;
    }
    second = paused ? 'still' : fresh ? (reducedMotion || demo ? 'still' : 'flow') : 'stale';
  }
  const art = `LM STUDIO ${busSegment(first, frame)} OBSERVER ${busSegment(second, frame + BUS_SEGMENT / 2)} THIS VIEW`;
  const text = [art, `source    ${source}`, `inventory ${inventory}`, `sample    ${sample}`].join('\n');
  const label = `Observation bus. Source ${source.toLowerCase()}. Inventory ${inventory.toLowerCase()}. Sample ${sample.toLowerCase()}.`;
  return { text, label, moving: first === 'flow' || second === 'flow' };
}

/**
 * The model load/unload path as monospace text: MODEL -> YOU -> LM STUDIO -> INVENTORY.
 * [x] done, [>] now, [!] failed, [?] not known, [ ] not yet. The last step is only [x] when fresh
 * inventory has confirmed the result; a failure after dispatch stays [?] because it may have happened.
 */
export function controlSchematic({ model, control, controlKnown, decision, confirming }) {
  const name = String(model?.name || model?.id || 'NO MODEL').toUpperCase();
  if (!controlKnown) return { text: `CONTROL   ${name}\nstate     UNKNOWN`, label: `Model control for ${name.toLowerCase()}: state unknown.` };
  // The last operation's outcome belongs to the model it was about; another chosen model starts from idle.
  // 'running' is global (one operation at a time), so it is always shown.
  const aboutThis = typeof control?.modelId !== 'string' || [model?.id, model?.instanceId, model?.modelKey].includes(control.modelId);
  const status = control?.status === 'running' || aboutThis ? control?.status : 'idle';
  const settlement = aboutThis ? control?.settlement : undefined;
  const action = String((status === 'idle' ? decision?.action : control?.action) || 'control').toUpperCase();
  let marks, state;
  if (status === 'running') {
    marks = ['x', 'x', '>', ' '];
    state = 'RUNNING';
  } else if (status === 'reported' || status === 'succeeded') {
    marks = settlement === 'confirmed' ? ['x', 'x', 'x', 'x'] : ['x', 'x', 'x', '?'];
    state = settlement === 'confirmed' ? 'DONE, INVENTORY CONFIRMS' : 'REPORTED, WAITING FOR INVENTORY';
  } else if (status === 'failed' && settlement === 'confirmed') {
    // A restart interrupted the operation, and fresh inventory later showed it had completed.
    marks = ['x', 'x', 'x', 'x'];
    state = 'INTERRUPTED, INVENTORY CONFIRMS';
  } else if (status === 'failed') {
    const sent = settlement !== 'not-started';
    marks = sent ? ['x', 'x', '!', '?'] : ['x', 'x', '!', ' '];
    state = sent ? 'FAILED, MAY HAVE HAPPENED' : 'FAILED, NOTHING WAS SENT';
  } else if (confirming) {
    marks = ['x', '>', ' ', ' '];
    state = 'WAITING FOR YOU';
  } else if (decision?.action) {
    marks = ['x', ' ', ' ', ' '];
    state = 'READY';
  } else {
    marks = ['?', ' ', ' ', ' '];
    state = 'UNAVAILABLE';
  }
  const trail = ['MODEL', 'YOU', 'LM STUDIO', 'INVENTORY'].map((step, i) => `${step} [${marks[i]}]`).join(' -> ');
  const why = state === 'UNAVAILABLE' ? decision?.reason : status !== 'idle' && typeof control?.message === 'string' ? control.message : '';
  const lines = [`${action.padEnd(9)} ${name}`, trail, `state     ${state}`];
  if (why) lines.push(`why       ${why.replace(/[^\x20-\x7e]/g, '?').slice(0, 120)}`);
  return { text: lines.join('\n'), label: `${action.toLowerCase()} ${name.toLowerCase()}: ${state.toLowerCase()}.${why ? ' ' + why : ''}` };
}

/** The control statuses model_control.py sends ('succeeded' after a confirmed load or unload) plus 'reported'. */
export const CONTROL_STATUSES = new Set(['idle', 'running', 'succeeded', 'reported', 'failed']);

export const TEXT_SIZES = ['100', '125', '150', '200'];

/** A saved text size, or 100 when the value is missing or not one of the offered sizes. */
export function textSizeChoice(saved) {
  return TEXT_SIZES.includes(String(saved)) ? String(saved) : '100';
}

/** Adds Text size beside Theme. The choice scales the whole page and is kept in this browser only. */
export function mountTextSize({ window, document }) {
  const theme = document.getElementById?.('jaryoTheme')?.closest?.('.j-theme');
  if (!theme?.after) return null;
  const root = document.documentElement;
  let saved = null;
  try { saved = window.localStorage?.getItem('agiw-text-size'); } catch (_) { saved = null; }
  const apply = value => {
    const size = textSizeChoice(value);
    root.dataset.textSize = size;
    try { window.localStorage?.setItem('agiw-text-size', size); } catch (_) { /* private mode: still applies */ }
    return size;
  };
  const label = document.createElement('label');
  label.className = 'j-theme j-text-size';
  label.append(document.createTextNode('Text size'));
  const select = document.createElement('select');
  select.id = 'jaryoTextSize';
  select.setAttribute('aria-label', 'Text size');
  for (const size of TEXT_SIZES) {
    const option = document.createElement('option');
    option.value = size;
    option.textContent = `${size}%`;
    select.append(option);
  }
  select.value = apply(saved);
  select.addEventListener('change', () => { select.value = apply(select.value); });
  label.append(select);
  theme.after(label);
  return label;
}

const ZOOM_SWAP_LIMIT_MS = 400;

/**
 * Runs `change` inside a View Transition so the new face zooms in (simple-view.css draws it), or
 * plainly when the browser has no View Transitions or the person asked for reduced motion.
 * `change` runs exactly once whichever path is taken, and the html class is always removed.
 */
export function zoomSwap({ window, document }, change) {
  let ran = false;
  let fallback = null;
  const once = () => {
    if (ran) return;
    ran = true;
    if (fallback !== null) window.clearTimeout?.(fallback);
    change();
  };
  const reduced = window?.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true;
  if (reduced || typeof document?.startViewTransition !== 'function' || !document.documentElement?.classList) {
    once();
    return false;
  }
  const classes = document.documentElement.classList;
  const done = () => classes.remove('j-zooming');
  try {
    classes.add('j-zooming');
    const transition = document.startViewTransition(once);
    // A window that gets no frames (hidden or covered) never calls back; the face must still change.
    if (typeof window?.setTimeout === 'function') fallback = window.setTimeout(once, ZOOM_SWAP_LIMIT_MS);
    if (typeof transition?.finished?.then === 'function') transition.finished.then(done, done);
    else done();
  } catch (_) {
    done();
    once();
    return false;
  }
  return true;
}

/** Compact read-only face for the store edition: local inventory, memory, and one confirmed unload. */
export function mountSimpleView({ window, document, fetch, setInterval, clearInterval, now, store = getMonitorStore(),
  setTimeout: schedule = globalThis.setTimeout, clearTimeout: cancel = globalThis.clearTimeout }) {
  const root = document.documentElement;
  const body = document.createElement('div');
  body.id = 'svBody';
  document.body.append(body);
  let snapshot = null;
  let snapshotFresh = false;
  let control = null;
  let controlKnown = false;
  let confirming = false;
  let stopped = false;
  const post = message => window.webkit?.messageHandlers?.monitor?.postMessage(message);
  const readView = () => (window.agiwPrefs?.view === 'simple' ? 'simple' : 'advanced');

  // The face a person has asked for, which can run ahead of the page while a transition captures the old one.
  let pendingView = null;
  let latestRequest = 0;
  const applyView = (next, echo, animate = false) => {
    const mine = ++latestRequest;
    const run = () => {
      if (mine !== latestRequest) return; // a newer request replaced this one before its swap landed
      pendingView = null;
      const changed = root.dataset.view !== next;
      root.dataset.view = next;
      root.dataset.svReady = '1';
      if (switcher) switcher.textContent = next === 'simple' ? 'Overview' : 'Simple';
      if (next === 'simple') {
        const pause = document.getElementById('pause');
        if (pause) pause.setAttribute('aria-pressed', 'false');
      }
      if (echo && changed) post({ action: next === 'simple' ? 'view-simple' : 'view-advanced' });
      draw();
    };
    if (animate && root.dataset.view !== next) {
      pendingView = next;
      zoomSwap({ window, document }, run);
    } else run();
  };

  const onSwitch = event => {
    const button = event?.target?.closest?.('#advancedSwitch') || event?.currentTarget;
    if (button?.id !== 'advancedSwitch' && event?.target) return;
    applyView((pendingView ?? root.dataset.view) === 'simple' ? 'advanced' : 'simple', true, true);
  };
  const switcher = document.getElementById('advancedSwitch');
  if (switcher) switcher.addEventListener('click', onSwitch);
  else document.addEventListener?.('click', onSwitch);
  window.addEventListener('agiw:view', event => {
    if (event.detail !== 'simple' && event.detail !== 'advanced') return;
    if ((pendingView ?? root.dataset.view) === event.detail) return;
    applyView(event.detail, false, true);
  });

  const studioLive = () => (snapshot?.sources || []).some(source => source.id === 'lmstudio-api' && source.state === 'live');
  const inventoryRows = () => {
    const raw = snapshot?.models;
    if (!Array.isArray(raw)) return null;
    if (raw.some(row => !row || typeof row !== 'object' || Array.isArray(row))) return null;
    return raw;
  };
  const chosen = () => {
    const rows = inventoryRows();
    if (!rows) return null;
    return rows.find(row => row.loaded === true) || rows[0] || null;
  };
  const readNow = () => {
    const clock = typeof now === 'function' ? now() : Date.now() / 1000;
    return Number.isFinite(clock) ? clock : NaN;
  };
  const sampleIsFresh = data => {
    const at = data?.fullSampledAt ?? data?.sampledAt;
    const age = readNow() - at;
    return snapshotFresh && Number.isFinite(at) && Number.isFinite(age) && age >= -1 && age <= 3;
  };

  const draw = () => {
    body.replaceChildren();
    if (root.dataset.view !== 'simple') return;
    if (!snapshot) {
      const unknown = document.createElement('div');
      unknown.className = 'sv-row';
      unknown.setAttribute('data-row', 'lmstudio');
      unknown.setAttribute('data-tone', 'muted');
      const copy = document.createElement('p');
      copy.textContent = 'Inventory unknown. Control is unavailable.';
      unknown.append(copy);
      body.append(unknown);
      return;
    }
    const fresh = sampleIsFresh(snapshot);
    const studio = document.createElement('div');
    studio.className = 'sv-row';
    studio.setAttribute('data-row', 'lmstudio');
    studio.setAttribute('data-tone', fresh && studioLive() && snapshot.demo !== true && controlKnown ? 'ok' : 'muted');
    const studioCopy = document.createElement('p');
    if (snapshot.demo === true) studioCopy.textContent = 'Showing sample data, not your Mac';
    else if (!fresh) {
      const at = snapshot.fullSampledAt ?? snapshot.sampledAt, age = readNow() - at;
      const seen = Number.isFinite(at) && Number.isFinite(age) && age >= 0 ? ` (${Math.floor(age)} s old)` : '';
      studioCopy.textContent = `Inventory is stale${seen}. Control is unavailable.`;
    }
    else if (!studioLive()) studioCopy.textContent = "LM Studio isn't answering";
    else if (!controlKnown) studioCopy.textContent = 'Control is unavailable.';
    else studioCopy.textContent = 'LM Studio inventory is live';
    studio.append(studioCopy);
    if (snapshot.demo === true || !studioLive()) {
      const open = document.createElement('button');
      open.type = 'button';
      open.textContent = 'Open LM Studio';
      open.addEventListener('click', () => post({ action: 'open-runtime-app' }));
      studio.append(open);
    }
    const models = document.createElement('div');
    models.className = 'sv-row';
    models.setAttribute('data-row', 'models');
    const rows = inventoryRows();
    const model = chosen();
    const state = document.createElement('p');
    state.className = 'sv-model-state';
    const inventoryTrusted = fresh && snapshot.demo !== true && studioLive();
    const confirmedLoaded = rows?.find(row => row.loaded === true && row.state !== 'unknown' && row.state !== 'stale' && typeof row.state === 'string');
    const confirmedEmpty = Array.isArray(snapshot?.models) && snapshot.models.length === 0;
    const confirmedNone = Array.isArray(rows) && rows.length > 0 && rows.every(row => row.loaded === false && (row.state === 'unloaded' || row.state === 'idle'));
    state.textContent = !inventoryTrusted || !rows ? 'Model state unknown'
      : confirmedLoaded ? 'Loaded'
      : confirmedEmpty || confirmedNone ? 'No local model'
      : 'Model state unknown';
    models.append(state);
    if (model) {
      const decision = modelControlView(model, {
        host: 'mac',
        feedFresh: fresh && controlKnown && snapshot.demo !== true && studioLive(),
        supported: snapshot.modelControl?.supported === true,
        reason: snapshot.modelControl?.reason || '',
        status: control,
        models: snapshot.models || [],
        unloadMode: snapshot.modelControl?.unloadMode || null,
      });
      if (decision.action === 'unload' && !confirming) {
        const unload = document.createElement('button');
        unload.type = 'button';
        unload.textContent = `Unload ${model.name || model.id}`;
        unload.addEventListener('click', () => { confirming = true; draw(); });
        models.append(unload);
      }
      if (confirming) {
        const confirm = document.createElement('p');
        confirm.className = 'sv-confirm';
        confirm.textContent = "AGIW can't see whether it's answering right now. Unloading stops it.";
        models.append(confirm);
      }
      const path = controlSchematic({ model, control, controlKnown, decision, confirming });
      const schematic = document.createElement('pre');
      schematic.className = 'j-bus sv-control';
      schematic.setAttribute('role', 'img');
      schematic.setAttribute('aria-label', path.label);
      schematic.textContent = path.text;
      models.append(schematic);
    }
    const memory = document.createElement('div');
    memory.className = 'sv-row';
    memory.setAttribute('data-row', 'memory');
    const percent = snapshot.memory?.availablePercent;
    // A percentage is shown only for a fresh sample and a value that can be one; otherwise it is not reported.
    memory.textContent = fresh && Number.isFinite(percent) && percent >= 0 && percent <= 100 ? `${percent}% available` : 'Memory not reported';
    body.append(studio, models, memory);
  };

  const subline = document.getElementById?.('subline');
  let bus = null;
  let busFrame = 0;
  if (subline?.after) {
    bus = document.createElement('pre');
    bus.id = 'svBus';
    bus.className = 'j-bus';
    bus.setAttribute('role', 'img');
    subline.after(bus);
  }
  const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true;
  const drawBus = () => {
    if (!bus) return false;
    const view = observationBus({
      snapshot,
      fresh: sampleIsFresh(snapshot),
      studioLive: studioLive(),
      frame: busFrame,
      reducedMotion: reducedMotion(),
      paused: document.getElementById?.('pause')?.getAttribute?.('aria-pressed') === 'true',
    });
    if (bus.textContent !== view.text) bus.textContent = view.text;
    if (bus.getAttribute('aria-label') !== view.label) bus.setAttribute('aria-label', view.label);
    return view.moving;
  };
  const busTimer = bus ? setInterval(() => {
    if (root.dataset.view === 'simple') return;
    busFrame += 1;
    drawBus();
  }, 250) : null;

  let inFlight = false, request = null, controlAt = null;
  const controlRecord = body => (
    body && typeof body.status === 'string' && CONTROL_STATUSES.has(body.status) ? body : null
  );
  const redraw = () => {
    if (controlAt === null || readNow() - controlAt > 3) controlKnown = false;
    if (!(sampleIsFresh(snapshot) && controlKnown && snapshot?.demo !== true && studioLive())) confirming = false;
    draw();
    drawBus();
  };
  const tick = async () => {
    if (stopped || inFlight || !sampleIsFresh(snapshot)) return;
    inFlight = true;
    const controller = new AbortController();
    request = controller;
    let timeout;
    const deadline = new Promise((_, reject) => {
      timeout = schedule(() => { reject(new Error('Control deadline')); controller.abort(); }, 1800);
    });
    try {
      const nextControl = await Promise.race([deadline, (async () => {
        const reply = await fetch('/api/models/control', {cache: 'no-store', signal: controller.signal});
        return reply?.ok === true ? controlRecord(await reply.json()) : null;
      })()]);
      if (stopped) return;
      control = nextControl;
      controlKnown = nextControl !== null;
      controlAt = controlKnown ? readNow() : null;
    } catch (_) {
      if (stopped) return;
      control = null;
      controlKnown = false;
      controlAt = null;
    } finally {
      cancel(timeout);
      if (request === controller) request = null;
      inFlight = false;
      if (!stopped) redraw();
    }
  };

  applyView(readView(), false);
  const unsubscribe = store.subscribe(state => {
    if (stopped) return;
    snapshot = state.snapshot;
    snapshotFresh = state.snapshotStatus.fresh === true && state.snapshotStatus.available === true && !state.snapshotStatus.error;
    redraw();
    if (controlAt === null) tick();
  });
  store.start({snapshot: true});
  const timer = setInterval(() => { redraw(); tick(); }, 1000);

  return {
    destroy() {
      stopped = true;
      unsubscribe();
      request?.abort();
      clearInterval(timer);
      if (busTimer !== null) clearInterval(busTimer);
      bus?.remove?.();
      switcher?.removeEventListener?.('click', onSwitch);
      document.removeEventListener?.('click', onSwitch);
      body.remove();
    },
  };
}

if (typeof document !== 'undefined' && document.getElementById?.('advancedSwitch')) {
  mountTextSize({ window, document });
  mountSimpleView({
    window,
    document,
    fetch,
    setInterval,
    clearInterval,
  });
}
