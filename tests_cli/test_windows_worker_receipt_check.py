import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agiw.windows_worker_receipt_check import evaluate, read_regular, parse_receipt, main

class ReceiptCheckTests(unittest.TestCase):
    def setUp(self):
        self.r={'schema_version':1,'updated_unix':99,'status':'running','models':['fixture-model'],'worker_version':'1.3'}
    def check(self, **kwargs):
        return evaluate(self.r, expected_model='fixture-model', now=100, **kwargs)
    def test_fresh_claim_never_admits(self):
        r=self.check(); self.assertTrue(r['exactModelAdvertised']); self.assertTrue(r['workerVersionClaimPresent'])
        self.assertFalse(r['admission']); self.assertEqual(r['workerDeployment'],'UNVERIFIED')
    def test_observer_badge_is_not_worker(self):
        for update in ({'scope':'local-observer'}, {'workerVersion':'win-1.1.0'}):
            self.r.update(update); self.assertEqual(self.check()['heartbeatClaim'],'OBSERVER_NOT_WORKER_PROOF')
    def test_stale_future_and_bad_timestamp(self):
        for value in (0,101,None,True,float('nan'),10**400):
            self.r['updated_unix']=value; self.assertFalse(self.check()['exactModelAdvertised'])
    def test_schema_is_exact_integer(self):
        for v in (True,'1',2,None):
            self.r['schema_version']=v; self.assertEqual(self.check()['heartbeatClaim'],'INVALID')
    def test_not_running(self):
        self.r['status']='stopped'; self.assertEqual(self.check()['heartbeatClaim'],'NOT_RUNNING')
    def test_model_exact_no_alias_or_case_guess(self):
        for models in (['Fixture-model'], ['other'], 'fixture-model', [None], ['fixture-model']*65):
            self.r['models']=models; self.assertFalse(self.check()['exactModelAdvertised'])
    def test_digest_match_is_not_deployment(self):
        r=self.check(actual_digest='a'*64,expected_digest='a'*64)
        self.assertEqual(r['localSourceDigest'],'MATCH'); self.assertFalse(r['admission'])
        self.assertEqual(r['workerDeployment'],'UNVERIFIED')
    def test_digest_failure_states(self):
        for args,want in (({'expected_digest':'bad'},'INVALID_BASELINE'),({'expected_digest':'a'*64},'SOURCE_NOT_READ'),({'actual_digest':'b'*64,'expected_digest':'a'*64},'MISMATCH')):
            self.assertEqual(self.check(**args)['localSourceDigest'],want)
    def test_untrusted_digest_in_receipt_ignored(self):
        self.r['sha256']='a'*64; self.assertEqual(self.check()['localSourceDigest'],'NOT_CHECKED')
    def test_duplicate_and_nonfinite_json_refused(self):
        for data in (b'{"a":1,"a":2}',b'{"a":NaN}',b'{'):
            with self.assertRaises(ValueError): parse_receipt(data)
    def test_safe_bounded_local_read(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td).resolve()/'r';p.write_bytes(b'{}');self.assertEqual(read_regular(p,2),b'{}')
            with self.assertRaises(ValueError): read_regular(p,1)
            with self.assertRaises(ValueError): read_regular(Path(td).resolve(),20)
    def test_cli_redacts_payload_and_paths(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td).resolve()/'receipt'; self.r['output']='SECRET_FIXTURE';p.write_text(json.dumps(self.r))
            out=io.StringIO()
            with contextlib.redirect_stdout(out), patch('agiw.windows_worker_receipt_check.time.time',return_value=100):
                self.assertEqual(main(['--receipt',str(p),'--expected-model','fixture-model']),0)
            self.assertNotIn(td,out.getvalue());self.assertNotIn('SECRET_FIXTURE',out.getvalue())
            self.assertFalse(json.loads(out.getvalue())['admission'])
    def test_cli_malformed_redacted(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td).resolve()/'bad';p.write_text('{');out=io.StringIO()
            with contextlib.redirect_stdout(out): self.assertEqual(main(['--receipt',str(p),'--expected-model','x']),2)
            self.assertNotIn(td,out.getvalue())

if __name__ == '__main__': unittest.main()
