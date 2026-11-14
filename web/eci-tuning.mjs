const labels = {baseline_protocol: 'Baseline local protocol', pipeline_configured: 'Pipeline-configured · separate protocol', published_reference: 'Published reference · different artifacts', unclassified: 'Unclassified protocol'};
export const metric = value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : 'unknown';
export const ci = row => `${metric(row.eci_posterior_mean)} [${metric(row.eci_p05)}, ${metric(row.eci_p95)}]`;
const text = value => value === null || value === undefined || value === '' ? 'unknown' : typeof value === 'object' ? JSON.stringify(value) : String(value);
const interval = value => Array.isArray(value) && value.length === 2 ? `[${metric(value[0])}, ${metric(value[1])}]` : 'unknown';
const reasons = value => Array.isArray(value) ? value.map(text).join('; ') || 'unknown' : text(value);
const routeRows = routes => Object.entries(routes || {}).map(([name, route]) => [name, text(route.model), text(route.run_id), text(route.reason || route.basis), text(route.status)]);
function componentKey(key) {
  try { const result = JSON.parse(key); return Array.isArray(result) ? result : [key]; }
  catch { return [key]; }
}
function screeningBenchmarks(row) {
  return (row.benchmarks || []).map(b => [text(row.record_id), text(row.model), text(b.benchmark), text(b.planned), text(b.attempted), text(b.completed), text(b.correct), text(b.wrong), text(b.parsed), text(b.unparsed), text(b.cut_off), text(b.truncations), text(b.errors), text(b.timeouts), text(b.unknown_finish), text(b.stop_reason), metric(b.accuracy), interval(b.interval95), metric(b.normalized_score), interval(b.normalized_interval95), text(b.retried)]);
}
function screeningPairs(row) {
  const project = (kind, peer, verdict, benchmarks) => Object.entries(benchmarks || {}).map(([name, b]) => [kind, text(row.record_id), text(peer), text(name), text(verdict), text(b.verdict), text(b.verification), text(b.reason), text(b.n_expected), text(b.n_candidate), text(b.n_baseline), text(b.n_shared), text(b.both_correct), text(b.both_wrong), text(b.model_only), text(b.baseline_only), text(b.n_discordant), text(b.p_value), text(b.alpha)]);
  return [
    ...(row.comparisons || []).flatMap(pair => project('Cohort peer', pair.record_id, pair.verdict, pair.benchmarks)),
    ...project('Bound baseline', row.baseline_id, null, row.baseline_comparison)
  ];
}

