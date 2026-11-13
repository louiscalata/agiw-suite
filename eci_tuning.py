"""AGIW x ECI Model Tuning Tool. Offline data/policy operations; no model calls.

Source checkout CLI: python3 -B -m eci_tuning --help
Contract and adapters: docs/eci-model-tuning.md and eci_tuning.schema.json.
"""
from __future__ import annotations

import argparse
import copy
import csv
import difflib
import json
import math
import os
from pathlib import Path
import sys
import tempfile

import eci_fast_check as fast

ROOT = Path(__file__).resolve().parent
CATALOG = ROOT / 'eci_tuning_catalog.json'
DEFAULT_POLICY = ROOT / 'eci_tuning_routes.json'
CLASSES = ('baseline_protocol', 'pipeline_configured', 'published_reference', 'unclassified')
STATUSES = ('COMPUTED', 'NOT_COMPUTED', 'NOT_RUN', 'FAILED')
SCORERS = ('strict_choice', 'lenient_v1', 'gsm_numeric_em')
SETTINGS = ('temperature', 'max_tokens', 'ctx', 'n_gpu_layers', 'reasoning_budget',
            'stop', 'system_message', 'n_epochs', 'prompt_template_id', 'scorer_id')
IDENTITY = ('quant', 'backend', 'backend_version', 'machine')
METRICS = ('latency_s', 'tokens_per_s', 'cost_usd', 'memory_bytes', 'peak_memory_bytes', 'gpu_memory_bytes')
PROTOCOL_ALIASES = ('protocol_id', 'protocol')
PROTOCOL_FIELDS = (*PROTOCOL_ALIASES, 'kind')


class ValidationError(ValueError):
    pass


def number(value, field, minimum=None, maximum=None):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValidationError(f'{field} must be a finite number or null')
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValidationError(f'{field} must be a finite number or null') from None
    if not math.isfinite(value) or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValidationError(f'{field} outside allowed range')
    return value


def text(value, field, required=False):
    if value is None or value == '':
        if required:
            raise ValidationError(f'{field} is required')
        return None
    if not isinstance(value, str):
        raise ValidationError(f'{field} must be text')
    return value


def decode_cell(value):
    if not isinstance(value, str):
        return value
    if value == '' or value.lower() in ('null', 'none'):
        return None
    if value.lower() in ('true', 'false'):
        return value.lower() == 'true'
    try:
        return json.loads(value)
    except ValueError:
        return value


