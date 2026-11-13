export const CLOCK_SKEW_SECONDS = 1; // shared with the run-evidence views
/** Read the server's fleet contract. Never reconstruct membership from legacy feeds. */
const STATES = new Set(['endpoint answering', 'endpoint unavailable', 'unknown', 'stale', 'reported']);
const numeric = value => typeof value === 'number' && Number.isFinite(value);
const stamp = value => typeof value === 'string' && /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) && Number.isFinite(Date.parse(value)) ? Date.parse(value) / 1000 : null;
const list = value => Array.isArray(value) ? value : [];

export function fleetState(input, {now = Date.now() / 1000} = {}) {
  const contract = input?.fleetState ?? (input?.schemaVersion === 1 && Array.isArray(input?.facts) ? input : null);
  if (!contract || contract.schemaVersion !== 1) return null;
  const seen = new Set();
  const facts = list(contract.facts).filter(fact => fact && typeof fact.id === 'string' && fact.id &&
    ['machine', 'lane'].includes(fact.scope) && !seen.has(fact.id) && seen.add(fact.id)).map(fact => {
    const at = stamp(fact.observedAt), expiry = stamp(fact.expiresAt);
    const ageSeconds = at !== null && numeric(now) && now >= at - 2 ? Math.max(0, now - at) : null;
    const stale = at !== null && expiry !== null && expiry >= at && numeric(now) && now >= at - 2 && now > expiry;
    const unavailable = at === null || expiry === null || expiry < at || ageSeconds === null;
    const evidence = fact.evidence && typeof fact.evidence === 'object' ? {...fact.evidence} : {};
    if (stale || unavailable) delete evidence.endpointAnswered;
    return {...fact, evidence, ageSeconds,
      state: stale ? 'stale' : unavailable || !STATES.has(fact.state) ? 'unknown' : fact.state,
      completeness: stale || unavailable ? 'unknown' : fact.completeness,
      reason: stale ? 'stale' : unavailable ? (at !== null && numeric(now) && now < at - 2 ? 'clock_skew' : fact.reason || 'observation_missing') : fact.reason};
  });
  return {...contract, facts};
}
export function fleetFacts(input, options) { return fleetState(input, options)?.facts || []; }
export function fleetMachines(input, options) { return fleetFacts(input, options).filter(fact => fact.scope === 'machine'); }
export function fleetLanes(input, options) { return fleetFacts(input, options).filter(fact => fact.scope === 'lane'); }
export function fleetAge(fact) { return numeric(fact?.ageSeconds) ? `${Math.round(fact.ageSeconds)} s ago` : 'Age unknown'; }
export function fleetWord(fact) { return fact?.state === 'stale' ? `stale · ${fleetAge(fact)}` :
  fact?.state === 'unknown' && fact.evidence?.endpointAnswered === true ? 'unknown · answered, identity unknown' :
  STATES.has(fact?.state) ? fact.state : 'unknown'; }
