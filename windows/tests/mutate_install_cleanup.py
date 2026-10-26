"""Portable handler-removal mutation; run from windows with python -B tests/mutate_install_cleanup.py.

The mutation replaces remove_tree with bare rmtree, removing both retry APIs.
Only assertion failures kill it; syntax/import/cleanup errors never count as kills.
No production file is edited, and no native signature verdict is implied.
"""
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import install_helpers as helpers
from tests.test_install_cleanup import InstallRootCleanup


def run():
    names = ('test_readonly_git_object_is_removed_after_assertions',
             'test_assertion_inside_context_stays_a_failure')
    output = io.StringIO()
    suite = unittest.TestSuite(InstallRootCleanup(name) for name in names)
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    return {'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'skipped': len(result.skipped), 'killed': bool(result.failures) and not result.errors,
            'log': output.getvalue()}


def main():
    reports = {'cleanup-control': run()}
    with mock.patch.object(helpers, 'remove_tree', new=shutil.rmtree):
        reports['cleanup-handler-removed'] = run()
    print(json.dumps(reports, indent=2))
    control = reports['cleanup-control']
    mutant = reports['cleanup-handler-removed']
    return 0 if (control['run'] == 2 and not control['failures'] and not control['errors']
                 and not control['skipped'] and mutant['run'] == 2
                 and not mutant['skipped'] and mutant['killed']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
