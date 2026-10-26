"""Raw-boundary regressions complementing the cross-language source matrix."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agiw_win import probes, collect

SPEC = dict(id='fast', port=1235, alias='model-fast', device='CPU', parallel=2)
NOW = 1_800_000_000


def lane(slot_payload=None):
    def fetch(url, *_):
        if url.endswith('/health'):
            return {'status': 'ok'}
        if url.endswith('/v1/models'):
            return {'data': [{'id': SPEC['alias']}]}
        return slot_payload if slot_payload is not None else [{'id': i, 'is_processing': False} for i in range(2)]
    return probes.probe_lane(SPEC, fetch)


class RawEvidence(unittest.TestCase):
    def test_slot_envelope_incomplete_or_capped_never_reports_idle(self):
        for flags in ({'complete': False}, {'truncated': True}):
            with self.subTest(flags=flags):
                result = lane({'slots': [{'id': 0, 'is_processing': False}], **flags})
                self.assertEqual(result['status'], 'slots_unknown')
                self.assertIsNone(result['slotsBusy'])

    def test_cached_lane_future_timestamp_is_not_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = collect.Collector(Path(tmp), build={}, clock=lambda: NOW)
            self.addCleanup(collector.close)
            collector._lane_ownership['fast'] = 'allowed'
            collector._lane_owners['fast'] = {'pid': 1, 'created': 1}
            with mock.patch.object(collect, 'LanePoller') as poller:
                poller.return_value.read.return_value = (NOW + 60, lane())
                result = collector._lane(SPEC, NOW)
            self.assertEqual(result['status'], 'unknown')
            self.assertIsNone(result['slotsBusy'])
            self.assertIsNone(result['slotsTotal'])

    def test_job_history_missing_capped_and_future_manifests_are_incomplete(self):
        for mode in ('missing', 'capped', 'future'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for i in range(2):
                    folder = root / f'20261018-12000{i}'
                    folder.mkdir()
                    if mode == 'missing' and i == 1:
                        continue
                    path = folder / 'manifest.json'
                    path.write_text(json.dumps({'metrics': [{'kind': 'local', 'finish': 'stop', 'model': 'test', 'sec': 1}]}))
                    os.utime(path, (NOW + 60 if mode == 'future' else NOW,) * 2)
                with mock.patch.object(probes, 'CODEMODE_RUNS_MAX', 1 if mode == 'capped' else 20):
                    jobs, source = probes.codemode_jobs(root, [SPEC], NOW)
                self.assertEqual(jobs['availability']['state'], 'unknown')
                self.assertNotEqual(source['state'], 'live')
                if mode == 'future':
                    self.assertEqual(jobs['recent'], [])

    def test_queue_hidden_malformed_and_future_entries_are_unknown(self):
        for mode in ('hidden', 'malformed', 'future'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in ('pending', 'claimed', 'completed'):
                    (root / name).mkdir()
                target = root / 'pending/task.json'
                if mode == 'hidden':
                    target.symlink_to(root / 'missing.json')
                else:
                    target.write_text('{' if mode == 'malformed' else '{}')
                    os.utime(target, (NOW + 60 if mode == 'future' else NOW,) * 2)
                pipeline, source = probes.route_queue(root, NOW)
                self.assertIn(pipeline['status'], (None, 'unknown'))
                if pipeline['queue'] is not None:
                    self.assertFalse(pipeline['queue']['complete'])
                    for key in ('pending', 'claimed', 'completed', 'stalePending', 'staleClaimed'):
                        self.assertIsNone(pipeline['queue'][key])
                self.assertNotEqual(source['state'], 'live')
