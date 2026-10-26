"""Bounded in-memory mutations. Run from windows: python -B tests/mutate_fix2.py.

No production file is edited. Signature mutations require native Windows/PowerShell;
skipped tests are reported as unexecuted, never as killed mutations.
"""
import ast
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes
from tests import test_build_and_feed as build_tests
from tests.test_integration_fix2 import QueueDirectoryEvidence


def run(suite):
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    return {'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'skipped': len(result.skipped), 'killed': bool(result.failures) and not result.errors,
            'log': output.getvalue()}


def queue_suite():
    return unittest.defaultTestLoader.loadTestsFromTestCase(QueueDirectoryEvidence)


def main():
    reports = {'queue-control': run(queue_suite())}
    source = Path(probes.__file__).read_text(encoding='utf-8')
    source = ast.get_source_segment(source, next(node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == 'route_queue'))
    for name, before, after in (
        ('queue-root-guard-removed', 'is_link_or_junction(root) or not root.is_dir()', 'not root.is_dir()'),
        ('queue-state-guard-removed', 'if is_link_or_junction(directory):', 'if False:'),
    ):
        assert source.count(before) == 1, name
        parsed = ast.parse(source.replace(before, after))
        function = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'route_queue')
        namespace = dict(probes.__dict__)
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<' + name + '>', 'exec'), namespace)
        with mock.patch.object(probes, 'route_queue', namespace['route_queue']):
            reports[name] = run(queue_suite())
    names = (
        'test_crlf_staged_payload_uses_declared_git_text_normalization',
        'test_private_enrollment_does_not_invalidate_a_signed_package',
        'test_staged_bytes_are_checked_against_head_even_when_size_is_unchanged',
    )
    def signature_suite():
        return unittest.TestSuite(build_tests.InstallerOnWindows(name) for name in names)
    reports['signature-control'] = run(signature_suite())
    original = build_tests.INSTALLER
    call = "        & { $ErrorActionPreference = 'Continue'; & $GitPath -C $Repo verify-commit $Commit 2>$null } | Out-Null"
    assert original.count(call) == 1
    for name, replacement in (('signature-call-removed', ''),
                              ('signature-check-bypassed', '        $global:LASTEXITCODE = 0')):
        with mock.patch.object(build_tests, 'INSTALLER', original.replace(call, replacement)):
            reports[name] = run(signature_suite())
    print(json.dumps(reports, indent=2))
    if any(reports[name]['skipped'] for name in reports if name.startswith('signature-')):
        return 2  # Native mutation evidence still required.
    controls = [value for key, value in reports.items() if key.endswith('-control')]
    mutants = [value for key, value in reports.items() if not key.endswith('-control')]
    return 0 if all(not r['failures'] and not r['errors'] for r in controls) and all(r['killed'] for r in mutants) else 1


if __name__ == '__main__':
    raise SystemExit(main())
