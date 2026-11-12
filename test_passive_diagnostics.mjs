import {createMonitorStore} from './web/monitor-store.mjs';
import * as fleet from './web/fleet-state.mjs';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import * as layout from './web/map-layout.mjs';
import * as usage from './usage-format.mjs';
import * as onlineMode from './web/online-code-mode.mjs';
import * as modelControl from './web/model-control-view.mjs';
import * as monitorTabs from './web/monitor-tabs.mjs';
import * as theme from './jaryo/theme.mjs';

const read = file => readFileSync(new URL(file, import.meta.url), 'utf8');
const executable = source => source.replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');

// Exercise the actual page startup and open handlers in a VM. Fetch, streams,
// timers and the DOM are fixtures; this file never opens a browser or a socket.
function harness(html) {
  const elements = new Map(), requests = [], streams = [], streamObjects = [], nativeMessages = [];
  const document = {hidden: false, activeElement: null};
  function node(id = '') {
    const classes = new Set(), listeners = new Map(), attributes = new Map();
    const value = {id, dataset: {}, style: {}, children: [], hidden: false, disabled: false,
      className: '', textContent: '', clientWidth: 1000, clientHeight: 700,
      classList: {add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        contains: name => classes.has(name),
        toggle(name, on) { const next = on === undefined ? !classes.has(name) : on; next ? classes.add(name) : classes.delete(name); return next; }},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      setAttribute: (name, text) => attributes.set(name, String(text)),
      getAttribute: name => attributes.get(name) ?? null,
      addEventListener(type, callback) { listeners.set(type, [...listeners.get(type) || [], callback]); },
      removeEventListener() {},
      dispatch(type, extra = {}) { for (const callback of listeners.get(type) || []) callback({target: value, currentTarget: value, preventDefault() {}, ...extra}); },
      focus() { document.activeElement = value; },
      contains: item => item === value,
      querySelector: () => null, querySelectorAll: () => [], closest: () => null,
      showModal() { this.open = true; }, close() { this.open = false; this.dispatch('close'); },
      getBoundingClientRect: () => ({left: 0, top: 0, width: 1000, height: 700}),
    };
    Object.defineProperty(value, 'lastElementChild', {get: () => value.children.at(-1) || node()});
    return value;
  }
  for (const tag of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const value = node(tag[1]);
    value.hidden = /\shidden(?:\s|>)/.test(tag[0]);
    for (const attr of tag[0].matchAll(/data-([a-z-]+)="([^"]*)"/g)) {
      value.dataset[attr[1].replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = attr[2];
    }
    elements.set(value.id, value);
  }
  document.body = node('body'); document.documentElement = node('html');
  document.activeElement = document.body;
  document.getElementById = id => elements.get(id) || null;
  document.createElement = document.createElementNS = () => node();
  document.createTextNode = text => Object.assign(node(), {textContent: text});
  document.querySelector = selector => selector.startsWith('#') ? document.getElementById(selector.slice(1)) : null;
  document.querySelectorAll = selector => {
    const key = {'[data-view]': 'view', '[data-model-scope]': 'modelScope'}[selector];
    return key ? [...elements.values()].filter(value => key in value.dataset) : [];
  };
  document.addEventListener = () => {};
  const storage = {getItem: () => null, setItem() {}};
  const window = {location: {hash: '', search: ''}, localStorage: storage,
    matchMedia: () => ({matches: false, addEventListener() {}, removeEventListener() {}}),
    addEventListener() {}, dispatchEvent() {}, requestAnimationFrame(callback) {callback();},
    webkit: {messageHandlers: {monitor: {postMessage: message => nativeMessages.push(message)}}}};
  let store;
  const context = vm.createContext({...layout, ...fleet, ...usage, ...onlineMode, ...modelControl, ...monitorTabs,
    getMonitorStore: () => store ||= createMonitorStore({fetch: (...args) => context.fetch(...args),
      EventSource: context.EventSource, setTimeout: context.setTimeout, clearTimeout: context.clearTimeout}),
    ...theme, document, window, location: window.location, localStorage: storage,
    fetch: (url, options = {}) => { requests.push({url, method: options.method || 'GET', body: options.body}); return new Promise(() => {}); },
    EventSource: class { constructor(url) { streams.push(url);streamObjects.push(this);this.listeners={}; } addEventListener(type,callback) {this.listeners[type]=callback;} close() {} },
    AbortController, AbortSignal, performance: {now: () => 0},
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    ResizeObserver: class {observe() {}}, Date, console,
  });
  return {context, document, elements, requests, streams, streamObjects, nativeMessages,
    run: source => vm.runInContext(source, context),
    assertPassive() { assert.ok(requests.every(row => row.method === 'GET'), JSON.stringify(requests)); assert.deepEqual(nativeMessages, []); }};
}

