"""Sol7 regressions: hidden sockets, partial aliases and per-instance advice."""
import subprocess
import unittest

import local_callers
import mem_guard
import server
import telemetry


class Sol7Findings(unittest.TestCase):
    def test_hidden_lan_server_socket_is_not_zero_callers(self):
        for address in ('10.99.0.176', '[fd00::176]'):
            with self.subTest(address=address):
                def runner(args, **kwargs):
                    text = ('p982\ncLM Studio\nn*:1235\n' if '-sTCP:LISTEN' in args else
                            f'p9\ncCodex\nn{address}:51111->{address}:1235\n')
                    return subprocess.CompletedProcess(args, 0, text, '')
                owner = (982, '/Applications/LM Studio.app/Contents/MacOS/LM Studio')
                callers, source = local_callers.LocalCallers(runner, executable=lambda _: owner[1]).sample(
                    [{'host': 'mac', 'loaded': True, 'state': 'busy'}], port=1235, listener=owner)
                self.assertIsNone(callers)
                self.assertEqual(source['state'], 'unavailable')
                self.assertNotIn('0 local', source['detail'])

    def test_partial_alias_match_withholds_history_total(self):
        api = telemetry._parse_api({'models': [{'key': 'provider/a', 'loaded_instances': [
            {'id': 'alias-1'}, {'id': 'alias-2'}, {'id': 'alias-3'}]}]}, 1000)
        cli = telemetry._parse_lms({'models': [{'identifier': 'alias-1', 'modelKey': 'provider/a',
                                               'status': 'idle', 'queued': 0}]}, 1000)
        rows = {r['id']: r for r in api}
        telemetry._merge_cli_rows(rows, cli)
        store = server.SnapshotStore()
        store.publish({'sampledAt': 1000, 'models': list(rows.values()),
                       'sources': [{'id': 'lmstudio-api', 'state': 'live'},
                                   {'id': 'lms-ps', 'state': 'live'}]})
        result = store.read()
        self.assertIsNone(result['history'][-1]['loaded'])
        self.assertFalse(result['activityKnown'])

    def test_unloading_one_alias_keeps_distinct_route_pair(self):
        rows = [{'id': name, 'modelKey': key, 'loaded': True, 'host': 'mac', 'state': 'idle'}
                for name, key in [('a-1', 'provider/a'), ('a-2', 'provider/a'), ('b-1', 'provider/b')]]
        tips = mem_guard.suggestions([], rows, route_pair={'a-1': 'author', 'a-2': 'author', 'b-1': 'reviewer'})
        for name in ('a-1', 'a-2'):
            tip = next(t for t in tips if f'model {name} ' in t)
            self.assertNotIn('stop until', tip)
            self.assertIn('another loaded', tip)
        self.assertIn('stop until', next(t for t in tips if 'model b-1 ' in t))

    def test_wildcard_unmatched_destination_cannot_be_proved_unrelated(self):
        sockets = 'p9\ncCodex\nn192.0.2.1:51111->192.0.2.2:1235\n'
        self.assertIsNone(local_callers.parse(sockets, {982}, 1235, wildcard=True))

    def test_same_id_cli_cannot_hide_unmatched_api_instances(self):
        api = telemetry._parse_api({'models': [{'key': 'provider/a', 'loaded_instances': [
            {'id': 'provider/a'}, {'id': 'alias-2'}]}]}, 1000)
        cli = telemetry._parse_lms({'models': [{'identifier': 'provider/a', 'modelKey': 'provider/a',
                                               'status': 'idle'}]}, 1000)
        rows = {row['id']: row for row in api}
        telemetry._merge_cli_rows(rows, cli)
        self.assertFalse(server.inventory_is_known(list(rows.values()), {'lms-ps': 'live'}))


if __name__ == '__main__':
    unittest.main()