def load(path):
    path = Path(path)
    if path.suffix == '.csv':
        with path.open(newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is not None and len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValidationError('duplicate CSV column')
            return [{key: decode_cell(value) for key, value in row.items()} for row in reader]
    if path.suffix == '.jsonl':
        return [json.loads(line, object_pairs_hook=fast.unique_object)
                for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=fast.unique_object)


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get('status') not in STATUSES:
        raise ValidationError('invalid manifest status')
    ci_level = (manifest.get('sampling') or {}).get('ci_level')
    if ci_level is not None and ci_level != .9:
        raise ValidationError('manifest CI level is not 90%')


def comparability(row, hint=None):
    # Lenient extraction can never be admitted into the baseline class by a flag.
    settings = row.get('settings') or {}
    marker = ' '.join(str(row.get(key, '')) for key in ('model_id', 'model', 'scoring', 'protocol', 'scorer_id'))
    marker += ' ' + str(settings.get('scorer_id', ''))
    configuration = str(row.get('configuration_label') or '')
    fallback = ('pipeline_configured' if configuration.startswith('pipeline-configured') else
                'baseline_protocol' if configuration == 'baseline' else hint)
    label = row.get('comparability_class', row.get('comparability', fallback)) or 'unclassified'
    label = {'epoch_comparable': 'baseline_protocol', 'epoch-comparable': 'baseline_protocol',
             'pipeline-configured': 'pipeline_configured'}.get(label, label)
    if any(word in marker.lower() for word in ('lenient', 'pipecfg', 'pipeline-configured')):
        if label == 'baseline_protocol':
            raise ValidationError('lenient scoring cannot be baseline_protocol')
        label = 'pipeline_configured'
    if label not in CLASSES:
        raise ValidationError('unknown comparability_class')
    return label


def reject_fast_marker(value):
    if isinstance(value, str) and any(marker in value.strip().lower()
                                      for marker in (fast.PROTOCOL, 'agiw_fast_check')):
        raise ValidationError('fast checks belong only in fast_checks, never ECI runs or items')


def eci_protocol(row):
    """Validate provenance before a row can lose aliases during normalization.

    kind is an input record tag; protocol and protocol_id are aliases for the
    same provenance. Check every supplied value even when another alias wins.
    """
    if not isinstance(row, dict):
        raise ValidationError('ECI row must be an object')
    for key in PROTOCOL_FIELDS:
        reject_fast_marker(text(row.get(key), key))
    protocols = [text(row.get(key), key) for key in PROTOCOL_ALIASES]
    known = {value for value in protocols if value is not None}
    if len(known) > 1:
        raise ValidationError('conflicting protocol aliases')
    return next(iter(known), None)


def validate_eci_provenance(value, inherited_protocol=None, dedicated_fast_checks=False):
    """Inspect every supplied level before any adapter can discard metadata.

    Only the root normalized dataset's fast_checks collection is a separate
    protocol domain. A nested field with that name does not create an escape.
    Protocol aliases at a child must agree with a supplied ancestor protocol.
    Marker strings in arbitrary values, list entries and mapping keys are also
    rejected, including metadata that a normalization adapter would discard.
    """
    if isinstance(value, dict):
        protocol = eci_protocol(value)
        if inherited_protocol is not None and protocol is not None and protocol != inherited_protocol:
            raise ValidationError('conflicting parent/child protocol fields')
        inherited_protocol = protocol or inherited_protocol
        for key, child in value.items():
            reject_fast_marker(key)
            if (dedicated_fast_checks and key == 'fast_checks'
                    and value.get('schema_version') == 1 and 'runs' in value and 'items' in value):
                if not isinstance(child, list):
                    raise ValidationError('fast_checks must be a collection')
                continue
            validate_eci_provenance(child, inherited_protocol)
    elif isinstance(value, list):
        for child in value:
            validate_eci_provenance(child, inherited_protocol)
    else:
        reject_fast_marker(value)


def normalize_run(row, hint=None, run_id=None):
    if not isinstance(row, dict):
        raise ValidationError('run must be an object')
    validate_eci_provenance(row)
    protocol_id = eci_protocol(row)
    result = {'run_id': text(row.get('run_id') or run_id, 'run_id', True),
              'model': text(row.get('model') or row.get('model_id'), 'model', True),
              'status': {'OK': 'NOT_COMPUTED', 'PARTIAL': 'NOT_COMPUTED'}.get(row.get('status'), row.get('status')) or 'NOT_COMPUTED',
              'comparability_class': comparability(row, hint),
              'protocol_id': protocol_id,
              'reason': text(row.get('reason'), 'reason')}
    result['source_status'] = text(row.get('source_status', row.get('status')), 'source_status')
    result['partial'] = row.get('partial', False)
    if not isinstance(result['partial'], bool):
        raise ValidationError('partial must be boolean')
    result['admitted_score'] = number(row.get('admitted_score'), 'admitted_score', 0, 1)
    if result['status'] not in STATUSES:
        raise ValidationError('unknown run status')
    for key in IDENTITY:
        result[key] = text(row.get(key, row.get('host') if key == 'machine' else None), key)
    if result['machine'] is not None:
        result['machine'] = result['machine'].lower()
    source_settings = row.get('settings') or {}
    if not isinstance(source_settings, dict):
        raise ValidationError('settings must be an object')
    result['settings'] = {key: row.get(key, source_settings.get(key)) for key in SETTINGS}
    scorer = result['settings']['scorer_id']
    if scorer is not None and scorer not in SCORERS:
        raise ValidationError('unknown scorer_id')
    for key in ('temperature', 'max_tokens', 'ctx', 'n_gpu_layers', 'n_epochs'):
        value = result['settings'][key]
        if value is not None:
            result['settings'][key] = number(value, key, -1 if key == 'n_gpu_layers' else 0)
    for key in ('prompt_template_id', 'system_message'):
        text(result['settings'][key], key)
    stop = result['settings']['stop']
    if stop is not None and not (isinstance(stop, str) or isinstance(stop, list) and all(isinstance(v, str) for v in stop)):
        raise ValidationError('stop must be text, a list of text, or null')
    for key in METRICS:
        alias = {'memory_bytes': 'mem_bytes', 'gpu_memory_bytes': 'gpu_mem_bytes'}.get(key, key)
        result[key] = number(row.get(key, row.get(alias)), key, 0)
    result['benchmark'] = text(row.get('benchmark'), 'benchmark')
    # The parallel analysis table associates full refits with component runs.
    # These are retained as associations, never promoted to component-run ECI.
    result['eci_associations'] = []
    for association in row.get('eci_associations') or []:
        result['eci_associations'].append({'fit_id': text(association.get('fit_id'), 'fit_id', True),
            **{key: number(association.get(key), key) for key in ('eci', 'eci_p05', 'eci_p95')}})
    result['aggregate_score'] = number(row.get('aggregate_score', row.get('score')), 'aggregate_score', 0, 1)
    result['n'] = number(row.get('n', row.get('n_items')), 'n', 0)
    result['stderr'] = number(row.get('stderr'), 'stderr', 0)
    for key in ('eci_posterior_mean', 'eci_p05', 'eci_p95'):
        value = row.get(key)
        if 'eci' in row and 'eci_posterior_mean' not in row:
            value = None  # analysis CSV bounds belong to its full-fit associations
        result[key] = number(value, key)
    values = [result[key] for key in ('eci_p05', 'eci_posterior_mean', 'eci_p95')]
    if any(v is not None for v in values):
        if not all(v is not None for v in values) or values != sorted(values) or result['status'] != 'COMPUTED':
            raise ValidationError('ECI requires COMPUTED status and ordered mean/90% CI')
        if result['partial'] or result['source_status'] in ('FAILED', 'PARTIAL', 'NOT_RUN', 'NOT_COMPUTED'):
            raise ValidationError('failed, partial or pending source cannot admit computed ECI')
    diagnostics = row.get('diagnostics') or {}
    if not isinstance(diagnostics, dict):
        raise ValidationError('diagnostics must be an object')
    result['diagnostics'] = {'status': row.get('diagnostic_status', diagnostics.get('status'))}
    for key in ('divergences', 'max_rhat', 'min_bulk_ess'):
        result['diagnostics'][key] = number(row.get(key, diagnostics.get(key)), key, 0)
    result['components'] = {}
    components = row.get('components') or {}
    raw = row.get('component_raw_scores') or {}
    normalized = row.get('normalized_scores') or {}
    if not all(isinstance(obj, dict) for obj in (components, raw, normalized)):
        raise ValidationError('component score maps must be objects')
    for name in components.keys() | raw.keys() | normalized.keys():
        component = components.get(name, {})
        if not isinstance(component, dict):
            component = {'score': component}
        if 'admitted' in component and not isinstance(component['admitted'], bool):
            raise ValidationError('component admitted must be boolean')
        if component.get('scorer_id') is not None and component['scorer_id'] not in SCORERS:
            raise ValidationError('unknown component scorer_id')
        result['components'][name] = {
            'score': number(component.get('score', raw.get(name)), 'component score', 0, 1),
            'normalized_score': number(component.get('normalized_score', normalized.get(name)), 'normalized score'),
            'n': number(component.get('n'), 'component n', 0),
            'stderr': number(component.get('stderr'), 'component stderr', 0),
            'scorer_id': component.get('scorer_id', scorer),
            'prompt_template_id': component.get('prompt_template_id', result['settings']['prompt_template_id']),
            'admitted': bool(component.get('admitted', row.get('status') == 'COMPUTED' or component.get('status') == 'OK'))
                        and row.get('status') not in ('FAILED', 'NOT_RUN', 'PARTIAL') and not row.get('partial', False)}
    if result['benchmark'] and result['aggregate_score'] is not None:
        result['components'][result['benchmark']] = {
            'score': result['aggregate_score'], 'normalized_score': None, 'n': result['n'], 'stderr': result['stderr'],
            'scorer_id': scorer, 'prompt_template_id': result['settings']['prompt_template_id'],
            'admitted': result['source_status'] == 'OK' and result['admitted_score'] is not None and not result['partial']}
    return result


def normalize_item(row):
    run = normalize_run(dict(row, status='NOT_COMPUTED'))
    item = {key: run[key] for key in ('run_id', 'model', 'comparability_class', 'protocol_id', *IDENTITY, *METRICS)}
    for key in ('inference_id', 'split'):
        item[key] = text(row.get(key), key)
    item.update(run['settings'])
    for key in ('benchmark', 'item_id', 'scorer_id'):
        item[key] = text(row.get(key), key, True)
    if item['scorer_id'] not in SCORERS:
        raise ValidationError('unknown scorer_id')
    for key in ('gold', 'parsed_answer', 'finish_reason'):
        item[key] = row.get(key)
    for key in ('correct', 'truncated'):
        value = row.get(key)
        if type(value) is int and value in (0, 1):
            value = bool(value)
        if value is not None and not isinstance(value, bool):
            raise ValidationError(f'{key} must be boolean or null')
        item[key] = value
    for key in ('tokens_in', 'tokens_out', 'epoch'):
        item[key] = number(row.get(key), key, 0)
    return item


def ingest(paths, hint=None, require_normalized=False):
    """Normalize only named files. Never recurse into inspect logs or model files."""
    runs, items, fast_checks = {}, [], {}

    def add_fast(row):
        try:
            normalized = fast.validate_record(copy.deepcopy(row))
        except (ValueError, TypeError, KeyError) as error:
            raise ValidationError('invalid fast-check record') from error
        key = normalized['record_id']
        if key in fast_checks:
            raise ValidationError('duplicate fast-check record')
        fast_checks[key] = normalized

    def add_run(row, run_id=None):
        normalized = normalize_run(row, hint, run_id)
        key = normalized['run_id']
        if key in runs and runs[key] != normalized:
            raise ValidationError(f'conflicting run_id: {key}; use distinct run IDs for protocols/settings')
        runs[key] = normalized

    for path in map(Path, paths):
        data = load(path)
        if require_normalized:
            if (not isinstance(data, dict) or type(data.get('schema_version')) is not int
                    or data['schema_version'] != 1
                    or not all(isinstance(data.get(key), list) for key in ('runs', 'items'))
                    or not isinstance(data.get('fast_checks', []), list)):
                raise ValidationError('expected normalized dataset version 1 with collections')
        validate_eci_provenance(data, dedicated_fast_checks=True)
        if path.name == 'run_manifest.json':
            # Attached via --manifest to a refit below; standalone validation only.
            validate_manifest(data)
            continue
        if isinstance(data, dict) and 'schema_version' in data:
            if data['schema_version'] != 1:
                raise ValidationError('unsupported schema_version')
            if not all(key in data for key in ('runs', 'items')):
                raise ValidationError('normalized dataset requires runs and items')
            for row in data.get('runs', []):
                add_run(row)
            items.extend(normalize_item(row) for row in data.get('items', []))
            for row in data.get('fast_checks', []):
                add_fast(row)
        elif isinstance(data, dict) and 'results' in data:
            for row in data['results']:
                add_run(row, row.get('run_id') or f'{path.parent.name}::{row.get("model_id", "unknown")}')
        else:
            rows = data if isinstance(data, list) else [data]
            extras = {}
            for row in rows:
                if not isinstance(row, dict):
                    raise ValidationError('record must be an object')
                if 'item_id' in row:
                    items.append(normalize_item(row))
                elif 'raw_score' in row:
                    # Validate every source component before aggregation can drop it.
                    component_protocol = eci_protocol(row)
                    model = row.get('model_id') or row.get('model')
                    key = row.get('run_id') or f'{path.stem}::{model}'
                    target = extras.setdefault(key, dict(row, run_id=key, status='NOT_COMPUTED', components={}))
                    target_protocol = eci_protocol(target)
                    if target_protocol is not None and component_protocol is not None and target_protocol != component_protocol:
                        raise ValidationError('extra-score protocol conflict')
                    target['protocol_id'] = target_protocol or component_protocol
                    name = text(row.get('benchmark'), 'benchmark', True)
                    if name in target['components']:
                        raise ValidationError('duplicate extra score benchmark')
                    target['components'][name] = {'score': row.get('raw_score'), 'status': row.get('status'), 'n': row.get('n_scored', row.get('n_items')),
                                                   'stderr': row.get('stderr'), 'scorer_id': row.get('scorer_id')}
                else:
                    add_run(row)
            for row in extras.values():
                add_run(row)
    seen = set()
    for item in items:
        identity = tuple(item[k] for k in ('run_id', 'benchmark', 'scorer_id', 'item_id', 'epoch'))
        if identity in seen:
            raise ValidationError('duplicate item record')
        seen.add(identity)
        if item['run_id'] not in runs:
            # Per-item evidence alone never computes an ECI.
            runs[item['run_id']] = normalize_run({key: value for key, value in item.items() if key not in METRICS}, hint)
        run = runs[item['run_id']]
        if run['comparability_class'] != 'unclassified' and item['comparability_class'] != 'unclassified' and run['comparability_class'] != item['comparability_class']:
            raise ValidationError('item/run comparability conflict')
        if run['protocol_id'] is not None and item['protocol_id'] is not None and run['protocol_id'] != item['protocol_id']:
            raise ValidationError('item/run protocol conflict')
        for key in ('model', *IDENTITY):
            if item[key] is not None and run[key] is not None and item[key] != run[key]:
                raise ValidationError(f'item/run {key} conflict')
        for key in SETTINGS:
            if item[key] is not None and run['settings'][key] is not None and item[key] != run['settings'][key]:
                raise ValidationError(f'item/run {key} conflict; use distinct run IDs')
        if item['scorer_id'] == 'lenient_v1' and run['comparability_class'] == 'baseline_protocol':
            raise ValidationError('lenient item cannot join baseline run')
    return {'schema_version': 1, 'runs': list(runs.values()), 'items': items,
            'fast_checks': list(fast_checks.values())}


def overlap(a, b):
    return all(row.get(key) is not None for row in (a, b) for key in ('eci_p05', 'eci_p95')) and max(a['eci_p05'], b['eci_p05']) <= min(a['eci_p95'], b['eci_p95'])


def performance(rows, items, metric):
    """Measured run metric or item mean, with its denominator; no imputed values."""
    measured, unknown = [], []
    for row in rows:
        observed = [item for item in items if item['run_id'] == row['run_id']]
        values = [item[metric] for item in observed if item[metric] is not None]
        value = row[metric]
        basis, count = 'provided run metric', None
        if value is None and values:
            value = math.fsum(values) / len(values)
            basis, count = 'mean of measured items; includes API/prefill/transport time for latency', len(values)
        if value is None:
            unknown.append(row['run_id'])
        else:
            measured.append({'run_id': row['run_id'], 'model': row['model'], metric: value,
                             'measurement': {'basis': basis, 'n_measured': count, 'n_items': len(observed) or None},
                             'status': row['status']})
    return sorted(measured, key=lambda row: row[metric]), unknown


def rank(data):
    # Pure callers may provide normalized data without going through ingest.
    # Apply the provenance barrier here too; recommend/apply both use rank.
    validate_eci_provenance(data, dedicated_fast_checks=True)
    result = {}
    for label in CLASSES:
        rows = [row for row in data['runs'] if row['comparability_class'] == label]
        computed = sorted((row for row in rows if row['status'] == 'COMPUTED' and row['eci_posterior_mean'] is not None),
                          key=lambda row: -row['eci_posterior_mean'])
        notes = [f'{a["run_id"]} / {b["run_id"]}: 90% CIs overlap; no clean winner.' for index, a in enumerate(computed)
                 for b in computed[index + 1:] if overlap(a, b)]
        if len(computed) == 1:
            notes.append('Only one computed ECI in this class; no overall winner established.')
        for row in computed:
            if row['diagnostics']['status'] != 'PASS':
                notes.append(f'{row["run_id"]}: diagnostics not PASS; excluded from recommendations.')
        component_ranks = {}
        for row in rows:
            for benchmark, component in row['components'].items():
                if component['score'] is None or not component['admitted']:
                    continue
                key = json.dumps([benchmark, component['scorer_id'], component['prompt_template_id'], row['protocol_id']])
                component_ranks.setdefault(key, []).append({'run_id': row['run_id'], 'model': row['model'], **component})
        latency, unknown_latency = performance(rows, data.get('items', []), 'latency_s')
        cost, unknown_cost = performance(rows, data.get('items', []), 'cost_usd')
        result[label] = {'eci': computed, 'notes': sorted(set(notes)),
                         'components': {key: sorted(values, key=lambda r: -r['score']) for key, values in component_ranks.items()},
                         'latency': latency, 'cost': cost,
                         'unknown_latency': unknown_latency, 'unknown_cost': unknown_cost}
    # This key has no ECI/components lists and is intentionally outside CLASSES.
    result['fast_check'] = {'label': 'fast check', 'hardware_views': fast.rank(data.get('fast_checks', []))}
    return result


def recommend(data):
    eligible = [row for row in rank(data)['baseline_protocol']['eci']
                if (row['machine'] or '').lower() == 'pc' and row['diagnostics']['status'] == 'PASS']
    chosen = eligible[0] if eligible else None
    route = {'model': chosen['model'] if chosen else None, 'machine': 'pc',
             'run_id': chosen['run_id'] if chosen else None,
             'status': 'PROVISIONAL' if chosen else 'NOT_COMPUTED',
             'confidence': 'low-to-moderate' if chosen else 'unknown',
             'eci': [chosen[k] for k in ('eci_posterior_mean', 'eci_p05', 'eci_p95')] if chosen else None,
             'basis': 'Highest available baseline local ECI; task-specific routing outcomes NOT_MEASURED. CI overlap is not a clean win.'}
    return {'schema_version': 1, 'routes': {task: copy.deepcopy(route) for task in ('general', 'reasoning', 'coding')},
            'nisi_mac': {'status': 'NOT_APPLIED', 'author': None, 'reviewer': None,
                         'requires': 'Two distinct LLMs already resident in Mac LM Studio; no PC artifact.'},
            'runtime_activation': 'NOT_APPLIED'}


def policy_path():
    return Path(os.environ.get('AGIW_ECI_TUNING_CONFIG', str(Path.home() / '.config/agiw/eci-tuning.json'))).expanduser()


def read_policy(path=None):
    target = Path(path) if path is not None else policy_path()
    policy = load(target if target.exists() else DEFAULT_POLICY)
    if not isinstance(policy, dict) or policy.get('schema_version') != 1 or not isinstance(policy.get('routes'), dict):
        raise ValidationError('invalid routing policy')
    return policy


def select_route(task_type, machine, policy=None):
    """Config hook for an external AGIW router; never dispatches or starts a model."""
    policy = policy if policy is not None else read_policy()
    route = policy.get('routes', {}).get(task_type)
    if not isinstance(route, dict) or str(machine).lower() != 'pc' or route.get('machine') != 'pc' or route.get('status') != 'PROVISIONAL':
        return None
    return copy.deepcopy(route)


def apply_policy(data, target, write=False):
    """Preserve unrelated configuration; dry-run by default, atomic explicit write."""
    target = Path(target).expanduser()
    if target.is_symlink():
        raise ValidationError('refusing a symlink config target')
    old = target.read_text(encoding='utf-8') if target.exists() else ''
    policy = json.loads(old) if old else {}
    if not isinstance(policy, dict):
        raise ValidationError('policy must be an object')
    proposed = recommend(data)
    if not proposed['routes']['general']['model']:
        raise ValidationError('no computed baseline PC model with passing diagnostics; policy unchanged')
    existing_routes = policy.get('routes', {})
    if not isinstance(existing_routes, dict):
        raise ValidationError('routes must be an object')
    proposed['routes'] = {**existing_routes, **proposed['routes']}
    policy.update({key: value for key, value in proposed.items() if key != 'nisi_mac'})
    # Never overwrite an existing Mac pair or suggest that it has been applied.
    policy.setdefault('nisi_mac', proposed['nisi_mac'])
    new = json.dumps(policy, indent=2, allow_nan=False) + '\n'
    diff = ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile='current policy', tofile='proposed policy'))
    if write and new != old:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.eci-policy-', dir=target.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(new)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return diff


