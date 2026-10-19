"""Actual proc/sysfs readers under generated file and visibility failures.

The healthy filesystem comes from the same fixture used by the end-to-end observer
regressions. File content mutations go through the real bounded readers. Visibility
failures are injected at os.open / os.readlink / scandir, never by contacting Linux.
"""
import contextlib
import copy
import errno
import itertools
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agiw_linux import collect, linsys, probes, server
from test_fixture_observer import FixtureObserverTest
from pipeline_fixture import fixture_path, forbidden

RAW_SOURCES = ('tcp4', 'tcp6', 'process-status', 'process-stat', 'process-command',
               'process-exe', 'socket-visibility', 'gpu-inventory', 'gpu-value', 'gpu-used', 'gpu-total', 'gpu-temperature', 'gpu-power', 'memory-file', 'boot-file')
RAW_MODES = ('missing', 'unreadable', 'malformed', 'partial', 'capped', 'hidden', 'stale', 'future')


class RawCollector(collect.Collector):
    def __init__(self, fixture, mutations, activity, system_snapshot):
        self.poll = 0
        self.mutations = mutations
        self.activity = activity
        encoded_system = json.dumps(system_snapshot, allow_nan=False)
        class System:
            def snapshot(self, previous):
                return json.loads(encoded_system)
        super().__init__(root=fixture.root, home=fixture.home, system=System(),
                         fetch=self.fetch, clock=lambda: 1000 + self.poll, host='raw-linux')

    def fetch(self, url, *_):
        if url.endswith('/health'):
            return {'status': 'ok'}
        if url.endswith('/v1/models'):
            return {'data': [{'id': 'gpu-code'}]}
        if url.endswith('/models'):
            return {'data': [{'id': 'pair-model', 'status': {'value': 'loaded'}}]}
        if url.endswith('/slots'):
            return [{'id': 0, 'id_task': 1, 'is_processing': self.activity == 'generating', 'n_decoded': 10 * self.poll}]
        raise AssertionError('unexpected fixture URL')

    def collect(self):
        self.poll += 1
        value = super().collect()
        for source, mode in self.mutations:
            if mode == 'stale':
                value['sampledAt'] = 1
            elif mode == 'future':
                value['sampledAt'] = 2000
        return value


@contextlib.contextmanager
def mutate(fixture, mutations):
    paths = {'tcp4': '/proc/net/tcp', 'tcp6': '/proc/net/tcp6', 'process-status': '/proc/201/status',
             'process-stat': '/proc/201/stat', 'process-command': '/proc/201/cmdline',
             'process-exe': '/proc/201/exe', 'gpu-value': fixture.device + '/gpu_busy_percent',
             'gpu-used': fixture.device + '/mem_info_vram_used', 'gpu-total': fixture.device + '/mem_info_vram_total',
             'gpu-temperature': fixture.device + '/hwmon/hwmon12/temp1_input',
             'gpu-power': fixture.device + '/hwmon/hwmon12/power1_input',
             'memory-file': '/proc/meminfo', 'boot-file': '/proc/sys/kernel/random/boot_id'}
    changed = []
    denied = set()
    hidden_links = {}
    hidden_scans = {}
    original_open, original_readlink, original_scandir = os.open, os.readlink, os.scandir
    original_read = linsys.read_text
    original_kernel_path = linsys._kernel_path
    # Cache only immutable reader inputs, never parsed observations/projections.
    # Every distinct file payload/visibility/bound is still read by the real
    # descriptor-walking reader; all pairwise parsers/collectors run unchanged.
    versions = {fixture.root / paths[source].lstrip('/'): mode
                for source, mode in mutations if source in paths and mode not in ('stale', 'future')}
    # Sysfs links and directory topology are fixed by setUp. Only the missing
    # mutation changes resolution; content/permission failures still reach the
    # real read_text and injected open. Keep missing basenames in the key so a
    # removed target cannot reuse a previously resolved path.
    missing = tuple(sorted(path.name for path, mode in versions.items() if mode == 'missing'))
    def kernel_path(root, name, tree):
        key = (root, name, tree, missing)
        if key not in fixture.pipeline_path_cache:
            fixture.pipeline_path_cache[key] = original_kernel_path(root, name, tree)
        return fixture.pipeline_path_cache[key]

    def read(path, max_bytes=65536):
        path = Path(path)
        key = (path, max_bytes, versions.get(path), path.name in denied)
        if key not in fixture.pipeline_read_cache:
            fixture.pipeline_read_cache[key] = original_read(path, max_bytes)
        return fixture.pipeline_read_cache[key]
    try:
        for source, mode in mutations:
            if mode in ('stale', 'future'):
                continue
            if source in ('gpu-inventory', 'socket-visibility'):
                hidden_scans[fixture.root / ('sys/class/drm' if source == 'gpu-inventory' else 'proc/201/fd')] = mode
                continue
            path = fixture.root / paths[source].lstrip('/')
            if source == 'process-exe':
                hidden_links[path] = mode
                continue
            if mode in ('hidden', 'unreadable'):
                # The real descriptor-walking reader receives EACCES on this basename.
                denied.add(path.name)
                continue
            before = path.read_bytes()
            changed.append((path, before))
            if mode == 'missing':
                path.unlink()
            elif mode == 'malformed':
                path.write_bytes(b'malformed\n')
            elif mode == 'partial':
                # A truncated first line has no intact record terminator.
                path.write_bytes(b'25e' if source in ('gpu-value', 'gpu-used', 'gpu-total', 'gpu-temperature', 'gpu-power') else before.split(b'\n')[0][:12])
            elif mode == 'capped':
                path.write_bytes(b'x' * (2 ** 20 + 1))
        def opened(path, *args, **kwargs):
            if str(path) in denied:
                raise PermissionError(errno.EACCES, 'fixture hidden source')
            return original_open(path, *args, **kwargs)
        def linked(path, *args, **kwargs):
            if Path(path) in hidden_links:
                mode = hidden_links[Path(path)]
                if mode == 'missing':
                    raise FileNotFoundError(errno.ENOENT, 'fixture missing executable')
                if mode in ('unreadable', 'hidden'):
                    raise PermissionError(errno.EACCES, 'fixture hidden executable')
                return '/malformed\n' if mode == 'malformed' else '/' if mode == 'partial' else '/' + 'x' * 32769
            return original_readlink(path, *args, **kwargs)
        class Scan:
            def __init__(self, path, mode):
                self.path, self.mode = path, mode
            def __enter__(self):
                return iter(self)
            def __exit__(self, *_):
                return False
            def __iter__(self):
                if self.mode == 'partial':
                    yield SimpleNamespace(path=self.path / ('card9' if self.path.name == 'drm' else '3'))
                    raise OSError(errno.EIO, 'fixture interrupted scan')
                count = 1 if self.mode == 'malformed' else 257
                for index in range(count):
                    yield SimpleNamespace(path=self.path / (f'card{index + 1000}' if self.path.name == 'drm' else str(index + 1000)))
        def scanned(path):
            if not isinstance(path, int) and Path(path) in hidden_scans:
                mode = hidden_scans[Path(path)]
                if mode == 'missing':
                    raise FileNotFoundError(errno.ENOENT, 'fixture missing directory')
                if mode in ('unreadable', 'hidden'):
                    raise PermissionError(errno.EACCES, 'fixture hidden directory')
                return Scan(Path(path), mode)
            return original_scandir(path)
        with patch.object(os, 'open', new=opened), patch.object(os, 'readlink', new=linked), \
                patch.object(os, 'scandir', new=scanned), \
                patch.object(os, 'statvfs', new=lambda _: fixture.statvfs), \
                patch.object(linsys, 'read_text', new=read), \
                patch.object(linsys, '_kernel_path', new=kernel_path):
            yield
    finally:
        for path, before in changed:
            path.write_bytes(before)