export function viewModel(data) {
  const activation = data.runtime_activation || {};
  const route = data.policy?.routes?.general;
  return {
    recommendation: routeRows(data.recommendation?.routes),
    policy: routeRows(data.policy?.routes),
    policyOrigin: text(data.policy?.origin),
    activation: [text(activation.status), text(activation.source_run_id)],
    dataset: data.dataset,
    defaultNote: route?.model ? `Recorded policy: ${route.model} · ${text(route.status)} · confidence ${text(route.confidence)}. External runtime activation NOT_APPLIED.` : route?.status === 'NOT_COMPUTED' ? 'Policy NOT_COMPUTED. External runtime activation NOT_APPLIED.' : 'Policy unknown. External runtime activation NOT_APPLIED.',
    sections: Object.entries(data.rankings || {}).filter(([key]) => key !== 'fast_check').map(([key, group]) => ({
      title: labels[key] || key,
      rows: (group.eci || []).map(row => [text(row.run_id), text(row.model), ci(row), text(row.machine), metric(row.latency_s), metric(row.cost_usd), text(row.reason)]),
      conditions: (group.eci || []).map(row => [text(row.run_id), text(row.hardware_profile_id), text(row.dataset_artifact_id), text(row.model_artifact_id), text(row.protocol_id), text(row.backend), text(row.backend_version), text(row.settings_id), text(row.settings)]),
      evidence: (group.eci || []).map(row => [text(row.run_id), text(row.item_evidence?.observed), text(row.item_evidence?.truncated), text(row.item_evidence?.truncation_unknown), text(row.item_evidence?.missing_parsed_answer)]),
      comparisons: (group.comparisons || []).map(row => [text(row.left_run_id), text(row.right_run_id), row.comparable === true ? 'Comparable' : row.comparable === false ? 'Not comparable' : 'unknown', reasons(row.reasons), row.interval_relation === 'overlap' ? 'recorded 90% intervals overlap' : row.interval_relation === 'disjoint' ? 'recorded 90% intervals are disjoint' : 'interval relation unknown', text(row.uncertainty)]),
      notes: [...(group.notes || []), `Latency unknown for ${Array.isArray(group.unknown_latency) ? group.unknown_latency.length : 'unknown'} run(s); cost unknown for ${Array.isArray(group.unknown_cost) ? group.unknown_cost.length : 'unknown'} run(s).`],
      components: Object.entries(group.components || {}).flatMap(([key, rows]) => rows.map(row => [text(componentKey(key)[0]), text(row.run_id), text(row.model), text(row.n), metric(row.score), metric(row.normalized_score), text(row.stderr), text(row.scorer_id), text(row.prompt_template_id), text(componentKey(key)[3])])),
      performance: ['latency', 'cost'].flatMap(kind => (group[kind] || []).map(row => [kind, text(row.run_id), text(row.model), metric(row[kind === 'latency' ? 'latency_s' : 'cost_usd']), text(row.status), text(row.measurement?.basis), text(row.measurement?.n_measured), text(row.measurement?.n_items)]))
    })),
    pending: (data.pending || []).map(row => `${text(row.model)}: ${text(row.status)}. ${row.reason || ''}`),
    fastChecks: (data.rankings?.fast_check?.hardware_views || []).map(group => ({
      // A screening cohort does not establish a hardware-wide winner.
      title: `Screening results${group.as_of ? ` as of ${text(group.as_of)}` : ''} · ${text(group.hardware?.machine_label || group.hardware?.id)} · fast check`,
      note: `${text(group.note)} Score bounds are means of component Wilson endpoints, not a joint 95% interval.`,
      conditions: [['Hardware profile', text(group.hardware)], ['Frozen plan', text(group.plan_id)], ['Runtime settings', text(group.runtime_settings)], ['Alpha', text(group.alpha)], ['Admission policy', text(group.admission_policy)]],
      rows: (group.rows || []).map(row => [text(row.record_id), text(row.created_at), text(row.baseline_id), text(row.model), text(row.artifact_sha256), metric(row.normalized_score), interval(row.interval), text(row.verdict), row.finalist === true ? `Screening finalist · full run ${text(row.full_run)}` : row.finalist === false ? 'Screened' : 'unknown', reasons(row.reasons)]),
      benchmarks: [...(group.rows || []), ...(group.pending || [])].flatMap(screeningBenchmarks),
      pairs: [...(group.rows || []), ...(group.pending || [])].flatMap(screeningPairs),
      notComparable: (group.not_comparable || []).map(row => [text(row.record_id), text(row.other_record_id), reasons(row.reasons)]),
      pending: (group.pending || []).map(row => `${text(row.record_id)} · ${text(row.created_at)} · ${text(row.model)}: fast check · pre ${text(row.pre)}, post ${text(row.post)}. ${text(row.reason)} ${Array.isArray(row.reasons) && row.reasons.length ? reasons(row.reasons) : ''}`)
    }))
  };
}