const pages = ['index', 'monitor', 'machines', 'setup', 'components', 'eci-tuning', 'identity'];
for (const page of pages) {
  test(`opening /${page === 'index' ? '' : page} starts no POST or native action`, () => {
    const html = read(`./web/${page}.html`), h = harness(html);
    const scripts = [...html.matchAll(/<script\b[^>]*src="\/([^"]+)"[^>]*>/g)].map(match => match[1]);
    assert.ok(scripts.length > 0, 'the actual page scripts are exercised');
    for (const script of scripts) {
      let source = executable(read(`./web/${script}`));
      if (script === 'app.js') {
        // Layout/rendering needs browser geometry; retain every transport and
        // action handler, replacing only painting for this source-level test.
        const split = source.indexOf("$('pause').addEventListener");
        const paints = `
render=()=>{};renderModelAction=()=>{};renderAutoUnload=()=>{};
updateOnlineCodeMode=()=>{};updateInferenceFix=()=>{};renderActivity=()=>{};
updateFreshness=()=>{};vitalsInput=()=>({feed:[]});syncCompactUI=()=>{};bindMonitorTabs=()=>{};
renderMonitorTabs=()=>{};fitMap=()=>{};revealSelected=()=>{};
`;
        source = source.slice(0, split) + paints + source.slice(split);
      }
      h.run(`(() => {${source}\n})()`);
    }
    h.assertPassive();
    const expected = {monitor: '/api/inference', components: '/api/components', 'eci-tuning': '/api/eci-tuning'}[page];
    if (expected) assert.ok(h.requests.some(row => row.url === expected), `${expected} was read`);
    if (['machines', 'setup'].includes(page)) assert.deepEqual(h.streams, ['/api/stream']);
    if (page === 'index') assert.deepEqual(h.streams, ['/api/stream']);
  });
}

test('opening readiness, repair details, deep links, Activity, About and Browse stays passive', () => {
  const html = read('./web/index.html'), h = harness(html);
  const source = executable(read('./web/app.js'));
  const split = source.indexOf("$('pause').addEventListener");
  h.run(source.slice(0, split) + `
render=()=>{};renderModelAction=()=>{};renderAutoUnload=()=>{};
updateOnlineCodeMode=()=>{};updateInferenceFix=()=>{};renderActivity=()=>{};
updateFreshness=()=>{};vitalsInput=()=>({feed:[]});syncCompactUI=()=>{};bindMonitorTabs=()=>{};
renderMonitorTabs=()=>{};fitMap=()=>{};revealSelected=()=>{};
snapshot={host:'mac',sampledAt:Date.now()/1000};connected=true;
` + source.slice(split));
  for (const id of ['onlineCodeMode', 'fixInference', 'drawerFixButton', 'vitalActivity', 'about', 'aboutCompact', 'sidebarToggle']) {
    h.elements.get(id).dispatch('click');
  }
  for (const hash of ['#model-tuning', '#diagnostics', '#readiness']) {
    h.context.window.location.hash = hash;
    h.run('openTuningLink();');
  }
  for (const value of ['runtime', 'runs']) h.run(`setView('${value}');`);
  h.assertPassive();
  assert.ok(h.requests.some(row => row.url === '/api/online-code-mode/readiness'), 'readiness must read the cached GET endpoint');
});

test('a completed cached readiness read never chains into an action', async () => {
  for (const body of [
    {readOnly: true, status: 'observed', message: 'Cached status only.'},
    {readOnly: true, status: 'unknown', message: 'No observations.'},
    {readOnly: false, status: 'ready', message: 'Untrusted response.'},
  ]) {
    const h = harness(read('./web/index.html'));
    h.context.fetch = async (url, options = {}) => {
      h.requests.push({url, method: options.method || 'GET'});
      return {ok: true, json: async () => body};
    };
    const source = executable(read('./web/app.js')).split("$('pause').addEventListener")[0];
    h.run(source);
    await h.run('readCachedReadiness();');
    await h.run('readCachedReadiness();');
    assert.deepEqual(h.requests.map(row => row.url), [
      '/api/online-code-mode/readiness', '/api/online-code-mode/readiness',
    ]);
    h.assertPassive();
    assert.match(h.elements.get('cachedReadinessStatus').textContent, /cached/i);
    if (!body.readOnly) assert.match(h.elements.get('cachedReadinessStatus').textContent, /unknown/i);
  }
});

async function componentsHarness() {
  const h = harness(read('./web/components.html'));
  const state = {nisi: {nisi: {integrity: 'verified', version: '0.2.0', entry: '/fixture/nisi.mjs'},
    node: {found: true}, check: {state: 'not-run'}}, jev: {configured: true, state: 'ready'}};
  h.context.fetch = async (url, options = {}) => {
    h.requests.push({url, method: options.method || 'GET', body: options.body});
    return {ok: true, status: 200, json: async () => url === '/api/components' ? state.nisi : state.jev};
  };
  h.run(read('./web/components.js'));
  await new Promise(resolve => setImmediate(resolve));
  return {...h, state};
}

