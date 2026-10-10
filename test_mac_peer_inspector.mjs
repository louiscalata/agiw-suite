import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
import * as layout from './web/map-layout.mjs';

// Run the actual inspector renderer, without its polling loop. Only the DOM
// operations used by these passive peer panels are needed.
const definitions = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8')
  .replace(/^import .*;\r?\n/gm, '').split("$('pause').addEventListener")[0];

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.className = ''; this.ownText = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.ownText = ''; }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
}

function inspector(macPeer, {kind = 'mac-peer', peers = [], snapshotAge = 0} = {}) {
  const now = 1700000000;
  class Clock extends Date { static now() { return now * 1000; } }
  const document = {createElement: tag => new Element(tag)};
  const api = runInNewContext(`${definitions}
    ;({draw(data, node, body) { snapshot=data; connected=true; return inspectorBody(node, body); }})`,
    {...layout, document, Date: Clock});
  const body = new Element('main');
  api.draw({schemaVersion: 1, host: 'windows', sampledAt: now - snapshotAge, fullSampledAt: now,
    models: [], macPeer, peers}, {id: kind === 'mac-peer' ? 'mac-peer' : 'mac-model:a',
    kind, macModel: {id: 'a', state: 'loaded'}}, body);
  return body;
}

const linked = {state: 'linked', via: 'sharedchami', ageSeconds: 2, linkAgeSeconds: 2,
  loadedCount: 1, models: [{id: 'a', state: 'loaded'}]};

test('the Windows linked-peer inspector identifies a share report without a LAN answer or closed-server claim', () => {
  const body = inspector(linked);
  assert.match(body.textContent, /Reported via SharedChami/);
  assert.match(body.textContent, /unsigned file, not verified/);
  assert.doesNotMatch(body.textContent, /LM Studio answered|LM Studio not open|closed to the LAN|Mac answering on the LAN/i);
  const summary = body.children.find(child => child.className === 'panel-summary');
  assert.equal(summary.dataset.tone, 'muted', 'an unsigned report uses the report tone');
});

test('the linked model inspector attributes reported residency to the share source', () => {
  const body = inspector(linked, {kind: 'mac-model'});
  assert.match(body.textContent, /share|SharedChami/i);
  assert.doesNotMatch(body.textContent, /listed as loaded by the Mac's LM Studio|LM Studio answered|LM Studio not open/i);
  assert.match(body.textContent, /not proof|does not (?:prove|verify|establish)/i);
});

test('the Windows LAN-peer inspector still explains a proven model-listing answer', () => {
  const body = inspector({...linked, state: 'reachable', via: 'mdns', latencyMs: 3});
  assert.match(body.textContent, /Mac answering on the LAN/);
  assert.match(body.textContent, /LM Studio answered/);
  assert.doesNotMatch(body.textContent, /Reported via SharedChami/);
});

test('a selected Mac model loses its loaded claim after either evidence source expires', () => {
  for (const [state, ageSeconds] of [['linked', 29], ['reachable', 44]]) {
    const body = inspector({...linked, state, ageSeconds, linkAgeSeconds: ageSeconds}, {
      kind: 'mac-model', snapshotAge: 2,
    });
    assert.match(body.textContent, /Mac reachability unknown/);
    assert.match(body.textContent, /residency.*unknown|unknown.*residency/i);
    assert.doesNotMatch(body.textContent, /was (?:listed|reported) as loaded|LM Studio answered|Mac answering on the LAN/i);
  }
});

test('the peer inspector ages share rows and expires the report while the local feed is fresh', () => {
  const body = inspector({...linked, ageSeconds: 29, linkAgeSeconds: 29}, {
    snapshotAge: 2,
    peers: [{host: 'mac-peer', fresh: true, ageSeconds: 29, platform: 'macos',
      models: [{id: 'a', loadedState: 'unknown'}]}],
  });
  assert.match(body.textContent, /Mac reachability unknown/);
  const pairs = body.descendants().filter(node => node.tagName === 'dl');
  const instanceRows = pairs.find(node => node.textContent.includes('mac-peer'));
  assert.match(instanceRows.textContent, /Stale.*loaded state unknown.*31 s ago/i);
  assert.doesNotMatch(instanceRows.textContent, /0 loaded|Reported \(unverified\)/);
});
