import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const swift = read('./Monitor.swift');
const gate = swift.slice(swift.indexOf('let documentation = Set('), swift.indexOf('// Only the explicit documentation links above'));
const allowed = new Set([...gate.slice(0, gate.indexOf('])')).matchAll(/"([^"]+)"/g)].map(m => m[1]));

function offered() {
  const urls = new Set();
  for (const name of readdirSync(new URL('./web/', import.meta.url)).filter(file => /\.(html|mjs|js)$/.test(file))) {
    for (const m of read(`./web/${name}`).matchAll(/href="(https?:\/\/[^"]+)"/g)) urls.add(m[1]);
  }
  return urls;
}

test('the links a shipped page offers are exactly the links the Mac app opens', () => {
  const pages = offered();
  assert.ok(allowed.size >= 5 && pages.size >= 5, `${allowed.size} allowed, ${pages.size} offered`);
  for (const url of pages) assert.ok(allowed.has(url), `${url} is offered by a page but the app would not open it`);
  for (const url of allowed) assert.ok(pages.has(url), `${url} is opened by the app but no page offers it`);
});

test('the app opens only https documentation links, never http or a pattern', () => {
  for (const url of allowed) assert.match(url, /^https:\/\/[a-z0-9.-]+\/[^\s"]*$/, url);
  assert.doesNotMatch(gate, /hasPrefix|hasSuffix|range\(of|contains\(where|\*|\.host ==/);
});

test('the gate opens a link only when the person clicked it and it is an exact member of that set', () => {
  assert.match(gate, /navigationAction\.navigationType == \.linkActivated/);
  assert.match(gate, /documentation\.contains\(url\.absoluteString\)/);
  const opens = [...swift.matchAll(/NSWorkspace\.shared\.open\(/g)].length;
  const inGate = [...gate.matchAll(/NSWorkspace\.shared\.open\(url\)\s*\n\s*decisionHandler\(\.cancel\)/g)].length;
  assert.equal(inGate, 1, 'the open is followed by cancelling the in-app navigation');
  assert.ok(opens >= 1);
  // Nothing else in the navigation delegate lets a main-frame navigation leave the monitor's own origin.
  assert.match(swift, /allowedOrigin\(navigationAction\.request\.url\) \? \.allow : \.cancel/);
});
