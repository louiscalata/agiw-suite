import test from 'node:test';
import assert from 'node:assert/strict';
import {macPeerView,agiwPeersRows,pcAdaptersView} from './web/map-layout.mjs';

const peer = (over = {}) => ({state: 'reachable', ageSeconds: 4, latencyMs: 3.6, address: '10.0.0.176', via: 'mdns',
  loadedCount: 1, expectedVerifyModel: 'openai/gpt-oss-20b', expectedVerifyLoaded: true,
  models: [{id: 'openai/gpt-oss-20b', state: 'loaded'}, {id: 'qwen/qwen3.8-27b', state: 'not-loaded'}], ...over});

test('reachable peer lists loaded models as inventory only', () => {
  const v = macPeerView(peer(), {feedFresh: true, snapshotAge: 1});
  assert.equal(v.state, 'reachable');
  assert.equal(v.subtitle, 'LAN 4 MS · 1 LOADED');
  assert.deepEqual(v.satellites.map(m => m.id), ['openai/gpt-oss-20b']);
  assert.equal(v.chip.tone, 'ok');
  assert.match(v.rows.find(([k]) => k === 'Expected verify model')[1], /loaded$/);
});

test('a stale feed or an old probe never reads reachable', () => {
  assert.equal(macPeerView(peer(), {feedFresh: false}).state, 'unknown');
  assert.equal(macPeerView(peer({ageSeconds: 40}), {feedFresh: true, snapshotAge: 6}).state, 'unknown');
  assert.equal(macPeerView(peer({ageSeconds: -1}), {feedFresh: true}).state, 'unknown');
});

test('unreachable and malformed inputs', () => {
  const down = macPeerView({state: 'unreachable', ageSeconds: 2, models: [{id: 'x', state: 'loaded'}]}, {feedFresh: true});
  assert.equal(down.subtitle, 'NOT ANSWERING ON THE LAN');
  assert.deepEqual(down.satellites, []);
  assert.equal(down.chip.tone, 'warn');
  for (const bad of [null, [], 'x', {state: 'bogus', ageSeconds: 1}]) assert.equal(macPeerView(bad, {feedFresh: true}).state, 'unknown');
});

const failureCauseClaim = /offline|asleep|off (?:the )?network|\bMac (?:is )?on (?:the )?(?:network|LAN)\b|LM Studio (?:is )?(?:closed|not[- ](?:open|listening|serving))|firewall (?:is )?(?:blocking|dropping|rejecting)/i;
const failureSurfaces = view => [view.subtitle, view.brief, view.chip.detail, view.chip.short,
  view.rows.find(([key]) => key === 'Reachability')[1]];

test('fresh refused and timeout probes have distinct neutral diagnoses on every Mac peer surface', () => {
  for (const [reason, diagnosis] of [['refused', /refused/i], ['timeout', /no answer|timed out|timeout/i]]) {
    const view = macPeerView(peer({state: 'unreachable', reason}), {feedFresh: true});
    assert.equal(view.reason, reason);
    assert.match(view.failureTitle, diagnosis);
    assert.match(view.failureDetail, diagnosis);
    for (const text of failureSurfaces(view)) {
      assert.match(text, diagnosis);
      assert.doesNotMatch(text, failureCauseClaim);
    }
    assert.doesNotMatch(view.failureTitle + ' ' + view.failureDetail, failureCauseClaim);
    assert.equal(view.reachable, false);
    assert.equal(view.loadedKnown, false);
    assert.equal(view.loadedCount, null);
    assert.deepEqual(view.models, []);
    assert.deepEqual(view.loaded, []);
    assert.deepEqual(view.satellites, []);
    assert.equal(view.rows.find(([key]) => key === 'Loaded models')[1], 'Unknown');
    assert.doesNotMatch(view.chip.detail, /\d+ loaded|generating|running/i);
  }
});

test('other current canonical probe reasons describe only the failed observation', () => {
  for (const [reason, description] of [['invalid', /list|invalid/i],
    ['unresolved', /unresolved|resolv|name|address/i], ['error', /probe.*error|error.*probe|probe failed/i]]) {
    const view = macPeerView(peer({state: 'unreachable', reason}), {feedFresh: true});
    assert.equal(view.reason, reason);
    assert.match(view.failureTitle + ' ' + view.failureDetail, description);
    assert.doesNotMatch(view.failureTitle + ' ' + view.failureDetail, failureCauseClaim);
    assert.equal(view.reachable, false);
    assert.equal(view.loadedKnown, false);
  }
});

