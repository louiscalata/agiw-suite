import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import * as layout from './web/map-layout.mjs';

const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8')
  .replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];
const css = readFileSync(new URL('./web/style.css', import.meta.url), 'utf8');
const now = 1700000000;
const peer = (over = {}) => ({state: 'linked', via: 'sharedchami', ageSeconds: 2,
  linkAgeSeconds: 2, loadedCount: 2,
  models: [{id: 'qwen/qwen3.8-27b', state: 'loaded'},
    {id: 'google/gemma-4-26b-a4b-qat', state: 'loaded'}], ...over});

// Inspect the actual stylesheet's default caption cascade, including selector
// specificity. Media/interaction overrides remain covered by the UI suite and
// live layout checks; no browser or polling loop is started here.
function cssRules(source) {
  const out = [], cleaned = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const walk = (text, media = '') => {
    let offset = 0;
    while (offset < text.length) {
      const open = text.indexOf('{', offset); if (open < 0) break;
      const head = text.slice(offset, open).trim();
      let close = open + 1, depth = 1;
      while (depth && close < text.length) {
        if (text[close] === '{') depth++; else if (text[close] === '}') depth--;
        close++;
      }
      const body = text.slice(open + 1, close - 1);
      if (head.startsWith('@media')) walk(body, head.slice(6).trim());
      else if (!head.startsWith('@')) out.push({media, selectors: head.split(',').map(s => s.trim()), body});
      offset = close;
    }
  };
  walk(cleaned); return out;
}
const rules = cssRules(css);
function captionStyle(nodeClasses, target) {
  const names = new Set(['node', ...nodeClasses]), result = new Map();
  for (const rule of rules) {
    if (rule.media) continue;
    for (const selector of rule.selectors) {
      const match = /^(\.[\w.-]+)\s+(\.(?:label|sub)|text)$/.exec(selector);
      if (!match || (match[2] !== 'text' && match[2] !== `.${target}`)) continue;
      const required = match[1].split('.').filter(Boolean);
      if (!required.every(name => names.has(name))) continue;
      const specificity = required.length * 100 + (match[2] === 'text' ? 1 : 100);
      for (const declaration of rule.body.split(';')) {
        const colon = declaration.indexOf(':'); if (colon < 0) continue;
        const property = declaration.slice(0, colon).trim();
        if (!['opacity', 'pointer-events'].includes(property)) continue;
        const value = declaration.slice(colon + 1).trim();
        if (!result.has(property) || specificity >= result.get(property).specificity)
          result.set(property, {specificity, value});
      }
    }
  }
  return Object.fromEntries([...result].map(([property, entry]) => [property, entry.value]));
}

function graph(raw = peer(), {host = 'windows', width = 820, height = 604,
  snapshotAge = 0, feedAge = 0, connected = true} = {}) {
  class Clock extends Date { static now() { return now * 1000; } }
  const feed = {host, sampledAt: now - snapshotAge, fullSampledAt: now - feedAge,
    models: [], clients: [], sources: [], pipeline: {status: 'idle'}, macPeer: raw};
  const api = runInNewContext(`${app}
    snapshot=input; connected=isConnected; runtimeGraph();
    ({nodes:graph.nodes, peer:graph.nodes.find(n=>n.kind==='mac-peer')?.macPeer})`,
  {...layout, input: feed, isConnected: connected, Date: Clock,
    document: {getElementById: id => id === 'graphRegion' ? {clientWidth: width, clientHeight: height} : null}});
  return JSON.parse(JSON.stringify(api));
}

test('Mac model names are visible and clickable at rest while their source subtitles stay hidden', () => {
  const label = captionStyle(['mac-model'], 'label'), subtitle = captionStyle(['mac-model'], 'sub');
  assert.ok(Number(label.opacity) > 0, 'the real caption cascade shows the Mac model name');
  assert.equal(label['pointer-events'], 'visiblePainted', 'clicking a visible name belongs to its node');
  assert.equal(Number(subtitle.opacity), 0, 'source subtitles still require interaction');
  assert.equal(subtitle['pointer-events'], 'none', 'hidden text does not intercept map clicks');
});

test('the always-visible rule is Mac-model scoped and preserves other default caption states', () => {
  const visible = rules.filter(rule => !rule.media && rule.selectors.includes('.node.mac-model .label')
    && rule.body.split(';').some(declaration => /^\s*opacity\s*:\s*(?:0?\.\d+|1)\s*$/.test(declaration)));
  assert.ok(visible.length, 'an explicit Mac-model caption rule exists');
  for (const classes of [['model'], ['model', 'unknown'], ['client-model'], ['mac-peer'], ['pipeline']])
    assert.equal(Number(captionStyle(classes, 'label').opacity), 0, `${classes.join(' ')} stays hidden at rest`);
  assert.equal(Number(captionStyle(['model', 'loaded'], 'label').opacity), .88);
  assert.equal(Number(captionStyle(['windows-lane'], 'label').opacity), .9);
});

