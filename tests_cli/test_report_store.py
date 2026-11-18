"""Fixture-mode execution persists a verifiable Nisi report inside the store dir."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agiw.fleet_local_pilot import ROOT, execute, Refused

NISI_AVAILABLE = (
    (ROOT / 'preview-pilot/vendor/nisi-0.3.1/workflow/engine.mjs').is_file()
    and shutil.which('node') is not None
)


@unittest.skipUnless(NISI_AVAILABLE, 'pinned Nisi vendor or node missing')
class ReportStoreTests(unittest.TestCase):
    def test_fixture_stores_report_in_temp_dir(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / 'store'
            result = execute(
                'fixture', 'http://127.0.0.1:8081/v1/chat/completions',
                'fixture-only', report_store=str(store),
            )
            self.assertTrue(result['reportStored'])
            self.assertEqual(result['report']['outcome'], 'COMPLETED')
            path = Path(result['reportPath'])
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, store.resolve())
            written = json.loads(path.read_text())
            self.assertEqual(written['runId'], result['runId'])
            self.assertEqual(written['outcome'], result['report']['outcome'])

    def test_store_dir_is_private(self):
        import os
        import stat
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / 'store'
            execute('fixture', 'http://127.0.0.1:8081/v1/chat/completions',
                    'fixture-only', report_store=str(store))
            self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o700)
            for p in store.iterdir():
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_bad_program_refused(self):
        with self.assertRaises(Refused):
            execute('fixture', 'http://127.0.0.1:8081/v1/chat/completions',
                    'fixture-only', program='evil.mjs')


if __name__ == '__main__':
    unittest.main()