test('malformed or noncanonical reason values do not impersonate a refused connection or timeout', () => {
  for (const reason of [undefined, null, true, false, 1, [], ['refused'], {reason: 'timeout'},
    new String('refused'), 'REFUSED', 'Refused', 'TIMEOUT', ' refused', 'timeout ', 'bogus']) {
    const view = macPeerView(peer({state: 'unreachable', reason}), {feedFresh: true});
    assert.equal(view.reason, null);
    assert.equal(view.state, 'unreachable');
    const text = [view.failureTitle, view.failureDetail, ...failureSurfaces(view)].join(' ');
    assert.doesNotMatch(text, /refused|timed out|timeout|bogus/i);
    assert.doesNotMatch(text, failureCauseClaim);
  }
});

test('expired probe or local feed removes its reason and hides the old failure diagnosis', () => {
  for (const reason of ['refused', 'timeout']) {
    for (const [ageSeconds, options] of [[46, {feedFresh: true}],
      [44, {feedFresh: true, snapshotAge: 2}], [0, {feedFresh: false}]]) {
      const view = macPeerView(peer({state: 'unreachable', ageSeconds, reason,
        detail: reason === 'refused' ? 'Connection refused on 10.0.0.176 port 1234' : 'Probe timed out'}), options);
      assert.equal(view.state, 'unknown');
      assert.equal(view.reason, null);
      assert.equal(view.failureTitle, null);
      assert.equal(view.failureDetail, null);
      assert.doesNotMatch([...failureSurfaces(view), ...view.rows.map(([, value]) => value)].join(' '),
        /refused|timed out|timeout/i);
      assert.equal(view.loadedKnown, false);
    }
  }
});

test('linked and reachable observations ignore a retained failed-probe reason', () => {
  for (const state of ['linked', 'reachable']) {
    for (const reason of ['refused', 'timeout', ['refused']]) {
      const view = macPeerView(peer({state, reason}), {feedFresh: true});
      assert.equal(view.state, state);
      assert.equal(view.reason, null);
      assert.equal(view.failureTitle, null);
      assert.equal(view.failureDetail, null);
      assert.equal(view.loadedKnown, true);
      assert.doesNotMatch(failureSurfaces(view).join(' '), /refused|timed out|timeout/i);
    }
  }
});

test('v1 listing without loaded state says so', () => {
  const v = macPeerView(peer({loadedCount: null, models: [{id: 'm', state: 'listed'}]}), {feedFresh: true});
  assert.equal(v.loadedKnown, false);
  assert.match(v.subtitle, /LOADED STATE UNKNOWN/);
  assert.deepEqual(v.satellites, []);
});

test('model ids with control characters or spaces are dropped', () => {
  const v = macPeerView(peer({models: [{id: 'bad\nid', state: 'loaded'}, {id: 'a b', state: 'loaded'}, {id: 'ok', state: 'loaded'}]}), {feedFresh: true});
  assert.deepEqual(v.loaded.map(m => m.id), ['ok']);
});

test('linked through SharedChami reads as connected without claiming the LAN', () => {
  const v = macPeerView({state: 'linked', ageSeconds: 1, via: 'sharedchami', linkAgeSeconds: 8, loadedCount: 1,
    models: [{id: 'qwen/qwen3.8-27b', state: 'loaded'}]}, {feedFresh: true});
  assert.equal(v.reachable, true);
  assert.equal(v.linked, true);
  assert.equal(v.subtitle, 'REPORTED VIA SHARE · 1 LOADED');
  assert.match(v.rows[0][1], /Reported via SharedChami.*unsigned file, not verified/);
  assert.doesNotMatch(v.rows[0][1], /LM Studio not open|LM Studio.*answer|closed to the LAN/i);
  assert.equal(v.chip.short, 'Reported · 1 loaded');
  assert.equal(v.chip.tone, 'muted');
});

test('share reports expire at 30 seconds while LAN probes remain current for 45', () => {
  const linked = peer({state: 'linked', via: 'sharedchami', ageSeconds: 25, linkAgeSeconds: 25});
  const atLimit = macPeerView(linked, {feedFresh: true, snapshotAge: 5});
  assert.equal(atLimit.state, 'linked');
  assert.equal(atLimit.age, 30);
  assert.match(atLimit.rows[0][1], /30 s old/);
  const expired = macPeerView(linked, {feedFresh: true, snapshotAge: 5.01});
  assert.equal(expired.state, 'unknown');
  assert.equal(expired.reachable, false);
  assert.equal(expired.loadedKnown, false);
  assert.deepEqual(expired.satellites, []);
  assert.equal(macPeerView(peer({ageSeconds: 40}), {feedFresh: true, snapshotAge: 5}).state, 'reachable');
  assert.equal(macPeerView(peer({ageSeconds: 40}), {feedFresh: true, snapshotAge: 5.01}).state, 'unknown');
});

