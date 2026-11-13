const labels = {baseline_protocol: 'Epoch-comparable / baseline local protocol', pipeline_configured: 'Pipeline-configured · separate protocol', published_reference: 'Published reference · different artifacts', unclassified: 'Unclassified protocol'};
export const metric = value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : 'unknown';
export const ci = row => `${metric(row.eci_posterior_mean)} [${metric(row.eci_p05)}, ${metric(row.eci_p95)}]`;

export function viewModel(data) {
  const route = data.policy?.routes?.general;
  return {
    dataset: data.dataset,
    defaultNote: route?.model ? `Provisional PC policy: ${route.model} · confidence ${route.confidence}. External runtime activation NOT_APPLIED.` : 'PC policy NOT_COMPUTED.',
    sections: Object.entries(data.rankings || {}).filter(([key]) => key !== 'fast_check').map(([key, group]) => ({
      title: labels[key] || key,
      rows: group.eci.map(row => [row.model, ci(row), row.machine || 'unknown', metric(row.latency_s), metric(row.cost_usd)]),
      notes: [...group.notes, `Latency unknown for ${group.unknown_latency.length} run(s); cost unknown for ${group.unknown_cost.length} run(s).`],
      components: Object.entries(group.components).flatMap(([key, rows]) => rows.map(row => [JSON.parse(key)[0], row.model, metric(row.score), metric(row.normalized_score), row.scorer_id || 'unknown', row.prompt_template_id || 'unknown', JSON.parse(key)[3] || 'unknown'])),
      performance: ['latency', 'cost'].flatMap(kind => (group[kind] || []).map(row => [kind, row.model, metric(row[kind === 'latency' ? 'latency_s' : 'cost_usd']), row.status, row.measurement.basis, row.measurement.n_measured ?? 'unknown', row.measurement.n_items ?? 'unknown']))
    })),
    pending: (data.pending || []).map(row => `${row.model}: ${row.status}. ${row.reason || ''}`),
    fastChecks: (data.rankings?.fast_check?.hardware_views || []).map(group => ({
      title: `Fast screening · ${group.hardware.machine_label || group.hardware.id || 'unknown hardware'}`,
      note: `${group.note} Plan ${group.plan_id}. Score bounds are means of component Wilson endpoints, not a joint 95% interval.`,
      rows: group.rows.map(row => [row.label, row.model, `${metric(row.normalized_score)} [${row.interval.map(metric).join(', ')}]`, row.tie_with.length ? `Tie · INCONCLUSIVE with ${row.tie_with.length} run(s)` : row.verdict, row.finalist ? 'Finalist · full run NOT_RUN' : 'Screened']),
      pending: group.pending.map(row => `${row.model}: fast check · pre ${row.pre}, post ${row.post}. ${row.reason}`)
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
  if (dataset) root.append(element('p', dataset.status === 'configured' ? 'Source: configured dataset.' : dataset.status === 'bundled' ? 'Source: bundled catalog; no dataset configured.' : 'Dataset source unknown.'));
  root.append(element('p', model.defaultNote));
  root.append(element('p', 'Recommendations for general, reasoning and coding PC-eligible work use baseline ECI provisionally. Task-specific routing outcomes NOT_MEASURED. Nisi Mac NOT_APPLIED; two distinct resident Mac LLMs are required.'));
  root.append(element('p', data.cross_class_note));
  for (const group of model.sections) {
    const section = element('section'); section.append(element('h2', group.title));
    section.append(group.rows.length ? table(['Model', 'ECI [90% CI]', 'Machine', 'Latency (s)', 'Cost (USD)'], group.rows, 'ECI ranking within this comparability class') : element('p', 'ECI NOT_COMPUTED in this class.'));
    group.notes.forEach(note => section.append(element('p', note)));
    if (group.components.length) section.append(table(['Component', 'Model', 'Raw score', 'Normalized score', 'Scorer', 'Prompt template', 'Protocol'], group.components, 'Admitted component rankings grouped by scorer, prompt template and protocol'));
    if (group.performance.length) section.append(table(['Metric', 'Model', 'Value (s or USD)', 'Score status', 'Basis', 'Measured items', 'Observed items'], group.performance, 'Recorded cost and latency rankings; workloads may differ'));
    root.append(section);
  }
  for (const group of model.fastChecks) {
    const section = element('section'); section.append(element('h2', group.title));
    section.append(element('p', group.note));
    section.append(group.rows.length ? table(['Protocol', 'Model', 'Normalized screen [bounds]', 'Paired verdict', 'Next gate'], group.rows, 'Fast-check ranking for this hardware and frozen plan; separate from ECI') : element('p', 'No admissible fast check.'));
    group.pending.forEach(note => section.append(element('p', note))); root.append(section);
  }
  const pending = element('section'); pending.append(element('h2', 'Pending scores'));
  model.pending.forEach(note => pending.append(element('p', note))); root.append(pending);
}
if (typeof document !== 'undefined') {
  const status = document.getElementById('tuningStatus');
  fetch('/api/eci-tuning', {cache: 'no-store'}).then(response => {
    if (!response.ok) throw new Error('unavailable');
    return response.json();
  }).then(data => {render(data, document.getElementById('tuningEvidence')); status.textContent = data.dataset?.status === 'unknown' ? 'Recorded scores unknown; see dataset reason below.' : 'Recorded scores loaded. No live inference tested.';})
    .catch(() => {status.textContent = 'Recorded scores unavailable. Status unknown; no model selection changed.';});
}
