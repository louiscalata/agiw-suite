// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// The observer discovers OpenCode settings and tests explicitly saved MCP endpoints.
// Neither operation grants an agent a tool or invokes tools/call.
const ID = /^[a-z][a-z0-9-]{0,31}$/;
const LOOPBACK = /^http:\/\/127\.0\.0\.1:(\d{4,5})(\/[A-Za-z0-9._~/-]{0,127})$/;
const STATES = new Set(['not-tested', 'checking', 'ready', 'error']);
const ACTION_ERRORS = Object.freeze({
  INVALID_ID: 'Use a lowercase name starting with a letter, up to 32 letters, numbers, or hyphens.',
  INVALID_URL: 'Use a literal http://127.0.0.1:PORT/path endpoint on port 1024–65535.',
  ID_EXISTS: 'That name is already saved for a different endpoint. Remove the saved endpoint first.',
  LIMIT_REACHED: 'Eight managed endpoints are already saved. Remove one to add another.',
  TEST_BUSY: 'Another endpoint test is in progress. Wait for its result.',
  NOT_MANAGED: 'That endpoint is no longer saved. Refresh the list.',
  CONFIG_UNSAFE: 'Saved endpoint settings cannot be read safely.',
  CONFIG_INVALID: 'Saved endpoint settings are invalid.',
  SAVE_FAILED: 'The endpoint could not be saved safely.',
  STOPPED: 'The local connector service is stopping.',
  TEST_UNAVAILABLE: 'The endpoint test could not start.',
});
const TEST_ERRORS = Object.freeze({
  MCP_UNREACHABLE: 'The endpoint did not answer within the test limit.',
  MCP_HTTP_ERROR: 'The endpoint rejected the protocol check.',
  REDIRECT_REFUSED: 'The endpoint redirected the protocol check.',
  INVALID_SESSION: 'The endpoint returned an invalid session.',
  UNSUPPORTED_TRANSPORT: 'The endpoint response transport is unsupported.',
  INVALID_MCP_RESPONSE: 'The endpoint returned an invalid MCP response.',
  INTERACTIVE_SERVER_UNSUPPORTED: 'The endpoint requested an unsupported interaction.',
  SSE_INCOMPLETE: 'The endpoint stream ended before its response.',
  INVALID_EVENT_ID: 'The endpoint stream returned an invalid event ID.',
  SSE_RETRY_UNSUPPORTED: 'The endpoint requested an unsupported stream retry.',
  SSE_RESUME_UNSUPPORTED: 'The endpoint cannot resume its stream within the test.',
  SSE_RESUME_LIMIT: 'The endpoint exceeded the stream resume limit.',
  MCP_RESPONSE_TOO_LARGE: 'The endpoint response exceeded the test limit.',
  UNSUPPORTED_VERSION: 'The endpoint returned an unsupported protocol version.',
  NO_TOOLS_CAPABILITY: 'The endpoint did not declare tool discovery.',
  MCP_ERROR: 'The endpoint rejected the protocol check.',
  INVALID_TOOL_LIST: 'The endpoint returned an invalid tool list.',
  TOO_MANY_PAGES: 'The endpoint tool list exceeded the page limit.',
  TEST_FAILED: 'The endpoint test failed.',
});

export function validLoopbackEndpoint(value) {
  if (typeof value !== 'string' || value.length > 180) return false;
  const match = LOOPBACK.exec(value);
  if (!match || Number(match[1]) < 1024 || Number(match[1]) > 65535 || match[1].startsWith('0')) return false;
  const path = match[2];
  return !path.includes('//') && !path.split('/').some(part => part === '.' || part === '..');
}

function validTest(value) {
  if (!value || typeof value !== 'object' || !STATES.has(value.state)) return false;
  if (value.state === 'ready') return typeof value.protocolVersion === 'string'
    && value.protocolVersion.length <= 32 && Number.isSafeInteger(value.toolCount)
    && value.toolCount >= 0 && Number.isFinite(value.checkedAtUnix);
  if (value.state === 'error') return typeof value.code === 'string'
    && value.code.length <= 64 && Number.isFinite(value.checkedAtUnix);
  return true;
}