test('an unknown or incomplete model listing never becomes a known loaded total', () => {
  const incomplete = [
    [{id: 'a', state: 'unknown'}],
    [{id: 'a', state: 'listed'}],
    [{id: 'a', state: 'loaded'}, {id: 'b', state: 'unknown'}],
    [{id: 'a', state: 'not-loaded'}, {id: 'b', state: 'listed'}],
    [{id: 'a', state: 'loaded'}, {id: 'b', state: 'busy'}],
    [{id: 'a', state: 'loaded'}, null],
  ];
  for (const state of ['linked', 'reachable']) {
    for (const models of incomplete) {
      for (const loadedCount of [0, 1]) {
        const v = macPeerView(peer({state, models, loadedCount}), {feedFresh: true});
        assert.equal(v.loadedKnown, false, `${state}: ${JSON.stringify(models)} must not confirm ${loadedCount}`);
        assert.equal(v.loadedCount, null);
        assert.match(v.subtitle, /LOADED STATE UNKNOWN/);
        assert.doesNotMatch(v.chip.short, /\d+ loaded/);
      }
    }
  }
});

test('an empty LAN listing proves zero loaded while an empty share report leaves residency unknown', () => {
  const lan = macPeerView(peer({models: [], loadedCount: 0}), {feedFresh: true});
  assert.equal(lan.loadedKnown, true);
  assert.equal(lan.loadedCount, 0);
  assert.match(lan.subtitle, /0 LOADED/);
  for (const loadedCount of [0, 1]) {
    const share = macPeerView(peer({state: 'linked', models: [], loadedCount}), {feedFresh: true});
    assert.equal(share.loadedKnown, false);
    assert.equal(share.loadedCount, null);
    assert.match(share.subtitle, /LOADED STATE UNKNOWN/);
  }
});

test('peer loaded totals cannot understate the validated loaded rows', () => {
  const v = macPeerView(peer({loadedCount: 1, models: [
    {id: 'a', state: 'loaded'}, {id: 'b', state: 'loaded'},
  ]}), {feedFresh: true});
  assert.equal(v.loadedKnown, false);
  assert.equal(v.loadedCount, null);
  assert.match(v.subtitle, /LOADED STATE UNKNOWN/);
});

test('fully known model residency keeps loaded and zero-loaded totals', () => {
  for (const state of ['linked', 'reachable']) {
    const known = macPeerView(peer({state, loadedCount: 1}), {feedFresh: true});
    assert.equal(known.loadedKnown, true);
    assert.equal(known.loadedCount, 1);
    assert.deepEqual(known.loaded.map(m => m.id), ['openai/gpt-oss-20b']);
    const inactive = macPeerView(peer({state, loadedCount: 0,
      models: [{id: 'a', state: 'not-loaded'}, {id: 'b', state: 'not-loaded'}]}), {feedFresh: true});
    assert.equal(inactive.loadedKnown, true);
    assert.equal(inactive.loadedCount, 0);
    assert.match(inactive.subtitle, /0 LOADED/);
    assert.equal(inactive.rows.find(([k]) => k === 'Loaded models')[1], 'None');
  }
});

test('a linked expected-model row follows current share residency instead of an old LAN boolean', () => {
  const id = 'openai/gpt-oss-20b';
  for (const [state, expectedVerifyLoaded, label, loadedCount] of [
    ['loaded', false, 'loaded', 1], ['not-loaded', true, 'not loaded', 0],
  ]) {
    const v = macPeerView(peer({state: 'linked', expectedVerifyLoaded, loadedCount,
      models: [{id, state}]}), {feedFresh: true});
    assert.equal(v.rows.find(([key]) => key === 'Expected verify model')[1], `${id} · ${label}`);
  }
});

