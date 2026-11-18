// Shared per-lane model-call settings for the pilot programs. No secrets are read or recorded.
export const DEFAULT_MODEL_TIMEOUT_MS = 30000;
export const MAX_MODEL_TIMEOUT_MS = 900000;

export function modelTimeoutMs(cfg) {
  const value = cfg?.modelTimeoutMs ?? DEFAULT_MODEL_TIMEOUT_MS;
  if (!Number.isSafeInteger(value) || value < 1000 || value > MAX_MODEL_TIMEOUT_MS) throw Error('MODEL_TIMEOUT_INVALID');
  return value;
}

// Whole-run Nisi deadline: the model call plus a fixed margin for checks and the report store.
export function workflowDeadlineMs(cfg) {
  return modelTimeoutMs(cfg) + 10000;
}

// Client-side reasoning off for one request (llama-server honours chat_template_kwargs and
// reasoning_format; OpenAI-compatible servers honour reasoning_effort). Off by default.
export function reasoningFields(cfg) {
  if (cfg?.disableThinking === undefined || cfg.disableThinking === false) return {};
  if (cfg.disableThinking !== true) throw Error('DISABLE_THINKING_INVALID');
  return {chat_template_kwargs: {enable_thinking: false}, reasoning_format: 'none', reasoning_effort: 'none'};
}

