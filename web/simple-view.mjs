import { modelControlView } from '/model-control-view.mjs';

/** Compact read-only face for the store edition: local inventory, memory, and one confirmed unload. */
export function mountSimpleView({ window, document, fetch, setInterval, clearInterval }) {
  const root = document.documentElement;
  const body = document.createElement('div');
  body.id = 'svBody';
  document.body.append(body);
  let snapshot = null;
  let control = null;
  let confirming = false;
  let stopped = false;
  const post = message => window.webkit?.messageHandlers?.monitor?.postMessage(message);
  const readView = () => (window.agiwPrefs?.view === 'advanced' ? 'advanced' : 'simple');

  const applyView = (next, echo) => {
    const changed = root.dataset.view !== next;
    root.dataset.view = next;
    root.dataset.svReady = '1';
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
  const chosen = () => (snapshot?.models || []).find(model => model.loaded === true) || (snapshot?.models || [])[0] || null;

  const draw = () => {
    body.replaceChildren();
    if (root.dataset.view !== 'simple' || !snapshot) return;
    const studio = document.createElement('div');
    studio.className = 'sv-row';
    studio.setAttribute('data-row', 'lmstudio');
    studio.setAttribute('data-tone', studioLive() && snapshot.demo !== true ? 'ok' : 'muted');
    const studioCopy = document.createElement('p');
    if (snapshot.demo === true) studioCopy.textContent = 'Showing sample data, not your Mac';
    else if (!studioLive()) studioCopy.textContent = "LM Studio isn't answering";
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
    const model = chosen();
    const state = document.createElement('p');
    state.className = 'sv-model-state';
    state.textContent = model?.state === 'loaded' ? 'Loaded' : 'No local model';
    models.append(state);
    if (model) {
      const decision = modelControlView(model, {
        host: 'mac',
        feedFresh: true,
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
    }
    const memory = document.createElement('div');
    memory.className = 'sv-row';
    memory.setAttribute('data-row', 'memory');
    const percent = snapshot.memory?.availablePercent;
    memory.textContent = Number.isFinite(percent) ? `${percent}% available` : 'Memory not reported';
    body.append(studio, models, memory);
  };

  const tick = async () => {
    if (stopped) return;
    try {
      const snapshotReply = await fetch('/api/snapshot');
      snapshot = await snapshotReply.json();
      const controlReply = await fetch('/api/models/control');
      control = await controlReply.json();
    } catch (_) {
      snapshot = snapshot || null;
    }
    draw();
  };

  applyView(readView(), false);
  const timer = setInterval(() => { tick(); }, 1000);
  tick();
  return {
    destroy() {
      stopped = true;
      clearInterval(timer);
      switcher?.removeEventListener?.('click', onSwitch);
      document.removeEventListener?.('click', onSwitch);
      body.remove();
    },
  };
}

if (typeof document !== 'undefined' && document.getElementById?.('advancedSwitch')) {
  mountSimpleView({
    window,
    document,
    fetch,
    setInterval,
    clearInterval,
  });
}