test('an old loaded claim cannot fill unknown or absent expected-model residency in a share report', () => {
  const id = 'openai/gpt-oss-20b';
  for (const models of [[{id, state: 'unknown'}], [{id, state: 'listed'}], [],
    [{id: 'qwen/qwen3.8-27b', state: 'loaded'}]]) {
    const v = macPeerView(peer({state: 'linked', expectedVerifyLoaded: true, models}), {feedFresh: true});
    const expected = v.rows.find(([key]) => key === 'Expected verify model')[1];
    assert.ok(expected.includes(id));
    assert.doesNotMatch(expected, / · (?:not )?loaded(?:$| ·)/,
      `current share states ${JSON.stringify(models)} do not establish expected-model residency`);
  }
});

test('absence from a complete LAN inventory proves the expected model is not loaded', () => {
  const id = 'openai/gpt-oss-20b';
  for (const models of [[], [{id: 'qwen/qwen3.8-27b', state: 'loaded'}]]) {
    const v = macPeerView(peer({expectedVerifyLoaded: true, models, loadedCount: models.length}), {feedFresh: true});
    assert.equal(v.rows.find(([key]) => key === 'Expected verify model')[1], `${id} · not loaded`);
  }
});

test('peer rows name each AGIW instance and its freshness', () => {
  const rows = agiwPeersRows([{host: 'louis-m5-pro', fresh: true, platform: 'macos', edition: 'mac', version: '1.0.0', ageSeconds: 4,
    source: 'agiw-peers', modelsComplete: true, models: [{id: 'a', loadedState: 'loaded'}]}, {host: 'bad host!', ageSeconds: 1}], {feedFresh: true});
  assert.equal(rows.length, 1);
  assert.match(rows[0][1], /^Reported \(unverified\) · macos · mac · 1\.0\.0 · 1 loaded · 4 s ago/);
  assert.deepEqual(agiwPeersRows([], {feedFresh: true}), [['AGIW peers', 'None has published on SharedChami yet']]);
});

test('peer rows disclose unknown or partial residency instead of zero loaded', () => {
  for (const models of [undefined, [], [{id: 'a', loadedState: 'unknown'}],
    [{id: 'a', loadedState: 'loaded'}, {id: 'b', loadedState: 'unknown'}],
    [{id: 'a', loadedState: 'inactive'}, {id: 'b'}]]) {
    const rows = agiwPeersRows([{host: 'mac-peer', fresh: true, ageSeconds: 2, models, modelsComplete: true}], {feedFresh: true});
    assert.match(rows[0][1], /loaded state unknown/i);
    assert.doesNotMatch(rows[0][1], /(?:^| · )\d+ loaded(?: · |$)/);
  }
  assert.match(agiwPeersRows([{host: 'mac-peer', fresh: true, ageSeconds: 2,
    modelsComplete: true, models: [{id: 'a', loadedState: 'loaded'}, {id: 'b', loadedState: 'inactive'}]}],
    {feedFresh: true})[0][1], /1 loaded/);
  assert.match(agiwPeersRows([{host: 'mac-peer', fresh: true, ageSeconds: 2,
    modelsComplete: true, models: [{id: 'a', loadedState: 'inactive'}]}], {feedFresh: true})[0][1], /0 loaded/);
  assert.match(agiwPeersRows([{host: 'mac-peer', fresh: true, ageSeconds: 2, modelsComplete:true, models:[]}],
    {feedFresh:true})[0][1], /Loaded state unknown/);
});

test('peer rows age share freshness with the dashboard snapshot', () => {
  const reported = [{host: 'mac-peer', fresh: true, ageSeconds: 28,
    models: [{id: 'a', loadedState: 'loaded'}]}];
  assert.match(agiwPeersRows(reported, {feedFresh: true, snapshotAge: 2})[0][1], /^Reported \(unverified\).*30 s ago/);
  const expired = agiwPeersRows(reported, {feedFresh: true, snapshotAge: 2.01})[0][1];
  assert.match(expired, /^Stale/);
  assert.doesNotMatch(expired, /Reported \(unverified\)|Linked/);
  assert.match(agiwPeersRows([{...reported[0], fresh: false, ageSeconds: 1}], {feedFresh: true})[0][1], /^Stale/);
});

test('the reported loaded count wins over the validated name list, and the gap is disclosed', () => {
  const v = macPeerView({state: 'reachable', ageSeconds: 1, loadedCount: 2, models: [{id: 'bad name', state: 'loaded'}, {id: 'ok', state: 'loaded'}]}, {feedFresh: true});
  assert.equal(v.loadedCount, 2);
  assert.match(v.subtitle, /2 LOADED/);
  assert.equal(v.rows.find(([k]) => k === 'Loaded models')[1], 'ok · 1 more with an unreadable name');
});

