/**
 * Setup: the activation guide (docs/activation.md) as a checklist that reads what is already true.
 * Read only. It never installs, starts, loads, saves or checks anything; every action stays with the
 * person, in LM Studio, a terminal, or the Components page. Each step is DONE, NOT YET, FAILED, UNKNOWN
 * or OPTIONAL, from evidence the monitor already reads. No path or free text from a reply is shown.
 */

const text = (value, max = 160) => (typeof value === 'string' && /^[\x20-\x7e]+$/.test(value) ? value.slice(0, max) : null);
const source = (snapshot, id) => (Array.isArray(snapshot?.sources) ? snapshot.sources.find(row => row?.id === id) : undefined);

function step(id, title, status, evidence, next = null, link = null) {
  return {id, title, status, evidence, next, link};
}

/** The steps in the guide's order. `components` and `jev` are the GET replies, or null when unread. */
export function setupSteps({snapshot, fresh, components, jev}) {
  const steps = [];
  steps.push(!snapshot
    ? step('monitor', 'The monitor is running', 'UNKNOWN', 'No snapshot could be read from the observer.', 'Open the Inference Monitor app. A fresh reading shows LIVE at the top.')
    : fresh
      ? step('monitor', 'The monitor is running', 'DONE', 'A fresh sample arrived from the observer.')
      : step('monitor', 'The monitor is running', 'NOT YET', 'The last sample is old.', 'Wait for LIVE at the top; if it stays stale, restart the Inference Monitor app.'));

  const live = fresh === true;
  const api = source(snapshot, 'lmstudio-api');
  steps.push(!snapshot || !live
    ? step('lmstudio', 'LM Studio is serving models', 'UNKNOWN', 'Needs a fresh sample first.')
    : api?.state === 'live'
      ? step('lmstudio', 'LM Studio is serving models', 'DONE', "LM Studio's local server is answering.")
      : step('lmstudio', 'LM Studio is serving models', 'NOT YET', api ? "LM Studio's local server is not answering." : 'No LM Studio source in this sample.',
        "In LM Studio's Developer tab, start the API server on 127.0.0.1:1234."));

  const cli = source(snapshot, 'lms-ps');
  steps.push(!snapshot || !live
    ? step('activity', 'Model activity is visible', 'UNKNOWN', 'Needs a fresh sample first.')
    : cli?.state === 'live' && snapshot.activityKnown === true
      ? step('activity', 'Model activity is visible', 'DONE', "LM Studio's command line reports which models are working.")
      : step('activity', 'Model activity is visible', 'NOT YET', 'Activity is unknown: the LM Studio command line is not reporting.',
        "Install LM Studio's command line (lms) in your home folder; model controls need that copy."));

  const models = Array.isArray(snapshot?.models) ? snapshot.models.filter(row => row && typeof row === 'object') : null;
  const loaded = models ? models.filter(row => row.loaded === true).length : 0;
  steps.push(!live || api?.state !== 'live' || !models
    ? step('model', 'A local model is loaded', 'UNKNOWN', 'Needs a fresh, live LM Studio inventory first.')
    : loaded
      ? step('model', 'A local model is loaded', 'DONE', `${loaded} model${loaded === 1 ? '' : 's'} loaded of ${models.length} installed.`)
      : step('model', 'A local model is loaded', 'NOT YET', models.length ? `${models.length} installed, none loaded.` : 'No model is installed in LM Studio.',
        models.length ? 'Load a model in LM Studio, or select one in Overview and press Load.' : 'Download a model in LM Studio that fits this computer\'s memory.'));

  const nisi = components?.nisi;
  steps.push(!components
    ? step('nisi', 'Nisi is included and intact', 'UNKNOWN', 'The component status could not be read.')
    : nisi?.integrity === 'verified'
      ? step('nisi', 'Nisi is included and intact', 'DONE', `Nisi ${text(nisi.version, 20) || ''} package integrity verified.`.replace('  ', ' '))
      : step('nisi', 'Nisi is included and intact', 'NOT YET', 'The bundled Nisi package is not verified.', 'Reinstall the app; a changed package is not repaired in place.'));

  const node = components?.node;
  steps.push(!components
    ? step('node', 'Node.js is installed for Nisi', 'UNKNOWN', 'The component status could not be read.')
    : node?.found === true
      ? step('node', 'Node.js is installed for Nisi', 'DONE', 'Node.js was found.')
      : step('node', 'Node.js is installed for Nisi', 'NOT YET', 'Node.js was not found.', 'Install Node.js 22 or newer for Apple silicon.'));

  const check = components?.check?.state;
  steps.push(!components
    ? step('selfcheck', 'Nisi passed its self-check', 'UNKNOWN', 'The component status could not be read.')
    : check === 'passed'
      ? step('selfcheck', 'Nisi passed its self-check', 'DONE', 'The fixed workflow self-check passed. No model was called.')
      : check === 'failed'
        ? step('selfcheck', 'Nisi passed its self-check', 'FAILED', text(components.check.message) || 'The self-check did not pass.', 'Open Components to read the report and run it again.', '/components')
        : check === 'running'
          ? step('selfcheck', 'Nisi passed its self-check', 'NOT YET', 'The self-check is running.')
          : step('selfcheck', 'Nisi passed its self-check', 'NOT YET', 'The self-check has not run.', 'Open Components and press Run Nisi self-check. It calls no model.', '/components'));

  steps.push(!jev
    ? step('jev', 'Hosted Jev (optional)', 'UNKNOWN', 'The Jev status could not be read.')
    : jev.configured === true
      ? step('jev', 'Hosted Jev (optional)', 'DONE', 'A Jev key is saved in the Keychain.')
      : step('jev', 'Hosted Jev (optional)', 'OPTIONAL', text(jev.message) || 'No Jev key is saved.', 'Only if you want it: set it up from Components inside the installed app.', '/components'));

  const others = Array.isArray(snapshot?.machines)
    ? snapshot.machines.filter(row => row && typeof row === 'object' && row.host !== snapshot.host && row.platform !== snapshot.host) : [];
  const up = others.filter(row => row.state === 'up').length;
  steps.push(!snapshot
    ? step('machines', 'Other machines (optional)', 'UNKNOWN', 'No snapshot could be read.')
    : up
      ? step('machines', 'Other machines (optional)', 'DONE', `${up} other machine${up === 1 ? ' is' : 's are'} up.`, null, '/machines')
      : step('machines', 'Other machines (optional)', 'OPTIONAL', others.length ? `${others.length} other machine${others.length === 1 ? '' : 's'} known, none up.` : 'No other machine in this sample.',
        'Only if you use one: the PC worker and coding router are installed and set up separately.', '/machines'));
  return steps;
}

