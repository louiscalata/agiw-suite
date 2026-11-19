// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// Built-in client for TypeSafe's hosted Jev (System One) API. `planWithJev` and
// `createJevReviewer` call it when `judge` is `'typesafe'`. The key alone does not.
//
// What it does: POST `{ model, state, questions }` to one fixed HTTPS endpoint with
// a Bearer key and return `{ model, answers }` for the advisor to validate. The
// task text, and for the reviewer the candidate's files, in `state` leave your
// machine for that service.
//
// Boundaries: the endpoint is fixed (a key is never sent anywhere else), redirects
// are errors, the request and response are size-bounded, the call has a timeout,
// and neither the key nor the response body ever appears in an error or a log.

import https from 'node:https';

const ENDPOINT = 'https://api.typesafe.ai/v1/systemone';
const DEFAULT_MODEL = 'jev-latest';
const KEY_ENV = 'TYPESAFE_API_KEY';
const MAX_REQUEST_BYTES = 1_048_576;
const MAX_RESPONSE_BYTES = 1_048_576;
// Same ceiling as the local-chat adapter. The default transport is node:https, which
// has no 300 s header cap; a caller-supplied fetch still does.
const MAX_TIMEOUT_MS = 86_400_000;
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = code => { const error = new Error(code); error.code = code; throw error; };
const unavailable = () => Object.assign(new Error('JEV_UNAVAILABLE'), { code: 'JEV_UNAVAILABLE' });

// POST to the fixed endpoint. Node's global fetch gives up after 300 s with no
// response headers (undici's headersTimeout). node:https does not, so timeoutMs
// and the caller's signal alone stop the call. Redirects are not followed.
// `requestImpl` is https.request in production and a stand-in in tests. The options
// always name this host, port and path. A stand-in that ignores them is the
// caller's own transport and already holds the key. The key is a request header
// and is never copied into an error.
function hostedFetch(url, { method = 'POST', headers = {}, body, signal } = {}, requestImpl = https.request) {
  if (url !== ENDPOINT) return Promise.reject(unavailable());
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(unavailable()); return; }
    const payload = body === undefined ? undefined : Buffer.from(body);
    let settled = false;
    const finish = error => { if (settled) return; settled = true; reject(error ?? unavailable()); };
    let request;
    try {
      request = requestImpl({
        host: 'api.typesafe.ai', port: 443, servername: 'api.typesafe.ai', path: '/v1/systemone', method, signal,
        headers: { ...headers, ...(payload ? { 'content-length': String(payload.length) } : {}) },
      }, res => {
        if (settled) { res.destroy(); return; }
        const status = res.statusCode ?? 0;
        if (status === 204 || status === 205 || status === 304 || status < 200 || status > 599) {
          res.destroy();
          settled = true;
          resolve(new Response(null, { status: status >= 200 && status <= 599 ? status : 502 }));
          return;
        }
        let finished = false;
        const stream = new ReadableStream({
          start(controller) {
            const close = action => { if (finished) return; finished = true; try { action(); } catch { /* already closed or cancelled */ } };
            res.on('data', chunk => { if (finished) return; try { controller.enqueue(chunk); } catch { finished = true; res.destroy(); return; } if (controller.desiredSize !== null && controller.desiredSize <= 0) res.pause(); });
            res.on('end', () => close(() => controller.close()));
            res.on('error', () => close(() => controller.error(unavailable())));
            res.on('aborted', () => close(() => controller.error(unavailable())));
          },
          pull() { res.resume(); },
          cancel() { finished = true; res.destroy(); },
        });
        settled = true;
        resolve(new Response(stream, { status }));
      });
    } catch { finish(); return; }
    if (!request || typeof request.on !== 'function' || typeof request.end !== 'function') { finish(); return; }
    request.on('error', () => finish());
    try { request.end(payload); } catch { finish(); }
  });
}

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
    // Cancelling on abort ends a pending read as done. That body is truncated, not complete.
    if (signal.aborted) fail('JEV_UNAVAILABLE');
  } catch (error) { cancel(); throw error; }
  finally { signal.removeEventListener('abort', cancel); try { reader.releaseLock(); } catch { /* released */ } }
  const bytes = new Uint8Array(total); let offset = 0;
  for (const part of chunks) { bytes.set(part, offset); offset += part.byteLength; }
  try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); } catch { fail('JEV_RESPONSE_INVALID'); }
}

// Returns a `judge({ state, questions })` for planWithJev / createJevReviewer.
// `apiKey` defaults to the TYPESAFE_API_KEY environment variable. `model` defaults
// to `jev-latest`; pin a version (for example `jev-1.14.0`) for repeatable runs.
// `timeoutMs` is 60 s by default and may be from 1 s to 24 h. The default transport
// is node:https to the fixed endpoint, so a value above 300 s is not cut short by
// fetch. `fetch` replaces that transport in tests; an injected fetch keeps fetch's
// own header timeout. `request` replaces `https.request` in tests and still receives
// only the fixed host, port and path.
export function createJevJudge({ apiKey, model = DEFAULT_MODEL, timeoutMs = 60_000, fetch: fetchImpl, request } = {}) {
  const key = apiKey ?? process.env[KEY_ENV];
  if (typeof key !== 'string' || key.length === 0) fail('JEV_API_KEY_REQUIRED');
  if (key.length > 512 || !/^[\x21-\x7e]+$/u.test(key)) fail('JEV_API_KEY_INVALID');
  if (typeof model !== 'string' || !/^[a-z0-9][a-z0-9._-]{0,63}$/u.test(model)) fail('JEV_MODEL_INVALID');
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > MAX_TIMEOUT_MS) fail('JEV_TIMEOUT_INVALID');
  if (fetchImpl !== undefined && typeof fetchImpl !== 'function') fail('JEV_UNAVAILABLE');
  if (request !== undefined && typeof request !== 'function') fail('JEV_UNAVAILABLE');
  const transport = fetchImpl ?? ((url, init) => hostedFetch(url, init, request));
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
        response = await transport(ENDPOINT, { method: 'POST', redirect: 'error', signal: controller.signal,
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