test('every PC adapter is listed; the AMD card shows counter load without inventing temperature or power', () => {
  const v = pcAdaptersView([{name: 'NVIDIA GeForce RTX 3080 Ti', utilizationPercent: 0, memoryUsedMiB: 11769, memoryTotalMiB: 12086, temperatureC: 39, powerW: 7.3},
    {name: 'AMD Radeon RX 5700 XT', utilizationPercent: 11.3, memoryUsedMiB: 1701, memoryTotalMiB: 8152, temperatureC: null, powerW: null}], {feedFresh: true});
  assert.equal(v.tile, 'RTX 3080 Ti 0% · RX 5700 XT 11%');
  assert.equal(v.rows[1][1], '11% busy · 1.7 of 8.0 GB');
  assert.match(v.rows[0][1], /39 °C · 7 W$/);
  assert.equal(pcAdaptersView([], {feedFresh: false}).known, false);
});

test('a claimed Windows route reads as a recorded claim, never as running', async () => {
  const {vitalsView} = await import('./web/map-layout.mjs');
  const v = vitalsView({fresh: true, pipeline: {status: 'claimed'}});
  assert.equal(v.route.detail, 'Claim recorded · execution unknown');
  assert.equal(v.route.tone, 'muted');
});

const peerCountEvidence = (over = {}) => ({host: 'count-peer', fresh: true, platform: 'macos', edition: 'mac',
  version: '1.0.0', ageSeconds: 4, source: 'agiw-peers',
  models: [{id: 'loaded', loadedState: 'loaded'}, {id: 'inactive', loadedState: 'inactive'}], ...over});

test('peer count UI: on every host a count needs an explicit complete inventory (Daybreak #8)', () => {
  // Both editions' readers emit modelsComplete, so no host keeps an exact count from a missing or false flag.
  const unknown = [
    ['count-peer', 'Reported (unverified) · macos · mac · 1.0.0 · Loaded state unknown · 4 s ago · agiw-peers'],
    ['idle-peer', 'Reported (unverified) · macos · mac · 1.0.0 · Loaded state unknown · 4 s ago · agiw-peers'],
  ];
  const exact = [
    ['count-peer', 'Reported (unverified) · macos · mac · 1.0.0 · 1 loaded · 4 s ago · agiw-peers'],
    ['idle-peer', 'Reported (unverified) · macos · mac · 1.0.0 · 0 loaded · 4 s ago · agiw-peers'],
  ];
  for (const options of [{feedFresh: true}, {feedFresh: true, host: 'mac'},
    {feedFresh: true, host: 'linux'}, {feedFresh: true, host: 'Windows'}, {feedFresh: true, host: 'windows'}]) {
    for (const [completeness, expected] of [[{}, unknown], [{modelsComplete: false}, unknown], [{modelsComplete: true}, exact]]) {
      const peers = [peerCountEvidence(completeness), peerCountEvidence({...completeness,
        host: 'idle-peer', models: [{id: 'inactive', loadedState: 'inactive'}]})];
      assert.deepEqual(agiwPeersRows(peers, options), expected, `host ${options.host ?? 'default'} ${JSON.stringify(completeness)}`);
    }
  }
});

test('Windows peer count UI: explicitly complete known residency preserves exact one and zero counts', () => {
  const peers = [peerCountEvidence({modelsComplete: true}), peerCountEvidence({host: 'idle-peer',
    modelsComplete: true, models: [{id: 'inactive', loadedState: 'inactive'}]})];
  assert.deepEqual(agiwPeersRows(peers, {feedFresh: true, host: 'windows'}), [
    ['count-peer', 'Reported (unverified) · macos · mac · 1.0.0 · 1 loaded · 4 s ago · agiw-peers'],
    ['idle-peer', 'Reported (unverified) · macos · mac · 1.0.0 · 0 loaded · 4 s ago · agiw-peers'],
  ]);
});

