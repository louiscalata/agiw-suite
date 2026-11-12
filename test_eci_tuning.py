"""Offline data and policy tests. No model runtime, browser, or live server calls."""
import ast
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import eci_tuning as e

FIXTURES = e.ROOT / 'test_support/eci_tuning'


class TuningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=e.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.override = patch.dict(os.environ, {'AGIW_ECI_TUNING_CONFIG': str(self.path / 'override.json')})
        self.override.start()
        self.addCleanup(self.override.stop)
        self.data = e.ingest([e.CATALOG])

    def file(self, name, value):
        p = self.path / name
        p.write_text(json.dumps(value), encoding='utf-8')
        return p

    def test_refit_and_manifest_preserve_eci_and_components(self):
        data = e.ingest([FIXTURES / 'results.json', FIXTURES / 'run_manifest.json'])
        row = data['runs'][0]
        self.assertAlmostEqual(row['eci_posterior_mean'], 105.37592661908786)
        self.assertEqual(row['diagnostics']['status'], 'PASS')
        self.assertEqual(row['components']['OpenBookQA']['score'], .752)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = e.main(['validate', str(FIXTURES / 'results.json'), '--manifest', str(FIXTURES / 'run_manifest.json')])
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(out.getvalue())['valid'])

    def test_extra_scores_alone_do_not_compute_eci(self):
        data = e.ingest([FIXTURES / 'extra_scores.json'])
        self.assertEqual(data['runs'][0]['status'], 'NOT_COMPUTED')
        self.assertIsNone(data['runs'][0]['eci_posterior_mean'])
        self.assertEqual(len(data['runs'][0]['components']), 4)

    def test_items_keep_unknowns_and_do_not_invent_eci(self):
        data = e.ingest([FIXTURES / 'items.jsonl'])
        self.assertEqual(len(data['items']), 2)
        self.assertEqual(data['runs'][0]['status'], 'NOT_COMPUTED')
        self.assertTrue(data['items'][1]['truncated'])
        self.assertFalse(data['items'][1]['correct'])
        self.assertIsNone(data['items'][0]['memory_bytes'])
        self.assertIsNone(data['items'][0]['tokens_out'])
        self.assertIsNone(e.recommend(data)['routes']['general']['model'])

    def test_analysis_table_layout_and_integer_correctness(self):
        path = self.path / 'runs.csv'
        path.write_text('run_id,model,machine,status,benchmark,configuration_label,scorer_id,n,aggregate_score,eci,eci_p05,eci_p95,eci_associations,partial\n'
                        'analysis-run,fixture-model,PC,OK,fixture-choice,baseline,strict_choice,2,0.5,105,94,115,[],False\n')
        items = [json.loads(line) for line in (FIXTURES / 'items.jsonl').read_text().splitlines()]
        for item in items:
            item.update(run_id='analysis-run', correct=int(item['correct']))
        data = e.ingest([path, self.file('items.json', items)])
        row = data['runs'][0]
        self.assertEqual(row['comparability_class'], 'baseline_protocol')
        self.assertEqual(row['components']['fixture-choice']['n'], 2)
        self.assertIsNone(row['eci_posterior_mean'])
        self.assertIsNone(row['eci_p05'])
        self.assertIs(data['items'][0]['correct'], True)

    def test_analysis_associations_are_not_transferred_to_artifact(self):
        row = dict(run_id='component-run', model='fixture-model', status='OK', configuration_label='pipeline-configured lenient rescore',
                   eci=108.59, eci_p05=98.29, eci_p95=118.43,
                   eci_associations=[dict(fit_id='full-fit', eci=108.59, eci_p05=98.29, eci_p95=118.43)])
        data = e.ingest([self.file('runs.json', [row])])
        self.assertEqual(data['runs'][0]['eci_associations'][0]['eci'], 108.59)
        self.assertFalse(e.rank(data)['pipeline_configured']['eci'])

    def test_comparability_split_and_cross_class_overlap(self):
        ranked = e.rank(self.data)
        self.assertAlmostEqual(ranked['baseline_protocol']['eci'][0]['eci_posterior_mean'], 105.37592661908786)
        self.assertAlmostEqual(ranked['pipeline_configured']['eci'][0]['eci_posterior_mean'], 108.58924763219414)
        self.assertIn('90% CIs overlap; no clean improvement', e.status(self.data)['cross_class_note'])
        self.assertIn('Only one computed', ranked['baseline_protocol']['notes'][0])

    def test_overlap_wording_within_class_and_touching_bounds(self):
        row = copy.deepcopy(self.data['runs'][0])
        row.update(run_id='second', model='second', eci_posterior_mean=110, eci_p05=100, eci_p95=120)
        self.data['runs'].append(row)
        self.assertTrue(any('no clean winner' in note for note in e.rank(self.data)['baseline_protocol']['notes']))
        self.assertTrue(e.overlap(dict(eci_p05=1, eci_p95=2), dict(eci_p05=2, eci_p95=3)))
        self.assertFalse(e.overlap(dict(eci_p05=None, eci_p95=None), row))

    def test_lenient_cannot_be_forced_into_baseline(self):
        row = dict(run_id='lenient', model='fixture', status='NOT_COMPUTED', scorer_id='lenient_v1')
        self.assertEqual(e.normalize_run(row)['comparability_class'], 'pipeline_configured')
        with self.assertRaises(e.ValidationError):
            e.normalize_run(row, 'baseline_protocol')

    def test_recommend_is_pc_only_and_excludes_failed_diagnostics(self):
        policy = e.recommend(self.data)
        self.assertIn('Llama', policy['routes']['coding']['model'])
        self.assertEqual(policy['routes']['coding']['confidence'], 'low-to-moderate')
        self.assertIsNone(e.select_route('coding', 'mac', policy))
        self.assertIsNotNone(e.select_route('coding', 'PC', policy))
        self.assertIsNone(policy['nisi_mac']['author'])
        self.assertIsNone(policy['nisi_mac']['reviewer'])
        self.data['runs'][0]['diagnostics']['status'] = 'FAIL'
        self.assertIsNone(e.recommend(self.data)['routes']['general']['model'])

    def test_apply_preview_explicit_write_preserve_mac_and_other_routes(self):
        target = self.path / 'policy.json'
        existing = dict(schema_version=1, note='retained', routes={'custom': {'model': 'other'}},
                        nisi_mac=dict(author='resident-a', reviewer='resident-b'))
        target.write_text(json.dumps(existing))
        diff = e.apply_policy(self.data, target)
        self.assertIn('+', diff)
        self.assertEqual(json.loads(target.read_text()), existing)
        e.apply_policy(self.data, target, write=True)
        policy = json.loads(target.read_text())
        self.assertEqual(policy['note'], 'retained')
        self.assertEqual(policy['routes']['custom'], existing['routes']['custom'])
        self.assertEqual(policy['nisi_mac'], existing['nisi_mac'])
        self.assertEqual(policy['runtime_activation'], 'NOT_APPLIED')
        self.assertEqual(e.apply_policy(self.data, target), '')

    def test_no_candidate_apply_refuses_to_write_and_symlink_rejected(self):
        target = self.path / 'policy.json'
        with self.assertRaises(e.ValidationError):
            e.apply_policy(dict(runs=[], items=[], schema_version=1), target, write=True)
        self.assertFalse(target.exists())
        target.symlink_to(self.path / 'other.json')
        with self.assertRaises(e.ValidationError):
            e.apply_policy(self.data, target, write=True)

    def test_override_path_and_honest_pending(self):
        e.apply_policy(self.data, e.policy_path(), write=True)
        self.assertEqual(e.read_policy()['routes']['general']['machine'], 'pc')
        pending = {r['run_id']: r for r in e.status(self.data)['pending']}
        self.assertEqual(pending['pc-lfm3']['status'], 'NOT_RUN')
        for key in ('pc-bonsai', 'mac-glimmer'):
            self.assertEqual(pending[key]['status'], 'NOT_COMPUTED')
            self.assertIsNone(pending[key]['eci_posterior_mean'])

    def test_invalid_scores_and_identity_conflicts_fail(self):
        base = copy.deepcopy(self.data['runs'][0])
        for changes in (dict(eci_p05=200), dict(eci_p95=None), dict(eci_posterior_mean=float('nan')),
                        dict(status='NOT_RUN'), dict(n=-1), dict(settings={'scorer_id':'made-up'})):
            with self.subTest(changes=changes), self.assertRaises(e.ValidationError):
                e.normalize_run(dict(base, **changes))
        with self.assertRaises(e.ValidationError):
            e.ingest([self.file('conflict.json', [base, dict(base, model='other')])])

    def test_duplicates_and_settings_conflicts_do_not_merge(self):
        item = json.loads((FIXTURES / 'items.jsonl').read_text().splitlines()[0])
        with self.assertRaises(e.ValidationError):
            e.ingest([self.file('items.json', [item, item])])
        other = dict(item, item_id='different', temperature=.5)
        with self.assertRaises(e.ValidationError):
            e.ingest([self.file('items.json', [item, other])])

    def test_cli_error_does_not_echo_private_payload(self):
        path = self.file('bad.json', dict(model='PRIVATE_SENTINEL'))
        with contextlib.redirect_stderr(io.StringIO()) as out:
            code = e.main(['validate', str(path)])
        self.assertEqual(code, 2)
        self.assertNotIn('PRIVATE_SENTINEL', out.getvalue())

    def test_failed_partial_and_withheld_components_are_not_ranked(self):
        rows = [dict(run_id=state, model='fixture', machine='Mac', status=state, benchmark='fixture-choice',
                     aggregate_score=.99, admitted_score=None, partial=state == 'PARTIAL', configuration_label='baseline')
                for state in ('FAILED', 'PARTIAL', 'NOT_RUN')]
        data = e.ingest([self.file('withheld.json', rows)])
        self.assertFalse(e.rank(data)['baseline_protocol']['components'])
        self.assertTrue(data['runs'][1]['partial'])
        self.assertEqual(data['runs'][1]['source_status'], 'PARTIAL')
        extras = [dict(run_id='extra',model='fixture',raw_score=.9,status='NOT_RUN',benchmark='fixture')]
        self.assertFalse(e.rank(e.ingest([self.file('extras.json', extras)]))['unclassified']['components'])

    def test_known_protocol_and_class_conflicts_are_rejected(self):
        item = json.loads((FIXTURES / 'items.jsonl').read_text().splitlines()[0])
        first = dict(item, comparability_class='baseline_protocol', protocol_id='a')
        for other in (dict(first, item_id='two', comparability_class='pipeline_configured'),
                      dict(first, item_id='two', protocol_id='b')):
            with self.assertRaises(e.ValidationError):
                e.ingest([self.file('protocol.json', [first, other])])

    def test_manifest_conflicts_fail_validation(self):
        result = self.file('results.json',json.loads((FIXTURES/'results.json').read_text()))
        for changes in (dict(diagnostic_status='FAIL'), dict(sampling={'ci_level':.95})):
            manifest = self.file('manifest.json',dict(status='COMPUTED',**changes))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(e.main(['validate',str(result),'--manifest',str(manifest)]),2)

    def test_http_policy_projection_drops_private_extras(self):
        policy = e.recommend(self.data)
        policy['private_detail'] = 'PRIVATE_SENTINEL'
        self.file('override.json',policy)
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(e.status(self.data)))

    def test_all_protocol_fields_and_levels_cannot_reach_any_eci_consumer(self):
        # Derive aliases from the actual ingestion module, not a copied inventory.
        # A newly declared alias and a new literal protocol-bearing read both join
        # this end-to-end barrier test automatically.
        tree = ast.parse(Path(e.__file__).read_text())
        aliases = set(e.PROTOCOL_FIELDS)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == 'get' and node.args and isinstance(node.args[0], ast.Constant):
                    key = node.args[0].value
                    if isinstance(key, str) and ('protocol' in key or key == 'kind'):
                        aliases.add(key)
            elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                  and isinstance(node.slice, ast.Constant)):
                key = node.slice.value
                if isinstance(key, str) and ('protocol' in key or key == 'kind'):
                    aliases.add(key)
        markers = (e.fast.PROTOCOL, 'agiw_fast_check')
        target = self.path / 'protected-policy.json'
        before = b'{"preserved": true}\n'
        item = json.loads((FIXTURES / 'items.jsonl').read_text().splitlines()[0])

        def clean():
            data = copy.deepcopy(self.data)
            data['runs'] = [data['runs'][0]]
            data['items'] = []
            return data

        def locate(data, location):
            run = data['runs'][0]
            if location == 'container':
                return data
            if location == 'run':
                return run
            if location == 'item':
                data['items'] = [copy.deepcopy(item)]
                return data['items'][0]
            if location == 'component':
                return run['components']['Winogrande']
            if location == 'settings':
                return run['settings']
            if location == 'diagnostics':
                return run['diagnostics']
            if location == 'association':
                run['eci_associations'] = [dict(fit_id='fit')]
                return run['eci_associations'][0]
            if location == 'discarded_metadata':
                run['discarded_metadata'] = {'deep': [{}]}
                return run['discarded_metadata']['deep'][0]
            raise AssertionError(location)

        actions = (
            ('component ranking', lambda data: e.rank(data)['baseline_protocol']['components']),
            ('ECI ranking', lambda data: e.rank(data)['baseline_protocol']['eci']),
            ('recommend', e.recommend),
            ('policy apply', lambda data: e.apply_policy(data, target, write=True)),
        )
        # Prove the clean control reaches both rankings and policy application.
        for _, action in actions:
            target.write_bytes(before)
            self.assertTrue(action(e.ingest([self.file('clean.json', clean())])))

        for alias in sorted(aliases):
            for marker in markers:
                for location in ('container', 'run', 'item', 'component', 'settings',
                                 'diagnostics', 'association', 'discarded_metadata'):
                    data = clean()
                    locate(data, location)[alias] = marker
                    with self.subTest(alias=alias, marker=marker, location=location):
                        target.write_bytes(before)
                        # The entire ingest -> component ranking -> ECI ranking ->
                        # recommendation -> policy write chain stops at ingest.
                        with self.assertRaisesRegex(e.ValidationError, 'fast checks belong only'):
                            normalized = e.ingest([self.file('fast-alias.json', data)])
                            for _, action in actions:
                                action(normalized)
                        self.assertEqual(target.read_bytes(), before)
                        # Callers bypassing ingest meet the same recursive barrier.
                        for name, action in actions:
                            with self.subTest(action=name):
                                with self.assertRaisesRegex(e.ValidationError, 'fast checks belong only'):
                                    action(data)
                                self.assertEqual(target.read_bytes(), before)

        # Raw adapters use different envelopes before normalization. Exercise
        # each one with the same source-derived alias inventory and full chain.
        for alias in sorted(aliases):
            for marker in markers:
                row = copy.deepcopy(self.data['runs'][0])
                extra = dict(run_id='extra', model='fixture', benchmark='choice', raw_score=.98)
                for name, payload in (
                        ('list run', [dict(row, **{alias: marker})]),
                        ('results run', {'results': [dict(row, **{alias: marker})]}),
                        ('results envelope', {'results': [row], alias: marker}),
                        ('extra score', [extra, dict(extra, benchmark='second', **{alias: marker})]),
                        ('list item', [dict(item, **{alias: marker})]),
                        ('manifest', dict(status='COMPUTED', **{alias: marker}))):
                    target.write_bytes(before)
                    filename = 'run_manifest.json' if name == 'manifest' else 'raw-provenance.json'
                    with self.subTest(alias=alias, marker=marker, envelope=name):
                        with self.assertRaisesRegex(e.ValidationError, 'fast checks belong only'):
                            normalized = e.ingest([self.file(filename, payload)])
                            for _, action in actions:
                                action(normalized)
                        self.assertEqual(target.read_bytes(), before)

    def test_fast_markers_in_arbitrary_strings_keys_and_lists_reject(self):
        target = self.path / 'protected-policy.json'
        before = b'{"preserved": true}\n'
        actions = (
            lambda data: e.rank(data)['baseline_protocol']['components'],
            lambda data: e.rank(data)['baseline_protocol']['eci'],
            e.recommend,
            lambda data: e.apply_policy(data, target, write=True),
        )
        for marker in (e.fast.PROTOCOL, 'agiw_fast_check'):
            for location in ('root metadata', 'run reason', 'component metadata',
                             'list member', 'map key', 'discarded metadata'):
                data = copy.deepcopy(self.data)
                row = data['runs'][0]
                if location == 'root metadata':
                    data['origin'] = marker
                elif location == 'run reason':
                    row['reason'] = 'prefix ' + marker.upper() + ' suffix'
                elif location == 'component metadata':
                    row['components']['Winogrande']['origin'] = marker
                elif location == 'list member':
                    row['tags'] = ['ordinary', [marker]]
                elif location == 'map key':
                    row['extra'] = {marker: 'ordinary'}
                else:
                    row['discarded_metadata'] = {'deep': [{'origin': marker}]}
                target.write_bytes(before)
                with self.subTest(marker=marker, location=location):
                    with self.assertRaisesRegex(e.ValidationError, 'fast checks belong only'):
                        normalized = e.ingest([self.file('arbitrary-fast-marker.json', data)])
                        for action in actions:
                            action(normalized)
                    self.assertEqual(target.read_bytes(), before)
                for index, action in enumerate(actions):
                    with self.subTest(marker=marker, location=location, action=index):
                        with self.assertRaisesRegex(e.ValidationError, 'fast checks belong only'):
                            action(data)
                        self.assertEqual(target.read_bytes(), before)

    def test_nested_conflicting_protocols_reject_before_normalization(self):
        base = copy.deepcopy(self.data['runs'][0])
        for fields in ({'protocol_id': 'one', 'protocol': 'two'},
                       {'protocol_id': 'different-from-parent'}):
            row = copy.deepcopy(base)
            row['components']['Winogrande'].update(fields)
            with self.subTest(fields=fields), self.assertRaisesRegex(e.ValidationError, 'protocol'):
                e.ingest([self.file('nested-conflict.json', [row])])
        payload = {'schema_version': 1, 'runs': [base], 'items': [], 'protocol_id': 'envelope'}
        with self.assertRaisesRegex(e.ValidationError, 'protocol'):
            e.ingest([self.file('parent-conflict.json', payload)])

    def test_fast_records_only_enter_through_dedicated_collection(self):
        from test_eci_fast_check import run
        record = run()
        for misplaced in (record, [record], {'results': [record]},
                          {'schema_version': 1, 'runs': [record], 'items': []}):
            with self.subTest(payload_type=type(misplaced)), self.assertRaisesRegex(
                    e.ValidationError, 'fast checks belong only'):
                e.ingest([self.file('misplaced-fast.json', misplaced)])
        collection = {'schema_version': 1, 'runs': [], 'items': [], 'fast_checks': [record]}
        self.assertEqual(e.ingest([self.file('proper-fast.json', collection)])['fast_checks'], [record])
        # A same-named nested field is not the dedicated root collection.
        collection['runs'] = [dict(run_id='nested', model='fixture', fast_checks=[record])]
        with self.assertRaisesRegex(e.ValidationError, 'fast checks belong only'):
            e.ingest([self.file('nested-fast-collection.json', collection)])

    def test_duplicate_protocol_fields_cannot_discard_fast_check_markers(self):
        row = copy.deepcopy(self.data['runs'][0])
        row.pop('protocol_id', None)
        jsonl = self.path / 'duplicate.jsonl'
        jsonl.write_text('{"protocol":"agiw-fast-check-v1","protocol":"ordinary",' +
                         json.dumps(row)[1:] + '\n')
        csv = self.path / 'duplicate.csv'
        csv.write_text('run_id,model,status,machine,protocol,protocol\n'
                       'duplicate,fixture,NOT_COMPUTED,pc,agiw-fast-check-v1,ordinary\n')
        for source in (jsonl, csv):
            with self.subTest(source=source.suffix), self.assertRaises(ValueError):
                e.ingest([source])

    def test_ingest_verifies_bound_baseline_and_refuses_forged_snapshot(self):
        from test_eci_fast_check import run, rehash
        baseline = run(answer=lambda item: ('A' if int(item['id']) < 130 else 'B', 1, 1))
        candidate = run(baseline=baseline)
        self.assertEqual(candidate['comparison']['arc']['verdict'], 'BETTER')
        ingested = e.ingest([self.file('bound-baseline.json', dict(schema_version=1, runs=[], items=[], fast_checks=[candidate]))])
        self.assertEqual(ingested['fast_checks'][0]['baseline']['record']['record_id'], baseline['record_id'])
        forged = copy.deepcopy(candidate)
        forged['baseline']['outcomes_sha256'] = '0' * 64
        rehash(forged)
        with self.assertRaises(e.ValidationError):
            e.ingest([self.file('forged-baseline.json', dict(schema_version=1, runs=[], items=[], fast_checks=[forged]))])

    def test_ingest_unresolved_baseline_requires_unverified_inconclusive(self):
        from test_eci_fast_check import run, rehash
        baseline = run(answer=lambda item: ('A' if int(item['id']) < 130 else 'B', 1, 1))
        candidate = run(baseline=baseline)
        candidate['baseline']['record'] = None
        rehash(candidate)
        with self.assertRaises(e.ValidationError):
            e.ingest([self.file('unverified-conclusive.json', dict(schema_version=1, runs=[], items=[], fast_checks=[candidate]))])
        candidate['comparison'] = e.fast.comparisons(candidate, candidate['baseline'], candidate['alpha'])
        rehash(candidate)
        ingested = e.ingest([self.file('unverified-baseline.json', dict(schema_version=1, runs=[], items=[], fast_checks=[candidate]))])
        comparison = ingested['fast_checks'][0]['comparison']['arc']
        self.assertEqual(comparison['verification'], 'UNVERIFIED')
        self.assertEqual(comparison['verdict'], 'INCONCLUSIVE')
        ranked = e.rank(ingested)['fast_check']['hardware_views'][0]
        self.assertTrue(all(row['verdict'] == 'INCONCLUSIVE' for row in ranked['rows']))

    def test_protocol_alias_preserved_and_conflicts_rejected(self):
        base = copy.deepcopy(self.data['runs'][0])
        base.pop('protocol_id', None)
        normalized = e.normalize_run(dict(base, protocol='fixture-protocol'))
        self.assertEqual(normalized['protocol_id'], 'fixture-protocol')
        self.assertEqual(e.normalize_run(normalized)['protocol_id'], 'fixture-protocol')
        for aliases in (dict(protocol_id='one', protocol='two'),
                        dict(protocol_id='ordinary', protocol=e.fast.PROTOCOL)):
            with self.subTest(aliases=aliases), self.assertRaises(e.ValidationError):
                e.normalize_run(dict(base, **aliases))
        item = json.loads((FIXTURES / 'items.jsonl').read_text().splitlines()[0])
        payload = {'schema_version': 1, 'runs': [dict(base, protocol='one')],
                   'items': [dict(item, run_id=base['run_id'], protocol='two')]}
        with self.assertRaises(e.ValidationError):
            e.ingest([self.file('alias-conflict.json', payload)])

    def test_extra_score_aggregation_preserves_and_checks_protocol_aliases(self):
        first = dict(run_id='extra', model='fixture', benchmark='first', raw_score=.9)
        second = dict(first, benchmark='second', protocol='fixture-protocol')
        data = e.ingest([self.file('extra-provenance.json', [first, second])])
        self.assertEqual(data['runs'][0]['protocol_id'], 'fixture-protocol')
        with self.assertRaises(e.ValidationError):
            e.ingest([self.file('extra-conflict.json',
                               [dict(first, protocol_id='different'), second])])

    def test_normalized_round_trip_preserves_component_admission(self):
        row = dict(run_id='a',model='fixture',status='OK',benchmark='fixture',aggregate_score=.5,admitted_score=.5)
        data = e.ingest([self.file('admitted.json',[row])])
        again = e.ingest([self.file('normalized.json',data)])
        self.assertTrue(again['runs'][0]['components']['fixture']['admitted'])

    def test_failed_or_partial_source_cannot_admit_computed_eci(self):
        base = copy.deepcopy(self.data['runs'][0])
        for changes in (dict(source_status='FAILED'), dict(source_status='PARTIAL'), dict(partial=True)):
            with self.assertRaises(e.ValidationError):
                e.normalize_run(dict(base, **changes))

    def test_manifest_attachment_rejects_unrelated_or_ambiguous_results(self):
        base = copy.deepcopy(self.data['runs'][0])
        path = self.file('results.json',dict(results=[base,dict(base,run_id='different')]))
        manifest = self.file('run_manifest.json',dict(status='COMPUTED',diagnostic_status='PASS'))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(e.main(['validate',str(path),'--manifest',str(manifest)]),2)
        self.file('results.json',dict(results=[base]))
        self.file('run_manifest.json',dict(status='COMPUTED',diagnostic_status='PASS',run_id='unrelated'))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(e.main(['validate',str(path),'--manifest',str(manifest)]),2)

    def test_item_performance_uses_measured_mean_and_denominator(self):
        item = json.loads((FIXTURES / 'items.jsonl').read_text().splitlines()[0])
        data = e.ingest([self.file('latency.json', [dict(item,latency_s=2),dict(item,item_id='two',latency_s=4),dict(item,item_id='three')])])
        self.assertIsNone(data['runs'][0]['latency_s'])
        ranked = e.rank(data)['unclassified']
        self.assertEqual(ranked['latency'][0]['latency_s'],3)
        self.assertEqual(ranked['latency'][0]['measurement']['n_measured'],2)
        self.assertEqual(ranked['latency'][0]['measurement']['n_items'],3)
        self.assertEqual(len(ranked['unknown_cost']),1)


if __name__ == '__main__':
    unittest.main()
