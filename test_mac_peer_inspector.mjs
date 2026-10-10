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
  set innerHTML(_) { throw new Error('the inspector must render report details as literal text'); }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
}

function inspector(macPeer, {kind = 'mac-peer', peers = [], sources, snapshotAge = 0,
  feedAge = 0, connected = true, paused = false} = {}) {
  const now = 1700000000;
  class Clock extends Date { static now() { return now * 1000; } }
  const document = {createElement: tag => new Element(tag)};
  const api = runInNewContext(`${definitions}
    ;({draw(data, node, body, isConnected, isPaused) { snapshot=data; connected=isConnected; paused=isPaused; return inspectorBody(node, body); }})`,
    {...layout, document, Date: Clock});
  const body = new Element('main');
  api.draw({schemaVersion: 1, host: 'windows', sampledAt: now - snapshotAge, fullSampledAt: now - feedAge,
    models: [], macPeer, peers, sources}, {id: kind === 'mac-peer' ? 'mac-peer' : 'mac-model:a',
    kind, macModel: {id: 'a', state: 'loaded'}}, body, connected, paused);
  return body;
}

function publishingValue(body) {
  const panel = body.children.find(node => node.className === 'panel-section'
    && node.children.some(child => child.textContent === 'AGIW instances on SharedChami'));
  assert.ok(panel, 'the SharedChami section is rendered');
  const pairs = panel.children.find(node => node.tagName === 'dl');
  assert.ok(pairs, 'publishing is shown with the instance rows');
  const keys = pairs.children.filter(node => node.tagName === 'dt' && node.textContent === 'This PC');
  assert.equal(keys.length, 1, 'exactly one row identifies this PC');
  const value = pairs.children[pairs.children.indexOf(keys[0]) + 1];
  assert.equal(value.tagName, 'dd');
  return value;
}

const publishSource = (outcome, over = {}) => ({id: 'share-publish', outcome,
  ageSeconds: outcome === 'ok' ? 4 : null, ...over});
const publicationUnknown = value => {
  assert.match(value, /unknown|off/i);
  assert.doesNotMatch(value, /published\s+\d|publishing failed|first publish pending/i);
};

const linked = {state: 'linked', via: 'sharedchami', ageSeconds: 2, linkAgeSeconds: 2,
  loadedCount: 1, models: [{id: 'a', state: 'loaded'}]};
const failureCauseClaim = /offline|asleep|off (?:the )?network|\bMac (?:is )?on (?:the )?(?:network|LAN)\b|LM Studio (?:is )?(?:closed|not[- ](?:open|listening|serving))|firewall (?:is )?(?:blocking|dropping|rejecting)/i;

function inspectorPair(body, label) {
  for (const pairs of body.descendants().filter(node => node.tagName === 'dl')) {
    const key = pairs.children.find(node => node.tagName === 'dt' && node.textContent === label);
    if (key) return pairs.children[pairs.children.indexOf(key) + 1].textContent;
  }
  assert.fail(`missing inspector detail ${label}`);
}

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

test('both Windows Mac inspectors distinguish connection refusal from timeout without guessing a cause', () => {
  for (const kind of ['mac-peer', 'mac-model']) {
    for (const [reason, diagnosis] of [['refused', /refused/i], ['timeout', /no answer|timed out|timeout/i]]) {
      const body = inspector({...linked, state: 'unreachable', reason}, {kind});
      const summary = body.children.find(node => node.className === 'panel-summary');
      assert.equal(summary.dataset.tone, 'warn');
      assert.match(summary.descendants().find(node => node.tagName === 'strong').textContent, diagnosis);
      assert.match(summary.textContent, diagnosis);
      assert.match(inspectorPair(body, 'Reachability'), diagnosis);
      assert.doesNotMatch(body.textContent, failureCauseClaim);
      assert.equal(inspectorPair(body, 'Loaded models'), 'Unknown');
      assert.doesNotMatch(body.textContent, /was (?:listed|reported) as loaded|LM Studio answered|Mac (?:is )?generating/i);
      if (kind === 'mac-model') assert.match(summary.textContent, /residency.*unknown|unknown.*residency/i);
    }
  }
});

test('Windows Mac inspectors reject malformed reasons and keep generic failures free of cause claims', () => {
  for (const reason of [null, ['refused'], {reason: 'timeout'}, true, 'REFUSED', 'TIMEOUT', 'bogus',
    'invalid', 'unresolved', 'error']) {
    const body = inspector({...linked, state: 'unreachable', reason});
    const summary = body.children.find(node => node.className === 'panel-summary');
    assert.equal(summary.dataset.tone, 'warn');
    assert.doesNotMatch(body.textContent, /refused|timed out|timeout|bogus/i);
    assert.doesNotMatch(body.textContent, failureCauseClaim);
    assert.equal(inspectorPair(body, 'Loaded models'), 'Unknown');
  }
});

