"""Model selection: explicit model, tiny-model refusal and a discoverable node binary."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agiw import fleet_local_pilot as pilot

ROOT = Path(pilot.__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'preview-pilot'))
import once_common  # noqa: E402


class ModelSelectionTests(unittest.TestCase):
    def test_live_mode_requires_explicit_model(self):
        with patch.object(sys, 'argv', ['prog', '--mode', 'live']):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = pilot.main()
        result = __import__('json').loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(result['outcome'], 'BLOCKED')
        self.assertIn('MODEL', result['reason'])

    def test_tiny_model_refused_without_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            model = Path(td) / 'SmolLM2-135M-Instruct-Q4_K_M.gguf'
            model.write_bytes(b'tiny fixture weights')
            with self.assertRaises(once_common.Refused):
                once_common.model_metadata(str(model))
            meta = once_common.model_metadata(str(model), allow_tiny=True)
            self.assertEqual(meta['basename'], model.name)
            self.assertEqual(meta['size_bytes'], model.stat().st_size)
            self.assertEqual(len(meta['sha256']), 64)

    def test_model_path_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(once_common.Refused):
                once_common.model_metadata(None)
            with self.assertRaises(once_common.Refused):
                once_common.resolve_server(None)

    def test_missing_node_is_refused(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
                pilot.shutil, 'which', return_value=None):
            with self.assertRaises(pilot.Refused):
                pilot.node_binary()

    def test_node_from_env(self):
        with patch.dict(os.environ, {'AGIW_NODE': '/fixture/node'}):
            self.assertEqual(pilot.node_binary(), '/fixture/node')

    def test_no_hardcoded_private_paths_in_scripts(self):
        for name in ('tiny-arithmetic-once.py', 'tiny-extraction-once.py', 'tiny-live-once.py'):
            body = (ROOT / 'preview-pilot' / name).read_text()
            self.assertNotIn('/Users/', body)
            self.assertNotIn('/usr/local/bin/node', body)
            self.assertNotIn('SmolLM2-135M-Instruct', body)
        helper = (ROOT / 'preview-pilot' / 'once_common.py').read_text()
        self.assertIn('--model-path', helper)
        self.assertIn('--llama-server', helper)


if __name__ == '__main__':
    unittest.main()
