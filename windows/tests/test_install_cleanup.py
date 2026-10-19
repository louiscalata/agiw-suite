"""Portable replay of Windows' read-only unlink behavior, including unittest outcomes."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from tests import install_helpers as helpers


@contextmanager
def windows_readonly_deletes(locked_name=None):
    """Use real rmtree traversal; reject non-write-bit files as Windows does."""
    denied = []

    def guarded(operation):
        def remove(path, *, dir_fd=None):
            mode = os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode
            if not mode & stat.S_IWRITE or Path(path).name == locked_name:
                denied.append(str(path))
                raise PermissionError(13, "simulated Windows read-only/locked path", str(path))
            if dir_fd is None:
                return operation(path)
            return operation(path, dir_fd=dir_fd)
        return remove

    with mock.patch.object(os, 'unlink', new=guarded(os.unlink)), \
            mock.patch.object(os, 'rmdir', new=guarded(os.rmdir)):
        yield denied


def readonly_object(root, name='object'):
    path = root / 'source-repo/.git/objects/ab' / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b'git loose object fixture')
    path.chmod(stat.S_IREAD)
    return path


class InstallRootCleanup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profile = Path(temporary.name)

    def run_case(self, body, *, locked_name=None):
        class FixtureCase(unittest.TestCase):
            def runTest(case):
                body(case)

        result = unittest.TestResult()
        with mock.patch.object(helpers.Path, 'home', return_value=self.profile), \
                windows_readonly_deletes(locked_name) as denied:
            FixtureCase().run(result)
        return result, denied

    def test_old_teardown_raises_and_leaves_readonly_object(self):
        # The exact pre-fix lifetime: finally/rmtree on context exit.
        @contextmanager
        def old_root():
            root = self.profile / 'old-root'
            root.mkdir()
            try:
                yield root
            finally:
                shutil.rmtree(root)

        with windows_readonly_deletes() as denied:
            with self.assertRaises(PermissionError):
                with old_root() as root:
                    path = readonly_object(root)
        self.assertTrue(denied)
        self.assertTrue(path.exists())
        helpers.remove_tree(root)

    def test_readonly_git_object_is_removed_after_assertions(self):
        roots = []
        def body(case):
            with helpers.install_root(case) as root:
                roots.append(root)
                path = readonly_object(root)
            case.assertTrue(path.exists(), 'context exit must not tear down before assertions')
        result, denied = self.run_case(body)
        self.assertTrue(denied, 'the simulation must actually refuse a deletion')
        self.assertEqual((len(result.failures), len(result.errors)), (0, 0))
        self.assertFalse(roots[0].exists())

    def test_assertion_inside_context_stays_a_failure(self):
        roots = []
        def body(case):
            with helpers.install_root(case) as root:
                roots.append(root)
                readonly_object(root)
                case.fail('original assertion inside install_root')
        result, denied = self.run_case(body)
        self.assertTrue(denied)
        self.assertEqual((len(result.failures), len(result.errors)), (1, 0))
        self.assertIn('original assertion inside install_root', result.failures[0][1])
        self.assertFalse(roots[0].exists())

    def test_failed_retry_reports_cleanup_error_and_preserves_assertion(self):
        roots = []
        def body(case):
            with helpers.install_root(case) as root:
                roots.append(root)
                readonly_object(root, 'locked')
                case.fail('assertion must survive a permanent cleanup failure')
        result, denied = self.run_case(body, locked_name='locked')
        self.assertGreaterEqual(len(denied), 2, 'initial deletion and retry must both fail')
        self.assertEqual((len(result.failures), len(result.errors)), (1, 1))
        self.assertIn('assertion must survive', result.failures[0][1])
        self.assertIn('PermissionError', result.errors[0][1])
        self.assertTrue(roots[0].exists())
        helpers.remove_tree(roots[0])

    def test_cleanup_error_after_success_is_not_swallowed(self):
        roots = []
        def body(case):
            with helpers.install_root(case) as root:
                roots.append(root)
                readonly_object(root, 'locked')
            case.assertTrue(root.exists())
        result, _ = self.run_case(body, locked_name='locked')
        self.assertEqual((len(result.failures), len(result.errors)), (0, 1))
        helpers.remove_tree(roots[0])

    def test_legacy_onerror_removes_readonly_git_object(self):
        root = self.profile / 'legacy'
        root.mkdir()
        readonly_object(root)
        real_rmtree = shutil.rmtree
        # Supply the older API signature even on a host that also offers onexc.
        def legacy_rmtree(path, *, onerror):
            return real_rmtree(path, onerror=onerror)
        with windows_readonly_deletes() as denied, \
                mock.patch.object(helpers.sys, 'version_info', (3, 11)), \
                mock.patch.object(helpers.shutil, 'rmtree', new=legacy_rmtree):
            helpers.remove_tree(root)
        self.assertTrue(denied)
        self.assertFalse(root.exists())

    def test_readonly_directory_retries_rmdir(self):
        root = self.profile / 'readonly-directory'
        root.mkdir()
        root.chmod(stat.S_IREAD | stat.S_IEXEC)
        with windows_readonly_deletes() as denied:
            helpers.remove_tree(root)
        self.assertTrue(denied)
        self.assertFalse(root.exists())

    def test_unrelated_errors_are_not_retried(self):
        error = OSError('unrelated traversal failure')
        with mock.patch.object(helpers.os, 'chmod') as chmod:
            with self.assertRaises(OSError) as caught:
                helpers._retry_readonly(os.unlink, 'unused', error)
        self.assertIs(caught.exception, error)
        chmod.assert_not_called()
