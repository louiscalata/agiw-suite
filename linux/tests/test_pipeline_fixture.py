"""The generated contract must not depend on host state or a symlinked TMPDIR."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import generate_pipeline_cases  # Select the local product source before imports.
from agiw_linux import linsys, probes, server
from pipeline_fixture import AmbientAccess, NOW, fixture_environment, fixture_path
from raw_pipeline_cases import mutate
import test_fixture_observer


class PipelineFixtureTests(unittest.TestCase):
    def test_symlink_tmpdir_is_physical_and_history_stays_measured(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            (base / 'actual').mkdir()
            (base / 'alias').symlink_to(base / 'actual')
            with patch.object(tempfile, 'tempdir', str(base / 'alias')), fixture_environment():
                path = fixture_path('last-code.log')
                self.assertEqual(path.parent, path.parent.resolve(strict=True))
                path.write_text('prompt eval time = 1 ms (1 ms per token, 80 tokens per second)\n'
                                'eval time = 1 ms (1 ms per token, 30 tokens per second)\n', encoding='utf-8')
                for age in (.001, 1, 86400):
                    os.utime(path, (NOW - age, NOW - age))
                    row = probes.last_rates(path, NOW)
                    self.assertEqual(row['kind'], 'last-measured')
                    self.assertEqual(row['promptTokensPerSecond'], 80)
                    self.assertEqual(row['generationTokensPerSecond'], 30)

    def test_ambient_paths_and_services_fail_closed(self):
        with fixture_environment():
            for call in (Path.home, server.default_state_dir, subprocess.Popen, socket.socket,
                         lambda: linsys.read_text('/proc/stat'),
                         lambda: probes._journal_names(Path('/tmp')),
                         lambda: probes.last_rates(Path('/tmp/last-code.log'), NOW),
                         lambda: linsys.LinuxSystem('/').snapshot()):
                with self.subTest(call=call), self.assertRaises(AmbientAccess):
                    call()

    def test_shifted_wall_clock_does_not_shift_observation_clock(self):
        for offset in (0, 86400):
            with patch.dict(os.environ, {'AGIW_FIXTURE_WALL_OFFSET': str(offset)}), fixture_environment():
                self.assertEqual(time.time(), NOW + offset)
                row = next(generate_pipeline_cases.boundary_cases())
                self.assertEqual(row['now'], NOW)
                self.assertEqual(row['snapshot']['observedUnix'], NOW)

    def test_cached_reader_matches_uncached_pairwise_file_failures(self):
        with fixture_environment():
            fixture = test_fixture_observer.FixtureObserverTest()
            fixture.fixture_parent = fixture_path('raw')
            fixture.fixture_parent.mkdir()
            fixture.setUp()
            fixture.pipeline_read_cache = {}
            fixture.pipeline_path_cache = {}
            try:
                # Exercise reuse, both orders, content restoration, and the
                # basename-wide permission injection (including another PID).
                pairs = [[], [('process-stat', 'hidden'), ('tcp6', 'malformed')],
                         [('gpu-value', 'partial'), ('memory-file', 'capped')],
                         [('gpu-inventory', 'hidden'), ('gpu-value', 'missing')],
                         [('process-exe', 'unreadable'), ('socket-visibility', 'partial')]]
                for changes in pairs + list(reversed(pairs)):
                    with self.subTest(changes=changes):
                        with mutate(fixture, changes):
                            cached = linsys.LinuxSystem(fixture.root, fixture.home).snapshot()
                        fixture.pipeline_read_cache.clear()
                        fixture.pipeline_path_cache.clear()
                        with mutate(fixture, changes):
                            fresh = linsys.LinuxSystem(fixture.root, fixture.home).snapshot()
                        self.assertEqual(cached, fresh)
            finally:
                fixture.doCleanups()
