// completion-grammar request mode: URL derivation, grammar, stop_type mapping, request bodies, failures.
import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {completionURLs, gbnfFromSchema, finishFromStopType, completionGrammarCall, completionSettings, requestMode,
  DEFAULT_PREFILL, modelMatches} from './preview-pilot/model-call.mjs';

const ARITH = {type: 'object', properties: {answer: {type: 'integer'}}, required: ['answer'], additionalProperties: false};
const COLOR = {type: 'object', properties: {color: {type: 'string'}}, required: ['color'], additionalProperties: false};
const EP = 'http://127.0.0.1:41234/v1/chat/completions';
const MSGS = [{role: 'system', content: 'Answer.'}, {role: 'user', content: 'What is 6 multiplied by 7?'}];
const RENDERED = '<|start|>system<|message|>Answer.<|eot|><|start|>user<|message|>What is 6 multiplied by 7?<|eot|><|start|>assistant';

// Fake http.request keyed by path; records [opts, body]. No network.
function fake(routes, calls) {
  return (opts, onResponse) => {
    const req = new EventEmitter();
    req.end = text => {
      calls.push({opts, body: JSON.parse(text)});
      const [status, body] = routes[opts.path].shift();
      const res = new EventEmitter();
      res.statusCode = status;
      onResponse(res);
      res.emit('data', JSON.stringify(body));
      res.emit('end');
    };
    return req;
  };
}
const settings = (over = {}) => completionSettings({requestMode: 'completion-grammar', ...over});
const run = (routes, over = {}) => {
  const calls = [], receipt = {};
  const p = completionGrammarCall({endpoint: EP, messages: MSGS, schema: ARITH, maxTokens: 512, timeoutMs: 840000, receipt,
    settings: settings(over), parse: JSON.parse, request: fake(routes, calls)});
  return {p, calls, receipt};
};
const DONE = (stop_type = 'eos', content = '{"answer":42}') => [200, {content, stop_type, tokens_predicted: 6, tokens_evaluated: 60, model: 'm'}];

test('URLs derive from the same 127.0.0.1 origin; anything else refused', () => {
  assert.deepEqual(completionURLs(EP), {port: 41234, applyTemplate: '/apply-template', completion: '/completion'});
  for (const bad of ['http://localhost:41234/v1/chat/completions', 'http://127.0.0.1/v1/chat/completions', 'https://127.0.0.1:1/v1/chat/completions',
    'http://127.0.0.1:1/completion', 'http://u:p@127.0.0.1:1/v1/chat/completions', 'http://127.0.0.1:1/v1/chat/completions?x=1']) {
    assert.throws(() => completionURLs(bad), /ENDPOINT_REFUSED/, bad);
  }
});

test('GBNF for both programs, and unsupported schemas refused', () => {
  assert.equal(gbnfFromSchema(ARITH), 'root ::= "{" ws "\\"answer\\"" ws ":" ws integer ws "}"\ninteger ::= "-"? ("0" | [1-9] [0-9]*)\nws ::= " "?\n');
  assert.equal(gbnfFromSchema(COLOR), 'root ::= "{" ws "\\"color\\"" ws ":" ws string ws "}"\nstring ::= "\\"" [^"\\\\\\x00-\\x1F]* "\\""\nws ::= " "?\n');
  for (const bad of [{...ARITH, additionalProperties: true}, {...ARITH, required: []}, {type: 'array'},
    {...ARITH, properties: {answer: {type: 'number'}}}, {...ARITH, properties: {answer: {type: 'integer', minimum: 0}}},
    {type: 'object', properties: {'a"b': {type: 'string'}}, required: ['a"b'], additionalProperties: false}]) {
    assert.throws(() => gbnfFromSchema(bad), /SCHEMA_UNSUPPORTED/);
  }
});

test('stop_type eos/word -> stop, limit -> length, else unknown', () => {
  assert.deepEqual(finishFromStopType({stop_type: 'eos'}), {stopType: 'eos', finishReason: 'stop'});
  assert.deepEqual(finishFromStopType({stop_type: 'word'}), {stopType: 'word', finishReason: 'stop'});
  assert.deepEqual(finishFromStopType({stop_type: 'limit'}), {stopType: 'limit', finishReason: 'length'});
  assert.deepEqual(finishFromStopType({stopped_limit: true}), {stopType: 'limit', finishReason: 'length'});
  assert.deepEqual(finishFromStopType({stop_type: 'none'}), {stopType: 'none', finishReason: null});
  assert.deepEqual(finishFromStopType({}), {stopType: null, finishReason: null});
});