export function fleetSummary(input, options) {
  const state = fleetState(input, options), facts = state?.facts || [], lanes = facts.filter(fact => fact.scope === 'lane');
  const machines = facts.filter(fact => fact.scope === 'machine');
  const registry = state?.registry;
  const now = options?.now ?? Date.now() / 1000;
  const registryAt = stamp(registry?.observedAt), registryExpiry = stamp(registry?.expiresAt);
  const registryPartial = registry && (registry.completeness !== 'complete' || registry.reason !== 'registry_configured' ||
    registryAt === null || registryExpiry === null || registryExpiry < registryAt ||
    !numeric(now) || now > registryExpiry || now < registryAt - 2);
  const prefix = registryPartial ? `Partial coverage; ${registry.reason === 'registry_invalid' ? 'registry invalid' : 'registry unknown'}; ` : '';
  const counts = {machines: machines.length, lanes: lanes.length,
    answering: lanes.filter(fact => fact.state === 'endpoint answering').length,
    unavailable: lanes.filter(fact => fact.state === 'endpoint unavailable').length,
    stale: lanes.filter(fact => fact.state === 'stale').length,
    unknown: lanes.filter(fact => fact.state === 'unknown').length, loaded: null, busy: null};
  if (state && !registryPartial && machines.length && machines.every(fact => fact.state === 'reported' && fact.completeness === 'complete' && fact.evidence.state === 'up' && fact.evidence.modelsComplete === true)) {
    for (const [key, field] of [['loaded', 'loadedCount'], ['busy', 'busyCount']]) {
      if (machines.every(fact => Number.isSafeInteger(fact.evidence[field]) && fact.evidence[field] >= 0))
        {const total = machines.reduce((sum, fact) => sum + fact.evidence[field], 0); counts[key] = Number.isSafeInteger(total) ? total : null;}
    }
  }
  if (counts.answering === 0 && (!lanes.length || counts.unknown || counts.stale || registryPartial)) counts.answering = null;
  const completeness = lanes.length && !counts.unknown && !counts.stale && !registryPartial ? 'complete' : 'partial';
  if (!state || !lanes.length) return {headline: prefix + 'Endpoint coverage unknown', text: prefix + 'Endpoint coverage unknown; no lane observations.', state: 'unknown', counts, completeness};
  const notes = [];
  for (const [word, count] of [['unavailable', counts.unavailable], ['stale', counts.stale], ['unknown', counts.unknown]]) {
    if (count) notes.push(`${count} ${word}: ${lanes.filter(fact => fact.state === (word === 'unavailable' ? 'endpoint unavailable' : word)).map(fact => `${fact.label || fact.id} (${fact.id})${word === 'stale' ? `, ${fleetAge(fact)}` : word === 'unknown' && fact.evidence.endpointAnswered === true ? ', answered, identity unknown' : ''}`).join(', ')}`);
  }
  const headline = prefix + (counts.answering === null ? `Answering count unknown; ${counts.lanes} lanes configured` : `${counts.answering} of ${counts.lanes} lanes answering`);
  return {headline, text: `${headline}${notes.length ? '; ' + notes.join('; ') : ''}`, state: 'coverage', counts, completeness};
}

/** Machine headlines describe lane coverage, separately from unmeasured host health. */
export function fleetMachineCoverage(fact, lanes = []) {
  const children = list(lanes).filter(lane => lane.parentId === fact?.id);
  const counts = {lanes: children.length,
    answering: children.filter(lane => lane.state === 'endpoint answering').length,
    unavailable: children.filter(lane => lane.state === 'endpoint unavailable').length,
    stale: children.filter(lane => lane.state === 'stale').length,
    unknown: children.filter(lane => lane.state === 'unknown').length};
  const noun = counts.lanes === 1 ? 'lane' : 'lanes';
  const answeredUnknown = children.filter(lane => lane.state === 'unknown' && lane.evidence?.endpointAnswered === true).length;
  const observed = children.some(lane => lane.state !== 'unknown' || numeric(lane.ageSeconds));
  let text = 'UNKNOWN', state = 'unknown', glyph = '?', tone = 'muted';
  if (children.length && (counts.answering > 0 || counts.unavailable === children.length)) {
    text = `${counts.answering} of ${counts.lanes} ${noun} answering`;
    state = counts.answering === children.length ? 'ok' : counts.unavailable === children.length ? 'down' : 'partial';
    glyph = counts.answering > 0 ? '■' : '×';
  } else if (counts.stale) {
    text = `${counts.lanes} ${noun} · evidence stale`; state = 'stale'; glyph = '~';
  } else if (answeredUnknown) {
    text = `${answeredUnknown === counts.lanes ? answeredUnknown : `${answeredUnknown} of ${counts.lanes}`} ${noun} answered · identity unknown`;
    state = 'partial'; glyph = '?';
  } else if (observed || fact?.state === 'reported') {
    text = children.length ? `${counts.lanes} ${noun} · endpoint evidence pending` : 'peer report observed';
    state = 'partial'; glyph = '?';
  } else if (fact?.state === 'stale') {
    text = 'peer report stale'; state = 'stale'; glyph = '~';
  }
  const details = ['Host health not measured.'];
  for (const [key, label] of [['unavailable', 'endpoint unavailable'], ['stale', 'stale'], ['unknown', 'unobserved']])
    if (counts[key]) details.push(`${counts[key]} ${counts[key] === 1 ? 'lane' : 'lanes'} ${label}.`);
  return {text, word: text, state, glyph, tone, detail: details.join(' '), counts};
}
