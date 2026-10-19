"""Probe-to-publisher regressions; the exhaustive JSON-to-UI matrix runs in Node."""
import unittest
from unittest import mock

import peer_feed
import server
from invariant_pipeline import collect, NOW


class CombinedInventoryTests(unittest.TestCase):
    def test_sol7_combined_cap_withholds_counts_history_and_presence(self):
        api = {'models': [{'key': f'inactive-{i}', 'loaded_instances': []} for i in range(256)]}
        cli = {'models': [{'identifier': 'outside', 'status': 'generating'}]}
        snapshot = collect(api=api, cli=cli)
        self.assertEqual(len(snapshot['models']), 256)
        mac = snapshot['machines'][0]
        self.assertFalse(mac['modelsComplete'])
        self.assertIsNone(mac['loadedCount'])
        self.assertIsNone(mac['busyCount'])
        store = server.SnapshotStore()
        with mock.patch.object(server.time, 'time', return_value=NOW):
            store.publish(snapshot)
            published = store.read()
        self.assertEqual(published['history'][-1], {'at': NOW, 'active': None, 'loaded': None})
        with self.assertRaises(peer_feed.InventoryUnknown):
            peer_feed.mac_presence(published, 'fixture-mac')


if __name__ == '__main__':
    unittest.main()
