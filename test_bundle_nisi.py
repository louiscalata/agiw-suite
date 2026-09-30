import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import bundle_nisi as bundle


class BundleNisiTests(unittest.TestCase):
    def test_exact_payload_and_copy(self):
        self.assertEqual(bundle.verify()['version'], '0.2.0')
        self.assertEqual(len(bundle.resource_inputs()), 19)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve() / 'nisi'
            self.assertEqual(bundle.copy_bundle(target)['status'], 'PASS')
            self.assertEqual(bundle.verify(target), bundle.verify())
            with self.assertRaises(ValueError):
                bundle.copy_bundle(target)

    def test_tamper_extras_and_symlink_refused(self):
        for mutation in ('tamper', 'extra', 'directory', 'symlink', 'manifest'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                target = Path(directory).resolve() / 'nisi'
                bundle.copy_bundle(target)
                if mutation == 'tamper':
                    (target / 'package' / 'index.mjs').write_text('changed')
                elif mutation == 'extra':
                    (target / 'extra').write_text('extra')
                elif mutation == 'directory':
                    (target / 'unexpected').mkdir()
                elif mutation == 'symlink':
                    (target / 'package' / 'index.mjs').unlink()
                    (target / 'package' / 'index.mjs').symlink_to(bundle.SOURCE / 'package' / 'index.mjs')
                else:
                    (target / 'manifest.json').write_text('{}')
                with self.assertRaises(ValueError):
                    bundle.verify(target)

    def test_list_inputs(self):
        result = subprocess.run(['python3', str(Path(bundle.__file__)), '--list-inputs'], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 19)
        self.assertIn('vendor/nisi/manifest.json', result.stdout.splitlines())

    def test_destination_symlink_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            (parent / 'link').symlink_to(parent, target_is_directory=True)
            with self.assertRaises(ValueError):
                bundle.copy_bundle(parent / 'link' / 'nisi')

    def test_real_node_version_and_model_free_demo(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node not installed')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve() / 'nisi'
            bundle.copy_bundle(target)
            cli = target / 'package' / 'bin' / 'nisi.mjs'
            version = subprocess.run([node, str(cli), '--version'], capture_output=True, text=True, timeout=15)
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout.strip(), '0.2.0')
            demo = subprocess.run([node, str(cli), 'demo'], cwd=directory, capture_output=True, text=True, timeout=30)
            self.assertEqual(demo.returncode, 0, demo.stderr)
            result = json.loads(demo.stdout)
            self.assertEqual(result['outcome'], 'COMPLETED')
            self.assertEqual(result['modelCalls'], 0)
            self.assertTrue(result['reportStored'])


if __name__ == '__main__':
    unittest.main()