test('Windows peer count UI: incomplete or non-boolean completeness never creates an exact total', () => {
  for (const completeness of [{}, ...[false, null, 0, 1, 'true', 'false', [], {}]
    .map(modelsComplete => ({modelsComplete}))]) {
    const peers = [peerCountEvidence(completeness), peerCountEvidence({...completeness,
      host: 'idle-peer', models: [{id: 'inactive', loadedState: 'inactive'}]})];
    const rows = agiwPeersRows(peers, {feedFresh: true, host: 'windows'});
    assert.deepEqual(rows, [
      ['count-peer', 'Reported (unverified) · macos · mac · 1.0.0 · Loaded state unknown · 4 s ago · agiw-peers'],
      ['idle-peer', 'Reported (unverified) · macos · mac · 1.0.0 · Loaded state unknown · 4 s ago · agiw-peers'],
    ], `completeness ${JSON.stringify(completeness)} cannot certify the retained inventory`);
    for (const [, line] of rows) assert.doesNotMatch(line, /(?:^| · )\d+ loaded(?: · |$)/);
  }
});

test('Windows peer count UI: a complete empty or unknown inventory still has no known loaded total', () => {
  for (const models of [undefined, [], [{id: 'unproved', loadedState: 'unknown'}],
    [{id: 'loaded', loadedState: 'loaded'}, {id: 'unproved', loadedState: 'unknown'}]]) {
    const rows = agiwPeersRows([peerCountEvidence({modelsComplete: true, models})],
      {feedFresh: true, host: 'windows'});
    assert.deepEqual(rows, [
      ['count-peer', 'Reported (unverified) · macos · mac · 1.0.0 · Loaded state unknown · 4 s ago · agiw-peers'],
    ]);
    assert.doesNotMatch(rows[0][1], /(?:^| · )\d+ loaded(?: · |$)/);
  }
});

test('Windows peer count UI: completeness leaves the thirty-second freshness boundary and other fields unchanged', () => {
  const cases = [
    {modelsComplete: true, verified: false,
      fresh: 'Reported (unverified) · macos · mac · 1.0.0 · 1 loaded · 30 s ago · agiw-peers',
      stale: 'Stale · macos · mac · 1.0.0 · 1 loaded · 30 s ago · agiw-peers',
      markedStale: 'Stale · macos · mac · 1.0.0 · 1 loaded · 1 s ago · agiw-peers'},
    {modelsComplete: true, verified: true,
      fresh: 'Linked · macos · mac · 1.0.0 · 1 loaded · 30 s ago · agiw-peers',
      stale: 'Stale · macos · mac · 1.0.0 · 1 loaded · 30 s ago · agiw-peers',
      markedStale: 'Stale · macos · mac · 1.0.0 · 1 loaded · 1 s ago · agiw-peers'},
    {modelsComplete: false, verified: false,
      fresh: 'Reported (unverified) · macos · mac · 1.0.0 · Loaded state unknown · 30 s ago · agiw-peers',
      stale: 'Stale · macos · mac · 1.0.0 · Loaded state unknown · 30 s ago · agiw-peers',
      markedStale: 'Stale · macos · mac · 1.0.0 · Loaded state unknown · 1 s ago · agiw-peers'},
    {modelsComplete: false, verified: true,
      fresh: 'Linked · macos · mac · 1.0.0 · Loaded state unknown · 30 s ago · agiw-peers',
      stale: 'Stale · macos · mac · 1.0.0 · Loaded state unknown · 30 s ago · agiw-peers',
      markedStale: 'Stale · macos · mac · 1.0.0 · Loaded state unknown · 1 s ago · agiw-peers'},
  ];
  for (const {modelsComplete, verified, fresh, stale, markedStale} of cases) {
    const peers = [peerCountEvidence({ageSeconds: 28, modelsComplete, verified})];
    assert.deepEqual(agiwPeersRows(peers, {feedFresh: true, snapshotAge: 2, host: 'windows'}),
      [['count-peer', fresh]]);
    assert.deepEqual(agiwPeersRows(peers, {feedFresh: true, snapshotAge: 2.01, host: 'windows'}),
      [['count-peer', stale]]);
    assert.deepEqual(agiwPeersRows([{...peers[0], fresh: false, ageSeconds: 1}],
      {feedFresh: true, host: 'windows'}), [['count-peer', markedStale]]);
  }
});

test('Windows peer count UI: stale feeds and fresh empty peer feeds retain their literal messages', () => {
  for (const completeness of [{}, {modelsComplete: true}, {modelsComplete: false}]) {
    assert.deepEqual(agiwPeersRows([peerCountEvidence(completeness)], {feedFresh: false, host: 'windows'}),
      [['AGIW peers', 'Unknown (feed stale)']]);
  }
  assert.deepEqual(agiwPeersRows([], {feedFresh: true, host: 'windows'}),
    [['AGIW peers', 'None has published on SharedChami yet']]);
});