export function toolConnectionRows(value) {
  if (value?.schemaVersion !== 1 || !Array.isArray(value.connectors)
      || value.connectors.length > 72 || !['readable', 'missing', 'unavailable'].includes(value.opencodeConfig?.state)) {
    throw new Error('TOOL_CONNECTION_STATUS_INVALID');
  }
  const names = new Set();
  for (const row of value.connectors) {
    if (!row || typeof row.id !== 'string' || row.id.length > 73 || names.has(row.id)
        || !['opencode', 'agiw'].includes(row.source) || typeof row.configured !== 'boolean'
        || row.agentPermission !== 'unknown' || !validTest(row.transportTest)) {
      throw new Error('TOOL_CONNECTION_STATUS_INVALID');
    }
    if (row.source === 'agiw') {
      if (!ID.test(row.id) || !validLoopbackEndpoint(row.url) || row.transport !== 'streamable-http'
          || row.configured !== true || row.enabledInOpenCode !== false
          || row.detailCode !== 'AGIW_TEST_REGISTRY_ONLY') throw new Error('TOOL_CONNECTION_STATUS_INVALID');
    } else if (!/^opencode:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(row.id)
        || !['local', 'remote', 'unknown'].includes(row.transport)
        || ![true, false, null].includes(row.enabledInOpenCode)
        || row.transportTest.state !== 'not-tested'
        || !['CONFIGURED_ONLY', 'INVALID_ENTRY'].includes(row.detailCode)) {
      throw new Error('TOOL_CONNECTION_STATUS_INVALID');
    }
    names.add(row.id);
  }
  if (value.connectors.filter(row => row.source === 'agiw').length > 8) {
    throw new Error('TOOL_CONNECTION_STATUS_INVALID');
  }
  return value;
}

const actionError = code => Object.hasOwn(ACTION_ERRORS, code)
  ? ACTION_ERRORS[code] : 'The connector action failed. Refresh and try again.';
const testError = code => Object.hasOwn(TEST_ERRORS, code)
  ? TEST_ERRORS[code] : 'The endpoint protocol check failed.';
const checkedTime = value => {
  const date = new Date(value * 1000);
  return Number.isFinite(date.getTime()) ? date.toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC') : 'time unknown';
};

