import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./web/simple-view.mjs', import.meta.url), 'utf8')
  .replace("'/model-control-view.mjs'", JSON.stringify(new URL('./web/model-control-view.mjs', import.meta.url).href))
  .replace("'/monitor-store.mjs'", JSON.stringify(new URL('./web/monitor-store.mjs', import.meta.url).href));
const { observationBus } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const live = { models: [{ id: 'a', loaded: true }, { id: 'b', loaded: false }] };
const line = (view, n) => view.text.split('\n')[n];

test('no sample is unknown everywhere and never moves', () => {
  const view = observationBus({ snapshot: null, fresh: false, studioLive: false });
  assert.match(line(view, 0), /^LM STUDIO -+\?-+ OBSERVER -+\?-+ THIS VIEW$/);
  assert.equal(line(view, 1), 'source    UNKNOWN');
  assert.equal(line(view, 2), 'inventory UNKNOWN');
  assert.equal(line(view, 3), 'sample    NO SAMPLE YET');
  assert.equal(view.moving, false);
});

test('a fresh live sample says so in words and moves the glyph with the frame', () => {
  const a = observationBus({ snapshot: live, fresh: true, studioLive: true, frame: 0 });
  const b = observationBus({ snapshot: live, fresh: true, studioLive: true, frame: 1 });
  assert.equal(line(a, 1), 'source    LIVE');
  assert.equal(line(a, 2), 'inventory 2 MODELS / 1 LOADED');
  assert.equal(line(a, 3), 'sample    FRESH');
  assert.equal(a.moving, true);
  assert.notEqual(line(a, 0), line(b, 0));
  assert.equal(line(a, 0).length, line(b, 0).length);
});

test('reduced motion keeps every word and stops the glyph', () => {
  const a = observationBus({ snapshot: live, fresh: true, studioLive: true, frame: 0, reducedMotion: true });
  const b = observationBus({ snapshot: live, fresh: true, studioLive: true, frame: 5, reducedMotion: true });
  assert.equal(a.text, b.text);
  assert.equal(a.moving, false);
  assert.equal(line(a, 1), 'source    LIVE');
});

test('a stale sample breaks the second segment and hides the inventory count', () => {
  const view = observationBus({ snapshot: live, fresh: false, studioLive: true });
  assert.match(line(view, 0), /OBSERVER \.{12} THIS VIEW$/);
  assert.equal(line(view, 2), 'inventory UNKNOWN');
  assert.equal(line(view, 3), 'sample    STALE');
  assert.equal(view.moving, false);
});

test('a silent LM Studio shows a break, not a flow', () => {
  const view = observationBus({ snapshot: live, fresh: true, studioLive: false });
  assert.match(line(view, 0), /^LM STUDIO -+x-+ OBSERVER/);
  assert.equal(line(view, 1), 'source    NOT ANSWERING');
});

test('sample data is labelled and never animated', () => {
  const view = observationBus({ snapshot: { ...live, demo: true }, fresh: true, studioLive: false, frame: 3 });
  assert.equal(line(view, 1), 'source    SAMPLE DATA');
  assert.equal(line(view, 2), 'inventory 2 MODELS / 1 LOADED (SAMPLE)');
  assert.equal(view.moving, false);
});

test('paused stops motion and says paused', () => {
  const view = observationBus({ snapshot: live, fresh: true, studioLive: true, paused: true });
  assert.equal(line(view, 3), 'sample    PAUSED');
  assert.equal(view.moving, false);
});

test('a malformed inventory stays unknown', () => {
  const view = observationBus({ snapshot: { models: [null] }, fresh: true, studioLive: true });
  assert.equal(line(view, 2), 'inventory UNKNOWN');
});

test('the label reads the same facts as the text', () => {
  const view = observationBus({ snapshot: live, fresh: true, studioLive: true });
  assert.equal(view.label, 'Observation bus. Source live. Inventory 2 models / 1 loaded. Sample fresh.');
});

test('the bus is plain ASCII', () => {
  for (const args of [{ snapshot: null }, { snapshot: live, fresh: true, studioLive: true }, { snapshot: live, fresh: false, studioLive: false }]) {
    assert.match(observationBus(args).text, /^[\x20-\x7e\n]+$/);
  }
});


test('expired source evidence is stale whether its last state was answering or unavailable', () => {
  for (const studioLive of [true, false]) {
    const view = observationBus({snapshot: live, fresh: false, studioLive});
    assert.equal(line(view, 1), 'source    STALE');
    assert.equal(view.moving, false);
    assert.doesNotMatch(view.text, /source    LIVE|source    NOT ANSWERING/);
    assert.match(view.label, /Source stale/);
  }
});
