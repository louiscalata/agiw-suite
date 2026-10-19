"""Inventory certainty and alias counts across the observer's consumers."""
import unittest

import mem_guard
import peer_feed
import peer_link
import server
from test_peer_presence import HOST, pc_probes, snapshot


def model(model_id='alias', **fields):
    return {'id': model_id, 'name': model_id, 'modelKey': 'provider/model', 'host': 'mac',
            'loaded': True, 'state': 'idle', 'source': 'lms-ps', 'ageSeconds': 0,
            'sizeBytes': 100, **fields}


class InventoryCertainty(unittest.TestCase):
    def publish(self, rows, sources):
        store = server.SnapshotStore()
        store.publish(snapshot(models=rows, sources=sources))
        return store.read()

    def test_live_empty_cli_does_not_resolve_unknown_api_residency(self):
        sources = [{'id': 'lmstudio-api', 'state': 'live'}, {'id': 'lms-ps', 'state': 'live'}]
        data = self.publish([model(loaded=None, state='unknown', source='lmstudio-api')], sources)
        self.assertFalse(data['activityKnown'])
        self.assertIsNone(data['history'][-1]['active'])
        self.assertIsNone(data['history'][-1]['loaded'])

    def test_partial_api_inventory_does_not_publish_a_loaded_total(self):
        sources = [{'id': 'lmstudio-api', 'state': 'live'}, {'id': 'lms-ps', 'state': 'error'}]
        data = self.publish([model(), model('unknown', loaded=None)], sources)
        self.assertIsNone(data['history'][-1]['loaded'])

    def test_alias_catalog_does_not_double_history_presence_or_memory(self):
        rows = [model(), model('provider/model', representedByInstances=True, source='lmstudio-api')]
        data = self.publish(rows, [{'id': 'lms-ps', 'state': 'live'},
                                   {'id': 'lmstudio-api', 'state': 'live', 'route': 'direct'}])
        self.assertTrue(data['activityKnown'])
        self.assertEqual(data['history'][-1]['loaded'], 1)
        self.assertEqual(mem_guard._loaded_mac_models(rows), (1, 100))
        self.assertEqual(mem_guard._loaded_model_ids(rows), ['alias'])
        self.assertEqual(mem_guard._route_candidates(rows), {'provider/model'})
        self.assertEqual(mem_guard.suggestions([], rows), ['Unload idle model alias from the monitor'])
        doc = peer_feed.mac_presence(data, HOST)
        self.assertEqual(doc['modelsTotal'], 1)
        self.assertEqual([m['id'] for m in doc['models']], ['provider/model'])

    def test_memory_route_roles_follow_instance_aliases_and_distinct_weights(self):
        rows = [model(), model('alias-2'), model('review', modelKey='provider/review')]
        pair, live = server.memory_route_context({'models': rows, 'components': [
            {'id': 'nisi', 'state': 'ready', 'authorModel': 'provider/model',
             'reviewerModel': 'provider/review'}]})
        self.assertEqual(pair, {'alias': 'author', 'alias-2': 'author', 'review': 'reviewer'})
        self.assertFalse(live)
        self.assertEqual(mem_guard._route_candidates(rows), {'provider/model', 'provider/review'})
        tips = mem_guard.suggestions([], rows, route_pair=pair)
        self.assertTrue(all('Nisi' in tip for tip in tips))
        self.assertTrue(all('switch to another loaded' in tip for tip in tips if 'model alias' in tip))
        self.assertIn('stop until', next(tip for tip in tips if 'model review ' in tip))

    def test_memory_omits_unknown_inventory_and_unknown_activity_suggestions(self):
        class Guard:
            def sample(self, _gpu, models, **_context):
                return models

        rows = [model(state='loaded')]
        self.assertEqual(mem_guard.suggestions([], rows), [])
        self.assertIsNone(server.memory_sample(Guard(), {'models': rows}))
        self.assertIsNone(server.memory_sample(Guard(), {
            'models': [model(loaded=None)], 'sources': [{'id': 'lmstudio-api', 'state': 'live'}]}))
        self.assertEqual(server.memory_sample(Guard(), {
            'models': rows, 'sources': [{'id': 'lmstudio-api', 'state': 'live'}]}), rows)

    def test_loaded_only_presence_does_not_claim_downloaded_inventory_complete(self):
        for api in ('unavailable', 'error', 'recorded'):
            with self.subTest(api=api):
                data = snapshot(models=[model()], sources=[{'id': 'lms-ps', 'state': 'live'},
                                                          {'id': 'lmstudio-api', 'state': api}])
                doc = peer_feed.mac_presence(data, HOST)
                self.assertNotIn('modelsTotal', doc)
                self.assertTrue(doc['loadedListComplete'])
                projected = peer_link.project_status(doc)
                self.assertTrue(projected['loadedListComplete'])
                report = pc_probes()._validate_peer(doc, data['sampledAt'])
                self.assertFalse(report['modelsComplete'])
                self.assertTrue(report['loadedListComplete'])

    def test_loaded_list_completeness_rejects_unknown_capped_or_dropped_rows(self):
        for rows in ([model(loaded=None)], [model(str(i), modelKey=str(i)) for i in range(33)],
                     [model(None)]):
            with self.subTest(rows=len(rows)):
                try:
                    doc = peer_feed.mac_presence(snapshot(models=rows), HOST)
                except peer_feed.InventoryUnknown:
                    continue  # A1 refuses the entire unsafe publication.
                self.assertFalse(doc['loadedListComplete'])
        doc = peer_feed.mac_presence(snapshot(models=[]), HOST)
        self.assertTrue(doc['loadedListComplete'])
        for rows in ([{'id': 'a', 'loadedState': 'unknown'}], [None],
                     [{'id': str(i), 'loadedState': 'loaded'} for i in range(33)]):
            doc.update(models=rows, loadedListComplete=True)
            self.assertFalse(pc_probes()._validate_peer(doc, doc['observedUnix'])['loadedListComplete'])

    def test_caller_identity_is_retained_only_from_direct_live_inventory(self):
        owner = {'pid': 99, 'kind': 'lmstudio', 'executable': '/Applications/LM Studio.app/Contents/MacOS/LM Studio'}
        source = {'id': 'lmstudio-api', 'state': 'live', 'route': 'direct',
                  'endpoint': '127.0.0.1:1235', 'verifiedListener': owner}
        self.assertEqual(server._verified_direct_lmstudio_listener({'sources': [source]}),
                         (99, owner['executable']))
        for changes in ({'state': 'error'}, {'route': 'via-pair'}, {'verifiedListener': None},
                        {'verifiedListener': dict(owner, kind='pair')},
                        {'verifiedListener': dict(owner, pid=True)}):
            with self.subTest(changes=changes):
                self.assertIsNone(server._verified_direct_lmstudio_listener({'sources': [dict(source, **changes)]}))


if __name__ == '__main__':
    unittest.main()
