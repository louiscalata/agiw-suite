"""Round-two review regressions. In-memory adapters and temporary files only."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import eci_fast_check as f
import eci_fast_check_adapter as a
from test_eci_fast_check import datasets, run, observation, rehash, FakeClock
import test_eci_fast_check_adapter as adapter_fixtures
from test_eci_fast_check_adapter import Stub, completion, props, MODEL_ID


class ReviewFixTests(unittest.TestCase):
    def test_01_error_stop_resumes_with_superseding_entries(self):
        data = datasets(8)
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / 'checkpoint.jsonl'
            first = run(data, answer=lambda row: (_ for _ in ()).throw(TimeoutError()), checkpoint=checkpoint)
            self.assertEqual(first['benchmarks'][0]['stop_reason'], 'errors')
            saved = checkpoint.read_bytes()
            calls = []
            resumed = run(data, answer=lambda row: (calls.append(row['id']) or ('A', 4, 2)), checkpoint=checkpoint)
            self.assertEqual(len(calls), 8)
            self.assertEqual(resumed['benchmarks'][0]['errors'], 0)
            self.assertEqual(resumed['benchmarks'][0]['stop_reason'], 'complete')
            self.assertTrue(checkpoint.read_bytes().startswith(saved))
            entries = [json.loads(line) for line in checkpoint.read_text().splitlines()]
            self.assertEqual(len(entries), 11)
            for old, new in zip(entries[:3], entries[3:6]):
                self.assertEqual(new['supersedes_sha256'], old['entry_sha256'])
            run(data, answer=lambda row: self.fail('completed checkpoint must be reused'), checkpoint=checkpoint)

    def test_01_resume_has_a_fresh_three_error_budget_and_cli_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / 'checkpoint.jsonl'
            for _ in range(2):
                calls = []
                def error(row):
                    calls.append(row['id'])
                    raise TimeoutError()
                record = run(datasets(8), answer=error, checkpoint=checkpoint)
                self.assertEqual(len(calls), 3)
            output = io.StringIO()
            with patch.object(a, 'run', return_value=record), contextlib.redirect_stdout(output):
                code = a.main(['--base-url','fixture','--model','fixture-model','--samples','fixture.json','--output-dir','fixture-output'])
            self.assertEqual(code, 3)
            self.assertIn('Repeat the same command to resume', output.getvalue())

    def test_02_retries_counted_and_successful_attempt_timed(self):
        clock = FakeClock()
        client = a.OpenAIAdapter('http://127.0.0.1:1', MODEL_ID)
        calls = []
        def request(*args, **kwargs):
            calls.append(1)
            clock.now += 10 if len(calls) == 1 else 2
            if len(calls) == 1:
                raise TimeoutError()
            return completion()
        with patch.object(client, 'request', side_effect=request), patch.object(a.time, 'monotonic', clock):
            record = run(datasets(1), answer=lambda row: client.answer(row, benchmark='arc', spec={}), clock=clock)
        bench = record['benchmarks'][0]
        self.assertEqual(bench['observations'][0].get('attempts'), 2)
        self.assertEqual(bench.get('retried'), 1)
        self.assertEqual(bench['answer_seconds'], 2)
        self.assertEqual(bench['tokens_per_s'], 3.5)
        self.assertEqual(record['wall_time_s'], 12)

    def test_02_failed_retries_are_visible(self):
        client = a.OpenAIAdapter('http://127.0.0.1:1', MODEL_ID)
        with patch.object(client, 'request', side_effect=TimeoutError()):
            record = run(datasets(1), answer=lambda row: client.answer(row, benchmark='arc', spec={}))
        self.assertEqual(record['benchmarks'][0]['observations'][0].get('attempts'), 2)
        self.assertEqual(record['benchmarks'][0].get('retried'), 1)

    def test_03_machine_labels_exclude_hosts_and_addresses(self):
        # Never read or embed the real machine name in this regression.
        with patch.object(a.socket, 'gethostname', return_value='Example-Orchid.example.invalid'):
            for value in ('example-orchid.example.invalid', 'EXAMPLE-ORCHID', 'example.local',
                          'example.LAN', 'example.home', 'example.HOME.', '2001:db8::1', '::', '192.0.2.17'):
                with self.subTest(value=value):
                    self.assertIsNone(a.safe_text(value))
            self.assertEqual(a.safe_text('example-profile-a'), 'example-profile-a')

    def test_04_transport_failure_is_unknown_not_a_parser_rejection(self):
        evidence = dict(planned=200, attempted=200, correct=150, parsed=150, errors=50,
                        truncations=0, chance='0.25', answer_options=4,
                        top_answer_count=40, unknown_finish=0)
        result = f.post_check([evidence])
        self.assertEqual(result['decision'], 'UNKNOWN')
        self.assertTrue(any('Errors exceed' in reason for reason in result['reasons']))
        self.assertFalse(any('could not be parsed' in reason for reason in result['reasons']))

    def test_04_errors_are_missing_pairs_not_wrong_answers(self):
        data = datasets(200)
        plan = f.make_plan(data)
        ids = set(plan['benchmarks'][0]['item_ids'][::4])
        def answer(row):
            return observation('error', error='TimeoutError') if row['id'] in ids else observation('correct')
        candidate = run(data, plan, answer)
        baseline = run(data, plan)
        verdict, details = f.pair_verdict(candidate, baseline)
        pair = details['arc']
        self.assertEqual(pair['baseline_only'], 0)
        self.assertEqual(pair['n_shared'], 150)
        self.assertEqual(pair['reason'], 'incomplete pairs')
        self.assertEqual(verdict, 'INCONCLUSIVE')
        self.assertEqual(f.screening_evidence(candidate)[0]['unparsed'], 0)

    def test_06_server_size_is_separate_from_declared_file_size(self):
        fixtures = adapter_fixtures.AdapterTests()
        with fixtures.tempdir() as directory, Stub(properties={}, get_status=404,
                model_meta={'size':101, 'ftype':'Q4_K_M'}) as stub:
            record = fixtures.run_fixture(stub, [fixtures.sample(directory)], Path(directory)/'out')
            self.assertEqual(record['model']['file_size_bytes'], 100)
            self.assertEqual(record['provenance']['declared_file_size_bytes'], 100)
            self.assertEqual(record['provenance']['server_reported_size_bytes'], 101)
            self.assertEqual(record['provenance']['size_match'], 'unverified')
        with Stub(model_meta={'size':100}) as stub:
            model, _ = a.OpenAIAdapter(stub.url, MODEL_ID).discover()
            self.assertIsNone(model['file_size_bytes'])

    def test_07_gsm_percent_scores_number_and_keeps_unit(self):
        for content, unit in [('#### 12%', '%'), ('Answer: 12 percent', 'percent')]:
            with self.subTest(content=content), Stub(replies=[completion(content)]) as stub:
                result = a.OpenAIAdapter(stub.url, MODEL_ID).answer(
                    dict(id='one', question='Example calculation.', answer='12'), benchmark='gsm8k', spec={})
                self.assertEqual(result['outcome'], 'correct')
                self.assertEqual(result['answer_value'], '12')
                self.assertEqual(result['parsed_unit'], unit)

    def test_08_cli_floor_rejects_before_run(self):
        with patch.object(a, 'run') as run_mock, contextlib.redirect_stderr(io.StringIO()):
            code = a.main(['--base-url','fixture','--model','fixture-model','--samples','fixture.json',
                           '--output-dir','fixture-output','--max-tokens','8191'])
        self.assertEqual(code, 2)
        run_mock.assert_not_called()

    def test_08_context_cap_leaves_prompt_headroom(self):
        properties = props()
        properties['default_generation_settings']['n_ctx'] = 4096
        with Stub(properties=properties) as stub:
            client = a.OpenAIAdapter(stub.url, MODEL_ID)
            client.discover()
            client.answer(dict(id='one', question='Example calculation.', answer='A'), benchmark='arc', spec={})
            payload = stub.posts[0]
            content_bytes = sum(len(message['content'].encode('utf-8')) for message in payload['messages'])
            self.assertLess(payload['max_tokens'], 4096 - content_bytes)
            self.assertGreater(payload['max_tokens'], 0)

    def test_10_validator_rejects_continuation_after_three_errors(self):
        record = run(datasets(8))
        spec = record['plan']['benchmarks'][0]
        rows = copy.deepcopy(record['benchmarks'][0]['observations'])
        for i in range(3):
            rows[i] = dict(rows[i], correct=False, **observation('error', error='TimeoutError'))
        record['benchmarks'] = [f.summarize(spec, rows, 0, 'complete')]
        record['comparison'] = f.comparisons(record, None, record['alpha'])
        record['post'] = f.post_check(f.post_inputs(record), alpha=record['alpha'])
        rehash(record)
        with self.assertRaisesRegex(f.ValidationError, 'consecutive error'):
            f.validate_record(record)

    def test_resume_across_benchmarks_survives_interrupted_supersession(self):
        data = datasets(2)
        data['winogrande'] = copy.deepcopy(data['arc'])
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory)/'checkpoint.jsonl'
            first = run(data, answer=lambda row: (_ for _ in ()).throw(TimeoutError()), checkpoint=checkpoint)
            self.assertEqual([b['attempted'] for b in first['benchmarks']], [2, 1])
            append = f.checkpoint_append
            def interrupted(*args):
                append(*args)
                raise KeyboardInterrupt()
            with patch.object(f, 'checkpoint_append', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
                run(data, checkpoint=checkpoint)
            calls = []
            recovered = run(data, answer=lambda row: (calls.append(row['id']) or ('A',4,2)), checkpoint=checkpoint)
            self.assertEqual(len(calls), 3)
            self.assertEqual([b['errors'] for b in recovered['benchmarks']], [0, 0])
            run(data, answer=lambda row: self.fail('must reuse'), checkpoint=checkpoint)

    def test_checkpoint_cannot_supersede_success_or_nontrailing_error(self):
        for error_first in (False, True):
            with self.subTest(error_first=error_first), tempfile.TemporaryDirectory() as directory:
                checkpoint = Path(directory)/'checkpoint.jsonl'
                calls = []
                def answer(row):
                    calls.append(row['id'])
                    return observation('error' if error_first and len(calls) == 1 else 'correct')
                run(datasets(3), answer=answer, checkpoint=checkpoint)
                entries = [json.loads(line) for line in checkpoint.read_text().splitlines()]
                forged = copy.deepcopy(entries[0])
                forged['previous_sha256'] = entries[-1]['entry_sha256']
                forged['supersedes_sha256'] = entries[0]['entry_sha256']
                forged['entry_sha256'] = f.digest({k:v for k,v in forged.items() if k != 'entry_sha256'})
                with checkpoint.open('a') as stream:
                    stream.write(f.canonical(forged)+'\n')
                with self.assertRaisesRegex(f.ValidationError, 'supersession'):
                    run(datasets(3), checkpoint=checkpoint, answer=lambda row: self.fail('must refuse before calls'))

    def test_error_rows_excluded_from_both_sides_of_unparsed_rate(self):
        evidence = dict(planned=200, attempted=200, correct=170, parsed=179, errors=2,
                        truncations=0, chance='0.25', answer_options=4,
                        top_answer_count=45, unknown_finish=0)
        self.assertEqual(f.post_check([evidence])['decision'], 'ADMIT')
        # 20 unparsed / 198 responses is just over 10%, unlike 20 / 200.
        result = f.post_check([dict(evidence, parsed=178)])
        self.assertEqual(result['decision'], 'REJECT')
        self.assertTrue(any('could not be parsed' in reason for reason in result['reasons']))

    def test_server_size_alone_cannot_supply_file_fit(self):
        fixtures = adapter_fixtures.AdapterTests()
        with fixtures.tempdir() as directory, Stub() as stub:
            sample = fixtures.sample(directory)
            with self.assertRaisesRegex(f.ValidationError, 'fit gate'):
                a.run(stub.url, MODEL_ID, [sample], Path(directory)/'out')
            self.assertEqual(stub.posts, [])
            record = fixtures.run_fixture(stub, [sample], Path(directory)/'out')
            self.assertEqual(record['provenance']['size_match'], 'equal')

    def test_prompt_without_context_headroom_makes_no_completion_call(self):
        client = a.OpenAIAdapter('http://127.0.0.1:1', MODEL_ID, max_tokens=8192)
        client.ctx = 32
        with patch.object(client,'request') as request, self.assertRaisesRegex(f.ValidationError,'prompt estimate'):
            client.answer(dict(id='one',question='Example '*100,answer='A'),benchmark='arc',spec={})
        request.assert_not_called()

    def test_incomplete_error_stop_keeps_overall_quality_unknown(self):
        complete = dict(planned=200,attempted=200,correct=0,parsed=0,errors=0,
                        truncations=0,chance='0.25',answer_options=4,
                        top_answer_count=0,unknown_finish=0)
        stopped = dict(complete,attempted=3,errors=3)
        self.assertEqual(f.post_check([complete,stopped])['decision'],'UNKNOWN')




if __name__ == '__main__':
    unittest.main()