test('request bodies: apply-template with the messages, then grammar completion with prefill', async () => {
  const {p, calls, receipt} = run({'/apply-template': [[200, {prompt: RENDERED}]], '/completion': [DONE()]});
  const r = await p;
  assert.deepEqual(calls.map(c => [c.opts.hostname, c.opts.port, c.opts.path, c.opts.method]),
    [['127.0.0.1', 41234, '/apply-template', 'POST'], ['127.0.0.1', 41234, '/completion', 'POST']]);
  assert.deepEqual(calls[0].body, {messages: MSGS});
  assert.deepEqual(calls[1].body, {prompt: RENDERED + DEFAULT_PREFILL, n_predict: 512, temperature: 0, stream: false, cache_prompt: false,
    grammar: gbnfFromSchema(ARITH)});
  assert.equal(DEFAULT_PREFILL, ' to=self<|message|><|eom|><|start|>assistant to=user<|message|>');
  assert.deepEqual(r, {finishReason: 'stop', model: 'm', content: '{"answer":42}', usage: {prompt_tokens: 60, completion_tokens: 6}, toolCalls: false, refusal: false});
  assert.deepEqual(receipt, {requestMode: 'completion-grammar', templateRender: 'OK', attempts: 1, constraint: 'grammar', httpStatus: 200, stopType: 'eos', tokensPredicted: 6});
  assert.doesNotMatch(JSON.stringify(receipt), /multiplied/);
});

test('limit maps to length (the caller fails closed)', async () => {
  const {p} = run({'/apply-template': [[200, {prompt: RENDERED}]], '/completion': [DONE('limit', '{"answer":4')]});
  assert.equal((await p).finishReason, 'length');
});

test('apply-template failure stops before any completion call', async () => {
  for (const [answer, code] of [[[500, {error: {message: 'template error'}}], 'TEMPLATE_RENDER_FAILED'], [[200, {nope: 1}], 'TEMPLATE_RENDER_INVALID'],
    [[200, {prompt: RENDERED + '<|eot|>'}], 'TEMPLATE_SUFFIX_MISMATCH']]) {
    const {p, calls, receipt} = run({'/apply-template': [answer], '/completion': [DONE()]});
    await assert.rejects(p, new RegExp(code));
    assert.equal(calls.length, 1);
    assert.equal(receipt.templateRender, 'FAILED');
  }
});

test('json-schema constraint falls back to the grammar on rejection, recorded', async () => {
  const {p, calls, receipt} = run({'/apply-template': [[200, {prompt: RENDERED}]], '/completion': [[400, {error: {message: 'unsupported json_schema'}}], DONE()]},
    {completionConstraint: 'json-schema'});
  await p;
  assert.deepEqual(calls[1].body.json_schema, ARITH);
  assert.equal(calls[1].body.grammar, undefined);
  assert.equal(calls[2].body.grammar, gbnfFromSchema(ARITH));
  assert.equal(receipt.constraintFallback, 'json_schema->grammar');
  assert.equal(receipt.attempts, 2);
  // A grammar rejection is never retried.
  const second = run({'/apply-template': [[200, {prompt: RENDERED}]], '/completion': [[500, {error: 'bad grammar'}], DONE()]});
  await assert.rejects(second.p, /HTTP_REFUSED/);
  assert.equal(second.calls.length, 2);
});

test('settings and mode validation', () => {
  assert.equal(requestMode({}), 'chat');
  assert.equal(requestMode({requestMode: 'completion-grammar'}), 'completion-grammar');
  assert.throws(() => requestMode({requestMode: 'completion'}), /REQUEST_MODE_INVALID/);
  assert.deepEqual(settings(), {prefill: DEFAULT_PREFILL, promptSuffix: '<|start|>assistant', constraint: 'grammar'});
  assert.equal(settings({completionPrefill: ''}).prefill, '');
  assert.throws(() => settings({completionPrefill: 'x\u0000'}), /COMPLETION_PREFILL_INVALID/);
  assert.throws(() => settings({completionPrefill: 'x'.repeat(161)}), /COMPLETION_PREFILL_INVALID/);
  assert.throws(() => settings({promptSuffix: 7}), /PROMPT_SUFFIX_INVALID/);
  assert.throws(() => settings({completionConstraint: 'regex'}), /COMPLETION_CONSTRAINT_INVALID/);
  assert.equal(modelMatches({model: null}, 'completion-grammar', 'm'), true);
  assert.equal(modelMatches({model: 'x'}, 'completion-grammar', 'm'), false);
  assert.equal(modelMatches({model: null}, 'chat', 'm'), false);
});