function element(tag, value) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  return node;
}
function table(headers, rows, caption) {
  const wrapper = element('div'); wrapper.className = 'table-scroll';
  const table = element('table'); table.append(element('caption', caption));
  const head = element('thead'), hr = element('tr');
  headers.forEach(value => {const th = element('th', value); th.scope = 'col'; hr.append(th);});
  head.append(hr); table.append(head);
  const body = element('tbody');
  rows.forEach(row => {const tr = element('tr'); row.forEach(value => tr.append(element('td', value))); body.append(tr);});
  table.append(body); wrapper.append(table); return wrapper;
}
export function render(data, root) {
  const model = viewModel(data);
  const dataset = model.dataset;
  if (dataset?.status === 'unknown') {
    root.replaceChildren(element('p', `Dataset unknown: ${dataset.reason || 'No reason supplied.'}`));
    return;
  }
  root.replaceChildren();
  if (dataset) {
    const source = dataset.status === 'configured' ? 'Source: configured dataset' : dataset.status === 'bundled' ? 'Source: bundled catalog; no dataset configured' : 'Dataset source unknown';
    root.append(element('p', `${source}; ${dataset.as_of ? `snapshot ${dataset.as_of}` : 'snapshot date unknown'}.${dataset.ingested_at ? ` Dataset ingested ${dataset.ingested_at}; this is not the evidence date.` : ''}`));
  }
  root.append(element('p', model.defaultNote));
  if (data.policy?.reason) root.append(element('p', data.policy.reason));
  const policy = element('section'); policy.append(element('h2', 'Recommendation, saved policy and activation'));
  policy.append(model.recommendation.length ? table(['Route', 'Model', 'Source run', 'Basis', 'Status'], model.recommendation, 'Recommendation') : element('p', 'Recommendation NOT_COMPUTED.'));
  policy.append(element('p', `Policy origin: ${model.policyOrigin}.`));
  policy.append(model.policy.length ? table(['Route', 'Model', 'Source run', 'Basis', 'Status'], model.policy, model.policyOrigin === 'saved' ? 'Saved policy' : 'Policy reference') : element('p', 'Saved policy unavailable.'));
  policy.append(table(['Status', 'Source run'], [model.activation], 'Activation by this tool; external router NOT_VERIFIED'));
  root.append(policy);
  if (data.cross_class_note) root.append(element('p', data.cross_class_note));
  for (const group of model.sections) {
    const section = element('section'); section.append(element('h2', group.title));
    section.append(group.rows.length ? table(['Run', 'Model', 'ECI [90% posterior interval]', 'Machine', 'Latency (s)', 'Cost (USD)', 'Run reason'], group.rows, 'Recorded ECI results; pairwise comparability below') : element('p', 'ECI NOT_COMPUTED in this class.'));
    if (group.conditions.length) section.append(table(['Run', 'Hardware profile', 'Dataset artifact', 'Model artifact', 'Protocol', 'Backend', 'Backend version', 'Settings ID', 'Settings'], group.conditions, 'Recorded full ECI conditions'));
    if (group.evidence.length) section.append(table(['Run', 'Observed items', 'Truncated', 'Truncation unknown', 'Parsed answer missing'], group.evidence, 'Recorded item coverage and deviations'));
    section.append(group.comparisons.length ? table(['Left run', 'Right run', 'Comparability', 'Reasons', 'Recorded intervals', 'Uncertainty'], group.comparisons, 'Pairwise comparability') : element('p', 'Pairwise comparison unavailable.'));
    group.notes.forEach(note => section.append(element('p', note)));
    if (group.components.length) section.append(table(['Component', 'Run', 'Model', 'Denominator', 'Raw score', 'Normalized score', 'Standard error', 'Scorer', 'Prompt template', 'Protocol'], group.components, 'Recorded components'));
    if (group.performance.length) section.append(table(['Metric', 'Run', 'Model', 'Value (s or USD)', 'Score status', 'Basis', 'Measured items', 'Observed items'], group.performance, 'Recorded cost and latency; workloads may differ'));
    root.append(section);
  }
  for (const group of model.fastChecks) {
    const section = element('section'); section.append(element('h2', group.title));
    section.append(element('p', group.note));
    section.append(table(['Condition', 'Recorded value'], group.conditions, 'Screening conditions'));
    section.append(group.rows.length ? table(['Run', 'Recorded at', 'Bound baseline', 'Model', 'Model artifact SHA-256', 'Normalized screen', 'Bounds', 'Paired verdict', 'Next gate', 'Reasons'], group.rows, 'Screening results; separate from ECI') : element('p', 'No admissible screening result.'));
    if (group.benchmarks.length) section.append(table(['Run', 'Model', 'Benchmark', 'Planned', 'Attempted', 'Completed', 'Correct', 'Wrong', 'Parsed', 'Unparsed', 'Cut off', 'Truncations', 'Errors', 'Timeouts', 'Unknown finish', 'Stop reason', 'Accuracy', 'Wilson 95% interval', 'Normalized score', 'Normalized 95% interval', 'Retried'], group.benchmarks, 'Screening coverage, deviations and component intervals'));
    if (group.pairs.length) section.append(table(['Comparison source', 'Run', 'Other run', 'Benchmark', 'Overall verdict', 'Component verdict', 'Verification', 'Reason', 'Expected', 'Candidate', 'Baseline', 'Shared', 'Both correct', 'Both wrong', 'Candidate only', 'Baseline only', 'Discordant', 'p value', 'Alpha'], group.pairs, 'Paired screening outcomes'));
    if (group.notComparable.length) section.append(table(['Run', 'Other run', 'Why not comparable'], group.notComparable, 'Not comparable'));
    group.pending.forEach(note => section.append(element('p', note))); root.append(section);
  }
  const pending = element('section'); pending.append(element('h2', 'Pending scores'));
  model.pending.forEach(note => pending.append(element('p', note))); root.append(pending);
}
if (typeof document !== 'undefined') {
  const status = document.getElementById('tuningStatus');
  let attempts = 0;
  const refresh = () => fetch('/api/eci-tuning', {cache: 'no-store'}).then(response => {
    if (!response.ok) throw new Error('unavailable');
    return response.json();
  }).then(data => {
    render(data, document.getElementById('tuningEvidence'));
    status.textContent = data.dataset?.status === 'unknown' ? 'Recorded scores unknown; see dataset reason below.' : 'Recorded scores loaded. No live inference tested.';
    if (data.dataset?.pending && ++attempts < 10) setTimeout(refresh, 1000);
    else if (data.dataset?.pending) status.textContent = 'Dataset validation continues. Reload this page to read the result; scores remain unknown.';
  })
    .catch(() => {status.textContent = 'Recorded scores unavailable. Status unknown; no model selection changed.';});
  refresh();
}
