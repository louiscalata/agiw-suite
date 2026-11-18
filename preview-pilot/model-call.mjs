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