/** "N of M required steps done", counting only the steps that are not optional. */
export function setupProgress(steps) {
  const required = steps.filter(item => !['jev', 'machines'].includes(item.id));
  return {done: required.filter(item => item.status === 'DONE').length, total: required.length};
}

const GLYPH = {DONE: '■', 'NOT YET': '□', FAILED: '×', UNKNOWN: '?', OPTIONAL: '·'};

function el(document, tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

export function paintSetup(document, state) {
  const list = document.getElementById('setupSteps');
  if (!list) return;
  const steps = setupSteps(state);
  const {done, total} = setupProgress(steps);
  const progress = document.getElementById('setupProgress');
  if (progress) progress.textContent = `${done} of ${total} required steps done.`;
  const items = steps.map((item, i) => {
    const row = el(document, 'li', `j-setup-step j-setup-${item.status.toLowerCase().replace(' ', '-')}`);
    row.setAttribute('aria-label', `Step ${i + 1}, ${item.title}: ${item.status.toLowerCase()}. ${item.evidence}${item.next ? ' ' + item.next : ''}`);
    const head = el(document, 'p', 'j-status');
    head.append(el(document, 'span', 'j-glyph', GLYPH[item.status]), el(document, 'span', 'j-word', item.status));
    row.append(el(document, 'h2', 'j-setup-title', `${i + 1}. ${item.title}`), head, el(document, 'p', 'j-setup-evidence', item.evidence));
    if (item.next) {
      const next = el(document, 'p', 'j-setup-next', item.next);
      if (item.link) {
        const link = el(document, 'a', null, item.link === '/components' ? 'Open Components' : 'Open Machines');
        link.href = item.link;
        next.append(' ', link);
      }
      row.append(next);
    }
    return row;
  });
  list.replaceChildren(...items);
}

/** Reads the snapshot every second and the component status every ten; only GET requests. */
export function mountSetup({document, fetch, setTimeout, now = () => Date.now() / 1000}) {
  let stopped = false, timer = null, ticks = 0, components = null, jev = null;
  const read = async path => {
    try {
      const reply = await fetch(path, {cache: 'no-store'});
      if (!reply.ok) return null;
      const body = await reply.json();
      return body && typeof body === 'object' && !Array.isArray(body) ? body : null;
    } catch (_) {
      return null;
    }
  };
  const tick = async () => {
    const snapshot = await read('/api/snapshot');
    if (ticks % 10 === 0) [components, jev] = await Promise.all([read('/api/components'), read('/api/components/jev')]);
    ticks += 1;
    if (stopped) return;
    const age = now() - snapshot?.sampledAt;
    paintSetup(document, {snapshot, fresh: Number.isFinite(age) && age >= -2 && age <= 5, components, jev});
    timer = setTimeout(tick, 1000);
  };
  tick();
  return {destroy() { stopped = true; if (timer !== null) clearTimeout(timer); }};
}

if (typeof document !== 'undefined' && document.getElementById?.('setupSteps')) {
  mountSetup({document, fetch: (...args) => fetch(...args), setTimeout: (...args) => setTimeout(...args)});
}