for (const [id, path, action] of [
  ['check', '/api/components/nisi/check', 'self-check'],
  ['jevCheck', '/api/components/jev/check', 'connection-check'],
]) {
  test(`${id} reviews consequences, cancels safely and dispatches only once after confirmation`, async () => {
    const h = await componentsHarness(), opener = h.elements.get(id);
    h.assertPassive();
    opener.focus();opener.dispatch('click');
    assert.equal(h.elements.get('actionReview').open, true);
    assert.equal(h.document.activeElement, h.elements.get('actionReviewCancel'));
    for (const field of ['Target', 'Effects', 'Duration']) assert.ok(h.elements.get(`actionReview${field}`).textContent);
    h.elements.get('actionReviewCancel').dispatch('click');
    assert.equal(h.document.activeElement, opener);
    h.assertPassive();
    opener.dispatch('click');
    h.elements.get('actionReviewConfirm').dispatch('click');
    h.elements.get('actionReviewConfirm').dispatch('click');
    await new Promise(resolve => setImmediate(resolve));
    const posts = h.requests.filter(row => row.method === 'POST');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, path);
    assert.deepEqual(JSON.parse(posts[0].body), {action});
  });

  test(`${id} revalidates availability after review and refuses a changed target`, async () => {
    const h = await componentsHarness();
    h.elements.get(id).dispatch('click');
    if (id === 'check') h.state.nisi.nisi.entry = '/fixture/replaced-nisi.mjs';
    else h.state.jev.configured = false;
    h.elements.get('actionReviewConfirm').dispatch('click');
    await new Promise(resolve => setImmediate(resolve));
    h.assertPassive();
  });
}


for (const page of pages) {
  test(`successful startup reads on /${page === 'index' ? '' : page} remain GET only with controls available`, async () => {
    const html = read(`./web/${page}.html`), h = harness(html);
    const snapshot = {schemaVersion: 1, host: 'mac', sampledAt: Date.now() / 1000,
      fullSampledAt: Date.now() / 1000, models: [{id: 'fixture-model', host: 'mac', loaded: true, state: 'idle'}],
      sources: [{id: 'lmstudio-api', state: 'live'}], modelControl: {supported: true},
      onlineCodeMode: {setupState: 'fresh'}, machines: [], activity: {runs: []}};
    const bodies = {
      '/api/snapshot': snapshot,
      '/api/inference': {observedAt: new Date().toISOString(), nodes: [], edges: [], tunnel: {state: 'not_measured'}},
      '/api/components': {nisi: {integrity: 'verified', version: '0.2.0', entry: '/fixture/nisi.mjs'}, node: {found: true}, check: {state: 'passed'}},
      '/api/components/jev': {configured: true, state: 'passed'},
      '/api/eci-tuning': {rankings: {}, pending: [], policy: {}},
      '/api/models/control': {status: 'idle', supported: true},
      '/api/models/auto-unload': {enabled: true, configError: null},
      '/api/online-code-mode/readiness': {readOnly: true, status: 'observed', message: 'Cached fixture.'},
      '/api/online-code-mode/entry': {status: 'idle', operationId: 0},
    };
    h.context.fetch = async (url, options = {}) => {
      h.requests.push({url, method: options.method || 'GET', body: options.body});
      assert.ok(Object.hasOwn(bodies, url), `unexpected startup request: ${url}`);
      return {ok: true, status: 200, json: async () => bodies[url]};
    };
    const scripts = [...html.matchAll(/<script\b[^>]*src="\/([^"]+)"[^>]*>/g)].map(match => match[1]);
    for (const script of scripts) {
      let source = executable(read(`./web/${script}`));
      if (script === 'app.js') {
        const split = source.indexOf("$('pause').addEventListener");
        source = source.slice(0, split) + `
render=()=>{};renderModelAction=()=>{};renderAutoUnload=()=>{};
updateOnlineCodeMode=()=>{};updateInferenceFix=()=>{};renderActivity=()=>{};
updateFreshness=()=>{};vitalsInput=()=>({feed:[]});syncCompactUI=()=>{};bindMonitorTabs=()=>{};
renderMonitorTabs=()=>{};fitMap=()=>{};revealSelected=()=>{};
` + source.slice(split);
      } else if (script === 'machines.mjs') {
        source = source.replace("if (typeof document !== 'undefined'", "paintMachines=()=>{};\nif (typeof document !== 'undefined'");
      } else if (script === 'setup.mjs') {
        source = source.replace("if (typeof document !== 'undefined'", "paintSetup=()=>{};\nif (typeof document !== 'undefined'");
      } else if (script === 'monitor.mjs') {
        source = source.replace("if (typeof document !== 'undefined'", "render=()=>{};\nif (typeof document !== 'undefined'");
      } else if (script === 'eci-tuning.mjs') {
        source = source.replace("if (typeof document !== 'undefined'", "render=()=>{};\nif (typeof document !== 'undefined'");
      }
      h.run(`(() => {${source}\n})()`);
    }
    for (const stream of h.streamObjects) stream.listeners.snapshot?.({data: JSON.stringify(snapshot)});
    // Flush completed reads and continuations; rendering is replaced because
    // this test checks transport/action behavior without browser geometry.
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
    h.assertPassive();
    if (page === 'components') {
      assert.equal(h.elements.get('check').disabled, false);
      assert.equal(h.elements.get('jevCheck').disabled, false);
    }
    if (page === 'setup') assert.ok(h.requests.some(row => row.url === '/api/components/jev'));
  });
}
