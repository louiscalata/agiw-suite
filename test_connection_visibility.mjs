import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const html = readFileSync(new URL('./web/index.html', import.meta.url), 'utf8');

test('the single live connection status is visible when disclosures are closed', () => {
  const details = [];
  let connections = 0;
  const tags = /<\/?(?:details|summary)\b[^>]*>|<[a-z][^>]*\bid\s*=\s*["']connection["'][^>]*>/gi;
  for (const [tag] of html.matchAll(tags)) {
    if (/^<details\b/i.test(tag)) {
      details.push({ open: /\sopen(?:\s|=|>)/i.test(tag), inSummary: false });
    } else if (/^<\/details\b/i.test(tag)) {
      details.pop();
    } else if (/^<\/?summary\b/i.test(tag) && details.length) {
      details.at(-1).inSummary = !/^<\//.test(tag);
    }
    if (/\bid\s*=\s*["']connection["']/i.test(tag)) {
      connections++;
      assert.ok(details.every(ancestor => ancestor.open || ancestor.inSummary),
        '#connection must be outside closed details or inside each closed ancestor\'s summary');
    }
  }
  assert.equal(connections, 1, 'keep exactly one live #connection status');
});
