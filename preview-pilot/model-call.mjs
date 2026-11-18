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
