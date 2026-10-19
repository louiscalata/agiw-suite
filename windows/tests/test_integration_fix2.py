"""Linked queue directories cannot establish exact queue activity."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from agiw_win import probes


class QueueDirectoryEvidence(unittest.TestCase):
    def assert_unknown(self, root):
        pipeline, source = probes.route_queue(root, 1_800_000_000)
        self.assertIn(pipeline['status'], (None, 'unknown'))
        self.assertIs(pipeline['pipelinesComplete'], False)
        self.assertIsNone(pipeline['queue'])  # No exact counts, including zeroes.
        self.assertEqual(source['state'], 'unavailable')

    @unittest.skipIf(os.name == 'nt', 'POSIX symlinks; Windows reparse metadata covered separately')
    def test_linked_queue_root_and_each_state_are_unknown(self):
        for linked in ('root', 'pending', 'claimed', 'completed'):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root, target = base / 'queue', base / 'target'
                target.mkdir()
                root.mkdir()
                for state in ('pending', 'claimed', 'completed'):
                    (root / state).mkdir()
                path = root if linked == 'root' else root / linked
                if linked == 'root':
                    for state in ('pending', 'claimed', 'completed'):
                        (path / state).rmdir()
                        (target / state).mkdir()
                path.rmdir()
                path.symlink_to(target, target_is_directory=True)
                self.assert_unknown(root)

    def test_reparse_queue_root_and_each_state_are_unknown(self):
        for linked in ('root', 'pending', 'claimed', 'completed'):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'queue'
                root.mkdir()
                for state in ('pending', 'claimed', 'completed'):
                    (root / state).mkdir()
                path = root if linked == 'root' else root / linked
                lstat = os.lstat

                def reparse(candidate, *args, **kwargs):
                    info = lstat(candidate, *args, **kwargs)
                    if Path(candidate) == path:
                        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                    return info

                # Same FILE_ATTRIBUTE_REPARSE_POINT emulation as the run-entry matrix;
                # exercise the real is_link_or_junction, rather than mocking its verdict.
                with mock.patch.object(probes.os, 'lstat', side_effect=reparse):
                    self.assert_unknown(root)

    def test_absent_queue_is_unknown_and_complete_empty_queue_is_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'queue'
            self.assert_unknown(root)
            root.mkdir()
            self.assert_unknown(root)  # sol10-A1 #2: all state directories absent.
            for state in ('pending', 'claimed', 'completed'):
                (root / state).mkdir()
            pipeline, source = probes.route_queue(root, 1_800_000_000)
            self.assertEqual(pipeline['status'], 'idle')
            self.assertIs(pipeline['pipelinesComplete'], True)
            self.assertIs(pipeline['queue']['complete'], True)
            for state in ('pending', 'claimed', 'completed'):
                self.assertEqual(pipeline['queue'][state], 0)
            self.assertEqual(source['state'], 'live')