def raw_cases():
    fixture = FixtureObserverTest()
    fixture.fixture_parent = fixture_path("raw")
    fixture.fixture_parent.mkdir()
    fixture.setUp()
    fixture.pipeline_read_cache = {}
    fixture.pipeline_path_cache = {}
    original_record = probes._record
    original_names = probes._journal_names
    records, names = {}, {}
    def journal_names(directory):
        # No raw mutation changes the fixture's journal directory inventory.
        if directory not in names:
            names[directory] = original_names(directory)
        return list(names[directory])
    def record(path, max_bytes, *, tail=False):
        key = (path, max_bytes, tail)
        if key not in records:
            records[key] = original_record(path, max_bytes, tail=tail)
        return records[key]
    record_patch = patch.object(probes, '_record', new=record)
    record_patch.start()
    names_patch = patch.object(probes, '_journal_names', new=journal_names)
    names_patch.start()
    try:
        mutations = [[]]
        mutations += [[(source, mode)] for source in RAW_SOURCES for mode in RAW_MODES]
        mutations += [[(first, a), (second, b)] for first, second in itertools.combinations(RAW_SOURCES, 2)
                      for a, b in itertools.product(RAW_MODES, repeat=2)]
        # Reader evidence is identical across idle/generating and snapshot-clock
        # changes. Execute each distinct file/visibility mutation once, then feed
        # its immutable evidence through both real two-poll publishers.
        evidence = {}
        for activity in ('idle', 'generating'):
            for changes in mutations:
                try:
                    key = tuple((source, mode) for source, mode in changes if mode not in ('stale', 'future'))
                    if key not in evidence:
                        with mutate(fixture, key):
                            evidence[key] = linsys.LinuxSystem(fixture.root, fixture.home).snapshot()
                    with patch.object(probes, '_http_get', new=forbidden):
                        raw = server.once(RawCollector(fixture, changes, activity, evidence[key]), clock=lambda: 1002,
                                          sleep=lambda _: None, state_dir=fixture.root / 'no-cache')
                    import json
                    yield {'family': 'raw', 'activity': activity, 'mutations': changes,
                           'now': 1002, 'snapshot': json.loads(raw)}
                except Exception as error:
                    yield {'family': 'raw', 'activity': activity, 'mutations': changes,
                           'now': 1002, 'error': type(error).__name__ + ': ' + str(error)}
    finally:
        names_patch.stop()
        record_patch.stop()
        fixture.doCleanups()
