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

function machinesStep(snapshot) {
  // This machine is the first row that names the snapshot's host (as the Machines page and the peer map read it); a
  // second PC or a second Mac is another machine and counts.
  const rows = Array.isArray(snapshot?.machines) ? snapshot.machines.filter(row => row && typeof row === 'object') : [];
  const own = rows.findIndex(row => row.host === snapshot.host || row.platform === snapshot.host);
  const others = rows.filter((_, i) => i !== own);
  const up = others.filter(row => row.state === 'up').length;
  return !snapshot
    ? step('machines', 'Other machines (optional)', 'UNKNOWN', 'No snapshot could be read.')
    : up
      ? step('machines', 'Other machines (optional)', 'DONE', `${up} other machine${up === 1 ? ' is' : 's are'} up.`, null, '/machines')
      : step('machines', 'Other machines (optional)', 'OPTIONAL', others.length ? `${others.length} other machine${others.length === 1 ? '' : 's'} known, none up.` : 'No other machine in this sample.',
        'Only if you use one: the PC worker and coding router are installed and set up separately.', '/machines');
}

// The PC's own row says how its lanes are doing, as `reasonCode` (the shared words) or, on older PC builds,
// as a hyphenated `reason`. Both are read; neither is guessed from the other when absent.
const PC_REASON_CODES = {
  serving: 'observed', 'partly-serving': 'reported_degraded', 'no-lane-serving': 'reported_down',
  'lane-state-unknown': 'unavailable', stale: 'stale', unconfigured: 'not_observed', 'config-unreadable': 'not_observed',
};
const pcLaneCode = row => {
  if (row?.reasonCode !== undefined && row?.reasonCode !== null) {
    return typeof row.reasonCode === 'string' && /^[a-z_]{1,40}$/.test(row.reasonCode) ? row.reasonCode : null;
  }
  return typeof row?.reason === 'string' && Object.prototype.hasOwnProperty.call(PC_REASON_CODES, row.reason) ? PC_REASON_CODES[row.reason] : null;
};
// What to do about a lane state that is not known, by why it is not known.
const LANE_REMEDY = {
  not_observed: 'Enroll the local lanes in lane-processes.json beside the observer, then restart it.',
  unavailable: 'Enroll the local lanes in lane-processes.json beside the observer, then restart it.',
  stale: 'Wait for a fresh sample. If it stays old, start AGIW Monitor again.',
  clock_skew: 'Check the PC clock, then wait for a fresh sample.',
};

/** The Windows edition's steps. Nisi, Node and Jev belong to the Mac app, so they are not listed here. */
function windowsSteps({snapshot, fresh}) {
  const steps = [];
  steps.push(fresh
    ? step('monitor', 'The monitor is running', 'DONE', 'A fresh sample arrived from the observer.')
    : step('monitor', 'The monitor is running', 'NOT YET', 'The last sample is old.', 'Wait for LIVE at the top; if it stays stale, start AGIW Monitor again (AGIW Monitor.vbs in the install folder).'));
  const rows = Array.isArray(snapshot?.machines) ? snapshot.machines.filter(row => row && typeof row === 'object') : [];
  const mine = rows.find(row => row.platform === 'windows') || null;
  const code = pcLaneCode(mine);
  if (!fresh || !mine) {
    steps.push(step('lanes', 'A model lane is serving', 'UNKNOWN', !fresh ? 'Needs a fresh sample first.' : 'This PC has no row in the sample.'));
  } else if (code === 'observed' && mine.state === 'up') {
    steps.push(step('lanes', 'A model lane is serving', 'DONE', 'A model lane on this PC reports that it is serving.'));
  } else if (code === 'reported_degraded' && mine.state === 'up') {
    steps.push(step('lanes', 'A model lane is serving', 'NOT YET', 'Some lanes on this PC are serving, not all.', 'Start the lane that is not serving.'));
  } else if (code === 'reported_down' && mine.state === 'down') {
    steps.push(step('lanes', 'A model lane is serving', 'NOT YET', 'No lane on this PC is serving.', 'Start a model lane on this PC.'));
  } else {
    steps.push(step('lanes', 'A model lane is serving', 'UNKNOWN', 'The lane state of this PC is not known.', LANE_REMEDY[code] || null));
  }
  steps.push(machinesStep(snapshot));
  return steps;
}

