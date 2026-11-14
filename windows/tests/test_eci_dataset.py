"""Windows endpoint dataset contract, exercised without a listening socket."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

WINDOWS = Path(__file__).resolve().parents[1]
ROOT = WINDOWS.parent
sys.path.insert(0, str(WINDOWS))
from agiw_win import server  # noqa: E402
import eci_tuning  # noqa: E402


class Endpoint:
    """Only the handler's HTTP boundary is faked; status and ingest are real."""
    path = '/api/eci-tuning'

    def same_origin(self):
        return True

    def json_reply(self, code, value):
        # Enforce the real JSON encoder's finite-number contract.
        self.response = (code, json.loads(json.dumps(value, allow_nan=False)))
        return self.response


def get_endpoint():
    # The dataset is validated by a background worker and the first answers may say "pending"
    # (the wait is 0.1 s by design); a loaded runner needs a few more polls, so ask again.
    deadline = time.monotonic() + 30
    while True:
        code, body = server.Handler.do_GET(Endpoint())
        dataset = body.get('dataset') if isinstance(body, dict) else None
        if not (isinstance(dataset, dict) and dataset.get('pending')) or time.monotonic() > deadline:
            return code, body
        time.sleep(0.05)


class DatasetEndpointTests(unittest.TestCase):
    def test_windows_server_and_dataset_import_without_fcntl(self):
        # A fresh interpreter prevents a prior import from masking a dependency.
        program = '''
import builtins
import os
import sys
real_import = builtins.__import__
def without_fcntl(name, *args, **kwargs):
    if name == "fcntl" or name.startswith("fcntl."):
        raise ModuleNotFoundError("fcntl deliberately unavailable")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_fcntl
sys.modules.pop("fcntl", None)
sys.path.insert(0, "windows")
import eci_tuning
from agiw_win import server
os.environ.pop('AGIW_ECI_TUNING_DATASET_DIR', None)
os.environ['AGIW_ECI_TUNING_CONFIG'] = str(eci_tuning.ROOT / 'test_support/eci_tuning/absent-policy.json')
assert eci_tuning.status()['dataset']['status'] == 'bundled'
assert callable(server.Handler.do_GET)
'''
        result = subprocess.run([sys.executable, '-B', '-c', program], cwd=str(ROOT),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ingest_to_endpoint_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = ROOT / 'test_support' / 'eci_tuning' / 'results.json'
            ingested = eci_tuning.ingest([source])
            # The producer uses a fake adapter and fake clock; no model or port is used.
            from test_eci_fast_check import run
            record = run()
            raw = directory / 'input.json'
            raw.write_text(json.dumps(dict(ingested, fast_checks=[record])), encoding='utf-8')
            ingested = eci_tuning.ingest([raw])
            (directory / 'dataset.json').write_text(json.dumps(ingested), encoding='utf-8')
            env = {'AGIW_ECI_TUNING_DATASET_DIR': str(directory),
                   'AGIW_ECI_TUNING_CONFIG': str(directory / 'absent-policy.json')}
            with mock.patch.dict(os.environ, env):
                code, body = get_endpoint()
                self.assertEqual(code, 200)
                self.assertEqual(body['dataset']['status'], 'configured')
                rows = body['rankings']['baseline_protocol']['eci']
                self.assertEqual([row['run_id'] for row in rows], ['pc-llama31-baseline-max1024'])
                self.assertAlmostEqual(rows[0]['eci_posterior_mean'], 105.37592661908786)
                screening = body['rankings']['fast_check']['hardware_views'][0]['rows'][0]
                self.assertEqual(screening['record_id'], record['record_id'])
                self.assertEqual(screening['full_run'], 'NOT_RUN')
                self.assertNotIn('eci_posterior_mean', screening)
                self.assertNotIn(record['record_id'], [row['run_id'] for row in rows])
                (directory / 'dataset.json').write_text('{invalid', encoding='utf-8')
                code, body = get_endpoint()
                self.assertEqual(code, 200)
                self.assertEqual(body['dataset']['status'], 'unknown')
                self.assertTrue(body['dataset']['reason'])
                self.assertFalse(body.get('rankings'))

    def test_unconfigured_uses_bundled_catalog(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, clear=False):
            os.environ.pop('AGIW_ECI_TUNING_DATASET_DIR', None)
            os.environ['AGIW_ECI_TUNING_CONFIG'] = str(Path(folder) / 'absent-policy.json')
            code, body = get_endpoint()
            self.assertEqual(code, 200)
            self.assertEqual(body['dataset']['status'], 'bundled')
            self.assertTrue(body['rankings']['baseline_protocol']['eci'])


if __name__ == '__main__':
    unittest.main()
