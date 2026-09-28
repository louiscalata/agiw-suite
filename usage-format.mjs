/** Unknown telemetry must never become a fabricated zero. */
export function formatTokens(value) {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? (value === 0 ? 0 : value).toLocaleString('en-US')
    : 'Unknown';
}

/** Sums only the supplied recorded calls, never whole-run or billing usage. */
export function summarizeReportedCalls(value) {
  const calls = Array.isArray(value) ? value : [];
  const known = n => typeof n === 'number' && Number.isSafeInteger(n) && n >= 0;
  let tokens = 0, elapsed = 0, tokenKnownCount = 0, timedCallCount = 0;
  let duplicateIdentity = false;
  const identities = new Set();
  for (const call of calls) {
    if (typeof call?.id === 'string' && call.id.length > 0) {
      if (identities.has(call.id)) duplicateIdentity = true;
      identities.add(call.id);
    }
    const usage = call?.usage;
    if (known(usage?.inputTokens) && known(usage?.outputTokens)
        && known(usage?.totalTokens)
        && usage.inputTokens + usage.outputTokens === usage.totalTokens) {
      tokenKnownCount++;
      tokens += usage.totalTokens;
    }
    if (known(call?.elapsedMs)) {
      timedCallCount++;
      elapsed += call.elapsedMs;
    }
  }
  return {
    callCount: calls.length, tokenKnownCount, timedCallCount, duplicateIdentity,
    totalTokens: calls.length > 0 && !duplicateIdentity
      && tokenKnownCount === calls.length && known(tokens) ? tokens : null,
    elapsedMs: calls.length > 0 && !duplicateIdentity
      && timedCallCount === calls.length && known(elapsed) ? elapsed : null,
  };
}
