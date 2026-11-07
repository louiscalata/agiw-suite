import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./web/simple-view.mjs', import.meta.url), 'utf8')
  .replace("'/model-control-view.mjs'", JSON.stringify(new URL('./web/model-control-view.mjs', import.meta.url).href));
const { controlSchematic } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const model = { id: 'lfm3-1.7b', name: 'LFM3 1.7B' };
const ready = { action: 'unload', reason: 'Ready to unload this idle local model.' };
const line = (view, n) => view.text.split('\n')[n];
const idle = { status: 'idle', settlement: 'not-started', message: 'No model operation has been requested.' };

test('an unknown control reply says unknown and draws no trail', () => {
  const view = controlSchematic({ model, control: null, controlKnown: false, decision: ready });
  assert.equal(view.text, 'CONTROL   LFM3 1.7B\nstate     UNKNOWN');
});

test('ready marks only the model', () => {
  const view = controlSchematic({ model, control: idle, controlKnown: true, decision: ready });
  assert.equal(line(view, 0), 'UNLOAD    LFM3 1.7B');
  assert.equal(line(view, 1), 'MODEL [x] -> YOU [ ] -> LM STUDIO [ ] -> INVENTORY [ ]');
  assert.equal(line(view, 2), 'state     READY');
  assert.equal(view.text.split('\n').length, 3, 'idle shows no backend message');
});

test('confirming waits on the person', () => {
  const view = controlSchematic({ model, control: idle, controlKnown: true, decision: ready, confirming: true });
  assert.equal(line(view, 1), 'MODEL [x] -> YOU [>] -> LM STUDIO [ ] -> INVENTORY [ ]');
  assert.equal(line(view, 2), 'state     WAITING FOR YOU');
});

test('running is at LM Studio and shows the backend sentence', () => {
  const view = controlSchematic({ model, controlKnown: true, decision: { action: null },
    control: { status: 'running', action: 'unload', settlement: 'pending', message: 'LM Studio unload requested; checking current state.' } });
  assert.equal(line(view, 0), 'UNLOAD    LFM3 1.7B');
  assert.equal(line(view, 1), 'MODEL [x] -> YOU [x] -> LM STUDIO [>] -> INVENTORY [ ]');
  assert.equal(line(view, 3), 'why       LM Studio unload requested; checking current state.');
});

test('reported is done only once inventory confirms', () => {
  const waiting = controlSchematic({ model, controlKnown: true, control: { status: 'reported', action: 'unload', settlement: 'pending' } });
  const done = controlSchematic({ model, controlKnown: true, control: { status: 'reported', action: 'unload', settlement: 'confirmed' } });
  assert.match(line(waiting, 1), /INVENTORY \[\?\]$/);
  assert.equal(line(waiting, 2), 'state     REPORTED, WAITING FOR INVENTORY');
  assert.match(line(done, 1), /INVENTORY \[x\]$/);
  assert.equal(line(done, 2), 'state     DONE, INVENTORY CONFIRMS');
});

test('a failure after dispatch may have happened; one before dispatch did not', () => {
  const after = controlSchematic({ model, controlKnown: true, control: { status: 'failed', action: 'unload', settlement: 'unconfirmed' } });
  const before = controlSchematic({ model, controlKnown: true, control: { status: 'failed', action: 'load', settlement: 'not-started' } });
  assert.equal(line(after, 1), 'MODEL [x] -> YOU [x] -> LM STUDIO [!] -> INVENTORY [?]');
  assert.equal(line(after, 2), 'state     FAILED, MAY HAVE HAPPENED');
  assert.equal(line(before, 0), 'LOAD      LFM3 1.7B');
  assert.equal(line(before, 1), 'MODEL [x] -> YOU [x] -> LM STUDIO [!] -> INVENTORY [ ]');
  assert.equal(line(before, 2), 'state     FAILED, NOTHING WAS SENT');
});

test('unavailable gives the reason the UI already decided', () => {
  const view = controlSchematic({ model, controlKnown: true, control: idle, decision: { action: null, reason: 'Live feed is stale; control is unavailable.' } });
  assert.equal(line(view, 1), 'MODEL [?] -> YOU [ ] -> LM STUDIO [ ] -> INVENTORY [ ]');
  assert.equal(line(view, 2), 'state     UNAVAILABLE');
  assert.equal(line(view, 3), 'why       Live feed is stale; control is unavailable.');
});

test('backend text is kept to printable ASCII and 120 characters', () => {
  const view = controlSchematic({ model, controlKnown: true, control: { status: 'running', action: 'unload', message: 'é'.repeat(300) } });
  assert.match(view.text, /^[\x20-\x7e\n]+$/);
  assert.ok(line(view, 3).length <= 'why       '.length + 120);
});