def status(data=None):
    dataset = {'status': 'supplied', 'reason': None}
    if data is None:
        configured = os.environ.get('AGIW_ECI_TUNING_DATASET_DIR')
        if configured is None:
            data = ingest([CATALOG])
            dataset['status'] = 'bundled'
        else:
            dataset['status'] = 'configured'
            try:
                directory = Path(configured).expanduser()
                if not configured.strip() or not directory.is_absolute():
                    raise ValidationError('dataset directory must be an absolute path')
                source = directory / 'dataset.json'
                if source.is_symlink():
                    raise ValidationError('dataset file must not be a symlink')
                if not source.exists():
                    raise FileNotFoundError
                if not source.is_file():
                    raise ValidationError('dataset file must be a regular file')
                data = ingest([source], require_normalized=True)
            except FileNotFoundError:
                return {'status': 'unknown', 'dataset': {'status': 'unknown', 'reason': 'Configured dataset.json was not found.'}}
            except (OSError, UnicodeError):
                return {'status': 'unknown', 'dataset': {'status': 'unknown', 'reason': 'Configured dataset could not be read.'}}
            except (ValueError, TypeError, KeyError, AttributeError, IndexError):
                return {'status': 'unknown', 'dataset': {'status': 'unknown', 'reason': 'Configured dataset is invalid; check normalized dataset schema and records.'}}
    overlaps = [f'{a["run_id"]} / {b["run_id"]}: 90% CIs overlap; no clean improvement.'
                for a in data['runs'] for b in data['runs']
                if a['comparability_class'] == 'baseline_protocol' and b['comparability_class'] == 'pipeline_configured'
                and a['model'] == b['model'] and overlap(a, b)]
    policy = read_policy()
    public_policy = {'routes': {task: {key: route.get(key) for key in ('model', 'machine', 'status', 'confidence', 'eci')}
                               for task, route in policy['routes'].items() if task in ('general', 'reasoning', 'coding') and isinstance(route, dict)},
                     'runtime_activation': 'NOT_APPLIED', 'nisi_mac': {'status': 'NOT_APPLIED'}}
    return {'name': 'AGIW x ECI Model Tuning Tool', 'schema_version': 1, 'dataset': dataset,
            'rankings': rank(data), 'recommendation': recommend(data),
            'policy': public_policy, 'pending': [r for r in data['runs'] if r['status'] != 'COMPUTED' or r['eci_posterior_mean'] is None],
            'cross_class_note': 'Comparability classes are separate. Published Epoch scores belong to different artifacts. ' + ' '.join(overlaps)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('ingest', 'validate', 'rank', 'recommend', 'apply', 'status'):
        p = sub.add_parser(command)
        p.add_argument('inputs', nargs='*', help='Named JSON, JSONL, or CSV files (no directory scans)')
        p.add_argument('--comparability', choices=CLASSES, help='Explicit protocol assertion; unknown by default')
        if command in ('ingest', 'validate'):
            p.add_argument('--manifest', type=Path, help='Refit run_manifest.json; diagnostic metadata only')
        if command == 'ingest':
            p.add_argument('--output', type=Path)
        if command == 'apply':
            p.add_argument('--config', type=Path, default=policy_path())
            p.add_argument('--write', action='store_true', help='Explicitly write the displayed diff')
    args = parser.parse_args(argv)
    try:
        data = ingest(args.inputs or [CATALOG], args.comparability)
        if getattr(args, 'manifest', None):
            manifest = load(args.manifest)
            validate_manifest(manifest)
            if len(data['runs']) != 1 or len(args.inputs) != 1:
                raise ValidationError('manifest requires exactly one refit input and model')
            source = Path(args.inputs[0])
            payload = load(source)
            if source.resolve().parent != args.manifest.resolve().parent or not isinstance(payload, dict) or 'results' not in payload:
                raise ValidationError('manifest must accompany its sibling refit results')
            for run in data['runs']:
                if manifest.get('run_id') is not None and manifest['run_id'] != run['run_id']:
                    raise ValidationError('manifest and result run_id conflict')
                identities = manifest.get('local_identity_map')
                if identities and run['model'] not in identities:
                    raise ValidationError('manifest and result model identity conflict')
                if run['status'] == 'COMPUTED' and manifest['status'] != 'COMPUTED':
                    raise ValidationError('manifest and result status conflict')
                diagnostic = manifest.get('diagnostic_status')
                if diagnostic is not None and run['diagnostics']['status'] is not None and diagnostic != run['diagnostics']['status']:
                    raise ValidationError('manifest and result diagnostics conflict')
                if run['diagnostics']['status'] is None:
                    run['diagnostics']['status'] = manifest.get('diagnostic_status')
        if args.command == 'apply':
            print(apply_policy(data, args.config, args.write) or 'No policy changes.')
            print('Policy file written.' if args.write else 'Dry run; pass --write to apply.')
            print('External runtime activation NOT_APPLIED; Nisi Mac configuration unchanged.')
            return 0
        result = {'ingest': lambda: data, 'validate': lambda: {'valid': True, 'runs': len(data['runs']), 'items': len(data['items']), 'fast_checks': len(data['fast_checks'])},
                  'rank': lambda: rank(data), 'recommend': lambda: recommend(data), 'status': lambda: status(data)}[args.command]()
        output = json.dumps(result, indent=2, allow_nan=False) + '\n'
        if getattr(args, 'output', None):
            args.output.write_text(output, encoding='utf-8')
        else:
            print(output, end='')
        return 0
    except (ValidationError, OSError, ValueError, TypeError, KeyError) as error:
        # Input payloads/paths can be private; do not echo them on validation failure.
        print(f'ECI operation failed ({type(error).__name__}); check inputs against eci_tuning.schema.json.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
