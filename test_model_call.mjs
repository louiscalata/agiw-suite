// Per-lane model-call settings shared by the pilot programs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {modelTimeoutMs, workflowDeadlineMs, reasoningFields, errorEvidence, sanitizeMessage} from './preview-pilot/model-call.mjs';

test('model timeout comes from config with a bounded default', () => {
  assert.equal(modelTimeoutMs({}), 30000);
  assert.equal(modelTimeoutMs({modelTimeoutMs: 400000}), 400000);
  assert.equal(workflowDeadlineMs({modelTimeoutMs: 400000}), 410000);
  for (const bad of [0, 999, 900001, '30000', 1.5]) assert.throws(() => modelTimeoutMs({modelTimeoutMs: bad}), /MODEL_TIMEOUT_INVALID/);
});

test('reasoning can be disabled per request, off by default', () => {
  assert.deepEqual(reasoningFields({}), {});
  assert.deepEqual(reasoningFields({disableThinking: false}), {});
  assert.deepEqual(reasoningFields({disableThinking: true}),
    {chat_template_kwargs: {enable_thinking: false}, reasoning_format: 'none', reasoning_effort: 'none'});
  assert.throws(() => reasoningFields({disableThinking: 'yes'}), /DISABLE_THINKING_INVALID/);
});

test('adapter errors become sanitized codes and messages', () => {
  const timeout = Object.assign(new Error('The operation was aborted due to timeout'), {name: 'TimeoutError'});
  assert.equal(errorEvidence(timeout).errorCode, 'MODEL_TIMEOUT');
  const http = Object.assign(new Error('HTTP_REFUSED'), {serverMessage: 'output does not match the expected peg-native format'});
  assert.deepEqual(errorEvidence(http), {errorName: 'Error', errorCode: 'HTTP_REFUSED', errorMessage: 'HTTP_REFUSED',
    serverMessage: 'output does not match the expected peg-native format'});
  const leaky = sanitizeMessage('token=sk-abcdefabcdefabcdefabcdef at /Users/me/key.txt and C:\\Users\\me\\x');
  assert.doesNotMatch(leaky, /sk-abc|\/Users|C:\\Users/);
});

test('no pilot program hardcodes a model timeout or deadline', () => {
  for (const program of ['nisi-smoke.mjs', 'tiny-arithmetic.mjs', 'tiny-extraction.mjs']) {
    const src = readFileSync(new URL('./preview-pilot/' + program, import.meta.url), 'utf8');
    assert.doesNotMatch(src, /AbortSignal\.timeout\(\d/);
    assert.doesNotMatch(src, /timeoutMs:\d/);
    assert.doesNotMatch(src, /totalDeadlineMs:\d/);
    assert.match(src, /workflowDeadlineMs\(cfg\)/);
  }
  for (const program of ['tiny-arithmetic.mjs', 'tiny-extraction.mjs']) {
    const src = readFileSync(new URL('./preview-pilot/' + program, import.meta.url), 'utf8');
    assert.match(src, /\.\.\.REASONING/);
    assert.match(src, /errorEvidence\(e\)/);
  }
});