export function mountToolConnections(root, {
  fetch: fetcher = globalThis.fetch,
  setTimeout: later = globalThis.setTimeout,
  clearTimeout: cancel = globalThis.clearTimeout,
} = {}) {
  if (!root || root.dataset.toolConnectionsMounted) return null;
  const doc = root.ownerDocument;
  const form = root.querySelector('[data-tool-add]');
  const name = root.querySelector('[data-tool-id]');
  const url = root.querySelector('[data-tool-url]');
  const add = root.querySelector('[data-tool-submit]');
  const status = root.querySelector('[data-tool-status]');
  const actionStatus = root.querySelector('[data-tool-action-status]');
  const openCodeStatus = root.querySelector('[data-opencode-status]');
  const openCodeList = root.querySelector('[data-opencode-list]');
  const managedStatus = root.querySelector('[data-managed-status]');
  const managedList = root.querySelector('[data-managed-list]');
  if ([form, name, url, add, status, actionStatus, openCodeStatus, openCodeList,
    managedStatus, managedList].some(node => !node)) return null;
  root.dataset.toolConnectionsMounted = 'true';
  let timer = null, current = null, busy = false, disposed = false, revision = 0, reading = false;
  let awaitedTest = null;
  let controls = new Map();
  function focusedAction() {
    for (const [id, buttons] of controls) {
      if (doc.activeElement === buttons.test) return {id, action: 'test'};
      if (doc.activeElement === buttons.disconnect) return {id, action: 'disconnect'};
    }
    return null;
  }
  function restoreFocus(target) {
    if (!target) return;
    const buttons = controls.get(target.id);
    const preferred = buttons?.[target.action];
    // A disabled Test action must never move keyboard focus to Remove.
    const destination = preferred && !preferred.disabled ? preferred : name;
    destination.focus?.();
  }
  const element = (tag, className, content) => {
    const node = doc.createElement(tag);
    node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const setStatus = (node, value) => { if (node.textContent !== value) node.textContent = value; };
  function schedule() {
    cancel(timer);
    if (!disposed) timer = later(refresh, current?.connectors.some(row =>
      row.source === 'agiw' && row.transportTest.state === 'checking') ? 1000 : 8000);
  }
  function render(value) {
    const focus = focusedAction();
    current = value;
    const discovered = value.connectors.filter(row => row.source === 'opencode');
    const managed = value.connectors.filter(row => row.source === 'agiw');
    setStatus(status, 'Local connector settings loaded. A protocol result is a point-in-time check.');
    setStatus(openCodeStatus, value.opencodeConfig.state === 'missing'
      ? 'No global OpenCode MCP config file found at the inspected path.'
      : value.opencodeConfig.state === 'unavailable'
        ? 'The inspected global OpenCode file cannot be read safely.'
        : discovered.length ? `${discovered.length} MCP entries found in the inspected global file. No server or agent permission was tested.`
          : 'No MCP entries found in the inspected global OpenCode file.');
    openCodeList.replaceChildren();
    for (const row of discovered) {
      const item = element('li', 'tool-connection-row');
      item.append(element('strong', 'tool-connection-name', row.id),
        element('span', 'tool-connection-state', row.configured
          ? row.enabledInOpenCode === true ? 'Enabled setting in inspected global file'
            : row.enabledInOpenCode === false ? 'Disabled setting in inspected global file'
              : 'Enabled setting in inspected global file unknown'
          : 'Invalid configuration entry'),
        element('p', 'tool-connection-detail', `Transport: ${row.transport}. Agent permission: unknown. ${row.configured
          ? 'Configured only; live connection and model use unverified.' : 'Check the OpenCode config directly.'}`));
      openCodeList.append(item);
    }
    setStatus(managedStatus, `${managed.length} of 8 saved. AGIW tests these endpoints only; OpenCode permissions stay unchanged.`);
    managedList.replaceChildren();
    const newControls = new Map();
    const anyChecking = managed.some(row => row.transportTest.state === 'checking');
    for (const row of managed) {
      const item = element('li', 'tool-connection-row');
      item.dataset.state = row.transportTest.state;
      const state = row.transportTest;
      let label = 'Configured · not tested', detail = 'Save does not contact the server.';
      if (state.state === 'checking') {
        label = 'Checking protocol…'; detail = 'Initializing MCP and listing tools. No tool is invoked.';
      } else if (state.state === 'ready') {
        label = `Ready at ${checkedTime(state.checkedAtUnix)} · protocol discovery only`;
        detail = `${state.toolCount} tool${state.toolCount === 1 ? '' : 's'} listed · MCP ${state.protocolVersion}. Agent permission and model use remain unverified.`;
      } else if (state.state === 'error') {
        label = `Protocol check failed at ${checkedTime(state.checkedAtUnix)}`;
        detail = Object.hasOwn(TEST_ERRORS, state.code)
          ? `${state.code}: ${testError(state.code)}` : testError(state.code);
      }
      const endpoint = element('code', 'tool-connection-endpoint', row.url);
      const test = element('button', 'tool-connection-test', 'Test protocol');
      test.type = 'button';
      test.disabled = busy || anyChecking;
      test.setAttribute('aria-label', `Test ${row.id} protocol`);
      const disconnect = element('button', 'tool-connection-disconnect', 'Remove saved endpoint');
      disconnect.type = 'button';
      disconnect.disabled = busy;
      disconnect.setAttribute('aria-label', `Remove ${row.id} from AGIW; the external server stays running`);
      test.addEventListener('click', () => submit('test', row.id));
      disconnect.addEventListener('click', () => submit('disconnect', row.id));
      newControls.set(row.id, {test, disconnect});
      const actions = element('div', 'tool-connection-actions');
      actions.append(test, disconnect);
      item.append(element('strong', 'tool-connection-name', row.id), endpoint,
        element('span', 'tool-connection-state', label),
        element('p', 'tool-connection-detail', detail), actions);
      managedList.append(item);
    }
    controls = newControls;
    if (!busy) restoreFocus(focus);
    add.disabled = busy || managed.length >= 8;
    if (awaitedTest) {
      const tested = managed.find(row => row.id === awaitedTest);
      if (!tested) awaitedTest = null;
      else if (tested.transportTest.state === 'ready' || tested.transportTest.state === 'error') {
        actionStatus.textContent = tested.transportTest.state === 'ready'
          ? `Protocol discovery completed for ${awaitedTest}. Agent permission remains unknown.`
          : `Protocol check failed for ${awaitedTest}: ${testError(tested.transportTest.code)}`;
        awaitedTest = null;
      }
    }
  }
  function unavailable(code) {
    const focus = focusedAction();
    current = null;
    awaitedTest = null;
    setStatus(status, code && ACTION_ERRORS[code] ? ACTION_ERRORS[code]
      : 'Tool Connections status is unavailable. Reload the page to retry.');
    setStatus(openCodeStatus, 'Discovery unavailable.');
    setStatus(managedStatus, 'Saved endpoint status unavailable.');
    openCodeList.replaceChildren();
    managedList.replaceChildren();
    controls = new Map();
    add.disabled = true;
    if (focus) name.focus?.();
  }
  async function refresh() {
    if (disposed || reading) return;
    reading = true;
    const version = revision;
    try {
      const response = await fetcher('/api/tool-connectors', {
        cache: 'no-store', signal: AbortSignal.timeout(5000),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body?.code === 'string' ? body.code : 'STATUS_UNAVAILABLE');
      const value = toolConnectionRows(body);
      // Keep existing nodes and the reader's virtual cursor on an unchanged poll.
      if (!disposed && !busy && version === revision
          && (!current || JSON.stringify(current) !== JSON.stringify(value))) render(value);
    } catch (error) {
      if (!disposed && !busy && version === revision) unavailable(error.message);
    } finally {
      reading = false;
      schedule();
    }
  }
  async function submit(action, id, endpoint) {
    if (disposed || busy || !current || !['add', 'test', 'disconnect'].includes(action)) return false;
    if (action === 'add') {
      if (!ID.test(id)) { actionStatus.textContent = actionError('INVALID_ID'); return false; }
      if (!validLoopbackEndpoint(endpoint)) { actionStatus.textContent = actionError('INVALID_URL'); return false; }
    } else if (!ID.test(id) || !current.connectors.some(row => row.source === 'agiw' && row.id === id)) {
      actionStatus.textContent = actionError('NOT_MANAGED'); return false;
    }
    if (action === 'test' && current.connectors.some(row =>
      row.source === 'agiw' && row.transportTest.state === 'checking')) {
      actionStatus.textContent = actionError('TEST_BUSY'); return false;
    }
    revision++;
    busy = true;
    const focus = focusedAction();
    add.disabled = true;
    for (const button of managedList.querySelectorAll('button')) button.disabled = true;
    actionStatus.textContent = action === 'add' ? 'Saving endpoint…'
      : action === 'test' ? 'Starting one bounded MCP protocol check…' : 'Removing saved endpoint…';
    try {
      const body = action === 'add' ? {action, id, url: endpoint} : {action, id};
      const response = await fetcher('/api/tool-connectors', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body), signal: AbortSignal.timeout(6000),
      });
      const value = await response.json();
      if (!response.ok) throw new Error(typeof value?.code === 'string' ? value.code : 'ACTION_FAILED');
      toolConnectionRows(value);
      if (disposed) return true;
      if (action === 'test') awaitedTest = id;
      if (action === 'disconnect' && awaitedTest === id) awaitedTest = null;
      actionStatus.textContent = action === 'add' ? 'Endpoint saved. Press Test protocol to check it.'
        : action === 'test' ? 'Protocol check started. Its result will appear below.'
          : 'Saved endpoint removed. The server and OpenCode settings are unchanged.';
      render(value);
      if (action === 'add') { name.value = ''; url.value = ''; }
      return true;
    } catch (error) {
      if (!disposed) actionStatus.textContent = actionError(error.message);
      return false;
    } finally {
      revision++;
      busy = false;
      if (!disposed) {
        if (current) render(current);
        restoreFocus(focus);
        schedule();
      }
    }
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    void submit('add', name.value.trim(), url.value.trim());
  });
  const ready = refresh();
  return {ready, refresh, submit, destroy() { disposed = true; cancel(timer); }};
}

if (typeof document !== 'undefined') {
  for (const root of document.querySelectorAll('[data-tool-connections]')) {
    const controller = mountToolConnections(root);
    if (controller) root.toolConnections = controller;
  }
}