// Error name/code only (our own UPPER_CASE codes, Node error codes, AbortError/TimeoutError).
// Never messages from servers, payloads, paths or headers.
const REDACTIONS = [
  [/(bearer|token|api[_-]?key|authorization|password|secret)\s*[:=]?\s*\S+/gi, '$1 [redacted]'],
  [/[A-Za-z]:\\[^\s'"]+/g, '[path]'],
  [/(^|[\s'"(=])\/[^\s'"]+/g, '$1[path]'],
  [/[A-Za-z0-9+/_=-]{24,}/g, '[redacted]'],
];

// Short, sanitized diagnostic text: no tokens, long opaque strings or filesystem paths.
export function sanitizeMessage(value) {
  if (typeof value !== 'string' || !value) return null;
  let text = value.replace(/[\u0000-\u001f\u007f]+/g, ' ');
  for (const [pattern, replacement] of REDACTIONS) text = text.replace(pattern, replacement);
  text = text.trim().slice(0, 200);
  return text || null;
}

export function errorEvidence(error) {
  const name = typeof error?.name === 'string' && /^[A-Za-z]{1,40}$/.test(error.name) ? error.name : 'Error';
  const fromMessage = typeof error?.message === 'string' && /^[A-Z][A-Z0-9_]{1,59}$/.test(error.message) ? error.message : null;
  const fromCode = typeof error?.code === 'string' && /^[A-Z][A-Z0-9_]{1,59}$/.test(error.code) ? error.code : null;
  const evidence = {errorName: name, errorCode: fromMessage ?? fromCode ?? (name === 'TimeoutError' || name === 'AbortError' ? 'MODEL_TIMEOUT' : 'UNCLASSIFIED'),
    errorMessage: sanitizeMessage(error?.message)};
  const server = sanitizeMessage(error?.serverMessage);
  if (server) evidence.serverMessage = server;
  return evidence;
}

export const MIN_MAX_TOKENS = 16;
export const MAX_MAX_TOKENS = 4096;

// Output-token cap for one model call, from the job/worker/lane config; programs keep their own default.
export function maxTokens(cfg, fallback) {
  const value = cfg?.maxTokens ?? fallback;
  if (!Number.isSafeInteger(value) || value < MIN_MAX_TOKENS || value > MAX_MAX_TOKENS) throw Error('MAX_TOKENS_INVALID');
  return value;
}

// Sanitized, truncated sample of raw model output for diagnosing refused or unparsable answers.
export function outputSample(value, limit = 300) {
  if (typeof value !== 'string' || !value) return null;
  let text = value.replace(/[\u0000-\u001f\u007f]+/g, ' ');
  for (const [pattern, replacement] of REDACTIONS) text = text.replace(pattern, replacement);
  return text.trim().slice(0, limit) || null;
}

const HARMONY_FINAL = /^(?:<\|start\|>assistant)?<\|channel\|>analysis<\|message\|>[\s\S]*?<\|end\|>\s*<\|start\|>assistant<\|channel\|>final<\|message\|>([\s\S]*?)(?:<\|return\|>|<\|end\|>)?$/;
const HARMONY_FINAL_ONLY = /^(?:<\|start\|>assistant)?<\|channel\|>final<\|message\|>([\s\S]*?)(?:<\|return\|>|<\|end\|>)?$/;
const THINK_BLOCK = /^<think>[\s\S]*?<\/think>\s*([\s\S]*)$/;

// Strict, fail-closed final-answer extraction. Plain JSON is used as is. Otherwise only a known
// preamble (harmony analysis->final channels, or one leading <think> block) followed by exactly one
// JSON object is accepted; the object text is returned unchanged. Anything else is refused.
export function extractFinalJSON(content, parse) {
  if (typeof content !== 'string') throw Error('CONTENT_REFUSED');
  const isObject = text => { const v = parse(text); return v !== null && typeof v === 'object' && !Array.isArray(v); };
  try { parse(content); return {content, stripped: null}; } catch { /* try known preambles */ }
  const trimmed = content.trim();
  for (const [kind, pattern] of [['harmony', HARMONY_FINAL], ['harmony', HARMONY_FINAL_ONLY], ['think', THINK_BLOCK]]) {
    const match = pattern.exec(trimmed);
    if (!match) continue;
    const inner = match[1].trim();
    if (!inner || /<\||<\/?think>/.test(inner)) throw Error('CONTENT_REFUSED');
    let ok = false;
    try { ok = isObject(inner); } catch { ok = false; }
    if (!ok) throw Error('CONTENT_REFUSED');
    return {content: inner, stripped: kind};
  }
  throw Error('CONTENT_REFUSED');
}

// Opt-in structured output per lane/worker/job. 'json-schema' sends an OpenAI-style strict
// json_schema (llama-server compiles it to a grammar, which also forces EOS after the object).
export const RESPONSE_FORMATS = ['json-object', 'json-schema'];
export function responseFormatMode(cfg) {
  const value = cfg?.responseFormat;
  if (value === undefined || value === null) return null;
  if (!RESPONSE_FORMATS.includes(value)) throw Error('RESPONSE_FORMAT_INVALID');
  return value;
}

export function responseFormatFor(mode, {name, schema, fallback}) {
  if (mode === 'json-schema') return {type: 'json_schema', json_schema: {name, strict: true, schema}};
  if (mode === 'json-object') return {type: 'json_object'};
  return fallback;
}

// Statuses with which a server rejects an unsupported or uncompilable schema.
export const SCHEMA_REJECT_STATUSES = new Set([400, 422, 500, 501]);

// One loopback POST. Errors carry httpStatus and the server's own error text (sanitized later).
export function postJSON({port, path, body, timeoutMs, maxBytes = 16384, request}) {
  return new Promise((resolve, reject) => {
    const req = request({hostname: '127.0.0.1', port, path, method: 'POST', headers: {'Content-Type': 'application/json'},
      signal: AbortSignal.timeout(timeoutMs)}, res => {
      if (res.statusCode !== 200) {
        let b = '';
        res.on('data', c => { if (b.length < 2048) b += c; });
        res.on('end', () => {
          const e = Object.assign(Error('HTTP_REFUSED'), {httpStatus: res.statusCode});
          try { const j = JSON.parse(b); e.serverMessage = typeof j?.error?.message === 'string' ? j.error.message : (typeof j?.error === 'string' ? j.error : null); } catch { e.serverMessage = b.slice(0, 200); }
          reject(e);
        });
        res.on('error', reject);
        return;
      }
      let s = '';
      res.on('data', c => { s += c; if (Buffer.byteLength(s) > maxBytes) { res.destroy(); reject(Error('RESPONSE_CAP')); } });
      res.on('end', () => resolve(s));
      res.on('error', reject);
    });
    req.on('error', reject);
    req.end(JSON.stringify(body));
  });
}

// Chat call with the json_schema -> json_object fallback. The fallback happens at most once, only when
// the schema was opted in and the server rejected the request with an HTTP error (never on timeouts or
// on a model answer), within the same overall time budget. It is recorded on the receipt.
export async function chatCompletion({port, path, payload, timeoutMs, receipt, allowFallback, request, now = Date.now}) {
  const started = now();
  receipt.responseFormat = payload.response_format?.type ?? null;
  receipt.attempts = 1;
  try {
    const raw = await postJSON({port, path, body: payload, timeoutMs, request});
    receipt.httpStatus = 200;
    return raw;
  } catch (e) {
    if (e?.httpStatus) receipt.httpStatus = e.httpStatus;
    if (!allowFallback || payload.response_format?.type !== 'json_schema' || !SCHEMA_REJECT_STATUSES.has(e?.httpStatus)) throw e;
    const remaining = timeoutMs - (now() - started);
    if (remaining < 1000) throw e;
    receipt.responseFormatFallback = 'json_schema->json_object';
    receipt.fallbackHttpStatus = e.httpStatus;
    const why = sanitizeMessage(e.serverMessage);
    if (why) receipt.fallbackServerMessage = why;
    receipt.responseFormat = 'json_object';
    receipt.attempts = 2;
    const raw = await postJSON({port, path, body: {...payload, response_format: {type: 'json_object'}}, timeoutMs: remaining, request});
    receipt.httpStatus = 200;
    return raw;
  }
}

// ---- Request modes --------------------------------------------------------------------------
// 'chat' (default): /v1/chat/completions. 'completion-grammar' (opt-in per lane): render the chat
// template with /apply-template, append a lane prefill, and decode with /completion under a grammar
// built from the expected answer schema. Same 127.0.0.1 host and port as the configured endpoint.
export const REQUEST_MODES = ['chat', 'completion-grammar'];
export const DEFAULT_PREFILL = ' to=self<|message|><|eom|><|start|>assistant to=user<|message|>';
export const DEFAULT_PROMPT_SUFFIX = '<|start|>assistant';
export const CONSTRAINTS = ['grammar', 'json-schema'];
const TEMPLATE_TEXT = /^[\x20-\x7e\n]{0,160}$/;

export function requestMode(cfg) {
  const value = cfg?.requestMode ?? 'chat';
  if (!REQUEST_MODES.includes(value)) throw Error('REQUEST_MODE_INVALID');
  return value;
}

export function completionSettings(cfg) {
  const prefill = cfg?.completionPrefill ?? DEFAULT_PREFILL;
  const promptSuffix = cfg?.promptSuffix ?? DEFAULT_PROMPT_SUFFIX;
  const constraint = cfg?.completionConstraint ?? 'grammar';
  if (typeof prefill !== 'string' || !TEMPLATE_TEXT.test(prefill)) throw Error('COMPLETION_PREFILL_INVALID');
  if (typeof promptSuffix !== 'string' || !TEMPLATE_TEXT.test(promptSuffix)) throw Error('PROMPT_SUFFIX_INVALID');
  if (!CONSTRAINTS.includes(constraint)) throw Error('COMPLETION_CONSTRAINT_INVALID');
  return {prefill, promptSuffix, constraint};
}

// Sibling llama-server routes on the same loopback origin; anything else is refused.
export function completionURLs(endpoint) {
  const u = new URL(endpoint);
  if (u.protocol !== 'http:' || u.hostname !== '127.0.0.1' || !u.port || u.username || u.password || u.search || u.hash ||
      u.pathname !== '/v1/chat/completions') throw Error('ENDPOINT_REFUSED');
  return {port: Number(u.port), applyTemplate: '/apply-template', completion: '/completion'};
}

const KEY = /^[a-z_][a-z0-9_]{0,31}$/;
const GBNF_TYPES = {
  integer: 'integer ::= "-"? ("0" | [1-9] [0-9]*)',
  string: 'string ::= "\\"" [^"\\\\\\x00-\\x1F]* "\\""',
  boolean: 'boolean ::= "true" | "false"',
};

// GBNF for a flat, closed JSON object schema: every property required, no extra keys, fixed key
// order, scalar types only. Anything else is refused rather than approximated.
export function gbnfFromSchema(schema) {
  const props = schema?.properties;
  if (schema?.type !== 'object' || schema.additionalProperties !== false || !props || typeof props !== 'object') throw Error('SCHEMA_UNSUPPORTED');
  const keys = Object.keys(props);
  const required = Array.isArray(schema.required) ? schema.required : [];
  if (!keys.length || keys.length > 8 || required.length !== keys.length || !keys.every(k => required.includes(k) && KEY.test(k))) throw Error('SCHEMA_UNSUPPORTED');
  const used = [];
  const parts = keys.map((k, i) => {
    const type = props[k]?.type;
    if (!GBNF_TYPES[type] || Object.keys(props[k]).length !== 1) throw Error('SCHEMA_UNSUPPORTED');
    if (!used.includes(type)) used.push(type);
    return `${i ? ' ws "," ws ' : ''}"\\"${k}\\"" ws ":" ws ${type}`;
  });
  return [`root ::= "{" ws ${parts.join('')} ws "}"`, ...used.map(t => GBNF_TYPES[t]), 'ws ::= " "?'].join('\n') + '\n';
}

// llama-server stop_type (or the older stopped_* flags) -> OpenAI-style finish reason.
export function finishFromStopType(body) {
  const type = typeof body?.stop_type === 'string' ? body.stop_type
    : body?.stopped_eos === true ? 'eos' : body?.stopped_word === true ? 'word' : body?.stopped_limit === true ? 'limit' : null;
  return {stopType: type, finishReason: type === 'eos' || type === 'word' ? 'stop' : type === 'limit' ? 'length' : null};
}

// Template render + grammar-constrained completion within one time budget. Returns the same shape
// as the chat path. Never records prompt text; the caller keeps its 300-character output sample.
export async function completionGrammarCall({endpoint, messages, schema, maxTokens, timeoutMs, receipt, settings, parse, request, now = Date.now}) {
  const {port, applyTemplate, completion} = completionURLs(endpoint);
  const started = now();
  const left = () => timeoutMs - (now() - started);
  receipt.requestMode = 'completion-grammar';
  let prompt;
  try {
    const rendered = parse(await postJSON({port, path: applyTemplate, body: {messages}, timeoutMs: left(), request}));
    if (typeof rendered?.prompt !== 'string' || !rendered.prompt || rendered.prompt.length > 16384) throw Error('TEMPLATE_RENDER_INVALID');
    if (settings.prefill && !rendered.prompt.endsWith(settings.promptSuffix)) throw Error('TEMPLATE_SUFFIX_MISMATCH');
    prompt = rendered.prompt + settings.prefill;
    receipt.templateRender = 'OK';
  } catch (e) {
    receipt.templateRender = 'FAILED';
    if (e?.httpStatus) receipt.httpStatus = e.httpStatus;
    const code = typeof e?.message === 'string' && /^TEMPLATE_[A-Z_]+$/.test(e.message) ? e.message : 'TEMPLATE_RENDER_FAILED';
    throw Object.assign(Error(code), {httpStatus: e?.httpStatus, serverMessage: e?.serverMessage, cause: e});
  }
  const grammar = gbnfFromSchema(schema);
  const base = {prompt, n_predict: maxTokens, temperature: 0, stream: false, cache_prompt: false};
  const send = (constraint, budget) => {
    if (budget < 1000) throw Error('MODEL_TIMEOUT');
    receipt.constraint = constraint;
    const body = constraint === 'json_schema' ? {...base, json_schema: schema} : {...base, grammar};
    return postJSON({port, path: completion, body, timeoutMs: budget, maxBytes: 65536, request});
  };
  receipt.attempts = 1;
  let raw;
  try {
    raw = await send(settings.constraint === 'json-schema' ? 'json_schema' : 'grammar', left());
  } catch (e) {
    if (e?.httpStatus) receipt.httpStatus = e.httpStatus;
    if (receipt.constraint !== 'json_schema' || !SCHEMA_REJECT_STATUSES.has(e?.httpStatus) || left() < 1000) throw e;
    receipt.constraintFallback = 'json_schema->grammar';
    receipt.fallbackHttpStatus = e.httpStatus;
    const why = sanitizeMessage(e.serverMessage);
    if (why) receipt.fallbackServerMessage = why;
    receipt.attempts = 2;
    raw = await send('grammar', left());
  }
  receipt.httpStatus = 200;
  const body = parse(raw);
  const {stopType, finishReason} = finishFromStopType(body);
  receipt.stopType = stopType;
  if (Number.isSafeInteger(body?.tokens_predicted)) receipt.tokensPredicted = body.tokens_predicted;
  const usage = Number.isSafeInteger(body?.tokens_predicted)
    ? {prompt_tokens: Number.isSafeInteger(body?.tokens_evaluated) ? body.tokens_evaluated : null, completion_tokens: body.tokens_predicted} : null;
  return {finishReason, model: typeof body?.model === 'string' ? body.model : null, content: body?.content, usage, toolCalls: false, refusal: false};
}

// Chat path, normalized to the same shape.
export async function chatCall({endpoint, payload, timeoutMs, receipt, allowFallback, parse, request, now}) {
  const u = new URL(endpoint);
  receipt.requestMode = 'chat';
  const raw = await chatCompletion({port: u.port, path: u.pathname, payload, timeoutMs, receipt, allowFallback, request, now});
  const envelope = parse(raw);
  if (!Array.isArray(envelope.choices) || envelope.choices.length !== 1) throw Error('CHOICES_REFUSED');
  const c = envelope.choices[0];
  return {finishReason: c.finish_reason, model: envelope.model, content: c.message?.content, usage: envelope.usage ?? null,
    toolCalls: Boolean(c.message?.tool_calls?.length), refusal: Boolean(c.message?.refusal)};
}

// Chat requires the served model id to match exactly. /completion bodies may omit it; when present it must match.
export function modelMatches(result, mode, model) {
  return mode === 'chat' ? result.model === model : (result.model === null || result.model === model);
}
