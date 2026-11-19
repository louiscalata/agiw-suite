// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// Built-in client for TypeSafe's hosted Jev (System One) API, so `planWithJev` and
// `createJevReviewer` work with one environment variable and no hand-written judge.
//
// What it does: POST `{ model, state, questions }` to one fixed HTTPS endpoint with
// a Bearer key and return `{ model, answers }` for the advisor to validate. The
// task text, and for the reviewer the candidate's files, in `state` leave your
// machine for that service.
//
// Boundaries: the endpoint is fixed (a key is never sent anywhere else), redirects
// are errors, the request and response are size-bounded, the call has a timeout,
// and neither the key nor the response body ever appears in an error or a log.

const ENDPOINT = 'https://api.typesafe.ai/v1/systemone';
const DEFAULT_MODEL = 'jev-latest';
const KEY_ENV = 'TYPESAFE_API_KEY';
const MAX_REQUEST_BYTES = 1_048_576;
const MAX_RESPONSE_BYTES = 1_048_576;
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = code => { const error = new Error(code); error.code = code; throw error; };

export const JEV_API_KEY_ENV = KEY_ENV;

async function readBounded(response, cap, signal) {
  if (!response?.body?.getReader) fail('JEV_RESPONSE_INVALID');
  const reader = response.body.getReader(); const chunks = []; let total = 0;
  const cancel = () => { try { Promise.resolve(reader.cancel()).catch(() => {}); } catch { /* keep the original cause */ } };
  signal.addEventListener('abort', cancel, { once: true });
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      if (!(part.value instanceof Uint8Array)) fail('JEV_RESPONSE_INVALID');
      total += part.value.byteLength;
      if (total > cap) fail('JEV_RESPONSE_TOO_LARGE');
      chunks.push(part.value);
    }
  } catch (error) { cancel(); throw error; }
  finally { signal.removeEventListener('abort', cancel); try { reader.releaseLock(); } catch { /* released */ } }
  const bytes = new Uint8Array(total); let offset = 0;
  for (const part of chunks) { bytes.set(part, offset); offset += part.byteLength; }
  try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); } catch { fail('JEV_RESPONSE_INVALID'); }
}

// Returns a `judge({ state, questions })` for planWithJev / createJevReviewer.
// `apiKey` defaults to the TYPESAFE_API_KEY environment variable. `model` defaults
// to `jev-latest`; pin a version (for example `jev-1.14.0`) for repeatable runs.
// The call can run at most 300 s: Node's fetch itself gives up on a request that has
// received no response headers for that long. `fetch` is for tests.
export function createJevJudge({ apiKey, model = DEFAULT_MODEL, timeoutMs = 60_000, fetch: fetchImpl = globalThis.fetch } = {}) {
  const key = apiKey ?? process.env[KEY_ENV];
  if (typeof key !== 'string' || key.length === 0) fail('JEV_API_KEY_REQUIRED');
  if (key.length > 512 || !/^[\x21-\x7e]+$/u.test(key)) fail('JEV_API_KEY_INVALID');
  if (typeof model !== 'string' || !/^[a-z0-9][a-z0-9._-]{0,63}$/u.test(model)) fail('JEV_MODEL_INVALID');
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 300_000) fail('JEV_TIMEOUT_INVALID');
  if (typeof fetchImpl !== 'function') fail('JEV_UNAVAILABLE');
  return async function judge({ state, questions } = {}) {
    if (!isRecord(state) || !isRecord(questions) || Object.keys(questions).length === 0) fail('JEV_REQUEST_INVALID');
    const body = JSON.stringify({ model, state, questions });
    if (Buffer.byteLength(body, 'utf8') > MAX_REQUEST_BYTES) fail('JEV_REQUEST_TOO_LARGE');
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    try {
      let response;
      try {
        response = await fetchImpl(ENDPOINT, { method: 'POST', redirect: 'error', signal: controller.signal,
          headers: { authorization: `Bearer ${key}`, 'content-type': 'application/json', accept: 'application/json' }, body });
      } catch { fail(timedOut ? 'JEV_TIMEOUT' : 'JEV_UNAVAILABLE'); }
      if (!response?.ok) {
        try { Promise.resolve(response?.body?.cancel?.()).catch(() => {}); } catch { /* ignore */ }
        const status = response?.status;
        fail(status === 401 || status === 403 ? 'JEV_AUTH_FAILED' : status === 429 ? 'JEV_RATE_LIMITED' : 'JEV_HTTP_ERROR');
      }
      let text;
      try { text = await readBounded(response, MAX_RESPONSE_BYTES, controller.signal); }
      catch (error) { fail(timedOut ? 'JEV_TIMEOUT' : typeof error?.code === 'string' && error.code.startsWith('JEV_') ? error.code : 'JEV_UNAVAILABLE'); }
      let parsed;
      try { parsed = JSON.parse(text); } catch { fail('JEV_RESPONSE_INVALID'); }
      if (!isRecord(parsed) || typeof parsed.model !== 'string' || !isRecord(parsed.answers)) fail('JEV_RESPONSE_INVALID');
      return { model: parsed.model, answers: parsed.answers };
    } finally { clearTimeout(timer); }
  };
}
