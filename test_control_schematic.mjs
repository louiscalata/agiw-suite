import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./web/simple-view.mjs', import.meta.url), 'utf8')
  .replace("'/model-control-view.mjs'", JSON.stringify(new URL('./web/model-control-view.mjs', import.meta.url).href));
const { controlSchematic, CONTROL_STATUSES } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

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

test('succeeded, the status the backend sends after a confirmed operation, is done only once inventory confirms', () => {
  const done = controlSchematic({ model, controlKnown: true, control: { status: 'succeeded', action: 'unload', settlement: 'confirmed' } });
  assert.match(done.text, /MODEL \[x\] -> YOU \[x\] -> LM STUDIO \[x\] -> INVENTORY \[x\]/);
  assert.match(done.text, /state\s+DONE, INVENTORY CONFIRMS/);
  const unsure = controlSchematic({ model, controlKnown: true, control: { status: 'succeeded', action: 'unload' } });
  assert.match(unsure.text, /INVENTORY \[\?\]/);
  assert.match(unsure.text, /REPORTED, WAITING FOR INVENTORY/);
});

test('every control status model_control.py can send is accepted by the compact view', () => {
  const python = readFileSync(new URL('./model_control.py', import.meta.url), 'utf8');
  const sent = new Set([...python.matchAll(/\bstatus="(\w+)"/g)].map(m => m[1])
    .concat([...python.matchAll(/"status": "(\w+)"/g)].map(m => m[1]), [...python.matchAll(/\("(succeeded|failed)", /g)].map(m => m[1])));
  assert.ok(sent.has('succeeded') && sent.has('running') && sent.has('failed') && sent.has('idle'), [...sent].join());
  for (const status of sent) assert.ok(CONTROL_STATUSES.has(status), `${status} is not accepted`);
});

test('the last operation is shown only for the model it was about; running is shown for any', () => {
  const other = { id: 'other-model', name: 'Other' };
  const done = { status: 'succeeded', action: 'unload', modelId: 'lfm3-1.7b', settlement: 'confirmed', message: 'Model unload confirmed.' };
  const about = controlSchematic({ model, controlKnown: true, control: done });
  assert.match(about.text, /DONE, INVENTORY CONFIRMS/);
  const elsewhere = controlSchematic({ model: other, controlKnown: true, control: done, decision: ready });
  assert.match(elsewhere.text, /state\s+READY/, 'another model starts from where it is, not from the last operation');
  assert.doesNotMatch(elsewhere.text, /DONE|confirmed/i);
  for (const status of ['reported', 'failed', 'succeeded']) {
    assert.doesNotMatch(controlSchematic({ model: other, controlKnown: true, control: { ...done, status }, decision: ready }).text, /DONE|FAILED|REPORTED/, status);
  }
  const running = controlSchematic({ model: other, controlKnown: true, control: { status: 'running', action: 'load', modelId: 'lfm3-1.7b', settlement: 'pending' } });
  assert.match(running.text, /RUNNING/);
  // The instance and key forms of the id count as the same model.
  for (const model2 of [{ id: 'x', instanceId: 'lfm3-1.7b' }, { id: 'x', modelKey: 'lfm3-1.7b' }]) {
    assert.match(controlSchematic({ model: model2, controlKnown: true, control: done }).text, /DONE, INVENTORY CONFIRMS/);
  }
  assert.match(controlSchematic({ model, controlKnown: true, control: { ...done, modelId: undefined } }).text, /DONE/, 'a record that names no model is read as before');
});

test('an operation a restart interrupted and fresh inventory later confirmed is not shown as failed', () => {
  const interrupted = { status: 'failed', action: 'unload', modelId: 'lfm3-1.7b', settlement: 'confirmed', message: 'The interrupted model operation is now confirmed by fresh inventory; no command was repeated.' };
  const view = controlSchematic({ model, controlKnown: true, control: interrupted });
  assert.match(view.text, /MODEL \[x\] -> YOU \[x\] -> LM STUDIO \[x\] -> INVENTORY \[x\]/);
  assert.match(view.text, /state\s+INTERRUPTED, INVENTORY CONFIRMS/);
  assert.doesNotMatch(view.text, /FAILED/);
  assert.match(controlSchematic({ model, controlKnown: true, control: { ...interrupted, settlement: 'unconfirmed' } }).text, /FAILED, MAY HAVE HAPPENED/);
  assert.match(controlSchematic({ model, controlKnown: true, control: { ...interrupted, settlement: 'not-started' } }).text, /FAILED, NOTHING WAS SENT/);
});
