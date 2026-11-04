"""Ambiguous lane declarations must never select a process owner."""
import unittest
from unittest.mock import patch
from agiw_win import probes


class LaneUniquenessTests(unittest.TestCase):
    def test_duplicate_ports_or_aliases_are_unprobed(self):
        for same_port in (True, False):
            lanes = {'fast': {'port': 1234, 'alias': 'fast-model'},
                     'deep': {'port': 1234 if same_port else 1236,
                              'alias': 'deep-model' if same_port else 'fast-model'}}
            with self.subTest(same_port=same_port), \
                 patch.object(probes, 'read_json', return_value={'runtime': {'lanes': lanes}}):
                specs, source = probes.lane_specs()
                self.assertEqual(source['state'], 'error')
                self.assertTrue(all(row.get('port') is None for row in specs))