test('both Windows Mac inspectors hide failure diagnoses after the probe or feed expires', () => {
  for (const kind of ['mac-peer', 'mac-model']) {
    for (const reason of ['refused', 'timeout']) {
      for (const options of [{snapshotAge: 2}, {feedAge: 4}, {connected: false}]) {
        const body = inspector({...linked, state: 'unreachable', ageSeconds: 44, reason,
          detail: reason === 'refused' ? 'Connection refused on <mac-lan-ip> port 1234' : 'Probe timed out'},
        {kind, ...options});
        const summary = body.children.find(node => node.className === 'panel-summary');
        assert.equal(summary.dataset.tone, 'muted');
        assert.match(summary.textContent, /unknown|stale/i);
        assert.doesNotMatch(body.textContent, /refused|timed out|timeout/i);
        assert.equal(inspectorPair(body, 'Loaded models'), 'Unknown');
      }
    }
  }
});

test('both Windows Mac inspectors ignore an old failure reason on a fresh share report or LAN answer', () => {
  for (const kind of ['mac-peer', 'mac-model']) {
    for (const state of ['linked', 'reachable']) {
      for (const reason of ['refused', 'timeout']) {
        const body = inspector({...linked, state, reason}, {kind});
        const summary = body.children.find(node => node.className === 'panel-summary');
        assert.equal(summary.dataset.tone, state === 'linked' ? 'muted' : 'ok');
        assert.match(summary.textContent, state === 'linked' ? /reported.*SharedChami/i : /LM Studio/i);
        assert.doesNotMatch(body.textContent, /refused|timed out|timeout/i);
        assert.equal(inspectorPair(body, 'Loaded models'), 'a');
      }
    }
  }
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

test('both Windows Mac inspectors show this PC publishing alongside peer reports with elapsed snapshot age', () => {
  for (const kind of ['mac-peer', 'mac-model']) {
    const body = inspector(linked, {kind, snapshotAge: 2, sources: [publishSource('ok')],
      peers: [{host: 'mac-instance', fresh: true, ageSeconds: 1, platform: 'macos',
        models: [{id: 'a', loadedState: 'loaded'}]}]});
    assert.match(publishingValue(body).textContent, /published\s+6\s+s\s+ago/i);
    const peersPanel = body.children.find(node => node.className === 'panel-section');
    assert.match(peersPanel.textContent, /mac-instance.*reported.*3 s ago/i);
  }
});

test('both Windows Mac inspectors show publishing failure, pending, and recovery from current source outcomes', () => {
  for (const kind of ['mac-peer', 'mac-model']) {
    const failed = publishingValue(inspector(linked, {kind,
      sources: [publishSource('failed', {detail: 'status: PermissionError 32; any previous file is kept'})]}));
    assert.match(failed.textContent, /publishing failed/i);
    assert.match(failed.textContent, /previous file(?: is)? kept/i);
    assert.doesNotMatch(failed.textContent, /published\s+\d/i);
    const pending = publishingValue(inspector(linked, {kind, sources: [publishSource('pending')]}));
    assert.match(pending.textContent, /first publish pending/i);
    assert.doesNotMatch(pending.textContent, /publishing failed|published\s+\d/i);
    const recovered = publishingValue(inspector(linked, {kind, sources: [publishSource('ok', {ageSeconds: 0})]}));
    assert.match(recovered.textContent, /published\s+0\s+s\s+ago/i);
    assert.doesNotMatch(recovered.textContent, /failed|pending/i);
  }
});

test('both Windows Mac inspectors show one concise publishing failure without losing its evidence', () => {
  const detail = 'Publishing to SharedChami failed (status: PermissionError 32); last success 11 s ago; any previous file is kept';
  for (const kind of ['mac-peer', 'mac-model']) {
    const value = publishingValue(inspector(linked, {kind, sources: [publishSource('failed', {detail})]}));
    assert.equal(value.textContent,
      'Publishing failed (status: PermissionError 32); last success 11 s ago; previous file kept');
    assert.equal((value.textContent.match(/\bpublishing\b/gi) || []).length, 1);
    assert.match(value.textContent, /PermissionError 32/);
    assert.match(value.textContent, /last success 11 s ago/);
    assert.match(value.textContent, /previous file kept/);
    assert.equal(value.children.length, 0);
  }
});

test('stale, paused, or disconnected Windows inspector feeds never preserve a publishing success or failure claim', () => {
  for (const kind of ['mac-peer', 'mac-model']) {
    for (const outcome of ['ok', 'failed', 'pending']) {
      for (const options of [{feedAge: 3.01}, {connected: false}, {paused: true}, {snapshotAge: 3.01},
        {feedAge: -2, snapshotAge: -2}]) {
        const value = publishingValue(inspector(linked, {kind, ...options,
          sources: [publishSource(outcome, {detail: 'status refused'})]}));
        publicationUnknown(value.textContent);
      }
    }
  }
});

test('the actual Windows publication helper accepts zero age and adds elapsed time only to a unique source', () => {
  assert.equal(typeof layout.windowsSharePublishRow, 'function');
  for (const [ageSeconds, snapshotAge, expected] of [[0, 0, 0], [4, 2, 6], [1, 3, 4]]) {
    const row = layout.windowsSharePublishRow([publishSource('ok', {ageSeconds}),
      {id: 'lane-inventory', outcome: 'ok', ageSeconds: 0}], {feedFresh: true, snapshotAge});
    assert.equal(row[0], 'This PC');
    assert.match(row[1], new RegExp(`published\\s+${expected}\\s+s\\s+ago`, 'i'));
  }
});

test('malformed, missing, or duplicate publication evidence cannot assert success', () => {
  assert.equal(typeof layout.windowsSharePublishRow, 'function');
  const ok = publishSource('ok');
  const invalid = [undefined, null, [], 'share-publish', ok, [null], ['share-publish'], [[ok]],
    [{outcome: 'ok', ageSeconds: 0}], [publishSource('ok', {id: ['share-publish']})],
    [publishSource('ok', {id: true})], [publishSource('ok', {id: 'SHARE-PUBLISH'})],
    [publishSource('OK')], [publishSource(['ok'])], [publishSource({value: 'ok'})],
    [publishSource(true)], [publishSource(null)], [ok, ok], [ok, publishSource('failed')],
    [ok, publishSource(['failed'])]];
  for (const ageSeconds of [undefined, null, true, false, -1, NaN, Infinity, -Infinity, '0', [], {}]) {
    invalid.push([publishSource('ok', {ageSeconds})]);
  }
  for (const sources of invalid) {
    const row = layout.windowsSharePublishRow(sources, {feedFresh: true, snapshotAge: 0});
    assert.equal(row[0], 'This PC');
    publicationUnknown(row[1]);
  }
});

test('the publication helper rejects expired or malformed local freshness before reading an outcome', () => {
  assert.equal(typeof layout.windowsSharePublishRow, 'function');
  const options = [{}, {feedFresh: false}, {feedFresh: 'true'}, {feedFresh: 1}];
  for (const snapshotAge of [null, true, false, -1, 3.01, NaN, Infinity, -Infinity, '0', [], {}]) {
    options.push({feedFresh: true, snapshotAge});
  }
  for (const freshness of options) {
    const row = layout.windowsSharePublishRow([publishSource('ok')], freshness);
    assert.equal(row[0], 'This PC');
    publicationUnknown(row[1]);
  }
});

test('the actual inspector renders malformed publication rows as unknown without losing Mac evidence', () => {
  const ok = publishSource('ok');
  for (const sources of [undefined, ok, [[ok]], [ok, publishSource('pending')],
    [publishSource('ok', {ageSeconds: '0'})], [publishSource(['ok'])]]) {
    const body = inspector(linked, {sources});
    publicationUnknown(publishingValue(body).textContent);
    assert.match(body.textContent, /Mac reported through SharedChami/i);
  }
});

test('failure detail is bounded literal DOM text and malformed detail cannot inject nodes', () => {
  const detail = '<img src=x onerror=alert(1)> & <b>share refused</b>';
  const body = inspector(linked, {sources: [publishSource('failed', {detail})]});
  const value = publishingValue(body);
  assert.match(value.textContent, /publishing failed/i);
  assert.ok(value.textContent.includes(detail));
  assert.equal(value.children.length, 0, 'the failure value is one text node');
  assert.equal(body.descendants().filter(node => ['img', 'b', 'script'].includes(node.tagName)).length, 0);
  const longValue = publishingValue(inspector(linked, {sources: [publishSource('failed', {
    detail: 'x'.repeat(2000) + 'UNBOUNDED-END-MARKER'})]}));
  assert.match(longValue.textContent, /publishing failed/i);
  assert.ok(longValue.textContent.length <= 512);
  assert.doesNotMatch(longValue.textContent, /UNBOUNDED-END-MARKER/);
  for (const malformedDetail of [null, ['<img>'], {html: '<img>'}, true, 1]) {
    const malformed = publishingValue(inspector(linked, {
      sources: [publishSource('failed', {detail: malformedDetail})]}));
    assert.match(malformed.textContent, /publishing failed/i);
    assert.doesNotMatch(malformed.textContent, /<img>|object Object/);
  }
});
