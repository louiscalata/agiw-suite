"""Audit regressions: only local data and in-memory observations."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import eci_tuning as e


def comparable_run(run_id='left'):
    return e.normalize_run(dict(run_id=run_id, model='fixture-model', status='COMPUTED',
        comparability_class='baseline_protocol', protocol_id='fixture-protocol',
        hardware_profile_id='fixture-profile', dataset_artifact_id='fixture-dataset',
        model_artifact_id='fixture-artifact', settings_id='fixture-settings',
        machine='fixture-machine', quant='fixture-quant', backend='fixture-backend', backend_version='fixture-build',
        settings=dict(temperature=0, max_tokens=100, ctx=200, n_gpu_layers=0,
                      reasoning_budget=0, stop=[], system_message='', n_epochs=1,
                      prompt_template_id='fixture-template', scorer_id='strict_choice'),
        eci_p05=90, eci_posterior_mean=100, eci_p95=110,
        diagnostics={'status':'PASS'},
        components={'choice':dict(score=.5, normalized_score=.25, n=2,
                       scorer_id='strict_choice',prompt_template_id='fixture-template',admitted=True)}))


def observations(run_id):
    return [dict(run_id=run_id, benchmark='choice', item_id=str(i), truncated=False,
                 parsed_answer='A', latency_s=None, cost_usd=None) for i in range(2)]


def compare_with_items(a, b, items):
    return e.compare_runs(*(dict(row, item_evidence=e.item_evidence(
        [item for item in items if item['run_id'] == row['run_id']])) for row in (a, b)))


class TuningCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=e.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'AGIW_ECI_TUNING_CONFIG':str(self.directory/'policy.json')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, value):
        source = self.directory/'dataset.json'
        source.write_text(json.dumps(value), encoding='utf-8')
        return source

    def settled(self):
        for _ in range(100):
            result = e.status()
            if 'in progress' not in (result.get('dataset',{}).get('reason') or ''):
                return result
            time.sleep(.01)
        self.fail('fixture validation did not settle')

    def test_comparisons_require_complete_matching_evidence(self):
        a,b=comparable_run(),comparable_run('right')
        items=observations('left')+observations('right')
        self.assertTrue(compare_with_items(a,b,items)['comparable'])
        cases=[('machine', 'other'), ('machine',None), ('quant','other'),
               ('hardware_profile_id',None), ('dataset_artifact_id','other')]
        for key,value in cases:
            with self.subTest(key=key,value=value):
                changed=copy.deepcopy(b);changed[key]=value
                self.assertFalse(compare_with_items(a,changed,items)['comparable'])
        for key in ('score','normalized_score','n'):
            changed=copy.deepcopy(b);changed['components']['choice'][key]=None
            self.assertFalse(compare_with_items(a,changed,items)['comparable'])
        changed=copy.deepcopy(b);changed['components']['choice']['n']=3
        self.assertFalse(compare_with_items(a,changed,items)['comparable'])
        for values in ({'truncated':True},{'truncated':None},{'parsed_answer':None}):
            bad=[dict(item,**values) for item in items]
            self.assertFalse(compare_with_items(a,b,bad)['comparable'])
        self.assertFalse(compare_with_items(a,b,[])['comparable'])
        a['settings']['temperature']=None;b['settings']['temperature']=None
        self.assertFalse(compare_with_items(a,b,items)['comparable'])

    def test_comparisons_require_complete_integer_component_denominators(self):
        a, b = comparable_run(), comparable_run('right')
        items = observations('left') + observations('right')
        self.assertFalse(compare_with_items(a, b, items[1:])['comparable'])
        for row in (a, b):
            row['components']['choice']['n'] = 1.5
        self.assertFalse(compare_with_items(a, b, items)['comparable'])

    def test_pair_wording_interval_relation_and_both_classes(self):
        a,b=comparable_run(),comparable_run('right')
        items=observations('left')+observations('right')
        self.assertEqual(compare_with_items(a,b,items)['uncertainty'], '90% posterior intervals overlap; no clean winner.')
        b.update(eci_p05=120,eci_posterior_mean=130,eci_p95=140)
        self.assertEqual(compare_with_items(a,b,items)['uncertainty'], '90% posterior intervals do not overlap; causal superiority is not established.')
        b['comparability_class']='pipeline_configured'
        result=e.rank(dict(runs=[a,b],items=items,fast_checks=[]))
        for label in ('baseline_protocol','pipeline_configured'):
            pair=result[label]['comparisons'][0]
            self.assertEqual((pair['left_run_id'],pair['right_run_id']),('left','right'))
            self.assertFalse(pair['comparable'])
            self.assertEqual(pair['interval_relation'],'disjoint')

    def test_default_policy_and_recommendation_do_not_select_a_model(self):
        self.assertIsNone(e.select_route('general','pc'))
        self.assertEqual(e.read_policy()['origin'],'bundled reference')
        self.assertTrue(all(r['model'] is None for r in e.recommend(e.ingest([e.CATALOG]))['routes'].values()))

    def test_saved_policy_missing_source_and_corrupt_policy_preserve_dataset(self):
        data=e.ingest([e.CATALOG])
        policy=e.read_policy()
        policy['routes']['general'].update(model='fixture', run_id='missing',status='PROVISIONAL')
        target=self.directory/'policy.json';target.write_text(json.dumps(policy))
        result=e.status(data)
        self.assertEqual(result['policy']['origin'],'saved')
        self.assertEqual(result['policy']['routes']['general']['status'],'UNKNOWN')
        self.assertIn('source run',result['policy']['routes']['general']['reason'])
        target.write_text('{')
        result=e.status(data)
        self.assertTrue(result['rankings'])
        self.assertEqual(result['policy']['origin'],'unreadable')

    def test_first_slow_validation_returns_unknown_before_deadline_and_caches(self):
        source=self.write(dict(schema_version=1,runs=[],items=[]))
        entered,release=threading.Event(),threading.Event()
        original=e.ingest
        calls=[]
        def slow(*args,**kwargs):
            calls.append(1);entered.set();release.wait(5)
            return original(*args,**kwargs)
        with patch.dict(os.environ,{'AGIW_ECI_TUNING_DATASET_DIR':str(self.directory)}), patch.object(e,'ingest',side_effect=slow):
            try:
                started=time.monotonic();result=e.status();elapsed=time.monotonic()-started
                self.assertLess(elapsed,1.0)
                self.assertEqual(result['dataset']['status'],'unknown')
                self.assertIn('in progress',result['dataset']['reason'])
                self.assertTrue(entered.is_set())
            finally:
                release.set()
            self.assertEqual(self.settled()['dataset']['status'],'configured')
            self.assertEqual(self.settled()['dataset']['status'],'configured')
            self.assertEqual(len(calls),1)
            source.write_text(json.dumps(dict(schema_version=1,runs=[],items=[],as_of='fixture-date')))
            self.assertEqual(self.settled()['dataset']['as_of'],'fixture-date')
            self.assertEqual(len(calls),2)

    def test_dataset_paths_size_and_exception_failures_have_reasons(self):
        for value in ('','relative',str(self.directory/'..'/self.directory.name)):
            with self.subTest(value=value),patch.dict(os.environ,{'AGIW_ECI_TUNING_DATASET_DIR':value}):
                self.assertIn('absolute path',self.settled()['dataset']['reason'])
        source=self.write(dict(schema_version=1,runs=[],items=[]))
        target=self.directory/'target.json';source.rename(target);source.symlink_to(target)
        with patch.dict(os.environ,{'AGIW_ECI_TUNING_DATASET_DIR':str(self.directory)}):
            self.assertIn('not a link',self.settled()['dataset']['reason'])
        source.unlink();source.write_bytes(b' '*1025)
        with patch.dict(os.environ,{'AGIW_ECI_TUNING_DATASET_DIR':str(self.directory)}),patch.object(e,'MAX_DATASET_BYTES',1024):
            self.assertIn('size limit',self.settled()['dataset']['reason'])
        source.unlink();os.link(target,source)
        with patch.dict(os.environ,{'AGIW_ECI_TUNING_DATASET_DIR':str(self.directory)}):
            self.assertIn('not a link',self.settled()['dataset']['reason'])
        source.unlink();source.write_text('['*1200+'0'+']'*1200)
        with patch.dict(os.environ,{'AGIW_ECI_TUNING_DATASET_DIR':str(self.directory)}):
            self.assertEqual(self.settled()['dataset']['status'],'unknown')
        with patch.dict(os.environ,{'AGIW_ECI_TUNING_DATASET_DIR':'~missing-fixture-user'}),patch.object(Path,'expanduser',side_effect=RuntimeError('fixture')):
            self.assertEqual(self.settled()['dataset']['status'],'unknown')
        with self.assertRaises(e.ValidationError):
            e.number(10**1000,'fixture')

    def test_apply_explains_withheld_recommendation(self):
        with contextlib.redirect_stderr(io.StringIO()) as out,contextlib.redirect_stdout(io.StringIO()):
            code=e.main(['apply','--config',str(self.directory/'policy.json')])
        self.assertEqual(code,2)
        self.assertIn('No recommendation is available; policy unchanged',out.getvalue())
        self.assertNotIn('check inputs',out.getvalue())

    def test_oversize_comparison_view_is_unknown_before_pair_expansion(self):
        runs = [comparable_run('fixture-' + str(i)) for i in range(65)]
        self.write(dict(schema_version=1, runs=runs, items=[]))
        with patch.dict(os.environ, {'AGIW_ECI_TUNING_DATASET_DIR': str(self.directory)}):
            result = self.settled()
        self.assertEqual(result['dataset']['status'], 'unknown')
        self.assertIn('comparison limit', result['dataset']['reason'])

    def test_oversize_cached_response_is_withheld(self):
        self.write(dict(schema_version=1, runs=[], items=[]))
        with patch.dict(os.environ, {'AGIW_ECI_TUNING_DATASET_DIR': str(self.directory)}), patch.object(e, 'MAX_DATASET_VIEW_BYTES', 256):
            result = self.settled()
        self.assertEqual(result['dataset']['status'], 'unknown')
        self.assertIn('response size limit', result['dataset']['reason'])

    def test_loading_legacy_dataset_does_not_invent_an_ingestion_time(self):
        self.write(dict(schema_version=1, runs=[], items=[]))
        with patch.dict(os.environ, {'AGIW_ECI_TUNING_DATASET_DIR': str(self.directory)}):
            result = self.settled()
        self.assertIsNone(result['dataset']['as_of'])
        self.assertIsNone(result['dataset']['ingested_at'])

    def test_dataset_snapshot_and_ingest_timestamp_are_preserved(self):
        data=e.ingest([e.CATALOG])
        self.assertEqual(data['as_of'],'2026-11-10')
        self.assertTrue(data['ingested_at'].endswith('Z'))
        self.assertEqual(e.status(data)['dataset']['as_of'],'2026-11-10')



class FastCheckRejectionTests(unittest.TestCase):
    """One invalid fast-check record is reported, never fails the whole dataset."""

    def ingest(self, folder, document, name='dataset.json', **kwargs):
        source = Path(folder) / name
        source.write_text(json.dumps(document))
        return e.ingest([source], **kwargs)

    def test_invalid_record_is_reported_by_record_id_next_to_valid_records(self):
        from test_eci_fast_check import datasets, rehash, run
        from test_eci_fast_check_legacy import FROZEN_V1
        clean = run(datasets(6))
        broken = copy.deepcopy(FROZEN_V1)
        broken['benchmarks'][0]['unknown_finish'] = 0
        rehash(broken)
        with tempfile.TemporaryDirectory() as folder:
            data = self.ingest(folder, dict(schema_version=1, runs=[], items=[],
                                            fast_checks=[copy.deepcopy(FROZEN_V1), broken, clean]))
            self.assertEqual([r['record_id'] for r in data['fast_checks']],
                             [FROZEN_V1['record_id'], clean['record_id']])
            self.assertEqual(data['rejected_fast_checks'], [
                {'record_id': broken['record_id'], 'reason': 'summary disagrees with observations'}])
            self.assertIn('fast_check', e.rank(data))
            # A normalized dataset written from this result keeps the report.
            again = self.ingest(folder, data, name='normalized.json', require_normalized=True)
            self.assertEqual(again['rejected_fast_checks'], data['rejected_fast_checks'])

    def test_rejected_record_id_and_reason_never_echo_input_text(self):
        with tempfile.TemporaryDirectory() as folder:
            data = self.ingest(folder, dict(schema_version=1, runs=[], items=[], fast_checks=[
                {'record_id': 'private text, not a digest'}, 'not an object']))
        self.assertEqual([entry['record_id'] for entry in data['rejected_fast_checks']], [None, None])
        for entry in data['rejected_fast_checks']:
            self.assertNotIn('private', entry['reason'])

    def test_rejection_reason_is_generic_for_non_validation_errors(self):
        secret = 'private input text'
        for error in (ValueError(secret), TypeError(secret), KeyError(secret), AttributeError(secret),
                      e.ValidationError(secret)):
            with self.subTest(error=type(error).__name__):
                reason = e.rejection_reason(error)
                self.assertNotIn('private', reason)
                self.assertEqual(reason, f'invalid fast-check record ({type(error).__name__})')
        self.assertEqual(e.rejection_reason(e.fast.ValidationError('summary disagrees')), 'summary disagrees')

    def test_ingest_never_echoes_a_non_validation_error_message(self):
        from test_eci_fast_check import datasets, run
        clean = run(datasets(6))
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(e.fast, 'validate_record', side_effect=KeyError('private input text')):
            data = self.ingest(folder, dict(schema_version=1, runs=[], items=[], fast_checks=[clean]))
        self.assertEqual(data['rejected_fast_checks'], [
            {'record_id': clean['record_id'], 'reason': 'invalid fast-check record (KeyError)'}])

    def test_malformed_carried_rejections_fail_validation(self):
        for entry in ({'record_id': 'x', 'reason': 'r'}, {'record_id': None}, {'record_id': None, 'reason': ''},
                      {'record_id': None, 'reason': 'r', 'extra': 1}):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as folder, \
                    self.assertRaises(e.ValidationError):
                self.ingest(folder, dict(schema_version=1, runs=[], items=[], fast_checks=[],
                                         rejected_fast_checks=[entry]))

    def test_duplicate_valid_records_still_fail_the_dataset(self):
        from test_eci_fast_check import datasets, run
        clean = run(datasets(6))
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(e.ValidationError, 'duplicate'):
            self.ingest(folder, dict(schema_version=1, runs=[], items=[], fast_checks=[clean, clean]))


class FingerprintTests(unittest.TestCase):
    class Info:
        st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns = 1, 2, 3, 4, 5

    def test_windows_fingerprint_ignores_handle_and_creation_identity(self):
        other = self.Info()
        other.st_dev, other.st_ino, other.st_ctime_ns = 9, 8, 7
        with patch.object(e.os, 'name', 'nt'):
            self.assertEqual(e._fingerprint(self.Info()), e._fingerprint(other))
            changed = self.Info()
            changed.st_size = 99
            self.assertNotEqual(e._fingerprint(self.Info()), e._fingerprint(changed))

    def test_posix_fingerprint_still_includes_inode_and_ctime(self):
        other = self.Info()
        other.st_ino = 8
        with patch.object(e.os, 'name', 'posix'):
            self.assertNotEqual(e._fingerprint(self.Info()), e._fingerprint(other))


if __name__=='__main__':
    unittest.main()
