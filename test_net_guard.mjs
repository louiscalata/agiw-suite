// net-guard.mjs: only the configured 127.0.0.1:port may be reached from a pilot program.
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {installNetGuard} from './preview-pilot/net-guard.mjs';

const listen = () => new Promise(resolve => { const s = http.createServer((q, r) => r.end('ok')).listen(0, '127.0.0.1', () => resolve(s)); });
const get = (host, port) => new Promise(resolve => {
  const req = http.request({hostname: host, port, path: '/', method: 'GET'}, res => { res.resume(); res.on('end', () => resolve('ok')); });
  req.on('error', e => resolve(e.code || e.message));
  req.end();
});

const getPath = (host, port, path) => new Promise(resolve => {
  const req = http.request({hostname: host, port, path, method: 'GET'}, res => { res.resume(); res.on('end', () => resolve('ok')); });
  req.on('error', e => resolve(e.code || e.message));
  req.end();
});

test('rejects non-loopback endpoints', () => {
  for (const bad of ['http://localhost:1237/v1/chat/completions', 'http://10.0.0.2:1237/x', 'https://127.0.0.1:1237/x', 'http://127.0.0.1/x']) {
    assert.throws(() => installNetGuard(bad), /NET_GUARD_ENDPOINT_REFUSED/);
  }
});

test('allows only the configured port on 127.0.0.1', async () => {
  const allowed = await listen(), other = await listen();
  try {
    const port = allowed.address().port;
    assert.equal(installNetGuard(`http://127.0.0.1:${port}/v1/chat/completions`), port);
    assert.equal(await get('127.0.0.1', port), 'ok');
    // completion-grammar's sibling routes share the origin, so they pass; other ports still do not.
    const {completionURLs} = await import('./preview-pilot/model-call.mjs');
    const urls = completionURLs(`http://127.0.0.1:${port}/v1/chat/completions`);
    assert.equal(urls.port, port);
    for (const path of [urls.applyTemplate, urls.completion]) assert.equal(await getPath('127.0.0.1', port, path), 'ok');
    assert.equal(await getPath('127.0.0.1', other.address().port, urls.completion), 'NET_GUARD_REFUSED');
    assert.equal(await get('127.0.0.1', other.address().port), 'NET_GUARD_REFUSED');
    assert.equal(await get('localhost', port), 'NET_GUARD_REFUSED');
    assert.throws(() => installNetGuard(`http://127.0.0.1:${other.address().port}/v1/chat/completions`), /NET_GUARD_ALREADY_INSTALLED/);
  } finally { allowed.close(); other.close(); }
});