test('the actual Windows graph gives share and LAN satellites distinct source captions with valid names', () => {
  for (const [state, subtitle] of [['linked', 'REPORTED ON MAC'], ['reachable', 'LISTED ON MAC']]) {
    for (const [width, height] of [[390, 844], [640, 504], [820, 604], [1400, 900]]) {
      const result = graph(peer({state}), {width, height});
      const satellites = result.nodes.filter(node => node.kind === 'mac-model');
      assert.deepEqual(satellites.map(node => node.id), ['mac-model:0', 'mac-model:1']);
      assert.deepEqual(satellites.map(node => node.label), ['Qwen3.8 27b', 'Gemma 4']);
      for (const satellite of satellites) {
        assert.equal(satellite.subtitle, subtitle);
        assert.equal(satellite.macPeer.satelliteSubtitle, subtitle);
        assert.ok(!satellite.active && !satellite.inFlight, 'residency is not model activity');
        assert.doesNotMatch(satellite.subtitle, /LOADED ON MAC|generating|busy|active|verified/i);
      }
    }
  }
});

test('map and sidebar source phrases are derived together from the same validated peer view', () => {
  for (const [state, brief, detail] of [['linked', 'REPORTED ON MAC', 'Reported loaded on the Mac'],
    ['reachable', 'LISTED ON MAC', 'Listed loaded by LM Studio']]) {
    const view = layout.macPeerView(peer({state}), {feedFresh: true});
    assert.equal(view.satelliteSubtitle, brief);
    assert.equal(view.satelliteDetail, detail);
    const rows = layout.windowsMacListEntries(view).filter(row => row.key.startsWith('mac-model:'));
    assert.equal(rows.length, 2);
    assert.ok(rows.every(row => row.subtitle === view.satelliteDetail));
  }
  for (const raw of [null, peer({state: 'unreachable', reason: 'refused'}), peer({ageSeconds: 31})]) {
    const view = layout.macPeerView(raw, {feedFresh: true});
    assert.equal(view.satelliteSubtitle, 'RESIDENCY UNKNOWN');
    assert.deepEqual(layout.windowsMacListEntries(view).filter(row => row.key.startsWith('mac-model:')), []);
  }
});

test('expired or malformed residency produces no Mac satellite caption or loaded model dot', () => {
  for (const [raw, options] of [[peer({ageSeconds: 29}), {snapshotAge: 2}],
    [peer({state: 'reachable', ageSeconds: 44}), {snapshotAge: 2}], [peer(), {feedAge: 4}],
    [peer(), {connected: false}], [null, {}],
    [peer({models: [{id: 'x', state: 'unknown'}]}), {}],
    [peer({models: [{id: 'x', state: 'listed'}]}), {}],
    [peer({models: [{id: 'bad\nid', state: 'loaded'}]}), {}]]) {
    assert.deepEqual(graph(raw, options).nodes.filter(node => node.kind === 'mac-model'), []);
  }
});

test('Mac edition creates no Windows Mac-peer or Mac-model nodes for either evidence source', () => {
  for (const state of ['linked', 'reachable'])
    assert.deepEqual(graph(peer({state}), {host: 'mac'}).nodes.filter(node =>
      node.kind === 'mac-peer' || node.kind === 'mac-model'), []);
});

test('actual cluster collision input reserves each visible Mac model name but not its hidden subtitle', () => {
  class Clock extends Date { static now() { return now * 1000; } }
  let captured;
  const groups = {children: []}, reads = [0, 0];
  const result = runInNewContext(`${app}
    snapshot=input; connected=true; runtimeGraph(); camera.z=1;
    const satellites=graph.nodes.filter(node=>node.kind==='mac-model');
    for(const node of satellites) nodeElements.set(node.id, {children:[null,null,null,null,
      {getBBox(){reads[0]++; return {x:-40,y:4,width:80,height:14};}},
      {getBBox(){reads[1]++; return {x:-999,y:30,width:1998,height:100};}}]});
    groups.children=graph.labels.map(()=>({getBBox(){return {width:120,height:11};},setAttribute(){}}));
    placeClusterLabels(); satellites.map(node=>({id:node.id,x:node.x,y:node.y}))`,
  {...layout, Date: Clock, groups, reads,
    input: {host: 'windows', sampledAt: now, models: [], clients: [], pipeline: {status: 'idle'}, macPeer: peer()},
    resolveLabelCollisions: (labels, obstacles) => { captured = JSON.parse(JSON.stringify(obstacles)); return labels; },
    document: {getElementById: id => id === 'groups' ? groups : id === 'graphRegion'
      ? {clientWidth: 820, clientHeight: 604} : null}});
  assert.equal(reads[0], 2, 'both actual Mac name elements are measured');
  assert.equal(reads[1], 0, 'hidden Mac source text does not reserve invisible space');
  for (const node of result) {
    const expected = {left: node.x - 40, right: node.x + 40, top: node.y + 4, bottom: node.y + 18};
    assert.ok(captured.some(box => Object.keys(expected).every(key => box[key] === expected[key])),
      `${node.id} name is a collision obstacle`);
  }
});
