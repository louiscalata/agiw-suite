import { modelControlView } from '/model-control-view.mjs';

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
  const status = control?.status, settlement = control?.settlement;
  const action = String((status === 'idle' ? decision?.action : control?.action) || 'control').toUpperCase();
  let marks, state;
  if (status === 'running') {
    marks = ['x', 'x', '>', ' '];
    state = 'RUNNING';
  } else if (status === 'reported') {
    marks = settlement === 'confirmed' ? ['x', 'x', 'x', 'x'] : ['x', 'x', 'x', '?'];
    state = settlement === 'confirmed' ? 'DONE, INVENTORY CONFIRMS' : 'REPORTED, WAITING FOR INVENTORY';
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

/** Compact read-only face for the store edition: local inventory, memory, and one confirmed unload. */
export function mountSimpleView({ window, document, fetch, setInterval, clearInterval, now }) {
  const root = document.documentElement;
  const body = document.createElement('div');
  body.id = 'svBody';
  document.body.append(body);
  let snapshot = null;
  let control = null;
  let controlKnown = false;
  let confirming = false;
  let stopped = false;
  const post = message => window.webkit?.messageHandlers?.monitor?.postMessage(message);
  const readView = () => (window.agiwPrefs?.view === 'simple' ? 'simple' : 'advanced');

  const applyView = (next, echo) => {
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

  const onSwitch = event => {
    const button = event?.target?.closest?.('#advancedSwitch') || event?.currentTarget;
    if (button?.id !== 'advancedSwitch' && event?.target) return;
    applyView(root.dataset.view === 'simple' ? 'advanced' : 'simple', true);
  };
  const switcher = document.getElementById('advancedSwitch');
  if (switcher) switcher.addEventListener('click', onSwitch);
  else document.addEventListener?.('click', onSwitch);
  window.addEventListener('agiw:view', event => {
    if (event.detail !== 'simple' && event.detail !== 'advanced') return;
    if (root.dataset.view === event.detail) return;
    applyView(event.detail, false);
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
    return Number.isFinite(at) && Number.isFinite(age) && age >= -1 && age <= 3;
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
    else if (!studioLive()) studioCopy.textContent = "LM Studio isn't answering";
    else if (!fresh) studioCopy.textContent = 'Inventory is stale. Control is unavailable.';
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
    memory.textContent = Number.isFinite(percent) ? `${percent}% available` : 'Memory not reported';
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

  const CONTROL_STATUSES = new Set(['idle', 'running', 'reported', 'failed']);
  let pollGeneration = 0;
  const acceptedBody = async reply => {
    if (reply?.ok !== true) return null;
    const body = await reply.json();
    return body && typeof body === 'object' && !Array.isArray(body) ? body : null;
  };
  const controlRecord = body => (
    body && typeof body.status === 'string' && CONTROL_STATUSES.has(body.status) ? body : null
  );
  const tick = async () => {
    if (stopped) return;
    const mine = ++pollGeneration;
    const current = () => !stopped && mine === pollGeneration;
    let nextSnapshot = null;
    let nextControl = null;
    let snapshotAccepted = false;
    let nextControlKnown = false;
    try {
      const snapshotReply = await fetch('/api/snapshot');
      if (!current()) return;
      nextSnapshot = await acceptedBody(snapshotReply);
      snapshotAccepted = nextSnapshot !== null;
    } catch (_) {
      snapshotAccepted = false;
    }
    if (!current()) return;
    if (snapshotAccepted) {
      try {
        const controlReply = await fetch('/api/models/control');
        if (!current()) return;
        nextControl = controlRecord(await acceptedBody(controlReply));
        nextControlKnown = nextControl !== null;
      } catch (_) {
        nextControlKnown = false;
      }
    }
    if (!current()) return;
    if (!snapshotAccepted) {
      snapshot = null;
      control = null;
      controlKnown = false;
    } else {
      snapshot = nextSnapshot;
      control = nextControlKnown ? nextControl : null;
      controlKnown = nextControlKnown;
    }
    if (!(sampleIsFresh(snapshot) && controlKnown && snapshot?.demo !== true && studioLive())) confirming = false;
    draw();
    drawBus();
  };

  applyView(readView(), false);
  const timer = setInterval(() => { tick(); }, 1000);
  tick();
  return {
    destroy() {
      stopped = true;
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
