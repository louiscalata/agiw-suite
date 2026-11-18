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

import {maxTokens, outputSample, extractFinalJSON} from './preview-pilot/model-call.mjs';
const strict = text => JSON.parse(text);

test('max tokens come from config within bounds', () => {
  assert.equal(maxTokens({}, 64), 64);
  assert.equal(maxTokens({maxTokens: 512}, 64), 512);
  for (const bad of [8, 5000, 1.5, '64']) assert.throws(() => maxTokens({maxTokens: bad}, 64), /MAX_TOKENS_INVALID/);
});

test('output sample is sanitized and truncated', () => {
  const s = outputSample('<|channel|>analysis ' + 'y'.repeat(10) + ' secret=hunter2hunter2 ' + 'z '.repeat(400));
  assert.ok(s.length <= 300);
  assert.doesNotMatch(s, /hunter2/);
});

test('plain JSON is returned unchanged', () => {
  assert.deepEqual(extractFinalJSON('{"answer":42}', strict), {content: '{"answer":42}', stripped: null});
});

test('known harmony and think preambles yield the final JSON only', () => {
  const h = '<|channel|>analysis<|message|>6*7<|end|><|start|>assistant<|channel|>final<|message|>{"answer":42}<|return|>';
  assert.deepEqual(extractFinalJSON(h, strict), {content: '{"answer":42}', stripped: 'harmony'});
  assert.deepEqual(extractFinalJSON('<|channel|>final<|message|>{"answer":42}', strict), {content: '{"answer":42}', stripped: 'harmony'});
  assert.deepEqual(extractFinalJSON('<think>multiply</think>\n{"answer":42}', strict), {content: '{"answer":42}', stripped: 'think'});
});

test('extraction fails closed on anything else', () => {
  for (const bad of [
    'Sure: {"answer":42}',
    '<|channel|>analysis<|message|>no final channel',
    '<|channel|>analysis<|message|>x<|end|><|start|>assistant<|channel|>final<|message|>not json',
    '<|channel|>final<|message|>[42]',
    '<|channel|>final<|message|>{"answer":42} trailing',
    '<|channel|>final<|message|>{"answer":42}<|end|><|start|>assistant<|channel|>final<|message|>{"answer":1}',
    '<think>a</think>{"answer":42}<think>b</think>',
    'prefix <think>a</think>{"answer":42}',
    '',
  ]) assert.throws(() => extractFinalJSON(bad, strict), /CONTENT_REFUSED/, bad);
  assert.throws(() => extractFinalJSON(null, strict), /CONTENT_REFUSED/);
});
