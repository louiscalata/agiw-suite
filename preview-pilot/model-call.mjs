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
