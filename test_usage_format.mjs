import assert from 'node:assert/strict';
import test from 'node:test';
import * as usageFormat from './usage-format.mjs';

const { formatTokens, summarizeReportedCalls } = usageFormat;

test('exports the named telemetry formatter and reported-call summary', () => {
  assert.deepEqual(Object.keys(usageFormat), ['formatTokens', 'summarizeReportedCalls']);
  assert.equal(typeof formatTokens, 'function');
});

const call = (id, inputTokens, outputTokens, elapsedMs) => ({
  id, elapsedMs, usage: { inputTokens, outputTokens, totalTokens: inputTokens + outputTokens },
});

test('reported-call summary sums complete recorded calls without mutating them', () => {
  const calls = Object.freeze([
    Object.freeze(call('author', 654, 244, 4854)),
    Object.freeze(call('reviewer', 1310, 187, 3968)),
    Object.freeze(call(null, 1342, 213, 457)),
  ]);
  assert.deepEqual(summarizeReportedCalls(calls), {
    callCount: 3, tokenKnownCount: 3, timedCallCount: 3,
    duplicateIdentity: false, totalTokens: 3950, elapsedMs: 9279,
  });
});

test('missing calls or missing metrics do not produce a partial total or a zero', () => {
  for (const value of [undefined, null, {}, []]) {
    const result = summarizeReportedCalls(value);
    assert.equal(result.callCount, 0);
    assert.equal(result.totalTokens, null);
    assert.equal(result.elapsedMs, null);
  }
  const result = summarizeReportedCalls([call('a', 1, 2, 4), {id: 'b'}]);
  assert.equal(result.tokenKnownCount, 1);
  assert.equal(result.timedCallCount, 1);
  assert.equal(result.totalTokens, null);
  assert.equal(result.elapsedMs, null);
});

test('zero telemetry remains a known zero and time is independent of token availability', () => {
  assert.equal(summarizeReportedCalls([call('a', 0, 0, 0)]).totalTokens, 0);
  assert.equal(summarizeReportedCalls([call('a', 0, 0, 0)]).elapsedMs, 0);
  assert.equal(summarizeReportedCalls([{elapsedMs: 4}]).elapsedMs, 4);
  assert.equal(summarizeReportedCalls([call('a', 1, 2, null)]).totalTokens, 3);
});

test('invalid components and inconsistent reported totals cannot be aggregated', () => {
  for (const bad of [true, '1', -1, 1.5, Infinity, NaN, Number.MAX_SAFE_INTEGER + 1]) {
    for (const field of ['inputTokens', 'outputTokens', 'totalTokens']) {
      const row = call('a', 1, 2, 3);
      row.usage[field] = bad;
      assert.equal(summarizeReportedCalls([row]).totalTokens, null);
    }
    const row = call('a', 1, 2, bad);
    assert.equal(summarizeReportedCalls([row]).elapsedMs, null);
  }
  const inconsistent = call('a', 1, 2, 3);
  inconsistent.usage.totalTokens = 8;
  assert.equal(summarizeReportedCalls([inconsistent]).totalTokens, null);
});

test('safe individual values cannot overflow a displayed aggregate', () => {
  const rows = [call('a', Number.MAX_SAFE_INTEGER, 0, Number.MAX_SAFE_INTEGER), call('b', 1, 0, 1)];
  const result = summarizeReportedCalls(rows);
  assert.equal(result.tokenKnownCount, 2);
  assert.equal(result.totalTokens, null);
  assert.equal(result.elapsedMs, null);
});

test('duplicate call identities cannot double count usage or duration', () => {
  const row = call('same', 1, 2, 3);
  const result = summarizeReportedCalls([row, {...row}]);
  assert.equal(result.duplicateIdentity, true);
  assert.equal(result.totalTokens, null);
  assert.equal(result.elapsedMs, null);
});

test('uses en-US grouping for valid primitive integer token counts', () => {
  for (const [value, expected] of [
    [1, '1'],
    [12, '12'],
    [999, '999'],
    [1000, '1,000'],
    [1234567, '1,234,567'],
    [1000000000, '1,000,000,000'],
    [Number.MAX_SAFE_INTEGER, '9,007,199,254,740,991'],
  ]) {
    assert.equal(formatTokens(value), expected, `valid count ${value}`);
  }
});

test('zero and negative zero both remain known zero', () => {
  assert.equal(formatTokens(0), '0');
  assert.equal(formatTokens(-0), '0');
});

test('missing, undefined, and null stay Unknown', () => {
  assert.equal(formatTokens(), 'Unknown');
  assert.equal(formatTokens(undefined), 'Unknown');
  assert.equal(formatTokens(null), 'Unknown');
});

test('negative, fractional, nonfinite, and unsafe numbers stay Unknown', () => {
  for (const value of [
    -1,
    -1000,
    Number.MIN_SAFE_INTEGER,
    -0.5,
    0.5,
    1234.5,
    Number.MIN_VALUE,
    NaN,
    Infinity,
    -Infinity,
    Number.MAX_SAFE_INTEGER + 1,
    Number.MAX_VALUE,
  ]) {
    assert.equal(formatTokens(value), 'Unknown', `invalid count ${value}`);
  }
});

test('nonnumbers are never coerced into known token counts', () => {
  const invalid = [
    '',
    '0',
    '1000',
    '1,000',
    'NaN',
    true,
    false,
    0n,
    1000n,
    Symbol('tokens'),
    [],
    [1000],
    {},
    new Number(1000),
    () => 1000,
  ];
  for (const value of invalid) {
    assert.equal(formatTokens(value), 'Unknown');
  }
});

test('rejects objects without invoking conversion hooks or mutating inputs', () => {
  const calls = [];
  const value = Object.freeze({
    count: 1000,
    valueOf() { calls.push('valueOf'); throw new Error('No numeric coercion permitted'); },
    toString() { calls.push('toString'); throw new Error('No string coercion permitted'); },
    [Symbol.toPrimitive]() { calls.push('toPrimitive'); throw new Error('No coercion permitted'); },
  });
  assert.equal(formatTokens(value), 'Unknown');
  assert.deepEqual(calls, []);
  assert.equal(value.count, 1000);
});

test('repeated mixed calls do not retain or substitute a prior known value', () => {
  assert.deepEqual([formatTokens(1000), formatTokens(null), formatTokens(0),
    formatTokens('1000'), formatTokens(1000)], ['1,000', 'Unknown', '0', 'Unknown', '1,000']);
});