/** One line for pages whose steps are not the Mac app's, or null. */
export function setupNote(snapshot) {
  return snapshot?.host === 'windows'
    ? 'This is the Windows edition. Nisi, Node.js and hosted Jev are set up in the Mac app, so they are not steps here.' : null;
}

/** The steps in the guide's order. `components` and `jev` are the GET replies, or null when unread. */
export function setupSteps({snapshot, fresh, components, jev}) {
  if (snapshot?.host === 'windows') return windowsSteps({snapshot, fresh: fresh === true});
  // Without a snapshot the edition is not known either, so only the monitor itself is listed.
  if (!snapshot) {
    return [step('monitor', 'The monitor is running', 'UNKNOWN', 'No snapshot could be read from the observer.', 'Start the monitor app. A fresh reading shows LIVE at the top.')];
  }
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
  const needsNode = components?.check?.state === 'needs-node';
  steps.push(!components
    ? step('node', 'Node.js is installed for Nisi', 'UNKNOWN', 'The component status could not be read.')
    : needsNode
      ? step('node', 'Node.js is installed for Nisi', 'NOT YET', 'Node.js is missing, too old, or not running natively on Apple silicon.', 'Install Node.js 22 or newer for Apple silicon.')
    : node?.found === true
      ? step('node', 'Node.js is installed for Nisi', 'DONE', 'Node.js was found.')
      : step('node', 'Node.js is installed for Nisi', 'NOT YET', 'Node.js was not found.', 'Install Node.js 22 or newer for Apple silicon.'));

  const check = components?.check?.state;
  steps.push(!components
    ? step('selfcheck', 'Nisi passed its self-check', 'UNKNOWN', 'The component status could not be read.')
    : check === 'passed'
      ? step('selfcheck', 'Nisi passed its self-check', 'DONE', 'The fixed workflow self-check passed. No model was called.')
      : check === 'needs-node'
        ? step('selfcheck', 'Nisi passed its self-check', 'NOT YET', 'The self-check needs a usable Node.js first.', 'Install Node.js, then press Run Nisi self-check on Components.', '/components')
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

  steps.push(machinesStep(snapshot));
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
  const note = document.getElementById('setupNote');
  if (note) {
    const line = setupNote(state.snapshot);
    note.textContent = line || '';
    note.hidden = !line;
  }
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
  let stopped = false, timer = null, ticks = 0, nextComponentsAt = 0, components = null, jev = null;
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
    // The component routes are the Mac app's; the Windows edition answers 501 to them, so it is not asked.
    if (snapshot?.host === 'mac' && ticks >= nextComponentsAt) {
      nextComponentsAt = ticks + 10;
      [components, jev] = await Promise.all([read('/api/components'), read('/api/components/jev')]);
    }
    ticks += 1;
    if (stopped) return;
    const stamp = snapshot?.fullSampledAt ?? snapshot?.sampledAt;
    const age = typeof stamp === 'number' ? now() - stamp : NaN;
    paintSetup(document, {snapshot, fresh: Number.isFinite(age) && age >= -1 && age <= 3, components, jev});
    timer = setTimeout(tick, 1000);
  };
  tick();
  return {destroy() { stopped = true; if (timer !== null) clearTimeout(timer); }};
}

if (typeof document !== 'undefined' && document.getElementById?.('setupSteps')) {
  mountSetup({document, fetch: (...args) => fetch(...args), setTimeout: (...args) => setTimeout(...args)});
}
